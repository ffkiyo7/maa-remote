#!/usr/bin/env python3
"""Maintain the weekly Depot truth snapshot and between-snapshot drop deltas."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import maa_copilot
from maa_common import (
    STATE_DIR,
    atomic_write_json,
    discord_request,
    discord_token,
    emulator_is_online,
    load_config,
    load_json,
    maa_subprocess_env,
    merge_last_run,
    now_beijing,
    report_emulator_busy,
    run_lock,
)


DEPOT_PATH = STATE_DIR / "depot.json"
SCHEMA_VERSION = 1
RECONCILE_INTERVAL = timedelta(days=7)
# MaaCore's StageDrops callback sometimes uses this technical name for a
# furniture drop (seen in the 2026-09-01 am log). Depot scans only the in-game
# “养成材料” tab, so furniture is intentionally outside this inventory model.
NON_DEPOT_DROP_ITEMS = frozenset({"furni"})


def _state(path: Path = DEPOT_PATH) -> dict[str, Any]:
    value = load_json(path, {})
    if not isinstance(value, dict):
        raise RuntimeError("depot.json 不是 JSON 对象")
    version = value.get("schema_version")
    if version not in (None, SCHEMA_VERSION):
        raise RuntimeError(f"depot.json schema_version 不受支持：{version}")
    return value


def _item_names(config: dict[str, Any]) -> dict[str, str]:
    """Use the same MaaCore/MaaResource precedence as the existing stage index."""
    for resource_dir in maa_copilot._resource_dirs(config):
        raw = load_json(resource_dir / "item_index.json", {})
        if not isinstance(raw, dict) or not raw:
            continue
        names = {
            str(item_id): str(detail.get("name") or item_id)
            for item_id, detail in raw.items()
            if isinstance(detail, dict)
        }
        if names:
            return names
    raise RuntimeError("找不到 MaaCore item_index.json，无法给 Depot 物品 ID 对表")


def _json_objects_after(output: str, marker: str) -> Iterable[dict[str, Any]]:
    """maa-cli 把回调整条打成 `<marker> <json>`；把每一条解出来。

    `Depot:` 与 `OperBox:` 是同一形状（maa-cli `run/callback/`：两者都走
    `info!("{}: {}", …, json_pretty(message))`），所以两个解析器共用这一份。
    """
    decoder = json.JSONDecoder()
    cursor = 0
    while True:
        start = output.find(marker, cursor)
        if start < 0:
            return
        start += len(marker)
        fragment = output[start:].lstrip()
        try:
            value, consumed = decoder.raw_decode(fragment)
        except json.JSONDecodeError:
            cursor = start
            continue
        cursor = start + len(output[start:]) - len(fragment) + consumed
        if isinstance(value, dict):
            yield value


def _json_objects_after_depot(output: str) -> Iterable[dict[str, Any]]:
    return _json_objects_after(output, "Depot:")


def parse_depot_output(output: str, config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return only MaaCore's final, complete Depot observation.

    Current MaaCore emits intermediate callbacks with ``done=false``. Treating
    one as the result would silently delete everything on later pages, so no
    fallback to the last partial callback is allowed.
    """
    completed: dict[str, Any] | None = None
    for message in _json_objects_after_depot(output):
        # MaaCore v6.17.0-beta.8 emits DepotInfo here (confirmed from the
        # 2026-09-02 run's asst.log). Do not accept other extra-info events.
        if message.get("taskchain") != "Depot" or message.get("what") != "DepotInfo":
            continue
        details = message.get("details")
        if not isinstance(details, dict) or details.get("done") is not True:
            continue
        raw_data = details.get("data")
        if not isinstance(raw_data, str):
            raise RuntimeError("Depot 完成事件缺少 details.data")
        try:
            value = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Depot 完成事件 data 不是合法 JSON：{exc}") from exc
        if not isinstance(value, dict) or not value:
            raise RuntimeError("Depot 完成事件返回空库存")
        completed = value
    if completed is None:
        raise RuntimeError("maa Depot 输出中没有 done=true 的完整库存")

    names = _item_names(config)
    items: dict[str, dict[str, Any]] = {}
    for raw_id, raw_quantity in completed.items():
        item_id = str(raw_id).strip()
        if not item_id or isinstance(raw_quantity, bool) or not isinstance(raw_quantity, int):
            raise RuntimeError(f"Depot 物品数量格式错误：{raw_id!r}={raw_quantity!r}")
        if raw_quantity < 0:
            raise RuntimeError(f"Depot 物品数量不能为负数：{item_id}={raw_quantity}")
        items[item_id] = {"name": names.get(item_id, item_id), "quantity": raw_quantity}
    return items


# ---------------------------------------------------------------------------
# 练度后备：森空岛不可用时顺手跑一趟 OperBox（四期步骤 5）
# ---------------------------------------------------------------------------

# maa-cli 0.7.5 把 `OperBox` 的 extra-info 回调整条打成 `OperBox: <json>`
# （`crates/maa-cli/src/run/callback/`：`"OperBox" => info!("{}: {}", "OperBox",
# json_pretty(message))`，与 `Depot:` 同一形状，二进制里两个字面量挨着），
# 所以解析器与 Depot 那份共用 `_json_objects_after()`。
# `details` 的字段来自 MaaCore `OperBoxRecognitionTask`：
# `{done, all_opers[], own_opers[{id, name, own, elite, level, potential, rarity}]}`。
OPERBOX_TASK = "operbox"
OPERBOX_TIMEOUT_DEFAULT = 480


def parse_operbox_output(output: str) -> dict[str, dict[str, Any]]:
    """maa-cli 的 `OperBox:` 输出 → `roster.json` 兼容的 `chars`。

    只认 `done=true` 的那一条完整回调（和 Depot 一样，过程回调会漏人）。
    `own_opers` 里没有的字段（技能专精 / 模组）**留空**，`maa_copilot` 见到
    `source == "operbox"` 时按「不满足」处理（fail-closed，D12）。
    """
    completed: dict[str, Any] | None = None
    for message in _json_objects_after(output, "OperBox:"):
        if message.get("taskchain") != "OperBox" or message.get("what") != "OperBoxInfo":
            continue
        details = message.get("details")
        if not isinstance(details, dict) or details.get("done") is not True:
            continue
        completed = details
    if completed is None:
        raise RuntimeError("maa OperBox 输出中没有 done=true 的完整干员列表")
    own = completed.get("own_opers")
    if not isinstance(own, list) or not own:
        raise RuntimeError("OperBox 完成事件没有 own_opers")

    chars: dict[str, dict[str, Any]] = {}
    for raw in own:
        if not isinstance(raw, dict):
            continue
        char_id = str(raw.get("id") or "").strip()
        # id 是 MaaCore BattleData 的 `char_xxx`，与森空岛 `charId` 同一套；
        # 认不出 id 的干员**直接丢掉**——留一个假 key 会让作业匹配以为你有这人。
        if not char_id.startswith("char_"):
            continue
        chars[char_id] = {
            "name": str(raw.get("name") or char_id)[:40],
            "level": _safe_int(raw.get("level")),
            "elite": _safe_int(raw.get("elite")),
            "potential": _safe_int(raw.get("potential")),
            "rarity": _safe_int(raw.get("rarity")),
            # 读不到的两项：留空 = 不满足（不是 0 级，也不是「没要求」）。
            "main_skill_lvl": 0,
            "skills": [],
            "module_levels": [],
        }
    if not chars:
        raise RuntimeError("OperBox 识别结果里没有一个可用的干员 id")
    return chars


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def skland_unavailable(config: dict[str, Any], *, at: datetime | None = None) -> str:
    """森空岛能不能用；不能用时返回原因（空串 = 能用）。

    **只读本机状态、绝不发请求**：这条判断跑在 Depot 那趟的锁内，不能在这里等网络。
    判据两条（四期步骤 5）：
    ① `skland.json.last_error` 非空——上一次刷新已经明确失败（401 / 网络 / 限流）；
    ② `roster.json` 缺失、坏掉，或已经超过 `skland_roster_refresh_days`——懒刷新挂在
       抄作业与 watcher 的每条路径上，还这么旧就说明刷新一直没成功。
    """
    moment = at or now_beijing()
    state = load_json(maa_copilot.SKLAND_PATH, None)
    if isinstance(state, dict) and str(state.get("last_error") or "").strip():
        return f"森空岛上次刷新失败：{str(state['last_error'])[:200]}"
    roster = load_json(maa_copilot.ROSTER_PATH, None)
    if not isinstance(roster, dict) or not isinstance(roster.get("chars"), dict):
        return "本机还没有森空岛练度快照"
    fetched = _parse_time(roster.get("fetched_at"))
    if fetched is None:
        return "森空岛练度快照没有可解析的时间戳"
    limit = max(1, int(config.get("skland_roster_refresh_days", 7)))
    age_days = (moment - fetched).days
    if age_days >= limit:
        return f"森空岛练度快照已 {age_days} 天未刷新（上限 {limit} 天）"
    return ""


def run_operbox(
    config: dict[str, Any],
    env: dict[str, str],
    *,
    at: datetime | None = None,
) -> int:
    """在 Depot 那趟已经开好的会话里跑一次 `OperBox`，写练度后备快照。

    返回识别到的干员数。**只在 `skland_unavailable()` 有话说时才调**；失败由调用方
    吞掉——练度后备坏掉不该把仓库对表这条主线也带红（不变量 #22 的精神）。
    """
    result = subprocess.run(
        [str(config.get("maa_bin", "/opt/homebrew/bin/maa")), "run", OPERBOX_TASK, "-v", "--batch"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=max(60, int(config.get("operbox_timeout", OPERBOX_TIMEOUT_DEFAULT))),
        env=env,
    )
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise RuntimeError(f"maa OperBox 失败（退出码 {result.returncode}）：{detail}")
    if "OperBox Completed" not in output:
        raise RuntimeError("maa OperBox 未报告 Completed")
    chars = parse_operbox_output(output)
    roster = {
        "fetched_at": (at or now_beijing()).isoformat(),
        "uid": "",
        "source": maa_copilot.OPERBOX_SOURCE,
        "chars": chars,
    }
    maa_copilot.ensure_private_dirs()
    atomic_write_json(maa_copilot.OPERBOX_ROSTER_PATH, roster)
    os.chmod(maa_copilot.OPERBOX_ROSTER_PATH, 0o600)
    return len(chars)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def is_due(state: dict[str, Any], at: datetime) -> bool:
    observed_at = _parse_time(state.get("snapshot_at"))
    if observed_at is None:
        return True
    if observed_at.tzinfo is None:
        raise RuntimeError("depot.json snapshot_at 缺少时区")
    return at - observed_at >= RECONCILE_INTERVAL


def _quantity_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for item_id, detail in value.items():
        if isinstance(detail, dict):
            quantity = detail.get("quantity")
            if isinstance(quantity, int) and not isinstance(quantity, bool) and quantity >= 0:
                result[str(item_id)] = quantity
    return result


def reconcile(
    items: dict[str, dict[str, Any]],
    *,
    at: datetime,
    path: Path = DEPOT_PATH,
) -> dict[str, Any]:
    """Atomically install one complete truth snapshot and derive consumption."""
    previous = _state(path)
    previous_items = _quantity_map(previous.get("snapshot_items"))
    pending = _quantity_map(previous.get("drops_since_snapshot"))
    history = previous.get("consumption_history", [])
    if not isinstance(history, list):
        raise RuntimeError("depot.json consumption_history 不是数组")
    history = list(history)

    previous_at = _parse_time(previous.get("snapshot_at"))
    if previous_items and previous_at is not None:
        elapsed_days = max((at - previous_at).total_seconds() / 86400, 1 / 86400)
        interval_items: dict[str, dict[str, Any]] = {}
        current_quantities = _quantity_map(items)
        old_details = previous.get("snapshot_items", {})
        for item_id in sorted(set(previous_items) | set(pending) | set(current_quantities)):
            calculated = (
                previous_items.get(item_id, 0)
                + pending.get(item_id, 0)
                - current_quantities.get(item_id, 0)
            )
            old_name = old_details.get(item_id, {}).get("name") if isinstance(old_details, dict) else None
            name = str(items.get(item_id, {}).get("name") or old_name or item_id)
            interval_items[item_id] = {
                "name": name,
                # Untracked gains can make the formula negative. Keep the signed
                # observation for audit, but never call a gain consumption.
                "calculated": calculated,
                "consumed": max(0, calculated),
                "per_day": round(max(0, calculated) / elapsed_days, 6),
            }
        history.append(
            {
                "from": previous_at.isoformat(),
                "to": at.isoformat(),
                "days": round(elapsed_days, 6),
                "items": interval_items,
            }
        )

    total_days = sum(float(entry.get("days", 0)) for entry in history if isinstance(entry, dict))
    totals: dict[str, dict[str, Any]] = {}
    for entry in history:
        if not isinstance(entry, dict):
            continue
        interval_items = entry.get("items")
        if not isinstance(interval_items, dict):
            continue
        for item_id, detail in interval_items.items():
            if not isinstance(detail, dict):
                continue
            target = totals.setdefault(
                str(item_id), {"name": str(detail.get("name") or item_id), "consumed": 0}
            )
            target["consumed"] += int(detail.get("consumed", 0))
    for detail in totals.values():
        detail["per_day"] = round(detail["consumed"] / total_days, 6) if total_days else 0

    updated = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": at.isoformat(),
        "snapshot_at": at.isoformat(),
        "snapshot_items": items,
        "items": items,
        "drops_since_snapshot": {},
        "consumption_history": history,
        "consumption_rates": totals,
    }
    atomic_write_json(path, updated)
    return updated


# ---------------------------------------------------------------------------
# 消耗率取数口径（PLAN_CLEARING 步骤 6 用）
# ---------------------------------------------------------------------------

# ≥4 周滚动窗口。养成消耗是**突发的**：一次精二吃掉 20 个，平时是 0。
# 用一周窗口看，几乎所有材料在多数周里都是 0，「可用天数」会全变成 ∞。
CONSUMPTION_WINDOW = timedelta(weeks=4)


def has_measured_consumption(state: dict[str, Any]) -> bool:
    """是否已经有实测消耗数据，可以离开手工权重的冷启动降级。

    判据写死为「`consumption_history` 里有 ≥2 次成功对表」——**每两次对表之间
    产生一条区间记录**，所以等价于 `len(consumption_history) >= 1`。
    1 次对表只有库存快照、算不出消耗。不留「看情况」。
    """
    history = state.get("consumption_history")
    return isinstance(history, list) and len([
        entry for entry in history if isinstance(entry, dict)
    ]) >= 1


def consumption_rates_window(
    state: dict[str, Any],
    *,
    at: datetime | None = None,
    window: timedelta = CONSUMPTION_WINDOW,
) -> dict[str, dict[str, Any]]:
    """滚动窗口内的日均消耗：取 `to` 落在窗口内的区间，`sum(consumed)/sum(days)`。

    不是「只看最近一次对表」——那正是把突发消耗看成 0 的原因。
    返回 `{item_id: {"name", "consumed", "days", "per_day"}}`；
    `per_day == 0` 表示**窗口内确实没消耗**（不是「不知道」，调用方按零消耗降级）。
    """
    moment = at or now_beijing()
    cutoff = moment - window
    history = state.get("consumption_history")
    totals: dict[str, dict[str, Any]] = {}
    if not isinstance(history, list):
        return totals
    for entry in history:
        if not isinstance(entry, dict):
            continue
        ended = _parse_time(entry.get("to"))
        if ended is None or ended.tzinfo is None or ended < cutoff:
            continue
        try:
            days = float(entry.get("days", 0))
        except (TypeError, ValueError):
            continue
        items = entry.get("items")
        if not isinstance(items, dict) or days <= 0:
            continue
        for item_id, detail in items.items():
            if not isinstance(detail, dict):
                continue
            target = totals.setdefault(
                str(item_id),
                {"name": str(detail.get("name") or item_id), "consumed": 0, "days": 0.0},
            )
            try:
                target["consumed"] += int(detail.get("consumed", 0))
            except (TypeError, ValueError):
                continue
            target["days"] += days
    for detail in totals.values():
        detail["per_day"] = (
            round(detail["consumed"] / detail["days"], 6) if detail["days"] > 0 else 0.0
        )
    return totals


def stock_by_name(state: dict[str, Any]) -> dict[str, int]:
    """材料名 → 当前库存估算（真值快照 + 期间掉落增量，`items` 已经合并好）。"""
    result: dict[str, int] = {}
    items = state.get("items")
    if not isinstance(items, dict):
        return result
    for detail in items.values():
        if not isinstance(detail, dict):
            continue
        name = str(detail.get("name") or "").strip()
        quantity = detail.get("quantity")
        if not name or not isinstance(quantity, int) or isinstance(quantity, bool):
            continue
        result[name] = result.get(name, 0) + max(0, quantity)
    return result


def record_drops(
    pairs: Iterable[tuple[str, int]],
    config: dict[str, Any],
    *,
    at: datetime | None = None,
    path: Path = DEPOT_PATH,
) -> dict[str, Any]:
    """Apply maa_daily._drop_pairs() output to the local inventory estimate."""
    additions: dict[str, int] = {}
    for raw_name, raw_quantity in pairs:
        name = str(raw_name).strip()
        quantity = int(raw_quantity)
        if not name or quantity <= 0:
            raise RuntimeError(f"掉落增量格式错误：{raw_name!r}={raw_quantity!r}")
        additions[name] = additions.get(name, 0) + quantity
    if not additions:
        return _state(path)

    state = _state(path)
    names = _item_names(config)
    name_to_id = {name: item_id for item_id, name in names.items()}
    items = dict(state.get("items") or {})
    pending = dict(state.get("drops_since_snapshot") or {})
    for name, quantity in additions.items():
        item_id = name_to_id.get(name)
        if item_id is None and name in NON_DEPOT_DROP_ITEMS:
            continue
        if item_id is None:
            raise RuntimeError(f"item_index.json 找不到战报掉落：{name}")
        current = dict(items.get(item_id) or {"name": name, "quantity": 0})
        current["name"] = name
        current["quantity"] = int(current.get("quantity", 0)) + quantity
        items[item_id] = current
        delta = dict(pending.get(item_id) or {"name": name, "quantity": 0})
        delta["name"] = name
        delta["quantity"] = int(delta.get("quantity", 0)) + quantity
        pending[item_id] = delta
    state.update(
        {
            "schema_version": SCHEMA_VERSION,
            "updated_at": (at or now_beijing()).isoformat(),
            "items": items,
            "drops_since_snapshot": pending,
        }
    )
    state.setdefault("consumption_history", [])
    state.setdefault("consumption_rates", {})
    atomic_write_json(path, state)
    return state


def run_depot(config: dict[str, Any], *, at: datetime | None = None) -> dict[str, Any]:
    """Run one real Depot scan; install it only after the whole flow succeeds.

    模拟器已经在用时**什么都不做**（D18）：写一条灰卡与 `depot_status`，
    `snapshot_at` 原样不动，明天 04:30 那趟自然重试。返回的 dict 带
    `status = "skipped_busy"`，由 `run_if_due()` 原样传给 `main()`。
    """
    # Local import avoids a module cycle: maa_daily calls record_drops().
    import maa_daily

    observed_at = at or now_beijing()
    with run_lock(blocking=False) as acquired:
        if not acquired:
            raise RuntimeError("另一个 MAA 任务正在运行，Depot 对表未执行")
        online, probe_detail = emulator_is_online(config)
        if online:
            reason = (
                "检测到模拟器正在使用，本次仓库对表未执行，也没有碰设备；"
                "明天 04:30 会自然重试。"
            )
            report_emulator_busy(
                config,
                title="⏭️ MAA 仓库对表已跳过",
                reason=reason,
                detail=probe_detail,
                at=observed_at,
                state_update={
                    "depot_status": "skipped_busy",
                    "depot_attempted_at": observed_at.isoformat(),
                    "depot_error": None,
                    "emulator_probe": probe_detail,
                },
            )
            return {"status": "skipped_busy", "probe": probe_detail}
        emulator_opened = False
        error: Exception | None = None
        items: dict[str, dict[str, Any]] | None = None
        operbox_note = ""
        try:
            maa_daily.require_executable(config, "maa_bin", "/opt/homebrew/bin/maa", "maa-cli")
            maa_daily.require_executable(config, "adb_path", "/opt/homebrew/bin/adb", "ADB")
            maa_daily.open_emulator(config)
            emulator_opened = True
            maa_daily.wait_for_android(config)
            maa_daily.verify_game_installed(config)
            maa_daily.settle_android_runtime(config)
            env = maa_subprocess_env(config)
            env["MAA_LOG_PREFIX"] = "Never"
            result = subprocess.run(
                [str(config.get("maa_bin", "/opt/homebrew/bin/maa")), "run", "depot", "-v", "--batch"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=max(60, int(config.get("depot_timeout", 480))),
                env=env,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[-1000:]
                raise RuntimeError(f"maa Depot 失败（退出码 {result.returncode}）：{detail}")
            output = "\n".join(part for part in (result.stdout, result.stderr) if part)
            if "Depot Completed" not in output:
                raise RuntimeError("maa Depot 未报告 Completed")
            items = parse_depot_output(output, config)
            # 练度后备（四期步骤 5）：森空岛不可用时，趁这台已经开好的模拟器顺手
            # 跑一趟 OperBox。**放在 Depot 之后**——仓库对表是这趟的正事，
            # 练度快照失败一个字都不许影响它（不变量 #22）。
            operbox_reason = skland_unavailable(config, at=observed_at)
            if operbox_reason:
                try:
                    count = run_operbox(config, env, at=observed_at)
                    operbox_note = f"{operbox_reason}；OperBox 后备已记录 {count} 名干员"
                except Exception as operbox_exc:
                    operbox_note = (
                        f"{operbox_reason}；OperBox 后备也失败："
                        f"{type(operbox_exc).__name__}: {operbox_exc}"
                    )
                print(operbox_note, flush=True)
        except Exception as exc:
            error = exc
        cleanup_errors = maa_daily.close_everything(config) if emulator_opened else []
        if cleanup_errors:
            cleanup_detail = "；".join(cleanup_errors)
            if error is not None:
                error = RuntimeError(
                    f"{type(error).__name__}: {error}；Depot 清理也失败：{cleanup_detail}"
                )
            else:
                error = RuntimeError("Depot 清理失败：" + cleanup_detail)
        if error is not None:
            raise error
        assert items is not None
        updated = reconcile(items, at=observed_at)
        # 说明只跟着这一次的返回值走，不写进 `depot.json`（那是库存真值文件）。
        return {**updated, "operbox_note": operbox_note} if operbox_note else updated


def run_if_due(config: dict[str, Any], *, at: datetime | None = None) -> dict[str, Any]:
    moment = at or now_beijing()
    state = _state()
    if not is_due(state, moment):
        return {"status": "not_due", "snapshot_at": state.get("snapshot_at")}
    updated = run_depot(config, at=moment)
    if updated.get("status") == "skipped_busy":
        # 真值没换，`snapshot_at` 必须保持旧值——否则这次「没跑」会被记成
        # 一次成功对表，7 天间隔白白往后推一周。
        return {"status": "skipped_busy", "snapshot_at": state.get("snapshot_at")}
    return {
        "status": "updated",
        "snapshot_at": updated["snapshot_at"],
        "item_count": len(updated["snapshot_items"]),
        "operbox_note": str(updated.get("operbox_note") or ""),
    }


def main() -> int:
    config = load_config()
    attempted_at = now_beijing()
    try:
        result = run_if_due(config, at=attempted_at)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        state_error = ""
        try:
            merge_last_run(
                {
                    "depot_status": "failed",
                    "depot_attempted_at": attempted_at.isoformat(),
                    "depot_error": detail,
                }
            )
        except Exception as state_exc:
            state_error = f"；Depot 失败状态写入也失败：{type(state_exc).__name__}: {state_exc}"
        channel_id = str(config.get("channel_id", "")).strip()
        if not channel_id.isdigit():
            raise RuntimeError(
                f"{detail}{state_error}；Depot 失败告警未发送：config.json 缺少 channel_id"
            ) from exc
        try:
            discord_request(
                discord_token(),
                "POST",
                f"/channels/{channel_id}/messages",
                {
                    "embeds": [
                        {
                            "title": "❌ MAA Depot 对表失败",
                            "description": detail[:1500],
                            "color": 0xED4245,
                            "timestamp": attempted_at.isoformat(),
                        }
                    ],
                    "allowed_mentions": {"parse": []},
                },
            )
        except Exception as notify_exc:
            raise RuntimeError(
                f"{detail}{state_error}；Depot 失败告警也发送失败："
                f"{type(notify_exc).__name__}: {notify_exc}"
            ) from exc
        raise RuntimeError(detail + state_error) from exc

    record = {
        "depot_status": result["status"],
        "depot_snapshot_at": result.get("snapshot_at"),
        "depot_error": None,
    }
    if result.get("item_count") is not None:
        record["depot_item_count"] = result["item_count"]
    if result.get("operbox_note"):
        # 森空岛不可用时那趟顺手跑的练度后备，成败都留一行在 `last_run.json` 里。
        record["depot_operbox_note"] = result["operbox_note"]
    merge_last_run(record)
    if result["status"] == "updated":
        print(
            json.dumps(
                {
                    "ok": True,
                    "snapshot_at": result["snapshot_at"],
                    "items": result["item_count"],
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"MAA Depot 对表失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
