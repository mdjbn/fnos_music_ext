"""网易云登录态探测、降级门控与 PushPlus 提醒。

整个扩展只有一个网易云私人账号（扫码登录的那一个）。该账号决定了：

- **已登录**：走账号自身权益，VIP / 无损 / 付费专辑曲目都能拿到真实直链，
  并且可以拉取「每日推荐」歌单；
- **未登录 / cookie 过期**：降级为只播免费曲目（``FNMUSIC_FREE_ONLY_ON_LOGOUT``），
  保证飞牛音乐基础可用，同时通过 PushPlus 推送提醒用户重新扫码。

登录态带 TTL 缓存，避免每次搜索都去打一次 musicbox /auth/status。
状态翻转（登录 ↔ 掉线）与 VIP 临期都会触发推送，节流由 pushplus 模块负责。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

logger = logging.getLogger("fnmusic_proxy.netease_auth")

try:  # 作为包导入（proxy.app）
    from . import pushplus as _pushplus_mod
except ImportError:  # uvicorn --app-dir proxy 扁平导入
    import pushplus as _pushplus_mod  # type: ignore


def _pushplus():
    return _pushplus_mod

STATE_TTL_S = float(os.environ.get("FNMUSIC_LOGIN_STATE_TTL", "300"))
CHECK_INTERVAL_S = float(os.environ.get("FNMUSIC_LOGIN_CHECK_INTERVAL", "3600"))
VIP_WARN_DAYS = int(os.environ.get("FNMUSIC_VIP_WARN_DAYS", "7"))
STATUS_TIMEOUT_S = 5.0


def free_only_on_logout() -> bool:
    return (os.environ.get("FNMUSIC_FREE_ONLY_ON_LOGOUT") or "true").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


def daily_enabled() -> bool:
    return (os.environ.get("FNMUSIC_DAILY_ENABLED") or "true").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


@dataclass
class LoginState:
    logged_in: bool = False
    nickname: str = ""
    user_id: str = ""
    vip_type: int = 0
    vip_expires_ms: int = 0
    # 上游是否真的提供了到期时间。NEMbox 0.5.3 没有任何返回 VIP 到期时间的接口
    # （get_account_info 只给 vipType），因此这个值经常拿不到——
    # 拿不到时必须如实说"未知"，绝不能显示成"剩余 0 天"或臆造一个日期。
    vip_expires_known: bool = False
    # 0.0 表示"尚未探测过"：模块级哨兵必须用它，否则初始状态会被当成新鲜缓存，
    # 导致启动后一个 TTL 周期内 fetch_state 根本不查上游、一直误报未登录。
    checked_at: float = field(default_factory=time.time)
    error: str = ""
    probed_via: str = ""

    @property
    def vip_active(self) -> bool:
        if not self.logged_in:
            return False
        if self.vip_type <= 0:
            return False
        if self.vip_expires_ms and self.vip_expires_ms < time.time() * 1000:
            return False
        return True

    @property
    def vip_days_left(self) -> int | None:
        if not self.vip_expires_ms or not self.vip_active:
            return None
        return int((self.vip_expires_ms / 1000.0 - time.time()) // 86400)

    def to_public_dict(self) -> dict:
        """healthz 对外可见字段，不含任何凭据。"""
        return {
            "logged_in": self.logged_in,
            "nickname": self.nickname,
            "vip": self.vip_active,
            "vip_type": self.vip_type if self.logged_in else 0,
            "vip_days_left": self.vip_days_left,
            "vip_expires_known": bool(self.vip_expires_known),
            "free_only": (not self.logged_in) and free_only_on_logout(),
            "checked_at": int(self.checked_at),
            "age_s": int(time.time() - self.checked_at),
        }


_STATE = LoginState(checked_at=0.0)
_LOCK = asyncio.Lock()
_TASK: asyncio.Task | None = None
_LAST_ALERT: dict[str, str] = {}
_VIP_ALERT_DAY: str = ""


def current_state() -> LoginState:
    return _STATE


def invalidate_state() -> None:
    """作废缓存，强制下一次 fetch_state 真的去查上游。"""
    global _STATE
    _STATE = LoginState(checked_at=0.0, error="invalidated")


def _first_str(d: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def _first_int(d: dict, keys: tuple[str, ...]) -> int:
    for k in keys:
        v = d.get(k)
        if v in (None, ""):
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return 0


def parse_status_payload(payload: Any, *, via: str = "auth/status") -> LoginState:
    """解析登录态载荷，兼容多种信封结构。

    ``/api/v1/auth/detail``（走进程内 NEMbox，字段全）与
    ``/api/v1/auth/status``（走 musicbox CLI，只有 logged_in/nickname/user_id）
    都能解析；后者拿不到 VIP 字段时如实留空，不补 0 以外的猜测值。
    """
    data: dict = {}
    if isinstance(payload, dict):
        inner = payload.get("data")
        if isinstance(inner, dict):
            data = inner
        else:
            data = payload
    if not isinstance(data, dict):
        return LoginState(probed_via=via)

    profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    src = profile or account or data

    logged_in = data.get("logged_in")
    if logged_in is None:
        logged_in = bool(profile or account or data.get("user_id") or data.get("nickname"))
    else:
        logged_in = bool(logged_in)

    expires_ms = _first_int(src, ("vipExpiryTime", "vipExpires", "vip_expire", "vipExpiry"))
    if not expires_ms:
        expires_ms = _first_int(data, ("vip_expires_ms", "vipExpiryTime", "vip_expires_ms"))
        if not expires_ms:
            expires_ms = _first_int(account, ("vipExpiryTime", "vip_expire"))

    # 到期时间是否可信：必须是个未来的毫秒时间戳
    expires_known = bool(expires_ms and expires_ms > time.time() * 1000)

    return LoginState(
        logged_in=bool(logged_in),
        nickname=_first_str(src, ("nickname", "userName", "nick_name", "name"))
        or _first_str(data, ("nickname",)),
        user_id=_first_str(src, ("userId", "user_id", "id")) or _first_str(data, ("user_id", "userId")),
        vip_type=_first_int(src, ("vipType", "vip_type"))
        or _first_int(account, ("vipType", "vip_type"))
        or _first_int(data, ("vip_type",)),
        vip_expires_ms=expires_ms if expires_known else 0,
        vip_expires_known=expires_known,
        checked_at=time.time(),
        probed_via=via,
    )


async def fetch_state(client: httpx.AsyncClient | None, *, force: bool = False) -> LoginState:
    """带 TTL 缓存地读取登录态。

    探测顺序：``/api/v1/auth/detail``（进程内 NEMbox，含 VIP 字段）
    → ``/api/v1/auth/status``（CLI，字段少，兜底）。
    只探后者的话，VIP 状态恒为"非 VIP"——那是 v2.1.2 之前的实际故障。
    """
    global _STATE

    now = time.time()
    if not force and _STATE.error != "invalidated" and (now - _STATE.checked_at) < STATE_TTL_S:
        return _STATE

    async with _LOCK:
        # 双重检查：等锁期间可能已被别的协程刷新
        now = time.time()
        if not force and (now - _STATE.checked_at) < STATE_TTL_S and _STATE.error != "invalidated":
            return _STATE

        if client is None:
            _STATE = LoginState(checked_at=now, error="no_client")
            return _STATE

        last_error = ""
        for path, via in (("/api/v1/auth/detail", "auth/detail"),
                          ("/api/v1/auth/status", "auth/status")):
            try:
                r = await client.get(path, timeout=STATUS_TIMEOUT_S)
            except Exception as exc:
                last_error = "unreachable"
                logger.warning("netease %s probe failed: %s", via, exc)
                continue
            if r.status_code != 200:
                last_error = f"http_{r.status_code}"
                logger.info("netease %s probe returned %s", via, r.status_code)
                continue
            try:
                state = parse_status_payload(r.json(), via=via)
            except Exception as exc:
                last_error = "bad_json"
                logger.warning("netease %s payload unparsable: %s", via, exc)
                continue
            if state.logged_in or via == "auth/status":
                _STATE = state
                return _STATE
            # detail 说未登录时也接受它（字段更全），但记下来以便兜底端点复核
            _STATE = state
            last_error = ""
            return _STATE

        _STATE = LoginState(checked_at=now, error=last_error or "unreachable",
                            probed_via="failed")
        return _STATE


async def require_login(client: httpx.AsyncClient | None) -> bool:
    st = await fetch_state(client)
    return st.logged_in


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _login_url_hint() -> str:
    """推送里给出的重新登录指引。"""
    return (
        "在 NAS 上执行 `./netease_login.sh`（或 `./extend.sh --qr`）终端扫码；"
        "也可用浏览器打开 `http://<NAS_IP>:8770/api/v1/auth/login/qr.png` 扫码。"
    )


async def _notify_logout(prev: LoginState, now_state: LoginState) -> None:
    pushplus = _pushplus()
    if prev.logged_in and not now_state.logged_in:
        title = "网易云登录已失效"
        content = (
            f"### 网易云登录态丢失\n\n"
            f"- 原登录账号：**{prev.nickname or prev.user_id or '未知'}**\n"
            f"- 检测时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"- 当前行为：降级为**仅免费曲目**，VIP / 无损曲目已不可播\n\n"
            f"**恢复方式**：{_login_url_hint()}"
        )
    else:
        title = "网易云尚未登录"
        content = (
            f"### 飞牛音乐扩展尚未登录网易云\n\n"
            f"- 当前行为：仅能播放免费曲目，「每日推荐」不可用\n"
            f"- 检测时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"**登录方式**：{_login_url_hint()}\n\n"
            f"登录后即可使用你私人账号的 VIP / 无损音源与每日推荐。"
        )
    await pushplus.send(None, title, content)


async def _notify_login(state: LoginState) -> None:
    pushplus = _pushplus()

    vip = ""
    if state.vip_days_left is not None:
        vip = f"，VIP 剩余 {state.vip_days_left} 天"
    content = (
        f"### 网易云登录成功\n\n"
        f"- 账号：**{state.nickname or state.user_id}**{vip}\n"
        f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- 已恢复：VIP / 无损曲库 + 每日推荐歌单"
    )
    await pushplus.send(None, "网易云登录成功", content)


async def _notify_vip_expiring(state: LoginState) -> None:
    pushplus = _pushplus()

    global _VIP_ALERT_DAY
    days = state.vip_days_left
    if days is None or days > VIP_WARN_DAYS:
        return
    today = _today()
    if _VIP_ALERT_DAY == today:
        return
    _VIP_ALERT_DAY = today
    when = datetime.fromtimestamp(state.vip_expires_ms / 1000.0).strftime("%Y-%m-%d")
    content = (
        f"### 网易云 VIP 即将到期\n\n"
        f"- 账号：**{state.nickname or state.user_id}**\n"
        f"- 到期日：**{when}**（剩余 {days} 天）\n"
        f"- 影响：到期后 VIP / 无损曲目将降级为不可播，仅保留免费曲目"
    )
    await pushplus.send(None, f"网易云 VIP 将在 {days} 天后到期", content)


async def refresh_and_notify(client: httpx.AsyncClient | None, *, force: bool = True) -> LoginState:
    """刷新登录态，并在状态翻转 / VIP 临期时推送。供后台巡检和 healthz 调用。"""
    pushplus = _pushplus()

    prev = _STATE
    state = await fetch_state(client, force=force)

    try:
        if not pushplus.enabled():
            return state

        key = "offline" if not state.logged_in else "online"
        if prev.logged_in != state.logged_in:
            _LAST_ALERT.clear()
        if key in _LAST_ALERT:
            return state

        if not state.logged_in:
            await _notify_logout(prev, state)
        else:
            if prev.checked_at and not prev.logged_in:
                await _notify_login(state)
            await _notify_vip_expiring(state)
        _LAST_ALERT[key] = state.nickname or state.user_id or "1"
    except Exception as exc:
        logger.warning("netease login notify failed: %s", exc)
    return state


async def _watch_loop(client: httpx.AsyncClient | None, stop_event: asyncio.Event) -> None:
    """后台巡检：定期检查登录态并推送变化。"""
    pushplus = _pushplus()

    # 启动后先给音源服务一点起来的时间
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=20.0)
        return
    except asyncio.TimeoutError:
        pass

    while not stop_event.is_set():
        try:
            await refresh_and_notify(client, force=True)
        except Exception as exc:
            logger.debug("login watch iteration failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL_S)
            return
        except asyncio.TimeoutError:
            continue


def start_watch(client: httpx.AsyncClient | None, stop_event: asyncio.Event) -> asyncio.Task:
    global _TASK
    if _TASK is not None and not _TASK.done():
        return _TASK
    _TASK = asyncio.create_task(_watch_loop(client, stop_event))
    return _TASK


def stop_watch() -> None:
    global _TASK
    if _TASK is not None and not _TASK.done():
        _TASK.cancel()
    _TASK = None


def reset_for_test() -> None:
    """测试钩子：清空全局状态，避免用例之间互相污染。"""
    global _STATE, _VIP_ALERT_DAY
    _STATE = LoginState(checked_at=0.0)
    _LAST_ALERT.clear()
    _VIP_ALERT_DAY = ""
    stop_watch()
