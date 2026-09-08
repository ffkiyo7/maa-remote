# Spike：`copilot_list` 多作业模式接在现有 Fight 导航之后（真机）

日期：2026-09-08 23:35 – 23:58（北京时间）
执行者：Claude。**未改动仓库、`~/.hermes/scripts/`、`~/.config/maa-cli/` 的任何文件**；
所有产物在 `/private/tmp/claude-501/-Users-wadorudi-Desktop-dev-maa-remote/9daa745d-eac7-4a0a-b6bc-afe2f2fa1e90/scratchpad/`（下文简称 `<S>`）。

## 结论速览

| | 结果 | 一句话 |
|---|---|---|
| **Q1** | **✓** | 不用关详情面板。Fight 导航停在 SR-5 详情界面 → `copilot_list` 第 1 项（SR-5）走「详情已展开」快路径 0 点击接手；第 2 项（SR-6）在战斗结束回到关卡列表后由插件全屏 OCR 找到并点击，**一场模拟器内连打两关都三星通过** |
| **Q2** | **✗（区分不了）** | maa-cli 0.7.5 的 stdout **不打印 `CopilotListLoadTaskFileSuccess`**（二进制里根本没这个字符串），`Copilot Start` / `Copilot Completed` 是**任务级**、整趟只各一次。逐关只能靠 `BattleFormation` / `MissionStart` 出现次数**数**，且**看不出哪一关赢了**。「第 1 关成功、第 2 关失败」**无法**从 stdout 区分 |
| **Q3** | **未验（被别的东西卡住）** | 第 3 项（SR-5 `is_raid=true`）**根本没走到突袭切换**：SR-6 首通弹出活动的「获得了塔罗牌」全屏弹窗，插件的导航路径**不处理任何弹窗**，`find_stage` 只 OCR 到塔罗牌文字，`Copilot@FullStageNavigation` 空滑，死循环 9 分钟直到我 900s 超时。`RaidConfirm` / `ChangeToRaidDifficulty` 一次都没执行 |

理智：**实耗 33**（SR-5 -12、SR-6 -21）。前 207/207，后约 174。第 3 项没进战斗，不耗理智。
全程 `medicine=0` / `stone=0`、没传 `use_sanity_potion`、没传 `--ignore-requirements`。

---

## 0. 前置检查（全部通过）

```
$ TZ=Asia/Shanghai date "+%Y-%m-%d %H:%M:%S %Z"
2026-09-08 23:35:41 CST          # < 03:30 ✓

$ pgrep -f BlueStacks
(空)                              # ✓

$ python3 -c "...activity_watch.json..."
status: ready                     # ✓ 不是 preparing/executing
```

- `run.lock`：`maa_common.run_lock(blocking=False)` 一次抢到 → `[23:41:07] run.lock acquired`，
  整个 spike 期间持有，退出时释放。
- `last_run.json` 显示 22:00 那场 `"status": "skipped"`，当晚没消耗理智。
- cron（`~/.hermes/cron/jobs.json`，**表达式是 UTC**）：日常 `0 0`/`0 14` = BJT 08:00/22:00，
  Depot `30 20` = BJT 04:30，计划卡 `50 23` = BJT 07:50 —— 都不与本次窗口冲突。

> **注意（不是我干的）**：`activity_watch.json` 的 `status` 在 23:45:40 由 `ready` 变成
> `dismissed`。那是 cron 里 `*/15 * * * *` 的「MAA 活动作业监控」（`maa_activity_watch.py`）
> 干的（`last_notified_error: "搜索状态丢失或已过期"`，token `ec679c61bb10` 是 15:30 那次搜索遗留）。
> 本 spike 没有读写过这个文件，也没写 `last_run.json` / `plan.json` / `searches/`，没发任何 Discord 消息。

## 1. 选关与理智预算

`~/Library/Application Support/com.loong.maa/resource/stages.json`：

| code | stageId | apCost |
|---|---|---|
| SR-1 / SR-2 | act54side_01/02 | 9 |
| SR-3 / SR-4 | act54side_03/04 | 12 |
| **SR-5** | act54side_05 | **12** |
| **SR-6** | act54side_06 | **21** |
| SR-7 / SR-8 | act54side_07/08 | 21 |

`resource/tasks/Stages/SR.json` 顶层键只有
`SR-5 / SR-6 / SR-7 / SR-8`（+ 各自 `@SideStoryStage`、`SR-Open`、`SR-OpenOcr`、
`SR-OpenOpt`、`SRChapterToSR`）——**SR-1..SR-4 没有键**，上游 Fight 导航不认。
所以「有导航定义且最便宜的两关」= **SR-5(12) + SR-6(21) = 33**，在 60 上限内。

理智估算（`maa_copilot.load_roster()` + `estimated_sanity(roster["sanity"])`）：

```
fetched_at  2026-09-08T15:30:47+08:00
sanity      {"current":186,"max":207,"last_ap_add_at":1788852133,"server_current_at":1788852646}
estimated_sanity(...) -> 207
sanity_preflight(roster, 12) -> {'known':True,'enough':True,'current':207,'max':207,'cost':12}
```

真机截图 `<S>/02-after-fight-nav-SR-5.png` 右上角实读 **207/207**，与估算一致。

## 2. 作业来源

```python
maa_copilot.search_copilots(stage_id, code, config, roster, limit=1000, use_cache=False)
```

| 关卡 | 候选总数 | `ratio==1.0 and gaps==[] and soft_gaps==[]` | 选用 |
|---|---|---|---|
| SR-5 / act54side_05 | 21 | 9 | id=**101751**「SR-5 - 月斜 初版」quality 0.9071，作者 萨拉托加 |
| SR-6 / act54side_06 | 22 | 8 | id=**101752**「SR-6 - 月蚀 初版」quality 0.9121，作者 萨拉托加 |

两份作业的 `content.stage_name` 本来就等于 stageId（`execution_uri()` 返回
`maa://<id>`、`needs_rewrite=False`），但 `copilot_list.filename` 要**本地文件路径**，
所以按 `_prepare_op_file()` 的 rewrite 分支自己拉全文落盘：

- `<S>/op-101751.json`（干员：娜仁图亚、圣聆初雪、德克萨斯）
- `<S>/op-101752.json`（干员：圣聆初雪、德克萨斯）

## 3. 上机前的源码依据

`MultiCopilotTaskPlugin.cpp`（v6.17.2，本次现拉到 `<S>/MultiCopilotTaskPlugin.cpp`）的
`navigate_to_stage()` 是 Q1 的关键：

```cpp
std::string templ_path = StageNavigationHelper::get_stage_template_path(stage_name);
if (!templ_path.empty()) { ... 模板匹配分支 ... }

auto image = ctrler()->get_image();
if (is_stage_detail_opened(image)) {          // StartButton1 模板 = "开始行动"
    bool ret = confirm_stage_name(image, stage_name);
    if (ret) { return true; }                 // ← 详情面板已经是目标关：0 点击直接成功
}
auto stages = find_stage(image, ...);         // ← 否则整屏 HSV 阈值 + OCR 找关卡名
if (命中) { if (enter_stage(it->rect, stage_name)) return true; }
ProcessTask(*this, {"Copilot@FullStageNavigation"}).set_retry_times(20).run();   // 右滑 10 次
... 再 find_stage ... 再 Copilot@StageNavigationSlowlySwipeLeft ... 再 ChapterSwipeToTheRightAndPlot
return false;
```

本机 `resource/template/StageNavigation/SideStory/` 只有 `FC IC IS LE NL OF SN TD WB`，
**没有 `SR`** → 走 OCR 分支（实测日志里就是 `No stage template available, using image-based OCR for SR-5`）。

`CopilotTask.cpp::set_params` 的 list 分支：每一关的 subtask 链被整体复制 N 份，
链条是 `MultiCopilotTaskPlugin` → `BattleStartPre` → `BattleStartPre@UseMedicine`(disabled)
→ `BattleFormationTask` → `BattleStartAll` → `SkipForbiddenOperConfirm` → `BattleProcessTask`
→ stop task(`Copilot@ClickCornerUntilEndOfAction`，即「非三星即停」)。
本次 3 项 = **24 个 subtask**（日志：`run subtask 1 / 24`）。

**`Copilot Start` / `Copilot Completed` 是整个 Copilot 任务级的，不是逐关的**——Q2 的根因。

插件里另有一条对本项目直接有用的：导航成功后固定跑 `ProcessTask({"NotUsePrts"})`（关代理指挥），
所以 `maa_copilot_worker` 那个「确认要接管作战」watchdog 在 list 模式下多半用不上
（本次两关的 `NotUsePrts` 都是 `SubTaskError`，因为代理指挥本来就没开，`ignore_error` 吃掉）。

## 4. 任务文件与 dry-run

临时 `MAA_CONFIG_DIR` = `<S>/cfg/`（从 `~/.config/maa-cli` 复制 `profiles/ resource/ cli.toml`，
**没有**任何 `FightBegin` 覆盖），任务文件 `<S>/cfg/tasks/copilot-list.toml`。
`~/.config/maa-cli/tasks/` 一个字节都没动。

```toml
[[tasks]]
name = "多作业连打"
type = "Copilot"

[tasks.params]
formation = true

[[tasks.params.copilot_list]]
filename = "<S>/op-101751.json"
stage_name = "SR-5"
is_raid = false

[[tasks.params.copilot_list]]
filename = "<S>/op-101752.json"
stage_name = "SR-6"
is_raid = false

[[tasks.params.copilot_list]]
filename = "<S>/op-101751.json"
stage_name = "SR-5"
is_raid = true
```

```
$ MAA_CONFIG_DIR=<S>/cfg maa run copilot-list --dry-run -vv --batch
[2026-09-08 23:41:01 DEBUG] Adding task [多作业连打] with params: {
  "copilot_list": [
    {"filename": ".../op-101751.json", "is_raid": false, "stage_name": "SR-5"},
    {"filename": ".../op-101752.json", "is_raid": false, "stage_name": "SR-6"},
    {"filename": ".../op-101751.json", "is_raid": true,  "stage_name": "SR-5"}
  ],
  "formation": true
}
```

**maa-cli 0.7.5 的 `maa run` 把 `[tasks.params]` 原样透传给 MaaCore**，
`[[tasks.params.copilot_list]]` 表数组正确变成 JSON 数组。
另外 dry-run 输出里有 `Loading resource from <S>/cfg/resource`，说明
`maa run` 会自动把 `$MAA_CONFIG_DIR/resource` 当 user resource 加载，**不需要 `--user-resource`**。

## 5. 真机执行

脚本 `<S>/spike.py`；日志 `<S>/spike.out`（时间线）、`<S>/nav.log`（Fight 导航）、
`<S>/copilot-list.log`（`maa run copilot-list` 的 stdout）。

### 5.1 开机 + Fight 导航

```
[23:41:07] estimated sanity BEFORE = 207
[23:41:07] run.lock acquired
[23:41:25] emulator ready; settle=          # open_emulator → wait_for_android
                                            # → verify_game_installed → settle_android_runtime
[23:42:46] _navigate_to_stage(SR-5) -> ok=True detail='' (81s)
```

`<S>/nav.log`：

```
[启动国服] 23:41:28 - 23:42:05 (36s) Completed
[导航到关卡] 23:42:05 - 23:42:43 (38s) Completed
[检查关卡界面] 23:42:44 - 23:42:46 (1s) Completed
StartUp Start / StartUp Completed / Fight Start / Fight Completed
Custom Start / Custom Completed / AllTasksCompleted
```

截图 **`<S>/02-after-fight-nav-SR-5.png`**：SR-5 关卡详情界面，右上「207/207」，
左下地图上 `SR-4 / SR-5 / SR-6` 三个节点同屏可见，右下「开始行动 -12」，
「代理指挥」未勾选，SR-5 已三星。

### 5.2 `maa run copilot-list`

```
[23:42:47] RUN: MAA_CONFIG_DIR=<S>/cfg /opt/homebrew/bin/maa run copilot-list -v --batch
[23:57:55] maa run copilot-list TIMEOUT 900s
[23:57:55] maa run copilot-list rc=124 (908s)
[23:58:09] close_everything -> []
[23:58:09] EXIT 0
$ pgrep -f BlueStacks
(空)                                          # ✓ 复核
```

`<S>/copilot-list.log` **完整内容**（27 行，一字不删）：

```
 1  Updating hot update files...
 2  Hot update completed successfully
 3  Connected
 4  FastestWayToScreencap RawByNc 102
 5  Copilot Start
 6  BattleFormation 娜仁图亚, 圣聆初雪, 德克萨斯, 奶
 7  BattleFormationSelected 华法琳
 8  BattleFormationSelected 德克萨斯
 9  BattleFormationSelected 圣聆初雪
10  BattleFormationSelected 娜仁图亚
11  MissionStart
12  CurrentSteps Deploy 德克萨斯
13  CurrentSteps Deploy 圣聆初雪
14  CurrentSteps Deploy 奶
15  CurrentSteps Deploy 娜仁图亚
16  CurrentSteps SwitchSpeed 
17  CurrentSteps SkillDaemon 
18  BattleFormation 圣聆初雪, 德克萨斯
19  MissionStart
20  CurrentSteps Deploy 圣聆初雪
21  CurrentSteps Deploy 德克萨斯
22  CurrentSteps SwitchSpeed 
23  CurrentSteps SkillDaemon 
24  Summary
25  ----------------------------------------
26  [多作业连打] 23:42:48 - Unfinished
27  Error: Interrupted by user!
```

（第 27 行的 `Interrupted by user!` = 我的 900s 超时 `proc.terminate()`，不是游戏里的中断。）

---

## Q1 —— ✓ 通过（不必先回关卡列表 / 不必关详情面板）

### 第 1 项 SR-5：详情面板开着直接接手，0 点击

`~/Library/Application Support/com.loong.maa/debug/asst.log`（下称 asst.log）L26599–26646：

```
23:42:48.103  run | run subtask 1 / 24 {"class":"asst::MultiCopilotTaskPlugin",...,"taskid":1}
23:42:48.103  asst::MultiCopilotTaskPlugin::_run | enter
23:42:48.103  SubTaskExtraInfo {"details":{"file_name":"<S>/op-101751.json","id":-1,
                                "stage_name":"act54side_05"},...,"what":"CopilotListLoadTaskFileSuccess"}
23:42:48.103  No stage template available, using image-based OCR for SR-5
23:42:48.252  analyze | OcrDetect StartButton1 [{ text: 开始行动, rect: [1126,645,104,26], score: 0.999991 }]
23:42:48.335  asst::CharOcr [{ text: SR-5, rect: [79 (924), 10 (82), 84, 28], score: 0.996437 }, ...]
23:42:48.841  ProcessTask ... to_be_recognized:["NotUsePrts"]
23:42:48.984  SubTaskError {"first":["NotUsePrts"],...}          # 代理指挥本来就没开，ignore_error
23:42:48.984  asst::MultiCopilotTaskPlugin::_run | leave, 881 ms
23:42:49.486  run | run subtask 2 / 24 ... first:["BattleStartPre"]
```

→ `is_stage_detail_opened()` 命中「开始行动」，`confirm_stage_name()` 在
`ClickedCorrectStage` 的 roi `[845,72,220,50]` 里 OCR 到 `SR-5`，
**881 ms 内返回 true，没有任何点击、没有任何滑动**。
Fight 导航留下的详情界面被 `copilot_list` 直接接手。

### 第 2 项 SR-6：从关卡列表全屏 OCR 找到并点进去

SR-5 战斗 23:43:16–23:46:15（`BattleProcessTask::_run | leave, 178566 ms`），
`Copilot@EndOfAction` → `StageDrops-Stars-3.png` **score 0.998274**（三星），
stop task `Copilot@ClickCornerUntilStartButton` 退回关卡列表。然后 asst.log L35261–35302：

```
23:46:24.660  asst::MultiCopilotTaskPlugin::_run | enter
23:46:24.662  SubTaskExtraInfo {"details":{"file_name":"<S>/op-101752.json","id":-1,
                                "stage_name":"act54side_06"},...,"what":"CopilotListLoadTaskFileSuccess"}
23:46:24.662  No stage template available, using image-based OCR for SR-6
23:46:24.834  asst::WordOcr [{ text: _, rect: [0 (1010), 0 (625), 260, 61], score: 0.772052 }]
              ← StartButton1 未命中：此刻详情面板是关着的（战斗结算已退回列表）
23:46:25.086  find_stage stage results: [{text: PERATION, rect:[33,390,...]},
                {text: OPERATION, rect:[349,390,...]}, {text: PERATION, rect:[620,393,...]},
                {text: OPERATION, rect:[878,390,...]}, ...]
23:46:25.086  Click with scaled coordinates (877, 412) 1.5
23:46:25.086  maatouch click: (1315, 618)                       ← 点 SR-6 节点
23:46:25.874  analyze | OcrDetect StartButton1 [{ text: 开始行动, ... score: 0.999991 }]
23:46:26.442  asst::CharOcr [{ text: SR-6, rect: [79 (924), 10 (82), 84, 28], score: 0.996329 }, ...]
23:46:27.086  asst::MultiCopilotTaskPlugin::_run | leave, 2426 ms
23:46:27.591  run | run subtask 10 / 24 ... first:["BattleStartPre"]
23:46:27.786  analyze | OcrDetect BattleStartPre [{ text: 开始行动 ... }]   # 顺带 OCR 到 "-21"
23:46:30.048  BattleFormation {"formation":["圣聆初雪","德克萨斯"]}
23:46:31.205  match_templ | BattleStartNormal.png score: 0.998494 → maatouch click (1639,759)
```

→ **2.4 秒内自己点到了另一关并开打**，一次滑动都没用上。
SR-6 战斗 23:46:37–23:48:56（`BattleProcessTask::_run | leave, 139621 ms`），
`StageDrops-Stars-3.png` **score 0.978943**（三星）。

### Q1 的确切边界（照实说）

- **直接实测到的**：①「Fight 导航停在 SR-5 详情面板」+「list 第 1 项就是 SR-5」→ 快路径接手 ✓；
  ②「战斗结算退回关卡列表」+「list 下一项是另一关」→ 全屏 OCR 点过去 ✓。
- **没有直接实测到的**：「详情面板开着、而 list 第 1 项是**另一**关」。
  但源码摆着：`confirm_stage_name` 失败会**继续往下走** `find_stage(image, ...)`，
  而 `<S>/02-after-fight-nav-SR-5.png` 证明详情面板展开时 `SR-4 / SR-5 / SR-6`
  三个节点仍然在屏幕上、OCR 拿得到——所以这条路也应该通，只是本次没跑到。
- 结论：**「必须先回到关卡列表 / 必须先关掉详情面板」不成立。**

截图：`<S>/01-emulator-ready.png`（BlueStacks 启动器）、
`<S>/02-after-fight-nav-SR-5.png`（SR-5 详情，207/207，-12）、
`<S>/live-03..live-45.png`（每 60s 一张，覆盖两场战斗与之后的卡死）。

---

## Q2 —— ✗ 从 stdout **区分不了**逐关成败

### 事实 1：maa-cli 0.7.5 不认识 `CopilotListLoadTaskFileSuccess`

```
$ strings /opt/homebrew/bin/maa | grep -i "CopilotListLoadTaskFileSuccess"
(无输出)
```

这个 `what` 只出现在 **MaaCore 的 asst.log**（上面 Q1 引的两条
`SubTaskExtraInfo ... "what":"CopilotListLoadTaskFileSuccess"`），
**一行都不会进 maa-cli 的 stdout**。所以题面假设的那个标记在 CLI 输出里是不存在的。

### 事实 2：`Copilot Start` / `Copilot Completed` 是任务级、整趟各一次

3 关 = 一个 Copilot 任务的 24 个 subtask（`run subtask 1/24 … 10/24 …`），
所以 `Copilot Start` 只在第 5 行出现一次；正常跑完也只会有**一个** `Copilot Completed`。
本次因超时被杀，收尾是 `[多作业连打] 23:42:48 - Unfinished` + `Error: Interrupted by user!`。

### 事实 3：stdout 里唯一的逐关分隔是 `BattleFormation` / `MissionStart`

两关分别对应第 6–17 行和第 18–23 行，**每关一组** `BattleFormation` →
（可选 `BattleFormationSelected` ×N）→ `MissionStart` → `CurrentSteps ...`。
可以据此**数**打了几关，但：

- **看不出胜负**：三星判定（`Copilot@StageDrops-Stars-3` 模板匹配）只在 asst.log 里，
  stdout 一个字都没有。本次两关都三星，stdout 与「打了但没赢」长得一模一样。
- **看不出是哪一关**：`BattleFormation` 只列干员名，不含关卡名。要对上关卡得靠
  `copilot_list` 的顺序去猜，而中途某关导航失败时顺序就错位了。
- 「第 1 关成功、第 2 关失败」这种情形，stdout 只会显示 1 组 `MissionStart`
  （因为非三星即停）或 2 组但都没有成败标记 —— **推不出来**。

### 结论与替代判据

> **只看 maa-cli stdout 做不了逐关记账。** 要逐关判定，必须去
> `~/Library/Application Support/com.loong.maa/debug/asst.log` 拿 MaaCore 回调：
> - 每关开始：`SubTaskExtraInfo ... "what":"CopilotListLoadTaskFileSuccess"`，
>   `details` 里有 **`file_name`（本地作业路径）+ `stage_name`（stageId，如 `act54side_06`）**
>   —— 这是唯一带关卡身份的分隔标记。
> - 每关胜负：`match_templ | StageDrops-Stars-3.png`（三星）/ `StageDrops-Stars-2/1`。
> - 导航失败：反复出现 `No stage template available, using image-based OCR for <关卡>`
>   而中间没有 `CopilotListLoadTaskFileSuccess` 递进。
>
> 也就是说，现有 `maa_clearing.copilot_outcome()`（只认 `Copilot Completed` /
> `Copilot Error` / `Copilot Stopped` / `Error: Some error`）**在 list 模式下会退化成
> 「整趟一个结论」**，不能用来给队列的 cursor 逐关前进。

---

## Q3 —— 未验（没走到突袭那一步，卡在活动塔罗牌弹窗）

第 3 项 `{op-101751, "SR-5", is_raid=true}` 从 23:49:05 起反复导航失败，
到 23:57:55 被 900s 超时杀掉，`RaidConfirm` / `ChangeToRaidDifficulty` **一次都没执行**
（asst.log 里全程 0 次匹配）。

根因：**SR-6 首通后弹出活动「获得了塔罗牌」全屏弹窗，插件的导航路径不处理任何弹窗。**

asst.log L40096 起（第一次尝试）：

```
23:49:05.553  No stage template available, using image-based OCR for SR-5
23:49:05.710  asst::WordOcr [{ text: _, rect: [0 (1010), 0 (625), 260, 61], score: 0.986409 }]
              ← StartButton1 不命中（被弹窗盖住）
23:49:06.125  asst::CharOcr [{ text: X, rect: [1022,206,48,40] }, { text: 317H#, ... },
                { text: SEKYIT, ... }, { text: THEMAGICIAN, rect: [402,446,108,43], score: 0.990655 }]
23:49:06.126  find_stage stage results: [{text: 317H#...}, {text: SEKYIT...}, {text: THEMAGICIAN...}]
              ← 一个关卡名都没有，全是塔罗牌上的字
23:49:06.126  ProcessTask ... to_be_recognized:["Copilot@FullStageNavigation"]
23:49:06.126  Swipe with scaled coordinates (618,115) (301,114) 1.5   ← 空滑，弹窗吃掉
```

之后每 ~57 秒一轮，共 9 轮（23:49:05 / 23:50:03 / 23:50:59 / 23:51:56 / 23:52:53 /
23:53:50 / 23:54:47 / 23:55:44 / 23:56:41），每轮都是同样的
`find_stage → 只有塔罗牌文字 → FullStageNavigation 空滑 10 次 → SlowlySwipeLeft`。

截图 **`<S>/03-after-copilot-list.png`**（也见 `<S>/live-42.png`、`<S>/live-45.png`）：
全屏「获得了塔罗牌 / 魔术师 / 安德切尔 / NEW TAROT」，右上有个 `×`，
左边只露出半个 `SR-4` 节点。

**讽刺的是 MAA 早就为这个弹窗写了处理，只是没接到 copilot 链上**
（`resource/tasks/tasks.json`）：

```json
"SR-TarotClose": {
  "doc": "月行水上代理结算退出后概率弹出的塔罗牌弹窗，拦截点击；结算退出后先于开始行动关闭，此时无需返还次数",
  "algorithm": "OcrDetect", "text": ["获得了塔罗牌"], "roi": [580,280,180,60],
  "action": "ClickRect", "specificRect": [1044,219,10,10], "postDelay": 1000,
  "next": ["StartButton1"]
},
"SR-TarotClose-AfterStart": { "baseTask": "SR-TarotClose", "reduceOtherTimes": ["StartButton1"] }
```

引用它的只有 `PreStartCheck -> SR-TarotClose` 和 `StartButton1 -> SR-TarotClose-AfterStart`，
**即只在 Fight / StartButton1 那条链上**。`MultiCopilotTaskPlugin::navigate_to_stage()`
的 OCR 分支里没有任何 `ProcessTask` 会去关它。

> 顺带：`stages.json` 里 `act54side_*` **没有任何 `#f#` 突袭变体**，
> `<S>/02-after-fight-nav-SR-5.png` 里也看不到突袭切换按钮
> （`ChangeToRaidDifficulty` 的 roi `[600,610,130,110]` 处只有「演习 / 开始行动」）。
> 所以就算没被弹窗卡住，SR-5 大概率也**没有**突袭模式可切。
> **Q3 记「未验」，需要挑一关确实有突袭的关卡重跑。**

---

## 6. 对项目设计的含义

### Q1 通过 → 「Fight 导航一次 + `copilot_list` 连打」这条路**可行**

一场模拟器内连抄多关是成立的，成本比现在「每关一次 `_navigate_to_stage()` +
一次 `maa copilot`」低很多（本次两关只开了一次模拟器、跑了一次 StartUp、一次 Fight 导航）。
落地前提：

1. **首关必须由现有 Fight 导航送到详情界面**，且 `copilot_list` 的**第一项就是那一关**——
   这样插件走 881ms 的快路径，最稳。（不要指望 `copilot_list` 从主页自己进活动。）
2. **每一关都要在 `Stages/<前缀>.json` 有顶层键**，否则第 1 关的 Fight 导航进不去
   （后续关靠插件 OCR，不吃这张表，但首关吃）。
3. **必须用 `maa run <任务文件>`**，任务类型 `Copilot`，参数原样透传
   （`maa copilot` 只在 >1 个 URI 时才用 list 模式）。任务文件要写进**复制出来的临时
   `MAA_CONFIG_DIR`** 的 `tasks/` 下（本次做法可直接抄）。
4. **`filename` 必须是本地文件路径**，`maa://` 不行 → 复用 `_prepare_op_file()` 的
   rewrite 分支，把所有作业（不管 `needs_rewrite`）都落到本地。
5. **超时要按关数放大**：本次两关战斗就占了 318s，加上导航/编队/结算，
   3 关跑到 900s 是不够的。建议 `每关 400s + 120s` 之类的公式，别沿用单关的 900s。
6. `--formation` 对应 `formation = true`；`medicine/stone/use_sanity_potion` 一律不传。
   watchdog（接管弹窗）在 list 模式下可以不带——插件自己会跑 `NotUsePrts`。

### 但有两个**必须先解决**的坑，否则不能上生产

**坑 A（Q2）：逐关成败在 maa-cli stdout 里读不出来。**
现在的 `maa_clearing.copilot_outcome()` 在 list 模式下只能给出「整趟一个结论」。
通关队列的 cursor 是**逐关**前进的，一旦按整趟结论记账，要么把没打的关标成打了
（cursor 静默跳关，正是 `copilot_outcome` 的 docstring 里最怕的事），
要么把打过的关重打（白烧理智）。
→ 想上 list 模式，**必须改成读 asst.log 的 `CopilotListLoadTaskFileSuccess` +
`StageDrops-Stars-N`**，或者退一步：**一次 list 只放一关**（那就没意义了）。
这不是小改动，得先决定要不要引入「解析 MaaCore asst.log」这条新依赖。

**坑 B（Q3 附带发现，比 Q3 本身重要）：插件导航不处理弹窗，会死循环到超时。**
本次 SR-6 首通弹出塔罗牌，插件就在那儿空滑了 9 分钟，
**既不报错也不退出**，一直到外部超时才停。真跑生产的话这是「白占 run.lock 15 分钟 +
后面几关全丢」。而且 MAA 明明有 `SR-TarotClose`，只是没接到 copilot 链上。
可选对策：
- 学 worker 现有的接管弹窗 watchdog，在 `maa run` 期间 OCR 到「获得了塔罗牌」就
  ADB 点右上角 `×`（弹窗的 `×` 在 1280×720 坐标约 `(1044,219)`，`SR-TarotClose`
  的 `specificRect` 就写着这个值，可以直接照抄换算）；
- 或者临时 `MAA_CONFIG_DIR` 里给 `Copilot@FullStageNavigation` 之类加一层
  overlay 把 `SR-TarotClose` 接进去（但这就是「自写导航定义」，与
  PLAN_CLEARING 的硬闸门抵触，得先讨论）；
- 或者干脆**每关之间仍旧回到现有的 Fight 导航**（Fight 链上有 `SR-TarotClose`），
  即放弃 list 模式的导航、只用它省 StartUp —— 但 list 模式的导航是它唯一的卖点，
  这样就没什么剩下的了。

**一句话**：Q1 证明了技术路径通，但 Q2（读不出逐关成败）和坑 B（弹窗死循环）
是两个**独立的、都必须解决**的前置条件；两个都指向「不能只依赖 maa-cli stdout」。

---

## 7. 产物清单

| 文件 | 说明 |
|---|---|
| `<S>/spike.py` | 本次执行脚本（只读线上模块，不改任何东西） |
| `<S>/spike.out` / `<S>/spike.log` | 时间线 |
| `<S>/nav.log` | Fight 导航（StartUp + Fight + Custom）输出 |
| `<S>/copilot-list.log` | `maa run copilot-list` 的完整 stdout（27 行） |
| `<S>/cfg/tasks/copilot-list.toml` | 临时任务文件 |
| `<S>/op-101751.json` / `<S>/op-101752.json` | 落到本地的两份作业 |
| `<S>/candidates.json` | 两关的完美候选列表 |
| `<S>/MultiCopilotTaskPlugin.cpp` | v6.17.2 插件源码（本次现拉） |
| `<S>/01-emulator-ready.png` | BlueStacks 启动器 |
| `<S>/02-after-fight-nav-SR-5.png` | **Fight 导航后的 SR-5 详情界面（207/207，-12）** |
| `<S>/live-03.png` … `<S>/live-45.png` | 执行期间每 60s 一张 |
| `<S>/03-after-copilot-list.png` | **卡死现场：活动塔罗牌全屏弹窗** |

MaaCore 侧原始日志（非本目录，会被轮转）：
`~/Library/Application Support/com.loong.maa/debug/asst.log`，
本次相关区间 **L25540–L45100**（2026-09-08 23:41:32 – 23:57:5x）。

## 8. 本次对线上状态的写入（自查）

`find ~/.hermes/state/maa -newermt "2026-09-08 23:35" -type f`：

- `copilot/cache/act54side_05.json`、`copilot/cache/act54side_06.json`
  —— **我写的**，是 `search_copilots(use_cache=False)` 的既有副作用（12h TTL 搜索缓存），无害。
- `activity_watch.json`、`config.json`
  —— **不是我写的**，是 cron 里 `*/15 * * * *` 的 `maa_activity_watch.py`（23:45:40、00:00:40 各一次）。

`last_run.json` / `plan.json` / `copilot/searches/` 一个字节都没动；没发任何 Discord 消息；
`~/.config/maa-cli/tasks/` 四个文件 mtime 全部早于本次会话；
`~/.hermes/scripts/` 全部脚本 mtime 未变；`git status` 里没有本 spike 产生的新文件
（`docs/PLAN_PHASE4.md` 与 `docs/spikes/` 是并行的其它会话留下的，与本 spike 无关）。
