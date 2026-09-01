#!/usr/bin/env python3
"""Detect activity rotations and prepare one Discord-confirmed copilot run."""

from __future__ import annotations

import fcntl
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import maa_action
import maa_copilot
import maa_plan_card
from maa_common import (
    BUTTONS_PATH,
    STATE_DIR,
    atomic_write_json,
    discord_request,
    discord_token,
    is_run_locked,
    load_config,
    load_json,
    now_beijing,
    retire_discord_components,
    state_lock,
)


WATCH_PATH = STATE_DIR / "activity_watch.json"
WATCH_LOCK_PATH = STATE_DIR / "activity_watch.lock"


def _search_path(token: str) -> Path:
    return maa_copilot.SEARCHES_DIR / f"{token}.json"


def _minutes_since(value: Any, at: datetime) -> float:
    try:
        return max(0.0, (at - datetime.fromisoformat(str(value))).total_seconds() / 60)
    except (TypeError, ValueError):
        return float("inf")


def _notify(config: dict[str, Any], title: str, description: str, color: int) -> None:
    channel_id = str(config.get("channel_id") or "")
    if not channel_id.isdigit():
        raise RuntimeError("config.json 中尚未配置 Discord channel_id")
    discord_request(
        discord_token(),
        "POST",
        f"/channels/{channel_id}/messages",
        {
            "embeds": [
                {
                    "title": title[:256],
                    "description": description[:1500],
                    "color": color,
                    "timestamp": now_beijing().isoformat(),
                }
            ],
            "allowed_mentions": {"parse": []},
        },
    )


def _supersede_search(token: str, config: dict[str, Any]) -> bool:
    changed, message_id = maa_copilot.supersede_search(token)
    if not changed:
        return False
    try:
        retire_discord_components(config, message_id)
    except Exception as exc:
        print(
            f"旧活动卡按钮收起失败但已经失效：{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
    return True


def _start_search(
    target: dict[str, str],
    config: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Crash recovery: start_copilot_search persists its token before spawning
    # the child.  If this watcher died before writing WATCH_PATH, adopt that
    # state on the next cron instead of creating a second executable card.
    matching: list[dict[str, Any]] = []
    for path in maa_copilot.SEARCHES_DIR.glob("*.json"):
        state = load_json(path, None)
        if not isinstance(state, dict):
            raise RuntimeError(f"搜索状态格式错误：{path.name}")
        if (
            state.get("source") != "activity-watch"
            or state.get("activity_key") != target["key"]
            or state.get("stage") != target["stage"]
            or state.get("status") == "superseded"
        ):
            continue
        try:
            expired = now_beijing() > datetime.fromisoformat(
                str(state.get("expires_at") or "")
            )
        except ValueError:
            expired = True
        if expired:
            _supersede_search(str(state.get("token") or ""), config)
            continue
        matching.append(state)

    if matching:
        priority = {
            "canceled": 10,
            "executed": 10,
            "execution_failed": 10,
            "executing": 9,
            "queued": 8,
            "preparing": 8,
            "done": 7,
            "running": 6,
            "pending": 6,
            "failed": 5,
        }
        matching.sort(
            key=lambda state: (
                priority.get(str(state.get("status") or ""), 0),
                str(state.get("created_at") or ""),
                str(state.get("token") or ""),
            ),
            reverse=True,
        )
        adopted = matching[0]
        for duplicate in matching[1:]:
            _supersede_search(str(duplicate.get("token") or ""), config)
        search_status = str(adopted.get("status") or "")
        watch_status = {
            "done": "ready",
            "queued": "preparing",
            "preparing": "preparing",
            "executing": "executing",
            "canceled": "dismissed",
            "executed": "completed",
            "execution_failed": "completed",
            "failed": "waiting",
        }.get(search_status, "searching")
        record = {
            "observation_key": target["key"],
            "activity_title": target["title"],
            "target_stage": target["stage"],
            "search_token": str(adopted.get("token") or ""),
            "status": watch_status,
            "attempted_at": str(
                adopted.get("created_at") or now_beijing().isoformat()
            ),
            "updated_at": now_beijing().isoformat(),
            "last_notified_error": "",
        }
        if search_status == "execution_failed":
            record["execution_result"] = "failed"
        return record

    ok, message, token = maa_action.start_copilot_search(
        target["stage"],
        auto_confirm=True,
        activity_title=target["title"],
        activity_key=target["key"],
    )
    if not ok or token is None:
        raise RuntimeError(message)
    record = {
        "observation_key": target["key"],
        "activity_title": target["title"],
        "target_stage": target["stage"],
        "search_token": token,
        "status": "searching",
        "attempted_at": now_beijing().isoformat(),
        "updated_at": now_beijing().isoformat(),
        "last_notified_error": "",
    }
    if isinstance(previous, dict):
        record["last_notified_error"] = str(
            previous.get("last_notified_error") or ""
        )[:500]
    return record


def run_once() -> dict[str, Any]:
    config = load_config()
    snapshot = maa_plan_card.query_activity_snapshot(config)
    target = maa_plan_card.activity_target(snapshot)
    previous = load_json(WATCH_PATH, {})
    if not isinstance(previous, dict):
        raise RuntimeError("activity_watch.json 格式错误")
    previous.pop("probe_error", None)
    now = now_beijing()

    if target is None:
        if previous.get("observation_key"):
            empty_observations = int(previous.get("empty_observations") or 0) + 1
            if empty_observations < 2:
                previous["empty_observations"] = empty_observations
                previous["updated_at"] = now.isoformat()
                atomic_write_json(WATCH_PATH, previous)
                return previous
            token = str(previous.get("search_token") or "")
            if token:
                _supersede_search(token, config)
        maa_plan_card.persist_activity_snapshot(config, snapshot)
        idle = {
            "observation_key": "",
            "status": "idle",
            "updated_at": now.isoformat(),
        }
        atomic_write_json(WATCH_PATH, idle)
        return idle

    maa_plan_card.persist_activity_snapshot(config, snapshot)
    previous.pop("empty_observations", None)

    if previous.get("observation_key") != target["key"]:
        token = str(previous.get("search_token") or "")
        if token:
            _supersede_search(token, config)
        record = _start_search(target, config)
        atomic_write_json(WATCH_PATH, record)
        return record

    previous_stage = str(previous.get("target_stage") or "")
    if previous_stage and not maa_plan_card.activity_target_is_current(
        snapshot, previous_stage, target["key"]
    ):
        token = str(previous.get("search_token") or "")
        if token:
            _supersede_search(token, config)
        record = _start_search(target, config)
        atomic_write_json(WATCH_PATH, record)
        return record

    status = str(previous.get("status") or "")
    if status in {"dismissed", "completed"}:
        previous["updated_at"] = now.isoformat()
        atomic_write_json(WATCH_PATH, previous)
        return previous

    token = str(previous.get("search_token") or "")
    search = load_json(_search_path(token), None) if token else None
    search_status = str(search.get("status") or "") if isinstance(search, dict) else "missing"
    if search_status == "done":
        previous["status"] = "ready"
        previous["updated_at"] = now.isoformat()
        atomic_write_json(WATCH_PATH, previous)
        return previous
    if search_status == "executed":
        previous["status"] = "completed"
        previous["completed_at"] = str(search.get("executed_at") or now.isoformat())
        previous["updated_at"] = now.isoformat()
        atomic_write_json(WATCH_PATH, previous)
        return previous
    if search_status == "execution_failed":
        previous["status"] = "completed"
        previous["execution_result"] = "failed"
        previous["completed_at"] = str(
            search.get("execution_failed_at") or now.isoformat()
        )
        previous["updated_at"] = now.isoformat()
        atomic_write_json(WATCH_PATH, previous)
        return previous
    if search_status in {"queued", "preparing"}:
        restored = maa_copilot.recover_stale_prebattle_dispatch(
            token, search if isinstance(search, dict) else {}
        )
        if restored is not None:
            message = str(restored["last_prepare_error"])
            _notify(
                config,
                f"⚠️ {target['stage']} 自动作业未启动",
                message,
                0xFEE75C,
            )
            previous["status"] = "ready"
        else:
            previous["status"] = "preparing"
        previous["updated_at"] = now.isoformat()
        atomic_write_json(WATCH_PATH, previous)
        return previous
    if search_status == "executing":
        max_minutes = max(
            90,
            int(config.get("maa_run_timeout", 3000)) // 60 + 30,
        )
        if _minutes_since(search.get("execution_started_at"), now) > max_minutes and not is_run_locked():
            message = "执行进程已失联；因无法确认是否已消耗理智，本次不会自动重跑。"
            failed = False
            failed_message_id: Any = None
            with state_lock():
                fresh = load_json(_search_path(token), None)
                if isinstance(fresh, dict) and fresh.get("status") == "executing":
                    registry = load_json(BUTTONS_PATH, {})
                    if not isinstance(registry, dict):
                        raise RuntimeError("共享按钮登记表格式错误")
                    maa_copilot.prune_search_buttons(token, registry)
                    atomic_write_json(BUTTONS_PATH, registry)
                    fresh["status"] = "execution_failed"
                    fresh["execution_failed_at"] = now.isoformat()
                    fresh["error"] = message
                    failed_message_id = fresh.get("message_id")
                    atomic_write_json(_search_path(token), fresh)
                    failed = True
            if failed:
                try:
                    retire_discord_components(config, failed_message_id)
                except Exception as exc:
                    print(
                        "失联作业卡按钮收起失败但已经失效："
                        f"{type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                _notify(
                    config,
                    f"❌ {target['stage']} 自动作业执行失联",
                    message,
                    0xED4245,
                )
                previous["status"] = "completed"
                previous["execution_result"] = "failed"
                previous["completed_at"] = now.isoformat()
                previous["updated_at"] = now.isoformat()
                atomic_write_json(WATCH_PATH, previous)
                return previous
        previous["status"] = "executing"
        previous["updated_at"] = now.isoformat()
        atomic_write_json(WATCH_PATH, previous)
        return previous
    if search_status == "canceled":
        previous["status"] = "dismissed"
        previous["updated_at"] = now.isoformat()
        atomic_write_json(WATCH_PATH, previous)
        return previous

    retry_minutes = max(5, int(config.get("activity_search_retry_minutes", 30)))
    attempted_at = previous.get("attempted_at")
    if search_status in {"pending", "running"} and _minutes_since(
        attempted_at, now
    ) < retry_minutes:
        previous["updated_at"] = now.isoformat()
        atomic_write_json(WATCH_PATH, previous)
        return previous

    error = ""
    if isinstance(search, dict) and search_status == "failed":
        error = str(search.get("error") or "作业匹配失败")[:500]
    elif search_status == "missing":
        error = "搜索状态丢失或已过期"
    elif search_status in {"pending", "running"}:
        error = "作业匹配超时"
    else:
        error = f"作业匹配状态异常：{search_status or '空'}"

    if error != str(previous.get("last_notified_error") or ""):
        waiting = "暂时没有" in error and "作业" in error
        _notify(
            config,
            f"{'⏳' if waiting else '❌'} {target['title']}自动作业尚未就绪",
            (
                f"已检测到 {target['stage']}，prts.plus 暂无满足当前练度的作业；"
                f"每 {retry_minutes} 分钟自动重试。"
                if waiting
                else f"{error}\n每 {retry_minutes} 分钟自动重试；不会自动执行。"
            ),
            0xFEE75C if waiting else 0xED4245,
        )
        previous["last_notified_error"] = error

    if _minutes_since(attempted_at, now) < retry_minutes:
        previous["status"] = "waiting"
        previous["updated_at"] = now.isoformat()
        atomic_write_json(WATCH_PATH, previous)
        return previous

    if token:
        _supersede_search(token, config)
    record = _start_search(target, config, previous)
    atomic_write_json(WATCH_PATH, record)
    return record


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with WATCH_LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        os.chmod(WATCH_LOCK_PATH, 0o600)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            run_once()
            return 0
        except Exception as exc:
            config = load_config()
            state = load_json(WATCH_PATH, {})
            if not isinstance(state, dict):
                state = {}
            error = f"{type(exc).__name__}: {exc}"[:500]
            if error != str(state.get("probe_error") or ""):
                _notify(config, "❌ MAA 活动监控失败", error, 0xED4245)
            state["probe_error"] = error
            state["updated_at"] = now_beijing().isoformat()
            atomic_write_json(WATCH_PATH, state)
            print(f"MAA 活动监控失败：{error}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
