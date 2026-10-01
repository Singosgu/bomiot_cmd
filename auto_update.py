"""
Bomiot 增量自动更新模块。

从 launcher.py 剥离而来，不包含任何 GUI / Django 启动逻辑，
只负责：下载远程 manifest → 对比 → 增量下载 → 生成更新脚本 → 返回是否触发更新。
"""

import os
import sys
import json
import time
import hashlib
import fnmatch
import platform
import subprocess
import tempfile
import shutil
import socket
import urllib.request
import urllib.error
from time import sleep
from bomiot_cmd.baseurl import baseurl


# === Incremental update config (change to your actual address) ===
# Base URL of the remote releases directory; launcher auto-appends manifest and version file paths
# Layout: {UPDATE_URL}manifest-{os}-{arch}.json  and  {UPDATE_URL}GreaterWMS-{version}-{Platform}/
BASE_URL = baseurl()
UPDATE_URL = f"{BASE_URL}auth/"
# Normalize: ensure UPDATE_URL ends with "/" (keep empty if empty) to avoid 404 from a missing "/" when concatenating
if UPDATE_URL:
    UPDATE_URL = UPDATE_URL.rstrip("/") + "/"


# Block-level incremental update parameters
# Files larger than BLOCK_THRESHOLD are recorded in block format in the manifest,
# so updates only download blocks whose hash differs (HTTP Range), avoiding re-downloading the whole file.
BLOCK_SIZE = 0.25 * 1024 * 1024          # each block is 1 MiB
BLOCK_THRESHOLD = 1 * 1024 * 1024  # enable block mode for files >= 8 MiB


def _detect_platform():
    """Detect the current platform, return (os_str, arch_str, platform_display)"""
    if sys.platform == "win32":
        _os = "windows"
        _display = "Windows"
    elif sys.platform == "darwin":
        _os = "macos"
        _display = "macOS"
    else:
        _os = "linux"
        _display = "Linux"
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        _arch = "arm64"
    elif machine.startswith("armv"):
        _arch = "armv7"
    else:
        _arch = "x64"
    return _os, _arch, _display


def _app_dir():
    """Return the directory where launcher resides (launcher.dist/)"""
    return os.path.dirname(sys.executable)


def _load_community_key(app_dir):
    """Read COMMUNITY_KEY from build.json generated during the build step."""
    build_json_path = os.path.join(app_dir, "build.json")
    if not os.path.exists(build_json_path):
        return None
    try:
        with open(build_json_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("COMMUNITY_KEY")
    except Exception:
        return None


def _read_gitignore(app_dir):
    """Read the .gitignore rule list"""
    patterns = []
    gi = os.path.join(app_dir, ".gitignore")
    if not os.path.exists(gi):
        return patterns
    with open(gi, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    return patterns


def _is_ignored(rel_path, patterns):
    """Check whether a file matches any .gitignore rule.

    ``node_modules`` is always excluded regardless of .gitignore rules.
    """
    # Always exclude node_modules from update comparison.
    if "node_modules" in rel_path.split("/"):
        return True

    basename = os.path.basename(rel_path)
    for pat in patterns:
        if fnmatch.fnmatch(rel_path, pat) or fnmatch.fnmatch(basename, pat):
            return True
        if pat.endswith("/"):
            d = pat.rstrip("/")
            if rel_path.startswith(d + "/") or ("/" + d + "/") in rel_path:
                return True
    return False


def _sha256_file(path):
    """Compute the SHA256 of a single file"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_blocks(path, block_size=BLOCK_SIZE):
    """Split a file into blocks of block_size and return the list of per-block SHA256 hashes."""
    hashes = []
    with open(path, "rb") as f:
        while True:
            chunk = f.read(block_size)
            if not chunk:
                break
            hashes.append(hashlib.sha256(chunk).hexdigest())
    return hashes


def _is_block_entry(val):
    """Whether a manifest entry is in block-level format (a dict containing 'blocks')."""
    return isinstance(val, dict) and isinstance(val.get("blocks"), list) and "sha256" in val


def _entry_sha256(val):
    """Get the overall SHA256 from a manifest entry (a string or a block-level dict)."""
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return val.get("sha256")
    return None


def _scan_local_files(app_dir, ignore_patterns):
    """
    Scan all local files and return {relative_path: SHA256}.
    Skip: .gitignore matches / manifest.json / manifest-*.json / update.bat / update.sh
    -- manifest-*.json must be excluded, otherwise when it is absent from remote files it would be added to to_delete,
       and after the update the local manifest gets deleted, permanently skipping the update check on next startup (issue 2).
    Note: the current check_update diff logic has been changed to "rely only on manifest.files"; this function is no longer called by the main flow, kept as a debugging tool.
    """
    result = {}
    for root, dirs, files in os.walk(app_dir):
        for fn in files:
            if fn in ("manifest.json", "update.bat", "update.sh"):
                continue
            if fn.startswith("manifest-") and fn.endswith(".json"):
                continue  # e.g. manifest-windows-x64.json
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, app_dir).replace(os.sep, "/")
            if _is_ignored(rel, ignore_patterns):
                continue
            try:
                result[rel] = _sha256_file(full)
            except Exception:
                pass
    return result


def _download(url, dest, max_retries=3):
    """Download a file to the given path, with retries"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "GreaterWMS-Updater"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
            return
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                sleep(1 * attempt)
    raise last_err


def _download_range(url, dest, byte_start, byte_end, max_retries=3):
    """
    Download the byte range [byte_start, byte_end] via HTTP Range and write it to dest at the given offset.
    If the server does not support Range (returns 200 full body), fall back to downloading the whole file.
    Returns True if Range succeeded, False if it fell back to the whole file.
    """
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "GreaterWMS-Updater",
                    "Range": f"bytes={byte_start}-{byte_end}",
                },
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                status = getattr(resp, "status", 200)
                if status == 206:
                    # Range supported: write at the given offset
                    with open(dest, "r+b") as f:
                        f.seek(byte_start)
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
                    return True
                else:
                    # Server does not support Range (returns 200), write the whole file
                    with open(dest, "wb") as f:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
                    return False
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                sleep(1 * attempt)
    raise last_err


def _fetch_manifest(url, max_retries=3):
    """
    Download and parse the remote manifest JSON, with retries and robust error handling.

    Handles:
      - Server returns empty content (0 bytes)
      - HTTP 404/5xx errors
      - Non-JSON response (e.g. HTML error page, blank line, plain text)
      - JSON missing required fields (app_name / version / files)
      - Network timeout / connection failure

    Returns the parsed dict on success; raises on failure (caller handles uniformly).
    """
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "GreaterWMS-Updater"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = getattr(resp, "status", 200)
                # 1. HTTP status check (urlopen raises HTTPError on 4xx/5xx; this is extra defense)
                if status < 200 or status >= 300:
                    raise RuntimeError(f"HTTP {status}")
                raw = resp.read()
                # 2. Empty response check
                if not raw or not raw.strip():
                    raise RuntimeError("empty response (0 bytes)")
                # 3. JSON parse (defend against non-JSON content like HTML/404 pages)
                try:
                    data = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    snippet = raw[:200].decode("utf-8", errors="replace")
                    raise RuntimeError(f"non-JSON response: {snippet!r}") from e
                # 4. Type check: must be an object
                if not isinstance(data, dict):
                    raise RuntimeError(f"manifest is not an object, got {type(data).__name__}")
                # 5. Required field completeness check
                missing = [k for k in ("app_name", "version", "files") if k not in data]
                if missing:
                    raise RuntimeError(f"missing required fields: {missing}")
                if not isinstance(data["files"], dict):
                    raise RuntimeError(f"'files' field is not an object, got {type(data['files']).__name__}")
                return data
        except Exception as e:
            last_err = e
            print(f"[Update] Fetch manifest attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                sleep(1 * attempt)
    raise last_err


def _can_reach_server(update_url, timeout=2.0):
    """
    Quick pre-check: use a raw socket to judge whether the host:port of UPDATE_URL is reachable.
    The goal is to skip the update check within 2 seconds when the network is down / NIC disabled /
    server is fully down, avoiding 3 x 10s = 33s of manifest download retries blocking the splash.
    Returns True meaning "possibly reachable" (only TCP layer works, HTTP layer may still fail),
    returns False meaning definitely unreachable.
    """
    try:
        from urllib.parse import urlparse
        u = urlparse(update_url if update_url.endswith("/") else update_url + "/")
        host = u.hostname
        if not host:
            return True  # invalid URL, let the upper layer decide
        if u.scheme == "https":
            port = u.port or 443
        elif u.scheme == "http":
            port = u.port or 80
        else:
            return True  # unknown protocol, skip pre-check
        # socket.create_connection = DNS + TCP SYN, fails fast within timeout
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.gaierror,         # DNS resolution failed (domain does not exist)
            socket.timeout,          # DNS/TCP timeout
            TimeoutError,            # timeout alias on some platforms
            ConnectionRefusedError,  # host reachable but port not open
            OSError):                # NIC disabled, no route, cable unplugged etc (10051/ENETUNREACH)
        return False
    except Exception:
        return True  # if the pre-check itself errors, do not block; let the upper HTTP request fall back


def _url_head_ok(url, timeout=5.0, max_retries=2):
    """
    Lightweight HTTP HEAD probe to verify whether the remote version directory actually exists before bulk download.
    -- Avoids the case where the server only updated the manifest but forgot to sync the
       GreaterWMS-{version}-{Platform}/ folder, which would trigger _download to retry 3x60s on a 404 file,
       freezing the splash for nearly 3 minutes.
    Returns (ok: bool, reason: str).
    """
    last_reason = ""
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(
                url, method="HEAD", headers={"User-Agent": "GreaterWMS-Updater"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200)
                if 200 <= status < 300:
                    return True, f"HTTP {status}"
                last_reason = f"HTTP {status}"
        except urllib.error.HTTPError as e:
            last_reason = f"HTTP {e.code}"
            if e.code == 404:
                return False, last_reason  # 404 needs no retry, immediately judge "folder does not exist"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_reason = f"{type(e).__name__}"
        except Exception as e:
            last_reason = f"{type(e).__name__}: {str(e)[:40]}"
        if attempt < max_retries:
            sleep(1)
    return False, last_reason


def _generate_update_script(app_dir, temp_dir, to_delete, manifest_name=None,
                           new_exe_path=None):
    """
    Generate the update script (update.bat / update.sh) and return its path.

    Args:
      - new_exe_path: absolute path of the new exe corresponding to the remote version
        (e.g. {app_dir}/GreaterWMS-3.0.1-Windows.exe). When a version upgrade changes the
        exe name, this value must be passed; otherwise the bat will still start the old
        version's exe (sys.executable), leaving the built-in version as the old value
        after restart -> update detected again -> infinite loop (user sees "flash exit").
    """
    is_win = sys.platform == "win32"
    exe_name = os.path.basename(sys.executable)
    exe_path = sys.executable
    # Prefer starting the new exe (when version changes the new exe is a different file), otherwise fall back to the current exe
    target_exe_path = new_exe_path if new_exe_path and os.path.isabs(new_exe_path) else exe_path
    target_exe_name = os.path.basename(target_exe_path)

    crash_log = os.path.join(app_dir, "update_crash.log")
    if is_win:
        script_path = os.path.join(app_dir, "update.bat")
        lines = ["@echo off"]
        lines.append(f':wait')
        # Use a temp file as intermediary to avoid the tasklist|find pipe deadlock (find's stdin hangs on EOF under CREATE_NO_WINDOW)
        lines.append(f'tasklist /fi "imagename eq {exe_name}" >"%TEMP%\\_bomiot_wait.tmp" 2>nul')
        lines.append(f'find /i "{exe_name}" "%TEMP%\\_bomiot_wait.tmp" >nul 2>nul')
        lines.append(f'if not errorlevel 1 ( del "%TEMP%\\_bomiot_wait.tmp" >nul 2>nul & ping -n 2 127.0.0.1 >nul 2>nul & goto wait )')
        lines.append(f'del "%TEMP%\\_bomiot_wait.tmp" >nul 2>nul')
        # /H copies hidden+system files (e.g. .gitignore); redirect output to temp log for diagnosis on failure
        lines.append(f'xcopy /Y /S /E /I /H "{temp_dir}" "{app_dir}" >"%TEMP%\\_bomiot_xcopy.log" 2>&1')
        # Bug fix: detect xcopy failure. On error, log and abort — do NOT start the new exe
        # with a half-overwritten app dir (would leave the install in an inconsistent state).
        lines.append(f'if errorlevel 1 goto copyfail')
        lines.append(f'del "%TEMP%\\_bomiot_xcopy.log" >nul 2>nul')
        lines.append(f'goto copyok')
        lines.append(f':copyfail')
        lines.append(f'echo [Update] xcopy failed ^(errorlevel^>^=1^), update aborted. >> "{crash_log}"')
        lines.append(f'type "%TEMP%\\_bomiot_xcopy.log" >> "{crash_log}" 2>nul')
        lines.append(f'del "%TEMP%\\_bomiot_xcopy.log" >nul 2>nul')
        lines.append(f'exit /b 1')
        lines.append(f':copyok')
        # Explicit double insurance: server manifest overwrites local manifest
        if manifest_name:
            lines.append(f'copy /Y "{os.path.join(temp_dir, manifest_name)}" "{os.path.join(app_dir, manifest_name)}" >nul 2>nul')
        lines.append(f'rd /S /Q "{temp_dir}" 2>nul')
        for f in to_delete:
            lines.append(f'del /F /Q "{os.path.join(app_dir, f)}" 2>nul')
        # Start the new exe (target_exe_path) first, then delete the old exe after a 3s ping delay (only needed when new exe name != old exe name)
        #   - do not directly rename the old exe (file lock may fail)
        #   - do not delete before xcopy (old exe is still running)
        lines.append(f'start "" /MIN "{target_exe_path}"')
        if target_exe_name.lower() != exe_name.lower():
            lines.append(f'ping -n 4 127.0.0.1 >nul 2>nul')
            lines.append(f'del /F /Q "{exe_path}" 2>nul')
        lines.append(f'del "%~f0"')
        with open(script_path, "w", encoding="utf-8") as f:
            f.write("\r\n".join(lines))
    else:
        script_path = os.path.join(app_dir, "update.sh")
        lines = ["#!/bin/bash"]
        lines.append(f'while pgrep -f "{exe_path}" > /dev/null 2>&1; do sleep 1; done')
        # Use "{temp_dir}"/. so hidden files (e.g. .gitignore) are also copied; * does not match dotfiles
        lines.append(f'if ! cp -rf "{temp_dir}"/. "{app_dir}"/ 2>> "{crash_log}"; then')
        lines.append(f'    echo "[Update] cp failed, update aborted." >> "{crash_log}"')
        lines.append(f'    exit 1')
        lines.append(f'fi')
        # Explicit double insurance: server manifest overwrites local manifest
        if manifest_name:
            lines.append(f'cp -f "{os.path.join(temp_dir, manifest_name)}" "{os.path.join(app_dir, manifest_name)}" 2>/dev/null')
        lines.append(f'rm -rf "{temp_dir}"')
        for f in to_delete:
            lines.append(f'rm -f "{os.path.join(app_dir, f)}"')
        lines.append(f'nohup "{target_exe_path}" > /dev/null 2>&1 &')
        if target_exe_name.lower() != exe_name.lower():
            lines.append(f'sleep 3')
            lines.append(f'rm -f "{exe_path}" 2>/dev/null')
        lines.append(f'rm -- "$0"')
        with open(script_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        os.chmod(script_path, 0o755)

    return script_path


def check_update(app_name, version, status_label=None, progress_bar=None):
    """
    Check and apply the incremental update.

    Args:
      app_name:   compiled into the exe application name (e.g. "GreaterWMS")
      version:    compiled into the exe version string (e.g. "3.0.1")

    Returns True if an update was triggered (caller should exit), False if no update needed.
    """
    app_dir = _app_dir()
    # Build the update URL by appending COMMUNITY_KEY (from build.json) to BASE_URL.
    community_key = _load_community_key(app_dir)
    if community_key:
        update_url = f"{BASE_URL.rstrip('/')}/{community_key}/media/update/"
    else:
        update_url = UPDATE_URL
    if not update_url or not update_url.strip():
        print("[Update] UPDATE_URL is empty, update check skipped")
        return False
    _os, _arch, _display = _detect_platform()
    manifest_name = f"manifest-{_os}-{_arch}.json"
    print(f"[Update] Detected platform: {_os} {_arch} ({_display})")
    print(f"[Update] Manifest file name: {manifest_name}")
    print(f"[Update] Manifest URL: {update_url}{manifest_name}")

    # Read local manifest (same filename as on the remote server)
    local_manifest_path = os.path.join(app_dir, manifest_name)
    if not os.path.exists(local_manifest_path):
        return False  # no manifest, skip
    try:
        with open(local_manifest_path, encoding="utf-8") as f:
            local_manifest = json.load(f)
    except Exception:
        return False

    # Read .gitignore rules
    ignore_patterns = _read_gitignore(app_dir)

    # ========= Quick network pre-check (2s, avoid 33s hang when offline) =========
    if status_label:
        status_label.config(text="Checking for updates...")
        status_label.update()
    if not _can_reach_server(update_url, timeout=2.0):
        err_msg = "Update check failed: network unreachable, skipping"
        print(f"[Update] {err_msg} (server unreachable, pre-check 2s)")
        if status_label:
            status_label.config(text=err_msg)
            status_label.update()
        return False

    # Download remote manifest (same filename as the local artifact)
    manifest_url = f"{update_url}{manifest_name}"
    print(f"[Update] Downloading remote manifest: {manifest_url}")
    try:
        _t0 = time.time()
        remote_manifest = _fetch_manifest(manifest_url, max_retries=3)
        _size_kb = len(json.dumps(remote_manifest, ensure_ascii=False)) / 1024
        _elapsed = time.time() - _t0
        print(f"[Update] Remote manifest downloaded: {_size_kb:.1f}KB ({_elapsed:.1f}s)")
    except urllib.error.HTTPError as e:
        # HTTP-level error (404, 500, etc.), server not deployed or temporary failure
        err_msg = f"Update check failed: HTTP {e.code}, skipping"
        print(f"[Update] {err_msg}")
        if status_label:
            status_label.config(text=err_msg)
            status_label.update()
        return False
    except urllib.error.URLError as e:
        # Network-level error (DNS failure, connection timeout, no network, etc.)
        reason = str(e.reason)[:40]
        err_msg = "Update check failed: network error, skipping"
        print(f"[Update] {err_msg} ({reason})")
        if status_label:
            status_label.config(text=err_msg)
            status_label.update()
        return False
    except (TimeoutError, OSError) as e:
        # Directly raised socket-level exceptions (some urllib versions don't wrap them in URLError)
        # e.g. socket.timeout / ConnectionRefusedError / ENETUNREACH(10051)
        reason = f"{type(e).__name__}: {str(e)[:40]}"
        err_msg = "Update check failed: network error, skipping"
        print(f"[Update] {err_msg} ({reason})")
        if status_label:
            status_label.config(text=err_msg)
            status_label.update()
        return False
    except Exception as e:
        # Others: empty response, non-JSON content, missing fields, timeout retries exhausted
        reason = str(e)[:60]
        err_msg = "Update check failed: server unresponsive, skipping"
        print(f"[Update] {err_msg} ({reason})")
        if status_label:
            status_label.config(text=err_msg)
            status_label.update()
        return False

    # Quick version-equality skip (server manifest is the source of truth; no >/< direction check, supports rollback)
    if remote_manifest.get("app_name") != app_name:
        return False
    _remote_ver = remote_manifest.get("version", "0")
    if _remote_ver == version:
        print(f"[Update] Version matches (binary {version} == remote {_remote_ver}), skipping hash scan")
        if progress_bar:
            progress_bar["value"] = 0
            progress_bar.update()
        if status_label:
            status_label.config(text="Already up to date")
            status_label.update()
        return False

    remote_version = _remote_ver
    remote_files = remote_manifest.get("files", {})
    # File download base: {UPDATE_URL}GreaterWMS-{version}-{Platform}/
    # Note: in rollback scenarios (remote version < binary version) the concatenated path is the old version directory; the server must keep the corresponding version folder
    file_base_url = f"{update_url}{app_name}-{remote_version}-{_display}/"

    # Pre-probe: verify remote version directory exists BEFORE the expensive hash diff.
    if remote_files:
        _probe_path = next(iter(remote_files))
        _probe_url = file_base_url + _probe_path
        _ok, _reason = _url_head_ok(_probe_url, timeout=5.0, max_retries=2)
        if not _ok:
            if _reason == "HTTP 404":
                err_msg = f"Server version directory not found ({remote_version}), skipping update"
            else:
                err_msg = f"Update probe failed ({_reason}), skipping update"
            print(f"[Update] {err_msg} probe={_probe_url}")
            if status_label:
                status_label.config(text=err_msg)
                status_label.update()
            return False
        print(f"[Update] Remote version folder verified via HEAD: {_probe_path} ({_reason})")

    _local_ver = local_manifest.get("version", "?")
    if _remote_ver < version:
        _direction = "rollback"
    else:
        _direction = "upgrade"
    if status_label:
        status_label.config(text=f"New version {remote_version} found ({_direction}), updating...")
        status_label.update()
    if progress_bar:
        progress_bar["value"] = 0
        progress_bar.update()

    print(f"[Update] {_direction} needed: binary={version} local_manifest={_local_ver} remote={remote_version}")

    # ================================================================
    # Diff: dual-manifest intersection model (remote.files = A; local.files = B)
    #   A ∩ B and hash(A) != hash(B)  → download update (intersection)
    #   A − B (declared remotely, absent locally)  → download new (e.g. new version exe)
    #   B − A (declared locally, removed remotely)  → add to to_delete (e.g. old version exe, pyd/dll no longer produced by CI)
    # ================================================================
    to_download = []
    to_delete = []
    local_files = local_manifest.get("files") if isinstance(local_manifest, dict) else None
    if isinstance(local_files, dict):
        # ==== Standard path: dual-manifest bidirectional diff ====
        A_keys = set(remote_files.keys())
        B_keys = set(local_files.keys())
        # 1) A ∩ B: intersection, compare per file
        for rel in A_keys & B_keys:
            remote_entry = remote_files[rel]
            remote_hash = _entry_sha256(remote_entry)
            local_path = os.path.join(app_dir, rel.replace("/", os.sep))
            # Safety lock: a file declared in local manifest but deleted by the user at runtime → re-download as "missing"
            if not os.path.isfile(local_path):
                to_download.append({"path": rel})
                continue
            # Block-level incremental: remote is a block-level entry → compare each block's hash
            if _is_block_entry(remote_entry):
                remote_blocks = remote_entry["blocks"]
                block_size = remote_entry.get("block_size", BLOCK_SIZE)
                try:
                    local_blocks = _hash_blocks(local_path, block_size)
                except Exception:
                    local_blocks = []
                if (len(local_blocks) == len(remote_blocks)
                        and all(lb == rb for lb, rb in zip(local_blocks, remote_blocks))):
                    continue
                changed = [
                    i for i in range(len(remote_blocks))
                    if i >= len(local_blocks) or local_blocks[i] != remote_blocks[i]
                ]
                to_download.append({
                    "path": rel,
                    "blocks": changed,
                    "block_size": block_size,
                    "size": remote_entry.get("size"),
                    "sha256": remote_hash,
                })
            else:
                # Small file: full-file hash comparison
                try:
                    local_hash = _sha256_file(local_path)
                except Exception:
                    local_hash = None
                if local_hash != remote_hash:
                    to_download.append({"path": rel})
        # 2) A − B: remote new files → full-file download
        for rel in A_keys - B_keys:
            to_download.append({"path": rel})
        # 3) B − A: removed remotely → to_delete
        protected_prefix = ("dbs/", "logs/", "__pycache__/")
        for rel in B_keys - A_keys:
            basename = os.path.basename(rel)
            if basename in ("update.bat", "update.sh", "manifest.json"):
                continue
            if basename.startswith("manifest-") and basename.endswith(".json"):
                continue
            rel_norm = rel.replace("\\", "/")
            skip_prefix = False
            for p in protected_prefix:
                if rel_norm.startswith(p) or ("/" + p).rstrip("/") + "/" in "/" + rel_norm:
                    skip_prefix = True
                    break
            if skip_prefix:
                continue
            if _is_ignored(rel_norm, ignore_patterns):
                continue
            local_path = os.path.join(app_dir, rel.replace("/", os.sep))
            if os.path.isfile(local_path):
                to_delete.append(rel)
    else:
        # ==== Fallback path: local manifest has no files → remote-only one-way (no deletion) ====
        print("[Update] local manifest missing 'files', falling back to one-way compare (no delete)")
        for rel, remote_entry in remote_files.items():
            remote_hash = _entry_sha256(remote_entry)
            local_path = os.path.join(app_dir, rel.replace("/", os.sep))
            if not os.path.isfile(local_path):
                to_download.append({"path": rel})
                continue
            try:
                local_hash = _sha256_file(local_path)
            except Exception:
                local_hash = None
            if local_hash != remote_hash:
                to_download.append({"path": rel})

    print(f"[Update] diff stats: to_download={len(to_download)}  to_delete(B−A)={len(to_delete)}  "
          f"(remote.files={len(remote_files)}  local.files={len(local_files) if isinstance(local_files, dict) else 'N/A'})")

    if not to_download and not to_delete:
        return False  # no actual change

    # Download changed files into a temp directory
    temp_dir = tempfile.mkdtemp(prefix="bomiot_update_")

    # ========= Progress tracking: compute total download size (block-level known upfront) =========
    total_bytes = 0
    for item in to_download:
        blks = item.get("blocks")
        if blks:
            total_bytes += len(blks) * item.get("block_size", BLOCK_SIZE)
    downloaded_bytes = 0

    def _update_progress(extra_text=""):
        if not progress_bar:
            return
        pct = (downloaded_bytes / total_bytes * 100) if total_bytes > 0 else 0
        progress_bar["value"] = min(pct, 100)
        mb_done = downloaded_bytes / 1024 / 1024
        mb_total = total_bytes / 1024 / 1024
        if status_label:
            if extra_text:
                status_label.config(text=f"{extra_text}  {mb_done:.1f}MB / {mb_total:.1f}MB ({pct:.0f}%)")
            else:
                status_label.config(text=f"Downloading  {mb_done:.1f}MB / {mb_total:.1f}MB ({pct:.0f}%)")
            status_label.update()
        progress_bar.update()

    if progress_bar:
        progress_bar["value"] = 0
        progress_bar.update()
        _update_progress(f"Preparing to download {len(to_download)} files")

    for item in to_download:
        path = item["path"]
        url = file_base_url + path
        dest = os.path.join(temp_dir, path.replace("/", os.sep))
        try:
            blocks = item.get("blocks")
            if blocks:
                # ===== Block-level incremental: copy local file + download only changed blocks =====
                local_path = os.path.join(app_dir, path.replace("/", os.sep))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if os.path.isfile(local_path):
                    shutil.copyfile(local_path, dest)
                else:
                    # No local file: create from scratch, download all blocks via Range
                    open(dest, "wb").close()
                block_size = item["block_size"]
                remote_size = item.get("size")
                range_ok = True
                for bi in blocks:
                    byte_start = bi * block_size
                    byte_end = min((bi + 1) * block_size, remote_size) - 1 if remote_size else byte_start + block_size - 1
                    ok = _download_range(url, dest, byte_start, byte_end)
                    if ok:
                        downloaded_bytes += block_size
                    else:
                        downloaded_bytes += remote_size if remote_size else block_size
                        range_ok = False
                        break
                    _update_progress(f"Downloading {os.path.basename(path)}")
                # Verify the whole-file SHA256; fall back to full download on mismatch
                if range_ok:
                    try:
                        actual = _sha256_file(dest)
                    except Exception:
                        actual = None
                    if actual != item.get("sha256"):
                        print(f"[Update] block verify mismatch for {path}, falling back to full download")
                        _download(url, dest)
            else:
                _download(url, dest)
                # Small file: add its actual size to both total and downloaded (size unknown before download)
                fsize = os.path.getsize(dest)
                total_bytes += fsize
                downloaded_bytes += fsize
                _update_progress(f"Downloading {os.path.basename(path)}")
        except Exception as e:
            print(f"Download failed: {path} - {e}")
            shutil.rmtree(temp_dir, ignore_errors=True)
            if progress_bar:
                progress_bar["value"] = 0
                progress_bar.update()
            if status_label:
                status_label.config(text="Update download failed, skipping")
                status_label.update()
            return False

    if progress_bar:
        progress_bar["value"] = 100
        progress_bar.update()
    if status_label:
        status_label.config(text="Update download complete, applying...")
        status_label.update()

    # Also write the new-version manifest into temp_dir
    new_manifest_path = os.path.join(temp_dir, manifest_name)
    try:
        with open(new_manifest_path, "w", encoding="utf-8") as f:
            json.dump(remote_manifest, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Update] Warning: failed to write new manifest to temp: {e}")

    # Compute the absolute path of the new exe
    _plat_is_win = sys.platform == "win32"
    if _plat_is_win:
        new_exe_name = f"{app_name}.exe"
    else:
        new_exe_name = app_name
    new_exe_path = os.path.join(app_dir, new_exe_name)

    # Generate update script
    script_path = _generate_update_script(
        app_dir, temp_dir, to_delete,
        manifest_name=manifest_name,
        new_exe_path=new_exe_path,
    )

    # Start the script (silent: do not pop up any cmd / terminal black window)
    is_win = sys.platform == "win32"
    _started_script = False
    _script_err = ""
    try:
        if is_win:
            CREATE_NO_WINDOW          = int(getattr(subprocess, "CREATE_NO_WINDOW",          0x08000000))
            CREATE_NEW_PROCESS_GROUP  = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP",  0x00000200))
            flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
            startupinfo = subprocess.STARTUPINFO()
            try:
                startupinfo.dwFlags = int(getattr(subprocess, "STARTF_USESHOWWINDOW", 1))
            except Exception:
                startupinfo.dwFlags = 1
            startupinfo.wShowWindow = 0
            subprocess.Popen(
                ["cmd.exe", "/c", script_path],
                cwd=app_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                shell=False,
                startupinfo=startupinfo,
                creationflags=flags,
            )
        else:
            subprocess.Popen(
                ["bash", script_path],
                cwd=app_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                shell=False,
                start_new_session=True,
            )
        _started_script = True
    except Exception as e:
        import traceback as _tb
        _script_err = f"{type(e).__name__}: {e}\n{_tb.format_exc()}"
        print(f"[Update] FATAL: failed to start update script.\n{_script_err}")
        try:
            crash_log = os.path.join(app_dir, "update_crash.log")
            with open(crash_log, "a", encoding="utf-8") as f:
                f.write(f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                f.write(f"script_path={script_path}\n")
                f.write(_script_err)
                f.write("\n")
        except Exception:
            pass

    if not _started_script:
        # Script start failed: clean up leftovers + do not exit
        shutil.rmtree(temp_dir, ignore_errors=True)
        try:
            os.remove(script_path)
        except Exception:
            pass
        if status_label:
            status_label.config(text="Update script failed to start, skipping (see update_crash.log)")
            status_label.update()
        return False

    # Give the bat child process a 300ms startup buffer
    sleep(0.3)
    return True
