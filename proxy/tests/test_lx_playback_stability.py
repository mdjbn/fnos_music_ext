"""lx 播放稳定性：HEAD 探测不再因 4s 死线误判「不可播」，解析失败必须留痕。

用户报（2026-10-06）：「lx 音源同一首歌有事可以播放，有时直接下一曲了。」日志里失败的
HEAD 探测**恰好 4.0 秒**后才 404，成功的 0.1–0.9s。根因（诊断结论）：

1. `_stream_head_response` 给 `_open_online_stream` 只有 4s 死线，而 lxmusic 单档解析
   预算 12s、端点总预算 20s（野生用户源实测 3–16s）⇒ 超时一律 404，播放器直接下一曲；
   解析成功过一次被 lx 服务缓存（LX_CACHE_TTL=1800）后才秒开，所以同一首歌时好时坏。
2. 解析失败按音质档位重复计数，一首故障曲就能打开 600s 熔断 ⇒ 之后所有曲目 0.07s
   失败（成片跳曲）。
3. `resolve_lx_url` 只认 HTTP 200，非 200（404/502）与 `ok:false` 被静默跳过，journal
   里只剩播放器那句 404，看不到 `user source circuit open` / `播放地址解析失败`。
"""
from __future__ import annotations

import logging

import httpx
import pytest
from starlette.requests import Request

from proxy import app as appmod
from proxy.app import CONF


def _req(path: str = "/music/api/v1/track/stream") -> Request:
    return Request({"type": "http", "app": appmod.app, "headers": [], "method": "HEAD",
                    "path": path, "query_string": b"", "server": ("test", 1), "scheme": "http"})


def _lx_source(monkeypatch, *, lx: bool = True) -> None:
    monkeypatch.setitem(CONF, "lx_enabled", lx)
    monkeypatch.setitem(CONF, "lx_sources", [])


# ===========================================================================
# A：HEAD 未命中缓存时不再解析直链
# ===========================================================================


@pytest.mark.anyio
async def test_head_probe_does_not_resolve_online_source(monkeypatch):
    """HEAD 只做门控：不试开直链、不谎报长度，避免慢解析被 4s 死线判成 404。"""
    _lx_source(monkeypatch)
    called: list = []

    async def fake_open(*args, **kwargs):
        called.append(args)
        raise AssertionError("HEAD 不该解析直链")

    monkeypatch.setattr(appmod, "_open_online_stream", fake_open)
    resp = await appmod._stream_head_response(_req(), "online:lx:tx:002ON7z32288op", None, None)

    assert resp.status_code == 200
    assert not called
    assert resp.headers["accept-ranges"] == "bytes"
    assert "content-length" not in resp.headers


@pytest.mark.anyio
async def test_head_probe_cached_file_still_reports_real_size(tmp_path):
    """缓存命中照旧回真实大小（播放器拖动依赖 content-length）。"""
    payload = b"ID3\x03\x00" + b"\x00" * 64
    cached = tmp_path / "cached.mp3"
    cached.write_bytes(payload)

    resp = await appmod._stream_head_response(_req(), "online:lx:tx:002ON7z32288op", str(cached), None)

    assert resp.status_code == 200
    assert resp.headers["content-length"] == str(len(payload))


@pytest.mark.anyio
async def test_head_probe_still_gated_on_source(monkeypatch):
    """源未启用/平台被过滤：仍要立刻 404（这条廉价门控必须保留）。"""
    _lx_source(monkeypatch, lx=False)
    resp = await appmod._stream_head_response(_req(), "online:lx:tx:002ON7z32288op", None, None)
    assert resp.status_code == 404


# ===========================================================================
# C：解析失败原因留痕（并按 (song, 档位, 原因) 去重）
# ===========================================================================


def _proxy_messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "fnmusic_proxy"]


@pytest.mark.anyio
async def test_resolve_lx_url_logs_reason_and_dedupes(monkeypatch, caplog):
    """非 200 的 error 串必须进日志（熔断/无直链可辨），且 60s 内不重复刷屏。"""
    appmod._LX_FAIL_LOG.clear()
    monkeypatch.setitem(CONF, "lx_quality", "lossless")
    monkeypatch.setitem(CONF, "quality_mode", "high")
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(502, json={"ok": False, "error": "resolve failed: user source circuit open"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://lx.test")
    try:
        with caplog.at_level(logging.WARNING, logger="fnmusic_proxy"):
            assert await appmod.resolve_lx_url(client, "lx:tx:002ON7z32288op") is None
            first = _proxy_messages(caplog)
            assert await appmod.resolve_lx_url(client, "lx:tx:002ON7z32288op") is None
            second = _proxy_messages(caplog)
    finally:
        await client.aclose()

    assert hits["n"] == 6  # 两轮 × 三档（lossless/high/standard）
    assert any("user source circuit open" in m for m in first)
    assert len(first) == 4  # 三档各一条 + 一条汇总
    assert second == first  # 同一 (song, 档位, 原因) 在窗口内不重复


# ===========================================================================
# A 续：GET 侧单次解析预算对 lx 放宽到总预算（6s 掐不住用户源脚本）
# ===========================================================================


def test_online_resolve_budget_gives_lx_the_full_window():
    """lx 走用户源脚本（实测 3–16s），6s 单次预算会把「能放但慢」的曲子判死。"""
    assert appmod.online_resolve_budget("online:lx:tx:002ON7z32288op") == 12.0
    assert appmod.online_resolve_budget("online:lx:kw:228908") == 12.0
    # 网易云仍是 musicbox 的毫秒级取链预算，不放宽「真·不可播」的失败回传速度。
    assert appmod.online_resolve_budget("online:nm:123456") == 6.0
    assert appmod.online_resolve_budget("online:netease:1") == 6.0
    assert appmod.LX_RESOLVE_BUDGET_S <= 12.0  # 不得超过 GET 总预算
