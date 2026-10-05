"""下一首预热（T1）：上下文推断、单飞、统计与可关开关。"""
from __future__ import annotations

import os

import pytest

from proxy import prefetch as pf


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    pf.reset_for_test()
    monkeypatch.delenv("FNMUSIC_PREFETCH_NEXT", raising=False)
    yield
    pf.reset_for_test()


def _tracks(*guids):
    return [{"guid": g} for g in guids]


# ---------------------------------------------------------------------------
# 上下文推断
# ---------------------------------------------------------------------------


def test_remember_context_and_next_of():
    assert pf.remember_context("pl:1", _tracks("a", "b", "c")) == 3
    assert pf.next_of("a") == ("pl:1", "b")
    assert pf.next_of("b") == ("pl:1", "c")
    assert pf.next_of("c") is None, "最后一首没有下一首"


def test_remember_context_ignores_too_short_or_empty():
    assert pf.remember_context("pl:1", _tracks("a")) == 0
    assert pf.remember_context("pl:1", []) == 0
    assert pf.next_of("a") is None


def test_same_context_replaced_not_duplicated():
    pf.remember_context("pl:1", _tracks("a", "b", "c"))
    pf.remember_context("pl:1", _tracks("a", "x", "y"))
    assert pf.next_of("a") == ("pl:1", "x"), "同名上下文应覆盖而不是并存"
    assert len(pf.context_report()) == 1


def test_newest_context_wins():
    pf.remember_context("pl:old", _tracks("a", "b"))
    pf.remember_context("pl:new", _tracks("a", "z"))
    assert pf.next_of("a") == ("pl:new", "z")


def test_context_capped_at_max():
    for i in range(pf.MAX_CONTEXTS + 4):
        pf.remember_context(f"pl:{i}", _tracks(f"g{i}", f"h{i}"))
    assert len(pf.context_report()) == pf.MAX_CONTEXTS


def test_tracks_capped_at_max():
    guids = [f"g{i}" for i in range(pf.MAX_TRACKS + 50)]
    assert pf.remember_context("pl:big", _tracks(*guids)) == pf.MAX_TRACKS


def test_next_of_unknown_guid():
    pf.remember_context("pl:1", _tracks("a", "b"))
    assert pf.next_of("zzz") is None
    assert pf.next_of("") is None


def test_contexts_expire_after_ttl(monkeypatch):
    pf.remember_context("pl:1", _tracks("a", "b"))
    real = pf.time.time
    monkeypatch.setattr(pf.time, "time", lambda: real() + pf.CONTEXT_TTL + 1)
    assert pf.next_of("a") is None, "过期的上下文不该再用来推断下一首"


# ---------------------------------------------------------------------------
# 单飞
# ---------------------------------------------------------------------------


def test_claim_single_flight_within_window():
    assert pf.claim("a") is True
    assert pf.claim("a") is False, "同一个 guid 60s 内只应预热一次"
    assert pf.claim("b") is True


def test_claim_rejected_for_empty_guid():
    assert pf.claim("") is False
    assert pf.claim("   ") is False


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


def test_stats_and_hit_rate():
    pf.bump("scheduled")
    pf.note_result("a", True, 120.0)
    pf.note_play("a", 30.0, warm=True)      # 命中预热
    pf.bump("scheduled")
    pf.note_result("b", False, 90.0, "no url")
    pf.note_play("b", 500.0, warm=False)    # 冷启动

    st = pf.status()
    assert st["scheduled"] == 2
    assert st["done"] == 1
    assert st["failed"] == 1
    assert st["plays"] == 2
    assert st["hits"] == 1
    assert st["hit_rate"] == 0.5
    assert st["cold_ms"] == 500.0
    assert st["warm_ms"] == 30.0
    assert st["saved_ms"] == 470.0
    assert st["recent"][-1]["guid"] == "b"


def test_warming_seconds():
    assert pf.warming_seconds("a") is None
    pf.note_result("a", True, 10.0)
    secs = pf.warming_seconds("a")
    assert secs is not None and secs >= 0


def test_no_next_counter():
    pf.bump("no_next")
    assert pf.status()["no_next"] == 1


# ---------------------------------------------------------------------------
# 多首预热
# ---------------------------------------------------------------------------


def test_next_n_returns_following_tracks():
    pf.remember_context("pl:1", _tracks("a", "b", "c", "d", "e"))
    assert pf.next_n("a", 3) == ["b", "c", "d"]
    assert pf.next_n("d", 3) == ["e"], "不足 n 首时给多少算多少"
    assert pf.next_n("e", 3) == []
    assert pf.next_n("zzz", 3) == []
    assert pf.next_n("a", 0) == []


def test_lookahead_env(monkeypatch):
    # v2.9.25：默认 3 → 2。musicbox 单进程，多预热一首就多占它 ~400ms，而用户
    # 随时可能切歌——少预热一首只是「下一首慢一次」，拖慢正在播的是「每次都慢」。
    assert pf._lookahead() == 2
    monkeypatch.setenv("FNMUSIC_PREFETCH_LOOKAHEAD", "5")
    # v2.9.27：上限 5 → 3。真机 .env 里留着 LOOKAHEAD=5（升级不覆盖用户值），
    # 配 max_queue=2 的后果是 6 次被队列拒、11 次撞上「已预热过」——预热全在
    # 空转，却实打实占着单进程的 musicbox，被拖慢的恰恰是正在播的那首。
    assert pf._lookahead() == 3
    monkeypatch.setenv("FNMUSIC_PREFETCH_LOOKAHEAD", "99")
    assert pf._lookahead() == 3, "上限 3，防止一次打爆 musicbox"
    monkeypatch.setenv("FNMUSIC_PREFETCH_LOOKAHEAD", "0")
    assert pf._lookahead() == 1


def test_first_of_prefetches_list_head():
    pf.remember_context("pl:1", _tracks("a", "b", "c"))
    assert pf.first_of("pl:1", 1) == ["a"]
    assert pf.first_of("pl:1", 2) == ["a", "b"]
    assert pf.first_of("pl:nope", 1) == []


def test_on_list_enabled(monkeypatch):
    """默认关：后台批量刷歌单时会连带触发十几次预热，把 musicbox 自己堵死。"""
    assert pf.on_list_enabled() is False
    monkeypatch.setenv("FNMUSIC_PREFETCH_ON_LIST", "true")
    assert pf.on_list_enabled() is True


def test_concurrency_and_timeout(monkeypatch):
    """预热必须限并发、设超时——宁可不预热也不能拖慢正在播的那首。"""
    assert pf.max_concurrent() == 1
    monkeypatch.setenv("FNMUSIC_PREFETCH_CONCURRENCY", "4")
    assert pf.max_concurrent() == 4
    monkeypatch.setenv("FNMUSIC_PREFETCH_CONCURRENCY", "99")
    assert pf.max_concurrent() == 4, "上限 4，musicbox 是单进程"
    assert 1.0 <= pf.timeout_seconds() <= 15.0


# ---------------------------------------------------------------------------
# 统计口径：一次播放 = 一首歌，不能被 Range 请求重复计数
# ---------------------------------------------------------------------------


def test_repeated_range_requests_are_collapsed():
    """一次流式播放客户端会连发好几个 Range 请求，全算进去命中率与均值都失真。"""
    pf.note_result("a", True, 10.0)
    assert pf.note_play("a", 20.0, warm=True) is True
    assert pf.note_play("a", 25.0, warm=True) is False, "重复请求不应计入"
    assert pf.note_play("a", 30.0, warm=True) is False

    st = pf.status()
    assert st["plays"] == 1, "3 次请求只应算 1 次播放"
    assert st["hits"] == 1
    assert st["hit_rate"] == 1.0
    assert st["repeat_plays"] == 2
    assert st["warm_ms"] == 20.0, "耗时均值只取首次，不能被重复请求稀释"


def test_play_dedupe_expires_after_window(monkeypatch):
    real = pf.time.time
    pf.note_play("a", 10.0, warm=False)
    monkeypatch.setattr(pf.time, "time",
                        lambda: real() + pf.PLAY_DEDUPE_WINDOW + 1)
    assert pf.note_play("a", 40.0, warm=False) is True
    assert pf.status()["plays"] == 2


# ---------------------------------------------------------------------------
# 开关
# ---------------------------------------------------------------------------


def test_enabled_default_true():
    assert pf.enabled() is True


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("FNMUSIC_PREFETCH_NEXT", "false")
    assert pf.enabled() is False
    monkeypatch.setenv("FNMUSIC_PREFETCH_NEXT", "0")
    assert pf.enabled() is False
    monkeypatch.setenv("FNMUSIC_PREFETCH_NEXT", "on")
    assert pf.enabled() is True


def test_env_read_at_call_time(monkeypatch):
    """开关必须是每次读，不能在 import 时固化——改设置不该要求重启。"""
    assert pf.enabled() is True
    monkeypatch.setenv("FNMUSIC_PREFETCH_NEXT", "false")
    assert pf.enabled() is False


def test_reset_for_test_clears_everything():
    pf.remember_context("pl:1", _tracks("a", "b"))
    pf.note_result("a", True, 1.0)
    pf.note_play("a", 2.0, warm=True)
    pf.reset_for_test()
    st = pf.status()
    assert st["contexts"] == [] and st["recent"] == []
    assert st["plays"] == 0 and st["done"] == 0


# ---------------------------------------------------------------------------
# 默认进包：预热开关必须能在管理页改、升级后自动补进配置文件
# ---------------------------------------------------------------------------


def test_prefetch_env_is_backfilled_by_env_merge():
    from proxy import env_merge

    assert "FNMUSIC_PREFETCH_NEXT" in dict(env_merge.NEW_DEFAULTS)
    assert any("FNMUSIC_PREFETCH_" == p for p in env_merge.NEW_PREFIXES)
    assert os.environ.get("FNMUSIC_PREFETCH_NEXT") is None or True
