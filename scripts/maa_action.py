#!/usr/bin/env python3
"""The only permitted mutation and dispatch entry point for the MAA plan."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from maa_common import (
    BEIJING,
    CONFIG_PATH,
    LAST_RUN_PATH,
    PLAN_PATH,
    RUNS_DIR,
    SCRIPTS_DIR,
    atomic_write_json,
    effective_plan,
    is_run_locked,
    load_config,
    load_json,
    maa_subprocess_env,
    medicine_label,
    now_beijing,
    plan_stage_label,
    require_executable,
    state_lock,
)


STAGE_PATTERN = re.compile(r"^[A-Za-z0-9]{1,8}-\d{1,2}$")
ACTIVITY_STAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,30}-\d{1,2}$")


def emit(ok: bool, message: str) -> None:
    # Persistent button runner accepts at most 8192 bytes and parses only JSON.
    raw = json.dumps({"ok": ok, "message": message}, ensure_ascii=False)
    if len(raw.encode("utf-8")) > 7900:
        raw = json.dumps(
            {"ok": ok, "message": message[:1800] + "\n…（已截断）"},
            ensure_ascii=False,
        )
    print(raw)


def _activity_output_allows(stage: str, config: dict[str, Any]) -> bool:
    configured = config.get("activity_stages", [])
    if isinstance(configured, list) and stage in {str(item) for item in configured}:
        return True
    if not ACTIVITY_STAGE_PATTERN.fullmatch(stage):
        return False
    maa_bin = str(config.get("maa_bin", "/opt/homebrew/bin/maa"))
    try:
        result = subprocess.run(
            [maa_bin, "activity", "Official"],
            capture_output=True,
            text=True,
            timeout=0.7,
            check=False,
            env=maa_subprocess_env(config),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    output = f"{result.stdout}\n{result.stderr}"
    token = re.compile(
        rf"(?<![A-Za-z0-9_-]){re.escape(stage)}(?![A-Za-z0-9_-])"
    )
    return result.returncode == 0 and bool(token.search(output))


def valid_stage(stage: str, config: dict[str, Any]) -> bool:
    return stage == "" or bool(STAGE_PATTERN.fullmatch(stage)) or _activity_output_allows(stage, config)


def _today_override() -> dict[str, Any]:
    now = now_beijing()
    current = load_json(PLAN_PATH, {})
    if not isinstance(current, dict) or current.get("date") != now.date().isoformat():
        current = {"date": now.date().isoformat()}
    # Discard all fields outside the script-level allowlist.
    return {
        key: current[key]
        for key in ("date", "stage", "skip", "source", "updated_at")
        if key in current
    }


def _save_override(key: str, value: Any) -> dict[str, Any]:
    with state_lock():
        plan = _today_override()
        plan[key] = value
        plan["source"] = "discord"
        plan["updated_at"] = now_beijing().isoformat()
        # There is deliberately no stone field in this write path.
        atomic_write_json(PLAN_PATH, plan)
    return effective_plan()


def _running_context() -> tuple[str, str]:
    state = load_json(LAST_RUN_PATH, {})
    if not isinstance(state, dict) or state.get("status") != "running":
        return "本场", "上次的本"
    phase = str(state.get("phase", "now"))
    phase_label = {"am": "早场", "pm": "晚场", "now": "手动场"}.get(phase, "本场")
    plan = state.get("plan", {})
    stage = plan_stage_label(plan if isinstance(plan, dict) else {})
    return phase_label, stage


def _confirmation(plan: dict[str, Any], was_running: bool) -> str:
    now = now_beijing()
    stage = plan_stage_label(plan)
    medicine = medicine_label(str(plan.get("medicine", "none")))
    skip = bool(plan.get("skip"))
    if was_running:
        phase, running_stage = _running_context()
        if phase == "早场":
            target = "晚 10 点那场会跳过" if skip else f"晚 10 点那场刷 {stage}（{medicine}）"
        elif phase == "晚场":
            target = "今天没有后续自动场次，本次覆盖明天会自动失效"
        else:
            target = "下一场仍在今天时会采用新计划"
        return f"{phase}正在跑 {running_stage}，不打断它；{target}。"
    if skip:
        return f"今天（{now.month}/{now.day}）的自动场次已设为跳过。"
    return (
        f"今天（{now.month}/{now.day}）刷 {stage}，{medicine}；"
        "早 8 点和晚 10 点各跑一次。"
    )


def set_stage(stage: str) -> tuple[bool, str]:
    config = load_config()
    if not valid_stage(stage, config):
        return (
            False,
            "关卡不在白名单：只接受常规关卡格式（如 CE-6、1-7）、空串（上次的本）"
            "或当前活动关。碎石设置永远不受支持。",
        )
    was_running = is_run_locked()
    plan = _save_override("stage", stage)
    return True, _confirmation(plan, was_running)


def set_medicine(value: str) -> tuple[bool, str]:
    del value
    return False, "理智规则已固定：普通理智药 0，只自动使用临期药，碎石 0。"


def skip_today(value: str) -> tuple[bool, str]:
    if value not in {"on", "off"}:
        return False, "skip-today 只接受 on 或 off。"
    was_running = is_run_locked()
    plan = _save_override("skip", value == "on")
    return True, _confirmation(plan, was_running)


def show_plan() -> tuple[bool, str]:
    plan = effective_plan()
    status = "跳过" if plan.get("skip") else "照常执行"
    return (
        True,
        f"今天刷：{plan_stage_label(plan)}；理智药："
        f"{medicine_label(str(plan.get('medicine', 'none')))}；{status}；碎石：0。",
    )


def run_now() -> tuple[bool, str]:
    if is_run_locked():
        phase, stage = _running_context()
        return False, f"{phase}已经在跑 {stage}，没有重复启动。"
    script = SCRIPTS_DIR / "maa_daily.py"
    if not script.is_file():
        return False, "maa_daily.py 尚未部署。"
    config = load_config()
    try:
        require_executable(config, "maa_bin", "/opt/homebrew/bin/maa", "maa-cli")
        require_executable(config, "adb_path", "/opt/homebrew/bin/adb", "ADB")
    except RuntimeError as exc:
        return False, f"暂时不能运行：{exc}"
    subprocess.Popen(
        [sys.executable, str(script), "now"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return True, "▶️ 已派发手动场；后台会自行加锁，开跑消息稍后出现在频道。"


def log_tail() -> tuple[bool, str]:
    state = load_json(LAST_RUN_PATH, {})
    if not isinstance(state, dict) or not state.get("status"):
        return True, "📋 还没有 MAA 执行结果。"
    summary = str(state.get("summary", "")).strip()
    # Existing state may have been produced by the pre-translation runner.
    # Rebuild it from the local log without ever exposing raw CLI lines.
    if not summary.startswith("🌟 总体："):
        log_path = Path(str(state.get("log", "")))
        if log_path.is_file():
            from maa_daily import extract_summary

            raw = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            plan = state.get("plan", {})
            summary = extract_summary(
                raw,
                [],
                plan=plan if isinstance(plan, dict) else {},
                ok=state.get("status") == "success",
            )
    if not summary:
        summary = "🌟 总体：暂无可解析的执行明细"
    finished = str(state.get("finished_at", "")).replace("T", " ")[:19]
    when = f"（{finished}）" if finished else ""
    return True, f"📋 最近一次结果{when}\n{summary}"


def dispatch(argv: list[str]) -> tuple[bool, str]:
    if argv[:2] == ["plan", "set-stage"] and len(argv) == 3:
        return set_stage(argv[2])
    if argv[:2] == ["plan", "skip-today"] and len(argv) in {2, 3}:
        return skip_today(argv[2] if len(argv) == 3 else "on")
    if argv == ["plan", "show"]:
        return show_plan()
    if argv == ["run", "now"]:
        return run_now()
    if argv == ["log", "tail"]:
        return log_tail()
    return False, "不支持的动作；只允许 plan set-stage/skip-today/show、run now、log tail。"


def main(argv: list[str]) -> int:
    try:
        ok, message = dispatch(argv[1:])
    except Exception as exc:
        emit(False, f"操作失败：{type(exc).__name__}: {exc}")
        return 1
    emit(ok, message)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
