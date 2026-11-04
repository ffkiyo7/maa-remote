"""森空岛（`maa_skland`）：过期提醒、理智估算。

从原 `tests/test_maa.py` 按领域拆出；共用桩见 `_helpers.py`。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

# 必须先于任何 scripts/ 模块：把 HOME 换成临时目录，碰真实 home / 外网当场炸。
import _isolation  # noqa: E402
import _helpers  # noqa: E402
import maa_common  # noqa: E402
import maa_copilot  # noqa: E402


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


class SklandDailyHealthTests(unittest.TestCase):
    """森空岛过期：每个北京日在频道里提醒一次，直到恢复。"""

    def _check(self, *, skland, error=None, at=None):
        import maa_skland

        notices: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "skland.json"
            if skland is not None:
                maa_common.atomic_write_json(path, skland)
            with patch.object(maa_skland, "SKLAND_PATH", path), \
                 patch.object(maa_copilot, "ensure_private_dirs"), \
                 patch.object(maa_skland, "refresh_roster", side_effect=error,
                              return_value={}):
                status = maa_skland.daily_health_check(
                    lambda title, body: notices.append((title, body)), at
                )
            stored = json.loads(path.read_text("utf-8"))
        return status, notices, stored

    AT = datetime(2026, 10, 12, 7, 50, tzinfo=maa_common.BEIJING)

    def test_an_expired_login_is_announced_with_the_fix(self) -> None:
        status, notices, stored = self._check(
            skland={"hg_token": "t"},
            error=RuntimeError("cred 过期且重认证失败；请在本机终端运行 ... link"),
            at=self.AT,
        )
        self.assertEqual(status, "notified")
        self.assertEqual(len(notices), 1)
        self.assertIn("过期", notices[0][0])
        self.assertIn("maa_skland.py link", notices[0][1])
        self.assertEqual(stored["health_checked_on"], "2026-10-12")
        self.assertEqual(stored["hg_token"], "t")

    def test_only_one_notice_per_day(self) -> None:
        status, notices, _ = self._check(
            skland={"hg_token": "t", "health_checked_on": "2026-10-12"},
            error=RuntimeError("过期"), at=self.AT,
        )
        self.assertEqual(status, "checked")
        self.assertEqual(notices, [])

    def test_the_next_day_reminds_again(self) -> None:
        status, notices, _ = self._check(
            skland={"hg_token": "t", "health_checked_on": "2026-10-11"},
            error=RuntimeError("cred 过期"), at=self.AT,
        )
        self.assertEqual(status, "notified")
        self.assertEqual(len(notices), 1)

    def test_a_healthy_login_stays_quiet(self) -> None:
        status, notices, stored = self._check(skland={"hg_token": "t"}, at=self.AT)
        self.assertEqual(status, "ok")
        self.assertEqual(notices, [])
        self.assertEqual(stored["health_checked_on"], "2026-10-12")

    def test_a_failed_discord_post_leaves_the_day_unchecked(self) -> None:
        import maa_skland

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "skland.json"
            maa_common.atomic_write_json(path, {"hg_token": "t"})

            def notify(_t, _b):
                raise RuntimeError("discord down")

            with patch.object(maa_skland, "SKLAND_PATH", path), \
                 patch.object(maa_skland, "refresh_roster",
                              side_effect=RuntimeError("过期")):
                with self.assertRaises(RuntimeError):
                    maa_skland.daily_health_check(notify, self.AT)
            self.assertNotIn("health_checked_on", json.loads(path.read_text("utf-8")))

    def test_a_successful_refresh_clears_the_stale_error(self) -> None:
        import maa_skland

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "skland.json"
            maa_common.atomic_write_json(
                path, {"hg_token": "t", "cred": "c", "sign_token": "s", "uid": "1",
                       "last_error": "cred 过期"}
            )
            with patch.object(maa_skland, "SKLAND_PATH", path), \
                 patch.object(maa_skland, "ROSTER_PATH", root / "roster.json"), \
                 patch.object(maa_skland, "fetch_roster",
                              return_value={"chars": {}, "activity_progress": {}}):
                maa_skland.refresh_roster()
            self.assertNotIn("last_error", json.loads(path.read_text("utf-8")))


class SklandSanityEstimateTests(unittest.TestCase):
    def test_regeneration_counts_from_the_last_change_not_from_the_read(self) -> None:
        """2026-10-20：06:30 读到 current=28、lastApAddTime=前一天 18:24，实际是 164。"""
        tz = timezone(timedelta(hours=8))
        last_add = datetime(2026, 10, 19, 18, 24, 58, tzinfo=tz)
        read_at = datetime(2026, 10, 20, 6, 30, 0, tzinfo=tz)
        snapshot = {"current": 28, "max": 208,
                    "last_ap_add_at": int(last_add.timestamp()),
                    "server_current_at": int(read_at.timestamp())}
        self.assertEqual(maa_copilot.estimated_sanity(snapshot, read_at), 28 + 120)
        at_eight = datetime(2026, 10, 20, 8, 1, 10, tzinfo=tz)  # MAA 读到 164 的时刻
        self.assertEqual(maa_copilot.estimated_sanity(snapshot, at_eight), 164)
        self.assertTrue(maa_copilot.sanity_preflight({"sanity": snapshot}, 30, read_at)["enough"])

    def test_the_estimate_never_exceeds_the_cap(self) -> None:
        snapshot = {"current": 200, "max": 208, "last_ap_add_at": 1, "server_current_at": 2}
        self.assertEqual(maa_copilot.estimated_sanity(snapshot), 208)



if __name__ == "__main__":
    unittest.main()
