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

三条 cron 全是 `--no-agent --deliver local` 的**短命进程**：跑完即退，不常驻，
stdout 不投递到 Discord（消息由脚本自己用 Discord REST API 发）。

---

## 2. 组件清单

| 文件 | 角色 | 触发方式 | 典型耗时 |
|---|---|---|---|
| `scripts/maa_common.py` | 无第三方依赖的公共层：时区、两把锁、原子写、Discord REST/multipart、清理 | 被其他三个 import | — |
| `scripts/maa_plan_card.py` | 每日计划卡；周期性 `maa update`；刷新活动关；写按钮登记；清理旧子区/旧卡片/旧产物 | cron 京 07:50 | 数秒～5 分钟（含 `maa update`） |
| `scripts/maa_daily.py` | 执行主体：模拟器 → maa-cli → 流式进度 → 截图 → 清进程 → 战报 | cron 京 08:00 / 22:00；`run now` 派发 | 10～50 分钟 |
| `scripts/maa_action.py` | **唯一**状态变更与派发入口（按钮、下拉、聊天共用） | Discord 组件交互；agent 用 terminal 调用 | < 1s（硬性要求） |
| `deploy.py` | 幂等部署 + 给 Hermes 打本地补丁 + 备份 | 人工 | 秒级 |
| `skills/games/maa-operator/SKILL.md` | 约束 LLM 只能走 `maa_action.py` 的护栏 | gateway 按频道绑定加载 | — |
| `tests/test_maa.py` | 27 个纯 stdlib 单测，全部 mock，不碰网络/模拟器 | 人工 | < 1s |

---

## 3. 链路一：计划卡（`maa_plan_card.py`，京 07:50）

`main()`（`scripts/maa_plan_card.py:381`）严格按这个顺序：

1. **`update_resources_if_due()`** — 距上次 `resource_update_attempted_at` ≥ 7 天才跑
   `maa update`（超时 300s）。成功后立刻刷一次活动关；失败发红色 Embed 告警，
   并在 `main()` 末尾 `raise` 让 cron 记为失败。
2. **`refresh_activity_stages()`** — 每天都跑 `maa activity Official`，解析出形如 `TO-9`
   的活动关列表写回 `config.json`；同时检测公告里的「资源收集」字样，命中则置
   `all_supply_stages_open=true`（全部资源本开放）。
3. **重新 `load_config()`** — 上一步刚改过磁盘上的 config，卡片必须从落盘后的真值渲染。
4. **`build_components()`** — 组装一个下拉 + 三个按钮，同时产出登记表条目。
5. **写按钮登记（关键）** — 在 `state_lock()` 里 **先删所有 `hermes:btn:maa:` 前缀的 key，
   再 merge 写回** `~/.hermes/state/agent-book/buttons.json`。这个文件是**全局共享**的，
   agent-book 的按钮也在里面，**绝不能整体覆盖**。删前缀的副作用是旧卡片上的
   MAA 控件立即失效（点了会报未登记）。
6. `cleanup_old_threads()` 删 >24h 的子区、`cleanup_run_files()` 删 >14 天的 `runs/` 产物。
7. `retire_old_plan_cards()` — 扫最近 100 条消息，把标题为 `🌱 MAA 今日计划` 且仍带
   components 的旧卡片 `PATCH components: []`，避免同时存在多张可点的卡。
8. 发新卡片。

### 关卡下拉是怎么构成的

`_stage_choices()`（`:213`）顺序拼接，上限 25 项：

- `上次作战关卡`（空串 → MAA 自动沿用游戏内上次关卡）
- `1-7`
- **当天开放的资源本** — `current_supply_choices()`（`:60`）用 `(now - 4h).weekday()` 算「游戏日」
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

## 4. 链路二：执行一场日常（`maa_daily.py`）

`execute(phase)`（`scripts/maa_daily.py:764`）：

1. **定场次** — `select_phase()`（`:56`）优先级：argv (`am|pm|now`) > **当前工作目录名**
   > 按北京时间的小时猜。cron 用 workdir 传场次，是因为 Hermes 的 `--script` 不支持 argv：

   ```
   --workdir ~/.hermes/state/maa/am  →  phase=am
   --workdir ~/.hermes/state/maa/pm  →  phase=pm
   ```

2. **抢锁** — `run_lock(blocking=False)`。**抢不到就直接 `return 0`，静默退出，不发任何消息。**
   调试时看不到任何反应，先怀疑上一场还没退干净。
3. 清理 >14 天产物；把**本场实际使用的计划**快照到 `runs/<date>-<phase>.json`；
   `last_run.json` 置 `status=running` 并把上一场的终态字段清空（避免运行中读到旧结果）。
   > 快照是「跑起来之后再改计划不影响本场」的实现基础——后续对 `plan.json` 的写入
   > 既改不了内存里这个 dict，也改不了这个文件。
4. `plan.skip` 为真 → 发一条灰色「已跳过」Embed，`status=skipped`，结束。**不开模拟器。**
5. **预检 → 起模拟器** — `require_executable` 校验 maa/adb 可执行（**在开模拟器之前**）→
   `open -a BlueStacks` → 轮询 `adb connect` + `getprop sys.boot_completed`（超时 180s）→
   `pm path com.hypergryph.arknights` 确认国服客户端已装。任一步失败且此时还没建子区，
   走 `send_direct_failure()` 直接在频道发红色告警。
6. **发开跑消息 → 建子区 → 发进度占位消息**，thread_id 记进 `last_run.threads`（供 24h 清理）。
7. **渲染 `daily.toml`** — `render_daily_toml(plan, config)`（`:76`）把本场快照渲染成 TOML 并
   `atomic_write_text` 覆盖 `~/.config/maa-cli/tasks/daily.toml`。任务链固定：

   `StartUp → Fight → Infrast → Recruit → Mall → Award → Custom(Home)`

   其中 `medicine = 0`、`stone = 0`、`series = 0` 是**硬编码字面量**，不从任何状态派生；
   `Infrast` 固定 `mode = 20000`（只点游戏内预设队列轮换）。**链里没有 CloseDown**——
   它被拆出去在截图之后单独跑。
8. **`run_maa()`**（`:544`）— `maa run daily -v --batch`，注入 `MAA_CONFIG_DIR` /
   `MAA_LOG_PREFIX=Never` /（可选）剔除代理变量。两个 reader 线程把 stdout+stderr
   同时 tee 到 `runs/<date>-<phase>.log`；主循环每 5 秒把**解析后的中文状态** PATCH 进
   子区那条消息，内容无变化就不发，Discord 异常一律吞掉（流式是尽力而为，不能拖垮主流程）。
   - 超时 `maa_run_timeout`（默认 3000s = 50 分钟）→ terminate/kill，返回码记 **124**。
     外层 Hermes cron 的上限是 3600s，这 10 分钟余量留给截图与清理。
   - `KeyboardInterrupt` → 返回码 **130**。
9. **截图** — 等 `home_screenshot_delay` 秒让动画稳定 → `adb exec-out screencap -p`，
   校验 PNG magic 后 chmod 600。**失败不阻断**，只在战报里标一句。
10. **`close_everything()`**（`:472`，在 `finally` 里）— `maa closedown Official` →
    `osascript quit`（bundle id）→ sleep `quit_grace_seconds` → `pkill -f BlueStacks` 兜底 →
    `adb disconnect` → 用 `[B]lueStacks` 这种加方括号的模式 `pgrep` 复核残留（防止匹配到自己）。
11. **判定与上报** — `ok = (return_code == 0 and not cleanup_errors)`。最后编辑一次子区消息
    （✅/❌ + 总耗时），然后往频道发战报 Embed + `attachment://shot.png` 单条多段消息；
    多段上传失败时降级成「无图 Embed + `hermes send MEDIA:`」两条。最后写 `last_run.json`。

### CLI 输出 → 中文的转换

`progress_content()`（`:273`）和 `extract_summary()`（`:308`）是两个**纯函数**：输入 CLI 行数组，
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

子命令（`dispatch()`，`scripts/maa_action.py:224`）：

| 命令 | 行为 |
|---|---|
| `plan set-stage <关卡>` | 白名单校验后写今日覆盖 |
| `plan skip-today on\|off` | 写今日覆盖 |
| `plan show` | 只读 |
| `run now` | 抢锁探测 → 预检 → `Popen` 派发 `maa_daily.py now` 后立即返回 |
| `log tail` | 读 `last_run.summary`；若是旧版格式则回读 log 用 `extract_summary` 重建 |

关卡白名单 `valid_stage()`（`:76`）：空串 ✔ / `^[A-Za-z0-9]{1,8}-\d{1,2}$` ✔ /
在 `config.activity_stages` 里 ✔ / 否则**现场跑 `maa activity Official` 并按词边界精确匹配**
（超时只给 **0.7s**，因为整体预算是 20s）。其余一律拒。

写状态时的两个细节：
- `_today_override()`（`:80`）只保留 `date/stage/skip/source/updated_at` 五个键，其余字段丢弃；
  日期不是今天就整个重置。所以 `plan.json` 天然「今天说的话明天失效」，不需要清理任务。
- `is_run_locked()` 决定确认话术：正在跑就明确回「不打断本场，X 生效于……」。

`emit()`（`:40`）把输出压在 7900 字节内——组件运行器只接受 ≤8192 字节的 JSON。

### 技能这一层为什么必要

`SKILL.md` 是给 LLM 的护栏：禁止 `write_file` 直接改状态文件、禁止调 `maa_daily.py`、
碎石永久关闭、原样转述脚本返回的 `message`。**但它只是第一道防线**——即使 LLM 完全无视技能，
`maa_action.py` 的白名单和 `render_daily_toml` 里的硬编码字面量仍然拦得住。
写代码时请始终假设「提示词会失效」。

---

## 6. 状态模型

```
~/.hermes/state/maa/
  config.json     环境与阈值（路径、超时、channel_id、活动关缓存）    ← 计划卡会改写
  defaults.json   持久默认：stage="" / medicine=expiring / tasks 全开
  plan.json       今日覆盖：{date, stage, skip, source, updated_at}
  last_run.json   最近一次运行状态 + threads[] + 资源更新时间戳
  run.lock        flock 独占锁（执行）
  am/ pm/         空目录，仅用于给 cron 传场次
  runs/<date>-<phase>.{json,log,png}   本场快照 / 完整日志 / 截图（保留 14 天）

~/.hermes/state/agent-book/
  buttons.json    全局共享按钮登记表（MAA 只碰 hermes:btn:maa:* 前缀）
  state.lock      共享写锁
```

**计划的求值**（`effective_plan()`，`scripts/maa_common.py:226`）：

```
result = normalize_plan(defaults.json)
if plan.json.date == 今天(Asia/Shanghai):   只叠加 stage 和 skip 两个键
result = normalize_plan(result)             ← 再规范化一次
```

`normalize_plan()` 每次都**强制** `medicine="expiring"`、`stone=0`，
即使磁盘上被人手工塞了别的值也没用（对应测试
`test_persisted_medicine_choice_cannot_disable_fixed_expiry_policy`）。

**两把锁，别搞混**：

| 锁 | 作用域 | 用法 |
|---|---|---|
| `run.lock` | 「同一时刻只有一场 MAA」 | runner 用非阻塞抢，抢不到就退；`maa_action` 只用它做只读探测 |
| `agent-book/state.lock` | 按钮登记表 + `plan.json` 写入 | 阻塞式，跨功能共享 |

`merge_last_run()`（`:420`）语义特殊：普通字段覆盖，`threads` 数组**按 thread_id 去重后追加**——
子区清理依赖这份累积列表，不能改成整体覆盖。

同一天同场次的 `runs/` 文件名会复用（`<date>-<phase>`），所以一天里多次 `run now`
会覆盖 `<date>-now.log/png`。要保留请自行改名或改 `run_stem`。

---

## 7. 部署链路（`deploy.py`）

```bash
python3 -m unittest discover -s tests -v   # 必须在仓库根目录跑（测试要 import 根目录的 deploy.py）
python3 deploy.py                          # 幂等；输出 {"ok":true,"changed":[…],"backup":"…"}
python3 deploy.py --check                  # 只检查 4 个脚本是否已部署
```

做的事：

1. **备份** — 任何将被修改的文件，先按原路径复制到
   `~/.hermes/backups/maa-setup/<YYYYMMDDTHHMMSS>/`。
2. **幂等写** — `write_bytes()` 内容一致就跳过（不写、不记 changed、不备份），
   否则 `mkstemp + fsync + os.replace` 原子替换，权限 0600/0700。
3. **状态的部署策略不同**（`deploy_state()`，`deploy.py:90`）：
   - `config.json`：**逐键合并**，仓库的键覆盖同名键，本地新增键保留。
     注意仓库里的 `activity_stages` 会盖掉线上缓存，下一次计划卡会重新刷。
   - `defaults.json` / `plan.json` / `last_run.json`：**只在不存在时**才复制种子，
     之后强制 `defaults.medicine="expiring"`、`defaults.stone=0`，并剥掉 `plan.json`
     里可能残留的 `medicine`/`stone`。
4. **给 Hermes 打 4 处补丁**：

| 目标 | 改动 |
|---|---|
| `hermes-agent/plugins/platforms/discord/adapter.py` | 白名单加 `maa_action.py`；`_run_hermes_button_script` 增加 `selected_values` 参数；增加 `select_args` 精确查表分支；调用处传入 Discord 的 `values` |
| `hermes-agent/LOCAL_PATCHES.md` | 登记上述补丁 |
| `~/.hermes/.env` | `DISCORD_FREE_RESPONSE_CHANNELS` 追加频道 ID（免 @） |
| `~/.hermes/config.yaml` | 频道绑定 `maa-operator` 技能 + 频道提示词 |

> `patch_adapter_source()`（`deploy.py:152`）用**精确字符串匹配**，匹配不到就 `RuntimeError`
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
hermes cron create "50 23 * * *" --name "MAA 计划卡"  --script maa_plan_card.py --no-agent --deliver local
hermes cron create "0 0 * * *"   --name "MAA 日常·早" --script maa_daily.py --no-agent --deliver local --workdir ~/.hermes/state/maa/am
hermes cron create "0 14 * * *"  --name "MAA 日常·晚" --script maa_daily.py --no-agent --deliver local --workdir ~/.hermes/state/maa/pm
```

cron 表达式是 **UTC**（`config.yaml: timezone: Etc/UTC`），脚本内部一律用 `Asia/Shanghai`。
改时间时两边都要想清楚。当前在册：`03d5e98ce8d4`（计划卡）、`50cd1fb095f2`（早）、
`4f48cf619d9f`（晚），可用 `hermes cron list` 复核。

---

## 8. 不可破坏的不变量

改任何代码前先读这张表；每一条都有对应单测，改坏了会红。

| # | 不变量 | 落点 |
|---|---|---|
| 1 | `stone = 0`，永不提供任何入口 | `render_daily_toml` 字面量 + `normalize_plan` 强制 + 技能第 3 条 |
| 2 | `medicine = 0`，只用 `medicine_expire_days` 吃临期药 | 同上；`set_medicine()` 直接返回 `ok:false` |
| 3 | `series = 0`（由 MaaCore 选当前理智支持的最大连战） | `render_daily_toml` 字面量 |
| 4 | 基建只做 `mode = 20000` 预设队列轮换，facility 只列 `Mfg/Trade/Reception` | `render_daily_toml` |
| 5 | 截图发生在 `maa run` 之后、`closedown` 之前；CloseDown 不进任务链 | `execute()` 步骤 9/10 + `daily.toml` 注释 |
| 6 | 按钮脚本必须 < 20s 返回 | `run_now()` 用 `Popen`；活动关探测 0.7s |
| 7 | `buttons.json` 只能 merge，且只碰 `hermes:btn:maa:` 前缀 | `maa_plan_card.main()` |
| 8 | Discord 下拉值必须经 `select_args` 查表，不能直通 argv | adapter 补丁 + `_stage_select()` |
| 9 | 原始 CLI 文本不进 Discord | `progress_content` / `extract_summary` 是唯一出口 |
| 10 | 开跑后快照即冻结，改计划不影响本场 | `runs/<date>-<phase>.json` + 内存 plan |
| 11 | 所有 maa-cli 子进程显式注入 `MAA_CONFIG_DIR` | `maa_subprocess_env()`（macOS 默认目录不是 `~/.config/maa-cli`） |
| 12 | 代理变量只在 maa-cli 子进程里剔除，不动用户会话 | `maa_subprocess_env()` |

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

**单测不能替代真机验收。** 图像识别、连战倍率选择、基建轮换这些只有实机能验。
需要跑完整链路又不想耗理智时，把 `config.json` 的 `fight_times` 临时设成 `0`
（Fight 立即达限但任务链完整），验完**务必**改回 `2147483647`。
`config/maa-cli/tasks/infrast-smoke.toml` 是单独验证预设队列轮换的诊断任务。

调试期间想避免定时任务插进来：`hermes cron pause <id>`，完事 `hermes cron resume <id>`。

---

## 10. 排障：症状 → 看哪里

| 症状 | 先看 |
|---|---|
| 到点什么都没发生，频道无任何消息 | `run_lock` 被占（上一场没退干净）→ `pgrep -fi bluestacks`；或 cron 被 pause |
| 频道有开跑消息，子区进度不动 | `runs/<date>-<phase>.log` 尾部；多半卡在客户端更新/登录页，50 分钟后会以 124 终止并告警 |
| 战报 ❌ 但任务都是 ✅ | `cleanup_errors` 非空也会判失败——看 Embed 备注栏和 `last_run.cleanup_errors` |
| 点按钮/下拉没反应或报未登记 | 卡片过期（每天 07:50 会清前缀重写）；或 gateway 没重启；或 `buttons.json` 被别的写入方覆盖 |
| 下拉里没有活动关 | `maa activity Official` 的输出变了 → 看 `parse_activity_stages()` 的正则 |
| 聊天改计划无效 | 看 agent 是否真的调了 `maa_action.py`；`plan.json` 的 `date` 是否是今天 |
| 战报里步骤全是「未执行」 | maa-cli 输出格式变了 → 改 `_task_state()` / `TASK_STEPS` |
| 截图失败 | ADB 断了或返回非 PNG；不阻断主流程，看 Embed 备注 |
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
