from __future__ import annotations

import os
import io
import json
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
import maa_daily  # noqa: E402
import maa_plan_card  # noqa: E402
import deploy  # noqa: E402


BEIJING = ZoneInfo("Asia/Shanghai")



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
                self.assertEqual(len(deploy.maa_config_file_pairs()), 6)
                self.assertEqual(len(checked), 5)

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
            maa_daily, "require_executable"
        ), patch.object(maa_daily, "open_emulator"), patch.object(
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
    """PLAN_NAV_GATE B: automatic phases must not steal a device in use."""

    @staticmethod
    def _completed(returncode: int, stdout: str = "") -> SimpleNamespace:
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    def test_probe_reports_online_from_the_process_signal_alone(self) -> None:
        # A freshly launched BlueStacks has no Android yet, so the process
        # signal must be able to answer "online" without asking ADB at all.
        with patch.object(
            maa_daily.subprocess, "run", return_value=self._completed(0, "4242\n")
        ) as run:
            online, detail = maa_daily.emulator_is_online({})
        self.assertTrue(online)
        self.assertIn("进程在跑", detail)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][:2], ["pgrep", "-f"])

    def test_probe_reports_online_when_adb_says_boot_completed(self) -> None:
        # A hung process can outlive its device and vice versa, so ADB is the
        # second half of the OR.
        results = [self._completed(1), self._completed(0), self._completed(0, "1\n")]
        with patch.object(maa_daily.subprocess, "run", side_effect=results):
            online, detail = maa_daily.emulator_is_online({})
        self.assertTrue(online)
        self.assertIn("sys.boot_completed", detail)

    def test_probe_reports_offline_when_both_signals_miss(self) -> None:
        results = [self._completed(1), self._completed(0), self._completed(0, "\n")]
        with patch.object(maa_daily.subprocess, "run", side_effect=results):
            online, detail = maa_daily.emulator_is_online({})
        self.assertFalse(online)
        self.assertIn("未连上", detail)

    def test_a_broken_probe_falls_back_to_offline_and_never_raises(self) -> None:
        # Grabbing the device once by mistake is far cheaper than a daily that
        # stops forever because the probe itself broke (B-4).
        for failure in (
            maa_daily.subprocess.TimeoutExpired(cmd="pgrep", timeout=2),
            OSError("adb missing"),
        ):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(maa_daily.subprocess, "run", side_effect=failure):
                    online, detail = maa_daily.emulator_is_online({})
                self.assertFalse(online)
                self.assertTrue(detail)

    def test_probe_never_calls_the_180_second_boot_wait(self) -> None:
        # `wait_for_android` waits up to 180s for a device we just started;
        # reusing it here would blow the run budget and 不变量 #6.
        with patch.object(
            maa_daily.subprocess, "run", return_value=self._completed(0, "4242\n")
        ), patch.object(maa_daily, "wait_for_android") as wait, patch.object(
            maa_daily, "settle_android_runtime"
        ) as settle:
            maa_daily.emulator_is_online({})
        wait.assert_not_called()
        settle.assert_not_called()

    def test_probe_refuses_an_unvalidated_app_name_without_blocking_the_run(self) -> None:
        with patch.object(maa_daily.subprocess, "run") as run:
            online, detail = maa_daily.emulator_is_online({"emulator_app": "evil; rm -rf /"})
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
        ) as request, patch.object(maa_daily, "require_executable"), patch.object(
            maa_daily, "open_emulator", side_effect=RuntimeError("STOP: probe passed")
        ) as opened, patch.object(maa_daily, "send_direct_failure"):
            code = maa_daily.execute(phase)
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



class ClearingQueueTests(unittest.TestCase):
    """PLAN_CLEARING 步骤 3：队列、闸门、理智预算、needs_manual 出口。"""

    def setUp(self) -> None:
        self.config = {"clearing_manual_after_days": 2}
        self.state = maa_clearing.new_state(
            "标题|AT", "标题", "AT", ["AT-1", "AT-2", "AT-3"],
            at=datetime(2026, 9, 2, 12, 0, tzinfo=BEIJING),
        )

    # --- 队列形态 -------------------------------------------------------
    def test_queue_is_derived_from_the_recommended_stages_max_index(self) -> None:
        # config.activity_stages 只有 MAA 推荐的 4 关，队列要 AT-1..AT-8。
        prefix, queue = maa_clearing.queue_from_activity_stages(
            ["AT-8", "AT-7", "AT-6", "AT-4"]
        )
        self.assertEqual(prefix, "AT")
        self.assertEqual(queue, [f"AT-{index}" for index in range(1, 9)])

    def test_queue_ignores_non_numeric_and_empty_input(self) -> None:
        self.assertEqual(maa_clearing.queue_from_activity_stages([]), ("", []))
        self.assertEqual(
            maa_clearing.queue_from_activity_stages(["AT-EX-3", "垃圾"]), ("", [])
        )

    # --- EX 闸门 --------------------------------------------------------
    def test_ex_gate_blocks_while_any_zone_has_zero_total_stage(self) -> None:
        roster = {
            "activity_progress": {
                "act99side": [
                    {"zoneId": "z1", "clearedStage": 8, "totalStage": 8},
                    {"zoneId": "z2", "clearedStage": 0, "totalStage": 0},
                ]
            }
        }
        allowed, reason = maa_clearing.ex_gate(roster)
        self.assertFalse(allowed)
        self.assertIn("z2", reason)
        state, notify, blocked = maa_clearing.apply_ex_gate(self.state, roster, 8)
        self.assertTrue(notify)
        self.assertEqual(state["stages"], ["AT-1", "AT-2", "AT-3"])
        self.assertEqual(state["ex_gate_blocked_reason"], blocked)
        self.assertTrue(state["ex_gate_blocked_since"])

    def test_ex_gate_opens_only_when_every_zone_is_positive(self) -> None:
        roster = {
            "activity_progress": {
                "act99side": [
                    {"zoneId": "z1", "clearedStage": 8, "totalStage": 8},
                    {"zoneId": "z2", "clearedStage": 1, "totalStage": 8},
                ]
            }
        }
        allowed, _reason = maa_clearing.ex_gate(roster)
        self.assertTrue(allowed)
        state, notify, blocked = maa_clearing.apply_ex_gate(self.state, roster, 3)
        self.assertFalse(notify)
        self.assertEqual(blocked, "")
        self.assertEqual(
            state["stages"],
            ["AT-1", "AT-2", "AT-3", "AT-EX-1", "AT-EX-2", "AT-EX-3"],
        )
        self.assertTrue(state["ex_appended"])

    def test_ex_gate_fails_closed_when_skland_is_unavailable(self) -> None:
        # 401 / 无数据绝不能当成「全开」：多等一天没有代价，误判会让队列
        # 去打一个根本没开的段。
        for roster in (None, {}, {"activity_progress": {}}, {"activity_progress": []}):
            with self.subTest(roster=roster):
                allowed, reason = maa_clearing.ex_gate(roster)
                self.assertFalse(allowed)
                self.assertTrue(reason)

    def test_ex_gate_fail_closed_is_visible_and_deduplicated(self) -> None:
        # 护栏 4：不许只是安静地不入队——状态里要能自证，卡片发且只发一次，
        # 原因变了要重新发。
        first = {"activity_progress": {"a": [{"zoneId": "z2", "totalStage": 0}]}}
        state, notify, reason = maa_clearing.apply_ex_gate(self.state, first, 8)
        self.assertTrue(notify)
        self.assertTrue(maa_clearing.should_notify(state, "ex_gate", reason))
        # 同一原因再来一轮：状态还在，但不再发第二张卡。
        state, notify, reason2 = maa_clearing.apply_ex_gate(state, first, 8)
        self.assertFalse(notify)
        self.assertFalse(maa_clearing.should_notify(state, "ex_gate", reason2))
        self.assertEqual(state["ex_gate_blocked_reason"], reason)
        # 原因变了（森空岛整个不可用）→ 重新发。
        state, notify, reason3 = maa_clearing.apply_ex_gate(state, None, 8)
        self.assertTrue(notify)
        self.assertNotEqual(reason3, reason)
        self.assertTrue(maa_clearing.should_notify(state, "ex_gate", reason3))

    def test_ex_gate_is_not_wired_to_zone_progress_warning(self) -> None:
        # 两条独立的单向规则，合并会把「沉默不代表已解锁」这条性质弄丢。
        # 结构断言而不是字符串搜索（注释里正当地提到对方）：领域层不 import
        # maa_copilot，所以它调不到那个启发式；反过来 maa_copilot 也不 import
        # 队列，所以两条单向规则不可能互相依赖。
        import ast

        for module in ("maa_clearing", "maa_copilot"):
            tree = ast.parse(
                (ROOT / "scripts" / f"{module}.py").read_text(encoding="utf-8")
            )
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            other = "maa_copilot" if module == "maa_clearing" else "maa_clearing"
            self.assertNotIn(other, imported, f"{module} 不该 import {other}")
        self.assertFalse(
            [
                node
                for node in ast.walk(
                    ast.parse(
                        (ROOT / "scripts" / "maa_clearing.py").read_text(
                            encoding="utf-8"
                        )
                    )
                )
                if isinstance(node, ast.Call)
                and "zone_progress_warning" in ast.dump(node.func)
            ]
        )

    # --- 理智预算 -------------------------------------------------------
    def test_sanity_budget_deducts_todays_annihilation_first(self) -> None:
        # 地基事实 H：剿灭同样吃理智，不先扣就会把可用理智算多。
        plan = {"annihilation": True, "tasks": {"fight": True}}
        self.assertEqual(maa_clearing.annihilation_reserve(plan, {}), 25)
        self.assertEqual(maa_clearing.sanity_budget(100, plan, {}), 75)
        self.assertEqual(
            maa_clearing.sanity_budget(100, plan, {"clearing_annihilation_sanity": 40}),
            60,
        )

    def test_sanity_budget_reserves_nothing_once_annihilation_is_done(self) -> None:
        plan = {"annihilation": False, "tasks": {"fight": True}}
        self.assertEqual(maa_clearing.annihilation_reserve(plan, {}), 0)
        self.assertEqual(maa_clearing.sanity_budget(100, plan, {}), 100)
        off = {"annihilation": True, "tasks": {"fight": False}}
        self.assertEqual(maa_clearing.annihilation_reserve(off, {}), 0)

    def test_sanity_budget_never_goes_negative_or_guesses(self) -> None:
        plan = {"annihilation": True, "tasks": {"fight": True}}
        self.assertEqual(maa_clearing.sanity_budget(10, plan, {}), 0)
        self.assertEqual(maa_clearing.sanity_budget(None, plan, {}), 0)

    # --- 排一轮 ---------------------------------------------------------
    def test_round_stops_at_the_budget_without_moving_the_cursor(self) -> None:
        result = maa_clearing.plan_round(self.state, 45, lambda _stage: 20)
        self.assertEqual(result["stages"], ["AT-1", "AT-2"])
        self.assertEqual(result["spent"], 40)
        self.assertEqual(result["stop_reason"], "sanity")
        self.assertEqual(self.state["cursor"], 0)

    def test_unknown_sanity_cost_stops_the_round_and_freezes_the_cursor(self) -> None:
        # stage_sanity_cost() 对 EX 返回 None（步骤 4 才解）：不入当轮、
        # 不进理智预算、cursor 不变——前进会把这关永久跳过。
        costs = {"AT-1": 18, "AT-2": None, "AT-3": 18}
        result = maa_clearing.plan_round(self.state, 200, costs.get)
        self.assertEqual(result["stages"], ["AT-1"])
        self.assertEqual(result["spent"], 18)
        self.assertEqual(result["unknown_cost"], ["AT-2"])
        self.assertEqual(result["stop_reason"], "unknown_cost")
        self.assertEqual(self.state["cursor"], 0)

    def test_unknown_sanity_cost_notice_is_sent_only_once(self) -> None:
        self.assertTrue(maa_clearing.should_notify(self.state, "unknown_cost", "AT-2"))
        self.assertFalse(maa_clearing.should_notify(self.state, "unknown_cost", "AT-2"))
        self.assertTrue(maa_clearing.should_notify(self.state, "unknown_cost", "AT-3"))

    # --- needs_manual ---------------------------------------------------
    def test_manual_threshold_counts_distinct_dates_not_attempts(self) -> None:
        # 活动首日 prts.plus 上作业本来就少；按次数计会让每一关当天就触发
        # needs_manual，把「永不卡死的出口」变成「每关都要用户手打」。
        day1 = datetime(2026, 9, 2, 9, 0, tzinfo=BEIJING)
        for hour in (9, 13, 18, 23):
            state, turned = maa_clearing.record_stage_failure(
                self.state, "AT-1", kind="op", detail="没作业",
                config=self.config, at=day1.replace(hour=hour),
            )
            self.assertFalse(turned, f"同一天第 {hour} 小时就触发了")
        self.assertEqual(state["stage_state"]["AT-1"]["failed_dates"], ["2026-09-02"])
        self.assertEqual(maa_clearing.awaiting_manual(state), "")

        day2 = datetime(2026, 9, 3, 9, 0, tzinfo=BEIJING)
        state, turned = maa_clearing.record_stage_failure(
            self.state, "AT-1", kind="op", detail="还是没作业",
            config=self.config, at=day2,
        )
        self.assertTrue(turned)
        self.assertEqual(maa_clearing.awaiting_manual(state), "AT-1")
        self.assertEqual(
            state["stage_state"]["AT-1"]["failed_dates"], ["2026-09-02", "2026-09-03"]
        )
        # 已经在等待了就不重复翻牌（卡片不刷屏）。
        state, turned = maa_clearing.record_stage_failure(
            self.state, "AT-1", kind="op", detail="x",
            config=self.config, at=datetime(2026, 9, 4, 9, 0, tzinfo=BEIJING),
        )
        self.assertFalse(turned)

    def test_manual_threshold_is_configurable(self) -> None:
        self.assertEqual(maa_clearing.manual_after_days({}), 2)
        self.assertEqual(
            maa_clearing.manual_after_days({"clearing_manual_after_days": 5}), 5
        )
        self.assertEqual(
            maa_clearing.manual_after_days({"clearing_manual_after_days": "x"}), 2
        )

    def test_needs_manual_holds_the_cursor_until_the_user_clicks(self) -> None:
        for day in (2, 3):
            state, _turned = maa_clearing.record_stage_failure(
                self.state, "AT-1", kind="op", detail="没作业", config=self.config,
                at=datetime(2026, 9, day, 9, 0, tzinfo=BEIJING),
            )
        self.assertEqual(state["cursor"], 0)
        self.assertEqual(maa_clearing.current_stage(state), "AT-1")
        maa_clearing.advance_cursor(state)
        self.assertEqual(state["cursor"], 1)
        self.assertEqual(maa_clearing.awaiting_manual(state), "")
        self.assertEqual(maa_clearing.current_stage(state), "AT-2")

    def test_manual_card_button_uses_its_own_prefix(self) -> None:
        # 不变量 #7/#14 的清扫边界一个字节都不用改：`hermes:btn:maa:` 每天
        # 07:50 被整段清扫（卡片要跨天存活，用它就死），
        # `hermes:btn:maa-copilot:` 由 prune_expired_searches 按 token 清理
        # （队列没有 token）。所以队列用第三个独立前缀。
        state, _turned = maa_clearing.record_stage_failure(
            self.state, "AT-1", kind="nav", detail="导航失败", config={"clearing_manual_after_days": 1},
        )
        _embed, components, entries = maa_clearing.build_manual_card(state, "AT-1")
        custom_id = components[0]["components"][0]["custom_id"]
        self.assertEqual(list(entries), [custom_id])
        self.assertTrue(custom_id.startswith("hermes:btn:maa-clearing:"))
        self.assertFalse(custom_id.startswith(maa_plan_card.BUTTON_PREFIX))
        self.assertFalse(custom_id.startswith(maa_copilot.BUTTON_PREFIX))
        self.assertEqual(entries[custom_id]["script"], "maa_action.py")
        self.assertEqual(entries[custom_id]["args"], ["clearing", "manual-done", "AT-1"])

    # --- nav 失败冻结 ---------------------------------------------------
    def test_nav_failure_freezes_the_stage_for_the_rest_of_the_day(self) -> None:
        at = datetime(2026, 9, 2, 9, 0, tzinfo=BEIJING)
        state, _turned = maa_clearing.record_stage_failure(
            self.state, "AT-1", kind="nav", detail="导航失败", config=self.config, at=at
        )
        self.assertTrue(maa_clearing.is_nav_frozen(state, "AT-1", at))
        self.assertEqual(
            maa_clearing.plan_round(state, 200, lambda _s: 18, at=at)["stages"], []
        )
        tomorrow = datetime(2026, 9, 3, 9, 0, tzinfo=BEIJING)
        self.assertFalse(maa_clearing.is_nav_frozen(state, "AT-1", tomorrow))
        self.assertEqual(
            maa_clearing.plan_round(state, 200, lambda _s: 18, at=tomorrow)["stages"],
            ["AT-1", "AT-2", "AT-3"],
        )

    # --- 「队列活跃」的三条件 -------------------------------------------
    def test_queue_is_active_requires_all_three_conditions(self) -> None:
        at = datetime(2026, 9, 2, 9, 0, tzinfo=BEIJING)
        self.assertTrue(maa_clearing.queue_is_active(self.state, at))
        self.assertFalse(maa_clearing.queue_is_active(None, at))

        finished = dict(self.state, cursor=3)
        self.assertFalse(maa_clearing.queue_is_active(finished, at))

        frozen = maa_clearing.new_state("k", "t", "AT", ["AT-1"])
        maa_clearing.record_stage_failure(
            frozen, "AT-1", kind="nav", detail="x", config=self.config, at=at
        )
        self.assertFalse(maa_clearing.queue_is_active(frozen, at))

        waiting = maa_clearing.new_state("k", "t", "AT", ["AT-1"])
        maa_clearing.record_stage_failure(
            waiting, "AT-1", kind="op", detail="x",
            config={"clearing_manual_after_days": 1}, at=at,
        )
        self.assertEqual(maa_clearing.awaiting_manual(waiting), "AT-1")
        self.assertFalse(maa_clearing.queue_is_active(waiting, at))

    def test_state_round_trips_through_the_atomic_writer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "clearing.json"
            maa_clearing.save_state(self.state, path)
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
            loaded = maa_clearing.load_state(path)
            self.assertEqual(loaded["stages"], ["AT-1", "AT-2", "AT-3"])
            path.write_text("not json", encoding="utf-8")
            self.assertIsNone(maa_clearing.load_state(path))


class ClearingDailyYieldTests(unittest.TestCase):
    """日常让路：队列活跃时只摘「刷理智」，剿灭永远保留。"""

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

    def test_active_queue_removes_farming_and_keeps_annihilation(self) -> None:
        rendered = maa_daily.render_daily_toml({**self.plan, "farming": False}, self.config)
        self.assertIn('name = "每日剿灭"', rendered)
        self.assertIn('stage = "Annihilation"', rendered)
        self.assertNotIn('name = "刷理智"', rendered)
        self.assertNotIn('stage = "CE-6"', rendered)
        # 让路只动 Fight 的第二个块，其余任务照常。
        for name in ('name = "基建换班"', 'name = "公开招募"', 'name = "信用商店"',
                     'name = "领取奖励"', 'name = "返回主界面"'):
            self.assertIn(name, rendered)
        self.assertIn("stone = 0", rendered)
        self.assertIn("medicine = 0", rendered)

    def test_two_independent_switches_cover_all_four_combinations(self) -> None:
        for annihilation, farming in ((True, True), (True, False), (False, True), (False, False)):
            with self.subTest(annihilation=annihilation, farming=farming):
                rendered = maa_daily.render_daily_toml(
                    {**self.plan, "annihilation": annihilation, "farming": farming},
                    self.config,
                )
                self.assertEqual('name = "每日剿灭"' in rendered, annihilation)
                self.assertEqual('name = "刷理智"' in rendered, farming)

    def _yield_for(self, state, *, enabled=True):
        with patch.object(maa_clearing, "load_state", return_value=state):
            return maa_daily.attach_clearing_yield(
                self.plan, {"clearing_enabled": enabled},
                datetime(2026, 9, 2, 9, 0, tzinfo=BEIJING),
            )

    def test_frozen_day_still_farms(self) -> None:
        # 队列因冻结无法消耗理智的那天，日常照常刷理智——否则那天既不打队列
        # 也不刷理智，一整天的理智白白溢出（日常判绿、卡片正常，什么都没打）。
        at = datetime(2026, 9, 2, 9, 0, tzinfo=BEIJING)
        state = maa_clearing.new_state("k", "t", "AT", ["AT-1", "AT-2"])
        maa_clearing.record_stage_failure(
            state, "AT-1", kind="nav", detail="x", config={}, at=at
        )
        rendered = maa_daily.render_daily_toml(self._yield_for(state), self.config)
        self.assertIn('name = "刷理智"', rendered)
        self.assertIn('name = "每日剿灭"', rendered)

    def test_needs_manual_waiting_day_still_farms(self) -> None:
        at = datetime(2026, 9, 2, 9, 0, tzinfo=BEIJING)
        state = maa_clearing.new_state("k", "t", "AT", ["AT-1", "AT-2"])
        maa_clearing.record_stage_failure(
            state, "AT-1", kind="op", detail="x",
            config={"clearing_manual_after_days": 1}, at=at,
        )
        rendered = maa_daily.render_daily_toml(self._yield_for(state), self.config)
        self.assertIn('name = "刷理智"', rendered)
        self.assertIn('name = "每日剿灭"', rendered)

    def test_active_queue_day_yields_the_farming_block(self) -> None:
        state = maa_clearing.new_state("k", "t", "AT", ["AT-1", "AT-2"])
        rendered = maa_daily.render_daily_toml(self._yield_for(state), self.config)
        self.assertNotIn('name = "刷理智"', rendered)
        self.assertIn('name = "每日剿灭"', rendered)

    def test_disabled_feature_never_yields(self) -> None:
        state = maa_clearing.new_state("k", "t", "AT", ["AT-1"])
        self.assertTrue(self._yield_for(state, enabled=False)["farming"])

    def test_a_broken_queue_read_falls_back_to_farming(self) -> None:
        # 探测自己坏了不能顺手把理智也停掉：宁可多刷一场。
        with patch.object(maa_clearing, "load_state", side_effect=OSError("boom")):
            plan = maa_daily.attach_clearing_yield(self.plan, {"clearing_enabled": True})
        self.assertTrue(plan["farming"])


class ClearingActionTests(unittest.TestCase):
    """`maa_action.py` 的新子命令必须进白名单并保持 <20s 的派发姿态。"""

    def test_new_subcommands_are_on_the_dispatch_allowlist(self) -> None:
        with patch.object(maa_action, "clearing_status", return_value=(True, "s")) as status:
            self.assertEqual(maa_action.dispatch(["clearing", "status"]), (True, "s"))
        status.assert_called_once_with()
        with patch.object(
            maa_action, "clearing_manual_done", return_value=(True, "m")
        ) as done:
            self.assertEqual(
                maa_action.dispatch(["clearing", "manual-done", "AT-1"]), (True, "m")
            )
        done.assert_called_once_with("AT-1")
        for argv in (["clearing"], ["clearing", "nope"], ["clearing", "manual-done"]):
            ok, message = maa_action.dispatch(argv)
            self.assertFalse(ok)
            self.assertIn("不支持的动作", message)

    def test_clearing_is_refused_while_the_feature_is_off(self) -> None:
        with patch.object(maa_action, "load_config", return_value={}):
            ok, message = maa_action.clearing_start()
        self.assertFalse(ok)
        self.assertIn("clearing_enabled", message)

    def test_clearing_run_dispatches_and_returns_immediately(self) -> None:
        state = maa_clearing.new_state("k", "t", "AT", ["AT-1"])
        with patch.object(
            maa_action, "load_config", return_value={"clearing_enabled": True}
        ), patch.object(maa_clearing, "load_state", return_value=state), patch.object(
            maa_action.subprocess, "Popen"
        ) as popen, patch.object(
            maa_action, "_copilot_blackout_reason", return_value=None
        ), patch.object(maa_action.Path, "is_file", return_value=True):
            ok, message = maa_action.clearing_run()
        self.assertTrue(ok, message)
        self.assertEqual(popen.call_args.args[0][-1], "clearing")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_clearing_run_respects_the_daily_blackout(self) -> None:
        state = maa_clearing.new_state("k", "t", "AT", ["AT-1"])
        with patch.object(
            maa_action, "load_config", return_value={"clearing_enabled": True}
        ), patch.object(maa_clearing, "load_state", return_value=state), patch.object(
            maa_action, "_copilot_blackout_reason", return_value="太靠近 08:00"
        ), patch.object(maa_action.subprocess, "Popen") as popen:
            ok, message = maa_action.clearing_run()
        self.assertFalse(ok)
        self.assertEqual(message, "太靠近 08:00")
        popen.assert_not_called()

    def test_the_depot_window_is_kept_clear_of_the_queue(self) -> None:
        import maa_copilot_worker

        for hour, minute, blocked in (
            (4, 19, False), (4, 20, True), (4, 30, True), (4, 59, True), (5, 0, False)
        ):
            with self.subTest(hour=hour, minute=minute):
                reason = maa_copilot_worker._depot_window_reason(
                    datetime(2026, 9, 2, hour, minute, tzinfo=BEIJING)
                )
                self.assertEqual(bool(reason), blocked)

if __name__ == "__main__":
    unittest.main()
