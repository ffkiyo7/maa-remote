---
name: maa-operator
description: "通过受控脚本查看或修改今天早场 / 晚场各自的 MAA 日常计划，支持关卡、跳过、抄作业（选中即执行）和查看中文执行结果。"
version: 4.0.0
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
   不吃任何场次覆盖，永远走缺口默认关。
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
```

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
- “现在跑” → `run now`（手动场豁免 skip 且不吃场次覆盖：设了跳过也照跑，走默认关）
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
- 「🤖 自动作业已排程」卡 → 不需要用户确认，到点自己跑；不能把“已排程”说成“已完成”，
  最终状态只认随后那条战报。
- 战报里某关 ❌ → 不要建议立刻重试；下一轮 watcher 会按退避自己再试，
  练度不满足的关要等练度或等上游出新作业。

一句话同时包含多个明确变更时，逐个调用对应子命令，并在全部执行后简短合并脚本
确认语。关卡或意图不明确时先问一句，不要猜。正在运行时脚本会明确说明不打断本场、
新计划何时生效；必须保留这层说明。聊天里只转述脚本与卡片给出的状态，
不要声称未经验证的执行结果。
