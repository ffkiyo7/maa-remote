"""刷图推荐（`maa_farming`）：三项加权分、候选集、抓取层、掉落上限、自动定关。

从原 `tests/test_maa.py` 按领域拆出；共用桩见 `_helpers.py`。
"""
from __future__ import annotations

import contextlib
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
import maa_action  # noqa: E402
import maa_common  # noqa: E402
import maa_copilot  # noqa: E402
import maa_activity_watch  # noqa: E402
import maa_daily  # noqa: E402
import maa_farming  # noqa: E402
import maa_gamedata  # noqa: E402
import maa_plan_card  # noqa: E402
import deploy  # noqa: E402
from _helpers import BEIJING, _offline_market  # noqa: E402


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


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


# `stage/info` 的常驻哨兵值（2098-12-31 16:00 CST）。**`end` 永不为 null**，
# 判常驻只能靠 `stageType` 或这个哨兵，不能判 null（spike B §1）。
PERMANENT_SENTINEL = 4070880000000


class FarmingFixture(unittest.TestCase):
    """三项加权分的公共桩：上游资源 + 一图流三个接口的最小样本，全部离线。"""

    at = datetime(2026, 9, 10, 8, 0, tzinfo=BEIJING)

    def setUp(self) -> None:
        self._resources = tempfile.TemporaryDirectory()
        self.addCleanup(self._resources.cleanup)
        self._cache = tempfile.TemporaryDirectory()
        self.addCleanup(self._cache.cleanup)
        self.cache_dir = Path(self._cache.name)
        maa_plan_card._STAGE_DROPS_CACHE.clear()
        self.addCleanup(maa_plan_card._STAGE_DROPS_CACHE.clear)
        self.config: maa_common.Config = {
            "maa_resource_dir": self._resources.name,
            # 指到一个不存在的路径：否则 `candidate_stages()` 会去读**本机真实**的
            # `StageActivityV2.json`，结果随机器而变。单测必须密闭。
            "maa_activity_cache": str(self.cache_dir / "no-such-activity.json"),
            "activity_stages": ["SR-8"],
            "farming_stage_table": {"固源岩组": "1-7"},
            "farming_manual_weights": {"固源岩组": 100, "碳素": 40},
            "farming_weights": {"scarcity": 0.5, "gap": 0.3, "efficiency": 0.2},
        }
        self._seed_resources()
        # 解锁判据读的是**本机真实**的森空岛快照与账本，同样要密闭：
        # 默认两关普通都通过，需要「没解锁」的用例自己覆盖 `self.unlock`。
        self.unlock = (None, self.ledger_with("SR-7", "SR-8"))
        evidence = patch.object(maa_farming, "_unlock_evidence", lambda: self.unlock)
        evidence.start()
        self.addCleanup(evidence.stop)

    @staticmethod
    def ledger_with(*cleared: str, key: str = "SideStory「月行水上」|SR") -> dict:
        return {
            "activity_key": key,
            "stages": {
                stage: {"normal": "2026-09-05T03:00:00+08:00", "raid": None, "attempts": []}
                for stage in cleared
            },
        }

    @staticmethod
    def roster_with(cleared: int, total: int = 11) -> dict:
        return {
            "chars": {},
            "activity_progress": {
                "act54side": [
                    {"zoneId": "act54side_zone1", "clearedStage": cleared, "totalStage": total}
                ]
            },
        }

    # --- 上游资源（stages.json / item_index.json / 导航键） -------------------
    def _seed_resources(self) -> None:
        root = Path(self._resources.name)
        stages = [
            {"stageId": "main_01-07", "code": "1-7", "apCost": 6,
             "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "30013"}]},
            {"stageId": "main_14-11", "code": "14-12", "apCost": 21,
             "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "30043"}]},
            {"stageId": "act54side_08", "code": "SR-8", "apCost": 21,
             "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "30043"}]},
            {"stageId": "act54side_07", "code": "SR-7", "apCost": 18,
             "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "30043"}]},
            {"stageId": "wk_armor_3", "code": "SK-5", "apCost": 20, "dropInfos": []},
        ]
        (root / "stages.json").write_text(
            json.dumps(stages, ensure_ascii=False), encoding="utf-8"
        )
        (root / "item_index.json").write_text(
            json.dumps(
                {
                    "30013": {"name": "固源岩组"},
                    "30043": {"name": "异铁组"},
                    "act_token": {"name": "活动代币"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        nav = root / "tasks" / "Stages"
        nav.mkdir(parents=True, exist_ok=True)
        (nav / "SR.json").write_text(
            json.dumps({"SR-7": {}, "SR-8": {}}, ensure_ascii=False), encoding="utf-8"
        )
        maa_plan_card._STAGE_DROPS_CACHE.clear()

    # --- 外部数据（一图流 stage/info + matrix + item/v7/value） ---------------
    def _stage_info(self) -> list[dict]:
        opened = self.at - timedelta(days=6)
        closes = self.at + timedelta(days=8)
        return [
            {"stageId": "main_01-07", "stageCode": "1-7", "zoneId": "main_1",
             "stageType": "MAIN", "start": _ms(datetime(2019, 5, 1, tzinfo=BEIJING)),
             "end": PERMANENT_SENTINEL, "apCost": 6},
            {"stageId": "main_14-11", "stageCode": "14-12", "zoneId": "main_14",
             "stageType": "MAIN", "start": _ms(datetime(2023, 5, 1, tzinfo=BEIJING)),
             "end": PERMANENT_SENTINEL, "apCost": 21},
            {"stageId": "act54side_08", "stageCode": "SR-8", "zoneId": "act54side_zone1",
             "stageType": "ACT", "start": _ms(opened), "end": _ms(closes), "apCost": 21},
            {"stageId": "act54side_07", "stageCode": "SR-7", "zoneId": "act54side_zone1",
             "stageType": "ACT", "start": _ms(opened), "end": _ms(closes), "apCost": 18},
            {"stageId": "act50side_08", "stageCode": "OLD-8", "zoneId": "act50side_zone1",
             "stageType": "ACT",
             "start": _ms(self.at - timedelta(days=200)),
             "end": _ms(self.at - timedelta(days=180)), "apCost": 21},
        ]

    def _matrix(self) -> list[dict]:
        return [
            # 异铁组：活动关 27.0 理智/件 vs 主线最优 40.4 —— 活动关便宜 1.49 倍
            {"stageId": "act54side_08", "itemId": "30043",
             "times": 1590021, "quantity": 1235993, "start": 1, "end": None},
            {"stageId": "main_14-11", "itemId": "30043",
             "times": 358998, "quantity": 186720, "start": 1, "end": None},
            # 样本不足 300：一律不计
            {"stageId": "act54side_07", "itemId": "30043",
             "times": 100, "quantity": 90, "start": 1, "end": None},
            # 已经关闭的往期活动关：`end` 早于 now，不计
            {"stageId": "act50side_08", "itemId": "30043",
             "times": 900000, "quantity": 900000, "start": 1,
             "end": _ms(self.at - timedelta(days=180))},
            # 固源岩组：1-7，10 理智/件
            {"stageId": "main_01-07", "itemId": "30013",
             "times": 100000, "quantity": 60000, "start": 1, "end": None},
        ]

    def market(self, **overrides) -> dict:
        data = {
            "matrix": self._matrix(),
            "stages": self._stage_info(),
            "item_values": {"30013": 17.21, "30043": 39.0, "act_token": None},
            "store": None,
            "degraded_reasons": [],
        }
        data.update(overrides)
        return data

    # --- Depot ------------------------------------------------------------
    @staticmethod
    def depot(rates: dict[str, float], stock: dict[str, int], *, at: datetime) -> dict:
        """一条 28 天的消耗区间 + 一份库存估算。`rates` 是「每天用几个」。"""
        return {
            "consumption_history": [
                {
                    "from": (at - timedelta(days=30)).isoformat(),
                    "to": (at - timedelta(days=2)).isoformat(),
                    "days": 28.0,
                    "items": {
                        f"i{index}": {"name": name, "consumed": round(rate * 28)}
                        for index, (name, rate) in enumerate(rates.items())
                    },
                }
            ],
            "items": {
                f"s{index}": {"name": name, "quantity": quantity}
                for index, (name, quantity) in enumerate(stock.items())
            },
        }

    def result(self, *, depot=None, available=None, data=None, config=None) -> dict:
        return maa_farming.farming_result(
            config or self.config,
            depot if depot is not None else {},
            available_stages=available if available is not None else {"SR-8", "1-7"},
            at=self.at,
            data=data if data is not None else self.market(),
        )


class FarmingScoreTests(FarmingFixture):
    """四期步骤 3a：稀缺 / 缺口 / 效率 三项加权分。**全部离线**（`data=` 直接喂桩）。"""

    # --- 掉率过滤 ---------------------------------------------------------
    def test_a_low_sample_row_never_becomes_a_drop_rate(self) -> None:
        rates = maa_farming.drop_rates(self._matrix(), min_samples=300, at=self.at)
        self.assertNotIn("act54side_07", rates["30043"])
        loose = maa_farming.drop_rates(self._matrix(), min_samples=50, at=self.at)
        self.assertIn("act54side_07", loose["30043"])

    def test_an_expired_activity_row_is_not_counted(self) -> None:
        """活动关的统计只在开放期有效；往期那几行会让「历史最优」停在打不到的关上。"""
        rates = maa_farming.drop_rates(self._matrix(), min_samples=300, at=self.at)
        self.assertNotIn("act50side_08", rates["30043"])

    # --- 常驻判定：哨兵值，不是 null --------------------------------------
    def test_permanence_uses_the_sentinel_not_a_null_check(self) -> None:
        windows = maa_farming.stage_windows(self._stage_info())
        self.assertTrue(maa_farming.is_permanent(windows["main_01-07"], self.at))
        self.assertFalse(maa_farming.is_permanent(windows["act54side_08"], self.at))
        # 哨兵值 + 非常驻 stageType 也算常驻：判据是「10 年之后」而不是 null。
        sentinel_act = {"type": "ACT", "start": self.at, "ap": 21,
                        "end": maa_farming._moment(PERMANENT_SENTINEL)}
        self.assertTrue(maa_farming.is_permanent(sentinel_act, self.at))
        # `end` 是 None（企鹅那边的常驻写法）不当成「已关闭」，也不当成常驻。
        self.assertTrue(maa_farming.is_open({"start": None, "end": None}, self.at))

    # --- ① 稀缺 -----------------------------------------------------------
    def test_a_first_run_counts_down_to_the_median_rerun_gap(self) -> None:
        windows = maa_farming.stage_windows(self._stage_info())
        per_item = maa_farming.ap_per_item(
            maa_farming.drop_rates(self._matrix(), min_samples=300, at=self.at), windows
        )
        score, why = maa_farming.scarcity_score(
            "30043", per_item=per_item, windows=windows, at=self.at,
            activity_edge=1.3, rerun_days=378, horizon_days=400,
        )
        # 首发 start 是 6 天前 → 预计复刻 378-6=372 天后 → 372/400
        self.assertAlmostEqual(score, 372 / 400, places=2)
        self.assertIn("预计复刻", why)

    def test_a_rerun_is_treated_as_never_coming_back(self) -> None:
        stages = self._stage_info()
        for entry in stages:
            if entry["stageId"] == "act54side_08":
                entry["stageType"] = "ACT_REP"
        windows = maa_farming.stage_windows(stages)
        per_item = maa_farming.ap_per_item(
            maa_farming.drop_rates(self._matrix(), min_samples=300, at=self.at), windows
        )
        score, why = maa_farming.scarcity_score(
            "30043", per_item=per_item, windows=windows, at=self.at,
            activity_edge=1.3, rerun_days=378, horizon_days=400,
        )
        # end + 2 年 = 738 天后 > 400 天视野 → clamp 到 1
        self.assertEqual(score, 1.0)
        self.assertIn("不会再来", why)

    def test_scarcity_is_zero_when_the_permanent_stage_is_nearly_as_cheap(self) -> None:
        matrix = self._matrix()
        for row in matrix:
            if row["stageId"] == "main_14-11":
                # 主线也 27.5 理智/件 → 活动关只便宜 1.02 倍，未达 1.3
                row["quantity"] = int(row["times"] * 21 / 27.5)
        windows = maa_farming.stage_windows(self._stage_info())
        per_item = maa_farming.ap_per_item(
            maa_farming.drop_rates(matrix, min_samples=300, at=self.at), windows
        )
        score, why = maa_farming.scarcity_score(
            "30043", per_item=per_item, windows=windows, at=self.at,
            activity_edge=1.3, rerun_days=378, horizon_days=400,
        )
        self.assertEqual(score, 0.0)
        self.assertIn("未达", why)

    def test_a_permanent_material_is_never_scarce(self) -> None:
        windows = maa_farming.stage_windows(self._stage_info())
        per_item = maa_farming.ap_per_item(
            maa_farming.drop_rates(self._matrix(), min_samples=300, at=self.at), windows
        )
        score, _why = maa_farming.scarcity_score(
            "30013", per_item=per_item, windows=windows, at=self.at,
            activity_edge=1.3, rerun_days=378, horizon_days=400,
        )
        self.assertEqual(score, 0.0)

    def test_without_the_calendar_scarcity_degrades_to_activity_only_drops(self) -> None:
        """`stage/info` 不可用 → 退化成「当期活动关独有主掉落 = 1，其余 0」。"""
        result = self.result(
            depot=self.depot({"异铁组": 2.0}, {"异铁组": 5}, at=self.at),
            data=self.market(stages=[]),
        )
        parts = {item["material"]: item["parts"] for item in result["items"]}
        self.assertEqual(parts["异铁组"]["scarcity"], 1.0)
        # 固源岩组在手工表里（1-7 也掉），不算活动关独有
        self.assertEqual(parts["固源岩组"]["scarcity"], 0.0)

    # --- ② 缺口 -----------------------------------------------------------
    def test_the_gap_is_measured_in_sanity_not_in_pieces(self) -> None:
        result = self.result(
            depot=self.depot({"异铁组": 2.0}, {"异铁组": 5}, at=self.at)
        )
        head = next(item for item in result["items"] if item["material"] == "异铁组")
        # 30 天用 60 个，库存 5 → 还缺 55 个
        self.assertEqual(head["needed_qty"], 55)
        self.assertGreater(head["parts"]["gap"], 0)

    def test_a_null_item_value_never_reaches_the_gap_formula(self) -> None:
        """129 条里 16 条 `itemValue == null`；`float * None` 会直接崩（spike B §2）。"""
        config = self.config.copy()
        config["farming_stage_table"] = {"活动代币": "SR-8"}
        config["farming_manual_weights"] = {}
        result = maa_farming.farming_result(
            config,
            self.depot({"活动代币": 3.0}, {"活动代币": 0}, at=self.at),
            available_stages={"SR-8", "1-7"},
            at=self.at,
            data=self.market(),
        )
        token = next(item for item in result["items"] if item["material"] == "活动代币")
        self.assertEqual(token["parts"]["gap"], 0.0)

    def test_cold_start_uses_manual_weights_but_still_picks_a_stage(self) -> None:
        """改写不变量 #31：候选集受活动限制 + 有掉率兜底，**降级期也写默认关**。"""
        result = self.result(depot={})
        self.assertTrue(result["degraded"])
        self.assertFalse(result["measured"])
        stage, why = maa_farming.auto_stage(result)
        self.assertTrue(stage)
        self.assertIn("综合", why)
        self.assertIn("降级", maa_farming.farming_note(result))
        # 冷启动不设 drops 上限：缺口是猜的，拿它当「刷够就停」会提前收工。
        self.assertEqual(maa_farming.drops_cap(result), {})

    def test_zero_consumption_falls_back_to_the_manual_weight(self) -> None:
        """4 周窗口里没消耗 = 可用量**未知**，不是 0 缺口，退回手工权重。"""
        result = self.result(
            depot=self.depot({"固源岩组": 0.0}, {"固源岩组": 300}, at=self.at)
        )
        item = next(one for one in result["items"] if one["material"] == "固源岩组")
        self.assertEqual(item["basis"], "manual")
        self.assertEqual(item["needed_qty"], 0)
        self.assertGreater(item["parts"]["gap"], 0)

    # --- ③ 效率 -----------------------------------------------------------
    def test_efficiency_compares_the_candidate_best_with_the_all_time_best(self) -> None:
        windows = maa_farming.stage_windows(self._stage_info())
        per_item = maa_farming.ap_per_item(
            maa_farming.drop_rates(self._matrix(), min_samples=300, at=self.at), windows
        )
        best = maa_farming.efficiency_score(
            "30043", per_item=per_item, candidate_ids={"act54side_08", "main_14-11"}
        )
        worse = maa_farming.efficiency_score(
            "30043", per_item=per_item, candidate_ids={"main_14-11"}
        )
        self.assertEqual(best, 1.0)
        self.assertAlmostEqual(worse, 27.02 / 40.38, places=2)
        self.assertEqual(
            maa_farming.efficiency_score("30043", per_item=per_item, candidate_ids=set()),
            0.0,
        )

    # --- 权重 -------------------------------------------------------------
    def test_weights_normalize_whatever_they_sum_to(self) -> None:
        parts = {"scarcity": 1.0, "gap": 0.0, "efficiency": 0.0}
        self.assertAlmostEqual(
            maa_farming._blend(parts, {"scarcity": 5, "gap": 3, "efficiency": 2}), 0.5
        )
        self.assertAlmostEqual(
            maa_farming._blend(parts, {"scarcity": 50, "gap": 30, "efficiency": 20}), 0.5
        )
        self.assertEqual(
            maa_farming._blend(parts, {"scarcity": 0, "gap": 0, "efficiency": 0}), 0.0
        )

    def test_changing_the_weights_changes_the_order(self) -> None:
        """各给一个反例：稀缺当家时活动关材料在前，缺口当家时手工权重那个在前。"""
        depot = self.depot(
            {"异铁组": 1.0, "固源岩组": 40.0}, {"异铁组": 200, "固源岩组": 0}, at=self.at
        )
        config = self.config.copy()
        config["farming_stage_table"] = {"固源岩组": "1-7", "异铁组": "SR-8"}
        config["farming_weights"] = {"scarcity": 1, "gap": 0, "efficiency": 0}
        scarce_first = maa_farming.farming_result(
            config, depot, available_stages={"SR-8", "1-7"}, at=self.at,
            data=self.market(),
        )
        config = config.copy()
        config["farming_weights"] = {"scarcity": 0, "gap": 1, "efficiency": 0}
        gap_first = maa_farming.farming_result(
            config, depot, available_stages={"SR-8", "1-7"}, at=self.at,
            data=self.market(),
        )
        self.assertEqual(scarce_first["items"][0]["material"], "异铁组")
        self.assertEqual(gap_first["items"][0]["material"], "固源岩组")

    # --- 关卡查不到 / 今天没开 --------------------------------------------
    def test_a_material_without_a_stage_is_reported_not_guessed(self) -> None:
        config = self.config.copy()
        config["farming_stage_table"] = {}
        result = maa_farming.farming_result(
            config,
            self.depot({"聚合剂": 1.0}, {"聚合剂": 2}, at=self.at),
            available_stages={"SR-8", "1-7"},
            at=self.at,
            data=self.market(),
        )
        self.assertNotIn("聚合剂", [item["material"] for item in result["items"]])
        self.assertIn("聚合剂", result["skipped_no_stage"])

    def test_a_stage_that_is_not_open_today_is_reported_closed(self) -> None:
        config = self.config.copy()
        config["farming_stage_table"] = {"扭转醇": "9-9"}
        result = maa_farming.farming_result(
            config, {}, available_stages={"SR-8", "1-7"}, at=self.at, data=self.market()
        )
        self.assertIn("扭转醇→9-9", result["skipped_closed"])

    def test_losing_every_external_source_degrades_but_never_raises(self) -> None:
        """抓取全失败 + 缓存也没有 → 不推荐外部数据那两项，沿用手工权重，不抛。"""
        result = self.result(data=maa_farming.empty_market_data())
        self.assertTrue(result["degraded"])
        self.assertIn("外部掉率数据不可用", result["degraded_reasons"])
        for item in result["items"]:
            self.assertEqual(item["parts"]["efficiency"], 0.0)
        self.assertIn("外部掉率数据不可用", maa_farming.farming_note(result))

    def test_a_priced_material_picks_the_cheapest_open_stage(self) -> None:
        config = self.config.copy()
        config["farming_stage_table"] = {}
        result = maa_farming.farming_result(
            config,
            self.depot({"异铁组": 2.0}, {"异铁组": 0}, at=self.at),
            available_stages={"SR-8", "14-12", "1-7"},
            at=self.at,
            data=self.market(),
        )
        picked = {item["material"]: item["stage"] for item in result["items"]}
        self.assertEqual(picked["异铁组"], "SR-8")


class FarmingCandidateSetTests(FarmingFixture):
    """D5：活动期间候选集只有活动关；活动一结束退回资源本 + 1-7。"""

    def _activity_cache(self, expire: str) -> Path:
        path = Path(self._cache.name) / "StageActivityV2.json"
        path.write_text(
            json.dumps(
                {
                    "Official": {
                        "sideStoryStage": {
                            "SR": {
                                "Activity": {
                                    "Tip": "SideStory「月行水上」",
                                    "UtcStartTime": "2026/09/04 12:00:00",
                                    "UtcExpireTime": expire,
                                    "TimeZone": 8,
                                }
                            }
                        }
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def test_the_candidate_set_is_activity_only_while_it_runs(self) -> None:
        stages, in_activity, reason = maa_farming.candidate_stages(
            self.config, self.at,
            activity_cache=self._activity_cache("2026/09/18 03:59:59"),
        )
        self.assertTrue(in_activity)
        self.assertEqual(stages, {"SR-8"})
        self.assertIn("活动期", reason)
        self.assertNotIn("1-7", stages)

    def test_the_day_the_activity_ends_it_falls_back_to_supplies(self) -> None:
        stages, in_activity, _reason = maa_farming.candidate_stages(
            self.config, self.at,
            activity_cache=self._activity_cache("2026/09/09 03:59:59"),
        )
        self.assertFalse(in_activity)
        self.assertIn("1-7", stages)
        self.assertNotIn("SR-8", stages)

    def test_an_activity_stage_without_an_upstream_nav_key_is_dropped(self) -> None:
        config = self.config.copy()
        config["activity_stages"] = ["SR-8", "SR-9"]
        stages, _in_activity, _reason = maa_farming.candidate_stages(
            config, self.at,
            activity_cache=self._activity_cache("2026/09/18 03:59:59"),
        )
        self.assertEqual(stages, {"SR-8"})

    def _candidates(self, config: maa_common.Config | None = None):
        return maa_farming.candidate_stages(
            config or {**self.config, "activity_stages": ["SR-7", "SR-8"]},
            self.at,
            activity_cache=self._activity_cache("2026/09/18 03:59:59"),
        )

    def test_a_stage_skland_proves_locked_is_dropped(self) -> None:
        """2026-10-13：活动关根本没解锁，自动定关照样把理智定到它上面。"""
        # 7/11：SR-7 证明不了没通（照排），SR-8 的普通一定没通（剔掉）。
        self.unlock = (self.roster_with(cleared=7), self.ledger_with())
        stages, in_activity, reason = self._candidates()
        self.assertTrue(in_activity)
        self.assertEqual(stages, {"SR-7"})
        self.assertIn("SR-8", reason)

    def test_every_activity_stage_locked_falls_back_to_supplies(self) -> None:
        self.unlock = (self.roster_with(cleared=2), self.ledger_with())
        stages, in_activity, reason = self._candidates()
        self.assertFalse(in_activity)
        self.assertIn("1-7", stages)
        self.assertNotIn("SR-7", stages)
        self.assertNotIn("SR-8", stages)
        self.assertIn("刷不了", reason)

    def test_no_progress_and_no_ledger_means_not_farmable(self) -> None:
        """森空岛没有本期进度、账本也没记：没有任何通关证据，不押理智。"""
        self.unlock = (None, None)
        stages, in_activity, _reason = self._candidates()
        self.assertFalse(in_activity)
        self.assertNotIn("SR-8", stages)

    def test_a_ledger_clear_keeps_the_stage_without_skland(self) -> None:
        self.unlock = (None, self.ledger_with("SR-8"))
        stages, _in_activity, _reason = self._candidates()
        self.assertEqual(stages, {"SR-8"})

    def test_another_activitys_ledger_does_not_count(self) -> None:
        self.unlock = (None, self.ledger_with("SR-7", "SR-8", key="SideStory「旧」|YW"))
        stages, in_activity, _reason = self._candidates()
        self.assertFalse(in_activity)
        self.assertNotIn("SR-8", stages)

    def test_inconclusive_progress_keeps_the_stage(self) -> None:
        """进度够序号但没全清：证明不了没通，沿用以前的口径照排。"""
        self.unlock = (self.roster_with(cleared=9), self.ledger_with())
        stages, _in_activity, _reason = self._candidates()
        self.assertEqual(stages, {"SR-7", "SR-8"})

    def test_auto_stage_never_picks_a_locked_stage(self) -> None:
        self.unlock = (self.roster_with(cleared=2), self.ledger_with())
        config: maa_common.Config = {**self.config, "farming_stage_table": {}}
        result = maa_farming.farming_result(
            config,
            self.depot({"异铁组": 2.0}, {"异铁组": 0}, at=self.at),
            at=self.at,
            data=self.market(),
        )
        stage, _why = maa_farming.auto_stage(result)
        self.assertTrue(stage)
        self.assertNotIn(stage, {"SR-7", "SR-8"})


class FarmingFetchTests(unittest.TestCase):
    """抓取层：ETag 条件请求、缓存兜底、**失败一律不抛**（D-2 的教训）。"""

    def setUp(self) -> None:
        self._cache = tempfile.TemporaryDirectory()
        self.addCleanup(self._cache.cleanup)
        self.cache_dir = Path(self._cache.name)
        self.at = datetime(2026, 9, 10, 8, 0, tzinfo=BEIJING)

    def _seed(self, name: str, payload, *, etag: str = "", age_hours: float = 48) -> None:
        maa_common.atomic_write_json(
            self.cache_dir / f"{name}.json",
            {
                "fetched_at": (self.at - timedelta(hours=age_hours)).isoformat(),
                "etag": etag,
                "payload": payload,
            },
        )

    def test_every_source_failing_falls_back_to_the_cache(self) -> None:
        self._seed("matrix", {"matrix": [{"stageId": "s", "itemId": "i",
                                          "times": 400, "quantity": 200}]})
        with patch.object(maa_farming, "_http_json", side_effect=RuntimeError("网络错误")):
            data = maa_farming.load_market_data({}, at=self.at, cache_dir=self.cache_dir)
        self.assertEqual(len(data["matrix"]), 1)
        self.assertTrue(any("缓存" in reason for reason in data["degraded_reasons"]))

    def test_no_cache_and_no_network_degrades_instead_of_raising(self) -> None:
        with patch.object(maa_farming, "_http_json", side_effect=RuntimeError("网络错误")):
            data = maa_farming.load_market_data({}, at=self.at, cache_dir=self.cache_dir)
        self.assertEqual(data["matrix"], [])
        self.assertEqual(data["stages"], [])
        self.assertEqual(data["item_values"], {})
        self.assertTrue(data["degraded_reasons"])

    def test_a_304_keeps_the_cached_matrix(self) -> None:
        """`Cache-Control: max-age=7200` 不可信，用 `ETag`（spike B §5）。"""
        self._seed(
            "matrix",
            {"matrix": [{"stageId": "s", "itemId": "i", "times": 400, "quantity": 200}]},
            etag='"abc"',
        )
        seen: list[str] = []

        def stub(url, *, method="GET", body=None, etag="", timeout=20):
            seen.append(etag)
            if "matrix.json" in url:
                return 304, None, etag
            raise RuntimeError("其它来源本例不关心")

        with patch.object(maa_farming, "_http_json", side_effect=stub):
            data = maa_farming.load_market_data({}, at=self.at, cache_dir=self.cache_dir)
        self.assertIn('"abc"', seen)
        self.assertEqual(len(data["matrix"]), 1)
        # 304 不是失败，不该被写成降级原因。
        self.assertFalse(any("matrix 抓取失败" in r for r in data["degraded_reasons"]))

    def test_a_future_timestamp_is_never_treated_as_fresh(self) -> None:
        """时钟跳变或写坏的时间戳会把缓存冻到那个未来时刻，宁可多抓一次。"""
        self._seed("item_value", [{"itemId": "x", "itemValue": 1.0}], age_hours=-240)
        calls: list[str] = []

        def stub(url, *, method="GET", body=None, etag="", timeout=20):
            calls.append(url)
            return 200, [{"itemId": "30013", "itemValue": 17.2}], ""

        with patch.object(maa_farming, "_http_json", side_effect=stub):
            data = maa_farming.load_market_data({}, at=self.at, cache_dir=self.cache_dir)
        self.assertTrue(any("item/v7/value" in url for url in calls))
        self.assertEqual(data["item_values"], {"30013": 17.2})

    def test_a_fresh_cache_is_not_requested_again(self) -> None:
        self._seed("item_value", [{"itemId": "30013", "itemValue": 17.2}], age_hours=1)
        with patch.object(maa_farming, "_http_json", side_effect=RuntimeError("不该被调用")):
            data = maa_farming.load_market_data({}, at=self.at, cache_dir=self.cache_dir)
        self.assertEqual(data["item_values"], {"30013": 17.2})

    def test_penguin_zones_and_stages_become_the_yituliu_shape(self) -> None:
        """企鹅 `closeTime: null` = 常驻，一图流用哨兵——统一成一套（spike B §6a）。"""
        converted = maa_farming._penguin_stage_info(
            [
                {"zoneId": "main_1", "type": "MAINLINE",
                 "existence": {"CN": {"openTime": 1, "closeTime": None}}},
                {"zoneId": "act54side_zone1", "type": "ACTIVITY",
                 "existence": {"CN": {"openTime": 100, "closeTime": 200}}},
            ],
            [
                {"stageId": "main_01-07", "zoneId": "main_1", "code": "1-7", "apCost": 6},
                {"stageId": "act54side_08", "zoneId": "act54side_zone1",
                 "code": "SR-8", "apCost": 21},
            ],
        )
        by_id = {entry["stageId"]: entry for entry in converted}
        self.assertEqual(by_id["main_01-07"]["end"], PERMANENT_SENTINEL)
        self.assertEqual(by_id["main_01-07"]["stageType"], "MAIN")
        self.assertEqual(by_id["act54side_08"]["end"], 200)

    def test_stage_ap_cost_only_reads_the_cache(self) -> None:
        self._seed("stage_info", [{"stageId": "act54side_08", "stageCode": "SR-8",
                                   "stageType": "ACT", "apCost": 21}])
        with patch.object(maa_farming, "_http_json", side_effect=RuntimeError("不该联网")):
            self.assertEqual(
                maa_farming.stage_ap_cost("act54side_08", {}, cache_dir=self.cache_dir), 21
            )
            self.assertIsNone(
                maa_farming.stage_ap_cost("act54side_EX-1", {}, cache_dir=self.cache_dir)
            )


class FarmingExCostFallbackTests(unittest.TestCase):
    """步骤 2 留的坑：EX 关不在上游 `stages.json` 里，理智查表要有第二级。"""

    def test_an_ex_stage_falls_back_to_the_stage_info_cache(self) -> None:
        index = {"codes": {"SR-EX-1": "act54side_ex01"},
                 "stages": {"act54side_ex01": "SR-EX-1"}}
        with patch.object(maa_copilot, "stage_sanity_cost", return_value=None), \
             patch.object(maa_farming, "stage_ap_cost", return_value=27) as fallback:
            cost_of = maa_activity_watch.cost_lookup({}, index)
            self.assertEqual(cost_of("SR-EX-1#raid"), 27)
        # 突袭条目按**普通关名**查（不变量 #34）
        fallback.assert_called_once_with("act54side_ex01", {})

    def test_the_upstream_table_still_wins_when_it_knows_the_stage(self) -> None:
        index = {"codes": {"SR-8": "act54side_08"}, "stages": {"act54side_08": "SR-8"}}
        with patch.object(maa_copilot, "stage_sanity_cost", return_value=21), \
             patch.object(maa_farming, "stage_ap_cost") as fallback:
            self.assertEqual(maa_activity_watch.cost_lookup({}, index)("SR-8"), 21)
        fallback.assert_not_called()

    def test_neither_source_knowing_the_stage_still_fails_closed(self) -> None:
        index = {"codes": {"SR-EX-1": "act54side_ex01"},
                 "stages": {"act54side_ex01": "SR-EX-1"}}
        with patch.object(maa_copilot, "stage_sanity_cost", return_value=None), \
             patch.object(maa_farming, "stage_ap_cost", return_value=None), \
             patch.object(maa_gamedata, "load_layout",
                          return_value={"stages": {}, "zones": {}}):
            self.assertIsNone(maa_activity_watch.cost_lookup({}, index)("SR-EX-1"))

    def test_game_data_is_the_third_source_for_ex_costs(self) -> None:
        # 上游 `stages.json` 与一图流都没有 EX 关；游戏数据（`maa_gamedata`）有。
        index = {"codes": {"SR-EX-1": "act54side_ex01"},
                 "stages": {"act54side_ex01": "SR-EX-1"}}
        with patch.object(maa_copilot, "stage_sanity_cost", return_value=None), \
             patch.object(maa_farming, "stage_ap_cost", return_value=None):
            self.assertEqual(maa_activity_watch.cost_lookup({}, index)("SR-EX-1"), 10)


class FarmingDropsTests(unittest.TestCase):
    """四期步骤 3b：Fight `drops` 上限 + 刷够之后的第二个 Fight 块。"""

    def setUp(self) -> None:
        self.config: maa_common.Config = {
            "medicine_expire_days": 2,
            "fight_times": 2147483647,
            "recruit_times": 4,
        }
        self.plan = maa_common.normalize_plan(
            {
                "stage": "SR-8",
                "tasks": {},
                "farming_drops": {"30043": 55},
                "farming_fallback_stage": "1-7",
            }
        )

    def test_the_first_block_carries_the_drops_cap(self) -> None:
        rendered = maa_daily.render_daily_toml(self.plan, self.config)
        self.assertIn('drops = { "30043" = 55 }', rendered)
        self.assertLess(
            rendered.index('name = "刷理智"'), rendered.index('drops = { "30043" = 55 }')
        )

    def test_the_second_block_exists_has_no_drops_and_keeps_the_literals(self) -> None:
        rendered = maa_daily.render_daily_toml(self.plan, self.config)
        self.assertIn('name = "刷理智·续"', rendered)
        self.assertEqual(rendered.count("drops = "), 1)
        self.assertEqual(rendered.count("stone = 0"), 3)   # 剿灭 + 两个刷理智块
        self.assertEqual(rendered.count("series = 0"), 3)
        self.assertEqual(rendered.count("medicine = 0"), 3)
        second = rendered[rendered.index('name = "刷理智·续"'):]
        self.assertIn('stage = "1-7"', second)
        self.assertNotIn("drops", second)

    def test_the_two_blocks_sit_together_between_annihilation_and_infrast(self) -> None:
        """任务链顺序不变（禁止触碰项）：两个 Fight 块紧挨着。"""
        rendered = maa_daily.render_daily_toml(self.plan, self.config)
        order = [
            rendered.index(marker)
            for marker in (
                'name = "每日剿灭"',
                'name = "刷理智"',
                'name = "刷理智·续"',
                'name = "基建换班"',
            )
        ]
        self.assertEqual(order, sorted(order))

    def test_no_gap_means_no_drops_and_no_second_block(self) -> None:
        plan = maa_common.normalize_plan({"stage": "1-7", "tasks": {}})
        rendered = maa_daily.render_daily_toml(plan, self.config)
        self.assertNotIn("drops", rendered)
        self.assertNotIn("刷理智·续", rendered)
        self.assertEqual(rendered.count('type = "Fight"'), 2)  # 剿灭 + 刷理智

    def test_a_fallback_stage_without_a_cap_renders_only_one_block(self) -> None:
        """第一个块没上限时本来就打到理智不足，第二块没有意义。"""
        plan = maa_common.normalize_plan(
            {"stage": "1-7", "tasks": {}, "farming_fallback_stage": "CE-6"}
        )
        rendered = maa_daily.render_daily_toml(plan, self.config)
        self.assertNotIn("刷理智·续", rendered)

    def test_a_bad_item_id_never_reaches_the_toml(self) -> None:
        """itemId 会原样变成 TOML 表键，只接受上游那套字符集。"""
        plan = maa_common.normalize_plan(
            {
                "stage": "1-7",
                "tasks": {},
                "farming_drops": {'ev"il': 5, "30013": 0, "mod_unlock_token": 3},
                "farming_fallback_stage": "CE-6",
            }
        )
        self.assertEqual(plan["farming_drops"], {"mod_unlock_token": 3})
        rendered = maa_daily.render_daily_toml(plan, self.config)
        self.assertIn('drops = { "mod_unlock_token" = 3 }', rendered)
        self.assertNotIn("ev", rendered.split("drops = ")[1].splitlines()[0])

    def test_the_farming_block_switch_still_removes_both(self) -> None:
        """理智让路只摘刷理智、保留剿灭（不变量 #26）——两个块一起摘。"""
        rendered = maa_daily.render_daily_toml({**self.plan, "farming": False}, self.config)
        self.assertNotIn('name = "刷理智"', rendered)
        self.assertNotIn('name = "刷理智·续"', rendered)
        self.assertIn('name = "每日剿灭"', rendered)

    # --- 战报解析 ---------------------------------------------------------
    LOG = [
        "[刷理智] Fight SR-8 3 times",
        "Drops: 异铁组 × 4",
        "[刷理智] Fight Completed",
        "[刷理智·续] Fight 1-7 5 times",
        "Drops: 固源岩组 × 6",
        "[刷理智·续] Fight Completed",
        "[基建换班] Infrast Completed",
    ]

    def test_both_blocks_feed_one_drop_ledger(self) -> None:
        merged = dict(maa_daily._drop_pairs(maa_daily.farming_sections(self.LOG)))
        self.assertEqual(merged, {"异铁组": 4, "固源岩组": 6})

    def test_the_report_lists_the_second_block_separately(self) -> None:
        summary = maa_daily.extract_summary(self.LOG, [], plan=self.plan, ok=True)
        self.assertIn("刷理智·续", summary)
        self.assertIn("关卡 1-7", summary)
        self.assertIn("关卡 SR-8", summary)
        # 第一个块只统计自己那段，别把第二块的掉落算进来。
        first = summary[summary.index("刷理智："): summary.index("刷理智·续")]
        self.assertIn("异铁组 ×4", first)
        self.assertNotIn("固源岩组", first)

    def test_a_second_block_that_never_ran_is_not_reported_as_done(self) -> None:
        """`_task_state()` 的通配退路是 `"<key> Completed"`——两个块共用 `Fight`
        这个 key 会让根本没跑过的第二块被判成完成，所以它的 key 是 `Fight2`。"""
        lines = ["[刷理智] Fight Completed"]
        self.assertEqual(maa_daily._task_state(lines, "Fight", "刷理智"), "completed")
        self.assertEqual(maa_daily._task_state(lines, "Fight2", "刷理智·续"), "pending")

    def test_a_plan_without_a_second_block_does_not_show_the_step(self) -> None:
        plan = maa_common.normalize_plan({"stage": "1-7", "tasks": {}})
        summary = maa_daily.extract_summary(["[刷理智] Fight Completed"], [], plan=plan, ok=True)
        self.assertNotIn("刷理智·续", summary)

    # --- 计划字段 ---------------------------------------------------------
    def test_effective_plan_carries_the_cap_and_now_gets_none(self) -> None:
        at = datetime(2026, 9, 30, 8, 0, tzinfo=BEIJING)
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            defaults = Path(tmp) / "defaults.json"
            maa_common.atomic_write_json(defaults, {"stage": "1-7"})
            maa_common.atomic_write_json(
                plan_path,
                {
                    "date": at.date().isoformat(),
                    "sessions": {
                        "am": {
                            "stage": "SR-8",
                            "stage_source": "auto",
                            "farming_drops": {"30043": 55},
                            "farming_fallback_stage": "1-7",
                        }
                    },
                },
            )
            with patch.object(maa_common, "PLAN_PATH", plan_path), patch.object(
                maa_common, "DEFAULTS_PATH", defaults
            ):
                morning = maa_common.effective_plan(at, "am")
                evening = maa_common.effective_plan(at, "pm")
                manual = maa_common.effective_plan(at, "now")
        self.assertEqual(morning["farming_drops"], {"30043": 55})
        self.assertEqual(morning["farming_fallback_stage"], "1-7")
        self.assertNotIn("farming_drops", evening)
        self.assertNotIn("farming_drops", manual)
        self.assertNotIn("farming_fallback_stage", manual)

    def test_picking_a_stage_by_hand_clears_the_cap(self) -> None:
        """手动选的关不带 drops 上限：那个上限是给缺口第一名算的。"""
        at = maa_common.now_beijing()
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            defaults = Path(tmp) / "defaults.json"
            maa_common.atomic_write_json(defaults, {"stage": "1-7"})
            maa_common.atomic_write_json(
                plan_path,
                {
                    "date": at.date().isoformat(),
                    "sessions": {
                        "am": {
                            "stage": "SR-8",
                            "stage_source": "auto",
                            "farming_drops": {"30043": 55},
                            "farming_fallback_stage": "1-7",
                        }
                    },
                },
            )
            with patch.object(maa_action, "PLAN_PATH", plan_path), patch.object(
                maa_common, "PLAN_PATH", plan_path
            ), patch.object(maa_common, "DEFAULTS_PATH", defaults), patch.object(
                maa_action, "load_config", return_value={}
            ), patch.object(
                maa_action, "is_run_locked", return_value=False
            ), patch.object(
                maa_action, "state_lock", return_value=contextlib.nullcontext()
            ):
                ok, _message = maa_action.set_stage("CE-6", "am")
                self.assertTrue(ok)
                plan = maa_common.effective_plan(at, "am")
        self.assertEqual(plan["stage"], "CE-6")
        self.assertNotIn("farming_drops", plan)
        self.assertNotIn("farming_fallback_stage", plan)

    def test_the_auto_stage_writer_records_the_cap_and_the_spare_stage(self) -> None:
        result = {
            "degraded": False,
            "measured": True,
            "items": [
                {"material": "异铁组", "item_id": "30043", "stage": "SR-8", "score": 0.9,
                 "parts": {"scarcity": 1.0, "gap": 1.0, "efficiency": 1.0},
                 "needed_qty": 55, "ap_per_item": 27.0, "stock": 5, "per_day": 2.0,
                 "basis": "measured"},
                {"material": "固源岩组", "item_id": "30013", "stage": "1-7", "score": 0.2,
                 "parts": {"scarcity": 0.0, "gap": 0.4, "efficiency": 1.0},
                 "needed_qty": 0, "ap_per_item": 10.0, "stock": 900, "per_day": 1.0,
                 "basis": "measured"},
            ],
        }
        at = datetime(2026, 9, 30, 7, 50, tzinfo=BEIJING)
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            with patch.object(maa_plan_card, "PLAN_PATH", plan_path), patch.object(
                maa_common, "PLAN_PATH", plan_path
            ), patch.object(
                maa_plan_card, "state_lock", return_value=contextlib.nullcontext()
            ):
                self.assertEqual(maa_plan_card.apply_auto_stage(result, "am", at), "SR-8")
                written = json.loads(plan_path.read_text(encoding="utf-8"))
        session = written["sessions"]["am"]
        self.assertEqual(session["farming_drops"], {"30043": 55})
        self.assertEqual(session["farming_fallback_stage"], "1-7")


class FarmingConfigTests(unittest.TestCase):
    def test_the_new_farming_keys_are_seeded_with_the_documented_defaults(self) -> None:
        seed = deploy.load_json(deploy.ROOT / "state" / "config.json", {})
        for key, default in maa_farming.FARMING_DEFAULTS.items():
            self.assertIn(key, seed)
            self.assertEqual(seed[key], default, key)

    def test_the_new_farming_keys_are_repo_owned_not_runtime_caches(self) -> None:
        # 混进 PRESERVED_RUNTIME_CACHE_KEYS 会让用户改了仓库种子却永远不生效
        # （不变量 #25 的反面）。
        for key in maa_farming.FARMING_DEFAULTS:
            self.assertNotIn(key, deploy.PRESERVED_RUNTIME_CACHE_KEYS)

    def test_bad_config_values_fall_back_to_the_defaults(self) -> None:
        broken = cast(maa_common.Config, {  # 故意传坏值
            "farming_weights": {"scarcity": "x", "gap": -1},
            "farming_min_samples": "nope",
            "farming_target_days": 0,
        })
        self.assertEqual(maa_farming.weights(broken)["scarcity"], 0.5)
        self.assertEqual(maa_farming.weights(broken)["gap"], 0.0)
        self.assertEqual(maa_farming.setting(broken, "farming_min_samples"), 300)
        self.assertEqual(maa_farming.setting(broken, "farming_target_days"), 30)


class FarmingAutoStageTests(unittest.TestCase):
    """材料缺口 → 自动定关（2026-09-08 改版）：下拉删掉，映射从上游派生。

    改版前是「第三个下拉，按材料缺口排序让用户选」。删掉的理由：缺口天天变，
    下拉却只能选关、显示不了缺口，和第一个下拉语义完全重复。现在缺口是**后台
    固定计算**，结果直接写进当天计划，卡片上说明为什么。
    """

    def setUp(self) -> None:
        self._resources = tempfile.TemporaryDirectory()
        self.addCleanup(self._resources.cleanup)
        maa_plan_card._STAGE_DROPS_CACHE.clear()
        self.addCleanup(maa_plan_card._STAGE_DROPS_CACHE.clear)
        self.plan = maa_common.normalize_plan({"stage": "1-7"})
        self.config: maa_common.Config = {
            "channel_id": "1",
            "activity_stages": [],
            "maa_resource_dir": self._resources.name,
            "farming_stage_table": {"固源岩组": "1-7"},
            "farming_manual_weights": {"固源岩组": 100},
        }

    def _seed_resources(self, stages: list[dict], items: dict[str, str]) -> None:
        """写一份最小的上游资源：`stages.json` + `item_index.json`。"""
        root = Path(self._resources.name)
        (root / "stages.json").write_text(
            json.dumps(stages, ensure_ascii=False), encoding="utf-8"
        )
        (root / "item_index.json").write_text(
            json.dumps({k: {"name": v} for k, v in items.items()}, ensure_ascii=False),
            encoding="utf-8",
        )
        maa_plan_card._STAGE_DROPS_CACHE.clear()

    def _components(self, phase: maa_common.PlanPhase = "am"):
        with patch.object(maa_plan_card, "load_depot_state", return_value={}):
            return maa_plan_card.build_components(self.plan, self.config, phase)

    # --- 只剩一个下拉 ----------------------------------------------------
    def test_only_the_farming_stage_select_remains(self) -> None:
        rows, registry = self._components()
        self.assertNotIn("hermes:btn:maa:farming-select", registry)
        self.assertIn("hermes:btn:maa:am:stage-select", registry)
        # 抄作业下拉搬到了独立卡（D6），计划卡上不再有它。
        self.assertNotIn("hermes:btn:maa-copilot:stage-select", registry)
        custom_ids = [
            component["custom_id"]
            for row in rows
            for component in row["components"]
            if component.get("type") == 3
        ]
        self.assertEqual(custom_ids, ["hermes:btn:maa:am:stage-select"])
        self.assertEqual(len(rows), 2)

    # --- 派生映射：新活动自动跟随 ----------------------------------------
    def test_activity_stage_labels_carry_their_drop_from_upstream(self) -> None:
        """新活动上线后不改任何配置，下拉 1 就该显示它掉什么。"""
        self._seed_resources(
            [
                {
                    "code": "SR-8",
                    "apCost": 21,
                    "dropInfos": [
                        {"dropType": "NORMAL_DROP", "itemId": "30043"},
                        {"dropType": "FURNITURE", "itemId": "furni"},
                    ],
                }
            ],
            {"30043": "异铁组"},
        )
        self.config["activity_stages"] = ["SR-8"]
        labels = {
            stage: label
            for label, stage, _desc in maa_plan_card._stage_choices(
                self.plan, self.config
            )
        }
        self.assertEqual(labels["SR-8"], "SR-8 · 异铁组")

    def test_extra_and_furniture_drops_are_not_treated_as_the_stage_material(self) -> None:
        """只认 NORMAL_DROP：按副掉落定关，期望产出可能差一个数量级。"""
        self._seed_resources(
            [
                {
                    "code": "1-7",
                    "apCost": 6,
                    "dropInfos": [
                        {"dropType": "NORMAL_DROP", "itemId": "1"},
                        {"dropType": "EXTRA_DROP", "itemId": "2"},
                        {"dropType": "SPECIAL_DROP", "itemId": "3"},
                        {"dropType": "FURNITURE", "itemId": "furni"},
                    ],
                }
            ],
            {"1": "固源岩", "2": "赤金", "3": "家具零件"},
        )
        self.assertEqual(
            maa_plan_card.stage_drops(self.config)["1-7"]["materials"], ["固源岩"]
        )

    def test_a_material_resolves_to_an_open_stage_not_a_closed_past_activity(self) -> None:
        """同一材料多关掉时，必须在**今天可选**的关里挑。

        这是派生表最容易出的错：`异铁组` 在很多往期活动关里都掉，全局定死一个
        就会选中早已关闭的 DH-8，然后在「今天可选」那步被整条丢掉——材料明明
        有开放的关能刷，却从推荐里消失了。
        """
        self._seed_resources(
            [
                {"code": "DH-8", "apCost": 18,
                 "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "30043"}]},
                {"code": "SR-8", "apCost": 21,
                 "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "30043"}]},
            ],
            {"30043": "异铁组"},
        )
        candidates = maa_plan_card.material_stage_candidates(self.config)
        self.assertEqual(candidates["异铁组"], ["DH-8", "SR-8"])  # 低理智在前
        depot = {
            "consumption_history": [
                {
                    "from": "2026-09-01T00:00:00+08:00",
                    "to": "2026-09-29T00:00:00+08:00",
                    "days": 28,
                    "items": {"a": {"name": "异铁组", "consumed": 28, "per_day": 1.0}},
                }
            ],
            "items": {"a": {"name": "异铁组", "quantity": 10}},
        }
        result = maa_plan_card.farming_recommendations(
            self.config, depot, {"1-7", "SR-8"},
            datetime(2026, 9, 30, 8, 0, tzinfo=BEIJING),
        )
        picked = {item["material"]: item["stage"] for item in result["items"]}
        self.assertEqual(picked["异铁组"], "SR-8")
        self.assertEqual(result["skipped_closed"], [])

    def test_the_manual_table_wins_over_the_derived_one(self) -> None:
        """手工表覆盖派生表：资源本只有它覆盖得到，且用户写下的不该被悄悄改掉。"""
        self._seed_resources(
            [{"code": "2-4", "apCost": 12,
              "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "9"}]}],
            {"9": "固源岩组"},
        )
        candidates = maa_plan_card.material_stage_candidates(self.config)
        self.assertEqual(candidates["固源岩组"][0], "1-7")

    def test_unreadable_resources_do_not_take_down_the_card(self) -> None:
        """D-2 的教训：旁路数据失败只能降级，不能让整张计划卡消失。"""
        (Path(self._resources.name) / "stages.json").write_text("{ 坏 json", encoding="utf-8")
        maa_plan_card._STAGE_DROPS_CACHE.clear()
        self.assertEqual(maa_plan_card.stage_drops(self.config), {})
        self.assertEqual(
            maa_plan_card.material_stage_candidates(self.config)["固源岩组"], ["1-7"]
        )

    # --- 自动定关 --------------------------------------------------------
    def test_degraded_still_picks_a_stage_and_says_so(self) -> None:
        """四期步骤 3 **改写了不变量 #31**：降级期照样自动定关。

        三期不敢改是因为那时排序依据只有 `farming_manual_weights` 的手工权重。
        现在候选集受活动 / 当日开放限制，而且外部掉率给了效率与稀缺两项兜底，
        选出来的关是合理的；卡片标「降级」并列出原因即可。
        """
        result = maa_plan_card.farming_recommendations(
            self.config, {}, {"1-7"}, None, data=_offline_market()
        )
        self.assertTrue(result["degraded"])
        stage, why = maa_plan_card.auto_stage(result)
        self.assertEqual(stage, "1-7")
        self.assertIn("综合", why)
        self.assertIn("降级", maa_plan_card.farming_note(result))

    def test_no_candidate_material_keeps_the_default_stage(self) -> None:
        """一个材料都算不出来时才沿用默认关，而且说清楚。"""
        config = self.config.copy()
        config["farming_stage_table"] = {}
        result = maa_plan_card.farming_recommendations(
            config, {}, {"1-7"}, None, data=_offline_market()
        )
        stage, why = maa_plan_card.auto_stage(result)
        self.assertEqual(stage, "")
        self.assertIn("沿用默认关", why)

    def test_the_weighted_score_picks_the_stage(self) -> None:
        """缺口大的材料（按理智当量算）应当赢过库存充裕的那个。"""
        at = datetime(2026, 9, 30, 8, 0, tzinfo=BEIJING)
        depot = {
            "consumption_history": [
                {
                    "from": "2026-09-01T00:00:00+08:00",
                    "to": "2026-09-29T00:00:00+08:00",
                    "days": 28,
                    "items": {
                        "a": {"name": "固源岩组", "consumed": 28},
                        "b": {"name": "碳素", "consumed": 280},
                    },
                }
            ],
            "items": {
                "a": {"name": "固源岩组", "quantity": 1000},   # 30 天用 30，够
                "b": {"name": "碳素", "quantity": 20},          # 30 天用 300，差 280
            },
        }
        self.config["farming_stage_table"] = {"固源岩组": "1-7", "碳素": "SK-5"}
        self.config["farming_manual_weights"] = {"固源岩组": 100, "碳素": 10}
        # 缺口要按**理智当量**算，所以材料名必须能对回 itemId（item_index.json）。
        self._seed_resources([], {"30013": "固源岩组", "30017": "碳素"})
        result = maa_plan_card.farming_recommendations(
            self.config, depot, {"1-7", "SK-5"}, at,
            data=_offline_market(item_values={"30013": 17.2, "30017": 3.0}),
        )
        stage, why = maa_plan_card.auto_stage(result)
        self.assertEqual(stage, "SK-5")
        self.assertIn("碳素", why)

    def test_auto_stage_writes_this_session_and_never_overwrites_a_manual_pick(self) -> None:
        result = {"degraded": False, "measured": True, "items": [
            {"material": "碳素", "item_id": "30017", "stage": "SK-5", "score": 0.8,
             "parts": {"scarcity": 0.0, "gap": 1.0, "efficiency": 1.0},
             "needed_qty": 280, "ap_per_item": 4.0, "stock": 20, "per_day": 10.0,
             "basis": "measured"},
        ]}
        at = datetime(2026, 9, 30, 7, 50, tzinfo=BEIJING)
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            with patch.object(maa_plan_card, "PLAN_PATH", plan_path), \
                 patch.object(maa_common, "PLAN_PATH", plan_path):
                # 1) 空计划：写进**这一场**，并标明是自动定的
                self.assertEqual(maa_plan_card.apply_auto_stage(result, "am", at), "SK-5")
                written = json.loads(plan_path.read_text(encoding="utf-8"))
                self.assertEqual(written["sessions"]["am"]["stage"], "SK-5")
                self.assertEqual(written["sessions"]["am"]["stage_source"], "auto")
                self.assertNotIn("pm", written["sessions"])
                # 2) 自己写的可以再改（次日重算走的就是这条）
                self.assertEqual(maa_plan_card.apply_auto_stage(result, "am", at), "SK-5")
                # 3) 用户手动选过的那一场不许动，另一场照样能写（D3）
                plan_path.write_text(
                    json.dumps({
                        "date": at.date().isoformat(),
                        "sessions": {"am": {"stage": "CE-6", "stage_source": "discord"}},
                    }),
                    encoding="utf-8",
                )
                self.assertEqual(maa_plan_card.apply_auto_stage(result, "am", at), "")
                self.assertEqual(maa_plan_card.apply_auto_stage(result, "pm", at), "SK-5")
                kept = json.loads(plan_path.read_text(encoding="utf-8"))
                self.assertEqual(kept["sessions"]["am"]["stage"], "CE-6")
                self.assertEqual(kept["sessions"]["pm"]["stage"], "SK-5")

    def test_stage_source_survives_the_action_field_allowlist(self) -> None:
        """点一次「跳过」不能把当天的自动定关冻住——`stage_source` 必须在白名单里。"""
        at = maa_common.now_beijing()
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            plan_path.write_text(
                json.dumps({
                    "date": at.date().isoformat(),
                    "sessions": {
                        "am": {"stage": "SK-5", "stage_source": "auto", "junk": 1},
                        "zz": {"stage": "不是场次"},
                    },
                }),
                encoding="utf-8",
            )
            with patch.object(maa_action, "PLAN_PATH", plan_path):
                kept = maa_action._today_override()
        self.assertEqual(kept["sessions"]["am"]["stage_source"], "auto")
        # 白名单之外的字段与非法场次一律丢掉。
        self.assertNotIn("junk", kept["sessions"]["am"])
        self.assertNotIn("zz", kept["sessions"])

    def test_choosing_a_stage_in_discord_marks_it_so_auto_leaves_it_alone(self) -> None:
        """闸门的另一半：`plan set-stage` 必须真的写下 `stage_source="discord"`。

        `apply_auto_stage()` 只认这个字段。少了这一笔，用户早上选完关，07:50 的
        自动定关会照样把它改掉——而卡片上还写着用户自己选的那个来源。
        """
        result = {"degraded": False, "measured": True, "items": [
            {"material": "碳素", "item_id": "30017", "stage": "SK-5", "score": 0.8,
             "parts": {"scarcity": 0.0, "gap": 1.0, "efficiency": 1.0},
             "needed_qty": 280, "ap_per_item": 4.0, "stock": 20, "per_day": 10.0,
             "basis": "measured"},
        ]}
        at = maa_common.now_beijing()
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            with patch.object(maa_action, "PLAN_PATH", plan_path), \
                 patch.object(maa_plan_card, "PLAN_PATH", plan_path), \
                 patch.object(maa_common, "PLAN_PATH", plan_path), \
                 patch.object(maa_common, "DEFAULTS_PATH", Path(tmp) / "defaults.json"), \
                 patch.object(maa_action, "load_config", return_value={}), \
                 patch.object(maa_action, "is_run_locked", return_value=False), \
                 patch.object(maa_action, "state_lock",
                              return_value=contextlib.nullcontext()), \
                 patch.object(maa_plan_card, "state_lock",
                              return_value=contextlib.nullcontext()):
                ok, _message = maa_action.set_stage("CE-6", "am")
                self.assertTrue(ok)
                written = json.loads(plan_path.read_text(encoding="utf-8"))
                self.assertEqual(written["sessions"]["am"]["stage_source"], "discord")
                # 自动定关看到 discord 就收手，用户选的关原样留着。
                self.assertEqual(maa_plan_card.apply_auto_stage(result, "am", at), "")
                self.assertEqual(
                    json.loads(plan_path.read_text(encoding="utf-8"))
                    ["sessions"]["am"]["stage"],
                    "CE-6",
                )
                self.assertEqual(maa_plan_card.today_stage_source("am", at), "discord")
                self.assertEqual(maa_plan_card.today_stage_source("pm", at), "")

    def test_set_stage_and_skip_reject_an_unknown_phase(self) -> None:
        """场次只接受 am / pm；非法值一律拒绝，且不写盘。"""
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            with patch.object(maa_action, "PLAN_PATH", plan_path), \
                 patch.object(maa_action, "load_config", return_value={}), \
                 patch.object(maa_action, "is_run_locked", return_value=False):
                for phase in ("foo", "now", "AM", ""):
                    with self.subTest(phase=phase):
                        ok, message = maa_action.set_stage("1-7", phase)
                        self.assertFalse(ok)
                        self.assertIn("场次只接受", message)
                        ok, message = maa_action.skip_today("on", phase)
                        self.assertFalse(ok)
                        self.assertIn("场次只接受", message)
            self.assertFalse(plan_path.exists())

    def test_dispatch_requires_the_phase_argument(self) -> None:
        for argv in (
            ["plan", "set-stage", "1-7"],
            ["plan", "skip-today", "on"],
            ["plan", "skip-today"],
        ):
            with self.subTest(argv=argv):
                ok, message = maa_action.dispatch(argv)
                self.assertFalse(ok)
                self.assertIn("不支持的动作", message)

    def test_the_card_says_who_picked_todays_stage(self) -> None:
        """删掉下拉 3 之后，「这关是自动定的还是我选的」全靠这一行。

        归属只能从 `plan.json` 的 `stage_source` 来：`effective_plan()` 只叠
        `stage` / `skip`，`normalize_plan()` 会把 `stage_source` 丢掉，
        所以不能从传进来的 plan 里查它。
        """
        farming = {"degraded": False, "items": [], "skipped_no_stage": [],
                   "skipped_closed": []}
        auto = maa_plan_card.build_embed(
            self.plan, self.config, "am", None, farming, "auto"
        )
        picked = maa_plan_card.build_embed(
            self.plan, self.config, "am", None, farming, "discord"
        )
        neither = maa_plan_card.build_embed(self.plan, self.config, "am", None, farming)

        def stage_field(embed: dict) -> str:
            return next(f["value"] for f in embed["fields"] if f["name"] == "刷什么")

        self.assertIn("按材料缺口自动选", stage_field(auto))
        self.assertIn("你手动选的", stage_field(picked))
        self.assertEqual(stage_field(neither), "1-7")

    def test_today_stage_source_ignores_yesterdays_plan(self) -> None:
        at = datetime(2026, 9, 30, 7, 50, tzinfo=BEIJING)
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            plan_path.write_text(
                json.dumps({
                    "date": "2026-09-29",
                    "sessions": {"am": {"stage": "SK-5", "stage_source": "auto"}},
                }),
                encoding="utf-8",
            )
            with patch.object(maa_common, "PLAN_PATH", plan_path):
                self.assertEqual(maa_plan_card.today_stage_source("am", at), "")

    def test_the_card_says_when_the_recommendation_is_a_guess(self) -> None:
        """不变量 #32：降级必须**可见**——而且降级期明说本场不自动改关。"""
        result = maa_plan_card.farming_recommendations(
            self.config, {}, {"1-7"}, None, data=_offline_market()
        )
        embed = maa_plan_card.build_embed(self.plan, self.config, "am", None, result)
        names = [field["name"] for field in embed["fields"]]
        self.assertIn("为什么（降级）", names)
        value = next(
            field["value"] for field in embed["fields"] if field["name"].startswith("为什么")
        )
        self.assertIn("降级", value)
        # 降级期也会自动定关（改写后的不变量 #31），但原因必须写出来。
        self.assertIn("本场自动定为", value)
        self.assertIn("consumption_history", value)

    def test_the_seeded_table_only_names_stages_the_card_can_offer(self) -> None:
        # 种子里不许出现猜的关：每一个都必须是 1-7 或 SUPPLY_STAGES 里的资源本。
        seed = deploy.load_json(deploy.ROOT / "state" / "config.json", {})
        known = {"1-7"} | {stage for _l, stage, _d, _w in maa_plan_card.SUPPLY_STAGES}
        for material, stage in seed["farming_stage_table"].items():
            self.assertIn(stage, known, f"{material} 配了一个来路不明的关卡 {stage}")
        # 权重表覆盖关卡表里的每一个材料，否则降级模式下它排序全靠 0。
        self.assertEqual(
            set(seed["farming_stage_table"]) - set(seed["farming_manual_weights"]),
            set(),
        )

    def test_farming_config_keys_are_repo_owned_not_runtime_caches(self) -> None:
        # 它们是配置，仓库说了算；混进 PRESERVED_RUNTIME_CACHE_KEYS 会让用户
        # 改了仓库种子却永远不生效（不变量 #25 的反面）。
        for key in ("farming_stage_table", "farming_manual_weights",
                    "clearing_manual_after_days", "clearing_enabled"):
            self.assertNotIn(key, deploy.PRESERVED_RUNTIME_CACHE_KEYS)



if __name__ == "__main__":
    unittest.main()
