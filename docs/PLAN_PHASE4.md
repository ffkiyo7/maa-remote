# 四期：场次级计划卡 + 作业链统一 + 素材权重 + 肉鸽

> **本文按顺序分六步（0~5），每一步都可以单独派发给一个没有本次对话上下文的 agent 执行。**
> 每步自带「前置 / 目标 / 做法 / 允许改动的文件 / 禁止触碰 / 验收边界」六节。
>
> **本文不含实现代码。** 只描述做什么、改哪里、怎么算做完。
>
> **本文的线上快照会过期。** 每步开工第一件事是按 `archive/PLAN_CLEARING.md` 全局护栏 9 对一遍现场；
> 对不上就以现场为准并回报。本文写于 2026-09-08 深夜。

> **状态（2026-09-09 结项）**：步骤 0~5 已全部交付并部署（commit `560da38`…`6df489a`），当日两个线上缺陷（手动通关重抄、抄完杀游戏导致代理指挥未解锁）已修并真机验证。四期到此结项；遗留缺口不再在本文推进，列在这里备查：突袭功能默认关闭（首次开放的 SideStory 可能本来就没有突袭）、`copilot_list` 挂起、EX 关理智无数据源、肉鸽未真机跑过、抄作业收尾停在结算页不回主界面（已验证无害）。

## 进度

- [x] ~~步骤 0 — 收尾：提交并部署 09-08 的自动定关改版；排查 07:50 上游更新连续失败~~（09-09 验收通过：修 2 处缺陷补 4 条测试；根因 libgit2 不走代理，`cli.toml` 改 `git` 后端）
- [x] ~~步骤 1 — 场次级计划 + 早晚两张卡 + 单下拉 + 抄作业卡分离 + 模拟器探测统一~~（09-09 验收通过并部署：372 项单测；打回一次恢复 `log tail`；计划卡 custom_id 带场次；真机四条待看频道）
- [x] ~~步骤 2 — 作业链统一：静默执行、普通+突袭、已通关跳过、理智调度、串行多关~~（09-09 验收通过并部署：357 项单测，净减约 2000 行；打回一次补手动路径 blackout 闸门；突袭切换 `ChangeToRaidDifficulty` **09-09 真机验证失败**，`copilot_raid_switch_task` 种子已改空串 = 突袭功能关闭，见「Spike 结论 / 突袭切换真机验证（2026-09-09）」）
- [x] ~~步骤 3 — 素材权重公式（一图流 / 企鹅物流）+ 活动期间 all-in + Fight drops 上限~~（09-09 验收通过并部署：新模块 `maa_farming.py`，397 项单测；EX 关理智一图流也不收录，仍是缺口）
- [x] ~~步骤 4 — 肉鸽：手动触发、上游全托管、优雅停止~~（09-09 验收通过并部署：`maa_roguelike.py` + `maa_roguelike_worker.py`，456 项单测；打回一次修测试抢真锁 + 开机前先查闸门；真机首跑待验）
- [x] ~~步骤 5 — 上游增量（上报掉落、Award 开关、OperBox 后备）~~（09-09 验收通过并部署：掉落匿名上报、Award / Mall 开关、OperBox 练度后备（fail-closed）、非活动关独立账本；494 项单测）
- [x] ~~spike A — `copilot_list` 真机验证~~（09-08 23:58 完成：Q1 ✓ / Q2 ✗ / Q3 未验 + 弹窗死循环；**`copilot_list` 挂起择日再验**，多关连抄走 B 基线，见「Spike 结论」与 `docs/spikes/copilot-list-2026-09-08.md`）
- [x] ~~spike B — 一图流活动日历 / 物品价值接口~~（09-08 23:35 实测完成，见「Spike 结论」与 `docs/spikes/yituliu-2026-09-08.md`）
- [x] ~~spike C — 肉鸽上游参数与优雅停止~~（09-08 只读调研完成，见「Spike 结论」与 `docs/spikes/roguelike-2026-09-08.md`）

---

## Context

一期（日常）、二期（抄作业）、三期（通关队列 + 材料驱动选关，`PLAN_CLEARING.md`）都在线上。
2026-09-08 用户复盘后决定**部分改造**：体验层重排（早晚各一张卡、每张卡只剩一个下拉）、
作业链从「候选卡 + 按钮」改成「选关即跑、跑完汇报」、素材计算接外部掉率与活动日历、
新增肉鸽。三期的通关队列不再以「全通关」为目标，只保留能复用的领域函数。

**所有时间按北京时间（UTC+8）**；cron 表达式按 UTC 写（`config.yaml: timezone: Etc/UTC`）。

---

## 决策记录（2026-09-08 用户拍板，执行时不要再问）

| # | 决策 |
|---|---|
| D1 | 每天两张计划卡：07:50（早场）与 21:50（晚场）。**晚场卡不跟随上游更新**（brew / `maa update` 只在 07:50 那张跑） |
| D2 | 计划卡只保留**一个下拉**（今天刷什么）+「跳过」按钮。**「跳过」只跳本场次**，当日另一场不受影响。删「最近结果」「请 Hermes 解释」和抄作业下拉 |
| D3 | 下拉手动选择**只影响本场次**：早上改了，晚上仍走缺口默认 |
| D4 | 「现在跑」取缺口默认关，不看卡片上的手动选择 |
| D5 | 默认关 = 仓库缺口计算结果。活动期间**只在活动关里选**，不刷主线、不刷常驻资源本（月卡都不开，理智必须 all in 活动才换得完代币商店）。手动从下拉选资源本仍然允许 |
| D6 | 抄作业是独立卡片，由聊天「抄作业」触发：开放关一览 + 一个下拉；**选中即执行**，不再有候选卡和 ▶️/🔥/换一批 按钮 |
| D7 | **活动数字关一律 `--raid both`**（先普通后突袭），作业匹配也按「同一份作业普通 + 突袭都打」考虑；资源本 / 主线只普通（它们没有突袭）。**突袭失败则该关不记为通关** |
| D8 | 自动匹配只针对**上游写了导航定义**的活动关（`Stages/<前缀>.json` 顶层键），常驻关不自动匹配；上游没导航的关不自动抄、不自动进（`PLAN_CLEARING` 步骤 2 的决策不变） |
| D9 | EX / S 关**不因为「还没开放」就不去匹配**：上游没有单关开放时间（见地基事实 E），按「活动进行中 + 该关已出现在本地索引与导航键 + 森空岛 zone `totalStage > 0`」判定，每轮重试 |
| D10 | 已通关判定 = **森空岛 zone 全清 + 本地记账**（森空岛只有 zone 粒度，这是妥协；代价是用户手打过的关可能被再抄一遍，用户接受）。**09-09 修正**：这个代价真的付了一次（SR-5/6/7/8 重抄，见「手动通关重抄」小节），补了 `copilot mark-cleared <关卡>` / `copilot unmark <关卡>` 让用户把手打过的关写进账本 |
| D11 | 自动匹配**静默执行 + 事后汇报**，不等确认；理智不够就排到理智够的时刻执行；执行后若理智不够跑默认关，日常跳过刷理智、其余环节照常 |
| D12 | **练度不满足的关自动跳过**，绝不用助战、绝不 `--ignore-requirements`（借了助战之后这关不能开代理，以后日常刷到它会执行不了） |
| D13 | 手动抄作业**不降级**：森空岛刷新失败直接报失败；同样不允许助战 / `--ignore-requirements` |
| D14 | 手动 vs 自动：自动负责活动上线后把上游有导航的关（普通 + EX + S，含突袭）首通一遍；手动只剩三种场景：练度不满足被自动跳过的关（等练度够了再手动补）、非活动关（主线 / 资源本）、重打某关。两者**共用一条执行链**，手动卡上显示自动进度 |
| D15 | `maa_clearing.py` 保留理智预算 / 通关判据 / 失败记账；**删掉** needs_manual、EX 闸门、编队 OCR 探测（那些是为「全通关」服务的） |
| D16 | 一场模拟器内**串行连抄多关**：基线是「每关一次上游导航 + 单作业 copilot」（现有队列 worker 的做法）；`copilot_list` 值得验（spike A），验过再决定是否换 |
| D17 | 素材公式三级**加权分**：① 平均用量 / 复刻时间预估（来自**一图流活动日历**：活动排期、每个活动掉落材料的时间与总结）② 仓库现缺口 ③ 掉落效率。用上游 Fight `drops` 参数（刷到 N 个即停）作为缺口计算的补充 |
| D18 | 自动行为（日常 / 抄作业 / 队列 / Depot / 肉鸽）开模拟器前**先探进程**：在则汇报并不执行（进程在 = 上一个任务没跑完或用户在玩） |
| D19 | 肉鸽：手动触发「刷肉鸽」，项目只负责调上游 + 持 `run.lock`，执行与输出由上游全程控制；支持当前全部肉鸽主题；上游可配置项做成 Discord 下拉（字段不多的话）；支持手动指令退出，**在本局结束后再停**，不把进度卡在地图里 |
| D20 | `copilot_list` 的 OCR 导航能点到上游没写导航定义的关，**不算**越过 D8 的边界（它是上游自己的机制） |
| D21 | 09-08 15:30 挂着的 SR-8 确认卡已取消（`ec679c61bb10` → canceled），重构期间不留旧卡 |
| D22 | （09-09）`copilot_list` 挂起择日再验，不提上游 issue；多关连抄按 B 基线做。执行方式：每步由新的 opus 子代理实现、不部署不提交，主会话逐步验收（代码整洁、实现质量、有无遗漏偏离），通过即分批提交并部署；新增文档先单独提交 |

---

## 地基事实（2026-09-08 逐条核对过；「实测」指有命令输出或文件可查）

### A. 线上现状

- **线上 = HEAD（`01c46f4`）**。工作区 `scripts/maa_action.py` / `scripts/maa_plan_card.py` / `tests/test_maa.py`
  的改动（删第三下拉、后台自动定关、活动关标材料、`stage_source` 进白名单）**未提交、未部署**；
  线上计划卡仍是三个下拉。`python3 deploy.py --check` 干净；单测 332 项全绿（2.8s）。
- `plan.json` 只有「按天」覆盖（`{date, stage, skip, source, updated_at}`），没有场次概念；
  `effective_plan()` 只叠 `stage` / `skip` 两个键。
- 一天只有 07:50 一张卡，卡片链路里绑着 brew / `maa update`。**09-07、09-08 两天上游更新都失败**
  （`SecureTransport error: connection closed via error; class=Net (12)`），cron 记 error，卡片照发。
- `emulator_is_online()` 全库只有 `maa_daily.execute()` 一个调用点（只在 am / pm）。
  抄作业 worker `_run_locked()`、队列 `_clearing_run_locked()`、`maa_depot.run_depot()` 都不探。
- Depot：只有 1 份快照（09-02 00:04），`consumption_history` 为空，下次到期 **09-09 04:30**。
  素材计算在此之前必然走手工权重降级。
- 森空岛：09-08 15:30 刷新成功，`roster.json` 有 `chars`（275 个）、`activity_progress`、`sanity`。
  **`activity_progress` 只有 zone 级** `{zoneId, clearedStage, totalStage}`，没有单关标记
  （`maa_skland._normalize_activity_progress()` 的注释亦如此）。
- `activity_watch.json`：SR 活动，目标 SR-8，那轮已取消（D21）。

### B. 上游 maa-cli 0.7.5 / MaaCore v6.17.2（实测 `--help` 与源码）

- `maa copilot [URI]...` **接受多个 URI**；源码 `crates/maa-cli/src/run/preset/copilot.rs`：
  展开后条目 > 1（多个 URI，或单 URI 配 `--raid both`）就走 MaaCore 的 **`copilot_list`** 模式，
  否则单文件模式（`filename` + `loop_times`）。`--raid` 是全局的，不能逐 URI 指定。
- MaaCore `CopilotTask::set_params`：`copilot_list` 每项 JSON 字段 **`filename` / `stage_name` / `is_raid`**
  （`CopilotTask.h`：`MEO_JSONIZATION(filename, stage_name, MEO_OPT is_raid)`；官方文档写的
  `nav_name_override` 是过时叫法）。`stage_name` 是**导航用的显示关卡名**（如 `SR-5`）。
- `MultiCopilotTaskPlugin::_run()`：每关跑 `<stage_name>@Copilot@StageNavigationBegin`
  （`ClickStageName` OCR 点关卡名 + `FullStageNavigation` 左右滑），`is_raid` 时再跑
  `RaidConfirm` / `ChangeToRaidDifficulty`；列表模式 `set_wait_until_end(true)`，
  停止任务 `Copilot@ClickCornerUntilEndOfAction`，**非三星即中止**。
  它**不负责从主界面进活动地图**（MAA GUI 的作业列表也要求先进关卡列表界面）。
  → 是否能接在现有 Fight 导航之后用，是 **spike A** 要验的。
- 单文件模式（现状）：`m_battle_task_ptr->set_stage_name()` 只校验 Tile-Pos 有该关，**不导航**，
  必须停在关卡详情界面。项目现有的 `_navigate_to_stage()`（临时 `MAA_CONFIG_DIR` 下
  `Fight(times=1)` + `FightBegin` 改成只识别不点击 + `Custom(CopilotNavigationReady)`）
  09-08 15:17 真机通过（`runs/2026-09-08-copilot-9e14e500ac25.log`）。
- **`Copilot Completed` 标记在真实日志里存在**（同一份日志第 47 行），
  `PLAN_CLEARING` 不变量 #33「未用真实日志核对」这条可以关掉。
- `maa activity` 读的是 **`~/Library/Caches/com.loong.maa/StageActivityV2.json`**
  （带 etag，`maa activity` 时按 etag 拉取），不是 `MaaResource` 目录（见 E）。
- 其它未用的上游能力（`maa <cmd> --help` 与 https://docs.maa.plus/zh-cn/protocol/integration.html）：
  Fight `drops`（`-D<itemId>=<n>` 刷到 N 个即停）、`report_to_penguin` / `report_to_yituliu`、`DrGrandet`；
  Copilot `support_unit_usage` / `support_unit_name` / `add_trust` / `formation_index` / `user_additional`；
  Award `orundum` / `mining` / `specialaccess`；Mall `buy_first` / `blacklist` / `reserve_max_credit` /
  `credit_fight`；Infrast `continue_training` / 线索交换三项；`OperBox`（干员识别）；
  `roguelike` / `reclamation` / `ssscopilot` / `paradoxcopilot`；任务文件 `condition`
  （Weekday / Time / DateTime / DayMod / OnSideStory / And / Or / Not）。

### C. 上游资源（实测本机文件）

- `stages.json` 616 关全带 `dropInfos`（`NORMAL_DROP` / `EXTRA_DROP` / `FURNITURE`），**无掉率、无开放时间**。
  当期 `SR-1..SR-8` 都在，`apCost`：SR-5 = 12、SR-8 = 21；**没有 `SR-EX-*`**（本期没有 EX）。
  资源本（`CE-6` / `AP-5`）只有 `FURNITURE`，龙门币 / 红票不以掉落建模，只能靠手工表。
- `item_index.json` 474 条，`itemId → 中文名`。
- `resource/tasks/Stages/SR.json` 顶层键：`SR-6, SR-7, SR-8, SR-5`（与 `maa activity` 推荐关一致）；
  `Supplies.json` 有 `CE-5/6, LS-5/6, CA-5, AP-5, SK-5, PR-A/B/C/D-1/2`。
- 本地 `stage_index.json`（Tile-Pos）codes 里 SR 有 `SR-1..SR-8` 和 `SR-TR-1`，共 2132 个 code。
- `maa activity Official` 输出自带每关主掉落文本（`SR-8: 异铁组`、`SR-5: 搓玉效率待定`）。

### D. 外部掉率数据（实测 curl，2026-09-08 23:1x）

| 源 | URL | 形态 | 结果 |
|---|---|---|---|
| 一图流 | `https://cos.yituliu.cn/arknights/stage-drop/matrix.json` | 静态 JSON，`{"matrix":[{stageId,itemId,times,quantity,stdDev,start,end}]}` | 200，1.4 MB，10975 行；当期 `act54side_*` 29 行，`end: null` 表示进行中；`Cache-Control: max-age=7200` |
| 企鹅物流 | `https://penguin-stats.io/PenguinStats/api/v2/result/matrix?server=CN&show_closed_zones=false` | 同构 | 200，1.0 MB，8085 行 |
| 一图流物品价值 | `POST https://backend.yituliu.cn/item/v7/value`（body `{}`） | JSON，129 条 | 200（`cos.yituliu.cn/arknights/item/value.json` 是 404，那是猜的路径；详见 spike B） |
| 一图流排期 | `GET https://backend.yituliu.cn/stage/info` | JSON，1550 关 | 200，含 `stageType ∈ {MAIN, ACT, ACT_REP, ACT_PERM, ACT_MINI}` 与 `start/end`（详见 spike B） |

`stageId` / `itemId` 与 MAA 的 `stages.json.stageId` / `item_index.json` 键**直接 join**，不用映射。
掉率 = `quantity / times`；每个材料期望理智 = `apCost / 掉率`。样本量要过滤（一图流默认 300）。
一图流许可 **CC BY-NC 4.0**（旧会话核对）→ 运行时抓取、不 vendor、README 致谢。

### E. 活动时间数据（`StageActivityV2.json`，实测）

```jsonc
"Official": {
  "sideStoryStage": {
    "SR": { "MinimumRequired": "v6.17.0",
            "Activity": { "Tip": "SideStory「月行水上」", "StageName": "月行水上",
                          "UtcStartTime": "2026/09/04 12:00:00", "UtcExpireTime": "2026/09/18 03:59:59", "TimeZone": 8 },
            "Stages": [ {"Display":"SR-8","Value":"SR-8","Drop":"30043"}, … {"Display":"SR-5","Drop":"搓玉效率待定"} ] },
    "SSReopen": { … "Stages": [ {"Display":"SSReopen-AT","Value":"SSReopen-AT","Drop":"代理1~8关"}, {"Display":"AT-8","Drop":"31093"}, … ] }
  },
  "resourceCollection": { "Tip": "资源收集", "UtcStartTime": …, "UtcExpireTime": …, "IsResourceCollection": true },
  "miniGame": [ … ]
}
```

- 有**活动级**开始 / 结束时间（字段名带 `Utc` 但实际是 `TimeZone` 本地时间，maa-cli 源码注释亦如此）、
  推荐关的 `Drop` 物品 ID、复刻活动的 `SSReopen-<前缀>` 代理入口。
- **没有 EX / S 单关开放时间**。D9 的判定只能组合：活动 `is_active` + 本地索引与导航键里出现该关 +
  森空岛对应 zone `totalStage > 0`。
- `resourceCollection` 有时间段，可替代现在「公告里出现『资源收集』字样」的判断。

### F. prts.plus 作业

- 当期 SR-8 缓存 33 份作业，`content.difficulty` **全为空**，不能用它判「支持突袭」。
  D7 因此定为「活动数字关一律 both，同一份作业先普通后突袭」。
- `ratio == 1.0 且 gaps == [] 且 soft_gaps == []` 的筛选与 Top-1 逻辑在 `maa_copilot_worker.cmd_search()` 已有。

---

## 全局护栏

沿用 `archive/PLAN_CLEARING.md`「全局护栏」11 条（仓库 ≠ 运行时、根目录跑单测、不变量表一条不破、
不许新增静默 return、按钮 < 20s、下拉值查表、stone / medicine 永不松动、每步各自提交、
开工先对现场、不符就停下回报、区分实测与推断）。本文额外：

12. **每步结束都要同步三份文档**：`README.md`（功能 / 使用 / 配置）、`docs/DISCORD_GUIDE.md`
    （频道里会发生什么、能点什么、能说什么）、`docs/DEV_GUIDE.md`（链路 + 不变量 §8 + 排障 §10）。
    删掉的功能在文档里**删干净**，不留「已废弃」段落。
13. **删代码要连测试、SKILL.md 意图映射、`maa_action.dispatch()` 白名单一起删**。
14. 森空岛 / 鹰角 token 不经 Discord / argv / agent 上下文（不变量 #16）。spike 与执行 agent 都不得读 `hg_token.txt`。

---

## 步骤 0 — 收尾

### 前置

无。

### 目标

把 09-08 已离线验证的改版落到线上，让后面每一步都从干净的 HEAD 出发；顺手把 07:50 上游更新
连续失败查清（它不阻塞四期，但每天一条红卡会掩盖真正的问题）。

### 做法

1. 工作区改动（`git status`：README / DEV_GUIDE / PLAN_CLEARING / maa_action / maa_plan_card / test_maa /
   docs 目录搬迁 / 新增 DISCORD_GUIDE）**按现状提交**，不要顺手改设计——本步只做落地。
   提交前跑单测，提交后 `python3 deploy.py` + `--check`。
2. 07:50 失败：看 `runs/` 无关；看 `last_run.resource_update_detail`（SecureTransport 连接被关）。
   在本机终端**带代理**手跑一次 `maa update --batch` / `maa hot-update --batch`（`maa_subprocess_env(keep_proxy=True)`
   的同等环境），区分是 GitHub 连接问题还是 libgit2 后端问题（`cli.toml: backend = "libgit2"`）。
   只记录结论进 DEV_GUIDE §10；修法不在本步范围，除非是一行配置。
3. `PLAN_CLEARING.md` 不变量 #33 的「未用真实日志核对」批注按地基事实 B 关掉。

### 允许改动的文件

已在工作区的那几个；`docs/DEV_GUIDE.md`；`docs/archive/PLAN_CLEARING.md`（当时还在 `docs/` 下；只改批注）。

### 禁止触碰

任何新功能；`state/config.json` 的键；cron。

### 验收边界

```bash
python3 -m unittest discover -s tests -v       # 全绿
python3 deploy.py && python3 deploy.py --check # {"ok": true, "missing": [], "mismatched": []}
git status --short                              # 空
```

真机：次日 07:50 计划卡是**两个**下拉（刷图 + 抄作业），卡片上有「📦 材料缺口推荐（降级）」字段，
且 `plan.json` 出现 `stage_source`（降级期不会自动改关，所以 `stage` 仍是默认）。

---

## 步骤 1 — 场次级计划 + 两张卡 + 单下拉 + 抄作业卡分离 + 探测统一

### 前置

步骤 0。

### 目标

D1 ~ D4、D6（卡片部分）、D18。做完之后：每天 07:50 / 21:50 各一张卡，每张只有一个下拉和一个
「跳过本场」按钮；手动选择只影响本场；「抄作业」是聊天触发的独立卡；所有自动行为开模拟器前先探进程。
**本步不改执行链**（抄作业选中后仍走现有搜索 → 候选卡流程，步骤 2 再换）。

### 做法

**1a. 场次级计划（`maa_common.py` / `maa_action.py`）**

- `plan.json` 改成 `{date, sessions: {am: {...}, pm: {...}}, updated_at}`，每个场次
  `{stage, stage_source, skip, source, updated_at}`。旧格式（顶层 `stage/skip`）读到就当空，
  不做迁移（一天后自然失效）。
- `effective_plan(at, phase)`：`phase ∈ {am, pm, now}`；`now` **只叠加 `sessions.now`**（本步不写它，
  所以等于默认；D4）。`_today_override()` 的字段白名单改成按场次；`stage_source` 必须留在白名单里
  （`PLAN_CLEARING` 不变量 #31 的教训）。
- `plan set-stage <关卡> <场次>`、`plan skip-today on|off <场次>`：场次参数只接受 `am` / `pm`，
  由登记表的 `select_args` / `args` 写死，不从 Discord 直通（不变量 #8）。
- `plan show` 输出两场各自的关、来源（自动 / 手动）、是否跳过。
- `maa_daily.execute(phase)` 用 `effective_plan(started, phase)`；`should_skip()` 语义不变
  （`now` 永远照跑，不变量 #5d）。

**1b. 两张卡（`maa_plan_card.py`）**

- `main(phase)`：场次同日常一样用 **workdir** 传（`~/.hermes/state/maa/am` / `pm` 已存在）。
  07:50 = am 卡：跑 `check_and_update_upstream()` → `refresh_activity_stages()` → 算默认关 → 发卡。
  21:50 = pm 卡：**跳过** `check_and_update_upstream()`（D1），其余相同。
- 默认关：本步沿用工作区的 `farming_recommendations()` + `auto_stage()` + `apply_auto_stage()`，
  只是写到**本场次**的 `sessions.<phase>`；步骤 3 再换公式。
- 卡片控件：一行 `stage-select` 下拉 + 一行一个按钮「⏭️ 跳过本场（早场 08:00）」/「▶️ 恢复本场」。
  删 `📋 最近结果`、`🤖 请 Hermes 解释`、`_copilot_stage_select()`、`_farming_stage_select()`（后者工作区已删）。
- 下拉选项与文案（每项都标材料，25 项上限）：`1-7 · 固源岩` / 当天开放资源本（`SUPPLY_STAGES` 里已有中文说明，
  `CE-6 · 龙门币` 这种）/ 活动关（`stages.json` 主掉落，工作区已实现）。「上次作战关卡」保留在第一位。
- embed 文案：标题带场次（`🌱 MAA 今日计划 · 早场 08:00`），字段「刷什么」「为什么」（缺口理由或降级说明）、
  「本场跳过」「当前活动关」「活动截止」（来自 `StageActivityV2` 的 `UtcExpireTime`，地基事实 E）、
  am 卡多一个「上游更新」。
- `retire_old_plan_cards()` 按标题前缀 `🌱 MAA 今日计划` 匹配（两种场次都收）。
  按钮登记表仍整段清 `hermes:btn:maa:` 前缀（不变量 #7），21:50 清掉早场卡的控件是预期行为。
- 新 cron：`hermes cron create "50 13 * * *" --name "MAA 计划卡·晚" --script maa_plan_card.py --no-agent --deliver local --workdir ~/.hermes/state/maa/pm`；
  现有 `03d5e98ce8d4` 改成带 `--workdir …/am`（或删了重建）。README / DEV_GUIDE §7 的 cron 段同步。

**1c. 抄作业卡（`maa_action.py` + `maa_plan_card.py` 或新文件 `maa_copilot_card.py`）**

- 新子命令 `copilot card`：聊天说「抄作业 / 有什么关能抄」→ SKILL.md 映射到它。< 20s：只读本地
  `activity_stages`、`StageActivityV2` 缓存、`Stages/<前缀>.json` 键、本地记账（步骤 2 才有，本步显示「未记录」）。
- 卡片内容：embed 列**当前开放关一览**（`maa activity` 快照的每一关 + 主掉落 + 是否上游有导航 +
  活动截止时间），下拉只列 `activity_stages ∩ 导航键`（本步先这样；步骤 2 扩成 Tile-Pos 全集 ∩ 导航键）。
  选中 → 登记表 `select_args` → `copilot find <关卡>`。
- 按钮前缀用 `hermes:btn:maa-copilot:card:`（在 `maa_copilot.BUTTON_PREFIX` 之下），
  发新卡时把上一张的组件 PATCH 空并删登记；不进 07:50 的 `hermes:btn:maa:` 清扫。
- 无开放活动时也发卡：一览为空、下拉退化成资源本 / 主线里上游有导航的关（`Supplies.json` 键 + `EpisodeN`）。

**1d. 模拟器探测统一（D18）**

- `emulator_is_online()` 与一个通用的「已在线 → 汇报」函数搬到 `maa_common.py`（或留在 `maa_daily.py`，
  worker 已经从它 import `open_emulator` 等，随你；但**探测预算 5s、永不抛异常、失败按离线**三条不变）。
- 调用点：`maa_copilot_worker._run_locked()`（在 `open_emulator()` 前）、`_clearing_run_locked()`、
  `maa_depot.run_depot()`。在线时：
  - 抄作业：恢复 `done`（原卡可再点，本步）/ 置 `waiting` 让 watcher 稍后重试（步骤 2），发「⏸️ 模拟器正在使用，本次未执行」。
  - Depot：写 `depot_status = "skipped_busy"`、发灰色卡，**不改 `snapshot_at`**，明天 04:30 自然重试。
  - 队列：现有 `_clearing_notice` 路径。
- `now` 场与 `sanity-probe` 保持豁免。

### 允许改动的文件

`scripts/maa_common.py`、`scripts/maa_action.py`、`scripts/maa_plan_card.py`、`scripts/maa_daily.py`
（只改 `effective_plan` 调用与 skip 文案）、`scripts/maa_copilot_worker.py`（只加探测）、`scripts/maa_depot.py`
（只加探测）、`skills/games/maa-operator/SKILL.md`、`deploy.py`（cron 文案 / `--check` 不变）、
`tests/`、三份文档。

### 禁止触碰

- 执行链（`_run_locked()` 除探测外的任何一行、`render_daily_toml()` 的 Fight 块、`copilot pick` 语义）
- 不变量 #1/#2/#3/#8/#13/#16
- `farming_recommendations()` 的算法（步骤 3 的事）

### 验收边界

```bash
python3 -m unittest discover -s tests -v
python3 deploy.py && python3 deploy.py --check
hermes cron list | grep -A3 'MAA 计划卡'      # 两条，workdir 分别是 …/am 与 …/pm
```

必须有的单测：

- `effective_plan(at, "am")` 只叠 `sessions.am`，`"pm"` 只叠 `sessions.pm`，`"now"` 两者都不叠。
- 早场 `skip` 为真时 `execute("pm")` 照跑；`execute("now")` 永远照跑。
- am 卡的 `select_args` 全部带 `am`，pm 卡全部带 `pm`；值仍是 `choice:N`（不变量 #8 测试延用）。
- pm 卡的 `main()` 不调用 `check_and_update_upstream()`（用桩断言调用次数为 0）。
- 计划卡组件恰好两行：一个 type 3 下拉 + 一个按钮；登记表里没有 `explain` / `log` / `farming-select` / copilot 下拉的键。
- `copilot card` 的下拉只含导航键里有的关；无活动时退化成资源本 / 主线且不抛异常。
- 探测：`_run_locked()` 在 `emulator_is_online()` 返回 True 时**不调用** `open_emulator()`，且状态恢复 + 发通知；Depot 同理且 `snapshot_at` 不变。

真机：

- 07:50 与 21:50 各收到一张卡，早场卡选 `CE-6`、晚场卡不动 → 08:00 跑 CE-6、22:00 跑默认关（看两场的 `runs/*.json` 快照）。
- 早场点「跳过本场」→ 08:00 发「已跳过」，22:00 照跑。
- 说「抄作业」→ 收到一览卡；选一关 → 走现有搜索流程（本步终点）。
- 手动开着 BlueStacks 时说「抄 SR-8」并点候选 → 收到「模拟器正在使用」而不是被踢出游戏。

---

## 步骤 2 — 作业链统一

### 前置

步骤 1。spike A 已有结论（见「Spike 结论」）：**多关连抄走 B 基线**（每关一次 Fight 导航 + 单文件 copilot），
`copilot_list` 只作可选优化且两个前置条件未满足前不上生产；突袭切换要在本步真机验收里第一个验。

### 目标

D6（执行部分）、D7 ~ D16、D20。做完之后：活动上线 → watcher 自动排出「上游有导航的关」全集，
按理智分批静默执行（普通 + 突袭），跑完汇报；手动卡选关同样选中即跑；两者共用一条执行链；
练度不满足的关跳过并在卡上列出；已通关（zone 全清 / 本地记账）不再匹配；通关队列的「全通关」机制删除。

### 做法

**2a. 本地记账（新 `state/maa/copilot/ledger.json`，0600，`atomic_write_json`）**

```jsonc
{ "activity_key": "SideStory「月行水上」|SR",
  "stages": { "SR-5": { "normal": "2026-09-09T02:10:00+08:00", "raid": null,
                        "attempts": [ {"at": …, "mode": "raid", "op": 102169, "outcome": "failed", "detail": "…"} ],
                        "skipped_reason": "" } } }
```

- 一关记「通关」= 所需模式全部有 `Copilot Completed`（`maa_clearing.copilot_outcome()` 判据不变）。
  活动数字关所需模式 = `normal + raid`（D7）；资源本 / 主线 = `normal`。**突袭失败 → 该关不算通关**（D7）。
- `activity_key` 变化（新活动 / 复刻）→ 归档到 `ledger-<key>.json`，新开一份。
- 「已通关」判定（D10）= 本地记账所需模式齐全 **或** 森空岛该关所属 zone `clearedStage == totalStage`（zone 全清）。
  `zone_progress_warning()` 那条单向启发**不参与**，也不合并（`PLAN_CLEARING` 不变量 #29 的精神）。
- （09-09 补）**用户手打过的关**：`maa_action.py copilot mark-cleared <关卡> [normal|raid|all]` 写入
  `maa_copilot.mark_stage_cleared()`——只补空位、`attempts` 追加一条 `won`（非 `won` 会被 `attempts_today()`
  算成当天失败），活动关落 `ledger.json`、非活动关落 `ledger-standalone.json`；`copilot unmark <关卡>` 清掉
  `normal` / `raid` 时间戳但保留 `attempts`。硬编码种子 `SEED_CLEARED_NORMAL` 只救 SR 这一期，以后靠这条入口。

**2b. 目标集合与顺序（`maa_activity_watch.py`）**

- 目标 = 当期活动前缀在本地 `stage_index.codes` 里的全部关（`SR-1..8`、将来 `XX-EX-n` / `XX-S-n`）
  ∩ `Stages/<前缀>.json` 顶层键（`maa_clearing.nav_checker()`），减去已通关，减去本轮已标「练度不满足」且
  距上次搜索不足 `activity_search_retry_minutes` 的关。教学关（`-TR-`）排除。
- D9：EX / S 未出现在索引或导航键 → 自然不在集合里，下次热更新后再进；森空岛 zone `totalStage == 0` → 该 zone 的关先不排。
- 顺序：普通数字关升序 → EX 升序 → S 升序；每关先 `normal` 后 `raid`。
- 每关一次 prts 搜索（`use_cache=False`，`limit=1000`，`ratio == 1.0 且无软硬缺口`，D12）；同一关同一份作业用于两种模式。
  搜到 → 进本轮执行清单；搜不到 → 记「练度不满足 / 暂无作业」，按现有 30 分钟退避重试，**不发确认卡**。
- 森空岛刷新频率：只在「集合里有未执行的关」的 tick 和「执行前复核」刷新；其它 tick 用缓存 + 6 分钟/点估算
  （`maa_copilot.estimated_sanity()`）。

**2c. 理智调度与静默执行（watcher + worker）**

- 预算：`maa_clearing.sanity_budget()`（先扣当天剿灭那份，地基事实 H 不变）；`plan_round()` 按顺序排出打得起的
  `(关, 模式)` 条目（条目形状沿用 `#raid` 后缀，`split_entry()` 不变）。
- 排不满时：用 `sanity_preflight()` 的 `ready_at` 算下一条目的可执行时刻，写 `activity_watch.scheduled_at`；
  watcher 每 tick 检查「`now ≥ scheduled_at` 且不在 `copilot_blackout_minutes` 内 且 `run.lock` 空闲 且模拟器离线」才派发。
- 时间预算：一场里每完成一关都检查距下一场日常（08:00 / 22:00）是否还够 `auto_copilot_min_lead_minutes`
  （新配置，默认 40）再开下一关；不够就收尾，剩下的留给下一轮。
- 派发时发一条「🤖 自动作业已排程」（关卡 + 模式 + 预计理智 + 预计时刻），只带一个「❌ 取消本轮」按钮
  （`hermes:btn:maa-copilot:<token>:cancel`，语义同现在）。执行开始发「▶️ 开始」，结束发战报（现有 embed 扩成多关表格）。
- 执行后日常让路（D11）：`maa_daily.execute()` 在渲染 `daily.toml` 前估算理智；`< 默认关 apCost` → `plan["farming"] = False`
  （开关已存在），开跑消息写明「理智不足以刷默认关，本场跳过刷理智」；剿灭 / 基建 / 公招 / 商店 / 领奖照常。
  `attach_clearing_yield()` 改名为「理智让路」，队列那层判断删除。

**2d. 一条执行链（`maa_copilot_worker.py`）**

- 统一入口 `run-batch <token>`：token 对应一份 `searches/<token>.json`，里面是本轮的
  `[{stage, stage_id, mode, candidate}]` 列表（手动卡只有一关、两种模式；自动是多关）。
  `cmd_run()` / `copilot pick` 删除；`copilot cancel` 保留（只能在 `queued/preparing` 取消）。
- 锁内流程：探测模拟器（1d）→ `open_emulator` → `wait_for_android` → `verify_game_installed` →
  `settle_android_runtime` → **循环**每个条目：复核活动与理智（现有三段复核）→ 导航 → `maa copilot <uri> --formation --batch -v --raid <mode>`
  （带 watchdog）→ `copilot_outcome()` → 记账 → 下一条。任一关失败：记账、**继续下一关**（不是停下），
  除非失败种类是导航 / 连接类（那说明环境坏了，停）。结束：截图 → `close_everything()` → 一条汇总战报。
- 突袭的导航：Fight 导航到的是普通难度详情页。走 B 路线时在导航后追加 `Custom(task_names=["ChangeToRaidDifficulty"])`
  （上游任务名，`resource/tasks/tasks.json` 里有；spike A 里 MaaCore 自己就是用 `RaidConfirm` / `ChangeToRaidDifficulty` 切的），
  再单文件 copilot。**本步真机验收第一项**：切换后截图 OCR 能看到突袭标识，且 `Copilot Completed`。验不过 → 自动路径先只做 `normal`，突袭留人工。
- 多关连抄：**B 基线**（每关一次 Fight 导航，不退模拟器，现有 `_clearing_run_locked()` 的循环形态），每关成败用 stdout 的
  `Copilot Completed` 判（spike A Q2：list 模式下 stdout 分不出逐关）。Fight 导航链自带 `<前缀>-TarotClose` 这类首通弹窗处理，
  copilot_list 的导航链没有（spike A 弹窗死循环）。**每关之间**要再做一次「活动 + 理智」复核和时间预算检查。
  `copilot_list` 优化的两个前置条件（读 asst.log 逐关记账、弹窗处理）见 Spike 结论，不在本步范围。
- 每关的 `maa copilot` 子进程超时按单关设（现有 900 s 够一关），整场总时长受 `auto_copilot_min_lead_minutes` 约束。
- **永不**传 `--ignore-requirements` / `--use-sanity-potion` / `--support-unit-*`（D12 / D13，不变量 #15 收紧为「三者都不出现」）。
  代码里 `ratio < 1.0` 的分支整个删掉。

**2e. 手动路径（D13 / D14）**

- 手动卡选关 → `copilot find <关卡>` → 搜索 worker：森空岛刷新失败 → **直接失败**发红卡（不再「沿用缓存并提示」）；
  无 `ratio == 1.0` 作业 → 失败发红卡（写清缺什么）；有 → 直接 `queued` + `run-batch`。模式：活动数字关 `both`，其它 `normal`。
- 手动卡的一览里显示自动进度：每关「✅ 已通（普通+突袭）/ 🔁 普通已通、突袭待补 / ⏳ 排程 HH:MM / 🚫 练度不满足（缺：…）/ ⛔ 上游无导航」。
- 「重打某关」= 手动选一个已通关的关，允许（记账 `attempts` 追加，不改 `normal/raid` 时间戳）。

**2f. 精简 `maa_clearing.py`（D15）**

保留：`split_entry / entry_label / annihilation_reserve / sanity_budget / plan_round / copilot_outcome /
record_stage_failure（去掉 needs_manual 转换）/ should_notify / clear_notification / nav_checker / read_nav_keys / stage_nav_prefix`。
删除：`needs_manual` 全套（`build_manual_card / manual_button_id / awaiting_manual / clearing manual-done`）、
`ex_gate / apply_ex_gate / ex_gate_embed`、`sanity_cost_from_ocr / ocr_sanity_cost / parse_ocr_lines / png_size /
cached_sanity_cost / remember_sanity_cost / resolve_cost / probe_candidate`、`queue_is_active / mark_blocked / blocked_today /
is_nav_frozen`、`clearing.json` 状态文件与 `clearing start/stop/run/status` 子命令、`cmd_clearing / _clearing_run_locked /
_clearing_probe_sanity / cmd_sanity_probe`、`config.clearing_*` 五个键、`hermes:btn:maa-clearing:` 前缀、
`maa_daily.dispatch_clearing_round / report_clearing_dispatch_failure`。
对应单测（`Clearing*Tests`、`SanityOcrTests`）、SKILL.md 意图映射、README / DEV_GUIDE / DISCORD_GUIDE 相关段落、
不变量 #26 ~ #30、#33 ~ #37 一并**改写或删除**（#33 的判据、#34 的 `#raid` 后缀、#37 的导航键闸门要**保留并改写落点**）。

### 允许改动的文件

`scripts/maa_activity_watch.py`、`scripts/maa_copilot_worker.py`、`scripts/maa_copilot.py`、`scripts/maa_clearing.py`、
`scripts/maa_action.py`、`scripts/maa_daily.py`（让路 + 派发点删除）、`scripts/maa_plan_card.py`（手动卡一览）、
`state/config.json`（删 `clearing_*`，加 `auto_copilot_min_lead_minutes`）、`deploy.py`、`skills/…/SKILL.md`、`tests/`、三份文档。

### 禁止触碰

- 不变量 #1/#2/#3/#8/#13/#16/#17/#19/#20/#21/#22/#23/#24/#25
- `maa_skland.py` 签名与认证链
- `_navigate_to_stage()` 的隔离配置做法（不变量 #37 的「只读上游目录」）
- `render_daily_toml()` 的任务链顺序与硬编码字面量

### 验收边界

```bash
python3 -m unittest discover -s tests -v
grep -rn "ignore-requirements\|use-sanity-potion\|support-unit" scripts/   # 期望：无输出
grep -rn "clearing_enabled\|needs_manual\|ex_gate" scripts/ state/ skills/ docs/   # 期望：无输出
```

必须有的单测：

- 目标集合：索引里有、导航键没有的关不进集合；教学关不进；本地记账齐全的关不进；zone 全清的关不进；
  zone `totalStage == 0` 的关不进但**不标失败**。
- 顺序：`SR-5#normal, SR-5#raid, SR-6#normal, …`；资源本条目只有 `normal`。
- 记账：`normal` 成功 + `raid` 失败 → 该关**不算通关**，下轮仍在集合里且只剩 `raid` 条目。
- 调度：预算排不满 → `scheduled_at` = 下一条目的 `ready_at`；blackout / 模拟器在线 / 锁被占 → 不派发且原因可见。
- 时间预算：距日常不足 `auto_copilot_min_lead_minutes` 时不开新关。
- 让路：估算理智 < 默认关 `apCost` → 渲染出的 `daily.toml` 无「刷理智」有「每日剿灭」；估算失败（森空岛不可用）→ **照常刷**（宁可多刷，不变量 #26 的精神）。
- 手动：森空岛刷新失败 → `searches/<token>.json.status == failed`，不派发 worker；`ratio < 1` 的作业不入候选。
- 一关失败继续下一关；导航 / 连接类失败停止；两种情况战报都列出每关结果。
- 不变量 #37 的导航键闸门测试保留（改到新的目标集合函数上）。

真机（**需要一个有 ≥2 关未通的活动**，当期 SR 若已通完就等下一个活动）：

- 活动上线 → 无人操作 → 收到「已排程」→ 到点自动开跑 → 战报列出 ≥2 关（含突袭）→ `ledger.json` 记账正确。
- 故意把理智耗到不足 → 「已排程」写出预计时刻 → 到点执行。
- 执行后紧接着的日常：开跑消息写「跳过刷理智」，剿灭 / 基建正常。
- 手动卡选一个已通关的关 → 照跑（重打）；选一个练度不满足的关 → 红卡「缺：…」不开模拟器。

**失败即停**：突袭切换在真机上任何一条路都不通 → 停下回报，自动路径先只做 `normal`，突袭留人工。

---

## 步骤 3 — 素材权重公式 + 活动期间 all-in + Fight drops 上限

### 前置

步骤 1（默认关写进场次）。**spike B 的结论**（活动日历 / 物品价值接口）。Depot 至少 2 次对表
（09-09 04:30 之后才有第一个消耗区间；之前只能验冷启动路径）。

### 目标

D5、D17。「今天刷什么」由三项加权分决定，数据来自一图流（企鹅物流后备）+ Depot 消耗率 + 活动日历；
活动期间候选集只有活动关；刷够缺口数量就停（Fight `drops`）。

### 做法

**3a. 新模块 `scripts/maa_farming.py`（纯函数 + 一个抓取层）**

- 抓取（四个来源，全部落 `state/maa/farming/` 缓存，0600；失败**不抛**，D-2 教训，降级路径要在卡片上可见）：
  - `matrix.json`（一图流镜像，`ETag` 条件请求；失败退企鹅 `show_closed_zones=true`）；
  - `GET backend.yituliu.cn/stage/info`（排期 / 复刻标记 / `apCost` / `spm`；1h TTL；失败退企鹅 `/zones` + `/stages`）；
  - `POST backend.yituliu.cn/item/v7/value`（body `{}`；24h TTL；`itemValue == null` 的条目跳过）；
  - `GET backend.yituliu.cn/item/v5/store/activity`（当期活动商店，用于「代币能换什么」提示；可选）。
  统一 UA `maa-remote/<版本> (+仓库地址)`；单测里网络层全部桩掉（DEV_GUIDE §9 断网复核）。
- 掉率：`rate(stage_id, item_id) = quantity / times`，`times < farming_min_samples`（新配置，默认 300）视为无数据；
  `ap_per_item = apCost / rate`；`start / end` 里 `end == null` 或 `end > now` 才算当前有效（活动关数据只在开放期有效）。
- 候选关集合（D5）：`StageActivityV2` 里有活动 `is_active`（或 `activity_stages` 非空）→ **只有活动关**
  （`activity_stages ∩ 导航键`，与步骤 2 同一集合）；否则 `1-7` + 当天开放资源本（`SUPPLY_STAGES` /
  `resourceCollection` 时间段内全开）。手工表 `farming_stage_table` 继续作为资源本材料的唯一来源。
- 三项打分（每项归一到 0..1，权重在 `config.farming_weights`，默认 `{"scarcity": 0.5, "gap": 0.3, "efficiency": 0.2}`，
  可调）：
  1. **稀缺 / 复刻时间预估**（数据：`stage/info` + `matrix.json`）：
     - 对材料 m，算「当前开放关里最便宜的理智/件」`best_now(m)` 与「常驻关（`MAIN` / `ACT_PERM` / 周常）里最便宜」`best_perm(m)`；
       `best_perm / best_now ≥ farming_activity_edge`（新配置，默认 1.3）才算「活动关明显更优」。
     - 「下次能这么便宜刷到的时间」= 当期活动 `end`（关卡关闭）之后，取该活动的**预计复刻日** =
       `首发 start + farming_rerun_days`（新配置，默认 378，取自 42 组 `ACT → ACT_REP` 的中位数；p25–p75 351–387）；
       已是 `ACT_REP` 的活动视为「不会再来」（复刻后通常常驻化或不再复刻，按 `end + 2 年` 处理）。
     - 分值 = `clamp((下次可刷日 − now) / farming_scarcity_horizon_days, 0, 1)`（新配置，默认 400）× [活动关明显更优]；
       常驻可刷、或活动关不比常驻便宜的材料分 0。`stage/info` 不可用时退化为「当期活动关独有主掉落 = 1，其余 0」。
     - 「平均用量」这半边就是第 2 项的日均消耗，不重复计分；本项只管「窗口要关了」这个时间维度。
  2. **缺口**（数据：Depot + `item/v7/value`）：缺口件数 = `farming_target_days` 天用量 − 当前库存估算（用 4 周日均消耗）；
     缺口价值 = 缺口件数 × `itemValue`（把不同稀有度拉到同一理智量纲：1 个晶体电子单元 ≈ 302 理智 ≈ 17.6 个固源岩组）；
     分值 = 缺口价值 / 本场可用理智 归一并 clamp 到 1。零消耗材料退回手工权重（`PLAN_CLEARING` 不变量 #32）；
     冷启动（无消耗区间）整项用手工权重；`itemValue == null` 的材料不参与。
  3. **效率**（数据：`matrix.json` + `stage/info.apCost`）：候选集内该材料最优关的「件/理智」÷ 全游戏该材料历史最优「件/理智」→ 0..1。
     样本 `times < farming_min_samples`（默认 300）不计；`end` 已过期的活动关行不计。
     可选把一图流的综合关卡效率（`E / apCost`，含龙门币项）作为并列指标显示在卡片上，不参与打分。
- 输出：`[{material, stage, score, parts: {scarcity, gap, efficiency}, needed_qty}]` 降序；第一名写进本场次
  `sessions.<phase>.stage`（`stage_source = "auto"`），卡片「为什么」字段列前三名的三项分。

**3b. Fight `drops` 上限（`maa_daily.render_daily_toml()`）**

- 「刷理智」Fight 块加 `drops = { "<itemId>" = <needed_qty> }`（MaaCore 参数，达到即停）；
  `needed_qty` = 缺口目标 − 当前库存估算（目标 = `farming_target_days` 天用量，新配置，默认 30；冷启动不设上限）。
- 达到上限后剩余理智不浪费：渲染**第二个** Fight 块（第二名的关，无 `drops`，打到理智不足）。
  两个块都保持 `medicine = 0 / stone = 0 / series = 0` 字面量（不变量 #1 ~ #3）。
- 战报解析：`TASK_STEPS` 增加第二个刷理智块的显示名；`_drop_pairs()` 合并两块的掉落。

**3c. 与 Depot 的衔接**

- `record_drops()` 不变（战报回写库存估算）。
- Depot 的 7 天间隔不动；卡片上「消耗数据来自最近 N 次对表、窗口 4 周」照旧标明。

### 允许改动的文件

新增 `scripts/maa_farming.py`；`scripts/maa_plan_card.py`（默认关调用）、`scripts/maa_daily.py`（Fight 块 + 战报）、
`scripts/maa_depot.py`（只读接口）、`state/config.json`（`farming_weights / farming_min_samples / farming_target_days /
farming_activity_edge / farming_rerun_days / farming_scarcity_horizon_days`）、`deploy.py`、`tests/`、
三份文档（README「第三方组件与致谢」加一图流 / 企鹅物流，CC BY-NC 4.0，卡片底部带「数据来源」一行）。

### 禁止触碰

- 不变量 #1/#2/#3/#22/#23
- 数据不 vendor 进仓库（许可）
- `farming_stage_table` 语义（手工表优先于派生表，不变量 #38）

### 验收边界

```bash
python3 -m unittest discover -s tests -v
python3 -c "import socket; ..."      # DEV_GUIDE §9 的断网复核，单测不许碰一图流 / 企鹅
```

必须有的单测（网络层全部桩掉）：

- 活动期间候选集只含活动关；活动结束当天（`UtcExpireTime` 已过）退回资源本 + 1-7。
- 样本 < 300 的掉率不参与；`end` 已过期的活动关数据不参与。
- 三项分的权重可配且和为任意值时归一；改权重后排序变化符合预期（各给一个反例）。
- 冷启动：无消耗区间 → 缺口项退手工权重，卡片标「降级」，**仍会写默认关**（这里与 `PLAN_CLEARING` 不变量 #31 的
  「降级期不自动改关」不同：现在候选集受活动限制、且有掉率数据兜底，降级期也能选出合理的关；改写该不变量并说明理由）。
- `daily.toml` 含 `drops` 且值 = 缺口；缺口 ≤ 0 时不写 `drops`；第二个 Fight 块存在且无 `drops`。
- 抓取全部失败 → 用缓存；缓存也没有 → 退回「不推荐、沿用默认」，不抛异常。

真机：

- 09-09 04:30 Depot 对表后（第一个消耗区间），次日 07:50 卡片「为什么」字段出现三项分；
- 一场日常里第一个 Fight 达到 `drops` 上限后进入第二个 Fight（看 `runs/*.log` 里两个 Fight 段）。

---

## 步骤 4 — 肉鸽

### 前置

步骤 1（探测统一、场次级 skip）。**spike C 的结论**（参数表、优雅停止方案、输出标记）。

### 目标

D19。聊天说「刷肉鸽」→ 一张配置卡（主题等下拉 + 开始按钮）→ worker 持 `run.lock` 循环调 `maa roguelike`；
说「停」→ 本局结束后停，模拟器关干净；过程与结果由上游输出，项目只转成中文汇总。

### 做法

- 新子命令 `roguelike card / start <token> / stop / abort / status`；新状态 `state/maa/roguelike.json`
  （`{status: idle|running|stopping, theme, params, started_at, runs: [{n, started_at, ended_at, outcome, invest, exp}],
  stop_requested_at, abort_requested_at, last_theme, dirty}`）。
- **执行路线 = `maa run` 任务文件，不用 `maa roguelike` 子命令**（spike C：CLI 缺 BlackFlow、mode 限 0..5、两处默认值反常、Phantom 丢难度）。
  照 `_navigation_config()` 的做法开临时 `MAA_CONFIG_DIR`，写 `tasks/roguelike.toml`：`type = "Roguelike"`，`[tasks.params]` 里
  `theme / mode / squad / roles / difficulty / starts_count = 1 / investment_enabled = true / stop_when_investment_full = true`；
  `core_char` 留空（上游按练度选）；不传 `use_support`。
- 卡片（4 个下拉 + 1 行按钮，正好 5 行）：
  1. 主题（6 项：傀影 / 水月 / 萨米 / 萨卡兹 / 界园 / 黑流树海），默认萨卡兹；
  2. 模式（刷源石锭 1 / 刷等级 0 / 烧水凹开局 4；萨米追加坍缩范式 5；其余高级模式不上卡），默认 1。
     **黑流树海例外**：MaaCore v6.17.0 起该主题只有三个策略，下拉直接映射 `blackflow_strategy`：
     「刷等级，快速飞三层」= `burn`、「刷源石锭，投资完成后自动退出」= `investment`、「刷襁褓动物」= `baby_animal`
     （对应 `mode = 30001`，`blackflow_cultivation_target` 默认 `swaddled_cat`）；maa-cli 0.7.5 的 `maa roguelike` 没有这个主题，
     任务文件路线才传得进去（本机 MaaCore v6.17.2 已支持，v6.17.1 修过一批识别问题）；
  3. 难度（不指定 / MAX / n..0，各主题上限 15 或 18，≤ 21 项），默认取手册推荐（萨卡兹 4、界园 3、水月 3、萨米 4、傀影 2、黑流树海不指定）；
  4. 分队（按主题 + 模式从 MAA GUI 字典生成，≤ 19 项），默认：萨卡兹刷锭 → 点刺成锭分队，其余 → 指挥分队；
  按钮：「▶️ 开始」「⏹️ 本局结束后停止」「⏏️ 立即中止（会丢当前局）」。职业组固定 `稳扎稳打`（傀影 `取长补短`）。
  下拉值仍是不透明 `choice:N`，四个下拉的选择先存进 `roguelike.json.pending`，「开始」按钮才派发（不变量 #8）。
  卡片按钮前缀 `hermes:btn:maa-roguelike:`，发新卡时清旧。卡片写明「换主题前先在游戏里把该主题置顶到终端」。
- worker `roguelike <token>`：探测模拟器（1d）→ 抢 `run.lock` → 开模拟器（同 `_clearing_run_locked` 的开机链）→
  `while` 未收到停止 / 中止 且未到 deadline：**每局一个子进程**（`Popen` + `terminate()` + `wait(timeout)` + 最后才 `kill()`，
  **不许** `subprocess.run(timeout=)`；单局超时 `roguelike_run_timeout`，新配置，默认 3600）→ 解析输出（`Start exploration` / `Gain N exp` /
  `Exploration Abandoned` / `Deposit a / b / c` / `TraderInvestSystemFull` / `Roguelike Completed|Stopped|Error`）→ 每局发一条进度 →
  储备满或上游 `Roguelike Error` 连续 2 次就收尾 → `close_everything()` → 汇总战报（把 maa-cli 结尾 `Explorations: … Summary:` 段转成 embed）。
- deadline：距下一场日常不足 `copilot_blackout_minutes` 时不开新局（日常优先），也避开 Depot 04:20–05:00 窗口；战报注明「因日常临近停止」。
- 停止：`roguelike stop` 只写 `stop_requested_at`（< 20s），回「本局结束后停止」；`roguelike abort` 写 `abort_requested_at`，worker 对当前子进程
  `terminate()`，战报注明当前局作废。**中断后 `dirty = true` 且记 `last_theme`**：下次开始若主题不同，先拒绝并提示「上次 X 主题未干净结束，
  先用同主题跑一局收尾」（上游只在同主题下善后残局）。
- 肉鸽**不消耗理智**（spike C 已核实），不参与理智预算与让路。
- SKILL.md 意图映射：「刷肉鸽」「肉鸽停」「肉鸽立刻停」「肉鸽到哪了」。
- 真机首跑要补的数据：一局耗时（本机没跑过）、`starts_count = 1` 收尾画面能否无缝接下一局、BlackFlow 是否真能跑。

### 允许改动的文件

新增 `scripts/maa_roguelike.py`（领域层）；`scripts/maa_copilot_worker.py`（或新建 `maa_roguelike_worker.py`，
二选一，不要两边都有执行逻辑）；`scripts/maa_action.py`；`deploy.py`；`skills/…/SKILL.md`；`tests/`；三份文档。

### 禁止触碰

- `run.lock` 语义（不变量 #13）
- 日常 / 抄作业执行链

### 验收边界

必须有的单测：`stop` 后 worker 不再派发下一局；`abort` 对子进程 `terminate()` 且标 `dirty`；`dirty` 时换主题被拒；blackout / Depot 窗口临近
自动收尾；模拟器在线不启动；参数只从登记表 `select_args` 来（不变量 #8）；任务文件渲染出的 params 含 `starts_count = 1`、不含 `use_support`；
输出解析对 spike C 列的每个标记各一个用例；`Roguelike Completed` + 退出码 0 才算一局干净结束。
真机：跑 2 局后说「停」→ 第 2 局打完才退出 → 模拟器关闭 → 战报 2 局；再试一次「立刻停」→ 下次同主题开始能自动善后残局。

---

## 步骤 5 — 上游增量（各自独立，任意顺序）

| 项 | 做法 | 价值 |
|---|---|---|
| 上报掉落 | 日常 Fight 块加 `report_to_yituliu = true`（可选 `report_to_penguin`），ID 留空匿名 | 回馈步骤 3 的数据源 |
| Award 开关 | `orundum / mining / specialaccess` 进 `config.json` 布尔键，默认按用户习惯 | 少点几下 |
| OperBox 后备 | 森空岛 401 时，Depot 那趟顺手跑 `OperBox` 拿练度快照，`roster.json` 标 `source = operbox` | 练度不再单点依赖森空岛 |
| Mall 细化 | `buy_first / blacklist / reserve_max_credit` 进配置 | 信用不浪费 |

每项单独提交、单独验收，不进本文进度以外的范围。

---

## 不做的事

- 不自写导航定义、不为上游没导航的关补自动战斗（D8）。
- 不做全通关队列的 needs_manual / EX 闸门 / 编队 OCR（D15）。
- 不用助战、不 `--ignore-requirements`、不吃药、不碎石（D12 / D13、不变量 #1 / #2）。
- 不 vendor 一图流 / 企鹅物流数据。
- 不做 `PLAN_NAV_GATE.md` A-1 的导航闸门（导航键闸门已经覆盖它）。
- 不接生息演算 / 保全 / 悖论模拟（列入地基事实 B 备查，不立项）。

---

## Spike 结论（回填区）

### spike A — `copilot_list` 真机验证（2026-09-08 23:40–23:58，SR-5 + SR-6，实耗理智 33；全文见 `docs/spikes/copilot-list-2026-09-08.md`）

| 问 | 结论 | 证据 |
|---|---|---|
| Q1 Fight 导航停在关卡详情页后，`copilot_list` 能否自己点到另一关 | **✓** | 第 1 项 SR-5 走 `is_stage_detail_opened + confirm_stage_name` 快路径，881 ms 零点击接手；第 2 项 SR-6 在结算退回列表后 `find_stage` 自己点 `(1315, 618)`，2.4 s 内开打；两关都三星（`StageDrops-Stars-3` 0.998 / 0.979）。不必先回列表、不必关详情面板 |
| Q2 maa-cli stdout 能否区分逐关成败 | **✗** | `Copilot Start / Completed` 是任务级、整趟各一次；`CopilotListLoadTaskFileSuccess` maa-cli 0.7.5 **不打印**（`strings` 无此串），只在 MaaCore `debug/asst.log` 里（带 `file_name` + `stage_name`）。stdout 全文 27 行，逐关只有重复的 `BattleFormation / MissionStart`，无关名无胜负。`maa_clearing.copilot_outcome()` 在 list 模式下退化成整趟一个结论 |
| Q3 `is_raid=true` 能否同场切突袭 | **未验**（B 路线已于 09-09 补验，**失败**，见下一小节） | 第 3 项没走到，`RaidConfirm / ChangeToRaidDifficulty` 零次执行（被下面的弹窗卡死） |

**顺带挖出的坑（比 Q3 重要）**：SR-6 首通弹出全屏「获得了塔罗牌」弹窗，`MultiCopilotTaskPlugin` 的导航路径不处理任何弹窗：
`find_stage` 只 OCR 到塔罗牌文字，`Copilot@FullStageNavigation` 空滑，**死循环 9 轮 / 9 分钟不报错不退出**，直到 900 s 超时。
上游其实写了 `SR-TarotClose`（`text: ["获得了塔罗牌"]`），但只挂在 Fight 链的 `PreStartCheck` / `StartButton1` 上，copilot 链够不着。
生产上等于白占 `run.lock` 15 分钟、后面几关全丢。

**对设计的裁定（2026-09-09 用户拍板，写进步骤 2）**：
- 多关连抄**基线走 B**（每关一次 Fight 导航 + 单文件 copilot，不退模拟器）。理由：B 的每关成败能从 stdout 的 `Copilot Completed` 直接判；
  Fight 导航链自带 `SR-TarotClose` 这类弹窗处理；不需要解析 asst.log。
- **`copilot_list` 整体挂起，择日再验**（用户当时不在场，塔罗牌弹窗的现场没看到）。不给上游提 issue。
  将来重验的前置条件仍是两个：① 逐关记账改读 `asst.log`（`CopilotListLoadTaskFileSuccess` + `StageDrops-Stars-N`），② 弹窗有解。
- 突袭切换在两条路都没验过；步骤 2 真机验收第一项就是「B 路线：Fight 导航 → `Custom(ChangeToRaidDifficulty)` → 单文件 copilot」能否切到突袭。
  **→ 09-09 05:01 补验，B 路线失败**（`Custom Error`，详情页上没有突袭控件）；结论与下一步见下一小节。
- 记账种子：SR-5 / SR-6 用户此前已自己打过（spike 等于重打了一遍），SR-8 09-08 15:17 由抄作业通关；本地记账上线时把
  **SR-5 / SR-6 / SR-8 的普通模式**写入种子，突袭留空。
- 约束核对：全程 `medicine = 0 / stone = 0`、无 `use_sanity_potion`、无 `--ignore-requirements`；`close_everything → []`；仓库与线上未改；未发 Discord；
  唯一副作用是 `copilot/cache/act54side_0{5,6}.json` 搜索缓存。

### 突袭切换真机验证（2026-09-09）❌ —— spike A 的 Q3 补验，**失败，功能默认关闭**

**现场**（`runs/2026-09-09-copilot-5814e6deb762.{log,png}`，账本 `copilot/ledger.json` SR-5 `raid` 一次 `failed`）：

| 段 | 结果 |
|---|---|
| `[启动国服]` 05:00:58–05:01:29 | Completed |
| `[导航到关卡]` 05:01:29–05:01:51 | Completed（`Fight Completed`） |
| `[检查关卡界面]` 05:01:51–05:01:53 | Completed（`Custom Completed`） |
| `[切换突袭难度]` 05:01:55–05:02:08（13 s） | **Error**（`Custom Error` + `Error: Some error occurred during running task!`） |

**① 画面停在哪**：截图是 SideStory「月行水上」的**关卡选择 / 详情页，普通难度**——SR-5「月斜」标题带**三星**（普通已通）、
推荐等级「精英1 LV.30」、右下 `演习` / `开始行动 -12`、其上 `代理指挥` 复选框（未勾）、`敌方情报` / `地图` / `报酬`、
底部 SR-4…SR-7 关卡链、右上理智 86/207。导航本身没问题，卡在切换那一步。
**整屏没有任何「突袭」切换控件**。上游 ROI `[600,610,130,110]`（1280×720）折算到本机 1920×1080 是 `x 900–1095, y 915–1080`
（×1.5），裁出来看是**纯背景**（蓝色底图 + 那条横向路径线），最近的控件 `代理指挥`（x≈1565–1870）、
`演习/开始行动`（x≈1250–1900）都在它右侧几百像素外。所以 `ChangeToRaidDifficulty` 的模板永远匹配不到，
`maxTimes: 3` 三次点空 → `Custom Error`。

**② 更可能的正确做法**（按可信度排序，**未验证，不要据此直接改默认值**）：

1. **最可能的根因：这个活动压根没有突袭难度。** 突袭只在**复刻**活动里存在，「月行水上」是首次开放的 SideStory。
   佐证：上游 `tasks/Stages/SR.json` 的顶层键只有 `SR-5…SR-8` + `SR-Open*` / `SRChapterToSR`，**没有任何突袭变体**；
   `ChangeToRaidDifficulty` 的 `doc` 写的是 `["突袭", "15章险地"]`，`RaidDifficulty-Chapter15.png` 与
   `NormalDifficulty*.png`（放在 `template/StageNavigation/**Episode/**` 下）都是**主线章节**资产。
   若成立，则「D7 活动关一律普通 + 突袭」这条前提本身要改：得先判出「这个活动有没有突袭」再决定排不排。
2. **任务名列表照抄上游插件**：`task_names = ["RaidConfirm", "ChangeToRaidDifficulty"]`。
   MaaCore 的 `MultiCopilotTaskPlugin` 就是这么并列两个候选的——`RaidConfirm` 是 `DoNothing` + 模板
   `NormalDifficulty*.png`（"现在能切回普通" = **已经在突袭**），先判「已是突袭」再点切换。
   这修的是「重复执行 / 已切过」那一类，**修不了「屏幕上没这个按钮」**；但它与上游一致，值得作为启用时的默认值。
3. 面板遮挡 / 入口不在详情页：`ChangeToRaidDifficulty` 的 ROI 在**屏幕底部中央偏左**，那正好是主线章节页
   「标准 / 突袭」切换的位置。若复刻 SideStory 的入口在别处（例如活动主页而非关卡详情页），
   就得先退出详情面板再切、切完重新导航——这条要拿一个**真的有突袭的活动**才能验。

**③ 下次真机验证的清单**（用户在场时做；**先找一个确定有突袭的活动**，如复刻 SideStory 或主线 15 章）：

```bash
# 0. 前提：不在 blackout / Depot 窗口；模拟器没人用。开关先手动打开：
#    ~/.hermes/state/maa/config.json 里 copilot_raid_switch_task 改成 "ChangeToRaidDifficulty"
#    （或按上面第 2 条，先把 worker 的 RAID_SWITCH_TOML_TEMPLATE 改成两个任务名并列）
# 1. 排一关：手动搜作业 → 自动派 worker（modes 会是 normal + raid）
python3 ~/.hermes/scripts/maa_action.py copilot find <关卡>
# 2. 盯这一轮的日志，重点看 `# raid-switch <关卡> task=…` 之后那一段
tail -f ~/.hermes/state/maa/runs/$(date +%F)-copilot-*.log
# 3. 收尾截图：切换成功的话，关卡详情页应能看到突袭标识 / 理智消耗变高
open ~/.hermes/state/maa/runs/$(date +%F)-copilot-*.png
# 4. 记账复核：raid 必须真的拿到 Copilot Completed 才写时间戳
python3 -c "import json;print(json.load(open('$HOME/.hermes/state/maa/copilot/ledger.json'))['stages'])"
```

**看什么**：日志里 `[切换突袭难度] … Completed` + `Custom Completed`（不是 `Custom Error`）；
截图里详情页确实是突袭；`maa copilot … --raid raid` 那一段有 `Copilot Completed`；
账本 `stages.<关>.raid` 出现时间戳。四条缺一条就算没验过，把开关改回空串。

**已落地的语义修正（不需要再验）**：`copilot_raid_switch_task` 为空**不再是「跳过切换直接打」**，
而是**突袭功能整体关闭**——单文件 `maa copilot <uri> --raid raid` 根本不切难度（`is_raid` 只对
`copilot_list` 的条目有意义），跳过切换打完会把**普通难度**记成一次突袭通关。
现在：`required_modes()` 不再产生 `raid` 条目 → worker `_batch_run_locked()` 再兜一道判 `skipped` →
`switch_to_raid()` 直接拒绝。仓库种子已改成 `""`（D22：验不过就退回只打普通）。

### 手动通关重抄（2026-09-09 03:00–05:02）—— D10 的代价兑现了一次，已补写路径

用户手打通了 SR-5/6/7/8，SR 上线后第一轮照样全抄。账本 `copilot/ledger.json` 现场：

| 关 | 自动抄的时间 | 各自的原因 |
|---|---|---|
| SR-5 | 03:06、03:35、04:06 普通；05:02 突袭 `failed` | 种子没种上 + 30 分钟退避重排 + 突袭当时还开着 |
| SR-6 | 03:09、03:39、04:09 | 同上 |
| SR-7 | 03:42 | 不在 `SEED_CLEARED_NORMAL` 里，账本无记录 |
| SR-8 | 03:47 | 种子没种上 |

三个叠加的原因，前两个当天已修：

1. **手动通关没有渠道进账本**（结构性）。「已通关」只认本地记账与 zone 全清，森空岛 `act54side_zone1` 是 10/11 没全清，
   整个活动这条判据都不会生效。→ 补 `copilot mark-cleared` / `copilot unmark`（见 2a），SKILL.md 与 DISCORD_GUIDE 同步。
2. **种子没种上**：02:23「墟」复刻（`|AT`）先建了账，03:00 走「归档 + 新开」时 `ledger is not None`，种子一次没跑。→ `2434d42` 改按 `seeded` 标记补。
3. **30 分钟退避重排**：`unknown` 退避一到就重排。→ 同一 commit 加 `auto_copilot_max_attempts_per_day = 2`。
4. **突袭**：05:26 `6df489a` 把 `copilot_raid_switch_task` 默认清空。

### SR-8 三星但代理指挥未解锁（2026-09-09 08:02 早场）—— 抄完直接杀游戏，结算流程被掐断

**现场**：早场日常刷 SR-8，导航成功（08:02:39 OCR 认到 SR-8），`UsePrts.png` 点了 10 次
`UsePrtsSuccessCheck` 都没亮，08:03:03 `Fight Error`，一颗理智没花；MaaCore 接着把基建 / 公招 /
商店 / 奖励跑完。游戏里 SR-8 显示三星、「代理指挥」带锁。

**排除「借了助战」**（asst.log 两场 SR-8 抄作业，09-08 15:18 与 09-09 03:43）：`append_task Copilot`
参数 `formation: true`，无 `support_unit_usage`（maa-cli 默认 0 = 不用助战）；`BattleFormationSelected`
8 个位置全是自己的干员（黍填「奶盾」组）；日志里的 `BattleQuickFormationRole-Support` 是快速编队的
职业筛选「辅助」页签，真借助战走的是 `BattleSupportUnitFormation`，两场都没出现。

**根因（推断，待真机验）**：两场都是 `Copilot Completed` 后 **5 秒** `CloseDown/StopGameTask`，
收尾截图（`runs/2026-09-08-copilot-9e14e500ac25.png`、`2026-09-09-copilot-fda56c1811ba.png`）
都停在战后剧情页（右上「自动 OFF / 跳过 ▶」）。三星在战斗结束时就记上了，代理记录要等结算流程走完。

**修法**（同日）：① worker 每关打完先 `settle_after_battle()`（跳过剧情 + 返回主界面）再退游戏，
见 DEV_GUIDE §5.5 关键事实 11；② 日常 runner 主关 Fight 空跑失败时改刷 `farming_fallback_stage`
（`fight_failed_dry()` / `_fallback_fight_run()`），见 DEV_GUIDE §11.13。

**真机验证 ✅（09-09 08:59，手动 `copilot find SR-8`，`runs/2026-09-09-copilot-71bfa8404dbe.log`）**：
剧情又放了一遍（昨天没看完，游戏没记成已看）→ 「跳过」与确认框都点到 → 进结算页 → **代理指挥解锁**。
两段收尾任务都报 Error：第一段等上游 `EndOfPlot` 白等 20 轮（跳过后直接进结算页，没有剧情结束画面），
第二段主界面任务不认结算页。推断坐实：**代理记录在结算页落库，昨天两次都没到结算页**。
收口（同日）：跳过链派生一份、确认后 `next` 清空，两段任一 Completed 即算收尾成功，措辞改中性；
结算页本身不处理（多关连抄由下一关 `StartUp` 处理，最后一关停在结算页被关掉已验证无害）。
「跳过剧情后先处理结算页再回主界面」的完整版**不做**，触发条件与坑见 DEV_GUIDE 关键事实 11。

### spike B — 一图流活动日历 / 物品价值接口 ✅（2026-09-08 23:35 实测，报告全文在当晚 scratchpad `research_yituliu.md`，要点如下）

**结论一句话**：一图流没有独立的「活动日历」JSON，排期在 `stage/info`；物品价值有 POST 接口；
「关卡效率」没有接口、前端现算，算法已复现并用 `1-7 == 1.000` 验过；复刻间隔可从历史统计，中位数 378 天。

| 接口 | 形态 | 关键字段 | 备注 |
|---|---|---|---|
| `GET https://backend.yituliu.cn/stage/info` | 313 KB，1550 关 | `stageId`（与 MAA / 企鹅同套）、`stageCode`、`zoneId`、`zoneName`、`stageType ∈ {MAIN, ACT, ACT_REP, ACT_PERM, ACT_MINI}`、`start` / `end`（Unix 毫秒，**`end` 永不为 null**，常驻用哨兵 `4070880000000`）、`apCost`、`spm`（理智/分钟） | **`ACT_REP` 就是复刻标记**（`stageId` 带 `_rep`）。当期新活动 `zoneName` 是占位符「新活动关卡」。**不含 EX、不含周常 `wk_*`**。无缓存头，自己做 1h TTL |
| `POST https://backend.yituliu.cn/item/v7/value`，body `{}` | 22 KB，129 条 | `itemId`、`itemName`、`itemValue`（理智当量）、`itemValueAp`、`rarity`（1 起）、`type` | **16 条 `itemValue == null`**（代币 / 小样）必须判空；114 条能对上 MAA `item_index.json` |
| `GET https://backend.yituliu.cn/item/v5/store/activity` / `store/act/history` | 1 条 / 64 条 | `actName`（正式活动名）、`endTime`（商店关闭，晚于关卡关闭）、`actStore[] {itemId, itemPrice, itemQuantity}`、`actTagArea[]`（署名文案） | 「活动商店能换哪些材料」的唯一结构化来源 |
| `GET https://cos.yituliu.cn/arknights/stage-drop/matrix.json` | 1.4 MB，10975 行 | `stageId / itemId / times / quantity / stdDev / start / end` | **实际约 10 分钟更新一次**，`max-age=7200` 不可信，用 `ETag`；是企鹅 `show_closed_zones=true` 的严格子集（裁掉低样本 1516 行） |
| `GET https://backend.yituliu.cn/v3/api-docs` | 95 KB | OpenAPI 全量清单 | 接口权威依据 |
| 企鹅 `/api/v2/zones` | 167 zone | `existence.CN.{openTime, closeTime}`（**`closeTime` 可为 null** = 常驻）、`zoneName`（含正式名与 `·复刻`）、`stages[]` | 一图流的后备；`max-age=300` |
| 企鹅 `/api/v2/stages` / `/items` | 1612 / 155 | `dropInfos[]`（定性）、`minClearTime`；`alias.zh` 俗名 | 企鹅 `rarity` 是 0 起，**比一图流小 1** |

- **复刻间隔统计**（`ACT → ACT_REP` 42 组）：中位数 378 天，p25–p75 = 351–387，近两年 345–410。
  `ACT → ACT_PERM`（常驻化）19 组波动大（20–686 天），不能用来预估。**官方前瞻排期不存在**（前端里的两条未来卡池是抽卡计算器用的硬编码，与材料无关）。
- **一图流关卡效率公式**（从 bundle 复现）：
  `E(stage) = Σ_item (quantity/times × itemValue[item]) + apCost × 12 × itemValue["4001"] (+ 活动关：活动商店最优兑换物折算)`；
  `stageEfficiency = E / apCost`。样本阈值 `times ≥ 300`。复现结果 `1-7 = 1.000`、`14-17 = 2.012`；异铁组：SR-8 27.0 理智/件 vs 主线最优 14-12 40.4 理智/件（活动关便宜 33%）。
- **许可**：一图流页脚 CC BY-NC 4.0 + 强制署名（商店接口自带文案「来源：明日方舟一图流 ark.yituliu.cn 转载需保留本提示」）；
  企鹅数据 CC BY-NC 4.0，无限速但要求 fair use。裸 curl 全通，无 UA / Referer / token 要求；建议项目统一 UA 并在卡片上带一行数据来源。
- **坑**：`item/v7/value` 自定义口径里 `chipPreference` 的 key 必须小写开头（大写会 500）；`/item/v5/custom` 是前端死代码（404）。

### spike C — 肉鸽上游参数与优雅停止 ✅（2026-09-08 只读调研，`--help` / `--dry-run` / 源码 / 文档；全文见 `docs/spikes/roguelike-2026-09-08.md`）

**结论一句话**：不要用 `maa roguelike` 子命令，走 `maa run` 任务文件（`type = "Roguelike"`，params 自由透传）；
每局一个子进程（`starts_count = 1`），worker 在局间读停止标志，就是「本局结束后再停」；紧急停用 SIGTERM 会丢当前局。

- **主题**：MaaCore v6.17.2 支持 `Phantom / Mizuki / Sami / Sarkaz / JieGarden / BlackFlow`（本机资源里有 `resource/roguelike/BlackFlow/`），
  但 maa-cli 0.7.5 的 `maa roguelike` 枚举**没有 `BlackFlow`**，`mode` 也硬限 0..5（MaaCore 另有 6 / 7 / 10001 / 20001 / 30001），
  还有 12 个 MaaCore 字段 CLI 覆盖不到。任务文件路线全部绕开。
- **maa-cli 预设的两处反常默认**：`stop_when_investment_full` 不加 flag 时发 `true`（协议默认 false）；Sami 不加 flag 时强制 `use_foldartal = false`。
  另 Phantom 主题会丢弃 `--difficulty`；`start_with_seed` CLI 发 bool 而协议要 string。任务文件路线同样绕开。
- **停止语义**：maa-cli 收到 SIGINT / SIGTERM 是 `bail!("Interrupted by user!")`，**不调 `AsstStop`**，第二次信号直接被内核杀；
  `AsstStop` 本身也是立即中止（下一个检查点退栈），停在哪算哪。上游自带的两个干净收手点：
  `starts_count` 用尽（在准备开第 N+1 局时停，即第 N 局已结算、人在主题入口页）；投资达标 / 储备满 999 时 `exit_then_stop(abandon=true)`。
  没有 API 能运行中改 `starts_count`。
- **推荐方案**：`with run_lock: 开模拟器 → while 未收到停止 且 未到 deadline: 子进程 maa run roguelike（starts_count=1）→ 解析输出发进度 → close_everything()`。
  每局重连开销实测约 1 s（asst.log 两次子进程 `Process Start → Assistant::start` 0.9 / 1.2 s），忽略不计。
  子进程**必须** `Popen` + `terminate()` + `wait(timeout)` + 最后才 `kill()`，不能 `subprocess.run(timeout=)`（那是 SIGKILL，模拟器会留在地图里）。
- **约束**：中断后**必须同主题重启**（上游 `Roguelike@Begin` 的 `Continue → ExitThenAbandon → AbandonConfirm` 链只在同主题下善后；跨主题会死循环，issue #13245）；
  手册要求把目标主题**置顶在终端**，自动导航「不保证长期可用」。
- **输出标记**（maa-cli 默认 info 级就有）：`Roguelike Start / Completed / Stopped / Error`；一局开始 `Start exploration N times`；
  一局结束 `Gain N exp during this exploration` 或 `Exploration Abandoned`；投资 `Deposit a / b / c originium ingots`（储备上限 999）；
  储备满 `TraderInvestSystemFull`；结尾 summary 段 `Explorations: … Summary: Passed x, Failed y, …` 可整块转 embed。
  `RoguelikeInvestmentReachLimit / ReachFull` 回调 maa-cli 不打印（要 `-vvv`）。
- **不耗理智**（PRTS wiki）；`maa roguelike` / 任务文件都**不含 StartUp**，模拟器与游戏由项目自己起（同 `_clearing_run_locked` 的开机链）；`--batch` 非必需但无害；
  环境用 `maa_subprocess_env(config)`（不留代理）+ `MAA_LOG_PREFIX=Never`。
- **难度上限**（GUI 字典）：Phantom 15 / Mizuki 18 / Sami 15 / Sarkaz 18 / JieGarden 18 / BlackFlow 15。
  **分队 / 职业组**是 OCR 字符串，白名单在 MAA GUI 的 `_squadDictionary`（各主题专属 + 通用 6 个 + 高规格；`roles` = 先手必胜 / 稳扎稳打 / 取长补短，
  界园与黑流树海另有灵活部署 / 坚不可摧，所有主题都有随心所欲）。手册推荐开局：萨卡兹 直面魂灵 4~10 / 蓝图测绘 + 远程战术 / 稳扎稳打 / 维什戴尔；
  刷锭用 `点刺成锭分队` 触发刷新商店加速；界园难度 ≥ 3 + 指挥分队触发时光之末跳关。
- **未验**：一局耗时（本机从未跑过肉鸽）；`starts_count = 1` 收尾瞬间的画面能否无缝接下一局；BlackFlow 走任务文件是否真能跑；
  `start_with_seed` 类型冲突的实际行为；SIGKILL 残局的恢复；帧率是否满足 60 帧要求。
