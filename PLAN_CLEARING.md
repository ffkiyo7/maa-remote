# 三期：活动通关队列 + 材料驱动选关

> **本文按顺序分七步（0~6），每一步都可以单独派发给一个没有本次对话上下文的 agent 执行。**
> 每步都自带「前置 / 目标 / 做法 / 允许改动的文件 / 禁止触碰 / 验收边界」六节。
>
> **本文不含实现代码。** 只描述做什么、改哪里、怎么算做完。bash 验收命令和表格形式的
> 数据结构描述是验收边界的一部分，不算实现代码。写代码是执行 agent 的事。
>
> **本文的线上快照会过期。** 每步开工第一件事是按全局护栏 9 对一遍现场；对不上就以
> 现场为准并回报，不要照着旧快照动手。

**进度**：步骤 0 ✅（`a983ccb`）· 步骤 1 ✅（`49da169`，含一条遗留观测项）· 步骤 2~6 未开工

---

## Context

一期（日常自动刷本）与二期（prts.plus 抄作业）都已上线并结案。现在要做的是第三件事：
**把一个活动从头打到尾，并且让"今天刷哪个关"由真实材料缺口决定，而不是由手工权重猜。**

拆开来是两条互相咬合的线：

1. **通关队列（clearing）** —— 活动上线后自动按顺序推进 AT-1 → AT-2 → … → AT-8 →
   （全开后）EX 段，每关抄一份作业打过去。
2. **材料驱动选关（farming）** —— 知道自己仓库里缺什么、消耗多快，据此决定日常刷哪个关。

第 1 条卡在一个地基问题上：**MAA 只为每个活动写了 4 个左右的推荐关导航定义**，
其余关卡在资源里根本不存在。所以第 2 步（导航定义生成器）是整条 clearing 的成败点，
它不通，后面全白搭——这就是为什么它排在队列本身前面。

第 0 步和第 1 步是**清障**：现存的两个 bug 会卡住后面的验证，基建配置有一处仓库与
线上不一致的隐患。它们不属于三期新功能，但必须先处理掉。

---

## 地基事实

**开工前读完这一节。** 每一条都是核对过的，不是从文档推的。

### A. 仓库现状（已在位，不要重复造）

| 事实 | 落点 |
|---|---|
| maa-cli 用户资源覆盖层**已启用** | `config/maa-cli/profiles/default.toml` 的 `user_resource = true` |
| 覆盖层当前只有一条任务 | `config/maa-cli/resource/tasks/tasks.json` → `InfrastDormDoubleConfirmButton` |
| 覆盖层的部署路径 | `deploy.py: deploy_maa_config()` → `~/.config/maa-cli/resource/tasks/tasks.json` |
| Tile-Pos 关卡枚举**已实现**（0.53s 建 2099 条 code→stageId） | `maa_copilot.build_stage_index()` / `_resource_dirs()` / `resource_signature()` |
| 关卡理智读取**已实现**（但对 EX 返回 `None`） | `maa_copilot.stage_sanity_cost()`，数据源 `stages.json` |
| 森空岛 zone 进度**已归一化落盘** | `maa_skland.py` → `roster["activity_progress"]`；`maa_copilot.zone_progress_warning()` 在用 |
| 掉落解析**已实现** | `maa_daily._parse_drop_items()` / `_drop_batches()` / `_drop_pairs()` / `extract_summary()` |
| 关卡下拉的构造范式 | `maa_plan_card._stage_choices()` / `_stage_select()` / `_copilot_stage_select()` |
| 命令白名单入口 | `maa_action.dispatch()`（新增子命令必须进这里，并同步 `SKILL.md`） |
| 活动轮换 watcher（每 15 分钟） | `maa_activity_watch.py`，状态在 `state/maa/activity_watch.json` |

### B. Stages 导航定义的实测（2026-09-01）

MaaCore 自带资源 `resource/tasks/Stages/<前缀>.json` 的**顶层关卡键**：

```text
AT.json = AT-8, AT-7, AT-6, AT-4
TO.json = TO-7, TO-8, TO-9, TO-5
AD.json = AD-8, AD-7, AD-6, AD-3
CF.json = CF-8
（同目录另有 <前缀>-OpenOpt / -Open / -OpenOcr / <前缀>Chapter… 等上游写好的「进图」任务，
  不是关卡键）
```

`AT-4` 的定义只有三个字段：`algorithm: JustReturn`、`sub: [AT-4@AT-OpenOpt]`、
`next: [AT-4@SideStoryStage, AT-4@SwipeToStage]`。其中：

- `SideStoryStage` 是**前缀驱动的通用任务**（`tasks.json` 里 `text: []` + `baseTask: Stage`），
  OCR 目标由 `<关卡名>@` 前缀给出
- `SwipeToStage` 是通用的左滑找关
- 进活动地图的 `<前缀>-OpenOpt / -Open / -OpenOcr / <前缀>Chapter…` 上游都已写好，直接复用
- `SideStoryStage` 的 `ocrReplace` 里已有 `.*?([A-Z]{2}-EX-\d+)` 这一条
  —— **上游明确为 EX 关名写过 OCR 修正**，说明这套导航本来就设计成能处理 `AT-EX-3`，
  只是没人去填那几行 JSON

**推论（尚未验证，是第 2 步 spike 的假设）**：这些定义是可以自己补的，补进用户资源
覆盖层即可，每个活动几十行自动生成的 JSON。

### C. 三个样本指向同一个结论

| 样本 | `Stages/*.json` 键集 | 其他数据源 |
|---|---|---|
| TO（2026-08-02，`PLAN_NAV_GATE.md` 结论 2 实测） | 当时读到 TO-7/8/9 | Fight 实际接受 `TO-5, TO-7, TO-8, TO-9` |
| TO（2026-09-01 复核，`maa update` 之后） | **TO-7, TO-8, TO-9, TO-5** | 同上 —— **逐字吻合** |
| AT（2026-09-01，当期） | **AT-8, AT-7, AT-6, AT-4** | `config.activity_stages` = `['AT-8','AT-7','AT-6','AT-4']` —— **逐字吻合** |

### D. ⚠️ 对 `PLAN_NAV_GATE.md` 的订正

该文档结论 2 的「反直觉但已证伪」表里有一行：

> 判据是 `resource/tasks/Stages/TO.json` 的键 → ✗ 该文件只有 TO-7/8/9，但 TO-5 被接受

**这一行现在不成立。** 见上表：当前资源里 `TO.json` 含 TO-5。当时读到的应该是尚未
`maa update` 的旧资源。

后果：**「补 Stages 定义能否扩大 Fight 的接受集」重新变成开放问题，且倾向于「能」。**
这正是第 2 步 spike 存在的理由。**但它仍然只是假设**——三个样本证明的是「键集与接受集
一致」，没有证明「加键能改变接受集」。这两件事之间隔着一个 spike，不要跳过。

### E. `PLAN_NAV_GATE.md` 的未完成项（交叉引用，不并入本计划）

| 项 | 状态 |
|---|---|
| A-3 `activity_stages` 过期 | ✅ 已由 `maa_activity_watch.py` 解决 |
| A-1 / A-2 导航闸门 `stage_is_navigable` | ❌ 未实现（全库无此符号） |
| B 自动任务执行前探测模拟器 | ❌ 未实现（`maa_daily.py:778` 那个 probe 是**开机稳定性**探测，不是这个） |

**A-1 的判据会被第 2 步 spike 的结论直接改写**（如果导航定义能自己补，"哪些关能导航"
这个问题的答案就变了），所以先做第 2 步，再回头决定 A-1 怎么写。
**B 与本计划完全正交，可以随时插队做。**

### F. 基建覆盖层的实测记录（2026-09-01 全天，已随步骤 1 收敛）

> ⚠️ **本节是历史，不是现场。** 派发任何一步之前先自己跑一遍「开工前先对现场」
> （见全局护栏 9），不要拿这里的快照当线上真值——这一节本身就是被这么坑过一次
> 才写成现在这样的。

两版覆盖层：

```text
极简版   InfrastDormDoubleConfirmButton = { roi: [630, 430, 650, 290] }
         只覆盖 roi，template/action/next 全部继承上游

完整版   InfrastDormDoubleConfirmButton = { template: PopupConfirm.png,
           roi: [620,430,660,150], action: ClickRect,
           specificRect: [800,450,90,90], postDelay, next }
```

**当天时间线**（`~/.hermes/state/maa/runs/*.log` 的 Summary 段 + `~/.hermes/backups/maa-setup/`
各次部署前备份，两边可交叉核对）：

| 时刻 | 事件 | 进程内生效的覆盖层 | 结果 |
|---|---|---|---|
| 08:00:58 | am 场启动 | 极简 | 基建 08:05:54–08:13:57 **Completed**，逐个宿舍确认 |
| 22:00:49 | pm 场启动 | 极简 | 基建 22:06:45–22:15:16 **Completed**，4× `InfrastDormDoubleConfirmed` |
| 22:05:23 | 步骤 0 跑 `deploy.py` **顺手**把线上改成完整版 | — | 文件变了，但 pm 场进程已在 22:00:49 载入完毕 |
| 23:00:56 | 按本步骤原文部署极简版 | — | — |
| 23:01:43 | now 场启动 | 极简 | 基建 23:04:56–23:06:27 **Error**，0 次确认 |
| 23:09:37 | 恢复完整版（当前仓库与线上都是它） | — | 尚无 run 带着它启动过 |

**能得出的结论只有这些：**

1. **同一份极简配置，两成一败。** 三场读的都是极简版——22:00 那场虽然基建段发生在
   22:05 改写之后，但 MaaCore 在**进程启动时一次性载入 `resource/`**，内存里仍是
   22:00:49 载入的极简版。所以它不能作为完整版的证据。
2. **完整版至今零实测。** 它现在在仓库、在线上，但没有任何一场 run 是带着它启动的。
   下一场自动日常（08:00/22:00）才是它的第一次真实数据。
3. **23:01 那场有未排除的干扰**，不能直接归因给覆盖层：它是当天**第二次**基建，22:15
   刚排完班，四个宿舍本就没有"撤下干员"弹窗要确认（22:00 那场第二轮扫宿舍同样 0 次
   确认、照样 Completed）；而且它之后 Recruit / Mall / Award / Custom **全部 Error**，
   都是 13–30 秒的快速失败，更像界面卡死或连接退化，不像"红色确认条点不掉"。

**因此：宿舍确认这一步当前状态是「会飘」，两版都没有被证明是稳的。** 谁也别把
「换成 X 版就好了」当成已知结论——这是第 1 步交付后仍然敞着的一个观测项。

**已经修掉的**：`deploy.py --check` 当时只检查文件**存不存在**、不比内容，所以仓库与
线上不一致时它报"干净"，22:05 那次静默覆盖就是这么发生的。现在它会比对内容，
不一致列入 `mismatched` 并返回失败（commit `49da169`）。

### G. 两个现存 bug（第 0 步要修的）

当前 `state/maa/activity_watch.json` 里挂着两条错：

```text
probe_error         : "RuntimeError: maa activity 输出格式已变化：找不到活动关分区"
last_notified_error : "RuntimeError: 森空岛练度与理智刷新失败：
                       RuntimeError: skland HTTP 401: 登录已过期，请重新登录"
```

**G-1 空窗期被误判为故障。** 当期复刻活动「SideStory「墟」复刻」已于 2026-09-01
03:59:59 结束，现在 `maa activity Official` 的输出里只剩 `Opening mini games:` 分区，
没有活动关分区。`maa_plan_card.py:86` 的 `activity_snapshot()` 只要找不到活动关分区就
抛「输出格式已变化」——但**"当前没有活动"和"输出格式真的变了"是两件事**，现在被混成
了同一个异常，导致空窗期持续误报。

顺带：`config.activity_stages` 现在仍是已结束活动的 `['AT-8','AT-7','AT-6','AT-4']`，
没有被清空。

**G-2 森空岛 cred 过期。** `maa_skland.py:326` 的重认证只重试一次然后写 `last_error`
（这是对的，不能循环重试），但鹰角 token 本身过期时**只能由用户在本机终端重跑
`python3 ~/.hermes/scripts/maa_skland.py link`**，代码无法自愈。

这条直接卡住第 3 步：**EX 开放闸门完全建立在森空岛 zone 数据上**，401 期间拿不到数据。

### H. 剿灭也吃理智

`daily.toml` 里「每日剿灭」是一个 `Fight(stage=Annihilation)`，**它同样消耗理智**。
第 3 步的队列在排理智预算时**必须先扣掉剿灭那份**，否则会把可用理智算多。

---

## 全局护栏

**每个执行 agent 开工前必读。**

1. **仓库 ≠ 运行时**（DEV_GUIDE §0）。改完必须 `python3 deploy.py`；只改仓库不部署等于没做。
2. 测试必须**在仓库根目录**跑：`python3 -m unittest discover -s tests -v`，**全绿才算完**。
3. `DEV_GUIDE.md §8` 不变量表里的**全部条目一条都不许破**（表在长，不要在这里记条数）。
   新增行为若产生新不变量，**同步补进 §8 并配单测**。
4. **不许新增静默 `return`。** 这是 `PLAN_NAV_GATE.md` B-3 的教训：任何跳过都必须发
   Discord 并写 `last_run.json`，否则会永久停摆且无人知情。
5. 按钮脚本 **< 20s 返回**（不变量 #6）；长任务一律 `Popen` 甩给 worker。
6. Discord 下拉值必须经 `select_args` **查表**（不变量 #8），不能直通 argv。
7. `stone = 0` / `medicine = 0` **永不松动**（不变量 #1、#2）。
8. **每步做完各自提交**，不要攒成一个大 commit。
9. **开工前先对现场，本文的线上快照一律当过期处理。** 本文写于 2026-09-01，其中任何
   「线上现在是 X」「某文件还是 untracked」「某次实测证明 Y」都是**当时**的观测，
   随时可能被后续步骤自己改掉——步骤 0 跑一次 `deploy.py` 就顺手改写了步骤 1 的前提，
   而步骤 1 照着过期前提操作，把一版正在用的配置换掉了。所以每步第一件事是：

   ```bash
   git log --oneline -8
   git status --short
   python3 deploy.py --check                     # 仓库与线上是否已漂移
   diff <(python3 -m json.tool config/maa-cli/resource/tasks/tasks.json) \
        <(python3 -m json.tool ~/.config/maa-cli/resource/tasks/tasks.json)
   ```

   对不上就**以现场为准并回报差异**，不要照着本文的旧快照动手。
10. 遇到与本文描述**不符**的现实（尤其是第 2 步 spike），**停下来回报，不要自己改设计**。
11. **区分「实测证明」和「推断」。** 写进本文的实测结论必须能指到日志或备份文件；
    推断要显式标成推断。同一份配置在不同场次结果不同时，先找干扰项，不要急着归因。

---

## 步骤 0 — 修掉 watcher 的两条错

### 前置

无。可以立刻开工。

### 目标

让活动 watcher 在**空窗期**保持安静且正确（而不是持续误报"格式已变化"），并让森空岛
cred 过期时**用户知道要去终端做什么**。做完之后，下一个活动上线时 watcher 拿到的是干净
信号，第 2/3 步才有真实数据可验。

### 做法

**G-1 空窗期 ≠ 格式变化**（`maa_plan_card.py:84` 的 `activity_snapshot()`）

- 把「找不到活动关分区」这一个异常**拆成两种结果**：
  - **查询成功、确实没有活动**（输出结构可辨认，例如仍有 `Opening …` 分区）→
    返回一个**空快照**，不是异常
  - **输出无法辨认 / 命令失败** → 仍然抛异常，保持现有的 fail-loud 姿态
- 判别依据要写死在代码注释里，不要留成"看起来像"。
- `parse_activity_events()` **已经**把 `Opening ` 开头的行当作活动关分区的终止符，
  说明解析器本来就认识这个分区——判别依据从这里取，不要另造一套。
- **空快照时 `config.activity_stages` 要清空**。注意与 `PLAN_NAV_GATE.md` A-3 的约束区分：
  那条说的是「**查询失败**不能把空结果当成成功」，现在新增的是「**查询成功但确实无活动**
  要清空」。两者的分界就是上面那个判别依据，务必在注释里点明这个区别。
- watcher（`maa_activity_watch.py`）在空窗期应转入 idle 且**不重复告警**。

**G-2 森空岛 cred 过期的出路**（`maa_skland.py` 的 `last_error` 链路）

- 代码**不做**自动续期（鹰角 token 本身过期时无解，且绝不能循环重试——沿用现有姿态）。
- 要做的是让**用户知道该干什么**：`last_error` 已经写了指引文案，确认它能一路传到
  Discord 卡片上，而不是只躺在 `activity_watch.json` 里。
- **同一个错误不重复刷屏**（现有 `last_notified_error` 去重机制要保持有效）。
- 用户侧的动作（不是代码）：在本机终端跑 `python3 ~/.hermes/scripts/maa_skland.py link`。
  这一步要写进验收清单，但**agent 不能代跑**——token 绝不经过 agent 上下文（不变量 #16）。

### 允许改动的文件

- `scripts/maa_plan_card.py`
- `scripts/maa_activity_watch.py`
- `scripts/maa_skland.py`（**仅**错误上报路径，不碰签名与认证链）
- `tests/test_maa.py`、`tests/test_copilot.py`
- `DEV_GUIDE.md`

### 禁止触碰

- 森空岛的**签名算法与 grant→cred 链**（`PLAN_PHASE2.md` 里有黄金向量测试，改了会红）
- 不变量 #16：token 永不经 Discord / argv / agent 上下文
- `parse_activity_events()` 对**有**活动时的解析行为——现有解析是对的，只改"没有活动"这条路径
- 抄作业与日常任何执行链路

### 验收边界

```bash
python3 -m unittest discover -s tests -v      # 全绿

# 空窗期不再报错（现在正是空窗期，可直接验）
python3 ~/.hermes/scripts/maa_activity_watch.py
python3 -c "import json,os;d=json.load(open(os.path.expanduser( \
  '~/.hermes/state/maa/activity_watch.json'))); \
  print('probe_error =', repr(d.get('probe_error'))); print('status =', d.get('status'))"
# 期望：probe_error 为空 / 不存在；status 为 idle

# activity_stages 已清空
python3 -c "import json,os;print(json.load(open(os.path.expanduser( \
  '~/.hermes/state/maa/config.json')))['activity_stages'])"
# 期望：[]
```

新增单测必须覆盖**三种**输入，且三者结论互不相同：
1. 有活动的真实输出 → 正常解析出关卡（回归，不许改坏）
2. 空窗期的真实输出（只有 `Opening mini games:` 那种）→ **空快照，不抛异常，清空清单**
3. 真正无法辨认的输出（空串 / 乱码）→ **仍然抛异常，不清空清单**

**失败即停**：第 3 种如果也变成了"空快照"，就是把 fail-loud 改成了 fail-silent，
违反护栏 4，必须回退重做。

用户侧：`maa_skland.py link` 重跑后 `roster.json` 能刷新出 `activity_progress`。

---

## 步骤 1 — 挡住部署漂移 ✅ 已交付（commit `49da169`，2026-09-01 23:10）

### 前置

无。与步骤 0 正交，可并行。

### 目标

原目标是「消除仓库与线上不一致的隐患」。**交付时目标只完成了一半，另一半被证明是伪命题**：

- ✅ **已达成**：`deploy.py --check` 现在**比对内容**，漂移列入 `mismatched` 并返回失败，
  并做了反向漂移验证（手工改一个字节 → `--check` 必须红）。`config/maa-cli/resource/`
  已纳入版本管理。这条防线成立了——22:05 那次静默覆盖以后不会再无声发生。
- ❌ **不成立**：原文让"以线上为准、回灌极简版"。执行时这么做了，结果 23:01 实机
  **失败**，随即恢复完整版。见地基事实 F。

### 做法（已完成，留档）

1. `deploy.py --check` 从「只查文件存在」改为**逐文件比对内容**，输出加 `mismatched`
   字段，不一致即失败。
2. 反向漂移验证：手工改一个字节 → `--check` 报不一致 → 改回 → 干净。
3. `config/maa-cli/resource/` 纳入版本管理。
4. 仓库与线上**统一为完整重定义**（`template`/`roi:[620,430,660,150]`/`action`/
   `specificRect`/`postDelay`/`next`），二者内容一致。
5. 为什么用完整重定义、极简版为何被放弃，写进 **`DEV_GUIDE.md`**——
   **不要写进 `tasks.json`**：那是严格 JSON，`//` 或 `/* */` 会让 `json.tool` 和
   MaaCore 双双解析失败，也和本步骤的验收命令直接冲突。

### 遗留的观测项（不阻塞后续步骤）

宿舍确认目前**会飘**，两版覆盖层都没有被证明是稳的（地基事实 F）。完整重定义
**尚无任何一场 run 带着它启动过**，下一场自动日常（08:00 / 22:00）是它的第一次真实数据。
观测方式，不需要专门跑一场：

```bash
grep -cE "InfrastDormDoubleConfirmed" ~/.hermes/state/maa/runs/$(date +%F)-am.log
grep -E "Infrast (Completed|Error)"   ~/.hermes/state/maa/runs/$(date +%F)-am.log
```

再失败一次就**不要再换 roi 猜位置**——那是在没有观测手段的情况下瞎调。该做的是抓一张
卡住时的截图，确认红色确认条的实际位置与上游模板的匹配度，把结论补进地基事实 F。

### 允许改动的文件

- `config/maa-cli/resource/tasks/tasks.json`
- `deploy.py`（`--check` 的内容比对）
- `tests/test_maa.py`
- `DEV_GUIDE.md`（§7 部署链路 + §8 不变量 #4 的描述随之校准）

### 禁止触碰

- `config/maa-cli/tasks/daily.toml`、`infrast-smoke.toml`、`profiles/default.toml`、`cli.toml`
  —— 这一步只处理覆盖层与 `--check`
- Fight / 队列 / 抄作业任何一条链路
- `scripts/maa_daily.py`

### 验收边界

```bash
python3 -m unittest discover -s tests -v      # 全绿（交付时 192 项）

# 仓库与线上应完全一致
diff <(python3 -m json.tool config/maa-cli/resource/tasks/tasks.json) \
     <(python3 -m json.tool ~/.config/maa-cli/resource/tasks/tasks.json)
# 期望：无输出（顺带证明它仍是合法 JSON——所以不许在里面写注释）

python3 deploy.py --check
# 期望：{"ok": true, "missing": [], "mismatched": []}

# 反向漂移验证：手工改一个字节 → --check 必须报 mismatched 且失败 → 改回 → 干净
```

**关于 `python3 deploy.py` 的 `changed`**：不要预期它「不含 tasks.json」。`changed`
取决于**部署前线上的实际内容**，而线上可能已被别的步骤改过（22:05 就是这么发生的）。
判据用上面的 `--check`，不要用 `changed` 的成员。

真机回归（**必须做**，因为动的是正在工作的配置）：

```bash
python3 ~/.hermes/scripts/maa_action.py run now
```

期望：整场判绿；日志里 `InfrastDormDoubleConfirmed` 出现后**继续推进到
`Infrast Completed`**；截图不得停在「撤下宿舍锁定位置的干员」。

**失败即停**：如果真机回归红了，说明"以线上为准"这个前提有问题——
**不许改 `infrast-smoke.toml` 或调 roi 去迁就**，回报现象。

---

## 步骤 2 — 导航定义生成器（spike 先行，硬闸门）

### 前置

步骤 1 完成（覆盖层的仓库/线上已对齐——本步要往同一个覆盖层里加东西，
不对齐会把两个问题搅在一起）。

### 目标

让 MAA 能导航到活动里**上游没写导航定义**的普通关（AT-1/2/3/5 这类）。
**这是整条 clearing 的地基**——不通的话第 3 步没有存在意义。

### 做法

**分 2a / 2b 两段。2a 不过就不许写 2b。**

#### 2a spike（先做，一次性，产出结论不产出功能）

三问，逐条要 ✓/✗：

| Q | 问题 | 需要模拟器 |
|---|---|---|
| **Q1** | `~/.config/maa-cli/resource/tasks/Stages/<前缀>.json` 这条用户资源路径**会不会被 MaaCore 合并**？（目前只验证过 `tasks/tasks.json` 这一条路径能生效） | 否 |
| **Q2** | Fight 的接受判据到底是 `Stages/*.json` 的键，还是 `maa activity` / `StageActivityV2`？手写**一条**普通关的导航定义丢进覆盖层，看 `maa run` 是否还报 `Failed to add task` | 否（拒绝发生在连接设备之前） |
| **Q3** | 若接受，`Fight(stage=<该关>, times=0)` 能否**真的停在该关的编队界面** | 是 |

- 手写那条定义**照抄 `AT-4` 的三字段形状**（`algorithm` / `sub` / `next`），只换关卡名。
- 当前是活动空窗期，做 2a 时**当期活动可能已经不是 AT**。用当时真实的当期活动前缀，
  不要硬套 AT。
- **产出**：把三问结论就地追加进本文档的「2a 实测结论」小节（带日期），
  格式照抄 `PLAN_NAV_GATE.md` 的「结论 1/2/3」——写清楚**实测命令和实际输出**，
  不要只写结论。

**🚧 硬闸门**：Q1 或 Q2 为 ✗ → **停下来回报，不要写 2b**。
clearing 需要整体重新设计，那是另一次立项的事。

#### 2b 生成器（仅在 2a 三问全过时才做）

- 新增 `scripts/maa_nav_gen.py`：
  - 从 Tile-Pos 枚举当期活动的全部关卡 —— **复用 `maa_copilot.build_stage_index()` 与
    `_resource_dirs()`**，不要另写一套扫描
  - 按 `AT-4` 的三字段模板批量生成
  - 写到 `config/maa-cli/resource/tasks/Stages/<前缀>.json`
- **本步只做普通数字关**（AT-1/2/3/5 这类）。把握度分层如下，**EX / S / P / TR 一律不做**：

  | 关卡段 | 把握 | 理由 |
  |---|---|---|
  | AT-1/2/3/5 等普通数字关 | **高** | 与 AT-4 同构，同一列滑动可达 |
  | AT-EX-1..8 | 中 | `ocrReplace` 明确支持 EX 格式，但 EX 在地图上是否同列**未实测** |
  | AT-S-* / AT-P-* | 低 | 支线节点，可能不在滑动列表里 |
  | AT-TR-* | 低 | 教学节点，同上 |

  EX 留到**下一个活动上线当天**用真实数据实测，不在本步猜。
- **幂等**：上游已有的键**不覆盖**（`AT-8/7/6/4` 保持上游定义）。
- `deploy.py` 增加 `config/maa-cli/resource/tasks/Stages/` 目录的部署与 `--check` 覆盖。
- **顺带修文档**：`README.md` / `DEV_GUIDE.md` 里「关卡未解锁时抄作业会失败（导航找不到
  节点）」的说法要改成「**MAA 没为该关写导航定义**」。真正原因多半不是未解锁，
  这是一个长期误解，值得单独改掉。

### 允许改动的文件

- 新增 `scripts/maa_nav_gen.py`
- 新增 `config/maa-cli/resource/tasks/Stages/`（整个目录）
- `deploy.py`
- `tests/test_maa.py` 或新增测试文件
- `DEV_GUIDE.md`、`README.md`
- 本文档（追加「2a 实测结论」）

### 禁止触碰

- `config/maa-cli/resource/tasks/tasks.json` 里的 `InfrastDormDoubleConfirmButton`
  —— 那是步骤 1 的成果，本步只往 `Stages/` 子目录加东西
- `maa_copilot.build_stage_index()` 的既有行为（只读它，不改它）
- 任何执行链路（本步不跑战斗，只解决"能不能导航过去"）

### 验收边界

**2a**：三问各有一条**实测命令 + 实际输出**记录在本文档里。Q1/Q2 任一 ✗ 即停并回报。

**2b**：

```bash
python3 -m unittest discover -s tests -v      # 全绿

# 生成器覆盖当期活动全部普通数字关
python3 scripts/maa_nav_gen.py                 # 或其最终的调用形式
python3 -c "import json,glob; \
  [print(f, [k for k in json.load(open(f)) if '@' not in k]) \
   for f in glob.glob('config/maa-cli/resource/tasks/Stages/*.json')]"
# 期望：键集 ⊇ 当期活动全部普通数字关，且不含上游已有的键

python3 deploy.py && python3 deploy.py --check  # 干净
```

真机：

- `maa run` 对新增关**不再报 `Failed to add task`**
- **至少一关真机停在编队界面**（这一条才是真验收，前面都是纸面）

**失败即停**：生成的定义导致原本能导航的关（AT-8/7/6/4）也坏了 → 立即回滚覆盖层并回报。

---

## 步骤 3 — `stage_queue` + 日常让路 + `needs_manual` 出口

### 前置

步骤 2 完成且 2a 三问全过。**2a 没过就不要开始这一步。**
步骤 0 完成（EX 闸门依赖森空岛数据，401 期间验不了）。

### 目标

活动上线后自动按顺序把关卡打通，中间任何一关卡住都能绕过去而不是卡死整个队列；
队列活跃期间日常把理智让出来，但保留剿灭。

### 做法

**架构（用户已拍板）**：新增 `scripts/maa_clearing.py` 领域层（近纯函数，对齐
`maa_copilot.py` 的角色）+ **复用 `maa_copilot_worker.py`** 真正开模拟器执行。
**不新建第二个 worker，不并进 `maa_daily.py`。**

**队列形态**：

```text
[教学关]                            ← 若确实需要，直接走 needs_manual
AT-1 → AT-2 → … → AT-8              普通关，无突袭
─── 等 EX zone 开放（见下）───
AT-EX-1 普通 → AT-EX-1 突袭
AT-EX-2 普通 → AT-EX-2 突袭 → …     突袭紧跟对应普通关
```

**EX 开放闸门**（单向规则，比日期表可靠，且是本账号视角的真值）：

- 只要 `roster["activity_progress"]` 里**还存在 `totalStage == 0` 的 zone**
  → 活动尚未全开 → **EX / S 段不入队**
- **全部** zone 都 `> 0` → 全开 → EX 段入队
- 依据：四个活动的缓存比对中，唯一出现 `totalStage == 0` 的就是当时正在进行、
  尚未全开的那个活动；且 act51side 的 zone2 `total = 8` 恰好等于它的 EX 关数。
- **⚠️ 这条推论未实地验证。** 当期复刻活动已于 2026-09-01 03:59:59 结束，来不及验，
  靶子是**下一个活动**。写代码时按此实现，但要在注释里标明"未验证"，
  并在下个活动上线当天核对一次。
- **森空岛不可用时（401 / 无数据）必须 fail-closed：不入队 EX**，不能当成"全开"。
- 与既有 `maa_copilot.zone_progress_warning()` 的关系：那个是**只在能证明未解锁时才开口**
  的单向启发（沉默 ≠ 已解锁），本闸门是**另一条独立的单向规则**。
  **不要合并、不要互相依赖**——合并会把"沉默不代表已解锁"这条性质弄丢。

**`needs_manual` 出口**（队列永不卡死的关键）：

- 触发：某一关**连续 N 次拿不到作业**，或 **nav 失败**
- 行为：发一张卡片「某关需要你手动打一次，打完点『已完成』」→ 用户点了 cursor++ 继续
- **这不是为教学关特设的补丁**——任何一关卡住都走它，队列就永远卡不死。
  教学关只是最可能触发它的那个（既大概率没有导航定义，又大概率没有作业）。
- nav 失败还要**冻结该段、每天重试一次**，不要每轮都撞。

**日常让路**：

- `maa_daily.render_daily_toml()` 现在用**一个** `tasks.fight` 开关同时控制「每日剿灭」
  和「刷理智」两个 Fight 块 → **拆成两个独立开关**
- 队列活跃时**只摘「刷理智」，保留「每日剿灭」**
  （周上限 1800 合成玉、代理卡驱动，摘掉纯亏）
- **剿灭同样消耗理智**（地基事实 H）——队列排关前的理智预算**必须先扣掉剿灭那份**，
  否则会把可用理智算多。**这条单列一个验收项和一个单测。**
- 基建 / 公招 / 信用 / 领奖**照常**

**状态**：`state/maa/clearing.json`，0600，走 `atomic_write_json`。

### 允许改动的文件

- 新增 `scripts/maa_clearing.py`
- `scripts/maa_daily.py`（`render_daily_toml` 的开关拆分 + `execute` 的让路分支）
- `scripts/maa_copilot_worker.py`（队列驱动的执行入口）
- `scripts/maa_action.py`（新子命令，必须进 `dispatch()` 白名单）
- `scripts/maa_plan_card.py`
- `deploy.py`、`tests/`
- `skills/games/maa-operator/SKILL.md`、`DEV_GUIDE.md`

### 禁止触碰

- `stone` / `medicine` 不变量（#1、#2）
- `run.lock` 与 `state.lock` 的既有语义（不变量 #13）
- `hermes:btn:maa:` 与 `hermes:btn:maa-copilot:` 两个前缀的清扫边界（不变量 #7、#14）
- `zone_progress_warning()` 的既有行为
- **不许新增静默跳过**（护栏 4）

### 验收边界

```bash
python3 -m unittest discover -s tests -v      # 全绿
```

必须有的单测：

- 队列活跃时渲染出的 `daily.toml`：**有剿灭、无刷理智**（逐字断言）
- **理智预算扣除剿灭**（独立用例，这条最容易算漏）
- EX 闸门：存在 `totalStage == 0` 的 zone → 不入队 EX
- EX 闸门：森空岛不可用 → **不入队 EX**（fail-closed，不能当成全开）
- `needs_manual` 触发后 cursor **不前进**，用户点「已完成」后才前进
- nav 失败 → 该段冻结，同日不重复重试

真机：

- 队列**连续推进 ≥2 关**
- `needs_manual` 卡片能发出并被点掉，点掉后队列继续
- 让路当天的日常整场判绿，基建/公招/信用/领奖不受影响

**失败即停**：如果为了让队列跑通而动了 `stone`/`medicine`，立即回退。

---

## 步骤 4 — EX 理智 OCR 实测

### 前置

步骤 3 完成（队列存在才有排理智预算的需求）。也可在步骤 3 后期并行。

### 目标

解掉「`stages.json` 没有 EX 关条目 → 队列排不出 EX 段理智预算」这个洞。

### 做法

- **问题**：`maa_copilot.stage_sanity_cost()` 的数据源是 `stages.json`，
  该文件**没有 EX 关条目**，所以对 EX 返回 `None`。
- **方案**：在**编队界面**用 OCR 读理智真值。
  复用已部署的 `~/.hermes/scripts/ocrbox`（本机 Vision OCR）与
  `maa_daily.capture_screenshot()`，用法参照 `maa_daily.capture_home_screenshot()` /
  `verify_home_screenshot()` 那一段的既有范式。
- **先校准再用**：先对一关 `stages.json` **有**真值的普通关跑一遍，
  证明 OCR 读出来的数就是对的，再拿去读 EX。
- **读不出时必须返回 `None`，不许猜一个数** —— 沿用 `maa_copilot._one_cost()`
  那种"歧义就 fail-closed"的姿态。理智算错会直接导致队列空跑。

### 允许改动的文件

- `scripts/maa_clearing.py` 或 `scripts/maa_copilot.py`（理智解析那一段）
- `scripts/ocrbox.swift`（**仅在必要时**）
- `tests/`
- `DEV_GUIDE.md`

### 禁止触碰

- `stage_sanity_cost()` 对**普通关**的既有行为（`stages.json` 有真值时以它为准，
  OCR 只是 EX 的补充路径）
- 不变量 #5b：日常战报的截图必须 OCR 确认在主页——本步是另一套 OCR 用途，
  **不要复用或改动那道闸门**

### 验收边界

- 对一关 `stages.json` **有**真值的普通关：OCR 读出的值 **== `stages.json` 的值**
  （校准，先证明方法本身对）
- 对一关 EX：能读出**非 `None`** 的值
- 人为喂一张读不出的图：**返回 `None`**，不是 0、不是猜测值
- `python3 -m unittest discover -s tests -v` 全绿

**失败即停**：校准那一条不过（普通关都读不准）→ 说明 OCR 路径本身不可靠，
**不要拿去读 EX**，回报并考虑别的数据源。

---

## 步骤 5 — Depot 对表 + 掉落增量 + 消耗率

### 前置

无强依赖，但排在队列之后做更有意义（队列跑起来才有稳定的掉落流）。

### 目标

知道自己仓库里**有什么、缺什么、消耗多快**，为第 6 步的选关提供真实数据。

### 做法

**一次真值 + 本地增量**：

- **每周一次全量 Depot**（MaaCore 的 Depot 任务）→ 落盘 `state/maa/depot.json`
- **期间用战报解析出的掉落做增量** —— `maa_daily._drop_pairs()` **已经在输出**
  「掉落 固源岩组 ×30」这种结构，直接接上，不要另写解析
- **为什么必须定期对表**：手动升干员会消耗材料，本地估算只会**越估越高**，必然漂移

**对表顺带产出更值钱的东西**：

```text
本周真实消耗 = 上次库存 + 这期间刷到的 − 这次库存
```

跑几周就有了**本账号的真实消耗率**，比任何手工权重表都准。这正是第 6 步要用的输入。

- 触发点接在 `maa_plan_card.py` 的每周节奏上（该脚本已经在跑每周 `maa update`）。
- **对表失败不得污染已有库存快照** —— 宁可这周没数据，也不要写进一份错的。

### 允许改动的文件

- 新增 `scripts/maa_depot.py`
- 新增 `config/maa-cli/tasks/depot.toml`
- `scripts/maa_daily.py`（战报里回写掉落到累计增量）
- `scripts/maa_plan_card.py`（每周触发）
- `deploy.py`、`tests/`
- `DEV_GUIDE.md`（§6 状态模型要加 `depot.json`）

### 禁止触碰

- `extract_summary()` 输出给 Discord 的**文案**（不变量 #9：原始 CLI 文本不进 Discord）
  —— 只取它已经解析好的结构，不改它说什么
- `daily.toml` 的任务链顺序
- 不变量 #1/#2/#3

### 验收边界

```bash
python3 -m unittest discover -s tests -v      # 全绿
python3 deploy.py && python3 deploy.py --check
```

真机：

- 跑一次 Depot 拿到**非空**库存快照
- **连续两次对表能算出消耗率**（这一条需要跨周，做完先记 TODO，下周复核）
- 掉落增量与战报里的数字**一致**（抽查一场，逐项对）
- 人为让对表失败一次 → 已有快照**未被污染**

---

## 步骤 6 — 计划卡加 farming 选关下拉

### 前置

步骤 5 完成（没有消耗率数据这一步只能跑降级模式）。

### 目标

让"今天刷哪个关"由真实材料缺口决定，并且这个决定能在计划卡上一键选中。

### 做法

**排序指标只用一个**：

```text
可用天数 = 当前库存 / 该材料日均消耗    →    刷「可用天数」最小的那个
```

- 「库存特别少」天然就是「可用天数特别小」，会自动排到最前，
  **不需要额外的例外规则**。不要再叠一层"库存低于 N 就优先"。
- **冷启动**（还没有历史消耗数据时）：先用一组 `config.json` 里可调的**手工权重**顶上，
  攒够数据后自动切换到实测消耗率。
  **切换条件要写死**（例如"有 ≥2 次成功对表"），不要留成"看情况"。
  降级期间**卡片上要明说是降级**——用户要能分辨"这是实测推荐"还是"这是猜的"。
- **做法**：在 `maa_plan_card` 现有两个下拉之外新增**第三个**，
  范式照抄 `_stage_select()` / `_copilot_stage_select()`。

### 允许改动的文件

- `scripts/maa_plan_card.py`
- `scripts/maa_action.py`
- `tests/`
- `DEV_GUIDE.md`、`README.md`

### 禁止触碰

- 既有两个下拉的 `custom_id` 与按钮前缀
- `select_args` 查表机制（不变量 #8）
- 每早 `hermes:btn:maa:` 前缀清扫的边界（不变量 #7、#14）

### 验收边界

```bash
python3 -m unittest discover -s tests -v      # 全绿
```

必须有的断言：

- 新下拉的值**经 `select_args` 查表**，不直通 argv（不变量 #8）
- 07:50 前缀清扫后新按钮**仍在**（不变量 #7、#14）
- 无消耗数据时**降级到手工权重**，且卡片文案**明说是降级**
- 排序确实按"可用天数"升序

真机：次日 07:50 的计划卡上三个下拉都在，选中后 `plan.json` 正确更新。

---

## 全局验收

七步全部做完之后：

```bash
python3 -m unittest discover -s tests -v       # 全绿
python3 deploy.py && python3 deploy.py --check # 干净

# 不变量抽查
grep -rn "use-sanity-potion" scripts/          # 期望：无输出
grep -rn "stone = 0" scripts/ config/          # 期望：daily 渲染与 toml 里都在

# 按钮登记表三个前缀都健在
python3 -c "import json,os; d=json.load(open(os.path.expanduser( \
  '~/.hermes/state/agent-book/buttons.json'))); \
  print({p: sum(1 for k in d if k.startswith(p)) for p in \
  ('hermes:btn:agent-book:','hermes:btn:maa:','hermes:btn:maa-copilot:')})"
```

端到端：**下一个活动上线当天**，队列自动从第一关推进到 EX 段，
日常在此期间只让出刷理智、保留剿灭，计划卡按可用天数推荐关卡。

---

## 不做的事

- **不做 EX / S / P / TR 的导航生成**（步骤 2 只做普通数字关）。等下个活动上线实测，
  尤其是"EX 在地图上是否与普通关同列"这个未知。
- **不把 `PLAN_NAV_GATE.md` A-1 的导航闸门顺手做了** —— 它的判据要等步骤 2 spike 的结论。
  做完步骤 2 再回头立项。
- **不合并 `zone_progress_warning()` 与 EX 开放闸门** —— 两条独立的单向规则，
  合并会把"沉默不代表已解锁"这条性质弄丢。
- **不建第二个 worker** —— 执行统一复用 `maa_copilot_worker.py`。
- **不给森空岛做自动续期** —— 鹰角 token 本身过期时无解，且绝不能循环重试。
  出路是用户在本机终端重跑 `link`。
- **不在"库存特别少"上叠额外的优先级规则** —— "可用天数"这一个指标已经覆盖它。
