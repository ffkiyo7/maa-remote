#!/usr/bin/env python3
"""Copilot domain layer for prts.plus homework ops (抄作业).

Almost-pure functions: stage index (Tile-Pos), prts.plus client, matching and
scoring against a Skland roster, gap text, and candidate card building.
Imported by maa_action.py and maa_copilot_worker.py; never run directly.

Design: PLAN_PHASE2.md (phase 2 of the MAA-Remote project).
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from maa_common import (
    STATE_DIR,
    atomic_write_json,
    http_get_json as _common_http_get_json,
    load_json,
    now_beijing,
    prune_button_registry,
)

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

COPILOT_DIR = STATE_DIR / "copilot"
STAGE_INDEX_PATH = COPILOT_DIR / "stage_index.json"
SKLAND_PATH = COPILOT_DIR / "skland.json"
ROSTER_PATH = COPILOT_DIR / "roster.json"
CACHE_DIR = COPILOT_DIR / "cache"
SEARCHES_DIR = COPILOT_DIR / "searches"
SEARCH_LOCK_PATH = COPILOT_DIR / "copilot_search.lock"
PREPARED_DIR = COPILOT_DIR / "prepared"

PRTS_BASE = "https://prts.maa.plus"
CACHE_TTL = timedelta(hours=12)
SEARCH_TTL = timedelta(hours=25)
SEARCH_TOKEN_RE = re.compile(r"^[0-9a-f]{12}$")
DEFAULT_CORE_VERSION = "v6.16.0"
DEFAULT_RESOURCE_DIR = Path.home() / "Library/Application Support/com.loong.maa/resource"

_battle_data_cache: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Stage index (local Tile-Pos is the authoritative code -> stageId mapping)
# ---------------------------------------------------------------------------

def resource_signature(config: dict[str, Any]) -> str:
    """Signature of the local Maa resource tree (version.json + dir mtime)."""
    res_dir = Path(str(config.get("maa_resource_dir", DEFAULT_RESOURCE_DIR)))
    version = load_json(res_dir / "version.json", {})
    last_updated = (
        str(version.get("last_updated", "")) if isinstance(version, dict) else ""
    )
    tile_dir = res_dir / "Arknights-Tile-Pos"
    mtime = ""
    try:
        mtime = str(int(tile_dir.stat().st_mtime))
    except OSError:
        pass
    return f"{last_updated}|{mtime}"


def build_stage_index(config: dict[str, Any]) -> dict[str, Any]:
    """Scan Arknights-Tile-Pos and persist code->stageId / stageId->code maps.

    #f# variant files (686 of them) are raid-unrelated duplicates; skip them.
    Code collisions across events resolve to the newest (last-seen) stage.
    """
    res_dir = Path(str(config.get("maa_resource_dir", DEFAULT_RESOURCE_DIR)))
    tile_dir = res_dir / "Arknights-Tile-Pos"
    codes: dict[str, str] = {}
    stages: dict[str, str] = {}
    for path in tile_dir.glob("*.json"):
        if "#f#" in path.name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        stage_id = str(data.get("stageId") or "")
        code = str(data.get("code") or "")
        if not stage_id or not code:
            continue
        codes[code] = stage_id  # last wins -> newest event on collision
        stages.setdefault(stage_id, code)
    index = {
        "built_at": now_beijing().isoformat(),
        "resource_signature": resource_signature(config),
        "codes": codes,
        "stages": stages,
    }
    atomic_write_json(STAGE_INDEX_PATH, index)
    return index


def load_stage_index(config: dict[str, Any]) -> dict[str, Any]:
    """Cached stage index; rebuild only when the resource tree changed."""
    index = load_json(STAGE_INDEX_PATH, {})
    if (
        isinstance(index, dict)
        and index.get("resource_signature") == resource_signature(config)
        and isinstance(index.get("codes"), dict)
    ):
        return index
    return build_stage_index(config)


def resolve_copilot_stage(
    name: str, index: dict[str, Any]
) -> tuple[str, str] | None:
    """Return (stage_id, code) for a display code or internal stageId."""
    name = str(name or "").strip()[:40]
    if not name:
        return None
    codes = index.get("codes", {})
    stages = index.get("stages", {})
    if name in codes:
        return codes[name], name
    if name in stages:
        return name, stages[name]
    return None


# ---------------------------------------------------------------------------
# battle_data.json (op name -> charId, prefer char_*)
# ---------------------------------------------------------------------------

def battle_data(config: dict[str, Any]) -> dict[str, Any]:
    """charId -> {name, ...} map from the local battle_data.json (cached)."""
    global _battle_data_cache
    if _battle_data_cache is not None:
        return _battle_data_cache
    res_dir = Path(str(config.get("maa_resource_dir", DEFAULT_RESOURCE_DIR)))
    raw = load_json(res_dir / "battle_data.json", {})
    chars = raw.get("chars", {}) if isinstance(raw, dict) else {}
    _battle_data_cache = chars if isinstance(chars, dict) else {}
    return _battle_data_cache


def name_to_char_ids(name: str, config: dict[str, Any]) -> list[str]:
    """Resolve an op name to candidate charIds; summons resolve to token_*."""
    data = battle_data(config)
    found: list[str] = []
    for char_id, info in data.items():
        if not isinstance(info, dict):
            continue
        if str(info.get("name") or "") == name:
            found.append(char_id)
    # prefer char_* (operators) over token_*/trap_* (summons)
    found.sort(key=lambda cid: (not cid.startswith("char_"), cid))
    return found


# ---------------------------------------------------------------------------
# prts.plus client
# ---------------------------------------------------------------------------

def http_get_json(url: str, timeout: int = 30) -> Any:
    """Alias so callers/tests can keep patching maa_copilot.http_get_json."""
    return _common_http_get_json(url, timeout=timeout)


def prts_query(params: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
    """One /copilot/query call; returns the inner data dict."""
    query = urllib.parse.urlencode({k: str(v) for k, v in params.items()})
    payload = http_get_json(f"{PRTS_BASE}/copilot/query?{query}", timeout)
    inner = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(inner, dict) or not isinstance(inner.get("data"), list):
        raise RuntimeError("prts 返回格式异常")
    return inner


def parse_content(item: dict[str, Any]) -> dict[str, Any]:
    content = item.get("content", "")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            content = {}
    return content if isinstance(content, dict) else {}


def upload_ts(item: dict[str, Any]) -> int:
    raw = item.get("upload_ts", item.get("upload_time", ""))
    if isinstance(raw, (int, float)) and raw > 0:
        return int(raw)
    if not raw:
        return 0
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except ValueError:
        return 0


def _parse_version(raw: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", str(raw or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def core_too_old(minimum_required: str, config: dict[str, Any]) -> bool:
    """True when the op needs a newer MaaCore than the local one."""
    if not minimum_required:
        return False
    local = _parse_version(str(config.get("maa_core_version", DEFAULT_CORE_VERSION)))
    required = _parse_version(minimum_required)
    return local is not None and required is not None and required > local


# ---------------------------------------------------------------------------
# Matching & scoring
# ---------------------------------------------------------------------------

def effective_skill_level(char: dict[str, Any], skill_index: int) -> int | None:
    """Effective skill level of a roster char for the op's 1-based skill slot.

    专精1/2/3 -> 8/9/10; otherwise main_skill_lvl (1..7). None when the skill
    slot is not unlocked at all (hard gap).
    """
    slots = char.get("skills") or []
    if not isinstance(slots, list) or not 1 <= skill_index <= len(slots):
        return None
    spec = slots[skill_index - 1]
    try:
        spec = int(spec)
    except (TypeError, ValueError):
        spec = 0
    if spec > 0:
        return 7 + spec
    return int(char.get("main_skill_lvl", 0) or 0)


def skill_label(level: int) -> str:
    if level >= 8:
        return f"专精{level - 7}"
    return f"技能{level}"


def lookup_roster_char(
    roster: dict[str, Any], name: str, config: dict[str, Any]
) -> tuple[dict[str, Any] | None, bool, bool]:
    """Resolve an op name against the roster.

    Returns (char, summon, unknown): char is the roster entry or None;
    summon is True when the name only resolves to token_*/trap_* (ignore,
    not a gap); unknown is True when the name cannot be resolved at all
    (local battle_data lags new operators -> soft gap, not a hard one).
    """
    chars = roster.get("chars", {}) if isinstance(roster, dict) else {}
    if not isinstance(chars, dict) or not chars:
        return None, False, False
    resolved = name_to_char_ids(name, config)
    if not resolved:
        return None, False, True  # unknown name -> cannot verify
    for char_id in resolved:
        if not char_id.startswith("char_"):
            continue
        entry = chars.get(char_id)
        if isinstance(entry, dict):
            return entry, False, False
        return None, False, False  # known operator absent from roster -> not owned
    # only summons matched -> treat as satisfied (e.g. Mon3tr)
    return None, True, False


def check_op_requirements(
    op: dict[str, Any],
    roster: dict[str, Any] | None,
    config: dict[str, Any],
) -> tuple[bool, float, list[str], list[str]]:
    """One operator slot: (satisfied, score, hard_gaps, soft_gaps).

    hard gaps: missing op / elite / level / skill slot; module is a soft
    constraint (in-game UI index cannot map to equip ids) worth half.
    """
    name = str(op.get("name") or "").strip()
    if not name:
        return True, 1.0, [], []
    req = op.get("requirements") or {}
    if not isinstance(req, dict):
        req = {}
    try:
        skill_index = int(op.get("skill") or 0)
    except (TypeError, ValueError):
        skill_index = 0
    # prts uses -1 as "no requirement" sentinel for several fields
    if skill_index < 1:
        skill_index = 1

    if roster is None:
        return True, 1.0, [], []  # degraded mode: cannot verify

    char, summon, unknown = lookup_roster_char(roster, name, config)
    if summon:
        return True, 1.0, [], []
    if char is None:
        if unknown:
            return False, 0.5, [], [f"{name}（本地资源未收录，无法校验）"]
        return False, 0.0, [f"{name}（未拥有）"], []

    hard: list[str] = []
    soft: list[str] = []
    req_elite = max(0, int(req.get("elite") or 0))
    char_elite = int(char.get("elite", 0) or 0)
    if req_elite and char_elite < req_elite:
        hard.append(f"{name} 精英化{req_elite}（你精{char_elite}）")
    req_level = max(0, int(req.get("level") or 0))
    char_level = int(char.get("level", 0) or 0)
    if req_level and char_level < req_level:
        hard.append(f"{name} 等级{req_level}（你{char_level}级）")
    req_skill = max(0, int(req.get("skill_level") or 0))
    if req_skill:
        effective = effective_skill_level(char, skill_index)
        if effective is None:
            hard.append(f"{name} 技能{skill_index}未解锁")
        elif effective < req_skill:
            hard.append(f"{name} {skill_label(req_skill)}（你{skill_label(effective)}）")
    req_module = max(0, int(req.get("module") or 0))
    if req_module:
        module_levels = char.get("module_levels") or []
        if not isinstance(module_levels, list):
            module_levels = []
        if len(module_levels) < req_module or int(module_levels[req_module - 1] or 0) < 1:
            soft.append(f"{name} 模组{req_module}（未解锁，无法精确校验）")

    if hard:
        return False, 0.0, hard, soft
    return True, 0.5 if soft else 1.0, [], soft


def evaluate_candidate(
    item: dict[str, Any],
    roster: dict[str, Any] | None,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Score one raw query item into a candidate dict (or None to exclude)."""
    content = parse_content(item)
    opers = content.get("opers") or []
    groups = content.get("groups") or []
    if not isinstance(opers, list):
        opers = []
    if not isinstance(groups, list):
        groups = []
    slots = len(opers) + len(groups)
    if slots == 0:
        return None
    if item.get("available") is False:
        return None
    if core_too_old(str(content.get("minimum_required") or ""), config):
        return None

    total_score = 0.0
    hard_gaps: list[str] = []
    soft_gaps: list[str] = []
    for op in opers:
        if not isinstance(op, dict):
            continue
        ok, score, hard, soft = check_op_requirements(op, roster, config)
        total_score += score if ok else 0.0
        hard_gaps.extend(hard)
        soft_gaps.extend(soft)

    for group in groups:
        if not isinstance(group, dict):
            continue
        members = group.get("opers") or []
        if not isinstance(members, list):
            members = []
        best_score = 0.0
        best_hard: list[str] = []
        best_soft: list[str] = []
        for member in members:
            if not isinstance(member, dict):
                continue
            ok, score, hard, soft = check_op_requirements(member, roster, config)
            if ok:
                if score > best_score:
                    best_score = score
                    best_hard, best_soft = [], soft
            elif not best_hard or len(hard) < len(best_hard):
                best_hard, best_soft = hard, soft
        total_score += best_score
        hard_gaps.extend(best_hard)
        soft_gaps.extend(best_soft)

    ratio = None
    if roster is not None:
        ratio = total_score / slots

    doc = content.get("doc") or {}
    if not isinstance(doc, dict):
        doc = {}
    return {
        "id": int(item.get("id") or 0),
        "stage_name": str(content.get("stage_name") or ""),
        "title": str(doc.get("title") or f"作业 {item.get('id')}")[:120],
        "uploader": str(item.get("uploader") or "")[:40],
        "ratio": ratio,
        "slots": slots,
        "gaps": hard_gaps[:8],
        "soft_gaps": soft_gaps[:4],
        "quality": quality_score(item),
        "upload_ts": upload_ts(item),
        "views": int(item.get("views") or 0),
        "hot_score": float(item.get("hot_score") or 0.0),
        "rating_ratio": float(item.get("rating_ratio") or 0.0),
        "not_enough_rating": bool(item.get("not_enough_rating")),
        "minimum_required": str(content.get("minimum_required") or ""),
    }


def quality_score(item: dict[str, Any]) -> float:
    """0.5*rating_ratio + 0.3*log-normalized views + 0.2*normalized hot."""
    rating = float(item.get("rating_ratio") or 0.0)
    if item.get("not_enough_rating"):
        rating = 0.0
    views = int(item.get("views") or 0)
    hot = float(item.get("hot_score") or 0.0)
    log_views = math.log10(views + 1) / 6.0 if views > 0 else 0.0
    return round(
        0.5 * max(min(rating, 1.0), 0.0)
        + 0.3 * max(min(log_views, 1.0), 0.0)
        + 0.2 * max(min(hot / 100.0, 1.0), 0.0),
        4,
    )


# ---------------------------------------------------------------------------
# Search orchestration
# ---------------------------------------------------------------------------

def _absorb(seen: dict[int, dict[str, Any]], inner: dict[str, Any],
            stage_id: str, code: str) -> None:
    """Client-side filter: only trust content.stage_name, never the server."""
    for item in inner.get("data", []):
        if not isinstance(item, dict):
            continue
        stage_name = str(parse_content(item).get("stage_name") or "")
        if stage_name not in (stage_id, code):
            continue
        try:
            item_id = int(item.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if item_id > 0:
            seen[item_id] = item


def fetch_stage_copilots(
    stage_id: str, code: str, config: dict[str, Any], timeout: int = 60
) -> list[dict[str, Any]]:
    """All raw query items for one stage (deduped, client-filtered)."""
    probe = prts_query({"limit": 1}, timeout=timeout)
    full_total = int(probe.get("total", 0) or 0)

    seen: dict[int, dict[str, Any]] = {}

    # 1) level_keyword filter — only trusted when it actually filtered
    level_inner = prts_query({"level_keyword": stage_id, "limit": 50}, timeout=timeout)
    level_total = int(level_inner.get("total", 0) or 0)
    if level_total < full_total:
        page = 1
        while page <= 4:
            inner = prts_query(
                {"level_keyword": stage_id, "limit": 50, "page": page}, timeout=timeout
            )
            _absorb(seen, inner, stage_id, code)
            if not inner.get("has_next"):
                break
            page += 1

    # 2) document= full-text search (cheap supplement, hard-capped at 50)
    for document in (code, stage_id):
        try:
            inner = prts_query({"document": document, "limit": 50}, timeout=timeout)
            _absorb(seen, inner, stage_id, code)
        except RuntimeError:
            continue

    # 3) degraded server index -> scan the newest pages (covers current event)
    if level_total >= full_total:
        page = 1
        empty_pages = 0
        while page <= 8:
            inner = prts_query(
                {"order_by": "id", "desc": "true", "limit": 100, "page": page},
                timeout=timeout,
            )
            before = len(seen)
            _absorb(seen, inner, stage_id, code)
            if len(seen) == before:
                empty_pages += 1
            else:
                empty_pages = 0
            if empty_pages >= 4 or not inner.get("has_next"):
                break
            page += 1

    return list(seen.values())


def _slim_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip actions (and other bulk) so cached search results stay small."""
    slim: list[dict[str, Any]] = []
    for item in items:
        copy = dict(item)
        content = parse_content(item)
        slim_content = {
            key: value
            for key, value in content.items()
            if key != "actions"
        }
        copy["content"] = slim_content
        slim.append(copy)
    return slim


def search_copilots(
    stage_id: str,
    code: str,
    config: dict[str, Any],
    roster: dict[str, Any] | None = None,
    limit: int = 3,
    *,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    """Search, evaluate and sort candidates for a stage. Top-N by ratio/quality.

    Cache (actions stripped, TTL 12h) is keyed by stage_id; cached items are
    re-evaluated against the current roster.
    """
    cache_path = CACHE_DIR / f"{stage_id}.json"
    cached = load_json(cache_path, None) if use_cache else None
    if isinstance(cached, dict) and isinstance(cached.get("items"), list):
        fetched_at = cached.get("fetched_at", "")
        try:
            age = now_beijing() - datetime.fromisoformat(str(fetched_at))
        except ValueError:
            age = CACHE_TTL + timedelta(seconds=1)
        if age <= CACHE_TTL:
            items = cached["items"]
        else:
            items = fetch_stage_copilots(stage_id, code, config)
            atomic_write_json(
                cache_path,
                {"fetched_at": now_beijing().isoformat(), "items": _slim_items(items)},
            )
    else:
        items = fetch_stage_copilots(stage_id, code, config)
        atomic_write_json(
            cache_path,
            {"fetched_at": now_beijing().isoformat(), "items": _slim_items(items)},
        )

    candidates = [
        candidate
        for item in items
        if (candidate := evaluate_candidate(item, roster, config)) is not None
    ]
    candidates.sort(
        key=lambda c: (
            round(c["ratio"], 2) if c["ratio"] is not None else 0.0,
            c["quality"],
            c["upload_ts"],
        ),
        reverse=True,
    )
    return candidates[: max(1, limit)]


# ---------------------------------------------------------------------------
# Execution URI decision (rewrite display-code ops locally)
# ---------------------------------------------------------------------------

def execution_uri(
    candidate: dict[str, Any], stage_id: str
) -> tuple[str, bool]:
    """Return (uri, needs_rewrite).

    stage_name already internal -> maa://<id> (maa-cli cache).
    stage_name is a display code -> must /copilot/get, rewrite, file://.
    """
    cand_id = int(candidate.get("id") or 0)
    stage_name = str(candidate.get("stage_name") or "")
    if stage_name == stage_id:
        return f"maa://{cand_id}", False
    return f"file://{PREPARED_DIR}/{cand_id}.json", True


# ---------------------------------------------------------------------------
# Text rendering (candidate cards)
# ---------------------------------------------------------------------------

BUTTON_PREFIX = "hermes:btn:maa-copilot:"


def ensure_private_dirs() -> None:
    for path in (COPILOT_DIR, CACHE_DIR, SEARCHES_DIR, PREPARED_DIR):
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)


def search_button_prefix(token: str) -> str:
    """Registry key prefix owned by one search (trailing ':' keeps it exact)."""
    return f"{BUTTON_PREFIX}{token}:"


def search_button_ids(token: str, count: int) -> list[str]:
    """All registry ids owned by one search (pick/raid/cancel/again)."""
    ids = [f"{BUTTON_PREFIX}{token}:cancel", f"{BUTTON_PREFIX}{token}:again"]
    for index in range(1, count + 1):
        ids.append(f"{BUTTON_PREFIX}{token}:{index}")
        ids.append(f"{BUTTON_PREFIX}{token}:raid-{index}")
    return ids


def prune_search_buttons(token: str, registry: dict[str, Any] | None = None) -> None:
    """Remove ALL registry keys owned by one search (call under state_lock).

    registry given -> mutate it in place, caller persists (avoids lost update);
    registry None  -> read-modify-write BUTTONS_PATH directly.
    """
    prefix = search_button_prefix(token)
    if registry is not None:
        for custom_id in [k for k in registry if k.startswith(prefix)]:
            registry.pop(custom_id)
        return
    prune_button_registry(prefix)


def prune_expired_searches(registry: dict[str, Any] | None = None) -> int:
    """Remove expired search state files and their buttons; returns count.

    Call under state_lock.  Pass the caller's in-memory registry when the
    caller will persist it, otherwise this writes BUTTONS_PATH itself.
    """
    ensure_private_dirs()
    now = now_beijing()
    expired: list[str] = []
    for path in SEARCHES_DIR.glob("*.json"):
        state = load_json(path, None)
        if not isinstance(state, dict):
            expired.append(path.stem)
            continue
        expires_at = state.get("expires_at", "")
        try:
            expired_flag = now > datetime.fromisoformat(str(expires_at))
        except ValueError:
            expired_flag = True
        if expired_flag:
            expired.append(path.stem)
    for token in expired:
        prune_search_buttons(token, registry)
        try:
            (SEARCHES_DIR / f"{token}.json").unlink()
        except FileNotFoundError:
            pass
    return len(expired)


def build_candidate_card(
    token: str,
    candidates: list[dict[str, Any]],
    roster_ok: bool,
    stage_label: str,
    roster_hint: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Embed + components + registry entries for a candidate card."""
    description = search_summary(stage_label, candidates, roster_ok)
    if roster_hint:
        description += f"\n{roster_hint}"
    embed = {
        "title": f"🤖 {stage_label} 抄作业候选",
        "description": description[:4000],
        "color": 0x5865F2,
        "timestamp": now_beijing().isoformat(),
    }
    buttons: list[dict[str, Any]] = []
    registry: dict[str, dict[str, Any]] = {}
    for index, _candidate in enumerate(candidates[:3], start=1):
        custom_id = f"{BUTTON_PREFIX}{token}:{index}"
        buttons.append(
            {"type": 2, "style": 3, "label": f"▶️ {index}", "custom_id": custom_id}
        )
        registry[custom_id] = {
            "kind": "exec",
            "script": "maa_action.py",
            "args": ["copilot", "pick", token, str(index), "normal"],
        }
        raid_id = f"{BUTTON_PREFIX}{token}:raid-{index}"
        buttons.append(
            {"type": 2, "style": 4, "label": f"🔥 {index}", "custom_id": raid_id}
        )
        registry[raid_id] = {
            "kind": "exec",
            "script": "maa_action.py",
            "args": ["copilot", "pick", token, str(index), "raid"],
        }
    cancel_id = f"{BUTTON_PREFIX}{token}:cancel"
    buttons.append({"type": 2, "style": 2, "label": "❌ 取消", "custom_id": cancel_id})
    registry[cancel_id] = {
        "kind": "exec",
        "script": "maa_action.py",
        "args": ["copilot", "cancel", token],
    }
    again_id = f"{BUTTON_PREFIX}{token}:again"
    buttons.append({"type": 2, "style": 2, "label": "🔄 换一批", "custom_id": again_id})
    registry[again_id] = {
        "kind": "exec",
        "script": "maa_action.py",
        "args": ["copilot", "again", token],
    }
    components = [{"type": 1, "components": buttons[:5]}]
    if len(buttons) > 5:
        components.append({"type": 1, "components": buttons[5:10]})
    return embed, components, registry


def ratio_label(ratio: float | None, roster_ok: bool) -> str:
    if ratio is None:
        return "未接入森空岛，练度未校验" if not roster_ok else "练度未知"
    return f"满足 {ratio * 100:.0f}%"


def candidate_lines(candidates: list[dict[str, Any]], roster_ok: bool) -> list[str]:
    lines: list[str] = []
    for index, cand in enumerate(candidates, start=1):
        ratio = cand["ratio"]
        slots = max(int(cand.get("slots") or 0), 1)
        gaps = cand.get("gaps") or []
        soft = cand.get("soft_gaps") or []
        head = f"{index}. **{cand['title']}** — {cand['uploader']}"
        if ratio is not None:
            satisfied = min(int(round(ratio * slots)), slots)
            if ratio >= 1:
                head += f" · ✅ 满足 {slots}/{slots}"
            else:
                head += f" · ⚠️ 满足 {satisfied}/{slots}"
        else:
            head += " · " + ratio_label(None, roster_ok)
        lines.append(head)
        if gaps:
            lines.append(f"   ⚠️ 缺：{'；'.join(gaps[:3])}")
        if soft:
            lines.append(f"   · {'；'.join(soft[:2])}")
        if cand.get("minimum_required"):
            lines.append(f"   · 需 MaaCore {cand['minimum_required']}")
    return lines


# ---------------------------------------------------------------------------
# Roster helpers (shared with maa_skland.py productization, C4)
# ---------------------------------------------------------------------------

def load_roster() -> dict[str, Any] | None:
    roster = load_json(ROSTER_PATH, None)
    if isinstance(roster, dict) and isinstance(roster.get("chars"), dict):
        return roster
    return None


def roster_age_hint(roster: dict[str, Any]) -> str:
    fetched_at = str(roster.get("fetched_at") or "")
    try:
        age = now_beijing() - datetime.fromisoformat(fetched_at)
        days = age.days
    except ValueError:
        return ""
    if days <= 0:
        return ""
    return f"（练度数据 {days} 天前）"


def search_summary(
    stage_label: str,
    candidates: list[dict[str, Any]],
    roster_ok: bool,
) -> str:
    if not candidates:
        return f"🔍 没找到 {stage_label} 的可执行作业。"
    lines = [f"🔍 {stage_label} 的作业候选："] + candidate_lines(candidates, roster_ok)
    if not roster_ok:
        lines.append("（未接入森空岛，练度未校验，按热度排序）")
    return "\n".join(lines)
