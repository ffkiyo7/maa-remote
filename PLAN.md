# MAA 长草助手 · macOS + Discord 实现方案

## Context

把参考图里的 5 步全自动流程（定时叫醒 → 自动开模拟器 → 刷理智 → 基建/公招/商店/领奖 → 关进程 + 推送）落到本机：**Apple M4 / macOS 26.6 / 16GB**，明日方舟**国服**账号，通知与交互走 **Discord**（不是图里的飞书）。

`~/maa-remote-spike` 是旧方案，平台完全不同（Windows + MuMu + 飞书 + DeepSeek 常驻服务），已重新做过本机 spike，只保留概念（揉揉乐、安全阀、StartUp 恒开），代码与架构不复用。

**最终形态**：每天早 7:50 在专用频道收到「今日计划卡」，通过原生下拉菜单选择常用关卡和当前开放活动关；早 8 点和晚 10 点各自动跑一遍完整日常，子区只显示带 emoji 的中文步骤，跑完在频道发中文战报卡片 + 游戏主页面截图，并把模拟器和游戏进程清干净。

**核心简化**：不新增任何常驻进程。Hermes gateway 已常驻并接管 Discord，所以只需要 3 个 cron 脚本 + 1 个动作脚本，全是「跑完就退」的短命进程，状态落盘在 `~/.hermes/state/maa/`。旧方案那套 DeepSeek 意图识别 + prompts/schema/evals 三件套被 Discord 原生按钮取代，只在「自然语言临时改计划」这一条兜底通道上保留 LLM，且有脚本级硬校验兜底。

---

## 已确认的决策

| 项 | 决定 |
|---|---|
| exec 按钮白名单 | **打本地补丁**（一行） |
| Mac 睡眠 | **不用管** —— 已关自动睡眠，关显示器不影响 |
| 晚 10 点跑什么 | **与早场完全一致**的任务链 |
| 默认关卡 | **上次的本**（MAA `stage = ""` 自动识别当前/上次作战） |
| 自然语言可改范围 | **关卡 + 跳过今天**（脚本级白名单校验） |
| 已开跑后才发指令 | **写入并应用到晚场**，明确告知不打断早场 |
| 碎石 | 永不提供入口，`stone` 恒 0，脚本级硬拒 |
| 连战次数 | Fight 固定 `series = 0`，由 MaaCore 选择当前理智支持的最大次数；倍率上限跟随当前客户端与 MaaCore，不写死 |
| 基建换班 | 固定 `mode = 20000`，只使用游戏内预设队列轮换；设施仅列 `Mfg/Trade/Reception`，MAA 不自行挑选或清空进驻干员 |
| 流式步骤 | 滚在**当次子区**里，每 5 秒编辑同一条消息 |
| 子区命名 / 生命周期 | `MAA日常·早 · 08-02 08:00`；**24h 后自动删除** |
| 截图 | MaaCore 返回游戏主页面后截图，**随战报卡片发到频道** |

---

## 环境现状（2026-08-01 实测）

已有：Hermes gateway 常驻（LaunchAgent `ai.hermes.gateway`）、Discord 已连 `wadoruru的服务器`、8 个 cron 在跑、Homebrew。

缺失，需 P0 安装：BlueStacks、maa-cli + MaaCore、adb、国服客户端。

**关键可行性已验证**：MAA 官方文档明确支持 BlueStacks Air（M 系列），`127.0.0.1:5555` + **MaaTouch**；maa-cli 的 Homebrew tap 提供 macOS 26 / aarch64 预编译包。

---

## 架构

```
        ┌──────── Hermes cron（UTC，北京时间 −8）────────┐
        │  50 23 * * *  → maa_plan_card.py   京 07:50 计划卡 + 清理过期子区
        │   0  0 * * *  → maa_daily.py am    京 08:00 执行
        │   0 14 * * *  → maa_daily.py pm    京 22:00 执行
        └───────────────────┬────────────────────────────┘
                            │
  Discord #MAA长草助手 ◄────┤   频道：计划卡 / 开跑消息 / 战报卡片＋截图
    │  └─ 子区（每次跑一个）    子区：流式步骤（每 5s 编辑同一条消息）
    │                       │
    ├─ 选下拉/点按钮 ─┐     │
    │           ├──► maa_action.py ──► plan.json（今日覆盖，带日期戳）
    └─ 说人话 ──┘  (<1s 返回)              │
       （频道或子区都行，                   │
        子区继承父频道绑定）                ▼
                    ┌──── maa_daily.py ────────────────────────┐
                    │ 1. flock run.lock（防并发）               │
                    │ 2. 快照本场计划到 runs/<date>-<phase>.json│
                    │ 3. open -a BlueStacks → 轮询 adb 就绪     │
                    │ 4. 频道发「🟡 开跑」→ 在其下建子区        │
                    │ 5. maa run；CLI 事件转中文步骤            │
                    │ 6. MaaCore 返回主界面 → ★ 截图           │
                    │ 7. maa closedown → 退模拟器 → 解锁        │
                    │ 8. 频道发战报卡片（内嵌截图）             │
                    └───────────────────────────────────────────┘
```

### 计划的状态模型（覆盖自动过期）

```
defaults.json   持久默认：stage="" / 固定临期药策略 / 任务全开
plan.json       今日覆盖：{date:"2026-08-02", stage:"CE-6",
                          skip:false, source:"discord", updated_at:...}
```

Runner 取计划：`plan = defaults`，若 `plan.json.date == 今天(Asia/Shanghai)` 才叠加覆盖。
→ **今天说的话明天自动失效**，不需要任何清理任务。

> 正确性细节：runner 启动时把「本场实际使用的计划」快照到 `runs/<date>-<phase>.json` 再开跑。之后写入 `plan.json` 的指令不会影响正在跑的那一场——这是「已开跑 → 应用到晚场」能成立的基础。

---

## Discord 机制：已验证的事实与三条硬约束

Hermes 已有一套重启不失效的原生按钮（`hermes:btn:` 前缀，`agent-book` 领读在用，是最好的抄袭对象）。**下面三条都是读 `adapter.py` 源码才发现的，文档里没有**：

| # | 约束 | 位置 | 对策 |
|---|---|---|---|
| 1 | exec 脚本**硬超时 20 秒** | `_run_hermes_button_script` | MAA 跑十几分钟，按钮**只能**写状态或 `Popen(start_new_session=True)` 派发后台任务后立刻返回 |
| 2 | exec 白名单**只有 `agent_book_action.py`** | `adapter.py:60` `_HERMES_BUTTON_SCRIPTS` | 加 `"maa_action.py"`。原注释特意说明不能用通配模式（会误放行日推脚本），只能显式加名字 |
| 3 | 登记表路径写死且**全局共享** | `adapter.py:5758` → `state/agent-book/buttons.json` | MAA 按钮用 `hermes:btn:maa:*` 前缀写进**同一个文件**，必须 **merge 不能覆盖**，并复用 `agent_book_common.py` 的同一把 `state.lock` |

**已验证可以直接用的**：

- 授权：`DISCORD_ALLOWED_USERS` 只有你一人，且**没设 `DISCORD_ALLOWED_CHANNELS`**，新频道开箱即用
- **子区自动继承父频道**：`_discord_channel_keys_from_channel` 把 parent_id 计入免 @ 判定，`resolve_channel_skills`/`resolve_channel_prompt` 也回落到 parent → **在子区说话和在频道说话完全等效，零额外配置**
- 附件：`hermes send --to discord:<id> "MEDIA:/path/x.png"` 是官方支持的发图方式（适配器内部走 `payload_json` + `files[N]` 多段上传）

---

## 「说一句就改今天计划」怎么走通

1. 频道 ID 加进 `.env` 的 `DISCORD_FREE_RESPONSE_CHANNELS` → 不用 @ 就能触达 agent（`adapter.py:1374-1377`）
2. `config.yaml` 的 `platforms.discord.channel_skill_bindings` 把 `maa-operator` 技能绑到该频道（子区自动继承）
3. Discord 会话的 `hermes-discord` toolset = `_HERMES_CORE_TOOLS` + discord/discord_admin，**含 `terminal` 和 `write_file`**（`toolsets.py:31,457`）→ agent 有能力执行脚本
4. 技能强制 agent 走 `maa_action.py`，脚本校验后写 `plan.json`
5. runner 只认 `plan.json`

**关键护栏**：技能里必须明写「**禁止直接用 write_file 改 plan.json，一切变更必须通过 `maa_action.py`**」。否则 agent 会绕过所有校验直接写文件。

**脚本级白名单**（靠代码不靠提示词）：`stage` 匹配 `^[A-Za-z0-9]{1,8}-\d{1,2}$` / 空串 / 在 `maa activity` 当期活动关列表里，其余拒绝；`medicine=0`、`medicine_expire_days=2` 与 `stone=0` 固定且无修改入口。拒绝时返回 `ok:false` + 原因，agent 原样转述。

这比旧方案「先问确认再放行」更强——**即使 LLM 误解析，脚本也会拒绝**。

---

## 流式步骤 → 子区

**关键实现事实**：`maa-cli` 的高层事件默认主要走 stderr。runner 同时捕获 stderr/stdout 并只写入本地日志；Discord 内容由确定性解析器转换为中文，不直出 CLI。

- 开跑时在频道发「🟡 早场开跑…」，在其下 `POST /channels/{id}/messages/{mid}/threads` 建子区，`name = "MAA日常·早 · 08-02 08:00"`，`auto_archive_duration = 1440`
- 子区内先发一条占位消息，之后**只编辑它**（不刷屏）
- reader 线程收集任务事件；主循环**每 5 秒**、且中文状态有变化时才 PATCH 一次
  - Discord 编辑限流约 5 次 / 5 秒 / 频道，1 次/5s 有充足余量；遇 429 按 `retry_after` 退避
  - 只显示“启动游戏 / 刷理智 / 基建换班 / 公招 / 商店 / 领奖 / 返回主界面”等描述，整体守住 2000 字符上限
- 结束时最后编辑一次：✅/❌ + 总耗时 + 中文步骤状态；频道战报再给出关卡次数、掉落、设施和公招计数

**子区清理**：thread_id + created_at 记进 `last_run.json`；每次 `maa_plan_card.py`（07:50）跑时顺手 `DELETE /channels/{thread_id}` 掉超过 24h 的。`auto_archive_duration=1440` 作为兜底（即使删除失败也会自动归档折叠）。删除子区需要 bot 有 **Manage Threads** 权限，P4 要确认。

---

## 截图

- 时机：任务链最后执行 `Custom(task_names=[Home...])` 回到游戏主页面；**`maa run` 结束后、`maa closedown` 之前**截图。`CloseDown` 继续从 `daily.toml` 拆出，用 `maa closedown Official` 单独跑。
- 取图：`adb -s 127.0.0.1:5555 exec-out screencap -p > runs/<date>-<phase>.png`
- 发送：战报 embed 加 `image: {url: "attachment://shot.png"}`，与图片在**同一条多段消息**里发出（`maa_common.py` 加一个 ~25 行的 `discord_upload()`，stdlib 手写 multipart 即可）。若多段上传调不通，退回 `hermes send --to discord:<id> "MEDIA:<path>"` 单独发一条。
- 失败不阻断：截图失败只在战报里标一句「截图失败」，不影响关进程和主流程。

---

## 交付物清单

```
~/.hermes/scripts/
  maa_common.py      # 抄 agent_book_common.py：state_lock / atomic_write_json
                     # / discord_token（从 .env 只读一个键）/ discord_request
                     # 新增：discord_upload（multipart）/ thread 建与删
  maa_plan_card.py   # 07:50 发计划卡 + merge 写按钮登记 + 清理 >24h 子区
  maa_daily.py       # 执行主体：起模拟器 → 建子区 → 流式 → 截图 → 关进程 → 战报
  maa_action.py      # 唯一的计划变更入口（按钮 + 聊天共用）← 需加进 exec 白名单

~/.hermes/state/maa/{config,defaults,plan,last_run}.json · run.lock
                    runs/<date>-<phase>.{json,log,png}

~/.hermes/skills/games/maa-operator/SKILL.md    # 绑定到新频道

~/.config/maa-cli/profiles/default.toml
~/.config/maa-cli/tasks/daily.toml              # 不含 CloseDown

改动：adapter.py:60（加白名单）· LOCAL_PATCHES.md（登记）
     .env（FREE_RESPONSE_CHANNELS 追加）· config.yaml（技能绑定 + 频道提示词）
```

### `maa_action.py` 子命令（全部 <1s，输出 `{"ok":bool,"message":str}`）

```
plan set-stage <stage>              plan skip-today [on|off]
plan show
run now                             log tail
```

`plan set-*` 时用 `flock(LOCK_EX|LOCK_NB)` 试探 `run.lock`：
- 空闲 → 「今天（8/2）刷 CE-6，其余按默认。早 8 点和晚 10 点各跑一次。」
- 被持有 → 仍写入，但回「早场正在跑<上次的本>，不打断它；晚 10 点那场刷 CE-6。」

### maa-cli 配置

`profiles/default.toml`：`adb_path="/opt/homebrew/bin/adb"`、`address="127.0.0.1:5555"`、`touch_mode="MaaTouch"`、`[resource] global_resource=""`（国服留空）。

`tasks/daily.toml` 顺序：`StartUp` → `Fight(series=0, medicine=0, medicine_expire_days=2, stone=0)` → `Infrast(mode=20000)` → `Recruit` → `Mall` → `Award` → `Custom(Home)`。基建只触发游戏内预设队列轮换。**CloseDown 不进链**。runner 将 stderr/stdout 只写入本地日志，并把识别出的任务事件转成中文步骤后更新 Discord 子区。

---

## 实施阶段（每阶段没过 DoD 不进下一阶段）

### P0 · 装环境（人工约 40 分钟，大部分是下载和登录）

```bash
brew install --cask bluestacks android-platform-tools
brew install MaaAssistantArknights/tap/maa-cli
maa install
```

手工部分（脚本代替不了）：BlueStacks 设置 → 高级 → 开 **Android Debug (ADB)**；分配 4 核 / 4–6GB；关掉开机自启与后台常驻；分辨率设 1280×720 或 1920×1080（MAA 识别依赖固定比例）；侧载国服 APK 并**手动登录一次**，点掉新手引导与公告；`ls /Applications` 确认 app 准确名字填进 `config.json`。

**DoD**：`adb connect 127.0.0.1:5555 && adb devices` 看到 device；游戏能进主界面；`adb exec-out screencap -p > /tmp/t.png` 能出图。

### P1 · 命令行跑通（含唯一的花钱验证）

先把 Fight 的 `times` 临时设成 6，并保持 `series = 0`，跑 `maa run daily -v`，验证
MaaCore 会按当前理智选择可用的最大次数。国服 2.7.61 的新版列表需要 Beta MaaCore
6.16.0；实机已成功选择 6 连战并完成一批（36 理智），且没有吃药、没有碎石。

Beta 在有限 `times=6` 下改选倍率后，偶发无法立即重识别倍率，内部计数会退回 1，可能
继续启动下一批。因此完整 P2 E2E 临时把 `times` 设为 0，让 Fight 立即达限而不再消耗
理智；其余任务链、Discord 流式进度、截图、战报和清理照常验收。全部通过后恢复
`2147483647`，生产规则仍为 `series=0` 自动选择当前理智支持的最大倍率。

**DoD**：能连上、能识别主界面、能打一把；**确认 `-v` 下 stderr 的步骤行长什么样**（流式解析要用）；记录 summary 输出格式（战报要用）；⚠️ **肉眼确认日志里没吃囤药、没碎石**——`expiring_medicine` 自 MAA v6.8.0 起废弃、改为 `medicine_expire_days`（语义从「48 小时内」变成「N 天内」），旧 spike 的写法在新版上很可能失效甚至反转。先 `maa --version` 看 MaaCore 版本再定参数写法。

### P2 · 脚本化 + 截图 + 进程清理

`maa_common.py` + `maa_daily.py`：起模拟器 → 轮询 `getprop sys.boot_completed`（超时 180s）→ `maa run daily -v` → **截图** → `maa closedown Official` → 退模拟器。

关进程顺序（先让游戏正常存档，别直接 kill）：`maa closedown` → `osascript -e 'tell application "BlueStacks" to quit'` → sleep 10 → `pkill -f BlueStacks` 兜底 → `adb disconnect`。

**DoD**：一条命令从「什么都没开」跑到「进程全清」，`ps aux | grep -i bluestacks` 为空，且 `runs/` 下有当次的 log + png。

### P3 · 挂 cron

```bash
hermes cron create "50 23 * * *" --name "MAA 计划卡"  --script maa_plan_card.py --no-agent --deliver local
hermes cron create "0 0 * * *"   --name "MAA 日常·早" --script maa_daily.py     --no-agent --deliver local
hermes cron create "0 14 * * *"  --name "MAA 日常·晚" --script maa_daily.py     --no-agent --deliver local
```

`--deliver local` 因为脚本自己发卡片（与 `agent_book_daily.py` 一致）。cron 是 UTC（`config.yaml: timezone: Etc/UTC`，现有 `0 0 * * 1-5` 那条即京 8 点，已验证）。

**DoD**：`hermes cron run <id>` 手动触发能走完全流程并在频道里看到卡片。

### P4 · 频道 + 子区 + 按钮 + 自然语言

1. Discord UI 手建频道 `🌱maa长草助手`（`discord_admin` 没有 `create_channel`，手建 30 秒最快），ID 写进 `config.json`；确认 bot 有 **Manage Threads**（删子区要用）
2. `.env` 的 `DISCORD_FREE_RESPONSE_CHANNELS` 追加该 ID（原有值为另外两个频道，见本机 `.env`）
3. 打白名单补丁（adapter.py:60 加 `"maa_action.py"`）+ 写进 `LOCAL_PATCHES.md`
4. `maa_plan_card.py` 发卡片 + **merge** 写按钮登记（共享 agent-book 那把锁）+ 清理过期子区
5. 写 `maa-operator` 技能（抄 `agent-book-tutor/SKILL.md` 的 frontmatter 结构）+ `config.yaml` 绑定
6. `hermes gateway restart` 让补丁与配置生效

计划卡组件布局：

| 行 | 按钮 |
|---|---|
| 关卡 | `🎯 选择今天要刷的关卡` 下拉菜单（常用关卡 + 当前开放活动关） |
| 操作 | `⏭️ 今天跳过/▶️ 恢复自动场次` `📋 最近结果` `🤖 请 Hermes 解释` |

战报默认是**确定性卡片，不过 LLM**（零成本、不会瞎编）；想要自然语言解读就点「🤖 让 Hermes 讲讲」，走 `kind:"prompt"` 按钮起会话——保留旧方案「LLM 润色」的体验但不强制。

**DoD**：下拉选择「CE-6」后 ephemeral 秒回且 `plan.json` 真被改；旧卡片与已删除按钮失效；**重启 gateway 后下拉菜单仍有效**；频道或子区里说「今天刷 CE-6」效果一致；跑起来之后再改关卡，回复明确说明应用到后续场次。

### P5 · 加固

每周 `maa update`（国服活动更新资源）；失败告警（非零退出 / adb 超时 / 游戏要更新卡住）发红色 Embed，**绝不静默**；`runs/` 日志与截图保留 14 天。

---

## 端到端验收

1. 清空 `plan.json`，`hermes cron run <计划卡 id>` → 频道出现计划卡，显示「今天刷：上次的本」
2. 频道里发「今天刷 CE-6，其他不变」→ 收到确认；`cat ~/.hermes/state/maa/plan.json` 见 `stage:"CE-6"` 且 `date` 是今天
3. `hermes cron run <早场 id>` → 模拟器自动起、频道出现开跑消息、**子区标题为 `MAA日常·早 · MM-DD HH:MM`**、子区内步骤每 5 秒滚动更新
4. 跑的过程中**在子区里**发「改成 1-7」→ 回复明确说不打断早场、晚场生效（验证子区继承）
5. 跑完 → 频道出现战报卡片**且内嵌一张游戏截图**；`ps aux | grep -i bluestacks` 为空
6. `hermes gateway restart` 后使用最新卡片下拉菜单 → 仍然有效；更旧卡片控件已被收起
7. 次日 07:50 计划卡跑完 → 昨天的子区被删掉；覆盖失效，回到「上次的本」

---

## 风险

1. **BlueStacks 需要活跃图形会话** —— 关显示器/锁屏没问题，注销或切换用户会失败。gateway 是跟用户会话走的 LaunchAgent，方向正确。
2. **白名单补丁会被 `hermes update` 覆盖** —— 必须登记进 `LOCAL_PATCHES.md`，升级后复验按钮。
3. **`buttons.json` 与领读功能共享** —— 不 merge 就会把 agent-book 的按钮全干掉。
4. **流式编辑触发 Discord 限流** —— 固定 5 秒节流 + 内容无变化不发 + 429 退避；宁可少更几次也不要被限流拖垮主流程（限流异常必须吞掉，不能让它中断 MAA 运行）。
5. **删子区需要 Manage Threads 权限** —— 没有就退化成只归档（`auto_archive_duration=1440`），P4 确认。
6. **模拟器后台可能被 macOS 降频** —— 建议放单独 Space 而不是最小化；也会影响截图，P1 实测确认。
7. **国服客户端更新** —— StartUp 会卡在更新界面，需超时检测 + 明确报「游戏要更新，去手动点一下」。
8. **吃药参数版本差异** —— 见 P1，唯一会花钱的地方，必须实测。
9. 磁盘/内存：BlueStacks + 国服约 15GB（可用 120GB）；16GB 内存给模拟器 4–6GB 合适。

---

> 本文件是方案最终版（2026-08-01）。参考图：`IMG_8701.JPG`。旧 Windows 方案在 `~/maa-remote-spike`，仅作概念参考。
