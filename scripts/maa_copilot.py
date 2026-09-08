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
from typing import Any, Iterable

from maa_common import (
    BEIJING,
    BUTTONS_PATH,
    STATE_DIR,
    atomic_write_json,
    http_get_json as _common_http_get_json,
    is_run_locked,
    load_json,
    now_beijing,
    prune_button_registry,
    state_lock,
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
SANITY_RECOVERY_SECONDS = 6 * 60

_battle_data_cache: dict[str, Any] | None = None


class StaleActivityError(RuntimeError):
    """A prepared automatic card no longer belongs to the live rotation."""


# ---------------------------------------------------------------------------
# Stage index (local Tile-Pos is the authoritative code -> stageId mapping)
# ---------------------------------------------------------------------------

def resource_signature(config: dict[str, Any]) -> str:
    """Signature of the local Maa resource tree (version.json + dir mtime)."""
    parts: list[str] = []
    for res_dir in _resource_dirs(config):
        version = load_json(res_dir / "version.json", {})
        last_updated = (
            str(version.get("last_updated", "")) if isinstance(version, dict) else ""
        )
        tile_dir = res_dir / "Arknights-Tile-Pos"
        try:
            mtime = str(int(tile_dir.stat().st_mtime))
        except OSError:
            mtime = ""
        parts.append(f"{res_dir}:{last_updated}|{mtime}")
    return "||".join(parts)


def build_stage_index(config: dict[str, Any]) -> dict[str, Any]:
    """Scan Arknights-Tile-Pos and persist code->stageId / stageId->code maps.

    #f# variant files (686 of them) are raid-unrelated duplicates; skip them.
    Code collisions across events resolve to the newest (last-seen) stage.
    """
    codes: dict[str, str] = {}
    stages: dict[str, str] = {}
    # Base first, hot-update overlay last: the overlay is authoritative when a
    # new event reuses a code or has not reached the bundled resource yet.
    for res_dir in reversed(_resource_dirs(config)):
        tile_dir = res_dir / "Arknights-Tile-Pos"
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
            codes[code] = stage_id
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


def _resource_dirs(config: dict[str, Any]) -> list[Path]:
    """MaaResource hot-update overlay first, bundled resource second."""
    configured = config.get("maa_resource_dir")
    base = Path(str(configured or DEFAULT_RESOURCE_DIR))
    candidates = [base]
    if configured is None:
        candidates.insert(0, base.parent / "MaaResource" / "resource")
    result: list[Path] = []
    for path in candidates:
        if path not in result:
            result.append(path)
    return result


def _one_cost(values: list[Any]) -> int | None:
    costs: set[int] = set()
    for value in values:
        try:
            cost = int(value)
        except (TypeError, ValueError):
            continue
        if cost > 0:
            costs.add(cost)
    return next(iter(costs)) if len(costs) == 1 else None


def stage_sanity_cost(
    stage_id: str,
    code: str,
    config: dict[str, Any],
) -> int | None:
    """Resolve one battle's AP cost from MaaResource ``stages.json``.

    Reruns can map Tile-Pos ``actXX_08`` to a stages entry named
    ``actXX_08_rep``.  Prefer an exact internal id, then fall back to an exact
    display code.  Ambiguous costs fail closed instead of guessing.
    """
    for resource_dir in _resource_dirs(config):
        stages = load_json(resource_dir / "stages.json", [])
        if not isinstance(stages, list):
            continue
        exact = [
            item.get("apCost")
            for item in stages
            if isinstance(item, dict) and str(item.get("stageId") or "") == stage_id
        ]
        if exact:
            return _one_cost(exact)
        by_code = [
            item.get("apCost")
            for item in stages
            if isinstance(item, dict) and str(item.get("code") or "") == code
        ]
        if by_code:
            return _one_cost(by_code)
    return None


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
    chars: dict[str, Any] = {}
    for res_dir in reversed(_resource_dirs(config)):
        raw = load_json(res_dir / "battle_data.json", {})
        current = raw.get("chars", {}) if isinstance(raw, dict) else {}
        if isinstance(current, dict):
            chars.update(current)
    _battle_data_cache = chars
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
            # Soft gap, half score — same semantics as the module soft
            # constraint.  (returning False here would make evaluate_candidate
            # drop the 0.5 via its `score if ok else 0.0` accumulator.)
            return True, 0.5, [], [f"{name}（本地资源未收录，无法校验）"]
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
        any_satisfied = False
        for member in members:
            if not isinstance(member, dict):
                continue
            ok, score, hard, soft = check_op_requirements(member, roster, config)
            if ok:
                any_satisfied = True
                if score > best_score:
                    best_score = score
                    best_hard, best_soft = [], soft
            elif not any_satisfied and (
                not best_hard or len(hard) < len(best_hard)
            ):
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


def pick_perfect_candidate(
    stage: str,
    stage_id: str,
    config: dict[str, Any],
    roster: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str, list[str]]:
    """这一关唯一的作业筛选口径：`ratio == 1.0` 且**无软硬缺口**（D12 / 不变量 #18）。

    返回 `(候选 | None, 说不到的原因, 最接近那份作业还缺什么)`。
    永不降级到助战、永不忽略练度要求——借了助战之后这关不能开代理，
    以后日常刷到它会执行不了。缓存一律绕过（`use_cache=False`）：新活动首日
    prts 上的作业每小时都在长，读 12 小时前的缓存等于永远搜不到。
    """
    candidates = search_copilots(
        stage_id, stage, config, roster, limit=1000, use_cache=False
    )
    for candidate in candidates:
        if (
            candidate.get("ratio") == 1.0
            and not candidate.get("gaps")
            and not candidate.get("soft_gaps")
        ):
            return candidate, "", []
    if not candidates:
        return None, f"{stage} 在 prts.plus 上暂时没有作业", []
    best = candidates[0]
    gaps = list(best.get("gaps") or []) + list(best.get("soft_gaps") or [])
    return None, f"{stage} 暂无完全满足当前干员练度的作业", gaps[:6]


# ---------------------------------------------------------------------------
# 目标集合（四期 2b）：当期活动前缀在本地索引里的全部关，按打的顺序排好
# ---------------------------------------------------------------------------

# 教学关不打（上游导航链也不走它）。
TUTORIAL_MARKER = "-TR-"
_NUMBERED_RE = re.compile(r"^(?P<prefix>[A-Za-z0-9]{1,8})-(?P<seq>\d{1,2})$")
_EX_RE = re.compile(r"^(?P<prefix>[A-Za-z0-9]{1,8})-EX-(?P<seq>\d{1,2})$")
_S_RE = re.compile(r"^(?P<prefix>[A-Za-z0-9]{1,8})-S-(?P<seq>\d{1,2})$")


def activity_stage_codes(index: dict[str, Any], prefix: str) -> list[str]:
    """本地 Tile-Pos 索引里属于 `prefix` 的全部关，按**数字 → EX → S** 升序。

    索引（`stage_index.json`）是唯一的关卡全集来源：`maa activity` 只给上游
    推荐的那 4 关，而目标集合要的是整个活动（`SR-1..8`、将来的 `XX-EX-n` /
    `XX-S-n`）。教学关（`-TR-`）排除；认不出形状的关排在最后（字典序），
    这样上游哪天多出一种关名也不会被静默丢掉。
    """
    prefix = str(prefix or "")
    if not prefix:
        return []
    codes = index.get("codes")
    if not isinstance(codes, dict):
        return []
    ranked: list[tuple[int, int, str]] = []
    for raw in codes:
        code = str(raw)
        if TUTORIAL_MARKER in code:
            continue
        for group, pattern in ((0, _NUMBERED_RE), (1, _EX_RE), (2, _S_RE)):
            match = pattern.match(code)
            if match and match.group("prefix") == prefix:
                ranked.append((group, int(match.group("seq")), code))
                break
        else:
            if code.split("-", 1)[0] == prefix and code != prefix:
                ranked.append((3, 0, code))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return [code for _group, _seq, code in ranked]


# ---------------------------------------------------------------------------
# 本地作业记账（四期 2a）：`copilot/ledger.json`
# ---------------------------------------------------------------------------

LEDGER_PATH = COPILOT_DIR / "ledger.json"
LEDGER_SCHEMA_VERSION = 1
LEDGER_ATTEMPT_LIMIT = 40

# 记账种子（来源：spike A 报告 `docs/spikes/copilot-list-2026-09-08.md` 与用户自述）。
# SR-5 / SR-6 用户此前自己打过，09-08 23:40 的 spike 又重打了一遍；SR-8 是 09-08
# 15:17 由抄作业通关的。三关的**突袭都没打过**，留空。只在**首次建账**且当期活动
# 前缀就是 `SR` 时写——换了活动、或账本已经存在，一律不写。
SEED_ACTIVITY_PREFIX = "SR"
SEED_CLEARED_NORMAL = ("SR-5", "SR-6", "SR-8")
SEED_AT = "2026-09-08T23:58:00+08:00"


def _safe_key(value: str) -> str:
    """活动 key → 能当文件名的一段（归档账本用）。"""
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", str(value or "")).strip("-")
    return (cleaned or "unknown")[:80]


def new_ledger(activity_key: str) -> dict[str, Any]:
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "activity_key": str(activity_key or "")[:200],
        "stages": {},
        "updated_at": now_beijing().isoformat(),
    }


def load_ledger(path: Path | None = None) -> dict[str, Any] | None:
    """读记账；文件不存在、坏掉或结构不对都返回 None（= 还没建账）。

    坏文件不抛异常是刻意的：记账挂在 watcher 与卡片两条只读路径上，
    一个坏掉的账本绝不能把整轮带走（D-2 的教训）。返回 None 的后果是
    「什么都没通关」→ 最多重抄一遍，不会漏抄。
    """
    try:
        state = load_json(path or LEDGER_PATH, None)
    except (ValueError, OSError):
        return None
    if not isinstance(state, dict) or not isinstance(state.get("stages"), dict):
        return None
    return state


def save_ledger(ledger: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    target = path or LEDGER_PATH
    ledger["schema_version"] = LEDGER_SCHEMA_VERSION
    ledger["updated_at"] = now_beijing().isoformat()
    ensure_private_dirs()
    atomic_write_json(target, ledger)
    os.chmod(target, 0o600)
    return ledger


def seed_ledger(ledger: dict[str, Any], activity_key: str) -> dict[str, Any]:
    """把已知已通的关写进账本，避免上线第一轮重抄一遍。

    只认 `SEED_ACTIVITY_PREFIX`；且**只补空位**——已有记账一律不覆盖，
    所以重复调用是幂等的。种完打上 `seeded` 标记，`ensure_ledger()` 靠它
    认出「这本账还没种过」。
    """
    if _activity_prefix(activity_key) != SEED_ACTIVITY_PREFIX:
        return ledger
    stages = ledger.setdefault("stages", {})
    for stage in SEED_CLEARED_NORMAL:
        entry = stages.setdefault(stage, {"normal": None, "raid": None, "attempts": []})
        if not entry.get("normal"):
            entry["normal"] = SEED_AT
    ledger["seeded"] = SEED_ACTIVITY_PREFIX
    return ledger


def _activity_prefix(activity_key: str) -> str:
    """`SideStory「月行水上」|SR` → `SR`（key 由 `activity_target()` 造）。"""
    return str(activity_key or "").rpartition("|")[2]


def ensure_ledger(
    activity_key: str, *, path: Path | None = None
) -> tuple[dict[str, Any], str]:
    """拿到当期活动的账本，返回 `(ledger, 变更说明)`。

    活动 key 变了（新活动 / 复刻）→ 旧账归档到 `ledger-<key>.json` 再新开一份。
    归档而不是删：用户回头想知道上个活动抄到哪了，只有这份记录说得清。

    **种子按 `seeded` 标记补，不按「是不是首次建账」补**（2026-09-09 线上缺陷）：
    原来只在 `load_ledger()` 返回 `None` 时才种，而账本常常先被**别的活动**的
    tick 建出来——线上 02:23「墟」复刻（`|AT`）那一 tick 先建了账，03:00 SR 上线
    走的是「归档 + 新开」这条路，`ledger is not None`，种子一次都没跑，
    SR-5/6/8 的普通全是空的，第一轮就把它们重抄了一遍。
    `seed_ledger()` 只补空位、幂等，所以按标记补是安全的。
    """
    target = path or LEDGER_PATH
    ledger = load_ledger(target)
    prefix = _activity_prefix(activity_key)
    if ledger is not None and ledger.get("activity_key") == str(activity_key or ""):
        if ledger.get("seeded") != prefix:
            seed_ledger(ledger, activity_key)
            if ledger.get("seeded") == prefix:  # 真的种进去了才落盘
                save_ledger(ledger, target)
        return ledger, ""
    note = ""
    if ledger is not None:
        archive = target.parent / f"ledger-{_safe_key(ledger.get('activity_key'))}.json"
        ensure_private_dirs()
        atomic_write_json(archive, ledger)
        os.chmod(archive, 0o600)
        note = f"旧账已归档到 {archive.name}"
    fresh = seed_ledger(new_ledger(activity_key), activity_key)
    save_ledger(fresh, target)
    return fresh, note


def ledger_stage(ledger: dict[str, Any], stage: str) -> dict[str, Any]:
    stages = ledger.setdefault("stages", {})
    entry = stages.get(stage)
    if not isinstance(entry, dict):
        entry = {"normal": None, "raid": None, "attempts": [], "skipped_reason": ""}
        stages[stage] = entry
    if not isinstance(entry.get("attempts"), list):
        entry["attempts"] = []
    return entry


def record_copilot_attempt(
    ledger: dict[str, Any],
    stage: str,
    mode: str,
    *,
    outcome: str,
    op: int = 0,
    detail: str = "",
    at: datetime | None = None,
    log_name: str = "",
) -> dict[str, Any]:
    """记一次执行。**只有 `won` 才写模式的通关时间戳**。

    突袭失败时普通那格照旧保留（它确实打过了），但这一关**不算通关**——
    `ledger_pending_modes()` 会在下一轮只排剩下的 `raid`（D7）。

    `unknown` 的 `detail` 会补上日志文件名（`log_name`，通常是
    `<date>-copilot-<token>.log`）：这条结论的意思就是「去日志里核对胜利标记」，
    不写清楚是哪一份日志等于让人从 `runs/` 里一个个翻。
    """
    entry = ledger_stage(ledger, stage)
    moment = (at or now_beijing()).isoformat()
    text = str(detail or "")
    if str(outcome) == "unknown" and log_name:
        text = f"{text[:220]} · 日志 runs/{log_name}".strip(" ·")
    entry["attempts"].append(
        {
            "at": moment,
            "mode": mode,
            "op": int(op or 0),
            "outcome": str(outcome)[:20],
            "detail": text[:300],
        }
    )
    del entry["attempts"][:-LEDGER_ATTEMPT_LIMIT]
    if outcome == "won" and mode in ("normal", "raid"):
        entry[mode] = moment
    return ledger


DEFAULT_DAILY_ATTEMPT_CAP = 2


def _attempt_day(raw: Any) -> str:
    """`attempts[].at` → `Asia/Shanghai` 自然日；认不出来返回空串（不计数）。"""
    try:
        moment = datetime.fromisoformat(str(raw or ""))
    except (TypeError, ValueError):
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BEIJING)
    return moment.astimezone(BEIJING).date().isoformat()


def attempts_today(
    ledger: dict[str, Any] | None,
    stage: str,
    mode: str,
    at: datetime | None = None,
) -> int:
    """今天这一 `(关, 模式)` 打过几次但**没通**（`failed` 与 `unknown` 都算）。

    `won` 不计入：通了的模式压根不会再排（`ledger_pending_modes()` 先一步把它
    摘掉），把它算进上限只会让「普通通了、突袭还差」的关平白少一次机会。
    日界用 `Asia/Shanghai` 自然日，与 `maa_clearing.today_key()` 同一把尺子。
    """
    day = _attempt_day((at or now_beijing()).isoformat())
    stages = (ledger or {}).get("stages")
    entry = stages.get(stage) if isinstance(stages, dict) else None
    attempts = entry.get("attempts") if isinstance(entry, dict) else None
    if not isinstance(attempts, list):
        return 0
    return sum(
        1
        for item in attempts
        if isinstance(item, dict)
        and str(item.get("mode") or "normal") == str(mode)
        and str(item.get("outcome") or "") != "won"
        and _attempt_day(item.get("at")) == day
    )


def daily_attempt_cap(config: dict[str, Any] | None) -> int:
    """`auto_copilot_max_attempts_per_day`：同一 `(关, 模式)` 当天最多试几次。

    ≤ 0 视为不限制。读不出来退回默认值，绝不抛——它挂在排轮与卡片两条路径上。
    """
    try:
        return int(
            (config or {}).get(
                "auto_copilot_max_attempts_per_day", DEFAULT_DAILY_ATTEMPT_CAP
            )
        )
    except (TypeError, ValueError):
        return DEFAULT_DAILY_ATTEMPT_CAP


def daily_capped_modes(
    ledger: dict[str, Any] | None,
    stage: str,
    modes: Iterable[str],
    config: dict[str, Any] | None,
    at: datetime | None = None,
) -> list[tuple[str, int]]:
    """今天已经用满次数的 `(模式, 次数)`——排轮据此把整关跳到明天。

    2026-09-09 的教训：缺陷 1 让每一关都记成 `unknown`，30 分钟退避一到就又排
    进来，03:30 和 04:00 两轮把 SR-5/6/7 原样重打了一遍。退避管的是「多久之后
    再试」，管不住「一天试几次」，所以要有这条当日上限。
    """
    cap = daily_attempt_cap(config)
    if cap <= 0:
        return []
    capped: list[tuple[str, int]] = []
    for mode in modes:
        used = attempts_today(ledger, stage, mode, at)
        if used >= cap:
            capped.append((str(mode), used))
    return capped


def ledger_pending_modes(
    ledger: dict[str, Any] | None,
    roster: dict[str, Any] | None,
    stage: str,
    stage_id: str,
    modes: Iterable[str],
) -> list[str]:
    """这一关还差哪些模式（D10 的两条「已通关」判据合并在这里）。

    ① 本地记账里该模式有时间戳 → 那个模式过了；
    ② 森空岛该关所属 zone 全清 → 整关都当过了（zone 粒度的妥协）。
    两条是**或**的关系：记账是精确但可能缺（上线前打的），zone 是粗但覆盖历史。
    """
    if zone_fully_cleared(roster, stage_id):
        return []
    stages = (ledger or {}).get("stages")
    entry = stages.get(stage) if isinstance(stages, dict) else None
    done = entry if isinstance(entry, dict) else {}
    return [mode for mode in modes if not done.get(mode)]


# ---------------------------------------------------------------------------
# Text rendering (cards)
# ---------------------------------------------------------------------------

BUTTON_PREFIX = "hermes:btn:maa-copilot:"
# 抄作业卡（四期 1c）自己的一段前缀。它落在 `hermes:btn:maa-copilot:` 之下，
# 所以**不会**被计划卡 07:50 那趟 `hermes:btn:maa:` 清扫碰到；又因为 `card` 不是
# 12 位 hex（`SEARCH_TOKEN_RE`），`prune_expired_searches()` 也永远轮不到它——
# 那个函数只按 `searches/<token>.json` 里真实存在的 token 清。
CARD_BUTTON_PREFIX = f"{BUTTON_PREFIX}card:"
# 上一张抄作业卡的 message_id：发新卡前要把它的组件 PATCH 空。
# 刻意**不放在 `SEARCHES_DIR`** 下——那个目录里的每个 `*.json` 都会被
# `prune_expired_searches()` 当成一次搜索删掉。
CARD_PATH = COPILOT_DIR / "card.json"

# 还能取消的状态：`scheduled`（排好没派）与 `queued`（派了没抢到锁）。
# `executing` 起就可能已经消耗理智，取消没有意义也不安全。
CANCELABLE_STATUSES = frozenset({"pending", "running", "failed", "scheduled", "queued"})
# 一轮「还没跑完」的状态：卡片按它判「这关是不是已经排进这一轮了」。
LIVE_ROUND_STATUSES = frozenset({"scheduled", "queued", "executing"})


def ensure_private_dirs() -> None:
    for path in (COPILOT_DIR, CACHE_DIR, SEARCHES_DIR, PREPARED_DIR):
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)


def search_button_prefix(token: str) -> str:
    """Registry key prefix owned by one search (trailing ':' keeps it exact)."""
    return f"{BUTTON_PREFIX}{token}:"


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


def supersede_search(token: str) -> tuple[bool, Any]:
    """Atomically invalidate one search that has not entered battle execution."""
    if not SEARCH_TOKEN_RE.fullmatch(token or ""):
        return False, None
    path = SEARCHES_DIR / f"{token}.json"
    with state_lock():
        state = load_json(path, None)
        if not isinstance(state, dict) or state.get("status") not in CANCELABLE_STATUSES:
            return False, None
        registry = load_json(BUTTONS_PATH, {})
        if not isinstance(registry, dict):
            raise RuntimeError("共享按钮登记表格式错误")
        prune_search_buttons(token, registry)
        atomic_write_json(BUTTONS_PATH, registry)
        state["status"] = "superseded"
        state["superseded_at"] = now_beijing().isoformat()
        atomic_write_json(path, state)
        return True, state.get("message_id")


def recover_stale_dispatch(token: str, state: dict[str, Any]) -> dict[str, Any] | None:
    """派出去的 worker 在碰模拟器之前就失联时，把这一轮放回 `scheduled`。

    只处理 `queued`（动作脚本已原子接单、worker 还没抢到锁）这一种；`executing`
    起就可能已经消耗理智，绝不自动重排。
    """
    if state.get("status") != "queued":
        return None
    started = state.get("execution_requested_at")
    try:
        age = now_beijing() - datetime.fromisoformat(str(started))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("作业派发时间格式错误") from exc
    if age <= timedelta(minutes=20) or is_run_locked():
        return None

    path = SEARCHES_DIR / f"{token}.json"
    with state_lock():
        fresh = load_json(path, None)
        if (
            not isinstance(fresh, dict)
            or fresh.get("status") != "queued"
            or fresh.get("execution_requested_at") != started
        ):
            return None
        fresh["status"] = "scheduled"
        fresh["last_prepare_error"] = "执行进程在启动模拟器前失联；本轮已放回排程。"
        fresh["last_prepare_failed_at"] = now_beijing().isoformat()
        fresh.pop("execution_requested_at", None)
        atomic_write_json(path, fresh)
        return fresh


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


# 森空岛 zone 定位用的 stage_id 形状：`act54side_05` → (`act54side`, 5)。
# EX / 剧情关（`act5d0_ex01`、`act53side_sp01`）永远匹配不上，落到 None。
STAGE_SERIAL_RE = re.compile(r"^([A-Za-z0-9]+?)_(\d+)$")


def stage_zone(
    roster: dict[str, Any] | None, stage_id: str
) -> tuple[dict[str, Any], int] | None:
    """定位这一关在森空岛里的 zone：`(zone, 关卡序号)`，定位不到返回 None。

    只做**定位**，不下任何结论。`zone_progress_warning()`（只在能证明未解锁时
    开口的单向启发）与 `zone_fully_cleared()`（D10 的 zone 全清判据）共用这段
    解析，但**两条规则本身不合并**——把「沉默 ≠ 已解锁」和「全清 = 通关」混在
    一起会立刻弄丢前者的性质（三期不变量 #29 的精神）。

    只认第一个 `totalStage > 0` 的 zone：把全局战斗关序号映射到 zone 内的
    cleared 计数在跨 zone 时本来就不成立（剧情关污染边界）。
    """
    if not isinstance(roster, dict):
        return None
    progress = roster.get("activity_progress")
    if not isinstance(progress, dict):
        return None
    match = STAGE_SERIAL_RE.match(str(stage_id or ""))
    if not match:
        return None
    act_id, seq = match.group(1), int(match.group(2))
    zones = progress.get(act_id)
    if not isinstance(zones, list) or not zones:
        return None
    target = next(
        (
            zone
            for zone in zones
            if isinstance(zone, dict) and int(zone.get("totalStage") or 0) > 0
        ),
        None,
    )
    if target is None:
        return None
    return target, seq


def zone_fully_cleared(roster: dict[str, Any] | None, stage_id: str) -> bool:
    """森空岛该关所属 zone 是否 `clearedStage == totalStage`（D10）。

    这是「已通关」的第二条判据（第一条是本地记账）。森空岛只有 zone 粒度，
    所以代价是**用户自己手打过的关可能被再抄一遍**——用户已接受（D10）。
    定位不到 zone（EX / 剧情关、活动不在进度里）一律 False，交给本地记账。
    """
    found = stage_zone(roster, stage_id)
    if found is None:
        return False
    zone, _seq = found
    total = int(zone.get("totalStage") or 0)
    return total > 0 and int(zone.get("clearedStage") or 0) >= total


def zone_is_unopened(roster: dict[str, Any] | None, stage_id: str) -> bool:
    """该关所属活动的 zone 是否**全部** `totalStage == 0`（D9：活动尚未全开）。

    真为真时该关先不排，下一轮再看；**不记失败**（等下去会自己变好）。
    森空岛不可用、活动不在进度里一律返回 False——多排一关的代价远小于
    「森空岛一坏就整个活动不抄」。
    """
    if not isinstance(roster, dict):
        return False
    progress = roster.get("activity_progress")
    if not isinstance(progress, dict):
        return False
    match = STAGE_SERIAL_RE.match(str(stage_id or ""))
    if not match:
        return False
    zones = progress.get(match.group(1))
    if not isinstance(zones, list) or not zones:
        return False
    return all(
        isinstance(zone, dict) and int(zone.get("totalStage") or 0) == 0
        for zone in zones
    )


def zone_progress_warning(
    stage_label: str, stage_id: str, roster: dict[str, Any] | None
) -> str | None:
    """One-line preflight warning for a stage.

    ONE-WAY heuristic: skland's zone progress counts EVERY stage including
    story stages (clearedStage/totalStage), while stage_id's trailing number
    counts BATTLE stages only — different rulers.  The only sound inferences:

      cleared < seq     -> stage N's normal is surely uncleared -> raid locked
      cleared < seq-1   -> stage N-1 surely uncleared -> stage N surely locked
      cleared >= seq    -> CANNOT CONCLUDE anything (necessary, not sufficient)

    So this function only speaks when it can prove the stage is locked;
    silence does NOT mean unlocked — never gate anything on its absence.
    Numbered stages spilling past the first zone (act17side) stay silent:
    a wrong "not unlocked" on a fully cleared zone is worse than silence.
    """
    found = stage_zone(roster, stage_id)
    if found is None:
        return None
    target, seq = found
    act_id = STAGE_SERIAL_RE.match(str(stage_id or "")).group(1)  # type: ignore[union-attr]
    total = int(target.get("totalStage") or 0)
    if seq > total:
        return None
    cleared = int(target.get("clearedStage") or 0)
    zone_label = str(target.get("zoneId") or act_id)
    if cleared >= seq:
        return None
    # Unlock progress changes daily but the roster refreshes lazily (7d) and
    # skland returns the snapshot at last logout — stamp the age so a stale
    # warning reads as stale instead of as a current fact.  (Keep this even
    # though roster_hint also prints the age: the hint only shows when
    # roster_ok, the warning can appear without it.)
    days = roster_age_days(roster)
    suffix = (
        f"（森空岛数据 {days} 天前，如已推进请忽略）"
        if days and days > 0
        else ""
    )
    if cleared >= seq - 1:
        return (
            f"⚠️ {zone_label} 仅通关 {cleared}/{total}：{stage_label} 的突袭大概率未解锁"
            f"（需先通关普通），点了可能空跑。{suffix}"
        )
    return (
        f"⚠️ {zone_label} 仅通关 {cleared}/{total}：{stage_label} 大概率未解锁"
        f"（突袭更不可能），点了可能空跑。{suffix}"
    )


def entry_lines(entries: list[dict[str, Any]]) -> list[str]:
    """把一轮的条目渲染成人话（已排程卡 / 战报共用）。"""
    lines: list[str] = []
    for entry in entries:
        candidate = entry.get("candidate") or {}
        mode_label = "突袭" if entry.get("mode") == "raid" else "普通"
        cost = entry.get("sanity_cost")
        cost_text = f" · {cost} 理智" if isinstance(cost, int) and cost > 0 else ""
        lines.append(
            f"`{entry.get('stage', '')}`（{mode_label}）{cost_text} · "
            f"{str(candidate.get('title') or '')[:40]}"
        )
    return lines


def build_scheduled_card(
    token: str,
    entries: list[dict[str, Any]],
    *,
    title: str,
    scheduled_at: str = "",
    note: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """「已排程」卡：一轮要打的 `(关, 模式)` 清单 + **唯一**一个「取消本轮」按钮。

    候选卡的 ▶️ / 🔥 / 🔄 三种按钮在四期步骤 2 全部删除（D6）：选关即执行，
    执行方式由记账与理智预算决定，没有可供用户挑选的候选。
    """
    total = sum(
        int(entry.get("sanity_cost") or 0)
        for entry in entries
        if isinstance(entry.get("sanity_cost"), int)
    )
    fields: list[dict[str, Any]] = [
        {
            "name": "本轮清单",
            "value": ("\n".join(entry_lines(entries)) or "—")[:1024],
            "inline": False,
        },
        {"name": "预计理智", "value": f"{total}", "inline": True},
    ]
    when = "现在"
    try:
        when = f"{datetime.fromisoformat(scheduled_at):%m-%d %H:%M}"
    except (TypeError, ValueError):
        pass
    fields.append({"name": "预计开始", "value": when, "inline": True})
    if note:
        fields.append({"name": "说明", "value": note[:1024], "inline": False})
    embed = {
        "title": title[:256],
        "description": "跑完会发一条多关战报；不使用助战、不忽略练度要求、不吃药不碎石。",
        "color": 0x5865F2,
        "fields": fields,
        "timestamp": now_beijing().isoformat(),
    }
    cancel_id = f"{BUTTON_PREFIX}{token}:cancel"
    components = [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 2,
                    "label": "❌ 取消本轮",
                    "custom_id": cancel_id,
                }
            ],
        }
    ]
    registry = {
        cancel_id: {
            "kind": "exec",
            "script": "maa_action.py",
            "args": ["copilot", "cancel", token],
        }
    }
    return embed, components, registry


# ---------------------------------------------------------------------------
# Roster helpers (shared with maa_skland.py productization, C4)
# ---------------------------------------------------------------------------

def load_roster() -> dict[str, Any] | None:
    roster = load_json(ROSTER_PATH, None)
    if isinstance(roster, dict) and isinstance(roster.get("chars"), dict):
        return roster
    return None


def estimated_sanity(snapshot: dict[str, Any], at: datetime | None = None) -> int | None:
    """Estimate regenerated sanity from a Skland snapshot without overstating cap."""
    try:
        current = int(snapshot["current"])
        maximum = int(snapshot["max"])
        observed = int(snapshot["server_current_at"])
        last_add = int(snapshot["last_ap_add_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if current < 0 or maximum <= 0 or observed <= 0 or last_add <= 0:
        return None
    if current >= maximum:
        return current
    now_epoch = int((at or now_beijing()).timestamp())
    observed_bucket = max(0, (observed - last_add) // SANITY_RECOVERY_SECONDS)
    current_bucket = max(0, (now_epoch - last_add) // SANITY_RECOVERY_SECONDS)
    regenerated = max(0, current_bucket - observed_bucket)
    return min(maximum, current + regenerated)


def sanity_preflight(
    roster: dict[str, Any] | None,
    cost: int | None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Return a fail-closed, user-facing AP preflight result."""
    try:
        required = int(cost or 0)
    except (TypeError, ValueError):
        required = 0
    if required <= 0:
        return {"known": False, "reason": "关卡理智消耗未知"}
    snapshot = roster.get("sanity") if isinstance(roster, dict) else None
    if not isinstance(snapshot, dict):
        return {"known": False, "cost": required, "reason": "森空岛理智尚未读取"}
    current = estimated_sanity(snapshot, at)
    try:
        maximum = int(snapshot["max"])
        last_add = int(snapshot["last_ap_add_at"])
    except (KeyError, TypeError, ValueError):
        return {"known": False, "cost": required, "reason": "森空岛理智数据格式异常"}
    if current is None:
        return {"known": False, "cost": required, "reason": "森空岛理智数据格式异常"}
    result: dict[str, Any] = {
        "known": True,
        "enough": current >= required,
        "current": current,
        "max": maximum,
        "cost": required,
    }
    if current < required and required <= maximum:
        now = at or now_beijing()
        now_epoch = int(now.timestamp())
        bucket = max(0, (now_epoch - last_add) // SANITY_RECOVERY_SECONDS)
        needed = required - current
        ready_epoch = last_add + (bucket + needed) * SANITY_RECOVERY_SECONDS
        result["ready_at"] = datetime.fromtimestamp(
            ready_epoch, tz=now.tzinfo or timezone.utc
        ).isoformat()
    return result


def sanity_status_text(result: dict[str, Any]) -> str:
    if not result.get("known"):
        reason = str(result.get("reason") or "暂时无法读取")
        return f"🧠 理智：{reason}；确认后会实时核验，失败不会启动模拟器。"
    current = int(result["current"])
    maximum = int(result["max"])
    cost = int(result["cost"])
    if result.get("enough"):
        return f"🧠 理智：{current}/{maximum}，本次需要 {cost}，当前够用。"
    ready = str(result.get("ready_at") or "")
    ready_hint = ""
    try:
        ready_hint = f"，预计 {datetime.fromisoformat(ready):%H:%M} 足够"
    except (TypeError, ValueError):
        pass
    return f"🧠 理智：{current}/{maximum}，本次需要 {cost}，当前不足{ready_hint}。"


def roster_age_days(roster: dict[str, Any]) -> int | None:
    """Days since the skland snapshot, or None when unknown.

    Catches TypeError too: a naive fetched_at minus an aware now raises
    TypeError, not ValueError.
    """
    try:
        age = now_beijing() - datetime.fromisoformat(
            str(roster.get("fetched_at") or "")
        )
    except (ValueError, TypeError):
        return None
    return age.days


def roster_age_hint(roster: dict[str, Any]) -> str:
    days = roster_age_days(roster)
    return f"（森空岛数据 {days} 天前）" if days and days > 0 else ""


