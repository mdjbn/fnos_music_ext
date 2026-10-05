"""网易云登录态探测、降级门控与推送提醒测试。"""
import time

import httpx
import pytest

from proxy import netease_auth, pushplus


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("FNMUSIC_FREE_ONLY_ON_LOGOUT", raising=False)
    monkeypatch.delenv("FNMUSIC_DAILY_ENABLED", raising=False)
    monkeypatch.delenv("FNMUSIC_PUSHPLUS_TOKEN", raising=False)
    monkeypatch.delenv("FNMUSIC_PUSHPLUS_ENABLED", raising=False)
    netease_auth.reset_for_test()
    pushplus.reset_throttle()
    yield
    netease_auth.reset_for_test()


# fetch_state 现在先探 /api/v1/auth/detail（字段全，含 VIP），失败才兜底
# /api/v1/auth/status（CLI，字段少）。mock 必须两个都应答，否则测的是兜底路径。
PRIMARY_PATH = "/api/v1/auth/detail"


def _status_client(payload=None, *, logged_in=True, status=200, calls=None,
                   raise_exc=None, primary_status=None, legacy_only=False):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        if raise_exc is not None:
            raise raise_exc
        path = request.url.path
        if path not in ("/api/v1/auth/detail", "/api/v1/auth/status"):
            return httpx.Response(404)
        if legacy_only and path == "/api/v1/auth/detail":
            # 模拟没有 detail 端点的旧版音源服务 -> 必须回落到 auth/status
            return httpx.Response(404)
        if path == "/api/v1/auth/detail" and primary_status:
            return httpx.Response(primary_status)
        if status != 200:
            return httpx.Response(status)
        if payload is not None:
            return httpx.Response(200, json=payload)
        data = {"logged_in": logged_in}
        if logged_in:
            data.update({"nickname": "张三", "user_id": "10086",
                         "profile": {"nickname": "张三", "userId": "10086",
                                     "vipType": 11,
                                     "vipExpiryTime": int((time.time() + 30 * 86400) * 1000)}})
        return httpx.Response(200, json={"ok": True, "data": data})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             base_url="http://127.0.0.1:8770")


# ------------------------------------------------------------ 配置开关 ----

@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("0", False), ("no", False), ("off", False), ("", True),
])
def test_free_only_on_logout_flag(monkeypatch, raw, expected):
    monkeypatch.setenv("FNMUSIC_FREE_ONLY_ON_LOGOUT", raw)
    assert netease_auth.free_only_on_logout() is expected


@pytest.mark.parametrize("raw,expected", [("true", True), ("false", False), ("", True)])
def test_daily_enabled_flag(monkeypatch, raw, expected):
    monkeypatch.setenv("FNMUSIC_DAILY_ENABLED", raw)
    assert netease_auth.daily_enabled() is expected


def test_defaults_are_secure_by_default(monkeypatch):
    """默认必须开启降级（保证服务可用）与日推。"""
    monkeypatch.delenv("FNMUSIC_FREE_ONLY_ON_LOGOUT", raising=False)
    monkeypatch.delenv("FNMUSIC_DAILY_ENABLED", raising=False)
    assert netease_auth.free_only_on_logout() is True
    assert netease_auth.daily_enabled() is True


# ------------------------------------------------------ parse_status_payload ----

def test_parse_nested_profile_envelope():
    st = netease_auth.parse_status_payload({
        "ok": True,
        "data": {"logged_in": True, "profile": {"nickname": "张三", "userId": 10086,
                                                "vipType": 11, "vipExpiryTime": 1800000000000}},
    })
    assert st.logged_in is True
    assert st.nickname == "张三"
    assert st.user_id == "10086"
    assert st.vip_type == 11
    assert st.vip_expires_ms == 1800000000000


def test_parse_account_envelope():
    st = netease_auth.parse_status_payload({
        "ok": True, "data": {"account": {"nickname": "李四", "id": 555, "vipType": 0}},
    })
    assert st.logged_in is True
    assert st.nickname == "李四"
    assert st.user_id == "555"
    assert st.vip_type == 0


def test_parse_flat_envelope():
    st = netease_auth.parse_status_payload(
        {"ok": True, "data": {"logged_in": True, "nickname": "王五", "user_id": "777"}}
    )
    assert st.logged_in is True
    assert st.nickname == "王五"
    assert st.user_id == "777"


def test_parse_logged_out_infers_from_absence():
    st = netease_auth.parse_status_payload({"ok": True, "data": {"logged_in": False}})
    assert st.logged_in is False
    assert st.nickname == ""


def test_parse_logged_out_by_empty_profile():
    """上游明确返回 account/profile 为 null 时判定未登录。"""
    st = netease_auth.parse_status_payload(
        {"ok": True, "data": {"code": 200, "account": None, "profile": None}}
    )
    assert st.logged_in is False


def test_parse_garbage_payloads_never_raise():
    for junk in (None, [], "str", 123, {}, {"ok": False}, {"data": None}, {"data": "x"},
                 {"data": {"logged_in": "yes"}}, {"data": {"vipType": "abc", "vipExpiryTime": None}}):
        st = netease_auth.parse_status_payload(junk)
        assert isinstance(st, netease_auth.LoginState)


def test_parse_tolerates_string_numbers():
    st = netease_auth.parse_status_payload({
        "data": {"profile": {"nickname": "n", "userId": "1", "vipType": "11",
                             "vipExpiryTime": "1800000000000"}}
    })
    assert st.vip_type == 11
    assert st.vip_expires_ms == 1800000000000


# ------------------------------------------------------------ LoginState ----

def test_vip_active_requires_login_and_type_and_not_expired():
    now_ms = int(time.time() * 1000)
    assert netease_auth.LoginState(logged_in=True, vip_type=11,
                                   vip_expires_ms=now_ms + 86400_000).vip_active is True
    assert netease_auth.LoginState(logged_in=False, vip_type=11,
                                   vip_expires_ms=now_ms + 86400_000).vip_active is False
    assert netease_auth.LoginState(logged_in=True, vip_type=0,
                                   vip_expires_ms=now_ms + 86400_000).vip_active is False
    assert netease_auth.LoginState(logged_in=True, vip_type=11,
                                   vip_expires_ms=now_ms - 86400_000).vip_active is False
    # 无到期时间时不臆断过期
    assert netease_auth.LoginState(logged_in=True, vip_type=11).vip_active is True


def test_vip_days_left():
    now_ms = int(time.time() * 1000)
    st = netease_auth.LoginState(logged_in=True, vip_type=11, vip_expires_ms=now_ms + 3 * 86400_000)
    # 构造与断言之间有毫秒级流逝，向下取整可能落在 2 或 3
    assert st.vip_days_left in (2, 3)
    assert netease_auth.LoginState(logged_in=True).vip_days_left is None
    assert netease_auth.LoginState(logged_in=False, vip_type=11,
                                   vip_expires_ms=now_ms + 86400_000).vip_days_left is None
    # 已过期
    assert netease_auth.LoginState(logged_in=True, vip_type=11,
                                   vip_expires_ms=now_ms - 86400_000).vip_days_left is None


def test_to_public_dict_exposes_no_credentials():
    st = netease_auth.LoginState(logged_in=True, nickname="张三", user_id="1",
                                 vip_type=11, vip_expires_ms=int(time.time() * 1000) + 86400_000)
    pub = st.to_public_dict()
    assert set(pub) == {"logged_in", "nickname", "vip", "vip_type", "vip_days_left",
                        "vip_expires_known", "free_only", "checked_at", "age_s"}
    for banned in ("cookie", "token", "csrf", "password", "vip_expires_ms",
                   "user_id", "probed_via", "error"):
        assert banned not in pub


def test_to_public_dict_free_only_flag(monkeypatch):
    monkeypatch.setenv("FNMUSIC_FREE_ONLY_ON_LOGOUT", "true")
    assert netease_auth.LoginState(logged_in=False).to_public_dict()["free_only"] is True
    assert netease_auth.LoginState(logged_in=True).to_public_dict()["free_only"] is False
    monkeypatch.setenv("FNMUSIC_FREE_ONLY_ON_LOGOUT", "false")
    assert netease_auth.LoginState(logged_in=False).to_public_dict()["free_only"] is False


# ------------------------------------------------------------- fetch_state ----

@pytest.mark.anyio
async def test_fresh_process_state_is_not_treated_as_fresh_cache():
    """回归：模块级哨兵状态必须 checked_at=0，否则进程刚起来时
    fetch_state 会在整个 TTL 周期内跳过探测、一直误报未登录。"""
    import importlib

    fresh = importlib.reload(netease_auth)
    try:
        st = fresh.current_state()
        assert st.checked_at == 0.0, "哨兵状态必须标记为「从未探测过」"
        calls = []
        await fresh.fetch_state(_status_client(calls=calls))
        assert calls == [PRIMARY_PATH], f"首次 fetch_state 必须真的查上游，实际 {calls}"
        assert fresh.current_state().logged_in is True
    finally:
        importlib.reload(netease_auth)
        netease_auth.reset_for_test()


@pytest.mark.anyio
async def test_fetch_state_caches_within_ttl():
    calls = []
    client = _status_client(calls=calls)
    st1 = await netease_auth.fetch_state(client)
    st2 = await netease_auth.fetch_state(client)
    assert st1.logged_in is True and st2.logged_in is True
    assert calls.count(PRIMARY_PATH) == 1, "TTL 内只应打一次主探针"


@pytest.mark.anyio
async def test_fetch_state_force_bypasses_cache():
    calls = []
    client = _status_client(calls=calls)
    await netease_auth.fetch_state(client)
    await netease_auth.fetch_state(client, force=True)
    assert calls.count(PRIMARY_PATH) == 2


@pytest.mark.anyio
async def test_fetch_state_ttl_expiry(monkeypatch):
    monkeypatch.setattr(netease_auth, "STATE_TTL_S", 0.01)
    calls = []
    client = _status_client(calls=calls)
    await netease_auth.fetch_state(client)
    time.sleep(0.02)
    await netease_auth.fetch_state(client)
    assert calls.count(PRIMARY_PATH) == 2


@pytest.mark.anyio
async def test_fetch_state_no_client():
    st = await netease_auth.fetch_state(None)
    assert st.logged_in is False
    assert st.error == "no_client"


@pytest.mark.anyio
async def test_fetch_state_http_error():
    st = await netease_auth.fetch_state(_status_client(status=500))
    assert st.logged_in is False
    assert st.error == "http_500"


@pytest.mark.anyio
async def test_fetch_state_network_error():
    st = await netease_auth.fetch_state(
        _status_client(raise_exc=httpx.ConnectError("连接被拒绝")))
    assert st.logged_in is False
    assert st.error == "unreachable"


@pytest.mark.anyio
async def test_current_state_and_invalidate():
    client = _status_client()
    await netease_auth.fetch_state(client)
    assert netease_auth.current_state().logged_in is True
    netease_auth.invalidate_state()
    assert netease_auth.current_state().logged_in is False
    assert netease_auth.current_state().error == "invalidated"


@pytest.mark.anyio
async def test_invalidate_forces_refetch_on_next_call():
    calls = []
    client = _status_client(calls=calls)
    await netease_auth.fetch_state(client)
    assert calls.count(PRIMARY_PATH) == 1
    netease_auth.invalidate_state()
    await netease_auth.fetch_state(client)
    assert calls.count(PRIMARY_PATH) == 2


@pytest.mark.anyio
async def test_require_login():
    assert await netease_auth.require_login(_status_client(logged_in=True)) is True
    netease_auth.invalidate_state()
    assert await netease_auth.require_login(_status_client(logged_in=False)) is False
    assert await netease_auth.require_login(None) is False


# ------------------------------------------------------------ 推送提醒 ----

def _capture_push(monkeypatch):
    sent = []

    async def fake_send(client, title, content, **kw):
        sent.append((title, content))
        return True

    monkeypatch.setattr(pushplus, "send", fake_send)
    monkeypatch.setenv("FNMUSIC_PUSHPLUS_ENABLED", "true")
    monkeypatch.setenv("FNMUSIC_PUSHPLUS_TOKEN", "t" * 32)
    return sent


@pytest.mark.anyio
async def test_notify_on_transition_to_logged_out(monkeypatch):
    sent = _capture_push(monkeypatch)
    # 预置「本来是登录的」
    monkeypatch.setattr(netease_auth, "_STATE",
                        netease_auth.LoginState(logged_in=True, nickname="张三", user_id="1"))
    await netease_auth.refresh_and_notify(_status_client(logged_in=False), force=True)
    assert len(sent) == 1
    title, content = sent[0]
    assert "失效" in title or "登录" in title
    assert "张三" in content
    assert "netease_login.sh" in content, "必须给出可执行的恢复指引"


@pytest.mark.anyio
async def test_notify_once_per_state(monkeypatch):
    sent = _capture_push(monkeypatch)
    await netease_auth.refresh_and_notify(_status_client(logged_in=False), force=True)
    await netease_auth.refresh_and_notify(_status_client(logged_in=False), force=True)
    await netease_auth.refresh_and_notify(_status_client(logged_in=False), force=True)
    assert len(sent) == 1, "同一状态反复巡检不应刷屏推送"


@pytest.mark.anyio
async def test_notify_on_login_success(monkeypatch):
    sent = _capture_push(monkeypatch)
    monkeypatch.setattr(netease_auth, "_STATE", netease_auth.LoginState(logged_in=False))
    await netease_auth.refresh_and_notify(_status_client(logged_in=True), force=True)
    assert any("登录成功" in t for t, _ in sent)


@pytest.mark.anyio
async def test_notify_vip_expiring(monkeypatch):
    sent = _capture_push(monkeypatch)
    monkeypatch.setattr(netease_auth, "VIP_WARN_DAYS", 7)
    soon_ms = int((time.time() + 3 * 86400) * 1000)
    payload = {"ok": True, "data": {"logged_in": True, "profile": {
        "nickname": "张三", "userId": "1", "vipType": 11, "vipExpiryTime": soon_ms}}}
    monkeypatch.setattr(netease_auth, "_STATE", netease_auth.LoginState(logged_in=True))
    await netease_auth.refresh_and_notify(_status_client(payload=payload), force=True)
    assert any("VIP" in t and "到期" in t for t, _ in sent), sent


@pytest.mark.anyio
async def test_no_vip_alert_when_far_from_expiry(monkeypatch):
    sent = _capture_push(monkeypatch)
    monkeypatch.setattr(netease_auth, "VIP_WARN_DAYS", 7)
    far_ms = int((time.time() + 300 * 86400) * 1000)
    payload = {"ok": True, "data": {"logged_in": True, "profile": {
        "nickname": "张三", "userId": "1", "vipType": 11, "vipExpiryTime": far_ms}}}
    monkeypatch.setattr(netease_auth, "_STATE", netease_auth.LoginState(logged_in=True))
    await netease_auth.refresh_and_notify(_status_client(payload=payload), force=True)
    assert not any("VIP" in t for t, _ in sent)


@pytest.mark.anyio
async def test_vip_alert_once_per_day(monkeypatch):
    sent = _capture_push(monkeypatch)
    soon_ms = int((time.time() + 2 * 86400) * 1000)
    payload = {"ok": True, "data": {"logged_in": True, "profile": {
        "nickname": "张三", "userId": "1", "vipType": 11, "vipExpiryTime": soon_ms}}}
    client = _status_client(payload=payload)

    # 每次翻转状态以清掉 _LAST_ALERT，但 VIP 告警应按天去重
    for _ in range(3):
        monkeypatch.setattr(netease_auth, "_STATE", netease_auth.LoginState(logged_in=True))
        netease_auth._LAST_ALERT.clear()
        await netease_auth.refresh_and_notify(client, force=True)
    assert sum(1 for t, _ in sent if "VIP" in t) == 1


@pytest.mark.anyio
async def test_no_push_when_disabled(monkeypatch):
    """没配 token 时一次也不发（也不该因为推送失败影响登录态返回）。"""
    sent = []

    async def fake_send(client, title, content, **kw):
        sent.append(title)
        return True

    monkeypatch.setattr(pushplus, "send", fake_send)
    monkeypatch.delenv("FNMUSIC_PUSHPLUS_TOKEN", raising=False)
    st = await netease_auth.refresh_and_notify(_status_client(logged_in=False), force=True)
    assert st.logged_in is False
    assert sent == []


@pytest.mark.anyio
async def test_refresh_notify_never_raises_on_push_failure(monkeypatch):
    async def boom(client, title, content, **kw):
        raise RuntimeError("推送炸了")

    monkeypatch.setattr(pushplus, "send", boom)
    monkeypatch.setenv("FNMUSIC_PUSHPLUS_ENABLED", "true")
    monkeypatch.setenv("FNMUSIC_PUSHPLUS_TOKEN", "t" * 32)
    st = await netease_auth.refresh_and_notify(_status_client(logged_in=False), force=True)
    assert isinstance(st, netease_auth.LoginState), "推送异常绝不能打断登录态刷新"


@pytest.mark.anyio
async def test_push_content_has_no_credentials(monkeypatch):
    sent = _capture_push(monkeypatch)
    monkeypatch.setattr(netease_auth, "_STATE",
                        netease_auth.LoginState(logged_in=True, nickname="张三", user_id="1"))
    await netease_auth.refresh_and_notify(_status_client(logged_in=False), force=True)
    _, content = sent[0]
    for banned in ("cookie", "csrf", "MUSIC_U", "t" * 30):
        assert banned not in content


# ------------------------------------------------------------- watch loop ----

@pytest.mark.anyio
async def test_start_watch_is_idempotent_and_stoppable(monkeypatch):
    import asyncio

    stop = asyncio.Event()
    monkeypatch.setattr(netease_auth, "CHECK_INTERVAL_S", 3600)
    t1 = netease_auth.start_watch(_status_client(), stop)
    t2 = netease_auth.start_watch(_status_client(), stop)
    assert t1 is t2, "重复启动应复用同一个巡检任务"

    netease_auth.stop_watch()
    stop.set()
    await asyncio.sleep(0)
    assert t1.done() or t1.cancelled()


@pytest.mark.anyio
async def test_watch_stops_immediately_on_event(monkeypatch):
    import asyncio

    stop = asyncio.Event()
    stop.set()
    task = netease_auth.start_watch(_status_client(), stop)
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()
