# maa-remote

在 macOS 上用 Discord 遥控 [MAA](https://github.com/MaaAssistantArknights/MaaAssistantArknights)，
跑《明日方舟》国服日常。游戏是 PlayCover 里的原生 iOS 版。

- 07:50 和 21:50 各发一张计划卡，可以改这一场的关卡，或者跳过这一场
- 08:00 和 22:00 跑完整日常：剿灭、刷理智、基建、公招、信用商店、领奖，跑完回到主界面
- 跑完发中文战报和截图，然后关掉游戏
- 新活动上线后，自动从 prts.plus 找作业，把活动关打一遍
- 聊天里可以说「现在跑」「抄 TO-9」「刷肉鸽」

自用项目，只在 Apple Silicon Mac + PlayCover 国服上跑过。配置和状态文件的格式随时可能改。

> ⚠️ 非官方工具，和鹰角、森空岛、MAA 官方都没有关系。用自动化可能违反游戏用户协议，账号风险自己承担。
> 森空岛接口是社区逆向出来的。仓库里没有任何凭据。

## 架构

不加任何常驻进程。Discord 网关和 cron 都用 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 现成的，
本项目只提供一组跑完就退的脚本，状态存在 `~/.hermes/state/maa/`。

| 时间（北京） | 脚本 |
|---|---|
| 每 15 分钟 | `maa_activity_watch.py`：活动自动抄作业 |
| 04:30 | `maa_depot.py`：仓库对表，每 7 天一次 |
| 07:50 / 21:50 | `maa_plan_card.py`：发计划卡；早场那次顺带更新 maa-cli / MaaCore |
| 08:00 / 22:00 | `maa_daily.py`：日常 |
| 按钮 / 聊天 | `maa_action.py`：唯一能改状态的入口 |

**仓库不是运行目录**：改完代码要跑 `deploy.py`，脚本才会拷进 `~/.hermes/`。

## 环境

- Apple Silicon Mac；Python 3.9+，只用标准库
- Hermes Agent gateway 常驻，并且已经连上 Discord
- [PlayCover 的 MAA 分支](https://github.com/hguandl/PlayCover) 装好明日方舟国服，开启 MaaTools，端口 `1717`
- maa-cli（`brew install MaaAssistantArknights/tap/maa-cli`）
- 用 [BetterDisplay](https://github.com/waydabber/BetterDisplay) 建一块虚拟屏 `MAA-Game`，给游戏窗口单独用。窗口不在屏上时，MaaTools 截不到图
- 给 gateway 用的 Python 开「辅助功能」权限，用来挪游戏窗口

## 部署

```bash
python3 -m unittest discover -s tests
```

```bash
MAA_CHANNEL_ID=<频道ID> python3 deploy.py
```

```bash
python3 deploy.py --check
```

第一次部署要带 `MAA_CHANNEL_ID`，以后直接跑 `python3 deploy.py` 就行。`--check` 只做检查，不改任何东西。
跑过 `hermes update` 之后要重新部署。cron 的建法见 [DEV_GUIDE](./docs/DEV_GUIDE.md)（cron 按 UTC 写，场次靠 `--workdir` 区分：`…/maa/am` 是早场，`…/maa/pm` 是晚场）。

## 文档

- [docs/DISCORD_GUIDE.md](./docs/DISCORD_GUIDE.md)：频道里会发生什么，能点什么，能说什么
- [docs/DEV_GUIDE.md](./docs/DEV_GUIDE.md)：链路怎么串起来、配置项（§6.2）、部署和 cron（§7）、不能破坏的约定、出问题先看哪里、踩过的坑
- [docs/spikes/](./docs/spikes)、[docs/archive/](./docs/archive)：实测记录和历史方案

## 致谢与许可

全部通过命令行或 HTTP 调用，不含下面这些项目的源码：MAA / maa-cli（AGPL-3.0）、Hermes Agent（MIT）、
[prts.plus](https://prts.plus)、[明日方舟一图流](https://ark.yituliu.cn) 和 [企鹅物流](https://penguin-stats.io)（数据按 CC BY-NC 4.0 使用，会匿名上报掉落）。

[MIT](./LICENSE) © 2026 ffkiyo7
