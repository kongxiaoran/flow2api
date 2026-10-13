"""Link：定期轮换 Google 会话 Cookie，防止服务器上的副本过期。

Google 会不断轮换 ``__Secure-*PSIDTS`` / ``*PSIDCC`` / ``SIDCC``。浏览器里由
``accounts.google.com/RotateCookiesPage`` 完成；服务器上保存的 Cookie 如果长期没有被
任何浏览器页面更新，副本过期后 AT 刷新会返回 401，Token 被自动禁用。

这里模拟浏览器的 ``POST accounts.google.com/RotateCookies``，把响应里的新值合并回
``tokens.google_cookies``。不依赖浏览器，不使用账号密码。

只处理 JSON 数组形式的 Cookie（name/value/domain/...），Cookie 头文本形式不处理。
日志与返回值只包含 Cookie 名字，不包含值。
"""

import json
import os
import time
from pathlib import Path
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

from curl_cffi.requests import AsyncSession

from ..core.logger import debug_logger

ROTATE_URL = "https://accounts.google.com/RotateCookies"
ROTATE_BODY = '[000,"-0000000000000000000"]'
GOOGLE_DOMAINS = {"google.com"}
IMPERSONATE = "chrome"
REQUEST_TIMEOUT_SECONDS = 30


def keepalive_interval_minutes() -> int:
    """FLOW_COOKIE_KEEPALIVE_MINUTES：0 或未设置表示关闭；有效范围 2～240 分钟。"""
    try:
        minutes = int(os.environ.get("FLOW_COOKIE_KEEPALIVE_MINUTES", "0").strip() or 0)
    except ValueError:
        return 0
    if minutes <= 0:
        return 0
    return max(2, min(240, minutes))


def default_state_path() -> Path:
    """上次轮换时间的落盘位置（与 SQLite 同目录 /app/data，容器重建后仍在）。"""
    return Path(__file__).resolve().parents[2] / "data" / "cookie_keepalive_state.json"


def load_state(path: Optional[Path] = None) -> Dict[int, float]:
    """读取 {token_id: 上次尝试轮换的 unix 时间}；文件不存在或损坏时返回空。"""
    try:
        raw = json.loads((path or default_state_path()).read_text(encoding="utf-8"))
        return {int(key): float(value) for key, value in raw.items()}
    except Exception:
        return {}


def save_state(state: Dict[int, float], path: Optional[Path] = None) -> None:
    """原子写入；失败只记日志，不影响保活本身。"""
    target = path or default_state_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps({str(k): v for k, v in state.items()}), encoding="utf-8")
        os.replace(tmp, target)
    except Exception as exc:
        debug_logger.log_warning(f"[COOKIE_KEEPALIVE] 保存状态失败: {exc}")


def _domain_key(domain: Any) -> str:
    return str(domain or "").strip().lower().lstrip(".")


def load_cookie_list(raw: Any) -> Optional[List[Dict[str, Any]]]:
    """解析存储的 Cookie；不是 JSON 数组（或 {"cookies": [...]}）时返回 None。"""
    text = str(raw or "").strip()
    if not text or text[:1] not in {"[", "{"}:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if isinstance(payload, dict):
        payload = payload.get("cookies")
    if not isinstance(payload, list):
        return None
    return [item for item in payload if isinstance(item, dict) and item.get("name")]


def build_cookie_header(cookies: List[Dict[str, Any]]) -> str:
    """只取 .google.com 域的 Cookie（同名只取一份），用于请求 accounts.google.com。"""
    pairs: Dict[str, str] = {}
    for cookie in cookies:
        if _domain_key(cookie.get("domain")) not in GOOGLE_DOMAINS:
            continue
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        if name and value and name not in pairs:
            pairs[name] = value
    return "; ".join(f"{name}={value}" for name, value in pairs.items())


def parse_set_cookie(line: str) -> Optional[Dict[str, Any]]:
    """解析一行 Set-Cookie，只保留合并需要的字段。"""
    parts = [segment.strip() for segment in str(line or "").split(";")]
    if not parts or "=" not in parts[0]:
        return None
    name, value = parts[0].split("=", 1)
    cookie: Dict[str, Any] = {
        "name": name.strip(),
        "value": value,
        "domain": ".google.com",
        "path": "/",
        "secure": False,
        "httpOnly": False,
        "expires": None,
    }
    max_age: Optional[int] = None
    for attr in parts[1:]:
        key, _, attr_value = attr.partition("=")
        key = key.strip().lower()
        attr_value = attr_value.strip()
        if key == "domain" and attr_value:
            cookie["domain"] = attr_value
        elif key == "path" and attr_value:
            cookie["path"] = attr_value
        elif key == "secure":
            cookie["secure"] = True
        elif key == "httponly":
            cookie["httpOnly"] = True
        elif key == "max-age":
            try:
                max_age = int(attr_value)
            except ValueError:
                pass
        elif key == "expires" and attr_value:
            try:
                cookie["expires"] = parsedate_to_datetime(attr_value).timestamp()
            except (TypeError, ValueError):
                pass
    if max_age is not None:
        cookie["expires"] = time.time() + max_age
    if not cookie["name"] or not cookie["value"]:
        return None
    return cookie


def merge_rotated_cookies(
    cookies: List[Dict[str, Any]], rotated: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """把轮换得到的新值写回同名同域的 Cookie；没有同名项时追加一条。"""
    merged = [dict(cookie) for cookie in cookies]
    for new in rotated:
        domain = _domain_key(new["domain"])
        matched = False
        for cookie in merged:
            if cookie.get("name") == new["name"] and _domain_key(cookie.get("domain")) == domain:
                cookie["value"] = new["value"]
                if new["expires"] is not None:
                    cookie["expires"] = new["expires"]
                matched = True
        if not matched:
            merged.append(
                {
                    "name": new["name"],
                    "value": new["value"],
                    "domain": new["domain"],
                    "path": new["path"],
                    "sameSite": "",
                    "expires": new["expires"] if new["expires"] is not None else -1,
                    "secure": new["secure"],
                    "httpOnly": new["httpOnly"],
                }
            )
    return merged


def _set_cookie_lines(headers: Any) -> List[str]:
    getter = getattr(headers, "get_list", None)
    if callable(getter):
        return [str(line) for line in getter("set-cookie")]
    single = headers.get("set-cookie") if hasattr(headers, "get") else None
    return [str(single)] if single else []


async def rotate_google_cookies(
    raw_cookies: Any, proxy: Optional[str] = None
) -> Dict[str, Any]:
    """请求一次 RotateCookies。

    返回 ``{"ok": bool, "raw": <新的 Cookie JSON 文本>, "rotated": [名字...], "error": str}``。
    只有响应里出现新的 ``__Secure-1PSIDTS`` 才算成功；失败时不返回 ``raw``，调用方不要写库。
    """
    cookies = load_cookie_list(raw_cookies)
    if cookies is None:
        return {"ok": False, "error": "Cookie 不是 JSON 数组格式，跳过保活"}
    header = build_cookie_header(cookies)
    if "__Secure-1PSID=" not in header:
        return {"ok": False, "error": "缺少 .google.com 的 __Secure-1PSID，跳过保活"}

    session_kwargs: Dict[str, Any] = {"impersonate": IMPERSONATE, "trust_env": False}
    normalized_proxy = str(proxy or "").strip()
    if normalized_proxy:
        if "://" not in normalized_proxy:
            normalized_proxy = f"http://{normalized_proxy}"
        session_kwargs["proxy"] = normalized_proxy

    try:
        async with AsyncSession(**session_kwargs) as session:
            response = await session.post(
                ROTATE_URL,
                headers={
                    "Content-Type": "application/json",
                    "Cookie": header,
                    "Origin": "https://accounts.google.com",
                },
                data=ROTATE_BODY,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
    except Exception as exc:
        return {"ok": False, "error": f"请求 RotateCookies 异常: {exc}"}

    if response.status_code != 200:
        return {
            "ok": False,
            "status": response.status_code,
            "error": f"RotateCookies 返回 HTTP {response.status_code}",
        }
    rotated = [
        item
        for item in (parse_set_cookie(line) for line in _set_cookie_lines(response.headers))
        if item
    ]
    if not any(item["name"] == "__Secure-1PSIDTS" for item in rotated):
        return {"ok": False, "error": "响应里没有新的 __Secure-1PSIDTS"}

    merged = merge_rotated_cookies(cookies, rotated)
    return {
        "ok": True,
        "raw": json.dumps(merged, ensure_ascii=False, separators=(",", ":")),
        "rotated": sorted({item["name"] for item in rotated}),
        "error": "",
    }


def log_result(token_id: int, result: Dict[str, Any]) -> None:
    if result.get("ok"):
        debug_logger.log_info(
            f"[COOKIE_KEEPALIVE] Token {token_id}: 已轮换 {len(result['rotated'])} 个 Cookie "
            f"({', '.join(result['rotated'])})"
        )
    else:
        debug_logger.log_warning(
            f"[COOKIE_KEEPALIVE] Token {token_id}: 保活失败 - {result.get('error')}"
        )
