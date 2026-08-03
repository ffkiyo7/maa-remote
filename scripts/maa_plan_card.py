#!/usr/bin/env python3
"""Publish the 07:50 MAA plan card and maintain persistent buttons."""

from __future__ import annotations

import subprocess
import sys
import re
from datetime import datetime, timedelta
from typing import Any

import maa_copilot
from maa_common import (
    BUTTONS_PATH,
    CONFIG_PATH,
    LAST_RUN_PATH,
    atomic_write_json,
    cleanup_old_threads,
    cleanup_run_files,
    discord_request,
    discord_token,
    effective_plan,
    load_config,
    load_json,
    maa_subprocess_env,
    medicine_label,
    merge_last_run,
    now_beijing,
    plan_stage_label,
    state_lock,
)


BUTTON_PREFIX = "hermes:btn:maa:"
ACTIVITY_LINE_RE = re.compile(
    r"^\s*-\s+([A-Za-z0-9][A-Za-z0-9_-]{0,30}-\d{1,2})(?::|\s|$)"
)
SUPPLY_STAGES: tuple[tuple[str, str, str, frozenset[int]], ...] = (
    ("AP-5 · 红票", "AP-5", "采购凭证（红票）关卡", frozenset({0, 3, 5, 6})),
    ("CA-5 · 技能书", "CA-5", "技能概要关卡", frozenset({1, 2, 4, 6})),
    ("CE-6 · 龙门币", "CE-6", "龙门币关卡", frozenset({1, 3, 5, 6})),
    ("LS-6 · 作战记录", "LS-6", "作战记录关卡", frozenset(range(7))),
    ("SK-5 · 基建材料", "SK-5", "碳素与家具零件关卡", frozenset({0, 2, 4, 5})),
)


def parse_activity_stages(output: str) -> list[str]:
    stages: list[str] = []
    for line in output.splitlines():
        match = ACTIVITY_LINE_RE.match(line)
        if match and match.group(1) not in stages:
            stages.append(match.group(1))
    return stages


def all_supply_stages_open(output: str) -> bool:
    """Detect MAA's Official-server resource-collection announcement."""
    return any("资源收集" in line for line in output.splitlines())


def current_supply_choices(
    config: dict[str, Any],
    at: datetime | None = None,
) -> list[tuple[str, str, str]]:
    # Arknights' CN game day changes at 04:00 Beijing time, not at midnight.
    game_day = ((at or now_beijing()) - timedelta(hours=4)).weekday()
    show_all = bool(config.get("all_supply_stages_open", False))
    return [
        (label, stage, description)
        for label, stage, description, weekdays in SUPPLY_STAGES
        if show_all or game_day in weekdays
    ]


def refresh_activity_stages(config: dict[str, Any]) -> tuple[bool, str]:
    maa = str(config.get("maa_bin", "/opt/homebrew/bin/maa"))
    try:
        result = subprocess.run(
            [maa, "activity", "Official"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=maa_subprocess_env(config),
        )
    except Exception as exc:
        return False, f"活动关读取异常：{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return False, f"活动关读取失败：{detail or result.returncode}"

    output = f"{result.stdout}\n{result.stderr}"
    stages = parse_activity_stages(output)
    updated = dict(config)
    updated["activity_stages"] = stages
    updated["all_supply_stages_open"] = all_supply_stages_open(output)
    atomic_write_json(CONFIG_PATH, updated)
    config["activity_stages"] = stages
    config["all_supply_stages_open"] = updated["all_supply_stages_open"]
    return True, "、".join(stages) if stages else "当前无开放活动关"


def resource_update_due(
    last_run: dict[str, Any],
    at: datetime,
    interval_days: int,
) -> bool:
    """Return whether the bounded weekly Maa resource refresh should run."""
    raw = last_run.get("resource_update_attempted_at")
    if not raw:
        return True
    try:
        attempted = datetime.fromisoformat(str(raw)).astimezone(at.tzinfo)
    except (TypeError, ValueError):
        return True
    return at - attempted >= timedelta(days=max(1, interval_days))


def update_resources_if_due(
    token: str,
    channel_id: str,
    config: dict[str, Any],
) -> bool:
    """Run ``maa update`` at most once per configured interval and alert on failure."""
    at = now_beijing()
    last_run = load_json(LAST_RUN_PATH, {})
    if not isinstance(last_run, dict):
        last_run = {}
    interval_days = int(config.get("resource_update_interval_days", 7))
    if not resource_update_due(last_run, at, interval_days):
        return True

    maa = str(config.get("maa_bin", "/opt/homebrew/bin/maa"))
    timeout = max(30, int(config.get("resource_update_timeout", 300)))
    ok = False
    detail = ""
    try:
        result = subprocess.run(
            [maa, "update"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=maa_subprocess_env(config),
        )
        ok = result.returncode == 0
        detail = (result.stderr or result.stdout).strip()
        if not ok and not detail:
            detail = f"maa update 退出码 {result.returncode}"
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"

    merge_last_run(
        {
            "resource_update_attempted_at": at.isoformat(),
            "resource_update_status": "ok" if ok else "failed",
            "resource_update_detail": detail[-1000:],
        }
    )
    if ok:
        activity_ok, activity_detail = refresh_activity_stages(config)
        if activity_ok:
            merge_last_run(
                {
                    "activity_stages_updated_at": at.isoformat(),
                    "activity_stages": list(config.get("activity_stages", [])),
                }
            )
            return True
        ok = False
        detail = activity_detail
        merge_last_run(
            {
                "resource_update_status": "failed",
                "resource_update_detail": detail[-1000:],
            }
        )

    discord_request(
        token,
        "POST",
        f"/channels/{channel_id}/messages",
        {
            "embeds": [
                {
                    "title": "❌ MAA 每周资源更新失败",
                    "description": f"```text\n{detail[-1500:]}\n```",
                    "color": 0xED4245,
                    "timestamp": at.isoformat(),
                }
            ],
            "allowed_mentions": {"parse": []},
        },
    )
    return False


def _button(
    label: str,
    custom_id: str,
    *,
    style: int = 2,
    disabled: bool = False,
) -> dict[str, Any]:
    return {
        "type": 2,
        "style": style,
        "label": label[:80],
        "custom_id": custom_id,
        "disabled": disabled,
    }


def _stage_choices(
    plan: dict[str, Any],
    config: dict[str, Any],
) -> list[tuple[str, str, str]]:
    choices: list[tuple[str, str, str]] = [
        ("上次作战关卡", "", "沿用游戏内最近一次作战关卡"),
        ("1-7 · 固源岩", "1-7", "常用主线关卡"),
    ]
    choices.extend(current_supply_choices(config))
    activities = config.get("activity_stages", [])
    if isinstance(activities, list):
        for item in activities:
            stage = str(item).strip()
            if stage and stage not in {value for _label, value, _desc in choices}:
                choices.append((stage, stage, "当前开放活动关卡"))
    selected = str(plan.get("stage", ""))
    if selected and selected not in {value for _label, value, _desc in choices}:
        choices.insert(1, (selected, selected, "当前计划关卡"))
    return choices[:25]


def _stage_select(
    plan: dict[str, Any],
    config: dict[str, Any],
    registry: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    custom_id = f"{BUTTON_PREFIX}stage-select"
    selected = str(plan.get("stage", ""))
    select_args: dict[str, list[str]] = {}
    options: list[dict[str, Any]] = []
    for index, (label, stage, description) in enumerate(_stage_choices(plan, config)):
        value = f"choice:{index}"
        option: dict[str, Any] = {
            "label": label[:100],
            "value": value,
            "description": description[:100],
            "default": selected == stage,
            "emoji": {"name": "🎯" if selected == stage else "▫️"},
        }
        options.append(option)
        select_args[value] = ["plan", "set-stage", stage]
    registry[custom_id] = {
        "kind": "exec",
        "script": "maa_action.py",
        # The adapter resolves the selected opaque value through this exact
        # allowlist. Discord values never become command arguments directly.
        "select_args": select_args,
    }
    return {
        "type": 3,
        "custom_id": custom_id,
        "placeholder": "🎯 选择今天要刷的关卡",
        "min_values": 1,
        "max_values": 1,
        "options": options,
    }


def _copilot_stage_select(
    plan: dict[str, Any],
    config: dict[str, Any],
    registry: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Second dropdown: pick a stage to match prts.plus ops for (抄作业)."""
    custom_id = f"{maa_copilot.BUTTON_PREFIX}stage-select"
    select_args: dict[str, list[str]] = {}
    options: list[dict[str, Any]] = []
    for index, (label, stage, description) in enumerate(_stage_choices(plan, config)):
        if not stage:
            # "上次作战关卡" makes sense for farming, not for copilot: an
            # empty stage would blow up `copilot find ""`.
            continue
        value = f"copilot-choice:{index}"
        options.append(
            {
                "label": label[:100],
                "value": value,
                "description": description[:100],
                "emoji": {"name": "🤖"},
            }
        )
        select_args[value] = ["copilot", "find", stage]
    registry[custom_id] = {
        "kind": "exec",
        "script": "maa_action.py",
        "select_args": select_args,
    }
    return {
        "type": 3,
        "custom_id": custom_id,
        "placeholder": "🤖 抄作业：选择关卡匹配作业",
        "min_values": 1,
        "max_values": 1,
        "options": options,
    }


def build_components(
    plan: dict[str, Any],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    registry: dict[str, dict[str, Any]] = {}
    stage_select = _stage_select(plan, config, registry)
    copilot_select = _copilot_stage_select(plan, config, registry)

    skip_spec = (
        ("▶️ 恢复自动场次", "unskip", 3, ["plan", "skip-today", "off"])
        if plan.get("skip")
        else ("⏭️ 跳过自动场次", "skip", 4, ["plan", "skip-today", "on"])
    )
    action_specs = [skip_spec, ("📋 最近结果", "log", 2, ["log", "tail"])]
    action_buttons: list[dict[str, Any]] = []
    for label, suffix, style, args in action_specs:
        custom_id = f"{BUTTON_PREFIX}{suffix}"
        action_buttons.append(_button(label, custom_id, style=style))
        registry[custom_id] = {
            "kind": "exec",
            "script": "maa_action.py",
            "args": args,
        }

    explain_id = f"{BUTTON_PREFIX}explain"
    action_buttons.append(_button("🤖 请 Hermes 解释", explain_id, style=1))
    channel_id = str(config.get("channel_id", ""))
    registry[explain_id] = {
        "kind": "prompt",
        "thread_id": channel_id,
        "thread_name": "MAA长草助手",
        "prompt": (
            "请读取 ~/.hermes/state/maa/last_run.json 和 runs/ 下最新日志，"
            "用简短中文解释最近一次 MAA 日常结果、异常和下一步；只读，不修改计划，"
            "不要声称未从文件验证的执行结果。"
        ),
    }
    rows: list[dict[str, Any]] = [
        {"type": 1, "components": [stage_select]},
        {"type": 1, "components": action_buttons},
    ]
    if copilot_select.get("options"):
        # Discord rejects selects with zero options; skip the row entirely
        # when there is nothing to offer (e.g. no known stages at all).
        rows.insert(1, {"type": 1, "components": [copilot_select]})
    return (rows, registry)


def build_embed(plan: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    activity_stages = config.get("activity_stages", [])
    activity_note = "、".join(str(item) for item in activity_stages) or "未配置"
    # skip 只作用于 08:00/22:00；手动开跑是明确指令，永远照跑，别让卡片读成全停。
    skip = "是（手动开跑不受影响）" if plan.get("skip") else "否"
    return {
        "title": "🌱 MAA 今日计划",
        "description": "从下拉菜单选择关卡；今天的覆盖会在明天自动失效。",
        "color": 0x57F287,
        "fields": [
            {"name": "刷什么", "value": plan_stage_label(plan), "inline": True},
            {
                "name": "🧪 理智规则",
                "value": medicine_label(str(plan.get("medicine", "none"))),
                "inline": False,
            },
            {"name": "跳过自动场次", "value": skip, "inline": True},
            {"name": "当前活动关", "value": activity_note[:1024], "inline": False},
            {
                "name": "自动场次",
                "value": "08:00 早场 · 22:00 晚场（北京时间）",
                "inline": False,
            },
        ],
        "footer": {
            "text": "体力不足时停止 · 碎石永久关闭 · 计划变更不会打断已开始的场次"
        },
        "timestamp": now_beijing().isoformat(),
    }


def retire_old_plan_cards(token: str, channel_id: str) -> dict[str, int]:
    """Remove controls from recent superseded plan cards."""
    result = {"retired": 0, "failed": 0}
    messages = discord_request(
        token,
        "GET",
        f"/channels/{channel_id}/messages?limit=100",
    )
    if not isinstance(messages, list):
        return result
    for message in messages:
        if not isinstance(message, dict) or not message.get("components"):
            continue
        embeds = message.get("embeds", [])
        if not isinstance(embeds, list) or not any(
            isinstance(embed, dict) and embed.get("title") == "🌱 MAA 今日计划"
            for embed in embeds
        ):
            continue
        message_id = str(message.get("id", ""))
        if not message_id.isdigit():
            continue
        try:
            discord_request(
                token,
                "PATCH",
                f"/channels/{channel_id}/messages/{message_id}",
                {"components": []},
            )
            result["retired"] += 1
        except Exception:
            result["failed"] += 1
    return result


def main() -> int:
    config = load_config()
    channel_id = str(config.get("channel_id", "")).strip()
    if not channel_id.isdigit():
        raise RuntimeError("config.json 中尚未配置 Discord channel_id")
    token = discord_token()
    update_ok = update_resources_if_due(token, channel_id, config)
    # Availability changes daily at the game reset and independently of the
    # weekly resource package update, so probe it for every plan card.
    refresh_activity_stages(config)
    # A successful refresh rewrites config.json and mutates this copy; reload
    # so the card is always derived from the just-persisted source of truth.
    config = load_config()
    plan = effective_plan()
    components, entries = build_components(plan, config)

    # Registry writes are a merge under agent-book's exact same lock.
    with state_lock():
        registry = load_json(BUTTONS_PATH, {})
        if not isinstance(registry, dict):
            raise RuntimeError("共享按钮登记表格式错误")
        # Invalidates removed controls on old cards, including run-now and all
        # medication buttons, while preserving other features' registry keys.
        for custom_id in list(registry):
            if custom_id.startswith(BUTTON_PREFIX):
                registry.pop(custom_id)
        # Expire stale copilot candidate buttons (kept under their own
        # hermes:btn:maa-copilot: prefix, untouched by the sweep above).
        # Mutates this same registry so the write below can't resurrect them.
        maa_copilot.prune_expired_searches(registry)
        registry.update(entries)
        atomic_write_json(BUTTONS_PATH, registry)

    cleanup_result = cleanup_old_threads(token)
    cleanup_run_files(int(config.get("retention_days", 14)))
    retired = retire_old_plan_cards(token, channel_id)
    embed = build_embed(plan, config)
    if cleanup_result["failed"]:
        embed["footer"]["text"] += f" · {cleanup_result['failed']} 个旧子区删除失败"
    if retired["failed"]:
        embed["footer"]["text"] += f" · {retired['failed']} 张旧卡片收起失败"
    discord_request(
        token,
        "POST",
        f"/channels/{channel_id}/messages",
        {
            "embeds": [embed],
            "components": components,
            "allowed_mentions": {"parse": []},
        },
    )
    if not update_ok:
        raise RuntimeError("maa update 失败；已发送红色告警")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"MAA 计划卡失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
