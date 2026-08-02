# 二期（prts.plus 抄作业）代码评审 — 修复清单

**评审对象**：`ee0dcbc`（`~/maa-remote-ph2`，detached HEAD，宿主仓库 `~/maa-remote-spike`）
**基线**：`f558ac1`（`PLAN_PHASE2.md`）
**评审日期**：2026-08-02
**状态**：13 files / +2838 −15；`python3 -m unittest discover -s tests` 64 项全绿

> 注意：这些提交目前**不在** `~/Desktop/maa-remote` 里。修完之后需要把 `df00526`/`bbe1baa`/`ee0dcbc`
> 加上本轮修复一起搬回主仓库。`~/maa-remote-spike` 里还有一条平行的抄作业历史
> （`ada539f` merge PR #3 `feat/copilot-catalog-foundation`），与本实现无关，不要混用。

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
