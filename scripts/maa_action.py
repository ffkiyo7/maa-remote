#!/usr/bin/env python3
"""The only permitted mutation and dispatch entry point for the MAA plan."""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import maa_copilot
import maa_roguelike
from maa_common import (
    BUTTONS_PATH,
    DEFAULT_MANUAL_RUN_BLACKOUT_MINUTES,
    LAST_RUN_PATH,
    MANUAL_RUN_BUTTON_PREFIX,
    MANUAL_RUN_PATH,
    MANUAL_RUN_TTL,
    PLAN_PATH,
    PLAN_PHASES,
    PLAN_SESSION_FIELDS,
    SCRIPTS_DIR,
    WATCH_PATH,
    atomic_write_json,
    discord_request,
    discord_token,
    effective_plan,
    emulator_is_online,
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
# 场次的中文说法。只有这两个场次能被覆盖；`now` 是手动场，没有覆盖可写——
# 它的关来自选关卡（`manual_run.json`，PLAN_MANUAL_RUN.md M4），不进 `plan.json`。
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
    # 同时**清掉 drops 上限和后备关**：那两个是给缺口第一名算的，换了关就不成立了；
    # 手动选的关一律打到理智不足为止（四期步骤 3b）。
    plan = _save_override(
        phase,
        "stage",
        stage,
        stage_source="discord",
        farming_drops={},
        farming_fallback_stage="",
    )
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


# ---------------------------------------------------------------------------
# 手动场（「现在跑」）：先发选关卡，选了才派发，10 分钟不选作废（PLAN_MANUAL_RUN.md）
# ---------------------------------------------------------------------------

MANUAL_RUN_CARD_TITLE = "🎯 手动场选关"
MANUAL_RUN_PENDING_TEXT = "计算中…"


def manual_run_blackout_reason(
    config: dict[str, Any], at: datetime | None = None
) -> str | None:
    """定时场优先（M10）：距 08:00 / 22:00 不足 N 分钟，手动场不发卡也不派发。

    与 `_copilot_blackout_reason()` 同构但另开配置键：手动日常一场约 14 分钟，
    加上卡片 10 分钟有效期，默认 30 比抄作业的 25 宽。0 = 关闭。
    """
    minutes = max(
        0,
        int(config.get("manual_run_blackout_minutes", DEFAULT_MANUAL_RUN_BLACKOUT_MINUTES)),
    )
    if minutes == 0:
        return None
    now = at or now_beijing()
    for hour in (8, 22):
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now > target:
            target = target + timedelta(days=1)
        if (target - now).total_seconds() / 60 <= minutes:
            return (
                f"距 {hour:02d}:00 日常场次不足 {minutes} 分钟，定时场优先，"
                "请在日常结束后再手动跑。"
            )
    return None


def _load_manual_run() -> dict[str, Any]:
    state = load_json(MANUAL_RUN_PATH, {})
    return state if isinstance(state, dict) else {}


def _manual_run_custom_id(token: str) -> str:
    return f"{MANUAL_RUN_BUTTON_PREFIX}{token}:stage-select"


def _manual_run_components(
    choices: list[list[str]] | list[tuple[str, str, str]],
    token: str,
    recommended: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """选关卡的一行下拉 + 它的登记项。

    选项与早晚场卡同源（`maa_plan_card._stage_choices()`，M2），但**没有 `default`**：
    这张卡的意义就是强制用户选（M1）。值仍是不透明 `choice:N`，argv 写死在登记表里
    （不变量 #8）。`recommended` 只影响 emoji（🎯 标缺口推荐，M8），不预选。
    """
    select_args: dict[str, list[str]] = {}
    options: list[dict[str, Any]] = []
    for index, (label, stage, description) in enumerate(choices):
        value = f"choice:{index}"
        options.append(
            {
                "label": str(label)[:100],
                "value": value,
                "description": str(description)[:100],
                "emoji": {"name": "🎯" if recommended is not None and stage == recommended else "▫️"},
            }
        )
        select_args[value] = ["run", "start", str(stage), token]
    custom_id = _manual_run_custom_id(token)
    rows = [
        {
            "type": 1,
            "components": [
                {
                    "type": 3,
                    "custom_id": custom_id,
                    "placeholder": "🎯 选这次手动场要刷的关（选中即开跑）",
                    "min_values": 1,
                    "max_values": 1,
                    "options": options,
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


def _manual_run_embed(
    state: dict[str, Any], *, footer: str = "", color: int = 0x5865F2
) -> dict[str, Any]:
    try:
        deadline = f"{datetime.fromisoformat(str(state.get('expires_at'))):%H:%M}"
    except (TypeError, ValueError):
        deadline = "10 分钟"
    recommendation = str(state.get("recommendation") or MANUAL_RUN_PENDING_TEXT)
    return {
        "title": MANUAL_RUN_CARD_TITLE,
        "description": (
            "从下拉选一关＝**立刻**开一场手动日常（基建 / 公招 / 商店 / 奖励照跑）。\n"
            f"不选的话 **{deadline}** 后这张卡作废，要跑得重新说「现在跑」。\n"
            "手动场不看早晚场卡上的选择；设了跳过也照跑；手动选的关不带掉落上限，打到理智不足为止。"
        ),
        "color": color,
        "fields": [{"name": "缺口推荐", "value": recommendation[:1024], "inline": False}],
        "footer": {"text": footer or f"{deadline} 前有效 · 定时场前 30 分钟内不接受"},
        "timestamp": str(state.get("created_at") or now_beijing().isoformat()),
    }


def _manual_run_message_patch(
    config: dict[str, Any], state: dict[str, Any], payload: dict[str, Any]
) -> bool:
    channel_id = str(config.get("channel_id") or "")
    message_id = str(state.get("message_id") or "")
    if not channel_id.isdigit() or not message_id.isdigit():
        return False
    discord_request(
        discord_token(),
        "PATCH",
        f"/channels/{channel_id}/messages/{message_id}",
        payload,
    )
    return True


def _retire_manual_run(
    config: dict[str, Any], status: str, footer: str
) -> dict[str, Any] | None:
    """把当前 `pending` 请求收成 `status`：状态 → 删登记 → 收卡片组件。

    返回收掉的请求（没有 pending 请求返回 None）。Discord PATCH 失败会抛，
    调用方决定是进回执尾巴还是吞掉——本地登记在抛之前已经删干净。
    """
    with state_lock():
        state = _load_manual_run()
        if state.get("status") != "pending":
            return None
        state["status"] = status
        state["updated_at"] = now_beijing().isoformat()
        atomic_write_json(MANUAL_RUN_PATH, state)
        registry = load_json(BUTTONS_PATH, {})
        if isinstance(registry, dict):
            for custom_id in [k for k in registry if k.startswith(MANUAL_RUN_BUTTON_PREFIX)]:
                registry.pop(custom_id)
            atomic_write_json(BUTTONS_PATH, registry)
    _manual_run_message_patch(
        config,
        state,
        {"embeds": [_manual_run_embed(state, footer=footer, color=0x99AAB5)], "components": []},
    )
    return state


def _spawn_action(args: list[str]) -> None:
    """派一个脱离会话的 `maa_action.py` 子进程（不受 Hermes exec 的 20s 限制）。"""
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def _skipped_sessions_note() -> str:
    # 跳过是按场次的（D2），所以这里要逐场看，不能再说「今天设了跳过」。
    skipped = [
        PHASE_LABELS[phase]
        for phase in PLAN_PHASES
        if effective_plan(phase=phase).get("skip")
    ]
    return f"（今天的{'、'.join(skipped)}设了跳过，手动场照跑）" if skipped else ""


def run_now() -> tuple[bool, str]:
    """「现在跑」→ 发一张选关卡（M-b）。不开跑；开跑在 `run_start()`。"""
    from maa_plan_card import _stage_choices

    if is_run_locked():
        phase, stage = _running_context()
        return False, f"{phase}已经在跑 {stage}，没有重复启动。"
    config = load_config()
    reason = manual_run_blackout_reason(config)
    if reason:
        return False, f"⛔ {reason}"
    script = SCRIPTS_DIR / "maa_daily.py"
    if not script.is_file():
        return False, "maa_daily.py 尚未部署。"
    try:
        require_executable(config, "maa_bin", "/opt/homebrew/bin/maa", "maa-cli")
        require_executable(config, "adb_path", "/opt/homebrew/bin/adb", "ADB")
    except RuntimeError as exc:
        return False, f"暂时不能运行：{exc}"
    channel_id = str(config.get("channel_id", "")).strip()
    if not channel_id.isdigit():
        return False, "config.json 中尚未配置 Discord channel_id。"

    warning = ""
    # 先收旧卡（状态 → 登记 → 组件），再写新登记、再发新卡（不变量 #14b）。
    try:
        _retire_manual_run(config, "superseded", "已被新的选关卡取代")
    except Exception as exc:
        warning = f"；上一张选关卡收起失败：{type(exc).__name__}: {exc}"

    now = now_beijing()
    token = _new_search_token()
    choices = [list(choice) for choice in _stage_choices(effective_plan(phase="now"), config)]
    rows, entries = _manual_run_components(choices, token)
    state = {
        "token": token,
        "status": "pending",
        "stage": "",
        "choices": choices,
        "recommendation": "",
        "recommended_stage": "",
        "message_id": "",
        "created_at": now.isoformat(),
        "expires_at": (now + MANUAL_RUN_TTL).isoformat(),
        "updated_at": now.isoformat(),
    }
    with state_lock():
        registry = load_json(BUTTONS_PATH, {})
        if not isinstance(registry, dict):
            raise RuntimeError("共享按钮登记表格式错误")
        for custom_id in [k for k in registry if k.startswith(MANUAL_RUN_BUTTON_PREFIX)]:
            registry.pop(custom_id)
        registry.update(entries)
        atomic_write_json(BUTTONS_PATH, registry)
    message = discord_request(
        discord_token(),
        "POST",
        f"/channels/{channel_id}/messages",
        {
            "embeds": [_manual_run_embed(state)],
            "components": rows,
            "allowed_mentions": {"parse": []},
        },
    )
    state["message_id"] = str(message.get("id", "")) if isinstance(message, dict) else ""
    with state_lock():
        atomic_write_json(MANUAL_RUN_PATH, state)
    # 过期哨兵（M-d，尽力而为的 UI 清理）与缺口推荐补写（M-g，可能要查森空岛）
    # 都是脱离会话的子进程，不占这个 20s 进程。
    _spawn_action(["run", "expire", token, "--after", str(int(MANUAL_RUN_TTL.total_seconds()))])
    _spawn_action(["run", "recommend", token])
    deadline = f"{now + MANUAL_RUN_TTL:%H:%M}"
    return (
        True,
        f"🎯 已发手动场选关卡{_skipped_sessions_note()}，从下拉选一关即开跑；"
        f"{deadline} 前不选就作废{warning}。",
    )


def run_start(stage: str, token: str) -> tuple[bool, str]:
    """卡上选了关 → 校验 → 派发 `maa_daily.py now <token>`（M-c）。只由登记表触发。"""
    state = _load_manual_run()
    if (
        not maa_copilot.SEARCH_TOKEN_RE.fullmatch(token or "")
        or state.get("token") != token
        or state.get("status") != "pending"
    ):
        return False, "这张选关卡已失效，请再说一次「现在跑」。"
    config = load_config()
    now = now_beijing()
    try:
        expires_at = datetime.fromisoformat(str(state.get("expires_at")))
    except (TypeError, ValueError):
        expires_at = now - timedelta(seconds=1)
    if now > expires_at:
        # 硬闸门：哨兵晚到、adapter 重放、旧登记残留都拦在这里。
        warning = ""
        try:
            _retire_manual_run(config, "expired", "⌛ 已过期，重新说「现在跑」")
        except Exception as exc:
            warning = f"（卡片收起失败：{type(exc).__name__}: {exc}）"
        return False, f"⌛ 这张选关卡已过期（{expires_at:%H:%M}），请再说一次「现在跑」。{warning}"
    if not valid_stage(stage, config):
        return False, "关卡不在白名单：只接受常规关卡格式（如 CE-6、1-7）、空串（上次的本）或当前活动关。"
    if is_run_locked():
        phase, running = _running_context()
        return False, f"{phase}已经在跑 {running}，等它结束后再选（这张卡 {expires_at:%H:%M} 前有效）。"
    reason = manual_run_blackout_reason(config, now)
    if reason:
        return False, f"⛔ {reason}"
    script = SCRIPTS_DIR / "maa_daily.py"
    if not script.is_file():
        return False, "maa_daily.py 尚未部署。"
    try:
        require_executable(config, "maa_bin", "/opt/homebrew/bin/maa", "maa-cli")
        require_executable(config, "adb_path", "/opt/homebrew/bin/adb", "ADB")
    except RuntimeError as exc:
        return False, f"暂时不能运行：{exc}"

    with state_lock():
        state = _load_manual_run()
        if state.get("token") != token or state.get("status") != "pending":
            return False, "这张选关卡已失效，请再说一次「现在跑」。"
        state["status"] = "dispatched"
        state["stage"] = stage
        state["dispatched_at"] = now.isoformat()
        state["updated_at"] = now.isoformat()
        atomic_write_json(MANUAL_RUN_PATH, state)
        registry = load_json(BUTTONS_PATH, {})
        if isinstance(registry, dict):
            for custom_id in [k for k in registry if k.startswith(MANUAL_RUN_BUTTON_PREFIX)]:
                registry.pop(custom_id)
            atomic_write_json(BUTTONS_PATH, registry)
    warning = ""
    try:
        _manual_run_message_patch(
            config,
            state,
            {
                "embeds": [
                    _manual_run_embed(
                        state, footer=f"▶️ 已选 {plan_stage_label(state)}，手动场已派发", color=0x57F287
                    )
                ],
                "components": [],
            },
        )
    except Exception as exc:
        warning = f"；卡片收起失败：{type(exc).__name__}: {exc}"
    subprocess.Popen(
        [sys.executable, str(script), "now", token],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return (
        True,
        f"▶️ 已派发手动场，刷 {plan_stage_label(state)}（不带掉落上限，打到理智不足为止）"
        f"{_skipped_sessions_note()}；开跑消息稍后出现在频道{warning}。",
    )


def run_expire(token: str, after: int = 0) -> tuple[bool, str]:
    """到期收卡（M-d）。幂等：不是这个 token 的 pending 请求、或还没到期，就什么都不做。"""
    if after > 0:
        time.sleep(after)
    state = _load_manual_run()
    if state.get("token") != token or state.get("status") != "pending":
        return True, "没有待过期的选关卡。"
    try:
        expires_at = datetime.fromisoformat(str(state.get("expires_at")))
    except (TypeError, ValueError):
        expires_at = now_beijing()
    if now_beijing() < expires_at:
        return True, f"选关卡 {expires_at:%H:%M} 才到期。"
    config = load_config()
    retired = _retire_manual_run(config, "expired", "⌛ 已过期，重新说「现在跑」")
    return True, "⌛ 选关卡已过期并收起。" if retired else "没有待过期的选关卡。"


def run_recommend(token: str) -> tuple[bool, str]:
    """缺口推荐补写（M-g）：算完 PATCH 卡片，只在请求仍 pending 时写回。

    只由 `run_now()` 派发的脱离会话子进程调用；计算链与 07:50 建卡相同（D5 口径）。
    """
    from maa_plan_card import auto_stage, current_farming_result, farming_note

    config = load_config()
    recommended = ""
    try:
        result = current_farming_result(effective_plan(phase="now"), config)
        recommended, why = auto_stage(result)
        note = farming_note(result)
        if recommended:
            text = f"**{recommended}**" + (f" · {why}" if why else "")
        else:
            text = f"推荐暂不可用（{why or '缺口算不出来'}）"
        if note:
            text = f"{text}\n{note}"
    except Exception as exc:
        text = f"推荐暂不可用（{type(exc).__name__}: {exc}）"
    with state_lock():
        state = _load_manual_run()
        if state.get("token") != token or state.get("status") != "pending":
            return True, "选关卡已不在等待中，推荐不再写回。"
        state["recommendation"] = text[:1000]
        state["recommended_stage"] = recommended
        state["updated_at"] = now_beijing().isoformat()
        atomic_write_json(MANUAL_RUN_PATH, state)
    choices = state.get("choices") if isinstance(state.get("choices"), list) else []
    rows, _entries = _manual_run_components(choices, token, recommended or None)
    _manual_run_message_patch(
        config, state, {"embeds": [_manual_run_embed(state)], "components": rows}
    )
    return True, f"缺口推荐已写回：{recommended or '暂不可用'}。"


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


# Depot 的独立 cron 在北京时间 04:30 触发，到期时会持 run.lock 数分钟
# （不变量 #23）。自动作业避开这个窗口，免得两边互相抢锁、谁也跑不成。
DEPOT_WINDOW_START_MINUTES = 4 * 60 + 20
DEPOT_WINDOW_END_MINUTES = 5 * 60


DEFAULT_MIN_LEAD_MINUTES = 40


def min_lead_reason(config: dict[str, Any], at: datetime | None = None) -> str | None:
    """距下一场日常还够不够**开新的一关**（四期 2c 的时间预算）。

    与 `copilot_blackout_minutes` 是两条不同的闸门：blackout 管「能不能开这一轮」，
    这条管「一轮里还能不能再开一关」——一关十来分钟，撞上 08:00/22:00 会让日常
    抢不到锁。默认 40 分钟，比 blackout（25）宽，因为它要覆盖整关而不只是启动。
    watcher 派发前查一次（省得派出去立刻收尾），worker 每关开始前再查一次。
    """
    minutes = max(0, int(config.get("auto_copilot_min_lead_minutes", DEFAULT_MIN_LEAD_MINUTES)))
    if minutes == 0:
        return None
    now = at or now_beijing()
    for hour in (8, 22):
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now > target:
            target = target + timedelta(days=1)
        if (target - now).total_seconds() / 60 <= minutes:
            return (
                f"距 {hour:02d}:00 日常场次不足 {minutes} 分钟，本轮到此收尾，"
                "剩下的关留给下一轮。"
            )
    return None


def depot_window_reason(at: datetime | None = None) -> str | None:
    moment = at or now_beijing()
    minutes = moment.hour * 60 + moment.minute
    if DEPOT_WINDOW_START_MINUTES <= minutes < DEPOT_WINDOW_END_MINUTES:
        return "处于 Depot 对表窗口（北京时间 04:20–05:00），不与它抢 run.lock。"
    return None


def dispatch_blocker(
    config: dict[str, Any], scheduled_at: str, at: datetime | None = None
) -> str:
    """还不能派发的原因；可以派发返回空串。**每一条都要能说出口**（护栏 4）。

    自动与手动共用这**一份**实现（不变量 #13）：watcher 每 tick 派发前查一次，
    手动 `start_copilot_search()` 写 `pending` 之前查一次、`cmd_search()` 搜完
    派发之前再查一次（搜索要跑 5~20s，可能刚好跨进 blackout）。
    """
    moment = at or now_beijing()
    if scheduled_at:
        try:
            ready = datetime.fromisoformat(scheduled_at)
        except ValueError:
            ready = moment
        if moment < ready:
            return f"等理智恢复到 {ready:%m-%d %H:%M}"
    for reason in (
        _copilot_blackout_reason(config),
        depot_window_reason(moment),
        min_lead_reason(config, moment),
    ):
        if reason:
            return reason
    if is_run_locked():
        return "另一场 MAA 正在运行（run.lock 被占）"
    online, detail = emulator_is_online(config)
    if online:
        return f"模拟器正在使用：{detail}"
    return ""


def publish_round(
    token: str,
    entries: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    title: str,
    scheduled_at: str = "",
    note: str = "",
) -> str:
    """发一张「已排程」卡并登记它**唯一**的「取消本轮」按钮，返回 message_id。

    顺序与抄作业卡一致（不变量 #14b）：先删这一 token 的旧登记，再 merge 写新登记。
    """
    embed, components, registry = maa_copilot.build_scheduled_card(
        token, entries, title=title, scheduled_at=scheduled_at, note=note
    )
    message = discord_request(
        discord_token(),
        "POST",
        f"/channels/{config.get('channel_id', '')}/messages",
        {"embeds": [embed], "components": components,
         "allowed_mentions": {"parse": []}},
    )
    message_id = str(message.get("id", "")) if isinstance(message, dict) else ""
    if not message_id.isdigit():
        raise RuntimeError("Discord 发卡响应缺少消息编号")
    with state_lock():
        buttons = load_json(BUTTONS_PATH, {})
        if not isinstance(buttons, dict):
            raise RuntimeError("共享按钮登记表格式错误")
        maa_copilot.prune_search_buttons(token, buttons)
        buttons.update(registry)
        atomic_write_json(BUTTONS_PATH, buttons)
    return message_id


def dispatch_batch(token: str) -> None:
    """把一轮交给 worker 的 `run-batch`。调用方先把状态原子改成 `queued`。

    手动（`copilot find` → 搜索 worker）与自动（watcher）**共用这一条派发口**，
    执行链因此只有一条（D14）。
    """
    worker = SCRIPTS_DIR / "maa_copilot_worker.py"
    if not worker.is_file():
        raise RuntimeError("maa_copilot_worker.py 尚未部署")
    subprocess.Popen(
        [sys.executable, str(worker), "run-batch", token],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def new_round_token() -> str:
    return _new_search_token()


def start_copilot_search(stage: str) -> tuple[bool, str, str | None]:
    """手动抄一关：写一份 `pending` 搜索并派后台 worker（<20s，不变量 #6）。

    模式按 D7 定：当期活动关普通 + 突袭，资源本 / 主线只有普通。
    **`config.copilot_raid_switch_task` 为空时突袭功能整体关闭**，活动关也只排普通。
    """
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
    # 不变量 #13：手动路径同样过闸门，且**在写 `pending` 之前**——被挡就什么都不写、
    # 不派 worker，也不排程（手动轮没人替你重试，排下去只会烂在那里）。
    blocker = dispatch_blocker(config, "", now_beijing())
    if blocker:
        return False, f"{blocker}；请日常 / 对表结束后再点。", None
    activity_key = str(load_json(WATCH_PATH, {}).get("activity_key") or "")
    prefix = maa_copilot._activity_prefix(activity_key)
    # 突袭开关为空时这里只会给出 `["normal"]`——手动路径同样不许排突袭。
    modes = list(maa_clearing.required_modes(code, prefix, config))
    token = _new_search_token()
    maa_copilot.ensure_private_dirs()
    state = {
        "token": token,
        "status": "pending",
        "stage": code,
        "stage_id": stage_id,
        "modes": modes,
        "entries": [],
        "message_id": None,
        "created_at": now_beijing().isoformat(),
        "expires_at": (now_beijing() + maa_copilot.SEARCH_TTL).isoformat(),
        "source": "discord",
        # 手动关也带 activity_key：执行前的活动复核靠它判「活动还在不在」。
        # 非活动关（资源本 / 主线）拿到的是空串，那一段复核整个跳过。
        "activity_key": activity_key if prefix and code.startswith(f"{prefix}-") else "",
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
    mode_text = "普通 + 突袭" if len(modes) > 1 else "普通"
    return (
        True,
        f"🔍 正在为 {code}（{mode_text}）匹配作业；搜到就直接开跑，结果发到频道。",
        token,
    )


def copilot_find(stage: str) -> tuple[bool, str]:
    ok, message, _token = start_copilot_search(stage)
    return ok, message


MARK_MODE_ARGS = {"normal": ("normal",), "raid": ("raid",), "all": ("normal", "raid")}


def _ledger_target(stage: str) -> tuple[str, str, str, dict[str, Any]] | str:
    """`(关卡代号, 活动 key, 活动前缀, config)`，或一句拒绝理由。

    活动 key 的取法与 `start_copilot_search()` 完全一致：当期活动关落活动账本，
    非活动关（主线 / 资源本）落 `ledger-standalone.json`。取错文件就等于写进
    一本没人读的账。
    """
    config = load_config()
    resolved = valid_copilot_stage(stage, config)
    if resolved is None:
        return f"关卡 {stage[:40]} 不在本地关卡索引里；只接受当期活动关、主线或资源关。"
    _stage_id, code = resolved
    activity_key = str(load_json(WATCH_PATH, {}).get("activity_key") or "")
    prefix = maa_copilot._activity_prefix(activity_key)
    if not (prefix and code.startswith(f"{prefix}-")):
        activity_key = ""
    return code, activity_key, prefix, config


def copilot_mark_cleared(stage: str, mode_arg: str = "") -> tuple[bool, str]:
    """「SR-7 我自己打过了」→ 写进账本，自动排轮从此跳过这一关。

    不传模式时按 D7 取这一关**要打的模式**（突袭功能关着就只有普通）；
    `normal` / `raid` / `all` 可以指定。只补空位，重复标记幂等。
    """
    target = _ledger_target(stage)
    if isinstance(target, str):
        return False, target
    code, activity_key, prefix, config = target
    import maa_clearing

    mode_key = str(mode_arg or "").strip().lower()
    if mode_key and mode_key not in MARK_MODE_ARGS:
        return False, "模式只接受 normal / raid / all，不传则按当前规则取这一关要打的模式。"
    modes = MARK_MODE_ARGS.get(mode_key) or maa_clearing.required_modes(code, prefix, config)
    with state_lock():
        ledger, _note = maa_copilot.ensure_ledger(activity_key)
        marked = maa_copilot.mark_stage_cleared(ledger, code, modes)
        if marked:
            maa_copilot.save_ledger(ledger, maa_copilot.ledger_path(activity_key))
    labels = "、".join(
        maa_clearing.entry_label(maa_clearing.make_entry(code, mode)) for mode in marked
    )
    if not marked:
        return True, f"ℹ️ {code} 账本里已经记着通关了，没有改动。"
    return True, (
        f"✅ 已把 {labels} 记为手动通关；自动抄作业不再排这一关，"
        f"抄作业卡上会显示 ✅ 已通。标错了可用 `copilot unmark {code}` 撤销。"
    )


def copilot_unmark(stage: str) -> tuple[bool, str]:
    """撤销某关的通关记账（普通 + 突袭），下一轮自动排轮会重新考虑它。"""
    target = _ledger_target(stage)
    if isinstance(target, str):
        return False, target
    code, activity_key, _prefix, _config = target
    with state_lock():
        ledger = maa_copilot.load_ledger(maa_copilot.ledger_path(activity_key))
        cleared = maa_copilot.unmark_stage_cleared(ledger, code) if ledger else []
        if cleared:
            maa_copilot.save_ledger(ledger, maa_copilot.ledger_path(activity_key))
    if not cleared:
        return True, f"ℹ️ {code} 账本里本来就没有通关记录，没有改动。"
    return True, f"↩️ 已撤销 {code} 的通关记账（{'、'.join(cleared)}）；下一轮自动排轮会重新考虑它。"


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


# ---------------------------------------------------------------------------
# Roguelike（肉鸽）— 手动触发，卡片选参数，worker 循环跑（四期步骤 4 / D19）
# ---------------------------------------------------------------------------

def dispatch_roguelike(token: str) -> None:
    """把一轮肉鸽交给 worker。调用方先把状态原子改成 `running`。"""
    worker = SCRIPTS_DIR / "maa_roguelike_worker.py"
    if not worker.is_file():
        raise RuntimeError("maa_roguelike_worker.py 尚未部署")
    subprocess.Popen(
        [sys.executable, str(worker), "run", token],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def _send_roguelike_card(config: dict[str, Any], state: dict[str, Any]) -> str:
    """发一张新的肉鸽卡并收掉上一张，返回给用户看的补充说明。

    顺序与抄作业卡一致（不变量 #14b）：**PATCH 旧卡组件 → 删旧登记 →
    merge 写新登记 → 发卡 → 记新 message_id**。反过来会出现「旧卡还能点、
    登记已经指向新卡」的窗口。
    """
    channel_id = str(config.get("channel_id", "")).strip()
    if not channel_id.isdigit():
        raise RuntimeError("config.json 中尚未配置 Discord channel_id。")
    embed, rows, entries = maa_roguelike.build_card(state)
    warning = ""
    try:
        retire_discord_components(config, state.get("message_id"))
    except Exception as exc:
        warning = f"；上一张卡的按钮收起失败：{type(exc).__name__}: {exc}"
    with state_lock():
        registry = load_json(BUTTONS_PATH, {})
        if not isinstance(registry, dict):
            raise RuntimeError("共享按钮登记表格式错误")
        for custom_id in [
            key for key in registry if key.startswith(maa_roguelike.BUTTON_PREFIX)
        ]:
            registry.pop(custom_id)
        registry.update(entries)
        atomic_write_json(BUTTONS_PATH, registry)
    message = discord_request(
        discord_token(),
        "POST",
        f"/channels/{channel_id}/messages",
        {"embeds": [embed], "components": rows, "allowed_mentions": {"parse": []}},
    )
    message_id = str(message.get("id", "")) if isinstance(message, dict) else ""
    pending = state.get("pending")
    # 只合并这两个字段：worker 可能正在同一份文件里写 `runs` / `dirty`。
    maa_roguelike.update_state(
        lambda fresh: fresh.update({"pending": pending, "message_id": message_id})
    )
    return warning


def roguelike_card() -> tuple[bool, str]:
    """发一张肉鸽配置卡（4 个下拉 + 1 行按钮，正好 5 行）。"""
    config = load_config()
    state = maa_roguelike.load_state()
    state["pending"] = maa_roguelike.normalize_pending(state.get("pending"))
    warning = _send_roguelike_card(config, state)
    choice = maa_roguelike.params_text(maa_roguelike.resolve_params(state["pending"]))
    return True, f"🎲 肉鸽卡已发：{choice}。选好后点「▶️ 开始」{warning}"


def roguelike_set(field: str, value: str) -> tuple[bool, str]:
    """写一项待开始的选择（值来自登记表 `select_args`，仍然逐项校验）。"""
    if field not in maa_roguelike.PARAM_FIELDS:
        return False, f"只支持 {'/'.join(maa_roguelike.PARAM_FIELDS)} 四项。"
    state = maa_roguelike.load_state()
    if state.get("status") in maa_roguelike.LIVE_STATUSES and not (
        maa_roguelike.round_is_stale(state, is_run_locked())
    ):
        return False, "肉鸽正在跑，参数要等这一轮停下来再改。"
    pending = maa_roguelike.normalize_pending(state.get("pending"))
    theme = pending["theme"]
    if field == "theme":
        if not maa_roguelike.valid_theme(value):
            return False, f"未知的肉鸽主题：{value[:40]!r}。"
        # 模式 / 难度 / 分队的**选项**都随主题变，所以整份选择重置成新主题的
        # 默认值，并**重发一张卡**——留着旧卡只会让人从上一主题的分队里挑。
        state["pending"] = maa_roguelike.default_pending(value)
        warning = _send_roguelike_card(load_config(), state)
        choice = maa_roguelike.params_text(
            maa_roguelike.resolve_params(state["pending"])
        )
        return True, (
            f"🎲 已换成{maa_roguelike.theme_name(value)}，"
            f"并按该主题重发了一张卡：{choice}。"
            f"开始前记得在游戏里把这个主题置顶到终端{warning}"
        )
    if field == "mode":
        if not maa_roguelike.valid_mode_token(theme, value):
            return False, f"{maa_roguelike.theme_name(theme)} 没有这个模式。"
        pending["mode"] = value
    elif field == "difficulty":
        try:
            number = int(value)
        except ValueError:
            return False, f"难度必须是整数，收到 {value[:20]!r}。"
        if not maa_roguelike.valid_difficulty(theme, number):
            return False, (
                f"{maa_roguelike.theme_name(theme)} 的难度上限是 "
                f"{maa_roguelike.max_difficulty(theme)}。"
            )
        pending["difficulty"] = number
    else:
        if value not in maa_roguelike.all_squad_values(theme):
            return False, f"{maa_roguelike.theme_name(theme)} 没有「{value[:20]}」。"
        pending["squad"] = value
    normalized = maa_roguelike.normalize_pending(pending)
    maa_roguelike.update_state(lambda fresh: fresh.update({"pending": normalized}))
    choice = maa_roguelike.params_text(maa_roguelike.resolve_params(normalized))
    return True, f"🎲 已记下：{choice}。点「▶️ 开始」才会真的开跑。"


def roguelike_start() -> tuple[bool, str]:
    """派发一轮肉鸽。闸门与抄作业**共用同一份**（不变量 #13）。"""
    state = maa_roguelike.load_state()
    stale = maa_roguelike.round_is_stale(state, is_run_locked())
    if state.get("status") in maa_roguelike.LIVE_STATUSES and not stale:
        return False, "肉鸽已经在跑了，没有重复启动；要停就点「⏹️」或「⏏️」。"
    pending = maa_roguelike.normalize_pending(state.get("pending"))
    conflict = maa_roguelike.dirty_theme_conflict(state, pending["theme"])
    if conflict:
        return False, conflict
    config = load_config()
    # 肉鸽不耗理智，但它一样要开模拟器、一样持 run.lock，所以照样过这道闸门。
    blocker = dispatch_blocker(config, "", now_beijing())
    if blocker:
        return False, f"{blocker}；肉鸽本轮未排程，日常 / 对表结束后再点「开始」。"
    params = maa_roguelike.resolve_params(pending)
    token = _new_search_token()
    started = {
        "status": maa_roguelike.RUNNING,
        "theme": params["theme"],
        "params": params,
        "pending": pending,
        "token": token,
        "started_at": now_beijing().isoformat(),
        "runs": [],
        "stop_requested_at": None,
        "abort_requested_at": None,
    }
    maa_roguelike.update_state(lambda fresh: fresh.update(started))
    try:
        dispatch_roguelike(token)
    except Exception as exc:
        maa_roguelike.update_state(
            lambda fresh: fresh.update(
                {"status": maa_roguelike.IDLE, "token": ""}
            )
        )
        return False, f"肉鸽 worker 派发失败：{type(exc).__name__}: {exc}"
    recovered = "（上一轮的 worker 已经不在了，这次按新的一轮重开）" if stale else ""
    return True, (
        f"▶️ 已派发肉鸽{recovered}：{maa_roguelike.params_text(params)}。"
        "每打完一局发一条进度；说「肉鸽停」会在本局结束后退出。"
    )


def _request_roguelike_stop(abort: bool) -> tuple[bool, str]:
    """只写标志位、立刻返回（<20s，不变量 #6）。真正的收手由 worker 做。"""
    state = maa_roguelike.load_state()
    if state.get("status") not in maa_roguelike.LIVE_STATUSES:
        return False, "肉鸽现在没有在跑，没有可以停的东西。"
    now = now_beijing().isoformat()

    def mark(fresh: dict[str, Any]) -> None:
        fresh["stop_requested_at"] = fresh.get("stop_requested_at") or now
        fresh["status"] = maa_roguelike.STOPPING
        if abort:
            fresh["abort_requested_at"] = now

    maa_roguelike.update_state(mark)
    if abort:
        return True, (
            "⏏️ 正在中止：当前这一局作废（分数与源石锭都不算），"
            "模拟器随后会关干净。下次开始必须还是同一个主题，上游才会自己收掉残局。"
        )
    return True, (
        "⏹️ 收到；这一局会打完再退出，进度不丢。"
        "要立刻停就点「⏏️ 立即中止（会丢当前局）」。"
    )


def roguelike_stop() -> tuple[bool, str]:
    return _request_roguelike_stop(abort=False)


def roguelike_abort() -> tuple[bool, str]:
    return _request_roguelike_stop(abort=True)


def roguelike_status() -> tuple[bool, str]:
    state = maa_roguelike.load_state()
    status = str(state.get("status") or maa_roguelike.IDLE)
    runs = state.get("runs") or []
    if status == maa_roguelike.IDLE:
        tail = ""
        if state.get("dirty"):
            tail = (
                f"；上次 {maa_roguelike.theme_name(str(state.get('last_theme') or ''))}"
                "没有干净结束，下次要用同主题收尾"
            )
        return True, f"🎲 肉鸽空闲中（说「刷肉鸽」发配置卡）{tail}。"
    params = state.get("params") if isinstance(state.get("params"), dict) else {}
    head = "▶️ 正在跑" if status == maa_roguelike.RUNNING else "⏹️ 已收到停止指令，本局结束后退出"
    return True, (
        f"{head}：{maa_roguelike.params_text(params)}\n"
        f"已完成 {len(runs)} 局：\n{maa_roguelike.run_lines(runs)}"
    )


def copilot_cancel(token: str) -> tuple[bool, str]:
    config = load_config()
    message_id: Any = None
    with state_lock():
        state = _load_search(token)
        if state is None:
            return False, "找不到这次搜索。"
        if state.get("status") not in maa_copilot.CANCELABLE_STATUSES:
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
    entries = state.get("entries") or []
    if status == "running":
        return True, "⏳ 还在匹配作业，稍等片刻。"
    if status == "failed":
        return True, f"❌ 未开始：{state.get('error', '未知原因')}"
    if status == "scheduled":
        when = str(state.get("scheduled_at") or "") or "条件满足后立刻开跑"
        return True, f"⏳ 已排程 {len(entries)} 项，预计 {when}。"
    if status == "queued":
        return True, "⏳ 已派发，正在抢锁与开机。"
    if status == "executing":
        return True, f"▶️ 正在执行本轮 {len(entries)} 项。"
    if status == "executed":
        return True, "✅ 本轮已经执行完成，战报在频道里。"
    if status == "execution_failed":
        return True, "❌ 本轮已执行但未全部通过，不会自动重跑；战报里有逐关结果。"
    if status in {"canceled", "superseded"}:
        return True, "⛔ 这一轮已经失效。"
    return True, "⏳ 等待匹配……"


def dispatch(argv: list[str]) -> tuple[bool, str]:
    if argv[:2] == ["plan", "set-stage"] and len(argv) == 4:
        return set_stage(argv[2], argv[3])
    if argv[:2] == ["plan", "skip-today"] and len(argv) == 4:
        return skip_today(argv[2], argv[3])
    if argv == ["plan", "show"]:
        return show_plan()
    if argv == ["run", "now"]:
        return run_now()
    if argv[:2] == ["run", "start"] and len(argv) == 4:
        return run_start(argv[2], argv[3])
    if argv[:2] == ["run", "expire"] and len(argv) == 3:
        return run_expire(argv[2])
    if (
        argv[:2] == ["run", "expire"]
        and len(argv) == 5
        and argv[3] == "--after"
        and argv[4].isdigit()
    ):
        return run_expire(argv[2], int(argv[4]))
    if argv[:2] == ["run", "recommend"] and len(argv) == 3:
        return run_recommend(argv[2])
    if argv == ["log", "tail"]:
        return log_tail()
    if argv == ["copilot", "card"]:
        return copilot_card()
    if argv[:2] == ["copilot", "find"] and len(argv) == 3:
        return copilot_find(argv[2])
    if argv[:2] == ["copilot", "cancel"] and len(argv) == 3:
        return copilot_cancel(argv[2])
    if argv[:2] == ["copilot", "status"] and len(argv) == 3:
        return copilot_status(argv[2])
    if argv[:2] == ["copilot", "mark-cleared"] and len(argv) in (3, 4):
        return copilot_mark_cleared(argv[2], argv[3] if len(argv) == 4 else "")
    if argv[:2] == ["copilot", "unmark"] and len(argv) == 3:
        return copilot_unmark(argv[2])
    if argv == ["roguelike", "card"]:
        return roguelike_card()
    if argv[:2] == ["roguelike", "set"] and len(argv) == 4:
        return roguelike_set(argv[2], argv[3])
    if argv == ["roguelike", "start"]:
        return roguelike_start()
    if argv == ["roguelike", "stop"]:
        return roguelike_stop()
    if argv == ["roguelike", "abort"]:
        return roguelike_abort()
    if argv == ["roguelike", "status"]:
        return roguelike_status()
    return False, (
        "不支持的动作；只允许 plan set-stage <关卡> <am|pm>、"
        "plan skip-today <on|off> <am|pm>、plan show、run now、run start <关卡> <token>、"
        "run expire <token> [--after 秒]、run recommend <token>、log tail、"
        "copilot card/find/cancel/status/mark-cleared/unmark、"
        "roguelike card/set/start/stop/abort/status。"
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
