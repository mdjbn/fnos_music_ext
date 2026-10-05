"""v2.8 本地曲库优先：索引构建、匹配归一化、音质类约束、stream 集成。"""
from __future__ import annotations

import os
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import local_library as ll
from proxy import netease_auth
from proxy.app import app, CONF
from proxy import app as P


# ---------------------------------------------------------------------------
# 测试库：模拟飞牛 music.db（列名用常见命名，schema 容错扫描应能识别）
# ---------------------------------------------------------------------------


def _make_music_db_at(db: str, rows):
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE track (id INTEGER PRIMARY KEY, title TEXT, "
                "artist TEXT, path TEXT, duration INTEGER)")
    for i, (title, artist, path) in enumerate(rows):
        con.execute("INSERT INTO track (id, title, artist, path, duration) "
                    "VALUES (?, ?, ?, ?, 200)", (i + 1, title, artist, path))
    con.commit()
    con.close()
    return db


def _make_music_db(tmp_path, rows):
    return _make_music_db_at(str(tmp_path / "music.db"), rows)


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    ll.reset_for_test()
    P.reset_music_db_cache_for_test()
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(CONF, "library_dir", str(tmp_path / "library"))
    os.makedirs(CONF["library_dir"], exist_ok=True)
    # 每个用例自己显式声明开关；默认值一律清掉，避免上一条用例或宿主环境残留。
    # A 侧 FNMUSIC_LOCAL_FIRST 默认 false（G 默认 true），用例要开就自己 setenv。
    for k in ("FNMUSIC_LOCAL_FIRST", "FNMUSIC_LOCAL_FIRST_ANY_CLASS",
              "FNMUSIC_LOCAL_FIRST_CELLULAR_LOSSY_ONLY", "FNMUSIC_QUALITY_POLICY",
              "FNMUSIC_QUALITY_WIFI", "FNMUSIC_QUALITY_CELLULAR",
              "FNMUSIC_QUALITY_FIXED", "FNMUSIC_QUALITY_DYNAMIC"):
        monkeypatch.delenv(k, raising=False)
    # 隔离本机 ambient 环境：DSH shell 导出了飞牛网关的授权目录变量（且
    # /var/run/trim_open_gateway_apiscope.socket 真实存在），会让依赖授权的断言
    # 在不同机器上结果不同。这里清掉，保证断言可复现（是环境隔离，不是弱化断言）。
    monkeypatch.delenv("TRIM_DATA_ACCESSIBLE_PATHS", raising=False)
    monkeypatch.delenv("TRIM_DATA_SHARE_PATHS", raising=False)
    yield
    ll.reset_for_test()


def _write_audio(name: str, size: int = 4096) -> str:
    path = os.path.join(CONF["library_dir"], name)
    with open(path, "wb") as f:
        f.write(b"A" * size)
    return path


# ---------------------------------------------------------------------------
# 索引与匹配
# ---------------------------------------------------------------------------


def test_build_index_and_match(tmp_path):
    flac = _write_audio("周杰伦 - 晴天.flac")
    db = _make_music_db(tmp_path, [
        ("晴天", "周杰伦", flac),
        ("晴天", "翻唱者", _write_audio("翻唱 - 晴天.mp3")),   # 同名不同人
        ("别的歌", "别人", _write_audio("别人 - 别的歌.mp3")),
        ("无路径歌", "谁", ""),                                # 脏数据
    ])
    hit = ll.find_local_match("晴天", "周杰伦", db)
    assert hit is not None
    assert hit["path"] == flac
    assert hit["klass"] == "lossless"

    # 同名不同艺术家：主艺术家不匹配 → 不命中（宁可不命中也不能放错歌）
    assert ll.find_local_match("晴天", "别人", db) is None
    # 艺术家一侧缺失视为匹配（排序里无损优先，翻唱是 mp3、原唱 flac → 取 flac）
    hit2 = ll.find_local_match("晴天", "", db)
    assert hit2 is not None and hit2["path"] == flac
    # 无同名 → 不命中
    assert ll.find_local_match("不存在的歌", "周杰伦", db) is None


def test_match_normalization_case_and_punctuation(tmp_path):
    flac = _write_audio("x - Song_Name.flac")
    db = _make_music_db(tmp_path, [("Song Name", "Some Artist", flac)])
    # 大小写、空格、下划线差异都应命中
    assert ll.find_local_match("song  name", "some artist", db) is not None
    assert ll.find_local_match("SONG-NAME", "SOME ARTIST / Guest", db) is not None


def test_index_cached_and_ttl(tmp_path, monkeypatch):
    flac = _write_audio("a - t.flac")
    db = _make_music_db(tmp_path, [("t", "a", flac)])
    assert ll.find_local_match("t", "a", db) is not None
    # TTL 内即使 db 被删也走缓存（索引已加载）
    os.remove(db)
    assert ll.find_local_match("t", "a", db) is not None
    # 过期后重建 → db 没了 → 不命中（且不抛异常）
    monkeypatch.setenv("FNMUSIC_LOCAL_INDEX_TTL", "0")
    _stale = str(tmp_path / "stale.db")
    ll._INDEX_CACHE[db] = (0.0, ll._INDEX_CACHE[db][1])
    assert ll.find_local_match("t", "a", db) is None


def test_db_missing_returns_none(tmp_path):
    assert ll.find_local_match("晴天", "周杰伦", str(tmp_path / "nope.db")) is None


def test_klass_helpers(monkeypatch):
    assert ll.klass_of_ext("flac") == "lossless"
    assert ll.klass_of_ext(".wav") == "lossless"
    assert ll.klass_of_ext("mp3") == "lossy"
    assert ll.klass_of_level("jymaster") == "lossless"
    assert ll.klass_of_level("lossless") == "lossless"
    assert ll.klass_of_level("exhigh") == "lossy"
    assert ll.klass_of_level("standard") == "lossy"


def test_serves_request_default_is_any_class(monkeypatch):
    """v2.9.16 起默认「本地有就播」。

    真机 level=jymaster + 本地 mp3 被严格同类拒掉 → 转去网易云要无损，
    **而网易云给的其实也是 MP3**（proxy.log 实锤）。绕一圈出外网拿回同样的
    东西，所以默认改成不挑档位（同名多首时仍优先无损）。
    """
    monkeypatch.delenv("FNMUSIC_LOCAL_FIRST_ANY_CLASS", raising=False)
    assert ll.any_class_allowed() is True
    assert ll.serves_request({"ext": "mp3"}, "jymaster") is True
    # 不传 network 时按**非局域网**处理（fail-safe）：宁可让给在线 320k，
    # 也不能在窄管道上灌 30MB 本地无损。要放行本地无损必须明确传 lan/wifi。
    assert ll.serves_request({"ext": "flac"}, "exhigh") is False
    assert ll.serves_request({"ext": "flac"}, "exhigh", "lan") is True


def test_serves_request_strict_mode(monkeypatch):
    """设成 false 才要求同类：非无损不播 / 省流量时不喂母带。"""
    monkeypatch.setenv("FNMUSIC_LOCAL_FIRST_ANY_CLASS", "false")
    assert ll.any_class_allowed() is False
    assert ll.serves_request({"ext": "flac"}, "jymaster") is True
    assert ll.serves_request({"ext": "flac"}, "exhigh") is False   # 省流量不喂本地母带
    assert ll.serves_request({"ext": "mp3"}, "jymaster") is False  # 320k 冒充不了无损
    assert ll.serves_request({"ext": "mp3"}, "exhigh") is True


# ---------------------------------------------------------------------------
# stream 集成：local-first 三种结局
# ---------------------------------------------------------------------------

_SONG_ID = "228908"


def _wire(monkeypatch, *, play_url=None, cdn=(200, b"NETEASE_AUDIO" * 300),
          info_title="晴天", info_artist="周杰伦"):
    def _musicbox(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/auth/detail":
            return httpx.Response(200, json={"ok": True, "data": {"logged_in": True}})
        if path == f"/api/v1/song/{_SONG_ID}/info":
            return httpx.Response(200, json={"ok": True, "data": {
                "name": info_title,
                "ar": [{"name": info_artist}],
                "al": {"name": "叶惠美", "picUrl": ""},
                "dt": 269000,
                "sq": {"size": 28000000},
            }})
        if path == f"/api/v1/song/{_SONG_ID}/lyric":
            return httpx.Response(200, json={"ok": True, "data": {"lyric": "", "tlyric": ""}})
        if path == f"/api/v1/song/{_SONG_ID}/url":
            if not play_url:
                return httpx.Response(200, json={"ok": True, "data": {"code": 404, "url": None}})
            return httpx.Response(200, json={"ok": True,
                                             "data": {"code": 200, "url": play_url}})
        return httpx.Response(404, json={"ok": False})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500)), base_url="http://unix")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_musicbox), base_url="http://127.0.0.1:8770")
    netease_auth.invalidate_state()

    if play_url:
        status, content = cdn
        orig_init = httpx.AsyncClient.__init__

        def _mock_init(self, *args, **kwargs):
            if "base_url" not in kwargs and not kwargs.get("transport"):
                kwargs["transport"] = httpx.MockTransport(
                    lambda r: httpx.Response(status, content=content,
                                             headers={"Content-Type": "audio/mpeg"}))
            orig_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_init)


def test_stream_local_first_serves_local_when_class_matches(monkeypatch, tmp_path):
    """策略要 lossless、本地有无损同名曲 → 直接读本地，响应即本地文件字节。"""
    # A 是三音源架构且 netease 默认关闭（G 是网易单源）；本用例要走网易链路，
    # 必须显式启用该源——这是架构差异的构造适配，断言未变弱。
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setenv("FNMUSIC_LOCAL_FIRST", "true")
    flac_bytes = b"LOCAL_FLAC_BYTES" * 300
    local_flac = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.flac")
    with open(local_flac, "wb") as f:
        f.write(flac_bytes)
    db = _make_music_db(tmp_path, [("晴天", "周杰伦", local_flac)])
    monkeypatch.setitem(CONF, "music_db", db)

    # 网易云侧故意全挂：无直链。本地命中 + 档位一致 → 仍应出声
    _wire(monkeypatch, play_url=None)
    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/track/stream?guid=online:netease:{_SONG_ID}",
                          headers={"Range": "bytes=0-"})
        assert resp.status_code == 206
        assert resp.content == flac_bytes
        assert resp.headers["content-type"] == "audio/flac"


def test_stream_local_first_skipped_when_policy_wants_lower(monkeypatch, tmp_path):
    """严格模式下：策略要 exhigh（省流量）、本地只有 Hi-Res/无损 → 不用本地。"""
    monkeypatch.setitem(CONF, "netease_enabled", True)   # A 默认关闭 netease，见上
    monkeypatch.setenv("FNMUSIC_LOCAL_FIRST", "true")
    local_flac = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.flac")
    with open(local_flac, "wb") as f:
        f.write(b"LOCAL_FLAC" * 300)
    db = _make_music_db(tmp_path, [("晴天", "周杰伦", local_flac)])
    monkeypatch.setitem(CONF, "music_db", db)
    monkeypatch.setenv("FNMUSIC_QUALITY_POLICY", "by_network")
    monkeypatch.setenv("FNMUSIC_QUALITY_WIFI", "exhigh")
    monkeypatch.setenv("FNMUSIC_LOCAL_FIRST_ANY_CLASS", "false")

    cdn_audio = b"NETEASE_320K" * 300
    _wire(monkeypatch, play_url="http://cdn.test/song.mp3", cdn=(200, cdn_audio))
    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/track/stream?guid=online:netease:{_SONG_ID}",
                          headers={"Range": "bytes=0-"})
        assert resp.status_code == 200
        assert resp.content.startswith(b"NETEASE_320K"), "省流量档必须走网易云 320k，不能喂本地无损"


def test_stream_local_first_disabled_by_env(monkeypatch, tmp_path):
    """FNMUSIC_LOCAL_FIRST=false 时完全不启用本地匹配。"""
    monkeypatch.setitem(CONF, "netease_enabled", True)   # 确保 404 来自"取链失败"而非"源被关"
    local_flac = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.flac")
    with open(local_flac, "wb") as f:
        f.write(b"LOCAL_FLAC" * 300)
    db = _make_music_db(tmp_path, [("晴天", "周杰伦", local_flac)])
    monkeypatch.setitem(CONF, "music_db", db)
    monkeypatch.setenv("FNMUSIC_LOCAL_FIRST", "false")

    _wire(monkeypatch, play_url=None)   # 网易云无直链
    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/track/stream?guid=online:netease:{_SONG_ID}")
        assert resp.status_code == 404, "关闭本地优先时无直链应如实 404"


# ---------------------------------------------------------------------------
# v2.9.14：索引不再只靠 music.db —— 曲库目录文件系统扫描
# ---------------------------------------------------------------------------


def test_fs_index_builds_from_library_dir_when_db_has_no_tracks(tmp_path):
    """真机事故：music.db 里只有 shared_library、没有曲目表 → 索引恒为空、
    本地优先一次都不命中，界面上却毫无迹象。现在目录扫描必须能兜住。"""
    flac = _write_audio("周杰伦 - 晴天.flac")
    empty_db = str(tmp_path / "nope.db")     # 库不存在

    assert ll.find_local_match("晴天", "周杰伦", empty_db, CONF["library_dir"])["path"] == flac
    assert ll.find_local_match("不存在的歌", "周杰伦", empty_db, CONF["library_dir"]) is None


def test_fs_index_uses_parent_dir_as_artist(tmp_path):
    """/曲库/许嵩/庐州月.flac —— 文件名没分隔符时退用父目录名当艺术家。"""
    folder = os.path.join(CONF["library_dir"], "许嵩")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "庐州月.flac")
    with open(path, "wb") as f:
        f.write(b"A" * 1024)

    hit = ll.find_local_match("庐州月", "许嵩", str(tmp_path / "n.db"), CONF["library_dir"])
    assert hit is not None and hit["path"] == path
    assert ll.artist_compatible(hit["artist"], "许嵩")


def test_fs_index_ignores_non_audio_and_empty_files(tmp_path):
    _write_audio("cover.jpg", 2048)
    _write_audio("歌词.lrc", 512)
    _write_audio("空的 - 文件.mp3", 0)          # 0 字节不算曲目
    good = _write_audio("周杰伦 - 稻香.mp3")

    idx = ll.build_fs_index(CONF["library_dir"])
    paths = [i["path"] for es in idx.values() for i in es]
    assert paths == [good], paths


def test_index_merges_db_and_fs_without_duplicate_paths(tmp_path):
    """同一首歌 db 和目录都索引到时，只保留一条（db 优先，它带真实标签）。"""
    shared = _write_audio("周杰伦 - 晴天.flac")
    other = _write_audio("周杰伦 - 稻香.mp3")
    db = _make_music_db(tmp_path, [("晴天", "周杰伦", shared)])

    hit = ll.find_local_match("晴天", "周杰伦", db, CONF["library_dir"])
    assert hit["path"] == shared
    assert hit.get("src") == "db", "同一 path 去重后应保留 music.db 的条目"

    st = ll.status(db, CONF["library_dir"])
    assert st["from_db"] == 1
    assert other in [i["path"] for es in
                     ll._get_index(db, CONF["library_dir"]).values() for i in es]


def test_artist_compatible_allows_containment():
    assert ll.artist_compatible("", "周杰伦") is True          # 一侧缺失
    assert ll.artist_compatible("周杰伦", "") is True
    assert ll.artist_compatible("周杰伦", "周杰伦") is True
    assert ll.artist_compatible("许嵩 / 何曼婷", "许嵩") is True  # 主艺术家包含
    assert ll.artist_compatible("许嵩", "许嵩 / 何曼婷") is True
    assert ll.artist_compatible("周杰伦", "蔡依林") is False
    assert ll.artist_compatible("周杰伦", "周杰伦与合唱团") is True


def test_split_name_separators():
    assert ll._split_name("周杰伦 - 晴天") == ("周杰伦", "晴天")
    assert ll._split_name("许嵩 _ 庐州月") == ("许嵩", "庐州月")
    assert ll._split_name("庐州月", "许嵩") == ("许嵩", "庐州月")


def test_match_strips_parenthesised_suffix(tmp_path):
    """网易云「晴天 (Live)」「演员（伴奏）」在本地库里往往就叫「晴天」「演员」。

    注意必须在归一化**之前**剥括号：_STRIP_RE 只去掉括号字符、留下里面的词，
    "晴天 (Live)" 会归一化成 "晴天live"，仍然对不上。
    """
    flac = _write_audio("周杰伦 - 晴天.flac")
    db = _make_music_db(tmp_path, [("晴天", "周杰伦", flac)])

    assert ll.find_local_match("晴天 (Live)", "周杰伦", db)["path"] == flac
    assert ll.find_local_match("晴天（现场版）", "周杰伦", db)["path"] == flac
    # 反向：本地文件名带括号，网易云是干净标题
    live = _write_audio("周杰伦 - 稻香 (Live).flac")
    db2 = str(tmp_path / "music2.db")
    _make_music_db_at(db2, [("稻香 (Live)", "周杰伦", live)])
    assert ll.find_local_match("稻香", "周杰伦", db2)["path"] == live


def test_db_title_that_is_actually_a_filename(tmp_path):
    """真机 music.db 的 title 存的是完整文件名、artist 为空（v2.9.17 核心 bug）。

    title = "Beyond - 光辉岁月.flac" / artist = None
    之前直接拿它归一化 → "beyond光辉岁月flac"，而查询用的是网易云的纯标题
    "光辉岁月" —— 915 首的索引一首都匹配不上，自测却显示「能匹配上自己」。
    """
    p = _write_audio("Beyond - 光辉岁月.flac")
    db = _make_music_db(tmp_path, [("Beyond - 光辉岁月.flac", None, p)])

    hit = ll.find_local_match("光辉岁月", "Beyond", db)
    assert hit is not None and hit["path"] == p
    assert hit["artist"] == "Beyond", "artist 也应从文件名里补出来"
    # 不带艺术家也能中（网易云有时给的是 "Beyond / 黄家驹" 这类写法）
    assert ll.find_local_match("光辉岁月", "", db) is not None


def test_entry_variants_covers_filename_and_path():
    v = ll.entry_variants("Beyond - 光辉岁月.flac", None, "/x/Beyond - 光辉岁月.flac")
    titles = {t for t, _ in v}
    assert "光辉岁月" in titles
    assert "Beyond - 光辉岁月" in titles
    # 纯标题 + 空 artist 时不该被拆坏
    assert ("晴天", "周杰伦") in ll.entry_variants("晴天", "周杰伦", "/a/周杰伦 - 晴天.flac")
    assert ll.entry_variants("", "", "") == []


def test_title_keys():
    assert ll.title_keys("晴天") == ["晴天"]
    assert ll.title_keys("晴天 (Live)") == ["晴天live", "晴天"]
    assert ll.title_keys("") == []


def test_status_reports_empty_reason_when_nothing_indexed(monkeypatch, tmp_path):
    """索引为空时诊断页必须给出「为什么空」，而不是一个沉默的 0。"""
    monkeypatch.setenv("FNMUSIC_LOCAL_FIRST", "true")
    st = ll.status(str(tmp_path / "nope.db"), "")
    assert st["entries"] == 0
    assert st["empty_reason"], "必须说明索引为什么是空的"
    assert st["enabled"] is True


def test_lookup_log_records_miss_reason(tmp_path):
    ll.find_local_match("不存在的歌", "周杰伦", str(tmp_path / "n.db"))
    st = ll.status(str(tmp_path / "n.db"), "")
    assert st["lookups"] == 1 and st["lookup_hits"] == 0
    assert st["recent"][-1]["reason"] == "title-not-in-index"


# ---------------------------------------------------------------------------
# v2.8.1：music.db 自动定位
# ---------------------------------------------------------------------------


def test_resolve_music_db_prefers_existing_explicit(monkeypatch, tmp_path):
    db = _make_music_db(tmp_path, [("t", "a", "/x.flac")])
    monkeypatch.setitem(CONF, "music_db", db)
    P.reset_music_db_cache_for_test()
    assert P.resolve_music_db() == db


def test_resolve_music_db_falls_back_when_explicit_missing(monkeypatch, tmp_path):
    """显式配置的路径不存在时必须探测常见布局，而不是带着死路径静默失效。

    真机事故：默认路径 /usr/local/apps/... 在该机器上不存在（飞牛数据在
    /vol*/@appdata），诊断里「music.db 不存在」，本地曲库优先建立在空库上。
    """
    monkeypatch.setitem(CONF, "music_db", "/nonexistent/music.db")
    P.reset_music_db_cache_for_test()
    fake = str(tmp_path / "vol1_appdata.db")
    con = sqlite3.connect(fake)
    con.execute("CREATE TABLE t (x)")
    con.commit()
    con.close()
    import glob as _glob
    orig_glob = _glob.glob
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(P.glob, "glob",
                   lambda pat, *a, **k: [fake] if "trim.music" in pat else orig_glob(pat))
        got = P.resolve_music_db()
    assert got == fake, "应探测到 /vol*/@appdata/trim.music/db/music.db 布局"


def test_resolve_music_db_uses_default_when_nothing_exists(monkeypatch):
    monkeypatch.setitem(CONF, "music_db", "/nonexistent/music.db")
    P.reset_music_db_cache_for_test()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(P.glob, "glob", lambda pat, *a, **k: [])
        assert P.resolve_music_db() == "/nonexistent/music.db"


# ------------------------------------------------- v2.9.20 music.db 失效记录


def test_broken_db_records_are_dropped_and_do_not_shadow_fs(tmp_path, monkeypatch):
    """music.db 里文件已删的记录必须剔除——否则它会按 path 相等把目录扫到的
    同名真文件挡在门外（fs 补充恒为 0），诊断里就只剩一句没主语的 file-missing。"""
    import proxy.local_library as L
    from proxy.local_library import reset_for_test

    lib = tmp_path / "lib"
    lib.mkdir()
    real = lib / "Beyond - 光辉岁月.flac"
    real.write_bytes(b"fake")
    db = tmp_path / "music.db"
    _make_music_db_at(
        db,
        [( "Beyond - 光辉岁月.flac", None, str(tmp_path / "已搬走" / "旧路径.flac"))],
    )
    reset_for_test()
    idx = L._get_index(str(db), str(lib))
    paths = {str(i.get("path")) for es in idx.values() for i in es}
    assert str(real) in paths, "目录扫到的真文件必须补进来"
    assert not any("旧路径" in p for p in paths), "失效记录不该留在索引里"
    assert L._INDEX_META[str(db) + "|" + str(lib)]["db_broken"] == 1


def test_broken_db_paths_are_reported_in_status(tmp_path):
    import proxy.local_library as L
    from proxy.local_library import reset_for_test

    db = tmp_path / "music.db"
    _make_music_db_at(db, [("张三 - 随便.mp3", None, "/不存在的目录/随便.mp3")])
    reset_for_test()
    st = L.status(str(db), "")
    assert st["db_broken"] == 1
    assert st["entries"] == 0


def test_file_missing_records_the_failing_path(tmp_path):
    """file-missing 不带路径就是一句废话——到底是拼错了还是文件真没了查不下去。"""
    import proxy.local_library as L
    from proxy.local_library import reset_for_test

    db = tmp_path / "music.db"
    gone = "/不存在的目录/光辉岁月.flac"
    _make_music_db_at(db, [("Beyond - 光辉岁月.flac", None, gone)])
    reset_for_test()
    # 直接走索引路径：让索引保留失效记录，验证 miss_path 被记下
    L._INDEX_CACHE.clear()
    L._INDEX_META.clear()
    L._LOOKUP_LOG.clear()
    idx = L.build_index(str(db))
    L._INDEX_CACHE[str(db)] = (L.time.time() + 9999, idx)
    assert L.find_local_match("光辉岁月", "Beyond", str(db), "") is None
    assert L._LOOKUP_LOG[-1]["reason"] == "file-missing"
    assert L._LOOKUP_LOG[-1]["miss_path"] == gone
    # 命中时不带 miss_path，免得看着像出错
    L._LOOKUP_LOG.clear()


# --------------------------------- v2.9.20 流量网络下本地无损让给在线省流档


def test_cellular_skips_local_lossless(monkeypatch):
    """本地 FLAC 30~40MB vs 在线 320k ~9MB：数据网络下必须让给在线。

    真机反例：本地优先命中率修好之后用户反而反馈「比之前慢，等 7~8 秒」——
    命中的全是 .flac，全都从 NAS 灌 30MB 到手机。
    """
    import proxy.local_library as L
    assert L.serves_request({"ext": "flac"}, "exhigh", "cellular") is False
    assert L._CELL_SKIPS["n"] == 1
    # 有损体积与在线 320k 相当，本地仍更快（省掉外网往返），照旧走本地
    assert L.serves_request({"ext": "mp3"}, "exhigh", "cellular") is True
    assert L.serves_request({"ext": "m4a"}, "exhigh", "cellular") is True


def test_wifi_and_lan_keep_using_local_lossless():
    """WiFi / 局域网不受影响——流量规则只在窄管道下才有意义。"""
    import proxy.local_library as L
    assert L.serves_request({"ext": "flac"}, "jymaster", "wifi") is True
    assert L.serves_request({"ext": "flac"}, "jymaster", "lan") is True
    # 未给出网络线索时按非局域网处理：这是刻意的 fail-safe，不是漏判
    assert L.serves_request({"ext": "flac"}, "jymaster", "") is False


def test_cellular_lossy_only_can_be_disabled(monkeypatch):
    monkeypatch.setenv("FNMUSIC_LOCAL_FIRST_CELLULAR_LOSSY_ONLY", "false")
    import proxy.local_library as L
    assert L.serves_request({"ext": "flac"}, "exhigh", "cellular") is True


def test_status_reports_cellular_rule_and_skip_count(monkeypatch):
    import proxy.local_library as L
    from proxy.local_library import reset_for_test
    reset_for_test()
    L.serves_request({"ext": "flac"}, "exhigh", "cellular")
    L.serves_request({"ext": "flac"}, "exhigh", "cellular")
    st = L.status("", "")
    assert st["cellular_lossy_only"] is True
    assert st["cellular_skips"] == 2
