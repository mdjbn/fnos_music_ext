"""更多口径歌单、账户歌单与收藏归档（v2.2）的单元测试。
"""
from __future__ import annotations

import json
import time
import os
from pathlib import Path

import pytest

from proxy import download as dl
from proxy import playlists as pl


# ===========================================================================
# guid 规范与口径开关
# ===========================================================================


def test_guid_shapes_are_parsed():
    assert pl.is_channel_guid("online:playlist:ne:12345")
    assert pl.is_channel_guid("online:playlist:nealbum:999")
    assert pl.is_channel_guid(pl.NETEASE_FM_GUID)
    # 曲目 guid 与其它命名空间绝不能被误当成伪歌单
    assert not pl.is_channel_guid("online:netease:12345")
    assert not pl.is_channel_guid("online:playlist:daily:20260911")
    assert not pl.is_channel_guid("")
    assert not pl.is_channel_guid(None)

    assert pl.channel_of("online:playlist:nealbum:999") == "newalbum"
    assert pl.channel_of(pl.NETEASE_FM_GUID) == "fm"
    assert pl.channel_of("online:playlist:ne:7") == "playlist"
    assert pl._target_id("online:playlist:ne:7") == "7"
    assert pl._target_id("online:playlist:nealbum:88") == "88"
    assert pl._target_id(pl.NETEASE_FM_GUID) == ""


def test_channels_enabled_filters_and_dedupes(monkeypatch):
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "toplist, mine,toplist,Bogus,,daily")
    # daily 由独立开关 daily_enabled 控制，不该混在这里；未知 key 必须丢弃
    assert pl.channels_enabled() == ("mine", "toplist")

    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "")
    assert pl.channels_enabled() == ("mine", "toplist", "category"), "未配置应有合理默认"

    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "totally-unknown")
    assert pl.channels_enabled() == ()


def test_channel_limit_is_bounded(monkeypatch):
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_LIMIT", "8")
    assert pl.channel_limit() == 8
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_LIMIT", "9999")
    assert pl.channel_limit() == 50, "必须有硬上限，否则本地歌单会被淹掉"
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_LIMIT", "0")
    assert pl.channel_limit() == 1
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_LIMIT", "abc")
    assert pl.channel_limit() == 8


# ===========================================================================
# 封面协议与命名
# ===========================================================================


def test_cover_url_upgraded_to_https():
    """上游歌单 coverImgUrl 是 http://（歌曲 picUrl 才是 https）。

    飞牛 UI 跑在 https 下，http 图片会被浏览器按混合内容拦掉 —— 表现就是没封面。
    """
    assert pl._https("http://p1.music.126.net/a.jpg") == "https://p1.music.126.net/a.jpg"
    assert pl._https("https://p1.music.126.net/a.jpg") == "https://p1.music.126.net/a.jpg"
    assert pl._https("") == ""
    assert pl._https(None) == ""

    rec = pl.build_record("g", "n", "http://x/y.jpg", 3, "category")
    assert rec["cover_url"] == "https://x/y.jpg"


def test_display_name_prefixes():
    """账户歌单按用户要求加前缀区分本地歌单；收藏来的再标一次。"""
    assert pl._display_name("mine", "我的最爱") == "网易云·我的最爱"
    assert pl._display_name("mine", "别人的单", subscribed=True) == "网易云·收藏别人的单"
    assert pl._display_name("toplist", "飙升榜") == "榜｜飙升榜"
    assert pl._display_name("newalbum", "某专辑") == "新碟｜某专辑"
    assert pl._display_name("mine", "") == "网易云·未命名歌单"
    assert pl._display_name("mine", None) == "网易云·未命名歌单"


# ===========================================================================
# 注册表（必须落盘：飞牛只要 guid 来取封面，不重启就还能解析出名字）
# ===========================================================================


@pytest.fixture
def registry_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_PLAYLIST_CACHE_DIR", str(tmp_path / "plc"))
    pl._registry_cache = None
    yield tmp_path / "plc"
    pl._registry_cache = None


def test_registry_roundtrip_survives_reload(registry_dir):
    rec = pl.build_record("online:playlist:ne:42", "榜｜热歌榜",
                          "http://p/x.jpg", 100, "toplist")
    pl.remember(rec)
    pl.save_registry()
    assert (registry_dir / "registry.json").exists()

    pl._registry_cache = None          # 模拟进程重启
    got = pl.lookup("online:playlist:ne:42")
    assert got["name"] == "榜｜热歌榜"
    assert got["cover_url"] == "https://p/x.jpg", "落盘前就应已升级协议"
    assert got["track_count"] == 100


def test_remember_keeps_existing_fields_on_partial_update(registry_dir):
    pl.remember(pl.build_record("online:playlist:ne:7", "网易云·我的单", "http://c", 0, "mine"))
    pl.save_registry()
    # 只更新曲目数，不能把名字与封面冲成空
    pl.remember({"guid": "online:playlist:ne:7", "track_count": 25})
    got = pl.lookup("online:playlist:ne:7")
    assert got["name"] == "网易云·我的单"
    assert got["cover_url"] == "http://c" or got["cover_url"] == "https://c"
    assert got["track_count"] == 25


def test_forget_stale_keeps_daily_and_current(registry_dir):
    pl.remember(pl.build_record("online:playlist:ne:1", "a", "", 1, "toplist"))
    pl.remember(pl.build_record("online:playlist:ne:2", "b", "", 1, "toplist"))
    pl.remember(pl.build_record("online:playlist:daily:20260911", "每日推荐", "", 20, "daily"))
    pl.save_registry()
    n = pl.forget_stale({"online:playlist:ne:1"})
    assert n == 1
    assert pl.lookup("online:playlist:ne:1")
    assert not pl.lookup("online:playlist:ne:2")
    assert pl.lookup("online:playlist:daily:20260911"), "每日推荐归 recommend.py 管，不能被清掉"


def test_registry_corruption_does_not_break_listing(registry_dir, monkeypatch):
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / "registry.json").write_text("{not json", encoding="utf-8")
    pl._registry_cache = None
    assert pl.load_registry() == {}, "注册表坏了就重建，不能让歌单列表整个挂掉"


# ===========================================================================
# 未登录时「需登录口径」必须消失，而不是塞一个点进去没内容的空歌单
# ===========================================================================


class _FakeClient:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def get(self, path, params=None, timeout=None):
        self.calls.append((path, params))
        body = self.routes.get(path)
        if body is None:
            return _Resp(404, {})
        return _Resp(200, body)


class _Resp:
    def __init__(self, code, body):
        self.status_code = code
        self._body = body

    def json(self):
        return self._body


@pytest.mark.anyio
async def test_login_required_channels_absent_when_logged_out(registry_dir, monkeypatch):
    # 只启用需登录的口径：本用例要证明的正是「它们一个都不出现」
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "mine,nrec,fm")
    c = _FakeClient({"/api/v1/playlists/user": {"ok": False, "error": "not_logged_in", "data": []},
                     "/api/v1/playlists/recommend": {"ok": False, "error": "not_logged_in", "data": []},
                     "/api/v1/radio/fm": {"ok": False, "error": "not_logged_in", "data": []}})
    recs, keep, complete = await pl.collect_records(c, logged_in=False)
    assert recs == [], f"未登录时不该注入任何需登录口径：{[r['channel'] for r in recs]}"
    # 连上游都不该去打（明知未登录还去请求，纯属白跑一趟并拖慢列表）
    assert c.calls == []
    assert keep == set() and complete is False, "未登录时清单不完整，不得据此清理注册表"


@pytest.mark.anyio
async def test_public_channels_available_when_logged_out(registry_dir, monkeypatch):
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "toplist,category,newalbum")
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_LIMIT", "2")
    c = _FakeClient({
        "/api/v1/playlists/toplists": {"ok": True, "data": [
            {"playlist_id": 1, "name": "飙升榜", "cover_url": ""},
            {"playlist_id": 2, "name": "新歌榜", "cover_url": ""},
            {"playlist_id": 3, "name": "热歌榜", "cover_url": ""}]},
        "/api/v1/playlists/category": {"ok": True, "data": [
            {"playlist_id": 11, "name": "华语热单", "cover_url": "http://p/11.jpg", "track_count": 40},
            {"playlist_id": 12, "name": "欧美", "cover_url": "http://p/12.jpg", "track_count": 30}]},
        "/api/v1/playlists/newalbums": {"ok": True, "data": [
            {"album_id": 21, "name": "新专辑A", "artist": "甲", "cover_url": "http://p/21.jpg"},
            {"album_id": 22, "name": "新专辑B", "artist": "", "cover_url": ""}]},
    })
    recs, _, _c = await pl.collect_records(c, logged_in=False)
    chans = [r["channel"] for r in recs]
    assert chans == ["toplist", "toplist", "category", "category", "newalbum", "newalbum"]
    assert [r["guid"] for r in recs][:2] == ["online:playlist:ne:1", "online:playlist:ne:2"]
    assert recs[4]["guid"] == "online:playlist:nealbum:21", "专辑口径必须用自己的 guid 前缀"
    assert recs[2]["name"].startswith("华语｜")
    assert recs[4]["name"] == "新碟｜新专辑A - 甲"
    assert recs[5]["name"] == "新碟｜新专辑B", "没有艺术家时不该出现悬空的 ' - '"
    assert all(r["cover_url"].startswith("https://") or r["cover_url"] == "" for r in recs)


@pytest.mark.anyio
async def test_mine_playlists_mark_subscribed(registry_dir, monkeypatch):
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "mine")
    c = _FakeClient({"/api/v1/playlists/user": {"ok": True, "data": [
        {"playlist_id": 5, "name": "自建单", "cover_url": "http://p/5.jpg", "track_count": 12,
         "subscribed": False},
        {"playlist_id": 6, "name": "收藏单", "cover_url": "", "track_count": 3, "subscribed": True},
    ]}})
    recs, _, _c = await pl.collect_records(c, logged_in=True)
    assert [r["name"] for r in recs] == ["网易云·自建单", "网易云·收藏收藏单"]
    assert pl.lookup("online:playlist:ne:6")["track_count"] == 3


@pytest.mark.anyio
async def test_channel_failure_is_isolated(registry_dir, monkeypatch):
    """一个口径挂了只影响它自己，不能整个歌单列表都出不来。"""
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "toplist,newalbum")

    class _Boom(_FakeClient):
        async def get(self, path, params=None, timeout=None):
            if path.endswith("toplists"):
                raise RuntimeError("upstream exploded")
            return await super().get(path, params, timeout)

    c = _Boom({"/api/v1/playlists/newalbums": {"ok": True, "data": [
        {"album_id": 31, "name": "还在的专辑", "artist": "", "cover_url": ""}]}})
    recs, _, _c = await pl.collect_records(c, logged_in=False)
    assert [r["name"] for r in recs] == ["新碟｜还在的专辑"]


# ===========================================================================
# 歌单内容解析
# ===========================================================================


def test_tracks_path_for_each_guid_kind():
    assert pl.tracks_path_for("online:playlist:ne:7") == ("/api/v1/playlist/7/tracks",
                                                          {"limit": pl.playlist_track_limit()})
    assert pl.tracks_path_for("online:playlist:nealbum:8")[0] == "/api/v1/album/8/tracks"
    assert pl.tracks_path_for(pl.NETEASE_FM_GUID)[0] == "/api/v1/radio/fm"
    assert pl.tracks_path_for("online:playlist:ne:not-a-number") is None
    assert pl.tracks_path_for("online:netease:5") is None
    assert pl.tracks_path_for(None) is None


@pytest.mark.anyio
async def test_resolve_track_items_dedupes_and_maps(registry_dir):
    rows = [{"song_id": 1, "song_name": "甲", "artist": "A", "album_name": "", "duration": 200,
             "quality": "HD 320k", "mp3_url": "http://u/1", "album_pic_url": ""},
            {"song_id": 1, "song_name": "甲重复", "artist": "A", "album_name": "", "duration": 200,
             "quality": "HD 320k", "mp3_url": "http://u/1", "album_pic_url": ""},
            {"song_id": 2, "song_name": "乙", "artist": "B", "album_name": "", "duration": 180,
             "quality": "HD 320k", "mp3_url": "http://u/2", "album_pic_url": ""}]
    c = _FakeClient({"/api/v1/playlist/7/tracks": {"ok": True, "data": rows}})
    enriched = []

    async def fake_enrich(client, items):
        enriched.append(len(items))

    from proxy import netease_items

    items = await pl.resolve_track_items(c, "online:playlist:ne:7",
                                        netease_items.map_netease_song, fake_enrich)
    assert [i["id"] for i in items] == ["netease:1", "netease:2"], "重复曲目必须去重"
    assert enriched == [2], "补齐只应跑一次批量请求"
    # 可播数量回填注册表，让列表上的曲目数是真实值
    assert pl.lookup("online:playlist:ne:7")["track_count"] == 2


@pytest.mark.anyio
async def test_resolve_track_items_tolerates_upstream_failure(registry_dir):
    from proxy import netease_items

    c = _FakeClient({"/api/v1/playlist/9/tracks": {"ok": False, "error": "boom", "data": []}})
    assert await pl.resolve_track_items(c, "online:playlist:ne:9",
                                       netease_items.map_netease_song, None) == []
    c2 = _FakeClient({})           # 端点直接 404
    assert await pl.resolve_track_items(c2, "online:playlist:ne:9",
                                       netease_items.map_netease_song, None) == []


@pytest.mark.anyio
async def test_resolve_track_items_skips_enrich_when_covers_present(registry_dir):
    """v2.7：曲目自带封面（song_info 的 album_pic_url）时不再走 enrich。

    enrich 在上游要做 songs_detail + songs_url 两次跨洋往返，是
    「打开歌单/榜单 5~8 秒」的主要构成之一；封面齐全时跳过它。
    """
    from proxy import netease_items

    rows = [{"song_id": i, "song_name": f"歌{i}", "artist": "A", "album_name": "",
             "duration": 200, "quality": "HD 320k", "album_pic_url": f"https://p1.music.126.net/{i}.jpg"}
            for i in range(1, 4)]
    c = _FakeClient({"/api/v1/playlist/11/tracks": {"ok": True, "data": rows}})
    calls = {"enrich": 0}

    async def enrich_spy(client, items):
        calls["enrich"] += 1

    items = await pl.resolve_track_items(c, "online:playlist:ne:11",
                                        netease_items.map_netease_song, enrich_spy)
    assert len(items) == 3
    assert calls["enrich"] == 0, "封面齐全时不应再打 enrich（上游两次往返）"
    assert all(str(it.get("cover_url") or "").endswith(".jpg") for it in items)

    # 只要有一条缺封面，仍必须补齐（只补缺的那部分）
    rows[1]["album_pic_url"] = ""
    c2 = _FakeClient({"/api/v1/playlist/11/tracks": {"ok": True, "data": rows}})
    items2 = await pl.resolve_track_items(c2, "online:playlist:ne:11",
                                         netease_items.map_netease_song, enrich_spy)
    assert calls["enrich"] == 1
    assert len(items2) == 3


@pytest.mark.anyio
async def test_channel_records_cache_hit_and_stale_refresh(monkeypatch):
    """v2.7 口径清单短缓存：TTL 内零上游；过期先回旧值再后台刷新。"""
    import asyncio
    import time as _time

    from proxy import app as proxy_app

    calls = {"n": 0}

    async def fake_collect(client, logged_in):
        calls["n"] += 1
        return ([{"guid": "online:playlist:ne:1", "name": "榜｜飙升榜", "channel": "toplist"}],
                {"online:playlist:ne:1"}, True)

    async def fake_logged_in():
        return True

    monkeypatch.setattr(pl, "collect_records", fake_collect)
    monkeypatch.setattr(proxy_app, "_netease_logged_in", fake_logged_in)

    class _Client:
        pass

    recs, keep, complete = await proxy_app._channel_playlist_records(_Client())
    assert calls["n"] == 1
    assert complete is True

    # TTL 内命中缓存：不再拉上游
    recs2, _, _ = await proxy_app._channel_playlist_records(_Client())
    assert calls["n"] == 1
    assert recs2 == recs

    # 过期：先返回旧值（列表页要快），后台单飞刷新
    key = proxy_app._channel_recs_cache_key()
    proxy_app._channel_recs_cache[key]["ts"] = (
        _time.time() - proxy_app._CHANNEL_LIST_CACHE_TTL - 1)
    recs3, _, _ = await proxy_app._channel_playlist_records(_Client())
    assert calls["n"] == 1, "过期路径必须先返回旧值"
    assert recs3 == recs
    for _ in range(50):
        if calls["n"] >= 2:
            break
        await asyncio.sleep(0.02)
    assert calls["n"] == 2, "后台刷新必须真正执行"


# ===========================================================================
# v2.4：歌单大类顺序自定义 + 稳定时间戳
# ===========================================================================


def test_channel_order_custom_and_fallback(monkeypatch):
    # 自定义顺序：用户给的顺序原样生效，漏掉的按默认序追加
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_ORDER", "toplist, mine, daily")
    assert pl.channel_order()[:4] == ("localdaily", "toplist", "mine", "daily")
    assert pl.channel_order()[4:] == ("nrec", "category", "newalbum", "fm")

    # 未知 key 忽略，空值/坏值回落默认
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_ORDER", "")
    assert pl.channel_order() == ("localdaily", "daily", "mine", "nrec", "toplist", "category", "newalbum", "fm")
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_ORDER", "bogus,,???")
    assert pl.channel_order() == ("localdaily", "daily", "mine", "nrec", "toplist", "category", "newalbum", "fm")

    assert pl.rank_of("toplist") == pl.channel_order().index("toplist")
    assert pl.rank_of("nonexistent") == len(pl.CHANNELS)


def test_channels_enabled_follows_custom_order(monkeypatch):
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "mine,toplist,category")
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_ORDER", "category,toplist,mine")
    assert pl.channels_enabled() == ("category", "toplist", "mine"), (
        "勾选的口径要按大类顺序配置输出，否则飞牛列表里的顺序不受用户控制"
    )


def test_stamp_display_order_gives_distinct_ascending_timestamps(registry_dir):
    """每条注入条目拿到互不相同、按位置递增、且远早于本地歌单的时间戳。

    v2.4.0 真机验证：飞牛客户端按时间戳**升序**排列歌单列表。所有条目共用
    int(time.time()) 时（v2.3）顺序随机；用"当前时间递减"时（v2.4.0）每日推荐
    （时间戳最大）反而沉底。正确做法：从小基准（2020-09）开始递增——
    注入条目永远排在本地歌单（fnOS 2023 年才发布）之前，顺序=注入顺序。
    """
    items = [
        {"guid": "online:playlist:daily:20260912", "name": "每日推荐"},
        {"guid": "online:playlist:ne:1", "name": "a", "channel": "mine"},
        {"guid": "online:playlist:ne:2", "name": "b", "channel": "mine"},
        {"guid": "online:playlist:ne:3", "name": "c", "channel": "toplist"},
    ]
    pl.remember(pl.build_record("online:playlist:ne:1", "a", "", 1, "mine"))
    out = pl.stamp_display_order(items)
    ts = [it["createdAt"] for it in out]
    assert ts == sorted(ts), "时间戳必须严格递增"
    assert len(set(ts)) == len(ts), "时间戳必须互不相同，否则非稳定排序仍会乱"
    assert all(it["createdAt"] == it["updatedAt"] for it in out)
    assert max(ts) < 1700000000, "展示时间戳必须早于任何真实 fnOS 歌单（2023+）"
    # 升序排序（客户端实际行为）应还原注入顺序
    assert [it["name"] for it in sorted(out, key=lambda x: x["createdAt"])] == \
        ["每日推荐", "a", "b", "c"]
    # 注册表 ts 同步：详情页回显要与列表页一致
    assert pl.lookup("online:playlist:ne:1")["ts"] == ts[1]
    # 每日推荐也进注册表（详情页取同一份时间戳），并带真实 seen 供过期清理
    daily_reg = pl.lookup("online:playlist:daily:20260912")
    assert daily_reg["ts"] == ts[0]
    assert daily_reg.get("seen", 0) > 1700000000


def test_stamp_display_order_syncs_registry_for_detail_pages(registry_dir):
    pl.remember(pl.build_record("online:playlist:ne:8", "y", "", 1, "category"))
    pl.remember(pl.build_record("online:playlist:ne:9", "x", "", 1, "category"))
    pl.save_registry()
    pl.stamp_display_order([
        {"guid": "online:playlist:ne:8", "name": "y"},
        {"guid": "online:playlist:ne:9", "name": "x"},
    ])
    assert pl.lookup("online:playlist:ne:9")["ts"] == pl.lookup("online:playlist:ne:8")["ts"] + 1


def test_forget_stale_prunes_old_daily_entries(registry_dir):
    """每日推荐注册条目按 seen 过期清理（guid 含日期，不清理会无限累积）。"""
    import time as _time
    fresh = "online:playlist:daily:20260912:user-a"
    stale = "online:playlist:daily:20260101:user-a"
    pl.stamp_display_order([{"guid": fresh, "name": "今日", "trackCount": 3}])
    pl.remember({"guid": stale, "name": "年初", "track_count": 0, "channel": "daily",
                 "ts": 1600000000})
    # remember() 只认标准字段，seen 需要手工补上（模拟 stamp 写入后又过了 15 天）
    reg = pl.load_registry()
    reg[stale]["seen"] = int(_time.time()) - 15 * 86400
    pl.save_registry()
    n = pl.forget_stale(set())
    assert n == 1, "只应清掉 14 天没见过的 daily 条目"
    assert pl.lookup(fresh), "今天刚出现的 daily 条目必须保留"
    assert not pl.lookup(stale)


# ===========================================================================
# v2.4：playlist_list 注入顺序 = 大类顺序配置，且时间戳严格有序
# ===========================================================================

import httpx
from fastapi.testclient import TestClient

from proxy import netease_auth
from proxy.app import app as proxy_app


def _mb_handler_logged_in():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/auth/status":
            return httpx.Response(200, json={"ok": True,
                                             "data": {"logged_in": True, "nickname": "u"}})
        if path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/v1/playlists/user":
            return httpx.Response(200, json={"ok": True, "data": [
                {"playlist_id": 11, "name": "我的自建单", "cover_url": "",
                 "track_count": 5, "subscribed": False}]})
        if path == "/api/v1/playlists/toplists":
            return httpx.Response(200, json={"ok": True, "data": [
                {"playlist_id": 19723756, "name": "飙升榜", "cover_url": "",
                 "track_count": 100}]})
        if path == "/api/v1/recommend/daily":
            return httpx.Response(200, json={"ok": True, "data": [
                {"song_id": "d1", "song_name": "晴天", "artist": "周", "album_name": "叶",
                 "duration": 269, "quality": "SQ"}]})
        if path == "/api/v1/songs/detail":
            return httpx.Response(200, json={"ok": True, "data": [
                {"song_id": "d1", "album_pic_url": "http://p/1.jpg", "has_sq": True}]})
        return httpx.Response(404, json={"ok": False})
    return handler


def _upstream_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/user/me"):
        return httpx.Response(200, json={"code": 0, "data": {"guid": "user-ord"}})
    if path.endswith("/playlist/list"):
        return httpx.Response(200, json={"code": 0, "data": {
            # 本地歌单的真实时间戳是 1.7e9 级（fnOS 2023+），注入条目的展示
            # 时间戳必须比它更小才能在客户端升序排序里排在前面
            "list": [{"guid": "localpl", "name": "本地单", "coverId": "c",
                      "createdAt": 1700000000, "updatedAt": 1700000000}], "total": 1}})
    if "search/track" in path:
        return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
    return httpx.Response(200, json={"code": 0, "data": None})


def test_playlist_list_follows_custom_channel_order(registry_dir, monkeypatch):
    """大类顺序配置必须左右注入顺序（含 daily 的位置），时间戳严格递减。"""
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "mine,toplist")
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_ORDER", "toplist,daily,mine")
    monkeypatch.setenv("FNMUSIC_DAILY_ENABLED", "true")
    proxy_app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_upstream_handler), base_url="http://unix")
    proxy_app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_mb_handler_logged_in()),
        base_url="http://127.0.0.1:8770")
    netease_auth.invalidate_state()

    with TestClient(proxy_app) as client:
        body = client.get("/music/api/v1/playlist/list").json()

    lst = body["data"]["list"]
    names = [it["name"] for it in lst]
    # toplist → daily → mine → 官方本地歌单
    assert names[0].startswith("榜｜"), names
    from proxy import recommend as _rec
    assert _rec.is_daily_playlist_guid(lst[1]["guid"]), names
    assert names[2].startswith("网易云·"), names
    assert lst[-1]["guid"] == "localpl"
    # 展示时间戳严格递增（客户端升序排序 ⇒ 显示顺序=注入顺序），且全部早于本地歌单
    head_ts = [it["updatedAt"] for it in lst[:-1]]
    assert head_ts == sorted(head_ts), head_ts
    assert len(set(head_ts)) == len(head_ts), "时间戳必须互不相同"
    assert max(head_ts) < 1700000000, "必须早于真实 fnOS 歌单的创建时间"
    # 客户端升序排序后，显示顺序应与注入顺序一致：榜 → 日推 → 我的歌单
    asc = sorted(lst, key=lambda x: x["updatedAt"])
    assert [it["name"] for it in asc[:3]] == names[:3]

    # 详情页 / 批量详情页回显的时间戳必须与列表页同一份（客户端按它排序）
    with TestClient(proxy_app) as client2:
        d = client2.get(f"/music/api/v1/playlist/detail?guid={lst[1]['guid']}").json()
        assert d["data"]["createdAt"] == lst[1]["createdAt"], "详情页与列表页时间戳必须一致"
        b = client2.get(f"/music/api/v1/playlist/batch-detail?guids={lst[1]['guid']}").json()
        daily_entry = [x for x in b["data"]["list"]
                       if _rec.is_daily_playlist_guid(str(x.get("guid")))]
        assert daily_entry and daily_entry[0]["createdAt"] == lst[1]["createdAt"]


# ===========================================================================
# v2.5：手动歌单顺序（token 列表，实时读 .env）
# ===========================================================================


def test_explicit_order_tokens_from_live_env(tmp_path, monkeypatch):
    """手动顺序必须实时读 ${FNMUSIC_HOME}/.env——用户在管理页保存后，
    下一次 playlist/list 就是新顺序，不等代理重启。"""
    monkeypatch.setenv("FNMUSIC_HOME", str(tmp_path))
    pl._reset_live_env_cache_for_test()

    # 没写 .env 时回落进程环境变量
    monkeypatch.setenv("FNMUSIC_NETEASE_PLAYLIST_ORDER", "daily, online:playlist:ne:7")
    assert pl.explicit_order_tokens() == ("daily", "online:playlist:ne:7")

    # 写入 .env 后立即以文件为准（含空值——用户清空就是清空，不能回落旧环境变量）
    (tmp_path / ".env").write_text(
        "FNMUSIC_NETEASE_PLAYLIST_ORDER='toplist,online:playlist:ne:1,daily'\n",
        encoding="utf-8")
    assert pl.explicit_order_tokens() == ("toplist", "online:playlist:ne:1", "daily")

    # 文件内容变化（mtime/size 指纹变化）后自动重读
    (tmp_path / ".env").write_text("FNMUSIC_NETEASE_PLAYLIST_ORDER=''\n", encoding="utf-8")
    assert pl.explicit_order_tokens() == (), "清空 .env 里的顺序 = 回到大类顺序"
    pl._reset_live_env_cache_for_test()


def test_apply_explicit_order_overrides_and_appends():
    items = [
        {"guid": "online:playlist:daily:20260912:u1", "name": "每日推荐"},
        {"guid": "online:playlist:ne:11", "name": "我的自建单"},
        {"guid": "online:playlist:ne:12", "name": "收藏的单"},
        {"guid": "online:playlist:ne:19723756", "name": "飙升榜"},
        {"guid": "online:playlist:nefm", "name": "私人FM"},
    ]
    # 手动顺序：飙升榜 → 每日推荐 → 我的自建单；没排到的（收藏的单/私人FM）按原相对顺序跟在后面
    ordered = pl.apply_explicit_order_with(
        items, ("online:playlist:ne:19723756", "daily", "online:playlist:ne:11"))
    assert [it["name"] for it in ordered] == \
        ["飙升榜", "每日推荐", "我的自建单", "收藏的单", "私人FM"]

    # 空 token 列表 = 原样返回（大类顺序继续生效）
    assert pl.apply_explicit_order_with(items, ()) == items

    # 全部未知 token = 不影响顺序（防止手滑把整个列表打乱）
    assert [it["name"] for it in pl.apply_explicit_order_with(
        items, ("bogus", "online:playlist:ne:999"))] == \
        [it["name"] for it in items]


def test_playlist_list_applies_manual_order(registry_dir, monkeypatch):
    """playlist_list 注入顺序被手动 token 列表整体覆盖（每日推荐可被排到后面）。"""
    import httpx as _hx
    from fastapi.testclient import TestClient as _TC
    from proxy import netease_auth as _na
    from proxy.app import app as _app

    monkeypatch.setenv("FNMUSIC_HOME", str(registry_dir.parent))  # 无 .env → 回落环境变量
    pl._reset_live_env_cache_for_test()
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "mine,toplist")
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_ORDER", "toplist,daily,mine")
    monkeypatch.setenv("FNMUSIC_DAILY_ENABLED", "true")
    monkeypatch.setenv("FNMUSIC_NETEASE_PLAYLIST_ORDER",
                       "online:playlist:ne:11,daily,online:playlist:ne:19723756")
    _app.state.upstream_client = _hx.AsyncClient(
        transport=_hx.MockTransport(_upstream_handler), base_url="http://unix")
    _app.state.musicbox_client = _hx.AsyncClient(
        transport=_hx.MockTransport(_mb_handler_logged_in()), base_url="http://127.0.0.1:8770")
    _na.invalidate_state()

    with _TC(_app) as client:
        body = client.get("/music/api/v1/playlist/list").json()
    lst = body["data"]["list"]
    names = [it["name"] for it in lst]
    # 手动顺序：我的自建单 → 每日推荐 → 飙升榜 → 本地歌单
    assert names[0].startswith("网易云·我的自建单"), names
    from proxy import recommend as _rec
    assert _rec.is_daily_playlist_guid(lst[1]["guid"]), names
    assert names[2].startswith("榜｜"), names
    assert lst[-1]["guid"] == "localpl"
    # 展示时间戳仍然严格递增（顺序=注入顺序）
    head_ts = [it["updatedAt"] for it in lst[:-1]]
    assert head_ts == sorted(head_ts) and len(set(head_ts)) == len(head_ts)


def test_playlists_preview_endpoint(registry_dir, monkeypatch):
    """管理页「歌单顺序」卡片的数据源：清单与顺序和 playlist_list 同源。"""
    import httpx as _hx
    from fastapi.testclient import TestClient as _TC
    from proxy import netease_auth as _na
    from proxy.app import app as _app

    monkeypatch.setenv("FNMUSIC_HOME", str(registry_dir.parent))
    pl._reset_live_env_cache_for_test()
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "mine,toplist")
    monkeypatch.setenv("FNMUSIC_NETEASE_PLAYLIST_ORDER", "daily,online:playlist:ne:19723756")
    _app.state.upstream_client = _hx.AsyncClient(
        transport=_hx.MockTransport(_upstream_handler), base_url="http://unix")
    _app.state.musicbox_client = _hx.AsyncClient(
        transport=_hx.MockTransport(_mb_handler_logged_in()), base_url="http://127.0.0.1:8770")
    _na.invalidate_state()

    with _TC(_app) as client:
        r = client.get("/_ext/playlists/preview")
    assert r.status_code == 200
    data = r.json()["data"]
    names = [it["name"] for it in data["items"]]
    assert names[0].startswith("每日推荐"), names
    assert names[1].startswith("榜｜"), names
    assert any(n.startswith("网易云·") for n in names), names
    assert data["items"][0]["is_daily"] is True
    assert data["manual_order"] == ["daily", "online:playlist:ne:19723756"]
    assert data["logged_in"] is True


# ===========================================================================
# v2.6：歌单曲目缓存（stale-while-revalidate + 定时刷新）
# ===========================================================================


def test_tracks_cache_roundtrip_and_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_PLAYLIST_TRACK_CACHE_DIR", str(tmp_path))
    assert pl.load_cached_tracks("online:playlist:ne:11") is None, "无缓存返回 None"

    items = [{"id": "online:netease:1", "title": "晴天", "artist": "周杰伦"}]
    assert pl.store_cached_tracks("online:playlist:ne:11", items) is True
    ts, got = pl.load_cached_tracks("online:playlist:ne:11")
    assert got == items
    assert (time.time() - ts) < 10

    # 空列表不落盘：上游抖动不该把好缓存覆盖成空的
    assert pl.store_cached_tracks("online:playlist:ne:11", []) is False
    monkeypatch.setenv("FNMUSIC_PLAYLIST_TRACK_CACHE_TTL", "60")
    assert pl.tracks_cache_ttl() == 60
    # guid 里的冒号等字符被安全转义，不会跑出缓存目录
    p = pl._tracks_cache_path("online:playlist:ne:11/../../etc")
    assert os.path.dirname(p) == pl.tracks_cache_dir()


def test_tracks_cache_bad_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_PLAYLIST_TRACK_CACHE_DIR", str(tmp_path))
    (tmp_path / "x.json").write_text("{not json", encoding="utf-8")
    assert pl.load_cached_tracks("x") is None


def test_daily_refresh_time_parsing(monkeypatch):
    monkeypatch.setenv("FNMUSIC_PLAYLIST_REFRESH_AT", "04:30")
    assert pl.refresh_time_of_day() == "04:30"
    d = pl.seconds_until_daily_refresh()
    assert d is not None and 0 < d <= 86400

    monkeypatch.setenv("FNMUSIC_PLAYLIST_REFRESH_AT", "")
    assert pl.seconds_until_daily_refresh() is None, "留空 = 关闭定时刷新"
    for bad in ("bogus", "25:00", "12:99", "4pm"):
        monkeypatch.setenv("FNMUSIC_PLAYLIST_REFRESH_AT", bad)
        assert pl.seconds_until_daily_refresh() is None, bad


def test_forget_stale_drops_tracks_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_PLAYLIST_TRACK_CACHE_DIR", str(tmp_path))
    pl.remember(pl.build_record("online:playlist:ne:1", "a", "", 1, "toplist"))
    pl.store_cached_tracks("online:playlist:ne:1", [{"id": "online:netease:9"}])
    pl.save_registry()
    n = pl.forget_stale(set())
    assert n == 1
    assert pl.load_cached_tracks("online:playlist:ne:1") is None, \
        "注册表条目被清掉时，曲目缓存文件也该一起删"


# ===========================================================================
# 本地每日推荐的排序（v2.9.8）
#
# 它排在列表第几位，取决于三处互斥的逻辑，任何一处漏掉就会沉底：
#   1. channel_order() 的大类顺序
#   2. apply_explicit_order() 的手动顺序（对大类顺序是"整体覆盖"）
#   3. pin_local_daily_first() 的最后兜底
# ===========================================================================


def test_local_daily_token_matches_by_prefix(monkeypatch):
    """guid 带日期和用户，token 必须按前缀认领，不能全等比较。"""
    guid = "online:playlist:localdaily:20260914:u-abc"
    assert pl._token_matches("localdaily", guid), "写死 guid 第二天就失配，必须前缀匹配"
    assert pl._token_matches("localdaily", "online:playlist:localdaily:20260915:u-abc")
    assert not pl._token_matches("localdaily", "online:playlist:daily:20260914:u-abc")
    assert not pl._token_matches("localdaily", "online:playlist:ne:123")


def test_local_daily_survives_explicit_order_without_its_token(monkeypatch):
    """手动顺序里没有 localdaily / 存的是昨天的 guid 时，它不能被甩到最后。"""
    monkeypatch.delenv("FNMUSIC_NETEASE_CHANNEL_ORDER", raising=False)
    items = [
        {"guid": "online:playlist:daily:20260914:u1", "name": "每日推荐"},
        {"guid": "online:playlist:ne:1", "name": "我的歌单"},
        {"guid": "online:playlist:localdaily:20260914:u1", "name": "本地每日推荐"},
    ]
    # 手动顺序只认得网易云歌单：localdaily 匹配不上，会被归到"未匹配"组
    out = pl.apply_explicit_order_with(items, ("online:playlist:daily:20260914:u1",
                                               "online:playlist:ne:1"))
    assert out[-1]["guid"].startswith(pl.LOCAL_DAILY_NS), "先确认问题确实存在"

    pinned = pl.pin_local_daily_first(out)
    assert pinned[0]["guid"].startswith(pl.LOCAL_DAILY_NS), "兜底后必须回到第一位"
    assert [i["name"] for i in pinned[1:]] == ["每日推荐", "我的歌单"], "其余相对顺序不变"


def test_pin_respects_explicit_channel_order_position(monkeypatch):
    """用户在大类顺序里显式给了位置，就以配置为准（可以故意排后面）。"""
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_ORDER",
                       "daily,mine,localdaily,nrec,toplist,category,newalbum,fm")
    assert pl.local_daily_pinned_index() == 2
    items = [
        {"guid": "online:playlist:daily:20260914:u1"},
        {"guid": "online:playlist:ne:1"},
        {"guid": "online:playlist:localdaily:20260914:u1"},
    ]
    out = pl.pin_local_daily_first(list(items))
    assert out[2]["guid"].startswith(pl.LOCAL_DAILY_NS)


def test_pin_defaults_to_head_when_order_unset(monkeypatch):
    """没配置大类顺序（默认）时，本地每日推荐排第一。"""
    monkeypatch.delenv("FNMUSIC_NETEASE_CHANNEL_ORDER", raising=False)
    assert pl.local_daily_pinned_index() == 0
    assert pl.channel_order()[0] == "localdaily"


def test_pin_is_noop_without_local_daily():
    items = [{"guid": "online:playlist:ne:1"}, {"guid": "online:playlist:ne:2"}]
    assert pl.pin_local_daily_first(list(items)) == items
    assert pl.pin_local_daily_first([]) == []


def test_channel_order_default_puts_local_daily_first(monkeypatch):
    """四处默认值必须一致，否则用户保存一次配置顺序就乱。"""
    monkeypatch.delenv("FNMUSIC_NETEASE_CHANNEL_ORDER", raising=False)
    assert pl.DEFAULT_CHANNEL_ORDER.split(",")[0] == "localdaily"
    from proxy import env_merge
    d = dict(env_merge.NEW_DEFAULTS)
    assert d["FNMUSIC_NETEASE_CHANNEL_ORDER"].split(",")[0] == "localdaily", \
        "env_merge 的默认值漏改了，新装用户会拿到 daily 在前"
