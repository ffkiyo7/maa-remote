"""PlayCover 设备层（`maa_playcover`）的单测。

真游戏一概不碰：MaaTools 换成本机随机端口上的假服务端，`pgrep` / `open` / `pkill` /
`osascript` / `screencapture` 全部打桩。
"""

from __future__ import annotations

import io
import plistlib
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

# 必须先于任何 scripts/ 模块：把 HOME 换成临时目录，碰真实 home / 外网当场炸。
import _isolation  # noqa: E402
import maa_playcover  # noqa: E402

if TYPE_CHECKING:
    import maa_common


def tearDownModule() -> None:
    _isolation.assert_shared_home_clean()


class FakeMaaTools:
    """最小的 MaaTools 服务端：握手、VERN、SIZE、SCRN、TUCH、TERM，记下收到的命令。

    `version` 是 VERN 回的协议版本；设成 `None` 时收到 VERN 就断开（模拟不认 VERN 的旧版）。
    """

    def __init__(self, size=(3840, 2160), frame: bytes | None = None) -> None:
        self.size = size
        self.frame = frame
        self.version: int | None = maa_playcover.MIN_MAATOOLS_VERSION
        self.commands: list[tuple[bytes, bytes]] = []
        self.handlers: list[threading.Thread] = []
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def config(self) -> maa_common.Config:
        return {"maatools_address": f"127.0.0.1:{self.port}"}

    def _recv(self, conn: socket.socket, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            handler = threading.Thread(target=self._handle, args=(conn,), daemon=True)
            self.handlers.append(handler)
            handler.start()

    def drained(self) -> list[tuple[bytes, bytes]]:
        """等客户端断开后服务端把缓冲里的命令读完，再交出命令表。"""
        for handler in list(self.handlers):
            handler.join(timeout=2)
        return self.commands

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            try:
                if self._recv(conn, 4) != b"MAA\x00":
                    return
                conn.sendall(b"OKAY")
                while True:
                    (length,) = struct.unpack(">H", self._recv(conn, 2))
                    body = self._recv(conn, length)
                    command, payload = body[:4], body[4:]
                    self.commands.append((command, payload))
                    if command == b"VERN":
                        if self.version is None:
                            return
                        conn.sendall(struct.pack(">I", self.version))
                    elif command == b"SIZE":
                        conn.sendall(struct.pack(">HH", *self.size))
                    elif command == b"SCRN":
                        frame = self.frame or b""
                        conn.sendall(struct.pack(">I", len(frame)) + frame)
            except (ConnectionError, OSError, struct.error):
                return

    def close(self) -> None:
        self.server.close()


def _read_png(data: bytes) -> tuple[int, int, bytes]:
    """解 `_rgba_png` 写出的 PNG（只有一个 IDAT、filter 全 0）。"""
    assert data[:8] == maa_playcover.PNG_MAGIC
    width, height = struct.unpack(">II", data[16:24])
    offset, idat = 8, b""
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset:offset + 4])
        kind = data[offset + 4:offset + 8]
        if kind == b"IDAT":
            idat += data[offset + 8:offset + 8 + length]
        offset += 12 + length
    return width, height, zlib.decompress(idat)


class MaaToolsProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeMaaTools()
        self.addCleanup(self.fake.close)

    def test_ready_reports_the_window_size(self) -> None:
        ready, detail = maa_playcover.maatools_ready(self.fake.config)
        self.assertTrue(ready)
        self.assertIn("3840x2160", detail)
        self.assertIn("v4", detail)

    def test_handshake_reads_the_protocol_version_before_any_command(self) -> None:
        self.fake.version = 5
        with maa_playcover.MaaTools(self.fake.config) as tools:
            self.assertEqual(tools.version, 5)
            tools.size()
        commands = [command for command, _ in self.fake.drained()]
        self.assertEqual(commands, [b"VERN", b"SIZE"])
        self.assertEqual(self.fake.commands[0], (b"VERN", b""))

    def test_an_old_protocol_version_is_refused_and_the_socket_closed(self) -> None:
        self.fake.version = 3
        tools = maa_playcover.MaaTools(self.fake.config)
        with self.assertRaises(maa_playcover.MaaToolsError) as caught:
            tools.__enter__()
        message = str(caught.exception)
        self.assertIn("v3", message)
        self.assertIn("v4", message)
        self.assertIn("升级 PlayCover MAA 分支", message)
        self.assertIsNone(tools.sock)
        self.assertIsNone(tools.version)
        ready, detail = maa_playcover.maatools_ready(self.fake.config)
        self.assertFalse(ready)
        self.assertIn("低于要求", detail)
        # 版本不够就不该再发任何命令。
        self.assertNotIn(b"SIZE", [command for command, _ in self.fake.drained()])

    def test_a_peer_that_hangs_up_on_vern_gets_its_own_error(self) -> None:
        self.fake.version = None
        tools = maa_playcover.MaaTools(self.fake.config)
        with self.assertRaises(maa_playcover.MaaToolsError) as caught:
            tools.__enter__()
        message = str(caught.exception)
        self.assertIn("VERN", message)
        self.assertIn("对端关闭", message)
        self.assertIsNone(tools.sock)
        ready, detail = maa_playcover.maatools_ready(self.fake.config)
        self.assertFalse(ready)
        self.assertIn("VERN", detail)

    def test_a_silent_peer_on_vern_times_out_with_a_clear_error(self) -> None:
        # 回了 OKAY 却对 VERN 一声不吭：超时也要报成 VERN 读不到，而不是挂住。
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        held: list[socket.socket] = []

        def serve() -> None:
            conn, _ = server.accept()
            held.append(conn)
            conn.recv(4)
            conn.sendall(b"OKAY")

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        config: maa_common.Config = {"maatools_address": f"127.0.0.1:{server.getsockname()[1]}"}
        tools = maa_playcover.MaaTools(config, timeout=0.3)
        with self.assertRaisesRegex(maa_playcover.MaaToolsError, "读不到 VERN"):
            tools.__enter__()
        self.assertIsNone(tools.sock)
        thread.join(timeout=2)
        for conn in held:
            conn.close()

    def test_ready_is_false_when_nothing_listens(self) -> None:
        self.fake.close()
        ready, _detail = maa_playcover.maatools_ready(self.fake.config, timeout=0.5)
        self.assertFalse(ready)

    def test_a_zero_sized_window_is_not_ready(self) -> None:
        self.fake.size = (0, 0)
        ready, detail = maa_playcover.maatools_ready(self.fake.config)
        self.assertFalse(ready)
        self.assertIn("0", detail)

    def test_tap_scales_base_coordinates_to_the_window_and_lifts_the_finger(self) -> None:
        with patch.object(maa_playcover.time, "sleep"):
            maa_playcover.tap(self.fake.config, 960, 540)
        touches = [
            struct.unpack(">BHH", payload)
            for command, payload in self.fake.drained()
            if command == b"TUCH"
        ]
        self.assertEqual(touches, [(0, 1920, 1080), (3, 1920, 1080)])

    def test_tap_refuses_coordinates_outside_the_game(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "越界"):
            maa_playcover.tap(self.fake.config, 2000, 10)
        self.assertFalse([c for c, _ in self.fake.commands if c == b"TUCH"])

    def test_capture_downsamples_a_retina_frame_to_1920_wide(self) -> None:
        width, height = 8, 4
        pixels = b"".join(
            bytes((x * 10, y * 10, 0, 255)) for y in range(height) for x in range(width)
        )
        self.fake.size, self.fake.frame = (width, height), pixels
        with tempfile.TemporaryDirectory() as raw, patch.object(
            maa_playcover, "MAX_SHOT_WIDTH", 4
        ):
            shot = Path(raw) / "shot.png"
            geometry = maa_playcover.capture_geometry(self.fake.config, shot)
            png_width, png_height, rows = _read_png(shot.read_bytes())
        self.assertEqual(geometry, {"source": "maatools", "width": 4, "height": 2, "top": 0})
        self.assertEqual((png_width, png_height), (4, 2))
        # 每行 1 字节 filter + 4 像素 × 4 字节；隔一个像素取一个。
        self.assertEqual(len(rows), 2 * (1 + 16))
        self.assertEqual(rows[1:5], bytes((0, 0, 0, 255)))
        self.assertEqual(rows[5:9], bytes((20, 0, 0, 255)))
        self.assertEqual(rows[18:22], bytes((0, 20, 0, 255)))

    def test_an_empty_frame_falls_back_to_the_window_and_fails_loudly_offscreen(self) -> None:
        # 窗口被台前调度收走时 SCRN 回 0 字节：退到按窗口截图；窗口也不在屏上就明说。
        self.fake.frame = b""
        with tempfile.TemporaryDirectory() as raw, patch.object(
            maa_playcover, "game_window", return_value={"id": 7, "onscreen": False}
        ):
            ok, error = maa_playcover.capture_screenshot(self.fake.config, Path(raw) / "s.png")
        self.assertFalse(ok)
        self.assertIn("不在屏上", error)

    def test_a_short_frame_is_an_error_not_a_garbled_png(self) -> None:
        self.fake.size, self.fake.frame = (4, 4), b"\x00" * 12
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(RuntimeError, "尺寸不符"):
                maa_playcover.capture_geometry(self.fake.config, Path(raw) / "s.png")


class GeometryTests(unittest.TestCase):
    def test_window_capture_coordinates_drop_the_title_bar(self) -> None:
        geometry = {"source": "window", "width": 3840, "height": 2216, "top": 56}
        self.assertEqual(maa_playcover.image_to_base(geometry, 3840, 56), (1920, 0))
        self.assertEqual(maa_playcover.image_to_base(geometry, 1920, 1136), (960, 540))

    def test_maatools_capture_is_already_in_base_coordinates(self) -> None:
        geometry = {"source": "maatools", "width": 1920, "height": 1080, "top": 0}
        self.assertEqual(maa_playcover.image_to_base(geometry, 100, 200), (100, 200))


class ConfigTests(unittest.TestCase):
    def test_only_loopback_addresses_are_accepted(self) -> None:
        self.assertEqual(maa_playcover.maatools_address({}), ("127.0.0.1", 1717))
        self.assertEqual(
            maa_playcover.maatools_address({"maatools_address": "localhost:1718"}),
            ("127.0.0.1", 1718),
        )
        for bad in ("10.0.0.2:1717", "127.0.0.1", "127.0.0.1:99999", "evil:1717"):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                maa_playcover.maatools_address({"maatools_address": bad})

    def test_paths_with_shell_metacharacters_are_refused(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "非法字符"):
            maa_playcover.app_path({"playcover_app_path": "/tmp/x; rm -rf ~"})
        with self.assertRaisesRegex(RuntimeError, "格式非法"):
            maa_playcover.bundle_id({"game_bundle_id": "com.x$(id)"})

    def test_process_pattern_matches_the_game_but_not_pgrep_itself(self) -> None:
        import re

        with tempfile.TemporaryDirectory() as raw:
            app = Path(raw) / "com.hypergryph.arknights.app"
            app.mkdir()
            with (app / "Info.plist").open("wb") as handle:
                plistlib.dump({"CFBundleExecutable": "arknights"}, handle)
            config: maa_common.Config = {"playcover_app_path": str(app)}
            pattern = maa_playcover.process_pattern(config)
            executable = str(app / "arknights")
        self.assertRegex(executable, pattern)
        self.assertIsNone(re.search(pattern, f"pgrep -f {pattern}"))


class InstallCheckTests(unittest.TestCase):
    def _layout(self, root: Path, settings: dict | None) -> maa_common.Config:
        app = root / "com.hypergryph.arknights.app"
        app.mkdir()
        with (app / "Info.plist").open("wb") as handle:
            plistlib.dump({"CFBundleExecutable": "arknights"}, handle)
        (app / "arknights").write_bytes(b"")
        settings_dir = root / "App Settings"
        settings_dir.mkdir()
        if settings is not None:
            with (settings_dir / "com.hypergryph.arknights.plist").open("wb") as handle:
                plistlib.dump(settings, handle)
        return {"playcover_app_path": str(app), "playcover_settings_dir": str(settings_dir)}

    def test_an_installed_game_with_maatools_on_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = self._layout(Path(raw), {"maaTools": True, "maaToolsPort": 1717})
            maa_playcover.verify_game_installed(config)

    def test_maatools_off_or_on_another_port_is_refused(self) -> None:
        cases = (
            ({"maaTools": False}, "没有启用 MaaTools"),
            ({"maaTools": True, "maaToolsPort": 1800}, "端口是 1800"),
            (None, "读不到"),
        )
        for settings, message in cases:
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as raw:
                config = self._layout(Path(raw), settings)
                with self.assertRaisesRegex(RuntimeError, message):
                    maa_playcover.verify_game_installed(config)

    def test_a_missing_game_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(RuntimeError, "没有明日方舟"):
                maa_playcover.verify_game_installed(
                    {"playcover_app_path": str(Path(raw) / "nope.app")}
                )


def _completed(returncode: int, stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


class ProbeTests(unittest.TestCase):
    """PLAN_NAV_GATE B / D18：自动链路开跑前问「你在玩吗」，探测永不抛。"""

    def test_a_running_game_process_is_online(self) -> None:
        with patch.object(maa_playcover.subprocess, "run", return_value=_completed(0, "4242\n")) as run:
            online, detail = maa_playcover.game_is_online({})
        self.assertTrue(online)
        self.assertIn("4242", detail)
        self.assertEqual(run.call_args.args[0][:2], ["pgrep", "-f"])

    def test_no_process_is_offline(self) -> None:
        with patch.object(maa_playcover.subprocess, "run", return_value=_completed(1)):
            online, _detail = maa_playcover.game_is_online({})
        self.assertFalse(online)

    def test_a_broken_probe_falls_back_to_offline_and_never_raises(self) -> None:
        for failure in (
            maa_playcover.subprocess.TimeoutExpired(cmd="pgrep", timeout=2),
            OSError("pgrep missing"),
        ):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(maa_playcover.subprocess, "run", side_effect=failure):
                    online, detail = maa_playcover.game_is_online({})
                self.assertFalse(online)
                self.assertTrue(detail)

    def test_the_probe_budget_is_bounded_and_disablable(self) -> None:
        with patch.object(maa_playcover.subprocess, "run") as run:
            online, detail = maa_playcover.game_is_online({"game_probe_timeout": 0})
        self.assertFalse(online)
        self.assertIn("探测已禁用", detail)
        run.assert_not_called()
        with patch.object(maa_playcover.subprocess, "run", return_value=_completed(1)) as run:
            maa_playcover.game_is_online({"game_probe_timeout": 60})
        self.assertLessEqual(run.call_args.kwargs["timeout"], 3.0)

    def test_an_unvalidated_path_is_offline_without_running_anything(self) -> None:
        with patch.object(maa_playcover.subprocess, "run") as run:
            online, _detail = maa_playcover.game_is_online(
                {"playcover_app_path": "/x; rm -rf /"}
            )
        self.assertFalse(online)
        run.assert_not_called()


def _win(onscreen: bool, x: int = 2141, y: int = 110, width: int = 1478, height: int = 859) -> dict:
    return {"id": 8385, "pid": 4242, "onscreen": onscreen, "x": x, "y": y, "width": width, "height": height}


# 2026-11-05 真机：游戏开在主屏，前台一切走就被台前调度缩成这个缩略图。
SHELVED = _win(True, x=15, y=770, width=144, height=149)
ON_MAIN = _win(True, x=234, y=89)
# 同一进程挂着的几个不是游戏画面的窗口。
STRIP = _win(False, x=1920, y=0, width=1920, height=30)
PHANTOM = _win(False, x=0, y=580, width=500, height=500)


class GameWindowTests(unittest.TestCase):
    def _pick(self, windows):
        with patch.object(maa_playcover, "game_pids", return_value=[4242]), patch.object(
            maa_playcover, "_jxa_windows", return_value=windows
        ):
            return maa_playcover.game_window({})

    def test_a_shelved_thumbnail_beats_the_offscreen_phantom_window(self) -> None:
        # 11-05 晚场：缩略图被「宽 ≥ 300」滤掉，挑中了 500×500 的空窗口，挪屏一直挪错对象。
        self.assertEqual(self._pick([STRIP, PHANTOM, SHELVED, STRIP]), SHELVED)
        self.assertTrue(maa_playcover.window_shelved(SHELVED))
        self.assertFalse(maa_playcover.window_usable(SHELVED))

    def test_the_real_window_beats_strips_and_phantom(self) -> None:
        self.assertEqual(self._pick([STRIP, PHANTOM, ON_MAIN]), ON_MAIN)
        self.assertTrue(maa_playcover.window_usable(ON_MAIN))
        # 最小化的真窗口也比空窗口大，挑它。
        minimised = _win(False)
        self.assertEqual(self._pick([PHANTOM, minimised, STRIP]), minimised)
        self.assertFalse(maa_playcover.window_shelved(minimised))

    def test_only_strips_means_no_window(self) -> None:
        self.assertIsNone(self._pick([STRIP, STRIP]))


class SettleTests(unittest.TestCase):
    def setUp(self) -> None:
        # 换屏那一步单独在 MoveToDisplayTests 里测；这里只看「在不在屏上」。
        mover = patch.object(maa_playcover, "move_to_game_display", return_value=(False, ""))
        self.mover = mover.start()
        self.addCleanup(mover.stop)
        sleep = patch.object(maa_playcover.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        # 激活后的轮询只看一次，免得 return_value 固定的用例真的空转 5 秒。
        wait = patch.object(maa_playcover, "SETTLE_ACTIVATE_WAIT", 0)
        wait.start()
        self.addCleanup(wait.stop)
        screens = patch.object(
            maa_playcover,
            "_screens",
            return_value=[
                {"name": "Q24G2", "x": 0, "y": 0, "width": 1920, "height": 1080},
                {"name": "MAA-Game", "x": 1920, "y": 0, "width": 1920, "height": 1080},
            ],
        )
        screens.start()
        self.addCleanup(screens.stop)

    def _settle(self, windows):
        if isinstance(windows, list):
            finder = patch.object(maa_playcover, "game_window", side_effect=windows)
        else:
            finder = patch.object(maa_playcover, "game_window", return_value=windows)
        with finder, patch.object(maa_playcover, "launch_game") as launch:
            return maa_playcover.settle_game_window({}), launch

    def test_a_window_on_the_wrong_display_is_moved_and_rechecked(self) -> None:
        self.mover.return_value = (True, "")
        warning, launch = self._settle([ON_MAIN, _win(True)])
        self.assertEqual(warning, "")
        # 挪过去以后它就在台前了，不必再抢一次焦点。
        launch.assert_not_called()
        self.mover.assert_called_once_with({}, ON_MAIN)

    def test_a_failed_move_is_reported_without_blocking(self) -> None:
        self.mover.return_value = (False, "没有「辅助功能」权限")
        warning, launch = self._settle(_win(True))
        self.assertIn("辅助功能", warning)
        launch.assert_not_called()

    def test_an_onscreen_window_is_left_alone(self) -> None:
        warning, launch = self._settle(_win(True))
        self.assertEqual(warning, "")
        launch.assert_not_called()

    def test_a_hidden_window_is_brought_back_once(self) -> None:
        warning, launch = self._settle([_win(False), _win(True)])
        self.assertEqual(warning, "")
        launch.assert_called_once()

    def test_a_shelved_thumbnail_is_activated_then_moved_to_the_virtual_screen(self) -> None:
        # 11-05 晚场的样子：缩略图算「在屏」，以前直接放行或者挪错窗口；
        # 现在不挪缩略图，激活后一回到主屏就马上挪去虚拟屏。
        self.mover.return_value = (True, "")
        moved = _win(True)
        warning, launch = self._settle([SHELVED, ON_MAIN, moved])
        self.assertEqual(warning, "")
        launch.assert_called_once()
        self.mover.assert_called_once_with({}, ON_MAIN)

    def test_a_window_that_comes_back_on_the_virtual_screen_is_still_recentred(self) -> None:
        # 11-05 复现：激活后直接回到 MAA-Game，却在 (2630, 290) 伸出屏外，以前不会再挪。
        # 最小化的窗口本来就在虚拟屏上（第一次不用挪）；激活后伸出屏外，要再挪一次。
        self.mover.side_effect = [(False, ""), (True, "")]
        hanging = _win(True, x=2630, y=290)
        warning, launch = self._settle([_win(False), hanging, _win(True)])
        self.assertEqual(warning, "")
        launch.assert_called_once()
        self.assertEqual(self.mover.call_args_list[-1].args, ({}, hanging))

    def test_a_window_that_stays_shelved_says_so_with_geometry(self) -> None:
        warning, launch = self._settle(SHELVED)
        launch.assert_called_once()
        self.mover.assert_not_called()
        self.assertIn("侧边栏", warning)
        self.assertIn("144×149 @ (15, 770)", warning)
        self.assertIn("MAA-Game (1920, 0)", warning)

    def test_a_window_that_stays_away_warns_but_never_raises(self) -> None:
        warning, _launch = self._settle(_win(False))
        self.assertIn("不在屏上", warning)
        self.assertIn("1478×859", warning)
        warning, _launch = self._settle(None)
        self.assertIn("没找到游戏窗口", warning)
        with patch.object(maa_playcover, "game_window", side_effect=OSError("jxa")):
            self.assertIn("照常开跑", maa_playcover.settle_game_window({}))

    def test_an_unreadable_screen_list_does_not_hide_the_warning(self) -> None:
        with patch.object(maa_playcover, "_screens", side_effect=RuntimeError("jxa")):
            warning, _launch = self._settle(_win(False))
        self.assertIn("不在屏上", warning)
        self.assertIn("读不到（RuntimeError）", warning)


class MoveToDisplayTests(unittest.TestCase):
    SCREENS = [
        {"name": "", "x": 0, "y": 0, "width": 1920, "height": 1080},
        {"name": "MAA-Game", "x": 1920, "y": 0, "width": 1920, "height": 1080},
    ]
    ON_MAIN = {"id": 5, "pid": 4242, "onscreen": True, "x": 221, "y": 89, "width": 1478, "height": 859}

    def _move(self, window, *, returncode=0, screens=None, helper=True, config=None):
        with tempfile.TemporaryDirectory() as raw:
            winmove = Path(raw) / "winmove"
            if helper:
                winmove.write_bytes(b"")
            with patch.object(maa_playcover, "_screens", return_value=screens or self.SCREENS), patch.object(
                maa_playcover, "WINMOVE", winmove
            ), patch.object(
                maa_playcover.subprocess, "run", return_value=_completed(returncode)
            ) as run:
                result = maa_playcover.move_to_game_display(config or {}, window)
        return result, run

    def test_a_window_on_the_main_screen_is_centred_on_the_virtual_screen(self) -> None:
        (moved, warning), run = self._move(self.ON_MAIN)
        self.assertEqual((moved, warning), (True, ""))
        argv = run.call_args.args[0]
        self.assertEqual(argv[1:], ["4242", str(1920 + (1920 - 1478) // 2), str((1080 - 859) // 2)])

    def test_a_window_already_there_is_left_alone(self) -> None:
        window = {**self.ON_MAIN, "x": 2141, "y": 60}
        (moved, warning), run = self._move(window)
        self.assertEqual((moved, warning), (False, ""))
        run.assert_not_called()

    def test_a_window_hanging_off_the_virtual_screen_is_recentred(self) -> None:
        # 2026-11-04 实测：直接开在虚拟屏上，中心在屏内，右边却伸出去 268px。
        window = {**self.ON_MAIN, "x": 2630, "y": 290}
        (moved, warning), run = self._move(window)
        self.assertEqual((moved, warning), (True, ""))
        self.assertEqual(run.call_args.args[0][1:], ["4242", "2141", "110"])

    def test_a_window_bigger_than_the_screen_is_not_moved_again_once_centred(self) -> None:
        big = {**self.ON_MAIN, "width": 2000, "height": 1200}
        (moved, _warning), run = self._move({**big, "x": 100, "y": 50})
        self.assertTrue(moved)
        self.assertEqual(run.call_args.args[0][1:], ["4242", "1920", "0"])
        (moved, _warning), run = self._move({**big, "x": 1920, "y": 0})
        self.assertFalse(moved)
        run.assert_not_called()

    def test_missing_permission_missing_screen_and_missing_helper_are_warnings(self) -> None:
        (moved, warning), _run = self._move(self.ON_MAIN, returncode=3)
        self.assertFalse(moved)
        self.assertIn("辅助功能", warning)
        (moved, warning), run = self._move(self.ON_MAIN, screens=self.SCREENS[:1])
        self.assertFalse(moved)
        self.assertIn("找不到虚拟屏 MAA-Game", warning)
        run.assert_not_called()
        (moved, warning), run = self._move(self.ON_MAIN, helper=False)
        self.assertFalse(moved)
        self.assertIn("winmove", warning)
        run.assert_not_called()

    def test_an_empty_display_name_turns_the_move_off(self) -> None:
        (moved, warning), run = self._move(self.ON_MAIN, config={"game_display_name": ""})
        self.assertEqual((moved, warning), (False, ""))
        run.assert_not_called()


class LaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        # 窗口恢复那一步单独在 WindowRestoreTests 里测；这里只看 `open -a`。
        self.restore = patch.object(maa_playcover, "ensure_window_restore", return_value="").start()
        self.pids = patch.object(maa_playcover, "game_pids", return_value=[]).start()
        self.addCleanup(patch.stopall)

    def test_launch_opens_the_container_app_by_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            app = Path(raw) / "com.hypergryph.arknights.app"
            app.mkdir()
            with patch.object(maa_playcover.subprocess, "run", return_value=_completed(0)) as run:
                maa_playcover.launch_game({"playcover_app_path": str(app)})
        self.assertEqual(run.call_args.args[0], ["open", "-a", str(app)])
        self.restore.assert_called_once()

    def test_launch_retries_then_gives_up_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            app = Path(raw) / "a.app"
            app.mkdir()
            failed = SimpleNamespace(returncode=1, stdout="", stderr="LSOpen -10670")
            with patch.object(maa_playcover.subprocess, "run", return_value=failed) as run, patch.object(
                maa_playcover.time, "sleep"
            ):
                with self.assertRaisesRegex(RuntimeError, "-10670"):
                    maa_playcover.launch_game({"playcover_app_path": str(app)})
        self.assertEqual(run.call_count, 3)

    def test_activating_a_running_game_leaves_its_settings_alone(self) -> None:
        # settle_game_window() 也用 launch_game() 激活游戏；那时游戏在跑，别去动它的偏好设置。
        self.pids.return_value = [4242]
        with tempfile.TemporaryDirectory() as raw:
            app = Path(raw) / "a.app"
            app.mkdir()
            with patch.object(maa_playcover.subprocess, "run", return_value=_completed(0)):
                maa_playcover.launch_game({"playcover_app_path": str(app)})
        self.restore.assert_not_called()

    def test_a_failed_restore_or_probe_never_blocks_the_launch(self) -> None:
        self.restore.return_value = "没能打开游戏的窗口恢复（x）"
        with tempfile.TemporaryDirectory() as raw:
            app = Path(raw) / "a.app"
            app.mkdir()
            for pids in (None, subprocess.TimeoutExpired("pgrep", 2)):
                if pids is not None:
                    self.pids.side_effect = pids
                with patch.object(maa_playcover.subprocess, "run", return_value=_completed(0)) as run, patch(
                    "sys.stderr", new_callable=io.StringIO
                ) as err:
                    maa_playcover.launch_game({"playcover_app_path": str(app)})
                self.assertEqual(run.call_args.args[0], ["open", "-a", str(app)])
                self.assertTrue(err.getvalue().strip())


class WindowRestoreTests(unittest.TestCase):
    """2026-11-05：不开窗口恢复，游戏每次都开在主屏正中，得再从台前挪去 MAA-Game。"""

    DOMAIN = str(
        Path.home() / "Library/Containers/com.hypergryph.arknights/Data/Library/Preferences/com.hypergryph.arknights"
    )

    def _ensure(self, *results):
        with patch.object(maa_playcover.subprocess, "run", side_effect=list(results)) as run:
            warning = maa_playcover.ensure_window_restore({})
        return warning, [call.args[0] for call in run.call_args_list]

    def test_an_already_enabled_setting_is_only_read(self) -> None:
        warning, argvs = self._ensure(_completed(0, "1\n"))
        self.assertEqual(warning, "")
        self.assertEqual(argvs, [["defaults", "read", self.DOMAIN, "NSQuitAlwaysKeepsWindows"]])

    def test_a_missing_or_off_setting_is_turned_on(self) -> None:
        for current in (SimpleNamespace(returncode=1, stdout="", stderr="does not exist"), _completed(0, "0\n")):
            warning, argvs = self._ensure(current, _completed(0))
            self.assertEqual(warning, "")
            self.assertEqual(
                argvs[-1], ["defaults", "write", self.DOMAIN, "NSQuitAlwaysKeepsWindows", "-bool", "true"]
            )

    def test_failures_are_warnings_not_exceptions(self) -> None:
        denied = SimpleNamespace(returncode=1, stdout="", stderr="Could not write domain")
        warning, _argvs = self._ensure(_completed(0, "0\n"), denied)
        self.assertIn("Could not write domain", warning)
        warning, _argvs = self._ensure(subprocess.TimeoutExpired("defaults", 10))
        self.assertIn("TimeoutExpired", warning)

    def test_the_domain_follows_the_configured_bundle_id(self) -> None:
        domain = maa_playcover.game_prefs_domain({"game_bundle_id": "com.example.game"})
        self.assertEqual(domain.name, "com.example.game")
        self.assertEqual(domain.parent.parent.parent.parent.name, "com.example.game")


class CloseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeMaaTools()
        self.addCleanup(self.fake.close)

    def _close(self, pgrep_results: list[SimpleNamespace], **config):
        calls: list[list[str]] = []
        results = list(pgrep_results)

        def fake_run(argv, **_kwargs):
            calls.append(list(argv))
            if argv[0] == "pgrep":
                return results.pop(0) if results else _completed(1)
            return _completed(0)

        clock = {"now": 0.0}
        with patch.object(maa_playcover.subprocess, "run", side_effect=fake_run), patch.object(
            maa_playcover.time, "monotonic", side_effect=lambda: clock["now"]
        ), patch.object(
            maa_playcover.time, "sleep", side_effect=lambda s: clock.__setitem__("now", clock["now"] + s)
        ):
            errors = maa_playcover.close_game(cast("maa_common.Config", {**self.fake.config, **config}))
        return errors, calls

    def test_a_closed_game_needs_nothing(self) -> None:
        errors, calls = self._close([_completed(1)])
        self.assertEqual(errors, [])
        self.assertEqual([argv[0] for argv in calls], ["pgrep"])
        self.assertFalse(self.fake.commands)

    def test_term_is_sent_and_the_exit_is_verified(self) -> None:
        errors, calls = self._close([_completed(0, "17210\n"), _completed(0, "17210\n"), _completed(1)])
        self.assertEqual(errors, [])
        self.assertIn((b"TERM", b""), self.fake.drained())
        self.assertNotIn("pkill", [argv[0] for argv in calls])

    def test_a_game_that_ignores_term_is_pkilled_then_reported(self) -> None:
        alive = _completed(0, "17210\n")
        errors, calls = self._close([alive] * 500, game_quit_timeout=2, cleanup_verify_timeout=2)
        self.assertIn("pkill", [argv[0] for argv in calls])
        self.assertIn("仍有明日方舟进程存活", errors)
        pkill = next(argv for argv in calls if argv[0] == "pkill")
        # 只按游戏可执行文件的完整路径杀，PlayCover 本体不动。
        self.assertEqual(pkill[:2], ["pkill", "-f"])
        self.assertIn("arknights", pkill[2])


class CopilotScreenTests(unittest.TestCase):
    """抄作业 worker 的两处「自己看屏幕、自己点」：接管弹窗看门狗与区域入口。

    PlayCover 下截图可能来自按窗口截图（带标题栏、Retina 2×），点击用 1920×1080
    游戏坐标——这里钉住两者之间的换算。
    """

    WINDOW = {"source": "window", "width": 3840, "height": 2216, "top": 56}

    def test_ocr_boxes_are_rewritten_into_game_coordinates(self) -> None:
        import maa_copilot_worker as worker

        lines = ["通学路\t[1562,1036,686,112]", "没有坐标的行"]
        self.assertEqual(
            worker._base_box_lines(lines, self.WINDOW),
            ["通学路\t[781,490,343,56]", "没有坐标的行"],
        )
        # 跨进标题栏的框被夹到 0，`zone_button` 的正则不认负数。
        self.assertEqual(
            worker._base_box_lines(["x\t[0,40,20,40]"], self.WINDOW), ["x\t[0,0,10,12]"]
        )

    def test_the_watchdog_taps_the_calibrated_confirm_button(self) -> None:
        import maa_copilot_worker as worker

        class Proc:
            def __init__(self) -> None:
                self.polls = 0
                self.returncode = 0

            def poll(self):
                self.polls += 1
                return None if self.polls <= 1 else 0

        popup = "接管后无法回到代理指挥\t[1500,900,900,80]\n确认要接管作战吗？\t[1562,1036,686,112]\n"
        taps: list[tuple[float, float]] = []
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "ocrbox").write_bytes(b"")
            log_path = root / "copilot.log"
            with patch.object(worker, "SCRIPTS_DIR", root), patch.object(
                worker, "RUNS_DIR", root
            ), patch.object(worker.subprocess, "Popen", return_value=Proc()), patch.object(
                worker.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=popup, stderr="")
            ), patch.object(
                maa_playcover, "capture_geometry", return_value=self.WINDOW
            ), patch.object(
                maa_playcover, "tap", side_effect=lambda _c, x, y: taps.append((x, y))
            ), patch.object(worker.time, "sleep"):
                code, tapped = worker._run_copilot_with_watchdog(
                    {}, ["maa", "copilot"], None, log_path, {}, timeout=60
                )
            logged = log_path.read_text(encoding="utf-8")
        self.assertEqual((code, tapped), (0, 1))
        # 2026-08-02 标定：文字框 [781,490,343,56] → 确认键 (781+343+130, 490+56+171)。
        self.assertEqual(taps, [(1254, 717)])
        self.assertIn("tap 1254,717", logged)


if __name__ == "__main__":
    unittest.main()
