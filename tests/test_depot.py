"""仓库识别（`maa_depot`）与 OperBox 练度后备。

从原 `tests/test_maa.py` 按领域拆出；共用桩见 `_helpers.py`。
"""
from __future__ import annotations

import contextlib
import io
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
import maa_common  # noqa: E402
import maa_copilot  # noqa: E402
import maa_depot  # noqa: E402
import maa_daily  # noqa: E402
import maa_plan_card  # noqa: E402
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


class DepotTests(unittest.TestCase):
    def _resource(self, root: Path) -> maa_common.Config:
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
        config: maa_common.Config = {"maa_bin": "/maa"}
        with patch.object(maa_depot, "run_lock", return_value=context), patch.object(
            maa_playcover, "game_is_online", return_value=(False, "离线")
        ), patch.object(maa_daily, "require_executable"), patch.object(
            maa_playcover, "launch_game"
        ), patch.object(
            maa_playcover, "wait_for_game"
        ), patch.object(maa_playcover, "verify_game_installed"), patch.object(
            maa_playcover, "settle_game_window"
        ), patch.object(maa_playcover, "close_game", return_value=[]), patch.object(
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
        config: maa_common.Config = {"skland_roster_refresh_days": 7}
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
            config: maa_common.Config = {"maa_bin": "/maa", "maa_resource_dir": str(root)}
            operbox_roster = root / "roster-operbox.json"
            with patch.object(maa_depot, "run_lock", return_value=context), patch.object(
                maa_playcover, "game_is_online", return_value=(False, "离线")
            ), patch.object(maa_daily, "require_executable"), patch.object(
                maa_playcover, "launch_game"
            ), patch.object(maa_playcover, "wait_for_game"), patch.object(
                maa_playcover, "verify_game_installed"
            ), patch.object(maa_playcover, "settle_game_window"), patch.object(
                maa_playcover, "close_game", return_value=[]
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



if __name__ == "__main__":
    unittest.main()
