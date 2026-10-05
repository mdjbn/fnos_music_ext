"""PushPlus 推送（登录态失效 / VIP 到期等运维提醒）。

配置全部来自环境变量，token 绝不写入日志、缓存或 healthz 响应。

    FNMUSIC_PUSHPLUS_ENABLED    true|false，默认 true（未配 token 时自动视为关闭）
    FNMUSIC_PUSHPLUS_TOKEN      用户 token（必填才会真正发送）
    FNMUSIC_PUSHPLUS_TOPIC      群组编码，留空只发给自己
    FNMUSIC_PUSHPLUS_TEMPLATE   html|txt|json|markdown，默认 markdown
    FNMUSIC_PUSHPLUS_URL        默认 https://www.pushplus.plus/send

PushPlus 免费档限制（官方 help/limit.html）：每日 200 次、每分钟 5 次、
相同内容每小时限 3 条。因此本模块内置两道节流：
    1. 相同 (title, content) 指纹在 _DEDUPE_TTL 内只发一次；
    2. 全局发送间隔不小于 _MIN_INTERVAL_S。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger("fnmusic_proxy.pushplus")

DEFAULT_URL = "https://www.pushplus.plus/send"
DEFAULT_TEMPLATE = "markdown"
REQUEST_TIMEOUT_S = 8.0

_DEDUPE_TTL = float(os.environ.get("FNMUSIC_PUSHPLUS_DEDUPE_TTL", "3600"))
_MIN_INTERVAL_S = float(os.environ.get("FNMUSIC_PUSHPLUS_MIN_INTERVAL", "12"))

# PushPlus 返回码（官方 doc/guide/code.html）
CODE_OK = 200
CODE_NOT_LOGIN = 302
CODE_UNAUTHORIZED = 401
CODE_IP_DENIED = 403
CODE_SERVER_ERROR = 500
CODE_DATA_ERROR = 600
CODE_DENIED = 805
CODE_NO_CREDIT = 888
CODE_RATE_LIMITED = 900
CODE_BAD_TOKEN = 903
CODE_NOT_REALNAME = 905

_CODE_HINTS = {
    CODE_NOT_LOGIN: "未登录",
    CODE_UNAUTHORIZED: "请求未授权",
    CODE_IP_DENIED: "请求 IP 未加入白名单",
    CODE_SERVER_ERROR: "PushPlus 系统异常",
    CODE_DATA_ERROR: "数据异常，操作失败",
    CODE_DENIED: "无权查看",
    CODE_NO_CREDIT: "积分不足，需要充值",
    CODE_RATE_LIMITED: "请求次数过多已被限流",
    CODE_BAD_TOKEN: "用户 token 无效",
    CODE_NOT_REALNAME: "账号未实名认证，无法发送",
}

_LOCK = asyncio.Lock()
_SENT: dict[str, float] = {}
_LAST_SEND_TS = 0.0

# --- 配置来源 ---------------------------------------------------------------
#
# 代理进程由 run_proxy.sh 启动，会 `set -a; . .env`，所以这些键就在 os.environ 里。
# 但**管理页面进程**的启动命令只显式传了一组必要变量（刻意不把 token 塞进进程环境，
# 免得它出现在 /proc/<pid>/environ 里），于是 os.environ 里没有 token ——
# 结果页面把"已配置 token"显示成"未启用"，页面内触发的登录成功推送也发不出去。
#
# 因此这里支持从 .env 文件读取：os.environ 优先（有则用，包括显式的空值/false），
# 缺失时回落到文件。文件按 (路径, mtime, size) 缓存，改完配置无需重启即生效。
_ENV_FILE_CACHE: tuple[str, float, int, dict[str, str]] = ("", 0.0, -1, {})


def _dotenv_path() -> str:
    for key in ("FNMUSIC_PUSHPLUS_ENV_FILE", "FNMUSIC_ADMIN_ENV_FILE"):
        v = (os.environ.get(key) or "").strip()
        if v:
            return v
    home = (os.environ.get("FNMUSIC_HOME") or "").strip()
    if home:
        return os.path.join(home, ".env")
    return ""


def _dotenv_values() -> dict[str, str]:
    global _ENV_FILE_CACHE
    path = _dotenv_path()
    if not path or not os.path.exists(path):
        return {}
    try:
        st = os.stat(path)
        stamp = (path, st.st_mtime, st.st_size)
    except OSError:
        return {}
    if _ENV_FILE_CACHE[0:3] == stamp:
        return _ENV_FILE_CACHE[3]

    values: dict[str, str] = {}
    try:
        from . import env_merge  # type: ignore

        kv, _other = env_merge.parse_env_file(path)
        values = dict(kv)
    except ImportError:
        try:
            import env_merge  # type: ignore

            kv, _other = env_merge.parse_env_file(path)
            values = dict(kv)
        except Exception:  # noqa: BLE001
            values = _parse_dotenv_minimal(path)
    except Exception:  # noqa: BLE001
        values = _parse_dotenv_minimal(path)

    _ENV_FILE_CACHE = (*stamp, values)
    return values


_DOTENV_RE: "re.Pattern[str] | None" = None


def _parse_dotenv_minimal(path: str) -> dict[str, str]:
    """env_merge 不可用时的兜底解析（pushplus 不该因为一个可选依赖而失效）。"""
    global _DOTENV_RE
    if _DOTENV_RE is None:
        import re

        _DOTENV_RE = re.compile(
            r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$"""
        )
    out: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                m = _DOTENV_RE.match(line)
                if not m:
                    continue
                v = m.group(2).strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1].replace("'\\''", "'")
                out[m.group(1)] = v
    except OSError:
        return {}
    return out


def reset_config_cache() -> None:
    """测试钩子 / 配置改写后强制重读。"""
    global _ENV_FILE_CACHE
    _ENV_FILE_CACHE = ("", 0.0, -1, {})


def _conf(key: str, default: str = "") -> str:
    """os.environ 优先（含显式空值），缺失才回落到 .env 文件。"""
    if key in os.environ:
        return os.environ[key]
    return str(_dotenv_values().get(key, default))


def token() -> str:
    return _conf("FNMUSIC_PUSHPLUS_TOKEN").strip()


def enabled() -> bool:
    flag = (_conf("FNMUSIC_PUSHPLUS_ENABLED", "true") or "true").strip().lower()
    if flag not in ("true", "1", "yes", "on"):
        return False
    return bool(token())


def push_url() -> str:
    return (_conf("FNMUSIC_PUSHPLUS_URL", DEFAULT_URL).strip() or DEFAULT_URL).strip()


def template() -> str:
    tpl = _conf("FNMUSIC_PUSHPLUS_TEMPLATE", DEFAULT_TEMPLATE).strip().lower()
    return tpl or DEFAULT_TEMPLATE


def topic() -> str:
    return _conf("FNMUSIC_PUSHPLUS_TOPIC").strip()


def _fingerprint(title: str, content: str) -> str:
    raw = f"{title}\x00{content}\x00{token()}\x00{topic()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _prune_dedupe(now: float) -> None:
    stale = [k for k, ts in _SENT.items() if now - ts >= _DEDUPE_TTL]
    for k in stale:
        _SENT.pop(k, None)


def reset_throttle() -> None:
    """测试/登录状态翻转后清空节流窗口。"""
    global _LAST_SEND_TS
    _SENT.clear()
    _LAST_SEND_TS = 0.0


def code_hint(code: int) -> str:
    return _CODE_HINTS.get(code, "未知错误")


def _redact(text: Any) -> str:
    """服务端返回的文本可能回显请求里的 token，落日志前一律抹掉。"""
    s = str(text or "")[:200]
    tok = token()
    if tok and tok in s:
        s = s.replace(tok, "***")
    return s


async def send(
    http_client: httpx.AsyncClient | None,
    title: str,
    content: str,
    *,
    tpl: str | None = None,
    force: bool = False,
) -> bool:
    """发送一条推送。返回 True 表示 PushPlus 已受理（code==200）。

    永不抛异常：推送失败只记日志，绝不影响音乐播放主链路。
    """
    global _LAST_SEND_TS

    if not enabled():
        return False
    if not (title or content):
        return False

    now = time.time()
    fp = _fingerprint(title, content)

    async with _LOCK:
        if not force:
            _prune_dedupe(now)
            last = _SENT.get(fp)
            if last is not None:
                logger.debug("pushplus skip duplicate within ttl: %s", title)
                return False
            wait = _MIN_INTERVAL_S - (now - _LAST_SEND_TS)
            if wait > 0:
                await asyncio.sleep(min(wait, _MIN_INTERVAL_S))

        payload = {
            "token": token(),
            "title": title[:100],
            "content": content,
            "template": tpl or template(),
        }
        topic_val = topic()
        if topic_val:
            payload["topic"] = topic_val

        own_client = http_client is None
        client = http_client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)
        try:
            resp = await client.post(push_url(), json=payload, timeout=REQUEST_TIMEOUT_S)
            status = resp.status_code
            body: Any = {}
            if status < 500:
                try:
                    body = resp.json()
                except Exception:
                    body = {}
        except Exception as exc:
            logger.warning("pushplus request failed: %s", exc)
            return False
        finally:
            if own_client:
                try:
                    await client.aclose()
                except Exception:
                    pass

            _LAST_SEND_TS = time.time()
            _SENT[fp] = _LAST_SEND_TS

        if not isinstance(body, dict):
            logger.warning("pushplus unexpected response: http=%s body=%s", status, _redact(body))
            return False

        try:
            code = int(body.get("code") or 0)
        except (TypeError, ValueError):
            code = 0

        if code == CODE_OK:
            logger.info("pushplus sent: %s (flow=%s)", title, _redact(body.get("data")))
            return True

        logger.warning(
            "pushplus rejected code=%s hint=%s msg=%s",
            code,
            code_hint(code),
            _redact(body.get("msg") or body.get("data")),
        )
        # token 无效/未实名属于永久性配置错误，清掉指纹避免每分钟空转重试
        if code in (CODE_BAD_TOKEN, CODE_NOT_REALNAME, CODE_UNAUTHORIZED):
            _SENT.pop(fp, None)
            logger.error(
                "PushPlus 配置有误（%s），已停止后续重试；请到 pushplus.plus 核对 token 与实名状态。",
                code_hint(code),
            )
        return False
