"""清关规划（`maa_clearing` / `maa_gamedata`）：模式、目标集合、排轮、胜负判定、逐关列表。

从原 `tests/test_maa.py` 按领域拆出；共用桩见 `_helpers.py`。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

# 必须先于任何 scripts/ 模块：把 HOME 换成临时目录，碰真实 home / 外网当场炸。
import _isolation  # noqa: E402
import _helpers  # noqa: E402
import maa_clearing  # noqa: E402
import maa_common  # noqa: E402
import maa_copilot  # noqa: E402
import maa_gamedata  # noqa: E402
from _helpers import (  # noqa: E402
    BEIJING,
    RAID_OFF,
    RAID_ON,
    SR_INDEX,
    SR_KEY,
    _layout_stage,
    _nav_patch,
    _zone,
)


# 设备层桩见 `_isolation.NoRealGame`：开游戏、TERM、点屏幕、pgrep 一律当场炸。
_DEVICE_GUARDS: list = []
# 网络 / 游戏数据 / PyYAML 三组模块级桩，见 `_helpers.start_module_patches()`。
_MODULE_PATCHES: list = []


def setUpModule() -> None:
    _DEVICE_GUARDS[:] = _isolation.guard_devices()
    _MODULE_PATCHES[:] = _helpers.start_module_patches()


def tearDownModule() -> None:
    _helpers.stop_patches(_MODULE_PATCHES)
    for item in reversed(_DEVICE_GUARDS):
        item.stop()
    _isolation.assert_shared_home_clean()


class CopilotModeTests(unittest.TestCase):
    """D7：活动关普通 + 突袭；资源本 / 主线只有普通。突袭开关为空时活动关也只有普通。"""

    def test_activity_stages_need_both_modes(self) -> None:
        for stage in ("SR-1", "SR-8", "SR-EX-1", "SR-S-1"):
            with self.subTest(stage=stage):
                self.assertEqual(
                    maa_clearing.required_modes(stage, "SR", RAID_ON),
                    ("normal", "raid"),
                )

    def test_supplies_and_main_have_no_raid(self) -> None:
        for stage in ("CE-6", "1-7", "AP-5"):
            with self.subTest(stage=stage):
                self.assertEqual(
                    maa_clearing.required_modes(stage, "SR", RAID_ON), ("normal",)
                )

    def test_no_activity_means_normal_only(self) -> None:
        self.assertEqual(maa_clearing.required_modes("SR-8", "", RAID_ON), ("normal",))

    def test_an_empty_switch_task_turns_the_whole_raid_feature_off(self) -> None:
        """空开关 = 突袭功能关闭，不是「跳过切换直接打」（2026-09-09 线上缺陷）。"""
        for stage in ("SR-1", "SR-8", "SR-EX-1", "SR-S-1"):
            with self.subTest(stage=stage):
                self.assertEqual(
                    maa_clearing.required_modes(stage, "SR", RAID_OFF), ("normal",)
                )
        self.assertFalse(maa_clearing.raid_enabled(RAID_OFF))
        self.assertFalse(maa_clearing.raid_enabled({"copilot_raid_switch_task": "   "}))
        self.assertTrue(maa_clearing.raid_enabled(RAID_ON))

    def test_the_switch_task_defaults_only_when_the_key_is_absent(self) -> None:
        # 键缺失 → 取默认任务名（与 worker 的取值口径一致）；config 不是字典 → 关闭。
        self.assertEqual(
            maa_clearing.raid_switch_task({}), maa_clearing.DEFAULT_RAID_SWITCH_TASK
        )
        self.assertEqual(maa_clearing.required_modes("SR-8", "SR", {}),
                         ("normal", "raid"))
        for bad in (None, "", []):
            with self.subTest(config=bad):
                self.assertEqual(maa_clearing.raid_switch_task(bad), "")  # pyright: ignore[reportArgumentType]  # 故意传坏值
                self.assertEqual(maa_clearing.required_modes("SR-8", "SR", bad),  # pyright: ignore[reportArgumentType]  # 故意传坏值
                                 ("normal",))
        # 不传 config 一律 fail-closed。
        self.assertEqual(maa_clearing.required_modes("SR-8", "SR"), ("normal",))

    def test_entry_round_trip_keeps_the_raid_suffix(self) -> None:
        self.assertEqual(maa_clearing.make_entry("SR-5", "raid"), "SR-5#raid")
        self.assertEqual(maa_clearing.make_entry("SR-5", "normal"), "SR-5")
        self.assertEqual(maa_clearing.split_entry("SR-5#raid"), ("SR-5", "raid"))
        self.assertEqual(maa_clearing.entry_label("SR-5#raid"), "SR-5（突袭）")


class CopilotTargetSetTests(unittest.TestCase):
    """2b：目标集合与顺序。不变量 #37 的导航键闸门落在这里。"""

    def setUp(self) -> None:
        self.config: maa_common.Config = {"activity_search_retry_minutes": 30}
        self.at = datetime(2026, 9, 9, 12, 0, tzinfo=BEIJING)

    def _collect(self, *, ledger=None, roster=None, state=None, config=None,
                 keys=("SR-1", "SR-2", "SR-5", "SR-6", "SR-8", "SR-EX-1", "SR-S-1",
                       "SR-TR-1")):
        import maa_activity_watch as watch

        with _nav_patch(set(keys)):
            return watch.collect_targets(
                cast(maa_common.Config, {**self.config, **(config or {})}),
                state if state is not None else {}, "SR",
                SR_INDEX, ledger or {}, roster, self.at,
            )

    def test_the_order_is_numbered_then_ex_then_s_normal_before_raid(self) -> None:
        import maa_activity_watch as watch

        targets, _excluded = self._collect()
        self.assertEqual(
            [target["stage"] for target in targets],
            ["SR-1", "SR-2", "SR-5", "SR-6", "SR-8", "SR-EX-1", "SR-S-1"],
        )
        entries = watch._entry_list(targets[:2])
        self.assertEqual(entries, ["SR-1", "SR-1#raid", "SR-2", "SR-2#raid"])

    def test_an_empty_raid_switch_keeps_raid_entries_out_of_the_round(self) -> None:
        """突袭开关为空 → 排轮里一个 `#raid` 都不许有（2026-09-09 线上缺陷）。"""
        import maa_activity_watch as watch

        targets, _excluded = self._collect(config=RAID_OFF)
        self.assertTrue(targets)
        self.assertEqual({tuple(t["modes"]) for t in targets}, {("normal",)})
        entries = watch._entry_list(targets)
        self.assertEqual(entries, [t["stage"] for t in targets])
        self.assertFalse([e for e in entries if e.endswith(maa_clearing.RAID_SUFFIX)])

    def test_an_activity_stage_with_normal_done_is_cleared_when_raid_is_off(self) -> None:
        # 普通已通、突袭没打：开关开着要补突袭，开关关掉就算通关，不再排。
        ledger = {"stages": {"SR-1": {"normal": "2026-09-09T02:00:00+08:00",
                                      "raid": None, "attempts": []}}}
        targets, _ = self._collect(ledger=ledger, config=RAID_ON)
        self.assertEqual([t["modes"] for t in targets if t["stage"] == "SR-1"],
                         [["raid"]])
        targets, excluded = self._collect(ledger=ledger, config=RAID_OFF)
        self.assertNotIn("SR-1", [t["stage"] for t in targets])
        self.assertIn("SR-1", [e["stage"] for e in excluded if e["reason"] == "cleared"])

    def test_a_stage_without_an_upstream_nav_key_never_enters_the_set(self) -> None:
        # 不变量 #37：本地索引里有、上游 `tasks/Stages/SR.json` 里没有 → 不排。
        targets, excluded = self._collect(keys={"SR-1", "SR-2"})
        self.assertEqual([t["stage"] for t in targets], ["SR-1", "SR-2"])
        missing = {e["stage"]: e for e in excluded if e["reason"] == "no_nav"}
        self.assertIn("SR-8", missing)
        self.assertIn("没有 SR-8 这个顶层键", missing["SR-8"]["detail"])

    def test_a_raid_entry_is_gated_under_its_plain_stage_name(self) -> None:
        with _nav_patch({"SR-5"}):
            check = maa_clearing.nav_checker([])
        self.assertEqual(check("SR-5#raid"), (True, ""))
        self.assertFalse(check("SR-6#raid")[0])

    def test_tutorial_stages_never_enter_the_set(self) -> None:
        targets, _excluded = self._collect()
        self.assertNotIn("SR-TR-1", [target["stage"] for target in targets])
        self.assertNotIn("SR-TR-1", maa_copilot.activity_stage_codes(SR_INDEX, "SR"))

    def test_a_stage_with_a_complete_ledger_never_enters_the_set(self) -> None:
        ledger = {"stages": {"SR-5": {"normal": "t", "raid": "t"}}}
        targets, excluded = self._collect(ledger=ledger)
        self.assertNotIn("SR-5", [target["stage"] for target in targets])
        self.assertIn("SR-5", [e["stage"] for e in excluded if e["reason"] == "cleared"])

    def test_a_half_cleared_stage_comes_back_with_only_the_missing_mode(self) -> None:
        ledger = {"stages": {"SR-5": {"normal": "t", "raid": None}}}
        targets, _excluded = self._collect(ledger=ledger)
        target = next(t for t in targets if t["stage"] == "SR-5")
        self.assertEqual(target["modes"], ["raid"])

    def test_a_fully_cleared_zone_removes_every_stage_in_it(self) -> None:
        targets, excluded = self._collect(roster=_zone(11, 11))
        self.assertEqual([t["stage"] for t in targets], ["SR-EX-1", "SR-S-1"])
        self.assertIn("SR-5", [e["stage"] for e in excluded if e["reason"] == "cleared"])

    def test_an_unopened_zone_is_skipped_without_being_marked_failed(self) -> None:
        # D9：`totalStage == 0` 是「还没开」，等热更新，绝不记成搜索失败。
        state: dict = {}
        targets, excluded = self._collect(roster=_zone(0, 0), state=state)
        skipped = [e["stage"] for e in excluded if e["reason"] == "zone_unopened"]
        self.assertIn("SR-1", skipped)
        self.assertNotIn("SR-1", [target["stage"] for target in targets])
        self.assertEqual(state.get("stage_state", {}), {})

    def test_a_recently_failed_stage_backs_off_until_the_retry_window(self) -> None:
        state: dict = {}
        maa_clearing.record_stage_failure(
            state, "SR-5", kind="op", detail="SR-5 暂无完全满足当前干员练度的作业",
            at=self.at - timedelta(minutes=10), gaps=["史尔特尔 专精3（你技能7）"],
        )
        targets, excluded = self._collect(state=state)
        self.assertNotIn("SR-5", [target["stage"] for target in targets])
        self.assertIn("SR-5", [e["stage"] for e in excluded if e["reason"] == "backoff"])
        # 过了退避窗口就回来。
        maa_clearing.record_stage_failure(
            state, "SR-5", kind="op", detail="x", at=self.at - timedelta(minutes=31)
        )
        targets, _excluded = self._collect(state=state)
        self.assertIn("SR-5", [target["stage"] for target in targets])


class CopilotRoundPlanningTests(unittest.TestCase):
    """2c：理智预算、排轮、`scheduled_at`。"""

    def setUp(self) -> None:
        self.plan: maa_common.Plan = {"annihilation": True, "tasks": {"fight": True}}

    def test_the_budget_deducts_todays_annihilation_first(self) -> None:
        self.assertEqual(maa_clearing.sanity_budget(100, self.plan, {}), 75)
        self.assertEqual(
            maa_clearing.sanity_budget(100, {**self.plan, "annihilation": False}, {}), 100
        )
        self.assertEqual(
            maa_clearing.sanity_budget(100, self.plan, {"annihilation_sanity": 40}), 60
        )

    def test_unknown_sanity_is_a_zero_budget_not_a_guess(self) -> None:
        self.assertEqual(maa_clearing.sanity_budget(None, self.plan, {}), 0)
        self.assertEqual(maa_clearing.sanity_budget("x", self.plan, {}), 0)  # pyright: ignore[reportArgumentType]  # 故意传坏值
        self.assertEqual(maa_clearing.sanity_budget(10, self.plan, {}), 0)

    def test_plan_round_stops_at_the_budget(self) -> None:
        result = maa_clearing.plan_round(
            ["SR-5", "SR-5#raid", "SR-6", "SR-6#raid"], 30, lambda entry: 12
        )
        self.assertEqual(result["entries"], ["SR-5", "SR-5#raid"])
        self.assertEqual(result["spent"], 24)
        self.assertEqual(result["stop_reason"], "sanity")

    def test_plan_round_never_guesses_an_unknown_cost(self) -> None:
        result = maa_clearing.plan_round(
            ["SR-5", "SR-EX-1"], 200, lambda entry: None if "EX" in entry else 12
        )
        self.assertEqual(result["entries"], ["SR-5"])
        self.assertEqual(result["unknown_cost"], ["SR-EX-1"])
        self.assertEqual(result["stop_reason"], "unknown_cost")

    def _round(self, targets, budget, sanity, roster=None, state=None):
        import maa_activity_watch as watch

        with patch.object(watch, "round_budget", return_value=(budget, sanity)), \
             patch.object(maa_copilot, "stage_sanity_cost", return_value=12), \
             patch.object(
                 maa_copilot, "pick_perfect_candidate",
                 return_value=({"id": 1, "title": "稳定作业"}, "", []),
             ):
            return watch.build_round(
                {}, state if state is not None else {}, targets, SR_INDEX, roster,
                datetime(2026, 9, 9, 12, 0, tzinfo=BEIJING),
            )

    def test_a_full_budget_schedules_the_whole_prefix_right_now(self) -> None:
        targets = [
            {"stage": "SR-5", "stage_id": "act54side_05", "modes": ["normal", "raid"]},
            {"stage": "SR-6", "stage_id": "act54side_06", "modes": ["normal", "raid"]},
        ]
        entries, scheduled_at, note = self._round(targets, 30, 55)
        self.assertEqual(
            [(entry["stage"], entry["mode"]) for entry in entries],
            [("SR-5", "normal"), ("SR-5", "raid")],
        )
        self.assertEqual(note, "")
        self.assertEqual(scheduled_at, "2026-09-09T12:00:00+08:00")

    def test_an_empty_budget_schedules_one_entry_at_its_ready_at(self) -> None:
        # 排不满 → `scheduled_at` = 下一条目的 `ready_at`（`sanity_preflight()`）。
        now = datetime(2026, 9, 9, 12, 0, tzinfo=BEIJING)
        roster = {"sanity": {
            "current": 6, "max": 135,
            "server_current_at": int(now.timestamp()),
            "last_ap_add_at": int(now.timestamp()),
        }}
        targets = [{"stage": "SR-5", "stage_id": "act54side_05", "modes": ["normal"]}]
        entries, scheduled_at, note = self._round(targets, 0, 6, roster=roster)
        self.assertEqual(len(entries), 1)
        ready = datetime.fromisoformat(scheduled_at)
        # 差 6 点理智，每点 6 分钟 → 36 分钟后。
        self.assertEqual(int((ready - now).total_seconds() / 60), 36)
        self.assertIn("不足以打 SR-5", note)

    def test_a_stage_without_a_perfect_op_is_dropped_and_recorded(self) -> None:
        import maa_activity_watch as watch

        state: dict = {}
        targets = [
            {"stage": "SR-5", "stage_id": "act54side_05", "modes": ["normal"]},
            {"stage": "SR-6", "stage_id": "act54side_06", "modes": ["normal"]},
        ]

        def pick(stage, _stage_id, _config, _roster):
            if stage == "SR-5":
                return None, "SR-5 暂无完全满足当前干员练度的作业", ["史尔特尔 专精3"]
            return {"id": 2, "title": "t"}, "", []

        with patch.object(watch, "round_budget", return_value=(100, 100)), \
             patch.object(maa_copilot, "stage_sanity_cost", return_value=12), \
             patch.object(maa_copilot, "pick_perfect_candidate", side_effect=pick):
            entries, _scheduled_at, _note = watch.build_round(
                {}, state, targets, SR_INDEX, None,
                datetime(2026, 9, 9, 12, 0, tzinfo=BEIJING),
            )
        self.assertEqual([entry["stage"] for entry in entries], ["SR-6"])
        self.assertEqual(state["stage_state"]["SR-5"]["gaps"], ["史尔特尔 专精3"])
        self.assertNotIn("SR-6", state.get("stage_state", {}))


class CopilotVictoryJudgementTests(unittest.TestCase):
    """记账只在这一关被判定为通关时才写：退出码 0 不够（不变量 #33）。"""

    def test_a_failure_marker_beats_a_zero_exit_code(self) -> None:
        for marker in maa_clearing.COPILOT_FAILURE_MARKERS:
            with self.subTest(marker=marker):
                outcome, detail = maa_clearing.copilot_outcome(
                    f"MissionStart\n{marker}\n", 0
                )
                self.assertEqual(outcome, "failed")
                self.assertIn(marker, detail)

    def test_a_victory_marker_with_a_zero_exit_code_is_a_win(self) -> None:
        outcome, detail = maa_clearing.copilot_outcome(
            "MissionStart\nCopilot Completed\n", 0
        )
        self.assertEqual(outcome, "won")
        self.assertEqual(detail, "")

    def test_no_marker_at_all_is_unknown_not_a_win(self) -> None:
        outcome, detail = maa_clearing.copilot_outcome("MissionStart\n", 0)
        self.assertEqual(outcome, "unknown")
        self.assertIn("COPILOT_VICTORY_MARKERS", detail)

    def test_a_nonzero_exit_code_is_always_a_failure(self) -> None:
        self.assertEqual(
            maa_clearing.copilot_outcome("Copilot Completed", 1)[0], "failed"
        )

    def test_an_unknown_outcome_is_never_recorded_as_cleared(self) -> None:
        ledger = maa_copilot.new_ledger(SR_KEY)
        maa_copilot.record_copilot_attempt(
            ledger, "SR-5", "normal", outcome="unknown", detail="no marker"
        )
        self.assertIsNone(ledger["stages"]["SR-5"]["normal"])


def _yw_layout() -> dict:
    """照真实 ArknightsGameData 的 YW（act52side）抄的地形，只截前后几关。"""
    zone1, zone2 = "act52side_zone1", "act52side_zone2"
    chain = [
        ("act52side_st01", "YW-ST-1", True), ("act52side_01", "YW-1", False),
        ("act52side_tr01", "YW-TR-1", False), ("act52side_02", "YW-2", False),
        ("act52side_03", "YW-3", False), ("act52side_04", "YW-4", False),
        ("act52side_st02", "YW-ST-2", True), ("act52side_05", "YW-5", False),
        ("act52side_06", "YW-6", False), ("act52side_st03", "YW-ST-3", True),
    ]
    stages = {}
    previous = "main_01-10"
    for stage_id, code, story in chain:
        stages[stage_id] = _layout_stage(code, zone1, [previous], story=story)
        previous = stage_id
    stages["act52side_ex01"] = _layout_stage("YW-EX-1", zone2, ["act52side_st03"])
    stages["act52side_ex02"] = _layout_stage("YW-EX-2", zone2, ["act52side_ex01"])
    return {
        "stages": stages,
        "zones": {zone1: {"name": "雷亚-伊比利亚", "index": 0},
                  zone2: {"name": "“我们的伊比利亚”", "index": 1}},
    }


def _yw(code: str, mode: str = "normal") -> dict:
    stage_id = {
        "YW-EX-1": "act52side_ex01", "YW-EX-2": "act52side_ex02",
    }.get(code) or f"act52side_{int(code.rsplit('-', 1)[1]):02d}"
    return {"stage": code, "stage_id": stage_id, "mode": mode,
            "candidate": {"id": 100 + len(code)}, "sanity_cost": 12}


class CopilotGameDataTests(unittest.TestCase):
    def test_extract_keeps_activity_stages_and_drops_raid_variants(self) -> None:
        stage_table = {"stages": {
            "act52side_ex01": {"code": "YW-EX-1", "zoneId": "act52side_zone2",
                               "isStoryOnly": False, "apCost": 10,
                               "unlockCondition": [{"stageId": "act52side_st03"}]},
            "act52side_ex01#f#": {"code": "YW-EX-1", "zoneId": "act52side_zone2"},
            "main_01-07": {"code": "1-7", "zoneId": "main_1"},
        }}
        zone_table = {"zones": {
            "act52side_zone2": {"zoneNameSecond": "“我们的伊比利亚”", "zoneIndex": 1},
            "main_1": {"zoneNameSecond": "黑暗时代·下"},
        }}
        layout = maa_gamedata.extract_layout(stage_table, zone_table)
        self.assertEqual(list(layout["stages"]), ["act52side_ex01"])
        self.assertEqual(layout["stages"]["act52side_ex01"]["ap_cost"], 10)
        self.assertEqual(layout["stages"]["act52side_ex01"]["unlock"], ["act52side_st03"])
        self.assertEqual(maa_gamedata.zone_name(layout, "act52side_zone2"), "“我们的伊比利亚”")
        self.assertNotIn("main_1", layout["zones"])

    def test_ancestors_run_from_far_to_near_inside_one_activity(self) -> None:
        layout = _yw_layout()
        codes = [layout["stages"][s]["code"]
                 for s in maa_gamedata.ancestors(layout, "act52side_ex01")]
        self.assertEqual(codes[0], "YW-ST-1")  # 主线 1-10 不归活动管
        self.assertEqual(codes[-1], "YW-ST-3")
        self.assertIn("YW-TR-1", codes)

    def test_skland_progress_is_trusted_only_when_the_totals_match(self) -> None:
        layout = _yw_layout()
        roster = {"activity_progress": {"act52side": [
            {"zoneId": "act52side_zone1", "clearedStage": 3, "totalStage": 10},
            {"zoneId": "act52side_zone2", "clearedStage": 1, "totalStage": 8},
        ]}}
        cleared = maa_gamedata.skland_cleared(layout, roster, "act52side")
        # 区域一是链上前 3 关；区域二总数对不上（数据里只有 2 关），一关都不信。
        self.assertEqual(cleared, {"act52side_st01", "act52side_01", "act52side_tr01"})


class CopilotListPlanTests(unittest.TestCase):
    """`plan_segments()`：剧情关插段、教学关 / 缺口截断、EX 换区域。"""

    def test_a_fresh_activity_reads_the_first_story_and_stops_at_the_tutorial(self) -> None:
        segments, dropped = maa_clearing.plan_segments(
            [_yw("YW-1"), _yw("YW-2")], _yw_layout(), set()
        )
        self.assertEqual([s["kind"] for s in segments], ["story", "copilot"])
        self.assertEqual(segments[0]["code"], "YW-ST-1")
        self.assertEqual([e["stage"] for e in segments[1]["items"]], ["YW-1"])
        self.assertEqual([e["stage"] for e, _r in dropped], ["YW-2"])
        self.assertIn("教学关 YW-TR-1", dropped[0][1])

    def test_a_story_between_two_runs_splits_the_list(self) -> None:
        cleared = {"act52side_tr01"}  # 森空岛说教学关过了
        segments, dropped = maa_clearing.plan_segments(
            [_yw("YW-2"), _yw("YW-3"), _yw("YW-4"), _yw("YW-5")], _yw_layout(), cleared
        )
        self.assertEqual(dropped, [])
        self.assertEqual(
            [(s["kind"], s.get("code") or [e["stage"] for e in s["items"]]) for s in segments],
            [("copilot", ["YW-2", "YW-3", "YW-4"]), ("story", "YW-ST-2"),
             ("copilot", ["YW-5"])],
        )
        # 看过 YW-ST-1 不必再看：YW-TR-1 过了说明它前面的全过了。
        self.assertNotIn("YW-ST-1", [s.get("code") for s in segments])

    def test_ex_stages_come_after_the_last_story_in_their_own_zone(self) -> None:
        segments, _dropped = maa_clearing.plan_segments(
            [_yw("YW-EX-1"), _yw("YW-EX-2")], _yw_layout(), {"act52side_06"}
        )
        self.assertEqual([s["kind"] for s in segments], ["story", "copilot"])
        self.assertEqual(segments[0]["zone_id"], "act52side_zone1")
        self.assertEqual(segments[0]["code"], "YW-ST-3")
        self.assertEqual(segments[1]["zone_id"], "act52side_zone2")

    def test_an_uncleared_stage_without_a_job_cuts_the_rest(self) -> None:
        segments, dropped = maa_clearing.plan_segments(
            [_yw("YW-6")], _yw_layout(), {"act52side_04"}
        )
        self.assertEqual(segments[0]["code"], "YW-ST-2")
        self.assertEqual(len(segments), 1)
        self.assertIn("前置关 YW-5", dropped[0][1])

    def test_raid_needs_its_normal_first(self) -> None:
        _segments, dropped = maa_clearing.plan_segments(
            [_yw("YW-EX-1", "raid")], _yw_layout(), {"act52side_06"}
        )
        self.assertIn("普通难度还没过", dropped[0][1])
        segments, dropped = maa_clearing.plan_segments(
            [_yw("YW-EX-1"), _yw("YW-EX-1", "raid")], _yw_layout(), {"act52side_06"}
        )
        self.assertEqual(dropped, [])
        self.assertEqual([e["mode"] for e in segments[-1]["items"]], ["normal", "raid"])


def _list_event(path: str) -> str:
    return (
        "[2026-10-19 13:41:01 TRACE] UnknownSubTaskExtraInfo: {\n"
        '  "class": "asst::MultiCopilotTaskPlugin",\n'
        f'  "details": {{\n    "file_name": "{path}",\n    "id": -1,\n'
        '    "stage_name": "act52side_04"\n  },\n'
        '  "what": "CopilotListLoadTaskFileSuccess"\n}\n'
    )


class CopilotListOutcomeTests(unittest.TestCase):
    """逐关成败：第 k 份开始了 = 前 k−1 份三星通过（2026-10-19 真机日志的形状）。"""

    FILES = ["/p/a.json", "/p/b.json", "/p/c.json"]

    def test_a_completed_list_wins_every_item(self) -> None:
        output = "".join(_list_event(f) for f in self.FILES) + "[INFO ] Copilot Completed\n"
        self.assertEqual(maa_clearing.list_outcomes(self.FILES, output, 0),
                         [("won", "")] * 3)

    def test_the_last_started_item_takes_the_failure(self) -> None:
        output = _list_event(self.FILES[0]) + _list_event(self.FILES[1]) + "Copilot Error\n"
        verdicts = [v for v, _d in maa_clearing.list_outcomes(self.FILES, output, 1)]
        self.assertEqual(verdicts, ["won", "failed", "skipped"])

    def test_a_stall_timeout_fails_the_item_it_was_stuck_on(self) -> None:
        output = _list_event(self.FILES[0]) + "maa copilot 无进展超时\n"
        verdicts = [v for v, _d in maa_clearing.list_outcomes(self.FILES, output, 124)]
        self.assertEqual(verdicts, ["failed", "skipped", "skipped"])

    def test_events_out_of_order_never_count_as_wins(self) -> None:
        output = _list_event(self.FILES[1]) + "Copilot Completed\n"
        verdicts = {v for v, _d in maa_clearing.list_outcomes(self.FILES, output, 0)}
        self.assertEqual(verdicts, {"unknown"})


class CopilotZoneEntryTests(unittest.TestCase):
    # 2026-10-19 真机 OCR：「我们的伊比利亚」被认成「伊比和亚」，普通区域名也在同一屏。
    SCREEN = [
        "雷亚-伊比利並\t[1152,926,201,37]",
        "我们的伊比和亚\t[1506,959,201,37]",
        "完成「雷亚-伊比利亚」后解锁\t[1459,680,323,31]",
    ]

    def test_the_closest_zone_name_wins_despite_ocr_typos(self) -> None:
        self.assertEqual(
            maa_clearing.zone_button(self.SCREEN, "“我们的伊比利亚”", ["雷亚-伊比利亚"]),
            (1606, 977),
        )
        self.assertEqual(
            maa_clearing.zone_button(self.SCREEN, "雷亚-伊比利亚", ["“我们的伊比利亚”"]),
            (1252, 944),
        )

    def test_nothing_close_enough_means_no_tap(self) -> None:
        self.assertIsNone(
            maa_clearing.zone_button(self.SCREEN[2:], "“我们的伊比利亚”", ["雷亚-伊比利亚"])
        )


class CopilotListPrepTests(unittest.TestCase):
    def test_a_job_must_target_the_stage_and_carry_actions(self) -> None:
        good = {"stage_name": "act52side_04", "opers": [{"name": "能天使"}],
                "actions": [{"type": "Deploy"}]}
        self.assertEqual(maa_clearing.job_file_problem(good, "act52side_04"), "")
        self.assertIn("不是", maa_clearing.job_file_problem(good, "act52side_05"))
        trimmed = {**good, "actions": []}
        self.assertIn("精简版", maa_clearing.job_file_problem(trimmed, "act52side_04"))
        self.assertTrue(maa_clearing.job_file_problem("not json", "act52side_04"))

    def test_the_anchor_is_the_lowest_numbered_normal_key_with_a_next(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            stages = Path(raw) / "tasks" / "Stages"
            stages.mkdir(parents=True)
            (stages / "YW.json").write_text(json.dumps({
                "YW-OpenOpt": {"next": ["YWChapterToYW"]},
                "YW-8": {"sub": ["YW-8@YW-OpenOpt"], "next": ["YW-8@SideStoryStage"]},
                "YW-4": {"sub": ["YW-4@YW-OpenOpt"], "next": ["YW-4@SideStoryStage"]},
            }), encoding="utf-8")
            self.assertEqual(maa_clearing.pick_anchor([Path(raw)], "YW"),
                             ("YW-4", ["YW-4@SideStoryStage"], ""))
            anchor, _nexts, error = maa_clearing.pick_anchor([Path(raw)], "SR")
        self.assertEqual(anchor, "")
        self.assertIn("SR", error)


class CopilotReachableTargetTests(unittest.TestCase):
    def test_stages_behind_a_missing_job_are_not_scheduled(self) -> None:
        import maa_activity_watch as watch

        targets = [
            {"stage": "SR-1", "stage_id": "act54side_01", "modes": ["normal"]},
            {"stage": "SR-5", "stage_id": "act54side_05", "modes": ["normal"]},
        ]
        kept, reason = watch._reachable_targets(targets, {}, None, SR_INDEX)
        self.assertEqual([t["stage"] for t in kept], ["SR-1"])
        self.assertIn("前置关 SR-2", reason)



if __name__ == "__main__":
    unittest.main()
