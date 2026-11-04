"""计划（`maa_common` 的计划读写 / 覆盖）与聊天命令、手动「现在跑」选关卡。

从原 `tests/test_maa.py` 按领域拆出；共用桩见 `_helpers.py`。
"""
from __future__ import annotations

import contextlib
import os
import json
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
import maa_daily  # noqa: E402
import maa_plan_card  # noqa: E402
import maa_playcover  # noqa: E402
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
        config: maa_common.Config = {"activity_stages": ["SSReopen-IC"], "maa_bin": "/missing/maa"}
        self.assertTrue(maa_action.valid_stage("", config))
        self.assertTrue(maa_action.valid_stage("CE-6", config))
        self.assertTrue(maa_action.valid_stage("1-7", config))
        self.assertTrue(maa_action.valid_stage("SSReopen-IC", config))
        self.assertFalse(maa_action.valid_stage("../../bad", config))
        self.assertFalse(maa_action.valid_stage("CE-6;shutdown", config))

    def test_activity_probe_requires_an_exact_stage_token(self) -> None:
        config: maa_common.Config = {"activity_stages": [], "maa_bin": "/maa"}
        result = SimpleNamespace(
            returncode=0,
            stdout="Official server: current activity SSReopen-IC-9",
            stderr="",
        )
        with patch.object(maa_action.subprocess, "run", return_value=result):
            self.assertTrue(maa_action.valid_stage("SSReopen-IC-9", config))
            self.assertFalse(maa_action.valid_stage("Official", config))
            self.assertFalse(maa_action.valid_stage("Reopen-IC-9", config))


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
        stack.enter_context(
            patch.object(maa_playcover, "verify_game_installed", lambda *a: None)
        )
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


class PlanFightSwitchTests(unittest.TestCase):
    """`plan fight off pm`：这一场只收菜，剿灭和刷理智都不打。"""

    def _with_plan(self, argv):
        with tempfile.TemporaryDirectory() as raw:
            plan_path = Path(raw) / "plan.json"
            defaults = Path(raw) / "defaults.json"
            with patch.object(maa_common, "PLAN_PATH", plan_path), \
                 patch.object(maa_common, "DEFAULTS_PATH", defaults), \
                 patch.object(maa_action, "PLAN_PATH", plan_path), \
                 patch.object(maa_action, "is_run_locked", return_value=False):
                ok, message = maa_action.dispatch(argv)
                return ok, message, maa_common.effective_plan(phase="pm"), \
                    maa_common.effective_plan(phase="am")

    def test_fight_off_drops_both_fight_blocks_for_that_session_only(self) -> None:
        ok, message, pm, am = self._with_plan(["plan", "fight", "off", "pm"])
        self.assertTrue(ok, message)
        self.assertIn("不清理智", message)
        self.assertFalse(pm["tasks"]["fight"])
        self.assertTrue(pm["tasks"]["infrast"])
        self.assertTrue(am["tasks"]["fight"])
        toml = maa_daily.render_daily_toml(pm, {})
        self.assertNotIn('type = "Fight"', toml)
        self.assertIn('type = "Infrast"', toml)

    def test_bad_values_are_refused(self) -> None:
        ok, _message, pm, _am = self._with_plan(["plan", "fight", "maybe", "pm"])
        self.assertFalse(ok)
        self.assertTrue(pm["tasks"]["fight"])



if __name__ == "__main__":
    unittest.main()
