"""
Bomiot Builder - Nuitka 构建脚本

从项目根目录的 builder.toml 读取配置，调用 Nuitka 进行 standalone 编译。

用法:
    bomiot build

配置文件:
    builder.toml (项目根目录)
"""

import os
import sys
import re
import json
import shutil
import hashlib
import fnmatch
import platform
import subprocess
import tomlkit


# ---------------------------------------------------------------------------
# 1. 读取 builder.toml
# ---------------------------------------------------------------------------

def load_config():
    """从 cwd 的 builder.toml 读取构建配置"""
    toml_path = os.path.join(os.getcwd(), "builder.toml")
    if not os.path.exists(toml_path):
        raise RuntimeError(
            f"未找到 builder.toml: {toml_path}\n"
            "请在项目根目录创建 builder.toml 配置文件"
        )
    with open(toml_path, "r", encoding="utf-8") as f:
        data = tomlkit.parse(f.read())
    return data["build"]


# ---------------------------------------------------------------------------
# 2. 读取 app_name 和 version（从 launcher.py）
# ---------------------------------------------------------------------------

def read_launcher_meta():
    """从 launcher.py 读取 app_name 和 version"""
    app_name = None
    version = None
    launcher_path = os.path.join(os.getcwd(), "launcher.py")
    if not os.path.exists(launcher_path):
        raise RuntimeError(f"未找到 launcher.py: {launcher_path}")
    with open(launcher_path, "r", encoding="utf-8") as f:
        for line in f:
            m = re.match(r'^app_name\s*=\s*"([^"]+)"', line)
            if m:
                app_name = m.group(1)
            m = re.match(r'^version\s*=\s*"([^"]+)"', line)
            if m:
                version = m.group(1)
    if not app_name:
        raise RuntimeError("无法从 launcher.py 读取 app_name")
    if not version:
        raise RuntimeError("无法从 launcher.py 读取 version")
    return app_name, version


# ---------------------------------------------------------------------------
# 3. 生成 apps.json
# ---------------------------------------------------------------------------

def generate_apps_json():
    """调用 discovered_apps.main() 生成 apps.json"""
    workspace = os.getcwd()
    sys.path.insert(0, workspace)
    from discovered_apps import main
    _orig_argv = sys.argv
    sys.argv = [sys.argv[0]]
    main(workspace)
    sys.argv = _orig_argv
    print(f"[builder] apps.json 已生成")


# ---------------------------------------------------------------------------
# 4. 平台相关参数
# ---------------------------------------------------------------------------

def get_platform():
    """返回 (os_label, arch_label, display_label, icon_arg)"""
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
# 5. Nuitka 构建
# ---------------------------------------------------------------------------

def build_nuitka_args(app_name, version, os_label, icon_arg, config):
    """组装 Nuitka 命令行参数"""
    args = [
        "nuitka",  # argv[0]
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

    for pkg in config["include_packages"]:
        args.append(f"--include-package={pkg}")
        args.append(f"--include-package-data={pkg}")

    for mod in config.get("include_modules", []):
        args.append(f"--include-module={mod}")

    for mod in config.get("nofollow_import_to", []):
        args.append(f"--nofollow-import-to={mod}")

    for data in config.get("include_data_files", []):
        args.append(f"--include-data-file={data}")

    return args


def run_nuitka(args):
    """调用 Nuitka 进行编译（子进程方式，避免 sys.exit 终止 builder）"""
    print(f"[builder] Nuitka 参数: {' '.join(args[1:])}")
    result = subprocess.run(
        [sys.executable, "-m", "nuitka"] + args[1:],
        env=os.environ.copy()
    )
    if result.returncode != 0:
        raise RuntimeError(f"Nuitka 编译失败，退出码: {result.returncode}")


# ---------------------------------------------------------------------------
# 6. 杀掉残留的应用进程
# ---------------------------------------------------------------------------

def kill_app_process(app_name):
    """杀掉同名应用进程，释放被占用的 .pyd/.dll 文件"""
    import psutil
    target = app_name.lower()
    for proc in psutil.process_iter(["pid", "name"]):
        name = (proc.info.get("name") or "").lower()
        if name.startswith(target) or name.startswith(target + ".exe"):
            try:
                proc.kill()
                print(f"[builder] 杀掉残留进程: {name} (PID {proc.info['pid']})")
            except Exception as e:
                print(f"[builder] 杀掉进程失败 {name}: {e}")


# ---------------------------------------------------------------------------
# 7. 重命名输出目录
# ---------------------------------------------------------------------------

def rename_dist_folder(app_name, folder_name):
    """将 {app_name}.dist 重命名为 {app_name}-{version}-{display}"""
    src = os.path.join("build", f"{app_name}.dist")
    dst = os.path.join("build", folder_name)

    if os.path.exists(dst):
        shutil.rmtree(dst)

    if os.path.isdir(src):
        shutil.move(src, dst)
        print(f"[builder] 重命名: {src} -> {dst}")
    else:
        app_bundle = os.path.join("build", f"{app_name}.app")
        if os.path.isdir(app_bundle):
            shutil.move(app_bundle, dst)
            print(f"[builder] 重命名(.app): {app_bundle} -> {dst}")
        else:
            print(f"[builder] 警告: {src} 和 .app bundle 都不存在")


# ---------------------------------------------------------------------------
# 7. 生成 manifest.json（增量更新用）
# ---------------------------------------------------------------------------

BLOCK_SIZE = 1024 * 1024          # 1MB
BLOCK_THRESHOLD = 8 * 1024 * 1024  # >=8MB 的文件用块级哈希


def load_gitignore(dist_dir):
    """加载 .gitignore 规则"""
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
    """检查文件是否被 .gitignore 忽略"""
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
    """扫描构建产物，生成 manifest.json"""
    dist_dir = os.path.join("build", folder_name)
    manifest_name = f"manifest-{os_label}-{arch}.json"

    if not os.path.isdir(dist_dir):
        raise RuntimeError(f"构建产物目录不存在: {dist_dir}")

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
    print(f"[builder] 生成 {in_dist}: {len(files)} 个文件")

    shutil.copyfile(in_dist, os.path.join("build", manifest_name))
    print(f"[builder] 复制到 build/{manifest_name}")


# ---------------------------------------------------------------------------
# 8. 主流程
# ---------------------------------------------------------------------------

def build():
    """bomiot build 入口"""
    # 1. 读取配置
    config = load_config()

    env_app = os.environ.get("APP_NAME")
    env_version = os.environ.get("BASE_VERSION")
    if env_app and env_version:
        app_name, version = env_app, env_version
    else:
        app_name, version = read_launcher_meta()
    print(f"[builder] 应用: {app_name}  版本: {version}")

    # 2. 生成 apps.json
    generate_apps_json()

    # 3. 获取平台信息
    os_label, arch, display, icon_arg = get_platform()
    folder_name = f"{app_name}-{version}-{display}"
    print(f"[builder] 平台: {display} ({os_label}/{arch})")

    # 4. 复制 launcher.py -> {app_name}.py
    shutil.copy("launcher.py", f"{app_name}.py")

    # 5. 设置环境变量
    os.environ["DJANGO_SETTINGS_MODULE"] = "bomiot.server.server.settings"
    os.environ["RUN_MAIN"] = "true"
    workspace = os.getcwd()
    sep = ";" if os_label == "windows" else ":"
    os.environ["PYTHONPATH"] = f"{workspace}{sep}{os.path.join(workspace, 'bomiot')}"
    os.environ["PYTHONIOENCODING"] = "utf-8"

    # 6. 运行 Nuitka
    args = build_nuitka_args(app_name, version, os_label, icon_arg, config)
    run_nuitka(args)

    # 7. 杀掉残留的应用进程（释放被占用的 .pyd/.dll）
    kill_app_process(app_name)

    # 8. 重命名输出目录
    rename_dist_folder(app_name, folder_name)

    # 8. 生成 manifest.json
    generate_manifest(app_name, version, os_label, arch, folder_name)

    # 9. 清理临时文件
    temp_py = f"{app_name}.py"
    if os.path.exists(temp_py):
        os.remove(temp_py)

    print(f"\n[builder] 构建完成!")
    print(f"[builder] 产物目录: build/{folder_name}")
    print(f"[builder] Manifest: build/manifest-{os_label}-{arch}.json")
