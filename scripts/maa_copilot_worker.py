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
    state = load_json(maa_copilot.SEARCHES_DIR / f"{token}.json", {})
    if not isinstance(state, dict):
        state = {"token": token}
    state["status"] = "failed"
    state["error"] = message[:400]
    atomic_write_json(maa_copilot.SEARCHES_DIR / f"{token}.json", state)


def cmd_search(token: str) -> int:
    if not maa_copilot.SEARCH_TOKEN_RE.fullmatch(token or ""):
        print("invalid token", file=sys.stderr)
        return 2
    maa_copilot.ensure_private_dirs()

    # Serialize searches: a concurrent search simply exits silently.
    with maa_copilot.SEARCH_LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        os.chmod(maa_copilot.SEARCH_LOCK_PATH, 0o600)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0

        state = load_json(maa_copilot.SEARCHES_DIR / f"{token}.json", None)
        if not isinstance(state, dict) or state.get("status") != "pending":
            return 0
        state["status"] = "running"
        atomic_write_json(maa_copilot.SEARCHES_DIR / f"{token}.json", state)

        config: dict[str, Any] = {}
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
            try:
                import maa_skland

                if maa_skland.roster_needs_refresh(config):
                    maa_skland.refresh_roster()
            except Exception as exc:
                print(
                    f"roster refresh skipped: {type(exc).__name__}: {exc}",
                    flush=True,
                )
            roster = maa_copilot.load_roster()
            roster_ok = roster is not None
            candidates = maa_copilot.search_copilots(
                stage_id, code, config, roster, limit=3
            )
            if not candidates:
                raise RuntimeError(f"{code} 暂时没有可执行的作业")

            embed, components, entries = maa_copilot.build_candidate_card(
                token,
                candidates,
                roster_ok,
                code,
                roster_hint=maa_copilot.roster_age_hint(roster)
                if roster_ok
                else "",
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

            with state_lock():
                registry = load_json(BUTTONS_PATH, {})
                if not isinstance(registry, dict):
                    raise RuntimeError("共享按钮登记表格式错误")
                maa_copilot.prune_search_buttons(token, registry)
                registry.update(entries)
                atomic_write_json(BUTTONS_PATH, registry)

            state["status"] = "done"
            state["candidates"] = candidates
            state["message_id"] = message_id
            state["finished_at"] = now_beijing().isoformat()
            atomic_write_json(maa_copilot.SEARCHES_DIR / f"{token}.json", state)
        except Exception as exc:
            _fail_search(token, f"{type(exc).__name__}: {exc}")
            try:
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
    candidates = state.get("candidates", []) or []
    if not isinstance(candidates, list) or not 1 <= index <= len(candidates):
        return 2
    candidate = candidates[index - 1]
    if not isinstance(candidate, dict):
        return 2

    with run_lock(blocking=False) as acquired:
        if not acquired:
            print("copilot run SKIPPED: run.lock 被占用（另一场 MAA 正在运行）")
            return 0
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
        open_emulator(config)
        wait_for_android(config)
        verify_game_installed(config)

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

        copilot_command = [
            maa_bin, "copilot", uri,
            "--formation", "--batch", "-v",
            "--raid", mode,
        ]
        if ratio is not None and ratio < 1.0:
            copilot_command.append("--ignore-requirements")
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
        try:
            cleanup_errors = close_everything(config)
        except Exception:
            pass
        try:
            discord_request(
                token_text,
                "POST",
                f"/channels/{channel_id}/messages",
                {
                    "embeds": [
                        {
                            "title": f"❌ 抄作业启动失败：{stage_label}",
                            "description": f"{type(exc).__name__}: {exc}"[:1500],
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
        os._exit(1)


def main(argv: list[str]) -> int:
    if argv[:1] == ["search"] and len(argv) == 2:
        return cmd_search(argv[1])
    if argv[:1] == ["run"] and len(argv) == 4:
        return cmd_run(argv[1], argv[2], argv[3])
    print("用法: maa_copilot_worker.py search <token> | run <token> <n> <mode>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
