# MAA 长草助手（macOS + Discord）

这里是 [PLAN.md](./PLAN.md) 的可部署实现。核心运行时仅包含 3 个 cron job 和 1 个
按钮/聊天动作入口，不新增常驻进程。

## 已实现

- 北京时间 07:50 计划卡；`1-7`、当天开放的常规资源本（`AP-5` 红票、`CA-5`
  技能书、`CE-6` 龙门币、`LS-6` 作战记录、`SK-5` 基建材料）与开放活动关集中在
  Discord 下拉菜单中。资源本按国服 04:00 刷新与全开放活动筛选；旧计划卡的控件会
  被收起，组件登记与 agent-book 共用锁。
- 08:00 / 22:00 完整任务链：StartUp → Fight → Infrast → Recruit → Mall → Award →
  Custom 返回主界面。
- 基建固定使用 `mode = 20000` 的游戏内预设队列轮换：只点“一键轮换/队列轮换”，
  不让 MAA 自行挑选、清空或覆盖干员进驻；设施列表只保留制造、贸易、会客室用于该
  模式支持的基础操作，控制中枢、电站、宿舍和办公室不进入配置。
- `medicine = 0`、`stone = 0` 与 `series = 0` 由渲染器硬编码；只自动使用配置窗口内
  即将过期的理智药，不提供吃药策略按钮或状态覆盖。
  MaaCore 会自动选择当前理智支持的最大连战次数；Beta 6.16.0 已适配国服 2.7.61
  的新版连战列表（当前代码支持 1–10 次，maa-cli 0.7.5 文档中的 1–6 尚未同步）。
- 代理指挥失误（作战录像跟不上关卡随机性）会被 MaaCore 报成「掉落理智 ×N」并照样判
  Fight 完成。战报把返还的理智剔出掉落；整场一关没打成时刷理智翻红、写「实际完成 0 次」，
  并提示去手动重打三星刷新录像。连续 2 批失误就熔断 `maa run`，再补跑一趟去掉刷理智的
  链条把基建/公招/商店/奖励捞回来（两趟共用同一个超时预算）。见 DEV_GUIDE §11.13。
- 启动时快照当场计划；运行中的计划修改不影响本场。
- 「跳过」只关掉 08:00、22:00 两个自动场次；说「现在跑」派发的手动场豁免 skip，照跑。
- Discord 子区内每 5 秒编辑同一条中文进度消息；原始 stderr/stdout 只写入本地日志，
  频道、子区和“最近结果”都不展示 CLI 原文。
- 任务链最后用 MaaCore 的 Home 导航返回游戏主页面（`task_names` 每项都带 `Home@`
  前缀，否则 `#back` 无从解析，回到主页也判不了成功），等待动画稳定后截图，再用本机
  Vision OCR 核对截图确实停在主页——不在就重截，重试用尽才判红。截图发生在独立
  `maa closedown Official` 之前，随后退出模拟器、兜底清进程并断开 ADB。
  抄作业结束在结算界面，不受这条主页闸门约束。
- 模拟器按 bundle ID 请求正常退出，随后 `pkill` 兜底，并用不会自匹配的进程模式复核。
- 启动前预检 maa-cli/ADB，ADB 后预检国服包；`maa run` 最长 50 分钟，为 Hermes 外层
  1 小时上限预留清理时间，卡在更新或登录页会终止并发红色告警。
- 确定性战报、失败红色告警、24 小时子区清理与 14 天运行产物保留。
- 计划卡发布后按 7 天间隔运行一次 `maa update`；失败会发红色告警并令 cron 报错。
- `maa_action.py` 是唯一状态变更入口；技能明确禁止直接写 `plan.json`。
- **二期（2026-08-02）：prts.plus 抄作业**。计划卡新增「🤖 抄作业」下拉；聊天说
  「抄 TO-9」→ 从 prts.plus 匹配作业（prts 客户端过滤 + 森空岛练度校验）→ 频道发
  候选卡片（▶️ 普通 / 🔥 突袭 / ❌ 取消 / 🔄 换一批）→ 点按钮后台执行：
  渲染导航任务停到编队界面 → `maa copilot`（接管弹窗 watchdog 兜底）→ 截图 →
  清理 → 战报。与日常互斥（`run.lock` + 08:00/22:00 前 25 分钟黑名单）。
- 练度来自森空岛（`maa_skland.py`，token 只从 stdin 或 `SKLAND_HG_TOKEN` 进，
  签名公式已冻结黄金向量测试）；269 名干员的专精/模组/精英化参与匹配，
  缺口文案形如「缺：史尔特尔 专精2（你专精1）」，模组为软约束半权重。
- 未接入森空岛时自动降级：按热度排序并明示「练度未校验」。

## 部署与验证

`deploy.py` 会先在 `~/.hermes/backups/maa-setup/` 备份被修改的 Hermes 文件，再幂等部署
脚本、状态、maa-cli 配置、技能、按钮白名单、频道绑定和免 @ 配置。

真实 Discord 频道 ID 不入库：`state/config.json` 存的是占位符，部署时由
`MAA_CHANNEL_ID` 或本机已部署的配置解析。

```bash
python3 -m unittest discover -s tests -v
python3 deploy.py                                  # 已配置过的机器
MAA_CHANNEL_ID=<频道数字ID> python3 deploy.py      # 新机器首次部署
```

cron 的早晚任务使用不同工作目录传递场次标记，因为 Hermes 的 `--script` 不支持 argv：

```bash
hermes cron create "50 23 * * *" --name "MAA 计划卡" --script maa_plan_card.py --no-agent --deliver local
hermes cron create "0 0 * * *" --name "MAA 日常·早" --script maa_daily.py --no-agent --deliver local --workdir ~/.hermes/state/maa/am
hermes cron create "0 14 * * *" --name "MAA 日常·晚" --script maa_daily.py --no-agent --deliver local --workdir ~/.hermes/state/maa/pm
```

## 本机落地状态（2026-08-01）

- 已完成：BlueStacks Air 安装；4 核 / 4GB；1920×1080；本机 ADB
  `127.0.0.1:5555`；启动→ADB→截图→退出冒烟测试；Discord Manage Threads 实测。
- 已完成：稳定版 maa-cli 0.7.5、Beta MaaCore 6.16.0 与配套资源安装；Beta 修复了国服
  2.7.61 新版连战下拉框的 `FightSeries-Opened` 识别；国服
  `com.hypergryph.arknights` 2.7.61 已侧载并登录到主页面。
- 已完成 P1：实机成功选择 6 连战，单批消耗 36 理智并正确汇总掉落；全程
  `medicine = 0`、`medicine_expire_days = 0`、`stone = 0`。Beta 在有限 `times=6`
  的改选后偶发重识别不到倍率，导致又启动一批；已人工中止并确认 runner 完整清理。
- 已完成 P2：以 `times=0` 跑通完整 Discord E2E，StartUp、Fight、预设队列轮换基建、
  Recruit、Mall、Award 全部 Completed；截图在 CloseDown 前生成，战报发送成功，
  `return_code=0`、`cleanup_errors=[]`，随后确认 BlueStacks/MAA 进程与 ADB 连接均为空。
- 生产 `fight_times` 已恢复为 `2147483647`；三个 MAA cron 在实机验收后恢复：

```bash
hermes cron resume 03d5e98ce8d4
hermes cron resume 50cd1fb095f2
hermes cron resume 4f48cf619d9f
```

- P1 用有限 `fight_times=6` + `series=0` 验证了 6 连战。由于 Beta 新 UI 改选后的
  倍率重识别仍有缺陷，P2 验收临时使用 `fight_times=0`，让 Fight 立即达限但保留完整
  任务链；验收后已恢复 `2147483647`。生产规则始终是 `series=0` 自动选择最大可用倍率。

完整端到端验收必须在上述 GUI 步骤完成后执行，不能用单元测试替代真实游戏识别。

本机使用 BlueStacks Air 随应用签名分发的 `hd-adb`（35.0.1）。Homebrew 的
`android-platform-tools` 37.0.1 下载物与 cask 校验和不一致，因此未绕过校验强装；
maa-cli profile 与 runner 已统一指向内置 ADB。

macOS 上 maa-cli 0.7.5 的平台默认配置目录是 `~/Library/Application Support/com.loong.maa/config`，
与方案约定不同。所有脚本会显式注入 `MAA_CONFIG_DIR=~/.config/maa-cli`；本机失效的代理变量
也只在 maa-cli 子进程中移除，不改变用户会话或其他程序的代理环境。
