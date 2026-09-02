# 开发指南（基于当前代码实现）

面向要改这套东西的人。[PLAN.md](./PLAN.md) 是设计意图，[README.md](./README.md) 是功能与落地状态，
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
   │ 50 23 * * *  maa_plan_card.py     │      │ 组件交互 hermes:btn:maa:*    │
   │ (京 07:50)                        │      │   → buttons.json 查登记      │
   │  0  0 * * *  maa_daily.py  am     │      │   → exec 白名单校验          │
   │ (京 08:00, workdir=…/maa/am)      │      │   → maa_action.py（≤20s）    │
   │  0 14 * * *  maa_daily.py  pm     │      │                              │
   │ (京 22:00, workdir=…/maa/pm)      │      │ 自然语言（免 @ 频道）        │
   └───────────────────────────────────┘      │   → maa-operator 技能        │
                                              │   → terminal 调 maa_action.py│
                                              └────┬─────────────────────────┘
                                                   │ 只写 plan.json / 派发
                        ┌──────────────────────────┘
                        ▼
   ~/.hermes/state/maa/{config,defaults,plan,last_run}.json + run.lock + runs/
                        │  effective_plan() = defaults ⊕ 当天的 plan 覆盖
                        ▼
   maa_daily.py ── flock ── 快照 runs/<date>-<phase>.json ── 起 BlueStacks
        └─ 渲染 daily.toml ─ maa run daily ─ 5s 一次编辑子区消息
                                 └─ 截图 ─ closedown/退出/断 ADB ─ 战报 embed
```

五条 cron 全是 `--no-agent --deliver local` 的**短命进程**：跑完即退，不常驻，
stdout 不投递到 Discord（消息由脚本自己用 Discord REST API 发）。

---

## 2. 组件清单

| 文件 | 角色 | 触发方式 | 典型耗时 |
|---|---|---|---|
| `scripts/maa_common.py` | 无第三方依赖的公共层：时区、三把锁、原子写、Discord REST/multipart、清理、`prune_button_registry` | 被其他几个 import | — |
| `scripts/maa_plan_card.py` | 每日计划卡；每次先检查/跟随 maa-cli 与 MaaCore；刷新活动关；写按钮登记；清理旧子区/旧卡片/旧产物 | cron 京 07:50 | 数秒～5 分钟（含 `maa update` / `brew`） |
| `scripts/maa_depot.py` | 每周仓库对表；只接纳 MaaCore `done=true` 的完整 Depot 结果，计算本地掉落增量与账号消耗率 | cron 京 04:30 每日检查，满 7 天才执行 | 到期时数分钟；未到期不启动模拟器 |
| `scripts/maa_activity_watch.py` | 轮询 MAA 活动视图；活动切换时派发 Top-1 作业搜索，维护重试/取消/完成状态 | cron 每 15 分钟 | 通常 <1s；需要搜索时后台 5~20s |
| `scripts/maa_daily.py` | 执行主体：模拟器 → maa-cli → 流式进度 → 截图 → 清进程 → 战报 | cron 京 08:00 / 22:00；`run now` 派发 | 10～50 分钟 |
| `scripts/maa_action.py` | **唯一**状态变更与派发入口（按钮、下拉、聊天共用） | Discord 组件交互；agent 用 terminal 调用 | < 1s（硬性要求） |
| `scripts/maa_copilot.py` | 抄作业领域层（纯函数）：关卡索引、prts 客户端、匹配打分、缺口文案、候选卡片 | 被 action/worker import | — |
| `scripts/maa_copilot_worker.py` | 抄作业后台：`search <token>` 搜索发卡；`run <token> <n> <mode>` 真执行 | 仅由 `maa_action.py` 派发 | 搜索 5~20s；执行 8~15 分钟 |
| `scripts/maa_skland.py` | 森空岛协议：stdlib HMAC 签名、grant→cred 链、练度与理智拉取、`link`（stdin 收 token） | 本机终端；worker 懒刷新或执行前强制刷新 | 秒级 |
| `scripts/ocrbox.swift` | macOS Vision OCR 小工具（编译为 `~/.hermes/scripts/ocrbox`），watchdog 识别接管弹窗 | 被 worker 调用 | <1s/次 |
| `deploy.py` | 幂等部署 + 给 Hermes 打本地补丁 + 备份 + 编译 ocrbox | 人工 | 秒级 |
| `skills/games/maa-operator/SKILL.md` | 约束 LLM 只能走 `maa_action.py` 的护栏 | gateway 按频道绑定加载 | — |
| `tests/test_maa.py` + `tests/test_copilot.py` | stdlib 单测，不碰模拟器 | 人工 | 约 10s |

---

## 3. 链路一：计划卡（`maa_plan_card.py`，京 07:50）

`main()`（`scripts/maa_plan_card.py`）严格按这个顺序：

1. **`check_and_update_upstream()`** — 每次计划卡都跑：`brew outdated/upgrade` 跟随
   maa-cli，再依次用 `maa update --batch` 和 `maa hot-update --batch` 更新 Beta
   MaaCore 与 MaaResource 热更新层（超时分别 180s / 300s）。
   **这条必须保留本机代理**（`maa_subprocess_env(..., keep_proxy=True)`），否则直连
   GitHub 会超时。有新版本就装上，把版本写回 `config.json`，摘要进计划卡「上游更新」
   字段。失败发红色 Embed 告警，并在 `main()` 末尾 `raise` 让 cron 记为失败。
2. **`refresh_activity_stages()`** — 每天都跑 `maa activity Official`，解析出形如 `TO-9`
   的活动关列表写回 `config.json`；同时检测公告里的「资源收集」字样，命中则置
   `all_supply_stages_open=true`（全部资源本开放）。
3. **重新 `load_config()`** — 上一步刚改过磁盘上的 config，卡片必须从落盘后的真值渲染。
4. **`build_components()`** — 组装刷图/抄作业两个下拉 + 三个按钮，同时产出登记表条目。
5. **写按钮登记（关键）** — 在 `state_lock()` 里 **先删所有 `hermes:btn:maa:` 前缀的 key，
   再 merge 写回** `~/.hermes/state/agent-book/buttons.json`。这个文件是**全局共享**的，
   agent-book 的按钮也在里面，**绝不能整体覆盖**。删前缀的副作用是旧卡片上的
   MAA 控件立即失效（点了会报未登记）。
6. `cleanup_old_threads()` 删 >24h 的子区、`cleanup_run_files()` 删 >14 天的 `runs/` 产物。
7. `retire_old_plan_cards()` — 扫最近 100 条消息，把标题为 `🌱 MAA 今日计划` 且仍带
   components 的旧卡片 `PATCH components: []`，避免同时存在多张可点的卡。
8. 发新卡片。

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
"hermes:btn:maa:stage-select": {
  "kind": "exec", "script": "maa_action.py",
  "select_args": { "choice:0": ["plan","set-stage",""], "choice:1": ["plan","set-stage","1-7"], … }
}
```

adapter 补丁只做「精确查表」，Discord 传回的值**永远不会直接变成命令行参数**；未知值、
多值一律拒。改下拉相关代码时必须保持这个性质（对应测试
`test_adapter_patch_dispatches_only_registered_select_values`）。

`🤖 请 Hermes 解释` 是唯一 `kind:"prompt"` 的按钮——它起一个只读的 agent 会话，不是 exec。

---

## 3.5 链路一半：活动上线自动备好作业（`maa_activity_watch.py`）

watcher 每 15 分钟调用 `query_activity_snapshot()`，解析 `maa activity Official` 的活动标题与
推荐关。`activity_watch.lock` 防止两次 cron 重叠；一个轮换只取 MAA 输出的第一推荐关，避免
多关并发撞 `copilot_search.lock`。

判定「空窗」还是「格式变了」只有一个判据：`is_side_story_heading()`，闸门与
`parse_activity_events()` 共用它，两边不可能对「什么算分区标题」有分歧。三种结局：

| 输出 | 结论 |
|---|---|
| 有活动关分区标题行 | 正常解析 |
| 没有标题行，但有解析器认识的其它 `Opening …` 分区 | **查询成功的活动空窗** → 空快照 |
| 标题文本出现了、却不是完整一行（被装饰/缩进/换行） | **格式已变** → 抛异常 |
| 连一个认识的分区都没有 | 同上，抛异常 |

标题下方的缩进是有语义的（区分活动标题与关卡条目），所以缩进过的标题算格式变化，不做归一。

收卡是单向操作，因此要**连续 `EMPTY_OBSERVATIONS_BEFORE_IDLE`（2）次**读到空快照才真的
清空活动关、进入 idle 并撤掉旧卡；第一次只记数，不落盘、不动卡。真收卡时会发一条
「活动已结束」通知——撤掉用户手里的卡不允许无声发生。活动 identity 真正变化时，
旧搜索在 `state_lock()` 内原子改为 `superseded` 并同步删登记，旧 worker 即使稍后返回也不能
覆盖新状态或重新挂回按钮。watcher 若在派发 child 后、写 `activity_watch.json` 前崩溃，下一轮会
按 `activity_key + stage` 接管已落盘的孤儿搜索，不会再发第二张卡。

新观察通过 `maa_action.start_copilot_search(auto_confirm=True)` 进入现有搜索 worker：prts 客户端
过滤、森空岛练度打分都不复制。自动搜索强制刷新 roster，并以 `use_cache=False` 绕过手动搜索
的 12 小时作业缓存，所以新活动首次暂无作业时，后续重试能看到上游刚上传的作业。候选必须同时
满足 `ratio == 1.0`、`gaps == []`、`soft_gaps == []`，然后只展示 Top-1 普通作业与
**确认 / 取消**；自动路径不做“按热度且练度未校验”的降级。

搜索抢锁、prts 暂无完全满足的作业或资源尚无理智消耗时进入 waiting，默认每 30 分钟重试；
同一错误只告警一次。取消会把本轮记为 dismissed，活动不变就不再打扰；成功或失败执行后
worker 写执行终态，watcher 统一记 completed。

关卡索引同时扫描 MaaCore 自带 resource 与 `MaaResource/resource` 热更新覆盖层，基础资源先读、
覆盖层后读；否则活动已经被 `maa activity` 看见、导航图却还只在覆盖层时会永远匹配失败。

活动与理智按三个安全点复核：`stages.json` 按 stageId 精确查理智消耗、复刻时按 code 精确兜底；
按钮点击先现场读取活动，并按最近一次森空岛快照加自然恢复估算当前理智；worker 抢到 `run.lock`
后、打开模拟器前再次读取活动并强制刷新森空岛理智；导航到编队界面后、真正调用 `maa copilot`
前还会再做一次相同的实时复核。活动已轮换、消耗未知、API 失败或理智不足都明确拒绝。

确认后的底层状态依次是 `queued → preparing → executing → executed/execution_failed`。`queued` 是
动作脚本已原子接单但 worker 尚未拿锁，`preparing` 已过第一轮复核但还没碰模拟器，`executing`
从准备打开模拟器开始持续到战报终态。确认按钮只在最后一次进战复核通过后才撤掉；在此之前，
作业下载、可执行文件、模拟器、ADB、导航或复核失败，只要还没有调用 `maa copilot` 且清理成功，
就恢复为 `done`，原卡可以再点。战斗一旦开始，或清理失败导致是否安全重跑不确定，就写
`execution_failed`，绝不自动重跑。

`queued/preparing` 超过 20 分钟且 `run.lock` 已空闲，说明 worker 在触碰战斗前失联：watcher 或下一次
手动点击会原子恢复为 `done`。`executing` 超过运行上限且锁已释放时只能写
`execution_failed` 并撤掉按钮，不能猜测这场是否已经消耗理智。

`activity_watch.json` 是面向轮换的粗状态：`idle`（无活动）、`searching` / `waiting`（搜索或退避）、
`ready`（待确认）、`preparing`（确认后进战前）、`executing`、`dismissed`（本轮取消）、
`completed`（本轮成功或失败已有终态）。`searches/<token>.json` 保存上面更细的执行状态。

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
   结束。**不开模拟器。** skip 只关掉 08:00/22:00 两个自动场次；手动场是用户当场下的
   明确指令，本身就是对今天这条计划的推翻，照跑（`run_now()` 的回执会提示这一点）。
5. **预检 → 起模拟器** — `require_executable` 校验 maa/adb 可执行（**在开模拟器之前**）→
   `open -a BlueStacks` → 轮询 `adb connect` + `getprop sys.boot_completed`（超时 180s）→
   `pm path com.hypergryph.arknights` 确认国服客户端已装。任一步失败且此时还没建子区，
   走 `send_direct_failure()` 直接在频道发红色告警。
6. **发开跑消息 → 建子区 → 发进度占位消息**，thread_id 记进 `last_run.threads`（供 24h 清理）。
7. **渲染 `daily.toml`** — `render_daily_toml(plan, config)` 把本场快照渲染成 TOML 并
   `atomic_write_text` 覆盖 `~/.config/maa-cli/tasks/daily.toml`。任务链固定：

   `StartUp → [当日第一场 Annihilation] → Fight(1-7) → Infrast → Recruit → Mall → Award → Custom(Home)`

   `StartUp` 本身已经复用 MaaCore 的首登处理链：登录后会处理签到/今日配给等弹窗，
   不是另起一条自定义点击任务。剿灭复用第二个 `Fight(stage = "Annihilation")`：
   每个国服游戏日（04:00 刷新）只进一次，由 `last_run.annihilation_game_day` 记帐；
   MaaCore 从右上角待办进入当期剿灭，自动点「全权委托」消耗代理指挥卡，每次结算
   OCR 周进度，满 1800 合成玉就停。两个 Fight 都硬编码 `stone = 0`。默认理智关
   是 `1-7`，当天仍可通过计划卡临时覆盖。

   `Award` 固定启用 `award = true` 与 `mail = true`，每场都会尝试领取日常、周常和
   邮箱内全部可领取附件；MAA 在没有可领取内容时正常结束。

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
| `plan set-stage <关卡>` | 白名单校验后写今日覆盖 |
| `plan skip-today on\|off` | 写今日覆盖 |
| `plan show` | 只读 |
| `run now` | 抢锁探测 → 预检 → `Popen` 派发 `maa_daily.py now` 后立即返回 |
| `log tail` | 读 `last_run.summary`；若是旧版格式则回读 log 用 `extract_summary` 重建 |

关卡白名单 `valid_stage()`：空串 ✔ / `^[A-Za-z0-9]{1,8}-\d{1,2}$` ✔ /
在 `config.activity_stages` 里 ✔ / 否则**现场跑 `maa activity Official` 并按词边界精确匹配**
（超时只给 **0.7s**，因为整体预算是 20s）。其余一律拒。

写状态时的两个细节：
- `_today_override()` 只保留 `date/stage/skip/source/updated_at` 五个键，其余字段丢弃；
  日期不是今天就整个重置。所以 `plan.json` 天然「今天说的话明天失效」，不需要清理任务。
- `is_run_locked()` 决定确认话术：正在跑就明确回「不打断本场，X 生效于……」。

`emit()` 把输出压在 7900 字节内——组件运行器只接受 ≤8192 字节的 JSON。

### 技能这一层为什么必要

`SKILL.md` 是给 LLM 的护栏：禁止 `write_file` 直接改状态文件、禁止调 `maa_daily.py`、
碎石永久关闭、原样转述脚本返回的 `message`。**但它只是第一道防线**——即使 LLM 完全无视技能，
`maa_action.py` 的白名单和 `render_daily_toml` 里的硬编码字面量仍然拦得住。
写代码时请始终假设「提示词会失效」。

---

## 5.5 链路四：抄作业（`maa_copilot.py` + `maa_copilot_worker.py`）

二期新增的按需链路。**互斥、不并行**：与日常刷理智共用 `run.lock`，且 08:00/22:00 前
`copilot_blackout_minutes`（默认 25）内 `copilot pick` 直接拒绝。

```
计划卡第二下拉 / 聊天「抄 TO-9」
   → maa_action.py copilot find TO-9     ~10ms（读本地 stage_index，零网络）
       ├─ 写 searches/<token>.json {status:pending}（token=12 位 hex）
       └─ Popen(maa_copilot_worker.py search <token>) 后立即返回
                    worker search（5~20s，flock copilot_search.lock 防并发）
       ├─ prts 搜索：limit=1 探针 → document=code/stage_id → level_keyword
       │   （filtered.total < 全量 total 才可信，否则扫最新 8 页）
       ├─ 客户端过滤 content.stage_name ∈ {stage_id, code}——永不信服务端
       ├─ 练度匹配（roster 缺失→降级模式）+ 排序 → Top-3
       ├─ state_lock() 下：清过期搜索 → merge hermes:btn:maa-copilot:* 键
       └─ 发候选卡片（▶️/🔥 按钮 + ❌取消 + 🔄换一批）
   用户点 ▶️ → maa_action.py copilot pick <token> <n> <mode>   ~50ms
       ├─ 校验 token/index/mode/candidate id + blackout + run.lock 预检
       └─ Popen(worker run <token> <n> <mode>) 后立即返回
                    worker run（8~15 分钟）
       ├─ run_lock(blocking=False) 抢锁，抢不到恢复确认卡并发「未启动」通知
       ├─ 渲染 tasks/copilot-nav.toml（StartUp + Fight times=0 导航）——单作业
       │   copilot 不会自己从主界面导航，必须先停在编队界面（C3 实测结论）
       ├─ 模拟器 → maa run copilot-nav（导航失败=关卡未解锁/不可达，直接报错）
       ├─ maa copilot <uri> --formation --batch -v --raid <mode>
       │   [--ignore-requirements 仅当 ratio < 1.0]
       │   └─ watchdog：每 2s OCR 屏幕，出现「确认要接管作战」弹窗就点确认
       │      （代理指挥开关开着时点开始行动必弹窗，MaaCore 不处理）
       ├─ 截图 → close_everything() → 战报 embed（含部署干员/进战斗次数）
       └─ runs/<date>-copilot-<token>.{json,log,png}，并入 last_run.json
```

### 抄作业的关键事实（C3 实机验证，2026-08-02）

1. **单作业 `maa copilot` 从主界面跑不起来**——任务链没有 StageNavigationTask，
   必须从编队选择界面开始。解法：先跑 `Fight times=0` 的任务（完整导航链但不开打）
   把游戏停在编队界面。资源本（CE-6 等）导航固定且快（~15s）；活动本在章节地图里
   滑动找节点，**关卡未解锁时找不到节点会报错**——这是预期行为，报告会说明。
2. **代理指挥开关**：点开始行动时代理开着会弹「确认要接管作战吗？」，MaaCore 的
   copilot 链不处理 → 必失败。worker 内置 watchdog（`ocrbox` OCR + ADB tap）在弹窗
   出现时点确认。正常路径（游戏新会话代理默认关）watchdog 不会触发。
3. **prts 的 `-1` 是「无要求」哨兵**：requirements 的 elite/level/skill_level/module
   为 -1 时视为无要求（实测踩坑：module=-1 曾导致 `module_levels[-1]` 越界）。
4. **prts 服务端索引滞后**：当期活动关 `level_keyword` 会静默退化（total=全量），
   必须用探针比对 + 客户端过滤兜底（对应测试 `test_level_keyword_degradation_*`）。
5. **作业 stage_name 两种写法并存**（内部 id / 展示码）：展示码的作业执行时从
   `/copilot/get/<id>` 取全文、改写 `stage_name` 为 stageId 后经 `file://` 传入。
6. **解锁预检是单向的**（`zone_progress_warning`）：森空岛的 zone 进度含剧情关、
   `stage_id` 序号只数战斗关——两把尺子。因此只有 `cleared < seq`（突袭必锁）和
   `cleared < seq-1`（关卡必锁）两个方向成立；**有警告 = 基本锁着，没警告 = 不知道**，
   绝不能在别处把「没警告」当成「已解锁」的依据（尤其不能拿去 gate 执行按钮）。
   序号超出第一个 zone（act17side 型多 zone 活动）一律静默；警告带森空岛快照时效
   标注（roster 7 天才懒刷新，进度却天天变）。

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
  归一化进 `roster.sanity`。候选卡可按 6 分钟/点估算缓存后的自然恢复，但自动作业真正执行前
  必须再次调用 `refresh_roster()`，缓存估算不能替代最终真值。
- 名字解析：skland 响应只有 charId；干员名→charId 用本地 `battle_data.json` 的
  `chars` 字典（`names` 键不存在，C0 实测）。只解析到 `token_*`/`trap_*` 的视为
  召唤物、不计缺口。
- token 只从 stdin 进（`link` 子命令）或 `SKLAND_HG_TOKEN` 环境变量进（deploy），
  永不经 argv/Discord/agent 上下文；`skland.json`/`roster.json` 0600。
- cred 过期（10002/HTTP 401）→ 用存的 `hg_token` 重跑 grant，**只重试一次**，
  再失败记 `last_error` 提示重新 `link`，绝不循环。

---

## 6. 状态模型

```
~/.hermes/state/maa/
  config.json     环境与阈值（路径、超时、channel_id、活动关缓存）    ← 计划卡会改写
  defaults.json   持久默认：stage="1-7" / medicine=expiring / tasks 全开
  plan.json       今日覆盖：{date, stage, skip, source, updated_at}
  last_run.json   最近一次运行状态 + threads[] + 资源更新时间戳
  activity_watch.json 当前活动观察 + 搜索 token + idle/searching/waiting/ready/
                      preparing/executing/dismissed/completed 状态
  depot.json      最近完整 Depot 真值 + 期间掉落增量 + 历次消耗与日均消耗率
  run.lock        flock 独占锁（执行）
  last_run.lock   flock 独占锁（保护 last_run.json 的跨进程 read-modify-write）
  am/ pm/         空目录，仅用于给 cron 传场次
  runs/<date>-<phase>.{json,log,png}   本场快照 / 完整日志 / 截图（保留 14 天）

~/.hermes/state/agent-book/
  buttons.json    全局共享按钮登记表（MAA 只碰 hermes:btn:maa:* 前缀）
  state.lock      共享写锁
```

**计划的求值**（`effective_plan()`，`scripts/maa_common.py`）：

```
result = normalize_plan(defaults.json)
if plan.json.date == 今天(Asia/Shanghai):   只叠加 stage 和 skip 两个键
result = normalize_plan(result)             ← 再规范化一次
```

`normalize_plan()` 每次都**强制** `medicine="expiring"`、`stone=0`，
即使磁盘上被人手工塞了别的值也没用（对应测试
`test_persisted_medicine_choice_cannot_disable_fixed_expiry_policy`）。

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
python3 deploy.py --check                  # 检查脚本/ocrbox 与 6 个 maa-cli 配置是否存在，并逐字节核对其中 5 个
```

做的事：

1. **备份** — 任何将被修改的文件，先按原路径复制到
   `~/.hermes/backups/maa-setup/<YYYYMMDDTHHMMSS>/`。
2. **幂等写** — `write_bytes()` 内容一致就跳过（不写、不记 changed、不备份），
   否则 `mkstemp + fsync + os.replace` 原子替换，权限 0600/0700。
   `--check` 对仓库管理的 6 个 `config/maa-cli/**` 文件查存在，并对其中 5 个逐字节比对
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
     注意仓库里的 `activity_stages` 会盖掉线上缓存，下一次计划卡会重新刷。
     `maa_core_version` / `maa_cli_version` 以本机已写入的为准，避免种子版本盖掉计划卡刚探测到的真值。
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
hermes cron create "50 23 * * *" --name "MAA 计划卡"  --script maa_plan_card.py --no-agent --deliver local
hermes cron create "0 0 * * *"   --name "MAA 日常·早" --script maa_daily.py --no-agent --deliver local --workdir ~/.hermes/state/maa/am
hermes cron create "0 14 * * *"  --name "MAA 日常·晚" --script maa_daily.py --no-agent --deliver local --workdir ~/.hermes/state/maa/pm
```

cron 表达式是 **UTC**（`config.yaml: timezone: Etc/UTC`），所以 Depot 的 `20:30 UTC` 是
次日北京时间 `04:30`；脚本内部一律用 `Asia/Shanghai`。
改时间时两边都要想清楚。当前在册：`3ba48fb85d0b`（活动作业监控）、
`b2f15f13589f`（仓库对表）、`03d5e98ce8d4`（计划卡）、`50cd1fb095f2`（早）、
`4f48cf619d9f`（晚），可用
`hermes cron list` 复核。

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
| 5d | `skip` 只挡自动场次；`phase == "now"` 永远照跑 | `execute()` 的 skip 分支带 `and phase != "now"` |
| 6 | 按钮脚本必须 < 20s 返回 | `run_now()`/`copilot_*` 用 `Popen`；活动关探测 0.7s |
| 7 | `buttons.json` 只能 merge，且只碰 `hermes:btn:maa:` 前缀 | `maa_plan_card.main()` |
| 8 | Discord 下拉值必须经 `select_args` 查表，不能直通 argv | adapter 补丁 + `_stage_select()`/`_copilot_stage_select()` |
| 9 | 原始 CLI 文本不进 Discord | `progress_content` / `extract_summary` / `_copilot_summary` 是唯一出口 |
| 10 | 开跑后快照即冻结，改计划不影响本场 | `runs/<date>-<phase>.json` + 内存 plan |
| 11 | 所有 maa-cli 子进程显式注入 `MAA_CONFIG_DIR` | `maa_subprocess_env()`（macOS 默认目录不是 `~/.config/maa-cli`） |
| 12 | 日常 `maa run` 剔除代理；`maa update` / brew 保留代理以便访问 GitHub | `maa_subprocess_env(keep_proxy=…)` |
| 13 | 抄作业与日常互斥：同一时刻只有一场 MAA（`run.lock`）；08:00/22:00 前 `copilot_blackout_minutes` 内拒绝 pick | `copilot_pick()` 预检 + worker `run_lock(blocking=False)` |
| 14 | 抄作业按钮用 `hermes:btn:maa-copilot:` 前缀（连字符），早清扫 `hermes:btn:maa:` 碰不到；过期由 `prune_expired_searches()` 清理 | `maa_copilot.BUTTON_PREFIX` + `maa_plan_card` 锁块 |
| 15 | 抄作业路径永不传 `--use-sanity-potion`（`--ignore-requirements` 只在用户点的候选 `ratio < 1.0` 时出现） | `_run_locked()` argv 构造 + 测试断言 |
| 16 | 森空岛/鹰角 token 永不经 Discord、argv、agent 上下文；`copilot/` 下文件 0600 | `maa_skland.py` stdin/环境变量注入 + 测试 |
| 17 | 抄作业关卡只接受本地 `stage_index` 精确查表，不许猜别名 | `valid_copilot_stage()` |
| 18 | 活动自动作业只给完全满足练度且无软硬缺口的 Top-1 普通模式；自动搜索绕过 12h 缓存，开模拟器前和进战前都复核活动与实时理智 | watcher 状态机 + `cmd_search()` + `copilot_pick()` + `_require_auto_run_preflight()` |
| 19 | 只有「查询成功且确实无活动」才允许清空活动关；格式不可辨认一律抛异常。闸门与解析器必须共用 `is_side_story_heading()`，不许各判各的 | `activity_snapshot()` + `test_snapshot_gate_and_parser_agree_on_what_a_heading_is` |
| 20 | 收掉用户手里的作业卡必须连续 2 次空快照才发生，且发生时必发 Discord 通知 | `run_once()` 的 `EMPTY_OBSERVATIONS_BEFORE_IDLE` + `test_watcher_enters_idle_on_recognized_empty_snapshot` |
| 21 | 森空岛失败文案必须带原始错误：`link` 修不了网络故障，不能让用户照着错的指引跑 | `fetch_roster()` 的 `detail` + `test_link_guidance_keeps_the_original_failure_cause` |
| 22 | Depot 只有退出成功、任务 Completed、存在 `done=true` 非空库存且模拟器清理成功时才可替换真值；过程回调、识别失败和清理失败都不得污染旧快照 | `maa_depot.run_depot()` / `parse_depot_output()` + Depot 回归测试 |
| 23 | Depot 不得在计划卡链路内运行；独立 cron 每日只做 7 天到期检查。任何日常抢锁失败都必须写 `last_lock_conflict_*`、发 Discord 并返回失败；`merge_last_run()` 必须持专用锁，不能用陈旧整份文件覆盖持锁任务的新状态 | `maa_depot.main()` / `maa_daily.report_lock_conflict()` / `maa_common.last_run_lock()` + 调度与锁冲突回归测试 |

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
hermes cron run 03d5e98ce8d4          # 计划卡
python3 ~/.hermes/scripts/maa_action.py plan show
hermes cron run 50cd1fb095f2          # 完整早场（会真的开模拟器、真的耗理智）
```

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
| 点按钮/下拉没反应或报未登记 | 卡片过期（每天 07:50 会清前缀重写）；或 gateway 没重启；或 `buttons.json` 被别的写入方覆盖 |
| 下拉里没有活动关 | `maa activity Official` 的输出变了 → 看 `activity_snapshot()` / `parse_activity_events()` 的分区解析与正则 |
| 新活动上线后没有确认卡 | `hermes cron list` 看“MAA 活动作业监控”；再看 `activity_watch.json` 与对应 `copilot/searches/<token>.json`。waiting 会按 `activity_search_retry_minutes` 自动重试 |
| 自动确认提示理智读取失败 | 森空岛凭据已过期或 API 变化；只在本机终端重新跑 `maa_skland.py link`，不要把 token 发到 Discord |
| 聊天改计划无效 | 看 agent 是否真的调了 `maa_action.py`；`plan.json` 的 `date` 是否是今天 |
| 战报里步骤全是「未执行」 | maa-cli 输出格式变了 → 改 `_task_state()` / `TASK_STEPS` |
| 截图失败 | ADB 断了或返回非 PNG；不阻断主流程，看 Embed 备注 |
| 战报说「截图不是游戏主页」 | 看 `runs/<date>-<phase>.png` 到底停在哪一页；`last_run.home_verify_detail` 有命中了几个标识。UI 改版导致误杀就调 `HOME_MARKERS` |
| `返回主界面` 报 Error 但截图确实在主页 | `maa update` 后 QuickSwitch 任务图变了 → 翻 `asst.log` 里 `taskchain":"Custom"` 的 `to_be_recognized`，对照资源 json 看少了哪一项 |
| 基建在制造站或宿舍停住，截图显示“撤下宿舍锁定位置的干员”或“预设队列中的干员撤下” | 检查 profile 的 `user_resource = true`，以及 `~/.config/maa-cli/resource/tasks/tasks.json` 是否部署；诊断任务应出现 `InfrastDormDoubleConfirmed` 后继续到 `Infrast Completed` |
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
10. **接管弹窗**：`maa copilot` 不处理「确认要接管作战吗？」，靠 worker 的 ocrbox watchdog；
   手动复现问题时弹窗确认按钮在正文下方约 100px（1920×1080）。
11. **关卡未解锁**：活动本按顺序解锁，没打前面的关时导航找不到节点、报「导航到编队界面失败」——
    是预期行为不是 bug；用户应先打前面的关或换资源本/主线。
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
