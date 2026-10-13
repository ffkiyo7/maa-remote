#!/usr/bin/env python3
"""肉鸽（集成战略）领域层：参数字典、下拉选项、任务文件渲染、输出解析、状态读写。

**为什么走 `maa run` 任务文件而不是 `maa roguelike` 子命令**（spike C，
`docs/spikes/roguelike-2026-09-08.md`）：maa-cli 0.7.5 的 `Theme` 枚举没有
`BlackFlow`、`mode` 硬限 `0..=5`、`stop_when_investment_full` 与
`use_foldartal` 两处默认值和 MaaCore 协议相反、Phantom 主题直接丢弃
`--difficulty`。任务文件的 `[tasks.params]` 是自由透传，一次绕开全部四个坑。

这一层**只有纯函数 + 一份状态文件的读写**，不碰 Discord、不起子进程：
编排在 `maa_roguelike_worker.py`，派发与卡片在 `maa_action.py`。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from maa_common import (
    STATE_DIR,
    atomic_write_json,
    load_json,
    now_beijing,
    state_lock,
)

STATE_PATH = STATE_DIR / "roguelike.json"

# 按钮前缀。它不在 `hermes:btn:maa:` 之下（连字符），所以 07:50 计划卡那趟
# 整段清扫碰不到它；也不在 `hermes:btn:maa-copilot:` 之下，抄作业的
# `prune_expired_searches()` 同样碰不到（不变量 #50）。
BUTTON_PREFIX = "hermes:btn:maa-roguelike:"

# GUI 的「MAX (n)」就是 `int.MaxValue`；`-1` 是协议自带的「不指定」哨兵。
MAX_DIFFICULTY = 2147483647
NO_DIFFICULTY = -1

# 职业组：手册对除傀影外的所有主题都推荐「稳扎稳打」，傀影推荐「取长补短」。
# 它不占 UI（4 个下拉已经用满 5 行里的 4 行），按主题在这里映射。
ROLES_STEADY = "稳扎稳打"
ROLES_COMPLEMENT = "取长补短"

# 每个主题都追加的通用分队（MAA GUI `_commonSquads`）。
COMMON_SQUADS = (
    "指挥分队",
    "后勤分队",
    "突击战术分队",
    "堡垒战术分队",
    "远程战术分队",
    "破坏战术分队",
)
# 高规格分队：除黑流树海外每个主题都追加。
PREMIUM_SQUAD = "高规格分队"

# 主题字典。`max_difficulty` 抄 GUI 的 `GetMaxDifficultyForTheme`，
# `default_difficulty` 抄手册 2026/08/18 的推荐开局。
THEMES: dict[str, dict[str, Any]] = {
    "Phantom": {
        "name": "傀影",
        "full_name": "傀影与猩红血钻",
        "max_difficulty": 15,
        "default_difficulty": 2,
        "roles": ROLES_COMPLEMENT,
        "premium_squad": True,
        "squads": ("集群分队", "矛头分队", "研究分队"),
    },
    "Mizuki": {
        "name": "水月",
        "full_name": "水月与深蓝之树",
        "max_difficulty": 18,
        "default_difficulty": 3,
        "roles": ROLES_STEADY,
        "premium_squad": True,
        "squads": (
            "集群分队", "矛头分队", "心胜于物分队", "物尽其用分队",
            "以人为本分队", "研究分队",
        ),
    },
    "Sami": {
        "name": "萨米",
        "full_name": "探索者的银凇止境",
        "max_difficulty": 15,
        "default_difficulty": 4,
        "roles": ROLES_STEADY,
        "premium_squad": True,
        "squads": (
            "集群分队", "矛头分队", "永恒狩猎分队", "生活至上分队",
            "科学主义分队", "特训分队",
        ),
    },
    "Sarkaz": {
        "name": "萨卡兹",
        "full_name": "萨卡兹的无终奇语",
        "max_difficulty": 18,
        "default_difficulty": 4,
        "roles": ROLES_STEADY,
        "premium_squad": True,
        "squads": (
            "集群分队", "矛头分队", "魂灵护送分队", "博闻广记分队",
            "蓝图测绘分队", "因地制宜分队", "异想天开分队", "点刺成锭分队",
            "拟态学者分队", "专业人士分队",
        ),
        # GUI 的 `Sarkaz_1`：刷源石锭时只列这几支（其余在该模式下没意义）。
        "squads_by_mode": {
            1: (
                "集群分队", "矛头分队", "博闻广记分队", "蓝图测绘分队",
                "点刺成锭分队", "拟态学者分队",
            ),
        },
    },
    "JieGarden": {
        "name": "界园",
        "full_name": "岁的界园志异",
        "max_difficulty": 18,
        "default_difficulty": 3,
        "roles": ROLES_STEADY,
        "premium_squad": True,
        "squads": (
            "特勤分队", "高台突破分队", "地面突破分队", "游客分队",
            "司岁台分队", "天师府分队", "花团锦簇分队", "棋行险着分队",
            "岁影回音分队", "代理人分队", "知学分队", "商贾分队",
        ),
    },
    "BlackFlow": {
        "name": "黑流树海",
        "full_name": "黑流树海",
        "max_difficulty": 15,
        # 手册对黑流树海「暂无难度推荐」，所以默认不指定。
        "default_difficulty": NO_DIFFICULTY,
        "roles": ROLES_STEADY,
        # GUI 的通用分队列表里，只有黑流树海不追加高规格分队。
        "premium_squad": False,
        "squads": (
            "特勤分队", "矛头分队", "高台突破分队", "地面突破分队",
            "本源研修分队", "文明开化分队", "开拓者分队", "多边贸易分队",
            "地质调查分队",
        ),
    },
}

THEME_ORDER = ("Phantom", "Mizuki", "Sami", "Sarkaz", "JieGarden", "BlackFlow")

# 模式。**下拉里的值是不透明 token**，不是数字本身：黑流树海那三项映射的是
# `blackflow_strategy` 而不是 `mode`，用同一个 token 空间才不会两套逻辑。
BASE_MODES = (
    ("1", "刷源石锭（第一层投资完就退）", 1),
    ("0", "刷等级 / 分数", 0),
    ("4", "烧水凹开局", 4),
)
SAMI_MODES = BASE_MODES + (("5", "刷坍缩范式（仅萨米）", 5),)
# MaaCore v6.17.0 起黑流树海只有三个策略，`mode` 只是它的伴随值。
BLACKFLOW_MODES = (
    ("investment", "刷源石锭，投资完成后自动退出", 1, "investment"),
    ("burn", "刷等级，快速飞三层", 0, "burn"),
    ("baby_animal", "刷襁褓动物", 30001, "baby_animal"),
)
BLACKFLOW_CULTIVATION_TARGET = "swaddled_cat"

DEFAULT_THEME = "Sarkaz"
DEFAULT_MODE_TOKEN = "1"
# 萨卡兹刷锭用点刺成锭分队会触发「刷新商店」加速流程（手册）；其余一律指挥分队。
SARKAZ_INGOT_SQUAD = "点刺成锭分队"
DEFAULT_SQUAD = "指挥分队"

PARAM_FIELDS = ("theme", "mode", "difficulty", "squad")


# ---------------------------------------------------------------------------
# 字典查询（纯函数）
# ---------------------------------------------------------------------------

def valid_theme(theme: str) -> bool:
    return theme in THEMES


def theme_name(theme: str) -> str:
    entry = THEMES.get(theme)
    return str(entry["name"]) if entry else theme


def mode_table(theme: str) -> tuple[tuple, ...]:
    """本主题可选的模式表；每项 `(token, 中文, mode)`（黑流树海多一个策略名）。"""
    if theme == "BlackFlow":
        return BLACKFLOW_MODES
    if theme == "Sami":
        return SAMI_MODES
    return BASE_MODES


def valid_mode_token(theme: str, token: str) -> bool:
    return any(row[0] == token for row in mode_table(theme))


def mode_number(theme: str, token: str) -> int:
    for row in mode_table(theme):
        if row[0] == token:
            return int(row[2])
    return 1


def blackflow_strategy(token: str) -> str:
    """黑流树海的三个策略之一；非黑流树海主题永远返回空串。"""
    for row in BLACKFLOW_MODES:
        if row[0] == token:
            return str(row[3])
    return ""


def max_difficulty(theme: str) -> int:
    entry = THEMES.get(theme)
    return int(entry["max_difficulty"]) if entry else 15


def difficulty_values(theme: str) -> list[int]:
    """`不指定` → `MAX` → `n..0` 倒序，与 GUI 的下拉一致。最多 21 项 < 25。"""
    return [NO_DIFFICULTY, MAX_DIFFICULTY] + list(
        range(max_difficulty(theme), -1, -1)
    )


def difficulty_label(value: int) -> str:
    if value == NO_DIFFICULTY:
        return "不指定"
    if value == MAX_DIFFICULTY:
        return "MAX（当前已解锁的最高难度）"
    return f"难度 {value}"


def valid_difficulty(theme: str, value: int) -> bool:
    return value in difficulty_values(theme)


def squad_values(theme: str, mode_token: str = "") -> list[str]:
    """本主题（+模式）合法的分队：主题专属 + 通用 6 个 + 高规格。最多 19 项。"""
    entry = THEMES.get(theme)
    if entry is None:
        return list(COMMON_SQUADS)
    by_mode = entry.get("squads_by_mode") or {}
    own = by_mode.get(mode_number(theme, mode_token)) if mode_token else None
    squads = list(own or entry["squads"])
    squads.extend(COMMON_SQUADS)
    if entry.get("premium_squad"):
        squads.append(PREMIUM_SQUAD)
    return squads


def all_squad_values(theme: str) -> list[str]:
    """本主题在**任何**模式下都算合法的分队全集（`set squad` 的白名单）。"""
    values: list[str] = []
    for token, *_rest in mode_table(theme):
        for squad in squad_values(theme, token):
            if squad not in values:
                values.append(squad)
    return values


def default_squad(theme: str, mode_token: str) -> str:
    if theme == "Sarkaz" and mode_number(theme, mode_token) == 1:
        return SARKAZ_INGOT_SQUAD
    return DEFAULT_SQUAD


def default_mode_token(theme: str) -> str:
    """默认「刷源石锭」：单局最短、收益最直观，最适合手动触发跑几局。"""
    if theme == "BlackFlow":
        return "investment"
    return DEFAULT_MODE_TOKEN


def default_pending(theme: str = DEFAULT_THEME) -> dict[str, Any]:
    theme = theme if valid_theme(theme) else DEFAULT_THEME
    mode_token = default_mode_token(theme)
    return {
        "theme": theme,
        "mode": mode_token,
        "difficulty": int(THEMES[theme]["default_difficulty"]),
        "squad": default_squad(theme, mode_token),
    }


def normalize_pending(pending: Any) -> dict[str, Any]:
    """把磁盘上的 `pending` 收敛成一份合法选择；任何非法值退回该主题的默认。"""
    raw = pending if isinstance(pending, dict) else {}
    theme = str(raw.get("theme") or "")
    if not valid_theme(theme):
        theme = DEFAULT_THEME
    result = default_pending(theme)
    mode_token = str(raw.get("mode") or "")
    if valid_mode_token(theme, mode_token):
        result["mode"] = mode_token
        result["squad"] = default_squad(theme, mode_token)
    try:
        difficulty = int(raw.get("difficulty"))
    except (TypeError, ValueError):
        difficulty = result["difficulty"]
    if valid_difficulty(theme, difficulty):
        result["difficulty"] = difficulty
    squad = str(raw.get("squad") or "")
    if squad in squad_values(theme, result["mode"]):
        result["squad"] = squad
    return result


# ---------------------------------------------------------------------------
# 参数 → 任务文件
# ---------------------------------------------------------------------------

def resolve_params(pending: Any) -> dict[str, Any]:
    """一份下拉选择 → 一份 MaaCore `Roguelike` 参数。

    固定项与理由（spike C 第 7 节）：
    - `starts_count = 1` — 每个子进程只跑一局，停止指令在局间生效（这是「本局
      结束后再停」的全部实现，不该暴露成 UI）。
    - `investment_enabled` / `stop_when_investment_full = true` — 储备满 999 时
      上游自己 `exit_then_stop(abandon=true)`，干净收手，不把进度卡在地图里。
    - **不传 `use_support`** — 助战要先配好开局干员名才有意义。
    - `core_char` 留空 — 上游按练度自动选。
    """
    choice = normalize_pending(pending)
    theme = choice["theme"]
    mode_token = choice["mode"]
    squad = choice["squad"]
    if squad not in squad_values(theme, mode_token):
        squad = default_squad(theme, mode_token)
    params: dict[str, Any] = {
        "theme": theme,
        "mode": mode_number(theme, mode_token),
        "squad": squad,
        "roles": str(THEMES[theme]["roles"]),
        "core_char": "",
        "difficulty": int(choice["difficulty"]),
        "starts_count": 1,
        "investment_enabled": True,
        "stop_when_investment_full": True,
    }
    strategy = blackflow_strategy(mode_token) if theme == "BlackFlow" else ""
    if strategy:
        params["blackflow_strategy"] = strategy
        if strategy == "baby_animal":
            params["blackflow_cultivation_target"] = BLACKFLOW_CULTIVATION_TARGET
    return params


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


# **每局的任务链必须以 `StartUp` 开头**（2026-09-11 线上事故）：`maa roguelike`
# 预设和我们这份任务文件都**不会自己启动游戏**（spike C 第 6.1 节），而项目的开机链
#（`open_emulator / wait_for_android / verify_game_installed / settle_android_runtime`）
# 只负责把**模拟器**拉起来，从来没有一步去起明日方舟。首轮真机现象：模拟器停在
# Android 桌面，MaaCore 直接 `Roguelike Start → Roguelike Error`，37 秒退出码 1，
# 两局连挂后 worker 判失败。日常 `config/maa-cli/tasks/daily.toml` 与抄作业的
# `maa_copilot_worker.NAV_TOML_TEMPLATE` 都是靠这一段把游戏拉起来并登录到主界面的。
#
# **放在 `render_task_toml()` 里**（而不是 worker 只在首局单跑一次 `maa startup`）：
# ① 和抄作业完全一致，任务链只有一处定义、一处单测；② 每局一个子进程，子进程之间
# MaaCore 全部销毁重连，没有「上一局已经登录过」这种跨进程状态可依赖；
# ③ 少一次子进程与一条错误处理分支。
#
# **第 2 局及之后是幂等的**：上游 `StartUp` 在游戏已经在前台时不重复启动，
# 走的是「确认停在主界面」那条链（`config/maa-cli/tasks/operbox.toml` 的注释、
# DEV_GUIDE §11.13 补跑那条「StartUp 顺手把卡在结算页的游戏带回主界面」）；
# 抄作业连抄多关就是靠下一关的 `StartUp` 从上一关的结算页回主界面的
#（2026-09-09 03:06 SR-5 → SR-6 的真机日志）。上一局用尽 `starts_count` 时
# MaaCore 停在**主题入口页**，`EndOfAction → ClickCorner` 那条返回链同样适用。
# 万一它在某个肉鸽内部界面认不出来：`StartUp Error` 之后 MaaCore 仍会继续跑
# `Roguelike`，而 `<Theme>@Roguelike@Begin` 自带 `Continue → ExitThenAbandon`
# 善后链，所以最坏情况是白花几十秒，不会把这一轮带死（`parse_run_output()` 的
# `startup_failed` 会把它记进战报）。**这一条尚未真机验收。**
STARTUP_TOML = (
    "[[tasks]]\n"
    'name = "启动国服"\n'
    'type = "StartUp"\n'
    "[tasks.params]\n"
    'client_type = "Official"\n'
    "start_game_enabled = true\n"
)


def render_task_toml(params: dict[str, Any]) -> str:
    """maa-cli 任务文件：`StartUp`（拉起游戏）→ `Roguelike`。

    `[tasks.params]` 原样透传给 MaaCore。`StartUp` 的理由见上面的注释。
    """
    lines = [
        STARTUP_TOML,
        "[[tasks]]",
        'name = "肉鸽"',
        'type = "Roguelike"',
        "[tasks.params]",
    ]
    for key, value in params.items():
        lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def params_text(params: dict[str, Any]) -> str:
    """一行中文的参数摘要，给卡片和战报用。"""
    theme = str(params.get("theme", ""))
    bits = [
        f"主题 {theme_name(theme)}",
        f"模式 {params.get('mode')}",
        f"难度 {difficulty_label(int(params.get('difficulty', NO_DIFFICULTY)))}",
        f"分队 {params.get('squad')}",
        f"职业组 {params.get('roles')}",
    ]
    strategy = str(params.get("blackflow_strategy") or "")
    if strategy:
        bits.insert(1, f"策略 {strategy}")
    return " · ".join(bits)


# ---------------------------------------------------------------------------
# 输出解析（spike C 第 4.6 节）
# ---------------------------------------------------------------------------

START_RE = re.compile(r"Start exploration (\d+) times")
EXP_RE = re.compile(r"Gain (\d+) exp during this exploration")
DEPOSIT_RE = re.compile(r"Deposit (\d+) / (\d+) / (\d+) originium ingot")
ABANDONED_MARK = "Exploration Abandoned"
INVEST_FULL_MARK = "TraderInvestSystemFull"
CLEAN_MARK = "Roguelike Completed"
STOPPED_MARK = "Roguelike Stopped"
ERROR_MARK = "Roguelike Error"
# 任务链现在以 `StartUp` 开头，所以输出里多了 `StartUp Start/Completed`。它们和上面
# 这些 `Roguelike *` 标记不重名，其余判据一个都不用动；只有「游戏根本没起来」这种
# 情况要认出来——否则它会落进 `unknown`，`ERROR_STREAK_LIMIT` 那道收尾闸门就不生效，
# worker 会一局接一局地空转下去（正是 2026-09-11 那次事故的放大器）。
STARTUP_ERROR_MARK = "StartUp Error"

OUTCOME_LABELS = {
    "settled": "已结算",
    "abandoned": "放弃本局",
    "stopped": "被中断",
    "error": "上游报错",
    "unknown": "结果未知",
}


def parse_run_output(output: str, return_code: int) -> dict[str, Any]:
    """一个子进程（= 一局）的输出 → 结构化结论。

    `clean` 的判据是 **`Roguelike Completed` 且退出码 0**（照抄
    `maa_clearing.copilot_outcome()` 的写法）：只看退出码会把被中断的那一局
    当成干净结束，下次换主题就会撞上上游的跨主题死循环（issue #13245）。
    """
    text = output or ""
    exp_hit = EXP_RE.findall(text)
    deposit_hit = DEPOSIT_RE.findall(text)
    invest = None
    if deposit_hit:
        count, total, deposit = deposit_hit[-1]
        invest = {"count": int(count), "total": int(total), "deposit": int(deposit)}
    result: dict[str, Any] = {
        "started": bool(START_RE.search(text)),
        "exp": int(exp_hit[-1]) if exp_hit else None,
        "abandoned": ABANDONED_MARK in text,
        "invest": invest,
        "invest_full": INVEST_FULL_MARK in text,
        "clean": CLEAN_MARK in text and return_code == 0,
        "interrupted": STOPPED_MARK in text,
        "error": ERROR_MARK in text,
        "startup_failed": STARTUP_ERROR_MARK in text,
        "return_code": return_code,
    }
    if result["error"]:
        outcome = "error"
    elif result["interrupted"]:
        outcome = "stopped"
    elif result["exp"] is not None:
        outcome = "settled"
    elif result["abandoned"]:
        outcome = "abandoned"
    else:
        outcome = "unknown"
    # 只在「什么都没发生」时才让 StartUp 的失败决定结论：结算 / 放弃 / 被中断都是
    # 更具体的事实，不该被开头那一段盖掉。
    if outcome == "unknown" and result["startup_failed"]:
        outcome = "error"
    result["outcome"] = outcome
    detail = [OUTCOME_LABELS[outcome]]
    if result["startup_failed"]:
        detail.append("游戏没起来（StartUp 失败）")
    if result["exp"] is not None:
        detail.append(f"经验 {result['exp']}")
    if invest:
        detail.append(f"投资 {invest['count']}（储备 {invest['deposit']}/999）")
    if result["invest_full"]:
        detail.append("储备已满")
    if not result["clean"]:
        detail.append(f"未干净结束（退出码 {return_code}）")
    result["detail"] = " · ".join(detail)
    return result


SUMMARY_LINE_RE = re.compile(
    r"^\s*(\d+)\.\s*(Passed|Failed|Abandoned|Unknown)"
    r"(?:,\s*invest\s+(\d+)\s+originium\s+ingot\w*)?"
    r"(?:,\s*gained\s+(\d+)\s+exp)?",
)
SUMMARY_TOTALS_RE = re.compile(
    r"Passed\s+(\d+),\s*Failed\s+(\d+)(?:,\s*Abandoned\s+(\d+))?(?:,\s*Unknown\s+(\d+))?"
)
SUMMARY_INVEST_RE = re.compile(r"Total invest\s+(\d+)\s+originium\s+ingot")
SUMMARY_EXP_RE = re.compile(r"Total gained\s+(\d+)\s+exp")

SUMMARY_STATUS_LABELS = {
    "Passed": "通关",
    "Failed": "失败",
    "Abandoned": "放弃",
    "Unknown": "未知",
}


def parse_summary(output: str) -> dict[str, Any]:
    """maa-cli 结尾的 `Explorations: … Summary:` 段 → 结构化数据。

    **不能把这段原文贴进 Discord**（不变量 #9：原始 CLI 文本不进频道），
    所以这里拆成字段，由 `summary_fields()` 渲染成中文。
    """
    text = output or ""
    explorations: list[dict[str, Any]] = []
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.endswith("Explorations:"):
            in_block = True
            continue
        if not in_block:
            continue
        if stripped.endswith("Summary:"):
            break
        match = SUMMARY_LINE_RE.match(stripped)
        if match:
            explorations.append(
                {
                    "n": int(match.group(1)),
                    "status": match.group(2),
                    "invest": int(match.group(3)) if match.group(3) else 0,
                    "exp": int(match.group(4)) if match.group(4) else 0,
                }
            )
    totals = SUMMARY_TOTALS_RE.search(text)
    invest = SUMMARY_INVEST_RE.search(text)
    exp = SUMMARY_EXP_RE.search(text)
    return {
        "explorations": explorations,
        "passed": int(totals.group(1)) if totals else 0,
        "failed": int(totals.group(2)) if totals else 0,
        "abandoned": int(totals.group(3)) if totals and totals.group(3) else 0,
        "unknown": int(totals.group(4)) if totals and totals.group(4) else 0,
        "total_invest": int(invest.group(1)) if invest else 0,
        "total_exp": int(exp.group(1)) if exp else 0,
        "found": bool(explorations or totals),
    }


def summary_fields(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """`parse_summary()` 的结果 → Discord embed 字段（中文，不含 CLI 原文）。"""
    if not summary.get("found"):
        return []
    lines = [
        f"第 {item['n']} 局 · {SUMMARY_STATUS_LABELS.get(item['status'], item['status'])}"
        + (f" · 投资 {item['invest']}" if item["invest"] else "")
        + (f" · 经验 {item['exp']}" if item["exp"] else "")
        for item in summary["explorations"]
    ]
    fields = []
    if lines:
        fields.append(
            {"name": "上游逐局小结", "value": "\n".join(lines)[:1024], "inline": False}
        )
    fields.append(
        {
            "name": "上游合计",
            "value": (
                f"通关 {summary['passed']} · 失败 {summary['failed']} · "
                f"放弃 {summary['abandoned']} · 未知 {summary['unknown']}\n"
                f"共投资 {summary['total_invest']} 源石锭 · 共获得 {summary['total_exp']} 经验"
            )[:1024],
            "inline": False,
        }
    )
    return fields


# ---------------------------------------------------------------------------
# 状态文件（`state/maa/roguelike.json`，0600）
# ---------------------------------------------------------------------------

IDLE = "idle"
RUNNING = "running"
STOPPING = "stopping"
LIVE_STATUSES = frozenset({RUNNING, STOPPING})

EMPTY_STATE: dict[str, Any] = {
    "status": IDLE,
    "theme": "",
    "params": {},
    "pending": {},
    "token": "",
    "message_id": "",
    "started_at": "",
    "runs": [],
    "stop_requested_at": None,
    "abort_requested_at": None,
    "last_theme": "",
    "dirty": False,
    # `save_state()` 每次都写它，`round_is_stale()` 靠它认崩溃残留——
    # 必须留在这张表里，否则 `load_state()` 会把它过滤掉。
    "updated_at": "",
}


def load_state(path: Any = None) -> dict[str, Any]:
    """读状态；文件不存在 / 坏掉一律回一份空状态，**绝不抛**（D-2 的教训）。"""
    raw = load_json(path or STATE_PATH, None)
    state = dict(EMPTY_STATE)
    if isinstance(raw, dict):
        for key in EMPTY_STATE:
            if key in raw:
                state[key] = raw[key]
    if state["status"] not in {IDLE, RUNNING, STOPPING}:
        state["status"] = IDLE
    if not isinstance(state["runs"], list):
        state["runs"] = []
    return state


def save_state(state: dict[str, Any], path: Any = None) -> dict[str, Any]:
    state["updated_at"] = now_beijing().isoformat()
    atomic_write_json(path or STATE_PATH, state)
    return state


def update_state(mutate: Any, path: Any = None) -> dict[str, Any]:
    """在共享写锁下 read-modify-write 一次。

    **两个进程会同时写这份状态**：worker 写 `runs` / `dirty` / `status`，
    按钮写 `pending` / `stop_requested_at` / `abort_requested_at`。整份覆盖
    会让「点停止」和「刚跑完一局」互相吃掉对方的字段。
    """
    with state_lock():
        state = load_state(path)
        mutate(state)
        return save_state(state, path)


def dirty_theme_conflict(state: dict[str, Any], theme: str) -> str:
    """上次没干净结束时换主题的拒绝理由；可以开始返回空串。

    上游 `Roguelike@Begin` 的 `Continue → ExitThenAbandon → AbandonConfirm`
    善后链**只在同主题下**成立，跨主题会死循环（issue #13245）。
    """
    if not state.get("dirty"):
        return ""
    last = str(state.get("last_theme") or "")
    if not last or last == theme:
        return ""
    return (
        f"上次 {theme_name(last)} 主题没有干净结束（残局还在地图里），"
        f"先用 {theme_name(last)} 主题跑一局把它收掉，再换 {theme_name(theme)}。"
    )


# worker 每跑完一局就写一次状态，一局最长 `roguelike_run_timeout`。超过这个时限
# 又没人占 `run.lock`，只能是 worker 进程没了（崩溃 / 被 kill / 机器重启）。
STALE_GRACE_MINUTES = 10


def round_is_stale(
    state: dict[str, Any], locked: bool, at: datetime | None = None,
    *, grace_minutes: int = STALE_GRACE_MINUTES,
) -> bool:
    """状态说在跑，但其实没人在跑——**否则功能会被一次崩溃永久锁死**。

    两条同时成立才算：① `run.lock` 空闲（worker 整轮都持着它，包括局间）；
    ② 状态很久没更新过（`roguelike start` 到 worker 抢锁之间有几秒窗口，
    这条负责把那个窗口排除掉）。
    """
    if state.get("status") not in LIVE_STATUSES or locked:
        return False
    try:
        updated = datetime.fromisoformat(str(state.get("updated_at") or ""))
    except ValueError:
        return True
    moment = at or now_beijing()
    return (moment - updated) > timedelta(minutes=grace_minutes)


def run_lines(runs: Any) -> str:
    """`runs[]` → 逐局中文列表。"""
    icons = {
        "settled": "✅", "abandoned": "⏏️", "stopped": "⏹️",
        "error": "❌", "unknown": "❓",
    }
    lines = []
    for item in runs if isinstance(runs, list) else []:
        if not isinstance(item, dict):
            continue
        icon = icons.get(str(item.get("outcome")), "❓")
        detail = str(item.get("detail") or "")
        lines.append(f"{icon} 第 {item.get('n')} 局 — {detail[:120]}")
    return "\n".join(lines) or "—"


# ---------------------------------------------------------------------------
# 卡片（4 个下拉 + 1 行按钮 = 正好 5 行）
# ---------------------------------------------------------------------------

def _select(
    custom_id: str,
    placeholder: str,
    options: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "type": 3,
        "custom_id": custom_id,
        "placeholder": placeholder[:150],
        "min_values": 1,
        "max_values": 1,
        "options": options[:25],
    }


def _button(label: str, custom_id: str, style: int) -> dict[str, Any]:
    return {
        "type": 2,
        "style": style,
        "label": label[:80],
        "custom_id": custom_id,
        "disabled": False,
    }


def build_card(
    state: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """`(embed, components, registry)`。

    四个下拉的值都是不透明 `choice:N`，真正的 argv 写死在登记表的
    `select_args` 里（不变量 #8）——Discord 侧注入不了任意主题 / 分队字符串。
    """
    pending = normalize_pending(state.get("pending"))
    theme = pending["theme"]
    mode_token = pending["mode"]
    registry: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []

    def add_select(field: str, placeholder: str, choices: list[tuple[str, str, str]],
                   selected: str) -> None:
        custom_id = f"{BUTTON_PREFIX}{field}-select"
        select_args: dict[str, list[str]] = {}
        options: list[dict[str, Any]] = []
        for index, (label, value, description) in enumerate(choices[:25]):
            key = f"choice:{index}"
            options.append(
                {
                    "label": label[:100],
                    "value": key,
                    "description": description[:100],
                    "default": value == selected,
                }
            )
            select_args[key] = ["roguelike", "set", field, value]
        registry[custom_id] = {
            "kind": "exec",
            "script": "maa_action.py",
            "select_args": select_args,
        }
        rows.append({"type": 1, "components": [_select(custom_id, placeholder, options)]})

    add_select(
        "theme",
        "🎲 选主题（换主题会重发一张卡）",
        [
            (f"{THEMES[key]['name']}｜{THEMES[key]['full_name']}", key,
             f"最高难度 {THEMES[key]['max_difficulty']}")
            for key in THEME_ORDER
        ],
        theme,
    )
    add_select(
        "mode",
        "🎯 选模式",
        [(row[1], row[0], "上游模式 " + str(row[2])) for row in mode_table(theme)],
        mode_token,
    )
    add_select(
        "difficulty",
        "🔥 选难度",
        [
            (difficulty_label(value), str(value),
             "上限 " + str(max_difficulty(theme)))
            for value in difficulty_values(theme)
        ],
        str(pending["difficulty"]),
    )
    add_select(
        "squad",
        "🧩 选分队",
        [(squad, squad, "开局分队") for squad in squad_values(theme, mode_token)],
        pending["squad"],
    )

    buttons = []
    for label, suffix, style, args in (
        ("▶️ 开始", "start", 3, ["roguelike", "start"]),
        ("⏹️ 本局结束后停止", "stop", 2, ["roguelike", "stop"]),
        ("⏏️ 立即中止（会丢当前局）", "abort", 4, ["roguelike", "abort"]),
    ):
        custom_id = f"{BUTTON_PREFIX}{suffix}"
        registry[custom_id] = {
            "kind": "exec",
            "script": "maa_action.py",
            "args": args,
        }
        buttons.append(_button(label, custom_id, style))
    rows.append({"type": 1, "components": buttons})

    params = resolve_params(pending)
    status = str(state.get("status") or IDLE)
    status_text = {
        IDLE: "空闲",
        RUNNING: f"正在跑（已完成 {len(state.get('runs') or [])} 局）",
        STOPPING: "已收到停止指令，本局结束后退出",
    }.get(status, status)
    fields = [
        {"name": "这一次会用", "value": params_text(params), "inline": False},
        {"name": "状态", "value": status_text, "inline": True},
        {
            "name": "怎么停",
            "value": "「⏹️ 本局结束后停止」把这一局打完再退，进度不丢；"
                     "「⏏️ 立即中止」当场杀子进程，**当前这一局作废**。",
            "inline": False,
        },
        {
            "name": "⚠️ 换主题前先做一件事",
            "value": "在游戏里把要跑的肉鸽主题**置顶到终端**——上游的自动导航"
                     "「不保证长期可用」，主题不对会一路点空。\n"
                     "另外：上一局没干净结束时**不许换主题**（上游只在同主题下善后残局）。",
            "inline": False,
        },
    ]
    if state.get("dirty"):
        fields.append(
            {
                "name": "🚧 有残局",
                "value": f"上次 {theme_name(str(state.get('last_theme') or ''))} "
                         "没有干净结束，下一局必须还是这个主题（上游会自己放弃残局）。",
                "inline": False,
            }
        )
    embed = {
        "title": "🎲 肉鸽（集成战略）",
        "description": "选好四项再点「开始」。肉鸽**不消耗理智**，不影响日常刷图。",
        "color": 0x9B59B6,
        "fields": fields,
        "timestamp": now_beijing().isoformat(),
    }
    return embed, rows, registry
