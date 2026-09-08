> **已归档（2026-09-09）**：二期评审记录，29 条已全部结案。其中与候选卡 / 降级相关的结论已随四期步骤 2 失效，其余仍是不变量的来源。

# 二期（prts.plus 抄作业）代码评审 — 修复清单

> ## ✅ 本文档已结案，无待办事项
>
> 五轮共 29 条问题全部关闭并实测验收通过，最终状态提交于 `098defe`
> （仓库 / worktree / `~/.hermes/scripts` 三处逐字节一致）。
>
> 保留本文档是为了记录**为什么**某些地方要那样写——尤其是 P0-1 的响应形状、
> P0-4 的 lost update、NEW-2 的半分语义，以及 **P1-23 的单向语义**
> （`zone_progress_warning` 有警告 = 基本锁着，没警告 = **不知道**，
> 绝不能拿「没警告」当「已解锁」用）。改到这些位置前先读对应小节，
> 别把已经踩过的坑再踩一遍。**不需要再按本文档做任何修复。**

**基线**：`f558ac1`（`PLAN_PHASE2.md`）　**结案于**：`098defe`（`main`，已推送 origin）

| 轮次 | 评审对象 | 规模 | 测试 | 结论 |
|---|---|---|---|---|
| 第一轮 | `ee0dcbc` | 13 files / +2838 −15 | 64 全绿 | 4 个 P0、5 个 P1、8 个 P2 |
| 第二轮 | `4ab6dc0` | 9 files / +742 −57 | 73 全绿 | 第一轮 17 条全部实测通过；新增 1 个阻断级回归 + 1 个半吊子修复 + 2 个小问题 |
| 第三轮 | `d052461` | 4 files / +238 −20 | 75 全绿 | NEW-1 ~ NEW-4 全部实测通过，无新增问题 |
| 第四轮 | `4e88b28`（解锁预检） | 4 files / +239 −3 | 84 全绿 | 1 个 P0、1 个 P1、4 个 P2 |
| 第五轮 | `add4616` | 4 files / +94 −42 | 85 全绿 | 第四轮 5/6 条通过；NEW-28 文案写坏 + NEW-29 零覆盖 |
| 复验 | `098defe` | 2 files / +67 −14 | 88 全绿 | **NEW-28 / NEW-29 实测通过，无新增问题；结案** |

第三轮验收实测（2026-08-02）：

- `copilot_cancel` → `(True, '❌ 已取消，相关按钮已清理。')`，不再 NameError
- 未收录干员 `ratio = 0.75`（原 0.5）；「未拥有」仍是硬缺口 `(False, 0.0, ['史尔特尔（未拥有）'], [])`，边界未被改坏
- `search_button_ids` 全库无残留；75 项测试 stdout 干净
- 端到端真实网络：`TO-9 → act53side_09`，3 个候选中 `id=98660`（展示码）改写落盘为 `stage_name='act53side_09'`
- `deploy.py --check` → `{"ok": true, "missing": []}`；全库无 `--use-sanity-potion`；`stone = 0` 保持

历史遗留说明：本工程曾在 `~/maa-remote-ph2`（宿主仓库 `~/maa-remote-spike`）的
`ph2-fixes` 分支上开发，已线性搬回 `~/Desktop/maa-remote` 的 `main`，
未混入该宿主仓库中那条平行的抄作业历史（`ada539f` merge PR #3
`feat/copilot-catalog-foundation`）。

**尚未真实验证的功能有两项**（详见文末[复验小节](#仍未真实验证的功能)）：
突袭模式（`--raid raid` 从未真跑过一场），以及解锁预检的「报警」路径
（离线验过文案，但没在 Discord 里端到端点过一次）。

## 已经守住的（不要在修复中破坏）

- 客户端按 `content.stage_name` 过滤，不信服务端 `level_keyword`
- `--formation --batch` 齐全；`--raid normal|raid` 取值经 `maa copilot --help` 核对无误
- 排序只用 `/copilot/query` 的元数据，`/copilot/get` 只取 `content`
- 按钮前缀 `hermes:btn:maa-copilot:`，不被 `maa_plan_card.py` 每早的 `hermes:btn:maa:` 清扫命中
- 零 adapter 补丁、零 `LOCAL_PATCHES.md`、零 gateway 重启
- `stone = 0`；全仓库无 `--use-sanity-potion`
- 导航任务（`Fight times=0` 停编队界面）与接管弹窗 OCR watchdog 是计划外的必要补充，保留

---

# P0 — 阻断级

## P0-1 `/copilot/get` 解包少了一层，43% 的作业点了必炸

**位置**：`scripts/maa_copilot_worker.py:199-209`（`_prepare_op_file`）

**问题**：真实响应是 `{"status_code":…, "data":{…, "content":"…"}}`，顶层没有 `content`。实测：

```
data.get('content')         -> None
data['data'].get('content') -> '{"version":3,"stage_name":"act53side_09",…'
```

`content` 恒为 `None` → 必抛 `作业 <id> 内容获取失败`。这正是用来救那 61 份展示码作业的
`file://` 改写路径，等于整条链路没通。当前真实 TO-9 的 Top-3 里第 2 个就是展示码作业：

```
1. id=98636 stage_name='act53side_09' 需改写=False
2. id=98660 stage_name='TO-9'         需改写=True   ← 点 ▶️ 2 直接失败
3. id=98774 stage_name='act53side_09' 需改写=False
```

`tests/test_copilot.py` 里没有任何一处 mock `/copilot/get`，所以测试全绿也挡不住；
CE-6 那次端到端成功走的是 `maa://` 分支，从未触及改写路径。

**修法**：

```python
    cand_id = int(candidate.get("id") or 0)
    payload = maa_copilot.http_get_json(
        f"{maa_copilot.PRTS_BASE}/copilot/get/{cand_id}", timeout=30
    )
    inner = payload.get("data") if isinstance(payload, dict) else None
    content = inner.get("content") if isinstance(inner, dict) else None
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            content = None
    if not isinstance(content, dict):
        raise RuntimeError(f"作业 {cand_id} 内容获取失败")
    content["stage_name"] = stage_id
```

**必须补的测试**：用真实响应形状 `{"status_code":200,"data":{"id":…,"content":"<json 字符串>"}}`
mock `maa_copilot.http_get_json`，断言 `prepared/<id>.json` 落盘且 `stage_name == stage_id`、
返回的 uri 是 `file://…`。**不要**用扁平的 `{"content": …}` 造 mock——那正是这个 bug 逃过测试的原因。

---

## P0-2 月末 `copilot pick` 抛 ValueError

**位置**：`scripts/maa_action.py`，`_copilot_blackout_reason`

**问题**：`target = target.replace(day=target.day + 1)` 在月末给出 `day=32`：

```
8/31 20:00 -> !! ValueError: day is out of range for month
8/31 09:00 -> !! ValueError: day is out of range for month
8/15 20:00 -> None
```

每个 31 号（以及 30/29/28 号等各月月末）全天点不动执行按钮。

**修法**：改用 `timedelta`。先补 import：

```python
from datetime import datetime, timedelta
```

```python
    for hour in (8, 22):
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now > target:
            target = target + timedelta(days=1)
        delta = (target - now).total_seconds() / 60
        if delta <= minutes:
            return (...)
```

**补测试**：对 8/31 20:00、8/31 09:00、2/28、12/31 断言不抛异常。

---

## P0-3 候选按钮永不清理，`buttons.json` 无界增长

**位置**：`scripts/maa_copilot.py:619-631`（`search_button_ids` / `prune_search_buttons`）

**问题**：三个调用点（`maa_copilot_worker.cmd_search`、`maa_action.copilot_cancel`、
`prune_expired_searches`）全部使用默认 `count=0`，而 `search_button_ids(tok, 0)`
只产出 `cancel` 和 `again`：

```
实际删除:  [':again', ':cancel']
搜索创建:  [':1', ':2', ':3', ':again', ':cancel', ':raid-1', ':raid-2', ':raid-3']
```

`:1/:2/:3/:raid-*` 六个键每次搜索新增、永不删除。这是与 agent-book 全局共享的注册表。

**修法**：不要靠调用方传 `count`，改成按 token 前缀删。同时为 P0-4 预留 `registry` 参数：

```python
def search_button_prefix(token: str) -> str:
    return f"{BUTTON_PREFIX}{token}:"


def prune_search_buttons(token: str, registry: dict[str, Any] | None = None) -> None:
    """Remove ALL registry keys owned by one search (call under state_lock).

    registry given -> mutate it in place, caller persists (avoids lost update);
    registry None  -> read-modify-write BUTTONS_PATH directly.
    """
    prefix = search_button_prefix(token)
    if registry is not None:
        for custom_id in [k for k in registry if k.startswith(prefix)]:
            registry.pop(custom_id)
        return
    prune_button_registry(prefix)
```

前缀带尾部 `:`，所以不会误伤 `hermes:btn:maa-copilot:stage-select`。
`search_button_ids()` 只剩构造用途，可保留。

**补测试**：写入一次搜索的全部 8 个键 + 一个 `hermes:btn:agent-book:x` + `…:stage-select`，
调用 `prune_search_buttons(token)` 后断言 8 个全没、另外两个还在。

---

## P0-4 每早的过期清理是空操作（lost update）

**位置**：`scripts/maa_plan_card.py:436-449`

**问题**：

```
437  registry = load_json(BUTTONS_PATH)          # 内存副本，含 copilot 键
442  pop 掉 hermes:btn:maa: 键                   # 只改内存
447  maa_copilot.prune_expired_searches()        # 独立读盘 → 删 copilot 键 → 写盘
449  atomic_write_json(BUTTONS_PATH, registry)   # 用 437 的陈旧副本覆盖 ← 全部复活
```

过期的 `searches/<token>.json` 确实被 unlink 了，但按钮被写了回来。
DoD 里「25 小时前的卡片按钮返回『已失效』」不成立，且孤儿键永久留存。

**修法**：让 `prune_expired_searches` 在调用方给定的 registry 上原地操作。

`scripts/maa_copilot.py`：

```python
def prune_expired_searches(registry: dict[str, Any] | None = None) -> int:
    """Remove expired search state files and their buttons; returns count.

    Call under state_lock.  Pass the caller's in-memory registry when the
    caller will persist it, otherwise this writes BUTTONS_PATH itself.
    """
    ...
    for token in expired:
        prune_search_buttons(token, registry)
        try:
            (SEARCHES_DIR / f"{token}.json").unlink()
        except FileNotFoundError:
            pass
    return len(expired)
```

`scripts/maa_plan_card.py:447`：

```python
        maa_copilot.prune_expired_searches(registry)
```

**顺带统一 `cmd_search`**（`maa_copilot_worker.py:151-157`）：那里目前是「先删盘、再读盘」，
顺序碰巧是对的，但两次读改写没必要，改成单次：

```python
            with state_lock():
                registry = load_json(BUTTONS_PATH, {})
                if not isinstance(registry, dict):
                    raise RuntimeError("共享按钮登记表格式错误")
                maa_copilot.prune_search_buttons(token, registry)
                registry.update(entries)
                atomic_write_json(BUTTONS_PATH, registry)
```

**补测试**：造一个已过期的 search + 它的 8 个按钮 + 一个 agent-book 键，
跑 `maa_plan_card` 的注册表合并段，断言落盘后 8 个键真的没了、agent-book 键还在。

---

# P1 — 该修但不阻断

## P1-5 `maa_skland.py` 是死代码，C4 的懒刷新没实现

**位置**：全仓库

**问题**：除自身 `main()` 与测试外，无人调用 `refresh_roster()` / `roster_needs_refresh()`；
`config.json` 的 `skland_roster_refresh_days: 7` 从未被读取。worker 只调
`maa_copilot.load_roster()` 读文件。结果：练度只有手动去终端跑才更新，之后无限期变陈旧，
卡片上的「练度数据 N 天前」会一直涨下去。

**修法**：在 `maa_copilot_worker.cmd_search` 里、`load_roster()` 之前插入懒刷新。
搜索本来就是后台任务，不受 20s 预算约束；失败必须降级而不是中断搜索：

```python
            try:
                import maa_skland
                if maa_skland.roster_needs_refresh(config):
                    maa_skland.refresh_roster()
            except Exception as exc:
                print(f"roster refresh skipped: {type(exc).__name__}: {exc}", flush=True)
            roster = maa_copilot.load_roster()
```

**补测试**：`roster_needs_refresh` 返回 True 且 `refresh_roster` 抛异常时，
`cmd_search` 仍然用旧 roster 正常出候选卡片。

---

## P1-6 模组下标错位

**位置**：`scripts/maa_skland.py:183-190`（`_normalize_chars`）

**问题**：

```python
module_levels = [
    int(e.get("level") or 0)
    for e in equips
    if isinstance(e, dict) and not e.get("locked")
]
```

过滤掉 locked 的 equip 会让列表塌缩，而 `check_op_requirements`
（`maa_copilot.py:339`）用 `module_levels[req_module - 1]` 当作游戏内模组序号。
模组 1 锁着、模组 2 解锁时，查「模组 1」读到的是模组 2 的等级——两个方向都可能错
（假通过 / 假缺口）。

**修法**：保留全部 equip 的顺序，locked 记 0：

```python
        module_levels = []
        equips = raw.get("equip") or []
        if isinstance(equips, list):
            module_levels = [
                0 if e.get("locked") else int(e.get("level") or 0)
                for e in equips
                if isinstance(e, dict)
            ]
```

**补测试**：`equip=[{locked:True,level:0},{locked:False,level:3}]` →
`module_levels == [0, 3]`；`req_module=1` 判为未解锁、`req_module=2` 判为满足。

---

## P1-7 异常路径里 `close_everything` 是未绑定局部名

**位置**：`scripts/maa_copilot_worker.py:447-453`（函数内 import）与 `:593`（except 块）

**问题**：函数体内的 `from maa_daily import …` 让这些名字成为 `_run_locked` 的局部变量。
若异常发生在 import 之前——`_prepare_op_file`（P0-1 就必然命中）、`require_executable`、
或第一条 Discord 消息失败——则 except 块里的 `close_everything(config)` 抛
`UnboundLocalError`，被外层 `except Exception: pass` 吞掉。实际影响有限（那些路径模拟器还没开），
但战报里「模拟器与 ADB 已执行清理」是假的。

**修法**：把 import 提到模块顶层。`maa_daily` 只依赖 `maa_common`，无循环导入风险；
worker 是后台进程，import 开销无所谓（`maa_action.py:207` 的函数内 import 是为 20s 预算，
这里不适用）。

```python
import maa_copilot
from maa_daily import (
    capture_screenshot,
    close_everything,
    open_emulator,
    verify_game_installed,
    wait_for_android,
)
from maa_common import (...)
```

并删掉 `_run_locked` 里第 447-453 行的那段函数内 import。

---

## P1-8 `deploy.py --check` 不检查 `ocrbox`

**位置**：`deploy.py`，`deploy_ocrbox()` 与 `--check`

**问题**：没有 `swiftc` 时 `deploy_ocrbox()` 只往 stderr 打一行就跳过，`--check` 照样报 `ok`；
worker 里 `if not ocrbox.is_file(): continue` 静默禁用 watchdog。结果是每次开着代理指挥的关卡
都会以一个看不懂的 MaaCore 错误失败，而所有自检都是绿的。

**修法**：把 `ocrbox` 纳入 `--check`（本项目本来就只跑 macOS）：

```python
        checks = [
            HERMES_HOME / "scripts" / name
            for name in (
                "maa_common.py", "maa_plan_card.py", "maa_daily.py", "maa_action.py",
                "maa_copilot.py", "maa_copilot_worker.py", "maa_skland.py",
                "ocrbox",
            )
        ]
```

同时把 `deploy_ocrbox()` 的失败提示写得更硬（明确指出「代理指挥关卡将全部失败」），
或在编译失败时 `changed.append` 一条 warning 供部署输出可见。

---

## P1-9 抄作业下拉的第一项必然报错

**位置**：`scripts/maa_plan_card.py`，`_copilot_stage_select`

**问题**：直接复用了 `_stage_choices`，其首项是 `("上次作战关卡", "", …)`，stage 为空串 →
`copilot find ""` → 「关卡 不在本地关卡索引里」。刷本可以「沿用上次关卡」，抄作业必须有具体关卡。

**修法**：

```python
    for index, (label, stage, description) in enumerate(_stage_choices(plan, config)):
        if not stage:
            continue
        value = f"copilot-choice:{index}"
        ...
```

若 `options` 为空则整行不要渲染（Discord 不接受 0 选项的 select）——
在 `build_components` 里判断 `if copilot_select["options"]:` 再加入那一行。

---

# P2 — 小问题

| # | 位置 | 问题 | 修法 |
|---|---|---|---|
| 10 | `maa_copilot.py:584` | `candidates[: max(3, limit)]` 忽略 `limit<3` | 改 `candidates[: max(1, limit)]` |
| 11 | `maa_copilot.py:511` | 退化扫描连续 2 页无新增就 break，作业不在最新 ~200 条里的关卡会假性返回空 | `empty_pages >= 4`；当期活动无影响，冷门关更稳 |
| 12 | `maa_copilot_worker.py:588` | `os._exit` 是对「解释器滞留 10 分钟」的补丁，根因未查明 | 补丁本身**安全**（`run_lock` 是纯 flock，进程死即释放），保留；但 `discord_request`/`discord_upload` 都有超时，不是它们。在 `os._exit` 前加一行 `threading.enumerate()` / `len(gc.get_objects())` 之类的诊断输出，下次复现时能定位 |
| 13 | `maa_copilot_worker.py:514` | `for index in range(...)` 覆盖候选编号参数 `index` | 改名 `line_index`（当前之后未再使用，属待爆的坑） |
| 14 | `maa_copilot.py:270-272` | 本地 `battle_data.json` 查不到的干员名一律记「未拥有」，资源包滞后于新干员时报假硬缺口 | 改为软缺口：`return None, False` 之外再区分一种「未知」，文案「{name}（本地资源未收录，无法校验）」，计入 `soft_gaps` 而非 `hard_gaps` |
| 15 | `state/config.json` | 新增硬编码 `/Users/wadorudi/…` 绝对路径，而 `DEFAULT_RESOURCE_DIR` 本来就用 `Path.home()` | 删掉 `maa_resource_dir` 这一项 |
| 16 | `maa_skland.py:292` | 测试跑完往 stdout 漏 `✅ 森空岛接入成功…` | 测试里用 `contextlib.redirect_stdout` 包住（这条 print 是给终端用户看的，保留） |
| 17 | `maa_copilot.py:45,46,106` | 死常量 `USER_AGENT`、`MAX_HTTP_BYTES`、`raid_variants` | 删除 |

---

# 建议的修复顺序

1. **P0-1**（含真实响应形状的测试）——不修就没有 43% 的作业
2. **P0-2**——一行，立刻消除月末全天不可用
3. **P0-3 + P0-4**——一起改，共用新的 `prune_search_buttons(token, registry)` 签名
4. **P1-7**——一行 import 上提，让 P0-1 修复后的错误路径能真正清理
5. **P1-5、P1-6**——练度链路的正确性
6. **P1-8、P1-9**——可观测性与 UX
7. **P2**——顺手

# 验收

```bash
python3 -m unittest discover -s tests -v      # 应 > 64 项，全绿，stdout 干净

# P0-1：确认展示码作业能落盘改写
python3 -c "import sys; sys.path.insert(0,'scripts'); import maa_copilot as c; \
  i=c.load_stage_index({}); sid,code=c.resolve_copilot_stage('TO-9',i); \
  [print(x['id'], x['stage_name'], c.execution_uri(x,sid)) \
   for x in c.search_copilots(sid,code,{},None,3)]"
# 然后在 Discord 里点中那个 需改写=True 的候选，必须真的跑起来而不是「内容获取失败」

# P0-2
python3 -c "import sys; sys.path.insert(0,'scripts'); from datetime import datetime; \
  import maa_action, maa_common; \
  maa_action.now_beijing=lambda: datetime(2026,8,31,20,0,tzinfo=maa_common.BEIJING); \
  print(maa_action._copilot_blackout_reason({'copilot_blackout_minutes':25}))"

# P0-3/P0-4：跑一次搜索 → 手动把 expires_at 改成过去 → 跑 maa_plan_card → 复查
python3 -c "import json,os; d=json.load(open(os.path.expanduser( \
  '~/.hermes/state/agent-book/buttons.json'))); \
  print({p: sum(1 for k in d if k.startswith(p)) for p in \
  ('hermes:btn:agent-book:','hermes:btn:maa:','hermes:btn:maa-copilot:')})"
# 期望：agent-book 数量不变；maa-copilot 只剩 stage-select + 当前有效搜索的键

python3 deploy.py && python3 deploy.py --check   # missing 必须为空（含 ocrbox）
```

回归确认：08:00 / 22:00 的日常刷本不受影响，`buttons.json` 里 agent-book 的键完好。

---
---

# 第二轮评审（对象 `4ab6dc0`）

## 第一轮 17 条的验收结果：全部通过

逐条实测（不是只看 diff），结论如下，**这部分不需要再动**：

| 编号 | 验证方式与结果 |
|---|---|
| P0-1 | 真实网络调 `/copilot/get/98660`：落盘 `prepared/98660.json`，`stage_name` 已改写为 `act53side_09`，返回 `file://…` ✅ |
| P0-2 | 8/31 20:00、8/31 09:00、8/28 23:00 均返回 `None`，不再抛 ✅ |
| P0-3 | 一次搜索的 8 个键全删；`stage-select`、`agent-book:x`、`maa:run-now` 三类完好 ✅ |
| P0-4 | 模拟计划卡注册表合并段：落盘后只剩 `agent-book:x` + 新键，过期 search 文件也已 unlink ✅ |
| P1-5 | 已接入 `cmd_search`；`refresh_roster` 抛异常时降级用旧 roster、搜索继续 ✅ |
| P1-6 | `[{locked:True,level:0},{locked:False,level:3}]` → `[0, 3]` ✅ |
| P1-7 | 顶层导入，无循环依赖，worker import 0.02s ✅ |
| P1-8 | `ocrbox` 已入 `--check`；本机 `{"ok": true, "missing": []}` ✅ |
| P1-9 | 6 个选项无一为空；卡片 3 行、第二行为 `maa-copilot:stage-select` ✅ |
| P2-10/11/13/15/16/17 | 均已处理 ✅ |

新测试里的 `test_prepare_op_file_rejects_flat_payload` 是正确的回归护栏，保留。

---

## NEW-1（阻断级，本轮引入的回归）`copilot_cancel` 抛 NameError，❌ 取消按钮全死

**位置**：`scripts/maa_action.py:336,339`

**问题**：P0-3/P0-4 的修改在 `copilot_cancel` 里新用了 `BUTTONS_PATH`，但没把它加进
`from maa_common import (...)` 列表：

```
copilot_cancel -> !! NameError: name 'BUTTONS_PATH' is not defined
maa_action 里有 BUTTONS_PATH: False
```

`tests/test_copilot.py` 里 `copilot_cancel` 零覆盖，所以 73 项全绿也没挡住。

**修法**：在 `maa_action.py` 的 `from maa_common import (` 列表里补一行，按现有字母序排在 `BEIJING` 之后：

```python
from maa_common import (
    BEIJING,
    BUTTONS_PATH,
    CONFIG_PATH,
    ...
)
```

**必须补的测试**：`copilot_cancel` 的正常路径——造一个 search state + 它的 8 个按钮，
调用后断言 (a) 不抛异常、(b) 返回 `(True, "❌ 已取消，相关按钮已清理。")`、
(c) `buttons.json` 里该 token 的键全没了、agent-book 的键还在。
这是本轮唯一一个「改了代码但没有任何测试碰过」的函数，补上它。

---

## NEW-2（半吊子修复）P2-14 的 0.5 分被丢弃

**位置**：`scripts/maa_copilot.py:311-314`（`check_op_requirements`）

**问题**：未收录干员返回 `(False, 0.5, [], [soft])`，但调用方
（`evaluate_candidate`，`maa_copilot.py:375`）是：

```python
total_score += score if ok else 0.0     # ok=False → 0.5 被扔掉
```

实测（两个位置，一个满足、一个未收录）：

```
ratio = 0.5      ← 期望 0.75
hard gaps = []
soft gaps = ['某个本地没有的新干员（本地资源未收录，无法校验）']
```

写下的 `0.5` 是死值。而同属软约束的模组走的是 `return True, 0.5 if soft else 1.0` ——
两处语义不一致：模组给半分，未收录给零分。P2-14 的本意是「计入 soft_gaps 而非 hard_gaps」，
即半分。

**修法**：

```python
    if char is None:
        if unknown:
            return True, 0.5, [], [f"{name}（本地资源未收录，无法校验）"]
        return False, 0.0, [f"{name}（未拥有）"], []
```

改完 ratio 仍 `< 1.0`，所以 `--ignore-requirements` 照常会带上，行为是安全的。

**补测试**：现有的 `test_unknown_operator_is_soft_gap_not_hard` 只断言了 gap 的分类、
没断言分数——这正是它漏掉的原因。加一条 ratio 断言：
两个位置、一个满足、一个未收录 → `ratio == 0.75`。

---

## NEW-3（小）`search_button_ids()` 成了死代码

**位置**：`scripts/maa_copilot.py:625`

P0-3 改用前缀删除后，`search_button_ids` 已无任何调用方（`scripts/` 和 `tests/` 全库为零）。
`build_candidate_card` 是自己内联拼 id 的。**删掉**。

---

## NEW-4（小）测试 stdout 又漏了一行

**位置**：`tests/test_copilot.py` 中 P1-5 的新用例

原来的 `✅ 森空岛接入成功…` 已修掉，但新测试又漏出：

```
............................roster refresh skipped: RuntimeError: skland down
.............................................
```

`maa_copilot_worker.py` 里那条 print 是有用的运行期诊断，保留；在测试侧用
`contextlib.redirect_stdout(io.StringIO())` 包住即可。

---

## 第二轮验收

```bash
python3 -m unittest discover -s tests -v    # 应 > 73 项，全绿，stdout 干净无杂行

# NEW-1
python3 -c "import sys,tempfile,pathlib,json; sys.path.insert(0,'scripts'); \
  import maa_action, maa_copilot as c; \
  t=pathlib.Path(tempfile.mkdtemp()); c.SEARCHES_DIR=t; tok='abcdef012345'; \
  (t/f'{tok}.json').write_text(json.dumps({'token':tok,'status':'done','stage':'TO-9','candidates':[]})); \
  print(maa_action.copilot_cancel(tok))"
# 期望打印 (True, '❌ 已取消，相关按钮已清理。')，不得抛 NameError

# NEW-2
python3 -c "import sys; sys.path.insert(0,'scripts'); import maa_copilot as c; \
  roster={'chars':{'char_002_amiya':{'name':'阿米娅','level':90,'elite':2,'main_skill_lvl':7,'skills':[3,3,3],'module_levels':[3]}}}; \
  item={'id':1,'content':{'stage_name':'x','opers':[{'name':'阿米娅','skill':1,'requirements':{}},{'name':'本地没有的新干员','skill':1,'requirements':{}}]}}; \
  print('ratio =', c.evaluate_candidate(item, roster, {})['ratio'])"
# 期望 0.75

# NEW-3
grep -rn "search_button_ids" scripts/ tests/    # 期望无输出
```

回归确认同第一轮：08:00 / 22:00 的日常刷本不受影响，`buttons.json` 里 agent-book 的键完好。

---

# 第四轮：`4e88b28` — 解锁预检

**评审对象**：`4e88b28`（4 files / +239 −3）　**测试**：84 全绿　
**结论**：1 个 P0、1 个 P1、4 个 P2。

功能方向是对的，森空岛数据选得也对（`activity[].zones[]` 确实是唯一可得的进度源），
`build_candidate_card` 的参数带默认值、`refresh_roster` 展平回归测试、旧 roster
无 `activity_progress` 时静默降级——这几点都做得干净。问题全部集中在**一个隐含前提**上：

> 代码假设 `stage_id` 里的序号（`act53side_09` → 9）和 `clearedStage` 是同一把尺子。
> **它们不是。** `seq` 数的是「第几个战斗关」，`clearedStage` 数的是「zone 里通了几关，含剧情关」。

下面所有问题都是这条前提的推论。**先读这段再动手，否则会按错误的方向修。**

## 先明确一件事：现在的判据其实是「单向可靠」的

这点很重要，别在修的时候把它改坏。设 zone 内共通关 `cleared` 关（含剧情关），
要通关第 `seq` 个战斗关，至少得先通关 `seq` 关。所以：

- `cleared < seq` ⟹ 第 `seq` 关**一定没通** ⟹ 突袭一定没解锁。**逻辑成立。**
- `cleared < seq-1` ⟹ 第 `seq-1` 关一定没通 ⟹ 第 `seq` 关一定没解锁。**逻辑成立。**
- `cleared >= seq` ⟹ **什么都推不出来**（必要条件不是充分条件）。

也就是说：**它发出的警告不会错，但它该发的时候经常不发。**
代码注释里写的 `cleared >= seq -> both plausible`（都有可能）措辞是错的，
正确的措辞是 `cannot conclude`（推不出来）。这不是文字游戏——写成「都有可能」，
下一个人就会试图去「优化」那条静默分支，然后引入假警告。

---

## P0-22（严重）多 zone 时拿全局序号比 zone 局部计数，会输出自相矛盾的警告

**位置**：`scripts/maa_copilot.py` `zone_progress_warning()` 的 cumulative 循环

```python
cumulative += total
if seq <= cumulative:
    target = zone
    break
...
cleared = int(target.get("clearedStage") or 0)
if cleared >= seq: ...
```

定位 zone 用的是**累计** total，但取出 `cleared` 之后又拿它和**未减偏移的全局** `seq` 比。
一旦命中的不是第一个 zone，这个比较就没有意义了。

**实测（act17side 的真实 zone 结构，把进度调成全通）**：

```
zone1 6/6 + zone2 3/3 + zone3 2/2 —— 每个 zone 都 100% 通关
  act17side_07: ⚠️ act17side_zone2 仅通关 3/3：X-7 大概率未解锁（突袭更不可能）
  act17side_08: ⚠️ act17side_zone2 仅通关 3/3：X-8 大概率未解锁（突袭更不可能）
  act17side_10: ⚠️ act17side_zone3 仅通关 2/2：X-10 大概率未解锁（突袭更不可能）
```

「仅通关 3/3」——3/3 是满的，还说未解锁。这是**自相矛盾的输出**，
比不发警告更糟：用户会不信任整个提示。

**不是假设的边界**：真实 roster 里 `act17side` 就是这个形状
（zone1 total=6，但 Tile-Pos 里有 10 个编号关），序号 7~10 现在就走 zone2/zone3。
只是用户当前 zone1 是 1/6，恰好让错误结论和正确结论撞在了一起，所以没暴露。

**修法（推荐，比修偏移更简单也更正确）**：删掉 cumulative 循环，
**只认第一个 `totalStage > 0` 的 zone；序号超出它就静默**。

```python
    target = next((z for z in zones
                   if isinstance(z, dict) and int(z.get("totalStage") or 0) > 0), None)
    if target is None:
        return None
    total = int(target.get("totalStage") or 0)
    if seq > total:
        # Numbered stages spill past the first zone (act17side).  We cannot
        # map a global stage number onto a zone-local cleared count, and a
        # wrong "not unlocked" on a fully cleared zone is worse than silence.
        return None
```

为什么不推荐「减去 zone 偏移」那个修法：`totalStage` 里混着剧情关（见 P1-23），
累计出来的边界跟战斗关序号本来就对不齐，减偏移只是把一个错误换成另一个错误。
静默是这里唯一诚实的选项。实测 60 个活动里只有 `act17side` 1 个会因此变静默，
其余 59 个行为不变。

## P1-23 `clearedStage` 含剧情关而 `seq` 不含，导致警告在多数活动里整片静默

**位置**：同上，`cleared >= seq` 的阈值判断。

`totalStage` / `clearedStage` 把**剧情关也算进去**了，而 `seq` 只数战斗关。
拿真实 roster 和本地 Tile-Pos 对了一遍 60 个活动：

```
60 个活动里，zone1.totalStage == 战斗关数 的只有 2 个
其余 58 个 totalStage 都更大，多出来的是剧情关（+1 到 +8 个）
  act53side  9 战斗关 → zone1 total 13（+4 剧情关）
  act44side  8 战斗关 → zone1 total 16（+8 剧情关）
  act25side 10 战斗关 → zone1 total 18（+8 剧情关）
```

剧情关把 `cleared` 顶高，于是 `cleared >= seq` 提前成立，警告提前闭嘴。
**实测用户自己的 roster**，这几个「打了一半」的活动里，警告对**所有**战斗关都是静默：

```
act21side zone1 10/15（10 战斗关 + 5 剧情关）→ 01~10 全部静默，但还剩 5 关没通
act23side zone1 11/13（ 9 战斗关 + 4 剧情关）→ 01~09 全部静默，但还剩 2 关没通
act19side zone1  9/13（ 8 战斗关 + 5 剧情关）→ 01~08 全部静默，但还剩 4 关没通
act38side zone1 15/18（10 战斗关 + 8 剧情关）→ 01~10 全部静默，但还剩 3 关没通
```

这正是这个功能存在的意义所在的那类场景（打了一半、不确定解锁到哪），
而且「打了一半的活动」本来就是抄作业最高频的对象。

**这不是 bug，是能力上限**——按上面那节的推理，`cleared >= seq` 本来就推不出结论，
静默是正确的。想真正判准需要单关粒度，森空岛不给。所以**不要试图用启发式去猜**
（比如按比例估算剧情关位置），那会引入假警告，得不偿失。

**修法：改文档和措辞，不改判据。**

1. `zone_progress_warning()` 的 docstring 把 `cleared >= seq -> both plausible`
   改成 `-> cannot conclude (clearedStage counts 剧情关, seq does not)`，
   并写明「本函数只在能确定锁着时发声，静默 ≠ 已解锁」。
2. `REVIEW_PHASE2.md` / `DEV_GUIDE.md` 记一条：这个预检是**单向**的，
   有警告 = 基本锁着；没警告 = 不知道。不要在别处把它当成「已解锁」的依据
   （尤其别拿它去 gate `copilot pick` 的按钮）。

## P2-24 进度按天变，roster 却 7 天才懒刷新一次，警告不带时效标注

`skland_roster_refresh_days: 7`，且只有 `cmd_search` 会触发刷新。练度是慢变量，
7 天没问题；**但解锁进度在活动期间是天天变的**，而这个警告恰恰只对当期活动生效。

后果是**假警告**：用户今天推完 TO-1~TO-9，明天来抄 TO-9，roster 还是几天前的快照，
卡片上照样写「TO-9 大概率未解锁」。按钮还能点（这点设计是对的），但提示就成了噪音。

叠加上上一轮已经确认的一件事：森空岛返回的是**最后一次登出游戏时的快照**
（`lastOnlineTs`），不是实时。所以「刚打完就搜」这个最常见的路径必然是过期的。

**修法（二选一，我倾向 a）**：

a. 警告行带上时效，让用户自己判断：
```python
    age = roster_age_hint(roster)   # 已有函数，返回「（练度数据 N 天前）」或 ""
    suffix = f"（数据为 {age} 的快照，如已推进请忽略）" if age else ""
```
   顺带把 `roster_age_hint` 的措辞从「练度数据」改成「森空岛数据」——
   现在这份快照已经不只是练度了。

b. 搜索的关卡命中 `activity_progress` 里的当期活动时，把刷新阈值降到 1 天。
   代价是当期活动的每次搜索几乎都会多一次森空岛往返（含一次 401 重认证）。

## P2-25 `int()` 可抛异常，逃出 `fetch_roster` 的兜底，整份 roster 拉取失败

**位置**：`scripts/maa_skland.py` `_normalize_activity_progress()`

```python
"clearedStage": int(zone.get("clearedStage") or 0),
```

`fetch_roster` 的重试兜底只 `except RuntimeError`。实测：

```
clearedStage=[1]     -> RAISES TypeError
clearedStage='abc'   -> RAISES ValueError
```

这两个都会穿过 `fetch_roster` 一路冒到 `cmd_search` 的 `except Exception`，
打印 `roster refresh skipped` ——**结果是为了一个可选的提示，把 269 个干员的练度数据整个丢掉**，
候选卡片降级成「未接入森空岛」。新增的可选功能不该有能力搞挂主数据。

**修法**：加个 `_safe_int`，并给整个 activity 解析包一层兜底。

```python
def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
```
`fetch_roster` 里改成：
```python
        try:
            progress = _normalize_activity_progress(data.get("activity"))
        except Exception:
            progress = {}       # optional hint data must never break the roster
        return {"chars": _normalize_chars(chars, battle), "activity_progress": progress}
```

## P2-26 两处测试不判别，等于没测

又是前两轮那个模式（NEW-1 / NEW-2 同因）：**断言只覆盖了改动的一半**。

1. `test_zone_just_before_stage_warns_raid_only`：
   ```python
   self.assertNotIn("大概率未解锁\n", warning)
   ```
   `warning` 是**单行、不含换行**的字符串，这个断言恒真，永远不会失败。
   想表达的应该是「不能误报成普通关也锁了」，改成正向断言：
   ```python
   self.assertIn("的突袭大概率未解锁", warning)
   self.assertNotIn("（突袭更不可能）", warning)   # 这才是另一档的独有文案
   ```

2. `test_stage_in_second_zone_maps_by_cumulative_total`：
   用的数据是 zone1 6/6、zone2 0/7、seq 9。**在正确实现和 P0-22 的错误实现下，
   输出逐字相同**（都是 `act53side_zone2 仅通关 0/7 … 大概率未解锁`），
   所以它没有测到它名字里声称测的那件事。
   按 P0-22 的修法这个用例应该整个改写成「序号溢出第一个 zone → 返回 None」。

## P2-27 docstring 里 EX/ST 那句和代码对不上

```python
    if target is None:
        # seq beyond every zone's total (EX/ST stages) -> no hint
```
EX/ST 关的 `stage_id` 形如 `act5d0_ex01`、`act53side_sp01`，
**连 `^([A-Za-z0-9]+?)_(\d+)$` 都匹配不上**，在函数开头就 `return None` 了，
根本走不到这个分支。注释按 P0-22 的修法一起重写。

---

## 第四轮验证

```bash
python3 -m unittest discover -s tests -v        # 应 ≥ 84 项全绿

# P0-22：zone 全通仍报未解锁（修好后应全部 None）
python3 -c "import sys; sys.path.insert(0,'scripts'); import maa_copilot as c; \
  r={'chars':{},'activity_progress':{'act17side':[ \
    {'zoneId':'act17side_zone1','clearedStage':6,'totalStage':6}, \
    {'zoneId':'act17side_zone2','clearedStage':3,'totalStage':3}]}}; \
  print([c.zone_progress_warning(f'X-{s}', f'act17side_{s:02d}', r) for s in (7,8)])"
# 期望 [None, None]，现在会打出两条「仅通关 3/3：…大概率未解锁」

# P0-22 回归：用户当前真实场景必须仍然报警
python3 -c "import sys,json,os; sys.path.insert(0,'scripts'); import maa_copilot as c; \
  r=json.load(open(os.path.expanduser('~/.hermes/state/maa/copilot/roster.json'))); \
  print(c.zone_progress_warning('TO-9','act53side_09',r))"
# 期望仍是「⚠️ act53side_zone1 仅通关 1/13：TO-9 大概率未解锁（突袭更不可能），点了可能空跑。」

# P2-25：脏数据不得搞挂 roster
python3 -c "import sys; sys.path.insert(0,'scripts'); import maa_skland as s; \
  print(s._normalize_activity_progress([{'actId':'a','zones':[ \
    {'zoneId':'z','clearedStage':[1],'totalStage':'abc'}]}]))"
# 期望正常返回（clearedStage/totalStage 归 0），不得抛 TypeError/ValueError
```

回归确认同前三轮：08:00 / 22:00 的日常刷本不受影响，`buttons.json` 里 agent-book 的键完好。

---

# 第五轮：`add4616` — 复验与 2 条遗留

**评审对象**：`add4616`（4 files / +94 −42）　**测试**：85 全绿　
**结论**：第四轮 6 条中 5 条实测通过；P2-24 的实现本身写坏了，另发现 1 条测试盲区。

## 先说好消息：P0-22 的新判据在多 zone 下是可证成立的

改成「只认第一个非空 zone，序号超出即静默」之后，我担心的是另一种情况：
`seq <= zone1.total` 但那一关其实在 zone2 里（zone1 被剧情关撑大了）。
推了一遍，这种情况下判据**依然成立**：

> 要通关 zone2 里的关，必须先打完整个 zone1，即 `cleared₁ == total₁ >= seq`。
> 逆否：`cleared₁ < seq <= total₁` ⟹ zone1 没打完 ⟹ zone2 根本没解锁 ⟹ 第 seq 关必然没通。

所以「有警告 = 基本锁着」这个方向在跨 zone 时也没有漏洞。**这不是碰巧对的，别再改回累计映射。**

实测复验（全部通过）：

```
act17side zone1 6/6 + zone2 3/3（全通）→ X-7/X-8/X-10 全部 None   ← P0-22 已修
用户真实 roster TO-9 → ⚠️ act53side_zone1 仅通关 1/13：…            ← 回归未破
_normalize_activity_progress 脏数据 [1] / 'abc' / None / 1.9 → 全部归 0，不抛  ← P2-25 已修
docstring 已改为 cannot conclude；EX/ST 注释已挪到正则那一行         ← P1-23 / P2-27 已修
DEV_GUIDE 不变量 #6 已记「有警告=基本锁着，没警告=不知道」           ← P1-23 已修
两处废断言已改正向，cumulative 用例已改写为溢出静默                  ← P2-26 已修
```

## NEW-28（P2）时效标注括号套括号，且同一张卡上重复两遍

**位置**：`scripts/maa_copilot.py` `zone_progress_warning()`

```python
    age = roster_age_hint(roster)
    suffix = f"（{age}，如已推进请忽略）" if age else ""
```

`roster_age_hint()` 的返回值**自带全角括号**（`（森空岛数据 4 天前）`），外面再包一层就成了：

```
⚠️ act53side_zone1 仅通关 1/13：TO-9 大概率未解锁（突袭更不可能），点了可能空跑。（（森空岛数据 4 天前），如已推进请忽略）
```

而且卡片 description 已经在上一行单独打过一次同样的年龄，整张卡长这样：

```
🔍 TO-9 的作业候选：
1. **作业 1** — … · 练度未知
（森空岛数据 4 天前）                                    ← roster_hint 行
⚠️ … 点了可能空跑。（（森空岛数据 4 天前），如已推进请忽略）  ← 又一遍，还套了括号
```

**修法**：把「算天数」和「拼文案」拆开，两个调用点各拼各的。

```python
def roster_age_days(roster: dict[str, Any]) -> int | None:
    """Days since the skland snapshot, or None when unknown."""
    try:
        age = now_beijing() - datetime.fromisoformat(str(roster.get("fetched_at") or ""))
    except (ValueError, TypeError):
        return None
    return age.days


def roster_age_hint(roster: dict[str, Any]) -> str:
    days = roster_age_days(roster)
    return f"（森空岛数据 {days} 天前）" if days and days > 0 else ""
```

`zone_progress_warning()` 里改成：

```python
    days = roster_age_days(roster)
    suffix = f"（森空岛数据 {days} 天前，如已推进请忽略）" if days and days > 0 else ""
```

顺带修掉一个原有隐患：旧 `roster_age_hint` 只 `except ValueError`，
`fetched_at` 若是 naive 时间戳，`now_beijing() - naive` 抛的是 **TypeError**，会一路冒出去。
上面的写法把两个都接住了。

**重复问题**：warning 行的标注是必要的（`roster_hint` 只在 `roster_ok` 为真时才加，
而 warning 在 roster 有 `activity_progress` 时就会出现，两者不总是同时在场），
所以**保留 warning 里的标注，不要删**。如果嫌卡片啰嗦，就把 `roster_hint` 那一行去掉，
别去掉 warning 里的——警告脱离时效就会被当成当前事实。

## NEW-29（P2）时效标注这条路径 85 项测试一项都没覆盖到

**位置**：`tests/test_copilot.py` `ZoneProgressTests._roster_with_progress`

```python
    def _roster_with_progress(self, zones):
        return {"chars": {}, "activity_progress": {"act53side": zones}}
```

**没有 `fetched_at`** → `roster_age_hint` 走 `fromisoformat("")` 抛 ValueError → 返回 `""`
→ `suffix` 恒为 `""`。于是 `ZoneProgressTests` 里每一个用例走的都是「无标注」分支，
P2-24 新加的那段格式化代码**一行都没被执行过**，所以括号套括号能带着 85 全绿进主干。

这已经是第四次同一个模式了（NEW-1 没测 `copilot_cancel`、NEW-2 只断言分类不断言分数、
P2-26 两处断言恒真、这次是新分支完全没进测试）。**改了用户可见文案，就必须有一条逐字断言。**

**修法**：helper 加可选 `fetched_at`，并补一条逐字断言。

```python
    def _roster_with_progress(self, zones, fetched_at=None):
        roster = {"chars": {}, "activity_progress": {"act53side": zones}}
        if fetched_at:
            roster["fetched_at"] = fetched_at
        return roster

    def test_warning_stamps_snapshot_age_without_nesting_parens(self) -> None:
        # NEW-28: roster_age_hint() already carries its own （）— re-wrapping
        # produced （（森空岛数据 4 天前），如已推进请忽略）.
        stale = (now_beijing() - timedelta(days=4)).isoformat()
        r = self._roster_with_progress(
            [{"zoneId": "act53side_zone1", "clearedStage": 1, "totalStage": 13}],
            fetched_at=stale,
        )
        warning = maa_copilot.zone_progress_warning("TO-9", "act53side_09", r)
        self.assertTrue(warning.endswith("（森空岛数据 4 天前，如已推进请忽略）"))
        self.assertNotIn("））", warning)
        self.assertNotIn("（（", warning)

    def test_fresh_roster_omits_age_stamp(self) -> None:
        r = self._roster_with_progress(
            [{"zoneId": "act53side_zone1", "clearedStage": 1, "totalStage": 13}],
            fetched_at=now_beijing().isoformat(),
        )
        self.assertTrue(
            maa_copilot.zone_progress_warning("TO-9", "act53side_09", r)
            .endswith("点了可能空跑。")
        )
```

## 可选加固（不做也行）

`zone_progress_warning()` 里读 zone 用的是裸 `int(z.get("totalStage") or 0)`。
数据来自本进程自己写的 `roster.json`（`_safe_int` 已归一化过），所以现在不会炸；
但这个函数跑在 `build_candidate_card` 里，真抛异常会**整张候选卡片发不出去**。
想彻底防住就把 `_safe_int` 提到 `maa_common.py` 两边共用。优先级很低。

---

## 第五轮验证

```bash
python3 -m unittest discover -s tests -v        # 应 ≥ 87 项全绿

# NEW-28：括号不得嵌套，年龄只出现一次
python3 -c "import sys,json,os; sys.path.insert(0,'scripts'); import maa_copilot as c; \
  r=json.load(open(os.path.expanduser('~/.hermes/state/maa/copilot/roster.json'))); \
  r['fetched_at']='2026-07-29T10:00:00+08:00'; \
  w=c.zone_progress_warning('TO-9','act53side_09',r); print(w); \
  assert '（（' not in w and '））' not in w, '括号仍然嵌套'"
# 期望结尾是 …点了可能空跑。（森空岛数据 4 天前，如已推进请忽略）

# NEW-28 回归：新鲜 roster 不得出现标注
python3 -c "import sys,json,os; sys.path.insert(0,'scripts'); import maa_copilot as c; \
  r=json.load(open(os.path.expanduser('~/.hermes/state/maa/copilot/roster.json'))); \
  print(repr(c.zone_progress_warning('TO-9','act53side_09',r)))"
# 期望以「点了可能空跑。」结尾，无时效后缀

# 第四轮判据不得被改坏
python3 -c "import sys; sys.path.insert(0,'scripts'); import maa_copilot as c; \
  r={'chars':{},'activity_progress':{'act17side':[ \
    {'zoneId':'act17side_zone1','clearedStage':6,'totalStage':6}, \
    {'zoneId':'act17side_zone2','clearedStage':3,'totalStage':3}]}}; \
  assert [c.zone_progress_warning(f'X-{s}', f'act17side_{s:02d}', r) for s in (7,8,10)] == [None]*3"
```

回归确认同前四轮：08:00 / 22:00 的日常刷本不受影响，`buttons.json` 里 agent-book 的键完好。

---

# 复验：`098defe` — 结案

**评审对象**：`098defe`（2 files / +67 −14）　**测试**：88 全绿　**结论**：无新增问题。

```
时效标注（stale roster 4 天）→ …点了可能空跑。（森空岛数据 4 天前，如已推进请忽略）
                               无嵌套括号                                  ← NEW-28 已修
用户真实 roster（当天）        → …点了可能空跑。   无时效后缀               ← 回归未破
act17side zone1 6/6 + zone2 3/3 → X-7/X-8/X-10 全 None                     ← P0-22 未被改坏
88 项测试全绿，新增 3 条覆盖 suffix 分支                                    ← NEW-29 已修
```

`roster_age_days()` 的边界全测了一遍，**全部返回 None/"" 且不抛**：
缺 `fetched_at`、空串、`'not-a-date'`、naive 时间戳、非字符串 `12345`、
未来时间戳（`days=-3`）、当天（`days=0`）。`roster_age_hint()` 的对外语义与拆分前逐字一致
（`days <= 0` 或无法解析 → `""`），唯一调用方 `cmd_search` 不受影响。

三处同步已核：`scripts/` 下 7 个脚本与 `~/.hermes/scripts/` **逐字节一致**，
worktree `~/maa-remote-ph2` 同在 `098defe`。
不变量保持：全库无 `--use-sanity-potion`、`stone = 0`、`deploy.py --check` → `{"ok": true, "missing": []}`。

**刻意保留的一处冗余**：卡片上年龄会出现两次——`roster_hint` 单独一行、
warning 行尾再带一次。这是[第五轮 NEW-28](#new-28p2时效标注括号套括号且同一张卡上重复两遍年龄)
里说明过的：`roster_hint` 只在 `roster_ok` 为真时才加，warning 却可能在没有它的情况下出现，
所以 warning 必须自带时效。嫌啰嗦就去掉 `roster_hint` 那一行，**不要去掉 warning 里的**。

## 仍未真实验证的功能

1. **突袭模式**（`--raid raid`）——取值已核对 `maa copilot --help` 无误，但从未真跑过一场。
2. **解锁预检的「报警」路径**——离线用真实 roster 验证过文案，但没在 Discord 里
   端到端点过一次「搜一个锁着的关 → 卡片带警告」。
