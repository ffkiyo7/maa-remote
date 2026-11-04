"""`_isolation` 这道闸门本身的回归：路径常量都指向临时 HOME，碰真实 home / 外网当场炸。"""

from __future__ import annotations

import os
import socket
import sys
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
