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
