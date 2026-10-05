"""收藏自动归档（最高品质 + 歌词 + 歌手子目录）的单元测试。

这是本扩展里唯一会**主动往用户自己指定的目录写文件**的功能，因此边界条件必须钉死：
路径校验、半截文件、重复归档、更高品质替换。写坏了用户很难自己排查。
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from proxy import download as dl


# --------------------------------------------------------------------- 夹具


class _Resp:
    def __init__(self, code, body=None, chunks=None, size=None, ctype="audio/flac"):
        self.status_code = code
        self._body = body or {}
        self._chunks = chunks if chunks is not None else [b"x" * 4096]
        self.headers = {"content-type": ctype}
        if size is not None:
            self.headers["content-length"] = str(size)

    def json(self):
        return self._body

    async def aiter_bytes(self, chunk_size=None):
        for c in self._chunks:
            yield c

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    """替身 httpx client：只需要 get / stream 两个方法。"""

    def __init__(self, routes=None, stream_resp=None):
        self.routes = routes or {}
        self.stream_resp = stream_resp
        self.calls = []
        self.streamed = []

    async def get(self, path, params=None, timeout=None):
        self.calls.append(path)
        body = self.routes.get(path)
        return _Resp(200, body) if body is not None else _Resp(404, {})

    def stream(self, method, url, timeout=None, follow_redirects=None):
        self.streamed.append(url)
        return self.stream_resp or _Resp(200, chunks=[b"a" * 2048], size=2048)


@pytest.fixture
def archive_dir(tmp_path, monkeypatch):
    d = tmp_path / "网易云收藏"
    d.mkdir()
    monkeypatch.setenv("FNMUSIC_DOWNLOAD_DIR", str(d))
    monkeypatch.setenv("FNMUSIC_DOWNLOAD_ON_FAVORITE", "true")
    yield d
    dl._TASKS.clear()


BEST = {"ok": True, "data": {"url": "http://cdn/a.flac", "code": 200, "best_quality": "lossless",
                            "level": "lossless", "type": "flac", "br": 999000}}
LYRIC = {"ok": True, "data": {"lyric": "[00:01.00]第一句\n[00:05.00]第二句"}}
META = {"title": "晴天", "artist": "周杰伦", "album": "叶惠美"}


# --------------------------------------------------------------------- 路径安全


def test_validate_dir_rejects_unsafe_paths(tmp_path, monkeypatch):
    ok, why = dl.validate_dir("")
    assert not ok and "未配置" in why

    ok, why = dl.validate_dir("relative/path")
    assert not ok and "绝对路径" in why

    ok, why = dl.validate_dir(str(tmp_path / "does-not-exist"))
    assert not ok and "不存在" in why

    # 系统目录必须硬拦：往这些地方递归建目录写音频，用户几乎无法自己清理
    for bad in ("/", "/etc", "/var", "/root", "/vol1/@appdata"):
        ok, why = dl.validate_dir(bad)
        assert not ok, f"{bad} 必须被拒绝"
        assert "系统目录" in why

    good = tmp_path / "ok"
    good.mkdir()
    ok, why = dl.validate_dir(str(good))
    assert ok and why == "ok"


def test_enabled_requires_dir_and_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("FNMUSIC_DOWNLOAD_DIR", "")
    monkeypatch.setenv("FNMUSIC_DOWNLOAD_ON_FAVORITE", "true")
    assert dl.download_enabled() is False, "没配目录就必须是关闭状态"

    d = tmp_path / "d"; d.mkdir()
    monkeypatch.setenv("FNMUSIC_DOWNLOAD_DIR", str(d))
    assert dl.download_enabled() is True
    monkeypatch.setenv("FNMUSIC_DOWNLOAD_ON_FAVORITE", "false")
    assert dl.download_enabled() is False

    monkeypatch.delenv("FNMUSIC_FAV_SYNC_LIKE", raising=False)
    # v2.9.28：收藏同步回网易云已取消（页面不再提供入口），默认改为关闭。
    assert dl.like_sync_enabled() is False, "红心同步默认关"
    monkeypatch.setenv("FNMUSIC_FAV_SYNC_LIKE", "off")
    assert dl.like_sync_enabled() is False


def test_safe_component_sanitizes_artist_names(tmp_path, monkeypatch):
    """网易云的多歌手名是 `A / B` 形式，直接当目录名会建出错误层级。"""
    monkeypatch.setenv("FNMUSIC_DOWNLOAD_DIR", str(tmp_path))
    assert "/" not in dl.safe_component("周杰伦 / 费玉清")
    assert dl.safe_component("周杰伦 / 费玉清") == "周杰伦 _ 费玉清"
    # 非法字符全替换后还要 strip 掉首尾的 . _ 空格，否则会留下 AC_DC______ 这种丑名字
    assert dl.safe_component('AC:DC*?"<>|') == "AC_DC"
    assert dl.safe_component("...") == "未知", "全是分隔符时不能生成空文件名"
    assert dl.safe_component("   ") == "未知"
    assert dl.safe_component(None) == "未知"
    assert len(dl.safe_component("x" * 300)) <= 80


def test_target_paths_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_DOWNLOAD_DIR", str(tmp_path))
    audio, lrc = dl.target_paths("周杰伦", "晴天", "flac")
    assert audio == str(tmp_path / "周杰伦" / "周杰伦 - 晴天.flac")
    assert lrc == str(tmp_path / "周杰伦" / "周杰伦 - 晴天.lrc")
    assert Path(lrc).parent == Path(audio).parent, "歌词必须与音频同目录同名"


def test_ext_for_info():
    assert dl._ext_for({"type": "flac"}) == "flac"
    assert dl._ext_for({"encodeType": "flac"}) == "flac"
    assert dl._ext_for({"type": "mp3"}) == "mp3"
    assert dl._ext_for({"level": "hires"}) == "flac"
    assert dl._ext_for({"level": "jymaster"}) == "flac"
    assert dl._ext_for({}) == "mp3"


# --------------------------------------------------------------------- 归档主体


@pytest.mark.anyio
async def test_archive_happy_path(archive_dir):
    client = _FakeClient({"/api/v1/song/100/best_url": BEST, "/api/v1/song/100/lyric": LYRIC})
    refs = []
    res = await dl.archive_song(client, "100", META, ref_writer=refs.append)
    assert res["ok"] is True, res
    assert res["quality"] == "lossless"

    audio = archive_dir / "周杰伦" / "周杰伦 - 晴天.flac"
    lrc = archive_dir / "周杰伦" / "周杰伦 - 晴天.lrc"
    assert audio.exists() and audio.stat().st_size == 2048
    assert "[00:01.00]第一句" in lrc.read_text(encoding="utf-8")

    marker = dl.read_marker(str(audio))
    assert marker["song_id"] == "100" and marker["quality"] == "lossless"

    assert refs == [str(audio)], "必须登记 .ref，卸载时才能精确删除"
    assert not list(archive_dir.rglob("*.part")), "临时文件必须清掉"


@pytest.mark.anyio
async def test_archive_without_lyric_still_writes_audio(archive_dir):
    client = _FakeClient({"/api/v1/song/100/best_url": BEST,
                          "/api/v1/song/100/lyric": {"ok": True, "data": {"lyric": ""}}})
    res = await dl.archive_song(client, "100", META)
    assert res["ok"] is True
    audio = archive_dir / "周杰伦" / "周杰伦 - 晴天.flac"
    assert audio.exists()
    assert res["lrc"] is False
    assert not (archive_dir / "周杰伦" / "周杰伦 - 晴天.lrc").exists(), "空歌词不该留个空文件"


@pytest.mark.anyio
async def test_incomplete_download_is_discarded(archive_dir):
    """半截文件绝不能留下：飞牛扫到一段坏音频比没有文件更糟。"""
    client = _FakeClient(
        {"/api/v1/song/100/best_url": BEST},
        stream_resp=_Resp(200, chunks=[b"a" * 500], size=100000),   # 声明 100KB 只给 500B
    )
    res = await dl.archive_song(client, "100", META)
    assert res["ok"] is False
    assert res["reason"].startswith("incomplete_download"), res
    assert not (archive_dir / "周杰伦").exists() or not list((archive_dir / "周杰伦").iterdir())
    assert not list(archive_dir.rglob("*.part"))


@pytest.mark.anyio
async def test_tiny_stream_is_discarded(archive_dir):
    client = _FakeClient({"/api/v1/song/100/best_url": BEST},
                         stream_resp=_Resp(200, chunks=[b"a"], size=None))
    res = await dl.archive_song(client, "100", META)
    assert res["ok"] is False and res["reason"].startswith("incomplete_download")


@pytest.mark.anyio
async def test_upstream_http_error_is_reported(archive_dir):
    client = _FakeClient({"/api/v1/song/100/best_url": BEST},
                         stream_resp=_Resp(403, chunks=[]))
    res = await dl.archive_song(client, "100", META)
    assert res["ok"] is False and res["reason"] == "upstream_http_403"
    assert not list(archive_dir.rglob("*.flac"))


@pytest.mark.anyio
async def test_no_playable_quality_is_reported(archive_dir):
    """账号拿不到任何品质直链时如实说明，不要写一个 0 字节文件充数。"""
    client = _FakeClient({"/api/v1/song/100/best_url": {"ok": False, "error": "no_playable_quality",
                                                       "data": {}}})
    res = await dl.archive_song(client, "100", META)
    assert res["ok"] is False and res["reason"] == "no_playable_quality"
    assert list(archive_dir.iterdir()) == []


@pytest.mark.anyio
async def test_archive_disabled_without_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_DOWNLOAD_DIR", "")
    res = await dl.archive_song(_FakeClient({}), "100", META)
    assert res["ok"] is False and "未配置" in res["reason"]


@pytest.mark.anyio
async def test_already_archived_same_quality_is_skipped(archive_dir):
    client = _FakeClient({"/api/v1/song/100/best_url": BEST, "/api/v1/song/100/lyric": LYRIC})
    first = await dl.archive_song(client, "100", META)
    assert first["ok"] is True
    audio = archive_dir / "周杰伦" / "周杰伦 - 晴天.flac"
    mtime = audio.stat().st_mtime

    again = await dl.archive_song(client, "100", META)
    assert again["ok"] is True and again["skipped"] == "already_archived"
    assert audio.stat().st_mtime == mtime, "同品质不该重写文件"


@pytest.mark.anyio
async def test_higher_quality_replaces_existing(archive_dir):
    client = _FakeClient({"/api/v1/song/100/best_url": BEST, "/api/v1/song/100/lyric": LYRIC})
    await dl.archive_song(client, "100", META)
    audio = archive_dir / "周杰伦" / "周杰伦 - 晴天.flac"

    better = {"ok": True, "data": {"url": "http://cdn/a2.flac", "code": 200,
                                   "best_quality": "jymaster", "level": "jymaster", "type": "flac"}}
    client2 = _FakeClient({"/api/v1/song/100/best_url": better, "/api/v1/song/100/lyric": LYRIC},
                          stream_resp=_Resp(200, chunks=[b"b" * 9000], size=9000))
    res = await dl.archive_song(client2, "100", META)
    assert res["ok"] is True and res["quality"] == "jymaster"
    assert audio.stat().st_size == 9000, "拿到更高品质必须替换掉旧文件"
    assert dl.read_marker(str(audio))["quality"] == "jymaster"


def test_quality_rank_ordering():
    assert dl._quality_rank_of("jymaster") > dl._quality_rank_of("hires")
    assert dl._quality_rank_of("hires") > dl._quality_rank_of("lossless")
    assert dl._quality_rank_of("lossless") > dl._quality_rank_of("exhigh")
    assert dl._quality_rank_of("") == -1, "未知品质必须排在所有已知品质之下"


@pytest.mark.anyio
async def test_enqueue_dedupes_in_progress(monkeypatch, archive_dir):
    started = []

    async def slow_archive(client, song_id, meta, ref_writer=None):
        started.append(song_id)
        import asyncio
        await asyncio.sleep(0.3)
        return {"ok": True}

    monkeypatch.setattr(dl, "archive_song", slow_archive)
    import httpx

    client = httpx.AsyncClient(base_url="http://testserver")
    assert dl.enqueue(client, "7", META) == "enqueued"
    assert dl.enqueue(client, "7", META) == "in_progress", "同一首在跑就不该重复下载"
    assert dl.enqueue(client, "8", META) == "enqueued"
    assert dl.pending_count() == 2
    import asyncio
    await asyncio.sleep(0.4)
    assert dl.pending_count() == 0, "任务结束后必须自己出队，否则字典只增不减"
    assert sorted(started) == ["7", "8"]
    await client.aclose()
