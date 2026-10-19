#!/usr/bin/env python3
"""Background worker for the copilot (抄作业) flow.

Subcommands:
  search <token>     — prts search for one manually chosen stage (network, 5-20s);
                       success dispatches `run-batch` straight away (四期 D6)
  run-batch <token>  — 一条执行链：抢锁 → 开机 → 串行连抄 `entries` 里的每个
                       `(关, 模式)` → 截图 → 清理 → 一条多关战报

Deliberately NOT in the Hermes button whitelist; only maa_action.py and
maa_activity_watch.py may spawn this process.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import maa_action
import maa_clearing
import maa_copilot
import maa_gamedata
from maa_daily import (
    HOME_TASK_NAMES,
    capture_screenshot,
    close_everything,
    open_emulator,
    settle_android_runtime,
    verify_game_installed,
    wait_for_android,
)
from maa_common import (
    BUTTONS_PATH,
    RUNS_DIR,
    SCRIPTS_DIR,
    atomic_write_json,
    atomic_write_text,
    cleanup_run_files,
    discord_request,
    discord_token,
    emulator_is_online,
    load_config,
    load_json,
    maa_subprocess_env,
    merge_last_run,
    now_beijing,
    require_executable,
    retire_discord_components,
    run_lock,
    state_lock,
)

# 探到模拟器在用时的统一说法（D18）。抄作业还没碰设备，说清楚「未执行」。
EMULATOR_BUSY_NOTICE = (
    "检测到模拟器正在使用（多半是你在玩，或上一个任务没退干净），本次未执行，"
    "也没有碰设备。等你退出后再试一次即可。"
)

# Use upstream Fight navigation with a process-local, recognition-only FightBegin.
# v6.17.2 skips ALL Fight subtasks for times=0. Never alter shared resources.
NAV_TOML_TEMPLATE = """[[tasks]]
name = "启动国服"
type = "StartUp"
[tasks.params]
client_type = "Official"
start_game_enabled = true

[[tasks]]
name = "导航到关卡"
type = "Fight"
[tasks.params]
stage = "{stage}"
times = 1
series = 0
medicine = 0
stone = 0
server = "CN"
client_type = "Official"

[[tasks]]
name = "检查关卡界面"
type = "Custom"
[tasks.params]
task_names = ["CopilotNavigationReady"]
"""

# ⚠️ **突袭切换 2026-09-09 真机验证失败，功能默认关闭**（DEV_GUIDE §5.5 第 3 条）。
# Fight 导航停在的是**普通难度**详情页，要打突袭得先把难度切过去；MaaCore 自己
# 在 `MultiCopilotTaskPlugin` 里用的就是 `RaidConfirm` / `ChangeToRaidDifficulty`
# 这两个上游任务名，所以这里照抄后者。今晨的实测：SR-5 导航成功，
# `Custom(ChangeToRaidDifficulty)` 13 秒后 `Custom Error`——同一轮的收尾截图里
# 那个 ROI 是纯背景，SideStory「月行水上」的详情页上根本没有突袭切换按钮。
# 现在 `config.copilot_raid_switch_task` 空串 = **突袭功能整体关闭**：排轮不产生
# `raid` 条目，`_batch_run_locked()` 再兜一道，`switch_to_raid()` 直接拒绝。
RAID_SWITCH_TOML_TEMPLATE = """[[tasks]]
name = "切换突袭难度"
type = "Custom"
[tasks.params]
task_names = [{task}]
"""
DEFAULT_RAID_SWITCH_TASK = maa_clearing.DEFAULT_RAID_SWITCH_TASK

# 战后收尾（2026-09-09 加）：**先跳掉首通剧情，再回主界面，然后才许退游戏**。
# 现场：SR-8 两次抄作业（09-08 15:22、09-09 03:47）`Copilot Completed` 之后 5 秒
# `close_everything()` 就 `maa closedown` 把游戏杀了，两张收尾截图都停在战后剧情页
# （右上「自动 OFF / 跳过 ▶」）。结果是三星记上了、**代理指挥没解锁**——早场日常
# 点 SR-8 的代理指挥 10 次点不亮，`Fight Error` 空跑一场。
# 09-09 08:59 真机验证（`runs/2026-09-09-copilot-71bfa8404dbe.log`）：剧情跳过成功，
# 游戏进到**结算页**，代理指挥就解锁了——**界线是结算页，不是主界面**。顺序是
# 战斗结束 → 剧情 → 结算页，昨天被杀在剧情里所以结算页从没出现过。
# 第一段 `CopilotSettleSkip` 与主界面任务并列：在剧情页就走跳过链，在详情页就直接回家；
# 第二段再兜一道回主界面。跳过链派生自上游 `SkipBattlePlot`（`SETTLE_OVERLAY_TASKS`），
# 只把确认之后的 `next` 清空：上游接的 `EndOfPlot` 在跳过后直接进结算页时永远等不到，
# 真机上白等了 20 轮才报 Error。派生任务只写临时目录（不变量 #37）。
# 结算页本身**不处理**：多关连抄时下一关的 `StartUp` 自带 `EndOfAction → ClickCorner`
# （03:06 SR-5 → SR-6 的日志），最后一关停在结算页被关掉已验证无害。若哪一关结算页
# 之后还有第二个弹窗 / 剧情，再回来派生 `EndOfAction`（注意上游 `ClickCorner.next` 里
# 有 `StartButton2`，照搬会再开一局）。
SETTLE_SKIP_TASK = "CopilotSettleSkip"
SETTLE_OVERLAY_TASKS = {
    SETTLE_SKIP_TASK: {"baseTask": "SkipBattlePlot", "next": ["CopilotSettleSkipConfirm"]},
    "CopilotSettleSkipConfirm": {"baseTask": "SkipBattlePlotConfirm", "next": []},
}
SETTLE_TOML_TEMPLATE = """[[tasks]]
name = "跳过剧情"
type = "Custom"
[tasks.params]
task_names = {skip_names}

[[tasks]]
name = "返回主界面"
type = "Custom"
[tasks.params]
task_names = {home_names}
"""
RAID_DISABLED_NOTICE = (
    "突袭切换未启用（config.copilot_raid_switch_task 为空）；"
    "单文件 maa copilot --raid raid 不会切难度，跑下去等于拿普通难度冒充突袭"
)

# The takeover popup appears when the agent-command (代理指挥) switch is on:
# "接管后，本次行动内将无法回到代理指挥模式 / 确认要接管作战吗？"
# MaaCore's copilot chain does not handle it; tap the confirm button.
# Calibrated on a 1920x1080 capture (2026-08-02): popup text line sits at
# [781,490,343,56] and the confirm button sits opposite the × (cancel) at
# about (1252,739) — i.e. text right edge +130, text bottom +171.
POPUP_MARK = "确认要接管作战"
POPUP_CONFIRM_X_OFFSET = 130
POPUP_CONFIRM_Y_OFFSET = 171

def _search_path(token: str) -> Path:
    return maa_copilot.SEARCHES_DIR / f"{token}.json"


def _notice(
    config: dict[str, Any],
    title: str,
    body: str,
    color: int,
    components: list[dict[str, Any]] | None = None,
) -> None:
    """任何一次「没干成」都必须可见（护栏 4：不许静默 return）。"""
    payload: dict[str, Any] = {
        "embeds": [
            {
                "title": title[:250],
                "description": body[:3500],
                "color": color,
                "timestamp": now_beijing().isoformat(),
            }
        ],
        "allowed_mentions": {"parse": []},
    }
    if components:
        payload["components"] = components
    try:
        discord_request(
            discord_token(),
            "POST",
            f"/channels/{config.get('channel_id', '')}/messages",
            payload,
        )
    except Exception as exc:
        print(f"copilot notice failed: {type(exc).__name__}: {exc}", flush=True)


# ---------------------------------------------------------------------------
# 手动搜索（`copilot find <关卡>` 的后台部分）
# ---------------------------------------------------------------------------

def _fail_search(token: str, message: str) -> None:
    with state_lock():
        state = load_json(_search_path(token), {})
        if not isinstance(state, dict):
            state = {"token": token}
        if state.get("status") not in {"pending", "running"}:
            return
        state["status"] = "failed"
        state["error"] = message[:400]
        state["finished_at"] = now_beijing().isoformat()
        atomic_write_json(_search_path(token), state)


def _claim_search(token: str) -> dict[str, Any] | None:
    """Atomically claim one still-pending search."""
    path = _search_path(token)
    with state_lock():
        state = load_json(path, None)
        if not isinstance(state, dict) or state.get("status") != "pending":
            return None
        state["status"] = "running"
        state["search_started_at"] = now_beijing().isoformat()
        atomic_write_json(path, state)
        return state


def cmd_search(token: str) -> int:
    """手动路径：搜一关的作业，搜到就直接排程 + 派发（D6 / D13）。

    **不降级**：森空岛刷新失败直接报失败（不再「沿用缓存并提示」），
    没有 `ratio == 1.0` 且无缺口的作业也直接报失败并写清缺什么。
    """
    if not maa_copilot.SEARCH_TOKEN_RE.fullmatch(token or ""):
        print("invalid token", file=sys.stderr)
        return 2
    maa_copilot.ensure_private_dirs()

    # Serialize searches: a concurrent search is marked failed so the caller
    # can retry it instead of leaving a permanent pending state.
    with maa_copilot.SEARCH_LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        os.chmod(maa_copilot.SEARCH_LOCK_PATH, 0o600)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail_search(token, "已有另一项作业正在匹配，请稍后重试")
            return 1

        state = _claim_search(token)
        if state is None:
            return 0

        config: dict[str, Any] = {}
        code = str(state.get("stage") or "")
        try:
            config = load_config()
            with state_lock():
                maa_copilot.prune_expired_searches()

            index = maa_copilot.load_stage_index(config)
            stage_id = str(state.get("stage_id") or "")
            if not stage_id or stage_id not in index.get("stages", {}):
                raise RuntimeError(f"关卡 {code} 已不在本地索引中")
            sanity_cost = maa_copilot.stage_sanity_cost(stage_id, code, config)
            if sanity_cost is None:
                # EX 关不在上游 `stages.json` 里，游戏数据里有。
                sanity_cost = maa_gamedata.ap_cost(maa_gamedata.load_layout(), stage_id)
            if sanity_cost is None:
                raise RuntimeError(f"{code} 的理智消耗尚未进入 MaaResource，拒绝执行")

            import maa_skland

            try:
                maa_skland.refresh_roster()
            except Exception as exc:
                raise RuntimeError(
                    f"森空岛练度与理智刷新失败，本次不降级执行：{type(exc).__name__}: {exc}"
                ) from exc
            roster = maa_copilot.load_roster()

            candidate, reason, gaps = maa_copilot.pick_perfect_candidate(
                code, stage_id, config, roster
            )
            if candidate is None:
                detail = reason
                if gaps:
                    detail += "\n缺：" + "；".join(gaps)
                raise RuntimeError(detail)

            modes = [
                mode
                for mode in (state.get("modes") or ["normal"])
                if mode in ("normal", "raid")
            ] or ["normal"]
            entries = [
                {
                    "stage": code,
                    "stage_id": stage_id,
                    "mode": mode,
                    "candidate": candidate,
                    "sanity_cost": sanity_cost,
                }
                for mode in modes
            ]
            # 搜索本身要跑 5~20s，起点合规不代表现在还合规（07:50 点的关，搜完
            # 已经 07:51 也可能刚好跨进 blackout）。派发前再过一次同一份闸门。
            blocker = maa_action.dispatch_blocker(config, "", now_beijing())
            note = (
                "手动选关，选中即执行。"
                if not blocker
                else f"本轮未启动：{blocker}\n"
                "手动轮不会自动重试，请日常结束后重新选关；"
                "这一轮点「❌ 取消本轮」即可收起，过期也会自动清掉。"
            )
            message_id = maa_action.publish_round(
                token,
                entries,
                config,
                title=f"🤖 已排程：{code}",
                note=note,
            )
            committed = False
            with state_lock():
                latest = load_json(_search_path(token), None)
                if isinstance(latest, dict) and latest.get("status") == "running":
                    latest["status"] = "scheduled" if blocker else "queued"
                    latest["entries"] = entries
                    latest["message_id"] = message_id
                    latest["finished_at"] = now_beijing().isoformat()
                    if blocker:
                        latest["last_prepare_error"] = blocker
                        latest["last_prepare_failed_at"] = now_beijing().isoformat()
                    else:
                        latest["execution_requested_at"] = now_beijing().isoformat()
                    atomic_write_json(_search_path(token), latest)
                    committed = True
            if not committed:
                retire_discord_components(config, message_id)
                return 0
            if blocker:
                print(f"copilot search: 本轮未派发（{blocker}）", flush=True)
                return 0
            maa_action.dispatch_batch(token)
        except Exception as exc:
            _fail_search(token, f"{type(exc).__name__}: {exc}")
            _notice(
                config,
                f"❌ {code} 抄作业未开始",
                f"{exc}\n"
                "不使用助战、不忽略练度要求，所以这一关只能等你练度够了或上游出新作业。",
                0xED4245,
            )
            return 1
    return 0


# ---------------------------------------------------------------------------
# 一条执行链（四期 2d）
# ---------------------------------------------------------------------------

def _prepare_op_file(
    candidate: dict[str, Any], stage_id: str, config: dict[str, Any]
) -> tuple[str, bool]:
    """Resolve the op URI; rewrite display-code ops into prepared/<id>.json."""
    uri, needs_rewrite = maa_copilot.execution_uri(candidate, stage_id)
    if not needs_rewrite:
        return uri, False
    cand_id = int(candidate.get("id") or 0)
    payload = maa_copilot.http_get_json(
        f"{maa_copilot.PRTS_BASE}/copilot/get/{cand_id}", timeout=30
    )
    # Real shape: {"status_code":…, "data":{…, "content":"<json string>"}}
    inner = payload.get("data") if isinstance(payload, dict) else None
    content = inner.get("content") if isinstance(inner, dict) else None
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            content = None
    if not isinstance(content, dict):
        raise RuntimeError(f"作业 {cand_id} 内容获取失败")
    content["stage_name"] = stage_id
    target = maa_copilot.PREPARED_DIR / f"{cand_id}.json"
    atomic_write_json(target, content)
    return uri, True


def _copilot_summary(lines: list[str], *, ok: bool) -> str:
    """Chinese summary of one `maa copilot` run (regexes tuned on real v6 logs)."""
    parts: list[str] = []
    missions = 0
    deployments: list[str] = []
    for line in lines:
        text = str(line).strip()
        if not text:
            continue
        if "MissionStart" in text:
            missions += 1
        deploy = re.search(r"CurrentSteps Deploy (\S+)", text)
        if deploy:
            name = deploy.group(1)
            if name not in deployments:
                deployments.append(name)
    if missions:
        parts.append(f"进入战斗 ×{missions}")
    if deployments:
        parts.append("部署：" + "、".join(deployments[:6]))
    if not ok:
        for line in lines:
            text = str(line).strip()
            if re.search(r"(Copilot Error|Error: Some error)", text):
                parts.append(text[:100])
                break
    return " · ".join(parts[:4])


def _run_copilot_with_watchdog(
    config: dict[str, Any],
    command: list[str],
    log_handle: Any,
    log_path: Path,
    env: dict[str, str],
    *,
    timeout: int | None = None,
    stall: tuple[str, int] | None = None,
) -> tuple[int, int]:
    """Run maa copilot; watchdog taps the takeover popup if it appears.

    The popup blocks MaaCore's battle recognition, so without handling it
    every run on a 代理指挥-enabled stage fails.  Poll the screen with the
    local OCR binary (deployed as ~/.hermes/scripts/ocrbox) and tap the
    confirm button when the exact popup text is seen.

    `stall = (标记, 秒)` 是多作业的「无进展」超时：日志里这个标记（每开始一份作业
    出现一次）隔这么久都没多出一个，就当它卡在地图弹窗上了，结束进程
    （2026-09-08 SR-6 塔罗牌弹窗让列表模式空转 9 分钟）。
    """
    adb = str(config.get("adb_path", "/opt/homebrew/bin/adb"))
    address = str(config.get("adb_address", "127.0.0.1:5555"))
    ocrbox = SCRIPTS_DIR / "ocrbox"
    watch_png = RUNS_DIR / ".watch.png"
    # 单关级超时：整场的长度由 `min_lead_reason()` 的时间预算约束，不在这里叠加。
    if timeout is None:
        timeout = int(config.get("maa_run_timeout", 3000))
    start_offset = log_path.stat().st_size if log_path.exists() else 0
    proc = subprocess.Popen(
        command,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        env=env,
    )
    taps = 0
    deadline = time.monotonic() + timeout
    seen_marks = 0
    stall_deadline = time.monotonic() + (stall[1] if stall else timeout)
    next_stall_check = time.monotonic() + 30
    try:
        while proc.poll() is None:
            now = time.monotonic()
            if stall and now >= next_stall_check:
                next_stall_check = now + 30
                marks = _log_since(log_path, start_offset).count(stall[0])
                if marks > seen_marks:
                    seen_marks = marks
                    stall_deadline = now + stall[1]
            if now >= deadline or (stall and now >= stall_deadline):
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        "maa copilot 超时\n" if now >= deadline
                        else f"maa copilot 无进展超时（{stall[1]}s 没有开始下一份作业）\n"
                    )
                return 124, taps
            time.sleep(2)
            if not ocrbox.is_file():
                continue
            try:
                with watch_png.open("wb") as handle:
                    subprocess.run(
                        [adb, "-s", address, "exec-out", "screencap", "-p"],
                        stdout=handle,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=15,
                    )
                if watch_png.stat().st_size < 8:
                    continue
                result = subprocess.run(
                    [str(ocrbox), str(watch_png)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
            except Exception:
                continue
            if POPUP_MARK not in result.stdout:
                continue
            match = re.search(
                rf"{re.escape(POPUP_MARK)}[^\t]*\t\[(\d+),(\d+),(\d+),(\d+)\]",
                result.stdout,
            )
            if not match:
                continue
            x, y, w, h = (int(part) for part in match.groups())
            tap_x = x + w + POPUP_CONFIRM_X_OFFSET
            tap_y = y + h + POPUP_CONFIRM_Y_OFFSET
            try:
                subprocess.run(
                    [adb, "-s", address, "shell", "input", "tap", str(tap_x), str(tap_y)],
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                taps += 1
            except Exception:
                pass
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"# watchdog: 检测到接管弹窗，点击确认 (tap {tap_x},{tap_y})\n")
            time.sleep(4)
    finally:
        try:
            watch_png.unlink()
        except OSError:
            pass
    return proc.returncode or 0, taps


@contextmanager
def _isolated_config(
    env: dict[str, str],
    task_files: dict[str, str],
    overlay_tasks: dict[str, dict[str, Any]] | None = None,
    *,
    no_battle: bool = True,
):
    """临时 `MAA_CONFIG_DIR`，把不进战斗的覆盖与任务文件隔离在本进程内。

    只**读**上游 resource 目录、只**写**这个临时目录（不变量 #37）：日常与随后
    真正的 `maa copilot` 仍然用原配置，`~/.config/maa-cli/` 一个字节都不动。
    `overlay_tasks` 是调用方额外派生的任务（`baseTask` 继承上游、只改 `next`），
    与这里固定的 `FightBegin` / `CopilotNavigationReady` 覆盖写进同一份 tasks.json。
    `no_battle=False` 只给多作业那一趟用：它本来就要开打，不能挡掉 `FightBegin`。
    """
    source = Path(env["MAA_CONFIG_DIR"])
    with tempfile.TemporaryDirectory(prefix="maa-copilot-nav-") as raw:
        root = Path(raw)
        for name in ("profiles", "resource"):
            if (source / name).is_dir():
                shutil.copytree(source / name, root / name)
        for name in ("cli.toml", "asst.toml"):
            if (source / name).is_file():
                shutil.copy2(source / name, root / name)
        # Keep upstream recognition, but no click, subtask, or continuation into battle.
        ready = {
            "baseTask": "BattleStartPre", "algorithm": "OcrDetect", "action": "DoNothing",
            "sub": [], "next": [], "onErrorNext": [], "exceededNext": [],
        }
        resource = root / "resource" / "tasks" / "tasks.json"
        overlay = load_json(resource, {})
        if no_battle:
            overlay["FightBegin"] = dict(ready)
            overlay["CopilotNavigationReady"] = dict(ready)
        for name, task in (overlay_tasks or {}).items():
            overlay[name] = dict(task)
        atomic_write_json(resource, overlay)
        for name, body in task_files.items():
            atomic_write_text(root / "tasks" / name, body)
        yield {**env, "MAA_CONFIG_DIR": str(root)}


@contextmanager
def _navigation_config(stage: str, env: dict[str, str]):
    """Isolate the no-battle override from daily runs and subsequent Copilot."""
    with _isolated_config(
        env, {"copilot-nav.toml": NAV_TOML_TEMPLATE.format(stage=stage)}
    ) as nav_env:
        yield nav_env


def _navigate_to_stage(
    config: dict[str, Any], stage: str, env: dict[str, str], log_path: Path
) -> tuple[bool, str]:
    """Run upstream navigation, then recognize the stage screen without clicking."""
    supported, detail = maa_clearing.nav_checker(maa_copilot._resource_dirs(config))(stage)
    if not supported:
        return False, detail
    maa_bin = str(config.get("maa_bin", "/opt/homebrew/bin/maa"))
    with _navigation_config(stage, env) as nav_env:
        nav_result = subprocess.run(
            [maa_bin, "run", "copilot-nav", "--user-resource", "-v", "--batch"],
            capture_output=True, text=True, check=False, timeout=900, env=nav_env,
        )
    nav_output = f"{nav_result.stdout}\n{nav_result.stderr}"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(nav_output)
    if (nav_result.returncode != 0 or "Fight Completed" not in nav_output
            or "Custom Completed" not in nav_output):
        detail = " | ".join(nav_output.strip().splitlines()[-3:])[-400:]
        return False, f"{stage} 导航或关卡界面检查失败：{detail}"
    return True, ""


def switch_to_raid(
    config: dict[str, Any], stage: str, env: dict[str, str], log_path: Path
) -> tuple[bool, str]:
    """把关卡详情页从普通难度切到突袭（⚠️ **2026-09-09 真机验失败**，见文件头注释）。

    `config.copilot_raid_switch_task` 是这一步的开关，**空串 = 突袭功能关闭**，
    此时这里**直接失败**而不是「跳过切换接着打」：单文件
    `maa copilot <uri> --raid raid` 不会去切难度（`is_raid` 只在 `copilot_list`
    的条目里有意义），跳过切换打完记的会是一次假的突袭通关。正常路径根本走不到
    这里——排轮与 `_batch_run_locked()` 早就把 `raid` 条目滤掉了。
    """
    task = maa_clearing.raid_switch_task(config)
    if not task:
        return False, f"{stage} {RAID_DISABLED_NOTICE}"
    maa_bin = str(config.get("maa_bin", "/opt/homebrew/bin/maa"))
    body = RAID_SWITCH_TOML_TEMPLATE.format(task=json.dumps(task, ensure_ascii=False))
    with _isolated_config(env, {"copilot-raid.toml": body}) as raid_env:
        result = subprocess.run(
            [maa_bin, "run", "copilot-raid", "--user-resource", "-v", "--batch"],
            capture_output=True, text=True, check=False, timeout=300, env=raid_env,
        )
    output = f"{result.stdout}\n{result.stderr}"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"# raid-switch {stage} task={task}\n{output}")
    if result.returncode != 0 or "Custom Completed" not in output:
        detail = " | ".join(output.strip().splitlines()[-3:])[-400:]
        return False, f"{stage} 切换突袭难度失败：{detail}"
    return True, ""


def settle_after_battle(
    config: dict[str, Any], stage: str, env: dict[str, str], log_path: Path
) -> tuple[bool, str]:
    """一关打完后收尾：跳过战后剧情 → 返回主界面（见 `SETTLE_TOML_TEMPLATE`）。

    **两段任一 `Completed` 即算成功**：第一段完成 = 剧情已跳过（或本来就在能回家的
    页面），此时游戏至少到了结算页，代理指挥的记录已经落库（09-09 真机）；第二段完成
    = 真的回到了主界面。只影响收尾质量，不改这一关的胜负判定：失败只把一句中性说明
    带回战报。跑在与导航同样隔离的临时 `MAA_CONFIG_DIR` 里。
    """
    maa_bin = str(config.get("maa_bin", "/opt/homebrew/bin/maa"))
    body = SETTLE_TOML_TEMPLATE.format(
        skip_names=json.dumps([SETTLE_SKIP_TASK, *HOME_TASK_NAMES], ensure_ascii=False),
        home_names=json.dumps(HOME_TASK_NAMES, ensure_ascii=False),
    )
    timeout = max(60, int(config.get("copilot_settle_timeout", 240)))
    try:
        with _isolated_config(
            env, {"copilot-settle.toml": body}, overlay_tasks=SETTLE_OVERLAY_TASKS
        ) as settle_env:
            result = subprocess.run(
                [maa_bin, "run", "copilot-settle", "--user-resource", "-v", "--batch"],
                capture_output=True, text=True, check=False, timeout=timeout, env=settle_env,
            )
    except Exception as exc:
        note = f"{stage} 战后收尾异常：{type(exc).__name__}: {exc}"
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"# settle {stage}\n{note}\n")
        return False, note[:300]
    output = f"{result.stdout}\n{result.stderr}"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"# settle {stage}\n{output}")
    skipped = re.search(r"\[跳过剧情\][^\n]*Completed", output) is not None
    home_done = re.search(r"\[返回主界面\][^\n]*Completed", output) is not None
    if not (skipped or home_done):
        detail = " | ".join(output.strip().splitlines()[-3:])[-300:]
        return False, f"{stage} 战后未回到主界面：{detail}"
    return True, ""


class PrefightError(RuntimeError):
    """开打前的**环境**故障（森空岛读不到、理智消耗查不到）。

    和「理智不够」分开：后者等一会儿自己会好，前者不修就每 15 分钟开一次
    模拟器白跑一趟（2026-10-11 森空岛过期，YW-4 空转 17 轮）。worker 把它记进
    这一轮的 `prefight_error`，watcher 按天计数、到上限当天停排。
    """


def _recheck_before_stage(
    config: dict[str, Any], state: dict[str, Any], entry: dict[str, Any]
) -> tuple[bool, str]:
    """每关开打前的复核：活动还在不在、理智够不够。

    理智不够 → `(False, 说明)`；环境故障 → 抛 `PrefightError`。

    活动只按 **key** 复核（不看具体关卡）：目标集合是整个活动的关，而
    `maa activity` 只列上游推荐的那 4 关，按关卡复核会把 `SR-1` 判成过期。
    非活动关（资源本 / 主线）没有 `activity_key`，整段跳过。
    """
    stage = str(entry.get("stage") or "")
    activity_key = str(state.get("activity_key") or "")
    if activity_key:
        from maa_plan_card import activity_target, query_activity_snapshot

        target = activity_target(query_activity_snapshot(config))
        if target is None or target["key"] != activity_key:
            raise maa_copilot.StaleActivityError(
                f"活动已切换，{stage} 不再属于当前目标，本轮停止"
            )
    try:
        cost = int(entry.get("sanity_cost") or 0)
    except (TypeError, ValueError):
        cost = 0
    if cost <= 0:
        raise PrefightError(f"无法确认 {stage} 的理智消耗，拒绝开打")
    import maa_skland

    try:
        roster = maa_skland.refresh_roster()
    except Exception as exc:
        raise PrefightError(
            f"森空岛实时理智读取失败：{type(exc).__name__}: {exc}"
        ) from exc
    preflight = maa_copilot.sanity_preflight(roster, cost)
    if not preflight.get("known"):
        raise PrefightError(
            f"森空岛实时理智读取失败：{preflight.get('reason', '未知原因')}"
        )
    if not preflight.get("enough"):
        return False, maa_copilot.sanity_status_text(preflight)
    return True, ""


def _set_status(
    token: str, expected: set[str], status: str, **extra: Any
) -> dict[str, Any] | None:
    """状态机的唯一改写口：只有当前状态在 `expected` 里才改，返回改后的状态。"""
    path = _search_path(token)
    with state_lock():
        fresh = load_json(path, None)
        if not isinstance(fresh, dict) or fresh.get("status") not in expected:
            return None
        fresh["status"] = status
        fresh.update(extra)
        atomic_write_json(path, fresh)
        return fresh


def _retire_round_controls(token: str, config: dict[str, Any], message_id: Any) -> None:
    """收掉「取消本轮」按钮：开始碰设备之后取消已经没有意义。"""
    with state_lock():
        registry = load_json(BUTTONS_PATH, {})
        if not isinstance(registry, dict):
            raise RuntimeError("共享按钮登记表格式错误")
        maa_copilot.prune_search_buttons(token, registry)
        atomic_write_json(BUTTONS_PATH, registry)
    try:
        retire_discord_components(config, message_id)
    except Exception as exc:
        print(f"round card retire failed: {type(exc).__name__}: {exc}", flush=True)


def cmd_run_batch(token: str) -> int:
    if not maa_copilot.SEARCH_TOKEN_RE.fullmatch(token or ""):
        return 2
    state = load_json(_search_path(token), None)
    if not isinstance(state, dict) or state.get("status") != "queued":
        return 2
    entries = state.get("entries")
    if not isinstance(entries, list) or not entries:
        return 2

    with run_lock(blocking=False) as acquired:
        if not acquired:
            message = "另一场 MAA 正在运行，本轮未启动；到点会自动重试。"
            print("copilot run-batch SKIPPED: run.lock 被占用")
            _set_status(token, {"queued"}, "scheduled", last_prepare_error=message)
            _notice(load_config(), "⏸️ 自动作业本轮未启动", message, 0x99AAB5)
            return 1
        # 最后一道防线（不变量 #13）：派发与真正开跑之间可能隔了很久（worker 排队、
        # 锁等待），这里再过一次三条**时间**闸门。run.lock 与模拟器不再查——锁刚
        # 被自己拿在手上，再查只会自相矛盾；模拟器由 `_batch_run_locked()` 探。
        config = load_config()
        if state.get("source") == "activity-watch" and maa_copilot.auto_pause_state():
            # 派发之后才按的暂停：还没碰设备，直接作废，不开模拟器。
            print("copilot run-batch SKIPPED: 自动作业已暂停")
            _set_status(token, {"queued"}, "superseded", superseded_at=now_beijing().isoformat())
            return 1
        for reason in (
            maa_action._copilot_blackout_reason(config),
            maa_action.depot_window_reason(),
            maa_action.min_lead_reason(config),
        ):
            if reason:
                message = f"本轮未启动：{reason}"
                print(f"copilot run-batch SKIPPED: {reason}")
                _set_status(token, {"queued"}, "scheduled", last_prepare_error=message)
                _notice(config, "⏸️ 自动作业本轮未启动", message, 0x99AAB5)
                return 1
        return _batch_run_locked(state)


def _entry_result_lines(results: list[dict[str, Any]]) -> str:
    icons = {"won": "✅", "failed": "❌", "unknown": "❓", "skipped": "⏭️"}
    lines: list[str] = []
    for result in results:
        label = maa_clearing.entry_label(
            maa_clearing.make_entry(result["stage"], result["mode"])
        )
        icon = icons.get(str(result.get("outcome")), "❓")
        detail = str(result.get("detail") or "")
        lines.append(f"{icon} `{label}`" + (f" — {detail[:120]}" if detail else ""))
    return "\n".join(lines) or "—"


def _use_list_path(state: dict[str, Any]) -> bool:
    """当期活动关（`activity_key` 非空）走多作业路径，其余走 B 基线。"""
    return bool(str(state.get("activity_key") or ""))


def _batch_run_locked(state: dict[str, Any]) -> int:
    """锁内连抄。两条路：

    - 当期活动关（`activity_key` 非空）：锚点导航停在地图 → 剧情关 / 区域入口 →
      `copilot_list` 多作业（`_run_activity_round()`，2026-10-19 解除 D22）。
      spike A 挂起它的两个原因现在都有对策：逐关成败看 `-vvv` 下每份开始时的
      `CopilotListLoadTaskFileSuccess`；地图弹窗卡死由「无进展」超时收住。
    - 其余的关（资源本、主线）：每关一次上游 Fight 导航 + 单作业 copilot（B 基线）。
    """
    config = load_config()
    token = str(state.get("token") or "")
    entries = [entry for entry in state.get("entries") or [] if isinstance(entry, dict)]
    at = now_beijing()
    stem = f"{at.date().isoformat()}-copilot-{token}"
    log_path = RUNS_DIR / f"{stem}.log"
    png_path = RUNS_DIR / f"{stem}.png"
    started = time.monotonic()
    emulator_touched = False
    results: list[dict[str, Any]] = []
    stop_reason = ""
    cleanup_errors: list[str] = []

    # D18：抢到锁之后、碰设备之前先探一次。在线就把这一轮放回排程，什么都不做——
    # 任何失败路径都会 `close_everything()` 退掉 BlueStacks，等于把人踢出游戏。
    online, probe_detail = emulator_is_online(config)
    if online:
        _set_status(token, {"queued"}, "scheduled", last_prepare_error=EMULATOR_BUSY_NOTICE)
        _notice(
            config,
            "⏸️ 自动作业本轮未启动",
            f"{EMULATOR_BUSY_NOTICE}\n探测：{probe_detail}",
            0x99AAB5,
        )
        merge_last_run(
            {
                "status": "skipped",
                "phase": f"copilot-{token}",
                "skip_reason": "emulator_busy",
                "summary": EMULATOR_BUSY_NOTICE,
                "emulator_probe": probe_detail,
                "finished_at": now_beijing().isoformat(),
            }
        )
        return 0

    fresh = _set_status(
        token, {"queued"}, "executing", execution_started_at=at.isoformat()
    )
    if fresh is None:
        print("copilot run-batch: 状态已在启动前失效", flush=True)
        return 1
    _retire_round_controls(token, config, fresh.get("message_id"))

    # 非活动关（资源本 / 主线）的 `activity_key` 是空串：账本另有一份
    # `ledger-standalone.json`，`ledger_path()` 与 `ensure_ledger()` 必须用**同一个
    # key** 算落点，否则记完账会把独立账本写回当期活动账本。
    activity_key = str(state.get("activity_key") or "")
    ledger, _note = maa_copilot.ensure_ledger(activity_key)
    ledger_file = maa_copilot.ledger_path(activity_key)
    failed_stages: set[str] = set()
    # 只有 watcher 自动排的轮受「暂停自动作业」开关管；手动 `copilot find` 照打。
    auto_round = str(state.get("source") or "") == "activity-watch"
    prefight_error = ""
    start_components: list[dict[str, Any]] | None = None
    if auto_round:
        try:
            maa_copilot.register_auto_buttons()
            start_components = [maa_copilot.pause_button_row()]
        except Exception as exc:
            print(f"pause button register failed: {type(exc).__name__}: {exc}", flush=True)
    try:
        require_executable(config, "maa_bin", "/opt/homebrew/bin/maa", "maa-cli")
        require_executable(config, "adb_path", "/opt/homebrew/bin/adb", "ADB")
        atomic_write_json(
            RUNS_DIR / f"{stem}.json",
            {
                "date": at.date().isoformat(),
                "phase": f"copilot-{token}",
                "entries": [
                    {"stage": entry.get("stage"), "mode": entry.get("mode")}
                    for entry in entries
                ],
                "started_at": at.isoformat(),
                "status": "running",
            },
        )
        merge_last_run(
            {
                "status": "running",
                "phase": f"copilot-{token}",
                "stage": str(entries[0].get("stage") or ""),
                "started_at": at.isoformat(),
                "log": str(log_path),
            }
        )
        _notice(
            config,
            f"▶️ 开始抄作业：共 {len(entries)} 项",
            "\n".join(maa_copilot.entry_lines(entries))[:3500],
            0x5865F2,
            start_components,
        )

        open_emulator(config)
        emulator_touched = True
        wait_for_android(config)
        verify_game_installed(config)
        # 抄作业开的是同一台冷启模拟器，同样会撞上 MaaCore 那 3 秒的 MaaTouch 窗口
        # （DEV_GUIDE §11.14）。这里没有日常那层重试兜底，闸门更不能省。
        settle_android_runtime(config)
        env = maa_subprocess_env(config)
        env["MAA_LOG_PREFIX"] = "Never"

        if _use_list_path(state):
            # 当期活动关：一次锚点导航 + 分段多作业（`_run_activity_round()`）。
            stop_reason = _run_activity_round(
                config, state, entries, env, log_path, ledger, ledger_file,
                results, auto_round,
            )
        else:
            raid_on = maa_clearing.raid_enabled(config)
            for entry in entries:
                stage = str(entry.get("stage") or "")
                mode = str(entry.get("mode") or "normal")
                if mode == "raid" and not raid_on:
                    # 最后一道防线：条目可能是开关关掉**之前**排的（轮次落盘后配置改了、
                    # 手动轮排在前面），照跑会打普通难度却记成突袭通关。宁可什么都不做。
                    results.append(
                        {"stage": stage, "mode": mode, "outcome": "skipped",
                         "detail": RAID_DISABLED_NOTICE}
                    )
                    continue
                if stage in failed_stages:
                    # 普通没过就别去撞突袭：突袭是同一关再打一遍，普通没通它根本没解锁。
                    results.append(
                        {"stage": stage, "mode": mode, "outcome": "skipped",
                         "detail": "同一关的普通模式未通过"}
                    )
                    continue
                if auto_round and maa_copilot.auto_pause_state():
                    stop_reason = "自动作业已暂停"
                    break
                lead = maa_action.min_lead_reason(config)
                if lead:
                    stop_reason = lead
                    break
                ok, detail = _recheck_before_stage(config, state, entry)
                if not ok:
                    stop_reason = detail
                    break
                outcome, detail = _fight_one(config, entry, env, log_path)
                if outcome != "nav":
                    # 进过战斗就要正常收尾：跳过首通剧情、回主界面，再往下走（或退游戏）。
                    # 直接 closedown 会把结算流程掐断——SR-8 三星却没解锁代理指挥（2026-09-09）。
                    settled, settle_note = settle_after_battle(config, stage, env, log_path)
                    if not settled:
                        detail = " · ".join(part for part in (detail, settle_note) if part)[:400]
                verdict = {"won": "won", "unknown": "unknown"}.get(outcome, "failed")
                maa_copilot.record_copilot_attempt(
                    ledger,
                    stage,
                    mode,
                    outcome=verdict,
                    op=int((entry.get("candidate") or {}).get("id") or 0),
                    detail=detail,
                    log_name=log_path.name,
                )
                maa_copilot.save_ledger(ledger, ledger_file)
                results.append(
                    {"stage": stage, "mode": mode, "outcome": verdict, "detail": detail}
                )
                if verdict == "won":
                    continue
                # 突袭没过 → 这一关不算通关（D7），下一轮只排它剩下的模式。
                failed_stages.add(stage)
                if outcome == "nav":
                    # 导航 / 切难度失败说明环境坏了，后面几关只会一路失败。
                    stop_reason = detail
                    break
    except maa_copilot.StaleActivityError as exc:
        stop_reason = str(exc)
    except PrefightError as exc:
        stop_reason = str(exc)
        prefight_error = stop_reason
    except Exception as exc:
        stop_reason = f"{type(exc).__name__}: {exc}"
        print(f"copilot run-batch EXCEPTION: {stop_reason}", flush=True)
        if not any(r["outcome"] in ("won", "failed", "unknown") for r in results):
            # 一关都没进就炸（开机、ADB、游戏没装）：同样是环境故障，
            # 不记账就永远到不了 daily_cap，watcher 会每 tick 重派。
            prefight_error = stop_reason
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

    won = [r for r in results if r["outcome"] == "won"]
    ok = bool(won) and not stop_reason and not cleanup_errors and all(
        r["outcome"] in ("won", "skipped") for r in results
    )
    duration = time.monotonic() - started
    embed: dict[str, Any] = {
        "title": f"{'✅' if ok else '⚠️'} 抄作业本轮结束：{len(won)}/{len(entries)} 通过",
        "color": 0x57F287 if ok else 0xFEE75C,
        "fields": [
            {"name": "逐关结果", "value": _entry_result_lines(results)[:1024], "inline": False},
            {"name": "总耗时", "value": f"{duration:.0f}s", "inline": True},
        ],
        "footer": {"text": "模拟器与 ADB 已执行清理 · 不吃药不碎石"},
        "timestamp": now_beijing().isoformat(),
    }
    if stop_reason:
        embed["fields"].append({"name": "停在", "value": stop_reason[:1024], "inline": False})
    if cleanup_errors:
        embed["fields"].append(
            {"name": "清理异常", "value": "; ".join(cleanup_errors)[:1024], "inline": False}
        )
    _post_report(config, embed, png_path if emulator_touched else None)

    merge_last_run(
        {
            "status": "success" if ok else "failed",
            "finished_at": now_beijing().isoformat(),
            "summary": f"抄作业：{len(won)}/{len(entries)} 通过"
            + (f"；停在 {stop_reason}" if stop_reason else ""),
            "ok": ok,
            "cleanup_errors": cleanup_errors,
        }
    )
    # 运行快照收到终态：以前一直停在 `running`，排查时分不清是还在跑还是早挂了。
    snapshot_path = RUNS_DIR / f"{stem}.json"
    snapshot = load_json(snapshot_path, None)
    if isinstance(snapshot, dict):
        snapshot.update(
            {
                "status": "success" if ok else "failed",
                "finished_at": now_beijing().isoformat(),
                "results": results,
                "stop_reason": stop_reason,
            }
        )
        atomic_write_json(snapshot_path, snapshot)
    cleanup_run_files(int(config.get("retention_days", 14)))
    extra: dict[str, Any] = {
        ("executed_at" if ok else "execution_failed_at"): now_beijing().isoformat()
    }
    if prefight_error:
        extra["prefight_error"] = prefight_error[:400]
    _set_status(
        token,
        {"executing"},
        "executed" if ok else "execution_failed",
        **extra,
    )
    print(
        f"copilot batch ok={ok} results={results} "
        f"threads={[t.name for t in threading.enumerate()]}",
        flush=True,
    )
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# 活动关：锚点导航停在地图 → 剧情关 / 区域入口 → 多作业（2026-10-19）
# ---------------------------------------------------------------------------

# 下面每段任务文件都照 2026-10-19 真机跑通的那份写（Mac 上
# `~/Developer/cc-scratch/2026-10-19-copilot-list-verify/` 的 map / story /
# storyplay / copilot）。只有区域入口那一步没在真机上跑过（EX 还没解锁）。
ZONE_READY_TASK = "CopilotZoneReady"
MAP_TOML_TEMPLATE = """[[tasks]]
name = "启动国服"
type = "StartUp"
[tasks.params]
client_type = "Official"
start_game_enabled = true

[[tasks]]
name = "回主界面"
type = "Custom"
[tasks.params]
task_names = {home_names}

[[tasks]]
name = "进终端"
type = "Custom"
[tasks.params]
task_names = ["Terminal-Entry"]

[[tasks]]
name = "进活动地图"
type = "Custom"
[tasks.params]
task_names = [{anchor}]

[[tasks]]
name = "核对区域"
type = "Custom"
[tasks.params]
task_names = ["{ready}"]
"""
STORY_TOML_TEMPLATE = """[[tasks]]
name = "点剧情关"
type = "Custom"
[tasks.params]
task_names = ["CopilotStoryOpen"]

[[tasks]]
name = "跳过剧情"
type = "Custom"
[tasks.params]
task_names = ["CopilotStoryStart"]

[[tasks]]
name = "核对区域"
type = "Custom"
[tasks.params]
task_names = ["{ready}"]
"""
ZONE_CHECK_TOML_TEMPLATE = """[[tasks]]
name = "核对区域"
type = "Custom"
[tasks.params]
task_names = ["{ready}"]
"""
# 三个降级开关一个都不写（不变量 #15）：不写就是 MaaCore 的默认值——不吃药、
# 不借助战、不忽略练度要求。
LIST_TOML_TEMPLATE = """[[tasks]]
name = "多作业"
type = "Copilot"
[tasks.params]
formation = true
copilot_list = [
{items}
]
"""
LIST_ITEM_TIMEOUT = 1500


def _recognize_only(base: str, texts: list[str]) -> dict[str, Any]:
    return {
        "baseTask": base, "text": list(texts), "action": "DoNothing",
        "sub": [], "next": [], "onErrorNext": [], "exceededNext": [],
    }


def _zone_ready_overlay(codes: list[str]) -> dict[str, Any]:
    """「站在这个区域的地图上」= 屏幕上认得出这个区域的任一关卡代号。"""
    return {ZONE_READY_TASK: _recognize_only("SideStoryStage", codes)}


def _story_overlay(code: str) -> dict[str, Any]:
    return {
        "CopilotStoryOpen": {"algorithm": "JustReturn",
                             "next": ["CopilotStoryClick", "CopilotStorySwipe"]},
        "CopilotStoryClick": {
            "baseTask": "SideStoryStage", "text": [code], "action": "ClickSelf",
            "postDelay": 2000, "sub": [], "next": [], "onErrorNext": [], "exceededNext": [],
        },
        "CopilotStorySwipe": {"baseTask": "SwipeToStage", "maxTimes": 8,
                              "next": ["CopilotStoryClick", "CopilotStorySwipe"]},
        "CopilotStoryStart": {
            "algorithm": "OcrDetect", "text": ["开始剧情"], "roi": [880, 380, 200, 120],
            "action": "ClickSelf", "postDelay": 3000, "next": ["CopilotStorySkip"],
        },
        "CopilotStorySkip": {"baseTask": "SkipBattlePlot",
                             "template": "SkipThePreBattlePlot.png",
                             "next": ["CopilotStorySkipConfirm", "CopilotStorySkip"]},
        "CopilotStorySkipConfirm": {"baseTask": "SkipBattlePlotConfirm",
                                    "template": "SkipThePreBattlePlotConfirm.png",
                                    "postDelay": 4000, "next": []},
    }


def _run_tasks(
    config: dict[str, Any],
    env: dict[str, str],
    log_path: Path,
    name: str,
    body: str,
    overlay: dict[str, Any],
    *,
    timeout: int = 900,
) -> tuple[bool, str]:
    """在隔离配置里跑一份不进战斗的任务文件；每个 `[[tasks]]` 都 Completed 才算成功。"""
    maa_bin = str(config.get("maa_bin", "/opt/homebrew/bin/maa"))
    labels = re.findall(r'^name = "([^"]+)"', body, flags=re.MULTILINE)
    try:
        with _isolated_config(env, {f"{name}.toml": body}, overlay_tasks=overlay) as run_env:
            result = subprocess.run(
                [maa_bin, "run", name, "--user-resource", "-v", "--batch"],
                capture_output=True, text=True, check=False, timeout=timeout, env=run_env,
            )
    except subprocess.TimeoutExpired:
        return False, f"{name} 超时（{timeout}s）"
    output = f"{result.stdout}\n{result.stderr}"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"# {name}\n{output}")
    done = all(
        re.search(rf"\[{re.escape(label)}\][^\n]*Completed", output) for label in labels
    )
    if result.returncode != 0 or not done:
        detail = " | ".join(output.strip().splitlines()[-3:])[-300:]
        return False, detail
    return True, ""


def _screen_text(config: dict[str, Any]) -> list[str]:
    """当前屏幕的 OCR 行（`ocrbox`：`文字\\t[x,y,w,h]`）。读不到返回空表。"""
    adb = str(config.get("adb_path", "/opt/homebrew/bin/adb"))
    address = str(config.get("adb_address", "127.0.0.1:5555"))
    ocrbox = SCRIPTS_DIR / "ocrbox"
    shot = RUNS_DIR / ".zone.png"
    if not ocrbox.is_file():
        return []
    try:
        with shot.open("wb") as handle:
            subprocess.run([adb, "-s", address, "exec-out", "screencap", "-p"],
                           stdout=handle, stderr=subprocess.DEVNULL, check=False, timeout=15)
        result = subprocess.run([str(ocrbox), str(shot)], capture_output=True,
                                text=True, check=False, timeout=15)
        return result.stdout.splitlines()
    except Exception:
        return []
    finally:
        try:
            shot.unlink()
        except OSError:
            pass


def _adb(config: dict[str, Any], *args: str) -> None:
    adb = str(config.get("adb_path", "/opt/homebrew/bin/adb"))
    address = str(config.get("adb_address", "127.0.0.1:5555"))
    subprocess.run([adb, "-s", address, "shell", *args],
                   capture_output=True, check=False, timeout=15)


class ActivityNavigator:
    """一轮活动关的「站位」：保证每段开始前人站在目标区域的地图上。

    `zone` 记着现在站在哪个区域的地图上；`None` = 不知道（刚开机、刚打完一张列表、
    刚失败过），这时从主界面重新走一遍锚点导航。
    """

    def __init__(self, config, env, log_path, layout, anchor, anchor_nexts, home_zone):
        self.config, self.env, self.log_path = config, env, log_path
        self.layout = layout
        self.anchor, self.anchor_nexts, self.home_zone = anchor, anchor_nexts, home_zone
        self.zone: str | None = None

    def _codes(self, zone_id: str) -> list[str]:
        return maa_clearing.zone_codes(self.layout, zone_id)

    def to_map(self) -> tuple[bool, str]:
        """方案 B：借锚点的上游链进活动、点进普通区域，把点关那一步换成只识别。"""
        overlay = _zone_ready_overlay(self._codes(self.home_zone))
        for name in self.anchor_nexts:
            overlay[name] = _recognize_only("SideStoryStage", self._codes(self.home_zone))
        body = MAP_TOML_TEMPLATE.format(
            home_names=json.dumps(HOME_TASK_NAMES, ensure_ascii=False),
            anchor=json.dumps(self.anchor, ensure_ascii=False),
            ready=ZONE_READY_TASK,
        )
        ok, detail = _run_tasks(self.config, self.env, self.log_path,
                                "copilot-map", body, overlay)
        self.zone = self.home_zone if ok else None
        return ok, "" if ok else f"没能停在活动地图上（锚点 {self.anchor}）：{detail}"

    def _enter_zone(self, zone_id: str) -> tuple[bool, str]:
        """在当前屏幕上按区域名点入口；当前屏没有就退一步再找一次。⚠️ 未经真机验证。"""
        name = maa_gamedata.zone_name(self.layout, zone_id)
        act = maa_gamedata.act_id_of(zone_id)
        others = [
            maa_gamedata.zone_name(self.layout, other)
            for other in (self.layout.get("zones") or {})
            if other != zone_id and maa_gamedata.act_id_of(other) == act
        ]
        if not name:
            return False, f"游戏数据里没有区域 {zone_id} 的名字"
        for attempt in range(2):
            point = maa_clearing.zone_button(_screen_text(self.config), name, others)
            if point is not None:
                _adb(self.config, "input", "tap", str(point[0]), str(point[1]))
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(f"# zone-entry {zone_id} {name} tap {point}\n")
                time.sleep(5)
                ok, detail = _run_tasks(
                    self.config, self.env, self.log_path, "copilot-zone",
                    ZONE_CHECK_TOML_TEMPLATE.format(ready=ZONE_READY_TASK),
                    _zone_ready_overlay(self._codes(zone_id)), timeout=120,
                )
                if ok:
                    return True, ""
                return False, f"点了「{name}」入口，但没认出这个区域的关卡：{detail}"
            if attempt == 0:
                _adb(self.config, "input", "keyevent", "4")
                time.sleep(3)
        return False, f"屏幕上没认出区域入口「{name}」，没有乱点"

    def ensure(self, zone_id: str) -> tuple[bool, str]:
        if self.zone == zone_id:
            return True, ""
        if self.zone is None:
            ok, detail = self.to_map()
            if not ok:
                return False, detail
        if self.zone == zone_id:
            return True, ""
        ok, detail = self._enter_zone(zone_id)
        self.zone = zone_id if ok else None
        return ok, detail

    def story(self, segment: dict[str, Any]) -> tuple[bool, str]:
        code = str(segment.get("code") or "")
        overlay = {**_story_overlay(code), **_zone_ready_overlay(self._codes(segment["zone_id"]))}
        ok, detail = _run_tasks(self.config, self.env, self.log_path, "copilot-story",
                                STORY_TOML_TEMPLATE.format(ready=ZONE_READY_TASK), overlay)
        if not ok:
            self.zone = None
            return False, f"剧情关 {code} 没能点开并跳过：{detail}"
        return True, ""


def _prepare_list_file(candidate: dict[str, Any], stage_id: str, target: Path) -> str:
    """下载完整作业、把关卡改成 stageId、逐份预检后写到 `target`。返回问题说明（空 = 好）。"""
    cand_id = int(candidate.get("id") or 0)
    try:
        payload = maa_copilot.http_get_json(
            f"{maa_copilot.PRTS_BASE}/copilot/get/{cand_id}", timeout=30
        )
    except Exception as exc:  # noqa: BLE001
        return f"作业 {cand_id} 下载失败：{type(exc).__name__}: {exc}"
    inner = payload.get("data") if isinstance(payload, dict) else None
    content = inner.get("content") if isinstance(inner, dict) else None
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            content = None
    if isinstance(content, dict):
        content["stage_name"] = stage_id
    problem = maa_clearing.job_file_problem(content, stage_id)
    if problem:
        return f"作业 {cand_id}：{problem}"
    atomic_write_json(target, content)
    return ""


def _run_copilot_list(
    config: dict[str, Any],
    env: dict[str, str],
    log_path: Path,
    items: list[tuple[dict[str, Any], Path]],
) -> list[tuple[str, str]]:
    """一张多作业列表：返回每项的 `(结论, 明细)`，与 `items` 一一对应。"""
    maa_bin = str(config.get("maa_bin", "/opt/homebrew/bin/maa"))
    lines = [
        "  { filename = %s, stage_name = %s, is_raid = %s },"
        % (
            json.dumps(str(path), ensure_ascii=False),
            json.dumps(str(entry.get("stage") or ""), ensure_ascii=False),
            "true" if entry.get("mode") == "raid" else "false",
        )
        for entry, path in items
    ]
    body = LIST_TOML_TEMPLATE.format(items="\n".join(lines))
    per_item = max(300, int(config.get("copilot_list_item_timeout", LIST_ITEM_TIMEOUT)))
    with _isolated_config(env, {"copilot-list.toml": body}, no_battle=False) as list_env:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"# {now_beijing().isoformat()} copilot-list "
                         + " ".join(maa_clearing.make_entry(str(e.get("stage")), str(e.get("mode")))
                                    for e, _p in items) + "\n")
            handle.flush()
            offset = os.fstat(handle.fileno()).st_size
            return_code, _taps = _run_copilot_with_watchdog(
                config,
                [maa_bin, "run", "copilot-list", "--user-resource", "-vvv", "--batch"],
                handle, log_path, list_env,
                timeout=per_item * len(items) + 300,
                stall=(maa_clearing.LIST_ITEM_EVENT, per_item),
            )
    output = _log_since(log_path, offset)
    return maa_clearing.list_outcomes([str(path) for _e, path in items], output, return_code)


def _run_activity_round(
    config: dict[str, Any],
    state: dict[str, Any],
    entries: list[dict[str, Any]],
    env: dict[str, str],
    log_path: Path,
    ledger: dict[str, Any],
    ledger_file: Path,
    results: list[dict[str, Any]],
    auto_round: bool,
) -> str:
    """当期活动的一轮：分段执行，返回停下的原因（空串 = 全部打完）。"""
    prefix = maa_copilot._activity_prefix(str(state.get("activity_key") or ""))
    layout = maa_gamedata.load_layout()
    anchor, anchor_nexts, anchor_error = maa_clearing.pick_anchor(
        maa_copilot._resource_dirs(config), prefix
    )

    def skip(entry: dict[str, Any], detail: str) -> None:
        results.append({"stage": entry.get("stage"), "mode": entry.get("mode"),
                        "outcome": "skipped", "detail": detail})

    def record(entry: dict[str, Any], verdict: str, detail: str) -> None:
        maa_copilot.record_copilot_attempt(
            ledger, str(entry.get("stage") or ""), str(entry.get("mode") or "normal"),
            outcome=verdict, op=int((entry.get("candidate") or {}).get("id") or 0),
            detail=detail, log_name=log_path.name,
        )
        maa_copilot.save_ledger(ledger, ledger_file)
        results.append({"stage": entry.get("stage"), "mode": entry.get("mode"),
                        "outcome": verdict, "detail": detail})

    raid_on = maa_clearing.raid_enabled(config)
    runnable = []
    for entry in entries:
        if entry.get("mode") == "raid" and not raid_on:
            skip(entry, RAID_DISABLED_NOTICE)
        else:
            runnable.append(entry)
    anchor_stage = maa_copilot.resolve_copilot_stage(anchor, maa_copilot.load_stage_index(config))
    anchor_info = maa_gamedata.stage_info(layout, anchor_stage[0]) if anchor_stage else None
    if not anchor or anchor_info is None:
        reason = anchor_error or f"游戏数据里没有锚点 {anchor}"
        for entry in runnable:
            skip(entry, reason)
        return reason

    cleared = maa_copilot.ledger_cleared_stage_ids(ledger, maa_copilot.load_stage_index(config))
    cleared |= maa_gamedata.skland_cleared(
        layout, maa_copilot.load_roster(), maa_gamedata.act_id_of(anchor_stage[0])
    )
    segments, dropped = maa_clearing.plan_segments(runnable, layout, cleared)
    navigator = ActivityNavigator(config, env, log_path, layout, anchor, anchor_nexts,
                                  str(anchor_info.get("zone_id") or ""))
    stop_reason = ""
    for index, segment in enumerate(segments):
        pending = [item for later in segments[index:] if later["kind"] == "copilot"
                   for item in later["items"]]
        if auto_round and maa_copilot.auto_pause_state():
            stop_reason = "自动作业已暂停"
        else:
            stop_reason = maa_action.min_lead_reason(config)
        if not stop_reason:
            ok, detail = navigator.ensure(str(segment["zone_id"]))
            if ok and segment["kind"] == "story":
                ok, detail = navigator.story(segment)
            if not ok:
                stop_reason = detail
                if pending:
                    # 导航坏了按「第一关失败」记账：计入当日上限，防止每 tick 重来一遍。
                    record(pending[0], "failed", detail[:400])
                    pending = pending[1:]
        if stop_reason:
            for entry in pending:
                skip(entry, "本轮已停止")
            break
        if segment["kind"] != "copilot":
            continue
        items: list[tuple[dict[str, Any], Path]] = []
        for position, entry in enumerate(segment["items"]):
            target = maa_copilot.PREPARED_DIR / f"list-{index}-{position}-{entry.get('stage_id')}.json"
            problem = _prepare_list_file(entry.get("candidate") or {},
                                         str(entry.get("stage_id") or ""), target)
            if problem:
                # 坏文件会让整张表跑不起来；它后面的关首通时也还锁着，一起退回。
                for rest in segment["items"][position:]:
                    skip(rest, problem if rest is entry else "前一份作业文件无效")
                stop_reason = problem
                break
            items.append((entry, target))
        if items:
            total = sum(int(entry.get("sanity_cost") or 0) for entry, _p in items)
            ok, detail = _recheck_before_stage(
                config, state, {"stage": items[0][0].get("stage"), "sanity_cost": total}
            )
            if not ok:
                for entry, _path in items:
                    skip(entry, detail)
                stop_reason = detail
            else:
                outcomes = _run_copilot_list(config, env, log_path, items)
                navigator.zone = None
                settled, settle_note = settle_after_battle(
                    config, str(items[-1][0].get("stage") or ""), env, log_path
                )
                for (entry, _path), (verdict, detail) in zip(items, outcomes):
                    if verdict == "skipped":
                        skip(entry, detail)
                        continue
                    if verdict != "won" and not settled:
                        detail = " · ".join(p for p in (detail, settle_note) if p)[:400]
                    record(entry, verdict, detail)
                failed = next((d for v, d in outcomes if v != "won"), None)
                if failed is not None:
                    stop_reason = failed or "有一份作业没过"
        if stop_reason:
            for later in segments[index + 1:]:
                for entry in later.get("items") or []:
                    skip(entry, "本轮已停止")
            break
    for entry, reason in dropped:
        skip(entry, reason)
    if dropped and not stop_reason:
        stop_reason = dropped[0][1]
    return stop_reason


def _log_since(log_path: Path, offset: int) -> str:
    """从 `offset` 字节读到文件末尾——也就是「这一关自己的那段输出」。

    按**字节偏移**切而不是按标记字符串切：偏移是在标记落盘之后取的，
    子进程之后往同一个 fd 追加的每一个字节都在它后面，不依赖标记有没有
    刷出来、也不怕同一个标记在日志里出现两次。
    """
    try:
        with log_path.open("rb") as handle:
            handle.seek(max(0, int(offset)))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _fight_one(
    config: dict[str, Any],
    entry: dict[str, Any],
    env: dict[str, str],
    log_path: Path,
) -> tuple[str, str]:
    """打一关一种模式，返回 `(结论, 明细)`。

    结论 ∈ {`"won"`, `"nav"`, `"failed"`, `"unknown"`}：`nav` 是导航 / 切难度失败，
    说明环境坏了，调用方停止整轮；其余两种只影响这一关，下一关照打。
    """
    stage = str(entry.get("stage") or "")
    stage_id = str(entry.get("stage_id") or "")
    mode = str(entry.get("mode") or "normal")
    candidate = entry.get("candidate") or {}
    maa_bin = str(config.get("maa_bin", "/opt/homebrew/bin/maa"))

    uri, _rewritten = _prepare_op_file(candidate, stage_id, config)
    navigated, detail = _navigate_to_stage(config, stage, env, log_path)
    if not navigated:
        return "nav", detail
    if mode == "raid":
        switched, switch_detail = switch_to_raid(config, stage, env, log_path)
        if not switched:
            return "nav", switch_detail

    # `maa copilot --help`：normal / raid / both。**三个降级开关（忽略练度要求 /
    # 吃理智药 / 用助战）一个都不许出现在这条 argv 上**（D12 / D13，不变量 #15）。
    # 它们连字面量都不许写进脚本，`CopilotDowngradeGuardTests` 全库 grep 守着。
    command = [maa_bin, "copilot", uri, "--formation", "--batch", "-v", "--raid", mode]
    marker = f"# {now_beijing().isoformat()} copilot {stage} mode={mode} "
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{marker}id={candidate.get('id')}\n")
        # **必须 flush 再把句柄交给子进程**（2026-09-09 线上缺陷）：不 flush 的话
        # 标记还躺在 Python 的缓冲区里，子进程直接往同一个 fd 写，输出先落盘、
        # 标记要等 `with` 退出才刷出来——排在这一关全部输出**之后**。于是
        # 「本关输出」切出来是空的，`copilot_outcome()` 一律返回 `unknown`，
        # 通关永远记不上（`runs/2026-09-09-copilot-fda56c1811ba.log` 第 42 行）。
        handle.flush()
        offset = os.fstat(handle.fileno()).st_size
        return_code, _taps = _run_copilot_with_watchdog(
            config, command, handle, log_path, env
        )
    this_run = _log_since(log_path, offset)
    outcome, detail = maa_clearing.copilot_outcome(this_run, return_code)
    if outcome == "won":
        return "won", ""
    summary = _copilot_summary(this_run.splitlines(), ok=False)
    return outcome, f"{detail}{(' · ' + summary) if summary else ''}"[:400]


def _post_report(
    config: dict[str, Any], embed: dict[str, Any], png_path: Path | None
) -> None:
    """战报：有截图就内嵌上传，上传失败退回纯 embed（截图不该带走战报）。"""
    token_text = discord_token()
    channel_id = str(config.get("channel_id", ""))
    has_shot = bool(png_path and png_path.is_file() and png_path.stat().st_size > 0)
    payload: dict[str, Any] = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    if has_shot:
        embed["image"] = {"url": "attachment://shot.png"}
        payload["attachments"] = [{"id": 0, "filename": "shot.png"}]
        from maa_common import discord_upload

        try:
            discord_upload(token_text, channel_id, payload, png_path)
            return
        except Exception as exc:
            embed.pop("image", None)
            payload.pop("attachments", None)
            embed["fields"].append(
                {"name": "图片上传", "value": f"内嵌上传失败：{type(exc).__name__}"}
            )
    try:
        discord_request(
            token_text, "POST", f"/channels/{channel_id}/messages", payload
        )
    except Exception as exc:
        print(f"copilot report failed: {type(exc).__name__}: {exc}", flush=True)


def main(argv: list[str]) -> int:
    if argv[:1] == ["search"] and len(argv) == 2:
        return cmd_search(argv[1])
    if argv[:1] == ["run-batch"] and len(argv) == 2:
        code = cmd_run_batch(argv[1])
        # Force-exit: a lingering interpreter (threads/fds) once held run.lock
        # for 10 minutes after a successful run, silently blocking the next
        # copilot.  flock dies with the process, so this is safe.
        sys.stdout.flush()
        os._exit(code)
    print(
        "用法: maa_copilot_worker.py search <token> | run-batch <token>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
