"""`_isolation` 这道闸门本身的回归：路径常量都指向临时 HOME，碰真实 home / 外网当场炸。"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import _isolation  # noqa: E402
import deploy  # noqa: E402
import maa_common  # noqa: E402
import maa_copilot  # noqa: E402
import maa_plan_card  # noqa: E402
import maa_playcover  # noqa: E402


class IsolationTests(unittest.TestCase):
    def test_import_time_paths_point_into_the_temp_home(self) -> None:
        for path in (
            maa_common.HERMES_HOME,
            maa_common.STATE_DIR,
            maa_common.MAA_CONFIG_DIR,
            deploy.HERMES_HOME,
            deploy.MAA_CONFIG_HOME,
            deploy.backup_root(),
            maa_copilot.DEFAULT_RESOURCE_DIR,
            maa_plan_card.DEFAULT_STAGE_ACTIVITY_PATH,
            maa_playcover.executable_path({}),
        ):
            with self.subTest(path=str(path)):
                self.assertTrue(
                    Path(path).is_relative_to(_isolation.TEMP_HOME),
                    f"{path} 不在临时 HOME {_isolation.TEMP_HOME} 下",
                )

    def test_backup_root_follows_a_patched_hermes_home(self) -> None:
        other = _isolation.TEMP_HOME / "elsewhere"
        with patch.object(deploy, "HERMES_HOME", other):
            self.assertTrue(deploy.backup_root().is_relative_to(other))

    def test_touching_the_real_home_raises_past_except_exception(self) -> None:
        live = _isolation.REAL_HOME / ".hermes" / "state" / "maa" / "config.json"
        with self.assertRaises(_isolation.RealHomeTouched):
            try:
                live.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001 - 正是要验它吞不掉
                self.fail("RealHomeTouched 被 except Exception 吞掉了")
        with self.assertRaises(_isolation.RealHomeTouched):
            os.listdir(_isolation.REAL_HOME / ".hermes")
        with self.assertRaises(_isolation.RealHomeTouched):
            maa_common.atomic_write_json(live.with_name("probe.json"), {})

    def test_reaching_the_internet_raises_but_loopback_is_allowed(self) -> None:
        if _isolation.LIVE_TESTS:
            self.skipTest("MAA_LIVE_TESTS=1 时故意放开外网")
        with self.assertRaises(_isolation.RealNetworkTouched):
            socket.getaddrinfo("discord.com", 443)
        socket.getaddrinfo("127.0.0.1", 0)


class SharedHomeCleanTests(unittest.TestCase):
    """`assert_shared_home_clean()`：共享临时 HOME 里留了状态文件要报错。

    在独立的临时目录上验（`root=`），不往真正的共享 HOME 里放东西。
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def touch(self, rel: str, content: str = "") -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_empty_whitelisted_locks_and_empty_dirs_pass(self) -> None:
        for rel in _isolation.SHARED_HOME_ALLOWED:
            self.touch(rel)
        (self.root / ".hermes" / "state" / "maa" / "copilot").mkdir(parents=True)
        _isolation.assert_shared_home_clean(root=self.root)

    def test_a_leaked_state_file_is_reported_by_path(self) -> None:
        self.touch(".hermes/state/maa/run.lock")
        self.touch(".hermes/state/maa/copilot/stage_index.json", "{}")
        with self.assertRaises(AssertionError) as caught:
            _isolation.assert_shared_home_clean(root=self.root)
        message = str(caught.exception)
        self.assertIn(".hermes/state/maa/copilot/stage_index.json（2 B）", message)
        self.assertNotIn("run.lock", message)

    def test_a_whitelisted_lock_with_content_is_reported(self) -> None:
        self.touch(".hermes/state/agent-book/state.lock", "123")
        with self.assertRaises(AssertionError) as caught:
            _isolation.assert_shared_home_clean(root=self.root)
        self.assertIn("锁文件应为空", str(caught.exception))

    def test_the_whitelist_is_by_exact_path_and_can_be_narrowed(self) -> None:
        # 同名锁换个目录不算白名单。
        self.touch("run.lock")
        with self.assertRaises(AssertionError):
            _isolation.assert_shared_home_clean(root=self.root)
        (self.root / "run.lock").unlink()
        self.touch(".hermes/state/maa/run.lock")
        with self.assertRaises(AssertionError):
            _isolation.assert_shared_home_clean(frozenset(), root=self.root)

    def test_the_default_root_is_the_shared_temp_home(self) -> None:
        self.assertEqual(
            _isolation.shared_home_files(),
            _isolation.shared_home_files(_isolation.TEMP_HOME),
        )

    def test_every_test_module_checks_the_shared_home_on_teardown(self) -> None:
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            if path.name == "test_isolation.py":
                continue
            with self.subTest(module=path.name):
                body = path.read_text(encoding="utf-8").split("def tearDownModule()")
                self.assertEqual(len(body), 2, "缺 tearDownModule()")
                self.assertIn(
                    "_isolation.assert_shared_home_clean()",
                    body[1].split("\ndef ")[0].split("\nclass ")[0],
                )
