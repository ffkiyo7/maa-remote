# maa-remote

在 macOS 上用 Discord 遥控 [MAA](https://github.com/MaaAssistantArknights/MaaAssistantArknights)
跑《明日方舟》国服日常：每天早上收一张计划卡，早晚各自动跑一遍完整任务链，跑完发中文战报和主界面截图，
再把模拟器和游戏进程清干净。也能在聊天里说一句「抄 TO-9」，从 prts.plus 匹配作业并直接开跑。

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
  [`DEV_GUIDE.md`](./DEV_GUIDE.md) §11 那 14 条踩坑记录，以及 `scripts/` 里针对 MaaCore
  实际行为写的那些兜底。

## ⚠️ 免责声明

- 本项目是**非官方**的第三方自动化工具，与 Hypergryph／鹰角网络、森空岛、MAA 官方均无关联。
  《明日方舟》及相关名称的权利归其各自所有者。
- 游戏自动化**可能违反游戏的用户协议**，是否使用、以及由此产生的任何账号风险，**由使用者自行承担**。
- `scripts/maa_skland.py` 通过**社区逆向得到的**森空岛接口读取练度数据。接口随时可能变更或失效，
  且同样存在账号风险。**不接鹰角 token 也能用**，只是抄作业退化为「按热度排序、练度未校验」。
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
[Hermes Agent](https://github.com/NousResearch/hermes-agent) 提供，本项目只贡献 3 个 cron 脚本
和 1 个动作脚本，全部是「跑完就退」的短命进程，状态落盘在 `~/.hermes/state/maa/`。

```
        Hermes gateway（唯一常驻进程）
        ├── cron 调度器（UTC）                     ├── Discord 适配器
        │                                          │
   ┌────┴────────────────────────────┐      ┌──────┴───────────────────────┐
   │ 京 07:50  maa_plan_card.py      │      │ 组件交互 hermes:btn:maa:*    │
   │ 京 08:00  maa_daily.py  （早）  │      │   → 按钮登记表 → exec 白名单 │
   │ 京 22:00  maa_daily.py  （晚）  │      │ 自然语言（免 @ 频道）        │
   └─────────────────────────────────┘      │   → maa-operator 技能        │
                                            └──────┬───────────────────────┘
                        ┌──────────────────────────┘
                        ▼   （唯一状态变更入口，< 1s 返回）
                  maa_action.py
                        │
                        ▼
   ~/.hermes/state/maa/{config,defaults,plan,last_run}.json + run.lock + runs/
                        │   effective_plan() = defaults ⊕ 当天的 plan 覆盖
                        ▼
   maa_daily.py ── flock ── 快照本场计划 ── 起 BlueStacks ── 等 ADB
        └─ 渲染 daily.toml ── maa run ── 每 5s 编辑子区里的同一条进度消息
              └─ 返回主界面 ── Vision OCR 核对 ── 截图 ── closedown ── 战报
```

一天的时间线：

| 北京时间 | 发生什么 |
|---|---|
| 07:50 | 检查并跟随 maa-cli / MaaCore 更新 → 刷新当期活动关 → 频道里发**今日计划卡**（一个关卡下拉 + 三个按钮）→ 清理 24h 前的子区和 14 天前的运行产物 |
| 08:00 | 跑一遍完整日常。频道发「🟡 开跑」，其下开一个子区滚动中文步骤；跑完发战报卡片 + 主界面截图 |
| 22:00 | 同上，任务链完全一致 |
| 任意时刻 | 点计划卡改关卡／跳过／立刻跑；或在频道里说人话，也可以说「抄 TO-9」走抄作业流程 |

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

### 计划卡（每天 07:50）

- **关卡下拉**：`上次作战关卡` / `1-7` / **当天开放的资源本** / 当期活动关。
  资源本按国服 04:00 的游戏日刷新过滤（`AP-5` 红票、`CA-5` 技能书、`CE-6` 龙门币、
  `LS-6` 作战记录、`SK-5` 基建材料），公告里出现「资源收集」时全部放开。
- 下拉选项的 `value` 是**不透明的 `choice:N`**，真实 argv 只存在按钮登记表的 `select_args` 里，
  Discord 侧无法注入任意关卡名。
- **上游更新**：每天先 `brew` 跟随 maa-cli，再 `maa update` / `maa hot-update` 跟随 Beta MaaCore
  与资源热更新层，结果写进卡片的「上游更新」字段；失败发红色告警并让 cron 记为失败。
- 发新卡时把旧卡片的组件 `PATCH` 成空，避免同时存在多张可点的卡。

### 日常执行（08:00 / 22:00）

任务链固定为：`StartUp → [每日剿灭] → Fight → Infrast → Recruit → Mall → Award → Custom(返回主界面)`

| 环节 | 行为 |
|---|---|
| StartUp | 顺手处理每日首次登录的签到 / 配给弹窗 |
| 每日剿灭 | 每个游戏日（04:00 刷新）的第一场追加一次当期剿灭，自动点开全权委托消耗代理指挥卡，MaaCore 在周上限 1800 合成玉后停止；同一游戏日的后续场次不再进 |
| Fight | 默认 `1-7`。`medicine = 0`、`stone = 0`、`series = 0` 由渲染器**硬编码**——只自动使用配置窗口内即将过期的理智药，**永不碎石**，连战次数由 MaaCore 按当前理智自动选最大 |
| Infrast | 固定 `mode = 20000`，只点游戏内的「一键 / 队列轮换」预设队列，不让 MAA 自行挑选、清空或覆盖进驻干员；设施只列 `Mfg / Trade / Reception` |
| Recruit | 刷新公招，4 次，`select = [4,5,6]`、`confirm = [3,4,5,6]`，不使用加急许可 |
| Mall | 访问好友 + 信用商店购物，不打信用作战 |
| Award | 领日常、周常，以及邮箱内全部可领取附件 |
| 返回主界面 | 用 MaaCore 的 Home 导航（每项都带 `Home@` 前缀，否则 `#back` 无从解析），等动画稳定后截图，再用本机 **Vision OCR 核对截图确实停在主页**——不在就重截，重试用尽才判红 |

跑完按 bundle ID 请求模拟器正常退出，`pkill` 兜底，再用不会自匹配的进程模式复核，最后断开 ADB。

**「跳过」只关掉 08:00 和 22:00 两个自动场次**；说「现在跑」派发的手动场豁免 skip，照跑。
启动时快照当场计划，运行中改计划不影响本场。

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

聊天里说「抄 TO-9」→ 从 [prts.plus](https://prts.plus) 匹配作业（prts 客户端过滤 + 森空岛练度校验）
→ 频道发候选卡片（▶️ 普通 / 🔥 突袭 / ❌ 取消 / 🔄 换一批）→ 点按钮后台执行：
渲染导航任务停到编队界面 → `maa copilot`（接管弹窗靠 ocrbox watchdog 兜底）→ 截图 → 清理 → 战报。

- 与日常**互斥**：`run.lock` + 08:00 / 22:00 前 25 分钟黑名单。
- 练度来自森空岛（`maa_skland.py`）：269 名干员的专精 / 模组 / 精英化参与匹配，
  缺口文案形如「缺：史尔特尔 专精2（你专精1）」，模组是软约束、半权重。签名公式已冻结黄金向量测试。
- **未接森空岛时自动降级**：按热度排序并明示「练度未校验」。
- 抄作业结束在结算界面，不受返回主页那道闸门约束。

### 安全边界

- `maa_action.py` 是**唯一**状态变更入口。技能文件（`SKILL.md`）明确禁止 LLM 直接写
  `plan.json` / `defaults.json` / `config.json` / 按钮登记表，也禁止直接调用 runner。
- LLM 能改的只有**今天的关卡**和**是否跳过**，其余（任务链、次数、吃药、ADB、模拟器、Discord 配置）
  一律拒绝。碎石**永久关闭**，不提供入口、脚本级硬拒。
- 技能明确要求**绝不索取、接收、复述或写入森空岛 / 鹰角 token**；用户想在聊天里贴，引导去本机终端。
- Discord 侧永不展示 maa-cli 的原始 stdout/stderr，只展示中文步骤和战报；CLI 原文只写本地日志。
- `copilot pick` 的 token 校验 `^[0-9a-f]{12}$`、index 限 `1..len(candidates)`、
  `mode ∈ {normal, raid}`、作业 id 断言 `isdigit()` 且 `0 < id < 1e9`。

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

然后建三条 cron。早晚场用**不同工作目录**传递场次标记，因为 Hermes 的 `--script` 不支持 argv：

```bash
hermes cron create "50 23 * * *" --name "MAA 计划卡" --script maa_plan_card.py --no-agent --deliver local
```

```bash
hermes cron create "0 0 * * *" --name "MAA 日常·早" --script maa_daily.py --no-agent --deliver local --workdir ~/.hermes/state/maa/am
```

```bash
hermes cron create "0 14 * * *" --name "MAA 日常·晚" --script maa_daily.py --no-agent --deliver local --workdir ~/.hermes/state/maa/pm
```

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
maa_action.py plan set-stage <关卡>     # 「刷 CE-6」「今天 1-7」
maa_action.py plan set-stage ""         # 「刷上次的本」
maa_action.py plan skip-today on|off    # 「今天别跑」/「恢复今天」
maa_action.py plan show                 # 「今天怎么跑」
maa_action.py run now                   # 「现在跑」（豁免 skip）
maa_action.py log tail                  # 「最近怎么了」
maa_action.py copilot find <关卡>       # 「抄 TO-9」
maa_action.py copilot status|cancel|again <token>
maa_action.py copilot pick <token> <n> <mode>   # 只由候选卡片按钮触发
```

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
| `fight_times` | `2147483647` | 生产值。配合 `series = 0` 一直打到理智不足 |
| `proxy_failure_limit` | `2` | 连续多少批代理指挥失误触发熔断 |
| `proxy_failure_salvage` | `true` | 熔断后是否补跑去掉 Fight 的链条 |
| `retention_days` | `14` | `runs/` 产物保留天数（子区固定 24h） |
| `copilot_blackout_minutes` | `25` | 自动场次前多久禁止派发抄作业 |

`defaults.json` 是持久默认（关卡、任务开关），`plan.json` 是**带日期戳的今日覆盖**，
过期自动失效。`medicine` / `stone` 已从可变状态里移除，改由 runner 硬编码策略。

## 开发

```bash
python3 -m unittest discover -s tests -v
```

151 个 stdlib 单测，约 10 秒，不碰模拟器。**必须在仓库根目录跑。**

改完代码要跑 `deploy.py` 才生效；改了 adapter / `config.yaml` / `.env` / `SKILL.md` 还要重启 gateway。

文档分工：

| 文件 | 讲什么 |
|---|---|
| [DEV_GUIDE.md](./DEV_GUIDE.md) | **现状的链路怎么串起来、改哪里影响什么**。§8 是不可破坏的不变量，§10 是「症状 → 看哪里」，§11 是 14 条踩坑记录 |
| [PLAN.md](./PLAN.md) | 一期的设计意图与决策表 |
| [PLAN_PHASE2.md](./PLAN_PHASE2.md) | 二期（prts.plus 抄作业）方案 |
| [PLAN_NAV_GATE.md](./PLAN_NAV_GATE.md) | 导航闸门 + 自动任务模拟器探测 |
| [REVIEW_PHASE2.md](./REVIEW_PHASE2.md) | 二期五轮评审的 29 条问题与修复记录 |

## 已知限制

- **只测过一种环境**（见[自用优先](#自用优先)）。国服以外的服务器、其他模拟器都还没有适配。
- **BlueStacks 需要活跃的图形会话**——锁屏 / 关显示器没问题，注销或切换用户会失败。
- **macOS 上 maa-cli 的默认配置目录**是 `~/Library/Application Support/com.loong.maa/config`，
  与本项目约定不同，所以每个子进程都显式注入 `MAA_CONFIG_DIR`。新增调用 maa-cli 的代码
  一定要走 `maa_subprocess_env()`，不要裸 `subprocess.run`。
- **代理开关是分裂的**：日常 `maa run` 要剔除代理，计划卡的 `maa update` 必须**带着**代理访问 GitHub。
  代理变量只在 maa-cli 子进程里移除，不改用户会话或其他程序的环境。
- **删子区需要 bot 的 Manage Threads 权限**，没有就只能靠 `auto_archive_duration=1440` 归档。
- **关卡未解锁时抄作业会失败**（活动本按顺序解锁，导航找不到节点）——这是预期行为，不是 bug。
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
| 森空岛 | 练度数据来源，社区逆向接口 | — |

感谢 MAA 项目组把这条自动化链路做成可能。

## 许可

[MIT](./LICENSE) © 2026 ffkiyo7

许可只覆盖本仓库自己的代码与文档。上面那些第三方项目各自适用它们自己的许可。
