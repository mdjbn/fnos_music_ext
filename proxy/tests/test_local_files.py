"""proxy/local_files.py —— 本地曲目索引 / 标签 / 封面 单元测试。

背景：本地曲目 guid 是合成的 local:file:<sha1>。v2.9.5 之前 metadata 与封面都
转发给官方后端（它不认这个 guid），于是封面 400、元数据空、duration=0 被客户端
判定不可播——歌单有清单但就是放不了。这些用例把「自己从文件里读出来」钉死。
"""

from __future__ import annotations

import os

import pytest

try:  # 作为包导入（从仓库根跑 pytest）
    from proxy import app, local_files
except ImportError:  # 扁平导入（从 proxy/ 目录跑）
    import app  # type: ignore
    import local_files  # type: ignore


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """把索引与封面缓存都指到 tmp，别污染仓库。"""
    monkeypatch.setenv("FNMUSIC_CACHE_DIR", str(tmp_path / "cache"))
    local_files.reset_for_test()
    yield
    local_files.reset_for_test()


def _mk(tmp_path, name: str, payload: bytes = b"F" * 512) -> str:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return str(path)


# ---------------------------------------------------------------------------
# 索引：sha1 → path
# ---------------------------------------------------------------------------

def test_guid_is_stable_across_relative_and_absolute(tmp_path):
    p = _mk(tmp_path, "a.flac")
    assert local_files.sha1_of_path(p) == local_files.sha1_of_path(os.path.abspath(p))


def test_record_then_lookup_roundtrip(tmp_path):
    p = _mk(tmp_path, "歌手 - 歌曲.flac")
    local_files.record_files([{"path": p, "title": "歌曲", "artist": "歌手", "ext": "flac"}])
    guid = "local:file:" + local_files.sha1_of_path(p)
    ent = local_files.lookup(guid)
    assert ent and ent["path"] == p and ent["title"] == "歌曲"
    assert local_files.resolve(guid) == p


def test_lookup_returns_none_for_unknown_guid():
    assert local_files.lookup("local:file:deadbeef") is None
    assert local_files.resolve("local:file:deadbeef") is None


def test_lookup_returns_none_when_file_vanished(tmp_path):
    """文件被移走/删掉时不能返回陈旧路径，否则播放会去读一个不存在的文件。"""
    p = _mk(tmp_path, "gone.flac")
    local_files.record_files([{"path": p, "title": "x", "artist": "", "ext": "flac"}])
    guid = "local:file:" + local_files.sha1_of_path(p)
    os.unlink(p)
    assert local_files.lookup(guid) is None


def test_index_survives_new_process(tmp_path):
    """索引必须落盘：metadata/cover 请求发生在另一次构建之后。"""
    p = _mk(tmp_path, "子.dir", b"")
    p = _mk(tmp_path / "子", "歌手 - 歌.flac")
    local_files.record_files([{"path": p, "title": "歌", "artist": "歌手", "ext": "flac"}])
    local_files.reset_for_test()          # 模拟进程重启（内存缓存清空）
    guid = "local:file:" + local_files.sha1_of_path(p)
    assert local_files.resolve(guid) == p


# ---------------------------------------------------------------------------
# 标签探测
# ---------------------------------------------------------------------------

def test_probe_gives_real_size_and_zero_duration_for_junk(tmp_path):
    """文件不是音频/损坏时：体积要真、时长可以给 0，但绝不能抛异常。"""
    p = _mk(tmp_path, "junk.flac", b"X" * 1234)
    info = local_files.probe(p)
    assert info["size"] == 1234
    assert info["duration"] >= 0


def test_probe_missing_file_is_safe(tmp_path):
    info = local_files.probe(str(tmp_path / "nope.flac"))
    assert info["duration"] == 0 and info["size"] == 0


# ---------------------------------------------------------------------------
# 封面
# ---------------------------------------------------------------------------

def test_sibling_cover_is_found(tmp_path):
    folder = tmp_path / "album"
    song = _mk(folder, "歌手 - 歌.flac")
    (folder / "cover.jpg").write_bytes(b"\xff\xd8\xfffake-jpeg")
    local_files.record_files([{"path": song, "title": "歌", "artist": "歌手", "ext": "flac"}])
    guid = "local:file:" + local_files.sha1_of_path(song)
    found = local_files.cover(guid)
    assert found is not None
    data, mime = found
    assert data == b"\xff\xd8\xfffake-jpeg" and mime == "image/jpeg"


def test_cover_returns_none_for_artless_file_and_negative_caches(tmp_path):
    """无内嵌图也无同目录图 → None；且第二次直接命中负缓存，不再解标签。"""
    song = _mk(tmp_path, "artless.flac")
    local_files.record_files([{"path": song, "title": "t", "artist": "a", "ext": "flac"}])
    guid = "local:file:" + local_files.sha1_of_path(song)
    assert local_files.cover(guid) is None
    assert local_files.cover(guid) is None   # 再调一次不能炸，也不该重复解析


def test_cover_unknown_guid_is_none():
    assert local_files.cover("local:file:unknown") is None


# ---------------------------------------------------------------------------
# metadata 应答形状（客户端会无防护读这些字段）
# ---------------------------------------------------------------------------

def test_local_metadata_payload_shape(tmp_path):
    """genres / album / artists 缺一，客户端 resolveTrackPlayback 就抛错并跳过播放。"""
    song = _mk(tmp_path, "歌手 - 歌.flac")
    local_files.record_files([{"path": song, "title": "歌", "artist": "歌手", "ext": "flac"}])
    guid = "local:file:" + local_files.sha1_of_path(song)
    entry = local_files.entry_with_probe(guid)
    body = app.build_local_metadata_payload(guid, entry)

    assert body["code"] == 0
    track = body["data"]["track"]
    assert isinstance(track["genres"], list)
    assert isinstance(track["album"], dict)
    assert isinstance(track["artists"], list)
    assert track["guid"] == guid
    assert track["album"]["coverId"] == guid
    assert track["duration"] >= 0


def test_local_duration_uses_same_unit_as_online():
    """决定性回归：duration 单位必须与在线曲目一致（毫秒）。

    早期本地版填的是秒，客户端把 240 读成 240 毫秒 → 判定不可播，表现就是
    「列表有歌、点开没反应」。这里直接拿在线构造器做对照，不许再分叉。
    """
    online = app.build_online_track({
        "id": "netease:1", "source": "netease",
        "duration_s": 180.0, "file_size": 4096, "ext": "flac",
        "title": "t", "artist": "a", "album": "b",
    })
    local = app.build_local_metadata_payload("local:file:abc", {
        "path": "/x/t.flac", "duration": 180, "duration_ms": 180000,
        "size": 4096, "ext": "flac", "bitrate": 900000,
        "sample_rate": 44100, "channels": 2,
    })
    assert local["data"]["track"]["duration"] == online["duration"] == 180000
    assert local["data"]["track"]["duration_s"] == online["duration_s"] == 180.0


def test_local_audiospec_carries_path_with_extension():
    """audioSpec.path 必须带真实后缀：飞牛 ll() 靠它解析容器格式。"""
    body = app.build_local_metadata_payload("local:file:abc", {
        "path": "/x/t.flac", "duration": 180, "duration_ms": 180000,
        "size": 4096, "ext": "flac",
    })
    spec = body["data"]["track"]["audioSpec"]
    assert spec["path"].endswith(".flac"), spec
    assert spec["format"] == spec["codec"] == spec["container"] == "flac"
    assert spec["duration"] == 180000
    assert spec["size"] == 4096


def test_local_metadata_title_falls_back_to_filename(tmp_path):
    """标签里没标题时，用文件名兜底，不能返回空壳条目。"""
    song = _mk(tmp_path, "歌手 - 歌.flac")
    guid = "local:file:" + local_files.sha1_of_path(song)
    body = app.build_local_metadata_payload(guid, {"path": song, "ext": "flac"})
    track = body["data"]["track"]
    assert track["title"] == "歌手 - 歌"
    assert track["album"]["name"]
    assert isinstance(track["genres"], list)


def test_local_metadata_path_is_real_absolute_path(tmp_path):
    """客户端「文件位置」必须显示真实路径，不能是 local/<sha1>.mp3 这种假路径。

    v2.9.12 之前 audioSpec.path 写的是 local/<sha1>.<ext>，后缀是真的但路径是
    假的——用户点开歌曲详情看到的「文件位置」毫无意义。播放走 /track/stream
    按 guid 反查路径，本来就不依赖这个字段，改成真实路径没有任何风险。
    """
    song = _mk(tmp_path, "许嵩 - 庐州月.flac")
    guid = "local:file:" + local_files.sha1_of_path(song)
    body = app.build_local_metadata_payload(guid, {
        "path": song, "ext": "flac", "duration": 200,
        "duration_ms": 200000, "size": 1024,
    })
    track = body["data"]["track"]
    assert track["audioSpec"]["path"] == song, "必须是真实绝对路径"
    assert track["path"] == song
    assert track["filePath"] == song
    assert not track["audioSpec"]["path"].startswith("local/"), "不能再是假路径"
    assert track["audioSpec"]["path"].endswith(".flac"), "后缀仍要真实（ll() 靠它解析容器）"


def test_local_metadata_path_falls_back_when_index_has_no_path():
    """索引里没存路径时退回 local/<sha>.<ext>，绝不留空（空 path 拿不到容器格式）。"""
    body = app.build_local_metadata_payload("local:file:deadbeef", {"ext": "mp3"})
    assert body["data"]["track"]["audioSpec"]["path"] == "local/deadbeef.mp3"
