"""肉鸽（`maa_roguelike` / `maa_roguelike_worker`）。

从原 `tests/test_maa.py` 按领域拆出；共用桩见 `_helpers.py`。
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
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
import maa_plan_card  # noqa: E402
import maa_playcover  # noqa: E402
import maa_roguelike  # noqa: E402
import deploy  # noqa: E402
from _helpers import BEIJING  # noqa: E402


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


class RoguelikeDictionaryTests(unittest.TestCase):
    """spike C 第 2.2 / 2.3 / 7 节抄进来的字典必须原样成立。"""

    def test_every_theme_fits_in_a_discord_select(self) -> None:
        # Discord 每个 select ≤ 25 项；难度上限直接抄 GUI 的 GetMaxDifficultyForTheme。
        expected = {"Phantom": 15, "Mizuki": 18, "Sami": 15,
                    "Sarkaz": 18, "JieGarden": 18, "BlackFlow": 15}
        self.assertEqual(set(maa_roguelike.THEME_ORDER), set(expected))
        for theme, ceiling in expected.items():
            with self.subTest(theme=theme):
                self.assertEqual(maa_roguelike.max_difficulty(theme), ceiling)
                difficulties = maa_roguelike.difficulty_values(theme)
                self.assertLessEqual(len(difficulties), 25)
                self.assertEqual(difficulties[0], maa_roguelike.NO_DIFFICULTY)
                self.assertEqual(difficulties[1], maa_roguelike.MAX_DIFFICULTY)
                self.assertEqual(max(difficulties[2:]), ceiling)
                squads = maa_roguelike.squad_values(
                    theme, maa_roguelike.default_mode_token(theme)
                )
                self.assertLessEqual(len(squads), 25)
                self.assertLessEqual(len(maa_roguelike.mode_table(theme)), 25)

    def test_the_common_squads_are_appended_and_only_blackflow_skips_premium(self) -> None:
        for theme in maa_roguelike.THEME_ORDER:
            squads = maa_roguelike.squad_values(theme)
            for common in maa_roguelike.COMMON_SQUADS:
                self.assertIn(common, squads)
            self.assertEqual(
                maa_roguelike.PREMIUM_SQUAD in squads, theme != "BlackFlow"
            )

    def test_sarkaz_ingot_mode_narrows_the_squad_list(self) -> None:
        ingot = maa_roguelike.squad_values("Sarkaz", "1")
        every = maa_roguelike.squad_values("Sarkaz", "0")
        self.assertIn("点刺成锭分队", ingot)
        self.assertNotIn("魂灵护送分队", ingot)
        self.assertIn("魂灵护送分队", every)

    def test_the_defaults_follow_the_manual(self) -> None:
        # 手册推荐：萨卡兹 4 / 界园 3 / 水月 3 / 萨米 4 / 傀影 2 / 黑流树海不指定。
        wanted = {"Sarkaz": 4, "JieGarden": 3, "Mizuki": 3, "Sami": 4,
                  "Phantom": 2, "BlackFlow": maa_roguelike.NO_DIFFICULTY}
        for theme, difficulty in wanted.items():
            self.assertEqual(
                maa_roguelike.default_pending(theme)["difficulty"], difficulty, theme
            )
        # 萨卡兹刷锭 → 点刺成锭分队（触发刷新商店加速）；其余 → 指挥分队。
        self.assertEqual(maa_roguelike.default_pending("Sarkaz")["squad"], "点刺成锭分队")
        self.assertEqual(maa_roguelike.default_pending("Mizuki")["squad"], "指挥分队")
        # 职业组不占 UI：傀影取长补短，其余稳扎稳打。
        self.assertEqual(maa_roguelike.resolve_params(None)["roles"], "稳扎稳打")
        self.assertEqual(
            maa_roguelike.resolve_params({"theme": "Phantom"})["roles"], "取长补短"
        )

    def test_only_sami_offers_the_collapsal_paradigm_mode(self) -> None:
        self.assertTrue(maa_roguelike.valid_mode_token("Sami", "5"))
        self.assertFalse(maa_roguelike.valid_mode_token("Sarkaz", "5"))

    def test_a_broken_pending_falls_back_instead_of_raising(self) -> None:
        pending = maa_roguelike.normalize_pending(
            {"theme": "Nope", "mode": "999", "difficulty": "x", "squad": "不存在分队"}
        )
        self.assertEqual(pending, maa_roguelike.default_pending())


class RoguelikeTaskFileTests(unittest.TestCase):
    """执行路线是 `maa run` 任务文件，不是 `maa roguelike` 子命令（spike C 第 6.7 节）。"""

    @_isolation.needs_tomllib
    def test_the_rendered_params_carry_the_stop_primitives_and_no_support(self) -> None:
        import tomllib

        toml = maa_roguelike.render_task_toml(maa_roguelike.resolve_params(None))
        parsed = tomllib.loads(toml)
        task = parsed["tasks"][1]
        self.assertEqual(task["type"], "Roguelike")
        params = task["params"]
        # 每局一个子进程：`starts_count = 1` 是「本局结束后再停」的全部实现。
        self.assertEqual(params["starts_count"], 1)
        self.assertTrue(params["investment_enabled"])
        self.assertTrue(params["stop_when_investment_full"])
        self.assertEqual(params["core_char"], "")
        # 助战要先配好开局干员名才有意义，永远不传（也别写进字面量）。
        self.assertNotIn("use_support", params)
        self.assertNotIn("use_support", toml)
        self.assertNotIn("use_nonfriend_support", toml)

    @_isolation.needs_tomllib
    def test_every_round_starts_the_game_itself(self) -> None:
        """2026-09-11 事故：任务文件没有 StartUp，模拟器起来了但游戏没起来。

        开机链（`launch_game / wait_for_game / verify_game_installed /
        settle_game_window`）只管模拟器，拉起游戏的只能是 MaaCore 的 `StartUp`。
        """
        import tomllib

        import maa_copilot_worker

        toml = maa_roguelike.render_task_toml(maa_roguelike.resolve_params(None))
        tasks = tomllib.loads(toml)["tasks"]
        self.assertEqual([task["type"] for task in tasks], ["StartUp", "Roguelike"])
        startup = tasks[0]["params"]
        self.assertEqual(startup["client_type"], "Official")
        self.assertTrue(startup["start_game_enabled"])
        # 和日常 / 抄作业同一段（别让三处漂移）。
        for line in ('type = "StartUp"', 'client_type = "Official"',
                     "start_game_enabled = true"):
            self.assertIn(line, maa_copilot_worker.NAV_TOML_TEMPLATE)
            self.assertIn(line, toml)

    def test_blackflow_modes_become_a_strategy_not_a_bare_mode(self) -> None:
        wanted = {"burn": ("burn", 0), "investment": ("investment", 1),
                  "baby_animal": ("baby_animal", 30001)}
        for token, (strategy, mode) in wanted.items():
            params = maa_roguelike.resolve_params(
                {"theme": "BlackFlow", "mode": token, "difficulty": -1,
                 "squad": "特勤分队"}
            )
            self.assertEqual(params["blackflow_strategy"], strategy)
            self.assertEqual(params["mode"], mode)
        # 只有「刷襁褓动物」才带培养目标。
        baby = maa_roguelike.resolve_params(
            {"theme": "BlackFlow", "mode": "baby_animal"}
        )
        self.assertEqual(baby["blackflow_cultivation_target"], "swaddled_cat")
        self.assertNotIn(
            "blackflow_cultivation_target",
            maa_roguelike.resolve_params({"theme": "BlackFlow", "mode": "burn"}),
        )
        # 其它主题一个 blackflow_* 字段都不许出现。
        self.assertNotIn(
            "blackflow_strategy", maa_roguelike.resolve_params({"theme": "Sarkaz"})
        )

    def test_phantom_keeps_its_difficulty(self) -> None:
        # maa-cli 预设会把 Phantom 的 --difficulty 丢掉并 warn；任务文件路线不会。
        params = maa_roguelike.resolve_params({"theme": "Phantom", "difficulty": 15})
        self.assertEqual(params["difficulty"], 15)


class RoguelikeOutputTests(unittest.TestCase):
    """spike C 第 4.6 节的每个标记各一个用例。"""

    def test_a_started_exploration_is_recognized(self) -> None:
        parsed = maa_roguelike.parse_run_output("Start exploration 1 times", 0)
        self.assertTrue(parsed["started"])

    def test_the_settlement_line_carries_the_exp(self) -> None:
        parsed = maa_roguelike.parse_run_output(
            "Gain 340 exp during this exploration", 0
        )
        self.assertEqual(parsed["exp"], 340)
        self.assertEqual(parsed["outcome"], "settled")

    def test_an_abandoned_exploration_is_not_a_settlement(self) -> None:
        parsed = maa_roguelike.parse_run_output("Exploration Abandoned", 0)
        self.assertTrue(parsed["abandoned"])
        self.assertIsNone(parsed["exp"])
        self.assertEqual(parsed["outcome"], "abandoned")

    def test_the_deposit_line_gives_this_run_and_the_reserve(self) -> None:
        parsed = maa_roguelike.parse_run_output(
            "Deposit 3 / 21 / 999 originium ingots", 0
        )
        self.assertEqual(parsed["invest"], {"count": 3, "total": 21, "deposit": 999})

    def test_a_full_reserve_is_visible(self) -> None:
        self.assertTrue(
            maa_roguelike.parse_run_output("TraderInvestSystemFull", 0)["invest_full"]
        )

    def test_only_completed_plus_exit_zero_counts_as_clean(self) -> None:
        self.assertTrue(
            maa_roguelike.parse_run_output("Roguelike Completed", 0)["clean"]
        )
        # 退出码不是 0 → 不算干净（不然被中断的一局会被当成收尾过的）。
        self.assertFalse(
            maa_roguelike.parse_run_output("Roguelike Completed", 1)["clean"]
        )
        # 有退出码 0 但没有标记也不算。
        self.assertFalse(
            maa_roguelike.parse_run_output("Start exploration 1 times", 0)["clean"]
        )

    def test_stopped_and_error_are_told_apart(self) -> None:
        stopped = maa_roguelike.parse_run_output("Roguelike Stopped", 1)
        self.assertTrue(stopped["interrupted"])
        self.assertEqual(stopped["outcome"], "stopped")
        error = maa_roguelike.parse_run_output("Roguelike Error", 1)
        self.assertTrue(error["error"])
        self.assertEqual(error["outcome"], "error")

    def test_the_startup_lines_do_not_disturb_any_verdict(self) -> None:
        """任务链多了一段 `StartUp`，原有判据一个都不许被带偏。"""
        output = (
            "StartUp Start\n"
            "StartUp Completed\n"
            "Roguelike Start\n"
            "Start exploration 1 times\n"
            "Deposit 3 / 21 / 300 originium ingots\n"
            "Gain 340 exp during this exploration\n"
            "Roguelike Completed\n"
        )
        parsed = maa_roguelike.parse_run_output(output, 0)
        self.assertEqual(parsed["outcome"], "settled")
        self.assertEqual(parsed["exp"], 340)
        self.assertTrue(parsed["clean"])
        self.assertFalse(parsed["startup_failed"])

    def test_a_failed_startup_is_an_error_not_an_unknown(self) -> None:
        """2026-09-11 事故的放大器：游戏没起来时必须能触发连续报错收尾闸门。"""
        parsed = maa_roguelike.parse_run_output("StartUp Start\nStartUp Error\n", 1)
        self.assertTrue(parsed["startup_failed"])
        self.assertEqual(parsed["outcome"], "error")
        self.assertIn("游戏没起来", parsed["detail"])
        # 结算过的一局不会被开头那段盖掉。
        settled = maa_roguelike.parse_run_output(
            "StartUp Error\nGain 340 exp during this exploration\n", 1
        )
        self.assertEqual(settled["outcome"], "settled")
        self.assertTrue(settled["startup_failed"])

    def test_the_summary_block_survives_the_startup_section(self) -> None:
        """maa-cli 的结尾 summary 现在多一段 `[启动国服]`，解析不能被它带偏。"""
        output = (
            "Summary\n"
            "----------------------------------------\n"
            "[启动国服] 18:30:43 - 18:31:25 (42s) Completed\n"
            "----------------------------------------\n"
            "[肉鸽] 18:31:25 - 18:45:02 (13m 37s) Completed\n"
            "Explorations:\n"
            "1. Passed, invest 18 originium ingotes, gained 200 exp;\n"
            "Summary:\n"
            "Passed 1, Failed 0\n"
            "Total invest 18 originium ingotes\n"
            "Total gained 200 exp\n"
        )
        summary = maa_roguelike.parse_summary(output)
        self.assertEqual(len(summary["explorations"]), 1)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["total_invest"], 18)
        self.assertEqual(summary["total_exp"], 200)
        rendered = "\n".join(
            field["value"] for field in maa_roguelike.summary_fields(summary)
        )
        self.assertIn("第 1 局 · 通关 · 投资 18 · 经验 200", rendered)
        self.assertNotIn("启动国服", rendered)

    def test_the_summary_block_becomes_chinese_embed_fields(self) -> None:
        output = (
            "Explorations:\n"
            "1. Failed, invest 10 originium ingotes, gained 100 exp;\n"
            "2. Passed, invest 18 originium ingotes, gained 200 exp;\n"
            "3. Unknown;\n"
            "Summary:\n"
            "Passed 1, Failed 1, Unknown 1\n"
            "Total invest 28 originium ingotes\n"
            "Total gained 300 exp\n"
        )
        summary = maa_roguelike.parse_summary(output)
        self.assertEqual(len(summary["explorations"]), 3)
        self.assertEqual(summary["total_invest"], 28)
        self.assertEqual(summary["total_exp"], 300)
        fields = maa_roguelike.summary_fields(summary)
        rendered = "\n".join(field["value"] for field in fields)
        self.assertIn("第 2 局 · 通关 · 投资 18 · 经验 200", rendered)
        # 不变量 #9：CLI 原文不进 Discord。
        self.assertNotIn("originium", rendered)
        self.assertNotIn("Passed", rendered)

    def test_a_run_without_a_summary_block_adds_no_fields(self) -> None:
        self.assertEqual(
            maa_roguelike.summary_fields(maa_roguelike.parse_summary("Roguelike Start")),
            [],
        )


class RoguelikeCardTests(unittest.TestCase):
    """卡片：4 个下拉 + 1 行按钮 = 正好 5 行；值不透明（不变量 #8 / #50）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.state_path = self.root / "roguelike.json"
        self.buttons = self.root / "buttons.json"

    def test_the_card_has_exactly_four_selects_and_one_button_row(self) -> None:
        _embed, rows, registry = maa_roguelike.build_card(
            maa_roguelike.load_state(self.state_path)
        )
        self.assertEqual(len(rows), 5)
        self.assertEqual([row["components"][0]["type"] for row in rows[:4]], [3] * 4)
        labels = [button["label"] for button in rows[4]["components"]]
        self.assertEqual(
            labels, ["▶️ 开始", "⏹️ 本局结束后停止", "⏏️ 立即中止（会丢当前局）"]
        )
        self.assertEqual(len(registry), 7)

    def test_the_select_values_are_opaque_and_resolved_through_the_registry(self) -> None:
        _embed, rows, registry = maa_roguelike.build_card(
            maa_roguelike.load_state(self.state_path)
        )
        theme_id = f"{maa_roguelike.BUTTON_PREFIX}theme-select"
        options = rows[0]["components"][0]["options"]
        self.assertEqual([option["value"] for option in options][:2],
                         ["choice:0", "choice:1"])
        for option in options:
            self.assertNotIn("Sarkaz", option["value"])
        args = registry[theme_id]["select_args"]
        self.assertEqual(args["choice:0"], ["roguelike", "set", "theme", "Phantom"])
        self.assertEqual(
            registry[f"{maa_roguelike.BUTTON_PREFIX}start"]["args"],
            ["roguelike", "start"],
        )
        for entry in registry.values():
            self.assertEqual(entry["script"], "maa_action.py")

    def test_the_card_says_to_pin_the_theme_in_game(self) -> None:
        embed, _rows, _registry = maa_roguelike.build_card(
            maa_roguelike.load_state(self.state_path)
        )
        rendered = "\n".join(field["value"] for field in embed["fields"])
        self.assertIn("置顶", rendered)
        self.assertIn("不消耗理智", embed["description"])

    def test_the_roguelike_prefix_survives_both_sweeps(self) -> None:
        """既不进 07:50 的 `hermes:btn:maa:` 清扫，也不被抄作业的 TTL 清扫误删。"""
        key = f"{maa_roguelike.BUTTON_PREFIX}start"
        self.assertFalse(key.startswith(maa_plan_card.BUTTON_PREFIX))
        self.assertFalse(key.startswith(maa_copilot.BUTTON_PREFIX))
        maa_common.atomic_write_json(
            self.buttons, {"hermes:btn:maa:am:stage-select": {}, key: {}}
        )
        with patch.object(maa_common, "BUTTONS_PATH", self.buttons):
            maa_common.prune_button_registry(maa_plan_card.BUTTON_PREFIX)
        registry = json.loads(self.buttons.read_text(encoding="utf-8"))
        self.assertNotIn("hermes:btn:maa:am:stage-select", registry)
        self.assertIn(key, registry)
        searches = self.root / "searches"
        searches.mkdir()
        (searches / f"{'d' * 12}.json").write_text(
            json.dumps({"expires_at": "2020-01-01T00:00:00+08:00"}), encoding="utf-8"
        )
        with patch.object(maa_copilot, "SEARCHES_DIR", searches), patch.object(
            maa_copilot, "ensure_private_dirs"
        ):
            maa_copilot.prune_expired_searches(registry)
        self.assertIn(key, registry)

    def _act(self, argv, *, state=None, config=None, post_id="999"):
        posted: list[dict] = []
        # `save_state()` 每次都会写 `updated_at`，桩数据也得有——否则会被
        # `round_is_stale()` 当成崩溃残留。
        maa_common.atomic_write_json(
            self.state_path,
            {**maa_roguelike.EMPTY_STATE,
             "updated_at": maa_common.now_beijing().isoformat(),
             **(state or {})},
        )
        maa_common.atomic_write_json(self.buttons, {"hermes:btn:agent-book:x": {}})
        with patch.object(maa_roguelike, "STATE_PATH", self.state_path), patch.object(
            maa_roguelike, "state_lock", return_value=contextlib.nullcontext()
        ), patch.object(
            maa_action, "load_config", return_value=config or {"channel_id": "123"}
        ), patch.object(maa_action, "BUTTONS_PATH", self.buttons), patch.object(
            maa_action, "state_lock", return_value=contextlib.nullcontext()
        ), patch.object(
            maa_action, "retire_discord_components"
        ) as retire, patch.object(
            maa_action, "discord_token", return_value="token"
        ), patch.object(
            maa_action, "discord_request",
            side_effect=lambda *a, **k: posted.append(a[3]) or {"id": post_id},
        ), patch.object(maa_action, "dispatch_roguelike") as dispatch:
            ok, message = maa_action.dispatch(argv)
        return SimpleNamespace(
            ok=ok, message=message, posted=posted, retire=retire, dispatch=dispatch,
            state=json.loads(self.state_path.read_text(encoding="utf-8")),
            registry=json.loads(self.buttons.read_text(encoding="utf-8")),
        )

    def test_choosing_a_value_only_writes_pending(self) -> None:
        result = self._act(["roguelike", "set", "difficulty", "9"])
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.state["pending"]["difficulty"], 9)
        self.assertEqual(result.state["status"], "idle")
        # 「开始」才派发。
        result.dispatch.assert_not_called()
        self.assertEqual(result.posted, [])

    def test_an_illegal_value_is_refused_before_it_reaches_the_task_file(self) -> None:
        for argv, mark in (
            (["roguelike", "set", "theme", "Nope"], "未知的肉鸽主题"),
            (["roguelike", "set", "difficulty", "19"], "难度上限"),
            (["roguelike", "set", "squad", "不存在分队"], "没有"),
            (["roguelike", "set", "mode", "5"], "没有这个模式"),
            (["roguelike", "set", "core_char", "维什戴尔"], "四项"),
        ):
            with self.subTest(argv=argv):
                result = self._act(argv)
                self.assertFalse(result.ok)
                self.assertIn(mark, result.message)

    def test_changing_the_theme_reposts_the_card_and_drops_the_old_registry(self) -> None:
        stale = f"{maa_roguelike.BUTTON_PREFIX}squad-select"
        maa_common.atomic_write_json(
            self.buttons,
            {stale: {"select_args": {"choice:0": ["roguelike", "set", "squad", "旧的"]}},
             "hermes:btn:agent-book:x": {}},
        )
        result = self._act(
            ["roguelike", "set", "theme", "JieGarden"],
            state={"pending": {"theme": "Sarkaz", "mode": "1",
                               "difficulty": 4, "squad": "点刺成锭分队"},
                   "message_id": "555"},
        )
        self.assertTrue(result.ok, result.message)
        # 旧卡的组件先被 PATCH 空，登记也换成了新主题的（不变量 #14b 的顺序）。
        self.assertEqual(result.retire.call_args.args[1], "555")
        squads = result.registry[stale]["select_args"].values()
        self.assertIn(["roguelike", "set", "squad", "司岁台分队"], list(squads))
        self.assertNotIn(["roguelike", "set", "squad", "旧的"], list(squads))
        self.assertIn("hermes:btn:agent-book:x", result.registry)
        self.assertEqual(result.state["message_id"], "999")
        self.assertEqual(result.state["pending"]["theme"], "JieGarden")
        self.assertEqual(len(result.posted), 1)
        self.assertEqual(len(result.posted[0]["components"]), 5)

    def test_a_dirty_run_refuses_to_switch_theme(self) -> None:
        result = self._act(
            ["roguelike", "start"],
            state={"dirty": True, "last_theme": "Sami",
                   "pending": {"theme": "Sarkaz"}},
        )
        self.assertFalse(result.ok)
        self.assertIn("萨米", result.message)
        self.assertIn("先用", result.message)
        result.dispatch.assert_not_called()
        self.assertEqual(result.state["status"], "idle")

    def test_a_dirty_run_may_restart_on_the_same_theme(self) -> None:
        with patch.object(maa_action, "dispatch_blocker", return_value=""):
            result = self._act(
                ["roguelike", "start"],
                state={"dirty": True, "last_theme": "Sami",
                       "pending": {"theme": "Sami"}},
            )
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.state["status"], "running")
        self.assertEqual(result.state["params"]["theme"], "Sami")
        result.dispatch.assert_called_once()

    def test_starting_inside_the_blackout_is_refused_and_never_scheduled(self) -> None:
        with patch.object(
            maa_action, "dispatch_blocker",
            return_value="距 22:00 日常场次不足 25 分钟",
        ):
            result = self._act(["roguelike", "start"])
        self.assertFalse(result.ok)
        self.assertIn("25 分钟", result.message)
        result.dispatch.assert_not_called()
        self.assertEqual(result.state["status"], "idle")
        self.assertEqual(result.state["token"], "")

    def test_the_start_gate_is_the_shared_one(self) -> None:
        source = (ROOT / "scripts" / "maa_action.py").read_text(encoding="utf-8")
        body = source.split("def roguelike_start(")[1].split("\ndef ")[0]
        self.assertIn("dispatch_blocker(config", body)

    def test_stop_only_writes_a_flag_and_abort_writes_both(self) -> None:
        live = {"status": "running", "token": "b" * 12}
        stop = self._act(["roguelike", "stop"], state=live)
        self.assertTrue(stop.ok)
        self.assertTrue(stop.state["stop_requested_at"])
        self.assertIsNone(stop.state["abort_requested_at"])
        self.assertEqual(stop.state["status"], "stopping")
        self.assertIn("这一局会打完再退出", stop.message)
        abort = self._act(["roguelike", "abort"], state=live)
        self.assertTrue(abort.ok)
        self.assertTrue(abort.state["abort_requested_at"])
        self.assertTrue(abort.state["stop_requested_at"])
        self.assertIn("作废", abort.message)

    def test_stopping_an_idle_roguelike_says_so(self) -> None:
        for argv in (["roguelike", "stop"], ["roguelike", "abort"]):
            result = self._act(argv)
            self.assertFalse(result.ok)
            self.assertIn("没有在跑", result.message)

    def test_status_is_read_only(self) -> None:
        result = self._act(
            ["roguelike", "status"],
            state={"status": "running", "token": "b" * 12,
                   "params": maa_roguelike.resolve_params(None),
                   "runs": [{"n": 1, "outcome": "settled", "detail": "已结算 · 经验 340"}]},
        )
        self.assertTrue(result.ok)
        self.assertIn("已完成 1 局", result.message)
        self.assertIn("已结算", result.message)
        self.assertEqual(result.posted, [])
        result.dispatch.assert_not_called()

    def test_parameters_cannot_be_changed_while_it_runs(self) -> None:
        result = self._act(
            ["roguelike", "set", "difficulty", "9"],
            state={"status": "running", "token": "b" * 12},
        )
        self.assertFalse(result.ok)
        self.assertIn("正在跑", result.message)

    def test_the_dispatcher_rejects_anything_else(self) -> None:
        for argv in (["roguelike"], ["roguelike", "run"], ["roguelike", "set", "theme"]):
            ok, message = maa_action.dispatch(argv)
            self.assertFalse(ok)
            self.assertIn("不支持的动作", message)


class RoguelikeWorkerTests(unittest.TestCase):
    """worker：一局一个子进程、停止只在局间生效、abort 才 terminate。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.state_path = self.root / "roguelike.json"
        self.token = "b" * 12

    def _seed(self, **extra) -> dict:
        params = maa_roguelike.resolve_params(extra.pop("pending", None))
        state = {
            **maa_roguelike.EMPTY_STATE,
            "status": "running",
            "token": self.token,
            "theme": params["theme"],
            "params": params,
            "runs": [],
        }
        state.update(extra)
        maa_common.atomic_write_json(self.state_path, state)
        return state

    def _run(self, runs, *, online=(False, ""), blackout=None, depot=None, **seed):
        """跑一轮；`runs` 是每局的 `(退出码, 输出, 提前收手)` 或一个可调用的桩。"""
        import maa_roguelike_worker as worker

        self._seed(**seed)
        calls: list[int] = []
        notices: list[tuple[str, str]] = []
        reports: list[dict] = []
        launch_game = unittest.mock.MagicMock()

        def fake_run(config, env, log_path, index):
            calls.append(index)
            item = runs[min(index - 1, len(runs) - 1)]
            return item(self.state_path) if callable(item) else item

        def gate(value) -> dict[str, Any]:
            """闸门桩：给个 callable 就当 `side_effect`（同一轮里前后不同答案）。"""
            return ({"side_effect": value} if callable(value)
                    else {"return_value": value})

        patches = [
            patch.object(maa_roguelike, "STATE_PATH", self.state_path),
            patch.object(maa_roguelike, "state_lock",
                         return_value=contextlib.nullcontext()),
            # 线上 `run.lock` 必须打桩：不打的话这一组用例会去抢
            # `~/.hermes/state/maa/run.lock`，于是 ① Depot（04:20–05:00）或任何
            # 正在跑的真作业一占锁，整组就红；② 反过来测试自己拿到锁的那几秒，
            # 真的定时任务会被挡在门外。单测不许依赖墙钟、更不许碰线上状态。
            patch.object(worker, "run_lock",
                         return_value=contextlib.nullcontext(True)),
            patch.object(worker, "RUNS_DIR", self.root),
            patch.object(worker, "load_config",
                         return_value={"channel_id": "1",
                                       "roguelike_run_timeout": 3600}),
            patch.object(maa_playcover, "game_is_online", return_value=online),
            patch.object(worker, "require_executable"),
            patch.object(maa_playcover, "launch_game", launch_game),
            patch.object(maa_playcover, "wait_for_game"),
            patch.object(maa_playcover, "verify_game_installed"),
            patch.object(maa_playcover, "settle_game_window"),
            patch.object(worker, "maa_subprocess_env", return_value={}),
            patch.object(maa_playcover, "capture_screenshot", return_value=(True, "")),
            patch.object(maa_playcover, "close_game", return_value=[]),
            patch.object(worker, "merge_last_run"),
            patch.object(worker, "cleanup_run_files"),
            patch.object(worker, "_isolated_config",
                         side_effect=lambda env, files: contextlib.nullcontext(env)),
            patch.object(worker, "_run_one_exploration", side_effect=fake_run),
            # 两条时间闸门也只认桩，不认真实时钟（`_next_round_blocker` 会读它们）。
            patch.object(maa_action, "_copilot_blackout_reason", **gate(blackout)),
            patch.object(maa_action, "depot_window_reason", **gate(depot)),
            patch.object(
                worker, "_notice",
                side_effect=lambda c, title, body, color: notices.append((title, body)),
            ),
            patch.object(
                worker, "_post_report",
                side_effect=lambda config, embed, png: reports.append(embed),
            ),
        ]
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            with contextlib.redirect_stdout(io.StringIO()):
                code = worker.cmd_run(self.token)
        return SimpleNamespace(
            code=code, calls=calls, notices=notices,
            report=reports[0] if reports else None,
            launch_game=launch_game,
            state=json.loads(self.state_path.read_text(encoding="utf-8")),
        )

    CLEAN = (0, "Start exploration 1 times\nGain 340 exp during this exploration\n"
                "Roguelike Completed\n", "")

    def _field(self, report, name):
        return next(
            (field["value"] for field in report["fields"] if field["name"] == name), ""
        )

    def test_stop_between_runs_lets_the_current_one_finish(self) -> None:
        def first(path):
            state = json.loads(path.read_text(encoding="utf-8"))
            state["stop_requested_at"] = "2026-09-09T12:00:00+08:00"
            state["status"] = "stopping"
            maa_common.atomic_write_json(path, state)
            return self.CLEAN

        result = self._run([first, self.CLEAN])
        # 第 2 局根本没开：停止只在局间生效。
        self.assertEqual(result.calls, [1])
        self.assertEqual(len(result.state["runs"]), 1)
        self.assertEqual(result.state["status"], "idle")
        self.assertFalse(result.state["dirty"])
        self.assertIsNone(result.state["stop_requested_at"])
        self.assertIn("本局结束后停止", self._field(result.report, "停止原因"))

    def test_a_clean_run_clears_dirty_and_a_dirty_one_keeps_it(self) -> None:
        stop_after_one = [
            lambda path: (
                maa_common.atomic_write_json(
                    path,
                    {**json.loads(path.read_text(encoding="utf-8")),
                     "stop_requested_at": "2026-09-09T12:00:00+08:00"},
                ),
                self.CLEAN,
            )[1]
        ]
        ok = self._run(stop_after_one)
        self.assertFalse(ok.state["dirty"])
        self.assertEqual(ok.state["last_theme"], "Sarkaz")
        # 退出码 0 但没有 `Roguelike Completed` → 不算干净，留下 dirty。
        messy = [
            lambda path: (
                maa_common.atomic_write_json(
                    path,
                    {**json.loads(path.read_text(encoding="utf-8")),
                     "stop_requested_at": "2026-09-09T12:00:00+08:00"},
                ),
                (0, "Start exploration 1 times\n", ""),
            )[1]
        ]
        bad = self._run(messy)
        self.assertTrue(bad.state["dirty"])
        self.assertIn("有残局", [field["name"] for field in bad.report["fields"]][-1])

    def test_an_abort_ends_the_round_and_marks_it_dirty(self) -> None:
        result = self._run([(1, "Roguelike Stopped\n", "abort")])
        self.assertEqual(result.calls, [1])
        self.assertTrue(result.state["dirty"])
        self.assertEqual(result.state["last_theme"], "Sarkaz")
        self.assertEqual(result.state["runs"][0]["outcome"], "stopped")
        self.assertIn("作废", self._field(result.report, "停止原因"))

    def test_a_full_reserve_ends_the_round(self) -> None:
        full = (0, "Deposit 3 / 21 / 999 originium ingots\nTraderInvestSystemFull\n"
                   "Exploration Abandoned\nRoguelike Completed\n", "")
        result = self._run([full, self.CLEAN])
        self.assertEqual(result.calls, [1])
        self.assertIn("储备已达上限", self._field(result.report, "停止原因"))

    def test_two_upstream_errors_in_a_row_end_the_round(self) -> None:
        error = (1, "Roguelike Error\n", "")
        result = self._run([error, error, self.CLEAN])
        self.assertEqual(result.calls, [1, 2])
        self.assertIn("连续 2 局报错", self._field(result.report, "停止原因"))

    def test_a_single_error_does_not_end_the_round(self) -> None:
        error = (1, "Roguelike Error\n", "")
        def stop_third(path):
            maa_common.atomic_write_json(
                path,
                {**json.loads(path.read_text(encoding="utf-8")),
                 "stop_requested_at": "2026-09-09T12:00:00+08:00"},
            )
            return self.CLEAN

        result = self._run([error, self.CLEAN, stop_third])
        self.assertEqual(result.calls, [1, 2, 3])

    def test_the_blackout_stops_it_before_the_emulator_is_opened(self) -> None:
        # 首局就被挡：一次都别开机（开机加清理好几分钟，全程占着 run.lock），
        # 但必须留下痕迹——一张灰卡加一条战报（护栏 4）。
        result = self._run(
            [self.CLEAN], blackout="距 22:00 日常场次不足 25 分钟"
        )
        self.assertEqual(result.calls, [])
        result.launch_game.assert_not_called()
        self.assertIn("25 分钟", self._field(result.report, "停止原因"))
        self.assertTrue(any("未启动" in title for title, _body in result.notices))
        self.assertEqual(result.state["status"], "idle")

    def test_the_depot_window_stops_it_before_the_emulator_is_opened(self) -> None:
        result = self._run([self.CLEAN], depot=lambda: "处于 Depot 对表窗口")
        self.assertEqual(result.calls, [])
        result.launch_game.assert_not_called()
        self.assertIn("Depot", self._field(result.report, "停止原因"))

    def test_a_gate_that_closes_mid_round_stops_the_next_run(self) -> None:
        # 第 N+1 局被挡：已经开的机照跑完这一局，下一局不开，也不再多发灰卡。
        opened: list[int] = []

        def blackout(_config):
            return "距 22:00 日常场次不足 25 分钟" if opened else None

        result = self._run(
            [lambda _path: (opened.append(1), self.CLEAN)[1], self.CLEAN],
            blackout=blackout,
        )
        self.assertEqual(result.calls, [1])
        result.launch_game.assert_called_once()
        self.assertIn("25 分钟", self._field(result.report, "停止原因"))
        self.assertFalse([t for t, _b in result.notices if "未启动" in t])
        self.assertEqual(result.state["status"], "idle")

    def test_a_busy_emulator_touches_nothing(self) -> None:
        result = self._run([self.CLEAN], online=(True, "游戏进程在跑"))
        self.assertEqual(result.calls, [])
        result.launch_game.assert_not_called()
        self.assertIsNone(result.report)
        self.assertEqual(result.state["status"], "idle")
        self.assertTrue(any("未启动" in title for title, _body in result.notices))

    def test_a_stale_token_is_refused(self) -> None:
        import maa_roguelike_worker as worker

        self._seed()
        with patch.object(maa_roguelike, "STATE_PATH", self.state_path), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(worker.cmd_run("c" * 12), 2)

    def test_the_lock_conflict_puts_the_round_back_to_idle(self) -> None:
        import maa_roguelike_worker as worker

        self._seed()
        notices: list[str] = []
        with patch.object(maa_roguelike, "STATE_PATH", self.state_path), patch.object(
            maa_roguelike, "state_lock", return_value=contextlib.nullcontext()
        ), patch.object(
            worker, "run_lock", return_value=contextlib.nullcontext(False)
        ), patch.object(worker, "load_config", return_value={"channel_id": "1"}
        ), patch.object(
            worker, "_notice",
            side_effect=lambda config, title, body, color: notices.append(title),
        ), patch.object(worker, "_run_locked") as locked:
            with contextlib.redirect_stdout(io.StringIO()):
                code = worker.cmd_run(self.token)
        locked.assert_not_called()
        self.assertEqual(code, 1)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "idle")
        self.assertTrue(any("未启动" in title for title in notices))


class RoguelikeSubprocessTests(unittest.TestCase):
    """子进程必须是 `Popen` + `terminate()`；`subprocess.run(timeout=)` 是禁止写法。"""

    class FakeProc:
        def __init__(self) -> None:
            self.terminated = 0
            self.killed = 0
            self.returncode = 1
            self.stubborn = False

        def poll(self):
            return None if not self.terminated else self.returncode

        def terminate(self) -> None:
            self.terminated += 1

        def kill(self) -> None:
            self.killed += 1

        def wait(self, timeout=None):
            if self.stubborn and not self.killed:
                raise subprocess.TimeoutExpired("maa", timeout)  # pyright: ignore[reportArgumentType]  # 假进程原样转发 None
            return self.returncode

    def _run_one(self, *, abort=False, timeout_now=False, stubborn=False):
        import maa_roguelike_worker as worker

        proc = self.FakeProc()
        proc.stubborn = stubborn
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "roguelike.log"
            log_path.write_text("", encoding="utf-8")
            with patch.object(
                worker.subprocess, "Popen", return_value=proc
            ) as popen, patch.object(
                worker, "_stop_flag",
                side_effect=lambda key: "2026-09-09T12:00:00+08:00"
                if (abort and key == "abort_requested_at") else "",
            ), patch.object(worker.time, "monotonic",
                            side_effect=[0.0] + [10 ** 9 if timeout_now else 1.0] * 40):
                code, output, halt = worker._run_one_exploration(
                    {"maa_bin": "/bin/maa", "roguelike_run_timeout": 3600},
                    {"MAA_CONFIG_DIR": "/tmp/x"}, log_path, 1,
                )
        return SimpleNamespace(
            proc=proc, code=code, output=output, halt=halt,
            command=popen.call_args.args[0], kwargs=popen.call_args.kwargs,
        )

    def test_the_command_is_the_task_file_route(self) -> None:
        result = self._run_one(abort=True)
        self.assertEqual(
            result.command,
            ["/bin/maa", "run", "roguelike", "--user-resource", "--batch", "-v"],
        )
        # 环境由 `maa_subprocess_env()` + MAA_LOG_PREFIX 决定，调用方传进来。
        self.assertEqual(result.kwargs["env"]["MAA_CONFIG_DIR"], "/tmp/x")

    def test_an_abort_terminates_and_never_kills_first(self) -> None:
        result = self._run_one(abort=True)
        self.assertEqual(result.halt, "abort")
        self.assertEqual(result.proc.terminated, 1)
        self.assertEqual(result.proc.killed, 0)

    def test_a_stubborn_process_is_killed_only_after_terminate(self) -> None:
        result = self._run_one(abort=True, stubborn=True)
        self.assertEqual(result.proc.terminated, 1)
        self.assertEqual(result.proc.killed, 1)

    def test_the_single_run_timeout_terminates_too(self) -> None:
        result = self._run_one(timeout_now=True)
        self.assertEqual(result.halt, "timeout")
        self.assertEqual(result.proc.terminated, 1)

    def test_the_worker_never_calls_subprocess_run(self) -> None:
        """`subprocess.run(timeout=)` 超时走 SIGKILL，会把模拟器留在地图里。"""
        source = (ROOT / "scripts" / "maa_roguelike_worker.py").read_text(
            encoding="utf-8"
        )
        called = {
            ast.unparse(node.func)
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
        }
        self.assertNotIn("subprocess.run", called)
        self.assertIn("subprocess.Popen", called)
        self.assertIn("proc.terminate", called)


class RoguelikeDeployTests(unittest.TestCase):
    def test_both_new_scripts_are_deployed_and_checked(self) -> None:
        # 部署和 `--check` 共用 `deploy.SCRIPT_NAMES`（DeployCheckTests 覆盖两边都用它）。
        self.assertIn("maa_roguelike.py", deploy.SCRIPT_NAMES)
        self.assertIn("maa_roguelike_worker.py", deploy.SCRIPT_NAMES)
        checked = {target.name for _source, target in deploy.script_file_pairs()}
        self.assertLessEqual({"maa_roguelike.py", "maa_roguelike_worker.py"}, checked)

    def test_the_single_run_timeout_is_seeded_and_not_a_runtime_cache(self) -> None:
        config = json.loads(
            (ROOT / "state" / "config.json").read_text(encoding="utf-8")
        )
        self.assertEqual(config["roguelike_run_timeout"], 3600)
        self.assertNotIn("roguelike_run_timeout", deploy.PRESERVED_RUNTIME_CACHE_KEYS)

    def test_the_skill_only_offers_the_six_roguelike_commands(self) -> None:
        skill = (ROOT / "skills" / "games" / "maa-operator" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        for command in ("roguelike card", "roguelike stop", "roguelike abort",
                        "roguelike status"):
            self.assertIn(command, skill)
        # 主题 / 难度只能用户在卡上点，聊天里不许替他选。
        self.assertNotIn("maa_action.py roguelike set", skill)
        self.assertNotIn("maa_action.py roguelike start", skill)


class RoguelikeStaleRoundTests(unittest.TestCase):
    """worker 崩了不能把功能永久锁死：状态说在跑但锁是空的 → 允许重开。"""

    def _state(self, minutes_ago: int, status: str = "running") -> tuple[dict, datetime]:
        moment = datetime(2026, 9, 9, 12, 0, tzinfo=BEIJING)
        return {
            **maa_roguelike.EMPTY_STATE,
            "status": status,
            "token": "b" * 12,
            "updated_at": (moment - timedelta(minutes=minutes_ago)).isoformat(),
        }, moment

    def test_a_held_lock_always_means_it_is_really_running(self) -> None:
        state, at = self._state(600)
        self.assertFalse(maa_roguelike.round_is_stale(state, True, at))

    def test_a_fresh_state_is_not_stale_even_without_the_lock(self) -> None:
        # `roguelike start` 写状态到 worker 抢锁之间有几秒窗口，不能把它误判成崩溃。
        state, at = self._state(0)
        self.assertFalse(maa_roguelike.round_is_stale(state, False, at))

    def test_an_old_state_without_the_lock_is_stale(self) -> None:
        state, at = self._state(30)
        self.assertTrue(maa_roguelike.round_is_stale(state, False, at))

    def test_an_idle_round_is_never_stale(self) -> None:
        state, at = self._state(600, status="idle")
        self.assertFalse(maa_roguelike.round_is_stale(state, False, at))

    def test_a_stale_round_can_be_started_again(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "roguelike.json"
            state, _at = self._state(600)
            maa_common.atomic_write_json(path, state)
            with patch.object(maa_roguelike, "STATE_PATH", path), patch.object(
                maa_roguelike, "state_lock", return_value=contextlib.nullcontext()
            ), patch.object(maa_action, "is_run_locked", return_value=False
            ), patch.object(maa_action, "load_config", return_value={"channel_id": "1"}
            ), patch.object(maa_action, "dispatch_blocker", return_value=""
            ), patch.object(maa_action, "dispatch_roguelike") as dispatch:
                ok, message = maa_action.roguelike_start()
            fresh = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(ok, message)
        self.assertIn("worker 已经不在了", message)
        self.assertEqual(fresh["status"], "running")
        self.assertNotEqual(fresh["token"], "b" * 12)
        dispatch.assert_called_once()



if __name__ == "__main__":
    unittest.main()
