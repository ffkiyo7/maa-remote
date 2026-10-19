from __future__ import annotations

import ast
import contextlib
import os
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import maa_action  # noqa: E402
import maa_clearing  # noqa: E402
import maa_common  # noqa: E402
import maa_copilot  # noqa: E402
import maa_depot  # noqa: E402
import maa_activity_watch  # noqa: E402
import maa_daily  # noqa: E402
import maa_farming  # noqa: E402
import maa_gamedata  # noqa: E402
import maa_plan_card  # noqa: E402
import maa_roguelike  # noqa: E402
import deploy  # noqa: E402


BEIJING = ZoneInfo("Asia/Shanghai")

# 整套单测**一条都不许碰网**（DEV_GUIDE §9）。`maa_farming` 的抓取层默认把缓存写进
# 线上 `~/.hermes/state/maa/farming/`，所以一条漏了打桩的用例既会打一图流，
# 又会往运行时状态里塞一份带假时间戳的缓存。这里在模块级把网络出口整个封死，
# 需要自己造响应的用例（`FarmingFetchTests`）再在 `with` 里覆盖它。
_NO_NETWORK = None


_GAMEDATA_PATCHES: list = []


def setUpModule() -> None:
    global _NO_NETWORK
    _NO_NETWORK = patch.object(
        maa_farming,
        "_http_json",
        side_effect=AssertionError("单测不许访问一图流 / 企鹅物流：传 data= 或自行打桩"),
    )
    _NO_NETWORK.start()
    # 游戏数据同理：不许抓 ArknightsGameData，也不许读写线上 `copilot/gamedata.json`。
    # 默认给一份月行水上（SR）的小地形，要别的地形的用例自己 patch `load_layout`。
    _GAMEDATA_PATCHES[:] = [
        patch.object(maa_gamedata, "_fetch",
                     side_effect=AssertionError("单测不许抓游戏数据：自行打桩")),
        patch.object(maa_gamedata, "load_layout", side_effect=lambda: json.loads(
            json.dumps(SR_LAYOUT))),
        patch.object(maa_gamedata, "refresh_layout", side_effect=lambda *_a, **_k: (
            maa_gamedata.load_layout(), "")),
    ]
    for item in _GAMEDATA_PATCHES:
        item.start()


def tearDownModule() -> None:
    if _NO_NETWORK is not None:
        _NO_NETWORK.stop()
    for item in reversed(_GAMEDATA_PATCHES):
        item.stop()




class PlanTests(unittest.TestCase):
    def test_last_run_merge_holds_its_dedicated_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "last_run.json"
            maa_common.atomic_write_json(path, {"status": "running"})
            context = unittest.mock.MagicMock()
            context.__enter__.return_value = None
            context.__exit__.return_value = False
            with patch.object(maa_common, "LAST_RUN_PATH", path), patch.object(
                maa_common, "last_run_lock", return_value=context
            ) as lock:
                result = maa_common.merge_last_run({"last_lock_conflict_phase": "am"})
        lock.assert_called_once_with()
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["last_lock_conflict_phase"], "am")

    def test_maa_environment_is_deterministic_and_scoped(self) -> None:
        with patch.dict(
            os.environ,
            {"HTTPS_PROXY": "http://127.0.0.1:1", "KEEP_ME": "yes"},
            clear=True,
        ):
            env = maa_common.maa_subprocess_env(
                {
                    "maa_config_dir": "/tmp/maa-config",
                    "maa_disable_proxy": True,
                }
            )
        self.assertEqual(env["MAA_CONFIG_DIR"], "/tmp/maa-config")
        self.assertEqual(env["KEEP_ME"], "yes")
        self.assertNotIn("HTTPS_PROXY", env)

    def test_maa_update_keeps_proxy_when_requested(self) -> None:
        with patch.dict(
            os.environ,
            {"HTTPS_PROXY": "http://127.0.0.1:7890", "KEEP_ME": "yes"},
            clear=True,
        ):
            env = maa_common.maa_subprocess_env(
                {
                    "maa_config_dir": "/tmp/maa-config",
                    "maa_disable_proxy": True,
                },
                keep_proxy=True,
            )
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(env["KEEP_ME"], "yes")

    def test_normalize_plan_hard_resets_stone(self) -> None:
        plan = maa_common.normalize_plan(
            {"stage": "CE-6", "medicine": "expiring", "stone": 99}
        )
        self.assertEqual(plan["stone"], 0)

    def test_default_stage_is_1_7(self) -> None:
        self.assertEqual(maa_common.DEFAULT_PLAN["stage"], "1-7")
        self.assertEqual(maa_common.normalize_plan({})["stage"], "1-7")
        self.assertEqual(maa_common.normalize_plan({"stage": ""})["stage"], "")

    def test_session_overrides_apply_per_phase_and_expire_next_day(self) -> None:
        """四期 1a：覆盖是按场次的，`now` 一场都不叠（D4）。"""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            defaults = root / "defaults.json"
            override = root / "plan.json"
            maa_common.atomic_write_json(defaults, {"stage": "1-7", "medicine": "none"})
            maa_common.atomic_write_json(
                override,
                {
                    "date": "2026-08-02",
                    "sessions": {
                        "am": {"stage": "CE-6", "stage_source": "discord", "skip": True},
                        "pm": {"stage": "SK-5"},
                    },
                },
            )
            at = datetime(2026, 8, 2, tzinfo=BEIJING)
            with patch.object(maa_common, "DEFAULTS_PATH", defaults), patch.object(
                maa_common, "PLAN_PATH", override
            ):
                morning = maa_common.effective_plan(at, "am")
                evening = maa_common.effective_plan(at, "pm")
                manual = maa_common.effective_plan(at, "now")
                tomorrow = maa_common.effective_plan(
                    datetime(2026, 8, 3, tzinfo=BEIJING), "am"
                )
        self.assertEqual(morning["stage"], "CE-6")
        self.assertTrue(morning["skip"])
        # 早场的关与跳过都不会漏到晚场（D3）。
        self.assertEqual(evening["stage"], "SK-5")
        self.assertFalse(evening["skip"])
        # 手动场永远是缺口默认关（D4）。
        self.assertEqual(manual["stage"], "1-7")
        self.assertFalse(manual["skip"])
        self.assertEqual(tomorrow["stage"], "1-7")
        self.assertEqual(tomorrow["medicine"], "expiring")

    def test_legacy_day_level_plan_reads_as_empty_and_never_raises(self) -> None:
        """旧的按天格式（顶层 stage/skip）不迁移，直接当空——一天后自然失效。"""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            defaults = root / "defaults.json"
            override = root / "plan.json"
            maa_common.atomic_write_json(defaults, {"stage": "1-7"})
            at = datetime(2026, 8, 2, tzinfo=BEIJING)
            for legacy in (
                {"date": "2026-08-02", "stage": "CE-6", "skip": True},
                {"date": "2026-08-02", "sessions": ["not", "a", "dict"]},
                ["not a dict at all"],
            ):
                maa_common.atomic_write_json(override, legacy)
                with self.subTest(legacy=legacy):
                    with patch.object(
                        maa_common, "DEFAULTS_PATH", defaults
                    ), patch.object(maa_common, "PLAN_PATH", override):
                        self.assertEqual(maa_common.plan_sessions(at), {})
                        for phase in ("am", "pm", "now"):
                            plan = maa_common.effective_plan(at, phase)
                            self.assertEqual(plan["stage"], "1-7")
                            self.assertFalse(plan["skip"])

    def test_normalize_plan_carries_stage_source_through(self) -> None:
        """不再吃掉 `stage_source`：卡片靠它说「这关是谁定的」。"""
        self.assertEqual(
            maa_common.normalize_plan({"stage_source": "auto"})["stage_source"], "auto"
        )
        self.assertNotIn("stage_source", maa_common.normalize_plan({}))

    def test_skip_wording_everywhere_says_manual_still_runs(self) -> None:
        skipped = maa_common.normalize_plan({"skip": True})
        receipt = maa_action._confirmation(skipped, False, "am")
        self.assertIn("现在跑", receipt)
        self.assertIn("早场（08:00）", receipt)
        self.assertIn("晚场（22:00）不受影响", receipt)
        card = maa_plan_card.build_embed(skipped, {}, "am")
        field = next(f for f in card["fields"] if f["name"] == "本场跳过")
        self.assertIn("另一场与手动开跑不受影响", field["value"])
        annihilation = next(f for f in card["fields"] if f["name"] == "每日剿灭")
        self.assertIn("1800", annihilation["value"])
        components, registry = maa_plan_card.build_components(
            maa_common.normalize_plan({"skip": False}), {"channel_id": "1"}, "am"
        )
        labels = [
            component["label"]
            for row in components
            for component in row.get("components", [])
            if "label" in component
        ]
        self.assertIn("⏭️ 跳过本场（早场 08:00）", labels)
        self.assertTrue(any(entry["kind"] == "exec" for entry in registry.values()))

    def test_stage_allowlist(self) -> None:
        config = {"activity_stages": ["SSReopen-IC"], "maa_bin": "/missing/maa"}
        self.assertTrue(maa_action.valid_stage("", config))
        self.assertTrue(maa_action.valid_stage("CE-6", config))
        self.assertTrue(maa_action.valid_stage("1-7", config))
        self.assertTrue(maa_action.valid_stage("SSReopen-IC", config))
        self.assertFalse(maa_action.valid_stage("../../bad", config))
        self.assertFalse(maa_action.valid_stage("CE-6;shutdown", config))

    def test_activity_probe_requires_an_exact_stage_token(self) -> None:
        config = {"activity_stages": [], "maa_bin": "/maa"}
        result = SimpleNamespace(
            returncode=0,
            stdout="Official server: current activity SSReopen-IC-9",
            stderr="",
        )
        with patch.object(maa_action.subprocess, "run", return_value=result):
            self.assertTrue(maa_action.valid_stage("SSReopen-IC-9", config))
            self.assertFalse(maa_action.valid_stage("Official", config))
            self.assertFalse(maa_action.valid_stage("Reopen-IC-9", config))


class RenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = maa_common.normalize_plan(
            {"stage": "CE-6", "medicine": "expiring", "tasks": {}}
        )
        self.config = {
            "medicine_expire_days": 2,
            "fight_times": 2147483647,
            "recruit_times": 4,
        }

    def test_task_order_and_screenshot_guardrail(self) -> None:
        rendered = maa_daily.render_daily_toml(self.plan, self.config)
        positions = [
            rendered.index(f'type = "{name}"')
            for name in ("StartUp", "Fight", "Infrast", "Recruit", "Mall", "Award", "Custom")
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("CloseDown", rendered)
        self.assertIn("stone = 0", rendered)
        self.assertIn("series = 0", rendered)
        self.assertIn("mode = 0", rendered)
        self.assertNotIn("mode = 20000", rendered)
        self.assertIn(
            'facility = ["Mfg", "Trade", "Power", "Control", "Reception", "Office", "Dorm"]',
            rendered,
        )
        self.assertIn('drones = "_NotUse"', rendered)
        self.assertIn("replenish = false", rendered)
        self.assertIn("dorm_trust_enabled = false", rendered)
        self.assertIn("medicine = 0", rendered)
        self.assertIn("medicine_expire_days = 2", rendered)
        self.assertIn("mail = true", rendered)
        self.assertNotIn("mail = false", rendered)
        self.assertIn('name = "每日剿灭"', rendered)
        self.assertIn('stage = "Annihilation"', rendered)
        self.assertNotIn("weekdays", rendered)
        self.assertNotIn("[[tasks.variants]]", rendered)
        self.assertLess(rendered.index('name = "每日剿灭"'), rendered.index('name = "刷理智"'))
        self.assertIn('name = "返回主界面"', rendered)
        # Every QuickSwitch/ReturnButtons name must carry the "Home@" caller
        # prefix, otherwise `#back` resolves to a non-existent task and the
        # chain errors out after it has already reached the home page.
        self.assertIn(
            'task_names = ["Home", "Home@QuickSwitch@ToHome@Entry", '
            '"Home@QuickSwitch@ToHome@Open", "Home@ReturnButton", '
            '"Home@FromStageSN", "Home@FromAnnihilation"]',
            rendered,
        )
        self.assertNotIn('"QuickSwitch@ToHome"', rendered)
        self.assertNotIn('"ReturnButtons"', rendered)
        for name in maa_daily.HOME_TASK_NAMES[1:]:
            self.assertTrue(name.startswith("Home@"), name)

    def test_persisted_medicine_choice_cannot_disable_fixed_expiry_policy(self) -> None:
        self.plan["medicine"] = "none"
        rendered = maa_daily.render_daily_toml(self.plan, self.config)
        self.assertIn("medicine = 0", rendered)
        self.assertIn("medicine_expire_days = 2", rendered)
        self.assertIn("stone = 0", rendered)

    def test_zero_fight_times_is_preserved_for_non_combat_commissioning(self) -> None:
        self.config["fight_times"] = 0
        rendered = maa_daily.render_daily_toml(self.plan, self.config)
        self.assertIn("times = 0", rendered)
        self.assertIn("series = 0", rendered)
        self.assertIn("stone = 0", rendered)

    def test_disabling_fight_also_disables_weekly_annihilation(self) -> None:
        self.plan["tasks"]["fight"] = False
        rendered = maa_daily.render_daily_toml(self.plan, self.config)
        self.assertNotIn('stage = "Annihilation"', rendered)
        self.assertNotIn('name = "刷理智"', rendered)

    def test_annihilation_omitted_when_already_done_today(self) -> None:
        self.plan["annihilation"] = False
        rendered = maa_daily.render_daily_toml(self.plan, self.config)
        self.assertNotIn('stage = "Annihilation"', rendered)
        self.assertNotIn('name = "每日剿灭"', rendered)
        self.assertIn('name = "刷理智"', rendered)
        self.assertIn('stage = "CE-6"', rendered)

    def test_annihilation_once_per_cn_game_day(self) -> None:
        plan = maa_common.normalize_plan({"stage": "1-7"})
        monday_am = datetime(2026, 8, 17, 8, 0, tzinfo=BEIJING)
        monday_pm = datetime(2026, 8, 17, 22, 0, tzinfo=BEIJING)
        before_reset = datetime(2026, 8, 18, 3, 59, tzinfo=BEIJING)
        after_reset = datetime(2026, 8, 18, 4, 0, tzinfo=BEIJING)
        self.assertTrue(maa_daily.should_run_annihilation(plan, monday_am, {}))
        last = {"annihilation_game_day": maa_daily.cn_game_day(monday_am)}
        self.assertEqual(last["annihilation_game_day"], "2026-08-17")
        self.assertFalse(maa_daily.should_run_annihilation(plan, monday_pm, last))
        self.assertFalse(maa_daily.should_run_annihilation(plan, before_reset, last))
        self.assertTrue(maa_daily.should_run_annihilation(plan, after_reset, last))
        no_fight = maa_common.normalize_plan({"stage": "1-7", "tasks": {"fight": False}})
        self.assertFalse(maa_daily.should_run_annihilation(no_fight, monday_am, {}))
        attached = maa_daily.attach_annihilation(plan, monday_pm, last)
        self.assertFalse(attached["annihilation"])

    def test_annihilation_consumed_after_startup_or_named_task(self) -> None:
        self.assertTrue(maa_daily.annihilation_consumed(["StartUp Completed"]))
        self.assertTrue(maa_daily.annihilation_consumed(["[每日剿灭] Completed"]))
        self.assertTrue(maa_daily.annihilation_consumed(["[每周剿灭] Completed"]))
        self.assertTrue(maa_daily.annihilation_consumed(["Fight Annihilation 1 times"]))
        self.assertFalse(
            maa_daily.annihilation_consumed(["StartUp Start", "TouchModeNotAvailable"])
        )

    def test_skip_only_silences_the_automatic_sessions(self) -> None:
        skipped = maa_common.normalize_plan({"skip": True})
        running = maa_common.normalize_plan({"skip": False})
        self.assertTrue(maa_daily.should_skip(skipped, "am"))
        self.assertTrue(maa_daily.should_skip(skipped, "pm"))
        # 手动场是明确指令，否则「跳过今天」之后就完全没法开跑了。
        self.assertFalse(maa_daily.should_skip(skipped, "now"))
        for phase in ("am", "pm", "now"):
            self.assertFalse(maa_daily.should_skip(running, phase))

    def test_phase_uses_cron_workdir_marker(self) -> None:
        at = datetime(2026, 8, 2, 16, 0, tzinfo=BEIJING)
        self.assertEqual(maa_daily.select_phase(["maa_daily.py"], at, Path("/tmp/am")), "am")
        self.assertEqual(maa_daily.select_phase(["maa_daily.py"], at, Path("/tmp/pm")), "pm")
        self.assertEqual(maa_daily.select_phase(["maa_daily.py", "now"], at), "now")

    def test_progress_message_stays_under_discord_limit(self) -> None:
        content = maa_daily.progress_content(["x" * 1000 for _ in range(30)])
        self.assertLessEqual(len(content), 1990)
        self.assertNotIn("```", content)
        self.assertNotIn("x" * 50, content)

    def test_progress_and_summary_translate_cli_events(self) -> None:
        lines = [
            "StartUp Start",
            "StartUp Completed",
            "Fight Start",
            "Current sanity: 120/207",
            "Mission started (6 times, use 36 sanity)",
            "Use 1 expiring medicine",
            "Drops: 固源岩: 8, 龙门币: 432",
            'total drops: {"固源岩": 8, "龙门币": 432}',
            "Fight Completed",
            "Infrast Start",
            "EnterFacility Mfg #0",
            "EnterFacility Trade #0",
            "EnterFacility Power #0",
            "EnterFacility Control #0",
            "EnterFacility Reception #0",
            "EnterFacility Office #0",
            "EnterFacility Dorm #0",
            "Infrast Completed",
            "Recruit Start",
            "Recruit Completed",
            "Recruited 4 times",
            "Refreshed 3 times",
            "Mall Start",
            "Mall Completed",
            "Award Start",
            "Award Completed",
            "Custom Start",
            "Custom Completed",
        ]
        plan = maa_common.normalize_plan({"stage": "1-7"})
        progress = maa_daily.progress_content(lines[:4], plan=plan)
        self.assertIn("⚔️ 正在刷 1-7", progress)
        self.assertNotIn("Fight Start", progress)
        summary = maa_daily.extract_summary(lines, [], plan=plan, ok=True)
        self.assertIn("🌟 总体：✅ 成功", summary)
        self.assertIn("关卡 1-7", summary)
        self.assertIn("6 次", summary)
        self.assertIn("临期理智药 ×1", summary)
        self.assertIn("固源岩 ×8", summary)
        self.assertNotIn("固源岩 ×16", summary)
        self.assertIn("制造站 ×1", summary)
        self.assertIn("发电站 ×1", summary)
        self.assertIn("控制中枢 ×1", summary)
        self.assertIn("宿舍 ×1", summary)
        self.assertIn("招募 4 次", summary)
        self.assertIn("🏠 返回主界面：✅ 完成", summary)
        self.assertNotIn("Completed", summary)

    def test_named_fight_summaries_keep_annihilation_separate(self) -> None:
        lines = [
            "Fight Start",
            "Fight Completed",
            "Fight Start",
            "Fight Error",
            "[每周剿灭] Completed",
            "Fight Annihilation 5 times",
            "total drops: 合成玉 ×1800",
            "[刷理智] Error",
            "Fight 1-7 3 times, drops:",
            "total drops: 固源岩 ×3",
        ]
        plan = maa_common.normalize_plan({"stage": "1-7"})
        summary = maa_daily.extract_summary(lines, [], plan=plan, ok=False)
        self.assertIn("☄️ 每日剿灭：✅ 完成（当期剿灭 5 次", summary)
        self.assertIn("代理卡全权委托", summary)
        self.assertIn("1800 合成玉/周", summary)
        self.assertIn("⚔️ 刷理智：❌ 未完成（关卡 1-7；3 次；掉落 固源岩 ×3）", summary)
        self.assertNotIn("合成玉 ×1800", summary)

    def test_summary_notes_annihilation_already_done(self) -> None:
        plan = maa_common.normalize_plan({"stage": "1-7"})
        plan["annihilation"] = False
        summary = maa_daily.extract_summary(
            ["StartUp Completed", "[刷理智] Completed"],
            [],
            plan=plan,
            ok=True,
        )
        self.assertIn("☄️ 每日剿灭：⏭️ 今日已刷过，本场跳过", summary)
        self.assertNotIn("每日剿灭：⏸️ 未执行", summary)

    def test_cleanup_continues_after_closedown_is_missing(self) -> None:
        results = [
            FileNotFoundError("maa"),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=1, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=1, stdout="", stderr=""),
        ]

        def fake_run(*_args, **_kwargs):
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with patch.object(maa_daily.subprocess, "run", side_effect=fake_run) as run, patch.object(
            maa_daily.time, "sleep"
        ):
            errors = maa_daily.close_everything(
                {
                    "maa_bin": "/missing/maa",
                    "adb_path": "/missing/adb",
                    "emulator_app": "BlueStacks",
                    "quit_grace_seconds": 0,
                }
            )
        self.assertEqual(run.call_count, 5)
        self.assertTrue(any("closedown" in item for item in errors))
        self.assertEqual(
            run.call_args_list[-1].args[0],
            ["pgrep", "-fi", "[B]lueStacks"],
        )

    def test_cleanup_waits_for_processes_to_exit_after_fallback(self) -> None:
        results = [
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=1, stdout="", stderr="cancelled"),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="123\n", stderr=""),
            SimpleNamespace(returncode=1, stdout="", stderr=""),
        ]
        with patch.object(maa_daily.subprocess, "run", side_effect=results) as run, patch.object(
            maa_daily.time, "sleep"
        ):
            errors = maa_daily.close_everything(
                {
                    "maa_bin": "/maa",
                    "adb_path": "/adb",
                    "emulator_app": "BlueStacks",
                    "emulator_bundle_id": "com.now.gg.BlueStacks",
                    "quit_grace_seconds": 0,
                    "cleanup_verify_timeout": 2,
                }
            )
        self.assertEqual(errors, [])
        self.assertEqual(run.call_count, 6)

    def test_game_package_preflight(self) -> None:
        config = {
            "adb_path": "/adb",
            "adb_address": "127.0.0.1:5555",
            "game_package": "com.hypergryph.arknights",
        }
        installed = SimpleNamespace(
            returncode=0,
            stdout="package:/data/app/com.hypergryph.arknights/base.apk\n",
            stderr="",
        )
        with patch.object(maa_daily.subprocess, "run", return_value=installed) as run:
            maa_daily.verify_game_installed(config)
        self.assertEqual(
            run.call_args.args[0],
            [
                "/adb",
                "-s",
                "127.0.0.1:5555",
                "shell",
                "pm",
                "path",
                "com.hypergryph.arknights",
            ],
        )

        missing = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(maa_daily.subprocess, "run", return_value=missing):
            with self.assertRaisesRegex(RuntimeError, "未检测到"):
                maa_daily.verify_game_installed(config)

    def test_maa_run_timeout_terminates_and_returns_124(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.stdout = io.StringIO("")
                self.stderr = io.StringIO("")
                self.terminated = False

            def poll(self):
                return 0 if self.terminated else None

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.terminated = True

            def wait(self, timeout=None):
                return 0

        process = FakeProcess()
        with tempfile.TemporaryDirectory() as raw, patch.object(
            maa_daily.subprocess, "Popen", return_value=process
        ) as popen, patch.object(maa_daily.time, "monotonic", side_effect=[0, 0, 61]):
            log_path = Path(raw) / "test.log"
            code, _stdout, stderr = maa_daily.run_maa(
                {"maa_bin": "/maa", "maa_run_timeout": 60},
                log_path,
                "token",
                "thread",
                "message",
            )
            command = popen.call_args.args[0]
            self.assertNotIn("--log-file", " ".join(command))
            self.assertNotIn("--user-resource", command)
            self.assertIn("maa run 超时", log_path.read_text(encoding="utf-8"))
        self.assertEqual(code, 124)
        self.assertTrue(process.terminated)
        self.assertTrue(any("客户端更新" in line for line in stderr))

    def test_run_maa_tees_both_streams_to_log(self) -> None:
        process = SimpleNamespace(
            stdout=io.StringIO("Summary\nFight Succeeded\n"),
            stderr=io.StringIO("Connecting to device\n"),
            poll=lambda: 0,
            wait=lambda timeout=None: 0,
        )
        with tempfile.TemporaryDirectory() as raw, patch.object(
            maa_daily.subprocess, "Popen", return_value=process
        ):
            log_path = Path(raw) / "maa.log"
            code, stdout, stderr = maa_daily.run_maa(
                {"maa_bin": "/maa"}, log_path, "token", "thread", "message"
            )
            log = log_path.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertIn("Summary", stdout)
        self.assertIn("Connecting to device", stderr)
        self.assertIn("Summary", log)
        self.assertIn("Connecting to device", log)

    def test_run_maa_handles_keyboard_interrupt_and_closes_streams(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.stdout = io.StringIO("partial summary\n")
                self.stderr = io.StringIO("partial progress\n")
                self.terminated = False

            def poll(self):
                return 0 if self.terminated else None

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.terminated = True

            def wait(self, timeout=None):
                return 130 if self.terminated else 0

        process = FakeProcess()
        with tempfile.TemporaryDirectory() as raw, patch.object(
            maa_daily.subprocess, "Popen", return_value=process
        ), patch.object(maa_daily.time, "monotonic", side_effect=[0, 0, -1]), patch.object(
            maa_daily.time, "sleep", side_effect=KeyboardInterrupt
        ):
            log_path = Path(raw) / "interrupted.log"
            code, _stdout, stderr = maa_daily.run_maa(
                {"maa_bin": "/maa"}, log_path, "token", "thread", "message"
            )
            log = log_path.read_text(encoding="utf-8")
        self.assertEqual(code, 130)
        self.assertTrue(process.terminated)
        self.assertTrue(any("人工指令" in line for line in stderr))
        self.assertIn("maa run 已按人工指令终止", log)


class EmulatorBootDiagnosticTests(unittest.TestCase):
    def test_reports_upgrade_gate_before_vm_start(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            player_log = Path(raw) / "Player.log"
            player_log.write_text(
                "old session\n[QTUI] Start Virtual Machine\n"
                "=================== xlogStart [5.21] =================\n"
                'cldDoPostRequest url: "https://cloud.bluestacks.com/bs3/check_upgrade"\n'
            )
            detail = maa_daily.emulator_boot_diagnostic(
                {"emulator_player_log": str(player_log)}
            )
        self.assertIn("升级检查没有放行", detail)

    def test_ignores_session_that_started_the_vm(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            player_log = Path(raw) / "Player.log"
            player_log.write_text(
                "=================== xlogStart [5.21] =================\n"
                'cldDoPostRequest url: "https://cloud.bluestacks.com/bs3/check_upgrade"\n'
                "[QTUI] Start Virtual Machine\n"
            )
            detail = maa_daily.emulator_boot_diagnostic(
                {"emulator_player_log": str(player_log)}
            )
        self.assertEqual(detail, "")


class DiscordCardTests(unittest.TestCase):
    def test_activity_events_parser(self) -> None:
        output = """Opening side story stages:
- SideStory「直到大地变成一颗酸橙」
  - TO-9: 晶体元件
  - TO-8: 凝胶
  - TO-7: 糖组
Opening mini games:
- 黑流树海刷钱
"""
        self.assertEqual(
            maa_plan_card.parse_activity_events(output),
            [
                {
                    "title": "SideStory「直到大地变成一颗酸橙」",
                    "stages": ["TO-9", "TO-8", "TO-7"],
                }
            ],
        )
        self.assertFalse(maa_plan_card.all_supply_stages_open(output))
        self.assertTrue(maa_plan_card.all_supply_stages_open("资源收集\n"))

    def test_activity_snapshot_ignores_stage_shaped_minigame_bullets(self) -> None:
        output = """Opening side story stages:
- SideStory「活动」
  - NEW-8: 材料
Opening mini games:
- XX-1: 这不是活动作战关
"""
        self.assertEqual(maa_plan_card.activity_snapshot(output)["stages"], ["NEW-8"])

    def test_activity_snapshot_returns_empty_during_recognized_gap(self) -> None:
        output = "Opening mini games:\n- XX-1: 小游戏\n"
        self.assertEqual(
            maa_plan_card.activity_snapshot(output),
            {
                "stages": [],
                "events": [],
                "all_supply_stages_open": False,
            },
        )

    def test_activity_snapshot_fails_closed_when_upstream_format_changes(self) -> None:
        for output in ("", "not a maa activity response\n"):
            with self.subTest(output=output):
                with self.assertRaisesRegex(RuntimeError, "输出格式已变化"):
                    maa_plan_card.activity_snapshot(output)

    def test_activity_snapshot_fails_closed_on_a_decorated_heading(self) -> None:
        """A heading the parser cannot match must raise, not read as a gap.

        The gate and parse_activity_events() share is_side_story_heading(), so
        a reflowed or decorated heading can never silently produce zero events
        and clear the live stage list.
        """
        for heading in (
            "Opening side story stages: (2)",
            "Opening side story stages:!",
            "**Opening side story stages:**",
            "  Opening side story stages:",
        ):
            output = f"{heading}\n- SideStory「活动」\n  - NEW-8: 材料\nOpening mini games:\n"
            with self.subTest(heading=heading):
                with self.assertRaisesRegex(RuntimeError, "输出格式已变化"):
                    maa_plan_card.activity_snapshot(output)

    def test_snapshot_gate_and_parser_agree_on_what_a_heading_is(self) -> None:
        """The gate may only fall through to the parser on headings it parses."""
        for heading in (
            "Opening side story stages:",
            "Opening side story stages:   ",
            "Opening side story stages: (2)",
            "  Opening side story stages:",
        ):
            output = f"{heading}\n- SideStory「活动」\n  - NEW-8: 材料\n"
            with self.subTest(heading=heading):
                parsed = bool(maa_plan_card.parse_activity_events(output))
                try:
                    gated = bool(maa_plan_card.activity_snapshot(output)["stages"])
                except RuntimeError:
                    gated = False
                self.assertEqual(parsed, gated)

    def test_empty_activity_snapshot_clears_persisted_stages(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config_path = Path(raw) / "config.json"
            config = {"activity_stages": ["OLD-8"]}
            with patch.object(maa_plan_card, "CONFIG_PATH", config_path):
                maa_plan_card.persist_activity_snapshot(
                    config,
                    maa_plan_card.activity_snapshot(
                        "Opening mini games:\n- 当前开放小游戏\n"
                    ),
                )
            persisted = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["activity_stages"], [])
        self.assertEqual(persisted["activity_stages"], [])

    def test_activity_snapshot_does_not_clear_on_unrecognized_output(self) -> None:
        config = {"activity_stages": ["OLD-8"]}
        with self.assertRaisesRegex(RuntimeError, "输出格式已变化"):
            maa_plan_card.activity_snapshot("garbled output")
        self.assertEqual(config["activity_stages"], ["OLD-8"])

    def test_persist_activity_snapshot_drops_the_obsolete_event_cache(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config_path = Path(raw) / "config.json"
            config = {"activity_events": [{"title": "旧活动"}]}
            with patch.object(maa_plan_card, "CONFIG_PATH", config_path):
                maa_plan_card.persist_activity_snapshot(
                    config,
                    {
                        "stages": ["NEW-8"],
                        "events": [{"title": "新活动", "stages": ["NEW-8"]}],
                        "all_supply_stages_open": False,
                    },
                )
            persisted = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertNotIn("activity_events", config)
        self.assertNotIn("activity_events", persisted)
        self.assertEqual(persisted["activity_stages"], ["NEW-8"])

    def test_buttons_fit_limits_and_registry_is_deterministic(self) -> None:
        plan = maa_common.normalize_plan({"stage": "CE-6", "medicine": "none"})
        components, registry = maa_plan_card.build_components(
            plan, {"channel_id": "123", "activity_stages": []}, "am"
        )
        self.assertLessEqual(len(components), 5)
        for row in components:
            self.assertLessEqual(len(row["components"]), 5)
        select = components[0]["components"][0]
        self.assertEqual(select["type"], 3)
        self.assertLessEqual(len(select["options"]), 25)
        self.assertEqual(sum(bool(option.get("default")) for option in select["options"]), 1)
        self.assertIn("select_args", registry["hermes:btn:maa:am:stage-select"])
        self.assertNotIn("hermes:btn:maa:run-now", registry)
        self.assertFalse(any("medicine" in key for key in registry))

    def test_the_card_has_exactly_one_select_and_one_button(self) -> None:
        """D2：计划卡只剩一个下拉 + 一个「跳过本场」按钮，恰好两行。"""
        for phase, expect_label in (
            ("am", "⏭️ 跳过本场（早场 08:00）"),
            ("pm", "⏭️ 跳过本场（晚场 22:00）"),
        ):
            with self.subTest(phase=phase):
                rows, registry = maa_plan_card.build_components(
                    maa_common.normalize_plan({}),
                    {"channel_id": "123", "activity_stages": []},
                    phase,
                )
                self.assertEqual(len(rows), 2)
                self.assertEqual(len(rows[0]["components"]), 1)
                self.assertEqual(rows[0]["components"][0]["type"], 3)
                self.assertEqual(len(rows[1]["components"]), 1)
                self.assertEqual(rows[1]["components"][0]["type"], 2)
                self.assertEqual(rows[1]["components"][0]["label"], expect_label)
                # 被删掉的控件不许留在登记表里。
                self.assertEqual(len(registry), 2)
                for key in registry:
                    self.assertNotIn("explain", key)
                    self.assertNotIn("log", key)
                    self.assertNotIn("farming-select", key)
                self.assertFalse(
                    any(key.startswith("hermes:btn:maa-copilot:") for key in registry)
                )
                self.assertTrue(
                    all(entry["kind"] == "exec" for entry in registry.values())
                )

    def test_resuming_a_skipped_session_offers_the_restore_button(self) -> None:
        rows, registry = maa_plan_card.build_components(
            maa_common.normalize_plan({"skip": True}),
            {"channel_id": "123", "activity_stages": []},
            "pm",
        )
        button = rows[1]["components"][0]
        self.assertEqual(button["label"], "▶️ 恢复本场")
        self.assertEqual(
            registry[button["custom_id"]]["args"],
            ["plan", "skip-today", "off", "pm"],
        )

    def test_activity_dropdown_uses_configured_stages(self) -> None:
        plan = maa_common.normalize_plan({})
        components, registry = maa_plan_card.build_components(
            plan, {"channel_id": "123", "activity_stages": ["HS-9"]}, "pm"
        )
        select = components[0]["components"][0]
        option = next(option for option in select["options"] if option["label"] == "HS-9")
        self.assertEqual(
            registry["hermes:btn:maa:pm:stage-select"]["select_args"][option["value"]],
            ["plan", "set-stage", "HS-9", "pm"],
        )

    def test_every_control_carries_its_own_phase(self) -> None:
        """不变量 #8 的场次版：场次写死在登记表里，不从 Discord 直通。"""
        for phase in ("am", "pm"):
            with self.subTest(phase=phase):
                _rows, registry = maa_plan_card.build_components(
                    maa_common.normalize_plan({}),
                    {"channel_id": "123", "activity_stages": ["HS-9"]},
                    phase,
                )
                select = registry[f"hermes:btn:maa:{phase}:stage-select"]
                for value, args in select["select_args"].items():
                    self.assertTrue(value.startswith("choice:"))
                    self.assertEqual(args[0], "plan")
                    self.assertEqual(args[-1], phase)
                skip = registry[f"hermes:btn:maa:{phase}:skip"]
                self.assertEqual(skip["args"], ["plan", "skip-today", "on", phase])

    def test_dropdown_includes_every_regular_supply_stage(self) -> None:
        plan = maa_common.normalize_plan({"stage": "AP-5"})
        components, registry = maa_plan_card.build_components(
            plan,
            {
                "channel_id": "123",
                "activity_stages": [],
                "all_supply_stages_open": True,
            },
            "am",
        )
        select = components[0]["components"][0]
        options = {option["label"]: option for option in select["options"]}
        expected = {
            "AP-5 · 红票": "AP-5",
            "CA-5 · 技能书": "CA-5",
            "CE-6 · 龙门币": "CE-6",
            "LS-6 · 作战记录": "LS-6",
            "SK-5 · 基建材料": "SK-5",
        }
        select_args = registry["hermes:btn:maa:am:stage-select"]["select_args"]
        for label, stage in expected.items():
            option = options[label]
            self.assertEqual(
                select_args[option["value"]], ["plan", "set-stage", stage, "am"]
            )
        self.assertTrue(options["AP-5 · 红票"]["default"])

    def test_supply_dropdown_uses_cn_reset_and_weekday_schedule(self) -> None:
        # 03:59 Sunday is still Saturday's game day; 04:00 switches to Sunday.
        before_reset = maa_plan_card.current_supply_choices(
            {}, datetime(2026, 8, 2, 3, 59, tzinfo=BEIJING)
        )
        after_reset = maa_plan_card.current_supply_choices(
            {}, datetime(2026, 8, 2, 4, 0, tzinfo=BEIJING)
        )
        self.assertEqual(
            {stage for _label, stage, _description in before_reset},
            {"AP-5", "CE-6", "LS-6", "SK-5"},
        )
        self.assertEqual(
            {stage for _label, stage, _description in after_reset},
            {"AP-5", "CA-5", "CE-6", "LS-6"},
        )

    def test_retire_old_cards_only_patches_maa_cards_with_controls(self) -> None:
        messages = [
            {
                "id": "111",
                "embeds": [{"title": "🌱 MAA 今日计划"}],
                "components": [{"type": 1}],
            },
            {
                "id": "222",
                "embeds": [{"title": "别的卡片"}],
                "components": [{"type": 1}],
            },
        ]
        with patch.object(
            maa_plan_card,
            "discord_request",
            side_effect=[messages, {}],
        ) as request:
            result = maa_plan_card.retire_old_plan_cards("token", "123")
        self.assertEqual(result, {"retired": 1, "failed": 0})
        self.assertEqual(request.call_args_list[1].args[1:3], ("PATCH", "/channels/123/messages/111"))
        self.assertEqual(request.call_args_list[1].args[3], {"components": []})

    def test_adapter_patch_dispatches_only_registered_select_values(self) -> None:
        source = '''_HERMES_BUTTON_SCRIPTS = frozenset({"agent_book_action.py"})
    async def _run_hermes_button_script(
        self,
        entry: dict[str, Any],
    ) -> tuple[bool, str]:
        raw_args = entry.get("args", [])
        if not isinstance(raw_args, list) or len(raw_args) > 10:
            return False, "按钮动作参数无效。"
        args = [str(value) for value in raw_args]
        if any(len(value) > 200 for value in args):
            return False, "按钮动作参数过长。"
                ok, message = await self._run_hermes_button_script(entry)
'''
        patched = deploy.patch_adapter_source(source)
        self.assertIn('"maa_action.py"', patched)
        self.assertIn('select_args.get(str(selected_values[0]))', patched)
        self.assertIn('(interaction.data or {}).get("values", [])', patched)
        self.assertEqual(deploy.patch_adapter_source(patched), patched)

    def test_adapter_patch_accepts_a_multiline_native_allowlist(self) -> None:
        # hermes-agent f6b3e394b7（2026-09-03）把 maa_action.py 原生写进多行的
        # frozenset；只看第一行的检测会把它误判成未知基线并拒绝部署。
        already_dispatching = deploy.patch_adapter_source(
            '''_HERMES_BUTTON_SCRIPTS = frozenset({"agent_book_action.py"})
    async def _run_hermes_button_script(
        self,
        entry: dict[str, Any],
    ) -> tuple[bool, str]:
        raw_args = entry.get("args", [])
        if not isinstance(raw_args, list) or len(raw_args) > 10:
            return False, "按钮动作参数无效。"
        args = [str(value) for value in raw_args]
        if any(len(value) > 200 for value in args):
            return False, "按钮动作参数过长。"
                ok, message = await self._run_hermes_button_script(entry)
'''
        ).split("\n", 1)[1]
        native = (
            '_HERMES_BUTTON_SCRIPTS = frozenset({\n'
            '    "agent_book_action.py",\n'
            '    "maa_action.py",\n'
            '    "luxraykit_pr_action.py",\n'
            '})\n' + already_dispatching
        )
        self.assertEqual(deploy.patch_adapter_source(native), native)
        # 真正未知的基线仍然拒绝。
        with self.assertRaises(RuntimeError):
            deploy.patch_adapter_source(
                '_HERMES_BUTTON_SCRIPTS = frozenset({\n    "agent_book_action.py",\n})\n'
                + already_dispatching
            )

    # config.yaml 的键顺序归 Hermes 管：timezone 早于 platforms.discord。
    # 2026-09-04 的事故就是补丁拿 `timezone: Etc/UTC` 当锚点，把 prompt 插进了
    # 上一块 `skills:` 里。
    CONFIG_SAMPLE = (
        "delegation:\n"
        "  max_iterations: 50\n"
        "skills:\n"
        "  creation_nudge_interval: 15\n"
        "timezone: Etc/UTC\n"
        "platforms:\n"
        "  discord:\n"
        "    channel_skill_bindings:\n"
        "      - id: '1531685973501542422'\n"
        "        skills:\n"
        "          - agent-book-tutor\n"
        "    channel_prompts:\n"
        "      '1531685973501542422': '你是领读老师。'\n"
    )

    def test_channel_prompt_lands_in_channel_prompts_whatever_the_key_order(self) -> None:
        patched = deploy.patch_config_yaml_text(self.CONFIG_SAMPLE, "1533066727313637457")
        parsed = deploy.parse_yaml(patched)
        self.assertEqual(parsed["timezone"], "Etc/UTC")
        # 事故的直接症状：prompt 落进了 skills 块。
        self.assertEqual(parsed["skills"], {"creation_nudge_interval": 15})
        discord = parsed["platforms"]["discord"]
        self.assertEqual(
            discord["channel_prompts"]["1533066727313637457"], deploy.MAA_CHANNEL_PROMPT
        )
        self.assertIn(
            {"id": "1533066727313637457", "skills": ["maa-operator"]},
            discord["channel_skill_bindings"],
        )

    def test_channel_prompt_is_not_duplicated_and_old_wording_is_rewritten(self) -> None:
        once = deploy.patch_config_yaml_text(self.CONFIG_SAMPLE, "1533066727313637457")
        self.assertEqual(deploy.patch_config_yaml_text(once, "1533066727313637457"), once)

        # Hermes 重排/折叠过文本以后，只认字面量的旧实现会再插一遍；按解析值判断不会。
        head, tail = deploy.MAA_CHANNEL_PROMPT.split(" ", 1)
        reflowed = once.replace(
            f"      '1533066727313637457': '{deploy.MAA_CHANNEL_PROMPT}'\n",
            f"      '1533066727313637457': {head}\n        {tail}\n",
        )
        self.assertEqual(
            deploy.parse_yaml(reflowed)["platforms"]["discord"]["channel_prompts"][
                "1533066727313637457"
            ],
            deploy.MAA_CHANNEL_PROMPT,
        )
        self.assertEqual(
            deploy.patch_config_yaml_text(reflowed, "1533066727313637457"), reflowed
        )

        stale = self.CONFIG_SAMPLE.replace(
            "      '1531685973501542422': '你是领读老师。'\n",
            "      '1531685973501542422': '你是领读老师。'\n"
            f"      '1533066727313637457': '{deploy.OLD_MAA_CHANNEL_PROMPTS[0]}'\n",
        )
        upgraded = deploy.patch_config_yaml_text(stale, "1533066727313637457")
        self.assertEqual(upgraded.count("'1533066727313637457':"), 1)
        self.assertEqual(
            deploy.parse_yaml(upgraded)["platforms"]["discord"]["channel_prompts"][
                "1533066727313637457"
            ],
            deploy.MAA_CHANNEL_PROMPT,
        )

    def test_unparsable_result_raises_before_the_file_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            config = home / "config.yaml"
            config.write_text(self.CONFIG_SAMPLE, encoding="utf-8")
            real = deploy.parse_yaml
            calls = {"n": 0}

            def flaky(text: str):
                calls["n"] += 1
                if calls["n"] == 1:
                    return real(text)
                raise ValueError("mapping values are not allowed in this context")

            with patch.object(deploy, "HERMES_HOME", home), patch.object(
                deploy, "parse_yaml", side_effect=flaky
            ):
                with self.assertRaises(RuntimeError):
                    deploy.patch_config_yaml("1533066727313637457")
            self.assertEqual(config.read_text(encoding="utf-8"), self.CONFIG_SAMPLE)

    def test_an_already_broken_config_is_refused_instead_of_patched(self) -> None:
        broken = self.CONFIG_SAMPLE.replace(
            "timezone: Etc/UTC\n",
            "      '1533066727313637457': '你是 MAA 长草助手。'\ntimezone: Etc/UTC\n",
        )
        with self.assertRaises(RuntimeError):
            deploy.patch_config_yaml_text(broken, "1533066727313637457")

    def test_channel_id_is_resolved_and_placeholder_never_deploys(self) -> None:
        repo_config = deploy.load_json(deploy.ROOT / "state" / "config.json", {})
        self.assertEqual(repo_config["channel_id"], deploy.CHANNEL_PLACEHOLDER)

        with patch.dict(os.environ, {"MAA_CHANNEL_ID": "424242"}, clear=False):
            self.assertEqual(deploy.resolve_channel_id(), "424242")

        # Falls back to whatever this host already runs, so a redeploy on a
        # configured machine needs no argument.
        with patch.dict(os.environ, {"MAA_CHANNEL_ID": ""}, clear=False), patch.object(
            deploy, "load_json", return_value={"channel_id": "999"}
        ):
            self.assertEqual(deploy.resolve_channel_id(), "999")

        with patch.dict(os.environ, {"MAA_CHANNEL_ID": ""}, clear=False), patch.object(
            deploy, "load_json", return_value={"channel_id": deploy.CHANNEL_PLACEHOLDER}
        ):
            with self.assertRaises(RuntimeError):
                deploy.resolve_channel_id()

    def test_deploy_check_detects_maa_config_content_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "repo.toml"
            target = root / "runtime.toml"
            source.write_bytes(b"same\n")
            target.write_bytes(b"same\n")
            self.assertEqual(deploy.content_mismatches([(source, target)]), [])

            target.write_bytes(b"drifted\n")
            self.assertEqual(
                deploy.content_mismatches([(source, target)]),
                [str(target)],
            )

    def test_deploy_check_skips_daily_toml_but_still_guards_tasks_json(self) -> None:
        # `maa_daily.render_daily_toml()` rewrites the deployed daily.toml before
        # every run, so `--check` must not read that into deployment drift; the
        # user-resource overlay has no such excuse and must stay byte-compared.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            runtime = root / "runtime"
            for relative in deploy.MAA_CONFIG_RELATIVE_PATHS:
                for base in (repo / "config" / "maa-cli", runtime):
                    (base / relative).parent.mkdir(parents=True, exist_ok=True)
                    (base / relative).write_bytes(b"seed\n")

            with patch.object(deploy, "ROOT", repo), patch.object(
                deploy, "MAA_CONFIG_HOME", runtime
            ):
                checked = deploy.maa_config_content_checked_pairs()
                self.assertEqual(deploy.content_mismatches(checked), [])
                # Every config is still deployed and existence-checked.
                self.assertEqual(len(deploy.maa_config_file_pairs()), 7)
                self.assertEqual(len(checked), 6)

                daily = runtime / "tasks" / "daily.toml"
                daily.write_bytes(b"rewritten by render_daily_toml\n")
                self.assertEqual(
                    deploy.content_mismatches(deploy.maa_config_content_checked_pairs()),
                    [],
                )

                overlay = runtime / "resource" / "tasks" / "tasks.json"
                overlay.write_bytes(b"drifted\n")
                self.assertEqual(
                    deploy.content_mismatches(deploy.maa_config_content_checked_pairs()),
                    [str(overlay)],
                )

    def test_deploy_keeps_live_runtime_caches_and_seeds_only_when_absent(self) -> None:
        # activity_stages / the version probes are runtime caches, not config.
        # A stale seed clobbering them re-creates PLAN_NAV_GATE A-3 staleness.
        self.assertEqual(
            deploy.load_json(deploy.ROOT / "state" / "config.json", {})["activity_stages"],
            [],
        )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            hermes = root / "hermes"
            live = hermes / "state" / "maa"
            live.mkdir(parents=True)
            seed = {
                "activity_stages": ["SEED-1"],
                "maa_cli_version": "v0.0.1",
                "maa_core_version": "v0.0.2",
                "retention_days": 14,
                "channel_id": deploy.CHANNEL_PLACEHOLDER,
            }
            repo_state = root / "repo" / "state"
            repo_state.mkdir(parents=True)
            for name in ("defaults.json", "plan.json", "last_run.json"):
                (repo_state / name).write_text("{}", encoding="utf-8")
            (repo_state / "config.json").write_text(
                json.dumps(seed), encoding="utf-8"
            )

            def deploy_once() -> dict:
                with patch.object(deploy, "ROOT", root / "repo"), patch.object(
                    deploy, "HERMES_HOME", hermes
                ):
                    deploy.deploy_state("424242")
                return json.loads((live / "config.json").read_text(encoding="utf-8"))

            # 1. Live host already has its own values -> deploy must not touch them.
            (live / "config.json").write_text(
                json.dumps(
                    {
                        "activity_stages": [],
                        "maa_cli_version": "v0.7.5",
                        "maa_core_version": "v6.17.0-beta.1",
                    }
                ),
                encoding="utf-8",
            )
            result = deploy_once()
            self.assertEqual(result["activity_stages"], [])
            self.assertEqual(result["maa_cli_version"], "v0.7.5")
            self.assertEqual(result["maa_core_version"], "v6.17.0-beta.1")
            # Non-cache keys still deploy normally.
            self.assertEqual(result["retention_days"], 14)
            self.assertEqual(result["channel_id"], "424242")

            # A live non-empty list survives too: "preserve" is not "force empty".
            (live / "config.json").write_text(
                json.dumps({"activity_stages": ["AT-8", "AT-7"]}), encoding="utf-8"
            )
            self.assertEqual(deploy_once()["activity_stages"], ["AT-8", "AT-7"])

            # 2. First boot: no live value -> the seed is the fallback.
            (live / "config.json").unlink()
            self.assertEqual(deploy_once()["activity_stages"], ["SEED-1"])

    def test_every_known_runtime_cache_key_is_preserved_on_deploy(self) -> None:
        """DEV_GUIDE §8 不变量 #25 — the checklist that catches a forgotten key.

        This list is maintained by hand on purpose. When you add a config key
        that a live process writes back, this test fails until you also add it
        to `deploy.PRESERVED_RUNTIME_CACHE_KEYS` — which is exactly the reminder
        that was missing when a deploy clobbered `activity_stages` on
        2026-09-02. If a key here is genuinely repo-owned, delete it from the
        list and say why in the commit.
        """
        known_runtime_caches = {
            # written by maa_plan_card's version probe
            "maa_core_version",
            "maa_cli_version",
            # written by maa_activity_watch / maa_plan_card's activity snapshot
            "activity_stages",
        }
        preserved = set(deploy.PRESERVED_RUNTIME_CACHE_KEYS)
        self.assertEqual(
            known_runtime_caches - preserved,
            set(),
            "运行时缓存键漏进 deploy.PRESERVED_RUNTIME_CACHE_KEYS，部署会用种子盖掉线上真值",
        )
        # The reverse too: preserving a key the repo really owns would silently
        # freeze it on every host that already has a value.
        self.assertEqual(preserved - known_runtime_caches, set())
        # No duplicates — the tuple is the single source of truth.
        self.assertEqual(
            len(deploy.PRESERVED_RUNTIME_CACHE_KEYS), len(preserved)
        )
        # Every preserved key must actually exist in the seed, otherwise it is
        # a typo that would silently never match.
        seed = deploy.load_json(deploy.ROOT / "state" / "config.json", {})
        for key in deploy.PRESERVED_RUNTIME_CACHE_KEYS:
            self.assertIn(key, seed, f"{key} 不在仓库种子里，多半是拼错了")

    def test_parse_maa_versions_from_cli_output(self) -> None:
        cli, core = maa_plan_card.parse_maa_versions(
            "maa-cli v0.7.5\nMaaCore v6.17.0-beta.1\n"
        )
        self.assertEqual(cli, "v0.7.5")
        self.assertEqual(core, "v6.17.0-beta.1")

    def test_update_summary_reports_today_upgrade_and_no_op(self) -> None:
        upgraded = maa_plan_card.format_update_summary(
            {
                "ok": True,
                "changed": True,
                "cli_before": "v0.7.5",
                "cli_after": "v0.7.5",
                "core_before": "v6.17.0-beta.1",
                "core_after": "v6.17.0-beta.4",
            }
        )
        self.assertTrue(upgraded.startswith("今天已更新："))
        self.assertIn("MaaCore v6.17.0-beta.1 → v6.17.0-beta.4", upgraded)
        self.assertIn("maa-cli v0.7.5", upgraded)
        unchanged = maa_plan_card.format_update_summary(
            {
                "ok": True,
                "changed": False,
                "cli_before": "v0.7.5",
                "cli_after": "v0.7.5",
                "core_before": "v6.17.0-beta.1",
                "core_after": "v6.17.0-beta.1",
            }
        )
        self.assertTrue(unchanged.startswith("今天已检查，无新版本"))
        failed = maa_plan_card.format_update_summary(
            {"ok": False, "detail": "failed to connect to github.com"}
        )
        self.assertTrue(failed.startswith("今天检查失败："))

    def test_plan_card_embed_includes_today_update_field(self) -> None:
        card = maa_plan_card.build_embed(
            maa_common.normalize_plan({"skip": False}),
            {},
            "am",
            {
                "ok": True,
                "changed": True,
                "summary": "今天已更新：MaaCore v6.17.0-beta.1 → v6.17.0-beta.4",
            },
        )
        field = next(item for item in card["fields"] if item["name"] == "上游更新")
        self.assertIn("今天已更新", field["value"])
        self.assertEqual(card["title"], "🌱 MAA 今日计划 · 早场 08:00")
        # 晚场卡不跟随上游更新（D1），所以它连这个字段都没有。
        evening = maa_plan_card.build_embed(
            maa_common.normalize_plan({}), {}, "pm"
        )
        self.assertEqual(evening["title"], "🌱 MAA 今日计划 · 晚场 22:00")
        self.assertNotIn("上游更新", [item["name"] for item in evening["fields"]])

    def test_failed_upstream_update_is_recorded_and_alerted(self) -> None:
        at = datetime(2026, 8, 8, 7, 50, tzinfo=BEIJING)
        calls = {"version": 0}

        def fake_run(argv, **_kwargs):
            if argv[:2] == ["/maa", "version"]:
                calls["version"] += 1
                return SimpleNamespace(
                    returncode=0,
                    stdout="maa-cli v0.7.5\nMaaCore v6.17.0-beta.1\n",
                    stderr="",
                )
            if argv[:2] == ["/opt/homebrew/bin/brew", "outdated"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[:2] == ["/maa", "update"]:
                return SimpleNamespace(
                    returncode=1, stdout="", stderr="network down"
                )
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as raw:
            config_path = Path(raw) / "config.json"
            maa_common.atomic_write_json(config_path, {})
            with patch.object(maa_plan_card, "now_beijing", return_value=at), patch.object(
                maa_plan_card, "CONFIG_PATH", config_path
            ), patch.object(maa_plan_card.shutil, "which", return_value="/opt/homebrew/bin/brew"), patch.object(
                maa_plan_card.subprocess,
                "run",
                side_effect=fake_run,
            ), patch.object(maa_plan_card, "merge_last_run") as merge, patch.object(
                maa_plan_card, "discord_request"
            ) as request:
                result = maa_plan_card.check_and_update_upstream(
                    "token",
                    "123",
                    {"maa_bin": "/maa", "resource_update_timeout": 30},
                )
        self.assertFalse(result["ok"])
        self.assertTrue(result["summary"].startswith("今天检查失败："))
        self.assertEqual(merge.call_args.args[0]["resource_update_status"], "failed")
        self.assertEqual(request.call_args.args[1:3], ("POST", "/channels/123/messages"))
        self.assertGreaterEqual(calls["version"], 2)

    def test_daily_upstream_check_keeps_proxy_and_reports_core_upgrade(self) -> None:
        at = datetime(2026, 8, 19, 7, 50, tzinfo=BEIJING)
        versions = iter(
            [
                "maa-cli v0.7.5\nMaaCore v6.17.0-beta.1\n",
                "maa-cli v0.7.5\nMaaCore v6.17.0-beta.4\n",
            ]
        )
        seen_env: list[dict[str, str]] = []

        def fake_run(argv, **kwargs):
            seen_env.append(kwargs.get("env") or {})
            if argv[:2] == ["/maa", "version"]:
                return SimpleNamespace(
                    returncode=0, stdout=next(versions), stderr=""
                )
            if argv[:2] == ["/opt/homebrew/bin/brew", "outdated"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="Updated tap")
            if argv[:2] == ["/maa", "update"]:
                self.assertTrue(argv[-1] == "--batch" or "--batch" in argv)
                return SimpleNamespace(
                    returncode=0, stdout="updated maa-core", stderr=""
                )
            if argv[:2] == ["/maa", "hot-update"]:
                self.assertIn("--batch", argv)
                return SimpleNamespace(
                    returncode=0, stdout="updated hot resources", stderr=""
                )
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as raw:
            config_path = Path(raw) / "config.json"
            maa_common.atomic_write_json(
                config_path, {"maa_bin": "/maa", "maa_disable_proxy": True}
            )
            with patch.dict(
                os.environ,
                {"HTTPS_PROXY": "http://127.0.0.1:7890"},
                clear=False,
            ), patch.object(maa_plan_card, "now_beijing", return_value=at), patch.object(
                maa_plan_card, "CONFIG_PATH", config_path
            ), patch.object(
                maa_plan_card.shutil, "which", return_value="/opt/homebrew/bin/brew"
            ), patch.object(
                maa_plan_card.subprocess, "run", side_effect=fake_run
            ), patch.object(maa_plan_card, "merge_last_run") as merge, patch.object(
                maa_plan_card, "discord_request"
            ) as request:
                result = maa_plan_card.check_and_update_upstream(
                    "token",
                    "123",
                    {
                        "maa_bin": "/maa",
                        "maa_config_dir": "/tmp/maa-config",
                        "maa_disable_proxy": True,
                        "resource_update_timeout": 30,
                    },
                )
            persisted = maa_common.load_json(config_path, {})
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertIn("今天已更新", result["summary"])
        self.assertIn("v6.17.0-beta.1 → v6.17.0-beta.4", result["summary"])
        self.assertEqual(merge.call_args.args[0]["resource_update_status"], "ok")
        request.assert_not_called()
        self.assertEqual(persisted.get("maa_core_version"), "v6.17.0-beta.4")
        update_envs = [
            env for env in seen_env if env.get("MAA_CONFIG_DIR") == "/tmp/maa-config"
        ]
        self.assertTrue(update_envs)
        self.assertGreaterEqual(len(update_envs), 2)
        self.assertTrue(all("HTTPS_PROXY" in env for env in update_envs))

    def test_hot_resource_failure_fails_the_upstream_check(self) -> None:
        def fake_run(argv, **_kwargs):
            if argv[:2] == ["/maa", "version"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout="maa-cli v0.7.5\nMaaCore v6.17.0-beta.6\n",
                    stderr="",
                )
            if argv[:2] == ["/opt/homebrew/bin/brew", "outdated"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[:2] == ["/maa", "update"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[:2] == ["/maa", "hot-update"]:
                return SimpleNamespace(
                    returncode=1, stdout="", stderr="missing resource"
                )
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as raw:
            config_path = Path(raw) / "config.json"
            maa_common.atomic_write_json(config_path, {})
            with patch.object(
                maa_plan_card, "CONFIG_PATH", config_path
            ), patch.object(
                maa_plan_card.shutil, "which", return_value="/opt/homebrew/bin/brew"
            ), patch.object(
                maa_plan_card.subprocess, "run", side_effect=fake_run
            ), patch.object(maa_plan_card, "merge_last_run"), patch.object(
                maa_plan_card, "discord_request"
            ):
                result = maa_plan_card.check_and_update_upstream(
                    "token", "123", {"maa_bin": "/maa"}
                )
        self.assertFalse(result["ok"])
        self.assertIn("missing resource", result["detail"])

    def test_outdated_maa_cli_is_upgraded_via_brew(self) -> None:
        argv_log: list[list[str]] = []

        def fake_run(argv, **_kwargs):
            argv_log.append(list(argv))
            if argv[:2] == ["/maa", "version"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout="maa-cli v0.7.5\nMaaCore v6.17.0-beta.1\n",
                    stderr="",
                )
            if argv[:2] == ["/opt/homebrew/bin/brew", "outdated"]:
                return SimpleNamespace(
                    returncode=0, stdout="maa-cli", stderr=""
                )
            if argv[:2] == ["/opt/homebrew/bin/brew", "upgrade"]:
                return SimpleNamespace(returncode=0, stdout="upgraded maa-cli", stderr="")
            if argv[:2] == ["/maa", "update"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[:2] == ["/maa", "hot-update"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as raw:
            config_path = Path(raw) / "config.json"
            maa_common.atomic_write_json(config_path, {})
            with patch.object(maa_plan_card, "CONFIG_PATH", config_path), patch.object(
                maa_plan_card.shutil, "which", return_value="/opt/homebrew/bin/brew"
            ), patch.object(
                maa_plan_card.subprocess, "run", side_effect=fake_run
            ), patch.object(maa_plan_card, "merge_last_run"), patch.object(
                maa_plan_card, "discord_request"
            ):
                result = maa_plan_card.check_and_update_upstream(
                    "token", "123", {"maa_bin": "/maa"}
                )
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertTrue(
            any(item[:2] == ["/opt/homebrew/bin/brew", "upgrade"] for item in argv_log)
        )


# 2026-08-07 早场 TO-9 的真实日志骨架：代理指挥失误连着四批，理智全额返还，
# 而 MaaCore 照样判 Fight Completed 并把返还的理智汇总成 `total drops`。
PROXY_WIPEOUT_LOG = [
    "StartUp Completed",
    "Fight Start",
    "Current sanity: 115/207",
    "Mission started (1 times, use 105 sanity)",
    "Drops: 理智 × 105",
    "Current sanity: 116/207",
    "Mission started (5 times, use 105 sanity)",
    "Drops: 理智 × 105",
    "Current sanity: 116/207",
    "Mission started (5 times, use 105 sanity)",
    "Drops: 理智 × 105",
    "Current sanity: 117/207",
    "Fight Completed",
    "Fight TO-9 16 times, drops:",
    "total drops: 理智 × 315",
]


# 2026-08-08 早场的真实日志，一字不改：MaaTouch 没在 MaaCore 的 3 秒窗口内握上手，
# connect 判失败，maa-cli 却照样 start()，StartUpTask 直接 SIGSEGV，35 秒退 1。
CONNECT_CRASH_LOG = [
    "Updating hot update files...",
    "Hot update completed successfully",
    "Connected",
    "TouchModeNotAvailable",
    "StartUp Start",
]


class ConnectFailureTests(unittest.TestCase):
    def test_the_2026_08_08_morning_crash_is_recognised(self) -> None:
        self.assertEqual(
            maa_daily.connect_failed(CONNECT_CRASH_LOG, 1), "TouchModeNotAvailable"
        )

    def test_a_run_that_completed_any_task_is_never_retried(self) -> None:
        # 安全闸门：只要有任务真跑过，重开一趟就会重复已经做完的事。
        lines = CONNECT_CRASH_LOG + ["StartUp Completed", "Infrast Error"]
        self.assertEqual(maa_daily.connect_failed(lines, 1), "")
        self.assertEqual(
            maa_daily.connect_failed(CONNECT_CRASH_LOG + ["AllTasksCompleted"], 1), ""
        )

    def test_our_own_return_codes_are_not_connection_failures(self) -> None:
        for code in (0, 124, maa_daily.PROXY_BREAK_CODE, 130):
            self.assertEqual(maa_daily.connect_failed(CONNECT_CRASH_LOG, code), "", code)

    def test_an_ordinary_failure_without_the_marker_is_not_retried(self) -> None:
        lines = ["Connected", "FastestWayToScreencap RawByNc 108", "StartUp Start"]
        self.assertEqual(maa_daily.connect_failed(lines, 1), "")

    def test_summary_names_the_real_culprit_instead_of_blaming_the_game(self) -> None:
        summary = maa_daily.extract_summary(
            CONNECT_CRASH_LOG,
            [],
            plan=maa_common.normalize_plan({"stage": "TO-9"}),
            ok=False,
            home_verified=False,
            home_verify_detail="识别到 0/8 个主页标识",
        )
        self.assertIn("🚀 登录与首登领取：❌ 中断（没连上模拟器（TouchModeNotAvailable）", summary)
        self.assertIn("一个键都没点", summary)

    def test_report_note_explains_the_retries(self) -> None:
        payload = maa_daily.report_payload(
            ok=True,
            phase="am",
            plan=maa_common.normalize_plan({"stage": "TO-9"}),
            duration=900,
            summary="",
            screenshot_ok=True,
            screenshot_error="",
            cleanup_errors=[],
            home_verified=True,
            connect_retries=1,
        )
        note = next(f for f in payload["embeds"][0]["fields"] if f["name"] == "备注")
        self.assertIn("已重开 1 趟", note["value"])

    def test_no_note_when_the_first_attempt_connected(self) -> None:
        payload = maa_daily.report_payload(
            ok=True,
            phase="am",
            plan=maa_common.normalize_plan({"stage": "TO-9"}),
            duration=900,
            summary="",
            screenshot_ok=True,
            screenshot_error="",
            cleanup_errors=[],
            home_verified=True,
        )
        self.assertFalse(
            [f for f in payload["embeds"][0]["fields"] if f["name"] == "备注"]
        )


class ConnectRetryTests(unittest.TestCase):
    def _resilient(self, attempts: list[tuple[int, list[str], list[str]]], config: dict):
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "run.log"
            with (
                patch.object(maa_daily, "run_maa", side_effect=attempts) as runner,
                patch.object(maa_daily.time, "sleep"),
            ):
                result = maa_daily.run_maa_resilient(
                    {"maa_run_timeout": 3000, **config},
                    log_path,
                    "token",
                    "1",
                    "2",
                    maa_common.normalize_plan({"stage": "TO-9"}),
                )
            self.runner = runner
            self.log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        return result

    def test_a_connect_crash_is_retried_and_the_second_attempt_stands(self) -> None:
        code, out, err, retries = self._resilient(
            [
                (1, CONNECT_CRASH_LOG, []),
                (0, ["StartUp Completed", "Fight Completed", "Custom Completed"], []),
            ],
            {},
        )
        self.assertEqual((code, retries), (0, 1))
        # 两趟的日志都留着，第二趟必须是追加而不是覆盖。
        self.assertIn("TouchModeNotAvailable", out)
        self.assertIn("Fight Completed", out)
        self.assertFalse(self.runner.call_args_list[0].kwargs["append"])
        self.assertTrue(self.runner.call_args_list[1].kwargs["append"])
        self.assertIn("等 8s 重开第 1 趟", "".join(err))
        self.assertIn("重开第 1 趟", self.log_text)

    def test_a_healthy_run_is_never_retried(self) -> None:
        code, _out, err, retries = self._resilient([(0, ["Custom Completed"], [])], {})
        self.assertEqual((code, retries), (0, 0))
        self.assertEqual(self.runner.call_count, 1)
        self.assertEqual(err, [])

    def test_retries_stop_at_the_limit_and_say_so(self) -> None:
        code, _out, err, retries = self._resilient(
            [(1, CONNECT_CRASH_LOG, [])] * 3, {"connect_retry_limit": 2}
        )
        self.assertEqual((code, retries), (1, 2))
        self.assertEqual(self.runner.call_count, 3)
        self.assertIn("重试 2 次仍未连上", "".join(err))

    def test_retrying_can_be_turned_off(self) -> None:
        code, _out, err, retries = self._resilient(
            [(1, CONNECT_CRASH_LOG, [])], {"connect_retry_limit": 0}
        )
        self.assertEqual((code, retries), (1, 0))
        self.assertEqual(self.runner.call_count, 1)
        self.assertIn("重试 0 次仍未连上", "".join(err))

    def test_every_attempt_shares_one_timeout_budget(self) -> None:
        self._resilient(
            [(1, CONNECT_CRASH_LOG, []), (0, ["Custom Completed"], [])],
            {"maa_run_timeout": 600},
        )
        budgets = [call.kwargs["timeout"] for call in self.runner.call_args_list]
        self.assertLessEqual(budgets[1], budgets[0])
        self.assertLessEqual(budgets[0], 600)

    def test_no_retry_when_the_budget_cannot_fit_another_attempt(self) -> None:
        # 预算只剩几十秒时重开一趟毫无意义，直接把结果交出去。
        code, _out, err, retries = self._resilient(
            [(1, CONNECT_CRASH_LOG, [])], {"maa_run_timeout": 30}
        )
        self.assertEqual(retries, 0)
        self.assertEqual(self.runner.call_count, 1)
        self.assertIn("仍未连上", "".join(err))

    def test_the_failed_attempt_feeds_the_next_progress_message(self) -> None:
        self._resilient(
            [(1, CONNECT_CRASH_LOG, []), (0, ["Custom Completed"], [])], {}
        )
        carried = self.runner.call_args_list[1].kwargs["carry_lines"]
        self.assertIn("TouchModeNotAvailable", carried)


class SettleTests(unittest.TestCase):
    def _settle(self, costs: list[float], config: dict) -> tuple[str, float]:
        """用假时钟跑闸门，返回 (告警, 花掉的秒数)。costs 是每次探针的耗时。"""
        clock = {"now": 0.0}
        sequence = list(costs)

        def fake_run(*_args, **_kwargs):
            clock["now"] += sequence.pop(0) if sequence else 0.02
            return SimpleNamespace(returncode=0, stdout="0", stderr="")

        def fake_sleep(seconds: float) -> None:
            clock["now"] += seconds

        with (
            patch.object(maa_daily.subprocess, "run", side_effect=fake_run),
            patch.object(maa_daily.time, "monotonic", side_effect=lambda: clock["now"]),
            patch.object(maa_daily.time, "sleep", side_effect=fake_sleep),
        ):
            warning = maa_daily.settle_android_runtime({"adb_path": "/adb", **config})
        return warning, clock["now"]

    def test_a_quiet_emulator_is_handed_over_after_one_quiet_window(self) -> None:
        warning, spent = self._settle([0.03] * 40, {})
        self.assertEqual(warning, "")
        self.assertLess(spent, 12)

    def test_a_stall_restarts_the_quiet_window(self) -> None:
        # 2026-08-08 实测的形状：安静 4 秒 → 突然卡 2.8 秒 → 恢复。卡顿必须把安静
        # 计时清零，否则闸门会在 8 秒刚过时开门，正好把场子交在抖动上。
        warning, spent = self._settle([0.03] * 8 + [2.8] + [0.03] * 60, {})
        self.assertEqual(warning, "")
        self.assertGreater(spent, 15)

    def test_a_permanently_busy_emulator_warns_but_does_not_block(self) -> None:
        warning, _spent = self._settle([2.0] * 200, {"emulator_settle_timeout": 20})
        self.assertIn("未在 20s 内稳定", warning)

    def test_the_gate_can_be_turned_off(self) -> None:
        self.assertEqual(self._settle([], {"emulator_settle_quiet_seconds": 0}), ("", 0.0))
        self.assertEqual(self._settle([], {"emulator_settle_timeout": 0}), ("", 0.0))


class ProxyFailureTests(unittest.TestCase):
    def test_returned_sanity_is_not_a_drop(self) -> None:
        self.assertEqual(maa_daily._drop_pairs(PROXY_WIPEOUT_LOG), [])
        self.assertEqual(maa_daily.proxy_failures(PROXY_WIPEOUT_LOG), (3, 315, 3))
        self.assertEqual(maa_daily.fight_wiped_out(PROXY_WIPEOUT_LOG), (3, 315))

    def test_wipeout_turns_fight_red_despite_maacore_reporting_completed(self) -> None:
        plan = maa_common.normalize_plan({"stage": "TO-9"})
        summary = maa_daily.extract_summary(PROXY_WIPEOUT_LOG, [], plan=plan, ok=False)
        self.assertIn("⚔️ 刷理智：❌ 未完成", summary)
        self.assertIn("实际完成 0 次", summary)
        self.assertIn("代理指挥失误 3 批，理智已返还 ×315", summary)
        # MaaCore 汇总出来的 16 次和「掉落」都是假的，一个都不许出现在战报里。
        self.assertNotIn("16 次", summary)
        self.assertNotIn("掉落", summary)

    def test_real_drops_keep_fight_green_and_report_partial_failures(self) -> None:
        lines = [
            "Fight Start",
            "Drops: 理智 × 105",
            "Drops: 固源岩 × 8, 龙门币 × 2160",
            "Fight Completed",
        ]
        self.assertEqual(maa_daily.proxy_failures(lines), (1, 105, 0))
        self.assertEqual(maa_daily.fight_wiped_out(lines), (0, 0))
        plan = maa_common.normalize_plan({"stage": "TO-9"})
        summary = maa_daily.extract_summary(lines, [], plan=plan, ok=True)
        self.assertIn("⚔️ 刷理智：✅ 完成", summary)
        self.assertIn("代理指挥失误 1 批，理智已返还 ×105", summary)
        self.assertIn("固源岩 ×8", summary)
        self.assertNotIn("理智 ×105、", summary)

    def test_streak_resets_after_a_successful_batch(self) -> None:
        lines = [
            "Drops: 理智 × 105",
            "Drops: 理智 × 105",
            "Drops: 固源岩 × 8",
            "Drops: 理智 × 105",
        ]
        # 熔断只看末尾连续段：中间打成一批就重新计数，不因随机性误伤。
        self.assertEqual(maa_daily.proxy_failures(lines), (3, 315, 1))

    def test_healthy_run_reports_no_proxy_failure(self) -> None:
        lines = [
            "Fight Start",
            "Drops: 固源岩 × 8, 龙门币 × 2160",
            "Fight Completed",
            "total drops: 固源岩 × 8, 龙门币 × 2160",
        ]
        self.assertEqual(maa_daily.proxy_failures(lines), (0, 0, 0))
        self.assertEqual(maa_daily.fight_wiped_out(lines), (0, 0))
        self.assertEqual(
            maa_daily._drop_pairs(lines), [("固源岩", 8), ("龙门币", 2160)]
        )

    def test_summary_only_total_line_still_detects_the_refund(self) -> None:
        lines = ["Fight Completed", "total drops: 理智 × 210"]
        self.assertEqual(maa_daily.proxy_failures(lines), (1, 210, 1))

    def test_progress_hides_the_inflated_count_and_warns(self) -> None:
        progress = maa_daily.progress_content(
            PROXY_WIPEOUT_LOG, plan=maa_common.normalize_plan({"stage": "TO-9"})
        )
        self.assertIn("⚠️ 代理指挥失误 3 批，理智已返还 ×315", progress)
        self.assertNotIn("已完成作战", progress)

    def test_report_note_tells_the_user_to_refresh_the_recording(self) -> None:
        payload = maa_daily.report_payload(
            ok=False,
            phase="am",
            plan=maa_common.normalize_plan({"stage": "TO-9"}),
            duration=877,
            summary="",
            screenshot_ok=True,
            screenshot_error="",
            cleanup_errors=[],
            home_verified=True,
            proxy_batches=3,
            proxy_refunded=315,
        )
        note = next(f for f in payload["embeds"][0]["fields"] if f["name"] == "备注")
        self.assertIn("代理指挥失误 3 批", note["value"])
        self.assertIn("三星", note["value"])


class FakeMaaProcess:
    """够 run_maa 用的 Popen 替身：吐完预设日志后一直挂着，等熔断来终止。"""

    def __init__(self, stdout_text: str) -> None:
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO("")
        self.terminated = False

    def poll(self) -> int | None:
        return 0 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


class CircuitBreakerTests(unittest.TestCase):
    def _run(self, stdout_text: str, config: dict) -> tuple[int, list[str], list[str]]:
        process = FakeMaaProcess(stdout_text)
        with tempfile.TemporaryDirectory() as raw:
            with (
                patch.object(maa_daily.subprocess, "Popen", return_value=process),
                patch.object(maa_daily, "discord_request"),
                patch.object(maa_daily, "PROGRESS_INTERVAL_SECONDS", 0.05),
                patch.object(maa_daily, "MIN_RUN_TIMEOUT_SECONDS", 1),
                patch.object(maa_daily, "maa_subprocess_env", return_value={}),
            ):
                result = maa_daily.run_maa(
                    {"maa_run_timeout": 60, **config},
                    Path(raw) / "run.log",
                    "token",
                    "1",
                    "2",
                    maa_common.normalize_plan({"stage": "TO-9"}),
                )
        self.process = process
        return result

    def test_two_consecutive_proxy_failures_abort_the_run(self) -> None:
        code, _out, err = self._run(
            "Fight Start\nDrops: 理智 × 105\nDrops: 理智 × 105\n",
            {"proxy_failure_limit": 2},
        )
        self.assertEqual(code, maa_daily.PROXY_BREAK_CODE)
        self.assertTrue(self.process.terminated)
        self.assertIn("连续 2 批代理指挥失误", err[-1])

    def test_a_single_failure_does_not_trip_the_breaker(self) -> None:
        # 只有一批失误时不该中止；用 1 秒超时让这一趟自然收尾。
        code, _out, err = self._run(
            "Fight Start\nDrops: 理智 × 105\nDrops: 固源岩 × 8\n",
            {"proxy_failure_limit": 2, "maa_run_timeout": 1},
        )
        self.assertEqual(code, 124)
        self.assertNotIn("代理指挥失误", "".join(err))

    def test_breaker_can_be_disabled(self) -> None:
        code, _out, err = self._run(
            "Drops: 理智 × 105\nDrops: 理智 × 105\nDrops: 理智 × 105\n",
            {"proxy_failure_limit": 0, "maa_run_timeout": 1},
        )
        self.assertEqual(code, 124)
        self.assertNotIn("代理指挥失误", "".join(err))


class SalvageTests(unittest.TestCase):
    def test_salvage_plan_drops_fight_and_keeps_the_rest(self) -> None:
        plan = maa_common.normalize_plan({"stage": "TO-9"})
        retry = maa_daily.salvage_plan(plan)
        assert retry is not None
        self.assertFalse(retry["tasks"]["fight"])
        self.assertTrue(retry["tasks"]["infrast"])
        self.assertNotIn("Fight", maa_daily.render_daily_toml(retry, {}))
        self.assertIn("基建换班", maa_daily.render_daily_toml(retry, {}))
        # 原计划必须原封不动，它还要进快照和战报。
        self.assertTrue(plan["tasks"]["fight"])

    def test_salvage_plan_is_none_when_only_fight_was_enabled(self) -> None:
        plan = maa_common.normalize_plan({"stage": "TO-9"})
        plan["tasks"] = {
            "fight": True,
            "infrast": False,
            "recruit": False,
            "mall": False,
            "award": False,
        }
        self.assertIsNone(maa_daily.salvage_plan(plan))

    def test_salvage_is_skipped_when_the_time_budget_is_spent(self) -> None:
        with patch.object(maa_daily, "run_maa") as runner:
            code, out, err = maa_daily._salvage_run(
                config={"maa_run_timeout": 3000},
                plan=maa_common.normalize_plan({"stage": "TO-9"}),
                task_path=Path("/nonexistent/daily.toml"),
                log_path=Path("/nonexistent/salvage.log"),
                token="t",
                thread_id="1",
                progress_message_id="2",
                carry_lines=[],
                elapsed=2900,
            )
        runner.assert_not_called()
        self.assertIsNone(code)
        self.assertIn("跳过补跑", err[0])

    def test_salvage_can_be_turned_off(self) -> None:
        with patch.object(maa_daily, "run_maa") as runner:
            code, _out, err = maa_daily._salvage_run(
                config={"proxy_failure_salvage": False},
                plan=maa_common.normalize_plan({"stage": "TO-9"}),
                task_path=Path("/nonexistent/daily.toml"),
                log_path=Path("/nonexistent/salvage.log"),
                token="t",
                thread_id="1",
                progress_message_id="2",
                carry_lines=[],
                elapsed=0,
            )
        runner.assert_not_called()
        self.assertIsNone(code)
        self.assertEqual(err, [])

    def test_salvage_reruns_the_remaining_chain_within_the_leftover_budget(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_path = Path(raw) / "daily.toml"
            with patch.object(
                maa_daily, "run_maa", return_value=(0, ["Infrast Completed"], [])
            ) as runner:
                code, out, err = maa_daily._salvage_run(
                    config={"maa_run_timeout": 3000},
                    plan=maa_common.normalize_plan({"stage": "TO-9"}),
                    task_path=task_path,
                    log_path=Path(raw) / "salvage.log",
                    token="t",
                    thread_id="1",
                    progress_message_id="2",
                    carry_lines=["Fight Start"],
                    elapsed=600,
                )
            self.assertEqual(code, 0)
            self.assertEqual(out, ["Infrast Completed"])
            self.assertIn("已补跑剩余任务", err[0])
            self.assertEqual(runner.call_args.kwargs["timeout"], 2400)
            self.assertEqual(runner.call_args.kwargs["carry_lines"], ["Fight Start"])
            # 补跑用的 daily.toml 必须已经落盘，且不含 Fight。
            self.assertNotIn("Fight", task_path.read_text(encoding="utf-8"))


    def test_summary_merges_the_break_and_the_salvage_run(self) -> None:
        broken = [
            "StartUp Completed",
            "Fight Start",
            "Current sanity: 115/207",
            "Drops: 理智 × 105",
            "Current sanity: 116/207",
            "Drops: 理智 × 105",
        ]
        salvaged = [
            "StartUp Completed",
            "Infrast Start",
            "EnterFacility Mfg #0",
            "Infrast Completed",
            "Recruit Start",
            "Recruited 4 times",
            "Recruit Completed",
            "Mall Completed",
            "Award Completed",
            "Custom Completed",
        ]
        summary = maa_daily.extract_summary(
            broken + salvaged,
            [],
            plan=maa_common.normalize_plan({"stage": "TO-9"}),
            ok=False,
            home_verified=True,
        )
        # 刷理智照旧翻红，但补跑回来的四件事必须记成完成。
        self.assertIn("⚔️ 刷理智：❌ 未完成", summary)
        self.assertIn("代理指挥失误 2 批，理智已返还 ×210", summary)
        self.assertIn("🏭 基建换班：✅ 完成", summary)
        self.assertIn("🎫 公开招募：✅ 完成（招募 4 次）", summary)
        self.assertIn("🛒 信用商店：✅ 完成", summary)
        self.assertIn("🎁 领取奖励：✅ 完成", summary)
        self.assertIn("🏠 返回主界面：✅ 完成", summary)


# 2026-09-09 08:02 早场的真实汇总段：SR-8 代理指挥未解锁，Fight 一颗理智没花就 Error，
# MaaCore 接着把其余链条跑完了。`farming_fallback_stage = SR-7` 躺在计划里没人用。
DRY_FIGHT_ERROR_LOG = [
    "StartUp Completed",
    "Fight Start",
    "Current sanity: 116/207",
    "Mission started (1 times, use 25 sanity)",
    "Drops: 初级作战记录 × 9, 合成玉 × 320, 声望 × 240, 理智 × 1, 龙门币 × 240",
    "Fight Completed",
    "Fight Start",
    "Fight Error",
    "Infrast Completed",
    "Recruit Completed",
    "Mall Completed",
    "Award Completed",
    "Custom Completed",
    "[启动国服] 08:01:08 - 08:01:39 (30s) Completed",
    "[每日剿灭] 08:01:39 - 08:02:06 (26s) Completed",
    "Fight 拉特兰 1 times, drops:",
    "1. 初级作战记录 × 9, 合成玉 × 320, 声望 × 240, 理智 × 1, 龙门币 × 240",
    "[刷理智] 08:02:07 - 08:03:03 (56s) Error",
    "[基建换班] 08:03:04 - 08:11:05 (8m 1s) Completed",
    "[公开招募] 08:11:06 - 08:12:01 (55s) Completed",
    "[信用商店] 08:12:02 - 08:13:53 (1m 51s) Completed",
    "[领取奖励] 08:13:54 - 08:14:28 (34s) Completed",
    "[返回主界面] 08:14:29 - 08:14:29 (0s) Completed",
    "Error: Some error occurred during running task!",
]


class FightFallbackTests(unittest.TestCase):
    """主关空跑失败 → 改刷后备关（2026-09-09 早场 SR-8 的教训）。"""

    def _plan(self, **extra):
        return maa_common.normalize_plan({
            "stage": "SR-8", "farming_fallback_stage": "SR-7", "stage_source": "auto", **extra,
        })

    def test_the_real_morning_log_reads_as_a_dry_failure(self) -> None:
        self.assertTrue(maa_daily.fight_failed_dry(DRY_FIGHT_ERROR_LOG))

    def test_a_failure_after_real_battles_is_not_dry(self) -> None:
        lines = list(DRY_FIGHT_ERROR_LOG)
        idx = lines.index("[刷理智] 08:02:07 - 08:03:03 (56s) Error")
        lines[idx + 1:idx + 1] = ["Fight SR-8 2 times, drops:", "1. 龙门币 × 400"]
        self.assertFalse(maa_daily.fight_failed_dry(lines))

    def test_a_completed_fight_or_a_second_block_is_not_dry(self) -> None:
        completed = [l.replace("(56s) Error", "(5m) Completed") for l in DRY_FIGHT_ERROR_LOG]
        self.assertFalse(maa_daily.fight_failed_dry(completed))
        with_second = DRY_FIGHT_ERROR_LOG + ["[刷理智·续] 08:20 - 08:30 Completed"]
        self.assertFalse(maa_daily.fight_failed_dry(with_second))
        self.assertFalse(maa_daily.fight_failed_dry(["StartUp Completed"]))

    def test_the_fallback_plan_keeps_only_the_fight_on_the_spare_stage(self) -> None:
        plan = self._plan()
        retry = maa_daily.fallback_fight_plan(plan)
        assert retry is not None
        self.assertEqual(retry["stage"], "SR-7")
        self.assertTrue(retry["tasks"]["fight"])
        for key in ("infrast", "recruit", "mall", "award"):
            self.assertFalse(retry["tasks"][key])
        self.assertFalse(retry["annihilation"])
        toml = maa_daily.render_daily_toml(retry, {})
        self.assertIn('name = "刷理智·续"', toml)
        self.assertIn('stage = "SR-7"', toml)
        self.assertNotIn('name = "刷理智"\n', toml)
        self.assertNotIn("每日剿灭", toml)
        self.assertNotIn("drops", toml)
        self.assertNotIn("基建换班", toml)
        # 原计划原封不动：它还要进快照和战报。
        self.assertEqual(plan["stage"], "SR-8")
        self.assertTrue(plan["tasks"]["infrast"])

    def test_no_fallback_without_a_distinct_spare_stage(self) -> None:
        self.assertIsNone(maa_daily.fallback_fight_plan(self._plan(farming_fallback_stage="")))
        self.assertIsNone(maa_daily.fallback_fight_plan(self._plan(farming_fallback_stage="SR-8")))
        let_way = self._plan()
        let_way["farming"] = False  # 理智让路那一场本来就没有刷理智
        self.assertIsNone(maa_daily.fallback_fight_plan(let_way))
        off = self._plan()
        off["tasks"]["fight"] = False
        self.assertIsNone(maa_daily.fallback_fight_plan(off))

    def test_the_fallback_run_respects_the_switch_and_the_budget(self) -> None:
        for config, elapsed, note in (
            ({"fight_fallback_on_error": False}, 0, ""),
            ({"maa_run_timeout": 3000}, 2900, "跳过后备关补跑"),
        ):
            with self.subTest(config=config), patch.object(maa_daily, "run_maa") as runner:
                code, _out, err = maa_daily._fallback_fight_run(
                    config=config, plan=self._plan(), task_path=Path("/nonexistent/daily.toml"),
                    log_path=Path("/nonexistent/fallback.log"), token="t", thread_id="1",
                    progress_message_id="2", carry_lines=[], elapsed=elapsed,
                )
            runner.assert_not_called()
            self.assertIsNone(code)
            if note:
                self.assertIn(note, err[0])
            else:
                self.assertEqual(err, [])

    def test_the_fallback_run_writes_the_toml_and_reuses_the_leftover_budget(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_path = Path(raw) / "daily.toml"
            with patch.object(
                maa_daily, "run_maa",
                return_value=(0, ["[刷理智·续] 08:20 - 08:30 Completed", "Fight SR-7 3 times, drops:"], []),
            ) as runner:
                code, out, err = maa_daily._fallback_fight_run(
                    config={"maa_run_timeout": 3000}, plan=self._plan(), task_path=task_path,
                    log_path=Path(raw) / "fallback.log", token="t", thread_id="1",
                    progress_message_id="2", carry_lines=["Fight Error"], elapsed=900,
                )
            self.assertEqual(code, 0)
            self.assertEqual(len(out), 2)
            self.assertIn("已改刷后备关 SR-7", err[0])
            self.assertEqual(runner.call_args.kwargs["timeout"], 2100)
            self.assertEqual(runner.call_args.kwargs["carry_lines"], ["Fight Error"])
            self.assertTrue(runner.call_args.args[5]["fallback_run"])
            written = task_path.read_text(encoding="utf-8")
            self.assertIn('stage = "SR-7"', written)
            self.assertNotIn("基建换班", written)

    def test_the_summary_shows_the_dry_failure_and_the_spare_stage_result(self) -> None:
        merged = DRY_FIGHT_ERROR_LOG + [
            "Fight Start",
            "Mission started (3 times, use 63 sanity)",
            "Drops: 龙门币 × 900",
            "Fight Completed",
            "[刷理智·续] 08:20:00 - 08:30:00 (10m) Completed",
            "Fight SR-7 3 times, drops:",
            "1. 龙门币 × 900",
        ]
        summary = maa_daily.extract_summary(
            merged, [], plan=self._plan(), ok=True, home_verified=True,
        )
        self.assertIn("⚔️ 刷理智：❌ 未完成（关卡 SR-8；未消耗理智，已改刷后备关 SR-7", summary)
        self.assertIn("⚔️ 刷理智·续：✅ 完成（关卡 SR-7；3 次", summary)
        # 没补跑时只说「未消耗理智」，不许假装刷了后备关。
        alone = maa_daily.extract_summary(
            DRY_FIGHT_ERROR_LOG, [], plan=self._plan(), ok=False, home_verified=True,
        )
        self.assertIn("关卡 SR-8；未消耗理智；", alone)
        self.assertNotIn("已改刷后备关", alone)


class RetentionTests(unittest.TestCase):
    def test_cleanup_only_removes_old_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runs = Path(raw)
            old = runs / "old.log"
            fresh = runs / "fresh.log"
            old.write_text("old")
            fresh.write_text("fresh")
            at = datetime(2026, 8, 20, tzinfo=BEIJING)
            old_time = (at - timedelta(days=15)).timestamp()
            fresh_time = (at - timedelta(days=1)).timestamp()
            os.utime(old, (old_time, old_time))
            os.utime(fresh, (fresh_time, fresh_time))
            with patch.object(maa_common, "RUNS_DIR", runs):
                removed = maa_common.cleanup_run_files(14, at)
            self.assertEqual(removed, ["old.log"])
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())


# Text columns copied verbatim from ocrbox output on real 1920x1080 captures.
HOME_OCR = "\n".join(
    [
        "透明信笺\t[114,111,117,33]",
        "wado\t[97,583,131,44]",
        "2026/08/03 08:09\t[1216,17,255,66]",
        "TERMINA\t[1382,163,206,47]",
        "终端\t[1384,217,50,28]",
        "理智/207\t[1239,314,120,33]",
        "SANITY\t[1238,345,108,23]",
        "SOUADS\t[1248,495,119,40]",
        "编队\t[1311,538,50,28]",
        "采购中心\t[1244,733,128,42]",
        "公开招募\t[1509,722,103,36]",
        "任务\t[1308,923,47,33]",
        "基建\t[1643,962,55,33]",
        "好友\t[608,867,30,17]",
        "档案\t[605,948,33,22]",
    ]
)
# The 任务 (mission) list page — where a broken return-to-home chain leaves us.
# It also says 好友, which is why a single marker must never be enough.
MISSION_OCR = "\n".join(
    [
        "日常任务\t[826,27,135,53]",
        "台周常任务\t[1057,24,204,60]",
        "主线任务\t[1426,33,131,36]",
        "特勤任务\t[1685,30,142,39]",
        "reauirement通关任意关卡10次\t[733,178,360,30]",
        "使用好友助战干员完成任意关卡5次\t[816,640,503,39]",
        "累计歼灭任意1000名敌兵\t[834,485,354,36]",
    ]
)
SPLASH_OCR = "\n".join(
    [
        "鹰角网络\t[691,399,235,73]",
        "HYPERGRYPH\t[736,462,142,30]",
        "健康游戏忠告：抵制不良游戏，拒绝盗版游戏。\t[114,722,1691,33]",
    ]
)


class HomeScreenshotTests(unittest.TestCase):
    """The screenshot is the deliverable, so it gets verified on its own."""

    def _verify(self, stdout: str, returncode: int = 0) -> tuple[bool | None, str]:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "ocrbox").write_text("#!/bin/sh\n")
            shot = root / "shot.png"
            shot.write_bytes(b"\x89PNG\r\n\x1a\n")
            with patch.object(maa_daily, "SCRIPTS_DIR", root), patch.object(
                maa_daily.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=returncode, stdout=stdout, stderr=""),
            ):
                return maa_daily.verify_home_screenshot(shot)

    def test_real_home_capture_is_accepted(self) -> None:
        verified, detail = self._verify(HOME_OCR)
        self.assertIs(verified, True)
        self.assertIn("终端", detail)
        self.assertIn(f"/{len(maa_daily.HOME_MARKERS)}", detail)

    def test_mission_page_and_splash_are_rejected(self) -> None:
        for sample in (MISSION_OCR, SPLASH_OCR):
            verified, _ = self._verify(sample)
            self.assertIs(verified, False)

    def test_one_incidental_marker_is_not_enough(self) -> None:
        self.assertGreater(maa_daily.HOME_MARKER_MIN, 1)
        verified, detail = self._verify("使用好友助战干员完成任意关卡5次\t[0,0,1,1]")
        self.assertIs(verified, False)
        self.assertIn("好友", detail)

    def test_unreadable_ocr_never_turns_a_good_run_red(self) -> None:
        # None means "cannot tell" and must stay distinct from False.
        self.assertIsNone(self._verify("", returncode=1)[0])
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            shot = root / "shot.png"
            shot.write_bytes(b"\x89PNG\r\n\x1a\n")
            with patch.object(maa_daily, "SCRIPTS_DIR", root):
                verified, detail = maa_daily.verify_home_screenshot(shot)
            self.assertIsNone(verified)
            self.assertIn("ocrbox", detail)

    def test_capture_retries_while_the_home_page_animates_in(self) -> None:
        seen: list[str] = []

        def fake_capture(config: dict[str, object], path: Path) -> tuple[bool, str]:
            seen.append("capture")
            return True, ""

        results = [(False, "识别到 0/8 个主页标识"), (True, "识别到 8/8 个主页标识")]
        with patch.object(maa_daily, "capture_screenshot", fake_capture), patch.object(
            maa_daily, "verify_home_screenshot", lambda path: results.pop(0)
        ), patch.object(maa_daily.time, "sleep", lambda seconds: None):
            ok, error, verified, detail = maa_daily.capture_home_screenshot(
                {"home_screenshot_attempts": 3}, Path("/tmp/shot.png")
            )
        self.assertTrue(ok)
        self.assertEqual(error, "")
        self.assertIs(verified, True)
        self.assertIn("8/8", detail)
        self.assertEqual(len(seen), 2)

    def test_capture_failure_short_circuits_without_ocr(self) -> None:
        with patch.object(
            maa_daily, "capture_screenshot", lambda config, path: (False, "adb 挂了")
        ), patch.object(maa_daily, "verify_home_screenshot", lambda path: (True, "不该被调用")), patch.object(
            maa_daily.time, "sleep", lambda seconds: None
        ):
            ok, error, verified, _ = maa_daily.capture_home_screenshot({}, Path("/tmp/shot.png"))
        self.assertFalse(ok)
        self.assertEqual(error, "adb 挂了")
        self.assertIsNone(verified)

    def test_a_screenshot_that_is_not_home_fails_the_run(self) -> None:
        lines = ["StartUp Completed", "Award Completed", "Custom Start", "Custom Completed"]
        plan = maa_common.normalize_plan({"stage": "1-7"})
        summary = maa_daily.extract_summary(
            lines,
            [],
            plan=plan,
            ok=False,
            home_verified=False,
            home_verify_detail="识别到 0/8 个主页标识",
        )
        self.assertIn("🌟 总体：❌ 未完成", summary)
        self.assertIn("🏠 返回主界面：❌ 未完成（截图不在主页：识别到 0/8 个主页标识）", summary)
        payload = maa_daily.report_payload(
            ok=False,
            phase="am",
            plan=plan,
            duration=60,
            summary=summary,
            screenshot_ok=True,
            screenshot_error="",
            cleanup_errors=[],
            home_verified=False,
            home_verify_detail="识别到 0/8 个主页标识",
        )
        notes = [f for f in payload["embeds"][0]["fields"] if f["name"] == "备注"]
        self.assertEqual(len(notes), 1)
        self.assertIn("截图不是游戏主页", notes[0]["value"])

    def test_a_verified_home_annotates_even_when_maa_errored(self) -> None:
        # Exactly the 2026-08-03 早场 case: the chain errored, the game was home.
        lines = ["StartUp Completed", "Custom Start", "Custom Error"]
        summary = maa_daily.extract_summary(
            lines,
            [],
            plan=maa_common.normalize_plan({"stage": "1-7"}),
            ok=False,
            home_verified=True,
            home_verify_detail="识别到 8/8 个主页标识",
        )
        self.assertIn("🏠 返回主界面：❌ 未完成（截图已确认在主页）", summary)

    def test_home_gate_does_not_touch_copilot(self) -> None:
        # 抄作业 must keep using the plain capture; it never ends on the home page.
        worker = (ROOT / "scripts" / "maa_copilot_worker.py").read_text(encoding="utf-8")
        self.assertIn("capture_screenshot(config, png_path)", worker)
        self.assertNotIn("capture_home_screenshot", worker)
        self.assertNotIn("verify_home_screenshot", worker)

    def test_checked_in_daily_toml_matches_the_renderer(self) -> None:
        toml = (ROOT / "config" / "maa-cli" / "tasks" / "daily.toml").read_text(encoding="utf-8")
        rendered = maa_daily.render_daily_toml(
            maa_common.normalize_plan({"stage": "1-7", "tasks": {}}),
            {"medicine_expire_days": 2, "fight_times": 2147483647, "recruit_times": 4},
        )
        # The checked-in file is the deploy seed plus explanatory comments;
        # every executable line before those comments must exactly match what
        # the runtime renderer will overwrite it with.
        self.assertTrue(toml.startswith(rendered), "daily.toml drifted from runtime renderer")

    def test_infrast_smoke_uses_the_same_safe_automatic_scheduling_policy(self) -> None:
        toml = (ROOT / "config" / "maa-cli" / "tasks" / "infrast-smoke.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn("mode = 0", toml)
        self.assertIn(
            'facility = ["Mfg", "Trade", "Power", "Control", "Reception", "Office", "Dorm"]',
            toml,
        )
        self.assertIn('drones = "_NotUse"', toml)
        self.assertIn("replenish = false", toml)
        self.assertIn("dorm_trust_enabled = false", toml)
        profile = (
            ROOT / "config" / "maa-cli" / "profiles" / "default.toml"
        ).read_text(encoding="utf-8")
        self.assertIn("[resource]", profile)
        self.assertIn("user_resource = true", profile)
        override = json.loads(
            (
                ROOT
                / "config"
                / "maa-cli"
                / "resource"
                / "tasks"
                / "tasks.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            override["InfrastDormDoubleConfirmButton"]["roi"],
            [620, 430, 660, 150],
        )
        self.assertEqual(
            override["InfrastDormDoubleConfirmButton"],
            {
                "template": "PopupConfirm.png",
                "roi": [620, 430, 660, 150],
                "action": "ClickRect",
                "specificRect": [800, 450, 90, 90],
                "postDelay": 1000,
                "next": [
                    "InfrastDormConfirmButtonWait",
                    "InfrastDormDoubleConfirmButton",
                    "Stop",
                ],
            },
        )


class DepotTests(unittest.TestCase):
    def _resource(self, root: Path) -> dict[str, object]:
        maa_common.atomic_write_json(
            root / "item_index.json",
            {
                "30012": {"name": "固源岩组"},
                "30022": {"name": "糖组"},
            },
        )
        return {"maa_resource_dir": str(root)}

    def test_parser_accepts_only_the_final_complete_depot_callback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self._resource(root)
            partial = {
                "taskchain": "Depot",
                "what": "DepotInfo",
                "details": {"done": False, "data": '{"30012": 1}'},
            }
            complete = {
                "taskchain": "Depot",
                "what": "DepotInfo",
                "details": {"done": True, "data": '{"30012": 40, "30022": 9}'},
            }
            output = "\n".join(
                [
                    "Depot: " + json.dumps(partial, ensure_ascii=False, indent=2),
                    "Depot: " + json.dumps(complete, ensure_ascii=False, indent=2),
                ]
            )
            items = maa_depot.parse_depot_output(output, config)
        self.assertEqual(items["30012"], {"name": "固源岩组", "quantity": 40})
        self.assertEqual(items["30022"]["quantity"], 9)

    def test_partial_or_empty_depot_result_is_a_visible_error(self) -> None:
        partial = {
            "taskchain": "Depot",
            "what": "DepotInfo",
            "details": {"done": False, "data": '{"30012": 1}'},
        }
        with self.assertRaisesRegex(RuntimeError, "done=true"):
            maa_depot.parse_depot_output("Depot: " + json.dumps(partial), {})

    def test_drop_delta_and_second_snapshot_produce_consumption_rate(self) -> None:
        first_at = datetime(2026, 8, 1, 7, 50, tzinfo=BEIJING)
        second_at = first_at + timedelta(days=7)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "depot.json"
            config = self._resource(root)
            maa_depot.reconcile(
                {
                    "30012": {"name": "固源岩组", "quantity": 100},
                    "30022": {"name": "糖组", "quantity": 50},
                },
                at=first_at,
                path=path,
            )
            parsed_drops = maa_daily._drop_pairs(
                ["total drops: 固源岩组 ×30, 糖组 ×5, furni ×1"]
            )
            after_drop = maa_depot.record_drops(
                parsed_drops,
                config,
                at=first_at + timedelta(days=1),
                path=path,
            )
            self.assertEqual(after_drop["items"]["30012"]["quantity"], 130)
            self.assertEqual(after_drop["drops_since_snapshot"]["30012"]["quantity"], 30)
            self.assertNotIn("furni", after_drop["drops_since_snapshot"])
            result = maa_depot.reconcile(
                {
                    "30012": {"name": "固源岩组", "quantity": 90},
                    "30022": {"name": "糖组", "quantity": 60},
                },
                at=second_at,
                path=path,
            )
        interval = result["consumption_history"][0]["items"]
        self.assertEqual(interval["30012"]["calculated"], 40)
        self.assertEqual(interval["30012"]["consumed"], 40)
        # 50 + 5 - 60 = -5: an untracked gain is retained for audit, not
        # misrepresented as negative consumption.
        self.assertEqual(interval["30022"]["calculated"], -5)
        self.assertEqual(interval["30022"]["consumed"], 0)
        self.assertAlmostEqual(
            result["consumption_rates"]["30012"]["per_day"], 40 / 7, places=6
        )
        self.assertEqual(result["drops_since_snapshot"], {})

    def test_failed_real_scan_never_calls_the_snapshot_writer(self) -> None:
        context = unittest.mock.MagicMock()
        context.__enter__.return_value = True
        context.__exit__.return_value = False
        failed = SimpleNamespace(returncode=1, stdout="", stderr="recognition failed")
        config = {"maa_bin": "/maa", "adb_path": "/adb"}
        with patch.object(maa_depot, "run_lock", return_value=context), patch.object(
            maa_depot, "emulator_is_online", return_value=(False, "离线")
        ), patch.object(maa_daily, "require_executable"), patch.object(
            maa_daily, "open_emulator"
        ), patch.object(
            maa_daily, "wait_for_android"
        ), patch.object(maa_daily, "verify_game_installed"), patch.object(
            maa_daily, "settle_android_runtime"
        ), patch.object(maa_daily, "close_everything", return_value=[]), patch.object(
            maa_depot.subprocess, "run", return_value=failed
        ), patch.object(maa_depot, "reconcile") as writer:
            with self.assertRaisesRegex(RuntimeError, "recognition failed"):
                maa_depot.run_depot(config)
        writer.assert_not_called()

    def test_weekly_gate_is_due_only_after_seven_days(self) -> None:
        at = datetime(2026, 8, 8, 7, 50, tzinfo=BEIJING)
        state = {"snapshot_at": (at - timedelta(days=6, hours=23)).isoformat()}
        self.assertFalse(maa_depot.is_due(state, at))
        state["snapshot_at"] = (at - timedelta(days=7)).isoformat()
        self.assertTrue(maa_depot.is_due(state, at))

    def test_cron_entry_checks_due_without_starting_depot_early(self) -> None:
        at = datetime(2026, 8, 8, 4, 30, tzinfo=BEIJING)
        with patch.object(maa_depot, "load_config", return_value={}), patch.object(
            maa_depot, "now_beijing", return_value=at
        ), patch.object(
            maa_depot,
            "run_if_due",
            return_value={"status": "not_due", "snapshot_at": "2026-08-02T04:30:00+08:00"},
        ) as due, patch.object(maa_depot, "run_depot") as real_scan, patch.object(
            maa_depot, "merge_last_run"
        ) as merge, patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(maa_depot.main(), 0)
        due.assert_called_once_with({}, at=at)
        real_scan.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(merge.call_args.args[0]["depot_status"], "not_due")

    def test_cron_entry_alerts_on_failure_and_keeps_it_out_of_plan_card(self) -> None:
        at = datetime(2026, 8, 8, 4, 30, tzinfo=BEIJING)
        with patch.object(
            maa_depot, "load_config", return_value={"channel_id": "123"}
        ), patch.object(maa_depot, "now_beijing", return_value=at), patch.object(
            maa_depot, "run_if_due", side_effect=RuntimeError("recognition failed")
        ), patch.object(maa_depot, "merge_last_run") as merge, patch.object(
            maa_depot, "discord_token", return_value="token"
        ), patch.object(maa_depot, "discord_request") as request:
            with self.assertRaisesRegex(RuntimeError, "recognition failed"):
                maa_depot.main()
        self.assertEqual(merge.call_args.args[0]["depot_status"], "failed")
        self.assertEqual(request.call_args.args[1:3], ("POST", "/channels/123/messages"))
        self.assertNotIn("maa_depot", maa_plan_card.__dict__)


class LockConflictTests(unittest.TestCase):
    def test_conflict_is_recorded_without_overwriting_the_active_status(self) -> None:
        at = datetime(2026, 8, 8, 8, 0, tzinfo=BEIJING)
        with patch.object(maa_daily, "merge_last_run") as merge, patch.object(
            maa_daily, "discord_request"
        ) as request:
            maa_daily.report_lock_conflict({}, "token", "123", "am", at)
        record = merge.call_args.args[0]
        self.assertNotIn("status", record)
        self.assertEqual(record["last_lock_conflict_phase"], "am")
        self.assertIn("run.lock", record["last_lock_conflict_reason"])
        payload = request.call_args.args[3]
        self.assertIn("未启动", payload["embeds"][0]["title"])

    def test_daily_lock_loss_reports_and_returns_failure(self) -> None:
        at = datetime(2026, 8, 8, 8, 0, tzinfo=BEIJING)
        context = unittest.mock.MagicMock()
        context.__enter__.return_value = False
        context.__exit__.return_value = False
        with patch.object(maa_daily, "now_beijing", return_value=at), patch.object(
            maa_daily, "load_config", return_value={"channel_id": "123"}
        ), patch.object(maa_daily, "discord_token", return_value="token"), patch.object(
            maa_daily, "load_json", return_value={}
        ), patch.object(
            maa_daily, "effective_plan", return_value=maa_common.normalize_plan({})
        ), patch.object(maa_daily, "run_lock", return_value=context), patch.object(
            maa_daily, "report_lock_conflict"
        ) as report:
            self.assertEqual(maa_daily.execute("am"), 1)
        report.assert_called_once_with(
            {"channel_id": "123"}, "token", "123", "am", at
        )


class EmulatorProbeTests(unittest.TestCase):
    """PLAN_NAV_GATE B: automatic phases must not steal a device in use.

    实现自四期 1d 起在 `maa_common`，四条自动链路共用（D18）；
    这里直接测那一份，`maa_daily.emulator_is_online` 只是同一个对象。
    """

    @staticmethod
    def _completed(returncode: int, stdout: str = "") -> SimpleNamespace:
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    def test_probe_reports_online_from_the_process_signal_alone(self) -> None:
        # A freshly launched BlueStacks has no Android yet, so the process
        # signal must be able to answer "online" without asking ADB at all.
        with patch.object(
            maa_common.subprocess, "run", return_value=self._completed(0, "4242\n")
        ) as run:
            online, detail = maa_common.emulator_is_online({})
        self.assertTrue(online)
        self.assertIn("进程在跑", detail)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][:2], ["pgrep", "-f"])

    def test_probe_reports_online_when_adb_says_boot_completed(self) -> None:
        # A hung process can outlive its device and vice versa, so ADB is the
        # second half of the OR.
        results = [self._completed(1), self._completed(0), self._completed(0, "1\n")]
        with patch.object(maa_common.subprocess, "run", side_effect=results):
            online, detail = maa_common.emulator_is_online({})
        self.assertTrue(online)
        self.assertIn("sys.boot_completed", detail)

    def test_probe_reports_offline_when_both_signals_miss(self) -> None:
        results = [self._completed(1), self._completed(0), self._completed(0, "\n")]
        with patch.object(maa_common.subprocess, "run", side_effect=results):
            online, detail = maa_common.emulator_is_online({})
        self.assertFalse(online)
        self.assertIn("未连上", detail)

    def test_a_broken_probe_falls_back_to_offline_and_never_raises(self) -> None:
        # Grabbing the device once by mistake is far cheaper than a daily that
        # stops forever because the probe itself broke (B-4).
        for failure in (
            maa_common.subprocess.TimeoutExpired(cmd="pgrep", timeout=2),
            OSError("adb missing"),
        ):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(maa_common.subprocess, "run", side_effect=failure):
                    online, detail = maa_common.emulator_is_online({})
                self.assertFalse(online)
                self.assertTrue(detail)

    def test_the_probe_budget_is_bounded_and_disablable(self) -> None:
        # `wait_for_android` 要等 180s、`settle_android_runtime` 是开机后的稳定性探测，
        # 复用任何一个都会撑爆运行预算和不变量 #6。公共层不 import `maa_daily`，
        # 结构上就够不着它们。
        source = (ROOT / "scripts" / "maa_common.py").read_text(encoding="utf-8")
        self.assertNotIn("import maa_daily", source)
        with patch.object(maa_common.subprocess, "run") as run:
            online, detail = maa_common.emulator_is_online({"emulator_probe_timeout": 0})
        self.assertFalse(online)
        self.assertIn("探测已禁用", detail)
        run.assert_not_called()
        timeouts = []
        with patch.object(
            maa_common.subprocess,
            "run",
            side_effect=lambda *a, **k: timeouts.append(k["timeout"])
            or self._completed(1),
        ):
            maa_common.emulator_is_online({})
        self.assertTrue(timeouts)
        self.assertLessEqual(sum(timeouts), 8.0)

    def test_probe_refuses_an_unvalidated_app_name_without_blocking_the_run(self) -> None:
        with patch.object(maa_common.subprocess, "run") as run:
            online, detail = maa_common.emulator_is_online({"emulator_app": "evil; rm -rf /"})
        self.assertFalse(online)
        self.assertIn("非法字符", detail)
        run.assert_not_called()

    def _execute(self, phase: str, *, online: bool, probe=None):
        """Drive execute() far enough to observe the probe branch."""
        at = datetime(2026, 8, 8, 8, 0, tzinfo=BEIJING)
        lock = unittest.mock.MagicMock()
        lock.__enter__.return_value = True
        lock.__exit__.return_value = False
        probe = probe or unittest.mock.MagicMock(return_value=(online, "探测明细"))
        with patch.object(maa_daily, "now_beijing", return_value=at), patch.object(
            maa_daily, "load_config", return_value={"channel_id": "123"}
        ), patch.object(maa_daily, "discord_token", return_value="token"), patch.object(
            maa_daily, "load_json", return_value={}
        ), patch.object(
            maa_daily, "effective_plan", return_value=maa_common.normalize_plan({})
        ), patch.object(maa_daily, "run_lock", return_value=lock), patch.object(
            maa_daily, "cleanup_run_files"
        ), patch.object(maa_daily, "atomic_write_json"), patch.object(
            maa_daily, "should_skip", return_value=False
        ), patch.object(
            maa_daily, "emulator_is_online", probe
        ), patch.object(maa_daily, "merge_last_run") as merge, patch.object(
            maa_daily, "discord_request"
        ) as request, patch.object(
            # 探测在线的汇报走 maa_common 那份通用实现（四期 1d），
            # 它的 Discord / 状态写入必须一起桩掉，否则单测会真的打网络。
            maa_common, "merge_last_run"
        ) as common_merge, patch.object(
            maa_common, "discord_request"
        ) as common_request, patch.object(
            maa_common, "discord_token", return_value="token"
        ), patch.object(maa_daily, "require_executable"), patch.object(
            maa_daily, "open_emulator", side_effect=RuntimeError("STOP: probe passed")
        ) as opened, patch.object(maa_daily, "send_direct_failure"):
            code = maa_daily.execute(phase)
        merge.call_args_list = merge.call_args_list + common_merge.call_args_list
        request.call_args_list = request.call_args_list + common_request.call_args_list
        return SimpleNamespace(
            code=code, merge=merge, request=request, opened=opened, probe=probe
        )

    def test_automatic_phase_skips_loudly_when_the_emulator_is_busy(self) -> None:
        for phase in ("am", "pm"):
            with self.subTest(phase=phase):
                result = self._execute(phase, online=True)
                self.assertEqual(result.code, 0)
                result.opened.assert_not_called()

                # 护栏 4: a skip that nobody can see is a permanent silent stall.
                records = [call.args[0] for call in result.merge.call_args_list]
                skipped = [r for r in records if r.get("skip_reason") == "emulator_busy"]
                self.assertEqual(len(skipped), 1)
                self.assertEqual(skipped[0]["status"], "skipped")
                self.assertEqual(skipped[0]["phase"], phase)
                self.assertEqual(skipped[0]["emulator_probe"], "探测明细")
                self.assertTrue(skipped[0]["finished_at"])

                payloads = [
                    call.args[3]
                    for call in result.request.call_args_list
                    if call.args[1:3] == ("POST", "/channels/123/messages")
                ]
                self.assertEqual(len(payloads), 1)
                embed = payloads[0]["embeds"][0]
                self.assertIn("已跳过", embed["title"])
                self.assertIn("判定你在玩", embed["description"])
                # The way out matters: otherwise a forgotten emulator means the
                # daily can never run again.
                self.assertIn("现在跑", embed["description"])

    def test_automatic_phase_runs_normally_when_the_emulator_is_offline(self) -> None:
        result = self._execute("am", online=False)
        result.probe.assert_called_once()
        result.opened.assert_called_once()

    def test_manual_run_never_defers_to_the_probe(self) -> None:
        # The single easiest thing to get backwards: "now" is the user saying
        # "run it anyway", so the probe must not even be consulted.
        result = self._execute("now", online=True)
        result.probe.assert_not_called()
        result.opened.assert_called_once()

    def test_only_the_cron_phases_are_probe_gated(self) -> None:
        self.assertEqual(maa_daily.AUTO_PHASES, frozenset({"am", "pm"}))
        self.assertNotIn("now", maa_daily.AUTO_PHASES)



class LogTailTests(unittest.TestCase):
    """`log tail` 是聊天命令（「最近怎么了 / 看日志」），计划卡上没有对应按钮。

    四期步骤 1 只删了卡片上的「📋 最近结果」按钮，命令必须还在。
    """

    def test_log_tail_is_on_the_dispatch_allowlist(self) -> None:
        with patch.object(maa_action, "log_tail", return_value=(True, "t")) as tail:
            self.assertEqual(maa_action.dispatch(["log", "tail"]), (True, "t"))
        tail.assert_called_once_with()
        ok, message = maa_action.dispatch(["log"])
        self.assertFalse(ok)
        self.assertIn("log tail", message)

    def test_missing_state_says_so_instead_of_failing(self) -> None:
        with patch.object(maa_action, "load_json", return_value={}):
            ok, message = maa_action.log_tail()
        self.assertTrue(ok)
        self.assertIn("还没有 MAA 执行结果", message)

    def test_translated_summary_is_returned_as_is(self) -> None:
        state = {
            "status": "success",
            "summary": "🌟 总体：✅ 成功\n· 刷理智：1-7 ×3",
            "finished_at": "2026-09-09T08:31:02+08:00",
        }
        with patch.object(maa_action, "load_json", return_value=state):
            ok, message = maa_action.log_tail()
        self.assertTrue(ok)
        self.assertIn("📋 最近一次结果（2026-09-09 08:31:02）", message)
        self.assertIn("🌟 总体：✅ 成功", message)

    def test_legacy_state_is_rebuilt_from_the_log_without_raw_cli(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "run.log"
            log_path.write_text("[Fight] 1-7 raw cli line\n", encoding="utf-8")
            state = {
                "status": "success",
                "summary": "Fight completed",
                "log": str(log_path),
                "plan": {"tasks": {"fight": True}},
            }
            with patch.object(
                maa_action, "load_json", return_value=state
            ), patch.object(
                maa_daily, "extract_summary", return_value="🌟 总体：✅ 成功"
            ) as extract:
                ok, message = maa_action.log_tail()
        self.assertTrue(ok)
        self.assertEqual(extract.call_args.args[0], ["[Fight] 1-7 raw cli line"])
        self.assertEqual(extract.call_args.kwargs["plan"], {"tasks": {"fight": True}})
        self.assertTrue(extract.call_args.kwargs["ok"])
        self.assertIn("🌟 总体：✅ 成功", message)
        self.assertNotIn("raw cli line", message)


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _offline_market(**overrides) -> dict:
    """一份「外部数据全都没有」的视图：单测**永远不许碰网**（DEV_GUIDE §9）。"""
    data = {
        "matrix": [],
        "stages": [],
        "item_values": {},
        "store": None,
        "degraded_reasons": [],
    }
    data.update(overrides)
    return data


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
        self.config = {
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
        config = dict(self.config)
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
        config = dict(self.config)
        config["farming_stage_table"] = {"固源岩组": "1-7", "异铁组": "SR-8"}
        config["farming_weights"] = {"scarcity": 1, "gap": 0, "efficiency": 0}
        scarce_first = maa_farming.farming_result(
            config, depot, available_stages={"SR-8", "1-7"}, at=self.at,
            data=self.market(),
        )
        config = dict(config)
        config["farming_weights"] = {"scarcity": 0, "gap": 1, "efficiency": 0}
        gap_first = maa_farming.farming_result(
            config, depot, available_stages={"SR-8", "1-7"}, at=self.at,
            data=self.market(),
        )
        self.assertEqual(scarce_first["items"][0]["material"], "异铁组")
        self.assertEqual(gap_first["items"][0]["material"], "固源岩组")

    # --- 关卡查不到 / 今天没开 --------------------------------------------
    def test_a_material_without_a_stage_is_reported_not_guessed(self) -> None:
        config = dict(self.config)
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
        config = dict(self.config)
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
        config = dict(self.config)
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
        config = dict(self.config)
        config["activity_stages"] = ["SR-8", "SR-9"]
        stages, _in_activity, _reason = maa_farming.candidate_stages(
            config, self.at,
            activity_cache=self._activity_cache("2026/09/18 03:59:59"),
        )
        self.assertEqual(stages, {"SR-8"})

    def _candidates(self, config: dict | None = None):
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
        config = {**self.config, "farming_stage_table": {}}
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
        self.config = {
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
        broken = {
            "farming_weights": {"scarcity": "x", "gap": -1},
            "farming_min_samples": "nope",
            "farming_target_days": 0,
        }
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
        self.config = {
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

    def _components(self, phase: str = "am"):
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
        config = dict(self.config)
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


class SessionDailyTests(unittest.TestCase):
    """四期 1a：`execute(phase)` 只吃**这一场**的覆盖，`now` 一场都不吃。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.defaults = root / "defaults.json"
        self.plan_path = root / "plan.json"
        maa_common.atomic_write_json(self.defaults, {"stage": "1-7"})
        maa_common.atomic_write_json(
            self.plan_path,
            {
                "date": datetime(2026, 8, 8, tzinfo=BEIJING).date().isoformat(),
                "sessions": {"am": {"stage": "CE-6", "skip": True}},
            },
        )

    def _execute(self, phase: str):
        at = datetime(2026, 8, 8, 8, 0, tzinfo=BEIJING)
        lock = unittest.mock.MagicMock()
        lock.__enter__.return_value = True
        lock.__exit__.return_value = False
        with patch.object(maa_daily, "now_beijing", return_value=at), patch.object(
            maa_daily, "load_config", return_value={"channel_id": "123"}
        ), patch.object(maa_daily, "discord_token", return_value="token"), patch.object(
            maa_daily, "load_json", return_value={}
        ), patch.object(maa_common, "DEFAULTS_PATH", self.defaults), patch.object(
            maa_common, "PLAN_PATH", self.plan_path
        ), patch.object(maa_daily, "run_lock", return_value=lock), patch.object(
            maa_daily, "cleanup_run_files"
        ), patch.object(maa_daily, "atomic_write_json") as snapshot, patch.object(
            maa_daily, "emulator_is_online", return_value=(False, "离线")
        ), patch.object(maa_daily, "merge_last_run"), patch.object(
            maa_daily, "discord_request"
        ) as request, patch.object(maa_daily, "require_executable"), patch.object(
            maa_daily, "open_emulator", side_effect=RuntimeError("STOP: 已越过跳过闸门")
        ) as opened, patch.object(maa_daily, "send_direct_failure"):
            code = maa_daily.execute(phase)
        return SimpleNamespace(
            code=code, opened=opened, request=request, snapshot=snapshot
        )

    def test_a_skipped_morning_does_not_silence_the_evening_or_a_manual_run(self) -> None:
        morning = self._execute("am")
        morning.opened.assert_not_called()
        titles = [
            call.args[3]["embeds"][0]["title"]
            for call in morning.request.call_args_list
            if call.args[1] == "POST"
        ]
        self.assertTrue(any("已跳过" in title for title in titles))
        for phase in ("pm", "now"):
            with self.subTest(phase=phase):
                other = self._execute(phase)
                other.opened.assert_called_once()

    def test_each_phase_snapshots_its_own_stage(self) -> None:
        morning = self._execute("am")
        evening = self._execute("pm")
        # 早场被跳过前也会先落一份快照，晚场则走默认关。
        self.assertEqual(morning.snapshot.call_args.args[1]["plan"]["stage"], "CE-6")
        self.assertEqual(evening.snapshot.call_args.args[1]["plan"]["stage"], "1-7")

    def test_should_skip_never_silences_a_manual_run(self) -> None:
        skipped = maa_common.normalize_plan({"skip": True})
        self.assertTrue(maa_daily.should_skip(skipped, "am"))
        self.assertTrue(maa_daily.should_skip(skipped, "pm"))
        self.assertFalse(maa_daily.should_skip(skipped, "now"))


class PlanCardPhaseTests(unittest.TestCase):
    """四期 1b：早晚两张卡，晚场卡不跟随上游更新（D1）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.buttons = root / "buttons.json"
        self.plan_path = root / "plan.json"
        self.defaults = root / "defaults.json"
        maa_common.atomic_write_json(self.defaults, {"stage": "1-7"})
        maa_common.atomic_write_json(self.buttons, {"hermes:btn:agent-book:x": {}})

    def _main(self, argv: list[str]):
        with patch.object(
            maa_plan_card, "load_config", return_value={"channel_id": "123"}
        ), patch.object(
            maa_plan_card, "discord_token", return_value="token"
        ), patch.object(
            maa_plan_card, "check_and_update_upstream", return_value={"ok": True}
        ) as upstream, patch.object(
            maa_plan_card, "refresh_activity_stages", return_value=(True, "")
        ), patch.object(
            maa_plan_card, "load_depot_state", return_value={}
        ), patch.object(
            # 单测一条都不许碰网（DEV_GUIDE §9）：计划卡这条链会去抓一图流。
            maa_farming, "load_market_data", return_value=_offline_market()
        ), patch.object(
            maa_plan_card, "activity_deadline_text", return_value=""
        ), patch.object(
            maa_plan_card, "state_lock", return_value=contextlib.nullcontext()
        ), patch.object(maa_plan_card, "BUTTONS_PATH", self.buttons), patch.object(
            maa_plan_card, "PLAN_PATH", self.plan_path
        ), patch.object(maa_common, "PLAN_PATH", self.plan_path), patch.object(
            maa_common, "DEFAULTS_PATH", self.defaults
        ), patch.object(
            maa_plan_card, "cleanup_old_threads", return_value={"failed": 0}
        ), patch.object(maa_plan_card, "cleanup_run_files"), patch.object(
            maa_plan_card, "retire_old_plan_cards", return_value={"failed": 0}
        ), patch.object(maa_plan_card, "discord_request") as request:
            code = maa_plan_card.main(argv)
        return SimpleNamespace(code=code, upstream=upstream, request=request)

    def test_the_evening_card_never_follows_upstream(self) -> None:
        evening = self._main(["pm"])
        self.assertEqual(evening.upstream.call_count, 0)
        payload = evening.request.call_args.args[3]
        self.assertEqual(payload["embeds"][0]["title"], "🌱 MAA 今日计划 · 晚场 22:00")
        self.assertEqual(len(payload["components"]), 2)
        morning = self._main(["am"])
        self.assertEqual(morning.upstream.call_count, 1)

    def test_the_card_phase_comes_from_the_workdir_name(self) -> None:
        for marker, expected in (("am", "am"), ("pm", "pm"), ("runs", "am")):
            with self.subTest(marker=marker):
                self.assertEqual(
                    maa_plan_card.select_phase(
                        [],
                        at=datetime(2026, 9, 9, 7, 50, tzinfo=BEIJING),
                        cwd=Path("/x") / marker,
                    ),
                    expected,
                )
        self.assertEqual(
            maa_plan_card.select_phase(
                [], at=datetime(2026, 9, 9, 21, 50, tzinfo=BEIJING), cwd=Path("/x/runs")
            ),
            "pm",
        )
        with self.assertRaisesRegex(ValueError, "只接受 am 或 pm"):
            maa_plan_card.select_phase(["evening"])

    def test_each_card_writes_only_its_own_session(self) -> None:
        with patch.object(
            maa_plan_card, "auto_stage", return_value=("SK-5", "碳素快没了")
        ):
            self._main(["am"])
            self._main(["pm"])
        written = json.loads(self.plan_path.read_text(encoding="utf-8"))
        registry = json.loads(self.buttons.read_text(encoding="utf-8"))
        # 自动定关写的是**场次**，不是旧的顶层 stage/skip 结构。
        self.assertEqual(set(written["sessions"]), {"am", "pm"})
        self.assertEqual(written["sessions"]["am"]["stage"], "SK-5")
        self.assertEqual(written["sessions"]["am"]["stage_source"], "auto")
        self.assertNotIn("stage", written)
        # 晚场卡那趟把早场卡的控件整段清掉了（不变量 #7），别人的键留着。
        self.assertIn("hermes:btn:agent-book:x", registry)
        self.assertNotIn("hermes:btn:maa:am:stage-select", registry)
        self.assertIn("hermes:btn:maa:pm:stage-select", registry)

    def test_old_cards_are_retired_by_title_prefix(self) -> None:
        messages = [
            {
                "id": "111",
                "embeds": [{"title": "🌱 MAA 今日计划 · 早场 08:00"}],
                "components": [{"type": 1}],
            },
            {
                "id": "222",
                "embeds": [{"title": "🤖 抄作业：现在能抄的关"}],
                "components": [{"type": 1}],
            },
        ]
        with patch.object(
            maa_plan_card, "discord_request", side_effect=[messages, {}]
        ) as request:
            result = maa_plan_card.retire_old_plan_cards("token", "123")
        self.assertEqual(result, {"retired": 1, "failed": 0})
        self.assertEqual(
            request.call_args_list[1].args[1:3],
            ("PATCH", "/channels/123/messages/111"),
        )


class ActivityDeadlineTests(unittest.TestCase):
    """StageActivityV2 的活动截止时间：读得到就显示，读不到就整行不出现。"""

    def _write(self, payload) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "StageActivityV2.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_expire_time_is_read_in_its_declared_timezone(self) -> None:
        path = self._write(
            {
                "Official": {
                    "sideStoryStage": {
                        "SR": {
                            "Activity": {
                                "Tip": "SideStory「月行水上」",
                                "UtcExpireTime": "2026/09/18 03:59:59",
                                "TimeZone": 8,
                            }
                        },
                        "XX": {"Activity": {"StageName": "别的", "TimeZone": 8}},
                    }
                }
            }
        )
        deadlines = maa_plan_card.activity_deadlines(path)
        self.assertEqual(len(deadlines), 1)
        title, expire = deadlines[0]
        self.assertEqual(title, "SideStory「月行水上」")
        self.assertEqual(expire, datetime(2026, 9, 18, 3, 59, 59, tzinfo=BEIJING))
        text = maa_plan_card.activity_deadline_text(
            {"maa_activity_cache": str(path)},
            datetime(2026, 9, 9, 12, 0, tzinfo=BEIJING),
        )
        self.assertIn("截止 09-18 03:59", text)
        self.assertIn("还剩", text)

    def test_an_unreadable_cache_never_raises_and_shows_no_line(self) -> None:
        broken = self._write({"Official": {"sideStoryStage": "不是对象"}})
        broken.with_name("bad.json").write_text("{ 坏 json", encoding="utf-8")
        for path in (
            broken,
            broken.with_name("bad.json"),
            broken.with_name("missing.json"),
        ):
            with self.subTest(path=path.name):
                self.assertEqual(maa_plan_card.activity_deadlines(path), [])
                embed = maa_plan_card.build_embed(
                    maa_common.normalize_plan({}),
                    {"maa_activity_cache": str(path)},
                    "am",
                )
                self.assertNotIn(
                    "活动截止", [field["name"] for field in embed["fields"]]
                )

    def test_an_already_expired_activity_is_not_shown(self) -> None:
        path = self._write(
            {
                "Official": {
                    "sideStoryStage": {
                        "SR": {
                            "Activity": {
                                "Tip": "旧活动",
                                "UtcExpireTime": "2026/09/01 03:59:59",
                                "TimeZone": 8,
                            }
                        }
                    }
                }
            }
        )
        self.assertEqual(
            maa_plan_card.activity_deadline_text(
                {"maa_activity_cache": str(path)},
                datetime(2026, 9, 9, 12, 0, tzinfo=BEIJING),
            ),
            "",
        )


class EmulatorProbeSharedTests(unittest.TestCase):
    """四期 1d：一份探测实现，四条自动链路共用（D18 / 不变量 #24）。"""

    def test_there_is_exactly_one_probe_implementation(self) -> None:
        self.assertIs(maa_daily.emulator_is_online, maa_common.emulator_is_online)
        self.assertIs(maa_depot.emulator_is_online, maa_common.emulator_is_online)
        import maa_copilot_worker

        self.assertIs(
            maa_copilot_worker.emulator_is_online, maa_common.emulator_is_online
        )
        source = (ROOT / "scripts" / "maa_daily.py").read_text(encoding="utf-8")
        self.assertNotIn("def emulator_is_online", source)

    def test_a_busy_emulator_stops_the_copilot_round_before_it_opens_anything(self) -> None:
        import maa_copilot_worker as worker

        token = "a" * 12
        queued = {
            "token": token,
            "status": "queued",
            "activity_key": "SideStory「月行水上」|SR",
            "entries": [
                {"stage": "SR-5", "stage_id": "act54side_05", "mode": "normal",
                 "candidate": {"id": 7}, "sanity_cost": 12}
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            searches = Path(raw) / "searches"
            searches.mkdir()
            (searches / f"{token}.json").write_text(
                json.dumps(queued), encoding="utf-8"
            )
            notices: list[tuple[str, str]] = []
            with patch.object(maa_copilot, "SEARCHES_DIR", searches), patch.object(
                worker, "state_lock", return_value=contextlib.nullcontext()
            ), patch.object(
                worker, "load_config", return_value={"channel_id": "1"}
            ), patch.object(
                worker, "emulator_is_online", return_value=(True, "BlueStacks 进程在跑")
            ), patch.object(
                worker, "_notice",
                side_effect=lambda c, t, b, col: notices.append((t, b)),
            ), patch.object(worker, "merge_last_run") as merge, patch.object(
                worker, "_fight_one"
            ) as fight, patch.object(worker, "open_emulator") as open_emulator:
                code = worker._batch_run_locked(queued)
            restored = json.loads((searches / f"{token}.json").read_text("utf-8"))
        self.assertEqual(code, 0)
        open_emulator.assert_not_called()
        fight.assert_not_called()
        # 这一轮放回排程，watcher 下一 tick 再试；跳过必须可见（护栏 4）。
        self.assertEqual(restored["status"], "scheduled")
        self.assertIn("模拟器正在使用", notices[0][1])
        self.assertIn("BlueStacks 进程在跑", notices[0][1])
        self.assertEqual(merge.call_args.args[0]["skip_reason"], "emulator_busy")


    def test_a_busy_emulator_leaves_the_depot_snapshot_untouched(self) -> None:
        at = datetime(2026, 9, 9, 4, 30, tzinfo=BEIJING)
        previous = "2026-09-02T04:30:00+08:00"
        context = unittest.mock.MagicMock()
        context.__enter__.return_value = True
        context.__exit__.return_value = False
        with tempfile.TemporaryDirectory() as raw:
            depot_path = Path(raw) / "depot.json"
            maa_common.atomic_write_json(
                depot_path, {"snapshot_at": previous, "snapshot_items": {}}
            )
            with patch.object(
                maa_depot,
                "_state",
                side_effect=lambda path=depot_path: maa_common.load_json(path, {}),
            ), patch.object(maa_depot, "run_lock", return_value=context), patch.object(
                maa_depot, "emulator_is_online", return_value=(True, "BlueStacks 进程在跑")
            ), patch.object(
                maa_depot, "report_emulator_busy"
            ) as report, patch.object(
                maa_depot, "reconcile"
            ) as writer, patch.object(maa_daily, "open_emulator") as open_emulator:
                result = maa_depot.run_if_due({"channel_id": "1"}, at=at)
            after = json.loads(depot_path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "skipped_busy")
        # `snapshot_at` 不能动：动了这次「没跑」就会被记成一次成功对表，
        # 7 天间隔白白往后推一周。
        self.assertEqual(result["snapshot_at"], previous)
        self.assertEqual(after["snapshot_at"], previous)
        writer.assert_not_called()
        open_emulator.assert_not_called()
        self.assertEqual(
            report.call_args.kwargs["state_update"]["depot_status"], "skipped_busy"
        )

    def test_the_shared_busy_report_writes_state_and_posts_a_grey_card(self) -> None:
        at = datetime(2026, 9, 9, 8, 0, tzinfo=BEIJING)
        with patch.object(maa_common, "merge_last_run") as merge, patch.object(
            maa_common, "discord_request"
        ) as request:
            maa_common.report_emulator_busy(
                {"channel_id": "123"},
                title="⏭️ 测试已跳过",
                reason="模拟器在用",
                detail="进程在跑",
                at=at,
                state_update={"status": "skipped"},
                token="token",
            )
        self.assertEqual(merge.call_args.args[0], {"status": "skipped"})
        embed = request.call_args.args[3]["embeds"][0]
        self.assertEqual(embed["color"], 0x99AAB5)
        self.assertIn("进程在跑", embed["description"])

    def test_a_failing_report_is_never_swallowed(self) -> None:
        with patch.object(
            maa_common, "merge_last_run", side_effect=OSError("boom")
        ), patch.object(
            maa_common, "discord_request", side_effect=OSError("net")
        ):
            with self.assertRaises(RuntimeError) as caught:
                maa_common.report_emulator_busy(
                    {"channel_id": "1"},
                    title="t",
                    reason="r",
                    detail="d",
                    at=maa_common.now_beijing(),
                    state_update={},
                    token="token",
                )
        self.assertIn("状态写入失败", str(caught.exception))
        self.assertIn("告警发送失败", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# 四期步骤 2：作业链统一
# ---------------------------------------------------------------------------

SR_INDEX = {
    "codes": {
        "SR-1": "act54side_01",
        "SR-2": "act54side_02",
        "SR-5": "act54side_05",
        "SR-6": "act54side_06",
        "SR-8": "act54side_08",
        "SR-EX-1": "act54side_ex01",
        "SR-S-1": "act54side_s01",
        "SR-TR-1": "act54side_tr01",
        "1-7": "main_01-07",
        "CE-6": "wk_melee_6",
    },
    "stages": {
        "act54side_01": "SR-1",
        "act54side_02": "SR-2",
        "act54side_05": "SR-5",
        "act54side_06": "SR-6",
        "act54side_08": "SR-8",
        "act54side_ex01": "SR-EX-1",
        "act54side_s01": "SR-S-1",
        "act54side_tr01": "SR-TR-1",
        "main_01-07": "1-7",
        "wk_melee_6": "CE-6",
    },
}
SR_KEY = "SideStory「月行水上」|SR"


def _layout_stage(code, zone, unlock, *, story=False, cost=10):
    return {"code": code, "zone_id": zone, "story": story, "ap_cost": 0 if story else cost,
            "unlock": list(unlock)}


# 测试用的 SR 地形：只含 `SR_INDEX` 里的关，链是 ST-1 → 1 → 2 → 5 → 6 → 8 → ST-2 → EX-1
# → S-1。教学关挂在 SR-1 之后但不挡别的关（真实活动里它挡路，见 CopilotListPlanTests）。
SR_LAYOUT = {
    "stages": {
        "act54side_st01": _layout_stage("SR-ST-1", "act54side_zone1", [], story=True),
        "act54side_01": _layout_stage("SR-1", "act54side_zone1", ["act54side_st01"]),
        "act54side_tr01": _layout_stage("SR-TR-1", "act54side_zone1", ["act54side_01"]),
        "act54side_02": _layout_stage("SR-2", "act54side_zone1", ["act54side_01"]),
        "act54side_05": _layout_stage("SR-5", "act54side_zone1", ["act54side_02"], cost=12),
        "act54side_06": _layout_stage("SR-6", "act54side_zone1", ["act54side_05"], cost=12),
        "act54side_08": _layout_stage("SR-8", "act54side_zone1", ["act54side_06"], cost=21),
        "act54side_st02": _layout_stage("SR-ST-2", "act54side_zone1", ["act54side_08"],
                                        story=True),
        "act54side_ex01": _layout_stage("SR-EX-1", "act54side_zone2", ["act54side_st02"]),
        "act54side_s01": _layout_stage("SR-S-1", "act54side_zone3", ["act54side_ex01"],
                                       cost=20),
    },
    "zones": {
        "act54side_zone1": {"name": "通学路", "index": 0},
        "act54side_zone2": {"name": "殡仪堂", "index": 1},
        "act54side_zone3": {"name": "怀旧区", "index": 2},
    },
}


def _nav_patch(keys):
    """把 `maa_clearing.nav_checker` 换成一张固定的导航键表。"""
    def factory(_dirs):
        def check(entry):
            stage, _mode = maa_clearing.split_entry(entry)
            if stage in keys:
                return True, ""
            return False, f"上游 tasks/Stages/SR.json 里没有 {stage} 这个顶层键"
        return check
    # 活动关的闸门（`copilot_nav_checker`）在测试里用同一张键表：「在表里」= 够得着锚点。
    return _both(
        patch.object(maa_clearing, "nav_checker", side_effect=factory),
        patch.object(maa_copilot, "copilot_nav_checker",
                     side_effect=lambda *_a, **_k: factory(())),
    )


@contextlib.contextmanager
def _both(*patches):
    with contextlib.ExitStack() as stack:
        mocks = [stack.enter_context(item) for item in patches]
        yield mocks[0]


def _zone(cleared: int, total: int, act: str = "act54side") -> dict:
    return {"activity_progress": {act: [
        {"zoneId": f"{act}_zone1", "clearedStage": cleared, "totalStage": total}
    ]}}


RAID_ON = {"copilot_raid_switch_task": "ChangeToRaidDifficulty"}
RAID_OFF = {"copilot_raid_switch_task": ""}


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
                self.assertEqual(maa_clearing.raid_switch_task(bad), "")
                self.assertEqual(maa_clearing.required_modes("SR-8", "SR", bad),
                                 ("normal",))
        # 不传 config 一律 fail-closed。
        self.assertEqual(maa_clearing.required_modes("SR-8", "SR"), ("normal",))

    def test_entry_round_trip_keeps_the_raid_suffix(self) -> None:
        self.assertEqual(maa_clearing.make_entry("SR-5", "raid"), "SR-5#raid")
        self.assertEqual(maa_clearing.make_entry("SR-5", "normal"), "SR-5")
        self.assertEqual(maa_clearing.split_entry("SR-5#raid"), ("SR-5", "raid"))
        self.assertEqual(maa_clearing.entry_label("SR-5#raid"), "SR-5（突袭）")


class LedgerTests(unittest.TestCase):
    """2a：本地记账。通关 = 所需模式全部有 `Copilot Completed`。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "ledger.json"
        self.addCleanup(self._tmp.cleanup)

    def _ensure(self, key):
        with patch.object(maa_copilot, "LEDGER_PATH", self.path), patch.object(
            maa_copilot, "ensure_private_dirs"
        ):
            return maa_copilot.ensure_ledger(key, path=self.path)

    def test_the_seed_only_lands_on_a_fresh_ledger_for_the_seeded_activity(self) -> None:
        ledger, note = self._ensure(SR_KEY)
        self.assertEqual(note, "")
        for stage in maa_copilot.SEED_CLEARED_NORMAL:
            self.assertEqual(ledger["stages"][stage]["normal"], maa_copilot.SEED_AT)
            # 突袭留空：spike A 一次都没打过（Q3 未验）。
            self.assertIsNone(ledger["stages"][stage]["raid"])
        self.assertEqual(json.loads(self.path.read_text("utf-8"))["seeded"], "SR")

    def test_another_activity_is_never_seeded(self) -> None:
        ledger, _note = self._ensure("SideStory「墟」复刻|AT")
        self.assertEqual(ledger["stages"], {})

    def test_an_existing_but_unseeded_ledger_still_gets_the_seed(self) -> None:
        """2026-09-09 线上缺陷：账本被别的活动的 tick 先建出来，种子就再也没跑过。

        `ensure_ledger()` 原来只在 `load_ledger()` 返回 `None` 时才种，而线上
        02:23「墟」复刻（`|AT`）那一 tick 先建了账，03:00 SR 上线走「归档 + 新开」，
        `ledger is not None`，种子整个被跳过 → SR-5/6/8 的普通全空、第一轮就重抄。
        现在按 `seeded` 标记补，`seed_ledger()` 只补空位所以是幂等的。
        """
        maa_common.atomic_write_json(self.path, {
            "activity_key": SR_KEY,
            "stages": {"SR-5": {"normal": "2026-09-09T01:00:00+08:00", "raid": None,
                                "attempts": [{"at": "2026-09-09T01:00:00+08:00",
                                              "mode": "normal", "outcome": "won"}]}},
        })
        ledger, note = self._ensure(SR_KEY)
        self.assertEqual(note, "")
        # 已有记账原样保留——种子只补空位，绝不覆盖。
        self.assertEqual(ledger["stages"]["SR-5"]["normal"], "2026-09-09T01:00:00+08:00")
        self.assertEqual(len(ledger["stages"]["SR-5"]["attempts"]), 1)
        # 空位补上了。
        for stage in ("SR-6", "SR-8"):
            self.assertEqual(ledger["stages"][stage]["normal"], maa_copilot.SEED_AT)
            self.assertIsNone(ledger["stages"][stage]["raid"])
        self.assertEqual(json.loads(self.path.read_text("utf-8"))["seeded"], "SR")

    def test_a_ledger_already_seeded_is_left_alone(self) -> None:
        self._ensure(SR_KEY)
        ledger, _note = self._ensure(SR_KEY)
        ledger["stages"]["SR-5"]["normal"] = None
        maa_common.atomic_write_json(self.path, ledger)
        again, _note = self._ensure(SR_KEY)
        # `seeded` 已经是 SR → 一个字节都不再补（用户自己抹掉的记账不会被种回来）。
        self.assertIsNone(again["stages"]["SR-5"]["normal"])

    def test_the_seed_survives_an_activity_rotation_into_the_seeded_one(self) -> None:
        """先「墟」复刻建账、再换到 SR：归档 + 新开那条路也必须种。"""
        self._ensure("SideStory「墟」复刻|AT")
        ledger, note = self._ensure(SR_KEY)
        self.assertIn("ledger-", note)
        for stage in maa_copilot.SEED_CLEARED_NORMAL:
            self.assertEqual(ledger["stages"][stage]["normal"], maa_copilot.SEED_AT)

    def test_a_new_activity_archives_the_old_ledger(self) -> None:
        self._ensure(SR_KEY)
        ledger, note = self._ensure("SideStory「墟」复刻|AT")
        archive = self.path.parent / "ledger-SideStory-SR.json"
        self.assertTrue(archive.is_file(), sorted(p.name for p in self.path.parent.iterdir()))
        self.assertIn("ledger-", note)
        self.assertEqual(json.loads(archive.read_text("utf-8"))["activity_key"], SR_KEY)
        self.assertEqual(ledger["stages"], {})

    def test_a_non_activity_stage_never_archives_the_running_ledger(self) -> None:
        """手动抄资源本 / 主线时 `activity_key` 是空串（四期步骤 5 修的线上隐患）。

        原来 `ensure_ledger("")` 见 key 不同就把当期账本归档、新开一份空的，
        下一 tick watcher 再归档回来——真实通关记录散进归档文件。
        """
        activity, _note = self._ensure(SR_KEY)
        maa_copilot.record_copilot_attempt(activity, "SR-7", "normal", outcome="won")
        with patch.object(maa_copilot, "ensure_private_dirs"):
            maa_copilot.save_ledger(activity, self.path)
        before = self.path.read_bytes()

        ledger, note = self._ensure("")
        self.assertEqual(note, "")
        # 当期账本一个字节都没动，也没有多出任何归档文件。
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(
            sorted(p.name for p in self.path.parent.iterdir()),
            ["ledger-standalone.json", "ledger.json"],
        )
        self.assertEqual(ledger["stages"], {})
        # 返回的账本照样能记账，且落到独立文件里。
        standalone = maa_copilot.ledger_path("", path=self.path)
        maa_copilot.record_copilot_attempt(ledger, "CE-6", "normal", outcome="won")
        with patch.object(maa_copilot, "ensure_private_dirs"):
            maa_copilot.save_ledger(ledger, standalone)
        saved = json.loads(standalone.read_text("utf-8"))
        self.assertTrue(saved["stages"]["CE-6"]["normal"])
        self.assertEqual(saved["activity_key"], "")
        # 活动账本里既没有 CE-6，也没丢 SR-7。
        kept = json.loads(self.path.read_text("utf-8"))
        self.assertNotIn("CE-6", kept["stages"])
        self.assertTrue(kept["stages"]["SR-7"]["normal"])

    def test_the_standalone_ledger_is_reused_and_never_reseeded(self) -> None:
        first, _note = self._ensure("")
        maa_copilot.record_copilot_attempt(first, "1-7", "normal", outcome="won")
        with patch.object(maa_copilot, "ensure_private_dirs"):
            maa_copilot.save_ledger(first, maa_copilot.ledger_path("", path=self.path))
        again, note = self._ensure("")
        self.assertEqual(note, "")
        self.assertTrue(again["stages"]["1-7"]["normal"])
        # 空 key 的前缀不是 `SR`，种子绝不会写进这一份。
        self.assertNotIn("SR-5", again["stages"])

    def test_the_ledger_path_splits_activity_from_standalone(self) -> None:
        self.assertEqual(maa_copilot.ledger_path(SR_KEY, path=self.path), self.path)
        self.assertEqual(
            maa_copilot.ledger_path("   ", path=self.path).name, "ledger-standalone.json"
        )
        self.assertEqual(maa_copilot.ledger_path(SR_KEY), maa_copilot.LEDGER_PATH)
        self.assertEqual(maa_copilot.ledger_path(""), maa_copilot.LEDGER_STANDALONE_PATH)

    def test_a_broken_ledger_reads_as_absent_instead_of_raising(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(maa_copilot.load_ledger(self.path))

    def test_raid_failure_means_the_stage_is_not_cleared(self) -> None:
        ledger = maa_copilot.new_ledger(SR_KEY)
        maa_copilot.record_copilot_attempt(ledger, "SR-5", "normal", outcome="won", op=1)
        maa_copilot.record_copilot_attempt(
            ledger, "SR-5", "raid", outcome="failed", op=1, detail="Copilot Error"
        )
        pending = maa_copilot.ledger_pending_modes(
            ledger, None, "SR-5", "act54side_05", ("normal", "raid")
        )
        self.assertEqual(pending, ["raid"])
        self.assertEqual(len(ledger["stages"]["SR-5"]["attempts"]), 2)
        self.assertIsNone(ledger["stages"]["SR-5"]["raid"])

    def test_an_unknown_attempt_says_which_log_to_read(self) -> None:
        # `unknown` 的结论就是「去日志里核对胜利标记」，不写清是哪一份等于让人翻 runs/。
        ledger = maa_copilot.new_ledger(SR_KEY)
        maa_copilot.record_copilot_attempt(
            ledger, "SR-5", "normal", outcome="unknown",
            detail="退出码 0 但输出里没有 `Copilot Completed`",
            log_name="2026-09-09-copilot-fda56c1811ba.log",
        )
        maa_copilot.record_copilot_attempt(
            ledger, "SR-6", "normal", outcome="won",
            log_name="2026-09-09-copilot-fda56c1811ba.log",
        )
        attempts = ledger["stages"]
        self.assertIn(
            "日志 runs/2026-09-09-copilot-fda56c1811ba.log",
            attempts["SR-5"]["attempts"][0]["detail"],
        )
        # 通关那笔不需要人去翻日志。
        self.assertNotIn("日志 runs/", attempts["SR-6"]["attempts"][0]["detail"])

    def test_both_modes_won_means_cleared(self) -> None:
        ledger = maa_copilot.new_ledger(SR_KEY)
        for mode in ("normal", "raid"):
            maa_copilot.record_copilot_attempt(ledger, "SR-5", mode, outcome="won")
        self.assertEqual(
            maa_copilot.ledger_pending_modes(
                ledger, None, "SR-5", "act54side_05", ("normal", "raid")
            ),
            [],
        )

    def test_a_fully_cleared_zone_counts_as_cleared_without_any_ledger(self) -> None:
        # D10：森空岛只有 zone 粒度，用户手打过的关靠它认出来。
        roster = _zone(11, 11)
        self.assertTrue(maa_copilot.zone_fully_cleared(roster, "act54side_05"))
        self.assertEqual(
            maa_copilot.ledger_pending_modes(
                {}, roster, "SR-5", "act54side_05", ("normal", "raid")
            ),
            [],
        )

    def test_a_partially_cleared_zone_does_not_clear_anything(self) -> None:
        roster = _zone(3, 11)
        self.assertFalse(maa_copilot.zone_fully_cleared(roster, "act54side_05"))
        self.assertEqual(
            maa_copilot.ledger_pending_modes(
                {}, roster, "SR-5", "act54side_05", ("normal", "raid")
            ),
            ["normal", "raid"],
        )

    def test_zone_helpers_stay_two_separate_rules(self) -> None:
        """`zone_fully_cleared` 与 `zone_progress_warning` 共用定位、不共用结论。"""
        import ast

        source = (ROOT / "scripts" / "maa_copilot.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for name in ("zone_fully_cleared", "zone_progress_warning"):
            node = next(
                item for item in tree.body
                if isinstance(item, ast.FunctionDef) and item.name == name
            )
            calls = {
                child.func.id
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            }
            self.assertIn("stage_zone", calls, name)
            # 两条规则互不调用。
            self.assertNotIn("zone_fully_cleared", calls - {name})
            self.assertNotIn("zone_progress_warning", calls - {name})


class CopilotTargetSetTests(unittest.TestCase):
    """2b：目标集合与顺序。不变量 #37 的导航键闸门落在这里。"""

    def setUp(self) -> None:
        self.config = {"activity_search_retry_minutes": 30}
        self.at = datetime(2026, 9, 9, 12, 0, tzinfo=BEIJING)

    def _collect(self, *, ledger=None, roster=None, state=None, config=None,
                 keys=("SR-1", "SR-2", "SR-5", "SR-6", "SR-8", "SR-EX-1", "SR-S-1",
                       "SR-TR-1")):
        import maa_activity_watch as watch

        with _nav_patch(set(keys)):
            return watch.collect_targets(
                {**self.config, **(config or {})},
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


class CopilotDailyAttemptCapTests(unittest.TestCase):
    """当日上限：同一 `(关, 模式)` 打满 `auto_copilot_max_attempts_per_day` 就到明天。

    2026-09-09 03:30 与 04:00 两轮把 SR-5/6/7 原样重打了一遍——记账全是 `unknown`
    （缺陷 1），30 分钟退避一到就又排进来。退避管「隔多久再试」，管不住「一天几次」。
    """

    def setUp(self) -> None:
        self.config = {"activity_search_retry_minutes": 30}
        self.at = datetime(2026, 9, 9, 12, 0, tzinfo=BEIJING)

    def _ledger(self, *attempts):
        ledger = maa_copilot.new_ledger(SR_KEY)
        for mode, outcome, at in attempts:
            maa_copilot.record_copilot_attempt(
                ledger, "SR-5", mode, outcome=outcome, at=at
            )
        return ledger

    def _collect(self, ledger, at=None):
        import maa_activity_watch as watch

        with _nav_patch({"SR-5", "SR-6"}):
            return watch.collect_targets(
                self.config, {}, "SR", SR_INDEX, ledger, None, at or self.at,
            )

    def test_two_unknowns_today_take_the_stage_out_until_tomorrow(self) -> None:
        ledger = self._ledger(
            ("normal", "unknown", self.at - timedelta(hours=9)),
            ("normal", "unknown", self.at - timedelta(hours=8)),
        )
        targets, excluded = self._collect(ledger)
        self.assertNotIn("SR-5", [t["stage"] for t in targets])
        capped = {e["stage"]: e for e in excluded if e["reason"] == "daily_cap"}
        self.assertIn("SR-5", capped)
        self.assertIn("今天已尝试 2 次", capped["SR-5"]["detail"])
        # 同一批里没打过的关照排。
        self.assertIn("SR-6", [t["stage"] for t in targets])
        # 次日自动恢复——不用清任何状态，`attempts_today()` 换了日子自然归零。
        targets, _excluded = self._collect(ledger, at=self.at + timedelta(days=1))
        self.assertIn("SR-5", [t["stage"] for t in targets])

    def test_one_failure_today_still_leaves_a_try(self) -> None:
        ledger = self._ledger(("normal", "failed", self.at - timedelta(hours=2)))
        targets, _excluded = self._collect(ledger)
        self.assertIn("SR-5", [t["stage"] for t in targets])

    def test_a_win_never_counts_toward_the_cap(self) -> None:
        # 普通通了、突袭还差：普通那两笔 `won` 不许把这一关的机会吃掉。
        ledger = self._ledger(
            ("normal", "won", self.at - timedelta(hours=4)),
            ("normal", "won", self.at - timedelta(hours=3)),
            ("raid", "failed", self.at - timedelta(hours=2)),
        )
        self.assertEqual(maa_copilot.attempts_today(ledger, "SR-5", "normal", self.at), 0)
        self.assertEqual(maa_copilot.attempts_today(ledger, "SR-5", "raid", self.at), 1)
        targets, _excluded = self._collect(ledger)
        target = next(t for t in targets if t["stage"] == "SR-5")
        self.assertEqual(target["modes"], ["raid"])

    def test_yesterdays_attempts_do_not_count(self) -> None:
        ledger = self._ledger(
            ("normal", "unknown", self.at - timedelta(days=1)),
            ("normal", "unknown", self.at - timedelta(days=1, hours=1)),
        )
        self.assertEqual(maa_copilot.attempts_today(ledger, "SR-5", "normal", self.at), 0)
        self.assertIn("SR-5", [t["stage"] for t in self._collect(ledger)[0]])

    def test_a_zero_cap_turns_the_rule_off(self) -> None:
        self.config = {**self.config, "auto_copilot_max_attempts_per_day": 0}
        ledger = self._ledger(*[
            ("normal", "unknown", self.at - timedelta(hours=n)) for n in range(1, 6)
        ])
        self.assertIn("SR-5", [t["stage"] for t in self._collect(ledger)[0]])

    def test_a_broken_cap_config_falls_back_to_the_default(self) -> None:
        self.assertEqual(maa_copilot.daily_attempt_cap({}), 2)
        self.assertEqual(maa_copilot.daily_attempt_cap(None), 2)
        self.assertEqual(
            maa_copilot.daily_attempt_cap({"auto_copilot_max_attempts_per_day": "x"}), 2
        )

    def test_the_cap_is_not_a_runtime_cache_key(self) -> None:
        # 仓库拥有这个键：线上没有写回它的路径，部署要能把改动带过去（不变量 #25）。
        seed = json.loads((ROOT / "state" / "config.json").read_text("utf-8"))
        self.assertEqual(seed["auto_copilot_max_attempts_per_day"], 2)
        self.assertNotIn(
            "auto_copilot_max_attempts_per_day", deploy.PRESERVED_RUNTIME_CACHE_KEYS
        )


class CopilotRoundPlanningTests(unittest.TestCase):
    """2c：理智预算、排轮、`scheduled_at`。"""

    def setUp(self) -> None:
        self.plan = {"annihilation": True, "tasks": {"fight": True}}

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
        self.assertEqual(maa_clearing.sanity_budget("x", self.plan, {}), 0)
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


class CopilotDispatchGateTests(unittest.TestCase):
    """2c：派发闸门——到点 + 不在 blackout + 锁空闲 + 模拟器离线。"""

    def setUp(self) -> None:
        self.at = datetime(2026, 9, 9, 12, 0, tzinfo=BEIJING)

    def _blocker(self, scheduled_at="", *, blackout=None, depot=None,
                 locked=False, online=(False, "")):
        with patch.object(maa_action, "_copilot_blackout_reason", return_value=blackout), \
             patch.object(maa_action, "depot_window_reason", return_value=depot), \
             patch.object(maa_action, "is_run_locked", return_value=locked), \
             patch.object(maa_action, "emulator_is_online", return_value=online):
            return maa_action.dispatch_blocker({}, scheduled_at, self.at)

    def test_the_gate_has_exactly_one_implementation(self) -> None:
        """不变量 #13：闸门只许住在 `maa_action.py`，watcher 只是调用方。"""
        import maa_activity_watch as watch

        source = (ROOT / "scripts" / "maa_activity_watch.py").read_text(encoding="utf-8")
        self.assertNotIn("def dispatch_blocker", source)
        self.assertIn("maa_action.dispatch_blocker", source)
        self.assertFalse(hasattr(watch, "dispatch_blocker"))
        self.assertTrue(callable(maa_action.dispatch_blocker))
        # 三处调用点各查一次同一份闸门（手动两处 + 自动一处）。
        action_source = (ROOT / "scripts" / "maa_action.py").read_text(encoding="utf-8")
        worker_source = (ROOT / "scripts" / "maa_copilot_worker.py").read_text(encoding="utf-8")
        self.assertIn("dispatch_blocker(config, \"\", now_beijing())", action_source)
        self.assertIn("maa_action.dispatch_blocker(config, \"\", now_beijing())",
                      worker_source)

    def test_all_clear_dispatches(self) -> None:
        self.assertEqual(self._blocker(), "")

    def test_every_blocker_is_visible(self) -> None:
        cases = [
            ({"scheduled_at": (self.at + timedelta(minutes=30)).isoformat()}, "等理智恢复"),
            ({"blackout": "距 08:00 日常场次不足 25 分钟"}, "08:00"),
            ({"depot": "处于 Depot 对表窗口"}, "Depot"),
            ({"locked": True}, "run.lock"),
            ({"online": (True, "BlueStacks 进程在跑")}, "模拟器正在使用"),
        ]
        for kwargs, expected in cases:
            with self.subTest(expected=expected):
                self.assertIn(expected, self._blocker(**kwargs))

    def test_the_depot_window_is_kept_clear(self) -> None:
        for hour, minute, blocked in (
            (4, 19, False), (4, 20, True), (4, 30, True), (4, 59, True), (5, 0, False)
        ):
            with self.subTest(hour=hour, minute=minute):
                reason = maa_action.depot_window_reason(
                    datetime(2026, 9, 2, hour, minute, tzinfo=BEIJING)
                )
                self.assertEqual(bool(reason), blocked)

    def test_dispatch_flips_the_state_to_queued_and_spawns_the_worker(self) -> None:
        import maa_activity_watch as watch

        token = "a" * 12
        with tempfile.TemporaryDirectory() as raw:
            searches = Path(raw) / "searches"
            searches.mkdir()
            (searches / f"{token}.json").write_text(
                json.dumps({"token": token, "status": "scheduled", "entries": []}),
                encoding="utf-8",
            )
            with patch.object(maa_copilot, "SEARCHES_DIR", searches), \
                 patch.object(watch, "state_lock", return_value=contextlib.nullcontext()), \
                 patch.object(maa_action, "dispatch_blocker", return_value=""), \
                 patch.object(maa_action, "dispatch_batch") as dispatch:
                status = watch._dispatch({}, token, {}, self.at)
            state = json.loads((searches / f"{token}.json").read_text("utf-8"))
        self.assertEqual(status, "dispatched")
        self.assertEqual(state["status"], "queued")
        self.assertTrue(state["execution_requested_at"])
        dispatch.assert_called_once_with(token)

    def test_a_blocked_round_stays_scheduled_and_is_never_dispatched(self) -> None:
        import maa_activity_watch as watch

        with patch.object(maa_action, "dispatch_blocker", return_value="run.lock 被占"), \
             patch.object(maa_action, "dispatch_batch") as dispatch:
            status = watch._dispatch({}, "a" * 12, {}, self.at)
        self.assertEqual(status, "scheduled")
        dispatch.assert_not_called()

    def test_the_dispatcher_argv_is_exactly_run_batch(self) -> None:
        with patch.object(maa_action.subprocess, "Popen") as popen, patch.object(
            maa_action.Path, "is_file", return_value=True
        ):
            maa_action.dispatch_batch("b" * 12)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[-2:], ["run-batch", "b" * 12])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])


class CopilotTimeBudgetTests(unittest.TestCase):
    """2c：距下一场日常不足 `auto_copilot_min_lead_minutes` 就不开新关。"""

    def test_a_near_daily_run_stops_the_round(self) -> None:
        for at in (datetime(2026, 9, 9, 7, 30, tzinfo=BEIJING),
                   datetime(2026, 9, 9, 21, 25, tzinfo=BEIJING)):
            with self.subTest(at=at):
                self.assertIsNotNone(maa_action.min_lead_reason({}, at))

    def test_a_far_daily_run_lets_the_round_continue(self) -> None:
        self.assertIsNone(
            maa_action.min_lead_reason({}, datetime(2026, 9, 9, 12, 0, tzinfo=BEIJING))
        )

    def test_the_lead_is_configurable_and_zero_disables_it(self) -> None:
        at = datetime(2026, 9, 9, 7, 30, tzinfo=BEIJING)
        self.assertIsNone(
            maa_action.min_lead_reason({"auto_copilot_min_lead_minutes": 0}, at)
        )
        self.assertIsNotNone(
            maa_action.min_lead_reason({"auto_copilot_min_lead_minutes": 120}, at)
        )
        # 它比 blackout 宽：blackout 只管「能不能开这一轮」。
        self.assertGreater(maa_action.DEFAULT_MIN_LEAD_MINUTES, 25)

    def test_the_watcher_gate_and_the_worker_share_the_same_lead(self) -> None:
        # 派发前查一次（省得派出去立刻收尾），每关开始前再查一次，同一个函数。
        import maa_activity_watch as watch
        import maa_copilot_worker as worker

        # watcher 经 `maa_action.dispatch_blocker()` 查（闸门只有一份，见 #13）。
        self.assertIn("maa_action.dispatch_blocker",
                      (ROOT / "scripts" / "maa_activity_watch.py").read_text(encoding="utf-8"))
        self.assertIn("maa_action.min_lead_reason",
                      (ROOT / "scripts" / "maa_copilot_worker.py").read_text(encoding="utf-8"))
        self.assertFalse(hasattr(worker, "min_lead_reason"))
        self.assertTrue(hasattr(watch.maa_action, "min_lead_reason"))
        self.assertIn("min_lead_reason",
                      (ROOT / "scripts" / "maa_action.py").read_text(encoding="utf-8"))


class CopilotBatchExecutionTests(unittest.TestCase):
    """2d：一条执行链——串行连抄、逐关记账、失败分级。"""

    def _entry(self, stage, mode="normal", stage_id=None, cost=12):
        return {
            "stage": stage,
            "stage_id": stage_id or SR_INDEX["codes"][stage],
            "mode": mode,
            "candidate": {"id": 1, "title": "稳定作业"},
            "sanity_cost": cost,
        }

    def _run(self, entries, fight, *, activity_key=SR_KEY, lead=None, ledger_dir=None,
             config=None, settle=None, recheck=None, source=None, paused=False):
        import maa_copilot_worker as worker

        token = "a" * 12
        ledger = maa_copilot.new_ledger(activity_key)
        reports: list[dict] = []
        settled: list[str] = []

        def default_settle(_config, stage, _env, _log):
            settled.append(stage)
            return True, ""
        # `ledger_dir` 打开的是**真的记账**（账本落在这个目录里），用来验证 worker
        # 把哪一份账本写到了哪个文件；其余用例只关心内存里的账本。
        ledger_patches = (
            [
                patch.object(maa_copilot, "LEDGER_PATH", ledger_dir / "ledger.json"),
                patch.object(
                    maa_copilot,
                    "LEDGER_STANDALONE_PATH",
                    ledger_dir / "ledger-standalone.json",
                ),
                patch.object(maa_copilot, "ensure_private_dirs"),
            ]
            if ledger_dir is not None
            else [
                patch.object(maa_copilot, "ensure_ledger", return_value=(ledger, "")),
                patch.object(maa_copilot, "save_ledger", side_effect=lambda l, *a: l),
            ]
        )
        with tempfile.TemporaryDirectory() as raw:
            searches = Path(raw) / "searches"
            searches.mkdir()
            state = {
                "token": token, "status": "queued", "activity_key": activity_key,
                "entries": entries, "message_id": "999",
            }
            if source:
                state["source"] = source
            (searches / f"{token}.json").write_text(json.dumps(state), encoding="utf-8")
            pause_path = Path(raw) / "auto_pause.json"
            if paused:
                pause_path.write_text(json.dumps({"paused": True}), encoding="utf-8")
            patches = [
                patch.object(maa_copilot, "SEARCHES_DIR", searches),
                patch.object(maa_copilot, "AUTO_PAUSE_PATH", pause_path),
                patch.object(maa_copilot, "register_auto_buttons"),
                patch.object(worker, "RUNS_DIR", Path(raw)),
                patch.object(worker, "state_lock", return_value=contextlib.nullcontext()),
                patch.object(
                    worker, "load_config",
                    return_value={"channel_id": "1", **(config or {})},
                ),
                patch.object(worker, "emulator_is_online", return_value=(False, "")),
                patch.object(worker, "_retire_round_controls"),
                patch.object(worker, "_notice"),
                patch.object(worker, "require_executable"),
                patch.object(worker, "open_emulator"),
                patch.object(worker, "wait_for_android"),
                patch.object(worker, "verify_game_installed"),
                patch.object(worker, "settle_android_runtime"),
                patch.object(worker, "maa_subprocess_env", return_value={}),
                patch.object(worker, "capture_screenshot", return_value=(True, "")),
                patch.object(worker, "close_everything", return_value=[]),
                patch.object(worker, "merge_last_run"),
                patch.object(worker, "cleanup_run_files"),
                patch.object(maa_action, "min_lead_reason", return_value=lead),
                patch.object(worker, "_recheck_before_stage", return_value=(True, ""),
                             side_effect=recheck),
                *ledger_patches,
                # 这一组验的是 B 基线（逐关 Fight 导航）；多作业路径见 CopilotListRoundTests。
                patch.object(worker, "_use_list_path", return_value=False),
                patch.object(worker, "_fight_one", side_effect=fight),
                patch.object(worker, "settle_after_battle", side_effect=settle or default_settle),
                patch.object(
                    worker, "_post_report",
                    side_effect=lambda config, embed, png: reports.append(embed),
                ),
            ]
            with contextlib.ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                with contextlib.redirect_stdout(io.StringIO()):
                    code = worker._batch_run_locked(json.loads(
                        (searches / f"{token}.json").read_text("utf-8")
                    ))
            final = json.loads((searches / f"{token}.json").read_text("utf-8"))
            snapshots = [
                json.loads(path.read_text("utf-8"))
                for path in Path(raw).glob("*-copilot-*.json")
            ]
        return SimpleNamespace(
            code=code, ledger=ledger, report=reports[0], state=final,
            results=reports[0]["fields"][0]["value"], settled=settled,
            snapshot=snapshots[0] if snapshots else None,
        )

    def test_a_prefight_environment_failure_is_marked_on_the_round(self) -> None:
        """2026-10-11：森空岛过期，每轮都在复核时退出、不记账，watcher 每 15 分钟重派。"""
        import maa_copilot_worker as worker

        def boom(*_a):
            raise worker.PrefightError("森空岛实时理智读取失败：HTTP 401")

        fought: list[str] = []
        result = self._run(
            [self._entry("SR-5")], lambda *a: fought.append("x") or ("won", ""),
            recheck=boom, source="activity-watch",
        )
        self.assertEqual(fought, [])
        self.assertEqual(result.state["status"], "execution_failed")
        self.assertIn("森空岛", result.state["prefight_error"])
        self.assertEqual(result.snapshot["status"], "failed")
        self.assertIn("森空岛", result.snapshot["stop_reason"])

    def test_sanity_shortage_is_not_a_prefight_environment_failure(self) -> None:
        result = self._run(
            [self._entry("SR-5")], lambda *a: ("won", ""),
            recheck=lambda *a: (False, "理智不足"),
        )
        self.assertNotIn("prefight_error", result.state)

    def test_a_finished_round_closes_its_run_snapshot(self) -> None:
        result = self._run([self._entry("SR-5")], lambda *a: ("won", ""))
        self.assertEqual(result.snapshot["status"], "success")
        self.assertIn("finished_at", result.snapshot)

    def test_a_paused_auto_round_stops_before_the_next_stage(self) -> None:
        fought: list[str] = []
        result = self._run(
            [self._entry("SR-5")], lambda *a: fought.append("x") or ("won", ""),
            source="activity-watch", paused=True,
        )
        self.assertEqual(fought, [])
        self.assertIn("暂停", result.snapshot["stop_reason"])
        self.assertNotIn("prefight_error", result.state)

    def test_pausing_never_stops_a_manual_round(self) -> None:
        result = self._run(
            [self._entry("SR-5")], lambda *a: ("won", ""), source="discord", paused=True,
        )
        self.assertEqual(result.code, 0)

    def test_every_fought_stage_is_settled_before_moving_on(self) -> None:
        """打过就要收尾（跳剧情 + 回主界面），导航失败的不收尾——它没进过战斗。

        2026-09-09：SR-8 两次 `Copilot Completed` 之后 5 秒就 closedown，截图停在
        战后剧情页，三星记上了、代理指挥没解锁，早场日常因此空跑一场。
        """
        entries = [self._entry("SR-5"), self._entry("SR-6"), self._entry("SR-8")]
        outcomes = iter([("won", ""), ("failed", "撤退"), ("nav", "SR-8 导航失败")])
        result = self._run(entries, lambda *a: next(outcomes))
        self.assertEqual(result.settled, ["SR-5", "SR-6"])

    def test_a_won_stage_whose_settle_failed_is_not_recorded_as_cleared(self) -> None:
        """2026-10-15 YW-4：赢了但战后剧情没跳过，代理指挥没解锁。记成通关就再也不会重抄。"""
        entries = [self._entry("SR-8")]
        result = self._run(
            entries, lambda *a: ("won", ""),
            settle=lambda *a: (False, "SR-8 战后未回到主界面：x"),
        )
        self.assertIn("❌ `SR-8`", result.results)
        self.assertIn("代理指挥未解锁", result.results)
        stage = result.ledger["stages"]["SR-8"]
        self.assertFalse(stage["normal"])
        self.assertEqual(stage["attempts"][-1]["outcome"], "failed")
        self.assertIn("战后未回到主界面", stage["attempts"][-1]["detail"])

    def test_a_failed_settle_after_a_lost_battle_keeps_the_original_detail(self) -> None:
        result = self._run(
            [self._entry("SR-8")], lambda *a: ("failed", "撤退"),
            settle=lambda *a: (False, "SR-8 战后未回到主界面：x"),
        )
        detail = result.ledger["stages"]["SR-8"]["attempts"][-1]["detail"]
        self.assertTrue(detail.startswith("撤退"), detail)
        self.assertNotIn("代理指挥", detail)

    def test_a_clean_round_records_every_mode_and_reports_each_stage(self) -> None:
        entries = [self._entry("SR-5"), self._entry("SR-5", "raid"), self._entry("SR-6")]
        result = self._run(entries, lambda *a: ("won", ""))
        self.assertEqual(result.code, 0)
        self.assertEqual(result.state["status"], "executed")
        self.assertEqual(
            maa_copilot.ledger_pending_modes(
                result.ledger, None, "SR-5", "act54side_05", ("normal", "raid")
            ),
            [],
        )
        for label in ("SR-5", "SR-5（突袭）", "SR-6"):
            self.assertIn(label, result.results)
        self.assertIn("3/3 通过", result.report["title"])

    def test_a_raid_entry_is_skipped_outright_when_the_switch_is_empty(self) -> None:
        """最后一道防线：开关为空时 `raid` 条目**绝不**跑 `maa copilot`，也不记账。

        单文件 `maa copilot --raid raid` 不切难度，真跑下去打的是普通难度，
        却会按突袭记一次通关（2026-09-09 线上缺陷）。
        """
        import maa_copilot_worker as worker

        entries = [self._entry("SR-5"), self._entry("SR-5", "raid")]
        fought: list[str] = []

        def fight(config, entry, env, log_path):
            fought.append(f"{entry['stage']}#{entry['mode']}")
            return "won", ""

        result = self._run(entries, fight, config=dict(RAID_OFF))
        self.assertEqual(fought, ["SR-5#normal"])
        # 记账里只有普通那一次；`raid` 的时间戳仍是 None。
        stage = result.ledger["stages"]["SR-5"]
        self.assertIsNone(stage["raid"])
        self.assertEqual([a["mode"] for a in stage["attempts"]], ["normal"])
        self.assertIn("突袭切换未启用", result.results)
        self.assertIn("⏭️", result.results)
        # 跳过不算失败：这一轮照样是「成功」，不发红卡也不停整轮。
        self.assertEqual(result.code, 0)
        self.assertEqual(result.state["status"], "executed")
        self.assertTrue(hasattr(worker, "RAID_DISABLED_NOTICE"))

    def test_a_raid_entry_still_runs_when_the_switch_is_configured(self) -> None:
        fought: list[str] = []
        self._run(
            [self._entry("SR-5"), self._entry("SR-5", "raid")],
            lambda config, entry, env, log: (
                fought.append(f"{entry['stage']}#{entry['mode']}") or ("won", "")
            ),
            config=dict(RAID_ON),
        )
        self.assertEqual(fought, ["SR-5#normal", "SR-5#raid"])

    def test_a_non_activity_round_writes_only_the_standalone_ledger(self) -> None:
        """手动抄资源本：`activity_key` 是空串，当期活动账本一个字节都不许动。"""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            activity = root / "ledger.json"
            maa_common.atomic_write_json(
                activity,
                {
                    "activity_key": SR_KEY,
                    "seeded": "SR",
                    "stages": {"SR-5": {"normal": "2026-09-09T02:00:00+08:00",
                                        "raid": None, "attempts": []}},
                },
            )
            before = activity.read_bytes()
            entries = [self._entry("CE-6", stage_id="wk_melee_6", cost=30)]
            result = self._run(
                entries, lambda *a: ("won", ""), activity_key="", ledger_dir=root
            )
            self.assertEqual(result.code, 0)
            self.assertEqual(activity.read_bytes(), before)
            # 没有归档文件；记账落在独立账本里。
            self.assertEqual(
                sorted(p.name for p in root.iterdir()),
                ["ledger-standalone.json", "ledger.json"],
            )
            standalone = json.loads((root / "ledger-standalone.json").read_text("utf-8"))
        self.assertTrue(standalone["stages"]["CE-6"]["normal"])
        self.assertEqual(standalone["activity_key"], "")

    def test_one_failed_stage_does_not_stop_the_next_one(self) -> None:
        entries = [self._entry("SR-5"), self._entry("SR-6"), self._entry("SR-8")]
        calls: list[str] = []

        def fight(_config, entry, _env, _log):
            calls.append(entry["stage"])
            if entry["stage"] == "SR-5":
                return "failed", "输出含失败标记：Copilot Error"
            return "won", ""

        result = self._run(entries, fight)
        self.assertEqual(calls, ["SR-5", "SR-6", "SR-8"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.state["status"], "execution_failed")
        self.assertIn("❌ `SR-5`", result.results)
        self.assertIn("✅ `SR-6`", result.results)
        self.assertIn("✅ `SR-8`", result.results)

    def test_a_navigation_failure_stops_the_whole_round(self) -> None:
        entries = [self._entry("SR-5"), self._entry("SR-6")]
        calls: list[str] = []

        def fight(_config, entry, _env, _log):
            calls.append(entry["stage"])
            return "nav", "SR-5 导航或关卡界面检查失败：…"

        result = self._run(entries, fight)
        self.assertEqual(calls, ["SR-5"])
        self.assertEqual(result.code, 1)
        self.assertIn("❌ `SR-5`", result.results)
        stop = next(f for f in result.report["fields"] if f["name"] == "停在")
        self.assertIn("导航", stop["value"])

    def test_a_failed_normal_skips_its_own_raid_but_keeps_the_next_stage(self) -> None:
        entries = [self._entry("SR-5"), self._entry("SR-5", "raid"), self._entry("SR-6")]
        calls: list[tuple[str, str]] = []

        def fight(_config, entry, _env, _log):
            calls.append((entry["stage"], entry["mode"]))
            if entry["stage"] == "SR-5":
                return "failed", "Copilot Error"
            return "won", ""

        result = self._run(entries, fight)
        self.assertEqual(calls, [("SR-5", "normal"), ("SR-6", "normal")])
        self.assertIn("⏭️ `SR-5（突袭）`", result.results)

    def test_the_time_budget_stops_the_round_before_the_first_stage(self) -> None:
        entries = [self._entry("SR-5"), self._entry("SR-6")]
        calls: list[str] = []

        def fight(_config, entry, _env, _log):
            calls.append(entry["stage"])
            return "won", ""

        result = self._run(entries, fight, lead="距 22:00 日常场次不足 40 分钟")
        self.assertEqual(calls, [])
        stop = next(f for f in result.report["fields"] if f["name"] == "停在")
        self.assertIn("40 分钟", stop["value"])

    def test_the_copilot_argv_never_carries_a_downgrade_flag(self) -> None:
        import maa_copilot_worker as worker

        commands: list[list[str]] = []

        def watchdog(_config, command, handle, _log_path, _env):
            commands.append(command)
            handle.write("Copilot Completed\n")
            return 0, 0

        with tempfile.TemporaryDirectory() as raw, \
             patch.object(worker, "_prepare_op_file", return_value=("maa://1", False)), \
             patch.object(worker, "_navigate_to_stage", return_value=(True, "")), \
             patch.object(worker, "switch_to_raid", return_value=(True, "")), \
             patch.object(worker, "_run_copilot_with_watchdog", side_effect=watchdog):
            log_path = Path(raw) / "copilot.log"
            log_path.write_text("", encoding="utf-8")
            outcome, _detail = worker._fight_one(
                {"maa_bin": "/bin/maa"}, self._entry("SR-5", "raid"), {}, log_path
            )
        self.assertEqual(outcome, "won")
        self.assertEqual(
            commands[0],
            ["/bin/maa", "copilot", "maa://1", "--formation", "--batch", "-v",
             "--raid", "raid"],
        )

    def test_an_earlier_run_in_the_same_log_cannot_fake_a_win(self) -> None:
        # 日志是追加的：上一趟的 `Copilot Completed` 不能把这一趟算成通关。
        import maa_copilot_worker as worker

        with tempfile.TemporaryDirectory() as raw, \
             patch.object(worker, "_prepare_op_file", return_value=("maa://1", False)), \
             patch.object(worker, "_navigate_to_stage", return_value=(True, "")), \
             patch.object(worker, "_run_copilot_with_watchdog", return_value=(0, 0)):
            log_path = Path(raw) / "copilot.log"
            log_path.write_text("Copilot Completed\n", encoding="utf-8")
            outcome, detail = worker._fight_one(
                {"maa_bin": "/bin/maa"}, self._entry("SR-5"), {}, log_path
            )
        self.assertEqual(outcome, "unknown", detail)


class CopilotLogMarkerTests(unittest.TestCase):
    """标记必须 flush 之后再把句柄交给子进程（不变量 #33 的落点）。

    2026-09-09 线上现象：`runs/2026-09-09-copilot-fda56c1811ba.log` 第 37 行是
    `Copilot Completed`，标记行却在第 42 行——排在这一关全部输出**之后**。
    标记没 `flush()` 就把句柄交给子进程，子进程直接往同一个 fd 写、先落盘，
    标记要等 `with` 退出才刷出来。于是「本关输出」是空的，`copilot_outcome()`
    一律 `unknown`，通关永远记不上、30 分钟退避一到就重打一遍。

    **这条必须用真实子进程**：任何在 Python 里往同一个 handle 写字符串的桩都会
    走同一个缓冲区，顺序天然正确，测不出这个 bug。
    """

    def _fight(self, log_path: Path) -> tuple[str, str]:
        import maa_copilot_worker as worker

        def watchdog(_config, _command, handle, _log_path, _env):
            # 真子进程 + 共享 fd：与 `maa copilot` 的写法一致。
            proc = subprocess.Popen(
                [sys.executable, "-c", "print('MissionStart')\nprint('Copilot Completed')"],
                stdout=handle,
                stderr=subprocess.STDOUT,
            )
            return proc.wait(), 0

        with patch.object(worker, "_prepare_op_file", return_value=("maa://1", False)), \
             patch.object(worker, "_navigate_to_stage", return_value=(True, "")), \
             patch.object(worker, "_run_copilot_with_watchdog", side_effect=watchdog):
            return worker._fight_one(
                {"maa_bin": "/bin/maa"},
                {"stage": "SR-5", "stage_id": "act54side_05", "mode": "normal",
                 "candidate": {"id": 101751}},
                {},
                log_path,
            )

    def test_the_marker_lands_before_the_subprocess_output_and_the_win_is_seen(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "2026-09-09-copilot-fda56c1811ba.log"
            log_path.write_text("", encoding="utf-8")
            outcome, detail = self._fight(log_path)
            lines = log_path.read_text("utf-8").splitlines()
        marker = next(i for i, line in enumerate(lines) if line.startswith("# "))
        completed = next(i for i, line in enumerate(lines) if line == "Copilot Completed")
        # 标记在前、子进程输出在后——旧写法下这两行是反的。
        self.assertLess(marker, completed, lines)
        self.assertEqual(outcome, "won", detail)

    def test_only_this_stages_output_is_judged(self) -> None:
        """上一关的 `Copilot Completed` 落在偏移之前，切不进这一关。"""
        import maa_copilot_worker as worker

        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "copilot.log"
            log_path.write_text("Copilot Completed\n", encoding="utf-8")
            with patch.object(worker, "_prepare_op_file", return_value=("maa://1", False)), \
                 patch.object(worker, "_navigate_to_stage", return_value=(True, "")), \
                 patch.object(worker, "_run_copilot_with_watchdog", return_value=(0, 0)):
                outcome, detail = worker._fight_one(
                    {"maa_bin": "/bin/maa"},
                    {"stage": "SR-5", "stage_id": "act54side_05", "mode": "normal",
                     "candidate": {"id": 1}},
                    {}, log_path,
                )
        self.assertEqual(outcome, "unknown", detail)


class CopilotRaidSwitchTests(unittest.TestCase):
    """突袭切换：⚠️ 真机未验，因此必须是一个可以一键关掉的配置开关。"""

    def test_the_switch_task_runs_a_custom_task_in_an_isolated_config(self) -> None:
        import maa_copilot_worker as worker

        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "config"
            (source / "resource" / "tasks").mkdir(parents=True)
            (source / "resource" / "tasks" / "tasks.json").write_text("{}", encoding="utf-8")
            log_path = Path(raw) / "copilot.log"
            log_path.write_text("", encoding="utf-8")
            written: dict[str, str] = {}
            with patch.object(
                worker.subprocess, "run",
                return_value=SimpleNamespace(returncode=0, stdout="Custom Completed", stderr=""),
            ) as run, patch.object(
                worker, "atomic_write_text",
                side_effect=lambda path, body: written.__setitem__(Path(path).name, body),
            ):
                ok, detail = worker.switch_to_raid(
                    {"maa_bin": "/bin/maa"}, "SR-5",
                    {"MAA_CONFIG_DIR": str(source)}, log_path,
                )
        self.assertTrue(ok, detail)
        self.assertIn("ChangeToRaidDifficulty", written["copilot-raid.toml"])
        self.assertEqual(run.call_args.args[0][:3], ["/bin/maa", "run", "copilot-raid"])
        # 只读上游、只写临时目录（不变量 #37）。
        self.assertNotEqual(run.call_args.kwargs["env"]["MAA_CONFIG_DIR"], str(source))

    def test_an_empty_switch_task_refuses_instead_of_faking_a_raid_clear(self) -> None:
        """空开关 = 功能关闭。**不许**跳过切换接着打：那打的是普通难度（2026-09-09）。"""
        import maa_copilot_worker as worker

        with patch.object(worker.subprocess, "run") as run:
            ok, detail = worker.switch_to_raid(
                {"copilot_raid_switch_task": ""}, "SR-5", {}, Path("/nonexistent")
            )
        self.assertFalse(ok)
        self.assertIn("突袭切换未启用", detail)
        run.assert_not_called()

    def test_a_failed_switch_is_a_navigation_class_failure(self) -> None:
        import maa_copilot_worker as worker

        with tempfile.TemporaryDirectory() as raw, patch.object(
            worker, "_prepare_op_file", return_value=("maa://1", False)
        ), patch.object(
            worker, "_navigate_to_stage", return_value=(True, "")
        ), patch.object(
            worker, "switch_to_raid", return_value=(False, "SR-5 切换突袭难度失败：…")
        ), patch.object(worker, "_run_copilot_with_watchdog") as watchdog:
            log_path = Path(raw) / "copilot.log"
            log_path.write_text("", encoding="utf-8")
            outcome, detail = worker._fight_one(
                {}, {"stage": "SR-5", "stage_id": "act54side_05", "mode": "raid",
                     "candidate": {"id": 1}},
                {}, log_path,
            )
        self.assertEqual(outcome, "nav")
        self.assertIn("切换突袭难度失败", detail)
        watchdog.assert_not_called()


class CopilotSettleTests(unittest.TestCase):
    """战后收尾：跳过首通剧情 → 返回主界面，跑在隔离的临时 MAA_CONFIG_DIR 里。"""

    def _settle(self, stdout, returncode=0, raise_exc=None):
        import maa_copilot_worker as worker

        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "config"
            (source / "resource" / "tasks").mkdir(parents=True)
            (source / "resource" / "tasks" / "tasks.json").write_text("{}", encoding="utf-8")
            log_path = Path(raw) / "copilot.log"
            log_path.write_text("", encoding="utf-8")
            written: dict[str, str] = {}
            overlays: dict[str, dict] = {}
            run_kwargs = (
                {"side_effect": raise_exc} if raise_exc
                else {"return_value": SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")}
            )
            with patch.object(worker.subprocess, "run", **run_kwargs) as run, patch.object(
                worker, "atomic_write_text",
                side_effect=lambda path, body: written.__setitem__(Path(path).name, body),
            ), patch.object(
                worker, "atomic_write_json",
                side_effect=lambda path, body: overlays.__setitem__(Path(path).name, body),
            ):
                ok, detail = worker.settle_after_battle(
                    {"maa_bin": "/bin/maa"}, "SR-8", {"MAA_CONFIG_DIR": str(source)}, log_path
                )
            log = log_path.read_text("utf-8")
        return SimpleNamespace(
            ok=ok, detail=detail, written=written, overlays=overlays, run=run, log=log,
        )

    def test_the_settle_run_skips_the_plot_then_returns_home(self) -> None:
        import maa_copilot_worker as worker

        result = self._settle("[跳过剧情] 03:47:20 Completed\n[返回主界面] 03:47:30 Completed\n")
        self.assertTrue(result.ok, result.detail)
        toml = result.written["copilot-settle.toml"]
        self.assertIn('name = "跳过剧情"', toml)
        self.assertIn('"CopilotSettleSkip"', toml)
        self.assertIn('name = "返回主界面"', toml)
        for name in worker.HOME_TASK_NAMES:
            self.assertIn(f'"{name}"', toml)
        self.assertEqual(result.run.call_args.args[0][:3], ["/bin/maa", "run", "copilot-settle"])
        self.assertIn("--user-resource", result.run.call_args.args[0])
        self.assertNotIn("config", result.run.call_args.kwargs["env"]["MAA_CONFIG_DIR"].split("/")[-1])
        self.assertIn("# settle SR-8", result.log)

    def test_the_skip_chain_is_derived_from_upstream_and_stops_after_the_confirm(self) -> None:
        """09-09 真机：上游 `SkipBattlePlotConfirm.next = [EndOfPlot]`，跳过后直接进结算页，
        `EndOfPlot` 永远等不到，白等 20 轮才 Error。派生一份把 `next` 清空，只写临时目录。"""
        result = self._settle("[跳过剧情] Completed\n")
        tasks = result.overlays["tasks.json"]
        self.assertEqual(tasks["CopilotSettleSkip"]["baseTask"], "SkipBattlePlot")
        self.assertEqual(tasks["CopilotSettleSkip"]["next"], ["CopilotSettleSkipConfirm"])
        self.assertEqual(tasks["CopilotSettleSkipConfirm"]["baseTask"], "SkipBattlePlotConfirm")
        self.assertEqual(tasks["CopilotSettleSkipConfirm"]["next"], [])
        # 派生任务不继承 `template`（10-15 YW-4：`templ not found CopilotSettleSkip.png`）。
        self.assertEqual(tasks["CopilotSettleSkip"]["template"], "SkipThePreBattlePlot.png")
        self.assertEqual(
            tasks["CopilotSettleSkipConfirm"]["template"], "SkipThePreBattlePlotConfirm.png"
        )
        # 原有的两条导航覆盖仍在同一份 tasks.json 里。
        self.assertIn("CopilotNavigationReady", tasks)

    def test_either_segment_completing_counts_as_settled(self) -> None:
        # 剧情跳过了、没回到主界面：游戏已到结算页，代理记录已落库（09-09 真机），算成功。
        skipped = self._settle("[跳过剧情] 09:04 Completed\n[返回主界面] 09:04 Error\n", returncode=1)
        self.assertTrue(skipped.ok, skipped.detail)
        # 没有剧情、直接回家：第一段 Error（找不到跳过键）、第二段 Completed，也算成功。
        home = self._settle("[跳过剧情] 09:04 Error\n[返回主界面] 09:04 Completed\n", returncode=1)
        self.assertTrue(home.ok, home.detail)
        # 两段都 Error（09-09 08:59 的真机现场）：措辞中性，不提代理指挥。
        neither = self._settle("[跳过剧情] 09:04:05 Error\n[返回主界面] 09:04:20 Error\n", returncode=1)
        self.assertFalse(neither.ok)
        self.assertIn("SR-8 战后未回到主界面", neither.detail)
        self.assertNotIn("代理指挥", neither.detail)

    def test_a_settle_exception_is_reported_instead_of_raised(self) -> None:
        result = self._settle("", raise_exc=subprocess.TimeoutExpired("maa", 240))
        self.assertFalse(result.ok)
        self.assertIn("战后收尾异常", result.detail)
        self.assertIn("TimeoutExpired", result.log)


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


class SanityYieldTests(unittest.TestCase):
    """D11：理智不够刷默认关时让路，估不出一律照常刷。"""

    def setUp(self) -> None:
        self.plan = maa_common.normalize_plan(
            {"stage": "CE-6", "medicine": "expiring", "tasks": {}}
        )
        self.plan["annihilation"] = True
        self.config = {
            "medicine_expire_days": 2,
            "fight_times": 2147483647,
            "recruit_times": 4,
        }
        self.at = datetime(2026, 9, 9, 22, 0, tzinfo=BEIJING)

    def _yield(self, *, sanity, cost=18):
        roster = None if sanity is None else {"sanity": {
            "current": sanity, "max": 135,
            "server_current_at": int(self.at.timestamp()),
            "last_ap_add_at": int(self.at.timestamp()),
        }}
        with patch.object(
            maa_copilot, "load_stage_index",
            return_value={"codes": {"CE-6": "wk_melee_6"},
                          "stages": {"wk_melee_6": "CE-6"}},
        ), patch.object(maa_copilot, "stage_sanity_cost", return_value=cost), \
             patch.object(maa_copilot, "load_roster", return_value=roster):
            return maa_daily.attach_sanity_yield(self.plan, self.config, self.at)

    def test_not_enough_sanity_drops_farming_and_keeps_annihilation(self) -> None:
        plan = self._yield(sanity=7)
        self.assertFalse(plan["farming"])
        self.assertIn("理智 7 不足以刷 CE-6", plan["farming_skip_reason"])
        rendered = maa_daily.render_daily_toml(plan, self.config)
        self.assertNotIn('name = "刷理智"', rendered)
        self.assertIn('name = "每日剿灭"', rendered)
        for name in ('name = "基建换班"', 'name = "公开招募"', 'name = "信用商店"',
                     'name = "领取奖励"', 'name = "返回主界面"'):
            self.assertIn(name, rendered)

    def test_enough_sanity_farms_as_usual(self) -> None:
        plan = self._yield(sanity=60)
        self.assertTrue(plan["farming"])
        self.assertIn('name = "刷理智"', maa_daily.render_daily_toml(plan, self.config))

    def test_an_unreadable_estimate_farms_as_usual(self) -> None:
        # 森空岛不可用 / 关卡理智未知 → 宁可多刷一场（不变量 #26 的精神）。
        self.assertTrue(self._yield(sanity=None)["farming"])
        self.assertTrue(self._yield(sanity=7, cost=None)["farming"])

    def test_a_throwing_probe_never_takes_the_farming_block_away(self) -> None:
        with patch.object(maa_copilot, "load_stage_index", side_effect=OSError("boom")):
            with contextlib.redirect_stdout(io.StringIO()):
                plan = maa_daily.attach_sanity_yield(self.plan, self.config, self.at)
        self.assertTrue(plan["farming"])

    def test_two_independent_switches_cover_all_four_combinations(self) -> None:
        for annihilation, farming in ((True, True), (True, False),
                                      (False, True), (False, False)):
            with self.subTest(annihilation=annihilation, farming=farming):
                rendered = maa_daily.render_daily_toml(
                    {**self.plan, "annihilation": annihilation, "farming": farming},
                    self.config,
                )
                self.assertEqual('name = "每日剿灭"' in rendered, annihilation)
                self.assertEqual('name = "刷理智"' in rendered, farming)

    def test_the_daily_no_longer_dispatches_a_clearing_round(self) -> None:
        self.assertFalse(hasattr(maa_daily, "dispatch_clearing_round"))
        self.assertFalse(hasattr(maa_daily, "report_clearing_dispatch_failure"))
        self.assertFalse(hasattr(maa_daily, "attach_clearing_yield"))


class ManualClearMarkTests(unittest.TestCase):
    """手动通关的写路径：`copilot mark-cleared` / `copilot unmark`。

    2026-09-09 线上：SR-5/6/7/8 用户手打通了，账本与森空岛 zone（10/11）都不知道，
    第一轮原样重抄。硬编码种子只救 SR 这一期，所以补一条用户能说话的入口。
    """

    def _run(self, argv, *, watch_state=None, config=None, ledger=None):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            watch = root / "activity_watch.json"
            maa_common.atomic_write_json(watch, watch_state or {"activity_key": SR_KEY})
            ledger_file = root / "ledger.json"
            if ledger is not None:
                maa_common.atomic_write_json(ledger_file, ledger)
            with patch.object(maa_action, "WATCH_PATH", watch), \
                 patch.object(maa_action, "load_config", return_value=config or {}), \
                 patch.object(maa_action, "state_lock", return_value=contextlib.nullcontext()), \
                 patch.object(maa_copilot, "LEDGER_PATH", ledger_file), \
                 patch.object(maa_copilot, "ensure_private_dirs"), \
                 patch.object(maa_copilot, "load_stage_index", return_value=SR_INDEX):
                ok, message = maa_action.dispatch(argv)
            files = {
                path.name: json.loads(path.read_text("utf-8"))
                for path in root.glob("ledger*.json")
            }
        return SimpleNamespace(ok=ok, message=message, files=files)

    def test_marking_writes_the_required_modes_into_the_activity_ledger(self) -> None:
        result = self._run(["copilot", "mark-cleared", "SR-1"], config=dict(RAID_OFF))
        self.assertTrue(result.ok, result.message)
        entry = result.files["ledger.json"]["stages"]["SR-1"]
        self.assertTrue(entry["normal"])
        # 突袭功能关着 → 只标普通，不替用户把没打过的突袭也记上。
        self.assertIsNone(entry["raid"])
        self.assertEqual(entry["attempts"][-1]["outcome"], "won")
        self.assertEqual(entry["attempts"][-1]["detail"], maa_copilot.MANUAL_MARK_DETAIL)
        self.assertIn("SR-1", result.message)
        self.assertIn("copilot unmark SR-1", result.message)
        # 顺手把账建好、种子也种上（`ensure_ledger` 那条路），不另开一套。
        self.assertEqual(result.files["ledger.json"]["seeded"], "SR")

    def test_marking_with_raid_on_covers_both_modes_and_all_is_explicit(self) -> None:
        both = self._run(["copilot", "mark-cleared", "SR-1"])
        entry = both.files["ledger.json"]["stages"]["SR-1"]
        self.assertTrue(entry["normal"] and entry["raid"])
        self.assertIn("SR-1（突袭）", both.message)
        only_raid = self._run(["copilot", "mark-cleared", "SR-1", "raid"], config=dict(RAID_OFF))
        entry = only_raid.files["ledger.json"]["stages"]["SR-1"]
        self.assertIsNone(entry["normal"])
        self.assertTrue(entry["raid"])
        bad = self._run(["copilot", "mark-cleared", "SR-1", "hard"])
        self.assertFalse(bad.ok)
        self.assertIn("normal / raid / all", bad.message)

    def test_marking_is_idempotent_and_never_overwrites_an_existing_timestamp(self) -> None:
        seeded = {
            "activity_key": SR_KEY, "seeded": "SR",
            "stages": {"SR-5": {"normal": "2026-09-09T03:35:38+08:00", "raid": None,
                                "attempts": []}},
        }
        result = self._run(["copilot", "mark-cleared", "SR-5"], config=dict(RAID_OFF),
                           ledger=seeded)
        self.assertTrue(result.ok)
        self.assertIn("已经记着通关了", result.message)
        entry = result.files["ledger.json"]["stages"]["SR-5"]
        self.assertEqual(entry["normal"], "2026-09-09T03:35:38+08:00")
        self.assertEqual(entry["attempts"], [])

    def test_the_manual_mark_does_not_count_against_the_daily_cap(self) -> None:
        """写成 `won` 是刻意的：`attempts_today()` 把非 `won` 全算成当天失败。"""
        ledger = maa_copilot.new_ledger(SR_KEY)
        maa_copilot.mark_stage_cleared(ledger, "SR-7", ("normal",))
        self.assertEqual(maa_copilot.attempts_today(ledger, "SR-7", "normal"), 0)
        self.assertEqual(
            maa_copilot.ledger_pending_modes(ledger, None, "SR-7", "act54side_07", ("normal",)),
            [],
        )

    def test_a_non_activity_stage_lands_in_the_standalone_ledger(self) -> None:
        result = self._run(["copilot", "mark-cleared", "CE-6"])
        self.assertTrue(result.ok, result.message)
        self.assertNotIn("ledger.json", result.files)
        entry = result.files["ledger-standalone.json"]["stages"]["CE-6"]
        self.assertTrue(entry["normal"])
        self.assertIsNone(entry["raid"])

    def test_an_unknown_stage_is_refused_without_touching_the_ledger(self) -> None:
        result = self._run(["copilot", "mark-cleared", "SR-99"])
        self.assertFalse(result.ok)
        self.assertIn("不在本地关卡索引里", result.message)
        self.assertEqual(result.files, {})

    def test_unmark_clears_both_timestamps_but_keeps_the_history(self) -> None:
        marked = {
            "activity_key": SR_KEY, "seeded": "SR",
            "stages": {"SR-8": {"normal": "2026-09-08T15:22:14+08:00",
                                "raid": "2026-09-09T05:00:00+08:00",
                                "attempts": [{"at": "2026-09-08T15:22:14+08:00",
                                              "mode": "normal", "outcome": "won"}]}},
        }
        result = self._run(["copilot", "unmark", "SR-8"], ledger=marked)
        self.assertTrue(result.ok, result.message)
        self.assertIn("normal、raid", result.message)
        entry = result.files["ledger.json"]["stages"]["SR-8"]
        self.assertIsNone(entry["normal"])
        self.assertIsNone(entry["raid"])
        self.assertEqual(len(entry["attempts"]), 1)
        again = self._run(["copilot", "unmark", "SR-8"], ledger=result.files["ledger.json"])
        self.assertTrue(again.ok)
        self.assertIn("本来就没有通关记录", again.message)

    def test_unmark_without_a_ledger_changes_nothing(self) -> None:
        result = self._run(["copilot", "unmark", "SR-8"])
        self.assertTrue(result.ok)
        self.assertIn("本来就没有通关记录", result.message)
        self.assertEqual(result.files, {})


class ManualCopilotPathTests(unittest.TestCase):
    """2e：手动路径——选关即执行，不降级。"""

    def _start(self, stage, watch_state, *, blocker="", config=None):
        with tempfile.TemporaryDirectory() as raw:
            watch = Path(raw) / "activity_watch.json"
            maa_common.atomic_write_json(watch, watch_state)
            searches = Path(raw) / "searches"
            searches.mkdir()
            with patch.object(maa_action, "WATCH_PATH", watch), \
                 patch.object(maa_action, "load_config", return_value=config or {}), \
                 patch.object(maa_copilot, "SEARCHES_DIR", searches), \
                 patch.object(maa_copilot, "ensure_private_dirs"), \
                 patch.object(maa_action, "dispatch_blocker", return_value=blocker), \
                 patch.object(
                     maa_action, "valid_copilot_stage",
                     return_value=(SR_INDEX["codes"][stage], stage),
                 ), \
                 _nav_patch({"SR-8", "CE-6"}), \
                 patch.object(maa_action.Path, "is_file", return_value=True), \
                 patch.object(maa_action.subprocess, "Popen") as popen:
                ok, message, token = maa_action.start_copilot_search(stage)
            written = sorted(path.name for path in searches.glob("*.json"))
            state = (
                json.loads((searches / f"{token}.json").read_text("utf-8"))
                if token
                else {}
            )
        return SimpleNamespace(
            ok=ok, message=message, state=state, popen=popen, written=written,
        )

    def test_an_activity_stage_expands_to_both_modes(self) -> None:
        result = self._start("SR-8", {"activity_key": SR_KEY})
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.state["modes"], ["normal", "raid"])
        self.assertEqual(result.state["activity_key"], SR_KEY)
        self.assertIn("普通 + 突袭", result.message)
        self.assertEqual(result.popen.call_args.args[0][-2:], ["search", result.state["token"]])

    def test_an_activity_stage_gets_one_entry_when_the_raid_switch_is_empty(self) -> None:
        """手动路径同样受总开关约束：只写一条普通条目，文案不再写「普通 + 突袭」。"""
        result = self._start("SR-8", {"activity_key": SR_KEY}, config=dict(RAID_OFF))
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.state["modes"], ["normal"])
        self.assertNotIn("突袭", result.message)

    def test_a_supply_stage_only_gets_the_normal_mode(self) -> None:
        result = self._start("CE-6", {"activity_key": SR_KEY})
        self.assertEqual(result.state["modes"], ["normal"])
        # 非活动关不参与活动复核，`activity_key` 留空。
        self.assertEqual(result.state["activity_key"], "")

    def test_a_blacked_out_manual_pick_writes_nothing_and_spawns_nothing(self) -> None:
        """不变量 #13 的手动落点：07:50 说「抄 SR-8」当场拒绝，且不排程。"""
        result = self._start(
            "SR-8", {"activity_key": SR_KEY},
            blocker="距 08:00 日常场次不足 25 分钟，为避免互斥抢占，请在日常结束后再抄作业。",
        )
        self.assertFalse(result.ok)
        self.assertIn("距 08:00 日常场次不足 25 分钟", result.message)
        self.assertIn("请日常 / 对表结束后再点", result.message)
        # 什么都不写、不派 worker：手动轮没人替它重试，排下去只会烂在 searches/。
        self.assertEqual(result.written, [])
        result.popen.assert_not_called()

    def test_the_manual_gate_is_the_shared_one_and_runs_before_the_state_file(self) -> None:
        # 三条时间闸门 + run.lock + 模拟器，一条都不能少（复用 dispatch_blocker）。
        for blocker in ("处于 Depot 对表窗口（北京时间 04:20–05:00）",
                        "距 22:00 日常场次不足 40 分钟",
                        "另一场 MAA 正在运行（run.lock 被占）",
                        "模拟器正在使用：BlueStacks 进程在跑"):
            with self.subTest(blocker=blocker):
                result = self._start("SR-8", {"activity_key": SR_KEY}, blocker=blocker)
                self.assertFalse(result.ok)
                self.assertIn(blocker, result.message)
                self.assertEqual(result.written, [])

    def test_a_stage_without_upstream_navigation_is_refused_before_any_work(self) -> None:
        with patch.object(maa_action, "load_config", return_value={}), \
             patch.object(
                 maa_action, "valid_copilot_stage", return_value=("act54side_09", "SR-9")
             ), _nav_patch({"SR-8"}), patch.object(
                 maa_action.subprocess, "Popen"
             ) as popen:
            ok, message, token = maa_action.start_copilot_search("SR-9")
        self.assertFalse(ok)
        self.assertIsNone(token)
        self.assertIn("导航不到这一关", message)
        popen.assert_not_called()

    def _search(self, *, blocker=""):
        import maa_copilot_worker as worker
        import maa_skland

        token = "a" * 12
        published: list[dict] = []
        with tempfile.TemporaryDirectory() as raw:
            searches = Path(raw) / "searches"
            searches.mkdir()
            (searches / f"{token}.json").write_text(
                json.dumps({
                    "token": token, "status": "pending", "stage": "SR-8",
                    "stage_id": "act54side_08", "modes": ["normal", "raid"],
                    "activity_key": SR_KEY,
                }),
                encoding="utf-8",
            )
            with patch.object(maa_copilot, "SEARCHES_DIR", searches), \
                 patch.object(maa_copilot, "SEARCH_LOCK_PATH", Path(raw) / "s.lock"), \
                 patch.object(maa_copilot, "prune_expired_searches", return_value=0), \
                 patch.object(worker, "load_config", return_value={"channel_id": "1"}), \
                 patch.object(worker, "state_lock", return_value=contextlib.nullcontext()), \
                 patch.object(maa_copilot, "load_stage_index",
                              return_value={"stages": {"act54side_08": "SR-8"}}), \
                 patch.object(maa_copilot, "stage_sanity_cost", return_value=21), \
                 patch.object(maa_copilot, "load_roster", return_value=None), \
                 patch.object(maa_skland, "refresh_roster", return_value={}), \
                 patch.object(
                     maa_copilot, "pick_perfect_candidate",
                     return_value=({"id": 5, "title": "稳定作业"}, "", []),
                 ), \
                 patch.object(maa_action, "dispatch_blocker", return_value=blocker), \
                 patch.object(
                     maa_action, "publish_round",
                     side_effect=lambda *a, **kw: (published.append(kw), "777")[1],
                 ), \
                 patch.object(maa_action, "dispatch_batch") as dispatch:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = worker.cmd_search(token)
            state = json.loads((searches / f"{token}.json").read_text("utf-8"))
        return SimpleNamespace(
            rc=rc, state=state, dispatch=dispatch, token=token,
            note=str(published[0].get("note") or "") if published else "",
        )

    def test_the_search_worker_queues_and_dispatches_the_round(self) -> None:
        result = self._search()
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.state["status"], "queued")
        self.assertEqual(
            [(entry["stage"], entry["mode"]) for entry in result.state["entries"]],
            [("SR-8", "normal"), ("SR-8", "raid")],
        )
        self.assertEqual(result.state["entries"][0]["sanity_cost"], 21)
        self.assertEqual(result.state["message_id"], "777")
        self.assertEqual(result.note, "手动选关，选中即执行。")
        result.dispatch.assert_called_once_with(result.token)

    def test_a_search_that_runs_into_the_blackout_is_never_dispatched(self) -> None:
        """搜索跑了 20s 刚好跨进 blackout：卡照发，但状态停在 `scheduled`。"""
        reason = "距 08:00 日常场次不足 25 分钟，为避免互斥抢占，请在日常结束后再抄作业。"
        result = self._search(blocker=reason)
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.state["status"], "scheduled")
        self.assertEqual(result.state["last_prepare_error"], reason)
        # 手动轮不自动重试；卡上说清「未启动」和出路，❌ 按钮仍然能点。
        self.assertIn(f"本轮未启动：{reason}", result.note)
        self.assertIn("重新选关", result.note)
        self.assertIn("scheduled", maa_copilot.CANCELABLE_STATUSES)
        self.assertNotIn("execution_requested_at", result.state)
        result.dispatch.assert_not_called()

    def test_the_run_batch_worker_refuses_to_touch_the_device_in_a_blackout(self) -> None:
        """最后一道防线：抢到锁了也不开机，状态回 `scheduled`（第 4 点）。"""
        import maa_copilot_worker as worker

        token = "a" * 12
        for gate in ("_copilot_blackout_reason", "depot_window_reason", "min_lead_reason"):
            with self.subTest(gate=gate), tempfile.TemporaryDirectory() as raw:
                searches = Path(raw) / "searches"
                searches.mkdir()
                (searches / f"{token}.json").write_text(
                    json.dumps({
                        "token": token, "status": "queued", "message_id": "9",
                        "entries": [{"stage": "SR-8", "mode": "normal"}],
                    }),
                    encoding="utf-8",
                )
                gates = {
                    "_copilot_blackout_reason": None,
                    "depot_window_reason": None,
                    "min_lead_reason": None,
                    gate: "闸门拦下了",
                }
                notices: list[str] = []
                with patch.object(maa_copilot, "SEARCHES_DIR", searches), \
                     patch.object(worker, "state_lock",
                                  return_value=contextlib.nullcontext()), \
                     patch.object(worker, "load_config", return_value={"channel_id": "1"}), \
                     patch.object(worker, "run_lock",
                                  return_value=contextlib.nullcontext(True)), \
                     patch.object(worker, "open_emulator") as open_emulator, \
                     patch.object(worker, "emulator_is_online",
                                  return_value=(False, "")) as probe, \
                     patch.object(worker, "_notice",
                                  side_effect=lambda c, t, b, k: notices.append(b)), \
                     patch.object(worker, "_batch_run_locked") as run_locked, \
                     patch.object(maa_action, "_copilot_blackout_reason",
                                  return_value=gates["_copilot_blackout_reason"]), \
                     patch.object(maa_action, "depot_window_reason",
                                  return_value=gates["depot_window_reason"]), \
                     patch.object(maa_action, "min_lead_reason",
                                  return_value=gates["min_lead_reason"]):
                    with contextlib.redirect_stdout(io.StringIO()):
                        rc = worker.cmd_run_batch(token)
                state = json.loads((searches / f"{token}.json").read_text("utf-8"))
                self.assertEqual(rc, 1)
                self.assertEqual(state["status"], "scheduled")
                self.assertEqual(state["last_prepare_error"], "本轮未启动：闸门拦下了")
                open_emulator.assert_not_called()
                probe.assert_not_called()
                run_locked.assert_not_called()
                self.assertTrue(any("闸门拦下了" in body for body in notices))

    def test_a_clear_run_batch_still_reaches_the_locked_body(self) -> None:
        import maa_copilot_worker as worker

        token = "a" * 12
        with tempfile.TemporaryDirectory() as raw:
            searches = Path(raw) / "searches"
            searches.mkdir()
            (searches / f"{token}.json").write_text(
                json.dumps({
                    "token": token, "status": "queued",
                    "entries": [{"stage": "SR-8", "mode": "normal"}],
                }),
                encoding="utf-8",
            )
            with patch.object(maa_copilot, "SEARCHES_DIR", searches), \
                 patch.object(worker, "load_config", return_value={}), \
                 patch.object(worker, "run_lock",
                              return_value=contextlib.nullcontext(True)), \
                 patch.object(maa_action, "_copilot_blackout_reason", return_value=None), \
                 patch.object(maa_action, "depot_window_reason", return_value=None), \
                 patch.object(maa_action, "min_lead_reason", return_value=None), \
                 patch.object(worker, "_batch_run_locked", return_value=0) as run_locked:
                rc = worker.cmd_run_batch(token)
        self.assertEqual(rc, 0)
        run_locked.assert_called_once()

    def test_only_a_perfect_op_is_ever_accepted(self) -> None:
        # `ratio < 1.0` 或有软 / 硬缺口的作业一律不入候选（D12）。
        items = [
            {"id": 1, "ratio": 0.9, "gaps": ["史尔特尔（未拥有）"], "soft_gaps": []},
            {"id": 2, "ratio": 1.0, "gaps": [], "soft_gaps": ["莫斯提马 模组1"]},
            {"id": 3, "ratio": 1.0, "gaps": [], "soft_gaps": []},
        ]
        with patch.object(maa_copilot, "search_copilots", return_value=items) as search:
            candidate, reason, gaps = maa_copilot.pick_perfect_candidate(
                "SR-8", "act54side_08", {}, None
            )
        self.assertEqual(candidate["id"], 3)
        self.assertEqual((reason, gaps), ("", []))
        self.assertFalse(search.call_args.kwargs["use_cache"])
        self.assertEqual(search.call_args.kwargs["limit"], 1000)

        with patch.object(maa_copilot, "search_copilots", return_value=items[:2]):
            candidate, reason, gaps = maa_copilot.pick_perfect_candidate(
                "SR-8", "act54side_08", {}, None
            )
        self.assertIsNone(candidate)
        self.assertIn("练度", reason)
        self.assertIn("史尔特尔（未拥有）", gaps)


class CopilotDowngradeGuardTests(unittest.TestCase):
    """不变量 #15（收紧）：三个降级开关一个都不许出现在任何脚本里。"""

    def test_no_script_ever_mentions_a_downgrade_flag(self) -> None:
        for path in sorted((ROOT / "scripts").glob("*.py")):
            source = path.read_text(encoding="utf-8")
            for flag in ("--ignore-requirements", "--use-sanity-potion",
                         "--support-unit", "support_unit_usage", "support_unit_name"):
                with self.subTest(path=path.name, flag=flag):
                    self.assertNotIn(flag, source)

    def test_the_clearing_queue_is_gone_from_every_surface(self) -> None:
        needles = ("clearing_enabled", "needs_manual", "ex_gate", "sanity-probe",
                   "maa-clearing")
        targets = [
            *sorted((ROOT / "scripts").glob("*.py")),
            ROOT / "state" / "config.json",
            ROOT / "skills" / "games" / "maa-operator" / "SKILL.md",
            ROOT / "README.md",
            ROOT / "docs" / "DEV_GUIDE.md",
            ROOT / "docs" / "DISCORD_GUIDE.md",
        ]
        for path in targets:
            source = path.read_text(encoding="utf-8")
            for needle in needles:
                with self.subTest(path=path.name, needle=needle):
                    self.assertNotIn(needle, source)


class DeployPromptPatchTests(unittest.TestCase):
    """步骤 1 部署踩到的坑：Hermes 会把提示词重排成无引号、折行的 plain scalar。"""

    OLD = deploy.OLD_MAA_CHANNEL_PROMPTS[1]

    def _document(self, body: str) -> str:
        return (
            "timezone: Etc/UTC\n"
            "platforms:\n"
            "  discord:\n"
            "    channels:\n"
            "      - id: '123'\n"
            "        skills:\n"
            "          - maa-operator\n"
            "    channel_prompts:\n"
            f"{body}"
            "      '456': '别的频道'\n"
        )

    def test_a_reflowed_two_line_prompt_is_replaced_in_place(self) -> None:
        # 实例：Hermes 把这一条重排成无引号、在 `maa-operator` 后折行的 plain scalar。
        head, _space, tail = self.OLD.rpartition(" ")
        text = self._document(f"      '123': {head}\n        {tail}\n")
        patched = deploy.patch_config_yaml_text(text, "123")
        document = deploy.parse_yaml(patched)
        prompts = document["platforms"]["discord"]["channel_prompts"]
        self.assertEqual(prompts["123"], deploy.MAA_CHANNEL_PROMPT)
        self.assertEqual(prompts["456"], "别的频道")
        self.assertEqual(document["timezone"], "Etc/UTC")
        # 折行的续行整条被吃掉，旧文案一个字都不留。
        self.assertNotIn("只处理今天的关卡", patched)
        self.assertEqual(len(patched.splitlines()), len(text.splitlines()) - 1)

    def test_a_single_quoted_prompt_is_still_replaced(self) -> None:
        text = self._document(f"      '123': '{self.OLD}'\n")
        patched = deploy.patch_config_yaml_text(text, "123")
        prompts = deploy.parse_yaml(patched)["platforms"]["discord"]["channel_prompts"]
        self.assertEqual(prompts["123"], deploy.MAA_CHANNEL_PROMPT)

    def test_an_already_current_prompt_is_left_alone(self) -> None:
        text = self._document(f"      '123': '{deploy.MAA_CHANNEL_PROMPT}'\n")
        self.assertEqual(deploy.patch_config_yaml_text(text, "123"), text)

    def test_an_unexpected_prompt_still_refuses_to_deploy(self) -> None:
        text = self._document("      '123': 别人手工写的文案\n")
        with self.assertRaises(RuntimeError) as caught:
            deploy.patch_config_yaml_text(text, "123")
        self.assertIn("非预期文案", str(caught.exception))

    def test_a_missing_prompt_is_inserted(self) -> None:
        text = self._document("")
        patched = deploy.patch_config_yaml_text(text, "123")
        prompts = deploy.parse_yaml(patched)["platforms"]["discord"]["channel_prompts"]
        self.assertEqual(prompts["123"], deploy.MAA_CHANNEL_PROMPT)

    def test_retired_clearing_keys_are_swept_from_the_live_config(self) -> None:
        self.assertIn("clearing_enabled", deploy.RETIRED_CONFIG_KEYS)
        self.assertEqual(len(deploy.RETIRED_CONFIG_KEYS), 5)
        repo = json.loads((ROOT / "state" / "config.json").read_text("utf-8"))
        for key in deploy.RETIRED_CONFIG_KEYS:
            self.assertNotIn(key, repo)
        for key in ("auto_copilot_min_lead_minutes", "copilot_raid_switch_task",
                    "annihilation_sanity"):
            self.assertIn(key, repo)


class CopilotCardTests(unittest.TestCase):
    """2e：抄作业卡——候选集 = 自动路径的目标集合（含已通关的关，供重打）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.watch = self.root / "activity_watch.json"
        self.searches = self.root / "searches"
        self.searches.mkdir()
        self.nav_keys = {"SR-1", "SR-5", "SR-8", "1-7", "CE-6"}

    def _view(self, config, *, watch=None, ledger=None, roster=None, at=None):
        maa_common.atomic_write_json(self.watch, watch or {})
        with patch.object(maa_plan_card, "WATCH_PATH", self.watch), \
             patch.object(maa_copilot, "SEARCHES_DIR", self.searches), \
             patch.object(maa_copilot, "load_stage_index", return_value=SR_INDEX), \
             patch.object(maa_copilot, "load_ledger", return_value=ledger), \
             patch.object(maa_copilot, "load_roster", return_value=roster), \
             _nav_patch(self.nav_keys), \
             patch.object(
                 maa_plan_card, "stage_drops",
                 return_value={"SR-8": {"materials": ["异铁组"], "ap": 21}},
             ), \
             patch.object(maa_plan_card, "activity_deadline_text", return_value=""):
            return maa_plan_card.copilot_card_view(config, at)

    def test_the_candidate_set_is_the_whole_activity_not_just_the_recommended_stages(self) -> None:
        # `maa activity` 只推荐 4 关，目标集合是本地索引里整个前缀（2b）。
        _embed, options = self._view({"activity_stages": ["SR-8"]},
                                     watch={"activity_key": SR_KEY})
        self.assertEqual([stage for _l, stage, _d in options], ["SR-1", "SR-5", "SR-8"])

    def test_stages_without_upstream_navigation_are_listed_but_never_offered(self) -> None:
        embed, options = self._view({}, watch={"activity_key": SR_KEY})
        listing = embed["fields"][0]["value"]
        self.assertIn("SR-2", listing)
        self.assertIn("⛔", listing)
        self.assertNotIn("SR-2", [stage for _l, stage, _d in options])

    def test_each_stage_shows_its_own_progress(self) -> None:
        maa_common.atomic_write_json(
            self.searches / f"{'a' * 12}.json",
            {
                "status": "scheduled",
                "scheduled_at": "2026-09-09T14:30:00+08:00",
                "entries": [{"stage": "SR-1", "mode": "normal"}],
            },
        )
        ledger = {"stages": {
            "SR-5": {"normal": "t", "raid": "t"},
            "SR-8": {"normal": "t", "raid": None},
        }}
        embed, options = self._view(
            {}, watch={"activity_key": SR_KEY, "batch_token": "a" * 12},
            ledger=ledger,
        )
        listing = embed["fields"][0]["value"]
        self.assertIn("`SR-5` · ✅ 已通", listing)
        self.assertIn("🔁 普通已通、突袭待补", listing)
        self.assertIn("⏳ 排程 14:30", listing)
        # 已通的关照样能选——手动路径允许重打（D14）。
        self.assertIn("SR-5", [stage for _l, stage, _d in options])

    def test_with_the_raid_switch_off_a_normal_clear_says_so_on_the_card(self) -> None:
        """开关关掉时 ✅ 只代表普通难度打过了，卡上必须写明白，不能让人误以为突袭也通了。"""
        ledger = {"stages": {"SR-5": {"normal": "t", "raid": None}}}
        embed, _options = self._view(
            dict(RAID_OFF), watch={"activity_key": SR_KEY}, ledger=ledger
        )
        listing = embed["fields"][0]["value"]
        self.assertIn("`SR-5` · ✅ 已通（突袭功能未启用）", listing)
        self.assertNotIn("突袭待补", listing)
        # 非活动关不带这句尾巴（它们本来就没有突袭）。
        embed, _options = self._view(
            {**RAID_OFF, "all_supply_stages_open": True}, watch={},
            ledger={"stages": {"CE-6": {"normal": "t", "raid": None}}},
            at=datetime(2026, 9, 9, 12, 0, tzinfo=BEIJING),
        )
        self.assertNotIn("突袭功能未启用", embed["fields"][0]["value"])

    def test_a_stage_at_its_daily_cap_says_so_but_stays_selectable(self) -> None:
        at = datetime(2026, 9, 9, 12, 0, tzinfo=BEIJING)
        ledger = maa_copilot.new_ledger(SR_KEY)
        for hour in (9, 8):
            maa_copilot.record_copilot_attempt(
                ledger, "SR-5", "normal", outcome="unknown", at=at - timedelta(hours=hour)
            )
        embed, options = self._view({}, watch={"activity_key": SR_KEY},
                                    ledger=ledger, at=at)
        self.assertIn("🚫 今日已尝试 2 次", embed["fields"][0]["value"])
        # 只挡自动排轮：手动想重打照样能从下拉里选（D14）。
        self.assertIn("SR-5", [stage for _l, stage, _d in options])

    def test_a_stage_that_failed_the_op_search_shows_what_is_missing(self) -> None:
        watch = {"activity_key": SR_KEY, "stage_state": {}}
        maa_clearing.record_stage_failure(
            watch, "SR-8", kind="op", detail="SR-8 暂无完全满足当前干员练度的作业",
            gaps=["史尔特尔 专精3（你技能7）"],
        )
        embed, _options = self._view({}, watch=watch)
        self.assertIn("🚫 练度不满足（缺：史尔特尔 专精3（你技能7））",
                      embed["fields"][0]["value"])

    def test_without_an_activity_it_degrades_to_supplies_and_main(self) -> None:
        embed, options = self._view(
            {"all_supply_stages_open": True}, watch={},
            at=datetime(2026, 9, 9, 12, 0, tzinfo=BEIJING),
        )
        stages = [stage for _label, stage, _desc in options]
        self.assertIn("1-7", stages)
        self.assertIn("CE-6", stages)
        self.assertNotIn("SR-8", stages)
        self.assertIn("资源本 / 主线", embed["fields"][0]["name"])

    def test_the_select_values_are_opaque_and_resolved_through_the_registry(self) -> None:
        rows, entries = maa_action._copilot_card_components(
            [("SR-8 · 异铁组", "SR-8", "🕐 待抄")]
        )
        custom_id = "hermes:btn:maa-copilot:card:stage-select"
        self.assertEqual(rows[0]["components"][0]["custom_id"], custom_id)
        option = rows[0]["components"][0]["options"][0]
        self.assertEqual(option["value"], "choice:0")
        self.assertNotIn("SR-8", option["value"])
        self.assertEqual(
            entries[custom_id]["select_args"]["choice:0"],
            ["copilot", "find", "SR-8"],
        )

    def test_no_navigable_stage_means_no_control_at_all(self) -> None:
        rows, entries = maa_action._copilot_card_components([])
        self.assertEqual(rows, [])
        self.assertEqual(entries, {})

    def _send_card(self, options, registry_seed, card_state):
        posted: list[tuple] = []
        buttons = self.root / "buttons.json"
        card_path = self.root / "card.json"
        maa_common.atomic_write_json(buttons, registry_seed)
        if card_state is not None:
            maa_common.atomic_write_json(card_path, card_state)
        with patch.object(
            maa_action, "load_config", return_value={"channel_id": "123"}
        ), patch.object(
            maa_plan_card, "copilot_card_view", return_value=({"title": "t"}, options)
        ), patch.object(maa_copilot, "CARD_PATH", card_path), patch.object(
            maa_copilot, "ensure_private_dirs"
        ), patch.object(maa_action, "BUTTONS_PATH", buttons), patch.object(
            maa_action, "state_lock", return_value=contextlib.nullcontext()
        ), patch.object(maa_action, "retire_discord_components") as retire, patch.object(
            maa_action, "discord_token", return_value="token"
        ), patch.object(
            maa_action, "discord_request",
            side_effect=lambda *a, **k: posted.append(a) or {"id": "999"},
        ):
            ok, message = maa_action.copilot_card()
        return SimpleNamespace(
            ok=ok, message=message, retire=retire, posted=posted,
            registry=json.loads(buttons.read_text(encoding="utf-8")),
            card=json.loads(card_path.read_text(encoding="utf-8")),
        )

    def test_a_new_card_retires_the_previous_one_and_its_registry_entry(self) -> None:
        stale = "hermes:btn:maa-copilot:card:stage-select"
        result = self._send_card(
            [("SR-8", "SR-8", "d")],
            {stale: {"select_args": {"choice:0": ["copilot", "find", "OLD-1"]}},
             "hermes:btn:agent-book:x": {}},
            {"message_id": "555"},
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.retire.call_args.args[1], "555")
        self.assertEqual(
            result.registry[stale]["select_args"]["choice:0"],
            ["copilot", "find", "SR-8"],
        )
        self.assertIn("hermes:btn:agent-book:x", result.registry)
        self.assertEqual(result.card["message_id"], "999")

    def test_a_card_without_options_leaves_no_stale_registry_entry(self) -> None:
        stale = "hermes:btn:maa-copilot:card:stage-select"
        result = self._send_card([], {stale: {}}, {"message_id": "555"})
        self.assertTrue(result.ok)
        self.assertNotIn(stale, result.registry)
        self.assertIn("没有上游写了导航定义的关", result.message)

    def test_the_card_prefix_survives_both_sweeps(self) -> None:
        """它既不进 07:50 的 `hermes:btn:maa:` 清扫，也不被搜索 TTL 清扫误删。"""
        card_key = f"{maa_copilot.CARD_BUTTON_PREFIX}stage-select"
        self.assertFalse(card_key.startswith(maa_plan_card.BUTTON_PREFIX))
        buttons = self.root / "sweep-buttons.json"
        maa_common.atomic_write_json(
            buttons, {"hermes:btn:maa:am:stage-select": {}, card_key: {}}
        )
        with patch.object(maa_common, "BUTTONS_PATH", buttons):
            maa_common.prune_button_registry(maa_plan_card.BUTTON_PREFIX)
        registry = json.loads(buttons.read_text(encoding="utf-8"))
        self.assertNotIn("hermes:btn:maa:am:stage-select", registry)
        self.assertIn(card_key, registry)
        (self.searches / f"{'d' * 12}.json").write_text(
            json.dumps({"expires_at": "2020-01-01T00:00:00+08:00"}), encoding="utf-8"
        )
        with patch.object(maa_copilot, "SEARCHES_DIR", self.searches), patch.object(
            maa_copilot, "ensure_private_dirs"
        ):
            maa_copilot.prune_expired_searches(registry)
        self.assertIn(card_key, registry)

    def test_the_card_state_file_is_not_inside_the_searches_directory(self) -> None:
        self.assertNotEqual(maa_copilot.CARD_PATH.parent, maa_copilot.SEARCHES_DIR)
        self.assertFalse(maa_copilot.SEARCH_TOKEN_RE.fullmatch("card"))


class ActivityWatchTickTests(unittest.TestCase):
    """watcher 一 tick 的整链：轮换 → 排轮 → 发已排程卡 → 派发。"""

    SNAPSHOT = {
        "stages": ["SR-8", "SR-7"],
        "events": [{"title": "SideStory「月行水上」", "stages": ["SR-8", "SR-7"]}],
        "all_supply_stages_open": False,
    }

    def _tick(self, state, *, snapshot=None, entries=None, targets=None,
              blocker="", notify=None, seed_rounds=None, skland_error=None,
              paused=False, config=None):
        import maa_activity_watch as watch

        dispatch = unittest.mock.MagicMock()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            watch_path = root / "activity_watch.json"
            searches = root / "searches"
            searches.mkdir()
            pause_path = root / "auto_pause.json"
            if paused:
                maa_common.atomic_write_json(pause_path, {"paused": True})
            for token, round_state in (seed_rounds or {}).items():
                maa_common.atomic_write_json(searches / f"{token}.json", round_state)
            maa_common.atomic_write_json(watch_path, state)
            ledger = maa_copilot.new_ledger(SR_KEY)
            patches = [
                patch.object(watch, "WATCH_PATH", watch_path),
                patch.object(maa_copilot, "SEARCHES_DIR", searches),
                patch.object(maa_copilot, "AUTO_PAUSE_PATH", pause_path),
                patch.object(watch, "_supersede_round",
                             side_effect=lambda token, cfg: maa_copilot.supersede_search(token)),
                patch.object(maa_copilot, "state_lock",
                             return_value=contextlib.nullcontext()),
                patch.object(maa_copilot, "BUTTONS_PATH", root / "buttons.json"),
                patch.object(watch, "load_config",
                             return_value={"channel_id": "1", **(config or {})}),
                patch.object(watch, "state_lock", return_value=contextlib.nullcontext()),
                patch.object(maa_plan_card, "query_activity_snapshot",
                             return_value=snapshot if snapshot is not None else self.SNAPSHOT),
                patch.object(maa_plan_card, "persist_activity_snapshot"),
                patch.object(watch, "_notify",
                             side_effect=notify or (lambda *a, **k: None)),
                patch.object(maa_copilot, "ensure_ledger", return_value=(ledger, "")),
                patch.object(maa_copilot, "load_stage_index", return_value=SR_INDEX),
                patch.object(maa_copilot, "load_roster", return_value=None),
                patch.object(maa_copilot, "ensure_private_dirs"),
                patch.object(
                    watch, "collect_targets",
                    return_value=(targets if targets is not None else [], []),
                ),
                patch.object(
                    watch, "build_round",
                    return_value=(entries or [], "2026-09-09T12:00:00+08:00", ""),
                ),
                patch.object(maa_action, "dispatch_blocker", return_value=blocker),
                patch.object(maa_action, "publish_round", return_value="888"),
                patch.object(maa_action, "dispatch_batch", dispatch),
            ]
            with contextlib.ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                import maa_skland

                stack.enter_context(
                    patch.object(maa_skland, "refresh_roster", return_value={},
                                 side_effect=skland_error)
                )
                result = watch.run_once()
                rounds = [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in sorted(searches.glob("*.json"))
                ]
        return SimpleNamespace(state=result, rounds=rounds, dispatch=dispatch)

    TARGETS = [{"stage": "SR-1", "stage_id": "act54side_01", "modes": ["normal"]}]
    ENTRIES = [{"stage": "SR-1", "stage_id": "act54side_01", "mode": "normal",
                "candidate": {"id": 1}, "sanity_cost": 12}]

    def test_a_skland_failure_blocks_scheduling_instead_of_using_the_cache(self) -> None:
        """2026-10-11：森空岛过期一个月，缓存里没有当期 zone 进度，YW-4 没解锁也被排。"""
        notices: list[str] = []
        result = self._tick(
            {"activity_key": SR_KEY, "prefix": "SR"},
            targets=self.TARGETS, entries=self.ENTRIES,
            skland_error=RuntimeError("cred 过期且重认证失败"),
            notify=lambda config, title, body, color: notices.append(title),
        )
        self.assertEqual(result.state["status"], "blocked")
        self.assertEqual(result.rounds, [])
        result.dispatch.assert_not_called()
        self.assertEqual(len(notices), 1)
        # 同一原因下一 tick 不再刷屏。
        again = self._tick(
            result.state, targets=self.TARGETS, entries=self.ENTRIES,
            skland_error=RuntimeError("cred 过期且重认证失败"),
            notify=lambda config, title, body, color: notices.append(title),
        )
        self.assertEqual(again.state["status"], "blocked")
        self.assertEqual(len(notices), 1)

    def test_prefight_failures_stop_scheduling_for_the_day_at_the_cap(self) -> None:
        """开打前就挂的轮不记账、到不了 daily_cap：另记一本按天的账。"""
        notices: list[str] = []
        token = "b" * 12
        failed_round = {
            "token": token, "status": "execution_failed", "source": "activity-watch",
            "entries": self.ENTRIES, "prefight_error": "森空岛实时理智读取失败：x",
        }
        first = self._tick(
            {"activity_key": SR_KEY, "prefix": "SR", "batch_token": token},
            targets=self.TARGETS, entries=self.ENTRIES, blocker="run.lock 被占",
            seed_rounds={token: failed_round},
            notify=lambda config, title, body, color: notices.append(title),
        )
        self.assertEqual(first.state["prefight_failures"]["count"], 1)
        self.assertEqual(first.state["status"], "scheduled")  # 一次还没到上限，照排
        second = self._tick(
            {**first.state, "batch_token": token},
            targets=self.TARGETS, entries=self.ENTRIES,
            seed_rounds={token: failed_round},
            notify=lambda config, title, body, color: notices.append(title),
        )
        self.assertEqual(second.state["prefight_failures"]["count"], 2)
        self.assertEqual(second.state["status"], "blocked")
        second.dispatch.assert_not_called()
        self.assertEqual(sum("不再排轮" in title for title in notices), 1)
        third = self._tick(
            second.state, targets=self.TARGETS, entries=self.ENTRIES,
            notify=lambda config, title, body, color: notices.append(title),
        )
        self.assertEqual(third.state["status"], "blocked")
        self.assertEqual(sum("不再排轮" in title for title in notices), 1)

    def test_prefight_failures_from_yesterday_do_not_block_today(self) -> None:
        result = self._tick(
            {"activity_key": SR_KEY, "prefix": "SR",
             "prefight_failures": {"date": "2000-01-01", "count": 9}},
            targets=self.TARGETS, entries=self.ENTRIES,
        )
        self.assertEqual(result.state["status"], "dispatched")

    def test_pausing_supersedes_the_scheduled_round_and_schedules_nothing(self) -> None:
        token = "c" * 12
        result = self._tick(
            {"activity_key": SR_KEY, "prefix": "SR", "batch_token": token},
            targets=self.TARGETS, entries=self.ENTRIES, paused=True,
            seed_rounds={token: {"token": token, "status": "scheduled",
                                 "source": "activity-watch", "entries": self.ENTRIES}},
        )
        self.assertEqual(result.state["status"], "paused")
        self.assertNotIn("batch_token", result.state)
        self.assertEqual([r["status"] for r in result.rounds], ["superseded"])
        result.dispatch.assert_not_called()

    def test_a_new_activity_resets_the_watch_state_and_announces_itself(self) -> None:
        notices: list[str] = []
        result = self._tick(
            {"activity_key": "旧活动|AT", "stage_state": {"AT-1": {}}},
            notify=lambda config, title, body, color: notices.append(title),
        )
        self.assertEqual(result.state["activity_key"], SR_KEY)
        self.assertEqual(result.state["prefix"], "SR")
        self.assertEqual(result.state["stage_state"], {})
        self.assertTrue(any("已上线" in title for title in notices))

    def test_nothing_left_to_do_parks_the_watcher(self) -> None:
        result = self._tick({"activity_key": SR_KEY, "prefix": "SR"}, targets=[])
        self.assertEqual(result.state["status"], "done")
        self.assertEqual(result.rounds, [])

    def test_a_planned_round_is_written_published_and_dispatched(self) -> None:
        entries = [{"stage": "SR-1", "stage_id": "act54side_01", "mode": "normal",
                    "candidate": {"id": 1}, "sanity_cost": 12}]
        result = self._tick(
            {"activity_key": SR_KEY, "prefix": "SR"},
            targets=[{"stage": "SR-1", "stage_id": "act54side_01", "modes": ["normal"]}],
            entries=entries,
        )
        self.assertEqual(result.state["status"], "dispatched")
        self.assertEqual(len(result.rounds), 1)
        self.assertEqual(result.rounds[0]["source"], "activity-watch")
        self.assertEqual(result.rounds[0]["activity_key"], SR_KEY)
        self.assertEqual(result.rounds[0]["message_id"], "888")
        self.assertEqual(result.rounds[0]["entries"], entries)

    def test_a_blocked_round_is_written_but_left_scheduled(self) -> None:
        entries = [{"stage": "SR-1", "stage_id": "act54side_01", "mode": "normal",
                    "candidate": {"id": 1}, "sanity_cost": 12}]
        result = self._tick(
            {"activity_key": SR_KEY, "prefix": "SR"},
            targets=[{"stage": "SR-1", "stage_id": "act54side_01", "modes": ["normal"]}],
            entries=entries,
            blocker="run.lock 被占",
        )
        self.assertEqual(result.state["status"], "scheduled")
        self.assertEqual(result.rounds[0]["status"], "scheduled")

    def test_the_watcher_never_adopts_a_manual_scheduled_round(self) -> None:
        """手动轮被闸门挡下停在 `scheduled`，watcher 不许替它重试（第 3 点）。

        watcher 只跟踪它自己写进 `state["batch_token"]` 的那个 token，
        `source == "discord"` 的轮子对它是不存在的。
        """
        manual = "f" * 12
        result = self._tick(
            {"activity_key": SR_KEY, "prefix": "SR"},
            targets=[],
            seed_rounds={
                manual: {
                    "token": manual,
                    "status": "scheduled",
                    "source": "discord",
                    "stage": "SR-8",
                    "entries": [{"stage": "SR-8", "mode": "normal"}],
                    "last_prepare_error": "距 08:00 日常场次不足 25 分钟",
                }
            },
        )
        # 没有 batch_token → watcher 连看都不看它，更不会派发。
        self.assertNotIn("batch_token", result.state)
        result.dispatch.assert_not_called()
        self.assertEqual(len(result.rounds), 1)
        self.assertEqual(result.rounds[0]["status"], "scheduled")
        self.assertEqual(result.rounds[0]["source"], "discord")
        self.assertNotIn("execution_requested_at", result.rounds[0])

    def test_the_activity_ending_needs_two_empty_snapshots(self) -> None:
        empty = {"stages": [], "events": [], "all_supply_stages_open": False}
        first = self._tick({"activity_key": SR_KEY}, snapshot=empty)
        self.assertEqual(first.state["empty_observations"], 1)
        self.assertEqual(first.state["activity_key"], SR_KEY)
        notices: list[str] = []
        second = self._tick(
            {"activity_key": SR_KEY, "empty_observations": 1}, snapshot=empty,
            notify=lambda config, title, body, color: notices.append(title),
        )
        self.assertEqual(second.state["status"], "idle")
        self.assertEqual(second.state["activity_key"], "")
        self.assertTrue(any("活动已结束" in title for title in notices))


class SklandDailyHealthTests(unittest.TestCase):
    """森空岛过期：每个北京日在频道里提醒一次，直到恢复。"""

    def _check(self, *, skland, error=None, at=None):
        import maa_skland

        notices: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "skland.json"
            if skland is not None:
                maa_common.atomic_write_json(path, skland)
            with patch.object(maa_skland, "SKLAND_PATH", path), \
                 patch.object(maa_copilot, "ensure_private_dirs"), \
                 patch.object(maa_skland, "refresh_roster", side_effect=error,
                              return_value={}):
                status = maa_skland.daily_health_check(
                    lambda title, body: notices.append((title, body)), at
                )
            stored = json.loads(path.read_text("utf-8"))
        return status, notices, stored

    AT = datetime(2026, 10, 12, 7, 50, tzinfo=maa_common.BEIJING)

    def test_an_expired_login_is_announced_with_the_fix(self) -> None:
        status, notices, stored = self._check(
            skland={"hg_token": "t"},
            error=RuntimeError("cred 过期且重认证失败；请在本机终端运行 ... link"),
            at=self.AT,
        )
        self.assertEqual(status, "notified")
        self.assertEqual(len(notices), 1)
        self.assertIn("过期", notices[0][0])
        self.assertIn("maa_skland.py link", notices[0][1])
        self.assertEqual(stored["health_checked_on"], "2026-10-12")
        self.assertEqual(stored["hg_token"], "t")

    def test_only_one_notice_per_day(self) -> None:
        status, notices, _ = self._check(
            skland={"hg_token": "t", "health_checked_on": "2026-10-12"},
            error=RuntimeError("过期"), at=self.AT,
        )
        self.assertEqual(status, "checked")
        self.assertEqual(notices, [])

    def test_the_next_day_reminds_again(self) -> None:
        status, notices, _ = self._check(
            skland={"hg_token": "t", "health_checked_on": "2026-10-11"},
            error=RuntimeError("cred 过期"), at=self.AT,
        )
        self.assertEqual(status, "notified")
        self.assertEqual(len(notices), 1)

    def test_a_healthy_login_stays_quiet(self) -> None:
        status, notices, stored = self._check(skland={"hg_token": "t"}, at=self.AT)
        self.assertEqual(status, "ok")
        self.assertEqual(notices, [])
        self.assertEqual(stored["health_checked_on"], "2026-10-12")

    def test_a_failed_discord_post_leaves_the_day_unchecked(self) -> None:
        import maa_skland

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "skland.json"
            maa_common.atomic_write_json(path, {"hg_token": "t"})

            def notify(_t, _b):
                raise RuntimeError("discord down")

            with patch.object(maa_skland, "SKLAND_PATH", path), \
                 patch.object(maa_skland, "refresh_roster",
                              side_effect=RuntimeError("过期")):
                with self.assertRaises(RuntimeError):
                    maa_skland.daily_health_check(notify, self.AT)
            self.assertNotIn("health_checked_on", json.loads(path.read_text("utf-8")))

    def test_a_successful_refresh_clears_the_stale_error(self) -> None:
        import maa_skland

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "skland.json"
            maa_common.atomic_write_json(
                path, {"hg_token": "t", "cred": "c", "sign_token": "s", "uid": "1",
                       "last_error": "cred 过期"}
            )
            with patch.object(maa_skland, "SKLAND_PATH", path), \
                 patch.object(maa_skland, "ROSTER_PATH", root / "roster.json"), \
                 patch.object(maa_skland, "fetch_roster",
                              return_value={"chars": {}, "activity_progress": {}}):
                maa_skland.refresh_roster()
            self.assertNotIn("last_error", json.loads(path.read_text("utf-8")))


class CopilotAutoPauseCommandTests(unittest.TestCase):
    """`copilot auto off|on|status`：Discord 里能叫停自动作业。"""

    def _run(self, value, *, watch=None, rounds=None, paused=False):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            searches = root / "searches"
            searches.mkdir()
            for token, state in (rounds or {}).items():
                maa_common.atomic_write_json(searches / f"{token}.json", state)
            watch_path = root / "activity_watch.json"
            maa_common.atomic_write_json(watch_path, watch or {})
            pause_path = root / "auto_pause.json"
            if paused:
                maa_common.atomic_write_json(pause_path, {"paused": True})
            with patch.object(maa_copilot, "SEARCHES_DIR", searches), \
                 patch.object(maa_copilot, "AUTO_PAUSE_PATH", pause_path), \
                 patch.object(maa_copilot, "ensure_private_dirs"), \
                 patch.object(maa_copilot, "state_lock",
                              return_value=contextlib.nullcontext()), \
                 patch.object(maa_copilot, "BUTTONS_PATH", root / "buttons.json"), \
                 patch.object(maa_action, "WATCH_PATH", watch_path), \
                 patch.object(maa_action, "load_config", return_value={"channel_id": "1"}), \
                 patch.object(maa_action, "retire_discord_components") as retire:
                ok, message = maa_action.dispatch(["copilot", "auto", value])
                paused_now = maa_copilot.auto_pause_state() is not None
            final_rounds = {
                path.stem: json.loads(path.read_text("utf-8"))
                for path in searches.glob("*.json")
            }
            final_watch = json.loads(watch_path.read_text("utf-8"))
        return SimpleNamespace(ok=ok, message=message, paused=paused_now,
                               rounds=final_rounds, watch=final_watch, retire=retire)

    def test_off_pauses_and_supersedes_the_scheduled_round(self) -> None:
        token = "d" * 12
        result = self._run(
            "off", watch={"batch_token": token},
            rounds={token: {"token": token, "status": "scheduled", "message_id": "5"}},
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.paused)
        self.assertEqual(result.rounds[token]["status"], "superseded")
        result.retire.assert_called_once()

    def test_off_never_touches_an_executing_round(self) -> None:
        token = "e" * 12
        result = self._run(
            "off", watch={"batch_token": token},
            rounds={token: {"token": token, "status": "executing"}},
        )
        self.assertEqual(result.rounds[token]["status"], "executing")
        self.assertIn("打完就停", result.message)

    def test_on_resumes_and_lifts_the_prefight_block(self) -> None:
        result = self._run(
            "on", paused=True,
            watch={"prefight_failures": {"date": "2026-10-11", "count": 2},
                   "blocked_reason": "x"},
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.paused)
        self.assertNotIn("prefight_failures", result.watch)

    def test_status_and_bad_values(self) -> None:
        self.assertIn("已暂停", self._run("status", paused=True).message)
        self.assertIn("运行中", self._run("status").message)
        self.assertFalse(self._run("maybe").ok)

    def test_the_pause_buttons_survive_round_pruning(self) -> None:
        registry = dict(maa_copilot.AUTO_BUTTON_REGISTRY)
        maa_copilot.prune_search_buttons("a" * 12, registry)
        self.assertEqual(registry, maa_copilot.AUTO_BUTTON_REGISTRY)
        self.assertEqual(
            registry[maa_copilot.AUTO_PAUSE_BUTTON_ID]["args"], ["copilot", "auto", "off"]
        )

    def test_only_auto_rounds_get_a_pause_button(self) -> None:
        entries = [{"stage": "SR-1", "mode": "normal", "sanity_cost": 12}]
        _e, manual, manual_reg = maa_copilot.build_scheduled_card(
            "a" * 12, entries, title="t")
        _e, auto, auto_reg = maa_copilot.build_scheduled_card(
            "a" * 12, entries, title="t", pause_button=True)
        self.assertEqual(len(manual[0]["components"]), 1)
        self.assertEqual(len(auto[0]["components"]), 2)
        self.assertIn(maa_copilot.AUTO_PAUSE_BUTTON_ID, auto_reg)
        self.assertNotIn(maa_copilot.AUTO_PAUSE_BUTTON_ID, manual_reg)


# ---------------------------------------------------------------------------
# 四期步骤 4：肉鸽
# ---------------------------------------------------------------------------

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

    def test_every_round_starts_the_game_itself(self) -> None:
        """2026-09-11 事故：任务文件没有 StartUp，模拟器起来了但游戏没起来。

        开机链（`open_emulator / wait_for_android / verify_game_installed /
        settle_android_runtime`）只管模拟器，拉起游戏的只能是 MaaCore 的 `StartUp`。
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
        open_emulator = unittest.mock.MagicMock()

        def fake_run(config, env, log_path, index):
            calls.append(index)
            item = runs[min(index - 1, len(runs) - 1)]
            return item(self.state_path) if callable(item) else item

        def gate(value):
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
            patch.object(worker, "emulator_is_online", return_value=online),
            patch.object(worker, "require_executable"),
            patch.object(worker, "open_emulator", open_emulator),
            patch.object(worker, "wait_for_android"),
            patch.object(worker, "verify_game_installed"),
            patch.object(worker, "settle_android_runtime"),
            patch.object(worker, "maa_subprocess_env", return_value={}),
            patch.object(worker, "capture_screenshot", return_value=(True, "")),
            patch.object(worker, "close_everything", return_value=[]),
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
            open_emulator=open_emulator,
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
        result.open_emulator.assert_not_called()
        self.assertIn("25 分钟", self._field(result.report, "停止原因"))
        self.assertTrue(any("未启动" in title for title, _body in result.notices))
        self.assertEqual(result.state["status"], "idle")

    def test_the_depot_window_stops_it_before_the_emulator_is_opened(self) -> None:
        result = self._run([self.CLEAN], depot=lambda: "处于 Depot 对表窗口")
        self.assertEqual(result.calls, [])
        result.open_emulator.assert_not_called()
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
        result.open_emulator.assert_called_once()
        self.assertIn("25 分钟", self._field(result.report, "停止原因"))
        self.assertFalse([t for t, _b in result.notices if "未启动" in t])
        self.assertEqual(result.state["status"], "idle")

    def test_a_busy_emulator_touches_nothing(self) -> None:
        result = self._run([self.CLEAN], online=(True, "BlueStacks 进程在跑"))
        self.assertEqual(result.calls, [])
        result.open_emulator.assert_not_called()
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
                raise subprocess.TimeoutExpired("maa", timeout)
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
        source = (ROOT / "deploy.py").read_text(encoding="utf-8")
        self.assertEqual(source.count('"maa_roguelike.py"'), 2)
        self.assertEqual(source.count('"maa_roguelike_worker.py"'), 2)

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

    def _state(self, minutes_ago: int, status: str = "running") -> dict:
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


class DropReportTests(unittest.TestCase):
    """步骤 5：日常掉落匿名上报一图流 / 企鹅物流（回馈步骤 3 的数据源）。"""

    def setUp(self) -> None:
        self.plan = maa_common.normalize_plan({"stage": "1-7", "tasks": {}})
        self.plan["farming_drops"] = {"30013": 40}
        self.plan["farming_fallback_stage"] = "CE-6"
        self.config = {
            "medicine_expire_days": 2,
            "fight_times": 2147483647,
            "recruit_times": 4,
        }

    def _fight_blocks(self, rendered: str) -> list[str]:
        return [block for block in rendered.split("[[tasks]]") if 'type = "Fight"' in block]

    def test_every_fight_block_reports_anonymously_by_default(self) -> None:
        rendered = maa_daily.render_daily_toml(self.plan, self.config)
        blocks = self._fight_blocks(rendered)
        # 每日剿灭 + 刷理智 + 刷理智·续。
        self.assertEqual(len(blocks), 3)
        for block in blocks:
            self.assertIn("report_to_yituliu = true", block)
            self.assertIn("report_to_penguin = true", block)
            # ID 一律空串 = 匿名，任何情况下都不带账号标识。
            self.assertIn('yituliu_id = ""', block)
            self.assertIn('penguin_id = ""', block)

    def test_the_switch_removes_every_report_line_and_nothing_else(self) -> None:
        on = maa_daily.render_daily_toml(self.plan, self.config)
        off = maa_daily.render_daily_toml(
            self.plan, {**self.config, "report_drops": False}
        )
        for marker in ("report_to_yituliu", "report_to_penguin", "yituliu_id", "penguin_id"):
            self.assertNotIn(marker, off)
        # 差别**只有**上报那四行：把它们删掉两份必须一模一样。
        stripped = "\n".join(
            line
            for line in on.splitlines()
            if not line.startswith(("report_to_", "yituliu_id", "penguin_id"))
        )
        self.assertEqual(stripped, off.rstrip("\n") if off.endswith("\n") else off)

    def test_the_safety_literals_are_untouched_by_reporting(self) -> None:
        for report in (True, False):
            rendered = maa_daily.render_daily_toml(
                self.plan, {**self.config, "report_drops": report}
            )
            with self.subTest(report=report):
                for block in self._fight_blocks(rendered):
                    self.assertIn("medicine = 0", block)
                    self.assertIn("stone = 0", block)
                    self.assertIn("series = 0", block)
                self.assertNotIn("stone = 1", rendered)


class AwardAndMallOptionTests(unittest.TestCase):
    """步骤 5：Award 三个开关 + Mall 细化，全部从 `config.json` 读。"""

    def setUp(self) -> None:
        self.plan = maa_common.normalize_plan({"stage": "1-7", "tasks": {}})
        self.config = {
            "medicine_expire_days": 2,
            "fight_times": 2147483647,
            "recruit_times": 4,
        }

    def _award(self, config: dict) -> str:
        rendered = maa_daily.render_daily_toml(self.plan, config)
        return next(b for b in rendered.split("[[tasks]]") if 'type = "Award"' in b)

    def _mall(self, config: dict) -> str:
        rendered = maa_daily.render_daily_toml(self.plan, config)
        return next(b for b in rendered.split("[[tasks]]") if 'type = "Mall"' in b)

    def test_award_defaults_match_the_behaviour_before_the_switches(self) -> None:
        block = self._award(self.config)
        for line in ("orundum = false", "mining = false", "specialaccess = false"):
            self.assertIn(line, block)
        # 领日常 / 周常 / 邮件这三条不受开关影响。
        self.assertIn("award = true", block)
        self.assertIn("mail = true", block)

    def test_award_switches_come_from_config(self) -> None:
        block = self._award(
            {
                **self.config,
                "award_orundum": True,
                "award_mining": True,
                "award_specialaccess": True,
            }
        )
        for line in ("orundum = true", "mining = true", "specialaccess = true"):
            self.assertIn(line, block)

    def test_a_non_boolean_switch_is_read_as_off(self) -> None:
        block = self._award({**self.config, "award_orundum": "yes"})
        self.assertIn("orundum = false", block)

    def test_mall_defaults_stay_empty_and_off(self) -> None:
        block = self._mall(self.config)
        self.assertIn("buy_first = []", block)
        self.assertIn("blacklist = []", block)
        self.assertIn("reserve_max_credit = false", block)
        self.assertIn("only_buy_discount = false", block)
        # 信用作战仍然固定关（借助战，不变量 #15 的精神）。
        self.assertIn("credit_fight = false", block)

    def test_mall_lists_come_from_config_and_go_through_the_whitelist(self) -> None:
        block = self._mall(
            {
                **self.config,
                "mall_buy_first": [
                    "招聘许可",
                    "技能概要·卷3",
                    '坏"名字',          # 引号会撬开 TOML 字符串
                    "x" * 40,           # 超长
                    "赤金\n注入",        # 换行
                    42,                  # 根本不是字符串
                ],
                "mall_blacklist": ["碳"],
                "mall_reserve_max_credit": True,
                "mall_only_buy_discount": True,
            }
        )
        self.assertIn('buy_first = ["招聘许可", "技能概要·卷3"]', block)
        self.assertIn('blacklist = ["碳"]', block)
        self.assertIn("reserve_max_credit = true", block)
        self.assertIn("only_buy_discount = true", block)
        for rejected in ('坏"名字', "x" * 40, "注入"):
            self.assertNotIn(rejected, block)

    def test_a_broken_list_never_takes_the_whole_render_down(self) -> None:
        block = self._mall({**self.config, "mall_buy_first": "招聘许可"})
        self.assertIn("buy_first = []", block)


class OperBoxFallbackTests(unittest.TestCase):
    """步骤 5：森空岛不可用时，Depot 那趟顺手拿一份 OperBox 练度快照。"""

    def _message(self, done: bool, own: list[dict]) -> str:
        return "OperBox: " + json.dumps(
            {
                "taskchain": "OperBox",
                "what": "OperBoxInfo",
                "details": {"done": done, "all_opers": [], "own_opers": own},
            },
            ensure_ascii=False,
            indent=2,
        )

    def _own(self, char_id: str = "char_350_surtr") -> dict:
        return {
            "id": char_id,
            "name": "史尔特尔",
            "own": True,
            "elite": 2,
            "level": 90,
            "potential": 1,
            "rarity": 6,
        }

    def test_parser_accepts_only_the_final_complete_callback(self) -> None:
        output = "\n".join(
            [
                self._message(False, [self._own()]),
                self._message(True, [self._own(), self._own("char_002_amiya")]),
            ]
        )
        chars = maa_depot.parse_operbox_output(output)
        self.assertEqual(sorted(chars), ["char_002_amiya", "char_350_surtr"])
        entry = chars["char_350_surtr"]
        self.assertEqual(entry["elite"], 2)
        self.assertEqual(entry["level"], 90)
        self.assertEqual(entry["potential"], 1)
        # 读不到的两项留空 = 不满足，不是「0 级」也不是「没要求」。
        self.assertEqual(entry["skills"], [])
        self.assertEqual(entry["module_levels"], [])
        self.assertEqual(entry["main_skill_lvl"], 0)

    def test_a_partial_or_unusable_result_is_a_visible_error(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "done=true"):
            maa_depot.parse_operbox_output(self._message(False, [self._own()]))
        with self.assertRaisesRegex(RuntimeError, "own_opers"):
            maa_depot.parse_operbox_output(self._message(True, []))
        # 认不出 id 的干员全丢掉 → 一个都不剩时报错，绝不写一份假快照。
        with self.assertRaisesRegex(RuntimeError, "干员 id"):
            maa_depot.parse_operbox_output(
                self._message(True, [{"id": "", "name": "认不出"}])
            )

    def test_skland_health_is_decided_from_local_state_only(self) -> None:
        at = datetime(2026, 9, 9, 4, 30, tzinfo=BEIJING)
        config = {"skland_roster_refresh_days": 7}
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            skland = root / "skland.json"
            roster = root / "roster.json"
            with patch.object(maa_copilot, "SKLAND_PATH", skland), patch.object(
                maa_copilot, "ROSTER_PATH", roster
            ):
                # 还没有快照。
                self.assertIn("还没有", maa_depot.skland_unavailable(config, at=at))
                maa_common.atomic_write_json(skland, {"hg_token": "x"})
                maa_common.atomic_write_json(
                    roster,
                    {
                        "fetched_at": (at - timedelta(days=1)).isoformat(),
                        "source": "skland",
                        "chars": {"char_002_amiya": {"level": 90}},
                    },
                )
                self.assertEqual(maa_depot.skland_unavailable(config, at=at), "")
                # 快照太旧（懒刷新一直没成功）。
                maa_common.atomic_write_json(
                    roster,
                    {
                        "fetched_at": (at - timedelta(days=9)).isoformat(),
                        "source": "skland",
                        "chars": {"char_002_amiya": {"level": 90}},
                    },
                )
                self.assertIn("9 天", maa_depot.skland_unavailable(config, at=at))
                # 上一次刷新明确失败（401 / 网络）。
                maa_common.atomic_write_json(
                    skland, {"hg_token": "x", "last_error": "森空岛返回 401"}
                )
                self.assertIn("401", maa_depot.skland_unavailable(config, at=at))

    def _run_depot(self, *, unavailable: str, operbox_output: str, returncode: int = 0):
        depot_output = "Depot Completed\nDepot: " + json.dumps(
            {
                "taskchain": "Depot",
                "what": "DepotInfo",
                "details": {"done": True, "data": '{"30012": 40}'},
            }
        )
        calls: list[list[str]] = []

        def fake_run(argv, **_kwargs):
            calls.append(list(argv))
            if argv[2] == "depot":
                return SimpleNamespace(returncode=0, stdout=depot_output, stderr="")
            return SimpleNamespace(returncode=returncode, stdout=operbox_output, stderr="")

        context = unittest.mock.MagicMock()
        context.__enter__.return_value = True
        context.__exit__.return_value = False
        at = datetime(2026, 9, 9, 4, 30, tzinfo=BEIJING)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            maa_common.atomic_write_json(
                root / "item_index.json", {"30012": {"name": "固源岩组"}}
            )
            config = {"maa_bin": "/maa", "adb_path": "/adb", "maa_resource_dir": str(root)}
            operbox_roster = root / "roster-operbox.json"
            with patch.object(maa_depot, "run_lock", return_value=context), patch.object(
                maa_depot, "emulator_is_online", return_value=(False, "离线")
            ), patch.object(maa_daily, "require_executable"), patch.object(
                maa_daily, "open_emulator"
            ), patch.object(maa_daily, "wait_for_android"), patch.object(
                maa_daily, "verify_game_installed"
            ), patch.object(maa_daily, "settle_android_runtime"), patch.object(
                maa_daily, "close_everything", return_value=[]
            ), patch.object(maa_depot, "maa_subprocess_env", return_value={}), patch.object(
                maa_depot.subprocess, "run", side_effect=fake_run
            ), patch.object(
                maa_depot, "skland_unavailable", return_value=unavailable
            ), patch.object(
                maa_copilot, "OPERBOX_ROSTER_PATH", operbox_roster
            ), patch.object(
                maa_copilot, "ensure_private_dirs"
            ), patch.object(
                maa_depot, "reconcile", side_effect=lambda items, **kw: {
                    "snapshot_at": kw["at"].isoformat(), "snapshot_items": items
                }
            ):
                with contextlib.redirect_stdout(io.StringIO()) as printed:
                    result = maa_depot.run_depot(config, at=at)
            roster = (
                json.loads(operbox_roster.read_text("utf-8"))
                if operbox_roster.is_file()
                else None
            )
        return SimpleNamespace(
            result=result, calls=calls, roster=roster, printed=printed.getvalue()
        )

    def test_a_healthy_skland_never_runs_operbox(self) -> None:
        run = self._run_depot(unavailable="", operbox_output="")
        self.assertEqual([argv[2] for argv in run.calls], ["depot"])
        self.assertIsNone(run.roster)
        self.assertNotIn("operbox_note", run.result)

    def test_a_broken_skland_gets_the_operbox_snapshot(self) -> None:
        output = "OperBox Completed\n" + self._message(True, [self._own()])
        run = self._run_depot(unavailable="森空岛返回 401", operbox_output=output)
        self.assertEqual([argv[2] for argv in run.calls], ["depot", "operbox"])
        self.assertEqual(run.roster["source"], "operbox")
        self.assertEqual(run.roster["chars"]["char_350_surtr"]["elite"], 2)
        self.assertIn("1 名干员", run.result["operbox_note"])
        # 仓库真值仍然是这趟的正事，练度只是搭车。
        self.assertEqual(run.result["snapshot_items"]["30012"]["quantity"], 40)

    def test_a_failed_operbox_never_takes_the_depot_snapshot_down(self) -> None:
        run = self._run_depot(
            unavailable="森空岛返回 401", operbox_output="boom", returncode=1
        )
        self.assertEqual(run.result["snapshot_items"]["30012"]["quantity"], 40)
        self.assertIsNone(run.roster)
        self.assertIn("OperBox 后备也失败", run.result["operbox_note"])

    def test_the_newest_snapshot_wins_between_skland_and_operbox(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            skland_roster = root / "roster.json"
            operbox_roster = root / "roster-operbox.json"
            with patch.object(maa_copilot, "ROSTER_PATH", skland_roster), patch.object(
                maa_copilot, "OPERBOX_ROSTER_PATH", operbox_roster
            ):
                maa_common.atomic_write_json(
                    operbox_roster,
                    {
                        "fetched_at": "2026-09-09T04:30:00+08:00",
                        "source": "operbox",
                        "chars": {"char_350_surtr": {"level": 90}},
                    },
                )
                # 森空岛那份不在 → 用后备。
                self.assertEqual(maa_copilot.load_roster()["source"], "operbox")
                maa_common.atomic_write_json(
                    skland_roster,
                    {
                        "fetched_at": "2026-09-08T15:30:00+08:00",
                        "source": "skland",
                        "chars": {"char_350_surtr": {"level": 90}},
                    },
                )
                # 森空岛那份更旧 → 仍然用后备。
                self.assertEqual(maa_copilot.load_roster()["source"], "operbox")
                maa_common.atomic_write_json(
                    skland_roster,
                    {
                        "fetched_at": "2026-09-10T07:50:00+08:00",
                        "source": "skland",
                        "chars": {"char_350_surtr": {"level": 90}},
                    },
                )
                # 森空岛恢复 → 后备自动退场（它有技能与模组，能校验得更细）。
                self.assertEqual(maa_copilot.load_roster()["source"], "skland")
                # 两份都坏掉不许抛：练度读不到的后果是「一律判不满足」。
                skland_roster.write_text("{not json", encoding="utf-8")
                operbox_roster.write_text("{not json", encoding="utf-8")
                self.assertIsNone(maa_copilot.load_roster())

    def test_the_task_file_is_deployed_and_asks_for_operbox(self) -> None:
        toml = (ROOT / "config" / "maa-cli" / "tasks" / "operbox.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn('type = "OperBox"', toml)
        # StartUp 保留（它兼作「确认停在主页」），CloseDown 仍然不进任务链。
        self.assertIn('type = "StartUp"', toml)
        self.assertNotIn("CloseDown\"", toml)
        self.assertIn(
            Path("tasks/operbox.toml"), list(deploy.MAA_CONFIG_RELATIVE_PATHS)
        )
        # 任务文件没有运行时重写，必须留在逐字节比对名单里。
        self.assertNotIn(
            Path("tasks/operbox.toml"), deploy.RUNTIME_REWRITTEN_MAA_CONFIGS
        )


class ManualRunCardTests(unittest.TestCase):
    """`PLAN_MANUAL_RUN.md` 步骤 M：「现在跑」先发选关卡，选了才派发，10 分钟不选作废。"""

    CHOICES = [
        ("上次作战关卡", "", "沿用游戏内最近一次作战关卡"),
        ("1-7 · 固源岩", "1-7", "常用主线关卡"),
        ("CE-6 · 龙门币", "CE-6", "今日开放的资源本"),
    ]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.buttons = self.root / "buttons.json"
        self.manual = self.root / "manual_run.json"
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        (self.scripts / "maa_daily.py").write_text("#!/usr/bin/env python3\n")
        maa_common.atomic_write_json(self.buttons, {"hermes:btn:agent-book:x": {}})
        self.posted: list[tuple] = []
        self.popen: list[list[str]] = []
        self.now = datetime(2026, 9, 9, 12, 0, tzinfo=BEIJING)

    def _discord(self, *args, **_kwargs):
        self.posted.append(args)
        return {"id": "999"}

    def _popen(self, argv, **_kwargs):
        self.popen.append([str(part) for part in argv])
        return None

    def _stack(self, locked, config, now, stage_choices):
        stack = contextlib.ExitStack()
        moment = now or self.now
        stack.enter_context(patch.object(maa_action, "MANUAL_RUN_PATH", self.manual))
        stack.enter_context(patch.object(maa_action, "BUTTONS_PATH", self.buttons))
        stack.enter_context(patch.object(maa_action, "SCRIPTS_DIR", self.scripts))
        stack.enter_context(
            patch.object(maa_action, "state_lock", return_value=contextlib.nullcontext())
        )
        stack.enter_context(patch.object(maa_action, "is_run_locked", lambda: locked))
        stack.enter_context(
            patch.object(maa_action, "load_config", return_value=config or {"channel_id": "123"})
        )
        stack.enter_context(patch.object(maa_action, "require_executable", lambda *a: None))
        stack.enter_context(patch.object(maa_action, "discord_token", return_value="t"))
        stack.enter_context(patch.object(maa_action, "discord_request", side_effect=self._discord))
        stack.enter_context(patch.object(maa_action.subprocess, "Popen", side_effect=self._popen))
        stack.enter_context(patch.object(maa_action, "now_beijing", lambda: moment))
        stack.enter_context(
            patch.object(maa_action, "effective_plan", lambda phase="now": {"stage": "1-7", "skip": phase == "pm"})
        )
        stack.enter_context(
            patch.object(maa_plan_card, "_stage_choices", return_value=stage_choices or self.CHOICES)
        )
        return stack

    def _state(self):
        return json.loads(self.manual.read_text(encoding="utf-8"))

    def _registry(self):
        return json.loads(self.buttons.read_text(encoding="utf-8"))

    def _send_card(self, **kwargs):
        with self._stack(kwargs.pop("locked", False), kwargs.pop("config", None),
                         kwargs.pop("now", None), kwargs.pop("stage_choices", None)):
            return maa_action.run_now()

    # ---- run now ------------------------------------------------------

    def test_run_now_posts_a_card_whose_options_mirror_the_plan_card_source(self) -> None:
        ok, message = self._send_card()
        self.assertTrue(ok, message)
        self.assertIn("从下拉选一关即开跑", message)
        self.assertIn("12:10 前不选就作废", message)
        # 跳过是按场次的：回执仍点名被跳过的场次。
        self.assertIn("晚场（22:00）", message)
        self.assertNotIn("早场", message)
        state = self._state()
        self.assertEqual(state["status"], "pending")
        self.assertEqual(state["message_id"], "999")
        self.assertEqual(state["expires_at"], (self.now + timedelta(minutes=10)).isoformat())
        token = state["token"]
        self.assertRegex(token, r"^[0-9a-f]{12}$")
        # 卡片：下拉选项与 `_stage_choices()` 逐项相同，且没有任何 default（M1 / M2）。
        payload = self.posted[-1][3]
        select = payload["components"][0]["components"][0]
        self.assertEqual(
            [(o["label"], o["description"]) for o in select["options"]],
            [(label, desc) for label, _stage, desc in self.CHOICES],
        )
        self.assertFalse(any(o.get("default") for o in select["options"]))
        self.assertEqual(payload["embeds"][0]["fields"][0]["value"], "计算中…")
        # 登记：值不透明，argv 写死关卡 + token（不变量 #8），前缀是自己的。
        custom_id = f"hermes:btn:maa-now:{token}:stage-select"
        self.assertEqual(select["custom_id"], custom_id)
        registry = self._registry()
        self.assertEqual(
            registry[custom_id]["select_args"],
            {f"choice:{i}": ["run", "start", stage, token] for i, (_l, stage, _d) in enumerate(self.CHOICES)},
        )
        self.assertIn("hermes:btn:agent-book:x", registry)
        # 不开跑，只派过期哨兵和推荐补写两个脱离会话的子进程。
        self.assertEqual(len(self.popen), 2)
        self.assertEqual(self.popen[0][2:], ["run", "expire", token, "--after", "600"])
        self.assertEqual(self.popen[1][2:], ["run", "recommend", token])
        self.assertFalse(any("maa_daily.py" in part for argv in self.popen for part in argv))

    def test_run_now_refuses_while_a_run_holds_the_lock(self) -> None:
        with patch.object(maa_action, "_running_context", return_value=("早场", "1-7")):
            ok, message = self._send_card(locked=True)
        self.assertFalse(ok)
        self.assertIn("已经在跑", message)
        self.assertFalse(self.manual.exists())
        self.assertEqual(self.posted, [])
        self.assertEqual(self.popen, [])

    def test_run_now_refuses_inside_the_scheduled_run_blackout(self) -> None:
        for hour, minute in ((7, 31), (21, 45), (7, 59)):
            ok, message = self._send_card(now=datetime(2026, 9, 9, hour, minute, tzinfo=BEIJING))
            self.assertFalse(ok, (hour, minute))
            self.assertIn("定时场优先", message)
        self.assertFalse(self.manual.exists())
        self.assertEqual(self.posted, [])
        ok, _ = self._send_card(now=datetime(2026, 9, 9, 7, 29, tzinfo=BEIJING))
        self.assertTrue(ok)
        # 0 = 关闭拒绝窗。
        ok, _ = self._send_card(
            now=datetime(2026, 9, 9, 7, 59, tzinfo=BEIJING),
            config={"channel_id": "123", "manual_run_blackout_minutes": 0},
        )
        self.assertTrue(ok)

    def test_a_second_run_now_supersedes_the_pending_card(self) -> None:
        self._send_card()
        first = self._state()["token"]
        self.posted.clear()
        ok, _ = self._send_card(now=self.now + timedelta(minutes=3))
        self.assertTrue(ok)
        state = self._state()
        self.assertNotEqual(state["token"], first)
        self.assertEqual(state["status"], "pending")
        # 旧卡：PATCH 空组件 + footer 标作废；旧登记键不在了。
        patch_call = self.posted[0]
        self.assertEqual(patch_call[1], "PATCH")
        self.assertTrue(patch_call[2].endswith("/messages/999"))
        self.assertEqual(patch_call[3]["components"], [])
        self.assertIn("已被新的选关卡取代", patch_call[3]["embeds"][0]["footer"]["text"])
        keys = [k for k in self._registry() if k.startswith("hermes:btn:maa-now:")]
        self.assertEqual(keys, [f"hermes:btn:maa-now:{state['token']}:stage-select"])

    # ---- run start ----------------------------------------------------

    def _start(self, stage, token, **kwargs):
        with self._stack(kwargs.pop("locked", False), kwargs.pop("config", None),
                         kwargs.pop("now", None), None):
            return maa_action.run_start(stage, token)

    def test_selecting_a_stage_dispatches_the_manual_run_with_the_token(self) -> None:
        self._send_card()
        token = self._state()["token"]
        self.popen.clear()
        self.posted.clear()
        ok, message = self._start("CE-6", token, now=self.now + timedelta(minutes=5))
        self.assertTrue(ok, message)
        self.assertIn("刷 CE-6", message)
        self.assertIn("晚场（22:00）", message)
        state = self._state()
        self.assertEqual(state["status"], "dispatched")
        self.assertEqual(state["stage"], "CE-6")
        self.assertEqual(
            [k for k in self._registry() if k.startswith("hermes:btn:maa-now:")], []
        )
        self.assertEqual(self.popen, [[sys.executable, str(self.scripts / "maa_daily.py"), "now", token]])
        self.assertEqual(self.posted[-1][3]["components"], [])

    def test_selecting_the_last_stage_option_dispatches_an_empty_stage(self) -> None:
        self._send_card()
        token = self._state()["token"]
        ok, message = self._start("", token)
        self.assertTrue(ok, message)
        self.assertEqual(self._state()["stage"], "")
        self.assertIn("上次的本", message)

    def test_run_start_rejects_unknown_stale_or_foreign_tokens_without_dispatching(self) -> None:
        self._send_card()
        token = self._state()["token"]
        self.popen.clear()
        for bad in ("b" * 12, "not-a-token", ""):
            ok, message = self._start("CE-6", bad)
            self.assertFalse(ok)
            self.assertIn("已失效", message)
        self._start("CE-6", token)  # 消费掉
        ok, message = self._start("CE-6", token)
        self.assertFalse(ok)
        self.assertIn("已失效", message)
        self.assertEqual(len(self.popen), 1)

    def test_an_expired_card_is_refused_and_swept_on_the_spot(self) -> None:
        self._send_card()
        token = self._state()["token"]
        self.popen.clear()
        self.posted.clear()
        ok, message = self._start("CE-6", token, now=self.now + timedelta(minutes=10, seconds=1))
        self.assertFalse(ok)
        self.assertIn("已过期（12:10）", message)
        self.assertEqual(self.popen, [])
        self.assertEqual(self._state()["status"], "expired")
        self.assertEqual([k for k in self._registry() if "maa-now" in k], [])
        self.assertEqual(self.posted[-1][3]["components"], [])
        self.assertIn("已过期", self.posted[-1][3]["embeds"][0]["footer"]["text"])

    def test_run_start_refuses_bad_stages_locks_and_the_blackout_but_keeps_the_card(self) -> None:
        self._send_card(now=datetime(2026, 9, 9, 7, 25, tzinfo=BEIJING))
        token = self._state()["token"]
        self.popen.clear()
        ok, message = self._start("rm -rf", token, now=datetime(2026, 9, 9, 7, 26, tzinfo=BEIJING))
        self.assertFalse(ok)
        self.assertIn("白名单", message)
        with patch.object(maa_action, "_running_context", return_value=("早场", "1-7")):
            ok, message = self._start("CE-6", token, locked=True,
                                      now=datetime(2026, 9, 9, 7, 26, tzinfo=BEIJING))
        self.assertFalse(ok)
        self.assertIn("已经在跑", message)
        ok, message = self._start("CE-6", token, now=datetime(2026, 9, 9, 7, 31, tzinfo=BEIJING))
        self.assertFalse(ok)
        self.assertIn("定时场优先", message)
        self.assertEqual(self._state()["status"], "pending")
        self.assertEqual(self.popen, [])

    # ---- run expire / run recommend ------------------------------------

    def test_run_expire_is_idempotent_and_only_fires_after_the_deadline(self) -> None:
        self._send_card()
        token = self._state()["token"]
        self.posted.clear()
        with self._stack(False, None, self.now + timedelta(minutes=9), None):
            ok, message = maa_action.run_expire(token)
        self.assertTrue(ok)
        self.assertIn("才到期", message)
        self.assertEqual(self._state()["status"], "pending")
        with self._stack(False, None, self.now + timedelta(minutes=11), None):
            ok, message = maa_action.run_expire(token)
        self.assertIn("已过期并收起", message)
        self.assertEqual(self._state()["status"], "expired")
        self.assertEqual([k for k in self._registry() if "maa-now" in k], [])
        self.assertEqual(self.posted[-1][1], "PATCH")
        self.posted.clear()
        with self._stack(False, None, self.now + timedelta(minutes=12), None):
            ok, message = maa_action.run_expire(token)
        self.assertIn("没有待过期", message)
        self.assertEqual(self.posted, [])

    def test_run_recommend_writes_the_gap_stage_back_and_marks_the_option(self) -> None:
        self._send_card()
        token = self._state()["token"]
        self.posted.clear()
        with self._stack(False, None, None, None), patch.object(
            maa_plan_card, "current_farming_result", return_value={"x": 1}
        ), patch.object(
            maa_plan_card, "auto_stage", return_value=("CE-6", "龙门币缺口第一")
        ), patch.object(maa_plan_card, "farming_note", return_value="来源：森空岛"):
            ok, message = maa_action.run_recommend(token)
        self.assertTrue(ok, message)
        state = self._state()
        self.assertEqual(state["recommended_stage"], "CE-6")
        self.assertIn("CE-6", state["recommendation"])
        payload = self.posted[-1][3]
        self.assertEqual(self.posted[-1][1], "PATCH")
        self.assertIn("**CE-6**", payload["embeds"][0]["fields"][0]["value"])
        options = payload["components"][0]["components"][0]["options"]
        self.assertEqual([o["emoji"]["name"] for o in options], ["▫️", "▫️", "🎯"])
        self.assertFalse(any(o.get("default") for o in options))
        self.assertEqual(options[2]["value"], "choice:2")

    def test_run_recommend_degrades_to_a_note_when_the_gap_cannot_be_computed(self) -> None:
        self._send_card()
        token = self._state()["token"]
        self.posted.clear()
        with self._stack(False, None, None, None), patch.object(
            maa_plan_card, "current_farming_result", side_effect=RuntimeError("森空岛 401")
        ):
            ok, _ = maa_action.run_recommend(token)
        self.assertTrue(ok)
        self.assertIn("推荐暂不可用", self.posted[-1][3]["embeds"][0]["fields"][0]["value"])
        self.assertEqual(self._state()["status"], "pending")

    def test_run_recommend_does_not_touch_a_card_that_is_no_longer_pending(self) -> None:
        self._send_card()
        token = self._state()["token"]
        self._start("1-7", token)
        self.posted.clear()
        with self._stack(False, None, None, None), patch.object(
            maa_plan_card, "current_farming_result", return_value={}
        ), patch.object(maa_plan_card, "auto_stage", return_value=("CE-6", "")), patch.object(
            maa_plan_card, "farming_note", return_value=""
        ):
            maa_action.run_recommend(token)
        self.assertEqual(self.posted, [])

    # ---- dispatch / sweeps / daily ------------------------------------

    def test_the_dispatch_table_knows_the_manual_run_commands(self) -> None:
        calls: list[tuple] = []
        with patch.object(maa_action, "run_start", lambda s, t: calls.append(("start", s, t)) or (True, "")), \
             patch.object(maa_action, "run_expire", lambda t, a=0: calls.append(("expire", t, a)) or (True, "")), \
             patch.object(maa_action, "run_recommend", lambda t: calls.append(("recommend", t)) or (True, "")):
            maa_action.dispatch(["run", "start", "CE-6", "a" * 12])
            maa_action.dispatch(["run", "expire", "a" * 12])
            maa_action.dispatch(["run", "expire", "a" * 12, "--after", "600"])
            maa_action.dispatch(["run", "recommend", "a" * 12])
            ok, message = maa_action.dispatch(["run", "expire", "a" * 12, "--after", "6x"])
        self.assertFalse(ok)
        self.assertIn("不支持的动作", message)
        self.assertEqual(calls, [
            ("start", "CE-6", "a" * 12), ("expire", "a" * 12, 0),
            ("expire", "a" * 12, 600), ("recommend", "a" * 12),
        ])

    def test_the_manual_prefix_survives_the_plan_card_sweep(self) -> None:
        key = "hermes:btn:maa-now:aaaaaaaaaaaa:stage-select"
        self.assertFalse(key.startswith(maa_plan_card.BUTTON_PREFIX))
        self.assertFalse(key.startswith(maa_copilot.BUTTON_PREFIX))
        buttons = self.root / "sweep.json"
        maa_common.atomic_write_json(buttons, {"hermes:btn:maa:am:stage-select": {}, key: {}})
        with patch.object(maa_common, "BUTTONS_PATH", buttons):
            maa_common.prune_button_registry(maa_plan_card.BUTTON_PREFIX)
        registry = json.loads(buttons.read_text(encoding="utf-8"))
        self.assertNotIn("hermes:btn:maa:am:stage-select", registry)
        self.assertIn(key, registry)

    def test_the_daily_accepts_a_token_only_after_now(self) -> None:
        token = "c" * 12
        self.assertEqual(maa_daily.select_phase(["x", "now", token]), "now")
        self.assertEqual(maa_daily.manual_token_from_argv(["x", "now", token]), token)
        self.assertEqual(maa_daily.manual_token_from_argv(["x", "now"]), "")
        with self.assertRaises(ValueError):
            maa_daily.select_phase(["x", "am", token])
        with self.assertRaises(ValueError):
            maa_daily.select_phase(["x", "now", "not-hex"])

    def _execute(self, manual_token, state):
        if state is not None:
            maa_common.atomic_write_json(self.manual, state)
        seen: dict[str, Any] = {}
        alerts: list[tuple] = []

        def capture(plan, *_args, **_kwargs):
            seen["plan"] = dict(plan)
            raise RuntimeError("stop-here")

        with patch.object(maa_daily, "MANUAL_RUN_PATH", self.manual), patch.object(
            maa_daily, "state_lock", return_value=contextlib.nullcontext()
        ), patch.object(maa_daily, "load_config", return_value={"channel_id": "123"}), patch.object(
            maa_daily, "discord_token", return_value="t"
        ), patch.object(maa_daily, "discord_request", side_effect=lambda *a, **k: alerts.append(a)), patch.object(
            maa_daily, "effective_plan",
            lambda at, phase: {"stage": "1-7", "stage_source": "auto",
                               "farming_drops": {"30012": 5}, "farming_fallback_stage": "SR-7"},
        ), patch.object(maa_daily, "attach_annihilation", side_effect=capture), patch.object(
            maa_daily, "run_lock", side_effect=AssertionError("must not take the run lock")
        ):
            try:
                code = maa_daily.execute("now", manual_token)
            except RuntimeError as exc:
                self.assertEqual(str(exc), "stop-here")
                code = "reached-plan"
        return code, seen.get("plan"), alerts

    def test_the_daily_takes_the_selected_stage_and_drops_the_farming_caps(self) -> None:
        token = "d" * 12
        code, plan, alerts = self._execute(token, {"token": token, "status": "dispatched", "stage": "CE-6"})
        self.assertEqual(code, "reached-plan")
        self.assertEqual(plan["stage"], "CE-6")
        self.assertEqual(plan["stage_source"], "discord")
        self.assertEqual(plan["farming_drops"], {})
        self.assertEqual(plan["farming_fallback_stage"], "")
        self.assertEqual(alerts, [])

    def test_the_daily_refuses_a_mismatched_request_before_taking_the_lock(self) -> None:
        token = "e" * 12
        for state in (
            None,
            {"token": "f" * 12, "status": "dispatched", "stage": "CE-6"},
            {"token": token, "status": "pending", "stage": ""},
            {"token": token, "status": "expired", "stage": ""},
        ):
            code, plan, alerts = self._execute(token, state)
            self.assertEqual(code, 1, state)
            self.assertIsNone(plan)
            self.assertIn("手动未启动", alerts[-1][3]["embeds"][0]["title"])

    def test_the_daily_without_a_token_keeps_the_defaults(self) -> None:
        code, plan, alerts = self._execute("", {"token": "a" * 12, "status": "dispatched", "stage": "CE-6"})
        self.assertEqual(code, "reached-plan")
        self.assertEqual(plan["stage"], "1-7")
        self.assertEqual(plan["farming_fallback_stage"], "SR-7")
        self.assertEqual(alerts, [])


# ---------------------------------------------------------------------------
# 活动关多作业（2026-10-19）：游戏数据、排段、逐关事件、区域入口、执行
# ---------------------------------------------------------------------------

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


class CopilotListRoundTests(unittest.TestCase):
    """`_run_activity_round()`：按段执行、逐关记账、导航失败停整轮。"""

    def _run(self, entries, *, outcomes=None, tasks_ok=True, prep_problem="",
             settle=(True, "")):
        import maa_copilot_worker as worker

        ledger = maa_copilot.new_ledger(SR_KEY)
        results: list[dict] = []
        calls: list[str] = []

        def run_tasks(_config, _env, _log, name, _body, overlay, **_kw):
            calls.append(name if name != "copilot-story" else
                         f"story:{overlay['CopilotStoryClick']['text'][0]}")
            return tasks_ok, "" if tasks_ok else "认不出关卡"

        def run_list(_config, _env, _log, items):
            calls.append("list:" + ",".join(e["stage"] for e, _p in items))
            return outcomes or [("won", "")] * len(items)

        with tempfile.TemporaryDirectory() as raw, \
             patch.object(maa_clearing, "pick_anchor",
                          return_value=("SR-8", ["SR-8@SideStoryStage"], "")), \
             patch.object(maa_copilot, "load_stage_index", return_value=SR_INDEX), \
             patch.object(maa_copilot, "load_roster", return_value=None), \
             patch.object(maa_copilot, "PREPARED_DIR", Path(raw)), \
             patch.object(maa_copilot, "save_ledger", side_effect=lambda l, *a: l), \
             patch.object(maa_action, "min_lead_reason", return_value=""), \
             patch.object(worker, "_run_tasks", side_effect=run_tasks), \
             patch.object(worker, "_prepare_list_file", return_value=prep_problem), \
             patch.object(worker, "_run_copilot_list", side_effect=run_list), \
             patch.object(worker, "_recheck_before_stage", return_value=(True, "")), \
             patch.object(worker, "settle_after_battle", return_value=settle):
            stop = worker._run_activity_round(
                {"copilot_raid_switch_task": ""}, {"activity_key": SR_KEY}, entries, {},
                Path(raw) / "run.log", ledger, Path(raw) / "ledger.json", results, False,
            )
        return SimpleNamespace(stop=stop, results=results, calls=calls, ledger=ledger)

    def _sr(self, code, mode="normal"):
        return {"stage": code, "stage_id": SR_INDEX["codes"][code], "mode": mode,
                "candidate": {"id": 7}, "sanity_cost": 12}

    def test_one_map_trip_then_story_then_list(self) -> None:
        result = self._run([self._sr("SR-1"), self._sr("SR-2")])
        self.assertEqual(result.stop, "")
        self.assertEqual(result.calls, ["copilot-map", "story:SR-ST-1", "list:SR-1,SR-2"])
        self.assertTrue(result.ledger["stages"]["SR-1"]["normal"])
        self.assertTrue(result.ledger["stages"]["SR-2"]["normal"])

    def test_a_failed_item_stops_and_later_items_are_not_recorded(self) -> None:
        result = self._run(
            [self._sr("SR-1"), self._sr("SR-2")],
            outcomes=[("failed", "Copilot Error"), ("skipped", "前一份没过，这份没开始")],
        )
        self.assertEqual(result.stop, "Copilot Error")
        self.assertEqual([r["outcome"] for r in result.results], ["failed", "skipped"])
        self.assertNotIn("SR-2", result.ledger.get("stages", {}))

    def test_a_failed_settle_does_not_clear_the_last_won_item(self) -> None:
        import maa_copilot_worker as worker

        result = self._run([self._sr("SR-1"), self._sr("SR-2")],
                           settle=(False, "SR-2 战后未回到主界面：x"))
        self.assertEqual([r["outcome"] for r in result.results], ["won", "failed"])
        self.assertTrue(result.ledger["stages"]["SR-1"]["normal"])
        self.assertIn(worker.UNSETTLED_NOTICE, result.stop)

    def test_a_navigation_failure_is_charged_to_the_first_stage(self) -> None:
        result = self._run([self._sr("SR-1"), self._sr("SR-2")], tasks_ok=False)
        self.assertIn("没能停在活动地图上", result.stop)
        self.assertEqual([r["outcome"] for r in result.results], ["failed", "skipped"])
        self.assertEqual(result.calls, ["copilot-map"])

    def test_a_bad_job_file_never_reaches_maa(self) -> None:
        result = self._run([self._sr("SR-1")], prep_problem="作业 7：作业没有操作步骤")
        self.assertEqual(result.calls, ["copilot-map", "story:SR-ST-1"])
        self.assertEqual([r["outcome"] for r in result.results], ["skipped"])

    def test_raid_is_skipped_while_the_switch_is_off(self) -> None:
        result = self._run([self._sr("SR-1"), self._sr("SR-1", "raid")])
        self.assertEqual(result.calls[-1], "list:SR-1")
        self.assertIn("skipped", [r["outcome"] for r in result.results if r["mode"] == "raid"])


class ManualMultiStageTests(unittest.TestCase):
    """手动一次排多关 / 一个区域：同一轮连打，模拟器只开一次。"""

    def _expand(self, names, ledger=None, nav=("SR-1", "SR-2", "SR-5", "SR-8", "SR-EX-1")):
        index = {**SR_INDEX, "codes": {**SR_INDEX["codes"], "SR-EX-2": "act54side_ex02",
                                       "SR-EX-3": "act54side_ex03"}}
        with patch.object(maa_copilot, "load_stage_index", return_value=index), \
             patch.object(maa_action, "valid_copilot_stage",
                          side_effect=lambda n, _c: maa_copilot.resolve_copilot_stage(n, index)), \
             patch.object(maa_copilot, "load_ledger", return_value=ledger), \
             _nav_patch(set(nav)):
            return maa_action._expand_copilot_targets(names, {}, "SR", SR_KEY)

    def test_an_area_expands_in_number_order_and_skips_cleared_modes(self) -> None:
        ledger = {"stages": {"SR-EX-1": {"normal": "2026-10-19T17:57", "raid": None}}}
        targets, refusal, note = self._expand(
            ["SR-EX"], ledger, nav=("SR-EX-1", "SR-EX-2", "SR-EX-3"))
        self.assertEqual(refusal, "")
        self.assertEqual([(t["stage"], t["modes"]) for t in targets],
                         [("SR-EX-1", ["raid"]), ("SR-EX-2", ["normal", "raid"]),
                          ("SR-EX-3", ["normal", "raid"])])
        self.assertEqual(note, "")

    def test_an_area_drops_stages_without_navigation_with_a_note(self) -> None:
        targets, refusal, note = self._expand(["SR-EX"], nav=("SR-EX-1",))
        self.assertEqual([t["stage"] for t in targets], ["SR-EX-1"])
        self.assertIn("SR-EX-2", note)

    def test_explicit_stages_replay_even_when_cleared(self) -> None:
        ledger = {"stages": {"SR-1": {"normal": "x", "raid": "x"}}}
        targets, refusal, _note = self._expand(["SR-1", "SR-2"], ledger)
        self.assertEqual(refusal, "")
        self.assertEqual([t["stage"] for t in targets], ["SR-1", "SR-2"])

    def test_an_unknown_name_is_refused(self) -> None:
        targets, refusal, _note = self._expand(["SR-EX", "XX-9"])
        self.assertEqual(targets, [])
        self.assertIn("XX-9", refusal)

    def _plan(self, costs, candidates, sanity):
        import maa_copilot_worker as worker

        targets = [{"stage": f"SR-EX-{i + 1}", "stage_id": f"act54side_ex0{i + 1}",
                    "modes": ["normal", "raid"]} for i in range(len(costs))]
        picks = iter(candidates)
        with patch.object(maa_copilot, "estimated_sanity", return_value=sanity), \
             patch.object(maa_copilot, "pick_perfect_candidate",
                          side_effect=lambda *a: next(picks)):
            return worker._plan_search_entries(targets, costs, {}, {"sanity": {}}, "SR-EX-1")

    def test_stages_are_kept_while_sanity_lasts(self) -> None:
        ok = ({"id": 1}, "", [])
        entries, note = self._plan([10, 10, 15], [ok, ok, ok], sanity=45)
        self.assertEqual([(e["stage"], e["mode"]) for e in entries],
                         [("SR-EX-1", "normal"), ("SR-EX-1", "raid"),
                          ("SR-EX-2", "normal"), ("SR-EX-2", "raid")])
        self.assertIn("后面 1 关", note)
        self.assertIn("理智不够", note)

    def test_a_later_stage_without_a_job_cuts_the_round_there(self) -> None:
        ok = ({"id": 1}, "", [])
        entries, note = self._plan([10, 10, 10], [ok, (None, "没有作业", [])], sanity=200)
        self.assertEqual({e["stage"] for e in entries}, {"SR-EX-1"})
        self.assertIn("SR-EX-2 没有完全满足练度的作业", note)

    def test_the_first_stage_without_a_job_still_fails_loudly(self) -> None:
        with self.assertRaises(RuntimeError):
            self._plan([10], [(None, "没有作业", ["缺：能天使"])], sanity=200)

    def test_the_first_stage_is_kept_even_when_sanity_looks_short(self) -> None:
        entries, note = self._plan([20], [({"id": 1}, "", [])], sanity=5)
        self.assertEqual(len(entries), 2)
        self.assertEqual(note, "")


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
