#!/usr/bin/env python3
"""Publish the 07:50 MAA plan card and maintain persistent buttons."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from typing import Any

import maa_copilot
from maa_common import (
    BUTTONS_PATH,
    CONFIG_PATH,
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
SIDE_STORY_SECTION = "Opening side story stages:"
SUPPLY_STAGES: tuple[tuple[str, str, str, frozenset[int]], ...] = (
    ("AP-5 · 红票", "AP-5", "采购凭证（红票）关卡", frozenset({0, 3, 5, 6})),
    ("CA-5 · 技能书", "CA-5", "技能概要关卡", frozenset({1, 2, 4, 6})),
    ("CE-6 · 龙门币", "CE-6", "龙门币关卡", frozenset({1, 3, 5, 6})),
    ("LS-6 · 作战记录", "LS-6", "作战记录关卡", frozenset(range(7))),
    ("SK-5 · 基建材料", "SK-5", "碳素与家具零件关卡", frozenset({0, 2, 4, 5})),
)
DEFAULT_MAA_CLI_FORMULA = "MaaAssistantArknights/tap/maa-cli"
_VERSION_RE = re.compile(r"(?im)^(maa-cli|MaaCore|maa)\s+(v?\d+\.\d+\.\d+\S*)")


def parse_activity_events(output: str) -> list[dict[str, Any]]:
    """Return the currently open side stories and their recommended stages.

    ``maa activity`` prints one unindented bullet per activity and indented
    bullets for its navigation aliases and recommended farming stages.  Keep
    the human title as part of the observation identity: an original run and
    a rerun can reuse the same stage prefix but are different rotations.
    """
    events: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_side_stories = False
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if line == SIDE_STORY_SECTION:
            in_side_stories = True
            current = None
            continue
        if in_side_stories and line.startswith("Opening "):
            break
        if not in_side_stories:
            continue
        if line.startswith("- "):
            current = {"title": line[2:].strip()[:120], "stages": []}
            events.append(current)
            continue
        if current is None:
            continue
        match = ACTIVITY_LINE_RE.match(line)
        if match and match.group(1) not in current["stages"]:
            current["stages"].append(match.group(1))
    return [event for event in events if event["stages"]]


def activity_snapshot(output: str) -> dict[str, Any]:
    if SIDE_STORY_SECTION not in output:
        raise RuntimeError("maa activity 输出格式已变化：找不到活动关分区")
    events = parse_activity_events(output)
    stages: list[str] = []
    for event in events:
        for stage in event["stages"]:
            if stage not in stages:
                stages.append(stage)
    return {
        "stages": stages,
        "events": events,
        "all_supply_stages_open": all_supply_stages_open(output),
    }


def activity_target(snapshot: dict[str, Any]) -> dict[str, str] | None:
    """Return MaaCore's first recommended stage and its rotation identity."""
    stages = snapshot.get("stages")
    if not isinstance(stages, list) or not stages:
        return None
    stage = str(stages[0])
    title = "当前活动"
    events = snapshot.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            event_stages = event.get("stages")
            if isinstance(event_stages, list) and stage in event_stages:
                title = str(event.get("title") or title)[:120]
                break
    head, separator, tail = stage.rpartition("-")
    family = head if separator and tail.isdigit() else stage
    return {"title": title, "stage": stage, "key": f"{title}|{family}"}


def activity_target_is_current(
    snapshot: dict[str, Any], stage: str, observation_key: str
) -> bool:
    """Whether a prepared stage still belongs to the observed rotation.

    MaaCore may reorder the recommended farming stages without the activity
    itself changing.  Keep an existing card valid while its exact stage is
    still open and the title/family identity remains the same.
    """
    target = activity_target(snapshot)
    stages = snapshot.get("stages")
    return bool(
        target is not None
        and target["key"] == observation_key
        and isinstance(stages, list)
        and stage in stages
    )


def query_activity_snapshot(
    config: dict[str, Any],
    *,
    timeout: float = 30,
) -> dict[str, Any]:
    """Read the live activity view from MaaCore or raise with its real error."""
    maa = str(config.get("maa_bin", "/opt/homebrew/bin/maa"))
    result = subprocess.run(
        [maa, "activity", "Official"],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=maa_subprocess_env(config),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"活动关读取失败：{detail or result.returncode}")
    return activity_snapshot(f"{result.stdout}\n{result.stderr}")


def persist_activity_snapshot(
    config: dict[str, Any], snapshot: dict[str, Any]
) -> None:
    updated = dict(config)
    updated["activity_stages"] = list(snapshot.get("stages", []))
    updated["activity_events"] = list(snapshot.get("events", []))
    updated["all_supply_stages_open"] = bool(
        snapshot.get("all_supply_stages_open", False)
    )
    atomic_write_json(CONFIG_PATH, updated)
    config.update(
        {
            "activity_stages": updated["activity_stages"],
            "activity_events": updated["activity_events"],
            "all_supply_stages_open": updated["all_supply_stages_open"],
        }
    )


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
    try:
        snapshot = query_activity_snapshot(config)
    except Exception as exc:
        return False, f"活动关读取异常：{type(exc).__name__}: {exc}"
    stages = list(snapshot.get("stages", []))
    persist_activity_snapshot(config, snapshot)
    return True, "、".join(stages) if stages else "当前无开放活动关"


def parse_maa_versions(output: str) -> tuple[str, str]:
    """Return ``(maa-cli version, MaaCore version)`` from ``maa version`` output."""
    cli = ""
    core = ""
    for match in _VERSION_RE.finditer(output or ""):
        version = match.group(2)
        if not version.lower().startswith("v"):
            version = f"v{version}"
        if match.group(1).lower() == "maacore":
            core = version
        else:
            cli = version
    return cli, core


def format_update_summary(result: dict[str, Any]) -> str:
    """One Discord field for today's maa-cli / MaaCore check."""
    if not result.get("ok"):
        detail = str(result.get("detail") or "未知错误").strip()
        first = detail.splitlines()[0] if detail else "未知错误"
        return f"今天检查失败：{first[:300]}"

    def _line(label: str, before: str, after: str) -> tuple[bool, str]:
        before = before or "未知"
        after = after or before
        if before != after:
            return True, f"{label} {before} → {after}"
        return False, f"{label} {after}"

    cli_changed, cli_text = _line(
        "maa-cli",
        str(result.get("cli_before") or ""),
        str(result.get("cli_after") or ""),
    )
    core_changed, core_text = _line(
        "MaaCore",
        str(result.get("core_before") or ""),
        str(result.get("core_after") or ""),
    )
    if result.get("changed") or cli_changed or core_changed:
        return f"今天已更新：{cli_text}；{core_text}"
    return f"今天已检查，无新版本（{cli_text} · {core_text}）"


def _combined_output(stdout: str, stderr: str) -> str:
    return "\n".join(part for part in (stdout, stderr) if part)


def _run_captured(
    argv: list[str],
    *,
    env: dict[str, str] | None,
    timeout: int,
) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=env,
        )
    except Exception as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"
    return (
        result.returncode,
        (result.stdout or "").strip(),
        (result.stderr or "").strip(),
    )


def _brew_env(*, auto_update: bool = True) -> dict[str, str]:
    env = os.environ.copy()
    env["HOMEBREW_NO_ENV_HINTS"] = "1"
    if not auto_update:
        env["HOMEBREW_NO_AUTO_UPDATE"] = "1"
    return env


def read_maa_versions(config: dict[str, Any]) -> tuple[str, str]:
    maa = str(config.get("maa_bin", "/opt/homebrew/bin/maa"))
    code, stdout, stderr = _run_captured(
        [maa, "version"],
        env=maa_subprocess_env(config, keep_proxy=True),
        timeout=30,
    )
    if code != 0:
        return "", ""
    return parse_maa_versions(_combined_output(stdout, stderr))


def persist_installed_versions(
    config: dict[str, Any],
    cli_version: str,
    core_version: str,
) -> None:
    if not cli_version and not core_version:
        return
    current = load_json(CONFIG_PATH, {})
    if not isinstance(current, dict):
        current = {}
    if cli_version:
        current["maa_cli_version"] = cli_version
        config["maa_cli_version"] = cli_version
    if core_version:
        current["maa_core_version"] = core_version
        config["maa_core_version"] = core_version
    atomic_write_json(CONFIG_PATH, current)


def upgrade_maa_cli(config: dict[str, Any]) -> dict[str, Any]:
    """Upgrade the Homebrew maa-cli formula when a newer bottle exists."""
    formula = str(config.get("maa_cli_formula", DEFAULT_MAA_CLI_FORMULA)).strip()
    timeout = max(30, int(config.get("maa_cli_update_timeout", 180)))
    brew = shutil.which("brew")
    if not brew:
        return {"ok": True, "changed": False, "detail": "未找到 brew，跳过 maa-cli"}
    code, stdout, stderr = _run_captured(
        [brew, "outdated", "--quiet", "--formula", formula],
        env=_brew_env(auto_update=True),
        timeout=timeout,
    )
    if code != 0:
        detail = _combined_output(stdout, stderr) or f"brew outdated 退出码 {code}"
        return {"ok": False, "changed": False, "detail": detail}
    if not stdout:
        return {"ok": True, "changed": False, "detail": "maa-cli 已是最新"}
    code, up_out, up_err = _run_captured(
        [brew, "upgrade", "--formula", formula],
        env=_brew_env(auto_update=False),
        timeout=timeout,
    )
    detail = _combined_output(up_out, up_err) or stdout
    if code != 0:
        return {
            "ok": False,
            "changed": False,
            "detail": detail or f"brew upgrade 退出码 {code}",
        }
    return {"ok": True, "changed": True, "detail": detail or f"已升级 {formula}"}


def update_maa_core(config: dict[str, Any]) -> dict[str, Any]:
    """Download a newer Beta MaaCore (and bundled resources) when one exists."""
    maa = str(config.get("maa_bin", "/opt/homebrew/bin/maa"))
    timeout = max(30, int(config.get("resource_update_timeout", 300)))
    code, stdout, stderr = _run_captured(
        [maa, "update", "--batch"],
        env=maa_subprocess_env(config, keep_proxy=True),
        timeout=timeout,
    )
    detail = _combined_output(stdout, stderr)
    if code != 0:
        return {
            "ok": False,
            "detail": detail or f"maa update 退出码 {code}",
        }
    return {"ok": True, "detail": detail}


def update_hot_resources(config: dict[str, Any]) -> dict[str, Any]:
    """Keep the MaaResource overlay compatible with the installed MaaCore."""
    maa = str(config.get("maa_bin", "/opt/homebrew/bin/maa"))
    timeout = max(30, int(config.get("resource_update_timeout", 300)))
    code, stdout, stderr = _run_captured(
        [maa, "hot-update", "--batch"],
        env=maa_subprocess_env(config, keep_proxy=True),
        timeout=timeout,
    )
    detail = _combined_output(stdout, stderr)
    if code != 0:
        return {
            "ok": False,
            "detail": detail or f"maa hot-update 退出码 {code}",
        }
    return {"ok": True, "detail": detail}


def check_and_update_upstream(
    token: str,
    channel_id: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Check maa-cli and MaaCore on every plan card; follow upstream if newer."""
    at = now_beijing()
    cli_before, core_before = read_maa_versions(config)
    cli_step = upgrade_maa_cli(config)
    core_step = update_maa_core(config)
    # The hot-update directory overrides files bundled with MaaCore. Updating
    # only MaaCore can therefore leave an older infrast.json pointing at
    # templates that no longer exist in the new bundle. Do not advance the
    # overlay when the core update itself failed, because the inverse mismatch
    # (new resources on an old core) is unsafe too.
    hot_step = (
        update_hot_resources(config)
        if core_step.get("ok")
        else {"ok": False, "detail": "MaaCore 更新失败，未更新热更新资源"}
    )
    cli_after, core_after = read_maa_versions(config)
    cli_after = cli_after or cli_before
    core_after = core_after or core_before
    persist_installed_versions(config, cli_after, core_after)
    ok = (
        bool(cli_step.get("ok"))
        and bool(core_step.get("ok"))
        and bool(hot_step.get("ok"))
    )
    changed = bool(cli_step.get("changed")) or (
        bool(cli_before) and bool(cli_after) and cli_before != cli_after
    ) or (bool(core_before) and bool(core_after) and core_before != core_after)
    detail = "\n".join(
        str(part)
        for part in (
            cli_step.get("detail"),
            core_step.get("detail"),
            hot_step.get("detail"),
        )
        if part
    )
    if not ok and not detail:
        detail = "上游更新失败"
    result = {
        "ok": ok,
        "changed": changed,
        "cli_before": cli_before,
        "cli_after": cli_after,
        "core_before": core_before,
        "core_after": core_after,
        "detail": detail[-1000:],
    }
    result["summary"] = format_update_summary(result)
    merge_last_run(
        {
            "resource_update_attempted_at": at.isoformat(),
            "resource_update_status": "ok" if ok else "failed",
            "resource_update_detail": result["detail"],
            "resource_update_summary": result["summary"],
            "resource_update_changed": changed,
            "maa_cli_version": cli_after,
            "maa_core_version": core_after,
        }
    )
    if not ok:
        discord_request(
            token,
            "POST",
            f"/channels/{channel_id}/messages",
            {
                "embeds": [
                    {
                        "title": "❌ MAA 上游更新失败",
                        "description": f"```text\n{detail[-1500:]}\n```",
                        "color": 0xED4245,
                        "timestamp": at.isoformat(),
                    }
                ],
                "allowed_mentions": {"parse": []},
            },
        )
    return result


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


def build_embed(
    plan: dict[str, Any],
    config: dict[str, Any],
    update: dict[str, Any] | None = None,
) -> dict[str, Any]:
    activity_stages = config.get("activity_stages", [])
    activity_note = "、".join(str(item) for item in activity_stages) or "未配置"
    # skip 只作用于 08:00/22:00；手动开跑是明确指令，永远照跑，别让卡片读成全停。
    skip = "是（手动开跑不受影响）" if plan.get("skip") else "否"
    fields: list[dict[str, Any]] = [
        {"name": "刷什么", "value": plan_stage_label(plan), "inline": True},
        {
            "name": "🧪 理智规则",
            "value": medicine_label(str(plan.get("medicine", "none"))),
            "inline": False,
        },
        {
            "name": "每日剿灭",
            "value": "当天第一场用代理卡全权委托；满 1800 合成玉/周后自动停，当天不再进第二次",
            "inline": False,
        },
        {"name": "跳过自动场次", "value": skip, "inline": True},
        {"name": "当前活动关", "value": activity_note[:1024], "inline": False},
        {
            "name": "自动场次",
            "value": "08:00 早场 · 22:00 晚场（北京时间）",
            "inline": False,
        },
    ]
    if update:
        fields.append(
            {
                "name": "上游更新",
                "value": str(update.get("summary") or format_update_summary(update))[:1024],
                "inline": False,
            }
        )
    return {
        "title": "🌱 MAA 今日计划",
        "description": "从下拉菜单选择关卡；今天的覆盖会在明天自动失效。",
        "color": 0x57F287,
        "fields": fields,
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
    update = check_and_update_upstream(token, channel_id, config)
    # Availability changes daily at the game reset and independently of the
    # MaaCore package update, so probe it for every plan card.
    activity_ok, activity_detail = refresh_activity_stages(config)
    if not activity_ok:
        discord_request(
            token,
            "POST",
            f"/channels/{channel_id}/messages",
            {
                "embeds": [
                    {
                        "title": "❌ MAA 活动关探测失败",
                        "description": activity_detail[:1500],
                        "color": 0xED4245,
                        "timestamp": now_beijing().isoformat(),
                    }
                ],
                "allowed_mentions": {"parse": []},
            },
        )
        raise RuntimeError(activity_detail)
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
    embed = build_embed(plan, config, update)
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
    if not update.get("ok"):
        raise RuntimeError("maa 上游更新失败；已发送红色告警")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"MAA 计划卡失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
