import os
import json
import sys
import shutil
import zipfile
import requests
import importlib.util


def _read_launcher_meta():
    """Load launcher.py and read app_name + base_url from it.

    launcher.py is the project's entry point. Since the auto_update logic has
    been merged into it, launcher.py no longer triggers the baseurl dependency
    chain at import time, so it is safe to import directly.
    """
    launcher_path = os.path.join(os.getcwd(), "launcher.py")
    if not os.path.exists(launcher_path):
        print(f"[publisher] launcher.py not found: {launcher_path}")
        return None, None
    try:
        spec = importlib.util.spec_from_file_location("launcher", launcher_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        print(f"[publisher] failed to load launcher.py: {e}")
        return None, None
    app_name = getattr(mod, "app_name", None)
    base_url = getattr(mod, "base_url", None)
    if not app_name:
        print("[publisher] app_name not found in launcher.py")
    if not base_url:
        print("[publisher] base_url not found in launcher.py")
    return app_name, base_url


def publish(os_label, code, folder=""):
    """Package and upload build artifacts to the update server.

    Flow:
        1. Read app_name from launcher.py (cwd)
        2. Locate manifest--{app_name}-{os}-*.json in build/
        3. Parse manifest for app_name/version/os/arch
        4. Find {app_name}-{version}-{os} output folder (os case-insensitive)
        5. Read COMMUNITY_KEY/SPONSOR_KEY from build.json inside the folder
        6. POST {baseurl}/auth/{COMMUNITY_KEY}/ with manifest info; server
           returns {"msg": bool} -- True means upload is needed
        7. If upload needed: create publish/, copy manifest + entire output
           folder into it, zip both into {folder_name}.zip
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

    # 1. Read app_name and base_url from launcher.py
    app_name, base_url = _read_launcher_meta()
    if not app_name:
        return False
    if not base_url:
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
    base = base_url.rstrip("/")
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

    result = False

    # Copy manifest into publish/
    staged_manifest = os.path.join(publish_dir, manifest_name)
    shutil.copy2(manifest_path, staged_manifest)

    # Create a same-named folder inside publish/ (clear it from previous runs).
    # Use the double-dash naming convention so the path on the server matches
    # what auto_update.py requests: {app_name}--{version}-{os}/
    upload_folder_name = f"{app_name}--{version}-{manifest_os.lower()}"
    staged_folder = os.path.join(publish_dir, upload_folder_name)
    if os.path.exists(staged_folder):
        shutil.rmtree(staged_folder)
    os.makedirs(staged_folder, exist_ok=True)

    # Copy the entire output folder into the staged folder.
    shutil.copytree(output_dir, staged_folder, dirs_exist_ok=True)
    print(f"[publisher] copied all files from {output_dir} to {staged_folder}")

    # 8. Zip the folder and manifest into {upload_folder_name}.zip
    zip_path = os.path.join(publish_dir, f"{upload_folder_name}.zip")
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

    # 9. POST the zip to {baseurl}/auth/{community_key}/upload/
    upload_url = f"{base}/auth/{community_key}/upload/"
    print(f"[publisher] uploading zip to {upload_url}")

    try:
        with open(zip_path, "rb") as f:
            files = {"file": (f"{upload_folder_name}.zip", f, "application/zip")}
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

    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python publish.py <os> <code>")
        sys.exit(1)
    sys.exit(0 if publish(sys.argv[1], sys.argv[2]) else 1)
