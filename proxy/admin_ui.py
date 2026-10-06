"""飞牛桌面内的管理页面：扫码登录网易云 + 全部配置，一个页面搞定。

通过**统一网关**暴露为 `/app/fnmusicext`（由 `fpk/payload/ui/config` 注册，
应用服务监听 `${TRIM_APPDEST}/ui.sock`）。网关会在转发前校验 NAS 登录态，
并注入可信身份 Header：

    X-Trim-Userid / X-Trim-Isadmin / X-Trim-Username

因此本模块**只**信任这三个 Header。缺失即说明请求没有经过网关
（例如有人直接连到 unix socket），一律拒绝——官方明确要求
「不要信任客户端传入的用户 ID」。

登录与改配置都限定管理员：扫码会决定整个 NAS 用哪个网易云账号做音源，
PushPlus token 属于凭据，都不是家庭成员该随手改的东西。

安全约定：
  - PushPlus token 回显时一律打码，只在用户显式提交新值时写入；
  - token 绝不出现在日志、异常信息或任何响应体里；
  - 所有写入值都按白名单/正则校验后再落盘，`.env` 保持 0600 与原子替换。

本地开发（无网关）调试方式见 ADMIN_UI_ALLOW_NO_GATEWAY 的注释。
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

try:
    from . import config_schema
    from . import download
    from . import env_merge
    from . import loghouse
    from . import netease_auth
    from . import pushplus
    from .version import get_version
except ImportError:  # uvicorn --app-dir proxy
    import config_schema  # type: ignore
    import download  # type: ignore
    import env_merge  # type: ignore
    import loghouse  # type: ignore
    import netease_auth  # type: ignore
    import pushplus  # type: ignore
    from version import get_version  # type: ignore

# 校验规则统一由 proxy/config_schema.py 提供（控制台与 A 自带音源页的「扩展设置」共用一份），
# 这里保留原名字，页面代码与既有测试照常引用。
_as_bool = config_schema._as_bool
_int_range = config_schema._int_range
_in_choices = config_schema._in_choices
_http_url = config_schema._http_url
_free_text = config_schema._free_text
_token = config_schema._token
_as_library_dir = config_schema._as_library_dir
_as_path = config_schema._as_path
_as_time_of_day = config_schema._as_time_of_day
_as_channels = config_schema._as_channels
_as_channel_order = config_schema._as_channel_order
_as_playlist_order = config_schema._as_playlist_order

logger = logging.getLogger("fnmusic_proxy.admin_ui")

_STARTED_AT = time.time()

GATEWAY_PREFIX = os.environ.get("FNMUSIC_ADMIN_PREFIX", "/app/fnmusicext")
MUSICBOX_URL = os.environ.get("FNMUSIC_MUSICBOX_URL", "http://127.0.0.1:8770").rstrip("/")
PROXY_SOCK = os.environ.get("FNMUSIC_ADMIN_PROXY_SOCK", "/var/run/trim_music.socket")
ENV_FILE = os.environ.get("FNMUSIC_ADMIN_ENV_FILE", "")
RESTART_SCRIPT = os.environ.get("FNMUSIC_ADMIN_RESTART_SCRIPT", "")
UPSTREAM_SOCK = os.environ.get("FNMUSIC_UPSTREAM_SOCK", "/var/run/trim_music_upstream.socket")

# 仅在无网关的环境（git 克隆安装、本地开发）把管理页放到 TCP 端口上时才需要打开。
# 打开意味着任何能连到该端口的人都拿到管理员权限，因此默认关闭，
# 且必须由运维显式设置为 true，同时自行加防火墙/内网限制。
ALLOW_NO_GATEWAY = (
    os.environ.get("FNMUSIC_ADMIN_ALLOW_NO_GATEWAY", "false").strip().lower()
    in ("true", "1", "yes", "on")
)

MUSICBOX_TIMEOUT_S = 12.0
RESTART_TIMEOUT_S = 90.0
LOG_LINE_LIMIT = 400

# 日志保留策略：单文件超过 LOG_MAX_MB 就轮转，超过 LOG_MAX_DAYS 的文件直接删。
# 由 loghouse 模块执行；管理页面进程每小时跑一次，start.sh 在启动时也跑一次。
LOG_MAX_MB = float(os.environ.get("FNMUSIC_LOG_MAX_MB", "10"))
LOG_MAX_DAYS = float(os.environ.get("FNMUSIC_LOG_MAX_DAYS", "30"))
LOG_SCAN_INTERVAL_S = float(os.environ.get("FNMUSIC_LOG_SCAN_INTERVAL", "3600"))

# 配置项白名单：field -> (env key, 校验器, 是否敏感)
# 只有列在这里的键才允许被页面写入，杜绝任意 .env 注入。
QUALITIES = ("lossless", "exhigh", "higher", "standard")
# 音质策略可选档位比播放音质多两档：hires / jymaster 在 musicbox 的 QUALITY_WHITELIST
# 里合法（账号无对应权益时上游会自动降级，不会因此播不出来）。顺序由高到低。
QUALITY_LEVELS = ("jymaster", "hires", "lossless", "exhigh", "higher", "standard")
# v2.9.28：音质策略只剩「局域网 / 非局域网」两档；跟随飞牛、按网络分别设置、
# 固定音质三个策略与固定档一并取消（页面不再提供入口，对应键已列入废弃）。
QUALITY_POLICY = "by_lan"
QUALITY_LEVEL_LABELS = (
    ("standard", "标准 standard（128k）"),
    ("higher", "较高 higher（192k）"),
    ("exhigh", "极高 exhigh（320k）"),
    ("lossless", "无损 lossless（FLAC）"),
    ("hires", "高清无损 hires（Hi-Res）"),
    ("jymaster", "臻品母带 jymaster"),
)
TEMPLATES = ("markdown", "html", "txt", "json")


# 可在网页上勾选的歌单口径。「每日推荐」不在此列——它有独立开关 daily_enabled，
# 重复放一个勾只会让人以为两个开关各管一半。
CHANNEL_KEYS = ("mine", "nrec", "toplist", "category", "newalbum", "fm")


# 大类顺序里额外允许 daily / localdaily（它们不在勾选框里，由各自独立开关控制）
_ORDER_KEYS = ("daily", "localdaily") + CHANNEL_KEYS


CONFIG_FIELDS: dict[str, tuple[str, Any, bool]] = {
    "netease_quality": ("FNMUSIC_NETEASE_QUALITY", _in_choices(*QUALITIES), False),
    "free_only_on_logout": ("FNMUSIC_FREE_ONLY_ON_LOGOUT", _as_bool, False),
    "daily_enabled": ("FNMUSIC_DAILY_ENABLED", _as_bool, False),
    "daily_limit": ("FNMUSIC_DAILY_LIMIT", _int_range(1, 100), False),
    # --- 本地每日推荐（v2.9）：每天从本地曲库随机抽 N 首 ---
    "local_daily_enabled": ("FNMUSIC_LOCAL_DAILY_ENABLED", _as_bool, False),
    "local_daily_limit": ("FNMUSIC_LOCAL_DAILY_LIMIT", _int_range(1, 500), False),
    # 自动探测靠 music.db；飞牛各版本目录布局不统一，猜不中就整个功能静默失效，
    # 所以必须留一个手动指定的入口（留空 = 自动探测）。
    "library_dir": ("FNMUSIC_LIBRARY_DIR", _as_library_dir, False),
    # --- 本地曲库优先（v2.8 引入 / v2.9.14 修好）：播网易云歌单时优先读本地同名文件 ---
    "local_first": ("FNMUSIC_LOCAL_FIRST", _as_bool, False),
    "local_first_any_class": ("FNMUSIC_LOCAL_FIRST_ANY_CLASS", _as_bool, False),
    # --- 下一首预热（v2.9.14 T1）：提前取回下一首的直链与元数据 ---
    "prefetch_next": ("FNMUSIC_PREFETCH_NEXT", _as_bool, False),
    "prefetch_lookahead": ("FNMUSIC_PREFETCH_LOOKAHEAD", _int_range(1, 5), False),
    "pushplus_enabled": ("FNMUSIC_PUSHPLUS_ENABLED", _as_bool, False),
    "pushplus_token": ("FNMUSIC_PUSHPLUS_TOKEN", _token, True),
    "pushplus_topic": ("FNMUSIC_PUSHPLUS_TOPIC", _free_text(64), True),
    "pushplus_template": ("FNMUSIC_PUSHPLUS_TEMPLATE", _in_choices(*TEMPLATES), False),
    "pushplus_url": ("FNMUSIC_PUSHPLUS_URL", _http_url, False),
    "netease_search_limit": ("FNMUSIC_NETEASE_SEARCH_LIMIT", _int_range(1, 100), False),
    "online_limit": ("FNMUSIC_ONLINE_LIMIT", _int_range(1, 100), False),
    "search_cache_ttl_days": ("FNMUSIC_SEARCH_CACHE_TTL", _int_range(0, 365), False),
    "login_check_interval_h": ("FNMUSIC_LOGIN_CHECK_INTERVAL", _int_range(0, 168), False),
    "log_max_mb": ("FNMUSIC_LOG_MAX_MB", _int_range(0, 1024), False),
    "log_max_days": ("FNMUSIC_LOG_MAX_DAYS", _int_range(0, 3650), False),
    # v2.9.29 日志降噪：不记封面/心跳/轮询这类高频无信息量的访问行。
    # 关掉可看到完整原始日志（排障时用），代价是 proxy.log 涨得快、截断更频繁。
    "log_quiet": ("FNMUSIC_LOG_QUIET", _as_bool, False),
    # --- 更多口径歌单 / 账户歌单 ---
    "netease_channels": ("FNMUSIC_NETEASE_CHANNELS", _as_channels, False),
    "netease_channel_limit": ("FNMUSIC_NETEASE_CHANNEL_LIMIT", _int_range(1, 50), False),
    "netease_category": ("FNMUSIC_NETEASE_CATEGORY", _free_text(32), False),
    "netease_channel_order": ("FNMUSIC_NETEASE_CHANNEL_ORDER", _as_channel_order, False),
    "netease_playlist_order": ("FNMUSIC_NETEASE_PLAYLIST_ORDER", _as_playlist_order, False),
    "playlist_track_limit": ("FNMUSIC_PLAYLIST_TRACK_LIMIT", _int_range(1, 1000), False),
    # --- 歌单曲目缓存（v2.6）：打开秒开 ---
    "playlist_cache_ttl_h": ("FNMUSIC_PLAYLIST_TRACK_CACHE_TTL", _int_range(1, 168), False),
    "playlist_refresh_at": ("FNMUSIC_PLAYLIST_REFRESH_AT", _as_time_of_day, False),
    # --- 收藏归档与红心同步 ---
    "download_dir": ("FNMUSIC_DOWNLOAD_DIR", _as_path, True),
    "download_on_favorite": ("FNMUSIC_DOWNLOAD_ON_FAVORITE", _as_bool, False),
    "fav_sync_like": ("FNMUSIC_FAV_SYNC_LIKE", _as_bool, False),
    # --- 洛雪音乐同步服务器歌单（只读注入飞牛歌单列表）---
    "lx_sync_enabled": ("FNMUSIC_LX_SYNC_ENABLED", _as_bool, False),
    "lx_sync_url": ("FNMUSIC_LX_SYNC_URL", _http_url, False),
    "lx_sync_password": ("FNMUSIC_LX_SYNC_PASSWORD", _free_text(200), True),
    "lx_sync_refresh_s": ("FNMUSIC_LX_SYNC_REFRESH_S", _int_range(30, 86400), False),
    "lx_sync_device": ("FNMUSIC_LX_SYNC_DEVICE", _free_text(64), False),
    "lx_sync_insecure_tls": ("FNMUSIC_LX_SYNC_INSECURE_TLS", _as_bool, False),
    "lx_sync_writeback": ("FNMUSIC_LX_SYNC_WRITEBACK", _in_choices("off", "tracks", "all"), False),
    # --- 音质：局域网一档 / 非局域网一档（v2.9.28 起只有这两档）---
    # 「跟随飞牛」「按网络分别设置」「固定音质」三个策略与固定档一并取消：
    # 前两个依赖我们从未可靠拿到的客户端网络线索，第三个则把窄管道照灌母带。
    "quality_wifi": ("FNMUSIC_QUALITY_WIFI", _in_choices(*QUALITY_LEVELS), False),
    "quality_cellular": ("FNMUSIC_QUALITY_CELLULAR", _in_choices(*QUALITY_LEVELS), False),
}

# 页面上以「天/小时」为单位展示，落盘时换算成秒
UNIT_SECONDS = {
    "search_cache_ttl_days": 86400,
    "login_check_interval_h": 3600,
    "playlist_cache_ttl_h": 3600,
}

DEFAULTS = {
    "netease_quality": "lossless",
    "free_only_on_logout": "true",
    "daily_enabled": "true",
    "daily_limit": "20",
    "local_daily_enabled": "true",
    "local_daily_limit": "50",
    "library_dir": "",
    "local_first": "true",
    "local_first_any_class": "true",
    "prefetch_next": "true",
    "prefetch_lookahead": "3",
    "pushplus_enabled": "true",
    "pushplus_token": "",
    "pushplus_topic": "",
    "pushplus_template": "markdown",
    "pushplus_url": pushplus.DEFAULT_URL,
    "netease_search_limit": "50",
    "online_limit": "30",
    "search_cache_ttl_days": "7",
    "login_check_interval_h": "1",
    "log_max_mb": "10",
    "log_max_days": "30",
    "log_quiet": "true",
    "netease_channels": "mine,toplist,category",
    "netease_channel_limit": "8",
    "netease_category": "华语",
    # ⚠️ 必须与 fpk/payload/bin/setup.sh 的默认顺序保持一致：v2.9.0 这里漏了
    # localdaily，用户只要保存一次配置，大类顺序里就没有本地每日推荐了。
    "netease_channel_order": "localdaily,daily,mine,nrec,toplist,category,newalbum,fm",
    "netease_playlist_order": "",
    "playlist_track_limit": "300",
    "playlist_cache_ttl_h": "6",
    "playlist_refresh_at": "04:30",
    "download_dir": "",
    "download_on_favorite": "false",
    "fav_sync_like": "false",
    "lx_sync_enabled": "false",
    "lx_sync_url": "",
    "lx_sync_password": "",
    "lx_sync_refresh_s": "300",
    "lx_sync_device": "fnmusic-ext",
    "lx_sync_insecure_tls": "false",
    "lx_sync_writeback": "off",
    "quality_wifi": "lossless",
    "quality_cellular": "exhigh",
}

MASK = "••••••••"


# ---------------------------------------------------------------- 身份鉴权 ----


class GatewayIdentity:
    __slots__ = ("uid", "is_admin", "username")

    def __init__(self, uid: str, is_admin: bool, username: str):
        self.uid = uid
        self.is_admin = is_admin
        self.username = username

    def as_dict(self) -> dict:
        return {"uid": self.uid, "is_admin": self.is_admin, "username": self.username}


def identify(request: Request) -> GatewayIdentity | None:
    """从网关注入的 Header 取身份。缺失或非法一律返回 None。"""
    uid = str(request.headers.get("x-trim-userid") or "").strip()
    username = str(request.headers.get("x-trim-username") or "").strip()
    is_admin_raw = str(request.headers.get("x-trim-isadmin") or "").strip().lower()
    is_admin = is_admin_raw in ("true", "1", "yes")

    if not uid and not username:
        return None
    if not re.fullmatch(r"[\w.\-@]{1,64}", uid or ""):
        logger.warning("拒绝可疑的 X-Trim-Userid: %r", uid[:80])
        return None
    return GatewayIdentity(uid=uid, is_admin=is_admin, username=username)


def _deny(reason: str, *, code: int = 403) -> JSONResponse:
    return JSONResponse(status_code=code, content={"ok": False, "error": reason})


def _forbidden_page() -> HTMLResponse:
    body = (
        "<!doctype html><meta charset=utf-8>"
        "<title>无权访问</title>"
        "<body style=\"font-family:system-ui;margin:3rem auto;max-width:34rem;color:#222\">"
        "<h2>需要通过飞牛桌面打开</h2>"
        "<p>本页面依赖飞牛统一网关校验 NAS 登录态。</p>"
        "<p>请在飞牛桌面点击「飞牛音乐扩展」图标打开；"
        "直接访问端口或套接字不会带身份 Header，因此被拒绝。</p>"
        "</body>"
    )
    return HTMLResponse(content=body, status_code=403)


# ------------------------------------------------------------------ 配置 IO ----


def _env_file() -> str:
    return ENV_FILE or os.path.join(
        os.environ.get("FNMUSIC_HOME") or os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
        ".env",
    )


def _raw_env() -> dict[str, str]:
    path = _env_file()
    if not os.path.exists(path):
        return {}
    try:
        kv, _others = env_merge.parse_env_file(path)
        return dict(kv)
    except Exception as exc:  # noqa: BLE001 - 配置损坏不能让页面 500
        logger.warning("读取 .env 失败: %s", exc)
        return {}


def _to_editable(env: dict[str, str]) -> dict[str, str]:
    """内部编辑视图：**保留敏感项真实值**，只做单位换算。

    绝不能把它直接返回给前端——对客户端只允许返回 _to_display()。
    """
    out: dict[str, str] = {}
    for field, (key, _check, _secret) in CONFIG_FIELDS.items():
        raw = str(env.get(key, DEFAULTS.get(field, "")))
        if field in UNIT_SECONDS and raw.strip():
            try:
                raw = str(max(0, round(int(float(raw)) / UNIT_SECONDS[field])))
            except (TypeError, ValueError):
                raw = DEFAULTS.get(field, "")
        out[field] = raw
    return out


def _to_display(env: dict[str, str]) -> dict[str, str]:
    """对客户端的脱敏视图：敏感项一律打码。"""
    out = _to_editable(env)
    for field, (_key, _check, secret) in CONFIG_FIELDS.items():
        if secret and out.get(field):
            out[field] = MASK
    return out


def read_config_masked() -> dict[str, Any]:
    env = _raw_env()
    return {
        "values": _to_display(env),
        "env_file": _env_file(),
        "has_token": bool(str(env.get("FNMUSIC_PUSHPLUS_TOKEN", "")).strip()),
        "writable": os.access(os.path.dirname(_env_file()) or ".", os.W_OK),
    }


def _from_display(values: dict[str, str]) -> dict[str, str]:
    """把页面值换算回 .env 里的实际形式。"""
    out: dict[str, str] = {}
    for field, value in values.items():
        if field in UNIT_SECONDS:
            try:
                out[field] = str(int(value) * UNIT_SECONDS[field])
            except (TypeError, ValueError):
                out[field] = value
        else:
            out[field] = value
    return out


def apply_config(submitted: dict[str, Any]) -> tuple[dict[str, str] | None, str]:
    """校验并写入配置。返回 (新的脱敏视图, 错误信息)。

    token 留空表示「保持现有值不变」——password 字段不回显真实值，
    若照原样写回就会把用户之前填的 token 冲掉。
    """
    env = _raw_env()
    # base 必须取【真实值】视图：若这里用了打码视图，任何一次保存都会把
    # pushplus_token / topic 写成 ••••••••，把用户凭据冲掉。
    base = _to_editable(env)
    merged_display: dict[str, str] = {}
    errors: list[str] = []

    for field, (key, check, secret) in CONFIG_FIELDS.items():
        if field not in submitted:
            # 页面没提交这一项 → 原样保留真实值
            merged_display[field] = base.get(field, DEFAULTS.get(field, ""))
            continue
        raw_new = submitted.get(field)
        if secret and str(raw_new).strip() in ("", MASK):
            # 用户没改这一项 → 沿用真实原值（跳过校验，原值当初已通过校验）
            merged_display[field] = base.get(field, "")
            continue
        try:
            merged_display[field] = check(raw_new)
        except ValueError as exc:
            errors.append(f"{field}: {exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{field}: 取值非法（{exc}）")

    if errors:
        return None, "；".join(errors)

    desired_pairs = _from_display(merged_display)
    kv, _other = env_merge.parse_env_file(_env_file()) if os.path.exists(_env_file()) else ([], [])

    # 已有键按本次提交更新；缺失键补齐；其余用户自定义键原样保留
    explicit = {CONFIG_FIELDS[f][0] for f in desired_pairs}
    desired_kv = [(CONFIG_FIELDS[f][0], v) for f, v in desired_pairs.items()]
    if not kv:
        kv = [(k, DEFAULTS.get(f, "")) for f, (k, _c, _s) in CONFIG_FIELDS.items()
              if f not in desired_pairs]
    out_kv, summary = env_merge.merge_env(kv, desired_kv, explicit)
    out_kv, removed = env_merge.drop_obsolete(out_kv)
    # A 侧：显式传入本仓库的 NEW_DEFAULTS/NEW_PREFIXES（env_merge 不传参时仍只补 FNMUSIC_LX_*）
    out_kv, added = env_merge.ensure_prefix_defaults(
        out_kv, env_merge.NEW_DEFAULTS, prefixes=env_merge.NEW_PREFIXES
    )
    summary["added"].extend(added)

    path = _env_file()
    try:
        backup = f"{path}.bak"
        if os.path.exists(path):
            with open(path, "rb") as src, open(backup, "wb") as dst:
                dst.write(src.read())
            try:
                os.chmod(backup, 0o600)
            except OSError:
                pass
        env_merge.write_env_atomic(
            path,
            env_merge.render_env(out_kv, "generated by admin ui — do not commit"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("写入 .env 失败: %s", exc)
        return None, f"写入配置失败：{exc}"

    changed = sorted(summary.get("updated", [])) + sorted(summary.get("added", []))
    if removed:
        changed += [f"-{k}" for k in sorted(removed)]
    logger.info("配置已更新（%d 项变更，敏感值不落日志）", len(changed))
    return read_config_masked(), ""


# ------------------------------------------------------------------- 应用 ----

def _log_dir() -> str:
    d = os.environ.get("FNMUSIC_ADMIN_LOG_DIR") or ""
    if d:
        return d
    base = os.environ.get("FNMUSIC_ADMIN_VAR_DIR") or ""
    return os.path.join(base, "logs") if base else ""


def _num_setting(env_field: str, os_var: str, default: float) -> float:
    """读数值型设置：优先 .env（页面改完立即生效，无需重启），
    其次进程环境变量，最后默认值。"""
    raw = str(_raw_env().get(env_field, "") or "").strip()
    if not raw:
        raw = str(os.environ.get(os_var, "") or "").strip()
    try:
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def log_max_mb() -> float:
    return _num_setting("FNMUSIC_LOG_MAX_MB", "FNMUSIC_LOG_MAX_MB", LOG_MAX_MB)


def log_max_days() -> float:
    return _num_setting("FNMUSIC_LOG_MAX_DAYS", "FNMUSIC_LOG_MAX_DAYS", LOG_MAX_DAYS)


async def _log_janitor(stop_event: asyncio.Event) -> None:
    """日志清理巡检：默认每小时一次，也可由页面手动触发。"""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=LOG_SCAN_INTERVAL_S)
            return
        except asyncio.TimeoutError:
            pass
        try:
            loghouse.scan(_log_dir(), max_mb=log_max_mb(), max_days=log_max_days())
        except Exception as exc:  # noqa: BLE001 - 清理失败绝不影响服务
            logger.warning("日志清理巡检失败: %s", exc)


@asynccontextmanager
async def _lifespan(fastapi_app: FastAPI):
    log_dir = _log_dir()
    # 启动即清一次：上一次运行攒下的超大/过期日志不该继续占盘
    if log_dir:
        try:
            rep = loghouse.scan(log_dir, max_mb=log_max_mb(), max_days=log_max_days())
            if rep.get("actions"):
                logger.info("启动日志清理：%s",
                            json.dumps(rep["actions"], ensure_ascii=False)[:400])
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动日志清理失败: %s", exc)

    stop_event = asyncio.Event()
    task = asyncio.create_task(_log_janitor(stop_event))
    try:
        yield
    finally:
        stop_event.set()
        if not task.done():
            task.cancel()


app = FastAPI(title="fnmusic-ext 管理页面", lifespan=_lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


# 已知端点，按最长优先匹配。用于把「任意层数反代前缀 + 端点」归一化，
# 这样用户经组网工具、Nginx 反代、宝塔等再套一层前缀时页面依然可用。
KNOWN_ENDPOINTS = (
    "/api/login/qr.png",
    "/api/login/qr",
    "/api/login/check",
    "/api/login/state",
    "/api/health",
    "/api/diag",
    "/api/config",
    "/api/logs",
)

# /app/fnmusicext -> fnmusicext。用于识别「反代后仍以应用名结尾」的页面请求。
APP_SLUG = GATEWAY_PREFIX.rstrip("/").rsplit("/", 1)[-1]


def normalize_path(path: str) -> tuple[str, str]:
    """把外部路径归一化成服务内部路径。

    返回 ``(内部路径, base 前缀)``；base 前缀恒以 ``/`` 结尾（根时为 ``"/"``），
    由页面注入给前端，前端一律拼绝对 URL，不再依赖浏览器的相对路径解析。

    为什么不能靠相对路径：浏览器在 ``/app/fnmusicext``（无尾斜杠）下解析
    ``api/health`` 会得到 ``/app/api/health``，网关前缀被吃掉一段，
    请求根本到不了本服务，只会得到网关的 404。
    """
    prefix = GATEWAY_PREFIX.rstrip("/")

    # 1. 已经是内部路径
    if path in KNOWN_ENDPOINTS or path in ("/", "/index.html", "/favicon.ico"):
        return path, "/"

    # 2. 标准网关前缀
    if prefix and (path == prefix or path.startswith(prefix + "/")):
        rest = path[len(prefix):] or "/"
        base = prefix + "/"
        return (rest if rest.startswith("/") else "/" + rest), base

    # 3. 反代又套了一层（组网隧道 / Nginx / 宝塔等）：按已知端点做后缀匹配，
    #    把端点前面的整段当作 base。要求 path 比端点长，即前面确实有前缀。
    #    注意不要再检查前一字符是否为 "/" —— 端点自身就以 "/" 开头，
    #    前一字符是前缀的最后一个字符（如 fnmusicext 的 t）。
    for endpoint in KNOWN_ENDPOINTS:
        if len(path) > len(endpoint) and path.endswith(endpoint):
            return endpoint, path[: len(path) - len(endpoint)] + "/"

    # 4. 反代下的页面请求：路径以应用名（或 /app）结尾，视为首页
    stripped = path.rstrip("/")
    if stripped and (
        stripped.rsplit("/", 1)[-1] == APP_SLUG or stripped.endswith("/app")
    ):
        return "/", (stripped + "/") if stripped != "/" else "/"

    # 5. 认不出来就原样交给路由，让它自然 404
    return path, "/"


class PrefixStripMiddleware(BaseHTTPMiddleware):
    """路径归一化，并把 base 前缀透给下游，供页面注入给前端。"""

    async def dispatch(self, request: Request, call_next):
        raw = request.scope.get("path", "") or "/"
        new_path, base = normalize_path(raw)
        if new_path != raw:
            request.scope["path"] = new_path
            encoded = request.scope.get("raw_path")
            if isinstance(encoded, bytes):
                request.scope["raw_path"] = new_path.encode("utf-8")
        request.scope["fn_base"] = base
        request.scope["fn_original_path"] = raw
        return await call_next(request)


app.add_middleware(PrefixStripMiddleware)

_MB_CLIENT: httpx.AsyncClient | None = None


def mb_client() -> httpx.AsyncClient:
    global _MB_CLIENT
    if _MB_CLIENT is None:
        _MB_CLIENT = httpx.AsyncClient(base_url=MUSICBOX_URL, timeout=MUSICBOX_TIMEOUT_S)
    return _MB_CLIENT


def _err(code: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=code, content={"ok": False, "error": msg})


def _require(request: Request) -> GatewayIdentity:
    """返回管理员身份；不满足时抛 _AuthError。"""
    ident = identify(request)
    if ident is None:
        if ALLOW_NO_GATEWAY:
            return GatewayIdentity(uid="0", is_admin=True, username="local-dev")
        raise _AuthError("缺少飞牛网关身份 Header，请从飞牛桌面打开本页面")
    if not ident.is_admin:
        raise _AuthError("需要管理员权限")
    return ident


class _AuthError(Exception):
    def __init__(self, msg: str):
        self.msg = msg


@app.middleware("http")
async def _auth_error_middleware(request: Request, call_next):
    try:
        return await call_next(request)
    except _AuthError as exc:
        if request.scope.get("path", "/") in ("/", ""):
            return _forbidden_page()
        return _deny(exc.msg)


async def _probe_proxy_quality() -> dict:
    """向代理进程取「音质策略 + 跟随飞牛的发现证据」。

    观察记录只存在于代理进程内存里（记录客户端请求线索的中间件在那边），管理页面是
    **另一个进程**，直接 import 读到的永远是空 —— 必须经 unix socket 取。
    取不到就如实说明，**不要显示成"没有偏好"**，那会让人误以为已经读到了飞牛设置。
    """
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=PROXY_SOCK),
            base_url="http://unix",
            timeout=6.0,
        ) as client:
            r = await client.get("/_ext/quality")
            if r.status_code == 200:
                body = r.json()
                if isinstance(body, dict) and body.get("ok") is not False:
                    data = body.get("data")
                    if isinstance(data, dict):
                        return {"reachable": True, **data}
            return {"reachable": False, "status": r.status_code}
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"[:160]}


async def _probe_proxy_local_daily() -> dict:
    """向代理进程取「本地每日推荐」排障快照。

    曲库目录、扫描结果这些只存在于代理进程的一侧（它才持有 music.db 解析结果），
    管理页面是另一个进程，必须经 unix socket 取。
    """
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=PROXY_SOCK),
            base_url="http://unix",
            timeout=15.0,   # 大曲库扫描需要时间
        ) as client:
            r = await client.get("/_ext/localdaily")
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200 and isinstance(body, dict) and body.get("ok") is not False:
                return {"reachable": True, **(body.get("data") if isinstance(body.get("data"), dict) else {})}
            return {"reachable": False, "status": r.status_code,
                    "error": str(body.get("error") or "")[:160]}
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"[:160]}


async def _probe_proxy_path(path: str, timeout: float = 10.0) -> dict:
    """向代理进程取任意 /_ext 快照。

    观察记录只存在于代理进程内存里（索引、预热统计都在那边），管理页面是
    **另一个进程**，直接 import 读到的永远是空 —— 必须经 unix socket 取。
    """
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=PROXY_SOCK),
            base_url="http://unix",
            timeout=timeout,
        ) as client:
            r = await client.get(path)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200 and isinstance(body, dict) and body.get("ok") is not False:
                return {"reachable": True, **(body.get("data") if isinstance(body.get("data"), dict) else {})}
            return {"reachable": False, "status": r.status_code,
                    "error": str(body.get("error") or "")[:160]}
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"[:160]}


async def _probe_proxy_local_first() -> dict:
    """本地曲库优先的状态（索引构成 + 最近匹配结果）。"""
    return await _probe_proxy_path("/_ext/localfirst", timeout=15.0)


async def _probe_proxy_prefetch() -> dict:
    """下一首预热的统计（命中率 + 冷/热 gather 平均耗时）。"""
    return await _probe_proxy_path("/_ext/prefetch", timeout=10.0)


async def _probe_proxy_hls() -> dict:
    """官方 HLS 实时转码耗时（区分「转码启动慢」与「播出后跟不上」）。"""
    return await _probe_proxy_path("/_ext/hls", timeout=10.0)


async def _probe_proxy_playstart() -> dict:
    """play-start 记账：实际播了几首、折叠掉多少续传。"""
    return await _probe_proxy_path("/_ext/playstart", timeout=10.0)


async def _probe_proxy_failures() -> dict:
    """最近几次「播不出来」的留证（v2.9.27）。"""
    return await _probe_proxy_path("/_ext/failures", timeout=10.0)


async def _probe_proxy_streammode() -> dict:
    """音频走了哪条路：302 直连 / NAS 中转落缓存 / 本地文件（v2.9.28）。"""
    return await _probe_proxy_path("/_ext/streammode", timeout=10.0)


async def _probe_proxy_authorized() -> dict:
    """向代理进程取「飞牛应用授权目录」状态快照。

    这是判断「本地曲库到底有没有被合规授权」的唯一权威来源：网关 socket 只有
    代理进程那侧查得到，管理页面是另一个进程。
    """
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=PROXY_SOCK),
            base_url="http://unix",
            timeout=10.0,
        ) as client:
            r = await client.get("/_ext/authorized", params={"refresh": "1"})
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200 and isinstance(body, dict) and body.get("ok") is not False:
                return {"reachable": True, **(body.get("data") if isinstance(body.get("data"), dict) else {})}
            return {"reachable": False, "status": r.status_code,
                    "error": str(body.get("error") or "")[:160]}
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"[:160]}


async def _probe_proxy_health() -> dict:
    """透过被接管的 socket 读扩展自身的 healthz。"""
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=PROXY_SOCK),
            base_url="http://unix",
            timeout=5.0,
        ) as client:
            r = await client.get("/_ext/healthz")
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    return data
    except Exception as exc:  # noqa: BLE001
        logger.debug("proxy healthz probe failed: %s", exc)
    return {"ok": False, "upstream": "unknown", "musicbox": "unknown"}


async def _probe_musicbox(path: str = "/healthz", timeout: float = 4.0) -> dict:
    """探测音源服务的某个路径，把失败原因与响应体一起如实带回来。

    detail 会原样回显给浏览器，因此一律先过 _scrub() 脱敏：异常文本可能带上
    请求 URL 或上游返回内容，那里面理论上可能混进凭据。

    成功时顺带返回已解析的 ``body``，调用方直接复用，**不要再为拿 body 重复请求一次**
    ——CLI 支撑的端点在无外网环境下光 DNS 超时就能耗掉数秒，重复探测会把诊断页拖死。
    """
    t0 = time.time()
    try:
        r = await mb_client().get(path, timeout=timeout)
        ms = int((time.time() - t0) * 1000)
        if r.status_code == 200:
            body: Any = None
            try:
                body = r.json()
            except Exception:  # noqa: BLE001
                body = None
            return {"reachable": True, "status": 200, "ms": ms, "body": body}
        return {"reachable": True, "status": r.status_code, "ms": ms,
                "detail": _scrub(r.text[:200])}
    except Exception as exc:  # noqa: BLE001
        ms = int((time.time() - t0) * 1000)
        return {"reachable": False, "status": 0, "ms": ms,
                "detail": _scrub(f"{type(exc).__name__}: {exc}"[:200])}


def _selftest_data(probe: dict) -> dict:
    """从 selftest 探测结果里取出 data 段；取不到就是空 dict。"""
    body = probe.get("body") if isinstance(probe, dict) else None
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return {}


async def _probe_musicbox_all() -> dict:
    """并发探测音源服务的四个端点。

    串行探测会把各端点的超时累加（CLI 支撑的端点尤其慢，无外网时更明显），
    诊断页因此可能卡十几秒。并发后只受最慢那一个的超时约束。
    """
    down = {"reachable": False, "status": 0, "ms": 0, "detail": "probe error"}
    results = await asyncio.gather(
        _probe_musicbox("/healthz"),
        _probe_musicbox("/api/v1/auth/status"),
        _probe_musicbox("/api/v1/auth/detail"),
        _probe_musicbox("/api/v1/selftest", timeout=10.0),
        return_exceptions=True,
    )
    keys = ("healthz", "auth_status", "auth_detail", "selftest")
    return {
        k: (down if isinstance(v, Exception) else v)
        for k, v in zip(keys, results)
    }


@app.get("/api/diag")
async def api_diag(request: Request):
    """一次性把排障需要的信息全给出来，省掉 SSH。

    页面任何一处失败都可以点「诊断」，把这里的内容连同日志一起贴出来。
    不含任何凭据：token 只报「是否已配置 / 长度」，绝不报值。
    """
    try:
        ident = _require(request)
    except _AuthError as exc:
        return _deny(exc.msg)

    env = _raw_env()
    tok = str(env.get("FNMUSIC_PUSHPLUS_TOKEN", "")).strip()
    log_dir = os.environ.get("FNMUSIC_ADMIN_LOG_DIR") or ""

    logdir_info = []
    if log_dir and os.path.isdir(log_dir):
        for name in sorted(os.listdir(log_dir)):
            fp = os.path.join(log_dir, name)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            if not os.path.isfile(fp):
                continue
            logdir_info.append({
                "name": name,
                "bytes": st.st_size,
                "size_mb": round(st.st_size / 1048576, 2),
                "mtime": int(st.st_mtime),
                "age_days": round((time.time() - st.st_mtime) / 86400, 2),
            })

    env_file = _env_file()
    env_stat = None
    if os.path.exists(env_file):
        st = os.stat(env_file)
        env_stat = {"exists": True, "bytes": st.st_size,
                    "mode": oct(st.st_mode & 0o777)}
    else:
        env_stat = {"exists": False}

    probes = await _probe_musicbox_all()
    mb = probes["healthz"]
    mb_auth = probes["auth_detail"]
    mb_auth_status = probes["auth_status"]
    mb_selftest = probes["selftest"]
    selftest_data = _selftest_data(mb_selftest)
    login = await netease_auth.fetch_state(mb_client(), force=True)
    quality_probe = await _probe_proxy_quality()

    proxy_socket = {
        "path": PROXY_SOCK,
        "exists": os.path.exists(PROXY_SOCK),
        "is_socket": os.path.exists(PROXY_SOCK) and not os.path.isdir(PROXY_SOCK),
    }
    try:
        import stat as _stat
        proxy_socket["mode"] = oct(_stat.S_IMODE(os.stat(PROXY_SOCK).st_mode))
    except OSError:
        proxy_socket["mode"] = None
    proxy_socket["upstream_exists"] = os.path.exists(UPSTREAM_SOCK)

    # 看门狗与主动停机标志（v2.7）：诊断「应用异常退出」类问题的第一现场。
    # 看门狗活着 = 接管丢失/进程死亡会在一个轮询周期内自愈；
    # stopped.flag 存在 = 应用处于主动停机状态（用户在应用中心点过停止）。
    var_dir = os.environ.get("FNMUSIC_ADMIN_VAR_DIR", "")
    watchdog = {"var_dir": var_dir, "pid": None, "alive": False,
                "stopped_flag": False, "restart_marker": False}
    if var_dir:
        wd_pid_file = os.path.join(var_dir, "watchdog.pid")
        try:
            with open(wd_pid_file, encoding="utf-8") as f:
                wd_pid = int((f.read().strip().splitlines() or ["0"])[0] or 0)
            watchdog["pid"] = wd_pid
            if wd_pid > 0:
                os.kill(wd_pid, 0)
                watchdog["alive"] = True
        except (OSError, ValueError):
            pass
        watchdog["stopped_flag"] = os.path.exists(os.path.join(var_dir, "stopped.flag"))
        watchdog["restart_marker"] = os.path.exists(os.path.join(var_dir, "restart.inprogress"))

    ui_socket = os.environ.get("FNMUSIC_ADMIN_UI_SOCK", "")
    return {
        "ok": True,
        "version": get_version(),
        "request": {
            "original_path": request.scope.get("fn_original_path"),
            "normalized_path": request.scope.get("path"),
            "base_prefix": request.scope.get("fn_base"),
            "gateway_prefix_config": GATEWAY_PREFIX,
            "identity": ident.as_dict(),
            "seen_headers": sorted(
                k for k in request.headers.keys()
                if k.lower().startswith("x-trim") or k.lower() in ("host", "x-forwarded-prefix")
            ),
            "x_forwarded_prefix": request.headers.get("x-forwarded-prefix"),
        },
        "proxy_socket": proxy_socket,
        "watchdog": watchdog,
        "quality": quality_probe,
        "local_daily": await _probe_proxy_local_daily(),
        "authorized": await _probe_proxy_authorized(),
        "local_first": await _probe_proxy_local_first(),
        "prefetch": await _probe_proxy_prefetch(),
        "hls": await _probe_proxy_hls(),
        "playstart": await _probe_proxy_playstart(),
        "failures": await _probe_proxy_failures(),
        "streammode": await _probe_proxy_streammode(),
        "musicbox": {
            "url": MUSICBOX_URL,
            "healthz": mb,
            "auth_detail": mb_auth,
            "auth_status": mb_auth_status,
            "selftest": mb_selftest,
            "login": login.to_public_dict(),
            "login_error": login.error,
        },
        "selftest": selftest_data,
        "env_file": {**env_stat, "path": env_file,
                     "writable": os.access(os.path.dirname(env_file) or ".", os.W_OK),
                     "keys": len(env),
                     "pushplus_token_configured": bool(tok),
                     "pushplus_token_length": len(tok)},
        "pushplus": {"send_enabled": pushplus.enabled(), "send_url": pushplus.push_url(),
                     "template": pushplus.template(), "topic_set": bool(pushplus.topic())},
        "logs": {"dir": log_dir, "dir_exists": bool(log_dir and os.path.isdir(log_dir)),
                 "max_mb": log_max_mb(), "max_days": log_max_days(),
                 "scan_interval_s": LOG_SCAN_INTERVAL_S, "files": logdir_info,
                 # v2.9.29：日志降噪是否开启（UI 进程自己读，代理那边另有 install_log_quiet）
                 "quiet": (os.environ.get("FNMUSIC_LOG_QUIET") or "true").strip().lower()
                          in ("true", "1", "yes", "on")},
        "restart_script": {"path": RESTART_SCRIPT,
                           "exists": bool(RESTART_SCRIPT and os.path.exists(RESTART_SCRIPT))},
        "runtime": {"python": sys.version.split()[0], "pid": os.getpid(),
                    "uptime_s": int(time.time() - _STARTED_AT)},
    }


@app.get("/api/health")
async def api_health(request: Request):
    try:
        _require(request)
    except _AuthError as exc:
        return _deny(exc.msg)

    proxy_health, login = await asyncio.gather(
        _probe_proxy_health(),
        netease_auth.fetch_state(mb_client(), force=True),
        return_exceptions=True,
    )
    if isinstance(proxy_health, Exception):
        proxy_health = {"ok": False}
    if isinstance(login, Exception):
        login = netease_auth.LoginState(error="probe_failed")

    env = _raw_env()

    # 逐项给出人类可读的降级原因：只要有一项不健康，页面就要把它显式摊开，
    # 而不是只回一个 ok:true 让用户对着"音源服务 unknown"猜。
    problems: list[str] = []
    if proxy_health.get("upstream") != "ok":
        problems.append(
            f"官方后端不可达（upstream={proxy_health.get('upstream', '?')}）。"
            f"请确认飞牛音乐已启动，socket={PROXY_SOCK}"
        )
    probes = await _probe_musicbox_all()
    mb_health = probes["healthz"]
    if not mb_health.get("reachable"):
        problems.append(
            f"音源服务不可达（{MUSICBOX_URL}）：{mb_health.get('detail') or '连接失败'}"
        )
    elif mb_health.get("status") != 200:
        problems.append(
            f"音源服务返回 {mb_health.get('status')}：{mb_health.get('detail') or ''}"[:300]
        )
    if not os.path.exists(UPSTREAM_SOCK):
        problems.append(
            f"未检测到官方 socket 备份 {UPSTREAM_SOCK}，代理可能尚未完成接管"
        )
    if not login.logged_in:
        if netease_auth.free_only_on_logout():
            problems.append("网易云未登录：当前只能播放免费曲目，且不会有「每日推荐」")
        else:
            problems.append("网易云未登录且已关闭免费曲降级：在线播放完全不可用")
    mb_detail = probes["auth_detail"]
    # /api/v1/auth/status 走 musicbox CLI 子进程，与走进程内 NEMbox API 的 auth/detail
    # 是两条不同链路。只有同时探这两条才能暴露「CLI 解析不到 → 全线 502」，
    # 因为 /healthz 与 auth/detail 都会返回 200，看起来一切正常。
    mb_status_probe = probes["auth_status"]
    if mb_status_probe.get("status") == 502:
        problems.append(
            "音源服务的 musicbox CLI 子进程调用失败（502）。这会让搜索/取直链/扫码登录全部不可用，"
            f"而 healthz 仍显示正常。详情：{mb_status_probe.get('detail') or '无'}"
            "；请在诊断里看 selftest 的 resolved_by 与 venv_bin_dir。"
        )
    elif not mb_status_probe.get("reachable"):
        problems.append(f"音源服务登录态接口不可达：{mb_status_probe.get('detail') or '连接失败'}")
    if login.error and login.error not in ("", "invalidated"):
        problems.append(f"网易云登录态探测异常：{login.error}")

    selftest = probes["selftest"]
    st_data = _selftest_data(selftest)
    if st_data and st_data.get("cli_found") is False:
        problems.append(
            "自检确认：musicbox CLI 未找到（"
            + str(st_data.get("resolved_by", ""))[:200] + "）"
        )

    return {
        "ok": True,
        "healthy": not problems,
        "problems": problems,
        "version": get_version(),
        "proxy": proxy_health,
        "musicbox_probe": {"healthz": mb_health, "auth_detail": mb_detail,
                           "auth_status": mb_status_probe, "selftest": selftest},
        "selftest": st_data,
        "netease": login.to_public_dict(),
        "netease_error": login.error,
        "socket_takeover": bool(os.path.exists(UPSTREAM_SOCK)),
        "pushplus_enabled": pushplus.enabled(),
        "pushplus_configured": bool(str(env.get("FNMUSIC_PUSHPLUS_TOKEN", "")).strip()),
        "checked_at": int(time.time()),
    }


@app.get("/api/login/state")
async def api_login_state(request: Request):
    try:
        _require(request)
    except _AuthError as exc:
        return _deny(exc.msg)
    state = await netease_auth.fetch_state(mb_client(), force=True)
    return {"ok": True, "data": state.to_public_dict(), "error": state.error}


@app.post("/api/login/qr")
async def api_login_qr(request: Request):
    """发起一次扫码登录，返回 unikey 与二维码 PNG 的取图地址。

    二维码本身走独立 GET（img src 直接引用），但用同一个 unikey，
    保证「展示的码」与「轮询的码」是同一个。
    """
    try:
        _require(request)
    except _AuthError as exc:
        return _deny(exc.msg)

    try:
        r = await mb_client().post("/api/v1/auth/login", timeout=MUSICBOX_TIMEOUT_S)
        if r.status_code != 200:
            return _err(502, f"音源服务返回 {r.status_code}")
        payload = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("发起扫码失败: %s", exc)
        return _err(502, "音源服务不可达，请确认扩展已启动")

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}
    unikey = str(data.get("unikey") or data.get("codekey") or "")
    if not unikey:
        return _err(502, "音源服务未返回 unikey，无法生成二维码")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{8,128}", unikey):
        logger.warning("unikey 形状异常，已拒绝")
        return _err(502, "音源服务返回的 unikey 形状异常")

    return {"ok": True, "unikey": unikey, "qr_png": f"api/login/qr.png?unikey={unikey}"}


@app.get("/api/login/qr.png")
async def api_login_qr_png(request: Request, unikey: str = ""):
    try:
        _require(request)
    except _AuthError as exc:
        return _deny(exc.msg)
    unikey = (unikey or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{8,128}", unikey):
        return _err(400, "unikey 非法")
    try:
        r = await mb_client().get(
            "/api/v1/auth/login/qr.png", params={"unikey": unikey}, timeout=MUSICBOX_TIMEOUT_S
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("取二维码失败: %s", exc)
        return _err(502, "音源服务不可达")
    if r.status_code != 200:
        return _err(r.status_code, f"音源服务返回 {r.status_code}")
    return Response(content=r.content, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/login/check")
async def api_login_check(request: Request, unikey: str = ""):
    """轮询扫码结果。透传 musicbox 的 code：
    801 等待扫码 / 802 已扫码待手机确认 / 803 成功 / 800 已过期。"""
    try:
        _require(request)
    except _AuthError as exc:
        return _deny(exc.msg)
    unikey = (unikey or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{8,128}", unikey):
        return _err(400, "unikey 非法")

    try:
        r = await mb_client().get(
            "/api/v1/auth/login/check", params={"unikey": unikey}, timeout=MUSICBOX_TIMEOUT_S
        )
        payload = r.json() if r.status_code == 200 else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("轮询登录状态失败: %s", exc)
        return _err(502, "音源服务不可达")

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = {}
    code = 0
    try:
        code = int(data.get("code") or 0)
    except (TypeError, ValueError):
        code = 0

    result: dict[str, Any] = {"ok": True, "code": code}
    if code == 803:
        netease_auth.invalidate_state()
        state = await netease_auth.fetch_state(mb_client(), force=True)
        result["logged_in"] = state.logged_in
        result["nickname"] = state.nickname
        result["vip"] = state.vip_active
        # 代理是另一个进程，它自己的搜索缓存（可能全是登录前的空结果）和登录态
        # 不会因为这里扫码成功而失效，必须显式通知它清一次，否则用户登录后
        # 几分钟内搜到的仍然只有本地歌曲。
        result["proxy_cache_cleared"] = await _invalidate_proxy_cache()
        try:
            await pushplus.send(
                None, "网页端扫码登录成功",
                f"账号：{state.nickname or state.user_id}"
                + (f"，VIP 剩余 {state.vip_days_left} 天" if state.vip_days_left is not None else ""),
            )
        except Exception:  # noqa: BLE001
            pass
    return result


async def _invalidate_proxy_cache() -> bool:
    """通知代理进程清缓存（搜索 + 每日推荐 + 登录态）。失败只记日志。"""
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=PROXY_SOCK),
            base_url="http://unix",
            timeout=6.0,
        ) as client:
            r = await client.post("/_ext/cache/invalidate")
            if r.status_code == 200:
                logger.info("已通知代理清空缓存：%s", str(r.json())[:200])
                return True
            logger.warning("代理清缓存返回 %s", r.status_code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("通知代理清缓存失败（登录后可能有几分钟延迟）: %s", exc)
    return False


@app.get("/api/config")
async def api_config_get(request: Request):
    try:
        _require(request)
    except _AuthError as exc:
        return _deny(exc.msg)
    cfg = read_config_masked()
    cfg["ok"] = True
    cfg["schema"] = {
        field: {"sensitive": secret, "default": DEFAULTS.get(field, "")}
        for field, (_k, _c, secret) in CONFIG_FIELDS.items()
    }
    return cfg


@app.post("/api/config")
async def api_config_post(request: Request):
    try:
        _require(request)
    except _AuthError as exc:
        return _deny(exc.msg)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _err(400, "请求体不是合法 JSON")
    if not isinstance(body, dict):
        return _err(400, "请求体必须是 JSON 对象")

    values = body.get("values")
    if not isinstance(values, dict):
        return _err(400, "缺少 values 字段")
    unknown = [k for k in values if k not in CONFIG_FIELDS]
    if unknown:
        return _err(400, f"不支持的配置项：{', '.join(sorted(unknown))}")

    cfg, error = apply_config(values)
    if cfg is None:
        return _err(422, error)

    restart = bool(body.get("restart", True))
    result: dict[str, Any] = {"ok": True, "config": cfg, "restarted": False}
    if restart:
        ok, msg = await restart_services()
        result["restarted"] = ok
        result["restart_message"] = msg
        if not ok:
            result["warning"] = "配置已保存，但重启未成功，请在飞牛应用中心手动重启本应用"
    return result


@app.get("/api/playlists")
async def api_playlists(request: Request):
    """当前注入飞牛的歌单清单（真实名称 + 当前顺序），供「歌单顺序」卡片。

    数据来自代理进程的 ``/_ext/playlists/preview``：那份清单与飞牛歌单列表
    实际注入**同源同序**（大类顺序 → 手动顺序覆盖），所以网页上看到的顺序
    就是飞牛里的顺序。代理不可达时如实报错，不编造清单。
    """
    try:
        _require(request)
    except _AuthError as exc:
        return _deny(exc.msg)
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=PROXY_SOCK),
            base_url="http://unix",
            timeout=45.0,  # 首次要打网易云拉各口径清单，给足余量
        ) as client:
            r = await client.get("/_ext/playlists/preview")
    except Exception as exc:  # noqa: BLE001
        return _err(502, f"读取歌单清单失败（代理未运行？）：{type(exc).__name__}: {exc}")
    if r.status_code != 200:
        return _err(502, f"代理返回 HTTP {r.status_code}")
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        return _err(502, "代理返回的不是 JSON")
    if not body.get("ok"):
        return _err(502, f"代理预览失败：{str(body.get('error') or '')[:160]}")
    data = body.get("data") or {}
    cfg = read_config_masked()
    saved = str((cfg.get("values") or {}).get("netease_playlist_order") or "")
    data["saved_order"] = saved
    data["saved_order_tokens"] = [t for t in saved.split(",") if t.strip()]
    return {"ok": True, **data}


@app.get("/api/authorized")
async def api_authorized(request: Request):
    """飞牛「应用授权目录」状态，供管理页「授权目录」卡片。

    数据源是代理进程的 /_ext/authorized（网关 socket 只有那侧查得到）。
    """
    try:
        _require(request)
    except _AuthError as exc:
        return _deny(exc.msg)
    force = request.query_params.get("refresh") in ("1", "true", "yes")
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=PROXY_SOCK),
            base_url="http://unix",
            timeout=15.0,
        ) as client:
            r = await client.get("/_ext/authorized",
                                 params={"refresh": "1" if force else "0"})
    except Exception as exc:  # noqa: BLE001
        return _err(502, f"读取授权状态失败（代理未运行？）：{type(exc).__name__}: {exc}")
    if r.status_code != 200:
        return _err(502, f"代理返回 HTTP {r.status_code}")
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        return _err(502, "代理返回的不是 JSON")
    if not body.get("ok"):
        return _err(502, f"代理查询失败：{str(body.get('error') or '')[:160]}")
    return {"ok": True, **(body.get("data") or {})}


@app.post("/api/playlists/warm")
async def api_playlists_warm(request: Request):
    """「预热歌单缓存」按钮：让代理立即后台刷新全部歌单曲目缓存。"""
    try:
        _require(request)
    except _AuthError as exc:
        return _deny(exc.msg)
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=PROXY_SOCK),
            base_url="http://unix",
            timeout=10.0,
        ) as client:
            r = await client.post("/_ext/playlists/warm")
    except Exception as exc:  # noqa: BLE001
        return _err(502, f"调用代理失败（未运行？）：{type(exc).__name__}: {exc}")
    if r.status_code != 200:
        return _err(502, f"代理返回 HTTP {r.status_code}")
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        return _err(502, "代理返回的不是 JSON")
    if not body.get("ok"):
        return _err(502, f"预热触发失败：{str(body.get('error') or '')[:160]}")
    data = body.get("data") or {}
    if data.get("started"):
        return {"ok": True, "message": f"预热已开始（{data.get('total') or '?'} 个歌单，后台进行中）"}
    return {"ok": True, "message": "预热已在进行中，请稍候"}


async def restart_services() -> tuple[bool, str]:
    """调用生命周期脚本重启代理与音源服务（不含本页面的 ui 进程）。"""
    script = RESTART_SCRIPT
    if not script or not os.path.exists(script):
        return False, f"未找到重启脚本（{script or '未配置'}）"
    log_dir = os.path.join(os.path.dirname(script), "..", "logs")
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "FNMUSIC_ADMIN_LOG_DIR": os.path.abspath(log_dir)},
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=RESTART_TIMEOUT_S)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return False, f"重启超时（>{int(RESTART_TIMEOUT_S)}s），请到应用中心手动重启"
        text = (out or b"").decode("utf-8", "replace")[-1500:]
        if proc.returncode == 0:
            return True, "已重启，新配置生效"
        logger.warning("restart_services 失败 rc=%s: %s", proc.returncode, text[:400])
        return False, text.strip() or f"重启脚本返回 {proc.returncode}"
    except Exception as exc:  # noqa: BLE001
        logger.error("调用重启脚本异常: %s", exc)
        return False, f"调用重启脚本异常：{exc}"


def _secret_values() -> set[str]:
    """收集所有需要在日志里抹掉的凭据值。

    必须同时覆盖两处：
      1. 进程环境变量里的 token（代理/推送进程实际在用的）；
      2. `.env` 文件里的 token —— 用户在页面刚保存的新值还没被任何进程加载，
         只读环境变量的话，这条新 token 就会原样出现在日志回显里。
    另外把网关身份 Header 里的用户名一并纳入，避免日志侧信道泄露。
    """
    out = set()
    tok_env = pushplus.token()
    if len(tok_env) >= 6:
        out.add(tok_env)
    tok_file = str(_raw_env().get("FNMUSIC_PUSHPLUS_TOKEN", "")).strip()
    if len(tok_file) >= 6:
        out.add(tok_file)
    return out


def _scrub(text: str) -> str:
    for secret in _secret_values():
        if secret and secret in text:
            text = text.replace(secret, "***")
    return text


@app.get("/api/logs")
async def api_logs(request: Request, what: str = "info", lines: int = 80):
    """回看最近日志，省掉 SSH。只读白名单文件，且做路径穿越防护。"""
    try:
        _require(request)
    except _AuthError as exc:
        return _deny(exc.msg)

    allowed = {"info", "proxy", "musicbox", "setup", "restore", "ui", "config"}
    if what not in allowed:
        return _err(400, f"what 必须是 {'/'.join(sorted(allowed))} 之一")
    try:
        n = max(1, min(int(lines), LOG_LINE_LIMIT))
    except (TypeError, ValueError):
        n = 80

    log_dir = _log_dir()
    if not log_dir or not os.path.isdir(log_dir):
        return {"ok": True, "what": what, "lines": [],
                "note": f"日志目录未配置或不存在：{log_dir or '(未配置 FNMUSIC_ADMIN_LOG_DIR)'}",
                "policy": {"dir": log_dir, "max_mb": log_max_mb(), "max_days": log_max_days()}}

    path = os.path.realpath(os.path.join(log_dir, f"{what}.log"))
    if not path.startswith(os.path.realpath(log_dir) + os.sep):
        return _err(400, "非法日志路径")
    if not os.path.exists(path):
        return {"ok": True, "what": what, "lines": [], "note": "尚无日志"}

    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            tail = f.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return _err(500, f"读取日志失败：{exc}")

    rows = [ln for ln in tail.splitlines() if ln.strip()][-n:]
    # 防御性脱敏：日志理论上不该有凭据，但不能假设上游一定干净，
    # 尤其是"用户刚在页面保存、尚未被任何进程加载"的新 token。
    secrets = _secret_values()
    if secrets:
        rows = [_scrub(ln) for ln in rows]
    return {"ok": True, "what": what, "path": path, "lines": rows,
            "size_bytes": os.path.getsize(path) if os.path.exists(path) else 0,
            "policy": {"dir": log_dir, "max_mb": log_max_mb(), "max_days": log_max_days()}}


@app.post("/api/logs/rotate")
async def api_logs_rotate(request: Request):
    """手动触发一次日志清理（页面「立即清理」按钮）。返回逐个文件的处理动作。"""
    try:
        _require(request)
    except _AuthError as exc:
        return _deny(exc.msg)
    try:
        report = loghouse.scan(_log_dir(), max_mb=log_max_mb(), max_days=log_max_days())
    except Exception as exc:  # noqa: BLE001
        logger.error("手动日志清理失败: %s", exc)
        return _err(500, f"日志清理失败：{exc}")
    report["ok"] = not report.get("errors")
    if report.get("errors"):
        report["error"] = "；".join(report["errors"][:5])
    return report


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    try:
        ident = _require(request)
    except _AuthError:
        return _forbidden_page()
    # base 由服务端算出：它是唯一知道自己被什么前缀访问的一方
    base = str(request.scope.get("fn_base") or "/")
    return HTMLResponse(content=_render_page(ident, base))


def _render_page(ident: GatewayIdentity, base: str = "/") -> str:
    if not base.endswith("/"):
        base += "/"
    return (
        PAGE_HTML
        .replace("__BASE__", html.escape(base, quote=True))
        .replace("__USERNAME__", html.escape(ident.username or ident.uid or "unknown"))
    )


@app.get("/favicon.ico")
async def favicon():
    """浏览器必然会请求 favicon；给个 204 免得日志里出现无意义的 404 干扰排查。"""
    return Response(status_code=204)


# ------------------------------------------------------------------ 前端 ----
# 单文件、零外部依赖：NAS 可能处于离线/内网环境，不允许引任何 CDN。
PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>飞牛音乐扩展</title>
<style>
:root{
  --bg:#f5f6f8;--card:#fff;--fg:#1d2129;--mut:#6b7280;--line:#e5e7eb;
  --ok:#0a7d43;--okbg:#e8f6ee;--warn:#a15c00;--warnbg:#fdf3e3;--err:#b42318;--errbg:#fdecea;
  --acc:#4b3fd4;--accbg:#eeeaff;
}
@media(prefers-color-scheme:dark){:root{
  --bg:#14161a;--card:#1d2026;--fg:#e6e8eb;--mut:#9aa2ad;--line:#2c313a;
  --ok:#4ade80;--okbg:#12291c;--warn:#fbbf24;--warnbg:#2b2313;--err:#f87171;--errbg:#2d1717;
  --acc:#a99cff;--accbg:#221f3d;
}}
*{box-sizing:border-box}
body{margin:0;padding:20px;background:var(--bg);color:var(--fg);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:880px;margin:0 auto}
h1{font-size:18px;margin:0 0 2px}
.sub{color:var(--mut);font-size:12px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:14px}
.card h2{font-size:14px;margin:0 0 14px;padding-bottom:10px;border-bottom:1px solid var(--line)}
.row{display:flex;flex-wrap:wrap;gap:10px 22px}
.kv{min-width:200px;flex:1}
.kv .k{color:var(--mut);font-size:12px}
.kv .v{font-weight:600;font-size:14px;word-break:break-all}
.pill{display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;font-weight:600}
.pill.ok{background:var(--okbg);color:var(--ok)}
.pill.warn{background:var(--warnbg);color:var(--warn)}
.pill.err{background:var(--errbg);color:var(--err)}
label{display:block;margin-bottom:12px}
label .chbox{display:flex;flex-wrap:wrap;gap:6px 14px;margin:4px 0}.ck{display:inline-flex;align-items:center;gap:5px;font-weight:400;font-size:13px}.ck input{width:auto;margin:0}.lb{font-size:13px;font-weight:600;margin-bottom:4px}
label .ht{color:var(--mut);font-size:12px;margin-top:3px}
input,select{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;
  background:var(--bg);color:var(--fg);font:inherit;font-size:13px}
input:focus,select:focus{outline:2px solid var(--accbg);border-color:var(--acc)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:0 20px}
.sw{display:flex;align-items:center;gap:10px;padding:9px 0}
.sw input{width:auto;flex:0 0 auto;transform:scale(1.25)}
.sw .t{flex:1}
.sw .t .lb{font-size:13px;font-weight:600}
.sw .t .ht{color:var(--mut);font-size:12px}
.btn{padding:9px 18px;border-radius:8px;border:1px solid var(--line);background:var(--card);
  color:var(--fg);font:inherit;font-size:13px;font-weight:600;cursor:pointer}
.btn:hover{border-color:var(--acc);color:var(--acc)}
.btn.pri{background:var(--acc);border-color:var(--acc);color:#fff}
.btn.pri:hover{opacity:.9;color:#fff}
.btn:disabled{opacity:.5;cursor:not-allowed}
.acts{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:6px}
.plrow{display:flex;align-items:center;gap:10px;padding:7px 10px;border:1px solid var(--line);
  border-radius:8px;margin-bottom:6px;background:var(--card)}
.plrow .plpos{color:var(--mut);font-size:12px;min-width:22px;text-align:right;font-weight:600}
.plrow .plname{font-size:13px;font-weight:600;flex:1;word-break:break-all}
.plrow .plbtns{display:flex;gap:4px}
.plrow .plbtns .btn{padding:3px 10px;font-size:13px;line-height:1.4}
.msg{margin-top:12px;padding:10px 12px;border-radius:8px;font-size:13px;display:none;white-space:pre-wrap}
.msg.ok{display:block;background:var(--okbg);color:var(--ok)}
.msg.err{display:block;background:var(--errbg);color:var(--err)}
.msg.info{display:block;background:var(--accbg);color:var(--acc)}
.qr{display:flex;gap:20px;align-items:flex-start;flex-wrap:wrap;margin-top:14px}
.qr img{width:212px;height:212px;border:1px solid var(--line);border-radius:10px;background:#fff;padding:6px}
.qr .st{flex:1;min-width:220px}
.steps{color:var(--mut);font-size:13px;margin:0;padding-left:18px}
.steps li{margin-bottom:5px}
pre.log{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;
  font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;max-height:340px;overflow:auto;
  white-space:pre-wrap;word-break:break-all;margin:10px 0 0}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.tabs button{padding:5px 12px;border-radius:999px;border:1px solid var(--line);
  background:var(--card);color:var(--mut);font:inherit;font-size:12px;cursor:pointer}
.tabs button.on{background:var(--accbg);border-color:var(--acc);color:var(--acc);font-weight:600}
.spin{display:inline-block;width:12px;height:12px;border:2px solid currentColor;
  border-right-color:transparent;border-radius:50%;animation:r .7s linear infinite;vertical-align:-1px}
@keyframes r{to{transform:rotate(360deg)}}
.hide{display:none}
</style>
</head>
<body>
<div class="wrap">
  <h1>飞牛音乐扩展</h1>
  <div class="sub">当前用户 <b>__USERNAME__</b> · <span id="ver">…</span> · <a href="/app/fnmusic-ext/" target="_blank" rel="noopener">旧版音源设置页</a> · <span class="sub" style="margin:0">本页是完整管理控制台（桌面图标直接打开这里）</span></div>

  <div class="card">
    <h2>运行状态</h2>
    <div class="row" id="status"><div class="kv"><div class="k">加载中</div><div class="v"><span class="spin"></span></div></div></div>
    <div class="acts">
      <button class="btn" id="refresh">刷新状态</button>
      <button class="btn" id="diagBtn">一键诊断</button>
      <span id="statusMsg" class="sub" style="margin:0"></span>
    </div>
    <div class="msg" id="diagMsg"></div>
    <pre class="log hide" id="diagBox"></pre>
    <div class="acts hide" id="diagActs">
      <button class="btn" id="diagCopy">复制诊断信息</button>
      <span class="sub" style="margin:0">反馈问题时把这段连同日志一起贴上，能直接定位</span>
    </div>
  </div>

  <div class="card">
    <h2>网易云扫码登录</h2>
    <div class="sub" style="margin:-6px 0 12px">
      在线音源全部来自你扫码登录的这一个私人账号。未登录时只能播免费曲目，且不会出现「每日推荐」。
    </div>
    <div class="acts" style="margin-top:0">
      <button class="btn pri" id="qrBtn">生成二维码</button>
      <button class="btn hide" id="qrRefresh">换一张</button>
      <span id="qrState" class="sub" style="margin:0"></span>
    </div>
    <div class="qr hide" id="qrBox">
      <img id="qrImg" alt="网易云登录二维码">
      <div class="st">
        <ol class="steps">
          <li>打开手机上的<b>网易云音乐 App</b></li>
          <li>首页左上角菜单 → <b>扫一扫</b></li>
          <li>扫描左侧二维码，并在手机上<b>确认登录</b></li>
        </ol>
        <p class="sub" style="margin:12px 0 0">二维码约 3 分钟有效，过期会自动换一张，不用手动刷新。</p>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>配置</h2>
    <form id="cfgForm" autocomplete="off">
      <div class="grid">
        <label><span class="lb">音质</span>
          <select name="netease_quality">
            <option value="lossless">无损 lossless</option>
            <option value="exhigh">极高 exhigh (320k)</option>
            <option value="higher">较高 higher (192k)</option>
            <option value="standard">标准 standard (128k)</option>
          </select>
          <span class="ht">账号无对应权益时自动回退，不会因此播放失败</span>
        </label>

        <label><span class="lb">每日推荐曲目数</span>
          <input name="daily_limit" inputmode="numeric" placeholder="20">
          <span class="ht">1–100，抓取网易云官方每日推荐</span>
        </label>

        <label><span class="lb">歌单口径</span>
          <span class="chbox" id="chGroup">
            <label class="ck"><input type="checkbox" data-ch="mine">我的歌单（自建+收藏）</label>
            <label class="ck"><input type="checkbox" data-ch="nrec">推荐歌单</label>
            <label class="ck"><input type="checkbox" data-ch="toplist">排行榜</label>
            <label class="ck"><input type="checkbox" data-ch="category">分类歌单</label>
            <label class="ck"><input type="checkbox" data-ch="newalbum">新碟上架</label>
            <label class="ck"><input type="checkbox" data-ch="fm">私人FM</label>
          </span>
          <input type="hidden" name="netease_channels" value="">
          <span class="ht">勾哪些就往飞牛歌单列表注入哪些；需登录的口径未登录时自动不显示。每日推荐由上面的开关单独控制</span>
        </label>

        <label><span class="lb">每口径注入上限</span>
          <input name="netease_channel_limit" inputmode="numeric" placeholder="8">
          <span class="ht">1–50。排行榜上游有 63 个，不限就会把你自己的本地歌单淹掉</span>
        </label>

        <label><span class="lb">歌单大类顺序</span>
          <input name="netease_channel_order" placeholder="localdaily,daily,mine,nrec,toplist,category,newalbum,fm">
          <span class="ht">飞牛歌单列表里各大类的前后顺序，逗号分隔。可用值：
            daily(网易云每日推荐) / localdaily(本地每日推荐) / mine(我的歌单) / nrec(推荐歌单) /
            toplist(排行榜) / category(分类歌单) / newalbum(新碟上架) / fm(私人FM)。没列出来的排最后</span>
        </label>

        <label><span class="lb">分类歌单的分类</span>
          <input name="netease_category" placeholder="华语">
          <span class="ht">华语 / 欧美 / 日语 / 韩语 / 粤语 / 流行 / 摇滚 / 民谣 / 电子 ……</span>
        </label>

        <label><span class="lb">歌单曲目上限</span>
          <input name="playlist_track_limit" inputmode="numeric" placeholder="300">
          <span class="ht">1–1000，点开歌单时最多解析多少首（越多越慢）</span>
        </label>

        <label><span class="lb">歌单缓存有效期（小时）</span>
          <input name="playlist_cache_ttl_h" inputmode="numeric" placeholder="6">
          <span class="ht">缓存期内点开歌单直接读本地（秒开）；超期后先返回缓存、后台自动刷新，你看到的永远是上一次的结果</span>
        </label>

        <label><span class="lb">每日定时刷新歌单缓存</span>
          <input name="playlist_refresh_at" placeholder="04:30（留空关闭）">
          <span class="ht">每天在这个时间后台刷新全部歌单曲目，第二天打开就是最新内容</span>
        </label>

        <label><span class="lb">收藏归档目录</span>
          <input name="download_dir" placeholder="/vol1/1000-xxx/music/网易云收藏（留空=不下载）">
          <span class="ht">必须是已存在的可写<b>绝对路径</b>；系统目录会被拒绝。按 <code>歌手/歌手 - 歌名.flac</code> 落盘并配同名 .lrc</span>
        </label>

        <label><span class="lb">点收藏时自动下载</span>
          <input type="checkbox" name="download_on_favorite">
          <span class="ht">取账号能拿到的最高品质（jymaster→hires→lossless→exhigh 逐档降级），配歌词</span>
        </label>

        <label><span class="lb">收藏同步到网易云红心</span>
          <input type="checkbox" name="fav_sync_like">
          <span class="ht">点收藏/取消收藏时同步写你网易云账号的红心（双向）。这是对账号的写操作，默认关闭；需要登录态可用</span>
        </label>

        <label><span class="lb">同步洛雪歌单</span>
          <input type="checkbox" name="lx_sync_enabled">
          <span class="ht">把 lx-music-sync-server（洛雪客户端的同步服务）里的歌单，作为<b>只读</b>歌单注入飞牛音乐的歌单列表。
            只读：在飞牛这边加/删歌不会写回服务端，要改请去洛雪客户端改，改完等下一次同步自动生效</span>
        </label>

        <label><span class="lb">洛雪同步服务地址</span>
          <input name="lx_sync_url" placeholder="http://192.168.1.10:9527">
          <span class="ht">就是洛雪客户端「设置 → 同步 → 同步服务地址」里填的那个（默认端口 9527）。填错会在保存时直接报错</span>
        </label>

        <label><span class="lb">洛雪同步服务密码</span>
          <input type="password" name="lx_sync_password" placeholder="留空表示不修改" autocomplete="new-password">
          <span class="ht">服务端 config.js 里 <code>users[].password</code>（或环境变量 <code>LX_USER_&lt;用户名&gt;</code>）的那个密码。
            用户名不用填：服务端是拿密码去匹配账号的。<b>留空 = 保持原值</b></span>
        </label>

        <label><span class="lb">洛雪歌单刷新间隔（秒）</span>
          <input name="lx_sync_refresh_s" inputmode="numeric" placeholder="300">
          <span class="ht">默认 300。第一次打开歌单列表会等一次同步（最多 8 秒），之后都读缓存、后台按这个间隔刷新；
            服务端连不上时继续显示上一次的结果，不会让歌单列表变空</span>
        </label>

        <label><span class="lb">本机在洛雪同步里的设备名</span>
          <input name="lx_sync_device" placeholder="fnmusic-ext">
          <span class="ht">只影响同步服务「设备列表」里显示的名字，方便你认出这是飞牛插件而不是手机/电脑客户端</span>
        </label>

        <label><span class="lb">跳过同步服务的证书校验</span>
          <input type="checkbox" name="lx_sync_insecure_tls">
          <span class="ht">只在服务端用<b>自签 https 证书</b>时才需要勾（勾了之后到该地址的流量不再校验身份，能被人中间人替换；
            域名证书正常时不要勾）</span>
        </label>

        <label><span class="lb">洛雪歌单回写档位</span>
          <select name="lx_sync_writeback">
            <option value="off">只读 off（默认）</option>
            <option value="tracks">可增删歌 tracks</option>
            <option value="all">可增删歌 + 可删歌单 all</option>
          </select>
          <span class="ht"><b>off</b>：飞牛里加/删歌只是本地动作，洛雪那边不变；
            <b>tracks</b>：把歌加入/移出洛雪歌单会真的写回同步服务（电脑手机会看到）；
            <b>all</b>：额外允许在飞牛里删掉整张洛雪歌单——<b>对所有设备生效</b>（服务端保留快照，误删可在管理控制台还原）。
            从搜索里新加的歌缺少洛雪的品质档字段，客户端播放时会自行重解析（能播，属降级写入）</span>
        </label>

        <label><span class="lb">音质：局域网（家里 WiFi / 内网）</span>
          <select name="quality_wifi">
            <option value="standard">标准 standard（128k）</option>
            <option value="higher">较高 higher（192k）</option>
            <option value="exhigh">极高 exhigh（320k）</option>
            <option value="lossless">无损 lossless（FLAC）</option>
            <option value="hires">高清无损 hires（Hi-Res）</option>
            <option value="jymaster">臻品母带 jymaster</option>
          </select>
          <span class="ht">档位与网易云音乐一致，由低到高。账号无对应权益时上游自动降级，不会因此播放失败</span>
        </label>

        <label><span class="lb">音质：非局域网（流量 / 异地远程）</span>
          <select name="quality_cellular">
            <option value="standard">标准 standard（128k）</option>
            <option value="higher">较高 higher（192k）</option>
            <option value="exhigh">极高 exhigh（320k）</option>
            <option value="lossless">无损 lossless（FLAC）</option>
            <option value="hires">高清无损 hires（Hi-Res）</option>
            <option value="jymaster">臻品母带 jymaster</option>
          </select>
          <span class="ht"><b>看不出是不是局域网的请求也按这一档处理</b>——判不出时宁可少给一档音质
          （多半听不出来），也不能在窄管道上灌母带（立刻就卡）。诊断页「网络判定计数」可确认透传是否正常</span>
        </label>

        <label><span class="lb">日志降噪（不记封面 / 心跳 / 轮询）</span>
          <input type="checkbox" name="log_quiet">
          <span class="ht">默认开。日志里一大半是封面图、5 秒保活心跳、客户端状态轮询这类访问行——
          它们成功与否都不影响播放，却把 proxy.log 撑到 10MB 触发截断，
          真正有用的播放日志反而留不住（「播不出来时日志里一行都没有」，一半就是这个原因）。
          打开后这些行不再记录，<b>播放、取链失败、慢分片等排障日志一字不减</b>；
          要抓完整原始日志时关掉它，重启服务生效</span>
        </label>

        <label><span class="lb">单次搜索请求条数</span>
          <input name="netease_search_limit" inputmode="numeric" placeholder="50">
          <span class="ht">1–100，向网易云请求的候选数量</span>
        </label>

        <label><span class="lb">搜索结果并入上限</span>
          <input name="online_limit" inputmode="numeric" placeholder="30">
          <span class="ht">1–100，最终显示在飞牛搜索列表里的在线条数</span>
        </label>

        <label><span class="lb">搜索缓存有效期（天）</span>
          <input name="search_cache_ttl_days" inputmode="numeric" placeholder="7">
          <span class="ht">0 表示不缓存</span>
        </label>

        <label><span class="lb">登录态巡检间隔（小时）</span>
          <input name="login_check_interval_h" inputmode="numeric" placeholder="1">
          <span class="ht">0 表示只在请求时按需探测</span>
        </label>

        <label><span class="lb">单文件日志上限（MB）</span>
          <input name="log_max_mb" inputmode="numeric" placeholder="10">
          <span class="ht">超过即就地截断保留最近一半，0 表示不限制</span>
        </label>

        <label><span class="lb">日志保留天数</span>
          <input name="log_max_days" inputmode="numeric" placeholder="30">
          <span class="ht">超期的备份日志直接删除、超期的活跃日志清空，0 表示永久保留</span>
        </label>

        <label><span class="lb">PushPlus 接口地址</span>
          <input name="pushplus_url" placeholder="https://www.pushplus.plus/send">
          <span class="ht">一般不用改，除非你自建了转发</span>
        </label>
      </div>

      <div class="sw"><input type="checkbox" name="free_only_on_logout" id="c_free">
        <div class="t"><span class="lb">未登录时降级为只播免费曲目</span>
        <span class="ht">关闭则未登录时完全不提供在线播放，搜索结果只剩本地曲库</span></div></div>

      <div class="sw"><input type="checkbox" name="daily_enabled" id="c_daily">
        <div class="t"><span class="lb">启用网易云官方「每日推荐」歌单</span>
          <span class="ht">需要登录；未登录时不会注入空歌单</span></div></div>

      <div class="sw"><input type="checkbox" name="local_daily_enabled" id="c_local_daily">
        <div class="t"><span class="lb">启用「本地每日推荐」歌单</span>
          <span class="ht">每天从本地曲库随机抽一批歌组成歌单（与网易云每日推荐相互独立，无需登录）；播放直接读本地文件</span></div></div>

      <div class="sw"><input type="checkbox" name="local_first" id="c_local_first">
        <div class="t"><span class="lb">本地曲库优先</span>
          <span class="ht">播网易云歌单时，若 NAS 曲库里已有同一首歌就直接读本地文件（零外网、起步最快）；
            没有再走网易云。诊断页「本地曲库优先」一栏能看到索引条数与命中情况。</span></div></div>

      <div class="sw"><input type="checkbox" name="local_first_any_class" id="c_lf_any">
        <div class="t"><span class="lb">本地优先：不挑音质档位</span>
          <span class="ht"><b>推荐开启</b>：本地只要有同名曲就播，不再要求音质档位同类。
            真机上策略要无损（jymaster）时，严格模式会把本地 MP3 拒掉、转头去网易云要无损——
            <b>而网易云给的往往也是 MP3</b>，白出一趟外网。同名多首时仍然优先取无损那一条。
            关掉则恢复严格：策略要无损就只吃本地无损，策略要省流量就不喂本地母带。</span></div></div>

      <div class="sw"><input type="checkbox" name="prefetch_next" id="c_prefetch">
        <div class="t"><span class="lb">下一首预热</span>
          <span class="ht">播当前这首时，提前把「下一首」的直链与元数据取回来（只取几 KB 的 JSON，
            <b>不下载音频</b>）。下一首起步时缓存是热的，实测能省掉几百毫秒的往返。
            「下一首」由我们下发过的歌单顺序推断；随机播放时会猜错，但猜错不产生任何实质代价。</span></div></div>

      <div class="grid" style="margin-top:6px">
        <label><span class="lb">本地每日推荐数量</span>
          <input name="local_daily_limit" inputmode="numeric" placeholder="50">
          <span class="ht">1–500，每天随机抽这么多首本地歌</span></label>

        <label><span class="lb">本地曲库目录（留空=自动探测）</span>
          <input name="library_dir" placeholder="/vol1/1000/music">
          <span class="ht">自动探测依赖飞牛的 music.db；各版本目录布局不统一，猜不中时本地每日推荐
            会一首歌都扫不到（界面上表现为「不出现」）。此时在这里直接填曲库目录即可，
            例如 /vol1/1000/music。填错会在保存时直接报错，不会静默失败。</span></label>

        <label><span class="lb">预热首数（1–5）</span>
          <input name="prefetch_lookahead" inputmode="numeric" placeholder="3">
          <span class="ht">一次预热当前曲之后的几首。只取 JSON、不下载音频，多预热几首几乎不额外
            耗流量；但每台 NAS 的 musicbox 并发能力不同，卡的话调回 1。</span></label>
      </div>

      <div class="sw"><input type="checkbox" name="pushplus_enabled" id="c_push">
        <div class="t"><span class="lb">启用 PushPlus 推送提醒</span>
        <span class="ht">登录失效 / 首次未登录 / 登录成功 / VIP 临期</span></div></div>

      <div class="grid" style="margin-top:6px">
        <label><span class="lb">PushPlus 用户 token</span>
          <input name="pushplus_token" type="password" placeholder="留空表示不修改" autocomplete="new-password">
          <span class="ht">到 pushplus.plus 个人中心复制。该服务需实名认证，否则收不到推送。<b>留空 = 保持原值</b></span>
        </label>

        <label><span class="lb">PushPlus 群组编码</span>
          <input name="pushplus_topic" placeholder="留空则只推送给自己" autocomplete="off">
          <span class="ht">填了就推送到该群组（一对多）</span>
        </label>

        <label><span class="lb">消息模板</span>
          <select name="pushplus_template">
            <option value="markdown">markdown（推荐）</option>
            <option value="html">html</option>
            <option value="txt">txt 纯文本</option>
            <option value="json">json</option>
          </select>
          <span class="ht"></span>
        </label>
      </div>

      <div class="acts">
        <button class="btn pri" type="submit" id="saveBtn">保存并重启生效</button>
        <button class="btn" type="button" id="reloadBtn">放弃修改</button>
        <label style="display:flex;align-items:center;gap:6px;margin:0;width:auto">
          <input type="checkbox" id="restartChk" checked style="width:auto"> <span class="lb" style="margin:0">保存后重启</span>
        </label>
      </div>
      <div class="msg" id="cfgMsg"></div>
    </form>
  </div>

  <div class="card">
    <h2>歌单顺序（手动排序）</h2>
    <div class="sub">点「读取当前歌单」拉取当前实际注入飞牛的网易云歌单（真实名称，顺序与飞牛里显示一致），
      用 ▲▼ 调整后保存。保存后<b>立即生效、无需重启</b>；新出现的歌单会排在手动排过的之后。
      「恢复默认」清除手动顺序，回到按大类（每日推荐/我的歌单/排行榜…）排列。</div>
    <div class="acts">
      <button class="btn" id="plLoadBtn" type="button">读取当前歌单</button>
      <button class="btn pri" id="plSaveBtn" type="button">保存顺序</button>
      <button class="btn" id="plResetBtn" type="button">恢复默认（按大类）</button>
      <button class="btn" id="plWarmBtn" type="button">预热歌单缓存</button>
    </div>
    <div class="msg" id="plMsg"></div>
    <div class="sub" id="plCache" style="margin:0 0 10px"></div>
    <div id="plList"></div>
  </div>

  <div class="sub" style="text-align:center;margin-top:20px">
    fnmusic-ext · 音源仅来自你登录的私人网易云账号 ·
    <a href="https://github.com/gzywd/fnos_music_ext" style="color:var(--acc)">项目主页</a>
  </div>
</div>

<script>
(function(){
"use strict";
var $=function(s){return document.querySelector(s)};
// ⚠️ 任何新增的 checkbox 开关都必须登记进这里，否则会出现「页面永远显示未启用、
// 且保存时该字段根本不提交」的哑 bug（v2.9.0 的 local_daily_enabled 栽过一次）：
//   - loadCfg 只对 BOOLS 里的键做 el.checked=...，其余一律走 el.value=...，
//     而给 checkbox 赋 value 不会改变勾选外观；
//   - 提交时下面那句 `el.type==="checkbox"` 会把未登记的 checkbox 整个跳过，
//     该字段不会出现在 values 里。
var BOOLS=["free_only_on_logout","daily_enabled","local_daily_enabled","local_first","local_first_any_class","prefetch_next","pushplus_enabled","download_on_favorite","fav_sync_like","lx_sync_enabled","lx_sync_insecure_tls","log_quiet"];
var pollTimer=null, qrUnikey="", expireTimer=null;

// 服务端注入的绝对前缀（形如 /app/fnmusicext/）。
// 必须用它，不能用相对路径：页面 URL 无尾斜杠时（/app/fnmusicext），
// 浏览器会把 api/health 解析成 /app/api/health，网关前缀被吃掉一段，
// 请求根本到不了后端，只会拿到网关的 404。反代再套一层时同理。
var BASE="__BASE__";
if(BASE.charAt(BASE.length-1)!=="/") BASE+="/";
function url(p){ return BASE + String(p).replace(/^\/+/, "") }

function api(path,opt){
  return fetch(url(path),opt).then(function(r){
    return r.text().then(function(txt){
      var j=null; try{ j=JSON.parse(txt) }catch(e){}
      if(!j||typeof j!=="object") j={ok:false,error:"HTTP "+r.status+(txt?"："+txt.slice(0,160):"")};
      if(!r.ok&&!j.error) j.error="HTTP "+r.status;
      j._status=r.status;
      return j;
    });
  }).catch(function(e){return {ok:false,error:"网络错误 "+String(e)}});
}
function pill(ok,txt){return '<span class="pill '+(ok?"ok":"err")+'">'+txt+'</span>'}
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]})}

function kv(k,v){return '<div class="kv"><div class="k">'+esc(k)+'</div><div class="v">'+v+'</div></div>'}

function showDiag(txt){
  $("#diagMsg").className="msg err";
  $("#diagMsg").textContent=txt;
}
function status(){
  $("#statusMsg").innerHTML='<span class="spin"></span> 探测中';
  api("api/health").then(function(j){
    $("#statusMsg").textContent="";
    if(!j.ok){
      $("#status").innerHTML=kv("状态",pill(false,"获取失败"))+
        kv("原因",esc(j.error||("HTTP "+(j._status||"?"))));
      showDiag("状态获取失败："+(j.error||("HTTP "+(j._status||"?")))+
        "\n下面已自动抓取后端探测详情与日志，不必 SSH。");
      runDiag(true);
      return;
    }
    // 接口本身成功，但系统有降级项：把每一条原因显式摊开并自动抓诊断
    if(j.healthy===false && (j.problems||[]).length){
      var ps=j.problems;
      $("#status").innerHTML=ps.map(function(x){return kv("需要注意",pill(false,"异常"))+kv("原因",esc(x))}).join("")
        +kv("接口调用","成功（以下是探测到的真实问题）");
      $("#statusMsg").textContent="";
      showDiag("检测到 "+ps.length+" 项异常：\n  · "+ps.join("\n  · ")+
        "\n\n已自动抓取后端探测详情与各组件日志，展开下方即可看到；"+
        "需要反馈时点「复制诊断信息」。");
      if(!DIAG_DONE){ runDiag(true); }
      // 仍然把能拿到的状态渲染出来，别把页面变成一片空白
      renderStatus(j);
      return;
    }
    $("#statusMsg").textContent="";
    renderStatus(j);
  });
}

var DIAG_DONE=false;
function renderStatus(j){
    $("#ver").textContent="v"+(j.version||"?");
    var p=j.proxy||{}, n=j.netease||{};
    var take=j.socket_takeover;
    var mbp=(j.musicbox_probe&&j.musicbox_probe.healthz)||{};
    var h="";
    h+=kv("代理接管", take?pill(true,"已接管"):pill(false,"未接管"));
    h+=kv("官方后端", p.upstream==="ok"?pill(true,"连通"):pill(false,String(p.upstream||"未知")));
    h+=kv("音源服务", p.musicbox==="ok"?pill(true,"运行中"):(p.musicbox==="disabled"?pill(true,"已停用"):pill(false,String(p.musicbox||"未知"))+(mbp.detail?" · "+esc(String(mbp.detail)).slice(0,60):"")));
    if(n.logged_in){
      var v=n.vip?'<span class="pill ok">VIP</span> '
        :'<span class="pill warn">非 VIP'+(n.vip_type?(" (vipType="+n.vip_type+")"):"")+'</span> ';
      h+=kv("网易云", pill(true,"已登录")+" "+v+esc(n.nickname||""));
    }else{
      h+=kv("网易云", pill(false,"未登录")+(n.free_only?' <span class="pill warn">免费曲降级</span>':""));
    }
    h+=kv("每日推荐", p.daily==="ok"?pill(true,"可用"):(p.daily==="need_login"?'<span class="pill warn">需登录</span>':pill(false,String(p.daily||"未知"))));
    h+=kv("PushPlus", j.pushplus_enabled?pill(true,"已启用"):(j.pushplus_configured?'<span class="pill warn">已关闭</span>':'<span class="pill warn">未配置</span>'));
    h+=kv("检测时间", new Date((j.checked_at||0)*1000).toLocaleString());
    var box=$("#status");
    if(j.healthy===false && (j.problems||[]).length){
      box.innerHTML += h.replace(/^/,"");
    }else{
      box.innerHTML=h;
    }
}

function stopPolling(){
  if(pollTimer){clearInterval(pollTimer);pollTimer=null}
  if(expireTimer){clearTimeout(expireTimer);expireTimer=null}
}
function qrState(txt,cls){
  var e=$("#qrState");
  e.textContent=txt||"";
  e.className="sub"+(cls?" "+cls:"");
  e.style.margin="0";
}
function newQr(){
  stopPolling(); $("#qrRefresh").classList.add("hide");
  qrState("正在向音源服务申请二维码…");
  api("api/login/qr",{method:"POST"}).then(function(j){
    if(!j.ok){
      qrState("失败：" + (j.error||("HTTP "+(j._status||"?"))));
      showDiag("生成二维码失败："+(j.error||("HTTP "+(j._status||"?")))+
        "\n常见原因：音源服务（musicbox）没起来，或依赖未装好。点「一键诊断」查看详情与日志。");
      runDiag(true);
      return;
    }
    qrUnikey=j.unikey;
    $("#qrBox").classList.remove("hide");
    $("#qrRefresh").classList.remove("hide");
    $("#qrImg").src=url(j.qr_png)+"&t="+Date.now();
    qrState("等待扫码…","");
    pollTimer=setInterval(function(){poll()},2500);
    expireTimer=setTimeout(function(){qrState("二维码已过期，自动换一张…");newQr()},170000);
  });
}
function poll(){
  if(!qrUnikey) return;
  api("api/login/check?unikey="+encodeURIComponent(qrUnikey)).then(function(j){
    if(!j.ok) return;
    if(j.code===802){ qrState("已扫码，请在手机上点确认登录",""); }
    else if(j.code===803){
      stopPolling(); qrUnikey="";
      qrState("登录成功："+(j.nickname||""),"");
      $("#qrBox").classList.add("hide"); $("#qrRefresh").classList.add("hide");
      status(); loadCfg();
    }
    else if(j.code===800){ stopPolling(); qrState("二维码已过期，自动换一张…"); newQr(); }
  });
}

function loadCfg(){
  return api("api/config").then(function(j){
    if(!j.ok) return j;
    var v=j.values||{};
    Object.keys(v).forEach(function(k){
      var el=document.getElementsByName(k)[0];
      if(!el) return;
      if(BOOLS.indexOf(k)>=0) el.checked=(v[k]==="true");
      else el.value=v[k]==null?"":v[k];
      if(k==="pushplus_token") el.placeholder = j.has_token ? "已保存（留空则不修改）" : "留空表示不启用推送";
      if(k==="netease_channels") hiddenToChannels();
    });
    return j;
  });
}

// 歌单口径：多个勾选框 <-> 单个隐藏域 netease_channels
function channelsToHidden(){
  var picked=[];
  Array.prototype.forEach.call(document.querySelectorAll("#chGroup input[data-ch]"),function(el){
    if(el.checked) picked.push(el.getAttribute("data-ch"));
  });
  var order=["mine","nrec","toplist","category","newalbum","fm"];
  picked.sort(function(a,b){ return order.indexOf(a)-order.indexOf(b) });
  var h=document.getElementsByName("netease_channels")[0];
  if(h) h.value=picked.join(",");
}
function hiddenToChannels(){
  var h=document.getElementsByName("netease_channels")[0];
  if(!h) return;
  var on=(h.value||"").split(",");
  Array.prototype.forEach.call(document.querySelectorAll("#chGroup input[data-ch]"),function(el){
    el.checked = on.indexOf(el.getAttribute("data-ch"))>=0;
  });
}
Array.prototype.forEach.call(document.querySelectorAll("#chGroup input[data-ch]"),function(el){
  el.onchange=channelsToHidden;
});
hiddenToChannels();

$("#qrBtn").onclick=function(){ newQr() };
$("#qrRefresh").onclick=function(){ newQr() };
$("#refresh").onclick=status;
$("#reloadBtn").onclick=function(){ $("#cfgMsg").className="msg"; loadCfg() };

function fmtBytes(n){ n=Number(n)||0; return n>1048576 ? (n/1048576).toFixed(2)+" MB"
  : n>1024 ? (n/1024).toFixed(1)+" KB" : n+" B" }

function runDiag(auto){
  DIAG_DONE=true;
  var box=$("#diagBox"), acts=$("#diagActs");
  box.classList.remove("hide"); acts.classList.remove("hide");
  if(!auto){ $("#diagMsg").className="msg info"; $("#diagMsg").textContent="正在收集诊断信息…"; }
  var w="proxy";  // 日志页签已移除，固定取代理日志
  Promise.all([
    api("api/diag"),
    api("api/logs?what=info&lines=60"),
    api("api/logs?what="+encodeURIComponent(w)+"&lines=60"),
    api("api/logs?what=musicbox&lines=40"),
    api("api/logs?what=proxy&lines=40")
  ]).then(function(rs){
    var d=rs[0], out=[];
    out.push("========== fnmusic-ext 诊断 "+new Date().toLocaleString()+" ==========");
    if(!d.ok){ out.push("诊断接口失败："+(d.error||("HTTP "+(d._status||"?")))); }
    else{
      out.push("版本: "+d.version+"   页面进程 pid="+d.runtime.pid+" 已运行 "+d.runtime.uptime_s+"s");
      out.push("");
      out.push("-- 请求路径（反代/组网排障关键）--");
      out.push("  浏览器侧 base 前缀 : "+d.request.base_prefix);
      out.push("  后端收到原始路径   : "+d.request.original_path);
      out.push("  归一化后内部路径   : "+d.request.normalized_path);
      out.push("  配置的网关前缀     : "+d.request.gateway_prefix_config);
      out.push("  X-Forwarded-Prefix : "+(d.request.x_forwarded_prefix||"(无)"));
      out.push("  网关身份 Header    : "+JSON.stringify(d.request.identity));
      out.push("");
      out.push("-- socket 接管 --");
      out.push("  "+d.proxy_socket.path+" 存在="+d.proxy_socket.exists+" 权限="+d.proxy_socket.mode);
      out.push("  upstream("+d.proxy_socket.upstream_exists+")");
      var wd=d.watchdog||{};
      out.push("  看门狗: 运行中="+((wd.alive)?("是(pid="+wd.pid+")"):"否")+
              "   主动停机标志="+(wd.stopped_flag?"存在":"无")+
              "   配置重启标记="+(wd.restart_marker?"存在":"无"));
      out.push("  （看门狗负责在代理死亡/接管丢失时自动恢复；官方后端重启会重绑");
      out.push("   trim_music.socket，那正是「应用异常退出」的典型来源）");
      out.push("");
      out.push("-- 音质策略与「跟随飞牛」发现情况 --");
      var qy=d.quality||{};
      if(!qy.reachable){
        out.push("  代理进程不可达（"+(qy.error||("status="+qy.status))+
                 "）→ 看不到已观察到的客户端线索");
      }else{
        var _POLNAME={"by_lan":"局域网听无损 / 其他一律 320k",
                      "follow_fnos":"跟随飞牛偏好","by_network":"按网络分别设置",
                      "fixed":"固定音质"};
        out.push("  策略            : "+(qy.policy||"")
                 +(_POLNAME[qy.policy]?("（"+_POLNAME[qy.policy]+"）"):""));
        out.push("  档位配置        : "+JSON.stringify(qy.levels||{}));
        var cur=qy.current||{};
        out.push("  当前判定        : level="+cur.level+"  network="+cur.network+"  source="+cur.source);
        out.push("  ★ source 就是「有没有真的跟随上飞牛」的答案：auto:* = 读到了；"
                +"fallback:* = 没读到，在用手动脉位");
        var hits=qy.observed_client_hints||{};
        var hk=Object.keys(hits);
        out.push("  客户端音质/网络线索 : "+(hk.length?hk.length+" 种":"暂未观察到任何带音质或网络语义的键"));
        var cip=qy.client_ips||{};
        out.push("  客户端 IP 线索(XFF) : 局域网="+(cip.lan||0)+" 次  远程(公网)="+(cip.remote||0)+" 次"+
                 "  本机转发(回环)="+(cip.relay||0)+" 次");
        out.push("    远程样例        : "+((cip.remote_samples&&cip.remote_samples.length)
                  ?cip.remote_samples.join(" | ")+("（最近一次 "+cip.remote_last+"s 前）")
                  :"（一次都没观察到公网 IP）"));
        out.push("    局域网样例      : "+((cip.lan_samples&&cip.lan_samples.length)
                  ?cip.lan_samples.join(" | ")+("（最近一次 "+cip.lan_last+"s 前）")
                  :"（无）"));
        out.push("  ★ 怎么读出「现在是 WiFi 还是数据」：远程样例是真公网 IP 且 remote_last 很小"
                +"= 正在远程/数据访问；局域网样例是 192.168/10.x 且 lan_last 很小 = 正在家里 WiFi；"
                +"relay 次数很大 = 请求由本机中继转发，源 IP 证明不了客户端在哪（这种最容易被误判成局域网）");
        out.push("  （远程(公网)访问默认按流量场景走省流档，可用 FNMUSIC_REMOTE_AS_CELLULAR=false 关闭）");
        var nj=qy.network_judgements||{}, nk=Object.keys(nj);
        out.push("  网络判定计数    : "+(nk.length?nk.map(function(k){return k+"="+nj[k];}).join("  "):"（尚无）"));
        var ln=qy.last_network||{};
        out.push("  粘性结论        : "+(ln.network?((ln.network==="cellular"?"流量/远程":"局域网")+"（"+ln.age_s+"s 前判出，判不出的请求沿用它）"):"无（近期没有任何一次能明确判出网络）"));
        out.push("  ★ 判不出来的请求才会卡顿：它们不走降档分支，照发母带。"
                +"「unknown」次数远大于 lan/remote 时，把 FNMUSIC_UNKNOWN_AS_CELLULAR 设为 true"
                +"（当前="+qy.unknown_as_cellular+"）即可让所有判不出的请求一律走省流档");
        hk.slice(0,8).forEach(function(k){
          out.push("     "+k+"  x"+hits[k].count+"  样例="+JSON.stringify(hits[k].samples));
        });
        var paths=qy.observed_paths_with_hints||{};
        Object.keys(paths).slice(0,4).forEach(function(k){
          out.push("     路径 "+k+"  x"+paths[k]);
        });
        var dbs=qy.db_scan||{};
        out.push("  music.db 扫描   : available="+dbs.available+"  命中行="+(dbs.hits||0)
                +(dbs.error?("  error="+dbs.error):"")+(dbs.note?("  "+dbs.note):""));
        (dbs.sample_hits||[]).slice(0,3).forEach(function(h){
          out.push("     "+h.table+": "+JSON.stringify(h.row).slice(0,180));
        });
        if(cur.source&&cur.source.indexOf("fallback")===0)
          out.push("  ★ 目前是回落状态（没读到飞牛偏好）。把上面「客户端线索」与「music.db 命中行」"
                  +"贴出来，就能确定飞牛把音质偏好放在哪里，进而改成真正的自动跟随。");
      }
      out.push("");
      out.push("-- 本地每日推荐（排障）--");
      var ld=d.local_daily||{};
      if(!ld.reachable){
        out.push("  取不到代理侧快照: "+JSON.stringify(ld));
      }else{
        out.push("  开关           : enabled="+ld.enabled+"   数量上限="+ld.limit);
        out.push("  代理运行身份   : "+ld.proxy_identity);
        out.push("  曲库目录       : "+ld.library_dir+"  存在="+ld.library_dir_exists+"  可读="+ld.library_readable);
        if(ld.probe_error) out.push("  ★ 目录探测失败: "+ld.probe_error);
        out.push("  是否回落到空目录: "+ld.library_is_cache_fallback
                 +(ld.library_is_cache_fallback?("  ★ 这就是歌单不出现的原因（cache 目录="+ld.cache_dir+"）"):""));
        out.push("  扫到音频文件数 : "+ld.scanned_files);
        (ld.sample_files||[]).forEach(function(f){ out.push("     例: "+f); });
        var cp=ld.cover_probe;
        if(cp){
          out.push("  封面探测       : 曲目="+cp.tracks+"  已查="+cp.checked
                   +"  有索引="+cp.indexed+"  内嵌图="+cp.embedded_cover
                   +"  同目录图="+cp.sibling_cover+"  可用="+cp.usable);
          if(cp.reason) out.push("                   ★ "+cp.reason);
          if(cp.error) out.push("                   探测异常: "+cp.error);
        }
        out.push("  music.db       : "+ld.music_db.resolved+"  存在="+ld.music_db.exists
                 +(ld.music_db.read_error?("  读取失败="+ld.music_db.read_error):""));
        (ld.music_db.shared_library||[]).slice(0,5).forEach(function(p){ out.push("     shared_library: "+p); });
        out.push("  已探测候选路径 :");
        (ld.music_db.probed||[]).forEach(function(c){
          out.push("     "+(c.exists?"[存在] ":"[缺失] ")+c.path);
        });
        if(ld.hint) out.push("  ★ "+ld.hint);
      }
      // ---- 本地曲库优先（v2.9.14）：能不能命中，全看这一块 ----
      out.push("");
      out.push("-- 本地曲库优先（播网易云歌单时优先读本地同名文件）--");
      var lf=d.local_first||{};
      if(!lf.reachable){ out.push("  取不到代理侧快照: "+JSON.stringify(lf)); }
      else{
        out.push("  开关           : "+lf.enabled+"（any_class="+lf.any_class+"：true=不看音质档位，本地有就播）");
        if(lf.any_class&&lf.cellular_lossy_only)
          out.push("  流量网络       : 本地无损让给在线省流档（已让 "+lf.cellular_skips+" 次）"
                  +"——本地 FLAC 30~40MB vs 在线 320k ~9MB，数据网络下体积才是决定因素");
        var _fs = (lf.fs_scanned||0);
        out.push("  索引           : "+lf.entries+" 首 / "+lf.titles+" 个标题"
                 +"（music.db "+lf.from_db+" + 目录扫描新增 "+lf.from_fs+"）"
                 +(lf.db_broken?("  [已忽略 music.db 失效记录 "+lf.db_broken+" 条]"):""));
        // fs_scanned 必须单独给：新增为 0 常常不是「没扫到」而是「扫到的
        // 全在 music.db 里已有」，只显示 0 会让人误判成扫描坏了。
        out.push("  目录扫描       : "+(lf.library_dir?("扫到 "+_fs+" 个音频文件，"
                 +(_fs?("其中 "+lf.from_fs+" 首是 music.db 之外的补充"):"（目录不存在或扫不动）"))
                 :"（未给曲库目录，跳过）"));
        out.push("  曲库目录       : "+(lf.library_dir||"（未定位）"));
        var st=lf.selftest||{};
        out.push("  自测           : "+(st.ok?("能匹配上自己（"+st.title+" → "+st.path+"）")
                                        :(st.title?("★ 连索引里自己的歌都匹配不上: "+st.title):"（索引为空，无法自测）")));
        out.push("  查询/命中      : "+lf.lookups+" / "+lf.lookup_hits);
        (lf.recent||[]).slice(-5).forEach(function(r){
          out.push("     "+(r.hit?"[命中] ":"[未中] ")+r.title+" - "+r.artist
                   +(r.hit?(" → "+r.path):(" （"+r.reason
                     +(r.miss_path?(": "+r.miss_path):"")+"）")));
          // 未命中时给出索引里最像的标题：一眼分清「本地真没这首歌」
          // 和「名字写法的差异」——没有这个就只能靠猜。
          if(!r.hit && r.near && r.near.length){
            out.push("       索引里最接近: "+r.near.join(" / "));
          }
        });
        if(lf.hint) out.push("  ★ "+lf.hint);
      }
      // ---- 下一首预热（T1）----
      out.push("");
      out.push("-- 下一首预热（T1：提前取回下一首的直链与元数据，不下载音频）--");
      var pf=d.prefetch||{};
      if(!pf.reachable){ out.push("  取不到代理侧快照: "+JSON.stringify(pf)); }
      else{
        out.push("  开关           : "+pf.enabled+"（预热 "+pf.lookahead+" 首，列表下发即预热头首="+pf.on_list+"）");
        if(pf.lookahead_requested && pf.lookahead_requested > pf.lookahead)
          out.push("  ★ 已收敛       : .env 里写的是 "+pf.lookahead_requested+"，实际按 "+pf.lookahead
                   +" 执行（上限 3）。多预热一首就多占 musicbox 约 400ms，而队列上限只有 "
                   +pf.max_queue+"，超出的会被直接丢弃——等于白占资源、还拖慢正在播的这首。");
        out.push("  调度/成功/失败 : "+pf.scheduled+" / "+pf.done+" / "+pf.failed
                 +"（推断不出下一首 "+pf.no_next+" 次，已预热过跳过 "+pf.already+" 次"
                 +(pf.queued_out?("，队列满放弃 "+pf.queued_out+" 次"):"")+"）");
        out.push("  队列上限       : "+pf.max_queue+"（同时在飞的预热任务上限，满了就让位给播放）");
        out.push("  成果有效期     : "+pf.warm_ttl_s+"s（与直链缓存 TTL 对齐；以前写死 3600s，"
                 +"导致过期后照付往返的播放仍被标 warm）");
        out.push("  在线播放       : "+pf.plays+" 首，命中预热 "+pf.hits+" 首（命中率 "
                 +Math.round((pf.hit_rate||0)*100)+"%）"
                 +(pf.repeat_plays?("，另有 "+pf.repeat_plays+" 次是同一首的续传请求（不计入）"):""));
        // warm/cold 现在按「这次有没有真的省掉 musicbox 往返」分，不再是「曾经预热过」
        out.push("  取链耗时       : 未预热 "+pf.cold_ms+"ms → 命中预热 "+pf.warm_ms+"ms"
                 +(pf.saved_ms?("  ★ 省下约 "+pf.saved_ms+"ms")
                              :(pf.warm_ms?("  ★ 命中预热反而更慢 "+(pf.warm_ms-pf.cold_ms)
                                 +"ms——看下面的「撞上预热进行中」"):"")));
        if(pf.warming_hits)
          out.push("  撞上预热进行中 : "+pf.warming_hits+" 次（这些归在未预热里；预热没跑完就"
                   +"点播，不但吃不到成果还要跟它抢 musicbox，是「越预热越慢」的主因）");
        (pf.contexts||[]).forEach(function(c){
          out.push("     上下文: "+c.ctx+"（"+c.tracks+" 首，"+(c.age_s||0)+"s 前下发）");
        });
        (pf.recent||[]).slice(-5).forEach(function(r){
          out.push("     "+(r.ok?"[ok] ":"[fail] ")+r.guid+" "+r.ms+"ms"+(r.detail?(" "+r.detail):""));
        });
        if(!pf.plays) out.push("  （还没有在线播放记录：播一首网易云的歌再来看）");
      }
      out.push("");
      out.push("-- 官方 HLS 实时转码（播飞牛本地曲库时）--");
      var hs=d.hls||{};
      if(!hs.reachable){ out.push("  取不到代理侧快照: "+JSON.stringify(hs)); }
      else{
        out.push("  会话/分片      : "+hs.sessions+" 次会话 / "+hs.segments+" 个分片");
        out.push("  转码启动开销   : 平均 "+hs.first_ms_avg+"ms  最大 "+hs.first_ms_max+"ms"
                 +"（= 从客户端拿到 m3u8 到取回第一个分片的间隔）");
        out.push("  分片转发耗时   : 平均 "+hs.seg_ms_avg+"ms  最大 "+hs.seg_ms_max+"ms");
        if(hs.seg_slow)
          out.push("  ★ 慢分片       : "+hs.seg_slow+" 个分片超过 "+hs.seg_slow_ms+"ms"
                   +"（最慢 "+hs.seg_ms_max+"ms"
                   +(hs.seg_ms_max_guid?("，曲目 "+hs.seg_ms_max_guid+" / "+hs.seg_ms_max_seg):"")+"）"
                   +"——播放器一般等 7~8s 就跳歌，这个数字就是「等半天然后自动跳过」的现场；"
                   +"我们这边的转发会继续挂着，所以日志里的耗时比用户感知的更长。");
        out.push("  ★ 怎么读：first_ms 大 = 「点下去要等」（转码器初始化慢）；"
                +"seg_ms 最大远大于平均 = 「播起来断断续续」（转码吞吐跟不上）。"
                +"两者解法不同，先分清是哪一个。");
        out.push("  直出绕过       : "+hs.bypass_enabled+"（FNMUSIC_HLS_LOCAL_BYPASS，已绕过 "
                 +hs.bypassed+" 次）"
                 +(hs.bypass_enabled?"":"——打开后跳过转码直出原始流；本地多为 FLAC，"
                  +"放不出来就关掉，且直出不省流量"));
        if(!hs.sessions) out.push("  （还没有 HLS 播放记录：播一首飞牛本地曲库的歌再来看）");
      }
      out.push("");
      out.push("-- 播放起步日志（play-start 去重）--");
      var ps=d.playstart||{};
      if(!ps.reachable){ out.push("  取不到代理侧快照: "+JSON.stringify(ps)); }
      else{
        out.push("  实际播放       : "+ps.plays+" 首（日志里的 play-start 行数）");
        out.push("  折叠续传       : "+ps.folded+" 次（同一首歌的 Range 续传，不再刷屏）");
        out.push("  每次播放请求数 : "+ps.requests_per_play+"（窗口 "+ps.window_s+"s 内合并）");
        out.push("  ★ 怎么读：一次播放本来就要分几个 Range 拉，比值 2~10 都正常；"
                +"到几十就说明客户端在反复重开连接，那才是卡顿信号。"
                +"标记 local 表示音频走本地文件（不吃预热），cold/warm 只用于走网易云的播放。");
        if(!ps.plays) out.push("  （还没有播放记录：播一首歌再来看）");
      }
      out.push("");
      out.push("-- 最近播不出来（失败留证）--");
      var fl=d.failures||{};
      if(!fl.reachable){ out.push("  取不到代理侧快照: "+JSON.stringify(fl)); }
      else if(!fl.count){ out.push("  暂无记录。播放失败会在这里留一条，含阶段与耗时——"
                                  +"以前跳歌是完全不留痕的，事后只能靠猜。"); }
      else{
        out.push("  共 "+fl.count+" 条（保留最近 "+fl.kept+" 条，下面按时间倒序列出最新的）");
        (fl.items||[]).slice().reverse().forEach(function(f){
          out.push("     "+f.ts+"  ["+(f.stage||"-")+"]  "+f.guid
                   +(f.ms!=null?("  耗时 "+f.ms+"ms"):"")+"  "+f.reason);
        });
        out.push("  ★ 阶段怎么读：resolve = 连直链都没拿到（musicbox 慢/超时、登录态掉了、接口空返回）；"
                +"open-cdn = 直链拿到了但取流被拒（直链过期、403）；"
                +"slow-start = 连元数据带直链就花了 5s 以上，播放器 7~8s 跳歌时它已占掉大半。");
      }
      out.push("");
      out.push("-- 音频走哪条路（302 直连 / NAS 中转 / 本地文件）--");
      var sm=d.streammode||{};
      if(!sm.reachable){ out.push("  取不到代理侧快照: "+JSON.stringify(sm)); }
      else{
        out.push("  非局域网直连   : "+sm.enabled+"（FNMUSIC_CDN_REDIRECT）");
        out.push("  302 直连 CDN   : "+sm.redirect+" 次（音频不经 NAS，但也不落缓存）");
        out.push("  NAS 中转       : "+sm.tee+" 次（局域网：边播边存）");
        out.push("  本地文件       : "+sm.local+" 次");
        out.push("  取链超时       : "+sm.timeout_s+"s（上限 "+sm.timeout_cap_s
                 +"s；到点就回 404，让播放器立刻切下一首）");
        out.push("  直连失败回退   : "+sm.storm+" 首（同一首歌 "+sm.storm_n+" 次请求落在 "
                 +sm.storm_window_s+"s 内 = 客户端没从 CDN 取到音频，已改走 NAS 中转）");
        out.push("  ★ 怎么读：在外面（流量/远程）播放时「302 直连」应该是大头；"
                +"若它一直是 0，说明请求被判成了局域网（看上面「客户端 IP 线索」）或开关没开。");
        out.push("  ★ 一首歌正常只该来要 1 次直链，之后自己去 CDN 拉。「302 直连」次数远大于"
                +"实际播放首数，就是客户端在反复回来——那正是「没放完就被切歌」的样子，"
                +"现在会自动回退到中转；若「直连失败回退」长期不为 0，说明 302 这条路不适合你的客户端，关掉开关更省事。");
      }
      out.push("");
      out.push("-- 音源服务 (musicbox) --");
      out.push("  "+d.musicbox.url);
      out.push("  healthz      : "+JSON.stringify(d.musicbox.healthz));
      out.push("  auth/status  : "+JSON.stringify(d.musicbox.auth_status));
      out.push("  auth/detail  : "+JSON.stringify(d.musicbox.auth_detail));
      out.push("  登录态       : "+JSON.stringify(d.musicbox.login)+"  error="+d.musicbox.login_error);
      var stt=d.selftest||{};
      out.push("  -- CLI 自检 --");
      out.push("  cli_found    : "+stt.cli_found+"   resolved_by="+String(stt.resolved_by||"-"));
      out.push("  cli_cmd      : "+JSON.stringify(stt.cli_cmd||[]));
      out.push("  cli_exec_ok  : "+stt.cli_exec_ok+"   detail="+String(stt.cli_exec_detail||"-").slice(0,140));
      out.push("  venv_bin_dir : "+String(stt.venv_bin_dir||"-"));
      out.push("  interpreter  : "+String(stt.interpreter||"-")+"  ("+stt.python_version+")");
      out.push("  NEMbox 可导入: "+stt.nembox_importable+(stt.nembox_error?"  "+stt.nembox_error:""));
      out.push("  运行身份     : uid="+stt.uid+" user="+stt.running_as);
      out.push("  XDG          : "+JSON.stringify(stt.xdg||{}));
      if(stt.cli_found===false) out.push("  ★ CLI 没找到 → 搜索/取直链/扫码登录会全部 502。修复：重装依赖到音源服务的 venv，并确认 venv_bin_dir 下有 musicbox 可执行文件。");
      if(d.musicbox.auth_status&&d.musicbox.auth_status.status===502&&d.musicbox.healthz.status===200)
        out.push("  ★ healthz 200 但 auth/status 502：典型的 CLI 子进程不可用，见上面的 CLI 自检。");
      out.push("");
      out.push("-- 配置文件 --");
      out.push("  "+d.env_file.path+"  存在="+d.env_file.exists+" 可写="+d.env_file.writable
               +" 权限="+d.env_file.mode+" 键数="+d.env_file.keys);
      out.push("  PushPlus token 已配置="+d.env_file.pushplus_token_configured
               +" 长度="+d.env_file.pushplus_token_length+"（值不外泄）");
      out.push("  推送="+JSON.stringify(d.pushplus));
      out.push("  重启脚本="+d.restart_script.path+" 存在="+d.restart_script.exists);
      out.push("");
      out.push("-- 日志目录 "+d.logs.dir+" (存在="+d.logs.dir_exists+") 策略: >"+d.logs.max_mb
               +"MB 截断 / >"+d.logs.max_days+"天清理 --");
      out.push("  日志降噪 : "+(d.logs.quiet===false?"关（记录全部访问行）":"开（已过滤 封面/保活心跳/客户端轮询/自身探针；播放与失败日志不减）"));
      (d.logs.files||[]).forEach(function(f){
        out.push("  "+f.name+"  "+fmtBytes(f.bytes)+"  修改于 "+new Date(f.mtime*1000).toLocaleString());
      });
    }
    [["info",rs[1]],[w,rs[2]],["musicbox",rs[3]],["proxy",rs[4]]].forEach(function(pair){
      var name=pair[0], r=pair[1];
      out.push("");
      out.push("========== "+name+".log ==========");
      if(!r.ok){ out.push("读取失败："+(r.error||("HTTP "+(r._status||"?")))); return; }
      var ls=r.lines||[];
      out.push(ls.length?ls.join("\n"):((r.note||"（暂无日志）")+"  path="+(r.path||"?")));
    });
    var txt=out.join("\n");
    box.textContent=txt;
    box.scrollTop=0;
    $("#diagMsg").className = d.ok ? "msg ok" : "msg err";
    $("#diagMsg").textContent = d.ok
      ? "诊断信息已收集完毕，可点「复制诊断信息」贴给维护者。其中不含任何 token。"
      : "诊断接口本身也失败了，下面是已能取到的信息。";
    $("#diagCopy").onclick=function(){
      if(navigator.clipboard&&navigator.clipboard.writeText){
        navigator.clipboard.writeText(txt).then(function(){
          $("#diagMsg").className="msg ok";
          $("#diagMsg").textContent="已复制到剪贴板。若浏览器因非安全上下文拒绝，请手动全选复制。";
        },function(){ prompt("浏览器拒绝剪贴板，请手动全选复制：",txt) });
      }else{ prompt("请手动全选复制：",txt) }
    };
  });
}
$("#diagBtn").onclick=function(){ DIAG_DONE=false; runDiag(false) };

// ---------------- 歌单顺序（手动排序） ----------------
var PL_ITEMS=[];
var PL_CH_LABELS={daily:"每日推荐",mine:"我的歌单",nrec:"推荐歌单",toplist:"排行榜",
  category:"分类歌单",newalbum:"新碟上架",fm:"私人FM"};
function plLabel(ch){ return PL_CH_LABELS[ch]||ch||"" }
function plToken(it){ return it.channel==="daily" ? "daily" : it.guid }
function renderPl(){
  var box=$("#plList");
  if(!PL_ITEMS.length){
    box.innerHTML='<div class="sub" style="margin:0">（暂无歌单——需已登录且至少启用一个歌单口径）</div>';
    return;
  }
  box.innerHTML=PL_ITEMS.map(function(it,i){
    return '<div class="plrow">'+
      '<span class="plpos">'+(i+1)+'</span>'+
      '<span class="plname">'+esc(it.name)+'</span>'+
      '<span class="pill warn" style="margin:0">'+esc(plLabel(it.channel))+'</span>'+
      '<span class="plbtns">'+
        '<button class="btn" type="button" data-i="'+i+'" data-d="-1"'+(i===0?" disabled":"")+'>▲</button>'+
        '<button class="btn" type="button" data-i="'+i+'" data-d="1"'+(i===PL_ITEMS.length-1?" disabled":"")+'>▼</button>'+
      '</span></div>';
  }).join("");
  Array.prototype.forEach.call(box.querySelectorAll("button[data-i]"),function(b){
    b.onclick=function(){
      var i=+b.getAttribute("data-i"),d=+b.getAttribute("data-d"),j=i+d;
      if(j<0||j>=PL_ITEMS.length) return;
      var t=PL_ITEMS[i];PL_ITEMS[i]=PL_ITEMS[j];PL_ITEMS[j]=t;
      renderPl();
    };
  });
}
function loadPl(){
  var b=$("#plLoadBtn"); b.disabled=true; b.textContent="读取中…";
  api("api/playlists").then(function(j){
    b.disabled=false; b.textContent="读取当前歌单";
    var m=$("#plMsg"); m.className="msg";
    if(!j.ok){ m.className="msg err"; m.textContent="读取失败："+(j.error||("HTTP "+(j._status||"?"))); return; }
    PL_ITEMS=(j.items||[]).filter(function(it){ return it && it.guid });
    m.textContent = PL_ITEMS.length
      ? ("共 "+PL_ITEMS.length+" 个歌单"+(j.logged_in?"":"（当前未登录，需登录的口径未列出）")
         +(j.manual_order&&j.manual_order.length?"；已启用手动顺序":"；当前按大类顺序"))
      : "暂无歌单（未登录或未启用任何口径）";
    var c=j.cache||{};
    if(c.total!=null){
      $("#plCache").textContent="缓存："+c.cached+"/"+c.total+" 个歌单已有本地缓存"+
        (c.warming?"（正在预热…）":"")+"；有效期 "+Math.round((c.ttl_s||0)/3600)+" 小时"+
        (c.refresh_at?("；每日 "+c.refresh_at+" 定时刷新"):"；定时刷新已关闭")+
        "。已缓存的歌单点开即秒开。";
    }
    renderPl();
  });
}
function savePlOrder(val){
  var b=(val===null)?$("#plResetBtn"):$("#plSaveBtn");
  b.disabled=true; var orig=b.textContent; b.textContent="保存中…";
  api("api/config",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({values:{netease_playlist_order:(val==null?"":val)},restart:false})}
  ).then(function(j){
    b.disabled=false; b.textContent=orig;
    var m=$("#plMsg");
    if(!j.ok){ m.className="msg err"; m.textContent="保存失败："+(j.error||"未知错误"); return; }
    m.className="msg ok";
    m.textContent = (val==null||val==="") ? "已恢复默认（按大类）顺序。" : "顺序已保存，立即生效。";
    setTimeout(loadPl,800);   // 代理实时读 .env，回读确认新顺序
  });
}
$("#plLoadBtn").onclick=loadPl;
$("#plSaveBtn").onclick=function(){ savePlOrder(PL_ITEMS.map(plToken).join(",")) };
$("#plResetBtn").onclick=function(){ savePlOrder(null) };
$("#plWarmBtn").onclick=function(){
  var b=this; b.disabled=true; b.textContent="预热中…";
  api("api/playlists/warm",{method:"POST"}).then(function(j){
    b.disabled=false; b.textContent="预热歌单缓存";
    var m=$("#plMsg"); m.className=j.ok?"msg ok":"msg err";
    m.textContent=j.ok?(j.message||"预热已开始"):(j.error||"预热失败");
    setTimeout(loadPl,5000);  // 预热是后台任务，稍后回读缓存状态
  });
};

loadPl();
$("#cfgForm").onsubmit=function(ev){
  ev.preventDefault();
  var fd=new FormData(ev.target), values={};
  // 未登记的 checkbox 会被下面 `el.type==="checkbox"` 直接跳过 → 该字段丢失，
  // 后端按「页面没提交 → 保留原值」处理，于是用户勾了也永远不生效。改成
  // 以页面实际的 checkbox 为准兜底：凡是 BOOLS 里有、页面上却没有元素的就跳过。
  BOOLS.forEach(function(k){
    var b=document.getElementsByName(k)[0];
    if(b) values[k]=b.checked?"true":"false";
  });
  Array.prototype.forEach.call(document.querySelectorAll("#cfgForm input,#cfgForm select"),function(el){
    if(!el.name||BOOLS.indexOf(el.name)>=0||el.type==="checkbox") return;
    values[el.name]=el.value;
  });
  var restart=$("#restartChk").checked;
  if(restart){
    var okc=window.confirm(
      "保存后将重启扩展进程，新配置才会生效。\n\n"+
      "重启的几秒钟里接管会先解除、再重新建立，\n"+
      "期间飞牛音乐会短暂回到官方原生直连（本地曲库始终可用），\n"+
      "正在播放的在线曲目可能中断一次。\n\n"+
      "确定继续吗？"
    );
    if(!okc) return;
  }
  var btn=$("#saveBtn"); btn.disabled=true; btn.innerHTML='<span class="spin"></span> 保存中';
  $("#cfgMsg").className="msg info"; $("#cfgMsg").textContent="正在写入配置…";
  api("api/config",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({values:values,restart:restart})}).then(function(j){
    btn.disabled=false; btn.textContent="保存并重启生效";
    var m=$("#cfgMsg");
    if(!j.ok){ m.className="msg err"; m.textContent="失败："+(j.error||"未知错误"); return; }
    var lines=["配置已保存。"];
    if(j.restart_message) lines.push(j.restart_message);
    if(j.warning) lines.push("⚠ "+j.warning);
    m.className=(j.warning||j.restarted===false)?"msg err":"msg ok";
    m.textContent=lines.join("\n");
    loadCfg();
    setTimeout(status, 1500);
  });
};

status(); loadCfg();
setInterval(status, 60000);
})();
</script>
</body>
</html>
"""
