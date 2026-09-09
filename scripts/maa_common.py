#!/usr/bin/env python3
"""Dependency-free shared helpers for the Hermes MAA workflow."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
MAA_CONFIG_DIR = Path(
    os.environ.get("MAA_CONFIG_DIR", Path.home() / ".config" / "maa-cli")
)
SCRIPTS_DIR = HERMES_HOME / "scripts"
STATE_DIR = HERMES_HOME / "state" / "maa"
RUNS_DIR = STATE_DIR / "runs"
CONFIG_PATH = STATE_DIR / "config.json"
DEFAULTS_PATH = STATE_DIR / "defaults.json"
PLAN_PATH = STATE_DIR / "plan.json"
LAST_RUN_PATH = STATE_DIR / "last_run.json"
# 活动 watcher 的粗状态（当期活动、每关退避记账、本轮 token）。三个模块都读它，
# 路径只在这里定义一次。
WATCH_PATH = STATE_DIR / "activity_watch.json"
RUN_LOCK_PATH = STATE_DIR / "run.lock"
LAST_RUN_LOCK_PATH = STATE_DIR / "last_run.lock"
# 手动场选关请求（`PLAN_MANUAL_RUN.md` M-a）：单文件、同一时刻只有一个请求。
# 「现在跑」发一张选关卡并写 `pending`，从卡上选了关才派发 `maa_daily.py now <token>`；
# 10 分钟不选作废。手动关**不写 `plan.json`**（M4），只经这份文件传给手动场。
MANUAL_RUN_PATH = STATE_DIR / "manual_run.json"
MANUAL_RUN_TTL = timedelta(minutes=10)
# 选关卡自己的登记前缀：不在 `hermes:btn:maa:` 下（07:50 / 21:50 建卡整前缀清扫碰不到），
# 也不在 `hermes:btn:maa-copilot:` 下（`prune_expired_searches()` 碰不到）。
MANUAL_RUN_BUTTON_PREFIX = "hermes:btn:maa-now:"
# 定时场优先（M10）：距 08:00 / 22:00 不足这么多分钟，手动场既不发卡也不派发。
# 实测一场手动日常约 14 分钟 + 卡片 10 分钟有效期。
DEFAULT_MANUAL_RUN_BLACKOUT_MINUTES = 30

# Hermes' persistent-button patch intentionally has one global registry. MAA
# must merge its keys under the same lock used by agent-book.
BUTTONS_PATH = HERMES_HOME / "state" / "agent-book" / "buttons.json"
STATE_LOCK_PATH = HERMES_HOME / "state" / "agent-book" / "state.lock"

DISCORD_API_BASE = "https://discord.com/api/v10"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_TASKS = {
    "fight": True,
    "infrast": True,
    "recruit": True,
    "mall": True,
    "award": True,
}
DEFAULT_PLAN = {
    "stage": "1-7",
    # Kept in the normalized snapshot for backwards-compatible reporting.
    # The policy is fixed: no ordinary medicine, expiring medicine only.
    "medicine": "expiring",
    "skip": False,
    "stone": 0,
    "tasks": DEFAULT_TASKS,
}
PROXY_ENV_KEYS = (
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "all_proxy",
    "https_proxy",
    "http_proxy",
)
# 计划的覆盖是**按场次**的（四期 D1~D3）：早场改了关，晚场仍走缺口默认。
# `now`（手动场）不属于任何场次，永远只吃 defaults（D4）。
PLAN_PHASES = ("am", "pm")
# 一个场次覆盖允许持久化的字段。`stage_source` 必须在里面：卡片靠它区分
# 「这关是自动定的」和「你自己选的」，掉了它点一次「跳过」就会冻住当天的自动定关。
# `farming_drops` / `farming_fallback_stage` 是四期步骤 3 加的：前者是
# `{itemId: 还差多少个}`（渲染成 Fight 的 `drops`，刷够即停），后者是刷够之后
# 接着刷的第二个关。两者由 `maa_plan_card.apply_auto_stage()` 写，
# `maa_action.set_stage()` 在手动选关时清空——手动选的关不带上限。
PLAN_SESSION_FIELDS = (
    "stage",
    "stage_source",
    "skip",
    "source",
    "updated_at",
    "farming_drops",
    "farming_fallback_stage",
)
# 传给 `open -a` / `pgrep -f` 的模拟器名白名单：任何子进程都不接受未校验的名字。
SAFE_APP_RE = re.compile(r"^[A-Za-z0-9 ._+-]{1,80}$")
# 上游 itemId 的字符集（`30013` / `mod_unlock_token`）。它会原样变成 `daily.toml`
# 里 `drops` 表的键，所以在写进计划之前就校验，不留「渲染时再说」的口子。
SAFE_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def now_beijing() -> datetime:
    return datetime.now(BEIJING)


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def ensure_state_dirs() -> None:
    ensure_private_dir(STATE_DIR)
    ensure_private_dir(RUNS_DIR)
    ensure_private_dir(BUTTONS_PATH.parent)


@contextmanager
def state_lock() -> Iterator[None]:
    """Serialize all writers to Hermes' shared persistent-button registry."""
    ensure_private_dir(STATE_LOCK_PATH.parent)
    with STATE_LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        os.chmod(STATE_LOCK_PATH, 0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def run_lock(*, blocking: bool = False) -> Iterator[bool]:
    """Yield whether this process owns the single MAA execution lock."""
    ensure_private_dir(STATE_DIR)
    with RUN_LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        os.chmod(RUN_LOCK_PATH, 0o600)
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        acquired = False
        try:
            fcntl.flock(lock_file.fileno(), flags)
            acquired = True
        except BlockingIOError:
            pass
        try:
            yield acquired
        finally:
            if acquired:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def is_run_locked() -> bool:
    with run_lock(blocking=False) as acquired:
        return not acquired


@contextmanager
def last_run_lock() -> Iterator[None]:
    """Serialize last_run.json read-modify-write cycles across MAA processes."""
    ensure_private_dir(STATE_DIR)
    with LAST_RUN_LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        os.chmod(LAST_RUN_LOCK_PATH, 0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def load_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def http_get_json(url: str, *, timeout: int = 30, max_bytes: int = MAX_RESPONSE_BYTES) -> Any:
    """Small dependency-free GET for JSON APIs (prts, etc.)."""
    request = urllib.request.Request(url, headers={"User-Agent": "Hermes-MAA/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise RuntimeError("HTTP 响应超过大小上限")
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(64 * 1024).decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网络错误: {exc.reason}") from exc


def prune_button_registry(
    prefix: str,
    keep: set[str] | None = None,
    remove_only: set[str] | None = None,
) -> None:
    """Remove registry keys under a prefix, or only the ids in remove_only.

    Must be called under state_lock().  Returns nothing; the caller decides
    when to persist.  Never touches keys outside the prefix / remove list.
    """
    registry = load_json(BUTTONS_PATH, {})
    if not isinstance(registry, dict):
        return
    keep = keep or set()
    remove_only = remove_only or set()
    changed = False
    for custom_id in list(registry):
        if remove_only:
            if custom_id in remove_only:
                registry.pop(custom_id)
                changed = True
        elif custom_id.startswith(prefix) and custom_id not in keep:
            registry.pop(custom_id)
            changed = True
    if changed:
        atomic_write_json(BUTTONS_PATH, registry)


def atomic_write_json(path: Path, value: Any) -> None:
    """Write private JSON state atomically and durably."""
    ensure_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, value: str, *, mode: int = 0o600) -> None:
    ensure_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
        os.chmod(path, mode)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def load_config() -> dict[str, Any]:
    raw = load_json(CONFIG_PATH, {})
    if not isinstance(raw, dict):
        raise RuntimeError("config.json 必须是 JSON object")
    return raw


def require_executable(
    config: dict[str, Any],
    key: str,
    default: str,
    label: str,
) -> str:
    """Resolve one configured executable or fail before opening the emulator."""
    configured = str(config.get(key, default)).strip()
    resolved = configured if os.path.isabs(configured) else shutil.which(configured)
    if not resolved or not Path(resolved).is_file() or not os.access(resolved, os.X_OK):
        raise RuntimeError(f"{label} 不存在或不可执行：{configured}")
    return str(resolved)


def maa_subprocess_env(
    config: dict[str, Any],
    *,
    keep_proxy: bool = False,
) -> dict[str, str]:
    """Build the deterministic environment shared by every maa-cli call.

    Daily ``maa run`` strips proxy variables: MaaCore's in-run hot update
    misbehaves when those are set. ``maa update`` must keep them so GitHub
    downloads go through the machine's always-on proxy.
    """
    env = os.environ.copy()
    config_dir = str(config.get("maa_config_dir", MAA_CONFIG_DIR)).strip()
    if not config_dir:
        raise RuntimeError("maa_config_dir 不能为空")
    env["MAA_CONFIG_DIR"] = config_dir
    if bool(config.get("maa_disable_proxy", False)) and not keep_proxy:
        for key in PROXY_ENV_KEYS:
            env.pop(key, None)
    return env


def _normalized_tasks(value: Any) -> dict[str, bool]:
    tasks = dict(DEFAULT_TASKS)
    if isinstance(value, dict):
        for key in tasks:
            if key in value:
                tasks[key] = bool(value[key])
    return tasks


def normalize_plan(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    result = {
        "stage": str(raw.get("stage", DEFAULT_PLAN["stage"])),
        # Medication is intentionally not user-configurable. MaaCore receives
        # medicine=0 plus medicine_expire_days from the trusted local config.
        "medicine": "expiring",
        "skip": bool(raw.get("skip", False)),
        # Never trust persisted state for this guardrail.
        "stone": 0,
        "tasks": _normalized_tasks(raw.get("tasks")),
    }
    # `stage_source` 要**穿过**这层规范化：它是「这关是谁定的」的唯一依据。
    # 以前这里把它连同其余字段一起丢掉，卡片只好绕开 `effective_plan()`
    # 直接读 `plan.json`（PLAN_CLEARING 不变量 #31 的教训）。
    stage_source = str(raw.get("stage_source") or "")
    if stage_source:
        result["stage_source"] = stage_source
    # Fight 的 `drops` 上限与「刷够之后接着刷哪关」，同样要穿过这层规范化
    # （四期步骤 3）。**空值等于没有**：手动选关时写空就是「这关不带上限」。
    drops = raw.get("farming_drops")
    if isinstance(drops, dict) and drops:
        capped: dict[str, int] = {}
        for item_id, quantity in drops.items():
            try:
                count = int(quantity)
            except (TypeError, ValueError):
                continue
            # itemId 会原样进 TOML 的表键，只接受上游那套 id 的字符集。
            if count > 0 and SAFE_ITEM_ID_RE.match(str(item_id)):
                capped[str(item_id)] = count
        if capped:
            result["farming_drops"] = capped
    fallback = str(raw.get("farming_fallback_stage") or "")
    if fallback:
        result["farming_fallback_stage"] = fallback
    return result


def plan_sessions(at: datetime | None = None) -> dict[str, dict[str, Any]]:
    """当天 `plan.json` 的场次表 `{场次: 覆盖}`。

    只认新结构 `{date, sessions: {am, pm}, updated_at}`。**旧的按天格式**
    （顶层 `stage` / `skip`）读到就当空——它一天后自然失效，不值得为它写迁移。
    """
    moment = at or now_beijing()
    raw = load_json(PLAN_PATH, {})
    if not isinstance(raw, dict) or raw.get("date") != moment.date().isoformat():
        return {}
    sessions = raw.get("sessions")
    if not isinstance(sessions, dict):
        return {}
    return {
        str(name): dict(override)
        for name, override in sessions.items()
        if isinstance(override, dict)
    }


def session_override(phase: str, at: datetime | None = None) -> dict[str, Any]:
    """一个场次今天的覆盖；没有就是空 dict。"""
    return plan_sessions(at).get(str(phase), {})


def effective_plan(
    at: datetime | None = None, phase: str = "now"
) -> dict[str, Any]:
    """defaults ⊕ 当天 `sessions.<phase>` 的覆盖。

    `phase` 取 `am` / `pm` / `now`。手动场 `now` 从来没有人写过它的场次覆盖，
    所以它天然等于默认计划——这就是 D4「现在跑取缺口默认关，不看卡片上的手动选择」。
    """
    at = at or now_beijing()
    result = normalize_plan(load_json(DEFAULTS_PATH, DEFAULT_PLAN))
    override = session_override(phase, at)
    for key in (
        "stage",
        "stage_source",
        "skip",
        "farming_drops",
        "farming_fallback_stage",
    ):
        if key in override:
            result[key] = override[key]
    result = normalize_plan(result)
    result["date"] = at.date().isoformat()
    return result


def plan_stage_label(plan: dict[str, Any]) -> str:
    return str(plan.get("stage") or "上次的本")


def medicine_label(value: str) -> str:
    del value
    return "普通理智药 0 · 自动使用临期药"


def discord_token() -> str:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if token:
        return token

    env_path = HERMES_HOME / ".env"
    try:
        stat = env_path.stat()
        if stat.st_mode & 0o077:
            raise RuntimeError("Hermes .env 权限过宽")
        if stat.st_size > 256 * 1024:
            raise RuntimeError("Hermes .env 文件过大")
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise RuntimeError("定时任务无法读取 DISCORD_BOT_TOKEN") from exc

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        key, separator, raw_value = stripped.partition("=")
        if separator and key.strip() == "DISCORD_BOT_TOKEN":
            lexer = shlex.shlex(raw_value.strip(), posix=True)
            lexer.whitespace_split = True
            lexer.commenters = "#"
            token = "".join(list(lexer)).strip()
            if token:
                return token
            break
    raise RuntimeError("定时任务无法读取 DISCORD_BOT_TOKEN")


def _discord_http_error(exc: urllib.error.HTTPError) -> tuple[str, float | None]:
    raw = exc.read(64 * 1024)
    detail = raw.decode("utf-8", errors="replace")
    retry_after = None
    try:
        payload = json.loads(detail)
        if isinstance(payload, dict) and payload.get("retry_after") is not None:
            retry_after = float(payload["retry_after"])
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    return detail, retry_after


def discord_request(
    token: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: int = 20,
    attempts: int = 4,
) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    for attempt in range(attempts):
        request = urllib.request.Request(
            f"{DISCORD_API_BASE}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": "Hermes-MAA/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status == 204:
                    return None
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise RuntimeError("Discord response exceeded size limit")
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail, retry_after = _discord_http_error(exc)
            if exc.code == 429 and retry_after is not None and attempt + 1 < attempts:
                time.sleep(min(max(retry_after, 0.1), 30.0))
                continue
            raise RuntimeError(f"Discord API {exc.code}: {detail}") from exc
    raise RuntimeError("Discord API retry limit reached")


def retire_discord_components(config: dict[str, Any], message_id: Any) -> bool:
    """Remove controls from one tracked Discord message.

    Old search records may not have a message id; that is reported as False.
    API failures deliberately propagate so callers can make the UI error
    visible even though the local registry has already been made safe.
    """
    channel_id = str(config.get("channel_id") or "")
    message = str(message_id or "")
    if not channel_id.isdigit() or not message.isdigit():
        return False
    discord_request(
        discord_token(),
        "PATCH",
        f"/channels/{channel_id}/messages/{message}",
        {"components": []},
    )
    return True


def discord_upload(
    token: str,
    channel_id: str,
    payload: dict[str, Any],
    image_path: Path,
    *,
    filename: str = "shot.png",
    timeout: int = 30,
) -> Any:
    """Send one Discord message containing payload_json and one PNG file."""
    if not image_path.is_file():
        raise FileNotFoundError(image_path)
    boundary = f"----HermesMAA{secrets.token_hex(16)}"
    payload_json = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    image_data = image_path.read_bytes()
    if len(image_data) > 25 * 1024 * 1024:
        raise RuntimeError("截图超过 Discord 单文件限制")
    crlf = b"\r\n"
    parts = [
        f"--{boundary}".encode(),
        b'Content-Disposition: form-data; name="payload_json"',
        b"Content-Type: application/json",
        b"",
        payload_json,
        f"--{boundary}".encode(),
        f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"'.encode(),
        b"Content-Type: image/png",
        b"",
        image_data,
        f"--{boundary}--".encode(),
        b"",
    ]
    data = crlf.join(parts)
    request = urllib.request.Request(
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(data)),
            "User-Agent": "Hermes-MAA/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError("Discord response exceeded size limit")
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail, _retry_after = _discord_http_error(exc)
        raise RuntimeError(f"Discord API {exc.code}: {detail}") from exc


def create_thread(
    token: str,
    channel_id: str,
    message_id: str,
    name: str,
) -> dict[str, Any]:
    return discord_request(
        token,
        "POST",
        f"/channels/{channel_id}/messages/{message_id}/threads",
        {"name": name[:100], "auto_archive_duration": 1440},
    )


def delete_channel(token: str, channel_id: str) -> None:
    discord_request(token, "DELETE", f"/channels/{channel_id}")


def cleanup_run_files(retention_days: int = 14, at: datetime | None = None) -> list[str]:
    ensure_private_dir(RUNS_DIR)
    cutoff = (at or now_beijing()) - timedelta(days=retention_days)
    removed: list[str] = []
    for path in RUNS_DIR.iterdir():
        if not path.is_file() or path.is_symlink():
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=BEIJING)
        if modified < cutoff:
            path.unlink()
            removed.append(path.name)
    return removed


def merge_last_run(update: dict[str, Any]) -> dict[str, Any]:
    with last_run_lock():
        current = load_json(LAST_RUN_PATH, {})
        if not isinstance(current, dict):
            current = {}
        threads = current.get("threads", [])
        if not isinstance(threads, list):
            threads = []
        safe_update = dict(update)
        update_threads = safe_update.pop("threads", None)
        current.update(safe_update)
        if isinstance(update_threads, list):
            known = {str(item.get("thread_id")) for item in threads if isinstance(item, dict)}
            for item in update_threads:
                if isinstance(item, dict) and str(item.get("thread_id")) not in known:
                    threads.append(item)
        current["threads"] = threads
        atomic_write_json(LAST_RUN_PATH, current)
        return current


# ---------------------------------------------------------------------------
# 模拟器占用探测（四期 D18）：所有自动行为开模拟器前都走这一份实现
# ---------------------------------------------------------------------------

def emulator_is_online(config: dict[str, Any]) -> tuple[bool, str]:
    """模拟器是不是已经开着——也就是「用户此刻在玩」？

    在**每一条自动链路**抢到 `run.lock` 之后、`open_emulator()` 之前调用
    （日常 am/pm、抄作业 `run-batch`、Depot）。动机是 `PLAN_NAV_GATE.md`
    结论 3：任何失败路径都会 `close_everything()` 直接退掉 BlueStacks，
    等于把正在玩的用户踢出游戏。用户的决定是「先探，不改清理逻辑」。

    **这不是 `settle_android_runtime()`**：那个在我们开完模拟器、安卓起来之后跑，
    等 `app_process` 别抖，免得 MaaTouch 超时（DEV_GUIDE §11.14）。也不是
    `wait_for_android()`：那个为刚启动的设备等 180 秒；这个必须在几秒内回答
    一台**不是我们启动**的设备的状态。

    两个信号取或——命中任一即「在线」：

      1. 模拟器进程存在（`pgrep -f`）
      2. adb 连得上且 `sys.boot_completed` 是 `1`

    单独哪个都不够：刚拉起的 BlueStacks 还没有安卓，adb 会漏掉用户明明在用的设备；
    而挂死的进程能比它的设备活得久，pgrep 会在什么都不工作时命中。

    **永不抛异常。** 探测本身出错或超时一律报离线、照常执行：误抢一次设备远比
    「探测坏了导致日常永久停摆」便宜（B-4）。总预算 `emulator_probe_timeout`
    （默认 5s），不会吃掉运行时间，也守得住 <20s 的按钮预算（不变量 #6）。
    """
    budget = max(0.0, float(config.get("emulator_probe_timeout", 5)))
    if not budget:
        return False, "探测已禁用（emulator_probe_timeout = 0）"
    deadline = time.monotonic() + budget

    def remaining(cap: float) -> float:
        return min(cap, deadline - time.monotonic())

    app = str(config.get("emulator_app", "BlueStacks"))
    if not SAFE_APP_RE.fullmatch(app):
        # Same guard as open_emulator(): never hand an unvalidated name to a
        # subprocess. Refusing to probe is not refusing to run.
        return False, "emulator_app 名称含非法字符，跳过探测"
    notes: list[str] = []
    slice_ = remaining(2.0)
    if slice_ > 0:
        try:
            found = subprocess.run(
                ["pgrep", "-f", app],
                capture_output=True,
                text=True,
                check=False,
                timeout=slice_,
            )
            if found.returncode == 0 and found.stdout.strip():
                return True, f"{app} 进程在跑"
            notes.append(f"{app} 进程不在")
        except (subprocess.TimeoutExpired, OSError) as exc:
            notes.append(f"进程探测失败（{type(exc).__name__}）")
    else:
        notes.append("进程探测超预算未跑")

    adb = str(config.get("adb_path", "/opt/homebrew/bin/adb"))
    address = str(config.get("adb_address", "127.0.0.1:5555"))
    slice_ = remaining(3.0)
    if slice_ <= 0:
        notes.append("ADB 探测超预算未跑")
        return False, "；".join(notes)
    try:
        subprocess.run(
            [adb, "connect", address],
            capture_output=True,
            text=True,
            check=False,
            timeout=slice_,
        )
        slice_ = remaining(3.0)
        if slice_ <= 0:
            notes.append("ADB getprop 超预算未跑")
            return False, "；".join(notes)
        boot = subprocess.run(
            [adb, "-s", address, "shell", "getprop", "sys.boot_completed"],
            capture_output=True,
            text=True,
            check=False,
            timeout=slice_,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        notes.append(f"ADB 探测失败（{type(exc).__name__}）")
        return False, "；".join(notes)
    if boot.returncode == 0 and boot.stdout.strip() == "1":
        return True, f"ADB {address} 已就绪（sys.boot_completed = 1）"
    notes.append("ADB 未连上或系统未就绪")
    return False, "；".join(notes)


def report_emulator_busy(
    config: dict[str, Any],
    *,
    title: str,
    reason: str,
    detail: str,
    at: datetime,
    state_update: dict[str, Any],
    token: str = "",
    channel_id: str = "",
) -> None:
    """探到模拟器在用 → 写 `last_run.json` + 发一张灰卡。

    每条自动链路的「已在线」分支都走这里：跳过必须**看得见**，
    静默 return 是 PLAN_CLEARING 护栏 4 明令禁止的（不变量 #24）。
    两件事各自 try，任一失败都汇总成 RuntimeError 抛给调用方。
    """
    errors: list[str] = []
    try:
        merge_last_run(state_update)
    except Exception as exc:
        errors.append(f"跳过状态写入失败：{type(exc).__name__}: {exc}")
    try:
        channel = channel_id or str(config.get("channel_id", "")).strip()
        discord_request(
            token or discord_token(),
            "POST",
            f"/channels/{channel}/messages",
            {
                "embeds": [
                    {
                        "title": title,
                        "description": f"{reason}\n探测：{detail}",
                        "color": 0x99AAB5,
                        "timestamp": at.isoformat(),
                    }
                ],
                "allowed_mentions": {"parse": []},
            },
        )
    except Exception as exc:
        errors.append(f"跳过告警发送失败：{type(exc).__name__}: {exc}")
    if errors:
        raise RuntimeError("；".join(errors))


def cleanup_old_threads(token: str, at: datetime | None = None) -> dict[str, int]:
    at = at or now_beijing()
    current = load_json(LAST_RUN_PATH, {})
    if not isinstance(current, dict):
        current = {}
    threads = current.get("threads", [])
    if not isinstance(threads, list):
        threads = []
    kept: list[dict[str, Any]] = []
    deleted = 0
    failed = 0
    for item in threads:
        if not isinstance(item, dict):
            continue
        try:
            created = datetime.fromisoformat(str(item["created_at"])).astimezone(BEIJING)
            is_old = at - created >= timedelta(hours=24)
        except (KeyError, ValueError, TypeError):
            is_old = True
        if not is_old:
            kept.append(item)
            continue
        try:
            delete_channel(token, str(item["thread_id"]))
            deleted += 1
        except Exception:
            item["delete_failed_at"] = at.isoformat()
            kept.append(item)
            failed += 1
    current["threads"] = kept
    atomic_write_json(LAST_RUN_PATH, current)
    return {"deleted": deleted, "failed": failed}
