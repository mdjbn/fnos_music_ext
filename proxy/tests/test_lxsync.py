"""洛雪音乐同步歌单（lxsync）测试：转换、缓存/SWR、冷却、以及飞牛路由接线。

真实服务端的那一段在 `test_lxproto.py::test_e2e_against_real_sync_server`（默认跳过）；
这里把传输层换成假的，专注「数据怎么变成歌单、连不上时会不会把列表搞坏」。
"""
import asyncio
import os

import httpx
import pytest
from fastapi.testclient import TestClient

import proxy.app as appmod
from proxy import lxsync
from proxy.app import CONF, app, build_online_track

URL = "http://127.0.0.1:19527"
PWD = "testpass123"

SONG_KW = {"id": "kw_1", "name": "晴天", "singer": "周杰伦", "source": "kw", "interval": "04:29",
           "meta": {"songId": 12345, "albumName": "叶惠美", "picUrl": "http://pic/1.jpg"}}
SONG_KG = {"id": "kg_1", "name": "夜曲", "singer": "周杰伦", "source": "kg", "interval": "03:55",
           "meta": {"songId": "HASH1", "hash": "HASH1", "albumName": "十一月的萧邦", "picUrl": None}}
SONG_LOCAL = {"id": "local_1", "name": "本地歌", "singer": "某人", "source": "local", "interval": "01:00",
              "meta": {"songId": "/music/a.flac", "filePath": "/music/a.flac", "ext": "flac"}}
LIST_DATA = {"defaultList": [], "loveList": [SONG_KG],
             "userList": [{"id": "pl-1", "name": "我的测试歌单", "locationUpdateTime": None,
                           "list": [SONG_KW, SONG_KG, SONG_LOCAL]},
                          {"id": "pl/2:weird", "name": "怪 id 歌单", "locationUpdateTime": None,
                           "list": [SONG_KW]},
                          {"id": "pl-empty", "name": "空歌单", "locationUpdateTime": None, "list": []}]}


@pytest.fixture(autouse=True)
def lx_env(tmp_path, monkeypatch):
    lxsync.reset_for_test()
    monkeypatch.setenv("FNMUSIC_LX_SYNC_DIR", str(tmp_path / "lxsync"))
    monkeypatch.setenv("FNMUSIC_LX_SYNC_ENABLED", "true")
    monkeypatch.setenv("FNMUSIC_LX_SYNC_URL", URL)
    monkeypatch.setenv("FNMUSIC_LX_SYNC_PASSWORD", PWD)
    monkeypatch.delenv("FNMUSIC_LX_SYNC_REFRESH_S", raising=False)
    yield
    lxsync.reset_for_test()


def _fake_sync(monkeypatch, *, data=None, error=None, paired=True, calls=None):
    async def fake(base_url, password, device_name="", client_id="", key_b64="", timeout=0):
        if calls is not None:
            calls.append({"url": base_url, "password": password, "device": device_name,
                          "client_id": client_id, "key_b64": key_b64})
        if error is not None:
            raise error
        return {"list_data": data if data is not None else LIST_DATA,
                "client_id": "cid-1", "key": "a2V5", "paired": paired, "finished": True}
    monkeypatch.setattr(lxsync.lxproto, "sync_session", fake)


# ------------------------------------------------------------- 配置/guid ----

def test_enabled_gate_needs_switch_url_and_password(monkeypatch):
    assert lxsync.sync_enabled() is True
    monkeypatch.setenv("FNMUSIC_LX_SYNC_ENABLED", "false")
    assert lxsync.sync_enabled() is False
    monkeypatch.setenv("FNMUSIC_LX_SYNC_ENABLED", "true")
    monkeypatch.setenv("FNMUSIC_LX_SYNC_URL", "")
    assert lxsync.sync_enabled() is False
    monkeypatch.setenv("FNMUSIC_LX_SYNC_URL", URL)
    monkeypatch.setenv("FNMUSIC_LX_SYNC_PASSWORD", "")
    assert lxsync.sync_enabled() is False


def test_refresh_seconds_default_and_clamp(monkeypatch):
    assert lxsync.refresh_s() == 300.0
    monkeypatch.setenv("FNMUSIC_LX_SYNC_REFRESH_S", "5")
    assert lxsync.refresh_s() == 30.0      # 下限：别把服务端打爆
    monkeypatch.setenv("FNMUSIC_LX_SYNC_REFRESH_S", "abc")
    assert lxsync.refresh_s() == 300.0


def test_switches_are_hot_reloadable(tmp_path, monkeypatch):
    """管理页改完必须免重启生效：6 个键都要在 .env 热重载白名单里。"""
    keys = ("FNMUSIC_LX_SYNC_ENABLED", "FNMUSIC_LX_SYNC_URL", "FNMUSIC_LX_SYNC_PASSWORD",
            "FNMUSIC_LX_SYNC_REFRESH_S", "FNMUSIC_LX_SYNC_DEVICE", "FNMUSIC_LX_SYNC_INSECURE_TLS",
            "FNMUSIC_LX_SYNC_WRITEBACK")
    for key in keys:
        assert key in appmod._ENV_WATCH_KEYS, f"{key} 未纳入 .env 热重载白名单"
        monkeypatch.setenv(key, "")
    env_file = tmp_path / ".env"
    env_file.write_text("FNMUSIC_LX_SYNC_ENABLED=true\n"
                        "FNMUSIC_LX_SYNC_URL=https://host:9528/Code1\n"
                        "FNMUSIC_LX_SYNC_PASSWORD=pw-123\n"
                        "FNMUSIC_LX_SYNC_REFRESH_S=60\n", encoding="utf-8")
    appmod.apply_env_hot_reload(str(env_file))
    assert lxsync.sync_enabled() is True
    assert lxsync.server_url() == "https://host:9528/Code1"
    assert lxsync.refresh_s() == 60.0


def test_guid_helpers_handle_unsafe_ids():
    safe = lxsync.lxsync_playlist_guid("pl-1")
    assert safe == "online:playlist:lxsync:pl-1"
    assert lxsync.is_lxsync_playlist_guid(safe) and not lxsync.is_lxsync_playlist_guid("pl-1")
    assert lxsync.lxsync_playlist_id_from_guid(safe) == "pl-1"
    weird = lxsync.lxsync_playlist_guid("pl/2:weird")
    assert weird.startswith("online:playlist:lxsync:h")
    # 不安全 id 必须靠映射还原；没同步过时不能把 md5 短码当 id 去请求服务端
    assert lxsync.lxsync_playlist_id_from_guid(weird) == ""
    lxsync._state["id_map"][weird.rsplit(":", 1)[-1]] = "pl/2:weird"
    assert lxsync.lxsync_playlist_id_from_guid(weird) == "pl/2:weird"
    assert lxsync.lxsync_playlist_id_from_guid("") == ""
    assert lxsync.lxsync_playlist_id_from_guid("online:playlist:nm:5") == ""


# --------------------------------------------------------------- 转换 ----

@pytest.mark.parametrize("song,expect", [
    (SONG_KW, "lx:kw:12345"),
    (SONG_KG, "lx:kg:HASH1"),
    ({"source": "tx", "name": "x", "meta": {"strMediaMid": "MID1", "songId": 9}}, "lx:tx:MID1"),
    ({"source": "tx", "name": "x", "meta": {"songId": 9}}, "lx:tx:9"),
    ({"source": "wy", "name": "x", "meta": {"songId": 77}}, "lx:wy:77"),
    ({"source": "mg", "name": "x", "meta": {"copyrightId": "CID"}}, "lx:mg:CID"),
    ({"source": "kg", "name": "x", "meta": {}}, None),          # 没有标识 → 丢弃
    ({"source": "local", "name": "x", "meta": {"filePath": "/a"}}, None),
    ({"source": "unknown", "name": "x", "meta": {"songId": 1}}, None),
    # 旧形态（真机上收藏歌单用的就是这个）：没有 meta，元数据直接挂歌曲上
    ({"source": "tx", "name": "ethereal", "singer": "inertia.", "interval": "02:24",
      "songId": 446938063, "strMediaMid": "0048rvB93Uq6QH", "songmid": "0048rvB93Uq6QH"},
     "lx:tx:0048rvB93Uq6QH"),
    ({"source": "tx", "name": "x", "songmid": "001yKJVW4JbE12", "strMediaMid": "OTHER"}, "lx:tx:001yKJVW4JbE12"),
    ({"source": "wy", "name": "睨めっ娘", "songmid": 2154092645, "id": "2154092645"}, "lx:wy:2154092645"),
    ({"source": "kw", "name": "x", "songId": 7134345}, "lx:kw:7134345"),
    ({"source": "kg", "name": "x", "hash": "HASH9"}, "lx:kg:HASH9"),
    ({"source": "tx", "name": "x", "id": "tx_0004RXNa3bb8vE"}, "lx:tx:0004RXNa3bb8vE"),   # 前缀会被剥掉
    ({"source": "wy", "name": "x", "id": "wy:99"}, "lx:wy:99"),
])
def test_track_item_mapping(song, expect):
    item = lxsync.track_item(song)
    assert (item or {}).get("id") == expect
    if item:
        assert item["source"] == "lx" and item["lx_source"] == song["source"]


def test_track_item_fields_and_duration():
    item = lxsync.track_item(SONG_KW)
    assert (item["title"], item["artist"], item["album"]) == ("晴天", "周杰伦", "叶惠美")
    assert item["duration_s"] == 269.0            # 04:29
    assert item["cover_url"] == "http://pic/1.jpg"
    assert item["ext"] == "mp3" and item["lyric"] == ""
    # build_online_track 能吃下去（接线的关键契约）
    track = build_online_track(item)
    assert track["guid"] == "online:lx:kw:12345"


def test_build_state_cards_and_filters():
    cards, tracks, id_map = lxsync._build_state(LIST_DATA)
    names = [c["name"] for c in cards]
    assert names[0] == "我的收藏（洛雪）" and "我的测试歌单" in names and "空歌单" in names
    for card in cards:
        assert card["isDaily"] is True and card["trackCount"] >= 0
        assert card["guid"].startswith("online:playlist:lxsync:")
    by_name = {c["name"]: c for c in cards}
    # 3 首歌里 local 那首不可播 ⇒ 只算 2 首；空歌单仍然注入（trackCount=0）
    assert by_name["我的测试歌单"]["trackCount"] == 2
    assert by_name["空歌单"]["trackCount"] == 0
    assert by_name["我的收藏（洛雪）"]["trackCount"] == 1
    assert by_name["我的测试歌单"]["cover_url"] == "http://pic/1.jpg"
    assert id_map["pl-1"] == "pl-1"
    assert tracks["online:playlist:lxsync:pl-1"] == [SONG_KW, SONG_KG]   # local 已过滤


def test_default_list_is_injected_but_not_when_empty():
    """试听列表（defaultList，id='default'）也要注入；空的时候不挂空壳卡片。"""
    cards, tracks, _ = lxsync._build_state({"defaultList": [SONG_KW, SONG_KG],
                                            "loveList": [], "userList": []})
    assert [c["name"] for c in cards] == ["试听列表（洛雪）"]
    assert cards[0]["guid"] == "online:playlist:lxsync:default"
    assert cards[0]["trackCount"] == 2
    assert lxsync.lxsync_playlist_id_from_guid(cards[0]["guid"]) == "default"
    assert tracks[cards[0]["guid"]] == [SONG_KW, SONG_KG]
    assert lxsync._build_state({"defaultList": [], "loveList": [], "userList": []})[0] == []


def test_build_state_counts_legacy_shape_songs():
    """旧形态（无 meta）的歌必须算作可播：实测里整个 374 首的歌单曾被当成 0 首。"""
    legacy = [{"source": "tx", "name": "ethereal", "singer": "inertia.", "interval": "02:24",
               "songId": 446938063, "songmid": "0048rvB93Uq6QH", "albumName": "faded",
               "img": "https://y.gtimg.cn/x.jpg"},
              {"source": "wy", "name": "睨めっ娘", "singer": "友成空", "interval": "02:39",
               "songmid": 2154092645, "albumName": "睨めっ娘",
               "img": "https://p1.music.126.net/y.jpg"}]
    cards, tracks, _ = lxsync._build_state({"defaultList": [], "loveList": [],
                                            "userList": [{"id": "tx_1", "name": "旧形态", "list": legacy}]})
    assert cards[0]["trackCount"] == 2
    assert cards[0]["cover_url"] == "https://y.gtimg.cn/x.jpg"
    items = [lxsync.track_item(s) for s in tracks[cards[0]["guid"]]]
    assert [i["id"] for i in items] == ["lx:tx:0048rvB93Uq6QH", "lx:wy:2154092645"]
    assert items[1]["album"] == "睨めっ娘" and items[1]["cover_url"].endswith("y.jpg")
    lxsync._state["tracks"] = tracks          # cover_url_for 读的是缓存
    assert lxsync.cover_url_for("tx_1") == "https://y.gtimg.cn/x.jpg"


def test_build_state_caps_playlists_and_tracks(monkeypatch):
    monkeypatch.setattr(lxsync, "_MAX_PLAYLISTS", 2)
    monkeypatch.setattr(lxsync, "_MAX_TRACKS", 1)
    data = {"defaultList": [], "loveList": [],
            "userList": [{"id": f"p{i}", "name": f"n{i}", "list": [SONG_KW, SONG_KG]} for i in range(5)]}
    cards, tracks, _ = lxsync._build_state(data)
    assert len(cards) == 2 and all(c["trackCount"] == 1 for c in cards)


# --------------------------------------------------------- 缓存/SWR/冷却 ----

def test_peek_summaries_then_cache_hit(monkeypatch):
    calls = []
    _fake_sync(monkeypatch, calls=calls)
    cards = asyncio.run(lxsync.peek_summaries())
    assert len(cards) == 4 and len(calls) == 1
    # 新鲜期内不再打扰服务端
    assert asyncio.run(lxsync.peek_summaries()) == cards
    assert len(calls) == 1
    # 配对信息落盘（clientId/key 复用，避免服务端设备列表越滚越多）
    ident = lxsync.lxproto.load_identity(lxsync.identity_path())
    assert ident == {"client_id": "cid-1", "key": "a2V5"}


def test_peek_summaries_reuses_saved_identity(monkeypatch):
    calls = []
    _fake_sync(monkeypatch, calls=calls)
    asyncio.run(lxsync.peek_summaries())
    lxsync._state["saved_at"] = 0.0            # 让下次重新同步
    asyncio.run(lxsync.peek_summaries())
    assert calls[1]["client_id"] == "cid-1" and calls[1]["key_b64"] == "a2V5"


def test_peek_summaries_keeps_old_cards_on_failure(monkeypatch):
    async def main():
        _fake_sync(monkeypatch)
        cards = await lxsync.peek_summaries()
        assert cards
        _fake_sync(monkeypatch, error=lxsync.lxproto.LxSyncError("同步服务认证失败：密码不对"))
        lxsync._state["saved_at"] = 0.0
        again = await lxsync.peek_summaries()
        assert [c["guid"] for c in again] == [c["guid"] for c in cards]   # 旧数据照旧显示
        await asyncio.sleep(0.05)          # 后台刷新失败后才会记错误与冷却
        assert "密码不对" in lxsync.status()["error"]
        assert lxsync.status()["cooldown_s"] > 0

    asyncio.run(main())


def test_failure_cooldown_blocks_retry(monkeypatch):
    calls = []
    _fake_sync(monkeypatch, error=lxsync.lxproto.LxSyncError("boom"), calls=calls)
    assert asyncio.run(lxsync.peek_summaries()) == []
    assert len(calls) == 1
    assert asyncio.run(lxsync.peek_summaries()) == []
    assert len(calls) == 1                     # 冷却期内不再重试


def test_stale_cards_trigger_background_refresh(monkeypatch):
    async def main():
        calls = []
        _fake_sync(monkeypatch, calls=calls)
        await lxsync.peek_summaries()
        assert len(calls) == 1
        lxsync._state["saved_at"] = 0.0        # 过期
        stale = await lxsync.peek_summaries()
        assert stale                                     # 先返回旧卡片，不等网络
        await asyncio.sleep(0.05)                        # 后台刷新跑完
        assert len(calls) == 2
    asyncio.run(main())


def test_disabled_returns_empty_and_never_calls(monkeypatch):
    calls = []
    _fake_sync(monkeypatch, calls=calls)
    monkeypatch.setenv("FNMUSIC_LX_SYNC_ENABLED", "false")
    assert asyncio.run(lxsync.peek_summaries()) == []
    assert calls == []


def test_load_tracks_filters_and_builds(monkeypatch):
    _fake_sync(monkeypatch)
    asyncio.run(lxsync.peek_summaries())
    tracks = asyncio.run(lxsync.load_tracks("pl-1", build_online_track))
    assert [t["guid"] for t in tracks] == ["online:lx:kw:12345", "online:lx:kg:HASH1"]
    # 直接深链打开某歌单（列表页还没同步）时会自己拉一次
    lxsync.reset_for_test()
    _fake_sync(monkeypatch)
    assert asyncio.run(lxsync.load_tracks("pl-1", build_online_track))
    # build_track 抛异常只丢那一首，不影响其它
    def boom(_item):
        raise RuntimeError("bad")
    assert asyncio.run(lxsync.load_tracks("pl-1", boom)) == []


def test_card_cover_and_status():
    cards, _, _ = lxsync._build_state(LIST_DATA)
    lxsync._state.update({"cards": cards, "tracks": {"online:playlist:lxsync:pl-1": [SONG_KW]}})
    assert lxsync.card_for("pl-1")["name"] == "我的测试歌单"
    assert lxsync.card_for("nope") is None
    assert lxsync.cover_url_for("pl-1") == "http://pic/1.jpg"
    assert lxsync.cached_tracks("pl-1") == [SONG_KW]
    st = lxsync.status()
    assert st["password_configured"] is True and "password" not in st      # 不回显密码
    assert st["playlists"] == len(cards)


# ------------------------------------------------------- 飞牛路由接线 ----

@pytest.fixture
def lx_routes(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "_bind_registry_loaded", True)
    appmod._bind_registry.clear()
    appmod._FAKE_GUID_REVERSE.clear()
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(CONF, "fav_dir", str(tmp_path / "fav"))
    # 洛雪音源在跑：曲目列表按可播性过滤（_source_enabled），测试里显式打开
    monkeypatch.setitem(CONF, "lx_enabled", True)
    monkeypatch.setitem(CONF, "lx_sources", [])
    os.makedirs(CONF["cache_dir"], exist_ok=True)

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"guid": "user-a"}})
        return httpx.Response(200, json={"code": 0, "msg": "", "data": {"list": [], "total": 0}})

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream),
                                                 base_url="http://unix")
    for name in ("musicbox_client", "musicdl_client", "lx_client"):
        setattr(app.state, name, httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
            base_url="http://dead"))
    _fake_sync(monkeypatch)
    asyncio.run(lxsync.peek_summaries())      # 预暖缓存：详情/曲目端点都读它
    yield
    lxsync.reset_for_test()


def _auth_cookie():
    return {"music-token": "valid_token"}


def test_playlist_list_injects_lx_cards(lx_routes):
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list", cookies=_auth_cookie())
    assert resp.status_code == 200
    body = resp.json()
    guids = [it["guid"] for it in body["data"]["list"]]
    assert "online:playlist:lxsync:pl-1" in guids
    assert body["data"]["total"] >= len(guids)
    card = next(it for it in body["data"]["list"] if it["guid"] == "online:playlist:lxsync:pl-1")
    assert card["isDaily"] is True and card["trackCount"] == 2
    assert card["coverId"].startswith("track_")           # 封面假 id 已登记


def test_playlist_detail_and_tracks(lx_routes):
    with TestClient(app) as client:
        detail = client.get("/music/api/v1/playlist/detail",
                            params={"guid": "online:playlist:lxsync:pl-1"}, cookies=_auth_cookie())
        tracks = client.get("/music/api/v1/track/playlist-detail/list",
                            params={"playlistGUID": "online:playlist:lxsync:pl-1", "size": -1},
                            cookies=_auth_cookie())
        missing = client.get("/music/api/v1/playlist/detail",
                             params={"guid": "online:playlist:lxsync:nope"}, cookies=_auth_cookie())
    assert detail.json()["code"] == 0
    assert detail.json()["data"]["name"] == "我的测试歌单"
    assert detail.json()["data"]["trackCount"] == 2
    assert tracks.json()["code"] == 0
    # 下发到客户端时 guid 会被 stamp 成官方 32-hex 形态（对齐官方 App 的 id 格式过滤），
    # 所以这里断言曲目本身而不是 guid
    assert [t["title"] for t in tracks.json()["data"]["list"]] == ["晴天", "夜曲"]
    assert tracks.json()["data"]["total"] == 2
    assert missing.json()["code"] == -1


def test_readonly_playlist_writes_are_absorbed(lx_routes):
    """加歌/移歌/删歌单都不该透传官方（假 id 必被拒），也不该报错。"""
    guid = "online:playlist:lxsync:pl-1"
    with TestClient(app) as client:
        add = client.post("/music/api/v1/playlist/add-track",
                          json={"guid": guid, "trackGUIDs": ["online:lx:kw:12345"]}, cookies=_auth_cookie())
        rm = client.post("/music/api/v1/playlist/remove-track",
                         json={"guid": guid, "trackGUIDs": ["online:lx:kw:12345"]}, cookies=_auth_cookie())
        dele = client.post("/music/api/v1/playlist/delete", json={"guid": guid}, cookies=_auth_cookie())
    for resp in (add, rm, dele):
        assert resp.status_code == 200 and resp.json()["code"] == 0


def test_cover_redirects_to_first_track_pic(lx_routes):
    with TestClient(app) as client:
        client.get("/music/api/v1/playlist/list", cookies=_auth_cookie())     # 先把卡片填进缓存
        resp = client.get("/music/api/v1/static/cover",
                          params={"coverId": "online:playlist:lxsync:pl-1"},
                          cookies=_auth_cookie(), follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "http://pic/1.jpg"


def test_diag_endpoint(lx_routes):
    with TestClient(app) as client:
        resp = client.get("/_ext/lxsync", cookies=_auth_cookie())
    data = resp.json()
    assert resp.status_code == 200 and data["ok"] is True
    assert data["data"]["url"] == URL and data["data"]["password_configured"] is True
    assert "password" not in data["data"]


# ------------------------------------------------- 歌单回写（默认 off）----

def test_writeback_mode_parsing(monkeypatch):
    monkeypatch.delenv("FNMUSIC_LX_SYNC_WRITEBACK", raising=False)
    assert lxsync.writeback_mode() == "off"
    assert lxsync.writeback_tracks_enabled() is False and lxsync.writeback_all_enabled() is False
    monkeypatch.setenv("FNMUSIC_LX_SYNC_WRITEBACK", "tracks")
    assert (lxsync.writeback_mode(), lxsync.writeback_tracks_enabled(),
            lxsync.writeback_all_enabled()) == ("tracks", True, False)
    monkeypatch.setenv("FNMUSIC_LX_SYNC_WRITEBACK", "ALL")
    assert lxsync.writeback_all_enabled() is True
    monkeypatch.setenv("FNMUSIC_LX_SYNC_WRITEBACK", "write-everything")
    assert lxsync.writeback_mode() == "off"          # 非法值回落 off，绝不猜
    monkeypatch.setenv("FNMUSIC_LX_SYNC_WRITEBACK", "all")
    monkeypatch.setenv("FNMUSIC_LX_SYNC_ENABLED", "false")
    assert lxsync.writeback_all_enabled() is False   # 总开关关了就一律不能写


def test_song_from_item_prefers_cached_raw_song():
    raw = {"id": "tx_ABC", "source": "tx", "name": "原始", "singer": "s", "interval": "03:00",
           "meta": {"songId": "ABC", "songmid": "ABC", "qualitys": [{"type": "flac"}]}}
    lxsync._state["index"] = {"tx:ABC": raw}
    # 缓存里有原始对象就原样用（字段最全，品质档不丢）
    assert lxsync.song_from_item({"id": "online:lx:tx:ABC", "title": "别管我"}) is raw
    # 缓存没有（搜索来的歌）→ 合成最小可播对象
    song = lxsync.song_from_item({"id": "online:lx:kw:7134345", "title": "不该", "artist": "周杰伦",
                                  "album": "叶惠美", "duration_s": 290.4, "cover_url": "http://p"})
    assert song == {"id": "kw_7134345", "name": "不该", "singer": "周杰伦", "source": "kw",
                    "interval": "04:50",
                    "meta": {"songId": "7134345", "albumName": "叶惠美", "picUrl": "http://p"}}
    assert lxsync.song_from_item({"id": "lx:tx:MID1", "title": "t"})["meta"]["songmid"] == "MID1"
    assert lxsync.song_from_item({"id": "lx:kg:HH", "title": "t"})["meta"]["hash"] == "HH"
    assert lxsync.song_from_item({"id": "lx:mg:CID", "title": "t"})["meta"]["copyrightId"] == "CID"
    assert lxsync.song_from_item({"id": "lx:wy:99", "title": "t"})["meta"]["songId"] == "99"
    assert lxsync.song_from_item({"id": "online:netease:1", "title": "t"}) is None
    assert lxsync.song_from_item({"id": "lx:tx:", "title": "t"}) is None


def test_raw_song_and_song_id_helpers():
    raw = {"id": "210254111", "source": "wy", "name": "x"}
    lxsync._state["index"] = {"wy:210254111": raw}
    assert lxsync.raw_song_for_guid("online:lx:wy:210254111") is raw
    assert lxsync.raw_song_for_guid("lx:wy:210254111") is raw
    assert lxsync.raw_song_for_guid("online:netease:1") is None
    assert lxsync._song_id_for("online:lx:wy:210254111") == "210254111"   # 用洛雪自己的 id
    assert lxsync._song_id_for("online:lx:tx:ABC") == "tx_ABC"           # 缓存没有才拼
    assert lxsync._song_id_for("online:netease:1") == ""


class _FakeConn:
    instances: list = []

    def __init__(self, base_url, password, device_name="", client_id="", key_b64="", timeout=0):
        self.kwargs = {"base_url": base_url, "password": password, "device_name": device_name,
                       "client_id": client_id, "key_b64": key_b64}
        self.calls: list = []
        self.closed = False
        _FakeConn.instances.append(self)

    async def open(self):
        return self

    def start_pump(self):
        pass

    async def wait_for(self, pred, timeout=None):
        return True

    async def call(self, path, *args, timeout=0):
        self.calls.append((list(path), args))
        return "snapshot-1"

    async def close(self):
        self.closed = True


def _install_fake_conn(monkeypatch, exc=None):
    _FakeConn.instances = []

    class C(_FakeConn):
        async def call(self, path, *args, timeout=0):
            if exc is not None:
                raise exc
            return await super().call(path, *args, timeout=timeout)

    monkeypatch.setattr(lxsync.lxproto, "LxConnection", C)
    return C


def test_add_tracks_sends_list_music_add_and_invalidates_cache(monkeypatch):
    C = _install_fake_conn(monkeypatch)
    lxsync._state["saved_at"] = 123.0
    n = asyncio.run(lxsync.add_tracks("tx__7480045223", [
        {"id": "online:lx:tx:ABC", "title": "晴天", "artist": "周杰伦", "album": "叶惠美",
         "duration_s": 269, "cover_url": "http://p"},
        {"id": "online:netease:1", "title": "别的来源"},          # 会被跳过
    ]))
    assert n == 1
    conn = C.instances[0]
    path, args = conn.calls[0]
    assert path == ["onListSyncAction"]
    action = args[0]
    assert action["action"] == "list_music_add"
    assert action["data"]["id"] == "tx__7480045223"
    assert action["data"]["addMusicLocationType"] == "bottom"
    assert [s["id"] for s in action["data"]["musicInfos"]] == ["tx_ABC"]
    assert conn.closed is True
    assert lxsync._state["saved_at"] == 0.0                       # 写完失效缓存，界面回权威结果


def test_remove_tracks_uses_lx_song_ids(monkeypatch):
    C = _install_fake_conn(monkeypatch)
    lxsync._state["index"] = {"tx:ABC": {"id": "tx_ABC", "source": "tx"}}
    n = asyncio.run(lxsync.remove_tracks("pl-1", ["online:lx:tx:ABC", "online:lx:kw:7134345"]))
    assert n == 2
    assert C.instances[0].calls[0][1][0] == {
        "action": "list_music_remove", "data": {"listId": "pl-1", "ids": ["tx_ABC", "kw_7134345"]}}


def test_remove_playlist_and_write_failures(monkeypatch):
    C = _install_fake_conn(monkeypatch)
    asyncio.run(lxsync.remove_playlist("pl-1"))
    assert C.instances[0].calls[0][1][0] == {"action": "list_remove", "data": ["pl-1"]}
    with pytest.raises(lxsync.lxproto.LxSyncError):
        asyncio.run(lxsync.add_tracks("pl-1", [{"id": "online:netease:1"}]))
    with pytest.raises(lxsync.lxproto.LxSyncError):
        asyncio.run(lxsync.remove_tracks("pl-1", ["online:netease:1"]))
    _install_fake_conn(monkeypatch, exc=lxsync.lxproto.LxSyncError("连不上同步服务"))
    with pytest.raises(lxsync.lxproto.LxSyncError):
        asyncio.run(lxsync.add_tracks("pl-1", [{"id": "lx:kw:1", "title": "t"}]))


def test_routes_writeback_off_absorbs_without_writing(lx_routes, monkeypatch):
    called = []
    monkeypatch.setattr(lxsync, "add_tracks", lambda *a, **k: called.append(a))
    monkeypatch.setattr(lxsync, "remove_tracks", lambda *a, **k: called.append(a))
    monkeypatch.setattr(lxsync, "remove_playlist", lambda *a, **k: called.append(a))
    guid = "online:playlist:lxsync:pl-1"
    with TestClient(app) as client:
        r1 = client.post("/music/api/v1/playlist/add-track",
                         json={"guid": guid, "trackGUIDs": ["online:lx:tx:ABC"]}, cookies=_auth_cookie())
        r2 = client.post("/music/api/v1/playlist/remove-track",
                         json={"guid": guid, "trackGUIDs": ["online:lx:tx:ABC"]}, cookies=_auth_cookie())
        r3 = client.post("/music/api/v1/playlist/delete", json={"guid": guid}, cookies=_auth_cookie())
    assert [r.json()["code"] for r in (r1, r2, r3)] == [0, 0, 0]
    assert called == []          # 默认只读：一个写动作都不许发出去


def test_routes_writeback_tracks_writes_and_reports_failure(lx_routes, monkeypatch):
    monkeypatch.setenv("FNMUSIC_LX_SYNC_WRITEBACK", "tracks")
    calls = []

    async def fake_info(request, guid):
        return {"id": "lx:tx:ABC", "source": "lx", "title": "晴天", "artist": "周杰伦",
                "album": "叶惠美", "duration_s": 269, "cover_url": "http://p"}

    async def fake_add(pid, items, position="bottom"):
        calls.append(("add", pid, items, position))
        return len(items)

    async def fake_remove(pid, guids):
        calls.append(("remove", pid, guids))
        return len(guids)

    monkeypatch.setattr(appmod, "_best_effort_online_info", fake_info)
    monkeypatch.setattr(lxsync, "add_tracks", fake_add)
    monkeypatch.setattr(lxsync, "remove_tracks", fake_remove)
    guid = "online:playlist:lxsync:pl-1"
    with TestClient(app) as client:
        r1 = client.post("/music/api/v1/playlist/add-track",
                         json={"guid": guid, "trackGUIDs": ["online:lx:tx:ABC"]}, cookies=_auth_cookie())
        r2 = client.post("/music/api/v1/playlist/remove-track",
                         json={"guid": guid, "trackGUIDs": ["online:lx:tx:ABC"]}, cookies=_auth_cookie())
        # tracks 档：删歌单仍然是吸收（只有 all 档才允许）
        r3 = client.post("/music/api/v1/playlist/delete", json={"guid": guid}, cookies=_auth_cookie())
    assert [r.json()["code"] for r in (r1, r2, r3)] == [0, 0, 0]
    assert calls[0] == ("add", "pl-1",
                        [{"id": "online:lx:tx:ABC", "title": "晴天", "artist": "周杰伦",
                          "album": "叶惠美", "duration_s": 269, "cover_url": "http://p"}], "bottom")
    assert calls[1] == ("remove", "pl-1", ["online:lx:tx:ABC"])
    assert len(calls) == 2

    async def boom(*_a, **_k):
        raise lxsync.lxproto.LxSyncError("洛雪那边拒绝了")

    monkeypatch.setattr(lxsync, "add_tracks", boom)
    with TestClient(app) as client:
        bad = client.post("/music/api/v1/playlist/add-track",
                          json={"guid": guid, "trackGUIDs": ["online:lx:tx:ABC"]}, cookies=_auth_cookie())
    assert bad.status_code == 502 and bad.json()["code"] == 502
    assert "洛雪那边拒绝了" in bad.json()["msg"]        # 不再静默成功


def test_routes_writeback_all_allows_playlist_delete(lx_routes, monkeypatch):
    monkeypatch.setenv("FNMUSIC_LX_SYNC_WRITEBACK", "all")
    calls = []

    async def fake_remove_playlist(pid):
        calls.append(pid)

    monkeypatch.setattr(lxsync, "remove_playlist", fake_remove_playlist)
    with TestClient(app) as client:
        ok = client.post("/music/api/v1/playlist/delete",
                         json={"guid": "online:playlist:lxsync:pl-1"}, cookies=_auth_cookie())
    assert ok.json()["code"] == 0 and calls == ["pl-1"]

    async def boom(_pid):
        raise lxsync.lxproto.LxSyncError("删不掉")

    monkeypatch.setattr(lxsync, "remove_playlist", boom)
    with TestClient(app) as client:
        bad = client.post("/music/api/v1/playlist/delete",
                          json={"guid": "online:playlist:lxsync:pl-1"}, cookies=_auth_cookie())
    assert bad.status_code == 502 and "删不掉" in bad.json()["msg"]


def test_write_and_refresh_are_serialized_by_session_lock(monkeypatch):
    """同一个 clientId 不能同时开两条连接：服务端 checkDuplicateClient() 会踢掉先连的那条，
    实测表现为「写没生效 / 读到空歌单」。所以回写与刷新必须共用 _session_lock。"""
    seen = {}

    class C(_FakeConn):
        async def open(self):
            seen["locked"] = lxsync._session_lock.locked()
            return self

    monkeypatch.setattr(lxsync.lxproto, "LxConnection", C)
    asyncio.run(lxsync.add_tracks("pl-1", [{"id": "lx:kw:1", "title": "t"}]))
    assert seen["locked"] is True
