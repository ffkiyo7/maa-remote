"""状态文件 `schema_version`（DEV_GUIDE §6.1）的单测。

覆盖 `config.json` / `plan.json` / `last_run.json` / `manual_run.json` / `roguelike.json`：
每个文件都有「旧格式无版本号照常能读」「未知高版本」两类用例，外加写入点带版本号、
`deploy_state()` 的 config 迁移。全部在临时目录里，不碰真实 `~/.hermes`。
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

# 必须先于任何 scripts/ 模块：把 HOME 换成临时目录，碰真实 home / 外网当场炸。
import _isolation  # noqa: E402,F401
import maa_action  # noqa: E402
import maa_common  # noqa: E402
import maa_daily  # noqa: E402
import maa_farming  # noqa: E402
import maa_plan_card  # noqa: E402
import maa_roguelike  # noqa: E402
import deploy  # noqa: E402

FUTURE = 99
AT = datetime(2026, 11, 4, 9, 30, tzinfo=maa_common.BEIJING)
TODAY = AT.date().isoformat()


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        raw = tempfile.TemporaryDirectory()
        self.addCleanup(raw.cleanup)
        self.dir = Path(raw.name)

    def patch(self, target: object, name: str, value: object) -> None:
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def no_lock(self, *modules: object) -> None:
        for module in modules:
            self.patch(module, "state_lock", lambda: contextlib.nullcontext())


class CheckSchemaTests(unittest.TestCase):
    def test_a_missing_version_counts_as_one_and_returns_the_same_object(self) -> None:
        value = {"a": 1}
        self.assertIs(maa_common.check_schema("x.json", value, 1), value)
        self.assertNotIn("schema_version", value)

    def test_the_current_version_is_returned_untouched(self) -> None:
        value = {"schema_version": 2, "a": 1}
        self.assertIs(maa_common.check_schema("x.json", value, 2), value)

    def test_older_versions_are_upgraded_step_by_step_without_mutating_the_input(self) -> None:
        calls: list[int] = []

        def one_to_two(doc: dict) -> dict:
            # 旧文件本来就没有这个字段：迁移函数看到的是原样的 v1 文档。
            calls.append(doc.get("schema_version", 1))
            doc["b"] = doc.pop("a")
            return doc

        def two_to_three(doc: dict) -> dict:
            calls.append(doc["schema_version"])
            doc["c"] = doc["b"] * 10
            return doc

        original = {"a": 1}
        upgraded = maa_common.check_schema(
            "x.json", original, 3, {1: one_to_two, 2: two_to_three}
        )
        self.assertEqual(upgraded, {"b": 1, "c": 10, "schema_version": 3})
        self.assertEqual(calls, [1, 2])
        self.assertEqual(original, {"a": 1})

    def test_a_missing_migration_step_refuses(self) -> None:
        with self.assertRaisesRegex(maa_common.SchemaVersionError, "缺少到 3 的迁移"):
            maa_common.check_schema("x.json", {"schema_version": 2}, 3, {1: dict})

    def test_an_unknown_newer_version_refuses_when_strict(self) -> None:
        with self.assertRaisesRegex(maa_common.SchemaVersionError, "x.json.*99.*回滚"):
            maa_common.check_schema("x.json", {"schema_version": FUTURE}, 1)
        # 调用方现有的 `except RuntimeError` 照样接得住。
        self.assertTrue(issubclass(maa_common.SchemaVersionError, RuntimeError))

    def test_malformed_versions_refuse_when_strict(self) -> None:
        for bad in ("1", True, 0, -1, 1.0, None):
            with self.subTest(bad=bad):
                with self.assertRaises(maa_common.SchemaVersionError):
                    maa_common.check_schema("x.json", {"schema_version": bad}, 1)

    def test_lenient_mode_warns_on_stderr_and_returns_the_value(self) -> None:
        value = {"schema_version": FUTURE, "status": "success"}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = maa_common.check_schema("last_run.json", value, 1, strict=False)
        self.assertIs(result, value)
        self.assertIn("警告：last_run.json", stderr.getvalue())
        self.assertIn("宽读", stderr.getvalue())

    def test_non_objects_pass_through_for_the_caller_to_judge(self) -> None:
        for value in (None, [], "x", 3):
            with self.subTest(value=value):
                self.assertEqual(maa_common.check_schema("x.json", value, 1), value)


class EveryReadIsCheckedTests(unittest.TestCase):
    """不变量 #58：读这几份状态文件的 `load_json()` 必须直接包在 `check_schema()` 里。"""

    GUARDED_PATHS = {"CONFIG_PATH", "PLAN_PATH", "LAST_RUN_PATH", "MANUAL_RUN_PATH", "STATE_PATH"}

    def test_every_state_read_goes_through_check_schema(self) -> None:
        checked = 0
        for script in sorted((ROOT / "scripts").glob("maa_*.py")):
            tree = ast.parse(script.read_text(encoding="utf-8"))
            parents = {
                child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
            }
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "load_json"
                    and node.args
                ):
                    continue
                names = {n.id for n in ast.walk(node.args[0]) if isinstance(n, ast.Name)}
                guarded = names & self.GUARDED_PATHS
                if not guarded or (script.name != "maa_roguelike.py" and guarded == {"STATE_PATH"}):
                    continue
                parent = parents.get(node)
                with self.subTest(script=script.name, line=node.lineno):
                    self.assertTrue(
                        isinstance(parent, ast.Call)
                        and isinstance(parent.func, ast.Name)
                        and parent.func.id == "check_schema",
                        f"{script.name}:{node.lineno} 读 {guarded} 没过 check_schema()",
                    )
                checked += 1
        # 防止上面的匹配条件悄悄失效（一个都没查到也算「全绿」）。
        self.assertGreaterEqual(checked, 12)


class SeedVersionTests(unittest.TestCase):
    def test_seeds_carry_the_versions_the_scripts_expect(self) -> None:
        expected = {
            "config.json": maa_common.CONFIG_SCHEMA_VERSION,
            "plan.json": maa_common.PLAN_SCHEMA_VERSION,
            "last_run.json": maa_common.LAST_RUN_SCHEMA_VERSION,
        }
        for name, version in expected.items():
            with self.subTest(name=name):
                self.assertEqual(read(ROOT / "state" / name)["schema_version"], version)

    def test_schema_version_is_not_a_preserved_runtime_cache(self) -> None:
        # 版本号归仓库种子所有：混进保留表，种子升版本就永远推不到线上。
        self.assertNotIn("schema_version", deploy.PRESERVED_RUNTIME_CACHE_KEYS)
        self.assertNotIn("schema_version", deploy.RETIRED_CONFIG_KEYS)


class ConfigSchemaTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.path = self.dir / "config.json"
        self.patch(maa_common, "CONFIG_PATH", self.path)
        self.patch(maa_plan_card, "CONFIG_PATH", self.path)

    def test_a_legacy_config_without_a_version_loads(self) -> None:
        write(self.path, {"channel_id": "1"})
        self.assertEqual(maa_common.load_config(), {"channel_id": "1"})

    def test_an_unknown_newer_config_refuses_to_load(self) -> None:
        write(self.path, {"schema_version": FUTURE, "channel_id": "1"})
        with self.assertRaisesRegex(maa_common.SchemaVersionError, "config.json"):
            maa_common.load_config()

    def test_the_version_probe_keeps_the_version_and_refuses_a_newer_file(self) -> None:
        write(self.path, {"schema_version": 1, "maa_cli_version": "v0"})
        maa_plan_card.persist_installed_versions({}, "v0.7.5", "v6.21.3")
        self.assertEqual(
            read(self.path),
            {"schema_version": 1, "maa_cli_version": "v0.7.5", "maa_core_version": "v6.21.3"},
        )
        write(self.path, {"schema_version": FUTURE})
        with self.assertRaises(maa_common.SchemaVersionError):
            maa_plan_card.persist_installed_versions({}, "v0.7.5", "")
        self.assertEqual(read(self.path), {"schema_version": FUTURE})


class PlanSchemaTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.path = self.dir / "plan.json"
        self.defaults = self.dir / "defaults.json"
        write(self.defaults, {"stage": "1-7"})
        for module in (maa_common, maa_action, maa_plan_card):
            self.patch(module, "PLAN_PATH", self.path)
        self.patch(maa_common, "DEFAULTS_PATH", self.defaults)
        self.patch(maa_action, "now_beijing", lambda: AT)
        self.patch(maa_action, "load_config", lambda: {})
        self.patch(maa_action, "is_run_locked", lambda: False)
        self.no_lock(maa_action, maa_plan_card)

    def test_a_legacy_plan_without_a_version_still_drives_the_session(self) -> None:
        write(self.path, {"date": TODAY, "sessions": {"am": {"stage": "CE-6", "stage_source": "discord"}}})
        self.assertEqual(maa_common.effective_plan(AT, "am")["stage"], "CE-6")

    def test_an_unknown_newer_plan_refuses_to_run(self) -> None:
        write(self.path, {"schema_version": FUTURE, "date": TODAY, "sessions": {}})
        with self.assertRaisesRegex(maa_common.SchemaVersionError, "plan.json"):
            maa_common.effective_plan(AT, "am")
        with self.assertRaises(maa_common.SchemaVersionError):
            maa_action.set_stage("CE-6", "am")
        self.assertEqual(read(self.path)["schema_version"], FUTURE)

    def test_the_discord_writer_upgrades_a_legacy_plan_with_a_version(self) -> None:
        write(self.path, {"date": TODAY, "sessions": {"pm": {"skip": True}}})
        ok, _message = maa_action.set_stage("CE-6", "am")
        self.assertTrue(ok)
        written = read(self.path)
        self.assertEqual(written["schema_version"], maa_common.PLAN_SCHEMA_VERSION)
        self.assertEqual(written["sessions"]["pm"], {"skip": True})
        self.assertEqual(written["sessions"]["am"]["stage"], "CE-6")

    def test_the_auto_stage_writer_stamps_the_version(self) -> None:
        with patch.object(maa_plan_card, "auto_stage", return_value=("CE-6", "")), patch.object(
            maa_farming, "drops_cap", return_value={}
        ), patch.object(maa_farming, "fallback_stage", return_value=""):
            self.assertEqual(maa_plan_card.apply_auto_stage({}, "am", AT), "CE-6")
        self.assertEqual(read(self.path)["schema_version"], maa_common.PLAN_SCHEMA_VERSION)
        write(self.path, {"schema_version": FUTURE, "date": TODAY, "sessions": {}})
        with patch.object(maa_plan_card, "auto_stage", return_value=("CE-6", "")), patch.object(
            maa_farming, "drops_cap", return_value={}
        ), patch.object(maa_farming, "fallback_stage", return_value=""):
            with self.assertRaises(maa_common.SchemaVersionError):
                maa_plan_card.apply_auto_stage({}, "am", AT)


class LastRunSchemaTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.path = self.dir / "last_run.json"
        for module in (maa_common, maa_action):
            self.patch(module, "LAST_RUN_PATH", self.path)
        self.patch(maa_common, "last_run_lock", lambda: contextlib.nullcontext())

    def test_a_legacy_last_run_merges_and_gains_a_version(self) -> None:
        write(self.path, {"status": "running", "threads": [{"thread_id": "1"}]})
        result = maa_common.merge_last_run({"status": "success", "threads": [{"thread_id": "2"}]})
        self.assertEqual(result["status"], "success")
        self.assertEqual([t["thread_id"] for t in result["threads"]], ["1", "2"])
        self.assertEqual(read(self.path)["schema_version"], maa_common.LAST_RUN_SCHEMA_VERSION)

    def test_an_unknown_newer_last_run_only_warns_and_is_read_leniently(self) -> None:
        write(self.path, {"schema_version": FUTURE, "status": "running", "threads": "garbage"})
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = maa_common.merge_last_run({"status": "success"})
        self.assertIn("last_run.json", stderr.getvalue())
        self.assertEqual(result["status"], "success")
        # 现有 normalize 照旧：threads 不是 list 就重置成空表。
        self.assertEqual(result["threads"], [])

    def test_an_unknown_newer_last_run_still_reports_through_log_tail(self) -> None:
        write(
            self.path,
            {
                "schema_version": FUTURE,
                "status": "success",
                "summary": "🌟 总体：✅ 成功",
                "finished_at": "2026-11-04T08:31:02+08:00",
            },
        )
        with contextlib.redirect_stderr(io.StringIO()):
            ok, message = maa_action.log_tail()
        self.assertTrue(ok)
        self.assertIn("🌟 总体：✅ 成功", message)

    def test_thread_cleanup_reads_a_newer_file_leniently_and_stamps_it(self) -> None:
        write(self.path, {"schema_version": FUTURE, "threads": []})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                maa_common.cleanup_old_threads("token", AT), {"deleted": 0, "failed": 0}
            )
        self.assertEqual(read(self.path)["schema_version"], maa_common.LAST_RUN_SCHEMA_VERSION)


class ManualRunSchemaTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.path = self.dir / "manual_run.json"
        self.patch(maa_action, "MANUAL_RUN_PATH", self.path)
        self.patch(maa_daily, "MANUAL_RUN_PATH", self.path)
        self.no_lock(maa_daily)

    def test_a_legacy_manual_run_request_still_dispatches(self) -> None:
        write(self.path, {"token": "abc", "status": "dispatched", "stage": "CE-6"})
        self.assertEqual(maa_action._load_manual_run()["stage"], "CE-6")
        self.assertEqual(maa_daily.manual_run_stage("abc"), "CE-6")

    def test_an_unknown_newer_manual_run_refuses_to_run(self) -> None:
        write(self.path, {"schema_version": FUTURE, "token": "abc", "status": "dispatched", "stage": "CE-6"})
        with self.assertRaisesRegex(maa_common.SchemaVersionError, "manual_run.json"):
            maa_daily.manual_run_stage("abc")
        with self.assertRaises(maa_common.SchemaVersionError):
            maa_action._load_manual_run()

    def test_the_writer_stamps_the_version(self) -> None:
        maa_action._save_manual_run({"token": "abc", "status": "pending"})
        self.assertEqual(read(self.path)["schema_version"], maa_common.MANUAL_RUN_SCHEMA_VERSION)
        self.assertEqual(maa_action._load_manual_run()["status"], "pending")


class RoguelikeSchemaTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.path = self.dir / "roguelike.json"

    def test_a_legacy_state_without_a_version_loads(self) -> None:
        write(self.path, {"status": maa_roguelike.RUNNING, "theme": "Sami"})
        state = maa_roguelike.load_state(self.path)
        self.assertEqual(state["status"], maa_roguelike.RUNNING)
        self.assertEqual(state["theme"], "Sami")

    def test_an_unknown_newer_state_refuses(self) -> None:
        write(self.path, {"schema_version": FUTURE, "status": maa_roguelike.IDLE})
        with self.assertRaisesRegex(maa_common.SchemaVersionError, "roguelike.json"):
            maa_roguelike.load_state(self.path)

    def test_a_broken_state_still_reads_as_empty(self) -> None:
        write(self.path, ["not", "an", "object"])
        self.assertEqual(maa_roguelike.load_state(self.path)["status"], maa_roguelike.IDLE)
        self.assertEqual(maa_roguelike.load_state(self.dir / "missing.json")["status"], maa_roguelike.IDLE)

    def test_save_stamps_the_version(self) -> None:
        maa_roguelike.save_state(maa_roguelike.load_state(self.path), self.path)
        self.assertEqual(read(self.path)["schema_version"], maa_roguelike.STATE_SCHEMA_VERSION)


class DeployConfigMigrationTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo_state = self.dir / "repo" / "state"
        self.repo_state.mkdir(parents=True)
        for name in ("defaults.json", "plan.json", "last_run.json"):
            write(self.repo_state / name, {})
        self.live = self.dir / "hermes" / "state" / "maa"
        self.live.mkdir(parents=True)
        self.patch(deploy, "ROOT", self.dir / "repo")
        self.patch(deploy, "HERMES_HOME", self.dir / "hermes")

    def seed(self, value: dict) -> None:
        write(self.repo_state / "config.json", value)

    def test_a_legacy_live_config_gains_the_seed_version_and_keeps_its_caches(self) -> None:
        self.seed({"schema_version": 1, "activity_stages": ["SEED-1"], "retention_days": 14})
        write(self.live / "config.json", {"activity_stages": [], "clearing_enabled": True})
        deploy.deploy_state("424242")
        result = read(self.live / "config.json")
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["activity_stages"], [])
        self.assertEqual(result["retention_days"], 14)
        self.assertNotIn("clearing_enabled", result)

    def test_a_newer_live_config_refuses_the_deploy_and_is_left_alone(self) -> None:
        self.seed({"schema_version": 1})
        live = {"schema_version": 2, "channel_id": "424242"}
        write(self.live / "config.json", live)
        with self.assertRaisesRegex(RuntimeError, "schema_version=2.*旧提交"):
            deploy.deploy_state("424242")
        self.assertEqual(read(self.live / "config.json"), live)

    def test_migrations_run_before_the_seed_is_merged(self) -> None:
        seen: list[dict] = []

        def rename(doc: dict) -> dict:
            seen.append(dict(doc))
            doc["new_key"] = doc.pop("old_key")
            return doc

        self.seed({"schema_version": 2, "retention_days": 14})
        write(self.live / "config.json", {"old_key": "kept"})
        with patch.dict(deploy.CONFIG_MIGRATIONS, {1: rename}):
            deploy.deploy_state("424242")
        self.assertEqual(seen, [{"old_key": "kept"}])
        result = read(self.live / "config.json")
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["new_key"], "kept")
        self.assertNotIn("old_key", result)

    def test_a_missing_config_migration_refuses(self) -> None:
        self.seed({"schema_version": 2})
        write(self.live / "config.json", {})
        with self.assertRaisesRegex(RuntimeError, "1 → 2 的迁移"):
            deploy.deploy_state("424242")


if __name__ == "__main__":
    unittest.main()
