#!/usr/bin/env python3
"""Run one complete MAA daily session and stream its progress to Discord."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

import maa_depot
from maa_common import (
    BEIJING,
    LAST_RUN_PATH,
    MAA_CONFIG_DIR,
    RUNS_DIR,
    SCRIPTS_DIR,
    atomic_write_json,
    atomic_write_text,
    cleanup_run_files,
    create_thread,
    discord_request,
    discord_token,
    discord_upload,
    effective_plan,
    load_config,
    load_json,
    maa_subprocess_env,
    medicine_label,
    merge_last_run,
    now_beijing,
    plan_stage_label,
    require_executable,
    run_lock,
)


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SAFE_APP_RE = re.compile(r"^[A-Za-z0-9 ._+-]{1,80}$")
SAFE_BUNDLE_ID_RE = re.compile(r"^[A-Za-z0-9.-]{1,128}$")
PHASE_LABELS = {"am": "早", "pm": "晚", "now": "手动"}
TASK_STEPS = (
    ("StartUp", "🚀", "登录与首登领取", "正在登录并处理首登弹窗"),
    ("Annihilation", "☄️", "每日剿灭", "正在用代理卡全权委托清剿灭"),
    ("Fight", "⚔️", "刷理智", "正在刷理智"),
    ("Infrast", "🏭", "基建换班", "正在根据干员心情自动排班"),
    ("Recruit", "🎫", "公开招募", "正在处理公开招募"),
    ("Mall", "🛒", "信用商店", "正在领取信用并购买物资"),
    ("Award", "🎁", "领取奖励", "正在领取日常、周常与全部邮件奖励"),
    ("Custom", "🏠", "返回主界面", "正在返回游戏主界面，准备截图"),
)

# MAA 的 QuickSwitch 是子程序：点完小房子里的首页图标后，只能靠 next 里的 `#back`
# 回到调用方来确认成功，而 `#back` 取的是任务名第一个 `@` 之前的前缀。裸写
# "QuickSwitch@ToHome" 时前缀是并不存在的 "QuickSwitch"，`#back` 被静默丢弃
# （日志里 to_be_recognized 只剩 4 项，json 里是 5 项），于是画面已经回到主页却没有
# 任何任务能判定成功，空转到 20 次重试耗尽报 Custom Error——2026-08-02、08-03 两个
# 早场都是这样红的。加上 "Home@" 前缀后 `#back` → "Home"（action = Stop），认出主页
# 立即成功收尾。这里手工展开 MAA 自己用的
# ["Home@QuickSwitch@ToHome#next", "Home@ReturnButtons#next"]，因为顶层 task_names 是
# 原样交给 ProcessTask 的，不走 next 列表那套 `#` 展开。
HOME_TASK_NAMES = [
    "Home",
    "Home@QuickSwitch@ToHome@Entry",
    "Home@QuickSwitch@ToHome@Open",
    "Home@ReturnButton",
    "Home@FromStageSN",
    "Home@FromAnnihilation",
]
# 主页面独有的界面文案，用本机 Vision OCR（ocrbox）核对截图确实停在主页。
# 只取在实机截图里稳定读出的词：任务列表页也有「好友」，所以单个命中不算数。
HOME_MARKERS = ("终端", "编队", "采购中心", "公开招募", "基建", "好友", "档案", "SANITY")
HOME_MARKER_MIN = 4

# 代理指挥失误时游戏结算页写「本场战斗已视为单次作战结算，未进行作战的理智已返还」，
# 并把返还的理智画成一个道具图标。MaaCore 照单全收，于是日志里出现
# `Drops: 理智 × 105`、汇总成 `total drops: 理智 × 315`。理智永远不可能是关卡掉落，
# 所以掉落项里出现「理智」= 这一批代理失败、理智原路退回、一关没打成。
# 2026-08-07 早场 TO-9 连着 4 批都栽在这里，MaaCore 仍判 Fight Completed，
# 战报因此写成「✅ 完成（16 次；掉落 理智 ×315）」——见 DEV_GUIDE §11.13。
SANITY_REFUND_ITEM = "理智"
# maa run 被熔断中止时的返回码，跟超时的 124、人工中止的 130 区分开。
PROXY_BREAK_CODE = 125
# MaaCore 连接阶段就失败的标记。MaaTouch 的握手要 `app_process` 现开一个 ART 虚拟机，
# 而 MaaCore 给这个握手的窗口写死 3 秒；模拟器冷启后头几十秒 system_server 还在忙，
# 偶发就是卡过 3 秒 → 报 TouchModeNotAvailable、connect 返回失败。偏偏 maa-cli 不看
# 这个返回值，照样 Assistant::start()，StartUpTask 拿着没连上的 controller 直接
# SIGSEGV，maa run 秒退 1。此时一个键都没点过，重开一趟是安全的。见 DEV_GUIDE §11.14。
CONNECT_FAILURE_MARKERS = ("TouchModeNotAvailable", "ConnectFailed", "Connect failed")
# 这几个返回码是本脚本自己写上去的，不是「没连上」——重开一趟只会重复同一件事。
SELF_ASSIGNED_CODES = (124, PROXY_BREAK_CODE, 130)
# 进度消息的编辑节流，同时也是熔断的采样间隔——扫一遍全部日志行不便宜，
# 不能放进 0.25 秒的主循环里跑。
PROGRESS_INTERVAL_SECONDS = 5
# maa run 的最短存活时间：配置写歪了也不能让整条链条秒退。
MIN_RUN_TIMEOUT_SECONDS = 60
# 国服日刷新 04:00。剿灭按游戏日只进一次，避免早晚场各刷一轮。
CN_GAME_DAY_RESET = timedelta(hours=4)
# 周合成玉上限由 MaaCore 识别（待办消失 / 代理卡不可用）后停，这里只用于战报文案。
ANNIHILATION_WEEKLY_ORUNDUM = 1800
# maa-cli 最终汇总用任务名；旧日志仍是「每周剿灭」。
ANNIHILATION_TASK_NAMES = ("每日剿灭", "每周剿灭")


def select_phase(argv: list[str], at: datetime | None = None, cwd: Path | None = None) -> str:
    if len(argv) == 2 and argv[1] in PHASE_LABELS:
        return argv[1]
    if len(argv) > 1:
        raise ValueError("场次只接受 am、pm 或 now")
    marker = (cwd or Path.cwd()).name.lower()
    if marker in {"am", "pm"}:
        return marker
    hour = (at or now_beijing()).hour
    if 7 <= hour < 14:
        return "am"
    if 20 <= hour or hour < 2:
        return "pm"
    return "now"


def cn_game_day(at: datetime | None = None) -> str:
    """Official-server calendar date: resets at 04:00 Beijing time."""
    moment = at or now_beijing()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BEIJING)
    return (moment.astimezone(BEIJING) - CN_GAME_DAY_RESET).date().isoformat()


def should_run_annihilation(
    plan: dict[str, Any],
    at: datetime | None = None,
    last_run: dict[str, Any] | None = None,
) -> bool:
    """Include annihilation in this run: fight is on, and not yet done this game day."""
    if not bool(plan.get("tasks", {}).get("fight", True)):
        return False
    state = last_run if isinstance(last_run, dict) else {}
    return str(state.get("annihilation_game_day") or "") != cn_game_day(at)


def attach_annihilation(
    plan: dict[str, Any],
    at: datetime | None = None,
    last_run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a per-run plan copy with the once-per-game-day annihilation flag."""
    return {**plan, "annihilation": should_run_annihilation(plan, at, last_run)}


def annihilation_consumed(lines: list[str]) -> bool:
    """True when today's annihilation slot was used and must not run again today.

    Named maa-cli summaries and Annihilation Start are the hard signals. If
    StartUp already completed, the next task in the rendered chain is
    annihilation, so later sessions in the same game day must not retry it.
    """
    if any(f"[{name}]" in line for line in lines for name in ANNIHILATION_TASK_NAMES):
        return True
    if any("Annihilation Start" in line or "Fight Annihilation" in line for line in lines):
        return True
    return _task_state(lines, "StartUp", "登录与首登领取") == "completed"


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_daily_toml(plan: dict[str, Any], config: dict[str, Any]) -> str:
    """Render the exact immutable plan used for this run; no CloseDown task."""
    tasks = plan.get("tasks", {})
    blocks = [
        "[[tasks]]\n"
        'name = "启动国服"\n'
        'type = "StartUp"\n'
        "[tasks.params]\n"
        'client_type = "Official"\n'
        "start_game_enabled = true\n"
    ]
    if tasks.get("fight", True):
        # Fixed safety policy: never use ordinary medicine or Originite Prime;
        # only consume medicine MaaCore identifies as expiring soon.
        medicine_days = max(1, int(config.get("medicine_expire_days", 2)))
        # Default True so the deploy seed and isolated renderer tests still
        # include annihilation. execute() sets the flag from last_run.
        if plan.get("annihilation", True):
            # Fight(Annihilation) turns on 全权委托 and consumes a PRTS
            # 代理指挥卡. MaaCore stops when the weekly 1800 orundum cap is
            # reached (todo gone / cards refused).
            blocks.append(
                "[[tasks]]\n"
                'name = "每日剿灭"\n'
                'type = "Fight"\n'
                "[tasks.params]\n"
                'stage = "Annihilation"\n'
                "medicine = 0\n"
                f"medicine_expire_days = {max(0, medicine_days)}\n"
                "stone = 0\n"
                "series = 0\n"
                f"times = {int(config.get('fight_times', 2147483647))}\n"
                'server = "CN"\n'
                'client_type = "Official"\n'
            )
        blocks.append(
            "[[tasks]]\n"
            'name = "刷理智"\n'
            'type = "Fight"\n'
            "[tasks.params]\n"
            f"stage = {_toml_string(str(plan.get('stage', '')))}\n"
            "medicine = 0\n"
            f"medicine_expire_days = {max(0, medicine_days)}\n"
            # This literal is not derived from any state or user input.
            "stone = 0\n"
            # Let MaaCore select the largest series currently affordable in
            # the connected client instead of forcing a fixed multiplier.
            "series = 0\n"
            f"times = {int(config.get('fight_times', 2147483647))}\n"
            'server = "CN"\n'
            'client_type = "Official"\n'
        )
    if tasks.get("infrast", True):
        blocks.append(
            "[[tasks]]\n"
            'name = "基建换班"\n'
            'type = "Infrast"\n'
            "[tasks.params]\n"
            # Let MaaCore calculate efficient assignments from the operators
            # and morale currently visible in every shiftable base facility.
            "mode = 0\n"
            'facility = ["Mfg", "Trade", "Power", "Control", "Reception", "Office", "Dorm"]\n'
            'drones = "_NotUse"\n'
            "threshold = 0.3\n"
            "replenish = false\n"
            "dorm_trust_enabled = false\n"
        )
    if tasks.get("recruit", True):
        blocks.append(
            "[[tasks]]\n"
            'name = "公开招募"\n'
            'type = "Recruit"\n'
            "[tasks.params]\n"
            "refresh = true\n"
            "select = [4, 5, 6]\n"
            "confirm = [3, 4, 5, 6]\n"
            f"times = {int(config.get('recruit_times', 4))}\n"
            "set_time = true\n"
            "expedite = false\n"
            'server = "CN"\n'
        )
    if tasks.get("mall", True):
        blocks.append(
            "[[tasks]]\n"
            'name = "信用商店"\n'
            'type = "Mall"\n'
            "[tasks.params]\n"
            "visit_friends = true\n"
            "shopping = true\n"
            "force_shopping_if_credit_full = false\n"
            "credit_fight = false\n"
        )
    if tasks.get("award", True):
        blocks.append(
            "[[tasks]]\n"
            'name = "领取奖励"\n'
            'type = "Award"\n'
            "[tasks.params]\n"
            "award = true\n"
            "mail = true\n"
            "recruit = false\n"
            "orundum = false\n"
            "mining = false\n"
        )
    blocks.append(
        "[[tasks]]\n"
        'name = "返回主界面"\n'
        'type = "Custom"\n'
        "[tasks.params]\n"
        f"task_names = {json.dumps(HOME_TASK_NAMES, ensure_ascii=False)}\n"
    )
    return "\n".join(blocks)


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _clean_line(line: str, limit: int = 150) -> str:
    line = ANSI_RE.sub("", line).rstrip().replace("```", "``\u200b`")
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _task_display_names(key: str, label: str) -> tuple[str, ...]:
    if key == "Annihilation":
        return ANNIHILATION_TASK_NAMES
    return (label,)


def _task_state(lines: list[str], key: str, label: str) -> str:
    # maa-cli uses the configured task name in its final ``[name]`` summary.
    # Prefer that exact result when multiple tasks share one MaaCore type (the
    # daily annihilation and ordinary farming are both Fight tasks).
    names = _task_display_names(key, label)
    named = [line for line in lines if any(f"[{name}]" in line for name in names)]
    if named:
        if any("Completed" in line for line in named):
            return "completed"
        if any("Stopped" in line or "Error" in line for line in named):
            return "failed"
        return "running"
    if any(
        f"{key} Completed" in line
        or any(f"[{name}]" in line and "Completed" in line for name in names)
        for line in lines
    ):
        return "completed"
    if any(f"{key} Stopped" in line or f"{key} Error" in line for line in lines):
        return "failed"
    if any(f"{key} Start" in line or any(f"[{name}]" in line for name in names) for line in lines):
        return "running"
    return "pending"


def _last_match(lines: list[str], pattern: str) -> re.Match[str] | None:
    compiled = re.compile(pattern, re.IGNORECASE)
    result = None
    for line in lines:
        match = compiled.search(line)
        if match:
            result = match
    return result


def _named_task_section(lines: list[str], label: str) -> list[str]:
    """Return one maa-cli final-summary section, excluding later tasks."""
    start = next((index for index, line in enumerate(lines) if f"[{label}]" in line), None)
    if start is None:
        return []
    section: list[str] = []
    for line in lines[start:]:
        if section and re.match(r"^\[[^]]+\]", line):
            break
        section.append(line)
    return section


def _fight_times(lines: list[str]) -> int:
    # Final summaries normally include a stage code (``Fight CE-6 6 times``);
    # older fixture/log shapes omitted it.  Accept both before falling back to
    # streaming Mission-started events.
    summary = _last_match(lines, r"\bFight(?:\s+\S+)?\s+(\d+)\s+times\b")
    if summary:
        return int(summary.group(1))
    return sum(
        int(match.group(1))
        for line in lines
        if (match := re.search(r"Mission started\s*\((\d+)\s+times", line, re.I))
    )


def _expiring_medicine_used(lines: list[str]) -> int:
    summary = _last_match(lines, r"used\s+\d+\s+medicine\s*\((\d+)\s+expiring\)")
    if summary:
        return int(summary.group(1))
    return sum(
        int(match.group(1))
        for line in lines
        if (match := re.search(r"Use\s+(\d+)\s+expiring medicine", line, re.I))
    )


def _parse_drop_items(raw: str) -> list[tuple[str, int]]:
    """Parse one Drops payload into (item, quantity); JSON first, plain text as fallback."""
    parsed: Any = None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass
    pairs: list[tuple[str, int]] = []
    if isinstance(parsed, dict):
        pairs = [
            (str(name), int(quantity))
            for name, quantity in parsed.items()
            if isinstance(quantity, (int, float))
        ]
    elif isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            name = item.get("itemName") or item.get("name")
            quantity = item.get("quantity") or item.get("count")
            if name and isinstance(quantity, (int, float)):
                pairs.append((str(name), int(quantity)))
    if not pairs:
        pairs = [
            (name.strip(" \"'{}[]"), int(quantity))
            for name, quantity in re.findall(
                r"([A-Za-z一-鿿][A-Za-z0-9一-鿿· _-]{0,30})\s*(?:[:x×])\s*(\d+)",
                raw,
            )
        ]
    return [(name, quantity) for name, quantity in pairs if name and quantity]


def _drop_batches(
    lines: list[str],
) -> tuple[list[list[tuple[str, int]]], list[list[tuple[str, int]]]]:
    """Split Drops events into per-batch results and maa-cli's final total lines."""
    batches: list[list[tuple[str, int]]] = []
    totals: list[list[tuple[str, int]]] = []
    for line in lines:
        marker = re.search(r"(?:total\s+)?Drops:\s*(.+)$", line, re.I)
        if not marker:
            continue
        items = _parse_drop_items(marker.group(1).strip())
        if not items:
            continue
        if re.search(r"total\s+Drops:", line, re.I):
            totals.append(items)
        else:
            batches.append(items)
    return batches, totals


def _sanity_refund(items: list[tuple[str, int]]) -> int:
    return sum(quantity for name, quantity in items if name == SANITY_REFUND_ITEM)


def _drop_pairs(lines: list[str]) -> list[tuple[str, int]]:
    batches, totals = _drop_batches(lines)
    aggregate: dict[str, int] = {}
    # maa-cli can emit per-batch Drops events and one final total. Prefer the
    # latter when present so a result is never double-counted.
    for items in totals or batches:
        for name, quantity in items:
            # 返还的理智不是掉落，见 SANITY_REFUND_ITEM。
            if name == SANITY_REFUND_ITEM:
                continue
            aggregate[name] = aggregate.get(name, 0) + quantity
    return list(aggregate.items())[:8]


def proxy_failures(lines: list[str]) -> tuple[int, int, int]:
    """代理指挥失误统计：(失误批次, 累计返还理智, 末尾连续失误批次)。

    「末尾连续」供熔断使用——有真掉落的批次会把连续计数清零，只有一直失败才中止。
    """
    batches, totals = _drop_batches(lines)
    if not batches:
        # 没有逐批事件（只拿到 maa-cli 汇总行）时退回汇总；批次数不可知，按 1 计。
        refunded = sum(_sanity_refund(items) for items in totals)
        return (1, refunded, 1) if refunded else (0, 0, 0)
    failed = 0
    refunded = 0
    streak = 0
    for items in batches:
        batch_refund = _sanity_refund(items)
        if batch_refund:
            failed += 1
            refunded += batch_refund
            streak += 1
        else:
            streak = 0
    return failed, refunded, streak


def fight_wiped_out(lines: list[str]) -> tuple[int, int]:
    """全部批次都是代理失误（一关没打成）时返回 (批次, 返还理智)，否则 (0, 0)。"""
    failed, refunded, _streak = proxy_failures(lines)
    if failed and not _drop_pairs(lines):
        return failed, refunded
    return 0, 0


def connect_failed(lines: list[str], return_code: int) -> str:
    """连接阶段就死了（一个键都没点过）时返回命中的标记，否则空串。

    三个条件缺一不可：非零且不是我们自己写的返回码、日志里有连接失败标记、没有
    任何一条任务链跑到 Completed。第三条是安全闸门——只要有任务真跑过，就不能拿
    「重开一趟」当补救，那会重复已经做完的事。
    """
    if return_code == 0 or return_code in SELF_ASSIGNED_CODES:
        return ""
    task_keys = tuple(key for key, *_ in TASK_STEPS)
    for line in lines:
        if "AllTasksCompleted" in line or any(f"{key} Completed" in line for key in task_keys):
            return ""
    for line in lines:
        for marker in CONNECT_FAILURE_MARKERS:
            if marker in line:
                return marker
    return ""


def progress_content(
    lines: list[str],
    *,
    plan: dict[str, Any] | None = None,
    final: str | None = None,
) -> str:
    clean = [_clean_line(line, 300) for line in lines if line.strip()]
    heading = final or "🌱 MAA 正在执行"
    status_lines: list[str] = []
    for key, emoji, label, running_text in TASK_STEPS:
        state = _task_state(clean, key, label)
        if state == "completed":
            status_lines.append(f"✅ {emoji} {label}：完成")
        elif state == "failed":
            status_lines.append(f"❌ {emoji} {label}：未完成")
        elif state == "running":
            detail = running_text
            if key == "Fight" and plan is not None:
                detail = f"正在刷 {plan_stage_label(plan)}，自动选择最大连战次数"
            status_lines.append(f"⏳ {emoji} {detail}")

    sanity = _last_match(clean, r"Current sanity:\s*(\d+)\s*/\s*(\d+)")
    if sanity and _task_state(clean, "Fight", "刷理智") != "pending":
        status_lines.append(f"🧠 当前理智：{sanity.group(1)}/{sanity.group(2)}")
    proxy_batches, refunded, _streak = proxy_failures(clean)
    fight_times = _fight_times(clean)
    if fight_times and not fight_wiped_out(clean)[0]:
        status_lines.append(f"🔁 已完成作战：{fight_times} 次")
    if proxy_batches:
        status_lines.append(f"⚠️ 代理指挥失误 {proxy_batches} 批，理智已返还 ×{refunded}")
    refresh = _last_match(clean, r"RecruitTagsRefreshed:\s*(\d+)\s+times")
    if refresh:
        status_lines.append(f"🔄 公招已刷新：{refresh.group(1)} 次")
    if not status_lines:
        status_lines.append("⏳ 正在连接模拟器并准备任务…")
    return (heading + "\n" + "\n".join(status_lines))[:1990]


def extract_summary(
    stdout_lines: list[str],
    stderr_lines: list[str],
    *,
    plan: dict[str, Any] | None = None,
    ok: bool | None = None,
    home_verified: bool | None = None,
    home_verify_detail: str = "",
) -> str:
    lines = [_clean_line(line, 500) for line in stdout_lines + stderr_lines if line.strip()]
    tasks = (plan or {}).get("tasks", {})
    annihilation_in_logs = any(
        f"[{name}]" in line for line in lines for name in ANNIHILATION_TASK_NAMES
    )
    annihilation_planned = (plan or {}).get("annihilation")
    enabled = {
        "StartUp": True,
        # Show the step when this run included it, or when older logs still
        # have the named summary.  A False flag means "already done today".
        "Annihilation": annihilation_in_logs or annihilation_planned is True,
        "Fight": bool(tasks.get("fight", True)),
        "Infrast": bool(tasks.get("infrast", True)),
        "Recruit": bool(tasks.get("recruit", True)),
        "Mall": bool(tasks.get("mall", True)),
        "Award": bool(tasks.get("award", True)),
        "Custom": True,
    }
    result = [f"🌟 总体：{'✅ 成功' if ok is not False else '❌ 未完成'}"]
    for key, emoji, label, _running_text in TASK_STEPS:
        if not enabled[key]:
            if key == "Annihilation" and annihilation_planned is False:
                result.append(f"{emoji} {label}：⏭️ 今日已刷过，本场跳过")
            continue
        state = _task_state(lines, key, label)
        status = {"completed": "✅ 完成", "failed": "❌ 未完成", "running": "❌ 中断", "pending": "⏸️ 未执行"}[state]
        details: list[str] = []
        if key == "StartUp" and state != "completed":
            # 「启动游戏 ❌ 中断」看着像游戏那头的事，其实多半是根本没连上模拟器。
            # 把 MaaCore 的原始标记摊开，省得每次都去翻 asst.log。见 §11.14。
            marker = next(
                (m for m in CONNECT_FAILURE_MARKERS if any(m in line for line in lines)), ""
            )
            if marker:
                details.append(f"没连上模拟器（{marker}），游戏里一个键都没点")
        elif key == "Annihilation":
            annihilation_lines = []
            for name in ANNIHILATION_TASK_NAMES:
                annihilation_lines = _named_task_section(lines, name)
                if annihilation_lines:
                    break
            annihilation_lines = annihilation_lines or lines
            count = _fight_times(annihilation_lines)
            details.append(
                f"当期剿灭 {count} 次；代理卡全权委托；满 {ANNIHILATION_WEEKLY_ORUNDUM} 合成玉/周后停止"
                if count
                else f"代理卡全权委托；满 {ANNIHILATION_WEEKLY_ORUNDUM} 合成玉/周后停止"
            )
        elif key == "Fight":
            fight_lines = _named_task_section(lines, "刷理智") or lines
            details.append(f"关卡 {plan_stage_label(plan or {})}")
            proxy_batches, refunded, _streak = proxy_failures(fight_lines)
            wiped_batches, _wiped_refund = fight_wiped_out(fight_lines)
            count = _fight_times(fight_lines)
            if wiped_batches:
                # MaaCore 会把「一批都没打成」也判成 Fight Completed，而 count 数的是
                # 发起的连战批次，不是真打完的次数。这里直接翻红，别让战报骗人。
                status = "❌ 未完成"
                details.append("实际完成 0 次")
            elif count:
                details.append(f"{count} 次")
            if proxy_batches:
                details.append(f"代理指挥失误 {proxy_batches} 批，理智已返还 ×{refunded}")
            medicine = _expiring_medicine_used(fight_lines)
            if medicine:
                details.append(f"临期理智药 ×{medicine}")
            sanity = _last_match(lines, r"Current sanity:\s*(\d+)\s*/\s*(\d+)")
            if sanity:
                details.append(f"结束理智 {sanity.group(1)}/{sanity.group(2)}")
            drops = _drop_pairs(fight_lines)
            if drops:
                details.append("掉落 " + "、".join(f"{name} ×{quantity}" for name, quantity in drops))
        elif key == "Infrast":
            facilities = []
            for raw_name, display in (
                ("Mfg", "制造站"),
                ("Trade", "贸易站"),
                ("Power", "发电站"),
                ("Control", "控制中枢"),
                ("Reception", "会客室"),
                ("Office", "办公室"),
                ("Dorm", "宿舍"),
            ):
                count = sum(1 for line in lines if f"EnterFacility {raw_name}" in line)
                if count:
                    facilities.append(f"{display} ×{count}")
            if facilities:
                details.append("、".join(facilities))
        elif key == "Recruit":
            recruited = _last_match(lines, r"Recruited\s+(\d+)\s+times")
            refreshed = _last_match(lines, r"Refreshed\s+(\d+)\s+times")
            if recruited:
                details.append(f"招募 {recruited.group(1)} 次")
            if refreshed:
                details.append(f"刷新 {refreshed.group(1)} 次")
        elif key == "Custom":
            # 截图才是最终交付物，所以它说了算：MAA 报错但截图确实在主页时说清楚，
            # 反之截图不在主页就把这一步判红，哪怕 MAA 自称完成。
            if home_verified is True:
                details.append("截图已确认在主页")
            elif home_verified is False:
                status = "❌ 未完成"
                details.append(f"截图不在主页：{home_verify_detail}" if home_verify_detail else "截图不在主页")
        suffix = f"（{'；'.join(details)}）" if details else ""
        result.append(f"{emoji} {label}：{status}{suffix}")
    return "\n".join(result)[:3900]


def _reader(
    stream: TextIO,
    recent: deque[str],
    all_lines: list[str],
    log_handle: TextIO | None = None,
    log_lock: threading.Lock | None = None,
) -> None:
    for line in iter(stream.readline, ""):
        clean = line.rstrip("\n")
        recent.append(clean)
        if len(all_lines) < 10000:
            all_lines.append(clean)
        if log_handle is not None and log_lock is not None:
            with log_lock:
                log_handle.write(line)
                log_handle.flush()
    stream.close()


def open_emulator(config: dict[str, Any]) -> None:
    app = str(config.get("emulator_app", "BlueStacks"))
    if not SAFE_APP_RE.fullmatch(app):
        raise RuntimeError("emulator_app 名称含非法字符")
    result = subprocess.run(["open", "-a", app], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"无法启动 {app}: {(result.stderr or result.stdout).strip()}")


def emulator_boot_diagnostic(config: dict[str, Any]) -> str:
    """Explain the BlueStacks host-open/Android-not-started failure mode.

    BlueStacks checks for host updates before it starts the Android VM.  When
    an update is available it opens a modal dialog and does not emit the usual
    ``[QTUI] Start Virtual Machine`` line until somebody closes that dialog.
    ``open -a`` still returns success, so ADB alone can only report a vague
    connection refusal.  Read only the current Player.log session and turn
    that otherwise invisible host-side state into an actionable error.
    """
    configured = str(config.get("emulator_player_log", "")).strip()
    player_log = Path(configured) if configured else Path(
        "/Users/Shared/Library/Application Support/BlueStacks/Logs/Player.log"
    )
    try:
        text = player_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    marker = "=================== xlogStart"
    current = text.rsplit(marker, 1)[-1]
    if "/bs3/check_upgrade" in current and "[QTUI] Start Virtual Machine" not in current:
        return (
            "BlueStacks 宿主已打开，但升级检查没有放行 Android 虚拟机；"
            "通常是“有可用更新”弹窗正在等待关闭"
        )
    return ""


def wait_for_android(config: dict[str, Any]) -> None:
    adb = str(config.get("adb_path", "/opt/homebrew/bin/adb"))
    address = str(config.get("adb_address", "127.0.0.1:5555"))
    timeout = int(config.get("emulator_boot_timeout", 180))
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            connect = subprocess.run(
                [adb, "connect", address],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            boot = subprocess.run(
                [adb, "-s", address, "shell", "getprop", "sys.boot_completed"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            last_error = "ADB 命令超时"
            time.sleep(3)
            continue
        if connect.returncode == 0 and boot.returncode == 0 and boot.stdout.strip() == "1":
            return
        last_error = (boot.stderr or connect.stderr or connect.stdout).strip()
        time.sleep(3)
    diagnostic = emulator_boot_diagnostic(config)
    detail = diagnostic or last_error or "设备未就绪"
    raise RuntimeError(f"ADB 等待 {timeout}s 超时：{detail}")


def settle_android_runtime(config: dict[str, Any]) -> str:
    """等 `app_process` 的冷启抖动过去，再把场子交给 MaaCore。

    `sys.boot_completed=1` 只说明系统起来了，不代表安静：BlueStacks 冷启后头几十秒
    system_server 还在装自家 launcher，这期间任何 `app_process` 调用都可能卡上几秒。
    MaaCore 起 MaaTouch 走的正是 `app_process`，而它只等 3 秒——撞上就判
    TouchModeNotAvailable（§11.14）。这里用同样走 `app_process` 的只读命令
    `am get-current-user` 探路，连着 quiet_seconds 秒都够快才放行。

    2026-08-08 实测：冷启后 `sys.boot_completed=1` 在 +7.5s，探针在 +0…+4.3s 全是
    35ms，+5.4s 突然卡了 2.8s，+9.2s 恢复——所以只看「连着几次快」不够，必须要求一段
    连续安静的窗口。探不出安静窗口也不阻断：连接重试兜底比在这儿空等值钱。
    """
    adb = str(config.get("adb_path", "/opt/homebrew/bin/adb"))
    address = str(config.get("adb_address", "127.0.0.1:5555"))
    budget = max(0, int(config.get("emulator_settle_timeout", 40)))
    quiet_needed = max(0.0, float(config.get("emulator_settle_quiet_seconds", 8)))
    slow = max(0.1, float(config.get("emulator_settle_slow_seconds", 1.0)))
    if not budget or not quiet_needed:
        return ""
    deadline = time.monotonic() + budget
    quiet_since = time.monotonic()
    slowest = 0.0
    while time.monotonic() < deadline:
        begin = time.monotonic()
        try:
            probe = subprocess.run(
                [adb, "-s", address, "shell", "am", "get-current-user"],
                capture_output=True,
                text=True,
                check=False,
                timeout=max(5, int(slow * 5)),
            )
            responsive = probe.returncode == 0
        except subprocess.TimeoutExpired:
            responsive = False
        cost = time.monotonic() - begin
        slowest = max(slowest, cost)
        if not responsive or cost >= slow:
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since >= quiet_needed:
            return ""
        time.sleep(0.5)
    return f"模拟器 app_process 未在 {budget}s 内稳定下来（最慢 {slowest:.1f}s），照常开跑。"


def verify_game_installed(config: dict[str, Any]) -> None:
    adb = str(config.get("adb_path", "/opt/homebrew/bin/adb"))
    address = str(config.get("adb_address", "127.0.0.1:5555"))
    package = str(config.get("game_package", "com.hypergryph.arknights")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.]{3,160}", package):
        raise RuntimeError("game_package 格式非法")
    result = subprocess.run(
        [adb, "-s", address, "shell", "pm", "path", package],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if result.returncode != 0 or not result.stdout.strip().startswith("package:"):
        raise RuntimeError(f"未检测到明日方舟国服客户端（{package}）")


def capture_screenshot(config: dict[str, Any], path: Path) -> tuple[bool, str]:
    adb = str(config.get("adb_path", "/opt/homebrew/bin/adb"))
    address = str(config.get("adb_address", "127.0.0.1:5555"))
    try:
        with path.open("wb") as handle:
            result = subprocess.run(
                [adb, "-s", address, "exec-out", "screencap", "-p"],
                stdout=handle,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
        if result.returncode != 0 or path.stat().st_size < 8:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
        with path.open("rb") as handle:
            if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                raise RuntimeError("ADB 返回的不是 PNG")
        os.chmod(path, 0o600)
        return True, ""
    except Exception as exc:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return False, f"{type(exc).__name__}: {exc}"


def verify_home_screenshot(path: Path) -> tuple[bool | None, str]:
    """Check that a captured screenshot really shows the game home page.

    Returns (True | False | None, detail).  ``None`` means "could not tell"
    (ocrbox missing, OCR crashed) — that must never turn a good run red, so the
    caller only treats an explicit ``False`` as a failure.  Daily runs only:
    抄作业 keeps using capture_screenshot directly and is not gated by this.
    """
    ocrbox = SCRIPTS_DIR / "ocrbox"
    if not path.is_file():
        return None, "没有截图可校验"
    if not ocrbox.is_file():
        return None, "ocrbox 未部署，跳过主页校验"
    try:
        result = subprocess.run(
            [str(ocrbox), str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except Exception as exc:
        return None, f"OCR 失败：{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        return None, f"OCR 失败：{result.stderr.strip()[:120]}"
    # ocrbox prints "文本\t[x,y,w,h]" per line; only the text column matters.
    text = "\n".join(line.split("\t", 1)[0] for line in result.stdout.splitlines())
    hits = [marker for marker in HOME_MARKERS if marker in text]
    detail = (
        f"识别到 {len(hits)}/{len(HOME_MARKERS)} 个主页标识"
        + (f"（{'、'.join(hits)}）" if hits else "")
    )
    return len(hits) >= HOME_MARKER_MIN, detail


def capture_home_screenshot(
    config: dict[str, Any], path: Path
) -> tuple[bool, str, bool | None, str]:
    """Capture the end-of-run screenshot and confirm it is the home page.

    Retries a few times because the home page animates in; each attempt waits
    the same settle delay the single-shot version used.
    """
    delay = max(0.0, float(config.get("home_screenshot_delay", 1)))
    attempts = max(1, int(config.get("home_screenshot_attempts", 3)))
    ok, error, verified, detail = False, "未执行", None, ""
    for _ in range(attempts):
        time.sleep(delay)
        ok, error = capture_screenshot(config, path)
        if not ok:
            return ok, error, None, "没有截图可校验"
        verified, detail = verify_home_screenshot(path)
        if verified is not False:
            break
    return ok, error, verified, detail


def close_everything(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    maa = str(config.get("maa_bin", "/opt/homebrew/bin/maa"))
    adb = str(config.get("adb_path", "/opt/homebrew/bin/adb"))
    address = str(config.get("adb_address", "127.0.0.1:5555"))
    app = str(config.get("emulator_app", "BlueStacks"))
    bundle_id = str(config.get("emulator_bundle_id", "com.now.gg.BlueStacks"))
    if not SAFE_APP_RE.fullmatch(app):
        return ["emulator_app 名称含非法字符，拒绝清理"]
    if not SAFE_BUNDLE_ID_RE.fullmatch(bundle_id):
        return ["emulator_bundle_id 含非法字符，拒绝清理"]
    try:
        closedown = subprocess.run(
            [maa, "closedown", "Official"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env=maa_subprocess_env(config),
        )
        if closedown.returncode != 0:
            errors.append("maa closedown 失败")
    except Exception as exc:
        errors.append(f"maa closedown 异常：{type(exc).__name__}")
    graceful_error = ""
    try:
        quit_result = subprocess.run(
            ["osascript", "-e", f'tell application id "{bundle_id}" to quit'],
            capture_output=True,
            text=True,
            check=False,
        )
        if quit_result.returncode != 0:
            graceful_error = "模拟器正常退出请求失败"
    except Exception as exc:
        graceful_error = f"模拟器正常退出异常：{type(exc).__name__}"
    time.sleep(max(0, int(config.get("quit_grace_seconds", 10))))
    try:
        subprocess.run(["pkill", "-f", app], capture_output=True, check=False)
    except Exception as exc:
        errors.append(f"模拟器兜底退出异常：{type(exc).__name__}")
    try:
        subprocess.run([adb, "disconnect", address], capture_output=True, check=False)
    except Exception as exc:
        errors.append(f"ADB 断开异常：{type(exc).__name__}")
    try:
        # A bracketed first character still matches the target but prevents
        # pgrep from matching its own command line.
        process_pattern = f"[{re.escape(app[0])}]{re.escape(app[1:])}"
        verify_deadline = time.monotonic() + max(
            1, int(config.get("cleanup_verify_timeout", 10))
        )
        while True:
            remaining = subprocess.run(
                ["pgrep", "-fi", process_pattern],
                capture_output=True,
                text=True,
                check=False,
            )
            if remaining.returncode == 1 or not remaining.stdout.strip():
                break
            if time.monotonic() >= verify_deadline:
                if graceful_error:
                    errors.append(graceful_error)
                errors.append(f"仍有 {app} 进程存活")
                break
            time.sleep(0.5)
    except Exception as exc:
        errors.append(f"进程清理确认异常：{type(exc).__name__}")
    return errors


def run_maa(
    config: dict[str, Any],
    log_path: Path,
    token: str,
    thread_id: str,
    progress_message_id: str,
    plan: dict[str, Any] | None = None,
    *,
    timeout: int | None = None,
    carry_lines: list[str] | None = None,
    append: bool = False,
) -> tuple[int, list[str], list[str]]:
    maa = str(config.get("maa_bin", "/opt/homebrew/bin/maa"))
    # maa-cli 0.7.5's --log-file redirects logs away from stderr.  Capture the
    # live streams ourselves and tee both to disk so Discord progress remains
    # observable without sacrificing the full local log.
    command = [maa, "run", "daily", "-v", "--batch"]
    env = maa_subprocess_env(config)
    env["MAA_LOG_PREFIX"] = "Never"
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    assert process.stdout is not None and process.stderr is not None
    stderr_recent: deque[str] = deque(maxlen=12)
    stderr_lines: list[str] = []
    stdout_recent: deque[str] = deque(maxlen=12)
    stdout_lines: list[str] = []
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_lock = threading.Lock()
    with log_path.open("a" if append else "w", encoding="utf-8", buffering=1) as log_handle:
        stderr_thread = threading.Thread(
            target=_reader,
            args=(process.stderr, stderr_recent, stderr_lines, log_handle, log_lock),
            daemon=True,
        )
        stdout_thread = threading.Thread(
            target=_reader,
            args=(process.stdout, stdout_recent, stdout_lines, log_handle, log_lock),
            daemon=True,
        )
        stderr_thread.start()
        stdout_thread.start()
        previous = ""
        next_update = time.monotonic()
        # Hermes' outer no-agent cron timeout is 3600s. Keep ten minutes of
        # headroom for boot, screenshot, cleanup and the final report.
        budget = int(config.get("maa_run_timeout", 3000) if timeout is None else timeout)
        deadline = time.monotonic() + max(MIN_RUN_TIMEOUT_SECONDS, budget)
        # 连着这么多批代理指挥失误就熔断：理智全额退回、一关没打成，再刷下去只会
        # 空转到超时，还会把游戏钉在结算页拖垮后面的基建/公招/商店/奖励。0 = 关闭。
        proxy_limit = max(0, int(config.get("proxy_failure_limit", 2)))
        prefix = list(carry_lines or [])
        timed_out = False
        interrupted = False
        proxy_tripped = 0
        try:
            while process.poll() is None:
                now = time.monotonic()
                if now >= deadline:
                    timed_out = True
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    break
                if now >= next_update:
                    live = list(stdout_lines) + list(stderr_lines)
                    if proxy_limit:
                        streak = proxy_failures(live)[2]
                        if streak >= proxy_limit:
                            proxy_tripped = streak
                            process.terminate()
                            try:
                                process.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=5)
                            break
                    content = progress_content(prefix + live, plan=plan)
                    if content != previous:
                        try:
                            discord_request(
                                token,
                                "PATCH",
                                f"/channels/{thread_id}/messages/{progress_message_id}",
                                {"content": content, "allowed_mentions": {"parse": []}},
                            )
                            previous = content
                        except Exception:
                            # Streaming is best-effort; final report is the hard signal.
                            pass
                    next_update = now + PROGRESS_INTERVAL_SECONDS
                time.sleep(0.25)
        except KeyboardInterrupt:
            interrupted = True
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        return_code = process.wait()
        stderr_thread.join(timeout=2)
        stdout_thread.join(timeout=2)
    if interrupted:
        interrupt_message = "maa run 已按人工指令终止。"
        stderr_lines.append(interrupt_message)
        with log_path.open("a", encoding="utf-8") as log_handle:
            log_handle.write(interrupt_message + "\n")
        return_code = 130
    elif timed_out:
        timeout_message = "maa run 超时，已终止；可能是客户端更新、登录提示或识别流程卡住。"
        stderr_lines.append(timeout_message)
        with log_path.open("a", encoding="utf-8") as log_handle:
            log_handle.write(timeout_message + "\n")
        return_code = 124
    elif proxy_tripped:
        break_message = (
            f"连续 {proxy_tripped} 批代理指挥失误，理智全额返还、一关没打成，"
            "已中止 maa run，避免空转到超时。"
        )
        stderr_lines.append(break_message)
        with log_path.open("a", encoding="utf-8") as log_handle:
            log_handle.write(break_message + "\n")
        return_code = PROXY_BREAK_CODE
    return return_code, stdout_lines, stderr_lines


def run_maa_resilient(
    config: dict[str, Any],
    log_path: Path,
    token: str,
    thread_id: str,
    progress_message_id: str,
    plan: dict[str, Any] | None = None,
    *,
    timeout: int | None = None,
    carry_lines: list[str] | None = None,
) -> tuple[int, list[str], list[str], int]:
    """跑 maa run；在连接阶段就崩掉的那种失败上重开。末位返回重试次数。

    只重试 `connect_failed()` 认定的那一类：MaaTouch 没握上手 → MaaCore SIGSEGV，
    一个键都没点过、一批理智都没花。所有重试共用同一个超时预算，绝不额外撑长
    Hermes 那一小时；日志追加进同一个文件，不覆盖上一趟的现场。
    """
    budget = int(config.get("maa_run_timeout", 3000) if timeout is None else timeout)
    limit = max(0, int(config.get("connect_retry_limit", 2)))
    delay = max(0, int(config.get("connect_retry_delay", 8)))
    started = time.monotonic()
    stdout_all: list[str] = []
    stderr_all: list[str] = []
    retries = 0
    while True:
        remaining = budget - int(time.monotonic() - started)
        code, out, err = run_maa(
            config,
            log_path,
            token,
            thread_id,
            progress_message_id,
            plan,
            timeout=remaining,
            carry_lines=list(carry_lines or []) + stdout_all + stderr_all,
            append=retries > 0,
        )
        stdout_all += out
        stderr_all += err
        marker = connect_failed(out + err, code)
        if not marker:
            return code, stdout_all, stderr_all, retries
        spent = int(time.monotonic() - started)
        if retries >= limit or budget - spent - delay < MIN_RUN_TIMEOUT_SECONDS:
            note = f"模拟器连接失败（{marker}），重试 {retries} 次仍未连上。"
            stderr_all.append(note)
            _append_log(log_path, note)
            return code, stdout_all, stderr_all, retries
        retries += 1
        note = (
            f"模拟器连接失败（{marker}）：MaaTouch 没在 MaaCore 的 3 秒窗口内握上手，"
            f"游戏里一个键都没点过。等 {delay}s 重开第 {retries} 趟。"
        )
        stderr_all.append(note)
        _append_log(log_path, note)
        time.sleep(delay)


def _append_log(log_path: Path, message: str) -> None:
    try:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    except OSError:
        pass


def salvage_plan(plan: dict[str, Any]) -> dict[str, Any] | None:
    """去掉 Fight 的补跑计划；除 Fight 外无事可做时返回 None。"""
    tasks = dict(plan.get("tasks", {}))
    if not any(bool(tasks.get(key, True)) for key in ("infrast", "recruit", "mall", "award")):
        return None
    tasks["fight"] = False
    return {**plan, "tasks": tasks}


def _salvage_run(
    *,
    config: dict[str, Any],
    plan: dict[str, Any],
    task_path: Path,
    log_path: Path,
    token: str,
    thread_id: str,
    progress_message_id: str,
    carry_lines: list[str],
    elapsed: float,
) -> tuple[int | None, list[str], list[str]]:
    """熔断后补跑剩余任务链。返回码为 None 表示没跑（无事可做或时间不够）。"""
    if not bool(config.get("proxy_failure_salvage", True)):
        return None, [], []
    retry_plan = salvage_plan(plan)
    if retry_plan is None:
        return None, [], []
    # 两趟 maa run 共用同一个 maa_run_timeout 预算，别把 Hermes 的一小时撑爆。
    remaining = int(config.get("maa_run_timeout", 3000)) - int(elapsed)
    floor = max(60, int(config.get("proxy_failure_salvage_min_seconds", 300)))
    if remaining < floor:
        return None, [], [f"剩余预算 {max(0, remaining)}s 不足 {floor}s，跳过补跑。"]
    atomic_write_text(task_path, render_daily_toml(retry_plan, config))
    code, out, err = run_maa(
        config,
        log_path,
        token,
        thread_id,
        progress_message_id,
        retry_plan,
        timeout=remaining,
        carry_lines=carry_lines,
    )
    return code, out, ["代理指挥失误熔断，已补跑剩余任务（不含刷理智）。", *err]


def report_payload(
    *,
    ok: bool,
    phase: str,
    plan: dict[str, Any],
    duration: float,
    summary: str,
    screenshot_ok: bool,
    screenshot_error: str,
    cleanup_errors: list[str],
    home_verified: bool | None = None,
    home_verify_detail: str = "",
    proxy_batches: int = 0,
    proxy_refunded: int = 0,
    connect_retries: int = 0,
    inventory_error: str = "",
) -> dict[str, Any]:
    label = PHASE_LABELS[phase]
    notes: list[str] = []
    if connect_retries:
        notes.append(
            f"模拟器连接失败（MaaTouch 握手超时），已重开 {connect_retries} 趟 maa run。"
            "偶发可以不管；每天都撞上就把 emulator_settle_quiet_seconds 调大。"
        )
    if proxy_batches:
        notes.append(
            f"代理指挥失误 {proxy_batches} 批，理智已返还 ×{proxy_refunded}，实际一关没打成。"
            f"多半是 {plan_stage_label(plan)} 的作战录像跟不上随机性——"
            "手动重打一次三星刷新录像，或换个关卡。"
        )
    if not screenshot_ok:
        notes.append(f"截图失败：{screenshot_error}")
    if home_verified is False:
        notes.append(f"截图不是游戏主页：{home_verify_detail}")
    if cleanup_errors:
        notes.append("清理异常：" + "；".join(cleanup_errors))
    if inventory_error:
        notes.append(f"库存增量记录失败：{inventory_error}")
    embed: dict[str, Any] = {
        "title": f"{'✅' if ok else '❌'} MAA 日常·{label}{'完成' if ok else '失败'}",
        "color": 0x57F287 if ok else 0xED4245,
        "fields": [
            {"name": "关卡", "value": plan_stage_label(plan), "inline": True},
            {
                "name": "理智药",
                "value": medicine_label(str(plan.get("medicine", "none"))),
                "inline": True,
            },
            {"name": "碎石", "value": "0", "inline": True},
            {"name": "总耗时", "value": _format_duration(duration), "inline": True},
            {"name": "📋 执行结果", "value": summary[:1024], "inline": False},
        ],
        "footer": {"text": "模拟器与 ADB 已执行清理"},
        "timestamp": now_beijing().isoformat(),
    }
    if notes:
        embed["fields"].append({"name": "备注", "value": "\n".join(notes)[:1024]})
    if screenshot_ok:
        embed["image"] = {"url": "attachment://shot.png"}
    payload: dict[str, Any] = {
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }
    if screenshot_ok:
        payload["attachments"] = [{"id": 0, "filename": "shot.png"}]
    return payload


def send_report(
    token: str,
    channel_id: str,
    payload: dict[str, Any],
    screenshot: Path,
    config: dict[str, Any],
) -> None:
    if "attachments" in payload:
        try:
            discord_upload(token, channel_id, payload, screenshot)
            return
        except Exception as exc:
            embed = dict(payload["embeds"][0])
            embed.pop("image", None)
            fields = list(embed.get("fields", []))
            fields.append({"name": "图片上传", "value": f"内嵌上传失败：{type(exc).__name__}"})
            embed["fields"] = fields
            discord_request(
                token,
                "POST",
                f"/channels/{channel_id}/messages",
                {"embeds": [embed], "allowed_mentions": {"parse": []}},
            )
            hermes = str(config.get("hermes_bin", Path.home() / ".local" / "bin" / "hermes"))
            subprocess.run(
                [hermes, "send", "--to", f"discord:{channel_id}", f"MEDIA:{screenshot}"],
                capture_output=True,
                check=False,
                timeout=30,
            )
            return
    discord_request(token, "POST", f"/channels/{channel_id}/messages", payload)


def send_direct_failure(
    token: str,
    channel_id: str,
    phase: str,
    plan: dict[str, Any],
    message: str,
) -> None:
    discord_request(
        token,
        "POST",
        f"/channels/{channel_id}/messages",
        {
            "embeds": [
                {
                    "title": f"❌ MAA 日常·{PHASE_LABELS[phase]}启动失败",
                    "description": message[:3500],
                    "color": 0xED4245,
                    "fields": [
                        {"name": "计划关卡", "value": plan_stage_label(plan), "inline": True},
                        {"name": "碎石", "value": "0", "inline": True},
                    ],
                    "timestamp": now_beijing().isoformat(),
                }
            ],
            "allowed_mentions": {"parse": []},
        },
    )


def should_skip(plan: dict[str, Any], phase: str) -> bool:
    """Whether this run bails out before touching the emulator.

    `skip` only silences the 08:00 / 22:00 cron sessions.  A manual run is the
    user asking for it right now, which overrides today's plan by definition —
    otherwise "跳过今天" would leave no way to run at all short of un-skipping,
    which would also bring the automatic sessions back.
    """
    return bool(plan.get("skip")) and phase != "now"


def execute(phase: str) -> int:
    started = now_beijing()
    config = load_config()
    channel_id = str(config.get("channel_id", "")).strip()
    if not channel_id.isdigit():
        raise RuntimeError("config.json 中尚未配置 Discord channel_id")
    token = discord_token()
    last_run_state = load_json(LAST_RUN_PATH, {})
    if not isinstance(last_run_state, dict):
        last_run_state = {}
    plan = attach_annihilation(effective_plan(started), started, last_run_state)
    run_stem = f"{started.date().isoformat()}-{phase}"
    snapshot_path = RUNS_DIR / f"{run_stem}.json"
    log_path = RUNS_DIR / f"{run_stem}.log"
    screenshot_path = RUNS_DIR / f"{run_stem}.png"

    with run_lock(blocking=False) as acquired:
        if not acquired:
            return 0
        cleanup_run_files(int(config.get("retention_days", 14)), started)
        snapshot = {
            "date": started.date().isoformat(),
            "phase": phase,
            "started_at": started.isoformat(),
            "plan": plan,
        }
        # This is the immutable per-run plan snapshot. Later plan.json writes
        # cannot change this Python object or this file.
        atomic_write_json(snapshot_path, snapshot)
        merge_last_run(
            {
                "status": "running",
                "phase": phase,
                "started_at": started.isoformat(),
                "plan": plan,
                "snapshot": str(snapshot_path),
                # Do not expose terminal fields from the previous run while
                # this run is still booting and has not created its thread.
                "thread_id": None,
                "finished_at": None,
                "duration_seconds": None,
                "return_code": None,
                "log": None,
                "screenshot": None,
                "screenshot_error": None,
                "startup_error": None,
                "cleanup_errors": [],
                "summary": "",
            }
        )

        if should_skip(plan, phase):
            discord_request(
                token,
                "POST",
                f"/channels/{channel_id}/messages",
                {
                    "embeds": [
                        {
                            "title": f"⏭️ MAA 日常·{PHASE_LABELS[phase]}已跳过",
                            "description": "今日计划设置了跳过自动场次；未启动模拟器。"
                            "手动开跑（「现在跑」）不受影响。",
                            "color": 0x99AAB5,
                            "timestamp": started.isoformat(),
                        }
                    ],
                    "allowed_mentions": {"parse": []},
                },
            )
            merge_last_run({"status": "skipped", "finished_at": now_beijing().isoformat()})
            return 0

        emulator_opened = False
        thread_id = ""
        progress_message_id = ""
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        return_code = 1
        screenshot_ok = False
        screenshot_error = "未执行"
        home_verified: bool | None = None
        home_verify_detail = ""
        cleanup_errors: list[str] = []
        inventory_error = ""
        salvage_log_path: Path | None = None
        connect_retries = 0
        startup_error = ""
        try:
            require_executable(config, "maa_bin", "/opt/homebrew/bin/maa", "maa-cli")
            require_executable(config, "adb_path", "/opt/homebrew/bin/adb", "ADB")
            open_emulator(config)
            emulator_opened = True
            wait_for_android(config)
            verify_game_installed(config)
            opening = discord_request(
                token,
                "POST",
                f"/channels/{channel_id}/messages",
                {
                    "content": (
                        f"🌱 MAA 日常·{PHASE_LABELS[phase]}场开始\n"
                        f"🎯 {plan_stage_label(plan)}"
                        + (" · ☄️ 今日剿灭" if plan.get("annihilation") else " · ☄️ 剿灭今日已刷")
                        + f" · 🧪 {medicine_label(plan['medicine'])}"
                    ),
                    "allowed_mentions": {"parse": []},
                },
            )
            thread_name = f"MAA日常·{PHASE_LABELS[phase]} · {started:%m-%d %H:%M}"
            thread = create_thread(token, channel_id, str(opening["id"]), thread_name)
            thread_id = str(thread["id"])
            placeholder = discord_request(
                token,
                "POST",
                f"/channels/{thread_id}/messages",
                {
                    "content": progress_content([], plan=plan),
                    "allowed_mentions": {"parse": []},
                },
            )
            progress_message_id = str(placeholder["id"])
            merge_last_run(
                {
                    "thread_id": thread_id,
                    "threads": [{"thread_id": thread_id, "created_at": started.isoformat()}],
                }
            )
            task_path = Path(str(config.get("maa_config_dir", MAA_CONFIG_DIR))) / "tasks" / "daily.toml"
            atomic_write_text(task_path, render_daily_toml(plan, config))
            # 最后一道门：`sys.boot_completed` 之后 app_process 还会抖，撞上 MaaCore
            # 那 3 秒的 MaaTouch 窗口就是一场空跑（§11.14）。探不出安静窗口不阻断。
            settle_warning = settle_android_runtime(config)
            if settle_warning:
                stderr_lines.append(settle_warning)
            prior_errors = list(stderr_lines)
            return_code, stdout_lines, run_errors, connect_retries = run_maa_resilient(
                config,
                log_path,
                token,
                thread_id,
                progress_message_id,
                plan,
                carry_lines=prior_errors,
            )
            stderr_lines = prior_errors + run_errors
            # 125 也可能是 maa 自己的退出码，所以还要确认日志里真有代理失误，
            # 免得凭一个数字就多跑一趟。
            if return_code == PROXY_BREAK_CODE and fight_wiped_out(stdout_lines + stderr_lines)[0]:
                # 熔断掐掉的是整条 maa run，基建/公招/商店/奖励也一起没了。补跑一趟
                # 去掉 Fight 的链条把它们捞回来——StartUp 会顺手把卡在结算页的游戏
                # 带回主界面。预算从总超时里扣，绝不额外撑长 Hermes 那一小时。
                salvage_log = RUNS_DIR / f"{run_stem}-salvage.log"
                salvage_code, salvage_out, salvage_err = _salvage_run(
                    config=config,
                    plan=plan,
                    task_path=task_path,
                    log_path=salvage_log,
                    token=token,
                    thread_id=thread_id,
                    progress_message_id=progress_message_id,
                    carry_lines=stdout_lines + stderr_lines,
                    elapsed=(now_beijing() - started).total_seconds(),
                )
                if salvage_code is not None:
                    return_code = salvage_code
                    salvage_log_path = salvage_log
                stdout_lines += salvage_out
                stderr_lines += salvage_err
            # The final Custom task has returned to Home. Let the last UI
            # animation settle, then capture before the separate CloseDown and
            # confirm by OCR that the frame we ship really is the home page.
            (
                screenshot_ok,
                screenshot_error,
                home_verified,
                home_verify_detail,
            ) = capture_home_screenshot(config, screenshot_path)
        except Exception as exc:
            exception_detail = f"{type(exc).__name__}: {exc}"
            stderr_lines.append(exception_detail)
            return_code = 1
            if not thread_id:
                startup_error = exception_detail
                try:
                    send_direct_failure(token, channel_id, phase, plan, exception_detail)
                except Exception:
                    pass
        finally:
            if emulator_opened:
                try:
                    cleanup_errors = close_everything(config)
                except Exception as exc:
                    cleanup_errors = [f"清理异常：{type(exc).__name__}: {exc}"]

        fight_lines = _named_task_section(stdout_lines + stderr_lines, "刷理智")
        drops = _drop_pairs(fight_lines) if fight_lines else []
        if drops:
            try:
                maa_depot.record_drops(drops, config, at=now_beijing())
            except Exception as exc:
                inventory_error = f"{type(exc).__name__}: {exc}"

        duration = (now_beijing() - started).total_seconds()
        # 代理指挥失误到一关没打成，就算后面的链条都补回来了也不算成功——得让人
        # 看见红色去修作战录像，否则理智白挂在那儿谁也不知道。
        proxy_batches, proxy_refunded = fight_wiped_out(stdout_lines + stderr_lines)
        ok = (
            return_code == 0
            and not cleanup_errors
            and home_verified is not False
            and not proxy_batches
            and not inventory_error
        )
        summary = extract_summary(
            stdout_lines,
            stderr_lines,
            plan=plan,
            ok=ok,
            home_verified=home_verified,
            home_verify_detail=home_verify_detail,
        )
        if thread_id and progress_message_id:
            final_heading = f"{'✅' if ok else '❌'} 结束 · 总耗时 {_format_duration(duration)}"
            try:
                discord_request(
                    token,
                    "PATCH",
                    f"/channels/{thread_id}/messages/{progress_message_id}",
                    {
                        "content": progress_content(
                            stdout_lines + stderr_lines,
                            plan=plan,
                            final=final_heading,
                        ),
                        "allowed_mentions": {"parse": []},
                    },
                )
            except Exception:
                pass
        # Startup failures were already reported directly. All runs that made
        # a thread get a deterministic battle report, with screenshot if valid.
        if thread_id:
            payload = report_payload(
                ok=ok,
                phase=phase,
                plan=plan,
                duration=duration,
                summary=summary,
                screenshot_ok=screenshot_ok,
                screenshot_error=screenshot_error,
                cleanup_errors=cleanup_errors,
                home_verified=home_verified,
                home_verify_detail=home_verify_detail,
                proxy_batches=proxy_batches,
                proxy_refunded=proxy_refunded,
                connect_retries=connect_retries,
                inventory_error=inventory_error,
            )
            send_report(token, channel_id, payload, screenshot_path, config)
        finished: dict[str, Any] = {
            "status": "success" if ok else "failed",
            "finished_at": now_beijing().isoformat(),
            "duration_seconds": int(duration),
            "return_code": return_code,
            "log": str(log_path),
            "salvage_log": str(salvage_log_path) if salvage_log_path else None,
            "screenshot": str(screenshot_path) if screenshot_ok else None,
            "screenshot_error": screenshot_error if not screenshot_ok else None,
            "startup_error": startup_error or None,
            "home_verified": home_verified,
            "home_verify_detail": home_verify_detail,
            "cleanup_errors": cleanup_errors,
            "summary": summary,
            "proxy_failure_batches": proxy_batches,
            "proxy_failure_sanity_refunded": proxy_refunded,
            "connect_retries": connect_retries,
            "inventory_error": inventory_error or None,
        }
        if plan.get("annihilation") and annihilation_consumed(stdout_lines + stderr_lines):
            finished["annihilation_game_day"] = cn_game_day(started)
            finished["annihilation_finished_at"] = now_beijing().isoformat()
        merge_last_run(finished)
        return 0 if ok else 1


def main(argv: list[str]) -> int:
    phase = select_phase(argv)
    return execute(phase)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except Exception as exc:
        print(f"MAA 日常失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
