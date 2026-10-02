import os
import json
import requests
from bomiot_cmd.baseurl import baseurl


def publish(folder=""):
    """Upload build artifacts to the update server.

    Reads manifest-{os}-{arch}.json from the build/ directory, locates the
    matching output folder, reads COMMUNITY_KEY from build.json inside that
    folder, constructs the update URL as {baseurl}/{community_key}/, then
    POSTs all files (including the manifest) to that URL.

    Returns True on success, False on any error (missing folder/file, upload failure).
    """
    build_dir = os.path.join(os.getcwd(), "build")
    if not os.path.isdir(build_dir):
        print("[publisher] build/ directory not found, nothing to publish")
        return False

    # 1. Find the manifest file in build/ root (manifest-{os}-{arch}.json)
    manifest_path = None
    manifest_name = None
    for fn in os.listdir(build_dir):
        if fn.startswith("manifest-") and fn.endswith(".json"):
            manifest_path = os.path.join(build_dir, fn)
            manifest_name = fn
            break

    if not manifest_path:
        print("[publisher] manifest-{os}-{arch}.json not found in build/")
        return False

    # 2. Parse manifest to get app_name, version, os, arch
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[publisher] failed to parse manifest: {e}")
        return False

    app_name = manifest.get("app_name")
    version = manifest.get("version")
    os_label = manifest.get("os")
    arch = manifest.get("arch")

    if not all([app_name, version, os_label, arch]):
        print("[publisher] manifest missing required fields (app_name/version/os/arch)")
        return False

    print(f"[publisher] manifest: app_name={app_name}, version={version}, os={os_label}, arch={arch}")

    # 3. Locate the output folder: build/{app_name}-{version}-{display}
    #    display follows build.py's get_platform() naming (Windows/macOS/Linux).
    import sys
    _display_map = {"win32": "Windows", "darwin": "macOS"}
    _sys_os = os_label if os_label in ("Windows", "macOS", "Linux") else _display_map.get(os_label, os_label)
    folder_name = f"{app_name}-{version}-{_display_map.get(sys.platform, _sys_os)}"

    output_dir = os.path.join(build_dir, folder_name)
    if not os.path.isdir(output_dir):
        # Fallback: match by prefix (app_name-version-) in case display label differs.
        for entry in os.listdir(build_dir):
            full_path = os.path.join(build_dir, entry)
            if os.path.isdir(full_path) and entry.startswith(f"{app_name}-{version}-"):
                folder_name = entry
                output_dir = full_path
                break

    if not os.path.isdir(output_dir):
        print(f"[publisher] output folder not found for app_name={app_name}, version={version}")
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

    # 5. Construct update URL: {baseurl}/{community_key}/
    base = baseurl().rstrip("/")
    update_url = f"{base}/{community_key}/"
    print(f"[publisher] update URL: {update_url}")

    # 6. Collect all files in the output folder (including manifest)
    files_to_upload = []

    try:
        # Add the manifest file (from build/ root)
        files_to_upload.append(("manifest", manifest_name, open(manifest_path, "rb")))

        # Walk the output folder
        for root, _, filenames in os.walk(output_dir):
            for fn in filenames:
                full_path = os.path.join(root, fn)
                rel_path = os.path.relpath(full_path, output_dir).replace(os.sep, "/")
                files_to_upload.append((rel_path, rel_path, open(full_path, "rb")))

        print(f"[publisher] uploading {len(files_to_upload)} files...")

        # 7. POST all files to the update URL
        multipart = []
        for field_name, filename, fh in files_to_upload:
            multipart.append((field_name, (filename, fh, "application/octet-stream")))

        resp = requests.post(update_url, files=multipart, timeout=600)

        if resp.status_code in (200, 201):
            print(f"[publisher] upload success, server response: {resp.text[:500]}")
            return True
        else:
            print(f"[publisher] upload failed, HTTP {resp.status_code}: {resp.text[:500]}")
            return False
    except requests.RequestException as e:
        print(f"[publisher] upload error: {e}")
        return False
    except OSError as e:
        print(f"[publisher] file read error: {e}")
        return False
    finally:
        # Close all file handles
        for _, _, fh in files_to_upload:
            try:
                fh.close()
            except Exception:
                pass


if __name__ == "__main__":
    import sys
    sys.exit(0 if publish() else 1)
