#!/usr/bin/env python3
"""活动关卡的「地形」：哪些关在哪个区域、谁解锁谁、区域叫什么（2026-10-19）。

来源是 ArknightsGameData 简中服的 `stage_table.json` 与 `zone_table.json`。
上游 MaaResource 里没有这两样：`stages.json` 不带 EX 的理智、Tile-Pos 不带区域，
而抄作业「从头抄到尾」要知道三件事——

1. 剧情关（`isStoryOnly`）和教学关（`-TR-`）挡在哪一关前面（`unlockCondition`）；
2. EX / S 关在哪个区域、那个区域的入口按钮上写什么（`zoneNameSecond`）；
3. EX 关一次要多少理智（`apCost`，上游 `stages.json` 里没有）。

原表 20 MB，只抽活动关（`act*`）存成一份小缓存 `copilot/gamedata.json`。
**排轮与执行都只读缓存**（`load_layout()`），抓取由 `refresh_layout()` 单独做，
一天最多一次，抓不到就沿用旧缓存——这份数据只在新活动上线时才会变。
"""

from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timedelta
from typing import Any

from maa_common import STATE_DIR, atomic_write_json, load_json, now_beijing

GAMEDATA_PATH = STATE_DIR / "copilot" / "gamedata.json"
DEFAULT_GAMEDATA_BASE = (
    "https://raw.githubusercontent.com/Kengxxiao/ArknightsGameData/master/zh_CN/gamedata/excel"
)
GAMEDATA_TTL = timedelta(hours=24)
GAMEDATA_MAX_BYTES = 64 * 1024 * 1024
GAMEDATA_SCHEMA = 1

# 突袭变体与普通关共用代号，只多一个 `#f#` 后缀；排轮里突袭另有 `#raid` 条目，
# 这里只保留普通关。
RAID_VARIANT_MARK = "#f#"
ACT_STAGE_RE = re.compile(r"^(act[A-Za-z0-9]+?)_")


def act_id_of(stage_id: str) -> str:
    """`act52side_04` → `act52side`；不是活动关返回空串。"""
    match = ACT_STAGE_RE.match(str(stage_id or ""))
    return match.group(1) if match else ""


def _fetch(url: str, timeout: int) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "maa-remote"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(GAMEDATA_MAX_BYTES + 1)
    if len(raw) > GAMEDATA_MAX_BYTES:
        raise RuntimeError(f"{url} 超过大小上限")
    return json.loads(raw.decode("utf-8"))


def extract_layout(stage_table: Any, zone_table: Any) -> dict[str, Any]:
    """原表 → 只含活动关的瘦身版 `{"stages": {...}, "zones": {...}}`。"""
    stages: dict[str, Any] = {}
    raw_stages = stage_table.get("stages") if isinstance(stage_table, dict) else None
    for stage_id, info in (raw_stages or {}).items():
        if not isinstance(info, dict) or RAID_VARIANT_MARK in str(stage_id):
            continue
        if not act_id_of(stage_id):
            continue
        unlock = [
            str(cond.get("stageId") or "")
            for cond in info.get("unlockCondition") or []
            if isinstance(cond, dict) and cond.get("stageId")
        ]
        try:
            ap_cost = int(info.get("apCost") or 0)
        except (TypeError, ValueError):
            ap_cost = 0
        stages[str(stage_id)] = {
            "code": str(info.get("code") or ""),
            "zone_id": str(info.get("zoneId") or ""),
            "story": bool(info.get("isStoryOnly")),
            "ap_cost": ap_cost,
            "unlock": unlock,
        }
    zones: dict[str, Any] = {}
    raw_zones = zone_table.get("zones") if isinstance(zone_table, dict) else None
    for zone_id, info in (raw_zones or {}).items():
        if not isinstance(info, dict) or not act_id_of(zone_id):
            continue
        zones[str(zone_id)] = {
            "name": str(info.get("zoneNameSecond") or info.get("zoneNameFirst") or ""),
            "index": int(info.get("zoneIndex") or 0),
        }
    return {"stages": stages, "zones": zones}


def load_layout() -> dict[str, Any]:
    """读缓存，**不发请求**。没有缓存返回空的 layout。"""
    data = load_json(GAMEDATA_PATH, {})
    if not isinstance(data, dict) or data.get("schema") != GAMEDATA_SCHEMA:
        return {"stages": {}, "zones": {}}
    return {
        "stages": data.get("stages") if isinstance(data.get("stages"), dict) else {},
        "zones": data.get("zones") if isinstance(data.get("zones"), dict) else {},
        "fetched_at": str(data.get("fetched_at") or ""),
    }


def layout_age(layout: dict[str, Any], at: datetime | None = None) -> timedelta | None:
    try:
        fetched = datetime.fromisoformat(str(layout.get("fetched_at") or ""))
    except ValueError:
        return None
    if fetched.tzinfo is None:
        return None
    age = (at or now_beijing()) - fetched
    return age if age >= timedelta(0) else None


def refresh_layout(
    config: dict[str, Any], *, force: bool = False, timeout: int = 60
) -> tuple[dict[str, Any], str]:
    """缓存过期（或 `force`）就重抓一次，返回 `(layout, 抓取失败的说明)`。

    抓不到不抛：旧缓存照用，说明交给调用方决定要不要提示。
    """
    layout = load_layout()
    age = layout_age(layout)
    if not force and layout["stages"] and age is not None and age < GAMEDATA_TTL:
        return layout, ""
    base = str(config.get("gamedata_base_url") or DEFAULT_GAMEDATA_BASE).rstrip("/")
    try:
        stage_table = _fetch(f"{base}/stage_table.json", timeout)
        zone_table = _fetch(f"{base}/zone_table.json", timeout)
        fresh = extract_layout(stage_table, zone_table)
    except Exception as exc:  # noqa: BLE001 - 网络 / 格式问题一律退回旧缓存
        return layout, f"游戏数据抓取失败：{type(exc).__name__}: {exc}"[:300]
    if not fresh["stages"]:
        return layout, "游戏数据里一个活动关都没有，格式可能变了"
    at = now_beijing()
    atomic_write_json(
        GAMEDATA_PATH,
        {"schema": GAMEDATA_SCHEMA, "fetched_at": at.isoformat(), **fresh},
    )
    return {**fresh, "fetched_at": at.isoformat()}, ""


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def stage_info(layout: dict[str, Any], stage_id: str) -> dict[str, Any] | None:
    info = (layout.get("stages") or {}).get(str(stage_id or ""))
    return info if isinstance(info, dict) else None


def zone_name(layout: dict[str, Any], zone_id: str) -> str:
    info = (layout.get("zones") or {}).get(str(zone_id or ""))
    return str(info.get("name") or "") if isinstance(info, dict) else ""


def stage_kind(info: dict[str, Any]) -> str:
    """`story`（纯剧情）/ `tutorial`（教学关，固定阵容）/ `battle`。"""
    if info.get("story"):
        return "story"
    if "-TR-" in str(info.get("code") or ""):
        return "tutorial"
    return "battle"


def ap_cost(layout: dict[str, Any], stage_id: str) -> int | None:
    info = stage_info(layout, stage_id)
    if info is None:
        return None
    cost = int(info.get("ap_cost") or 0)
    return cost if cost > 0 else None


def ancestors(layout: dict[str, Any], stage_id: str) -> list[str]:
    """同一活动里 `stage_id` 的全部前置关（不含自己），**由远到近**排好。

    只沿 `unlockCondition` 走、只走同一活动的关：`YW-ST-1` 的前置 `main_01-10`
    是主线关，不归这里管。环（数据坏了）靠 `seen` 截断。
    """
    stages = layout.get("stages") or {}
    act = act_id_of(stage_id)
    order: list[str] = []
    seen: set[str] = set()

    def visit(current: str) -> None:
        info = stages.get(current)
        if not isinstance(info, dict):
            return
        for parent in info.get("unlock") or []:
            if parent in seen or act_id_of(parent) != act or parent not in stages:
                continue
            seen.add(parent)
            visit(parent)
            order.append(parent)

    visit(str(stage_id or ""))
    return order


def zone_chain(layout: dict[str, Any], zone_id: str) -> list[str]:
    """一个区域里的全部关，按解锁先后排（前置越多越靠后）。"""
    stages = layout.get("stages") or {}
    members = [
        stage_id
        for stage_id, info in stages.items()
        if isinstance(info, dict) and info.get("zone_id") == zone_id
    ]
    return sorted(members, key=lambda stage_id: (len(ancestors(layout, stage_id)), stage_id))


def skland_cleared(layout: dict[str, Any], roster: dict[str, Any] | None, act_id: str) -> set[str]:
    """森空岛按区域给的 `clearedStage` → 具体哪些关已经过了。

    解锁是一条链，所以过了的关一定是区域链的前 `clearedStage` 个。只在森空岛的
    `totalStage` 与游戏数据里这个区域的关数**完全相等**时才信它，对不上就当不知道。
    """
    progress = (roster or {}).get("activity_progress") if isinstance(roster, dict) else None
    zones = progress.get(act_id) if isinstance(progress, dict) else None
    cleared: set[str] = set()
    for zone in zones or []:
        if not isinstance(zone, dict):
            continue
        chain = zone_chain(layout, str(zone.get("zoneId") or ""))
        try:
            total = int(zone.get("totalStage") or 0)
            done = int(zone.get("clearedStage") or 0)
        except (TypeError, ValueError):
            continue
        if not chain or total != len(chain) or not 0 <= done <= total:
            continue
        cleared.update(chain[:done])
    return cleared
