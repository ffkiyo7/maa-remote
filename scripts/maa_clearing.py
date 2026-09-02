#!/usr/bin/env python3
"""活动通关队列的领域层（PLAN_CLEARING 步骤 3）。

角色对齐 ``maa_copilot.py``：这里只有近纯函数——排队、闸门、理智预算、失败记账、
卡片构造。真正开模拟器执行的是 ``maa_copilot_worker.py``（**不新建第二个 worker**）。

状态落在 ``~/.hermes/state/maa/clearing.json``（0600，`atomic_write_json`），
**不塞进 `config.json`**：config.json 是环境与阈值，队列是会话状态。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from maa_common import (
    BEIJING,
    STATE_DIR,
    atomic_write_json,
    load_json,
    now_beijing,
)

CLEARING_PATH = STATE_DIR / "clearing.json"
SCHEMA_VERSION = 1

# 第三个按钮前缀。故意既不是 `hermes:btn:maa:`（每天 07:50 被计划卡整段清扫，
# 而 needs_manual 卡片必须跨天存活），也不是 `hermes:btn:maa-copilot:`
# （由 `prune_expired_searches()` 按 searches/<token>.json 的 expires_at 清理，
# 队列没有 token 也没有 25 小时 TTL）。独立前缀让两条既有清扫边界
# （不变量 #7、#14）**一个字节都不用改**，队列自己负责自己的生命周期：
# 卡片发出时登记，用户点掉或队列前进时由 `prune_manual_buttons()` 摘掉。
BUTTON_PREFIX = "hermes:btn:maa-clearing:"

DEFAULT_MANUAL_AFTER_DAYS = 2
# 剿灭同样吃理智（地基事实 H）。国服剿灭作战单次 25 理智；写成可配是因为
# 这个数字属于游戏侧常量，改版时不该改代码。
DEFAULT_ANNIHILATION_SANITY = 25

# 普通数字关：AT-1 … AT-8。EX 段形如 AT-EX-3。
NORMAL_STAGE_RE = re.compile(r"^([A-Za-z0-9]{1,8})-(\d{1,2})$")
EX_STAGE_RE = re.compile(r"^([A-Za-z0-9]{1,8})-EX-(\d{1,2})$")


# ---------------------------------------------------------------------------
# 状态读写
# ---------------------------------------------------------------------------

def load_state(path: Path = CLEARING_PATH) -> dict[str, Any] | None:
    """读队列状态；文件不存在、坏掉或结构不对都返回 None（= 队列不存在）。

    坏文件不抛异常是刻意的：`queue_is_active()` 挂在日常渲染路径上，
    一个坏掉的状态文件绝不能把整场日常带走（D-2 的教训）。返回 None 的后果是
    「队列不活跃」→ 日常照常刷理智，理智不会白丢。
    """
    try:
        state = load_json(path, None)
    except (ValueError, OSError):
        return None
    if not isinstance(state, dict):
        return None
    if not isinstance(state.get("stages"), list):
        return None
    return state


def save_state(state: dict[str, Any], path: Path = CLEARING_PATH) -> dict[str, Any]:
    state["schema_version"] = SCHEMA_VERSION
    state["updated_at"] = now_beijing().isoformat()
    atomic_write_json(path, state)
    return state


def new_state(
    activity_key: str,
    activity_title: str,
    prefix: str,
    stages: Iterable[str],
    *,
    at: datetime | None = None,
) -> dict[str, Any]:
    moment = at or now_beijing()
    return {
        "schema_version": SCHEMA_VERSION,
        "activity_key": str(activity_key)[:200],
        "activity_title": str(activity_title)[:120],
        "prefix": str(prefix)[:8],
        "stages": [str(stage) for stage in stages],
        "cursor": 0,
        "created_at": moment.isoformat(),
        "updated_at": moment.isoformat(),
        "stage_state": {},
        "needs_manual": None,
        "ex_appended": False,
        "notified": {},
    }


def today_key(at: datetime | None = None) -> str:
    """`Asia/Shanghai` 自然日。`needs_manual` 的 N 按它计（不是游戏日 04:00）。"""
    moment = at or now_beijing()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BEIJING)
    return moment.astimezone(BEIJING).date().isoformat()


# ---------------------------------------------------------------------------
# 队列形态
# ---------------------------------------------------------------------------

def build_queue(prefix: str, stages: Iterable[str]) -> list[str]:
    """普通数字关按序号升序；EX 段不在这里加（等闸门开）。"""
    numbered: list[tuple[int, str]] = []
    for raw in stages:
        stage = str(raw).strip()
        match = NORMAL_STAGE_RE.match(stage)
        if not match or match.group(1) != prefix:
            continue
        numbered.append((int(match.group(2)), stage))
    numbered.sort()
    seen: set[str] = set()
    result: list[str] = []
    for _seq, stage in numbered:
        if stage not in seen:
            seen.add(stage)
            result.append(stage)
    return result


def queue_from_activity_stages(stages: Iterable[str]) -> tuple[str, list[str]]:
    """从 `config.activity_stages` 推出完整队列。

    `activity_stages` 只有 MAA 推荐的那 4 关（例如 `AT-8/AT-7/AT-6/AT-4`），
    而队列要 `AT-1 → … → AT-8`：取同一前缀里的最大序号，补齐 1..max。
    多个前缀并存（复刻 + 新活动）时取关卡数最多的那个前缀，其余忽略。
    """
    buckets: dict[str, int] = {}
    counts: dict[str, int] = {}
    for raw in stages:
        match = NORMAL_STAGE_RE.match(str(raw).strip())
        if not match:
            continue
        prefix, seq = match.group(1), int(match.group(2))
        buckets[prefix] = max(buckets.get(prefix, 0), seq)
        counts[prefix] = counts.get(prefix, 0) + 1
    if not buckets:
        return "", []
    prefix = max(counts, key=lambda key: (counts[key], buckets[key]))
    return prefix, [f"{prefix}-{index}" for index in range(1, buckets[prefix] + 1)]


# 队列条目的突袭表示：**关卡名 + `#raid` 后缀**。
# 选后缀而不是把 stages 改成带 mode 的字典，是因为 stages 保持成一串纯字符串：
# 已落盘的 clearing.json 不需要迁移，`current_stage()` / `plan_round()` /
# `stage_state` 的键都不用改形状，Discord 的 custom_id 也直接拼得进去。
# **除了执行时的 `--raid` 取值，其余一切（理智查表、导航、状态展示）都用
# `split_entry()` 去掉后缀之后的关卡名。**
RAID_SUFFIX = "#raid"


def split_entry(entry: str) -> tuple[str, str]:
    """队列条目 → `(关卡名, mode)`，mode ∈ {"normal", "raid"}。"""
    text = str(entry or "")
    if text.endswith(RAID_SUFFIX):
        return text[: -len(RAID_SUFFIX)], "raid"
    return text, "normal"


def entry_label(entry: str) -> str:
    """给人看的名字：`AT-EX-1（突袭）`。"""
    stage, mode = split_entry(entry)
    return f"{stage}（突袭）" if mode == "raid" else stage


def ex_segment(prefix: str, count: int) -> list[str]:
    """EX 段：**普通 → 突袭紧跟对应普通关**（AT-EX-1 普通 → AT-EX-1 突袭 → AT-EX-2 …）。

    突袭是同一个关卡再打一遍，靠条目上的 `#raid` 后缀区分，执行时传
    `maa copilot --raid raid`（取值由 `maa copilot --help` 核实：
    `normal` / `raid` / `both`）。
    """
    entries: list[str] = []
    for index in range(1, max(0, count) + 1):
        stage = f"{prefix}-EX-{index}"
        entries.append(stage)
        entries.append(f"{stage}{RAID_SUFFIX}")
    return entries


def current_stage(state: dict[str, Any]) -> str:
    """当前**队列条目**（可能带 `#raid` 后缀）。取关卡名请用 `split_entry()`。"""
    stages = state.get("stages") or []
    cursor = int(state.get("cursor", 0))
    if not isinstance(stages, list) or not 0 <= cursor < len(stages):
        return ""
    return str(stages[cursor])


def is_finished(state: dict[str, Any]) -> bool:
    stages = state.get("stages") or []
    return int(state.get("cursor", 0)) >= len(stages)


def advance_cursor(state: dict[str, Any]) -> dict[str, Any]:
    """只在「这关确实过了 / 用户说手打完了」时调用。"""
    stage = current_stage(state)
    state["cursor"] = int(state.get("cursor", 0)) + 1
    if stage:
        stage_state = state.setdefault("stage_state", {})
        if isinstance(stage_state, dict):
            stage_state.pop(stage, None)
    state["needs_manual"] = None
    return state


# ---------------------------------------------------------------------------
# EX 开放闸门（与 `zone_progress_warning()` 是两条独立的单向规则，**不许合并**）
# ---------------------------------------------------------------------------

def ex_gate(roster: dict[str, Any] | None) -> tuple[bool, str]:
    """(是否放行 EX 段, 被挡住的原因)。**fail-closed**。

    规则：只要 `roster["activity_progress"]` 里还存在 `totalStage == 0` 的 zone，
    活动就尚未全开 → EX / S 段不入队；全部 zone 都 > 0 才放行。

    ⚠️ **此推论未实地验证**（PLAN_CLEARING 步骤 3）。依据是四个活动的缓存比对：
    唯一出现 `totalStage == 0` 的就是当时正在进行、尚未全开的那个活动，且
    act51side 的 zone2 `total = 8` 恰好等于它的 EX 关数。
    **下个活动上线当天必须核对一次**：把当天的 `activity_progress` 与游戏里
    EX 是否真的开放对一遍，对不上就回来改这里，不要让队列继续按它排。

    森空岛不可用（401 / 无数据）一律返回不放行，绝不当成「全开」——
    多等一天没有代价，误判成全开会让队列去打一个根本没开的段。
    """
    if not isinstance(roster, dict):
        return False, "森空岛数据不可用（roster 缺失），EX 段按未全开处理"
    progress = roster.get("activity_progress")
    if not isinstance(progress, dict) or not progress:
        return False, "森空岛未返回活动进度（activity_progress 为空），EX 段按未全开处理"
    for act_id, zones in progress.items():
        if not isinstance(zones, list):
            return False, f"活动 {act_id} 的 zone 进度格式异常，EX 段按未全开处理"
        for zone in zones:
            if not isinstance(zone, dict):
                return False, f"活动 {act_id} 的 zone 进度格式异常，EX 段按未全开处理"
            try:
                total = int(zone.get("totalStage") or 0)
            except (TypeError, ValueError):
                return False, f"活动 {act_id} 的 totalStage 不是整数，EX 段按未全开处理"
            if total == 0:
                zone_id = str(zone.get("zoneId") or act_id)
                return False, f"{zone_id} 的 totalStage 仍为 0，活动尚未全开，EX 段暂不入队"
    return True, ""


def apply_ex_gate(
    state: dict[str, Any],
    roster: dict[str, Any] | None,
    ex_count: int,
    *,
    at: datetime | None = None,
) -> tuple[dict[str, Any], bool, str]:
    """闸门放行就把 EX 段接到队尾；被挡住就把原因写进状态（fail-closed 可见）。

    返回 `(state, 是否需要发 Discord 卡片, 原因文案)`。护栏 4：不许只是安静地不入队。
    """
    moment = at or now_beijing()
    if state.get("ex_appended"):
        return state, False, ""
    allowed, reason = ex_gate(roster)
    if allowed:
        prefix = str(state.get("prefix") or "")
        stages = list(state.get("stages") or [])
        for stage in ex_segment(prefix, ex_count):
            if stage not in stages:
                stages.append(stage)
        state["stages"] = stages
        state["ex_appended"] = True
        state.pop("ex_gate_blocked_reason", None)
        state.pop("ex_gate_blocked_since", None)
        return state, False, ""
    previous = str(state.get("ex_gate_blocked_reason") or "")
    state["ex_gate_blocked_reason"] = reason
    if previous != reason:
        # 原因变了要重新发；同一原因不刷屏（沿用步骤 0 的 last_notified_error 范式）。
        state["ex_gate_blocked_since"] = moment.isoformat()
        return state, True, reason
    state.setdefault("ex_gate_blocked_since", moment.isoformat())
    return state, False, reason


# ---------------------------------------------------------------------------
# 理智预算
# ---------------------------------------------------------------------------

def annihilation_reserve(plan: dict[str, Any], config: dict[str, Any]) -> int:
    """今天还要打的剿灭要预留多少理智（地基事实 H）。

    剿灭是 `daily.toml` 里的第二个 `Fight(stage=Annihilation)`，**它同样消耗理智**。
    队列排关前必须先扣掉这份，否则会把可用理智算多、排出打不完的一轮。
    `plan["annihilation"]` 由 `maa_daily.attach_annihilation()` 按游戏日记帐；
    今天已经刷过就是 0。
    """
    if not isinstance(plan, dict) or not plan.get("annihilation"):
        return 0
    if not bool((plan.get("tasks") or {}).get("fight", True)):
        return 0
    try:
        cost = int(config.get("clearing_annihilation_sanity", DEFAULT_ANNIHILATION_SANITY))
    except (TypeError, ValueError):
        cost = DEFAULT_ANNIHILATION_SANITY
    return max(0, cost)


def sanity_budget(
    available: int | None,
    plan: dict[str, Any],
    config: dict[str, Any],
) -> int:
    """队列这一轮能花的理智 = 当前理智 − 剿灭那份。理智未知一律 0（fail-closed）。"""
    if available is None:
        return 0
    try:
        current = int(available)
    except (TypeError, ValueError):
        return 0
    return max(0, current - annihilation_reserve(plan, config))


# ---------------------------------------------------------------------------
# 失败记账 / 冻结 / needs_manual
# ---------------------------------------------------------------------------

def _stage_entry(state: dict[str, Any], stage: str) -> dict[str, Any]:
    stage_state = state.setdefault("stage_state", {})
    if not isinstance(stage_state, dict):
        stage_state = {}
        state["stage_state"] = stage_state
    entry = stage_state.get(stage)
    if not isinstance(entry, dict):
        entry = {"failed_dates": [], "nav_frozen_date": "", "last_error": ""}
        stage_state[stage] = entry
    if not isinstance(entry.get("failed_dates"), list):
        entry["failed_dates"] = []
    return entry


def is_nav_frozen(state: dict[str, Any], stage: str, at: datetime | None = None) -> bool:
    """nav 失败后冻结该段，**同一天不再重试**（每天最多撞一次）。"""
    stage_state = state.get("stage_state")
    if not isinstance(stage_state, dict):
        return False
    entry = stage_state.get(stage)
    if not isinstance(entry, dict):
        return False
    return str(entry.get("nav_frozen_date") or "") == today_key(at)


def manual_after_days(config: dict[str, Any]) -> int:
    try:
        value = int(config.get("clearing_manual_after_days", DEFAULT_MANUAL_AFTER_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_MANUAL_AFTER_DAYS
    return max(1, value)


def record_stage_failure(
    state: dict[str, Any],
    stage: str,
    *,
    kind: str,
    detail: str = "",
    config: dict[str, Any] | None = None,
    at: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """记一次失败，返回 `(state, 是否刚刚转入 needs_manual)`。

    **N 按「不同日期」计，不按尝试次数计**：记的是该关失败过的日期集合
    （`Asia/Shanghai` 日界），同一天失败 5 次只算 1。理由是活动首日 prts.plus 上
    作业本来就少，按次数计会让每一关当天就触发 needs_manual，把「永不卡死的出口」
    变成「每关都要用户手打」。按天计也与 nav 失败冻结的「每天重试一次」处在
    同一时间尺度上，两者不会互相打架。

    `kind == "nav"` 额外冻结该段到明天。
    """
    entry = _stage_entry(state, stage)
    day = today_key(at)
    if day not in entry["failed_dates"]:
        entry["failed_dates"].append(day)
    entry["last_error"] = str(detail)[:400]
    entry["last_failed_at"] = (at or now_beijing()).isoformat()
    if kind == "nav":
        entry["nav_frozen_date"] = day
    threshold = manual_after_days(config or {})
    if len(entry["failed_dates"]) < threshold:
        return state, False
    if isinstance(state.get("needs_manual"), dict) and state["needs_manual"].get(
        "stage"
    ) == stage:
        return state, False
    state["needs_manual"] = {
        "stage": stage,
        "since": (at or now_beijing()).isoformat(),
        "days": list(entry["failed_dates"]),
        "reason": str(detail)[:400],
        "message_id": None,
    }
    return state, True


def mark_blocked(
    state: dict[str, Any],
    kind: str,
    stages: list[str],
    *,
    at: datetime | None = None,
    detail: str = "",
) -> dict[str, Any]:
    """记下「今天排不出任何一关，而且不是因为理智不够」。

    理智不够是**暂时的**（等一会儿就恢复了），不该让日常把刷理智让出去继续等；
    但「理智成本算不出来」（EX 关，步骤 4 才解）会一直排不出来，队列会永久停在
    那里。不记这一笔的话 `queue_is_active()` 只看得到「cursor 没到末尾」，
    于是日常每场都摘掉刷理智、队列又一关都打不了，理智整天白白溢出——
    正是三条件定义要防的那种损失，只是原因不在原来那三条里。

    按**日期**记，次日自动过期重试一次，与 nav 失败冻结同一个时间尺度。
    """
    state["blocked"] = {
        "kind": str(kind),
        "date": today_key(at),
        "stages": [str(stage) for stage in stages],
        "at": (at or now_beijing()).isoformat(),
        "detail": str(detail)[:400],
    }
    return state


def clear_blocked(state: dict[str, Any]) -> dict[str, Any]:
    state.pop("blocked", None)
    return state


def blocked_today(state: dict[str, Any], at: datetime | None = None) -> str:
    """今天被堵住的原因种类；没被堵返回空串。"""
    blocked = state.get("blocked")
    if not isinstance(blocked, dict):
        return ""
    if str(blocked.get("date") or "") != today_key(at):
        return ""
    return str(blocked.get("kind") or "")


def awaiting_manual(state: dict[str, Any]) -> str:
    manual = state.get("needs_manual")
    if isinstance(manual, dict):
        return str(manual.get("stage") or "")
    return ""


# ---------------------------------------------------------------------------
# 「队列活跃」的精确定义（日常让路只看它）
# ---------------------------------------------------------------------------

def queue_is_active(state: dict[str, Any] | None, at: datetime | None = None) -> bool:
    """**四个**条件全部成立才算活跃：

    1. `clearing.json` 存在，且 cursor **未到末尾**；
    2. 今日**不处于 nav 失败冻结**；
    3. **不在 `needs_manual` 等待用户点击**；
    4. 今日**未被 `mark_blocked()` 堵住**（当前唯一的堵法是「理智成本算不出来」，
       见 `plan_round()` 的 `unknown_cost`）。

    只有活跃时日常才摘掉「刷理智」。**队列因冻结、等待或被堵而无法消耗理智的
    那天，日常照常刷理智**——否则那天既不打队列、也不刷理智，一整天的理智白白
    溢出，这是最难被发现的一种损失（日常判绿、卡片正常，只是什么都没打）。
    注意「活跃」不等于「clearing.json 存在」：只判存在会把上面三种日子一起算进去。
    """
    if not isinstance(state, dict):
        return False
    if is_finished(state):
        return False
    if awaiting_manual(state):
        return False
    if blocked_today(state, at):
        return False
    stage = current_stage(state)
    if stage and is_nav_frozen(state, stage, at):
        return False
    return True


# ---------------------------------------------------------------------------
# 排一轮
# ---------------------------------------------------------------------------

def plan_round(
    state: dict[str, Any],
    budget: int,
    cost_of: Callable[[str], int | None],
    *,
    at: datetime | None = None,
) -> dict[str, Any]:
    """从 cursor 起按序排出这一轮打得起的关；**不动 cursor**。

    返回 `{"stages": [...], "spent": int, "unknown_cost": [...], "stop_reason": str}`。

    理智成本未知的关（`stage_sanity_cost()` 对 EX 返回 `None`，步骤 4 才解）：
    **不排入当轮、不计入理智预算**，也**不前进 cursor**——那不是「这关打完了」，
    是「现在算不出来」，前进会把这关永久跳过。不许拿 0 顶替、不许拿同活动普通关
    的值猜：理智算错会直接导致队列空跑。
    """
    stages = state.get("stages") or []
    cursor = int(state.get("cursor", 0))
    remaining = max(0, int(budget))
    picked: list[str] = []
    unknown: list[str] = []
    stop_reason = "queue_end"
    for index in range(cursor, len(stages)):
        stage = str(stages[index])
        if is_nav_frozen(state, stage, at):
            stop_reason = "nav_frozen"
            break
        cost = cost_of(stage)
        if cost is None:
            unknown.append(stage)
            stop_reason = "unknown_cost"
            break
        try:
            cost = int(cost)
        except (TypeError, ValueError):
            unknown.append(stage)
            stop_reason = "unknown_cost"
            break
        if cost <= 0 or cost > remaining:
            stop_reason = "sanity"
            break
        picked.append(stage)
        remaining -= cost
    return {
        "stages": picked,
        "spent": max(0, int(budget)) - remaining,
        "unknown_cost": unknown,
        "stop_reason": stop_reason,
    }


# ---------------------------------------------------------------------------
# 去重通知
# ---------------------------------------------------------------------------

def should_notify(state: dict[str, Any], key: str, reason: str) -> bool:
    """同一原因不刷屏；原因变了要重新发。沿用步骤 0 的 `last_notified_error` 范式。"""
    notified = state.setdefault("notified", {})
    if not isinstance(notified, dict):
        notified = {}
        state["notified"] = notified
    if notified.get(key) == reason:
        return False
    notified[key] = reason
    return True


def clear_notification(state: dict[str, Any], key: str) -> None:
    notified = state.get("notified")
    if isinstance(notified, dict):
        notified.pop(key, None)


# ---------------------------------------------------------------------------
# Discord 卡片（构造在这里，发送在 worker / 计划卡）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 「这一关到底打过了没有」的判据
# ---------------------------------------------------------------------------

# maa-cli 把 MaaCore 的 TaskChain 回调打成 `<链名> Completed / Stopped / Error`
# ——`maa_daily._task_state()` 与 worker 导航段的 `"Fight Completed" not in …`
# 都建立在同一条约定上，后者是 C3 实测跑通过的。
COPILOT_VICTORY_MARKERS = ("Copilot Completed",)
COPILOT_FAILURE_MARKERS = (
    "Copilot Error",
    "Copilot Stopped",
    "Error: Some error",
)


def copilot_outcome(output: str, return_code: int) -> tuple[str, str]:
    """`maa copilot` 这一趟到底算不算打过了：`("won"|"failed"|"unknown", 明细)`。

    **为什么不能只看退出码**：抄作业那条链上退出码 0 就报成功是够用的——用户看得见
    战报、下次自己再点一次。但队列的 cursor 一旦前进就**永久跳过这一关**，
    撤退、三星未达、进程正常退出的 Fight Error 全都会被算成通关，队列会带着一串
    没打过的关一路推到底，而且没有任何人知道。

    **局限（必须知道）**：胜利标记 `Copilot Completed` 是从 maa-cli 的任务链约定
    推出来的，**没有拿真实的 copilot 日志核对过**——`runs/` 的 14 天保留期已经把
    2026-08 那批抄作业日志清掉了，当前 `asst.log` 窗口里也没有任何一次真实作战。
    所以这里**故意 fail-closed**：认不出胜利标记就返回 `unknown`，调用方按失败处理、
    cursor 不前进。代价是队列可能停下来问用户，好过静默跳关。
    第一次真机跑完请照着 `runs/<date>-clearing.log` 核对一次这个标记，对不上就改这里。
    """
    text = str(output or "")
    hit = [marker for marker in COPILOT_FAILURE_MARKERS if marker in text]
    if hit:
        return "failed", f"输出含失败标记：{'、'.join(hit)}"
    if return_code != 0:
        return "failed", f"maa copilot 退出码 {return_code}"
    if any(marker in text for marker in COPILOT_VICTORY_MARKERS):
        return "won", ""
    return (
        "unknown",
        "退出码 0 但输出里没有 `Copilot Completed`，无法确认这一关真的打过了；"
        "cursor 未前进（宁可停下来问，也不静默跳关）。"
        "请核对 runs/ 下的 clearing 日志，若胜利标记不是这个字符串就改 "
        "maa_clearing.COPILOT_VICTORY_MARKERS。",
    )


def manual_button_id(stage: str) -> str:
    return f"{BUTTON_PREFIX}manual-done:{stage}"


def build_manual_card(
    state: dict[str, Any], stage: str
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """`needs_manual` 出口卡片：用户手打一次，点「已完成」后 cursor 才前进。

    这不是为教学关特设的补丁——任何一关卡住都走它，队列就永远卡不死。
    """
    manual = state.get("needs_manual") or {}
    days = manual.get("days") if isinstance(manual, dict) else []
    day_text = "、".join(str(day) for day in days if day) or "—"
    reason = str(manual.get("reason") or "") if isinstance(manual, dict) else ""
    custom_id = manual_button_id(stage)
    label = entry_label(stage)
    embed = {
        "title": f"🙋 {label} 需要你手动打一次",
        "description": (
            f"通关队列在 {label} 卡住了，自动路径拿不到作业或导航不过去。\n"
            "请在游戏里手动把这一关打过，然后点下面的「已完成」，队列会从下一关继续。\n"
            "在你点掉之前，队列停在这里，日常会照常刷理智（不会白白溢出）。"
        ),
        "color": 0xFEE75C,
        "fields": [
            {"name": "失败日期", "value": day_text[:1024], "inline": False},
            {"name": "最后一次原因", "value": (reason or "—")[:1024], "inline": False},
        ],
        "timestamp": now_beijing().isoformat(),
    }
    components = [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 3,
                    "label": f"✅ {label} 已完成"[:80],
                    "custom_id": custom_id,
                }
            ],
        }
    ]
    entries = {
        custom_id: {
            "kind": "exec",
            "script": "maa_action.py",
            "args": ["clearing", "manual-done", stage],
        }
    }
    return embed, components, entries


def ex_gate_embed(reason: str) -> dict[str, Any]:
    return {
        "title": "⛔ EX 段未入队（等森空岛）",
        "description": (
            f"{reason}\n\n"
            "如果这是因为森空岛凭据过期，请在**本机终端**重跑：\n"
            "`python3 ~/.hermes/scripts/maa_skland.py link`\n"
            "（token 绝不要发到 Discord。）\n"
            "队列的普通关段不受影响；EX 是被闸门挡住的，不是打完了。"
        )[:3500],
        "color": 0xED4245,
        "timestamp": now_beijing().isoformat(),
    }


def unknown_sanity_embed(stages: list[str]) -> dict[str, Any]:
    listed = "、".join(stages[:8]) or "—"
    return {
        "title": "⏸️ 有关卡的理智消耗未知，队列暂停在这里",
        "description": (
            f"{listed} 的理智消耗当前算不出来（`stages.json` 没有 EX 关条目），"
            "等 PLAN_CLEARING 步骤 4 的 EX 理智 OCR 落地后自动恢复。\n"
            "**这不需要你做任何事**：cursor 没有前进，这关不会被跳过；"
            "也没有按钮要点。"
        )[:3500],
        "color": 0x99AAB5,
        "timestamp": now_beijing().isoformat(),
    }


def status_text(state: dict[str, Any] | None) -> str:
    if not isinstance(state, dict):
        return "通关队列：未建立（当前没有活动队列）。"
    stages = state.get("stages") or []
    cursor = int(state.get("cursor", 0))
    lines = [
        f"通关队列：{state.get('activity_title') or state.get('prefix') or '当前活动'}",
        f"进度：{min(cursor, len(stages))}/{len(stages)}",
    ]
    stage = current_stage(state)
    if stage:
        lines.append(f"当前关：{entry_label(stage)}")
    else:
        lines.append("当前关：已到队尾")
    manual = awaiting_manual(state)
    if manual:
        lines.append(f"⏳ 等你手动打完 {entry_label(manual)} 并点「已完成」")
    if stage and is_nav_frozen(state, stage):
        lines.append("❄️ 今日 nav 失败已冻结，明天再试")
    blocked = blocked_today(state)
    if blocked == "unknown_cost":
        lines.append("⏸️ 今日因理智消耗算不出来被堵住（等步骤 4），明天自动重试一次")
    elif blocked:
        lines.append(f"⏸️ 今日被堵住：{blocked}")
    blocked = state.get("ex_gate_blocked_reason")
    if blocked:
        lines.append(f"⛔ EX 段被闸门挡住：{blocked}")
    lines.append(f"活跃（日常让路）：{'是' if queue_is_active(state) else '否'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 步骤 4：编队界面 OCR 读理智（EX 关在 stages.json 里没有条目）
# ---------------------------------------------------------------------------

# 编队界面右下角「开始行动」旁边那个理智消耗数字（形如 `-21`）。ROI 是归一化的
# [x, y, w, h]，默认取右下角一块；**真机校准后**把实测值写进
# `config.clearing_sanity_ocr_roi`。ROI 故意不取整张图：顶部的「78/135」当前理智、
# 掉落物数量、代理次数都是数字，全图找会歧义。
DEFAULT_SANITY_OCR_ROI = (0.55, 0.70, 0.45, 0.30)
# 理智消耗在界面上带负号（`-21`）；Vision 偶尔把负号识成长横或漏掉，所以
# 无号数字也收，但**有号的优先**——两者同时出现时以带号的为准。
SANITY_OCR_TOKEN_RE = re.compile(r"^\s*(?P<sign>[-−–—])?\s*(?P<num>\d{1,3})\s*$")
SANITY_COST_MAX = 200


def parse_ocr_lines(text: str) -> list[tuple[str, tuple[int, int, int, int]]]:
    """ocrbox 每行打 `文本\\t[x,y,w,h]`（左上角像素坐标）；解析成 (文本, 框)。"""
    parsed: list[tuple[str, tuple[int, int, int, int]]] = []
    for raw in str(text or "").splitlines():
        if "\t" not in raw:
            continue
        label, _tab, box = raw.rpartition("\t")
        match = re.fullmatch(r"\s*\[(\d+),(\d+),(\d+),(\d+)\]\s*", box)
        if not match:
            continue
        parsed.append((label, tuple(int(part) for part in match.groups())))  # type: ignore[arg-type]
    return parsed


def sanity_ocr_roi(config: dict[str, Any] | None) -> tuple[float, float, float, float]:
    raw = (config or {}).get("clearing_sanity_ocr_roi")
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        try:
            values = tuple(float(part) for part in raw)
        except (TypeError, ValueError):
            return DEFAULT_SANITY_OCR_ROI
        if all(0.0 <= part <= 1.0 for part in values) and values[2] > 0 and values[3] > 0:
            return values  # type: ignore[return-value]
    return DEFAULT_SANITY_OCR_ROI


def png_size(path: Path) -> tuple[int, int] | None:
    """读 PNG 的 IHDR 拿宽高；不是 PNG 或读不到返回 None。"""
    try:
        with Path(path).open("rb") as handle:
            head = handle.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    width = int.from_bytes(head[16:20], "big")
    height = int.from_bytes(head[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def sanity_cost_from_ocr(
    lines: list[tuple[str, tuple[int, int, int, int]]],
    image_size: tuple[int, int],
    roi: tuple[float, float, float, float] = DEFAULT_SANITY_OCR_ROI,
) -> tuple[int | None, str]:
    """从 OCR 结果里取理智消耗。**歧义一律 None**（对齐 `maa_copilot._one_cost()`）。

    返回 `(cost | None, 说明)`。规则：
    - 只看框中心落在 ROI 里的文本；
    - 文本必须整体是「可选负号 + 1~3 位数字」（`78/135`、`x1`、`+3` 都不算）；
    - 有号候选优先；同一优先级里出现**两个不同的数**就放弃；
    - 0 或超过 `SANITY_COST_MAX` 视为没读到。
    读不出返回 None，**不许猜一个数**——理智算错会直接导致队列空跑。
    """
    width, height = image_size
    if width <= 0 or height <= 0:
        return None, "截图尺寸无效"
    rx, ry, rw, rh = roi
    x0, y0, x1, y1 = rx * width, ry * height, (rx + rw) * width, (ry + rh) * height
    signed: set[int] = set()
    bare: set[int] = set()
    seen: list[str] = []
    for label, (bx, by, bw, bh) in lines:
        cx, cy = bx + bw / 2, by + bh / 2
        if not (x0 <= cx <= x1 and y0 <= cy <= y1):
            continue
        seen.append(label)
        match = SANITY_OCR_TOKEN_RE.match(label)
        if not match:
            continue
        value = int(match.group("num"))
        if value <= 0 or value > SANITY_COST_MAX:
            continue
        (signed if match.group("sign") else bare).add(value)
    for bucket, kind in ((signed, "带负号"), (bare, "无号")):
        if len(bucket) == 1:
            value = next(iter(bucket))
            return value, f"ROI 内{kind}数字 {value}"
        if len(bucket) > 1:
            return None, f"ROI 内{kind}数字有歧义：{sorted(bucket)}"
    return None, f"ROI 内没有理智数字（看到：{seen[:6]}）"


def ocr_sanity_cost(
    png_path: Path,
    config: dict[str, Any] | None,
    *,
    ocrbox: Path | None = None,
    runner: Callable[..., Any] | None = None,
) -> tuple[int | None, str]:
    """对一张编队界面截图跑本机 Vision OCR（ocrbox），返回 `(理智 | None, 说明)`。

    任何环节失败（截图不是 PNG、ocrbox 未部署、OCR 崩溃、歧义）都返回 None。
    这是与不变量 #5b（日常战报截图的主页校验）**不同的一套用途**，不复用那道闸门。
    """
    import subprocess

    from maa_common import SCRIPTS_DIR

    run = runner or subprocess.run
    binary = ocrbox or (SCRIPTS_DIR / "ocrbox")
    size = png_size(Path(png_path))
    if size is None:
        return None, "截图不是可读的 PNG"
    if not Path(binary).is_file():
        return None, "ocrbox 未部署，读不出理智"
    try:
        result = run(
            [str(binary), str(png_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except Exception as exc:
        return None, f"OCR 失败：{type(exc).__name__}: {exc}"
    if getattr(result, "returncode", 1) != 0:
        return None, f"OCR 失败：{str(getattr(result, 'stderr', '') or '').strip()[:120]}"
    cost, detail = sanity_cost_from_ocr(
        parse_ocr_lines(getattr(result, "stdout", "") or ""), size, sanity_ocr_roi(config)
    )
    return cost, detail


def cached_sanity_cost(state: dict[str, Any], stage: str) -> int | None:
    """OCR 读到过的理智（按**普通关名**缓存；突袭与普通关同价）。

    存在独立的 `state["sanity_costs"]` 里而不是 `stage_state`：后者是失败记账，
    `advance_cursor()` 通关后会把它清掉，而理智缓存要留给紧跟着的突袭条目用。
    """
    costs = state.get("sanity_costs")
    if not isinstance(costs, dict):
        return None
    entry = costs.get(stage)
    if not isinstance(entry, dict):
        return None
    try:
        value = int(entry.get("cost"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def remember_sanity_cost(
    state: dict[str, Any], stage: str, cost: int, *, at: datetime | None = None
) -> dict[str, Any]:
    costs = state.setdefault("sanity_costs", {})
    if not isinstance(costs, dict):
        costs = {}
        state["sanity_costs"] = costs
    costs[stage] = {
        "cost": int(cost),
        "source": "ocr",
        "at": (at or now_beijing()).isoformat(),
    }
    return state


def resolve_cost(
    entry: str, state: dict[str, Any], lookup: Callable[[str], int | None]
) -> int | None:
    """队列排关用的理智：**`stages.json` 有真值时以它为准**，没有才用 OCR 缓存。

    `lookup` 就是 `stage_sanity_cost()` 那条路；OCR 只是 EX 的补充路径，
    不改普通关的既有行为。
    """
    stage, _mode = split_entry(entry)
    known = lookup(stage)
    if known is not None:
        return known
    return cached_sanity_cost(state, stage)


def probe_candidate(plan_result: dict[str, Any]) -> str:
    """这一轮该去编队界面 OCR 的关：`plan_round()` 撞上的第一个理智未知关。"""
    unknown = plan_result.get("unknown_cost") or []
    return str(unknown[0]) if unknown else ""
