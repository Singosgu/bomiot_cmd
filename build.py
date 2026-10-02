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

# Only packages that ship static/template/locale data need --include-package-data.
# Packages without data files (openpyxl, xlsxwriter, requests, aiofiles, etc.)
# only use --include-package, avoiding FileNotFoundError on stray references.
DEFAULT_INCLUDE_PACKAGE_DATA = [
    "bomiot",
    "django",
    "greaterwms",
    "PIL",
    "rest_framework",
    "django_filters",
    "corsheaders",
]

DEFAULT_INCLUDE_MODULES = [
    "django.core.management",
    "bomiot_cmd",
]

DEFAULT_NOFOLLOW_IMPORT_TO = [
    "pandas.tests",
    "django.test",
    "rest_framework.tests",
    "IPython",
    "pytest",
    "unittest",
    "node_modules",
    "src",
    "public"
]

DEFAULT_INCLUDE_DATA_FILES = [
    ".gitignore=.gitignore",
    "setup.ini=setup.ini",
    "splash.png=splash.png",
    "apps.json=apps.json",
    "greaterwms/server.py=greaterwms/server.py",
    "greaterwms/receiver.py=greaterwms/receiver.py",
    "greaterwms/files.py=greaterwms/files.py",
    "greaterwms/task.py=greaterwms/task.py"
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

    jobs = max(1, (os.cpu_count() or 4) - 1)

    args = [
        "Bomiot",  # argv[0] (display only; actual module is -m nuitka)
        f"{app_name}.py",
        "--mode=standalone",
        "--assume-yes-for-downloads",
        "--show-progress",
        "--lto=yes",
        f"--jobs={jobs}",
        f"--company-name=Bomiot",
        f"--product-name={app_name}",
        f"--file-description=No-environment delivery tool for Python apps Compile to binary · Cross-platform · Incremental updates · Source code protection",
        f"--file-version={version}",
        f"--product-version={version}",
        f"--copyright=Copyright (c) 2020 {app_name}",
        icon_arg,
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

    code_pkgs = set(_merged(DEFAULT_INCLUDE_PACKAGES, "include_packages"))
    data_pkgs = set(_merged(DEFAULT_INCLUDE_PACKAGE_DATA, "include_package_data"))

    # Iterate the union so a package can need --include-package-data without
    # --include-package (data-only packages) or vice versa.
    for pkg in code_pkgs | data_pkgs:
        if pkg in code_pkgs:
            args.append(f"--include-package={pkg}")
        if pkg in data_pkgs:
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
    Under ``templates/`` only files inside a ``dist/`` subdirectory are kept;
    everything else in ``templates/`` is ignored.
    """
    parts = rel_path.split("/")

    # Always exclude node_modules from the build output / manifest.
    if "node_modules" in parts:
        return True

    # Under templates/ only keep files that live in some dist/ subdirectory.
    if "templates" in parts:
        idx = parts.index("templates")
        if "dist" not in parts[idx:]:
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
    manifest_name = f"manifest--{app_name}-{os_label}-{arch}.json"

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

def check_sponsor(payload):
    """Sponsor verification placeholder.

    The auth server request is disabled. The payload (COMMUNITY_KEY and
    SPONSOR_KEY) is still generated and written to build.json so that
    downstream consumers can use it, but no network call is made.

    Args:
        payload: dict with "COMMUNITY_KEY" and "SPONSOR_KEY".

    Returns:
        Always True so the build proceeds.
    """
    print(f"[builder] sponsor check skipped (no auth request)")
    return True


# ---------------------------------------------------------------------------
# 8. Main flow
# ---------------------------------------------------------------------------

def build():
    """bomiot build entry point."""
    start_time = time.time()

    def _print_elapsed():
        elapsed = time.time() - start_time
        mins = int(elapsed // 60)
        secs = elapsed % 60
        print(f"\n[builder] total build time: {mins} min {secs:.1f} sec")

    try:
        # 0. Generate auth keys and check sponsor status.
        # CI (GitHub Actions / Gitee Go): use keys from environment variables.
        # Local / dev: generate keys via encrypt_info().
        is_github = os.environ.get("GITHUB_ACTIONS") == "true"
        is_gitee = bool(
            os.environ.get("GITEE_PIPELINE_NAME")
            or os.environ.get("GITEE_REPO")
        )

        if is_github or is_gitee:
            # Keys are injected into env by the workflow (deploy.py writes them
            # into greaterwms.yaml env section).
            community_key = os.environ.get("COMMUNITY_KEY", "")
            sponsor_key = os.environ.get("SPONSOR_KEY", "")
            if not community_key or not sponsor_key:
                msg = "[builder] CI environment detected but COMMUNITY_KEY / SPONSOR_KEY not set"
                print(msg)
                raise RuntimeError(msg)
        else:
            community_key, sponsor_key = encrypt_info()

        payload = {
            "COMMUNITY_KEY": community_key,
            "SPONSOR_KEY": sponsor_key,
        }
        if not check_sponsor(payload):
            # In CI, fail the build loudly so the workflow turns red.
            # Locally, exit silently after printing the expiry notice.
            if is_github or is_gitee:
                raise RuntimeError("Sponsor verification failed in CI; build aborted")
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

        # 10. Write build.json (auth payload) into the output directory root.
        output_dir = os.path.join("build", folder_name)
        build_json_path = os.path.join(output_dir, "build.json")
        with open(build_json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[builder] build.json: {build_json_path}")

        print(f"\n[builder] build complete!")
        print(f"[builder] output dir: build/{folder_name}")
        print(f"[builder] manifest: build/manifest-{os_label}-{arch}.json")
    finally:
        _print_elapsed()
