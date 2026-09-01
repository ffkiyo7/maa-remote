#!/usr/bin/env python3
"""Maintain the weekly Depot truth snapshot and between-snapshot drop deltas."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import maa_copilot
from maa_common import (
    STATE_DIR,
    atomic_write_json,
    load_config,
    load_json,
    maa_subprocess_env,
    now_beijing,
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


def _json_objects_after_depot(output: str) -> Iterable[dict[str, Any]]:
    decoder = json.JSONDecoder()
    cursor = 0
    marker = "Depot:"
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
    """Run one real Depot scan; install it only after the whole flow succeeds."""
    # Local import avoids a module cycle: maa_daily calls record_drops().
    import maa_daily

    observed_at = at or now_beijing()
    with run_lock(blocking=False) as acquired:
        if not acquired:
            raise RuntimeError("另一个 MAA 任务正在运行，Depot 对表未执行")
        emulator_opened = False
        error: Exception | None = None
        items: dict[str, dict[str, Any]] | None = None
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
        return reconcile(items, at=observed_at)


def run_if_due(config: dict[str, Any], *, at: datetime | None = None) -> dict[str, Any]:
    moment = at or now_beijing()
    state = _state()
    if not is_due(state, moment):
        return {"status": "not_due", "snapshot_at": state.get("snapshot_at")}
    updated = run_depot(config, at=moment)
    return {
        "status": "updated",
        "snapshot_at": updated["snapshot_at"],
        "item_count": len(updated["snapshot_items"]),
    }


def main() -> int:
    result = run_depot(load_config())
    print(
        json.dumps(
            {"ok": True, "snapshot_at": result["snapshot_at"], "items": len(result["items"])},
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
