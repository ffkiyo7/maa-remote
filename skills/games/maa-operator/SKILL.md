---
name: maa-operator
description: "通过受控脚本查看或修改今天的 MAA 日常计划，支持关卡、跳过、抄作业和查看中文执行结果。"
version: 2.0.0
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
   立即执行也必须走 `maa_action.py run now` / `copilot pick`，由它负责加锁和派发。
3. 永远不接受、建议或模拟碎石。用户要求使用源石时，直接说明“碎石永久关闭”，
   不执行任何变通命令。
4. 每个 Fight 任务由 runner 硬编码 `series = 0`，让 MaaCore 自动选择当前理智可支持的
   最大连战次数；不得改成固定单次或其他值。客户端连战上限由当前 MaaCore/游戏版本识别，
   不在技能中写死。
5. 理智规则固定为普通理智药 0、自动使用临期药、碎石 0，默认关卡是 `1-7`，并持续
   作战到剩余理智不足；不接受修改吃药或碎石。当天第一场会用代理卡全权委托刷当期
   剿灭，满 1800 合成玉/周后自动停，同一游戏日不再进第二次。只允许修改今天的关卡
   和是否跳过。不得修改任务链、次数、ADB、Discord、模拟器或默认配置。
6. 原样转述脚本 JSON 中的 `message`。若 `ok:false`，不要自行改写成成功，也不要
   绕过校验重试别的写法。
7. **绝不索取、接收、复述或写入森空岛/鹰角 token**。用户想在聊天里贴 token 时，
   引导他在本机终端运行 `python3 ~/.hermes/scripts/maa_skland.py link` 并从 stdin 粘贴。
8. 抄作业关卡不在本地索引里就直接拒绝，不许猜别名。

## 唯一允许的命令

用 terminal 以 argv 形式执行，不拼接额外 shell 操作：

```text
python3 ~/.hermes/scripts/maa_action.py plan set-stage <关卡>
python3 ~/.hermes/scripts/maa_action.py plan set-stage ""
python3 ~/.hermes/scripts/maa_action.py plan skip-today on
python3 ~/.hermes/scripts/maa_action.py plan skip-today off
python3 ~/.hermes/scripts/maa_action.py plan show
python3 ~/.hermes/scripts/maa_action.py run now
python3 ~/.hermes/scripts/maa_action.py log tail
python3 ~/.hermes/scripts/maa_action.py copilot find <关卡>
python3 ~/.hermes/scripts/maa_action.py copilot status <token>
python3 ~/.hermes/scripts/maa_action.py copilot cancel <token>
```

`copilot pick <token> <n> <mode>` 只通过候选卡片上的按钮触发，聊天里不要手拼；
token 形如 12 位十六进制，来自搜索确认消息，不要猜测。

## 意图映射

- “刷 CE-6 / 今天 CE-6 / 改成 1-7” → `plan set-stage CE-6` / `1-7`
- “刷上次的本” → `plan set-stage ""`
- “默认关卡 / 恢复 1-7” → `plan set-stage 1-7`
- “吃药 / 不吃药 / 揉揉乐” → 说明规则已固定：普通药 0、只自动使用临期药、碎石 0
- “今天别跑 / 跳过今天” → `plan skip-today on`（只关掉 08:00、22:00 两个自动场次）
- “恢复今天” → `plan skip-today off`
- “今天怎么跑 / 看计划” → `plan show`
- “现在跑” → `run now`（手动场豁免 skip：设了跳过也照跑，这是明确指令）
- “看日志 / 最近怎么了” → `log tail`（只返回中文结果，不展示 CLI 原文）
- “抄 TO-9 / TO-9 有没有作业 / 帮我抄一下” → `copilot find TO-9`
- “作业结果呢 / 匹配到哪了” → 让用户直接看频道里的候选卡片；也可 `copilot status <token>`
- “换一批 / 重找” → 候选卡片上的 🔄 按钮（`copilot again`），不用手拼
- “不抄了 / 取消” → 候选卡片上的 ❌ 按钮（`copilot cancel`）

一句话同时包含多个明确变更时，逐个调用对应子命令，并在全部执行后简短合并脚本
确认语。关卡或意图不明确时先问一句，不要猜。正在运行时脚本会明确说明不打断本场、
新计划何时生效；必须保留这层说明。候选卡片上的 ▶️（普通）/ 🔥（突袭）按钮由
`copilot pick` 校验互斥与黑名单时段后派发后台执行，聊天里只转述结果，不要声称
未从脚本或卡片验证的执行状态。
