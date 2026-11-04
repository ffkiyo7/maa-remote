"""计划卡（`maa_plan_card`）：活动快照、按钮 / 下拉、早晚两张卡、上游更新。

从原 `tests/test_maa.py` 按领域拆出；共用桩见 `_helpers.py`。
"""
from __future__ import annotations

import contextlib
import os
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

# 必须先于任何 scripts/ 模块：把 HOME 换成临时目录，碰真实 home / 外网当场炸。
import _isolation  # noqa: E402
import _helpers  # noqa: E402
import maa_common  # noqa: E402
import maa_farming  # noqa: E402
import maa_plan_card  # noqa: E402
import deploy  # noqa: E402
from _helpers import BEIJING, _offline_market, needs_yaml  # noqa: E402


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


class DiscordCardTests(unittest.TestCase):
    def test_activity_events_parser(self) -> None:
        output = """Opening side story stages:
- SideStory「直到大地变成一颗酸橙」
  - TO-9: 晶体元件
  - TO-8: 凝胶
  - TO-7: 糖组
Opening mini games:
- 黑流树海刷钱
"""
        self.assertEqual(
            maa_plan_card.parse_activity_events(output),
            [
                {
                    "title": "SideStory「直到大地变成一颗酸橙」",
                    "stages": ["TO-9", "TO-8", "TO-7"],
                }
            ],
        )
        self.assertFalse(maa_plan_card.all_supply_stages_open(output))
        self.assertTrue(maa_plan_card.all_supply_stages_open("资源收集\n"))

    def test_activity_snapshot_ignores_stage_shaped_minigame_bullets(self) -> None:
        output = """Opening side story stages:
- SideStory「活动」
  - NEW-8: 材料
Opening mini games:
- XX-1: 这不是活动作战关
"""
        self.assertEqual(maa_plan_card.activity_snapshot(output)["stages"], ["NEW-8"])

    def test_activity_snapshot_returns_empty_during_recognized_gap(self) -> None:
        output = "Opening mini games:\n- XX-1: 小游戏\n"
        self.assertEqual(
            maa_plan_card.activity_snapshot(output),
            {
                "stages": [],
                "events": [],
                "all_supply_stages_open": False,
            },
        )

    def test_activity_snapshot_fails_closed_when_upstream_format_changes(self) -> None:
        for output in ("", "not a maa activity response\n"):
            with self.subTest(output=output):
                with self.assertRaisesRegex(RuntimeError, "输出格式已变化"):
                    maa_plan_card.activity_snapshot(output)

    def test_activity_snapshot_fails_closed_on_a_decorated_heading(self) -> None:
        """A heading the parser cannot match must raise, not read as a gap.

        The gate and parse_activity_events() share is_side_story_heading(), so
        a reflowed or decorated heading can never silently produce zero events
        and clear the live stage list.
        """
        for heading in (
            "Opening side story stages: (2)",
            "Opening side story stages:!",
            "**Opening side story stages:**",
            "  Opening side story stages:",
        ):
            output = f"{heading}\n- SideStory「活动」\n  - NEW-8: 材料\nOpening mini games:\n"
            with self.subTest(heading=heading):
                with self.assertRaisesRegex(RuntimeError, "输出格式已变化"):
                    maa_plan_card.activity_snapshot(output)

    def test_snapshot_gate_and_parser_agree_on_what_a_heading_is(self) -> None:
        """The gate may only fall through to the parser on headings it parses."""
        for heading in (
            "Opening side story stages:",
            "Opening side story stages:   ",
            "Opening side story stages: (2)",
            "  Opening side story stages:",
        ):
            output = f"{heading}\n- SideStory「活动」\n  - NEW-8: 材料\n"
            with self.subTest(heading=heading):
                parsed = bool(maa_plan_card.parse_activity_events(output))
                try:
                    gated = bool(maa_plan_card.activity_snapshot(output)["stages"])
                except RuntimeError:
                    gated = False
                self.assertEqual(parsed, gated)

    def test_empty_activity_snapshot_clears_persisted_stages(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config_path = Path(raw) / "config.json"
            config: maa_common.Config = {"activity_stages": ["OLD-8"]}
            with patch.object(maa_plan_card, "CONFIG_PATH", config_path):
                maa_plan_card.persist_activity_snapshot(
                    config,
                    maa_plan_card.activity_snapshot(
                        "Opening mini games:\n- 当前开放小游戏\n"
                    ),
                )
            persisted = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["activity_stages"], [])
        self.assertEqual(persisted["activity_stages"], [])

    def test_activity_snapshot_does_not_clear_on_unrecognized_output(self) -> None:
        config: maa_common.Config = {"activity_stages": ["OLD-8"]}
        with self.assertRaisesRegex(RuntimeError, "输出格式已变化"):
            maa_plan_card.activity_snapshot("garbled output")
        self.assertEqual(config["activity_stages"], ["OLD-8"])

    def test_persist_activity_snapshot_drops_the_obsolete_event_cache(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config_path = Path(raw) / "config.json"
            config = cast(maa_common.Config, {"activity_events": [{"title": "旧活动"}]})  # 故意塞退役键
            with patch.object(maa_plan_card, "CONFIG_PATH", config_path):
                maa_plan_card.persist_activity_snapshot(
                    config,
                    {
                        "stages": ["NEW-8"],
                        "events": [{"title": "新活动", "stages": ["NEW-8"]}],
                        "all_supply_stages_open": False,
                    },
                )
            persisted = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertNotIn("activity_events", config)
        self.assertNotIn("activity_events", persisted)
        self.assertEqual(persisted["activity_stages"], ["NEW-8"])

    def test_buttons_fit_limits_and_registry_is_deterministic(self) -> None:
        plan = maa_common.normalize_plan({"stage": "CE-6", "medicine": "none"})
        components, registry = maa_plan_card.build_components(
            plan, {"channel_id": "123", "activity_stages": []}, "am"
        )
        self.assertLessEqual(len(components), 5)
        for row in components:
            self.assertLessEqual(len(row["components"]), 5)
        select = components[0]["components"][0]
        self.assertEqual(select["type"], 3)
        self.assertLessEqual(len(select["options"]), 25)
        self.assertEqual(sum(bool(option.get("default")) for option in select["options"]), 1)
        self.assertIn("select_args", registry["hermes:btn:maa:am:stage-select"])
        self.assertNotIn("hermes:btn:maa:run-now", registry)
        self.assertFalse(any("medicine" in key for key in registry))

    def test_the_card_has_exactly_one_select_and_one_button(self) -> None:
        """D2：计划卡只剩一个下拉 + 一个「跳过本场」按钮，恰好两行。"""
        cases: tuple[tuple[maa_common.PlanPhase, str], ...] = (
            ("am", "⏭️ 跳过本场（早场 08:00）"),
            ("pm", "⏭️ 跳过本场（晚场 22:00）"),
        )
        for phase, expect_label in cases:
            with self.subTest(phase=phase):
                rows, registry = maa_plan_card.build_components(
                    maa_common.normalize_plan({}),
                    {"channel_id": "123", "activity_stages": []},
                    phase,
                )
                self.assertEqual(len(rows), 2)
                self.assertEqual(len(rows[0]["components"]), 1)
                self.assertEqual(rows[0]["components"][0]["type"], 3)
                self.assertEqual(len(rows[1]["components"]), 1)
                self.assertEqual(rows[1]["components"][0]["type"], 2)
                self.assertEqual(rows[1]["components"][0]["label"], expect_label)
                # 被删掉的控件不许留在登记表里。
                self.assertEqual(len(registry), 2)
                for key in registry:
                    self.assertNotIn("explain", key)
                    self.assertNotIn("log", key)
                    self.assertNotIn("farming-select", key)
                self.assertFalse(
                    any(key.startswith("hermes:btn:maa-copilot:") for key in registry)
                )
                self.assertTrue(
                    all(entry["kind"] == "exec" for entry in registry.values())
                )

    def test_resuming_a_skipped_session_offers_the_restore_button(self) -> None:
        rows, registry = maa_plan_card.build_components(
            maa_common.normalize_plan({"skip": True}),
            {"channel_id": "123", "activity_stages": []},
            "pm",
        )
        button = rows[1]["components"][0]
        self.assertEqual(button["label"], "▶️ 恢复本场")
        self.assertEqual(
            registry[button["custom_id"]]["args"],
            ["plan", "skip-today", "off", "pm"],
        )

    def test_activity_dropdown_uses_configured_stages(self) -> None:
        plan = maa_common.normalize_plan({})
        components, registry = maa_plan_card.build_components(
            plan, {"channel_id": "123", "activity_stages": ["HS-9"]}, "pm"
        )
        select = components[0]["components"][0]
        option = next(option for option in select["options"] if option["label"] == "HS-9")
        self.assertEqual(
            registry["hermes:btn:maa:pm:stage-select"]["select_args"][option["value"]],
            ["plan", "set-stage", "HS-9", "pm"],
        )

    def test_every_control_carries_its_own_phase(self) -> None:
        """不变量 #8 的场次版：场次写死在登记表里，不从 Discord 直通。"""
        for phase in ("am", "pm"):
            with self.subTest(phase=phase):
                _rows, registry = maa_plan_card.build_components(
                    maa_common.normalize_plan({}),
                    {"channel_id": "123", "activity_stages": ["HS-9"]},
                    phase,
                )
                select = registry[f"hermes:btn:maa:{phase}:stage-select"]
                for value, args in select["select_args"].items():
                    self.assertTrue(value.startswith("choice:"))
                    self.assertEqual(args[0], "plan")
                    self.assertEqual(args[-1], phase)
                skip = registry[f"hermes:btn:maa:{phase}:skip"]
                self.assertEqual(skip["args"], ["plan", "skip-today", "on", phase])

    def test_dropdown_includes_every_regular_supply_stage(self) -> None:
        plan = maa_common.normalize_plan({"stage": "AP-5"})
        components, registry = maa_plan_card.build_components(
            plan,
            {
                "channel_id": "123",
                "activity_stages": [],
                "all_supply_stages_open": True,
            },
            "am",
        )
        select = components[0]["components"][0]
        options = {option["label"]: option for option in select["options"]}
        expected = {
            "AP-5 · 红票": "AP-5",
            "CA-5 · 技能书": "CA-5",
            "CE-6 · 龙门币": "CE-6",
            "LS-6 · 作战记录": "LS-6",
            "SK-5 · 基建材料": "SK-5",
        }
        select_args = registry["hermes:btn:maa:am:stage-select"]["select_args"]
        for label, stage in expected.items():
            option = options[label]
            self.assertEqual(
                select_args[option["value"]], ["plan", "set-stage", stage, "am"]
            )
        self.assertTrue(options["AP-5 · 红票"]["default"])

    def test_supply_dropdown_uses_cn_reset_and_weekday_schedule(self) -> None:
        # 03:59 Sunday is still Saturday's game day; 04:00 switches to Sunday.
        before_reset = maa_plan_card.current_supply_choices(
            {}, datetime(2026, 8, 2, 3, 59, tzinfo=BEIJING)
        )
        after_reset = maa_plan_card.current_supply_choices(
            {}, datetime(2026, 8, 2, 4, 0, tzinfo=BEIJING)
        )
        self.assertEqual(
            {stage for _label, stage, _description in before_reset},
            {"AP-5", "CE-6", "LS-6", "SK-5"},
        )
        self.assertEqual(
            {stage for _label, stage, _description in after_reset},
            {"AP-5", "CA-5", "CE-6", "LS-6"},
        )

    def test_retire_old_cards_only_patches_maa_cards_with_controls(self) -> None:
        messages = [
            {
                "id": "111",
                "embeds": [{"title": "🌱 MAA 今日计划"}],
                "components": [{"type": 1}],
            },
            {
                "id": "222",
                "embeds": [{"title": "别的卡片"}],
                "components": [{"type": 1}],
            },
        ]
        with patch.object(
            maa_plan_card,
            "discord_request",
            side_effect=[messages, {}],
        ) as request:
            result = maa_plan_card.retire_old_plan_cards("token", "123")
        self.assertEqual(result, {"retired": 1, "failed": 0})
        self.assertEqual(request.call_args_list[1].args[1:3], ("PATCH", "/channels/123/messages/111"))
        self.assertEqual(request.call_args_list[1].args[3], {"components": []})

    def test_adapter_patch_dispatches_only_registered_select_values(self) -> None:
        source = '''_HERMES_BUTTON_SCRIPTS = frozenset({"agent_book_action.py"})
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
        patched = deploy.patch_adapter_source(source)
        self.assertIn('"maa_action.py"', patched)
        self.assertIn('select_args.get(str(selected_values[0]))', patched)
        self.assertIn('(interaction.data or {}).get("values", [])', patched)
        self.assertEqual(deploy.patch_adapter_source(patched), patched)

    def test_adapter_patch_accepts_a_multiline_native_allowlist(self) -> None:
        # hermes-agent f6b3e394b7（2026-09-03）把 maa_action.py 原生写进多行的
        # frozenset；只看第一行的检测会把它误判成未知基线并拒绝部署。
        already_dispatching = deploy.patch_adapter_source(
            '''_HERMES_BUTTON_SCRIPTS = frozenset({"agent_book_action.py"})
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
        ).split("\n", 1)[1]
        native = (
            '_HERMES_BUTTON_SCRIPTS = frozenset({\n'
            '    "agent_book_action.py",\n'
            '    "maa_action.py",\n'
            '    "luxraykit_pr_action.py",\n'
            '})\n' + already_dispatching
        )
        self.assertEqual(deploy.patch_adapter_source(native), native)
        # 真正未知的基线仍然拒绝。
        with self.assertRaises(RuntimeError):
            deploy.patch_adapter_source(
                '_HERMES_BUTTON_SCRIPTS = frozenset({\n    "agent_book_action.py",\n})\n'
                + already_dispatching
            )

    # config.yaml 的键顺序归 Hermes 管：timezone 早于 platforms.discord。
    # 2026-09-04 的事故就是补丁拿 `timezone: Etc/UTC` 当锚点，把 prompt 插进了
    # 上一块 `skills:` 里。
    CONFIG_SAMPLE = (
        "delegation:\n"
        "  max_iterations: 50\n"
        "skills:\n"
        "  creation_nudge_interval: 15\n"
        "timezone: Etc/UTC\n"
        "platforms:\n"
        "  discord:\n"
        "    channel_skill_bindings:\n"
        "      - id: '1531685973501542422'\n"
        "        skills:\n"
        "          - agent-book-tutor\n"
        "    channel_prompts:\n"
        "      '1531685973501542422': '你是领读老师。'\n"
    )

    @needs_yaml
    def test_channel_prompt_lands_in_channel_prompts_whatever_the_key_order(self) -> None:
        patched = deploy.patch_config_yaml_text(self.CONFIG_SAMPLE, "1533066727313637457")
        parsed = deploy.parse_yaml(patched)
        self.assertEqual(parsed["timezone"], "Etc/UTC")
        # 事故的直接症状：prompt 落进了 skills 块。
        self.assertEqual(parsed["skills"], {"creation_nudge_interval": 15})
        discord = parsed["platforms"]["discord"]
        self.assertEqual(
            discord["channel_prompts"]["1533066727313637457"], deploy.MAA_CHANNEL_PROMPT
        )
        self.assertIn(
            {"id": "1533066727313637457", "skills": ["maa-operator"]},
            discord["channel_skill_bindings"],
        )

    @needs_yaml
    def test_channel_prompt_is_not_duplicated_and_old_wording_is_rewritten(self) -> None:
        once = deploy.patch_config_yaml_text(self.CONFIG_SAMPLE, "1533066727313637457")
        self.assertEqual(deploy.patch_config_yaml_text(once, "1533066727313637457"), once)

        # Hermes 重排/折叠过文本以后，只认字面量的旧实现会再插一遍；按解析值判断不会。
        head, tail = deploy.MAA_CHANNEL_PROMPT.split(" ", 1)
        reflowed = once.replace(
            f"      '1533066727313637457': '{deploy.MAA_CHANNEL_PROMPT}'\n",
            f"      '1533066727313637457': {head}\n        {tail}\n",
        )
        self.assertEqual(
            deploy.parse_yaml(reflowed)["platforms"]["discord"]["channel_prompts"][
                "1533066727313637457"
            ],
            deploy.MAA_CHANNEL_PROMPT,
        )
        self.assertEqual(
            deploy.patch_config_yaml_text(reflowed, "1533066727313637457"), reflowed
        )

        stale = self.CONFIG_SAMPLE.replace(
            "      '1531685973501542422': '你是领读老师。'\n",
            "      '1531685973501542422': '你是领读老师。'\n"
            f"      '1533066727313637457': '{deploy.OLD_MAA_CHANNEL_PROMPTS[0]}'\n",
        )
        upgraded = deploy.patch_config_yaml_text(stale, "1533066727313637457")
        self.assertEqual(upgraded.count("'1533066727313637457':"), 1)
        self.assertEqual(
            deploy.parse_yaml(upgraded)["platforms"]["discord"]["channel_prompts"][
                "1533066727313637457"
            ],
            deploy.MAA_CHANNEL_PROMPT,
        )

    @needs_yaml
    def test_unparsable_result_raises_before_the_file_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            config = home / "config.yaml"
            config.write_text(self.CONFIG_SAMPLE, encoding="utf-8")
            real = deploy.parse_yaml
            calls = {"n": 0}

            def flaky(text: str):
                calls["n"] += 1
                if calls["n"] == 1:
                    return real(text)
                raise ValueError("mapping values are not allowed in this context")

            with patch.object(deploy, "HERMES_HOME", home), patch.object(
                deploy, "parse_yaml", side_effect=flaky
            ):
                with self.assertRaises(RuntimeError):
                    deploy.patch_config_yaml("1533066727313637457")
            self.assertEqual(config.read_text(encoding="utf-8"), self.CONFIG_SAMPLE)

    @needs_yaml
    def test_an_already_broken_config_is_refused_instead_of_patched(self) -> None:
        broken = self.CONFIG_SAMPLE.replace(
            "timezone: Etc/UTC\n",
            "      '1533066727313637457': '你是 MAA 长草助手。'\ntimezone: Etc/UTC\n",
        )
        with self.assertRaises(RuntimeError):
            deploy.patch_config_yaml_text(broken, "1533066727313637457")

    def test_channel_id_is_resolved_and_placeholder_never_deploys(self) -> None:
        repo_config = deploy.load_json(deploy.ROOT / "state" / "config.json", {})
        self.assertEqual(repo_config["channel_id"], deploy.CHANNEL_PLACEHOLDER)

        with patch.dict(os.environ, {"MAA_CHANNEL_ID": "424242"}, clear=False):
            self.assertEqual(deploy.resolve_channel_id(), "424242")

        # Falls back to whatever this host already runs, so a redeploy on a
        # configured machine needs no argument.
        with patch.dict(os.environ, {"MAA_CHANNEL_ID": ""}, clear=False), patch.object(
            deploy, "load_json", return_value={"channel_id": "999"}
        ):
            self.assertEqual(deploy.resolve_channel_id(), "999")

        with patch.dict(os.environ, {"MAA_CHANNEL_ID": ""}, clear=False), patch.object(
            deploy, "load_json", return_value={"channel_id": deploy.CHANNEL_PLACEHOLDER}
        ):
            with self.assertRaises(RuntimeError):
                deploy.resolve_channel_id()

    def test_deploy_check_detects_maa_config_content_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "repo.toml"
            target = root / "runtime.toml"
            source.write_bytes(b"same\n")
            target.write_bytes(b"same\n")
            self.assertEqual(deploy.content_mismatches([(source, target)]), [])

            target.write_bytes(b"drifted\n")
            self.assertEqual(
                deploy.content_mismatches([(source, target)]),
                [str(target)],
            )

    def test_deploy_check_skips_daily_toml_but_still_guards_tasks_json(self) -> None:
        # `maa_daily.render_daily_toml()` rewrites the deployed daily.toml before
        # every run, so `--check` must not read that into deployment drift; the
        # user-resource overlay has no such excuse and must stay byte-compared.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            runtime = root / "runtime"
            for relative in deploy.MAA_CONFIG_RELATIVE_PATHS:
                for base in (repo / "config" / "maa-cli", runtime):
                    (base / relative).parent.mkdir(parents=True, exist_ok=True)
                    (base / relative).write_bytes(b"seed\n")

            with patch.object(deploy, "ROOT", repo), patch.object(
                deploy, "MAA_CONFIG_HOME", runtime
            ):
                checked = deploy.maa_config_content_checked_pairs()
                self.assertEqual(deploy.content_mismatches(checked), [])
                # Every config is still deployed and existence-checked.
                self.assertEqual(len(deploy.maa_config_file_pairs()), 16)
                self.assertEqual(len(checked), 15)

                daily = runtime / "tasks" / "daily.toml"
                daily.write_bytes(b"rewritten by render_daily_toml\n")
                self.assertEqual(
                    deploy.content_mismatches(deploy.maa_config_content_checked_pairs()),
                    [],
                )

                overlay = runtime / "resource" / "tasks" / "tasks.json"
                overlay.write_bytes(b"drifted\n")
                self.assertEqual(
                    deploy.content_mismatches(deploy.maa_config_content_checked_pairs()),
                    [str(overlay)],
                )

    def test_deploy_keeps_live_runtime_caches_and_seeds_only_when_absent(self) -> None:
        # activity_stages / the version probes are runtime caches, not config.
        # A stale seed clobbering them re-creates PLAN_NAV_GATE A-3 staleness.
        self.assertEqual(
            deploy.load_json(deploy.ROOT / "state" / "config.json", {})["activity_stages"],
            [],
        )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            hermes = root / "hermes"
            live = hermes / "state" / "maa"
            live.mkdir(parents=True)
            seed = {
                "activity_stages": ["SEED-1"],
                "maa_cli_version": "v0.0.1",
                "maa_core_version": "v0.0.2",
                "retention_days": 14,
                "channel_id": deploy.CHANNEL_PLACEHOLDER,
            }
            repo_state = root / "repo" / "state"
            repo_state.mkdir(parents=True)
            for name in ("defaults.json", "plan.json", "last_run.json"):
                (repo_state / name).write_text("{}", encoding="utf-8")
            (repo_state / "config.json").write_text(
                json.dumps(seed), encoding="utf-8"
            )

            def deploy_once() -> dict:
                with patch.object(deploy, "ROOT", root / "repo"), patch.object(
                    deploy, "HERMES_HOME", hermes
                ):
                    deploy.deploy_state("424242")
                return json.loads((live / "config.json").read_text(encoding="utf-8"))

            # 1. Live host already has its own values -> deploy must not touch them.
            (live / "config.json").write_text(
                json.dumps(
                    {
                        "activity_stages": [],
                        "maa_cli_version": "v0.7.5",
                        "maa_core_version": "v6.17.0-beta.1",
                    }
                ),
                encoding="utf-8",
            )
            result = deploy_once()
            self.assertEqual(result["activity_stages"], [])
            self.assertEqual(result["maa_cli_version"], "v0.7.5")
            self.assertEqual(result["maa_core_version"], "v6.17.0-beta.1")
            # Non-cache keys still deploy normally.
            self.assertEqual(result["retention_days"], 14)
            self.assertEqual(result["channel_id"], "424242")

            # A live non-empty list survives too: "preserve" is not "force empty".
            (live / "config.json").write_text(
                json.dumps({"activity_stages": ["AT-8", "AT-7"]}), encoding="utf-8"
            )
            self.assertEqual(deploy_once()["activity_stages"], ["AT-8", "AT-7"])

            # 2. First boot: no live value -> the seed is the fallback.
            (live / "config.json").unlink()
            self.assertEqual(deploy_once()["activity_stages"], ["SEED-1"])

    def test_every_known_runtime_cache_key_is_preserved_on_deploy(self) -> None:
        """DEV_GUIDE §8 不变量 #25 — the checklist that catches a forgotten key.

        This list is maintained by hand on purpose. When you add a config key
        that a live process writes back, this test fails until you also add it
        to `deploy.PRESERVED_RUNTIME_CACHE_KEYS` — which is exactly the reminder
        that was missing when a deploy clobbered `activity_stages` on
        2026-09-02. If a key here is genuinely repo-owned, delete it from the
        list and say why in the commit.
        """
        known_runtime_caches = {
            # written by maa_plan_card's version probe
            "maa_core_version",
            "maa_cli_version",
            # written by maa_activity_watch / maa_plan_card's activity snapshot
            "activity_stages",
        }
        preserved = set(deploy.PRESERVED_RUNTIME_CACHE_KEYS)
        self.assertEqual(
            known_runtime_caches - preserved,
            set(),
            "运行时缓存键漏进 deploy.PRESERVED_RUNTIME_CACHE_KEYS，部署会用种子盖掉线上真值",
        )
        # The reverse too: preserving a key the repo really owns would silently
        # freeze it on every host that already has a value.
        self.assertEqual(preserved - known_runtime_caches, set())
        # No duplicates — the tuple is the single source of truth.
        self.assertEqual(
            len(deploy.PRESERVED_RUNTIME_CACHE_KEYS), len(preserved)
        )
        # Every preserved key must actually exist in the seed, otherwise it is
        # a typo that would silently never match.
        seed = deploy.load_json(deploy.ROOT / "state" / "config.json", {})
        for key in deploy.PRESERVED_RUNTIME_CACHE_KEYS:
            self.assertIn(key, seed, f"{key} 不在仓库种子里，多半是拼错了")

    def test_parse_maa_versions_from_cli_output(self) -> None:
        cli, core = maa_plan_card.parse_maa_versions(
            "maa-cli v0.7.5\nMaaCore v6.17.0-beta.1\n"
        )
        self.assertEqual(cli, "v0.7.5")
        self.assertEqual(core, "v6.17.0-beta.1")

    def test_update_summary_reports_today_upgrade_and_no_op(self) -> None:
        upgraded = maa_plan_card.format_update_summary(
            {
                "ok": True,
                "changed": True,
                "cli_before": "v0.7.5",
                "cli_after": "v0.7.5",
                "core_before": "v6.17.0-beta.1",
                "core_after": "v6.17.0-beta.4",
            }
        )
        self.assertTrue(upgraded.startswith("今天已更新："))
        self.assertIn("MaaCore v6.17.0-beta.1 → v6.17.0-beta.4", upgraded)
        self.assertIn("maa-cli v0.7.5", upgraded)
        unchanged = maa_plan_card.format_update_summary(
            {
                "ok": True,
                "changed": False,
                "cli_before": "v0.7.5",
                "cli_after": "v0.7.5",
                "core_before": "v6.17.0-beta.1",
                "core_after": "v6.17.0-beta.1",
            }
        )
        self.assertTrue(unchanged.startswith("今天已检查，无新版本"))
        failed = maa_plan_card.format_update_summary(
            {"ok": False, "detail": "failed to connect to github.com"}
        )
        self.assertTrue(failed.startswith("今天检查失败："))

    def test_plan_card_embed_includes_today_update_field(self) -> None:
        card = maa_plan_card.build_embed(
            maa_common.normalize_plan({"skip": False}),
            {},
            "am",
            {
                "ok": True,
                "changed": True,
                "summary": "今天已更新：MaaCore v6.17.0-beta.1 → v6.17.0-beta.4",
            },
        )
        field = next(item for item in card["fields"] if item["name"] == "上游更新")
        self.assertIn("今天已更新", field["value"])
        self.assertEqual(card["title"], "🌱 MAA 今日计划 · 早场 08:00")
        # 晚场卡不跟随上游更新（D1），所以它连这个字段都没有。
        evening = maa_plan_card.build_embed(
            maa_common.normalize_plan({}), {}, "pm"
        )
        self.assertEqual(evening["title"], "🌱 MAA 今日计划 · 晚场 22:00")
        self.assertNotIn("上游更新", [item["name"] for item in evening["fields"]])

    def test_failed_upstream_update_is_recorded_and_alerted(self) -> None:
        at = datetime(2026, 8, 8, 7, 50, tzinfo=BEIJING)
        calls = {"version": 0}

        def fake_run(argv, **_kwargs):
            if argv[:2] == ["/maa", "version"]:
                calls["version"] += 1
                return SimpleNamespace(
                    returncode=0,
                    stdout="maa-cli v0.7.5\nMaaCore v6.17.0-beta.1\n",
                    stderr="",
                )
            if argv[:2] == ["/opt/homebrew/bin/brew", "outdated"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[:2] == ["/maa", "update"]:
                return SimpleNamespace(
                    returncode=1, stdout="", stderr="network down"
                )
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as raw:
            config_path = Path(raw) / "config.json"
            maa_common.atomic_write_json(config_path, {})
            with patch.object(maa_plan_card, "now_beijing", return_value=at), patch.object(
                maa_plan_card, "CONFIG_PATH", config_path
            ), patch.object(maa_plan_card.shutil, "which", return_value="/opt/homebrew/bin/brew"), patch.object(
                maa_plan_card.subprocess,
                "run",
                side_effect=fake_run,
            ), patch.object(maa_plan_card, "merge_last_run") as merge, patch.object(
                maa_plan_card, "discord_request"
            ) as request:
                result = maa_plan_card.check_and_update_upstream(
                    "token",
                    "123",
                    {"maa_bin": "/maa", "resource_update_timeout": 30},
                )
        self.assertFalse(result["ok"])
        self.assertTrue(result["summary"].startswith("今天检查失败："))
        self.assertEqual(merge.call_args.args[0]["resource_update_status"], "failed")
        self.assertEqual(request.call_args.args[1:3], ("POST", "/channels/123/messages"))
        self.assertGreaterEqual(calls["version"], 2)

    def test_daily_upstream_check_keeps_proxy_and_reports_core_upgrade(self) -> None:
        at = datetime(2026, 8, 19, 7, 50, tzinfo=BEIJING)
        versions = iter(
            [
                "maa-cli v0.7.5\nMaaCore v6.17.0-beta.1\n",
                "maa-cli v0.7.5\nMaaCore v6.17.0-beta.4\n",
            ]
        )
        seen_env: list[dict[str, str]] = []

        def fake_run(argv, **kwargs):
            seen_env.append(kwargs.get("env") or {})
            if argv[:2] == ["/maa", "version"]:
                return SimpleNamespace(
                    returncode=0, stdout=next(versions), stderr=""
                )
            if argv[:2] == ["/opt/homebrew/bin/brew", "outdated"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="Updated tap")
            if argv[:2] == ["/maa", "update"]:
                self.assertTrue(argv[-1] == "--batch" or "--batch" in argv)
                return SimpleNamespace(
                    returncode=0, stdout="updated maa-core", stderr=""
                )
            if argv[:2] == ["/maa", "hot-update"]:
                self.assertIn("--batch", argv)
                return SimpleNamespace(
                    returncode=0, stdout="updated hot resources", stderr=""
                )
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as raw:
            config_path = Path(raw) / "config.json"
            maa_common.atomic_write_json(
                config_path, {"maa_bin": "/maa", "maa_disable_proxy": True}
            )
            with patch.dict(
                os.environ,
                {"HTTPS_PROXY": "http://127.0.0.1:7890"},
                clear=False,
            ), patch.object(maa_plan_card, "now_beijing", return_value=at), patch.object(
                maa_plan_card, "CONFIG_PATH", config_path
            ), patch.object(
                maa_plan_card.shutil, "which", return_value="/opt/homebrew/bin/brew"
            ), patch.object(
                maa_plan_card.subprocess, "run", side_effect=fake_run
            ), patch.object(maa_plan_card, "merge_last_run") as merge, patch.object(
                maa_plan_card, "discord_request"
            ) as request:
                result = maa_plan_card.check_and_update_upstream(
                    "token",
                    "123",
                    {
                        "maa_bin": "/maa",
                        "maa_config_dir": "/tmp/maa-config",
                        "maa_disable_proxy": True,
                        "resource_update_timeout": 30,
                    },
                )
            persisted = maa_common.load_json(config_path, {})
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertIn("今天已更新", result["summary"])
        self.assertIn("v6.17.0-beta.1 → v6.17.0-beta.4", result["summary"])
        self.assertEqual(merge.call_args.args[0]["resource_update_status"], "ok")
        request.assert_not_called()
        self.assertEqual(persisted.get("maa_core_version"), "v6.17.0-beta.4")
        update_envs = [
            env for env in seen_env if env.get("MAA_CONFIG_DIR") == "/tmp/maa-config"
        ]
        self.assertTrue(update_envs)
        self.assertGreaterEqual(len(update_envs), 2)
        self.assertTrue(all("HTTPS_PROXY" in env for env in update_envs))

    def test_hot_resource_failure_fails_the_upstream_check(self) -> None:
        def fake_run(argv, **_kwargs):
            if argv[:2] == ["/maa", "version"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout="maa-cli v0.7.5\nMaaCore v6.17.0-beta.6\n",
                    stderr="",
                )
            if argv[:2] == ["/opt/homebrew/bin/brew", "outdated"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[:2] == ["/maa", "update"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[:2] == ["/maa", "hot-update"]:
                return SimpleNamespace(
                    returncode=1, stdout="", stderr="missing resource"
                )
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as raw:
            config_path = Path(raw) / "config.json"
            maa_common.atomic_write_json(config_path, {})
            with patch.object(
                maa_plan_card, "CONFIG_PATH", config_path
            ), patch.object(
                maa_plan_card.shutil, "which", return_value="/opt/homebrew/bin/brew"
            ), patch.object(
                maa_plan_card.subprocess, "run", side_effect=fake_run
            ), patch.object(maa_plan_card, "merge_last_run"), patch.object(
                maa_plan_card, "discord_request"
            ):
                result = maa_plan_card.check_and_update_upstream(
                    "token", "123", {"maa_bin": "/maa"}
                )
        self.assertFalse(result["ok"])
        self.assertIn("missing resource", result["detail"])

    def test_outdated_maa_cli_is_upgraded_via_brew(self) -> None:
        argv_log: list[list[str]] = []

        def fake_run(argv, **_kwargs):
            argv_log.append(list(argv))
            if argv[:2] == ["/maa", "version"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout="maa-cli v0.7.5\nMaaCore v6.17.0-beta.1\n",
                    stderr="",
                )
            if argv[:2] == ["/opt/homebrew/bin/brew", "outdated"]:
                return SimpleNamespace(
                    returncode=0, stdout="maa-cli", stderr=""
                )
            if argv[:2] == ["/opt/homebrew/bin/brew", "upgrade"]:
                return SimpleNamespace(returncode=0, stdout="upgraded maa-cli", stderr="")
            if argv[:2] == ["/maa", "update"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[:2] == ["/maa", "hot-update"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as raw:
            config_path = Path(raw) / "config.json"
            maa_common.atomic_write_json(config_path, {})
            with patch.object(maa_plan_card, "CONFIG_PATH", config_path), patch.object(
                maa_plan_card.shutil, "which", return_value="/opt/homebrew/bin/brew"
            ), patch.object(
                maa_plan_card.subprocess, "run", side_effect=fake_run
            ), patch.object(maa_plan_card, "merge_last_run"), patch.object(
                maa_plan_card, "discord_request"
            ):
                result = maa_plan_card.check_and_update_upstream(
                    "token", "123", {"maa_bin": "/maa"}
                )
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertTrue(
            any(item[:2] == ["/opt/homebrew/bin/brew", "upgrade"] for item in argv_log)
        )


class PlanCardPhaseTests(unittest.TestCase):
    """四期 1b：早晚两张卡，晚场卡不跟随上游更新（D1）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.buttons = root / "buttons.json"
        self.plan_path = root / "plan.json"
        self.defaults = root / "defaults.json"
        maa_common.atomic_write_json(self.defaults, {"stage": "1-7"})
        maa_common.atomic_write_json(self.buttons, {"hermes:btn:agent-book:x": {}})

    def _main(self, argv: list[str]):
        with patch.object(
            maa_plan_card, "load_config", return_value={"channel_id": "123"}
        ), patch.object(
            maa_plan_card, "discord_token", return_value="token"
        ), patch.object(
            maa_plan_card, "check_and_update_upstream", return_value={"ok": True}
        ) as upstream, patch.object(
            maa_plan_card, "refresh_activity_stages", return_value=(True, "")
        ), patch.object(
            maa_plan_card, "load_depot_state", return_value={}
        ), patch.object(
            # 单测一条都不许碰网（DEV_GUIDE §9）：计划卡这条链会去抓一图流。
            maa_farming, "load_market_data", return_value=_offline_market()
        ), patch.object(
            maa_plan_card, "activity_deadline_text", return_value=""
        ), patch.object(
            maa_plan_card, "state_lock", return_value=contextlib.nullcontext()
        ), patch.object(maa_plan_card, "BUTTONS_PATH", self.buttons), patch.object(
            maa_plan_card, "PLAN_PATH", self.plan_path
        ), patch.object(maa_common, "PLAN_PATH", self.plan_path), patch.object(
            maa_common, "DEFAULTS_PATH", self.defaults
        ), patch.object(
            maa_plan_card, "cleanup_old_threads", return_value={"failed": 0}
        ), patch.object(maa_plan_card, "cleanup_run_files"), patch.object(
            maa_plan_card, "retire_old_plan_cards", return_value={"failed": 0}
        ), patch.object(
            # 计划卡末尾的森空岛体检：不桩就拿 skland.json 里的 token 打森空岛、
            # 还把当天记成「已体检」。体检本身在 SklandDailyHealthTests 里测。
            maa_plan_card, "_skland_health_notice"
        ), patch.object(maa_plan_card, "discord_request") as request:
            code = maa_plan_card.main(argv)
        return SimpleNamespace(code=code, upstream=upstream, request=request)

    def test_the_evening_card_never_follows_upstream(self) -> None:
        evening = self._main(["pm"])
        self.assertEqual(evening.upstream.call_count, 0)
        payload = evening.request.call_args.args[3]
        self.assertEqual(payload["embeds"][0]["title"], "🌱 MAA 今日计划 · 晚场 22:00")
        self.assertEqual(len(payload["components"]), 2)
        morning = self._main(["am"])
        self.assertEqual(morning.upstream.call_count, 1)

    def test_the_card_phase_comes_from_the_workdir_name(self) -> None:
        for marker, expected in (("am", "am"), ("pm", "pm"), ("runs", "am")):
            with self.subTest(marker=marker):
                self.assertEqual(
                    maa_plan_card.select_phase(
                        [],
                        at=datetime(2026, 9, 9, 7, 50, tzinfo=BEIJING),
                        cwd=Path("/x") / marker,
                    ),
                    expected,
                )
        self.assertEqual(
            maa_plan_card.select_phase(
                [], at=datetime(2026, 9, 9, 21, 50, tzinfo=BEIJING), cwd=Path("/x/runs")
            ),
            "pm",
        )
        with self.assertRaisesRegex(ValueError, "只接受 am 或 pm"):
            maa_plan_card.select_phase(["evening"])

    def test_each_card_writes_only_its_own_session(self) -> None:
        with patch.object(
            maa_plan_card, "auto_stage", return_value=("SK-5", "碳素快没了")
        ):
            self._main(["am"])
            self._main(["pm"])
        written = json.loads(self.plan_path.read_text(encoding="utf-8"))
        registry = json.loads(self.buttons.read_text(encoding="utf-8"))
        # 自动定关写的是**场次**，不是旧的顶层 stage/skip 结构。
        self.assertEqual(set(written["sessions"]), {"am", "pm"})
        self.assertEqual(written["sessions"]["am"]["stage"], "SK-5")
        self.assertEqual(written["sessions"]["am"]["stage_source"], "auto")
        self.assertNotIn("stage", written)
        # 晚场卡那趟把早场卡的控件整段清掉了（不变量 #7），别人的键留着。
        self.assertIn("hermes:btn:agent-book:x", registry)
        self.assertNotIn("hermes:btn:maa:am:stage-select", registry)
        self.assertIn("hermes:btn:maa:pm:stage-select", registry)

    def test_old_cards_are_retired_by_title_prefix(self) -> None:
        messages = [
            {
                "id": "111",
                "embeds": [{"title": "🌱 MAA 今日计划 · 早场 08:00"}],
                "components": [{"type": 1}],
            },
            {
                "id": "222",
                "embeds": [{"title": "🤖 抄作业：现在能抄的关"}],
                "components": [{"type": 1}],
            },
        ]
        with patch.object(
            maa_plan_card, "discord_request", side_effect=[messages, {}]
        ) as request:
            result = maa_plan_card.retire_old_plan_cards("token", "123")
        self.assertEqual(result, {"retired": 1, "failed": 0})
        self.assertEqual(
            request.call_args_list[1].args[1:3],
            ("PATCH", "/channels/123/messages/111"),
        )


class ActivityDeadlineTests(unittest.TestCase):
    """StageActivityV2 的活动截止时间：读得到就显示，读不到就整行不出现。"""

    def _write(self, payload) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "StageActivityV2.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_expire_time_is_read_in_its_declared_timezone(self) -> None:
        path = self._write(
            {
                "Official": {
                    "sideStoryStage": {
                        "SR": {
                            "Activity": {
                                "Tip": "SideStory「月行水上」",
                                "UtcExpireTime": "2026/09/18 03:59:59",
                                "TimeZone": 8,
                            }
                        },
                        "XX": {"Activity": {"StageName": "别的", "TimeZone": 8}},
                    }
                }
            }
        )
        deadlines = maa_plan_card.activity_deadlines(path)
        self.assertEqual(len(deadlines), 1)
        title, expire = deadlines[0]
        self.assertEqual(title, "SideStory「月行水上」")
        self.assertEqual(expire, datetime(2026, 9, 18, 3, 59, 59, tzinfo=BEIJING))
        text = maa_plan_card.activity_deadline_text(
            {"maa_activity_cache": str(path)},
            datetime(2026, 9, 9, 12, 0, tzinfo=BEIJING),
        )
        self.assertIn("截止 09-18 03:59", text)
        self.assertIn("还剩", text)

    def test_an_unreadable_cache_never_raises_and_shows_no_line(self) -> None:
        broken = self._write({"Official": {"sideStoryStage": "不是对象"}})
        broken.with_name("bad.json").write_text("{ 坏 json", encoding="utf-8")
        for path in (
            broken,
            broken.with_name("bad.json"),
            broken.with_name("missing.json"),
        ):
            with self.subTest(path=path.name):
                self.assertEqual(maa_plan_card.activity_deadlines(path), [])
                embed = maa_plan_card.build_embed(
                    maa_common.normalize_plan({}),
                    {"maa_activity_cache": str(path)},
                    "am",
                )
                self.assertNotIn(
                    "活动截止", [field["name"] for field in embed["fields"]]
                )

    def test_an_already_expired_activity_is_not_shown(self) -> None:
        path = self._write(
            {
                "Official": {
                    "sideStoryStage": {
                        "SR": {
                            "Activity": {
                                "Tip": "旧活动",
                                "UtcExpireTime": "2026/09/01 03:59:59",
                                "TimeZone": 8,
                            }
                        }
                    }
                }
            }
        )
        self.assertEqual(
            maa_plan_card.activity_deadline_text(
                {"maa_activity_cache": str(path)},
                datetime(2026, 9, 9, 12, 0, tzinfo=BEIJING),
            ),
            "",
        )



if __name__ == "__main__":
    unittest.main()
