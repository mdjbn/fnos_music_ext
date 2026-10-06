"""洛雪音乐同步服务器（lx-music-sync-server）歌单 → 飞牛音乐列表（只读虚拟歌单）。

数据怎么来的：那个服务端**没有歌单 HTTP API**，只能在「AES 密码握手 + WebSocket +
message2call RPC」之后由服务端把歌单整包推下来——细节全在 `proxy/lxproto.py`。
本模块负责飞牛这一侧：

* 配置读取（`FNMUSIC_LX_SYNC_*`，直接读 os.environ，由 app.py 的 .env 热重载同步进来）；
* 把服务端的 lx 歌曲（`{source,name,singer,interval,meta{songId,hash,copyrightId,...}}`）
  转成 A 的在线曲目形态（`id="lx:<src>:<identifier>"`，与 `lxmusic-service` 的
  `/api/v1/track/url` 对得上，见 `fetch_lx_search` 的 item 形状）；
* 内存缓存 + stale-while-revalidate：有旧数据先照旧显示、后台刷新；失败进冷却，
  绝不让「同步服务连不上」拖垮官方歌单列表；
* clientId/会话 key 落盘复用，避免每次连接都在服务端新增一台设备。

只读语义：`list_sync_get_list_data` 一律回空表 ⇒ 服务端的「它本地有数据、对端没有」
分支会把歌单推给我们（`src/modules/list/sync/sync.ts:238`），因此不会改动服务端歌单。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from typing import Any

try:  # package 形态（tests / uvicorn --app-dir 两种都支持，与其它模块一致）
    from . import lxproto
except ImportError:  # pragma: no cover - uvicorn --app-dir proxy
    import lxproto  # type: ignore

logger = logging.getLogger("fnmusic.ext.lxsync")

GUID_PREFIX = "online:playlist:lxsync:"
# 服务端的列表 id 由客户端生成，理论上任意字符串；guid 要求是安全字符集，
# 不安全的一律换成确定性的 md5 短码（正反都能算出来，重启后也稳定）。
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
# 不安全 id 的替身形态（`h` + md5 前 16 位）；用它判定"这是短码而不是真 id"
_HASHED_ID = re.compile(r"^h[0-9a-f]{16}$")

_MAX_PLAYLISTS = 50
_MAX_TRACKS = 1000
_FIRST_FETCH_TIMEOUT_S = 8.0     # 首次（无任何缓存）时的同步等待上限
_SESSION_TIMEOUT_S = 20.0        # 单次同步会话总预算
_FAIL_COOLDOWN_S = 120.0         # 失败冷却：期间不再打扰服务端
_REFRESH_DEFAULT_S = 300.0       # 默认刷新间隔
_LX_SOURCES = ("kw", "kg", "tx", "wy", "mg")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def _flag(name: str, default: str = "false") -> bool:
    raw = str(os.environ.get(name, default) or default).strip().lower()
    return raw in ("1", "true", "yes", "on")


def sync_enabled() -> bool:
    return _flag("FNMUSIC_LX_SYNC_ENABLED", "false") and bool(server_url() and password())


def server_url() -> str:
    return str(os.environ.get("FNMUSIC_LX_SYNC_URL", "") or "").strip()


def password() -> str:
    return str(os.environ.get("FNMUSIC_LX_SYNC_PASSWORD", "") or "").strip()


def device_name() -> str:
    return str(os.environ.get("FNMUSIC_LX_SYNC_DEVICE", "") or "").strip() or "fnmusic-ext"


def refresh_s() -> float:
    try:
        return max(30.0, min(86400.0, float(str(os.environ.get("FNMUSIC_LX_SYNC_REFRESH_S", "") or _REFRESH_DEFAULT_S))))
    except (TypeError, ValueError):
        return _REFRESH_DEFAULT_S


def home_dir() -> str:
    env = (os.environ.get("FNMUSIC_HOME") or "").strip()
    if env:
        return env
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def cache_dir() -> str:
    return os.environ.get("FNMUSIC_LX_SYNC_DIR") or os.path.join(home_dir(), "lxsync_cache")


def identity_path() -> str:
    return os.path.join(cache_dir(), "identity.json")


# --------------------------------------------------------------- guid ----

def _token_of(pid: str) -> str:
    pid = str(pid)
    return pid if _SAFE_ID.match(pid) else "h" + hashlib.md5(pid.encode("utf-8")).hexdigest()[:16]


def lxsync_playlist_guid(pid: str) -> str:
    return f"{GUID_PREFIX}{_token_of(pid)}"


def is_lxsync_playlist_guid(guid: str | None) -> bool:
    return str(guid or "").startswith(GUID_PREFIX)


def lxsync_playlist_id_from_guid(guid: str | None) -> str:
    """guid → 服务端歌单 id（内存映射优先，其次安全字符原样返回）。

    不安全 id 被换成 `h<md5前16位>`，必须靠映射还原；映射里没有时返回空串
    （当作"未知歌单"），绝不拿这个短码当 id 去打服务端——这正是下面那条
    `^h[0-9a-f]{16}$` 规则的用途：形如短码又不在映射里，就一定是猜的。
    """
    g = str(guid or "")
    if not is_lxsync_playlist_guid(g):
        return ""
    token = g[len(GUID_PREFIX):]
    mapped = _state["id_map"].get(token)
    if mapped:
        return str(mapped)
    if _HASHED_ID.match(token):
        return ""
    return token if _SAFE_ID.match(token) else ""


# ------------------------------------------------------------- 曲目转换 ----

def _field(song: dict, *names: Any, default: Any = "") -> Any:
    """取字段：先看 `meta`（新版形态），再退回歌曲对象本身（旧版/收藏歌单形态）。

    真实数据里两种形态并存（同一账号的不同歌单！）：
    * 新版：`{name, singer, source, interval, meta:{songId, strMediaMid, albumName, picUrl}}`
    * 旧版：`{name, singer, source, interval, songId, strMediaMid, songmid, albumName, img}`
      —— 没有 `meta`，元数据直接挂在歌曲上。
    只认 `meta` 的后果实测过：整个歌单 374 首全部被当成「不可播」丢掉，卡片显示 0 首。
    """
    meta = song.get("meta") if isinstance(song.get("meta"), dict) else {}
    for name in names:
        for holder in (meta, song):
            val = holder.get(name)
            if val not in (None, ""):
                return val
    return default


def _duration_s(interval: Any) -> float:
    raw = str(interval or "").strip()
    m = re.match(r"^(\d{1,3}):([0-5]?\d)$", raw)
    if m:
        return float(int(m.group(1)) * 60 + int(m.group(2)))
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def _clean_ident(src: str, raw: Any) -> str:
    """去掉 `wy_`/`tx_` 前缀与 `src:id` 形态，只留平台主键。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith(f"{src}_"):
        text = text[len(src) + 1:]
    if ":" in text:
        text = text.split(":")[-1]
    return text.strip()


def _identifier_of(song: dict) -> "tuple[str, str]":
    """(规范平台码, lxmusic-service 认的歌曲标识)。

    标识字段按平台取，且与 `lxmusic-service/app.py` 对齐：
    * tx → **songmid**（`_fill_script_meta` 就是拿它去 `fcg_play_single_song.fcg?songmid=`，
      `lx:tx:<mid>` 里的 mid 也是搜索结果的 `mid`）——`strMediaMid` 只作兜底；
    * kg → 文件 hash；mg → copyrightId；kw/wy → songId（旧形态可能只有 `songmid`/`id`）。
    """
    src = str(song.get("source") or "").strip().lower()
    if src not in _LX_SOURCES:
        return "", ""
    if src == "kg":
        ident = _clean_ident(src, _field(song, "hash", "songId", "id"))
    elif src == "mg":
        ident = _clean_ident(src, _field(song, "copyrightId", "songId", "id"))
    elif src == "tx":
        ident = _clean_ident(src, _field(song, "songmid", "strMediaMid", "songId", "id"))
    else:
        ident = _clean_ident(src, _field(song, "songId", "songmid", "id"))
    return (src, ident) if ident else ("", "")


def track_item(song: dict) -> "dict | None":
    """lx 歌曲 → A 的在线曲目 item（`fetch_lx_search` 同款字段，可直接喂 build_online_track）。"""
    if not isinstance(song, dict):
        return None
    src, ident = _identifier_of(song)
    if not src:
        return None          # 本地文件/未知来源：飞牛这侧播不了，直接不放进来
    return {
        "id": f"lx:{src}:{ident}",
        "source": "lx",
        "lx_source": src,
        "version": "",
        "title": str(_field(song, "name") or ""),
        "artist": str(_field(song, "singer") or ""),
        "album": str(_field(song, "albumName") or ""),
        "duration_s": _duration_s(song.get("interval")),
        "ext": "mp3",
        "cover_url": str(_field(song, "picUrl", "img") or "").strip(),
        "file_size": 0,
        "lyric": "",
        "verified": False,
    }


# --------------------------------------------------------------- 缓存 ----

_state: dict[str, Any] = {
    "cards": [],          # 注入飞牛歌单列表的卡片
    "tracks": {},         # guid → [lx 歌曲 dict]（原始形态，按需转换）
    "saved_at": 0.0,      # 上次成功同步时间（monotonic）
    "fail_until": 0.0,
    "id_map": {},         # token → 服务端歌单 id
    "error": "",
}
_lock = asyncio.Lock()
_refresh_task: "asyncio.Task | None" = None


def reset_for_test() -> None:
    global _refresh_task
    _state.update({"cards": [], "tracks": {}, "saved_at": 0.0, "fail_until": 0.0,
                   "id_map": {}, "error": ""})
    _refresh_task = None


def cached_cards() -> list:
    return list(_state["cards"])


def cached_tracks(pid: str) -> "list | None":
    guid = lxsync_playlist_guid(pid)
    songs = _state["tracks"].get(guid)
    return None if songs is None else list(songs)


def card_for(pid: str) -> "dict | None":
    want = lxsync_playlist_guid(pid)
    for card in _state["cards"]:
        if str(card.get("guid") or "") == want:
            return dict(card)
    return None


def cover_url_for(pid: str) -> str:
    songs = _state["tracks"].get(lxsync_playlist_guid(pid)) or []
    for song in songs:
        pic = str(_field(song, "picUrl", "img") or "").strip()
        if pic:
            return pic
    return ""


def status() -> dict:
    """给 /_ext/lxSync 诊断用（不含密码）。"""
    return {
        "enabled": sync_enabled(),
        "url": server_url(),
        "password_configured": bool(password()),
        "device": device_name(),
        "insecure_tls": lxproto.tls_insecure(),
        "refresh_s": refresh_s(),
        "playlists": len(_state["cards"]),
        "tracks": {str(c.get("guid")): len(_state["tracks"].get(str(c.get("guid")) or "", []))
                   for c in _state["cards"]},
        "age_s": round(time.monotonic() - _state["saved_at"], 1) if _state["saved_at"] else None,
        "error": _state["error"],
        "cooldown_s": round(max(0.0, _state["fail_until"] - time.monotonic()), 1),
    }


# --------------------------------------------------------------- 同步 ----

def _build_state(list_data: dict) -> "tuple[list, dict, dict]":
    """服务端 list data → (cards, tracks, id_map)。"""
    cards: list[dict] = []
    tracks: dict[str, list] = {}
    id_map: dict[str, str] = {}
    now = int(time.time())
    groups: list[tuple[str, dict]] = []
    love = list_data.get("loveList") or []
    if isinstance(love, list) and love:
        groups.append(("我的收藏（洛雪）", {"id": "love", "name": "我的收藏（洛雪）",
                                       "locationUpdateTime": None, "list": love}))
    default = list_data.get("defaultList") or []
    if isinstance(default, list) and default:
        # 试听列表（LIST_IDS.DEFAULT = 'default'）：洛雪里点了但没加进歌单的临时队列。
        # 空的时候不挂空壳，免得歌单列表里多一张永远空的卡片。
        groups.append(("试听列表（洛雪）", {"id": "default", "name": "试听列表（洛雪）",
                                       "locationUpdateTime": None, "list": default}))
    for pl in list_data.get("userList") or []:
        if isinstance(pl, dict) and isinstance(pl.get("list"), list):
            groups.append((str(pl.get("name") or ""), pl))
    for name, pl in groups[:_MAX_PLAYLISTS]:
        pid = str(pl.get("id") or "")
        songs = [s for s in (pl.get("list") or []) if _identifier_of(s)[0]][:_MAX_TRACKS]
        if not pid:
            continue
        # 空歌单也注入：用户建了但还没加歌，看不见会以为"同步坏了"（trackCount=0 很直观）
        guid = lxsync_playlist_guid(pid)
        id_map[_token_of(pid)] = pid
        cover_pic = ""
        for s in songs:
            cover_pic = str(_field(s, "picUrl", "img") or "").strip()
            if cover_pic:
                break
        cards.append({
            "guid": guid,
            "name": name or f"洛雪歌单 {_token_of(pid)}",
            "cover_url": cover_pic,
            "createdAt": now,
            "updatedAt": now,
            "trackCount": len(songs),
            # 与推荐/账号歌单一致：客户端只对 isDaily 的 online: 歌单拉曲目列表
            "isDaily": True,
        })
        tracks[guid] = songs
    return cards, tracks, id_map


async def _do_refresh(timeout: float) -> bool:
    """连一次同步服务，把歌单整包拉回来并替换缓存。失败只记状态，不抛。

    失败时**在这里**就进冷却：SWR 路径（有旧卡片）也走这个函数，冷却若只由
    `peek_summaries` 的"无缓存"分支设置，服务端挂掉时每次列表请求都会重新连一次。
    """
    url, pwd = server_url(), password()
    if not (url and pwd):
        _state["error"] = "未配置同步服务地址或密码"
        return False
    ident = lxproto.load_identity(identity_path())
    try:
        result = await lxproto.sync_session(
            url, pwd, device_name=device_name(),
            client_id=ident.get("client_id", ""), key_b64=ident.get("key", ""),
            timeout=timeout,
        )
    except lxproto.LxSyncError as exc:
        _state["error"] = str(exc)
        _state["fail_until"] = time.monotonic() + _FAIL_COOLDOWN_S
        logger.warning("lx sync failed: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001
        _state["error"] = f"{type(exc).__name__}: {exc}"
        _state["fail_until"] = time.monotonic() + _FAIL_COOLDOWN_S
        logger.warning("lx sync error: %s: %s", type(exc).__name__, exc)
        return False
    if result.get("paired"):
        lxproto.save_identity(identity_path(), result["client_id"], result["key"])
    cards, tracks, id_map = _build_state(result.get("list_data") or {})
    _state.update({"cards": cards, "tracks": tracks, "id_map": id_map,
                   "saved_at": time.monotonic(), "fail_until": 0.0, "error": ""})
    logger.info("lx sync ok: %d playlists (%s)", len(cards), "paired" if result.get("paired") else "reuse")
    return True


def _schedule_refresh(timeout: float = _SESSION_TIMEOUT_S) -> None:
    """后台刷新（同一时刻只允许一个）。"""
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _refresh_task = loop.create_task(_do_refresh(timeout))


async def peek_summaries() -> list:
    """歌单列表页用：新鲜→直接给；过期→先给旧的、后台刷新；无缓存→同步拉一次。"""
    if not sync_enabled():
        return []
    now = time.monotonic()
    fresh = _state["saved_at"] and (now - _state["saved_at"]) < refresh_s()
    if _state["cards"] and fresh:
        return cached_cards()
    if now < _state["fail_until"]:
        return cached_cards()
    if _state["cards"]:
        _schedule_refresh()          # SWR：旧数据先展示
        return cached_cards()
    async with _lock:
        if not _state["cards"]:
            ok = await _do_refresh(_FIRST_FETCH_TIMEOUT_S)
            if not ok:
                _state["fail_until"] = time.monotonic() + _FAIL_COOLDOWN_S
    return cached_cards()


async def load_tracks(pid: str, build_track) -> list:
    """某个洛雪歌单的曲目（缓存里就有，不额外打服务端）。"""
    songs = cached_tracks(pid) or []
    if not songs:
        # 列表页还没同步过（例如直接深链打开某歌单）：先拉一次再取
        if sync_enabled():
            await peek_summaries()
            songs = cached_tracks(pid) or []
    out: list = []
    for song in songs:
        item = track_item(song)
        if item is None:
            continue
        try:
            track = build_track(item)
        except Exception as exc:  # noqa: BLE001
            logger.debug("lx track build failed: %s: %s", type(exc).__name__, exc)
            continue
        if isinstance(track, dict) and track.get("guid"):
            out.append(track)
    return out


def schedule_prefetch() -> None:
    """列表页顺带预热（冷却/在途由 _schedule_refresh 内部把关）。"""
    if sync_enabled() and _state["saved_at"] and (time.monotonic() - _state["saved_at"]) >= refresh_s():
        _schedule_refresh()
