---
name: maa-operator
description: "通过受控脚本查看或修改今天早场 / 晚场各自的 MAA 日常计划，支持关卡、跳过、抄作业（选中即执行）、肉鸽（卡片选参数 + 优雅停止）和查看中文执行结果。"
version: 4.1.0
metadata:
  hermes:
    tags: [maa, arknights, games, automation]
    category: games
---

# MAA 长草助手

这个技能只负责把 Discord 里的自然语言意图翻译成确定性的本地动作。所有状态以
`~/.hermes/state/maa/` 为准，执行入口只有 `~/.hermes/scripts/maa_action.py`。

## 不可绕过的安全边界

1. **禁止使用 `write_file`、Python 临时代码、重定向或任何其他方式直接修改**
   `plan.json`、`defaults.json`、`config.json`、`last_run.json`、`copilot/`
   下的任何文件或按钮登记表。
2. 一切计划变更必须通过下面列出的 `maa_action.py` 子命令。不要调用
   `maa_daily.py`、`maa_copilot_worker.py`、`maa_copilot.py`、`maa_skland.py`，
   立即执行也必须走 `maa_action.py run now` / `copilot find`，由它负责加锁和派发。
3. 永远不接受、建议或模拟碎石。用户要求使用源石时，直接说明“碎石永久关闭”，
   不执行任何变通命令。
4. 每个 Fight 任务由 runner 硬编码 `series = 0`，让 MaaCore 自动选择当前理智可支持的
   最大连战次数；不得改成固定单次或其他值。客户端连战上限由当前 MaaCore/游戏版本识别，
   不在技能中写死。
5. 理智规则固定为普通理智药 0、自动使用临期药、碎石 0，默认关卡是 `1-7`，并持续
   作战到剩余理智不足；不接受修改吃药或碎石。当天第一场会用代理卡全权委托刷当期
   剿灭，满 1800 合成玉/周后自动停，同一游戏日不再进第二次。只允许修改今天**某一场**
   的关卡和是否跳过。不得修改任务链、次数、ADB、Discord、模拟器或默认配置。
5b. **计划是按场次的**：`am` = 早场 08:00，`pm` = 晚场 22:00。`plan set-stage` 与
   `plan skip-today` 都**必须**带场次参数，只接受 `am` / `pm`；改一场不影响另一场。
   用户没说是哪一场时**先问**，不要替他挑，也不要两场都改。「现在跑」的手动场
   不吃任何场次覆盖：`run now` 只发一张选关卡，用户**在卡上选了关才开跑**，
   10 分钟不选作废；距 08:00 / 22:00 不足 30 分钟会被拒绝（定时场优先）。
   **不要替用户选关**，也不要自己调用 `run start`——那条只由卡片登记表触发。
6. 原样转述脚本 JSON 中的 `message`。若 `ok:false`，不要自行改写成成功，也不要
   绕过校验重试别的写法。
7. **绝不索取、接收、复述或写入森空岛/鹰角 token**。用户想在聊天里贴 token 时，
   引导他在本机终端运行 `python3 ~/.hermes/scripts/maa_skland.py link` 并从 stdin 粘贴。
8. 抄作业关卡不在本地索引里就直接拒绝，不许猜别名。
9. 活动上线后**自动抄作业是静默执行的**：`maa_activity_watch.py` 每 15 分钟排一轮，
   发一张「🤖 自动作业已排程」卡（只有「取消本轮」一个按钮），到点自己开跑，跑完发战报。
   聊天侧不得直接调用 watcher 或 worker，也不得替用户取消。
   自动与手动都**只执行完全满足练度、无软硬缺口**的作业：绝不使用助战、绝不忽略练度要求。
   活动已轮换、理智消耗未知、森空岛读取失败或理智不足时脚本会拒绝，必须原样转述。
10. 活动关一律**普通 + 突袭**都打，两种都过了才算通关；资源本 / 主线只有普通。
   突袭失败这一关就不算通关，下一轮只补突袭。
11. **肉鸽的四个参数（主题 / 模式 / 难度 / 分队）只能用户在卡上点**。聊天里
   **绝不**替用户选主题或难度，也不要自己调 `roguelike set` / `roguelike start`——
   说「刷肉鸽」就发卡（`roguelike card`），让用户在卡上选完自己点「▶️ 开始」。
   用户说要「换主题」时也只发卡，由他在下拉里点。
12. 肉鸽**不消耗理智**，不影响日常刷图；但它一样持 `run.lock`、一样开模拟器，
   所以日常临近 / 仓库对表窗口 / 模拟器在用时脚本会拒绝，原样转述即可。
   上一轮被「立即中止」过时**换主题会被拒**（上游只在同主题下善后残局），
   照脚本的话说，不要建议绕过。

## 唯一允许的命令

用 terminal 以 argv 形式执行，不拼接额外 shell 操作：

```text
python3 ~/.hermes/scripts/maa_action.py plan set-stage <关卡> <am|pm>
python3 ~/.hermes/scripts/maa_action.py plan set-stage "" <am|pm>
python3 ~/.hermes/scripts/maa_action.py plan skip-today on <am|pm>
python3 ~/.hermes/scripts/maa_action.py plan skip-today off <am|pm>
python3 ~/.hermes/scripts/maa_action.py plan show
python3 ~/.hermes/scripts/maa_action.py run now
python3 ~/.hermes/scripts/maa_action.py log tail
python3 ~/.hermes/scripts/maa_action.py copilot card
python3 ~/.hermes/scripts/maa_action.py copilot find <关卡>
python3 ~/.hermes/scripts/maa_action.py copilot status <token>
python3 ~/.hermes/scripts/maa_action.py copilot cancel <token>
python3 ~/.hermes/scripts/maa_action.py copilot mark-cleared <关卡> [normal|raid|all]
python3 ~/.hermes/scripts/maa_action.py copilot unmark <关卡>
python3 ~/.hermes/scripts/maa_action.py roguelike card
python3 ~/.hermes/scripts/maa_action.py roguelike stop
python3 ~/.hermes/scripts/maa_action.py roguelike abort
python3 ~/.hermes/scripts/maa_action.py roguelike status
```

肉鸽的 `roguelike set <字段> <值>` 与 `roguelike start` **只由卡片上的下拉和按钮触发**，
不在这张清单里——聊天侧永远只发卡（`roguelike card`），选参数和开跑都由用户自己点。

`copilot cancel <token>` 通常由「已排程」卡上的「❌ 取消本轮」按钮触发；
token 形如 12 位十六进制，来自那张卡，不要猜测。已经开跑的一轮取消不了。

## 意图映射

- “早上刷 CE-6 / 晚上改成 1-7” → `plan set-stage CE-6 am` / `plan set-stage 1-7 pm`
- “刷上次的本” → `plan set-stage "" <am|pm>`
- “默认关卡 / 恢复 1-7” → `plan set-stage 1-7 <am|pm>`
- “吃药 / 不吃药 / 揉揉乐” → 说明规则已固定：普通药 0、只自动使用临期药、碎石 0
- “早场别跑 / 今晚跳过” → `plan skip-today on am` / `plan skip-today on pm`
  （只关掉那一场，另一场照常）
- “恢复早场 / 晚上还是跑吧” → `plan skip-today off <am|pm>`
- “今天怎么跑 / 看计划” → `plan show`（一次念完两场）
- “现在跑” / “手动跑 CE-6” → `run now`（**只发选关卡**，用户从卡上的下拉选关才开跑，
  10 分钟不选作废；带关卡名也照样只发卡、让用户点，不要替他选。手动场豁免 skip：
  设了跳过也照跑。距 08:00 / 22:00 不足 30 分钟会拒绝，原样转述）
- “看日志 / 最近怎么了” → `log tail`（只返回中文结果，不展示 CLI 原文）
- “抄作业 / 有什么关能抄 / 抄作业卡” → `copilot card`（发一张当前能抄的关一览卡，
  用户从卡上的下拉选关；不要替用户选）
- “抄 SR-8 / SR-8 有没有作业 / 帮我抄一下” → `copilot find SR-8`（**选中即执行**：
  搜到完全满足练度的作业就直接开跑，没有候选可挑）
- “活动打到第几关 / 自动抄到哪了 / 哪些关还没打” → `copilot card`（卡片上每关标
  ✅ 已通 / 🔁 普通已通突袭待补 / ⏳ 排程 HH:MM / 🚫 练度不满足 / ⛔ 上游无导航）
- “这一轮到哪了” → `copilot status <token>`（token 来自「已排程」卡）
- “不抄了 / 取消这轮” → 「已排程」卡上的 ❌ 按钮（`copilot cancel`）
- “再打一遍 SR-6” → `copilot find SR-6`，已通的关也允许重打
- “SR-7 我自己打过了 / 别再抄 SR-7 / 这关我手动通了” → `copilot mark-cleared SR-7`
  （写进账本，自动排轮从此跳过；不传模式就按当前规则取要打的模式，用户明说
  「突袭也打过了」才加 `all`）。**用户没说打过的关不要替他标**——标错等于永远漏抄。
- “标错了 / SR-7 其实没打过” → `copilot unmark SR-7`（撤销记账，下一轮重新考虑）
- 「🤖 自动作业已排程」卡 → 不需要用户确认，到点自己跑；不能把“已排程”说成“已完成”，
  最终状态只认随后那条战报。
- 战报里某关 ❌ → 不要建议立刻重试；下一轮 watcher 会按退避自己再试，
  练度不满足的关要等练度或等上游出新作业。
- “刷肉鸽 / 开肉鸽卡 / 打集成战略 / 换个肉鸽主题” → `roguelike card`（发一张
  4 个下拉 + 3 个按钮的配置卡；**不要替用户选主题 / 模式 / 难度 / 分队**，
  也不要替他点「开始」）
- “肉鸽停 / 打完这局停 / 跑完这把就别跑了” → `roguelike stop`（本局结束后退出，进度不丢）
- “肉鸽立刻停 / 马上停 / 现在就退” → `roguelike abort`（当前这一局作废，要提醒这一点）
- “肉鸽到哪了 / 跑了几局了” → `roguelike status`（只读）
- 换主题前提醒一句：**先在游戏里把那个肉鸽主题置顶到终端**，上游的自动导航不保证长期可用。

一句话同时包含多个明确变更时，逐个调用对应子命令，并在全部执行后简短合并脚本
确认语。关卡或意图不明确时先问一句，不要猜。正在运行时脚本会明确说明不打断本场、
新计划何时生效；必须保留这层说明。聊天里只转述脚本与卡片给出的状态，
不要声称未经验证的执行结果。
