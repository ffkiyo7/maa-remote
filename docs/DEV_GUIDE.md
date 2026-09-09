# 开发指南（基于当前代码实现）

面向要改这套东西的人。[PLAN_MANUAL_RUN.md](./PLAN_MANUAL_RUN.md) 是最近一次改动（手动场选关卡，2026-09-09）的方案；四期及更早的设计意图与决策在 [archive/](./archive/)（[archive/PLAN_PHASE4.md](./archive/PLAN_PHASE4.md) 为四期），[README.md](../README.md) 是功能与落地状态，
本文只讲**现状的自动化链路是怎么串起来的、改哪里会影响什么**。

---

## 0. 先建立的心智模型：仓库 ≠ 运行时

这个仓库**不是运行目录**。所有东西真正跑起来的位置在 Hermes 家目录和 maa-cli 配置目录：

| 仓库里的 | 部署后运行于 | 谁负责搬运 |
|---|---|---|
| `scripts/*.py` | `~/.hermes/scripts/*.py` | `deploy.py` |
| `state/*.json` | `~/.hermes/state/maa/*.json` | `deploy.py`（种子/合并，见 §7） |
| `config/maa-cli/**` | `~/.config/maa-cli/**` | `deploy.py` |
| `skills/games/maa-operator/SKILL.md` | `~/.hermes/skills/games/maa-operator/SKILL.md` | `deploy.py` |
| —（无源文件） | `~/.hermes/hermes-agent/plugins/platforms/discord/adapter.py` 等 4 处 | `deploy.py` 打补丁 |

**改了仓库不跑 `python3 deploy.py`，线上行为不会有任何变化。** 反过来，直接手改
`~/.hermes/scripts/` 下的文件也不会回流到仓库，且下次 `deploy.py` 会被覆盖（覆盖前有备份）。

还有一个例外要特别记住：`~/.config/maa-cli/tasks/daily.toml` **每次执行前都会被
`maa_daily.py` 整个重写**（见 §4 第 6 步）。仓库里的那份只是初始种子和阅读参考，
手改它没有任何效果。

---

## 1. 全链路总图

```
        Hermes gateway（LaunchAgent ai.hermes.gateway，唯一常驻进程）
        ├── cron 调度器（UTC）                    ├── Discord 适配器（网关连接）
        │                                          │
   ┌────┴──────────────────────────────┐      ┌────┴─────────────────────────┐
   │ */15 * * * * maa_activity_watch  │      │                              │
   │ 30 20 * * *  maa_depot.py         │      │                              │
   │ (京 04:30；到期才开模拟器)       │      │                              │
   │ 50 23 * * *  maa_plan_card.py am  │      │ 组件交互 hermes:btn:maa:*    │
   │ (京 07:50, workdir=…/maa/am)      │      │   → buttons.json 查登记      │
   │ 50 13 * * *  maa_plan_card.py pm  │      │   → exec 白名单校验          │
   │ (京 21:50, workdir=…/maa/pm)      │      │   → maa_action.py（≤20s）    │
   │  0  0 * * *  maa_daily.py  am     │      │                              │
   │ (京 08:00, workdir=…/maa/am)      │      │ 自然语言（免 @ 频道）        │
   │  0 14 * * *  maa_daily.py  pm     │      │   → maa-operator 技能        │
   │ (京 22:00, workdir=…/maa/pm)      │      │   → terminal 调 maa_action.py│
   └───────────────────────────────────┘      └────┬─────────────────────────┘
                                                   │ 只写 plan.json / 派发
                        ┌──────────────────────────┘
                        ▼
   ~/.hermes/state/maa/{config,defaults,plan,last_run}.json + run.lock + runs/
                        │  effective_plan(at, phase) = defaults ⊕ 当天 sessions.<场次>
                        ▼
   maa_daily.py ── flock ── 探模拟器 ── 快照 runs/<date>-<phase>.json ── 起 BlueStacks
        └─ 渲染 daily.toml ─ maa run daily ─ 5s 一次编辑子区消息
                                 └─ 截图 ─ closedown/退出/断 ADB ─ 战报 embed
```

六条 cron 全是 `--no-agent --deliver local` 的**短命进程**：跑完即退，不常驻，
stdout 不投递到 Discord（消息由脚本自己用 Discord REST API 发）。

---

## 2. 组件清单

| 文件 | 角色 | 触发方式 | 典型耗时 |
|---|---|---|---|
| `scripts/maa_common.py` | 无第三方依赖的公共层：时区、三把锁、原子写、Discord REST/multipart、清理、`prune_button_registry`、**场次级计划求值**（`plan_sessions` / `effective_plan`）、**模拟器占用探测**（`emulator_is_online` / `report_emulator_busy`） | 被其他几个 import | — |
| `scripts/maa_plan_card.py` | 早晚两张计划卡 + 抄作业卡的渲染层；早场卡还先检查/跟随 maa-cli 与 MaaCore；刷新活动关；写按钮登记；清理旧子区/旧卡片/旧产物 | cron 京 07:50（am）/ 21:50（pm）；抄作业卡由 `maa_action.py` 调 | 早场数秒～5 分钟（含 `maa update` / `brew`），晚场数秒 |
| `scripts/maa_depot.py` | 每周仓库对表；只接纳 MaaCore `done=true` 的完整 Depot 结果，计算本地掉落增量与账号消耗率；**森空岛不可用时顺手跑一趟 `OperBox` 拿练度后备** | cron 京 04:30 每日检查，满 7 天才执行 | 到期时数分钟；未到期不启动模拟器 |
| `scripts/maa_activity_watch.py` | 轮询 MAA 活动视图；排目标集合、按理智排一轮 `(关, 模式)`、发「已排程」卡、到点派 `run-batch` | cron 每 15 分钟 | 通常 <1s；要现搜作业时每关 5~20s（每 tick ≤6 关） |
| `scripts/maa_daily.py` | 执行主体：模拟器 → maa-cli → 流式进度 → 截图 → 清进程 → 战报 | cron 京 08:00 / 22:00；选关卡 `run start` 派发 `now <token>` | 10～50 分钟 |
| `scripts/maa_action.py` | **唯一**状态变更与派发入口（按钮、下拉、聊天共用） | Discord 组件交互；agent 用 terminal 调用 | < 1s（硬性要求） |
| `scripts/maa_copilot.py` | 抄作业领域层（纯函数）：关卡索引、prts 客户端、匹配打分、目标集合、**本地作业记账**（`ledger.json`）、已排程卡 | 被 action/watch/worker/plan_card import | — |
| `scripts/maa_copilot_worker.py` | 抄作业后台：`search <token>` 手动搜一关并直接排程；`run-batch <token>` **一条执行链**串行连抄一轮 | 仅由 `maa_action.py` / `maa_activity_watch.py` 派发 | 搜索 5~20s；一关 8~15 分钟 |
| `scripts/maa_farming.py` | **素材权重领域层**：一图流 / 企鹅物流抓取层（缓存在 `state/maa/farming/`）+ 三项加权分纯函数（稀缺 / 缺口 / 效率）+ 候选关集合（D5）+ `stage_ap_cost()` 兜底 | 被 plan_card / activity_watch import | 有缓存 <0.5s；冷缓存 3~10s（下 1.4 MB matrix） |
| `scripts/maa_roguelike.py` | **肉鸽领域层**（纯函数 + 一份状态文件）：主题 / 模式 / 难度 / 分队字典、下拉选项、`render_task_toml()`、上游输出解析、配置卡 | 被 action / roguelike worker import | — |
| `scripts/maa_roguelike_worker.py` | **肉鸽后台**：`run <token>` 抢锁 → 开机 → **一局一个子进程**地循环 → 逐局进度 → 汇总战报 | 仅由 `maa_action.py` 派发 | 一局十几~几十分钟 × N |
| `scripts/maa_clearing.py` | 作业链领域层（近纯函数）：条目形态（`#raid` 后缀）、上游导航键闸门、理智预算与排轮、搜索失败退避记账、通关判据 | 被 action/watch/worker/plan_card import | — |
| `scripts/maa_skland.py` | 森空岛协议：stdlib HMAC 签名、grant→cred 链、练度与理智拉取、`link`（stdin 收 token） | 本机终端；手动搜索强制刷新（失败即失败）；watcher 在「有未执行的关」的 tick 刷新；每关开打前再刷一次 | 秒级 |
| `scripts/ocrbox.swift` | macOS Vision OCR 小工具（编译为 `~/.hermes/scripts/ocrbox`），watchdog 识别接管弹窗 | 被 worker 调用 | <1s/次 |
| `deploy.py` | 幂等部署 + 给 Hermes 打本地补丁 + 备份 + 编译 ocrbox | 人工 | 秒级 |
| `skills/games/maa-operator/SKILL.md` | 约束 LLM 只能走 `maa_action.py` 的护栏 | gateway 按频道绑定加载 | — |
| `tests/test_maa.py` + `tests/test_copilot.py` | stdlib 单测，不碰模拟器 | 人工 | 约 10s |

---

## 3. 链路一：两张计划卡（`maa_plan_card.py`，京 07:50 早场 / 21:50 晚场）

**一天两张卡，一张管一场**（四期 D1）。场次的来源与日常一样是
**workdir 名**（`~/.hermes/state/maa/{am,pm}`），也接受 argv；两者都没有时按小时兜底
（`select_phase()`，`< 14 点` → `am`）。Hermes 的 `--script` 不支持 argv，所以 cron 用 workdir。

`main(argv)`（`scripts/maa_plan_card.py`）严格按这个顺序：

1. **`check_and_update_upstream()` — 只有早场卡跑**：`brew outdated/upgrade` 跟随
   maa-cli，再依次用 `maa update --batch` 和 `maa hot-update --batch` 更新 Beta
   MaaCore 与 MaaResource 热更新层（超时分别 180s / 300s）。
   **这条必须保留本机代理**（`maa_subprocess_env(..., keep_proxy=True)`），否则直连
   GitHub 会超时。有新版本就装上，把版本写回 `config.json`，摘要进早场卡「上游更新」
   字段。失败发红色 Embed 告警，并在 `main()` 末尾 `raise` 让 cron 记为失败。
   **晚场卡整步跳过**（`update is None`）：早上刚跟过，21:50 再花几分钟等网络只会
   让晚场卡迟到；`main()` 末尾那个 `raise` 也因此要判 `update is not None`。
2. **`refresh_activity_stages()`** — 两张卡都跑 `maa activity Official`，解析出形如 `TO-9`
   的活动关列表写回 `config.json`；同时检测公告里的「资源收集」字样，命中则置
   `all_supply_stages_open=true`（全部资源本开放）。
3. **重新 `load_config()`** — 上一步刚改过磁盘上的 config，卡片必须从落盘后的真值渲染。
4. **算本场默认关** — `current_farming_result(effective_plan(phase=phase), config)` →
   `apply_auto_stage(farming, phase)`，写的是 `plan.json` 的 `sessions.<phase>`。
5. **`build_components(plan, config, phase)`** — **恰好两行**：一个关卡下拉 + 一个
   「跳过本场 / 恢复本场」按钮，同时产出登记表条目。
6. **写按钮登记（关键）** — 在 `state_lock()` 里 **先删所有 `hermes:btn:maa:` 前缀的 key，
   再 merge 写回** `~/.hermes/state/agent-book/buttons.json`。这个文件是**全局共享**的，
   agent-book 的按钮也在里面，**绝不能整体覆盖**。删前缀的副作用是旧卡片上的
   MAA 控件立即失效（点了会报未登记）——21:50 那趟顺手废掉早场卡的控件，是**预期行为**。
7. `cleanup_old_threads()` 删 >24h 的子区、`cleanup_run_files()` 删 >14 天的 `runs/` 产物。
8. `retire_old_plan_cards()` — 扫最近 100 条消息，把标题**以 `🌱 MAA 今日计划` 开头**
   （现在带 `· 早场 08:00` / `· 晚场 22:00` 后缀）且仍带 components 的旧卡片
   `PATCH components: []`，避免同时存在多张可点的卡。
9. 发新卡片。

### 卡片上有什么

| 字段 | 内容 |
|---|---|
| 刷什么 | 本场的关 + 归属（`（按材料缺口自动选）` / `（你手动选的）`，来自 `today_stage_source(phase)`） |
| 为什么 | 缺口理由或降级说明（原来的「📦 材料缺口」字段） |
| 本场跳过 | 是 / 否，并写明另一场与手动开跑不受影响 |
| 当前活动关 | `config.activity_stages` |
| 活动截止 | `activity_deadline_text()` 读 MaaCore 的 `StageActivityV2.json`。**读不出来整行不显示** |
| 🧪 理智规则 / 每日剿灭 | 不变 |
| 上游更新 | **只有早场卡有** |

`activity_deadlines()` 读 `~/Library/Caches/com.loong.maa/StageActivityV2.json` 的
`Official.sideStoryStage.<前缀>.Activity.{Tip,UtcExpireTime,TimeZone}`。字段名带 `Utc`
但**实际是 `TimeZone` 那个时区的本地时间**（maa-cli 源码注释亦如此），所以要按 `TimeZone`
构造 tzinfo 再换算成北京时间。文件不存在 / JSON 坏 / 字段缺失**一律返回空表、绝不抛**——
旁路信息不该带走整张卡（D-2 的教训）。路径可用 `config.maa_activity_cache` 覆盖（单测用）。

### 抄作业卡（`copilot card`，四期 1c）

渲染在 `copilot_card_view()`（同文件），派发在 `maa_action.copilot_card()`：

```
聊天「抄作业 / 有什么关能抄」
  → maa_action.py copilot card      只读本地，一次 Discord POST，同步做完（<20s）
      ├─ copilot_card_stages()：activity_stages（无活动 → 当天资源本 + 1-7）
      │    × stage_drops() 主掉落 × maa_clearing.nav_checker() 导航键 × 本地关卡索引
      ├─ 一览列全部候选（每关标 ✅已通 / 🔁突袭待补 / ⏳排程 / 🚫练度不满足 / ⛔无导航）
      ├─ 收旧卡：retire_discord_components(上一张 message_id) → 删旧登记
      ├─ state_lock() 下 merge 写 hermes:btn:maa-copilot:card:stage-select
      └─ 发卡 → 把新 message_id 写进 copilot/card.json（0600）
  选中一关 → select_args → ["copilot","find",<关卡>] → 搜索 worker → **选中即执行**（§5.5）
```

**第四个按钮前缀 `hermes:btn:maa-copilot:card:`**：它在 `hermes:btn:maa-copilot:` 之下，
所以计划卡那趟 `hermes:btn:maa:` 的整段清扫碰不到；又因为 `card` 不是 12 位 hex
（`SEARCH_TOKEN_RE`），`prune_expired_searches()` 也永远轮不到它——那个函数只按
`searches/<token>.json` 里真实存在的 token 清。**`card.json` 因此绝不能放进 `searches/`**，
放进去会被当成一次过期搜索连文件带登记一起删掉。

### 关卡下拉是怎么构成的

`_stage_choices()` 顺序拼接，上限 25 项：

- `上次作战关卡`（空串 → MAA 自动沿用游戏内上次关卡）
- `1-7`
- **当天开放的资源本** — `current_supply_choices()` 用 `(now - 4h).weekday()` 算「游戏日」
  （国服 04:00 刷新），按 `SUPPLY_STAGES` 的星期表过滤；`all_supply_stages_open` 为真时全给。
- `config.activity_stages` 里的活动关
- 若当前计划关卡不在以上任何一项里，插到第 2 位

### 下拉值不是关卡名（安全设计）

选项的 `value` 是**不透明的 `choice:N`**，真正的 argv 存在登记项的 `select_args` 里：

```jsonc
"hermes:btn:maa:am:stage-select": {
  "kind": "exec", "script": "maa_action.py",
  "select_args": { "choice:0": ["plan","set-stage","","am"], "choice:1": ["plan","set-stage","1-7","am"], … }
}
```

adapter 补丁只做「精确查表」，Discord 传回的值**永远不会直接变成命令行参数**；未知值、
多值一律拒。改下拉相关代码时必须保持这个性质（对应测试
`test_adapter_patch_dispatches_only_registered_select_values`）。

**场次也走同一条路**：`am` / `pm` 由登记表写死在 argv 末尾，Discord 侧注入不了场次。
custom_id 里也带场次（`hermes:btn:maa:{phase}:stage-select` / `:skip` / `:unskip`）——
万一 `retire_old_plan_cards()` 没收干净旧卡（它是尽力而为的），点旧卡只会得到
「未登记」，而不会把早场的选择悄悄写进晚场。

### 材料缺口 → 自动定关（四期步骤 3，2026-09-09 改版）

**这里曾经是第三个下拉（`hermes:btn:maa:farming-select`），已删除。** 删的理由：
材料缺口天天变，而下拉只能选关、显示不了缺口，语义和第一个下拉完全重复。
缺口应该是**后台固定计算**的结果，不是让人去挑的选项。

算法整个在 **`scripts/maa_farming.py`**（纯函数 + 抓取层分开），`maa_plan_card`
只剩三个薄封装（`farming_recommendations` / `auto_stage` / `farming_note`）负责渲染。

```
一图流 matrix.json（ETag）─┐
一图流 stage/info（1h）   ─┼─ load_market_data() ── state/maa/farming/*.json 缓存
一图流 item/v7/value（24h）┤     （企鹅 result/matrix / zones+stages 作后备）
一图流 store/activity（可选）┘                     │
                                                  ▼
stages.json + item_index.json ── stage_drops() ── material_stage_candidates()
                                                  │        （手工表在前，不变量 #38）
depot.json ── consumption_rates_window(≥4 周) ────┤
                                                  ▼
StageActivityV2 + 导航键 ── candidate_stages() ── farming_result()
                              （D5：活动期只有活动关）      │
                                                  ┌───────┴────────┐
                                          auto_stage()        drops_cap() / fallback_stage()
                                                  │                │
                                    apply_auto_stage() 写 sessions.<phase> 的
                                    stage / stage_source=auto / farming_drops /
                                    farming_fallback_stage
```

**候选关集合（D5）**——`maa_farming.candidate_stages()`：

| 情况 | 候选集 |
|---|---|
| 活动进行中（`StageActivityV2` 的截止时刻未过；读不到日历时退而看 `activity_stages` 非空） | **只有活动关**：`config.activity_stages ∩ 上游导航键`（`maa_clearing.nav_checker()`，与自动抄作业同一个闸门） |
| 活动已结束 / 无活动 | `1-7` + 当天开放的资源本（`current_supply_choices()`） |
| 活动期但一个带导航键的活动关都没有 | 退回上一行，并在卡片上写明——否则整张卡什么都推荐不了 |

判「活动是否还在」**优先看日历而不是 `activity_stages`**：后者是运行时缓存，
活动结束当天它可能还留着上一期的关。

**三项加权分**（权重 `config.farming_weights`，默认 `0.5 / 0.3 / 0.2`，**和为任意值都归一**）：

| 项 | 口径 | 数据 |
|---|---|---|
| ① 稀缺 | 「当前开放关最便宜 `best_now`」vs「常驻关最便宜 `best_perm`」，`best_perm / best_now ≥ farming_activity_edge`（1.3）才算活动关明显更优；下次可刷日 = `首发 start + farming_rerun_days`（378，42 组 `ACT→ACT_REP` 的中位数），已复刻（`ACT_REP`）视为不会再来按 `end + 2 年`；分值 = `clamp(剩余天数 / farming_scarcity_horizon_days, 0, 1)`。**只管「窗口要关了」这个时间维度**，平均用量归第 ② 项 | `stage/info` + `matrix.json` |
| ② 缺口 | 缺口件数 = `farming_target_days`（30）天用量 − 库存估算；缺口价值 = 件数 × `itemValue`（把稀有度拉到同一理智量纲：1 个晶体电子单元 ≈ 302 理智 ≈ 17.6 个固源岩组）；分值 = 缺口价值 / 本场可用理智，clamp 到 1 | Depot + `item/v7/value` |
| ③ 效率 | 候选集内该材料最优「件/理智」÷ 全游戏历史最优「件/理智」 | `matrix.json` + `apCost` |

掉率两条过滤，抄一图流前端口径：`times < farming_min_samples`（300）不计；
matrix 行 `end` 非空且早于 now 不计（**活动关统计只在开放期有效**）。
`ap_per_item = apCost / (quantity / times)`。

**几个只能踩一次的坑**（都写成了单测）：

- `stage/info` 的 `end` **永不为 null**，常驻用哨兵 `4070880000000`。判常驻只能靠
  `stageType ∈ {MAIN, ACT_PERM}` 或「end 在 10 年之后」，**不许判 null**。
  企鹅 `/zones` 相反，`closeTime: null` 才是常驻——后备转换时统一成哨兵。
- `item/v7/value` 129 条里 **16 条 `itemValue == null`**（活动代币、理智小样），
  不判空会 `float * None` 直接崩；它们不参与缺口计价。
- `matrix.json` 的 `Cache-Control: max-age=7200` 是骗人的（实测约 10 分钟刷一次），
  要新数据必须用 **ETag 条件请求**；一图流两个后端接口干脆没有任何缓存头，TTL 自己定。

**降级路径**（`degraded_reasons`，全部**不抛**、全部在卡片上可见）：

| 情况 | 处理 |
|---|---|
| 抓取失败 | 用本地缓存；缓存也没有就当空数据（稀缺退化成「活动关独有主掉落 = 1」，效率全 0），卡片列出原因 |
| 冷启动（`consumption_history` 不足 2 次对表） | 缺口项退 `farming_manual_weights` 手工权重，**仍然自动定关**（见改写后的不变量 #31），但**不写 drops 上限** |
| ≥4 周窗口内零消耗 | 可用量是**未知**不是 0 缺口 → 同样退手工权重 |
| 上游掉落表与手工表都查不到关卡 | 不推荐、卡片列出来。**永不猜关** |

排序同分时先比「理智/件」再比材料名，保证**确定性**——活动首日几个材料三项常常同分。

`stage_source` 仍然是「这关是谁定的」的唯一依据，必须留在
`maa_common.PLAN_SESSION_FIELDS` 白名单里；写它的只有 `apply_auto_stage()`（`auto`）
与 `maa_action.set_stage()`（`discord`）两处。`farming_drops` / `farming_fallback_stage`
是同一批新字段，`maa_action.set_stage()` 手动选关时会把它们**清空**。

现在所有登记项都是 `kind: "exec"`；`kind: "prompt"` 那条路径在 MAA 这边已经没有使用者。

---

## 3.5 链路一半：活动上线自动抄作业（`maa_activity_watch.py`）

四期步骤 2 把三期的「确认卡 + 通关队列」换成**静默执行 + 事后汇报**（D6 / D11）。
watcher 每 15 分钟一 tick，`activity_watch.lock` 防止两次 cron 重叠。

```
maa activity Official ── activity_snapshot() ── activity_target() ── key = "标题|前缀"
   │
   ├─ 没活动 → 连续 2 次空快照才 idle（不变量 #20），作废未打完的一轮并发通知
   ├─ key 变了 → 归档旧记账 → 新开账本 → 清 stage_state → 发「🆕 已上线」
   │
   ├─ 已有 batch_token
   │    executing → 记 running；queued → 20 分钟失联则放回 scheduled；
   │    scheduled → 查派发闸门，通过就 `run-batch`
   │    终态       → 清掉 token，按最新记账重排下一轮
   │
   └─ 排新一轮
        collect_targets()  索引里前缀相同的全部关 ∩ 上游导航键
                           − 已通关(记账 ∪ zone 全清) − 教学关 − zone 未开
                           − 当日已打满(daily_cap) − 退避中
        build_round()      plan_round(条目, 预算, 理智查表) → 现搜作业（≤6 关/tick）
                           排得满 → 现在打；排不满 → 只排第一条 + `sanity_preflight().ready_at`
        写 searches/<token>.json（status=scheduled）→ publish_round() 发「已排程」卡
        dispatch_blocker()  到点？blackout？Depot 窗口？时间预算？run.lock？模拟器在线？
```

**判定「空窗」还是「格式变了」只有一个判据**：`is_side_story_heading()`，闸门与
`parse_activity_events()` 共用它，两边不可能对「什么算分区标题」有分歧。三种结局：

| 输出 | 结论 |
|---|---|
| 有活动关分区标题行 | 正常解析 |
| 没有标题行，但有解析器认识的其它 `Opening …` 分区 | **查询成功的活动空窗** → 空快照 |
| 标题文本出现了、却不是完整一行（被装饰/缩进/换行） | **格式已变** → 抛异常 |
| 连一个认识的分区都没有 | 同上，抛异常 |

**目标集合的六种排除理由语义不同**（`collect_targets()` 的 `excluded`，卡片分开显示）：

| 理由 | 含义 | 会不会自己变好 |
|---|---|---|
| `no_nav` | 上游 `tasks/Stages/<前缀>.json` 里没这关的键（D8 / 不变量 #37） | **不会**，热更新带来才会 |
| `not_indexed` | 有导航键但本地 Tile-Pos 索引里没有 | 热更新后会 |
| `zone_unopened` | 森空岛该活动所有 zone `totalStage == 0`（D9） | 会，**不记失败** |
| `cleared` | 记账齐全 **或** zone 全清（D10） | — |
| `daily_cap` | 今天这一 `(关, 模式)` 失败 / `unknown` 已达 `auto_copilot_max_attempts_per_day` | 会，**次日归零** |
| `backoff` | 搜过、练度不满足 / 暂无作业，还没到 `activity_search_retry_minutes` | 练度或上游变了就会 |

`backoff` 与 `daily_cap` 是两把不同的尺子：前者管「隔多久再试」，后者管「一天试几次」。
只有前者时会出现 2026-09-09 那样的循环——记账全是 `unknown`，30 分钟一到又重打同一批关。

**目标集合的顺序**：数字关升序 → EX 升序 → S 升序，每关先 `normal` 后 `raid`
（`maa_copilot.activity_stage_codes()` + `maa_clearing.required_modes()`）。
认不出形状的关排在最后，**不静默丢弃**。

**森空岛刷新频率**：只在「集合里有未执行的关」的那个 tick 刷一次（练度与 zone 进度
决定这一轮排什么），其余 tick 用缓存 + 6 分钟/点估算（`estimated_sanity()`）；
真正开打前 worker 还会**每关**强制刷新一次读实时理智。

**状态**：`activity_watch.json` 是面向轮换的粗状态——`idle`（无活动）、`searching`
（刚换活动）、`waiting`（这一轮排不出条目）、`done`（没有未打的关了）、`scheduled`
（一轮已排、闸门没过）、`dispatched`（已派 worker）、`running`（worker 在打）。
一轮的细状态在 `searches/<token>.json`：`scheduled → queued → executing →
executed / execution_failed`，另有 `canceled` / `superseded`。
`queued` 超过 20 分钟且 `run.lock` 已空闲 = worker 在碰设备前失联，放回 `scheduled`；
`executing` 起绝不自动重排——**没法证明理智还没被花掉**。

关卡索引同时扫描 MaaCore 自带 resource 与 `MaaResource/resource` 热更新覆盖层，基础资源先读、
覆盖层后读；否则活动已经被 `maa activity` 看见、导航图却还只在覆盖层时会永远匹配失败。

---

## 4. 链路二：执行一场日常（`maa_daily.py`）

`execute(phase)`（`scripts/maa_daily.py`）：

1. **定场次** — `select_phase()` 优先级：argv (`am|pm|now`) > **当前工作目录名**
   > 按北京时间的小时猜。cron 用 workdir 传场次，是因为 Hermes 的 `--script` 不支持 argv：

   ```
   --workdir ~/.hermes/state/maa/am  →  phase=am
   --workdir ~/.hermes/state/maa/pm  →  phase=pm
   ```

2. **抢锁** — `run_lock(blocking=False)`。抢不到时不碰当前持锁任务的顶层状态，只追加
   `last_lock_conflict_*`，发 Discord 红色告警并返回失败；不得把整场日常静默吃掉。
3. 清理 >14 天产物；把**本场实际使用的计划**快照到 `runs/<date>-<phase>.json`；
   `last_run.json` 置 `status=running` 并把上一场的终态字段清空（避免运行中读到旧结果）。
   > 快照是「跑起来之后再改计划不影响本场」的实现基础——后续对 `plan.json` 的写入
   > 既改不了内存里这个 dict，也改不了这个文件。
4. `plan.skip` 为真**且 `phase != "now"`** → 发一条灰色「已跳过」Embed，`status=skipped`，
   结束。**不开模拟器。** skip 现在是**按场次**的（四期 D2）：它来自
   `sessions.<phase>`，关掉早场不影响晚场；手动场是用户当场下的明确指令，
   本身就是对今天这条计划的推翻，照跑（`run_now()` 的回执会列出哪几场设了跳过）。
4.5. **探模拟器（只对 `am`/`pm`）** — `maa_common.emulator_is_online()`：
   `pgrep -f BlueStacks` **或** `adb connect` + `getprop sys.boot_completed == 1`，
   任一命中即算「用户正在玩」，`report_emulator_busy()` 发灰色「已跳过」Embed、写
   `status=skipped` / `skip_reason=emulator_busy`，结束。**不开模拟器、不抢设备。**
   起因是 `PLAN_NAV_GATE.md` 结论 3：任何失败路径都会 `close_everything()` 把 BlueStacks
   关掉，等于把正在玩的用户踢出游戏。`now` 不走这条闸门（用户明确要求照跑）。
   探测预算 5s、失败按离线处理照常执行——**宁可误抢一次设备，也不能因为探测自己坏了
   就永久停摆**（不变量 #24）。
   **实现只有一份**，在 `maa_common.py`：日常、抄作业 `run-batch`、Depot 三条自动
   链路共用它（四期 D18）。`maa_daily.report_emulator_busy()` 只是把日常的文案与
   `last_run` 字段套进 `maa_common.report_emulator_busy()` 的薄封装。
   > 别和第 8 步的 `settle_android_runtime()` 搞混：那个在**开机之后**跑，等 `app_process`
   > 别抖；这个在**开机之前**跑，问的是"设备现在是不是别人的"。
5. **预检 → 起模拟器** — `require_executable` 校验 maa/adb 可执行（**在开模拟器之前**）→
   `open -a BlueStacks` → 轮询 `adb connect` + `getprop sys.boot_completed`（超时 180s）→
   `pm path com.hypergryph.arknights` 确认国服客户端已装。任一步失败且此时还没建子区，
   走 `send_direct_failure()` 直接在频道发红色告警。
6. **发开跑消息 → 建子区 → 发进度占位消息**，thread_id 记进 `last_run.threads`（供 24h 清理）。
7. **渲染 `daily.toml`** — `render_daily_toml(plan, config)` 把本场快照渲染成 TOML 并
   `atomic_write_text` 覆盖 `~/.config/maa-cli/tasks/daily.toml`。任务链固定：

   `StartUp → [当日第一场 Annihilation] → Fight(缺口第一名) → [Fight(第二名)] → Infrast → Recruit → Mall → Award → Custom(Home)`

   **刷理智是两个 Fight 块**（四期步骤 3b），名字写死在 `maa_daily.FARMING_TASK_NAMES`：

   | 块 | 名字 | 关 | `drops` |
   |---|---|---|---|
   | 第一个 | `刷理智` | `plan["stage"]`（缺口第一名） | `plan["farming_drops"]` = `{itemId: 还差多少个}`，**刷够即停**（上游 `-D` 的协议字段） |
   | 第二个 | `刷理智·续` | `plan["farming_fallback_stage"]`（缺口第二名） | **没有**，打到理智不足为止 |

   **三个 Fight 块（剿灭 + 两个刷理智）都走 `_fight_block()`**，所以掉落上报也只写一次：

   ```toml
   report_to_penguin = true    # 由 config.report_drops 总控，默认 true
   penguin_id = ""             # 空 = 匿名，任何情况下不带账号标识
   report_to_yituliu = true
   yituliu_id = ""
   ```

   上报的对象正是材料推荐吃的那两份公开掉落数据（四期步骤 3）。`report_drops = false`
   时这四行一行都不写——`DropReportTests` 里有一条「除了这四行两份渲染完全一致」的断言。

   第二个块**只在第一个块设了 `drops` 时才渲染**：没上限的话第一个块本来就会打到理智
   不足，第二块毫无意义。冷启动、缺口 ≤ 0、或用户手动选过关时 `farming_drops` 是空的，
   于是退回「一个块打到底」的老形态。两个块共用 `_fight_block()`，所以
   `medicine = 0 / stone = 0 / series = 0` 这三个字面量只写一次（不变量 #1~#3）。
   理智让路（`plan["farming"] = False`）一次摘掉两个块。

   战报里第二块的 `TASK_STEPS` key 是 **`Fight2` 而不是 `Fight`**：`_task_state()`
   认不出命名段时会退回 `"<key> Completed"` 这条通配，两个块共用 `Fight`
   会让根本没跑过的第二块被判成「完成」。掉落回写库存时用 `farming_sections()`
   把两段合起来喂 `_drop_pairs()`。

   `StartUp` 本身已经复用 MaaCore 的首登处理链：登录后会处理签到/今日配给等弹窗，
   不是另起一条自定义点击任务。剿灭复用第二个 `Fight(stage = "Annihilation")`：
   每个国服游戏日（04:00 刷新）只进一次，由 `last_run.annihilation_game_day` 记帐；
   MaaCore 从右上角待办进入当期剿灭，自动点「全权委托」消耗代理指挥卡，每次结算
   OCR 周进度，满 1800 合成玉就停。两个 Fight 都硬编码 `stone = 0`。默认理智关
   是 `1-7`，当天仍可通过计划卡临时覆盖。

   `Award` 固定启用 `award = true` 与 `mail = true`，每场都会尝试领取日常、周常和
   邮箱内全部可领取附件；MAA 在没有可领取内容时正常结束。另外三项从配置读（四期步骤 5）：

   | 参数 | 配置键 | 默认 | 含义 |
   |---|---|---|---|
   | `orundum` | `award_orundum` | `false` | 幸运墙每日合成玉 |
   | `mining` | `award_mining` | `false` | 限时开采许可（采矿）合成玉 |
   | `specialaccess` | `award_specialaccess` | `false` | 周年特权（月卡）奖励 |

   `Mall` 的 `visit_friends` / `shopping` 固定开、`credit_fight` 固定关（信用作战要借助战，
   不变量 #15 的精神），其余四项从配置读：

   | 参数 | 配置键 | 默认 | 含义 |
   |---|---|---|---|
   | `buy_first` | `mall_buy_first` | `[]` | 优先购买的物品名 |
   | `blacklist` | `mall_blacklist` | `[]` | 不买的物品名 |
   | `reserve_max_credit` | `mall_reserve_max_credit` | `false` | 信用点低于 300 停止购买 |
   | `only_buy_discount` | `mall_only_buy_discount` | `false` | 只买打折物品 |

   两个列表要过 `_mall_item_list()` 的 `SAFE_MALL_ITEM_RE` 白名单（中日文 / 拉丁字母 /
   数字 / `·`，≤20 字），**不合规的项静默丢弃、不抛异常**：它们会原样变成 TOML 字符串，
   一个带引号或换行的物品名既能撬开任务文件也能让整场日常渲染不出来（不变量 #51）。
   布尔键走 `_toml_bool()`，**只有真正的 `true` 才是 `true`**（写歪了一律按 false）。

   最后那个 `Custom` 的 `task_names` **每一项都必须带 `Home@` 前缀**（见
   `maa_daily.HOME_TASK_NAMES`）。MAA 的 `QuickSwitch` 是子程序，点完小房子里的首页图标
   之后只能靠 `next` 里的 `#back` 回到调用方来判定成功，而 `#back` 取的是任务名**第一个
   `@` 之前**的前缀。裸写 `"QuickSwitch@ToHome"` 时前缀是并不存在的 `QuickSwitch`，
   `#back` 被静默丢弃（asst.log 里 `to_be_recognized` 只剩 4 项，资源 json 里是 5 项），
   于是画面明明已经回到主页，却没有任何任务能判定成功，空转到 20 次重试耗尽报
   `Custom Error`——2026-08-02、08-03 两个早场都是这样红的。加前缀后 `#back` → `Home`
   （`action = Stop`），认出主页立即成功收尾。列表是 MAA 自己那套
   `["Home@QuickSwitch@ToHome#next", "Home@ReturnButtons#next"]` 手工展开的：顶层
   `task_names` 原样交给 `ProcessTask`，不走 `next` 列表那套 `#` 展开。

   其中 `medicine = 0`、`stone = 0`、`series = 0` 是**硬编码字面量**，不从任何状态派生；
   `Infrast` 固定 `mode = 0`，让 MaaCore 从游戏内实时识别的干员、基建技能和心情计算
   七类设施的排班；不使用无人机、不补源石碎片订单、宿舍不刷信赖。**链里没有 CloseDown**——
   它被拆出去在截图之后单独跑。`profiles/default.toml` 开启 maa-cli 用户资源层，
   `resource/tasks/tasks.json` 完整覆盖 `InfrastDormDoubleConfirmButton`：用现成
   `PopupConfirm.png` 识别国服当前红色确认条，并点击右侧对勾。

   **这一条目前没有 run 级实测数据，别当它已验证。** MaaCore 在**进程启动时一次性载入
   `resource/`**，所以要判断某场用的是哪一版，看的是那场**启动时刻**磁盘上的内容，不是
   基建段开始时的。2026-09-01 三场的实际情况：08:00 与 22:00 都是在极简覆盖
   （`roi = [630,430,650,290]`，其余继承上游）下 `Infrast Completed`——22:00 那场虽然
   22:05 磁盘已被换成完整覆盖，但它 22:00:49 就载入完了；23:01 那场读到的仍是极简覆盖，
   `Infrast Error`，0 次确认。完整覆盖是 23:09 才落定的，此后还没跑过一场。

   即：**同一份极简覆盖两成一败，宿舍确认这步会飘**。而且 23:01 那场是当天第二次基建
   （22:15 刚排完班，宿舍本就没有"撤下干员"弹窗），其后 Recruit/Mall/Award/Custom 全部
   在 13~30 秒内 Error，更像界面卡死而非确认条点不掉——归因还没坐实。再遇到卡确认条，
   **先抓卡住时的截图比对模板与实际位置，不要靠改 `roi` 猜**。
8. **`run_maa()`** — `maa run daily -v --batch`，注入 `MAA_CONFIG_DIR` /
   `MAA_LOG_PREFIX=Never` /（可选）剔除代理变量。两个 reader 线程把 stdout+stderr
   同时 tee 到 `runs/<date>-<phase>.log`；主循环每 5 秒把**解析后的中文状态** PATCH 进
   子区那条消息，内容无变化就不发，Discord 异常一律吞掉（流式是尽力而为，不能拖垮主流程）。
   - 超时 `maa_run_timeout`（默认 3000s = 50 分钟）→ terminate/kill，返回码记 **124**。
     外层 Hermes cron 的上限是 3600s，这 10 分钟余量留给截图与清理。
   - `KeyboardInterrupt` → 返回码 **130**。
9. **截图 + 主页校验**（`capture_home_screenshot()`）— 等 `home_screenshot_delay` 秒让动画
   稳定 → `adb exec-out screencap -p` → 校验 PNG magic 后 chmod 600 → 用本机 Vision OCR
   （`~/.hermes/scripts/ocrbox`）核对画面里有没有 `HOME_MARKERS`（终端/编队/采购中心/
   公开招募/基建/好友/档案/SANITY），命中 ≥ `HOME_MARKER_MIN`（4）才算真在主页；
   不在就重截，最多 `home_screenshot_attempts`（默认 3）次。
   - 截图**失败不阻断**，只在战报里标一句。
   - 校验**判定为 False**（确实不在主页）会让整场判红，并在战报备注栏写明命中数。
   - 校验**无法判定**（ocrbox 没部署 / OCR 崩了）记 `None`，不改变结论，只写进
     `last_run.home_verify_detail`——宁可漏报也不能凭一个不确定的信号误杀好场次。
   - 这条闸门只挂在日常链上。抄作业结束在战斗结算界面而不是主页，`maa_copilot_worker`
     仍然直接调 `capture_screenshot()`，不受影响。
10. **`close_everything()`**（在 `finally` 里）— `maa closedown Official` →
    `osascript quit`（bundle id）→ sleep `quit_grace_seconds` → `pkill -f BlueStacks` 兜底 →
    `adb disconnect` → 用 `[B]lueStacks` 这种加方括号的模式 `pgrep` 复核残留（防止匹配到自己）。
11. **判定与上报** — `ok = (return_code == 0 and not cleanup_errors and home_verified is not False)`。最后编辑一次子区消息
    （✅/❌ + 总耗时），然后往频道发战报 Embed + `attachment://shot.png` 单条多段消息；
    多段上传失败时降级成「无图 Embed + `hermes send MEDIA:`」两条。最后写 `last_run.json`。

### CLI 输出 → 中文的转换

`progress_content()` 和 `extract_summary()` 是两个**纯函数**：输入 CLI 行数组，
输出 Discord 文本。**原始 CLI 文本从不进 Discord**，只落 `runs/*.log`。
它们靠 `TASK_STEPS` 表 + 一组正则识别任务状态、理智、连战次数、临期药、掉落、
设施进入次数、公招次数。maa-cli / MaaCore 升级后输出格式变了，通常只需要改这两个函数
和它们的正则，可用 `test_progress_and_summary_translate_cli_events` 直接覆盖验证。

---

## 5. 链路三：Discord 交互（`maa_action.py`）

三种入口最终都收敛到同一个脚本：

```
按钮/下拉 ─ adapter 查 buttons.json ─┐
                                     ├─→ python3 ~/.hermes/scripts/maa_action.py <argv>
自然语言 ─ maa-operator 技能 ─ terminal ┘        └─ stdout: {"ok":bool,"message":str}
```

**三条来自 Hermes adapter 源码的硬约束**（不是文档写的，是读源码得到的）：

| 约束 | 后果 |
|---|---|
| exec 脚本硬超时 **20 秒** | `maa_action.py` 只能写状态或 `Popen(start_new_session=True)` 派发后立即返回。任何同步等待 MAA 的写法都会被杀。 |
| exec 白名单是**精确文件名**集合 | 必须由 `deploy.py` 显式加入 `maa_action.py`，不能用通配。 |
| `buttons.json` 路径写死且全局共享 | 必须 merge，必须用 agent-book 那把 `state.lock`。 |

子命令（`dispatch()`，`scripts/maa_action.py`）：

| 命令 | 行为 |
|---|---|
| `plan set-stage <关卡> <am\|pm>` | 白名单校验（关卡 + 场次）后写**那一场**的今日覆盖 |
| `plan skip-today on\|off <am\|pm>` | 写**那一场**的今日覆盖 |
| `plan show` | 只读，一次念完两场（关、来源、跳不跳） |
| `run now` | **不开跑**。抢锁探测 → 拒绝窗（`manual_run_blackout_minutes`，默认 30）→ 预检 → 作废上一张 pending 选关卡 → 发「🎯 手动场选关」卡（选项 = `maa_plan_card._stage_choices()`，无 default）→ 写 `manual_run.json`（`pending`，10 分钟 TTL）→ `Popen` 过期哨兵 + 推荐补写后返回（`PLAN_MANUAL_RUN.md`） |
| `run start <关卡> <token>` | **只由选关卡登记表触发**。token 匹配且 `pending` 且未过期 → 关卡白名单 → 锁 → 拒绝窗 → 预检 → 状态改 `dispatched` → 删登记 → 收卡 → `Popen maa_daily.py now <token>`。过期就地收卡（硬闸门） |
| `run expire <token> [--after 秒]` | 过期哨兵（`run now` 派的脱离会话子进程）：睡够后若仍 `pending` 且到期 → `expired` + 删登记 + PATCH 收卡；幂等 |
| `run recommend <token>` | 推荐补写（同上，脱离会话）：`current_farming_result` → `auto_stage`，仍 `pending` 才 PATCH 卡片写「缺口推荐」并给该选项打 🎯；算不出写「推荐暂不可用」 |
| `log tail` | 读 `last_run.summary`；若是旧版格式则回读 log 用 `extract_summary` 重建 |
| `copilot card` | 发抄作业一览卡（§3）。只读本地 + 一次 Discord POST，同步做完 |
| `copilot find <关卡>` | 写一份 `pending` 搜索并 `Popen` 派 `search` worker（§5.5） |
| `copilot status <token>` | 只读这一轮的状态 |
| `copilot cancel <token>` | 只在 `scheduled` / `queued` 可取消；开跑后按钮已被收起 |
| `roguelike card` | 发肉鸽配置卡（§5.7）。只读本地 + 一次 Discord POST，同步做完 |
| `roguelike set <theme\|mode\|difficulty\|squad> <值>` | 只写 `roguelike.json.pending`，**不派发**；`theme` 额外重发一张卡 |
| `roguelike start` | 过 `dispatch_blocker()` → 冻结 `params` → `Popen(roguelike worker run <token>)` |
| `roguelike stop` / `abort` | 只写 `stop_requested_at` / `abort_requested_at`（<20s），真正收手在 worker |
| `roguelike status` | 只读汇报本轮参数与逐局结果 |

`maa_action.py` 还导出两个**给别的进程用**的函数（它是唯一的派发入口）：
`publish_round()`（发「已排程」卡 + 登记它唯一的取消按钮）与 `dispatch_batch(token)`
（`Popen(worker run-batch <token>)`）。手动路径（搜索 worker）与自动路径（watcher）
共用这两个，执行链因此只有一条（D14）。

**场次参数不可省**（`dispatch()` 按 argv 长度精确匹配），且只接受 `am` / `pm`；
`now` 不是可写的场次——手动场按定义不吃任何覆盖（D4）。

场次白名单 `valid_phase()`：只有 `am` / `pm`。关卡白名单 `valid_stage()`：空串 ✔ / `^[A-Za-z0-9]{1,8}-\d{1,2}$` ✔ /
在 `config.activity_stages` 里 ✔ / 否则**现场跑 `maa activity Official` 并按词边界精确匹配**
（超时只给 **0.7s**，因为整体预算是 20s）。其余一律拒。

写状态时的两个细节：
- `_today_override()` 返回 `{date, sessions: {am, pm}}`，每个场次只保留
  `maa_common.PLAN_SESSION_FIELDS`（`stage/stage_source/skip/source/updated_at`）里的键，
  非法场次名与白名单外的字段一律丢弃；日期不是今天、或磁盘上还是**旧的按天格式**
  （顶层 `stage`/`skip`）就整个重置成空。所以 `plan.json` 天然「今天说的话明天失效」，
  不需要清理任务，**也不需要为旧格式写迁移**——它一天后自然失效。
- `is_run_locked()` 决定确认话术：正在跑就明确回「不打断本场，改的是哪一场」。

`emit()` 把输出压在 7900 字节内——组件运行器只接受 ≤8192 字节的 JSON。

### 技能这一层为什么必要

`SKILL.md` 是给 LLM 的护栏：禁止 `write_file` 直接改状态文件、禁止调 `maa_daily.py`、
碎石永久关闭、原样转述脚本返回的 `message`。**但它只是第一道防线**——即使 LLM 完全无视技能，
`maa_action.py` 的白名单和 `render_daily_toml` 里的硬编码字面量仍然拦得住。
写代码时请始终假设「提示词会失效」。

---

## 5.5 链路四：抄作业（`maa_copilot.py` + `maa_copilot_worker.py`）

**一条执行链，两个入口**（四期 D6 / D14）。与日常刷理智共用 `run.lock`，且 08:00/22:00 前
`copilot_blackout_minutes`（默认 25）内不派发。两个入口共用**同一份**闸门
`maa_action.dispatch_blocker()`（不变量 #13）。

**手动路径被闸门挡下时不排程**：blackout / Depot 窗口 / 时间预算（以及 `run.lock`、
模拟器在线）任意一条命中，`copilot find` 当场返回拒绝——不写 `searches/<token>.json`、
不派 worker。已经搜完才跨进 blackout 的那一轮停在 `scheduled` 并在卡上写明
「本轮未启动：<原因>」；**手动轮不自动重试**（watcher 只跟踪自己的 `batch_token`），
用户日常结束后重新选关即可，旧轮点 ❌ 收起或等 `prune_expired_searches()` 按 TTL 清掉。

```
自动（§3.5 的 watcher）                手动（抄作业卡下拉 / 聊天「抄 SR-8」）
   排目标集合 + 现搜作业                  maa_action.py copilot find SR-8   ~10ms
   写 searches/<token>.json               ├─ nav_checker() 先拒掉上游没导航的关
   publish_round() 发「已排程」卡          ├─ dispatch_blocker() 被挡 → 拒绝，不排程
   到点 dispatch_batch()                  ├─ modes = required_modes()（活动关 both；突袭开关空则只 normal）
                                          └─ Popen(worker search <token>)
                                                worker search（5~20s，flock 防并发）
                                            ├─ refresh_roster() 失败 → **直接失败**（D13）
                                            ├─ pick_perfect_candidate()：ratio==1.0 且无缺口
                                            │    搜不到 → 红卡写清缺什么，不降级（D12）
                                            ├─ dispatch_blocker() 再查一次（可能已跨进
                                            │    blackout）→ status=scheduled，卡上写
                                            │    「本轮未启动：<原因>」，不派发
                                            ├─ publish_round() 发「已排程」卡
                                            └─ status=queued → dispatch_batch()
                        │
                        ▼
        worker run-batch <token>（一关 8~15 分钟，串行连抄）
   ├─ run_lock(blocking=False)：抢不到 → 放回 scheduled + 发通知
   ├─ 三条时间闸门再查一次（blackout / Depot 窗口 / 时间预算）：命中 → 放回 scheduled
   │    + 灰卡「本轮未启动：<原因>」，不碰设备（自动轮下一 tick 重派，手动轮不重试）
   ├─ emulator_is_online()：在线 → 放回 scheduled + 「模拟器正在使用」，什么都不碰
   ├─ status=executing → 收掉「取消本轮」按钮 → 开机链（StartUp/ADB/settle）
   ├─ 循环每个条目：
   │    mode == "raid" 且突袭开关为空 → 该条目 skipped，绝不跑 maa copilot（最后一道防线）
   │    maa_action.min_lead_reason()  距下一场日常不足 auto_copilot_min_lead_minutes → 收尾
   │    _recheck_before_stage()  活动 key 还在吗 + refresh_roster() 实时理智够吗
   │    _navigate_to_stage()     临时 MAA_CONFIG_DIR 里 Fight(times=1) + Custom 界面检查
   │    switch_to_raid()         仅 raid：Custom(ChangeToRaidDifficulty)（❌ 真机验失败，默认关闭）
   │    maa copilot <uri> --formation --batch -v --raid <mode>   （watchdog 处理接管弹窗）
   │    copilot_outcome() → settle_after_battle()（跳过首通剧情 + 返回主界面，非 nav 都跑）
   │    → record_copilot_attempt() → save_ledger()
   │    一关失败 **继续下一关**；`nav`（导航/切难度）失败 **停止整轮**
   └─ 截图 → close_everything() → 一条多关战报 → status=executed/execution_failed
```

### 抄作业的关键事实（C3 实机验证，2026-08-02；spike A 补充 2026-09-08）

1. **单作业 `maa copilot` 从主界面跑不起来**——任务链没有 StageNavigationTask，
   先用 `_navigate_to_stage()` 复用上游 Fight 选关，再交给 Copilot 自动编队。
   MaaCore v6.17.2 的 `times=0` 会禁用全部子任务，不能导航。当前使用临时
   `MAA_CONFIG_DIR`（复制现有 profile/cli/用户资源）运行 `Fight(times=1)`，
   仅覆盖 `FightBegin` 为继承上游 `BattleStartPre` 的 OCR、`DoNothing`、空 sub/next。
   独立 `Custom(CopilotNavigationReady)` 再识别一次关卡详情界面；退出码 0、
   `Fight Completed`、`Custom Completed` 缺一不可。临时目录在退出或异常时删除，
   日常与正式 Copilot 仍使用原配置。没有新增任何关卡选路定义。
   2026-09-08 15:17 真机通过（`runs/2026-09-08-copilot-9e14e500ac25.log`）。
2. **多关连抄走 B 基线**（spike A 的裁定）：每关一次上游 Fight 导航 + 单文件 copilot，
   不退模拟器。MaaCore 的 `copilot_list` 模式**挂起**，两个原因：
   ① maa-cli 0.7.5 的 stdout 分不出逐关成败（`Copilot Start/Completed` 是任务级，
   逐关只有重复的 `BattleFormation / MissionStart`），逐关记账得改读 `debug/asst.log`；
   ② 它的导航链不处理首通弹窗——SR-6 的「获得了塔罗牌」让它**死循环 9 分钟不报错**，
   直到 900s 超时。Fight 导航链自带 `<前缀>-TarotClose` 这类处理，B 基线因此更稳。
   详见 `docs/spikes/copilot-list-2026-09-08.md`。
3. **突袭切换 ❌ 2026-09-09 真机验证失败，功能默认关闭**（现场与下次验证清单见
   `docs/archive/PLAN_PHASE4.md` 的「突袭切换真机验证（2026-09-09）」）。
   Fight 导航停在的是普通难度详情页，要打突袭得先切过去；MaaCore 自己在
   `MultiCopilotTaskPlugin` 里用 `RaidConfirm` / `ChangeToRaidDifficulty`，这里照抄后者，
   跑在与导航同样隔离的临时 `MAA_CONFIG_DIR` 里。实测：SR-5 导航两段都 Completed，
   `Custom(ChangeToRaidDifficulty)` 13 s 后 `Custom Error`
   （`runs/2026-09-09-copilot-5814e6deb762.log` 第 20–32 行）。收尾截图是
   SideStory「月行水上」的普通难度关卡详情页，**整屏没有突袭控件**；上游 ROI
   `[600,610,130,110]`（1280×720，本机 1920×1080 由 MaaCore 自己缩放 ×1.5 →
   `x 900–1095, y 915–1080`）裁出来是纯背景。最可能的根因是**首次开放的 SideStory
   本来就没有突袭难度**（突袭只在复刻活动里有；上游 `tasks/Stages/SR.json` 也没有任何
   突袭变体，`RaidDifficulty-Chapter15.png` / `NormalDifficulty*.png` 是主线章节资产）。
   **`config.copilot_raid_switch_task` 空串 = 突袭功能整体关闭**（不是「跳过切换直接打」
   ——单文件 `maa copilot <uri> --raid raid` 根本不切难度，`is_raid` 只对 `copilot_list`
   的条目有意义，跳过切换打完会把普通难度记成一次突袭通关）。三层落实：
   `maa_clearing.required_modes()` 不产生 `raid` 条目 → worker `_batch_run_locked()`
   拿到 `raid` 条目判 `skipped` → `switch_to_raid()` 直接拒绝。仓库种子是 `""`。
4. **代理指挥开关**：点开始行动时代理开着会弹「确认要接管作战吗？」，MaaCore 的
   copilot 链不处理 → 必失败。worker 内置 watchdog（`ocrbox` OCR + ADB tap）在弹窗
   出现时点确认。正常路径（游戏新会话代理默认关）watchdog 不会触发。
5. **prts 的 `-1` 是「无要求」哨兵**：requirements 的 elite/level/skill_level/module
   为 -1 时视为无要求（实测踩坑：module=-1 曾导致 `module_levels[-1]` 越界）。
6. **prts 服务端索引滞后**：当期活动关 `level_keyword` 会静默退化（total=全量），
   必须用探针比对 + 客户端过滤兜底（对应测试 `test_level_keyword_degradation_*`）。
7. **作业 stage_name 两种写法并存**（内部 id / 展示码）：展示码的作业执行时从
   `/copilot/get/<id>` 取全文、改写 `stage_name` 为 stageId 后经 `file://` 传入。
8. **prts 的 `content.difficulty` 全为空**（地基事实 F），判不出「这份作业支不支持突袭」。
   所以 D7 定为：活动关一律普通 + 突袭，同一份作业先打普通再打突袭。
9. **逐关标记必须 `flush()` 之后再把句柄交给子进程**（2026-09-09 线上缺陷）。
   `_fight_one()` 先往日志写一行 `# <时刻> copilot <关> mode=<模式> id=<作业>`，
   再把**同一个** handle 当 `stdout` 交给 `maa copilot`。不 flush 的话标记还躺在
   Python 的缓冲区里，子进程直接往同一个 fd 写、先落盘，标记要等 `with` 退出才刷
   出来——**排在这一关全部输出之后**。今晨的现象：
   `runs/2026-09-09-copilot-fda56c1811ba.log` 第 37 行 `Copilot Completed`、
   第 41 行 `[Copilot] … Completed`，标记行却在第 42 行。于是「本关输出」切出来是
   空的，`copilot_outcome()` 一律 `unknown`，战报全 ❓、记账一次通关都不记，
   30 分钟退避一到又把同一批关重打一遍。
   现在的写法：写完标记 `handle.flush()` → 记下 `os.fstat(handle.fileno()).st_size`
   → 事后 `_log_since(log_path, offset)` 从这个**字节偏移**读。按偏移切比按标记
   `rsplit` 稳：偏移是标记落盘之后取的，同一个标记出现两次也不怕。
   （`maa_roguelike_worker._run_one_exploration()` 本来就对：它把标记写在一个
   **单独的 `with` 块**里，块退出即 flush，才开第二个 handle 给子进程。）
10. **解锁预检是单向的**（`zone_progress_warning`）：森空岛的 zone 进度含剧情关、
   `stage_id` 序号只数战斗关——两把尺子。因此只有 `cleared < seq`（突袭必锁）和
   `cleared < seq-1`（关卡必锁）两个方向成立；**有警告 = 基本锁着，没警告 = 不知道**，
   绝不能在别处把「没警告」当成「已解锁」的依据。它现在只用于**抄作业卡上的一行显示**
   （`🔒 …大概率未解锁`），永远不当闸门。
   序号超出第一个 zone（act17side 型多 zone 活动）一律静默；警告带森空岛快照时效标注。
11. **打完不能直接杀游戏——先跳剧情、回主界面**（2026-09-09）。SR-8 两次抄作业
   （`runs/2026-09-08-copilot-9e14e500ac25`、`2026-09-09-copilot-fda56c1811ba`）
   `Copilot Completed` 之后 5 秒 `close_everything()` 就 `maa closedown`，两张收尾截图
   都停在**战后剧情页**（右上「自动 OFF / 跳过 ▶」）。结果：游戏里 SR-8 三星，
   **代理指挥却锁着**，早场日常点了 10 次 `UsePrts` 点不亮、`Fight Error` 空跑。
   asst.log 复核过两场都没借助战（Copilot 参数无 `support_unit_usage`，8 个位置全是
   自己的干员；`BattleQuickFormationRole-Support` 是职业筛选「辅助」页签）。
   **09-09 08:59 真机验证**（`runs/2026-09-09-copilot-71bfa8404dbe.log`）：顺序是
   战斗结束 → 剧情 → 结算页；剧情跳过成功、游戏进到结算页、代理指挥解锁，虽然最终
   没回到主界面。**界线是结算页，不是主界面**——昨天被杀在剧情里，结算页从没出现过，
   所以今天剧情还重放了一遍（游戏没记成已看）。
   现在 `_batch_run_locked()` 对每个进过战斗的条目（`outcome != "nav"`）调
   `settle_after_battle()`：隔离 `MAA_CONFIG_DIR` 里跑 `copilot-settle.toml` 两段 Custom
   ——`["CopilotSettleSkip", *HOME_TASK_NAMES]`（在剧情页走跳过链，在详情页直接回家）
   + `HOME_TASK_NAMES`。`CopilotSettleSkip` / `CopilotSettleSkipConfirm` 派生自上游
   `SkipBattlePlot` / `SkipBattlePlotConfirm`（`SETTLE_OVERLAY_TASKS`，经 `_isolated_config()`
   的 `overlay_tasks` 只写临时 tasks.json），差别只有确认后的 `next` 清空：上游接的
   `EndOfPlot` 在跳过后直接进结算页时永远等不到，真机白等了 20 轮才 Error。
   **两段任一 `Completed` 即算成功**；两段都 Error 才在战报和账本 `detail` 带一句中性的
   「战后未回到主界面」，**不改胜负**。`copilot_settle_timeout` 默认 240s。
   结算页本身**不处理**：多关连抄时下一关的 `StartUp` 自带 `EndOfAction → ClickCorner`
   （03:06 SR-5 → SR-6 的日志），最后一关停在结算页被关掉已验证无害。若哪一关结算页
   之后还有第二个弹窗 / 剧情才需要派生 `EndOfAction`——注意上游 `ClickCorner.next` 里
   有 `StartButton2`，照搬会再开一局。

### 本地作业记账（`copilot/ledger.json`，四期 2a）

```jsonc
{ "activity_key": "SideStory「月行水上」|SR",
  "stages": { "SR-5": { "normal": "2026-09-09T02:10:00+08:00", "raid": null,
                        "attempts": [ {"at": …, "mode": "raid", "op": 102169,
                                       "outcome": "failed", "detail": "…"} ] } } }
```

- **一关「通关」= 所需模式全部拿到 `Copilot Completed`**（`copilot_outcome()`，不变量 #33）。
  所需模式由 `required_modes(stage, prefix, config)` 定：当期活动前缀的关（数字 / EX / S）是
  `normal + raid`，资源本 / 主线只有 `normal`。**突袭失败 → 该关不算通关**，下一轮只排剩下的
  `raid`（D7）。**`config.copilot_raid_switch_task` 为空时活动关也只有 `normal`**（突袭功能
  关闭，现状即如此），卡上写「✅ 已通（突袭功能未启用）」以免 ✅ 被误读成突袭也通了。
- `record_copilot_attempt()` **只有 `won` 才写模式时间戳**；`unknown`（退出码 0 但没有
  胜利标记）与 `failed` 一样只进 `attempts`。`attempts` 只留最近 40 条。
- **「已通关」有两条判据，是「或」的关系**（D10，`ledger_pending_modes()`）：
  ① 本地记账里该模式有时间戳；② 森空岛该关所属 zone `clearedStage == totalStage`。
  ②是 zone 粒度的妥协——代价是用户自己手打过的关可能被再抄一遍，用户已接受。
- `activity_key` 变了（新活动 / 复刻）→ 旧账**归档**成 `ledger-<安全化的 key>.json` 再新开
  （`ensure_ledger()`）。归档而不是删：回头想知道上个活动抄到哪了只有这份说得清。
- **非活动关（资源本 / 主线）另有一份 `ledger-standalone.json`**（四期步骤 5 修的线上隐患）：
  手动抄 `CE-6` / `1-7` 时 `start_copilot_search()` 写的 `activity_key` 是**空串**，
  worker 照样 `ensure_ledger("")`。原来这一步见 key 不同就把当期活动账本归档、新开一份
  空的，下一 tick watcher 再归档回来——真实通关记录就这样散进归档文件。现在
  `ledger_path(activity_key)` 决定落点：空 key → 独立账本，**永不归档、永不新开活动账本**；
  worker 必须拿同一个 key 算出的路径去 `save_ledger()`，否则记完账会把独立账本写回活动账本。
  独立账本不参与自动排轮（自动只跑活动关），种子也永远种不进去（前缀不是 `SR`）。
- **种子**（`seed_ledger()`）：当期活动前缀是 `SR` 时，把 `SR-5 / SR-6 / SR-8`
  的**普通**模式写成 `2026-09-08T23:58:00+08:00`（来源：spike A 报告与用户自述——
  SR-5/SR-6 用户自己打过且 spike 又重打了一遍，SR-8 是 09-08 15:17 抄作业通关的），
  突袭留空。**只补空位、绝不覆盖已有记账**，换到别的活动绝不写。
  判据是账本上的 **`seeded` 标记**，不是「是不是首次建账」（2026-09-09 线上缺陷）：
  原来只在 `load_ledger()` 返回 `None` 时才种，而线上 02:23 上一个活动
  （「墟」复刻 `|AT`）的 watcher tick 先把账本建了出来，03:00 SR 上线走的是
  「归档旧账 + 新开一份」，`ledger is not None`，种子一次都没跑——SR-5/6/8 的普通
  全空，第一轮就把它们重抄了一遍。`seed_ledger()` 只补空位所以幂等，按标记补是安全的。
- **当日上限**（`auto_copilot_max_attempts_per_day`，默认 2）：同一 `(关, 模式)` 当天
  失败 / `unknown` 达到这个次数后，当天不再自动排它（`collect_targets()` 的排除理由
  `daily_cap`，数据取自 `attempts` 里按 `at` 落在今天的记录，`won` 不计入），
  抄作业卡上显示「🚫 今日已尝试 N 次」，次日 `attempts_today()` 自然归零、无需清状态。
  它挡的只是**自动**排轮，手动从下拉重打不受限（D14）。
  来由：今晨 03:30 与 04:00 两轮把 SR-5/6/7 原样重打了一遍——退避
  （`activity_search_retry_minutes`）管的是「隔多久再试」，管不住「一天试几次」。
- 记账写在 `save_ledger()`（0600，`atomic_write_json`）；账本坏掉 `load_ledger()` 返回
  `None` 而不是抛——最坏结果是重抄一遍，不会漏抄，也不会带走整轮（D-2 的教训）。

### 森空岛（`maa_skland.py`）

- 签名公式黄金向量已冻结（`test_signing_golden_vector`）：`MD5(HMAC-SHA256(
  path + query/body + t + header_ca))`，`t = now - 2`，GET query 不带 `?`，
  请求头用小写键 `cred/sign/platform/timestamp/dId/vName`，`dId/platform/vName`
  可为空串（数美风控放行，C0 实测）。
- 链路：grant → cred（返回的 `token` 是签名密钥）→ binding（`data.list[]` 按
  `appCode == "arknights"` 选，uid 取 `defaultUid`）→ `player/info?uid=` 的
  `chars[]`（字段：`evolvePhase`=精英化、`skills[].specializeLevel`=专精、
  `equip[].{id,level,locked}`=模组，锁着的模组不计数）。
- 同一响应的 `currentTs` 与 `status.ap.{current,max,lastApAddTime,completeRecoveryTime}`
  归一化进 `roster.sanity`。卡片与排轮可按 6 分钟/点估算缓存后的自然恢复，但**每关真正
  开打前必须再次 `refresh_roster()`**，缓存估算不能替代最终真值。
- 名字解析：skland 响应只有 charId；干员名→charId 用本地 `battle_data.json` 的
  `chars` 字典（`names` 键不存在，C0 实测）。只解析到 `token_*`/`trap_*` 的视为
  召唤物、不计缺口。
- token 只从 stdin 进（`link` 子命令）或 `SKLAND_HG_TOKEN` 环境变量进（deploy），
  永不经 argv/Discord/agent 上下文；`skland.json`/`roster.json` 0600。
- cred 过期（10002/HTTP 401）→ 用存的 `hg_token` 重跑 grant，**只重试一次**，
  再失败记 `last_error` 提示重新 `link`，绝不循环。

### 练度后备：OperBox（四期步骤 5）

森空岛挂了不该让练度整条断掉。**Depot 那趟**（`maa_depot.run_depot()`，每周一次、
04:30）在拿完库存之后、关模拟器之前，如果森空岛不可用就顺手跑一次
`maa run operbox`（仓库里的 `config/maa-cli/tasks/operbox.toml`，`type = "OperBox"`）。

- **判据 `skland_unavailable()` 只读本机状态、绝不发请求**（它跑在锁内，不能等网络）：
  `skland.json.last_error` 非空，或 `roster.json` 缺失 / 坏掉 / 没有时间戳 /
  已经超过 `skland_roster_refresh_days`（懒刷新挂在每条抄作业路径上，还这么旧就是一直没成功）。
- 输出形状：maa-cli 把回调整条打成 `OperBox: <json>`（与 `Depot:` 同一形状，
  `run/callback/` 里两者都走 `info!("{}: {}", …, json_pretty(message))`），
  `details` 来自 MaaCore `OperBoxRecognitionTask`：
  `{done, all_opers[], own_opers[{id, name, own, elite, level, potential, rarity}]}`。
  解析器与 Depot 共用 `_json_objects_after()`，同样**只认 `done=true` 的那一条**。
  `id` 是 `char_xxx`（BattleData 那套，与森空岛 `charId` 同源）；认不出 id 的条目直接丢，
  一个都不剩就报错——绝不写一份假快照。
- 快照落 `copilot/roster-operbox.json`（0600，`source = "operbox"`），**不覆盖
  `roster.json`**：它只有 `{name, elite, level, potential}`，覆盖过去连能校验的信息也没了。
  `load_roster()` 取两份里 `fetched_at` 更新的那一份，所以森空岛一恢复后备自动退场。
- **fail-closed（D12）**：`check_op_requirements()` 见到 `source == "operbox"` 时，
  技能与模组要求一律记**硬**缺口（森空岛那份里模组是软缺口）。
  后备快照也没有 `sanity` / `activity_progress`，于是理智预算按未知处理（不变量 #28）、
  zone 全清判据不成立——宁可多抄一遍，也不拿猜出来的练度去开代理。
- OperBox 失败**不影响仓库对表**：异常被吞掉，成败各写一行到
  `last_run.depot_operbox_note`（不变量 #22 的精神——练度只是搭车）。

---

## 5.6 理智让路（`maa_daily.attach_sanity_yield()`）

四期 D11。自动抄作业跑完常常把理智打到个位数，这时候日常再开一个 Fight 只会空转一次
连战识别。判据只有一条：

```
估算理智 < 默认关的 apCost  →  plan["farming"] = False（只摘刷理智，保留剿灭）
```

- 估算走 `maa_copilot.load_roster()` + `estimated_sanity()`（缓存 + 6 分钟/点），
  关卡理智走 `stage_index` + `stages.json`。
- **估不出来一律照常刷**（森空岛不可用、关卡不在 `stages.json`、状态文件坏掉、
  任何异常）——宁可多刷一场，也不能因为读不出数就一整天什么都不打（不变量 #26）。
- 让路时开跑消息里写明理由（`plan["farming_skip_reason"]`），不静默。
- `render_daily_toml()` 的两个开关**互相独立**：`plan["annihilation"]` 管每日剿灭，
  `plan["farming"]` 管刷理智，`tasks.fight` 是二者的总开关。剿灭是代理卡驱动的
  周上限 1800 合成玉，摘掉纯亏，所以它不跟着走。

> 三期的**通关队列**（`clearing.json`、「需要你手动打一次」出口、EX 开放闸门、
> 编队界面理智 OCR、日常收尾派发）在四期步骤 2 整体删除（D15）。它的目标是「全通关」，
> 而四期的目标是「上游有导航的关首通一遍」，两者的调度、记账、失败出口都不一样。
> 保留下来复用的只有领域函数：条目形态、导航键闸门、理智预算与排轮、通关判据。

---

## 5.7 链路五：肉鸽（`maa_roguelike.py` + `maa_roguelike_worker.py`）

**手动触发、上游全托管、优雅停止**（四期 D19，spike C 的结论全文在
`docs/spikes/roguelike-2026-09-08.md`）。肉鸽**不消耗理智**，不进理智预算、
不触发日常让路；但它一样开模拟器、一样持 `run.lock`，所以闸门一条不少。

```
聊天「刷肉鸽」
  → maa_action.py roguelike card       只读本地，一次 POST，同步做完（<20s）
      ├─ 收旧卡：retire_discord_components(roguelike.json.message_id) → 删旧登记
      ├─ state_lock() 下 merge 写 hermes:btn:maa-roguelike:{theme,mode,difficulty,squad}-select
      │                            + {start,stop,abort}
      └─ 发卡（4 个下拉 + 1 行 3 个按钮 = 正好 5 行）→ message_id 写回 roguelike.json
  下拉选中 → select_args → roguelike set <字段> <值> → 只写 pending
      └─ 字段是 theme 时：整份 pending 重置成新主题的默认 + **重发一张卡**
  「▶️ 开始」→ roguelike start → dispatch_blocker() → 冻结 params → Popen(worker)
                       │
                       ▼
      maa_roguelike_worker.py run <token>
   ├─ run_lock(blocking=False)：抢不到 → 灰卡 + 状态回 idle
   ├─ emulator_is_online()：在线 → 灰卡，什么都不碰（不变量 #24）
   ├─ while：
   │    读 abort_requested_at / stop_requested_at（**局间**才读 stop）
   │    _next_round_blocker()  blackout / Depot 窗口 → 不开新局
   │      首局就被挡 → 一次都不开机，灰卡「本轮未启动：<原因>」+ 照发战报
   │    首局（且只有首局）走开机链：open_emulator / wait_for_android
   │                                / verify_game_installed / settle_android_runtime
   │    _isolated_config() 写 tasks/roguelike.toml
   │    Popen([maa, run, roguelike, --user-resource, --batch, -v])
   │      └─ 每 2s 轮询 abort_requested_at 与单局超时 → terminate() → wait(10) → kill()
   │    parse_run_output() → 追加 runs[] → 发一条进度
   │    储备满（TraderInvestSystemFull）/ 连续 2 局 Roguelike Error → 收尾
   └─ 截图 → close_everything() → 汇总战报（局数 / 耗时 / 上游 summary 段 / 停止原因）
```

**为什么走 `maa run` 任务文件而不是 `maa roguelike` 子命令**（spike C 第 1、6.7~6.10 节）：
maa-cli 0.7.5 的 `Theme` 枚举里**没有 `BlackFlow`**、`mode` 硬校验 `0..=5`
（MaaCore 另有 `6/7/10001/20001/30001`）、`stop_when_investment_full` 与 Sami 的
`use_foldartal` 两处默认值和协议**相反**、Phantom 主题的 `--difficulty` 被直接丢弃，
还有 12 个 MaaCore 字段 CLI 覆盖不到（`blackflow_strategy` 就在里面）。
任务文件的 `[tasks.params]` 是自由 JSON/TOML 透传，一次绕开全部这些坑；
写法照 `_navigation_config()`：临时 `MAA_CONFIG_DIR`，只读上游 resource、只写临时目录。

**为什么每局一个子进程**（spike C 第 3.4~3.5 节）：MaaCore 只有两个「干净收手点」——
`starts_count` 用尽（**准备开第 N+1 局的那一刻**停，即第 N 局已结算、人在主题入口页）
和投资达标 / 储备满时的 `exit_then_stop(abandon=true)`；而 `AsstStop` 是**立即中止**，
停在哪算哪，也没有 API 能在运行中把 `starts_count` 改小。所以
`starts_count = 1` + worker 在**局间**读停止标志，就是「收到指令后本局结束再停」的
全部实现。每局重连开销实测约 1 秒（asst.log 两次子进程 0.9 / 1.2 s），相对一局
十几分钟可以忽略；换来的是每局一条进度、每局一个天然取消点、单局崩了不带走整轮。

**为什么 `Popen` 而不是 `subprocess.run(timeout=)`**（spike C 第 6.6 节）：
`subprocess.run` 超时走的是 `kill()`（SIGKILL），MaaCore 来不及 destroy，
模拟器会被留在地图里。maa-cli 收到 **SIGTERM** 才走 `bail!("Interrupted by user!")`
那条路（第二次同类信号会被内核直接杀掉），所以中止一律
`terminate()` → `wait(10)` → 万不得已才 `kill()`。

**同主题重启约束**（spike C 第 3.3 节，上游 issue #13245）：上游
`<Theme>@Roguelike@Begin` 的 `Continue → ExitThenAbandon → AbandonConfirm` 善后链
**只在同主题下**成立，跨主题会死循环。所以一局开跑**之前**就先把
`dirty = true` / `last_theme = <主题>` 落盘（进程被杀也留得住），只有
`Roguelike Completed` **且**退出码 0 才清掉；`dirty` 时 `roguelike start`
拒绝换主题并告诉用户先用同主题跑一局收尾。

**崩溃残留的自愈**（`round_is_stale()`）：worker 整轮（含局间）都持着 `run.lock`，
所以「状态说 `running` **但锁是空的** **且** 状态 10 分钟没更新过」只可能是 worker 进程
没了（崩溃 / 被 kill / 重启）。这时 `roguelike start` 与 `roguelike set` 放行，
`start` 直接按新的一轮重开并在回执里说明。两条判据缺一不可：只看锁会把
「`roguelike start` 写完状态、worker 还没抢到锁」那几秒误判成崩溃。
`dirty` / `last_theme` **不清**——残局还在游戏里。

**输出解析**（spike C 第 4 节，`maa_roguelike.parse_run_output()`）：
一局开始 `Start exploration N times`、一局结算 `Gain N exp during this exploration`、
放弃 `Exploration Abandoned`、投资 `Deposit a / b / c originium ingots`（储备上限 999）、
储备满 `TraderInvestSystemFull`、干净结束 `Roguelike Completed`、
被中断 `Roguelike Stopped`、出错 `Roguelike Error`。
`RoguelikeInvestmentReachLimit / ReachFull` 两个回调 maa-cli 不打印（要 `-vvv`），
所以「投资满因此收手」靠 `TraderInvestSystemFull` + 随后的 `Roguelike Completed` 判。
结尾的 `Explorations: … Summary:` 段由 `parse_summary()` 拆成字段、
`summary_fields()` 渲染成中文——**不能整段贴进 Discord**（不变量 #9）。

**卡片为什么是 4 个下拉**：Discord 一张卡最多 5 个 action row，select 独占一行，
每个 select ≤ 25 项。主题 6 项、模式 ≤4 项、难度 ≤21 项（不指定 + MAX + n..0，
上限 Phantom/Sami/BlackFlow 15、Mizuki/Sarkaz/JieGarden 18）、分队 ≤19 项
（主题专属 + 通用 6 个 + 高规格，**只有 BlackFlow 不加高规格**）——正好塞得下。
职业组（`roles`）不占 UI，按主题映射（傀影 `取长补短`，其余 `稳扎稳打`）；
`core_char` 留空让上游按练度选；`use_support` **永不传**。
**黑流树海是例外**：MaaCore v6.17.0 起它只有三个策略，模式下拉直接映射
`blackflow_strategy`（`burn` / `investment` / `baby_animal`，后者带
`blackflow_cultivation_target = swaddled_cat` 且 `mode = 30001`）。

---

## 6. 状态模型

```
~/.hermes/state/maa/
  config.json     环境与阈值（路径、超时、channel_id、活动关缓存）    ← 计划卡会改写
  defaults.json   持久默认：stage="1-7" / medicine=expiring / tasks 全开
  plan.json       今日覆盖（**按场次**）：{date, sessions: {am: {stage, stage_source,
                  skip, source, updated_at, farming_drops, farming_fallback_stage},
                  pm: {…}}, updated_at}
  manual_run.json 手动场选关请求（单文件、同一时刻只有一个）：{token, status ∈ pending/
                  dispatched/expired/superseded, stage, choices, recommendation,
                  recommended_stage, message_id, created_at, expires_at(+10min), …}
                  手动关**只经它**传给 `maa_daily.py now <token>`，不进 plan.json
  last_run.json   最近一次运行状态 + threads[] + 资源更新时间戳
  activity_watch.json 当前活动 + 本轮 token + 每关搜索失败退避（stage_state）+
                      idle/searching/waiting/done/scheduled/dispatched/running 状态
  depot.json      最近完整 Depot 真值 + 期间掉落增量 + 历次消耗与日均消耗率
  farming/matrix.json        一图流掉落矩阵镜像（ETag 条件请求，1.4 MB，0600）
  farming/stage_info.json    一图流关卡排期 / 复刻标记 / apCost（1h TTL）
  farming/item_value.json    一图流物品价值表（24h TTL）
  farming/store_activity.json 当期活动商店（可选，24h TTL）
                  以上四份都是 `{fetched_at, etag, payload}`；**只有 `maa_plan_card`
                  会写**，`maa_activity_watch.cost_lookup()` 只读它（EX 关理智兜底）
  copilot/ledger.json      当期活动的作业记账：每关每模式的通关时间戳 + attempts（0600）
  copilot/ledger-<活动>.json 换活动时归档的旧账本（0600）
  copilot/ledger-standalone.json 非活动关（资源本 / 主线）的记账（0600）。空
                      `activity_key` 只写这一份，**永不归档、永不碰活动账本**（不变量 #52）
  copilot/roster.json      森空岛练度 + 理智 + zone 进度快照（0600，`source = "skland"`）
  copilot/roster-operbox.json 森空岛不可用时 Depot 那趟拿的练度后备（0600，
                      `source = "operbox"`，只有精英化 / 等级 / 潜能）。`load_roster()`
                      取两份里 `fetched_at` 更新的那一份（不变量 #53）
  copilot/searches/<token>.json 一轮的执行清单 `entries: [{stage, stage_id, mode,
                      candidate, sanity_cost}]` + scheduled_at + 状态机（25h TTL）
  copilot/card.json 上一张抄作业卡的 message_id（0600）。**不能放进 copilot/searches/**
  roguelike.json  肉鸽一轮的全部状态（0600）：{status: idle|running|stopping, theme,
                  params（开始那一刻冻结的 MaaCore 参数）, pending（卡上选了但还没开始的）,
                  token, message_id（上一张卡）, started_at,
                  runs: [{n, started_at, ended_at, outcome, invest, exp, detail}],
                  stop_requested_at, abort_requested_at, last_theme, dirty, updated_at}
                  `updated_at` 不是装饰：`round_is_stale()` 靠「状态说在跑 + 锁是空的 +
                  它超过 10 分钟没动」认出崩掉的 worker，否则一次崩溃会把功能永久锁死
                  **两个进程同时写它**：按钮写 pending / 两个停止标志，worker 写
                  runs / dirty / status，所以一律走 `maa_roguelike.update_state()`
                  （`state_lock` 下 read-modify-write），不许整份覆盖
  run.lock        flock 独占锁（执行）
  last_run.lock   flock 独占锁（保护 last_run.json 的跨进程 read-modify-write）
  am/ pm/         空目录，仅用于给 cron 传场次
  runs/<date>-<phase>.{json,log,png}   本场快照 / 完整日志 / 截图（保留 14 天）

~/.hermes/state/agent-book/
  buttons.json    全局共享按钮登记表（MAA 只碰 hermes:btn:maa:* 前缀）
  state.lock      共享写锁
```

**计划的求值**（`effective_plan(at, phase)`，`scripts/maa_common.py`）：

```
result = normalize_plan(defaults.json)
if plan.json.date == 今天(Asia/Shanghai):
    只叠加 sessions[phase] 里的 stage / stage_source / skip /
    farming_drops / farming_fallback_stage 五个键
result = normalize_plan(result)             ← 再规范化一次
```

- `phase ∈ {am, pm, now}`。**`now` 永远拿不到覆盖**：没有任何写路径会产生
  `sessions.now`（`plan set-stage` / `skip-today` 只接受 `am` / `pm`），
  所以手动场天然走缺口默认关（D4）。
- 磁盘上是**旧的按天格式**（顶层 `stage` / `skip`）时 `plan_sessions()` 返回空表，
  等于当天没有覆盖。**不做迁移**：那份数据一天后本来就失效。
- `normalize_plan()` 每次都**强制** `medicine="expiring"`、`stone=0`，
  即使磁盘上被人手工塞了别的值也没用（对应测试
  `test_persisted_medicine_choice_cannot_disable_fixed_expiry_policy`）；
  但它**保留 `stage_source`**——那是卡片判断「这关是谁定的」的唯一依据；
  `farming_drops` / `farming_fallback_stage` 同样穿过这层，其中 `farming_drops`
  的 itemId 要过 `SAFE_ITEM_ID_RE`（它会原样变成 `daily.toml` 里 `drops` 表的键），
  **空值等于没有**——手动选关就是靠写空把上限清掉的。

**三把锁，别搞混**：

| 锁 | 作用域 | 用法 |
|---|---|---|
| `run.lock` | 「同一时刻只有一场 MAA」 | runner 用非阻塞抢并显式处理冲突；`maa_action` 只用它做只读探测 |
| `last_run.lock` | `last_run.json` 跨进程合并不丢字段 | `merge_last_run()` 内部阻塞持有，调用方不用手动加锁 |
| `agent-book/state.lock` | 按钮登记表 + `plan.json` 写入 | 阻塞式，跨功能共享 |

`merge_last_run()` 语义特殊：普通字段覆盖，`threads` 数组**按 thread_id 去重后追加**——
子区清理依赖这份累积列表，不能改成整体覆盖。

同一天同场次的 `runs/` 文件名会复用（`<date>-<phase>`），所以一天里多次 `run now`
会覆盖 `<date>-now.log/png`。要保留请自行改名或改 `run_stem`。

---

## 7. 部署链路（`deploy.py`）

```bash
python3 -m unittest discover -s tests -v   # 必须在仓库根目录跑（测试要 import 根目录的 deploy.py）
python3 deploy.py                          # 幂等；输出 {"ok":true,"changed":[…],"backup":"…"}
python3 deploy.py --check                  # 检查脚本/ocrbox 与 7 个 maa-cli 配置是否存在，并逐字节核对其中 6 个
```

做的事：

1. **备份** — 任何将被修改的文件，先按原路径复制到
   `~/.hermes/backups/maa-setup/<YYYYMMDDTHHMMSS>/`。
2. **幂等写** — `write_bytes()` 内容一致就跳过（不写、不记 changed、不备份），
   否则 `mkstemp + fsync + os.replace` 原子替换，权限 0600/0700。
   `--check` 对仓库管理的 7 个 `config/maa-cli/**` 文件查存在，并对其中 6 个逐字节比对
   仓库与运行时；文件存在但内容漂移会列入 `mismatched` 并返回失败，不再把“存在”误报成
   “部署干净”。
   - **`tasks/daily.toml` 是唯一的内容比对豁免项**（`deploy.RUNTIME_REWRITTEN_MAA_CONFIGS`）：
     它每场执行前都被 `render_daily_toml()` 整份重写（§0 的例外），线上内容本来就该与仓库
     种子不同。比内容会让 `--check` 每跑完一场日常就永久变红，红久了就没人看了。
     它仍然照常部署、照常查存在。
   - 其余文件一律仍比内容，**尤其 `resource/tasks/tasks.json`**——没有任何运行时逻辑重写它，
     它漂移就是真事故（`49da169` 的成果）。豁免名单要加项必须同时说明“谁在运行时重写它”。
3. **状态的部署策略不同**（`deploy_state()`，`deploy.py`）：
   - `config.json`：**逐键合并**，仓库的键覆盖同名键，本地新增键保留。
     **例外是三个运行时缓存键**（`deploy.PRESERVED_RUNTIME_CACHE_KEYS`，不变量 #25）：
     `maa_core_version` / `maa_cli_version`（计划卡的版本探测写）和
     `activity_stages`（活动 watcher / 计划卡写）——**线上已有值就一律保留**，
     种子只在线上还没有这个键时（首次部署）当兜底。
     它们是运行时缓存，不是仓库拥有的配置；让种子赢就等于每次部署都把
     `PLAN_NAV_GATE.md` A-3 修掉的那种过期又造一遍
     （2026-09-02 实际发生过一次：部署把正确的 `[]` 换成了一个月前 TO 活动的关卡，
     直到下一次 watcher 才清掉）。
     同时丢掉已废弃的 `resource_update_interval_days`。
   - **`channel_id` 例外**：仓库里存的是占位符 `REPLACE_WITH_DISCORD_CHANNEL_ID`，
     真实频道 ID 不入库。`resolve_channel_id()` 按
     `MAA_CHANNEL_ID` 环境变量 → 本机已部署的 `~/.hermes/state/maa/config.json`
     的顺序解析，都拿不到就直接报错终止。已配置过的机器重跑 `deploy.py` 不需要带参数；
     换新机器首次部署要写成：

     ```bash
     MAA_CHANNEL_ID=<频道数字ID> python3 deploy.py
     ```
   - `defaults.json` / `plan.json` / `last_run.json`：**只在不存在时**才复制种子，
     之后强制 `defaults.stage="1-7"`、`defaults.medicine="expiring"`、`defaults.stone=0`，并剥掉 `plan.json`
     里可能残留的 `medicine`/`stone`。
4. **给 Hermes 打 4 处补丁**：

| 目标 | 改动 |
|---|---|
| `hermes-agent/plugins/platforms/discord/adapter.py` | 白名单加 `maa_action.py`；`_run_hermes_button_script` 增加 `selected_values` 参数；增加 `select_args` 精确查表分支；调用处传入 Discord 的 `values` |
| `hermes-agent/LOCAL_PATCHES.md` | 登记上述补丁 |
| `~/.hermes/.env` | `DISCORD_FREE_RESPONSE_CHANNELS` 追加频道 ID（免 @） |
| `~/.hermes/config.yaml` | 频道绑定 `maa-operator` 技能 + 频道提示词 |

> `patch_config_yaml_text()` 只认 `channel_prompts:` 这一行做锚点（缩进按块内实际
> 缩进推导），**不许拿相邻的顶层键（如 `timezone`）当锚点**——键顺序归 Hermes 管，
> 会变（见 §11.17）。是否需要插入按 `yaml.safe_load` 的**解析值**判断而不是字面量，
> 这样 Hermes 折行重排过文本也不会重复插入；**替换**旧文案同样按解析值判断，
> 并由 `_replace_channel_prompt()` 连同折行的续行一起换掉（§11.17b）。写盘前后各解析一次，
> 顶层 `timezone` 丢失/被改、或 `platforms.discord.channel_prompts[<频道>]`
> 不等于目标文案就 `RuntimeError` 且不落盘；config.yaml 在打补丁前就解析不了也直接拒绝。

> `patch_adapter_source()` 用**精确字符串匹配**，匹配不到就 `RuntimeError`
> 而不是硬改。因此 **`hermes update` 之后 `deploy.py` 可能直接报错**——这是刻意的：
> 宁可失败，也不要在改了结构的 adapter 上瞎打补丁。届时需要读新 adapter 源码更新这些 old/new 串。

**什么时候必须 `hermes gateway restart`**：

| 改了什么 | 需要重启 gateway？ |
|---|---|
| `scripts/*.py` | 否（每次都是新进程） |
| `state/*.json`、`config/maa-cli/**` | 否 |
| `adapter.py`（白名单/下拉派发） | **是** |
| `.env`、`config.yaml`（频道绑定、提示词） | **是** |
| `SKILL.md` | 是（技能在会话初始化时加载） |

cron 本身只在首次创建时需要注册：

```bash
hermes cron create "*/15 * * * *" --name "MAA 活动作业监控" --script maa_activity_watch.py --no-agent --deliver local
hermes cron create "30 20 * * *"  --name "MAA 仓库对表"     --script maa_depot.py --no-agent --deliver local
hermes cron create "50 23 * * *" --name "MAA 计划卡·早" --script maa_plan_card.py --no-agent --deliver local --workdir ~/.hermes/state/maa/am
hermes cron create "50 13 * * *" --name "MAA 计划卡·晚" --script maa_plan_card.py --no-agent --deliver local --workdir ~/.hermes/state/maa/pm
hermes cron create "0 0 * * *"   --name "MAA 日常·早" --script maa_daily.py --no-agent --deliver local --workdir ~/.hermes/state/maa/am
hermes cron create "0 14 * * *"  --name "MAA 日常·晚" --script maa_daily.py --no-agent --deliver local --workdir ~/.hermes/state/maa/pm
```

cron 表达式是 **UTC**（`config.yaml: timezone: Etc/UTC`），所以 Depot 的 `20:30 UTC` 是
次日北京时间 `04:30`，计划卡的 `50 23` / `50 13 UTC` 是京 `07:50` / `21:50`；
脚本内部一律用 `Asia/Shanghai`。改时间时两边都要想清楚。

**计划卡现在也靠 workdir 传场次**（四期 1b）。在册的那条计划卡 cron
（`03d5e98ce8d4`）**没有 workdir**，部署这一步时要么 `hermes cron update` 给它加上
`--workdir ~/.hermes/state/maa/am`，要么删了按上面重建；晚场那条是**新建**的。
不改也不会崩（`select_phase()` 会按小时兜底判成 `am`），但那是兜底不是设计。

当前在册：`3ba48fb85d0b`（活动作业监控）、`b2f15f13589f`（仓库对表）、
`03d5e98ce8d4`（计划卡·早，待补 workdir）、`50cd1fb095f2`（日常早）、
`4f48cf619d9f`（日常晚），可用 `hermes cron list` 复核。

---

## 8. 不可破坏的不变量

改任何代码前先读这张表；每一条都有对应单测，改坏了会红。

| # | 不变量 | 落点 |
|---|---|---|
| 1 | `stone = 0`，永不提供任何入口 | `render_daily_toml` 字面量 + `normalize_plan` 强制 + 技能第 3 条 |
| 2 | `medicine = 0`，只用 `medicine_expire_days` 吃临期药 | 同上；`set_medicine()` 直接返回 `ok:false` |
| 3 | `series = 0`（由 MaaCore 选当前理智支持的最大连战） | `render_daily_toml` 字面量 |
| 4 | 基建用 `mode = 0` 实时自动排班，facility 覆盖七类设施；不使用无人机、不补源石碎片订单、宿舍不刷信赖；新版红色锁定位置确认条由用户资源层的 `InfrastDormDoubleConfirmButton` 完整覆盖接管（该覆盖尚无 run 级实测，见 §4 步骤 7）。该覆盖所在的 `resource/tasks/tasks.json` 必须被 `--check` **逐字节**守住，不得进 `RUNTIME_REWRITTEN_MAA_CONFIGS` 豁免名单 | `render_daily_toml` + `infrast-smoke.toml` + 用户资源 `InfrastDormDoubleConfirmButton` + `test_deploy_check_skips_daily_toml_but_still_guards_tasks_json` |
| 5 | 截图发生在 `maa run` 之后、`closedown` 之前；CloseDown 不进任务链 | `execute()` 步骤 9/10 + `daily.toml` 注释 |
| 5b | 日常战报的截图必须 OCR 确认在游戏主页；抄作业不受这条闸门约束 | `capture_home_screenshot()` / `verify_home_screenshot()`；`maa_copilot_worker` 直接用 `capture_screenshot()` |
| 5c | `Custom` 的 `task_names` 每项都带 `Home@` 前缀，`#back` 才有得解析 | `maa_daily.HOME_TASK_NAMES` + `config/maa-cli/tasks/daily.toml` 注释 |
| 5d | `skip` 只挡**设它的那一场**；`phase == "now"` 永远照跑 | `execute()` 的 skip 分支带 `and phase != "now"` + `SessionDailyTests` |
| 6 | 按钮脚本必须 < 20s 返回 | `run_now()`/`copilot_*` 用 `Popen`；活动关探测 0.7s |
| 7 | `buttons.json` 只能 merge，且计划卡只碰 `hermes:btn:maa:` 前缀 | `maa_plan_card.main()` |
| 8 | Discord 下拉值必须经 `select_args` 查表，不能直通 argv。**场次（`am`/`pm`）同样写死在登记表的 argv 里**，Discord 侧注入不了 | adapter 补丁 + `maa_plan_card._stage_select()` + `maa_action._copilot_card_components()` + `test_every_control_carries_its_own_phase` |
| 9 | 原始 CLI 文本不进 Discord | `progress_content` / `extract_summary` / `_copilot_summary` 是唯一出口 |
| 10 | 开跑后快照即冻结，改计划不影响本场 | `runs/<date>-<phase>.json` + 内存 plan |
| 11 | 所有 maa-cli 子进程显式注入 `MAA_CONFIG_DIR` | `maa_subprocess_env()`（macOS 默认目录不是 `~/.config/maa-cli`） |
| 12 | 日常 `maa run` 剔除代理；`maa update` / brew 保留代理以便访问 GitHub | `maa_subprocess_env(keep_proxy=…)` |
| 13 | 抄作业与日常互斥：同一时刻只有一场 MAA（`run.lock`）；08:00/22:00 前 `copilot_blackout_minutes` 内**拒绝派发**。闸门**只有一份**（`maa_action.dispatch_blocker()`：到点 / blackout / Depot 窗口 / 时间预算 / `run.lock` / 模拟器），自动与手动共用，三个落点各查一次：`start_copilot_search()`（写 `pending` 之前——被挡就拒绝、什么都不写、不排程，手动轮没人替它重试）、`cmd_search()`（搜完派发之前，搜索要跑 5~20s 可能跨进 blackout；被挡就把这一轮改成 `scheduled` 并在卡上写「本轮未启动：<原因>」，等用户重新选关，靠 TTL 清）、`cmd_run_batch()`（抢到锁之后、碰设备之前，只查三条**时间**闸门——锁在自己手上，再查 `run.lock` 只会自相矛盾） | `maa_action.dispatch_blocker()` + worker `run_lock(blocking=False)` + `CopilotDispatchGateTests.test_the_gate_has_exactly_one_implementation` / `ManualCopilotPathTests`（三处落点各一条） |
| 14 | 抄作业按钮用 `hermes:btn:maa-copilot:` 前缀（连字符），早清扫 `hermes:btn:maa:` 碰不到；过期由 `prune_expired_searches()` 清理 | `maa_copilot.BUTTON_PREFIX` + `maa_plan_card` 锁块 |
| 14b | **抄作业卡**用 `hermes:btn:maa-copilot:card:` 前缀：它在 #14 的前缀之下（躲开计划卡清扫），而 `card` 不是 12 位 hex，`prune_expired_searches()` 只按 `searches/<token>.json` 里真实存在的 token 清，也碰不到它。因此 `card.json` **必须放在 `copilot/` 而不是 `copilot/searches/`**——放进去会被当成一次过期搜索，连文件带登记一起删。发新卡的顺序固定为「PATCH 旧卡组件 → 删旧登记 → merge 写新登记 → 发卡 → 记新 message_id」，反过来会出现「旧卡还能点、登记已指向新卡」的窗口 | `maa_copilot.CARD_BUTTON_PREFIX` / `CARD_PATH` + `maa_action.copilot_card()` + `CopilotCardTests` |
| 15 | 抄作业路径**三个降级开关（忽略练度要求 / 理智药 / 助战）一个都不许出现**，连字面量都不许写进任何脚本（D12 / D13：借了助战之后这关不能开代理，以后日常刷到它会执行不了）。只接受 `ratio == 1.0` 且**无软硬缺口**的作业，搜不到就报失败 | `_fight_one()` argv 构造 + `maa_copilot.pick_perfect_candidate()` + `CopilotDowngradeGuardTests`（全库 grep） |
| 16 | 森空岛/鹰角 token 永不经 Discord、argv、agent 上下文；`copilot/` 下文件 0600 | `maa_skland.py` stdin/环境变量注入 + 测试 |
| 17 | 抄作业关卡只接受本地 `stage_index` 精确查表，不许猜别名 | `valid_copilot_stage()` |
| 18 | 自动与手动都只接受完全满足练度且无软硬缺口的作业，搜索一律绕过 12h 缓存（新活动首日 prts 上的作业每小时都在长）。**每关开打前**复核活动 key 与实时理智（`refresh_roster()`），不是每轮一次 | `maa_copilot.pick_perfect_candidate()` + `_recheck_before_stage()` + `ManualCopilotPathTests` |
| 19 | 只有「查询成功且确实无活动」才允许清空活动关；格式不可辨认一律抛异常。闸门与解析器必须共用 `is_side_story_heading()`，不许各判各的。`activity_stages` 是运行时缓存：**`deploy.py` 不得用仓库种子覆盖线上值**，否则等于绕过本闸门写回一份过期活动关 | `activity_snapshot()` + `test_snapshot_gate_and_parser_agree_on_what_a_heading_is`；`deploy.PRESERVED_RUNTIME_CACHE_KEYS` + `test_deploy_keeps_live_runtime_caches_and_seeds_only_when_absent` |
| 20 | 收掉未打完的一轮必须连续 2 次空快照才发生，且发生时必发 Discord 通知 | `run_once()` 的 `EMPTY_OBSERVATIONS_BEFORE_IDLE` + `ActivityWatchTickTests.test_the_activity_ending_needs_two_empty_snapshots` |
| 21 | 森空岛失败文案必须带原始错误：`link` 修不了网络故障，不能让用户照着错的指引跑 | `fetch_roster()` 的 `detail` + `test_link_guidance_keeps_the_original_failure_cause` |
| 22 | Depot 只有退出成功、任务 Completed、存在 `done=true` 非空库存且模拟器清理成功时才可替换真值；过程回调、识别失败和清理失败都不得污染旧快照 | `maa_depot.run_depot()` / `parse_depot_output()` + Depot 回归测试 |
| 23 | Depot 不得在计划卡链路内运行；独立 cron 每日只做 7 天到期检查。任何日常抢锁失败都必须写 `last_lock_conflict_*`、发 Discord 并返回失败；`merge_last_run()` 必须持专用锁，不能用陈旧整份文件覆盖持锁任务的新状态 | `maa_depot.main()` / `maa_daily.report_lock_conflict()` / `maa_common.last_run_lock()` + 调度与锁冲突回归测试 |
| 24 | **每一条自动链路**（日常 `am`/`pm`、抄作业 `run-batch`、Depot）在拿到 `run.lock` 之后、`open_emulator()` 之前探测模拟器：在线即判定「用户正在玩」并跳过本次，且**必须发 Discord 并写 `last_run.json`**（日常写 `status=skipped` + `skip_reason=emulator_busy`，Depot 写 `depot_status="skipped_busy"` 且**不动 `snapshot_at`**，抄作业把这一轮放回 `scheduled` 让 watcher 稍后重试），文案要给出出路。日常的 `now` **永不**被探测跳过。探测**永不抛异常**（失败按离线处理照常执行）、总预算 `emulator_probe_timeout` 默认 5s，且**不得复用** `wait_for_android()`（180s）或 `settle_android_runtime()`（那是开机后的稳定性探测，用途不同）。**实现只允许有一份**，在 `maa_common.py`；`maa_daily` 那个同名函数只是薄封装 | `maa_common.emulator_is_online()` / `report_emulator_busy()` + `maa_daily.AUTO_PHASES` + `EmulatorProbeTests` / `EmulatorProbeSharedTests.test_a_busy_emulator_stops_the_copilot_round_before_it_opens_anything` |
| 25 | `state/config.json` 里**由线上进程写回的运行时缓存键**（当前 `maa_core_version`、`maa_cli_version`、`activity_stages`）必须列入 `deploy.PRESERVED_RUNTIME_CACHE_KEYS`：线上已有值一律保留，仓库种子**只在首次部署时兜底**。**新增此类键时必须同步加进这个元组**，否则每次 `deploy.py` 都会用种子盖掉线上真值（2026-09-02 已实际发生过一次） | `deploy.PRESERVED_RUNTIME_CACHE_KEYS` / `deploy_state()` + `test_every_known_runtime_cache_key_is_preserved_on_deploy`（手工清单比对，漏加即红）、`test_deploy_keeps_live_runtime_caches_and_seeds_only_when_absent` |
| 26 | 「刷理智」与「每日剿灭」是 `render_daily_toml()` 里**两个独立开关**（`plan["farming"]` / `plan["annihilation"]`，`tasks.fight` 仍是二者的总开关）。理智让路**只摘刷理智、永远保留剿灭**（周上限 1800 合成玉、代理卡驱动，摘掉纯亏）。**估算失败一律照常刷**——宁可多刷一场，也不能因为读不出数就一整天什么都不打 | `render_daily_toml` 的两个 `if` + `attach_sanity_yield()` + `SanityYieldTests` |
| 27 | 理智让路的判据**只有一条**：估算理智 < 默认关 `apCost`。森空岛不可用、关卡不在 `stages.json`、任何异常 → `farming = True`。让路时必须在开跑消息里写明理由（`farming_skip_reason`），不许静默 | `maa_daily.attach_sanity_yield()` + `SanityYieldTests.test_an_unreadable_estimate_farms_as_usual` / `test_a_throwing_probe_never_takes_the_farming_block_away` |
| 28 | 排轮前**必须先扣掉剿灭那份理智**（地基事实 H）；理智未知一律 0（fail-closed）。理智成本为 `None` 的条目**不入当轮、不进预算**（那不是「打完了」，是「算不出来」） | `maa_clearing.sanity_budget()` / `plan_round()` + `CopilotRoundPlanningTests` |
| 29 | 「已通关」有**两条独立判据**、是「或」的关系：本地记账所需模式齐全 **或** 森空岛该 zone 全清（D10）。`zone_fully_cleared()` 与 `zone_progress_warning()` **共用 `stage_zone()` 的定位、绝不合并结论**——后者是「只在能证明未解锁时才开口」的单向启发，沉默 ≠ 已解锁，永远不当闸门用 | `maa_copilot.stage_zone()` / `zone_fully_cleared()` / `ledger_pending_modes()` + `LedgerTests.test_zone_helpers_stay_two_separate_rules`（AST 结构断言） |
| 30 | 记账**只有 `won` 才写模式时间戳**；活动关的所需模式是普通 + 突袭，**突袭失败 → 该关不算通关**，下一轮只排剩下的模式（D7）。`activity_key` 变了要**归档**旧账（`ledger-<key>.json`）而不是删。种子（`SR-5/6/8` 普通）**只在前缀是 `SR` 且账本还没种过（`seeded` 标记）时写，且只补空位**——判据不能是「是不是首次建账」，账本常常被上一个活动的 tick 先建出来（2026-09-09 已实际发生）。同一 `(关, 模式)` 当天失败 / `unknown` 达到 `auto_copilot_max_attempts_per_day`（默认 2）次后当天不再自动排，次日归零 | `maa_copilot.record_copilot_attempt()` / `ensure_ledger()` / `seed_ledger()` / `attempts_today()` / `daily_capped_modes()` + `LedgerTests` / `CopilotDailyAttemptCapTests` |
| 31 | 材料缺口是**后台固定计算**，不是一个下拉（2026-09-08 删掉了原第三个下拉——缺口天天变，下拉只能选关、显示不了缺口，与第一个下拉语义重复）。计算结果由 `apply_auto_stage(result, phase)` 写进当天**那一场**的 `sessions.<phase>.stage`（**同一个字段**：`daily.toml` 只有一条刷理智链，两个字段必然产生「谁赢」的歧义），并打 `stage_source="auto"`，连带写下 `farming_drops` 与 `farming_fallback_stage`。**用户在某一场手动选过（`stage_source="discord"`）当天那一场就不许再自动改**，另一场不受影响；`stage_source` 必须留在 `maa_common.PLAN_SESSION_FIELDS` 白名单里，否则点一次「跳过」就把那一场的自动定关冻住。排序指标是**稀缺 / 缺口 / 效率三项加权分**（四期步骤 3，权重在 `config.farming_weights`，和为任意值都归一），不许再叠「库存低于 N 就优先」这类例外规则。**降级期照样自动定关**——2026-09-09 改写：三期不敢改是因为那时排序依据只有手工权重；现在候选集受活动 / 当日开放限制（D5），而且外部掉率数据给了效率与稀缺两项兜底，选出来的关是合理的。降级必须在卡片上标出来并列出原因，而且**降级期不写 `drops` 上限**（缺口是猜的，拿它当「刷够就停」会提前收工）| `maa_farming.farming_result()` / `auto_stage()` / `maa_plan_card.apply_auto_stage()` + `FarmingScoreTests.test_cold_start_uses_manual_weights_but_still_picks_a_stage` / `FarmingAutoStageTests.test_degraded_still_picks_a_stage_and_says_so`
| 32 | 材料推荐的**每一条降级路径都不抛异常**（异常会带走整张计划卡，D-2 的教训）且**必须可见**（进 `degraded_reasons`，卡片上列出来）：外部抓取失败退本地缓存、缓存也没有就当空数据；冷启动（`consumption_history` 不足 2 次对表）缺口项退手工权重；≥4 周窗口内零消耗的材料可用量是**未知**、同样退手工权重；`itemValue == null` 的材料不参与缺口计价；上游掉落表与 `farming_stage_table` 都查不到关卡的材料不推荐且卡片说明原因——**永不猜关** | `maa_farming.load_market_data()` / `farming_result()` / `farming_note()` / `maa_depot.consumption_rates_window()` + `FarmingScoreTests` / `FarmingFetchTests`
| 38 | 材料 → 关卡的映射**以上游 `stages.json` 的 `dropInfos` 为主**（`item_index.json` 翻中文名），`farming_stage_table` 只作补充：这是「新活动上线自动跟随」的地基。只认 `NORMAL_DROP`（按 `EXTRA_DROP` 定关，期望产出可能差一个数量级）。映射必须是**有序候选列表**、在候选关集合里取命中的——全局定死一个关会选中早已关闭的往期活动关（`异铁组 → DH-8`），然后被整条丢掉。候选里有掉率数据时按「理智/件」最便宜的挑，没有数据才退回启发式顺序（手工表指定的最优先 → 理智低的在前 → 关卡名字典序兜底）。资源本（`CE-6`/`AP-5`）在上游只有 `FURNITURE`，**只能**靠手工表，所以两张表是合并不是替换。**没有主掉落的关也留在表里**（`materials: []`）：`maa_farming` 要靠它把关卡代号对到内部 `stageId` 与 `apCost`。读取失败一律降级成空表，不抛异常（D-2） | `maa_plan_card.stage_drops()` / `material_stage_candidates()` + `FarmingAutoStageTests` / `FarmingScoreTests.test_a_priced_material_picks_the_cheapest_open_stage`
| 33 | 记账**只在这一关被判定为通关时**才写：`copilot_outcome()` 要求退出码 0 **且**无失败标记 **且**有胜利标记 `Copilot Completed`，认不出胜利标记返回 `unknown` 并按失败处理。只看退出码会让撤退 / 三星未达 / 正常退出的 Fight Error 被当成通关而**永久跳关**。胜利标记字符串**已于 2026-09-08 用真实日志核对**（`runs/2026-09-08-copilot-9e14e500ac25.log` 第 47 行）。判据的输入是「**这一关自己的那段输出**」：逐关标记必须 `flush()` 之后再把日志句柄交给子进程，切片按 flush 后的**字节偏移**（`_log_since()`）而不是按标记 `rsplit`——不 flush 的话标记排在这一关全部输出之后，切出来是空段、一律判成 `unknown`（2026-09-09 线上现象，§5.5 关键事实 9） | `maa_clearing.copilot_outcome()` + `maa_copilot_worker._fight_one()` / `_log_since()` + `CopilotVictoryJudgementTests` / `CopilotLogMarkerTests`（**用真实子进程**共享 fd，Python 侧写字符串的桩测不出来） |
| 34 | 一轮条目的突袭用 `#raid` 后缀表示，每关先普通后突袭；**除了 `maa copilot --raid` 的取值，理智查表 / 导航键闸门 / 作业匹配 / 状态展示一律用 `split_entry()` 去掉后缀的关卡名**。`--raid` 合法取值 `normal`/`raid`/`both`（`maa copilot --help` 核实）。**`--raid raid` 只挑作业，不切难度**：难度切换是 `switch_to_raid()` 单独一趟，所以 `copilot_raid_switch_task` 为空 = 突袭功能关闭，不是「跳过切换直接打」 | `maa_clearing.split_entry()` / `make_entry()` / `required_modes()` / `raid_enabled()` + `CopilotModeTests` / `CopilotTargetSetTests.test_a_raid_entry_is_gated_under_its_plain_stage_name` / `CopilotBatchExecutionTests.test_a_raid_entry_is_skipped_outright_when_the_switch_is_empty` |
| 35 | 一轮里**一关失败继续下一关**，只有 `nav`（上游导航 / 切突袭难度）这类环境失败才停止整轮；两种情况战报都必须**逐关列出结果**。普通没过就跳过同一关的突袭（那关根本没解锁） | `maa_copilot_worker._batch_run_locked()` + `CopilotBatchExecutionTests` |
| 36 | 一轮里每开新的一关前检查距下一场日常是否还够 `auto_copilot_min_lead_minutes`（默认 40，比 `copilot_blackout_minutes` 宽，因为它要覆盖整关而不只是启动），不够就收尾、剩下的留给下一轮。**「能不能开」的三条闸门（blackout / Depot 窗口 / 时间预算）只有一份实现**，都在 `maa_action.py`：watcher 派发前查一次，worker 每关开始前再查一次 | `maa_action.min_lead_reason()` / `_copilot_blackout_reason()` / `depot_window_reason()` + `CopilotTimeBudgetTests` / `CopilotDispatchGateTests` |
| 39 | **计划的覆盖是按场次的**：`plan.json` 的形状是 `{date, sessions: {am, pm}, updated_at}`，`effective_plan(at, phase)` 只叠 `sessions[phase]`。改早场绝不影响晚场（D3）。**`now` 永远叠不到任何场次**——没有写路径能产生 `sessions.now`（`plan set-stage` / `skip-today` 只接受 `am`/`pm`），所以手动场按定义走缺口默认关（D4）。磁盘上是旧的按天格式（顶层 `stage`/`skip`）时**读成空**、不迁移、不抛 | `maa_common.plan_sessions()` / `effective_plan()` / `maa_action._today_override()` + `PlanTests.test_session_overrides_apply_per_phase_and_expire_next_day` / `test_legacy_day_level_plan_reads_as_empty_and_never_raises` / `SessionDailyTests` |
| 40 | **晚场计划卡不跟随上游更新**（D1）：`brew` / `maa update` / `maa hot-update` 只在早场卡跑。晚场卡因此也没有「上游更新」字段，`main()` 末尾那个「更新失败就 raise」只在 `update is not None` 时判。计划卡**恰好两行控件**：一个 `stage-select` 下拉 + 一个「跳过本场 / 恢复本场」按钮；登记表里不许再出现 `explain` / `log` / `farming-select` / 抄作业下拉的键 | `maa_plan_card.main()` / `build_components()` + `PlanCardPhaseTests.test_the_evening_card_never_follows_upstream` / `DiscordCardTests.test_the_card_has_exactly_one_select_and_one_button` |
| 37 | **只跑上游 `tasks/Stages/<前缀>.json` 里已有顶层键的关**（前缀 = 关卡名第一个 `-` 之前；突袭条目按去掉 `#raid` 的普通关名查）。本项目**不再自写导航定义**，也**不读、不建** `~/.config/maa-cli/resource/tasks/Stages/`；只读 `maa_copilot._resource_dirs()` 给的上游目录，文件缺失 / 读失败 / JSON 坏 / 顶层不是对象**一律视为无键**（fail-closed，异常摘要进原因）。无键的关**不进目标集合、不进抄作业卡的下拉**，只在一览里标「⛔ 上游无导航」——它**永远不会自动变好**，等下去毫无意义，所以既不重试也不记失败 | `maa_clearing.nav_checker()` / `read_nav_keys()` / `maa_activity_watch.collect_targets()` + `CopilotTargetSetTests.test_a_stage_without_an_upstream_nav_key_never_enters_the_set` |
| 41 | 「已排程」卡**只有一个按钮**（❌ 取消本轮）。候选卡的 ▶️ / 🔥 / 🔄 与 `copilot pick` / `copilot again` 整套在四期步骤 2 删除：选关即执行，没有可挑的候选。登记表里不许再出现 `:raid-` / `:again` / `:confirm` 的键。一轮只在 `scheduled` / `queued` 可取消（`CANCELABLE_STATUSES`），`executing` 起理智可能已经花掉 | `maa_copilot.build_scheduled_card()` / `CANCELABLE_STATUSES` + `ButtonRegistryTests.test_the_scheduled_card_carries_exactly_one_cancel_button` / `test_no_module_still_builds_candidate_buttons` |
| 42 | **候选关集合受活动限制**（D5）：`StageActivityV2` 说活动还没结束时，`maa_farming.candidate_stages()` 只返回 `config.activity_stages ∩ 上游导航键`——理智 all in 活动才换得完代币商店，不刷主线也不刷常驻资源本。活动结束（截止时刻已过）当天就退回 `1-7` + 当天开放资源本；判「还在不在」**优先看日历而不是 `activity_stages`**（后者是运行时缓存，结束当天可能还留着上一期的关）。用户从下拉手动选资源本仍然允许——这条只管**自动定关** | `maa_farming.candidate_stages()` + `FarmingCandidateSetTests` |
| 43 | 外部数据（一图流 / 企鹅物流）的抓取层**任何失败都不抛**，只返回缓存或空并把原因写进 `degraded_reasons`，由计划卡显示；四份缓存落 `state/maa/farming/`（0600、`atomic_write_json`），`matrix.json` 走 **ETag 条件请求**（`max-age=7200` 不可信），`stage/info` 1h TTL、`item/v7/value` 24h TTL。统一 UA `maa-remote/4 (+仓库地址)`，卡片带一行「数据来源：明日方舟一图流 / 企鹅物流（CC BY-NC 4.0）」。**数据不 vendor 进仓库**（许可），**单测一条都不许碰网**——桩数据手写 | `maa_farming.load_market_data()` / `_fetch_cached()` / `SOURCE_LINE` + `FarmingFetchTests` |
| 44 | Fight 的 `drops` 上限**只在有实测缺口时写**（`maa_farming.drops_cap()`：冷启动、缺口 ≤ 0、拿不到 itemId 一律空表），itemId 必须过 `maa_common.SAFE_ITEM_ID_RE`（它会原样变成 TOML 表键）。**第二个 Fight 块永远不带 `drops`**，而且只在第一块有上限时才渲染——没上限的话第一块本来就打到理智不足。两个块的 `medicine/stone/series` 由 `_fight_block()` 一处写死，任务链顺序不变（紧挨着，在剿灭之后、基建之前）。战报里第二块的 key 是 `Fight2` 而不是 `Fight`（共用 key 会让没跑过的第二块被 `"Fight Completed"` 这条通配判成完成），掉落用 `farming_sections()` 合并两段 | `maa_daily.render_daily_toml()` / `_drops_table()` / `_fight_block()` / `FARMING_TASK_NAMES` / `farming_sections()` + `FarmingDropsTests` |
| 45 | 理智查表是**两级 fail-closed**：先上游 `stages.json`（`maa_copilot.stage_sanity_cost()`），拿不到再查一图流 `stage/info` 的**本地缓存**（`maa_farming.stage_ap_cost()`，**只读缓存、绝不发请求**——watcher 每 15 分钟一 tick，不能在这条路径上等网络）。两处都查不到仍然返回 `None`，条目不入轮（不变量 #28）。第二级是为 **EX 关**加的：上游 `stages.json` 里根本没有 `XX-EX-n` | `maa_activity_watch.cost_lookup()` + `FarmingExCostFallbackTests` |
| 46 | 肉鸽的执行路线是 **`maa run` 任务文件**（`type = "Roguelike"`，临时 `MAA_CONFIG_DIR`），不是 `maa roguelike` 子命令——CLI 缺 `BlackFlow`、`mode` 硬限 0..5、两处默认值与协议相反、Phantom 丢 `--difficulty`。渲染出的 params 必须含 **`starts_count = 1`**（每局一个子进程是「本局结束后再停」的全部实现）、`investment_enabled` / `stop_when_investment_full = true`、`core_char = ""`，**永不出现 `use_support` / `use_nonfriend_support`** | `maa_roguelike.resolve_params()` / `render_task_toml()` + `RoguelikeTaskFileTests` |
| 47 | **停止只在局间生效**：`roguelike stop` 只写 `stop_requested_at`（<20s 返回），worker 打完当前这一局、在派下一局之前才读它。**只有 `abort` 才碰子进程**（写 `abort_requested_at`，worker 每 2s 轮询到就 `terminate()`），并且必须在战报和回执里说明「当前局作废」 | `maa_action._request_roguelike_stop()` / `maa_roguelike_worker._run_locked()` + `RoguelikeWorkerTests.test_stop_between_runs_lets_the_current_one_finish` / `test_an_abort_ends_the_round_and_marks_it_dirty` |
| 48 | 肉鸽子进程**只许** `Popen` + `terminate()` + `wait(timeout)` + 万不得已 `kill()`，**不许 `subprocess.run(timeout=)`**：后者超时走 SIGKILL，MaaCore 来不及 destroy，模拟器会留在地图里。单局上限是 `roguelike_run_timeout`（默认 3600），整轮长度由 blackout / Depot 窗口约束 | `maa_roguelike_worker._run_one_exploration()` + `RoguelikeSubprocessTests`（AST 断言全模块没有 `subprocess.run`） |
| 49 | **`dirty` 时不许换主题**：上游 `<Theme>@Roguelike@Begin` 的善后链只在同主题下成立，跨主题会死循环（issue #13245）。一局开跑**之前**就先落盘 `dirty = true` + `last_theme`（进程被杀也留得住），只有 **`Roguelike Completed` 且退出码 0** 才清掉——只看退出码会让被中断的一局被当成收过尾的 | `maa_roguelike.dirty_theme_conflict()` / `parse_run_output()` + `RoguelikeCardTests.test_a_dirty_run_refuses_to_switch_theme` / `RoguelikeOutputTests.test_only_completed_plus_exit_zero_counts_as_clean` |
| 50 | 肉鸽卡用 `hermes:btn:maa-roguelike:` 前缀：它**不在** `hermes:btn:maa:` 之下（07:50 那趟整段清扫碰不到），也**不在** `hermes:btn:maa-copilot:` 之下（`prune_expired_searches()` 碰不到）。发新卡的顺序与 #14b 相同：「PATCH 旧卡组件 → 删旧登记 → merge 写新登记 → 发卡 → 记新 message_id」。**换主题必须重发卡**——模式 / 难度 / 分队的选项都随主题变，留着旧卡等于让人从上一个主题的分队里挑 | `maa_roguelike.BUTTON_PREFIX` / `maa_action._send_roguelike_card()` + `RoguelikeCardTests.test_the_roguelike_prefix_survives_both_sweeps` / `test_changing_the_theme_reposts_the_card_and_drops_the_old_registry` |
| 51 | **配置里的字符串列表写进任务文件前必须过白名单**：`config.mall_buy_first` / `mall_blacklist` 由 `_mall_item_list()` 逐项过 `SAFE_MALL_ITEM_RE`（中日文 / 拉丁字母 / 数字 / `·`，≤20 字），不合规的项**静默丢弃**而不是抛异常（渲染器抛异常 = 整场日常没得跑）。同理布尔开关走 `_toml_bool()`，只有真正的 `true` 才写 `true`。渲染器不接受任何没校验过的外部字符串——`drops` 的表键早已这么办（`SAFE_ITEM_ID_RE`，不变量 #44） | `maa_daily._mall_item_list()` / `SAFE_MALL_ITEM_RE` / `_toml_bool()` + `AwardAndMallOptionTests` |
| 52 | **空 `activity_key` 永远不动活动账本**：非活动关（资源本 / 主线）的记账落 `ledger-standalone.json`，`ensure_ledger("")` 既不归档也不新开活动账本，`ledger_path()` 与 `ensure_ledger()` 必须拿同一个 key 算落点。原来空 key 会把当期账本归档 + 新开空账本，下一 tick watcher 再归档回来，真实通关记录散进归档文件 | `maa_copilot.ledger_path()` / `ensure_ledger()` + `maa_copilot_worker._batch_run_locked()` + `LedgerTests.test_a_non_activity_stage_never_archives_the_running_ledger` / `CopilotBatchExecutionTests.test_a_non_activity_round_writes_only_the_standalone_ledger` |
| 53 | **练度后备快照读不到的字段一律按「不满足」**（fail-closed，D12）：`roster.source == "operbox"` 时技能与模组要求记**硬**缺口。后备只在 `skland_unavailable()` 有话说时才写，落**单独的** `roster-operbox.json`（绝不覆盖森空岛那份），`load_roster()` 取 `fetched_at` 更新的一份，所以森空岛一恢复它就退场。OperBox 失败不得影响 Depot 的库存真值 | `maa_copilot.load_roster()` / `check_op_requirements()` / `maa_depot.skland_unavailable()` / `run_operbox()` + `OperBoxFallbackTests` / `OperBoxRosterTests` |
| 54 | **手动场只由选关卡派发**（`PLAN_MANUAL_RUN.md`）：「现在跑」只发卡不开跑；`run start <关卡> <token>` 只由登记表触发，token 必须与 `manual_run.json` 匹配、`pending` 且未过期（过期就地收卡，这是硬闸门，过期哨兵只是 UI 清理）；`maa_daily.py now <token>` 再核对一次 `dispatched` + 同 token，对不上不跑、发红色告警。手动关**不进 `plan.json`**（`_today_override()` 只认 am/pm）、不带 drops 上限和后备关。选关卡用 `hermes:btn:maa-now:` 前缀，不在 `hermes:btn:maa:` / `hermes:btn:maa-copilot:` 之下，两趟清扫都碰不到；发新卡顺序同 #14b。定时场优先：距 08:00/22:00 不足 `manual_run_blackout_minutes`（默认 30）既不发卡也不派发。缺口推荐（森空岛查询）只在 `run recommend` 的脱离会话子进程里算，绝不进 20s 的 `run now` / `run start` | `maa_action.run_now()` / `run_start()` / `run_expire()` / `run_recommend()` / `manual_run_blackout_reason()` + `maa_daily.manual_run_stage()` + `ManualRunCardTests` |

---

## 9. 日常开发流程

```bash
# 1. 改代码
# 2. 跑测试（必须在仓库根目录）
python3 -m unittest discover -s tests -v
# 3. 部署（自动备份）
python3 deploy.py
# 4. 若改了 adapter/config.yaml/.env/SKILL.md
hermes gateway restart
# 5. 真机验证（cron run 是在下一个调度 tick 触发，不是立即执行）
hermes cron run 03d5e98ce8d4          # 早场计划卡（workdir=…/maa/am）
python3 ~/.hermes/scripts/maa_action.py plan show     # 一次念完两场
hermes cron run 50cd1fb095f2          # 完整早场（会真的开模拟器、真的耗理智）
```

### 单测必须离线可跑

**整套单测不许碰网络**，一条都不许。曾经有一条 `test_search_copilots_respects_cache_ttl`
在验缓存 TTL 时顺手调了真实的 prts.plus，结果整套测试既慢（15～59s）又会被对方
一次 SSL EOF 判红——「全绿」这个验收标准就此不可信。现在网络层由
`tests/test_copilot.py: prts_http_stub()` 桩掉，整套 2.4s。

**一图流 / 企鹅物流那条出口在 `tests/test_maa.py` 的 `setUpModule()` 里被整个封死**
（模块级 patch 掉 `maa_farming._http_json`）。这一层不是洁癖：抓取层默认把缓存写进
**线上** `~/.hermes/state/maa/farming/`，一条漏了打桩的用例既会打对方接口，又会往运行时
状态里塞一份带假时间戳（用例的 `at=`）的缓存，把线上数据冻到那个未来时刻。
需要自己造响应的用例在 `with patch.object(maa_farming, "_http_json", …)` 里覆盖它；
其余用例一律传 `data=`。**断网复核抓不到这一类**——抓取层吞掉所有异常只降级，
不会让测试变红，所以这道模块级闸门是唯一的防线。
（另：`_cache_age()` 把「未来的 `fetched_at`」一律当成不新鲜，写坏的缓存最多多抓一次。）

改完测试用这条**断网复核**，必须仍然全绿：

```bash
python3 -c "
import socket, ssl, unittest
def blocked(*a, **k):
    raise RuntimeError('network access attempted during unit tests')
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.create_connection = blocked
socket.getaddrinfo = blocked
unittest.main(module=None, argv=['nonet', 'discover', '-s', 'tests'])
"
```

> 注意是 patch `socket.socket.connect` 而不是 `socket.socket` 本身——
> 后者会把 `ssl.SSLSocket` 的继承搞崩，测试还没跑就 ImportError。

**线上契约冒烟**（默认跳过，故意要连真网的那一条）：

```bash
MAA_LIVE_TESTS=1 python3 -m unittest tests.test_copilot -k LiveContract -v
```

验的是 prts.plus 仍然返回 `prts_query()` 能解析的信封与字段
（`id` / `content` / `available` / `rating_ratio` / `views`，以及 `content` 里的
`stage_name`）。**桩数据改了就该跑一次这个**，确认桩还对得上真实响应。
不要把它放进 CI 或默认套件。

**单测不能替代真机验收。** 图像识别、连战倍率选择、基建自动排班这些只有实机能验。
需要跑完整链路又不想耗理智时，把 `config.json` 的 `fight_times` 临时设成 `0`
（Fight 立即达限但任务链完整），验完**务必**改回 `2147483647`。
`config/maa-cli/tasks/infrast-smoke.toml` 是单独验证七类设施实时自动排班的诊断任务。

调试期间想避免定时任务插进来：`hermes cron pause <id>`，完事 `hermes cron resume <id>`。

---

## 10. 排障：症状 → 看哪里

| 症状 | 先看 |
|---|---|
| 到点什么都没发生，频道无任何消息 | `run_lock` 被占（上一场没退干净）→ `pgrep -fi bluestacks`；或 cron 被 pause |
| 场次不到 1 秒就 `return_code=1`，`runs/` 里连 `.log` 都没生成 | 失败发生在 `require_executable()`，比 `open_emulator()` 还早 → `ls /Applications/BlueStacks.app`。整个 bundle 不见了就是 §11.12 的自杀式清理 |
| 频道有开跑消息，子区进度不动 | `runs/<date>-<phase>.log` 尾部；多半卡在客户端更新/登录页，50 分钟后会以 124 终止并告警 |
| 场次 30～40 秒就红，日志只有五行且有 `TouchModeNotAvailable`，截图是模拟器桌面 | 没连上模拟器，MaaCore 在 StartUp 里 SIGSEGV 了 → §11.14。看 `last_run.connect_retries` 确认重试有没有接上；`com.loong.maa/crash.log` 是第二现场 |
| 战报 ❌ 但任务都是 ✅ | `cleanup_errors` 非空也会判失败——看 Embed 备注栏和 `last_run.cleanup_errors` |
| 点按钮/下拉没反应或报未登记 | 卡片过期（07:50 与 21:50 各清一次 `hermes:btn:maa:` 前缀并重写，所以 21:50 之后早场卡必然点不动）；或 gateway 没重启；或 `buttons.json` 被别的写入方覆盖 |
| 早场改的关晚上也生效了 / 改一场把另一场也改了 | 看 `plan.json` 的 `sessions`：是不是被写成了旧的顶层 `stage`。写路径只有 `maa_action._save_override(phase, …)` 和 `maa_plan_card.apply_auto_stage(result, phase)` 两处 |
| 卡片上没有「活动截止」这一行 | 是设计：`~/Library/Caches/com.loong.maa/StageActivityV2.json` 读不出来就整行不显示。跑一次 `maa activity Official` 让它刷出来，或用 `config.maa_activity_cache` 指到别处 |
| 说「抄作业」发的卡下拉是空的 | 一览里每关都标了「⛔ 上游无导航」——下拉只放上游写了导航定义的关（不变量 #37）。等热更新带来导航定义，或改抄资源本 / 主线 |
| 日常/抄作业/Depot 报「模拟器正在使用」 | 探测命中了（不变量 #24）。`pgrep -fi bluestacks` 看是不是真有残留进程；确实是残留就 `pkill` 掉再重试。日常想强行跑就说「现在跑」 |
| 卡片「为什么」写着「⚠️ 降级：… 抓取失败」 | 一图流 / 企鹅都没连上。`ls -l ~/.hermes/state/maa/farming/` 看缓存的 `fetched_at` 有多旧；本机手验：`curl -sI https://cos.yituliu.cn/arknights/stage-drop/matrix.json` 与 `curl -sS -X POST https://backend.yituliu.cn/item/v7/value -H 'Content-Type: application/json' -d '{}' \| head -c 200`。**抓不到不会让卡片消失**，只会让稀缺 / 效率两项退化 |
| 活动期间卡片说资源本「不在本场候选集里」 | 这是设计（D5 / 不变量 #42）：活动期理智 all in 活动关。想刷资源本就从下拉手动选，手动选的关不受这条限制、也不带 `drops` 上限 |
| 一场日常里只看到一个 Fight 段 | `runs/<date>-<phase>.json` 里 `plan.farming_drops` 是不是空的。冷启动、缺口 ≤ 0、或者用户手动选过关时**本来就不写上限**，第二个块也就不渲染（不变量 #44） |
| 战报里「刷理智·续」永远是「⏸️ 未执行」 | 第一个块没打到 `drops` 上限（理智先耗完了），MaaCore 直接把第二块跳过——正常。真要确认看 `runs/*.log` 里有没有 `[刷理智·续]` 段 |
| EX 关排不进自动作业轮 | 理智算不出来（不变量 #28 fail-closed）。上游 `stages.json` 没有 EX 条目，兜底靠 `farming/stage_info.json` 缓存——但**一图流也不收 EX 关**（spike B §1），所以这条兜底现在只能捞到「一图流有、上游 `stages.json` 没有」的关，EX 仍是已知缺口 |
| 下拉里没有活动关 | `maa activity Official` 的输出变了 → 看 `activity_snapshot()` / `parse_activity_events()` 的分区解析与正则 |
| 新活动上线后没有「已排程」卡 | `hermes cron list` 看“MAA 活动作业监控”；再看 `activity_watch.json`：`status=done` 说明目标集合是空的（看 `excluded` 里每关的理由），`waiting` 说明这一轮排不出条目，`scheduled` 说明排出来了但派发闸门没过 |
| 「已排程」卡发了但一直不开跑 | `activity_watch.json` 的 `status` 停在 `scheduled`。原因按 `dispatch_blocker()` 的顺序看：还没到 `scheduled_at`（等理智）/ blackout / Depot 窗口 / `run.lock` 被占 / 模拟器在线 |
| 某关一直不被自动抄 | 抄作业卡的一览就是答案：⛔ 上游无导航（**永远不会变好**，只能手动打或等热更新）、🚫 练度不满足（缺什么写在括号里）、✅ 已通（记账或森空岛 zone 全清）。`copilot/ledger.json` 是记账真值 |
| 战报里某关 ❓ 未确认 | `copilot_outcome()` 返回 `unknown`：退出码 0 但没有 `Copilot Completed`。**不记通关**，下一轮会重打。日志文件名写在记账 `attempts[].detail` 里。上游改措辞了就改 `maa_clearing.COPILOT_VICTORY_MARKERS` |
| 战报全 ❓、记账全 `unknown`，日志里却明明有 `Copilot Completed` | **看标记行 `# <时刻> copilot <关> mode=…` 是不是排在这一关的输出之后**。是的话就是逐关标记没 `flush()` 就把句柄交给了子进程（§5.5 关键事实 9 / 不变量 #33），切出来的「本关输出」是空段。2026-09-09 03:30 那轮就是这样，连带触发了 30 分钟退避后的重打 |
| 某关今天不再被自动排、卡上写「🚫 今日已尝试 N 次」 | 当日上限命中（`auto_copilot_max_attempts_per_day`，默认 2）。次日自动恢复；想现在就重打，从抄作业卡的下拉手动选它（手动路径不受这条限制）。`copilot/ledger.json` 的 `attempts` 是计数依据 |
| 突袭那次总是失败 | 现状：`copilot_raid_switch_task` 默认空串 = **突袭功能关闭**，正常不会排突袭。要重新启用先看 `runs/<date>-copilot-<token>.log` 里 `# raid-switch` 那段有没有 `Custom Completed`；2026-09-09 实测是 `Custom Error`（详情页上根本没有突袭控件），详见 §5.5 第 3 条 |
| 卡上写「✅ 已通（突袭功能未启用）」 | 不是缺陷：`copilot_raid_switch_task` 为空，这一关只按普通难度判通关。想补突袭要先把开关配好并真机验一关（清单在 `docs/PLAN_PHASE4.md`） |
| 点了「肉鸽停」还在跑 | 这是设计（不变量 #47）：停止只在**局间**生效，当前这一局会打完。要立刻停就点「⏏️ 立即中止」——那一局作废。看 `roguelike.json` 的 `stop_requested_at` 有没有写进去 |
| 肉鸽开始被拒「上次 X 主题没有干净结束」 | `roguelike.json` 的 `dirty = true`（不变量 #49）。用**同一个主题**跑一局，上游自己会 `Continue → ExitThenAbandon` 收掉残局，干净结束后 `dirty` 清零，就能换主题了 |
| 肉鸽一局都没跑就收尾了 | 战报的「停止原因」是答案：blackout（距 08:00 / 22:00 不足 `copilot_blackout_minutes`）、Depot 对表窗口（京 04:20–05:00）、模拟器在用。肉鸽不耗理智，所以**不会**是理智问题 |
| 肉鸽说「已经在跑了」但频道里什么都没发生 | worker 进程没了。`roguelike.json` 的 `status` 停在 `running` 而 `run.lock` 是空的、且 `updated_at` 超过 10 分钟时，再点一次「▶️ 开始」就会按新的一轮重开（`round_is_stale()`）。**不要手改状态文件**——等这 10 分钟或直接再点一次 |
| 肉鸽一直点空 / 进错主题 | 手册要求把目标主题**置顶在终端**，自动导航「不保证长期可用」（社区 issue #13245 就是界园被认成萨卡兹）。先在游戏里置顶，再看 `runs/<date>-roguelike-<token>.log` |
| 自动确认提示理智读取失败 | 森空岛凭据已过期或 API 变化；只在本机终端重新跑 `maa_skland.py link`，不要把 token 发到 Discord |
| 森空岛挂了之后作业全被判「练度不满足」 | 这是设计（不变量 #53）：练度退到了 `copilot/roster-operbox.json`，它读不到技能专精与模组，凡是有这两项要求的作业一律算不满足。`maa_skland.py link` 修好森空岛，下一次刷新后备就自动退场 |
| 森空岛挂着，`roster-operbox.json` 却一直不出现 | 后备只在 **Depot 那趟**（每周一次，京 04:30）顺手跑。看 `last_run.depot_operbox_note`：没有这行说明 `skland_unavailable()` 判定森空岛还能用（`roster.json` 还没到 `skland_roster_refresh_days`），有这行就写着失败原因 |
| 手动抄了一次资源本，活动记账好像丢了 | 先看 `copilot/` 下是不是多了 `ledger-<key>.json` 归档。这是 2026-09-09 修掉的隐患（不变量 #52）：空 `activity_key` 会把当期账本归档。修完之后非活动关记在 `ledger-standalone.json`，活动账本不受影响 |
| 信用商店没按 `mall_buy_first` 买 | 先看渲染出的 `~/.config/maa-cli/tasks/daily.toml` 里 `buy_first` 是不是空的：白名单（`SAFE_MALL_ITEM_RE`）会**静默丢弃**带引号、换行、超长（>20 字）的物品名。名字要和游戏里一模一样 |
| 聊天改计划无效 | 看 agent 是否真的调了 `maa_action.py`；`plan.json` 的 `date` 是否是今天 |
| 战报里步骤全是「未执行」 | maa-cli 输出格式变了 → 改 `_task_state()` / `TASK_STEPS` |
| 截图失败 | ADB 断了或返回非 PNG；不阻断主流程，看 Embed 备注 |
| 战报说「截图不是游戏主页」 | 看 `runs/<date>-<phase>.png` 到底停在哪一页；`last_run.home_verify_detail` 有命中了几个标识。UI 改版导致误杀就调 `HOME_MARKERS` |
| `返回主界面` 报 Error 但截图确实在主页 | `maa update` 后 QuickSwitch 任务图变了 → 翻 `asst.log` 里 `taskchain":"Custom"` 的 `to_be_recognized`，对照资源 json 看少了哪一项 |
| 基建在制造站或宿舍停住，截图显示“撤下宿舍锁定位置的干员”或“预设队列中的干员撤下” | 检查 profile 的 `user_resource = true`，以及 `~/.config/maa-cli/resource/tasks/tasks.json` 是否部署；诊断任务应出现 `InfrastDormDoubleConfirmed` 后继续到 `Infrast Completed` |
| 计划卡红「MAA 上游更新失败」，`last_run.resource_update_detail` 里是 `SecureTransport error: connection closed via error; class=Net (12)` | 失败的是 `maa hot-update`（MaaResource 的 git fetch），不是 `maa update`——判据：detail 里**没有**「MaaCore 更新失败，未更新热更新资源」那一行，说明 core 那步过了。根因见 §11.18：`cli.toml` 的 `backend` 必须是 `git`，`libgit2` 会绕开代理直连 GitHub。手动复现：`MAA_CONFIG_DIR=~/.config/maa-cli maa hot-update --batch -vv` |
| `deploy.py` 报 “no longer matches baseline” | Hermes 升级动了 adapter → 按 §7 更新补丁串 |

现场排查常用：

```bash
cat ~/.hermes/state/maa/last_run.json
ls -lt ~/.hermes/state/maa/runs/ | head
tail -100 ~/.hermes/state/maa/runs/<date>-<phase>.log
python3 -c "import json;print(json.load(open('$HOME/.hermes/state/agent-book/buttons.json')).keys())"
```

---

## 11. 已知坑

1. **BlueStacks 需要活跃图形会话**——锁屏/关显示器没问题，注销或切换用户会失败。
2. **`hermes update` 会覆盖 adapter 补丁**——升级后必须重跑 `deploy.py` 并复验按钮。
3. **Beta MaaCore 的连战重识别**：有限 `times` 下改选倍率后偶发识别不到，可能多起一批。
   生产用 `times=2147483647` + `series=0` 规避（README「本机落地状态」有实测记录）。
4. **本机 ADB 是 BlueStacks 自带的 `hd-adb`**（Homebrew 那份校验和对不上未装），
   `config.json` 和 maa-cli profile 都指向它，换模拟器时两处都要改。
5. **macOS 上 maa-cli 的默认配置目录是 `~/Library/Application Support/com.loong.maa/config`**，
   与本项目约定不同，所以每个子进程都要显式注入 `MAA_CONFIG_DIR`——新增调用 maa-cli 的代码时
   一定要用 `maa_subprocess_env()`，不要裸 `subprocess.run`。
6. **删子区需要 bot 的 Manage Threads 权限**，没有就只能靠 `auto_archive_duration=1440` 归档。
7. **MaaCore 资源目录与配置目录不同**：`maa update` 把资源装到
   `~/Library/Application Support/com.loong.maa/resource`（Tile-Pos/battle_data.json 在那），
   与 `MAA_CONFIG_DIR` 无关。`maa_resource_dir` 配错了抄作业关卡索引就是空的。
8. **maa-cli 任务文件是 `[[tasks]]` 数组**（不是 `[tasks.X]` map），0.7.5 会直接 TOML 报错。
9. **日常 `maa run` 要剔除代理；`maa update` 必须带着代理**：
   `maa_disable_proxy` 只对脚本的运行类子进程生效。手动复现日常任务时用
   `env -u ALL_PROXY -u HTTPS_PROXY -u HTTP_PROXY ... maa run ...`，否则 in-run hot update
   连代理会报错。计划卡的 `maa update` 相反，要走本机常开代理访问 GitHub——2026-08-16
   那次就是剥掉代理后 `github.com` 超时，失败还按 7 天间隔算一次，拖到下一周才重试。
   **注意这条只覆盖 `maa update`**：紧跟其后的 `maa hot-update` 是 git fetch，
   带不带代理由 `cli.toml` 的 `backend` 决定，与环境变量无关，见 §11.18。
10. **接管弹窗**：`maa copilot` 不处理「确认要接管作战吗？」，靠 worker 的 ocrbox watchdog；
   手动复现问题时弹窗确认按钮在正文下方约 100px（1920×1080）。
11. **MAA 没为该关写导航定义**：上游每个活动只写 4 个左右推荐关的导航定义，
    `Stages/<前缀>.json` 里没有的关根本进不去——是预期行为不是 bug，
    **原因通常不是"关卡未解锁"**。这样的关既不进自动目标集合、也不进抄作业卡的下拉，
    只在一览里标「⛔ 上游无导航」（不变量 #37）。出路是等热更新带来导航定义、
    自己在游戏里手打、或改抄资源本 / 主线（2026-09-05 决定：不自写导航定义）。
12. **动 BlueStacks 三件套里的任何一个，整套会被连坐删除**——安装包附带 LaunchDaemon
    `/Library/LaunchDaemons/com.now.gg.BlueStacks.cleanup.plist`，`WatchPaths` 盯着
    `/Applications/BlueStacks.app`、`/Applications/BlueAI.app`、
    `/Applications/BlueStacks Air multi-instance manager.app`。任一路径变动即触发
    `/Library/Application Support/BlueStacks/BlueStacksCleanup`，而该脚本的判定是
    「三个里少任何一个」就把三个连同 `/Users/Shared/Library/Application Support/BlueStacks`
    全部 `rm -rf`，收尾还会删掉自己和那个 plist（所以事后翻 `LaunchDaemons` 什么也找不到）。

    2026-08-04 22:17:55 用 mole 卸载捆绑的 BlueAI，就这样把模拟器和实例数据一起清空了；
    `/var/log/bst_cleanup.log` 留有带时间戳的完整记录，是排查这类「模拟器凭空消失」的第一现场。

    - **症状**：场次 0.x 秒内 `return_code=1`、`runs/<date>-<phase>.log` 根本没生成、
      `last_run.thread_id=null`、战报七项全「未执行」。因为 `adb_path` 指向
      `/Applications/BlueStacks.app/Contents/MacOS/hd-adb`（见 §11.4），bundle 一没
      `require_executable()` 就抛，比 `open_emulator()` 还早一步。
    - **要卸 BlueAI 就先拆引信**：`sudo launchctl bootout system
      /Library/LaunchDaemons/com.now.gg.BlueStacks.cleanup.plist` 并删掉该 plist，再动 app。
      直接 `mo uninstall` / 拖废纸篓都会引爆。
    - **重装不恢复实例数据**，得重走一遍：`brew install --cask bluestacks`（pkg 装机要 sudo，
      无 TTY 时用 `open` 调 GUI 安装器）→ 重建实例（4 核 / 4GB / **1920×1080 横屏**，
      默认实例是 1080×1920 竖屏，横屏不改 MAA 必然识别不到）→ 装国服 APK → 重新登录。
    - **国服 APK 官方下载链**：`https://ak.hypergryph.com/downloads/android_lastest` → 302 →
      `https://launcher.hypergryph.com/game/latest/GzD1CpaWgmSq1wew/1/1` → 302 → `ak.hycdn.cn`
      上的 `arknights-hg-<版本>.apk`（2.7.61 约 2.0GB）。装前可验签名者应为
      `CN=HyperGryph, O=HyperGryph, L=Shanghai`：
      `unzip -p <apk> META-INF/ARKNIGHT.RSA | openssl pkcs7 -inform DER -print_certs -text`。

13. **代理指挥失误会被 MaaCore 报成「掉落理智」，而且 Fight 仍判 Completed**——
    代理失败时游戏结算页写「本场战斗已视为单次作战结算，未进行作战的理智已返还」，
    并把返还的理智画成一个道具图标。MaaCore 照单全收，日志里出现
    `Drops: 理智 × 105`，汇总成 `total drops: 理智 × 315`；`Fight <关卡> N times`
    里的 N 数的是**发起**的连战批次，不是真打完的次数。

    2026-08-07 早场 TO-9 就是这样：连着 4 批代理失误、理智 115 → 117（净消耗 0、
    一关没打成），旧战报却写成「⚔️ 刷理智：✅ 完成（16 次；掉落 理智 ×315）」。
    用户的作战录像跟不上关卡随机性时就会这样。

    - **判据**：掉落项里出现「理智」= 代理失误。理智永远不可能是关卡掉落，
      所以这个信号是确定的，不需要额外 OCR。见 `SANITY_REFUND_ITEM`。
    - **战报**：`_drop_pairs()` 把「理智」剔出掉落；`fight_wiped_out()` 命中
      （有失误批次且零真掉落）时 Fight 翻红、写「实际完成 0 次」、总体判 ❌，
      并在备注里提示去手动重打三星刷新录像。
    - **熔断**：`run_maa()` 每 `PROGRESS_INTERVAL_SECONDS` 采样一次末尾**连续**失误
      批次，达到 `proxy_failure_limit`（默认 2）就终止 `maa run`，返回
      `PROXY_BREAK_CODE = 125`。中间有真掉落的批次会把连续计数清零，不因随机性误伤。
    - **补跑**：熔断掐掉的是整条链，所以 `_salvage_run()` 会用去掉 Fight 的
      `daily.toml` 再跑一趟把基建/公招/商店/奖励捞回来（StartUp 顺手把卡在结算页的
      游戏带回主界面）。两趟共用同一个 `maa_run_timeout` 预算，剩余不足
      `proxy_failure_salvage_min_seconds`（默认 300s）就跳过；
      `proxy_failure_salvage: false` 可整体关掉。日志落在
      `runs/<date>-<phase>-salvage.log`，不覆盖第一趟。
    - **未实机验收**：熔断与补跑的判定逻辑有单元测试（`ProxyFailureTests`、
      `CircuitBreakerTests`、`SalvageTests`，并用 2026-08-07/08-06 两份真实日志回放
      过解析），但「StartUp 能否从结算页把游戏带回主界面」只在真机上才算数，
      下次遇到代理失误时要盯一眼补跑那趟的日志。
    - **主关空跑失败 → 改刷后备关**（2026-09-09 早场加）：SR-8 的代理指挥没解锁，
      `[刷理智]` 段 `Error` 收尾、一颗理智没花，MaaCore 接着把基建 / 公招 / 商店 / 奖励
      跑完了，`farming_fallback_stage = SR-7` 却没人用，94 理智白挂到晚上。现在
      `execute()` 在 `return_code != 0` 且 `fight_failed_dry()`（段里没有
      `Fight <关> n times`、没有掉落、`[刷理智·续]` 没跑过）时调 `_fallback_fight_run()`：
      `fallback_fight_plan()` 只保留 Fight、`annihilation = False`、块名用
      `FARMING_TASK_NAMES[1]`（战报自然落在「刷理智·续」那一行，不带 drops）。预算同样
      从 `maa_run_timeout` 里扣，剩余不足 `fight_fallback_min_seconds`（默认 300s）就跳过，
      `fight_fallback_on_error: false` 整体关掉；日志落在 `runs/<date>-<phase>-fallback.log`
      （`last_run.salvage_log` 复用同一个字段）。打了几批之后才 Error 的**不补**——
      那是别的问题。测试：`FightFallbackTests`（含 09-09 早场真实汇总段回放）。
14. **MaaTouch 握手超 3 秒 → MaaCore SIGSEGV，整场 35 秒暴毙**——这一条和代理失误
    （§11.13）毫无关系，是连模拟器都没连上，游戏里一个键都没点过。

    MaaCore 起 MaaTouch 用的是 `app_process`，等于在安卓里现开一个 ART 虚拟机，
    而它给这次握手的窗口**写死 3 秒**（`call_and_hup_minitouch`）。超时就写
    `unable to find $ from pipe_str:`、抛 `TouchModeNotAvailable`，`Controller::connect`
    返回 false。真正致命的是下一步：**maa-cli 不看这个返回值**，照样
    `Assistant::start()`，`StartUpTask` 拿着没连上的 controller 直接
    `SIGSEGV (Segmentation Fault)`，`maa run` 退 1。

    2026-08-08 早场就是这样没的：08:00:41.105 起 MaaTouch → 3.001s 没回话 →
    08:00:44.106 `TouchModeNotAvailable` + `connect failed` + SIGSEGV，35 秒结束，
    截图停在 BlueStacks 的应用商店首页（游戏根本没启动）。

    - **判据**：`runs/<date>-<phase>.log` 只有五行且含 `TouchModeNotAvailable`，
      没有任何 `X Completed`；`~/Library/Application Support/com.loong.maa/crash.log`
      多出一对 `SIGSEGV` / `Unhandled exception`；截图是模拟器桌面而不是游戏。
      MaaCore 的原始现场在 `debug/asst.log` 里搜 `probe_minitouch | leave`。
    - **它确实是偶发**：整份 asst.log 里历史握手全在 165–395 ms，只有出事那次是
      3001 ms，而 6 秒后同一台机器上 `maa closedown` 的握手只花了 166 ms。
    - **触发条件是模拟器冷启**：`sys.boot_completed=1` 只代表系统起来了，不代表安静。
      2026-08-08 实测冷启：`boot_completed` 在 +7.5s，`app_process` 探针在 +0…+4.3s
      全是 35 ms，**+5.4s 突然卡 2.8s**，+9.2s 恢复；而当天 runner 正好在
      `boot_completed` 后约 5.5s 把场子交给了 MaaCore。同一天二次热启则全程无抖动。
    - **两层修法**（`maa_daily.py`）：
      1. `settle_android_runtime()`——`verify_game_installed` 之后、`maa run` 之前，
         用同样走 `app_process` 的只读命令 `am get-current-user` 探路，要求连着
         `emulator_settle_quiet_seconds`（默认 8s）都快过
         `emulator_settle_slow_seconds`（默认 1.0s）才放行。**卡一次就把安静计时清零**
         ——只数「连着几次快」会正好在抖动前开门。`emulator_settle_timeout`（默认 40s）
         用尽也不阻断，只在战报备注里留一句，因为下面那层兜得住。
      2. `run_maa_resilient()`——`connect_failed()` 认出这一类失败就重开 `maa run`，
         默认 `connect_retry_limit: 2` 次、间隔 `connect_retry_delay: 8` 秒。
         安全闸门是**日志里没有任何一条任务链 Completed**：只要有任务真跑过就绝不重开，
         免得把已经做完的事再做一遍。124/125/130 是我们自己写的返回码，一律不重试。
         所有重试共用同一个 `maa_run_timeout` 预算，日志**追加**进同一个
         `runs/<date>-<phase>.log`，不覆盖上一趟的现场。
    - **实机验收（2026-08-08）**：冷启下 `wait_for_android` 在 +9.2s 返回，闸门又花
      8.5s，+17.7s 才交给 MaaCore，此后探针稳定在 40 ms——正好跨过了那个抖动窗口。
      重试那一层只有单元测试（`ConnectFailureTests`、`ConnectRetryTests`，用当天的真实
      五行日志回放），因为这个崩溃没法按需复现；下次再撞上要盯一眼第二趟是否接上。

15. **BlueStacks 的“有可用更新”弹窗会卡死自动启动**——宿主进程已经打开，`open -a`
    也返回成功，但弹窗关闭前 Android 虚拟机根本不会启动，ADB 5555 因而持续拒绝连接。
    `Player.log` 的本次 session 会停在 `/bs3/check_upgrade`，看不到正常启动必有的
    `[QTUI] Start Virtual Machine`。`maa_daily.py` 超时后会据此给出明确诊断，并把原始
    启动错误写入 `last_run.startup_error`；关闭弹窗后 VM 会立即继续启动，无需重装实例。

16. **MaaCore 包与 MaaResource 热更新层必须同步更新**——MaaCore 启动时会用
    `MaaResource` 覆盖包内资源。只跑 `maa update` 可能留下旧 `infrast.json`，它再
    引用新包中已经改名的模板，导致 MaaCore 在真正连模拟器前直接退出。
    计划卡因此在 MaaCore 更新成功后紧接着跑 `maa hot-update --batch`；任一步
    失败都会红卡报错，不再把半更新状态当成可用。

17. **拿相邻顶层键当 YAML 锚点会写坏 config.yaml，代价是全部 cron 提前 8 小时**——
    旧 `patch_config_yaml()` 把频道提示词插在 `timezone: Etc/UTC` 之前，假定它紧跟
    `channel_prompts:`。2026-09-03 Hermes 重排了键顺序，`timezone` 落到了 `skills:`
    之后，于是那行 6 空格缩进的提示词插进了 `skills:` 块里，YAML 解析失败。
    Hermes 静默回退默认配置 → `timezone: Etc/UTC` 失效 → cron 全按本机 `+08:00` 解释，
    每个 job 都提前 8 小时跑。修法见 §7 的锚点说明；判断线上是否中招：
    `hermes cron list` 的 `Next run` 带 `+08:00` 而不是 `+00:00`。

17b. **Hermes 会把频道提示词重排成无引号、折成两行的 plain scalar**——
    2026-09-09 步骤 1 部署时踩到：线上那条变成了

    ```yaml
          '<频道>': 你是 MAA 长草助手。……必须遵守 maa-operator
            技能并通过 maa_action.py，……
    ```

    旧 `patch_config_yaml_text()` 按「单行带引号」整行匹配旧文案，找不到就抛
    「已有非预期文案」，部署直接停住。现在改成：**用解析后的值**（`_discord_section()`
    给的 `current`）与 `OLD_MAA_CHANNEL_PROMPTS` 比对来判断要不要换，换的时候由
    `_replace_channel_prompt()` 定位该 channel_id 的键行、把**缩进更深的续行一起吃掉**，
    整条替换成新的单行带引号条目。写盘前后各解析一次那道校验一个字没改。

18. **`maa hot-update` 的 libgit2 后端完全无视 `HTTP_PROXY` / `HTTPS_PROXY`**——
    §11.9 只说对了一半：`maa update` 走 reqwest，认代理；`maa hot-update` 走
    `cli.toml` 的 `[resource] backend`，而 **libgit2 后端既不读环境变量代理、也没有
    git config 代理可读**（`MaaResource` 仓库和全局 git config 里都没有 `http.proxy`），
    于是直连 `github.com/MaaAssistantArknights/MaaResource.git`。本机网络下这条直连
    时好时坏，坏的时候就是
    `Error: SecureTransport error: connection closed via error; class=Net (12)`。

    2026-09-07、09-08 连着两天的 07:50 红卡就是这个：`maa-cli 已是最新` + `maa update`
    静默成功（退出码 0、零输出），只有 hot-update 这一步炸了。**辨认方法**：
    `check_and_update_upstream()` 在 core 失败时会把「MaaCore 更新失败，未更新热更新资源」
    写进 detail；detail 里没有这句，失败的就是 hot-update。

    **2026-09-09 实测判据**（把代理指到一个关闭的端口 `127.0.0.1:9`）：

    | backend | 假代理 | 真代理 |
    |---|---|---|
    | `libgit2` | 照样 `Already up-to-date`（**说明压根没走代理**） | 成功 |
    | `git` | 秒失败 `Failed to connect to 127.0.0.1 port 9` | `Already up to date.` |

    **修法**：`config/maa-cli/cli.toml` 的 `backend` 改成 `"git"`（合法取值只有
    `git` / `libgit2`，填别的 maa-cli 直接 panic）。`git` 后端 shell out 到 `git`
    二进制，环境变量代理照单全收；`git` 找不到时 maa-cli 会自己回落到 libgit2
    （字符串 `Failed to execute git, falling back to libgit2 backend`），不会更糟。
    **改完必须 `python3 deploy.py`**：`cli.toml` 是 `--check` 逐字节守住的 5 个文件之一，
    不部署就一直 `mismatched`，而且线上跑的仍是旧的 libgit2。
