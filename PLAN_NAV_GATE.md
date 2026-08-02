# 抄作业导航闸门 + 自动任务模拟器探测

> 二期已结案（见 `REVIEW_PHASE2.md`，五轮 29 条全关）。本文档是**上线后实测发现的
> 两个新问题**的修复方案，不是评审遗留。两项互相独立，可以分开做、分开提交。

**触发事件**：2026-08-02 23:57 / 23:59，用户在 Discord 点了 TO-1 的抄作业候选，
两次都在 24 秒后失败，并且**把用户正在玩的模拟器杀掉了**。

---

# 背景：三个实测结论

下面每一条都是本机实跑测出来的，不是从文档推的。**动手前先读完，其中两条推翻了
原先代码注释和我第一版判断里的假设。**

## 结论 1：`maa copilot` 不会自己导航（原注释是对的）

`maa_copilot_worker.py:56` 那句 `single-op maa copilot must start from the stage's
formation screen` 成立。实测：游戏停在首页，跳过导航步直接跑
`maa copilot maa://98585 --formation --batch -v`，MaaCore 的 Copilot 链是：

```
subtask 1  ProcessTask  first=[BattleStartPre]    ← 就地找编队界面的「开始行动」
subtask 4  BattleFormationTask
subtask 5  ProcessTask  first=[BattleStartAll]    ← cur_retry 0→3 全部失败
           SubTaskError → TaskChainError           (25s, rc=1)
```

全链 8 个子任务里**没有任何导航成分**。

> ⚠️ `resource/tasks/tasks.json` 里确实有 `Copilot@FullStageNavigation`、
> `Copilot@ChapterSwipeToTheRightAndPlot`、`Copilot@StageNavigationSlowlySwipeLeft`
> 这些 override，看起来像「Copilot 自带导航」。**它们不在这条链里**（应该是 GUI 的
> 自动导航功能才会挂上）。别再被这几个键误导——我就是这么推错的。

## 结论 2：`Fight` 只接受 `maa activity` 列出的活动关，换成内部 id 也没用

这是本次修复的**核心依据**。逐关实测（`Failed to add task` = 被拒）：

```
TO-1 ✗   TO-2 ✗   TO-3 ✗   TO-4 ✗   TO-5 ✓
TO-6 ✗   TO-7 ✓   TO-8 ✓   TO-9 ✓

Fight 接受   : ['TO-5', 'TO-7', 'TO-8', 'TO-9']
maa activity : ['TO-9', 'TO-8', 'TO-7', 'TO-5']      ← 逐字吻合
```

主线 / 资源本一律接受：`1-7`、`CE-6`、`LS-6` 实测均通过。
内部 id 不是绕过手段：`stage = "act53side_01"` **同样被拒**。

几个**反直觉但已证伪**的猜测，别再走这些弯路：

| 猜测 | 实测 |
|---|---|
| 判据是 `resource/tasks/Stages/TO.json` 的键 | ✗ 该文件只有 TO-7/8/9，但 TO-5 被接受 |
| 判据是 `MaaResource/resource/stages.json` | ✗ 该文件 TO-1~TO-9 全都有，但只有 4 关被接受 |
| 传内部 stageId 可以绕过 | ✗ `act53side_01` 一样被拒 |
| 热更新里有另一份 TO.json | ✗ `MaaResource/resource/tasks/Stages/TO.json` 不存在 |

**唯一与实测吻合的数据源就是 `maa activity Official` 的输出**，也就是
`maa_plan_card.parse_activity_stages()` 解析出来、存在 `config["activity_stages"]`
里的那份清单。

> 附带发现：`config["activity_stages"]` 目前是 `["TO-9","TO-8","TO-7"]`（8/1 22:02 存的），
> **已经过期**，少了 TO-5。`parse_activity_stages()` 本身没问题——拿当前真实输出测过，
> 能正确解析出 4 个。只是没人重新刷过。这直接影响闸门，见 A-3。

## 结论 3：失败清理会杀掉用户正在玩的模拟器

失败后走 except → `close_everything(config)` → `maa closedown` +
`osascript ... to quit` 关掉 BlueStacks。`open_emulator()` 只是无脑
`open -a BlueStacks`，**没有记录进来时模拟器是不是已经在跑**，所以任何失败路径都会
把用户踢出游戏。

时间线佐证：本次 23:59:10 起、23:59:34 止，共 **24 秒**；而正常冷启动光「启动国服」
就要 50 秒。24 秒说明 BlueStacks 本来就开着——用户正在玩。

**用户决定不修「不是自己开的就别关」，改为做 B（自动任务前探测）。** 理由是探测能
顺带解决更重要的问题：早八/晚十的自动任务不该在用户正在玩的时候抢设备。

---

# 决策记录（用户已拍板，不要自作主张改回去）

1. **只接受能自动导航的关卡。** 导航不了的，**即使 prts 有作业也不准执行**——
   在搜索阶段就拒掉，不要放到候选卡片再说。
2. **不做「半自动」模式**（让用户手动进编队界面再点「就地开打」）。没有意义，不做。
3. **不做「不是自己开的模拟器就别关」。** 改为：**早八点 / 晚十点的自动任务执行前先
   探测模拟器是否在线，在线就说明用户在玩，直接跳过本场。**

---

# A. 抄作业导航闸门

## A-1 判据

新增 `maa_copilot.stage_is_navigable(stage_id, stage_label, config)`，
返回 `(bool, reason)`。规则是**白名单**（fail-closed，对应决策 1）：

| `stage_id` 形态 | 判定 | 实测依据 |
|---|---|---|
| `main_*` | 放行 | `1-7 → main_01-07` ✓ |
| `wk_*` | 放行 | `CE-6 → wk_melee_6`、`LS-6 → wk_kc_6` ✓ |
| `act*` | **必须在 `config["activity_stages"]` 里**（按 `stage_label` 比对） | TO-5/7/8/9 ✓、TO-1~4/6 ✗ |
| 其他（`ro_`、`camp_`、`sandbox_`、`mem_`、`lt_`…） | 拒绝 | 未实测，按 fail-closed 拒 |

资源本的 5 个 id 前缀都验过是 `wk_`：
`wk_melee_6 / wk_kc_6 / wk_toxic_5 / wk_armor_5 / wk_fly_5`。

```python
NAVIGABLE_PREFIXES = ("main_", "wk_")


def stage_is_navigable(
    stage_id: str, stage_label: str, config: dict[str, Any]
) -> tuple[bool, str]:
    """Can `maa run` (Fight) actually navigate the game to this stage?

    `maa copilot` never navigates on its own (its chain starts at
    BattleStartPre), so the Fight-based nav step is the only way in — and
    Fight accepts exactly the stages `maa activity` reports, verified
    stage-by-stage against TO-1..TO-9.  Allowlist, because guessing wrong
    costs a held run.lock plus a killed emulator.
    """
    if stage_id.startswith(NAVIGABLE_PREFIXES):
        return True, ""
    if not stage_id.startswith("act"):
        return False, f"{stage_label} 不是主线/资源本/当期活动关，MAA 无法自动导航。"
    activity = [str(s) for s in (config.get("activity_stages") or [])]
    if not activity:
        return False, (
            f"当期活动关清单未知，无法确认 {stage_label} 能否导航，先不执行。"
            "（等 07:50 计划卡刷新，或手动跑 maa activity Official）"
        )
    if stage_label in activity:
        return True, ""
    return False, (
        f"{stage_label} 有作业，但 MAA 只能自动导航到当期活动的 "
        f"{'、'.join(activity)}。这关只能手动打。"
    )
```

## A-2 挂在哪

**两处都要挂**，理由不同：

1. **`maa_action.py` 的 `copilot find`**：主闸门。在 `valid_copilot_stage()` 之后、
   写 `searches/<token>.json` 之前拒掉，直接回一句话，**不派 worker、不发候选卡片**。
   这是决策 1 要的效果——省掉整条链路。
2. **`maa_action.py` 的 `copilot pick`**：兜底。候选卡片 TTL 有 25 小时，活动可能在这
   期间轮换（`activity_stages` 每天 07:50 刷新）。这里再查一次，几乎零成本。

**不要**挂在 worker 的 `cmd_search` 里当唯一闸门——那时候卡片已经要发出去了。

## A-3 顺带修掉 `activity_stages` 过期

闸门建立在这份清单上，它过期就会**误拒**合法关卡（现在就少了 TO-5）。
现状：只有 `maa_plan_card.refresh_activity_stages()` 在 07:50 刷。

在 `maa_copilot_worker.cmd_search` 里加一次机会性刷新，**照抄现有 roster 懒刷新那段的
写法**（`maa_copilot_worker.py:135` 附近，P1-5 的成果）：

```python
        try:
            import maa_plan_card
            ok, detail = maa_plan_card.refresh_activity_stages(config)
            if not ok:
                print(f"activity refresh skipped: {detail}", flush=True)
        except Exception as exc:
            print(f"activity refresh skipped: {type(exc).__name__}: {exc}", flush=True)
```

`maa activity Official` 实测约 1 秒，在 worker 里（已脱离 20s 预算）完全够。
失败必须降级继续，不能中断搜索。

> ⚠️ 这里有个顺序陷阱：闸门在 `copilot find`（快路径）读的是**刷新前**的
> `config["activity_stages"]`。所以第一次搜 TO-5 仍会被误拒，刷新后第二次才通过。
> 可接受（一天最多错一次），但**必须在拒绝文案里说清楚**，别让用户以为是永久结论：
> 「若你确认该关当期开放，等一次 07:50 刷新或重试一次。」

## A-4 SKILL.md

`skills/games/maa-operator/SKILL.md` 第 8 条现在是：

> 8. 抄作业关卡不在本地索引里就直接拒绝，不许猜别名。

改成两条，把新的失败模式写清楚：

> 8. 抄作业关卡不在本地索引里就直接拒绝，不许猜别名。
> 9. 脚本回「MAA 无法自动导航」时，**原样转述，不要建议用户手动进编队界面再让你开打**，
>    也不要提议改用别的关卡代打。这类关卡本期不支持，就是不支持。

（第 2 条「不要调用 `maa_copilot_worker.py`」等已有约束不变。）

## A-5 测试

新增 `tests/test_copilot.py::NavigationGateTests`：

- `main_01-07` / `wk_melee_6` / `wk_kc_6` → 放行，且**不读 `activity_stages`**
  （传 `config={}` 也必须放行）
- `act53side_05` + `activity_stages=['TO-9','TO-8','TO-7','TO-5']` → 放行
- `act53side_01` + 同上 → 拒绝，且 reason 里含 `TO-9、TO-8、TO-7、TO-5`
- `act53side_01` + `activity_stages=[]` → 拒绝，且 reason 是「清单未知」那条
  （两种拒绝理由不能混，用户要能分辨「不支持」和「不知道」）
- `ro_*` / `camp_*` / `sandbox_*` → 拒绝
- `copilot find TO-1` 全链：断言**不写** `searches/<token>.json`、**不 Popen** worker
- `copilot pick` 对已失效活动关的兜底拒绝

---

# B. 自动任务执行前探测模拟器

## B-1 行为

`maa_daily.py` 的 `execute(phase)`，**只对 `phase in {"am", "pm"}` 生效**
（`PHASE_LABELS = {"am": "早", "pm": "晚", "now": "手动"}`）。
`now` 是用户明确说「现在跑」，**必须照跑，不许探测跳过**。

拿到 `run_lock` 之后、`open_emulator()` 之前：

```
模拟器在线？ ──是──> 判定「用户正在玩」 → 跳过本场 → 发 Discord 通知 → return 0
     │
     否
     ↓
  照常执行
```

## B-2 怎么算「在线」

两个信号取**或**，任一命中就算在线：

1. BlueStacks 进程存在（`pgrep -f`，参照现有 `SAFE_APP_RE` 白名单校验，别拼 shell）
2. adb 能连上且 `getprop sys.boot_completed` 为 `1`

只用 adb 不够：BlueStacks 刚启动、Android 还没起来时 adb 连不上，但用户确实在用。
只用进程也不够：有可能进程残留而设备已死。

**探测必须快**——总预算 ≤5 秒，`adb` 子进程 `timeout=3`。
**绝对不要复用 `wait_for_android()`**，那个默认等 180 秒。

## B-3 跳过必须上报，不能静默

这是最重要的一条。仓库里已经有一个静默跳过的坑：`maa_daily.py:777`

```python
    with run_lock(blocking=False) as acquired:
        if not acquired:
            return 0            # ← 静默
```

（`PLAN_PHASE2.md` 护栏 #4 已经点过它：07:58 起跑的抄作业会无声吃掉 08:00 的日常，
所以才有了 `copilot_blackout_minutes`。）

**不要再加一个静默分支。** 如果模拟器卡死没退干净，静默跳过会让日常刷本**永久停摆
且无人知情**。跳过时必须发 Discord，照抄现有 `⏭️ MAA 日常·{PHASE_LABELS[phase]}已跳过`
（`maa_daily.py:819`）那条的形状：

```
⏭️ MAA 日常·早场已跳过
检测到模拟器正在运行，判定你在玩，本场不抢设备。
需要照跑就说「现在跑」。
```

最后一句很关键——它给了用户一条明确的出路，否则模拟器忘了关就没法跑日常了。

同时把跳过原因写进 `last_run.json`（`status` / `summary`），让 `log tail` 能看到。

## B-4 测试

`tests/` 里新增：

- `phase="now"` + 模拟器在线 → **照常执行**（不跳过）。这条最容易写反，务必有
- `phase="am"` + 模拟器在线 → 跳过，且**发了 Discord 通知**（断言 discord_request 被调用）
- `phase="am"` + 模拟器离线 → 正常执行，`open_emulator` 被调用
- 探测里 adb 超时/抛异常 → **不得让整场挂掉**；按「离线」处理并照常执行
  （宁可抢一次设备，也不要因为探测本身出错就永久停摆）
- 断言探测没有调用 `wait_for_android`

---

# 验证

```bash
python3 -m unittest discover -s tests -v        # 应 ≥ 88 + 新增用例，全绿

# A：闸门判据（不需要模拟器）
python3 -c "import sys,json,os; sys.path.insert(0,'scripts'); import maa_copilot as c; \
  cfg=json.load(open(os.path.expanduser('~/.hermes/state/maa/config.json'))); \
  [print(f'{s:<6}', c.stage_is_navigable(sid, s, cfg)) for s, sid in \
   [('TO-1','act53side_01'),('TO-5','act53side_05'),('TO-9','act53side_09'), \
    ('CE-6','wk_melee_6'),('1-7','main_01-07')]]"
# 期望：TO-1 拒、TO-5 放行（需先刷过 activity_stages）、TO-9/CE-6/1-7 放行

# A：find 必须早退，不留 search 文件
python3 ~/.hermes/scripts/maa_action.py copilot find TO-1
ls ~/.hermes/state/maa/copilot/searches/          # 不得新增文件

# A-3：确认 activity_stages 已刷新到 4 个
python3 -c "import json,os; print(json.load(open(os.path.expanduser( \
  '~/.hermes/state/maa/config.json')))['activity_stages'])"
# 期望 ['TO-9','TO-8','TO-7','TO-5']

# B：模拟器在线时 am 跳过、now 照跑（开着 BlueStacks 手动验一次）
python3 ~/.hermes/scripts/maa_action.py run now      # 必须照跑

# 复核 Fight 真实接受集（本文档结论 2 的原始实验，约 1 分钟）
# 逐关写 Fight toml 跑 maa run，"Failed to add task" 即被拒；
# 不需要模拟器——拒绝发生在连接设备之前。
```

回归确认：08:00 / 22:00 日常刷本未受影响、`buttons.json` 里 agent-book 的键完好、
全库无 `--use-sanity-potion`、`stone = 0`、`deploy.py --check` 干净。

---

# 不做的事

- **半自动模式**（手动进编队界面 + 「就地开打」按钮）——用户明确否决。
- **用 `type = "Custom"` 自建导航链。** maa-cli 确实支持
  （实测 `Custom Start` → 因界面状态而 `Custom Error`，类型本身可用），
  理论上可用 `TOChapterToTO` + `FullStageNavigation` + `ClickStageName` 拼出
  TO-1~TO-6 的导航。**但依赖 MAA 未文档化的内部任务名，每次 `maa update` 都可能失效，
  收益只是活动本前几关。** 真要做单独立项，不要塞进本次。
- **「不是自己开的模拟器就别关」**——被 B 取代。
