#!/usr/bin/env python3
"""Idempotently deploy the reviewed MAA workflow into this user's Hermes home."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
USER_HOME = Path.home()
HERMES_HOME = USER_HOME / ".hermes"
MAA_CONFIG_HOME = USER_HOME / ".config" / "maa-cli"
CHANNEL_PLACEHOLDER = "REPLACE_WITH_DISCORD_CHANNEL_ID"
changed: list[str] = []
BACKUP_STAMP = datetime.now().strftime("%Y%m%dT%H%M%S")
backed_up: set[Path] = set()


def backup_root() -> Path:
    """本次部署的备份目录。调用时才按 `HERMES_HOME` 算：import 时算死的话，
    单测 patch 了 `HERMES_HOME` 备份也照样写进线上 `~/.hermes/backups/`。"""
    return HERMES_HOME / "backups" / "maa-setup" / BACKUP_STAMP


def ensure_dir(path: Path, mode: int = 0o700) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)


def backup(path: Path) -> None:
    if not path.exists() or path in backed_up:
        return
    relative = Path(str(path).lstrip("/"))
    target = backup_root() / relative
    ensure_dir(target.parent)
    shutil.copy2(path, target)
    os.chmod(target, 0o600)
    backed_up.add(path)


def write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    if path.exists() and path.read_bytes() == data:
        return
    backup(path)
    ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
        os.chmod(path, mode)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    changed.append(str(path))


def write_text(path: Path, value: str, mode: int = 0o600) -> None:
    write_bytes(path, value.encode("utf-8"), mode)


def copy_file(source: Path, target: Path, mode: int) -> None:
    write_bytes(target, source.read_bytes(), mode)


# Keys in `state/config.json` that live processes probe and rewrite. They are
# runtime caches, not configuration the repository owns, so a seed value is only
# a first-boot fallback: once the host has its own value, the seed must never
# clobber it (`deploy_state()`).
#   maa_core_version / maa_cli_version — written by the plan card's version probe
#   activity_stages                    — written by the activity watcher / plan card
# Letting the seed win here re-creates the staleness PLAN_NAV_GATE A-3 fixed:
# on 2026-09-02 a deploy replaced a correct empty list with a month-old TO
# event's stages, and only the next watcher tick cleaned it up.
# **Adding a key of this kind? Add it here too** — DEV_GUIDE §8 不变量 #25, and
# `test_every_known_runtime_cache_key_is_preserved_on_deploy` will fail until
# you do.
PRESERVED_RUNTIME_CACHE_KEYS = (
    "maa_core_version",
    "maa_cli_version",
    "activity_stages",
)

# 已经没有代码再读的配置键。`deploy_state()` 每次部署顺手删掉它们——
# config.json 是逐键 merge 的，不删就会永远留在线上（四期步骤 2 拆掉通关队列）。
RETIRED_CONFIG_KEYS = (
    "clearing_enabled",
    "clearing_manual_after_days",
    "clearing_annihilation_sanity",
    "clearing_ex_stage_count",
    "clearing_sanity_ocr_roi",
    # 模拟器 → PlayCover：设备层换成 `maa_playcover.py`，这些模拟器 / ADB 键没人读了。
    "emulator_app",
    "emulator_bundle_id",
    "emulator_process_ignore_case",
    "adb_path",
    "adb_address",
    "game_package",
    "emulator_boot_timeout",
    "emulator_settle_timeout",
    "emulator_settle_quiet_seconds",
    "emulator_settle_slow_seconds",
    "quit_grace_seconds",
    "emulator_probe_timeout",
)

MAA_CONFIG_RELATIVE_PATHS = (
    Path("profiles/default.toml"),
    Path("tasks/daily.toml"),
    Path("tasks/depot.toml"),
    # 练度后备（四期步骤 5）：森空岛不可用时由 `maa_depot.run_depot()` 在同一次
    # 会话里跑，`maa run operbox` 读的就是这个部署过去的任务文件。
    Path("tasks/operbox.toml"),
    Path("tasks/infrast-smoke.toml"),
    Path("cli.toml"),
    Path("resource/tasks/tasks.json"),
    # 三丽鸥联动界面主题的主页入口模板，由 tasks.json 里的 `<入口>Sanrio` 引用。
    *(
        Path("resource/template/UiTheme/Sanrio") / name
        for name in (
            "Friends.png",
            "Mall.png",
            "Task.png",
            "Terminal.png",
            "Recruit.png",
            "EnterInfrast.png",
            "DepotEnter.png",
            "OperBoxEnter.png",
            "GachaEnter.png",
        )
    ),
)

# `maa_daily.render_daily_toml()` rewrites the deployed `tasks/daily.toml` in
# full before every run (DEV_GUIDE §0), so the runtime copy legitimately differs
# from the repository seed the moment a run happens. Comparing its bytes would
# make `--check` permanently red and train us to ignore it, so it is checked for
# existence only. Every other maa-cli config — `resource/tasks/tasks.json`
# above all, which nothing rewrites at runtime — is still compared byte for byte.
RUNTIME_REWRITTEN_MAA_CONFIGS = frozenset({Path("tasks/daily.toml")})


def maa_config_file_pairs() -> list[tuple[Path, Path]]:
    """Return every repository-managed maa-cli config and its runtime target."""
    source_root = ROOT / "config" / "maa-cli"
    return [
        (source_root / path, MAA_CONFIG_HOME / path)
        for path in MAA_CONFIG_RELATIVE_PATHS
    ]


def maa_config_content_checked_pairs() -> list[tuple[Path, Path]]:
    """The subset of `maa_config_file_pairs()` whose bytes must match the repo."""
    source_root = ROOT / "config" / "maa-cli"
    return [
        (source_root / path, MAA_CONFIG_HOME / path)
        for path in MAA_CONFIG_RELATIVE_PATHS
        if path not in RUNTIME_REWRITTEN_MAA_CONFIGS
    ]


def content_mismatches(file_pairs: list[tuple[Path, Path]]) -> list[str]:
    """Report deployed files whose bytes differ; missing targets are reported separately."""
    return [
        str(target)
        for source, target in file_pairs
        if target.is_file() and target.read_bytes() != source.read_bytes()
    ]


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path: Path, value: Any) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def resolve_channel_id() -> str:
    """Resolve the Discord channel id without storing it in this repository.

    Prefers an explicit MAA_CHANNEL_ID, then the value already deployed to this
    machine, so a redeploy on a configured host needs no extra arguments.
    """
    configured = os.environ.get("MAA_CHANNEL_ID", "").strip()
    if not configured:
        deployed = load_json(HERMES_HOME / "state" / "maa" / "config.json", {})
        if isinstance(deployed, dict):
            configured = str(deployed.get("channel_id", "")).strip()
    if configured in ("", CHANNEL_PLACEHOLDER) or not configured.isdigit():
        raise RuntimeError(
            "未解析到 Discord 频道 ID。首次部署请执行："
            "MAA_CHANNEL_ID=<频道数字ID> python3 deploy.py"
        )
    return configured


# 部署到 `~/.hermes/scripts/` 的 Python 脚本，`deploy_scripts()` 和 `--check` 共用。
# 必须与仓库 `scripts/*.py` 一一对应（`test_script_names_match_the_repository_scripts`）。
SCRIPT_NAMES = (
    "maa_common.py",
    "maa_playcover.py",
    "maa_plan_card.py",
    "maa_activity_watch.py",
    "maa_depot.py",
    "maa_daily.py",
    "maa_action.py",
    "maa_clearing.py",
    "maa_copilot.py",
    "maa_farming.py",
    "maa_gamedata.py",
    "maa_roguelike.py",
    "maa_copilot_worker.py",
    "maa_roguelike_worker.py",
    "maa_skland.py",
)
# swiftc 编译产物：线上是二进制，`--check` 只查存在。
COMPILED_HELPERS = ("ocrbox", "winmove")
SKILL_RELATIVE_PATH = Path("skills") / "games" / "maa-operator" / "SKILL.md"


def script_file_pairs() -> list[tuple[Path, Path]]:
    target = HERMES_HOME / "scripts"
    return [(ROOT / "scripts" / name, target / name) for name in SCRIPT_NAMES]


def skill_file_pair() -> tuple[Path, Path]:
    return ROOT / SKILL_RELATIVE_PATH, HERMES_HOME / SKILL_RELATIVE_PATH


def deploy_scripts() -> None:
    ensure_dir(HERMES_HOME / "scripts", 0o700)
    for source, target in script_file_pairs():
        copy_file(source, target, 0o700)


def deploy_skland_token() -> None:
    """SKLAND_HG_TOKEN env -> copilot/hg_token.txt (0600); missing is fine."""
    token = os.environ.get("SKLAND_HG_TOKEN", "").strip()
    if not token:
        return
    target = HERMES_HOME / "state" / "maa" / "copilot" / "hg_token.txt"
    write_text(target, token + "\n", 0o600)


def deploy_ocrbox() -> None:
    """Compile the Vision-based OCR helper (macOS) into ~/.hermes/scripts/."""
    _compile_swift(
        "ocrbox",
        "接管弹窗 watchdog 将不可用，代理指挥开着的关卡会全部失败！",
    )


def deploy_winmove() -> None:
    """Compile the window mover `maa_playcover.settle_game_window()` uses."""
    _compile_swift(
        "winmove",
        "游戏窗口开在别的屏上时挪不回 MAA-Game，台前调度一挤它就截不到图！",
    )


def _compile_swift(name: str, consequence: str) -> None:
    source = ROOT / "scripts" / f"{name}.swift"
    target = HERMES_HOME / "scripts" / name
    if not source.is_file() or shutil.which("swiftc") is None:
        return
    if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
        return
    try:
        result = subprocess.run(
            ["swiftc", "-O", str(source), "-o", str(target)],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr[-500:])
        os.chmod(target, 0o700)
        changed.append(str(target))
    except Exception as exc:
        print(
            f"{name} 编译失败（{type(exc).__name__}: {exc}）——{consequence}",
            file=sys.stderr,
        )


def deploy_state(channel_id: str) -> None:
    target = HERMES_HOME / "state" / "maa"
    ensure_dir(target)
    ensure_dir(target / "runs")
    ensure_dir(target / "am")
    ensure_dir(target / "pm")
    for sub in ("copilot", "copilot/cache", "copilot/searches", "copilot/prepared"):
        ensure_dir(target / sub)

    desired_config = load_json(ROOT / "state" / "config.json", {})
    if isinstance(desired_config, dict):
        # The repository only carries a placeholder; never let it reach disk.
        desired_config["channel_id"] = channel_id
    current_config = load_json(target / "config.json", {})
    if not isinstance(current_config, dict):
        raise RuntimeError("existing MAA config.json is not an object")
    preserved_runtime_caches = {
        key: current_config[key]
        for key in PRESERVED_RUNTIME_CACHE_KEYS
        if key in current_config
    }
    current_config.update(desired_config)
    current_config.update(preserved_runtime_caches)
    for retired in ("resource_update_interval_days", *RETIRED_CONFIG_KEYS):
        current_config.pop(retired, None)
    write_json(target / "config.json", current_config)

    for name in ("defaults.json", "plan.json", "last_run.json"):
        path = target / name
        if not path.exists():
            copy_file(ROOT / "state" / name, path, 0o600)
    # Migrate legacy mutable medication state without disturbing the user's
    # selected stage or skip flag. The runner now enforces this policy itself.
    defaults_path = target / "defaults.json"
    current_defaults = load_json(defaults_path, {})
    if not isinstance(current_defaults, dict):
        raise RuntimeError("existing MAA defaults.json is not an object")
    current_defaults["medicine"] = "expiring"
    current_defaults["stone"] = 0
    current_defaults["stage"] = "1-7"
    write_json(defaults_path, current_defaults)

    plan_path = target / "plan.json"
    current_plan = load_json(plan_path, {})
    if not isinstance(current_plan, dict):
        raise RuntimeError("existing MAA plan.json is not an object")
    current_plan.pop("medicine", None)
    current_plan.pop("stone", None)
    write_json(plan_path, current_plan)
    lock = target / "run.lock"
    if not lock.exists():
        write_text(lock, "")


def deploy_maa_config() -> None:
    for source, target in maa_config_file_pairs():
        copy_file(source, target, 0o600)


def deploy_skill() -> None:
    source, target = skill_file_pair()
    copy_file(source, target, 0o600)


def patch_adapter_source(text: str) -> str:
    """Add the reviewed MAA script and exact-value select-menu dispatch."""
    old = '_HERMES_BUTTON_SCRIPTS = frozenset({"agent_book_action.py"})'
    new = '_HERMES_BUTTON_SCRIPTS = frozenset({"agent_book_action.py", "maa_action.py"})'
    # "Already allowed" must be judged on the whole frozenset literal, not on
    # its first line: hermes-agent f6b3e394b7 (2026-09-03) carries maa_action.py
    # natively in a multi-line frozenset, and a first-line check misread that
    # as an unknown baseline and refused to deploy anything.
    head, _sep, tail = text.partition("_HERMES_BUTTON_SCRIPTS = frozenset({")
    allowlist_literal = tail.split("})", 1)[0] if _sep else ""
    if '"maa_action.py"' not in allowlist_literal:
        if text.count(old) != 1:
            raise RuntimeError("Hermes button allowlist no longer matches the reviewed baseline")
        text = text.replace(old, new)

    old_signature = (
        "    async def _run_hermes_button_script(\n"
        "        self,\n"
        "        entry: dict[str, Any],\n"
        "    ) -> tuple[bool, str]:"
    )
    new_signature = (
        "    async def _run_hermes_button_script(\n"
        "        self,\n"
        "        entry: dict[str, Any],\n"
        "        selected_values: list[str] | None = None,\n"
        "    ) -> tuple[bool, str]:"
    )
    if new_signature not in text:
        if text.count(old_signature) != 1:
            raise RuntimeError("Hermes component runner signature no longer matches baseline")
        text = text.replace(old_signature, new_signature)

    old_args = (
        "        raw_args = entry.get(\"args\", [])\n"
        "        if not isinstance(raw_args, list) or len(raw_args) > 10:\n"
        "            return False, \"按钮动作参数无效。\"\n"
        "        args = [str(value) for value in raw_args]\n"
        "        if any(len(value) > 200 for value in args):\n"
        "            return False, \"按钮动作参数过长。\""
    )
    new_args = (
        "        select_args = entry.get(\"select_args\")\n"
        "        if select_args is not None:\n"
        "            if (\n"
        "                not isinstance(select_args, dict)\n"
        "                or not isinstance(selected_values, list)\n"
        "                or len(selected_values) != 1\n"
        "            ):\n"
        "                return False, \"下拉选项无效。\"\n"
        "            raw_args = select_args.get(str(selected_values[0]))\n"
        "        else:\n"
        "            raw_args = entry.get(\"args\", [])\n"
        "        if not isinstance(raw_args, list) or len(raw_args) > 10:\n"
        "            return False, \"按钮动作参数无效。\"\n"
        "        args = [str(value) for value in raw_args]\n"
        "        if any(len(value) > 200 for value in args):\n"
        "            return False, \"按钮动作参数过长。\""
    )
    if new_args not in text:
        if text.count(old_args) != 1:
            raise RuntimeError("Hermes component argument validation no longer matches baseline")
        text = text.replace(old_args, new_args)

    old_call = "                ok, message = await self._run_hermes_button_script(entry)"
    new_call = (
        "                raw_values = (interaction.data or {}).get(\"values\", [])\n"
        "                selected_values = (\n"
        "                    [str(value) for value in raw_values]\n"
        "                    if isinstance(raw_values, list)\n"
        "                    else []\n"
        "                )\n"
        "                ok, message = await self._run_hermes_button_script(\n"
        "                    entry, selected_values\n"
        "                )"
    )
    if new_call not in text:
        if text.count(old_call) != 1:
            raise RuntimeError("Hermes component dispatch call no longer matches baseline")
        text = text.replace(old_call, new_call)
    return text


def adapter_path() -> Path:
    return HERMES_HOME / "hermes-agent" / "plugins" / "platforms" / "discord" / "adapter.py"


def patch_adapter() -> None:
    adapter = adapter_path()
    text = adapter.read_text(encoding="utf-8")
    patched = patch_adapter_source(text)
    if patched != text:
        write_text(adapter, patched, adapter.stat().st_mode & 0o777)


def patch_local_patches() -> None:
    path = HERMES_HOME / "hermes-agent" / "LOCAL_PATCHES.md"
    text = path.read_text(encoding="utf-8")
    marker = "### plugins/platforms/discord/adapter.py — MAA 动作白名单"
    if marker not in text:
        addition = (
            "\n\n"
            + marker
            + "\n\n"
            "为 `hermes:btn:maa:*` 的确定性组件显式加入 `maa_action.py`。仍使用精确文件名"
            "白名单，不放行 `maa_daily.py` 或任何通配模式；脚本内部再对白名单关卡、"
            "跳过和并发锁做硬校验。组件登记继续 merge 到共享的 "
            "`~/.hermes/state/agent-book/buttons.json`。\n"
        )
        text = text.rstrip() + addition
    select_marker = "#### MAA 关卡下拉菜单"
    if select_marker not in text:
        text = (
            text.rstrip()
            + "\n\n"
            + select_marker
            + "\n\n"
            "持久组件处理器读取 Discord String Select 的单个 `values` 值，并且仅能通过"
            "登记项的 `select_args` 精确映射为 argv。未知值、多个值和直接参数注入全部"
            "拒绝；普通按钮仍沿用原有 `args` 分支。\n"
        )
    write_text(path, text, path.stat().st_mode & 0o777)


def patch_env(channel_id: str) -> None:
    path = HERMES_HOME / ".env"
    lines = path.read_text(encoding="utf-8").splitlines()
    key = "DISCORD_FREE_RESPONSE_CHANNELS"
    found = False
    for index, line in enumerate(lines):
        if line.startswith(key + "="):
            found = True
            values = [value.strip() for value in line.split("=", 1)[1].split(",") if value.strip()]
            if channel_id not in values:
                values.append(channel_id)
                lines[index] = key + "=" + ",".join(values)
            break
    if not found:
        lines.append(f"{key}={channel_id}")
    write_text(path, "\n".join(lines) + "\n", 0o600)


MAA_CHANNEL_PROMPT = (
    "你是 MAA 长草助手。只处理今天早场 / 晚场各自的关卡与跳过、以及抄作业；"
    "所有读取与变更必须遵守 maa-operator 技能并通过 maa_action.py，禁止直接写状态文件，碎石永久关闭。"
)
# Superseded prompt wordings that a previous deploy may have written. Kept so an
# upgrade rewrites them in place instead of appending a second entry.
OLD_MAA_CHANNEL_PROMPTS = (
    "你是 MAA 长草助手。只处理今天的关卡、临期理智药、跳过、立即运行和日志；"
    "所有读取与变更必须遵守 maa-operator 技能并通过 maa_action.py，禁止直接写状态文件，碎石永久关闭。",
    "你是 MAA 长草助手。只处理今天的关卡、跳过和最近结果；"
    "所有读取与变更必须遵守 maa-operator 技能并通过 maa_action.py，禁止直接写状态文件，碎石永久关闭。",
)
HERMES_VENV_PYTHON = HERMES_HOME / "hermes-agent" / "venv" / "bin" / "python"


def parse_yaml(text: str) -> Any:
    """Parse YAML, raising ValueError on malformed input.

    The repository's tests run on the system interpreter, which has no PyYAML.
    Hermes ships its own, so fall back to its venv rather than adding a
    third-party dependency to this script.
    """
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError:
        pass
    else:
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError as exc:  # pragma: no cover - depends on host
            raise ValueError(str(exc)) from exc
    if not HERMES_VENV_PYTHON.exists():
        raise RuntimeError(
            f"need PyYAML to validate config.yaml: neither this interpreter nor {HERMES_VENV_PYTHON} has it"
        )
    process = subprocess.run(
        [
            str(HERMES_VENV_PYTHON),
            "-c",
            "import json,sys,yaml; json.dump(yaml.safe_load(sys.stdin.read()), sys.stdout, default=str)",
        ],
        input=text,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise ValueError(process.stderr.strip() or "yaml.safe_load failed")
    return json.loads(process.stdout)


def _discord_section(document: Any) -> dict[str, Any]:
    platforms = document.get("platforms") if isinstance(document, dict) else None
    discord = platforms.get("discord") if isinstance(platforms, dict) else None
    return discord if isinstance(discord, dict) else {}


def patch_config_yaml_text(text: str, channel_id: str) -> str:
    """Return config.yaml with the MAA channel binding and prompt applied.

    The prompt is anchored on the `channel_prompts:` mapping itself, never on a
    neighbouring top-level key: key order in config.yaml is Hermes' to change,
    and an anchor that assumes it will eventually splice the entry into the
    wrong block (see DEV_GUIDE.md §11). The result is re-parsed before the
    caller is allowed to write it.
    """
    try:
        before = parse_yaml(text)
    except ValueError as exc:
        raise RuntimeError(
            f"config.yaml 当前就无法解析，请先手工修好再部署：{exc}"
        ) from exc
    if not isinstance(before, dict):
        raise RuntimeError("config.yaml 顶层不是映射，请先手工修好再部署")

    binding = (
        f"      - id: '{channel_id}'\n"
        "        skills:\n"
        "          - maa-operator\n"
    )
    if binding not in text:
        marker = "    channel_prompts:\n"
        if text.count(marker) != 1:
            raise RuntimeError("Discord channel_prompts marker is ambiguous")
        text = text.replace(marker, binding + marker)

    prompts = _discord_section(before).get("channel_prompts")
    current = prompts.get(channel_id) if isinstance(prompts, dict) else None
    if current != MAA_CHANNEL_PROMPT:
        entry = f"'{channel_id}': '{MAA_CHANNEL_PROMPT}'"
        if current is None:
            text = _insert_channel_prompt(text, entry)
        elif current in OLD_MAA_CHANNEL_PROMPTS:
            text = _replace_channel_prompt(text, channel_id, entry)
        else:
            raise RuntimeError(
                f"channel_prompts['{channel_id}'] 已有非预期文案，请手工确认后再部署：{current!r}"
            )

    try:
        after = parse_yaml(text)
    except ValueError as exc:
        raise RuntimeError(f"补丁后的 config.yaml 无法解析，已放弃写盘：{exc}") from exc
    if not isinstance(after, dict):
        raise RuntimeError("补丁后的 config.yaml 顶层不是映射，已放弃写盘")
    if "timezone" not in after or after.get("timezone") != before.get("timezone"):
        raise RuntimeError(
            "补丁后顶层 timezone 丢失或被改动，已放弃写盘："
            f"{before.get('timezone')!r} -> {after.get('timezone')!r}"
        )
    patched_prompts = _discord_section(after).get("channel_prompts")
    if not isinstance(patched_prompts, dict) or patched_prompts.get(channel_id) != MAA_CHANNEL_PROMPT:
        raise RuntimeError(
            "补丁后 platforms.discord.channel_prompts 未落到预期文案，已放弃写盘"
        )
    return text


def _prompt_key_pattern(channel_id: str) -> re.Pattern[str]:
    """`channel_prompts` 里这一条的键行：带单引号、双引号或裸写都算。"""
    quoted = re.escape(channel_id)
    return re.compile(
        rf"^(?P<indent>\s*)(?:'{quoted}'|\"{quoted}\"|{quoted})\s*:"
    )


def _replace_channel_prompt(text: str, channel_id: str, entry: str) -> str:
    """把这一条**整条**（含续行）换成新的单行带引号条目。

    不能按「单行带引号」整行匹配：Hermes 会把条目重排成**无引号、折成两行**的
    plain scalar，例如

        '123': 你是 MAA 长草助手。只处理今天的关卡、跳过和最近结果；所有读取与变更
          必须遵守 maa-operator 技能……

    这时旧写法找不到那一行，就会误判成「已有非预期文案」而拒绝部署
    （2026-09-09 步骤 1 部署时实际踩到）。所以定位键行之后，把**缩进更深的续行**
    一起吃掉再替换。是否需要替换由**解析后的值**判断（调用方比对
    `OLD_MAA_CHANNEL_PROMPTS`），不比文本。
    """
    lines = text.splitlines(keepends=True)
    pattern = _prompt_key_pattern(channel_id)
    hits = [index for index, line in enumerate(lines) if pattern.match(line)]
    if len(hits) != 1:
        raise RuntimeError(
            f"config.yaml 里 channel_prompts['{channel_id}'] 的条目定位不唯一（{len(hits)} 处）"
        )
    start = hits[0]
    indent = len(lines[start]) - len(lines[start].lstrip(" "))
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if not line.strip():
            break
        if len(line) - len(line.lstrip(" ")) <= indent:
            break
        end += 1
    lines[start:end] = [" " * indent + entry + "\n"]
    return "".join(lines)


def _insert_channel_prompt(text: str, entry: str) -> str:
    """Insert `entry` as the first item of the `channel_prompts:` mapping."""
    lines = text.splitlines(keepends=True)
    anchors = [
        index
        for index, line in enumerate(lines)
        if line.strip() == "channel_prompts:"
    ]
    if len(anchors) != 1:
        raise RuntimeError("Discord channel_prompts marker is ambiguous")
    anchor = anchors[0]
    anchor_indent = len(lines[anchor]) - len(lines[anchor].lstrip(" "))
    indent = anchor_indent + 2
    for line in lines[anchor + 1:]:
        if not line.strip():
            continue
        line_indent = len(line) - len(line.lstrip(" "))
        if line_indent > anchor_indent:
            indent = line_indent
        break
    lines.insert(anchor + 1, " " * indent + entry + "\n")
    return "".join(lines)


def patch_config_yaml(channel_id: str) -> None:
    path = HERMES_HOME / "config.yaml"
    text = path.read_text(encoding="utf-8")
    write_text(path, patch_config_yaml_text(text, channel_id), 0o600)


# ---------------------------------------------------------------------------
# `--check`：只读。不写文件、不打补丁，只报告线上与仓库是否一致。
# ---------------------------------------------------------------------------

GATEWAY_PLIST_RELATIVE_PATH = Path("Library") / "LaunchAgents" / "ai.hermes.gateway.plist"
# Hermes 仓库里可能并存的两个虚拟环境：`venv` 是 gateway 实际用的（HERMES_VENV_PYTHON），
# `.venv` 是 uv 等工具顺手建的；两个都在时容易装错包、跑错解释器。
HERMES_VENV_NAMES = ("venv", ".venv")
PYTHON_VERSION_PROBE = "import sys; print('.'.join(map(str, sys.version_info[:3])))"
PYTHON_VERSION_TIMEOUT = 10.0


def gateway_plist_path() -> Path:
    """调用时才按 `USER_HOME` 算，单测可以 patch。"""
    return USER_HOME / GATEWAY_PLIST_RELATIVE_PATH


def python_version(interpreter: Path, timeout: float = PYTHON_VERSION_TIMEOUT) -> str:
    """让 `interpreter` 自己打印版本号；起不来、超时、非零退出都抛 RuntimeError。"""
    try:
        result = subprocess.run(
            [str(interpreter), "-I", "-c", PYTHON_VERSION_PROBE],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"exit {result.returncode}: {result.stderr.strip()[-200:]}")
    version = result.stdout.strip()
    if not version:
        raise RuntimeError("没有输出版本号")
    return version


def same_interpreter(program: str, expected: str) -> bool:
    """路径字符串相同，或 realpath 相同且所在目录 realpath 也相同。

    只比文件 realpath 不够：venv 里的 `bin/python` 是指向 uv 解释器的符号链接，
    两个不同 venv 指向同一个 3.11 时 realpath 一样，但 site-packages 不同。
    """
    if program == expected:
        return True
    return os.path.realpath(os.path.dirname(program)) == os.path.realpath(
        os.path.dirname(expected)
    ) and os.path.realpath(program) == os.path.realpath(expected)


def check_adapter() -> str:
    """`patched` / `needs_patch` / `anchor_mismatch: …` / `missing`（/ `unreadable: …`）。"""
    path = adapter_path()
    if not path.is_file():
        return "missing"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"unreadable: {type(exc).__name__}: {exc}"
    try:
        patched = patch_adapter_source(text)
    except RuntimeError as exc:
        return f"anchor_mismatch: {exc}"
    return "patched" if patched == text else "needs_patch"


def check_gateway_python() -> dict[str, Any]:
    plist = gateway_plist_path()
    expected = str(HERMES_VENV_PYTHON)
    report: dict[str, Any] = {
        "plist": str(plist),
        "program": None,
        "version": None,
        "expected": expected,
        "matches": False,
    }
    if not plist.is_file():
        report["error"] = "plist 不存在"
        return report
    try:
        with plist.open("rb") as handle:
            document = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        report["error"] = f"plist 读不出：{type(exc).__name__}: {exc}"
        return report
    arguments = document.get("ProgramArguments") if isinstance(document, dict) else None
    if not isinstance(arguments, list) or not arguments or not isinstance(arguments[0], str):
        report["error"] = "plist 里没有 ProgramArguments[0]"
        return report
    program = arguments[0]
    report["program"] = program
    report["matches"] = same_interpreter(program, expected)
    try:
        report["version"] = python_version(Path(program))
    except RuntimeError as exc:
        report["error"] = f"取版本失败：{exc}"
    return report


def check_venvs() -> tuple[dict[str, str | None], list[str]]:
    versions: dict[str, str | None] = {}
    present: list[str] = []
    for name in HERMES_VENV_NAMES:
        root = HERMES_HOME / "hermes-agent" / name
        if not root.exists():
            versions[name] = None
            continue
        present.append(name)
        try:
            versions[name] = python_version(root / "bin" / "python")
        except RuntimeError as exc:
            versions[name] = f"error: {exc}"
    warnings: list[str] = []
    if len(present) > 1:
        warnings.append(
            "hermes-agent 下 venv 与 .venv 并存（"
            + "、".join(f"{name}={versions[name]}" for name in present)
            + f"）：gateway 用的是 {HERMES_VENV_PYTHON}，往另一个里装包不会生效"
        )
    return versions, warnings


# 最后一次真机跑通时的组件版本（DEV_GUIDE §7.1）。`--check` 只报告漂移，不影响 `ok`。
VERIFIED_VERSIONS_PATH = ROOT / "config" / "verified_versions.json"
VERIFIED_VERSIONS_SCHEMA = 1
PLAYCOVER_APP = Path("/Applications/PlayCover.app")
BETTERDISPLAY_APP = Path("/Applications/BetterDisplay.app")
ARKNIGHTS_APP_RELATIVE_PATH = (
    Path("Library") / "Containers" / "io.playcover.PlayCover" / "Applications"
    / "com.hypergryph.arknights.app"
)
GIT_TIMEOUT = 10.0
# `--check` 比对的组件，顺序即输出顺序。MaaTools 的 VERN 要游戏在跑才读得到，只记录不比对。
CHECKED_COMPONENTS = ("maa-cli", "MaaCore", "PlayCover", "Hermes", "BetterDisplay", "Arknights")


def load_verified_versions(path: Path | None = None) -> dict[str, str]:
    """`{组件: 记录的版本}`；文件缺失、格式或 schema 不对抛 RuntimeError。"""
    path = VERIFIED_VERSIONS_PATH if path is None else path
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"{path} 读不出：{type(exc).__name__}: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != VERIFIED_VERSIONS_SCHEMA:
        raise RuntimeError(f"{path} 的 schema_version 不是 {VERIFIED_VERSIONS_SCHEMA}")
    components = document.get("components")
    if not isinstance(components, dict):
        raise RuntimeError(f"{path} 里没有 components 对象")
    verified: dict[str, str] = {}
    for name, entry in components.items():
        if isinstance(entry, dict) and isinstance(entry.get("version"), str):
            verified[name] = entry["version"]
    return verified


def bundle_short_version(app: Path) -> str:
    """`<app>/Contents/Info.plist`（iOS 包没有 Contents 层）的 `CFBundleShortVersionString`。"""
    candidates = [app / "Contents" / "Info.plist", app / "Info.plist"]
    plist = next((path for path in candidates if path.is_file()), None)
    if plist is None:
        raise RuntimeError(f"{app} 下没有 Info.plist")
    try:
        with plist.open("rb") as handle:
            document = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise RuntimeError(f"{plist} 读不出：{type(exc).__name__}: {exc}") from exc
    version = document.get("CFBundleShortVersionString") if isinstance(document, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise RuntimeError(f"{plist} 里没有 CFBundleShortVersionString")
    return version.strip()


def probe_maa_versions() -> tuple[str, str]:
    """`(maa-cli, MaaCore)`，取不到的为空串。复用计划卡的 `maa version` 解析，避免两套。"""
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import maa_plan_card

    config = load_json(HERMES_HOME / "state" / "maa" / "config.json", {})
    return maa_plan_card.read_maa_versions(config if isinstance(config, dict) else {})


def probe_hermes_commit() -> str:
    repo = HERMES_HOME / "hermes-agent"
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
    commit = result.stdout.strip()
    if result.returncode != 0 or not commit:
        raise RuntimeError(f"git rev-parse 失败（exit {result.returncode}）：{result.stderr.strip()[-200:]}")
    return commit


def installed_versions() -> tuple[dict[str, str | None], dict[str, str]]:
    """`({组件: 版本或 None}, {组件: 取不到的原因})`。各探测失败互不影响，不抛。"""
    installed: dict[str, str | None] = {name: None for name in CHECKED_COMPONENTS}
    errors: dict[str, str] = {}
    try:
        cli, core = probe_maa_versions()
    except Exception as exc:  # 探测失败只记原因；--check 不能因为某个组件探测出错就整个崩掉
        errors["maa-cli"] = errors["MaaCore"] = f"{type(exc).__name__}: {exc}"
    else:
        for name, value in (("maa-cli", cli), ("MaaCore", core)):
            if value:
                installed[name] = value
            else:
                errors[name] = "maa version 没有输出这一项"
    probes = {
        "PlayCover": lambda: bundle_short_version(PLAYCOVER_APP),
        "Hermes": probe_hermes_commit,
        "BetterDisplay": lambda: bundle_short_version(BETTERDISPLAY_APP),
        "Arknights": lambda: bundle_short_version(USER_HOME / ARKNIGHTS_APP_RELATIVE_PATH),
    }
    for name, probe in probes.items():
        try:
            installed[name] = probe()
        except RuntimeError as exc:
            errors[name] = str(exc)
    return installed, errors


def version_matches(name: str, installed: str, verified: str) -> bool:
    if name == "Hermes":
        # 记录里可以是短 hash；rev-parse 给的是完整 hash。
        return len(verified) >= 7 and installed.lower().startswith(verified.lower())
    return installed == verified


def check_versions() -> tuple[dict[str, dict[str, Any]], list[str]]:
    """`({组件: {installed, verified, match}}, warnings)`。漂移、取不到都只进 warnings。"""
    warnings: list[str] = []
    try:
        verified = load_verified_versions()
    except RuntimeError as exc:
        verified = {}
        warnings.append(f"真机验证版本记录不可用：{exc}")
    installed, errors = installed_versions()
    report: dict[str, dict[str, Any]] = {}
    drifted: list[str] = []
    for name in CHECKED_COMPONENTS:
        have = installed.get(name)
        want = verified.get(name)
        match = None if have is None or want is None else version_matches(name, have, want)
        report[name] = {"installed": have, "verified": want, "match": match}
        if have is None:
            warnings.append(f"{name} 版本取不到：{errors.get(name, '未知原因')}")
        elif want is None and verified:
            warnings.append(f"{name} 在 {VERIFIED_VERSIONS_PATH.name} 里没有记录")
        elif match is False:
            drifted.append(f"{name} {want} → {have}")
    if drifted:
        warnings.append(
            "组件版本与最后一次真机验证不同（"
            + "；".join(drifted)
            + "）：跑通一次真机后按 DEV_GUIDE §7.1 更新 config/verified_versions.json"
        )
    return report, warnings


def check_deployment() -> dict[str, Any]:
    """`--check` 的报告。只读：不写任何文件。"""
    skill = skill_file_pair()
    exists_checked = [target for _source, target in script_file_pairs()]
    exists_checked.extend(HERMES_HOME / "scripts" / name for name in COMPILED_HELPERS)
    exists_checked.append(skill[1])
    exists_checked.extend(target for _source, target in maa_config_file_pairs())
    missing = [str(path) for path in exists_checked if not path.is_file()]
    mismatched = content_mismatches(
        [*script_file_pairs(), skill, *maa_config_content_checked_pairs()]
    )
    adapter = check_adapter()
    gateway_python = check_gateway_python()
    venvs, warnings = check_venvs()
    versions, version_warnings = check_versions()
    warnings.extend(version_warnings)
    ok = (
        not missing
        and not mismatched
        and adapter == "patched"
        and gateway_python["matches"] is True
    )
    return {
        "ok": ok,
        "missing": missing,
        "mismatched": mismatched,
        "adapter": adapter,
        "gateway_python": gateway_python,
        "venvs": venvs,
        "versions": versions,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="only report whether deployment is current (read-only)")
    args = parser.parse_args(argv)
    if args.check:
        report = check_deployment()
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 1

    channel_id = resolve_channel_id()
    deploy_scripts()
    deploy_state(channel_id)
    deploy_maa_config()
    deploy_skill()
    deploy_skland_token()
    deploy_ocrbox()
    deploy_winmove()
    patch_adapter()
    patch_local_patches()
    patch_env(channel_id)
    patch_config_yaml(channel_id)
    print(json.dumps({"ok": True, "changed": changed, "backup": str(backup_root())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
