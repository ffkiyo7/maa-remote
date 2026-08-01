#!/usr/bin/env python3
"""Dependency-free shared helpers for the Hermes MAA workflow."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import shlex
import shutil
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
RUN_LOCK_PATH = STATE_DIR / "run.lock"

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
    "stage": "",
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


def load_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


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


def maa_subprocess_env(config: dict[str, Any]) -> dict[str, str]:
    """Build the deterministic environment shared by every maa-cli call."""
    env = os.environ.copy()
    config_dir = str(config.get("maa_config_dir", MAA_CONFIG_DIR)).strip()
    if not config_dir:
        raise RuntimeError("maa_config_dir 不能为空")
    env["MAA_CONFIG_DIR"] = config_dir
    if bool(config.get("maa_disable_proxy", False)):
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
    return {
        "stage": str(raw.get("stage", "")),
        # Medication is intentionally not user-configurable. MaaCore receives
        # medicine=0 plus medicine_expire_days from the trusted local config.
        "medicine": "expiring",
        "skip": bool(raw.get("skip", False)),
        # Never trust persisted state for this guardrail.
        "stone": 0,
        "tasks": _normalized_tasks(raw.get("tasks")),
    }


def effective_plan(at: datetime | None = None) -> dict[str, Any]:
    at = at or now_beijing()
    result = normalize_plan(load_json(DEFAULTS_PATH, DEFAULT_PLAN))
    override = load_json(PLAN_PATH, {})
    if isinstance(override, dict) and override.get("date") == at.date().isoformat():
        for key in ("stage", "skip"):
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
