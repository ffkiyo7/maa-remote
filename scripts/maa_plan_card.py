#!/usr/bin/env python3
"""Publish the two daily MAA plan cards (07:50 早场 / 21:50 晚场) and the
on-demand 抄作业 card, and maintain their persistent buttons."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import maa_copilot
import maa_farming
from maa_common import (
    BEIJING,
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
    plan_sessions,
    plan_stage_label,
    PLAN_PATH,
    state_lock,
    WATCH_PATH,
)


BUTTON_PREFIX = "hermes:btn:maa:"
CARD_TITLE_PREFIX = "🌱 MAA 今日计划"
# 一天两张卡（D1）：07:50 的早场卡跟随上游更新，21:50 的晚场卡不跟。
PHASE_TITLES = {"am": "早场 08:00", "pm": "晚场 22:00"}
# MaaCore 的活动时间缓存（`maa activity` 按 etag 拉取的那份），地基事实 E。
DEFAULT_STAGE_ACTIVITY_PATH = (
    Path.home() / "Library" / "Caches" / "com.loong.maa" / "StageActivityV2.json"
)
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


def select_phase(
    argv: list[str],
    at: datetime | None = None,
    cwd: Path | None = None,
) -> str:
    """哪一场的卡：argv > **工作目录名** > 按北京时间的小时兜底。

    与 `maa_daily.select_phase()` 同一套做法——Hermes 的 `--script` 不支持 argv，
    cron 靠 `--workdir ~/.hermes/state/maa/{am,pm}` 传场次。
    """
    if len(argv) == 1 and argv[0] in PHASE_TITLES:
        return argv[0]
    if argv:
        raise ValueError("计划卡场次只接受 am 或 pm")
    marker = (cwd or Path.cwd()).name.lower()
    if marker in PHASE_TITLES:
        return marker
    return "am" if (at or now_beijing()).hour < 14 else "pm"


def activity_deadlines(
    path: Path | None = None,
) -> list[tuple[str, datetime]]:
    """当前活动的 `(名字, 截止时刻)`，时刻已换算成北京时间。

    数据来自 MaaCore 的 `StageActivityV2.json`（地基事实 E）：字段名带 `Utc`，
    但实际是 `TimeZone` 那个时区的本地时间，maa-cli 源码的注释也这么说。
    **文件不存在 / 解析失败 / 字段缺失一律返回空表，绝不抛**——
    卡片不该因为一条旁路信息整张消失（D-2 的教训）。
    """
    try:
        raw = load_json(path or DEFAULT_STAGE_ACTIVITY_PATH, None)
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    side_stories = (raw.get("Official") or {}).get("sideStoryStage")
    if not isinstance(side_stories, dict):
        return []
    result: list[tuple[str, datetime]] = []
    for prefix, entry in side_stories.items():
        activity = entry.get("Activity") if isinstance(entry, dict) else None
        if not isinstance(activity, dict):
            continue
        try:
            offset = timezone(timedelta(hours=int(activity.get("TimeZone", 8))))
            expire = datetime.strptime(
                str(activity["UtcExpireTime"]), "%Y/%m/%d %H:%M:%S"
            ).replace(tzinfo=offset)
        except (KeyError, TypeError, ValueError):
            continue
        title = str(
            activity.get("Tip") or activity.get("StageName") or prefix
        )[:120]
        result.append((title, expire.astimezone(BEIJING)))
    result.sort(key=lambda item: item[1])
    return result


def activity_deadline_text(
    config: dict[str, Any], at: datetime | None = None
) -> str:
    """计划卡 / 抄作业卡的「活动截止」一行；读不出来就是空串（调用方不显示这行）。"""
    configured = str(config.get("maa_activity_cache") or "").strip()
    moment = at or now_beijing()
    lines: list[str] = []
    for title, expire in activity_deadlines(
        Path(configured) if configured else None
    ):
        if expire <= moment:
            continue
        left = (expire - moment).total_seconds() / 86400
        lines.append(f"{title} · 截止 {expire:%m-%d %H:%M}（还剩 {left:.1f} 天）")
    return "\n".join(lines)[:1024]


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
    phase: str,
) -> dict[str, Any]:
    # custom_id 带场次：万一旧卡的组件没收干净（`retire_old_plan_cards()` 是尽力而为），
    # 点它只会得到「未登记」，而不会把早场的选择悄悄写进晚场。
    custom_id = f"{BUTTON_PREFIX}{phase}:stage-select"
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
        # 场次写死在登记表里，不从 Discord 直通（不变量 #8）。
        select_args[value] = ["plan", "set-stage", stage, phase]
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
        "placeholder": f"🎯 选择{PHASE_TITLES[phase]}要刷的关卡",
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
    """`{关卡代号: {"materials": [主掉落材料名], "ap": 理智, "stage_id": 内部 id}}`。

    **这是「新活动自动跟随」的地基**：活动关的材料映射不再靠手工表——上游每次热更新
    带来新活动，`stages.json` 里就有它每一关的 `dropInfos`，下一张计划卡自动认识。

    只取 `NORMAL_DROP`（主掉落）。`EXTRA_DROP` 是副掉落（`1-7` 有 8 个），把它们算进来
    会让「刷这关补这个材料」失真——按副掉落去定关，期望产出可能差一个数量级。
    `FURNITURE` / `SPECIAL_DROP` 同理排除。

    资源本（`CE-6` / `AP-5` 这些）在 `stages.json` 里**只有 `FURNITURE`**，龙门币和红票
    不是以掉落建模的，所以它们的 `materials` 是空表——它们继续由 `farming_stage_table`
    手工表覆盖，两张表是**合并**关系，派生表不能替换手工表。

    **没有主掉落的关也留在表里**（`materials: []`）：`maa_farming` 要靠这张表把
    「关卡代号 → 内部 stageId / apCost」对上一图流与企鹅的掉落矩阵，
    而资源本、EX 关这些恰恰是掉落表里查不到的那批。

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
            try:
                ap = int(entry.get("apCost") or 0)
            except (TypeError, ValueError):
                ap = 0
            table[code] = {
                "materials": materials,
                "ap": ap,
                "stage_id": str(entry.get("stageId") or ""),
            }
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


def farming_recommendations(
    config: dict[str, Any],
    depot_state: dict[str, Any],
    available_stages: set[str] | None = None,
    at: datetime | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """三项加权分推荐表。**算法整个在 `maa_farming`**，这里只是渲染层的入口。

    四期步骤 3 之前这里是「可用天数 = 库存 / 日均消耗」单指标排序；现在换成
    稀缺（复刻窗口）/ 缺口（理智当量）/ 效率（件每理智）三项加权，数据来自
    一图流与企鹅物流。`available_stages` 传 `None` 时由 `maa_farming` 自己按 D5
    算候选集（活动期只有活动关）。
    """
    return maa_farming.farming_result(
        config,
        depot_state,
        available_stages=available_stages,
        at=at,
        data=data,
    )


def farming_note(result: dict[str, Any]) -> str:
    """计划卡上的一句话说明：前三名的三项分、降级原因、数据来源。"""
    return maa_farming.farming_note(result)


def auto_stage(result: dict[str, Any]) -> tuple[str, str]:
    """今天自动定的关 + 一句为什么；定不下来返回 `("", 原因)`。"""
    return maa_farming.auto_stage(result)


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
    """今天的材料推荐。

    候选集**不再**是下拉的可选集：D5 规定活动期间理智 all in 活动关，
    所以由 `maa_farming.candidate_stages()` 说了算（活动期只有活动关，
    否则 `1-7` + 当天开放资源本）。下拉仍然照旧全给——手动选资源本是允许的。
    """
    del plan
    return farming_recommendations(config, load_depot_state())


def build_components(
    plan: dict[str, Any],
    config: dict[str, Any],
    phase: str,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """恰好两行：一个关卡下拉 + 一个「跳过本场 / 恢复本场」按钮（D2）。

    抄作业选关搬到了聊天触发的独立卡（`maa_action.py copilot card`，D6）。
    """
    registry: dict[str, dict[str, Any]] = {}
    stage_select = _stage_select(plan, config, registry, phase)

    label, suffix, style, args = (
        ("▶️ 恢复本场", "unskip", 3, ["plan", "skip-today", "off", phase])
        if plan.get("skip")
        else (
            f"⏭️ 跳过本场（{PHASE_TITLES[phase]}）",
            "skip",
            4,
            ["plan", "skip-today", "on", phase],
        )
    )
    custom_id = f"{BUTTON_PREFIX}{phase}:{suffix}"
    registry[custom_id] = {
        "kind": "exec",
        "script": "maa_action.py",
        "args": args,
    }
    rows: list[dict[str, Any]] = [
        {"type": 1, "components": [stage_select]},
        {"type": 1, "components": [_button(label, custom_id, style=style)]},
    ]
    return (rows, registry)


def apply_auto_stage(
    result: dict[str, Any], phase: str, at: datetime | None = None
) -> str:
    """把自动定的关写进当日 `plan.json` 的**这一场**。返回写入的关卡，没写返回空串。

    **绝不覆盖用户的手动选择**：只有这一场的覆盖里根本没有 `stage`、或者上一次
    写它的就是本函数（`stage_source == "auto"`）时才写。用户从下拉选过、
    或在聊天里说过「早场刷 X」之后，该场 `stage_source` 是 `discord`，
    当天这一场就不会再被自动改掉——另一场不受影响（D3）。

    每场卡各跑一次：卡片上写的关就是这一场真正会跑的关。
    次日 `plan.json` 按日期自然失效，重新自动算。

    连带写下这一场的 **`farming_drops`**（第一名材料的 `{itemId: 还差多少个}`，
    给 `daily.toml` 的 Fight `drops` 用）和 **`farming_fallback_stage`**（第二名的关，
    刷够上限之后接着刷它，理智不浪费）。用户手动选关时 `maa_action.set_stage()`
    会把这两个字段清掉——手动选的关不带 drops 上限。
    """
    stage, _why = auto_stage(result)
    if not stage:
        return ""
    drops = maa_farming.drops_cap(result)
    fallback = maa_farming.fallback_stage(result)
    moment = at or now_beijing()
    # 判据是 `stage_source` 而不是 `source`：`source` 记的是「最后一次写这份覆盖的是谁」，
    # 点一次「跳过」也会把它写成 discord，拿它当闸门等于点跳过就冻住当天的自动定关。
    with state_lock():
        current = load_json(PLAN_PATH, {})
        if (
            not isinstance(current, dict)
            or current.get("date") != moment.date().isoformat()
            or not isinstance(current.get("sessions"), dict)
        ):
            current = {"date": moment.date().isoformat(), "sessions": {}}
        sessions = current["sessions"]
        session = sessions.get(phase)
        session = dict(session) if isinstance(session, dict) else {}
        if "stage" in session and session.get("stage_source") != "auto":
            return ""
        session["stage"] = stage
        session["stage_source"] = "auto"
        session["farming_drops"] = drops
        session["farming_fallback_stage"] = fallback
        session["updated_at"] = moment.isoformat()
        sessions[phase] = session
        current["updated_at"] = moment.isoformat()
        atomic_write_json(PLAN_PATH, current)
    return stage


def today_stage_source(phase: str, at: datetime | None = None) -> str:
    """这一场今天的关是谁写的：`auto`（缺口算的）/ `discord`（你选的）/ 空。"""
    return str(plan_sessions(at).get(phase, {}).get("stage_source") or "")


def build_embed(
    plan: dict[str, Any],
    config: dict[str, Any],
    phase: str,
    update: dict[str, Any] | None = None,
    farming: dict[str, Any] | None = None,
    stage_source: str = "",
    at: datetime | None = None,
) -> dict[str, Any]:
    activity_stages = config.get("activity_stages", [])
    activity_note = "、".join(str(item) for item in activity_stages) or "未配置"
    # skip 只作用于**本场**；今天的另一场和手动开跑都不受影响（D2）。
    skip = "是（另一场与手动开跑不受影响）" if plan.get("skip") else "否"
    # 「刷什么」要能一眼看出这关是自动定的还是自己选的。归属只认
    # `stage_source`（`today_stage_source()` 按场次从 plan.json 读），不拿「今天算出来
    # 的关恰好等于计划里的关」当依据：手选的关和缺口算的关撞上时那会说成自动选的。
    stage_line = plan_stage_label(plan)
    if stage_source == "auto":
        stage_line = f"{stage_line}（按材料缺口自动选）"
    elif stage_source == "discord":
        stage_line = f"{stage_line}（你手动选的）"
    fields: list[dict[str, Any]] = [
        {"name": "刷什么", "value": stage_line, "inline": True},
    ]
    if farming is not None:
        auto, why = auto_stage(farming)
        lines = [f"{'✅ 本场自动定为 ' + auto if auto else '⏸️ 本场没有自动改关'}：{why}"]
        cap = maa_farming.drops_cap(farming)
        spare = maa_farming.fallback_stage(farming)
        if cap and auto:
            quantity = next(iter(cap.values()))
            lines.append(
                f"🎯 刷够 {quantity} 个就停"
                + (f"，剩下的理智去 {spare}" if spare else "")
            )
        note = farming_note(farming)
        if note:
            lines.append(note)
        fields.append(
            {
                "name": "为什么" + ("（降级）" if farming.get("degraded") else ""),
                "value": "\n".join(lines)[:1024],
                "inline": False,
            }
        )
    fields.append({"name": "本场跳过", "value": skip, "inline": True})
    fields.append(
        {"name": "当前活动关", "value": activity_note[:1024], "inline": False}
    )
    deadline = activity_deadline_text(config, at)
    if deadline:
        # 读不出来就整行不显示——这是旁路信息，不值得占一行说「未知」。
        fields.append({"name": "活动截止", "value": deadline, "inline": False})
    fields.append(
        {
            "name": "🧪 理智规则",
            "value": medicine_label(str(plan.get("medicine", "none"))),
            "inline": False,
        }
    )
    fields.append(
        {
            "name": "每日剿灭",
            "value": "当天第一场用代理卡全权委托；满 1800 合成玉/周后自动停，当天不再进第二次",
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
        "title": f"{CARD_TITLE_PREFIX} · {PHASE_TITLES[phase]}",
        "description": "从下拉菜单选择本场要刷的关卡；今天的覆盖会在明天自动失效。",
        "color": 0x57F287,
        "fields": fields,
        "footer": {
            "text": "体力不足时停止 · 碎石永久关闭 · 计划变更不会打断已开始的场次"
        },
        "timestamp": (at or now_beijing()).isoformat(),
    }


# ---------------------------------------------------------------------------
# 抄作业卡（四期 1c）：聊天说「抄作业」→ 一览 + 一个下拉，选中即去搜作业
# ---------------------------------------------------------------------------

COPILOT_CARD_TITLE = "🤖 抄作业：现在能抄的关"


def _scheduled_stages() -> dict[str, str]:
    """当前这一轮已排程的 `关卡 → 预计开始时刻`（读不出来一律空表，绝不抛）。"""
    watch = load_json(WATCH_PATH, {})
    token = str(watch.get("batch_token") or "") if isinstance(watch, dict) else ""
    if not token:
        return {}
    search = load_json(maa_copilot.SEARCHES_DIR / f"{token}.json", None)
    if not isinstance(search, dict) or (
        search.get("status") not in maa_copilot.LIVE_ROUND_STATUSES
    ):
        return {}
    when = str(search.get("scheduled_at") or "")
    try:
        label = f"{datetime.fromisoformat(when):%H:%M}"
    except (TypeError, ValueError):
        label = "待发"
    return {
        str(entry.get("stage") or ""): label
        for entry in (search.get("entries") or [])
        if isinstance(entry, dict)
    }


def copilot_card_stages(
    config: dict[str, Any], at: datetime | None = None
) -> tuple[list[dict[str, Any]], bool]:
    """`([{stage, code, materials, nav, reason, status, status_text}], 是不是活动期)`。

    候选集就是自动路径的目标集合（四期 2b）：**当期活动前缀在本地关卡索引里的
    全部关**（不只是 `maa activity` 推荐的那 4 关），外加已通关的关——手动路径
    允许重打（D14）。没有活动时退化成**当天开放的资源本 + `1-7`**。

    **只读本地**：关卡索引、上游 `tasks/Stages/*.json` 的导航键、`stages.json`
    的主掉落、`copilot/ledger.json`、`copilot/roster.json`、`activity_watch.json`。
    零网络、零子进程，整条按钮路径必须 < 20s（不变量 #6）。
    """
    import maa_clearing

    index = maa_copilot.load_stage_index(config)
    nav_of = maa_clearing.nav_checker(maa_copilot._resource_dirs(config))
    drops = stage_drops(config)
    watch = load_json(WATCH_PATH, {})
    activity_key = str(watch.get("activity_key") or "") if isinstance(watch, dict) else ""
    prefix = maa_copilot._activity_prefix(activity_key)
    stage_state = watch.get("stage_state") if isinstance(watch, dict) else {}
    if not isinstance(stage_state, dict):
        stage_state = {}
    ledger = maa_copilot.load_ledger() or {}
    roster = maa_copilot.load_roster()
    scheduled = _scheduled_stages()

    candidates = maa_copilot.activity_stage_codes(index, prefix) if prefix else []
    in_activity = bool(candidates)
    if not candidates:
        candidates = [
            stage for _label, stage, _desc in current_supply_choices(config, at)
        ] + ["1-7"]

    rows: list[dict[str, Any]] = []
    for stage in candidates:
        resolved = maa_copilot.resolve_copilot_stage(stage, index)
        supported, detail = nav_of(stage)
        if not supported:
            # 上游没写导航定义的关不自动进、也不给下拉（D8 / 不变量 #37）。
            status, text = "no_nav", f"⛔ {detail or '上游无导航'}"
        elif resolved is None:
            # 有导航键但本地 Tile-Pos 索引里没有 → `copilot find` 那步会直接拒，
            # 与其发一个点了必失败的选项，不如在一览里说清楚。
            status, text = "not_indexed", "⛔ 不在本地关卡索引里"
        else:
            pending = maa_copilot.ledger_pending_modes(
                ledger,
                roster,
                stage,
                resolved[0],
                maa_clearing.required_modes(stage, prefix),
            )
            failure = stage_state.get(stage) if isinstance(stage_state, dict) else None
            gaps = list((failure or {}).get("gaps") or []) if isinstance(failure, dict) else []
            capped = maa_copilot.daily_capped_modes(ledger, stage, pending, config, at)
            if not pending:
                status, text = "cleared", "✅ 已通"
            elif stage in scheduled:
                status, text = "scheduled", f"⏳ 排程 {scheduled[stage]}"
            elif capped:
                # 当日上限只挡**自动**排轮；这一关照旧留在下拉里，手动想重打就重打（D14）。
                status, text = (
                    "daily_cap",
                    f"🚫 今日已尝试 {max(used for _mode, used in capped)} 次",
                )
            elif pending == ["raid"]:
                status, text = "raid_pending", "🔁 普通已通、突袭待补"
            elif gaps:
                status, text = "gap", f"🚫 练度不满足（缺：{'；'.join(gaps[:2])}）"
            elif isinstance(failure, dict) and failure.get("last_error"):
                status, text = "gap", f"🚫 {str(failure['last_error'])[:60]}"
            else:
                # 单向启发：只在**能证明**这关锁着时开口，沉默不代表已解锁，
                # 所以它只是显示，永远不当闸门用（不变量 #29 的精神）。
                locked = maa_copilot.zone_progress_warning(stage, resolved[0], roster)
                status, text = (
                    ("locked", f"🔒 {locked[:80]}") if locked else ("todo", "🕐 待抄")
                )
        rows.append(
            {
                "stage": stage,
                "code": resolved[1] if resolved else "",
                "materials": (drops.get(stage) or {}).get("materials") or [],
                "nav": status not in {"no_nav", "not_indexed"},
                "reason": text if status in {"no_nav", "not_indexed"} else "",
                "status": status,
                "status_text": text,
            }
        )
    return rows, in_activity


def copilot_card_view(
    config: dict[str, Any], at: datetime | None = None
) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    """抄作业卡的 `(embed, [(标签, 关卡, 说明)])`。选项已按导航键过滤。"""
    rows, in_activity = copilot_card_stages(config, at)
    lines: list[str] = []
    options: list[tuple[str, str, str]] = []
    for row in rows:
        materials = "、".join(row["materials"])
        lines.append(
            f"`{row['stage']}`"
            + (f" · {materials}" if materials else "")
            + f" · {row['status_text']}"
        )
        if row["nav"]:
            label = f"{row['stage']} · {materials}" if materials else row["stage"]
            options.append((label, row["stage"], row["status_text"]))
    fields: list[dict[str, Any]] = [
        {
            "name": "当前活动关" if in_activity else "资源本 / 主线（当前无活动）",
            "value": ("\n".join(lines) or "没有可抄的关")[:1024],
            "inline": False,
        }
    ]
    deadline = activity_deadline_text(config, at)
    if deadline:
        fields.append({"name": "活动截止", "value": deadline, "inline": False})
    embed = {
        "title": COPILOT_CARD_TITLE,
        "description": (
            "自动路径会把上游有导航定义的关按理智排队抄完（普通 + 突袭），这里是它的进度。\n"
            "从下拉里选一关＝**立刻**去 prts.plus 搜作业，搜到就直接开跑；"
            "已通的关也能选，那是重打。上游没写导航定义的关不在下拉里，MAA 进不去。"
        ),
        "color": 0x5865F2,
        "fields": fields,
        "footer": {"text": "抄作业与日常互斥 · 08:00 / 22:00 前 25 分钟不接受"},
        "timestamp": (at or now_beijing()).isoformat(),
    }
    return embed, options[:25]


def retire_old_plan_cards(token: str, channel_id: str) -> dict[str, int]:
    """Remove controls from recent superseded plan cards.

    标题现在带场次后缀（`… · 早场 08:00`），所以按**前缀**匹配——
    21:50 的晚场卡要能收掉早上那张。
    """
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
            isinstance(embed, dict)
            and str(embed.get("title") or "").startswith(CARD_TITLE_PREFIX)
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


def main(argv: list[str] | None = None) -> int:
    phase = select_phase(list(argv or []))
    config = load_config()
    channel_id = str(config.get("channel_id", "")).strip()
    if not channel_id.isdigit():
        raise RuntimeError("config.json 中尚未配置 Discord channel_id")
    token = discord_token()
    # D1：只有早场卡跟随上游更新。晚场卡跑 brew / `maa update` 既没有意义
    # （早上刚跟过），又会在 21:50 引入一次几分钟的网络等待。
    update = (
        check_and_update_upstream(token, channel_id, config) if phase == "am" else None
    )
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
    # 就是**本场**真正会跑的关。用户手动选过的那一场当天不动（见 apply_auto_stage）。
    farming = current_farming_result(effective_plan(phase=phase), config)
    apply_auto_stage(farming, phase)
    plan = effective_plan(phase=phase)
    components, entries = build_components(plan, config, phase)

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
    embed = build_embed(
        plan, config, phase, update, farming, today_stage_source(phase)
    )
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
    if update is not None and not update.get("ok"):
        raise RuntimeError("maa 上游更新失败；已发送红色告警")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        print(f"MAA 计划卡失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
