"""Bounded adapters for the vendored, real social-auto-upload CLI.

Importing this module never logs in, accesses a platform, or uploads a file.
Only run() starts a platform operation. A submitted result is a submission
observation, not a claim that moderation passed or that a video is public.
"""
from __future__ import annotations

import importlib
import codecs
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import secrets
import signal
import subprocess
import sys
import threading
import tempfile
import time
from platform_workflows import PREPARE_PLATFORMS, BILIBILI_CATEGORIES, BILIBILI_DECLARATIONS

BASE = Path(__file__).resolve().parent
VENDOR = BASE / "vendor" / "social-auto-upload"
PYTHON = BASE / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
COOKIE_DIR = VENDOR / "cookies"
BILIBILI_QR = VENDOR / "qrcode.png"
TIMEOUTS = {"login": 660, "check": 310, "publish": 2400}
PLATFORMS = [
    {"id": pid, "name": name, "capabilities": {"cover": True, "description": True}}
    for pid, name in [
        ("douyin", "抖音"), ("xiaohongshu", "小红书"),
        ("bilibili", "哔哩哔哩"), ("youtube", "YouTube"),
        ("tencent", "微信视频号"), ("kuaishou", "快手"),
        ("baijiahao", "百家号"), ("alipay", "支付宝生活号"),
        ("weibo", "微博"), ("hupu", "虎扑"),
    ]
]
PLATFORMS.append({"id": "tiktok", "name": "TikTok", "capabilities": {"cover": True, "description": True}})
for platform in PLATFORMS:
    if platform["id"] == "baijiahao":
        platform["capabilities"].update(description=False, tags=False)
        platform["note"] = "当前百家号上传流程不支持正文和话题；填写时将停止，避免静默遗漏。"
    elif platform["id"] == "kuaishou":
        platform["capabilities"]["max_tags"] = 3
        platform["note"] = "当前快手上传流程最多支持 3 个话题。"
TITLE_LIMITS = {"douyin": 30, "xiaohongshu": 20, "bilibili": 80, "youtube": 100,
                "tencent": 16, "kuaishou": 50, "baijiahao": 30, "alipay": 30,
                "weibo": 30, "hupu": 40, "tiktok": 100}
CLI_NAMES = {"douyin": "Douyin", "xiaohongshu": "Xiaohongshu", "bilibili": "Bilibili",
             "youtube": "YouTube", "tencent": "Tencent/WeChat Channels", "kuaishou": "Kuaishou",
             "baijiahao": "Baijiahao", "alipay": "Alipay", "weibo": "Weibo", "hupu": "Hupu"}
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_RUNTIME_CACHE = {"time": 0.0, "value": None}
_RUNTIME_LOCK = threading.Lock()
PUBLISH_PHASES = ("preparation", "uploading", "processing", "metadata", "cover", "ready_to_submit", "submitting", "submitted")
PUBLISH_ERROR_CODES = frozenset({
    "preparation_failed", "upload_failed", "processing_failed", "metadata_failed", "cover_failed", "pre_submit_failed",
    "submission_unconfirmed", "submission_boundary_unknown", "process_failed",
})


def _environment(metadata=None):
    env = os.environ.copy()
    env.pop("PUBLISHER_NATIVE_CHROME_ENDPOINT", None)
    env.pop("PUBLISHER_NATIVE_CHROME_PLATFORM", None)
    env.update(PYTHONUNBUFFERED="1", PYTHONUTF8="1", SAU_PUBLISHER_SAFE_LOG="1",
               DOUYIN_COOKIE_AUTH_HEADLESS="true", NO_COLOR="1", RUST_LOG="info",
               PLAYWRIGHT_BROWSERS_PATH=str(BASE / ".browsers"),
               SAU_BILIUP_HOME=str(BASE / "data" / "tools" / "biliup"))
    env["SAU_PUBLISHER_PREPARE_ONLY"] = "1" if metadata and metadata.get("prepare_only") is True else "0"
    env["SAU_PUBLISHER_RESUME_COVER"] = "1" if metadata and metadata.get("prepare_only") is True and metadata.get("resume_cover") is True else "0"
    env["PUBLISHER_DATA_DIR"] = str(BASE / "data")
    if metadata is not None:
        if metadata.get("native_chrome_endpoint"):
            env["PUBLISHER_NATIVE_CHROME_ENDPOINT"] = metadata["native_chrome_endpoint"]
            env["PUBLISHER_NATIVE_CHROME_PLATFORM"] = metadata["id"]
        env["PUBLISHER_AI_GENERATED"] = "true" if metadata.get("ai_generated") is True else "false"
        env["PUBLISHER_WEIBO_TYPE"] = {"original": "原创", "recreation": "二创"}.get((metadata.get("weibo") or {}).get("content_type"), "")
        env["PUBLISHER_HUPU_ZONE"] = {"walking_street": "步行街主干道"}.get((metadata.get("hupu") or {}).get("zone"), "")
    return env


def runtime_status():
    with _RUNTIME_LOCK:
        if _RUNTIME_CACHE["value"] is not None and time.monotonic() - _RUNTIME_CACHE["time"] < 30:
            return dict(_RUNTIME_CACHE["value"])
        value = _probe_runtime()
        _RUNTIME_CACHE.update(time=time.monotonic(), value=value)
        return dict(value)


def _probe_runtime():
    """Local dependency probe only; this does not inspect account credentials."""
    result = {"ready": False, "python": str(PYTHON)}
    if not PYTHON.is_file():
        return {**result, "message": "本地 .venv 尚未安装，请先运行安装脚本。"}
    if not (VENDOR / "conf.py").is_file():
        return {**result, "message": "缺少 vendor/social-auto-upload/conf.py，请先运行安装脚本。"}
    try:
        probe = subprocess.run(
            [str(PYTHON), "-c", "import sau_cli; from patchright.sync_api import sync_playwright; "
             "from pathlib import Path; p=sync_playwright().start(); "
             "assert Path(p.chromium.executable_path).is_file(), 'missing chromium'; p.stop()"],
            cwd=str(VENDOR), env=_environment(), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=20, shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {**result, "message": "本地运行环境检查失败，请重新运行安装脚本。"}
    if probe.returncode:
        return {**result, "message": "上传依赖或 Patchright Chromium 尚未就绪，请运行安装脚本。"}
    return {**result, "ready": True, "message": "本地 CLI 与 Chromium 已就绪；账号与平台发布状态需分别核验。YouTube 使用本机 Google Chrome。"}


def _platform(platform):
    if platform not in TITLE_LIMITS:
        raise ValueError("不支持的平台")


def _file(value, label, required=False):
    if value in (None, ""):
        if required:
            raise ValueError(f"缺少{label}")
        return None
    if not isinstance(value, str) or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{label}路径无效")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"{label}文件不存在")
    return str(path)


def build_command(platform, action, metadata=None):
    """Construct a shell-free command; do not execute it or inspect account data."""
    _platform(platform)
    if action not in TIMEOUTS:
        raise ValueError("不支持的操作")
    if action == "publish" and (metadata or {}).get("prepare_only") is True and platform not in PREPARE_PLATFORMS:
        raise ValueError("此平台尚未适配最后由使用者发布，未开始上传")
    if platform == "tiktok":
        return importlib.import_module("adapter_tiktok").build_command(action, metadata)
    command = [str(PYTHON), "-u", str(VENDOR / "sau_cli.py"), platform,
               "upload-video" if action == "publish" else action, "--account", "default"]
    if action != "publish":
        if action == "login" and platform != "bilibili":
            command.append("--headed")
        return command
    m = metadata or {}
    title = m.get("title")
    if not isinstance(title, str) or not title.strip() or any(c in title for c in "\x00\r\n"):
        raise ValueError("标题不能为空")
    if len(title) > TITLE_LIMITS[platform]:
        raise ValueError(f"标题超过 {TITLE_LIMITS[platform]} 字，本工具不会截断标题")
    if platform == "hupu" and len(title) < 4:
        raise ValueError("虎扑标题至少 4 字")
    if platform == "tencent" and len(title) < 6:
        raise ValueError("视频号标题至少 6 字")
    if platform == "baijiahao" and len(title) <= 8:
        raise ValueError("百家号标题至少 9 字")
    desc = m.get("description", "")
    if not isinstance(desc, str) or "\x00" in desc:
        raise ValueError("正文格式不正确")
    tags = m.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(t, str) or not t or any(c in t for c in ",\x00\n\r") or t.startswith("#") for t in tags):
        raise ValueError("话题应为不含逗号、换行或开头 # 的文字列表")
    if platform in {"xiaohongshu", "bilibili"} and len(tags) > 10:
        raise ValueError("该平台最多支持 10 个话题")
    if platform == "kuaishou" and len(tags) > 3:
        raise ValueError("当前快手上传流程最多支持 3 个话题，本工具不会丢弃多余话题")
    if platform == "baijiahao" and (desc or tags):
        raise ValueError("当前百家号上传流程不支持正文和话题，请清空后再提交，本工具不会静默遗漏")
    if platform == "youtube" and len(",".join(tags)) > 500:
        raise ValueError("YouTube 话题总长度不能超过 500 字")
    if platform == "alipay" and len(title + "".join(" #" + t for t in tags)) > 30:
        raise ValueError("支付宝标题和话题合计不能超过 30 字，本工具不会丢弃话题")
    if platform == "youtube" and len(desc) > 5000:
        raise ValueError("YouTube 正文不能超过 5000 字")
    if type(m.get("ai_generated")) is not bool:
        raise ValueError("请明确选择视频是否含 AI 生成内容")
    if platform == "weibo" and (m.get("weibo") or {}).get("content_type") not in {"original", "recreation"}:
        raise ValueError("请明确选择微博内容类型")
    if platform == "hupu" and (m.get("hupu") or {}).get("zone") != "walking_street":
        raise ValueError("请选择虎扑发布专区")
    video = _file(m.get("video_path"), "视频", required=True)
    cover = _file(m.get("cover_path"), "封面", required=platform in {"baijiahao", "weibo"})
    landscape = _file(m.get("landscape_cover_path"), "横版封面")
    command += ["--file", video, "--title", title, "--desc", desc, "--tags", ",".join(tags)]
    if cover:
        command += ["--thumbnail", cover]
    if landscape and platform in {"douyin", "tencent"}:
        command += ["--thumbnail-landscape", landscape]
    if platform == "douyin" and m["ai_generated"]:
        command += ["--declaration", "内容由AI生成"]
    if platform == "bilibili":
        settings = m.get("bilibili") or {}
        if m.get("prepare_only") is True:
            if settings.get("category") not in BILIBILI_CATEGORIES or settings.get("declaration") not in BILIBILI_DECLARATIONS:
                raise ValueError("请选择 B站分区和创作声明")
            if not tags or any(len(tag)>20 for tag in tags):
                raise ValueError("B站需要标签，每个最多20字")
            if (m.get("ai_generated") is True) != (settings["declaration"] == "ai"):
                raise ValueError("B站创作声明与 AI 内容选择不一致")
            command += ["--category", settings["category"], "--declaration", settings["declaration"]]
        else:
            tid = settings.get("tid")
            if type(tid) is not int or tid <= 0:
                raise ValueError("请选择有效的 B 站分区编号")
            command += ["--tid", str(tid)]
    else:
        command.append("--headed")
    if platform == "youtube":
        y = m.get("youtube") or {}
        if y.get("visibility") not in {"public", "unlisted", "private"}:
            raise ValueError("请选择 YouTube 可见性")
        if type(y.get("made_for_kids")) is not bool:
            raise ValueError("请明确选择 YouTube 是否面向儿童")
        if type(y.get("altered_synthetic")) is not bool:
            raise ValueError("请明确选择 YouTube 是否包含逼真合成或修改内容")
        command += ["--visibility", y["visibility"], "--made-for-kids", str(y["made_for_kids"]).lower(),
                    "--altered-synthetic", str(y["altered_synthetic"]).lower()]
    return command


def _clean(line):
    return ANSI.sub("", line).replace("\r", "").strip()


def _progress(line):
    """Allowlist progress, never relay arbitrary vendor text or exception data."""
    line = _clean(line)
    # Node's compatibility warning mentions "errors" but is not a task failure.
    if re.match(r"^\(node:\d+\) \[DEP\d+\] DeprecationWarning:", line):
        return None
    if line in {"B站上传进程仍在运行。", "小红书视频仍在上传或处理中。"}:
        return line
    if re.search(r"cookie|token|authorization|sessdata|bili_jct|password|access.key|secret", line, re.I):
        return None
    for needles, label in [
        (("二维码", "请扫码"), "等待扫码登录，请查看浏览器或二维码预览。"),
        (("请在弹出的浏览器",), "请在打开的浏览器完成登录和平台验证。"),
        (("开始搬运", "开始上传", "开始上传视频", "Uploading"), "开始上传视频素材。"),
        (("视频已经传完", "上传完成", "Upload completed"), "视频素材传输完成，正在填写发布信息。"),
        (("填写标题", "填标题", "填写简介", "添加 desc"), "正在填写标题、正文和话题。"),
        (("封面",), "正在处理用户选择的封面。"),
        (("NEEDS_ATTENTION", "验证码", "身份验证", "captcha"), "平台需要人工核验，请检查当前页面。"),
    ]:
        if any(word in line for word in needles):
            return label
    if re.search(r"error|exception|traceback|失败|停止提交", line, re.I):
        return "平台流程报告异常；本工具将停止此平台并保留待核验状态。"
    return None


def _stop(process):
    if os.name == "nt":
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=3)
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    # A CLI can exit on SIGTERM while its browser/driver children ignore it.
    # Waiting for the leader alone does not prove that the process group stopped.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=3)


def _read_evidence(path, nonce, platform, action):
    """Only the final CLI stage writes this per-run channel; logs are untrusted."""
    try:
        if path.stat().st_size > 16384:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if (isinstance(value, dict) and value.get("nonce") == nonce
                and value.get("platform") == platform and value.get("action") == action):
            return value
    except (OSError, ValueError, TypeError):
        pass
    return None


def _read_progress(path, nonce, platform, action):
    """Validate the entire private schema; arbitrary child log text is irrelevant."""
    if action != "publish" or platform not in {"douyin", "xiaohongshu", "youtube", "bilibili"}:
        return None
    value = _read_evidence(path, nonce, platform, action)
    required = {"version", "nonce", "platform", "action", "phase", "updated_at"}
    if (not value or not required.issubset(value) or set(value) - required - {"error_code", "percent"}
            or type(value.get("version")) is not int or value["version"] != 1):
        return None
    phase, error_code = value["phase"], value.get("error_code")
    if phase not in PUBLISH_PHASES:
        if phase is not None or platform != "bilibili" or error_code != "submission_boundary_unknown":
            return None
    if error_code is not None and (not isinstance(error_code, str) or error_code not in PUBLISH_ERROR_CODES):
        return None
    if 'percent' in value and (phase not in ('uploading', 'processing') or type(value['percent']) not in (int, float) or not 0 <= value['percent'] <= 100):
        return None
    try:
        stamp = value["updated_at"]
        if not isinstance(stamp, str) or len(stamp) > 40 or datetime.fromisoformat(stamp).utcoffset() is None:
            return None
    except (ValueError, TypeError):
        return None
    # Never expose the per-run secret or filesystem location in server events.
    return {key: value[key] for key in ("phase", "updated_at", "error_code", "percent") if key in value}


def _execute(command, platform, action, on_log, cancel_event, metadata=None, timeout_seconds=None):
    with tempfile.TemporaryDirectory(prefix="publisher-result-") as directory:
        evidence = Path(directory) / "result.json"
        nonce = secrets.token_hex(32)
        return _execute_process(command, platform, action, on_log, cancel_event,
                                metadata, evidence, nonce, timeout_seconds)


def _execute_process(command, platform, action, on_log, cancel_event, metadata, evidence, nonce, timeout_seconds):
    """Read byte chunks so a silent browser or no-newline QR prompt cannot hang."""
    master = slave = None
    if platform == "bilibili" and action == "login":
        if os.name == "nt":
            raise RuntimeError("B站扫码登录适配器目前需要 macOS/Linux PTY；Windows 请在本地终端登录")
        import pty
        master, slave = pty.openpty()
        BILIBILI_QR.unlink(missing_ok=True)
    env = _environment(metadata)
    progress_file = evidence.with_name("progress.json")
    env.update(PUBLISHER_RESULT_FILE=str(evidence), PUBLISHER_RESULT_NONCE=nonce,
               PUBLISHER_PROGRESS_FILE=str(progress_file), PUBLISHER_PROGRESS_PLATFORM=platform,
               PUBLISHER_PROGRESS_ACTION=action)
    try:
        process = subprocess.Popen(
            command, cwd=str(VENDOR), env=env, shell=False,
            stdin=slave if slave is not None else subprocess.DEVNULL,
            stdout=slave if slave is not None else subprocess.PIPE,
            stderr=slave if slave is not None else subprocess.STDOUT,
            start_new_session=True,
        )
    except BaseException:
        for descriptor in (master, slave):
            if descriptor is not None:
                os.close(descriptor)
        raise
    if slave is not None:
        os.close(slave)
    fd = master if master is not None else process.stdout.fileno()
    reader = selectors.DefaultSelector()
    reader.register(fd, selectors.EVENT_READ)
    started = time.monotonic()
    buffer = ""
    menu_buffer = ""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    observed = {"returncode": None, "submitted": False, "login": False, "valid": False, "invalid": False,
                "cancelled": False, "timeout": False, "url": None, "phase": None, "retry_safe": False}
    latest_progress = None
    submission_possible = False

    def poll_progress():
        nonlocal latest_progress, submission_possible
        progress = _read_progress(progress_file, nonce, platform, action)
        if progress is None:
            return None
        phase = progress["phase"]
        if phase in {None, "submitting", "submitted"}:
            submission_possible = True
        if latest_progress is not None:
            old = latest_progress["phase"]
            if ((old is None and phase not in {None, "submitted"})
                    or (old is not None and phase is not None
                        and PUBLISH_PHASES.index(phase) < PUBLISH_PHASES.index(old))):
                submission_possible = True
                return None
        if progress != latest_progress:
            latest_progress = progress
            observed.update(progress)
            emit = getattr(on_log, "event", None)
            if callable(emit):
                emit(dict(progress))
        return progress

    logged = set()
    selected_qr = qr_announced = False
    stopped = False
    try:
        while True:
            poll_progress()
            if cancel_event is not None and cancel_event.is_set():
                observed["cancelled"] = True
                _stop(process)
                stopped = True
                break
            if time.monotonic() - started >= (TIMEOUTS[action] if timeout_seconds is None else timeout_seconds):
                observed["timeout"] = True
                _stop(process)
                stopped = True
                break
            if master is not None and not qr_announced and BILIBILI_QR.is_file():
                BILIBILI_QR.chmod(0o600)
                on_log("二维码已生成：" + str(BILIBILI_QR))
                qr_announced = True
            events = reader.select(0.2)
            if not events:
                if process.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(fd, 8192)
            except OSError:
                chunk = b""
            if not chunk:
                break
            decoded = decoder.decode(chunk)
            buffer += decoded
            # dialoguer's observed menu defaults to SMS. Select QR only when
            # the actual menu contains both expected labels; never type secrets.
            if master is not None and not selected_qr:
                menu_buffer = (menu_buffer + decoded)[-16000:]
            if master is not None and not selected_qr and "选择一种登录方式" in menu_buffer and "扫码登录" in menu_buffer:
                clean_menu = _clean(menu_buffer)
                if re.search(r"[>❯]\s*短信登录", clean_menu):
                    os.write(master, b"\x1b[B\r")
                    selected_qr = True
                    on_log("已选择 B 站扫码登录，等待生成二维码。")
            while "\n" in buffer:
                raw, buffer = buffer.split("\n", 1)
                line = _clean(raw)
                label = _progress(line)
                if label == "B站上传进程仍在运行。":
                    on_log(f"B站上传进程仍在运行（已运行 {int(time.monotonic() - started)} 秒）。")
                elif label == "小红书视频仍在上传或处理中。":
                    on_log(f"小红书正在等待素材上传完成（任务已运行 {int(time.monotonic() - started)} 秒）。")
                elif label and label not in logged:
                    logged.add(label)
                    on_log(label)
            buffer = buffer[-16000:]
        observed["returncode"] = process.wait(timeout=5)
        if master is not None and not qr_announced and BILIBILI_QR.is_file():
            BILIBILI_QR.chmod(0o600)
            on_log("二维码已生成：" + str(BILIBILI_QR))
        final = _read_evidence(evidence, nonce, platform, action)
        observed["result"] = final
        if final:
            expected = {"publish": "submitted", "login": "login", "check": "valid"}[action]
            observed[expected] = final.get("status") == expected
            observed["invalid"] = action == "check" and final.get("status") == "invalid"
    except BaseException:
        if not stopped:
            _stop(process)
            stopped = True
        raise
    finally:
        # Also reap stray drivers when their CLI exits unexpectedly or returns
        # before a child finishes. Detached manual-correction Chrome is outside
        # this group and is intentionally retained by the TikTok adapter.
        try:
            if not stopped:
                _stop(process)
        finally:
            reader.close()
            if master is not None:
                os.close(master)
            if process.stdout is not None:
                process.stdout.close()
    final_progress = poll_progress()
    # Stop/reap the process group before taking the last phase snapshot. A
    # surviving driver must not advance after a retry has been deemed safe.
    observed["retry_safe"] = bool(
        final_progress and not submission_possible and observed["returncode"] > 0
        and not observed["cancelled"] and not observed["timeout"]
        and final_progress["phase"] in PUBLISH_PHASES[:5]
        and (platform != "bilibili" or final_progress["phase"] == "preparation")
    )
    return observed


def run(platform, action, metadata=None, on_log=None, cancel_event=None):
    on_log = on_log or (lambda message: None)
    result = {"platform": platform, "status": "failed", "message": "操作未完成"}
    if action == "publish":
        # Local validation happens before any child process exists.
        result.update(phase="preparation", retry_safe=True)
    try:
        _platform(platform)
        if action == "publish" and (metadata or {}).get("prepare_only") is True and platform not in PREPARE_PLATFORMS:
            raise ValueError("此平台尚未接入人工确认流程，未开始上传")
        if platform == "tiktok":
            return importlib.import_module("adapter_tiktok").run(action, metadata, on_log, cancel_event)
        command = build_command(platform, action, metadata)
    except (ValueError, ImportError, TypeError) as exc:
        return {**result, "message": str(exc)}
    if cancel_event is not None and cancel_event.is_set():
        return {**result, "message": "操作在启动前已取消。"}
    account_file = COOKIE_DIR / f"{platform}_default.json"
    if platform == "bilibili" and action == "login" and account_file.is_file():
        on_log("正在复用已保存的 B站账号，先检查是否需要扫码。")
        existing = run(platform, "check", None, on_log, cancel_event)
        if existing["status"] != "login_required":
            return existing
    if platform == "bilibili" and action == "login":
        # General browser readiness does not include the native QR component.
        # Report setup failures before starting the CLI, not as expired login.
        from install_biliup import installation_paths, installation_valid
        try:
            _, executable, version_file = installation_paths()
            installed = installation_valid(executable, version_file)
        except (OSError, SystemExit):
            installed = False
        if not installed:
            return {**result, "error_code": "login_component_missing",
                    "message": "B站扫码组件未安装或校验失败，二维码尚未生成。请在项目目录运行 .venv/bin/python install_biliup.py 安装组件，然后重新扫码；无需重装浏览器或同步 Chrome。"}
    account_file = COOKIE_DIR / f"{platform}_default.json"
    if action == "check" and not account_file.is_file():
        return {**result, "status": "login_required", "message": "未连接发布台，不能据此判断浏览器是否已登录。请同步 Chrome 会话或使用独立登录。"}
    if action == "publish" and not (metadata or {}).get("native_chrome_endpoint") and not account_file.is_file():
        return {**result, "message": "未连接发布台，不能据此判断浏览器是否已登录。未启动上传，请先连接账号。"}
    # There is no implemented AI disclosure selector in these uploaders. Do not
    # silently publish a user-declared AI video without the required platform step.
    if action == "publish" and metadata.get("ai_generated") and (platform in {"xiaohongshu", "kuaishou"} or (platform == "bilibili" and metadata.get("prepare_only") is not True)):
        return {**result, "status": "needs_attention", "retry_safe": False, "message": "该平台自动流程尚不能可靠填写 AI 内容声明；未启动上传，请在平台发布页手动填写声明后提交。"}
    runtime = runtime_status()
    if not runtime["ready"]:
        return {**result, "message": runtime["message"]}
    COOKIE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    COOKIE_DIR.chmod(0o700)
    # CLI "login" can merely report that its existing session is still valid.
    # That is not a replacement and must not discard an imported channel lock.
    login_before = None
    if action == "login" and account_file.is_file():
        try:
            login_before = hashlib.sha256(account_file.read_bytes()).digest()
        except OSError:
            return {**result, "message": "现有会话无法安全读取，未启动独立登录。"}
    on_log("正在复用原编辑页继续设置封面，不重新上传视频。" if action == "publish" and (metadata or {}).get("resume_cover")
           else {"login": "正在打开登录流程。", "check": "正在只读核验登录状态。", "publish": "正在启动真实上传流程。"}[action])
    try:
        observed = _execute(command, platform, action, on_log, cancel_event, metadata)
    except Exception:
        return {**result, "status": "needs_attention" if action == "publish" else "verification_unavailable",
                **({"phase": None, "retry_safe": False} if action == "publish" else {}),
                "message": "上传进程异常中断。请先到平台核验是否已有稿件，再决定是否重试。" if action == "publish" else "平台进程未能完成，请检查浏览器或安装环境。"}
    if action == "publish":
        result.update(phase=observed.get("phase"), retry_safe=False)
        if observed.get("error_code") in PUBLISH_ERROR_CODES:
            result["error_code"] = observed["error_code"]
    if account_file.is_file():
        account_file.chmod(0o600)
    if observed["cancelled"] or observed["timeout"]:
        return {**result, "status": "needs_attention" if action == "publish" else "verification_unavailable",
                "message": ("操作已取消。" if observed["cancelled"] else "操作已超时并停止。") +
                ("可能已有上传或提交，请先到平台核验，勿直接重发。" if action == "publish" else "未确认登录状态。")}
    if action == "publish" and metadata.get("prepare_only") is True:
        evidence = observed.get("result") or {}
        if (observed["returncode"] == 0 and evidence.get("status") == "prepared"
                and evidence.get("phase") == "ready_to_submit" and observed.get("phase") == "ready_to_submit"):
            return {**result, "status": "prepared", "phase": "ready_to_submit", "retry_safe": False,
                    "cover_status": "set_in_browser" if metadata.get("cover_path") else "not_requested",
                    "message": "视频、文案和封面已准备好。编辑窗口已保留，请切回该窗口检查，最后由你点击发布。"}
        diagnostic = _xhs_failure_diagnostic(evidence.get("diagnostic"))
        detail = ""
        if diagnostic:
            result["diagnostic"] = diagnostic
            detail = {"preparation": "准备阶段未完成。", "account": "当前页面账号尚未核验。", "browser": "任务页面连接失败。", "metadata": "文案或平台设置未完成。", "navigation": "发布页面未能打开。", "upload": "视频文件未能交给上传控件。", "title": "标题填写未完成。", "description": "正文填写未完成。", "resume": "无法安全续接原编辑页。",
                      "processing": "平台尚未处理好视频。", "topic": "话题填写未完成。", "cover": "封面设置未完成。", "handoff": "发布前复核尚未通过。"}[diagnostic["step"]]
            detail += "错误类型：" + diagnostic["error_type"] + "。"
            detail += {"browser_closed": "原专用浏览器已关闭。", "browser_unverified": "原专用浏览器身份无法核对。", "session_changed": "原窗口与当前授权会话不一致。", "snapshot_changed": "登录快照已更新，请先处理旧窗口。", "session_missing": "原窗口已无可核验的登录会话。", "editor_missing": "未找到仍保留原视频、标题和文案的编辑页。", "editor_ambiguous": "存在多个相同编辑页，需要先明确要继续哪一个。"}.get(diagnostic.get('reason'), '')
        name = next(p['name'] for p in PLATFORMS if p['id'] == platform)
        return {**result, "status": "needs_attention", "retry_safe": False,
                "message": detail + (f"普通 Chrome 的{name}任务页尚未完成准备，请在对应标签页查看；未点击发布。" if metadata.get("native_chrome_endpoint") else
                    f"{name}准备失败，未确认是否存在可接手的编辑窗口；未点击发布。") }
    if action == "check":
        if observed["returncode"] == 0 and observed["valid"]:
            return {**result, "status": "verified", "message": "平台登录状态已通过本次在线检查。"}
        if platform == "youtube":
            diagnostic = (observed.get("result") or {}).get("diagnostic")
            diagnosed = _youtube_check_result(diagnostic)
            if diagnosed:
                return {**result, **diagnosed}
        if observed["invalid"]:
            return {**result, "status": "login_required", "message": "平台已明确要求登录，请在原账号窗口完成验证。"}
        return {**result, "status": "verification_unavailable", "message": "暂时无法检查账号；已保留本地登录，请稍后重试检查，无需重新扫码。"}
    if action == "login":
        if observed["returncode"] == 0 and observed["login"] and account_file.is_file():
            # A successful independent login replaces the imported session.
            # Clear its old source/channel binding only after that success.
            if hashlib.sha256(account_file.read_bytes()).digest() != login_before and (observed.get("result") or {}).get("session_reused") is not True:
                importlib.import_module("browser_sessions").clear_metadata(platform, COOKIE_DIR)
            on_log("登录流程完成，正在再次核验平台登录状态。")
            return run(platform, "check", None, on_log, cancel_event)
        return {**result, "status": "verification_unavailable", "message": "登录暂未核验完成；已保留原会话和登录窗口，请完成验证后重试检查。"}
    if observed["returncode"] == 0 and observed["submitted"]:
        result.update(status="submitted", message="上游流程已报告提交完成；尚未核验审核结果或公开可见状态，请到平台作品列表确认。",
                      phase="submitted", retry_safe=False,
                      cover_status="submitted_unverified" if metadata.get("cover_path") else "not_requested")
        if observed["url"]:
            result["url"] = observed["url"]
        return result
    if (observed.get("retry_safe") is True and observed["returncode"] > 0
            and observed.get("phase") in PUBLISH_PHASES[:5]
            and platform in {"xiaohongshu", "bilibili"}
            and (platform != "bilibili" or observed["phase"] == "preparation")):
        diagnostic = _xhs_failure_diagnostic((observed.get("result") or {}).get("diagnostic")) if platform == "xiaohongshu" else None
        detail = ""
        if diagnostic:
            result["diagnostic"] = diagnostic
            step = {"navigation": "打开发布页面", "upload": "选择视频文件", "title": "标题填写", "description": "正文填写", "topic": "话题填写", "cover": "封面设置", "resume": "继续原编辑页"}[diagnostic["step"]]
            if "topic_index" in diagnostic:
                step += f"（第 {diagnostic['topic_index']} 个话题）"
            detail = f"{step}失败。"
        return {**result, "status": "failed", "retry_safe": True,
                "message": detail + "流程在提交前停止，尚未尝试发布；修正问题后可重试。平台可能已保留未提交的素材或草稿。"}
    return {**result, "status": "needs_attention", "message": "未获得完整的提交完成证据。请先到平台作品或草稿列表核验，勿直接重发。"}


def _xhs_failure_diagnostic(value):
    """Only fixed metadata reaches the UI; no exception or page text is retained."""
    if not isinstance(value, dict) or value.get("step") not in {"preparation", "account", "browser", "metadata", "navigation", "upload", "processing", "title", "description", "topic", "cover", "handoff", "resume"}:
        return None
    if value.get("error_type") not in {"TimeoutError", "RuntimeError", "ValueError", "TypeError", "Error", "AttributeError", "OSError", "FileNotFoundError", "PermissionError", "OtherError"}:
        return None
    safe = {key: value[key] for key in ("step", "error_type")}
    if value['step'] == 'resume' and value.get('reason') in {"browser_closed", "browser_unverified", "session_changed", "snapshot_changed", "session_missing", "editor_missing", "editor_ambiguous"}:
        safe['reason'] = value['reason']
    if value["step"] == "topic":
        for key, lower, upper in (("topic_index", 1, 10), ("candidate_count", 0, 100)):
            number = value.get(key)
            if type(number) is int and lower <= number <= upper:
                safe[key] = number
    return safe


def _youtube_check_result(diagnostic):
    """Turn typed probe reasons into useful messages, never echo arbitrary errors."""
    if not isinstance(diagnostic, dict):
        return None
    messages = {
        'session_lock_invalid': ('needs_attention', 'YouTube 会话与选定频道的绑定不一致，请在目标频道页重新同步；未更改已有会话。'),
        'browser_start_failed': ('failed', '用于检查 YouTube 的浏览器未能启动。请查看本地运行环境；这不代表 Chrome 中的账号已退出。'),
        'navigation_failed': ('failed', '连接 YouTube Studio 失败，未能核验登录；已同步的会话保留。'),
        'login_redirect': ('login_required', '已同步的 YouTube 会话未被独立检查窗口接受，页面转回 Google 登录。原 Chrome 的登录未被退出；不要反复检查或同步同一会话，当前可使用独立登录完成平台验证。'),
        'channel_mismatch': ('needs_attention', '已进入 YouTube Studio，但当前频道与同步时选定的频道不同，已停止检查以避免发错账号。'),
        'studio_not_ready': ('needs_attention', 'YouTube Studio 未在时限内完成加载，尚不能确认登录；已同步会话保留，请稍后检查。'),
    }
    reason = diagnostic.get('reason')
    if reason not in messages:
        return None
    status, message = messages[reason]
    safe = {'reason': reason}
    host = diagnostic.get('host')
    if host in {'studio.youtube.com', 'www.youtube.com', 'youtube.com', 'accounts.google.com', 'consent.youtube.com'}:
        safe['host'] = host
    stage = diagnostic.get('stage')
    if stage in {'session_lock', 'browser_start', 'context_setup', 'navigation', 'studio_readiness', 'complete'}:
        safe['stage'] = stage
    net_error = diagnostic.get('net_error')
    if isinstance(net_error, str) and re.fullmatch(r'(?:net::)?ERR_[A-Z0-9_]{1,80}', net_error):
        safe['net_error'] = net_error
        message += ' 网络错误：' + net_error + '。'
    http_status = diagnostic.get('http_status')
    if type(http_status) is int and 100 <= http_status <= 599:
        safe['http_status'] = http_status
        if http_status >= 400:
            message += f' HTTP {http_status}。'
    for key in ('actual_channel', 'expected_channel'):
        value = diagnostic.get(key)
        if isinstance(value, str) and re.fullmatch(r'UC[A-Za-z0-9_-]{22}', value):
            safe[key] = value
    return {'status': status, 'message': message, 'diagnostic': safe}
