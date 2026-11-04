"""单测隔离：整套单测跑在一个临时 HOME 里，碰真实 home 下的线上目录、碰外网都当场炸。

**必须在 import 任何 `scripts/` 模块之前 import 本模块**：`maa_common` / `deploy` /
`maa_copilot` 在 import 时就按 `Path.home()` / `HERMES_HOME` 算好 `STATE_DIR`、备份目录、
MaaResource 路径这些常量，晚一步就已经指向线上了。

为什么要这一层（2026-11-04 用干净 HOME 复核时查实）：

- 开发机上「全绿」靠的是真实 `~/.hermes`：deploy 用例借 Hermes venv 的 PyYAML，
  手动开跑 / 早晚场用例真去读 PlayCover 里明日方舟的安装和 MaaTools 设置，
  模组用例读本机 MaaResource 的 `battle_data.json`。换一台机器就红 24 条。
- 用例往线上写：`deploy.backup_root` 在 import 时算死，每跑一次就往
  `~/.hermes/backups/maa-setup/` 留一个目录（当时 361 个里 271 个是单测留的）；
  计划卡用例走到 `_skland_health_notice()`，北京时间 07:50 之前跑会拿真 token 打森空岛、
  改写 `skland.json`，并把当天记成「已体检」，真计划卡那天就不再提醒过期。

所以这里做三件事：

1. 把 `HOME` / `HERMES_HOME` / `MAA_CONFIG_DIR` 换成进程级临时目录，清掉会改变部署行为的
   环境变量；
2. 装一个审计钩子（`sys.addaudithook`）：打开、列目录、建删改名、chmod 真实 home 下的
   `.hermes` / `.config/maa-cli` / `Library` 一律抛 `RealHomeTouched`；
3. 同一个钩子封外网：解析或连接非回环地址抛 `RealNetworkTouched`（`MAA_LIVE_TESTS=1`
   的线上契约冒烟例外）。

两个异常都继承 `BaseException`：脚本里大量 `except Exception` 吞不掉它们，
用例一定会红。

唯一故意留的口子：本解释器没有 PyYAML 时，`yaml_python()` 借 Hermes venv 的解释器
**只做 YAML 解析**（子进程从 stdin 读文本，不读任何状态文件）。
"""

from __future__ import annotations

import atexit
import functools
import importlib.util
import os
import pwd
import shutil
import site
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
_PROTECTED_NAMES = (".hermes", ".config/maa-cli", "Library")


def _protected_roots() -> tuple[str, ...]:
    homes = {str(REAL_HOME), os.path.realpath(REAL_HOME)}
    # 父进程已经改过 HOME（例如在另一个临时 HOME 里跑）时，那个 HOME 也护上。
    if os.environ.get("HOME"):
        homes.add(os.path.abspath(os.environ["HOME"]))
    return tuple(sorted({os.path.join(home, name) for home in homes for name in _PROTECTED_NAMES}))


PROTECTED_ROOTS = _protected_roots()
# 解释器自己的东西不算：macOS 自带的 Python 3.9 把 `.pyc` 写在
# `~/Library/Caches/com.apple.python/`（`sys.pycache_prefix`），`pip install --user`
# 的包装在 `~/Library/Python/3.9/`（`site.getuserbase()`）。
_INTERPRETER_DIRS = tuple(
    os.path.abspath(path)
    for path in (sys.pycache_prefix, site.getuserbase())
    if path
)
LIVE_TESTS = os.environ.get("MAA_LIVE_TESTS") == "1"


class RealHomeTouched(BaseException):
    """单测碰到了真实 home 下的线上目录。"""


class RealNetworkTouched(BaseException):
    """单测碰到了外网。"""


class RealGameTouched(BaseException):
    """单测碰到了真游戏。"""


# 事件名 → 前几个参数是路径。事件清单见 https://docs.python.org/3/library/audit_events.html
_PATH_EVENTS = {
    "open": 1,
    "os.listdir": 1,
    "os.scandir": 1,
    "glob.glob": 1,
    "os.mkdir": 1,
    "os.chmod": 1,
    "os.chown": 1,
    "os.chflags": 1,
    "os.utime": 1,
    "os.truncate": 1,
    "os.remove": 1,
    "os.rmdir": 1,
    "os.rename": 2,
    "os.replace": 2,
    "os.link": 2,
    "os.symlink": 2,
    "shutil.copyfile": 2,
    "shutil.copytree": 2,
    "shutil.move": 2,
    "shutil.rmtree": 1,
}
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "::", ""}


def is_protected(raw: Any) -> bool:
    """`raw` 是否落在真实 home 的 `.hermes` / `.config/maa-cli` / `Library` 之下。"""
    if isinstance(raw, int):  # 文件描述符
        return False
    try:
        path = os.path.abspath(os.fsdecode(os.fspath(raw)))
    except TypeError:
        return False
    if any(path.startswith(own + os.sep) for own in _INTERPRETER_DIRS):
        return False
    return any(path == root or path.startswith(root + os.sep) for root in PROTECTED_ROOTS)


def _is_loopback(host: Any) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip("[]").lower()
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def _audit(event: str, args: tuple) -> None:
    count = _PATH_EVENTS.get(event)
    if count is not None:
        for raw in args[:count]:
            if is_protected(raw):
                raise RealHomeTouched(
                    f"单测碰到了真实 home：{event} {os.fsdecode(os.fspath(raw))}——"
                    "路径要从 maa_common / deploy 的常量来（已指向临时 HOME），或自行打桩"
                )
        return
    if LIVE_TESTS:
        return
    if event == "socket.getaddrinfo":
        host = args[0] if args else None
    elif event == "socket.connect":
        address = args[1] if len(args) > 1 else None
        if not isinstance(address, tuple) or not address:
            return  # AF_UNIX 等本机通道
        host = address[0]
    else:
        return
    if not _is_loopback(host):
        raise RealNetworkTouched(f"单测碰到了外网：{event} {host!r}——自行打桩（DEV_GUIDE §9）")


if is_protected(tempfile.gettempdir()):
    raise RuntimeError(f"临时目录 {tempfile.gettempdir()} 落在真实 home 的受保护目录里：换个 TMPDIR 再跑")

TEMP_HOME = Path(tempfile.mkdtemp(prefix="maa-tests-home-"))
atexit.register(shutil.rmtree, TEMP_HOME, ignore_errors=True)

os.environ["HOME"] = str(TEMP_HOME)
os.environ["HERMES_HOME"] = str(TEMP_HOME / ".hermes")
os.environ["MAA_CONFIG_DIR"] = str(TEMP_HOME / ".config" / "maa-cli")
# deploy 读这两个变量决定频道和森空岛 token；开发机 shell 里设过就会改变用例行为。
for _name in ("MAA_CHANNEL_ID", "SKLAND_HG_TOKEN"):
    os.environ.pop(_name, None)

sys.addaudithook(_audit)


class NoRealGame:
    """顶替 `maa_playcover` 里的 `subprocess` / `socket`：开游戏、TERM、点屏幕、pgrep
    一律当场炸。2026-11-04 Depot 用例桩错了模块（桩的 `maa_daily.*`、调的是
    `maa_playcover.*`），整套单测真的把正在跑的明日方舟关掉了。设备层自己的行为在
    `test_playcover.py` 里测，那里自己给假的 subprocess / socket。"""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str):
        raise RealGameTouched(f"单测碰到了真游戏：maa_playcover.{self._name}.{attr}——自行打桩")


def guard_devices() -> list:
    """在 `setUpModule()` 里调：返回已 start 的 patch，`tearDownModule()` 里逆序 stop。"""
    from unittest.mock import patch

    import maa_playcover

    guards = [
        patch.object(maa_playcover, "subprocess", NoRealGame("subprocess")),
        patch.object(maa_playcover, "socket", NoRealGame("socket")),
    ]
    for item in guards:
        item.start()
    return guards


# 渲染出的 maa-cli TOML 要用 3.11+ 的 tomllib 解析回来验；脚本本身不用它。
# macOS 自带的 python3 是 3.9，那里这几条跳过而不是报 ModuleNotFoundError。
needs_tomllib = unittest.skipUnless(
    importlib.util.find_spec("tomllib") is not None, "需要 Python 3.11+ 的 tomllib"
)


@functools.lru_cache(maxsize=None)
def yaml_python() -> Path | None:
    """能 `import yaml` 的解释器：优先本解释器，其次 Hermes venv；都没有返回 None。"""
    if importlib.util.find_spec("yaml") is not None:
        return Path(sys.executable)
    candidate = REAL_HOME / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    if not candidate.is_file():
        return None
    probe = subprocess.run(
        [str(candidate), "-c", "import yaml"], capture_output=True, check=False
    )
    return candidate if probe.returncode == 0 else None
