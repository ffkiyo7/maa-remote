#!/usr/bin/env python3
"""肉鸽后台 worker：`run <token>` —— 抢锁 → 开机 → **一局一个子进程**地循环。

停止语义全部落在这里（spike C 第 3 节）：
- **本局结束后停** = 每局之间读一次 `stop_requested_at`。上游 `starts_count = 1`
  用尽时干净停在主题入口页，子进程正常退出，worker 看到标志就不再派下一局。
- **立即中止** = 轮询到 `abort_requested_at` 时对子进程 `terminate()`（SIGTERM），
  maa-cli 收到后 `bail!`，当前这一局作废。**绝不用 `subprocess.run(timeout=)`**：
  那条路超时走 `kill()`（SIGKILL），MaaCore 来不及 destroy，模拟器会留在地图里。

Deliberately NOT in the Hermes button whitelist; only maa_action.py may spawn it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import maa_action
import maa_roguelike
from maa_daily import (
    capture_screenshot,
    close_everything,
    open_emulator,
    settle_android_runtime,
    verify_game_installed,
    wait_for_android,
)
# 这三个是抄作业 worker 里已经在线上跑过的通用件：隔离临时 `MAA_CONFIG_DIR`、
# 灰卡通知、带截图的战报上传。复制一份只会多出第二处要维护的实现，
# 所以直接复用（执行逻辑仍然只有一份）。
from maa_copilot_worker import _isolated_config, _notice, _post_report
from maa_common import (
    RUNS_DIR,
    cleanup_run_files,
    emulator_is_online,
    load_config,
    maa_subprocess_env,
    merge_last_run,
    now_beijing,
    require_executable,
    run_lock,
)

# 单局超时：一局肉鸽十几到几十分钟，抄作业那条 900s 完全不够用。
DEFAULT_RUN_TIMEOUT = 3600
# 中止标志的轮询间隔。按钮把标志写进 `roguelike.json`，这里每 2s 读一次。
POLL_SECONDS = 2
# `terminate()` 之后给 maa-cli 收手的时间；超了才 `kill()`。
TERMINATE_GRACE = 10
# 上游连续报错就收摊：再跑下去只是反复空转、白占 run.lock。
ERROR_STREAK_LIMIT = 2

EMULATOR_BUSY_NOTICE = (
    "检测到模拟器正在使用（多半是你在玩，或上一个任务没退干净），本轮肉鸽未执行，"
    "也没有碰设备。等你退出后再点一次「▶️ 开始」。"
)


def _stop_flag(key: str) -> str:
    """从磁盘现读一次停止标志——按钮是另一个进程写的，内存里的状态一定是旧的。"""
    return str(maa_roguelike.load_state().get(key) or "")


def _next_round_blocker(config: dict[str, Any]) -> str:
    """还能不能再开一局：日常临近 / Depot 对表窗口。

    闸门实现只有一份，在 `maa_action`（不变量 #13）。肉鸽不耗理智，
    所以这里**不查**理智预算，只查这两条时间闸门：一局动辄几十分钟，
    撞上 08:00 / 22:00 会让日常抢不到 `run.lock`。
    """
    for reason in (
        maa_action._copilot_blackout_reason(config),
        maa_action.depot_window_reason(),
    ):
        if reason:
            return reason
    return ""


def _run_one_exploration(
    config: dict[str, Any], env: dict[str, str], log_path: Path, index: int
) -> tuple[int, str, str]:
    """跑一局，返回 `(退出码, 这一局的输出, 提前收手的原因)`。

    `Popen` + `terminate()` + `wait(timeout)` + 万不得已才 `kill()`——
    见文件头。`subprocess.run(timeout=)` 在这条路径上是被禁止的写法。
    """
    maa_bin = str(config.get("maa_bin", "/opt/homebrew/bin/maa"))
    timeout = int(config.get("roguelike_run_timeout", DEFAULT_RUN_TIMEOUT))
    command = [maa_bin, "run", "roguelike", "--user-resource", "--batch", "-v"]
    marker = f"# {now_beijing().isoformat()} roguelike run #{index}\n"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(marker)
    halt = ""
    with log_path.open("a", encoding="utf-8") as handle:
        proc = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT, env=env)
        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            if _stop_flag("abort_requested_at"):
                halt = "abort"
            elif time.monotonic() >= deadline:
                halt = "timeout"
            if halt:
                proc.terminate()
                try:
                    proc.wait(timeout=TERMINATE_GRACE)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=TERMINATE_GRACE)
                break
            time.sleep(POLL_SECONDS)
    if halt:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"# 本局被提前终止：{halt}\n")
    try:
        full = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        full = ""
    output = full.rsplit(marker, 1)[-1] if marker in full else full
    return proc.returncode or 0, output, halt


def _commit(token: str, fields: dict[str, Any]) -> None:
    """把这几个字段并进状态；token 对不上（用户已经开了新一轮）就不写。"""

    def mutate(state: dict[str, Any]) -> None:
        if str(state.get("token") or "") != token:
            return
        state.update(fields)

    maa_roguelike.update_state(mutate)


def _finish(token: str, *, dirty: bool, theme: str) -> None:
    """本轮收摊：状态回 idle，停止标志清掉，`dirty` / `last_theme` 留给下一轮判主题。"""
    _commit(
        token,
        {
            "status": maa_roguelike.IDLE,
            "token": "",
            "stop_requested_at": None,
            "abort_requested_at": None,
            "dirty": dirty,
            "last_theme": theme,
        },
    )


def cmd_run(token: str) -> int:
    state = maa_roguelike.load_state()
    if (str(state.get("token") or "") != token
            or state.get("status") not in maa_roguelike.LIVE_STATUSES):
        # 用户已经开了新的一轮（或这一轮已经收过尾）。新的那轮自己会发消息，
        # 这里再发一条只会互相打架。
        print("roguelike: 本轮已被取代或已结束，不执行")
        return 2
    params = state.get("params")
    if not isinstance(params, dict) or not params.get("theme"):
        _notice(
            load_config(),
            "❌ 肉鸽未启动",
            "状态里没有可执行的参数（多半是状态文件被改过）。说「刷肉鸽」重新发一张卡再开始。",
            0xED4245,
        )
        return 2

    with run_lock(blocking=False) as acquired:
        if not acquired:
            message = "另一场 MAA 正在运行，本轮肉鸽未启动；等它跑完再点「▶️ 开始」。"
            print("roguelike SKIPPED: run.lock 被占用")
            _finish(token, dirty=bool(state.get("dirty")),
                    theme=str(state.get("last_theme") or ""))
            _notice(load_config(), "⏸️ 肉鸽本轮未启动", message, 0x99AAB5)
            return 1
        return _run_locked(state, token)


def _run_locked(state: dict[str, Any], token: str) -> int:
    config = load_config()
    params = dict(state.get("params") or {})
    theme = str(params.get("theme") or "")
    at = now_beijing()
    stem = f"{at.date().isoformat()}-roguelike-{token}"
    log_path = RUNS_DIR / f"{stem}.log"
    png_path = RUNS_DIR / f"{stem}.png"
    started = time.monotonic()
    emulator_touched = False
    dirty = bool(state.get("dirty"))
    last_theme = str(state.get("last_theme") or "")
    runs: list[dict[str, Any]] = []
    stop_reason = ""
    cleanup_errors: list[str] = []
    last_output = ""

    # 不变量 #24：抢到 `run.lock` 之后、`open_emulator()` 之前探一次。
    # 在线就什么都不碰——任何失败路径都会 `close_everything()` 退掉 BlueStacks，
    # 等于把正在玩的用户踢出游戏。
    online, probe_detail = emulator_is_online(config)
    if online:
        _finish(token, dirty=dirty, theme=last_theme)
        _notice(
            config,
            "⏸️ 肉鸽本轮未启动",
            f"{EMULATOR_BUSY_NOTICE}\n探测：{probe_detail}",
            0x99AAB5,
        )
        merge_last_run(
            {
                "status": "skipped",
                "phase": f"roguelike-{token}",
                "skip_reason": "emulator_busy",
                "summary": EMULATOR_BUSY_NOTICE,
                "emulator_probe": probe_detail,
                "finished_at": now_beijing().isoformat(),
            }
        )
        return 0

    # 任务文件以 `StartUp` 开头（`maa_roguelike.STARTUP_TOML`）：下面的开机链只拉
    # **模拟器**，拉起**游戏**的是 MaaCore 的 `StartUp`。2026-09-11 那次事故就是任务文件
    # 里少了它，模拟器停在 Android 桌面、`Roguelike` 直接 Error。
    # 任务文件整轮只渲染一次：参数在 `roguelike start` 那一刻就冻结了，
    # 中途改卡改不动这一轮（`roguelike set` 在 running 时直接拒绝）。
    # 卡片**不收**：开跑之后「⏹️ / ⏏️」两个按钮正是要一直可点的。
    task_toml = maa_roguelike.render_task_toml(params)
    error_streak = 0
    try:
        require_executable(config, "maa_bin", "/opt/homebrew/bin/maa", "maa-cli")
        require_executable(config, "adb_path", "/opt/homebrew/bin/adb", "ADB")
        merge_last_run(
            {
                "status": "running",
                "phase": f"roguelike-{token}",
                "stage": theme,
                "started_at": at.isoformat(),
                "log": str(log_path),
            }
        )
        # 开机是**懒的**：闸门 / 停止标志都在循环头上查，过了才碰设备。
        # 从前是先开机再进循环，首局就被挡时白开一次模拟器——开机加清理好几分钟，
        # 全程还占着 run.lock，正是它要让路的那个 Depot / 日常在等的锁。
        env: dict[str, str] = {}
        while True:
            if _stop_flag("abort_requested_at"):
                stop_reason = "你点了「立即中止」。"
                break
            if _stop_flag("stop_requested_at"):
                stop_reason = "你点了「本局结束后停止」。"
                break
            blocker = _next_round_blocker(config)
            if blocker:
                stop_reason = blocker
                if not emulator_touched:
                    # 一局都没开成：灰卡说清楚为什么（护栏 4，跳过必须可见）。
                    # 第 N+1 局被挡则只写进下面的战报，不再多发一张。
                    _notice(
                        config,
                        "⏸️ 肉鸽本轮未启动",
                        f"本轮未启动：{blocker}",
                        0x99AAB5,
                    )
                break
            if not emulator_touched:
                _notice(
                    config,
                    "▶️ 开始刷肉鸽",
                    f"{maa_roguelike.params_text(params)}\n"
                    "每打完一局发一条进度；说「肉鸽停」会在本局结束后退出。",
                    0x9B59B6,
                )
                open_emulator(config)
                emulator_touched = True
                wait_for_android(config)
                verify_game_installed(config)
                # 冷启模拟器同样会撞上 MaaCore 那 3 秒的 MaaTouch 窗口
                #（DEV_GUIDE §11.14）。
                settle_android_runtime(config)
                env = maa_subprocess_env(config)
                env["MAA_LOG_PREFIX"] = "Never"
            index = len(runs) + 1
            # 一局开跑之前先认下 `dirty`：进程被杀 / 断电时状态里也留着
            # 「这个主题有残局」，换主题会被拒（上游只在同主题下善后）。
            dirty, last_theme = True, theme
            _commit(token, {"dirty": True, "last_theme": theme})
            # 复用抄作业那份隔离配置：只读上游 resource、只写临时目录（不变量 #37）。
            # 它顺带覆盖的 `FightBegin` / `CopilotNavigationReady` 属于 Fight / Copilot
            # 链，肉鸽任务链根本走不到，留着无害；换成第二份实现才是真的代价。
            with _isolated_config(env, {"roguelike.toml": task_toml}) as run_env:
                return_code, output, halt = _run_one_exploration(
                    config, run_env, log_path, index
                )
            last_output = output
            parsed = maa_roguelike.parse_run_output(output, return_code)
            if halt == "abort":
                parsed["outcome"] = "stopped"
                parsed["clean"] = False
                parsed["detail"] = "被立即中止，本局作废"
            elif halt == "timeout":
                parsed["outcome"] = "stopped"
                parsed["clean"] = False
                parsed["detail"] = (
                    f"单局超过 {config.get('roguelike_run_timeout', DEFAULT_RUN_TIMEOUT)}s，"
                    "已终止，本局作废"
                )
            entry = {
                "n": index,
                "started_at": now_beijing().isoformat(),
                "ended_at": now_beijing().isoformat(),
                "outcome": parsed["outcome"],
                "invest": (parsed["invest"] or {}).get("count", 0),
                "exp": parsed["exp"] or 0,
                "detail": parsed["detail"],
            }
            runs.append(entry)
            dirty = not parsed["clean"]
            _commit(token, {"runs": list(runs), "dirty": dirty, "last_theme": theme})
            _notice(
                config,
                f"🎲 肉鸽第 {index} 局结束",
                f"{parsed['detail']}\n累计 {len(runs)} 局 · {maa_roguelike.params_text(params)}",
                0x9B59B6 if parsed["clean"] else 0xFEE75C,
            )
            if halt:
                stop_reason = (
                    "你点了「立即中止」，当前这一局作废。" if halt == "abort"
                    else "单局超时，已终止本局。"
                )
                break
            if parsed["invest_full"]:
                stop_reason = "源石锭储备已达上限（999），上游自己收手了。"
                break
            # 按 `outcome` 而不是 `parsed["error"]` 数：游戏没起来时输出里只有
            # `StartUp Error`，`parse_run_output()` 会把它收敛成 `outcome == "error"`，
            # 这道闸门必须一样认（2026-09-11 事故：空转两局才靠 `Roguelike Error` 收尾）。
            error_streak = error_streak + 1 if parsed["outcome"] == "error" else 0
            if error_streak >= ERROR_STREAK_LIMIT:
                stop_reason = f"上游连续 {ERROR_STREAK_LIMIT} 局报错，本轮收尾。"
                break
    except Exception as exc:
        stop_reason = f"{type(exc).__name__}: {exc}"
        print(f"roguelike EXCEPTION: {stop_reason}", flush=True)
    finally:
        if emulator_touched:
            try:
                capture_screenshot(config, png_path)
            except Exception:
                pass
            try:
                cleanup_errors = close_everything(config)
            except Exception as exc:
                cleanup_errors = [f"清理异常：{type(exc).__name__}: {exc}"]

    ok = bool(runs) and not cleanup_errors and all(
        run["outcome"] in ("settled", "abandoned") for run in runs
    )
    duration = time.monotonic() - started
    embed: dict[str, Any] = {
        "title": f"{'✅' if ok else '⚠️'} 肉鸽本轮结束：共 {len(runs)} 局",
        "color": 0x57F287 if ok else 0xFEE75C,
        "fields": [
            {"name": "参数", "value": maa_roguelike.params_text(params), "inline": False},
            {"name": "逐局结果", "value": maa_roguelike.run_lines(runs)[:1024],
             "inline": False},
            {"name": "总耗时", "value": f"{duration / 60:.0f} 分钟", "inline": True},
        ],
        "footer": {"text": "肉鸽不消耗理智 · "
                           + ("模拟器与 ADB 已执行清理" if emulator_touched
                              else "全程没有碰设备")},
        "timestamp": now_beijing().isoformat(),
    }
    embed["fields"].extend(
        maa_roguelike.summary_fields(maa_roguelike.parse_summary(last_output))
    )
    if stop_reason:
        embed["fields"].append(
            {"name": "停止原因", "value": stop_reason[:1024], "inline": False}
        )
    if dirty:
        embed["fields"].append(
            {
                "name": "🚧 有残局",
                "value": f"最后一局没有干净结束，下次开始必须还是"
                         f"{maa_roguelike.theme_name(theme)}——上游只在同主题下"
                         "自己放弃残局，换主题会卡死。",
                "inline": False,
            }
        )
    if cleanup_errors:
        embed["fields"].append(
            {"name": "清理异常", "value": "; ".join(cleanup_errors)[:1024], "inline": False}
        )
    _post_report(config, embed, png_path if emulator_touched else None)

    merge_last_run(
        {
            "status": "success" if ok else "failed",
            "finished_at": now_beijing().isoformat(),
            "summary": f"肉鸽：{len(runs)} 局"
            + (f"；停在 {stop_reason}" if stop_reason else ""),
            "ok": ok,
            "cleanup_errors": cleanup_errors,
        }
    )
    cleanup_run_files(int(config.get("retention_days", 14)))
    _finish(token, dirty=dirty, theme=last_theme or theme)
    print(f"roguelike ok={ok} runs={len(runs)} stop={stop_reason}", flush=True)
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    if argv[:1] == ["run"] and len(argv) == 2:
        code = cmd_run(argv[1])
        # 与抄作业 worker 同样的强制退出：残留解释器曾把 run.lock 多握 10 分钟。
        sys.stdout.flush()
        os._exit(code)
    print("用法: maa_roguelike_worker.py run <token>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
