"""Optional NEMbox internals for batch detail / lyrics (NetEase-MusicBox)."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

try:
    import httpx
except ImportError:
    httpx = None

try:
    import requests
except ImportError:
    requests = None

logger = logging.getLogger("musicbox_service.netease_ext")

_api_lock = threading.Lock()
_api_instance = None

# 登录态探测结果缓存：(结果, 探测时刻单调钟)。失败短缓存、成功长缓存，
# 过期时强制重建实例重读磁盘 cookie 再下结论，避免常驻实例揣着过期 cookie。
_login_state_lock = threading.Lock()
_login_state: "tuple[bool, float] | None" = None
_LOGIN_TTL_OK_S = 300.0
_LOGIN_TTL_FAIL_S = 60.0

# 进程内取链结果缓存：(item, 过期时刻单调钟)。网易直链自带过期时间，只做短缓存。
_url_cache_lock = threading.Lock()
_url_cache: "dict[tuple[int, str], tuple[dict, float]]" = {}
_URL_CACHE_TTL_OK_S = 600.0
_URL_CACHE_TTL_FAIL_S = 60.0
_URL_CACHE_MAX = 4096


def _get_api_locked():
    """调用方必须已持有 _api_lock：取实例 + 用实例在同一次持锁内完成。"""
    global _api_instance
    if _api_instance is None:
        from runner import ensure_xdg_dirs

        ensure_xdg_dirs()
        from NEMbox.api import NetEase

        _api_instance = NetEase()
    return _api_instance


def _get_api():
    with _api_lock:
        return _get_api_locked()


def reset_api(reason: str = "") -> None:
    """丢弃常驻 NetEase 实例与相关缓存，下次 _get_api() 重建并重读磁盘 cookie。

    登录由 CLI 子进程完成并写盘，而 NEMbox 的 NetEase 只在构造时读一次盘；
    磁盘 cookie 变化（扫码成功等）后不重建实例，服务进程内就永远是旧登录态。
    """
    global _api_instance, _login_state
    with _api_lock:
        if _api_instance is not None:
            try:
                _api_instance.session.close()
            except Exception:
                pass
        _api_instance = None
    with _login_state_lock:
        _login_state = None
    with _url_cache_lock:
        _url_cache.clear()
    if reason:
        logger.info("netease api instance reset: %s", reason)


def _map_song_detail(item: dict[str, Any]) -> dict[str, Any]:
    sid = item.get("id") or item.get("song_id")
    song_id = int(sid) if sid is not None else 0
    name = str(item.get("name") or "")
    ar_list = item.get("ar") or item.get("artists") or []
    if isinstance(ar_list, list):
        artist = " / ".join(
            str(a.get("name")) for a in ar_list if isinstance(a, dict) and a.get("name")
        )
    else:
        artist = ""
    al = item.get("al") or item.get("album") or {}
    if isinstance(al, dict):
        album_name = str(al.get("name") or "")
        album_pic_url = str(al.get("picUrl") or al.get("pic_url") or "")
    else:
        album_name = ""
        album_pic_url = ""
    duration_ms = int(item.get("dt") or item.get("duration") or 0)
    return {
        "song_id": song_id,
        "name": name,
        "artist": artist,
        "album_name": album_name,
        "album_pic_url": album_pic_url,
        "duration_ms": duration_ms,
        "has_sq": bool(item.get("sq")),
        "has_hr": bool(item.get("hr")),
    }


def check_is_logged_in() -> bool:
    global _login_state
    with _login_state_lock:
        state = _login_state
    if state is not None:
        logged, at = state
        if time.monotonic() - at < (_LOGIN_TTL_OK_S if logged else _LOGIN_TTL_FAIL_S):
            return logged
        # 缓存过期：磁盘 cookie 可能已换（重新扫码/在别处登录），先重建实例再探测
        with _login_state_lock:
            _login_state = None
        reset_api("login-state ttl expired")
    try:
        with _api_lock:
            api = _get_api_locked()
            info = api.get_account_info()
        logged = bool(info and (info.get("account") or info.get("profile")))
    except Exception:
        logged = False
    with _login_state_lock:
        _login_state = (logged, time.monotonic())
    return logged


def filter_playable_song_ids(ids: list[int]) -> set[int]:
    """根据真实可播放状态过滤歌曲 ID。

    - 未登录时：使用 api.songs_url 批量获取真实可播状态。凡是 url 为空/404、或者带有 freeTrialInfo（试听片段）且 fee != 0 的曲目，一律过滤掉。
    - 已登录时：如果有账号权限能取到完整真实 url 且非试听，则允许返回；若无权限仍过滤。
    - 只能试听30~45秒片段（带 freeTrialInfo/试听限制）的歌曲，绝不能当作可播放曲目返回。
    """
    if not ids:
        return set()
    with _api_lock:
        try:
            api = _get_api_locked()
            urls_data = api.songs_url(ids)
        except Exception:
            return set()
    if not isinstance(urls_data, list):
        return set()

    logged_in = check_is_logged_in()
    playable_ids: set[int] = set()
    for item in urls_data:
        if not isinstance(item, dict):
            continue
        sid = item.get("id") or item.get("song_id")
        if not sid:
            continue
        try:
            sid_int = int(sid)
        except (ValueError, TypeError):
            continue

        url = item.get("url")
        code = item.get("code")
        fee = item.get("fee", 0)
        free_trial = item.get("freeTrialInfo")

        # 核心铁律：url 为空或 code == 404，坚决过滤
        if not url or not str(url).strip() or code == 404:
            continue
        # 凡是带有 freeTrialInfo（试听片段）且 fee != 0 的曲目，一律过滤掉
        # 并且只能试听片段的歌曲绝不当作可播返回
        if free_trial:
            continue
        # 未登录状态下，收费/VIP/专辑曲目坚决不返回
        if not logged_in and fee != 0 and fee not in (0, 8):
            continue

        playable_ids.add(sid_int)
    return playable_ids


def get_song_url(song_id: int, quality: str) -> "dict[str, Any] | None":
    """进程内解析单曲播放链接（复用常驻实例，免起 CLI 子进程）。

    返回对齐 CLI `song url --json` 的 data 字段（含 url/code/fee 等）；接口
    异常或返回空时返回 None，由调用方降级 CLI（保留结构化错误语义）。
    结果短缓存（成功 600s / 失败 60s，键含音质），reset_api 时随实例一并清空。
    """
    key = (int(song_id), str(quality))
    now = time.monotonic()
    with _url_cache_lock:
        hit = _url_cache.get(key)
        if hit and now < hit[1]:
            return hit[0]
    try:
        with _api_lock:
            api = _get_api_locked()
            # songs_url 的音质取自全局 Config：临时改写再恢复（CLI cmd_song_url 同款做法）
            from NEMbox.config import Config

            config = Config()
            old_quality = config.get("music_quality")
            config.config.setdefault("music_quality", {})["value"] = quality
            try:
                urls = api.songs_url([int(song_id)])
            finally:
                config.config["music_quality"]["value"] = old_quality
    except Exception:
        return None
    if not isinstance(urls, list) or not urls or not isinstance(urls[0], dict):
        return None
    item = urls[0]
    ok = item.get("code") == 200 and item.get("url")
    with _url_cache_lock:
        if len(_url_cache) >= _URL_CACHE_MAX:
            expire = time.monotonic()
            for k in [k for k, v in _url_cache.items() if v[1] <= expire]:
                del _url_cache[k]
        _url_cache[key] = (item, time.monotonic() + (_URL_CACHE_TTL_OK_S if ok else _URL_CACHE_TTL_FAIL_S))
    return item


def batch_song_details(ids: list[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    with _api_lock:
        api = _get_api_locked()
        raw_items = api.songs_detail(ids)
    if not raw_items or not isinstance(raw_items, list):
        return []

    playable_ids = filter_playable_song_ids(ids)

    detail_map: dict[int, dict[str, Any]] = {}
    for item in raw_items:
        if isinstance(item, dict):
            mapped = _map_song_detail(item)
            if mapped["song_id"] in playable_ids:
                detail_map[mapped["song_id"]] = mapped
    return [detail_map[sid] for sid in ids if sid in detail_map]


def song_lyric_pair(song_id: int) -> dict[str, str]:
    with _api_lock:
        api = _get_api_locked()
        raw_lyric = api.song_lyric(song_id)
        raw_tlyric = api.song_tlyric(song_id)
    lyric_str = "\n".join(str(line) for line in raw_lyric) if isinstance(raw_lyric, list) else ""
    tlyric_str = "\n".join(str(line) for line in raw_tlyric) if isinstance(raw_tlyric, list) else ""
    return {"lyric": lyric_str, "tlyric": tlyric_str}


def search_web_fallback(keyword: str, stype: str = "song", limit: int = 20) -> list[dict[str, Any]]:
    """网易官方 Web 搜索接口降级容错（https://music.163.com/api/search/get/web）。

    当主接口被风控（如 405 操作频繁）或失败时，调用官方备用接口获取歌曲列表。
    """
    if not keyword or not keyword.strip():
        return []
    url = "https://music.163.com/api/search/get/web"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://music.163.com",
        "Cookie": "os=pc",
    }
    type_map = {
        "song": 1,
        "album": 10,
        "artist": 100,
        "playlist": 1000,
    }
    data = {
        "s": keyword.strip(),
        "type": str(type_map.get(stype, 1)),
        "limit": str(limit),
        "offset": "0",
    }
    res = None
    try:
        if httpx is not None:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, headers=headers, data=data)
                resp.raise_for_status()
                res = resp.json()
        elif requests is not None:
            resp = requests.post(url, headers=headers, data=data, timeout=10.0)
            resp.raise_for_status()
            res = resp.json()
        else:
            import urllib.parse
            import urllib.request
            encoded = urllib.parse.urlencode(data).encode("utf-8")
            req = urllib.request.Request(url, data=encoded, headers=headers)
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                res = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning("search_web_fallback request failed: %s", e)
        return []

    if not isinstance(res, dict):
        return []
    result = res.get("result")
    if not isinstance(result, dict):
        return []

    songs = result.get("songs")
    if not isinstance(songs, list):
        return []

    out: list[dict[str, Any]] = []
    for s in songs:
        if not isinstance(s, dict):
            continue
        sid = s.get("id") or s.get("song_id")
        if not sid:
            continue
        try:
            sid_int = int(sid)
        except (ValueError, TypeError):
            continue

        name = str(s.get("name") or s.get("title") or "")
        artists = s.get("artists") or []
        if isinstance(artists, list):
            artist = " / ".join(str(a.get("name")) for a in artists if isinstance(a, dict) and a.get("name"))
        else:
            artist = str(s.get("artist") or "")
        album = s.get("album") or {}
        album_name = str(album.get("name") or "") if isinstance(album, dict) else str(s.get("album_name") or "")
        duration_raw = s.get("duration") or 0
        try:
            dur = float(duration_raw)
            if dur > 10000:
                dur = dur / 1000.0
        except (ValueError, TypeError):
            dur = 0.0

        out.append({
            "song_id": sid_int,
            "id": sid_int,
            "song_name": name,
            "title": name,
            "name": name,
            "artist": artist,
            "album_name": album_name,
            "album": album_name,
            "duration": dur,
            "quality": "lossless",
        })
    return out


def _int_or_zero(val: Any) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _ms_to_epoch_s(val: Any) -> int:
    """网易接口的毫秒时间戳 -> 秒；异常回落当前时间。"""
    n = _int_or_zero(val)
    if n > 0:
        return n // 1000
    return int(time.time())


def _map_playlist_summary(item: dict) -> dict:
    pid = _int_or_zero(item.get("id") or item.get("playlist_id"))
    return {
        "playlist_id": pid,
        "name": str(item.get("name") or ""),
        "cover_url": str(item.get("coverImgUrl") or item.get("cover_img_url") or ""),
        "track_count": _int_or_zero(item.get("trackCount") or item.get("track_count")),
        "created_at": _ms_to_epoch_s(item.get("createTime")),
        "updated_at": _ms_to_epoch_s(item.get("updateTime")),
        "user_id": _int_or_zero(item.get("userId") or item.get("user_id")),
        "special_type": _int_or_zero(item.get("specialType") or item.get("special_type")),
    }


def user_playlists(limit: int = 100) -> "dict[str, Any] | None":
    """当前登录账号的歌单列表（含自建与收藏，自建判定交给调用方按 user_id 过滤）。

    返回 {"uid": 账号id, "playlists": [摘要行]}；未登录/接口失败返回 None。
    """
    if not check_is_logged_in():
        return None
    try:
        with _api_lock:
            api = _get_api_locked()
            info = api.get_account_info()
            uid = 0
            for src in (info.get("account"), info.get("profile")):
                if isinstance(src, dict):
                    uid = _int_or_zero(src.get("id") or src.get("userId"))
                    if uid:
                        break
            if not uid:
                return None
            rows = api.user_playlist(uid, offset=0, limit=limit)
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    playlists = []
    for it in rows:
        if isinstance(it, dict):
            mapped = _map_playlist_summary(it)
            if mapped["playlist_id"] > 0:
                playlists.append(mapped)
    return {"uid": uid, "playlists": playlists}


def playlist_track_ids(playlist_id: int) -> "list[int] | None":
    """歌单曲目 ID 列表（保持网易歌单内顺序）；未登录返回 None，空歌单返回 []。"""
    if not check_is_logged_in():
        return None
    try:
        with _api_lock:
            api = _get_api_locked()
            raw = api.playlist_songlist(int(playlist_id))
    except Exception:
        return None
    if not isinstance(raw, list):
        return []
    ids: list[int] = []
    for it in raw:
        sid = it if isinstance(it, int) else (it.get("id") if isinstance(it, dict) else None)
        sid = _int_or_zero(sid)
        if sid > 0:
            ids.append(sid)
    return ids


# ===========================================================================
# W5 增量移植：G（gzywd v2.9.30）独有的进程内实现（只增不改）。
#
# 命名约定：与 A 既有同名函数语义/签名冲突的（user_playlists、playlist_track_ids、
# filter_playable_song_ids、check_is_logged_in、reset_api、_get_api）一律保留 A 版
# 不动；需要 G 语义时新增带后缀的兼容函数（user_playlists_for_uid、
# playlist_track_ids_limited、reset_api_instance），由 app.py 的新端点调用。
# ===========================================================================


def _cookie_stamp(api) -> tuple:
    """cookie 文件的 (mtime_ns, size) 指纹。取不到就用空指纹（视为"无 cookie"）。"""
    try:
        path = getattr(getattr(api, "storage", None), "cookie_path", "") or ""
        if path and os.path.exists(path):
            st = os.stat(path)
            return (st.st_mtime_ns, st.st_size)
    except OSError:
        pass
    return ()


def _build_api():
    from runner import ensure_xdg_dirs

    ensure_xdg_dirs()
    from NEMbox.api import NetEase

    return NetEase()


def invalidate_login_cache() -> None:
    """登录态变化（扫码登录/登出）后调用：只清探测缓存，不动常驻实例。"""
    global _login_state
    with _login_state_lock:
        _login_state = None


def reset_api_instance() -> None:
    """丢弃进程内实例、取链缓存与登录态缓存（对齐 G 的语义）。

    A 侧的实例生命周期由 ``reset_api(reason)`` 管理；这里仅做兼容封装，
    不改变 A 既有函数的行为。
    """
    reset_api("api instance reset")
    invalidate_login_cache()


def _pick(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


_VIP_EXPIRY_KEYS = (
    "vipExpiryTime", "vipExpiry", "vipExpires", "vip_expire", "vipExpireTime",
    "expireTime", "expiredTime", "endTime", "validTime",
)


def _looks_like_future_ms(value: Any) -> bool:
    """只接受"看起来像未来的毫秒时间戳"的值，绝不把别的字段硬凑成到期时间。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return False
    if n <= 0:
        return False
    now_ms = time.time() * 1000
    # 合理区间：当前时间之后、且不超过 50 年
    return now_ms < n < now_ms + 50 * 365 * 86400 * 1000


def _find_vip_expiry(*sources: dict[str, Any]) -> int:
    for src in sources:
        if not isinstance(src, dict):
            continue
        for key in _VIP_EXPIRY_KEYS:
            v = src.get(key)
            if _looks_like_future_ms(v):
                return int(v)
    return 0


def auth_detail() -> dict[str, Any]:
    """登录态详情：是否登录、昵称、userId、VIP 类型与到期时间。

    只读取 NEMbox 已有的 get_account_info()，不额外发请求；任何字段缺失都
    以 None/0 返回，不编造。VIP 到期时间对若干候选字段名做尽力探测，
    探不到就如实返回 0（上游 0.5.3 无提供到期时间的接口）。
    """
    try:
        api = _get_api()
        with _api_lock:
            info = api.get_account_info() or {}
    except Exception as exc:  # noqa: BLE001 - 上游异常统一降级为未登录
        return {"logged_in": False, "error": str(exc)[:200]}

    if not isinstance(info, dict):
        return {"logged_in": False}

    profile = info.get("profile") if isinstance(info.get("profile"), dict) else {}
    account = info.get("account") if isinstance(info.get("account"), dict) else {}
    src: dict[str, Any] = profile or account or info

    nickname = _pick(src, "nickname", "userName", "nick_name", "name")
    user_id = _pick(src, "userId", "user_id", "id") or _pick(info, "userId", "user_id")
    vip_type = _pick(src, "vipType", "vip_type") or _pick(account, "vipType", "vip_type")
    logged_in = bool(profile or account or (nickname and user_id))

    try:
        vip_type_int = int(vip_type) if vip_type is not None else 0
    except (TypeError, ValueError):
        vip_type_int = 0

    vip_expire_int = _find_vip_expiry(src, account, profile, info)
    if not vip_expire_int and vip_type_int > 0:
        # 尽力再问一次用户详情接口；失败或字段缺失都静默放弃，不编造
        try:
            uid = int(user_id) if user_id else 0
        except (TypeError, ValueError):
            uid = 0
        if uid:
            try:
                with _api_lock:
                    detail = api.request("POST", f"/weapi/v1/user/detail/{uid}") or {}
            except Exception:  # noqa: BLE001
                detail = {}
            if isinstance(detail, dict):
                dp = detail.get("profile") if isinstance(detail.get("profile"), dict) else {}
                vip_expire_int = _find_vip_expiry(dp, detail)

    return {
        "logged_in": logged_in,
        "nickname": str(nickname or ""),
        "user_id": str(user_id or ""),
        "vip_type": vip_type_int,
        "vip_expires_ms": vip_expire_int,
        # 上游没提供到期时间时明确标记，让前端显示"未知"而不是"剩余 0 天"
        "vip_expires_known": vip_expire_int > 0,
    }


def _flag_of(d: Any, key: str) -> bool:
    v = d.get(key) if isinstance(d, dict) else None
    return v is True or str(v).lower() == "true"


def is_trial_snippet(item: dict[str, Any]) -> bool:
    """判定该曲目是否只是「试听片段」（不能真正播放）。

    .. warning:: 千万不要写成 ``item.get("freeTrialInfo") or item.get("freeTrialPrivilege")``。

    ``freeTrialPrivilege`` 是网易云**每条 song/url 响应都必带**的标准结构体，即使
    一切正常也存在，且是个非空 dict（＝真理值）。真正的试听信号在这个结构体**内部
    的布尔位**里：``resConsumable`` / ``userConsumable`` 为 True 表示正在消耗试听额度。
    ``freeTrialInfo`` 则相反：只有确实是试听曲目才带非空内容，因此对它做存在性判断安全。
    """
    priv = item.get("freeTrialPrivilege")
    if _flag_of(priv, "resConsumable") or _flag_of(priv, "userConsumable"):
        return True
    info = item.get("freeTrialInfo")
    return bool(info) and info is not None


# 未登录时是否降级为只播免费曲目（默认开；关掉则未登录直接不放行任何在线曲目）。
# G 侧的同名开关；A 既有 filter_playable_song_ids 不走这条规则，二者互不影响。
FREE_ONLY_ON_LOGOUT = (
    os.environ.get("FNMUSIC_FREE_ONLY_ON_LOGOUT", "true").strip().lower()
    in ("true", "1", "yes", "on")
)


def playable_url_map(ids: list[int]) -> dict[int, dict[str, Any]]:
    """返回 {song_id: 直链信息} ——只包含当前账号**真实可播**的曲目。

    与上游 NEMbox 的 ``dig_info`` 有本质区别：dig_info 在**任意一首**歌取不到 url 时
    会 ``return []``，把整个列表清空。因此这里改为**逐首判定**：坏数据只影响它自己那一首。

    过滤规则：
    - 已登录：账号自身权益内、能拿到完整真实直链的曲目放行（含 VIP / 无损 / 已购）；
    - 未登录：降级为只播免费曲目（``FNMUSIC_FREE_ONLY_ON_LOGOUT``，默认开）；
    - 任何情况下，url 为空 / code 404 / 带试听片段标记的曲目都不放行。
    """
    if not ids:
        return {}
    api = _get_api()
    try:
        with _api_lock:
            urls_data = api.songs_url(ids)
    except Exception:
        return {}
    if not isinstance(urls_data, list):
        return {}

    logged_in = check_is_logged_in()
    out: dict[int, dict[str, Any]] = {}
    for item in urls_data:
        if not isinstance(item, dict):
            continue
        sid = item.get("id") or item.get("song_id")
        if not sid:
            continue
        try:
            sid_int = int(sid)
        except (ValueError, TypeError):
            continue

        url = item.get("url")
        code = item.get("code")
        fee = item.get("fee", 0)

        # 核心铁律：拿不到真实直链一律不放行，试听片段同样不放行
        if not url or not str(url).strip() or code == 404 or is_trial_snippet(item):
            continue
        # 未登录时降级：只保留免费曲目（fee 0=免费，8=VIP 但未登录必然无 url，已被上面挡掉）
        if not logged_in and FREE_ONLY_ON_LOGOUT and fee not in (0, 8):
            continue

        out[sid_int] = item
    return out


def quality_of(url_info: dict[str, Any]) -> str:
    """按上游 Parse.song_url 的同款逻辑判定音质字符串。

    刻意与 NEMbox 保持一致（LOSSLESS / HIRES / JYMASTER / FLAC / "HD 320k" …），
    这样代理层的无损判定和 UI 展示都拿的是同一套词汇。
    """
    level = str(url_info.get("level") or "").upper()
    stype = str(url_info.get("type") or "").upper()
    try:
        br = int(url_info.get("br") or 0)
    except (TypeError, ValueError):
        br = 0
    if level in ("LOSSLESS", "HIRES", "JYMASTER") and stype:
        return f"{level} {stype}"
    if stype == "FLAC" and level:
        return f"{level} FLAC"
    if stype == "FLAC":
        return "LOSSLESS FLAC"
    if br >= 999000:
        return "LOSSLESS"
    if br >= 320000:
        return f"HD {br // 1000}k"
    if br >= 192000:
        return f"MD {br // 1000}k"
    if br:
        return f"LD {br // 1000}k"
    return "LD 128k"


def _song_info_from_raw(raw: dict[str, Any], url_info: dict[str, Any] | None) -> dict[str, Any]:
    """把网易云原始 song dict 映射成与 CLI song_info 兼容的结构。

    额外带上 album_pic_url / has_sq / has_hr，让代理层不必再为补封面
    多发一次 /api/v1/songs/detail。
    """
    mapped = _map_song_detail(raw)
    info = {
        "song_id": mapped["song_id"],
        "song_name": mapped["name"],
        "artist": mapped["artist"],
        "album_name": mapped["album_name"],
        "album_id": (raw.get("al") or {}).get("id", "") if isinstance(raw.get("al"), dict) else "",
        "album_pic_url": mapped["album_pic_url"],
        "duration": int(round((mapped["duration_ms"] or 0) / 1000)),
        "has_sq": mapped["has_sq"],
        "has_hr": mapped["has_hr"],
        "quality": quality_of(url_info or {}),
        "mp3_url": str((url_info or {}).get("url") or ""),
    }
    return info


def _safe_sid(raw: Any) -> int:
    if not isinstance(raw, dict):
        return 0
    sid = raw.get("id") or raw.get("song_id")
    try:
        return int(sid) if sid is not None else 0
    except (TypeError, ValueError):
        return 0


def search_songs(keyword: str, limit: int = 50) -> list[dict[str, Any]]:
    """进程内搜索，逐首过滤可播性。

    不走 ``musicbox search`` CLI —— 它内部调 dig_info，任何一首取不到直链就会
    让整个结果集变成空列表（HTTP 仍为 200），用户表现为"搜不到任何在线歌曲"。
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    api = _get_api()
    try:
        with _api_lock:
            result = api.search(kw, limit=max(1, min(int(limit or 50), 200)))
    except Exception:
        return []
    if not isinstance(result, dict):
        return []
    raw_songs = result.get("songs")
    if not isinstance(raw_songs, list):
        return []

    ids = [_safe_sid(s) for s in raw_songs]
    ids = [i for i in ids if i]
    if not ids:
        return []

    playable = playable_url_map(ids)
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in raw_songs:
        if not isinstance(raw, dict):
            continue
        sid = _safe_sid(raw)
        if not sid or sid in seen or sid not in playable:
            continue
        seen.add(sid)
        out.append(_song_info_from_raw(raw, playable[sid]))
    return out[: max(1, int(limit or 50))]


def daily_songs(limit: int = 20) -> list[dict[str, Any]]:
    """网易云官方每日推荐，进程内实现 + 逐首过滤。

    同样不走 ``musicbox recommend songs`` CLI：它经 dig_info，一首坏数据就会
    把整份日推清空。这里逐首判定，只有真正拿不到直链的那几首被剔除。
    """
    api = _get_api()
    try:
        with _api_lock:
            raw = api.recommend_playlist(limit=max(1, min(int(limit or 20), 200)))
    except Exception:
        return []
    if not isinstance(raw, list) or not raw:
        return []

    ids = [_safe_sid(s) for s in raw]
    ids = [i for i in ids if i]
    if not ids:
        return []

    playable = playable_url_map(ids)
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for song in raw:
        if not isinstance(song, dict):
            continue
        sid = _safe_sid(song)
        if not sid or sid in seen or sid not in playable:
            continue
        seen.add(sid)
        out.append(_song_info_from_raw(song, playable[sid]))
        if len(out) >= max(1, int(limit or 20)):
            break
    return out


# 上游 api.songs_url 里 weapi 降级用的码率映射，原样照搬以免语义漂移
_LEVEL_RATE_MAP = {
    "exhigh": 320000,
    "higher": 192000,
    "standard": 128000,
    "lossless": 999000,
    "hires": 999000,
    "jymaster": 999000,
}


def _level_to_encode_type(level: str) -> str:
    """上游 ``level_to_encode_type`` 的薄封装。

    抽成模块级函数的唯一目的是让测试可以打桩——直接 ``from NEMbox.api import``
    写在函数体里的话，未安装真实 NetEase-MusicBox 的环境会直接 ImportError，
    这条纯逻辑分支就再也测不到。
    """
    from NEMbox.api import level_to_encode_type

    return level_to_encode_type(level)


def _quality_to_level(quality: str) -> str:
    """上游 ``music_quality_to_level`` 的薄封装，理由同上。"""
    from NEMbox.api import music_quality_to_level

    return music_quality_to_level(quality)


def _urls_for_level(api, ids: list[int], level: str) -> list[Any]:
    """按**指定** level 取直链，逐行对齐上游 ``api.songs_url`` 的实现。

    上游 ``songs_url(ids)`` 的 level 取自全局 ``Config().get("music_quality")``，
    **不接受参数**；而本服务的接口需要按请求音质（proxy 会依次试
    lossless → exhigh）取链。直接改全局 Config 会写坏用户配置文件，
    故在此按同样逻辑显式传 level。
    """
    params = {
        "ids": json.dumps(ids, separators=(",", ":")),
        "level": level,
        "encodeType": _level_to_encode_type(level),
    }
    try:
        data = api.eapi_request("/api/song/enhance/player/url/v1", params).get("data", [])
    except Exception:  # noqa: BLE001 - eapi 不可用时照上游走 weapi 降级
        data = []
    if data:
        return data if isinstance(data, list) else []
    return (api.request("POST", "/weapi/song/enhance/player/url",
                        {"ids": ids, "br": _LEVEL_RATE_MAP.get(level, 320000)}).get("data") or [])


def _pick_by_id(items: Any, song_id: int) -> dict[str, Any] | None:
    """从返回列表里挑出指定 id 的那条；挑不到就退化为返回唯一一条。"""
    if isinstance(items, dict):
        return items or None
    if not isinstance(items, list):
        return None
    for it in items:
        if isinstance(it, dict):
            try:
                if int(it.get("id") or it.get("song_id") or 0) == song_id:
                    return it
            except (TypeError, ValueError):
                continue
    singles = [x for x in items if isinstance(x, dict)]
    return singles[0] if len(singles) == 1 else None


def song_url_info(song_id: int, quality: str = "exhigh") -> dict[str, Any]:
    """进程内取单曲直链信息（含 code / url / br / level / freeTrialPrivilege）。

    返回结构与 CLI ``musicbox song url <id> --json`` 的 ``data`` 字段一致，
    因此代理侧 ``resolve_netease_url`` 无需改动：它只认 ``code == 200 and url``。
    取不到时返回 ``{}``，由调用方决定是否降级到 CLI。
    """
    sid = int(song_id)
    level = _quality_to_level(quality)
    api = _get_api()
    with _api_lock:
        data = _urls_for_level(api, [sid], level)
    return _pick_by_id(data, sid) or {}


def song_raw_detail(song_id: int) -> dict[str, Any]:
    """进程内取单曲**原始**详情（含 ar / al / dt / sq / hr / h）。

    代理的 ``_online_info`` 期望的正是这个原始形状（它自己解析 ar/al/dt/sq/h），
    而不是 ``_map_song_detail`` 映射后的 song_name/album_pic_url 那套。
    """
    sid = int(song_id)
    api = _get_api()
    with _api_lock:
        raw = api.songs_detail([sid])
    return _pick_by_id(raw, sid) or {}


# 逐档降级取"该账号能拿到的最高品质"。顺序即优先级：
# jymaster(臻品母带) > hires(高清无损) > lossless(无损) > exhigh(极高320k)
BEST_QUALITY_CHAIN = ("jymaster", "hires", "lossless", "exhigh")


# 上游返回的歌单封面是 http://（歌曲封面才是 https），必须升级协议：
# 飞牛 UI 跑在 https 下，http 图片会被浏览器按混合内容直接拦掉。
def _https_cover(url: Any) -> str:
    s = str(url or "").strip()
    if s.startswith("http://"):
        return "https://" + s[len("http://"):]
    return s


def _norm_playlist(raw: Any) -> dict[str, Any] | None:
    """把上游原始歌单 dict 归一化成本服务统一形状（容错缺字段）。"""
    if not isinstance(raw, dict):
        return None
    pid = raw.get("id") or raw.get("playlistId")
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    creator = raw.get("creator") if isinstance(raw.get("creator"), dict) else {}
    name = str(raw.get("name") or raw.get("title") or "").strip()
    if not name:
        name = f"歌单 {pid}"
    return {
        "playlist_id": pid,
        "name": name,
        "cover_url": _https_cover(raw.get("coverImgUrl") or raw.get("picUrl")
                                  or creator.get("backgroundUrl") or ""),
        "track_count": _to_int(raw.get("trackCount") or raw.get("track_count") or 0),
        "description": str(raw.get("description") or "")[:300],
        # 自建 vs 收藏：subscribed 为 True 表示是收藏的别人的歌单
        "subscribed": bool(raw.get("subscribed")),
        "creator": str(creator.get("nickname") or ""),
        "creator_id": _to_int(creator.get("userId") or raw.get("userId") or 0),
    }


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _playlist_list(raw: Any) -> list[dict[str, Any]]:
    out = []
    if not isinstance(raw, list):
        return out
    for it in raw:
        n = _norm_playlist(it)
        if n:
            out.append(n)
    return out


def user_playlists_for_uid(uid: int, offset: int = 0, limit: int = 50) -> list[dict[str, Any]]:
    """账户歌单（自建 + 收藏），需登录。

    兼容版本：A 既有 ``user_playlists(limit)`` 返回 {"uid","playlists"} 摘要，
    签名与 G 的 ``user_playlists(uid, offset, limit)`` 不同，故新增本函数承载
    G 的归一化形状，供 /api/v1/playlists/user 使用，不动 A 的既有函数。
    """
    api = _get_api()
    with _api_lock:
        raw = api.user_playlist(int(uid), offset=offset, limit=limit)
    return _playlist_list(raw)


def recommend_playlists() -> list[dict[str, Any]]:
    """网易云按账号口味的推荐歌单列表（recommend_resource），需登录。"""
    api = _get_api()
    with _api_lock:
        raw = api.recommend_resource()
    return _playlist_list(raw)


def toplists() -> list[dict[str, Any]]:
    """排行榜清单：上游返回 [(榜单名, 榜单歌单id)]，无需登录。"""
    api = _get_api()
    with _api_lock:
        raw = api.fetch_toplists()
    out = []
    if isinstance(raw, (list, tuple)):
        for pair in raw:
            try:
                name, pid = pair[0], int(pair[1])
            except (TypeError, ValueError, IndexError):
                continue
            out.append({"playlist_id": pid, "name": str(name), "cover_url": "",
                        "track_count": 0, "description": "", "subscribed": False,
                        "creator": "", "creator_id": 0})
    return out


def category_playlists(category: str = "华语", order: str = "hot",
                       limit: int = 20) -> list[dict[str, Any]]:
    """分类歌单（华语/欧美/场景/情感…），无需登录。"""
    api = _get_api()
    cat = str(category or "华语").strip()
    order_ = "new" if str(order or "hot").strip().lower() == "new" else "hot"
    with _api_lock:
        raw = api.top_playlists(cat, order_, 0, max(1, min(int(limit or 20), 50)))
    return _playlist_list(raw)


def playlist_categories() -> dict[str, list[str]]:
    """歌单分类目录 {大类名: [子类名]}；上游失败时用 NEMbox 内置常量兜底。"""
    api = _get_api()
    with _api_lock:
        raw = api.playlist_catelogs()
    parsed = api._parse_playlist_classes(raw) if isinstance(raw, dict) else {}
    if not parsed:
        try:
            parsed = dict(api._get_playlist_classes() or {})
        except Exception:  # noqa: BLE001
            parsed = {}
    return {str(k): [str(x) for x in v] for k, v in parsed.items() if v}


def new_albums(limit: int = 20) -> list[dict[str, Any]]:
    """新碟上架（专辑维度），无需登录。"""
    api = _get_api()
    with _api_lock:
        raw = api.new_albums(offset=0, limit=max(1, min(int(limit or 20), 50)))
    out = []
    if isinstance(raw, list):
        for a in raw:
            if not isinstance(a, dict):
                continue
            aid = _to_int(a.get("id"))
            if not aid:
                continue
            artist = a.get("artist") if isinstance(a.get("artist"), dict) else {}
            out.append({
                "album_id": aid,
                "name": str(a.get("name") or f"专辑 {aid}"),
                "cover_url": _https_cover(a.get("picUrl") or a.get("blurPicUrl") or ""),
                "artist": str(artist.get("name") or ""),
                "publish_time": _to_int(a.get("publishTime") or 0),
            })
    return out


def personal_fm() -> list[dict[str, Any]]:
    """私人FM / 漫游曲目，需登录。返回已按可播性过滤的 song_info 列表。"""
    api = _get_api()
    with _api_lock:
        raw = api.personal_fm()
    return _songs_from_raw_list(raw)


def playlist_track_ids_limited(playlist_id: int, limit: int = 300) -> list[int]:
    """取歌单内曲目 id（兼容版本，供公共歌单口径使用）。

    A 既有 ``playlist_track_ids(playlist_id)`` 会先判登录（未登录返回 None），
    且不接受 limit；公共歌单（排行榜/分类）未登录也要能取，故新增本函数承载
    G 的语义。上游 ``playlist_songlist`` 返回的 ``trackIds`` 不是纯 id 列表，
    而是 ``[{"id": …, "v": …, "at": …}, …]``，这里同时兼容两种形态。
    """
    api = _get_api()
    with _api_lock:
        raw = api.playlist_songlist(int(playlist_id))
    ids: list[int] = []
    if isinstance(raw, list):
        for it in raw:
            if isinstance(it, dict):
                sid = _to_int(it.get("id"))
            else:
                sid = _to_int(it)
            if sid:
                ids.append(sid)
            if len(ids) >= max(1, min(int(limit or 300), 1000)):
                break
    return ids


def album_songs(album_id: int, limit: int = 200) -> list[dict[str, Any]]:
    """专辑内曲目。上游 ``album()`` 直接返回歌曲 dict 列表。"""
    api = _get_api()
    with _api_lock:
        raw = api.album(int(album_id))
    return _songs_from_raw_list(raw, limit=limit)


def _songs_from_raw_list(raw: Any, limit: int = 300) -> list[dict[str, Any]]:
    """把一批上游原始 song dict 逐首过滤后映射成 song_info。

    与 search_songs / daily_songs 同一套规则：坏数据只影响它自己那一首，
    绝不整表清空。
    """
    if not isinstance(raw, list) or not raw:
        return []
    ids = [_safe_sid(s) for s in raw]
    ids = [i for i in ids if i]
    if not ids:
        return []
    playable = playable_url_map(ids)
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    cap = max(1, int(limit or 300))
    for song in raw:
        if not isinstance(song, dict):
            continue
        sid = _safe_sid(song)
        if not sid or sid in seen or sid not in playable:
            continue
        seen.add(sid)
        out.append(_song_info_from_raw(song, playable[sid]))
        if len(out) >= cap:
            break
    return out


def songs_by_ids(ids: list[int], limit: int = 300) -> list[dict[str, Any]]:
    """按 id 批量取可播曲目（歌单/排行榜内容都走这里）。"""
    clean = [int(i) for i in ids if _to_int(i)]
    if not clean:
        return []
    api = _get_api()
    with _api_lock:
        raw = api.songs_detail(clean[:1000])
    return _songs_from_raw_list(raw, limit=limit)


def best_url_info(song_id: int) -> dict[str, Any]:
    """按 jymaster → hires → lossless → exhigh 逐档试，取该账号能拿到的直链。

    ⚠️ 上游会按账号权益**自动降级**并以 ``code=200`` 返回：所以 ``best_quality``
    **必须取响应里的实际 ``level``**，而不是我们请求的档位。``requested_level``
    保留请求档位，便于对照"想要什么 vs 拿到什么"。一档都取不到返回 ``{}``。
    """
    sid = int(song_id)
    api = _get_api()
    for level in BEST_QUALITY_CHAIN:
        with _api_lock:
            try:
                data = _urls_for_level(api, [sid], level)
            except Exception:  # noqa: BLE001
                data = []
        item = _pick_by_id(data, sid)
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if url and item.get("code") == 200 and not is_trial_snippet(item):
            item = dict(item)
            actual = str(item.get("level") or "").strip().lower()
            item["best_quality"] = actual or level
            item["requested_level"] = level
            # 上游降级了要留痕：这是"账号权益不够"而非"我们没试更高档"
            item["downgraded"] = bool(actual) and actual != level
            return item
    return {}


def song_like(song_id: int, like: bool = True) -> dict[str, Any]:
    """红心/取消红心（收藏同步回网易云），需登录。

    上游是 ``song_like(songid, like=True)`` → eapi ``/api/song/like``。注意：
    ``like=False`` 这条分支在 NEMbox 源码里从未被调用过，属未经验证路径，
    因此这里把上游返回值与异常都如实回传给调用方，不静默吞掉。
    """
    api = _get_api()
    try:
        with _api_lock:
            ok = api.song_like(int(song_id), like=bool(like))
        return {"ok": bool(ok), "song_id": int(song_id), "like": bool(like),
                "requires_login": bool(not ok)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "song_id": int(song_id), "like": bool(like),
                "error": f"{type(exc).__name__}: {exc}"[:200]}
