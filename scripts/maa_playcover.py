#!/usr/bin/env python3
"""PlayCover 设备层：替代模拟器 + ADB 的那一层，全部 stdlib。

游戏是 PlayCover 原生运行的 iOS 客户端，MAA 经 PlayCover fork 里的 **MaaTools**
（游戏进程内的 TCP 服务，默认 `127.0.0.1:1717`）截图与点击。本模块只管设备生命周期，
MaaCore 自己的连接由 maa-cli profile 的 `[connection] preset = "PlayCover"` 负责：

| 模拟器时代 | PlayCover |
|---|---|
| `open -a <模拟器>` | `open -a <PlayCover 容器里的 .app>`：直接拉起游戏进程 |
| `adb connect` + `sys.boot_completed` | MaaTools 握手 `MAA\\0` → `OKAY`、`VERN` ≥ 4，`SIZE` 非零 |
| `pm path <包名>` | PlayCover 容器里的 `.app` 在、它的 App Settings 开了 MaaTools 且端口对得上 |
| `adb exec-out screencap` | MaaTools `SCRN`（游戏在前台时）→ 退回 `screencapture -l <窗口>` |
| `adb shell input tap` | MaaTools `TUCH`（Began + Ended） |
| `maa closedown` + 退模拟器 + `pkill` | MaaTools `TERM` → 等进程退出 → `pkill` 兜底 → 复核 |

**MaaTools 的截图（`SCRN`）要求游戏窗口真的在屏上**：不必是前台 app，但不能被最小化、
隐藏，也不能被台前调度收进侧边栏——那几种情况它回 0 字节（2026-11-04 实测：
本机开着台前调度，别的 app 一激活就把游戏挤下台）。所以游戏窗口常驻在一块**只放它**的
BetterDisplay 虚拟屏上（台前调度按屏各管各的）。PlayCover 每次拉起游戏都把窗口开在
当前活动的那块屏上、**不会**自己回到虚拟屏，所以 `settle_game_window()` 发现它不在
`game_display_name` 那块屏上就用 `winmove`（辅助功能接口，要权限）把它挪过去。
本模块自己的截图「`SCRN` 优先、拿不到再退回 `screencapture -l <窗口>`」，后者要求
发起进程有「录屏与系统录音」权限（线上就是 Hermes gateway 那个 python）。
MaaCore 的 `MacSCK`（ScreenCaptureKit）后台截图**不用**：maa-cli 是命令行进程，
SCK 在里面直接 `CGS_REQUIRE_INIT` 断言崩溃，而且被收进侧边栏的窗口它截到的也是缩略图。

协议（MaaCore `PlayToolsController.cpp`）：每条请求 = 2 字节大端长度 + 4 字节命令
（+ 负载），`SIZE` 回 2×u16，`SCRN` 回 u32 长度 + RGBA，`TUCH` 负载 = 1 字节阶段 +
2×u16 坐标（坐标系就是 `SIZE`），`TERM` 无回包。
"""

from __future__ import annotations

import json
import math
import plistlib
import re
import socket
import struct
import subprocess
import time
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from maa_common import Config

DEFAULT_BUNDLE_ID = "com.hypergryph.arknights"
DEFAULT_APP_PATH = (
    "~/Library/Containers/io.playcover.PlayCover/Applications/com.hypergryph.arknights.app"
)
DEFAULT_SETTINGS_DIR = "~/Library/Containers/io.playcover.PlayCover/App Settings"
DEFAULT_ADDRESS = "127.0.0.1:1717"
SAFE_BUNDLE_ID_RE = re.compile(r"^[A-Za-z0-9.-]{1,128}$")
# 只接受本机地址：MaaTools 是游戏进程里的明文 TCP 服务，不该被指到别的机器上。
ADDRESS_RE = re.compile(r"^(127\.0\.0\.1|localhost):(\d{2,5})$")
SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9 ._+/~-]{1,300}$")
# 游戏窗口常驻的那块 BetterDisplay 虚拟屏（`NSScreen.localizedName`）。空串 = 不管窗口在哪块屏。
DEFAULT_GAME_DISPLAY = "MAA-Game"
SAFE_DISPLAY_RE = re.compile(r"^[A-Za-z0-9 ._-]{0,64}$")
# deploy.py 把 scripts/winmove.swift 编译到和本模块同一个目录。
WINMOVE = Path(__file__).resolve().with_name("winmove")
# 本模块对外的坐标系：1920×1080 的游戏画面（不含标题栏）。截图、OCR 框、点击都先换到它。
BASE_WIDTH, BASE_HEIGHT = 1920, 1080
# 落盘截图的最大宽度。MaaTools 回的是渲染分辨率（Retina 下 3840×2160、33 MB RGBA），
# 原样编 PNG 有 11 MB，超过 Discord 附件上限。
MAX_SHOT_WIDTH = 1920
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
TOUCH_BEGAN, TOUCH_ENDED = 0, 3
# 握手后 `VERN` 回的 MaaTools 协议版本下限。上游 MaaMacGui 对 SIZE/SCRN/TUCH/TERM 要求 ≥ 2、
# RECT/BNDL/BGR 要求 ≥ 3；这里只认真机用过的 4（PlayCover MAA 分支 4.0.0.maa）。
MIN_MAATOOLS_VERSION = 4


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def bundle_id(config: Config) -> str:
    value = str(config.get("game_bundle_id", DEFAULT_BUNDLE_ID)).strip()
    if not SAFE_BUNDLE_ID_RE.fullmatch(value):
        raise RuntimeError("game_bundle_id 格式非法")
    return value


def app_path(config: Config) -> Path:
    raw = str(config.get("playcover_app_path", DEFAULT_APP_PATH)).strip()
    if not SAFE_PATH_RE.fullmatch(raw):
        raise RuntimeError("playcover_app_path 含非法字符")
    return Path(raw).expanduser()


def settings_path(config: Config) -> Path:
    raw = str(config.get("playcover_settings_dir", DEFAULT_SETTINGS_DIR)).strip()
    if not SAFE_PATH_RE.fullmatch(raw):
        raise RuntimeError("playcover_settings_dir 含非法字符")
    return Path(raw).expanduser() / f"{bundle_id(config)}.plist"


def maatools_address(config: Config) -> tuple[str, int]:
    raw = str(config.get("maatools_address", DEFAULT_ADDRESS)).strip()
    match = ADDRESS_RE.fullmatch(raw)
    if not match:
        raise RuntimeError("maatools_address 只接受 127.0.0.1:<端口> 或 localhost:<端口>")
    port = int(match.group(2))
    if not 0 < port < 65536:
        raise RuntimeError("maatools_address 端口越界")
    return ("127.0.0.1" if match.group(1) == "localhost" else match.group(1)), port


def executable_path(config: Config) -> Path:
    app = app_path(config)
    name = "Arknights"
    try:
        with (app / "Info.plist").open("rb") as handle:
            name = str(plistlib.load(handle).get("CFBundleExecutable") or name)
    except (OSError, plistlib.InvalidFileException, ValueError):
        pass
    return app / name


def process_pattern(config: Config) -> str:
    """`pgrep -f` / `pkill -f` 用的正则：游戏可执行文件的**完整路径**。

    首字符套一层方括号，正则照样命中目标，却命中不了 pgrep 自己的命令行。
    用完整路径而不是「Arknights」：Claude 会话、编辑器、日志查看器的命令行里都可能
    出现游戏名，模拟器时代就因为 `-fi` 把带 `landship-derived` 的会话当成残留判红过。
    """
    text = str(executable_path(config))
    return f"[{re.escape(text[0])}]{re.escape(text[1:])}"


def game_pids(config: Config, timeout: float = 2.0) -> list[int]:
    found = subprocess.run(
        ["pgrep", "-f", process_pattern(config)],
        capture_output=True,
        text=True,
        check=False,
        timeout=max(0.1, timeout),
    )
    if found.returncode != 0:
        return []
    return [int(line) for line in found.stdout.split() if line.strip().isdigit()]


# ---------------------------------------------------------------------------
# MaaTools 客户端
# ---------------------------------------------------------------------------

class MaaToolsError(RuntimeError):
    pass


class MaaTools:
    """一条 MaaTools 连接。`with MaaTools(config) as tools: tools.size()`。"""

    def __init__(self, config: Config, timeout: float = 5.0) -> None:
        self.address = maatools_address(config)
        self.timeout = timeout
        self.sock: socket.socket | None = None
        # 握手时 `VERN` 读回的协议版本；`__enter__` 成功后才有值。
        self.version: int | None = None

    def __enter__(self) -> "MaaTools":
        try:
            self.sock = socket.create_connection(self.address, timeout=self.timeout)
            self.sock.sendall(b"MAA\x00")
            reply = self._recv(4)
        except OSError as exc:
            self.close()
            raise MaaToolsError(f"连不上 MaaTools {self.address[0]}:{self.address[1]}（{exc}）") from exc
        if reply != b"OKAY":
            self.close()
            raise MaaToolsError(f"MaaTools 握手回包异常：{reply!r}")
        try:
            self._send(b"VERN")
            (version,) = struct.unpack(">I", self._recv(4))
        except MaaToolsError as exc:
            self.close()
            raise MaaToolsError(
                f"MaaTools 握手后读 VERN 时对端关闭（{exc}）：PlayCover 的 MaaTools 可能太旧、"
                "不认 VERN，升级 PlayCover MAA 分支"
            ) from exc
        except OSError as exc:
            self.close()
            raise MaaToolsError(
                f"MaaTools 握手后读不到 VERN（{exc}）：检查 PlayCover 的 MaaTools 是否正常，"
                "必要时重开游戏或升级 PlayCover MAA 分支"
            ) from exc
        if version < MIN_MAATOOLS_VERSION:
            self.close()
            raise MaaToolsError(
                f"MaaTools 协议版本 v{version} 低于要求的 v{MIN_MAATOOLS_VERSION}："
                "升级 PlayCover MAA 分支（本仓库验证过的是 4.0.0.maa）"
            )
        self.version = version
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _recv(self, size: int) -> bytes:
        assert self.sock is not None
        buffer = bytearray()
        while len(buffer) < size:
            chunk = self.sock.recv(min(1 << 22, size - len(buffer)))
            if not chunk:
                raise MaaToolsError("MaaTools 连接被对端关闭")
            buffer += chunk
        return bytes(buffer)

    def _send(self, command: bytes, payload: bytes = b"") -> None:
        assert self.sock is not None and len(command) == 4
        self.sock.sendall(struct.pack(">H", 4 + len(payload)) + command + payload)

    def size(self) -> tuple[int, int]:
        self._send(b"SIZE")
        width, height = struct.unpack(">HH", self._recv(4))
        return width, height

    def screencap_rgba(self) -> tuple[int, int, bytes]:
        """`(宽, 高, RGBA)`；游戏不在前台时 MaaTools 回 0 字节，这里返回空 bytes。"""
        width, height = self.size()
        self._send(b"SCRN")
        (length,) = struct.unpack(">I", self._recv(4))
        if not length:
            return width, height, b""
        data = self._recv(length)
        if not width or not height or length != width * height * 4:
            raise MaaToolsError(f"MaaTools 截图尺寸不符：{width}x{height} / {length} 字节")
        return width, height, data

    def touch(self, phase: int, x: int, y: int) -> None:
        self._send(b"TUCH", struct.pack(">BHH", phase, max(0, x), max(0, y)))

    def terminate(self) -> None:
        self._send(b"TERM")


def maatools_ready(config: Config, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        with MaaTools(config, timeout=timeout) as tools:
            width, height = tools.size()
            version = tools.version
    except (MaaToolsError, OSError, struct.error) as exc:
        return False, str(exc)
    if not width or not height:
        return False, "MaaTools 已连上，但游戏窗口尺寸还是 0"
    return True, f"MaaTools v{version} 就绪（{width}x{height}）"


# ---------------------------------------------------------------------------
# 「你在玩吗」探测（四期 D18 的 PlayCover 版）
# ---------------------------------------------------------------------------

def game_is_online(config: Config) -> tuple[bool, str]:
    """游戏是不是已经开着——也就是「用户此刻在玩」？

    在**每一条自动链路**抢到 `run.lock` 之后、`launch_game()` 之前调用（日常 am/pm、
    抄作业 `run-batch`、Depot、肉鸽）：任何失败路径都会 `close_game()`，开着就跑
    等于把正在玩的人踢下线。MaaTools 跑在游戏进程里，进程不在端口必然不在，
    所以只看进程一个信号。

    **永不抛异常。** 探测自己坏了一律报离线、照常执行：误抢一次远比「探测坏了导致
    日常永久停摆」便宜（B-4）。预算 `game_probe_timeout`（默认 5s）。
    """
    try:
        budget = max(0.0, float(config.get("game_probe_timeout", 5)))
    except (TypeError, ValueError):
        budget = 5.0
    if not budget:
        return False, "探测已禁用（game_probe_timeout = 0）"
    try:
        pids = game_pids(config, timeout=min(budget, 3.0))
    except Exception as exc:  # noqa: BLE001 — 探测永不抛
        return False, f"进程探测失败（{type(exc).__name__}），按离线处理"
    if pids:
        return True, f"明日方舟（PlayCover）进程在跑（pid {', '.join(map(str, pids))}）"
    return False, "明日方舟进程不在"


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------

def verify_game_installed(config: Config) -> None:
    """PlayCover 里装着游戏、并且 MaaTools 开着且端口和配置一致。启动前调用。"""
    executable = executable_path(config)
    if not executable.is_file():
        raise RuntimeError(f"PlayCover 里没有明日方舟：{executable}")
    path = settings_path(config)
    try:
        with path.open("rb") as handle:
            settings = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise RuntimeError(f"读不到 PlayCover 的游戏设置 {path}：{type(exc).__name__}") from exc
    if not settings.get("maaTools"):
        raise RuntimeError("PlayCover 里明日方舟没有启用 MaaTools（绕过设置 → MaaTools）")
    _, port = maatools_address(config)
    configured = settings.get("maaToolsPort")
    if configured is not None and int(configured) != port:
        raise RuntimeError(
            f"PlayCover 的 MaaTools 端口是 {configured}，config 里写的是 {port}"
        )


def launch_game(config: Config) -> None:
    """`open -a <容器里的 .app>`：没在跑就拉起，在跑就激活（把窗口请回台前）。

    不用 `open -b`：bundle id 会被 LaunchServices 解析到 `~/Applications/PlayCover/`
    下那个软链接壳，游戏已在跑时报 -10670、什么也不做（2026-11-04 实测）。
    上一个进程还在退出时也会报错，所以稍等重试。
    """
    app = app_path(config)
    if not app.is_dir():
        raise RuntimeError(f"PlayCover 里没有明日方舟：{app}")
    last = ""
    for attempt in range(3):
        result = subprocess.run(
            ["open", "-a", str(app)], capture_output=True, text=True, check=False, timeout=30
        )
        if result.returncode == 0:
            return
        last = (result.stderr or result.stdout).strip()
        time.sleep(3 + 2 * attempt)
    raise RuntimeError(f"无法启动明日方舟：{last[-200:]}")


def wait_for_game(config: Config) -> None:
    timeout = max(5, int(config.get("game_launch_timeout", 120)))
    deadline = time.monotonic() + timeout
    detail = "未就绪"
    while time.monotonic() < deadline:
        ready, detail = maatools_ready(config, timeout=3)
        if ready:
            return
        time.sleep(2)
    raise RuntimeError(f"等 MaaTools {timeout}s 超时：{detail}")


def _jxa_windows(pids: list[int]) -> list[dict[str, Any]]:
    """用 JXA 调 `CGWindowListCopyWindowInfo` 列出这些进程的普通窗口。

    读窗口编号 / 位置 / 是否在屏不需要任何权限（窗口标题才要录屏权限，这里不读）。
    """
    script = (
        'ObjC.import("CoreGraphics");'
        "function run(argv) {"
        " const pids = new Set(JSON.parse(argv[0] || '[]'));"
        " const all = ObjC.deepUnwrap(ObjC.castRefToObject("
        "$.CGWindowListCopyWindowInfo($.kCGWindowListOptionAll, 0))) || [];"
        " return JSON.stringify(all.filter(w => pids.has(w.kCGWindowOwnerPID)"
        " && w.kCGWindowLayer === 0)"
        ".map(w => ({id: w.kCGWindowNumber, pid: w.kCGWindowOwnerPID,"
        " onscreen: !!w.kCGWindowIsOnscreen,"
        " x: w.kCGWindowBounds.X, y: w.kCGWindowBounds.Y,"
        " width: w.kCGWindowBounds.Width, height: w.kCGWindowBounds.Height})));"
        "}"
    )
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", script, json.dumps(pids)],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"列窗口失败：{result.stderr.strip()[:160]}")
    windows = json.loads(result.stdout or "[]")
    return windows if isinstance(windows, list) else []


def game_window(config: Config) -> dict[str, Any] | None:
    """游戏的主窗口（面积最大的那个普通窗口）；没有就 None。"""
    pids = game_pids(config)
    if not pids:
        return None
    windows = [w for w in _jxa_windows(pids) if w.get("width", 0) >= 300]
    if not windows:
        return None
    return max(windows, key=lambda w: (bool(w.get("onscreen")), w["width"] * w["height"]))


def game_display_name(config: Config) -> str:
    value = str(config.get("game_display_name", DEFAULT_GAME_DISPLAY)).strip()
    if not SAFE_DISPLAY_RE.fullmatch(value):
        raise RuntimeError("game_display_name 含非法字符")
    return value


def _screens() -> list[dict[str, Any]]:
    """所有屏的名字与范围，换成和 CGWindow 一样的「主屏左上角为原点、y 向下」坐标。"""
    script = (
        'ObjC.import("AppKit");'
        "const s = $.NSScreen.screens.js; const top = s[0].frame.size.height;"
        "JSON.stringify(s.map(x => ({name: x.localizedName.js, x: x.frame.origin.x,"
        " y: top - x.frame.origin.y - x.frame.size.height,"
        " width: x.frame.size.width, height: x.frame.size.height})));"
    )
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"列屏幕失败：{result.stderr.strip()[:160]}")
    screens = json.loads(result.stdout or "[]")
    return screens if isinstance(screens, list) else []


def game_display(config: Config) -> dict[str, Any] | None:
    name = game_display_name(config)
    if not name:
        return None
    return next((s for s in _screens() if s.get("name") == name), None)


def _on_display(window: dict[str, Any], display: dict[str, Any]) -> bool:
    cx = window["x"] + window["width"] / 2
    cy = window["y"] + window["height"] / 2
    return (
        display["x"] <= cx < display["x"] + display["width"]
        and display["y"] <= cy < display["y"] + display["height"]
    )


def _centred_on(window: dict[str, Any], display: dict[str, Any]) -> tuple[int, int]:
    """窗口在这块屏上居中时的左上角（比屏还大就贴左上角）。"""
    x = display["x"] + max(0, (display["width"] - window["width"]) / 2)
    y = display["y"] + max(0, (display["height"] - window["height"]) / 2)
    return int(x), int(y)


def _fits_display(window: dict[str, Any], display: dict[str, Any]) -> bool:
    """窗口整个落在这块屏里。比屏还大的放不下，已在居中位置也算数，免得每次都挪。"""
    inside = (
        display["x"] <= window["x"]
        and display["y"] <= window["y"]
        and window["x"] + window["width"] <= display["x"] + display["width"]
        and window["y"] + window["height"] <= display["y"] + display["height"]
    )
    return inside or (int(window["x"]), int(window["y"])) == _centred_on(window, display)


def move_to_game_display(config: Config, window: dict[str, Any]) -> tuple[bool, str]:
    """游戏窗口没整个落在虚拟屏上就挪过去（居中）。返回 (挪了没有, 告警)。

    只看中心不够：PlayCover 有时直接开在虚拟屏上，却偏到右下、一截伸出屏外
    （2026-11-04 实测 (2630, 290)，右边多出 268px）——不影响截图，远程看屏时会被裁掉。
    """
    name = game_display_name(config)
    if not name:
        return False, ""
    display = game_display(config)
    if display is None:
        return False, f"找不到虚拟屏 {name}（BetterDisplay 没开？），游戏窗口留在原处。"
    if _fits_display(window, display):
        return False, ""
    if not WINMOVE.is_file():
        return False, f"游戏窗口不在 {name} 上，而 winmove 没部署，挪不过去。"
    x, y = _centred_on(window, display)
    result = subprocess.run(
        [str(WINMOVE), str(int(window["pid"])), str(int(x)), str(int(y))],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if result.returncode == 3:
        return False, (
            f"游戏窗口不在 {name} 上，挪不过去：Hermes 的 Python 没有「辅助功能」权限。"
            "窗口留在原处，别的窗口一盖住它截图就会失败。"
        )
    if result.returncode != 0:
        return False, f"把游戏窗口挪到 {name} 失败：{(result.stdout or result.stderr).strip()[:120]}"
    return True, ""


def settle_game_window(config: Config) -> str:
    """确认游戏窗口在虚拟屏上、在屏上（没被最小化 / 隐藏 / 被台前调度收进侧边栏）。

    返回告警文本，空串 = 一切正常。窗口不在屏上时 MaaTools 的 `SCRN` 回 0 字节，
    MaaCore 只会刷 `ScreencapFailed`。

    1. 窗口不在 `game_display_name` 那块虚拟屏上（PlayCover 每次都开在当前活动的屏上）
       → `move_to_game_display()` 挪过去，主屏上你干什么都不会再把它挤下台；
    2. 还是不在屏上 → 激活一次游戏：台前调度会把它请回台前，最小化的窗口也会还原——
       代价是抢一下焦点，只在真的掉出屏幕时才做。

    查不出来**不阻断**，交给 MaaCore 自己的截图报错，比在这儿猜更准。
    """
    warnings: list[str] = []
    try:
        window = game_window(config)
        if window:
            moved, warning = move_to_game_display(config, window)
            if warning:
                warnings.append(warning)
            if moved:
                time.sleep(1)
                window = game_window(config) or window
        if window and window.get("onscreen"):
            return " ".join(warnings)
        launch_game(config)
        time.sleep(3)
        window = game_window(config)
        if window and window.get("onscreen"):
            return " ".join(warnings)
        warnings.append("游戏窗口不在屏上（可能被最小化或隐藏了），后台截图可能失败。")
        return " ".join(warnings)
    except Exception as exc:  # noqa: BLE001 — 只是体检，不阻断
        warnings.append(f"没能确认游戏窗口状态（{type(exc).__name__}），照常开跑。")
        return " ".join(warnings)


def _rgba_png(width: int, height: int, data: bytes, step: int) -> bytes:
    pixels = memoryview(data).cast("I")
    out_width = len(range(0, width, step))
    rows = bytearray()
    for y in range(0, height, step):
        rows += b"\x00"
        rows += pixels[y * width:(y + 1) * width:step].tobytes()
    out_height = len(range(0, height, step))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", out_width, out_height, 8, 6, 0, 0, 0)
    return PNG_MAGIC + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b"")


def _png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        head = handle.read(24)
    if head[:8] != PNG_MAGIC:
        raise RuntimeError("截图不是 PNG")
    return struct.unpack(">II", head[16:24])


def capture_geometry(config: Config, path: Path) -> dict[str, Any]:
    """截一张游戏画面到 `path`，返回把图上坐标换回 1920×1080 游戏坐标所需的几何信息。

    `{"source", "width", "height", "top"}`：`top` 是图里游戏画面上方的标题栏高度，
    游戏画面占 `width × (height - top)`。失败抛 RuntimeError。
    """
    try:
        with MaaTools(config, timeout=10) as tools:
            width, height, data = tools.screencap_rgba()
    except (MaaToolsError, OSError, struct.error) as exc:
        raise RuntimeError(f"MaaTools 截图失败：{exc}") from exc
    if data:
        step = max(1, math.ceil(width / MAX_SHOT_WIDTH))
        path.write_bytes(_rgba_png(width, height, data, step))
        out_width, out_height = _png_size(path)
        return {"source": "maatools", "width": out_width, "height": out_height, "top": 0}
    # 游戏不在前台：MaaTools 给不了画面，按窗口编号让系统截（要录屏权限）。
    window = game_window(config)
    if not window:
        raise RuntimeError("游戏在后台且找不到它的窗口")
    if not window.get("onscreen"):
        raise RuntimeError("游戏窗口不在屏上（最小化 / 隐藏），截不到")
    result = subprocess.run(
        ["screencapture", "-x", "-o", f"-l{int(window['id'])}", str(path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0 or not path.is_file() or path.stat().st_size < 8:
        raise RuntimeError(
            "screencapture 失败（多半是缺「录屏与系统录音」权限）："
            + (result.stderr or result.stdout).strip()[:160]
        )
    out_width, out_height = _png_size(path)
    content_height = round(out_width * BASE_HEIGHT / BASE_WIDTH)
    top = max(0, out_height - content_height)
    return {"source": "window", "width": out_width, "height": out_height, "top": top}


def capture_screenshot(config: Config, path: Path) -> tuple[bool, str]:
    try:
        capture_geometry(config, path)
        path.chmod(0o600)
        return True, ""
    except Exception as exc:  # noqa: BLE001 — 截图失败只进战报，不带走整场
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return False, f"{type(exc).__name__}: {exc}"


def image_to_base(geometry: dict[str, Any], x: float, y: float) -> tuple[float, float]:
    """截图像素坐标 → 1920×1080 游戏坐标（扣掉标题栏）。"""
    scale = BASE_WIDTH / max(1, int(geometry["width"]))
    return x * scale, (y - int(geometry.get("top", 0))) * scale


def tap(config: Config, x: float, y: float) -> None:
    """在 1920×1080 游戏坐标 `(x, y)` 点一下。坐标按 MaaTools 的 `SIZE` 换算。"""
    if not (0 <= x <= BASE_WIDTH and 0 <= y <= BASE_HEIGHT):
        raise RuntimeError(f"点击坐标越界：({x:.0f}, {y:.0f})")
    with MaaTools(config, timeout=5) as tools:
        width, height = tools.size()
        if not width or not height:
            raise MaaToolsError("游戏窗口尺寸是 0")
        px = min(width - 1, round(x * width / BASE_WIDTH))
        py = min(height - 1, round(y * height / BASE_HEIGHT))
        tools.touch(TOUCH_BEGAN, px, py)
        time.sleep(0.05)
        tools.touch(TOUCH_ENDED, px, py)
        time.sleep(0.05)


def close_game(config: Config) -> list[str]:
    """结束游戏进程并复核。返回清理错误列表（空 = 干净）。

    先 MaaTools `TERM`（MaaCore 的 `stop_game` 就是这条，相当于游戏自己退出），
    等进程消失；超时再 `pkill -f` 兜底，最后复核一遍。PlayCover 本体不动。
    """
    errors: list[str] = []
    try:
        pattern = process_pattern(config)
    except Exception as exc:  # noqa: BLE001
        return [f"游戏进程模式非法，拒绝清理：{exc}"]
    try:
        if not game_pids(config):
            return []
    except Exception as exc:  # noqa: BLE001
        errors.append(f"进程探测异常：{type(exc).__name__}")
    term_note = ""
    try:
        with MaaTools(config, timeout=5) as tools:
            tools.terminate()
    except Exception as exc:  # noqa: BLE001 — 连不上就直接走 pkill
        term_note = f"MaaTools TERM 未送达（{type(exc).__name__}）"

    def wait_gone(seconds: float) -> bool:
        deadline = time.monotonic() + max(1.0, seconds)
        while time.monotonic() < deadline:
            try:
                if not game_pids(config):
                    return True
            except Exception:  # noqa: BLE001
                return False
            time.sleep(0.5)
        return False

    if wait_gone(float(config.get("game_quit_timeout", 30))):
        return errors
    try:
        subprocess.run(["pkill", "-f", pattern], capture_output=True, check=False, timeout=10)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"游戏兜底退出异常：{type(exc).__name__}")
    if not wait_gone(float(config.get("cleanup_verify_timeout", 10))):
        if term_note:
            errors.append(term_note)
        errors.append("仍有明日方舟进程存活")
    return errors
