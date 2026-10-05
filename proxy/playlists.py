"""网易云「更多口径歌单」与「账户歌单」注入飞牛歌单列表。

飞牛的歌单列表来自官方接口 ``/music/api/v1/playlist/list``，本模块在那份列表**头部**
追加若干由网易云内容构成的伪歌单。每日推荐（``online:playlist:daily:``）由
``recommend.py`` 单独负责，本模块不重叠；其余口径都在这里。

guid 规范（都在 ``online:playlist:`` 命名空间下，与曲目 guid ``online:netease:<sid>``
区分开）：

    online:playlist:ne:{playlist_id}     真实网易云歌单（账户歌单/推荐歌单/排行榜/分类歌单）
    online:playlist:nealbum:{album_id}   新碟上架里的专辑（当作一个歌单看待）
    online:playlist:nefm                 私人FM 无限流（做成会滚动更新的伪歌单）

为什么账户歌单、排行榜、分类歌单共用 ``ne:{id}`` 这一种 guid：它们最终都是「网易云
歌单 id → 曲目 id 列表」，取内容的路径完全一致，没必要为每个口径造一套 guid；
口径差别只体现在**注入时给它起的名字与封面**上，这些存在注册表里。

登录要求：账户歌单/推荐歌单/私人FM 需登录（上游语义就是账号绑定的），排行榜/分类
歌单/新碟无需登录。未登录时对应口径**不出现在列表里**，而不是塞一个空歌单——
空歌单点进去没内容，比不出现更容易让人误判成"坏了"。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Awaitable, Callable

try:
    from . import env_merge as _env_merge
except ImportError:  # uvicorn --app-dir proxy（顶层模块形态）
    import env_merge as _env_merge  # type: ignore

logger = logging.getLogger("fnmusic_proxy")

NETEASE_PLAYLIST_PREFIX = "online:playlist:ne:"
NETEASE_ALBUM_PREFIX = "online:playlist:nealbum:"
NETEASE_FM_GUID = "online:playlist:nefm"
CHANNEL_NS = "online:playlist:"
DAILY_NS = "online:playlist:daily:"
# 本地每日推荐（v2.9）。guid 形如 online:playlist:localdaily:{日}:{用户}——
# **每天、每个用户都不一样**，所以凡是要"认出它"的地方都不能做全等比较，
# 必须按前缀匹配，否则跨一天就失配（这正是它排不到第一的根因）。
LOCAL_DAILY_NS = "online:playlist:localdaily:"

# 注入歌单的展示时间戳基准。必须不大于任何真实歌单的时间戳（fnOS 2023 年
# 才发布，正常歌单都是 1.7e9 级；官方自动创建的歌单理论上可能给 0/1 这类
# 占位值），客户端按时间戳升序排列时注入条目才会排在本地歌单之前。
_DISPLAY_TS_BASE = 1

# 口径 -> (是否需要登录, 展示名前缀)
CHANNELS: dict[str, dict[str, Any]] = {
    "daily":      {"needs_login": True,  "label": "每日推荐"},     # 由 recommend.py 负责
    "mine":       {"needs_login": True,  "label": "我的歌单"},     # 自建 + 收藏
    "nrec":       {"needs_login": True,  "label": "推荐歌单"},     # recommend_resource
    "toplist":    {"needs_login": False, "label": "排行榜"},
    "category":   {"needs_login": False, "label": "分类歌单"},
    "newalbum":   {"needs_login": False, "label": "新碟上架"},
    "fm":         {"needs_login": True,  "label": "私人FM"},
}
DEFAULT_CHANNELS = "mine,toplist,category"

# 大类展示顺序（v2.4）：飞牛歌单列表里各口径的先后由它决定，管理页可改。
# 默认值同时是兜底序：未列出的口径按此顺序追加在末尾。
# v2.9 新增 localdaily（本地每日推荐，由 recommend.py 负责内容，这里只管排序）。
DEFAULT_CHANNEL_ORDER = "localdaily,daily,mine,nrec,toplist,category,newalbum,fm"

_PREFIX_BY_CHANNEL = {"mine": "", "nrec": "推荐", "toplist": "榜",
                      "category": "", "newalbum": "新碟", "fm": "电台"}


def _flag(name: str, default: str) -> bool:
    return str(os.environ.get(name, default) or default).strip().lower() in ("1", "true", "yes", "on")


def channel_order() -> tuple[str, ...]:
    """全部口径的展示顺序（含 daily）。

    解析 ``FNMUSIC_NETEASE_CHANNEL_ORDER``（逗号分隔的口径 key）：
    按用户给的顺序排，漏掉的口径按默认序追加在末尾，未知 key 忽略。
    任何解析异常都回落到默认序——顺序配置坏了不能让歌单列表整个消失。
    """
    raw = (os.environ.get("FNMUSIC_NETEASE_CHANNEL_ORDER") or "").strip()
    ordered: list[str] = []
    daily_listed = False
    if raw:
        for part in raw.replace(";", ",").split(","):
            key = part.strip().lower()
            if key == "localdaily":
                daily_listed = True
            if key in CHANNELS and key not in ordered:
                ordered.append(key)
            elif key == "localdaily" and key not in ordered:
                ordered.append(key)   # localdaily 不在 CHANNELS 里（不由本模块取数），但参与排序
    for key in DEFAULT_CHANNEL_ORDER.split(","):
        if key not in ordered:
            ordered.append(key)
    # 配置里没提 localdaily 时，**插到最前面**而不是按默认序补在后面：
    # 本地每日推荐是零外网、秒开的歌单，默认就该是列表第一项；用户在配置里
    # 明确给出位置时（含把它往后排）则以配置为准。
    if not daily_listed and "localdaily" in ordered:
        ordered.remove("localdaily")
        ordered.insert(0, "localdaily")
    return tuple(ordered)


def rank_of(channel: str) -> int:
    try:
        return channel_order().index(channel)
    except ValueError:
        return len(CHANNELS)


# ---------------------------------------------------------------------------
# 手动歌单顺序（v2.5）：管理页逐个拖排，token 列表存 .env
#
# token 形态：``daily``（每日推荐，guid 含日期与用户 id 不能直接写死）或
# ``online:playlist:...`` 完整 guid。空 = 不启用手动顺序，按大类顺序排。
#
# 这个值必须**实时从 .env 读**而不是进程环境变量：用户在管理页排完序点保存，
# 期望下一次刷新飞牛歌单列表就是新顺序，等代理重启太慢。
# ---------------------------------------------------------------------------

_LIVE_ENV_CACHE: "tuple[tuple[int, int], dict[str, str]] | None" = None


def _live_env() -> dict[str, str]:
    """读取 ``${FNMUSIC_HOME}/.env``（带 mtime+size 指纹缓存，文件变了自动重读）。"""
    global _LIVE_ENV_CACHE
    home = os.environ.get("FNMUSIC_HOME") or ""
    path = os.path.join(home, ".env") if home else ""
    try:
        stat = os.stat(path) if path else None
    except OSError:
        stat = None
    fp = (stat.st_mtime_ns, stat.st_size) if stat else ()
    if _LIVE_ENV_CACHE is not None and _LIVE_ENV_CACHE[0] == fp:
        return _LIVE_ENV_CACHE[1]
    kv: dict[str, str] = {}
    if stat:
        try:
            pairs, _others = _env_merge.parse_env_file(path)
            kv = dict(pairs)
        except Exception as exc:  # noqa: BLE001 - .env 坏了就当没有，回落环境变量
            logger.warning("live env read failed (%s): %s", path, exc)
    _LIVE_ENV_CACHE = (fp, kv)
    return kv


def _reset_live_env_cache_for_test() -> None:
    global _LIVE_ENV_CACHE
    _LIVE_ENV_CACHE = None


def explicit_order_tokens() -> tuple[str, ...]:
    """手动顺序 token 列表（保序去重；任何解析问题都安全回落为空）。"""
    kv = _live_env()
    if "FNMUSIC_NETEASE_PLAYLIST_ORDER" in kv:
        raw = kv["FNMUSIC_NETEASE_PLAYLIST_ORDER"]
    else:
        raw = os.environ.get("FNMUSIC_NETEASE_PLAYLIST_ORDER", "")
    out: list[str] = []
    for part in str(raw or "").replace(";", ",").split(","):
        t = part.strip()
        if t and t not in out:
            out.append(t)
    return tuple(out)


def _token_matches(token: str, guid: str) -> bool:
    """token 是否能认领这个 guid。

    ``daily`` / ``localdaily`` 这两个 guid **带日期（localdaily 还带用户）**，
    每天都在变，写死进 token 列表第二天就失配，所以必须走前缀匹配。
    v2.9.8 之前只给 daily 做了特判，localdaily 落到 ``token == guid`` 全等分支上
    永远匹配不上，于是被 apply_explicit_order 当成"没排到的新歌单"甩到列表最后。
    """
    if token == "daily":
        return guid.startswith(DAILY_NS)
    if token == "localdaily":
        return guid.startswith(LOCAL_DAILY_NS)
    return token == guid


def local_daily_pinned_index() -> int:
    """本地每日推荐应被钉住的位置；``None`` = 不干预（按大类/手动顺序自然排）。

    * 用户在 ``FNMUSIC_NETEASE_CHANNEL_ORDER`` 里**显式**给了 localdaily 的位置
      → 尊重配置，返回它的下标（哪怕用户故意把它排到后面）；
    * 配置里没提它（含未配置）→ 返回 0，钉在第一位。
    """
    raw = (os.environ.get("FNMUSIC_NETEASE_CHANNEL_ORDER") or "").strip()
    if raw:
        for idx, part in enumerate(raw.replace(";", ",").split(",")):
            if part.strip().lower() == "localdaily":
                return idx
    return 0


def pin_local_daily_first(items: list[dict]) -> list[dict]:
    """把本地每日推荐搬到它该在的位置（默认第一位），其余相对顺序不变。

    为什么需要这一步：``apply_explicit_order()`` 是对大类顺序的**整体覆盖**，
    凡匹配不到 token 的条目统一归到"未匹配"组排在最后。本地日推的 guid 天天变，
    一旦用户的手动顺序里存的是旧 guid（或压根没 localdaily 这个 token），它就会
    被甩到列表末尾——哪怕大类顺序里它明明排第一。这里做最后一道兜底。
    """
    if not items:
        return items
    target = local_daily_pinned_index()
    cur = next((i for i, it in enumerate(items)
                if str(it.get("guid") or "").startswith(LOCAL_DAILY_NS)), None)
    if cur is None or cur == target:
        return items
    item = items.pop(cur)
    items.insert(min(max(target, 0), len(items)), item)
    return items


def apply_explicit_order_with(items: list[dict], tokens: tuple[str, ...]) -> list[dict]:
    """``apply_explicit_order`` 的可注入版本（测试与 preview 共用排序逻辑）。"""
    if not tokens:
        return items

    def _key(it: dict) -> tuple[int, int]:
        guid = str(it.get("guid") or "")
        for idx, tok in enumerate(tokens):
            if _token_matches(tok, guid):
                return (0, idx)
        return (1, 0)

    return sorted(items, key=_key)


def apply_explicit_order(items: list[dict]) -> list[dict]:
    """按手动顺序重排注入条目；没排到的（新出现的歌单）按原相对顺序跟在后面。

    输入应已按大类顺序排好——手动顺序是对它的**整体覆盖**：凡出现在 token
    列表里的条目按 token 顺序提前，其余保持原有相对顺序排在后面。
    稳定排序保证同 token / 未匹配条目的先后不乱。
    """
    return apply_explicit_order_with(items, explicit_order_tokens())


def channels_enabled() -> tuple[str, ...]:
    """管理页勾选的口径。除 daily 外都在这里生效；daily 由 recommend.py 单独控制。

    输出按 ``channel_order()`` 的**用户自定义顺序**排列（默认即规范顺序）——
    飞牛歌单列表里的顺序由此决定，且必须稳定：不能因为用户先勾了排行榜
    就跑到我的歌单前面去。
    """
    raw = (os.environ.get("FNMUSIC_NETEASE_CHANNELS") or "").strip()
    if not raw:
        # 未配置、或被手工编辑成空串，一律按默认口径。
        # 管理页的 _as_channels 校验器本就不允许保存空值（会提示"至少勾选一个"），
        # 所以空值只可能是手工改出来的；此时回落到默认比让所有歌单凭空消失更合理。
        raw = DEFAULT_CHANNELS
    picked = {str(part).strip().lower() for part in raw.split(",")}
    return tuple(k for k in channel_order() if k != "daily" and k in picked)


def channel_limit() -> int:
    """每个口径最多注入几个歌单。

    排行榜上游有 63 个、分类歌单一次能取 50 个，全塞进飞牛歌单列表会把用户自己的
    本地歌单淹掉，所以必须有上限（默认 8，可调）。
    """
    try:
        return max(1, min(int(os.environ.get("FNMUSIC_NETEASE_CHANNEL_LIMIT", "8") or 8), 50))
    except ValueError:
        return 8


def category_name() -> str:
    return str(os.environ.get("FNMUSIC_NETEASE_CATEGORY", "华语") or "华语").strip()


def playlist_track_limit() -> int:
    try:
        return max(1, min(int(os.environ.get("FNMUSIC_PLAYLIST_TRACK_LIMIT", "300") or 300), 1000))
    except ValueError:
        return 300


def is_channel_guid(guid: str | None) -> bool:
    """是否本模块负责的伪歌单 guid（不含每日推荐）。"""
    g = str(guid or "")
    return g.startswith(NETEASE_PLAYLIST_PREFIX) or g.startswith(NETEASE_ALBUM_PREFIX) \
        or g == NETEASE_FM_GUID


def channel_of(guid: str | None) -> str:
    g = str(guid or "")
    if g.startswith(NETEASE_ALBUM_PREFIX):
        return "newalbum"
    if g == NETEASE_FM_GUID:
        return "fm"
    return "playlist"


def _target_id(guid: str | None) -> str:
    """从 guid 里取出网易云歌单 id / 专辑 id。"""
    g = str(guid or "")
    for pref in (NETEASE_ALBUM_PREFIX, NETEASE_PLAYLIST_PREFIX):
        if g.startswith(pref):
            return str(g[len(pref):]).strip()
    return ""


# ---------------------------------------------------------------------------
# 注册表：guid -> {name, cover_url, track_count, channel}
#
# 必须落盘。飞牛点开歌单封面时只会带 guid 过来，不会带名字与封面；若只存在内存里，
# 服务重启后 /static/cover 与 playlist/detail 就只能显示"网易云歌单 12345"且无封面。
# 而列表刷新可能间隔很久，不能指望届时还在内存中。
# ---------------------------------------------------------------------------


def registry_path() -> str:
    base = os.environ.get("FNMUSIC_PLAYLIST_CACHE_DIR") or os.path.join(
        os.environ.get("FNMUSIC_HOME") or os.path.expanduser("~"), "playlist_cache")
    return os.path.join(base, "registry.json")


_registry_cache: dict[str, dict] | None = None


def load_registry() -> dict[str, dict]:
    global _registry_cache
    if _registry_cache is not None:
        return _registry_cache
    path = registry_path()
    data: dict[str, dict] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            data = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 - 注册表坏了就重建，不能让歌单列表挂掉
        logger.warning("playlist registry load failed (%s): %s: %s",
                       path, type(exc).__name__, exc)
    _registry_cache = data
    return data


def save_registry() -> None:
    global _registry_cache
    reg = load_registry()
    path = registry_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(reg, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("playlist registry save failed: %s: %s", type(exc).__name__, exc)


def remember(record: dict) -> None:
    """把一条伪歌单的名字/封面记进注册表。"""
    guid = str(record.get("guid") or "")
    if not guid:
        return
    reg = load_registry()
    old = reg.get(guid) or {}
    entry = {
        "name": str(record.get("name") or old.get("name") or ""),
        "cover_url": str(record.get("cover_url") or old.get("cover_url") or ""),
        "track_count": int(record.get("track_count") or old.get("track_count") or 0),
        "channel": str(record.get("channel") or old.get("channel") or ""),
        "ts": int(time.time()),
    }
    reg[guid] = entry
    _registry_cache = reg


def lookup(guid: str | None) -> dict:
    reg = load_registry()
    return reg.get(str(guid or "")) or {}


def forget_stale(keep_guids: set[str]) -> int:
    """清掉本轮列表里已经不再出现的条目（例如取消了某个口径、或歌单被删除）。

    每日推荐条目（stamp_display_order 写入、供详情页回显同一份时间戳）按
    ``seen`` 字段清理：超过 14 天没出现在任何列表里就删——它的 guid 含日期
    与用户 id，不清理会一天一条地无限累积。
    """
    reg = load_registry()
    stale_cutoff = int(time.time()) - 14 * 86400
    doomed = []
    for g in reg:
        gs = str(g)
        if not gs.startswith(CHANNEL_NS):
            continue
        if gs.startswith(DAILY_NS):
            try:
                seen = int(reg[g].get("seen") or 0)
            except (TypeError, ValueError):
                seen = 0
            # 没有 seen 的（手工/旧版写入）保守保留；有 seen 且 14 天没出现过的清掉
            if seen and seen < stale_cutoff:
                doomed.append(g)
            continue
        if g not in keep_guids:
            doomed.append(g)
    for g in doomed:
        reg.pop(g, None)
        drop_tracks_cache(g)
    if doomed:
        save_registry()
    return len(doomed)


# ---------------------------------------------------------------------------
# 歌单曲目缓存（v2.6）：stale-while-revalidate + 每日定时刷新
#
# 打开一个伪歌单原先要现场跑完整条上游链路（歌单 trackIds → songs_detail →
# songs_url 逐首过滤 → 补封面），实测要好几秒。本节把解析结果按 guid 落盘：
#   * 命中且在 TTL 内 → 直接返回，零上游往返；
#   * 命中但已过 TTL → **先返回旧值**（打开永远是快的），后台单飞刷新；
#   * 未命中（首次打开）→ 现场拉取并落盘；
#   * 每天在配置的时间后台全量刷新一遍（歌单内容变了第二天自动跟上）。
# ---------------------------------------------------------------------------

_TRACKS_CACHE_TS = "ts"


def tracks_cache_dir() -> str:
    override = str(os.environ.get("FNMUSIC_PLAYLIST_TRACK_CACHE_DIR") or "").strip()
    if override:
        return override
    base = os.environ.get("FNMUSIC_PLAYLIST_CACHE_DIR") or os.path.join(
        os.environ.get("FNMUSIC_HOME") or os.path.expanduser("~"), "playlist_cache")
    return os.path.join(base, "tracks")


def _tracks_cache_path(guid: str | None) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(guid or ""))
    return os.path.join(tracks_cache_dir(), f"{safe}.json")


def tracks_cache_ttl() -> int:
    """缓存视为「新鲜」的时长（秒）；过新鲜期后走 stale-while-revalidate。"""
    try:
        return max(60, int(os.environ.get("FNMUSIC_PLAYLIST_TRACK_CACHE_TTL", "21600") or 21600))
    except (TypeError, ValueError):
        return 21600


def load_cached_tracks(guid: str | None) -> "tuple[float, list] | None":
    path = _tracks_cache_path(guid)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            body = json.load(fh)
        ts = float(body.get(_TRACKS_CACHE_TS) or 0)
        items = body.get("items")
        if ts <= 0 or not isinstance(items, list):
            return None
        return ts, items
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - 缓存坏了就当没有，现场拉取
        logger.warning("tracks cache load failed (%s): %s: %s",
                       path, type(exc).__name__, exc)
        return None


def store_cached_tracks(guid: str | None, items: list) -> bool:
    """落盘曲目缓存。空列表不写——上游一次抖动不该把好缓存覆盖成空的。"""
    if not items:
        return False
    path = _tracks_cache_path(guid)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({_TRACKS_CACHE_TS: time.time(), "items": items},
                      fh, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("tracks cache save failed (%s): %s: %s",
                       path, type(exc).__name__, exc)
        return False


def drop_tracks_cache(guid: str | None) -> None:
    try:
        os.remove(_tracks_cache_path(guid))
    except OSError:
        pass


def registry_channel_guids() -> list[str]:
    """注册表里当前在列的非每日推荐伪歌单 guid。"""
    reg = load_registry()
    return [g for g in reg
            if str(g).startswith(CHANNEL_NS) and not str(g).startswith(DAILY_NS)]


def refresh_time_of_day() -> str:
    """每日定时刷新时间（"HH:MM"）；空串 = 关闭定时刷新。"""
    return str(os.environ.get("FNMUSIC_PLAYLIST_REFRESH_AT", "04:30") or "").strip()


def seconds_until_daily_refresh(now: float | None = None) -> "float | None":
    """距下一次定时刷新还有多少秒；未配置返回 None。

    每天固定时刻触发：今天已过就排到明天同一时刻。解析失败按未配置处理
    （返回 None），绝不能让一个手滑写错的时间把调度循环变成忙轮询。
    """
    spec = refresh_time_of_day()
    if not spec:
        return None
    m = re.match(r"^(\d{1,2}):(\d{1,2})$", spec)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None
    import datetime as _dt

    base = _dt.datetime.fromtimestamp(time.time() if now is None else now)
    target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= base:
        target += _dt.timedelta(days=1)
    return max(1.0, (target - base).total_seconds())


# ---------------------------------------------------------------------------
# 名称/封面归一化
# ---------------------------------------------------------------------------


def _display_name(channel: str, raw_name: str, subscribed: bool = False) -> str:
    """给伪歌单起名。

    账户歌单按用户要求加「网易云·」前缀，与本地歌单区分；收藏来的歌单再标一次，
    免得把别人的歌单当成自己的。前缀必须短——飞牛列表一行放不下长名字。
    """
    name = str(raw_name or "").strip() or "未命名歌单"
    prefix = _PREFIX_BY_CHANNEL.get(channel, "")
    if channel == "mine":
        tag = "网易云·收藏" if subscribed else "网易云·"
        return f"{tag}{name}"
    if prefix:
        return f"{prefix}｜{name}"
    return name


def build_record(guid: str, name: str, cover_url: str, track_count: int, channel: str) -> dict:
    return {
        "guid": guid,
        "name": name,
        "cover_url": _https(cover_url),
        "track_count": int(track_count or 0),
        "channel": channel,
        "createdAt": int(time.time()),
        "updatedAt": int(time.time()),
    }


def _https(url: Any) -> str:
    """歌单/专辑封面上游给的是 http://，必须升级协议。

    飞牛 UI 跑在 https 下，http 图片会被浏览器按混合内容拦掉 —— 表现就是裂图/无封面。
    （歌曲封面 picUrl 上游本来就是 https，只有歌单 coverImgUrl 是 http）
    """
    s = str(url or "").strip()
    if s.startswith("http://"):
        return "https://" + s[len("http://"):]
    return s


# ---------------------------------------------------------------------------
# 拉取各口径的歌单清单
# ---------------------------------------------------------------------------


async def _get_json(client, path: str, params: dict | None = None) -> dict:
    try:
        r = await client.get(path, params=params or {}, timeout=20.0)
        if r.status_code != 200:
            return {}
        body = r.json()
        return body if isinstance(body, dict) else {}
    except Exception as exc:  # noqa: BLE001 - 单个口径失败不能拖垮整个歌单列表
        logger.warning("musicbox %s failed: %s: %s", path, type(exc).__name__, exc)
        return {}


async def fetch_channel_records(client, channel: str, logged_in: bool) -> list[dict]:
    """取某个口径要注入的伪歌单清单。失败/未登录返回空列表（宁可不出现）。"""
    limit = channel_limit()
    spec = CHANNELS.get(channel) or {}
    if spec.get("needs_login") and not logged_in:
        return []

    if channel == "fm":
        # 私人FM 是无限流，做成一个固定的伪歌单；曲目数按上次抓到的量报
        rows = (await _get_json(client, "/api/v1/radio/fm", {"limit": 10})).get("data") or []
        if not isinstance(rows, list) or not rows:
            return []
        rec = build_record(NETEASE_FM_GUID, "私人FM｜网易云电台", "", len(rows), "fm")
        remember(rec)
        return [rec]

    if channel == "newalbum":
        body = await _get_json(client, "/api/v1/playlists/newalbums", {"limit": min(limit, 50)})
        rows = body.get("data") or []
        out = []
        for a in rows if isinstance(rows, list) else []:
            if not isinstance(a, dict):
                continue
            aid = str(a.get("album_id") or "")
            if not aid:
                continue
            artist = str(a.get("artist") or "").strip()
            title = str(a.get("name") or f"专辑 {aid}")
            name = f"新碟｜{title}" + (f" - {artist}" if artist else "")
            rec = build_record(f"{NETEASE_ALBUM_PREFIX}{aid}", name,
                               a.get("cover_url"), 0, "newalbum")
            remember(rec)
            out.append(rec)
            if len(out) >= limit:
                break
        return out

    if channel == "category":
        path, params = ("/api/v1/playlists/category",
                        {"cat": category_name(), "order": "hot", "limit": min(limit, 50)})
        body = await _get_json(client, path, params)
        kind, rows = "category", body.get("data") or []
    elif channel == "toplist":
        body = await _get_json(client, "/api/v1/playlists/toplists")
        kind, rows = "toplist", body.get("data") or []
    elif channel == "mine":
        body = await _get_json(client, "/api/v1/playlists/user", {"limit": 200})
        kind, rows = "mine", body.get("data") or []
        if body.get("error"):
            logger.info("mine playlists unavailable: %s", body.get("error"))
            return []
    elif channel == "nrec":
        body = await _get_json(client, "/api/v1/playlists/recommend")
        kind, rows = "nrec", body.get("data") or []
    else:
        return []

    out = []
    for p in rows if isinstance(rows, list) else []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("playlist_id") or "")
        if not pid:
            continue
        name = _display_name(kind, p.get("name"), bool(p.get("subscribed")))
        if kind == "category":
            name = f"{category_name()}｜{p.get('name')}"
        rec = build_record(f"{NETEASE_PLAYLIST_PREFIX}{pid}", name, p.get("cover_url"),
                           p.get("track_count"), kind)
        remember(rec)
        out.append(rec)
        if len(out) >= limit:
            break
    if out:
        save_registry()
    return out


async def collect_records(client, logged_in: bool) -> tuple[list[dict], set[str], bool]:
    """按勾选顺序汇总全部口径。任一口径异常只影响它自己。

    各口径**并发**拉取（原先逐个 await，歌单列表页要等所有口径的跨洋往返
    串行加完）。NEMbox 侧仍有全局锁，上游调用本身不会真并发，但 HTTP
    与解析开销得以重叠，列表页尾延迟从「各口径之和」降到「最慢一个」。

    返回 ``(清单, guid 集合, complete)``。``complete`` 表示「这份清单可信到足以据此
    清理注册表」：未登录或有口径抛异常时为 False —— 那种情况下清单必然不完整，
    若仍拿它去做 forget_stale，会把暂时没取到的条目（连同名字与封面）一并抹掉，
    用户只是掉线一次就得重新等所有歌单刷新。
    """
    enabled = channels_enabled()
    todo: list[str] = []
    for ch in enabled:
        spec = CHANNELS.get(ch) or {}
        if spec.get("needs_login") and not logged_in:
            # 需登录的口径缺席是**正常**的，不代表清单不可信
            continue
        todo.append(ch)

    async def _fetch_one(channel: str) -> "list[dict] | None":
        try:
            return await fetch_channel_records(client, channel, logged_in)
        except Exception as exc:  # noqa: BLE001
            logger.warning("channel %s failed: %s: %s", channel, type(exc).__name__, exc)
            return None

    results = await asyncio.gather(*[_fetch_one(ch) for ch in todo]) if todo else []

    records: list[dict] = []
    complete = True
    for channel, rows in zip(todo, results):
        if rows is None:
            complete = False
        else:
            records.extend(rows)
    # guid 去重（不同口径可能给出同一个网易云歌单，例如账户歌单同时也在推荐里）
    seen: set[str] = set()
    uniq: list[dict] = []
    for r in records:
        g = str(r.get("guid") or "")
        if not g or g in seen:
            continue
        seen.add(g)
        uniq.append(r)
    if not logged_in:
        complete = False
    return uniq, seen, complete


def stamp_display_order(items: list[dict]) -> list[dict]:
    """给最终注入顺序里的条目盖上**互不相同且单调递增**的 createdAt/updatedAt。

    飞牛客户端对歌单列表按时间戳**升序**排列（v2.4.0 真机验证：注入条目用
    "当前时间递减"的时间戳时，每日推荐（时间戳最大）反而沉到了最底部）。
    因此注入条目必须从一个**远早于任何真实歌单**的基准开始递增：

      基准(1600000000 = 2020-09) + 位置序号

    fnOS 2023 年才发布，本地歌单的 createdAt 不可能早于 2020，所以注入条目
    永远排在本地歌单之前，且顺序 = 注入顺序：位置 0（默认是每日推荐）最小、
    排最前，同口径内部保持上游顺序。客户端无论按 createdAt 还是 updatedAt
    升序排，得到的都是同一顺序。

    就地修改并返回同一列表。注册表里的 ts 同步更新：playlist/detail 与
    batch-detail 回显的 createdAt/updatedAt 取的就是它，两处必须一致，
    否则详情页与列表页的顺序语义打架。
    """
    base = _DISPLAY_TS_BASE
    now = int(time.time())
    reg = load_registry()
    dirty = False
    for i, it in enumerate(items):
        ts = base + i
        it["createdAt"] = ts
        it["updatedAt"] = ts
        guid = str(it.get("guid") or "")
        if not guid:
            continue
        if guid.startswith(DAILY_NS):
            # 每日推荐不在注册表里（归 recommend.py 管），但详情页回显要和
            # 列表页同一份时间戳，这里补一条；seen 记录真实见到时间，供过期清理。
            entry = reg.get(guid)
            if not isinstance(entry, dict):
                entry = {"name": str(it.get("name") or ""), "cover_url": "",
                         "track_count": int(it.get("trackCount") or 0), "channel": "daily"}
                reg[guid] = entry
            if entry.get("ts") != ts:
                entry["ts"] = ts
                dirty = True
            if entry.get("seen") != now:
                entry["seen"] = now
                dirty = True
            continue
        entry = reg.get(guid)
        if isinstance(entry, dict) and entry.get("ts") != ts:
            entry["ts"] = ts
            dirty = True
    if dirty:
        global _registry_cache
        _registry_cache = reg
        save_registry()
    return items


# ---------------------------------------------------------------------------
# 解析歌单内容
# ---------------------------------------------------------------------------


def tracks_path_for(guid: str | None) -> tuple[str, dict] | None:
    """guid -> (musicbox 端点, query)。"""
    g = str(guid or "")
    limit = playlist_track_limit()
    if g == NETEASE_FM_GUID:
        return "/api/v1/radio/fm", {"limit": min(limit, 20)}
    tid = _target_id(g)
    if not tid or not tid.isdigit():
        return None
    if g.startswith(NETEASE_ALBUM_PREFIX):
        return f"/api/v1/album/{tid}/tracks", {"limit": limit}
    return f"/api/v1/playlist/{tid}/tracks", {"limit": limit}


async def resolve_track_items(
    client,
    guid: str | None,
    map_song: Callable[[dict], dict | None],
    enrich: Callable[[Any, list[dict]], Awaitable[None]] | None = None,
) -> list[dict]:
    """把伪歌单解析成扩展内部条目列表（已补封面、已去重）。

    ``map_song`` / ``enrich`` 由调用方注入（app.py 的 ``netease_items.map_netease_song``
    与 ``_enrich_netease_items``），避免本模块与 app 形成循环导入。
    """
    route = tracks_path_for(guid)
    if route is None:
        return []
    path, params = route
    body = await _get_json(client, path, params)
    if body.get("ok") is False:
        logger.info("playlist tracks unavailable guid=%s: %s", guid, body.get("error"))
        return []
    raw_rows = body.get("data") or []
    if not isinstance(raw_rows, list):
        return []

    items: list[dict] = []
    song_ids: list[str] = []
    seen: set[str] = set()
    for raw in raw_rows:
        item = map_song(raw)
        if item is None:
            continue
        sid = str(item.get("id") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        items.append(item)
        song_ids.append(sid)
    if items and enrich is not None:
        # 只补**缺封面**的条目：musicbox 的歌单曲目端点返回的 song_info 已带
        # album_pic_url（map_netease_song 会直读），全都有封面时这次 enrich
        # 纯属浪费——上游要做 songs_detail + songs_url 两次往返，正是
        # 「打开歌单/榜单 5~8 秒」的主要构成之一。
        missing = [it for it in items if not str(it.get("cover_url") or "").strip()]
        if missing:
            await enrich(client, missing)
            items = [it for it in items if str(it.get("id") or "") in set(song_ids)]

    # 实际可播数量回填注册表，让列表上的曲目数是真实值而不是上游的 trackCount
    reg = lookup(guid)
    if items and (not reg or reg.get("track_count") != len(items)):
        remember({"guid": str(guid), "name": reg.get("name", ""),
                  "cover_url": reg.get("cover_url", ""),
                  "track_count": len(items), "channel": reg.get("channel", "")})
        save_registry()
    return items
