#!/usr/bin/env python3
"""作业链的领域层（四期步骤 2）。

角色对齐 ``maa_copilot.py``：这里只有近纯函数——条目形态（普通 / 突袭）、
上游导航键闸门、理智预算与排轮、失败记账、通关判据、去重通知。
真正开模拟器执行的是 ``maa_copilot_worker.py``（**不新建第二个 worker**）。

三期的「通关队列」在四期步骤 2 被拆掉：``clearing.json`` 状态文件、「需要你手动打
一次」出口、EX 开放闸门、编队界面理智 OCR 全部删除（D15）。留下来的这些函数由
``maa_activity_watch.py``（排轮）与 worker（执行、记账）共用。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from maa_common import BEIJING, now_beijing

# 剿灭同样吃理智（地基事实 H）。国服剿灭作战单次 25 理智；写成可配是因为
# 这个数字属于游戏侧常量，改版时不该改代码。
DEFAULT_ANNIHILATION_SANITY = 25


def today_key(at: datetime | None = None) -> str:
    """`Asia/Shanghai` 自然日。搜索失败退避的记账按它计（不是游戏日 04:00）。"""
    moment = at or now_beijing()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BEIJING)
    return moment.astimezone(BEIJING).date().isoformat()


# ---------------------------------------------------------------------------
# 条目形态：关卡名 + 模式
# ---------------------------------------------------------------------------

# 条目的突袭表示：**关卡名 + `#raid` 后缀**。
# 选后缀而不是带 mode 的字典，是因为一轮的条目表保持成一串纯字符串：
# `plan_round()` 的排轮、理智查表、Discord 的 custom_id 都直接拼得进去。
# **除了执行时的 `--raid` 取值，其余一切（理智查表、导航、状态展示）都用
# `split_entry()` 去掉后缀之后的关卡名。**
RAID_SUFFIX = "#raid"


def split_entry(entry: str) -> tuple[str, str]:
    """轮次条目 → `(关卡名, mode)`，mode ∈ {"normal", "raid"}。"""
    text = str(entry or "")
    if text.endswith(RAID_SUFFIX):
        return text[: -len(RAID_SUFFIX)], "raid"
    return text, "normal"


def make_entry(stage: str, mode: str) -> str:
    """`(关卡名, mode)` → 条目。`split_entry()` 的逆。"""
    return f"{stage}{RAID_SUFFIX}" if mode == "raid" else str(stage)


def entry_label(entry: str) -> str:
    """给人看的名字：`SR-EX-1（突袭）`。"""
    stage, mode = split_entry(entry)
    return f"{stage}（突袭）" if mode == "raid" else stage


def stage_nav_prefix(stage: str) -> str:
    """关卡名 → 活动前缀 / Stages 文件名前缀：`SR-5` → `SR`，`SR-EX-1` → `SR`。"""
    text = str(stage or "").strip()
    return text.split("-", 1)[0] if text else ""


# 突袭切换那一步用的上游任务名。`config.copilot_raid_switch_task`
# **空串 = 突袭功能整体关闭**（不只是「跳过切换」）：单文件
# `maa copilot <uri> --raid raid` 根本不会去切难度（`is_raid` 只在 `copilot_list`
# 的条目里有意义），不切就打等于**拿普通难度冒充突袭通关**。所以开关为空时
# 突袭条目从排轮起就不产生，worker 那边再兜一道防线。
DEFAULT_RAID_SWITCH_TASK = "ChangeToRaidDifficulty"


def raid_switch_task(config: dict[str, Any] | None) -> str:
    """配置里的突袭切换任务名；空串 = 功能关闭。

    键**缺失**时取默认值（与 `maa_copilot_worker.switch_to_raid()` 同一套取值口径），
    键存在但为空串 / 空白才算关闭。`config` 不是字典一律当关闭（fail-closed）。
    """
    if not isinstance(config, dict):
        return ""
    return str(config.get("copilot_raid_switch_task", DEFAULT_RAID_SWITCH_TASK) or "").strip()


def raid_enabled(config: dict[str, Any] | None) -> bool:
    """突袭功能的总开关。**不传 config = 关闭**：宁可少排一个模式，也不许错记通关。"""
    return bool(raid_switch_task(config))


def required_modes(
    stage: str, activity_prefix: str, config: dict[str, Any] | None = None
) -> tuple[str, ...]:
    """这一关要打**哪些模式**才算通关（D7）。

    当期活动关（前缀相同：数字关、EX、S）是 `normal + raid`——prts 的
    `content.difficulty` 全为空（地基事实 F），判不出「这份作业支不支持突袭」，
    所以同一份作业先普通后突袭都打一遍。资源本 / 主线没有突袭，只有 `normal`。

    **突袭功能关掉时（`copilot_raid_switch_task` 为空）活动关也只返回
    `("normal",)`**：排轮与手动路径都不再产生 `raid` 条目，这一关只要普通模式
    拿到 `Copilot Completed` 就算通关。
    """
    prefix = str(activity_prefix or "")
    if prefix and stage_nav_prefix(stage) == prefix:
        return ("normal", "raid") if raid_enabled(config) else ("normal",)
    return ("normal",)


# ---------------------------------------------------------------------------
# 上游导航键闸门（2026-09-05 范围变更：**不再自写导航定义**）
# ---------------------------------------------------------------------------

# 自动作业只跑上游 `tasks/Stages/<前缀>.json` 里**已有导航键**的关（D8）。
# 原计划的「导航定义生成器」整段取消，于是「上游没写导航定义」这件事**永远不会
# 自动变好**：这样的关既不进目标集合、也不进下拉，只在一览里标「⛔ 上游无导航」。
#
# 只读**上游** resource 目录（`maa_copilot._resource_dirs()` 给的那几个）。
# 故意不读也不建 `~/.config/maa-cli/resource/tasks/Stages/`：本项目不再往那里
# 写自造的导航定义，读它只会让「上游有没有这关」这个判断失真。
NAV_STAGES_SUBDIR = ("tasks", "Stages")


def read_nav_keys(
    resource_dirs: Iterable[Path], prefix: str
) -> tuple[set[str], str]:
    """读上游 `tasks/Stages/<prefix>.json` 的**顶层键**，返回 `(键集, 异常摘要)`。

    文件不存在 / 读失败 / JSON 坏 / 顶层不是对象 → 一律**视为无键**（fail-closed）
    并把异常摘要带出去，写进卡片，绝不当成「有导航定义」。
    """
    prefix = str(prefix or "")
    if not prefix:
        return set(), "关卡名没有前缀，定位不到 Stages 文件"
    keys: set[str] = set()
    errors: list[str] = []
    parsed = False
    for resource_dir in resource_dirs:
        path = (Path(resource_dir) / "tasks" / "tasks.json" if prefix == "tasks"
                else Path(resource_dir).joinpath(*NAV_STAGES_SUBDIR, f"{prefix}.json"))
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            errors.append(f"{path} 不存在")
            continue
        except (OSError, ValueError) as exc:
            errors.append(f"{path} 读不出来：{type(exc).__name__}: {exc}")
            continue
        if not isinstance(data, dict):
            errors.append(f"{path} 顶层不是对象")
            continue
        parsed = True
        keys.update(str(key) for key in data)
    if parsed:
        return keys, ""
    return set(), "；".join(errors)[:300] or f"没找到 tasks/Stages/{prefix}.json"


def nav_checker(
    resource_dirs: Iterable[Path],
) -> Callable[[str], tuple[bool, str]]:
    """按前缀缓存的导航键判定器：`(entry) -> (上游有导航定义吗, 没有的话为什么)`。

    每个前缀只 open 一次文件（一轮里几十个条目共用同一份缓存），调用方每轮建一次。
    **突袭条目按普通关名查**（`split_entry()`），与理智查表一致。
    """
    dirs = [Path(path) for path in resource_dirs]
    cache: dict[str, tuple[set[str], str]] = {}

    def check(entry: str) -> tuple[bool, str]:
        stage, _mode = split_entry(entry)
        prefix = stage_nav_prefix(stage)
        # Supplies and chapter navigation are provided upstream outside activity files.
        if prefix in {"CE", "LS", "AP", "CA", "SK", "PR"}:
            prefix = "Supplies"
        main = re.fullmatch(r"[A-Za-z]{0,3}(\d{1,2})-\d{1,2}", stage)
        if main:
            prefix = "tasks"
        if prefix not in cache:
            cache[prefix] = read_nav_keys(dirs, prefix)
        keys, error = cache[prefix]
        if stage in keys or (main and f"Episode{main[1]}" in keys):
            return True, ""
        if error:
            return False, f"上游没有 {stage} 的导航定义：{error}"
        return False, f"上游 tasks/Stages/{prefix}.json 里没有 {stage} 这个顶层键"

    return check


# ---------------------------------------------------------------------------
# 活动关的多作业导航（2026-10-19，用户放宽 D8：只在隔离临时配置里补三小步）
# ---------------------------------------------------------------------------

# 活动关不再要求「每关都有上游键」：只要这期活动在上游有**任意一个**普通关键
# （锚点），就借它的进活动路径走到普通区域地图上停下（方案 B），之后：
#   - 找关、编队、打仗全部交给 MaaCore 的 `copilot_list`；
#   - 剧情关挡路 → 在地图上点开它、跳过剧情（`story` 段）；
#   - EX / S 关在另一个区域 → 按游戏数据里的区域名点入口（`zone` 段）。
# 每一步之后都要在屏幕上认出目标区域的关卡代号才算到位，认不出就停下通知。
# 这些都只写进 `maa_copilot_worker._isolated_config()` 的临时目录，不碰上游资源。

def read_nav_tasks(resource_dirs: Iterable[Path], prefix: str) -> dict[str, Any]:
    """上游 `tasks/Stages/<prefix>.json` 的完整内容（热更新覆盖本体）。读不到返回 `{}`。"""
    merged: dict[str, Any] = {}
    for resource_dir in reversed([Path(path) for path in resource_dirs]):
        path = Path(resource_dir).joinpath(*NAV_STAGES_SUBDIR, f"{prefix}.json")
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            merged.update(data)
    return merged


def pick_anchor(
    resource_dirs: Iterable[Path], prefix: str
) -> tuple[str, list[str], str]:
    """这期活动的锚点：`(锚点关名, 要改成只识别的 next 任务名, 失败原因)`。

    只认 `<前缀>-<数字>` 形状的顶层键（普通区域的战斗关），取序号最小的那个——
    方案 B 只借它「进活动、点进普通区域」那一段，锚点本身解没解锁都无所谓。
    `next` 为空说明上游链的形状变了，fail-closed。
    """
    tasks = read_nav_tasks(resource_dirs, prefix)
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d{{1,2}})$")
    keyed = sorted(
        (int(match.group(1)), key)
        for key in tasks
        if (match := pattern.match(str(key)))
    )
    for _seq, key in keyed:
        body = tasks.get(key)
        nexts = [str(name) for name in (body.get("next") or [])] if isinstance(body, dict) else []
        if nexts:
            return key, nexts, ""
    if keyed:
        return "", [], f"上游 {prefix} 的导航键没有 next，链的形状可能变了"
    return "", [], f"上游 tasks/Stages/{prefix}.json 里没有 {prefix} 普通关的导航键"


def zone_codes(layout: dict[str, Any], zone_id: str) -> list[str]:
    """某个区域在地图上会显示的全部关卡代号（战斗、剧情、教学关都算）。"""
    codes = {
        str(info.get("code") or "")
        for info in (layout.get("stages") or {}).values()
        if isinstance(info, dict) and info.get("zone_id") == zone_id and info.get("code")
    }
    return sorted(codes)


def cleared_closure(layout: dict[str, Any], cleared: Iterable[str]) -> set[str]:
    """已通关的关 + 它们的全部前置：解锁是一条链，打过 YW-6 就说明 YW-ST-2 看过了。"""
    import maa_gamedata

    done: set[str] = set()
    for stage_id in cleared:
        done.add(str(stage_id))
        done.update(maa_gamedata.ancestors(layout, str(stage_id)))
    return done


def plan_segments(
    entries: list[dict[str, Any]],
    layout: dict[str, Any],
    cleared: Iterable[str],
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
    """一轮活动条目 → 按顺序执行的段：`(段, [(没排进去的条目, 原因)])`。

    段有两种，都带 `zone_id`（执行前 worker 负责先站到这个区域的地图上）：
    `{"kind": "story", "stage_id", "code", "zone_id"}` 点开剧情关并跳过；
    `{"kind": "copilot", "zone_id", "items": [条目...]}` 一张多作业列表。

    每个条目先看它的前置关（`maa_gamedata.ancestors()`）里还有哪些没过：
    剧情关就插一段 `story`（已经看过的剧情再点一次也无害，不耗理智）；
    教学关、没排进本轮的战斗关挡在前面就**从这里截断**，后面的条目全部退回
    （解锁是一条链，后面的关首通时一定还锁着）。
    """
    import maa_gamedata

    done = cleared_closure(layout, cleared)
    segments: list[dict[str, Any]] = []
    dropped: list[tuple[dict[str, Any], str]] = []
    zone = ""

    def enter(target_zone: str) -> None:
        nonlocal zone
        if target_zone:
            zone = target_zone

    for position, entry in enumerate(entries):
        stage_id = str(entry.get("stage_id") or "")
        stage = str(entry.get("stage") or "")
        info = maa_gamedata.stage_info(layout, stage_id)
        blocker = ""
        if info is None:
            blocker = f"游戏数据里没有 {stage}"
        elif str(entry.get("mode") or "normal") == "raid":
            if stage_id not in done:
                blocker = f"{stage} 的普通难度还没过，突袭没解锁"
        else:
            for parent in maa_gamedata.ancestors(layout, stage_id):
                if parent in done:
                    continue
                parent_info = maa_gamedata.stage_info(layout, parent) or {}
                code = str(parent_info.get("code") or parent)
                kind = maa_gamedata.stage_kind(parent_info)
                if kind == "story":
                    enter(str(parent_info.get("zone_id") or ""))
                    segments.append(
                        {"kind": "story", "stage_id": parent, "code": code, "zone_id": zone}
                    )
                    done.add(parent)
                    continue
                blocker = (
                    f"教学关 {code} 还没打（固定阵容，需要你手动打一次）"
                    if kind == "tutorial"
                    else f"前置关 {code} 还没通关，本轮也没排它"
                )
                break
        if blocker:
            dropped.extend((rest, blocker) for rest in entries[position:])
            break
        enter(str(info.get("zone_id") or ""))
        last = segments[-1] if segments else None
        if last and last["kind"] == "copilot" and last["zone_id"] == zone:
            last["items"].append(entry)
        else:
            segments.append({"kind": "copilot", "zone_id": zone, "items": [entry]})
        done.add(stage_id)
    return segments, dropped


_ZONE_NOISE_RE = re.compile(r"[\s“”\"'「」『』《》()（）\-·・—_]")
ZONE_MATCH_MIN = 0.6


def _zone_text(text: str) -> str:
    return _ZONE_NOISE_RE.sub("", str(text or ""))


def zone_button(
    ocr_lines: Iterable[str], target: str, others: Iterable[str]
) -> tuple[int, int] | None:
    """在一屏 OCR（`文字\\t[x,y,w,h]`，`ocrbox` 的输出）里找区域入口，返回点击坐标。

    入口按钮上的字就是游戏数据里的区域名（普通区域 2026-10-19 真机核对过），但 OCR
    会认错字（「我们的伊比和亚」）。所以按相似度挑：和目标区域名的相似度要过
    `ZONE_MATCH_MIN`，还必须**严格高于**它和同活动其它区域名的相似度——YW 两个区域名
    都含「伊比利亚」，只认片段会点错。认不准返回 None，调用方停下通知。
    """
    from difflib import SequenceMatcher

    wanted = _zone_text(target)
    rivals = [_zone_text(name) for name in others if _zone_text(name)]
    if not wanted:
        return None
    best: tuple[float, int, int] | None = None
    for line in ocr_lines:
        match = re.match(r"^(.*)\t\[(\d+),(\d+),(\d+),(\d+)\]\s*$", str(line))
        if not match:
            continue
        text = _zone_text(match.group(1))
        if not text:
            continue
        score = SequenceMatcher(None, text, wanted).ratio()
        if score < ZONE_MATCH_MIN:
            continue
        if any(SequenceMatcher(None, text, rival).ratio() >= score for rival in rivals):
            continue
        x, y, w, h = (int(part) for part in match.groups()[1:])
        if best is None or score > best[0]:
            best = (score, x + w // 2, y + h // 2)
    return (best[1], best[2]) if best else None


def job_file_problem(content: Any, stage_id: str) -> str:
    """一份作业文件能不能进多作业列表；能就返回空串。

    MaaCore 在添加任务时就解析列表里的**全部**文件，有一份坏的整条任务直接报错
    （2026-10-19 真机：一份旧 SR-8 作业让整张列表没跑起来），所以组表前逐份查。
    打哪关只看文件里的 `stage_name`（列表项上的关名不起作用，同一天实测），
    所以它必须正好是目标关。
    """
    if not isinstance(content, dict):
        return "作业内容不是 JSON 对象"
    if str(content.get("stage_name") or "") != stage_id:
        return f"作业的关卡是 {content.get('stage_name') or '空'}，不是 {stage_id}"
    actions = content.get("actions")
    if not isinstance(actions, list) or not actions:
        return "作业没有操作步骤（拿到的可能是精简版）"
    if not any(isinstance(content.get(key), list) and content.get(key)
               for key in ("opers", "groups")):
        return "作业没有干员"
    return ""


# maa-cli 在 `-vvv` 下把 MaaCore 的 `SubTaskExtraInfo` 原样打成一个 JSON 块
# （`UnknownSubTaskExtraInfo: {` 开头、顶格 `}` 结尾）。多作业每开始一份作业发一次
# `CopilotListLoadTaskFileSuccess`，带这份作业的 `file_name`（2026-10-19 真机 3/3）。
LIST_ITEM_EVENT = "CopilotListLoadTaskFileSuccess"
_EXTRA_INFO_HEAD = re.compile(r"UnknownSubTaskExtraInfo:\s*\{\s*$")


def list_started_files(output: str) -> list[str]:
    """从 maa-cli 输出里按顺序取出「已开始」的作业文件路径。"""
    started: list[str] = []
    block: list[str] | None = None
    for line in str(output or "").splitlines():
        if block is None:
            if _EXTRA_INFO_HEAD.search(line):
                block = ["{"]
            continue
        block.append(line)
        if line.rstrip() != "}":
            continue
        try:
            data = json.loads("\n".join(block))
        except ValueError:
            data = None
        block = None
        if not isinstance(data, dict) or data.get("what") != LIST_ITEM_EVENT:
            continue
        name = str((data.get("details") or {}).get("file_name") or "")
        if name:
            started.append(name)
    return started


def list_outcomes(
    files: list[str], output: str, return_code: int
) -> list[tuple[str, str]]:
    """一张多作业列表跑完后，每一项的 `(结论, 明细)`，与 `files` 一一对应。

    MaaCore 每一份都要过三星检查才开始下一份，所以「第 k 份开始了」就证明
    前 k−1 份都三星通关。整条 `Copilot Completed` 且退出码 0 → 全部通过；
    否则最后开始的那一份记失败，没开始的记 `skipped`。
    开始事件和文件对不上（顺序乱了、出现列表外的文件）一律 `unknown`：宁可重打。
    """
    started = list_started_files(output)
    if started != files[: len(started)]:
        return [("unknown", "逐关事件与列表对不上，本轮不记通关")] * len(files)
    whole, detail = copilot_outcome(output, return_code)
    if whole == "won" and len(started) == len(files):
        return [("won", "")] * len(files)
    if whole == "won":
        # 整条报完成却有项没开始：不该发生，按看不懂处理。
        whole, detail = "unknown", "列表报完成，但有作业没开始"
    results: list[tuple[str, str]] = []
    for index in range(len(files)):
        if index < len(started) - 1:
            results.append(("won", ""))
        elif index == len(started) - 1:
            results.append((whole if whole != "won" else "unknown", detail))
        else:
            results.append(("skipped", "前一份没过，这份没开始"))
    return results


# ---------------------------------------------------------------------------
# 理智预算
# ---------------------------------------------------------------------------

def annihilation_reserve(plan: dict[str, Any], config: dict[str, Any]) -> int:
    """今天还要打的剿灭要预留多少理智（地基事实 H）。

    剿灭是 `daily.toml` 里的第二个 `Fight(stage=Annihilation)`，**它同样消耗理智**。
    排关前必须先扣掉这份，否则会把可用理智算多、排出打不完的一轮。
    `plan["annihilation"]` 由 `maa_daily.attach_annihilation()` 按游戏日记帐；
    今天已经刷过就是 0。
    """
    if not isinstance(plan, dict) or not plan.get("annihilation"):
        return 0
    if not bool((plan.get("tasks") or {}).get("fight", True)):
        return 0
    try:
        cost = int(config.get("annihilation_sanity", DEFAULT_ANNIHILATION_SANITY))
    except (TypeError, ValueError):
        cost = DEFAULT_ANNIHILATION_SANITY
    return max(0, cost)


def sanity_budget(
    available: int | None,
    plan: dict[str, Any],
    config: dict[str, Any],
) -> int:
    """这一轮能花的理智 = 当前理智 − 剿灭那份。理智未知一律 0（fail-closed）。"""
    if available is None:
        return 0
    try:
        current = int(available)
    except (TypeError, ValueError):
        return 0
    return max(0, current - annihilation_reserve(plan, config))


def plan_round(
    entries: Iterable[str],
    budget: int,
    cost_of: Callable[[str], int | None],
) -> dict[str, Any]:
    """从有序条目表里排出这一轮打得起的条目。

    返回 `{"entries": [...], "spent": int, "unknown_cost": [...], "stop_reason": str}`。

    理智成本未知的条目（`stage_sanity_cost()` 对 EX 返回 `None`）：
    **不排入当轮、不计入理智预算**——那不是「这关打完了」，是「现在算不出来」。
    不许拿 0 顶替、不许拿同活动普通关的值猜：理智算错会直接导致空跑。
    """
    remaining = max(0, int(budget))
    picked: list[str] = []
    unknown: list[str] = []
    stop_reason = "queue_end"
    for raw in entries:
        entry = str(raw)
        cost = cost_of(entry)
        try:
            cost = int(cost) if cost is not None else None
        except (TypeError, ValueError):
            cost = None
        if cost is None:
            unknown.append(entry)
            stop_reason = "unknown_cost"
            break
        if cost <= 0 or cost > remaining:
            stop_reason = "sanity"
            break
        picked.append(entry)
        remaining -= cost
    return {
        "entries": picked,
        "spent": max(0, int(budget)) - remaining,
        "unknown_cost": unknown,
        "stop_reason": stop_reason,
    }


# ---------------------------------------------------------------------------
# 失败记账（搜索侧：练度不满足 / 暂无作业的退避）
# ---------------------------------------------------------------------------

def _stage_entry(state: dict[str, Any], stage: str) -> dict[str, Any]:
    stage_state = state.setdefault("stage_state", {})
    if not isinstance(stage_state, dict):
        stage_state = {}
        state["stage_state"] = stage_state
    entry = stage_state.get(stage)
    if not isinstance(entry, dict):
        entry = {"failed_dates": [], "last_error": ""}
        stage_state[stage] = entry
    if not isinstance(entry.get("failed_dates"), list):
        entry["failed_dates"] = []
    return entry


def record_stage_failure(
    state: dict[str, Any],
    stage: str,
    *,
    kind: str,
    detail: str = "",
    at: datetime | None = None,
    reason_code: str = "",
    gaps: Iterable[str] | None = None,
) -> dict[str, Any]:
    """记一次失败到 `state["stage_state"][stage]`，返回同一个 `state`。

    **按「不同日期」记，不按尝试次数记**：同一天失败 5 次只算 1 天。`last_failed_at`
    是退避的唯一依据（`activity_search_retry_minutes`），`gaps` 让卡片能写出
    「🚫 练度不满足（缺：…）」而不是一句笼统的失败。
    """
    entry = _stage_entry(state, stage)
    day = today_key(at)
    if day not in entry["failed_dates"]:
        entry["failed_dates"].append(day)
    entry["kind"] = str(kind)[:40]
    entry["last_error"] = str(detail)[:400]
    entry["last_failed_at"] = (at or now_beijing()).isoformat()
    if reason_code:
        entry["reason_code"] = str(reason_code)[:60]
    entry["gaps"] = [str(gap)[:80] for gap in (gaps or [])][:6]
    return state


def clear_stage_failure(state: dict[str, Any], stage: str) -> dict[str, Any]:
    """这一关重新搜到作业了，把退避记账抹掉。"""
    stage_state = state.get("stage_state")
    if isinstance(stage_state, dict):
        stage_state.pop(stage, None)
    return state


def minutes_since_failure(
    state: dict[str, Any], stage: str, at: datetime | None = None
) -> float:
    """距上次搜索失败多少分钟；没失败过返回 `inf`（= 可以立刻搜）。"""
    stage_state = state.get("stage_state")
    entry = stage_state.get(stage) if isinstance(stage_state, dict) else None
    if not isinstance(entry, dict):
        return float("inf")
    try:
        last = datetime.fromisoformat(str(entry.get("last_failed_at") or ""))
    except (TypeError, ValueError):
        return float("inf")
    return max(0.0, ((at or now_beijing()) - last).total_seconds() / 60)


# ---------------------------------------------------------------------------
# 去重通知
# ---------------------------------------------------------------------------

def should_notify(state: dict[str, Any], key: str, reason: str) -> bool:
    """同一原因不刷屏；原因变了要重新发。"""
    notified = state.setdefault("notified", {})
    if not isinstance(notified, dict):
        notified = {}
        state["notified"] = notified
    if notified.get(key) == reason:
        return False
    notified[key] = reason
    return True


def clear_notification(state: dict[str, Any], key: str) -> None:
    notified = state.get("notified")
    if isinstance(notified, dict):
        notified.pop(key, None)


# ---------------------------------------------------------------------------
# 「这一关到底打过了没有」的判据
# ---------------------------------------------------------------------------

# maa-cli 把 MaaCore 的 TaskChain 回调打成 `<链名> Completed / Stopped / Error`
# ——`maa_daily._task_state()` 与 worker 导航段的 `"Fight Completed" not in …`
# 都建立在同一条约定上，后者是 C3 实测跑通过的。
COPILOT_VICTORY_MARKERS = ("Copilot Completed",)
COPILOT_FAILURE_MARKERS = (
    "Copilot Error",
    "Copilot Stopped",
    "Error: Some error",
)


def copilot_outcome(output: str, return_code: int) -> tuple[str, str]:
    """`maa copilot` 这一趟到底算不算打过了：`("won"|"failed"|"unknown", 明细)`。

    **为什么不能只看退出码**：记账一旦写成「通关」这一关就永远不会再排，
    撤退、三星未达、进程正常退出的 Fight Error 全都会被算成通关，而且没有人知道。

    胜利标记 `Copilot Completed` **已于 2026-09-08 用真实日志核对**
    （`runs/2026-09-08-copilot-9e14e500ac25.log` 第 47 行）。认不出胜利标记时
    仍然 fail-closed 返回 `unknown`，调用方按失败处理、不记通关。
    """
    text = str(output or "")
    hit = [marker for marker in COPILOT_FAILURE_MARKERS if marker in text]
    if hit:
        return "failed", f"输出含失败标记：{'、'.join(hit)}"
    if return_code != 0:
        return "failed", f"maa copilot 退出码 {return_code}"
    if any(marker in text for marker in COPILOT_VICTORY_MARKERS):
        return "won", ""
    return (
        "unknown",
        "退出码 0 但输出里没有 `Copilot Completed`，无法确认这一关真的打过了；"
        "本次不记通关（宁可重打，也不静默跳关）。"
        "请核对 runs/ 下的日志，若胜利标记不是这个字符串就改 "
        "maa_clearing.COPILOT_VICTORY_MARKERS。",
    )
