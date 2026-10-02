import os
import json
import sys
import requests
from bomiot_cmd.baseurl import baseurl


def publish(os_label, code, folder=""):
    """Upload build artifacts to the update server.

    Flow:
        1. Locate manifest-{os}-{arch}.json in build/ by os_label
        2. Find the {app_name}-{version}-{os_label} output folder
        3. Read COMMUNITY_KEY from build.json
        4. GET {baseurl}/auth/{community_key} to fetch server's app_name/version
        5. If server version matches local manifest, skip upload
        6. Otherwise POST all files (excluding media/) in batches

    Args:
        os_label: OS name (Windows/macOS/Linux), used to locate manifest
        code:     upload authentication code, sent with each POST

    Returns True on success (or skip), False on error.
    """
    build_dir = os.path.join(os.getcwd(), "build")
    if not os.path.isdir(build_dir):
        print("[publisher] build/ directory not found, nothing to publish")
        return False

    # 1. Find the manifest file matching the given os: manifest--{app_name}-{os}-{arch}.json
    manifest_path = None
    manifest_name = None
    for fn in os.listdir(build_dir):
        if fn.startswith("manifest--") and fn.endswith(".json") and f"-{os_label}-" in fn:
            manifest_path = os.path.join(build_dir, fn)
            manifest_name = fn
            break

    if not manifest_path:
        print(f"[publisher] manifest-{os_label}-{{arch}}.json not found in build/")
        return False

    # 2. Parse manifest to get app_name, version
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[publisher] failed to parse manifest: {e}")
        return False

    app_name = manifest.get("app_name")
    version = manifest.get("version")
    manifest_os = manifest.get("os")
    arch = manifest.get("arch")

    if not all([app_name, version]):
        print("[publisher] manifest missing required fields (app_name/version)")
        return False

    print(f"[publisher] local manifest: app_name={app_name}, version={version}, os={manifest_os}, arch={arch}")

    # 3. Locate the output folder: build/{app_name}-{version}-{os_label}
    folder_name = f"{app_name}-{version}-{os_label}"
    output_dir = os.path.join(build_dir, folder_name)

    if not os.path.isdir(output_dir):
        # Fallback: match by prefix (app_name-version-)
        for entry in os.listdir(build_dir):
            full_path = os.path.join(build_dir, entry)
            if os.path.isdir(full_path) and entry.startswith(f"{app_name}-{version}-"):
                folder_name = entry
                output_dir = full_path
                break

    if not os.path.isdir(output_dir):
        print(f"[publisher] output folder not found for app_name={app_name}, version={version}, os={os_label}")
        return False

    print(f"[publisher] output folder: {output_dir}")

    # 4. Read COMMUNITY_KEY from build.json inside the output folder
    build_json_path = os.path.join(output_dir, "build.json")
    if not os.path.exists(build_json_path):
        print("[publisher] build.json not found in output folder")
        return False

    try:
        with open(build_json_path, "r", encoding="utf-8") as f:
            build_info = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[publisher] failed to parse build.json: {e}")
        return False

    community_key = build_info.get("COMMUNITY_KEY")
    if not community_key:
        print("[publisher] COMMUNITY_KEY not found in build.json")
        return False

    # 5. GET {baseurl}/auth/{community_key} to check server version
    base = baseurl().rstrip("/")
    check_url = f"{base}/auth/{community_key}/"
    print(f"[publisher] checking server version: GET {check_url}")

    try:
        resp = requests.get(check_url, timeout=30)
        if resp.status_code == 404:
            print("[publisher] server returned 404, treating as no existing version, will upload")
        elif resp.status_code != 200:
            print(f"[publisher] server check failed, HTTP {resp.status_code}: {resp.text[:500]}")
            return False

        if resp.status_code == 200:
            try:
                server_info = resp.json()
            except (ValueError, json.JSONDecodeError):
                print(f"[publisher] server returned non-JSON response: {resp.text[:500]}")
                return False
            server_app = server_info.get("app_name")
            server_version = server_info.get("version")
            print(f"[publisher] server: app_name={server_app}, version={server_version}")

            # 6. If server already has the same version, skip upload
            if server_app == app_name and server_version == version:
                print("[publisher] server already has this version, skipping upload")
                return True
    except requests.RequestException as e:
        print(f"[publisher] server check error: {e}")
        return False

    # 7. Upload all files (excluding media/) in batches
    update_url = f"{base}/auth/{community_key}/"
    print(f"[publisher] update URL: {update_url}")

    file_entries = []

    # Add the manifest file (from build/ root)
    file_entries.append(("manifest", manifest_name, manifest_path))

    # Walk the output folder, skip media/ (handled on server side)
    skip_dirs = {"media"}
    for root, dirs, filenames in os.walk(output_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in filenames:
            full_path = os.path.join(root, fn)
            rel_path = os.path.relpath(full_path, output_dir).replace(os.sep, "/")
            file_entries.append((rel_path, rel_path, full_path))

    total = len(file_entries)
    print(f"[publisher] uploading {total} files...")

    BATCH_SIZE = 100
    data = {"code": code} if code else None
    success = True

    for batch_start in range(0, total, BATCH_SIZE):
        batch = file_entries[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

        multipart = []
        open_handles = []
        try:
            for field_name, filename, full_path in batch:
                fh = open(full_path, "rb")
                open_handles.append(fh)
                multipart.append((field_name, (filename, fh, "application/octet-stream")))

            resp = requests.post(update_url, files=multipart, data=data, timeout=600)

            if resp.status_code not in (200, 201):
                print(f"[publisher] batch {batch_num}/{total_batches} failed, HTTP {resp.status_code}: {resp.text[:500]}")
                success = False
                break

            print(f"[publisher] batch {batch_num}/{total_batches} ok ({len(batch)} files)")
        except requests.RequestException as e:
            print(f"[publisher] batch {batch_num}/{total_batches} upload error: {e}")
            success = False
            break
        except OSError as e:
            print(f"[publisher] batch {batch_num}/{total_batches} file read error: {e}")
            success = False
            break
        finally:
            for fh in open_handles:
                try:
                    fh.close()
                except Exception:
                    pass

    if success:
        print("[publisher] upload success")
    return success


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python publish.py <os> <code>")
        sys.exit(1)
    sys.exit(0 if publish(sys.argv[1], sys.argv[2]) else 1)
