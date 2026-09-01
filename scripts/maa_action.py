#!/usr/bin/env python3
"""The only permitted mutation and dispatch entry point for the MAA plan."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import maa_copilot
from maa_common import (
    BEIJING,
    BUTTONS_PATH,
    CONFIG_PATH,
    LAST_RUN_PATH,
    PLAN_PATH,
    RUNS_DIR,
    SCRIPTS_DIR,
    atomic_write_json,
    effective_plan,
    is_run_locked,
    load_config,
    load_json,
    maa_subprocess_env,
    medicine_label,
    now_beijing,
    plan_stage_label,
    require_executable,
    retire_discord_components,
    state_lock,
)


STAGE_PATTERN = re.compile(r"^[A-Za-z0-9]{1,8}-\d{1,2}$")
ACTIVITY_STAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,30}-\d{1,2}$")


def emit(ok: bool, message: str) -> None:
    # Persistent button runner accepts at most 8192 bytes and parses only JSON.
    raw = json.dumps({"ok": ok, "message": message}, ensure_ascii=False)
    if len(raw.encode("utf-8")) > 7900:
        raw = json.dumps(
            {"ok": ok, "message": message[:1800] + "\n…（已截断）"},
            ensure_ascii=False,
        )
    print(raw)


def _activity_output_allows(stage: str, config: dict[str, Any]) -> bool:
    configured = config.get("activity_stages", [])
    if isinstance(configured, list) and stage in {str(item) for item in configured}:
        return True
    if not ACTIVITY_STAGE_PATTERN.fullmatch(stage):
        return False
    maa_bin = str(config.get("maa_bin", "/opt/homebrew/bin/maa"))
    try:
        result = subprocess.run(
            [maa_bin, "activity", "Official"],
            capture_output=True,
            text=True,
            timeout=0.7,
            check=False,
            env=maa_subprocess_env(config),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    output = f"{result.stdout}\n{result.stderr}"
    token = re.compile(
        rf"(?<![A-Za-z0-9_-]){re.escape(stage)}(?![A-Za-z0-9_-])"
    )
    return result.returncode == 0 and bool(token.search(output))


def valid_stage(stage: str, config: dict[str, Any]) -> bool:
    return stage == "" or bool(STAGE_PATTERN.fullmatch(stage)) or _activity_output_allows(stage, config)


def _today_override() -> dict[str, Any]:
    now = now_beijing()
    current = load_json(PLAN_PATH, {})
    if not isinstance(current, dict) or current.get("date") != now.date().isoformat():
        current = {"date": now.date().isoformat()}
    # Discard all fields outside the script-level allowlist.
    return {
        key: current[key]
        for key in ("date", "stage", "skip", "source", "updated_at")
        if key in current
    }


def _save_override(key: str, value: Any) -> dict[str, Any]:
    with state_lock():
        plan = _today_override()
        plan[key] = value
        plan["source"] = "discord"
        plan["updated_at"] = now_beijing().isoformat()
        # There is deliberately no stone field in this write path.
        atomic_write_json(PLAN_PATH, plan)
    return effective_plan()


def _running_context() -> tuple[str, str]:
    state = load_json(LAST_RUN_PATH, {})
    if not isinstance(state, dict) or state.get("status") != "running":
        return "本场", "上次的本"
    phase = str(state.get("phase", "now"))
    phase_label = {"am": "早场", "pm": "晚场", "now": "手动场"}.get(phase, "本场")
    plan = state.get("plan", {})
    stage = plan_stage_label(plan if isinstance(plan, dict) else {})
    return phase_label, stage


def _confirmation(plan: dict[str, Any], was_running: bool) -> str:
    now = now_beijing()
    stage = plan_stage_label(plan)
    medicine = medicine_label(str(plan.get("medicine", "none")))
    skip = bool(plan.get("skip"))
    if was_running:
        phase, running_stage = _running_context()
        if phase == "早场":
            target = "晚 10 点那场会跳过" if skip else f"晚 10 点那场刷 {stage}（{medicine}）"
        elif phase == "晚场":
            target = "今天没有后续自动场次，本次覆盖明天会自动失效"
        else:
            target = "下一场仍在今天时会采用新计划"
        return f"{phase}正在跑 {running_stage}，不打断它；{target}。"
    if skip:
        return (
            f"今天（{now.month}/{now.day}）的自动场次已设为跳过；"
            "说「现在跑」仍然可以手动开一场。"
        )
    return (
        f"今天（{now.month}/{now.day}）刷 {stage}，{medicine}；"
        "早 8 点和晚 10 点各跑一次。"
    )


def set_stage(stage: str) -> tuple[bool, str]:
    config = load_config()
    if not valid_stage(stage, config):
        return (
            False,
            "关卡不在白名单：只接受常规关卡格式（如 CE-6、1-7）、空串（上次的本）"
            "或当前活动关。碎石设置永远不受支持。",
        )
    was_running = is_run_locked()
    plan = _save_override("stage", stage)
    return True, _confirmation(plan, was_running)


def set_medicine(value: str) -> tuple[bool, str]:
    del value
    return False, "理智规则已固定：普通理智药 0，只自动使用临期药，碎石 0。"


def skip_today(value: str) -> tuple[bool, str]:
    if value not in {"on", "off"}:
        return False, "skip-today 只接受 on 或 off。"
    was_running = is_run_locked()
    plan = _save_override("skip", value == "on")
    return True, _confirmation(plan, was_running)


def show_plan() -> tuple[bool, str]:
    plan = effective_plan()
    status = "跳过" if plan.get("skip") else "照常执行"
    return (
        True,
        f"今天刷：{plan_stage_label(plan)}；理智药："
        f"{medicine_label(str(plan.get('medicine', 'none')))}；{status}；碎石：0。",
    )


def run_now() -> tuple[bool, str]:
    if is_run_locked():
        phase, stage = _running_context()
        return False, f"{phase}已经在跑 {stage}，没有重复启动。"
    script = SCRIPTS_DIR / "maa_daily.py"
    if not script.is_file():
        return False, "maa_daily.py 尚未部署。"
    config = load_config()
    try:
        require_executable(config, "maa_bin", "/opt/homebrew/bin/maa", "maa-cli")
        require_executable(config, "adb_path", "/opt/homebrew/bin/adb", "ADB")
    except RuntimeError as exc:
        return False, f"暂时不能运行：{exc}"
    subprocess.Popen(
        [sys.executable, str(script), "now"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    note = "（今天设了跳过自动场次，手动场照跑）" if effective_plan().get("skip") else ""
    return True, f"▶️ 已派发手动场{note}；后台会自行加锁，开跑消息稍后出现在频道。"


def log_tail() -> tuple[bool, str]:
    state = load_json(LAST_RUN_PATH, {})
    if not isinstance(state, dict) or not state.get("status"):
        return True, "📋 还没有 MAA 执行结果。"
    summary = str(state.get("summary", "")).strip()
    # Existing state may have been produced by the pre-translation runner.
    # Rebuild it from the local log without ever exposing raw CLI lines.
    if not summary.startswith("🌟 总体："):
        log_path = Path(str(state.get("log", "")))
        if log_path.is_file():
            from maa_daily import extract_summary

            raw = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            plan = state.get("plan", {})
            summary = extract_summary(
                raw,
                [],
                plan=plan if isinstance(plan, dict) else {},
                ok=state.get("status") == "success",
            )
    if not summary:
        summary = "🌟 总体：暂无可解析的执行明细"
    finished = str(state.get("finished_at", "")).replace("T", " ")[:19]
    when = f"（{finished}）" if finished else ""
    return True, f"📋 最近一次结果{when}\n{summary}"


# ---------------------------------------------------------------------------
# Copilot (prts.plus 抄作业) — all under 20s, real work goes to the worker
# ---------------------------------------------------------------------------

def valid_copilot_stage(
    name: str, config: dict[str, Any]
) -> tuple[str, str] | None:
    """Exact lookup in the local stage index; never guess aliases."""
    index = maa_copilot.load_stage_index(config)
    return maa_copilot.resolve_copilot_stage(name, index)


def _new_search_token() -> str:
    return secrets.token_hex(6)


def _search_path(token: str) -> Path:
    return maa_copilot.SEARCHES_DIR / f"{token}.json"


def _load_search(token: str) -> dict[str, Any] | None:
    if not maa_copilot.SEARCH_TOKEN_RE.fullmatch(token or ""):
        return None
    state = load_json(_search_path(token), None)
    return state if isinstance(state, dict) else None


def _mark_search_dispatch_failed(token: str, message: str) -> None:
    with state_lock():
        state = _load_search(token)
        if state is None or state.get("status") != "pending":
            return
        state["status"] = "failed"
        state["error"] = message[:400]
        state["finished_at"] = now_beijing().isoformat()
        atomic_write_json(_search_path(token), state)


def _invalidate_search_card(token: str, config: dict[str, Any]) -> str:
    changed, message_id = maa_copilot.supersede_search(token)
    if not changed:
        return ""
    try:
        retire_discord_components(config, message_id)
    except Exception as exc:
        return f"；旧卡按钮收起失败但已不能执行：{type(exc).__name__}: {exc}"
    return ""


def _copilot_blackout_reason(config: dict[str, Any]) -> str | None:
    """Refuse to start a copilot run close to the 08:00/22:00 daily runs."""
    minutes = max(0, int(config.get("copilot_blackout_minutes", 25)))
    if minutes == 0:
        return None
    now = now_beijing()
    for hour in (8, 22):
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now > target:
            target = target + timedelta(days=1)
        delta = (target - now).total_seconds() / 60
        if delta <= minutes:
            return (
                f"距 {hour:02d}:00 日常场次不足 {minutes} 分钟，"
                "为避免互斥抢占，请在日常结束后再抄作业。"
            )
    return None


def start_copilot_search(
    stage: str,
    *,
    auto_confirm: bool = False,
    activity_title: str = "",
    activity_key: str = "",
) -> tuple[bool, str, str | None]:
    config = load_config()
    resolved = valid_copilot_stage(stage, config)
    if resolved is None:
        return (
            False,
            f"关卡 {stage[:40]} 不在本地关卡索引里；只接受当期活动关、主线或资源关。",
            None,
        )
    stage_id, code = resolved
    worker = SCRIPTS_DIR / "maa_copilot_worker.py"
    if not worker.is_file():
        return False, "maa_copilot_worker.py 尚未部署。", None
    token = _new_search_token()
    maa_copilot.ensure_private_dirs()
    state = {
        "token": token,
        "status": "pending",
        "stage": code,
        "stage_id": stage_id,
        "candidates": [],
        "message_id": None,
        "created_at": now_beijing().isoformat(),
        "expires_at": (now_beijing() + maa_copilot.SEARCH_TTL).isoformat(),
        "source": "activity-watch" if auto_confirm else "discord",
        "auto_confirm": auto_confirm,
        "activity_title": activity_title[:120],
        "activity_key": activity_key[:200],
    }
    atomic_write_json(_search_path(token), state)
    try:
        subprocess.Popen(
            [sys.executable, str(worker), "search", token],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:
        detail = f"搜索进程派发失败：{type(exc).__name__}: {exc}"
        _mark_search_dispatch_failed(token, detail)
        return False, detail, None
    return True, f"🔍 正在为 {code} 匹配作业，结果稍后发到频道。", token


def copilot_find(stage: str) -> tuple[bool, str]:
    ok, message, _token = start_copilot_search(stage)
    return ok, message


def copilot_again(token: str) -> tuple[bool, str]:
    worker = SCRIPTS_DIR / "maa_copilot_worker.py"
    if not worker.is_file():
        return False, "maa_copilot_worker.py 尚未部署。"
    config = load_config()
    state = _load_search(token)
    if state is None:
        return False, "找不到这次搜索（可能已过期）。"
    if state.get("auto_confirm") or state.get("status") != "done":
        return False, "这张作业卡当前不能重新匹配。"
    try:
        retired = retire_discord_components(config, state.get("message_id"))
    except Exception as exc:
        return False, f"旧作业卡收起失败，未重新匹配：{type(exc).__name__}: {exc}"
    if not retired:
        return False, "旧作业卡缺少消息编号，未重新匹配。"
    with state_lock():
        state = _load_search(token)
        if state is None:
            return False, "找不到这次搜索（可能已过期）。"
        if state.get("auto_confirm") or state.get("status") != "done":
            return False, "这张作业卡当前不能重新匹配。"
        state["status"] = "pending"
        state["candidates"] = []
        state["message_id"] = None
        state["created_at"] = now_beijing().isoformat()
        state["expires_at"] = (now_beijing() + maa_copilot.SEARCH_TTL).isoformat()
        atomic_write_json(_search_path(token), state)
        registry = load_json(BUTTONS_PATH, {})
        if not isinstance(registry, dict):
            raise RuntimeError("共享按钮登记表格式错误")
        maa_copilot.prune_search_buttons(token, registry)
        atomic_write_json(BUTTONS_PATH, registry)
    try:
        subprocess.Popen(
            [sys.executable, str(worker), "search", token],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:
        detail = f"搜索进程派发失败：{type(exc).__name__}: {exc}"
        _mark_search_dispatch_failed(token, detail)
        return False, detail
    return True, f"🔄 正在重新为 {state.get('stage', '')} 匹配作业。"


def copilot_cancel(token: str) -> tuple[bool, str]:
    config = load_config()
    message_id: Any = None
    with state_lock():
        state = _load_search(token)
        if state is None:
            return False, "找不到这次搜索。"
        if state.get("status") not in {
            "pending",
            "running",
            "failed",
            "done",
            "queued",
            "preparing",
        }:
            return False, "本次作业已经失效、开始或结束，不能再取消。"
        state["status"] = "canceled"
        state["canceled_at"] = now_beijing().isoformat()
        message_id = state.get("message_id")
        atomic_write_json(_search_path(token), state)
        registry = load_json(BUTTONS_PATH, {})
        if not isinstance(registry, dict):
            raise RuntimeError("共享按钮登记表格式错误")
        maa_copilot.prune_search_buttons(token, registry)
        atomic_write_json(BUTTONS_PATH, registry)
    try:
        retire_discord_components(config, message_id)
    except Exception as exc:
        return (
            True,
            "❌ 已取消；旧卡按钮收起失败但已经不能执行："
            f"{type(exc).__name__}: {exc}",
        )
    return True, "❌ 已取消，相关按钮已清理。"


def copilot_status(token: str) -> tuple[bool, str]:
    state = _load_search(token)
    if state is None:
        return False, "找不到这次搜索（可能已过期）。"
    status = str(state.get("status", ""))
    if status == "done":
        stage = str(state.get("stage", ""))
        count = len(state.get("candidates", []) or [])
        return True, f"✅ {stage} 的搜索已完成，共 {count} 个候选；结果卡片在频道里。"
    if status == "running":
        return True, "⏳ 还在匹配中，稍等片刻。"
    if status == "failed":
        return True, f"❌ 匹配失败：{state.get('error', '未知原因')}"
    if status in {"queued", "preparing"}:
        return True, "⏳ 已确认，正在做活动、理智和运行环境复核。"
    if status == "executing":
        return True, "▶️ 作业正在执行。"
    if status == "executed":
        return True, "✅ 作业已经执行完成。"
    if status == "execution_failed":
        return True, "❌ 作业已执行但未成功，不会自动重跑。"
    if status in {"canceled", "superseded"}:
        return True, "⛔ 这张作业卡已经失效。"
    return True, "⏳ 等待匹配……"


def copilot_pick(token: str, number: str, mode: str) -> tuple[bool, str]:
    config = load_config()
    if mode not in {"normal", "raid"}:
        return False, "模式只接受 normal 或 raid。"
    state = _load_search(token)
    if state is None:
        return False, "找不到这次搜索（可能已过期）。"
    if state.get("status") != "done":
        recovered = maa_copilot.recover_stale_prebattle_dispatch(token, state)
        if recovered is None:
            return False, "这张作业卡已取消、过期、执行中或已经执行，不能重复启动。"
        state = recovered
    candidates = state.get("candidates", []) or []
    if not isinstance(candidates, list):
        candidates = []
    try:
        index = int(number)
    except (TypeError, ValueError):
        return False, "候选项编号无效。"
    if not 1 <= index <= len(candidates):
        return False, f"候选项编号超出范围（1..{len(candidates)}）。"
    candidate = candidates[index - 1]
    if not isinstance(candidate, dict):
        return False, "候选项数据异常。"
    cand_id = candidate.get("id")
    try:
        cand_id = int(cand_id)
    except (TypeError, ValueError):
        return False, "候选项 id 无效。"
    if not 0 < cand_id < 1_000_000_000:
        return False, "候选项 id 超出安全范围。"

    if state.get("auto_confirm"):
        stage = str(state.get("stage") or "")
        try:
            from maa_plan_card import activity_target_is_current, query_activity_snapshot

            live = query_activity_snapshot(config, timeout=3)
        except Exception as exc:
            return False, f"活动开放状态核验失败，未启动：{type(exc).__name__}: {exc}"
        if not activity_target_is_current(
            live, stage, str(state.get("activity_key") or "")
        ):
            warning = _invalidate_search_card(token, config)
            return False, f"{stage} 已不属于当前自动活动目标，旧确认按钮已失效{warning}。"
        preflight = maa_copilot.sanity_preflight(
            maa_copilot.load_roster(), state.get("sanity_cost"), now_beijing()
        )
        if not preflight.get("known"):
            return False, f"{preflight.get('reason', '无法确认当前理智')}，未启动。"
        if preflight.get("known") and not preflight.get("enough"):
            return False, maa_copilot.sanity_status_text(preflight)

    reason = _copilot_blackout_reason(config)
    if reason:
        return False, reason
    if is_run_locked():
        return False, "已有 MAA 场次在跑，抄作业与日常互斥；请等本场结束后再点。"

    worker = SCRIPTS_DIR / "maa_copilot_worker.py"
    if not worker.is_file():
        return False, "maa_copilot_worker.py 尚未部署。"
    with state_lock():
        fresh = _load_search(token)
        if fresh is None or fresh.get("status") != "done":
            return False, "这张作业卡已取消、过期、执行中或已经执行，不能重复启动。"
        fresh_candidates = fresh.get("candidates", []) or []
        if not isinstance(fresh_candidates, list) or not 1 <= index <= len(fresh_candidates):
            return False, "作业候选已经变化，请使用最新卡片。"
        fresh_candidate = fresh_candidates[index - 1]
        if not isinstance(fresh_candidate, dict) or fresh_candidate.get("id") != candidate.get("id"):
            return False, "作业候选已经变化，请使用最新卡片。"
        fresh["status"] = "queued"
        fresh["requested_index"] = index
        fresh["requested_mode"] = mode
        fresh["execution_requested_at"] = now_beijing().isoformat()
        fresh["expires_at"] = (now_beijing() + maa_copilot.SEARCH_TTL).isoformat()
        atomic_write_json(_search_path(token), fresh)
    try:
        subprocess.Popen(
            [sys.executable, str(worker), "run", token, str(index), mode],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        with state_lock():
            fresh = _load_search(token)
            if fresh is not None and fresh.get("status") == "queued":
                fresh["status"] = "done"
                fresh.pop("requested_index", None)
                fresh.pop("requested_mode", None)
                atomic_write_json(_search_path(token), fresh)
        raise
    mode_label = "突袭" if mode == "raid" else "普通"
    return True, f"▶️ 已派发 {state.get('stage', '')}（{mode_label}）的抄作业场次，后台开跑消息稍后出现。"


def dispatch(argv: list[str]) -> tuple[bool, str]:
    if argv[:2] == ["plan", "set-stage"] and len(argv) == 3:
        return set_stage(argv[2])
    if argv[:2] == ["plan", "skip-today"] and len(argv) in {2, 3}:
        return skip_today(argv[2] if len(argv) == 3 else "on")
    if argv == ["plan", "show"]:
        return show_plan()
    if argv == ["run", "now"]:
        return run_now()
    if argv == ["log", "tail"]:
        return log_tail()
    if argv[:2] == ["copilot", "find"] and len(argv) == 3:
        return copilot_find(argv[2])
    if argv[:2] == ["copilot", "again"] and len(argv) == 3:
        return copilot_again(argv[2])
    if argv[:2] == ["copilot", "cancel"] and len(argv) == 3:
        return copilot_cancel(argv[2])
    if argv[:2] == ["copilot", "status"] and len(argv) == 3:
        return copilot_status(argv[2])
    if argv[:2] == ["copilot", "pick"] and len(argv) == 5:
        return copilot_pick(argv[2], argv[3], argv[4])
    return False, (
        "不支持的动作；只允许 plan set-stage/skip-today/show、run now、log tail、"
        "copilot find/again/cancel/status/pick。"
    )


def main(argv: list[str]) -> int:
    try:
        ok, message = dispatch(argv[1:])
    except Exception as exc:
        emit(False, f"操作失败：{type(exc).__name__}: {exc}")
        return 1
    emit(ok, message)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
