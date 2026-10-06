"""fnmusic-ext 拦截代理 (FastAPI + httpx).

功能：
1. 通用透传：所有非拦截路径原样转发到 trim-music unix socket
2. 搜索合并：GET /music/api/v1/search/track* （兼容 q/keyword，并行 musicdl）
3. 在线播放：stream + HLS 兜底 + transcode 空操作 + tee 缓存回放（音频与歌词 sidecar）
4. 在线元数据/歌词/封面
5. GET /_ext/healthz
"""
from __future__ import annotations

import asyncio
import glob
import hashlib
import math
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Any, AsyncGenerator, Callable, Coroutine
from urllib.parse import quote
from uuid import uuid4

import httpx
import anyio
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

try:
    from . import recommend as dailyrec
    from . import nmplaylists as nmpl
    from . import transcode as tc
    from . import quality
    from . import local_library
    from . import prefetch
    from . import netease_auth
    from . import playlists
    from . import netease_items
    from . import trimgw
    from . import pushplus
    from . import download as downloader
    from . import lxsync
    from .cache_gc import purge_rolling, sweep_orphan_lyrics
    from .env_merge import parse_env_file
    from .version import get_version
except ImportError:  # uvicorn --app-dir proxy
    import recommend as dailyrec  # type: ignore
    import nmplaylists as nmpl  # type: ignore
    import transcode as tc  # type: ignore
    import quality  # type: ignore
    import local_library  # type: ignore
    import prefetch  # type: ignore
    import netease_auth  # type: ignore
    import playlists  # type: ignore
    import netease_items  # type: ignore
    import trimgw  # type: ignore
    import pushplus  # type: ignore
    import download as downloader  # type: ignore
    import lxsync  # type: ignore
    from cache_gc import purge_rolling, sweep_orphan_lyrics  # type: ignore
    from env_merge import parse_env_file  # type: ignore
    from version import get_version  # type: ignore

logger = logging.getLogger("fnmusic_proxy")


def _env_float(name: str, default: float) -> float:
    """W6：读浮点环境变量（G 侧 `_float` 的等价实现，A 原本没有）。"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_HOME = dailyrec.home_dir()

# lx 平台别名 → 规范代码（与 lxmusic-service/app.py 的 _SOURCE_ALIASES 保持一致）
_LX_ALIASES = {
    "kg": "kg",
    "kugou": "kg",
    "wy": "wy",
    "netease": "wy",
    "163": "wy",
    "mg": "mg",
    "migu": "mg",
    "tx": "tx",
    "qq": "tx",
    "tencent": "tx",
    "kw": "kw",
    "kuwo": "kw",
}


def _normalize_lx_sources(raw: str) -> list[str]:
    """LX_SOURCES 环境变量 → 规范平台代码列表；空 = 不限制（跟随 lx 服务配置）。"""
    out: list[str] = []
    for part in (raw or "").split(","):
        code = _LX_ALIASES.get(part.strip().lower(), "")
        if code and code not in out:
            out.append(code)
    return out


CONF = {
    "musicdl_url": os.environ.get("FNMUSIC_MUSICDL_URL", "http://127.0.0.1:8768"),
    "musicbox_url": os.environ.get("FNMUSIC_MUSICBOX_URL", "http://127.0.0.1:8770"),
    "lx_url": os.environ.get("FNMUSIC_LX_URL", "http://127.0.0.1:8772"),
    "musicdl_enabled": os.environ.get("FNMUSIC_MUSICDL_ENABLED", "false").lower() in ("true", "1", "yes"),
    "netease_enabled": os.environ.get("FNMUSIC_NETEASE_ENABLED", "false").lower() in ("true", "1", "yes"),
    "lx_enabled": os.environ.get("FNMUSIC_LX_ENABLED", "false").lower() in ("true", "1", "yes"),
    "lx_search_limit": int(os.environ.get("FNMUSIC_LX_SEARCH_LIMIT", "20")),
    "lx_quality": os.environ.get("FNMUSIC_LX_QUALITY", "lossless"),
    "netease_wait_s": float(os.environ.get("FNMUSIC_NETEASE_WAIT_S", "3.0")),
    # 输入停满这么久才向音源发起搜索。窗口内的新词会替换旧词并重新计时。
    "search_debounce_s": float(os.environ.get("FNMUSIC_SEARCH_DEBOUNCE_S", "1.0")),
    "netease_quality": os.environ.get("FNMUSIC_NETEASE_QUALITY", "lossless"),
    "netease_search_limit": int(os.environ.get("FNMUSIC_NETEASE_SEARCH_LIMIT", "50")),
    "upstream_sock": os.environ.get("FNMUSIC_UPSTREAM_SOCK", "/var/run/trim_music_upstream.socket"),
    "online_limit": int(os.environ.get("FNMUSIC_ONLINE_LIMIT", "30")),
    "search_list_path": os.environ.get("FNMUSIC_SEARCH_LIST_PATH", "data.list"),
    "cache_dir": os.environ.get("FNMUSIC_CACHE_DIR", os.path.join(_HOME, "cache")),
    # 空=从飞牛 shared_library.path 自动探测；测试可覆盖到临时目录
    "library_dir": os.environ.get("FNMUSIC_LIBRARY_DIR", ""),
    "music_db": os.environ.get(
        "FNMUSIC_MUSIC_DB", "/usr/local/apps/@appdata/trim.music/db/music.db"
    ),
    # 边听边存：默认开；保存路径空=自动探测飞牛共享曲库，不可用自动回退；
    # tee_cache_max 仅在关闭边听边存时生效（滚动保留最新 N 首试听缓存）
    "tee_save_enabled": os.environ.get("FNMUSIC_TEE_SAVE_ENABLED", "true").lower() in ("true", "1", "yes"),
    "tee_save_dir": os.environ.get("FNMUSIC_TEE_SAVE_DIR", ""),
    "tee_cache_max": int(os.environ.get("FNMUSIC_TEE_CACHE_MAX", "2")),
    # 收藏/加入歌单自动绑定本地：默认关；在线歌曲收藏后后台自动整轨下载并绑定本地文件，
    # 官方曲库收录后再把收藏/歌单写进官方（成功后删本地映射，避免双列表重复显示）
    "fav_auto_bind": os.environ.get("FNMUSIC_FAV_AUTO_BIND", "false").lower() in ("true", "1", "yes"),
    # 官方绑定轮询窗口（秒）：下载落库后等待官方扫入库并完成官方写入的时间上限
    "official_bind_timeout_s": float(os.environ.get("FNMUSIC_OFFICIAL_BIND_TIMEOUT_S", "120")),
    # 边听边存切歌续传：允许并行完成的续传任务上限（防快速跳歌时的下载洪泛；0=关闭续传）
    "tee_handoff_max": int(os.environ.get("FNMUSIC_TEE_HANDOFF_MAX", "3")),
    # 自动下载封面：自动下载的音乐完整落库后，把源站封面内嵌进音频文件。
    # 飞牛扫描器只认内嵌图（dhowden/tag，无外置 cover.jpg 能力），这是官方
    # App 显示下载歌曲封面的唯一途径；封面在音乐落库成功后才处理，不存在
    # 音乐失败、封面先落污染目录的情况
    "auto_cover": os.environ.get("FNMUSIC_AUTO_COVER", "true").lower() in ("true", "1", "yes"),
    # 自动下载歌词（2.6.0）：默认关；仅边听边存开启时生效——音乐完整落库
    # 成功后才下载同名 .lrc 到歌曲所在目录（飞牛扫描入库即带歌词），音乐
    # 下载失败不产生任何歌词文件，不存在歌词先落污染目录的情况
    "lyric_auto_dl": os.environ.get("FNMUSIC_LYRIC_AUTO_DL", "false").lower() in ("true", "1", "yes"),
    # 在线取流 Range 探针：记录每条在线 /track/stream 的 Range 形态与落盘资格，
    # 用于真机确认手机播放器是否按定长窗口取流（那样边听边存永不触发）
    "stream_probe": os.environ.get("FNMUSIC_STREAM_PROBE", "true").lower() in ("true", "1", "yes"),
    # 标准音质转码（App 音质偏好=标准时走 HLS 转码而非直拉原文件）：
    # ffmpeg 输出 AAC fMP4 分片；宿主机无 ffmpeg 自动回落单分片桩
    "transcode_enabled": os.environ.get("FNMUSIC_TRANSCODE_ENABLED", "true").lower() in ("true", "1", "yes"),
    "transcode_bitrate": os.environ.get("FNMUSIC_TRANSCODE_BITRATE", "128k"),
    "transcode_hls_time": float(os.environ.get("FNMUSIC_TRANSCODE_HLS_TIME", "10")),
    "transcode_max_sessions": int(os.environ.get("FNMUSIC_TRANSCODE_MAX_SESSIONS", "2")),
    "transcode_ttl_s": float(os.environ.get("FNMUSIC_TRANSCODE_TTL_S", "90")),
    "transcode_cache_max_mb": int(os.environ.get("FNMUSIC_TRANSCODE_CACHE_MAX_MB", "512")),
    # 下载"标准"档目标码率：官方实测 MP3 320k（prepare quality=standard, FLAC 源 → 320067bps）
    "transcode_dl_bitrate": os.environ.get("FNMUSIC_TRANSCODE_DL_BITRATE", "320k"),
    # 落盘进曲库后通知官方重扫的接口路径（POST）；空=禁用。官方无公开文档，
    # 真机在官方 App 手动点一次扫描、从代理请求日志捕获真实路径后填入启用
    "library_scan_path": (os.environ.get("FNMUSIC_LIBRARY_SCAN_PATH", "") or "").strip(),
    "merge_suggest": os.environ.get("FNMUSIC_MERGE_SUGGEST", "false").lower() in ("true", "1", "yes"),
    "online_sources": os.environ.get("FNMUSIC_ONLINE_SOURCES", "KuwoMusicClient,MiguMusicClient"),
    # lx 平台白名单（同 .env 的 LX_SOURCES；install.sh --sources lx-<平台> 写入）；
    # 空 = 不限制。GUID 第 3 段携带平台（online:lx:kg:xxx），据此过滤与透传 ?sources=
    "lx_sources": _normalize_lx_sources(os.environ.get("LX_SOURCES", "")),
    "lyric_field": os.environ.get("FNMUSIC_LYRIC_FIELD", "data.lyric"),
    "search_timeout": float(os.environ.get("FNMUSIC_SEARCH_TIMEOUT", "15")),
    "search_probe": os.environ.get("FNMUSIC_SEARCH_PROBE", "false").lower() in ("true", "1", "yes"),
    "search_cache_ttl": float(os.environ.get("FNMUSIC_SEARCH_CACHE_TTL", "604800")),
    "late_page_wait_s": float(os.environ.get("FNMUSIC_LATE_PAGE_WAIT_S", "5.0")),
    "fav_dir": os.environ.get(
        "FNMUSIC_FAV_DIR", os.path.join(_HOME, "online_favorites")
    ),
    # 官方歌单内在线附加条目的存储目录（同 fav_dir 按用户分文件）
    "plt_dir": os.environ.get(
        "FNMUSIC_PLT_DIR", os.path.join(_HOME, "playlist_tracks")
    ),
    "llm_base_url": (os.environ.get("FNMUSIC_LLM_BASE_URL") or "").strip().rstrip("/"),
    "llm_model": (os.environ.get("FNMUSIC_LLM_MODEL") or "gpt-4o-mini").strip() or "gpt-4o-mini",
    # v2.0.0：音质模式 high|balanced|smooth（档序见 quality_order）；
    # 推荐双开关 / 封面补全 / .env 热重载默认开，均可被 .env 覆盖
    "quality_mode": (os.environ.get("FNMUSIC_QUALITY_MODE") or "high").strip().lower(),
    "recommend_hot": os.environ.get("FNMUSIC_RECOMMEND_HOT", "true").lower() in ("true", "1", "yes"),
    "recommend_daily": os.environ.get("FNMUSIC_RECOMMEND_DAILY", "true").lower() in ("true", "1", "yes"),
    # 网易账号歌单注入（2.6.0）：默认关；需网易盒子启用并扫码登录，
    # 音乐页在热门推荐与官方歌单之间展示账号自建歌单（只读，不回写网易）
    "netease_my_playlists": os.environ.get("FNMUSIC_NETEASE_MY_PLAYLISTS", "false").lower() in ("true", "1", "yes"),
    "cover_enrich": os.environ.get("FNMUSIC_COVER_ENRICH", "true").lower() in ("true", "1", "yes"),
    "env_watch": os.environ.get("FNMUSIC_ENV_WATCH", "true").lower() in ("true", "1", "yes"),
    # 官方端点取证：未拦截的 /music/api 请求首见 INFO、之后每 50 次采样一条；
    # =detail 时逐条记录。官方 App 更新引入新端点时（如 2.5.0 前的 download/*），
    # 日志可直接看到客户端在调什么，避免"Unknown 元数据"类问题无迹可循
    "trace_forward": os.environ.get("FNMUSIC_TRACE_FORWARD", "").strip().lower() in ("detail", "1", "true", "yes"),
}

_REDACT_KEY_PARTS = ("api_key", "apikey", "token", "secret", "password")

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

CACHE_EXTS = ("mp3", "flac", "wav", "ogg", "opus", "m4a", "aac", "ape", "wv", "dsf", "dff", "tta")

# 飞牛 Kl() 归一化：mpeg/mp3→mp3，wav/pcm→wav，m4a/aac/mp4→m4a，其余小写原样（flac/ogg/ape/wv…）
_FORMAT_ALIASES = {
    "mp3": "mp3",
    "mpeg": "mp3",
    "mpga": "mp3",
    "flac": "flac",
    "wav": "wav",
    "wave": "wav",
    "pcm": "wav",
    "lpcm": "wav",
    "ogg": "ogg",
    "vorbis": "ogg",
    "opus": "opus",
    "m4a": "m4a",
    "mp4": "m4a",
    "mp4a": "m4a",
    "aac": "m4a",
    "alac": "m4a",
    "ape": "ape",
    "wv": "wv",
    "wavpack": "wv",
    "dsf": "dsf",
    "dff": "dff",
    "dsd": "dsd",
    "tta": "tta",
    "tak": "tak",
    "wma": "wma",
    "aiff": "aiff",
    "aif": "aiff",
}


# 模块级搜索缓存
_SEARCH_CACHE: dict[str, dict] = {}
# 每个登录用户当前要搜的词。换词时作废这个用户其余关键词的缓存，不刷新 ts。
_USER_SEARCH_GEN: dict[str, int] = {}
_USER_SEARCH_WORD: dict[str, str] = {}
# 每个用户正在等待的输入窗口。新词到达时 set，让上一词立刻结束等待。
_SEARCH_DEBOUNCE: dict[str, asyncio.Event] = {}
# fetch_* 读这个范围：搜索框用凭证哈希，补链加 ":play"，联想加 ":suggest"。
_FETCH_SCOPE: ContextVar[str] = ContextVar("fnmusic_fetch_scope", default="")
_SCOPE_HEADER = "X-Fnmusic-Scope"


class _SupersededSearch:
    """同一用户的更新关键词替换了这次搜索。"""


_SUPERSEDED = _SupersededSearch()


class SourceSearchGate:
    """一个音源同时只跑一个关键词。每个用户在队列里只留最新词。

    cancellable 时，同一用户的新词取消正在跑的搜索并立刻开始。
    否则等当前请求自然结束，丢掉响应，再搜新词。其他用户按到达顺序排在后面。
    """

    def __init__(self, cancellable: bool):
        self.cancellable = cancellable
        self._epoch = 0
        self._running: dict | None = None
        self._queue: list[dict] = []

    def reset(self) -> None:
        self._epoch += 1
        job = self._running
        self._running = None
        queued = self._queue
        self._queue = []
        if job and job.get("task") and not job["task"].done():
            job["task"].cancel()
        for slot in queued:
            _resolve_waiters(slot, _SUPERSEDED)
        if job:
            _resolve_waiters(job, _SUPERSEDED)

    async def run(self, scope: str, keyword: str, factory):
        fut = asyncio.get_running_loop().create_future()
        self._admit(scope, keyword, factory, fut)
        self._pump()
        try:
            return await asyncio.shield(fut)
        except asyncio.CancelledError:
            self._detach(fut)
            raise

    def _admit(self, scope: str, keyword: str, factory, fut) -> None:
        running = self._running
        if running and running["scope"] == scope and running["keyword"] == keyword and not running.get("superseded"):
            running["waiters"].append(fut)
            return
        if running and running["scope"] == scope and running["keyword"] != keyword:
            running["superseded"] = True
            _resolve_waiters(running, _SUPERSEDED)
            if self.cancellable:
                task = running.get("task")
                if task and not task.done():
                    task.cancel()
            self._upsert(scope, keyword, factory, fut, front=True)
            return
        self._upsert(scope, keyword, factory, fut, front=False)

    def _upsert(self, scope: str, keyword: str, factory, fut, front: bool) -> None:
        for slot in self._queue:
            if slot["scope"] != scope:
                continue
            if slot["keyword"] != keyword:
                _resolve_waiters(slot, _SUPERSEDED)
                slot["keyword"] = keyword
                slot["factory"] = factory
                slot["waiters"] = [fut]
            else:
                slot["waiters"].append(fut)
            if front:
                self._queue.remove(slot)
                self._queue.insert(0, slot)
            return
        slot = {"scope": scope, "keyword": keyword, "factory": factory, "waiters": [fut]}
        if front:
            self._queue.insert(0, slot)
        else:
            self._queue.append(slot)

    def _detach(self, fut) -> None:
        running = self._running
        if running and fut in running["waiters"]:
            running["waiters"].remove(fut)
            if self.cancellable and not running["waiters"]:
                running["superseded"] = True
                task = running.get("task")
                if task and not task.done():
                    task.cancel()
            return
        for slot in list(self._queue):
            if fut in slot["waiters"]:
                slot["waiters"].remove(fut)
                if not slot["waiters"]:
                    self._queue.remove(slot)
                return

    def _pump(self) -> None:
        if self._running is not None or not self._queue:
            return
        slot = self._queue.pop(0)
        job = {
            "scope": slot["scope"],
            "keyword": slot["keyword"],
            "factory": slot["factory"],
            "waiters": slot["waiters"],
            "superseded": False,
            "task": None,
        }
        job["epoch"] = self._epoch
        self._running = job
        job["task"] = asyncio.get_running_loop().create_task(self._execute(job))

    async def _execute(self, job: dict) -> None:
        result = _SUPERSEDED
        try:
            result = await job["factory"]()
        except asyncio.CancelledError:
            result = _SUPERSEDED
        except Exception as exc:
            result = exc
        # 取消之后不能再 await，否则事件循环会把清理和下一词一起丢掉。
        self._finish(job, result)

    def _finish(self, job: dict, result) -> None:
        if job.get("epoch") != self._epoch:
            _resolve_waiters(job, _SUPERSEDED)
            return
        if self._running is job:
            self._running = None
        if job.get("superseded") or result is _SUPERSEDED:
            _resolve_waiters(job, _SUPERSEDED)
        elif isinstance(result, Exception):
            _resolve_waiters(job, result, error=True)
        else:
            _resolve_waiters(job, result)
        self._pump()


def _resolve_waiters(job: dict, result, error: bool = False) -> None:
    for fut in job.get("waiters", []):
        if fut.done():
            continue
        if error:
            fut.set_exception(result)
        else:
            fut.set_result(result)


def _note_user_keyword(scope: str, keyword: str) -> int:
    """记下这个用户正在搜的词。换词时清掉该用户其他词的缓存，ts 归零而不是续期。"""
    if _USER_SEARCH_WORD.get(scope) != keyword:
        _USER_SEARCH_GEN[scope] = _USER_SEARCH_GEN.get(scope, 0) + 1
        _USER_SEARCH_WORD[scope] = keyword
        for entry in _SEARCH_CACHE.values():
            if entry.get("credentials") == scope and entry.get("keyword") != keyword:
                entry["superseded"] = True
                entry["items"] = []
                entry["ts"] = 0
        pending = _SEARCH_DEBOUNCE.get(scope)
        if pending is not None:
            pending.set()
    return _USER_SEARCH_GEN.get(scope, 1)


async def _wait_search_debounce(scope: str) -> asyncio.Event | None:
    """停手满 search_debounce_s 才返回当前窗口。0 表示不延迟。"""
    delay = float(CONF.get("search_debounce_s") or 0)
    if delay <= 0:
        return None
    event = asyncio.Event()
    _SEARCH_DEBOUNCE[scope] = event
    try:
        await asyncio.wait_for(event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        pass
    return event


def _user_search_stale(entry: dict, scope: str) -> bool:
    return bool(entry.get("superseded")) or entry.get("gen") != _USER_SEARCH_GEN.get(scope)


def reset_source_search_gates() -> None:
    for gate in (_LX_SEARCH_GATE, _MUSICDL_SEARCH_GATE, _MUSICBOX_SEARCH_GATE):
        gate.reset()
    _USER_SEARCH_GEN.clear()
    _USER_SEARCH_WORD.clear()
    _SEARCH_DEBOUNCE.clear()


_LX_SEARCH_GATE = SourceSearchGate(cancellable=True)
_MUSICDL_SEARCH_GATE = SourceSearchGate(cancellable=False)
_MUSICBOX_SEARCH_GATE = SourceSearchGate(cancellable=False)


def _search_ttl(entry: dict) -> float:
    # Backend IDs/URLs are memory scoped; positive results revalidate in 5m.
    # 超时交回的空列表也是 partial。先看有没有歌，避免把 0 条缓存半分钟，
    # 用户紧接着再搜同词还是空的。
    if not entry.get("items"):
        return 10.0
    if entry.get("partial"):
        return 30.0
    return min(float(CONF.get("search_cache_ttl", 604800)), 300.0)


def _clean_search_cache() -> None:
    now = time.time()
    expired = [k for k, v in _SEARCH_CACHE.items() if now - v.get("accessed", v.get("ts", 0)) >= 900]
    if len(_SEARCH_CACHE) > 2000:
        expired += sorted(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k].get("accessed", 0))[:1000]
    for key in expired:
        entry = _SEARCH_CACHE.pop(key, {})
        task = entry.get("task")
        if task and not task.done():
            task.cancel()


def _set_search_cache(keyword: str, entry: dict) -> None:
    _clean_search_cache()
    _SEARCH_CACHE[keyword] = entry


# === .env 热重载（FNMUSIC_ENV_WATCH=1 默认开） ===
# WebUI 切源 / 手工编辑 .env 后无需重启 proxy：白名单键同步进 CONF 与
# os.environ（recommend 的 LLM 配置直接读环境变量），并清空搜索缓存让
# 新选音源立即生效。路径/端口类配置不在白名单，仍需重启。
_ENV_WATCH_KEYS: dict[str, tuple[str, str]] = {
    "FNMUSIC_MUSICDL_ENABLED": ("musicdl_enabled", "bool"),
    "FNMUSIC_NETEASE_ENABLED": ("netease_enabled", "bool"),
    "FNMUSIC_LX_ENABLED": ("lx_enabled", "bool"),
    "FNMUSIC_ONLINE_SOURCES": ("online_sources", "str"),
    "LX_SOURCES": ("lx_sources", "lx_sources"),
    "FNMUSIC_SEARCH_PROBE": ("search_probe", "bool"),
    "FNMUSIC_QUALITY_MODE": ("quality_mode", "quality_mode"),
    "FNMUSIC_TEE_SAVE_ENABLED": ("tee_save_enabled", "bool"),
    "FNMUSIC_TEE_SAVE_DIR": ("tee_save_dir", "str"),
    "FNMUSIC_TEE_CACHE_MAX": ("tee_cache_max", "tee_cache_max"),
    "FNMUSIC_FAV_AUTO_BIND": ("fav_auto_bind", "bool"),
    "FNMUSIC_OFFICIAL_BIND_TIMEOUT_S": ("official_bind_timeout_s", "bind_timeout"),
    "FNMUSIC_TEE_HANDOFF_MAX": ("tee_handoff_max", "tee_handoff_max"),
    "FNMUSIC_AUTO_COVER": ("auto_cover", "bool"),
    "FNMUSIC_LYRIC_AUTO_DL": ("lyric_auto_dl", "bool"),
    "FNMUSIC_LIBRARY_SCAN_PATH": ("library_scan_path", "str"),
    "FNMUSIC_RECOMMEND_HOT": ("recommend_hot", "bool"),
    "FNMUSIC_RECOMMEND_DAILY": ("recommend_daily", "bool"),
    "FNMUSIC_NETEASE_MY_PLAYLISTS": ("netease_my_playlists", "bool"),
    "FNMUSIC_COVER_ENRICH": ("cover_enrich", "bool"),
    "FNMUSIC_TRACE_FORWARD": ("trace_forward", "bool"),
    "FNMUSIC_TRANSCODE_ENABLED": ("transcode_enabled", "bool"),
    "FNMUSIC_TRANSCODE_BITRATE": ("transcode_bitrate", "str"),
    "FNMUSIC_TRANSCODE_HLS_TIME": ("transcode_hls_time", "seconds"),
    "FNMUSIC_TRANSCODE_MAX_SESSIONS": ("transcode_max_sessions", "tee_cache_max"),
    "FNMUSIC_TRANSCODE_TTL_S": ("transcode_ttl_s", "ttl_seconds"),
    "FNMUSIC_TRANSCODE_CACHE_MAX_MB": ("transcode_cache_max_mb", "mb_int"),
    "FNMUSIC_TRANSCODE_DL_BITRATE": ("transcode_dl_bitrate", "str"),
    "FNMUSIC_LLM_BASE_URL": ("llm_base_url", "llm_url"),
    "FNMUSIC_LLM_API_KEY": ("", "str"),
    "FNMUSIC_LLM_MODEL": ("llm_model", "str"),
    "FNMUSIC_SEARCH_TIMEOUT": ("search_timeout", "seconds"),
    # issue #29：推荐构建预算/候选数/逐首校验运行期可调（recommend.py 动态读 os.environ）
    "FNMUSIC_RECOMMEND_BUDGET_S": ("", "str"),
    "FNMUSIC_RECOMMEND_CANDIDATES": ("", "str"),
    "FNMUSIC_LLM_TIMEOUT_S": ("", "str"),
    "FNMUSIC_RECOMMEND_VERIFY_PLAYABLE": ("", "str"),
    "FNMUSIC_RECOMMEND_VERIFY_TIMEOUT_S": ("", "str"),
    "FNMUSIC_REC_SEARCH_CONCURRENCY": ("", "str"),
    "FNMUSIC_REC_SEARCH_INTERVAL": ("", "str"),
    # W3：动态音质决策 / 本地曲库优先（quality.py、local_library.py 直接读 os.environ）
    "FNMUSIC_QUALITY_DYNAMIC": ("", "str"),
    "FNMUSIC_QUALITY_POLICY": ("", "str"),
    "FNMUSIC_QUALITY_FIXED": ("", "str"),
    "FNMUSIC_QUALITY_WIFI": ("", "str"),
    "FNMUSIC_QUALITY_CELLULAR": ("", "str"),
    "FNMUSIC_REMOTE_AS_CELLULAR": ("", "str"),
    "FNMUSIC_UNKNOWN_AS_CELLULAR": ("", "str"),
    "FNMUSIC_LOCAL_FIRST": ("", "str"),
    "FNMUSIC_LOCAL_FIRST_ANY_CLASS": ("", "str"),
    "FNMUSIC_LOCAL_FIRST_CELLULAR_LOSSY_ONLY": ("", "str"),
    # 收藏联动：归档下载（download.py）+ 网易云红心（app.py 的 _sync_netease_like）。
    # 这两项都在管理页的「收藏归档目录 / 点收藏时自动下载 / 收藏同步红心」里配置，
    # download.py 直接读 os.environ，所以必须进白名单——否则用户在管理页勾完开关，
    # .env 变了、进程里的 os.environ 没变，功能要重启服务才生效（表现为"勾了没反应"）。
    "FNMUSIC_DOWNLOAD_DIR": ("", "str"),
    "FNMUSIC_DOWNLOAD_ON_FAVORITE": ("", "str"),
    "FNMUSIC_FAV_SYNC_LIKE": ("", "str"),
    # 洛雪音乐同步服务器（lx-music-sync-server）歌单同步：lxsync.py 直接读 os.environ，
    # 同理必须进白名单，否则管理页填完要重启才生效。
    "FNMUSIC_LX_SYNC_ENABLED": ("", "str"),
    "FNMUSIC_LX_SYNC_URL": ("", "str"),
    "FNMUSIC_LX_SYNC_PASSWORD": ("", "str"),
    "FNMUSIC_LX_SYNC_REFRESH_S": ("", "str"),
    "FNMUSIC_LX_SYNC_DEVICE": ("", "str"),
    "FNMUSIC_LX_SYNC_INSECURE_TLS": ("", "str"),
    "FNMUSIC_LX_SYNC_WRITEBACK": ("", "str"),
}
_ENV_WATCH_INTERVAL_S = 2.0
_ENV_WATCH_DEBOUNCE_S = 0.5


def _env_watch_path() -> str:
    return os.environ.get("FNMUSIC_ENV_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", ".env"
    )


def _env_watch_parse(raw: str, kind: str):
    raw = str(raw or "").strip()
    if kind == "bool":
        return raw.lower() in ("true", "1", "yes")
    if kind == "tee_cache_max":
        try:
            return max(1, min(100, int(raw)))
        except (TypeError, ValueError):
            return None
    if kind == "quality_mode":
        return raw.lower() if raw.lower() in ("high", "balanced", "smooth") else None
    if kind == "lx_sources":
        return _normalize_lx_sources(raw)
    if kind == "llm_url":
        return raw.rstrip("/")
    if kind == "seconds":
        try:
            return max(1.0, min(60.0, float(raw)))
        except (TypeError, ValueError):
            return None
    if kind == "bind_timeout":
        try:
            return max(10.0, min(3600.0, float(raw)))
        except (TypeError, ValueError):
            return None
    if kind == "ttl_seconds":
        try:
            return max(10.0, min(3600.0, float(raw)))
        except (TypeError, ValueError):
            return None
    if kind == "mb_int":
        try:
            return max(1, min(102400, int(raw)))
        except (TypeError, ValueError):
            return None
    if kind == "tee_handoff_max":
        try:
            return max(0, min(20, int(raw)))
        except (TypeError, ValueError):
            return None
    return raw


def apply_env_hot_reload(env_path: "str | None" = None) -> list[str]:
    """解析 .env 应用白名单键；返回发生变化的 CONF 键名（空 = 无变化）。

    环境变量同步写原始字符串（recommend 等模块直接读 os.environ）。
    """
    path = env_path or _env_watch_path()
    kv = dict(parse_env_file(path)[0])
    changed: list[str] = []
    for env_key, (conf_key, kind) in _ENV_WATCH_KEYS.items():
        if env_key not in kv:
            continue
        os.environ[env_key] = str(kv[env_key])
        value = _env_watch_parse(kv[env_key], kind)
        if value is None:
            continue
        if conf_key and CONF.get(conf_key) != value:
            CONF[conf_key] = value
            changed.append(conf_key)
    return changed


def _reset_search_cache() -> None:
    tasks = [
        entry.get("task") for entry in _SEARCH_CACHE.values()
        if entry.get("task") and not entry["task"].done()
    ]
    for task in tasks:
        task.cancel()
    _SEARCH_CACHE.clear()


def _env_watch_stat(path: "str | None" = None):
    try:
        st = os.stat(path or _env_watch_path())
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


# 音源集合相关的 CONF 键：任一变化意味着推荐缓存里的旧源曲目可能不可播（issue #22）
_SOURCE_CONF_KEYS = {"musicdl_enabled", "netease_enabled", "lx_enabled", "online_sources", "lx_sources"}


def _cancel_daily_tasks() -> None:
    for key in list(_DAILY_TASKS):
        old = _DAILY_TASKS.pop(key, None)
        if old is not None and not old.done():
            old.cancel()


async def _env_watch_loop() -> None:
    last = _env_watch_stat()
    while True:
        try:
            await asyncio.sleep(_ENV_WATCH_INTERVAL_S)
            cur = _env_watch_stat()
            if cur is None or cur == last:
                last = cur
                continue
            await asyncio.sleep(_ENV_WATCH_DEBOUNCE_S)
            settled = _env_watch_stat()
            if settled != cur:
                continue  # 仍在写入，下一轮再看
            last = settled
            changed = apply_env_hot_reload()
            if changed:
                _reset_search_cache()
                if _SOURCE_CONF_KEYS & set(changed):
                    # issue #22：切换音源后当日推荐立即失效，切回歌单按新音源重建，
                    # 旧源曲目不再残留到当日结束
                    removed = dailyrec.invalidate_today_cache_all_users()
                    _cancel_daily_tasks()
                    logger.info(
                        "音源配置变化(%s)：已失效当日推荐缓存 %d 个，推荐歌单将按新音源重建",
                        ",".join(sorted(_SOURCE_CONF_KEYS & set(changed))), removed,
                    )
                logger.info(".env 热重载生效: %s", ",".join(sorted(changed)))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("env watch loop error: %s", e)


def _source_config() -> dict:
    return {k: v for k, v in CONF.items() if k.endswith(("_enabled", "_url", "_limit", "_quality", "_sources")) or k == "online_sources"}


def _search_scope(request: Request) -> str:
    auth = [request.headers.get(k, "") for k in ("cookie", "authorization", "x-trim-music-temp-token")]
    config = _source_config()
    filters = sorted((k, v) for k, v in request.query_params.multi_items() if k not in ("page", "q", "query", "keyword"))
    return hashlib.sha256(json.dumps([auth, config, filters, request.url.path], sort_keys=True).encode()).hexdigest()


def _source_enabled(guid: str) -> bool:
    source = source_from_online_guid(guid)
    if not CONF.get({"netease": "netease_enabled", "lx": "lx_enabled"}.get(source, "musicdl_enabled")):
        return False
    if source == "lx":
        # lx 平台白名单（online:lx:<platform>:<id>）；未配置 = 跟随 lx 服务全部已启用平台
        selected = CONF.get("lx_sources") or []
        if selected:
            parts = guid.split(":")
            return len(parts) >= 4 and parts[2] in selected
        return True
    if source != "netease" and CONF.get("online_sources"):
        selected = {name.strip().lower().removesuffix("musicclient") for name in str(CONF["online_sources"]).split(",")}
        return source.lower() in selected
    return True


ONLINE_TRIAL_MARKERS = (
    "(试听)",
    "（试听）",
    "试听片段",
    "片段试听",
    "试听版",
    "[试听]",
    "【试听】",
    "- 试听",
    " - 试听",
)


def _has_trial_fragment(item: dict) -> bool:
    """条目是否只是「试听片段」。

    不能用 ``item.get("freeTrialInfo") or item.get("freeTrialPrivilege")`` 这种写法：
    ``freeTrialPrivilege`` 是网易云**每条 song/url 响应都必带**的标准结构体，
    正常曲目也一定存在且是非空 dict（真理值）。用它做真值判断会把**每一首歌**都判成
    试听而剔除，与是否登录、是否 VIP 完全无关，表现为「搜不到任何在线歌曲」。
    真正的信号在这个结构体**内部的布尔位**：``resConsumable`` / ``userConsumable``
    为 True 才表示正在消耗试听额度。``freeTrialInfo`` 语义正好相反：None 表示无试听，
    **只有确实是试听曲目才带非空内容**（形如 ``{"st": 起始秒, "et": 结束秒}``），
    因此对它做存在性判断是安全的。两者混在一起做真值判断正是本 bug 的成因。
    """
    def flag(d, k):
        v = d.get(k) if isinstance(d, dict) else None
        return v is True or str(v).lower() == "true"

    priv = item.get("freeTrialPrivilege")
    if flag(priv, "resConsumable") or flag(priv, "userConsumable"):
        return True
    return bool(item.get("freeTrialInfo"))


def is_playable_online_track(item: dict, require_id: bool = False, allow_paywall: bool = False) -> bool:
    """最终防线校验：过滤无音频流或试听标记的不可播曲目。"""
    if not isinstance(item, dict):
        return False

    title = str(item.get("title") or item.get("name") or item.get("song_name") or "").strip()
    if not title:
        return False

    if require_id:
        sid = str(item.get("id") or item.get("song_id") or item.get("guid") or "").strip()
        if not sid:
            return False

    # 1. 标题含试听标记
    if any(marker in title for marker in ONLINE_TRIAL_MARKERS):
        return False

    # 2. 字段试听标记
    if item.get("is_trial") is True or _has_trial_fragment(item):
        return False
    if int(item.get("is_free_part") or 0) != 0 or int(item.get("fail_process") or 0) == 4:
        return False

    # 3. 收费/VIP 拦截（verified 条目已由服务端完成"直链解析+Range探活"验证，可播性有实证，跳过收费元数据拦截；
    #    当 allow_paywall=True 时同样跳过收费拦截，用于逐曲探活关闭时放行第三方源候选歌曲）
    if not allow_paywall and item.get("verified") is not True:
        if int(item.get("pay_type") or 0) != 0:
            return False
        if int(item.get("pkg_price") or 0) != 0 or int(item.get("price") or 0) != 0:
            return False
        fee = item.get("fee")
        if fee is not None:
            try:
                if int(fee) not in (0, 8):
                    return False
            except (ValueError, TypeError):
                pass

    # 4. 显式不可播/无流标记
    if item.get("unplayable") is True or item.get("playable") is False:
        return False
    if item.get("has_stream") is False:
        return False

    # 5. 音频流直链校验：若带有 download_url 或 url 键，则必须合法可用，绝不能是空串或 404
    if "download_url" in item:
        d_url = str(item.get("download_url") or "").strip()
        if not d_url or not d_url.startswith(("http://", "https://")) or "404/error.html" in d_url or "error.html" in d_url:
            return False
    if "url" in item:
        u = str(item.get("url") or "").strip()
        if not u or "404/error.html" in u or "error.html" in u:
            return False

    # 6. 片段时长校验（<=35s 且带有试听迹象）
    duration = item.get("duration_s") or (item.get("duration") or 0)
    try:
        duration_s = float(duration)
        if 0 < duration_s <= 35 and ("试听" in title or item.get("is_trial")):
            return False
    except (ValueError, TypeError):
        pass

    return True


def _same_recording(left: dict, right: dict) -> bool:
    """Conservative identity: never strip live/remix/version markers."""
    for key in ("title", "artist", "version"):
        a, b = (str(x.get(key) or "").strip().casefold() for x in (left, right))
        if a != b or (key != "version" and not a):
            return False
    try:
        a, b = float(left.get("duration_s") or 0), float(right.get("duration_s") or 0)
        return math.isfinite(a) and math.isfinite(b) and a > 0 and b > 0 and abs(a - b) <= 2.0
    except (TypeError, ValueError):
        return False


def deduplicate_online_items(items: list[dict]) -> list[dict]:
    """Keep the published representative and strict recording alternatives."""
    result: list[dict] = []
    seen = set()
    for item in items:
        if not is_playable_online_track(item):
            continue
        guid = online_guid_from_item(item)
        if guid in seen:
            continue
        seen.add(guid)
        representative = next((x for x in result if _same_recording(x, item)), None)
        if representative is None:
            representative = dict(item)
            representative["_alternatives"] = list(item.get("_alternatives", []))
            result.append(representative)
        else:
            alternatives = representative.setdefault("_alternatives", [])
            if guid not in {online_guid_from_item(x) for x in alternatives}:
                alternatives.append({k: v for k, v in item.items() if k != "_alternatives"})
    return result


def play_format_from_ext(ext: str | None) -> str:
    raw = (ext or "mp3").strip().lower().lstrip(".")
    if raw.startswith("audio/"):
        raw = raw.split("/", 1)[-1]
    return _FORMAT_ALIASES.get(raw, raw or "mp3")


def _sanitize_content_disposition(value: str) -> str:
    """把 Content-Disposition 里的非 latin-1 文件名规范化为 RFC 6266/5987 格式。

    上游（官方 trim-music）在交付转码/下载文件时会在 filename="..." 填入原始中文歌名，
    同时在 filename*=UTF-8''... 填入 URL 编码的歌名。Starlette 要求所有响应头
    必须能被 latin-1 编码，否则抛出 UnicodeEncodeError 导致 500 报错。
    本函数将 filename="..." 中的非 ASCII 字符替换为安全字符，并确保 filename*= 存在。
    """
    m_star = re.search(r"filename\*=([^;]+)", value, re.IGNORECASE)
    m_plain = re.search(r'filename="([^"]+)"', value, re.IGNORECASE)
    if not m_plain:
        m_plain = re.search(r'filename=([^; ]+)', value, re.IGNORECASE)

    raw_fn = m_plain.group(1) if m_plain else "track"
    ext = "." + raw_fn.rsplit(".", 1)[-1] if "." in raw_fn else ""
    ascii_fn = re.sub(r'[^\x20-\x7e]', '_', raw_fn)
    if not ascii_fn.strip() or ascii_fn == ext:
        ascii_fn = f"track{ext}"

    if m_star:
        star_part = f"filename*={m_star.group(1).strip()}"
    else:
        quoted = quote(raw_fn, encoding="utf-8")
        star_part = f"filename*=UTF-8''{quoted}"

    disposition = value.split(";")[0].strip() or "attachment"
    return f'{disposition}; filename="{ascii_fn}"; {star_part}'


def filter_headers(headers: Any, exclude_keys: set | None = None) -> dict:
    exclude = HOP_BY_HOP | {k.lower() for k in (exclude_keys or set())}
    out = {}
    for k, v in headers.items():
        if k.lower() in exclude:
            continue
        try:
            str(v).encode("latin-1")
            out[k] = v
        except UnicodeEncodeError:
            if k.lower() == "content-disposition":
                out[k] = _sanitize_content_disposition(str(v))
            else:
                out[k] = str(v).encode("latin-1", errors="replace").decode("latin-1")
    return out


def copy_incoming_headers(request: Request) -> dict:
    """透传鉴权 Cookie / Token。Starlette 头名为小写，需显式回填以免丢失 music-token。

    authx 为新版官方前端登录后的逐请求签名头（含时间戳与随机数），
    原样转发给上游即可通过校验；切勿缓存或复用其值。"""
    headers = filter_headers(request.headers, exclude_keys={"host", "content-length"})
    headers["accept-encoding"] = "identity"
    for key in ("cookie", "authorization", "x-trim-music-temp-token", "authx"):
        val = request.headers.get(key)
        if val:
            headers[key] = val
    return headers


def get_by_path(d: Any, path: str) -> Any:
    curr = d
    for p in path.split("."):
        if isinstance(curr, dict) and p in curr:
            curr = curr[p]
        else:
            return None
    return curr


def set_by_path(d: dict, path: str, val: Any):
    parts = path.split(".")
    curr = d
    for p in parts[:-1]:
        if p not in curr or not isinstance(curr[p], dict):
            curr[p] = {}
        curr = curr[p]
    curr[parts[-1]] = val


def extract_keyword(request: Request) -> str:
    """前端打包用 q，部分调用/验收用 keyword。"""
    params = request.query_params
    return (params.get("keyword") or params.get("q") or params.get("query") or "").strip()


def online_guid_from_item(item: dict) -> str:
    raw_id = str(item.get("id") or "")
    src = str(item.get("source") or "")
    if raw_id.startswith("online:"):
        return raw_id
    if ":" in raw_id:
        return f"online:{raw_id}"
    return f"online:{src}:{raw_id}"


def song_id_from_online_guid(guid: str) -> str:
    if guid.startswith("online:"):
        return guid[len("online:") :]
    return guid


def is_online_guid(guid: str) -> bool:
    return bool(guid) and guid.startswith("online:")


def source_from_online_guid(guid: str) -> str:
    parts = (guid or "").split(":")
    return parts[1] if len(parts) >= 3 else ""


def build_online_track(item: dict) -> dict:
    """对齐飞牛前端 ZQ 解构 / _h() 期望：artists、album 对象、genres 数组、audioSpec、duration 毫秒。"""
    guid = online_guid_from_item(item)
    src = str(item.get("source") or source_from_online_guid(guid) or "")
    title = str(item.get("title") or item.get("name") or "")
    artist = str(item.get("artist") or "")
    album = str(item.get("album") or "")
    duration_s = item.get("duration_s") or 0
    try:
        duration_s = float(duration_s)
    except (TypeError, ValueError):
        duration_s = 0
    duration_ms = int(duration_s * 1000)
    ext = str(item.get("ext") or "mp3") or "mp3"
    play_format = play_format_from_ext(ext)
    file_size = item.get("file_size") or 0
    try:
        file_size = int(file_size or 0)
    except (TypeError, ValueError):
        file_size = 0
    cover = str(item.get("cover_url") or "")
    # 路径带真实后缀，飞牛 ll() 用 path 解析 extension；封面走 guid 以便 /static/cover 拦截
    spec_path = f"online/{src}/{guid}.{play_format}"

    artists_list = [{"name": artist, "guid": f"{guid}:artist"}] if artist else []
    album_obj = {
        "name": album,
        "guid": f"{guid}:album",
        "artists": artists_list,
        "coverId": guid,
    }
    # 专辑伪装 guid 同步登记（issue #22）：客户端点击专辑时按 fake 反解分源适配详情
    register_fake_album(guid, album, item)
    audio_spec = {
        "path": spec_path,
        "format": play_format,
        "codec": play_format,
        "container": play_format,
        "duration": duration_ms,
        "size": file_size,
        "channel": 2,
        "sampleRate": 44100,
        "bitDepth": 16 if play_format in ("wav", "flac", "aiff") else None,
        "bitrate": 1411000 if play_format in ("flac", "wav", "ape", "wv") else 320000,
    }
    audio_spec = {k: v for k, v in audio_spec.items() if v is not None}

    return {
        "guid": guid,
        "id": guid,
        "title": title,
        "name": title,
        "artist": artist,
        "artists": artists_list,
        "album": album_obj,
        "albumName": album,
        "audioSpec": audio_spec,
        "duration": duration_ms,
        "duration_ms": duration_ms,
        "durationMs": duration_ms,
        "duration_s": duration_s,
        "codec": play_format,
        "codecName": play_format,
        "format": play_format,
        "ext": ext,
        "size": file_size,
        "file_size": file_size,
        "coverId": guid,
        "cover_url": cover,
        "coverUrl": cover,
        "coverURL": cover,
        "source": src,
        "is_online": True,
        "isFavorite": False,
        "isCue": False,
        "hasLyric": bool(item.get("lyric")),
        "genres": [],
        "accessStatus": 0,
    }


def artist_from_track(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    a = item.get("artist") or item.get("singer") or item.get("singers") or ""
    if isinstance(a, list):
        names = []
        for x in a:
            if isinstance(x, dict):
                names.append(str(x.get("name") or ""))
            else:
                names.append(str(x))
        return " ".join(n for n in names if n).strip().lower()
    if isinstance(a, dict):
        return str(a.get("name") or "").strip().lower()
    return str(a).strip().lower()


def title_from_track(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("title") or item.get("name") or "").strip().lower()


def should_cache(range_header: str | None) -> bool:
    """完整拉取才落盘：无 Range，或 bytes=0-（开区间）。Safari bytes=0-1 探测不落盘。"""
    if not range_header:
        return True
    r = range_header.strip().lower()
    return bool(re.match(r"^bytes=0-$", r))


def is_range_from_zero_or_none(range_header: str | None) -> bool:
    return should_cache(range_header)


def log_stream_probe(method: str, guid: str, range_header: str | None, cached: bool) -> None:
    """在线取流探针：一行记录 Range 形态与落盘资格，供真机确认播放器取流行为。"""
    if CONF.get("stream_probe"):
        logger.info(
            "stream probe: %s %s range=%r cached=%s tee_eligible=%s",
            method, guid, range_header, cached, should_cache(range_header),
        )


def cache_safe_guid(guid: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", guid)


def online_file_id(guid: str) -> str:
    """online:migu:600929… → 600929…，仅用于查找旧文件，不再写进文件名。"""
    return song_id_from_online_guid(guid).rsplit(":", 1)[-1]


def safe_basename_title(title: str) -> str:
    t = re.sub(r'[/\\:\0]', "_", (title or "").strip()) or "unknown"
    t = re.sub(r"\s+", " ", t).strip(" .")
    return t[:120]


def library_basename(title: str, artist: str = "") -> str:
    """曲库文件名：歌手 - 歌名（无源站 id）。飞牛无标签时会用文件名当标题。"""
    title_s = safe_basename_title(title)
    artist_s = safe_basename_title(artist) if (artist or "").strip() else ""
    if artist_s and artist_s.lower() != title_s.lower() and artist_s != "unknown":
        return f"{artist_s} - {title_s}"
    return title_s


def media_ref_path(guid: str) -> str:
    return os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.ref")


def _path_stem(path: str) -> str:
    root, ext = os.path.splitext(path)
    known = set(CACHE_EXTS) | {"lrc", "part"}
    if ext.lstrip(".").lower() in known:
        return root
    return path


def remember_media_path(guid: str, media_path: str) -> None:
    """记住曲库里的文件词干（不含扩展名），音频和 .lrc 共用。"""
    stem = _path_stem(media_path)
    try:
        os.makedirs(CONF["cache_dir"], exist_ok=True)
        with open(media_ref_path(guid), "w", encoding="utf-8") as f:
            f.write(stem)
        # .ref 静默写丢会让已落库的歌重新出网、曲库出现重复文件，写后读回校验
        with open(media_ref_path(guid), encoding="utf-8") as f:
            if f.read().strip() != stem:
                raise OSError("read-back mismatch")
    except Exception as e:
        logger.warning("Failed to remember media path for %s: %s", guid, e)


def archive_ref_path(guid: str) -> str:
    """归档（收藏自动下载）用的 .ref：与曲库缓存的 .ref 分属两个命名空间。"""
    return os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.archive.ref")


def remember_archive_path(guid: str, media_path: str) -> None:
    """登记**归档**文件（收藏自动下载），与边播边存的缓存分用两个 ref 命名空间。

    必须分开：``remember_media_path`` 每个 guid 只有一个 ``.ref``，而归档目录与曲库
    缓存目录是两个不同位置；复用同一个 ref 会互相覆盖，卸载时就漏删其中一边。
    存的是**词干**（不含扩展名），这样卸载脚本按 ``${stem}.{ext}`` 逐个精确删除的
    老逻辑对归档同样适用（含 ``.lrc``）。
    """
    stem = _path_stem(media_path)
    try:
        os.makedirs(CONF["cache_dir"], exist_ok=True)
        with open(archive_ref_path(guid), "w", encoding="utf-8") as f:
            f.write(stem)
        # 与 remember_media_path 同理：写丢会让卸载时漏删用户目录里的归档文件
        with open(archive_ref_path(guid), encoding="utf-8") as f:
            if f.read().strip() != stem:
                raise OSError("read-back mismatch")
    except Exception as e:
        logger.warning("Failed to remember archive path for %s: %s: %s",
                       guid, type(e).__name__, e)


def recalled_media_stem(guid: str) -> str | None:
    ref = media_ref_path(guid)
    if not os.path.exists(ref):
        return None
    try:
        with open(ref, encoding="utf-8") as f:
            stem = _path_stem(f.read().strip())
        if stem:
            return stem
    except Exception:
        return None
    return None


def recalled_media_path(guid: str) -> str | None:
    stem = recalled_media_stem(guid)
    if not stem:
        return None
    for ext in CACHE_EXTS:
        path = f"{stem}.{ext}"
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    return None


def unique_library_path(directory: str, basename: str, ext: str) -> str:
    dest = os.path.join(directory, f"{basename}.{ext}")
    if not os.path.exists(dest):
        return dest
    n = 2
    while os.path.exists(os.path.join(directory, f"{basename} ({n}).{ext}")):
        n += 1
    return os.path.join(directory, f"{basename} ({n}).{ext}")


def write_audio_tags(path: str, title: str, artist: str = "", album: Any = "") -> None:
    """写入 title/artist/album，飞牛扫描后用标签而不是文件名显示。

    album 可能是前端曲目对象 {name, guid, ...}；先归一成纯字符串再写入，
    避免把整个对象的文本写进专辑标签。
    """
    title, artist, album = _tag_fields({"title": title, "artist": artist, "album": album})
    if not title and not artist:
        return
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
        if audio is None:
            return
        if getattr(audio, "tags", None) is None:
            try:
                audio.add_tags()
            except Exception:
                pass
        if title:
            audio["title"] = title
        if artist:
            audio["artist"] = artist
        if album:
            audio["album"] = album
        audio.save()
    except Exception as e:
        logger.warning("Failed to write audio tags for %s: %s", path, e)


_COVER_FETCH_TRANSPORT: "httpx.SyncBaseTransport | None" = None
_COVER_FETCH_MAX_BYTES = 10 * 1024 * 1024


def _sniff_image_mime(data: bytes) -> str:
    """魔数嗅探图片类型；非图片返回空串（Content-Type 会说谎，字节不会）。"""
    if data.startswith(b"\xFF\xD8\xFF"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return ""


def download_cover_bytes(url: str) -> "tuple[bytes, str] | None":
    """下载封面图字节（内嵌用）。非图片/超限/酷我文本假图/网络失败一律返回 None。"""
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    if _KW_TEXT_COVER_HOST in url:
        return None
    try:
        kwargs: dict = {"timeout": 8.0, "follow_redirects": True}
        if _COVER_FETCH_TRANSPORT is not None:
            kwargs["transport"] = _COVER_FETCH_TRANSPORT
        with httpx.Client(**kwargs) as client:
            r = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        data = r.content or b""
        if not data or len(data) > _COVER_FETCH_MAX_BYTES:
            return None
        mime = _sniff_image_mime(data)
        if not mime:
            return None
        return data, mime
    except Exception as e:
        logger.debug("cover fetch failed for %s: %s", url, type(e).__name__)
        return None


def _has_embedded_cover(path: str) -> bool:
    try:
        from mutagen import File as MutagenFile

        mf = MutagenFile(path)
        tags = getattr(mf, "tags", None)
        if tags is None:
            return False
        if hasattr(tags, "pictures"):  # FLAC/APE
            return bool(tags.pictures)
        if hasattr(tags, "getall"):  # ID3
            return bool(tags.getall("APIC"))
        return bool(tags.get("covr") or tags.get("METADATA_BLOCK_PICTURE"))  # MP4/OGG
    except Exception:
        return False


def embed_audio_cover(path: str, data: bytes, mime: str) -> bool:
    """把封面字节内嵌进音频文件（MP3 APIC / FLAC Picture / MP4 covr / OGG）。

    飞牛扫描器（dhowden/tag）只认内嵌图，不读外置 cover 图片文件，内嵌是
    官方 App 显示封面的唯一途径。文件已有内嵌图（源站自带）则跳过；不支持的
    格式（wav 等）返回 False。
    """
    try:
        import base64

        from mutagen import File as MutagenFile
        from mutagen.flac import Picture
        from mutagen.id3 import APIC, ID3
        from mutagen.mp4 import MP4Cover

        audio = MutagenFile(path)
        if audio is None or _has_embedded_cover(path):
            return False
        cls = audio.__class__.__name__
        if cls == "MP3":
            try:
                id3 = ID3(path)
            except Exception:
                id3 = ID3()
            id3.delall("APIC")
            id3.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            id3.save(path, v2_version=3)
            return True
        if getattr(audio, "tags", None) is None:
            try:
                audio.add_tags()
            except Exception:
                return False
        if cls == "MP4":
            if mime not in ("image/jpeg", "image/png"):
                return False
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            audio["covr"] = [MP4Cover(data, imageformat=fmt)]
            audio.save()
            return True
        if cls == "FLAC":
            pic = Picture()
            pic.type = 3
            pic.mime = mime
            pic.desc = "Cover"
            pic.data = data
            audio.add_picture(pic)
            audio.save()
            return True
        if cls in ("OggVorbis", "OggOpus", "OggSpeex"):
            pic = Picture()
            pic.type = 3
            pic.mime = mime
            pic.desc = "Cover"
            pic.data = data
            audio["METADATA_BLOCK_PICTURE"] = [base64.b64encode(pic.write()).decode("ascii")]
            audio.save()
            return True
        return False
    except Exception as e:
        logger.debug("cover embed failed for %s: %s", path, e)
        return False


# ---------------------------------------------------------------------------
# W3：music.db 自动定位（quality.py / local_library.py 的索引来源）
#
# 飞牛各版本把应用数据放在 /vol*/@appdata 下，层级并不统一；只在 trim.music 自己
# 的目录里递归 glob（不扫音乐库大盘）。结果按「显式配置值」为键缓存：测试换
# CONF["music_db"] 时键变了会自动重查，不必手动失效。
# ---------------------------------------------------------------------------

_MUSIC_DB_RESOLVED: dict[str, str] = {}


def _music_db_candidates() -> list[str]:
    """music.db 的候选路径（按优先级，去重保序）。"""
    explicit = str(CONF.get("music_db") or "").strip()
    cands: list[str] = []
    if explicit:
        cands.append(explicit)
    cands.append("/usr/local/apps/@appdata/trim.music/db/music.db")
    for pat in (
        "/vol*/@appdata/trim.music/db/music.db",
        "/vol*/@appdata/trim.music/*/db/music.db",
        "/vol*/@appcenter/trim.music/db/music.db",
        "/vol*/@appdata/trim.music/**/music.db",
        "/usr/local/apps/@appdata/trim.music/**/music.db",
    ):
        try:
            cands.extend(sorted(glob.glob(pat, recursive=True)))
        except Exception:  # noqa: BLE001 - 单个 glob 表达式异常不该影响整体
            continue
    seen: set[str] = set()
    out: list[str] = []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def resolve_music_db() -> str:
    """显式配置存在才用，否则在常见布局里探测；都没有则回退到显式值。"""
    explicit = str(CONF.get("music_db") or "").strip()
    cached = _MUSIC_DB_RESOLVED.get(explicit)
    if cached is not None:
        return cached

    candidates = _music_db_candidates()
    for cand in candidates:
        if cand and os.path.isfile(cand):
            _MUSIC_DB_RESOLVED[explicit] = cand
            if cand != explicit:
                logger.info("music.db 自动定位成功: %s（显式配置=%r）", cand, explicit or "未配置")
            return cand

    resolved = explicit or (candidates[1] if len(candidates) > 1 else "")
    _MUSIC_DB_RESOLVED[explicit] = resolved
    if not os.path.isfile(resolved):
        # 逐个列出候选与存在性：真机上「猜路径猜错」是本地曲库类功能静默失效的
        # 头号原因，没有这份清单就只能靠用户去 SSH 上 ls。
        logger.warning(
            "music.db 未找到（本地曲库优先不可用）。已探测: %s",
            "; ".join(f"{c}{'[存在]' if os.path.isfile(c) else '[缺失]'}" for c in candidates[:12]) or "无",
        )
    return resolved


def reset_music_db_cache_for_test() -> None:
    _MUSIC_DB_RESOLVED.clear()


def _db_library_dirs() -> "list[str]":
    """从飞牛 music.db 的 shared_library 表读曲库目录（纯候选，不做任何回退）。"""
    db = resolve_music_db()
    if not db or not os.path.exists(db):
        logger.warning("曲库目录无法确定：music.db 不存在（%s），且未配置 FNMUSIC_LIBRARY_DIR", db)
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT path FROM shared_library ORDER BY id").fetchall()
        finally:
            con.close()
    except Exception as e:
        logger.warning("Failed to read shared_library path: %s", e)
        return []
    out = [str(p) for p, in rows if p and os.path.isdir(str(p))]
    if not out:
        logger.warning("music.db 里没有可用的共享库路径（shared_library 行数=%d）: %s",
                       len(rows), db)
    return out


def _authorized_dirs() -> "list[str]":
    """飞牛**正式授权**给本应用的目录（开放网关 + 兼容环境变量）。

    W13 移植自 gzywd v2.9.4：以前靠 root 硬读 /vol1/...，既不合飞牛规范，
    也让管理员在应用设置里看不到「授权目录」入口、没法合规授权。现在先问网关
    「我被授权了哪些目录」，再用这些目录当曲库来源。
    """
    try:
        rep = trimgw.authorized_report()
    except Exception as exc:  # noqa: BLE001 - 查授权失败绝不能拖垮主流程
        logger.warning("查询飞牛授权目录失败: %s: %s", type(exc).__name__, exc)
        return []
    paths = rep.get("shared_paths") or []
    if not paths:
        logger.warning("飞牛尚未给 %s 授权任何目录：%s",
                       trimgw.app_name(), rep.get("hint") or rep.get("shared_error") or "")
    return list(paths)


def _strict_authorization() -> bool:
    """true = 只扫已授权目录，绝不依赖 root 直读未授权路径（合规最严档）。"""
    return (os.environ.get("FNMUSIC_STRICT_AUTHORIZATION") or "false").strip().lower() in (
        "true", "1", "yes", "on")


def library_authorization_state(path: str) -> dict:
    """判断一个曲库目录当前是否处在飞牛授权范围内（供诊断/日志使用）。"""
    try:
        rep = trimgw.authorized_report()
    except Exception as exc:  # noqa: BLE001
        return {"authorized": False, "paths": [], "hint": f"查询失败: {exc}"}
    paths = rep.get("shared_paths") or []
    covered = bool(path) and any(
        os.path.abspath(path) == os.path.abspath(a)
        or os.path.abspath(path).startswith(os.path.abspath(a).rstrip(os.sep) + os.sep)
        for a in paths
    )
    return {
        "authorized": covered,
        "paths": paths,
        "hint": rep.get("hint") or "",
        "error": rep.get("shared_error") or "",
    }


def detect_library_dir() -> str:
    """定位本地曲库目录。

    W13 移植自 gzywd v2.9.4，优先级：
    **管理页显式配置** → **飞牛正式授权目录** → music.db 的 shared_library
    → 最后才回退到 cache_dir。

    A 侧原本只有 显式配置 → shared_library → cache_dir 三段。补授权目录这一档
    是因为回退到 cache_dir 属「静默失效」：那里一首歌都没有，于是本地曲库优先、
    本地每日推荐全都表现成「功能没开」而不是「路径错了」——所以每次回退都要把
    原因写进日志。
    """
    explicit = str(CONF.get("library_dir") or "").strip()
    if explicit:
        if not os.path.isdir(explicit):
            logger.warning("FNMUSIC_LIBRARY_DIR 配置了但目录不存在: %s", explicit)
        else:
            st = library_authorization_state(explicit)
            if not st["authorized"]:
                logger.warning(
                    "曲库目录 %s 不在飞牛授权范围内（当前靠 root 直读）。"
                    "建议到「应用设置 → 授权目录」把它授权给本应用：%s",
                    explicit, st.get("hint") or "尚未授权任何目录",
                )
        return explicit

    authorized = _authorized_dirs()
    if authorized:
        db_dirs = _db_library_dirs()
        picked = trimgw.pick_library_from_authorized(db_dirs, authorized)
        if picked:
            logger.info("曲库目录取自飞牛已授权目录: %s", picked)
            return picked
        logger.info("曲库目录使用飞牛授权目录（music.db 未提供可用路径）: %s", authorized[0])
        return authorized[0]

    db_dirs = _db_library_dirs()
    if db_dirs:
        if _strict_authorization():
            logger.warning("FNMUSIC_STRICT_AUTHORIZATION=true，拒绝使用未授权目录 %s", db_dirs[0])
        else:
            logger.warning("飞牛未授权任何目录，暂按 music.db 路径 %s 读取（建议到应用设置授权）",
                           db_dirs[0])
            return db_dirs[0]

    logger.warning("曲库目录无法确定：既没有飞牛授权目录，music.db 也不可用，"
                   "且未配置 FNMUSIC_LIBRARY_DIR；回退到 %s（空目录，本地曲库优先不会命中）",
                   CONF["cache_dir"])
    return CONF["cache_dir"]


_TEE_SAVE_DIR_WARNED = False


def tee_save_dir() -> str:
    """边听边存落盘目录：配置路径可用则用，否则回退自动探测的曲库目录。"""
    explicit = str(CONF.get("tee_save_dir") or "").strip()
    if not explicit:
        return detect_library_dir()
    usable = False
    try:
        os.makedirs(explicit, exist_ok=True)
        usable = os.path.isdir(explicit) and os.access(explicit, os.W_OK)
    except Exception:
        usable = False
    if usable:
        return explicit
    global _TEE_SAVE_DIR_WARNED
    if not _TEE_SAVE_DIR_WARNED:
        _TEE_SAVE_DIR_WARNED = True
        logger.warning(
            "FNMUSIC_TEE_SAVE_DIR=%s 不可用（无法创建或不可写），边听边存回退到 %s",
            explicit, detect_library_dir(),
        )
    return detect_library_dir()


def iter_media_dirs() -> list[str]:
    dirs: list[str] = []
    explicit = str(CONF.get("tee_save_dir") or "").strip()
    for d in (([explicit] if explicit else []) + [detect_library_dir(), CONF["cache_dir"]]):
        if d and d not in dirs:
            dirs.append(d)
    return dirs


def adopt_library_perms(path: str) -> None:
    try:
        parent = os.path.dirname(path) or "."
        st = os.stat(parent)
        os.chown(path, st.st_uid, st.st_gid)
        os.chmod(path, 0o644)
    except Exception:
        pass


def find_cache_file(guid: str) -> str | None:
    recalled = recalled_media_path(guid)
    if recalled:
        return recalled
    safe = cache_safe_guid(guid)
    for d in iter_media_dirs():
        if not os.path.isdir(d):
            continue
        for ext in CACHE_EXTS:
            exact = os.path.join(d, f"{safe}.{ext}")
            if os.path.exists(exact) and os.path.getsize(exact) > 0:
                return exact
    return _find_file_by_snapshot_tags(guid)


# 标签兜底查库的阴性缓存：确认不在曲库的 guid 短期内不再反复扫快照/查库
_TAGS_LOOKUP_MISS: dict[str, float] = {}
_TAGS_MISS_TTL_S = 300.0


def _find_file_by_snapshot_tags(guid: str) -> "str | None":
    """.ref 与精确名都 miss 时的自愈兜底：按快照标签在官方曲库反查文件。

    命中说明歌已落库只是 .ref 丢了（或本就有同名同曲的本地文件），
    remember_media_path 自愈后下次直接走 recall。必须 title+artist 双条件
    才可能命中（album 有则再收紧），查库只在 miss 时发生。
    """
    now = time.monotonic()
    miss_at = _TAGS_LOOKUP_MISS.get(guid)
    if miss_at is not None and now - miss_at < _TAGS_MISS_TTL_S:
        return None
    snap = _lookup_online_snapshot(guid)
    title = artist = album = ""
    if snap:
        title, artist, album = _tag_fields(snap)
    path = library_file_by_tags(title, artist, album) if title and artist else None
    if not path:
        if len(_TAGS_LOOKUP_MISS) > 1024:
            _TAGS_LOOKUP_MISS.clear()
        _TAGS_LOOKUP_MISS[guid] = now
        return None
    remember_media_path(guid, path)
    logger.info("Recalled library file for %s via tags: %s", guid, path)
    return path


def promote_cache_hit(guid: str, audio_path: str) -> str:
    """旧 cache/ 音频：若曲库已有对应文件或歌词，则对齐过去。"""
    recalled = recalled_media_path(guid)
    if recalled:
        return recalled
    lib = detect_library_dir()
    try:
        if os.path.abspath(os.path.dirname(audio_path)) == os.path.abspath(lib):
            remember_media_path(guid, audio_path)
            return audio_path
    except Exception:
        return audio_path
    # Bare legacy IDs cannot prove source/track identity.
    return audio_path


def _is_rolling_cache_stem(path: str, guid: str) -> bool:
    """是否为 cache 目录下该 guid 的滚动缓存产物（cache_safe_guid 命名）。"""
    rolling = os.path.join(CONF["cache_dir"], cache_safe_guid(guid))
    try:
        return os.path.abspath(os.path.splitext(path)[0]) == os.path.abspath(rolling)
    except Exception:
        return False


def library_media_path(guid: str, title: str, ext: str, artist: str = "", directory: str | None = None) -> str:
    lib = directory or detect_library_dir()
    recalled = recalled_media_path(guid)
    if recalled and not _is_rolling_cache_stem(recalled, guid):
        return recalled
    stem = recalled_media_stem(guid)
    if stem and not _is_rolling_cache_stem(stem, guid):
        return f"{stem}.{ext}"
    os.makedirs(lib, exist_ok=True)
    return unique_library_path(lib, library_basename(title, artist), ext)


def find_lyric_file(guid: str) -> str | None:
    stem = recalled_media_stem(guid)
    if stem:
        sibling = f"{stem}.lrc"
        if os.path.exists(sibling) and os.path.getsize(sibling) > 0:
            return sibling
    audio = find_cache_file(guid)
    if audio:
        sibling = os.path.splitext(audio)[0] + ".lrc"
        if os.path.exists(sibling) and os.path.getsize(sibling) > 0:
            return sibling
    safe = cache_safe_guid(guid)
    for d in iter_media_dirs():
        if not os.path.isdir(d):
            continue
        exact = os.path.join(d, f"{safe}.lrc")
        if os.path.exists(exact) and os.path.getsize(exact) > 0:
            return exact
    return None


def lyric_cache_path(guid: str, title: str = "", artist: str = "") -> str:
    """歌词三档归属（音频到哪，歌词到哪）：已有歌词复用原位；有音频落在音频旁；
    无音频只落 cache（guid 命名）。绝不把曲库目录当无音频时的兜底——那会制造
    只有歌词没有音频的孤儿 .lrc（曲库在云盘上时还会产生上传流量）。
    """
    found = find_lyric_file(guid)
    if found:
        return found
    audio = find_cache_file(guid)
    if audio:
        return os.path.splitext(audio)[0] + ".lrc"
    return os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.lrc")


def read_lyric_cache(guid: str) -> str:
    path = find_lyric_file(guid)
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        logger.warning("Failed to read lyric cache %s: %s", path, e)
        return ""


def write_lyric_cache(guid: str, text: str, title: str = "", artist: str = "") -> None:
    text = (text or "").strip()
    if not text:
        return
    if text == read_lyric_cache(guid):
        return
    path = lyric_cache_path(guid, title=title, artist=artist)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    part_path = f"{path}.{uuid4().hex[:8]}.part"
    try:
        with open(part_path, "w", encoding="utf-8") as f:
            f.write(text)
            f.write("\n")
        os.replace(part_path, path)
        adopt_library_perms(path)
        remember_media_path(guid, path)
    except Exception as e:
        logger.warning("Failed to write lyric cache %s: %s", path, e)
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except Exception:
                pass


def promote_shadow_lyric(guid: str, audio_path: str) -> None:
    """音频落曲库后，把 cache 里的影子歌词（无音频时代的 guid 命名副本）提升为
    音频旁 sidecar，词曲贴身；曲库已有 sidecar 时不覆盖。跨文件系统时退化为复制。
    """
    dest = os.path.splitext(audio_path)[0] + ".lrc"
    if os.path.exists(dest):
        return
    shadow = os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.lrc")
    if not os.path.exists(shadow):
        return
    try:
        os.replace(shadow, dest)
    except OSError:
        try:
            shutil.copyfile(shadow, dest)
            os.remove(shadow)
        except Exception as e:
            logger.warning("shadow lyric promote failed for %s: %s", guid, e)
            return
    adopt_library_perms(dest)


async def cache_lyrics_from_musicdl(musicdl_client: httpx.AsyncClient, guid: str) -> dict | None:
    """与音频 tee 并行：把 musicdl /info 里的 LRC 落到曲库同目录 sidecar。"""
    song_id = song_id_from_online_guid(guid)
    try:
        r = await musicdl_client.get("/info", params={"id": song_id}, timeout=10.0)
        if r.status_code != 200:
            return None
        data = r.json()
        if not (isinstance(data, dict) and data.get("ok") is not False):
            return None
        write_lyric_cache(
            guid,
            str(data.get("lyric") or ""),
            title=str(data.get("title") or ""),
            artist=str(data.get("artist") or ""),
        )
        return data
    except Exception as e:
        logger.warning("lyric sidecar fetch failed for %s: %s", guid, e)
        return None


async def resolve_online_lyric(request: Request, guid: str) -> str:
    """本地 .lrc 优先；没有再向源站要，拿到就落盘。"""
    cached = read_lyric_cache(guid)
    if cached:
        return cached

    if not _source_enabled(guid):
        return ""
    src = source_from_online_guid(guid)
    if src == "netease":
        musicbox_client = get_musicbox_client(request.app)
        raw_song_id = song_id_from_online_guid(guid)
        song_id = raw_song_id.split(":")[-1]
        try:
            r = await musicbox_client.get(f"/api/v1/song/{song_id}/lyric", timeout=10.0)
            if r.status_code == 200:
                res_data = r.json()
                if isinstance(res_data, dict) and res_data.get("ok") is not False:
                    l_data = res_data.get("data")
                    if isinstance(l_data, dict):
                        lyric_text = str(l_data.get("lyric") or "").strip()
                        if lyric_text:
                            info = await _online_info(request, guid)
                            write_lyric_cache(
                                guid,
                                lyric_text,
                                title=str((info or {}).get("title") or ""),
                                artist=str((info or {}).get("artist") or ""),
                            )
                            return lyric_text
        except Exception as e:
            logger.warning("musicbox lyric fetch failed for %s: %s", guid, e)
        return ""

    data = await _online_info(request, guid)
    text = str((data or {}).get("lyric") or "").strip()
    if text:
        write_lyric_cache(
            guid,
            text,
            title=str((data or {}).get("title") or ""),
            artist=str((data or {}).get("artist") or ""),
        )
    return text


def media_type_for_ext(ext: str) -> str:
    return {
        "mp3": "audio/mpeg",
        "flac": "audio/flac",
        "wav": "audio/wav",
        "ogg": "audio/ogg",
        "opus": "audio/ogg",
        "m4a": "audio/mp4",
        "aac": "audio/aac",
        "ape": "audio/x-ape",
        "wv": "audio/x-wavpack",
        "dsf": "audio/x-dsd",
        "dff": "audio/x-dff",
        "tta": "audio/x-tta",
        "wma": "audio/x-ms-wma",
        "aiff": "audio/aiff",
    }.get(ext.lower(), "application/octet-stream")


_AUDIO_MAGIC = (
    (b"fLaC", "flac"),
    (b"OggS", "ogg"),
    (b"MAC ", "ape"),
    (b"wvpk", "wv"),
    (b"DSD ", "dsf"),
    (b"FRM8", "dff"),
)


def _sniff_audio_ext(first: bytes) -> str | None:
    """从**字节**判断容器格式；认不出来返回 None（绝不瞎猜）。

    为什么不能只信 content-type：网易云 CDN 对 FLAC 也常回 `audio/mpeg`，musicbox 的
    URL 载荷也把容器放在 `type` 而不是 `ext`。结果是「FLAC 声明成 MP3」，按 MIME 选
    解码器的播放器会一直转圈（用户实测症状）。
    """
    if not first:
        return None
    head = bytes(first[:16])
    for magic, ext in _AUDIO_MAGIC:
        if head.startswith(magic):
            return ext
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "m4a"
    if len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head.startswith(b"ID3"):
        return "mp3"
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"           # 无 ID3 的 MP3 帧同步
    return None


def _starts_at_zero(resp: httpx.Response, range_header: str | None) -> bool:
    """响应是否从文件第 0 字节开始（只有这时字节头才代表整首的容器）。"""
    if resp.status_code == 200:
        return True
    return (range_header or "").strip().lower().startswith("bytes=0-")


def _hls_segment_media(filename: str) -> str:
    """HLS 分片 MIME：音频流不能用 video/*（部分播放器见到 video 就整段不播）。"""
    if filename == tc.INIT_NAME:
        return "audio/mp4"
    if filename.endswith(".m4s"):
        return "audio/iso.segment"
    return "application/octet-stream"


def ext_from_content_type(content_type: str) -> str:
    ct = (content_type or "").lower()
    if "flac" in ct:
        return "flac"
    if "wavpack" in ct or "x-wv" in ct:
        return "wv"
    if "wav" in ct or "wave" in ct:
        return "wav"
    if "opus" in ct:
        return "opus"
    if "ogg" in ct:
        return "ogg"
    if "ape" in ct:
        return "ape"
    if "aiff" in ct:
        return "aiff"
    if "mp4" in ct or "m4a" in ct:
        return "m4a"
    if "aac" in ct:
        return "aac"
    if "mpeg" in ct or "mp3" in ct:
        return "mp3"
    return play_format_from_ext(ct.split("/")[-1] if "/" in ct else "mp3")


def parse_http_range(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    if not range_header:
        return None
    m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip(), re.I)
    if not m:
        return None
    start_s, end_s = m.group(1), m.group(2)
    if start_s == "" and end_s == "":
        return None
    if start_s == "":
        suffix = int(end_s)
        start = max(file_size - suffix, 0)
        end = file_size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else file_size - 1
    end = min(end, file_size - 1)
    if start < 0 or start >= file_size or start > end:
        return None
    return start, end


def serve_file_with_range(path: str, range_header: str | None, media_type: str,
                          extra_headers: dict | None = None) -> Response:
    file_size = os.path.getsize(path)
    rng = parse_http_range(range_header, file_size)

    def iter_file(offset: int, length: int) -> AsyncGenerator[bytes, None]:
        async def gen() -> AsyncGenerator[bytes, None]:
            remaining = length
            with open(path, "rb") as fp:
                fp.seek(offset)
                while remaining > 0:
                    chunk = fp.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return gen()

    extra = extra_headers or {}
    if rng is None:
        headers = {
            "Content-Type": media_type,
            "Content-Length": str(file_size),
            "Accept-Ranges": "bytes",
        }
        headers.update(extra)
        return StreamingResponse(
            iter_file(0, file_size),
            status_code=200,
            headers=headers,
        )

    start, end = rng
    length = end - start + 1
    headers = {
        "Content-Type": media_type,
        "Content-Length": str(length),
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
    }
    headers.update(extra)
    return StreamingResponse(
        iter_file(start, length),
        status_code=206,
        headers=headers,
    )


def get_upstream_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "upstream_client", None)
    if client is None:
        transport = httpx.AsyncHTTPTransport(uds=CONF["upstream_sock"])
        client = httpx.AsyncClient(transport=transport, base_url="http://unix", timeout=30.0)
        fastapi_app.state.upstream_client = client
    return client


def get_musicdl_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "musicdl_client", None)
    if client is None:
        client = httpx.AsyncClient(base_url=CONF["musicdl_url"], timeout=45.0)
        fastapi_app.state.musicdl_client = client
    return client


def get_musicbox_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "musicbox_client", None)
    if client is None:
        client = httpx.AsyncClient(base_url=CONF["musicbox_url"], timeout=20.0)
        fastapi_app.state.musicbox_client = client
    return client


def get_lx_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "lx_client", None)
    if client is None:
        client = httpx.AsyncClient(base_url=CONF["lx_url"], timeout=25.0)
        fastapi_app.state.lx_client = client
    return client


def get_llm_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "llm_client", None)
    if client is None:
        client = httpx.AsyncClient(timeout=dailyrec.LLM_TIMEOUT_S)
        fastapi_app.state.llm_client = client
    return client


def _new_cdn_client() -> httpx.AsyncClient:
    """进程级共享的 CDN 客户端（移植 G v2.9.30 的 `_new_cdn_client`）。

    旧实现里 `_open_online_stream` 每个请求都新建一个 httpx.AsyncClient：一次播放
    付一次 TCP+TLS 握手，下一首预热再付一次。改成共享客户端 + 有界连接池后，跨曲目
    复用连接，预热才真正便宜。

    超时沿用 A 的取值（read=60s）而不是 G 的扁平 30s：A 侧的长暂停容忍是既有取流
    行为，收紧会改变播放语义。
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0),
        follow_redirects=True,
        limits=httpx.Limits(
            max_connections=20,
            max_keepalive_connections=10,
            keepalive_expiry=30.0,
        ),
    )


def get_cdn_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "cdn_client", None)
    if client is None or client.is_closed:
        client = _new_cdn_client()
        fastapi_app.state.cdn_client = client
    return client


async def forward_to_upstream(request: Request, client: httpx.AsyncClient) -> Response:
    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"

    headers = copy_incoming_headers(request)
    body = await request.body()

    req = client.build_request(
        method=request.method,
        url=url_path,
        headers=headers,
        content=body if body else None,
    )
    # 幂等方法在"连接还没换来任何响应"的失败（官方服务重启窗口的陈旧连接 /
    # uds 瞬断）上重试一次：响应已开始流式传输后的失败不重试（无法安全回放）。
    resp = None
    attempts = 2 if request.method.upper() in ("GET", "HEAD") else 1
    for attempt in range(attempts):
        try:
            resp = await client.send(req, stream=True)
            break
        except (httpx.TransportError, httpx.RemoteProtocolError):
            if attempt + 1 >= attempts:
                raise
            logger.info("upstream forward retry (%s %s): connection-level failure",
                        request.method, request.url.path)
    resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})
    # 上游请求强制 accept-encoding: identity，其 Content-Length 即精确字节数；原样
    # 透传可让依赖总长度的播放内核走定长帧（对齐官方直连行为）。无 body 的状态
    # 码不能带；上游无视 identity 仍压缩时长度描述的是编码体，透传必错位，弃用。
    content_length = resp.headers.get("content-length")
    content_encoding = (resp.headers.get("content-encoding") or "").strip().lower()
    if (content_length and content_encoding in ("", "identity")
            and resp.status_code >= 200 and resp.status_code not in (204, 304)):
        resp_headers["content-length"] = content_length

    async def body_stream() -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        body_stream(),
        status_code=resp.status_code,
        headers=resp_headers,
    )


async def fetch_upstream_envelope(request: Request, client: httpx.AsyncClient) -> Response | dict:
    """透传上游并解析 JSON 信封。失败时返回 Response，成功返回 dict。"""
    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)
    body = await request.body()
    req = client.build_request(
        method=request.method,
        url=url_path,
        headers=headers,
        content=body if body else None,
    )
    resp = await client.send(req)
    resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})
    if resp.status_code != 200:
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    try:
        payload = resp.json()
    except Exception:
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    if not isinstance(payload, dict):
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    payload["_ext_headers"] = resp_headers
    return payload


async def fetch_musicdl_search(client: httpx.AsyncClient, keyword: str, limit: int, sources: str | None = None) -> dict | None:
    if not keyword:
        return None
    scope = _FETCH_SCOPE.get()

    async def _fetch_once(params: dict[str, Any], timeout: float) -> dict | None:
        try:
            r = await client.get("/search", params=params, timeout=timeout)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    raw_items = data.get("items")
                    if isinstance(raw_items, list):
                        data["items"] = [it for it in raw_items if is_playable_online_track(it)]
                    return data
        except Exception as e:
            logger.warning("Failed to fetch online search from musicdl: %s", e)
        return None

    async def _query():
        params: dict[str, Any] = {"keyword": keyword, "limit": limit}
        selected_sources = CONF["online_sources"] if sources is None else sources
        if selected_sources:
            params["sources"] = selected_sources
        timeout = max(1.0, float(CONF.get("search_timeout") or 15))
        started = time.monotonic()
        data = await _fetch_once(params, timeout)
        # musicdl 快速交回 0 条且带错误（全源 busy/熔断/瞬时失败）时不是
        # 终态：稍等片刻重试一次，避免把偶发空结果当成真"无结果"。
        if (isinstance(data, dict) and not data.get("items") and data.get("errors")
                and time.monotonic() - started < 5.0):
            await asyncio.sleep(1.5)
            retry = await _fetch_once(params, timeout)
            if isinstance(retry, dict) and retry.get("items"):
                data = retry
        if isinstance(data, dict) and data.get("errors"):
            logger.warning("musicdl search partial errors: %s", data.get("errors"))
        return data

    return await _MUSICDL_SEARCH_GATE.run(scope, keyword, _query)


async def fetch_musicbox_search(client: httpx.AsyncClient, keyword: str, limit: int) -> list[dict] | None:
    if not keyword:
        return None
    scope = _FETCH_SCOPE.get()

    async def _query():
        return await _musicbox_search_request(client, keyword, limit)

    return await _MUSICBOX_SEARCH_GATE.run(scope, keyword, _query)


async def _musicbox_search_request(client: httpx.AsyncClient, keyword: str, limit: int) -> list[dict] | None:
    try:
        r = await client.get(
            "/api/v1/search",
            params={"keyword": keyword, "limit": limit, "type": "song"},
            timeout=20.0,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict) or data.get("ok") is False:
            return None
        raw_list = data.get("data")
        if not isinstance(raw_list, list):
            return None
        items = []
        song_ids = []
        for it in raw_list:
            if not isinstance(it, dict):
                continue
            if not is_playable_online_track(it):
                continue
            sid = str(it.get("song_id") or it.get("id") or "")
            if not sid:
                continue
            title = str(it.get("song_name") or it.get("title") or it.get("name") or "")
            artist = str(it.get("artist") or "")
            album = str(it.get("album_name") or it.get("album") or "")
            duration = it.get("duration") or 0
            try:
                duration_s = float(duration)
            except (TypeError, ValueError):
                duration_s = 0.0
            quality = str(it.get("quality") or "").upper()
            ext = "flac" if any(q in quality for q in ("SQ", "HR", "无损")) else "mp3"
            items.append({
                "id": f"netease:{sid}",
                "source": "netease",
                "title": title,
                "version": str(it.get("version") or ""),
                "artist": artist,
                "album": album,
                "duration_s": duration_s,
                "ext": ext,
                "cover_url": "",
                "lyric": "",
            })
            song_ids.append(sid)

        if song_ids:
            try:
                detail_resp = await client.get(
                    "/api/v1/songs/detail",
                    params={"ids": ",".join(song_ids)},
                    timeout=15.0,
                )
                if detail_resp.status_code == 200:
                    detail_json = detail_resp.json()
                    if isinstance(detail_json, dict) and detail_json.get("ok") is not False:
                        detail_list = detail_json.get("data")
                        if isinstance(detail_list, list):
                            detail_map = {}
                            for d_item in detail_list:
                                if isinstance(d_item, dict):
                                    d_sid = str(d_item.get("song_id") or d_item.get("id") or "")
                                    if d_sid:
                                        detail_map[d_sid] = d_item
                            for item in items:
                                raw_sid = item["id"].split(":", 1)[-1]
                                d_info = detail_map.get(raw_sid)
                                if d_info:
                                    pic_url = str(d_info.get("album_pic_url") or "")
                                    if pic_url:
                                        item["cover_url"] = pic_url
                                    if d_info.get("has_sq") or d_info.get("has_hr"):
                                        item["ext"] = "flac"
            except Exception as detail_err:
                logger.warning("Failed to fetch songs detail for %s: %s", keyword, detail_err)

        return [it for it in items if is_playable_online_track(it)]
    except Exception as e:
        logger.warning("Failed to fetch musicbox search: %s", e)
        return None


class _SearchItems(list):
    """List-compatible normalized results with source degradation metadata."""
    def __init__(self, items, partial=False):
        super().__init__(items)
        self.partial = partial


async def fetch_lx_search(client: httpx.AsyncClient, keyword: str, limit: int, sources: "list[str] | str | None" = None) -> list[dict]:
    """洛雪音乐源搜索：返回统一 item（id = "lx:<source>:<identifier>"）。

    sources：平台白名单（列表或逗号串），None = 用 CONF["lx_sources"]；空 = 跟随 lx 服务配置。
    """
    if not keyword:
        return None  # type: ignore[return-value]
    scope = _FETCH_SCOPE.get()

    async def _query():
        return await _lx_search_request(client, keyword, limit, sources, scope)

    return await _LX_SEARCH_GATE.run(scope, keyword, _query)


async def _lx_search_request(client: httpx.AsyncClient, keyword: str, limit: int, sources, scope: str) -> list[dict]:
    params: dict[str, Any] = {
        "keyword": keyword,
        "limit": limit,
        "probe": 1 if CONF.get("search_probe") else 0,
    }
    selected = CONF.get("lx_sources") if sources is None else sources
    if isinstance(selected, str):
        selected = [s.strip() for s in selected.split(",") if s.strip()]
    if selected:
        params["sources"] = ",".join(selected)
    timeout = max(1.0, float(CONF.get("search_timeout") or 15))
    headers = {_SCOPE_HEADER: scope} if scope else None
    try:
        r = await client.get(
            "/api/v1/search",
            params=params,
            headers=headers,
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict) or data.get("ok") is False:
            return None
        raw_list = data.get("items")
        if not isinstance(raw_list, list):
            return None
        items = []
        allow_paywall = not bool(CONF.get("search_probe"))
        for it in raw_list:
            if not isinstance(it, dict):
                continue
            if not is_playable_online_track(it, allow_paywall=allow_paywall):
                continue
            tid = str(it.get("id") or "")
            if not tid:
                continue
            duration = it.get("duration_s") or 0
            try:
                duration_s = float(duration)
            except (TypeError, ValueError):
                duration_s = 0.0
            try:
                file_size = int(it.get("file_size") or 0)
            except (TypeError, ValueError):
                file_size = 0
            items.append({
                "id": tid,
                "source": "lx",
                "lx_source": str(it.get("lx_source") or ""),
                "version": str(it.get("version") or ""),
                "title": str(it.get("title") or it.get("name") or ""),
                "artist": str(it.get("artist") or ""),
                "album": str(it.get("album") or ""),
                "duration_s": duration_s,
                "ext": str(it.get("ext") or "mp3") or "mp3",
                "cover_url": str(it.get("cover_url") or ""),
                "file_size": file_size,
                "lyric": "",
                "verified": it.get("verified") is True,
            })
        return _SearchItems([it for it in items if is_playable_online_track(it, allow_paywall=allow_paywall)], partial=bool(data.get("errors")))
    except Exception as e:
        logger.warning("Failed to fetch online search from lxmusic: %s", e)
        return None


# 音质档（从低到高）：网易 = musicbox QUALITY_WHITELIST 全集；
# lx = 洛雪源脚本三档（128k/320k/flac 对应 standard/high/lossless）
_NETEASE_QUALITY_LADDER = ["standard", "higher", "exhigh", "lossless", "hires", "jymaster"]
_LX_QUALITY_LADDER = ["standard", "high", "lossless"]


def quality_order(ladder: "list[str] | tuple[str, ...]", mode: "str | None", primary: "str | None" = None) -> list[str]:
    """按音质模式排档序（ladder 从低到高）。

    high（默认）：锚定 primary（缺省取最高档）向下逐级，失败逐级降档；
    balanced：取中间档（偶数档取中间偏高），失败先向下再向上；
    smooth：从低到高，优先最省流量的档。
    """
    seq = [q for q in ladder if q]
    if not seq:
        return []
    mode = str(mode or "high").strip().lower()
    if mode == "smooth":
        return list(seq)
    if mode == "balanced":
        mid = len(seq) // 2
        return [seq[mid]] + list(reversed(seq[:mid])) + seq[mid + 1:]
    if primary in seq:
        idx = seq.index(primary)
    else:
        idx = len(seq) - 1
    return [seq[idx]] + list(reversed(seq[:idx]))


async def resolve_lx_url(client: httpx.AsyncClient, song_id: str) -> "dict | None":
    """洛雪音乐源直链解析：song_id 形如 "lx:kg:<hash>"。"""
    primary = str(CONF.get("lx_quality") or "lossless").strip()
    qualities = quality_order(_LX_QUALITY_LADDER, CONF.get("quality_mode"), primary)
    if primary and primary not in qualities:
        qualities.insert(0, primary)

    for q in qualities:
        try:
            r = await client.get(
                "/api/v1/track/url",
                params={"id": song_id, "quality": q},
                # 略高于 lxmusic 端点总预算（LX_URL_TIMEOUT，默认 20s）：让端点自己
                # 返回 404/502 完成降档缓存，而不是在 proxy 侧掐断后反复重解析
                timeout=22.0,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("ok") is not False:
                    inner = data.get("data")
                    if isinstance(inner, dict) and inner.get("url"):
                        return inner
        except Exception as e:
            logger.warning("resolve_lx_url error for %s (quality=%s): %s", song_id, q, e)
    return None


# W3：动态音质判定留痕。同一种 (level, source) 组合只记一次，避免整轨重试刷屏。
_LOGGED_QUALITY: set[tuple[str, str]] = set()


def _log_quality_decision(song_id: str, decision: dict) -> None:
    key = (str(decision.get("level") or ""), str(decision.get("source") or ""))
    if key in _LOGGED_QUALITY:
        return
    _LOGGED_QUALITY.add(key)
    logger.info(
        "音质判定 level=%s source=%s policy=%s network=%s (song=%s)",
        key[0], key[1], decision.get("policy"), decision.get("network"), song_id,
    )


# ---------------------------------------------------------------------------
# 网易云直链短缓存（移植 G v2.9.30）
# ---------------------------------------------------------------------------
# 直链 URL 带签名、会过期，所以只缓存很短一段时间。TTL 与
# `prefetch.warm_ttl_seconds()` 读同一个环境变量（且都在调用时读），保证
# 「预热还算不算数」与「缓存还有没有效」不会出现两套口径——G 的
# test_warm_ttl_follows_url_cache_not_a_magic_number 锁的就是这一点。
_URL_CACHE: dict[str, tuple[float, str]] = {}


def _url_cache_ttl() -> float:
    try:
        ttl = float(os.environ.get("FNMUSIC_URL_CACHE_TTL", "600") or 600.0)
    except (TypeError, ValueError):
        ttl = 600.0
    return ttl if ttl > 0 else 600.0


def _url_cache_max() -> int:
    try:
        return int(os.environ.get("FNMUSIC_URL_CACHE_MAX", "1000") or 1000)
    except (TypeError, ValueError):
        return 1000


def _url_cache_get(song_id: str, level: str) -> str | None:
    key = f"{song_id}:{level}"
    entry = _URL_CACHE.get(key)
    if not entry:
        return None
    ts, url = entry
    if time.monotonic() - ts >= _url_cache_ttl():
        _URL_CACHE.pop(key, None)
        return None
    return url


def _url_cache_put(song_id: str, level: str, url: str) -> None:
    _URL_CACHE[f"{song_id}:{level}"] = (time.monotonic(), url)
    limit = _url_cache_max()
    if limit > 0 and len(_URL_CACHE) > limit:
        # 超限时按写入时间淘汰最旧的一半，避免无界增长。
        for old in sorted(_URL_CACHE, key=lambda k: _URL_CACHE[k][0])[: max(1, limit // 2)]:
            _URL_CACHE.pop(old, None)


def _url_cache_peek(song_id: str) -> bool:
    """该歌曲是否有任一档未过期的直链缓存（播放路径判断 warm 用）。"""
    prefix = f"{song_id}:"
    return any(_url_cache_get(song_id, key[len(prefix):]) for key in list(_URL_CACHE) if key.startswith(prefix))


def _url_cache_drop_song(song_id: str) -> None:
    prefix = f"{song_id}:"
    for key in [k for k in _URL_CACHE if k.startswith(prefix)]:
        _URL_CACHE.pop(key, None)


def invalidate_url_cache() -> int:
    n = len(_URL_CACHE)
    _URL_CACHE.clear()
    return n


async def resolve_netease_url(client: httpx.AsyncClient, song_id: str,
                             request: Any = None, stats: dict | None = None) -> str | None:
    # 开关关闭（默认）时逐字节走 A 原有静态路径：netease_quality + quality_mode。
    # 打开时才用 quality.resolve() 的动态档位。
    if quality.dynamic_enabled():
        decision = quality.resolve(request, db_path=resolve_music_db())
        _log_quality_decision(song_id, decision)
        primary = str(decision.get("level") or CONF.get("netease_quality") or "lossless").strip()
        # 单次降档：档位已由 quality.resolve 定死，这里**不再**经 quality_mode /
        # quality_order 二次降级（否则同一请求会被降两次）。只保留一个 exhigh
        # 兜底重试，与 G 的行为一致。
        qualities = [primary] if primary else []
        if "exhigh" not in qualities:
            qualities.append("exhigh")
    else:
        primary = str(CONF.get("netease_quality") or "lossless").strip()
        qualities = quality_order(_NETEASE_QUALITY_LADDER, CONF.get("quality_mode"), primary)
        if primary and primary not in qualities:
            qualities.insert(0, primary)

    # `stats` 非 None ⇒ 调用方是延迟敏感的播放/预热链路，启用直链短缓存，并把
    # 「这次真的省掉了往返」标进 stats["url_cache_hit"]（prefetch.note_play 的 warm
    # 唯一依据）。诊断/探活类调用不传 stats，永远走实时解析——A 既有的逐档降级
    # 用例（test_netease/test_quality_policy/test_v2_features）都不传 stats，因此
    # 逐档网络调用序列与旧行为逐字节一致。
    use_cache = stats is not None
    if use_cache:
        for q in qualities:
            cached = _url_cache_get(song_id, q)
            if cached:
                stats["url_cache_hit"] = True
                stats["url_cache_quality"] = q
                return cached

    for q in qualities:
        try:
            r = await client.get(f"/api/v1/song/{song_id}/url", params={"quality": q}, timeout=10.0)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("ok") is not False:
                    inner = data.get("data")
                    if isinstance(inner, dict):
                        code = inner.get("code")
                        url = inner.get("url")
                        if code == 200 and url:
                            if use_cache:
                                _url_cache_put(song_id, q, str(url))
                            return str(url)
        except Exception as e:
            logger.warning("resolve_netease_url error for %s (quality=%s): %s", song_id, q, e)
    return None


def ensure_search_list(upstream_json: dict) -> list:
    """保证 data.list 存在，本地 0 条时仍能追加在线条目。"""
    data = upstream_json.get("data")
    if not isinstance(data, dict):
        data = {}
        upstream_json["data"] = data
    target = get_by_path(upstream_json, CONF["search_list_path"])
    if isinstance(target, list):
        return target
    for key in ("list", "items", "tracks", "records"):
        if isinstance(data.get(key), list):
            if key != "list":
                data["list"] = data[key]
            return data["list"]
    data["list"] = []
    if "total" not in data:
        data["total"] = 0
    return data["list"]


def merge_online_tracks(
    upstream_json: dict,
    online_data: list[dict] | dict | None,
    page: int = 1,
    size: int = 50,
    selected: bool = False,
) -> dict:
    target_list = ensure_search_list(upstream_json)
    if not online_data:
        return upstream_json

    if isinstance(online_data, dict):
        raw_items = online_data.get("items", [])
    elif isinstance(online_data, list):
        raw_items = online_data
    else:
        raw_items = []

    if not raw_items:
        return upstream_json

    existing_keys = set()
    for item in target_list:
        t = title_from_track(item)
        a = artist_from_track(item)
        if t and a:
            existing_keys.add((t, a))

    online_limit = CONF["online_limit"]
    if not selected:
        start = 0 if page == 1 else online_limit + (page - 2) * size
        raw_page = raw_items[start:start + (online_limit if page == 1 else size)]
    else:
        raw_page = raw_items
    filtered_online = []
    for online_item in raw_page:
        if not is_playable_online_track(online_item, require_id=True):
            continue
        ot = str(online_item.get("title") or online_item.get("name") or "").strip().lower()
        oa = str(online_item.get("artist") or "").strip().lower()
        if ot and oa and (ot, oa) in existing_keys:
            continue
        filtered_online.append(online_item)

    page_online = filtered_online

    for it in page_online:
        target_list.append(build_online_track(it))

    parts = CONF["search_list_path"].split(".")
    parent = upstream_json
    for p in parts[:-1]:
        if isinstance(parent, dict) and p in parent:
            parent = parent[p]
    if isinstance(parent, dict):
        orig_total = parent.get("total")
        if not isinstance(orig_total, int):
            orig_total = len(target_list) - len(page_online)
        parent["total"] = orig_total + sum(1 for item in raw_items if is_playable_online_track(item, require_id=True) and (title_from_track(item), artist_from_track(item)) not in existing_keys)

    return upstream_json


_FAKE_GUID_REVERSE: dict[str, str] = {}
_REGISTRY_WARMED = False
_ONLINE_ID_RE = re.compile(r"online:[A-Za-z0-9_:\-]+")


def fake_official_guid(real_guid: str) -> str:
    """在线 guid 确定性映射为官方 32-hex 形态。

    官方 App 会按 id 格式过滤条目（非 32-hex 的 online: 前缀 id 整条被丢弃，
    症状为收藏/播放历史列表空白或条目消失），且要求收藏/播放回读的 guid 与
    客户端自身持有的一致，因此所有下发的在线 id 必须统一伪装成官方形态。
    """
    fake = hashlib.md5(f"fnmusic-ext::{real_guid}".encode()).hexdigest()
    _FAKE_GUID_REVERSE.setdefault(fake, real_guid)
    return fake


# 专辑伪装 guid 登记表（issue #22）：fake 32-hex → 专辑元信息。
# 与 _FAKE_GUID_REVERSE 同生命周期（内存 + ensure_registry_warm 磁盘重建）；
# /search/album 的在线专辑锚点额外落盘持久化（这类锚点不存在于任何快照存储，
# 重启后 warm 重建不到，见 _persist_album_registry/_load_album_registry_persisted）。
_FAKE_ALBUM_REGISTRY: dict[str, dict] = {}
_ALBUM_REGISTRY_PATH = (
    os.environ.get("FNMUSIC_ALBUM_REGISTRY", "").strip()
    or os.path.join(_HOME, "album_registry.json")
)
_ALBUM_REGISTRY_PERSIST_MAX = 256
_ALBUM_REGISTRY_TTL_S = 14 * 86400
_ALBUM_PERSIST_MIN_INTERVAL_S = 5.0
_ALBUM_PERSIST_LAST = 0.0


def _album_name_from_obj(track_obj: dict) -> str:
    """从曲目对象（VO 快照或 info 形状）提取专辑名。"""
    album = track_obj.get("album")
    if isinstance(album, dict):
        return str(album.get("name") or "").strip()
    return str(track_obj.get("albumName") or track_obj.get("album") or "").strip()


def _person_name(value: Any) -> str:
    """歌手字段：字符串原样，对象取 name，列表取第一个有名字的成员。"""
    if isinstance(value, dict):
        return str(value.get("name") or "").strip()
    if isinstance(value, list):
        for item in value:
            name = _person_name(item)
            if name:
                return name
        return ""
    return str(value or "").strip()


def _tag_fields(info: dict | None) -> tuple[str, str, str]:
    """落盘/历史用的标题、歌手、专辑。恒为纯字符串。

    推荐歌单等前端曲目里 album 是 {name, guid, artists, coverId}，
    不能 str() 整个对象。对象 name 为空时再看 albumName。
    """
    src = info if isinstance(info, dict) else {}
    title_raw = src.get("title")
    if isinstance(title_raw, dict) or not str(title_raw or "").strip():
        title = _person_name(title_raw) or _person_name(src.get("name"))
    else:
        title = str(title_raw).strip()
    artist = _person_name(src.get("artist")) or _person_name(src.get("artists"))
    album = _album_name_from_obj(src) or str(src.get("albumName") or "").strip()
    return title, artist, album


def _cover_url_of(info: dict | None) -> str:
    src = info if isinstance(info, dict) else {}
    return str(src.get("cover_url") or src.get("coverUrl") or src.get("coverURL") or "").strip()


def _item_meta_richness(it: dict) -> int:
    """条目元数据丰富度：title/album 各 1 分。用于"更全的快照覆盖残缺的"合并。"""
    it = it if isinstance(it, dict) else {}
    return int(bool(str(it.get("title") or "").strip())) + int(bool(str(it.get("album") or "").strip()))


def _persist_album_registry(force: bool = False) -> None:
    """把内存专辑登记表落盘（节流 + 上限 + TTL），供重启后 ensure_registry_warm 恢复。

    只持久化 persist=True 的登记（/search/album 的在线专辑锚点与聚合代表曲目），
    而非全部曲目衍生登记——后者可从收藏/历史/推荐缓存重建，落盘只会膨胀。
    """
    global _ALBUM_PERSIST_LAST
    now = time.monotonic()
    if not force and now - _ALBUM_PERSIST_LAST < _ALBUM_PERSIST_MIN_INTERVAL_S:
        return
    _ALBUM_PERSIST_LAST = now
    try:
        os.makedirs(os.path.dirname(_ALBUM_REGISTRY_PATH) or ".", exist_ok=True)
        now_ts = int(time.time())
        entries = []
        for entry in _FAKE_ALBUM_REGISTRY.values():
            if not entry.get("persist"):
                continue
            entries.append({
                "track_guid": str(entry.get("track_guid") or ""),
                "source": str(entry.get("source") or ""),
                "album": str(entry.get("album") or ""),
                "album_id": str(entry.get("album_id") or ""),
                "item": entry.get("item") if isinstance(entry.get("item"), dict) else {},
                "saved_at": now_ts,
            })
        payload = {"version": 1, "savedAt": now_ts, "entries": entries[-_ALBUM_REGISTRY_PERSIST_MAX:]}
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(_ALBUM_REGISTRY_PATH) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, _ALBUM_REGISTRY_PATH)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except Exception as e:
        logger.debug("album registry persist failed: %s", type(e).__name__)


def _load_album_registry_persisted() -> int:
    """重启后从磁盘恢复 /search/album 登记的在线专辑（跨会话可反解，消除
    "重启后点旧搜索结果进专辑回落官方无此专辑"的窗口）。返回恢复条数。"""
    try:
        with open(_ALBUM_REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return 0
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return 0
    cutoff = time.time() - _ALBUM_REGISTRY_TTL_S
    restored = 0
    for row in entries:
        if not isinstance(row, dict):
            continue
        track_guid = str(row.get("track_guid") or "")
        saved_at = row.get("saved_at")
        if not track_guid or (isinstance(saved_at, (int, float)) and saved_at < cutoff):
            continue
        fake = fake_official_guid(f"{track_guid}:album")
        if fake in _FAKE_ALBUM_REGISTRY:
            continue
        _FAKE_ALBUM_REGISTRY[fake] = {
            "source": str(row.get("source") or source_from_online_guid(track_guid)),
            "album": str(row.get("album") or ""),
            "track_guid": track_guid,
            "item": row.get("item") if isinstance(row.get("item"), dict) else {},
            "album_id": str(row.get("album_id") or ""),
            "persist": True,
        }
        restored += 1
    return restored


def register_fake_album(
    track_guid: str, album_name: str, item: dict | None = None, album_id: str = "", persist: bool = False
) -> None:
    """登记专辑伪装 guid → (source, 专辑名, 所属曲目条目)，供专辑详情拦截反解。

    曲目 VO 构造时同步登记（build_online_track 统一挂接，搜索/推荐/收藏/历史
    全路径覆盖）；假 id 是确定性 md5，重启后由 ensure_registry_warm 从收藏/
    历史/歌单附加/推荐缓存重建时同样登记，保证跨会话可反解。
    album_id：netease 真实专辑 id（/search/album 在线专辑直达详情用，可空）。
    persist：/search/album 的在线专辑登记传 True——这类锚点不落在任何持久快照里，
    重启后无法由 warm 重建，必须落盘（其余曲目衍生登记不需要，避免写放大）。
    """
    src = source_from_online_guid(track_guid)
    if not src:
        return
    fake = fake_official_guid(f"{track_guid}:album")
    entry = _FAKE_ALBUM_REGISTRY.get(fake)
    if entry is None:
        _FAKE_ALBUM_REGISTRY[fake] = {
            "source": src,
            "album": str(album_name or "").strip(),
            "track_guid": track_guid,
            "item": dict(item) if isinstance(item, dict) and item else {},
            "album_id": str(album_id or ""),
            "persist": bool(persist),
        }
        if persist:
            _persist_album_registry()
        return
    # 已登记：残缺快照可被更全的后到登记覆盖——首次解析失败时 build_metadata_payload
    # 会用 stub（仅 id/source，issue #28 真机复现）先占位，若"首个非空即终"，
    # 之后的全量信息永远进不来，专辑合成/封面/命名全部拿到残缺条目。
    if album_name and not entry.get("album"):
        entry["album"] = str(album_name).strip()
    if album_id and not entry.get("album_id"):
        entry["album_id"] = str(album_id)
    if isinstance(item, dict) and item:
        cur = entry.get("item") if isinstance(entry.get("item"), dict) else {}
        if _item_meta_richness(item) > _item_meta_richness(cur):
            entry["item"] = dict(item)
    if persist:
        # 更新路径同样尝试落盘（节流 5s/次；更全的专辑名/album_id/item 需要跟着持久化）
        if not entry.get("persist"):
            entry["persist"] = True
        _persist_album_registry()


def resolve_fake_album(candidate: str) -> dict | None:
    """客户端回传的专辑伪装 guid → 登记条目；非伪装专辑（含官方 guid）返回 None。"""
    if not candidate:
        return None
    fake = candidate[6:] if candidate.startswith("track_") else candidate
    if not re.fullmatch(r"[0-9a-f]{32}", fake or ""):
        return None
    ensure_registry_warm()
    entry = _FAKE_ALBUM_REGISTRY.get(fake)
    if entry is None:
        # 专辑登记丢失（如旧会话仅命中曲目反查表）：按确定性映射重建最小条目
        real = _FAKE_GUID_REVERSE.get(fake) or ""
        if real.endswith(":album") and is_online_guid(real):
            track_guid = real[: -len(":album")]
            entry = {
                "source": source_from_online_guid(track_guid),
                "album": "",
                "track_guid": track_guid,
                "item": {},
            }
            _FAKE_ALBUM_REGISTRY[fake] = entry
    return entry or None


def album_entry_from_real_guid(real_guid: str) -> "dict | None":
    """真实形态专辑 guid（"online:...:album"）→ 登记条目；封面拦截按 cover_url 直出用。"""
    raw = str(real_guid or "")
    if not raw.endswith(":album"):
        return None
    fake = fake_official_guid(raw)
    return _FAKE_ALBUM_REGISTRY.get(fake)


def _register_fakes_from_items(items) -> None:
    for it in items or []:
        if not isinstance(it, dict):
            continue
        g = str(it.get("guid") or "")
        nested = it.get("track") if isinstance(it.get("track"), dict) else None
        if is_online_guid(g):
            fake_official_guid(g)
            # 专辑假 id 一并登记（issue #22）：收藏/历史/歌单附加/推荐缓存里的
            # 快照带专辑名，重建时保住专辑详情的反解依据
            src_obj = nested if nested is not None else it
            register_fake_album(g, _album_name_from_obj(src_obj), _snapshot_to_info(src_obj))
        elif nested is not None:
            _register_fakes_from_items([nested])


def _iter_registry_jsons(directory: str):
    """列出目录下的 json 文件（含一层子目录，兼容 recommend_cache/<user>/x.json）。"""
    try:
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if name.endswith(".json") and os.path.isfile(path):
                yield path
            elif os.path.isdir(path):
                for sub in sorted(os.listdir(path)):
                    if sub.endswith(".json"):
                        yield os.path.join(path, sub)
    except Exception:
        return


def _register_fakes_from_recommend_bundle(data) -> None:
    if not isinstance(data, dict):
        return
    _register_fakes_from_items(data.get("tracks"))
    playlist = data.get("playlist")
    if isinstance(playlist, dict):
        for key in ("guid", "coverId"):
            g = str(playlist.get(key) or "")
            if is_online_guid(g):
                fake_official_guid(g)


def _register_fakes_from_plt(data) -> bool:
    """playlist_tracks 存储：items 是 {歌单guid: [条目]} 字典形状，逐桶注册曲目假 id。"""
    if isinstance(data, dict) and isinstance(data.get("items"), dict):
        for bucket in data["items"].values():
            _register_fakes_from_items(bucket)
        return True
    return False


def ensure_registry_warm() -> None:
    """从收藏/历史/歌单附加/推荐缓存重建 fake→real 映射（假 id 是确定性 md5，可完整重建）。

    服务重启后内存注册表为空，而客户端仍持有重启前学到的假 id；此时上报的
    播放/收藏事件若反解失败会被当作官方事件透传而丢失。收藏与历史存储里
    出现过的 guid 覆盖客户端会回传的曲目假 id；歌单附加条目同理；推荐缓存
    里的歌单/曲目 guid 覆盖客户端会回传的歌单封面假 id（歌单 coverId 也走伪装下发）。
    """
    global _REGISTRY_WARMED
    if _REGISTRY_WARMED:
        return
    _REGISTRY_WARMED = True
    for directory in (CONF.get("fav_dir") or os.path.join(_HOME, "online_favorites"),
                      CONF.get("plt_dir") or os.path.join(_HOME, "playlist_tracks"),
                      dailyrec.play_history_dir(),
                      dailyrec.recommend_cache_dir(),
                      nmpl.cache_dir()):
        for path in _iter_registry_jsons(directory):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            if isinstance(data, dict) and data.get("tracks") is not None:
                _register_fakes_from_recommend_bundle(data)
                continue
            if _register_fakes_from_plt(data):
                continue
            _register_fakes_from_items(data.get("items") if isinstance(data, dict) else data)
    # /search/album 的在线专辑锚点（不在任何快照存储里）从落盘登记恢复
    try:
        _load_album_registry_persisted()
    except Exception as e:
        logger.debug("album registry restore failed: %s", type(e).__name__)


def resolve_real_guid(candidate: str) -> str:
    if not candidate:
        return candidate
    if candidate in _FAKE_GUID_REVERSE:
        return _FAKE_GUID_REVERSE[candidate]
    if candidate.startswith("track_") and len(candidate) > 6:
        stripped = candidate[6:]
        if stripped in _FAKE_GUID_REVERSE:
            return _FAKE_GUID_REVERSE[stripped]
        if re.fullmatch(r"[0-9a-f]{32}", stripped):
            ensure_registry_warm()
            if stripped in _FAKE_GUID_REVERSE:
                return _FAKE_GUID_REVERSE[stripped]
    if re.fullmatch(r"[0-9a-f]{32}", candidate or ""):
        ensure_registry_warm()
        if candidate in _FAKE_GUID_REVERSE:
            return _FAKE_GUID_REVERSE[candidate]
    return candidate


def disguise_client_json(obj):
    """递归把客户端可见数据里的 online: 前缀 id 全部替换为官方 32-hex 形态。

    所有下发出口（搜索/元数据/歌词/收藏/历史/每日推荐曲目）统一伪装；
    入口（extract_guid/create/delete/event）经 resolve_real_guid 反解回真实 guid。
    coverId 按官方惯例带 track_ 前缀；字符串内嵌的 guid（如 audioSpec.path）
    一并替换。纯内存变换，不落盘、不触碰官方库，restore 后自然消失。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and v.startswith("online:"):
                if k in ("coverId", "cover_id"):
                    out[k] = "track_" + fake_official_guid(v)
                else:
                    out[k] = fake_official_guid(v)
            else:
                out[k] = disguise_client_json(v)
        return out
    if isinstance(obj, list):
        return [disguise_client_json(x) for x in obj]
    if isinstance(obj, str):
        return _ONLINE_ID_RE.sub(lambda m: fake_official_guid(m.group(0)), obj)
    return obj


def extract_guid(request: Request, path_guid: str | None = None) -> str:
    if path_guid:
        return resolve_real_guid(path_guid)
    return resolve_real_guid(
        request.query_params.get("guid")
        or request.query_params.get("trackGUID")
        or request.query_params.get("trackGuid")
        or request.query_params.get("coverId")
        or request.query_params.get("id")
        or request.query_params.get("trackId")
        or ""
    )


async def extract_guid_from_body(request: Request) -> str:
    guid = extract_guid(request)
    if guid:
        return guid
    try:
        body = await request.json()
    except Exception:
        return ""
    if isinstance(body, dict):
        return resolve_real_guid(str(
            body.get("guid")
            or body.get("trackGUID")
            or body.get("trackGuid")
            or body.get("id")
            or body.get("trackId")
            or ""
        ))
    return ""


def empty_ok() -> JSONResponse:
    return JSONResponse(content={"code": 0, "msg": "ok", "data": {}})


def build_lyric_list_payload(guid: str, lyric_text: str) -> dict:
    """对齐飞牛 $n.lyric.list → xr(list, preferred)。

    每条需有非空 content；source=2 表示 EXTERNAL_LRC（非内嵌，不强制 offset）。
    """
    text = (lyric_text or "").strip()
    if not text:
        return {"code": 0, "msg": "ok", "data": {"list": [], "preferred": ""}}
    lyric_guid = f"{guid}:lyric"
    now = int(time.time())
    item = {
        "guid": lyric_guid,
        "content": text,
        "source": 2,
        "isLRC": True,
        "offset": 0,
        "createdAt": now,
        "updatedAt": now,
    }
    return {
        "code": 0,
        "msg": "ok",
        "data": {"list": [item], "preferred": lyric_guid},
    }


def stub_online_info(guid: str) -> dict:
    song_id = song_id_from_online_guid(guid)
    return {
        "id": song_id,
        "source": source_from_online_guid(guid),
        "title": "",
        "artist": "",
        "album": "",
        "duration_s": 0,
        "ext": "mp3",
        "file_size": 0,
        "cover_url": "",
        "lyric": "",
    }


def build_local_metadata_payload(guid: str, entry: dict) -> dict:
    """本地曲目（local:file:…）的 metadata 应答。

    形状对齐 ``build_metadata_payload``：飞牛客户端会无防护读 track.genres.join /
    album / artists，缺字段直接抛错 → 播放器跳过、连 stream 都不请求。
    duration 也必须给真值（早先恒为 0，客户端据此判定不可播）。
    """
    title = str(entry.get("title") or "") or _stem_of(entry.get("path"))
    artist = str(entry.get("artist") or "")
    album = str(entry.get("album") or "") or "本地曲库"
    duration_s = float(entry.get("duration") or 0)
    duration_ms = int(entry.get("duration_ms") or duration_s * 1000)
    size = int(entry.get("size") or 0)
    ext = str(entry.get("ext") or "mp3")
    play_format = play_format_from_ext(ext)
    artists = [{"name": artist, "guid": f"{guid}:artist"}] if artist else []
    album_obj = {
        "name": album,
        "guid": f"{guid}:album",
        "artists": artists,
        "coverId": guid,
    }
    # ⚠️ 两处必须与在线曲目（build_online_track）严格一致，否则客户端拒绝播放：
    #   1) duration 单位是**毫秒**（在线版 "duration": duration_ms）；
    #      早期这里填秒，客户端把 240 读成 240 毫秒 → 判定不可播。
    #   2) audioSpec.path 必须带真实后缀（"飞牛 ll() 用 path 解析 extension"）；
    #      早期这里整个 audioSpec 是另起炉灶的简版，缺 path 就拿不到容器格式。
    #
    # v2.9.12：path 改用**真实绝对路径**（/vol1/.../许嵩 - 庐州月.flac），
    # 不再用 local/<sha1>.<ext> 这种假路径。理由：
    #   * 后缀照样真实，ll() 解析 extension 不受影响；
    #   * 客户端「文件位置」显示的就是这个字段，假路径对用户毫无意义；
    #   * 播放走 /track/stream 拦截（按 guid 反查路径），本来就不依赖它。
    # 索引里万一没存路径，退回假路径保底，绝不留空（空 path = 拿不到容器格式）。
    _sha = guid.split("local:file:", 1)[-1]
    real_path = str(entry.get("path") or "")
    spec_path = real_path or f"local/{_sha}.{play_format}"
    audio_spec = {
        "path": spec_path,   # 真实绝对路径（客户端「文件位置」显示的就是它）
        "format": play_format,
        "codec": play_format,
        "container": play_format,
        "duration": duration_ms,
        "size": size,
        "channel": int(entry.get("channels") or 2) or 2,
        "sampleRate": int(entry.get("sample_rate") or 44100) or 44100,
        "bitDepth": 16 if play_format in ("wav", "flac", "aiff") else None,
        "bitrate": int(entry.get("bitrate") or 0)
        or (1411000 if play_format in ("flac", "wav", "ape", "wv") else 320000),
    }
    audio_spec = {k: v for k, v in audio_spec.items() if v is not None}
    track = {
        "guid": guid,
        "id": guid,
        # 顶层也给一份真实路径：不同版本客户端读的字段不一样，
        # audioSpec.path / path / filePath 都给上，哪个被读到都是真实路径。
        "path": spec_path,
        "filePath": real_path or spec_path,
        "title": title,
        "name": title,
        "artist": artist,
        "artists": artists,
        "album": album_obj,
        "albumName": album,
        "audioSpec": audio_spec,
        "duration": duration_ms,
        "duration_ms": duration_ms,
        "durationMs": duration_ms,
        "duration_s": duration_s,
        "codec": play_format,
        "codecName": play_format,
        "format": play_format,
        "ext": ext,
        "size": size,
        "file_size": size,
        "coverId": guid,
        "cover_url": "",
        "coverUrl": "",
        "coverURL": "",
        "source": "local",
        "is_online": False,
        "isFavorite": False,
        "isCue": False,
        "hasLyric": False,
        "genres": [],
        "accessStatus": 0,
    }
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            **track,
            "guid": guid,
            "id": guid,
            "album": album_obj,
            "audioSpec": audio_spec,
            "track": track,
        },
    }


def _stem_of(path: str | None) -> str:
    try:
        return os.path.splitext(os.path.basename(str(path or "")))[0].strip()
    except Exception:  # noqa: BLE001
        return ""


def build_metadata_payload(guid: str, data: dict | None) -> dict:
    """飞牛 resolveTrackPlayback._h() 会无防护读取 data.track.genres.join / album / artists。

    缺 genres 或 album 不是对象时直接抛错，播放器跳过且不会请求 stream。
    """
    info = dict(data or {})
    info.setdefault("id", song_id_from_online_guid(guid))
    info.setdefault("source", source_from_online_guid(guid))
    vo = build_online_track(info)
    album_obj = vo["album"] if isinstance(vo.get("album"), dict) else {
        "name": str(vo.get("album") or ""),
        "guid": f"{guid}:album",
        "artists": vo.get("artists") or [],
        "coverId": guid,
    }
    track = {
        "guid": guid,
        "id": guid,
        "title": vo.get("title") or "",
        "artists": vo.get("artists") or [],
        "album": album_obj,
        "genres": list(vo.get("genres") or []),
        "duration": vo.get("duration") or 0,
        "coverId": guid,
        "coverUrl": vo.get("coverUrl") or "",
        "format": vo.get("format") or "mp3",
        "hasLyric": bool(vo.get("hasLyric") or info.get("lyric")),
        "isFavorite": False,
        "isCue": False,
        "accessStatus": 0,
        "audioSpec": vo["audioSpec"],
    }
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            **vo,
            "guid": guid,
            "id": guid,
            "album": album_obj,
            "audioSpec": vo["audioSpec"],
            "track": track,
        },
    }


def _conf_log_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(part in lowered for part in _REDACT_KEY_PARTS):
        return "***" if value else ""
    return value


def _background_jobs_enabled() -> bool:
    """后台任务只在真实服务环境启用（takeover 注入 FNMUSIC_BACKGROUND_JOBS=1）；
    默认关闭，单元测试与手动导入 app 永不触发，避免误扫真实曲库目录。
    """
    return os.environ.get("FNMUSIC_BACKGROUND_JOBS", "").lower() in ("1", "true", "yes")


async def _lyric_orphan_sweeper() -> None:
    """启动即清一次、之后每 30 分钟复扫：清掉本插件写进曲库、音频已消失的孤儿
    .lrc（安全边界见 cache_gc.sweep_orphan_lyrics，用户自有歌词永不触碰）。
    """
    while True:
        try:
            dirs: list[str] = []
            for d in (tee_save_dir(), detect_library_dir()):
                if d and d not in dirs and d != CONF["cache_dir"]:
                    dirs.append(d)
            removed = await asyncio.to_thread(sweep_orphan_lyrics, CONF["cache_dir"], dirs)
            if removed:
                logger.info("orphan lyric sweep removed %d file(s): %s", len(removed), removed)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("orphan lyric sweep failed: %s", e)
        await asyncio.sleep(1800)


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    logger.info("=== fnmusic-ext v%s configuration ===", get_version())
    for k, v in CONF.items():
        logger.info("  %s = %s", k, _conf_log_value(k, v))
    logger.info("  llm_enabled = %s", dailyrec.llm_enabled())
    logger.info("==================================")

    sweeper_task = asyncio.create_task(_lyric_orphan_sweeper()) if _background_jobs_enabled() else None
    env_task = asyncio.create_task(_env_watch_loop()) if (
        _background_jobs_enabled() and CONF.get("env_watch", True)
    ) else None
    created_upstream = False
    created_musicdl = False
    created_musicbox = False
    created_lx = False
    created_llm = False
    created_cdn = False

    if getattr(fastapi_app.state, "upstream_client", None) is None:
        fastapi_app.state.upstream_client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=CONF["upstream_sock"]),
            base_url="http://unix",
            timeout=30.0,
        )
        created_upstream = True

    if getattr(fastapi_app.state, "musicdl_client", None) is None:
        fastapi_app.state.musicdl_client = httpx.AsyncClient(
            base_url=CONF["musicdl_url"],
            timeout=45.0,
        )
        created_musicdl = True

    if getattr(fastapi_app.state, "musicbox_client", None) is None:
        fastapi_app.state.musicbox_client = httpx.AsyncClient(
            base_url=CONF["musicbox_url"],
            timeout=20.0,
        )
        created_musicbox = True

    if getattr(fastapi_app.state, "lx_client", None) is None:
        fastapi_app.state.lx_client = httpx.AsyncClient(
            base_url=CONF["lx_url"],
            timeout=25.0,
        )
        created_lx = True

    if getattr(fastapi_app.state, "llm_client", None) is None:
        fastapi_app.state.llm_client = httpx.AsyncClient(timeout=dailyrec.LLM_TIMEOUT_S)
        created_llm = True

    if getattr(fastapi_app.state, "cdn_client", None) is None:
        fastapi_app.state.cdn_client = _new_cdn_client()
        created_cdn = True

    # W6：频道歌单缓存定时刷新（同 G，仅在真实服务环境 + 网易云启用时启动）。
    stop_event = asyncio.Event()
    refresh_task: asyncio.Task | None = None
    auth_task: asyncio.Task | None = None
    if _background_jobs_enabled() and CONF.get("netease_enabled"):
        refresh_task = asyncio.create_task(_playlist_refresh_loop(fastapi_app, stop_event))
        # W13：登录态巡检（掉线 / VIP 临期 → PushPlus 提醒），与 G 的 lifespan 一致。
        # pushplus.send(None, ...) 内部会自建 httpx 客户端（proxy/pushplus.py:263），
        # 所以不需要 app.state.push_client；巡检失败绝不能拖垮主链路启动。
        try:
            auth_task = netease_auth.start_watch(fastapi_app.state.musicbox_client, stop_event)
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动网易云登录态巡检失败: %s: %s", type(exc).__name__, exc)

    try:
        yield
    finally:
        stop_event.set()
        if auth_task is not None:
            netease_auth.stop_watch()
            auth_task.cancel()
            await asyncio.gather(auth_task, return_exceptions=True)
        if refresh_task is not None:
            refresh_task.cancel()
            await asyncio.gather(refresh_task, return_exceptions=True)
        tasks = [entry["task"] for entry in _SEARCH_CACHE.values() if entry.get("task") and not entry["task"].done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if sweeper_task:
            sweeper_task.cancel()
            await asyncio.gather(sweeper_task, return_exceptions=True)
        if env_task:
            env_task.cancel()
            await asyncio.gather(env_task, return_exceptions=True)
        try:
            await tc.shutdown_all()
        except Exception:  # noqa: BLE001
            pass
        if created_upstream and getattr(fastapi_app.state, "upstream_client", None):
            await fastapi_app.state.upstream_client.aclose()
            fastapi_app.state.upstream_client = None
        if created_musicdl and getattr(fastapi_app.state, "musicdl_client", None):
            await fastapi_app.state.musicdl_client.aclose()
            fastapi_app.state.musicdl_client = None
        if created_musicbox and getattr(fastapi_app.state, "musicbox_client", None):
            await fastapi_app.state.musicbox_client.aclose()
            fastapi_app.state.musicbox_client = None
        if created_lx and getattr(fastapi_app.state, "lx_client", None):
            await fastapi_app.state.lx_client.aclose()
            fastapi_app.state.lx_client = None
        if created_llm and getattr(fastapi_app.state, "llm_client", None):
            await fastapi_app.state.llm_client.aclose()
            fastapi_app.state.llm_client = None
        if created_cdn and getattr(fastapi_app.state, "cdn_client", None):
            await fastapi_app.state.cdn_client.aclose()
            fastapi_app.state.cdn_client = None


app = FastAPI(title="fnmusic-ext", lifespan=lifespan)


@app.middleware("http")
async def log_client_requests(request: Request, call_next):
    """记录 /music/ 请求的方法/路径/状态/UA，供 App 端兼容问题远程定位。

    不记 query（guid 无必要），UA 折叠空白并截断，配合 takeover 日志白名单的固定格式。
    """
    response = await call_next(request)
    if request.url.path.startswith("/music/"):
        ua = re.sub(r"\s+", " ", request.headers.get("user-agent") or "-")[:100]
        logger.info(
            "client request %s %s status=%s ua=%s",
            request.method,
            request.url.path,
            response.status_code,
            ua,
        )
    return response


@app.middleware("http")
async def observe_quality_signals(request: Request, call_next):
    """W3 接线：动态音质打开时，被动观察 /music/api/ 请求里的网络/音质线索。

    默认关闭时这里只做一次开关判断就放行，A 的请求路径与历史实现完全一致；
    观察本身是旁路，任何异常都不允许影响播放。
    """
    if quality.dynamic_enabled() and request.url.path.startswith("/music/api/"):
        try:
            quality.observe_request(request.method, request.url.path,
                                    request.query_params, request.headers)
        except Exception as e:  # noqa: BLE001 - 观察失败绝不能影响请求
            logger.debug("quality observe failed: %s", e)
    return await call_next(request)


@app.get("/_ext/quality")
async def ext_quality_report():
    """W3：动态音质决策 + 被动观察证据的诊断快照（只读，始终可用）。"""
    try:
        return {"ok": True, "data": quality.report(db_path=resolve_music_db())}
    except Exception as exc:  # noqa: BLE001 - 诊断端点不该 500
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "data": {}}


@app.get("/_ext/prefetch")
async def ext_prefetch_report():
    """下一首预热的诊断快照（W4 移植 G v2.9.30）。只读，始终可用。"""
    try:
        return {"ok": True, "data": prefetch.status()}
    except Exception as exc:  # noqa: BLE001 - 诊断端点不该 500
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "data": {}}


@app.get("/_ext/authorized")
async def ext_authorized(request: Request):
    """飞牛「应用授权目录」状态快照（W13 移植 G v2.9.4）。

    把「网关在不在 / token 有没有 / 授权了哪些目录 / 当前曲库是否覆盖」一次摊开，
    管理页据此提示去哪里点授权。只读；``refresh=1`` 才强制重探网关。
    """
    try:
        if request.query_params.get("refresh") in ("1", "true", "yes"):
            trimgw.invalidate_cache()
        rep = trimgw.authorized_report(force=request.query_params.get("refresh") in ("1", "true", "yes"))
        lib = detect_library_dir()
        rep["library_dir"] = lib
        rep["library_is_cache_fallback"] = bool(lib) and os.path.abspath(lib) == os.path.abspath(
            str(CONF["cache_dir"]))
        rep["strict"] = _strict_authorization()
        rep["env_share_paths"] = trimgw.env_share_paths()
        return {"ok": True, "data": rep}
    except Exception as exc:  # noqa: BLE001 - 诊断端点不该 500
        logger.warning("authorized diag failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "data": {}}


@app.get("/_ext/localfirst")
async def ext_local_first():
    """本地曲库优先的状态快照（W13 移植 G v2.9.14）。

    这个模块之前最大的问题不是逻辑错，而是**无法自证**：索引空了、匹配没命中，
    界面上一点迹象都没有，看起来就是「没做」。这里把索引构成、来源、最近查询
    结果全部摊开。只读，不触发扫描。
    """
    try:
        lib = detect_library_dir()
        rep = local_library.status(resolve_music_db(), lib)
        rep["policy_level"] = ""
        rep["hint"] = ""
        if not rep["enabled"]:
            rep["hint"] = "已在管理页关闭「本地曲库优先」。"
        elif not rep["entries"]:
            rep["hint"] = ("索引是空的，本地优先永远不会命中：" + (rep["empty_reason"] or "")
                           + "。请确认「本地曲库目录」填对、且已在应用设置里授权该目录。")
        elif rep["lookups"] and not rep["lookup_hits"]:
            rep["hint"] = ("索引有歌但一次都没匹配上——多半是标题写法对不上（网易云的标题"
                           "带「(Live)」「- Remaster」等后缀）。把「最近查询」贴给开发者即可定位。")
        return {"ok": True, "data": rep}
    except Exception as exc:  # noqa: BLE001 - 诊断端点不该 500
        logger.warning("local first diag failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "data": {}}


@app.get("/_ext/lxsync")
async def ext_lx_sync(refresh: int = 0):
    """洛雪音乐同步服务器歌单同步的状态快照（只读；`refresh=1` 时顺带拉一次）。

    这个功能同样是「连不上/没配对」时界面上毫无迹象的类型：歌单不出现，用户只能猜。
    这里把地址、是否配了密码、上次成功时间、每张歌单的可播曲目数与最近错误摊开。
    不回显密码，只回「有没有配」。
    """
    try:
        if refresh and lxsync.sync_enabled():
            await lxsync.peek_summaries()
        rep = lxsync.status()
        rep["hint"] = ""
        if not rep["enabled"]:
            rep["hint"] = "未开启：管理页勾上「同步洛雪歌单」并填服务地址与密码。"
        elif rep["error"]:
            rep["hint"] = f"上次同步失败：{rep['error']}。歌单列表会继续显示上一次成功的结果。"
        elif not rep["playlists"]:
            rep["hint"] = ("已连上但没有歌单：确认洛雪客户端里已经往这个同步服务推过数据"
                           "（客户端 → 设置 → 同步 → 上传）。")
        return {"ok": True, "data": rep}
    except Exception as exc:  # noqa: BLE001 - 诊断端点不该 500
        logger.warning("lx sync diag failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "data": {}}


@app.post("/_ext/cache/invalidate")
async def ext_cache_invalidate():
    """清空代理侧缓存（搜索 + 每日推荐 + 登录态 + 直链短缓存）。

    代理与管理页面是两个独立进程：页面里扫码登录成功，只重置了页面进程自己的
    登录态。代理这边仍会拿旧的 `_SEARCH_CACHE` 和旧的 `netease_auth` 登录态
    （TTL 默认 300s）继续服务几分钟，表现为"明明登录了，搜索还是只有本地歌曲"。
    所以登录成功后必须显式打这个端点（契约测试见
    proxy/tests/test_admin_ui.py::test_proxy_exposes_the_endpoint_admin_ui_calls）。

    只做丢弃内存/推荐缓存这一件事：不改配置、不影响播放、可重复调用。
    """
    dropped_search = len(_SEARCH_CACHE)
    _reset_search_cache()          # A 的既有语义：连同在飞的搜索任务一起取消
    daily_tasks = len(_DAILY_TASKS)
    _cancel_daily_tasks()
    # 已缓存的每日推荐歌单是按"未登录"或旧账号生成的，必须一并作废
    purged_daily = 0
    try:
        root = dailyrec.recommend_cache_dir()
        if os.path.isdir(root):
            for user_dir in os.listdir(root):
                sub = os.path.join(root, user_dir)
                if os.path.isdir(sub):
                    for name in os.listdir(sub):
                        if name.endswith(".json"):
                            try:
                                os.remove(os.path.join(sub, name))
                                purged_daily += 1
                            except OSError:
                                pass
    except OSError as exc:
        logger.warning("purge daily cache failed: %s", exc)

    try:
        netease_auth.invalidate_state()
        login_state = True
    except Exception as exc:  # noqa: BLE001 - 登录态辅助失败不能连累整个清缓存
        logger.warning("netease_auth.invalidate_state failed: %s", exc)
        login_state = False
    # W6：频道歌单清单内存缓存 + 在飞的后台刷新任务一并作废
    purged_channels = len(_channel_recs_cache)
    _channel_recs_cache.clear()
    for _task in list(_channel_recs_refresh.values()):
        if not _task.done():
            _task.cancel()
    _channel_recs_refresh.clear()
    purged_urls = invalidate_url_cache()
    logger.info(
        "cache invalidated: search=%d daily_tasks=%d daily_files=%d urls=%d channels=%d login_state=%s",
        dropped_search, daily_tasks, purged_daily, purged_urls, purged_channels, login_state,
    )
    return {
        "ok": True,
        "cleared": {
            "search_entries": dropped_search,
            "daily_tasks": daily_tasks,
            "daily_cache_files": purged_daily,
            # A 没有在线元信息缓存（G 独有）。保留键位与 G 一致，让调用方拿到的
            # 响应形状稳定；值恒为 0。
            "online_info_entries": 0,
            "play_urls": purged_urls,
            "channel_lists": purged_channels,
            "login_state": login_state,
        },
    }


@app.get("/_ext/playlists/preview")
async def ext_playlists_preview():
    """歌单管理页预览（W6，移植 G v2.9.30）：当前口径会被注入的频道歌单 + 缓存状态。"""
    client = get_musicbox_client(app)
    logged_in = await _netease_logged_in()
    items: list[dict] = []
    if CONF.get("recommend_daily", True) and logged_in:
        items.append({
            "guid": playlists.DAILY_NS + "preview",
            "name": f"每日推荐 {dailyrec.today_key()}",
            "channel": "daily",
            "track_count": 0,
        })
    channel_recs: list[dict] = []
    try:
        channel_recs, _keep, _complete = await _channel_playlist_records(client)
    except Exception as exc:  # noqa: BLE001 - 预览接口不该 500
        logger.warning("playlist preview: channel records failed: %s: %s", type(exc).__name__, exc)
    for rec in channel_recs:
        items.append({
            "guid": rec.get("guid"),
            "name": rec.get("name") or "网易云歌单",
            "channel": rec.get("channel") or "",
            "track_count": int(rec.get("track_count") or 0),
        })
    stamped_order = playlists.channel_order()
    items.sort(
        key=lambda it: stamped_order.index(it["channel"]) if it["channel"] in stamped_order else len(stamped_order)
    )
    items = playlists.apply_explicit_order(items)
    current_items = [it for it in items if str(it.get("channel") or "") != "daily"]
    cached_count = sum(
        1 for it in current_items if playlists.load_cached_tracks(str(it.get("guid") or "")) is not None
    )
    return {
        "ok": True,
        "data": {
            "items": [{**it, "is_daily": it["channel"] == "daily"} for it in items],
            "logged_in": logged_in,
            "manual_order": list(playlists.explicit_order_tokens()),
            "cache": {
                "cached": cached_count,
                "total": len(current_items),
                "ttl_s": playlists.tracks_cache_ttl(),
                "refresh_at": playlists.refresh_time_of_day(),
                "warming": _PLAYLIST_WARMING,
            },
        },
    }


@app.post("/_ext/playlists/warm")
async def ext_playlists_warm():
    """手动预热按钮（W6）：刷新当前口径下所有频道歌单的曲目缓存。"""
    client = get_musicbox_client(app)
    try:
        recs, keep, complete = await _channel_playlist_records(client)
    except Exception as exc:  # noqa: BLE001 - 清单拿不到就当空集
        logger.warning("预热按钮：拉取当前歌单清单失败: %s: %s", type(exc).__name__, exc)
        recs, keep, complete = [], set(), False
    if complete and keep:
        playlists.forget_stale(keep)
    guids = [str(r.get("guid") or "") for r in recs if r.get("guid")]
    if not _schedule_playlist_warm(app, force=True, guids=guids):
        return {"ok": True, "data": {"started": False, "reason": "already_running"}}
    return {"ok": True, "data": {"started": True, "total": len(guids)}}


@app.get("/_ext/livez")
async def ext_livez():
    return {"ok": True, "service": "fnmusic-ext", "pid": os.getpid()}


@app.get("/_ext/healthz")
async def ext_healthz(request: Request):
    async def probe(name: str, client: httpx.AsyncClient, path: str) -> dict:
        try:
            response = await asyncio.wait_for(client.get(path, timeout=2.0), timeout=2.4)
            healthy = response.status_code < 500 if name == "upstream" else response.status_code == 200
            detail: dict = {"status": "ok" if healthy else "fail", "http_status": response.status_code}
            if name != "upstream" and healthy:
                try:
                    payload = response.json()
                    if isinstance(payload, dict):
                        detail["dependency"] = payload
                        if payload.get("ok") is False:
                            detail["status"] = "fail"
                    else:
                        detail["status"] = "fail"
                except ValueError:
                    detail["status"] = "fail"
                    detail["error"] = "invalid health JSON"
            return detail
        except Exception as exc:
            return {"status": "fail", "error": type(exc).__name__}

    checks = [("upstream", True, get_upstream_client, "/music/api/v1/search/track?keyword=healthz_probe"),
              ("musicdl", CONF.get("musicdl_enabled"), get_musicdl_client, "/healthz"),
              ("musicbox", CONF.get("netease_enabled"), get_musicbox_client, "/healthz"),
              ("lxmusic", CONF.get("lx_enabled"), get_lx_client, "/healthz")]
    enabled = [(name, getter, path) for name, on, getter, path in checks if on]
    results = await asyncio.gather(*(probe(name, getter(request.app), path) for name, getter, path in enabled))
    details = {name: {"status": "disabled"} for name, on, _, _ in checks if not on}
    details.update({name: result for (name, _, _), result in zip(enabled, results)})
    statuses = {name: value["status"] for name, value in details.items()}
    failed = [name for name, status in statuses.items() if status == "fail"]
    source_ok = any(statuses[name] == "ok" for name in ("musicdl", "musicbox", "lxmusic"))
    return {"ok": statuses["upstream"] == "ok" and source_ok, "version": get_version(),
            **statuses, "llm": "enabled" if dailyrec.llm_enabled() else "disabled",
            # W13：掉线提醒通道状态（与 G 的 healthz 同字段名，供管理页展示）
            "pushplus": "enabled" if pushplus.enabled() else "disabled",
            "recommend": {
                "mode": "per-user: source-slot -> llm -> local-random",
                "netease": bool(CONF.get("netease_enabled")),
                "lx": bool(CONF.get("lx_enabled")),
                "llm": "enabled" if dailyrec.llm_enabled() else "disabled",
                "source_slot_claimed": dailyrec.source_slot_claimed(),
                "recent": dailyrec.last_recommend_summary(),
            },
            "degraded": bool(failed), "failures": failed, "details": details}


@app.get("/music/api/v1/search/track")
@app.get("/music/api/v1/search/track/{subpath:path}")
async def search_track(request: Request):
    upstream_client = get_upstream_client(request.app)
    musicdl_client = get_musicdl_client(request.app)
    musicbox_client = get_musicbox_client(request.app)
    keyword = extract_keyword(request)
    if keyword:
        _note_user_keyword(_credential_scope(request), keyword)

    page_str = request.query_params.get("page")
    try:
        page = int(page_str) if page_str else 1
    except (TypeError, ValueError):
        page = 1
    if page < 1:
        page = 1

    size_str = request.query_params.get("size")
    try:
        size = int(size_str) if size_str else 50
    except (TypeError, ValueError):
        size = 50
    if size < 1:
        size = 50

    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)

    req = upstream_client.build_request("GET", url_path, headers=headers)
    upstream_resp = await upstream_client.send(req)

    resp_headers = filter_headers(upstream_resp.headers, exclude_keys={"content-length", "content-encoding"})

    if upstream_resp.status_code != 200:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    try:
        upstream_json = upstream_resp.json()
    except Exception:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    if not isinstance(upstream_json, dict) or upstream_json.get("code") != 0:
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    if not keyword:
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    scope = _credential_scope(request)
    if _USER_SEARCH_WORD.get(scope) != keyword:
        return JSONResponse(content=disguise_client_json(upstream_json), status_code=upstream_resp.status_code, headers=resp_headers)

    key = _search_scope(request) + ":" + keyword
    _clean_search_cache()
    entry = _SEARCH_CACHE.get(key)
    if entry is None:
        entry = {"items": [], "ts": 0, "keyword": keyword,
                 "scope": _search_scope(request), "credentials": scope, "config": _source_config(), "task": None}
        _set_search_cache(key, entry)
    entry["gen"] = _USER_SEARCH_GEN.get(scope, 0)
    entry["superseded"] = False
    entry["accessed"] = time.time()
    task = entry.get("task")
    if (not task or task.done()) and time.time() - entry["ts"] >= _search_ttl(entry):
        task = asyncio.create_task(_aggregate_search(request, keyword, entry))
        entry["task"] = task
    # 在线搜索只有一个超时。有在线条目就立刻回复（本地在前、在线在后）；
    # 到点仍没有，就放弃这次还没回来的结果，只交本地列表。
    if task and not task.done():
        budget = max(0.05, float(CONF["search_timeout"])) + max(0.0, float(CONF.get("search_debounce_s") or 0))
        deadline = asyncio.get_running_loop().time() + budget
        while not task.done() and asyncio.get_running_loop().time() < deadline:
            if entry.get("items"):
                break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await asyncio.wait({task}, timeout=min(0.05, remaining))
        if not entry.get("items") and not task.done():
            entry["abandoned"] = True
            entry["items"] = []
            entry["partial"] = True
            entry["ts"] = time.time()
            task.cancel()
    local_list = ensure_search_list(upstream_json)
    local_keys = {(title_from_track(x), artist_from_track(x)) for x in local_list}
    total_online = sum(1 for x in entry["items"] if (title_from_track(x), artist_from_track(x)) not in local_keys)
    original_total = upstream_json.get("data", {}).get("total", len(local_list))
    if not isinstance(original_total, int):
        original_total = len(local_list)
    # 官方搜索越界页不返回空列表而是钳制回第 1 页（total=4 时 page=2 仍返回
    # 同样 4 条，收藏/歌单条目接口无此行为）。本页全局起点已越过官方段时必须
    # 清空官方列表，否则官方条目会拼上在线切片在每个后续页重复出现。
    if (page - 1) * size >= original_total and local_list:
        local_list.clear()
    # 官方搜索忽略 size 全量返回结果集（total=11 时 size=10 的 page=1/2 均返回
    # 同样 11 条，2026-09-25 官方更新实测）：返回条数超过单页窗口说明拿到的是
    # 全量列表而非请求页，按请求窗口原地切片，保证本地段全局分页不跨页重复。
    if len(local_list) > size:
        win_start = (page - 1) * size
        local_list[:] = local_list[win_start:win_start + size]
    # 本地优先全局布局：本地条目占据全局前 local_total 位，在线条目紧随其后。
    # 本页在线切片 = 全局分页区间与在线区间的交集；纯本地页（区间未触及在线段）
    # 在线切片为空，上游结果原样透传，只有 total 计入在线条数驱动客户端继续翻页。
    selected = _online_window(entry, page, size, original_total)
    merged = merge_online_tracks(upstream_json, selected, page=1, size=size, selected=True)
    merged["data"]["total"] = original_total + total_online
    fav_set = await _online_favorite_set(request)
    if fav_set and isinstance(merged.get("data"), dict) and isinstance(merged["data"].get("list"), list):
        for it in merged["data"]["list"]:
            if isinstance(it, dict) and str(it.get("guid") or "") in fav_set:
                it["isFavorite"] = True
    return JSONResponse(content=disguise_client_json(merged), status_code=upstream_resp.status_code, headers=resp_headers)


async def _aggregate_search(request: Request, keyword: str, entry: dict) -> None:
    scope = entry.get("credentials") or _credential_scope(request)
    if _user_search_stale(entry, scope):
        entry["items"] = []
        entry["superseded"] = True
        entry["ts"] = 0
        return
    window = await _wait_search_debounce(scope)
    if _user_search_stale(entry, scope) or (window is not None and _SEARCH_DEBOUNCE.get(scope) is not window):
        entry["items"] = []
        entry["superseded"] = True
        entry["ts"] = 0
        return
    token = _FETCH_SCOPE.set(scope)
    sources = []
    if CONF.get("netease_enabled"):
        sources.append(fetch_musicbox_search(get_musicbox_client(request.app), keyword, CONF["netease_search_limit"]))
    if CONF.get("musicdl_enabled"):
        sources.append(fetch_musicdl_search(get_musicdl_client(request.app), keyword, CONF["online_limit"]))
    if CONF.get("lx_enabled"):
        sources.append(fetch_lx_search(get_lx_client(request.app), keyword, CONF["lx_search_limit"]))
    tasks = [asyncio.create_task(coro) for coro in sources]
    pending = set(tasks)
    partial = False
    results: dict[asyncio.Task, list] = {}
    try:
        deadline = asyncio.get_running_loop().time() + max(0.05, float(CONF["search_timeout"]))
        while pending and not _user_search_stale(entry, scope):
            done, pending = await asyncio.wait(pending, timeout=max(0, deadline - asyncio.get_running_loop().time()), return_when=asyncio.FIRST_COMPLETED)
            if not done:
                partial = True
                break
            for task in tasks:
                if task not in done:
                    continue
                try:
                    data = task.result()
                except Exception:
                    data = None
                if entry.get("abandoned") or data is _SUPERSEDED or _user_search_stale(entry, scope):
                    if data is _SUPERSEDED or _user_search_stale(entry, scope):
                        entry["superseded"] = True
                    entry["items"] = []
                    results[task] = []
                    continue
                partial |= data is None or bool(getattr(data, "partial", False)) or (isinstance(data, dict) and bool(data.get("errors") or data.get("ok") is False))
                items = data.get("items", []) if isinstance(data, dict) else (data or [])
                results[task] = items
                if not entry["items"]:
                    ordered = [item for source_task in tasks for item in results.get(source_task, [])]
                else:
                    ordered = entry["items"] + items
                entry["items"] = deduplicate_online_items(ordered)[:2000]
        if entry.get("abandoned") or _user_search_stale(entry, scope):
            entry["items"] = []
            if _user_search_stale(entry, scope):
                entry["superseded"] = True
                entry["ts"] = 0
            elif not entry.get("ts"):
                entry["partial"] = True
                entry["ts"] = time.time()
            return
        entry["partial"] = partial
        # 全源失败（source busy/熔断/异常）交回的 0 条不是真"无结果"：不落缓存时间戳，
        # 同词紧跟着重搜会立即重新聚合，而不是吃 10 秒空缓存一直返回空。
        entry["ts"] = 0 if partial and not entry.get("items") else time.time()
    finally:
        _FETCH_SCOPE.reset(token)
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if entry.get("abandoned"):
                entry["items"] = []
                entry["partial"] = True
                if not entry.get("ts"):
                    entry["ts"] = time.time()


def _online_window(entry: dict, page: int, size: int, local_total: int) -> list[dict]:
    """本地优先布局下取本页的在线切片。

    全局布局：[本地 0..local_total) [在线 local_total..local_total+len(items))。
    本页全局区间 = [(page-1)*size, page*size)；与在线段求交集后映射到 items 下标。
    items 只追加不重排，同一 (page,size,local_total) 的切片稳定；items 后续
    增长只会让更靠后的页多出条目（已返回页的前缀不动）。
    """
    start_global = (page - 1) * size
    end_global = page * size
    start = max(0, start_global - local_total)
    end = max(0, end_global - local_total)
    if start >= end:
        return []
    window = [item for item in entry["items"][start:end] if _source_enabled(online_guid_from_item(item))]
    return window


@app.get("/music/api/v1/search/suggest")
@app.get("/music/api/v1/search/suggest/{subpath:path}")
async def search_suggest(request: Request):
    if not CONF["merge_suggest"]:
        return await forward_to_upstream(request, get_upstream_client(request.app))

    upstream_client = get_upstream_client(request.app)
    musicdl_client = get_musicdl_client(request.app)
    keyword = extract_keyword(request)

    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)

    musicdl_task: asyncio.Task | None = None
    suggest_scope = _FETCH_SCOPE.set(_credential_scope(request) + ":suggest")
    if keyword and CONF.get("musicdl_enabled"):
        musicdl_task = asyncio.create_task(fetch_musicdl_search(musicdl_client, keyword, 5))
    _FETCH_SCOPE.reset(suggest_scope)

    req = upstream_client.build_request("GET", url_path, headers=headers)
    upstream_resp = await upstream_client.send(req)
    resp_headers = filter_headers(upstream_resp.headers, exclude_keys={"content-length", "content-encoding"})

    if upstream_resp.status_code != 200:
        if musicdl_task:
            musicdl_task.cancel()
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    try:
        upstream_json = upstream_resp.json()
    except Exception:
        if musicdl_task:
            musicdl_task.cancel()
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    if not isinstance(upstream_json, dict) or upstream_json.get("code") != 0:
        if musicdl_task:
            musicdl_task.cancel()
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    musicdl_data = None
    if musicdl_task:
        try:
            musicdl_data = await asyncio.wait_for(asyncio.shield(musicdl_task), timeout=10.0)
        except Exception as e:
            logger.warning("Suggest musicdl error: %s", e)
            musicdl_task.cancel()

    data_field = upstream_json.get("data")
    if isinstance(data_field, list) and isinstance(musicdl_data, dict) and musicdl_data.get("items"):
        for item in musicdl_data.get("items", [])[:5]:
            title = item.get("title")
            if title and title not in data_field:
                data_field.append(title)

    return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)


def _lookup_online_snapshot(guid: str) -> "dict | None":
    """按 guid 在在线播放历史/在线收藏的用户快照里反查曲目元数据（issue #28）。

    两个存储都按用户分文件且量小（历史 ≤500 条/用户），tee 转正是低频路径，
    顺序扫描可接受；命中即返回含 title/artist/album 的快照 dict。
    """
    # ① 在线播放历史（play_history/<user>.json，条目最新的在末尾，倒序找）
    try:
        root = dailyrec.play_history_dir()
        for name in sorted(os.listdir(root)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(root, name), encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            items = data.get("items") if isinstance(data, dict) else data
            if not isinstance(items, list):
                continue
            for it in reversed(items):
                if isinstance(it, dict) and it.get("guid") == guid:
                    snap = it.get("track") if isinstance(it.get("track"), dict) else None
                    if snap and (snap.get("title") or snap.get("artist")):
                        return snap
    except OSError:
        pass
    # ② 在线收藏（online_favorites/<user>.json，形状同收藏下发条目）
    try:
        fav_root = CONF.get("fav_dir") or os.path.join(_HOME, "online_favorites")
        for name in sorted(os.listdir(fav_root)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(fav_root, name), encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            items = data.get("items") if isinstance(data, dict) else data
            if not isinstance(items, list):
                continue
            for it in reversed(items):
                if isinstance(it, dict) and it.get("guid") == guid:
                    snap = it.get("track") if isinstance(it.get("track"), dict) else None
                    if snap and (snap.get("title") or snap.get("artist")):
                        return snap
    except OSError:
        pass
    # ③ 歌单附加条目（playlist_tracks/<user>.json，{"items": {歌单guid: [条目]}}）：
    # 只加过歌单、从未收藏/播放的歌也要能反查（官方绑定与 tee 兜底的元数据来源）
    try:
        plt_root = plt_dir()
        for name in sorted(os.listdir(plt_root)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(plt_root, name), encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            buckets = data.get("items") if isinstance(data, dict) else None
            if not isinstance(buckets, dict):
                continue
            for bucket in buckets.values():
                if not isinstance(bucket, list):
                    continue
                for it in reversed(bucket):
                    if isinstance(it, dict) and it.get("guid") == guid:
                        snap = it.get("track") if isinstance(it.get("track"), dict) else None
                        if snap and (snap.get("title") or snap.get("artist")):
                            return snap
    except OSError:
        pass
    return None


def _track_in_list(tracks: Any, guid: str) -> dict | None:
    if not isinstance(tracks, list):
        return None
    for track in tracks:
        if not isinstance(track, dict) or str(track.get("guid") or "") != guid:
            continue
        title, artist, _album = _tag_fields(track)
        if title or artist:
            return track
    return None


def _lookup_playlist_cache_track(guid: str) -> dict | None:
    """推荐歌单、网易账号歌单的磁盘缓存按 guid 反查下发曲目。

    这两类列表不进搜索缓存。缓存里的曲目是前端形态（album 为对象），
    调用方必须用 _tag_fields 取标签，不能 str(album)。
    """
    if not guid:
        return None
    for root in (dailyrec.recommend_cache_dir(), nmpl.cache_dir()):
        for path in _iter_registry_jsons(root):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            hit = _track_in_list(data.get("tracks"), guid)
            if hit:
                return hit
    return None


def _merge_missing_tags(title: str, artist: str, album: str, snap: dict) -> tuple[str, str, str]:
    """只补空字段。已经是纯字符串的专辑不被缓存里的另一份专辑覆盖。"""
    snap_title, snap_artist, snap_album = _tag_fields(snap)
    return title or snap_title, artist or snap_artist, album or snap_album


def _tee_metadata_fallback(
    guid: str, title: str, artist: str, album: str, dest_info: dict | None = None,
) -> tuple[str, str, str]:
    """tee 落盘元数据缺失时的回查兜底，避免以 unknown 进曲库。

    顺序：推荐/网易账号歌单缓存（首播时历史往往还没写）→ 播放历史/收藏/自建歌单。
    缺哪个字段补哪个；已有的纯字符串专辑保持不变。封面 URL 只在调用方还没有时写入 dest_info。
    """
    title, artist, album = (title or "").strip(), (artist or "").strip(), (album or "").strip()
    need_cover = not _cover_url_of(dest_info)
    if not (title and artist and album and not need_cover):
        filled = False
        for snap in (_lookup_playlist_cache_track(guid), _lookup_online_snapshot(guid)):
            if not snap:
                continue
            title, artist, album = _merge_missing_tags(title, artist, album, snap)
            if need_cover and isinstance(dest_info, dict):
                cover = _cover_url_of(snap)
                if cover:
                    dest_info["cover_url"] = cover
                    need_cover = False
            filled = True
            if title and artist and album and not need_cover:
                break
        if filled:
            logger.info("tee metadata fallback hit for %s: %s - %s", guid, artist, title)
    if not (title and artist):
        logger.warning(
            "tee finalize missing metadata for %s (title=%r artist=%r)：将以 unknown 命名落盘，"
            "该曲目的元数据解析链路需要排查", guid, title, artist,
        )
    return title, artist, album


def _tee_finalize(part: str, guid: str, ext: str, info: dict | None, tee_enabled: bool) -> dict | None:
    """落盘转正：边听边存开→进曲库（定名/权限/标签，歌词仅在 FNMUSIC_LYRIC_AUTO_DL
    开启时随迁），关→进滚动缓存（不写歌词）。

    part 必须已完整写好且长度校验通过；成功后由调用方触发曲库扫描通知。
    无损档解码校验不过直接丢弃（上游损坏流绝不入库）并拉黑该 guid。
    返回实际落盘元数据 {"dest","title","artist","album"}（官方绑定按此匹配官方曲库）。
    """
    if str(ext or "").lower() in _LOSSLESS_EXTS and not _audio_file_ok(part):
        _lossless_blacklist(guid)
        logger.warning("tee finalize rejected corrupt lossless for %s", guid)
        try:
            os.remove(part)
        except OSError:
            pass
        return None
    src = dict(info or {})
    title, artist, album = _tag_fields(src)
    if tee_enabled:
        title, artist, album = _tee_metadata_fallback(guid, title, artist, album, src)
        dest = library_media_path(guid, title, ext, artist=artist, directory=tee_save_dir())
        os.replace(part, dest)
        remember_media_path(guid, dest)
        adopt_library_perms(dest)
        write_audio_tags(dest, title, artist, album)
        # 自动下载封面：音乐文件已完整落库才走到这里（下载失败根本进不了
        # finalize），封面字节直接内嵌进音频——不产生独立封面文件，无孤儿
        cover_url = _cover_url_of(src)
        if cover_url and CONF.get("auto_cover", True):
            try:
                fetched = download_cover_bytes(cover_url)
                if fetched and embed_audio_cover(dest, fetched[0], fetched[1]):
                    logger.info("cover embedded for %s: %s", guid, dest)
            except Exception as e:
                logger.debug("cover embed step skipped for %s: %s", guid, e)
        # 自动下载歌词开启时，无音频时代落在 cache 的影子歌词跟随音频进曲库，词曲贴身
        if CONF.get("lyric_auto_dl"):
            promote_shadow_lyric(guid, dest)
    else:
        # 边听边存关闭：只写滚动缓存（cache_safe_guid 命名，find_cache_file 精确名可命中）
        dest = os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.{ext}")
        os.replace(part, dest)
    logger.info("tee finalize done for %s: dest=%s", guid, dest)
    # 歌词只随"音乐完整落库成功"写入（tee 转正即到此处），且仅在自动下载歌词
    # 开启时；下载失败根本进不了 finalize，绝不产生先落歌词的孤儿文件
    if tee_enabled and CONF.get("lyric_auto_dl"):
        lyric = str(src.get("lyric") or src.get("lrc") or "")
        if lyric.strip():
            write_lyric_cache(guid, lyric, title, artist)
    if not tee_enabled:
        try:
            purge_rolling(CONF["cache_dir"], keep=int(CONF.get("tee_cache_max", 2)))
        except Exception as e:
            logger.warning("Rolling cache purge failed: %s", e)
    return {"dest": dest, "title": title, "artist": artist, "album": album}


async def _auto_lyric_after_finalize(request: Request, guid: str, meta: dict | None) -> None:
    """音乐完整落库成功后补齐歌词（FNMUSIC_LYRIC_AUTO_DL，默认关）。

    仅边听边存转正路径调用，且必须排在 _schedule_library_scan 之前——保证
    官方扫描入库前 .lrc 已落在音频旁。本地已有歌词（含影子提升产物）零请求；
    缺失时向源站补拉（酷我上游接口已失效返回空即跳过）。下载失败不会走到
    这里，不会产生只有歌词没有音频的孤儿文件。
    """
    if not CONF.get("lyric_auto_dl"):
        return
    dest = str((meta or {}).get("dest") or "")
    if not dest or _is_rolling_cache_stem(dest, guid):
        return
    if find_lyric_file(guid):
        return
    try:
        text = await asyncio.wait_for(resolve_online_lyric(request, guid), timeout=10.0)
    except Exception as e:
        logger.info("auto lyric fetch failed for %s: %s", guid, type(e).__name__)
        return
    if text:
        logger.info("auto lyric saved for %s (%d chars)", guid, len(text))
    else:
        logger.info("auto lyric unavailable for %s (source returned no lyric)", guid)


_SCAN_TTL_S = 90.0
_SCAN_MERGE_S = 3.0
_scan_last_attempt = 0.0
_scan_task: "asyncio.Task | None" = None


def _schedule_library_scan(headers: dict | None) -> None:
    """落盘进曲库后通知官方重扫（FNMUSIC_LIBRARY_SCAN_PATH 为空=禁用）。

    官方扫描接口无公开资料：路径可配置、默认禁用；3 秒合并窗口（连续落盘多首
    只发一次）+ 90 秒 TTL（成功与失败都节流，路径配错不会风暴）。凭证头取自
    触发请求（copy_incoming_headers），只在任务内一次性使用，不落盘。
    """
    global _scan_task, _scan_last_attempt
    if not str(CONF.get("library_scan_path") or "").strip() or not headers:
        return
    if time.monotonic() - _scan_last_attempt < _SCAN_TTL_S:
        return
    if _scan_task is not None and not _scan_task.done():
        return

    async def _run() -> None:
        global _scan_last_attempt
        await asyncio.sleep(_SCAN_MERGE_S)
        scan_path = str(CONF.get("library_scan_path") or "").strip()
        if not scan_path:
            return
        _scan_last_attempt = time.monotonic()
        try:
            client = get_upstream_client(app)
            req = client.build_request("POST", scan_path, headers=dict(headers))
            resp = await client.send(req)
            if resp.status_code == 200:
                logger.info("Library scan triggered via %s", scan_path)
            else:
                logger.warning(
                    "Library scan %s returned %s (check FNMUSIC_LIBRARY_SCAN_PATH)",
                    scan_path, resp.status_code,
                )
        except Exception as e:
            logger.warning("Library scan trigger failed: %s", e)

    _scan_task = asyncio.create_task(_run())


def stream_tee_response(
    resp: httpx.Response,
    guid: str,
    range_header: str | None,
    coro_factory: Callable[[], Coroutine[Any, Any, Any]] | None = None,
    client_to_close: httpx.AsyncClient | None = None,
    resolved_ext: str | None = None,
    pre_info: dict | None = None,
    chunks: Any = None,
    first_chunk: bytes = b"",
    scan_headers_factory: Callable[[], dict] | None = None,
    post_finalize: Callable[[dict | None], Coroutine[Any, Any, None]] | None = None,
) -> Response:
    headers = {"Accept-Ranges": "bytes"}
    for key in ("content-type", "content-length", "content-range"):
        if resp.headers.get(key):
            headers[key] = resp.headers[key]
    # 容器格式以**字节**为准（见 _sniff_audio_ext）：CDN 的 content-type 经常是 audio/mpeg
    # 而实际是 FLAC；只信它就会把无损标成 MP3，播放器解码不出来只能一直转圈。
    if _starts_at_zero(resp, range_header):
        sniffed = _sniff_audio_ext(first_chunk)
        if sniffed:
            resolved_ext = sniffed
    if resolved_ext:
        headers["content-type"] = media_type_for_ext(resolved_ext)
    length = resp.headers.get("content-length", "")
    expected = int(length) if length.isdigit() else None
    full_resource = resp.status_code == 200
    if resp.status_code == 206:
        # Content-Length proves only this segment, not the entire recording.
        match = re.fullmatch(r"bytes\s+0-(\d+)/(\d+)", resp.headers.get("content-range", "").strip(), re.I)
        full_resource = bool(match and int(match[1]) + 1 == int(match[2]) and int(match[2]) > 0)
        if full_resource:
            total = int(match[2])
            full_resource = expected is None or expected == total
            expected = total
    ext = resolved_ext or ext_from_content_type(resp.headers.get("content-type", ""))

    async def body() -> AsyncGenerator[bytes, None]:
        # Pull-through provides backpressure: no unbounded producer queue and no
        # downloader outliving its consumer. Only a clean EOF may finalize.
        part = None
        fp = None
        written = 0
        eof = False
        info_task = None
        tee_active_token = False
        upstream_aborted = False
        try:
            tee_enabled = bool(CONF.get("tee_save_enabled"))
            if should_cache(range_header) and full_resource:
                directory = tee_save_dir() if tee_enabled else CONF["cache_dir"]
                os.makedirs(directory, exist_ok=True)
                part = os.path.join(directory, f"{cache_safe_guid(guid)}.{uuid4().hex}.part")
                fp = open(part, "wb")
                tee_active_token = _tee_active_acquire(guid)
                if pre_info is None and coro_factory:
                    info_task = asyncio.create_task(coro_factory())
            iterator = chunks if chunks is not None else resp.aiter_bytes()
            if first_chunk:
                if fp:
                    fp.write(first_chunk)
                written += len(first_chunk)
                yield first_chunk
            try:
                async for chunk in iterator:
                    if chunk:
                        if fp:
                            fp.write(chunk)
                        written += len(chunk)
                        yield chunk
            except Exception as exc:
                # An aborted stream must not enter the cache, but it must leave
                # a trace: CDN stalls mid-file were previously invisible in the
                # journal. Client disconnects raise CancelledError (not caught
                # here) and stay silent on purpose.
                upstream_aborted = True
                logger.warning("Stream aborted mid-way for %s: %s", guid, type(exc).__name__)
                raise
            eof = True
            if fp:
                fp.close()
                fp = None
            if part and eof and written >= 1024 and (expected is None or written == expected):
                # 无论客户端连接此时是否已关闭（curl 接收完直接 EOF 退出，Starlette 会 aclose 生成器），
                # 完整的音频已全部接收完毕，落盘与标签写入必须受 shield 保护完整执行完毕，
                # 且立即解绑 part，绝不能被 GeneratorExit / CancelledError 提前打断导致 part 在 finally 中被误删。
                to_finalize = part
                part = None
                with anyio.CancelScope(shield=True):
                    info = pre_info
                    if info is None and info_task:
                        try:
                            info = await asyncio.wait_for(info_task, timeout=8.0)
                        except Exception:
                            info = None
                    try:
                        meta = await asyncio.to_thread(_tee_finalize, to_finalize, guid, ext, info, tee_enabled)
                        # 自动下载歌词：补拉必须在通知官方扫描之前完成（入库即带歌词）
                        if post_finalize is not None and meta:
                            await post_finalize(meta)
                        scan_headers = scan_headers_factory() if scan_headers_factory is not None else None
                        if scan_headers:
                            _schedule_library_scan(scan_headers)
                        if meta and tee_enabled:
                            # 官方绑定意图（收藏/歌单）统一在下载落库完成后调度
                            _dispatch_official_binding(guid, scan_headers, meta)
                    except Exception as exc:
                        logger.warning("tee finalize execution failed for %s: %s", guid, type(exc).__name__)
                        if os.path.exists(to_finalize):
                            try:
                                os.remove(to_finalize)
                            except OSError:
                                pass
        finally:
            if fp:
                fp.close()
                fp = None
            resume_part = None
            if part and os.path.exists(part):
                # 切歌/客户端退出不中断下载：未写完的 part 交接给后台续传任务完成
                # （仅边听边存开启的整轨流且非上游错误；written 不足或续传满载时
                # 按旧语义删除，上游流错误绝不进续传）
                if (tee_enabled and full_resource and not upstream_aborted
                        and written >= 1024 and _tee_handoff_slot_available()):
                    resume_part = part
                    part = None
                else:
                    try:
                        os.remove(part)
                    except OSError:
                        pass
                    part = None
            with anyio.CancelScope(shield=True):
                if tee_active_token:
                    _tee_active_release(guid)
                if info_task and not info_task.done():
                    info_task.cancel()
                    await asyncio.gather(info_task, return_exceptions=True)
                await resp.aclose()
                if client_to_close:
                    await client_to_close.aclose()
                if resume_part:
                    _register_tee_handoff(guid, resume_part, written, expected, ext, scan_headers_factory)

    return StreamingResponse(body(), status_code=resp.status_code, headers=headers)


def _credential_scope(request: Request) -> str:
    return hashlib.sha256(json.dumps([request.headers.get(k, "") for k in
        ("cookie", "authorization", "x-trim-music-temp-token")]).encode()).hexdigest()


def _retained_track(request: Request, guid: str) -> tuple[dict | None, dict | None]:
    _clean_search_cache()
    for entry in reversed(list(_SEARCH_CACHE.values())):
        if entry.get("credentials") != _credential_scope(request) or entry.get("config", _source_config()) != _source_config():
            continue
        for item in entry.get("items", []):
            if online_guid_from_item(item) == guid:
                return item, entry
            for alternative in item.get("_alternatives", []):
                if online_guid_from_item(alternative) == guid:
                    return alternative, entry
    return None, None


async def _recover_source(request: Request, guid: str, entry: dict | None) -> bool:
    """One bounded source re-search after a backend loses its in-memory IDs."""
    if not entry or not _source_enabled(guid):
        return False
    source = source_from_online_guid(guid)
    recovery_key = "recovered:" + source
    if time.monotonic() - entry.get(recovery_key, -1000) < 30:
        return False
    entry[recovery_key] = time.monotonic()
    keyword = entry.get("keyword", "")
    token = _FETCH_SCOPE.set(_credential_scope(request) + ":play")
    try:
        if source == "netease":
            coro = fetch_musicbox_search(get_musicbox_client(request.app), keyword, CONF["netease_search_limit"])
        elif source == "lx":
            # 按目标 GUID 的平台精确重搜（GUID 第 3 段），对齐 musicdl 分支按引擎重搜的行为
            parts = guid.split(":")
            coro = fetch_lx_search(get_lx_client(request.app), keyword, CONF["lx_search_limit"],
                                   sources=[parts[2]] if len(parts) >= 4 else None)
        else:
            selected = [name.strip() for name in str(CONF.get("online_sources") or "").split(",")
                        if name.strip().lower().removesuffix("musicclient") == source.lower()]
            coro = fetch_musicdl_search(get_musicdl_client(request.app), keyword, CONF["online_limit"],
                                       ",".join(selected) or source)
        try:
            result = await asyncio.wait_for(coro, timeout=3.0)
        except Exception:
            return False
        if result is _SUPERSEDED or not result:
            return False
        items = result.get("items", []) if isinstance(result, dict) else result
        if not isinstance(items, list):
            return False
        return any(online_guid_from_item(item) == guid for item in items)
    finally:
        _FETCH_SCOPE.reset(token)


async def _open_online_stream(request: Request, guid: str, range_header: str | None,
                              force_mp3: bool = False):
    """Resolve and read first bytes before committing HTTP headers to the client."""
    source = source_from_online_guid(guid)
    info, _ = _retained_track(request, guid)
    headers = {"Accept-Encoding": "identity"}
    if range_header:
        headers["Range"] = range_header
    via_musicdl = False
    owned = None
    resp = None
    ext = None
    try:
        if source in ("netease", "lx"):
            if source == "netease":
                # 带 stats ⇒ 播放链路启用直链短缓存：预热灌进来的链在这里零往返命中，
                # 命中与否也会被记录（供统计区分 warm/cold）。
                url = await resolve_netease_url(
                    get_musicbox_client(request.app),
                    song_id_from_online_guid(guid).split(":")[-1],
                    request,
                    stats={},
                )
                if not url:
                    return None
            else:
                resolved = await resolve_lx_url(get_lx_client(request.app), song_id_from_online_guid(guid))
                if not resolved:
                    return None
                url = resolved["url"]
                ext = resolved.get("ext")
                for key, value in (resolved.get("headers") or {}).items():
                    if key.lower() in ("referer", "user-agent"):
                        headers[key] = str(value)
            if info is None:
                try:
                    info = await asyncio.wait_for(_fetch_online_info(request, guid), timeout=0.75)
                except Exception as exc:
                    logger.info("online info fast-path missed for %s: %s", guid, type(exc).__name__)
            ext = ext or (info or {}).get("ext")
            # 连接复用（移植 G v2.9.30）：一次播放客户端要发十几个 Range 请求，
            # 每请求新建 client = 每个 Range 重做一次 TCP+TLS 握手。改成进程级共享
            # 客户端 + 有界连接池后 owned 保持 None，响应迭代器结束时只关 resp；
            # 未被读完的连接由 httpx 丢弃而不是放回池子，不会污染后续请求。
            # Read timeout 仍须容忍 CDN 中途限速（read=60s，见 _new_cdn_client）：
            # 扁平 10s 会在长暂停处杀掉流，表现为播到 70-80% 卡住。
            client = get_cdn_client(request.app)
            req = client.build_request("GET", url, headers=headers)
        else:
            client = get_musicdl_client(request.app)
            via_musicdl = True
            params = {"id": song_id_from_online_guid(guid), "proxy": "true"}
            # 无损档已被判定损坏的曲目（或上层明确要求）直接取 mp3 档；
            # 带断点续传的请求不切档——前后字节必须来自同一条流
            if (force_mp3 or _lossless_is_blacklisted(guid)) and not range_header:
                params["quality"] = "mp3"
            req = client.build_request("GET", "/stream", params=params, headers=headers)
        resp = await client.send(req, stream=True)
        content_type = resp.headers.get("content-type", "").lower()
        if (resp.status_code not in (200, 206)
                or any(x in content_type for x in ("text/", "json"))
                or resp.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity")):
            # Reject servers ignoring identity: decoded bytes cannot use encoded
            # Content-Length/Range offsets, and must not enter the audio cache.
            return None
        chunks = resp.aiter_bytes()
        first = await anext(chunks, b"")
        if not first:
            return None
        # 仅 musicdl 通道会服务端透明降级（坏无损流→官方 mp3 档）：以实际
        # content-type 为准修正扩展名，避免 mp3 字节按 .flac 命名入库。
        # 网易/洛雪直链相反——上游会错报 audio/mpeg，扩展名以解析结果为准。
        if via_musicdl and "audio/mpeg" in content_type and (not ext or ext in _LOSSLESS_EXTS):
            ext = "mp3"
        result = (resp, owned, ext, info, chunks, first)
        resp = owned = None  # transfer ownership to response iterator
        return result
    finally:
        if resp:
            await resp.aclose()
        if owned:
            await owned.aclose()


_FULL_FETCH_COOLDOWN_S = 1800.0
_full_fetch_tasks: "dict[str, asyncio.Task]" = {}
_full_fetch_failed: "dict[str, float]" = {}

# --- 损坏流防护（2026-09-29 实测）-----------------------------------------
# kuwo 官方 CDN 的无损档交付不可解码字节（fLaC 头与 Content-Length 都正常，
# musicdl 库探活只验 URL/扩展名），坏文件一旦入库，官方转码器编到坏点即报
# "transcoding error"、App 端表现为转码下载失败。对策：入库前全量解码校验，
# 坏流丢弃并按 guid 拉黑无损档——此后该曲目取流自动降级 mp3 档（musicdl
# 服务 /stream?quality=mp3）。
_LOSSLESS_EXTS = {"flac", "wav", "ape", "m4a", "alac", "ogg", "opus"}
_LOSSLESS_BAD: "dict[str, float]" = {}
_LOSSLESS_BAD_TTL_S = 6 * 3600.0


def _lossless_blacklist(guid: str) -> None:
    _LOSSLESS_BAD[guid] = time.monotonic()


def _lossless_is_blacklisted(guid: str) -> bool:
    ts = _LOSSLESS_BAD.get(guid)
    if ts is None:
        return False
    if time.monotonic() - ts > _LOSSLESS_BAD_TTL_S:
        _LOSSLESS_BAD.pop(guid, None)
        return False
    return True


def _audio_file_ok(path: str) -> bool:
    """完整音频文件的解码校验：ffmpeg 无任何错误输出才算通过。

    只对已通过长度校验的完整文件调用——截断的完好文件同样会报解码错误，
    报错文本无法区分二者，完整性必须由字节数校验先行保证。
    ffmpeg 不可用（测试守卫/宿主机缺件）时无从校验，只能放行。
    """
    if not tc.FFMPEG_BIN:
        return True
    try:
        r = subprocess.run(
            [tc.FFMPEG_BIN, "-v", "error", "-nostdin", "-i", path, "-f", "null", "-"],
            capture_output=True, text=True, timeout=300,
        )
        return r.returncode == 0 and not (r.stderr or "").strip()
    except Exception:  # noqa: BLE001
        return False


def _prune_full_fetch_state(now: float) -> None:
    if len(_full_fetch_failed) > 1000:
        for g in [g for g, at in _full_fetch_failed.items() if now - at >= _FULL_FETCH_COOLDOWN_S]:
            del _full_fetch_failed[g]


def _register_background_fetch(request: Request, guid: str, gate_key: str) -> bool:
    """后台整轨下载通用注册逻辑，按 gate_key 指定的 CONF 键做门禁。"""
    if not CONF.get(gate_key) or find_cache_file(guid):
        return False
    if guid in _tee_active:
        # tee 流式下载同 guid 进行中：不重复下载，tee 转正/切歌续传路径会调度后续动作
        return False
    task = _full_fetch_tasks.get(guid)
    if task is not None and not task.done():
        return False
    now = time.monotonic()
    _prune_full_fetch_state(now)
    if now - _full_fetch_failed.get(guid, -_FULL_FETCH_COOLDOWN_S) < _FULL_FETCH_COOLDOWN_S:
        return False
    headers = copy_incoming_headers(request)
    _full_fetch_tasks[guid] = asyncio.create_task(_full_fetch_download(guid, headers))
    return True


def _register_full_fetch(request: Request, guid: str) -> None:
    """定长窗口拉流客户端的边听边存兜底：注册后台整轨下载，客户端请求照常服务。

    should_cache 只认"无 Range / bytes=0-"，窗口内核（如 bytes=0-1048575 一段）
    永远过不了 tee 写盘门；由服务端另起整轨下载落盘补齐。同 guid 去重、
    失败后冷却期内不重试，防止坏源反复打上游。
    """
    _register_background_fetch(request, guid, "tee_save_enabled")


def _register_fav_autobind(request: Request, guid: str, user_guid: str = "",
                           playlist_guid: str | None = None) -> None:
    """收藏/加入歌单的在线歌曲自动绑定本地：登记官方绑定意图并确保整轨下载落库。

    下载不与边听边存重复：tee 正在流式下载或后台任务在途时只登记意图，由对应
    完成路径（tee 转正 / 后台整轨下载 / 切歌续传）统一调度官方绑定。文件已在
    库时无需下载，直接尝试官方绑定（官方可能已扫入库）。
    """
    if not CONF.get("fav_auto_bind"):
        return
    headers = copy_incoming_headers(request)
    if user_guid:
        _register_bind_intent(guid, user_guid, headers, playlist_guid)
    if find_cache_file(guid):
        _dispatch_official_binding(guid, headers, None)
        return
    _register_background_fetch(request, guid, "fav_auto_bind")


async def _info_for_background_save(request: Request, guid: str, info: dict | None) -> dict:
    """后台整轨/续传落盘前补齐元数据。

    本地推荐歌单、网易账号歌单、历史/收藏里已有标题就用那份，不再打源站。
    都没有时再拉一次歌曲信息（数秒，不占播放起播的 0.75 秒窗口）。
    """
    base = dict(info or {})
    if _tag_fields(base)[0]:
        return base
    local = _lookup_playlist_cache_track(guid) or _lookup_online_snapshot(guid)
    if local and _tag_fields(local)[0]:
        merged = dict(local)
        if base.get("ext"):
            merged["ext"] = base["ext"]
        if base.get("lyric"):
            merged["lyric"] = base["lyric"]
        return merged
    try:
        fetched = await asyncio.wait_for(_fetch_online_info(request, guid), timeout=4.0)
    except Exception as exc:
        logger.info("background online info missed for %s: %s", guid, type(exc).__name__)
        return base
    if isinstance(fetched, dict) and _tag_fields(fetched)[0]:
        if base.get("ext") and not fetched.get("ext"):
            fetched = dict(fetched)
            fetched["ext"] = base["ext"]
        return fetched
    logger.info("background online info empty for %s", guid)
    return base


async def _full_fetch_download(guid: str, cred_headers: dict, force_mp3: bool = False) -> None:
    """后台整轨下载 online guid 并落盘（独立于客户端连接，不占播放路径预算）。

    无损档解码校验失败时按 guid 拉黑并自动以 mp3 档重试一次（服务端
    quality=mp3），两次都坏才宣告失败——坏字节绝不入库。
    """
    part = None
    resp = None
    owned = None
    try:
        # 合成最小 Request scope（触发请求的凭证头 + app 实例），完整复用在线取
        # 流的解析/元数据/首字节校验逻辑；Range 传 None 保证拿到完整资源。
        fake_request = _synth_request(cred_headers)
        opened = await _open_online_stream(fake_request, guid, None, force_mp3=force_mp3)
        if not opened:
            raise RuntimeError("open failed")
        resp, owned, ext, info, chunks, first = opened
        expected = None
        length = resp.headers.get("content-length", "")
        if length.isdigit():
            expected = int(length)
        directory = tee_save_dir()
        os.makedirs(directory, exist_ok=True)
        part = os.path.join(directory, f"{cache_safe_guid(guid)}.{uuid4().hex}.part")
        written = 0
        with open(part, "wb") as fp:
            if first:
                fp.write(first)
                written += len(first)
            async for chunk in chunks:
                if chunk:
                    fp.write(chunk)
                    written += len(chunk)
        if written < 1024 or (expected is not None and written != expected):
            raise RuntimeError(f"size mismatch written={written} expected={expected}")
        info = await _info_for_background_save(fake_request, guid, info)
        ext = ext or (info or {}).get("ext") or "mp3"
        if ext in _LOSSLESS_EXTS and not await asyncio.to_thread(_audio_file_ok, part):
            _lossless_blacklist(guid)
            logger.warning("audio decode check failed for %s (ext=%s bytes=%d)", guid, ext, written)
            # 坏流不落库：关掉当前连接后改要 mp3 档重来一次
            with anyio.CancelScope(shield=True):
                if resp:
                    await resp.aclose()
                if owned:
                    await owned.aclose()
                resp = owned = None
            if not force_mp3:
                logger.warning("retrying %s with mp3 tier after corrupt lossless stream", guid)
                await _full_fetch_download(guid, cred_headers, force_mp3=True)
                return
            raise RuntimeError("corrupt stream even at mp3 tier")
        meta = await asyncio.to_thread(_tee_finalize, part, guid, ext, info or {}, True)
        part = None
        logger.info("Background full fetch saved %s (%d bytes)", guid, written)
        await _auto_lyric_after_finalize(fake_request, guid, meta)
        _schedule_library_scan(cred_headers)
        _dispatch_official_binding(guid, cred_headers, meta)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _full_fetch_failed[guid] = time.monotonic()
        logger.warning("Background full fetch failed for %s: %s", guid, type(e).__name__)
    finally:
        if part and os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
        with anyio.CancelScope(shield=True):
            if resp:
                await resp.aclose()
            if owned:
                await owned.aclose()
        if _full_fetch_tasks.get(guid) is asyncio.current_task():
            del _full_fetch_tasks[guid]


# === 边听边存 tee 活跃跟踪 + 切歌续传交接 ===
# 需求：开启边听边存后听歌即下载，切歌不中断下载；收藏/加歌单与正在进行的
# tee 下载不重复拉流。

_tee_active: "dict[str, int]" = {}
_tee_handoff_active: "set[str]" = set()


def _tee_active_acquire(guid: str) -> bool:
    _tee_active[guid] = _tee_active.get(guid, 0) + 1
    return True


def _tee_active_release(guid: str) -> None:
    left = _tee_active.get(guid, 0) - 1
    if left > 0:
        _tee_active[guid] = left
    else:
        _tee_active.pop(guid, None)


def _tee_handoff_slot_available() -> bool:
    """续传并发上限（FNMUSIC_TEE_HANDOFF_MAX，0=关闭）：防快速跳歌形成下载洪泛。"""
    try:
        limit = int(CONF.get("tee_handoff_max") or 0)
    except (TypeError, ValueError):
        limit = 0
    return limit > 0 and len(_tee_handoff_active) < limit


def _remove_quiet(path: str | None) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _synth_request(cred_headers: dict) -> Request:
    """合成最小 Request scope（凭证头 + app 实例），供后台下载复用在线取流逻辑。"""
    scope_headers = [
        (str(k).lower().encode("latin-1"), str(v).encode("latin-1"))
        for k, v in (cred_headers or {}).items()
    ]
    return Request({"type": "http", "headers": scope_headers, "app": app})


def _register_tee_handoff(guid: str, part: str, written: int, expected: int | None,
                          ext: str | None, headers_factory: "Callable[[], dict] | None") -> None:
    """tee 流式下载被客户端中断（切歌/退出）后，把未完成的 part 交给后台续传完成。"""
    if find_cache_file(guid):
        _remove_quiet(part)
        return
    task = _full_fetch_tasks.get(guid)
    if task is not None and not task.done():
        # 已有在途整轨下载：交给它完成，不重复续传
        _remove_quiet(part)
        return
    now = time.monotonic()
    _prune_full_fetch_state(now)
    if now - _full_fetch_failed.get(guid, -_FULL_FETCH_COOLDOWN_S) < _FULL_FETCH_COOLDOWN_S:
        _remove_quiet(part)
        return
    headers = headers_factory() if headers_factory is not None else {}
    task = asyncio.create_task(_tee_handoff_download(guid, part, written, expected, ext, headers))
    _full_fetch_tasks[guid] = task
    _tee_handoff_active.add(guid)

    def _done(t: "asyncio.Task") -> None:
        _tee_handoff_active.discard(guid)
        if _full_fetch_tasks.get(guid) is t:
            del _full_fetch_tasks[guid]

    task.add_done_callback(_done)


async def _tee_handoff_download(guid: str, part: str, written: int, expected: int | None,
                                ext: str | None, cred_headers: dict) -> None:
    """续传被中断的 tee 下载；源不支持断点续传或长度不符时回退整轨重下。"""
    try:
        resumed = await _resume_part_download(guid, part, written, expected, ext, cred_headers)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("Tee handoff resume error for %s: %s", guid, type(e).__name__)
        resumed = False
    if resumed:
        return
    _remove_quiet(part)
    await _full_fetch_download(guid, cred_headers)


async def _resume_part_download(guid: str, part: str, written: int, expected: int | None,
                                ext: str | None, cred_headers: dict) -> bool:
    """Range 断点续拉剩余字节追加进 part；不可续传返回 False（调用方回退整轨重下）。"""
    fake_request = _synth_request(cred_headers)
    opened = await _open_online_stream(fake_request, guid, f"bytes={written}-")
    if not opened:
        return False
    resp, owned, stream_ext, info, chunks, first = opened
    try:
        if resp.status_code != 206:
            # 源忽略 Range 返回 200：无法续传
            return False
        match = re.fullmatch(
            r"bytes\s+(\d+)-(\d+)/(\d+)", resp.headers.get("content-range", "").strip(), re.I,
        )
        if not match or int(match.group(1)) != written:
            return False
        total = int(match.group(3))
        if total <= 0 or (expected is not None and total != expected):
            return False
        append_written = 0
        with open(part, "ab") as fp:
            if first:
                fp.write(first)
                append_written += len(first)
            async for chunk in chunks:
                if chunk:
                    fp.write(chunk)
                    append_written += len(chunk)
        final_size = written + append_written
        if final_size < 1024 or final_size != total:
            raise RuntimeError(f"resume size mismatch final={final_size} total={total}")
        info = await _info_for_background_save(fake_request, guid, info)
        resolved_ext = ext or stream_ext or (info or {}).get("ext") or "mp3"
        meta = await asyncio.to_thread(_tee_finalize, part, guid, resolved_ext, info or {}, True)
        logger.info("Tee handoff resumed %s (+%d bytes, total %d)", guid, append_written, final_size)
        await _auto_lyric_after_finalize(fake_request, guid, meta)
        _schedule_library_scan(cred_headers)
        _dispatch_official_binding(guid, cred_headers, meta)
        return True
    finally:
        with anyio.CancelScope(shield=True):
            await resp.aclose()
            if owned:
                await owned.aclose()


# === 官方绑定：下载落库 → 等官方扫入库 → 写官方收藏/歌单 → 删本地映射 ===
# 官方接口只认曲库内 guid：先只读轮询官方 music.db 拿到新入库曲目的官方 guid，
# 再用触发时刻的凭证头直调官方写接口；成功后删除本地映射条目（官方列表唯一
# 显示，避免同一首歌官方/本地映射重复出现两次），失败保留本地映射兜底并
# 由列表读取路径自愈重试。凭证头仅留内存，绝不落盘。

_BIND_POLL_INTERVAL_S = 3.0
_BIND_RETRY_THROTTLE_S = 60.0
_BIND_RETRY_BATCH = 5
# guid -> user_guid -> {"fav": bool, "playlists": set[str], "headers": dict}
_bind_pending: "dict[str, dict[str, dict]]" = {}
_bind_tasks: "dict[tuple[str, str], asyncio.Task]" = {}
_bind_retry_last: "dict[str, float]" = {}


def _register_bind_intent(guid: str, user_guid: str, headers: "dict | None",
                          playlist_guid: str | None = None) -> None:
    """登记官方绑定意图（收藏或加入歌单），由下载完成路径统一调度执行。"""
    if not guid or not user_guid:
        return
    users = _bind_pending.setdefault(guid, {})
    intent = users.get(user_guid)
    if intent is None:
        intent = {"fav": False, "playlists": set(), "headers": dict(headers or {})}
        users[user_guid] = intent
    elif headers:
        intent["headers"] = dict(headers)
    if playlist_guid:
        intent["playlists"].add(playlist_guid)
    else:
        intent["fav"] = True


def official_track_guid_by_tags(title: str, artist: str, album: str = "") -> "str | None":
    """只读官方 music.db，按落盘标签匹配曲目的官方 guid（最新一条优先）。

    标签由 write_audio_tags 写入、官方扫描入库时读取，精确匹配即高保真；
    精确未命中再用大小写不敏感兜底一次（官方扫描器可能做过归一化）。
    """
    title_s = (title or "").strip()
    artist_s = (artist or "").strip()
    album_s = (album or "").strip()
    if not title_s or not artist_s:
        return None
    db = str(CONF.get("music_db") or "")
    if not db or not os.path.exists(db):
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except Exception as e:
        logger.warning("Official bind db open failed: %s", e)
        return None
    try:
        for nocase in (False, True):
            collate = " COLLATE NOCASE" if nocase else ""
            sql = (
                "SELECT t.guid FROM track t "
                "JOIN track_artist ta ON ta.track_id = t.id "
                "JOIN artist a ON a.id = ta.artist_id "
                f"WHERE t.title = ?{collate} AND a.name = ?{collate}"
            )
            params: list = [title_s, artist_s]
            if album_s:
                sql += (
                    " AND EXISTS (SELECT 1 FROM album al"
                    f" WHERE al.id = t.album_id AND al.name = ?{collate})"
                )
                params.append(album_s)
            sql += " ORDER BY t.id DESC LIMIT 1"
            try:
                row = con.execute(sql, params).fetchone()
            except Exception as e:
                logger.warning("Official bind db query failed: %s", e)
                return None
            if row and row[0]:
                return str(row[0])
        return None
    finally:
        con.close()


def library_file_by_tags(title: str, artist: str, album: str = "") -> "str | None":
    """按标签在官方 music.db 反查曲库文件路径（audio_file.path，最新优先）。

    find_cache_file 的兜底数据源：与 official_track_guid_by_tags 同款两遍
    精确/NOCASE 查询，但取 a.path 且逐行校验文件确实存在（官方库里的
    悬空记录不算命中）。
    """
    title_s = (title or "").strip()
    artist_s = (artist or "").strip()
    album_s = (album or "").strip()
    if not title_s or not artist_s:
        return None
    db = str(CONF.get("music_db") or "")
    if not db or not os.path.exists(db):
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except Exception as e:
        logger.warning("Library tags lookup db open failed: %s", e)
        return None
    try:
        for nocase in (False, True):
            collate = " COLLATE NOCASE" if nocase else ""
            sql = (
                "SELECT a.path FROM track t "
                "JOIN audio_file a ON a.id = t.audio_file_id "
                "JOIN track_artist ta ON ta.track_id = t.id "
                "JOIN artist ar ON ar.id = ta.artist_id "
                f"WHERE t.title = ?{collate} AND ar.name = ?{collate}"
            )
            params: list = [title_s, artist_s]
            if album_s:
                sql += (
                    " AND EXISTS (SELECT 1 FROM album al"
                    f" WHERE al.id = t.album_id AND al.name = ?{collate})"
                )
                params.append(album_s)
            sql += " ORDER BY t.id DESC"
            try:
                rows = con.execute(sql, params).fetchall()
            except Exception as e:
                logger.warning("Library tags lookup query failed: %s", e)
                return None
            for row in rows:
                path = str(row[0] or "").strip()
                if path and os.path.isfile(path) and os.path.getsize(path) > 0:
                    return path
        return None
    finally:
        con.close()


async def _official_upstream_write(method: str, path: str, headers: dict, payload: dict) -> bool:
    """以扩展名义直调官方写接口（复用触发请求凭证头）；code==0 视为成功。"""
    try:
        client = get_upstream_client(app)
        req = client.build_request(method, path, headers=dict(headers or {}), json=payload)
        resp = await client.send(req)
        if resp.status_code != 200:
            logger.warning("Official bind %s %s -> http %s", method, path, resp.status_code)
            return False
        body = resp.json()
    except Exception as e:
        logger.warning("Official bind %s %s failed: %s", method, path, type(e).__name__)
        return False
    if isinstance(body, dict) and body.get("code") == 0:
        return True
    logger.warning("Official bind %s %s -> %s", method, path, body)
    return False


def _fav_bind_pending(guid: str, user_guid: str) -> bool:
    try:
        return any(
            it.get("guid") == guid and it.get("bind") == "pending"
            for it in load_online_favorites(user_guid)
        )
    except Exception:
        return False


async def _remove_fav_bound(guid: str, user_guid: str) -> bool:
    """官方收藏写入成功后删本地映射条目（仅 bind=pending；老数据不动）。"""
    async with _FAV_LOCK:
        try:
            items = load_online_favorites(user_guid)
            kept = [it for it in items if not (it.get("guid") == guid and it.get("bind") == "pending")]
            if len(kept) == len(items):
                return False
            save_online_favorites(user_guid, kept)
            return True
        except Exception as e:
            logger.warning("Error removing bound favorite for %s/%s: %s", user_guid, guid, e)
            return False


def _plt_bind_pending(playlist_guid: str, guid: str, user_guid: str) -> bool:
    try:
        bucket = load_playlist_tracks(user_guid).get(playlist_guid) or []
        return any(it.get("guid") == guid and it.get("bind") == "pending" for it in bucket)
    except Exception:
        return False


async def _remove_plt_bound(playlist_guid: str, guid: str, user_guid: str) -> bool:
    async with _PLT_LOCK:
        try:
            items = load_playlist_tracks(user_guid)
            bucket = items.get(playlist_guid)
            if not bucket:
                return False
            kept = [it for it in bucket if not (it.get("guid") == guid and it.get("bind") == "pending")]
            if len(kept) == len(bucket):
                return False
            if kept:
                items[playlist_guid] = kept
            else:
                items.pop(playlist_guid, None)
            save_playlist_tracks(user_guid, items)
            return True
        except Exception as e:
            logger.warning("Error removing bound playlist track for %s/%s: %s", user_guid, guid, e)
            return False


def _dispatch_official_binding(guid: str, headers: "dict | None", meta: "dict | None") -> None:
    """下载落库完成后调度官方绑定：逐用户去重起任务；无意图时零开销。"""
    if not CONF.get("fav_auto_bind"):
        return
    users = _bind_pending.get(guid)
    if not users:
        return
    for user_guid in list(users):
        if headers:
            users[user_guid]["headers"] = dict(headers)
        key = (guid, user_guid)
        existing = _bind_tasks.get(key)
        if existing is not None and not existing.done():
            continue
        task = asyncio.create_task(_bind_official_task(guid, user_guid, meta))
        _bind_tasks[key] = task

        def _done(t: "asyncio.Task", key=key) -> None:
            if _bind_tasks.get(key) is t:
                del _bind_tasks[key]

        task.add_done_callback(_done)


async def _bind_official_task(guid: str, user_guid: str, meta: "dict | None") -> None:
    """把单个用户的收藏/歌单意图写入官方，成功后删本地映射（官方列表唯一显示）。"""
    intent = (_bind_pending.get(guid) or {}).get(user_guid)
    if not intent:
        return
    headers = dict(intent.get("headers") or {})
    title = str((meta or {}).get("title") or "")
    artist = str((meta or {}).get("artist") or "")
    album = str((meta or {}).get("album") or "")
    if not (title.strip() and artist.strip()):
        snap = _lookup_online_snapshot(guid)
        snap = _snapshot_to_info(snap) if isinstance(snap, dict) and snap else {}
        title = title.strip() or str(snap.get("title") or "")
        artist = artist.strip() or str(snap.get("artist") or "")
        album = album.strip() or str(snap.get("album") or "")
    if not (title.strip() and artist.strip()):
        logger.info("Official bind skip for %s: no usable metadata", guid)
        return
    # 官方只认曲库内 guid：等待官方把刚落盘文件扫入库（只读轮询，绝不写官方库）
    _schedule_library_scan(headers)
    try:
        timeout_s = max(1.0, float(CONF.get("official_bind_timeout_s") or 120))
    except (TypeError, ValueError):
        timeout_s = 120.0
    deadline = time.monotonic() + timeout_s
    official_guid = await asyncio.to_thread(official_track_guid_by_tags, title, artist, album)
    while not official_guid and time.monotonic() < deadline:
        await asyncio.sleep(_BIND_POLL_INTERVAL_S)
        official_guid = await asyncio.to_thread(official_track_guid_by_tags, title, artist, album)
    if not official_guid:
        logger.info(
            "Official bind wait timeout for %s (%s - %s)：官方曲库尚未收录，保留本地映射待自愈重试",
            guid, artist, title,
        )
        return
    if intent.get("fav") and _fav_bind_pending(guid, user_guid):
        ok = await _official_upstream_write(
            "POST", "/music/api/v1/favorite-track/create", headers, {"trackGUID": official_guid},
        )
        if ok and await _remove_fav_bound(guid, user_guid):
            _record_bind(user_guid, guid, official_guid)
            logger.info("Official favorite bound for %s (%s) -> %s", guid, user_guid, official_guid)
        elif ok:
            # 写官方期间用户已取消收藏：补偿撤销官方收藏，不留幽灵状态
            await _official_upstream_write(
                "POST", "/music/api/v1/favorite-track/delete", headers, {"trackGUID": official_guid},
            )
    for playlist_guid in sorted(set(intent.get("playlists") or ())):
        if not _plt_bind_pending(playlist_guid, guid, user_guid):
            continue
        ok = await _official_upstream_write(
            "POST", "/music/api/v1/playlist/add-track", headers,
            {"guid": playlist_guid, "trackGUIDs": [official_guid]},
        )
        if ok and await _remove_plt_bound(playlist_guid, guid, user_guid):
            _record_bind(user_guid, guid, official_guid, playlist_guid=playlist_guid)
            logger.info("Official playlist bound for %s (%s -> %s)", guid, playlist_guid, official_guid)
        elif ok:
            await _official_upstream_write(
                "POST", "/music/api/v1/playlist/remove-track", headers,
                {"guid": playlist_guid, "trackGUIDs": [official_guid]},
            )
    # 意图清理：本地已无 pending 条目则移除；失败项保留待列表读取自愈重试
    live = _bind_pending.get(guid, {}).get(user_guid)
    if live is not None:
        still_pending = (live.get("fav") and _fav_bind_pending(guid, user_guid)) or any(
            _plt_bind_pending(pl, guid, user_guid) for pl in (live.get("playlists") or ())
        )
        if not still_pending and _bind_pending.get(guid, {}).get(user_guid) is live:
            _bind_pending[guid].pop(user_guid, None)
            if not _bind_pending[guid]:
                _bind_pending.pop(guid, None)


# === 绑定登记（online guid -> 官方 guid，bind_registry.json） ===
# 官方绑定完成后本地映射已删，但客户端旧会话可能仍持伪装 id 操作取消收藏/
# 移出歌单；登记命中时翻译成官方 guid 转发官方删除，避免"取消没生效"。

_BIND_REGISTRY_LIMIT = 1000
_BIND_REGISTRY_TTL_S = 30 * 86400.0
_bind_registry: "dict[str, dict[str, dict]]" = {}
_bind_registry_loaded = False


def bind_registry_path() -> str:
    return os.path.join(_HOME, "bind_registry.json")


def _ensure_bind_registry() -> None:
    global _bind_registry_loaded
    if _bind_registry_loaded:
        return
    _bind_registry_loaded = True
    try:
        with open(bind_registry_path(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for user, entries in data.items():
                if isinstance(entries, dict):
                    _bind_registry[str(user)] = {
                        str(g): e for g, e in entries.items() if isinstance(e, dict)
                    }
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("Failed to load bind registry: %s", e)


def _persist_bind_registry() -> None:
    path = bind_registry_path()
    parent = os.path.dirname(path) or "."
    part_path = f"{path}.{uuid4().hex[:8]}.part"
    try:
        os.makedirs(parent, exist_ok=True)
        with open(part_path, "w", encoding="utf-8") as f:
            json.dump(_bind_registry, f, ensure_ascii=False, indent=2)
        os.replace(part_path, path)
    except Exception as e:
        logger.warning("Failed to persist bind registry: %s", e)
        _remove_quiet(part_path)


def _prune_bind_registry() -> None:
    now = time.time()
    for user in list(_bind_registry):
        entries = _bind_registry[user]
        for g, entry in list(entries.items()):
            try:
                stale = now - float(entry.get("at") or 0) >= _BIND_REGISTRY_TTL_S
            except (TypeError, ValueError):
                stale = True
            if stale:
                entries.pop(g, None)
        if not entries:
            _bind_registry.pop(user, None)
    while sum(len(v) for v in _bind_registry.values()) > _BIND_REGISTRY_LIMIT:
        oldest: "tuple[float, str, str] | None" = None
        for user, entries in _bind_registry.items():
            for g, entry in entries.items():
                try:
                    at = float(entry.get("at") or 0)
                except (TypeError, ValueError):
                    at = 0.0
                if oldest is None or at < oldest[0]:
                    oldest = (at, user, g)
        if oldest is None:
            break
        _bind_registry[oldest[1]].pop(oldest[2], None)
        if not _bind_registry[oldest[1]]:
            _bind_registry.pop(oldest[1], None)


def _bind_entry_alive(entry: dict) -> bool:
    return bool(entry.get("fav") or entry.get("playlists"))


def _record_bind(user_guid: str, guid: str, official_guid: str,
                 playlist_guid: str | None = None) -> None:
    """官方写入成功后登记 online guid → 官方 guid 映射（旧会话假 id 删除时翻译转发）。"""
    _ensure_bind_registry()
    entries = _bind_registry.setdefault(user_guid, {})
    entry = entries.get(guid)
    if not isinstance(entry, dict) or entry.get("official") != official_guid:
        entry = {"official": official_guid, "fav": False, "playlists": [], "at": time.time()}
        entries[guid] = entry
    if playlist_guid:
        pls = [p for p in (entry.get("playlists") or []) if p != playlist_guid]
        pls.append(playlist_guid)
        entry["playlists"] = pls
    else:
        entry["fav"] = True
    entry["at"] = time.time()
    _prune_bind_registry()
    _persist_bind_registry()


def lookup_bind_entry(user_guid: str, guid: str) -> "dict | None":
    _ensure_bind_registry()
    entry = _bind_registry.get(user_guid, {}).get(guid)
    return dict(entry) if isinstance(entry, dict) else None


def _forget_bind(user_guid: str, guid: str, fav: bool = False,
                 playlist_guid: str | None = None) -> None:
    """撤销登记中的收藏/歌单标记；两边都空则整条删除。"""
    _ensure_bind_registry()
    entries = _bind_registry.get(user_guid)
    if not entries:
        return
    entry = entries.get(guid)
    if not isinstance(entry, dict):
        return
    if fav:
        entry["fav"] = False
    if playlist_guid:
        entry["playlists"] = [p for p in (entry.get("playlists") or []) if p != playlist_guid]
    if not _bind_entry_alive(entry):
        entries.pop(guid, None)
    if not entries:
        _bind_registry.pop(user_guid, None)
    _persist_bind_registry()


def _forget_bind_playlist(user_guid: str, playlist_guid: str) -> None:
    """删除歌单后清掉登记里该歌单的标记（防孤儿转发）。"""
    _ensure_bind_registry()
    entries = _bind_registry.get(user_guid)
    if not entries:
        return
    changed = False
    for g in list(entries):
        entry = entries[g]
        pls = [p for p in (entry.get("playlists") or []) if p != playlist_guid]
        if pls != (entry.get("playlists") or []):
            entry["playlists"] = pls
            changed = True
            if not _bind_entry_alive(entry):
                entries.pop(g, None)
    if not entries:
        _bind_registry.pop(user_guid, None)
        changed = True
    if changed:
        _persist_bind_registry()


def _lookup_bind_any_user(raw_guid: str) -> "tuple[str, str, dict] | None":
    """跨用户按在线 guid 或其伪装 32hex 形态反查绑定登记。

    返回 (user_guid, online_guid, entry)。官方绑定完成后本地映射已删、伪装
    反查表可能不再认识该 id，这里兜底直接比对确定性 md5。
    """
    _ensure_bind_registry()
    raw = str(raw_guid or "").strip()
    if not raw or not _bind_registry:
        return None
    resolved = resolve_real_guid(raw)
    for user, entries in _bind_registry.items():
        if is_online_guid(resolved) and resolved in entries:
            return user, resolved, dict(entries[resolved])
    if re.fullmatch(r"[0-9a-fA-F]{32}", raw):
        for user, entries in _bind_registry.items():
            for g, entry in entries.items():
                if fake_official_guid(g) == raw:
                    return user, g, dict(entry)
    return None


def _maybe_retry_official_binds(request: Request, user_guid: str,
                                fav_items: "list[dict] | None" = None,
                                plt_map: "dict[str, list[dict]] | None" = None) -> None:
    """列表读取时的自愈：bind=pending 条目补发官方绑定或下载（用户级 60s 节流）。

    覆盖代理重启丢内存意图、官方迟扫入库、下载中断等场景；凭证头取当前请求，
    绝不持久化。
    """
    if not CONF.get("fav_auto_bind") or not user_guid:
        return
    fav_guids = [
        str(it.get("guid") or "") for it in (fav_items or [])
        if it.get("bind") == "pending" and it.get("guid")
    ]
    plt_guids: "dict[str, list[str]]" = {}
    for playlist_guid, bucket in (plt_map or {}).items():
        pending = [
            str(it.get("guid") or "") for it in (bucket or [])
            if it.get("bind") == "pending" and it.get("guid")
        ]
        if pending:
            plt_guids[playlist_guid] = pending
    if not fav_guids and not plt_guids:
        return
    now = time.monotonic()
    if now - _bind_retry_last.get(user_guid, -_BIND_RETRY_THROTTLE_S) < _BIND_RETRY_THROTTLE_S:
        return
    _bind_retry_last[user_guid] = now
    headers = copy_incoming_headers(request)
    uniq = list(dict.fromkeys(fav_guids + [g for gs in plt_guids.values() for g in gs]))
    for guid in fav_guids[:_BIND_RETRY_BATCH]:
        _register_bind_intent(guid, user_guid, headers, None)
    for playlist_guid, guids in plt_guids.items():
        for guid in guids[:_BIND_RETRY_BATCH]:
            _register_bind_intent(guid, user_guid, headers, playlist_guid)
    for guid in uniq[:_BIND_RETRY_BATCH]:
        if find_cache_file(guid):
            _dispatch_official_binding(guid, headers, None)
        else:
            _register_background_fetch(request, guid, "fav_auto_bind")


async def _stream_head_response(request: Request, guid: str, cached: str | None, range_header: str | None) -> Response:
    """HEAD 探测（部分手机播放器先 HEAD 后 GET）：缓存命中回真实大小头；在线源轻量试开一次即关。"""
    if cached:
        ext = os.path.splitext(cached)[1].lstrip(".") or "mp3"
        full = serve_file_with_range(cached, range_header, media_type_for_ext(ext))
        return Response(status_code=full.status_code, headers=dict(full.headers))
    opened = None
    try:
        opened = await asyncio.wait_for(_open_online_stream(request, guid, range_header), timeout=4.0)
    except Exception:
        opened = None
    if not opened:
        return JSONResponse(content={"code": 404, "msg": "online source unavailable", "data": None}, status_code=404)
    resp, owned, _ext, _info, _chunks, _first = opened
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    for key in ("content-type", "content-length", "content-range"):
        val = resp.headers.get(key)
        if val:
            headers[key] = val
    with anyio.CancelScope(shield=True):
        await resp.aclose()
        if owned:
            await owned.aclose()
    return Response(status_code=206 if "content-range" in headers else 200, headers=headers)


def _candidate_kbps(item: dict) -> float:
    """同录音候选的估算码率 kbps（file_size*8/duration）；未知返回 0。"""
    try:
        dur = float(item.get("duration_s") or 0)
        size = float(item.get("file_size") or 0)
        if dur > 0 and size > 0:
            return size * 8.0 / dur / 1000.0
    except (TypeError, ValueError):
        pass
    return 0.0


def ordered_stream_alternatives(item: dict) -> list[dict]:
    """musicdl 同录音跨平台候选按音质模式排序。

    high：码率高者优先；balanced：中间码率先向下再向上；smooth：码率低者优先。
    无码率信息时保持原始顺序。
    """
    alts = [
        x for x in (item.get("_alternatives") or [])
        if isinstance(x, dict) and _same_recording(item, x)
    ]
    mode = str(CONF.get("quality_mode") or "high").strip().lower()
    known = sorted({round(_candidate_kbps(x), 1) for x in alts if _candidate_kbps(x) > 0})
    if not known:
        return list(alts)
    if mode == "smooth":
        order = known
    elif mode == "balanced":
        mid = len(known) // 2
        order = [known[mid]] + list(reversed(known[:mid])) + known[mid + 1:]
    else:
        order = list(reversed(known))
    rank = {v: i for i, v in enumerate(order)}
    return sorted(alts, key=lambda x: rank.get(round(_candidate_kbps(x), 1), len(order)))


# ---------------------------------------------------------------------------
# W3：本地曲库优先（FNMUSIC_LOCAL_FIRST，默认关闭）
#
# 命中且档位兼容 → 直接出本地文件；网易云取链彻底失败时，本地命中作为兜底
# 出流（沿用 G 的「不论档位兜底」语义）。索引来源就是 A 现有的
# resolve_music_db() + detect_library_dir()，不涉及 trimgw 授权目录。
# ---------------------------------------------------------------------------


async def _local_first_match(request: Request, guid: str, item: "dict | None") -> "dict | None":
    """按标题/歌手在本地曲库找同曲；标题优先用搜索缓存条目，缺失才查在线信息。"""
    title = str((item or {}).get("title") or "")
    artist = str((item or {}).get("artist") or "")
    if not title:
        try:
            info = await asyncio.wait_for(
                _online_info(request, guid, include_lyric=False), timeout=1.5)
        except Exception as exc:  # noqa: BLE001 - 拿不到元信息就放弃本地优先
            logger.debug("local-first info lookup failed for %s: %s", guid, type(exc).__name__)
            info = None
        if isinstance(info, dict):
            title = str(info.get("title") or "")
            artist = artist or str(info.get("artist") or "")
    if not title.strip():
        return None
    return local_library.find_local_match(title, artist, resolve_music_db(), detect_library_dir())


def _serve_local_entry(entry: dict, range_header: "str | None") -> Response:
    return serve_file_with_range(entry["path"], range_header,
                                 media_type_for_ext(entry.get("ext") or "mp3"))


# ---------------------------------------------------------------------------
# 下一首预热接线（移植 G v2.9.30 的 _prefetch_* 全家桶）
# ---------------------------------------------------------------------------
# 预热的目的是缩短「点击 → 出声」的等待：播放当前曲目时，把上下文里「下一首」
# 的直链与元数据提前取回来（只填缓存，不下载音频字节）。
_PREFETCH_TASKS: dict[str, asyncio.Task] = {}
# 预热并发闸门。musicbox 是单进程，同时塞给它一堆请求只会让**所有**请求变慢，
# 串起来反而更快；也在用户连续切歌时保护「正在播的那一首」。
_PREFETCH_GATE: asyncio.Semaphore | None = None


def _prefetch_gate() -> asyncio.Semaphore:
    global _PREFETCH_GATE
    if _PREFETCH_GATE is None:
        _PREFETCH_GATE = asyncio.Semaphore(prefetch.max_concurrent())
    return _PREFETCH_GATE


def _online_url_cached(guid: str) -> bool:
    """该在线曲目的直链是否已在短缓存里。

    A 侧取流与解析都在 `_open_online_stream` 里完成，stream_track 拿不到那次解析
    的 stats，所以用「解析前缓存里有没有这首歌」作为实测口径：有就是这次播放
    极可能不付解析往返。只对网易云成立（三音源里只有它会走直链短缓存）。
    """
    if not is_online_guid(guid) or source_from_online_guid(guid) != "netease":
        return False
    return _url_cache_peek(song_id_from_online_guid(guid).split(":")[-1])


async def _prefetch_one(request: Request, guid: str) -> None:
    """后台把一首歌的直链与元数据取回来，只填缓存、不下载任何音频字节。

    全程受并发闸门与超时双重约束：宁可这次不预热，也不能把 musicbox 拖慢
    —— 那会让**正在播的那首**也跟着卡。
    """
    started = time.monotonic()
    try:
        async with _prefetch_gate():
            try:
                await asyncio.wait_for(_prefetch_inner(request, guid),
                                       timeout=prefetch.timeout_seconds())
            except asyncio.TimeoutError:
                prefetch.bump("timeout")
                prefetch.note_result(guid, False, (time.monotonic() - started) * 1000.0,
                                     f"timeout>{prefetch.timeout_seconds():.0f}s")
    except Exception as exc:  # noqa: BLE001 - 预热失败只记日志，绝不影响播放
        prefetch.note_result(guid, False, (time.monotonic() - started) * 1000.0,
                             f"{type(exc).__name__}: {exc}")
    finally:
        _PREFETCH_TASKS.pop(guid, None)


async def _prefetch_inner(request: Request, guid: str) -> None:
    song_id = song_id_from_online_guid(guid).split(":")[-1]
    if not song_id:
        prefetch.note_result(guid, False, 0.0, "no song id")
        return
    started = time.monotonic()
    client = get_musicbox_client(request.app)
    # 带 stats 调用 ⇒ 解析结果会灌进直链短缓存，下一首播放时即可零往返命中。
    flags: dict = {}
    url, _info = await asyncio.gather(
        resolve_netease_url(client, song_id, request, stats=flags),
        _online_info(request, guid),
        return_exceptions=True,
    )
    ok = bool(url) and not isinstance(url, Exception)
    prefetch.note_result(guid, ok, (time.monotonic() - started) * 1000.0,
                         "" if ok else f"url={url!r}")


def _schedule_prefetch(request: Request, guid: str) -> None:
    """为「下 N 首」安排预热。同步返回，失败静默，绝不阻塞当前播放。"""
    try:
        if not prefetch.enabled():
            return
        targets = prefetch.next_n(guid, prefetch._lookahead())
        if not targets:
            prefetch.bump("no_next")
            return
        _sched = 0
        for next_guid in targets:
            _sched += _prefetch_if_needed(request, guid, next_guid)
        if _sched:
            logger.info("prefetch schedule: %s -> %s", guid, ", ".join(targets))
    except Exception as exc:  # noqa: BLE001
        logger.debug("prefetch schedule failed for %s: %s: %s", guid, type(exc).__name__, exc)


def _prefetch_if_needed(request: Request, from_guid: str, next_guid: str) -> int:
    """给单首歌安排预热，返回 1 表示真的排了、0 表示跳过（已在预热/非在线）。"""
    if not is_online_guid(next_guid) or next_guid == from_guid:
        prefetch.bump("no_next")
        return 0
    # A 的三音源：只有网易云有直链短缓存可灌，lx/musicdl 的解析路径不同，不预热。
    if source_from_online_guid(next_guid) != "netease":
        prefetch.bump("no_next")
        return 0
    if prefetch.warming_seconds(next_guid) is not None or not prefetch.claim(next_guid):
        prefetch.bump("already")
        return 0
    # 总闸门：musicbox 是单进程，用户连续切歌时**每一首**都会追加 N 个预热，队列
    # 越堆越长它越消化不过来，最后被拖慢的恰恰是「正在播的那一首」。到上限就放弃
    # 这次预热——预热是锦上添花，跟播放冲突时必须让位。
    if len(_PREFETCH_TASKS) >= prefetch.max_queue():
        prefetch.bump("queued_out")
        return 0
    prefetch.bump("scheduled")
    _PREFETCH_TASKS[next_guid] = asyncio.create_task(_prefetch_one(request, next_guid))
    return 1


def _schedule_list_prefetch(request: Request, context_guid: str) -> None:
    """歌单列表一下发就预热它的头几首——用户点开时第一首的直链已经热了。

    只预热头 1 首：真机 musicbox 会同时收到十几个歌单的列表请求，一首歌单预热
    多了反而把它自己堵住。
    """
    try:
        if not prefetch.enabled() or not prefetch.on_list_enabled():
            return
        for g in prefetch.first_of(context_guid, 1):
            _prefetch_if_needed(request, "", g)
    except Exception as exc:  # noqa: BLE001
        logger.debug("list prefetch failed for %s: %s: %s",
                     context_guid, type(exc).__name__, exc)


def _remember_prefetch_context(request: Request, context_guid: str, tracks: list) -> None:
    """记下刚下发的一份有序曲目列表——stream 请求里没有歌单信息，「下一首是谁」
    全靠它推断（移植 G v2.9.30 的 playlist_track_list 接线点）。

    记不住只是不能预热，绝不影响打开歌单。
    """
    try:
        n = prefetch.remember_context(context_guid, tracks)
        if n:
            logger.info("prefetch context: %s（%d 首）", context_guid, n)
            # 列表刚下发就预热头一首：用户点开时它的直链已经是热的
            _schedule_list_prefetch(request, context_guid)
    except Exception as exc:  # noqa: BLE001
        logger.debug("prefetch context failed for %s: %s: %s",
                     context_guid, type(exc).__name__, exc)


@app.api_route("/music/api/v1/track/stream", methods=["GET", "HEAD"])
@app.api_route("/music/api/v1/track/stream/{subpath:path}", methods=["GET", "HEAD"])
async def stream_track(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))
    range_header = request.headers.get("range")
    cached = find_cache_file(guid)
    log_stream_probe(request.method, guid, range_header, bool(cached))
    if request.method == "HEAD":
        return await _stream_head_response(request, guid, cached, range_header)
    if cached:
        ext = os.path.splitext(cached)[1].lstrip(".") or "mp3"
        return serve_file_with_range(cached, range_header, media_type_for_ext(ext))
    item, entry = _retained_track(request, guid)
    # 播放当前曲目 → 把上下文里「下一首」的直链/元信息提前取回来（只填缓存）。
    # 放在 cached/HEAD 早返回之后：命中本地缓存或 HEAD 探测时不必再预热。
    _schedule_prefetch(request, guid)
    warming_now = guid in _PREFETCH_TASKS
    warm_before = _online_url_cached(guid)
    _play_started = time.monotonic()
    # W3：本地曲库优先（默认关闭）。关闭时 local_hit 恒为 None，下面两条分支都不
    # 会执行，也不会提前计算 quality.resolve —— 与历史代码路径完全一致。
    local_hit = None
    if local_library.local_first_enabled() and source_from_online_guid(guid) == "netease":
        try:
            local_hit = await _local_first_match(request, guid, item)
        except Exception as exc:  # noqa: BLE001 - 本地优先失败就退回在线链路
            logger.warning("local-first lookup failed for %s: %s", guid, type(exc).__name__)
            local_hit = None
        if local_hit:
            decision = quality.resolve(request, db_path=resolve_music_db())
            if local_library.serves_request(local_hit, decision.get("level") or "",
                                            decision.get("network") or ""):
                logger.info("local-first hit: %s -> %s (level=%s network=%s policy=%s)",
                            guid, local_hit.get("path"), decision.get("level"),
                            decision.get("network"), decision.get("policy"))
                return _serve_local_entry(local_hit, range_header)
            logger.debug("local-first skip: %s 本地 %s 与档位 %s 不兼容（network=%s）",
                         guid, local_hit.get("path"), decision.get("level"),
                         decision.get("network"))
    candidates = [guid]
    # Byte offsets are encoding-specific: do not cross sources on seek/probe.
    if should_cache(range_header) and item and request.query_params.get("_ext_rendition") != "1":
        candidates += [online_guid_from_item(x) for x in ordered_stream_alternatives(item)]
    deadline = asyncio.get_running_loop().time() + 12.0
    for candidate in list(dict.fromkeys(candidates))[:3]:
        if not _source_enabled(candidate):
            continue
        for attempt in range(2):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                # 单次解析预算与 musicbox-service 进程内取链（毫秒级）+ 网络抖动余量对齐
                opened = await asyncio.wait_for(_open_online_stream(request, candidate, range_header), timeout=min(6.0, remaining))
            except Exception as exc:
                logger.warning("Stream startup failed for %s: %s", candidate, type(exc).__name__)
                opened = None
            if opened:
                resp, owned, ext, info, chunks, first = opened
                if candidate != guid:
                    # Publish the selected source identity before any audio.
                    # The client owns B's URL for later Range requests even if
                    # this proxy's search session expires; never alias B as A.
                    with anyio.CancelScope(shield=True):
                        await resp.aclose()
                        if owned:
                            await owned.aclose()
                    # Behind the fnOS gateway over a unix socket the request
                    # scheme is always http and the Host header carries no
                    # port, so an absolute URL would strand https/wan clients
                    # on an unreachable address; only a relative Location is
                    # safe for every client entry point.
                    target = request.url.include_query_params(guid=candidate, _ext_rendition="1")
                    location = target.path + (f"?{target.query}" if target.query else "")
                    return RedirectResponse(location, status_code=307, headers={"Cache-Control": "no-store"})
                # play-start 计数：warm 用「解析前直链已在短缓存」实测，warming 表示
                # 这次播放撞上了正在跑的同曲预热（归 cold，但成因单列，不混进命中率）。
                prefetch.note_play(guid, (time.monotonic() - _play_started) * 1000.0,
                                   warm_before, warming=warming_now)
                # Cache the selected source's bytes under its own GUID, never
                # splice a failed stream or alias different encodings for seeks.
                if not should_cache(range_header):
                    # 定长窗口客户端：本响应过不了 tee 写盘门，注册后台整轨下载兜底
                    _register_full_fetch(request, candidate)
                return stream_tee_response(resp, candidate, range_header,
                    coro_factory=lambda: _online_info(request, candidate, include_lyric=bool(CONF.get("lyric_auto_dl"))),
                    client_to_close=owned,
                    resolved_ext=ext, pre_info=info, chunks=chunks, first_chunk=first,
                    scan_headers_factory=lambda: copy_incoming_headers(request),
                    post_finalize=lambda meta: _auto_lyric_after_finalize(request, candidate, meta))
            if attempt or deadline - asyncio.get_running_loop().time() <= 3:
                break
            if not await _recover_source(request, candidate, entry):
                break
    if local_hit:
        # 网易云取链彻底失败时本地命中不论档位兜底出流（沿用 G 的语义）。
        try:
            logger.info("local-first fallback: %s -> %s", guid, local_hit.get("path"))
            return _serve_local_entry(local_hit, range_header)
        except Exception as exc:  # noqa: BLE001 - 兜底失败仍如实 404
            logger.warning("local-first fallback failed for %s: %s", guid, type(exc).__name__)
    return JSONResponse(content={"code": 404, "msg": "online source unavailable", "data": None}, status_code=404)


async def _transcode_source(request: Request, guid: str) -> "tuple[str | None, dict | None]":
    """转码输入源：已落库/已缓存文件优先（源关了也能转），其次在线直链。"""
    cached = find_cache_file(guid)
    if cached:
        return cached, None
    if not _source_enabled(guid):
        return None, None
    source = source_from_online_guid(guid)
    try:
        if source == "netease":
            url = await resolve_netease_url(
                get_musicbox_client(request.app), song_id_from_online_guid(guid).split(":")[-1],
                request)
            return (url or None, None)
        if source == "lx":
            resolved = await resolve_lx_url(get_lx_client(request.app), song_id_from_online_guid(guid))
            if not resolved or not resolved.get("url"):
                return None, None
            headers = {k: str(v) for k, v in (resolved.get("headers") or {}).items()
                       if k.lower() in ("referer", "user-agent")}
            return str(resolved["url"]), headers or None
        url = f"{str(CONF['musicdl_url']).rstrip('/')}/stream?id={quote(song_id_from_online_guid(guid))}&proxy=true"
        if _lossless_is_blacklisted(guid):
            url += "&quality=mp3"
        return url, None
    except Exception as e:  # noqa: BLE001
        logger.warning("transcode source resolve failed for %s: %s", guid, e)
        return None, None


async def _start_transcode_session(request: Request, guid: str) -> "tc.Session | None":
    """启动/复用标准音质转码会话；不可用返回 None（调用方回落单分片桩）。"""
    try:
        info = await _online_info(request, guid) or {}
        duration_s = float(info.get("duration_s") or 0)
        src, headers = await _transcode_source(request, guid)
        if not src:
            return None
        return await tc.ensure_session(
            guid, src, duration_s,
            root=CONF["cache_dir"],
            bitrate=str(CONF.get("transcode_bitrate") or "128k"),
            hls_time=float(CONF.get("transcode_hls_time") or 10),
            max_active=max(1, int(CONF.get("transcode_max_sessions") or 2)),
            max_cache_bytes=max(16, int(CONF.get("transcode_cache_max_mb") or 512)) * 1024 * 1024,
            headers=headers,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("transcode session start failed for %s: %s", guid, e)
        return None


def _transcode_live(guid: str) -> "tc.Session | None":
    """取在跑或磁盘已完整的转码会话（不解析在线源）。"""
    return tc.get_session(guid) or tc.peek_cached(guid, CONF["cache_dir"])


def _legacy_hls_stub(guid: str, info: "dict | None") -> Response:
    """无 ffmpeg / 源不可用时的兜底：单分片=原始文件（历史行为）。"""
    try:
        duration_s = int(float((info or {}).get("duration_s") or 0))
    except (TypeError, ValueError):
        duration_s = 0
    if duration_s <= 0:
        duration_s = 240
    stream_url = f"/music/api/v1/track/stream?guid={quote(guid, safe='')}"
    playlist = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        f"#EXT-X-TARGETDURATION:{max(duration_s, 1)}\n"
        "#EXT-X-PLAYLIST-TYPE:VOD\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n"
        f"#EXTINF:{duration_s:.3f},\n"
        f"{stream_url}\n"
        "#EXT-X-ENDLIST\n"
    )
    return Response(content=playlist, media_type="application/vnd.apple.mpegurl")


@app.api_route("/music/api/v1/track/hls/{guid}/preset.m3u8", methods=["GET", "HEAD"])
@app.api_route("/music/api/v1/track/hls/{guid}/{filename}", methods=["GET", "HEAD"])
async def track_hls(request: Request, guid: str, filename: str = "preset.m3u8"):
    guid = resolve_real_guid(guid)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    if filename != "preset.m3u8" and not tc.valid_segment_name(filename):
        return JSONResponse(content={"code": 404, "msg": "not found", "data": None}, status_code=404)

    sess = _transcode_live(guid)
    if sess is None and CONF.get("transcode_enabled") and tc.FFMPEG_BIN:
        # App 有时不先 POST /track/transcode 直接拉 playlist（实测），此处惰性补启
        sess = await _start_transcode_session(request, guid)

    if sess is not None:
        if filename == "preset.m3u8":
            return Response(content=tc.playlist_text(sess), media_type="application/vnd.apple.mpegurl")
        path = await tc.wait_file(os.path.join(sess.directory, filename), sess, timeout=30.0)
        if path:
            return serve_file_with_range(path, request.headers.get("range"),
                                         _hls_segment_media(filename))
        return JSONResponse(content={"code": 404, "msg": "segment unavailable", "data": None}, status_code=404)

    if filename != "preset.m3u8":
        return JSONResponse(content={"code": 404, "msg": "segment unavailable", "data": None}, status_code=404)
    info = await _online_info(request, guid)
    return _legacy_hls_stub(guid, info)


@app.api_route("/music/api/v1/track/transcode/heartbeat", methods=["GET", "POST"])
@app.api_route("/music/api/v1/track/transcode/quit", methods=["GET", "POST"])
async def track_transcode_session(request: Request):
    guid = await extract_guid_from_body(request)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))
    if request.url.path.endswith("/heartbeat"):
        tc.heartbeat(guid)
        try:
            await tc.maintain(CONF["cache_dir"], float(CONF.get("transcode_ttl_s") or 90),
                              max(16, int(CONF.get("transcode_cache_max_mb") or 512)) * 1024 * 1024)
        except Exception:  # noqa: BLE001
            pass
    else:
        await tc.quit_session(guid)
    return JSONResponse(content={"code": 0, "msg": "ok", "data": {"guid": guid}})


@app.api_route("/music/api/v1/track/transcode", methods=["GET", "POST"])
async def track_transcode(request: Request):
    guid = await extract_guid_from_body(request)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))
    if CONF.get("transcode_enabled") and tc.FFMPEG_BIN:
        sess = _transcode_live(guid)
        if sess is None:
            sess = await _start_transcode_session(request, guid)
        if sess is not None and sess.status in ("starting", "running") and not find_cache_file(guid):
            # 标准音质播放不经过 /track/stream，tee 永不触发；挂后台整轨下载保住边听边存
            _register_full_fetch(request, guid)
    return JSONResponse(
        content={
            "code": 0,
            "msg": "ok",
            "status": "success",
            "data": {"guid": guid, "status": "ready"},
        }
    )


# === App 下载（音质偏好=标准 → 转码下载）仿真 ===
# 协议按官方实测对齐（本地歌曲透传抓包，2026-09-29）：
#   prepare POST {trackGUID, quality}  → data{status:"success", errno, errmsg, downloadId}
#   status  GET ?downloadId=           → data{status:"waiting|transcoding|ready|failed",
#                                             errno, errmsg, downloadId, percent}
#   file    GET ?downloadId=           → 音频字节（audio/mpeg，支持断点）
#   delete  POST {downloadId}          → data{downloadId, deleted:true}
# 官方"标准"档 = MP3 320kbps（FLAC 源实测 320067bps）；源已是 mp3 时直接供原文件
# 不做无意义重编码。官方服务不认识在线曲目的伪装 guid，透传必失败；此处仅当
# guid 是在线曲目才接管，本地曲目与未知 downloadId 一律透传官方。
_DL_TASKS: dict[str, dict] = {}
_DL_TASK_TTL_S = 3600.0
_DL_TRANSCODE_SUBDIR = "dltrans"


def _dl_transcode_path(guid: str) -> str:
    return os.path.join(CONF["cache_dir"], _DL_TRANSCODE_SUBDIR, f"{cache_safe_guid(guid)}.mp3")


def _dl_sweep() -> None:
    now = time.monotonic()
    for key, task in list(_DL_TASKS.items()):
        if now - float(task.get("touched", 0)) > _DL_TASK_TTL_S:
            _DL_TASKS.pop(key, None)


async def _dl_body_json(request: Request) -> dict:
    try:
        raw = await request.body()
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _dl_find_task(request: Request, body: dict) -> "dict | None":
    for key in ("downloadId", "taskGuid", "taskId", "id", "guid", "trackGUID"):
        value = request.query_params.get(key) or body.get(key)
        if value:
            hit = _DL_TASKS.get(str(value))
            if hit:
                hit["touched"] = time.monotonic()
                return hit
    return None


def _task_guid_of(task: dict) -> str:
    for key, value in _DL_TASKS.items():
        if value is task:
            task["downloadId"] = key
            return key
    return str(task.get("downloadId") or "")


def _dl_state_word(task: dict) -> str:
    """内部状态 → 官方 status 字段用词（completed=ready）。"""
    state = str(task.get("state") or "waiting")
    return {"completed": "ready"}.get(state, state)


async def _dl_produce(task: dict, cred_headers: dict) -> None:
    """后台产文件：标准档按官方规格 MP3 320k（源已是 mp3 直接供原文件）；原始档供原文件。"""
    guid = task["guid"]
    try:
        src = find_cache_file(guid)
        if not src and _source_enabled(guid):
            # 无本地文件：先走整轨下载管线（带元数据命名/落库，与边听边存同款）
            await _full_fetch_download(guid, cred_headers)
            src = find_cache_file(guid)
        if not src:
            task["state"] = "failed"
            task["errmsg"] = "online source unavailable"
            return
        if task.get("quality") == "original" or os.path.splitext(src)[1].lower() == ".mp3":
            task["path"] = src
        else:
            target = _dl_transcode_path(guid)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if not os.path.isfile(target):
                task["state"] = "transcoding"
                task["percent"] = 0
                proc = await asyncio.create_subprocess_exec(
                    tc.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-i", src, "-vn", "-map_metadata", "-1",
                    "-c:a", "libmp3lame", "-b:a", str(CONF.get("transcode_dl_bitrate") or "320k"),
                    "-f", "mp3", target,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                if await proc.wait() != 0:
                    task["state"] = "failed"
                    task["errmsg"] = "transcode failed"
                    return
            task["path"] = target
        task["state"] = "completed"
        task["percent"] = 100
        try:
            task["size"] = os.path.getsize(task["path"])
        except OSError:
            task["size"] = 0
    except Exception as e:  # noqa: BLE001
        logger.warning("download transcode produce failed for %s: %s", guid, e)
        task["state"] = "failed"
        task["errmsg"] = "internal error"


async def _dl_forward(request: Request):
    """download/* 透传官方；trace_forward=detail 时抓双向往返报文（协议对齐取证）。"""
    if not CONF.get("trace_forward"):
        return await forward_to_upstream(request, get_upstream_client(request.app))
    body = await request.body()
    if body:
        logger.info("[dl-capture] REQ %s %s body=%s", request.method, request.url.path, body[:2000])
    resp = await forward_to_upstream(request, get_upstream_client(request.app))
    captured: list[bytes] = []

    async def _tee():
        async for chunk in resp.body_iterator:
            captured.append(chunk)
            yield chunk
        if captured:
            logger.info("[dl-capture] RESP %s %s status=%s body=%s",
                        request.method, request.url.path, resp.status_code, b"".join(captured)[:2000])

    return StreamingResponse(_tee(), status_code=resp.status_code,
                             headers=filter_headers(resp.headers), media_type=resp.media_type)


@app.api_route("/music/api/v1/download/track/transcode/prepare", methods=["GET", "POST"])
async def dl_transcode_prepare(request: Request):
    body = await _dl_body_json(request)
    if CONF.get("trace_forward"):
        logger.info("[dl-capture] PREPARE req q=%s body=%s", dict(request.query_params), body)
    raw_guid = ""
    for key in ("guid", "trackGUID", "trackGuid"):
        value = request.query_params.get(key) or body.get(key)
        if value:
            raw_guid = str(value)
            break
    guid = resolve_real_guid(raw_guid)
    if not is_online_guid(guid):
        return await _dl_forward(request)
    quality = "standard"
    for key in ("quality", "qualityType"):
        value = str(body.get(key) or request.query_params.get(key) or "").lower()
        if value in ("original", "standard"):
            quality = value
            break
    if body.get("isOriginal") is True:
        quality = "original"
    _dl_sweep()
    download_id = uuid4().hex
    task = {"guid": guid, "quality": quality, "state": "waiting", "percent": 0,
            "downloadId": download_id, "touched": time.monotonic(), "started": time.time()}
    _DL_TASKS[download_id] = task
    asyncio.create_task(_dl_produce(task, copy_incoming_headers(request)))
    return JSONResponse(content={
        "code": 0, "msg": "",
        "data": {"status": "success", "errno": "", "errmsg": "", "downloadId": download_id},
    })


@app.api_route("/music/api/v1/download/track/transcode/status", methods=["GET", "POST"])
async def dl_transcode_status(request: Request):
    body = await _dl_body_json(request)
    task = _dl_find_task(request, body)
    if task is None:
        return await _dl_forward(request)
    return JSONResponse(content={
        "code": 0, "msg": "",
        "data": {"status": _dl_state_word(task), "errno": "",
                 "errmsg": str(task.get("errmsg") or ""), "downloadId": _task_guid_of(task),
                 "percent": int(task.get("percent", 0))},
    })


@app.api_route("/music/api/v1/download/track/transcode/file", methods=["GET", "HEAD"])
async def dl_transcode_file(request: Request):
    body = await _dl_body_json(request)
    task = _dl_find_task(request, body)
    if task is None:
        return await _dl_forward(request)
    path = task.get("path")
    if task["state"] != "completed" or not path or not os.path.isfile(path):
        return JSONResponse(content={"code": 404, "msg": "not ready", "data": None}, status_code=404)
    media = "audio/mpeg" if os.path.splitext(path)[1].lower() == ".mp3" else "audio/mp4"
    fname = os.path.basename(path)
    fn_ext = os.path.splitext(fname)[1]
    quoted = quote(fname, encoding="utf-8")
    ascii_fn = re.sub(r'[^\x20-\x7e]', '_', fname)
    if not ascii_fn.strip() or ascii_fn == fn_ext:
        ascii_fn = f"track{fn_ext}"
    disposition = f'attachment; filename="{ascii_fn}"; filename*=UTF-8\'\'{quoted}'
    return serve_file_with_range(path, request.headers.get("range"), media,
                                 extra_headers={"Content-Disposition": disposition})


@app.api_route("/music/api/v1/download/track/transcode/delete", methods=["GET", "POST", "DELETE"])
async def dl_transcode_delete(request: Request):
    body = await _dl_body_json(request)
    task = _dl_find_task(request, body)
    if task is None:
        return await _dl_forward(request)
    download_id = ""
    for key, value in list(_DL_TASKS.items()):
        if value is task:
            _DL_TASKS.pop(key, None)
            download_id = key
    return JSONResponse(content={
        "code": 0, "msg": "",
        "data": {"downloadId": download_id, "deleted": True},
    })


async def _online_info(request: Request, guid: str, include_lyric: bool = True) -> dict | None:
    retained, entry = _retained_track(request, guid)
    if not _source_enabled(guid):
        return retained
    try:
        data = await asyncio.wait_for(_fetch_online_info(request, guid, include_lyric=include_lyric), timeout=4.0)
        if data:
            return data
        if await _recover_source(request, guid, entry):
            data = await asyncio.wait_for(_fetch_online_info(request, guid, include_lyric=include_lyric), timeout=3.0)
            if data:
                return data
    except Exception:
        pass
    return retained


async def _fetch_online_info(request: Request, guid: str, include_lyric: bool = True) -> dict | None:
    src = source_from_online_guid(guid)
    if src == "netease":
        musicbox_client = get_musicbox_client(request.app)
        raw_song_id = song_id_from_online_guid(guid)
        song_id = raw_song_id.split(":")[-1]
        try:
            r = await musicbox_client.get(f"/api/v1/song/{song_id}/info", timeout=10.0)
            if r.status_code == 200:
                res_data = r.json()
                if isinstance(res_data, dict) and res_data.get("ok") is not False:
                    data = res_data.get("data")
                    if isinstance(data, dict):
                        name = str(data.get("name") or "")
                        ar = data.get("ar") or []
                        ar_names = []
                        if isinstance(ar, list):
                            for x in ar:
                                if isinstance(x, dict) and x.get("name"):
                                    ar_names.append(str(x["name"]))
                                elif isinstance(x, str):
                                    ar_names.append(x)
                        artist = " / ".join(ar_names)
                        al = data.get("al") or {}
                        album_name = str(al.get("name") or "") if isinstance(al, dict) else ""
                        cover_url = str(al.get("picUrl") or "") if isinstance(al, dict) else ""
                        dt = data.get("dt") or 0
                        duration_s = float(dt) / 1000.0 if dt else 0.0
                        sq = data.get("sq")
                        hr = data.get("hr")
                        h = data.get("h") or {}
                        ext = "flac" if (sq or hr) else "mp3"
                        size_obj = sq or h or {}
                        file_size = int(size_obj.get("size", 0) or 0) if isinstance(size_obj, dict) else 0

                        lyric_text = ""
                        if include_lyric:
                            try:
                                lr = await musicbox_client.get(f"/api/v1/song/{song_id}/lyric", timeout=10.0)
                                if lr.status_code == 200:
                                    l_res = lr.json()
                                    if isinstance(l_res, dict) and l_res.get("ok") is not False:
                                        l_data = l_res.get("data")
                                        if isinstance(l_data, dict):
                                            lyric_text = str(l_data.get("lyric") or "").strip()
                            except Exception as l_err:
                                logger.warning("musicbox lyric fetch in _online_info failed for %s: %s", guid, l_err)

                        return {
                            "id": f"netease:{song_id}",
                            "source": "netease",
                            "title": name,
                            "artist": artist,
                            "album": album_name,
                            "cover_url": cover_url,
                            "duration_s": duration_s,
                            "ext": ext,
                            "file_size": file_size,
                            "lyric": lyric_text,
                        }
        except Exception as e:
            logger.warning("musicbox /info failed for %s: %s", guid, e)
        return None

    if src == "lx":
        lx_client = get_lx_client(request.app)
        song_id = song_id_from_online_guid(guid)
        try:
            r = await lx_client.get("/api/v1/track/info", params={"id": song_id}, timeout=10.0)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("ok") is not False:
                    inner = data.get("data")
                    if isinstance(inner, dict):
                        lyric_text = str(inner.get("lyric") or "").strip()
                        if not lyric_text and include_lyric:
                            try:
                                lr = await lx_client.get(
                                    "/api/v1/track/lyric", params={"id": song_id}, timeout=10.0
                                )
                                if lr.status_code == 200:
                                    l_res = lr.json()
                                    if isinstance(l_res, dict) and l_res.get("ok") is not False:
                                        lyric_text = str((l_res.get("data") or {}).get("lyric") or "").strip()
                            except Exception as l_err:
                                logger.warning("lxmusic lyric fetch failed for %s: %s", guid, l_err)
                        return {
                            "id": song_id,
                            "source": "lx",
                            "lx_source": str(inner.get("lx_source") or ""),
                            "title": str(inner.get("title") or ""),
                            "artist": str(inner.get("artist") or ""),
                            "album": str(inner.get("album") or ""),
                            "cover_url": str(inner.get("cover_url") or ""),
                            "duration_s": float(inner.get("duration_s") or 0),
                            "ext": str(inner.get("ext") or "mp3") or "mp3",
                            "file_size": int(inner.get("file_size") or 0),
                            "lyric": lyric_text,
                        }
        except Exception as e:
            logger.warning("lxmusic /info failed for %s: %s", guid, e)
        return None

    musicdl_client = get_musicdl_client(request.app)
    song_id = song_id_from_online_guid(guid)
    try:
        r = await musicdl_client.get("/info", params={"id": song_id}, timeout=10.0)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and data.get("ok") is not False:
                return data
    except Exception as e:
        logger.warning("musicdl /info failed for %s: %s", guid, e)
    return None


@app.get("/music/api/v1/lyric/list")
@app.get("/music/api/v1/lyric/list/{subpath:path}")
async def lyric_list(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    lyric_text = await resolve_online_lyric(request, guid)
    return JSONResponse(content=disguise_client_json(build_lyric_list_payload(guid, lyric_text)))


@app.get("/music/api/v1/track/lyrics")
@app.get("/music/api/v1/track/lyrics/{subpath:path}")
@app.get("/music/api/v1/detail/lyrics/{subpath:path}")
async def track_lyrics(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    lyric_text = await resolve_online_lyric(request, guid)
    if lyric_text:
        res = {"code": 0, "msg": "ok", "data": {"guid": guid, "lyric": lyric_text}}
        set_by_path(res, CONF["lyric_field"], lyric_text)
        return JSONResponse(content=disguise_client_json(res))
    return empty_ok()


@app.get("/music/api/v1/track/metadata")
@app.get("/music/api/v1/track/metadata/{subpath:path}")
@app.get("/music/api/v1/track/audio-info")
async def track_metadata(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    data = await _online_info(request, guid) or stub_online_info(guid)
    cached_lyric = read_lyric_cache(guid)
    if cached_lyric:
        data = {**data, "lyric": cached_lyric}
    elif data.get("lyric"):
        write_lyric_cache(
            guid,
            str(data.get("lyric") or ""),
            title=str(data.get("title") or ""),
            artist=str(data.get("artist") or ""),
        )
    payload = build_metadata_payload(guid, data)
    if guid in await _online_favorite_set(request):
        if isinstance(payload.get("data"), dict):
            payload["data"]["isFavorite"] = True
            if isinstance(payload["data"].get("track"), dict):
                payload["data"]["track"]["isFavorite"] = True
    return JSONResponse(content=disguise_client_json(payload))


# === 封面兜底链：cover_url 302 → 源 CDN 直构（kw rid / qq albummid）→
# 网易 cloudsearch 补全 → 本地缓存文件内嵌图 → 本地占位图池。
# 在线曲目封面永不 404（空封面是客户端裂图的主要来源）。 ===
_COVER_CDN_CACHE: dict[str, tuple[str, float]] = {}
_COVER_CDN_TTL_S = 7 * 86400.0
_KW_TEXT_COVER_HOST = dailyrec.KW_TEXT_COVER_HOST
_PLACEHOLDER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "covers")
_PLACEHOLDER_COUNT = 6
_PLACEHOLDER_CACHE: dict[str, bytes] = {}
# 1x1 灰点：占位图文件意外缺失时的最终兜底，保证响应仍是合法图片
_TINY_GRAY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\xf0\x1f\x00\x05\x05\x02\x00_\xc8\xe7A\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _cover_cache_get(key: str) -> str:
    hit = _COVER_CDN_CACHE.get(key)
    if hit and time.time() - hit[1] < _COVER_CDN_TTL_S:
        return hit[0]
    _COVER_CDN_CACHE.pop(key, None)
    return ""


def _cover_cache_put(key: str, url: str) -> None:
    if len(_COVER_CDN_CACHE) > 2000:
        _COVER_CDN_CACHE.clear()
    _COVER_CDN_CACHE[key] = (url, time.time())


def _kw_rid_from_guid(guid: str) -> str:
    """酷我 rid：lx 平台 online:lx:kw:<rid>；musicdl online:kuwo:<rid>。"""
    parts = (guid or "").split(":")
    if len(parts) >= 4 and parts[1] == "lx" and parts[2] == "kw":
        return parts[3]
    if len(parts) == 3 and parts[1] in ("kw", "kuwo"):
        return parts[2]
    return ""


def _cover_cdn_client() -> httpx.AsyncClient:
    # 直构 CDN 探测用独立短超时客户端；transport 仅供测试注入 MockTransport
    kwargs: dict = {"timeout": 5.0, "follow_redirects": True}
    if _COVER_CDN_TRANSPORT is not None:
        kwargs["transport"] = _COVER_CDN_TRANSPORT
    return httpx.AsyncClient(**kwargs)


_COVER_CDN_TRANSPORT: "httpx.AsyncBaseTransport | None" = None


async def _kw_cover_by_rid(rid: str) -> str:
    """酷我 artistpicserver 按 rid 取真图（接口返回文本，需解析出图片地址）。"""
    rid = str(rid or "").strip()
    if not rid.isdigit():
        return ""
    cached = _cover_cache_get(f"kw:{rid}")
    if cached:
        return cached
    url = (
        "https://artistpicserver.kuwo.cn/pic?corp=kuwo&type=rid_pic"
        f"&pictype=500&size=500&rid={rid}"
    )
    try:
        async with _cover_cdn_client() as client:
            r = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.kuwo.cn/"},
            )
        if r.status_code != 200:
            return ""
        m = re.search(r"https?://[^\s'\"<>]+?\.(?:jpg|jpeg|png|webp)", r.text, re.IGNORECASE)
        if not m:
            return ""
        real = m.group(0)
    except Exception as e:
        logger.debug("kw artistpic resolve failed for rid=%s: %s", rid, e)
        return ""
    _cover_cache_put(f"kw:{rid}", real)
    return real


def _qq_cover_by_albummid(albummid: "str | None") -> str:
    mid = re.sub(r"[^0-9A-Za-z]", "", str(albummid or ""))
    if len(mid) < 8:
        return ""
    return f"https://y.gtimg.cn/music/photo_new/T002R800x800M000{mid}.jpg?max_age=2592000"


async def _enrich_cover_via_netease(request: Request, guid: str, data: dict) -> str:
    """musicdl/lx 空封面 → 网易 cloudsearch 同名曲补全（FNMUSIC_COVER_ENRICH=1）。"""
    if not CONF.get("cover_enrich", True):
        return ""
    title = str((data or {}).get("title") or "").strip()
    if not title:
        return ""
    artist = str((data or {}).get("artist") or "").strip()
    key = f"wy:{title.casefold()}|{artist.casefold()}"
    cached = _cover_cache_get(key)
    if cached:
        return cached
    musicbox_client = get_musicbox_client(request.app)
    keyword = " ".join(x for x in (artist, title) if x)
    target = ""
    try:
        r = await musicbox_client.get(
            "/api/v1/search",
            params={"keyword": keyword, "limit": 5, "type": "song"},
            timeout=8.0,
        )
        if r.status_code != 200:
            return ""
        payload = r.json()
        raw = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            return ""
        title_l = title.casefold()
        artist_l = artist.casefold()
        for it in raw:
            if not isinstance(it, dict):
                continue
            it_title = str(it.get("song_name") or it.get("title") or "")
            if it_title.casefold() != title_l:
                continue
            it_artist = str(it.get("artist") or "")
            if artist_l and artist_l not in it_artist.casefold() and it_artist.casefold() not in artist_l:
                continue
            sid = str(it.get("song_id") or it.get("id") or "")
            if not sid:
                continue
            d = await musicbox_client.get("/api/v1/songs/detail", params={"ids": sid}, timeout=8.0)
            if d.status_code == 200:
                dj = d.json()
                dl = dj.get("data") if isinstance(dj, dict) else None
                first = dl[0] if isinstance(dl, list) and dl and isinstance(dl[0], dict) else {}
                target = str(first.get("album_pic_url") or "")
            break
    except Exception as e:
        logger.debug("cover enrich via netease failed for %s: %s", guid, e)
        return ""
    if target:
        _cover_cache_put(key, target)
    return target


def _embedded_cover_bytes(guid: str) -> "tuple[bytes, str] | None":
    """本地边听边存缓存文件的内嵌专辑图（mutagen 读 ID3/FLAC/MP4 封面）。"""
    path = find_cache_file(guid)
    if not path:
        return None
    try:
        from mutagen import File as MutagenFile

        mf = MutagenFile(path)
        tags = getattr(mf, "tags", None)
        if tags is None:
            return None
        pics: list = []
        if hasattr(tags, "pictures"):  # FLAC/APE
            pics = list(tags.pictures or [])
        elif hasattr(tags, "getall"):  # ID3
            pics = list(tags.getall("APIC") or [])
        elif hasattr(tags, "get"):
            covr = tags.get("covr")  # MP4
            pics = list(covr) if covr else []
        for pic in pics:
            data = getattr(pic, "data", None)
            if not data:
                continue
            mime = str(getattr(pic, "mime", "") or "")
            if "/" not in mime:
                mime = "image/jpeg"
            return bytes(data), mime
    except Exception as e:
        logger.debug("embedded cover extract failed for %s: %s", guid, e)
    return None


def _placeholder_cover_response(guid: str) -> Response:
    """占位图池确定性选取（guid 哈希），客户端缓存 1 天。"""
    digest = hashlib.sha256(str(guid or "").encode()).hexdigest()
    name = f"placeholder-{int(digest[:8], 16) % _PLACEHOLDER_COUNT}.png"
    content = _PLACEHOLDER_CACHE.get(name)
    if content is None:
        try:
            with open(os.path.join(_PLACEHOLDER_DIR, name), "rb") as f:
                content = f.read()
            _PLACEHOLDER_CACHE[name] = content
        except OSError:
            logger.warning("占位图缺失: %s", os.path.join(_PLACEHOLDER_DIR, name))
            content = _TINY_GRAY_PNG
    return Response(
        content=content,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.api_route("/music/api/v1/static/cover", methods=["GET", "HEAD"])
@app.api_route("/music/api/v1/static/cover/{subpath:path}", methods=["GET", "HEAD"])
async def static_cover(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not guid and subpath.startswith("online:"):
        guid = subpath
    playlist_kind = dailyrec.online_playlist_kind(guid)
    if playlist_kind:
        upstream_client = get_upstream_client(request.app)
        is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
        if not is_authed and auth_resp is not None:
            return auth_resp
        cached = dailyrec.load_daily_cache(user_guid, dailyrec.today_key(), playlist_kind)
        tracks = (cached or {}).get("tracks") or []
        picked = dailyrec.pick_playlist_cover_track(tracks)
        picked_guid = str((picked or {}).get("guid") or "")
        if not is_online_guid(picked_guid):
            cover_id = str((picked or {}).get("coverId") or "")
            if picked and cover_id and not is_online_guid(cover_id):
                # 本地曲目封面：coverId 是真实官方封面 guid，透传官方静态封面端点
                upstream_client = get_upstream_client(request.app)
                headers = copy_incoming_headers(request)
                cover_req = upstream_client.build_request(
                    request.method,
                    f"/music/api/v1/static/cover?coverId={quote(cover_id, safe='')}",
                    headers=headers,
                )
                cover_resp = await upstream_client.send(cover_req, stream=True)
                cover_headers = filter_headers(
                    cover_resp.headers, exclude_keys={"content-length", "content-encoding"}
                )

                async def _cover_stream():
                    try:
                        async for chunk in cover_resp.aiter_bytes():
                            yield chunk
                    finally:
                        await cover_resp.aclose()

                return StreamingResponse(
                    _cover_stream(), status_code=cover_resp.status_code, headers=cover_headers
                )
            # 歌单里没有可用封面：不显示图标，客户端回落自带默认样式
            return Response(status_code=404)
        guid = picked_guid
    # 网易账号歌单封面：登记的封面直链直接 302（兼容重启后反查表未重建的裸假 id）
    nm_pl_id = nmpl.playlist_id_from_cover_request(guid, request.query_params.get("coverId") or subpath)
    if nm_pl_id:
        cover = nmpl.cover_url_for(nm_pl_id)
        if cover:
            return RedirectResponse(cover, status_code=302)
        return Response(status_code=404)
    lx_pid = (lxsync.lxsync_playlist_id_from_guid(guid)
              or lxsync.lxsync_playlist_id_from_guid(resolve_real_guid(guid)))
    if lx_pid:
        # 洛雪歌单没有独立封面：用第一首能拿到封面的歌的 picUrl，取不到就用默认图（404）
        cover = lxsync.cover_url_for(lx_pid)
        if cover:
            return RedirectResponse(cover, status_code=302)
        return Response(status_code=404)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    # 专辑锚点 guid（/search/album 在线专辑的 coverId）：登记时存了封面直链，直接 302
    album_entry = album_entry_from_real_guid(guid)
    if album_entry is not None:
        cover = str((album_entry.get("item") or {}).get("cover_url") or "")
        if cover and _KW_TEXT_COVER_HOST not in cover:
            return RedirectResponse(cover, status_code=302)
        return _placeholder_cover_response(guid)

    data = await _online_info(request, guid)
    cover = str((data or {}).get("cover_url") or "")
    # ① 已知直链封面直接 302；酷我文本封面（artistpicserver 返回的是文本页）除外
    if cover and _KW_TEXT_COVER_HOST not in cover:
        return RedirectResponse(cover, status_code=302)

    # ② 按源+ID 直构 CDN：kw rid → artistpicserver 真图；qq albummid → gtimg
    direct = ""
    kw_rid = _kw_rid_from_guid(guid)
    if kw_rid:
        direct = await _kw_cover_by_rid(kw_rid)
    if not direct:
        direct = _qq_cover_by_albummid((data or {}).get("albummid"))
    if direct:
        return RedirectResponse(direct, status_code=302)

    # ③ 网易 cloudsearch 同名曲补全
    enriched = await _enrich_cover_via_netease(request, guid, data or {})
    if enriched:
        return RedirectResponse(enriched, status_code=302)

    # ④ 本地边听边存文件的内嵌专辑图
    embedded = _embedded_cover_bytes(guid)
    if embedded:
        art, mime = embedded
        return Response(content=art, media_type=mime, headers={"Cache-Control": "public, max-age=86400"})

    # ⑤ 本地占位图池：在线曲目封面永不 404
    return _placeholder_cover_response(guid)


# === online favorites ===

_FAV_LOCK = asyncio.Lock()


def sanitize_user_guid(guid: str | None) -> str:
    """过滤文件名合法字符 [A-Za-z0-9-_]，非法字符替换为 _；为空则返回 'shared'。"""
    raw = str(guid or "").strip()
    safe = re.sub(r"[^A-Za-z0-9\-_]", "_", raw)
    return safe or "shared"


def user_fav_path(user_guid: str) -> str:
    fav_dir = CONF.get("fav_dir") or os.path.join(_HOME, "online_favorites")
    safe_name = sanitize_user_guid(user_guid)
    return os.path.join(fav_dir, f"{safe_name}.json")


def load_online_favorites(user_guid: str) -> list[dict]:
    path = user_fav_path(user_guid)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data["items"]
            if isinstance(data, list):
                return data
    except Exception as e:
        logger.warning("Failed to load online favorites for %s from %s: %s", user_guid, path, e)
    return []


def save_online_favorites(user_guid: str, items: list[dict]) -> bool:
    path = user_fav_path(user_guid)
    parent = os.path.dirname(path) or "."
    part_path = f"{path}.{uuid4().hex[:8]}.part"
    try:
        os.makedirs(parent, exist_ok=True)
        with open(part_path, "w", encoding="utf-8") as f:
            json.dump({"items": items}, f, ensure_ascii=False, indent=2)
        os.replace(part_path, path)
        return True
    except Exception as e:
        logger.warning("Failed to save online favorites for %s to %s: %s", user_guid, path, e)
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except Exception:
                pass
        return False


def official_track_template(official_list: list) -> dict | None:
    """取一条真实官方 track 作结构模板：App 端反序列化严格，在线条目字段集需与官方对齐。"""
    for item in official_list:
        if isinstance(item, dict):
            g = str(item.get("guid") or "")
            if g and not is_online_guid(g):
                return deepcopy(item)
    return None


def _snapshot_to_info(track: dict) -> dict:
    """把落盘的 track 对象（最终形状）还原成 build_online_track 的 info 输入形状。

    收藏/历史快照是最终 track 对象（artists 数组、duration 毫秒、audioSpec），
    重建时需映射回 artist/duration_s/ext；info 形状的历史快照字段已对齐则原样保留。
    """
    info = dict(track)
    artists = track.get("artists")
    if not info.get("artist") and isinstance(artists, list) and artists and isinstance(artists[0], dict):
        info["artist"] = artists[0].get("name") or ""
    album = track.get("album")
    if isinstance(album, dict):
        info["album"] = album.get("name") or ""
    if not info.get("duration_s") and track.get("duration"):
        try:
            info["duration_s"] = float(track["duration"]) / 1000.0
        except (TypeError, ValueError):
            pass
    spec = track.get("audioSpec")
    if isinstance(spec, dict):
        info.setdefault("ext", spec.get("format") or "mp3")
        info.setdefault("file_size", spec.get("size") or 0)
        if not info.get("cover_url"):
            info["cover_url"] = track.get("coverUrl") or track.get("cover_url") or ""
    return info


def build_favorite_track_obj(
    guid: str,
    info: dict | None = None,
    created_at: int | None = None,
    template: dict | None = None,
) -> dict:
    raw_info = dict(info or {})
    raw_info.setdefault("id", song_id_from_online_guid(guid))
    raw_info.setdefault("source", source_from_online_guid(guid))
    vo = build_online_track(raw_info)

    now = int(time.time())
    ts = created_at or now

    # App 端解析严格：artists 永不为空、album.name 永不为空，避免整列表被丢弃。
    # 官方习惯：artist.coverId 为 null（无独立歌手封面），不能填歌曲 guid。
    artist_name = vo.get("artist") or "未知艺术家"
    artists_list = [
        {
            "guid": f"{guid}:artist",
            "name": artist_name,
            "coverId": None,
            "createdAt": ts,
            "updatedAt": ts,
        }
    ]

    album_name = (
        vo.get("albumName")
        or (vo.get("album", {}).get("name") if isinstance(vo.get("album"), dict) else "")
        or "未知专辑"
    )
    # 官方 album 对象没有 artists 键；releaseDate/barcode 缺省为 null 而非 0/""
    album_obj = {
        "guid": f"{guid}:album",
        "name": album_name,
        "coverId": guid,
        "releaseDate": None,
        "barcode": None,
        "createdAt": ts,
        "updatedAt": ts,
    }

    audio_spec = vo.get("audioSpec") or {}

    obj = {
        "guid": guid,
        "title": vo.get("title") or "",
        "duration": vo.get("duration") or 0,
        "isFavorite": True,
        "isCue": False,
        "genres": [],
        "artists": artists_list,
        "album": album_obj,
        "audioSpec": audio_spec,
        "accessStatus": 0,
        "coverId": guid,
        "year": None,
        "discNo": None,
        "trackNo": None,
        "isrc": None,
        "createdAt": ts,
        "updatedAt": ts,
    }

    if isinstance(template, dict) and template:
        # 以官方真实条目为结构底版，再用在线字段全覆盖：本侧未构造的官方字段
        # 保留官方结构，避免 App 因缺字段整列表解析失败。
        merged = deepcopy(template)
        merged.update(obj)
        return merged
    return obj


async def _probe_upstream_auth(request: Request, client: httpx.AsyncClient) -> tuple[bool, str, Response | None]:
    """向上游探测用户是否已登录。复用当前请求 headers。
    返回 (is_authed, user_guid, error_response)。
    """
    headers = copy_incoming_headers(request)
    try:
        probe_req = client.build_request("GET", "/music/api/v1/user/me", headers=headers)
        probe_resp = await client.send(probe_req)
        resp_headers = filter_headers(probe_resp.headers, exclude_keys={"content-length", "content-encoding"})

        if probe_resp.status_code == 401:
            return False, "", Response(
                content=probe_resp.content,
                status_code=401,
                headers=resp_headers,
                media_type=probe_resp.headers.get("content-type"),
            )

        if probe_resp.status_code == 200:
            try:
                probe_json = probe_resp.json()
                if isinstance(probe_json, dict) and probe_json.get("code") == 99999:
                    return False, "", JSONResponse(
                        content=probe_json,
                        status_code=200,
                        headers=resp_headers,
                    )
                if isinstance(probe_json, dict) and probe_json.get("code") == 0:
                    data = probe_json.get("data")
                    if isinstance(data, dict) and data.get("guid"):
                        return True, str(data["guid"]), None
                    logger.warning("user/me response missing data.guid, falling back to 'shared': %s", probe_json)
                    return True, "shared", None
            except Exception as e:
                logger.warning("Failed to parse user/me json response: %s", e)
                return True, "shared", None
            return True, "shared", None

        # 其他非 200/401 状态码，上游异常
        return True, "shared", None
    except Exception as e:
        logger.warning("Upstream auth probe failed: %s", e)
        # 探测异常时保守放行
        return True, "shared", None


async def _online_favorite_set(request: Request) -> set[str]:
    """当前用户在线收藏 guid 集合（探测失败回退 shared/空集，绝不抛错）。"""
    try:
        upstream_client = get_upstream_client(request.app)
        is_authed, user_guid, _resp = await _probe_upstream_auth(request, upstream_client)
        if not is_authed:
            return set()
        async with _FAV_LOCK:
            return {str(it.get("guid") or "") for it in load_online_favorites(user_guid)}
    except Exception:
        return set()


async def _best_effort_online_info(request: Request, guid: str) -> dict:
    """尽力获取在线曲目元数据：拉不到时从缓存反查兜底，绝不为 None（收藏/歌单快照共用）。"""
    info = await _online_info(request, guid)
    if info:
        return info
    cached_lyric = read_lyric_cache(guid)
    title = ""
    artist = ""
    cached_media = find_cache_file(guid)
    if cached_media:
        base = os.path.splitext(os.path.basename(cached_media))[0]
        if " - " in base:
            artist, title = base.split(" - ", 1)
        else:
            title = base
    return {
        "id": song_id_from_online_guid(guid),
        "source": source_from_online_guid(guid),
        "title": title,
        "artist": artist,
        "lyric": cached_lyric,
    }


def _netease_song_id(guid: str) -> str:
    """从在线 guid 里取出**纯数字**的网易云歌曲 id；不是网易云来源则返回空。

    两个必须挡住的坑：
    1. ``song_id_from_online_guid`` 返回的是 ``netease:228908`` 这种带来源的串，
       直接 ``int()`` 会 ValueError —— 项目里其它地方一律再 ``.split(":")[-1]``。
    2. guid 也可能是 ``online:kuwo:123`` 这类**非网易云**来源，绝不能拿去加红心，
       那是往用户网易云账号里写一条不相干的歌。
    """
    g = str(guid or "")
    if not is_online_guid(g):
        return ""
    src = source_from_online_guid(g)
    if src and src != "netease":
        return ""
    sid = song_id_from_online_guid(g).split(":")[-1].strip()
    return sid if sid.isdigit() else ""


async def _sync_netease_like(client, song_id: str, like: bool) -> dict[str, Any]:
    """收藏 <-> 网易云红心（由 FNMUSIC_FAV_SYNC_LIKE 把关，默认关）。

    这是**对用户账号的写操作**，因此不静默失败：结果如实回传给调用方与日志，
    管理页能据此看出到底是没登录、上游拒绝，还是取消红心那条未验证分支不通。
    """
    if not downloader.like_sync_enabled():
        return {"ok": False, "skipped": "disabled"}
    if not str(song_id).isdigit():
        return {"ok": False, "error": "invalid_song_id"}
    try:
        r = await client.post(f"/api/v1/song/{int(song_id)}/like",
                              params={"like": "true" if like else "false"}, timeout=15.0)
        if r.status_code != 200:
            return {"ok": False, "error": f"http_{r.status_code}"}
        body = r.json()
        if not isinstance(body, dict):
            return {"ok": False, "error": "bad_payload"}
        if body.get("ok") is not True:
            return {"ok": False, "error": str(body.get("error") or "upstream_rejected")}
        return {"ok": True, "like": like}
    except Exception as exc:  # noqa: BLE001
        logger.warning("netease like sync failed song=%s like=%s: %s: %s",
                       song_id, like, type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:160]}


@app.post("/music/api/v1/favorite-track/create")
async def favorite_track_create(request: Request):
    upstream_client = get_upstream_client(request.app)
    try:
        body = await request.json()
    except Exception:
        body = {}

    guid = ""
    if isinstance(body, dict):
        guid = resolve_real_guid(str(body.get("trackGUID") or body.get("guid") or "").strip())

    if not is_online_guid(guid):
        return await forward_to_upstream(request, upstream_client)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    now = int(time.time())
    info = await _best_effort_online_info(request, guid)
    track_obj = build_favorite_track_obj(guid, info, created_at=now)

    saved = False
    autobind = bool(CONF.get("fav_auto_bind"))
    async with _FAV_LOCK:
        try:
            items = load_online_favorites(user_guid)
            # 查重
            idx = next((i for i, it in enumerate(items) if it.get("guid") == guid), None)
            if idx is not None:
                # 幂等更新（重新收藏视为新操作，官方绑定开关开启时补 pending 标记）
                items[idx]["track"] = track_obj
                if autobind:
                    items[idx]["bind"] = "pending"
            else:
                item = {
                    "guid": guid,
                    "createdAt": now,
                    "track": track_obj,
                }
                if autobind:
                    item["bind"] = "pending"
                items.append(item)
            save_online_favorites(user_guid, items)
            saved = True
        except Exception as e:
            logger.warning("Error updating online favorites for user %s: %s", user_guid, e)

    if saved:
        _register_fav_autobind(request, guid, user_guid)

        # 收藏同步回网易云（加红心，FNMUSIC_FAV_SYNC_LIKE 把关）+ 归档下载
        # （FNMUSIC_DOWNLOAD_ON_FAVORITE + FNMUSIC_DOWNLOAD_DIR 把关，默认关）。
        # 两者都只能尽力而为：本地收藏已经写成功了，任何一步失败都不该让这个接口报错，
        # 否则用户点一下收藏就看到红叉，反而把本来能用的功能弄坏。
        sid = _netease_song_id(guid)
        if sid:
            like_res = await _sync_netease_like(get_musicbox_client(request.app), sid, like=True)
            if not like_res.get("ok") and not like_res.get("skipped"):
                # 不静默：红心没加上必须留痕，否则用户以为同步了其实没有
                logger.warning("netease like not applied for song %s: %s", sid, like_res)
            if downloader.download_enabled():
                try:
                    state = downloader.enqueue(
                        get_musicbox_client(request.app), sid,
                        {"title": (info or {}).get("title") or "",
                         "artist": (info or {}).get("artist") or "",
                         "album": (info or {}).get("album") or ""},
                        ref_writer=lambda path: remember_archive_path(guid, path),
                    )
                except Exception as exc:  # noqa: BLE001 —— 归档失败绝不能弄坏收藏
                    state = f"error: {type(exc).__name__}: {exc}"
                logger.info("archive enqueued for song %s (%s)", sid, state)

    # ⚠️ data 必须保持 None：这是飞牛官方接口的响应形状，既有客户端按它解析。
    #    归档/红心的执行结果走日志，不要为了"回传细节"去改线上协议。
    return JSONResponse(content={"code": 0, "msg": "", "data": None})


@app.post("/music/api/v1/favorite-track/delete")
async def favorite_track_delete(request: Request):
    upstream_client = get_upstream_client(request.app)
    try:
        body = await request.json()
    except Exception:
        body = {}

    guid = ""
    raw_guid = ""
    if isinstance(body, dict):
        raw_guid = str(body.get("trackGUID") or body.get("guid") or "").strip()
        guid = resolve_real_guid(raw_guid)

    if not is_online_guid(guid):
        # 官方绑定已完成的歌曲本地映射已删，客户端旧会话可能仍持伪装 id 取消
        # 收藏：登记命中则翻译成官方 guid 撤销官方收藏；未命中照旧透传官方
        hit = _lookup_bind_any_user(raw_guid)
        if hit and hit[2].get("fav"):
            bound_user, bound_guid, entry = hit
            ok = await _official_upstream_write(
                "POST", "/music/api/v1/favorite-track/delete",
                copy_incoming_headers(request), {"trackGUID": entry["official"]},
            )
            if ok:
                _forget_bind(bound_user, bound_guid, fav=True)
            return JSONResponse(content={"code": 0, "msg": "", "data": None})
        return await forward_to_upstream(request, upstream_client)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    async with _FAV_LOCK:
        try:
            items = load_online_favorites(user_guid)
            items = [it for it in items if it.get("guid") != guid]
            save_online_favorites(user_guid, items)
        except Exception as e:
            logger.warning("Error deleting from online favorites for user %s: %s", user_guid, e)

    entry = lookup_bind_entry(user_guid, guid)
    if entry and entry.get("fav"):
        # 该曲已完成官方绑定（旧会话假 id）：同步撤销官方收藏
        if await _official_upstream_write(
            "POST", "/music/api/v1/favorite-track/delete",
            copy_incoming_headers(request), {"trackGUID": entry["official"]},
        ):
            _forget_bind(user_guid, guid, fav=True)

    # 取消收藏同步撤销网易云红心（双向同步，由 FNMUSIC_FAV_SYNC_LIKE 把关）。
    # 归档文件不删：那是用户主动要求下载到自定义目录的资产，取消收藏不等于要删文件。
    sid = _netease_song_id(guid)
    if sid:
        res = await _sync_netease_like(get_musicbox_client(request.app), sid, like=False)
        if not res.get("ok") and not res.get("skipped"):
            logger.warning("netease unlike not applied for song %s: %s", sid, res)

    return JSONResponse(content={"code": 0, "msg": "", "data": None})


@app.get("/music/api/v1/favorite-track/list")
async def favorite_track_list(request: Request):
    upstream_client = get_upstream_client(request.app)
    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)

    req = upstream_client.build_request("GET", url_path, headers=headers)
    upstream_resp = await upstream_client.send(req)
    resp_headers = filter_headers(upstream_resp.headers, exclude_keys={"content-length", "content-encoding"})

    if upstream_resp.status_code != 200:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    try:
        upstream_json = upstream_resp.json()
    except Exception:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    if not isinstance(upstream_json, dict) or upstream_json.get("code") != 0:
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    # 探测当前用户身份。官方列表已取回成功（同一组请求头），此时探测被拒
    # （如 App 一次性票据已被首次请求消耗）不能回传鉴权错误导致整列表空白，
    # 降级为仅返回官方列表。
    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed:
        logger.warning("favorite list degraded to official-only: auth probe rejected after official list ok")
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    # 成功获取官方列表，合并本地在线收藏
    data = upstream_json.get("data")
    if not isinstance(data, dict):
        data = {"list": [], "total": 0}
        upstream_json["data"] = data

    official_list = data.get("list")
    if not isinstance(official_list, list):
        official_list = []
        data["list"] = official_list

    # 飞牛音乐前端收藏列表依赖 isFavorite=True 状态判断，遍历补齐官方列表中可能缺失的字段
    for item in official_list:
        if isinstance(item, dict):
            item["isFavorite"] = True

    official_total = data.get("total")
    if not isinstance(official_total, int):
        official_total = len(official_list)

    async with _FAV_LOCK:
        try:
            fav_items = load_online_favorites(user_guid)
        except Exception as e:
            logger.warning("Error reading online favorites for list for user %s: %s", user_guid, e)
            fav_items = []

    # 自愈：bind=pending 的收藏在此补发官方绑定/下载（含代理重启后的恢复）
    _maybe_retry_official_binds(request, user_guid, fav_items=fav_items)

    # 官方真实条目作为结构模板：App 端解析严格，在线条目字段集需与官方对齐；
    # 旧数据存的快照也统一重建，避免历史遗留形状继续下发。
    template = official_track_template(official_list)

    # 按 createdAt 倒序
    fav_items_sorted = sorted(fav_items, key=lambda x: x.get("createdAt", 0), reverse=True)
    online_tracks = []
    for it in fav_items_sorted:
        g = str(it.get("guid") or "")
        if not g:
            continue
        snapshot = it.get("track") if isinstance(it.get("track"), dict) else None
        obj = build_favorite_track_obj(g, _snapshot_to_info(snapshot) if snapshot else None, created_at=it.get("createdAt"), template=template)
        obj["isFavorite"] = True
        online_tracks.append(disguise_client_json(obj))

    data["list"] = official_list + online_tracks
    data["total"] = official_total + len(online_tracks)

    return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)


# === daily recommend + play history ===

_HISTORY_LOCK = asyncio.Lock()
_DAILY_TASKS: dict[str, asyncio.Task] = {}  # key: f"{user}:{kind}:{day}"
# 空结果构建冷却：key 同 _DAILY_TASKS，value=该次空结果的 builtAt（见 _ensure_daily_task）
_EMPTY_BUILD_COOLDOWN: dict[str, float] = {}
_EMPTY_BUILD_COOLDOWN_S = 300.0


def _prune_stale_daily_tasks(day: str) -> None:
    stale = [k for k in list(_DAILY_TASKS) if not str(k).endswith(f":{day}")]
    for k in stale:
        old = _DAILY_TASKS.pop(k, None)
        if old is not None and not old.done():
            old.cancel()
    for k in [k for k in _EMPTY_BUILD_COOLDOWN if not str(k).endswith(f":{day}")]:
        _EMPTY_BUILD_COOLDOWN.pop(k, None)


def _recommend_kind_enabled(kind: str) -> bool:
    if kind == "hot":
        return bool(CONF.get("recommend_hot", True))
    return bool(CONF.get("recommend_daily", True))


def _recommend_kinds_enabled() -> list[str]:
    """按开关返回要注入的推荐歌单类型（顺序即歌单列表顺序）。"""
    return [k for k in ("daily", "hot") if _recommend_kind_enabled(k)]


def _is_shared_user_guid(user_guid: str) -> bool:
    """身份探测未解析到真实用户 guid（保守回落 shared）的会话。"""
    return str(user_guid or "").strip() == "shared"


def _recommend_injectable_kinds(user_guid: str) -> list[str]:
    """按用户身份返回可注入（可构建）的推荐歌单类型，注入/生成侧统一判定。

    issue #25 多用户隔离：_probe_upstream_auth 失败/解析异常/guid 缺失时所有用户
    都回落 user_guid="shared"。数据来源结论（recommend.py）：
      - daily 是个性化歌单——种子取个人 play_history（本地+在线）与个人收藏，
        网易日推也是已登录账号的个性化内容；shared 会话注入会让身份探测失败的
        所有用户共享同一份"每日推荐"，混合历史生成的歌单跨用户泄露，必须跳过。
      - hot 是公共榜单——构建链（网易热歌榜/lx 免登录榜单）不读任何个人历史与
        收藏、不做按用户排除，内容与用户身份无关（缓存仅按用户分目录存放，
        不含任何用户数据），shared 注入无隔离风险。
    生成侧同样收口：seeds 只按各自 user_guid 文件读取，shared 历史
    （play_history/shared.json）不会进入任何真实用户的 daily 种子；shared 的
    daily 经本判定在 _peek/_load 处直接短路，绝不触发构建。
    """
    kinds = _recommend_kinds_enabled()
    if _is_shared_user_guid(user_guid):
        return [k for k in kinds if k == "hot"]
    return kinds


async def _ensure_daily_task(request: Request, user_guid: str, kind: str = "daily") -> asyncio.Task:
    day = dailyrec.today_key()
    _prune_stale_daily_tasks(day)
    key = f"{user_guid}:{kind}:{day}"
    task = _DAILY_TASKS.get(key)
    if task is not None and not task.done():
        return task
    if task is not None and task.done():
        try:
            if task.exception() is None:
                result = task.result()
                if isinstance(result, dict) and len(result.get("tracks") or []) >= dailyrec.PLAYLIST_SIZE:
                    return task
                if isinstance(result, dict) and not result.get("tracks"):
                    # 空结果冷却：音源全挂/LLM 不可用时构建产出 0 首，缓存为空会让
                    # 每次打开列表都重新触发一轮构建（反复打搜索与 LLM）。5 分钟内
                    # 不重建，冷却过后自动重试（缓存非空时本分支不会到达）。
                    built_at = result.get("builtAt")
                    last = float(built_at) if isinstance(built_at, (int, float)) else time.time()
                    if time.time() - last < _EMPTY_BUILD_COOLDOWN_S:
                        _EMPTY_BUILD_COOLDOWN[key] = last
                        return task
                    _EMPTY_BUILD_COOLDOWN[key] = last
        except (asyncio.CancelledError, Exception):
            pass
    if time.time() - _EMPTY_BUILD_COOLDOWN.get(key, 0.0) < _EMPTY_BUILD_COOLDOWN_S:
        # 冷却期内：不重复构建（上方已把可返回的 done task 返回；此处是异常/无结果的兜底）
        done = _DAILY_TASKS.get(key)
        if done is not None:
            return done
    _EMPTY_BUILD_COOLDOWN.pop(key, None)
    async with _FAV_LOCK:
        favs = load_online_favorites(user_guid)
    task = asyncio.create_task(
        dailyrec.get_or_build_daily(
            user_guid=user_guid,
            musicdl_client=get_musicdl_client(request.app) if CONF.get("musicdl_enabled") else None,
            musicbox_client=get_musicbox_client(request.app) if CONF["netease_enabled"] else None,
            llm_http=get_llm_client(request.app) if dailyrec.llm_enabled() else None,
            build_track=build_online_track,
            netease_enabled=CONF["netease_enabled"],
            favorite_items=favs,
            lx_client=get_lx_client(request.app) if CONF.get("lx_enabled") else None,
            lx_enabled=bool(CONF.get("lx_enabled")),
            lx_sources=CONF.get("lx_sources") or None,
            recommend_hot=bool(CONF.get("recommend_hot", True)),
            recommend_daily=bool(CONF.get("recommend_daily", True)),
            kind=kind,
        )
    )
    _DAILY_TASKS[key] = task
    return task


async def _wait_progressive_bundle(
    request: Request, user_guid: str, kind: str, timeout_s: float,
) -> dict:
    """issue #23 渐进上架：后台构建期间轮询磁盘 checkpoint，出现任意一首即返回。

    recommend 构建已改为逐首 checkpoint（2s 节流落盘），这里 0.5s 轮询一次：
    歌单/曲目列表首屏必有歌（构建仍在继续，用户重进列表可见增长），不再
    空等整链构建完成。task 完成则返回最终结果；超时仍无任何曲目回占位歌单。
    """
    day = dailyrec.today_key()
    task = await _ensure_daily_task(request, user_guid, kind)
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=min(remaining, 0.5))
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            logger.warning("daily recommend build failed: %s", e)
            break
        cached = dailyrec.load_daily_cache(user_guid, day, kind)
        if cached and cached.get("tracks"):
            return cached
        if task.done():
            break
    cached = dailyrec.load_daily_cache(user_guid, day, kind)
    if cached and cached.get("tracks"):
        return cached
    return dailyrec.empty_daily_bundle(user_guid, kind)


async def _peek_daily_bundle(request: Request, user_guid: str, kind: str = "daily") -> dict:
    """歌单列表用：有缓存立刻返回；否则等渐进 checkpoint，最多 2s，超时仍返回占位歌单。"""
    if kind not in _recommend_injectable_kinds(user_guid):
        # 开关关闭或身份探测失败（shared）的个性化 daily：注入与生成一并跳过
        return dailyrec.empty_daily_bundle(user_guid, kind)
    day = dailyrec.today_key()
    dailyrec.purge_stale_daily_cache(user_guid, day)
    cached = dailyrec.load_daily_cache(user_guid, day, kind)
    if cached and cached.get("tracks"):
        return cached
    return await _wait_progressive_bundle(request, user_guid, kind, timeout_s=2.0)


async def _load_daily_bundle(request: Request, user_guid: str, kind: str = "daily") -> dict:
    if kind not in _recommend_injectable_kinds(user_guid):
        # 同 _peek_daily_bundle：shared 会话不构建/不下发个性化 daily（issue #25）
        return dailyrec.empty_daily_bundle(user_guid, kind)
    day = dailyrec.today_key()
    dailyrec.purge_stale_daily_cache(user_guid, day)
    cached = dailyrec.load_daily_cache(user_guid, day, kind)
    if cached and cached.get("tracks"):
        return cached
    return await _wait_progressive_bundle(request, user_guid, kind, timeout_s=20.0)


# === playlist tracks（官方歌单内的在线附加条目）===
# 官方 add-track/remove-track 对 DB 中不存在的 track guid 返回 100002，
# 在线曲目（客户端持有伪装 32-hex 假 id）直达官方必被拒。与收藏三接口同款
# 分工：在线条目记本地 JSON（按用户分文件，桶键=官方歌单 guid），官方条目
# 照旧透传官方；读取接口透传官方信封后合并下发。

_PLT_LOCK = asyncio.Lock()


def plt_dir() -> str:
    return CONF.get("plt_dir") or os.path.join(_HOME, "playlist_tracks")


def plt_dir_has_data() -> bool:
    """目录里存在任一用户文件才启用读接口拦截路径，否则纯透传（零开销零风险）。"""
    try:
        return any(name.endswith(".json") for name in os.listdir(plt_dir()))
    except OSError:
        return False


def user_plt_path(user_guid: str) -> str:
    return os.path.join(plt_dir(), f"{sanitize_user_guid(user_guid)}.json")


def load_playlist_tracks(user_guid: str) -> dict[str, list[dict]]:
    """返回 {歌单guid: [{"guid","addedAt","track"}, ...]}。"""
    path = user_plt_path(user_guid)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            return {str(k): v for k, v in data["items"].items() if isinstance(v, list)}
    except Exception as e:
        logger.warning("Failed to load playlist tracks for %s from %s: %s", user_guid, path, e)
    return {}


def save_playlist_tracks(user_guid: str, items: dict[str, list[dict]]) -> bool:
    path = user_plt_path(user_guid)
    parent = os.path.dirname(path) or "."
    part_path = f"{path}.{uuid4().hex[:8]}.part"
    try:
        os.makedirs(parent, exist_ok=True)
        with open(part_path, "w", encoding="utf-8") as f:
            json.dump({"items": items}, f, ensure_ascii=False, indent=2)
        os.replace(part_path, path)
        return True
    except Exception as e:
        logger.warning("Failed to save playlist tracks for %s to %s: %s", user_guid, path, e)
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except Exception:
                pass
        return False


def playlist_track_counts(user_guid: str) -> dict[str, int]:
    return {k: len(v) for k, v in load_playlist_tracks(user_guid).items()}


async def _parse_playlist_track_body(request: Request) -> tuple[str, list[str]]:
    """解析 add/remove-track 的 {"guid": 歌单, "trackGUIDs": [...]}（官方契约，批量）。"""
    try:
        body = json.loads((await request.body()).decode("utf-8") or "{}")
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return "", []
    playlist_guid = str(body.get("guid") or "").strip()
    raw = body.get("trackGUIDs")
    track_guids = [str(g or "").strip() for g in raw if str(g or "").strip()] if isinstance(raw, list) else []
    return playlist_guid, track_guids


async def _forward_official_playlist_tracks(
    request: Request, client: httpx.AsyncClient, action: str,
    playlist_guid: str, official: list[str],
) -> Response | None:
    """混合批次中官方部分先行。返回 None=官方成功，否则返回官方错误响应（本地不动）。"""
    headers = copy_incoming_headers(request)
    req = client.build_request(
        "POST",
        f"/music/api/v1/playlist/{action}",
        headers=headers,
        json={"guid": playlist_guid, "trackGUIDs": official},
    )
    resp = await client.send(req)
    resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})
    if resp.status_code != 200:
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    try:
        payload = resp.json()
    except Exception:
        payload = None
    if not (isinstance(payload, dict) and payload.get("code") == 0):
        return JSONResponse(content=payload or {"code": -1, "msg": "upstream error", "data": None})
    return None


@app.post("/music/api/v1/playlist/add-track")
async def playlist_add_track(request: Request):
    """往歌单加曲目：在线条目记本地存储（幂等，重复加刷新 addedAt 对齐官方语义）。"""
    upstream_client = get_upstream_client(request.app)
    playlist_guid, track_guids = await _parse_playlist_track_body(request)
    if not playlist_guid:
        return await forward_to_upstream(request, upstream_client)

    playlist_guid = resolve_real_guid(playlist_guid)
    if dailyrec.is_recommend_playlist_guid(playlist_guid):
        # 推荐歌单是按天重建的虚拟歌单，保持只读：吸收请求，避免伪装 id 直达官方被拒
        return JSONResponse(content={"code": 0, "msg": "", "data": None})
    if nmpl.is_nm_playlist_guid(playlist_guid):
        # 网易账号歌单只读（不回写网易）：同样吸收请求
        return JSONResponse(content={"code": 0, "msg": "", "data": None})
    lx_pl_id = lxsync.lxsync_playlist_id_from_guid(resolve_real_guid(playlist_guid))
    if lx_pl_id:
        if not lxsync.writeback_tracks_enabled():
            # 只读档（默认）：吸收请求。要改请去洛雪客户端改，下次同步自动生效
            return JSONResponse(content={"code": 0, "msg": "", "data": None})
        items = []
        for raw in track_guids:
            guid = resolve_real_guid(str(raw or "").strip())
            if not guid.startswith("online:lx:"):
                continue          # 只回写 lx 音源的曲目：别的来源洛雪那边认不出来
            info = await _best_effort_online_info(request, guid) or {}
            items.append({"id": guid, "title": info.get("title"), "artist": info.get("artist"),
                          "album": info.get("album"), "duration_s": info.get("duration_s"),
                          "cover_url": info.get("cover_url")})
        try:
            count = await lxsync.add_tracks(lx_pl_id, items)
        except Exception as exc:  # noqa: BLE001 - 写失败必须让客户端看得见，不能假成功
            logger.warning("lx writeback add failed (%s): %s: %s", lx_pl_id, type(exc).__name__, exc)
            return JSONResponse(content={"code": 502, "msg": f"写回洛雪歌单失败：{exc}"[:200], "data": None},
                                status_code=502)
        logger.info("lx writeback: added %d track(s) to %s", count, lx_pl_id)
        return JSONResponse(content={"code": 0, "msg": "", "data": None})

    online = [g for g in (resolve_real_guid(g) for g in track_guids) if is_online_guid(g)]
    if not online:
        # 全官方（或空列表）：原样透传，官方语义兜底（含参数校验错误回传）
        return await forward_to_upstream(request, upstream_client)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    official = [g for g in (resolve_real_guid(g) for g in track_guids) if not is_online_guid(g)]
    if official:
        err = await _forward_official_playlist_tracks(request, upstream_client, "add-track", playlist_guid, official)
        if err is not None:
            return err

    now = int(time.time())
    snapshots = {}
    for g in online:
        snapshots[g] = build_favorite_track_obj(g, await _best_effort_online_info(request, g), created_at=now)

    saved = False
    autobind = bool(CONF.get("fav_auto_bind"))
    async with _PLT_LOCK:
        try:
            items = load_playlist_tracks(user_guid)
            bucket = items.setdefault(playlist_guid, [])
            for g in online:
                idx = next((i for i, it in enumerate(bucket) if it.get("guid") == g), None)
                if idx is not None:
                    bucket[idx]["addedAt"] = now
                    bucket[idx]["track"] = snapshots[g]
                else:
                    entry = {"guid": g, "addedAt": now, "track": snapshots[g]}
                    if autobind:
                        entry["bind"] = "pending"
                    bucket.append(entry)
            save_playlist_tracks(user_guid, items)
            saved = True
        except Exception as e:
            logger.warning("Error updating playlist tracks for user %s: %s", user_guid, e)

    if saved:
        for g in online:
            _register_fav_autobind(request, g, user_guid, playlist_guid=playlist_guid)

    return JSONResponse(content={"code": 0, "msg": "", "data": None})


@app.post("/music/api/v1/playlist/remove-track")
async def playlist_remove_track(request: Request):
    """从歌单移除曲目：在线条目删本地存储（幂等），官方条目照旧透传。"""
    upstream_client = get_upstream_client(request.app)
    playlist_guid, track_guids = await _parse_playlist_track_body(request)
    if not playlist_guid:
        return await forward_to_upstream(request, upstream_client)

    playlist_guid = resolve_real_guid(playlist_guid)
    if dailyrec.is_recommend_playlist_guid(playlist_guid):
        return JSONResponse(content={"code": 0, "msg": "", "data": None})
    if nmpl.is_nm_playlist_guid(playlist_guid):
        return JSONResponse(content={"code": 0, "msg": "", "data": None})
    lx_pl_id = lxsync.lxsync_playlist_id_from_guid(resolve_real_guid(playlist_guid))
    if lx_pl_id:
        if not lxsync.writeback_tracks_enabled():
            return JSONResponse(content={"code": 0, "msg": "", "data": None})
        guids = [resolve_real_guid(str(raw or "").strip()) for raw in track_guids]
        try:
            count = await lxsync.remove_tracks(lx_pl_id, guids)
        except Exception as exc:  # noqa: BLE001
            logger.warning("lx writeback remove failed (%s): %s: %s", lx_pl_id, type(exc).__name__, exc)
            return JSONResponse(content={"code": 502, "msg": f"从洛雪歌单移除失败：{exc}"[:200], "data": None},
                                status_code=502)
        logger.info("lx writeback: removed %d track(s) from %s", count, lx_pl_id)
        return JSONResponse(content={"code": 0, "msg": "", "data": None})

    online = [g for g in (resolve_real_guid(g) for g in track_guids) if is_online_guid(g)]
    if not online:
        return await forward_to_upstream(request, upstream_client)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    official = [g for g in (resolve_real_guid(g) for g in track_guids) if not is_online_guid(g)]
    # 旧会话假 id 中已完成官方绑定的：翻译成官方 guid 一并从官方歌单移除
    bound_pairs: "list[tuple[str, str]]" = []
    for g in online:
        entry = lookup_bind_entry(user_guid, g)
        if entry and playlist_guid in (entry.get("playlists") or []):
            bound_pairs.append((g, str(entry.get("official") or "")))
    official += [og for _, og in bound_pairs if og]
    if official:
        err = await _forward_official_playlist_tracks(request, upstream_client, "remove-track", playlist_guid, official)
        if err is not None:
            return err
        for g, _og in bound_pairs:
            _forget_bind(user_guid, g, playlist_guid=playlist_guid)

    online_set = set(online)
    async with _PLT_LOCK:
        try:
            items = load_playlist_tracks(user_guid)
            bucket = items.get(playlist_guid)
            if bucket:
                kept = [it for it in bucket if it.get("guid") not in online_set]
                if kept:
                    items[playlist_guid] = kept
                else:
                    items.pop(playlist_guid, None)
                save_playlist_tracks(user_guid, items)
        except Exception as e:
            logger.warning("Error removing playlist tracks for user %s: %s", user_guid, e)

    return JSONResponse(content={"code": 0, "msg": "", "data": None})


@app.post("/music/api/v1/playlist/delete")
async def playlist_delete(request: Request):
    """删除歌单：透传官方；成功后清掉本地为该歌单存的在线附加条目（防孤儿数据）。"""
    upstream_client = get_upstream_client(request.app)
    try:
        _body = json.loads((await request.body()).decode("utf-8") or "{}")
    except Exception:
        _body = {}
    _pl_guid = str(_body.get("guid") or "").strip() if isinstance(_body, dict) else ""
    if nmpl.is_nm_playlist_guid(resolve_real_guid(_pl_guid)):
        # 网易账号歌单只读：吸收删除（不透传官方必被拒的假 id），下次刷新卡片仍在
        return JSONResponse(content={"code": 0, "msg": "ok", "data": None})
    lx_pl_id = lxsync.lxsync_playlist_id_from_guid(resolve_real_guid(_pl_guid))
    if lx_pl_id:
        if not lxsync.writeback_all_enabled():
            # 默认档：洛雪同步歌单在本地删不掉（服务端是权威），下次同步仍在
            return JSONResponse(content={"code": 0, "msg": "ok", "data": None})
        try:
            await lxsync.remove_playlist(lx_pl_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("lx writeback delete failed (%s): %s: %s", lx_pl_id, type(exc).__name__, exc)
            return JSONResponse(content={"code": 502, "msg": f"删除洛雪歌单失败：{exc}"[:200], "data": None},
                                status_code=502)
        logger.info("lx writeback: removed playlist %s", lx_pl_id)
        return JSONResponse(content={"code": 0, "msg": "ok", "data": None})
    envelope = await fetch_upstream_envelope(request, upstream_client)
    if isinstance(envelope, Response):
        return envelope
    headers = envelope.pop("_ext_headers", {})

    if envelope.get("code") == 0:
        try:
            body = json.loads((await request.body()).decode("utf-8") or "{}")
        except Exception:
            body = {}
        playlist_guid = str(body.get("guid") or "").strip() if isinstance(body, dict) else ""
        resolved = resolve_real_guid(playlist_guid)
        if playlist_guid and not dailyrec.is_recommend_playlist_guid(resolved) and plt_dir_has_data():
            is_authed, user_guid, _ = await _probe_upstream_auth(request, upstream_client)
            if is_authed:
                async with _PLT_LOCK:
                    try:
                        items = load_playlist_tracks(user_guid)
                        if items.pop(resolved, None) is not None or items.pop(playlist_guid, None) is not None:
                            save_playlist_tracks(user_guid, items)
                    except Exception as e:
                        logger.warning("Error purging playlist tracks for user %s: %s", user_guid, e)
                # 官方已写入该歌单的绑定登记同步清理，防孤儿转发
                _forget_bind_playlist(user_guid, resolved)

    return JSONResponse(content=envelope, headers=headers)


# ---------------------------------------------------------------------------
# W6（移植 G v2.9.30）：网易云「频道歌单」——榜/分类/我的/新碟/电台
# 清单走 SWR 内存缓存（stale-while-revalidate），曲目走磁盘缓存 + 后台刷新，
# 全部由 playlists.py 提供持久化与解析，app.py 只做接线与分发。
# ---------------------------------------------------------------------------
_CHANNEL_LIST_CACHE_TTL = _env_float("FNMUSIC_CHANNEL_LIST_CACHE_TTL", 300.0)
_channel_recs_cache: dict[tuple, dict] = {}
_channel_recs_refresh: dict[tuple, "asyncio.Task"] = {}


async def _netease_logged_in() -> bool:
    """网易登录态探测：失败按未登录处理（不抛出，避免拖垮歌单列表）。"""
    try:
        state = await netease_auth.fetch_state(get_musicbox_client(app))
        return bool(state.logged_in)
    except Exception as exc:  # noqa: BLE001 - 探测失败只降级为未登录
        logger.warning("login state probe failed: %s: %s", type(exc).__name__, exc)
        return False


def _channel_recs_cache_key() -> tuple:
    return (playlists.channels_enabled(), playlists.category_name(), playlists.channel_limit())


async def _collect_channel_records(client: httpx.AsyncClient, key: tuple):
    logged_in = await _netease_logged_in()
    recs, keep, complete = await playlists.collect_records(client, logged_in)
    # 记录采集时的事件循环：条目只对同一循环有效。生产只有一个常驻循环（等价于
    # G 的纯 TTL 缓存），而测试/进程重启会新建循环——不隔离就会把上一个循环里
    # 旧客户端采集的清单当成命中，跨用例串数据。
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    _channel_recs_cache[key] = {
        "ts": time.time(), "records": recs, "keep": keep, "complete": complete, "loop": loop,
    }
    return recs, keep, complete


def _schedule_channel_recs_refresh(client: httpx.AsyncClient, key: tuple) -> None:
    existing = _channel_recs_refresh.get(key)
    if existing is not None and not existing.done():
        return

    async def _job() -> None:
        try:
            await _collect_channel_records(client, key)
        except Exception as exc:  # noqa: BLE001 - 后台刷新失败只记日志
            logger.warning("口径清单后台刷新失败: %s: %s", type(exc).__name__, exc)
        finally:
            _channel_recs_refresh.pop(key, None)

    _channel_recs_refresh[key] = asyncio.create_task(_job())


async def _channel_playlist_records(client: httpx.AsyncClient):
    """返回 (records, keep, complete)；命中缓存立即返回，过期先返回旧值再后台刷新。"""
    if _CHANNEL_LIST_CACHE_TTL <= 0:
        logged_in = await _netease_logged_in()
        return await playlists.collect_records(client, logged_in)
    key = _channel_recs_cache_key()
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    hit = _channel_recs_cache.get(key)
    if hit is not None and hit.get("loop") is not loop:
        # 条目属于别的事件循环（测试用例/重启后的新循环）：当作未命中重新采集
        hit = None
    if hit is not None:
        if (time.time() - float(hit.get("ts") or 0.0)) < _CHANNEL_LIST_CACHE_TTL:
            return hit["records"], hit["keep"], hit["complete"]
        _schedule_channel_recs_refresh(client, key)
        return hit["records"], hit["keep"], hit["complete"]
    return await _collect_channel_records(client, key)


def _channel_public_fields(rec: dict) -> dict:
    now = int(time.time())
    return {
        "guid": rec.get("guid"),
        "name": rec.get("name") or "网易云歌单",
        "coverId": rec.get("guid"),
        "cover_url": rec.get("cover_url") or "",
        "coverUrl": rec.get("cover_url") or "",
        "createdAt": int(rec.get("createdAt") or now),
        "updatedAt": int(rec.get("updatedAt") or now),
        "trackCount": int(rec.get("track_count") or 0),
        "isDaily": False,
        "source": "netease",
        "channel": rec.get("channel") or "",
    }


async def _enrich_netease_items(client: httpx.AsyncClient, items: list[dict]) -> None:
    """批量补全歌曲详情（封面等）。失败只记日志，不影响已映射的曲目。"""
    ids = [str(it["id"]).split(":", 1)[-1] for it in items if it.get("id")]
    if not ids:
        return
    try:
        resp = await client.get("/api/v1/songs/detail", params={"ids": ",".join(ids[:100])}, timeout=15.0)
    except Exception as exc:  # noqa: BLE001 - 详情补全失败不阻断
        logger.warning("Failed to fetch songs detail: %s: %s", type(exc).__name__, exc)
        return
    if resp.status_code != 200:
        return
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return
    if not isinstance(payload, dict) or payload.get("ok") is False:
        return
    detail_list = payload.get("data")
    if not isinstance(detail_list, list):
        return
    detail_map: dict[str, dict] = {}
    for detail in detail_list:
        if isinstance(detail, dict):
            key = str(detail.get("song_id") or detail.get("id") or "")
            if key:
                detail_map[key] = detail
    for item in items:
        detail = detail_map.get(str(item.get("id") or "").split(":", 1)[-1])
        if detail:
            netease_items.apply_song_detail(item, detail)


async def _fetch_channel_tracks(client: httpx.AsyncClient, guid: str) -> list[dict]:
    return await playlists.resolve_track_items(client, guid, netease_items.map_netease_song, _enrich_netease_items)


_TRACK_REFRESH_TASKS: dict[str, "asyncio.Task"] = {}


def _schedule_track_refresh(fastapi_app: FastAPI, guid: str) -> None:
    """单飞去重：同一歌单只允许一个后台刷新任务在飞。"""
    existing = _TRACK_REFRESH_TASKS.get(guid)
    if existing is not None and not existing.done():
        return

    async def _job() -> None:
        try:
            items = await _fetch_channel_tracks(get_musicbox_client(fastapi_app), guid)
            if items:
                playlists.store_cached_tracks(guid, items)
                logger.info("歌单缓存已后台刷新：%s（%d 首）", guid, len(items))
        except Exception as exc:  # noqa: BLE001 - 后台刷新失败保留旧缓存
            logger.warning("歌单缓存后台刷新失败 %s: %s: %s", guid, type(exc).__name__, exc)
        finally:
            _TRACK_REFRESH_TASKS.pop(guid, None)

    _TRACK_REFRESH_TASKS[guid] = asyncio.create_task(_job())


async def _channel_tracks_items(fastapi_app: FastAPI, guid: str) -> list[dict]:
    hit = playlists.load_cached_tracks(guid)
    if hit is not None:
        ts, items = hit
        if (time.time() - float(ts or 0.0)) > playlists.tracks_cache_ttl():
            _schedule_track_refresh(fastapi_app, guid)
        return items
    items = await _fetch_channel_tracks(get_musicbox_client(fastapi_app), guid)
    playlists.store_cached_tracks(guid, items)
    return items


async def _channel_tracks(request: Request, guid: str, limit: int = 0) -> list[dict]:
    items = await _channel_tracks_items(request.app, guid)
    if limit and limit > 0:
        items = items[:limit]
    return [build_online_track(it) for it in items]


async def _channel_playlist_cover(request: Request, guid: str) -> str:
    """频道歌单封面：注册表优先，缺失时取首曲封面并回写注册表。"""
    rec = playlists.lookup(guid)
    cover = str(rec.get("cover_url") or "")
    if cover:
        return cover
    try:
        tracks = await _channel_tracks(request, guid, limit=1)
    except Exception:  # noqa: BLE001 - 取封面失败不能 500
        tracks = []
    first = str((tracks[0] or {}).get("coverUrl") or "") if tracks else ""
    if first:
        playlists.remember({**rec, "guid": guid, "cover_url": first})
        playlists.save_registry()
    return first


_PLAYLIST_WARMING = False
_LAST_AUTO_WARM_AT = 0.0


def _playlist_warm_cooldown() -> float:
    return float(playlists.tracks_cache_ttl())


def _warm_skip_fresh_s() -> float:
    return max(0.0, _env_float("FNMUSIC_WARM_SKIP_FRESH_S", 3600.0))


async def _warm_playlist_caches(
    fastapi_app: FastAPI, guids: list[str] | None = None, skip_fresh_s: float = 0.0
) -> dict:
    """预热频道歌单曲目缓存；单飞（_PLAYLIST_WARMING）避免按钮连点打爆上游。"""
    global _PLAYLIST_WARMING
    if _PLAYLIST_WARMING:
        return {"started": False, "reason": "already_running"}
    _PLAYLIST_WARMING = True
    refreshed = 0
    skipped_fresh = 0
    try:
        client = get_musicbox_client(fastapi_app)
        target = list(guids) if guids is not None else []
        if guids is None:
            try:
                recs, keep, complete = await _collect_channel_records(client, _channel_recs_cache_key())
                if complete and keep:
                    playlists.forget_stale(keep)
                target = [str(r.get("guid") or "") for r in recs if r.get("guid")]
            except Exception as exc:  # noqa: BLE001 - 清单拿不到就不预热
                logger.warning("歌单缓存预热：拉取清单失败 %s: %s", type(exc).__name__, exc)
                target = []
        for guid in target:
            if not guid:
                continue
            if skip_fresh_s > 0:
                hit = playlists.load_cached_tracks(guid)
                if hit is not None and (time.time() - float(hit[0] or 0.0)) < skip_fresh_s:
                    skipped_fresh += 1
                    continue
            try:
                items = await _fetch_channel_tracks(client, guid)
            except Exception as exc:  # noqa: BLE001 - 单个歌单失败继续下一个
                logger.warning("歌单缓存预热失败 %s: %s: %s", guid, type(exc).__name__, exc)
                continue
            if items:
                playlists.store_cached_tracks(guid, items)
                refreshed += 1
            await asyncio.sleep(1.0)
        logger.info("歌单缓存预热完成：刷新 %d、跳过（仍新鲜）%d、共 %d 个", refreshed, skipped_fresh, len(target))
        return {"started": True, "refreshed": refreshed, "skipped_fresh": skipped_fresh, "total": len(target)}
    finally:
        _PLAYLIST_WARMING = False


def _schedule_playlist_warm(
    fastapi_app: FastAPI, *, force: bool = False, guids: list[str] | None = None, skip_fresh_s: float = 0.0
) -> bool:
    global _LAST_AUTO_WARM_AT
    if _PLAYLIST_WARMING:
        return False
    if not force:
        if (time.time() - _LAST_AUTO_WARM_AT) < _playlist_warm_cooldown():
            return False
        _LAST_AUTO_WARM_AT = time.time()
    try:
        asyncio.create_task(_warm_playlist_caches(fastapi_app, guids=guids, skip_fresh_s=skip_fresh_s))
    except RuntimeError:  # 没有运行中的事件循环（同步上下文）时静默跳过
        return False
    return True


async def _playlist_refresh_loop(fastapi_app: FastAPI, stop_event: asyncio.Event) -> None:
    """每日定时（FNMUSIC_PLAYLIST_REFRESH_AT，默认 04:30）刷新频道歌单缓存。"""
    while not stop_event.is_set():
        delay = playlists.seconds_until_daily_refresh()
        if delay is None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=3600.0)
            except asyncio.TimeoutError:
                continue
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        else:
            return
        logger.info("定时刷新歌单缓存开始（%s）", playlists.refresh_time_of_day())
        _schedule_playlist_warm(fastapi_app, force=True, skip_fresh_s=_warm_skip_fresh_s())
        await asyncio.sleep(5.0)


def _playlist_public_fields(record: dict, tracks: list | None = None) -> dict:
    # 封面取曲在下发时重算（兼容当天旧缓存），并伪装成官方 track_+32hex 形态：
    # 官方 App 按 id 格式过滤，online: 原样下发的 coverId 不会被渲染成图标。
    # 本地曲目的 coverId 是真实官方封面 guid：原样下发（封面端点透传官方）。
    cover = ""
    picked = dailyrec.pick_playlist_cover_track(tracks)
    if picked:
        cover = str(picked.get("coverId") or picked.get("guid") or "")
    if not cover:
        cover = str(record.get("coverId") or record.get("guid") or "")
    if cover.startswith("online:"):
        cover = "track_" + fake_official_guid(cover)
    return {
        "guid": record.get("guid"),
        "name": record.get("name") or "每日推荐",
        "coverId": cover,
        "createdAt": int(record.get("createdAt") or time.time()),
        "updatedAt": int(record.get("updatedAt") or time.time()),
        "trackCount": int(record.get("trackCount") or 0),
        "isDaily": True,
    }


@app.get("/music/api/v1/playlist/list")
@app.get("/music/api/v1/playlist/list/{subpath:path}")
async def playlist_list(request: Request):
    upstream_client = get_upstream_client(request.app)
    envelope = await fetch_upstream_envelope(request, upstream_client)
    if isinstance(envelope, Response):
        return envelope
    headers = envelope.pop("_ext_headers", {})
    if envelope.get("code") != 0:
        return JSONResponse(content=envelope, headers=headers)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed:
        return auth_resp or JSONResponse(content=envelope, headers=headers)

    kinds = _recommend_injectable_kinds(user_guid)
    # 网易账号歌单：实例级内容（同热门推荐），shared 会话同样可见
    nm_on = bool(CONF.get("netease_my_playlists")) and bool(CONF.get("netease_enabled"))
    # W6 频道歌单（榜/分类/我的/新碟/电台）：SWR 内存缓存，上游不可用时返回空表
    channel_recs: list[dict] = []
    keep: set[str] = set()
    complete = False
    try:
        channel_recs, keep, complete = await _channel_playlist_records(get_musicbox_client(request.app))
        if complete:
            playlists.forget_stale(keep)
    except Exception as e:  # noqa: BLE001 - 频道清单失败不能拖垮官方歌单列表
        logger.warning("channel playlist inject failed: %s: %s", type(e).__name__, e)
        channel_recs = []
    if not kinds and not nm_on and not channel_recs and not lxsync.sync_enabled():
        # 两个推荐开关全关（或 shared 会话无任何可注入类型）、账号歌单与频道清单都为空：不注入
        return JSONResponse(content=envelope, headers=headers)

    data = envelope.get("data")
    if not isinstance(data, dict):
        data = {"list": [], "total": 0}
        envelope["data"] = data
    official = data.get("list")
    if not isinstance(official, list):
        official = []
        data["list"] = official
    # W6：推荐歌单 + 频道歌单统一按 channel_order 排序、按显式顺序重排、
    # 最后写入展示用递增时间戳（客户端按 updatedAt 排序渲染）。
    stamped_order = playlists.channel_order()
    head_items: list[tuple[str, dict]] = []
    for kind in kinds:
        try:
            bundle = await _peek_daily_bundle(request, user_guid, kind)
        except Exception as e:
            logger.warning("%s recommend list inject failed: %s", kind, e)
            continue
        tracks = bundle.get("tracks") or []
        if kind == "hot" and not tracks:
            # 热门歌单构建失败/无可用榜单时不挂空壳（每日推荐保留占位等待后台生成）
            continue
        rec = _playlist_public_fields(bundle.get("playlist") or {}, tracks)
        rec["trackCount"] = len(tracks)
        head_items.append((kind, rec))
    for ch_rec in channel_recs:
        ch = str(ch_rec.get("channel") or "")
        head_items.append((ch if ch in stamped_order else "category", _channel_public_fields(ch_rec)))
    head_items.sort(
        key=lambda pair: stamped_order.index(pair[0]) if pair[0] in stamped_order else len(stamped_order)
    )
    head = playlists.apply_explicit_order([it for _tok, it in head_items])
    head = playlists.stamp_display_order(head)
    nm_cards: list[dict] = []
    if nm_on:
        try:
            nm_cards = await nmpl.peek_summaries(get_musicbox_client(request.app))
            for card in nm_cards:
                g = str(card.get("guid") or "")
                if is_online_guid(g):
                    # 登记 guid/封面假 id 反查（coverId 与 guid 同源 md5）
                    fake_official_guid(g)
        except Exception as e:
            logger.warning("netease my-playlists inject failed: %s", e)
            nm_cards = []
        if nm_cards:
            nmpl.schedule_prefetch(get_musicbox_client(request.app), build_online_track, nm_cards)
    # 洛雪音乐同步服务器歌单：只读虚拟歌单（SWR，服务端连不上时保留上一次的卡片）
    lx_cards: list[dict] = []
    if lxsync.sync_enabled():
        try:
            lx_cards = await lxsync.peek_summaries()
            for card in lx_cards:
                g = str(card.get("guid") or "")
                if is_online_guid(g):
                    # 封面假 id 与 guid 同源：客户端带的是 coverId，需要能反解回歌单
                    card["coverId"] = "track_" + fake_official_guid(g)
        except Exception as e:  # noqa: BLE001 - 同步服务故障不能拖垮官方歌单列表
            logger.warning("lx sync playlist inject failed: %s: %s", type(e).__name__, e)
            lx_cards = []
        if lx_cards:
            lxsync.schedule_prefetch()
    official = [
        it for it in official
        if not (isinstance(it, dict) and (
            dailyrec.is_recommend_playlist_guid(str(it.get("guid") or ""))
            or playlists.is_channel_guid(str(it.get("guid") or ""))
            or lxsync.is_lxsync_playlist_guid(str(it.get("guid") or ""))
        ))
    ]
    data["list"] = head + nm_cards + lx_cards + official
    total = data.get("total")
    data["total"] = ((total if isinstance(total, int) else len(official))
                     + len(head) + len(nm_cards) + len(lx_cards))
    if head:
        # 频道清单在列表页顺带预热（冷却期由 _schedule_playlist_warm 控制）
        _schedule_playlist_warm(
            request.app,
            skip_fresh_s=_warm_skip_fresh_s(),
            guids=[str(r.get("guid") or "") for r in channel_recs if r.get("guid")],
        )
    return JSONResponse(content=envelope, headers=headers)


@app.get("/music/api/v1/playlist/detail")
async def playlist_detail(request: Request):
    guid = str(request.query_params.get("guid") or "").strip()
    nm_pl_id = nmpl.nm_playlist_id_from_guid(guid) or nmpl.nm_playlist_id_from_guid(resolve_real_guid(guid))
    if nm_pl_id:
        # 网易账号歌单：只读虚拟歌单，卡片从摘要（内存→磁盘）取；trackCount 用
        # 已缓存曲目数更准（可播过滤后可能少于网易侧计数）
        card = nmpl.card_for(nm_pl_id)
        if card is None:
            return JSONResponse(content={"code": -1, "msg": "playlist not found", "data": None})
        tracks = nmpl.cached_tracks(nm_pl_id)
        if tracks is not None:
            card = {**card, "trackCount": len(tracks)}
        return JSONResponse(content={"code": 0, "msg": "ok", "data": card})
    lx_pid = (lxsync.lxsync_playlist_id_from_guid(guid)
              or lxsync.lxsync_playlist_id_from_guid(resolve_real_guid(guid)))
    if lx_pid:
        # 洛雪同步歌单：只读虚拟歌单，卡片取自内存缓存（trackCount 已在同步时按可播数算好）
        card = lxsync.card_for(lx_pid)
        if card is None:
            return JSONResponse(content={"code": -1, "msg": "playlist not found", "data": None})
        card["coverId"] = "track_" + fake_official_guid(guid)
        return JSONResponse(content={"code": 0, "msg": "ok", "data": card})
    if playlists.is_channel_guid(guid):
        # W6 频道歌单：从注册表取卡片（createdAt/updatedAt 用注册表 ts，
        # 与列表页 stamp_display_order 写入的值一致，客户端按它排序）
        reg = playlists.lookup(guid)
        rec = {
            "guid": guid,
            "name": reg.get("name") or f"网易云歌单 {playlists._target_id(guid) or ''}".strip(),
            "cover_url": reg.get("cover_url") or "",
            "track_count": reg.get("track_count") or 0,
            "channel": reg.get("channel") or playlists.channel_of(guid),
            "createdAt": reg.get("ts") or int(time.time()),
            "updatedAt": reg.get("ts") or int(time.time()),
        }
        return JSONResponse(content={"code": 0, "msg": "ok", "data": _channel_public_fields(rec)})
    kind = dailyrec.online_playlist_kind(guid)
    if not kind:
        # 官方歌单：本地存在在线附加条目才拦截修正 trackCount，否则纯透传
        if not plt_dir_has_data():
            return await forward_to_upstream(request, get_upstream_client(request.app))
        upstream_client = get_upstream_client(request.app)
        envelope = await fetch_upstream_envelope(request, upstream_client)
        if isinstance(envelope, Response):
            return envelope
        headers = envelope.pop("_ext_headers", {})
        if envelope.get("code") != 0:
            return JSONResponse(content=envelope, headers=headers)
        # 官方明细已成功，此时探测被拒不能回传鉴权错误，降级为官方原样（同收藏列表）
        is_authed, user_guid, _ = await _probe_upstream_auth(request, upstream_client)
        if not is_authed:
            return JSONResponse(content=envelope, headers=headers)
        extras = load_playlist_tracks(user_guid).get(resolve_real_guid(guid)) or []
        if extras:
            data = envelope.get("data")
            if isinstance(data, dict):
                tc = data.get("trackCount")
                data["trackCount"] = (tc if isinstance(tc, int) else 0) + len(extras)
        return JSONResponse(content=envelope, headers=headers)

    upstream_client = get_upstream_client(request.app)
    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp
    bundle = await _load_daily_bundle(request, user_guid, kind)
    rec = _playlist_public_fields(bundle.get("playlist") or {}, bundle.get("tracks") or [])
    rec["trackCount"] = len(bundle.get("tracks") or [])
    # W6：列表页用 stamp_display_order 写的注册表 ts 必须覆盖详情页，
    # 否则客户端排序与列表页不一致（G v2.9.30 同款处理）
    reg_ts = playlists.lookup(guid).get("ts")
    if reg_ts:
        rec["createdAt"] = int(reg_ts)
        rec["updatedAt"] = int(reg_ts)
    return JSONResponse(content={"code": 0, "msg": "ok", "data": rec})


@app.get("/music/api/v1/playlist/batch-detail")
async def playlist_batch_detail(request: Request):
    raw = request.query_params.get("guids") or request.query_params.get("guid") or ""
    guids = [g.strip() for g in raw.split(",") if g.strip()]
    recommend_ids = [g for g in guids if dailyrec.is_recommend_playlist_guid(g)]
    channel_ids = [g for g in guids if playlists.is_channel_guid(g)]
    nm_ids = [
        pid for pid in (
            nmpl.nm_playlist_id_from_guid(g) or nmpl.nm_playlist_id_from_guid(resolve_real_guid(g))
            for g in guids
        ) if pid
    ]
    lx_ids = [
        pid for pid in (
            lxsync.lxsync_playlist_id_from_guid(g) or lxsync.lxsync_playlist_id_from_guid(resolve_real_guid(g))
            for g in guids
        ) if pid
    ]
    if not recommend_ids and not nm_ids and not channel_ids and not lx_ids and not plt_dir_has_data():
        return await forward_to_upstream(request, get_upstream_client(request.app))

    upstream_client = get_upstream_client(request.app)
    rest = [
        g for g in guids
        if not dailyrec.is_recommend_playlist_guid(g)
        and not nmpl.is_nm_playlist_guid(resolve_real_guid(g))
        and not playlists.is_channel_guid(g)
        and not lxsync.is_lxsync_playlist_guid(resolve_real_guid(g))
    ]
    official_list: list = []
    if rest:
        headers = copy_incoming_headers(request)
        req = upstream_client.build_request(
            "GET",
            f"/music/api/v1/playlist/batch-detail?guids={quote(','.join(rest), safe=',')}",
            headers=headers,
        )
        resp = await upstream_client.send(req)
        if resp.status_code == 200:
            try:
                payload = resp.json()
                if isinstance(payload, dict) and payload.get("code") == 0:
                    data = payload.get("data") or {}
                    if isinstance(data, dict) and isinstance(data.get("list"), list):
                        official_list = data["list"]
                    elif isinstance(data, list):
                        official_list = data
            except Exception:
                official_list = []

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp
    # 官方歌单 trackCount 并上本地在线附加条目
    if official_list:
        counts = playlist_track_counts(user_guid)
        for it in official_list:
            if isinstance(it, dict):
                extra = counts.get(str(it.get("guid") or ""))
                if extra:
                    tc = it.get("trackCount")
                    it["trackCount"] = (tc if isinstance(tc, int) else 0) + extra
    recs: list[dict] = []
    for g in recommend_ids:
        kind = dailyrec.online_playlist_kind(g) or "daily"
        bundle = await _load_daily_bundle(request, user_guid, kind)
        rec = _playlist_public_fields(bundle.get("playlist") or {}, bundle.get("tracks") or [])
        rec["trackCount"] = len(bundle.get("tracks") or [])
        reg_ts = playlists.lookup(g).get("ts")
        if reg_ts:
            rec["createdAt"] = int(reg_ts)
            rec["updatedAt"] = int(reg_ts)
        recs.append(rec)
    for g in channel_ids:
        # W6 频道歌单：注册表卡片（ts 与列表页一致）
        reg = playlists.lookup(g)
        recs.append(_channel_public_fields({
            "guid": g,
            "name": reg.get("name") or f"网易云歌单 {playlists._target_id(g) or ''}".strip(),
            "cover_url": reg.get("cover_url") or "",
            "track_count": reg.get("track_count") or 0,
            "channel": reg.get("channel") or playlists.channel_of(g),
            "createdAt": reg.get("ts"),
            "updatedAt": reg.get("ts"),
        }))
    for pid in nm_ids:
        card = nmpl.card_for(pid)
        if card is not None:
            tracks = nmpl.cached_tracks(pid)
            if tracks is not None:
                card = {**card, "trackCount": len(tracks)}
            recs.append(card)
    for pid in lx_ids:
        card = lxsync.card_for(pid)
        if card is not None:
            recs.append(card)
    return JSONResponse(content={"code": 0, "msg": "ok", "data": {"list": recs + official_list}})


@app.get("/music/api/v1/track/playlist-detail/list")
async def playlist_track_list(request: Request):
    guid = str(
        request.query_params.get("playlistGUID")
        or request.query_params.get("playlistGuid")
        or request.query_params.get("guid")
        or ""
    ).strip()
    kind = dailyrec.online_playlist_kind(guid)
    nm_pl_id = nmpl.nm_playlist_id_from_guid(guid) or nmpl.nm_playlist_id_from_guid(resolve_real_guid(guid))
    if nm_pl_id:
        # 网易账号歌单曲目：可播过滤后的 VO 分页下发（与推荐歌单同款分页语义）
        upstream_client = get_upstream_client(request.app)
        is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
        if not is_authed and auth_resp is not None:
            return auth_resp
        tracks = dailyrec.stamp_playlist_tracks(
            await nmpl.load_tracks(get_musicbox_client(request.app), nm_pl_id, build_online_track)
        )
        _remember_prefetch_context(request, guid, tracks)
        try:
            page = max(int(request.query_params.get("page") or 1), 1)
        except (TypeError, ValueError):
            page = 1
        try:
            size = int(request.query_params.get("size") or 50)
        except (TypeError, ValueError):
            size = 50
        if size == -1:
            size = max(len(tracks), 1)
        if size < 1:
            size = 50
        start = (page - 1) * size
        page_tracks = tracks[start:start + size]
        return JSONResponse(
            content=disguise_client_json({
                "code": 0,
                "msg": "ok",
                "data": {"list": page_tracks, "total": len(tracks), "sort": request.query_params.get("sort") or ""},
            })
        )
    lx_pid = (lxsync.lxsync_playlist_id_from_guid(guid)
              or lxsync.lxsync_playlist_id_from_guid(resolve_real_guid(guid)))
    if lx_pid:
        # 洛雪同步歌单曲目：整包数据在同步时已拿到（load_tracks 只做 VO 转换，不再打服务端）
        upstream_client = get_upstream_client(request.app)
        is_authed, _user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
        if not is_authed and auth_resp is not None:
            return auth_resp
        tracks = dailyrec.stamp_playlist_tracks(
            await lxsync.load_tracks(lx_pid, build_online_track)
        )
        _remember_prefetch_context(request, guid, tracks)
        try:
            page = max(int(request.query_params.get("page") or 1), 1)
        except (TypeError, ValueError):
            page = 1
        try:
            size = int(request.query_params.get("size") or 50)
        except (TypeError, ValueError):
            size = 50
        if size == -1:
            size = max(len(tracks), 1)
        if size < 1:
            size = 50
        start = (page - 1) * size
        page_tracks = tracks[start:start + size]
        return JSONResponse(
            content=disguise_client_json({
                "code": 0,
                "msg": "ok",
                "data": {"list": page_tracks, "total": len(tracks), "sort": request.query_params.get("sort") or ""},
            })
        )
    if playlists.is_channel_guid(guid):
        # W6 频道歌单曲目：磁盘缓存优先（过期则后台刷新），缺失时同步拉取
        upstream_client = get_upstream_client(request.app)
        is_authed, _user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
        if not is_authed and auth_resp is not None:
            return auth_resp
        started = time.monotonic()
        cache_state = "hit"
        try:
            if playlists.load_cached_tracks(guid) is None:
                cache_state = "miss"
            tracks = dailyrec.stamp_playlist_tracks(await _channel_tracks(request, guid))
        except Exception as e:  # noqa: BLE001 - 上游失败回 502，客户端可重试
            logger.warning("channel playlist tracks failed for %s: %s: %s", guid, type(e).__name__, e)
            return JSONResponse(
                content={"code": 502, "msg": "netease playlist unavailable", "data": None},
                status_code=502,
            )
        _remember_prefetch_context(request, guid, tracks)
        _slow_ms = (time.monotonic() - started) * 1000.0
        if _slow_ms > 1000:
            logger.info("playlist tracks slow open: %.0fms cache=%s %s", _slow_ms, cache_state, guid)
        try:
            page = max(int(request.query_params.get("page") or 1), 1)
        except (TypeError, ValueError):
            page = 1
        try:
            size = int(request.query_params.get("size") or 50)
        except (TypeError, ValueError):
            size = 50
        if size == -1:
            size = max(len(tracks), 1)
        if size < 1:
            size = 50
        start = (page - 1) * size
        page_tracks = tracks[start:start + size]
        return JSONResponse(
            content=disguise_client_json({
                "code": 0,
                "msg": "ok",
                "data": {"list": page_tracks, "total": len(tracks), "sort": request.query_params.get("sort") or ""},
            })
        )
    if not kind:
        # 官方歌单：本地存在在线附加条目才拦截合并，否则纯透传
        if not plt_dir_has_data():
            return await forward_to_upstream(request, get_upstream_client(request.app))
        upstream_client = get_upstream_client(request.app)
        envelope = await fetch_upstream_envelope(request, upstream_client)
        if isinstance(envelope, Response):
            return envelope
        headers = envelope.pop("_ext_headers", {})
        if envelope.get("code") != 0:
            return JSONResponse(content=envelope, headers=headers)
        # 官方列表已成功，此时探测被拒不能回传鉴权错误，降级为官方原样（同收藏列表）
        is_authed, user_guid, _ = await _probe_upstream_auth(request, upstream_client)
        if not is_authed:
            return JSONResponse(content=envelope, headers=headers)
        resolved_pl_guid = resolve_real_guid(guid)
        extras = load_playlist_tracks(user_guid).get(resolved_pl_guid) or []
        if not extras:
            return JSONResponse(content=envelope, headers=headers)
        # 自愈：bind=pending 的歌单条目在此补发官方绑定/下载
        _maybe_retry_official_binds(request, user_guid, plt_map={resolved_pl_guid: extras})

        data = envelope.get("data")
        if not isinstance(data, dict):
            data = {"list": [], "total": 0}
            envelope["data"] = data
        official_list = data.get("list") if isinstance(data.get("list"), list) else []
        official_total = data.get("total") if isinstance(data.get("total"), int) else len(official_list)

        # 在线附加条目接在官方条目之后（addedAt 倒序），逻辑区间
        # [official_total, official_total+len)；按请求页窗口切片，翻页不重不漏
        extras_sorted = sorted(extras, key=lambda x: x.get("addedAt", 0), reverse=True)
        fav_guids = {str(it.get("guid") or "") for it in load_online_favorites(user_guid)}
        template = official_track_template(official_list)
        online_objs = []
        for it in extras_sorted:
            g = str(it.get("guid") or "")
            if not g:
                continue
            snapshot = it.get("track") if isinstance(it.get("track"), dict) else None
            obj = build_favorite_track_obj(
                g, _snapshot_to_info(snapshot) if snapshot else None,
                created_at=it.get("addedAt"), template=template,
            )
            obj["isFavorite"] = g in fav_guids
            online_objs.append(obj)

        try:
            page = max(int(request.query_params.get("page") or 1), 1)
        except (TypeError, ValueError):
            page = 1
        try:
            size = int(request.query_params.get("size") or 50)
        except (TypeError, ValueError):
            size = 50
        if size == -1:
            online_page = online_objs
        else:
            if size < 1:
                size = 50
            start = (page - 1) * size
            lo = max(0, start - official_total)
            hi = max(0, start + size - official_total)
            online_page = online_objs[lo:hi]

        data["list"] = official_list + online_page
        data["total"] = official_total + len(online_objs)
        # 官方歌单没有 G 侧对应分支，但客户端同样按「官方条目 + 在线附加」的
        # 顺序播放，这里按同一顺序登记上下文（官方 guid 不在线会被预热环节跳过）。
        _remember_prefetch_context(request, guid, official_list + online_objs)
        return JSONResponse(content=disguise_client_json(envelope), headers=headers)


    upstream_client = get_upstream_client(request.app)
    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp
    bundle = await _load_daily_bundle(request, user_guid, kind)
    tracks = dailyrec.stamp_playlist_tracks(list(bundle.get("tracks") or []))
    _remember_prefetch_context(request, guid, tracks)
    try:
        page = max(int(request.query_params.get("page") or 1), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        size = int(request.query_params.get("size") or 50)
    except (TypeError, ValueError):
        size = 50
    if size == -1:
        # -1 是"返回全部"的约定值，须在 <1 兜底之前归一，否则永远到不了全量分支
        size = max(len(tracks), 1)
    if size < 1:
        size = 50
    start = (page - 1) * size
    page_tracks = tracks[start:start + size]
    return JSONResponse(
        content=disguise_client_json({
            "code": 0,
            "msg": "ok",
            "data": {"list": page_tracks, "total": len(tracks), "sort": request.query_params.get("sort") or ""},
        })
    )


# === album detail（在线曲目伪装专辑详情，issue #22）===
# 曲目 VO 的 album.guid 形如 "online:<src>:<id>:album"，disguise 后客户端拿到
# 官方 32-hex 形态；点击专辑时客户端请求官方专辑详情端点，原样转发官方必返回
# "无此专辑"。此处拦截分源适配：netease 经 musicbox 调真实专辑接口；musicdl/lx
# 从保留的搜索缓存/收藏/历史聚合同专辑名曲目合成；无法合成返回业务错误，
# 绝不透传官方"无此专辑"。官方专辑（guid 非 fake）原样转发不受影响。
#
# 取证说明：仓库内（proxy/static、webui-service/static、docs、tests）未找到客户端
# 专辑跳调端点的直接证据；官方 API 命名风格为 /music/api/v1/<资源>/detail?guid=
# （playlist/detail 同款），故按 /music/api/v1/album 前缀 + 携带 guid 参数保守
# 拦截——反解不到已登记伪装专辑的请求一律原样转发官方，行为与官方直连一致。


def _artist_names_joined(row: dict) -> str:
    """原生 netease 行的 ar/artists 列表 → "A / B" 拼接。"""
    ar = row.get("ar") or row.get("artists") or []
    if not isinstance(ar, list):
        return ""
    return " / ".join(str(a.get("name") or "") for a in ar if isinstance(a, dict) and a.get("name"))


def _album_detail_payload(album_guid: str, album_name: str, artist: str, tracks: list[dict]) -> dict:
    """官方专辑详情 VO：字段结构对齐 track VO 的 artists/album 对象风格。"""
    ts = int(time.time())
    artist_name = str(artist or "").strip() or "未知艺术家"
    artists_list = [{
        "guid": f"{album_guid}:artist",
        "name": artist_name,
        "coverId": None,
        "createdAt": ts,
        "updatedAt": ts,
    }]
    # 封面取第一首曲目 guid（/static/cover 按曲目封面解析），无曲目回落专辑自身
    cover_id = str((tracks[0] or {}).get("guid") or album_guid) if tracks else album_guid
    album_obj = {
        "guid": album_guid,
        "name": str(album_name or "").strip() or "未知专辑",
        "artists": artists_list,
        "coverId": cover_id,
        "releaseDate": None,
        "barcode": None,
        "createdAt": ts,
        "updatedAt": ts,
        "trackCount": len(tracks),
        "tracks": dailyrec.stamp_playlist_tracks(tracks),
    }
    return {"code": 0, "msg": "ok", "data": album_obj}


async def _netease_album_detail_payload(request: Request, entry: dict) -> dict | None:
    """netease 专辑：曲目详情解析真实专辑 ID，经 musicbox /api/v1/album/{id} 拉曲目列表。

    entry 带 album_id（/search/album 在线专辑登记）时跳过 song/info 二跳直达。
    """
    if not CONF.get("netease_enabled"):
        return None
    musicbox_client = get_musicbox_client(request.app)
    track_guid = str(entry.get("track_guid") or "")
    album_id = str(entry.get("album_id") or "")
    album_name = str(entry.get("album") or "")
    cover_url = ""
    if not album_id:
        song_id = song_id_from_online_guid(track_guid).rsplit(":", 1)[-1]
        try:
            r = await musicbox_client.get(f"/api/v1/song/{song_id}/info", timeout=10.0)
            if r.status_code == 200:
                body = r.json()
                if isinstance(body, dict) and body.get("ok") is not False:
                    data = body.get("data")
                    if isinstance(data, dict):
                        al = data.get("al") if isinstance(data.get("al"), dict) else {}
                        album_id = str(al.get("id") or "")
                        album_name = album_name or str(al.get("name") or "")
                        cover_url = str(al.get("picUrl") or "")
        except Exception as e:
            logger.warning("musicbox song info for album detail failed (%s): %s", track_guid, e)
    if not album_id:
        return None

    rows: list = []
    try:
        r = await musicbox_client.get(f"/api/v1/album/{album_id}", timeout=25.0)
        if r.status_code == 200:
            body = r.json()
            if isinstance(body, dict):
                rows = body.get("data") if isinstance(body.get("data"), list) else []
            elif isinstance(body, list):
                rows = body
    except Exception as e:
        logger.warning("musicbox album detail failed for %s: %s", album_id, e)

    # 行归一化：兼容 dig_info 形状（song_name/artist/duration 秒）与原生形状（name/ar/dt 毫秒）
    items: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("song_id") or row.get("id") or "")
        if not sid:
            continue
        title = str(row.get("song_name") or row.get("name") or "")
        artist = str(row.get("artist") or "") or _artist_names_joined(row)
        try:
            dur_f = float(row.get("duration") or row.get("dt") or 0)
        except (TypeError, ValueError):
            dur_f = 0.0
        items.append({
            "id": f"netease:{sid}",
            "source": "netease",
            "title": title,
            "artist": artist,
            "album": album_name,
            "duration_s": dur_f / 1000.0 if dur_f > 10000 else dur_f,
            "ext": "flac" if (row.get("has_sq") or row.get("sq") or row.get("has_hr")) else "mp3",
            "cover_url": cover_url,
        })
    if not items:
        return None
    tracks = [build_online_track(it) for it in items]
    return _album_detail_payload(f"{track_guid}:album", album_name, str(tracks[0].get("artist") or ""), tracks)


def _user_album_snapshots(user_guid: str) -> list[dict]:
    """当前用户的收藏/播放历史/歌单附加曲目快照（仅本用户，绝不扫其他用户数据）。"""
    out: list[dict] = []
    for loader in (load_online_favorites, dailyrec.load_online_play_history):
        try:
            for it in loader(user_guid) or []:
                snapshot = it.get("track") if isinstance(it, dict) and isinstance(it.get("track"), dict) else None
                if snapshot:
                    out.append(snapshot)
        except Exception:
            continue
    try:
        for bucket in load_playlist_tracks(user_guid).values():
            for it in bucket:
                snapshot = it.get("track") if isinstance(it, dict) and isinstance(it.get("track"), dict) else None
                if snapshot:
                    out.append(snapshot)
    except Exception:
        pass
    return out


def _normalize_album_candidate(it: dict) -> dict:
    """聚合候选归一为 info 形状：VO 快照还原，搜索条目原样。"""
    if isinstance(it.get("artists"), list) or isinstance(it.get("audioSpec"), dict):
        return _snapshot_to_info(it)
    return dict(it)


async def _aggregate_album_detail_payload(request: Request, entry: dict) -> dict | None:
    """musicdl/lx 专辑：无真实专辑接口，从内存搜索缓存 + 当前用户收藏/历史聚合同名曲目合成。"""
    album_name = str(entry.get("album") or "").strip()
    src = str(entry.get("source") or "")
    track_guid = str(entry.get("track_guid") or "")
    if not album_name:
        # 无专辑名无法按专辑聚合（搜索结果常缺专辑字段），走业务错误
        return None

    anchor = entry.get("item") if isinstance(entry.get("item"), dict) else {}
    candidates: list[dict] = [anchor] if anchor else []
    # ① 内存搜索缓存：音源目录数据，与用户身份无关
    _clean_search_cache()
    for cache_entry in _SEARCH_CACHE.values():
        for it in cache_entry.get("items") or []:
            if isinstance(it, dict):
                candidates.append(it)
    # ② 当前用户的收藏/历史/歌单附加快照（探测失败则跳过磁盘源，只用缓存）
    upstream_client = get_upstream_client(request.app)
    try:
        is_authed, user_guid, _resp = await _probe_upstream_auth(request, upstream_client)
    except Exception:
        is_authed, user_guid = False, ""
    if is_authed and user_guid:
        candidates.extend(_user_album_snapshots(user_guid))

    infos: list[dict] = []
    seen_guids: set[str] = set()
    for it in candidates:
        if not isinstance(it, dict) or not it:
            continue
        try:
            g = online_guid_from_item(it)
        except Exception:
            continue
        if not g or not is_online_guid(g) or g in seen_guids:
            continue
        if source_from_online_guid(g) != src:
            continue
        info = _normalize_album_candidate(it)
        if str(info.get("album") or "").strip() != album_name:
            continue
        seen_guids.add(g)
        infos.append(info)
    if not infos:
        return None
    tracks = [build_online_track(info) for info in infos]
    return _album_detail_payload(f"{track_guid}:album", album_name, str(tracks[0].get("artist") or ""), tracks)


# 专辑详情合成缓存（fake guid → (过期时间, payload)）：客户端进专辑页会先调
# /album/detail 再调 /track/album-detail/list，避免同专辑合成两次；TTL 短，仅省重复请求
_ALBUM_PAYLOAD_CACHE: dict[str, tuple[float, dict]] = {}
_ALBUM_PAYLOAD_TTL_S = 120.0


async def _synthesize_album_payload(request: Request, entry: dict, raw_guid: str) -> "dict | None":
    """按源合成专辑详情 payload（带 120s TTL 缓存）；合成失败返回 None。"""
    now = time.monotonic()
    cached = _ALBUM_PAYLOAD_CACHE.get(raw_guid)
    if cached and cached[0] > now:
        return cached[1]
    src = str(entry.get("source") or "")
    payload = None
    try:
        if src == "netease":
            payload = await _netease_album_detail_payload(request, entry)
        else:
            payload = await _aggregate_album_detail_payload(request, entry)
    except Exception as e:
        logger.warning("album detail adapt failed for %s: %s", raw_guid, e)
    if payload is not None:
        if len(_ALBUM_PAYLOAD_CACHE) > 256:
            _ALBUM_PAYLOAD_CACHE.clear()
        _ALBUM_PAYLOAD_CACHE[raw_guid] = (now + _ALBUM_PAYLOAD_TTL_S, payload)
    return payload


def _album_list_envelope(request: Request, tracks: list[dict]) -> dict:
    """官方专辑曲目列表信封：与 /track/playlist-detail/list 同款 {list,total,sort}。"""
    try:
        page = max(int(request.query_params.get("page") or 1), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        size = int(request.query_params.get("size") or 50)
    except (TypeError, ValueError):
        size = 50
    if size == -1:
        size = max(len(tracks), 1)
    if size < 1:
        size = 50
    start = (page - 1) * size
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "list": tracks[start:start + size],
            "total": len(tracks),
            "sort": request.query_params.get("sort") or "",
        },
    }


@app.api_route("/music/api/v1/album", methods=["GET", "HEAD"])
@app.api_route("/music/api/v1/album/{subpath:path}", methods=["GET", "HEAD"])
async def album_detail(request: Request, subpath: str = ""):
    raw_guid = str(
        request.query_params.get("guid")
        or request.query_params.get("albumGUID")
        or request.query_params.get("albumGuid")
        or request.query_params.get("id")
        or request.query_params.get("albumId")
        or ""
    ).strip()
    entry = resolve_fake_album(raw_guid)
    if entry is None:
        # 官方专辑或未知 guid：原样转发官方，官方专辑请求不受影响
        return await forward_to_upstream(request, get_upstream_client(request.app))

    payload = await _synthesize_album_payload(request, entry, raw_guid)
    if payload is not None:
        return JSONResponse(content=disguise_client_json(payload))
    logger.info("album detail not synthesizable: source=%s album=%r guid=%s", entry.get("source"), entry.get("album"), raw_guid)
    return JSONResponse(content={"code": -1, "msg": "该音源暂不支持专辑详情", "data": None})


@app.api_route("/music/api/v1/track/album-detail/list", methods=["GET", "HEAD"])
async def track_album_detail_list(request: Request):
    """专辑页曲目列表（官方 2.5 版客户端在 /album/detail 之后调用）。

    官方专辑原样透传；在线伪装专辑复用专辑详情合成结果分页下发，
    合成失败返回业务错误空列表，绝不透传官方"无此专辑"。
    """
    raw_guid = str(
        request.query_params.get("albumGUID")
        or request.query_params.get("albumGuid")
        or request.query_params.get("guid")
        or request.query_params.get("id")
        or ""
    ).strip()
    entry = resolve_fake_album(raw_guid)
    if entry is None:
        return await forward_to_upstream(request, get_upstream_client(request.app))

    payload = await _synthesize_album_payload(request, entry, raw_guid)
    if payload is None:
        return JSONResponse(content={"code": -1, "msg": "该音源暂不支持专辑详情", "data": None})
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    tracks = list(data.get("tracks") or [])
    return JSONResponse(content=disguise_client_json(_album_list_envelope(request, tracks)))


# === /search/album 在线专辑合并（issue #22，官方 2.5 版客户端专辑搜索 Tab）===
# 官方端点搜的是本地曲库专辑；这里透传官方结果后合并在线专辑：
#   - netease：musicbox type=album 真实专辑搜索，登记真实 album_id 直达详情
#   - musicdl/lx：无专辑接口，按关键词搜曲目后按"专辑名+源"聚合合成，
#     代表曲目 guid 注册伪装专辑（详情走聚合适配）
# 官方专辑永远排在前面；同名专辑（与官方结果撞名）不重复注入。

_ALBUM_SEARCH_LIMIT = 10


def _album_list_obj(album_guid: str, album_name: str, artist: str, cover_ref: str, track_count: int) -> dict:
    """专辑列表/搜索结果 VO（与 _album_detail_payload 同字段风格，无 tracks）。"""
    ts = int(time.time())
    artist_name = str(artist or "").strip() or "未知艺术家"
    return {
        "guid": album_guid,
        "name": str(album_name or "").strip() or "未知专辑",
        "artists": [{
            "guid": f"{album_guid}:artist",
            "name": artist_name,
            "coverId": None,
            "createdAt": ts,
            "updatedAt": ts,
        }],
        "coverId": cover_ref or album_guid,
        "releaseDate": None,
        "barcode": None,
        "createdAt": ts,
        "updatedAt": ts,
        "trackCount": int(track_count or 0),
    }


def _normalize_netease_album_row(row: dict) -> "tuple[str, str, str, str] | None":
    """网易专辑搜索行（CLI 与 web 兜底两种形状）→ (album_id, 名称, 歌手, 封面)。"""
    album_id = str(row.get("album_id") or row.get("albumId") or row.get("id") or "")
    name = str(row.get("album_name") or row.get("name") or row.get("title") or "").strip()
    if not album_id or album_id in ("0", "None") or not name:
        return None
    ar = row.get("artists") or row.get("artist") or []
    if isinstance(ar, list):
        artist = " / ".join(str(a.get("name") or "") for a in ar if isinstance(a, dict) and a.get("name"))
    else:
        artist = str(ar or "")
    cover = str(row.get("pic_url") or row.get("picUrl") or row.get("coverImgUrl") or row.get("cover") or "")
    return album_id, name, artist, cover


async def _netease_album_search_rows(request: Request, keyword: str, limit: int) -> list[dict]:
    if not CONF.get("netease_enabled"):
        return []
    client = get_musicbox_client(request.app)
    try:
        r = await client.get(
            "/api/v1/search",
            params={"keyword": keyword, "type": "album", "limit": max(limit, 5)},
            timeout=8.0,
        )
    except Exception as e:
        logger.debug("netease album search failed: %s", e)
        return []
    if r.status_code != 200:
        return []
    body = r.json() if r.headers.get("content-type", "").startswith(("application/json", "text/json")) else {}
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        norm = _normalize_netease_album_row(row)
        if norm is None:
            continue
        album_id, name, artist, cover = norm
        if album_id in seen:
            continue
        seen.add(album_id)
        # 锚点 guid 仅供专辑伪装登记/反解，不对应任何真实曲目；
        # 形态对齐曲目锚点约定：专辑 guid = 锚点 + ":album"（register 内部拼）
        anchor = f"online:netease:album:{album_id}"
        register_fake_album(anchor, name, {"cover_url": cover} if cover else {}, album_id=album_id, persist=True)
        out.append(_album_list_obj(f"{anchor}:album", name, artist, f"{anchor}:album", 0))
        if len(out) >= limit:
            break
    return out


async def _aggregate_album_search_rows(request: Request, keyword: str, limit: int) -> list[dict]:
    """musicdl/lx：关键词搜曲目后按"专辑名+源"聚合合成专辑卡片。"""
    jobs: list = []
    if CONF.get("musicdl_enabled"):
        jobs.append(("musicdl", get_musicdl_client(request.app).get(
            "/search", params={"keyword": keyword, "limit": 20}, timeout=6.0,
        )))
    if CONF.get("lx_enabled"):
        params: dict = {"keyword": keyword, "limit": 20}
        lx_sources = CONF.get("lx_sources") or None
        if lx_sources:
            params["sources"] = ",".join(lx_sources)
        jobs.append(("lx", get_lx_client(request.app).get("/api/v1/search", params=params, timeout=6.0)))

    async def _run(coro):
        try:
            return await coro
        except Exception as e:
            logger.debug("album aggregate source search failed: %s", e)
            return None

    groups: "dict[tuple[str, str], list[dict]]" = {}
    for fut in asyncio.as_completed([_run(coro) for _name, coro in jobs]):
        r = await fut
        if r is None or r.status_code != 200:
            continue
        try:
            rows = r.json().get("items")
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for it in rows:
            if not isinstance(it, dict):
                continue
            # lx 行的 id 是裸平台 id（如 kg:xxx），补 online: 前缀统一 guid 形态
            raw_id = str(it.get("id") or "")
            if raw_id and not raw_id.startswith(("online:", "lx:")):
                it = dict(it, id=f"lx:{raw_id}" if str(it.get("source") or "") == "lx" else raw_id)
            try:
                guid = online_guid_from_item(it)
            except Exception:
                continue
            src = source_from_online_guid(guid)
            album_name = str(it.get("album") or "").strip()
            if not guid or not is_online_guid(guid) or not album_name:
                continue
            groups.setdefault((src, album_name), []).append(it)

    out: list[dict] = []
    # 每组取曲目数最多的前 N 个专辑；代表曲目注册伪装专辑（详情走聚合适配）
    for (src, album_name), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        rep = items[0]
        try:
            rep_guid = online_guid_from_item(rep)
        except Exception:
            continue
        register_fake_album(rep_guid, album_name, rep, persist=True)
        out.append(_album_list_obj(
            f"{rep_guid}:album", album_name,
            str(rep.get("artist") or ""), rep_guid, len(items),
        ))
        if len(out) >= limit:
            break
    return out


async def _online_album_search(request: Request, keyword: str, limit: int) -> list[dict]:
    """在线专辑搜索：netease 真实接口优先，musicdl/lx 聚合补充（并行）。"""
    netease_coro = _netease_album_search_rows(request, keyword, limit)
    agg_coro = _aggregate_album_search_rows(request, keyword, max(limit // 2, 3))
    results = await asyncio.gather(netease_coro, agg_coro, return_exceptions=True)
    out: list[dict] = []
    seen: set[str] = set()
    for part in results:
        if isinstance(part, Exception):
            logger.debug("online album search branch failed: %s", part)
            continue
        for obj in part or []:
            name_key = str(obj.get("name") or "").strip().lower()
            if name_key and name_key in seen:
                continue
            seen.add(name_key)
            out.append(obj)
    return out[:limit]


@app.api_route("/music/api/v1/search/album", methods=["GET", "HEAD"])
@app.api_route("/music/api/v1/search/album/{subpath:path}", methods=["GET", "HEAD"])
async def search_album(request: Request, subpath: str = ""):
    upstream_client = get_upstream_client(request.app)
    envelope = await fetch_upstream_envelope(request, upstream_client)
    if not isinstance(envelope, dict) or envelope.get("code") != 0:
        # 官方错误（含未登录 401 信封）原样透传，不吞官方语义
        return envelope if not isinstance(envelope, dict) else JSONResponse(content=envelope)

    keyword = str(request.query_params.get("q") or request.query_params.get("keyword") or request.query_params.get("wd") or "").strip()
    online: list[dict] = []
    if keyword:
        try:
            online = await asyncio.wait_for(
                _online_album_search(request, keyword, _ALBUM_SEARCH_LIMIT), timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.debug("online album search timed out for %r", keyword)
        except Exception as e:
            logger.warning("online album search failed: %s", e)

    if online:
        data = envelope.get("data")
        if not isinstance(data, dict):
            data = {}
            envelope["data"] = data
        lst = data.get("list") if isinstance(data.get("list"), list) else None
        if lst is None:
            data["list"] = lst = []
        official_names = {
            str(x.get("name") or "").strip().lower()
            for x in lst if isinstance(x, dict)
        }
        add = [o for o in online if str(o.get("name") or "").strip().lower() not in official_names]
        data["list"] = lst + add
        if isinstance(data.get("total"), int):
            data["total"] = data["total"] + len(add)
        elif "total" not in data:
            data["total"] = len(data["list"])
    return JSONResponse(content=disguise_client_json(envelope))


@app.post("/music/api/v1/event/report")
async def event_report(request: Request):
    upstream_client = get_upstream_client(request.app)
    raw = await request.body()
    try:
        body = json.loads(raw.decode("utf-8") or "{}") if raw else {}
    except Exception:
        body = {}
    events = body.get("events") if isinstance(body, dict) else None
    online_plays: list[tuple[str, dict]] = []
    other_events: list = []
    if isinstance(events, list):
        for ev in events:
            if not isinstance(ev, dict):
                continue
            et = str(ev.get("eventType") or ev.get("type") or "")
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            guid = resolve_real_guid(str(payload.get("trackGUID") or payload.get("guid") or ""))
            if et in ("track_play", "TrackPlay") and is_online_guid(guid):
                online_plays.append((guid, payload))
            else:
                other_events.append(ev)
    else:
        return await forward_to_upstream(request, upstream_client)

    if online_plays:
        is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
        if is_authed:
            async with _HISTORY_LOCK:
                for guid, payload in online_plays:
                    info = stub_online_info(guid)
                    title = str(info.get("title") or "").strip()
                    artist = str(info.get("artist") or "").strip()
                    album = ""
                    duration_s = None
                    if payload:
                        # 客户端上报若带元数据（App/Web 字段名可能不同）优先采信，
                        # 避免历史条目只有 guid、标题歌手为空。专辑可能是对象，先解开。
                        p_title, p_artist, p_album = _tag_fields({
                            "title": payload.get("title"),
                            "name": payload.get("name"),
                            "artist": payload.get("artist") or payload.get("artistName"),
                            "artists": payload.get("artists"),
                            "album": payload.get("album"),
                            "albumName": payload.get("albumName"),
                        })
                        title = p_title or title
                        artist = p_artist or artist
                        album = p_album or album
                        raw_dur = payload.get("duration") or payload.get("durationMs") or payload.get("duration_ms")
                        try:
                            dv = float(raw_dur)
                            duration_s = dv / 1000.0 if dv > 10000 else dv
                        except (TypeError, ValueError):
                            duration_s = None
                    if not title or not artist or not album:
                        # 事件不带元数据时从内存搜索会话补齐（播放时会话必在）
                        retained, _entry = _retained_track(request, guid)
                        if retained:
                            r_title, r_artist, r_album = _tag_fields(retained)
                            title = title or r_title
                            artist = artist or r_artist
                            album = album or r_album
                            if not duration_s:
                                try:
                                    duration_s = float(retained.get("duration_s") or 0) or None
                                except (TypeError, ValueError):
                                    duration_s = None
                        if not title or not artist or not album:
                            served = _lookup_playlist_cache_track(guid)
                            if served:
                                s_title, s_artist, s_album = _tag_fields(served)
                                title = title or s_title
                                artist = artist or s_artist
                                album = album or s_album
                    cached = find_cache_file(guid)
                    if cached and (not title or not artist):
                        base = os.path.splitext(os.path.basename(cached))[0]
                        if " - " in base:
                            file_artist, file_title = base.split(" - ", 1)
                            artist = artist or file_artist
                            title = title or file_title
                        else:
                            title = title or base
                    snapshot = {
                        "guid": guid,
                        "title": title,
                        "artist": artist,
                        "source": source_from_online_guid(guid),
                    }
                    if album:
                        snapshot["album"] = album
                    if duration_s:
                        snapshot["duration_s"] = duration_s
                    dailyrec.record_online_play(user_guid, guid, snapshot)
        elif auth_resp is not None and not other_events:
            return auth_resp

    if other_events:
        headers = copy_incoming_headers(request)
        fwd = dict(body)
        fwd["events"] = other_events
        req = upstream_client.build_request(
            "POST",
            "/music/api/v1/event/report",
            headers=headers,
            content=json.dumps(fwd).encode("utf-8"),
        )
        resp = await upstream_client.send(req)
        resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    return JSONResponse(content={"code": 0, "msg": "ok", "data": None})


@app.get("/music/api/v1/play-history/list")
async def play_history_list(request: Request):
    upstream_client = get_upstream_client(request.app)
    envelope = await fetch_upstream_envelope(request, upstream_client)
    if isinstance(envelope, Response):
        return envelope
    headers = envelope.pop("_ext_headers", {})
    if envelope.get("code") != 0:
        return JSONResponse(content=envelope, headers=headers)

    # 官方历史已取回成功（同一组请求头），此时探测被拒不回传鉴权错误，
    # 降级为仅返回官方列表（同收藏列表）。
    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed:
        logger.warning("play history list degraded to official-only: auth probe rejected after official list ok")
        return JSONResponse(content=envelope, headers=headers)

    data = envelope.get("data")
    if not isinstance(data, dict):
        data = {"list": [], "total": 0}
        envelope["data"] = data
    official = data.get("list")
    if not isinstance(official, list):
        official = []
        data["list"] = official

    async with _HISTORY_LOCK:
        online_items = dailyrec.load_online_play_history(user_guid)
    fav_set = {str(it.get("guid") or "") for it in load_online_favorites(user_guid)}
    template = official_track_template(official)
    online_tracks = []
    for it in reversed(online_items):
        guid = str(it.get("guid") or "")
        if not guid:
            continue
        track = it.get("track") if isinstance(it.get("track"), dict) else {}
        info = _snapshot_to_info(track) if track else None
        if not (info or {}).get("title"):
            # 历史快照可能没带元数据：从内存搜索会话补齐（无网络开销）
            retained, _entry = _retained_track(request, guid)
            if retained:
                r_title, r_artist, r_album = _tag_fields(retained)
                info = {
                    **(info or {}),
                    "title": r_title,
                    "artist": r_artist,
                    "album": r_album,
                    "duration_s": retained.get("duration_s") or 0,
                    "ext": retained.get("ext") or "",
                }
        obj = build_favorite_track_obj(guid, info, created_at=int(it.get("playedAt") or time.time()), template=template)
        obj["isFavorite"] = guid in fav_set
        online_tracks.append(disguise_client_json(obj))

    seen = {str(x.get("guid")) for x in official if isinstance(x, dict)}
    merged_online = [t for t in online_tracks if t.get("guid") not in seen]
    data["list"] = merged_online + official
    official_total = data.get("total")
    if not isinstance(official_total, int):
        official_total = len(official)
    data["total"] = official_total + len(merged_online)
    return JSONResponse(content=envelope, headers=headers)


async def _forward_official_play_history_delete(
    request: Request, client: httpx.AsyncClient, official: list[str],
) -> Response | None:
    """混合批次中官方部分先行。返回 None=官方成功，否则返回官方错误响应（本地不动）。"""
    req = client.build_request(
        "POST",
        "/music/api/v1/play-history/delete",
        headers=copy_incoming_headers(request),
        json={"trackGUIDs": official},
    )
    resp = await client.send(req)
    resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})
    if resp.status_code != 200:
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    try:
        payload = resp.json()
    except Exception:
        payload = None
    if not (isinstance(payload, dict) and payload.get("code") == 0):
        return JSONResponse(content=payload or {"code": -1, "msg": "upstream error", "data": None})
    return None


@app.api_route("/music/api/v1/play-history/delete", methods=["POST", "DELETE"])
async def play_history_delete(request: Request):
    """删除播放历史：online 条目删本地存储，官方条目转发官方后端。

    列表是代理拼的（list 合并本地在线历史），删除闭环也必须在代理完成，否则
    带（伪装成官方 32-hex 的）在线 id 的删除请求直达官方被拒。官方契约是批量
    字段 trackGUIDs（前端单条移除与"编辑"多选移除共用，官方后端 binding:"required"），
    兼容单数 trackGUID/guid（含 query 透传）；混合批次官方部分先行，官方失败
    本地不动，与 playlist/remove-track 同款分工。
    """
    upstream_client = get_upstream_client(request.app)
    try:
        body = await request.json()
    except Exception:
        body = {}

    raw = body.get("trackGUIDs") if isinstance(body, dict) else None
    if isinstance(raw, list) and raw:
        candidates = [str(g or "").strip() for g in raw]
    else:
        single = ""
        if isinstance(body, dict):
            single = str(body.get("trackGUID") or body.get("guid") or "").strip()
        if not single:
            single = str(request.query_params.get("trackGUID") or request.query_params.get("guid") or "").strip()
        candidates = [single] if single else []

    resolved = [resolve_real_guid(g) for g in candidates if g]
    online = [g for g in resolved if is_online_guid(g)]
    if not online:
        return await forward_to_upstream(request, upstream_client)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    official = [g for g in resolved if not is_online_guid(g)]
    if official:
        err = await _forward_official_play_history_delete(request, upstream_client, official)
        if err is not None:
            return err

    async with _HISTORY_LOCK:
        for guid in online:
            try:
                dailyrec.remove_online_play(user_guid, guid)
            except Exception as e:
                logger.warning("Error deleting from online play history for user %s: %s", user_guid, e)

    return JSONResponse(content={"code": 0, "msg": "", "data": None})


# 官方端点取证：未拦截路径 -> 转发次数（仅内存，重启清零；用于首见告警与采样）
_FORWARDED_PATH_STATS: dict[str, int] = {}


def _trace_forwarded_endpoint(request: Request) -> None:
    """catch_all 透传前的官方端点取证日志。

    未拦截的 /music/api 请求：同一路径首见打 INFO（官方 App 更新引入新端点时
    日志立刻可见），之后每 50 次采样一条；FNMUSIC_TRACE_FORWARD=detail 逐条记录。
    """
    path = request.url.path
    if not path.startswith("/music/api/"):
        return
    count = _FORWARDED_PATH_STATS.get(path, 0) + 1
    _FORWARDED_PATH_STATS[path] = count
    if CONF.get("trace_forward") or count == 1 or count % 50 == 0:
        logger.info(
            "forward-unhandled %s %s (#%d) —— 官方端点未被代理拦截，仅透传",
            request.method,
            path,
            count,
        )


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def catch_all(request: Request, full_path: str):
    _trace_forwarded_endpoint(request)
    return await forward_to_upstream(request, get_upstream_client(request.app))
