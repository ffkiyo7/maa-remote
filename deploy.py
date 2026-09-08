#!/usr/bin/env python3
"""Idempotently deploy the reviewed MAA workflow into this user's Hermes home."""

from __future__ import annotations

import argparse
import json
import os
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
backup_root = HERMES_HOME / "backups" / "maa-setup" / datetime.now().strftime("%Y%m%dT%H%M%S")
backed_up: set[Path] = set()


def ensure_dir(path: Path, mode: int = 0o700) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)


def backup(path: Path) -> None:
    if not path.exists() or path in backed_up:
        return
    relative = Path(str(path).lstrip("/"))
    target = backup_root / relative
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
)

MAA_CONFIG_RELATIVE_PATHS = (
    Path("profiles/default.toml"),
    Path("tasks/daily.toml"),
    Path("tasks/depot.toml"),
    Path("tasks/infrast-smoke.toml"),
    Path("cli.toml"),
    Path("resource/tasks/tasks.json"),
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


def deploy_scripts() -> None:
    target = HERMES_HOME / "scripts"
    ensure_dir(target, 0o700)
    for name in (
        "maa_common.py",
        "maa_plan_card.py",
        "maa_activity_watch.py",
        "maa_depot.py",
        "maa_daily.py",
        "maa_action.py",
        "maa_clearing.py",
        "maa_copilot.py",
        "maa_farming.py",
        "maa_roguelike.py",
        "maa_copilot_worker.py",
        "maa_roguelike_worker.py",
        "maa_skland.py",
    ):
        copy_file(ROOT / "scripts" / name, target / name, 0o700)


def deploy_skland_token() -> None:
    """SKLAND_HG_TOKEN env -> copilot/hg_token.txt (0600); missing is fine."""
    token = os.environ.get("SKLAND_HG_TOKEN", "").strip()
    if not token:
        return
    target = HERMES_HOME / "state" / "maa" / "copilot" / "hg_token.txt"
    write_text(target, token + "\n", 0o600)


def deploy_ocrbox() -> None:
    """Compile the Vision-based OCR helper (macOS) into ~/.hermes/scripts/."""
    source = ROOT / "scripts" / "ocrbox.swift"
    target = HERMES_HOME / "scripts" / "ocrbox"
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
            f"ocrbox 编译失败（{type(exc).__name__}: {exc}）——"
            "接管弹窗 watchdog 将不可用，代理指挥开着的关卡会全部失败！",
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
    copy_file(
        ROOT / "skills" / "games" / "maa-operator" / "SKILL.md",
        HERMES_HOME / "skills" / "games" / "maa-operator" / "SKILL.md",
        0o600,
    )


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


def patch_adapter() -> None:
    adapter = HERMES_HOME / "hermes-agent" / "plugins" / "platforms" / "discord" / "adapter.py"
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="only report whether deployment is current")
    args = parser.parse_args()
    if args.check:
        checks = [
            HERMES_HOME / "scripts" / name
            for name in (
                "maa_common.py",
                "maa_plan_card.py",
                "maa_activity_watch.py",
                "maa_depot.py",
                "maa_daily.py",
                "maa_action.py",
                "maa_clearing.py",
                "maa_copilot.py",
                "maa_farming.py",
                "maa_roguelike.py",
                "maa_copilot_worker.py",
                "maa_roguelike_worker.py",
                "maa_skland.py",
                "ocrbox",
            )
        ]
        checks.extend(target for _source, target in maa_config_file_pairs())
        missing = [str(path) for path in checks if not path.is_file()]
        mismatched = content_mismatches(maa_config_content_checked_pairs())
        ok = not missing and not mismatched
        print(
            json.dumps(
                {"ok": ok, "missing": missing, "mismatched": mismatched},
                ensure_ascii=False,
            )
        )
        return 0 if ok else 1

    channel_id = resolve_channel_id()
    deploy_scripts()
    deploy_state(channel_id)
    deploy_maa_config()
    deploy_skill()
    deploy_skland_token()
    deploy_ocrbox()
    patch_adapter()
    patch_local_patches()
    patch_env(channel_id)
    patch_config_yaml(channel_id)
    print(json.dumps({"ok": True, "changed": changed, "backup": str(backup_root)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
