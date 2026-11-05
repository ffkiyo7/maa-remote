"""日常一场（`maa_daily`）：渲染、连接 / 代理失败、熔断、救场、后备关、截图、锁、探测。

从原 `tests/test_maa.py` 按领域拆出；共用桩见 `_helpers.py`。
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import struct
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime
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
import maa_depot  # noqa: E402
import maa_daily  # noqa: E402
import maa_playcover  # noqa: E402
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


def isolate_stage_index(case: unittest.TestCase) -> Path:
    """让 `maa_copilot.load_stage_index()` 的缓存写进本用例自己的临时目录。

    `maa_daily.execute()` 走到「理智不够让出刷理智」会建 `stage_index.json`；
    写进共享临时 HOME 的话，后面的用例读到的就是前一个用例建的缓存（DEV_GUIDE §9）。
    """
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    path = Path(tmp.name) / "stage_index.json"
    patcher = patch.object(maa_copilot, "STAGE_INDEX_PATH", path)
    patcher.start()
    case.addCleanup(patcher.stop)
    return path


class RenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = maa_common.normalize_plan(
            {"stage": "CE-6", "medicine": "expiring", "tasks": {}}
        )
        self.config: maa_common.Config = {
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
        self.assertNotIn("annihilation_skip_reason", attached)

    def test_annihilation_skipped_when_skland_says_this_week_is_full(self) -> None:
        plan = maa_common.normalize_plan({"stage": "1-7"})
        thursday = datetime(2026, 11, 5, 8, 0, tzinfo=BEIJING)

        def roster(current: int, fetched: datetime) -> dict[str, Any]:
            return {"annihilation_weekly": {
                "current": current, "total": 1800,
                "server_current_at": int(fetched.timestamp()),
            }}

        this_monday = datetime(2026, 11, 2, 4, 0, tzinfo=BEIJING)
        last_sunday_night = datetime(2026, 11, 2, 3, 59, tzinfo=BEIJING)
        self.assertEqual(maa_daily.cn_game_week(thursday), "2026-11-02")
        self.assertEqual(maa_daily.cn_game_week(last_sunday_night), "2026-10-26")

        full = maa_daily.attach_annihilation(plan, thursday, {}, roster(1800, this_monday))
        self.assertFalse(full["annihilation"])
        self.assertIn("1800/1800", full["annihilation_skip_reason"])
        # 上周的满额快照不算数；没满、没字段都照常打（读不到一律照常）。
        for snapshot in (
            roster(1800, last_sunday_night),
            roster(1500, thursday),
            {},
            {"annihilation_weekly": {"current": "bad"}},
        ):
            attached = maa_daily.attach_annihilation(plan, thursday, {}, snapshot)
            self.assertTrue(attached["annihilation"], snapshot)
            self.assertNotIn("annihilation_skip_reason", attached)
        # 今天已刷过优先：不去看本周进度，文案仍是「今日已刷过」。
        done = {"annihilation_game_day": maa_daily.cn_game_day(thursday)}
        attached = maa_daily.attach_annihilation(plan, thursday, done, roster(1800, thursday))
        self.assertNotIn("annihilation_skip_reason", attached)

    def test_attach_annihilation_reads_the_skland_roster_file(self) -> None:
        plan = maa_common.normalize_plan({"stage": "1-7"})
        thursday = datetime(2026, 11, 5, 8, 0, tzinfo=BEIJING)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "roster.json"
            path.write_text(json.dumps({"chars": {}, "annihilation_weekly": {
                "current": 1800, "total": 1800,
                "server_current_at": int(thursday.timestamp()),
            }}), encoding="utf-8")
            with patch.object(maa_copilot, "ROSTER_PATH", path):
                self.assertFalse(maa_daily.attach_annihilation(plan, thursday, {})["annihilation"])
            path.write_text("{broken", encoding="utf-8")
            with patch.object(maa_copilot, "ROSTER_PATH", path):
                self.assertTrue(maa_daily.attach_annihilation(plan, thursday, {})["annihilation"])

    def test_summary_names_weekly_full_annihilation_skip(self) -> None:
        plan = maa_common.normalize_plan({"stage": "1-7"})
        plan["annihilation"] = False
        plan["annihilation_skip_reason"] = "本周剿灭合成玉已满（1800/1800），本场跳过"
        summary = maa_daily.extract_summary(
            ["StartUp Completed", "[刷理智] Completed"], [], plan=plan, ok=True
        )
        self.assertIn("☄️ 每日剿灭：⏭️ 本周剿灭合成玉已满（1800/1800），本场跳过", summary)
        self.assertNotIn("今日已刷过", summary)

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
        self.assertIn("🚀 登录与首登领取：❌ 中断（没连上游戏（TouchModeNotAvailable）", summary)
        self.assertIn("MaaTools", summary)

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
    def _resilient(self, attempts: list[tuple[int, list[str], list[str]]], config: maa_common.Config):
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "run.log"
            with (
                patch.object(maa_daily, "run_maa", side_effect=attempts) as runner,
                patch.object(maa_daily.time, "sleep"),
                patch.object(maa_playcover, "settle_game_window", return_value="") as settle,
                patch.object(maa_playcover, "wait_for_game") as wait,
            ):
                result = maa_daily.run_maa_resilient(
                    {"maa_run_timeout": 3000, **config},
                    log_path,
                    "token",
                    "1",
                    "2",
                    maa_common.normalize_plan({"stage": "TO-9"}),
                )
            self.runner, self.settle, self.wait = runner, settle, wait
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
        self.assertIn("等 8s", "".join(err))
        self.assertIn("重开第 1 趟", "".join(err))
        self.assertIn("重开第 1 趟", self.log_text)
        # 重开之前先把游戏窗口摆回台前、等 MaaTools 就绪。
        self.settle.assert_called_once()
        self.wait.assert_called_once()

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
    def _run(self, stdout_text: str, config: maa_common.Config) -> tuple[int, list[str], list[str]]:
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
        completed = [line.replace("(56s) Error", "(5m) Completed") for line in DRY_FIGHT_ERROR_LOG]
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
        cases: tuple[tuple[maa_common.Config, int, str], ...] = (
            ({"fight_fallback_on_error": False}, 0, ""),
            ({"maa_run_timeout": 3000}, 2900, "跳过后备关补跑"),
        )
        for config, elapsed, note in cases:
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


SANRIO_HOME_OCR = "\n".join(
    [
        "wado\t[97,583,131,47]",
        "FRIENDS\t[527,803,80,25]",
        "•好友\t[560,826,47,22]",
        "TTGR ARCHIKE\t[454,881,156,50]",
        "•档案\t[560,906,47,17]",
        "TERMiN\t[1375,210,215,74]",
        "理智/208\t[1191,373,106,28]",
        "•SQUADS‘\t[1230,501,145,36]",
        "•编\t[1264,546,33,22]",
        "2013天葉购中心\t[1112,603,222,66]",
        "OPERATOR：\t[1579,503,181,41]",
        "RECRUIT\t[1501,711,100,33]",
        "公开招募\t[1497,740,86,33]",
        "NSTORE\t[1210,740,140,38]",
        "MISSION\t[1272,856,103,39]",
        "BASE\t[1640,930,80,40]",
        "甚建\t[1650,975,55,30]",
        "HEADHUNT\t[1700,711,140,33]",
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

    def test_a_themed_home_page_is_accepted_from_its_english_labels(self) -> None:
        # 2026-11-04 三丽鸥联动主题下的真机 OCR（PlayCover，节选）：中文入口名大多读碎了，
        # 旧的 8 个词只命中 3 个，好好的主页被判红。
        verified, detail = self._verify(SANRIO_HOME_OCR)
        self.assertIs(verified, True, detail)
        self.assertIn("终端", detail)

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
        with patch.object(maa_playcover, "capture_screenshot", fake_capture), patch.object(
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
            maa_playcover, "capture_screenshot", lambda config, path: (False, "adb 挂了")
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
        self.assertIn("maa_playcover.capture_screenshot(config, png_path)", worker)
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

    def test_sanrio_theme_entries_never_switch_theme_and_ship_their_templates(self) -> None:
        resource = ROOT / "config" / "maa-cli" / "resource"
        override = json.loads((resource / "tasks" / "tasks.json").read_text(encoding="utf-8"))
        deployed = set(deploy.MAA_CONFIG_RELATIVE_PATHS)
        entries = [key for key in override if key.endswith("-Entry")]
        self.assertEqual(len(entries), 9)
        self.assertNotIn("SwitchTheme", json.dumps(override))
        for entry in entries:
            name = entry.removesuffix("-Entry")
            # Upstream's day-theme template stays first.  A miss first tries to
            # dismiss the originite exchange popup, then fails loudly — never
            # upstream's SwitchTheme, which resets the user's theme.
            self.assertEqual(override[entry]["next"], [f"{name}Default", f"{name}Sanrio"])
            self.assertEqual(override[entry]["onErrorNext"], [f"{name}OriginiteExchangeCancel"])
            cancel = override[f"{name}OriginiteExchangeCancel"]
            self.assertEqual(cancel, {
                "baseTask": "OriginiteExchangeCancel",
                "next": [f"{name}Default", f"{name}Sanrio"],
            })
            sanrio = override[f"{name}Sanrio"]
            self.assertEqual(sanrio["baseTask"], f"{name}Default")
            template = Path("resource/template/UiTheme") / sanrio["template"]
            self.assertTrue((resource.parent / template).is_file(), template)
            self.assertIn(template, deployed)
            # The click stays inside the matched template, away from its edges
            # (2026-11-05: a click 10px under the Terminal template's top edge
            # hit the orundum bar and opened the originite exchange popup).
            width, height = struct.unpack(">II", (resource.parent / template).read_bytes()[16:24])
            dx, dy, w, h = sanrio["rectMove"]
            self.assertGreaterEqual(dx, width // 5, entry)
            self.assertGreaterEqual(dy, height // 5, entry)
            self.assertLessEqual(dx + w, width - width // 5, entry)
            self.assertLessEqual(dy + h, height - height // 10, entry)
        terminal_dy = override["TerminalSanrio"]["rectMove"][1]
        self.assertGreaterEqual(terminal_dy, 25)

    def test_originite_exchange_popup_is_only_ever_cancelled(self) -> None:
        override = json.loads(
            (ROOT / "config/maa-cli/resource/tasks/tasks.json").read_text(encoding="utf-8")
        )
        cancel = override["OriginiteExchangeCancel"]
        self.assertEqual(cancel["algorithm"], "OcrDetect")
        self.assertEqual(cancel["action"], "ClickRect")
        x, _y, w, _h = cancel["specificRect"]
        # The popup's ✗ half ends at x≈955 (1280×720); ✓ starts at 958.
        self.assertLessEqual(x + w, 940)
        self.assertLessEqual(cancel["maxTimes"], 3)


class LockConflictTests(unittest.TestCase):
    def setUp(self) -> None:
        isolate_stage_index(self)

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


class GameProbeTests(unittest.TestCase):
    """PLAN_NAV_GATE B: automatic phases must not steal a game in use.

    探测本身在 `maa_playcover.game_is_online`（`test_playcover.py` 里测），
    这里只测日常 `execute()` 怎么用它。
    """

    def setUp(self) -> None:
        isolate_stage_index(self)

    def _execute(self, phase: maa_common.Phase, *, online: bool, probe=None):
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
            maa_playcover, "game_is_online", probe
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
            # 不桩它就会真去读 PlayCover 里明日方舟的安装和 MaaTools 设置。
            maa_playcover, "verify_game_installed"
        ), patch.object(
            maa_playcover, "launch_game", side_effect=RuntimeError("STOP: probe passed")
        ) as opened, patch.object(maa_daily, "send_direct_failure"):
            code = maa_daily.execute(phase)
        merge.call_args_list = merge.call_args_list + common_merge.call_args_list  # pyright: ignore[reportAttributeAccessIssue]  # 合并两处桩的调用记录
        request.call_args_list = request.call_args_list + common_request.call_args_list  # pyright: ignore[reportAttributeAccessIssue]  # 合并两处桩的调用记录
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
                skipped = [r for r in records if r.get("skip_reason") == "game_busy"]
                self.assertEqual(len(skipped), 1)
                self.assertEqual(skipped[0]["status"], "skipped")
                self.assertEqual(skipped[0]["phase"], phase)
                self.assertEqual(skipped[0]["game_probe"], "探测明细")
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


class SessionDailyTests(unittest.TestCase):
    """四期 1a：`execute(phase)` 只吃**这一场**的覆盖，`now` 一场都不吃。"""

    def setUp(self) -> None:
        isolate_stage_index(self)
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

    def _execute(self, phase: maa_common.Phase):
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
            maa_playcover, "game_is_online", return_value=(False, "离线")
        ), patch.object(maa_daily, "merge_last_run"), patch.object(
            maa_daily, "discord_request"
        ) as request, patch.object(maa_daily, "require_executable"), patch.object(
            maa_playcover, "verify_game_installed"
        ), patch.object(
            maa_playcover, "launch_game", side_effect=RuntimeError("STOP: 已越过跳过闸门")
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


class EmulatorProbeSharedTests(unittest.TestCase):
    """四期 1d：一份探测实现，四条自动链路共用（D18 / 不变量 #24）。"""

    def test_there_is_exactly_one_probe_implementation(self) -> None:
        import maa_copilot_worker
        import maa_roguelike_worker

        for module in (
            maa_daily,
            maa_depot,
            maa_action,
            maa_copilot_worker,
            maa_roguelike_worker,
        ):
            with self.subTest(module=module.__name__):
                # 调用方只经 `maa_playcover.game_is_online()` 走，不留自己的同名引用。
                self.assertIs(module.maa_playcover, maa_playcover)
                self.assertFalse(hasattr(module, "game_is_online"))
        for name in ("maa_daily.py", "maa_common.py"):
            source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertNotIn("def game_is_online", source)

    def test_scripts_never_from_import_the_device_layer(self) -> None:
        # 设备函数只在 `maa_playcover` 一处打桩；`from maa_playcover import x`
        # 会在调用方留一份独立引用，桩打在 maa_playcover 上就漏过去了。
        offenders = []
        for path in sorted((ROOT / "scripts").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "maa_playcover":
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [])

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
                maa_playcover, "game_is_online", return_value=(True, "游戏进程在跑")
            ), patch.object(
                worker, "_notice",
                side_effect=lambda c, t, b, col: notices.append((t, b)),
            ), patch.object(worker, "merge_last_run") as merge, patch.object(
                worker, "_fight_one"
            ) as fight, patch.object(maa_playcover, "launch_game") as launch_game:
                code = worker._batch_run_locked(queued)
            restored = json.loads((searches / f"{token}.json").read_text("utf-8"))
        self.assertEqual(code, 0)
        launch_game.assert_not_called()
        fight.assert_not_called()
        # 这一轮放回排程，watcher 下一 tick 再试；跳过必须可见（护栏 4）。
        self.assertEqual(restored["status"], "scheduled")
        self.assertIn("明日方舟正在运行", notices[0][1])
        self.assertIn("游戏进程在跑", notices[0][1])
        self.assertEqual(merge.call_args.args[0]["skip_reason"], "game_busy")


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
                maa_playcover, "game_is_online", return_value=(True, "游戏进程在跑")
            ), patch.object(
                maa_depot, "report_game_busy"
            ) as report, patch.object(
                maa_depot, "reconcile"
            ) as writer, patch.object(maa_playcover, "launch_game") as launch_game:
                # Depot 调的是 `maa_playcover.launch_game`；桩在 `maa_daily` 上
                # 下面的 assert_not_called() 就永远成立，什么也证明不了。
                result = maa_depot.run_if_due({"channel_id": "1"}, at=at)
            after = json.loads(depot_path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "skipped_busy")
        # `snapshot_at` 不能动：动了这次「没跑」就会被记成一次成功对表，
        # 7 天间隔白白往后推一周。
        self.assertEqual(result["snapshot_at"], previous)
        self.assertEqual(after["snapshot_at"], previous)
        writer.assert_not_called()
        launch_game.assert_not_called()
        self.assertEqual(
            report.call_args.kwargs["state_update"]["depot_status"], "skipped_busy"
        )

    def test_the_shared_busy_report_writes_state_and_posts_a_grey_card(self) -> None:
        at = datetime(2026, 9, 9, 8, 0, tzinfo=BEIJING)
        with patch.object(maa_common, "merge_last_run") as merge, patch.object(
            maa_common, "discord_request"
        ) as request:
            maa_common.report_game_busy(
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
                maa_common.report_game_busy(
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


class SanityYieldTests(unittest.TestCase):
    """D11：理智不够刷默认关时让路，估不出一律照常刷。"""

    def setUp(self) -> None:
        self.plan = maa_common.normalize_plan(
            {"stage": "CE-6", "medicine": "expiring", "tasks": {}}
        )
        self.plan["annihilation"] = True
        self.config: maa_common.Config = {
            "medicine_expire_days": 2,
            "fight_times": 2147483647,
            "recruit_times": 4,
        }
        self.at = datetime(2026, 9, 9, 22, 0, tzinfo=BEIJING)

    def _yield(self, *, sanity, cost: int | None = 18):
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


class DropReportTests(unittest.TestCase):
    """步骤 5：日常掉落匿名上报一图流 / 企鹅物流（回馈步骤 3 的数据源）。"""

    def setUp(self) -> None:
        self.plan = maa_common.normalize_plan({"stage": "1-7", "tasks": {}})
        self.plan["farming_drops"] = {"30013": 40}
        self.plan["farming_fallback_stage"] = "CE-6"
        self.config: maa_common.Config = {
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
        self.config: maa_common.Config = {
            "medicine_expire_days": 2,
            "fight_times": 2147483647,
            "recruit_times": 4,
        }

    def _award(self, config: maa_common.Config) -> str:
        rendered = maa_daily.render_daily_toml(self.plan, config)
        return next(b for b in rendered.split("[[tasks]]") if 'type = "Award"' in b)

    def _mall(self, config: maa_common.Config) -> str:
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
        block = self._award({**self.config, "award_orundum": "yes"})  # pyright: ignore[reportArgumentType]  # 故意传坏值
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
                    42,                  # 根本不是字符串  # pyright: ignore[reportArgumentType]  # 故意传坏值
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
        block = self._mall({**self.config, "mall_buy_first": "招聘许可"})  # pyright: ignore[reportArgumentType]  # 故意传坏值
        self.assertIn("buy_first = []", block)



if __name__ == "__main__":
    unittest.main()
