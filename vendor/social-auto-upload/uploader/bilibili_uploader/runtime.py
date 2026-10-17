from __future__ import annotations

import platform
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

import requests


GITHUB_RELEASE_API = "https://api.github.com/repos/biliup/biliup/releases/latest"
BILIUP_UPLOAD_HEARTBEAT_SECONDS = 15.0
BILIUP_UPLOAD_HEARTBEAT = "B站上传进程仍在运行。"


def get_biliup_runtime_root() -> Path:
    if os.environ.get("SAU_BILIUP_HOME"):
        return Path(os.environ["SAU_BILIUP_HOME"]).resolve()
    return Path.home() / ".social-auto-upload" / "tools" / "biliup"


def _normalize_system(system_name: str | None = None) -> str:
    system_value = (system_name or platform.system()).strip().lower()
    if system_value == "darwin":
        return "macos"
    return system_value


def _normalize_machine(machine_name: str | None = None) -> str:
    machine_value = (machine_name or platform.machine()).strip().lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "arm64": "aarch64",
    }
    return aliases.get(machine_value, machine_value)


def _build_platform_key(system_name: str | None = None, machine_name: str | None = None) -> str:
    return f"{_normalize_system(system_name)}-{_normalize_machine(machine_name)}"


def build_biliup_runtime_path(system_name: str | None = None) -> Path:
    executable_name = "biliup.exe" if _normalize_system(system_name) == "windows" else "biliup"
    return get_biliup_runtime_root() / _build_platform_key(system_name) / executable_name


def _build_biliup_version_path(system_name: str | None = None) -> Path:
    return build_biliup_runtime_path(system_name).with_name("version.txt")


def _select_release_asset(assets: list[dict]) -> dict:
    platform_key = _build_platform_key()
    preferred_patterns = {
        "windows-x86_64": ("x86_64-windows.zip",),
        "linux-x86_64": ("x86_64-linux.tar.xz",),
        "linux-aarch64": ("aarch64-linux.tar.xz",),
        "linux-arm": ("arm-linux.tar.xz",),
        "macos-x86_64": ("x86_64-macos.tar.xz",),
        "macos-aarch64": ("aarch64-macos.tar.xz",),
    }
    patterns = preferred_patterns.get(platform_key)
    if not patterns:
        raise RuntimeError(f"Unsupported biliup platform: {platform_key}")

    for asset in assets:
        asset_name = asset.get("name", "")
        if any(pattern in asset_name for pattern in patterns):
            return {
                "asset_name": asset_name,
                "asset_url": asset.get("browser_download_url", ""),
            }

    raise RuntimeError(f"No matching biliup release asset found for platform: {platform_key}")


def fetch_latest_release() -> dict:
    response = requests.get(
        GITHUB_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "social-auto-upload",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    selected_asset = _select_release_asset(payload.get("assets", []))
    return {
        "tag_name": payload.get("tag_name", ""),
        "asset_name": selected_asset["asset_name"],
        "asset_url": selected_asset["asset_url"],
    }


def read_local_biliup_version() -> str | None:
    version_path = _build_biliup_version_path()
    if not version_path.exists():
        return None
    return version_path.read_text(encoding="utf-8").strip() or None


def write_local_biliup_version(version: str) -> None:
    version_path = _build_biliup_version_path()
    version_path.parent.mkdir(parents=True, exist_ok=True)
    version_path.write_text(version, encoding="utf-8")


def _pick_executable(extract_root: Path) -> Path:
    candidates = []
    for path in extract_root.rglob("*"):
        if not path.is_file():
            continue
        lower_name = path.name.lower()
        if lower_name in {"biliup", "biliup.exe", "biliupr", "biliupr.exe"}:
            candidates.append(path)
    if not candidates:
        raise RuntimeError("Downloaded biliup archive does not contain a runnable executable")
    candidates.sort(key=lambda item: len(str(item)))
    return candidates[0]


def download_biliup_asset(release: dict, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="biliup-download-") as temp_dir:
        temp_root = Path(temp_dir)
        archive_path = temp_root / release["asset_name"]
        with requests.get(release["asset_url"], stream=True, timeout=120) as response:
            response.raise_for_status()
            with archive_path.open("wb") as file_obj:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        file_obj.write(chunk)

        extract_root = temp_root / "extract"
        extract_root.mkdir(parents=True, exist_ok=True)
        if archive_path.suffix.lower() == ".zip":
            with zipfile.ZipFile(archive_path) as zip_file:
                zip_file.extractall(extract_root)
        else:
            with tarfile.open(archive_path, "r:xz") as tar_file:
                tar_file.extractall(extract_root)

        extracted_binary = _pick_executable(extract_root)
        temp_binary = destination.with_suffix(f"{destination.suffix}.tmp")
        shutil.copy2(extracted_binary, temp_binary)
        temp_binary.replace(destination)
        if _normalize_system() != "windows":
            destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return destination


def ensure_biliup_binary(force_check: bool = True) -> Path:
    binary_path = build_biliup_runtime_path()
    # The local publisher installs an audited, fixed release explicitly.
    # Runtime login/upload must never fetch or upgrade executable code.
    if os.environ.get("SAU_PUBLISHER_SAFE_LOG") == "1":
        if binary_path.is_file():
            return binary_path
        raise RuntimeError("本地 biliup 未安装，请先运行安装脚本")
    local_version = read_local_biliup_version()

    # 默认优先复用本地已存在的 biliup，避免每次执行都去请求 GitHub latest release。
    if binary_path.exists() and not force_check:
        return binary_path

    try:
        release = fetch_latest_release()
    except Exception:
        # 如果本地已经有可执行的 biliup，就在 GitHub 限流/网络失败时直接复用本地版本。
        if binary_path.exists():
            return binary_path
        raise

    latest_version = release["tag_name"]
    if binary_path.exists() and local_version == latest_version:
        return binary_path

    download_biliup_asset(release, binary_path)
    write_local_biliup_version(latest_version)
    return binary_path


def _is_upload_command(arguments: list[str]) -> bool:
    # Read only global options, so a cookie filename or another command's value
    # named "upload" cannot accidentally enable the upload progress channel.
    values = iter(arguments)
    for value in values:
        if value in {"-u", "--user-cookie", "-p", "--proxy", "--rust-log"}:
            next(values, None)
        elif value.startswith(("-u", "-p", "--user-cookie=", "--proxy=", "--rust-log=")):
            continue
        else:
            return value == "upload"
    return False


def _run_upload_with_heartbeat(command: list[str]) -> subprocess.CompletedProcess[str]:
    # Keep both native streams private and preserve CompletedProcess semantics.
    # communicate(timeout=...) drains both pipes while waiting, including silent
    # uploads and carriage-return progress bars. No native text is forwarded.
    # Inherit the process group so publisher cancellation stops both CLIs.
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        while True:
            if process.poll() is None:
                print(BILIUP_UPLOAD_HEARTBEAT, flush=True)
            try:
                stdout, stderr = process.communicate(timeout=BILIUP_UPLOAD_HEARTBEAT_SECONDS)
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                # A heartbeat confirms only that the process is still alive;
                # it makes no claim about transfer percentage or submission.
                continue
    except BaseException:
        process.kill()
        process.communicate()
        raise


def run_biliup_command(arguments: list[str], interactive: bool = False) -> subprocess.CompletedProcess[str]:
    binary_path = ensure_biliup_binary(force_check=False)
    command = [str(binary_path), *arguments]
    if interactive:
        return subprocess.run(command, check=False)
    if os.environ.get("SAU_PUBLISHER_SAFE_LOG") == "1" and _is_upload_command(arguments):
        return _run_upload_with_heartbeat(command)
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
