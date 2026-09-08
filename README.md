# maa-remote

在 macOS 上用 Discord 遥控 [MAA](https://github.com/MaaAssistantArknights/MaaAssistantArknights)
跑《明日方舟》国服日常：每天早上收一张计划卡，早晚各自动跑一遍完整任务链，跑完发中文战报和主界面截图，
再把模拟器和游戏进程清干净。新活动上线后会自动从 prts.plus 选好一份作业，Discord 只需确认是否执行；
也可以在聊天里说一句「抄 TO-9」手动匹配。

## 自用优先

这是我自己每天在用的项目，开源出来是觉得对同样在折腾这条链路的人可能有点参考价值。
有几件事想先说在前面，免得你抱着不太合适的期待：

- 它**只在一种环境里跑过**：Apple Silicon Mac + macOS + BlueStacks Air + 明日方舟国服。
  日服 / 美服、MuMu / 雷电、Windows / Linux 都还没适配，短期内大概也不会有。
- 开发时我会优先保证自己那台机器稳定，所以**配置项、状态文件格式、任务链结构都可能变动**，
  没有版本号，也不会写迁移脚本。跟进的话留意一下 diff 就好。
- 精力有限，**issue 和 PR 我会尽量看，但可能回得比较慢**。想改成自己顺手的样子的话，
  fork 一份通常比等我更快。
- 如果只是想拿点有用的东西走：我觉得最有价值的未必是整套能不能直接跑起来，而是
  [`docs/DEV_GUIDE.md`](./docs/DEV_GUIDE.md) §11 那 18 条踩坑记录，以及 `scripts/` 里针对 MaaCore
  实际行为写的那些兜底。

## ⚠️ 免责声明

- 本项目是**非官方**的第三方自动化工具，与 Hypergryph／鹰角网络、森空岛、MAA 官方均无关联。
  《明日方舟》及相关名称的权利归其各自所有者。
- 游戏自动化**可能违反游戏的用户协议**，是否使用、以及由此产生的任何账号风险，**由使用者自行承担**。
- `scripts/maa_skland.py` 通过**社区逆向得到的**森空岛接口读取练度与实时理智。接口随时可能变更或失效，
  且同样存在账号风险。手动作业在未接 token 时会退化为按热度排序；活动自动作业在执行前读不到理智则拒绝启动。
- 本仓库**不包含任何凭据**：Discord 频道 ID 是占位符，部署时注入；森空岛 token 只从 stdin 或环境变量进，
  永不落进 `config.json`、argv 或 Discord。

---

## 目录

- [它是怎么工作的](#它是怎么工作的)
- [仓库 ≠ 运行时](#仓库--运行时)
- [功能](#功能)
- [环境要求](#环境要求)
- [部署](#部署)
- [使用](#使用)
- [配置](#配置)
- [开发](#开发)
- [已知限制](#已知限制)
- [第三方组件与致谢](#第三方组件与致谢)
- [许可](#许可)

---

## 它是怎么工作的

核心简化是：**不新增任何常驻进程**。Discord 网关和 cron 调度由已经常驻的
[Hermes Agent](https://github.com/NousResearch/hermes-agent) 提供，本项目只贡献 6 条 cron
（4 个脚本）和 1 个动作脚本，全部是「跑完就退」的短命进程，状态落盘在 `~/.hermes/state/maa/`。

```
        Hermes gateway（唯一常驻进程）
        ├── cron 调度器（UTC）                     ├── Discord 适配器
        │                                          │
   ┌────┴────────────────────────────┐      ┌──────┴───────────────────────┐
   │ 每 15 分  maa_activity_watch.py │      │                              │
   │ 京 04:30  maa_depot.py          │      │                              │
   │ 京 07:50  maa_plan_card.py（早）│      │ 组件交互 hermes:btn:maa:*    │
   │ 京 21:50  maa_plan_card.py（晚）│      │   → 按钮登记表 → exec 白名单 │
   │ 京 08:00  maa_daily.py  （早）  │      │ 自然语言（免 @ 频道）        │
   │ 京 22:00  maa_daily.py  （晚）  │      │   → maa-operator 技能        │
   └─────────────────────────────────┘      └──────┬───────────────────────┘
                        ┌──────────────────────────┘
                        ▼   （唯一状态变更入口，< 1s 返回）
                  maa_action.py
                        │
                        ▼
   ~/.hermes/state/maa/{config,defaults,plan,last_run,activity_watch}.json + run.lock + runs/
                        │   effective_plan(at, phase) = defaults ⊕ 当天 sessions.<场次> 的覆盖
                        ▼
   maa_daily.py ── flock ── 探模拟器 ── 快照本场计划 ── 起 BlueStacks ── 等 ADB
        └─ 渲染 daily.toml ── maa run ── 每 5s 编辑子区里的同一条进度消息
              └─ 返回主界面 ── Vision OCR 核对 ── 截图 ── closedown ── 战报
```

一天的时间线：

| 北京时间 | 发生什么 |
|---|---|
| 每 15 分钟 | 读取 MAA 当前活动；活动期把「上游写了导航定义的关」排成目标集合（普通 + 突袭），按理智预算排出这一轮，发一张「🤖 自动作业已排程」卡（只有「取消本轮」一个按钮），到点**静默执行**、跑完发多关战报；练度不满足 / 暂无作业的关按 30 分钟退避重试 |
| 07:50 | 检查并跟随 maa-cli / MaaCore 更新 → 刷新当期活动关 → 发**早场计划卡**（一个关卡下拉 + 一个「跳过本场」按钮）→ 清理 24h 前的子区和 14 天前的运行产物 |
| 08:00 | 按**早场**的计划跑一遍完整日常。频道发「🟡 开跑」，其下开一个子区滚动中文步骤；跑完发战报卡片 + 主界面截图 |
| 21:50 | 刷新当期活动关 → 发**晚场计划卡**（同样两行控件）。**不跟随上游更新**，早上跟过了 |
| 22:00 | 按**晚场**的计划跑，任务链完全一致 |
| 任意时刻 | 点计划卡改**那一场**的关卡／跳过，或说「现在跑」立刻开一场手动日常；说「抄作业」拿一览卡（每关标已通 / 排程 / 练度不满足），从下拉选一关＝**立刻开跑** |
| 日常开跑前 | 估算理智；不够刷默认关就**跳过刷理智**，剿灭 / 基建 / 公招 / 商店 / 领奖照常（估不出来一律照常刷） |

## 仓库 ≠ 运行时

**这个仓库不是运行目录。** 改了仓库不跑 `deploy.py`，线上行为不会有任何变化。

| 仓库里的 | 部署后运行于 |
|---|---|
| `scripts/*.py` | `~/.hermes/scripts/*.py` |
| `scripts/ocrbox.swift` | `~/.hermes/scripts/ocrbox`（`swiftc` 编译产物） |
| `state/*.json` | `~/.hermes/state/maa/*.json`（种子 / 合并） |
| `config/maa-cli/**` | `~/.config/maa-cli/**` |
| `skills/games/maa-operator/SKILL.md` | `~/.hermes/skills/games/maa-operator/SKILL.md` |
| —（无源文件，`deploy.py` 打补丁） | Hermes 的 Discord adapter、`.env`、`config.yaml` |

一个特例：`~/.config/maa-cli/tasks/daily.toml` **每次执行前都会被 `maa_daily.py` 整个重写**，
仓库里那份只是初始种子和阅读参考，手改它没有效果。

## 功能

### 计划卡（每天两张：07:50 早场 / 21:50 晚场）

**计划是按场次的**：早场卡管 08:00 那场，晚场卡管 22:00 那场。改一场**不影响另一场**，
说「现在跑」的手动场两场覆盖都不吃，永远走缺口默认关。

每张卡**恰好两行控件**：一个关卡下拉 + 一个「⏭️ 跳过本场 / ▶️ 恢复本场」按钮。

- **关卡下拉**：`上次作战关卡` / `1-7` / **当天开放的资源本** / 当期活动关，
  每个选项都带**这关掉什么**（`SR-8 · 异铁组`）。材料名从上游 `stages.json` 的
  `dropInfos` 现取（`item_index.json` 翻中文名），所以**新活动上线自动跟随**，不用改配置。
  资源本按国服 04:00 的游戏日刷新过滤（`AP-5` 红票、`CA-5` 技能书、`CE-6` 龙门币、
  `LS-6` 作战记录、`SK-5` 基建材料），公告里出现「资源收集」时全部放开。
- 下拉选项的 `value` 是**不透明的 `choice:N`**，真实 argv（含场次）只存在按钮登记表的
  `select_args` 里，Discord 侧无法注入任意关卡名或场次。
- **材料缺口不是下拉，是后台固定计算**：每张卡算一次**三项加权分**，第一名直接写进
  当天该场次的计划，卡片「为什么」字段列出前三名和它们的三项分
  （「异铁组 → `SR-8` 综合 0.97（稀缺 0.93 / 缺口 1.00 / 效率 1.00）；还缺 52 个，27.0 理智/件」）。
  **你在某一场手动选过，当天那一场就不会再被自动改**，另一场照样自动算。

  | 项 | 权重 | 问的是什么 |
  |---|---|---|
  | 稀缺 | 0.5 | 这个材料现在能便宜刷，**下次能这么便宜刷是什么时候**。活动关比常驻关便宜 ≥1.3 倍才算数；下次可刷日按「首发 + 378 天」（历史 42 组复刻间隔的中位数）估，已经复刻过的活动按「不会再来」算 |
  | 缺口 | 0.3 | 按 30 天用量算还差多少个，再乘物品价值换成**理智当量**（1 个晶体电子单元 ≈ 302 理智 ≈ 17.6 个固源岩组，按件数排序会让便宜材料一直赢） |
  | 效率 | 0.2 | 今天能刷的关里，这个材料的「件/理智」离全游戏历史最优差多少 |

  权重在 `config.farming_weights` 里可以改，**和为任意值都会归一**。
  - 掉率与排期来自**明日方舟一图流**（企鹅物流作后备），运行时抓取、本地缓存，
    样本量 < 300 的关不计，往期已关闭的活动关数据不计。
  - 消耗率来自仓库对表，取 **≥4 周滚动窗口**（养成消耗是突发的：一次精二吃 20 个，
    平时是 0；只看最近一周会把几乎所有材料算成零消耗）。
  - **活动期间只在活动关里选**：月卡都不开，理智要 all in 活动才换得完代币商店。
    活动结束当天自动退回 `1-7` + 资源本。想刷资源本随时可以从下拉手动选。
  - **刷够就停**：第一名材料的缺口件数会变成 MAA 的 `drops` 参数（刷到 N 个即停），
    剩下的理智自动去刷第二名的关，不浪费。手动选的关不带这个上限。
  - **降级也会自动定关，但会明说**：数据抓不到就用本地缓存、缓存也没有就只用能算的那几项；
    `depot.json` 还没攒够 2 次对表时缺口项退回 `config.farming_manual_weights` 的手工权重，
    卡片标「降级」并列出原因，而且**此时不设 `drops` 上限**（缺口是猜的）。
    窗口内消耗为 0 的材料可用量是**未知**而不是 0 缺口，同样退回手工权重。
  - 上游掉落表和 `farming_stage_table` 都查不到关卡的材料**不推荐**、卡片明说原因——**不猜关**。
- **活动截止**：从 MaaCore 的 `StageActivityV2.json` 读当期活动的结束时刻（按它自己声明的
  时区解释），读不出来就整行不显示，不会因此让整张卡失败。
- **上游更新只在早场卡跑**：`brew` 跟随 maa-cli，再 `maa update` / `maa hot-update` 跟随
  Beta MaaCore 与资源热更新层，结果写进早场卡的「上游更新」字段；失败发红色告警并让 cron
  记为失败。晚场卡不跑这一步（早上刚跟过，21:50 再等几分钟网络没有意义）。
- 发新卡时把标题以 `🌱 MAA 今日计划` 开头的旧卡片组件 `PATCH` 成空，
  避免同时存在多张可点的卡——21:50 收掉早场卡是预期行为。

### 日常执行（08:00 / 22:00）

任务链固定为：`StartUp → [每日剿灭] → Fight → [Fight·续] → Infrast → Recruit → Mall → Award → Custom(返回主界面)`

| 环节 | 行为 |
|---|---|
| StartUp | 顺手处理每日首次登录的签到 / 配给弹窗 |
| 每日剿灭 | 每个游戏日（04:00 刷新）的第一场追加一次当期剿灭，自动点开全权委托消耗代理指挥卡，MaaCore 在周上限 1800 合成玉后停止；同一游戏日的后续场次不再进 |
| Fight | 关卡由计划卡的材料缺口算出（默认 `1-7`）。有缺口时带 `drops` 上限**刷够即停**，随后接一个**第二个 Fight 块**打第二名的关，把剩余理智用掉。`medicine = 0`、`stone = 0`、`series = 0` 由渲染器**硬编码**——只自动使用配置窗口内即将过期的理智药，**永不碎石**，连战次数由 MaaCore 按当前理智自动选最大 |
| Infrast | 使用 MaaCore `mode = 0`，按游戏内实时可见的干员、基建技能和心情自动排班；覆盖制造、贸易、发电、控制中枢、会客室、办公室与宿舍，不使用无人机、不补源石碎片订单、宿舍不刷信赖 |
| Recruit | 刷新公招，4 次，`select = [4,5,6]`、`confirm = [3,4,5,6]`，不使用加急许可 |
| Mall | 访问好友 + 信用商店购物，不打信用作战 |
| Award | 领日常、周常，以及邮箱内全部可领取附件 |
| 返回主界面 | 用 MaaCore 的 Home 导航（每项都带 `Home@` 前缀，否则 `#back` 无从解析），等动画稳定后截图，再用本机 **Vision OCR 核对截图确实停在主页**——不在就重截，重试用尽才判红 |

基建没有拿森空岛“拥有的干员”快照生成固定 JSON：该快照不包含可靠的当前心情、进驻位置和
房间状态，直接排会把旧数据冒充实时真值。这里复用 MaaCore 已有的智能排班，让它在游戏内读取
当场状态后计算，森空岛 token 过期也不会拖垮日常基建。

国服当前的“撤下宿舍锁定干员 / 预设队列干员”二次确认已改成红色确认条，而 MaaCore 上游基建
任务仍匹配旧蓝色按钮。本项目通过 maa-cli 用户资源层只覆盖这一项识别：复用资源内现成的
`PopupConfirm.png`，点击范围限制在右侧确认对勾；其余基建任务继续使用上游实现。

跑完按 bundle ID 请求模拟器正常退出，`pkill` 兜底，再用不会自匹配的进程模式复核，最后断开 ADB。

**「跳过」只关掉设它的那一场**（早场跳过不影响晚场）；说「现在跑」派发的手动场豁免 skip，照跑。
启动时快照当场计划，运行中改计划不影响本场。

**开跑前先探模拟器**：08:00 / 22:00、抄作业、仓库对表这些自动行为在拿到 `run.lock`
之后、开模拟器**之前**都会探一次——模拟器已经开着就判定「你在玩」，发一条灰卡说明并退出，
不会把你踢出游戏。探测预算 5 秒、永不抛异常、失败按离线处理照常执行。手动场「现在跑」豁免。

### 两条从实战里长出来的容错

- **代理指挥失误**（作战录像跟不上关卡随机性）会被 MaaCore 报成「掉落理智 ×N」，而且 Fight
  照样判 Completed。战报把返还的理智剔出掉落；整场一关没打成时刷理智翻红、写「实际完成 0 次」，
  并提示去手动重打三星刷新录像。**连续 2 批失误就熔断 `maa run`**，再补跑一趟去掉 Fight 的链条，
  把基建 / 公招 / 商店 / 奖励捞回来（两趟共用同一个超时预算）。详见 DEV_GUIDE §11.13。
- **模拟器冷启动竞态**：MaaCore 起 MaaTouch 只给 3 秒握手窗口，撞上开机抖动就报
  `TouchModeNotAvailable`，而 maa-cli 会带着没连上的 controller 继续跑，直接 SIGSEGV、
  整场 35 秒暴毙。所以冷启后先等 `app_process` 安静下来再交给 MaaCore；闸门之外还有一层重试，
  认出「连接阶段就崩、一条任务链都没 Completed」才重开 `maa run`，最多 2 次。详见 DEV_GUIDE §11.14。

### 抄作业（prts.plus）

**一条执行链，两个入口**（四期 D6 / D14）。自动负责活动上线后把关首通一遍，
手动只剩三种场景：练度不满足被跳过的关、非活动关（主线 / 资源本）、重打某关。
两者共用同一条 `run-batch` 执行链，手动卡上显示自动进度。

只支持上游已有导航的关卡，不自写选关路线：活动关查上游 `Stages/<前缀>.json`，
资源关查 `Supplies.json`，主线复用上游章节导航。导航使用临时配置：`Fight(times=1)`
仍执行上游选关，但 `FightBegin` 被限定为只识别「开始行动」、不点击；之后再做一次
独立界面识别。临时配置用完删除，不改变日常或抄作业本身的资源。`times=0` 在
MaaCore v6.17.2 会跳过整项任务，不能用来导航。
2026-09-08 15:17 已在游戏里实跑通过（SR-8：启动 → 导航到关卡 → 检查关卡界面 → Copilot Completed）。

**自动路径**（`maa_activity_watch.py`，每 15 分钟）：

1. 目标集合 = 当期活动前缀在本地关卡索引里的**全部关**（`SR-1..8`、将来的 EX / S）
   ∩ 上游导航键 − 已通关 − 教学关 − 退避中的关。森空岛 zone `totalStage == 0`
   （活动尚未全开）的关先不排，等热更新，**不记失败**。
2. 每关一次 prts 搜索（绕过 12 小时缓存），只接受 `ratio = 1.0` 且**无软硬缺口**的作业。
   搜不到就记「练度不满足 / 暂无作业」并按 `activity_search_retry_minutes` 退避，
   **不发确认卡、不降级**。
3. 按理智预算（当前理智 − 今天还要打的剿灭）排出这一轮的 `(关, 模式)` 条目：
   数字关升序 → EX → S，每关先普通后突袭。排不满就只排第一条并算出它的
   「理智够了的时刻」，写进卡片。
4. 到点 + 不在 08:00 / 22:00 前的黑名单 + `run.lock` 空闲 + 模拟器离线，才派发执行。

**执行链**（`maa_copilot_worker.py run-batch`）：抢锁 → 探模拟器 → 开机 →
**循环**每个条目（活动 + 理智复核 → 上游导航 → 突袭则先切难度 →
`maa copilot --formation --batch --raid <mode>` → 判胜负 → 记账）→ 截图 → 清理 →
一条多关战报。**一关失败继续下一关**；导航 / 切难度这类环境失败则停止整轮。
每关开始前还会检查距下一场日常是否够 `auto_copilot_min_lead_minutes`（默认 40 分钟），
不够就收尾，剩下的留给下一轮。

**通关判据与记账**：`state/maa/copilot/ledger.json`（0600）记每关每模式的通关时间戳。
「通关」= 所需模式全部出现 `Copilot Completed`（退出码 0 不算数）。活动关的所需模式是
**普通 + 突袭**（prts 的作业没有难度字段，同一份作业两种都打一遍），
**突袭失败这一关就不算通关**，下一轮只补突袭；资源本 / 主线只有普通。
「已通关」还认第二条判据：森空岛该关所属 zone 全清——那是 zone 粒度的妥协，
代价是你自己手打过的关可能被再抄一遍。换活动时旧账归档成 `ledger-<活动>.json`。
**当日上限**：同一「关 + 模式」当天失败 / 未确认达到 `auto_copilot_max_attempts_per_day`
（默认 2）次后当天不再自动排它，卡上写「🚫 今日已尝试 N 次」，次日自动恢复；
手动从下拉重打不受这条限制。

**手动路径**：聊天说「抄作业 / 有什么关能抄」→ 发一张**抄作业卡**，一览里每关标
`✅ 已通` / `🔁 普通已通、突袭待补` / `⏳ 排程 HH:MM` / `🚫 今日已尝试 N 次` /
`🚫 练度不满足（缺：…）` / `⛔ 上游无导航` / `🕐 待抄`；下拉里放所有上游有导航的关（**包括已通关的**，那是重打）。
**选中即执行**：森空岛刷新失败直接报失败，没有完全满足练度的作业直接报失败并写清缺什么，
有就直接排程 + 开跑。没有活动时下拉退化成当天开放的资源本 + `1-7`。
这张卡有自己的按钮前缀 `hermes:btn:maa-copilot:card:`，发新卡时先把上一张的组件 `PATCH` 空
并删掉旧登记；它既不进计划卡的 `hermes:btn:maa:` 清扫，也不被搜索的 TTL 清扫误删。

- 与日常**互斥**：`run.lock` + 08:00 / 22:00 前 25 分钟黑名单 + Depot 对表窗口（京 04:20–05:00）。
- **绝不降级**：不用助战、不忽略作业的练度要求、不吃理智药、不碎石。练度不够就是不打。
- 练度与活动进度来自森空岛（`maa_skland.py`）：有未执行的关时每 tick 刷新一次，
  每关开打前还会再强制刷新一次读实时理智。拉取成功也取决于森空岛上游同步进度，
  不保证刚打完的关卡立即可见。干员的专精 / 模组 / 精英化参与匹配，缺口文案形如
  「缺：史尔特尔 专精2（你专精1）」，模组是软约束、半权重。签名公式已冻结黄金向量测试。
- 已观察到的活动只有连续两次轮询都返回空，才会被视为结束并撤掉未打完的一轮；
  单次空结果只记一次防抖，不会把短暂的上游抖动误判成轮换。
- 抄作业结束在结算界面，不受返回主页那道闸门约束。
- ⚠️ **突袭切换（`ChangeToRaidDifficulty`）尚未真机验证**：验不过就把
  `config.copilot_raid_switch_task` 设成空串，自动路径退回只打普通，突袭留人工。

### 肉鸽（手动触发）

聊天说「刷肉鸽」→ 一张配置卡（**4 个下拉 + 1 行 3 个按钮，正好 5 行**）→ 点「▶️ 开始」→
后台持 `run.lock` 循环调上游，**每打完一局发一条进度**，跑完一条汇总战报。
**肉鸽不消耗理智**，不进理智预算、不影响日常刷图。

- **四个下拉**：主题（傀影 / 水月 / 萨米 / 萨卡兹 / 界园 / **黑流树海**，默认萨卡兹）、
  模式（刷源石锭 / 刷等级 / 烧水凹开局，萨米追加坍缩范式；默认刷源石锭）、
  难度（不指定 / MAX / n..0，上限按主题 15 或 18；默认取手册推荐）、
  分队（按主题 + 模式从 MAA GUI 的字典生成；萨卡兹刷锭默认点刺成锭分队，其余指挥分队）。
  职业组不占 UI，按主题固定（傀影 `取长补短`，其余 `稳扎稳打`）；开局干员留空让上游按练度选。
  **换主题会自动重发一张卡**——模式 / 难度 / 分队的选项都随主题变。
- **执行路线是 `maa run` 任务文件**，不是 `maa roguelike` 子命令：maa-cli 0.7.5 的枚举里
  没有黑流树海、`mode` 硬限 0..5、两处默认值和 MaaCore 协议相反、傀影的难度参数会被丢掉。
  任务文件的 `[tasks.params]` 原样透传，一次绕开这些坑（黑流树海因此还能传
  `blackflow_strategy`：刷等级快速飞三层 / 刷源石锭投完就退 / 刷襁褓动物）。
- **优雅停止**：`starts_count = 1`，**一局一个子进程**。
  「⏹️ 本局结束后停止」只写一个标志，worker 在**局间**读它——当前这一局由上游打完、
  干净停在主题入口页，**进度不丢**。「⏏️ 立即中止」才对子进程 `terminate()`（SIGTERM），
  **当前这一局作废**。
- **自动收尾**：源石锭储备满 999（上游自己 `exit_then_stop`）、连续 2 局上游报错、
  距 08:00 / 22:00 日常不足 25 分钟、进入 Depot 对表窗口（京 04:20–05:00），都不再开新局。
  单局超过 `roguelike_run_timeout`（默认 1 小时）会被终止。
- ⚠️ **换主题前先在游戏里把该主题置顶到终端**：上游的自动导航「不保证长期可用」。
  而且上一局被中止过（`dirty`）时**不许换主题**——上游只在同主题下才会自己放弃残局，
  跨主题会死循环，所以要先用同一个主题跑一局收尾。
- ⚠️ **本机从未真机跑过肉鸽**：一局耗时、`starts_count = 1` 的收尾画面能否无缝接下一局、
  黑流树海走任务文件是否真能跑，都要首跑验证。

### 安全边界

- `maa_action.py` 是**唯一**状态变更入口。技能文件（`SKILL.md`）明确禁止 LLM 直接写
  `plan.json` / `defaults.json` / `config.json` / 按钮登记表，也禁止直接调用 runner。
- LLM 能改的只有**今天某一场的关卡**和**那一场是否跳过**，其余（任务链、次数、吃药、ADB、
  模拟器、Discord 配置）一律拒绝。碎石**永久关闭**，不提供入口、脚本级硬拒。
- 场次参数只接受 `am` / `pm`，非法值脚本层拒绝；卡片上的场次由按钮登记表写死，不从 Discord 直通。
- 技能明确要求**绝不索取、接收、复述或写入森空岛 / 鹰角 token**；用户想在聊天里贴，引导去本机终端。
- Discord 侧永不展示 maa-cli 的原始 stdout/stderr，只展示中文步骤和战报；CLI 原文只写本地日志。
- `copilot cancel` 的 token 校验 `^[0-9a-f]{12}$`；一轮只在 `scheduled` / `queued`
  阶段可取消，开跑之后取消不了（那时理智可能已经花掉了）。
- 抄作业 argv 里**永不**出现忽略练度要求 / 理智药 / 助战三个降级开关，
  连字面量都不许写进脚本，有一条全库 grep 的单测守着。

## 环境要求

| | |
|---|---|
| 硬件 / 系统 | Apple Silicon Mac，macOS（开发机为 M4 / macOS 26.6 / 16GB） |
| 运行时依赖 | [Hermes Agent](https://github.com/NousResearch/hermes-agent) gateway 常驻，且已连上 Discord |
| Python | 开发机为 3.12。**零第三方依赖**，全部 stdlib（`zoneinfo` 需要 3.9+，但更低版本没验证过） |
| 模拟器 | BlueStacks Air，实例 4 核 / 4GB / **1920×1080 横屏**（默认实例是竖屏，不改 MAA 必然识别不到） |
| ADB | BlueStacks 自带的 `hd-adb`，`127.0.0.1:5555` |
| MAA | maa-cli（Homebrew tap `MaaAssistantArknights/tap/maa-cli`）+ MaaCore，`touch_mode = "MaaTouch"` |
| 游戏 | 明日方舟**国服** `com.hypergryph.arknights`，已侧载并登录到主页面 |
| 可选 | `swiftc`（编译 ocrbox；缺了抄作业的接管弹窗 watchdog 不可用）、森空岛账号 |

MAA 官方文档明确支持 BlueStacks Air（M 系列）+ `127.0.0.1:5555` + MaaTouch，这是整条链路成立的前提。

## 部署

`deploy.py` 是幂等的：先把要修改的 Hermes 文件备份到 `~/.hermes/backups/maa-setup/<时间戳>/`，
再部署脚本、状态、maa-cli 配置、技能、按钮白名单、频道绑定和免 @ 配置，最后编译 ocrbox。

```bash
python3 -m unittest discover -s tests -v
```

```bash
MAA_CHANNEL_ID=<频道数字ID> python3 deploy.py
```

已配置过的机器直接 `python3 deploy.py` 即可——频道 ID 会从本机已部署的 `config.json` 解析。
想同时写入森空岛 token：

```bash
SKLAND_HG_TOKEN=<token> MAA_CHANNEL_ID=<频道数字ID> python3 deploy.py
```

也可以事后单独在本机终端跑 `python3 ~/.hermes/scripts/maa_skland.py link`，从 stdin 粘贴。

然后建六条 cron。Depot 每天 04:30 只检查是否满 7 天，未到期不会启动模拟器；
**计划卡和日常都用不同工作目录传递场次标记**（`…/maa/am`、`…/maa/pm`），
因为 Hermes 的 `--script` 不支持 argv：

```bash
hermes cron create "*/15 * * * *" --name "MAA 活动作业监控" --script maa_activity_watch.py --no-agent --deliver local
```

```bash
hermes cron create "30 20 * * *" --name "MAA 仓库对表" --script maa_depot.py --no-agent --deliver local
```

```bash
hermes cron create "50 23 * * *" --name "MAA 计划卡·早" --script maa_plan_card.py --no-agent --deliver local --workdir ~/.hermes/state/maa/am
```

```bash
hermes cron create "50 13 * * *" --name "MAA 计划卡·晚" --script maa_plan_card.py --no-agent --deliver local --workdir ~/.hermes/state/maa/pm
```

```bash
hermes cron create "0 0 * * *" --name "MAA 日常·早" --script maa_daily.py --no-agent --deliver local --workdir ~/.hermes/state/maa/am
```

```bash
hermes cron create "0 14 * * *" --name "MAA 日常·晚" --script maa_daily.py --no-agent --deliver local --workdir ~/.hermes/state/maa/pm
```

这些 cron 表达式按 UTC 解释；Depot 的 `20:30 UTC` 对应北京时间次日 `04:30`，
计划卡的 `50 23` / `50 13 UTC` 对应京 `07:50` / `21:50`。

> **已经建过计划卡 cron 的机器**：原来那条（`03d5e98ce8d4`，无 workdir）要改成带
> `--workdir ~/.hermes/state/maa/am`，或者删了按上面重建，否则场次只能靠小时兜底判断。

检查部署是否完整：

```bash
python3 deploy.py --check
```

> **`hermes update` 会覆盖 adapter 补丁**——升级 Hermes 后必须重跑 `deploy.py` 并复验按钮。

完整端到端验收必须在真实游戏里做，单元测试替代不了真实的图像识别。

## 使用

日常操作点计划卡上的下拉和按钮就行。频道里也可以直接说人话，由 `maa-operator` 技能翻译成
下面这些命令——**这是 LLM 唯一被允许执行的命令集**：

```text
maa_action.py plan set-stage <关卡> <am|pm>     # 「早上刷 CE-6」「晚上换 1-7」
maa_action.py plan set-stage "" <am|pm>         # 「这场刷上次的本」
maa_action.py plan skip-today on|off <am|pm>    # 「早场别跑」/「晚上还是跑吧」
maa_action.py plan show                         # 「今天怎么跑」（一次念完两场）
maa_action.py run now                           # 「现在跑」（豁免 skip、不吃场次覆盖）
maa_action.py log tail                          # 「最近怎么了」「看日志」
maa_action.py copilot card                      # 「抄作业」「有什么关能抄」「打到第几关了」
maa_action.py copilot find <关卡>               # 「抄 SR-8」——搜到就直接开跑
maa_action.py copilot status <token>            # 「这轮到哪了」
maa_action.py copilot cancel <token>            # 「已排程」卡上的「❌ 取消本轮」
maa_action.py roguelike card                    # 「刷肉鸽」「开肉鸽卡」「打集成战略」
maa_action.py roguelike stop                    # 「肉鸽停」「打完这局停」
maa_action.py roguelike abort                   # 「肉鸽立刻停」（当前局作废）
maa_action.py roguelike status                  # 「肉鸽到哪了」
```

肉鸽的 `roguelike set <字段> <值>` 与 `roguelike start` **只由卡片上的下拉和按钮触发**，
不在 LLM 的清单里：主题 / 模式 / 难度 / 分队只能你自己在卡上点，聊天侧永远只发卡。

**场次参数不能省**：`am` = 早场 08:00，`pm` = 晚场 22:00。你没说是哪一场时它会先问，不会替你挑。

## 配置

`state/config.json` 是部署到 `~/.hermes/state/maa/config.json` 的种子（`update` 合并，不覆盖
计划卡探测到的版本号）。几个值得知道的键：

| 键 | 默认 | 含义 |
|---|---|---|
| `channel_id` | 占位符 | 部署时由 `MAA_CHANNEL_ID` 或本机已有配置解析，**不入库** |
| `adb_path` / `adb_address` | BlueStacks `hd-adb` / `127.0.0.1:5555` | 换模拟器时这里和 maa-cli profile 两处都要改 |
| `maa_run_timeout` | `3000`（50 分钟） | 为 Hermes 外层 1 小时上限预留清理时间 |
| `emulator_settle_quiet_seconds` | `8` | 冷启后等 `app_process` 安静多久再交给 MaaCore |
| `connect_retry_limit` | `2` | 连接阶段崩溃的重开次数，共用同一个超时预算 |
| `medicine_expire_days` | `2` | 只吃这个窗口内即将过期的理智药 |
| `emulator_probe_timeout` | `5`（不在种子里，代码默认） | 开模拟器前那次「你是不是在玩」探测的总预算（秒）；设 `0` 关闭探测 |
| `maa_activity_cache` | 不在种子里 | 覆盖 `StageActivityV2.json` 的路径（默认 `~/Library/Caches/com.loong.maa/StageActivityV2.json`），只用于卡片上的「活动截止」 |
| `fight_times` | `2147483647` | 生产值。配合 `series = 0` 一直打到理智不足 |
| `proxy_failure_limit` | `2` | 连续多少批代理指挥失误触发熔断 |
| `proxy_failure_salvage` | `true` | 熔断后是否补跑去掉 Fight 的链条 |
| `retention_days` | `14` | `runs/` 产物保留天数（子区固定 24h） |
| `copilot_blackout_minutes` | `25` | 自动场次前多久禁止派发抄作业 |
| `activity_search_retry_minutes` | `30` | 某关练度不满足 / 暂无作业时的重试间隔 |
| `auto_copilot_min_lead_minutes` | `40` | 距下一场日常不足这么多分钟就不再开新的一关（比黑名单宽，因为它要覆盖整关） |
| `auto_copilot_max_attempts_per_day` | `2` | 同一「关 + 模式」当天失败 / 未确认这么多次后，**当天不再自动排它**（卡上写「🚫 今日已尝试 N 次」），次日自动恢复。手动从下拉重打不受限。`0` = 不限制 |
| `copilot_raid_switch_task` | `ChangeToRaidDifficulty` | 从普通难度切到突袭用的上游任务名。**留空 = 跳过切换**，自动路径退回只打普通 |
| `roguelike_run_timeout` | `3600`（1 小时） | 肉鸽**单局**子进程的上限。超了走 `terminate()`（SIGTERM），不是 SIGKILL，好让 MaaCore 有机会收手。整轮的长度由 blackout / Depot 窗口约束，不在这里叠加 |
| `annihilation_sanity` | `25` | 排作业理智预算时为每日剿灭预留的理智 |
| `farming_stage_table` | 见下 | 材料 → 关卡的手工映射 |
| `farming_manual_weights` | 见下 | 冷启动/零消耗时的手工权重，数值越大越优先 |
| `farming_weights` | `{"scarcity":0.5,"gap":0.3,"efficiency":0.2}` | 三项加权分的权重。**和为任意值都会归一**，写 `5/3/2` 也行 |
| `farming_min_samples` | `300` | 掉率的最低样本量，抄一图流前端的口径；低于它的关不参与计算 |
| `farming_target_days` | `30` | 缺口按多少天的用量算（也是 `drops` 上限的来源） |
| `farming_activity_edge` | `1.3` | 活动关要比最优常驻关便宜多少倍才算「明显更优」，达不到就不给稀缺分 |
| `farming_rerun_days` | `378` | 预计复刻间隔（天）。42 组 `ACT→ACT_REP` 的历史中位数，p25–p75 是 351–387 |
| `farming_scarcity_horizon_days` | `400` | 稀缺分的视野：距下次可刷日超过这么多天就按满分 1.0 算 |

### 怎么改材料关卡表

**大多数情况下你不用改**：材料 → 关卡的映射以上游 `stages.json` 的掉落表为主，
新活动上线会自动跟随。手工表只在两种时候有用——**资源本**（`CE-6` / `AP-5` 这些在上游
只有家具掉落，掉落表里查不到龙门币和红票），以及你想**钉死**某个材料去固定的关。

两张表都在 `state/config.json`，改完 `python3 deploy.py` 生效（它们是**仓库拥有的
配置**，不是运行时缓存，所以部署时仓库的值会覆盖线上同名键）：

```jsonc
"farming_stage_table":   { "固源岩组": "1-7", "碳素": "SK-5" },   // 材料 → 去哪刷
"farming_manual_weights": { "固源岩组": 100,  "碳素": 60 }        // 越大越优先
```

- 材料名必须与 `depot.json` 里的 `items[].name` **逐字一致**（那是 MaaCore
  `item_index.json` 的中文名）；对不上的名字只是不出现在推荐里，不会报错。
- 手工表**优先于**上游派生表：你显式写下的映射不会被自动派生悄悄改掉。
- 上游掉落表和手工表都查不到关卡的材料**不会被推荐**，卡片上会列出来提醒你补表——
  **不要让代码去猜**，猜错就是每天几百理智刷错东西，而且从卡片上看不出来。
- 种子里只放了确定的关：`1-7` 与五个资源本（`AP-5`/`CA-5`/`CE-6`/`LS-6`/`SK-5`）。
  资源本按星期开放，当天没开的会被自动排除。
- 上游掉落表给的是「这关掉什么」，**没有掉率**。真实的「每理智期望产出」来自
  一图流 / 企鹅物流（见下面的致谢），抓不到时才退回启发式排序
  （手工表钉的优先 → 理智低的优先 → 关卡名兜底）。

`defaults.json` 是持久默认（关卡、任务开关），`plan.json` 是**带日期戳的今日覆盖**，
过期自动失效。`medicine` / `stone` 已从可变状态里移除，改由 runner 硬编码策略。

## 开发

```bash
python3 -m unittest discover -s tests -v
```

现有 stdlib 单测约 10 秒，不碰模拟器。**必须在仓库根目录跑。**

改完代码要跑 `deploy.py` 才生效；改了 adapter / `config.yaml` / `.env` / `SKILL.md` 还要重启 gateway。

文档分工——都在 [`docs/`](./docs)，根目录只留 README 和 AGENTS：

| 文件 | 讲什么 |
|---|---|
| [docs/DISCORD_GUIDE.md](./docs/DISCORD_GUIDE.md) | **面向使用者**：频道里会自动发生什么、能点什么、能说什么 |
| [docs/DEV_GUIDE.md](./docs/DEV_GUIDE.md) | **现状的链路怎么串起来、改哪里影响什么**。§8 是不可破坏的不变量，§10 是「症状 → 看哪里」，§11 是 18 条踩坑记录 |
| [docs/PLAN.md](./docs/PLAN.md) | 一期的设计意图与决策表 |
| [docs/PLAN_PHASE2.md](./docs/PLAN_PHASE2.md) | 二期（prts.plus 抄作业）方案 |
| [docs/PLAN_NAV_GATE.md](./docs/PLAN_NAV_GATE.md) | 导航闸门 + 自动任务模拟器探测 |
| [docs/PLAN_CLEARING.md](./docs/PLAN_CLEARING.md) | 三期方案与进度（通关队列已在四期步骤 2 拆除，留作历史记录） |
| [docs/REVIEW_PHASE2.md](./docs/REVIEW_PHASE2.md) | 二期五轮评审的 29 条问题与修复记录 |

## 已知限制

- **只测过一种环境**（见[自用优先](#自用优先)）。国服以外的服务器、其他模拟器都还没有适配。
- **BlueStacks 需要活跃的图形会话**——锁屏 / 关显示器没问题，注销或切换用户会失败。
- **macOS 上 maa-cli 的默认配置目录**是 `~/Library/Application Support/com.loong.maa/config`，
  与本项目约定不同，所以每个子进程都显式注入 `MAA_CONFIG_DIR`。新增调用 maa-cli 的代码
  一定要走 `maa_subprocess_env()`，不要裸 `subprocess.run`。
- **代理开关是分裂的**：日常 `maa run` 要剔除代理，计划卡的 `maa update` 必须**带着**代理访问 GitHub。
  代理变量只在 maa-cli 子进程里移除，不改用户会话或其他程序的环境。
- **删子区需要 bot 的 Manage Threads 权限**，没有就只能靠 `auto_archive_duration=1440` 归档。
- **MAA 没为该关写导航定义时抄作业会失败**（上游每个活动只写 4 个左右推荐关的导航定义，
  `Stages/<前缀>.json` 里没有该关的键）——这是预期行为，不是 bug；原因通常不是"关卡未解锁"。
  **这是一条明确的边界，不是待办**：本项目不自写导航定义，也不为上游没覆盖的关补自动战斗；
  日常、抄作业、自动匹配刷材料关一律只跑上游提供了导航的关（2026-09-08 决策，
  见 [docs/PLAN_CLEARING.md](./docs/PLAN_CLEARING.md) 步骤 2）。
- **活动监控依赖本机 MAA 热更新资源**。资源还没包含新活动时不会猜活动；07:50 更新资源后，
  watcher 才能按资源内的开放时间识别。自动作业只取上游第一推荐关，不代替前置关卡通关。
- **动 BlueStacks 三件套里的任何一个，整套会被连坐删除**——安装包附带的 LaunchDaemon
  盯着三个 app 路径，少任何一个就 `rm -rf` 全部（含实例数据）。要卸载捆绑的 BlueAI
  **必须先 bootout 并删掉那个 plist**。详见 DEV_GUIDE §11.12。
- 熔断后的补跑逻辑有单元测试和真实日志回放，但「StartUp 能否从结算页把游戏带回主界面」
  只在真机上才算数，尚未实机验收。

## 第三方组件与致谢

本项目**不包含**下列任何项目的源码，全部通过 CLI 子进程或 HTTP 接口调用：

| 项目 | 关系 | 许可 |
|---|---|---|
| [MaaAssistantArknights](https://github.com/MaaAssistantArknights/MaaAssistantArknights) / maa-cli | 以独立进程调用 `maa` 命令行；`config/maa-cli/**` 是按其公开 schema 写的配置 | AGPL-3.0 |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | 提供 Discord 网关与 cron；`deploy.py` 对其 Discord adapter 打本地补丁，补丁锚点引用了少量上游源码片段 | MIT，© 2025 Nous Research |
| [prts.plus](https://prts.plus) | 作业数据来源，只读其公开 HTTP 接口 | — |
| [明日方舟一图流](https://ark.yituliu.cn) | 掉落矩阵、关卡排期与复刻标记、物品价值表、活动商店。**运行时抓取、只缓存在本机，不入库**；材料推荐的输出带署名一行 | 数据 [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/deed.zh)，转载须注明来源 |
| [企鹅物流数据统计](https://penguin-stats.io) | 一图流的后备数据源（掉落矩阵、活动开放时间、关卡元数据），同样只读、不入库 | 数据 [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/deed.zh) |
| 森空岛 | 练度与执行前理智数据来源，社区逆向接口 | — |

感谢 MAA 项目组把这条自动化链路做成可能，也感谢一图流与企鹅物流长期维护这份公开的掉落数据。

本项目是自用的非商业工具，符合两家数据许可的 NC 条款；材料推荐的每一次输出都带一行
「数据来源：明日方舟一图流 ark.yituliu.cn / 企鹅物流 penguin-stats.io（CC BY-NC 4.0）」。

## 许可

[MIT](./LICENSE) © 2026 ffkiyo7

许可只覆盖本仓库自己的代码与文档。上面那些第三方项目各自适用它们自己的许可。
