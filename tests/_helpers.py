"""tests/ 下多个测试模块共用的桩、fixture 和模块级辅助函数（原 `test_maa.py` 顶部那一摊）。

只有被两个以上测试模块用到的东西才搬到这里；只有一个模块用的留在那个模块里。

每个测试模块的 `setUpModule()` 都要：先 `_isolation.guard_devices()` 封住设备层，
再 `start_module_patches()` 封住网络 / 游戏数据出口（见下）；`tearDownModule()` 反过来
`stop_patches()`，最后 `_isolation.assert_shared_home_clean()`。
"""
from __future__ import annotations

import contextlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

# 必须先于任何 scripts/ 模块：把 HOME 换成临时目录，碰真实 home / 外网当场炸。
import _isolation  # noqa: E402
import maa_clearing  # noqa: E402
import maa_common  # noqa: E402
import maa_copilot  # noqa: E402
import maa_farming  # noqa: E402
import maa_gamedata  # noqa: E402
import deploy  # noqa: E402


BEIJING = ZoneInfo("Asia/Shanghai")

# deploy 校验 config.yaml 要 PyYAML。本解释器没有时 `parse_yaml()` 会去找
# `deploy.HERMES_VENV_PYTHON`——那是临时 HOME 下的路径，不存在；`start_module_patches()`
# 把它指到 `_isolation.yaml_python()` 找到的解释器（只做解析，不读状态）。都没有就跳过，
# 不能让「缺 PyYAML 的 RuntimeError」冒充用例期望的 RuntimeError 混过去。
_YAML_PYTHON = _isolation.yaml_python()
needs_yaml = unittest.skipUnless(
    _YAML_PYTHON is not None, "需要 PyYAML：本解释器和 Hermes venv 都没有"
)


def start_module_patches() -> list:
    """启动每个测试模块都要的模块级桩，返回 patcher 列表（交给 `stop_patches()`）。

    - PyYAML：见 `_YAML_PYTHON`。
    - 整套单测**一条都不许碰网**（DEV_GUIDE §9）。`maa_farming` 的抓取层默认把缓存写进
      线上 `~/.hermes/state/maa/farming/`，所以一条漏了打桩的用例既会打一图流，
      又会往运行时状态里塞一份带假时间戳的缓存。这里把网络出口整个封死，
      需要自己造响应的用例（`FarmingFetchTests`）再在 `with` 里覆盖它。
    - 游戏数据同理：不许抓 ArknightsGameData，也不许读写线上 `copilot/gamedata.json`。
      默认给一份月行水上（SR）的小地形，要别的地形的用例自己 patch `load_layout`。
    """
    patches = []
    if _YAML_PYTHON is not None:
        patches.append(patch.object(deploy, "HERMES_VENV_PYTHON", _YAML_PYTHON))
    patches.append(patch.object(
        maa_farming,
        "_http_json",
        side_effect=AssertionError("单测不许访问一图流 / 企鹅物流：传 data= 或自行打桩"),
    ))
    patches += [
        patch.object(maa_gamedata, "_fetch",
                     side_effect=AssertionError("单测不许抓游戏数据：自行打桩")),
        patch.object(maa_gamedata, "load_layout", side_effect=lambda: json.loads(
            json.dumps(SR_LAYOUT))),
        patch.object(maa_gamedata, "refresh_layout", side_effect=lambda *_a, **_k: (
            maa_gamedata.load_layout(), "")),
    ]
    for item in patches:
        item.start()
    return patches


def stop_patches(patches: list) -> None:
    for item in reversed(patches):
        item.stop()


def _offline_market(**overrides) -> dict:
    """一份「外部数据全都没有」的视图：单测**永远不许碰网**（DEV_GUIDE §9）。"""
    data = {
        "matrix": [],
        "stages": [],
        "item_values": {},
        "store": None,
        "degraded_reasons": [],
    }
    data.update(overrides)
    return data


SR_INDEX = {
    "codes": {
        "SR-1": "act54side_01",
        "SR-2": "act54side_02",
        "SR-5": "act54side_05",
        "SR-6": "act54side_06",
        "SR-8": "act54side_08",
        "SR-EX-1": "act54side_ex01",
        "SR-S-1": "act54side_s01",
        "SR-TR-1": "act54side_tr01",
        "1-7": "main_01-07",
        "CE-6": "wk_melee_6",
    },
    "stages": {
        "act54side_01": "SR-1",
        "act54side_02": "SR-2",
        "act54side_05": "SR-5",
        "act54side_06": "SR-6",
        "act54side_08": "SR-8",
        "act54side_ex01": "SR-EX-1",
        "act54side_s01": "SR-S-1",
        "act54side_tr01": "SR-TR-1",
        "main_01-07": "1-7",
        "wk_melee_6": "CE-6",
    },
}


SR_KEY = "SideStory「月行水上」|SR"


def _layout_stage(code, zone, unlock, *, story=False, cost=10):
    return {"code": code, "zone_id": zone, "story": story, "ap_cost": 0 if story else cost,
            "unlock": list(unlock)}


# 测试用的 SR 地形：只含 `SR_INDEX` 里的关，链是 ST-1 → 1 → 2 → 5 → 6 → 8 → ST-2 → EX-1
# → S-1。教学关挂在 SR-1 之后但不挡别的关（真实活动里它挡路，见 CopilotListPlanTests）。
SR_LAYOUT = {
    "stages": {
        "act54side_st01": _layout_stage("SR-ST-1", "act54side_zone1", [], story=True),
        "act54side_01": _layout_stage("SR-1", "act54side_zone1", ["act54side_st01"]),
        "act54side_tr01": _layout_stage("SR-TR-1", "act54side_zone1", ["act54side_01"]),
        "act54side_02": _layout_stage("SR-2", "act54side_zone1", ["act54side_01"]),
        "act54side_05": _layout_stage("SR-5", "act54side_zone1", ["act54side_02"], cost=12),
        "act54side_06": _layout_stage("SR-6", "act54side_zone1", ["act54side_05"], cost=12),
        "act54side_08": _layout_stage("SR-8", "act54side_zone1", ["act54side_06"], cost=21),
        "act54side_st02": _layout_stage("SR-ST-2", "act54side_zone1", ["act54side_08"],
                                        story=True),
        "act54side_ex01": _layout_stage("SR-EX-1", "act54side_zone2", ["act54side_st02"]),
        "act54side_s01": _layout_stage("SR-S-1", "act54side_zone3", ["act54side_ex01"],
                                       cost=20),
    },
    "zones": {
        "act54side_zone1": {"name": "通学路", "index": 0},
        "act54side_zone2": {"name": "殡仪堂", "index": 1},
        "act54side_zone3": {"name": "怀旧区", "index": 2},
    },
}


def _nav_patch(keys):
    """把 `maa_clearing.nav_checker` 换成一张固定的导航键表。"""
    def factory(_dirs):
        def check(entry):
            stage, _mode = maa_clearing.split_entry(entry)
            if stage in keys:
                return True, ""
            return False, f"上游 tasks/Stages/SR.json 里没有 {stage} 这个顶层键"
        return check
    # 活动关的闸门（`copilot_nav_checker`）在测试里用同一张键表：「在表里」= 够得着锚点。
    return _both(
        patch.object(maa_clearing, "nav_checker", side_effect=factory),
        patch.object(maa_copilot, "copilot_nav_checker",
                     side_effect=lambda *_a, **_k: factory(())),
    )


@contextlib.contextmanager
def _both(*patches):
    with contextlib.ExitStack() as stack:
        mocks = [stack.enter_context(item) for item in patches]
        yield mocks[0]


def _zone(cleared: int, total: int, act: str = "act54side") -> dict:
    return {"activity_progress": {act: [
        {"zoneId": f"{act}_zone1", "clearedStage": cleared, "totalStage": total}
    ]}}


RAID_ON: maa_common.Config = {"copilot_raid_switch_task": "ChangeToRaidDifficulty"}


RAID_OFF: maa_common.Config = {"copilot_raid_switch_task": ""}
