"""v2.6 回归：歌单曲目缓存——秒开（先读缓存）、stale-while-revalidate、预热。

真机痛点：点开一个伪歌单要现场跑完整上游链路（trackIds → songs_detail →
songs_url 逐首过滤 → 补封面），好几秒。缓存后：
  * 命中且新鲜 → 零上游往返；
  * 命中但过 TTL → 先返回旧值（打开永远快），后台单飞刷新；
  * 打开歌单列表 → 自动安排一次后台预热（冷却期 = TTL）；
  * /_ext/playlists/warm → 立即全量预热（管理页按钮）。
"""
from __future__ import annotations

import json
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import netease_auth, playlists as pl
from proxy.app import app


GUID = "online:playlist:ne:424242"

CALLS: dict = {}


def _mb_tracks(song_name: str) -> dict:
    return {"ok": True, "data": [
        {"song_id": 2706544264, "song_name": song_name, "artist": "周杰伦",
         "album_name": "叶惠美", "duration": 269, "quality": "LOSSLESS FLAC",
         "mp3_url": "http://m/1.flac"}], "engine": "in-process"}


def musicbox_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    CALLS[path] = CALLS.get(path, 0) + 1
    if path == "/api/v1/auth/status":
        return httpx.Response(200, json={"ok": True,
                                         "data": {"logged_in": True, "nickname": "u"}})
    if path == "/healthz":
        return httpx.Response(200, json={"ok": True})
    if path == "/api/v1/playlists/toplists":
        # 当前口径在列的排行榜（guid 与 GUID 一致，注册表与当前清单对齐）
        return httpx.Response(200, json={"ok": True, "data": [
            {"playlist_id": 424242, "name": "测试榜", "cover_url": "", "track_count": 1}]})
    if path == f"/api/v1/playlist/424242/tracks":
        return httpx.Response(200, json=_mb_tracks(os.environ.get("_MB_SONG", "晴天")))
    if path == "/api/v1/songs/detail":
        return httpx.Response(200, json={"ok": True, "data": [
            {"song_id": "2706544264", "album_pic_url": "http://pic/1.jpg",
             "has_sq": True, "album_name": "叶惠美", "artist": "周杰伦"}]})
    if path.startswith("/api/v1/playlists/"):
        return httpx.Response(200, json={"ok": True, "data": []})
    return httpx.Response(404, json={"ok": False})


def upstream_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/user/me"):
        return httpx.Response(200, json={"code": 0, "data": {"guid": "user-cache"}})
    if path.endswith("/playlist/list"):
        return httpx.Response(200, json={"code": 0, "data": {
            "list": [{"guid": "localpl", "name": "本地单", "coverId": "c",
                      "createdAt": 1700000000, "updatedAt": 1700000000}], "total": 1}})
    if "search/track" in path:
        return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
    return httpx.Response(200, json={"code": 0, "data": None})


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    CALLS.clear()
    monkeypatch.setenv("FNMUSIC_HOME", str(tmp_path))
    monkeypatch.setenv("FNMUSIC_PLAYLIST_TRACK_CACHE_DIR", str(tmp_path / "tc"))
    monkeypatch.setenv("FNMUSIC_PLAYLIST_TRACK_CACHE_TTL", "3600")
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "toplist")
    monkeypatch.delenv("FNMUSIC_NETEASE_PLAYLIST_ORDER", raising=False)
    pl._reset_live_env_cache_for_test()
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770")
    netease_auth.invalidate_state()
    yield
    CALLS.clear()


def _open_playlist(client) -> dict:
    r = client.get(f"/music/api/v1/track/playlist-detail/list?playlistGUID={GUID}&page=1&size=50")
    assert r.status_code == 200
    return r.json()


def test_second_open_hits_cache_zero_upstream(wired):
    with TestClient(app) as client:
        first = _open_playlist(client)
        assert first["data"]["total"] == 1
        assert CALLS[f"/api/v1/playlist/424242/tracks"] == 1, "首次打开应现场拉取"

        second = _open_playlist(client)
        assert second["data"]["total"] == 1
        assert second["data"]["list"][0]["title"] == first["data"]["list"][0]["title"]
    assert CALLS[f"/api/v1/playlist/424242/tracks"] == 1, \
        "缓存命中时绝不能再打上游——这是「打开秒开」的全部"


def test_stale_cache_returns_old_then_refreshes_in_background(wired, monkeypatch):
    # 预热一份缓存（晴天），然后把它变陈旧、并让上游改返回「七里香」
    with TestClient(app) as client:
        _open_playlist(client)
    assert pl.load_cached_tracks(GUID) is not None

    path = pl._tracks_cache_path(GUID)
    with open(path, encoding="utf-8") as fh:
        body = json.loads(fh.read())
    body["ts"] = time.time() - 7200  # 超过 1h 的 TTL
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(body))
    monkeypatch.setenv("_MB_SONG", "七里香")

    with TestClient(app) as client:
        got = _open_playlist(client)
        # 1. 先返回旧值：打开永远是快的，内容是上一次的
        assert got["data"]["list"][0]["title"] == "晴天", "陈旧缓存应先返回旧值"

        # 2. 后台单飞刷新最终落盘新值（轮询等待，上限 5s）
        deadline = time.time() + 5
        refreshed = None
        while time.time() < deadline:
            hit = pl.load_cached_tracks(GUID)
            if hit and hit[1] and hit[1][0].get("title") == "七里香":
                refreshed = True
                break
            time.sleep(0.1)
        assert refreshed, "后台刷新应把新内容写进缓存"

        # 3. 再打开：返回新值，且没有引发重复刷新
        got2 = _open_playlist(client)
        assert got2["data"]["list"][0]["title"] == "七里香"


def test_refresh_failure_keeps_old_cache(wired, monkeypatch):
    with TestClient(app) as client:
        _open_playlist(client)
    path = pl._tracks_cache_path(GUID)
    with open(path, encoding="utf-8") as fh:
        body = json.loads(fh.read())
    body["ts"] = time.time() - 7200
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(body))

    def broken_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v1/playlist/424242/tracks":
            return httpx.Response(500)
        return musicbox_handler(request)

    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(broken_handler), base_url="http://127.0.0.1:8770")
    with TestClient(app) as client:
        got = _open_playlist(client)
        assert got["data"]["total"] == 1, "上游炸了也得有旧缓存兜底"
    deadline = time.time() + 3
    while time.time() < deadline:
        if pl.load_cached_tracks(GUID) is not None:
            break
        time.sleep(0.1)
    assert pl.load_cached_tracks(GUID) is not None, "刷新失败绝不能清掉旧缓存"


def test_warm_endpoint_refreshes_all_registry_playlists(wired):
    """预热按钮只刷**当前在列**歌单；注册表里的历史死条目既不刷、也被清掉。"""
    # 注册表塞两条：一条当前在列（GUID），一条历史遗留（旧分类的歌单）
    stale = "online:playlist:ne:777777"
    pl.remember(pl.build_record(GUID, "榜｜测试榜", "", 1, "toplist"))
    pl.remember(pl.build_record(stale, "华语｜历史死条目", "", 1, "category"))
    pl.store_cached_tracks(stale, [{"id": "netease:1", "source": "netease",
                                    "title": "x", "artist": "a", "album": "",
                                    "duration_s": 1, "ext": "mp3",
                                    "cover_url": "", "lyric": ""}])
    pl.save_registry()
    with TestClient(app) as client:
        r = client.post("/_ext/playlists/warm")
        assert r.status_code == 200
        assert r.json()["data"]["started"] is True
        assert r.json()["data"]["total"] == 1, "只应预热当前在列的那 1 个歌单"

        deadline = time.time() + 5
        while time.time() < deadline:
            if pl.load_cached_tracks(GUID) is not None:
                break
            time.sleep(0.1)
    assert pl.load_cached_tracks(GUID) is not None, "预热应把当前在列歌单写进缓存"
    # 历史死条目被 forget_stale 清理：注册表不再有它，曲目缓存也一并删掉
    assert pl.lookup(stale) == {}, "清单完整时历史死条目应从注册表清掉"
    assert pl.load_cached_tracks(stale) is None, "死条目的曲目缓存应一并删除"

    # 重复触发：已在跑/刚跑完时幂等，不炸
    with TestClient(app) as client:
        r = client.post("/_ext/playlists/warm")
        assert r.status_code == 200


def test_preview_reports_cache_stats(wired):
    """preview 的缓存统计：分子只数当前在列歌单，绝不能出现 cached > total。"""
    # 先打开一次让 GUID 有缓存；再往注册表塞一个有缓存的历史死条目
    with TestClient(app) as client:
        _open_playlist(client)
    stale = "online:playlist:ne:888888"
    pl.remember(pl.build_record(GUID, "榜｜测试榜", "", 1, "toplist"))
    pl.remember(pl.build_record(stale, "华语｜历史死条目", "", 1, "category"))
    pl.store_cached_tracks(stale, [{"id": "netease:1", "source": "netease",
                                    "title": "x", "artist": "a", "album": "",
                                    "duration_s": 1, "ext": "mp3",
                                    "cover_url": "", "lyric": ""}])
    pl.save_registry()

    with TestClient(app) as client:
        r = client.get("/_ext/playlists/preview")
        assert r.status_code == 200
        cache = r.json()["data"]["cache"]
        assert cache["ttl_s"] == 3600
        assert cache["refresh_at"] == "04:30"
        # 当前在列只有 toplist 里的 GUID 一个（daily 未登录不注入，也不计入 total）
        assert cache["total"] == 1
        assert cache["cached"] == 1, "当前在列且已缓存的歌单"
        assert cache["cached"] <= cache["total"], "分子绝不能大于分母"


# ---------------------------------------------------------------------------
# v2.8.2：预热跳过「仍新鲜」的缓存（定时刷新/自动预热），手动按钮全量
# ---------------------------------------------------------------------------


def _fake_item(name: str = "晴天") -> dict:
    return {"id": "netease:2706544264", "source": "netease", "title": name,
            "artist": "周杰伦", "album": "叶惠美", "duration_s": 269,
            "ext": "flac", "cover_url": "", "lyric": ""}


@pytest.mark.anyio
async def test_warm_skips_fresh_caches_but_refetches_new_and_stale(wired):
    """定时刷新/自动预热：一小时内刷过的跳过；新出现的（轮换口径）和过期的照常拉。"""
    import proxy.app as pa

    # GUID 已有「新鲜」缓存（刚写入）
    pl.store_cached_tracks(GUID, [_fake_item()])
    # 再造一个「过期」缓存和一个「无缓存」的 guid
    stale_guid = "online:playlist:ne:515151"
    fresh2_guid = "online:playlist:ne:616161"
    pl.store_cached_tracks(stale_guid, [_fake_item("旧歌")])
    pl.store_cached_tracks(fresh2_guid, [_fake_item("新碟歌")])
    # 把 stale_guid 与 fresh2 的 ts 分别调到 2 小时前 / 5 分钟前
    for g, age in ((stale_guid, 7200), (fresh2_guid, 300)):
        p = pl._tracks_cache_path(g)
        body = json.loads(open(p, encoding="utf-8").read())
        body["ts"] = time.time() - age
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(body))
    new_guid = "online:playlist:ne:717171"      # 无缓存（模拟新碟轮换进来的新歌单）

    with TestClient(app) as client:
        r = await pa._warm_playlist_caches(app, guids=[GUID, stale_guid, fresh2_guid, new_guid],
                                           skip_fresh_s=3600.0)
    assert r["started"] is True
    assert r["total"] == 4
    assert r["skipped_fresh"] == 2, "GUID 与 fresh2（5 分钟前）应被跳过"
    # 新 guid 与过期 guid 各被拉了一次上游（4 - 2 跳过 = 2 次拉取）
    fetched = sum(n for path, n in CALLS.items() if "/tracks" in path and "424242" not in path)
    assert CALLS.get("/api/v1/playlist/717171/tracks", 0) == 1, "无缓存的新歌单必须拉"
    assert CALLS.get("/api/v1/playlist/515151/tracks", 0) == 1, "过期缓存必须重拉"
    assert CALLS.get("/api/v1/playlist/616161/tracks", 0) == 0, "新鲜缓存不应重拉"


@pytest.mark.anyio
async def test_warm_manual_button_ignores_fresh_threshold(wired):
    """手动按钮（skip_fresh_s=0）：即使全部新鲜也全量刷新——按了按钮就是要刷新。"""
    import proxy.app as pa

    pl.store_cached_tracks(GUID, [_fake_item()])
    with TestClient(app) as client:
        r = await pa._warm_playlist_caches(app, guids=[GUID], skip_fresh_s=0.0)
    assert r["started"] is True
    assert r["skipped_fresh"] == 0
    deadline = time.time() + 5
    while time.time() < deadline:
        if CALLS.get("/api/v1/playlist/424242/tracks", 0) >= 1:
            break
        time.sleep(0.1)
    assert CALLS.get("/api/v1/playlist/424242/tracks", 0) == 1, "手动全量刷新必须真拉上游"
