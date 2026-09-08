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
    PLAN_PATH,
    state_lock,
)


BUTTON_PREFIX = "hermes:btn:maa:"
ACTIVITY_LINE_RE = re.compile(
    r"^\s*-\s+([A-Za-z0-9][A-Za-z0-9_-]{0,30}-\d{1,2})(?::|\s|$)"
)
SIDE_STORY_SECTION = "Opening side story stages:"
OPENING_SECTION_PREFIX = "Opening "
SUPPLY_STAGES: tuple[tuple[str, str, str, frozenset[int]], ...] = (
    ("AP-5 · 红票", "AP-5", "采购凭证（红票）关卡", frozenset({0, 3, 5, 6})),
    ("CA-5 · 技能书", "CA-5", "技能概要关卡", frozenset({1, 2, 4, 6})),
    ("CE-6 · 龙门币", "CE-6", "龙门币关卡", frozenset({1, 3, 5, 6})),
    ("LS-6 · 作战记录", "LS-6", "作战记录关卡", frozenset(range(7))),
    ("SK-5 · 基建材料", "SK-5", "碳素与家具零件关卡", frozenset({0, 2, 4, 5})),
)
DEFAULT_MAA_CLI_FORMULA = "MaaAssistantArknights/tap/maa-cli"
_VERSION_RE = re.compile(r"(?im)^(maa-cli|MaaCore|maa)\s+(v?\d+\.\d+\.\d+\S*)")


def is_side_story_heading(line: str) -> bool:
    """The one place that decides whether a line *is* the side-story heading.

    ``activity_snapshot()`` gates on this exact predicate, so the gate and the
    parser can never disagree about what counts as a recognized heading.
    Indentation is load-bearing below the heading — it is what separates an
    activity title from its stage bullets — so a shifted heading is a format
    change, not something to normalize away.
    """
    return line.rstrip() == SIDE_STORY_SECTION


def is_section_heading(line: str) -> bool:
    """Any ``Opening …`` section heading the parser already knows how to end on."""
    return line.rstrip().startswith(OPENING_SECTION_PREFIX)


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
        if is_side_story_heading(line):
            in_side_stories = True
            current = None
            continue
        if in_side_stories and is_section_heading(line):
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
    lines = output.splitlines()
    if not any(is_side_story_heading(line) for line in lines):
        # Three outcomes, and only the middle one may clear live state:
        #
        #   heading text present but never as a heading *line*  -> format
        #     changed under us (a decorated or reflowed heading).  The parser
        #     would silently yield zero events here, so fail loudly instead.
        #   no heading text, but some other recognized ``Opening …`` section
        #     -> the query succeeded during an activity gap.  Empty snapshot.
        #   nothing recognizable at all -> untrustworthy output.  Fail loudly.
        recognized_gap = SIDE_STORY_SECTION not in output and any(
            is_section_heading(line) for line in lines
        )
        if not recognized_gap:
            raise RuntimeError("maa activity 输出格式已变化：找不到活动关分区")
        return {
            "stages": [],
            "events": [],
            "all_supply_stages_open": all_supply_stages_open(output),
        }
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
    updated.pop("activity_events", None)
    updated["all_supply_stages_open"] = bool(
        snapshot.get("all_supply_stages_open", False)
    )
    atomic_write_json(CONFIG_PATH, updated)
    config.update(
        {
            "activity_stages": updated["activity_stages"],
            "all_supply_stages_open": updated["all_supply_stages_open"],
        }
    )
    config.pop("activity_events", None)


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
    drops = stage_drops(config)
    if isinstance(activities, list):
        for item in activities:
            stage = str(item).strip()
            if stage and stage not in {value for _label, value, _desc in choices}:
                # 活动关的主掉落从上游 `stages.json` 现取，新活动自动跟随，不靠手工表。
                materials = "、".join((drops.get(stage) or {}).get("materials") or [])
                label = f"{stage} · {materials}" if materials else stage
                choices.append(
                    (
                        label,
                        stage,
                        f"当前开放活动关卡{'（主掉落 ' + materials + '）' if materials else ''}",
                    )
                )
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


# ---------------------------------------------------------------------------
# 材料驱动选关（PLAN_CLEARING 步骤 6）
# ---------------------------------------------------------------------------

# 一次计划卡进程内只解析一遍 stages.json（807KB / 616 关）。键取**资源目录**，
# 因为那才是决定内容的东西——用配置目录做键会让换了资源目录的调用拿到上一份。
_STAGE_DROPS_CACHE: dict[str, dict[str, dict[str, Any]]] = {}


def stage_drops(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """`{关卡代号: {"materials": [主掉落材料名], "ap": 理智}}`，从上游资源派生。

    **这是「新活动自动跟随」的地基**：活动关的材料映射不再靠手工表——上游每次热更新
    带来新活动，`stages.json` 里就有它每一关的 `dropInfos`，下一张计划卡自动认识。

    只取 `NORMAL_DROP`（主掉落）。`EXTRA_DROP` 是副掉落（`1-7` 有 8 个），把它们算进来
    会让「刷这关补这个材料」失真——按副掉落去定关，期望产出可能差一个数量级。
    `FURNITURE` / `SPECIAL_DROP` 同理排除。

    资源本（`CE-6` / `AP-5` 这些）在 `stages.json` 里**只有 `FURNITURE`**，龙门币和红票
    不是以掉落建模的，所以这张表查不到它们——它们继续由 `farming_stage_table` 手工表覆盖，
    两张表是**合并**关系，派生表不能替换手工表。

    任何读取失败都返回空表，不抛异常：旁路数据不该带走整张计划卡（D-2 的教训）。
    """
    cache_key = str(config.get("maa_resource_dir") or "")
    if cache_key in _STAGE_DROPS_CACHE:
        return _STAGE_DROPS_CACHE[cache_key]
    table: dict[str, dict[str, Any]] = {}
    try:
        names: dict[str, str] = {}
        stages: Any = None
        for directory in maa_copilot._resource_dirs(config):
            if not names:
                index = load_json(directory / "item_index.json", {})
                if isinstance(index, dict):
                    for item_id, detail in index.items():
                        name = (
                            detail.get("name") if isinstance(detail, dict) else detail
                        )
                        if isinstance(name, str) and name:
                            names[str(item_id)] = name
            if stages is None:
                loaded = load_json(directory / "stages.json", None)
                if isinstance(loaded, list):
                    stages = loaded
        for entry in stages or []:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("code") or "").strip()
            if not code:
                continue
            materials: list[str] = []
            for drop in entry.get("dropInfos") or []:
                if not isinstance(drop, dict):
                    continue
                if drop.get("dropType") != "NORMAL_DROP":
                    continue
                name = names.get(str(drop.get("itemId")))
                if name and name not in materials:
                    materials.append(name)
            if materials:
                try:
                    ap = int(entry.get("apCost") or 0)
                except (TypeError, ValueError):
                    ap = 0
                table[code] = {"materials": materials, "ap": ap}
    except Exception:
        table = {}
    _STAGE_DROPS_CACHE[cache_key] = table
    return table


def material_stage_candidates(config: dict[str, Any]) -> dict[str, list[str]]:
    """`{材料名: [候选关卡]}`，**有序**，越靠前越优先。

    返回候选**列表**而不是单个关卡，因为同一材料常常有多个关会掉，而其中大多数
    是**已经关闭的往期活动关**。全局定死一个关会选中 `异铁组 → DH-8` 这种早就打不了的
    关，然后在「今天可选」那一步被整条丢掉——材料明明有开放的关可刷，却从推荐里消失了。
    真正的选择必须放到 `farming_recommendations()` 里、在当天可选集合内做。

    排序（`stages.json` **没有掉率**，所以这是启发式，不是算出来的最优解）：

    1. `farming_stage_table` 手工指定的关——用户显式写下的，不该被自动派生盖掉，
       而且资源本只有它覆盖得到；
    2. 理智消耗低的在前（同材料多关时，低理智关通常是刷素材的那个）；
    3. 关卡名字典序兜底，保证**确定性**——排序不稳的话，同样的库存今天推 A 明天推 B，
       用户会以为是缺口变了。
    """
    manual = config.get("farming_stage_table")
    manual = manual if isinstance(manual, dict) else {}
    derived: dict[str, list[tuple[int, str]]] = {}
    for stage, detail in stage_drops(config).items():
        for material in detail["materials"]:
            derived.setdefault(material, []).append((detail["ap"], stage))

    candidates: dict[str, list[str]] = {}
    for material in set(derived) | {str(key) for key in manual}:
        ordered: list[str] = []
        pinned = str(manual.get(material) or "").strip()
        if pinned:
            ordered.append(pinned)
        for _ap, stage in sorted(derived.get(material, [])):
            if stage not in ordered:
                ordered.append(stage)
        candidates[material] = ordered
    return candidates


def _manual_weight(config: dict[str, Any], material: str) -> float:
    weights = config.get("farming_manual_weights")
    if not isinstance(weights, dict):
        return 0.0
    try:
        return float(weights.get(material, 0))
    except (TypeError, ValueError):
        return 0.0


def farming_recommendations(
    config: dict[str, Any],
    depot_state: dict[str, Any],
    available_stages: set[str],
    at: datetime | None = None,
) -> dict[str, Any]:
    """按「可用天数 = 当前库存 / 该材料日均消耗」升序推荐关卡。

    **排序指标只用这一个。**「库存特别少」天然就是「可用天数特别小」，会自动排到
    最前，不需要额外的例外规则，也不许再叠一层「库存低于 N 就优先」。

    三条降级路径，都不抛异常（异常会带走整张计划卡，D-2 的教训）：

    1. **冷启动**：`consumption_history` 不足 2 次对表 → 全部按手工权重排，
       卡片上明说是降级；
    2. **零消耗**：≥4 周滚动窗口内该材料消耗为 0 → 它的可用天数不是 ∞ 而是
       **未知**，退回该材料的手工权重排序，排在有实测数据的材料**之后**
       （当成 0 天会把没消耗的材料顶到第一位，正好反了）；
    3. **查不到关卡**：手工表与上游派生表都没有这个材料的关 → 不推荐、卡片明说。
       **不许猜关**——猜错就是每天几百理智刷错东西，而且用户看不出来。
       注意「查不到」和「今天没开」是两回事，分别记在 `skipped_no_stage` 和
       `skipped_closed` 里：前者要补表，后者等它开就行。

    关卡来源是 `material_stage_candidates()` 的**有序候选**，在 `available_stages`
    里取第一个命中的。这样当期活动关（每期自动从 `stages.json` 认出来）能直接参与，
    往期关闭的活动关不会把材料整条吃掉。
    """
    import maa_depot

    table = material_stage_candidates(config)
    measured = maa_depot.has_measured_consumption(depot_state)
    rates = (
        maa_depot.consumption_rates_window(depot_state, at=at) if measured else {}
    )
    stock = maa_depot.stock_by_name(depot_state)
    per_day = {
        str(detail.get("name") or item_id): float(detail.get("per_day") or 0.0)
        for item_id, detail in rates.items()
        if isinstance(detail, dict)
    }

    manual = config.get("farming_stage_table")
    manual = manual if isinstance(manual, dict) else {}

    def worth_reporting(material: str) -> bool:
        """值不值得在卡片上说「这个材料今天没关刷」。

        派生表覆盖了全游戏几百种材料，其中绝大多数你既没在消耗、也没配过。
        全报出来这行就是噪音，会把真正要看的信息淹掉。只报两种：
        你**确实在消耗**的，和你**自己配进手工表**的（配了就说明你在意）。
        """
        return per_day.get(material, 0.0) > 0 or material in manual

    universe = set(table) | {name for name, rate in per_day.items() if rate > 0}
    ranked: list[dict[str, Any]] = []
    no_stage: list[str] = []
    closed: list[str] = []
    for material in sorted(universe):
        options = table.get(material) or []
        if not options:
            if worth_reporting(material):
                no_stage.append(material)
            continue
        stage = next((code for code in options if code in available_stages), "")
        if not stage:
            if worth_reporting(material):
                closed.append(f"{material}→{options[0]}")
            continue
        rate = per_day.get(material, 0.0)
        days_left = (
            round(stock.get(material, 0) / rate, 2) if measured and rate > 0 else None
        )
        ranked.append(
            {
                "material": material,
                "stage": stage,
                "stock": stock.get(material, 0),
                "per_day": rate,
                "days_left": days_left,
                "weight": _manual_weight(config, material),
                # measured = 实测推荐；manual = 猜的（冷启动或零消耗）
                "basis": "measured" if days_left is not None else "manual",
            }
        )
    # 有实测可用天数的排前面（升序）；其余按手工权重降序垫后。
    ranked.sort(
        key=lambda item: (
            0 if item["days_left"] is not None else 1,
            item["days_left"] if item["days_left"] is not None else 0.0,
            -item["weight"],
            item["material"],
        )
    )
    return {
        "degraded": not measured,
        "reason": (
            "尚无跨区间消耗数据（`depot.json` 的 `consumption_history` 不足 2 次对表），"
            "当前按 `config.farming_manual_weights` 的手工权重推荐"
            if not measured
            else ""
        ),
        "items": ranked,
        "skipped_no_stage": no_stage,
        "skipped_closed": closed,
    }


def farming_note(result: dict[str, Any]) -> str:
    """计划卡上的一句话说明：用户要能分辨「实测推荐」还是「这是猜的」。"""
    lines: list[str] = []
    if result.get("degraded"):
        lines.append(f"⚠️ **降级模式**：{result['reason']}。")
    items = result.get("items") or []
    if not items:
        lines.append("暂无可推荐的材料关卡。")
    else:
        measured = [item for item in items if item["basis"] == "measured"]
        if measured:
            head = measured[0]
            lines.append(
                f"最缺：{head['material']}（库存 {head['stock']}，"
                f"日均 {head['per_day']:.2f}，约可用 {head['days_left']} 天）"
            )
        manual = [item for item in items if item["basis"] == "manual"]
        if manual and not result.get("degraded"):
            lines.append(
                "（"
                + "、".join(item["material"] for item in manual[:4])
                + " 在近 4 周窗口里没有消耗，可用天数**未知**，按手工权重垫后）"
            )
    if result.get("skipped_no_stage"):
        lines.append(
            "未纳入推荐（上游掉落表和 `farming_stage_table` 里都查不到关卡）："
            + "、".join(result["skipped_no_stage"][:6])
        )
    if result.get("skipped_closed"):
        lines.append(
            # 这一行同时收「你在消耗的」和「你配进手工表的」两类（见 worth_reporting），
            # 所以措辞不能写成「在消耗但……」——手工表里那些多半并没有在消耗。
            "今天没有开放的关可刷：" + "、".join(result["skipped_closed"][:6])
        )
    return "\n".join(lines)[:1024]


def auto_stage(result: dict[str, Any]) -> tuple[str, str]:
    """今天自动定的关 + 一句为什么。定不下来就返回 `("", 原因)`，由调用方沿用默认。

    取代原来那个「按材料缺口选关」的下拉（2026-09-08 用户拍板删掉）。删的理由是
    那个下拉**没有存在意义**：缺口每天都在变，下拉却只能选关不能显示缺口，
    和第一个下拉的语义完全重复。缺口应该是**后台固定计算**的结果，不是让用户去挑的选项。

    **降级期不自动改关**：没有实测消耗数据时，排序依据只是 `farming_manual_weights`
    的手工权重——那是猜的。拿猜的结果去改每天刷什么，用户还看不出是猜的，
    正是「不许猜关」那条红线要防的事。此时沿用 `defaults.stage`，卡片写明在等数据。
    """
    if result.get("degraded"):
        return "", "还没有实测消耗数据（Depot 需要满 2 次对表），今天沿用默认关"
    for item in result.get("items") or []:
        if item["basis"] != "measured":
            continue
        return item["stage"], (
            f"{item['material']} 库存 {item['stock']}、日均 {item['per_day']:.2f}，"
            f"约只够 {item['days_left']} 天"
        )
    return "", "所有材料在近 4 周窗口里都没有消耗，今天沿用默认关"


def load_depot_state() -> dict[str, Any]:
    """读库存快照；读不出来就当空的——计划卡不因旁路数据失败而消失（D-2）。"""
    try:
        import maa_depot

        state = load_json(maa_depot.DEPOT_PATH, {})
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def current_farming_result(
    plan: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    """今天的材料推荐。只推**今天真的选得到**的关（与第一个下拉同一个可选集）。"""
    return farming_recommendations(
        config,
        load_depot_state(),
        {stage for _label, stage, _desc in _stage_choices(plan, config) if stage},
    )


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


def apply_auto_stage(result: dict[str, Any], at: datetime | None = None) -> str:
    """把自动定的关写进当日 `plan.json`。返回写入的关卡，没写返回空串。

    **绝不覆盖用户的手动选择**：只有当天的覆盖里根本没有 `stage`、或者上一次
    写它的就是本函数（`stage_source == "auto"`）时才写。用户从下拉 1 选过、
    或在聊天里说过「今天刷 X」之后，`stage_source` 是 `discord`，当天就不会再被自动改掉。

    只在 07:50 计划卡跑一次（用户拍板）：卡片上写的关就是当天真正会跑的关，
    早晚两场一致。次日 `plan.json` 按日期自然失效，重新自动算。
    """
    stage, _why = auto_stage(result)
    if not stage:
        return ""
    moment = at or now_beijing()
    # 判据是 `stage_source` 而不是 `source`：`source` 记的是「最后一次写这份覆盖的是谁」，
    # 点一次「跳过」也会把它写成 discord，拿它当闸门等于点跳过就冻住当天的自动定关。
    with state_lock():
        current = load_json(PLAN_PATH, {})
        if not isinstance(current, dict) or current.get("date") != moment.date().isoformat():
            current = {"date": moment.date().isoformat()}
        if "stage" in current and current.get("stage_source") != "auto":
            return ""
        current["stage"] = stage
        current["stage_source"] = "auto"
        current["updated_at"] = moment.isoformat()
        atomic_write_json(PLAN_PATH, current)
    return stage


def today_stage_source(at: datetime | None = None) -> str:
    """当天的 `plan.stage` 是谁写的：`auto`（缺口算的）/ `discord`（你选的）/ 空。

    **必须直接读 `plan.json`**：`effective_plan()` 只把 `stage` / `skip` 两个键叠进
    默认计划，`normalize_plan()` 会把 `stage_source` 连同其余字段一起丢掉——
    从它返回的 plan 里查这个字段永远是空的。
    """
    moment = at or now_beijing()
    current = load_json(PLAN_PATH, {})
    if not isinstance(current, dict):
        return ""
    if current.get("date") != moment.date().isoformat():
        return ""
    return str(current.get("stage_source") or "")


def build_embed(
    plan: dict[str, Any],
    config: dict[str, Any],
    update: dict[str, Any] | None = None,
    farming: dict[str, Any] | None = None,
    stage_source: str = "",
) -> dict[str, Any]:
    activity_stages = config.get("activity_stages", [])
    activity_note = "、".join(str(item) for item in activity_stages) or "未配置"
    # skip 只作用于 08:00/22:00；手动开跑是明确指令，永远照跑，别让卡片读成全停。
    skip = "是（手动开跑不受影响）" if plan.get("skip") else "否"
    # 「刷什么」要能一眼看出这关是自动定的还是自己选的——下拉 3 删掉之后，
    # 用户判断「缺口算出来是什么」全靠这一行和下面那段说明。归属只认
    # `stage_source`（`today_stage_source()` 从 plan.json 读），不拿「今天算出来的关
    # 恰好等于计划里的关」当依据：手选的关和缺口算的关撞上时那会说成自动选的。
    stage_line = plan_stage_label(plan)
    if stage_source == "auto":
        stage_line = f"{stage_line}（按材料缺口自动选）"
    elif stage_source == "discord":
        stage_line = f"{stage_line}（你手动选的）"
    fields: list[dict[str, Any]] = [
        {"name": "刷什么", "value": stage_line, "inline": True},
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
    if farming is not None:
        auto, why = auto_stage(farming)
        lines = [f"{'✅ 今天自动定为 ' + auto if auto else '⏸️ 今天没有自动改关'}：{why}"]
        note = farming_note(farming)
        if note:
            lines.append(note)
        fields.append(
            {
                "name": "📦 材料缺口" + ("（降级）" if farming.get("degraded") else ""),
                "value": "\n".join(lines)[:1024],
                "inline": False,
            }
        )
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
    # 材料缺口 → 自动定关，必须发生在读 plan 和建卡片**之前**：卡片上写的关
    # 就是当天两场真正会跑的关。用户手动选过的当天不动（见 apply_auto_stage）。
    farming = current_farming_result(effective_plan(), config)
    apply_auto_stage(farming)
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
    embed = build_embed(plan, config, update, farming, today_stage_source())
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
