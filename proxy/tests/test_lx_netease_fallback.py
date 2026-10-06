"""跨音源兜底：洛雪歌单里的网易云（wy）曲目在「当前音源 = 网易云」时借网易云链路播放。

用户报（2026-10-06）：「用网易云音源并勾选『同步洛雪歌单』时，虽然能加载洛雪歌单，
但是都无法播放。」日志每首都是
``stream probe: HEAD online:lx:wy:<id>`` → 3× ``WARNING resolve_lx_url error`` →
``HEAD /music/api/v1/track/stream status=404``：洛雪服务此刻没在运行（音源三选一互斥，
选了网易云就停了 lxmusic），而洛雪 wy 曲目的 id 就是网易云 songId，所以可以借网易云
链路播放；tx/kg/kw/mg 的 id 空间不同，绝不能混用。

反向问题（用户同一轮报的）：「用 lx 音源并勾选『音乐页显示网易账号歌单』时，网易云
歌单加载不出来」——musicbox 没在运行，账号/频道歌单不该硬拉。
"""
from __future__ import annotations

import pytest
from starlette.requests import Request

from proxy import app as appmod
from proxy.app import CONF


def _req(path: str = "/music/api/v1/track/stream") -> Request:
    return Request({"type": "http", "app": appmod.app, "headers": [], "method": "GET",
                    "path": path, "query_string": b"", "server": ("test", 1), "scheme": "http"})


def _sources(monkeypatch, *, lx: bool, netease: bool) -> None:
    monkeypatch.setitem(CONF, "lx_enabled", lx)
    monkeypatch.setitem(CONF, "netease_enabled", netease)


def _stub_playlist_plumbing(monkeypatch) -> None:
    """playlist_list 的其余依赖：上游信封与洛雪同步开关（避免真去连同步服务）。"""
    async def fake_envelope(request, client):
        return {"code": 0, "msg": "", "data": {"list": [], "total": 0}}

    monkeypatch.setattr(appmod, "fetch_upstream_envelope", fake_envelope)
    monkeypatch.setattr(appmod, "get_upstream_client", lambda _app: None)
    monkeypatch.setattr(appmod.lxsync, "sync_enabled", lambda: False)


# ===========================================================================
# 兜底判定
# ===========================================================================


def test_fallback_requires_lx_off_netease_on_wy_and_numeric(monkeypatch):
    _sources(monkeypatch, lx=False, netease=True)
    assert appmod._lx_netease_fallback("online:lx:wy:3348197008") is True
    assert appmod._source_enabled("online:lx:wy:3348197008") is True


@pytest.mark.parametrize("guid", [
    "online:lx:tx:002Y0Eb148Eyan",   # QQ 音乐
    "online:lx:kg:6B0A1C2D3E4F",    # 酷狗（hash）
    "online:lx:kw:228908",          # 酷我
    "online:lx:mg:6008361C1C1C",    # 咪咕（copyrightId）
    "online:lx:wy:abcdef",          # wy 但 id 不是数字 → 不猜
    "online:lx:wy:",                # 空 id
    "online:lx:wy",                 # 少一段
    "online:netease:3348197008",    # 本来就是网易云
    "online:musicdl:3348197008",    # 网盘
    "", None,
])
def test_fallback_only_for_wy_numeric_ids(monkeypatch, guid):
    _sources(monkeypatch, lx=False, netease=True)
    assert appmod._lx_netease_fallback(guid) is False, guid
    if isinstance(guid, str) and guid.startswith("online:lx:"):
        assert appmod._source_enabled(guid) is False, guid


def test_fallback_off_when_lx_is_the_music_source(monkeypatch):
    # 洛雪音源在跑：交给洛雪自己解析（直链带 referer/UA，wyy 也一样）
    _sources(monkeypatch, lx=True, netease=False)
    assert appmod._lx_netease_fallback("online:lx:wy:1") is False
    monkeypatch.setitem(CONF, "lx_sources", [])
    assert appmod._source_enabled("online:lx:wy:1") is True


def test_fallback_off_when_netease_source_disabled(monkeypatch):
    _sources(monkeypatch, lx=False, netease=False)
    assert appmod._lx_netease_fallback("online:lx:wy:1") is False
    assert appmod._source_enabled("online:lx:wy:1") is False


def test_lx_netease_guid_maps_to_song_id():
    assert appmod._lx_netease_guid("online:lx:wy:3348197008") == "online:netease:3348197008"


# ===========================================================================
# HEAD 探测：不可播的源立刻 404（不再逐个音质白等 3×22s）
# ===========================================================================


@pytest.mark.anyio
async def test_head_probe_gated_on_source(monkeypatch):
    _sources(monkeypatch, lx=False, netease=True)
    called = []

    async def fake_open(*args, **kwargs):
        called.append(args)
        raise AssertionError("不可播的源不该去解析")

    monkeypatch.setattr(appmod, "_open_online_stream", fake_open)
    resp = await appmod._stream_head_response(_req(), "online:lx:tx:002Y0Eb148Eyan", None, None)
    assert resp.status_code == 404
    assert not called


# ===========================================================================
# 播放链路：wy 曲目改走网易云解析，元信息取等价的网易云 guid
# ===========================================================================


@pytest.mark.anyio
async def test_stream_falls_back_to_netease_resolution(monkeypatch):
    _sources(monkeypatch, lx=False, netease=True)
    seen: dict = {}

    async def fake_resolve_netease(client, song_id, request, stats=None):
        seen["song_id"] = song_id
        return "http://m8.music.126.net/obj/x.mp3"

    async def fake_fetch_info(request, guid, include_lyric=True):
        seen["info_guid"] = guid
        return {"ext": "mp3"}

    class _Resp:
        status_code = 200
        headers = {"content-type": "audio/mpeg"}

        def aiter_bytes(self):
            async def gen():
                yield b"ID3\x04\x00\x00\x00\x00\x00\x00"
            return gen()

        async def aclose(self):
            return None

    class _Client:
        def build_request(self, method, url, headers=None):
            seen["url"] = url
            return object()

        async def send(self, req, stream=True):
            return _Resp()

    monkeypatch.setattr(appmod, "resolve_netease_url", fake_resolve_netease)
    monkeypatch.setattr(appmod, "get_cdn_client", lambda _app: _Client())
    monkeypatch.setattr(appmod, "_fetch_online_info", fake_fetch_info)
    monkeypatch.setattr(appmod, "_retained_track", lambda *a, **k: (None, None))

    result = await appmod._open_online_stream(_req(), "online:lx:wy:3348197008", None)
    assert result is not None
    resp, owned, ext, info, chunks, first = result
    assert seen["song_id"] == "3348197008"
    assert seen["url"] == "http://m8.music.126.net/obj/x.mp3"
    assert seen["info_guid"] == "online:netease:3348197008", "元信息必须按等价的网易云 guid 取"
    assert ext == "mp3"
    assert first


# ===========================================================================
# 注入门控：musicbox 没在运行时不去拉账号/频道歌单
# ===========================================================================


@pytest.mark.anyio
async def test_playlist_list_skips_channel_fetch_without_netease(monkeypatch):
    _sources(monkeypatch, lx=True, netease=False)
    pulled = []

    async def fake_channels(client):
        pulled.append(client)
        raise AssertionError("musicbox 没在运行不该去拉频道歌单")

    async def fake_auth(request, client):
        return True, "user-a", None

    monkeypatch.setattr(appmod, "_channel_playlist_records", fake_channels)
    monkeypatch.setattr(appmod, "_probe_upstream_auth", fake_auth)
    monkeypatch.setattr(appmod, "_recommend_injectable_kinds", lambda _g: ())
    _stub_playlist_plumbing(monkeypatch)
    resp = await appmod.playlist_list(_req("/music/api/v1/playlist/list"))
    assert resp.status_code == 200
    assert not pulled


@pytest.mark.anyio
async def test_playlist_list_fetches_channels_with_netease(monkeypatch):
    _sources(monkeypatch, lx=False, netease=True)
    pulled = []

    async def fake_channels(client):
        pulled.append(client)
        return [], set(), False

    async def fake_auth(request, client):
        return True, "user-a", None

    monkeypatch.setattr(appmod, "_channel_playlist_records", fake_channels)
    monkeypatch.setattr(appmod, "_probe_upstream_auth", fake_auth)
    monkeypatch.setattr(appmod, "_recommend_injectable_kinds", lambda _g: ())
    _stub_playlist_plumbing(monkeypatch)
    resp = await appmod.playlist_list(_req("/music/api/v1/playlist/list"))
    assert resp.status_code == 200
    assert pulled, "网易云音源下频道歌单照旧注入"
