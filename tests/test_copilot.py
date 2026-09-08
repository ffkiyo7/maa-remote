from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import deploy  # noqa: E402
import maa_action  # noqa: E402
import maa_common  # noqa: E402
import maa_copilot  # noqa: E402

BEIJING = ZoneInfo("Asia/Shanghai")


def op_item(item_id: int, stage_name: str, *, opers=None, groups=None,
             minimum_required: str = "", available: bool = True,
             rating_ratio: float = 1.0, views: int = 100,
             hot_score: float = 0.0, not_enough_rating: bool = False,
             title: str = "作业", upload_time: str = "2026-08-01T00:00:00") -> dict:
    content = {
        "stage_name": stage_name,
        "doc": {"title": title, "details": ""},
        "opers": opers or [],
        "groups": groups or [],
        "minimum_required": minimum_required,
    }
    return {
        "id": item_id,
        "content": json.dumps(content, ensure_ascii=False),
        "available": available,
        "rating_ratio": rating_ratio,
        "rating_level": 10,
        "not_enough_rating": not_enough_rating,
        "views": views,
        "hot_score": hot_score,
        "upload_time": upload_time,
        "uploader": "test_uploader",
    }


def prts_http_stub(
    items: list[dict],
    *,
    full_total: int = 39409,
    level_filtered: bool = True,
    scan_pages: list[list[dict]] | None = None,
):
    """Offline stand-in for prts.plus ``/copilot/query``.

    Patch this over ``maa_copilot.http_get_json`` — the lowest network entry —
    rather than over ``fetch_stage_copilots``. Everything above the wire then
    still runs for real: ``prts_query()``'s envelope validation, the
    ``level_keyword`` / ``document=`` query fan-out, ``_absorb()``'s
    client-side ``content.stage_name`` filter and the id dedup.

    The envelope (``{"status_code", "message", "data": {"total", "has_next",
    "page", "data": [...]}}``) is the shape of a real response; only the item
    count is trimmed. ``total`` for the unfiltered probe is a real observed
    value.

    ``level_filtered`` picks which branch `fetch_stage_copilots` takes:

    * ``True``  — ``level_keyword`` reports a smaller total, i.e. the server
      index really filtered, so the paged ``level_keyword`` walk runs.
    * ``False`` — ``level_keyword`` reports the *full* total, i.e. the server
      ignored the filter. That is the degraded case, and the fetcher falls back
      to scanning the newest pages by id.

    ``scan_pages`` feeds that degraded scan one item-list per page (page 1
    first). ``has_next`` stays true while further pages remain, so the loop's
    own stop conditions — the 8-page cap and the 4-consecutive-empty-pages
    bail-out — are what actually end it.
    """
    seen_params: list[dict[str, str]] = []

    def _get(url: str, timeout: int = 30):
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        seen_params.append(params)
        if params.keys() == {"limit"} and params["limit"] == "1":
            body = {"total": full_total, "has_next": False, "page": 1, "data": []}
        elif "level_keyword" in params:
            body = {
                "total": len(items) if level_filtered else full_total,
                "has_next": False,
                "page": int(params.get("page", 1)),
                "data": items,
            }
        elif "order_by" in params and scan_pages is not None:
            page = int(params.get("page", 1))
            page_items = scan_pages[page - 1] if page <= len(scan_pages) else []
            body = {
                "total": full_total,
                "has_next": page < len(scan_pages),
                "page": page,
                "data": page_items,
            }
        else:
            body = {
                "total": len(items),
                "has_next": False,
                "page": int(params.get("page", 1)),
                "data": items,
            }
        return {"status_code": 200, "message": "", "data": body}

    _get.seen_params = seen_params
    return _get


def roster(chars: dict) -> dict:
    return {"fetched_at": "2026-08-01T00:00:00+08:00", "uid": "1", "chars": chars}


def char(*, elite=2, level=90, main_skill_lvl=7, skills=None, module_levels=None):
    return {
        "elite": elite,
        "level": level,
        "main_skill_lvl": main_skill_lvl,
        "skills": skills if skills is not None else [0, 0, 0],
        "module_levels": module_levels if module_levels is not None else [],
    }


CONFIG = {"maa_resource_dir": "/nonexistent"}


class StageIndexTests(unittest.TestCase):
    def test_resolve_stage_accepts_code_and_stage_id(self) -> None:
        index = {
            "codes": {"TO-9": "act53side_09", "1-7": "main_01-07", "CE-6": "wk_melee_6"},
            "stages": {"act53side_09": "TO-9", "main_01-07": "1-7", "wk_melee_6": "CE-6"},
        }
        self.assertEqual(
            maa_copilot.resolve_copilot_stage("TO-9", index), ("act53side_09", "TO-9")
        )
        self.assertEqual(
            maa_copilot.resolve_copilot_stage("act53side_09", index), ("act53side_09", "TO-9")
        )
        self.assertEqual(
            maa_copilot.resolve_copilot_stage("CE-6", index), ("wk_melee_6", "CE-6")
        )
        self.assertIsNone(maa_copilot.resolve_copilot_stage("不存在的关", index))
        self.assertIsNone(maa_copilot.resolve_copilot_stage("", index))
        # input is truncated to 40 chars
        self.assertIsNone(
            maa_copilot.resolve_copilot_stage("TO-9" + "x" * 60, index)
        )

    def test_build_stage_index_skips_raid_variant_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            res = Path(raw)
            tile = res / "Arknights-Tile-Pos"
            tile.mkdir(parents=True)
            (tile / "main_01-07-obt-main-level_main_01-07.json").write_text(
                json.dumps({"code": "1-7", "stageId": "main_01-07"}), encoding="utf-8"
            )
            (tile / "main_01-07#f#-obt-main-level_main_01-07.json").write_text(
                json.dumps({"code": "1-7", "stageId": "main_01-07"}), encoding="utf-8"
            )
            (tile / "act53side_09-activities-act53side-level_act53side_09.json").write_text(
                json.dumps({"code": "TO-9", "stageId": "act53side_09"}), encoding="utf-8"
            )
            (tile / "broken.json").write_text("not json", encoding="utf-8")
            (res / "version.json").write_text(
                json.dumps({"last_updated": "2026-08-01 08:47:36.000"}), encoding="utf-8"
            )
            with patch.object(maa_copilot, "STAGE_INDEX_PATH", Path(raw) / "idx.json"):
                index = maa_copilot.build_stage_index(
                    {"maa_resource_dir": str(res)}
                )
            self.assertEqual(index["codes"]["1-7"], "main_01-07")
            self.assertEqual(index["codes"]["TO-9"], "act53side_09")
            self.assertEqual(index["stages"]["main_01-07"], "1-7")
            self.assertEqual(len(index["codes"]), 2)

    def test_stage_sanity_cost_uses_stage_id_then_rerun_code_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            resource = Path(raw)
            (resource / "stages.json").write_text(
                json.dumps(
                    [
                        {"stageId": "act53side_09", "code": "TO-9", "apCost": 18},
                        {"stageId": "act44side_08_rep", "code": "AT-8", "apCost": 21},
                    ]
                ),
                encoding="utf-8",
            )
            config = {"maa_resource_dir": str(resource)}
            self.assertEqual(
                maa_copilot.stage_sanity_cost("act53side_09", "TO-9", config), 18
            )
            self.assertEqual(
                maa_copilot.stage_sanity_cost("act44side_08", "AT-8", config), 21
            )

    def test_stage_sanity_cost_rejects_ambiguous_code(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            resource = Path(raw)
            (resource / "stages.json").write_text(
                json.dumps(
                    [
                        {"stageId": "old", "code": "XX-8", "apCost": 18},
                        {"stageId": "rerun", "code": "XX-8", "apCost": 21},
                    ]
                ),
                encoding="utf-8",
            )
            self.assertIsNone(
                maa_copilot.stage_sanity_cost(
                    "missing", "XX-8", {"maa_resource_dir": str(resource)}
                )
            )

    def test_hot_update_stage_index_overrides_bundled_resource(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            base = root / "resource"
            overlay = root / "MaaResource" / "resource"
            for resource, stage_id in ((base, "old_08"), (overlay, "new_08")):
                tile = resource / "Arknights-Tile-Pos"
                tile.mkdir(parents=True)
                (tile / f"{stage_id}.json").write_text(
                    json.dumps({"stageId": stage_id, "code": "NEW-8"}),
                    encoding="utf-8",
                )
            with patch.object(maa_copilot, "DEFAULT_RESOURCE_DIR", base), \
                 patch.object(maa_copilot, "STAGE_INDEX_PATH", root / "index.json"):
                index = maa_copilot.build_stage_index({})
            self.assertEqual(index["codes"]["NEW-8"], "new_08")


class SkillMappingTests(unittest.TestCase):
    def test_effective_skill_level_maps_specialize_to_8_9_10(self) -> None:
        c = char(main_skill_lvl=7, skills=[1, 2, 3])
        self.assertEqual(maa_copilot.effective_skill_level(c, 1), 8)
        self.assertEqual(maa_copilot.effective_skill_level(c, 2), 9)
        self.assertEqual(maa_copilot.effective_skill_level(c, 3), 10)

    def test_unskilled_slots_fall_back_to_main_skill_level(self) -> None:
        c = char(main_skill_lvl=5, skills=[0, 0, 0])
        self.assertEqual(maa_copilot.effective_skill_level(c, 1), 5)
        self.assertEqual(maa_copilot.effective_skill_level(c, 2), 5)

    def test_locked_skill_slot_is_a_hard_gap(self) -> None:
        # E0 char has only 1 unlocked skill slot
        c = char(main_skill_lvl=4, skills=[0])
        self.assertIsNone(maa_copilot.effective_skill_level(c, 2))
        self.assertIsNone(maa_copilot.effective_skill_level(c, 3))

    def test_skill_label_text(self) -> None:
        self.assertEqual(maa_copilot.skill_label(7), "技能7")
        self.assertEqual(maa_copilot.skill_label(8), "专精1")
        self.assertEqual(maa_copilot.skill_label(9), "专精2")
        self.assertEqual(maa_copilot.skill_label(10), "专精3")


class MatchingTests(unittest.TestCase):
    def test_group_any_member_satisfies(self) -> None:
        r = roster({
            "char_001_skadi": char(elite=2, level=90),
            "char_002_amiya": char(elite=2, level=90),
        })
        item = op_item(1, "act53side_09", groups=[
            {"name": "挡二", "opers": [
                {"name": "陈", "skill": 2, "requirements": {"elite": 2}},
                {"name": "斯卡蒂", "skill": 2, "requirements": {"elite": 2}},
            ]},
        ])
        with patch.object(maa_copilot, "battle_data", return_value={
            "char_001_skadi": {"name": "斯卡蒂"},
            "char_002_amiya": {"name": "阿米娅"},
        }):
            cand = maa_copilot.evaluate_candidate(item, r, CONFIG)
        self.assertIsNotNone(cand)
        self.assertEqual(cand["ratio"], 1.0)
        self.assertEqual(cand["gaps"], [])

        # A later missing alternative must not put a gap back after an earlier
        # member already satisfied the "choose any one" group.
        reversed_item = op_item(2, "act53side_09", groups=[
            {"name": "挡二", "opers": [
                {"name": "斯卡蒂", "skill": 2, "requirements": {"elite": 2}},
                {"name": "陈", "skill": 2, "requirements": {"elite": 2}},
            ]},
        ])
        with patch.object(maa_copilot, "battle_data", return_value={
            "char_001_skadi": {"name": "斯卡蒂"},
            "char_002_amiya": {"name": "阿米娅"},
        }):
            reversed_candidate = maa_copilot.evaluate_candidate(
                reversed_item, r, CONFIG
            )
        self.assertEqual(reversed_candidate["ratio"], 1.0)
        self.assertEqual(reversed_candidate["gaps"], [])

    def test_group_reports_min_gap_member(self) -> None:
        # 斯卡蒂 has 2 gaps; 陈 (known name, not owned) has 1 -> report 陈
        r = roster({
            "char_001_skadi": char(elite=1, level=50),
        })
        item = op_item(1, "act53side_09", groups=[
            {"name": "挡二", "opers": [
                {"name": "斯卡蒂", "skill": 2, "requirements": {"elite": 2, "level": 70}},
                {"name": "陈", "skill": 2, "requirements": {"elite": 2, "level": 90}},
            ]},
        ])
        with patch.object(maa_copilot, "battle_data", return_value={
            "char_001_skadi": {"name": "斯卡蒂"},
            "char_010_chen": {"name": "陈"},
        }):
            cand = maa_copilot.evaluate_candidate(item, r, CONFIG)
        self.assertIsNotNone(cand)
        self.assertEqual(cand["ratio"], 0.0)
        self.assertEqual(cand["gaps"], ["陈（未拥有）"])

    def test_gap_text_verbatim(self) -> None:
        # skill slot 2 is specialize 1 -> effective 专精1; op wants 专精2
        r = roster({
            "char_479_surtr": char(elite=2, level=90, main_skill_lvl=7, skills=[0, 1, 0]),
        })
        item = op_item(1, "act53side_09", opers=[
            {"name": "史尔特尔", "skill": 2, "requirements": {"skill_level": 9}},
        ])
        with patch.object(maa_copilot, "battle_data", return_value={
            "char_479_surtr": {"name": "史尔特尔"},
        }):
            cand = maa_copilot.evaluate_candidate(item, r, CONFIG)
        self.assertIsNotNone(cand)
        self.assertEqual(cand["ratio"], 0.0)
        self.assertEqual(cand["gaps"], ["史尔特尔 专精2（你专精1）"])

    def test_summon_units_are_ignored_not_gaps(self) -> None:
        r = roster({"char_003_kalts": char(elite=2, level=90)})
        item = op_item(1, "act53side_09", opers=[
            {"name": "凯尔希", "skill": 2, "requirements": {"elite": 2}},
            {"name": "Mon3tr", "skill": 1, "requirements": {"level": 90}},
        ])
        with patch.object(maa_copilot, "battle_data", return_value={
            "char_003_kalts": {"name": "凯尔希"},
            "token_10002_kalts_mon3tr": {"name": "Mon3tr"},
        }):
            cand = maa_copilot.evaluate_candidate(item, r, CONFIG)
        self.assertIsNotNone(cand)
        self.assertEqual(cand["ratio"], 1.0)
        self.assertEqual(cand["gaps"], [])

    def test_missing_operator_is_hard_gap(self) -> None:
        r = roster({"char_002_amiya": char(elite=2, level=90)})
        item = op_item(1, "act53side_09", opers=[
            {"name": "史尔特尔", "skill": 2, "requirements": {"elite": 2}},
        ])
        # battle_data resolves the name (char_350_surtr) but the roster does
        # not own her -> hard gap "未拥有" (not the soft "无法校验" path).
        with patch.object(maa_copilot, "battle_data", return_value={
            "char_350_surtr": {"name": "史尔特尔"},
        }):
            cand = maa_copilot.evaluate_candidate(item, r, CONFIG)
        self.assertEqual(cand["ratio"], 0.0)
        self.assertEqual(cand["gaps"], ["史尔特尔（未拥有）"])

    def test_module_is_soft_constraint_half_score(self) -> None:
        r = roster({
            "char_479_surtr": char(elite=2, level=90, module_levels=[0, 0]),
        })
        item = op_item(1, "act53side_09", opers=[
            {"name": "史尔特尔", "skill": 2, "requirements": {"module": 2}},
        ])
        with patch.object(maa_copilot, "battle_data", return_value={
            "char_479_surtr": {"name": "史尔特尔"},
        }):
            cand = maa_copilot.evaluate_candidate(item, r, CONFIG)
        self.assertEqual(cand["ratio"], 0.5)
        self.assertEqual(cand["soft_gaps"], ["史尔特尔 模组2（未解锁，无法精确校验）"])

    def test_minus_one_sentinel_means_no_requirement(self) -> None:
        """prts uses -1 as 'no requirement' for module/skill/level fields."""
        r = roster({
            "char_479_surtr": char(elite=2, level=90, main_skill_lvl=7, skills=[0, 0, 0], module_levels=[]),
        })
        item = op_item(1, "act53side_09", opers=[
            {"name": "史尔特尔", "skill": 2, "requirements": {
                "elite": -1, "level": -1, "skill_level": -1, "module": -1}},
        ])
        with patch.object(maa_copilot, "battle_data", return_value={
            "char_479_surtr": {"name": "史尔特尔"},
        }):
            cand = maa_copilot.evaluate_candidate(item, r, CONFIG)
        self.assertIsNotNone(cand)
        self.assertEqual(cand["ratio"], 1.0)
        self.assertEqual(cand["gaps"], [])
        self.assertEqual(cand["soft_gaps"], [])

    def test_degraded_mode_without_roster_returns_none_ratio(self) -> None:
        item = op_item(1, "act53side_09", opers=[
            {"name": "史尔特尔", "skill": 2, "requirements": {"elite": 2}},
        ])
        cand = maa_copilot.evaluate_candidate(item, None, CONFIG)
        self.assertIsNotNone(cand)
        self.assertIsNone(cand["ratio"])
        self.assertEqual(cand["gaps"], [])

    def test_hard_excludes_available_and_core_version(self) -> None:
        config = {"maa_resource_dir": "/nonexistent", "maa_core_version": "v6.16.0"}
        unavailable = op_item(1, "act53side_09", opers=[{"name": "x", "skill": 1}], available=False)
        self.assertIsNone(maa_copilot.evaluate_candidate(unavailable, None, config))
        too_new = op_item(
            2, "act53side_09", opers=[{"name": "x", "skill": 1}], minimum_required="v6.20.0"
        )
        self.assertIsNone(maa_copilot.evaluate_candidate(too_new, None, config))
        old_ok = op_item(
            3, "act53side_09", opers=[{"name": "x", "skill": 1}], minimum_required="v6.0.0"
        )
        self.assertIsNotNone(maa_copilot.evaluate_candidate(old_ok, None, config))
        empty = op_item(4, "act53side_09")
        self.assertIsNone(maa_copilot.evaluate_candidate(empty, None, config))


class SearchTests(unittest.TestCase):
    def test_level_keyword_degradation_triggers_latest_scan(self) -> None:
        """act53side_09 not in the server index -> total == full -> scan pages."""
        config = {"maa_resource_dir": "/nonexistent"}

        def fake_query(params, timeout=60):
            if "level_keyword" in params:
                return {"total": 39409, "has_next": False, "page": 1, "data": []}
            if "order_by" in params:
                page = int(params.get("page", 1))
                if page == 1:
                    return {"total": 39409, "has_next": True, "page": 1, "data": [
                        op_item(100, "act53side_09", opers=[{"name": "x", "skill": 1}]),
                        op_item(99, "TO-8", opers=[{"name": "x", "skill": 1}]),
                    ]}
                return {"total": 39409, "has_next": False, "page": 2, "data": []}
            if "document" in params:
                return {"total": 0, "has_next": False, "page": 1, "data": []}
            return {"total": 39409, "has_next": False, "page": 1, "data": []}

        with patch.object(maa_copilot, "prts_query", side_effect=fake_query), \
             patch.object(maa_copilot, "CACHE_DIR", Path(tempfile.mkdtemp())):
            items = maa_copilot.fetch_stage_copilots("act53side_09", "TO-9", config)
        ids = {item["id"] for item in items}
        self.assertIn(100, ids)
        self.assertNotIn(99, ids)  # wrong stage filtered client-side

    def test_level_keyword_trusted_when_it_filters(self) -> None:
        config = {"maa_resource_dir": "/nonexistent"}

        def fake_query(params, timeout=60):
            if "level_keyword" in params:
                return {"total": 297, "has_next": True, "page": 1, "data": [
                    op_item(1, "main_01-07", opers=[{"name": "x", "skill": 1}]),
                ]}
            return {"total": 39409, "has_next": False, "page": 1, "data": []}

        with patch.object(maa_copilot, "prts_query", side_effect=fake_query), \
             patch.object(maa_copilot, "CACHE_DIR", Path(tempfile.mkdtemp())):
            items = maa_copilot.fetch_stage_copilots("main_01-07", "1-7", config)
        self.assertEqual([item["id"] for item in items], [1])

    def test_document_search_is_supplementary_and_filtered(self) -> None:
        config = {"maa_resource_dir": "/nonexistent"}
        calls: list[dict] = []

        def fake_query(params, timeout=60):
            calls.append(params)
            if "document" in params:
                if params["document"] == "TO-9":
                    return {"total": 52, "has_next": False, "page": 1, "data": [
                        op_item(200, "TO-9", opers=[{"name": "x", "skill": 1}]),
                        op_item(201, "TO-8", opers=[{"name": "x", "skill": 1}]),  # wrong stage
                    ]}
                return {"total": 0, "has_next": False, "page": 1, "data": []}
            if "level_keyword" in params:
                return {"total": 39409, "has_next": False, "page": 1, "data": []}
            return {"total": 39409, "has_next": False, "page": 1, "data": []}

        with patch.object(maa_copilot, "prts_query", side_effect=fake_query), \
             patch.object(maa_copilot, "CACHE_DIR", Path(tempfile.mkdtemp())):
            items = maa_copilot.fetch_stage_copilots("act53side_09", "TO-9", config)
        self.assertEqual([item["id"] for item in items], [200])
        self.assertTrue(any(p.get("document") == "TO-9" for p in calls))

    def test_search_copilots_sorts_by_ratio_bucket_then_quality(self) -> None:
        config = {"maa_resource_dir": "/nonexistent"}
        perfect = op_item(1, "act53side_09", opers=[{"name": "阿米娅", "skill": 1, "requirements": {"elite": 2}}],
                          rating_ratio=0.5, views=50)
        hot_but_gappy = op_item(2, "act53side_09", opers=[
            {"name": "阿米娅", "skill": 1, "requirements": {"elite": 2}},
            {"name": "史尔特尔", "skill": 2, "requirements": {"elite": 2}},
        ], rating_ratio=1.0, views=100000, hot_score=50)
        with patch.object(maa_copilot, "prts_query", side_effect=lambda params, timeout=60: {
            "total": 39409, "has_next": False, "page": 1,
            "data": [perfect, hot_but_gappy] if "order_by" not in params else [],
        }), patch.object(maa_copilot, "battle_data", return_value={
            "char_002_amiya": {"name": "阿米娅"},
            # resolvable but NOT owned -> hard gap, 0 score (NEW-2 keeps
            # the half-score path for *unresolvable* names only)
            "char_350_surtr": {"name": "史尔特尔"},
        }), patch.object(maa_copilot, "CACHE_DIR", Path(tempfile.mkdtemp())):
            roster_ok = roster({"char_002_amiya": char(elite=2, level=90)})
            candidates = maa_copilot.search_copilots(
                "act53side_09", "TO-9", config, roster_ok, limit=3
            )
        # perfect (ratio 1.0) outranks gappy (ratio 0.5) despite lower quality
        self.assertEqual([c["id"] for c in candidates], [1, 2])
        self.assertEqual(candidates[0]["ratio"], 1.0)
        self.assertEqual(candidates[1]["ratio"], 0.5)

    def test_search_copilots_respects_cache_ttl(self) -> None:
        config = {"maa_resource_dir": "/nonexistent"}
        with tempfile.TemporaryDirectory() as raw:
            cache_dir = Path(raw) / "cache"
            cache_dir.mkdir()
            cache_path = cache_dir / "act53side_09.json"
            stale = {
                "fetched_at": (datetime.now().astimezone() - timedelta(hours=13)).isoformat(),
                "items": [op_item(7, "act53side_09", opers=[{"name": "x", "skill": 1}])],
            }
            cache_path.write_text(json.dumps(stale), encoding="utf-8")
            fetched: list[int] = []
            real_fetch = maa_copilot.fetch_stage_copilots
            returned: list[list[dict]] = []

            def counting_fetch(stage_id, code, config):
                fetched.append(1)
                # The real fetcher, exercised against a stubbed wire — see
                # prts_http_stub(). Never hits the network, so the suite stays
                # green offline and cannot go red on a prts.plus hiccup.
                items = real_fetch(stage_id, code, config)
                returned.append(items)
                return items

            # id 99 is for this stage; id 98 belongs to another stage and must
            # be dropped by the client-side filter, never trusted from the
            # server. Both come back through the same query.
            wire = prts_http_stub([
                op_item(99, "act53side_09", opers=[{"name": "x", "skill": 1}]),
                op_item(98, "act51side_01", opers=[{"name": "y", "skill": 1}]),
            ])
            with patch.object(maa_copilot, "fetch_stage_copilots", side_effect=counting_fetch), \
                 patch.object(maa_copilot, "http_get_json", side_effect=wire), \
                 patch.object(maa_copilot, "CACHE_DIR", cache_dir):
                maa_copilot.search_copilots("act53side_09", "TO-9", config, None, use_cache=True)
            self.assertEqual(len(fetched), 1)  # stale -> refetch
            # The refetch really parsed the wire: the matching item survived and
            # the other stage's item was filtered out.
            self.assertEqual([item["id"] for item in returned[0]], [99])
            # And it really went through prts_query's fan-out, not a shortcut.
            self.assertIn(
                "act53side_09", [p.get("level_keyword") for p in wire.seen_params]
            )

            fresh = {
                "fetched_at": (datetime.now().astimezone() - timedelta(hours=1)).isoformat(),
                "items": [op_item(8, "act53side_09", opers=[{"name": "x", "skill": 1}])],
            }
            cache_path.write_text(json.dumps(fresh), encoding="utf-8")
            with patch.object(maa_copilot, "fetch_stage_copilots", side_effect=counting_fetch), \
                 patch.object(maa_copilot, "http_get_json", side_effect=wire), \
                 patch.object(maa_copilot, "CACHE_DIR", cache_dir):
                candidates = maa_copilot.search_copilots("act53side_09", "TO-9", config, None, use_cache=True)
            self.assertEqual(len(fetched), 1)  # fresh cache -> no refetch
            self.assertEqual(candidates[0]["id"], 8)


class DegradedScanTests(unittest.TestCase):
    """`fetch_stage_copilots`'s fallback when the server index stops filtering.

    When `level_keyword` comes back with the *full* total the server clearly
    ignored the filter, so the fetcher scans the newest pages by id instead.
    That branch covers the current event — the case this whole feature exists
    for — and was previously untested because the old networked test only ever
    exercised the filtered path.
    """

    CONFIG = {"maa_resource_dir": "/nonexistent"}

    @staticmethod
    def _scan_params(wire) -> list[dict]:
        return [p for p in wire.seen_params if "order_by" in p]

    def _fetch(self, wire) -> list[dict]:
        with patch.object(maa_copilot, "http_get_json", side_effect=wire):
            return maa_copilot.fetch_stage_copilots(
                "act53side_09", "TO-9", self.CONFIG
            )

    def test_degraded_index_falls_back_to_scanning_newest_pages(self) -> None:
        wire = prts_http_stub(
            [],
            level_filtered=False,
            scan_pages=[
                # page 1: one for this stage, one for another stage
                [
                    op_item(11, "act53side_09", opers=[{"name": "x", "skill": 1}]),
                    op_item(12, "act51side_01", opers=[{"name": "y", "skill": 1}]),
                ],
                # page 2: matches by display code, which _absorb also accepts
                [op_item(13, "TO-9", opers=[{"name": "z", "skill": 1}])],
            ],
        )
        items = self._fetch(wire)

        # The degraded branch really ran...
        scans = self._scan_params(wire)
        self.assertTrue(scans, "没有走降级扫页分支")
        self.assertEqual(scans[0]["order_by"], "id")
        self.assertEqual(scans[0]["desc"], "true")
        self.assertEqual(scans[0]["limit"], "100")
        # ...and the paged level_keyword walk did not.
        self.assertEqual(
            [p for p in wire.seen_params if "level_keyword" in p and "page" in p], []
        )

        # Client-side stage_name filter still applies: 12 belongs to another
        # stage and must be dropped even though the server returned it.
        self.assertEqual(sorted(item["id"] for item in items), [11, 13])

    def test_degraded_scan_dedupes_ids_seen_on_more_than_one_page(self) -> None:
        repeated = op_item(21, "act53side_09", opers=[{"name": "x", "skill": 1}])
        wire = prts_http_stub(
            [],
            level_filtered=False,
            scan_pages=[
                [repeated],
                [repeated, op_item(22, "act53side_09", opers=[{"name": "y", "skill": 1}])],
            ],
        )
        items = self._fetch(wire)
        self.assertEqual(sorted(item["id"] for item in items), [21, 22])
        self.assertEqual(len(items), 2)

    def test_degraded_scan_stops_at_the_eight_page_cap(self) -> None:
        # Every page yields a new match, so the empty-page bail-out never fires
        # and has_next stays true — only the hard cap can stop this.
        wire = prts_http_stub(
            [],
            level_filtered=False,
            scan_pages=[
                [op_item(100 + n, "act53side_09", opers=[{"name": "x", "skill": 1}])]
                for n in range(12)
            ],
        )
        items = self._fetch(wire)
        self.assertEqual(len(self._scan_params(wire)), 8)
        self.assertEqual(len(items), 8)

    def test_degraded_scan_gives_up_after_four_consecutive_empty_pages(self) -> None:
        other = "act51side_01"
        wire = prts_http_stub(
            [],
            level_filtered=False,
            scan_pages=[
                [op_item(30, "act53side_09", opers=[{"name": "x", "skill": 1}])],
                [op_item(31, other)],  # empty #1 (nothing for our stage)
                [op_item(32, other)],  # empty #2
                [op_item(33, other)],  # empty #3
                [op_item(34, other)],  # empty #4 -> bail out here
                # never fetched, so this match must not show up:
                [op_item(35, "act53side_09", opers=[{"name": "y", "skill": 1}])],
            ],
        )
        items = self._fetch(wire)
        self.assertEqual(len(self._scan_params(wire)), 5)
        self.assertEqual([item["id"] for item in items], [30])

    def test_degraded_scan_stops_when_the_server_says_no_more_pages(self) -> None:
        wire = prts_http_stub(
            [],
            level_filtered=False,
            scan_pages=[
                [op_item(40, "act53side_09", opers=[{"name": "x", "skill": 1}])],
                [op_item(41, "act53side_09", opers=[{"name": "y", "skill": 1}])],
            ],
        )
        items = self._fetch(wire)
        # has_next is false on page 2, well before the cap.
        self.assertEqual(len(self._scan_params(wire)), 2)
        self.assertEqual(sorted(item["id"] for item in items), [40, 41])


@unittest.skipUnless(
    os.environ.get("MAA_LIVE_TESTS") == "1",
    "线上契约冒烟，默认跳过；用 MAA_LIVE_TESTS=1 显式开启",
)
class LiveContractTests(unittest.TestCase):
    """The one thing the old networked TTL test covered by accident.

    `test_search_copilots_respects_cache_ttl` used to call the real fetcher
    over the real wire. That was never its subject — it verifies the 12h cache
    TTL — but it did incidentally prove prts.plus still speaks the envelope
    `prts_query()` parses. Keeping that as a hidden side effect made the whole
    suite flaky (SSL EOF) and slow; asserting it here on purpose, opt-in, is
    the honest version. Never run by CI or by the default suite.
    """

    def test_prts_query_envelope_still_matches_the_parser(self) -> None:
        inner = maa_copilot.prts_query({"limit": 1}, timeout=30)
        # prts_query already raises "prts 返回格式异常" if these are missing;
        # assert them explicitly so a failure names the field that moved.
        self.assertIsInstance(inner.get("data"), list)
        self.assertIsInstance(int(inner.get("total", 0) or 0), int)
        self.assertGreater(int(inner.get("total", 0) or 0), 0)
        item = inner["data"][0]
        for field in ("id", "content", "available", "rating_ratio", "views"):
            self.assertIn(field, item, f"prts.plus 的作业条目少了 {field}")
        # content is a JSON *string* carrying stage_name — the field the
        # client-side filter in _absorb() trusts instead of the server.
        self.assertIn("stage_name", maa_copilot.parse_content(item))


class ExecutionUriTests(unittest.TestCase):
    def test_stage_id_ops_use_maa_uri(self) -> None:
        cand = {"id": 123, "stage_name": "act53side_09"}
        uri, rewrite = maa_copilot.execution_uri(cand, "act53side_09")
        self.assertEqual(uri, "maa://123")
        self.assertFalse(rewrite)

    def test_display_code_ops_are_rewritten_to_file_uri(self) -> None:
        cand = {"id": 123, "stage_name": "TO-9"}
        uri, rewrite = maa_copilot.execution_uri(cand, "act53side_09")
        self.assertTrue(rewrite)
        self.assertTrue(uri.startswith("file://"))
        self.assertIn("123.json", uri)


class ButtonRegistryTests(unittest.TestCase):
    def test_the_scheduled_card_carries_exactly_one_cancel_button(self) -> None:
        # 四期 D6：候选卡的 ▶️ / 🔥 / 🔄 三种按钮全部删除，选关即执行。
        entries = [
            {"stage": "SR-5", "mode": "normal", "sanity_cost": 12,
             "candidate": {"id": 1, "title": "稳定作业"}},
            {"stage": "SR-5", "mode": "raid", "sanity_cost": 12,
             "candidate": {"id": 1, "title": "稳定作业"}},
        ]
        embed, components, registry = maa_copilot.build_scheduled_card(
            "a" * 12, entries, title="🤖 自动作业已排程",
            scheduled_at="2026-09-09T02:10:00+08:00",
        )
        buttons = components[0]["components"]
        self.assertEqual([button["label"] for button in buttons], ["❌ 取消本轮"])
        cancel = registry[f"hermes:btn:maa-copilot:{'a' * 12}:cancel"]
        self.assertEqual(cancel["args"], ["copilot", "cancel", "a" * 12])
        self.assertEqual(len(registry), 1)
        # 登记表里再也不会出现候选按钮的键。
        self.assertFalse(
            any(":raid-" in key or key.endswith(":again") or key.endswith(":confirm")
                for key in registry)
        )
        listed = embed["fields"][0]["value"]
        self.assertIn("SR-5", listed)
        self.assertIn("突袭", listed)
        self.assertEqual(embed["fields"][1]["value"], "24")

    def test_no_module_still_builds_candidate_buttons(self) -> None:
        """候选卡整套（`build_candidate_card` / `copilot pick|again`）必须已删干净。"""
        self.assertFalse(hasattr(maa_copilot, "build_candidate_card"))
        self.assertFalse(hasattr(maa_action, "copilot_pick"))
        self.assertFalse(hasattr(maa_action, "copilot_again"))
        for argv in (["copilot", "pick", "a" * 12, "1", "normal"],
                     ["copilot", "again", "a" * 12]):
            ok, message = maa_action.dispatch(argv)
            self.assertFalse(ok)
            self.assertIn("不支持的动作", message)

    def test_copilot_prefix_survives_daily_maa_sweep(self) -> None:
        # DEV_GUIDE invariant #7: the daily card only removes hermes:btn:maa:
        # (with trailing colon); the copilot prefix uses a hyphen, so the
        # morning sweep can never touch candidate buttons.
        self.assertFalse("hermes:btn:maa-copilot:x".startswith("hermes:btn:maa:"))
        self.assertFalse("hermes:btn:maa-copilot:card:stage-select".startswith("hermes:btn:maa:"))
        self.assertFalse("hermes:btn:maa-copilot:aaa111bbb222:1".startswith("hermes:btn:maa:"))
        self.assertTrue("hermes:btn:maa-copilot:x".startswith("hermes:btn:maa-copilot:"))

    def test_prune_button_registry_only_touches_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            buttons = Path(raw) / "buttons.json"
            maa_common.atomic_write_json(buttons, {
                "hermes:btn:maa:stage-select": {},
                "hermes:btn:maa-copilot:abc123456789:1": {},
                "hermes:btn:agent-book:x": {},
            })
            with patch.object(maa_common, "BUTTONS_PATH", buttons):
                maa_common.prune_button_registry("hermes:btn:maa:")
            registry = json.loads(buttons.read_text(encoding="utf-8"))
            self.assertNotIn("hermes:btn:maa:stage-select", registry)
            self.assertIn("hermes:btn:maa-copilot:abc123456789:1", registry)
            self.assertIn("hermes:btn:agent-book:x", registry)

    def test_prune_search_buttons_removes_only_owned_ids(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            buttons = Path(raw) / "buttons.json"
            maa_common.atomic_write_json(buttons, {
                "hermes:btn:maa-copilot:aaa111bbb222:1": {},
                "hermes:btn:maa-copilot:aaa111bbb222:cancel": {},
                "hermes:btn:maa-copilot:ccc333ddd444:1": {},
                "hermes:btn:maa-copilot:card:stage-select": {},
            })
            with patch.object(maa_common, "BUTTONS_PATH", buttons):
                maa_copilot.prune_search_buttons("aaa111bbb222")
            registry = json.loads(buttons.read_text(encoding="utf-8"))
            self.assertNotIn("hermes:btn:maa-copilot:aaa111bbb222:1", registry)
            self.assertNotIn("hermes:btn:maa-copilot:aaa111bbb222:cancel", registry)
            self.assertIn("hermes:btn:maa-copilot:ccc333ddd444:1", registry)
            self.assertIn("hermes:btn:maa-copilot:card:stage-select", registry)

    def test_prune_search_buttons_registry_in_place_removes_all_owned(self) -> None:
        # P0-3: count=0 used to leave :1/:2/:3/:raid-* orphaned forever;
        # now pruning is prefix-based and mutates the caller's registry.
        registry: dict[str, Any] = {
            "hermes:btn:maa-copilot:aaa111bbb222:1": {},
            "hermes:btn:maa-copilot:aaa111bbb222:2": {},
            "hermes:btn:maa-copilot:aaa111bbb222:3": {},
            "hermes:btn:maa-copilot:aaa111bbb222:raid-1": {},
            "hermes:btn:maa-copilot:aaa111bbb222:raid-2": {},
            "hermes:btn:maa-copilot:aaa111bbb222:raid-3": {},
            "hermes:btn:maa-copilot:aaa111bbb222:again": {},
            "hermes:btn:maa-copilot:aaa111bbb222:cancel": {},
            "hermes:btn:maa-copilot:card:stage-select": {},
            "hermes:btn:agent-book:x": {},
        }
        maa_copilot.prune_search_buttons("aaa111bbb222", registry)
        leftovers = [k for k in registry if "aaa111bbb222" in k]
        self.assertEqual(leftovers, [])
        self.assertIn("hermes:btn:maa-copilot:card:stage-select", registry)
        self.assertIn("hermes:btn:agent-book:x", registry)

    def test_supersede_search_is_atomic_and_never_overwrites_executing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            searches = root / "searches"
            searches.mkdir()
            buttons = root / "buttons.json"
            token = "a" * 12
            path = searches / f"{token}.json"
            path.write_text(
                json.dumps({"token": token, "status": "scheduled", "message_id": "123"}),
                encoding="utf-8",
            )
            owned = f"hermes:btn:maa-copilot:{token}:cancel"
            buttons.write_text(json.dumps({owned: {}}), encoding="utf-8")
            with patch.object(maa_copilot, "SEARCHES_DIR", searches), \
                 patch.object(maa_copilot, "BUTTONS_PATH", buttons), \
                 patch.object(maa_copilot, "state_lock",
                              return_value=contextlib.nullcontext()):
                changed, message_id = maa_copilot.supersede_search(token)
                self.assertTrue(changed)
                self.assertEqual(message_id, "123")
                self.assertEqual(
                    json.loads(path.read_text(encoding="utf-8"))["status"],
                    "superseded",
                )
                self.assertNotIn(
                    owned, json.loads(buttons.read_text(encoding="utf-8"))
                )
                path.write_text(
                    json.dumps({"token": token, "status": "executing"}),
                    encoding="utf-8",
                )
                changed, _message_id = maa_copilot.supersede_search(token)
                self.assertFalse(changed)
                self.assertEqual(
                    json.loads(path.read_text(encoding="utf-8"))["status"],
                    "executing",
                )

    def test_prune_expired_searches_mutates_caller_registry(self) -> None:
        # P0-4: plan_card used to read the registry, prune to disk, then
        # overwrite with its stale copy -> buttons resurrected.  With the
        # registry passed in, pruning and the final write share one dict.
        with tempfile.TemporaryDirectory() as raw:
            searches = Path(raw) / "searches"
            searches.mkdir(parents=True)
            now = datetime.now().astimezone()
            stale = {"token": "b" * 12, "expires_at": (now - timedelta(hours=1)).isoformat()}
            (searches / ("b" * 12 + ".json")).write_text(json.dumps(stale), encoding="utf-8")
            registry: dict[str, Any] = {
                "hermes:btn:maa-copilot:" + "b" * 12 + ":1": {},
                "hermes:btn:maa-copilot:" + "b" * 12 + ":raid-1": {},
                "hermes:btn:maa-copilot:" + "b" * 12 + ":cancel": {},
                "hermes:btn:agent-book:x": {},
            }
            with patch.object(maa_copilot, "SEARCHES_DIR", searches):
                removed = maa_copilot.prune_expired_searches(registry)
            self.assertEqual(removed, 1)
            self.assertFalse((searches / ("b" * 12 + ".json")).exists())
            self.assertNotIn("hermes:btn:maa-copilot:" + "b" * 12 + ":1", registry)
            self.assertNotIn("hermes:btn:maa-copilot:" + "b" * 12 + ":raid-1", registry)
            self.assertNotIn("hermes:btn:maa-copilot:" + "b" * 12 + ":cancel", registry)
            self.assertIn("hermes:btn:agent-book:x", registry)

    def test_expired_searches_are_pruned_fresh_kept(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            searches = Path(raw) / "searches"
            searches.mkdir(parents=True)
            now = datetime.now().astimezone()
            fresh = {"token": "a" * 12, "expires_at": (now + timedelta(hours=1)).isoformat()}
            stale = {"token": "b" * 12, "expires_at": (now - timedelta(hours=1)).isoformat()}
            (searches / ("a" * 12 + ".json")).write_text(json.dumps(fresh), encoding="utf-8")
            (searches / ("b" * 12 + ".json")).write_text(json.dumps(stale), encoding="utf-8")
            with patch.object(maa_copilot, "SEARCHES_DIR", searches), \
                 patch.object(maa_copilot, "prune_search_buttons") as prune:
                removed = maa_copilot.prune_expired_searches()
            self.assertEqual(removed, 1)
            prune.assert_called_once_with("b" * 12, None)
            self.assertTrue((searches / ("a" * 12 + ".json")).exists())

    def test_expiry_cleanup_keeps_renewed_active_and_removes_abandoned(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            searches = Path(raw) / "searches"
            searches.mkdir(parents=True)
            expired_at = (datetime.now().astimezone() - timedelta(hours=1)).isoformat()
            renewed_at = (datetime.now().astimezone() + timedelta(hours=24)).isoformat()
            for token, status in (
                ("a" * 12, "queued"),
                ("b" * 12, "preparing"),
                ("c" * 12, "executing"),
            ):
                (searches / f"{token}.json").write_text(
                    json.dumps({
                        "token": token,
                        "status": status,
                        "expires_at": renewed_at,
                    }),
                    encoding="utf-8",
                )
            for token, status in (("d" * 12, "done"), ("e" * 12, "queued")):
                (searches / f"{token}.json").write_text(
                    json.dumps({
                        "token": token,
                        "status": status,
                        "expires_at": expired_at,
                    }),
                    encoding="utf-8",
                )
            with patch.object(maa_copilot, "SEARCHES_DIR", searches), \
                 patch.object(maa_copilot, "ensure_private_dirs"):
                removed = maa_copilot.prune_expired_searches({})
            self.assertEqual(removed, 2)
            for token in ("a" * 12, "b" * 12, "c" * 12):
                self.assertTrue((searches / f"{token}.json").exists())
            self.assertFalse((searches / f"{'d' * 12}.json").exists())
            self.assertFalse((searches / f"{'e' * 12}.json").exists())


class PickValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {"copilot_blackout_minutes": 25}

    def test_blackout_refuses_near_daily_runs(self) -> None:
        at = datetime(2026, 8, 2, 7, 50, tzinfo=BEIJING)
        with patch.object(maa_action, "now_beijing", return_value=at):
            self.assertIsNotNone(maa_action._copilot_blackout_reason(self.config))
        at = datetime(2026, 8, 2, 21, 50, tzinfo=BEIJING)
        with patch.object(maa_action, "now_beijing", return_value=at):
            self.assertIsNotNone(maa_action._copilot_blackout_reason(self.config))
        at = datetime(2026, 8, 2, 12, 0, tzinfo=BEIJING)
        with patch.object(maa_action, "now_beijing", return_value=at):
            self.assertIsNone(maa_action._copilot_blackout_reason(self.config))

    def test_blackout_month_end_never_raises(self) -> None:
        # P0-2: day+1 arithmetic blew up on the last day of the month.
        # The point is that these never raise; near-run times refuse,
        # far-from-run times pass.
        cases = [
            (datetime(2026, 8, 31, 20, 0, tzinfo=BEIJING), None),
            (datetime(2026, 8, 31, 9, 0, tzinfo=BEIJING), None),
            (datetime(2026, 2, 28, 21, 50, tzinfo=BEIJING), "refuse"),
            (datetime(2026, 12, 31, 7, 50, tzinfo=BEIJING), "refuse"),
        ]
        for at, expected in cases:
            with patch.object(maa_action, "now_beijing", return_value=at):
                reason = maa_action._copilot_blackout_reason(self.config)
            if expected is None:
                self.assertIsNone(reason, f"{at} 不应拒绝")
            else:
                self.assertIsNotNone(reason, f"{at} 应拒绝")

    def test_copilot_cancel_cleans_buttons_and_state(self) -> None:
        # NEW-1: copilot_cancel used BUTTONS_PATH without importing it
        # (NameError, ❌ button dead).  Full happy path: cancel a search,
        # all owned keys gone from buttons.json, agent-book keys untouched.
        with tempfile.TemporaryDirectory() as raw:
            searches = Path(raw) / "searches"
            searches.mkdir(parents=True)
            buttons = Path(raw) / "buttons.json"
            tok = "a" * 12
            (searches / f"{tok}.json").write_text(
                json.dumps({
                    "token": tok, "status": "scheduled", "stage": "TO-9",
                    "entries": [],
                }),
                encoding="utf-8",
            )
            maa_common.atomic_write_json(buttons, {
                f"hermes:btn:maa-copilot:{tok}:1": {},
                f"hermes:btn:maa-copilot:{tok}:raid-1": {},
                f"hermes:btn:maa-copilot:{tok}:cancel": {},
                "hermes:btn:agent-book:x": {},
            })
            with patch.object(maa_copilot, "SEARCHES_DIR", searches), \
                 patch.object(maa_action, "BUTTONS_PATH", buttons), \
                 patch.object(maa_action, "state_lock",
                              return_value=contextlib.nullcontext()):
                ok, message = maa_action.copilot_cancel(tok)
            self.assertTrue(ok)
            self.assertEqual(message, "❌ 已取消，相关按钮已清理。")
            registry = json.loads(buttons.read_text(encoding="utf-8"))
            self.assertNotIn(f"hermes:btn:maa-copilot:{tok}:1", registry)
            self.assertNotIn(f"hermes:btn:maa-copilot:{tok}:raid-1", registry)
            self.assertNotIn(f"hermes:btn:maa-copilot:{tok}:cancel", registry)
            self.assertIn("hermes:btn:agent-book:x", registry)
            state = json.loads((searches / f"{tok}.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "canceled")

class SklandTests(unittest.TestCase):
    def test_signing_golden_vector(self) -> None:
        import maa_skland
        with patch.object(maa_skland.time, "time", return_value=1785669053.0):
            sign, t = maa_skland._sign(
                "k" * 32, "/api/v1/game/player/info", "uid=300473951"
            )
        self.assertEqual(t, str(1785669053 - 2))
        self.assertEqual(len(sign), 32)
        self.assertRegex(sign, r"^[0-9a-f]{32}$")
        # Same inputs -> same sign (deterministic)
        with patch.object(maa_skland.time, "time", return_value=1785669053.0):
            sign2, _t = maa_skland._sign(
                "k" * 32, "/api/v1/game/player/info", "uid=300473951"
            )
        self.assertEqual(sign, sign2)

    def test_roster_normalization_maps_skland_fields(self) -> None:
        import maa_skland
        chars = [{
            "charId": "char_479_surtr",
            "level": 90,
            "evolvePhase": 2,
            "mainSkillLvl": 7,
            "skills": [{"id": "s1", "specializeLevel": 0}, {"id": "s2", "specializeLevel": 2}],
            "equip": [{"id": "u1", "level": 1, "locked": False}, {"id": "u2", "level": 0, "locked": True}],
            "rarity": 6,
        }]
        battle = {"char_479_surtr": {"name": "史尔特尔"}}
        normalized = maa_skland._normalize_chars(chars, battle)
        entry = normalized["char_479_surtr"]
        self.assertEqual(entry["name"], "史尔特尔")
        self.assertEqual(entry["elite"], 2)
        self.assertEqual(entry["skills"], [0, 2])
        # Locked modules keep their slot as 0 (P1-6): filtering them out
        # would shift module_levels[req_module-1] onto the wrong module.
        self.assertEqual(entry["module_levels"], [1, 0])

    def test_sanity_normalization_and_regeneration_estimate(self) -> None:
        import maa_skland

        at = datetime(2026, 8, 2, 12, 0, tzinfo=BEIJING)
        epoch = int(at.timestamp())
        sanity = maa_skland._normalize_sanity(
            {
                "currentTs": epoch,
                "status": {
                    "ap": {
                        "current": 19,
                        "max": 135,
                        "lastApAddTime": epoch - 60,
                        "completeRecoveryTime": epoch + 116 * 360,
                    }
                },
            }
        )
        self.assertIsNotNone(sanity)
        self.assertEqual(maa_copilot.estimated_sanity(sanity, at), 19)
        later = at + timedelta(minutes=12)
        self.assertEqual(maa_copilot.estimated_sanity(sanity, later), 21)
        preflight = maa_copilot.sanity_preflight(
            {"chars": {}, "sanity": sanity}, 21, at
        )
        self.assertFalse(preflight["enough"])
        self.assertIn("ready_at", preflight)

    def test_invalid_sanity_shape_is_not_faked_as_zero(self) -> None:
        import maa_skland

        self.assertIsNone(
            maa_skland._normalize_sanity(
                {"currentTs": 1, "status": {"ap": {"current": "bad"}}}
            )
        )

    def test_refresh_roster_packs_chars_and_progress_flat(self) -> None:
        # Regression: fetch_roster returning {"chars":…, "activity_progress":…}
        # must be unpacked, not nested under roster["chars"] (which made
        # load_roster see a 2-entry "chars" and lose every operator).
        import maa_skland
        with tempfile.TemporaryDirectory() as raw:
            roster_path = Path(raw) / "roster.json"
            with patch.object(maa_skland, "ROSTER_PATH", roster_path), \
                 patch.object(maa_skland, "load_skland_state",
                              return_value={"uid": "1"}), \
                 patch.object(maa_skland, "fetch_roster", return_value={
                     "chars": {"char_002_amiya": {"name": "阿米娅"}},
                     "sanity": {
                         "current": 80, "max": 135, "last_ap_add_at": 1,
                         "server_current_at": 1,
                     },
                     "activity_progress": {
                         "act53side": [{"zoneId": "act53side_zone1",
                                        "clearedStage": 1, "totalStage": 13}],
                     },
                 }):
                roster = maa_skland.refresh_roster()
            self.assertEqual(list(roster["chars"].keys()), ["char_002_amiya"])
            self.assertEqual(
                roster["activity_progress"]["act53side"][0]["clearedStage"], 1
            )
            self.assertEqual(roster["sanity"]["current"], 80)
            self.assertEqual(roster_path.stat().st_mode & 0o777, 0o600)

    def test_skland_files_are_0600_after_link(self) -> None:
        import maa_skland
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with patch.object(maa_skland, "SKLAND_PATH", root / "skland.json"), \
                 patch.object(maa_skland, "HG_TOKEN_FILE", root / "hg_token.txt"), \
                 patch.object(maa_copilot, "ensure_private_dirs"), \
                 patch.object(maa_skland, "refresh_roster", return_value={"chars": {}, "uid": "1"}), \
                 patch("sys.stdin", io.StringIO("tok123\n")):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = maa_skland.link_from_stdin()
            self.assertEqual(rc, 0)
            self.assertEqual((root / "skland.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / "hg_token.txt").stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / "hg_token.txt").read_text(encoding="utf-8"), "tok123\n")

    def test_expired_skland_cred_raises_link_guidance_after_one_retry(self) -> None:
        import maa_skland

        initial = {
            "hg_token": "secret",
            "cred": "expired",
            "sign_token": "expired-sign",
            "uid": "1",
        }
        refreshed = {
            "hg_token": "secret",
            "cred": "refreshed",
            "sign_token": "refreshed-sign",
            "uid": "1",
        }
        with tempfile.TemporaryDirectory() as raw:
            state_path = Path(raw) / "skland.json"
            with patch.object(maa_skland, "SKLAND_PATH", state_path), \
                 patch.object(maa_skland, "_ensure_cred",
                              side_effect=[initial, refreshed]) as ensure, \
                 patch.object(maa_skland, "_signed_get",
                              side_effect=RuntimeError("skland HTTP 401: 登录已过期")):
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"python3 ~/.hermes/scripts/maa_skland.py link",
                ):
                    maa_skland.fetch_roster(initial)
            persisted = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(ensure.call_count, 2)
        self.assertIn("maa_skland.py link", persisted["last_error"])

    def test_expired_skland_grant_failure_also_raises_link_guidance(self) -> None:
        import maa_skland

        initial = {
            "hg_token": "expired-secret",
            "cred": "expired",
            "sign_token": "expired-sign",
            "uid": "1",
        }
        with tempfile.TemporaryDirectory() as raw:
            state_path = Path(raw) / "skland.json"
            with patch.object(maa_skland, "SKLAND_PATH", state_path), \
                 patch.object(maa_skland, "_ensure_cred", side_effect=[
                     initial,
                     RuntimeError("skland HTTP 401: 登录已过期"),
                 ]) as ensure, \
                 patch.object(maa_skland, "_signed_get",
                              side_effect=RuntimeError("skland HTTP 401: 用户未登录")):
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"python3 ~/.hermes/scripts/maa_skland.py link",
                ):
                    maa_skland.fetch_roster(initial)
            persisted = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(ensure.call_count, 2)
        self.assertIn("maa_skland.py link", persisted["last_error"])

    def test_link_guidance_keeps_the_original_failure_cause(self) -> None:
        """`link` does not fix a network outage, so never hide why we failed."""
        import maa_skland

        initial = {
            "hg_token": "secret",
            "cred": "expired",
            "sign_token": "expired-sign",
            "uid": "1",
        }
        with tempfile.TemporaryDirectory() as raw:
            state_path = Path(raw) / "skland.json"
            with patch.object(maa_skland, "SKLAND_PATH", state_path), \
                 patch.object(maa_skland, "_ensure_cred", side_effect=[
                     initial,
                     RuntimeError("鹰角登录接口连接超时"),
                 ]), \
                 patch.object(maa_skland, "_signed_get",
                              side_effect=RuntimeError("skland HTTP 401: 用户未登录")):
                with self.assertRaises(RuntimeError) as caught:
                    maa_skland.fetch_roster(initial)
            persisted = json.loads(state_path.read_text(encoding="utf-8"))

        # Callers format this as f"{type}: {exc}" and never print a traceback,
        # so the cause has to survive in the message itself.
        self.assertIn("鹰角登录接口连接超时", str(caught.exception))
        self.assertIn("maa_skland.py link", str(caught.exception))
        self.assertIn("鹰角登录接口连接超时", persisted["last_error"])

    def test_link_guidance_truncates_a_runaway_cause(self) -> None:
        import maa_skland

        initial = {"hg_token": "secret", "cred": "expired", "uid": "1"}
        with tempfile.TemporaryDirectory() as raw:
            state_path = Path(raw) / "skland.json"
            with patch.object(maa_skland, "SKLAND_PATH", state_path), \
                 patch.object(maa_skland, "_ensure_cred", side_effect=[
                     initial,
                     RuntimeError("x" * 5000),
                 ]), \
                 patch.object(maa_skland, "_signed_get",
                              side_effect=RuntimeError("skland HTTP 401: 用户未登录")):
                with self.assertRaises(RuntimeError) as caught:
                    maa_skland.fetch_roster(initial)

        self.assertLess(len(str(caught.exception)), 400)


class DeployTokenTests(unittest.TestCase):
    def test_skland_token_env_writes_hg_token_file_not_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            hermes_home = Path(raw)
            with patch.object(deploy, "HERMES_HOME", hermes_home), \
                 patch.dict(os.environ, {"SKLAND_HG_TOKEN": "envtok"}), \
                 patch.object(deploy, "backup"), patch.object(deploy, "changed", []):
                deploy.deploy_skland_token()
            token_file = hermes_home / "state" / "maa" / "copilot" / "hg_token.txt"
            self.assertEqual(token_file.read_text(encoding="utf-8"), "envtok\n")
            self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)

    def test_deploy_skland_token_missing_env_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            hermes_home = Path(raw)
            with patch.object(deploy, "HERMES_HOME", hermes_home), \
                 patch.dict(os.environ, {}, clear=True), \
                 patch.object(deploy, "changed", []):
                deploy.deploy_skland_token()
            token_file = hermes_home / "state" / "maa" / "copilot" / "hg_token.txt"
            self.assertFalse(token_file.exists())


class ReviewFixTests(unittest.TestCase):
    """Regression tests for REVIEW_PHASE2.md findings (P0/P1/P2)."""

    def test_navigation_config_is_isolated_and_never_clicks_start(self) -> None:
        import maa_copilot_worker as worker
        import tomllib

        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw)
            resource = source / "resource" / "tasks" / "tasks.json"
            resource.parent.mkdir(parents=True)
            resource.write_text('{"ExistingTask": {"action": "Stop"}}')
            env = {"MAA_CONFIG_DIR": raw, "MAA_LOG_PREFIX": "Never"}
            with worker._navigation_config("SR-8", env) as nav_env:
                isolated = Path(nav_env["MAA_CONFIG_DIR"])
                self.assertNotEqual(isolated, source)
                task = tomllib.loads((isolated / "tasks/copilot-nav.toml").read_text())
                self.assertEqual(task["tasks"][1]["params"]["times"], 1)
                self.assertEqual(task["tasks"][1]["params"]["stage"], "SR-8")
                overlay = json.loads((isolated / "resource/tasks/tasks.json").read_text())
                self.assertIn("ExistingTask", overlay)
                for name in ("FightBegin", "CopilotNavigationReady"):
                    self.assertEqual(overlay[name]["algorithm"], "OcrDetect")
                    self.assertEqual(overlay[name]["baseTask"], "BattleStartPre")
                    self.assertEqual(overlay[name]["action"], "DoNothing")
                    self.assertEqual(overlay[name]["next"], [])
                    self.assertEqual(overlay[name]["sub"], [])
            self.assertFalse(isolated.exists())
            self.assertEqual(json.loads(resource.read_text()), {"ExistingTask": {"action": "Stop"}})
            self.assertEqual(env["MAA_CONFIG_DIR"], raw)

    def test_upstream_gate_keeps_supplies_and_main_story_navigation(self) -> None:
        import maa_clearing

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "tasks/Stages").mkdir(parents=True)
            (root / "tasks/Stages/Supplies.json").write_text('{"CE-6": {}}')
            (root / "tasks/tasks.json").write_text('{"Episode1": {}}')
            supported = maa_clearing.nav_checker([root])
            self.assertTrue(supported("CE-6")[0])
            self.assertTrue(supported("1-7")[0])
            self.assertFalse(supported("CE-99")[0])
            self.assertFalse(supported("19-7")[0])

    def test_navigation_rejects_empty_fight_success_and_accepts_screen_check(self) -> None:
        import maa_copilot_worker as worker

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            upstream = root / "upstream" / "tasks" / "Stages"
            upstream.mkdir(parents=True)
            (upstream / "SR.json").write_text('{"SR-8": {}}')
            config = {"maa_resource_dir": str(root / "upstream")}
            env = {"MAA_CONFIG_DIR": str(root / "config")}
            for output, code, expected in (
                ("Fight Completed", 0, False),
                ("Fight Completed\nCustom Completed", 0, True),
                ("Fight Completed\nCustom Completed", 1, False),
            ):
                with self.subTest(output=output, code=code), patch.object(
                    worker.subprocess, "run", return_value=SimpleNamespace(
                        returncode=code, stdout=output, stderr="",
                    ),
                ):
                    ok, _ = worker._navigate_to_stage(config, "SR-8", env, root / "nav.log")
                    self.assertEqual(ok, expected)
            with patch.object(worker.subprocess, "run") as run:
                ok, detail = worker._navigate_to_stage(config, "SR-1", env, root / "nav.log")
                self.assertFalse(ok)
                self.assertIn("SR-1", detail)
                run.assert_not_called()

    def test_prepare_op_file_unwraps_real_get_response_shape(self) -> None:
        # P0-1: /copilot/get really returns
        # {"status_code":…,"data":{…,"content":"<json string>"}}; the old
        # code read payload["content"] which is always None -> every
        # display-code op failed with "内容获取失败".
        import maa_copilot_worker as worker
        real_content = json.dumps(
            {"version": 3, "stage_name": "TO-9", "opers": []}, ensure_ascii=False
        )
        real_payload = {
            "status_code": 200,
            "data": {"id": 98660, "title": "演示", "content": real_content},
        }
        with tempfile.TemporaryDirectory() as raw:
            prepared = Path(raw) / "prepared"
            prepared.mkdir(parents=True)
            with patch.object(maa_copilot, "http_get_json", return_value=real_payload), \
                 patch.object(maa_copilot, "PREPARED_DIR", prepared):
                uri, rewrite = worker._prepare_op_file(
                    {"id": 98660, "stage_name": "TO-9"}, "act53side_09", {}
                )
            self.assertTrue(rewrite)
            self.assertTrue(uri.startswith("file://"))
            written = json.loads((prepared / "98660.json").read_text(encoding="utf-8"))
            self.assertEqual(written["stage_name"], "act53side_09")

    def test_prepare_op_file_rejects_flat_payload(self) -> None:
        # The buggy shape (content at top level) must NOT be accepted.
        import maa_copilot_worker as worker
        flat = {"content": json.dumps({"version": 3, "stage_name": "TO-9"})}
        with tempfile.TemporaryDirectory() as raw:
            prepared = Path(raw) / "prepared"
            prepared.mkdir(parents=True)
            with patch.object(maa_copilot, "http_get_json", return_value=flat), \
                 patch.object(maa_copilot, "PREPARED_DIR", prepared):
                with self.assertRaises(RuntimeError):
                    worker._prepare_op_file(
                        {"id": 98660, "stage_name": "TO-9"}, "act53side_09", {}
                    )

    def test_manual_search_hard_fails_when_skland_refresh_throws(self) -> None:
        # D13：手动抄作业**不降级**——森空岛刷新失败直接报失败红卡，
        # 绝不拿一份过期练度去挑作业（挑错了就是白打一场）。
        import maa_copilot_worker as worker
        import maa_skland

        with tempfile.TemporaryDirectory() as raw:
            searches = Path(raw) / "searches"
            searches.mkdir(parents=True)
            (searches / ("a" * 12 + ".json")).write_text(
                json.dumps({
                    "token": "a" * 12, "status": "pending", "stage": "SR-8",
                    "stage_id": "act54side_08", "modes": ["normal"],
                }),
                encoding="utf-8",
            )
            notices: list[tuple[str, str]] = []
            with patch.object(maa_copilot, "SEARCHES_DIR", searches), \
                 patch.object(maa_copilot, "SEARCH_LOCK_PATH", Path(raw) / "search.lock"), \
                 patch.object(maa_copilot, "prune_expired_searches", return_value=0), \
                 patch.object(worker, "load_config", return_value={"channel_id": "1"}), \
                 patch.object(worker, "state_lock", return_value=contextlib.nullcontext()), \
                 patch.object(worker, "_notice",
                              side_effect=lambda c, t_, b, col: notices.append((t_, b))), \
                 patch.object(maa_copilot, "load_stage_index",
                              return_value={"stages": {"act54side_08": "SR-8"}}), \
                 patch.object(maa_copilot, "stage_sanity_cost", return_value=21), \
                 patch.object(maa_copilot, "pick_perfect_candidate") as pick, \
                 patch.object(maa_skland, "refresh_roster",
                              side_effect=RuntimeError("skland down")):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = worker.cmd_search("a" * 12)
            state = json.loads((searches / ("a" * 12 + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(rc, 1)
        self.assertEqual(state["status"], "failed")
        self.assertIn("skland down", state["error"])
        self.assertIn("不降级", state["error"])
        pick.assert_not_called()
        self.assertIn("未开始", notices[0][0])

    def test_manual_search_refuses_when_no_op_fully_matches(self) -> None:
        # D12：练度不满足就直接红卡写清缺什么，绝不用助战 / 忽略练度要求。
        import maa_copilot_worker as worker
        import maa_skland

        with tempfile.TemporaryDirectory() as raw:
            searches = Path(raw) / "searches"
            searches.mkdir(parents=True)
            (searches / ("a" * 12 + ".json")).write_text(
                json.dumps({
                    "token": "a" * 12, "status": "pending", "stage": "SR-8",
                    "stage_id": "act54side_08", "modes": ["normal", "raid"],
                }),
                encoding="utf-8",
            )
            notices: list[tuple[str, str]] = []
            with patch.object(maa_copilot, "SEARCHES_DIR", searches), \
                 patch.object(maa_copilot, "SEARCH_LOCK_PATH", Path(raw) / "search.lock"), \
                 patch.object(maa_copilot, "prune_expired_searches", return_value=0), \
                 patch.object(worker, "load_config", return_value={"channel_id": "1"}), \
                 patch.object(worker, "state_lock", return_value=contextlib.nullcontext()), \
                 patch.object(worker, "_notice",
                              side_effect=lambda c, t_, b, col: notices.append((t_, b))), \
                 patch.object(maa_copilot, "load_stage_index",
                              return_value={"stages": {"act54side_08": "SR-8"}}), \
                 patch.object(maa_copilot, "stage_sanity_cost", return_value=21), \
                 patch.object(maa_copilot, "load_roster", return_value=roster({})), \
                 patch.object(maa_copilot, "pick_perfect_candidate",
                              return_value=(None, "SR-8 暂无完全满足当前干员练度的作业",
                                            ["史尔特尔 专精3（你技能7）"])), \
                 patch.object(maa_skland, "refresh_roster", return_value=roster({})):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = worker.cmd_search("a" * 12)
            state = json.loads((searches / ("a" * 12 + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(rc, 1)
        self.assertEqual(state["status"], "failed")
        self.assertIn("史尔特尔", state["error"])
        self.assertIn("练度", notices[0][1])

    def test_module_levels_keep_locked_slots_in_place(self) -> None:
        # P1-6: filtering locked equips collapsed the list and shifted
        # module_levels[req_module-1] to the wrong module.
        import maa_skland
        chars = [
            {
                "charId": "char_350_surtr",
                "level": 90,
                "evolvePhase": 2,
                "mainSkillLvl": 7,
                "skills": [{"id": 1, "specializeLevel": 2}],
                "equip": [
                    {"id": "mod1", "locked": True, "level": 0},
                    {"id": "mod2", "locked": False, "level": 3},
                ],
            }
        ]
        battle = {"char_350_surtr": {"name": "史尔特尔"}}
        normalized = maa_skland._normalize_chars(chars, battle)
        self.assertEqual(normalized["char_350_surtr"]["module_levels"], [0, 3])
        r = roster(normalized)
        satisfied, score, hard, soft = maa_copilot.check_op_requirements(
            {"name": "史尔特尔", "requirements": {"module": 1}}, r, {}
        )
        # module is a soft constraint: satisfied stays True but score halves
        self.assertTrue(satisfied)
        self.assertEqual(score, 0.5)
        self.assertTrue(any("模组1" in gap for gap in soft))
        satisfied2, _score2, hard2, _soft2 = maa_copilot.check_op_requirements(
            {"name": "史尔特尔", "requirements": {"module": 2}}, r, {}
        )
        self.assertTrue(satisfied2)
        self.assertEqual(hard2, [])
        self.assertEqual(_soft2, [])

    def test_unknown_operator_is_soft_gap_not_hard(self) -> None:
        # P2-14/NEW-2: a name missing from local battle_data (new operator,
        # stale resources) must be a soft gap with HALF score — not a fake
        # "未拥有" hard gap, and not a zero score either.
        r = roster({"char_479_surtr": char(elite=2, level=90)})
        with patch.object(maa_copilot, "name_to_char_ids", return_value=[]):
            satisfied, score, hard, soft = maa_copilot.check_op_requirements(
                {"name": "未来新干员", "requirements": {"elite": 2}}, r, {}
            )
        self.assertTrue(satisfied)
        self.assertEqual(score, 0.5)
        self.assertEqual(hard, [])
        self.assertEqual(len(soft), 1)
        self.assertIn("无法校验", soft[0])

    def test_unknown_operator_half_score_survives_evaluate_candidate(self) -> None:
        # NEW-2: the 0.5 must actually reach the candidate ratio; the old
        # code returned ok=False and evaluate_candidate's accumulator
        # (`score if ok else 0.0`) silently dropped it.
        r = roster({
            "char_002_amiya": {
                "name": "阿米娅", "level": 90, "elite": 2,
                "main_skill_lvl": 7, "skills": [3, 3, 3], "module_levels": [3],
            },
        })
        item = op_item(1, "act53side_09", opers=[
            {"name": "阿米娅", "skill": 1, "requirements": {}},
            {"name": "本地没有的新干员", "skill": 1, "requirements": {}},
        ])

        def fake_resolve(name: str, _config: dict[str, Any]) -> list[str]:
            return ["char_002_amiya"] if name == "阿米娅" else []

        with patch.object(maa_copilot, "name_to_char_ids", side_effect=fake_resolve):
            cand = maa_copilot.evaluate_candidate(item, r, CONFIG)
        self.assertIsNotNone(cand)
        self.assertEqual(cand["ratio"], 0.75)
        self.assertEqual(cand["gaps"], [])
        self.assertEqual(len(cand["soft_gaps"]), 1)

    def test_search_limit_one_returns_one(self) -> None:
        # P2-10: candidates[:max(3, limit)] ignored limit<3.
        #
        # Stub the fetch rather than prts_query: fetch_stage_copilots makes a
        # variable number of queries (probe, level_keyword paging, two document
        # searches, degraded scan), so a fixed side_effect list goes
        # StopIteration the moment that shape changes.  CACHE_DIR is redirected
        # too — this used to read and write the real ~/.hermes copilot cache and
        # only passed while a fresh act53side_09.json happened to sit there.
        # Each item needs at least one oper slot, otherwise evaluate_candidate
        # drops it and the assertion below passes/fails for the wrong reason.
        items = [
            op_item(index, "act53side_09", opers=[{"name": "x", "skill": 1}])
            for index in (1, 2)
        ]
        with tempfile.TemporaryDirectory() as raw, \
             patch.object(maa_copilot, "CACHE_DIR", Path(raw)), \
             patch.object(maa_copilot, "load_roster", return_value=None), \
             patch.object(maa_copilot, "fetch_stage_copilots", return_value=items) as fetch:
            result = maa_copilot.search_copilots(
                "act53side_09", "TO-9", {}, None, limit=1
            )
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(len(result), 1)
            # Same fetch, default limit: proves the slice follows limit.
            self.assertEqual(
                len(maa_copilot.search_copilots("act53side_09", "TO-9", {}, None)), 2
            )



class ZoneProgressTests(unittest.TestCase):
    """Preflight unlock warning (zone-level heuristic, skland data)."""

    def _roster_with_progress(
        self, zones: list[dict[str, Any]], fetched_at: str | None = None
    ) -> dict[str, Any]:
        roster: dict[str, Any] = {
            "chars": {},
            "activity_progress": {"act53side": zones},
        }
        if fetched_at:
            roster["fetched_at"] = fetched_at
        return roster

    def test_normalize_activity_progress_real_shape(self) -> None:
        import maa_skland
        activity = [
            {
                "actId": "act53side",
                "actReplicaId": "",
                "zones": [
                    {"zoneId": "act53side_zone1", "zoneReplicaId": "",
                     "clearedStage": 1, "totalStage": 13},
                    {"zoneId": "act53side_zone2", "zoneReplicaId": "",
                     "clearedStage": 0, "totalStage": 0},
                ],
            },
            {"actId": "", "zones": []},  # junk entry skipped
            "not-a-dict",
        ]
        progress = maa_skland._normalize_activity_progress(activity)
        self.assertEqual(progress["act53side"][0]["zoneId"], "act53side_zone1")
        self.assertEqual(progress["act53side"][0]["clearedStage"], 1)
        self.assertEqual(progress["act53side"][0]["totalStage"], 13)
        self.assertEqual(progress["act53side"][1]["clearedStage"], 0)
        self.assertEqual(len(progress), 1)

    def test_normalize_activity_progress_dirty_values_never_raise(self) -> None:
        # P2-25: int() on garbage used to escape fetch_roster's RuntimeError
        # retry guard and drop the whole roster for an optional hint field.
        import maa_skland
        activity = [
            {
                "actId": "act53side",
                "zones": [
                    {"zoneId": "act53side_zone1", "clearedStage": [1],
                     "totalStage": "abc"},
                    {"zoneId": "act53side_zone2", "clearedStage": None,
                     "totalStage": 0},
                ],
            }
        ]
        progress = maa_skland._normalize_activity_progress(activity)
        self.assertEqual(progress["act53side"][0]["clearedStage"], 0)
        self.assertEqual(progress["act53side"][0]["totalStage"], 0)
        self.assertEqual(progress["act53side"][1]["totalStage"], 0)

    def test_far_from_cleared_warns_both_modes(self) -> None:
        # The user's real situation: zone1 1/13 -> TO-9 almost surely locked.
        r = self._roster_with_progress([
            {"zoneId": "act53side_zone1", "clearedStage": 1, "totalStage": 13},
        ])
        warning = maa_copilot.zone_progress_warning("TO-9", "act53side_09", r)
        self.assertIsNotNone(warning)
        self.assertIn("act53side_zone1", warning)
        self.assertIn("1/13", warning)
        self.assertIn("大概率未解锁", warning)
        self.assertIn("突袭更不可能", warning)

    def test_zone_just_before_stage_warns_raid_only(self) -> None:
        # cleared == seq-1: stage 9's normal just unlocked, raid surely not.
        r = self._roster_with_progress([
            {"zoneId": "act53side_zone1", "clearedStage": 8, "totalStage": 13},
        ])
        warning = maa_copilot.zone_progress_warning("TO-9", "act53side_09", r)
        self.assertIsNotNone(warning)
        self.assertIn("的突袭大概率未解锁", warning)
        # the other tier's distinctive wording must be absent — this is the
        # "raid only" case, not the "stage itself locked" case
        self.assertNotIn("（突袭更不可能）", warning)
        self.assertIn("8/13", warning)

    def test_zone_cleared_through_stage_silent(self) -> None:
        # cleared >= seq: cannot conclude anything -> no warning.  (silence
        # means "unknown", never "unlocked" — see zone_progress_warning)
        r = self._roster_with_progress([
            {"zoneId": "act53side_zone1", "clearedStage": 9, "totalStage": 13},
        ])
        self.assertIsNone(maa_copilot.zone_progress_warning("TO-9", "act53side_09", r))

    def test_stage_past_first_zone_silent(self) -> None:
        # P0-22: numbered stages spilling past the first zone (real act17side
        # shape: zone1 6 battle stages, zone2 3).  A global battle-stage
        # number cannot be mapped onto a zone-local cleared count, so these
        # must stay silent — even with every zone at 100% cleared, a wrong
        # "not unlocked" on a fully cleared zone is worse than silence.
        r = self._roster_with_progress([
            {"zoneId": "act17side_zone1", "clearedStage": 6, "totalStage": 6},
            {"zoneId": "act17side_zone2", "clearedStage": 3, "totalStage": 3},
        ])
        self.assertIsNone(maa_copilot.zone_progress_warning("X-7", "act17side_07", r))
        self.assertIsNone(maa_copilot.zone_progress_warning("X-8", "act17side_08", r))
        self.assertIsNone(maa_copilot.zone_progress_warning("X-10", "act17side_10", r))

    def test_resource_and_main_stages_silent(self) -> None:
        r = self._roster_with_progress([
            {"zoneId": "act53side_zone1", "clearedStage": 1, "totalStage": 13},
        ])
        # resource stage: no matching activity
        self.assertIsNone(maa_copilot.zone_progress_warning("CE-6", "wk_melee_6", r))
        # main stage: stage_id shape has no trailing _<digits>
        self.assertIsNone(maa_copilot.zone_progress_warning("1-7", "main_01-07", r))

    def test_missing_progress_or_roster_silent(self) -> None:
        self.assertIsNone(
            maa_copilot.zone_progress_warning("TO-9", "act53side_09", None)
        )
        self.assertIsNone(
            maa_copilot.zone_progress_warning("TO-9", "act53side_09", {"chars": {}})
        )

    def test_zone_warning_is_available_for_the_copilot_card(self) -> None:
        r = self._roster_with_progress([
            {"zoneId": "act53side_zone1", "clearedStage": 1, "totalStage": 13},
        ])
        # 四期步骤 2：候选卡没了，这条单向启发改挂在抄作业卡的每关状态上。
        warning = maa_copilot.zone_progress_warning("TO-9", "act53side_09", r)
        self.assertIsNotNone(warning)
        self.assertIn("1/13", warning)
        self.assertIn("大概率未解锁", warning)

    def test_warning_stamps_snapshot_age_without_nesting_parens(self) -> None:
        # NEW-28/NEW-29: roster_age_hint() already carries its own （） —
        # re-wrapping produced （（森空岛数据 4 天前），如已推进请忽略）.
        # fetched_at present -> the suffix branch is actually exercised and
        # must be verbatim, with no nested parens.
        stale = (datetime.now(BEIJING) - timedelta(days=4)).isoformat()
        r = self._roster_with_progress(
            [{"zoneId": "act53side_zone1", "clearedStage": 1, "totalStage": 13}],
            fetched_at=stale,
        )
        warning = maa_copilot.zone_progress_warning("TO-9", "act53side_09", r)
        self.assertTrue(
            warning.endswith("（森空岛数据 4 天前，如已推进请忽略）"),
            f"unexpected warning: {warning!r}",
        )
        self.assertNotIn("））", warning)

    def test_warning_without_fetched_at_has_no_suffix(self) -> None:
        r = self._roster_with_progress([
            {"zoneId": "act53side_zone1", "clearedStage": 1, "totalStage": 13},
        ])
        warning = maa_copilot.zone_progress_warning("TO-9", "act53side_09", r)
        self.assertTrue(warning.endswith("点了可能空跑。"))
        self.assertNotIn("森空岛数据", warning)

    def test_roster_age_hint_tolerates_naive_timestamp(self) -> None:
        # NEW-28: naive fetched_at minus aware now raises TypeError, which
        # the old code (except ValueError only) let escape.  Tolerated =
        # returns None/"", never raises.
        naive = (datetime.now() - timedelta(days=2)).isoformat()
        roster = {"fetched_at": naive}
        self.assertIsNone(maa_copilot.roster_age_days(roster))
        self.assertEqual(maa_copilot.roster_age_hint(roster), "")


if __name__ == "__main__":
    unittest.main()
