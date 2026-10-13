import asyncio
import json

import pytest

from src.services import cookie_keepalive as ck


def _stored():
    return [
        {"name": "SID", "value": "sid-old", "domain": ".google.com", "path": "/", "secure": True, "httpOnly": False, "sameSite": "", "expires": 1900000000},
        {"name": "__Secure-1PSID", "value": "1psid", "domain": ".google.com", "path": "/", "secure": True, "httpOnly": True, "sameSite": "", "expires": 1900000000},
        {"name": "__Secure-1PSIDTS", "value": "ts-old", "domain": ".google.com", "path": "/", "secure": True, "httpOnly": True, "sameSite": "", "expires": 1800000000},
        {"name": "__Secure-1PSIDTS", "value": "ts-mirror", "domain": "www.google.com", "path": "/", "secure": True, "httpOnly": True, "sameSite": "None", "expires": 1800000000},
        {"name": "OSID", "value": "osid", "domain": "flow.google.com", "path": "/", "secure": True, "httpOnly": True, "sameSite": "", "expires": 1900000000},
    ]


def test_interval_env(monkeypatch):
    monkeypatch.delenv("FLOW_COOKIE_KEEPALIVE_MINUTES", raising=False)
    assert ck.keepalive_interval_minutes() == 0
    monkeypatch.setenv("FLOW_COOKIE_KEEPALIVE_MINUTES", "10")
    assert ck.keepalive_interval_minutes() == 10
    monkeypatch.setenv("FLOW_COOKIE_KEEPALIVE_MINUTES", "1")
    assert ck.keepalive_interval_minutes() == 2
    monkeypatch.setenv("FLOW_COOKIE_KEEPALIVE_MINUTES", "9999")
    assert ck.keepalive_interval_minutes() == 240
    monkeypatch.setenv("FLOW_COOKIE_KEEPALIVE_MINUTES", "abc")
    assert ck.keepalive_interval_minutes() == 0


def test_load_cookie_list_formats():
    assert ck.load_cookie_list("") is None
    assert ck.load_cookie_list("SID=1; HSID=2") is None
    assert len(ck.load_cookie_list(json.dumps(_stored()))) == 5
    assert len(ck.load_cookie_list(json.dumps({"cookies": _stored()}))) == 5


def test_header_only_uses_google_com_domain():
    header = ck.build_cookie_header(_stored())
    assert "SID=sid-old" in header and "__Secure-1PSID=1psid" in header
    assert "ts-old" in header
    assert "ts-mirror" not in header  # www.google.com 的镜像副本不参与
    assert "OSID" not in header  # flow.google.com 的 Cookie 不发给 accounts.google.com


def test_parse_set_cookie():
    line = "__Secure-1PSIDTS=ts-new; Domain=.google.com; Path=/; Expires=Fri, 02 Oct 2026 08:00:00 GMT; Secure; HttpOnly; Priority=HIGH"
    cookie = ck.parse_set_cookie(line)
    assert cookie["name"] == "__Secure-1PSIDTS" and cookie["value"] == "ts-new"
    assert cookie["domain"] == ".google.com" and cookie["secure"] and cookie["httpOnly"]
    assert cookie["expires"] and cookie["expires"] > 1.7e9
    assert ck.parse_set_cookie("garbage") is None
    assert ck.parse_set_cookie("x=; Domain=.google.com") is None  # 空值不合并


def test_merge_updates_matching_domain_only_and_appends_new():
    rotated = [
        {"name": "__Secure-1PSIDTS", "value": "ts-new", "domain": ".google.com", "path": "/", "secure": True, "httpOnly": True, "expires": 1850000000.0},
        {"name": "SIDCC", "value": "sidcc-new", "domain": ".google.com", "path": "/", "secure": False, "httpOnly": False, "expires": None},
    ]
    merged = ck.merge_rotated_cookies(_stored(), rotated)
    by = {(c["name"], c["domain"]): c for c in merged}
    assert by[("__Secure-1PSIDTS", ".google.com")]["value"] == "ts-new"
    assert by[("__Secure-1PSIDTS", ".google.com")]["expires"] == 1850000000.0
    assert by[("__Secure-1PSIDTS", "www.google.com")]["value"] == "ts-mirror"  # 其他域不动
    assert by[("SIDCC", ".google.com")]["value"] == "sidcc-new"  # 原来没有则追加
    assert by[("SID", ".google.com")]["value"] == "sid-old"
    assert len(merged) == 6
    assert _stored()[2]["value"] == "ts-old"  # 不改原对象


class _FakeHeaders:
    def __init__(self, lines):
        self._lines = lines

    def get_list(self, name):
        return self._lines if name == "set-cookie" else []


class _FakeResponse:
    def __init__(self, status, lines):
        self.status_code = status
        self.headers = _FakeHeaders(lines)


class _FakeSession:
    response = None
    seen = {}

    def __init__(self, **kwargs):
        _FakeSession.seen["kwargs"] = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, data=None, timeout=None):
        _FakeSession.seen.update(url=url, headers=headers, data=data)
        return _FakeSession.response


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_rotate_success_returns_merged_json_without_leaking_values(monkeypatch):
    monkeypatch.setattr(ck, "AsyncSession", _FakeSession)
    _FakeSession.response = _FakeResponse(
        200,
        [
            "__Secure-1PSIDTS=ts-new; Domain=.google.com; Path=/; Secure; HttpOnly",
            "__Secure-3PSIDTS=ts3-new; Domain=.google.com; Path=/; Secure; HttpOnly",
        ],
    )
    result = _run(ck.rotate_google_cookies(json.dumps(_stored()), proxy="socks5://relay:1080"))
    assert result["ok"] is True
    assert result["rotated"] == ["__Secure-1PSIDTS", "__Secure-3PSIDTS"]
    merged = {(c["name"], c["domain"]): c["value"] for c in json.loads(result["raw"])}
    assert merged[("__Secure-1PSIDTS", ".google.com")] == "ts-new"
    assert "ts-new" not in json.dumps(result["rotated"])
    assert _FakeSession.seen["url"] == ck.ROTATE_URL
    assert _FakeSession.seen["kwargs"]["proxy"] == "socks5://relay:1080"
    assert "__Secure-1PSID=1psid" in _FakeSession.seen["headers"]["Cookie"]


@pytest.mark.parametrize(
    "status,lines",
    [(401, []), (200, []), (200, ["SIDCC=x; Domain=.google.com"])],
)
def test_rotate_failure_does_not_return_raw(monkeypatch, status, lines):
    monkeypatch.setattr(ck, "AsyncSession", _FakeSession)
    _FakeSession.response = _FakeResponse(status, lines)
    result = _run(ck.rotate_google_cookies(json.dumps(_stored())))
    assert result["ok"] is False and "raw" not in result and result["error"]


def test_rotate_skips_non_json_and_missing_session():
    assert _run(ck.rotate_google_cookies("SID=1; HSID=2"))["ok"] is False
    only_flow = [c for c in _stored() if c["domain"] == "flow.google.com"]
    assert _run(ck.rotate_google_cookies(json.dumps(only_flow)))["ok"] is False


def test_state_roundtrip_and_corruption(tmp_path):
    path = tmp_path / "state.json"
    assert ck.load_state(path) == {}  # 不存在
    ck.save_state({2: 1791850000.5, 7: 1.0}, path)
    assert ck.load_state(path) == {2: 1791850000.5, 7: 1.0}
    path.write_text("{not json", encoding="utf-8")
    assert ck.load_state(path) == {}  # 损坏时不抛异常


def test_rate_limited_result_carries_status(monkeypatch):
    monkeypatch.setattr(ck, "AsyncSession", _FakeSession)
    _FakeSession.response = _FakeResponse(429, [])
    result = _run(ck.rotate_google_cookies(json.dumps(_stored())))
    assert result["ok"] is False and result["status"] == 429


class _FakeDB:
    def __init__(self, token):
        self.token = token
        self.updated = []

    async def get_active_tokens(self):
        return [self.token]

    async def get_token(self, token_id):
        return self.token

    async def update_token(self, token_id, **fields):
        self.updated.append(fields)


def _manager(monkeypatch, tmp_path, result):
    import types

    from src.services import token_manager as tm

    monkeypatch.setenv("FLOW_COOKIE_KEEPALIVE_MINUTES", "30")
    monkeypatch.setattr(ck, "default_state_path", lambda: tmp_path / "state.json")

    async def fake_rotate(raw, proxy=None):
        fake_rotate.calls += 1
        return result

    fake_rotate.calls = 0
    monkeypatch.setattr(ck, "rotate_google_cookies", fake_rotate)
    token = types.SimpleNamespace(
        id=2, is_active=True, google_cookies=json.dumps(_stored()), proxy_url=""
    )
    manager = tm.TokenManager.__new__(tm.TokenManager)  # 不初始化数据库 / flow_client
    import asyncio

    manager.db = _FakeDB(token)
    manager.flow_client = types.SimpleNamespace(proxy_manager=None)
    manager._refresh_lock_guard = asyncio.Lock()
    manager._refresh_locks = {}
    manager._cookie_keepalive_last = None
    return manager, fake_rotate


def test_keepalive_does_not_rotate_again_right_after_restart(monkeypatch, tmp_path):
    import time

    ck.save_state({2: time.time() - 60}, tmp_path / "state.json")  # 1 分钟前刚轮换过
    manager, fake_rotate = _manager(monkeypatch, tmp_path, {"ok": True, "raw": "[]", "rotated": ["SIDCC"]})
    _run(manager.run_cookie_keepalive_once())
    assert fake_rotate.calls == 0 and manager.db.updated == []


def test_keepalive_rotates_when_due_and_persists(monkeypatch, tmp_path):
    import time

    ck.save_state({2: time.time() - 31 * 60}, tmp_path / "state.json")  # 已超过 30 分钟
    manager, fake_rotate = _manager(monkeypatch, tmp_path, {"ok": True, "raw": "[1]", "rotated": ["__Secure-1PSIDTS"]})
    _run(manager.run_cookie_keepalive_once())
    assert fake_rotate.calls == 1 and manager.db.updated == [{"google_cookies": "[1]"}]
    assert time.time() - ck.load_state(tmp_path / "state.json")[2] < 5  # 时间已落盘


def test_keepalive_backs_off_one_extra_interval_on_429(monkeypatch, tmp_path):
    import time

    manager, fake_rotate = _manager(
        monkeypatch, tmp_path, {"ok": False, "status": 429, "error": "RotateCookies 返回 HTTP 429"}
    )
    _run(manager.run_cookie_keepalive_once())  # 没有状态文件：立即尝试一次
    assert fake_rotate.calls == 1 and manager.db.updated == []  # 失败不写库
    assert ck.load_state(tmp_path / "state.json")[2] > time.time() + 29 * 60  # 退让到下下个周期
    _run(manager.run_cookie_keepalive_once())
    assert fake_rotate.calls == 1  # 退让期内不再请求
