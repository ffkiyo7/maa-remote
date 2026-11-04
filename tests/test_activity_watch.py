"""活动 watcher（`maa_activity_watch`）一 tick 的整链、派发闸门、时间预算、自动作业开关。

从原 `tests/test_maa.py` 按领域拆出；共用桩见 `_helpers.py`。
"""
from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
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
from _helpers import BEIJING, SR_INDEX, SR_KEY  # noqa: E402


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


class CopilotDispatchGateTests(unittest.TestCase):
    """2c：派发闸门——到点 + 不在 blackout + 锁空闲 + 模拟器离线。"""

    def setUp(self) -> None:
        self.at = datetime(2026, 9, 9, 12, 0, tzinfo=BEIJING)

    def _blocker(self, scheduled_at="", *, blackout=None, depot=None,
                 locked=False, online=(False, "")):
        with patch.object(maa_action, "_copilot_blackout_reason", return_value=blackout), \
             patch.object(maa_action, "depot_window_reason", return_value=depot), \
             patch.object(maa_action, "is_run_locked", return_value=locked), \
             patch.object(maa_playcover, "game_is_online", return_value=online):
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
            ({"online": (True, "游戏进程在跑")}, "明日方舟正在运行"),
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



if __name__ == "__main__":
    unittest.main()
