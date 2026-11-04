"""抄作业执行链（`maa_copilot` / `maa_copilot_worker`）：记账、连抄、收尾、手动路径、作业卡。

从原 `tests/test_maa.py` 按领域拆出；共用桩见 `_helpers.py`。
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
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
import maa_clearing  # noqa: E402
import maa_common  # noqa: E402
import maa_copilot  # noqa: E402
import maa_plan_card  # noqa: E402
import maa_playcover  # noqa: E402
import deploy  # noqa: E402
from _helpers import BEIJING, RAID_OFF, RAID_ON, SR_INDEX, SR_KEY, _nav_patch, _zone  # noqa: E402


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


class CopilotDailyAttemptCapTests(unittest.TestCase):
    """当日上限：同一 `(关, 模式)` 打满 `auto_copilot_max_attempts_per_day` 就到明天。

    2026-09-09 03:30 与 04:00 两轮把 SR-5/6/7 原样重打了一遍——记账全是 `unknown`
    （缺陷 1），30 分钟退避一到就又排进来。退避管「隔多久再试」，管不住「一天几次」。
    """

    def setUp(self) -> None:
        self.config: maa_common.Config = {"activity_search_retry_minutes": 30}
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
        self.config: maa_common.Config = {**self.config, "auto_copilot_max_attempts_per_day": 0}
        ledger = self._ledger(*[
            ("normal", "unknown", self.at - timedelta(hours=n)) for n in range(1, 6)
        ])
        self.assertIn("SR-5", [t["stage"] for t in self._collect(ledger)[0]])

    def test_a_broken_cap_config_falls_back_to_the_default(self) -> None:
        self.assertEqual(maa_copilot.daily_attempt_cap({}), 2)
        self.assertEqual(maa_copilot.daily_attempt_cap(None), 2)
        self.assertEqual(
            maa_copilot.daily_attempt_cap({"auto_copilot_max_attempts_per_day": "x"}), 2  # pyright: ignore[reportArgumentType]  # 故意传坏值
        )

    def test_the_cap_is_not_a_runtime_cache_key(self) -> None:
        # 仓库拥有这个键：线上没有写回它的路径，部署要能把改动带过去（不变量 #25）。
        seed = json.loads((ROOT / "state" / "config.json").read_text("utf-8"))
        self.assertEqual(seed["auto_copilot_max_attempts_per_day"], 2)
        self.assertNotIn(
            "auto_copilot_max_attempts_per_day", deploy.PRESERVED_RUNTIME_CACHE_KEYS
        )


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
                patch.object(maa_copilot, "save_ledger", side_effect=lambda ledger, *a: ledger),
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
                patch.object(maa_playcover, "game_is_online", return_value=(False, "")),
                patch.object(worker, "_retire_round_controls"),
                patch.object(worker, "_notice"),
                patch.object(worker, "require_executable"),
                patch.object(maa_playcover, "launch_game"),
                patch.object(maa_playcover, "wait_for_game"),
                patch.object(maa_playcover, "verify_game_installed"),
                patch.object(maa_playcover, "settle_game_window"),
                patch.object(worker, "maa_subprocess_env", return_value={}),
                patch.object(maa_playcover, "capture_screenshot", return_value=(True, "")),
                patch.object(maa_playcover, "close_game", return_value=[]),
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
            run_kwargs: dict[str, Any] = (
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
                        "明日方舟正在运行：游戏进程在跑"):
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
                     patch.object(maa_playcover, "launch_game") as launch_game, \
                     patch.object(maa_playcover, "game_is_online",
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
                launch_game.assert_not_called()
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
             patch.object(maa_copilot, "save_ledger", side_effect=lambda ledger, *a: ledger), \
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



if __name__ == "__main__":
    unittest.main()
