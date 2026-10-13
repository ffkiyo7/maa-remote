#!/usr/bin/env python3
"""活动上线后自动把「上游有导航定义的关」抄一遍（四期步骤 2b / 2c）。

每 15 分钟一 tick，做四件事：

1. 读 `maa activity Official`，判断活动是否轮换 / 结束（沿用三期的空窗闸门）；
2. 排目标集合：当期前缀在本地关卡索引里的全部关 ∩ 上游导航键 − 已通关 − 退避中；
3. 按理智预算排出这一轮的 `(关, 模式)` 条目，写 `searches/<token>.json` 并发一张
   「已排程」卡（只有一个「取消本轮」按钮）；
4. 到点、不在 blackout、`run.lock` 空闲、模拟器离线时派 `run-batch`。

**静默执行、事后汇报**（D11）：不等用户确认，跑完发一条多关战报。
"""

from __future__ import annotations

import fcntl
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import maa_action
import maa_clearing
import maa_copilot
import maa_farming
import maa_plan_card
from maa_common import (
    LAST_RUN_PATH,
    STATE_DIR,
    atomic_write_json,
    discord_request,
    discord_token,
    load_config,
    load_json,
    now_beijing,
    retire_discord_components,
    state_lock,
    WATCH_PATH,
)


WATCH_LOCK_PATH = STATE_DIR / "activity_watch.lock"
# Consecutive empty snapshots required before a prepared round is retired.
EMPTY_OBSERVATIONS_BEFORE_IDLE = 2
# 一轮里最多现搜多少关：搜一关要 5~20s，无上限会让一次 cron 拖到十几分钟。
# 排不下的关下一 tick 继续，不会丢。
MAX_SEARCHES_PER_TICK = 6


def _search_path(token: str) -> Path:
    return maa_copilot.SEARCHES_DIR / f"{token}.json"


def _notify(config: dict[str, Any], title: str, description: str, color: int) -> None:
    channel_id = str(config.get("channel_id") or "")
    if not channel_id.isdigit():
        raise RuntimeError("config.json 中尚未配置 Discord channel_id")
    discord_request(
        discord_token(),
        "POST",
        f"/channels/{channel_id}/messages",
        {
            "embeds": [
                {
                    "title": title[:256],
                    "description": description[:1500],
                    "color": color,
                    "timestamp": now_beijing().isoformat(),
                }
            ],
            "allowed_mentions": {"parse": []},
        },
    )


def _supersede_round(token: str, config: dict[str, Any]) -> bool:
    """作废一轮还没开打的条目并收掉它的按钮。"""
    if not token:
        return False
    changed, message_id = maa_copilot.supersede_search(token)
    if not changed:
        return False
    try:
        retire_discord_components(config, message_id)
    except Exception as exc:
        print(
            f"旧活动卡按钮收起失败但已经失效：{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
    return True


def _save(state: dict[str, Any], at: datetime) -> dict[str, Any]:
    state["updated_at"] = at.isoformat()
    atomic_write_json(WATCH_PATH, state)
    return state


# ---------------------------------------------------------------------------
# 2b 目标集合
# ---------------------------------------------------------------------------

def collect_targets(
    config: dict[str, Any],
    state: dict[str, Any],
    prefix: str,
    index: dict[str, Any],
    ledger: dict[str, Any],
    roster: dict[str, Any] | None,
    at: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`(要打的关, 排除掉的关及原因)`，顺序已按「数字 → EX → S」排好。

    排除的四种理由各自的语义不同，卡片要分开显示：

    - `no_nav`：上游没写导航定义（D8 / 不变量 #37）——**永远不会自动变好**；
    - `not_indexed`：有导航键但本地 Tile-Pos 索引里没有，进去也会被拒；
    - `zone_unopened`：森空岛该活动的 zone `totalStage == 0`（D9）——等热更新，不记失败；
    - `cleared`：记账齐全或 zone 全清（D10）；
    - `daily_cap`：今天已经打满 `auto_copilot_max_attempts_per_day` 次还没通，明天再说；
    - `backoff`：本轮搜过、练度不满足 / 暂无作业，还没到重试点。
    """
    nav_of = maa_clearing.nav_checker(maa_copilot._resource_dirs(config))
    retry_minutes = max(5, int(config.get("activity_search_retry_minutes", 30)))
    targets: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for code in maa_copilot.activity_stage_codes(index, prefix):
        supported, detail = nav_of(code)
        if not supported:
            excluded.append({"stage": code, "reason": "no_nav", "detail": detail})
            continue
        resolved = maa_copilot.resolve_copilot_stage(code, index)
        if resolved is None:
            excluded.append({"stage": code, "reason": "not_indexed", "detail": ""})
            continue
        stage_id = resolved[0]
        if maa_copilot.zone_is_unopened(roster, stage_id):
            excluded.append({"stage": code, "reason": "zone_unopened", "detail": ""})
            continue
        modes = maa_copilot.ledger_pending_modes(
            ledger,
            roster,
            code,
            stage_id,
            maa_clearing.required_modes(code, prefix, config),
        )
        if not modes:
            excluded.append({"stage": code, "reason": "cleared", "detail": ""})
            continue
        capped = maa_copilot.daily_capped_modes(ledger, code, modes, config, at)
        if capped:
            # 退避管的是「多久之后再试」，管不住「一天试几次」：2026-09-09 那两轮
            # 隔 30 分钟就把同一批关原样重打了一遍。到上限的模式一个都不排，
            # 整关留到明天——次日 `attempts_today()` 自然归零，不用清任何状态。
            excluded.append(
                {
                    "stage": code,
                    "reason": "daily_cap",
                    "detail": "；".join(
                        f"{maa_clearing.entry_label(maa_clearing.make_entry(code, mode))}"
                        f" 今天已尝试 {used} 次"
                        for mode, used in capped
                    ),
                }
            )
            continue
        if maa_clearing.minutes_since_failure(state, code, at) < retry_minutes:
            entry = (state.get("stage_state") or {}).get(code) or {}
            excluded.append(
                {"stage": code, "reason": "backoff", "detail": str(entry.get("last_error") or "")}
            )
            continue
        targets.append({"stage": code, "stage_id": stage_id, "modes": modes})
    return targets, excluded


# ---------------------------------------------------------------------------
# 2c 理智预算与排轮
# ---------------------------------------------------------------------------

def round_budget(
    config: dict[str, Any], roster: dict[str, Any] | None, at: datetime
) -> tuple[int, int | None]:
    """`(可花理智, 估算出的当前理智)`。理智未知一律 0（fail-closed）。"""
    from maa_daily import attach_annihilation
    from maa_common import effective_plan

    last_run = load_json(LAST_RUN_PATH, {})
    plan = attach_annihilation(
        effective_plan(at), at, last_run if isinstance(last_run, dict) else {}
    )
    snapshot = (roster or {}).get("sanity")
    sanity = maa_copilot.estimated_sanity(
        snapshot if isinstance(snapshot, dict) else {}, at
    )
    return maa_clearing.sanity_budget(sanity, plan, config), sanity


def cost_lookup(config: dict[str, Any], index: dict[str, Any]):
    """条目 → 理智消耗。突袭条目按**普通关名**查（不变量 #34）。

    两级查表，仍然 fail-closed：先问上游 `stages.json`
    （`maa_copilot.stage_sanity_cost()`），它拿不到就问一图流 `stage/info`
    的**本地缓存**（`maa_farming.stage_ap_cost()`）。第二级是为 **EX 关**加的：
    上游 `stages.json` 里根本没有 `XX-EX-n` 的条目（步骤 2 留的坑），
    以前这类条目一律算不出理智、直接被排轮丢掉（不变量 #28）。
    两处都查不到就还是 `None`——**不猜**。
    """

    def cost_of(entry: str) -> int | None:
        stage, _mode = maa_clearing.split_entry(entry)
        resolved = maa_copilot.resolve_copilot_stage(stage, index)
        if resolved is None:
            return None
        cost = maa_copilot.stage_sanity_cost(resolved[0], resolved[1], config)
        if cost is not None:
            return cost
        try:
            return maa_farming.stage_ap_cost(resolved[0], config)
        except Exception:  # noqa: BLE001 - 兜底查表不该让整轮排程崩掉
            return None

    return cost_of


def _entry_list(targets: list[dict[str, Any]]) -> list[str]:
    """目标集合 → 有序条目表：每关先 `normal` 后 `raid`。"""
    return [
        maa_clearing.make_entry(target["stage"], mode)
        for target in targets
        for mode in target["modes"]
    ]


def build_round(
    config: dict[str, Any],
    state: dict[str, Any],
    targets: list[dict[str, Any]],
    index: dict[str, Any],
    roster: dict[str, Any] | None,
    at: datetime,
) -> tuple[list[dict[str, Any]], str, str]:
    """排出这一轮：`(条目, 预计开始时刻, 说明)`。

    排得满就现在打；排不满（理智不够）就只排**第一条**并算出它的 `ready_at`——
    多排也没用，理智恢复到能打第二条时早就是下一 tick 了。
    搜不到作业的关就地记账退避，并把它从这一轮剔掉后重排（D12：绝不降级）。
    """
    cost_of = cost_lookup(config, index)
    budget, sanity = round_budget(config, roster, at)
    remaining = list(targets)
    searched = 0
    while remaining and searched < MAX_SEARCHES_PER_TICK:
        plan = maa_clearing.plan_round(_entry_list(remaining), budget, cost_of)
        # 排不满时只排**第一个理智算得出来的**条目：理智算不出来的关（EX 还没进
        # `stages.json`）排进去只会在 worker 里立刻被复核拒掉，每 tick 白发一张卡。
        picked = plan["entries"] or [
            entry for entry in _entry_list(remaining) if cost_of(entry) is not None
        ][:1]
        if not picked:
            return [], "", ""
        stages: list[str] = []
        for entry in picked:
            stage, _mode = maa_clearing.split_entry(entry)
            if stage not in stages:
                stages.append(stage)
        entries: list[dict[str, Any]] = []
        dropped = ""
        for target in remaining:
            if target["stage"] not in stages:
                continue
            candidate = target.get("candidate")
            if candidate is None:
                searched += 1
                candidate, reason, gaps = maa_copilot.pick_perfect_candidate(
                    target["stage"], target["stage_id"], config, roster
                )
                if candidate is None:
                    maa_clearing.record_stage_failure(
                        state,
                        target["stage"],
                        kind="op",
                        detail=reason,
                        at=at,
                        gaps=gaps,
                    )
                    dropped = target["stage"]
                    break
                target["candidate"] = candidate
            for entry in picked:
                stage, mode = maa_clearing.split_entry(entry)
                if stage != target["stage"]:
                    continue
                entries.append(
                    {
                        "stage": stage,
                        "stage_id": target["stage_id"],
                        "mode": mode,
                        "candidate": candidate,
                        "sanity_cost": cost_of(entry),
                    }
                )
        if dropped:
            remaining = [t for t in remaining if t["stage"] != dropped]
            continue
        if not entries:
            return [], "", ""
        for target in remaining:
            if target["stage"] in stages:
                maa_clearing.clear_stage_failure(state, target["stage"])
        if plan["entries"]:
            return entries, at.isoformat(), ""
        # 预算一条都排不满：把第一条排到理智够的时刻（D11）。
        preflight = maa_copilot.sanity_preflight(
            roster, entries[0].get("sanity_cost"), at
        )
        ready_at = str(preflight.get("ready_at") or "")
        note = (
            f"当前理智 {sanity if sanity is not None else '未知'}，"
            f"不足以打 {entries[0]['stage']}，到点自动开跑。"
            if ready_at
            else "理智不足且算不出恢复时刻，下一 tick 再看。"
        )
        return entries[:1], ready_at, note
    return [], "", ""


# ---------------------------------------------------------------------------
# 派发闸门
# ---------------------------------------------------------------------------
# 闸门本身住在 `maa_action.dispatch_blocker()`（不变量 #13 / #36：只有一份实现），
# 自动与手动两条路共用。这里只负责「被挡了怎么记状态」。


def _dispatch(state: dict[str, Any], token: str, config: dict[str, Any], at: datetime) -> str:
    """派发一轮；返回状态说明。"""
    blocker = maa_action.dispatch_blocker(config, str(state.get("scheduled_at") or ""), at)
    if blocker:
        if maa_clearing.should_notify(state, "dispatch_blocked", blocker):
            print(f"activity watch: 本轮暂不派发（{blocker}）", flush=True)
        return "scheduled"
    maa_clearing.clear_notification(state, "dispatch_blocked")
    with state_lock():
        search = load_json(_search_path(token), None)
        if not isinstance(search, dict) or search.get("status") != "scheduled":
            return "scheduled"
        search["status"] = "queued"
        search["execution_requested_at"] = at.isoformat()
        atomic_write_json(_search_path(token), search)
    maa_action.dispatch_batch(token)
    return "dispatched"


# ---------------------------------------------------------------------------
# 开打前环境故障的当日上限
# ---------------------------------------------------------------------------
# `daily_cap` 按「(关, 模式) 的记账」数，而开打前就失败的轮一关都没进、不记账，
# 永远数不到上限。这里另记一本按天的流水账，到上限当天不再排。

DEFAULT_PREFIGHT_FAILURE_CAP = 2


def _prefight_cap(config: dict[str, Any]) -> int:
    try:
        return max(1, int(config.get(
            "auto_copilot_max_prefight_failures_per_day", DEFAULT_PREFIGHT_FAILURE_CAP
        )))
    except (TypeError, ValueError):
        return DEFAULT_PREFIGHT_FAILURE_CAP


def _count_prefight_failure(
    config: dict[str, Any], state: dict[str, Any], detail: str, at: datetime
) -> None:
    today = at.date().isoformat()
    record = state.get("prefight_failures")
    if not isinstance(record, dict) or record.get("date") != today:
        record = {"date": today, "count": 0}
    record["count"] = int(record.get("count") or 0) + 1
    record["last_error"] = detail[:400]
    state["prefight_failures"] = record
    cap = _prefight_cap(config)
    if record["count"] == cap:
        # 先落状态再发：Discord 挂了不能让下一 tick 重复计数。
        _save(state, at)
        _notify(
            config,
            "⛔ 自动作业今天不再排轮",
            f"开打前连续失败 {record['count']} 次（还没进关、没花理智）。"
            f"明天自动恢复；修好后想立刻恢复，说「恢复自动作业」。\n最近一次：{detail[:600]}",
            0xED4245,
        )


def _prefight_blocked(config: dict[str, Any], state: dict[str, Any], at: datetime) -> str:
    record = state.get("prefight_failures")
    if not isinstance(record, dict) or record.get("date") != at.date().isoformat():
        return ""
    if int(record.get("count") or 0) < _prefight_cap(config):
        return ""
    return f"今天开打前已失败 {record['count']} 次：{record.get('last_error', '')}"[:400]


# ---------------------------------------------------------------------------
# 一 tick
# ---------------------------------------------------------------------------

def run_once() -> dict[str, Any]:
    config = load_config()
    snapshot = maa_plan_card.query_activity_snapshot(config)
    target = maa_plan_card.activity_target(snapshot)
    state = load_json(WATCH_PATH, {})
    if not isinstance(state, dict):
        raise RuntimeError("activity_watch.json 格式错误")
    state.pop("probe_error", None)
    now = now_beijing()

    if target is None:
        return _go_idle(config, state, snapshot, now)

    maa_plan_card.persist_activity_snapshot(config, snapshot)
    state.pop("empty_observations", None)

    if state.get("activity_key") != target["key"]:
        _supersede_round(str(state.get("batch_token") or ""), config)
        _ledger, note = maa_copilot.ensure_ledger(target["key"])
        state = {
            "activity_key": target["key"],
            "activity_title": target["title"],
            "prefix": maa_copilot._activity_prefix(target["key"]),
            "status": "searching",
            "stage_state": {},
            "notified": {},
        }
        # 先落状态再发消息：Discord 挂了不能让下一 tick 把「换活动」重放一遍
        # （归档已经做过了，重放只会多发一条通知）。
        _save(state, now)
        _notify(
            config,
            f"🆕 {target['title']} 已上线",
            "开始自动匹配上游写了导航定义的关（普通 + 突袭），"
            "排到就静默执行、跑完发战报。" + (f"\n{note}。" if note else ""),
            0x5865F2,
        )

    token = str(state.get("batch_token") or "")
    if token:
        search = load_json(_search_path(token), None)
        status = str(search.get("status") or "") if isinstance(search, dict) else "missing"
        if status == "executing":
            state["status"] = "running"
            return _save(state, now)
        if status == "queued":
            restored = maa_copilot.recover_stale_dispatch(token, search)  # type: ignore[arg-type]
            if restored is None:
                state["status"] = "dispatched"
                return _save(state, now)
            _notify(
                config,
                "⚠️ 自动作业未启动，已放回排程",
                str(restored.get("last_prepare_error") or ""),
                0xFEE75C,
            )
            status = "scheduled"
        if status == "scheduled":
            if maa_copilot.auto_pause_state():
                _supersede_round(token, config)
                state.pop("batch_token", None)
                state.pop("scheduled_at", None)
                state["status"] = "paused"
                return _save(state, now)
            state["status"] = _dispatch(state, token, config, now)
            return _save(state, now)
        # 终态（executed / execution_failed / canceled / superseded / failed / missing）：
        # 这一轮结束了，下面按最新记账重新排。
        if isinstance(search, dict) and search.get("prefight_error"):
            _count_prefight_failure(config, state, str(search["prefight_error"]), now)
        state.pop("batch_token", None)
        state.pop("scheduled_at", None)

    if maa_copilot.auto_pause_state():
        state["status"] = "paused"
        return _save(state, now)
    blocked = _prefight_blocked(config, state, now)
    if blocked:
        state["status"] = "blocked"
        state["blocked_reason"] = blocked
        return _save(state, now)
    state.pop("blocked_reason", None)

    index = maa_copilot.load_stage_index(config)
    prefix = str(state.get("prefix") or maa_copilot._activity_prefix(target["key"]))
    ledger, _note = maa_copilot.ensure_ledger(target["key"])
    roster = maa_copilot.load_roster()
    targets, excluded = collect_targets(
        config, state, prefix, index, ledger, roster, now
    )
    if targets:
        # 森空岛只在「有未执行的关」的 tick 刷新：练度与 zone 进度决定这一轮排什么，
        # 拿一份 7 天前的快照排轮会漏关也会重抄。
        try:
            import maa_skland

            maa_skland.refresh_roster()
        except Exception as exc:
            # **读不到就不排**（fail-closed）。以前是「沿用缓存照排」：2026-10-11
            # 森空岛过期一个月，缓存里根本没有当期活动的 zone 进度，YW-4 没解锁也
            # 被排上，每 15 分钟开一次模拟器。登录过期由计划卡每天提醒一次
            # （`maa_skland.daily_health_check()`），这里只在原因变化时发一条。
            detail = f"{type(exc).__name__}: {exc}"
            print(f"森空岛刷新失败，本轮不排：{detail}", file=sys.stderr)
            if maa_clearing.should_notify(state, "skland", detail):
                _notify(
                    config,
                    "⛔ 自动作业暂停排轮：森空岛读取失败",
                    f"读不到练度与活动进度，不知道哪些关已解锁，先不排。\n{detail}",
                    0xED4245,
                )
            state["status"] = "blocked"
            state["blocked_reason"] = "森空岛读取失败"
            state["excluded"] = excluded[:40]
            return _save(state, now)
        maa_clearing.clear_notification(state, "skland")
        roster = maa_copilot.load_roster()
        targets, excluded = collect_targets(
            config, state, prefix, index, ledger, roster, now
        )

    if not targets:
        state["status"] = "done"
        state["excluded"] = excluded[:40]
        return _save(state, now)

    entries, scheduled_at, note = build_round(
        config, state, targets, index, roster, now
    )
    if not entries:
        state["status"] = "waiting"
        state["excluded"] = excluded[:40]
        return _save(state, now)

    token = maa_action.new_round_token()
    maa_copilot.ensure_private_dirs()
    atomic_write_json(
        _search_path(token),
        {
            "token": token,
            "status": "scheduled",
            "source": "activity-watch",
            "activity_key": target["key"],
            "activity_title": target["title"],
            "entries": entries,
            "scheduled_at": scheduled_at,
            "created_at": now.isoformat(),
            "expires_at": (now + maa_copilot.SEARCH_TTL).isoformat(),
            "message_id": None,
        },
    )
    message_id = maa_action.publish_round(
        token,
        entries,
        config,
        title="🤖 自动作业已排程",
        scheduled_at=scheduled_at,
        note=note,
        pause_button=True,
    )
    with state_lock():
        search = load_json(_search_path(token), None)
        if isinstance(search, dict):
            search["message_id"] = message_id
            atomic_write_json(_search_path(token), search)
    state["batch_token"] = token
    state["scheduled_at"] = scheduled_at
    state["excluded"] = excluded[:40]
    state["status"] = _dispatch(state, token, config, now)
    return _save(state, now)


def _go_idle(
    config: dict[str, Any],
    state: dict[str, Any],
    snapshot: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """活动结束：连续两次空快照才真的收摊（不变量 #20）。"""
    retired_title = ""
    if state.get("activity_key"):
        empty_observations = int(state.get("empty_observations") or 0) + 1
        if empty_observations < EMPTY_OBSERVATIONS_BEFORE_IDLE:
            state["empty_observations"] = empty_observations
            return _save(state, now)
        retired_title = str(state.get("activity_title") or "当前活动")
        _supersede_round(str(state.get("batch_token") or ""), config)
    maa_plan_card.persist_activity_snapshot(config, snapshot)
    idle = _save({"activity_key": "", "status": "idle"}, now)
    # 先落状态：Discord 失败不能让下一 tick 重放一次作废。
    if retired_title:
        _notify(
            config,
            "🏁 活动已结束",
            f"{retired_title} 已从 MAA 活动视图消失；未打完的一轮已收起，活动关清单已清空。"
            "本地记账保留，下个活动上线时会归档。",
            0x5865F2,
        )
    return idle


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with WATCH_LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        os.chmod(WATCH_LOCK_PATH, 0o600)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            run_once()
            return 0
        except Exception as exc:
            config = load_config()
            state = load_json(WATCH_PATH, {})
            if not isinstance(state, dict):
                state = {}
            error = f"{type(exc).__name__}: {exc}"[:500]
            if error != str(state.get("probe_error") or ""):
                _notify(config, "❌ MAA 活动监控失败", error, 0xED4245)
            state["probe_error"] = error
            state["updated_at"] = now_beijing().isoformat()
            atomic_write_json(WATCH_PATH, state)
            print(f"MAA 活动监控失败：{error}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
