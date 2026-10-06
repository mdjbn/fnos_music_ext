"""收藏联动接线测试：归档下载（FNMUSIC_DOWNLOAD_ON_FAVORITE）与网易云红心（FNMUSIC_FAV_SYNC_LIKE）。

这两条链路都是「收藏接口的副作用」：本地收藏写成功之后才顺带做，
所以本文件同时盯着两件事——
1. 开关打开时确实做了（红心写对 like=true/false、归档拿到正确的 song_id 与元数据）；
2. 任何一步失败都不改变 ``/music/api/v1/favorite-track/*`` 的响应形状
   （官方接口协议 ``{"code": 0, "msg": "", "data": None}``，飞牛客户端按它解析）。

``download.py`` 的落盘逻辑（路径安全、标记、去重）由 test_download_archive.py 覆盖，
这里只验证接线：谁被调用、传了什么、失败时接口还活着。
"""
import asyncio
import os

import httpx
import pytest
from fastapi.testclient import TestClient

import proxy.app as appmod
from proxy import download as downloader
from proxy.app import (
    CONF,
    _ENV_WATCH_KEYS,
    _netease_song_id,
    _sync_netease_like,
    app,
    archive_ref_path,
    apply_env_hot_reload,
    media_ref_path,
    remember_archive_path,
    remember_media_path,
)

NETEASE = "online:netease:228908"
NETEASE2 = "online:netease:228909"
KUWO = "online:kuwo:228908"
CREATE = "/music/api/v1/favorite-track/create"
DELETE = "/music/api/v1/favorite-track/delete"
LIKE_PATH = "/api/v1/song/228908/like"
INFO = {"id": "netease:228908", "source": "netease", "title": "晴天",
        "artist": "周杰伦", "album": "叶惠美", "lyric": ""}


def _switch(monkeypatch, rec, *, like=False, dl_dir=None, dl_flag=True):
    """设置三个开关：红心同步 / 归档目录 / 收藏自动下载。dl_dir 省略时用夹具给的目录。"""
    monkeypatch.setenv("FNMUSIC_FAV_SYNC_LIKE", "true" if like else "false")
    monkeypatch.setenv("FNMUSIC_DOWNLOAD_DIR", rec["dl_dir"] if dl_dir is None else dl_dir)
    monkeypatch.setenv("FNMUSIC_DOWNLOAD_ON_FAVORITE", "true" if dl_flag else "false")


@pytest.fixture
def fav_rec(tmp_path, monkeypatch):
    """隔离的收藏环境 + 只记录请求的假上游/假 musicbox，返回记录容器。"""
    monkeypatch.setattr(appmod, "_bind_registry_loaded", True)
    appmod._bind_registry.clear()
    appmod._FAKE_GUID_REVERSE.clear()
    appmod._full_fetch_tasks.clear()
    appmod._full_fetch_failed.clear()
    downloader._TASKS.clear()

    cache_dir = str(tmp_path / "cache")
    os.makedirs(cache_dir, exist_ok=True)
    monkeypatch.setitem(CONF, "cache_dir", cache_dir)
    monkeypatch.setitem(CONF, "fav_dir", str(tmp_path / "online_favorites"))
    monkeypatch.setitem(CONF, "plt_dir", str(tmp_path / "playlist_tracks"))
    # 本文件只测收藏联动：官方绑定链路（真实闭环见 test_fav_autobind.py）保持关闭
    monkeypatch.setitem(CONF, "fav_auto_bind", False)

    rec = {"netease": [], "enqueue": [], "dl_dir": str(tmp_path / "downloads"),
           "like_status": 200, "like_body": {"ok": True, "data": {}, "engine": "in-process"}}
    os.makedirs(rec["dl_dir"], exist_ok=True)
    monkeypatch.setenv("FNMUSIC_DOWNLOAD_DIR", rec["dl_dir"])
    monkeypatch.setenv("FNMUSIC_DOWNLOAD_ON_FAVORITE", "false")
    monkeypatch.setenv("FNMUSIC_FAV_SYNC_LIKE", "false")

    # 元数据直接给，不打源站（_best_effort_online_info 是模块级名字，patch 生效）
    async def fake_info(request, guid):
        return dict(INFO, id=appmod.song_id_from_online_guid(guid),
                    source=appmod.source_from_online_guid(guid))

    monkeypatch.setattr(appmod, "_best_effort_online_info", fake_info)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"guid": "user-a"}})
        return httpx.Response(200, json={"code": 0, "msg": "", "data": None})

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/like"):
            rec["netease"].append((request.url.path, request.url.params.get("like")))
            return httpx.Response(rec["like_status"], json=rec["like_body"])
        rec["netease"].append((request.url.path, None))
        return httpx.Response(404, json={"ok": False})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://musicbox")
    # 其余音源客户端一律 404，防止任何真实联网
    for name, base in (("musicdl_client", "http://musicdl"), ("lx_client", "http://lx")):
        setattr(app.state, name, httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
            base_url=base))

    yield rec

    downloader._TASKS.clear()


def _recording_enqueue(rec, ret="enqueued", exc=None):
    def fake_enqueue(client, song_id, meta, ref_writer=None):
        rec["enqueue"].append({"song_id": song_id, "meta": meta, "ref_writer": ref_writer})
        if exc is not None:
            raise exc
        return ret
    return fake_enqueue


def _post(client, path, guid):
    return client.post(path, json={"trackGUID": guid}, cookies={"music-token": "valid_token"})


# ---------------------------------------------------------------- 收藏 -> 归档 --

def test_create_enqueues_archive_and_remembers_ref(fav_rec, monkeypatch):
    """开关全开：create 既写红心，又把归档任务交给 download.enqueue，ref 落到 .archive.ref。"""
    _switch(monkeypatch, fav_rec, like=True)
    monkeypatch.setattr(appmod.downloader, "enqueue", _recording_enqueue(fav_rec))

    with TestClient(app) as client:
        resp = _post(client, CREATE, NETEASE)

    assert resp.status_code == 200
    assert resp.json() == {"code": 0, "msg": "", "data": None}
    assert fav_rec["netease"] == [(LIKE_PATH, "true")]
    assert len(fav_rec["enqueue"]) == 1
    call = fav_rec["enqueue"][0]
    assert call["song_id"] == "228908"
    assert call["meta"] == {"title": "晴天", "artist": "周杰伦", "album": "叶惠美"}

    # ref_writer 就是 remember_archive_path：写进归档命名空间，且与曲库 .ref 互不干扰
    call["ref_writer"](os.path.join(fav_rec["dl_dir"], "周杰伦", "周杰伦 - 晴天.flac"))
    with open(archive_ref_path(NETEASE), encoding="utf-8") as f:
        assert f.read() == os.path.join(fav_rec["dl_dir"], "周杰伦", "周杰伦 - 晴天")
    assert not os.path.exists(media_ref_path(NETEASE))


def test_create_gates_are_independent(fav_rec, monkeypatch):
    """红心开关关、归档开关开：只归档，不碰网易云账号。"""
    _switch(monkeypatch, fav_rec, like=False)
    monkeypatch.setattr(appmod.downloader, "enqueue", _recording_enqueue(fav_rec))

    with TestClient(app) as client:
        resp = _post(client, CREATE, NETEASE)

    assert resp.json() == {"code": 0, "msg": "", "data": None}
    assert fav_rec["netease"] == []
    assert [c["song_id"] for c in fav_rec["enqueue"]] == ["228908"]


@pytest.mark.parametrize("dl_dir,dl_flag", [("", True), ("/tmp/dl", False)])
def test_create_download_needs_both_dir_and_flag(fav_rec, monkeypatch, dl_dir, dl_flag):
    """目录为空或开关关闭都不归档——「填了目录但没勾」和「勾了但没填」都不该偷偷下载。"""
    _switch(monkeypatch, fav_rec, like=True, dl_dir=dl_dir, dl_flag=dl_flag)
    monkeypatch.setattr(appmod.downloader, "enqueue", _recording_enqueue(fav_rec))

    with TestClient(app) as client:
        resp = _post(client, CREATE, NETEASE)

    assert resp.json() == {"code": 0, "msg": "", "data": None}
    assert fav_rec["enqueue"] == []


def test_create_survives_like_500_and_enqueue_failure(fav_rec, monkeypatch):
    """红心 500 + 归档抛异常，接口仍返回官方形状（本地收藏已经写成功了）。"""
    _switch(monkeypatch, fav_rec, like=True)
    fav_rec["like_status"] = 500
    fav_rec["like_body"] = {"ok": False, "error": "boom"}
    monkeypatch.setattr(appmod.downloader, "enqueue",
                        _recording_enqueue(fav_rec, exc=RuntimeError("下载目录不存在")))

    with TestClient(app) as client:
        resp = _post(client, CREATE, NETEASE)

    assert resp.status_code == 200
    assert resp.json() == {"code": 0, "msg": "", "data": None}
    # 归档虽然失败，但仍然被尝试过（失败只进日志，不静默跳过）
    assert [c["song_id"] for c in fav_rec["enqueue"]] == ["228908"]


def test_create_non_netease_guid_never_touches_account(fav_rec, monkeypatch):
    """酷我来源的在线曲：绝不拿它的数字 id 去写网易云红心，也不去归档。"""
    _switch(monkeypatch, fav_rec, like=True)
    monkeypatch.setattr(appmod.downloader, "enqueue", _recording_enqueue(fav_rec))

    with TestClient(app) as client:
        resp = _post(client, CREATE, KUWO)

    assert resp.json() == {"code": 0, "msg": "", "data": None}
    assert fav_rec["netease"] == []
    assert fav_rec["enqueue"] == []


# ---------------------------------------------------------------- 取消收藏 ------

def test_delete_unlikes_and_keeps_archive_file(fav_rec, monkeypatch):
    """取消收藏撤销红心，但不动归档文件，也不触发新的下载。"""
    _switch(monkeypatch, fav_rec, like=True)
    monkeypatch.setattr(appmod.downloader, "enqueue", _recording_enqueue(fav_rec))
    remember_archive_path(NETEASE, os.path.join(fav_rec["dl_dir"], "周杰伦 - 晴天.flac"))

    with TestClient(app) as client:
        resp = _post(client, DELETE, NETEASE)

    assert resp.status_code == 200
    assert resp.json() == {"code": 0, "msg": "", "data": None}
    assert fav_rec["netease"] == [(LIKE_PATH, "false")]
    assert fav_rec["enqueue"] == []
    assert os.path.exists(archive_ref_path(NETEASE)), "取消收藏不该删归档资产"


def test_delete_survives_upstream_failure(fav_rec, monkeypatch):
    """取消红心失败同样不改响应形状。"""
    _switch(monkeypatch, fav_rec, like=True)
    fav_rec["like_status"] = 503
    fav_rec["like_body"] = {"ok": False}

    with TestClient(app) as client:
        resp = _post(client, DELETE, NETEASE)

    assert resp.json() == {"code": 0, "msg": "", "data": None}
    assert fav_rec["netease"] == [(LIKE_PATH, "false")]


# ------------------------------------------------------------------ 纯单元 ------

@pytest.mark.parametrize("guid,expect", [
    (NETEASE, "228908"),
    (NETEASE2, "228909"),
    ("netease:228908", ""),                # 非 online: 前缀（两个调用点都已先判定在线 guid）
    (KUWO, ""),                            # 非网易云来源一律拒绝
    ("online:lxmusic:228908", ""),
    ("online:netease:abc", ""),            # 非纯数字 id
    ("online:netease:", ""),
    ("official-track-999", ""),            # 非在线 guid
    ("", ""),
    (None, ""),
])
def test_netease_song_id_only_accepts_netease_digits(guid, expect):
    assert _netease_song_id(guid) == expect


def test_like_sync_disabled_by_default(monkeypatch):
    """默认关闭：一次请求都不发，并如实回 skipped=disabled。"""
    monkeypatch.delenv("FNMUSIC_FAV_SYNC_LIKE", raising=False)
    seen = []

    async def main():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={"ok": True})),
            base_url="http://musicbox",
        ) as client:
            return await _sync_netease_like(client, "228908", True)

    assert asyncio.run(main()) == {"ok": False, "skipped": "disabled"}
    assert seen == []


def test_like_sync_error_mapping(monkeypatch):
    """上游异常如实回传，绝不抛给收藏接口。"""
    monkeypatch.setenv("FNMUSIC_FAV_SYNC_LIKE", "true")

    def run(status, body, song_id="228908", like=True):
        async def main():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body)),
                base_url="http://musicbox",
            ) as client:
                return await _sync_netease_like(client, song_id, like)
        return asyncio.run(main())

    assert run(200, {"ok": True, "data": {}}) == {"ok": True, "like": True}
    assert run(200, {"ok": True, "data": {}}, like=False) == {"ok": True, "like": False}
    assert run(500, {"ok": False}) == {"ok": False, "error": "http_500"}
    assert run(200, {"ok": False, "error": "not_logged_in"}) == {"ok": False, "error": "not_logged_in"}
    assert run(200, {"ok": False}) == {"ok": False, "error": "upstream_rejected"}
    assert run(200, [1, 2]) == {"ok": False, "error": "bad_payload"}
    assert run(200, {"ok": True}, song_id="abc") == {"ok": False, "error": "invalid_song_id"}


def test_archive_ref_is_a_separate_namespace(monkeypatch, tmp_path):
    """两套 .ref 必须分开：复用同一个文件会让边播边存和归档互相覆盖（卸载漏删）。"""
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path))
    remember_archive_path(NETEASE, "/downloads/周杰伦 - 晴天.flac")
    remember_media_path(NETEASE, "/cache/abc/周杰伦 - 晴天.mp3")

    with open(archive_ref_path(NETEASE), encoding="utf-8") as f:
        assert f.read() == "/downloads/周杰伦 - 晴天"   # 存词干，扩展名去掉
    with open(media_ref_path(NETEASE), encoding="utf-8") as f:
        assert f.read() == "/cache/abc/周杰伦 - 晴天"
    assert os.path.basename(archive_ref_path(NETEASE)) != os.path.basename(media_ref_path(NETEASE))


def test_remember_archive_path_failure_is_swallowed(monkeypatch, tmp_path):
    """写 ref 失败只记 warning：不能因为缓存目录不可写就弄坏收藏/归档。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    monkeypatch.setitem(CONF, "cache_dir", str(blocker / "sub"))

    remember_archive_path(NETEASE, "/downloads/a.flac")  # 不抛异常

    assert not os.path.exists(str(blocker / "sub"))


def test_download_switches_are_hot_reloadable(tmp_path, monkeypatch):
    """管理页改完开关要免重启生效：三个键必须进 .env 白名单并同步进 os.environ。

    download.py 直接读 os.environ，不在白名单里就会出现"勾了没反应"（要重启服务才行）。
    """
    for key in ("FNMUSIC_DOWNLOAD_DIR", "FNMUSIC_DOWNLOAD_ON_FAVORITE", "FNMUSIC_FAV_SYNC_LIKE"):
        assert key in _ENV_WATCH_KEYS, f"{key} 未纳入 .env 热重载白名单"
        monkeypatch.setenv(key, "")  # 让 monkeypatch 在收尾时还原

    env_file = tmp_path / ".env"
    env_file.write_text(
        "FNMUSIC_DOWNLOAD_DIR=/tmp/fnmusic-downloads\n"
        "FNMUSIC_DOWNLOAD_ON_FAVORITE=true\n"
        "FNMUSIC_FAV_SYNC_LIKE=true\n",
        encoding="utf-8",
    )
    apply_env_hot_reload(str(env_file))

    assert downloader.download_dir() == "/tmp/fnmusic-downloads"
    assert downloader.download_enabled() is True
    assert downloader.like_sync_enabled() is True


def test_enqueue_threads_song_id_meta_and_ref_writer(monkeypatch):
    """enqueue 真链路：收藏接口给的 song_id/meta/ref_writer 原样交到 archive_song。"""
    calls = []

    async def fake_archive(client, song_id, meta, ref_writer=None):
        calls.append((song_id, meta, ref_writer))

    monkeypatch.setattr(downloader, "archive_song", fake_archive)
    downloader._TASKS.pop("228908", None)
    marker = object()

    async def main():
        state = downloader.enqueue(object(), "228908", {"title": "晴天"}, ref_writer=marker)
        await asyncio.sleep(0)  # 让后台任务跑完
        return state

    assert asyncio.run(main()) == "enqueued"
    assert calls and calls[0][0] == "228908"
    assert calls[0][1] == {"title": "晴天"}
    assert calls[0][2] is marker
