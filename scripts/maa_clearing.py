#!/usr/bin/env python3
"""作业链的领域层（四期步骤 2）。

角色对齐 ``maa_copilot.py``：这里只有近纯函数——条目形态（普通 / 突袭）、
上游导航键闸门、理智预算与排轮、失败记账、通关判据、去重通知。
真正开模拟器执行的是 ``maa_copilot_worker.py``（**不新建第二个 worker**）。

三期的「通关队列」在四期步骤 2 被拆掉：``clearing.json`` 状态文件、「需要你手动打
一次」出口、EX 开放闸门、编队界面理智 OCR 全部删除（D15）。留下来的这些函数由
``maa_activity_watch.py``（排轮）与 worker（执行、记账）共用。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from maa_common import BEIJING, now_beijing

# 剿灭同样吃理智（地基事实 H）。国服剿灭作战单次 25 理智；写成可配是因为
# 这个数字属于游戏侧常量，改版时不该改代码。
DEFAULT_ANNIHILATION_SANITY = 25


def today_key(at: datetime | None = None) -> str:
    """`Asia/Shanghai` 自然日。搜索失败退避的记账按它计（不是游戏日 04:00）。"""
    moment = at or now_beijing()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BEIJING)
    return moment.astimezone(BEIJING).date().isoformat()


# ---------------------------------------------------------------------------
# 条目形态：关卡名 + 模式
# ---------------------------------------------------------------------------

# 条目的突袭表示：**关卡名 + `#raid` 后缀**。
# 选后缀而不是带 mode 的字典，是因为一轮的条目表保持成一串纯字符串：
# `plan_round()` 的排轮、理智查表、Discord 的 custom_id 都直接拼得进去。
# **除了执行时的 `--raid` 取值，其余一切（理智查表、导航、状态展示）都用
# `split_entry()` 去掉后缀之后的关卡名。**
RAID_SUFFIX = "#raid"


def split_entry(entry: str) -> tuple[str, str]:
    """轮次条目 → `(关卡名, mode)`，mode ∈ {"normal", "raid"}。"""
    text = str(entry or "")
    if text.endswith(RAID_SUFFIX):
        return text[: -len(RAID_SUFFIX)], "raid"
    return text, "normal"


def make_entry(stage: str, mode: str) -> str:
    """`(关卡名, mode)` → 条目。`split_entry()` 的逆。"""
    return f"{stage}{RAID_SUFFIX}" if mode == "raid" else str(stage)


def entry_label(entry: str) -> str:
    """给人看的名字：`SR-EX-1（突袭）`。"""
    stage, mode = split_entry(entry)
    return f"{stage}（突袭）" if mode == "raid" else stage


def stage_nav_prefix(stage: str) -> str:
    """关卡名 → 活动前缀 / Stages 文件名前缀：`SR-5` → `SR`，`SR-EX-1` → `SR`。"""
    text = str(stage or "").strip()
    return text.split("-", 1)[0] if text else ""


# 突袭切换那一步用的上游任务名。`config.copilot_raid_switch_task`
# **空串 = 突袭功能整体关闭**（不只是「跳过切换」）：单文件
# `maa copilot <uri> --raid raid` 根本不会去切难度（`is_raid` 只在 `copilot_list`
# 的条目里有意义），不切就打等于**拿普通难度冒充突袭通关**。所以开关为空时
# 突袭条目从排轮起就不产生，worker 那边再兜一道防线。
DEFAULT_RAID_SWITCH_TASK = "ChangeToRaidDifficulty"


def raid_switch_task(config: dict[str, Any] | None) -> str:
    """配置里的突袭切换任务名；空串 = 功能关闭。

    键**缺失**时取默认值（与 `maa_copilot_worker.switch_to_raid()` 同一套取值口径），
    键存在但为空串 / 空白才算关闭。`config` 不是字典一律当关闭（fail-closed）。
    """
    if not isinstance(config, dict):
        return ""
    return str(config.get("copilot_raid_switch_task", DEFAULT_RAID_SWITCH_TASK) or "").strip()


def raid_enabled(config: dict[str, Any] | None) -> bool:
    """突袭功能的总开关。**不传 config = 关闭**：宁可少排一个模式，也不许错记通关。"""
    return bool(raid_switch_task(config))


def required_modes(
    stage: str, activity_prefix: str, config: dict[str, Any] | None = None
) -> tuple[str, ...]:
    """这一关要打**哪些模式**才算通关（D7）。

    当期活动关（前缀相同：数字关、EX、S）是 `normal + raid`——prts 的
    `content.difficulty` 全为空（地基事实 F），判不出「这份作业支不支持突袭」，
    所以同一份作业先普通后突袭都打一遍。资源本 / 主线没有突袭，只有 `normal`。

    **突袭功能关掉时（`copilot_raid_switch_task` 为空）活动关也只返回
    `("normal",)`**：排轮与手动路径都不再产生 `raid` 条目，这一关只要普通模式
    拿到 `Copilot Completed` 就算通关。
    """
    prefix = str(activity_prefix or "")
    if prefix and stage_nav_prefix(stage) == prefix:
        return ("normal", "raid") if raid_enabled(config) else ("normal",)
    return ("normal",)


# ---------------------------------------------------------------------------
# 上游导航键闸门（2026-09-05 范围变更：**不再自写导航定义**）
# ---------------------------------------------------------------------------

# 自动作业只跑上游 `tasks/Stages/<前缀>.json` 里**已有导航键**的关（D8）。
# 原计划的「导航定义生成器」整段取消，于是「上游没写导航定义」这件事**永远不会
# 自动变好**：这样的关既不进目标集合、也不进下拉，只在一览里标「⛔ 上游无导航」。
#
# 只读**上游** resource 目录（`maa_copilot._resource_dirs()` 给的那几个）。
# 故意不读也不建 `~/.config/maa-cli/resource/tasks/Stages/`：本项目不再往那里
# 写自造的导航定义，读它只会让「上游有没有这关」这个判断失真。
NAV_STAGES_SUBDIR = ("tasks", "Stages")


def read_nav_keys(
    resource_dirs: Iterable[Path], prefix: str
) -> tuple[set[str], str]:
    """读上游 `tasks/Stages/<prefix>.json` 的**顶层键**，返回 `(键集, 异常摘要)`。

    文件不存在 / 读失败 / JSON 坏 / 顶层不是对象 → 一律**视为无键**（fail-closed）
    并把异常摘要带出去，写进卡片，绝不当成「有导航定义」。
    """
    prefix = str(prefix or "")
    if not prefix:
        return set(), "关卡名没有前缀，定位不到 Stages 文件"
    keys: set[str] = set()
    errors: list[str] = []
    parsed = False
    for resource_dir in resource_dirs:
        path = (Path(resource_dir) / "tasks" / "tasks.json" if prefix == "tasks"
                else Path(resource_dir).joinpath(*NAV_STAGES_SUBDIR, f"{prefix}.json"))
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            errors.append(f"{path} 不存在")
            continue
        except (OSError, ValueError) as exc:
            errors.append(f"{path} 读不出来：{type(exc).__name__}: {exc}")
            continue
        if not isinstance(data, dict):
            errors.append(f"{path} 顶层不是对象")
            continue
        parsed = True
        keys.update(str(key) for key in data)
    if parsed:
        return keys, ""
    return set(), "；".join(errors)[:300] or f"没找到 tasks/Stages/{prefix}.json"


def nav_checker(
    resource_dirs: Iterable[Path],
) -> Callable[[str], tuple[bool, str]]:
    """按前缀缓存的导航键判定器：`(entry) -> (上游有导航定义吗, 没有的话为什么)`。

    每个前缀只 open 一次文件（一轮里几十个条目共用同一份缓存），调用方每轮建一次。
    **突袭条目按普通关名查**（`split_entry()`），与理智查表一致。
    """
    dirs = [Path(path) for path in resource_dirs]
    cache: dict[str, tuple[set[str], str]] = {}

    def check(entry: str) -> tuple[bool, str]:
        stage, _mode = split_entry(entry)
        prefix = stage_nav_prefix(stage)
        # Supplies and chapter navigation are provided upstream outside activity files.
        if prefix in {"CE", "LS", "AP", "CA", "SK", "PR"}:
            prefix = "Supplies"
        main = re.fullmatch(r"[A-Za-z]{0,3}(\d{1,2})-\d{1,2}", stage)
        if main:
            prefix = "tasks"
        if prefix not in cache:
            cache[prefix] = read_nav_keys(dirs, prefix)
        keys, error = cache[prefix]
        if stage in keys or (main and f"Episode{main[1]}" in keys):
            return True, ""
        if error:
            return False, f"上游没有 {stage} 的导航定义：{error}"
        return False, f"上游 tasks/Stages/{prefix}.json 里没有 {stage} 这个顶层键"

    return check


# ---------------------------------------------------------------------------
# 理智预算
# ---------------------------------------------------------------------------

def annihilation_reserve(plan: dict[str, Any], config: dict[str, Any]) -> int:
    """今天还要打的剿灭要预留多少理智（地基事实 H）。

    剿灭是 `daily.toml` 里的第二个 `Fight(stage=Annihilation)`，**它同样消耗理智**。
    排关前必须先扣掉这份，否则会把可用理智算多、排出打不完的一轮。
    `plan["annihilation"]` 由 `maa_daily.attach_annihilation()` 按游戏日记帐；
    今天已经刷过就是 0。
    """
    if not isinstance(plan, dict) or not plan.get("annihilation"):
        return 0
    if not bool((plan.get("tasks") or {}).get("fight", True)):
        return 0
    try:
        cost = int(config.get("annihilation_sanity", DEFAULT_ANNIHILATION_SANITY))
    except (TypeError, ValueError):
        cost = DEFAULT_ANNIHILATION_SANITY
    return max(0, cost)


def sanity_budget(
    available: int | None,
    plan: dict[str, Any],
    config: dict[str, Any],
) -> int:
    """这一轮能花的理智 = 当前理智 − 剿灭那份。理智未知一律 0（fail-closed）。"""
    if available is None:
        return 0
    try:
        current = int(available)
    except (TypeError, ValueError):
        return 0
    return max(0, current - annihilation_reserve(plan, config))


def plan_round(
    entries: Iterable[str],
    budget: int,
    cost_of: Callable[[str], int | None],
) -> dict[str, Any]:
    """从有序条目表里排出这一轮打得起的条目。

    返回 `{"entries": [...], "spent": int, "unknown_cost": [...], "stop_reason": str}`。

    理智成本未知的条目（`stage_sanity_cost()` 对 EX 返回 `None`）：
    **不排入当轮、不计入理智预算**——那不是「这关打完了」，是「现在算不出来」。
    不许拿 0 顶替、不许拿同活动普通关的值猜：理智算错会直接导致空跑。
    """
    remaining = max(0, int(budget))
    picked: list[str] = []
    unknown: list[str] = []
    stop_reason = "queue_end"
    for raw in entries:
        entry = str(raw)
        cost = cost_of(entry)
        try:
            cost = int(cost) if cost is not None else None
        except (TypeError, ValueError):
            cost = None
        if cost is None:
            unknown.append(entry)
            stop_reason = "unknown_cost"
            break
        if cost <= 0 or cost > remaining:
            stop_reason = "sanity"
            break
        picked.append(entry)
        remaining -= cost
    return {
        "entries": picked,
        "spent": max(0, int(budget)) - remaining,
        "unknown_cost": unknown,
        "stop_reason": stop_reason,
    }


# ---------------------------------------------------------------------------
# 失败记账（搜索侧：练度不满足 / 暂无作业的退避）
# ---------------------------------------------------------------------------

def _stage_entry(state: dict[str, Any], stage: str) -> dict[str, Any]:
    stage_state = state.setdefault("stage_state", {})
    if not isinstance(stage_state, dict):
        stage_state = {}
        state["stage_state"] = stage_state
    entry = stage_state.get(stage)
    if not isinstance(entry, dict):
        entry = {"failed_dates": [], "last_error": ""}
        stage_state[stage] = entry
    if not isinstance(entry.get("failed_dates"), list):
        entry["failed_dates"] = []
    return entry


def record_stage_failure(
    state: dict[str, Any],
    stage: str,
    *,
    kind: str,
    detail: str = "",
    at: datetime | None = None,
    reason_code: str = "",
    gaps: Iterable[str] | None = None,
) -> dict[str, Any]:
    """记一次失败到 `state["stage_state"][stage]`，返回同一个 `state`。

    **按「不同日期」记，不按尝试次数记**：同一天失败 5 次只算 1 天。`last_failed_at`
    是退避的唯一依据（`activity_search_retry_minutes`），`gaps` 让卡片能写出
    「🚫 练度不满足（缺：…）」而不是一句笼统的失败。
    """
    entry = _stage_entry(state, stage)
    day = today_key(at)
    if day not in entry["failed_dates"]:
        entry["failed_dates"].append(day)
    entry["kind"] = str(kind)[:40]
    entry["last_error"] = str(detail)[:400]
    entry["last_failed_at"] = (at or now_beijing()).isoformat()
    if reason_code:
        entry["reason_code"] = str(reason_code)[:60]
    entry["gaps"] = [str(gap)[:80] for gap in (gaps or [])][:6]
    return state


def clear_stage_failure(state: dict[str, Any], stage: str) -> dict[str, Any]:
    """这一关重新搜到作业了，把退避记账抹掉。"""
    stage_state = state.get("stage_state")
    if isinstance(stage_state, dict):
        stage_state.pop(stage, None)
    return state


def minutes_since_failure(
    state: dict[str, Any], stage: str, at: datetime | None = None
) -> float:
    """距上次搜索失败多少分钟；没失败过返回 `inf`（= 可以立刻搜）。"""
    stage_state = state.get("stage_state")
    entry = stage_state.get(stage) if isinstance(stage_state, dict) else None
    if not isinstance(entry, dict):
        return float("inf")
    try:
        last = datetime.fromisoformat(str(entry.get("last_failed_at") or ""))
    except (TypeError, ValueError):
        return float("inf")
    return max(0.0, ((at or now_beijing()) - last).total_seconds() / 60)


# ---------------------------------------------------------------------------
# 去重通知
# ---------------------------------------------------------------------------

def should_notify(state: dict[str, Any], key: str, reason: str) -> bool:
    """同一原因不刷屏；原因变了要重新发。"""
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

    **为什么不能只看退出码**：记账一旦写成「通关」这一关就永远不会再排，
    撤退、三星未达、进程正常退出的 Fight Error 全都会被算成通关，而且没有人知道。

    胜利标记 `Copilot Completed` **已于 2026-09-08 用真实日志核对**
    （`runs/2026-09-08-copilot-9e14e500ac25.log` 第 47 行）。认不出胜利标记时
    仍然 fail-closed 返回 `unknown`，调用方按失败处理、不记通关。
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
        "本次不记通关（宁可重打，也不静默跳关）。"
        "请核对 runs/ 下的日志，若胜利标记不是这个字符串就改 "
        "maa_clearing.COPILOT_VICTORY_MARKERS。",
    )
