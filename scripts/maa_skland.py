#!/usr/bin/env python3
"""Skland (森空岛) protocol: token -> cred -> signed roster fetch.

Signing and endpoints were validated live in the C0 spike (2026-08-02):
  - grant:      POST as.hypergryph.com/user/oauth2/v2/grant
  - cred:       POST zonai.skland.com/api/v1/user/auth/generate_cred_by_code
  - binding:    GET  zonai.skland.com/api/v1/game/player/binding
  - player/info: GET zonai.skland.com/api/v1/game/player/info?uid=<uid>
Signing quirks (all validated):
  - timestamp = now - 2 (server clock skew)
  - GET query participates WITHOUT the leading '?'
  - request headers use lowercase keys: cred/sign/platform/timestamp/dId/vName
  - dId/platform/vName may all be empty strings (passes risk control)

Tokens never appear in argv, env, or stdout.  `link` reads one line from
stdin.  Never retry auth in a loop: on 10002 re-grant exactly once.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from maa_common import atomic_write_json, atomic_write_text, load_json, now_beijing
import maa_copilot

APP_CODE = "4ca99fa6b56cc2ba"
GRANT_URL = "https://as.hypergryph.com/user/oauth2/v2/grant"
SKLAND_BASE = "https://zonai.skland.com"
USER_AGENT = "Skland/1.0.1 (com.hypergryph.skland; build:100001014; Android 31; ) Okhttp/4.11.0"
MAX_BYTES = 8 * 1024 * 1024

SKLAND_PATH = maa_copilot.SKLAND_PATH
ROSTER_PATH = maa_copilot.ROSTER_PATH
HG_TOKEN_FILE = maa_copilot.COPILOT_DIR / "hg_token.txt"


def _http_json(method: str, url: str, body: dict[str, Any] | None, headers: dict[str, str]) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise RuntimeError("skland 响应超过大小上限")
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"skland HTTP {exc.code}: {exc.read(2048).decode('utf-8', errors='replace')[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"skland 网络错误: {exc.reason}") from exc


def _sign(sign_token: str, path: str, payload: str) -> tuple[str, str]:
    """Golden-vector-tested signing: MD5(HMAC-SHA256(path+payload+t+header_ca))."""
    t = str(int(time.time()) - 2)
    header_ca = {"platform": "", "timestamp": t, "dId": "", "vName": ""}
    message = path + payload + t + json.dumps(header_ca, separators=(",", ":"))
    digest = hmac.new(sign_token.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    sign = hashlib.md5(digest.encode("utf-8")).hexdigest()
    return sign, t


def _signed_get(path: str, sign_token: str, cred: str) -> dict[str, Any]:
    path_part, _, query = path.partition("?")
    sign, t = _sign(sign_token, path_part, query)
    headers = {
        "cred": cred,
        "sign": sign,
        "platform": "",
        "timestamp": t,
        "dId": "",
        "vName": "",
    }
    return _http_json("GET", f"{SKLAND_BASE}{path}", None, headers)


def _grant_code(hg_token: str) -> str:
    payload = _http_json(
        "POST",
        GRANT_URL,
        {"token": hg_token, "appCode": APP_CODE, "type": 0},
        {},
    )
    if payload.get("status") != 0:
        raise RuntimeError(f"grant 失败：{payload.get('msg', payload)}")
    code = payload.get("data", {}).get("code", "")
    if not code:
        raise RuntimeError("grant 未返回 code")
    return code


def _fetch_cred(hg_token: str) -> dict[str, str]:
    code = _grant_code(hg_token)
    payload = _http_json(
        "POST",
        f"{SKLAND_BASE}/api/v1/user/auth/generate_cred_by_code",
        {"code": code, "kind": 1},
        {},
    )
    if payload.get("code") != 0:
        raise RuntimeError(f"cred 失败：{payload.get('message', payload)}")
    data = payload.get("data", {})
    if not isinstance(data, dict) or not data.get("cred") or not data.get("token"):
        raise RuntimeError("cred 响应缺字段")
    return {
        "cred": str(data["cred"]),
        "sign_token": str(data["token"]),
        "user_id": str(data.get("userId", "")),
    }


def load_skland_state() -> dict[str, Any] | None:
    """skland.json primary; seeds from the user's hg_token.txt if present."""
    state = load_json(SKLAND_PATH, None)
    if isinstance(state, dict) and state.get("hg_token"):
        return state
    if HG_TOKEN_FILE.is_file():
        token = HG_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return {"hg_token": token, "cred": "", "sign_token": "", "uid": ""}
    return None


def _ensure_cred(state: dict[str, Any]) -> dict[str, Any]:
    """Refresh cred when missing/expired. Never loops: exactly one retry."""
    cred = str(state.get("cred") or "")
    sign_token = str(state.get("sign_token") or "")
    if cred and sign_token:
        return state
    fresh = _fetch_cred(str(state.get("hg_token") or ""))
    state["cred"] = fresh["cred"]
    state["sign_token"] = fresh["sign_token"]
    state["user_id"] = fresh["user_id"]
    state["cred_updated_at"] = now_beijing().isoformat()
    state.pop("last_error", None)
    atomic_write_json(SKLAND_PATH, state)
    return state


def _resolve_uid(state: dict[str, Any], sign_token: str, cred: str) -> str:
    binding = _signed_get("/api/v1/game/player/binding", sign_token, cred)
    if binding.get("code") != 0:
        raise RuntimeError(f"binding 失败：{binding.get('message', binding)}")
    data = binding.get("data", {})
    apps = data.get("list", []) if isinstance(data, dict) else []
    arknights = next((a for a in apps if isinstance(a, dict) and a.get("appCode") == "arknights"), None)
    if not arknights:
        raise RuntimeError("账号未绑定明日方舟")
    uid = arknights.get("defaultUid") or ""
    if not uid:
        blist = arknights.get("bindingList", [])
        uid = str(blist[0].get("uid") or "") if blist else ""
    if not uid:
        raise RuntimeError("无法解析游戏 uid")
    return str(uid)


def _normalize_chars(chars: list[dict[str, Any]], battle_data: dict[str, Any]) -> dict[str, Any]:
    """charId -> {name, level, elite, main_skill_lvl, skills, module_levels, rarity}."""
    normalized: dict[str, Any] = {}
    for raw in chars:
        if not isinstance(raw, dict):
            continue
        char_id = str(raw.get("charId") or "")
        if not char_id:
            continue
        info = battle_data.get(char_id) or {}
        skills = raw.get("skills") or []
        module_levels: list[int] = []
        equips = raw.get("equip") or []
        if isinstance(equips, list):
            # Keep slot order intact: locked modules occupy their slot as 0,
            # otherwise module_levels[req_module-1] would read the wrong one.
            module_levels = [
                0 if e.get("locked") else int(e.get("level") or 0)
                for e in equips
                if isinstance(e, dict)
            ]
        normalized[char_id] = {
            "name": str(info.get("name") or char_id)[:40],
            "level": int(raw.get("level") or 0),
            "elite": int(raw.get("evolvePhase") or 0),
            "main_skill_lvl": int(raw.get("mainSkillLvl") or 0),
            "skills": [
                int(s.get("specializeLevel") or 0)
                for s in skills
                if isinstance(s, dict)
            ],
            "module_levels": module_levels,
            "rarity": int(raw.get("rarity") or 0),
        }
    return normalized


def _safe_int(value: Any) -> int:
    """int() that never raises (zone progress is optional hint data)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_activity_progress(activity: Any) -> dict[str, Any]:
    """actId -> [zone progress dicts] from player/info `activity`.

    Zone-level only (clearedStage/totalStage), no per-stage granularity —
    consumers must treat it as a strong hint, never a precise unlock check.
    """
    progress: dict[str, Any] = {}
    if not isinstance(activity, list):
        return progress
    for entry in activity:
        if not isinstance(entry, dict):
            continue
        act_id = str(entry.get("actId") or "")
        zones = entry.get("zones")
        if not act_id or not isinstance(zones, list) or not zones:
            continue
        cleaned = []
        for zone in zones:
            if not isinstance(zone, dict):
                continue
            cleaned.append(
                {
                    "zoneId": str(zone.get("zoneId") or ""),
                    "clearedStage": _safe_int(zone.get("clearedStage")),
                    "totalStage": _safe_int(zone.get("totalStage")),
                }
            )
        if cleaned:
            progress[act_id] = cleaned
    return progress


def _normalize_sanity(data: Any) -> dict[str, int] | None:
    """Keep only the AP fields needed for a bounded execution preflight."""
    if not isinstance(data, dict):
        return None
    status = data.get("status")
    ap = status.get("ap") if isinstance(status, dict) else None
    if not isinstance(ap, dict):
        return None
    try:
        normalized = {
            "current": int(ap["current"]),
            "max": int(ap["max"]),
            "last_ap_add_at": int(ap["lastApAddTime"]),
            "server_current_at": int(data["currentTs"]),
        }
    except (KeyError, TypeError, ValueError):
        return None
    if (
        normalized["current"] < 0
        or normalized["max"] <= 0
        or normalized["last_ap_add_at"] <= 0
        or normalized["server_current_at"] <= 0
    ):
        return None
    return normalized


def fetch_roster(state: dict[str, Any]) -> dict[str, Any]:
    """Full grant->cred->binding->player/info chain; returns normalized chars.

    On skland code 10002 (expired cred): re-grant once, retry once, then give
    up and record last_error (no loop).
    """
    state = _ensure_cred(state)

    def attempt() -> dict[str, Any]:
        cred = str(state.get("cred") or "")
        sign_token = str(state.get("sign_token") or "")
        uid = str(state.get("uid") or "")
        if not uid:
            uid = _resolve_uid(state, sign_token, cred)
            state["uid"] = uid
            atomic_write_json(SKLAND_PATH, state)
        info = _signed_get(f"/api/v1/game/player/info?uid={uid}", sign_token, cred)
        if info.get("code") != 0:
            raise RuntimeError(f"player/info 失败：{info.get('message', info)}")
        data = info.get("data", {})
        chars = data.get("chars", []) if isinstance(data, dict) else []
        if not isinstance(chars, list):
            raise RuntimeError("player/info chars 格式异常")
        battle = maa_copilot.battle_data({})
        try:
            progress = _normalize_activity_progress(
                data.get("activity") if isinstance(data, dict) else None
            )
        except Exception:
            progress = {}  # optional hint data must never break the roster
        return {
            "chars": _normalize_chars(chars, battle),
            "activity_progress": progress,
            "sanity": _normalize_sanity(data),
        }

    try:
        return attempt()
    except RuntimeError as exc:
        message = str(exc)
        expired = "10002" in message or "HTTP 401" in message or "用户未登录" in message
        if not expired:
            raise
        # expired cred: one re-grant, one retry
        state.pop("cred", None)
        state.pop("sign_token", None)
        state = _ensure_cred(state)
        try:
            return attempt()
        except RuntimeError:
            state["last_error"] = "cred 过期且重认证失败；请在本机终端重新运行 maa_skland.py link"
            atomic_write_json(SKLAND_PATH, state)
            raise


def refresh_roster() -> dict[str, Any]:
    """Fetch and persist roster.json (0600). Returns the roster."""
    state = load_skland_state()
    if state is None:
        raise RuntimeError("未配置森空岛 token；请在本机终端运行 python3 maa_skland.py link")
    data = fetch_roster(state)
    roster = {
        "fetched_at": now_beijing().isoformat(),
        "uid": str(state.get("uid") or ""),
        "source": "skland",
        "chars": data["chars"],
        "activity_progress": data.get("activity_progress", {}),
    }
    if isinstance(data.get("sanity"), dict):
        roster["sanity"] = data["sanity"]
    atomic_write_json(ROSTER_PATH, roster)
    return roster


def roster_needs_refresh(config: dict[str, Any]) -> bool:
    """Lazy refresh: roster older than skland_roster_refresh_days -> True."""
    roster = load_json(ROSTER_PATH, None)
    if not isinstance(roster, dict) or not isinstance(roster.get("chars"), dict):
        return True
    fetched = str(roster.get("fetched_at") or "")
    try:
        age_days = (now_beijing() - datetime.fromisoformat(fetched)).days
    except (ValueError, TypeError):
        return True
    return age_days >= max(1, int(config.get("skland_roster_refresh_days", 7)))


def link_from_stdin() -> int:
    """Read one token line from stdin, persist it, never echo it."""
    line = sys.stdin.readline().strip()
    if not line:
        print("未读取到 token。", file=sys.stderr)
        return 1
    maa_copilot.ensure_private_dirs()
    atomic_write_json(SKLAND_PATH, {"hg_token": line, "cred": "", "sign_token": "", "uid": ""})
    atomic_write_text(HG_TOKEN_FILE, line + "\n")
    try:
        roster = refresh_roster()
        print(f"✅ 森空岛接入成功：{len(roster['chars'])} 名干员已缓存（uid={roster['uid']}）。")
        return 0
    except Exception as exc:
        print(f"⚠️ token 已保存，但拉取练度失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("可以稍后手动运行 python3 maa_skland.py refresh 重试。", file=sys.stderr)
        return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["link"]:
        return link_from_stdin()
    if argv[:1] == ["refresh"]:
        roster = refresh_roster()
        print(f"✅ 练度已刷新：{len(roster['chars'])} 名干员（uid={roster['uid']}）。")
        return 0
    print("用法: python3 maa_skland.py link | refresh", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
