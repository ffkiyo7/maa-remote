# 手动场选关卡：「现在跑」先选关、10 分钟内不选就作废

> 写于 2026-09-09，四期结项之后。格式沿用 `archive/PLAN_PHASE4.md` 的单步模板（前置 / 目标 / 做法 /
> 允许改动 / 禁止触碰 / 验收边界），可以整步派给一个没有本次对话上下文的 agent。
> **本文不含实现代码。** 线上快照会过期，开工先按 `archive/PLAN_CLEARING.md` 全局护栏 9 对现场。

## 进度

- [ ] 步骤 M — 手动场选关卡（本文唯一一步）—— 🚧 2026-09-09 代码 + 19 条单测（459 项全过）+ 文档已落地，**未提交、未部署、未真机验**（验收清单见 §验收边界「真机」）

---

## 背景：现状与 D4 的出入

现状（2026-09-09 `main`，commit `1968aa1`）：

- 「现在跑」→ `maa_action.run_now()` 直接 `Popen maa_daily.py now`，中间没有任何选关步骤。
- `now` 场次的计划 = `effective_plan(phase="now")` = `state/defaults.json` ⊕ `sessions.now`；
  `sessions.now` 从来没人写（`_today_override()` 也只保留 `am` / `pm`），所以手动场**永远刷
  `defaults.json` 里写死的 `1-7`**。
- 四期 D4 写的是「现在跑取**缺口默认关**」，D5 写「默认关 = 仓库缺口计算结果」。但缺口自动定关
  `apply_auto_stage()` 只在 07:50 / 21:50 建卡时跑，只写 `sessions.am` / `sessions.pm`；
  `maa_daily.py` 不引用 `maa_farming` / `maa_plan_card`。**手动场拿不到缺口关，活动期间也去刷 1-7。**
  `DISCORD_GUIDE.md` / `SKILL.md` 写的「走默认关」字面没错，含义与 D4 不一致。

## 决策记录（2026-09-09 用户拍板，执行时不要再问）

| # | 决策 |
|---|---|
| M1 | 「现在跑」**不再直接开跑**。改为发一张「手动场选关」卡：一个下拉 + 说明。**只有从下拉选了关，这一场才派发**。 |
| M2 | 下拉的**可选关卡和早晚场卡完全一致**：同一个数据源 `maa_plan_card._stage_choices()`（上次作战关 / 1-7 / 当前开放资源本 / 当前活动关，主掉落从上游 `stages.json` 现取）。**不另造一份列表。** |
| M3 | 卡片 **10 分钟**不选就过期：下拉失效（组件收起、登记项删除、卡片标「已过期」），要跑得重新说「现在跑」。 |
| M4 | 选中的关只对**这一次**手动场生效，**不写 `plan.json`**，不影响早晚场覆盖（D3 不变）。手动选的关照四期步骤 3b：不带 `farming_drops` 上限、不带后备关，打到理智不足为止。 |
| M5 | 手动场仍**豁免 skip**（不变量 #5d），仍持 `run.lock`，正在跑时说「现在跑」或点下拉都拒绝。 |
| M6 | 终端直接 `python3 maa_daily.py now`（不带 token）保持旧行为（刷 `defaults.json`），那是开发 / 排障入口，不经 Discord。 |
| M7 | 四期 D4 改写为本文 M1~M4；D5 的「默认关 = 缺口计算」只对早晚场成立。 |
| M8 | 卡片显示「缺口推荐：X」。推荐**异步补**：卡先发（写「计算中…」），脱离会话的子进程查森空岛算缺口后 PATCH 卡片写上推荐关并给对应选项打 🎯；算不出写「推荐暂不可用」。推荐**只是提示**，不设 `default`，不替用户选。 |
| M9 | 终端 `python3 maa_daily.py now`（不带 token）不改，见 M6。 |
| M10 | **定时场优先于手动场**：距 08:00 / 22:00 不足 `manual_run_blackout_minutes`（默认 **30**；实测一场手动日常约 14 分钟 + 卡片 10 分钟有效期）时，「现在跑」**不发卡**，卡上选关也**拒绝派发**。文案：「距 HH:00 日常场次不足 30 分钟，定时场优先，请在日常结束后再手动跑。」 |

---

## 步骤 M — 手动场选关卡

### 前置

- 四期已结项；`maa_action.py` 20s 硬超时、`buttons.json` 全局共享、`select_args` 查表（不变量 #6 / #7 / #8）都在线上。
- 抄作业卡（`maa_action.copilot_card()` + `_copilot_card_components()`）是「发卡 → 登记下拉 → 选中即执行」的现成样板；
  本步的卡片、登记、收旧卡顺序都照它抄（不变量 #14b：先收旧卡组件 → 删旧登记 → 再写新登记）。
- 抄作业搜索的 `searches/<token>.json` + `expires_at` + `supersede_search()` 是「有 token、会过期、可作废」的现成样板。

### 目标

1. 说「现在跑」→ 频道里出现一张「🎯 手动场选关」卡，下拉选项与今天早晚场卡一模一样，没有预选项。
2. 从下拉选一关 → 立刻派发手动场，卡片组件收起，回执写清刷哪关；`maa_daily.py` 的开跑消息随后照旧出现。
3. 10 分钟没选 → 卡片组件收起、标「⌛ 已过期」，再点等于「未登记」；选关的登记项从 `buttons.json` 里消失。
4. 期间再说一次「现在跑」→ 旧卡作废（同 3），发新卡；正在跑时拒绝，不发卡。
5. 单测覆盖 §验收边界全部条目；`DISCORD_GUIDE.md` / `SKILL.md` / `DEV_GUIDE.md` / `PLAN_PHASE4.md` D4 同步。

### 做法

**M-a. 请求状态文件（`maa_common.py` 定路径，`maa_action.py` 读写）**

- 新增 `MANUAL_RUN_PATH = STATE_DIR / "manual_run.json"`，**单文件、同一时刻只有一个请求**（新请求覆盖旧请求）。
  结构：`{token, status, stage, message_id, created_at, expires_at, dispatched_at, updated_at}`。
  - `token`：12 位 hex，复用 `maa_action._new_search_token()`。
  - `status ∈ {pending, dispatched, expired, superseded}`。
  - `MANUAL_RUN_TTL = timedelta(minutes=10)`（放 `maa_common.py`，测试可 patch）。
- 所有读改写都在 `state_lock()` 下（与 `buttons.json` 同一把锁，不变量 #7）。

**M-b. 卡片与登记（`maa_action.py`；选项构造复用 `maa_plan_card.py`）**

- `run_now()` 改为：
  1. 锁在 → 沿用现有「X 已经在跑 Y，没有重复启动」拒绝，**不发卡**。
  1b. 拒绝窗（M10）：新写 `manual_run_blackout_reason(config, at)`，结构照 `_copilot_blackout_reason()`，
      读 `config.json` 的 `manual_run_blackout_minutes`（默认 30，0 = 关闭）；命中 → 回执拒绝、不发卡、不写请求。
  2. `require_executable` 两项照旧先查，查不过不发卡。
  3. 若 `manual_run.json` 里有 `pending` 请求 → 先作废它（M-d 的 `supersede`），再发新卡。
  4. 选项 = `maa_plan_card._stage_choices(effective_plan(phase="now"), config)`（**同一函数、同一 config**，与早晚场卡一致；M2）。
     下拉本身**不复用 `_stage_select()`**（它把 `set-stage <phase>` 写死进登记项、并按计划关标 🎯 默认），而是本步新写 `_manual_run_components(options, token)`：
     值仍是不透明 `choice:N`；`select_args[value] = ["run", "start", stage, token]`；**不设任何 `default`**（强制用户选；M1）。
     `custom_id = f"{MANUAL_RUN_BUTTON_PREFIX}{token}:stage-select"`，`MANUAL_RUN_BUTTON_PREFIX = "hermes:btn:maa-now:"`——
     **自己的前缀**，不落在 `hermes:btn:maa:` 下，07:50 / 21:50 建卡的整前缀清扫碰不到它（同抄作业卡的理由）；
     也不落在 `hermes:btn:maa-copilot:` 下，`prune_expired_searches()` 碰不到它。
  5. embed：标题 `🎯 手动场选关`（常量 `MANUAL_RUN_CARD_TITLE`），正文说明「选中即开跑；不选 10 分钟后作废；
     不看早晚场卡上的选择；设了跳过也照跑」，footer 写「HH:MM 前有效」；无 `default` 的下拉 placeholder「🎯 选这次手动场要刷的关」。
  6. 顺序照抄作业卡：收旧卡组件 → 锁内删 `MANUAL_RUN_BUTTON_PREFIX` 全部旧键 → 写新登记 → POST 卡片 → 写 `manual_run.json`（`status = pending`，`expires_at = now + TTL`）。
  7. 派一个**脱离会话的过期哨兵**（M-d）和一个**脱离会话的推荐补写进程**（M-g）。
  8. 回执：「🎯 已发手动场选关卡，从下拉选一关即开跑；HH:MM 前不选就作废。」照旧附「今天的早/晚场设了跳过，手动场照跑」提示。
- 一次「现在跑」= 一次 Discord POST + 本地读写，在 20s 预算内（不变量 #6）；森空岛 / 缺口计算**不在这个进程里做**（M8）。

**M-c. 选中即派发（`maa_action.py`）**

- 新子命令 `run start <关卡> <token>`（`dispatch()` 精确匹配 `len(argv) == 4`）。所有值都来自登记表，不从 Discord 直通（不变量 #8）。
- 校验顺序，任一不过就回执拒绝、**不派发**：
  1. `token` 合法（`SEARCH_TOKEN_RE`）且等于 `manual_run.json` 的 `token`，`status == pending`。否则「这张选关卡已失效，请再说一次『现在跑』」。
  2. `now <= expires_at`。过期 → 就地执行 M-d 的过期收尾（状态、登记、组件），回执「已过期（HH:MM），请再说一次『现在跑』」。
     这是**硬闸门**：哨兵晚到、adapter 重放、旧登记残留，都拦在这里。
  3. `valid_stage(stage, config)`（同 `set-stage` 的白名单）。
  4. `is_run_locked()` → 「X 已经在跑 Y」，卡片保持 pending（用户可等跑完再选；仍受 10 分钟约束）。
  4b. 拒绝窗（M10）：`manual_run_blackout_reason()` 命中 → 回执拒绝，卡片保持 pending（到期由哨兵收）。
  5. `require_executable` 两项。
- 通过后在 `state_lock()` 内原子改 `status = dispatched, stage = <关卡>, dispatched_at`，锁外：
  删这个 token 的登记键 → `retire_discord_components(message_id)`（失败只进回执尾巴，不阻断）→
  `Popen([sys.executable, maa_daily.py, "now", token])`（参数、`start_new_session` 等与现有 `run_now()` 一致）。
- 回执：「▶️ 已派发手动场，刷 X（不带掉落上限，打到理智不足为止）；开跑消息稍后出现在频道。」

**M-d. 过期与作废（`maa_action.py`）**

- 新子命令 `run expire <token>`：锁内读 `manual_run.json`；`token` 匹配且 `status == pending` 且 `now > expires_at` 才动手：
  `status = expired` → 删该 token 登记键 → PATCH 卡片 `components: []` 并把 embed footer 改成「⌛ 已过期，重新说『现在跑』」。
  不满足条件就静默返回（幂等，可重复调用）。
- 过期哨兵：`run_now()` 发卡后 `Popen([sys.executable, maa_action.py, "run", "expire", token, "--after", str(TTL 秒)], start_new_session=True)`；
  带 `--after N` 时先 `sleep(N)` 再做上面的事。它是**尽力而为的 UI 清理**，正确性由 M-c 的硬闸门保证。
  （`maa_action.py` 被 Hermes 直接 exec 时受 20s 限制；这里它是我们自己 Popen 出来的脱离会话子进程，与 `maa_daily.py` 同理不受限。）
- 作废（`supersede`）：`run_now()` 发新卡前对旧 `pending` 请求做与过期同样的三件事，`status = superseded`，footer 写「已被新的选关卡取代」。
- 拒绝重复派发：`status` 一旦离开 `pending`，`run start` 一律拒绝（M-c 第 1 条）。

**M-e. 手动场按请求取关（`maa_daily.py`）**

- `select_phase()` 允许 `argv == ["now", <token>]`（`am` / `pm` 不接受第二参数）；`main()` 把 token 传给 `execute(phase, manual_token="")`。
- `execute()` 中 `phase == "now"` 且 `manual_token` 非空：锁内读 `manual_run.json`，要求 `token` 匹配且 `status == dispatched` 且 `stage` 存在，
  否则**不跑**（发一条红色告警「手动场请求不匹配」，返回 1）。匹配则在 `effective_plan()` 结果上覆盖
  `stage = 请求的关, stage_source = "discord", farming_drops = {}, farming_fallback_stage = ""`，
  再走 `attach_annihilation` / `attach_sanity_yield` 与后续所有现有逻辑；快照 `runs/<date>-now.json` 照旧落盘（不可变）。
- `manual_token` 为空 → 完全维持现状（M6）。
- `should_skip()`、`run_lock`、报告、账本、后备关空跑重试（`farming_fallback_stage` 为空时自然不触发）都不改。

**M-g. 缺口推荐异步补写（`maa_action.py`，计算复用 `maa_plan_card.py`）**

- 新子命令 `run recommend <token>`：**只由 `run_now()` Popen 派发**（脱离会话，不受 20s 限制），不进登记表、不由技能调用。
- 做法：`current_farming_result(effective_plan(phase="now"), config)` → `auto_stage(result)`（与 07:50 建卡同一条计算链，D5 的口径），
  再 `farming_note(result)` 取一句理由。整段套 `try`，任何异常 → 推荐文案「推荐暂不可用（原因一句）」。
- 写回：锁内重读 `manual_run.json`，`token` 匹配且 `status == pending` 才 PATCH（过期 / 作废 / 已派发的卡不再改）；
  PATCH 同一条消息的 `embeds`（字段「缺口推荐」= `X · 理由`）和 `components`（把值等于推荐关的那个 option 的 emoji 改成 🎯，
  其余保持 ▫️；**仍不设 `default`**，`custom_id` / `select_args` 不变，登记表不用重写）。推荐关不在选项里就只写字段不打标。
- 记录 `recommended_stage` 进 `manual_run.json`，仅供回执与测试。
- 发卡时 embed 字段「缺口推荐」先写「计算中…」。

**M-f. 文案与文档**

- `SKILL.md`：「现在跑」→ `run now` 的说明改为「发一张选关卡，用户从下拉选关才开跑，10 分钟不选作废；**不要替用户选**」；
  新增「手动跑 CE-6」这类**带关卡**的说法 → 仍然 `run now`（先发卡，让用户在卡上点；不允许技能直接 `run start`——`run start` 只由登记表触发）。
- `DISCORD_GUIDE.md`：第 30 / 35 / 185 / 216 行附近「走默认关」改为本文流程；加一条「⌛ 已过期」的说明。
- `DEV_GUIDE.md`：子命令表加 `run start` / `run expire` / `run recommend`；config 键表加 `manual_run_blackout_minutes`；状态文件表加 `manual_run.json`；按钮前缀表加 `hermes:btn:maa-now:`；不变量表加一条
  「手动场只由 `run start <关卡> <token>` 派发，token 与 `manual_run.json` 匹配且未过期」。
- `PLAN_PHASE4.md` 决策表 D4 一行改为「见 `PLAN_MANUAL_RUN.md` M1~M4（2026-09-09 改）」；D5 补「仅早晚场」。

### 允许改动的文件

`scripts/maa_action.py`、`scripts/maa_daily.py`（仅 `select_phase` / `main` / `execute` 开头取计划处）、
`scripts/maa_common.py`（仅新增常量与路径）、`scripts/maa_plan_card.py`（**仅当**需要把 `_stage_choices` 的 `default` 标记拆参数；
否则不动）、`tests/test_maa.py`、`docs/DISCORD_GUIDE.md`、`docs/DEV_GUIDE.md`、`docs/archive/PLAN_PHASE4.md`（原 `docs/PLAN_PHASE4.md`，本步一并归档）、
`skills/games/maa-operator/SKILL.md`、`deploy.py`（仅当 `run start` / `run expire` 需要进部署校验列表）。

### 禁止触碰

- `plan.json` 的结构与 `_today_override()` 白名单（M4：手动关不进 `plan.json`）。
- `effective_plan()` 语义、`apply_auto_stage()`、早晚场卡的 `_stage_select()` 登记项内容。
- adapter 补丁（`deploy.py` 里的 old/new 串）：本步不需要 adapter 新能力，**只用现有 `select_args` 查表**。
- 抄作业 / 肉鸽的状态文件与前缀；`prune_expired_searches()`。
- 森空岛 / 缺口计算：不在被 Hermes 直接 exec 的 `run now` / `run start` 进程里调用（20s 预算）；只允许在 M-g 的脱离会话子进程里做。

### 验收边界

单测（`tests/test_maa.py`，从仓库根目录跑 `python3 -m unittest`）：

1. `run now`：发卡的下拉选项**与 `_stage_choices(effective_plan("now"), config)` 逐项相同**（标签、值、说明），所有 option 无 `default`；
   每个 `select_args` 值都是 `["run", "start", <关卡>, <token>]`，token 一致且为 12 位 hex（不变量 #8）。
2. `run now`：登记键全部以 `hermes:btn:maa-now:<token>:` 开头；写入前旧前缀键被清掉；`manual_run.json` 为 `pending` 且 `expires_at = created_at + 10min`；
   发卡后 Popen 了过期哨兵和推荐补写（断言两次 argv 分别含 `run expire <token> --after 600` 与 `run recommend <token>`）；
   发卡 embed 的「缺口推荐」字段为「计算中…」。
2b. `run recommend`：pending 时 PATCH 同一 message_id，embed 字段写推荐关，推荐关对应 option emoji 为 🎯、其余 ▫️、无 `default`、`custom_id` 不变；
   计算抛异常 → 字段写「推荐暂不可用」且仍 PATCH；状态非 pending → 不 PATCH。
3. `run now` 在锁在时不发卡、不写请求、不 Popen。
3b. `run now` 在 07:31 / 21:45 被拒绝窗拦下（不发卡、不写请求）；07:29 放行；`manual_run_blackout_minutes = 0` 时 07:59 也放行。
   `run start` 在拒绝窗内拒绝且不 Popen、状态仍 pending。
4. `run now` 第二次：旧请求 `superseded`、旧登记键消失、旧卡 PATCH 空组件，新卡 token 不同。
5. `run start`：token 不匹配 / 状态非 pending / 过期 / 关卡不在白名单 / 锁在 → 各自的拒绝文案，**均不 Popen**；过期分支还要断言状态变 `expired` 且登记键被删。
6. `run start` 成功：状态 `dispatched` 且 `stage` 记录正确；登记键删除；Popen argv 为 `[python, maa_daily.py, "now", token]`。
7. `run expire`：pending 且未到期 → 不动；pending 且到期 → `expired` + 删登记 + PATCH；非 pending → 幂等静默。
8. `maa_daily.select_phase(["x", "now", token])` 返回 `now` 且透传 token；`["x", "am", token]` 抛 `ValueError`。
9. `maa_daily.execute("now", manual_token=token)`：请求匹配 → 快照 `plan.stage` 为所选关、`stage_source == "discord"`、`farming_drops == {}`、后备关为空；
   请求不匹配 → 返回 1 且不进入 `run_lock`。无 token → `plan.stage` 等于 `defaults.json`（M6 回归）。
10. 07:50 建卡的整前缀清扫（`maa_plan_card.main()` 那段）**不删** `hermes:btn:maa-now:` 键（回归）。
11. 现有 `test_run_now_receipt_names_the_skipped_sessions` 改为断言新回执仍点名被跳过的场次。

真机（部署后在频道验，逐条截图或复述回执）：

- 「现在跑」→ 卡片出现，下拉列表与当天早场卡一致，没有预选；几秒到十几秒后「缺口推荐」从「计算中…」变成具体关卡并打 🎯。
- 选关 → 回执 + 卡片组件消失 + 开跑消息 + 结束汇报里关卡为所选关、无 drops 上限。
- 再发一张不选，等 10 分钟 → 组件消失、footer「已过期」；此时 `buttons.json` 没有 `maa-now` 键。
- 跑的过程中说「现在跑」→ 拒绝且不发卡。

---

## 未决问题（2026-09-09 已答）

| # | 问题 | 结论 |
|---|---|---|
| Q1 | 卡片上显示「缺口推荐：X」？ | **显示**，异步补写（M8 / M-g） |
| Q2 | 距 08:00 / 22:00 不足 N 分钟时「现在跑」要不要拒绝？ | **拒绝**（M10，默认 30 分钟）。理由：定时场优先；不拦的话手动场持锁撞上定时场，定时场直接放弃并发红色告警「run.lock 被另一场占用」，当场不补跑 |
| Q3 | `maa_daily.py now` 不带 token 是否也改为拒绝？ | **不改**（M6 / M9） |
