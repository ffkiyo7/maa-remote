# 二期：prts.plus 作业站接入（抄作业代打）

## Context

一期已上线：Hermes gateway 常驻接管 Discord，仓库只提供「跑完就退」的短命脚本，
每天 07:50 发计划卡、08:00/22:00 跑 `maa run daily` 自动刷理智。
关卡维度只有一个 `stage` 字符串，没有任何作业/编队能力。

二期要做的是：在 Discord 里说出一个关卡 → 自动从 prts.plus（MAA 作业站）匹配一份
**你的干员练度跑得动**的作业 → 传给 maa-cli 自动战斗。

约束（用户已确认）：
- 抄作业与原有自动刷理智**互斥、不并行**，默认行为仍是自动刷理智，抄作业只按需触发。
- 练度数据来自**森空岛**（一次性粘贴鹰角 token，本地缓存）。
- 匹配后**列 Top-3 + 按钮选**，不自动开跑。
- 没有完全满足练度的作业时，**列最接近的并标注缺口**，仍可由用户点按钮执行。
- 关卡覆盖：**当期活动 + 主线 + 资源本，且必须支持突袭**。

---

## Spike 实测结论（2026-08-02 实跑，优先于任何文档）

这些是设计的地基，都在本机/线上验证过：

1. **`maa copilot` 的关卡解析是本地的。** `get_stage_info()` 在
   `<resource>/Arknights-Tile-Pos/` 找文件名 `starts_with(stage_name)` 的 json。
   本机 4128 个文件，**0.53s 可建成 2099 个 code→stageId 索引**
   （`TO-9→act53side_09`、`1-7→main_01-07`、`CE-6→wk_melee_6`）。
   这就是关卡映射的权威来源，且 `maa update` 会刷新它 —— 计划卡每周已经在跑 `maa update`。
   不需要 18MB 的 ArknightsGameData，也不能用 prts 的关卡索引（见 3）。

2. **43% 的作业按原样跑不起来。** 作业的 `stage_name` 有两种写法并存：
   内部 `act53side_09` 和展示码 `TO-9`。后者在 Tile-Pos 里找不到文件，
   `get_stage_info` 直接 `bail!`（硬失败，不是交互提示）。
   实测当期 141 份作业里 **80 份可跑 / 61 份展示码形式**。
   → **解法：本地改写。** `maa copilot` 支持 `file://<path>`，把作业 JSON 下载下来、
   把 `stage_name` 改成 stageId 再落盘执行，这 61 份就全部救回来了。

3. **prts 的 `level_keyword` 会静默退化成「不过滤」。** 关卡不在它自己那份过期索引里时，
   返回的是全量 total。实测 `main_01-07`→297、`wk_melee_6`→105（正常），
   `act53side_09`→39406（= 全量，退化）。服务端索引最新只到 `act51side`，
   当期活动 `act53side` 根本没有。
   → **必须客户端按 `content.stage_name` 自己过滤**；用一次 `limit=1` 的全量探针比对 total 来
   确定性地检测退化，不要用阈值猜。

4. **`/copilot/get` 的排序元数据是坏的。** 同一个 id 98774：
   `query` 给 `hot_score 26.6 / rating_ratio 1.0 / rating_level 10`，
   `get` 给 `0.0 / 0.0 / 0`。→ **排序只用 `query` 的列表数据，`get` 只用来取执行用的完整
   `content`（含 `actions`，`query` 的结果要裁掉 `actions` 才能缓存）。**

5. **`document=` 全文搜索有效但硬顶 50 条**（`total:50, has_next:false`）。只能当便宜的补充源。

6. **练度：森空岛是唯一可行解。** 当期 141 份作业中 **79% 需要专精(`skill_level`≥8)或模组**，
   只有 21% 完全没有 requirements。MAA 自带的 `OperBox` 本地扫描
   （task type 确实存在于 maa-cli 0.7.5）**结构上只能读 拥有/精英化/等级/潜能**，
   读不到专精和模组 —— 它只能兜住 21%。
   `requirements` 的实际字段分布：`skill_level<8` 906 次、`level` 607、`module` 239、
   `skill_level>=8` 219、`elite` 34。

7. **不需要动 Hermes adapter。** `_load_hermes_button_registry()` 在每次交互时
   （`adapter.py:5871`）从磁盘重读 `buttons.json`，且 `maa_action.py` 已在
   `_HERMES_BUTTON_SCRIPTS` 白名单里。→ **本期新增按钮零 adapter 补丁、零
   `LOCAL_PATCHES.md`、零 gateway 重启**，一切走 `maa_action.py`。

8. **`--formation` 是硬要求。** 不传的话 maa-cli 会打印「Please set up your formation manually」
   并卡在交互式 BoolInput。无人值守必须 `--formation --batch`。
   另外 `maa copilot` **不含 StartUp**，需要像现有 `maa closedown` 那样单独跑 `maa startup Official`。

9. **突袭由 `--raid` 驱动，不能看 `#f#`。** `difficulty` 是位掩码（1 普通 / 2 突袭 / 3 兼容，
   常常缺失）。Tile-Pos 里 686 个 `#f#` 文件，但**当期 `act53side` 一个都没有** ——
   `#f#` 不是突袭可用性信号。失败是良性的：MaaCore `ChangeToRaidDifficulty`
   有 `maxTimes:3 / next:[RaidConfirm,#self]`，切不过去就按普通跑，不会挂死。

10. **干员名→charId 用本地 `battle_data.json`**（655KB，`maa update` 刷新，1325 条 / 456 个 `char_*`）。
    跨类型有名字冲突（`Mon3tr` 同时是 `char_4179_monstr` 和 `token_10002_kalts_mon3tr`），
    **解析时优先 `char_*`；只能解析到 `token_*`/`trap_*` 的是召唤物，忽略、不计入缺口**。

---

## 架构

新增 3 个脚本，走现有的「短命进程 + 后台 worker」范式：

| 文件 | 角色 | 可被按钮调用 |
|---|---|---|
| `scripts/maa_copilot.py` | 领域层（几乎纯函数）：关卡索引、prts 客户端、打分、缺口文案、卡片/按钮构建 | 否（被 import） |
| `scripts/maa_copilot_worker.py` | 两个后台长任务：`search <token>` / `run <token> <n> <mode>` | **否，刻意不进白名单** |
| `scripts/maa_skland.py` | 森空岛协议：stdlib HMAC 签名、grant→cred 链、练度拉取与归一化、`link`（只从 stdin 收 token） | 否 |

改动：`scripts/maa_common.py`（新路径常量、`prune_button_registry(prefix)`、通用
`http_get_json()`）、`scripts/maa_action.py`（新增 `copilot` 子命令 + `valid_copilot_stage()`）、
`scripts/maa_plan_card.py`（第二个下拉行 + 在**已有的** `state_lock()` 块里清理过期按钮）、
`deploy.py`、`skills/games/maa-operator/SKILL.md`、三份文档。

`maa_copilot_worker.py` **复用 `maa_daily.py` 的设备层**（`open_emulator`、`wait_for_android`、
`capture_screenshot`、`close_everything`、`send_report` 等）—— 这是仓库既有惯例，
`maa_action.py:207` 已经在 `from maa_daily import extract_summary`。
不把设备层上提到 `maa_common.py`：那会打乱现有 28 个测试的覆盖边界，收益不抵风险。

### 20 秒预算怎么绕开

```
按钮/下拉  →  maa_action.py copilot find <关卡>      ~10ms，零网络
                ├─ 读本地 stage_index.json 解析关卡
                ├─ 写 searches/<token>.json {status:pending}
                ├─ Popen(maa_copilot_worker.py search <token>, start_new_session=True)
                └─ emit "🔍 正在为 TO-9 匹配作业，结果稍后发到频道"
                          ↓ 脱离 20s 预算
           maa_copilot_worker.py search        5~20s
                ├─ flock copilot_search.lock（防刷，拿不到就静默退出）
                ├─ 必要时重建关卡索引；读 roster.json（可缺失→降级模式）
                ├─ 搜索 + 客户端过滤 + 打分 + 排序 → Top-3
                ├─ state_lock() 下：清过期 → merge 新的 hermes:btn:maa-copilot:* 键
                └─ 发候选卡片到频道，回写 searches/<token>.json
           用户点 ▶️/🔥 → maa_action.py copilot pick <token> <n> <mode>   ~50ms
                └─ 校验 → run.lock 预检 → Popen(worker run) → 立即 ack
```

**按钮前缀用 `hermes:btn:maa-copilot:`（是 `-` 不是 `:`）。**
`"hermes:btn:maa-copilot:x".startswith("hermes:btn:maa:")` 为 `False`，
所以 `maa_plan_card.py:404-406` 每早的前缀清扫**碰不到**它，
DEV_GUIDE 不变量 #7「只碰 `hermes:btn:maa:` 前缀」原文照旧成立。
清理分三层：新搜索时清过期、07:50 在已有锁块里清、`copilot cancel` 时即时清。

### 关键数据结构

```
~/.hermes/state/maa/copilot/           0700
  skland.json          0600  {hg_token, cred, sign_token, uid, cred_updated_at, last_error}
  roster.json          0600  {fetched_at, uid, chars:{charId:{name,level,elite,
                                main_skill_lvl,skills:[spec…],module_levels:[…],rarity}}}
  stage_index.json     0600  {built_at, resource_signature, codes:{}, stages:{}, raid_variants:[]}
  cache/<stage_id>.json 0600 搜索结果，裁掉 actions，TTL 12h
  searches/<token>.json 0600 {token,status,expires_at,stage,stage_id,candidates:[…],message_id}
  copilot_search.lock         flock，搜索去重
runs/<date>-copilot-<token>.{json,log,png}   复用现有 cleanup_run_files()
```

全部经 `atomic_write_json`（0600）。锁只多一把 `copilot_search.lock`；
注册表仍用 `state_lock()`，执行仍用 `run.lock`。
`stage_index` 用 `resource/version.json:last_updated` + hot-update 目录 mtime 作签名失效。
`maa_action.py` 只读该缓存，仅在文件完全缺失时才现建（0.53s，远在 20s 内）。

### 匹配算法

搜索（`maa_copilot.search_copilots`）：
先发一次 `limit=1` 全量探针拿 total → `document=code` / `document=stage_id`（各 ≤50）
→ `level_keyword=stage_id` 且 **`filtered.total < 全量 total`** 才认为过滤生效，
否则退化为扫最新 N 页（`order_by=id&desc=true`，实测 8 页 800 条 ≈13s 能覆盖当期全部）。
**最后一律按 `content.stage_name ∈ {stage_id, code}` 客户端过滤。**

打分：
```python
def effective_skill_level(char, skill_index):   # 作业的 skill 是 1-based
    slots = char["skills"]
    if not 1 <= skill_index <= len(slots):
        return None                             # 技能位未解锁 → 硬缺口
    spec = slots[skill_index - 1]
    return 7 + spec if spec > 0 else char["main_skill_lvl"]   # 专精1/2/3 → 8/9/10
```
- `groups` 是**备选组**：组内任一干员满足即算满足；都不满足取缺口最小的那个报缺。
- `module` 是 in-game 模组页的 UI 序号，不能可靠映射到森空岛 `equip[].id`
  → 按**软约束**半权重计分，文案写「模组N（未解锁，无法精确校验）」。
- 排序 `key = (round(ratio,2), quality, upload_ts)` 降序；
  `quality = 0.5*rating_ratio + 0.3*log 归一 views + 0.2*hot_score`
  （`not_enough_rating` 时 rating 记 0）。分桶是为了让「同样 100% 满足」时热度来决胜负。

硬排除：`available=False`、`minimum_required` 高于本机 MaaCore（`v6.16.0`）、
`opers`+`groups` 全空、重复 id。
**注意：`stage_name` 是展示码的那 43% 不排除**，改用下面的本地改写执行。

缺口文案就是用户要的形式：`⚠️ 满足 7/8 — 缺：史尔特尔 专精2（你专精1）`。

### 执行

```
maa startup Official
maa copilot <uri> --formation --batch -v [--raid normal|raid] [--ignore-requirements]
```
- `<uri>`：`stage_name` 已是 stageId 时用 `maa://<id>`（走 maa-cli 自己的缓存）；
  是展示码时**从 `/copilot/get/<id>` 取完整 content、改写 `stage_name`、写到
  `copilot/prepared/<id>.json`，再用 `file://` 传入**。
- **永不传 `--use-sanity-potion`**（新不变量，配套 argv 精确断言测试）。
- `--ignore-requirements` **只在用户点的候选 `ratio < 1.0` 时才带** ——
  MaaCore 自身的练度校验会让自动编队失败，不带就实现不了「标注缺口后仍可执行」。
  报告里明写「已忽略作业练度要求」。

### 森空岛

纯 stdlib 签名：
```python
t = str(int(time.time()) - 1)
header_ca = {"platform": "", "timestamp": t, "dId": "", "vName": ""}   # 顺序参与签名，不要排序
s = path + (query or body) + t + json.dumps(header_ca, separators=(",", ":"))
sign = hashlib.md5(hmac.new(sign_token.encode(), s.encode(), hashlib.sha256).hexdigest().encode()).hexdigest()
```
链路：`POST as.hypergryph.com/user/oauth2/v2/grant {token, appCode:"4ca99fa6b56cc2ba", type:0}`
→ code → `POST zonai.skland.com/api/v1/user/auth/generate_cred_by_code {code, kind:1}`
→ `{cred, token, userId}`（**返回的 `token` 是签名密钥，和 cred、和鹰角 token 都不是一个东西**）
→ `GET /api/v1/game/player/binding` 取 uid → `GET /api/v1/game/player/info?uid=` 取 `chars[]`。

token 两种注入方式，都不经过 Discord、不进 argv、不进 agent 上下文：
```bash
SKLAND_HG_TOKEN=<token> python3 deploy.py          # 对齐现有 MAA_CHANNEL_ID 的做法
python3 ~/.hermes/scripts/maa_skland.py link       # 本机终端，从 stdin 读
```
`deploy.resolve_skland_token()` 仿 `deploy.py:83 resolve_channel_id()`，但**缺失时不报错**
（没配森空岛也要能正常部署）。仓库不带占位符，`config.json` 永不存 token。
过期处理：`10002` → 用存着的 `hg_token` 重跑 grant，**只重试一次**，再失败就记 `last_error`
并在下张卡片提示「请在本机终端重新运行 maa_skland.py link」。绝不循环重试。

---

## 护栏

1. **不动 `STAGE_PATTERN` / `valid_stage`**（那是刷本计划的），新增更强的
   `valid_copilot_stage()`：输入长度截断到 40，然后**只接受本地 `stage_index` 里
   `codes` 或 `stages` 的键**。精确查表比正则更强。
2. `maa://<id>` 的 id 只从 `searches/<token>.json` 读，断言 `isdigit()` 且 `0 < id < 1e9`；
   token 校验 `^[0-9a-f]{12}$`；index 限 `1..len(candidates)`；mode ∈ `{normal, raid}`。
   Discord 下拉值仍是不透明 `choice:N` 经 `select_args` 查表。
3. `stone = 0` 不变量保持：抄作业路径不渲染 `daily.toml`、不碰 `plan.json`/`defaults.json`。
4. **互斥 + 防饿死**：worker 取 `run_lock(blocking=False)`；`copilot pick` 用
   `is_run_locked()` 预检。另外 `maa_daily.py:777` 在拿不到锁时是静默 `return 0` ——
   07:58 起跑的作业会无声吃掉 08:00 的日常。故 `copilot pick` 在 08:00/22:00 前
   `copilot_blackout_minutes`(25) 内拒绝并说明原因。
5. `SKILL.md` 增补：只允许 `copilot find/pick/again/cancel/status`；
   **绝不索取、接收、复述或写入森空岛/鹰角 token**（用户在聊天里贴了就引导去终端跑 `link`）；
   禁止直接调 `maa copilot`/`maa startup`/`maa_copilot_worker.py`；
   关卡不在本地索引里就直接拒绝、不许猜别名。
6. **对 Hermes 的新增攻击面为零**（见 spike #7）。

---

## 分期

- **C0 — 森空岛 spike（先做，最大未知，一次性代码，不进仓库）**
  用真 token 打通 grant→cred→sign→binding→player/info。必须产出：
  (a) `dId: ""` 能否过数美风控；(b) 真实 `chars[0]` 落地 `equip`/`skills` 的确切语义
  （锁着的模组是 `level:0` 还是不出现？E1 六星的 `skills[]` 是 3 个还是只有已解锁的？）；
  (c) 作业干员名 vs `battle_data.json` 的解析失败率；(d) `10002` 过期路径。
  **(a) 失败就当场评估 OperBox 兜底方案**（只能覆盖 21%，缺口文案退化为「未拥有」）。

- **C1 — 离线领域层**（不碰森空岛/Discord/MAA）
  `maa_copilot.py` 全部函数 + 新测试文件。
  DoD：`search('TO-9')` 20s 内返回 ≥3 个候选、全部可执行（含改写过的展示码作业）、
  排序正确、`ratio=None`；`python3 -m unittest discover -s tests` 全绿。

- **C2 — Discord 闭环，不执行**
  `copilot find/again/cancel/status` + `worker search` + 计划卡第二个下拉 + `deploy.py`。
  `pick` 先做成只写 `last_copilot_run.json` 的 dry-run。
  DoD：下拉选 TO-9 → 1s 内 ephemeral ack → ~20s 内候选卡片出现 → ▶️ 秒 ack →
  **不需要 gateway 重启** → `buttons.json` 里 agent-book 的键完好 → 次日 07:50 清扫后作业按钮仍在。

- **C3 — 真执行（最小可用纵切到此为止）**
  `worker run`：锁→快照→模拟器→子区→`maa startup`→`maa copilot`→截图→`close_everything()`→报告。
  **先抓一份真实 `maa copilot -v` 日志**再写 `copilot_progress_content()`/`copilot_summary()` 的正则 ——
  和 PLAN.md 里 P1 对日常链路的做法一致。
  DoD：一份真实 TO-9 作业端到端跑通，`run.lock` 生效，BlueStacks/ADB 清理干净，报告带截图。

  > C1+C2+C3 **不依赖森空岛也是完整可用的**（降级模式按热度/评分排序并明示
  > 「未接入森空岛，练度未校验」）。这是刻意的：把整期风险和唯一的未知解耦。

- **C4 — 森空岛接入**：把 C0 产品化，练度缓存 + 懒刷新，`ratio`/缺口进卡片。
  DoD：一张卡上同时出现 `✅ 练度满足 8/8` 和真实的 `缺：… 专精2（你专精1）`；
  强制 cred 过期后只重认证一次。

- **C5 — 突袭 + 加固 + 文档**：🔥 按钮与模式回报、blackout、TTL GC、`SKILL.md`、
  DEV_GUIDE 新增不变量与「链路四」、PLAN.md/README。
  DoD：突袭跑报告里回报实际模式；25 小时前的卡片按钮返回「已失效」；
  `deploy.py` 幂等且 `--check` 覆盖全部 7 个脚本。

---

## 验证

```bash
# 单元测试（stdlib，全 mock，无网络/无 MAA/无 Discord）
python3 -m unittest discover -s tests -v

# 关卡索引与解析（离线）
python3 -c "import sys; sys.path.insert(0,'scripts'); import maa_copilot as c; \
  i=c.build_stage_index(); print(len(i['codes']), i['codes']['TO-9'], i['codes']['1-7'])"

# 真实搜索（唯一联网的一步）
python3 -c "import sys; sys.path.insert(0,'scripts'); import maa_copilot as c; \
  [print(x['id'], x['stage_name'], x['title'][:40]) for x in c.search_copilots('act53side_09','TO-9',{})[:5]]"

# 部署 + 自检
python3 deploy.py && python3 deploy.py --check

# 端到端：Discord 里选 TO-9 → 候选卡片 → 点 ▶️ → 子区流式 → 报告带截图
# 回归：确认 08:00 日常刷本未受影响，buttons.json 里 agent-book 键完好
python3 -c "import json,os; d=json.load(open(os.path.expanduser( \
  '~/.hermes/state/agent-book/buttons.json'))); \
  print({p: sum(1 for k in d if k.startswith(p)) for p in \
  ('hermes:btn:agent-book:','hermes:btn:maa:','hermes:btn:maa-copilot:')})"
```

关键测试（新 `tests/test_copilot.py`，仿现有 28 个用例的风格）：
展示码作业被本地改写后可执行、`level_keyword` 退化检测、客户端过滤永不信服务端、
备选组任一满足即满足、专精映射 8/9/10、缺口文案逐字断言、召唤物不计缺口、
排序分桶、`argv` 精确断言（不含 `--use-sanity-potion`）、
`--ignore-requirements` 只在有缺口时出现、作业按钮扛过每早前缀清扫、
森空岛签名黄金向量（冻结 header 顺序与 MD5-of-hex 这两个最易错的点）、
cred 过期只重认证一次、`skland.json` 权限 0600、deploy 不把 token 写进 `config.json`。
