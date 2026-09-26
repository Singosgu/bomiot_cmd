"""
Bomiot Builder

Invokes the compiler to perform a standalone build. Core packages/modules/data
files are hardcoded as defaults; builder.toml in the project root directory is
optional and only appends extra entries on top of the defaults.

Usage:
    bomiot build

Config file (optional):
    builder.toml (project root directory)
"""

import os
import sys
import re
import json
import time
import shutil
import hashlib
import fnmatch
import platform
import subprocess
import tomlkit
import requests
from bomiot_token import encrypt_info


AUTH_URL = "https://www.bomiot.com/auth/"
ONE_MONTH_SECONDS = 30 * 24 * 3600


# ---------------------------------------------------------------------------
# Default compiler arguments (hardcoded; builder.toml can append extras).
# ---------------------------------------------------------------------------

DEFAULT_INCLUDE_PACKAGES = [
    "bomiot",
    "django",
    "fastapi",
    "flask",
    "orjson",
    "uvicorn",
    "pandas",
    "openpyxl",
    "watchdog",
    "tomlkit",
    "psutil",
    "xlsxwriter",
    "requests",
    "httptools",
    "aiofiles",
    "starlette",
    "bomiot_asgi",
    "bomiot_message",
    "bomiot_token",
    "django_filters",
    "rest_framework",
    "rest_framework_csv",
    "django_apscheduler",
    "corsheaders",
    "greaterwms",
    "PIL",
]

DEFAULT_INCLUDE_MODULES = [
    "django.core.management",
    "bomiot_cmd",
]

DEFAULT_NOFOLLOW_IMPORT_TO = [
    "pandas.tests",
    "node_modules",
]

DEFAULT_INCLUDE_DATA_FILES = [
    ".gitignore=.gitignore",
    "setup.ini=setup.ini",
    "splash.png=splash.png",
    "apps.json=apps.json",
]


# ---------------------------------------------------------------------------
# 1. Read builder.toml
# ---------------------------------------------------------------------------

def load_config():
    """Read extra build configuration from builder.toml in cwd.

    The file is optional: when absent, only the hardcoded defaults are used.
    Any list present in builder.toml is appended to the corresponding defaults.
    """
    toml_path = os.path.join(os.getcwd(), "builder.toml")
    if not os.path.exists(toml_path):
        return {}
    with open(toml_path, "r", encoding="utf-8") as f:
        data = tomlkit.parse(f.read())
    return data.get("build", {})


# ---------------------------------------------------------------------------
# 2. Read app_name and version from launcher.py
# ---------------------------------------------------------------------------

def read_launcher_meta():
    """Read app_name and version from launcher.py."""
    app_name = None
    version = None
    launcher_path = os.path.join(os.getcwd(), "launcher.py")
    if not os.path.exists(launcher_path):
        raise RuntimeError(f"launcher.py not found: {launcher_path}")
    with open(launcher_path, "r", encoding="utf-8") as f:
        for line in f:
            m = re.match(r'^app_name\s*=\s*"([^"]+)"', line)
            if m:
                app_name = m.group(1)
            m = re.match(r'^version\s*=\s*"([^"]+)"', line)
            if m:
                version = m.group(1)
    if not app_name:
        raise RuntimeError("Cannot read app_name from launcher.py")
    if not version:
        raise RuntimeError("Cannot read version from launcher.py")
    return app_name, version


# ---------------------------------------------------------------------------
# 3. Generate apps.json
# ---------------------------------------------------------------------------

def generate_apps_json():
    """Call discovered_apps.main() to generate apps.json."""
    workspace = os.getcwd()
    sys.path.insert(0, workspace)
    from discovered_apps import main
    _orig_argv = sys.argv
    sys.argv = [sys.argv[0]]
    main(workspace)
    sys.argv = _orig_argv
    print(f"[builder] apps.json generated")


# ---------------------------------------------------------------------------
# 4. Platform-specific arguments
# ---------------------------------------------------------------------------

def get_platform():
    """Return (os_label, arch_label, display_label, icon_arg)."""
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Windows":
        os_label = "windows"
        display = "Windows"
        icon_arg = "--windows-icon-from-ico=logo.ico"
    elif system == "Darwin":
        os_label = "macos"
        display = "macOS"
        icon_arg = "--macos-app-icon=logo.icns"
    else:
        os_label = "linux"
        display = "Linux"
        icon_arg = "--linux-icon=logo.png"

    if machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        arch = "x64"

    return os_label, arch, display, icon_arg


# ---------------------------------------------------------------------------
# 5. Compiler arguments
# ---------------------------------------------------------------------------

def build_compiler_args(app_name, version, os_label, icon_arg, config):
    """Assemble the compiler command-line arguments.

    Hardcoded defaults are always included; any entries in builder.toml are
    appended on top (duplicates are skipped).
    """
    args = [
        "Bomiot",  # argv[0] (display only; actual module is -m nuitka)
        f"{app_name}.py",
        "--mode=standalone",
        "--jobs=16",
        f"--company-name={app_name}",
        f"--product-name={app_name}",
        f"--file-version={version}",
        f"--product-version={version}",
        f"--copyright=Copyright (c) 2020 {app_name}",
        icon_arg,
        "--lto=yes",
        "--enable-plugin=tk-inter",
        "--module-parameter=django-settings-module=bomiot.server.server.settings",
        "--output-dir=build",
    ]

    if os_label == "windows":
        args.append("--windows-console-mode=disable")
    elif os_label == "macos":
        args.extend([
            f"--macos-app-name={app_name}",
            f"--macos-app-version={version}",
            f"--macos-signed-app-name={app_name}",
            "--macos-app-console-mode=disable",
        ])

    # Merge hardcoded defaults with extras from builder.toml (dedup).
    def _merged(defaults, extra_key):
        seen = set(defaults)
        result = list(defaults)
        for item in config.get(extra_key, []):
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    for pkg in _merged(DEFAULT_INCLUDE_PACKAGES, "include_packages"):
        args.append(f"--include-package={pkg}")
        args.append(f"--include-package-data={pkg}")

    for mod in _merged(DEFAULT_INCLUDE_MODULES, "include_modules"):
        args.append(f"--include-module={mod}")

    for mod in _merged(DEFAULT_NOFOLLOW_IMPORT_TO, "nofollow_import_to"):
        args.append(f"--nofollow-import-to={mod}")

    for data in _merged(DEFAULT_INCLUDE_DATA_FILES, "include_data_files"):
        args.append(f"--include-data-file={data}")

    return args


def run_compiler(args):
    """Run the compiler (subprocess to avoid sys.exit killing the builder)."""
    print(f"[builder] Bomiot args: {' '.join(args[1:])}")
    proc = subprocess.Popen(
        [sys.executable, "-m", "nuitka"] + args[1:],
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
        errors="replace",
    )
    for line in proc.stdout:
        # Replace the compiler brand with Bomiot in all log output.
        line = line.replace("Nuitka", "Bomiot").replace("NUITKA", "BOMIOT")
        print(line, end="")
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"Bomiot compilation failed, exit code: {proc.returncode}")


# ---------------------------------------------------------------------------
# 6. Kill leftover application processes
# ---------------------------------------------------------------------------

def kill_app_process(app_name):
    """Kill processes with the same name to release locked .pyd/.dll files."""
    import psutil
    target = app_name.lower()
    for proc in psutil.process_iter(["pid", "name"]):
        name = (proc.info.get("name") or "").lower()
        if name.startswith(target) or name.startswith(target + ".exe"):
            try:
                proc.kill()
                print(f"[builder] killed leftover process: {name} (PID {proc.info['pid']})")
            except Exception as e:
                print(f"[builder] failed to kill process {name}: {e}")


# ---------------------------------------------------------------------------
# 7. Rename output directory
# ---------------------------------------------------------------------------

def rename_dist_folder(app_name, folder_name):
    """Rename {app_name}.dist to {app_name}-{version}-{display}."""
    src = os.path.join("build", f"{app_name}.dist")
    dst = os.path.join("build", folder_name)

    if os.path.exists(dst):
        shutil.rmtree(dst)

    if os.path.isdir(src):
        shutil.move(src, dst)
        print(f"[builder] renamed: {src} -> {dst}")
    else:
        app_bundle = os.path.join("build", f"{app_name}.app")
        if os.path.isdir(app_bundle):
            shutil.move(app_bundle, dst)
            print(f"[builder] renamed (.app): {app_bundle} -> {dst}")
        else:
            print(f"[builder] warning: neither {src} nor .app bundle exists")

    # Remove node_modules from the final build output, if any.
    for root, dirs, _ in os.walk(dst):
        if "node_modules" in dirs:
            nm_path = os.path.join(root, "node_modules")
            shutil.rmtree(nm_path)
            print(f"[builder] removed: {nm_path}")
            dirs.remove("node_modules")


# ---------------------------------------------------------------------------
# 7. Generate manifest.json (for incremental updates)
# ---------------------------------------------------------------------------

BLOCK_SIZE = 1024 * 1024          # 1MB
BLOCK_THRESHOLD = 8 * 1024 * 1024  # files >= 8MB use block-level hashing


def load_gitignore(dist_dir):
    """Load .gitignore rules."""
    patterns = []
    for path in [os.path.join(dist_dir, ".gitignore"), ".gitignore"]:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        patterns.append(line)
    return patterns


def is_ignored(rel_path, patterns):
    """Check whether a file is ignored by .gitignore.

    ``node_modules`` is always excluded regardless of .gitignore rules.
    """
    # Always exclude node_modules from the build output / manifest.
    if "node_modules" in rel_path.split("/"):
        return True

    basename = os.path.basename(rel_path)
    for pattern in patterns:
        if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(basename, pattern):
            return True
        if pattern.endswith("/"):
            dir_name = pattern.rstrip("/")
            if rel_path.startswith(dir_name + "/") or ("/" + dir_name + "/") in rel_path:
                return True
    return False


def generate_manifest(app_name, version, os_label, arch, folder_name):
    """Scan build output and generate manifest.json."""
    dist_dir = os.path.join("build", folder_name)
    manifest_name = f"manifest-{os_label}-{arch}.json"

    if not os.path.isdir(dist_dir):
        raise RuntimeError(f"Build output directory does not exist: {dist_dir}")

    ignore_patterns = load_gitignore(dist_dir)
    files = {}

    for root, _, filenames in os.walk(dist_dir):
        for fn in filenames:
            if fn == manifest_name:
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, dist_dir).replace(os.sep, "/")

            if is_ignored(rel, ignore_patterns):
                continue

            file_size = os.path.getsize(full)
            if file_size >= BLOCK_THRESHOLD:
                full_hash = hashlib.sha256()
                blocks = []
                with open(full, "rb") as f:
                    while True:
                        chunk = f.read(BLOCK_SIZE)
                        if not chunk:
                            break
                        full_hash.update(chunk)
                        blocks.append(hashlib.sha256(chunk).hexdigest())
                files[rel] = {
                    "size": file_size,
                    "sha256": full_hash.hexdigest(),
                    "block_size": BLOCK_SIZE,
                    "blocks": blocks,
                }
            else:
                with open(full, "rb") as f:
                    files[rel] = hashlib.sha256(f.read()).hexdigest()

    manifest = {
        "app_name": app_name,
        "version": version,
        "os": os_label,
        "arch": arch,
        "files": files,
    }

    in_dist = os.path.join(dist_dir, manifest_name)
    with open(in_dist, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"[builder] generated {in_dist}: {len(files)} files")

    shutil.copyfile(in_dist, os.path.join("build", manifest_name))
    print(f"[builder] copied to build/{manifest_name}")


# ---------------------------------------------------------------------------
# Sponsor status check
# ---------------------------------------------------------------------------

def check_sponsor():
    """Verify sponsor status by sending encrypted keys to the auth server.

    Uses bomiot_token.encrypt_info() to generate COMMUNITY_KEY and SPONSOR_KEY,
    POSTs them as JSON to the auth endpoint, and prints the response. Based on
    the returned ``expired`` timestamp it prints a reminder when the sponsor
    time is within one month of expiring, or an expiry notice when overdue.

    Returns:
        True if the sponsor is valid and the build may proceed;
        False if expired, the response is invalid, or the request failed.
    """
    community_key = encrypt_info()
    sponsor_key = encrypt_info()

    payload = {
        "COMMUNITY_KEY": community_key,
        "SPONSOR_KEY": sponsor_key,
    }

    try:
        resp = requests.post(
            AUTH_URL,
            json=payload,
            headers={"Authed": "Bomiot"},
            timeout=10,
        )
        data = resp.json()
    except Exception as e:
        print(f"[builder] auth request failed: {e}")
        return False

    print(json.dumps(data, indent=2, ensure_ascii=False))

    expired = data.get("expired")
    if expired is None:
        print("[builder] no 'expired' field in response")
        return False

    # The server may return seconds or milliseconds; normalise to seconds.
    if expired > 1e12:
        expired = expired / 1000.0

    now = time.time()
    remaining = expired - now

    if remaining <= 0:
        print("Your Sponsor subscription has expired, please renew on the official website")
        return False

    if remaining <= ONE_MONTH_SECONDS:
        days = int(remaining // 86400)
        hours = int((remaining % 86400) // 3600)
        minutes = int((remaining % 3600) // 60)
        print(f"Your Sponsor subscription expires in {days} days {hours} hours {minutes} minutes")
    # remaining > one month: print nothing

    return True


# ---------------------------------------------------------------------------
# 8. Main flow
# ---------------------------------------------------------------------------

def build():
    """bomiot build entry point."""
    # 0. Check sponsor status; abort the build if expired or check fails.
    if not check_sponsor():
        return

    # 1. Read config
    config = load_config()

    env_app = os.environ.get("APP_NAME")
    env_version = os.environ.get("BASE_VERSION")
    if env_app and env_version:
        app_name, version = env_app, env_version
    else:
        app_name, version = read_launcher_meta()
    print(f"[builder] app: {app_name}  version: {version}")

    # 2. Generate apps.json
    generate_apps_json()

    # 3. Get platform info
    os_label, arch, display, icon_arg = get_platform()
    folder_name = f"{app_name}-{version}-{display}"
    print(f"[builder] platform: {display} ({os_label}/{arch})")

    # 4. Copy launcher.py -> {app_name}.py
    shutil.copy("launcher.py", f"{app_name}.py")

    # 5. Set environment variables
    os.environ["DJANGO_SETTINGS_MODULE"] = "bomiot.server.server.settings"
    os.environ["RUN_MAIN"] = "true"
    workspace = os.getcwd()
    sep = ";" if os_label == "windows" else ":"
    os.environ["PYTHONPATH"] = f"{workspace}{sep}{os.path.join(workspace, 'bomiot')}"
    os.environ["PYTHONIOENCODING"] = "utf-8"

    # 6. Run compiler
    args = build_compiler_args(app_name, version, os_label, icon_arg, config)
    run_compiler(args)

    # 7. Kill leftover app processes (release locked .pyd/.dll files)
    kill_app_process(app_name)

    # 8. Rename output directory
    rename_dist_folder(app_name, folder_name)

    # 8. Generate manifest.json
    generate_manifest(app_name, version, os_label, arch, folder_name)

    # 9. Clean up temp files
    temp_py = f"{app_name}.py"
    if os.path.exists(temp_py):
        os.remove(temp_py)

    print(f"\n[builder] build complete!")
    print(f"[builder] output dir: build/{folder_name}")
    print(f"[builder] manifest: build/manifest-{os_label}-{arch}.json")
