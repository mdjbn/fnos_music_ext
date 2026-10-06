"""在线流的容器/MIME 判定：以字节为准；HLS 分片不能用 video/*。

回归背景（用户实测）：网易云音源播放时「一直转圈」——CDN 把 FLAC 也标成 audio/mpeg，
代理由此把无损当 MP3 下发，播放器按 MIME 选了解码器，永远解不出有效帧。
"""
import httpx
import pytest

import proxy.app as appmod


@pytest.mark.parametrize("head,expect", [
    (b"fLaC\x00\x00\x00\x22", "flac"),
    (b"ID3\x04\x00\x00\x00\x00\x00\x00", "mp3"),
    (b"\xff\xfb\x90\x64", "mp3"),                     # 无 ID3 的帧同步
    (b"\x00\x00\x00\x20ftypM4A ", "m4a"),
    (b"RIFF\x00\x00\x00\x00WAVEfmt ", "wav"),
    (b"OggS\x00\x02\x00\x00", "ogg"),
    (b"MAC \x00\x00", "ape"),
    (b"wvpk\x00\x00", "wv"),
    (b"DSD \x00\x00", "dsf"),
    (b"FRM8\x00\x00", "dff"),
    (b"\x00\x01\x02\x03", None),                      # 认不出来必须回 None，不瞎猜
    (b"", None),
])
def test_sniff_audio_ext(head, expect):
    assert appmod._sniff_audio_ext(head) == expect


def test_stream_tee_response_overrides_wrong_upstream_mime():
    body = b"fLaC\x00\x00\x00\x22" + b"\x00" * 64
    resp = httpx.Response(200, headers={"content-type": "audio/mpeg", "content-length": str(len(body))},
                          content=body)
    out = appmod.stream_tee_response(resp, "online:netease:1", None, resolved_ext="mp3",
                                     chunks=iter([body]), first_chunk=body)
    assert out.headers["content-type"] == "audio/flac"


def test_stream_tee_response_keeps_upstream_when_range_not_at_zero():
    """从中间开始的 Range 拿到的不是文件头，不能凭它改判容器。"""
    body = b"fLaC\x00\x00\x00\x22" + b"\x00" * 64
    resp = httpx.Response(206, headers={"content-type": "audio/mpeg",
                                        "content-length": str(len(body)),
                                        "content-range": f"bytes 4096-{4096 + len(body) - 1}/99999"},
                          content=body)
    out = appmod.stream_tee_response(resp, "online:netease:1", "bytes=4096-", resolved_ext="mp3",
                                     chunks=iter([body]), first_chunk=body)
    assert out.headers["content-type"] == "audio/mpeg"


def test_hls_segment_media_types():
    assert appmod._hls_segment_media(appmod.tc.INIT_NAME) == "audio/mp4"
    assert appmod._hls_segment_media("00000.m4s") == "audio/iso.segment"
    assert appmod._hls_segment_media("unknown.bin") == "application/octet-stream"


# ----------------------------------------------------------- CDN UA ------

def test_cdn_client_defaults_to_browser_ua():
    """kuwo 等中文 CDN 对 python-httpx 默认 UA 直接 403（实测），客户端必须预置浏览器 UA。"""
    client = appmod._new_cdn_client()
    assert client.headers.get("user-agent") == appmod.CDN_UA
    assert "Mozilla" in appmod.CDN_UA
    assert appmod.CDN_UA == appmod.tc.CDN_UA


class _FakeResp:
    status_code = 200
    headers = {"content-type": "audio/mp4", "content-encoding": "identity", "content-length": "4"}

    async def aiter_bytes(self):
        yield b"fLaC\x00\x00\x00\x22"

    async def aclose(self):
        pass


class _FakeCdn:
    def __init__(self):
        self.seen: list[dict] = []

    def build_request(self, method, url, headers=None):
        self.seen.append(dict(headers or {}))
        return object()

    async def send(self, req, stream=True):
        return _FakeResp()


def test_lx_stream_request_carries_browser_ua(monkeypatch):
    """lx 取流请求必须带浏览器 UA，否则 kuwo 全 403 → 客户端表现为自动下一曲。"""
    import asyncio
    from starlette.requests import Request

    fake = _FakeCdn()
    monkeypatch.setattr(appmod, "get_cdn_client", lambda _app: fake)
    async def fake_resolve(*_a, **_k):
        return {"url": "http://car-lv.kuwo.cn/x.m4a", "ext": "m4a"}

    monkeypatch.setattr(appmod, "resolve_lx_url", fake_resolve)
    monkeypatch.setattr(appmod, "_retained_track", lambda *a, **k: ({"ext": "m4a"}, None))

    async def main():
        req = Request({"type": "http", "app": appmod.app, "headers": [], "method": "GET",
                       "path": "/music/api/v1/track/stream", "query_string": b"",
                       "server": ("test", 1), "scheme": "http"})
        return await appmod._open_online_stream(req, "online:lx:kw:378292913", None)

    out = asyncio.run(main())
    assert out is not None
    assert fake.seen and fake.seen[0].get("User-Agent") == appmod.CDN_UA


def test_ffmpeg_argv_defaults_to_browser_ua():
    """转码输入也是抓远程直链：没有显式 UA 时必须回落到浏览器 UA。"""
    import types

    sess = types.SimpleNamespace(bitrate="320k", hls_time=10)
    argv = appmod.tc._ffmpeg_argv("http://car-lv.kuwo.cn/x.m4a", None, sess, "/tmp/hls")
    assert "-user_agent" in argv
    assert argv[argv.index("-user_agent") + 1] == appmod.tc.CDN_UA


def test_musicbox_client_carries_browser_ua():
    """收藏自动下载用 musicbox 客户端抓 CDN 直链，UA 也必须是浏览器 UA。

    用全新的 FastAPI 实例，避免别的用例把 app.state.musicbox_client 换成 MockTransport。
    """
    from fastapi import FastAPI

    client = appmod.get_musicbox_client(FastAPI())
    assert client.headers.get("user-agent") == appmod.CDN_UA
