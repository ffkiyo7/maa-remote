#!/usr/bin/env python3
"""素材权重公式（四期步骤 3）：外部掉率 / 排期 / 物品价值的抓取层 + 打分纯函数。

模块分两层，**故意分开**：

- **抓取层**（`load_market_data()` 与它下面的 `_fetch_*`）：一图流为主、企鹅物流后备，
  四个来源全部落 `state/maa/farming/` 缓存（0600）。**任何失败都不抛**，只往
  `degraded_reasons` 里记一句，由计划卡显示出来（D-2 的教训：旁路数据不该带走整张卡）。
- **纯函数层**（`drop_rates()` / `scarcity_score()` / `gap_score()` /
  `efficiency_score()` / `farming_result()`）：只吃已经取到的数据结构，不碰网络，
  单测可以完全离线跑（DEV_GUIDE §9）。

许可：一图流与企鹅物流的数据都是 CC BY-NC 4.0，**运行时抓取、不 vendor 进仓库**，
输出材料推荐时带一行 `SOURCE_LINE` 署名。
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from maa_common import (
    BEIJING,
    STATE_DIR,
    atomic_write_json,
    load_json,
    now_beijing,
)


# ---------------------------------------------------------------------------
# 常量与配置默认值（**只在这里写一次**）
# ---------------------------------------------------------------------------

FARMING_CACHE_DIR = STATE_DIR / "farming"
# 统一 UA：spike B 实测四个接口都不要求 UA，但两家都要求 fair use，署名自己是礼貌。
USER_AGENT = "maa-remote/4 (+https://github.com/ffkiyo7/maa-remote)"
SOURCE_LINE = (
    "数据来源：明日方舟一图流 ark.yituliu.cn / 企鹅物流 penguin-stats.io（CC BY-NC 4.0）"
)

YITULIU_MATRIX_URL = "https://cos.yituliu.cn/arknights/stage-drop/matrix.json"
YITULIU_STAGE_INFO_URL = "https://backend.yituliu.cn/stage/info"
YITULIU_ITEM_VALUE_URL = "https://backend.yituliu.cn/item/v7/value"
YITULIU_STORE_URL = "https://backend.yituliu.cn/item/v5/store/activity"
PENGUIN_BASE = "https://penguin-stats.io/PenguinStats/api/v2"
PENGUIN_MATRIX_URL = f"{PENGUIN_BASE}/result/matrix?server=CN&show_closed_zones=true"
PENGUIN_ZONES_URL = f"{PENGUIN_BASE}/zones"
PENGUIN_STAGES_URL = f"{PENGUIN_BASE}/stages"

# 缓存新鲜度。matrix 走 ETag 条件请求（`Cache-Control: max-age=7200` 是骗人的，
# 实测约 10 分钟刷一次，spike B §5），其余两个后端接口**没有任何缓存头**，
# 照抄一图流前端自己的 1 小时 / 一天口径。
MATRIX_MIN_INTERVAL = timedelta(minutes=10)
STAGE_INFO_TTL = timedelta(hours=1)
ITEM_VALUE_TTL = timedelta(hours=24)
STORE_TTL = timedelta(hours=24)
HTTP_TIMEOUT = 20
# matrix 一图流 1.4 MB、企鹅 1.6 MB；留一倍余量，别让上游某天变大就整条断掉。
MAX_RESPONSE_BYTES = 12 * 1024 * 1024

# `stage/info` 的 `end` **永不为 null**，常驻用哨兵 `4070880000000`（2098 年）。
# 判「是不是常驻」只能用 `stageType` 或「end 在 10 年之后」，**不许判 null**（spike B §1）。
PERMANENT_HORIZON = timedelta(days=3650)
PERMANENT_TYPES = frozenset({"MAIN", "ACT_PERM"})
# 已经复刻过的活动按「不会再来」处理：复刻后通常常驻化或不再复刻。
RERUN_ONCE_ONLY = timedelta(days=730)
# 缺口价值归一化的分母。读得到实时理智就用实时值；读不出来用这个量级——
# 所有材料同除一个数，**不改变相对排序**，只影响 clamp 到 1 的早晚。
FALLBACK_SESSION_AP = 135.0

# 六个新配置键的默认值集中在这里，别散到调用点去。
FARMING_DEFAULTS: dict[str, Any] = {
    "farming_weights": {"scarcity": 0.5, "gap": 0.3, "efficiency": 0.2},
    "farming_min_samples": 300,
    "farming_target_days": 30,
    "farming_activity_edge": 1.3,
    "farming_rerun_days": 378,
    "farming_scarcity_horizon_days": 400,
}
WEIGHT_KEYS = ("scarcity", "gap", "efficiency")


def weights(config: dict[str, Any]) -> dict[str, float]:
    """三项权重。缺项按默认补齐，负数按 0；**和为任意值都归一**（见 `_blend()`）。"""
    raw = config.get("farming_weights")
    raw = raw if isinstance(raw, dict) else {}
    result: dict[str, float] = {}
    for key in WEIGHT_KEYS:
        try:
            value = float(raw.get(key, FARMING_DEFAULTS["farming_weights"][key]))
        except (TypeError, ValueError):
            value = float(FARMING_DEFAULTS["farming_weights"][key])
        result[key] = max(0.0, value)
    return result


def setting(config: dict[str, Any], key: str) -> float:
    """一个数值型配置键，坏值一律退回默认。"""
    default = float(FARMING_DEFAULTS[key])
    try:
        value = float(config.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# ---------------------------------------------------------------------------
# 抓取层
# ---------------------------------------------------------------------------


def _http_json(
    url: str,
    *,
    method: str = "GET",
    body: Any = None,
    etag: str = "",
    timeout: int = HTTP_TIMEOUT,
) -> tuple[int, Any, str]:
    """一次 JSON 请求 → `(状态码, 解析后的 body, 新 ETag)`。304 时 body 为 `None`。

    `maa_common.http_get_json()` 只做 GET、不带自定义头、也拿不到响应头，
    这里要的是 POST + `If-None-Match` + 回读 `ETag` 三样它都没有的东西，
    所以在本模块里另写一个小的——**不改 `maa_common` 的签名**。
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if etag:
        headers["If-None-Match"] = etag
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError("响应超过大小上限")
            new_etag = str(response.headers.get("ETag") or "")
            return int(response.status or 200), json.loads(raw.decode("utf-8")), new_etag
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, None, str(exc.headers.get("ETag") or etag)
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网络错误: {exc.reason}") from exc
    except (ValueError, OSError) as exc:
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc


def _unwrap(payload: Any) -> Any:
    """一图流的统一信封 `{code, msg, data}` → `data`；其它形态原样返回。"""
    if isinstance(payload, dict) and "data" in payload and "code" in payload:
        return payload.get("data")
    return payload


def _cache_path(name: str, cache_dir: Path | None = None) -> Path:
    return (cache_dir or FARMING_CACHE_DIR) / f"{name}.json"


def _read_cache(name: str, cache_dir: Path | None = None) -> dict[str, Any]:
    try:
        value = load_json(_cache_path(name, cache_dir), None)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_cache(
    name: str, payload: Any, *, etag: str, at: datetime, cache_dir: Path | None = None
) -> None:
    try:
        atomic_write_json(
            _cache_path(name, cache_dir),
            {"fetched_at": at.isoformat(), "etag": etag, "payload": payload},
        )
    except OSError:
        # 缓存写不下去不该影响本次推荐——数据已经在内存里了。
        pass


def _cache_age(entry: dict[str, Any], at: datetime) -> timedelta | None:
    """缓存有多旧；**读不出来或时间戳在未来一律返回 `None`**（= 当成不新鲜）。

    未来时间戳听起来不可能，但它真的会出现：机器时钟跳过、或者某次带
    `at=` 的调用把一个假时间写进了缓存。一旦把它当成「刚取的」，这份数据就会被
    冻到那个未来时刻为止——宁可多抓一次，也不能永远不刷新。
    """
    try:
        fetched = datetime.fromisoformat(str(entry.get("fetched_at") or ""))
    except ValueError:
        return None
    if fetched.tzinfo is None:
        return None
    age = at - fetched
    return age if age >= timedelta(0) else None


def _cached_payload(entry: dict[str, Any]) -> Any:
    return entry.get("payload") if "payload" in entry else None


def _fetch_cached(
    name: str,
    fetchers: Iterable[tuple[str, Callable[[str], tuple[int, Any, str]]]],
    *,
    ttl: timedelta | None,
    at: datetime,
    cache_dir: Path | None,
    reasons: list[str],
) -> Any:
    """按「缓存够新就不请求 → 依次试各来源 → 全失败退缓存」跑一遍。绝不抛。"""
    entry = _read_cache(name, cache_dir)
    age = _cache_age(entry, at)
    cached = _cached_payload(entry)
    if ttl is not None and cached is not None and age is not None and age < ttl:
        return cached
    etag = str(entry.get("etag") or "")
    failures: list[str] = []
    for label, call in fetchers:
        try:
            status, payload, new_etag = call(etag)
        except Exception as exc:  # noqa: BLE001 - 抓取层永不抛，只降级
            failures.append(f"{label}：{exc}")
            continue
        if status == 304:
            if cached is not None:
                # 304 是「你手上那份还新鲜」，不是失败：续期缓存时间戳，别每次都重问。
                _write_cache(name, cached, etag=new_etag or etag, at=at, cache_dir=cache_dir)
                return cached
            failures.append(f"{label}：304 但本地没有缓存")
            continue
        if payload is None:
            failures.append(f"{label}：空响应")
            continue
        _write_cache(name, payload, etag=new_etag, at=at, cache_dir=cache_dir)
        return payload
    if failures:
        reasons.append(f"{name} 抓取失败（{'；'.join(failures)}）")
    if cached is not None:
        reasons.append(f"{name} 用的是本地缓存")
        return cached
    return None


def _normalize_matrix(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("matrix") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        stage_id = str(row.get("stageId") or "").strip()
        item_id = str(row.get("itemId") or "").strip()
        if not stage_id or not item_id:
            continue
        result.append(
            {
                "stageId": stage_id,
                "itemId": item_id,
                "times": row.get("times"),
                "quantity": row.get("quantity"),
                "start": row.get("start"),
                "end": row.get("end"),
            }
        )
    return result


def _normalize_stage_info(payload: Any) -> list[dict[str, Any]]:
    rows = _unwrap(payload)
    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        stage_id = str(row.get("stageId") or "").strip()
        if not stage_id:
            continue
        result.append(
            {
                "stageId": stage_id,
                "stageCode": str(row.get("stageCode") or "").strip(),
                "zoneId": str(row.get("zoneId") or "").strip(),
                "zoneName": str(row.get("zoneName") or "").strip(),
                "stageType": str(row.get("stageType") or row.get("type") or "").strip(),
                "start": row.get("start"),
                "end": row.get("end"),
                "apCost": row.get("apCost"),
            }
        )
    return result


def _penguin_stage_info(zones: Any, stages: Any) -> list[dict[str, Any]]:
    """企鹅 `/zones` + `/stages` → 一图流 `stage/info` 的形状。

    两家的**常驻表示法不一样**：企鹅 `closeTime` 可以是 `null`，一图流用哨兵
    `4070880000000`。这里统一成哨兵，下游只认一套规则（spike B §6a）。
    """
    if not isinstance(zones, list) or not isinstance(stages, list):
        return []
    sentinel = 4070880000000
    windows: dict[str, tuple[Any, Any]] = {}
    types: dict[str, str] = {}
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        zone_id = str(zone.get("zoneId") or "")
        existence = zone.get("existence")
        cn = existence.get("CN") if isinstance(existence, dict) else None
        if not zone_id or not isinstance(cn, dict):
            continue
        close = cn.get("closeTime")
        windows[zone_id] = (cn.get("openTime"), sentinel if close is None else close)
        raw_type = str(zone.get("type") or "")
        types[zone_id] = {
            "MAINLINE": "MAIN",
            "ACTIVITY": "ACT",
            "ACTIVITY_PERMANENT": "ACT_PERM",
        }.get(raw_type, raw_type)
    result: list[dict[str, Any]] = []
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        stage_id = str(stage.get("stageId") or "").strip()
        zone_id = str(stage.get("zoneId") or "")
        if not stage_id:
            continue
        start, end = windows.get(zone_id, (None, sentinel))
        stage_type = types.get(zone_id, str(stage.get("stageType") or ""))
        if stage_id.endswith("_rep") and stage_type == "ACT":
            stage_type = "ACT_REP"
        result.append(
            {
                "stageId": stage_id,
                "stageCode": str(stage.get("code") or "").strip(),
                "zoneId": zone_id,
                "zoneName": "",
                "stageType": stage_type,
                "start": start,
                "end": end,
                "apCost": stage.get("apCost"),
            }
        )
    return result


def _normalize_item_values(payload: Any) -> dict[str, float | None]:
    rows = _unwrap(payload)
    if not isinstance(rows, list):
        return {}
    result: dict[str, float | None] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("itemId") or "").strip()
        if not item_id:
            continue
        raw = row.get("itemValue")
        # 129 条里有 16 条 `itemValue == null`（活动代币 / 理智小样）。**必须判空**，
        # 否则 `float * None` 直接崩（spike B §2 实测崩过一次）。
        try:
            result[item_id] = None if raw is None else float(raw)
        except (TypeError, ValueError):
            result[item_id] = None
    return result


def load_market_data(
    config: dict[str, Any],
    *,
    at: datetime | None = None,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    """四个来源 → 一份内存视图。**绝不抛**，失败只进 `degraded_reasons`。"""
    del config  # 目前四个来源都不需要配置；留参数是为了将来能配代理 / 关掉某个源
    moment = at or now_beijing()
    reasons: list[str] = []

    matrix_payload = _fetch_cached(
        "matrix",
        (
            (
                "一图流 matrix.json",
                lambda etag: _http_json(YITULIU_MATRIX_URL, etag=etag),
            ),
            ("企鹅 result/matrix", lambda _etag: _http_json(PENGUIN_MATRIX_URL)),
        ),
        ttl=MATRIX_MIN_INTERVAL,
        at=moment,
        cache_dir=cache_dir,
        reasons=reasons,
    )
    stage_payload = _fetch_cached(
        "stage_info",
        (
            ("一图流 stage/info", lambda _etag: _http_json(YITULIU_STAGE_INFO_URL)),
            (
                "企鹅 zones+stages",
                lambda _etag: (
                    200,
                    _penguin_stage_info(
                        _http_json(PENGUIN_ZONES_URL)[1],
                        _http_json(PENGUIN_STAGES_URL)[1],
                    ),
                    "",
                ),
            ),
        ),
        ttl=STAGE_INFO_TTL,
        at=moment,
        cache_dir=cache_dir,
        reasons=reasons,
    )
    value_payload = _fetch_cached(
        "item_value",
        (
            (
                "一图流 item/v7/value",
                lambda _etag: _http_json(
                    YITULIU_ITEM_VALUE_URL, method="POST", body={}
                ),
            ),
        ),
        ttl=ITEM_VALUE_TTL,
        at=moment,
        cache_dir=cache_dir,
        reasons=reasons,
    )
    # 活动商店是**可选**的提示信息（代币能换什么），拿不到连 reason 都不记。
    store_reasons: list[str] = []
    store_payload = _fetch_cached(
        "store_activity",
        (("一图流 item/v5/store/activity", lambda _etag: _http_json(YITULIU_STORE_URL)),),
        ttl=STORE_TTL,
        at=moment,
        cache_dir=cache_dir,
        reasons=store_reasons,
    )

    return {
        "matrix": _normalize_matrix(matrix_payload),
        "stages": _normalize_stage_info(stage_payload),
        "item_values": _normalize_item_values(value_payload),
        "store": _unwrap(store_payload),
        "degraded_reasons": reasons,
    }


def empty_market_data() -> dict[str, Any]:
    """一份「什么都没抓到」的视图，供调用方在完全离线时占位。"""
    return {
        "matrix": [],
        "stages": [],
        "item_values": {},
        "store": None,
        "degraded_reasons": ["外部掉率数据不可用"],
    }


# ---------------------------------------------------------------------------
# 纯函数层：时间、窗口、掉率
# ---------------------------------------------------------------------------


def _ms(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _moment(value: Any) -> datetime | None:
    milliseconds = _ms(value)
    if milliseconds is None:
        return None
    try:
        return datetime.fromtimestamp(milliseconds / 1000, tz=BEIJING)
    except (OverflowError, OSError, ValueError):
        return None


def stage_windows(stages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """`{stageId: {code, type, start, end, ap}}`。时间已换算成北京时间。"""
    result: dict[str, dict[str, Any]] = {}
    for entry in stages:
        stage_id = str(entry.get("stageId") or "")
        if not stage_id:
            continue
        try:
            ap = int(entry.get("apCost") or 0)
        except (TypeError, ValueError):
            ap = 0
        result[stage_id] = {
            "code": str(entry.get("stageCode") or ""),
            "type": str(entry.get("stageType") or ""),
            "start": _moment(entry.get("start")),
            "end": _moment(entry.get("end")),
            "ap": ap if ap > 0 else 0,
        }
    return result


def is_open(window: dict[str, Any], at: datetime) -> bool:
    start = window.get("start")
    end = window.get("end")
    if start is not None and start > at:
        return False
    if end is not None and end < at:
        return False
    return True


def is_permanent(window: dict[str, Any], at: datetime) -> bool:
    """常驻（主线 / 别传插曲常驻）。**用 `stageType` 或哨兵，不判 null**（spike B §1）。"""
    if str(window.get("type") or "") in PERMANENT_TYPES:
        return True
    end = window.get("end")
    return end is not None and end > at + PERMANENT_HORIZON


def drop_rates(
    matrix: list[dict[str, Any]],
    *,
    min_samples: float,
    at: datetime,
) -> dict[str, dict[str, float]]:
    """`{itemId: {stageId: 理智/件}}` 还差一步 apCost；这里先出 `件/次`。

    两条过滤，都是一图流前端的口径（spike B §3 / §8③）：

    - `times < farming_min_samples`（默认 300）视为无数据——低样本的掉率噪声极大；
    - `end` 非空且早于 now 的行不计：**活动关的统计只在开放期有效**，
      往期活动那几行留着会让「历史最优」永远停在一个打不到的关上。
      （matrix 用 `end == null` 表示区间仍在累积，与 `stage/info` 的哨兵值不是一回事。）
    """
    result: dict[str, dict[str, float]] = {}
    for row in matrix:
        try:
            times = float(row.get("times") or 0)
            quantity = float(row.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if times < min_samples or times <= 0 or quantity <= 0:
            continue
        end = _moment(row.get("end"))
        if end is not None and end < at:
            continue
        rate = quantity / times
        if rate <= 0:
            continue
        result.setdefault(str(row["itemId"]), {})[str(row["stageId"])] = rate
    return result


def ap_per_item(
    rates: dict[str, dict[str, float]],
    windows: dict[str, dict[str, Any]],
    *,
    extra_ap: dict[str, int] | None = None,
) -> dict[str, dict[str, float]]:
    """`{itemId: {stageId: 期望理智/件}}` = `apCost / 掉率`。理智未知的关整条丢掉。"""
    costs = dict(extra_ap or {})
    result: dict[str, dict[str, float]] = {}
    for item_id, per_stage in rates.items():
        for stage_id, rate in per_stage.items():
            window = windows.get(stage_id)
            ap = int(window.get("ap") or 0) if window else int(costs.get(stage_id, 0))
            if ap <= 0:
                ap = int(costs.get(stage_id, 0))
            if ap <= 0:
                continue
            result.setdefault(item_id, {})[stage_id] = ap / rate
    return result


def _best_stage(
    per_stage: dict[str, float],
    windows: dict[str, dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
) -> tuple[float | None, str]:
    best: float | None = None
    best_stage = ""
    for stage_id, cost in sorted(per_stage.items()):
        window = windows.get(stage_id)
        if window is None or not predicate(window):
            continue
        if best is None or cost < best:
            best, best_stage = cost, stage_id
    return best, best_stage


def scarcity_score(
    item_id: str,
    *,
    per_item: dict[str, dict[str, float]],
    windows: dict[str, dict[str, Any]],
    at: datetime,
    activity_edge: float,
    rerun_days: float,
    horizon_days: float,
) -> tuple[float, str]:
    """①「窗口要关了」这一维。返回 `(0..1 分值, 一句说明)`。

    只管**时间**：这个材料现在能便宜刷，下次能这么便宜刷是什么时候。
    「平均用量」那半边是第 ② 项的日均消耗，不在这里重复计分。

    判据（PLAN_PHASE4 步骤 3a 第 1 条）：

    1. 当前开放关里最便宜的 `best_now` vs 常驻关里最便宜的 `best_perm`，
       `best_perm / best_now >= farming_activity_edge`（默认 1.3）才算「活动关明显更优」；
       常驻本来就能刷、或活动关没便宜多少 → 0 分。
    2. 下次可刷日：`ACT_REP`（已复刻）视为不会再来，按 `end + 2 年`；
       其余按 `首发 start + farming_rerun_days`（默认 378，42 组 ACT→ACT_REP 的中位数）。
    3. 分值 = `clamp((下次可刷日 − now) / farming_scarcity_horizon_days, 0, 1)`。
    """
    per_stage = per_item.get(item_id) or {}
    if not per_stage:
        return 0.0, ""
    best_now, stage_id = _best_stage(per_stage, windows, lambda w: is_open(w, at))
    if best_now is None or best_now <= 0:
        return 0.0, ""
    window = windows.get(stage_id) or {}
    if is_permanent(window, at):
        return 0.0, "最优关是常驻关，随时能刷"
    best_perm, _perm_stage = _best_stage(
        per_stage, windows, lambda w: is_permanent(w, at)
    )
    edge = float("inf") if best_perm is None else best_perm / best_now
    if edge < activity_edge:
        return 0.0, f"活动关只便宜 {edge:.2f} 倍，未达 {activity_edge:.2f}"
    if str(window.get("type") or "") == "ACT_REP":
        end = window.get("end")
        next_at = (end or at) + RERUN_ONCE_ONLY
        basis = "已复刻过，按不会再来算"
    else:
        start = window.get("start")
        next_at = (start or at) + timedelta(days=rerun_days)
        basis = f"预计复刻 {next_at:%Y-%m-%d}"
    days = (next_at - at).total_seconds() / 86400
    score = max(0.0, min(1.0, days / horizon_days)) if horizon_days > 0 else 0.0
    edge_text = "常驻无关可刷" if best_perm is None else f"比常驻便宜 {edge:.2f} 倍"
    return score, f"{edge_text}，{basis}"


def gap_score(gap_value: float, session_ap: float) -> float:
    """② 缺口价值 ÷ 本场可用理智，clamp 到 1。

    先把件数乘以 `itemValue` 换成**理智当量**再比：1 个晶体电子单元 ≈ 302 理智
    ≈ 17.6 个固源岩组，按件数排序会把便宜材料顶到前面（spike B §8②）。
    """
    if session_ap <= 0 or gap_value <= 0:
        return 0.0
    return max(0.0, min(1.0, gap_value / session_ap))


def efficiency_score(
    item_id: str,
    *,
    per_item: dict[str, dict[str, float]],
    candidate_ids: set[str],
) -> float:
    """③ 候选集内最优「件/理智」÷ 全游戏历史最优「件/理智」，0..1。"""
    per_stage = per_item.get(item_id) or {}
    if not per_stage:
        return 0.0
    inside = [cost for stage_id, cost in per_stage.items() if stage_id in candidate_ids]
    if not inside:
        return 0.0
    best_anywhere = min(per_stage.values())
    best_inside = min(inside)
    if best_inside <= 0 or best_anywhere <= 0:
        return 0.0
    # 件/理智之比 = (1/best_inside) / (1/best_anywhere) = best_anywhere / best_inside
    return max(0.0, min(1.0, best_anywhere / best_inside))


def _blend(parts: dict[str, float], weight_map: dict[str, float]) -> float:
    """加权平均。**权重和为任意值都归一**，所以用户可以随手写 5/3/2。"""
    total = sum(weight_map.values())
    if total <= 0:
        return 0.0
    return sum(parts.get(key, 0.0) * weight_map.get(key, 0.0) for key in WEIGHT_KEYS) / total


# ---------------------------------------------------------------------------
# 候选关集合（D5）
# ---------------------------------------------------------------------------


def candidate_stages(
    config: dict[str, Any],
    at: datetime | None = None,
    *,
    activity_cache: Path | None = None,
) -> tuple[set[str], bool, str]:
    """`(候选关集合, 是不是活动期, 一句说明)`。

    D5：**活动期间只在活动关里选**（月卡都不开，理智必须 all in 活动才换得完代币商店），
    集合 = `config.activity_stages ∩ 上游导航键`（与自动抄作业同一个闸门，不变量 #37）。
    否则 = `1-7` + 当天开放的资源本。

    「是不是活动期」优先看 `StageActivityV2` 的截止时刻——`activity_stages` 是运行时
    缓存，活动结束当天它可能还留着上一期的关；截止时刻已过就该退回资源本。
    """
    import maa_clearing
    import maa_copilot
    import maa_plan_card

    moment = at or now_beijing()
    configured = str(config.get("maa_activity_cache") or "").strip()
    path = activity_cache or (Path(configured) if configured else None)
    try:
        deadlines = maa_plan_card.activity_deadlines(path)
    except Exception:  # noqa: BLE001 - 旁路信息，读不出来就按「没有日历」处理
        deadlines = []
    stages = [
        str(stage).strip()
        for stage in (config.get("activity_stages") or [])
        if str(stage).strip()
    ]
    active = (
        any(expire > moment for _title, expire in deadlines) if deadlines else bool(stages)
    )
    if active and stages:
        try:
            nav_of = maa_clearing.nav_checker(maa_copilot._resource_dirs(config))
            navigable = [stage for stage in stages if nav_of(stage)[0]]
        except Exception:  # noqa: BLE001 - 闸门读不出来时不该整条推荐消失
            navigable = list(stages)
        if navigable:
            return set(navigable), True, "活动期：候选集只有活动关（D5）"
    supplies = {
        stage
        for _label, stage, _desc in maa_plan_card.current_supply_choices(config, moment)
    }
    reason = (
        "活动期，但没有上游写了导航定义的活动关，退回 1-7 + 当天开放资源本"
        if active
        else "无活动：1-7 + 当天开放的资源本"
    )
    return ({"1-7"} | supplies), False, reason


# ---------------------------------------------------------------------------
# 上游资源：itemId ↔ 中文名，关卡 apCost
# ---------------------------------------------------------------------------


def item_names(config: dict[str, Any]) -> dict[str, str]:
    """`{itemId: 中文名}`，读不出来返回空表（绝不抛）。"""
    import maa_copilot

    try:
        for directory in maa_copilot._resource_dirs(config):
            raw = load_json(directory / "item_index.json", {})
            if not isinstance(raw, dict) or not raw:
                continue
            names = {
                str(item_id): str(
                    detail.get("name") if isinstance(detail, dict) else detail
                )
                for item_id, detail in raw.items()
            }
            names = {key: value for key, value in names.items() if value}
            if names:
                return names
    except Exception:  # noqa: BLE001
        return {}
    return {}


def stage_ap_cost(
    stage_id: str,
    config: dict[str, Any] | None = None,
    *,
    cache_dir: Path | None = None,
) -> int | None:
    """从 `stage/info` **缓存**取一关的 `apCost`；拿不到返回 `None`。

    步骤 2 留的坑：EX 关不在上游 `stages.json` 里，`maa_copilot.stage_sanity_cost()`
    对它一律 `None`，理智预算就把整条目丢掉。这里给 `maa_activity_watch.cost_lookup()`
    当**兜底**——注意它**只读本地缓存、绝不发请求**（watcher 每 15 分钟一 tick，
    不能在这条路径上等网络），所以缓存是由计划卡那趟抓取顺手写下的。
    仍然是 fail-closed：两处都查不到就还是 `None`，条目不入轮。
    """
    del config
    entry = _read_cache("stage_info", cache_dir)
    stages = _normalize_stage_info(_cached_payload(entry))
    wanted = str(stage_id or "").strip()
    if not wanted:
        return None
    for window in stages:
        if str(window.get("stageId") or "") != wanted:
            continue
        try:
            ap = int(window.get("apCost") or 0)
        except (TypeError, ValueError):
            return None
        return ap if ap > 0 else None
    return None


# ---------------------------------------------------------------------------
# 主入口：三项加权分
# ---------------------------------------------------------------------------


def _session_ap(config: dict[str, Any], at: datetime) -> float:
    """本场大致可用理智，只用来把缺口价值归一。读不出来用常量兜底。"""
    del config
    try:
        import maa_copilot

        roster = maa_copilot.load_roster()
        snapshot = (roster or {}).get("sanity")
        value = maa_copilot.estimated_sanity(
            snapshot if isinstance(snapshot, dict) else {}, at
        )
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    except Exception:  # noqa: BLE001
        pass
    return FALLBACK_SESSION_AP


def _manual_weights(config: dict[str, Any]) -> dict[str, float]:
    raw = config.get("farming_manual_weights")
    raw = raw if isinstance(raw, dict) else {}
    result: dict[str, float] = {}
    for name, value in raw.items():
        try:
            result[str(name)] = max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return result


def _stage_index(
    config: dict[str, Any],
    market: dict[str, Any],
    *,
    at: datetime,
    min_samples: float,
) -> dict[str, Any]:
    """把外部数据与上游资源合成一份查表用的索引。

    `stage/info` 只覆盖 1550 关，**不含 EX 与周常**（spike B §1），所以关卡代号 →
    `stageId` / `apCost` 两张表都要拿上游 `stages.json` 兜底；反过来 `stage/info`
    知道开放窗口，所以同名代号（首发与复刻共用 `GT-1` 这种）以**当前开放的那一条**为准。
    """
    import maa_plan_card

    windows = stage_windows(list(market.get("stages") or []))
    upstream = maa_plan_card.stage_drops(config)
    extra_ap = {
        str(detail.get("stage_id") or ""): int(detail.get("ap") or 0)
        for detail in upstream.values()
        if str(detail.get("stage_id") or "") and int(detail.get("ap") or 0) > 0
    }
    rates = drop_rates(list(market.get("matrix") or []), min_samples=min_samples, at=at)

    ids_by_name: dict[str, str] = {}
    for item_id, name in item_names(config).items():
        ids_by_name.setdefault(name, item_id)

    code_to_id: dict[str, str] = {}
    for code, detail in upstream.items():
        stage_id = str(detail.get("stage_id") or "")
        if stage_id:
            code_to_id[code] = stage_id
    for stage_id, window in windows.items():
        code = str(window.get("code") or "")
        if code and is_open(window, at):
            code_to_id[code] = stage_id

    return {
        "windows": windows,
        "per_item": ap_per_item(rates, windows, extra_ap=extra_ap),
        "code_to_id": code_to_id,
        "ids_by_name": ids_by_name,
        "upstream": upstream,
        "has_stage_info": bool(windows),
    }


def _consumption(depot_state: dict[str, Any], at: datetime) -> dict[str, Any]:
    """Depot 侧的取数：是否已有实测、4 周窗口日均消耗、库存估算、对表次数。"""
    import maa_depot

    measured = maa_depot.has_measured_consumption(depot_state)
    window_rates = (
        maa_depot.consumption_rates_window(depot_state, at=at) if measured else {}
    )
    intervals = depot_state.get("consumption_history")
    return {
        "measured": measured,
        "per_day": {
            str(detail.get("name") or item_id): float(detail.get("per_day") or 0.0)
            for item_id, detail in window_rates.items()
            if isinstance(detail, dict)
        },
        "stock": maa_depot.stock_by_name(depot_state),
        # 每两次对表之间产生一条区间记录，所以对表次数 = 区间数 + 1。
        "reconciles": (len(intervals) if isinstance(intervals, list) else 0) + 1,
    }


def farming_result(
    config: dict[str, Any],
    depot_state: dict[str, Any],
    *,
    available_stages: set[str] | None = None,
    at: datetime | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """三项加权分 → 降序推荐表。**任何一段坏掉都只降级，不抛**。

    返回 `{degraded, reason, degraded_reasons, items, skipped_no_stage,
    skipped_closed, candidate_reason, in_activity, consumption_note, weights}`；
    `items` 每项 `{material, item_id, stage, score, parts, needed_qty,
    ap_per_item, stock, per_day, basis, why}`。
    """
    import maa_plan_card

    moment = at or now_beijing()
    market = data if isinstance(data, dict) else load_market_data(config, at=moment)
    reasons = list(market.get("degraded_reasons") or [])

    derived, in_activity, candidate_reason = candidate_stages(config, moment)
    available = (
        derived
        if available_stages is None
        else {str(stage) for stage in available_stages if str(stage)}
    )

    weight_map = weights(config)
    min_samples = setting(config, "farming_min_samples")
    target_days = setting(config, "farming_target_days")
    activity_edge = setting(config, "farming_activity_edge")
    rerun_days = setting(config, "farming_rerun_days")
    horizon_days = setting(config, "farming_scarcity_horizon_days")

    index = _stage_index(config, market, at=moment, min_samples=min_samples)
    windows = index["windows"]
    per_item = index["per_item"]
    code_to_id = index["code_to_id"]
    ids_by_name = index["ids_by_name"]
    upstream = index["upstream"]
    has_stage_info = index["has_stage_info"]
    candidate_ids = {code_to_id[code] for code in available if code in code_to_id}

    table = maa_plan_card.material_stage_candidates(config)
    usage = _consumption(depot_state, moment)
    measured = usage["measured"]
    per_day = usage["per_day"]
    stock = usage["stock"]
    manual = _manual_weights(config)
    manual_top = max(manual.values()) if manual else 0.0
    item_values = dict(market.get("item_values") or {})
    session_ap = _session_ap(config, moment)
    manual_table = config.get("farming_stage_table")
    manual_table = manual_table if isinstance(manual_table, dict) else {}
    # `stage/info` 不可用时的稀缺退化口径（PLAN_PHASE4 步骤 3a 第 1 条最后一句）：
    # 「当期活动关独有主掉落 = 1，其余 0」。「独有」= 今天除了活动关之外没有别的来源：
    # 既不在手工表里（资源本靠它），也不是 1-7 / 当天资源本的主掉落。
    activity_codes = {
        str(stage).strip()
        for stage in (config.get("activity_stages") or [])
        if str(stage).strip()
    }
    activity_materials: set[str] = set()
    other_materials: set[str] = set(manual_table)
    for code in available | {"1-7"}:
        materials = set((upstream.get(code) or {}).get("materials") or [])
        (activity_materials if code in activity_codes else other_materials).update(
            materials
        )

    def worth_reporting(material: str) -> bool:
        """值不值得在卡片上说「这个材料今天没关刷」。只报在消耗的和手工配过的。"""
        return per_day.get(material, 0.0) > 0 or material in manual_table

    universe = set(table) | {name for name, rate in per_day.items() if rate > 0}
    items: list[dict[str, Any]] = []
    no_stage: list[str] = []
    closed: list[str] = []
    for material in sorted(universe):
        options = table.get(material) or []
        if not options:
            if worth_reporting(material):
                no_stage.append(material)
            continue
        open_options = [code for code in options if code in available]
        if not open_options:
            if worth_reporting(material):
                closed.append(f"{material}→{options[0]}")
            continue
        item_id = ids_by_name.get(material, "")
        costs = per_item.get(item_id) or {}
        # 同一材料多关可选时，有掉率就按「理智/件」挑最便宜的；
        # 没有掉率数据就沿用 `material_stage_candidates()` 的启发式顺序（手工表在前）。
        priced = [
            (costs[code_to_id[code]], code)
            for code in open_options
            if code in code_to_id and code_to_id[code] in costs
        ]
        if priced:
            best_cost, stage = min(priced)
        else:
            best_cost, stage = None, open_options[0]

        value = item_values.get(item_id)
        rate = per_day.get(material, 0.0)
        needed = 0
        gap_value = 0.0
        if measured and rate > 0:
            needed = max(0, math.ceil(rate * target_days - stock.get(material, 0)))
            if value is not None:
                gap_value = needed * value
        manual_part = (manual.get(material, 0.0) / manual_top) if manual_top > 0 else 0.0
        if not measured:
            # 冷启动：整项退手工权重（`consumption_history` 不足 2 次对表）。
            gap_part = manual_part
            basis = "manual"
        elif rate <= 0:
            # 零消耗：可用量不是 ∞ 而是**未知**，同样退手工权重（不变量 #32）。
            gap_part = manual_part
            basis = "manual"
        elif value is None:
            # `itemValue == null`（活动代币等）不参与缺口计价（spike B §2）。
            gap_part = 0.0
            basis = "measured"
        else:
            gap_part = gap_score(gap_value, session_ap)
            basis = "measured"

        if has_stage_info:
            scarcity, scarcity_why = scarcity_score(
                item_id,
                per_item=per_item,
                windows=windows,
                at=moment,
                activity_edge=activity_edge,
                rerun_days=rerun_days,
                horizon_days=horizon_days,
            )
        else:
            # 退化口径：当期活动关**独有**的主掉落记 1，其余 0。
            unique = material in activity_materials and material not in other_materials
            scarcity = 1.0 if unique else 0.0
            scarcity_why = "排期不可用，按「活动关独有主掉落」估" if unique else ""
        efficiency = efficiency_score(
            item_id, per_item=per_item, candidate_ids=candidate_ids
        )
        parts = {"scarcity": scarcity, "gap": gap_part, "efficiency": efficiency}
        items.append(
            {
                "material": material,
                "item_id": item_id,
                "stage": stage,
                "score": round(_blend(parts, weight_map), 4),
                "parts": {key: round(value_, 4) for key, value_ in parts.items()},
                "needed_qty": needed,
                "ap_per_item": None if best_cost is None else round(best_cost, 2),
                "stock": stock.get(material, 0),
                "per_day": rate,
                "basis": basis,
                "why": scarcity_why,
            }
        )
    # 降序；同分先看「理智/件」更便宜的（活动首日常常几个材料三项全同分），
    # 再按材料名兜底，保证**确定性**——排序不稳会让同样的库存今天推 A 明天推 B。
    items.sort(
        key=lambda item: (
            -item["score"],
            item["ap_per_item"] if item["ap_per_item"] is not None else float("inf"),
            item["material"],
        )
    )

    if not measured:
        reasons.append(
            "尚无跨区间消耗数据（`depot.json` 的 `consumption_history` 不足 2 次对表），"
            "缺口项按 `config.farming_manual_weights` 的手工权重算"
        )
    return {
        "degraded": bool(reasons),
        "reason": reasons[0] if reasons else "",
        "degraded_reasons": reasons,
        "measured": measured,
        "items": items,
        "skipped_no_stage": no_stage,
        "skipped_closed": closed,
        "candidate_reason": candidate_reason,
        "in_activity": in_activity,
        "consumption_note": (
            f"消耗数据来自最近 {usage['reconciles']} 次对表、窗口 4 周"
            if measured
            else "消耗数据：尚未攒够 2 次对表"
        ),
        "weights": weight_map,
        "source_line": SOURCE_LINE,
    }


def auto_stage(result: dict[str, Any]) -> tuple[str, str]:
    """今天自动定的关 + 一句为什么。定不下来返回 `("", 原因)`，调用方沿用默认关。

    **降级期照样定关**（四期步骤 3 改写不变量 #31）：候选集已经受活动 / 当日开放限制，
    而且外部掉率给了兜底，此时选出来的关是合理的；卡片上标「降级」并列出原因即可。
    这与三期「降级期不自动改关」的取舍不同——那时排序依据只有手工权重。
    """
    for item in result.get("items") or []:
        stage = str(item.get("stage") or "")
        if not stage:
            continue
        parts = item.get("parts") or {}
        return stage, (
            f"{item.get('material') or '未知材料'} 综合 {float(item.get('score') or 0):.2f}"
            f"（稀缺 {parts.get('scarcity', 0):.2f} / 缺口 {parts.get('gap', 0):.2f} / "
            f"效率 {parts.get('efficiency', 0):.2f}）"
        )
    return "", "候选关里没有能算出缺口的材料，沿用默认关"


def fallback_stage(result: dict[str, Any]) -> str:
    """第二名的关，用于 `drops` 达标后的第二个 Fight 块。与第一名相同时返回空串。"""
    items = result.get("items") or []
    if not items:
        return ""
    first = str(items[0].get("stage") or "")
    for item in items[1:]:
        stage = str(item.get("stage") or "")
        if stage and stage != first:
            return stage
    return ""


def drops_cap(result: dict[str, Any]) -> dict[str, int]:
    """第一名材料的 `{itemId: 还差多少个}`，给 Fight 的 `drops` 参数用。

    冷启动、缺口 ≤ 0、或者上游没给这个材料 itemId 时**返回空表**（= 不设上限）。
    """
    items = result.get("items") or []
    if not items or not result.get("measured"):
        return {}
    head = items[0]
    item_id = str(head.get("item_id") or "")
    try:
        needed = int(head.get("needed_qty") or 0)
    except (TypeError, ValueError):
        return {}
    if not item_id or needed <= 0:
        return {}
    return {item_id: needed}


def farming_note(result: dict[str, Any]) -> str:
    """计划卡「为什么」字段：前三名的三项分 + 降级原因 + 数据来源。"""
    lines: list[str] = []
    for reason in (result.get("degraded_reasons") or [])[:3]:
        lines.append(f"⚠️ **降级**：{reason}")
    lines.append(f"候选集：{result.get('candidate_reason') or '未知'}")
    items = result.get("items") or []
    if not items:
        lines.append("暂无可推荐的材料关卡。")
    for index, item in enumerate(items[:3], start=1):
        parts = item.get("parts") or {}
        detail = (
            f"{index}. {item.get('material') or '未知材料'} → `{item.get('stage') or ''}` "
            f"综合 {float(item.get('score') or 0):.2f}"
            f"（稀缺 {parts.get('scarcity', 0):.2f} / 缺口 {parts.get('gap', 0):.2f} / "
            f"效率 {parts.get('efficiency', 0):.2f}）"
        )
        extras: list[str] = []
        if item.get("needed_qty"):
            extras.append(f"还缺 {item['needed_qty']} 个")
        if item.get("ap_per_item"):
            extras.append(f"{item['ap_per_item']} 理智/件")
        if item.get("why"):
            extras.append(str(item["why"]))
        if extras:
            detail += "；" + "、".join(extras)
        lines.append(detail)
    if result.get("skipped_no_stage"):
        lines.append(
            "未纳入推荐（上游掉落表和 `farming_stage_table` 里都查不到关卡）："
            + "、".join(result["skipped_no_stage"][:6])
        )
    if result.get("skipped_closed"):
        # 活动期这一行大半是资源本——那是 D5 的设计（理智 all in 活动），不是故障，
        # 所以措辞要分开，别让用户以为资源本坏了。
        head = (
            "不在本场候选集里（活动期只刷活动关）："
            if result.get("in_activity")
            else "今天没有开放的关可刷："
        )
        lines.append(head + "、".join(result["skipped_closed"][:6]))
    lines.append(str(result.get("consumption_note") or ""))
    lines.append(str(result.get("source_line") or SOURCE_LINE))
    return "\n".join(line for line in lines if line)[:1024]
