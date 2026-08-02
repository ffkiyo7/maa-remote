from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
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
        with patch.object(maa_copilot, "battle_data", return_value={}):
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

            def counting_fetch(stage_id, code, config):
                fetched.append(1)
                return real_fetch(stage_id, code, config)

            with patch.object(maa_copilot, "fetch_stage_copilots", side_effect=counting_fetch), \
                 patch.object(maa_copilot, "CACHE_DIR", cache_dir):
                maa_copilot.search_copilots("act53side_09", "TO-9", config, None, use_cache=True)
            self.assertEqual(len(fetched), 1)  # stale -> refetch

            fresh = {
                "fetched_at": (datetime.now().astimezone() - timedelta(hours=1)).isoformat(),
                "items": [op_item(8, "act53side_09", opers=[{"name": "x", "skill": 1}])],
            }
            cache_path.write_text(json.dumps(fresh), encoding="utf-8")
            with patch.object(maa_copilot, "fetch_stage_copilots", side_effect=counting_fetch), \
                 patch.object(maa_copilot, "CACHE_DIR", cache_dir):
                candidates = maa_copilot.search_copilots("act53side_09", "TO-9", config, None, use_cache=True)
            self.assertEqual(len(fetched), 1)  # fresh cache -> no refetch
            self.assertEqual(candidates[0]["id"], 8)


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
    def test_copilot_prefix_survives_daily_maa_sweep(self) -> None:
        # DEV_GUIDE invariant #7: the daily card only removes hermes:btn:maa:
        # (with trailing colon); the copilot prefix uses a hyphen, so the
        # morning sweep can never touch candidate buttons.
        self.assertFalse("hermes:btn:maa-copilot:x".startswith("hermes:btn:maa:"))
        self.assertFalse("hermes:btn:maa-copilot:stage-select".startswith("hermes:btn:maa:"))
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
                "hermes:btn:maa-copilot:stage-select": {},
            })
            with patch.object(maa_common, "BUTTONS_PATH", buttons):
                maa_copilot.prune_search_buttons("aaa111bbb222", count=3)
            registry = json.loads(buttons.read_text(encoding="utf-8"))
            self.assertNotIn("hermes:btn:maa-copilot:aaa111bbb222:1", registry)
            self.assertNotIn("hermes:btn:maa-copilot:aaa111bbb222:cancel", registry)
            self.assertIn("hermes:btn:maa-copilot:ccc333ddd444:1", registry)
            self.assertIn("hermes:btn:maa-copilot:stage-select", registry)

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
            prune.assert_called_once_with("b" * 12)
            self.assertTrue((searches / ("a" * 12 + ".json")).exists())


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

    def test_pick_rejects_bad_token_index_mode(self) -> None:
        with patch.object(maa_action, "load_config", return_value=self.config):
            ok, _message = maa_action.copilot_pick("nothex!", "1", "normal")
            self.assertFalse(ok)
            ok, _message = maa_action.copilot_pick("a" * 12, "1", "evil")
            self.assertFalse(ok)
            ok, _message = maa_action.copilot_pick("a" * 12, "0", "normal")
            self.assertFalse(ok)

    def test_pick_rejects_out_of_range_candidate(self) -> None:
        state = {"token": "a" * 12, "candidates": [{"id": 1}]}
        with patch.object(maa_action, "load_config", return_value=self.config), \
             patch.object(maa_action, "_load_search", return_value=state):
            ok, message = maa_action.copilot_pick("a" * 12, "2", "normal")
            self.assertFalse(ok)
            self.assertIn("超出范围", message)

    def test_pick_rejects_candidate_id_out_of_bounds(self) -> None:
        state = {"token": "a" * 12, "candidates": [{"id": 10 ** 12}]}
        with patch.object(maa_action, "load_config", return_value=self.config), \
             patch.object(maa_action, "_load_search", return_value=state):
            ok, message = maa_action.copilot_pick("a" * 12, "1", "normal")
            self.assertFalse(ok)
            self.assertIn("超出安全范围", message)

    def test_pick_dispatches_worker_with_exact_argv(self) -> None:
        state = {"token": "a" * 12, "stage": "TO-9", "candidates": [{"id": 98774}]}
        with patch.object(maa_action, "load_config", return_value=self.config), \
             patch.object(maa_action, "_load_search", return_value=state), \
             patch.object(maa_action, "is_run_locked", return_value=False), \
             patch.object(maa_action.subprocess, "Popen") as popen, \
             patch.object(maa_action.Path, "is_file", return_value=True):
            ok, message = maa_action.copilot_pick("a" * 12, "1", "raid")
        self.assertTrue(ok)
        args = popen.call_args.args[0]
        self.assertEqual(args[-4:], ["run", "a" * 12, "1", "raid"])
        self.assertIn("突袭", message)


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
        self.assertEqual(entry["module_levels"], [1])  # locked module excluded

    def test_skland_files_are_0600_after_link(self) -> None:
        import maa_skland
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with patch.object(maa_skland, "SKLAND_PATH", root / "skland.json"), \
                 patch.object(maa_skland, "HG_TOKEN_FILE", root / "hg_token.txt"), \
                 patch.object(maa_skland, "refresh_roster", return_value={"chars": {}, "uid": "1"}), \
                 patch("sys.stdin", io.StringIO("tok123\n")):
                rc = maa_skland.link_from_stdin()
            self.assertEqual(rc, 0)
            self.assertEqual((root / "skland.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / "hg_token.txt").stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / "hg_token.txt").read_text(encoding="utf-8"), "tok123\n")


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


if __name__ == "__main__":
    unittest.main()
