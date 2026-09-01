#!/usr/bin/env python3
"""Background worker for the copilot (抄作业) flow.

Subcommands:
  search <token>   — prts search + match + candidate card (network, 5-20s)
  run <token> <n> <mode> — real execution: emulator -> maa startup ->
                     maa copilot -> screenshot -> cleanup -> report

Deliberately NOT in the Hermes button whitelist; only maa_action.py may
spawn this process.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import maa_copilot
from maa_daily import (
    capture_screenshot,
    close_everything,
    open_emulator,
    settle_android_runtime,
    verify_game_installed,
    wait_for_android,
)
from maa_common import (
    BUTTONS_PATH,
    LAST_RUN_PATH,
    MAA_CONFIG_DIR,
    RUNS_DIR,
    SCRIPTS_DIR,
    atomic_write_json,
    atomic_write_text,
    cleanup_run_files,
    discord_request,
    discord_token,
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

# Single-op `maa copilot` must start from the stage's formation screen.
# A Fight task with times=0 runs the full chain (incl. StageNavigationTask)
# but starts no battle, parking the game exactly there.  Rendered per run
# because maa-cli has no CLI stage override.
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
times = 0
series = 0
medicine = 0
stone = 0
server = "CN"
client_type = "Official"
"""

# The takeover popup appears when the agent-command (代理指挥) switch is on:
# "接管后，本次行动内将无法回到代理指挥模式 / 确认要接管作战吗？"
# MaaCore's copilot chain does not handle it; tap the confirm button.
# Calibrated on a 1920x1080 capture (2026-08-02): popup text line sits at
# [781,490,343,56] and the confirm button sits opposite the × (cancel) at
# about (1252,739) — i.e. text right edge +130, text bottom +171.
POPUP_MARK = "确认要接管作战"
POPUP_CONFIRM_X_OFFSET = 130
POPUP_CONFIRM_Y_OFFSET = 171


def _fail_search(token: str, message: str) -> None:
    with state_lock():
        state = load_json(maa_copilot.SEARCHES_DIR / f"{token}.json", {})
        if not isinstance(state, dict):
            state = {"token": token}
        if state.get("status") not in {"pending", "running"}:
            return
        state["status"] = "failed"
        state["error"] = message[:400]
        atomic_write_json(maa_copilot.SEARCHES_DIR / f"{token}.json", state)


def _claim_search(token: str) -> dict[str, Any] | None:
    """Atomically claim one still-pending search."""
    path = maa_copilot.SEARCHES_DIR / f"{token}.json"
    with state_lock():
        state = load_json(path, None)
        if not isinstance(state, dict) or state.get("status") != "pending":
            return None
        state["status"] = "running"
        state["search_started_at"] = now_beijing().isoformat()
        atomic_write_json(path, state)
        return state


def _roster_for_search(
    config: dict[str, Any], *, auto_confirm: bool
) -> tuple[dict[str, Any] | None, str]:
    """Automatic confirmation requires a fresh roster; manual search may degrade."""
    try:
        import maa_skland

        if auto_confirm or maa_skland.roster_needs_refresh(config):
            maa_skland.refresh_roster()
    except Exception as exc:
        if auto_confirm:
            raise RuntimeError(
                f"森空岛练度与理智刷新失败：{type(exc).__name__}: {exc}"
            ) from exc
        return maa_copilot.load_roster(), f"{type(exc).__name__}: {exc}"
    return maa_copilot.load_roster(), ""


def cmd_search(token: str) -> int:
    if not maa_copilot.SEARCH_TOKEN_RE.fullmatch(token or ""):
        print("invalid token", file=sys.stderr)
        return 2
    maa_copilot.ensure_private_dirs()

    # Serialize searches: a concurrent search is marked failed so the watcher
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
        posted_message_id = ""
        channel_id = ""
        try:
            config = load_config()
            # Expire old searches + their buttons under the shared lock.
            with state_lock():
                maa_copilot.prune_expired_searches()

            index = maa_copilot.load_stage_index(config)
            stage_id = str(state.get("stage_id") or "")
            code = str(state.get("stage") or "")
            if not stage_id or stage_id not in index.get("stages", {}):
                raise RuntimeError(f"关卡 {code} 已不在本地索引中")
            auto_confirm = bool(state.get("auto_confirm"))
            sanity_cost = maa_copilot.stage_sanity_cost(stage_id, code, config)
            if auto_confirm and sanity_cost is None:
                raise RuntimeError(f"{code} 的理智消耗尚未进入 MaaResource")
            roster, roster_refresh_error = _roster_for_search(
                config, auto_confirm=auto_confirm
            )
            if roster_refresh_error:
                print(
                    f"roster refresh skipped: {roster_refresh_error}",
                    flush=True,
                )
            roster_ok = roster is not None
            candidates = maa_copilot.search_copilots(
                stage_id,
                code,
                config,
                roster,
                # fetch_stage_copilots is currently hard-capped below 1,000
                # items.  Automatic mode must filter the complete evaluated
                # set before choosing Top-1, otherwise an eligible item after
                # an imperfect prefix would be missed.
                limit=1000 if auto_confirm else 3,
                use_cache=not auto_confirm,
            )
            if auto_confirm:
                candidates = [
                    candidate
                    for candidate in candidates
                    if candidate.get("ratio") == 1.0
                    and not candidate.get("gaps")
                    and not candidate.get("soft_gaps")
                ][:1]
            if not candidates:
                detail = "完全满足当前干员练度的作业" if auto_confirm else "可执行的作业"
                raise RuntimeError(f"{code} 暂时没有{detail}")

            sanity = maa_copilot.sanity_preflight(roster, sanity_cost)
            sanity_text = maa_copilot.sanity_status_text(sanity) if auto_confirm else ""
            latest = load_json(maa_copilot.SEARCHES_DIR / f"{token}.json", None)
            if (
                not isinstance(latest, dict)
                or latest.get("status") != "running"
                or latest.get("activity_key") != state.get("activity_key")
            ):
                return 0
            embed, components, entries = maa_copilot.build_candidate_card(
                token,
                candidates,
                roster_ok,
                code,
                roster_hint=maa_copilot.roster_age_hint(roster)
                if roster_ok
                else "",
                stage_id=stage_id,
                roster=roster,
                auto_confirm=auto_confirm,
                activity_title=str(state.get("activity_title") or ""),
                sanity_text=sanity_text,
            )
            channel_id = str(config.get("channel_id", ""))
            message = discord_request(
                discord_token(),
                "POST",
                f"/channels/{channel_id}/messages",
                {"embeds": [embed], "components": components,
                 "allowed_mentions": {"parse": []}},
            )
            message_id = str(message.get("id", "")) if isinstance(message, dict) else ""
            if not message_id.isdigit():
                raise RuntimeError("Discord 发卡响应缺少消息编号")
            posted_message_id = message_id

            committed = False
            with state_lock():
                latest = load_json(
                    maa_copilot.SEARCHES_DIR / f"{token}.json", None
                )
                registry = load_json(BUTTONS_PATH, {})
                if not isinstance(registry, dict):
                    raise RuntimeError("共享按钮登记表格式错误")
                if (
                    isinstance(latest, dict)
                    and latest.get("status") == "running"
                    and latest.get("activity_key") == state.get("activity_key")
                ):
                    maa_copilot.prune_search_buttons(token, registry)
                    registry.update(entries)
                    atomic_write_json(BUTTONS_PATH, registry)
                    latest["status"] = "done"
                    latest["candidates"] = candidates
                    latest["sanity_cost"] = sanity_cost
                    latest["sanity_checked_at"] = now_beijing().isoformat()
                    latest["message_id"] = message_id
                    latest["finished_at"] = now_beijing().isoformat()
                    atomic_write_json(
                        maa_copilot.SEARCHES_DIR / f"{token}.json", latest
                    )
                    committed = True
            if not committed:
                try:
                    discord_request(
                        discord_token(),
                        "PATCH",
                        f"/channels/{channel_id}/messages/{message_id}",
                        {"components": []},
                    )
                except Exception as retire_exc:
                    print(
                        "superseded candidate card could not be retired: "
                        f"{type(retire_exc).__name__}: {retire_exc}",
                        flush=True,
                    )
                return 0
        except Exception as exc:
            _fail_search(token, f"{type(exc).__name__}: {exc}")
            if posted_message_id:
                try:
                    retire_discord_components(config, posted_message_id)
                except Exception as retire_exc:
                    print(
                        "failed candidate card could not be retired: "
                        f"{type(retire_exc).__name__}: {retire_exc}",
                        flush=True,
                    )
            try:
                if state.get("auto_confirm"):
                    return 1
                discord_request(
                    discord_token(),
                    "POST",
                    f"/channels/{config.get('channel_id', '')}/messages",
                    {
                        "embeds": [
                            {
                                "title": f"❌ {state.get('stage', '')} 作业匹配失败",
                                "description": str(exc)[:1500],
                                "color": 0xED4245,
                                "timestamp": now_beijing().isoformat(),
                            }
                        ],
                        "allowed_mentions": {"parse": []},
                    },
                )
            except Exception:
                pass
            return 1
    return 0


# ---------------------------------------------------------------------------
# Execution (C3)
# ---------------------------------------------------------------------------

def _prepare_op_file(candidate: dict[str, Any], stage_id: str, config: dict[str, Any]) -> tuple[str, bool]:
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


def _copilot_summary(lines: list[str], *, ok: bool, mode: str) -> str:
    """Chinese summary of a maa copilot run (regexes tuned on real v6 logs)."""
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
    if ok:
        parts.append("🌟 总体：✅ 抄作业完成")
    else:
        parts.append("🌟 总体：❌ 抄作业失败（详见下方）")
    if missions:
        parts.append(f"⚔️ 进入战斗 ×{missions}")
    if deployments:
        parts.append("部署：" + "、".join(deployments[:6]))
    if not ok:
        for line in lines:
            text = str(line).strip()
            if re.search(r"(Copilot Error|Error: Some error)", text):
                parts.append(f"❌ {text[:100]}")
                break
    if len(parts) == 1:
        parts.append("（未能从 CLI 输出解析出明细，日志已留存）")
    mode_label = "突袭" if mode == "raid" else "普通"
    parts.append(f"模式：{mode_label} · 碎石 0")
    return "\n".join(parts[:12])


def cmd_run(token: str, number: str, mode: str) -> int:
    if not maa_copilot.SEARCH_TOKEN_RE.fullmatch(token or ""):
        return 2
    if mode not in {"normal", "raid"}:
        return 2
    try:
        index = int(number)
    except (TypeError, ValueError):
        return 2
    state = load_json(maa_copilot.SEARCHES_DIR / f"{token}.json", None)
    if not isinstance(state, dict):
        return 2
    if state.get("status") != "queued":
        return 2
    if state.get("requested_index") != index or state.get("requested_mode") != mode:
        return 2
    candidates = state.get("candidates", []) or []
    if not isinstance(candidates, list) or not 1 <= index <= len(candidates):
        return 2
    candidate = candidates[index - 1]
    if not isinstance(candidate, dict):
        return 2

    with run_lock(blocking=False) as acquired:
        if not acquired:
            print("copilot run SKIPPED: run.lock 被占用（另一场 MAA 正在运行）")
            message = "另一场 MAA 正在运行，本次未启动；原确认按钮仍可稍后重试。"
            restored = _restore_ready(token, {"queued"}, message)
            if restored:
                try:
                    _post_not_started(load_config(), state, message)
                except Exception as exc:
                    print(
                        f"copilot busy notification failed: {type(exc).__name__}: {exc}",
                        flush=True,
                    )
            return 1
        return _run_locked(state, candidate, index, mode)


def _run_copilot_with_watchdog(
    config: dict[str, Any],
    command: list[str],
    log_handle: Any,
    log_path: Path,
    env: dict[str, str],
) -> tuple[int, int]:
    """Run maa copilot; watchdog taps the takeover popup if it appears.

    The popup blocks MaaCore's battle recognition, so without handling it
    every run on a 代理指挥-enabled stage fails.  Poll the screen with the
    local OCR binary (deployed as ~/.hermes/scripts/ocrbox) and tap the
    confirm button when the exact popup text is seen.
    """
    adb = str(config.get("adb_path", "/opt/homebrew/bin/adb"))
    address = str(config.get("adb_address", "127.0.0.1:5555"))
    ocrbox = SCRIPTS_DIR / "ocrbox"
    watch_png = RUNS_DIR / ".watch.png"
    timeout = int(config.get("maa_run_timeout", 3000))
    proc = subprocess.Popen(
        command,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        env=env,
    )
    taps = 0
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write("maa copilot 超时\n")
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


def _require_auto_run_preflight(
    state: dict[str, Any], candidate: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any] | None:
    """Recheck activity availability and live AP before touching the emulator."""
    if not state.get("auto_confirm"):
        return None
    if (
        candidate.get("ratio") != 1.0
        or candidate.get("gaps")
        or candidate.get("soft_gaps")
    ):
        raise RuntimeError("自动作业不再完全满足当前记录的干员要求，拒绝执行")
    stage = str(state.get("stage") or "")
    from maa_plan_card import activity_target_is_current, query_activity_snapshot

    live = query_activity_snapshot(config)
    if not activity_target_is_current(
        live, stage, str(state.get("activity_key") or "")
    ):
        raise maa_copilot.StaleActivityError(
            f"{stage} 已不属于当前自动活动目标，拒绝执行旧作业"
        )
    try:
        cost = int(state.get("sanity_cost") or 0)
    except (TypeError, ValueError):
        cost = 0
    if cost <= 0:
        raise RuntimeError(f"无法确认 {stage} 的理智消耗，拒绝启动")
    try:
        import maa_skland

        roster = maa_skland.refresh_roster()
    except Exception as exc:
        raise RuntimeError(
            f"森空岛实时理智读取失败，未启动模拟器：{type(exc).__name__}: {exc}"
        ) from exc
    preflight = maa_copilot.sanity_preflight(roster, cost)
    if not preflight.get("known"):
        raise RuntimeError(
            f"森空岛实时理智读取失败，未启动模拟器：{preflight.get('reason', '未知原因')}"
        )
    if not preflight.get("enough"):
        raise RuntimeError(maa_copilot.sanity_status_text(preflight))
    return preflight


def _candidate_still_matches(
    state: dict[str, Any], candidate: dict[str, Any], index: int, mode: str
) -> bool:
    candidates = state.get("candidates") or []
    return bool(
        state.get("requested_index") == index
        and state.get("requested_mode") == mode
        and isinstance(candidates, list)
        and 1 <= index <= len(candidates)
        and isinstance(candidates[index - 1], dict)
        and candidates[index - 1].get("id") == candidate.get("id")
    )


def _begin_preparation(
    state: dict[str, Any], candidate: dict[str, Any], index: int, mode: str
) -> dict[str, Any]:
    token = str(state.get("token") or "")
    path = maa_copilot.SEARCHES_DIR / f"{token}.json"
    with state_lock():
        fresh = load_json(path, None)
        if (
            not isinstance(fresh, dict)
            or fresh.get("status") != "queued"
            or not _candidate_still_matches(fresh, candidate, index, mode)
        ):
            raise RuntimeError("作业确认已取消、失效或被另一进程接管")
        fresh["status"] = "preparing"
        fresh["preparation_started_at"] = now_beijing().isoformat()
        fresh.pop("last_prepare_error", None)
        atomic_write_json(path, fresh)
        return fresh


def _begin_execution(
    state: dict[str, Any], candidate: dict[str, Any], index: int, mode: str
) -> dict[str, Any]:
    token = str(state.get("token") or "")
    path = maa_copilot.SEARCHES_DIR / f"{token}.json"
    with state_lock():
        fresh = load_json(path, None)
        if (
            not isinstance(fresh, dict)
            or fresh.get("status") != "preparing"
            or not _candidate_still_matches(fresh, candidate, index, mode)
        ):
            raise RuntimeError("活动或作业确认在启动前已经失效")
        fresh["status"] = "executing"
        fresh["execution_started_at"] = now_beijing().isoformat()
        atomic_write_json(path, fresh)
        return fresh


def _restore_ready(token: str, expected: set[str], message: str) -> bool:
    path = maa_copilot.SEARCHES_DIR / f"{token}.json"
    with state_lock():
        fresh = load_json(path, None)
        if not isinstance(fresh, dict) or fresh.get("status") not in expected:
            return False
        fresh["status"] = "done"
        fresh["last_prepare_error"] = message[:500]
        fresh["last_prepare_failed_at"] = now_beijing().isoformat()
        for key in (
            "requested_index",
            "requested_mode",
            "execution_requested_at",
            "preparation_started_at",
            "execution_started_at",
        ):
            fresh.pop(key, None)
        atomic_write_json(path, fresh)
        return True


def _invalidate_prebattle_activity(
    token: str, config: dict[str, Any]
) -> bool:
    """Invalidate a stale automatic run while battle consumption is impossible."""
    path = maa_copilot.SEARCHES_DIR / f"{token}.json"
    with state_lock():
        fresh = load_json(path, None)
        if not isinstance(fresh, dict) or fresh.get("status") not in {
            "queued",
            "preparing",
            "executing",
        }:
            return False
        registry = load_json(BUTTONS_PATH, {})
        if not isinstance(registry, dict):
            raise RuntimeError("共享按钮登记表格式错误")
        maa_copilot.prune_search_buttons(token, registry)
        atomic_write_json(BUTTONS_PATH, registry)
        fresh["status"] = "superseded"
        fresh["superseded_at"] = now_beijing().isoformat()
        message_id = fresh.get("message_id")
        atomic_write_json(path, fresh)
    try:
        retire_discord_components(config, message_id)
    except Exception as exc:
        print(
            f"stale candidate card retire failed: {type(exc).__name__}: {exc}",
            flush=True,
        )
    return True


def _retire_execution_controls(state: dict[str, Any], config: dict[str, Any]) -> None:
    token = str(state.get("token") or "")
    path = maa_copilot.SEARCHES_DIR / f"{token}.json"
    with state_lock():
        fresh = load_json(path, None)
        if not isinstance(fresh, dict) or fresh.get("status") != "executing":
            raise RuntimeError("执行状态已经失效，拒绝进入战斗")
        registry = load_json(BUTTONS_PATH, {})
        if not isinstance(registry, dict):
            raise RuntimeError("共享按钮登记表格式错误")
        maa_copilot.prune_search_buttons(token, registry)
        atomic_write_json(BUTTONS_PATH, registry)
        message_id = fresh.get("message_id")
    try:
        retire_discord_components(config, message_id)
    except Exception as exc:
        print(
            f"candidate card retire failed: {type(exc).__name__}: {exc}",
            flush=True,
        )


def _finish_execution(
    token: str, config: dict[str, Any], *, ok: bool
) -> dict[str, Any]:
    path = maa_copilot.SEARCHES_DIR / f"{token}.json"
    with state_lock():
        fresh = load_json(path, None)
        if not isinstance(fresh, dict) or fresh.get("status") != "executing":
            raise RuntimeError("执行终态写入时状态不一致")
        registry = load_json(BUTTONS_PATH, {})
        if not isinstance(registry, dict):
            raise RuntimeError("共享按钮登记表格式错误")
        maa_copilot.prune_search_buttons(token, registry)
        atomic_write_json(BUTTONS_PATH, registry)
        message_id = fresh.get("message_id")
        fresh["status"] = "executed" if ok else "execution_failed"
        timestamp_key = "executed_at" if ok else "execution_failed_at"
        fresh[timestamp_key] = now_beijing().isoformat()
        for key in ("requested_index", "requested_mode"):
            fresh.pop(key, None)
        atomic_write_json(path, fresh)
    try:
        retire_discord_components(config, message_id)
    except Exception as exc:
        print(
            f"terminal candidate card retire failed: {type(exc).__name__}: {exc}",
            flush=True,
        )
    return fresh


def _post_not_started(
    config: dict[str, Any], state: dict[str, Any], message: str
) -> None:
    stage = str(state.get("stage") or "")
    discord_request(
        discord_token(),
        "POST",
        f"/channels/{config.get('channel_id', '')}/messages",
        {
            "embeds": [
                {
                    "title": f"⛔ 未启动抄作业：{stage}",
                    "description": message[:1500],
                    "color": 0xED4245,
                    "timestamp": now_beijing().isoformat(),
                }
            ],
            "allowed_mentions": {"parse": []},
        },
    )


def _run_locked(state: dict[str, Any], candidate: dict[str, Any], index: int, mode: str) -> int:
    config = load_config()
    token = str(state.get("token") or "")
    stage_id = str(state.get("stage_id") or "")
    stage_label = str(state.get("stage") or "")
    cand_id = int(candidate.get("id") or 0)
    ratio = candidate.get("ratio")
    channel_id = str(config.get("channel_id", ""))
    token_text = discord_token()
    at = now_beijing()
    stem = f"{at.date().isoformat()}-copilot-{token}"
    log_path = RUNS_DIR / f"{stem}.log"
    png_path = RUNS_DIR / f"{stem}.png"
    started = time.monotonic()
    emulator_touched = False
    battle_touched = False

    try:
        _require_auto_run_preflight(state, candidate, config)
        state = _begin_preparation(state, candidate, index, mode)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, maa_copilot.StaleActivityError):
            _invalidate_prebattle_activity(token, config)
        else:
            _restore_ready(token, {"queued", "preparing"}, message)
        try:
            _post_not_started(config, state, message)
        except Exception as notify_exc:
            print(
                f"copilot preflight notification failed: "
                f"{type(notify_exc).__name__}: {notify_exc}",
                flush=True,
            )
        merge_last_run(
            {
                "status": "failed",
                "finished_at": now_beijing().isoformat(),
                "summary": f"❌ 抄作业未启动：{message}"[:500],
                "ok": False,
                "cleanup_errors": [],
            }
        )
        return 1

    try:
        atomic_write_json(
            RUNS_DIR / f"{stem}.json",
            {
                "date": at.date().isoformat(),
                "phase": f"copilot-{token}",
                "stage": stage_label,
                "stage_id": stage_id,
                "candidate_id": cand_id,
                "candidate_title": str(candidate.get("title") or "")[:120],
                "mode": mode,
                "ratio": ratio,
                "started_at": at.isoformat(),
                "status": "running",
            },
        )
        merge_last_run(
            {
                "status": "running",
                "phase": f"copilot-{token}",
                "stage": stage_label,
                "started_at": at.isoformat(),
                "log": str(log_path),
                "plan": {"stage": stage_label, "skip": False},
            }
        )

        # Resolve the op file before touching the emulator.
        uri, rewritten = _prepare_op_file(candidate, stage_id, config)

        try:
            require_executable(config, "maa_bin", "/opt/homebrew/bin/maa", "maa-cli")
            require_executable(config, "adb_path", "/opt/homebrew/bin/adb", "ADB")
        except RuntimeError as exc:
            raise RuntimeError(str(exc)) from exc

        discord_request(
            token_text,
            "POST",
            f"/channels/{channel_id}/messages",
            {
                "embeds": [
                    {
                        "title": f"▶️ 开始抄作业：{stage_label}"
                        f"（{'突袭' if mode == 'raid' else '普通'}）",
                        "description": (
                            f"作业：{candidate.get('title', '')}\n"
                            f"作者：{candidate.get('uploader', '')}\n"
                            f"{'已忽略作业练度要求' if ratio is not None and ratio < 1.0 else ''}"
                        )[:3500],
                        "color": 0x5865F2,
                        "timestamp": at.isoformat(),
                    }
                ],
                "allowed_mentions": {"parse": []},
            },
        )

        # Emulator -> game ready -> navigate to formation screen -> copilot
        state = _begin_execution(state, candidate, index, mode)
        emulator_touched = True
        open_emulator(config)
        wait_for_android(config)
        verify_game_installed(config)
        # 抄作业开的是同一台冷启模拟器，同样会撞上 MaaCore 那 3 秒的 MaaTouch 窗口
        # （DEV_GUIDE §11.14）。这里没有日常那层重试兜底，闸门更不能省。
        settle_android_runtime(config)

        maa_bin = str(config.get("maa_bin", "/opt/homebrew/bin/maa"))
        env = maa_subprocess_env(config)
        env["MAA_LOG_PREFIX"] = "Never"

        # Navigation task: StartUp + Fight(times=0) parks the game at the
        # formation screen; single-op copilot cannot navigate on its own.
        nav_path = MAA_CONFIG_DIR / "tasks" / "copilot-nav.toml"
        nav_toml = NAV_TOML_TEMPLATE.format(stage=stage_label)
        atomic_write_text(nav_path, nav_toml)
        nav_result = subprocess.run(
            [maa_bin, "run", "copilot-nav", "-v", "--batch"],
            capture_output=True,
            text=True,
            check=False,
            timeout=900,
            env=env,
        )
        nav_output = f"{nav_result.stdout}\n{nav_result.stderr}"
        with log_path.open("a", encoding="utf-8") as log_handle:
            log_handle.write(nav_output)
        if nav_result.returncode != 0 or "Fight Completed" not in nav_output:
            detail = nav_output.strip().splitlines()[-3:]
            raise RuntimeError(
                "导航到编队界面失败（关卡可能未解锁或不可达）："
                + (" | ".join(detail)[-500:] if detail else f"退出码 {nav_result.returncode}")
            )

        # The card can sit open while the emulator boots.  Recheck the live
        # activity and AP once more at the last safe point before a battle can
        # consume sanity.
        _require_auto_run_preflight(state, candidate, config)
        _retire_execution_controls(state, config)

        copilot_command = [
            maa_bin, "copilot", uri,
            "--formation", "--batch", "-v",
            "--raid", mode,
        ]
        if ratio is not None and ratio < 1.0:
            copilot_command.append("--ignore-requirements")
        battle_touched = True
        with log_path.open("a", encoding="utf-8") as log_handle:
            log_handle.write(
                f"# {now_beijing().isoformat()} copilot {stage_label} id={cand_id} "
                f"mode={mode} ignore={ratio is not None and ratio < 1.0}\n"
            )
            return_code, _taps = _run_copilot_with_watchdog(
                config, copilot_command, log_handle, log_path, env
            )

        # Screenshot (best-effort, before cleanup)
        screenshot_ok, screenshot_error = capture_screenshot(config, png_path)
        cleanup_errors = close_everything(config)
        duration = time.monotonic() - started

        raw_lines: list[str] = []
        try:
            raw_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            pass
        # Only the current run's section: everything after the last run marker.
        marker_index = -1
        for line_index in range(len(raw_lines) - 1, -1, -1):
            if raw_lines[line_index].startswith("# ") and "copilot" in raw_lines[line_index]:
                marker_index = line_index
                break
        if marker_index >= 0:
            raw_lines = raw_lines[marker_index + 1:]
        ok = return_code == 0 and not cleanup_errors
        summary = _copilot_summary(raw_lines, ok=ok, mode=mode)
        if ratio is not None and ratio < 1.0:
            summary += "\n（已忽略作业练度要求）"

        embed: dict[str, Any] = {
            "title": f"{'✅' if ok else '❌'} 抄作业：{stage_label}",
            "color": 0x57F287 if ok else 0xED4245,
            "fields": [
                {"name": "作业", "value": str(candidate.get("title") or "")[:100], "inline": True},
                {"name": "模式", "value": "突袭" if mode == "raid" else "普通", "inline": True},
                {"name": "总耗时", "value": f"{duration:.0f}s", "inline": True},
                {"name": "📋 结果", "value": summary[:1024], "inline": False},
            ],
            "footer": {"text": "模拟器与 ADB 已执行清理"},
            "timestamp": now_beijing().isoformat(),
        }
        if screenshot_ok:
            embed["image"] = {"url": "attachment://shot.png"}
        payload: dict[str, Any] = {
            "embeds": [embed],
            "allowed_mentions": {"parse": []},
        }
        if screenshot_ok:
            payload["attachments"] = [{"id": 0, "filename": "shot.png"}]
        if screenshot_ok:
            from maa_common import discord_upload

            try:
                discord_upload(token_text, channel_id, payload, png_path)
            except Exception as upload_exc:
                embed.pop("image", None)
                fields = list(embed.get("fields", []))
                fields.append(
                    {"name": "图片上传", "value": f"内嵌上传失败：{type(upload_exc).__name__}"}
                )
                embed["fields"] = fields
                discord_request(
                    token_text,
                    "POST",
                    f"/channels/{channel_id}/messages",
                    {"embeds": [embed], "allowed_mentions": {"parse": []}},
                )
        else:
            if screenshot_error:
                embed["fields"].append({"name": "截图", "value": screenshot_error[:200]})
            discord_request(
                token_text,
                "POST",
                f"/channels/{channel_id}/messages",
                payload,
            )

        merge_last_run(
            {
                "status": "success" if ok else "failed",
                "finished_at": now_beijing().isoformat(),
                "summary": summary,
                "ok": ok,
                "cleanup_errors": cleanup_errors,
            }
        )
        cleanup_run_files(int(config.get("retention_days", 14)))
        state = _finish_execution(token, config, ok=ok)
        # Diagnostic for the one observed hang: a successful run whose
        # interpreter lingered ~10 min holding run.lock.  Neither
        # discord_request nor discord_upload can block (both have timeouts);
        # if this ever re-appears the next run's output shows what was alive.
        threads = [t.name for t in threading.enumerate()]
        print(
            f"copilot run ok={ok} return_code={return_code} "
            f"cleanup_errors={cleanup_errors} threads={threads}",
            flush=True,
        )
        sys.stdout.flush()
        # Force-exit: a lingering interpreter (threads/fds) once held run.lock
        # for 10 minutes after a successful run, silently blocking the next
        # copilot.  flock dies with the process, so this is safe.
        os._exit(0 if ok else 1)
    except Exception as exc:
        print(f"copilot run EXCEPTION: {type(exc).__name__}: {exc}", flush=True)
        cleanup_errors = []
        if emulator_touched:
            try:
                cleanup_errors = close_everything(config)
            except Exception as cleanup_exc:
                cleanup_errors = [
                    f"清理异常：{type(cleanup_exc).__name__}: {cleanup_exc}"
                ]
        message = f"{type(exc).__name__}: {exc}"
        stale_activity = isinstance(exc, maa_copilot.StaleActivityError)
        safe_before_battle = (
            not stale_activity and not battle_touched and not cleanup_errors
        )
        retryable = False
        if stale_activity:
            _invalidate_prebattle_activity(token, config)
        elif safe_before_battle:
            retryable = _restore_ready(
                token, {"preparing", "executing"}, message
            )
        if not stale_activity and not safe_before_battle:
            try:
                state = _finish_execution(token, config, ok=False)
            except RuntimeError as finish_exc:
                print(
                    f"execution terminal state failed: {finish_exc}",
                    flush=True,
                )
        try:
            discord_request(
                token_text,
                "POST",
                f"/channels/{channel_id}/messages",
                {
                    "embeds": [
                        {
                            "title": (
                                f"⛔ 活动已切换，旧作业未执行：{stage_label}"
                                if stale_activity
                                else f"⛔ 抄作业未启动，可重试：{stage_label}"
                                if retryable
                                else f"⛔ 抄作业已取消或失效：{stage_label}"
                                if safe_before_battle
                                else f"❌ 抄作业失败：{stage_label}"
                            ),
                            "description": message[:1500],
                            "color": 0xED4245,
                            "timestamp": now_beijing().isoformat(),
                        }
                    ],
                    "allowed_mentions": {"parse": []},
                },
            )
        except Exception:
            pass
        merge_last_run(
            {
                "status": "failed",
                "finished_at": now_beijing().isoformat(),
                "summary": f"❌ 抄作业失败：{type(exc).__name__}: {exc}"[:500],
                "ok": False,
                "cleanup_errors": cleanup_errors,
            }
        )
        sys.stdout.flush()
        if battle_touched:
            os._exit(1)
        return 1


def main(argv: list[str]) -> int:
    if argv[:1] == ["search"] and len(argv) == 2:
        return cmd_search(argv[1])
    if argv[:1] == ["run"] and len(argv) == 4:
        return cmd_run(argv[1], argv[2], argv[3])
    print("用法: maa_copilot_worker.py search <token> | run <token> <n> <mode>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
