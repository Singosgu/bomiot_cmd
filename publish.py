import os
import json
import sys
import shutil
import zipfile
import importlib.util
import requests
from bomiot_cmd.baseurl import baseurl


def _read_launcher_app_name():
    """Read app_name from launcher.py in the current working directory.

    launcher.py defines ``app_name`` as a module-level variable, so we load
    the file as a module and read the attribute directly instead of parsing
    with regex.
    """
    launcher_path = os.path.join(os.getcwd(), "launcher.py")
    if not os.path.exists(launcher_path):
        print(f"[publisher] launcher.py not found: {launcher_path}")
        return None
    try:
        spec = importlib.util.spec_from_file_location("launcher", launcher_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        print(f"[publisher] failed to load launcher.py: {e}")
        return None
    app_name = getattr(module, "app_name", None)
    if not app_name:
        print("[publisher] app_name not found in launcher.py")
        return None
    return app_name


def publish(os_label, code, folder=""):
    """Package and upload build artifacts to the update server.

    Flow:
        1. Read app_name from launcher.py (cwd)
        2. Locate manifest--{app_name}--{os}--*.json in build/
        3. Parse manifest for app_name/version/os/arch
        4. Find {app_name}-{version}-{os} output folder (os case-insensitive)
        5. Read COMMUNITY_KEY/SPONSOR_KEY from build.json inside the folder
        6. POST {baseurl}/auth/{COMMUNITY_KEY}/ with manifest info; server
           returns {"msg": bool} -- True means upload is needed
        7. If upload needed: create publish/, move manifest + output folder
           into it, zip both into {folder_name}.zip
        8. POST the zip to {baseurl}/auth/{COMMUNITY_KEY}/upload/

    Args:
        os_label: OS name (windows/macos/linux), used to locate the manifest
        code:     user-defined verification code (sent with the upload POST)

    Returns True on success (or skip), False on error.
    """
    build_dir = os.path.join(os.getcwd(), "build")
    if not os.path.isdir(build_dir):
        print("[publisher] build/ directory not found, nothing to publish")
        return False

    # 1. Read app_name from launcher.py
    app_name = _read_launcher_app_name()
    if not app_name:
        return False

    # 2. Locate manifest--{app_name}--{os}--*.json in build/
    manifest_path = None
    manifest_name = None
    prefix = f"manifest--{app_name}-{os_label}-"
    for fn in os.listdir(build_dir):
        if fn.startswith(prefix) and fn.endswith(".json"):
            manifest_path = os.path.join(build_dir, fn)
            manifest_name = fn
            break

    if not manifest_path:
        print(f"[publisher] manifest not found in build/ for app_name={app_name}, os={os_label}")
        return False

    print(f"[publisher] manifest: {manifest_name}")

    # 3. Parse manifest
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

    if not all([app_name, version, manifest_os, arch]):
        print("[publisher] manifest missing required fields (app_name/version/os/arch)")
        return False

    print(f"[publisher] manifest info: app_name={app_name}, version={version}, os={manifest_os}, arch={arch}")

    # 4. Find {app_name}-{version}-{os} output folder (os case-insensitive)
    output_dir = None
    folder_name = None
    for entry in os.listdir(build_dir):
        full_path = os.path.join(build_dir, entry)
        if not os.path.isdir(full_path):
            continue
        if entry.lower() == f"{app_name}-{version}-{os_label}".lower():
            folder_name = entry
            output_dir = full_path
            break

    if not output_dir:
        print(f"[publisher] output folder not found for {app_name}-{version}-{os_label}")
        return False

    print(f"[publisher] output folder: {output_dir}")

    # 5. Read COMMUNITY_KEY and SPONSOR_KEY from build.json
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
    sponsor_key = build_info.get("SPONSOR_KEY")
    if not community_key:
        print("[publisher] COMMUNITY_KEY not found in build.json")
        return False

    # 6. POST {baseurl}/auth/{community_key}/ to check if upload is needed
    base = baseurl().rstrip("/")
    check_url = f"{base}/auth/{community_key}/"
    print(f"[publisher] checking upload permission: POST {check_url}")

    payload = {
        "app_name": app_name,
        "version": version,
        "os": manifest_os,
        "arch": arch,
        "code": code,
    }

    try:
        resp = requests.post(check_url, json=payload, timeout=30)
        if resp.status_code != 200:
            print(f"[publisher] server check failed, HTTP {resp.status_code}: {resp.text[:500]}")
            return False

        try:
            server_info = resp.json()
        except (ValueError, json.JSONDecodeError):
            print(f"[publisher] server returned non-JSON response: {resp.text[:500]}")
            return False

        can_upload = server_info.get("msg")
        print(f"[publisher] server msg={can_upload}")

        if can_upload is False:
            print("[publisher] server rejected upload, skipping")
            return True
        if can_upload is not True:
            print(f"[publisher] unexpected server response: {server_info}")
            return False
    except requests.RequestException as e:
        print(f"[publisher] server check error: {e}")
        return False

    # 7. Create publish/ folder (remove any leftover from previous runs first)
    publish_dir = os.path.join(os.getcwd(), "publish")
    if os.path.exists(publish_dir):
        shutil.rmtree(publish_dir, ignore_errors=True)
    os.makedirs(publish_dir, exist_ok=True)

    # All steps below must clean up publish/ on exit (success or failure).
    result = False
    try:
        # Copy manifest into publish/
        staged_manifest = os.path.join(publish_dir, manifest_name)
        shutil.copy2(manifest_path, staged_manifest)

        # Create a same-named folder inside publish/ (clear it from previous runs).
        staged_folder = os.path.join(publish_dir, folder_name)
        if os.path.exists(staged_folder):
            shutil.rmtree(staged_folder)
        os.makedirs(staged_folder, exist_ok=True)

        # Determine which files differ from a reference manifest.
        local_files = manifest.get("files", {})
        reference_files = {}

        def _entry_hash(entry):
            """Return the sha256 of a manifest entry (str for small files, dict for big)."""
            if isinstance(entry, dict):
                return entry.get("sha256")
            return entry

        # Try the server manifest first:
        #   {baseurl}/media/update/{COMMUNITY_KEY}/{app_name}/{manifest_name}
        server_manifest_url = f"{base}/media/update/{community_key}/{app_name}/{manifest_name}"
        print(f"[publisher] fetching server manifest: {server_manifest_url}")
        try:
            resp = requests.get(server_manifest_url, timeout=30)
            if resp.status_code == 200:
                try:
                    reference_files = resp.json().get("files", {})
                    print("[publisher] using server manifest as reference")
                except (ValueError, json.JSONDecodeError):
                    print("[publisher] server returned non-JSON manifest, ignoring")
            else:
                print(f"[publisher] server manifest not found (HTTP {resp.status_code})")
        except requests.RequestException as e:
            print(f"[publisher] failed to fetch server manifest: {e}")

        # Fall back to bomiot's built-in manifest if the server has none.
        if not reference_files:
            import bomiot
            bomiot_dir = os.path.dirname(bomiot.__file__)
            py_ver = f"{sys.version_info.major}{sys.version_info.minor}"
            os_lower = manifest_os.lower()
            bomiot_manifest_dir = os.path.join(
                bomiot_dir, "cmd", "file", "manifest", os_lower, py_ver
            )
            bomiot_manifest_path = None
            if os.path.isdir(bomiot_manifest_dir):
                for fn in os.listdir(bomiot_manifest_dir):
                    if fn.endswith(".json"):
                        bomiot_manifest_path = os.path.join(bomiot_manifest_dir, fn)
                        break
            if bomiot_manifest_path:
                try:
                    with open(bomiot_manifest_path, "r", encoding="utf-8") as f:
                        reference_files = json.load(f).get("files", {})
                    print(f"[publisher] using bomiot built-in manifest: {bomiot_manifest_path}")
                except (OSError, json.JSONDecodeError) as e:
                    print(f"[publisher] failed to parse bomiot manifest: {e}")
            else:
                print(f"[publisher] bomiot built-in manifest not found at {bomiot_manifest_dir}")

        # Copy only files that are new or differ from the reference manifest.
        copied_count = 0
        for rel_path, local_entry in local_files.items():
            local_hash = _entry_hash(local_entry)
            ref_entry = reference_files.get(rel_path)
            ref_hash = _entry_hash(ref_entry) if ref_entry is not None else None
            if ref_hash is None or local_hash != ref_hash:
                src = os.path.join(output_dir, rel_path)
                dst = os.path.join(staged_folder, rel_path)
                if not os.path.exists(src):
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                copied_count += 1

        print(f"[publisher] copied {copied_count} changed/new files")

        # 8. Zip the folder and manifest into {folder_name}.zip
        zip_path = os.path.join(publish_dir, f"{folder_name}.zip")
        print(f"[publisher] packaging {zip_path}")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_LZMA) as zf:
            # Add the manifest file at the root of the zip
            zf.write(staged_manifest, arcname=manifest_name)
            # Add the staged folder (with all its contents)
            for root, dirs, files in os.walk(staged_folder):
                for fn in files:
                    full_path = os.path.join(root, fn)
                    arcname = os.path.relpath(full_path, publish_dir)
                    zf.write(full_path, arcname=arcname)

        print(f"[publisher] zip created: {zip_path}")

        # Clean up staged files, keep only the zip in publish/
        try:
            os.remove(staged_manifest)
        except OSError:
            pass
        shutil.rmtree(staged_folder, ignore_errors=True)

        # 9. POST the zip to {baseurl}/auth/{community_key}/upload/
        upload_url = f"{base}/auth/{community_key}/upload/"
        print(f"[publisher] uploading zip to {upload_url}")

        try:
            with open(zip_path, "rb") as f:
                files = {"file": (f"{folder_name}.zip", f, "application/zip")}
                data = {"code": code} if code else None
                resp = requests.post(upload_url, files=files, data=data, timeout=600)

            if resp.status_code not in (200, 201):
                print(f"[publisher] upload failed, HTTP {resp.status_code}: {resp.text[:500]}")
            else:
                print("[publisher] upload success")
                result = True
        except requests.RequestException as e:
            print(f"[publisher] upload error: {e}")
        except OSError as e:
            print(f"[publisher] failed to read zip: {e}")
    finally:
        # Always remove the entire publish/ folder, whether the flow
        # succeeded, failed, or raised an exception.
        shutil.rmtree(publish_dir, ignore_errors=True)
    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python publish.py <os> <code>")
        sys.exit(1)
    sys.exit(0 if publish(sys.argv[1], sys.argv[2]) else 1)
