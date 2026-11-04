"""部署（`deploy.py`）：`--check` 与 Hermes 提示词补丁。

从原 `tests/test_maa.py` 按领域拆出；共用桩见 `_helpers.py`。
"""
from __future__ import annotations

import contextlib
import os
import io
import json
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

# 必须先于任何 scripts/ 模块：把 HOME 换成临时目录，碰真实 home / 外网当场炸。
import _isolation  # noqa: E402
import _helpers  # noqa: E402
import maa_plan_card  # noqa: E402
import maa_playcover  # noqa: E402
import deploy  # noqa: E402
from _helpers import needs_yaml  # noqa: E402


# 设备层桩见 `_isolation.NoRealGame`：开游戏、TERM、点屏幕、pgrep 一律当场炸。
_DEVICE_GUARDS: list = []
# 网络 / 游戏数据 / PyYAML 三组模块级桩，见 `_helpers.start_module_patches()`。
_MODULE_PATCHES: list = []


def setUpModule() -> None:
    _DEVICE_GUARDS[:] = _isolation.guard_devices()
    _MODULE_PATCHES[:] = _helpers.start_module_patches()


def tearDownModule() -> None:
    _helpers.stop_patches(_MODULE_PATCHES)
    for item in reversed(_DEVICE_GUARDS):
        item.stop()
    _isolation.assert_shared_home_clean()


class DeployCheckTests(unittest.TestCase):
    """`deploy.py --check`：只读核对脚本 / 技能内容、adapter 补丁、gateway 解释器与 venv。

    每条用例自己一套假 home（`USER_HOME` / `HERMES_HOME` / `MAA_CONFIG_HOME` /
    `HERMES_VENV_PYTHON` 全部 patch 到这里），先按仓库内容「部署干净」，再逐项弄坏。
    假解释器是 `/bin/sh` 小脚本，只打印版本号。
    """

    UNPATCHED_ADAPTER = '''_HERMES_BUTTON_SCRIPTS = frozenset({"agent_book_action.py"})
    async def _run_hermes_button_script(
        self,
        entry: dict[str, Any],
    ) -> tuple[bool, str]:
        raw_args = entry.get("args", [])
        if not isinstance(raw_args, list) or len(raw_args) > 10:
            return False, "按钮动作参数无效。"
        args = [str(value) for value in raw_args]
        if any(len(value) > 200 for value in args):
            return False, "按钮动作参数过长。"
                ok, message = await self._run_hermes_button_script(entry)
'''

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="deploy-check-")
        self.addCleanup(tmp.cleanup)
        self.user_home = Path(tmp.name) / "home"
        self.hermes = self.user_home / ".hermes"
        self.venv_python = self.hermes / "hermes-agent" / "venv" / "bin" / "python"
        for item in (
            patch.object(deploy, "USER_HOME", self.user_home),
            patch.object(deploy, "HERMES_HOME", self.hermes),
            patch.object(deploy, "MAA_CONFIG_HOME", self.user_home / ".config" / "maa-cli"),
            patch.object(deploy, "HERMES_VENV_PYTHON", self.venv_python),
            patch.object(deploy, "changed", []),
        ):
            item.start()
            self.addCleanup(item.stop)

        pairs = [*deploy.script_file_pairs(), deploy.skill_file_pair(), *deploy.maa_config_file_pairs()]
        for source, target in pairs:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        for name in deploy.COMPILED_HELPERS:
            (self.hermes / "scripts" / name).write_bytes(b"\xcf\xfa\xed\xfe compiled")
        deploy.adapter_path().parent.mkdir(parents=True)
        deploy.adapter_path().write_text(
            deploy.patch_adapter_source(self.UNPATCHED_ADAPTER), encoding="utf-8"
        )
        self.fake_python(self.venv_python, "3.11.15")
        self.write_plist(str(self.venv_python))

        # 组件版本探测全部桩掉：默认装的正好是记录里的版本。app 是假的 Info.plist，
        # 走真 `bundle_short_version()`；maa / git 子进程换成 Mock。
        self.verified = deploy.load_verified_versions()
        apps = Path(tmp.name) / "Applications"
        self.playcover_app = apps / "PlayCover.app"
        self.betterdisplay_app = apps / "BetterDisplay.app"
        self.arknights_app = self.user_home / deploy.ARKNIGHTS_APP_RELATIVE_PATH
        self.write_app_version(self.playcover_app / "Contents", self.verified["PlayCover"])
        self.write_app_version(self.betterdisplay_app / "Contents", self.verified["BetterDisplay"])
        self.write_app_version(self.arknights_app, self.verified["Arknights"])
        for item in (
            patch.object(deploy, "PLAYCOVER_APP", self.playcover_app),
            patch.object(deploy, "BETTERDISPLAY_APP", self.betterdisplay_app),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.real_probe_maa_versions = deploy.probe_maa_versions
        self.real_probe_hermes_commit = deploy.probe_hermes_commit
        maa_probe = patch.object(
            deploy,
            "probe_maa_versions",
            return_value=(self.verified["maa-cli"], self.verified["MaaCore"]),
        )
        git_probe = patch.object(deploy, "probe_hermes_commit", return_value=self.verified["Hermes"])
        self.maa_probe = maa_probe.start()
        self.git_probe = git_probe.start()
        self.addCleanup(maa_probe.stop)
        self.addCleanup(git_probe.stop)

    @staticmethod
    def write_app_version(directory: Path, version: str | None) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        document: dict = {"CFBundleIdentifier": "example.app"}
        if version is not None:
            document["CFBundleShortVersionString"] = version
        with (directory / "Info.plist").open("wb") as handle:
            plistlib.dump(document, handle)

    @staticmethod
    def fake_python(path: Path, version: str, exit_code: int = 0) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/bin/sh\necho {version}\nexit {exit_code}\n", encoding="utf-8")
        path.chmod(0o755)

    def write_plist(self, program: str | None) -> None:
        plist = deploy.gateway_plist_path()
        plist.parent.mkdir(parents=True, exist_ok=True)
        document: dict = {"Label": "ai.hermes.gateway"}
        if program is not None:
            document["ProgramArguments"] = [program, "-m", "hermes_cli.main", "gateway", "run"]
        with plist.open("wb") as handle:
            plistlib.dump(document, handle)

    def snapshot(self) -> dict[str, tuple]:
        state: dict[str, tuple] = {}
        for directory, dirs, files in os.walk(self.user_home):
            for name in [*dirs, *files]:
                path = Path(directory) / name
                info = path.lstat()
                content = path.read_bytes() if path.is_file() and not path.is_symlink() else None
                state[str(path)] = (info.st_mode, info.st_mtime_ns, info.st_size, content)
        return state

    def run_check(self) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = deploy.main(["--check"])
        return code, out.getvalue()

    def test_script_names_match_the_repository_scripts(self) -> None:
        self.assertEqual(len(deploy.SCRIPT_NAMES), len(set(deploy.SCRIPT_NAMES)))
        self.assertEqual(
            set(deploy.SCRIPT_NAMES),
            {path.name for path in (deploy.ROOT / "scripts").glob("*.py")},
        )

    def test_deploy_scripts_copies_exactly_the_script_names(self) -> None:
        for _source, target in deploy.script_file_pairs():
            target.unlink()
        for name in deploy.COMPILED_HELPERS:
            (self.hermes / "scripts" / name).unlink()
        deploy.deploy_scripts()
        self.assertEqual(
            sorted(path.name for path in (self.hermes / "scripts").iterdir()),
            sorted(deploy.SCRIPT_NAMES),
        )

    def test_a_clean_deployment_is_ok_and_the_check_writes_nothing(self) -> None:
        before = self.snapshot()
        code, output = self.run_check()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(deploy.changed, [])
        self.assertFalse((self.hermes / "backups").exists())

        self.assertEqual(code, 0)
        self.assertEqual(output.count("\n"), 1, "输出必须是一行 JSON")
        report = json.loads(output)
        self.assertEqual(
            list(report),
            ["ok", "missing", "mismatched", "adapter", "gateway_python", "venvs", "versions", "warnings"],
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["missing"], [])
        self.assertEqual(report["mismatched"], [])
        self.assertEqual(report["adapter"], "patched")
        self.assertEqual(
            report["gateway_python"],
            {
                "plist": str(self.user_home / "Library" / "LaunchAgents" / "ai.hermes.gateway.plist"),
                "program": str(self.venv_python),
                "version": "3.11.15",
                "expected": str(self.venv_python),
                "matches": True,
            },
        )
        self.assertEqual(report["venvs"], {"venv": "3.11.15", ".venv": None})
        self.assertEqual(
            report["versions"],
            {
                name: {"installed": self.verified[name], "verified": self.verified[name], "match": True}
                for name in deploy.CHECKED_COMPONENTS
            },
        )
        self.assertEqual(list(report["versions"]), list(deploy.CHECKED_COMPONENTS))
        self.assertEqual(report["warnings"], [])

    def test_the_verified_versions_record_is_well_formed(self) -> None:
        document = json.loads(deploy.VERIFIED_VERSIONS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(document["schema_version"], 1)
        components = document["components"]
        self.assertEqual(
            set(components),
            {"maa-cli", "MaaCore", "PlayCover", "MaaTools", "Hermes", "BetterDisplay", "Arknights"},
        )
        for name, entry in components.items():
            with self.subTest(component=name):
                self.assertIsInstance(entry["version"], str)
                self.assertTrue(entry["version"])
                datetime.strptime(entry["verified_at"], "%Y-%m-%d")
        self.assertEqual(components["PlayCover"]["branch"], "maa")
        self.assertTrue(components["PlayCover"]["version"].endswith(".maa"))
        # VERN 要游戏在跑才读得到：只记录，不进 --check。
        self.assertIs(components["MaaTools"]["checked_by_deploy"], False)
        self.assertNotIn("MaaTools", deploy.CHECKED_COMPONENTS)
        self.assertEqual(set(deploy.CHECKED_COMPONENTS), set(components) - {"MaaTools"})
        self.assertGreaterEqual(
            int(components["MaaTools"]["version"]), maa_playcover.MIN_MAATOOLS_VERSION
        )

    def test_version_drift_only_warns_and_the_check_writes_nothing(self) -> None:
        self.write_app_version(self.playcover_app / "Contents", "4.1.0.maa")
        self.write_app_version(self.arknights_app, "2.8.01")
        self.maa_probe.return_value = ("v0.8.0", self.verified["MaaCore"])
        self.git_probe.return_value = "0" * 40
        before = self.snapshot()
        code, output = self.run_check()
        self.assertEqual(self.snapshot(), before)
        report = json.loads(output)
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        versions = report["versions"]
        self.assertEqual(
            versions["PlayCover"],
            {"installed": "4.1.0.maa", "verified": self.verified["PlayCover"], "match": False},
        )
        self.assertEqual(versions["maa-cli"]["installed"], "v0.8.0")
        self.assertFalse(versions["maa-cli"]["match"])
        self.assertTrue(versions["MaaCore"]["match"])
        self.assertFalse(versions["Hermes"]["match"])
        self.assertFalse(versions["Arknights"]["match"])
        self.assertTrue(versions["BetterDisplay"]["match"])
        self.assertEqual(len(report["warnings"]), 1, report["warnings"])
        warning = report["warnings"][0]
        self.assertIn(f"maa-cli {self.verified['maa-cli']} → v0.8.0", warning)
        self.assertIn(f"PlayCover {self.verified['PlayCover']} → 4.1.0.maa", warning)
        self.assertIn("Arknights", warning)
        self.assertIn("Hermes", warning)
        self.assertNotIn("MaaCore", warning)
        self.assertNotIn("BetterDisplay", warning)

    def test_unavailable_versions_are_null_and_warned_without_raising(self) -> None:
        shutil.rmtree(self.betterdisplay_app)
        self.write_app_version(self.arknights_app, None)
        self.maa_probe.return_value = ("v0.7.5", "")
        self.git_probe.side_effect = RuntimeError("git rev-parse 失败（exit 128）：not a git repository")
        report = deploy.check_deployment()
        self.assertTrue(report["ok"])
        versions = report["versions"]
        for name in ("MaaCore", "Hermes", "BetterDisplay", "Arknights"):
            with self.subTest(component=name):
                self.assertEqual(
                    versions[name],
                    {"installed": None, "verified": self.verified[name], "match": None},
                )
        self.assertTrue(versions["maa-cli"]["match"])
        self.assertTrue(versions["PlayCover"]["match"])
        warnings = report["warnings"]
        self.assertIn("MaaCore 版本取不到：maa version 没有输出这一项", warnings)
        self.assertIn("Hermes 版本取不到：git rev-parse 失败（exit 128）：not a git repository", warnings)
        self.assertIn(f"BetterDisplay 版本取不到：{self.betterdisplay_app} 下没有 Info.plist", warnings)
        self.assertIn(
            f"Arknights 版本取不到：{self.arknights_app / 'Info.plist'} 里没有 CFBundleShortVersionString",
            warnings,
        )
        self.assertEqual(len(warnings), 4, warnings)

        # maa 本身起不来（探测抛任何异常）也只是两项取不到。
        self.maa_probe.side_effect = OSError("No such file or directory: maa")
        self.maa_probe.return_value = None
        versions = deploy.check_deployment()["versions"]
        self.assertIsNone(versions["maa-cli"]["installed"])
        self.assertIsNone(versions["MaaCore"]["installed"])

    def test_a_missing_record_warns_and_reports_installed_versions_only(self) -> None:
        with patch.object(deploy, "VERIFIED_VERSIONS_PATH", self.user_home / "nope.json"):
            report = deploy.check_deployment()
        self.assertTrue(report["ok"])
        self.assertEqual(
            report["versions"]["PlayCover"],
            {"installed": self.verified["PlayCover"], "verified": None, "match": None},
        )
        self.assertEqual(len(report["warnings"]), 1, report["warnings"])
        self.assertTrue(report["warnings"][0].startswith("真机验证版本记录不可用："))

        bad = self.user_home / "bad.json"
        bad.write_text(json.dumps({"schema_version": 2, "components": {}}), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "schema_version 不是 1"):
            deploy.load_verified_versions(bad)

    def test_hermes_commit_matches_by_prefix(self) -> None:
        full = "f6b3e394b7c9e5219fbea7281f65e01b48402631"
        self.assertTrue(deploy.version_matches("Hermes", full, "f6b3e394b7"))
        self.assertTrue(deploy.version_matches("Hermes", full, full.upper()))
        self.assertFalse(deploy.version_matches("Hermes", full, "f6b3e3"))
        self.assertFalse(deploy.version_matches("Hermes", full, "a6b3e394b7"))
        self.assertFalse(deploy.version_matches("PlayCover", "4.0.0.maa", "4.0.0"))

    def test_maa_versions_are_probed_through_the_plan_card_parser(self) -> None:
        config_path = self.hermes / "state" / "maa" / "config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({"maa_bin": "/fake/maa"}), encoding="utf-8")
        calls: list[list[str]] = []

        def fake_run(argv, *, env, timeout):
            calls.append(argv)
            return 0, "maa-cli v0.7.6\nMaaCore v6.22.0", ""

        with patch.object(maa_plan_card, "_run_captured", side_effect=fake_run):
            self.assertEqual(self.real_probe_maa_versions(), ("v0.7.6", "v6.22.0"))
        self.assertEqual(calls, [["/fake/maa", "version"]])

    def test_hermes_commit_is_read_with_git_rev_parse(self) -> None:
        repo = self.hermes / "hermes-agent"
        completed = subprocess.CompletedProcess([], 0, stdout="abc1234def\n", stderr="")
        with patch.object(deploy.subprocess, "run", return_value=completed) as run:
            self.assertEqual(self.real_probe_hermes_commit(), "abc1234def")
        self.assertEqual(run.call_args.args[0], ["git", "-C", str(repo), "rev-parse", "HEAD"])
        failed = subprocess.CompletedProcess([], 128, stdout="", stderr="fatal: not a git repository")
        with patch.object(deploy.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "exit 128"):
                self.real_probe_hermes_commit()

    def test_script_and_skill_drift_is_mismatched_but_helpers_are_only_existence_checked(self) -> None:
        script = self.hermes / "scripts" / "maa_daily.py"
        skill = self.hermes / "skills" / "games" / "maa-operator" / "SKILL.md"
        script.write_bytes(script.read_bytes() + b"# drift\n")
        skill.write_bytes(b"old skill\n")
        before = self.snapshot()
        code, output = self.run_check()
        self.assertEqual(self.snapshot(), before)
        report = json.loads(output)
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["mismatched"], [str(script), str(skill)])
        self.assertEqual(report["missing"], [])

        skill.unlink()
        (self.hermes / "scripts" / "winmove").unlink()
        report = deploy.check_deployment()
        self.assertEqual(
            report["missing"], [str(self.hermes / "scripts" / "winmove"), str(skill)]
        )
        self.assertEqual(report["mismatched"], [str(script)])

    def test_adapter_patch_state_is_reported_without_touching_the_file(self) -> None:
        adapter = deploy.adapter_path()
        cases = [
            (self.UNPATCHED_ADAPTER, "needs_patch"),
            ("class DiscordAdapter:\n    pass\n", None),
        ]
        for text, expected in cases:
            with self.subTest(expected=expected):
                adapter.write_text(text, encoding="utf-8")
                before = self.snapshot()
                code, output = self.run_check()
                self.assertEqual(self.snapshot(), before)
                report = json.loads(output)
                if expected is None:
                    self.assertTrue(
                        report["adapter"].startswith(
                            "anchor_mismatch: Hermes button allowlist no longer matches"
                        ),
                        report["adapter"],
                    )
                else:
                    self.assertEqual(report["adapter"], expected)
                self.assertFalse(report["ok"])
                self.assertEqual(code, 1)
        adapter.unlink()
        report = deploy.check_deployment()
        self.assertEqual(report["adapter"], "missing")
        self.assertFalse(report["ok"])
        self.assertFalse(adapter.exists())

    def test_a_gateway_on_another_interpreter_fails_the_check(self) -> None:
        other = self.hermes / "hermes-agent" / ".venv" / "bin" / "python"
        self.fake_python(other, "3.12.13")
        self.write_plist(str(other))
        report = deploy.check_deployment()
        gateway = report["gateway_python"]
        self.assertEqual(gateway["program"], str(other))
        self.assertEqual(gateway["version"], "3.12.13")
        self.assertFalse(gateway["matches"])
        self.assertFalse(report["ok"])

    def test_a_venv_symlinked_to_the_same_binary_still_does_not_match(self) -> None:
        # 不同 venv 的 bin/python 常指向同一个 uv 解释器：只比文件 realpath 会误判一致。
        other = self.hermes / "hermes-agent" / ".venv" / "bin" / "python"
        other.parent.mkdir(parents=True)
        other.symlink_to(self.venv_python)
        self.write_plist(str(other))
        self.assertFalse(deploy.check_deployment()["gateway_python"]["matches"])

    def test_a_symlinked_hermes_home_still_matches(self) -> None:
        alias = self.user_home / "hermes-alias"
        alias.symlink_to(self.hermes)
        self.write_plist(str(alias / "hermes-agent" / "venv" / "bin" / "python"))
        report = deploy.check_deployment()
        self.assertTrue(report["gateway_python"]["matches"])
        self.assertTrue(report["ok"])

    def test_a_missing_or_unreadable_plist_is_reported(self) -> None:
        plist = deploy.gateway_plist_path()
        plist.unlink()
        gateway = deploy.check_deployment()["gateway_python"]
        self.assertEqual(gateway["error"], "plist 不存在")
        self.assertIsNone(gateway["program"])
        self.assertFalse(gateway["matches"])

        plist.write_bytes(b"not a plist at all")
        report = deploy.check_deployment()
        self.assertTrue(report["gateway_python"]["error"].startswith("plist 读不出："))
        self.assertFalse(report["ok"])

        self.write_plist(None)
        gateway = deploy.check_deployment()["gateway_python"]
        self.assertEqual(gateway["error"], "plist 里没有 ProgramArguments[0]")
        self.assertFalse(gateway["matches"])

    def test_version_probe_failures_are_recorded_not_raised(self) -> None:
        self.fake_python(self.venv_python, "", exit_code=3)
        report = deploy.check_deployment()
        self.assertIsNone(report["gateway_python"]["version"])
        self.assertTrue(report["gateway_python"]["error"].startswith("取版本失败：exit 3"))
        self.assertTrue(report["gateway_python"]["matches"])
        self.assertTrue(report["venvs"]["venv"].startswith("error: exit 3"))
        # 路径一致但解释器跑不起来：gateway 实际已经起不来，`ok` 必须是假。
        self.assertFalse(report["ok"])

    def test_a_dangling_venv_python_fails_the_check_even_though_the_path_matches(self) -> None:
        # uv 升级 Python 删掉旧解释器后，venv/bin/python 成了断链：路径照样一致。
        self.venv_python.unlink()
        self.venv_python.symlink_to(self.user_home / "uv-python-removed" / "python3.11")
        code, output = self.run_check()
        report = json.loads(output)
        gateway = report["gateway_python"]
        self.assertTrue(gateway["matches"])
        self.assertIsNone(gateway["version"])
        self.assertTrue(gateway["error"].startswith("取版本失败："), gateway["error"])
        self.assertFalse(report["ok"])
        self.assertEqual(code, 1)

        with self.assertRaisesRegex(RuntimeError, "FileNotFoundError"):
            deploy.python_version(self.user_home / "no-such-python")
        with patch.object(
            deploy.subprocess, "run", side_effect=subprocess.TimeoutExpired("python", 10)
        ), self.assertRaisesRegex(RuntimeError, "TimeoutExpired"):
            deploy.python_version(self.venv_python)

    def test_coexisting_venvs_only_warn(self) -> None:
        self.fake_python(self.hermes / "hermes-agent" / ".venv" / "bin" / "python", "3.12.13")
        code, output = self.run_check()
        report = json.loads(output)
        self.assertEqual(report["venvs"], {"venv": "3.11.15", ".venv": "3.12.13"})
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("venv 与 .venv 并存", report["warnings"][0])
        self.assertTrue(report["ok"])
        self.assertEqual(code, 0)


class DeployPromptPatchTests(unittest.TestCase):
    """步骤 1 部署踩到的坑：Hermes 会把提示词重排成无引号、折行的 plain scalar。"""

    OLD = deploy.OLD_MAA_CHANNEL_PROMPTS[1]

    def _document(self, body: str) -> str:
        return (
            "timezone: Etc/UTC\n"
            "platforms:\n"
            "  discord:\n"
            "    channels:\n"
            "      - id: '123'\n"
            "        skills:\n"
            "          - maa-operator\n"
            "    channel_prompts:\n"
            f"{body}"
            "      '456': '别的频道'\n"
        )

    @needs_yaml
    def test_a_reflowed_two_line_prompt_is_replaced_in_place(self) -> None:
        # 实例：Hermes 把这一条重排成无引号、在 `maa-operator` 后折行的 plain scalar。
        head, _space, tail = self.OLD.rpartition(" ")
        text = self._document(f"      '123': {head}\n        {tail}\n")
        patched = deploy.patch_config_yaml_text(text, "123")
        document = deploy.parse_yaml(patched)
        prompts = document["platforms"]["discord"]["channel_prompts"]
        self.assertEqual(prompts["123"], deploy.MAA_CHANNEL_PROMPT)
        self.assertEqual(prompts["456"], "别的频道")
        self.assertEqual(document["timezone"], "Etc/UTC")
        # 折行的续行整条被吃掉，旧文案一个字都不留。
        self.assertNotIn("只处理今天的关卡", patched)
        self.assertEqual(len(patched.splitlines()), len(text.splitlines()) - 1)

    @needs_yaml
    def test_a_single_quoted_prompt_is_still_replaced(self) -> None:
        text = self._document(f"      '123': '{self.OLD}'\n")
        patched = deploy.patch_config_yaml_text(text, "123")
        prompts = deploy.parse_yaml(patched)["platforms"]["discord"]["channel_prompts"]
        self.assertEqual(prompts["123"], deploy.MAA_CHANNEL_PROMPT)

    @needs_yaml
    def test_an_already_current_prompt_is_left_alone(self) -> None:
        text = self._document(f"      '123': '{deploy.MAA_CHANNEL_PROMPT}'\n")
        self.assertEqual(deploy.patch_config_yaml_text(text, "123"), text)

    @needs_yaml
    def test_an_unexpected_prompt_still_refuses_to_deploy(self) -> None:
        text = self._document("      '123': 别人手工写的文案\n")
        with self.assertRaises(RuntimeError) as caught:
            deploy.patch_config_yaml_text(text, "123")
        self.assertIn("非预期文案", str(caught.exception))

    @needs_yaml
    def test_a_missing_prompt_is_inserted(self) -> None:
        text = self._document("")
        patched = deploy.patch_config_yaml_text(text, "123")
        prompts = deploy.parse_yaml(patched)["platforms"]["discord"]["channel_prompts"]
        self.assertEqual(prompts["123"], deploy.MAA_CHANNEL_PROMPT)

    def test_retired_clearing_keys_are_swept_from_the_live_config(self) -> None:
        self.assertIn("clearing_enabled", deploy.RETIRED_CONFIG_KEYS)
        # 模拟器 → PlayCover 退役的设备键也在这里扫掉。
        for key in ("adb_path", "adb_address", "emulator_app", "game_package"):
            self.assertIn(key, deploy.RETIRED_CONFIG_KEYS)
        self.assertEqual(len(set(deploy.RETIRED_CONFIG_KEYS)), len(deploy.RETIRED_CONFIG_KEYS))
        repo = json.loads((ROOT / "state" / "config.json").read_text("utf-8"))
        for key in deploy.RETIRED_CONFIG_KEYS:
            self.assertNotIn(key, repo)
        for key in ("auto_copilot_min_lead_minutes", "copilot_raid_switch_task",
                    "annihilation_sanity"):
            self.assertIn(key, repo)



if __name__ == "__main__":
    unittest.main()
