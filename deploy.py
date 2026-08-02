#!/usr/bin/env python3
"""Idempotently deploy the reviewed MAA workflow into this user's Hermes home."""

from __future__ import annotations

import argparse
import json
import os
import shutil
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
    for name in ("maa_common.py", "maa_plan_card.py", "maa_daily.py", "maa_action.py"):
        copy_file(ROOT / "scripts" / name, target / name, 0o700)


def deploy_state(channel_id: str) -> None:
    target = HERMES_HOME / "state" / "maa"
    ensure_dir(target)
    ensure_dir(target / "runs")
    ensure_dir(target / "am")
    ensure_dir(target / "pm")

    desired_config = load_json(ROOT / "state" / "config.json", {})
    if isinstance(desired_config, dict):
        # The repository only carries a placeholder; never let it reach disk.
        desired_config["channel_id"] = channel_id
    current_config = load_json(target / "config.json", {})
    if not isinstance(current_config, dict):
        raise RuntimeError("existing MAA config.json is not an object")
    current_config.update(desired_config)
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
    for kind, name in (
        ("profiles", "default.toml"),
        ("tasks", "daily.toml"),
        ("tasks", "infrast-smoke.toml"),
    ):
        copy_file(
            ROOT / "config" / "maa-cli" / kind / name,
            MAA_CONFIG_HOME / kind / name,
            0o600,
        )
    copy_file(ROOT / "config" / "maa-cli" / "cli.toml", MAA_CONFIG_HOME / "cli.toml", 0o600)


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
    if '"maa_action.py"' not in text.split("_HERMES_BUTTON_SCRIPTS", 1)[1].split("\n", 2)[0]:
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


def patch_config_yaml(channel_id: str) -> None:
    path = HERMES_HOME / "config.yaml"
    text = path.read_text(encoding="utf-8")
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

    old_prompt = (
        f"      '{channel_id}': '你是 MAA 长草助手。只处理今天的关卡、临期理智药、跳过、立即运行和日志；"
        "所有读取与变更必须遵守 maa-operator 技能并通过 maa_action.py，禁止直接写状态文件，碎石永久关闭。'\n"
    )
    prompt = (
        f"      '{channel_id}': '你是 MAA 长草助手。只处理今天的关卡、跳过和最近结果；"
        "所有读取与变更必须遵守 maa-operator 技能并通过 maa_action.py，禁止直接写状态文件，碎石永久关闭。'\n"
    )
    if old_prompt in text:
        text = text.replace(old_prompt, prompt)
    elif prompt not in text:
        marker = "timezone: Etc/UTC"
        if text.count(marker) != 1:
            raise RuntimeError("timezone marker is ambiguous")
        text = text.replace(marker, prompt + marker)
    write_text(path, text, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="only report whether deployment is current")
    args = parser.parse_args()
    if args.check:
        checks = [
            HERMES_HOME / "scripts" / name
            for name in ("maa_common.py", "maa_plan_card.py", "maa_daily.py", "maa_action.py")
        ]
        missing = [str(path) for path in checks if not path.is_file()]
        print(json.dumps({"ok": not missing, "missing": missing}, ensure_ascii=False))
        return 0 if not missing else 1

    channel_id = resolve_channel_id()
    deploy_scripts()
    deploy_state(channel_id)
    deploy_maa_config()
    deploy_skill()
    patch_adapter()
    patch_local_patches()
    patch_env(channel_id)
    patch_config_yaml(channel_id)
    print(json.dumps({"ok": True, "changed": changed, "backup": str(backup_root)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
