"""洛雪/网盘音源下的网易账号歌单：「音乐页显示网易账号歌单」= 附加音源保活。

用户需求（2026-10-06）：「能否在使用lx 音源时也能获取网易云歌单？」

音乐盒进程按「音源三选一」只在该音源下运行，所以洛雪音源下账号歌单本来没有数据源。
做法是把那个开关升级为**附加音源开关**：WebUI 在开关打开且当前音源不是音乐盒时让
musicbox 常驻（webui-service/app.py 的 netease_aux_needed / ensure_netease_aux /
release_netease_aux），proxy 侧据此放行网易云歌单数据与曲目：

    _netease_playlists_on() = netease_enabled or netease_my_playlists

开关关掉且音源不是网易云时行为与修复前一致：频道/账号歌单不注入、网易云曲目不可播。
"""
from __future__ import annotations

import pytest
from starlette.requests import Request

from proxy import app as appmod
from proxy.app import CONF


def _req(path: str = "/music/api/v1/playlist/list") -> Request:
    return Request({"type": "http", "app": appmod.app, "headers": [], "method": "GET",
                    "path": path, "query_string": b"", "server": ("test", 1), "scheme": "http"})


def _sources(monkeypatch, *, lx: bool = False, netease: bool = False,
             my_playlists: bool = False) -> None:
    monkeypatch.setitem(CONF, "lx_enabled", lx)
    monkeypatch.setitem(CONF, "netease_enabled", netease)
    monkeypatch.setitem(CONF, "netease_my_playlists", my_playlists)


def _stub_playlist_plumbing(monkeypatch) -> None:
    """playlist_list 的其余依赖：上游信封、鉴权、洛雪同步开关（别真去连服务）。"""
    async def fake_envelope(request, client):
        return {"code": 0, "msg": "", "data": {"list": [], "total": 0}}

    async def fake_auth(request, client):
        return True, "user-a", None

    monkeypatch.setattr(appmod, "fetch_upstream_envelope", fake_envelope)
    monkeypatch.setattr(appmod, "get_upstream_client", lambda _app: None)
    monkeypatch.setattr(appmod, "_probe_upstream_auth", fake_auth)
    monkeypatch.setattr(appmod, "_recommend_injectable_kinds", lambda _g: ())
    monkeypatch.setattr(appmod.lxsync, "sync_enabled", lambda: False)


# ===========================================================================
# 判定：什么时候算「网易云歌单可用」
# ===========================================================================


def test_netease_playlists_on_truth_table(monkeypatch):
    _sources(monkeypatch)
    assert appmod._netease_playlists_on() is False
    _sources(monkeypatch, lx=True, my_playlists=True)
    assert appmod._netease_playlists_on() is True, "附加音源：洛雪音源 + 账号歌单开关"
    _sources(monkeypatch, netease=True)
    assert appmod._netease_playlists_on() is True
    _sources(monkeypatch, lx=True, my_playlists=False)
    assert appmod._netease_playlists_on() is False, "开关关掉后洛雪音源下没有数据源"


def test_netease_tracks_playable_under_lx_with_switch_on(monkeypatch):
    """网易云 guid 的可播判定跟着开关走（音乐盒被保活时才放行）。"""
    _sources(monkeypatch, lx=True, my_playlists=True)
    assert appmod._source_enabled("online:netease:3348197008") is True
    _sources(monkeypatch, lx=True, my_playlists=False)
    assert appmod._source_enabled("online:netease:3348197008") is False
    assert appmod._source_enabled("online:netease:3348197008") is False


# ===========================================================================
# 歌单注入：频道歌单 / 账号歌单
# ===========================================================================


@pytest.mark.anyio
async def test_playlist_list_fetches_channels_under_lx_when_switch_on(monkeypatch):
    _sources(monkeypatch, lx=True, my_playlists=True)
    pulled = []

    async def fake_channels(client):
        pulled.append(client)
        return [], set(), False

    _stub_playlist_plumbing(monkeypatch)
    monkeypatch.setattr(appmod, "_channel_playlist_records", fake_channels)
    resp = await appmod.playlist_list(_req())
    assert resp.status_code == 200
    assert pulled, "开关打开时音乐盒被保活，洛雪音源下也要注入频道歌单"


@pytest.mark.anyio
async def test_playlist_list_injects_account_playlists_under_lx(monkeypatch):
    """账号歌单卡片（nm）在洛雪音源 + 开关打开时同样注入。"""
    _sources(monkeypatch, lx=True, my_playlists=True)
    seen = []

    async def fake_channels(client):
        return [], set(), False

    async def fake_peek(client):
        seen.append(client)
        return []

    _stub_playlist_plumbing(monkeypatch)
    monkeypatch.setattr(appmod, "_channel_playlist_records", fake_channels)
    monkeypatch.setattr(appmod.nmpl, "peek_summaries", fake_peek)
    resp = await appmod.playlist_list(_req())
    assert resp.status_code == 200
    assert seen, "开关打开时账号歌单在洛雪音源下也要注入"


@pytest.mark.anyio
async def test_playlist_list_skips_account_playlists_under_lx_without_switch(monkeypatch):
    """开关关着时洛雪音源下不注入账号歌单（也不能去碰没在跑的音乐盒）。"""
    _sources(monkeypatch, lx=True, my_playlists=False)
    seen = []

    async def fake_channels(client):
        raise AssertionError("频道歌单同理：没数据源不该硬拉")

    async def fake_peek(client):
        seen.append(client)
        return []

    _stub_playlist_plumbing(monkeypatch)
    monkeypatch.setattr(appmod, "_channel_playlist_records", fake_channels)
    monkeypatch.setattr(appmod.nmpl, "peek_summaries", fake_peek)
    resp = await appmod.playlist_list(_req())
    assert resp.status_code == 200
    assert not seen


# ===========================================================================
# 歌单预览 / 预热按钮：同一门控
# ===========================================================================


@pytest.mark.anyio
async def test_preview_lists_channels_under_lx_when_switch_on(monkeypatch):
    _sources(monkeypatch, lx=True, my_playlists=True)
    pulled = []

    async def fake_logged_in():
        return False

    async def fake_channels(client):
        pulled.append(client)
        return [], set(), False

    monkeypatch.setattr(appmod, "_netease_logged_in", fake_logged_in)
    monkeypatch.setattr(appmod, "_channel_playlist_records", fake_channels)
    out = await appmod.ext_playlists_preview()
    assert out["ok"] is True and pulled


@pytest.mark.anyio
async def test_preview_skips_channels_under_lx_without_switch(monkeypatch):
    _sources(monkeypatch, lx=True, my_playlists=False)

    async def fake_logged_in():
        return False

    async def fake_channels(client):
        raise AssertionError("预览要如实反映实际注入，开关关着时不该列频道歌单")

    monkeypatch.setattr(appmod, "_netease_logged_in", fake_logged_in)
    monkeypatch.setattr(appmod, "_channel_playlist_records", fake_channels)
    out = await appmod.ext_playlists_preview()
    assert out["ok"] is True


@pytest.mark.anyio
async def test_warm_endpoint_disabled_under_lx_without_switch(monkeypatch):
    _sources(monkeypatch, lx=True, my_playlists=False)
    out = await appmod.ext_playlists_warm()
    assert out["data"] == {"started": False, "reason": "netease_disabled"}


@pytest.mark.anyio
async def test_warm_endpoint_runs_under_lx_with_switch_on(monkeypatch):
    _sources(monkeypatch, lx=True, my_playlists=True)
    called = []

    async def fake_channels(client):
        return [], set(), False

    monkeypatch.setattr(appmod, "_channel_playlist_records", fake_channels)
    monkeypatch.setattr(appmod, "_schedule_playlist_warm",
                        lambda _app, force=False, guids=None: True)
    out = await appmod.ext_playlists_warm()
    assert out["data"]["started"] is True
