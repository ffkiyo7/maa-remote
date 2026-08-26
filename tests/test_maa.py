from __future__ import annotations

import os
import io
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
import maa_common  # noqa: E402
import maa_daily  # noqa: E402
import maa_plan_card  # noqa: E402
import deploy  # noqa: E402


BEIJING = ZoneInfo("Asia/Shanghai")


class PlanTests(unittest.TestCase):
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

    def test_today_override_applies_and_stale_override_expires(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            defaults = root / "defaults.json"
            override = root / "plan.json"
            maa_common.atomic_write_json(defaults, {"stage": "1-7", "medicine": "none"})
            maa_common.atomic_write_json(
                override,
                {"date": "2026-08-02", "stage": "CE-6", "medicine": "expiring"},
            )
            with patch.object(maa_common, "DEFAULTS_PATH", defaults), patch.object(
                maa_common, "PLAN_PATH", override
            ):
                today = maa_common.effective_plan(datetime(2026, 8, 2, tzinfo=BEIJING))
                tomorrow = maa_common.effective_plan(datetime(2026, 8, 3, tzinfo=BEIJING))
            self.assertEqual(today["stage"], "CE-6")
            self.assertEqual(today["medicine"], "expiring")
            self.assertEqual(tomorrow["stage"], "1-7")
            self.assertEqual(tomorrow["medicine"], "expiring")

    def test_skip_wording_everywhere_says_manual_still_runs(self) -> None:
        skipped = maa_common.normalize_plan({"skip": True})
        self.assertIn("现在跑", maa_action._confirmation(skipped, False))
        card = maa_plan_card.build_embed(skipped, {})
        field = next(f for f in card["fields"] if f["name"] == "跳过自动场次")
        self.assertIn("手动开跑不受影响", field["value"])
        annihilation = next(f for f in card["fields"] if f["name"] == "每日剿灭")
        self.assertIn("1800", annihilation["value"])
        components, registry = maa_plan_card.build_components(
            maa_common.normalize_plan({"skip": False}), {"channel_id": "1"}
        )
        labels = [
            component["label"]
            for row in components
            for component in row.get("components", [])
            if "label" in component
        ]
        self.assertIn("⏭️ 跳过自动场次", labels)
        self.assertNotIn("⏭️ 今天跳过", labels)
        self.assertTrue(any(entry["kind"] == "exec" for entry in registry.values()))

    def test_run_now_receipt_flags_the_skip_override(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            script = Path(raw) / "maa_daily.py"
            script.write_text("#!/usr/bin/env python3\n")
            with patch.object(maa_action, "SCRIPTS_DIR", Path(raw)), patch.object(
                maa_action, "is_run_locked", lambda: False
            ), patch.object(maa_action, "load_config", lambda: {}), patch.object(
                maa_action, "require_executable", lambda *args: None
            ), patch.object(
                maa_action.subprocess, "Popen", lambda *args, **kwargs: None
            ):
                with patch.object(
                    maa_action, "effective_plan", lambda: {"skip": True}
                ):
                    ok, message = maa_action.run_now()
                self.assertTrue(ok)
                self.assertIn("手动场照跑", message)
                with patch.object(
                    maa_action, "effective_plan", lambda: {"skip": False}
                ):
                    _ok, plain = maa_action.run_now()
                self.assertNotIn("跳过", plain)

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
        self.assertIn("mode = 20000", rendered)
        self.assertNotIn("mode = 0", rendered)
        self.assertIn('facility = ["Mfg", "Trade", "Reception"]', rendered)
        self.assertNotIn('"Dorm"', rendered)
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
    def test_activity_output_parser(self) -> None:
        output = """Opening side story stages:
- SideStory「直到大地变成一颗酸橙」
  - TO-9: 晶体元件
  - TO-8: 凝胶
  - TO-7: 糖组
Opening mini games:
- 黑流树海刷钱
"""
        self.assertEqual(
            maa_plan_card.parse_activity_stages(output),
            ["TO-9", "TO-8", "TO-7"],
        )
        self.assertFalse(maa_plan_card.all_supply_stages_open(output))
        self.assertTrue(maa_plan_card.all_supply_stages_open("资源收集\n"))

    def test_buttons_fit_limits_and_registry_is_deterministic(self) -> None:
        plan = maa_common.normalize_plan({"stage": "CE-6", "medicine": "none"})
        components, registry = maa_plan_card.build_components(
            plan, {"channel_id": "123", "activity_stages": []}
        )
        self.assertLessEqual(len(components), 5)
        for row in components:
            self.assertLessEqual(len(row["components"]), 5)
        select = components[0]["components"][0]
        self.assertEqual(select["type"], 3)
        self.assertLessEqual(len(select["options"]), 25)
        self.assertEqual(sum(bool(option.get("default")) for option in select["options"]), 1)
        self.assertIn("select_args", registry["hermes:btn:maa:stage-select"])
        self.assertNotIn("hermes:btn:maa:run-now", registry)
        self.assertFalse(any("medicine" in key for key in registry))

    def test_activity_dropdown_uses_configured_stages(self) -> None:
        plan = maa_common.normalize_plan({})
        components, registry = maa_plan_card.build_components(
            plan, {"channel_id": "123", "activity_stages": ["HS-9"]}
        )
        select = components[0]["components"][0]
        option = next(option for option in select["options"] if option["label"] == "HS-9")
        self.assertEqual(
            registry["hermes:btn:maa:stage-select"]["select_args"][option["value"]],
            ["plan", "set-stage", "HS-9"],
        )

    def test_dropdown_includes_every_regular_supply_stage(self) -> None:
        plan = maa_common.normalize_plan({"stage": "AP-5"})
        components, registry = maa_plan_card.build_components(
            plan,
            {
                "channel_id": "123",
                "activity_stages": [],
                "all_supply_stages_open": True,
            },
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
        select_args = registry["hermes:btn:maa:stage-select"]["select_args"]
        for label, stage in expected.items():
            option = options[label]
            self.assertEqual(select_args[option["value"]], ["plan", "set-stage", stage])
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
            {
                "ok": True,
                "changed": True,
                "summary": "今天已更新：MaaCore v6.17.0-beta.1 → v6.17.0-beta.4",
            },
        )
        field = next(item for item in card["fields"] if item["name"] == "上游更新")
        self.assertIn("今天已更新", field["value"])

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


if __name__ == "__main__":
    unittest.main()
