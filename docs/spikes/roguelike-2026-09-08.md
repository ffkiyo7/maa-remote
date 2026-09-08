# maa-remote 肉鸽（Roguelike）只读调研

环境：maa-cli 0.7.5（`/opt/homebrew/bin/maa`）+ MaaCore v6.17.2（`~/Library/Application Support/com.loong.maa`，`asst.log` 自报 `Version v6.17.2 / Built at Sep 5 2026`）。
本次调研未启动模拟器、未连接设备；只跑了 `--help` 与 `--dry-run`。

出处约定：
- `[help]` = 本机 `MAA_CONFIG_DIR=~/.config/maa-cli maa roguelike --help` 输出
- `[cli:<file>:<line>]` = maa-cli `main` 分支源码（raw.githubusercontent，`crates/maa-cli/...`）
- `[core:<file>:<line>]` = MaaAssistantArknights tag **v6.17.2** 源码
- `[res:<file>]` = 本机 MaaCore 资源 `~/Library/Application Support/com.loong.maa/resource/...`
- `[doc:integration]` = `docs/zh-cn/protocol/integration.md` @ v6.17.2（对应线上 https://docs.maa.plus/zh-cn/protocol/integration.html）
- `[doc:manual]` = `docs/zh-cn/manual/introduction/integrated-strategy.md` @ v6.17.2

---

## 1. `maa roguelike --help` 完整选项清单

`Usage: maa roguelike [OPTIONS] <THEME>`

### 1.1 位置参数

| 参数 | 取值 | 说明 |
| - | - | - |
| `<THEME>` | `Phantom` / `Mizuki` / `Sami` / `Sarkaz` / `JieGarden` | 必填。**注意：没有 `BlackFlow`** `[help]`；`Theme` enum 只有 5 个变体 `[cli:run/preset/roguelike.rs:8-14]`，而 MaaCore v6.17.2 已支持 `BlackFlow`（黑流树海）`[doc:integration]`、本机资源里也有 `resource/roguelike/BlackFlow/`、`resource/tasks/Roguelike/BlackFlow.json` `[res]`。→ 见第 6 节「BlackFlow 缺口」。 |

### 1.2 任务选项（全部，未截断）

| 选项 | 类型 | maa-cli 默认 | 传给 MaaCore 的字段 | 说明 / 限制 |
| - | - | - | - | - |
| `--mode <MODE>` | i32 | `0` | `mode` | `0` 刷分；`1` 刷源石锭；`2` 已废弃；`3` 未实现；`4` 三层后重开（凹开局/烧水）；`5` 刷坍缩范式（仅 Sami，须配 `-P`）`[help]`。**maa-cli 硬校验 `0..=5`，超出直接 `bail!("Mode must be in range between 0 and 5")`**；`mode 5` 且非 Sami 也 bail `[cli:roguelike.rs:155-161]`。所以 MaaCore 的 `6/7/10001/20001/30001` 用 CLI 预设**不可达**。 |
| `--squad <SQUAD>` | String | 不传（MaaCore 默认 `指挥分队`） | `squad` | 中文分队名，OCR 匹配。合法值见 2.3。 |
| `--core-char <CORE_CHAR>` | String | 不传 | `core_char` | 单个干员**中文名**，留空按练度自动选 `[doc:integration]`。 |
| `--roles <ROLES>` | String | 不传（MaaCore 默认 `取长补短`） | `roles` | ⚠️ `--help` 里写 `"先手必胜" (default)`，**这条注释是错的**：maa-cli 不传该字段 `[cli:roguelike.rs:166]`，MaaCore 默认是 `取长补短` `[doc:integration]`。 |
| `--starts-count <N>`（别名 `--start-count`） | i32 | 不传 = 不停 | `starts_count` | 探索 N 次后停止。**这是唯一的「跑满 N 局就干净收手」开关**，见第 3 节。 |
| `--difficulty <D>` | i32 | 不传 | `difficulty` | **Phantom 主题下 maa-cli 直接丢弃并 warn**（`Difficulty is not valid for Phantom theme, ignored`）`[cli:roguelike.rs:174-180]`。其他主题原样透传。取值范围见 2.2。 |
| `--disable-investment` | flag | false | `investment_enabled=false` | 置位时**只发** `investment_enabled=false`，`investments_count` / `investment_with_more_score` / `stop_when_investment_full` 全部不发 `[cli:roguelike.rs:183-192]`。 |
| `--investment-with-more-score` | flag | false | `investment_with_more_score` | 投资后尝试购物，仅 mode 1 有效 `[doc:integration]`。 |
| `--investments-count <N>` | i32 | 不传 | `investments_count` | 投资达到 N 次后停止。 |
| `--no-stop-when-investment-full` | flag | false | `stop_when_investment_full = !flag` | ⚠️ **maa-cli 把默认值反了**：不加这个 flag 时发的是 `stop_when_investment_full: true`（MaaCore 协议默认是 `false`）`[cli:roguelike.rs:190]`，`--dry-run` 已实测确认（见 1.3）。 |
| `--use-support` | flag | false | `use_support` | 只有置位时才发 `use_support`/`use_nonfriend_support` `[cli:roguelike.rs:195-200]`。 |
| `--use-nonfriend-support` | flag | false | `use_nonfriend_support` | 仅在 `--use-support` 同时给出时才会被发送。 |
| `--start-with-elite-two` | flag | false | `start_with_elite_two` | 仅 mode 4 有效 `[doc:integration]`。 |
| `--only-start-with-elite-two` | flag | false | `only_start_with_elite_two` | 仅在 `--start-with-elite-two` 同时给出时才发送 `[cli:roguelike.rs:203-208]`。 |
| `--stop-at-final-boss` | flag | false | `stop_at_final_boss`（**总是发送**） | 第 5 层险路恶敌前停；仅非 Phantom 主题 `[doc:integration]`、`[core:RoguelikeTask.cpp:254-261]`。 |
| `--refresh-trader-with-dice` | flag | false | `refresh_trader_with_dice` | 仅 Mizuki；**Mizuki 主题下该字段总会被发送（false 也发）** `[cli:roguelike.rs:212-217]`。 |
| `--use-foldartal` | flag | false | `use_foldartal` | 仅 Sami；**Sami 主题下总会被发送** `[cli:roguelike.rs:219]`。注意 MaaCore 非 mode 5 的默认是 `true`，maa-cli 不加 flag 会强制发 `false` `[doc:integration]`。 |
| `-F, --start-foldartals <..>` | Vec\<String\> | `[]` | `start_foldartal_list` | 非空才发；仅 Sami + mode 4，且必须配「生活至上分队」`[doc:integration]`。 |
| `-P, --expected-collapsal-paradigms <..>` | Vec\<String\> | `[]` | `expected_collapsal_paradigms` | mode 5 下**必填**，为空则 bail `[cli:roguelike.rs:224-236]`；同时自动带上 `check_collapsal_paradigms=true`、`double_check_collapsal_paradigms=true`。 |
| `--start-with-seed` | flag（bool） | false | `start_with_seed` | ⚠️ maa-cli 发的是 **bool**，MaaCore 协议里 `start_with_seed` 是 **string（具体种子）** `[doc:integration]`。仅在 `theme=Sarkaz && mode==1` 时发送 `[cli:roguelike.rs:238-240]`。类型不匹配，实机行为待验证。 |

### 1.3 通用选项（同其他子命令）

`-a/--addr <ADDR>`、`-p/--profile <PROFILE>`、`--user-resource`、`--batch`、`--dry-run`、`--no-summary`、`-v/--verbose`、`-q/--quiet`、`--no-auto-reconnect`、`--log-file[=<PATH>]`、`-h/--help` `[help]`。

**实测 `--dry-run -vv`**（`maa roguelike Sarkaz --mode 1 --difficulty 15 --squad 点刺成锭分队 --starts-count 1 --dry-run -vv`）生成的参数：

```json
{
  "theme": "Sarkaz", "mode": 1, "squad": "点刺成锭分队", "starts_count": 1,
  "stop_at_final_boss": false, "difficulty": 15,
  "investment_enabled": true, "investment_with_more_score": false,
  "stop_when_investment_full": true, "start_with_seed": false
}
```

---

## 2. MaaCore `Roguelike` 任务参数表（v6.17.2）

### 2.1 完整字段表（引自 `[doc:integration]`）

| 字段 | 类型 | 默认 | 说明（原文） |
| - | - | - | - |
| `enable` | boolean | `true` | 是否启用本任务。 |
| `theme` | string | `Phantom` | `Phantom` 傀影与猩红血钻 / `Mizuki` 水月与深蓝之树 / `Sami` 探索者的银凇止境 / `Sarkaz` 萨卡兹的无终奇语 / `JieGarden` 岁的界园志异 / `BlackFlow` 黑流树海 |
| `mode` | number | `0` | `0` 刷分/奖励点数，尽可能稳定地打更多层数；`1` 刷源石锭，第一层投资完就退出；`2` **已移除**，传入被拒绝；`3` **未开放**，传入被拒绝；`4` 凹开局（先 0 难度到第三层重开，再到指定难度凹开局奖励，非热水壶/希望则回 0 难度重来；Phantom 下不切难度）；`5` 刷坍缩范式，仅 Sami，配 `expected_collapsal_paradigms`；`6` 刷月度小队蚊子腿；`7` 刷深入调查蚊子腿；`10001` 快速通过第一层，仅 Sarkaz；`20001` 刷常乐节点，仅 JieGarden，需配 `find_playTime_target`；`30001` 刷襁褓动物，仅 BlackFlow |
| `squad` | string | `指挥分队` | 开局分队名。 |
| `roles` | string | `取长补短` | 开局职业组。 |
| `core_char` | string | （空） | 开局干员名。仅支持单个干员**中文名**，无论区服；留空或 `""` 则根据练度自动选择。 |
| `use_support` | boolean | `false` | 开局干员是否为助战干员。 |
| `use_nonfriend_support` | boolean | `false` | 是否可以是非好友助战；仅 `use_support=true` 时有效。 |
| `starts_count` | number | `2147483647` | 开始探索的次数。达到后自动停止任务。 |
| `difficulty` | number | `-1` | 指定难度等级，`-1` 表示不指定。**若指定难度未解锁，则会选择当前已解锁的最高难度。** |
| `stop_at_final_boss` | boolean | `false` | 是否在第 5 层险路恶敌节点前停止任务。仅适用于除 Phantom 以外的主题。 |
| `stop_at_max_level` | boolean | `false` | 是否在肉鸽等级刷满后停止任务。**maa-cli 无对应选项**。 |
| `investment_enabled` | boolean | `true` | 是否投资源石锭。 |
| `investments_count` | number | `2147483647` | 投资源石锭的次数。达到后自动停止任务。 |
| `stop_when_investment_full` | boolean | `false` | 是否在投资到达上限后自动停止任务。 |
| `investment_with_more_score` | boolean | `false` | 是否在投资后尝试购物。仅适用于模式 1。 |
| `start_with_elite_two` | boolean | `false` | 凹开局同时凹干员精二直升。仅模式 4。 |
| `only_start_with_elite_two` | boolean | `false` | 只凹精二直升而忽视其他开局条件。仅模式 4 且前者为 true。 |
| `refresh_trader_with_dice` | boolean | `false` | 用骰子刷新商店购买特殊商品。仅 Mizuki，用于刷指路鳞。 |
| `first_floor_foldartal` | string | （空） | 希望第一层远见阶段得到的密文板。仅 Sami，不限模式；凹到则停止任务。**maa-cli 无对应选项**。 |
| `start_foldartal_list` | array\<string\> | `[]` | 凹开局时希望在开局奖励阶段得到的密文板。仅 Sami + 模式 4，须与「生活至上分队」同时使用。 |
| `collectible_mode_start_list` | object | 全 false | 凹开局期望的奖励，仅模式 4：`hot_water` 热水壶 / `shield` 护盾 / `ingot` 源石锭 / `hope` 希望（JieGarden 无）/ `random` 随机 / `key` 钥匙（仅 Mizuki）/ `dice` 骰子（仅 Mizuki）/ `ideas` 2 构想（仅 Sarkaz）/ `ticket` 票券（仅 JieGarden）。**maa-cli 无对应选项**。 |
| `use_foldartal` | boolean | mode 5 下 `false`，其他 `true` | 是否使用密文板。仅 Sami。 |
| `check_collapsal_paradigms` | boolean | mode 5 下 `true`，其他 `false` | 是否检测获取的坍缩范式。 |
| `double_check_collapsal_paradigms` | boolean | `true`（mode 5 下 true，其他 false） | 坍缩范式检测防漏措施。仅 Sami 且 `check_collapsal_paradigms=true`。 |
| `expected_collapsal_paradigms` | array\<string\> | `['目空一些','睁眼瞎','图像损坏','一抹黑']` | 希望触发的坍缩范式。仅 Sami + 模式 5。 |
| `monthly_squad_auto_iterate` | boolean | — | 是否启动月度小队自动切换。**maa-cli 无对应选项**。 |
| `monthly_squad_check_comms` | boolean | — | 是否将月度小队通信也作为切换依据。**maa-cli 无**。 |
| `deep_exploration_auto_iterate` | boolean | — | 是否启动深入调查自动切换。**maa-cli 无**。 |
| `collectible_mode_shopping` | boolean | `false` | 烧水是否启用购物。**maa-cli 无**。 |
| `collectible_mode_squad` | string | 同 `squad` | 烧水时使用的分队；`squad` 为空且未指定时为「指挥分队」。**maa-cli 无**。 |
| `start_with_seed` | **string** | （空） | 使用种子刷钱时填入固定种子，留空则不启用。仅 Sarkaz 主题、Investment 模式、「点刺成锭分队」或「后勤分队」时生效。 |
| `blackflow_strategy` | string | 按 `mode`+`investment_enabled` 推断 | `baby_animal` / `investment` / `burn_with_investment` / `burn`。**maa-cli 无**。 |
| `blackflow_cultivation_target` | string | `swaddled_cat` | `swaddled_cat` \| `swaddled_feathered_serpent` \| `swaddled_dog` \| `swaddled_cerberus`；仅 `blackflow_strategy=baby_animal`。**maa-cli 无**。 |
| `find_playTime_target` | number | — | `1` 令（掷地有声）/ `2` 黍（种因得果）/ `3` 年（三缺一）。仅 JieGarden + 模式 20001，该模式下**必填**。**maa-cli 无**。 |

### 2.2 `difficulty` 的合法上限（各主题）

来自 MAA GUI 的 `GetMaxDifficultyForTheme` `[core:src/MaaWpfGui/ViewModels/UserControl/TaskQueue/RoguelikeSettingsUserControlModel.cs:96-104]`：

| 主题 | 最大难度 | 备注 |
| - | - | - |
| Phantom 傀影 | 15 | ⚠️ maa-cli 预设**不允许**给 Phantom 传 difficulty（会 warn 后丢弃）`[cli:roguelike.rs:174-177]` |
| Mizuki 水月 | 18 | |
| Sami 萨米 | 15 | |
| Sarkaz 萨卡兹 | 18 | |
| JieGarden 界园 | 18 | |
| BlackFlow 黑流树海 | 15 | maa-cli 预设不支持该主题 |

GUI 的下拉是 `-1`（不指定）、`int.MaxValue`（显示 `MAX (n)`）、然后 `maxThemeDifficulty..0` 倒序 `[同上:75-90]`。

### 2.3 `squad` / `roles` / `core_char` 的合法取值来源

**这些值是 OCR 字符串，MaaCore 资源里没有白名单**（`resource/tasks/Roguelike/*.json` 里只有识别任务，没有分队清单 `[res]`）。权威清单是 MAA GUI 的下拉字典 `_squadDictionary` + `_commonSquads` `[core:RoguelikeSettingsUserControlModel.cs:172-262]`：

| 主题 | 主题专属分队 |
| - | - |
| Phantom | 集群分队、矛头分队、研究分队 |
| Mizuki | 集群分队、矛头分队、心胜于物分队、物尽其用分队、以人为本分队、研究分队 |
| Sami | 集群分队、矛头分队、永恒狩猎分队、生活至上分队、科学主义分队、特训分队 |
| Sarkaz（mode=1 时的子集 `Sarkaz_1`） | 集群分队、矛头分队、博闻广记分队、蓝图测绘分队、点刺成锭分队、拟态学者分队 |
| Sarkaz（默认） | 集群分队、矛头分队、魂灵护送分队、博闻广记分队、蓝图测绘分队、因地制宜分队、异想天开分队、点刺成锭分队、拟态学者分队、专业人士分队 |
| JieGarden | 特勤分队、高台突破分队、地面突破分队、游客分队、司岁台分队、天师府分队、花团锦簇分队、棋行险着分队、岁影回音分队、代理人分队、知学分队、商贾分队 |
| BlackFlow | 特勤分队、矛头分队、高台突破分队、地面突破分队、本源研修分队、文明开化分队、开拓者分队、多边贸易分队、地质调查分队 |

通用分队（每个主题都追加）：**指挥分队、后勤分队、突击战术分队、堡垒战术分队、远程战术分队、破坏战术分队**；此外 Phantom/Mizuki/Sami/Sarkaz/JieGarden 还追加 **高规格分队**（BlackFlow 不加）`[同上:249-283]`。

`roles` 合法值 `[同上:148-170]`：**先手必胜、稳扎稳打、取长补短**；JieGarden / BlackFlow 额外有 **灵活部署、坚不可摧**；所有主题最后再加 **随心所欲**。GUI 默认 `稳扎稳打`。

`core_char`：任意单个干员中文名，无白名单，留空自动选 `[doc:integration]`。

### 2.4 maa-cli 选项 ↔ MaaCore 参数对应关系小结

一一对应见 1.2 表的第 4 列。**maa-cli 覆盖不到的 MaaCore 字段**（共 12 个）：`enable`、`stop_at_max_level`、`first_floor_foldartal`、`collectible_mode_start_list`、`collectible_mode_shopping`、`collectible_mode_squad`、`monthly_squad_auto_iterate`、`monthly_squad_check_comms`、`deep_exploration_auto_iterate`、`blackflow_strategy`、`blackflow_cultivation_target`、`find_playTime_target`；外加 `theme=BlackFlow` 与 `mode ∈ {6,7,10001,20001,30001}`。

**绕过办法**：写一个 maa-cli 任务文件（TOML），用 `maa run <task>` 跑。任务文件的 `type` + `[tasks.params]` 是自由 JSON/TOML，原样透传给 MaaCore——本项目 `~/.config/maa-cli/tasks/daily.toml` 就是这么写的 `[res]`。

---

## 3. 优雅停止

### 3.1 (a) maa-cli 收到 SIGINT / SIGTERM 做什么

```rust
// crates/maa-cli/src/run/mod.rs:147-154
let stop_bool = Arc::new(AtomicBool::new(false));
for sig in TERM_SIGNALS {
    signal_hook::flag::register_conditional_default(*sig, Arc::clone(&stop_bool))?;
    signal_hook::flag::register(*sig, Arc::clone(&stop_bool))?;
}
```

```rust
// crates/maa-cli/src/run/mod.rs:226-237
while asst.running() {
    if stop_bool.load(atomic::Ordering::Relaxed) {
        bail!("Interrupted by user!");          // ← 直接 bail，跳过下面的 asst.stop()
    }
    if offline_stop.load(atomic::Ordering::Relaxed) { break; }
    std::thread::sleep(Duration::from_millis(100));
}
debug!("Stopping MAA...");
asst.stop()?;                                    // ← 正常路径才会走到
```

结论：
- **第一次** SIGINT/SIGTERM/SIGQUIT → 置 flag → 主循环 ≤100 ms 内 `bail!("Interrupted by user!")`。**`asst.stop()` 不会被调用**，`Assistant` 直接 drop → `AsstDestroy` `[cli:crates/maa-core/src/lib.rs:44-49]`。
- **第二次**同类信号 → 因为 `register_conditional_default` 的 flag 已为 true，**内核默认动作生效，进程当场被杀**。此时既不 destroy 也不清理，模拟器会被留在原地。
- 正常跑完后 `stop_bool` 被置 true `[cli:run/mod.rs:247]`，之后任何信号都直接杀进程。
- 注意 `bail!` 之后 `close_app` / 外部 app 关闭那段也被跳过 `[cli:run/mod.rs:240-243]`——本项目用自己的 `close_everything()` 兜底，问题不大。

### 3.2 (a续) `AsstStop` 是立即中止还是等本局结束

**立即中止。**

```cpp
// src/MaaCore/Assistant.cpp:526-541
bool Assistant::stop(bool block) {
    m_thread_idle = true;
    std::unique_lock<std::mutex> lock;
    if (block) lock = std::unique_lock<std::mutex>(m_mutex);
    m_tasks_list.clear();
    clear_cache();
    return true;
}
```

`working_proc` 在真正 `task_ptr->run()` 之前就 `lock.unlock()` 了（注释：*only one instance of working_proc running, unlock here to allow set_task_param to the running task*）`[core:Assistant.cpp:571-578]`，所以 `stop(block=true)` 不会等任务跑完就返回。正在跑的任务通过 `need_exit()`（读 `m_thread_idle`）在**下一个检查点**退栈，随后发 `AsstMsg::TaskChainStopped` `[core:Assistant.cpp:665]`。

→ **停在哪就是哪**：可能在战斗中、在地图节点上、在商店里。

### 3.3 (b) 上游能否自动处理「上一局未结束」

**能，同主题下可以。** MaaCore 的肉鸽任务链入口 `<Theme>@Roguelike@Begin` 的 `next` 列表里就带着各种「善后」分支 `[res:resource/tasks/Roguelike/base.json]`：

```json
"Roguelike@Begin": {"algorithm":"JustReturn","next":[
  "Roguelike@ChooseDifficulty","Roguelike@StartExplore","Roguelike@Stages#next",
  "Roguelike@GetDrops#next","Roguelike@NextLevel","Roguelike@NextLevel#next",
  "Roguelike@ExitThenAbandon","Roguelike@Abandon","Roguelike@TodoEnter",
  "Roguelike@Continue","Roguelike@StartExploreCD",
  "(Roguelike@IntegratedStrategies)@QuickSwitch@ToTerminal#next",
  "Roguelike@ReturnButtons#next","Roguelike@ReturnConfirm","Roguelike@StartUpThemes#next"]}
```

处理链：
- `Roguelike@Continue`（点「继续探索」）→ `next: ["Roguelike@MissionFailedFlag2","Roguelike@ExitThenAbandon"]`
- `Roguelike@ExitThenAbandon`（左上角退出）→ `Roguelike@Abandon` → `Roguelike@AbandonConfirm` → `next: ["Roguelike@MissionFailedFlag2","Roguelike@StartExplore","Roguelike@StartExploreCD"]`
- 卡在战斗里也有：`RoguelikeBattleExitBegin` → `RoguelikeBattleAbandon` → `RoguelikeBattleAbandonConfirm`
- 手册也明说：**「如果任务出现问题，会自动放弃当次探索并重试」** `[doc:manual]`

**⚠️ 跨主题不行**：手册明确要求「请提前手动结束**非目标主题**的探索，确保目标主题当前可开始探索」`[doc:manual]`。所以**中断后的下一次派发必须是同一个主题**，否则会卡（社区 issue #13245 就是主题识别错进错主题导致死循环）。

### 3.4 上游自带的两个「优雅停」原语

这两个是我们能利用的关键：

**① `starts_count` 用尽 → 干净停在主题入口页**

```cpp
// src/MaaCore/Task/Interface/RoguelikeTask.cpp:280
m_roguelike_task_ptr->set_times_limit(theme + "@Roguelike@StartExplore",
                                      params.get("starts_count", INT_MAX));
```

而 `Roguelike@StartExplore` 的 `exceededNext` 是 `["RoguelikeControlTaskPlugin-Stop"]` `[res:base.json]`，该插件把 `m_need_exit_then_stop=false`、直接 `m_task_ptr->set_enable(false)` `[core:RoguelikeControlTaskPlugin.cpp:29-32, 47-54]`。
→ 触发点是**准备开始第 N+1 局的那一刻**，也就是**第 N 局已经结算完、人在主题入口页**。这就是「本局结束后再停」。

**② 投资达标 / 储备满 → 先退本局再停（含放弃）**

```cpp
// src/MaaCore/Task/Roguelike/RoguelikeInvestTaskPlugin.cpp:233-237
void RoguelikeInvestTaskPlugin::stop_roguelike() const {
    m_control_ptr->exit_then_stop(true);   // abandon = true
    m_task_ptr->set_enable(false);
}
// 触发点：148-155（投资次数达到 investments_count，回调 RoguelikeInvestmentReachLimit）
//         156-164（deposit >= 999 且 stop_when_investment_full，回调 RoguelikeInvestmentReachFull）
```

```cpp
// RoguelikeControlTaskPlugin.cpp:41-47
void RoguelikeControlTaskPlugin::exit_then_stop(bool abandon) const {
    ProcessTask(*this, { theme + "@Roguelike@ExitThenAbandon" })
        .set_times_limit("Roguelike@Abandon", abandon ? INT_MAX : 0)
        .set_times_limit("Roguelike@StartExplore", 0).run();
}
```

→ 会主动「退出 + 放弃」当前这一局，再停。**不会把进度卡在地图里。**
另外 `Roguelike@StageTraderInvestSystemFull`（OCR「当前储备余额已达上限」）的 `exceededNext` 也直接指向 `RoguelikeControlTaskPlugin-ExitThenStop` `[res:base.json]`。

**⚠️ 没有 API 能从外部触发 `exit_then_stop`。** maa-cli 不暴露 `AsstSetTaskParams`（`crates/maa-core/src/lib.rs` 里有 `stop`/`start`/`append_task`，但 maa-cli 运行期只轮询 flag，不改参数）。所以「运行中动态把 starts_count 改小」这条路走不通。

### 3.5 (c) 推荐停止策略

#### ✅ 推荐方案：`--starts-count 1` + worker 循环派发（单锁单模拟器）

形状（照抄现有 `_clearing_run_locked` 的结构 `[scripts/maa_copilot_worker.py:1560-1600]`）：

```
with run_lock(blocking=False):          # 复用现有锁，不新增锁语义
    open_emulator / wait_for_android / verify_game_installed / settle_android_runtime
    while not stop_requested() and rounds_done < max_rounds and not deadline_passed():
        subprocess: maa roguelike <THEME> ... --starts-count 1
        解析 stdout → 发 Discord 进度
        rounds_done += 1
    close_everything()
```

- **停止指令** = 往 state 文件写一个 flag（或复用 buttons.json 的按钮回调），worker 在**每局之间**读一次。当前这一局由上游跑完 → `starts_count=1` 用尽 → 干净停在入口页 → 子进程正常退出 → worker 看到 flag 不再派发。**完全满足「收到指令后本局结束再停」**，且**一行 MaaCore 代码都不用碰**。
- **每局重连开销：实测约 1 s**。asst.log 里同一天的两次子进程（模拟器已开着）：
  - `Px40533`：`MaaCore Process Start 15:17:03.640` → `Assistant::start 15:17:04.866` = **1.23 s**（其中 `ctrl_connect` 892 ms）
  - `Px40663`：`15:18:02.717` → `15:18:03.625` = **0.91 s**（`ctrl_connect` 618 ms：AdbController 84 ms + MinitouchController 311 ms）
  `[res:debug/asst.log:4925-4927, 5445-5451, 5842-5846, 6280-6360]`
  再加 maa-cli 自身进程启动 + libMaaCore 加载，保守算 **每局 2–4 s**。相对一局十几到几十分钟，**开销 < 0.5%**，可以忽略。
- **额外收益**：每局一条 Discord 进度消息；每局一次天然的取消点；单个子进程崩了不会带走整轮；每局的 summary（`Explorations:` 段）可以直接贴出来。
- **代价**：worker 要自己算「跑几局 / 跑到几点」；每局都要重新做主题导航（上游 `Roguelike@Begin` 会处理，但多一次 OCR 往返）。

#### 备选 A：一个长子进程 `--starts-count N`，停止用 SIGTERM

- 代价：**SIGTERM = 立即中止**（3.1 + 3.2），会停在地图/战斗里；下一次开局要靠 `Roguelike@Begin` 的 `Continue → ExitThenAbandon → AbandonConfirm` 链善后，等于**白扔当前这一局的进度**。
- 而且第二次信号会直接杀进程（3.1），Python 的 `subprocess.run(timeout=…)` 超时正是 `kill()`（SIGKILL），会留下没清理的 MaaCore + 开着的模拟器。
- 只适合「立刻停、不在乎这一局」的紧急停止按钮。
- 若要用，必须用 `Popen` + `terminate()` + `wait(timeout)` + 只在万不得已时 `kill()`，**不能**直接用 `subprocess.run(timeout=…)`。

#### 备选 B：一个长子进程 + 投资模式自然停

- 只在 mode 1 有意义：`--investments-count K` 或让储备到 999，MaaCore 自己 `exit_then_stop(true)`（3.4②），干净。
- 代价：停止点由游戏内储备决定，**不受用户指令控制**；不适合「随时叫停」。
- 可以和推荐方案叠加（每个子进程既有 `--starts-count 1` 又有 `--investments-count`）。

#### 备选 C：改 maa-cli / 用 fork 暴露 `AsstSetTaskParams`

- 能做到「运行中把 starts_count 改成当前局数」= 真正的 graceful stop 且零重连开销。
- 代价：要维护一个 maa-cli fork 或写自己的 FFI 宿主；违背本项目「只调用上游、执行过程由上游全程控制」的定位。**不推荐。**

#### 两条必须遵守的约束

1. **中断后必须用同一主题重启**（3.3 的跨主题限制）。建议把「上次未干净结束的主题」持久化到 state，未清干净前禁止切主题。
2. **紧急停止（备选 A）之后**，下一轮开局会自动放弃残局，但那一局的分数/源石锭作废——Discord 上要说清楚这一点。

---

## 4. 输出格式：可用于判定的标记字符串

maa-cli 默认 log level 就打 `info`（现有 `~/.hermes/state/maa/runs/*.log` 里能看到 `Connected` / `StartUp Start`），下面的字符串**不加 `-v` 就有**（标注 trace 的除外）。全部来自 `[cli:run/callback/mod.rs]`。

### 4.1 任务链级（最可靠的判定点）

| 输出 | 来源 | 含义 |
| - | - | - |
| `Roguelike Start` | `TaskChainStart` `[:162-165]` | 子进程接管成功 |
| `Roguelike Completed` | `TaskChainCompleted` `[:166-169]` | **本次子进程干净跑完**（`starts_count` 用尽 / 投资达标 都走这里） |
| `Roguelike Stopped` (warn) | `TaskChainStopped` `[:170-173]` | 被 `AsstStop` 中断 |
| `Roguelike Error` (error) | `TaskChainError` `[:174-178]` | 任务链出错 |
| `AllTasksCompleted` | `[:66-69]` | 全部任务结束 |
| `Connected` / `Disconnected` / `Reconnect n times` / `ReconnectSuccess` | `ConnectionInfo` `[:123-131]` | 连接状态 |

### 4.2 局内标记（SubTaskStart）`[:256-306]`

| 输出 | `what` | 含义 |
| - | - | - |
| `Start exploration {exec_times} times` | `StartExplore` | **一局开始**（`exec_times` 是第几次） |
| `Exploration Abandoned` | `ExitThenAbandon` | 本局被放弃 |
| `ExplorationConfirmed` | `ExitThenConfirm` | 本局确认退出 |
| `MissionCompleted` | `MissionCompletedFlag` | 单场战斗胜 |
| `MissionFailed` | `MissionFailedFlag` | 单场战斗败（taskchain==Roguelike 时同时把本局标记为 Failed） |
| `RoguelikeGamePass` | `GamePass` | 通关 |
| `TraderInvestSystemFull` (warn) | `StageTraderInvestSystemFull` | **投资储备已满** |
| `StageTraderEnter` / `StageSafeHouseEnter` / `StageCombatOpsEnter` / `EmergencyOpsEnter` / `DreadfulFoe` | 各节点 | 进入对应节点 |
| `MissionStart` | `BattleStartAll` | 战斗开打 |
| `RoguelikeSpecialItemBought` | `StageTraderSpecialShoppingAfterRefresh` | 买到特殊商品 |
| `GameOffline` (warn) | `OfflineConfirm` | 掉线；`--no-auto-reconnect` 时会置 `offline_stop` 让 maa-cli 主动收尾 |
| `InfrastDormDoubleConfirmed` | — | （非肉鸽） |

### 4.3 局内标记（SubTaskExtraInfo）`[:501-525]`

| 输出 | `what` | 含义 |
| - | - | - |
| `StartCombat {name}` | `StageInfo` | 进入某关卡 |
| `StageInfoError` (error) | `StageInfoError` | 关卡识别失败 |
| `Deposit {count} / {total} / {deposit} originium ingots` | `RoguelikeInvestment` | **本次投了 count，累计 total，当前储备 deposit**（deposit 上限 999 `[core:RoguelikeInvestTaskPlugin.cpp:70,156]`） |
| `Gain {exp} exp during this exploration` | `RoguelikeSettlement` | **一局结算**（最可靠的「一局结束」标记，插件名就叫 RoguelikeSettlement） |

### 4.4 ⚠️ 两个「有回调但 maa-cli 不打印」的关键事件

MaaCore 会发 `RoguelikeInvestmentReachLimit`（带 `details.limit`）和 `RoguelikeInvestmentReachFull` `[core:RoguelikeInvestTaskPlugin.cpp:150-153, 160-161]`，但 maa-cli 的 `process_subtask_extra_info` 没有对应分支，会落到 `trace!("UnknownSubTaskExtraInfo: {json}")` `[cli:callback/mod.rs:555]` —— **只有 `-vvv` 级别才看得到**。
→ 想判定「投资已满因此收手」，实用做法是抓 `TraderInvestSystemFull`（warn，默认可见）+ 随后的 `Roguelike Completed`。

### 4.5 结尾 summary（`--no-summary` 可关，默认开）

`[cli:run/callback/summary.rs:698-725, 1146-1170]`，格式已由单测钉死：

```
Explorations:
1. Failed, invest 10 originium ingotes, gained 100 exp;
2. Passed, invest 18 originium ingotes, gained 200 exp;
3. Unknown;
Summary:
Passed 1, Failed 1, Unknown 1
Total invest 28 originium ingotes
Total gained 300 exp
```

状态枚举只有 4 个：`Passed` / `Failed` / `Abandoned` / `Unknown` `[summary.rs:728-755]`。
→ **这段可以直接整块转成 Discord embed**，是最省事的战报来源。

### 4.6 建议的解析规则

| 判定 | 规则 |
| - | - |
| 一局开始 | `Start exploration \d+ times` |
| 一局结束 | `Gain \d+ exp during this exploration`（结算）或 `Exploration Abandoned` |
| 投资进度 | `Deposit (\d+) / (\d+) / (\d+) originium ingots` |
| 投资已满 | `TraderInvestSystemFull` |
| 本次子进程干净结束 | `Roguelike Completed` **且** returncode==0（照抄 copilot 里 `"Fight Completed" not in nav_output` 的写法 `[scripts/maa_copilot_worker.py:1197-1200]`） |
| 被中断 | `Roguelike Stopped` |
| 出错 | `Roguelike Error` |

---

## 5. 资源与时间

| 问题 | 结论 | 出处 |
| - | - | - |
| **消耗理智吗？** | **不消耗。**「游玩本模式不消耗理智，不计入模组任务进度。」 | PRTS wiki《集成战略》条目 https://prts.wiki/w/集成战略 |
| 一局耗时 | **本机无数据可估**：`asst.log` / `asst.bak.log` 里**从未出现过 Roguelike 任务链回调**（grep `taskchain":"Roguelike"` 命中 0），该功能在本机没跑过。上游唯一给出的时间边界是单场战斗的看门狗：**战斗超 8 分钟自动撤退所有地面单位，超 10 分钟自动放弃当局战斗** | `[doc:manual]`；`[res:debug/asst.log]` |
| mode 1（刷源石锭）是否「不间断」推荐用法 | 是。协议原文：「`1` - 刷源石锭，**第一层投资完就退出**」，所以单局极短、天然适合循环刷。手册还给了加速技巧：Sarkaz「开局分队为 `点刺成锭分队` 时，将会使用刷新商店策略加速流程」；界园「难度 ≥ `请君入园·3`、刷源石锭、开局 `指挥分队` 时，将会使用 `时光之末` 跳关策略加速流程」 | `[doc:integration]`、`[doc:manual]` |
| 有无每日/每周上限 | **游戏本身对进入次数无限制**（PRTS 未提及次数上限）。实际的自然停止条件是**源石锭储备上限**：MaaCore 以 `deposit >= 999` 判定满仓，并有 OCR 任务匹配「当前储备余额已达上限」 | `[core:RoguelikeInvestTaskPlugin.cpp:70,156-164]`；`[res:base.json "Roguelike@StageTraderInvestSystemFull"]` |
| 建议的停止条件 | ① `stop_when_investment_full`（maa-cli 默认就是 true，见 1.2）→ 储备到 999 自动优雅收手；② `--investments-count K`；③ `--starts-count N` 控制局数；④ worker 侧的墙钟 deadline（避开 08:00/22:00 日常与 04:20–05:00 Depot 窗口，复用 `_copilot_blackout_reason` / `_depot_window_reason` `[scripts/maa_copilot_worker.py:1070-1090]`） | |
| 前置条件（会影响成功率） | 「至少 60 帧的**稳定**游戏帧数」「触控模式为 Minitouch、MaaTouch 或 MuMu 触控增强」「请在游戏内将对应肉鸽主题**置顶在终端处**，虽然目前也可以自动导航，但不保证长期可用性」「新客户端首次游玩会有新手教程，需手动关闭」 | `[doc:manual]` |
| 推荐开局（上游 2026/8/18 更新） | 傀影 正式调查·2 / 指挥分队·突击战术分队 / 取长补短 / 丰川祥子·棘刺；水月 波涛迭起·3~10 / 以人为本·心胜于物 / 稳扎稳打 / 维什戴尔；萨米 挑战自然·4~10 / 特训·远程战术 / 稳扎稳打 / 维什戴尔；萨卡兹 直面魂灵·4~10 / 蓝图测绘·远程战术 / 稳扎稳打 / 维什戴尔；界园 请君入园·3~10 / 指挥·远程战术 / 稳扎稳打 / 维什戴尔；黑流树海 暂无难度推荐 / 特勤·堡垒战术 / 稳扎稳打·坚不可摧 / 机械师（精二） | `[doc:manual]` |

---

## 6. 与现有基础设施的衔接 / 已知坑

| # | 事项 | 结论 |
| - | - | - |
| 6.1 | `maa roguelike` **不会**自动启动游戏 | 预设只 append 一个任务：`TaskConfig::new_with_task(task)` `[cli:run/preset/mod.rs:49-56]`，**没有 StartUp**。必须由项目自己 `open_emulator / wait_for_android / verify_game_installed / settle_android_runtime`，跟 `_clearing_run_locked` 完全一样 `[scripts/maa_copilot_worker.py:1512-1520]`。 |
| 6.2 | `--batch` 是否必需 | **不必需**。`--batch` 只用于跳过**任务文件里 input 字段**的交互提问 `[help]`；`roguelike` 预设的参数全部来自命令行 `[cli:roguelike.rs:163-244]`，没有 input。加上无害，建议照现有习惯加上以防万一。 |
| 6.3 | `maa_subprocess_env()` | 直接用 `maa_subprocess_env(config)`（**不要** `keep_proxy=True`）。注释写明：daily `maa run` 剔代理是因为「MaaCore's in-run hot update misbehaves when those are set」`[scripts/maa_common.py:248-267]`——肉鸽同样是 in-run，同样适用。另外建议照 clearing 的做法加 `env["MAA_LOG_PREFIX"] = "Never"` `[scripts/maa_copilot_worker.py:1521]`，便于解析。 |
| 6.4 | `run.lock` | 直接复用，非阻塞抢锁，抢不到就跳过并回 Discord——沿用不变量 #13，不新增锁语义 `[scripts/maa_copilot_worker.py:1310-1325]`。整轮（N 局）共用**一把锁、一次模拟器开关**。 |
| 6.5 | `close_everything()` | 放 `finally`，同 clearing `[scripts/maa_copilot_worker.py:1573-1578]`。注意 maa-cli 被 SIGINT 打断时会跳过自己的 close_app 分支 `[cli:run/mod.rs:240-243]`，我们的 `close_everything()` 是唯一兜底。 |
| 6.6 | **⚠️ subprocess 超时不能用 `subprocess.run(timeout=)`** | 现有 copilot 用 `subprocess.run(..., timeout=900)` `[scripts/maa_copilot_worker.py:1190-1194]`。肉鸽一局远超 900 s，且 `subprocess.run` 超时走 `kill()`（SIGKILL），MaaCore 来不及 destroy，模拟器会留在地图里。**必须**改用 `Popen` + `terminate()`（SIGTERM，走 3.1 的 flag 路径）+ `wait(timeout)` + 最后才 `kill()`。 |
| 6.7 | **⚠️ BlackFlow 缺口** | 用户要求「支持当前游戏内全部肉鸽主题」，但 maa-cli 0.7.5 的 `Theme` enum 没有 `BlackFlow` `[cli:roguelike.rs:8-14]`，本机 `--help` 也证实。**解法**：像 `_navigation_config()` 那样开一个临时 `MAA_CONFIG_DIR`，往 `tasks/roguelike.toml` 写 `type = "Roguelike"` + 自由 `[tasks.params]`，然后 `maa run roguelike` `[scripts/maa_copilot_worker.py:1154-1177]`。这条路顺便解锁 mode 6/7/10001/20001/30001 和全部 12 个 maa-cli 覆盖不到的字段。**建议直接走任务文件路线，不用 `maa roguelike` 子命令。** |
| 6.8 | **⚠️ maa-cli 的两个默认值反常** | ① `stop_when_investment_full` 在 maa-cli 里默认 **true**（MaaCore 协议默认 false）`[cli:roguelike.rs:190]`——`--dry-run` 已实测；② Sami 主题下 `use_foldartal` 不加 flag 会被强制发 `false`（MaaCore 非 mode5 默认 true）`[cli:roguelike.rs:219]`。走任务文件路线可以完全绕开这两个坑。 |
| 6.9 | **⚠️ Phantom 难度** | maa-cli 预设对 Phantom 直接丢弃 `--difficulty`，但 MaaCore/GUI 认为 Phantom 最高难度 15。→ 又一条「走任务文件」的理由。 |
| 6.10 | **⚠️ `start_with_seed` 类型不符** | maa-cli 发 bool，MaaCore 协议要 string（种子本身）。走任务文件可以正确传字符串种子。 |
| 6.11 | 主题置顶 | 手册要求把目标主题置顶在终端。自动导航「不保证长期可用性」`[doc:manual]`——建议在 Discord 卡片上写清楚「换主题前先手动在游戏里置顶」，或至少在连续失败时提示。 |
| 6.12 | 跨主题残局 | 见 3.5 的约束 1：中断后必须同主题重启。建议 state 里记 `last_theme` + `dirty` 标志。 |

---

## 7. 建议做成 Discord 下拉的字段清单

Discord 限制：一张卡最多 5 个 action row，每个 row 一个 select（select 独占一行），每个 select ≤ 25 个 option。
→ **4 个 select + 1 行按钮 = 正好 5 行。**

| # | 控件 | 字段 | 选项（数量） | 默认值 | 理由 |
| - | - | - | - | - | - |
| 1 | Select | `theme` | 傀影 Phantom / 水月 Mizuki / 萨米 Sami / 萨卡兹 Sarkaz / 界园 JieGarden / 黑流树海 BlackFlow（**6**） | `Sarkaz` | 需求明确要求「全部主题」。BlackFlow 需走任务文件路线（6.7）。 |
| 2 | Select | `mode` | 刷等级/分数 `0` / 刷源石锭 `1` / 烧水凹开局 `4` /（Sami 追加）刷坍缩范式 `5` /（可选）月度小队 `6`、深入调查 `7`、界园常乐 `20001`、黑流襁褓 `30001`（**≤8**） | `1`（刷源石锭） | 单局最短、收益最直观，最适合「手动触发跑几局」。 |
| 3 | Select | `difficulty` | `不指定 (-1)` + `MAX (n)` + `n … 0` 倒序（Phantom/Sami/BlackFlow 共 **18** 项；Mizuki/Sarkaz/JieGarden 共 **21** 项，均 < 25） | 按主题取手册推荐值（萨卡兹 4、界园 3、水月 3、萨米 4、傀影 2）`[doc:manual]` | 选项数天然在 25 以内；上限见 2.2。 |
| 4 | Select | `squad` | 按 `theme`（+`mode`）动态生成，见 2.3；最多 12 主题专属 + 6 通用 + 1 高规格 = **19** | 按手册推荐（萨卡兹刷锭 → `点刺成锭分队`；否则 `指挥分队`） | 分队直接决定加速策略（点刺成锭 → 刷新商店加速；界园指挥 → 时光之末跳关）`[doc:manual]`。 |
| 5 | Buttons | — | `▶️ 开始` / `⏹️ 本局结束后停止` / `⏏️ 立即中止` | — | 「本局结束后停止」= 置 flag，不派发下一局（3.5 推荐方案）；「立即中止」= SIGTERM（备选 A，须提示会丢当前局）。 |

**用默认值、不上卡片的字段**（连同理由）：

| 字段 | 采用值 | 理由 |
| - | - | - |
| `roles` 职业组 | `稳扎稳打` | 手册对除傀影外所有主题都推荐它 `[doc:manual]`；傀影推荐 `取长补短`，可按主题在代码里映射，不占 UI。 |
| `core_char` 开局干员 | 留空（自动） | 自由文本，做不成下拉；留空让上游按练度选。可用 Modal 二期补。 |
| `starts_count` | 固定 `1`（每个子进程一局） | 这是停止策略的实现细节，不该暴露（3.5）。轮数用「跑到几点/几局」由 worker 侧配置或第二张卡控制。 |
| `investments_count` | 不传 | 用 `stop_when_investment_full` 就够。 |
| `stop_when_investment_full` | `true` | 储备满 999 自动优雅收手（3.4②）。 |
| `use_support` / `use_nonfriend_support` | `false` | 需要先配好开局干员名才有意义 `[doc:manual]`。 |
| `stop_at_final_boss` / `stop_at_max_level` | `false` | 与「刷」的目标冲突。 |
| Sami 的 `-P` / `use_foldartal`、Mizuki 的 `refresh_trader_with_dice`、mode 4 的 `collectible_mode_start_list` 等 | 主题/模式相关的默认值 | 属于进阶玩法，UI 放不下；需要时二期加「高级」按钮弹 Modal。 |

---

## 8. 未确认 / 需要真机验证的点

1. **一局实际耗时**（各主题 × 各模式）。本机 MaaCore 日志里从未跑过肉鸽，无任何数据。这是排「跑到几点」和子进程超时上限的必要输入。**必须实测**。
2. **`--starts-count 1` 的收尾位置**。代码推导是「第 1 局结算完、回到主题入口页时停」，但 `RoguelikeControlTaskPlugin-Stop` 触发瞬间的实际画面（是否还有未关的结算弹窗）没验证过。要看第 2 次派发能否无缝接上。
3. **BlackFlow 走任务文件是否真能跑**。`maa run` + 自定义 TOML 传 `theme="BlackFlow"` 在类型层面没问题（params 自由透传），但没有实跑验证；且手册说黑流树海「自动战斗等策略尚在编写中」`[doc:manual]`，成功率存疑。
4. **`start_with_seed` 的 bool/string 类型冲突**在 MaaCore 侧的实际行为（是被忽略还是报错）。
5. **中断后同主题恢复链是否可靠**。`Roguelike@Begin` 的恢复分支在代码里存在，但「战斗中被 SIGKILL → 下次开局」这条最坏路径没验证过。
6. **游戏内主题置顶状态**。手册要求置顶目标主题；切主题时自动导航「不保证长期可用性」，需要确认本机当前置顶的是哪个主题、切换是否稳定（社区 issue #13245 就是界园被识别成萨卡兹导致死循环）。
7. **60 帧 / 触控模式前置条件**是否满足。本机 `FastestWayToScreencap` 是 `RawByNc`（106–149 ms），触控是 `MinitouchController` `[res:asst.log]`——触控 OK，帧率未测。
8. **`stop_when_investment_full` 触发后的储备**：MaaCore 用 999 判满，但游戏内「储备余额上限」是否恒为 999、是否随活动变化，未核实。
