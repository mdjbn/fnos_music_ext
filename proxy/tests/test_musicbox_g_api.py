"""W5 增量移植的独立测试：只覆盖 A 侧**新增**的 musicbox-service API 面。

本文件是纯加法：
- 不改写、不覆盖 ``proxy/tests/test_musicbox_service.py``（A 的基线测试）；
- 只覆盖新增的 15 条 ``/api/v1`` 路由 + ``netease_ext.py``/``runner.py`` 的新函数；
- 全部用 fake client / TestClient，绝不发真实网络请求、不碰真实 NEMbox 安装。

用例思路借鉴了 G（gzywd v2.9.30）的测试，但断言与打桩点全部按 A 的代码风格重写
（A 用 importlib 以独立模块名加载 app.py；G 直接 ``from app import app``）。
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# musicbox-service 内部是裸导入（import runner / import netease_ext）
MUSICBOX_SERVICE_DIR = Path(__file__).resolve().parent.parent.parent / "musicbox-service"
if str(MUSICBOX_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(MUSICBOX_SERVICE_DIR))

import netease_ext  # noqa: E402
import runner  # noqa: E402

# 复用同一 pytest 会话里已加载的 app 模块，避免重复执行 app.py 造成两套模块级状态
_APP_MODULE_NAME = "musicbox_service_app"
musicbox_app = sys.modules.get(_APP_MODULE_NAME)
if musicbox_app is None:
    _spec = importlib.util.spec_from_file_location(_APP_MODULE_NAME, MUSICBOX_SERVICE_DIR / "app.py")
    musicbox_app = importlib.util.module_from_spec(_spec)
    sys.modules[_APP_MODULE_NAME] = musicbox_app
    _spec.loader.exec_module(musicbox_app)

app = musicbox_app.app

# PORT_PLAN.md 里 W5 要求补齐的 15 条 G 侧路由
G_API_PATHS = (
    "/api/v1/album/{album_id}/tracks",
    "/api/v1/auth/detail",
    "/api/v1/channels/selftest",
    "/api/v1/playlist/{playlist_id}/tracks",
    "/api/v1/playlists/categories",
    "/api/v1/playlists/category",
    "/api/v1/playlists/newalbums",
    "/api/v1/playlists/recommend",
    "/api/v1/playlists/toplists",
    "/api/v1/playlists/user",
    "/api/v1/radio/fm",
    "/api/v1/recommend/daily",
    "/api/v1/selftest",
    "/api/v1/song/{song_id}/best_url",
    "/api/v1/song/{song_id}/like",
)


@pytest.fixture(autouse=True)
def reset_musicbox_state(monkeypatch):
    """模块级状态隔离：登录态缓存、取链缓存、实例、CLI 解析缓存与探测缓存。"""
    monkeypatch.setattr(netease_ext, "_login_state", None)
    monkeypatch.setattr(netease_ext, "_url_cache", {})
    monkeypatch.setattr(netease_ext, "_api_instance", None)
    monkeypatch.setattr(runner, "_CMD_CACHE", None)
    musicbox_app.reset_cli_probe_for_test()
    yield
    musicbox_app.reset_cli_probe_for_test()


class FakeApi:
    """最小 NetEase 替身：只暴露被测函数会调用的属性/方法。"""

    def __init__(self, **methods):
        for name, fn in methods.items():
            setattr(self, name, fn)


def _patch_api(monkeypatch, api):
    monkeypatch.setattr(netease_ext, "_get_api_locked", lambda: api)
    return api


def _url_item(sid, url="http://cdn.example/a.mp3", code=200, level="exhigh",
              br=320000, typ="mp3", **extra):
    item = {"id": sid, "url": url, "code": code, "level": level, "br": br, "type": typ}
    item.update(extra)
    return item


def _raw_song(sid, name=None):
    return {"id": sid, "name": name or f"歌曲 {sid}", "ar": [{"name": "歌手"}],
            "al": {"id": 900 + sid, "name": "专辑", "picUrl": "http://pic/x.jpg"},
            "dt": 245000, "sq": {"br": 999000}, "hr": None}


# ===========================================================================
# 1) 路由存在性：以后谁删了这些端点，这里立刻红
# ===========================================================================

def test_all_g_api_routes_are_registered():
    registered = {getattr(route, "path", None) for route in app.routes}
    missing = [path for path in G_API_PATHS if path not in registered]
    assert missing == [], f"W5 移植的端点缺失: {missing}"


def test_like_route_accepts_both_get_and_post():
    methods_by_path = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        if path:
            methods_by_path.setdefault(path, set()).update(getattr(route, "methods", set()) or set())
    like_methods = methods_by_path["/api/v1/song/{song_id}/like"]
    assert {"GET", "POST"} <= like_methods


def test_original_a_routes_are_untouched():
    """只增不改的回归护栏：A 既有端点在移植后必须全部还在。"""
    registered = {getattr(route, "path", None) for route in app.routes}
    for path in (
        "/healthz",
        "/api/v1/search",
        "/api/v1/song/{song_id}/url",
        "/api/v1/song/{song_id}/info",
        "/api/v1/songs/detail",
        "/api/v1/song/{song_id}/lyric",
        "/api/v1/artist/{artist_id}",
        "/api/v1/album/{album_id}",
        "/api/v1/playlist/{playlist_id}",
        "/api/v1/user/playlists",
        "/api/v1/user/playlists/{playlist_id}/tracks",
        "/api/v1/recommend/songs",
        "/api/v1/toplist",
        "/api/v1/auth/status",
        "/api/v1/auth/login",
        "/api/v1/auth/login/check",
        "/api/v1/auth/login/qr.png",
        "/api/v1/auth/qr.png",
        "/api/v1/auth/login/qr",
        "/api/v1/auth/qr",
    ):
        assert path in registered, f"A 既有端点被改动/删除: {path}"


def test_compat_helpers_do_not_shadow_a_functions():
    """冲突项的兼容命名：G 语义用带后缀的新函数承载，A 既有函数签名不变。"""
    import inspect

    assert list(inspect.signature(netease_ext.playlist_track_ids).parameters) == ["playlist_id"]
    assert list(inspect.signature(netease_ext.user_playlists).parameters) == ["limit"]
    assert list(inspect.signature(netease_ext.user_playlists_for_uid).parameters) == [
        "uid", "offset", "limit"]
    assert list(inspect.signature(netease_ext.playlist_track_ids_limited).parameters) == [
        "playlist_id", "limit"]


# ===========================================================================
# 2) /api/v1/selftest 与 CLI 探测缓存
# ===========================================================================

def test_selftest_reports_resolved_cli(monkeypatch):
    monkeypatch.setattr(runner, "musicbox_cmd", lambda: (["/opt/venv/bin/musicbox"], "absolute:/opt/venv/bin/musicbox"))
    monkeypatch.setattr(runner, "bin_dir", lambda: "/opt/venv/bin")
    monkeypatch.setattr(runner, "run_musicbox_resolved", lambda args, timeout=3.0: (0, "NEMbox 0.5.3", ""))

    with TestClient(app) as client:
        resp = client.get("/api/v1/selftest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["cli_found"] is True
    assert data["cli_cmd"] == ["/opt/venv/bin/musicbox"]  # 与 G 一致：argv 列表而非字符串
    assert data["resolved_by"] == "absolute:/opt/venv/bin/musicbox"
    assert data["venv_bin_dir"] == "/opt/venv/bin"
    assert data["cli_exec_ok"] is True
    assert data["cli_exec_detail"] == "NEMbox 0.5.3"
    assert set(data["xdg"]) == {"XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"}


def test_selftest_without_cli_does_not_probe(monkeypatch):
    monkeypatch.setattr(runner, "musicbox_cmd", lambda: ([], "not_found:/a/musicbox,/b/musicbox"))
    monkeypatch.setattr(runner, "bin_dir", lambda: "/a")
    calls = []
    monkeypatch.setattr(runner, "run_musicbox_resolved",
                        lambda args, timeout=3.0: calls.append(args) or (0, "", ""))

    with TestClient(app) as client:
        data = client.get("/api/v1/selftest").json()["data"]
    assert data["cli_found"] is False
    assert data["cli_exec_ok"] is False
    assert data["cli_exec_detail"] == ""
    assert calls == []


def test_probe_cli_exec_caches_deterministic_result(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run_musicbox_resolved",
                        lambda args, timeout=3.0: calls.append(args) or (0, "v1", ""))

    assert musicbox_app.probe_cli_exec(timeout_s=3.0) == (True, "v1")
    assert musicbox_app.probe_cli_exec(timeout_s=3.0) == (True, "v1")
    assert len(calls) == 1


def test_probe_cli_exec_does_not_cache_timeout(monkeypatch):
    calls = []

    def boom(args, timeout=3.0):
        calls.append(args)
        raise runner.MusicboxTimeoutError("cold start")

    monkeypatch.setattr(runner, "run_musicbox_resolved", boom)

    ok, detail = musicbox_app.probe_cli_exec(timeout_s=3.0)
    assert ok is False
    assert "MusicboxTimeoutError" in detail and "冷启动" in detail
    # 超时不缓存：下次调用要重新探测
    musicbox_app.probe_cli_exec(timeout_s=3.0)
    assert len(calls) == 2


def test_probe_cli_exec_caches_unexpected_exception(monkeypatch):
    calls = []

    def boom(args, timeout=3.0):
        calls.append(args)
        raise RuntimeError("weird")

    monkeypatch.setattr(runner, "run_musicbox_resolved", boom)
    ok, detail = musicbox_app.probe_cli_exec(timeout_s=3.0)
    assert ok is False and detail == "RuntimeError: weird"
    musicbox_app.probe_cli_exec(timeout_s=3.0)
    assert len(calls) == 1  # 确定性失败也会缓存


# ===========================================================================
# 3) /api/v1/auth/detail
# ===========================================================================

def test_auth_detail_returns_account_fields(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_auth_detail", lambda: {
        "logged_in": True, "nickname": "听歌的人", "user_id": "10086",
        "vip_type": 11, "vip_expires_ms": 0, "vip_expires_known": False})

    with TestClient(app) as client:
        body = client.get("/api/v1/auth/detail").json()
    assert body["ok"] is True
    assert body["data"]["logged_in"] is True
    assert body["data"]["user_id"] == "10086"
    assert body["data"]["vip_expires_known"] is False


def test_auth_detail_degrades_to_not_logged_in_on_error(monkeypatch):
    def boom():
        raise RuntimeError("upstream down")

    monkeypatch.setattr(musicbox_app, "ne_auth_detail", boom)
    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/detail")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["logged_in"] is False
    assert data["error"] == "upstream down"
    assert len(data["error"]) <= 200


def test_auth_detail_truncates_long_error(monkeypatch):
    def boom():
        raise RuntimeError("x" * 500)

    monkeypatch.setattr(musicbox_app, "ne_auth_detail", boom)
    with TestClient(app) as client:
        data = client.get("/api/v1/auth/detail").json()["data"]
    assert data["logged_in"] is False
    assert len(data["error"]) == 200


# ===========================================================================
# 4) /api/v1/recommend/daily
# ===========================================================================

def test_recommend_daily_not_logged_in_short_circuits(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: False)
    cli_calls = []
    monkeypatch.setattr(runner, "run_musicbox", lambda args, timeout=40.0: cli_calls.append(args) or (0, "", ""))

    with TestClient(app) as client:
        body = client.get("/api/v1/recommend/daily").json()
    assert body == {"ok": False, "error": "not_logged_in", "data": []}
    assert cli_calls == []


def test_recommend_daily_in_process(monkeypatch):
    rows = [{"song_id": i, "song_name": f"s{i}"} for i in range(1, 6)]
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: True)
    monkeypatch.setattr(musicbox_app, "ne_daily_songs", lambda limit=20: rows)

    with TestClient(app) as client:
        body = client.get("/api/v1/recommend/daily", params={"limit": 3}).json()
    assert body["ok"] is True
    assert body["engine"] == "in-process"
    assert [r["song_id"] for r in body["data"]] == [1, 2, 3]


def test_recommend_daily_limit_out_of_range_is_rejected():
    with TestClient(app) as client:
        # 本仓库的 FastAPI/Starlette 组合把请求校验错误映射为 400（A 既有测试同样如此）
        assert client.get("/api/v1/recommend/daily", params={"limit": 0}).status_code == 400
        assert client.get("/api/v1/recommend/daily", params={"limit": 101}).status_code == 400


def test_recommend_daily_falls_back_to_cli_and_filters(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: True)
    monkeypatch.setattr(musicbox_app, "ne_daily_songs", lambda limit=20: [])
    payload = {"ok": True, "data": [{"song_id": 1}, {"song_id": 2}, {"song_id": 3}]}
    monkeypatch.setattr(runner, "run_musicbox",
                        lambda args, timeout=40.0: (0, json.dumps(payload), ""))
    monkeypatch.setattr(musicbox_app, "filter_playable_song_ids", lambda ids: {1, 3})

    with TestClient(app) as client:
        body = client.get("/api/v1/recommend/daily").json()
    assert body["ok"] is True
    assert body["engine"] == "cli-fallback"
    assert [r["song_id"] for r in body["data"]] == [1, 3]


def test_recommend_daily_cli_exit_3_means_not_logged_in(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: True)
    monkeypatch.setattr(musicbox_app, "ne_daily_songs", lambda limit=20: [])
    monkeypatch.setattr(runner, "run_musicbox", lambda args, timeout=40.0: (3, "", ""))

    with TestClient(app) as client:
        body = client.get("/api/v1/recommend/daily").json()
    assert body == {"ok": False, "error": "not_logged_in", "data": []}


def test_recommend_daily_cli_timeout_is_504(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: True)
    monkeypatch.setattr(musicbox_app, "ne_daily_songs", lambda limit=20: [])

    def timeout(args, timeout=40.0):
        raise runner.MusicboxTimeoutError("too slow")

    monkeypatch.setattr(runner, "run_musicbox", timeout)

    with TestClient(app) as client:
        resp = client.get("/api/v1/recommend/daily")
    assert resp.status_code == 504
    assert resp.json()["error"] == "timeout"


def test_recommend_daily_cli_other_exit_is_502(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: True)
    monkeypatch.setattr(musicbox_app, "ne_daily_songs", lambda limit=20: [])
    monkeypatch.setattr(runner, "run_musicbox", lambda args, timeout=40.0: (9, "", "boom"))

    with TestClient(app) as client:
        resp = client.get("/api/v1/recommend/daily")
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"] == "upstream_error" and body["exit_code"] == 9


def test_recommend_daily_cli_bad_json_is_502(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: True)
    monkeypatch.setattr(musicbox_app, "ne_daily_songs", lambda limit=20: [])
    monkeypatch.setattr(runner, "run_musicbox", lambda args, timeout=40.0: (0, "{not json", ""))

    with TestClient(app) as client:
        resp = client.get("/api/v1/recommend/daily")
    assert resp.status_code == 502
    assert resp.json()["error"] == "bad_upstream_json"


def test_recommend_daily_cli_fallback_empty_has_note(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: True)
    monkeypatch.setattr(musicbox_app, "ne_daily_songs", lambda limit=20: [])
    payload = {"ok": True, "data": [{"song_id": 1}]}
    monkeypatch.setattr(runner, "run_musicbox",
                        lambda args, timeout=40.0: (0, json.dumps(payload), ""))
    monkeypatch.setattr(musicbox_app, "filter_playable_song_ids", lambda ids: set())

    with TestClient(app) as client:
        body = client.get("/api/v1/recommend/daily").json()
    assert body["ok"] is True
    assert body["data"] == []
    assert body["engine"] == "cli-fallback"
    assert "note" in body


# ===========================================================================
# 5) /api/v1/playlists/*
# ===========================================================================

def test_playlists_user_not_logged_in(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: False)
    with TestClient(app) as client:
        body = client.get("/api/v1/playlists/user").json()
    assert body == {"ok": False, "error": "not_logged_in", "data": []}


def test_playlists_user_resolves_uid_from_auth_detail(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: True)
    monkeypatch.setattr(musicbox_app, "ne_auth_detail", lambda: {"user_id": "10086"})
    seen = {}

    def fake_user_playlists(uid, offset=0, limit=50):
        seen.update(uid=uid, offset=offset, limit=limit)
        return [{"playlist_id": 7, "name": "我喜欢的音乐"}]

    monkeypatch.setattr(musicbox_app, "ne_user_playlists", fake_user_playlists)

    with TestClient(app) as client:
        body = client.get("/api/v1/playlists/user", params={"limit": 200}).json()
    assert body["ok"] is True
    assert body["uid"] == 10086
    assert body["data"] == [{"playlist_id": 7, "name": "我喜欢的音乐"}]
    assert seen == {"uid": 10086, "offset": 0, "limit": 200}


def test_playlists_user_uid_unavailable(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: True)
    monkeypatch.setattr(musicbox_app, "ne_auth_detail", lambda: {"user_id": ""})

    with TestClient(app) as client:
        body = client.get("/api/v1/playlists/user").json()
    assert body["ok"] is False
    assert body["error"] == "uid_unavailable"
    assert "hint" in body


def test_playlists_user_explicit_uid_skips_auth_detail(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: True)

    def must_not_call():
        raise AssertionError("显式传 uid 时不应再探测账号")

    monkeypatch.setattr(musicbox_app, "ne_auth_detail", must_not_call)
    monkeypatch.setattr(musicbox_app, "ne_user_playlists", lambda uid, offset=0, limit=50: [{"playlist_id": uid}])

    with TestClient(app) as client:
        body = client.get("/api/v1/playlists/user", params={"uid": 42}).json()
    assert body["ok"] is True and body["uid"] == 42


def test_playlists_user_upstream_error_is_reported(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: True)
    monkeypatch.setattr(musicbox_app, "ne_auth_detail", lambda: {"user_id": "1"})

    def boom(uid, offset=0, limit=50):
        raise RuntimeError("nope")

    monkeypatch.setattr(musicbox_app, "ne_user_playlists", boom)
    with TestClient(app) as client:
        body = client.get("/api/v1/playlists/user").json()
    assert body["ok"] is False and body["data"] == []
    assert "RuntimeError" in body["error"]


def test_playlists_recommend_gated_and_ok(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: False)
    with TestClient(app) as client:
        assert client.get("/api/v1/playlists/recommend").json()["error"] == "not_logged_in"

    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: True)
    monkeypatch.setattr(musicbox_app, "ne_recommend_playlists", lambda: [{"playlist_id": 5}])
    with TestClient(app) as client:
        body = client.get("/api/v1/playlists/recommend").json()
    assert body["ok"] is True and body["engine"] == "in-process"
    assert body["data"] == [{"playlist_id": 5}]


def test_playlists_toplists_public(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_toplists",
                        lambda: [{"playlist_id": 1, "name": "飙升榜"}])
    with TestClient(app) as client:
        body = client.get("/api/v1/playlists/toplists").json()
    assert body["ok"] is True and body["count"] == 1
    assert body["data"][0]["name"] == "飙升榜"


def test_playlists_category_forwards_args(monkeypatch):
    seen = {}

    def fake(cat, order, limit):
        seen.update(cat=cat, order=order, limit=limit)
        return [{"playlist_id": 2}]

    monkeypatch.setattr(musicbox_app, "ne_category_playlists", fake)
    with TestClient(app) as client:
        body = client.get("/api/v1/playlists/category",
                          params={"cat": "欧美", "order": "new", "limit": 7}).json()
    assert body["ok"] is True and body["cat"] == "欧美" and body["order"] == "new"
    assert seen == {"cat": "欧美", "order": "new", "limit": 7}


def test_playlists_categories(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_playlist_categories",
                        lambda: {"语种": ["华语", "欧美"]})
    with TestClient(app) as client:
        body = client.get("/api/v1/playlists/categories").json()
    assert body["ok"] is True and body["data"] == {"语种": ["华语", "欧美"]}


def test_playlists_newalbums_forwards_limit(monkeypatch):
    seen = {}
    monkeypatch.setattr(musicbox_app, "ne_new_albums",
                        lambda limit: seen.update(limit=limit) or [{"album_id": 1}])
    with TestClient(app) as client:
        body = client.get("/api/v1/playlists/newalbums", params={"limit": 5}).json()
    assert body["ok"] is True and seen == {"limit": 5}
    assert body["engine"] == "in-process"


def test_radio_fm_gated_then_limited(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: False)
    with TestClient(app) as client:
        assert client.get("/api/v1/radio/fm").json()["error"] == "not_logged_in"

    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: True)
    monkeypatch.setattr(musicbox_app, "ne_personal_fm", lambda: [{"song_id": i} for i in range(1, 9)])
    with TestClient(app) as client:
        body = client.get("/api/v1/radio/fm", params={"limit": 3}).json()
    assert body["ok"] is True and len(body["data"]) == 3


# ===========================================================================
# 6) 歌单 / 专辑曲目
# ===========================================================================

def test_playlist_tracks_hydrates(monkeypatch):
    seen = {}

    def fake_ids(playlist_id, limit=300):
        seen["ids"] = (playlist_id, limit)
        return [11, 22]

    def fake_songs(ids, limit=300):
        seen["songs"] = (list(ids), limit)
        return [{"song_id": 11}, {"song_id": 22}]

    monkeypatch.setattr(musicbox_app, "ne_playlist_track_ids", fake_ids)
    monkeypatch.setattr(musicbox_app, "ne_songs_by_ids", fake_songs)

    with TestClient(app) as client:
        body = client.get("/api/v1/playlist/123/tracks", params={"limit": 50}).json()
    assert body["ok"] is True
    assert body["playlist_id"] == 123 and body["total_ids"] == 2 and body["playable"] == 2
    assert seen == {"ids": (123, 50), "songs": ([11, 22], 50)}


def test_playlist_tracks_empty_ids_skips_hydration(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_playlist_track_ids", lambda playlist_id, limit=300: [])

    def must_not_call(ids, limit=300):
        raise AssertionError("没有 id 就不该去取详情")

    monkeypatch.setattr(musicbox_app, "ne_songs_by_ids", must_not_call)
    with TestClient(app) as client:
        body = client.get("/api/v1/playlist/123/tracks").json()
    assert body["ok"] is True and body["data"] == [] and body["total_ids"] == 0


def test_playlist_tracks_upstream_error_reported(monkeypatch):
    def boom(playlist_id, limit=300):
        raise RuntimeError("bad playlist")

    monkeypatch.setattr(musicbox_app, "ne_playlist_track_ids", boom)
    with TestClient(app) as client:
        body = client.get("/api/v1/playlist/123/tracks").json()
    assert body["ok"] is False and "RuntimeError" in body["error"]


def test_playlist_tracks_rejects_non_positive_id():
    with TestClient(app) as client:
        assert client.get("/api/v1/playlist/0/tracks").status_code == 400


def test_album_tracks(monkeypatch):
    seen = {}
    monkeypatch.setattr(musicbox_app, "ne_album_songs",
                        lambda album_id, limit=200: seen.update(album_id=album_id, limit=limit)
                        or [{"song_id": 1}])
    with TestClient(app) as client:
        body = client.get("/api/v1/album/77/tracks", params={"limit": 9}).json()
    assert body["ok"] is True and body["album_id"] == 77
    assert seen == {"album_id": 77, "limit": 9}


def test_album_tracks_error_reported(monkeypatch):
    def boom(album_id, limit=200):
        raise RuntimeError("nope")

    monkeypatch.setattr(musicbox_app, "ne_album_songs", boom)
    with TestClient(app) as client:
        body = client.get("/api/v1/album/77/tracks").json()
    assert body["ok"] is False and "RuntimeError" in body["error"]


# ===========================================================================
# 7) best_url / like
# ===========================================================================

def test_song_best_url_ok(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_best_url_info",
                        lambda sid: {"url": "http://cdn/f.flac", "code": 200,
                                     "best_quality": "lossless", "downgraded": False})
    with TestClient(app) as client:
        body = client.get("/api/v1/song/999/best_url").json()
    assert body["ok"] is True
    assert body["best_quality"] == "lossless"
    assert body["tried"] == list(netease_ext.BEST_QUALITY_CHAIN)
    assert body["data"]["url"] == "http://cdn/f.flac"


def test_song_best_url_no_quality(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_best_url_info", lambda sid: {})
    with TestClient(app) as client:
        body = client.get("/api/v1/song/999/best_url").json()
    assert body["ok"] is False
    assert body["error"] == "no_playable_quality"
    assert body["tried"] == list(netease_ext.BEST_QUALITY_CHAIN)


def test_song_best_url_error_reported(monkeypatch):
    def boom(sid):
        raise RuntimeError("url api down")

    monkeypatch.setattr(musicbox_app, "ne_best_url_info", boom)
    with TestClient(app) as client:
        body = client.get("/api/v1/song/999/best_url").json()
    assert body["ok"] is False and "RuntimeError" in body["error"]


def test_song_like_post_and_get(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: True)
    seen = []

    def fake_like(song_id, like=True):
        seen.append((song_id, like))
        return {"ok": True, "song_id": song_id, "like": like, "requires_login": False}

    monkeypatch.setattr(musicbox_app, "ne_song_like", fake_like)

    with TestClient(app) as client:
        post_body = client.post("/api/v1/song/5/like").json()
        get_body = client.get("/api/v1/song/5/like", params={"like": "false"}).json()
    assert post_body["ok"] is True and post_body["engine"] == "in-process"
    assert get_body["ok"] is True
    assert seen == [(5, True), (5, False)]


def test_song_like_gated_when_logged_out(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: False)
    called = []
    monkeypatch.setattr(musicbox_app, "ne_song_like",
                        lambda song_id, like=True: called.append(song_id) or {"ok": True})

    with TestClient(app) as client:
        body = client.post("/api/v1/song/5/like").json()
    assert body == {"ok": False, "error": "not_logged_in", "data": []}
    assert called == []


def test_song_like_failure_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: True)
    monkeypatch.setattr(musicbox_app, "ne_song_like",
                        lambda song_id, like=True: {"ok": False, "song_id": song_id,
                                                    "like": like, "error": "RuntimeError: 403"})
    with TestClient(app) as client:
        body = client.post("/api/v1/song/5/like").json()
    assert body["ok"] is False
    assert body["data"]["error"] == "RuntimeError: 403"


# ===========================================================================
# 8) /api/v1/channels/selftest
# ===========================================================================

def test_channels_selftest_logged_out_skips_account_probes(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: False)
    monkeypatch.setattr(musicbox_app, "ne_toplists", lambda: [{"name": "飙升榜"}])
    monkeypatch.setattr(musicbox_app, "ne_category_playlists", lambda cat, order, limit: [{"name": "a"}])
    monkeypatch.setattr(musicbox_app, "ne_playlist_categories", lambda: {"语种": ["华语"]})
    monkeypatch.setattr(musicbox_app, "ne_new_albums", lambda limit: [{"name": "album"}])

    with TestClient(app) as client:
        body = client.get("/api/v1/channels/selftest").json()
    report = body["data"]
    assert report["logged_in"] is False
    assert report["toplists"]["ok"] is True and report["toplists"]["count"] == 1
    assert report["toplists"]["sample"] == "飙升榜"
    assert report["categories"]["count"] == 1
    assert report["user_playlists"] == {"skipped": "not_logged_in"}
    assert report["recommend_playlists"] == {"skipped": "not_logged_in"}
    assert report["personal_fm"] == {"skipped": "not_logged_in"}


def test_channels_selftest_logged_in_probes_user_playlists(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: True)
    monkeypatch.setattr(musicbox_app, "ne_toplists", lambda: [])
    monkeypatch.setattr(musicbox_app, "ne_category_playlists", lambda cat, order, limit: [])
    monkeypatch.setattr(musicbox_app, "ne_playlist_categories", lambda: {})
    monkeypatch.setattr(musicbox_app, "ne_new_albums", lambda limit: [])
    monkeypatch.setattr(musicbox_app, "ne_recommend_playlists", lambda: [])
    monkeypatch.setattr(musicbox_app, "ne_personal_fm", lambda: [])
    monkeypatch.setattr(musicbox_app, "ne_auth_detail", lambda: {"user_id": "10086"})
    monkeypatch.setattr(musicbox_app, "ne_user_playlists",
                        lambda uid, offset=0, limit=50: [{"name": "我喜欢的音乐"}])

    with TestClient(app) as client:
        report = client.get("/api/v1/channels/selftest").json()["data"]
    assert report["logged_in"] is True
    assert report["user_playlists"]["ok"] is True
    assert report["user_playlists"]["count"] == 1
    assert report["user_playlists"]["sample"] == "我喜欢的音乐"


def test_channels_selftest_records_probe_failures(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: False)

    def boom():
        raise RuntimeError("chart api down")

    monkeypatch.setattr(musicbox_app, "ne_toplists", boom)
    monkeypatch.setattr(musicbox_app, "ne_category_playlists", lambda cat, order, limit: [])
    monkeypatch.setattr(musicbox_app, "ne_playlist_categories", lambda: {})
    monkeypatch.setattr(musicbox_app, "ne_new_albums", lambda limit: [])

    with TestClient(app) as client:
        report = client.get("/api/v1/channels/selftest").json()["data"]
    assert report["toplists"]["ok"] is False
    assert "RuntimeError: chart api down" == report["toplists"]["error"]


def test_channels_selftest_uid_unavailable(monkeypatch):
    monkeypatch.setattr(musicbox_app, "ne_check_is_logged_in", lambda: True)
    monkeypatch.setattr(musicbox_app, "ne_auth_detail", lambda: {"user_id": ""})
    monkeypatch.setattr(musicbox_app, "ne_toplists", lambda: [])
    monkeypatch.setattr(musicbox_app, "ne_category_playlists", lambda cat, order, limit: [])
    monkeypatch.setattr(musicbox_app, "ne_playlist_categories", lambda: {})
    monkeypatch.setattr(musicbox_app, "ne_new_albums", lambda limit: [])
    monkeypatch.setattr(musicbox_app, "ne_recommend_playlists", lambda: [])
    monkeypatch.setattr(musicbox_app, "ne_personal_fm", lambda: [])

    with TestClient(app) as client:
        report = client.get("/api/v1/channels/selftest").json()["data"]
    assert report["user_playlists"]["ok"] is False
    assert "uid_unavailable" in report["user_playlists"]["error"]


# ===========================================================================
# 9) netease_ext 新函数：纯逻辑
# ===========================================================================

@pytest.mark.parametrize("value,expected", [
    (0, False),
    (-5, False),
    ("abc", False),
    (None, False),
    (1000, False),  # 1970 年，明显不是"未来"
])
def test_looks_like_future_ms_rejects(value, expected):
    assert netease_ext._looks_like_future_ms(value) is expected


def test_looks_like_future_ms_accepts_near_future():
    soon = int((time.time() + 86400) * 1000)
    assert netease_ext._looks_like_future_ms(soon) is True


def test_looks_like_future_ms_rejects_beyond_50_years():
    far = int((time.time() + 60 * 365 * 86400) * 1000)
    assert netease_ext._looks_like_future_ms(far) is False


def test_find_vip_expiry_picks_first_plausible_source():
    soon = int((time.time() + 30 * 86400) * 1000)
    assert netease_ext._find_vip_expiry({"vipType": 11}, {"expireTime": soon}) == soon


def test_find_vip_expiry_ignores_garbage():
    assert netease_ext._find_vip_expiry({"expireTime": "not-a-time"}, None, {}) == 0


def test_pick_skips_empty_values():
    assert netease_ext._pick({"a": "", "b": None, "c": 3}, "a", "b", "c") == 3
    assert netease_ext._pick({}, "x") is None


@pytest.mark.parametrize("value,expected", [
    (True, True),
    ("true", True),
    ("True", True),
    (False, False),
    ("false", False),
    (1, False),
    (None, False),
])
def test_flag_of(value, expected):
    assert netease_ext._flag_of({"k": value}, "k") is expected
    assert netease_ext._flag_of(None, "k") is False


def test_is_trial_snippet_ignores_benign_privilege_struct():
    """回归护栏：freeTrialPrivilege 每条响应都带，非空 dict 绝不能被当成试听。"""
    item = {"freeTrialPrivilege": {"resConsumable": False, "userConsumable": False,
                                   "cannotListenReason": None}}
    assert netease_ext.is_trial_snippet(item) is False


def test_is_trial_snippet_detects_inner_boolean_flags():
    assert netease_ext.is_trial_snippet({"freeTrialPrivilege": {"resConsumable": True}}) is True
    assert netease_ext.is_trial_snippet({"freeTrialPrivilege": {"userConsumable": "true"}}) is True


def test_is_trial_snippet_detects_free_trial_info():
    assert netease_ext.is_trial_snippet({"freeTrialInfo": {"start": 0, "end": 30000}}) is True
    assert netease_ext.is_trial_snippet({"freeTrialInfo": {}}) is False
    assert netease_ext.is_trial_snippet({}) is False


@pytest.mark.parametrize("url_info,expected", [
    ({"level": "lossless", "type": "flac"}, "LOSSLESS FLAC"),
    ({"level": "hires", "type": "flac"}, "HIRES FLAC"),
    ({"level": "jymaster", "type": "flac"}, "JYMASTER FLAC"),
    ({"type": "flac"}, "LOSSLESS FLAC"),
    ({"br": 999000}, "LOSSLESS"),
    ({"br": 320000}, "HD 320k"),
    ({"br": 192000}, "MD 192k"),
    ({"br": 128000}, "LD 128k"),
    ({"br": 0}, "LD 128k"),
    ({}, "LD 128k"),
])
def test_quality_of(url_info, expected):
    assert netease_ext.quality_of(url_info) == expected


def test_safe_sid_variants():
    assert netease_ext._safe_sid({"id": 5}) == 5
    assert netease_ext._safe_sid({"song_id": "6"}) == 6
    assert netease_ext._safe_sid({"id": None}) == 0
    assert netease_ext._safe_sid({"id": "x"}) == 0
    assert netease_ext._safe_sid(None) == 0
    assert netease_ext._safe_sid([1]) == 0


def test_to_int_variants():
    assert netease_ext._to_int("7") == 7
    assert netease_ext._to_int(None) == 0
    assert netease_ext._to_int("abc") == 0


def test_song_info_from_raw_shape():
    info = netease_ext._song_info_from_raw(_raw_song(3), _url_item(3, url="http://cdn/3.flac",
                                                                  level="lossless", typ="flac"))
    assert info["song_id"] == 3
    assert info["song_name"] == "歌曲 3"
    assert info["artist"] == "歌手"
    assert info["album_name"] == "专辑"
    assert info["album_id"] == 903
    assert info["duration"] == 245
    assert info["has_sq"] is True and info["has_hr"] is False
    assert info["quality"] == "LOSSLESS FLAC"
    assert info["mp3_url"] == "http://cdn/3.flac"


def test_https_cover_upgrades_http_only():
    assert netease_ext._https_cover("http://p1.music.126.net/a.jpg").startswith("https://")
    assert netease_ext._https_cover("https://x/y.jpg") == "https://x/y.jpg"
    assert netease_ext._https_cover(None) == ""


def test_norm_playlist_full_shape():
    row = netease_ext._norm_playlist({
        "id": 101, "name": "夜跑歌单", "coverImgUrl": "http://img/cover.jpg",
        "trackCount": "18", "description": "x" * 400, "subscribed": True,
        "creator": {"nickname": "DJ", "userId": 88},
    })
    assert row["playlist_id"] == 101
    assert row["name"] == "夜跑歌单"
    assert row["cover_url"].startswith("https://")
    assert row["track_count"] == 18
    assert len(row["description"]) == 300
    assert row["subscribed"] is True
    assert row["creator"] == "DJ" and row["creator_id"] == 88


def test_norm_playlist_tolerates_missing_fields():
    row = netease_ext._norm_playlist({"id": "9"})
    assert row["playlist_id"] == 9
    assert row["name"] == "歌单 9"
    assert row["cover_url"] == ""
    assert row["creator"] == ""


def test_norm_playlist_uses_creator_background_fallback():
    row = netease_ext._norm_playlist({"id": 1, "creator": {"backgroundUrl": "http://b/g.jpg"}})
    assert row["cover_url"] == "https://b/g.jpg"


def test_norm_playlist_rejects_bad_input():
    assert netease_ext._norm_playlist(None) is None
    assert netease_ext._norm_playlist({"name": "no id"}) is None
    assert netease_ext._norm_playlist({"id": "abc"}) is None


def test_playlist_list_filters_invalid():
    assert netease_ext._playlist_list([{"id": 1}, None, {"no": "id"}, "x"]) == [
        netease_ext._norm_playlist({"id": 1})]
    assert netease_ext._playlist_list("not a list") == []


# ===========================================================================
# 10) netease_ext 新函数：上游交互（全部 fake client）
# ===========================================================================

def test_playable_url_map_logged_in_filters_bad_rows(monkeypatch):
    items = [
        _url_item(1),
        _url_item(2, url=None),
        _url_item(3, code=404),
        _url_item(4, freeTrialPrivilege={"resConsumable": True}),
    ]
    _patch_api(monkeypatch, FakeApi(songs_url=lambda ids: items))
    monkeypatch.setattr(netease_ext, "check_is_logged_in", lambda: True)

    out = netease_ext.playable_url_map([1, 2, 3, 4])
    assert sorted(out) == [1]


def test_playable_url_map_empty_ids_does_not_call_api(monkeypatch):
    def must_not_call(ids):
        raise AssertionError("空 id 列表不应请求上游")

    _patch_api(monkeypatch, FakeApi(songs_url=must_not_call))
    assert netease_ext.playable_url_map([]) == {}


def test_playable_url_map_swallows_upstream_error(monkeypatch):
    def boom(ids):
        raise RuntimeError("network")

    _patch_api(monkeypatch, FakeApi(songs_url=boom))
    assert netease_ext.playable_url_map([1]) == {}


def test_playable_url_map_logged_out_drops_paid_tracks(monkeypatch):
    items = [_url_item(1, fee=0), _url_item(2, fee=1), _url_item(3, fee=8)]
    _patch_api(monkeypatch, FakeApi(songs_url=lambda ids: items))
    monkeypatch.setattr(netease_ext, "check_is_logged_in", lambda: False)
    monkeypatch.setattr(netease_ext, "FREE_ONLY_ON_LOGOUT", True)

    assert sorted(netease_ext.playable_url_map([1, 2, 3])) == [1, 3]


def test_playable_url_map_free_only_switch_off(monkeypatch):
    items = [_url_item(1, fee=0), _url_item(2, fee=1)]
    _patch_api(monkeypatch, FakeApi(songs_url=lambda ids: items))
    monkeypatch.setattr(netease_ext, "check_is_logged_in", lambda: False)
    monkeypatch.setattr(netease_ext, "FREE_ONLY_ON_LOGOUT", False)

    assert sorted(netease_ext.playable_url_map([1, 2])) == [1, 2]


def test_level_to_encode_type_wraps_upstream(monkeypatch):
    """薄封装的意义：未装真实 NEMbox 的环境也能测这条分支。"""
    fake_api_mod = type("FakeApiMod", (), {"level_to_encode_type": staticmethod(lambda level: "enc-" + level)})()
    monkeypatch.setitem(sys.modules, "NEMbox", type("M", (), {})())
    monkeypatch.setitem(sys.modules, "NEMbox.api", fake_api_mod)
    assert netease_ext._level_to_encode_type("lossless") == "enc-lossless"


def test_quality_to_level_wraps_upstream(monkeypatch):
    fake_api_mod = type("FakeApiMod", (), {"music_quality_to_level": staticmethod(lambda q: "lvl-" + q)})()
    monkeypatch.setitem(sys.modules, "NEMbox", type("M", (), {})())
    monkeypatch.setitem(sys.modules, "NEMbox.api", fake_api_mod)
    assert netease_ext._quality_to_level("exhigh") == "lvl-exhigh"


def test_urls_for_level_uses_eapi_with_explicit_level(monkeypatch):
    seen = {}

    def eapi_request(path, params):
        seen["path"] = path
        seen["params"] = params
        return {"data": [_url_item(1, level="lossless")]}

    api = FakeApi(eapi_request=eapi_request)
    monkeypatch.setattr(netease_ext, "_level_to_encode_type", lambda level: "flac")

    out = netease_ext._urls_for_level(api, [1], "lossless")
    assert out and out[0]["id"] == 1
    assert seen["path"] == "/api/song/enhance/player/url/v1"
    assert seen["params"]["level"] == "lossless"
    assert seen["params"]["encodeType"] == "flac"
    assert seen["params"]["ids"] == "[1]"


def test_urls_for_level_falls_back_to_weapi(monkeypatch):
    seen = {}

    def eapi_request(path, params):
        raise RuntimeError("eapi unavailable")

    def request(method, path, params, **kw):
        seen["method"] = method
        seen["path"] = path
        seen["params"] = params
        return {"data": [_url_item(2)]}

    api = FakeApi(eapi_request=eapi_request, request=request)
    monkeypatch.setattr(netease_ext, "_level_to_encode_type", lambda level: "mp3")

    out = netease_ext._urls_for_level(api, [2], "exhigh")
    assert out and out[0]["id"] == 2
    assert seen["method"] == "POST"
    assert seen["path"] == "/weapi/song/enhance/player/url"
    assert seen["params"] == {"ids": [2], "br": 320000}


def test_pick_by_id_variants():
    assert netease_ext._pick_by_id([{"id": 1}, {"id": 2}], 2) == {"id": 2}
    assert netease_ext._pick_by_id([{"id": 9}], 5) == {"id": 9}      # 唯一一条时退化
    assert netease_ext._pick_by_id([{"id": 1}, {"id": 2}], 5) is None
    assert netease_ext._pick_by_id({"id": 3}, 3) == {"id": 3}
    assert netease_ext._pick_by_id(None, 3) is None


def test_song_url_info_uses_quality_level(monkeypatch):
    seen = {}
    monkeypatch.setattr(netease_ext, "_quality_to_level", lambda q: seen.setdefault("q", q) or "lossless")

    def eapi_request(path, params):
        seen["level"] = params["level"]
        return {"data": [_url_item(42, url="http://cdn/42.flac")]}

    _patch_api(monkeypatch, FakeApi(eapi_request=eapi_request))
    monkeypatch.setattr(netease_ext, "_level_to_encode_type", lambda level: "flac")

    info = netease_ext.song_url_info(42, quality="lossless")
    assert info["id"] == 42 and info["url"] == "http://cdn/42.flac"
    assert seen == {"q": "lossless", "level": "lossless"}


def test_song_raw_detail_picks_matching_id(monkeypatch):
    _patch_api(monkeypatch, FakeApi(songs_detail=lambda ids: [_raw_song(1), _raw_song(2)]))
    raw = netease_ext.song_raw_detail(2)
    assert raw["id"] == 2 and raw["name"] == "歌曲 2"


def test_song_raw_detail_missing_returns_empty(monkeypatch):
    _patch_api(monkeypatch, FakeApi(songs_detail=lambda ids: []))
    assert netease_ext.song_raw_detail(9) == {}


def test_search_songs_filters_and_caps(monkeypatch):
    raw = [_raw_song(i) for i in range(1, 6)]
    _patch_api(monkeypatch, FakeApi(search=lambda kw, limit=50: {"songs": raw}))
    monkeypatch.setattr(netease_ext, "playable_url_map",
                        lambda ids: {i: _url_item(i) for i in ids if i != 2})

    out = netease_ext.search_songs("  周杰伦 ", limit=2)
    assert [r["song_id"] for r in out] == [1, 3]


def test_search_songs_blank_keyword_and_bad_payload(monkeypatch):
    def must_not_call(kw, limit=50):
        raise AssertionError("空关键词不应请求上游")

    _patch_api(monkeypatch, FakeApi(search=must_not_call))
    assert netease_ext.search_songs("   ") == []

    _patch_api(monkeypatch, FakeApi(search=lambda kw, limit=50: "nope"))
    assert netease_ext.search_songs("x") == []


def test_search_songs_upstream_exception(monkeypatch):
    def boom(kw, limit=50):
        raise RuntimeError("search down")

    _patch_api(monkeypatch, FakeApi(search=boom))
    assert netease_ext.search_songs("x") == []


def test_daily_songs_filters_each_track(monkeypatch):
    raw = [_raw_song(i) for i in (1, 2, 3)]
    _patch_api(monkeypatch, FakeApi(recommend_playlist=lambda limit=20: raw))
    monkeypatch.setattr(netease_ext, "playable_url_map",
                        lambda ids: {i: _url_item(i) for i in ids if i != 3})

    out = netease_ext.daily_songs(limit=10)
    assert [r["song_id"] for r in out] == [1, 2]


def test_daily_songs_empty_upstream(monkeypatch):
    _patch_api(monkeypatch, FakeApi(recommend_playlist=lambda limit=20: []))
    assert netease_ext.daily_songs() == []


def test_daily_songs_upstream_exception(monkeypatch):
    def boom(limit=20):
        raise RuntimeError("rec down")

    _patch_api(monkeypatch, FakeApi(recommend_playlist=boom))
    assert netease_ext.daily_songs() == []


def test_songs_from_raw_list_dedups_and_respects_cap(monkeypatch):
    raw = [_raw_song(1), _raw_song(1), _raw_song(2), _raw_song(3)]
    monkeypatch.setattr(netease_ext, "playable_url_map",
                        lambda ids: {i: _url_item(i) for i in ids})

    out = netease_ext._songs_from_raw_list(raw, limit=2)
    assert [r["song_id"] for r in out] == [1, 2]


def test_songs_from_raw_list_ignores_bad_entries(monkeypatch):
    monkeypatch.setattr(netease_ext, "playable_url_map", lambda ids: {})
    assert netease_ext._songs_from_raw_list([None, "x", {"no": "id"}]) == []


def test_songs_by_ids_dedups_input_and_filters(monkeypatch):
    seen = {}

    def songs_detail(ids):
        seen["ids"] = list(ids)
        return [_raw_song(1), _raw_song(2)]

    _patch_api(monkeypatch, FakeApi(songs_detail=songs_detail))
    monkeypatch.setattr(netease_ext, "playable_url_map",
                        lambda ids: {1: _url_item(1)})

    out = netease_ext.songs_by_ids([1, 2, 0, "x"])
    assert seen["ids"] == [1, 2]
    assert [r["song_id"] for r in out] == [1]


def test_songs_by_ids_empty_input_short_circuits(monkeypatch):
    def must_not_call(ids):
        raise AssertionError("空输入不应请求上游")

    _patch_api(monkeypatch, FakeApi(songs_detail=must_not_call))
    assert netease_ext.songs_by_ids([]) == []


def test_user_playlists_for_uid_normalizes(monkeypatch):
    seen = {}

    def user_playlist(uid, offset=0, limit=50):
        seen.update(uid=uid, offset=offset, limit=limit)
        return [{"id": 1, "name": "自建"}, {"no": "id"}]

    _patch_api(monkeypatch, FakeApi(user_playlist=user_playlist))
    out = netease_ext.user_playlists_for_uid(10086, offset=0, limit=30)
    assert seen == {"uid": 10086, "offset": 0, "limit": 30}
    assert out[0]["playlist_id"] == 1


def test_recommend_playlists_normalizes(monkeypatch):
    _patch_api(monkeypatch, FakeApi(recommend_resource=lambda: [{"id": 8, "name": "推荐"}]))
    assert netease_ext.recommend_playlists()[0]["name"] == "推荐"


def test_toplists_pairs_and_skips_garbage(monkeypatch):
    _patch_api(monkeypatch, FakeApi(fetch_toplists=lambda: [("飙升榜", 1), ("坏数据",), None, ("新歌榜", "2")]))
    out = netease_ext.toplists()
    assert [(r["name"], r["playlist_id"]) for r in out] == [("飙升榜", 1), ("新歌榜", 2)]
    assert out[0]["cover_url"] == ""


def test_toplists_non_list_upstream(monkeypatch):
    _patch_api(monkeypatch, FakeApi(fetch_toplists=lambda: "nope"))
    assert netease_ext.toplists() == []


def test_category_playlists_normalizes_order_and_limit(monkeypatch):
    seen = {}

    def top_playlists(cat, order, offset, limit):
        seen.update(cat=cat, order=order, offset=offset, limit=limit)
        return [{"id": 3, "name": "分类"}]

    _patch_api(monkeypatch, FakeApi(top_playlists=top_playlists))
    out = netease_ext.category_playlists("欧美", "NEW", 999)
    assert seen == {"cat": "欧美", "order": "new", "offset": 0, "limit": 50}
    assert out[0]["playlist_id"] == 3


def test_category_playlists_invalid_order_defaults_hot(monkeypatch):
    seen = {}
    _patch_api(monkeypatch, FakeApi(
        top_playlists=lambda cat, order, offset, limit: seen.update(order=order) or []))
    netease_ext.category_playlists("华语", "weird", 5)
    assert seen["order"] == "hot"


def test_playlist_categories_parses_and_falls_back(monkeypatch):
    api = FakeApi(playlist_catelogs=lambda: {"语种": [{"name": "华语"}]},
                  _parse_playlist_classes=lambda raw: {"语种": ["华语", "欧美"]},
                  _get_playlist_classes=lambda: {"fallback": ["x"]})
    _patch_api(monkeypatch, api)
    assert netease_ext.playlist_categories() == {"语种": ["华语", "欧美"]}

    api2 = FakeApi(playlist_catelogs=lambda: "nope",
                   _parse_playlist_classes=lambda raw: {},
                   _get_playlist_classes=lambda: {"兜底": ["y"]})
    _patch_api(monkeypatch, api2)
    assert netease_ext.playlist_categories() == {"兜底": ["y"]}


def test_new_albums_normalizes(monkeypatch):
    seen = {}
    raw = [
        {"id": 5, "name": "新碟", "picUrl": "http://img/a.jpg", "artist": {"name": "A"},
         "publishTime": 1700000000000},
        {"name": "缺 id"},
        "垃圾",
    ]

    def new_albums(offset=0, limit=20):
        seen.update(offset=offset, limit=limit)
        return raw

    _patch_api(monkeypatch, FakeApi(new_albums=new_albums))
    out = netease_ext.new_albums(limit=99)
    assert seen == {"offset": 0, "limit": 50}
    assert out == [{"album_id": 5, "name": "新碟", "cover_url": "https://img/a.jpg",
                    "artist": "A", "publish_time": 1700000000000}]


def test_personal_fm_maps_songs(monkeypatch):
    _patch_api(monkeypatch, FakeApi(personal_fm=lambda: [_raw_song(1)]))
    monkeypatch.setattr(netease_ext, "playable_url_map",
                        lambda ids: {1: _url_item(1)})
    out = netease_ext.personal_fm()
    assert [r["song_id"] for r in out] == [1]


def test_playlist_track_ids_limited_accepts_dict_and_int_rows(monkeypatch):
    _patch_api(monkeypatch, FakeApi(playlist_songlist=lambda pid: [
        {"id": 1, "v": 0, "at": 1}, 2, None, {"id": "x"}, {"id": 3}]))

    assert netease_ext.playlist_track_ids_limited(9, limit=10) == [1, 2, 3]


def test_playlist_track_ids_limited_honours_limit(monkeypatch):
    _patch_api(monkeypatch, FakeApi(playlist_songlist=lambda pid: list(range(1, 11))))
    assert netease_ext.playlist_track_ids_limited(9, limit=3) == [1, 2, 3]


def test_playlist_track_ids_limited_is_not_login_gated(monkeypatch):
    """公共歌单（排行榜/分类）未登录也要能取：绝不能先判登录。"""
    def must_not_call():
        raise AssertionError("兼容版不应先判登录")

    monkeypatch.setattr(netease_ext, "check_is_logged_in", must_not_call)
    _patch_api(monkeypatch, FakeApi(playlist_songlist=lambda pid: [{"id": 1}]))
    assert netease_ext.playlist_track_ids_limited(9) == [1]


def test_album_songs_maps_tracks(monkeypatch):
    seen = {}
    _patch_api(monkeypatch, FakeApi(album=lambda aid: seen.update(aid=aid) or [_raw_song(1)]))
    monkeypatch.setattr(netease_ext, "playable_url_map", lambda ids: {1: _url_item(1)})

    out = netease_ext.album_songs(77)
    assert seen == {"aid": 77}
    assert [r["song_id"] for r in out] == [1]


def test_best_url_info_uses_actual_granted_level(monkeypatch):
    """上游会按账号权益自动降级：best_quality 必须取响应的实际 level，并留痕。"""
    seen = []
    monkeypatch.setattr(netease_ext, "_level_to_encode_type", lambda level: "flac")

    def eapi_request(path, params):
        seen.append(params["level"])
        return {"data": [_url_item(1, url="http://cdn/1.flac", level="exhigh", br=320000)]}

    _patch_api(monkeypatch, FakeApi(eapi_request=eapi_request))
    info = netease_ext.best_url_info(1)
    assert seen == ["jymaster"]
    assert info["best_quality"] == "exhigh"
    assert info["requested_level"] == "jymaster"
    assert info["downgraded"] is True


def test_best_url_info_returns_top_quality_when_granted(monkeypatch):
    monkeypatch.setattr(netease_ext, "_level_to_encode_type", lambda level: "flac")

    def eapi_request(path, params):
        if params["level"] == "jymaster":
            return {"data": [_url_item(1, url="http://cdn/1.flac", level="jymaster", br=1999000)]}
        return {"data": []}

    _patch_api(monkeypatch, FakeApi(eapi_request=eapi_request))
    info = netease_ext.best_url_info(1)
    assert info["best_quality"] == "jymaster"
    assert info["downgraded"] is False


def test_best_url_info_walks_whole_chain_then_gives_up(monkeypatch):
    seen = []
    monkeypatch.setattr(netease_ext, "_level_to_encode_type", lambda level: "flac")

    def eapi_request(path, params):
        seen.append(params["level"])
        return {"data": [_url_item(1, url=None, code=404)]}

    def request(method, path, params, **kw):
        return {"data": []}

    _patch_api(monkeypatch, FakeApi(eapi_request=eapi_request, request=request))
    assert netease_ext.best_url_info(1) == {}
    assert seen == list(netease_ext.BEST_QUALITY_CHAIN)


def test_best_url_info_skips_trial_snippets(monkeypatch):
    monkeypatch.setattr(netease_ext, "_level_to_encode_type", lambda level: "flac")

    def eapi_request(path, params):
        if params["level"] in ("jymaster", "hires"):
            return {"data": [_url_item(1, freeTrialPrivilege={"resConsumable": True})]}
        return {"data": [_url_item(1, url="http://cdn/1.mp3", level="exhigh")]}

    _patch_api(monkeypatch, FakeApi(eapi_request=eapi_request))
    info = netease_ext.best_url_info(1)
    # jymaster / hires 都是试听片段 → 被跳过，最终在 lossless 档拿到 exhigh 实链
    assert info["best_quality"] == "exhigh"
    assert info["requested_level"] == "lossless"
    assert info["downgraded"] is True


def test_song_like_reports_upstream_result(monkeypatch):
    seen = {}

    def song_like(song_id, like=True):
        seen.update(song_id=song_id, like=like)
        return True

    _patch_api(monkeypatch, FakeApi(song_like=song_like))
    assert netease_ext.song_like(7, like=True)["ok"] is True
    assert seen == {"song_id": 7, "like": True}

    _patch_api(monkeypatch, FakeApi(song_like=lambda song_id, like=True: False))
    res = netease_ext.song_like(7, like=False)
    assert res["ok"] is False and res["requires_login"] is True


def test_song_like_returns_error_instead_of_raising(monkeypatch):
    def boom(song_id, like=True):
        raise RuntimeError("403 forbidden")

    _patch_api(monkeypatch, FakeApi(song_like=boom))
    res = netease_ext.song_like(7)
    assert res["ok"] is False
    assert res["error"] == "RuntimeError: 403 forbidden"


def test_auth_detail_reads_cached_account_info(monkeypatch):
    soon = int((time.time() + 20 * 86400) * 1000)
    info = {"account": {"id": 10086, "vipType": 11},
            "profile": {"userId": 10086, "nickname": "听歌的人", "vipExpiryTime": soon}}
    _patch_api(monkeypatch, FakeApi(get_account_info=lambda: info))

    detail = netease_ext.auth_detail()
    assert detail["logged_in"] is True
    assert detail["nickname"] == "听歌的人"
    assert detail["user_id"] == "10086"
    assert detail["vip_type"] == 11
    assert detail["vip_expires_ms"] == soon
    assert detail["vip_expires_known"] is True


def test_auth_detail_logged_out_shape(monkeypatch):
    _patch_api(monkeypatch, FakeApi(get_account_info=lambda: {}))
    detail = netease_ext.auth_detail()
    assert detail["logged_in"] is False
    assert detail["nickname"] == ""
    assert detail["vip_type"] == 0
    assert detail["vip_expires_known"] is False


def test_auth_detail_swallows_upstream_error(monkeypatch):
    def boom():
        raise RuntimeError("account api down")

    _patch_api(monkeypatch, FakeApi(get_account_info=boom))
    detail = netease_ext.auth_detail()
    assert detail["logged_in"] is False
    assert detail["error"] == "account api down"


def test_auth_detail_probes_user_detail_for_vip_expiry(monkeypatch):
    soon = int((time.time() + 5 * 86400) * 1000)
    info = {"profile": {"userId": 10086, "nickname": "n", "vipType": 11}}
    seen = {}

    def request(method, path, **kw):
        seen.update(method=method, path=path)
        return {"profile": {"vipExpiryTime": soon}}

    _patch_api(monkeypatch, FakeApi(get_account_info=lambda: info, request=request))
    detail = netease_ext.auth_detail()
    assert detail["vip_expires_ms"] == soon
    assert detail["vip_expires_known"] is True
    assert seen["method"] == "POST"
    assert seen["path"] == "/weapi/v1/user/detail/10086"


def test_invalidate_login_cache_clears_state(monkeypatch):
    monkeypatch.setattr(netease_ext, "_login_state", (True, time.monotonic()))
    netease_ext.invalidate_login_cache()
    assert netease_ext._login_state is None


def test_reset_api_instance_delegates_to_reset_api(monkeypatch):
    reasons = []
    monkeypatch.setattr(netease_ext, "reset_api", lambda reason="": reasons.append(reason))
    monkeypatch.setattr(netease_ext, "_login_state", (True, time.monotonic()))

    netease_ext.reset_api_instance()
    assert reasons == ["api instance reset"]
    assert netease_ext._login_state is None


def test_build_api_constructs_upstream_instance(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "ensure_xdg_dirs", lambda: calls.append("xdg"))
    fake_api_mod = type("FakeApiMod", (), {"NetEase": staticmethod(lambda: "api-instance")})()
    monkeypatch.setitem(sys.modules, "NEMbox", type("M", (), {})())
    monkeypatch.setitem(sys.modules, "NEMbox.api", fake_api_mod)

    assert netease_ext._build_api() == "api-instance"
    assert calls == ["xdg"]


def test_cookie_stamp_uses_file_stat(tmp_path, monkeypatch):
    cookie = tmp_path / "cookie"
    cookie.write_text("MUSIC_U=1")
    api = FakeApi(storage=FakeApi(cookie_path=str(cookie)))
    stamp = netease_ext._cookie_stamp(api)
    assert stamp == (os.stat(cookie).st_mtime_ns, os.stat(cookie).st_size)


def test_cookie_stamp_without_path():
    assert netease_ext._cookie_stamp(FakeApi()) == ()
    assert netease_ext._cookie_stamp(FakeApi(storage=FakeApi(cookie_path="/nope/cookie"))) == ()


# ===========================================================================
# 11) runner 的 CLI 解析能力
# ===========================================================================

def test_bin_dir_matches_interpreter_dir():
    import os as _os
    assert runner.bin_dir() == _os.path.dirname(_os.path.abspath(sys.executable))


def test_candidate_paths_include_venv_bin(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "bin_dir", lambda: str(tmp_path))
    paths = runner.candidate_paths()
    assert str(tmp_path / "musicbox") in paths
    assert str(tmp_path / "musicbox.exe") in paths


def test_resolve_musicbox_cmd_prefers_venv_binary(monkeypatch, tmp_path):
    exe = tmp_path / "musicbox"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(runner, "bin_dir", lambda: str(tmp_path))

    cmd, how = runner.resolve_musicbox_cmd()
    assert cmd == [str(exe)]
    assert how == f"absolute:{exe}"


def test_resolve_musicbox_cmd_falls_back_to_which(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "bin_dir", lambda: str(tmp_path))
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/local/bin/musicbox")

    cmd, how = runner.resolve_musicbox_cmd()
    assert cmd == ["/usr/local/bin/musicbox"]
    assert how == "which:/usr/local/bin/musicbox"


def test_resolve_musicbox_cmd_module_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "bin_dir", lambda: str(tmp_path))
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    monkeypatch.setattr(runner, "module_fallback", lambda: "fake_nembox")
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess([], 0, "", ""))

    cmd, how = runner.resolve_musicbox_cmd()
    assert cmd == [sys.executable, "-m", "NEMbox"]
    assert how == "module:fake_nembox"


def test_resolve_musicbox_cmd_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "bin_dir", lambda: str(tmp_path))
    monkeypatch.setattr(runner, "module_fallback", lambda: "definitely_missing_mod_xyz")
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess([], 1, "", ""))

    cmd, how = runner.resolve_musicbox_cmd()
    assert cmd == []
    assert how.startswith("not_found:")


def test_musicbox_cmd_caches_until_reset(monkeypatch):
    calls = []

    def fake_resolve():
        calls.append(1)
        return (["/x/musicbox"], "absolute:/x/musicbox")

    monkeypatch.setattr(runner, "resolve_musicbox_cmd", fake_resolve)
    runner.reset_cmd_cache()
    assert runner.musicbox_cmd() == (["/x/musicbox"], "absolute:/x/musicbox")
    runner.musicbox_cmd()
    assert len(calls) == 1

    runner.reset_cmd_cache()
    runner.musicbox_cmd()
    assert len(calls) == 2


def test_run_musicbox_resolved_prepends_venv_bin_to_path(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(runner, "bin_dir", lambda: str(tmp_path))
    monkeypatch.setattr(runner, "musicbox_cmd", lambda: (["/x/musicbox"], "absolute:/x/musicbox"))
    monkeypatch.setattr(runner, "ensure_xdg_dirs", lambda: None)
    monkeypatch.setattr(runner, "get_clean_env", lambda: {"PATH": "/usr/bin"})

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["env"] = kw["env"]
        return subprocess.CompletedProcess(cmd, 0, "out", "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    code, out, err = runner.run_musicbox_resolved(["search", "x"])
    assert (code, out, err) == (0, "out", "")
    assert seen["cmd"] == ["/x/musicbox", "search", "x"]
    assert seen["env"]["PATH"] == f"{tmp_path}{os.pathsep}/usr/bin"


def test_run_musicbox_resolved_missing_binary_is_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "bin_dir", lambda: str(tmp_path))
    monkeypatch.setattr(runner, "musicbox_cmd", lambda: ([], "not_found:/a,/b"))
    monkeypatch.setattr(runner, "ensure_xdg_dirs", lambda: None)

    code, out, err = runner.run_musicbox_resolved(["--version"])
    assert code == 127 and out == ""
    assert "musicbox CLI not found" in err
    assert str(tmp_path) in err


def test_run_musicbox_resolved_timeout_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "bin_dir", lambda: str(tmp_path))
    monkeypatch.setattr(runner, "musicbox_cmd", lambda: (["/x/musicbox"], "absolute:/x/musicbox"))
    monkeypatch.setattr(runner, "ensure_xdg_dirs", lambda: None)

    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 3.0)

    monkeypatch.setattr(runner.subprocess, "run", timeout)
    with pytest.raises(runner.MusicboxTimeoutError):
        runner.run_musicbox_resolved(["--version"], timeout=3.0)
