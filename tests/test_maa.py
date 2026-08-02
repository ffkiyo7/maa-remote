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

    def test_normalize_plan_hard_resets_stone(self) -> None:
        plan = maa_common.normalize_plan(
            {"stage": "CE-6", "medicine": "expiring", "stone": 99}
        )
        self.assertEqual(plan["stone"], 0)

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
        self.assertIn('name = "返回主界面"', rendered)
        self.assertIn('task_names = ["Home", "QuickSwitch@ToHome", "ReturnButtons"]', rendered)

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

    def test_resource_update_due_uses_last_attempt(self) -> None:
        at = datetime(2026, 8, 8, 7, 50, tzinfo=BEIJING)
        self.assertTrue(maa_plan_card.resource_update_due({}, at, 7))
        self.assertFalse(
            maa_plan_card.resource_update_due(
                {"resource_update_attempted_at": "2026-08-02T07:50:00+08:00"},
                at,
                7,
            )
        )
        self.assertTrue(
            maa_plan_card.resource_update_due(
                {"resource_update_attempted_at": "2026-08-01T07:50:00+08:00"},
                at,
                7,
            )
        )

    def test_failed_resource_update_is_recorded_and_alerted(self) -> None:
        at = datetime(2026, 8, 8, 7, 50, tzinfo=BEIJING)
        with patch.object(maa_plan_card, "now_beijing", return_value=at), patch.object(
            maa_plan_card, "load_json", return_value={}
        ), patch.object(
            maa_plan_card.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="network down"),
        ), patch.object(maa_plan_card, "merge_last_run") as merge, patch.object(
            maa_plan_card, "discord_request"
        ) as request:
            ok = maa_plan_card.update_resources_if_due(
                "token", "123", {"maa_bin": "/maa", "resource_update_timeout": 30}
            )
        self.assertFalse(ok)
        self.assertEqual(merge.call_args.args[0]["resource_update_status"], "failed")
        self.assertEqual(request.call_args.args[1:3], ("POST", "/channels/123/messages"))


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


if __name__ == "__main__":
    unittest.main()
