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
    PLAN_PHASES,
    PLAN_SESSION_FIELDS,
    RUNS_DIR,
    SCRIPTS_DIR,
    atomic_write_json,
    discord_request,
    discord_token,
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
# 场次的中文说法。只有这两个场次能被覆盖；`now` 是手动场，没有覆盖可写（D4）。
PHASE_LABELS = {"am": "早场（08:00）", "pm": "晚场（22:00）"}


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


def valid_phase(phase: str) -> bool:
    """场次只接受 `am` / `pm`。值由登记表的 `select_args` / `args` 写死，不从 Discord 直通。"""
    return phase in PLAN_PHASES


def _today_override() -> dict[str, Any]:
    """当天的场次表，已按白名单过滤。

    结构是 `{date, sessions: {am: {...}, pm: {...}}, updated_at}`。日期不是今天、
    或磁盘上还是旧的按天格式（顶层 `stage` / `skip`），一律重置成空——
    旧格式一天后自然失效，不为它写迁移（四期 1a）。
    每个场次只保留 `PLAN_SESSION_FIELDS` 里的字段，其中 `stage_source` 是闸门：
    掉了它，用户点一次「跳过」就会让当天的自动定关被当成手动选择冻住。
    """
    now = now_beijing()
    current = load_json(PLAN_PATH, {})
    sessions: Any = {}
    if isinstance(current, dict) and current.get("date") == now.date().isoformat():
        sessions = current.get("sessions")
    kept: dict[str, dict[str, Any]] = {}
    if isinstance(sessions, dict):
        for phase in PLAN_PHASES:
            override = sessions.get(phase)
            if isinstance(override, dict):
                kept[phase] = {
                    key: override[key]
                    for key in PLAN_SESSION_FIELDS
                    if key in override
                }
    return {"date": now.date().isoformat(), "sessions": kept}


def _save_override(phase: str, key: str, value: Any, **extra: Any) -> dict[str, Any]:
    now = now_beijing()
    with state_lock():
        plan = _today_override()
        session = plan["sessions"].setdefault(phase, {})
        session[key] = value
        session.update(extra)
        session["source"] = "discord"
        session["updated_at"] = now.isoformat()
        plan["updated_at"] = now.isoformat()
        # There is deliberately no stone field in this write path.
        atomic_write_json(PLAN_PATH, plan)
    return effective_plan(phase=phase)


def _running_context() -> tuple[str, str]:
    state = load_json(LAST_RUN_PATH, {})
    if not isinstance(state, dict) or state.get("status") != "running":
        return "本场", "上次的本"
    phase = str(state.get("phase", "now"))
    phase_label = {"am": "早场", "pm": "晚场", "now": "手动场"}.get(phase, "本场")
    plan = state.get("plan", {})
    stage = plan_stage_label(plan if isinstance(plan, dict) else {})
    return phase_label, stage


def _confirmation(plan: dict[str, Any], was_running: bool, phase: str) -> str:
    """一句回执：改的是**哪一场**、另一场不受影响、正在跑的那场不被打断。"""
    now = now_beijing()
    stage = plan_stage_label(plan)
    medicine = medicine_label(str(plan.get("medicine", "none")))
    label = PHASE_LABELS.get(phase, "本场")
    other = "、".join(
        PHASE_LABELS[other_phase] for other_phase in PLAN_PHASES if other_phase != phase
    )
    if plan.get("skip"):
        # 这条回执对 set-stage 和 skip-today 通用，所以跳过时也报一下这场记的关：
        # 用户改完关才发现本场是跳过的，总比到点没跑再来问强。
        target = (
            f"今天（{now.month}/{now.day}）的{label}已设为跳过"
            f"（本场记的关是 {stage}，恢复后生效）；"
            f"{other}不受影响，说「现在跑」也仍然可以手动开一场。"
        )
    else:
        target = (
            f"今天（{now.month}/{now.day}）的{label}刷 {stage}，{medicine}；"
            f"{other}不受影响。"
        )
    if was_running:
        running_label, running_stage = _running_context()
        return f"{running_label}正在跑 {running_stage}，不打断它；{target}"
    return target


def _reject_phase(phase: str) -> tuple[bool, str]:
    return False, f"场次只接受 am（早场 08:00）或 pm（晚场 22:00），收到 {phase[:20]!r}。"


def set_stage(stage: str, phase: str) -> tuple[bool, str]:
    if not valid_phase(phase):
        return _reject_phase(phase)
    config = load_config()
    if not valid_stage(stage, config):
        return (
            False,
            "关卡不在白名单：只接受常规关卡格式（如 CE-6、1-7）、空串（上次的本）"
            "或当前活动关。碎石设置永远不受支持。",
        )
    was_running = is_run_locked()
    # 标成 discord：当天这一场的自动定关不再覆盖它（`maa_plan_card.apply_auto_stage()`）。
    plan = _save_override(phase, "stage", stage, stage_source="discord")
    return True, _confirmation(plan, was_running, phase)


def set_medicine(value: str) -> tuple[bool, str]:
    del value
    return False, "理智规则已固定：普通理智药 0，只自动使用临期药，碎石 0。"


def skip_today(value: str, phase: str) -> tuple[bool, str]:
    if value not in {"on", "off"}:
        return False, "skip-today 只接受 on 或 off。"
    if not valid_phase(phase):
        return _reject_phase(phase)
    was_running = is_run_locked()
    plan = _save_override(phase, "skip", value == "on")
    return True, _confirmation(plan, was_running, phase)


def show_plan() -> tuple[bool, str]:
    """两场各念一遍：刷什么、关是谁定的、跳不跳。"""
    origins = {"auto": "按材料缺口自动定的", "discord": "你手动选的"}
    lines: list[str] = []
    for phase in PLAN_PHASES:
        plan = effective_plan(phase=phase)
        origin = origins.get(str(plan.get("stage_source") or ""), "默认关")
        status = "跳过" if plan.get("skip") else "照常执行"
        lines.append(
            f"· {PHASE_LABELS[phase]}：{plan_stage_label(plan)}（{origin}）；{status}"
        )
    return (
        True,
        "今天的计划：\n"
        + "\n".join(lines)
        + f"\n理智药：{medicine_label('expiring')}；碎石：0。",
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
    # 跳过是按场次的（D2），所以这里要逐场看，不能再说「今天设了跳过」。
    skipped = [
        PHASE_LABELS[phase]
        for phase in PLAN_PHASES
        if effective_plan(phase=phase).get("skip")
    ]
    note = f"（今天的{'、'.join(skipped)}设了跳过，手动场照跑）" if skipped else ""
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
    import maa_clearing

    supported, detail = maa_clearing.nav_checker(maa_copilot._resource_dirs(config))(code)
    if not supported:
        return False, f"MAA 未提供该关导航，无法抄作业：{detail}", None
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


def _copilot_card_components(
    options: list[tuple[str, str, str]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """抄作业卡的一行下拉 + 它的登记项。值仍是不透明编号（不变量 #8）。"""
    if not options:
        # Discord 拒绝零选项的下拉；没有可抄的关就只发一览，不发控件。
        return [], {}
    select_args: dict[str, list[str]] = {}
    select_options: list[dict[str, Any]] = []
    for index, (label, stage, description) in enumerate(options):
        value = f"choice:{index}"
        select_options.append(
            {
                "label": label[:100],
                "value": value,
                "description": description[:100],
                "emoji": {"name": "🤖"},
            }
        )
        select_args[value] = ["copilot", "find", stage]
    custom_id = f"{maa_copilot.CARD_BUTTON_PREFIX}stage-select"
    rows = [
        {
            "type": 1,
            "components": [
                {
                    "type": 3,
                    "custom_id": custom_id,
                    "placeholder": "🤖 选一关，选中即开始搜作业",
                    "min_values": 1,
                    "max_values": 1,
                    "options": select_options,
                }
            ],
        }
    ]
    entries = {
        custom_id: {
            "kind": "exec",
            "script": "maa_action.py",
            "select_args": select_args,
        }
    }
    return rows, entries


def copilot_card() -> tuple[bool, str]:
    """发一张「现在能抄的关」卡（D6 的卡片部分）。

    只读本地数据、一次 Discord POST，因此可以在动作脚本里同步做完，
    不必派 worker（不变量 #6 的 20s 预算够）。
    """
    from maa_plan_card import copilot_card_view

    config = load_config()
    channel_id = str(config.get("channel_id", "")).strip()
    if not channel_id.isdigit():
        return False, "config.json 中尚未配置 Discord channel_id。"
    embed, options = copilot_card_view(config)
    rows, entries = _copilot_card_components(options)

    maa_copilot.ensure_private_dirs()
    previous = load_json(maa_copilot.CARD_PATH, {})
    warning = ""
    # 先收旧卡：组件 PATCH 空 → 删旧登记 → 再写新登记。反过来做会出现
    # 「旧卡还挂着按钮、登记已经指向新卡」的一段窗口。
    try:
        retire_discord_components(
            config, previous.get("message_id") if isinstance(previous, dict) else None
        )
    except Exception as exc:
        warning = f"；上一张卡的按钮收起失败：{type(exc).__name__}: {exc}"
    with state_lock():
        registry = load_json(BUTTONS_PATH, {})
        if not isinstance(registry, dict):
            raise RuntimeError("共享按钮登记表格式错误")
        for custom_id in [
            key
            for key in registry
            if key.startswith(maa_copilot.CARD_BUTTON_PREFIX)
        ]:
            registry.pop(custom_id)
        registry.update(entries)
        atomic_write_json(BUTTONS_PATH, registry)

    message = discord_request(
        discord_token(),
        "POST",
        f"/channels/{channel_id}/messages",
        {
            "embeds": [embed],
            "components": rows,
            "allowed_mentions": {"parse": []},
        },
    )
    atomic_write_json(
        maa_copilot.CARD_PATH,
        {
            "message_id": str(message.get("id", "")) if isinstance(message, dict) else "",
            "posted_at": now_beijing().isoformat(),
        },
    )
    if not options:
        return True, f"🤖 抄作业卡已发；当前没有上游写了导航定义的关可抄{warning}。"
    return True, f"🤖 抄作业卡已发，共 {len(options)} 关可选{warning}。"


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


# ---------------------------------------------------------------------------
# 通关队列（PLAN_CLEARING 步骤 3）
# ---------------------------------------------------------------------------

def _clearing_guard(config: dict[str, Any]) -> str | None:
    if not bool(config.get("clearing_enabled", False)):
        return (
            "通关队列当前是关闭的（`config.clearing_enabled = false`）。"
            "队列只跑上游写了导航定义的关（不变量 #37），没写的关会直接要求你手动打一次；"
            "打开之前请先确认这个边界可以接受。"
        )
    return None


def clearing_status() -> tuple[bool, str]:
    import maa_clearing

    config = load_config()
    guard = _clearing_guard(config)
    prefix = f"{guard}\n\n" if guard else ""
    return True, prefix + maa_clearing.status_text(maa_clearing.load_state())


def clearing_start() -> tuple[bool, str]:
    """按当前活动建队列。关卡表取 `config.activity_stages` 这份运行时缓存，
    不现场跑 `maa activity`——按钮脚本必须 <20s（不变量 #6）。"""
    import maa_clearing

    config = load_config()
    guard = _clearing_guard(config)
    if guard:
        return False, guard
    existing = maa_clearing.load_state()
    if existing is not None and not maa_clearing.is_finished(existing):
        return False, "已有一个未完成的队列；先 `clearing stop` 再重建。"
    stages = config.get("activity_stages", [])
    prefix, queue = maa_clearing.queue_from_activity_stages(
        stages if isinstance(stages, list) else []
    )
    if not queue:
        return False, "当前没有可用的活动关（`config.activity_stages` 为空或不是数字关）。"
    state = maa_clearing.new_state(
        activity_key=prefix,
        activity_title=str(config.get("activity_title") or prefix),
        prefix=prefix,
        stages=queue,
    )
    maa_clearing.save_state(state)
    return True, f"🗂️ 通关队列已建立：{queue[0]} → {queue[-1]}（共 {len(queue)} 关）。"


def clearing_stop() -> tuple[bool, str]:
    import maa_clearing

    state = maa_clearing.load_state()
    if state is None:
        return False, "当前没有通关队列。"
    stage = maa_clearing.awaiting_manual(state)
    with state_lock():
        registry = load_json(BUTTONS_PATH, {})
        if not isinstance(registry, dict):
            raise RuntimeError("共享按钮登记表格式错误")
        for custom_id in [
            key for key in registry if key.startswith(maa_clearing.BUTTON_PREFIX)
        ]:
            registry.pop(custom_id)
        atomic_write_json(BUTTONS_PATH, registry)
    try:
        maa_clearing.CLEARING_PATH.unlink()
    except FileNotFoundError:
        pass
    suffix = f"（原本在等 {stage} 的手动完成）" if stage else ""
    return True, f"🗑️ 通关队列已删除{suffix}；日常恢复照常刷理智。"


def clearing_manual_done(stage: str) -> tuple[bool, str]:
    """`needs_manual` 出口：用户手打完点这个按钮，cursor 才前进。"""
    import maa_clearing

    stage = str(stage or "").strip()[:40]
    with state_lock():
        state = maa_clearing.load_state()
        if state is None:
            return False, "当前没有通关队列。"
        awaiting = maa_clearing.awaiting_manual(state)
        if not awaiting:
            return False, "队列当前没有在等待手动完成。"
        if awaiting != stage:
            return False, f"队列在等的是 {awaiting}，不是 {stage}。"
        maa_clearing.advance_cursor(state)
        maa_clearing.save_state(state)
        registry = load_json(BUTTONS_PATH, {})
        if isinstance(registry, dict):
            registry.pop(maa_clearing.manual_button_id(stage), None)
            atomic_write_json(BUTTONS_PATH, registry)
    nxt = maa_clearing.current_stage(state)
    return True, (
        f"✅ 已记下 {stage} 手动完成；队列继续到 {nxt or '队尾（全部完成）'}。"
    )


def clearing_run() -> tuple[bool, str]:
    """派发一轮队列执行给 worker 后立即返回（不变量 #6：按钮脚本 <20s）。"""
    import maa_clearing

    config = load_config()
    guard = _clearing_guard(config)
    if guard:
        return False, guard
    state = maa_clearing.load_state()
    if state is None:
        return False, "当前没有通关队列；先 `clearing start`。"
    if not maa_clearing.queue_is_active(state):
        return False, "队列当前不活跃（已到队尾 / nav 冻结中 / 在等手动完成）。"
    blackout = _copilot_blackout_reason(config)
    if blackout:
        return False, blackout
    worker = SCRIPTS_DIR / "maa_copilot_worker.py"
    if not worker.is_file():
        return False, "maa_copilot_worker.py 尚未部署。"
    subprocess.Popen(
        [sys.executable, str(worker), "clearing"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return True, "▶️ 已派发一轮通关队列，进展稍后发到频道。"


def dispatch(argv: list[str]) -> tuple[bool, str]:
    if argv[:2] == ["plan", "set-stage"] and len(argv) == 4:
        return set_stage(argv[2], argv[3])
    if argv[:2] == ["plan", "skip-today"] and len(argv) == 4:
        return skip_today(argv[2], argv[3])
    if argv == ["plan", "show"]:
        return show_plan()
    if argv == ["run", "now"]:
        return run_now()
    if argv == ["log", "tail"]:
        return log_tail()
    if argv == ["copilot", "card"]:
        return copilot_card()
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
    if argv == ["clearing", "status"]:
        return clearing_status()
    if argv == ["clearing", "start"]:
        return clearing_start()
    if argv == ["clearing", "stop"]:
        return clearing_stop()
    if argv == ["clearing", "run"]:
        return clearing_run()
    if argv[:2] == ["clearing", "manual-done"] and len(argv) == 3:
        return clearing_manual_done(argv[2])
    return False, (
        "不支持的动作；只允许 plan set-stage <关卡> <am|pm>、"
        "plan skip-today <on|off> <am|pm>、plan show、run now、log tail、"
        "copilot card/find/again/cancel/status/pick、"
        "clearing status/start/stop/run/manual-done。"
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
