"""下一首预热（T1）：播放当前曲目时，把「下一首」的直链与元数据提前取回来。

## 为什么需要它

真机实测（`proxy.log`）：

```
play-start netease online:netease:96100 519ms
```

一首没缓存的在线歌，从点击到出声要 519ms。这段时间的绝大部分花在
``asyncio.gather(resolve_netease_url, _online_info)``——两个 musicbox 往返
（`/song/{id}/url`、`/song/{id}/info` + `/lyric`），起步等的是两者的较慢者。

而客户端**完全不做预取**：播放开始后的十几秒里只请求 metadata / 封面 /
heartbeat / 上报，没有任何对下一首的动作（2026-09-14 真机日志实锤）。
所以要提速只能由代理主动做。

## 「下一首是谁」——没有上下文参数，只能推断

```
GET /music/api/v1/track/stream?guid=online%3Anetease%3A96100
```

请求里只有 guid，没有 playlistId / context。但**列表是我们下发的**：客户端播
之前必然拉过歌单曲目（`/track/playlist-detail/list`），那份列表的顺序我们
知道。于是：

* 把我们下发过的有序列表记进「最近上下文」环形缓冲；
* stream 到达时，用当前 guid 在最近上下文里定位 → 后一个就是下一首。

随机播放时这个推断会错——所以本模块只做**零音频流量**的预热（几个 KB 的
JSON），猜错不产生任何实质代价；整首预下载（T2）必须先解决这个不确定性。

## 设计约束

* 永不阻塞当前播放：全是后台任务，失败静默只记日志；
* 永不重复：按 guid 单飞（inflight 去重），已预热过的不再重复；
* 可关：`FNMUSIC_PREFETCH_NEXT=false`；
* 可证：统计调度/完成/失败次数与「预热命中率」，诊断页直接给出。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger("fnmusic_proxy")

# 记住多少个「最近下发过的列表」。多于这个数的最久未用者被丢弃。
MAX_CONTEXTS = 8
# 单个列表最多记多少首（防止超大歌单把内存撑爆）
MAX_TRACKS = 3000
# 上下文多久算过期（秒）。太久没播放过说明用户早就不在这个列表里了。
CONTEXT_TTL = 3 * 3600.0

def _lookahead() -> int:
    """一次预热几首（含紧邻的下一首）。

    v2.9.25：默认 3 → 2。musicbox 是单进程，一首预热 ~400ms，多预热一首就多占
    它 400ms；而用户随时可能切歌，那首歌的 gather 得跟这些预热排队。少预热一首
    只是「下一首慢一次」，把正在播的拖慢是「每次都慢」——不划算。

    v2.9.27：上限 5 → 3。真机出现过 LOOKAHEAD=5 配 max_queue=2 的组合（.env 里
    的老值升级不覆盖）：一次想预热 5 首、队列只放得下 2 个，结果 6 次被队列拒、
    11 次撞上「已预热过」，预热全在空转，却实打实地占着 musicbox——被拖慢的正
    是当前这首。预热是纯优化，砍到 3 首不会让任何功能退化。
    """
    try:
        return max(1, min(3, int(float(os.environ.get("FNMUSIC_PREFETCH_LOOKAHEAD", "2") or 2))))
    except (TypeError, ValueError):
        return 2


def lookahead_requested() -> int:
    """用户在 .env 里实际写了多少（未经收敛）。诊断页用来提示「已收敛」。"""
    try:
        return int(float(os.environ.get("FNMUSIC_PREFETCH_LOOKAHEAD", "2") or 2))
    except (TypeError, ValueError):
        return 2


def max_queue() -> int:
    """同时在飞的预热任务硬上限（v2.9.25）。

    `_lookahead` 只是一首歌「想」预热几首，这个才是总闸门：用户连续切歌时**每
    一首**都会追加 N 个预热，队列越堆越长，musicbox 消化不过来，最后被拖慢的
    恰恰是「正在播的那一首」——真机「未预热 114ms → 命中预热 332ms」的反常数据
    就是这么来的。

    到上限就放弃新预热：预热是锦上添花，播放是正事，两者冲突时让位给播放。
    """
    try:
        return max(1, min(10, int(float(os.environ.get("FNMUSIC_PREFETCH_MAX_QUEUE", "2") or 2))))
    except (TypeError, ValueError):
        return 2


def on_list_enabled() -> bool:
    """歌单列表一下发就预热它的前几首（用户点开时第一首已经热了）。

    **默认关**（v2.9.16）：后台批量刷新歌单时（十几个歌单一起拉 tracks）会
    顺带触发十几次预热，请求全堆到 musicbox 上——真机实测预热耗时从 400ms
    涨到 1100ms，等于自己把自己堵死。只在用户真的点开歌单时预热才有意义。
    """
    return str(os.environ.get("FNMUSIC_PREFETCH_ON_LIST", "false") or "false") \
        .strip().lower() in ("true", "1", "yes", "on")


def max_concurrent() -> int:
    """同时最多几个预热在跑。真机 musicbox 是单进程，并发多了全变慢。"""
    try:
        return max(1, min(4, int(float(os.environ.get("FNMUSIC_PREFETCH_CONCURRENCY", "1") or 1))))
    except (TypeError, ValueError):
        return 1


def timeout_seconds() -> float:
    """单次预热最多等多久。超时就放弃（不重试），绝不能拖住别的请求。"""
    try:
        return max(1.0, min(15.0, float(os.environ.get("FNMUSIC_PREFETCH_TIMEOUT", "6") or 6)))
    except (TypeError, ValueError):
        return 6.0


# 同一个 guid 的重复 Range 请求多久内只算「一次播放」。
# 一次流式播放客户端会发好几个 Range 请求，不去重的话命中率与冷/热耗时
# 全被重复请求污染（真机 3 分钟里「78 次播放」其实是十几首歌）。
PLAY_DEDUPE_WINDOW = 600.0

_CONTEXTS: list[dict] = []
_INFLIGHT: dict[str, float] = {}
# guid -> 预热完成时间戳。用于判断「这次播放的直链是不是我们提前取回来的」
_WARMED: dict[str, float] = {}
# guid -> 首次播放时间戳（用于把重复 Range 请求折叠成一次播放）
_PLAY_SEEN: dict[str, float] = {}
_RECENT: list[dict] = []
_RECENT_MAX = 20

_STATS: dict[str, float] = {
    "scheduled": 0,     # 调度次数
    "done": 0,          # 预热成功
    "failed": 0,        # 预热失败（静默，仅计数）
    "no_next": 0,       # 推断不出下一首（没有上下文 / 已是最后一首）
    "already": 0,       # 已经预热过或正在预热，跳过
    "hits": 0,          # 播放时命中预热成果
    "plays": 0,         # 在线播放总次数（已折叠重复 Range 请求）
    "repeat_plays": 0,  # 同一首歌的重复 Range 请求（不计入冷/热对比，否则均值失真）
    "cold_ms_sum": 0.0,  # 冷启动（未预热）gather 耗时累计
    "cold_ms_n": 0,
    "warm_ms_sum": 0.0,  # 命中预热后的 gather 耗时累计
    "warm_ms_n": 0,
    # v2.9.25
    "warming_hits": 0,   # 播放撞上「预热正在进行中」（可能反而更慢，单独记）
    "queued_out": 0,     # 预热队列已满，主动放弃的调度次数
}


def enabled() -> bool:
    return str(os.environ.get("FNMUSIC_PREFETCH_NEXT", "true") or "true") \
        .strip().lower() in ("true", "1", "yes", "on")


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------

def remember_context(context_guid: str, tracks: list[Any]) -> int:
    """记下我们下发过的一份有序曲目列表。返回记下的曲目数。"""
    guids: list[str] = []
    for t in tracks or []:
        if isinstance(t, dict):
            g = str(t.get("guid") or "").strip()
        else:
            g = str(t or "").strip()
        if g and g not in guids:
            guids.append(g)
        if len(guids) >= MAX_TRACKS:
            break
    if len(guids) < 2:
        return 0
    _CONTEXTS[:] = [c for c in _CONTEXTS if str(c.get("ctx") or "") != str(context_guid or "")]
    _CONTEXTS.append({"ts": time.time(), "ctx": str(context_guid or ""), "tracks": guids})
    if len(_CONTEXTS) > MAX_CONTEXTS:
        del _CONTEXTS[:-MAX_CONTEXTS]
    return len(guids)


def _prune() -> None:
    now = time.time()
    _CONTEXTS[:] = [c for c in _CONTEXTS
                    if (now - float(c.get("ts") or 0.0)) < CONTEXT_TTL]
    _CONTEXTS.sort(key=lambda c: float(c.get("ts") or 0.0))


def next_of(guid: str) -> tuple[str, str] | None:
    """当前 guid 的下一首。返回 (上下文 guid, 下一首 guid)。"""
    g = str(guid or "").strip()
    if not g:
        return None
    _prune()
    # 最近用过的上下文优先：用户很可能刚从这个列表里开始播
    for c in reversed(_CONTEXTS):
        tracks = list(c.get("tracks") or [])
        try:
            i = tracks.index(g)
        except ValueError:
            continue
        if i + 1 < len(tracks):
            return str(c.get("ctx") or ""), str(tracks[i + 1])
        return None       # 已是最后一首，没有下一首
    return None


def next_n(guid: str, n: int = 1) -> list[str]:
    """当前 guid 之后的 n 首（按上下文顺序）。不足 n 首就给多少算多少。"""
    g = str(guid or "").strip()
    if not g or n <= 0:
        return []
    _prune()
    for c in reversed(_CONTEXTS):
        tracks = list(c.get("tracks") or [])
        try:
            i = tracks.index(g)
        except ValueError:
            continue
        return [str(x) for x in tracks[i + 1: i + 1 + n]]
    return []


def first_of(context_guid: str, n: int = 1) -> list[str]:
    """某个上下文（歌单）的头 n 首。用于「列表一下发就预热」。"""
    cg = str(context_guid or "").strip()
    if not cg or n <= 0:
        return []
    _prune()
    for c in reversed(_CONTEXTS):
        if str(c.get("ctx") or "") != cg:
            continue
        return [str(x) for x in list(c.get("tracks") or [])[:n]]
    return []


def context_report() -> list[dict]:
    _prune()
    return [{"ctx": str(c.get("ctx") or ""), "tracks": len(c.get("tracks") or []),
             "age_s": int(time.time() - float(c.get("ts") or 0.0))} for c in _CONTEXTS]


# ---------------------------------------------------------------------------
# 单飞与统计
# ---------------------------------------------------------------------------

def claim(guid: str) -> bool:
    """抢占预热资格（同一个 guid 同时只跑一个）。"""
    g = str(guid or "").strip()
    if not g:
        return False
    now = time.time()
    last = float(_INFLIGHT.get(g) or 0.0)
    if last and (now - last) < 60.0:
        return False
    _INFLIGHT[g] = now
    if len(_INFLIGHT) > 200:
        for k in sorted(_INFLIGHT, key=lambda kk: _INFLIGHT[kk])[:100]:
            _INFLIGHT.pop(k, None)
    return True


def warm_ttl_seconds() -> float:
    """预热成果的有效期（秒）——必须与直链缓存 TTL 对齐，不能自己定一个数。

    以前这里写死 3600s，而直链缓存只有 600s（FNMUSIC_URL_CACHE_TTL）。于是
    「20 分钟前预热过」的歌在诊断里**仍被标成 warm**，可那次播放一次缓存都没
    命中、照样付了全额 musicbox 往返。标记的窗口比成果的窗口长 6 倍，warm 这个
    字就彻底没了意义——真机据此算出「未预热 114ms → 命中预热 332ms」，读起来像
    预热在帮倒忙，实际是口径把自己骗了。
    """
    try:
        ttl = float(os.environ.get("FNMUSIC_URL_CACHE_TTL", "600") or 600)
    except (TypeError, ValueError):
        ttl = 600.0
    if ttl <= 0:      # 0 = 关闭直链缓存，那预热成果也活不过当次
        ttl = 600.0
    return min(3600.0, ttl)


def warming_seconds(guid: str) -> "float | None":
    """这个 guid 的直链是不是我们提前取回来的？是则返回预热至今的秒数。"""
    ts = float(_WARMED.get(str(guid or "").strip()) or 0.0)
    if ts <= 0:
        return None
    age = time.time() - ts
    return age if 0 <= age < warm_ttl_seconds() else None


def note_result(guid: str, ok: bool, ms: float, detail: str = "") -> None:
    if ok:
        _STATS["done"] += 1
        _WARMED[str(guid or "").strip()] = time.time()
    else:
        _STATS["failed"] += 1
    _RECENT.append({"ts": time.time(), "guid": str(guid or ""), "ok": bool(ok),
                    "ms": round(float(ms), 1), "detail": detail[:120]})
    del _RECENT[:-_RECENT_MAX]
    logger.info("prefetch %s %s %.0fms %s", "ok" if ok else "fail", guid, ms, detail)


def note_play(guid: str, gather_ms: float, warm: bool, warming: bool = False) -> bool:
    """一次在线播放的 gather 耗时记账：吃到预热成果的走 warm，否则走 cold。

    返回是否计入统计——**同一首歌的重复 Range 请求会被折叠掉**。一次流式播放
    客户端会连发好几个 Range，全算进去的话「播放次数」是「曲目数」的好几倍，
    命中率与冷/热均值也就都失真了（真机 3 分钟 78 次「播放」其实是十几首歌）。

    v2.9.25：`warm` 的含义收紧为「**这次真的省掉了 musicbox 往返**」（由调用方
    按直链缓存是否命中传入），不再是「曾经预热过这首歌」——后者会把「预热成果早
    过期了」的播放也算成 warm，均值自然失真。`warming` 单独记「撞上预热正在进
    行中」：那种情况不但没省，还可能因为跟预热抢 musicbox 而更慢，混进 cold 会
    低估预热的副作用，所以另开一列。
    """
    g = str(guid or "").strip()
    now = time.time()
    last = float(_PLAY_SEEN.get(g) or 0.0)
    if last and (now - last) < PLAY_DEDUPE_WINDOW:
        _STATS["repeat_plays"] += 1
        return False
    _PLAY_SEEN[g] = now
    if len(_PLAY_SEEN) > 500:
        for k in sorted(_PLAY_SEEN, key=lambda kk: _PLAY_SEEN[kk])[:200]:
            _PLAY_SEEN.pop(k, None)

    _STATS["plays"] += 1
    if warming:
        _STATS["warming_hits"] += 1
    if warm:
        _STATS["hits"] += 1
        _STATS["warm_ms_sum"] += max(0.0, float(gather_ms))
        _STATS["warm_ms_n"] += 1
    else:
        _STATS["cold_ms_sum"] += max(0.0, float(gather_ms))
        _STATS["cold_ms_n"] += 1
    if warm:
        logger.info("prefetch hit: %s（预热于 %.1fs 前，本次 gather %.0fms）",
                    guid, float(warming_seconds(guid) or 0.0), gather_ms)
    return True


def bump(key: str, n: int = 1) -> None:
    _STATS[key] = float(_STATS.get(key, 0)) + n


def status() -> dict:
    def _avg(s: str, n: str) -> float:
        cnt = float(_STATS.get(n) or 0)
        return round(float(_STATS.get(s) or 0.0) / cnt, 1) if cnt else 0.0

    cold = _avg("cold_ms_sum", "cold_ms_n")
    warm = _avg("warm_ms_sum", "warm_ms_n")
    return {
        "enabled": enabled(),
        "lookahead": _lookahead(),
        "lookahead_requested": lookahead_requested(),
        "on_list": on_list_enabled(),
        "scheduled": int(_STATS.get("scheduled") or 0),
        "done": int(_STATS.get("done") or 0),
        "failed": int(_STATS.get("failed") or 0),
        "no_next": int(_STATS.get("no_next") or 0),
        "already": int(_STATS.get("already") or 0),
        "queued_out": int(_STATS.get("queued_out") or 0),
        "max_queue": max_queue(),
        "plays": int(_STATS.get("plays") or 0),
        "hits": int(_STATS.get("hits") or 0),
        "repeat_plays": int(_STATS.get("repeat_plays") or 0),
        # 撞上「预热正在跑」的播放次数：它归在 cold 里，但成因与普通的没预热不同
        "warming_hits": int(_STATS.get("warming_hits") or 0),
        "warm_ttl_s": round(warm_ttl_seconds(), 1),
        "hit_rate": (round(float(_STATS.get("hits") or 0) / float(_STATS["plays"]), 3)
                     if _STATS.get("plays") else 0.0),
        "cold_ms": cold,
        "warm_ms": warm,
        "saved_ms": round(max(0.0, cold - warm), 1) if (cold and _STATS.get("warm_ms_n")) else 0.0,
        "contexts": context_report(),
        "recent": [dict(r) for r in _RECENT[-10:]],
    }


def reset_for_test() -> None:
    _CONTEXTS.clear()
    _INFLIGHT.clear()
    _WARMED.clear()
    _PLAY_SEEN.clear()
    _RECENT.clear()
    for k in _STATS:
        _STATS[k] = 0.0
