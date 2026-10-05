"""播放提速三件事（移植 G v2.9.25，W4 接入 A）：连接复用 / 预热让位 / warm 口径。

真机诊断给出的两个反直觉数字，是这次改动的全部动机：

1. 「每次播放请求数 14.62」——一次播放客户端要发十几个 Range 请求，而 A 的
   `_open_online_stream` 每次都新建一个 httpx.AsyncClient，等于每个 Range 重新
   做一次 TCP + TLS 握手；
2. 「未预热 114.2ms → 命中预热 332ms」——预热反而慢 3 倍。这不是预热的错，是
   **统计口径把自己骗了**：warm 标的是「曾经预热过这首歌」（窗口写死 3600s），
   而预热成果的寿命是直链缓存 TTL（600s），过期的那些播放一次缓存都没命中、
   照样付全额往返，却被记进 warm，均值自然失真到给出反方向结论。

这三个测试锁住的是「别再退回去了」，尤其是第 2 条：口径一松，诊断页那行
「省下 X ms」就会重新变成指错方向的指针。

A 侧适配说明（与 G 的差异仅两点，都不改判定语义）：

* A 的直链短缓存只在调用方传 `stats` 时启用（G 是无条件启用）。A 既有的
  `test_netease` / `test_quality_policy` / `test_v2_features` 都在同一 client 上
  连续调用 `resolve_netease_url` 并期望每次都回源，A 的 conftest 又没有 G 那样的
  `_URL_CACHE` 清理钩子；把缓存做成「播放/预热链路显式 opt-in」后，那些用例的
  网络调用序列逐字节不变，而预热（带 stats）照样能把链灌进去。
* A 的 conftest 不清理 `_URL_CACHE`，所以本文件的 autouse 夹具自己清，避免与
  其它测试文件互相污染。
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from fastapi import FastAPI

import proxy.app as P
import proxy.prefetch as pf


@pytest.fixture(autouse=True)
def _reset_prefetch():
    pf.reset_for_test()
    P._URL_CACHE.clear()
    saved = dict(P._PREFETCH_TASKS)
    P._PREFETCH_TASKS.clear()
    yield
    pf.reset_for_test()
    P._URL_CACHE.clear()
    P._PREFETCH_TASKS.clear()
    P._PREFETCH_TASKS.update(saved)


# ---------------------------------------------------------------------------
# ① CDN 连接复用
# ---------------------------------------------------------------------------
def test_cdn_client_is_shared_not_per_request():
    """同一个 app 上多次取流必须复用同一个 client。

    每次新建 = 每个 Range 请求重新 TCP + TLS 握手，而实测一次播放有 14.62 个
    Range 请求——那是 14 次握手，跟音质、带宽都无关的纯浪费。
    """
    app = FastAPI()
    c1 = P.get_cdn_client(app)
    c2 = P.get_cdn_client(app)
    assert c1 is c2
    assert c1 is P.get_cdn_client(app)
    assert not c1.is_closed


def test_cdn_client_uses_a_bounded_pool():
    """共享 client 必须有连接池上限，否则并发播放会把连接数打到无限。"""
    limits = P._new_cdn_client()._transport._pool._max_connections  # type: ignore[attr-defined]
    assert limits is not None and limits >= 1


# ---------------------------------------------------------------------------
# ② 预热让位：总闸门
# ---------------------------------------------------------------------------
def test_max_queue_default_and_bounds(monkeypatch):
    monkeypatch.delenv("FNMUSIC_PREFETCH_MAX_QUEUE", raising=False)
    assert pf.max_queue() == 2
    monkeypatch.setenv("FNMUSIC_PREFETCH_MAX_QUEUE", "99")
    assert pf.max_queue() == 10, "上限 10，别把 musicbox 打爆"
    monkeypatch.setenv("FNMUSIC_PREFETCH_MAX_QUEUE", "0")
    assert pf.max_queue() == 1


def test_queue_full_drops_new_prefetch(monkeypatch):
    """队列满了就放弃新预热：预热是锦上添花，跟正在播的冲突时必须让位。"""
    monkeypatch.setenv("FNMUSIC_PREFETCH_MAX_QUEUE", "2")
    P._PREFETCH_TASKS["a"] = object()
    P._PREFETCH_TASKS["b"] = object()
    assert P._prefetch_if_needed(None, "ctx", "online:netease:99") == 0
    assert float(pf._STATS.get("queued_out") or 0) == 1
    assert "online:netease:99" not in P._PREFETCH_TASKS, "不该为它建任务"


def test_queue_below_cap_still_schedules(monkeypatch):
    monkeypatch.setenv("FNMUSIC_PREFETCH_MAX_QUEUE", "2")
    P._PREFETCH_TASKS["a"] = object()
    guid = "online:netease:98"

    async def _run() -> int:
        got = P._prefetch_if_needed(None, "ctx", guid)
        task = P._PREFETCH_TASKS.pop(guid, None)
        if hasattr(task, "cancel"):      # 别让它真去连 musicbox
            task.cancel()
        return got

    assert asyncio.run(_run()) == 1
    assert float(pf._STATS.get("scheduled") or 0) == 1
    assert float(pf._STATS.get("queued_out") or 0) == 0


# ---------------------------------------------------------------------------
# ③ warm 口径
# ---------------------------------------------------------------------------
def test_warm_ttl_follows_url_cache_not_a_magic_number(monkeypatch):
    """预热成果的有效期必须跟着直链缓存 TTL 走。

    以前写死 3600s 而缓存只有 600s——标记比成果长寿 6 倍，warm 就失去了意义。
    """
    monkeypatch.delenv("FNMUSIC_URL_CACHE_TTL", raising=False)
    assert pf.warm_ttl_seconds() == 600.0
    monkeypatch.setenv("FNMUSIC_URL_CACHE_TTL", "120")
    assert pf.warm_ttl_seconds() == 120.0
    monkeypatch.setenv("FNMUSIC_URL_CACHE_TTL", "99999")
    assert pf.warm_ttl_seconds() == 3600.0, "封顶 1 小时，别无限期认账"


def test_expired_prefetch_is_no_longer_warm(monkeypatch):
    """30 分钟前预热的歌，直链缓存早过期了，不能再算 warm。"""
    monkeypatch.setenv("FNMUSIC_URL_CACHE_TTL", "600")
    pf._WARMED["g1"] = time.time() - 1800
    assert pf.warming_seconds("g1") is None
    pf._WARMED["g1"] = time.time() - 30
    assert pf.warming_seconds("g1") is not None


def test_stats_flag_only_set_on_real_cache_hit():
    """`url_cache_hit` 必须只在真的省掉往返时为真——这是 warm 判定的唯一依据。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.params.get("quality")))
        return httpx.Response(
            200, json={"ok": True, "data": {"code": 200, "url": "http://a.test/x.mp3"}}
        )

    async def _run() -> tuple[dict, dict]:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                   base_url="http://127.0.0.1:8770")
        s1: dict = {}
        await P.resolve_netease_url(client, "777001", None, stats=s1)
        s2: dict = {}
        await P.resolve_netease_url(client, "777001", None, stats=s2)
        await client.aclose()
        return s1, s2

    s1, s2 = asyncio.run(_run())
    assert bool(calls), "第一次不该命中缓存（否则这个测试没测到东西）"
    assert not s1.get("url_cache_hit"), "第一次走了真实往返，不能算命中"
    assert s2.get("url_cache_hit") is True, "第二次命中短缓存，零往返"
    assert len(calls) == 1, f"第二次不该再打 musicbox，实际打了 {len(calls)} 次"


def test_note_play_counts_warming_separately():
    """撞上「预热正在跑」的播放单列：它归 cold，但成因不同，混进去看不出来。"""
    pf.note_play("g1", 100.0, False, warming=True)
    assert float(pf._STATS.get("warming_hits") or 0) == 1
    assert int(pf._STATS.get("hits") or 0) == 0, "warming 不是命中，别混进命中率"
    assert int(pf._STATS.get("cold_ms_n") or 0) == 1, "它确实没省时间，算 cold"


def test_note_play_warm_is_what_the_caller_measured():
    """warm 由调用方按实测传入，不再由「曾经预热过」推断。"""
    pf.note_play("g1", 20.0, True)
    pf.note_play("g2", 300.0, False)
    assert int(pf._STATS.get("hits") or 0) == 1
    assert pf._STATS["warm_ms_sum"] == 20.0
    assert pf._STATS["cold_ms_sum"] == 300.0


def test_status_exposes_the_new_counters():
    st = pf.status()
    for key in ("queued_out", "warming_hits", "max_queue", "warm_ttl_s"):
        assert key in st, f"诊断页要读 {key}"
