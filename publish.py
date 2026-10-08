import os
import json
import sys
import tarfile
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
        5. POST {baseurl}/update/ with manifest info; server
           returns {"msg": bool} -- True means upload is needed
        6. If upload needed: package manifest + output folder into
           {folder_name}.tar.xz (LZMA2) directly from build/
        7. POST the tar.xz to {baseurl}/update/upload/

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

    # 5. POST {baseurl}/update/ to check if upload is needed
    base = base_url.rstrip("/")
    check_url = f"{base}/update/publish/"
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

    upload_folder_name = f"{app_name}--{version}-{manifest_os.lower()}"
    tar_filename = f"{upload_folder_name}.tar.xz"
    tar_path = os.path.join(build_dir, tar_filename)
    result = False

    try:
        print(f"[publisher] packaging tar.xz (LZMA2) from {output_dir} directly")
        with tarfile.open(tar_path, "w:xz") as tf:
            # Add manifest at the archive root
            tf.add(manifest_path, arcname=manifest_name)
            # Add all files from the build output dir, preserving the
            # {upload_folder_name}/ prefix so the server-side layout matches
            # what auto_update expects.
            for root, _, files in os.walk(output_dir):
                for fn in files:
                    full_path = os.path.join(root, fn)
                    rel = os.path.relpath(full_path, output_dir)
                    arcname = f"{upload_folder_name}/{rel}"
                    tf.add(full_path, arcname=arcname)

        tar_size = os.path.getsize(tar_path)
        print(f"[publisher] tar.xz created: {tar_path} ({tar_size / 1048576:.1f} MB)")

        # POST the tar.xz to {baseurl}/update/upload/
        upload_url = f"{base}/update/upload/"
        print(f"[publisher] uploading tar.xz to {upload_url}")

        with open(tar_path, "rb") as f:
            files = {"file": (tar_filename, f, "application/x-xz")}
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
        print(f"[publisher] file operation error: {e}")
    finally:
        if os.path.exists(tar_path):
            os.remove(tar_path)
            print(f"[publisher] cleaned up {tar_path}")

    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python publish.py <os> <code>")
        sys.exit(1)
    sys.exit(0 if publish(sys.argv[1], sys.argv[2]) else 1)
