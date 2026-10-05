"""飞牛开放能力网关客户端（trim open gateway / api-scope）。

背景（本模块的由来）
--------------------
之前本地曲库是「以 root 身份直接硬读 /vol1/... 目录」实现的。这在飞牛的开发
规范里是不合规的：应用访问用户存储空间中的文件夹前，必须**先获得授权**，由
系统把目标路径的 ACL 授予应用账号，之后应用才能实际访问。以 root 硬读虽在
多数机器上"碰巧能用"，但：

  * 管理员在应用设置里根本看不到「授权目录」入口（manifest 里
    disable_authorization_path=true 把它关掉了），无法合规地授权；
  * 一旦系统收紧或应用改用非 root 运行身份，读取直接 PermissionError，
    表现就是「本地每日推荐歌单不出现」且日志里查不到原因。

正确姿势（依据 https://developer.fnnas.com/api/overview/ ）
----------------------------------------------------------
1. 应用包 config/resource 声明需要的 api-scope：
     {"api-scope": ["trim.file.sharedAccess", "trim.file.userAccess"]}
2. 需要 JS SDK 时，manifest 声明 micro_app=true。
3. **后端**通过 Unix Socket /var/run/trim_open_gateway_apiscope.socket 调
   POST /api/v1/trimapp，Header 带 Authorization: Bearer <TRIM_API_TOKEN>。
   token 由系统在调用应用脚本时注入环境变量，**每次调用都要重新读**，
   绝不持久化（重装/重注册后会变）。
4. 管理员授权后，后端用 trim.file.getSharedAccessibleFolders 查询授权目录，
   用这些目录（而不是自己猜的路径）作为本地曲库来源。

设计约束
--------
* 零第三方依赖：标准库 socket 手写 HTTP/1.1（代理环境里没有 requests 的
  unix socket 适配器）。
* 永不抛异常：网关不存在/没授权/超时都只是"没有授权信息"，退化到既有逻辑，
  绝不能因为查授权把主流程搞挂。
* 结果短缓存（默认 60s）：授权目录不会频繁变，但改完设置希望能很快看到。
"""

from __future__ import annotations

import glob
import json
import logging
import os
import socket
import time
import uuid

logger = logging.getLogger("app")

GATEWAY_SOCKET = "/var/run/trim_open_gateway_apiscope.socket"
GATEWAY_PATH = "/api/v1/trimapp"
TOKEN_ENV = "TRIM_API_TOKEN"
APP_NAME_ENV = "TRIM_APPNAME"

# 老版本 fnOS 没有 apiscope 网关，会把管理员授权的目录直接塞进环境变量
# （分号或冒号分隔）。一并读取，兼容 1.1.x / 1.2.x 两种形态。
SHARE_PATHS_ENV = "TRIM_DATA_SHARE_PATHS"
# 真机（fnOS 1.2.0604）环境变量清单里确实有这两个键：
#   TRIM_DATA_SHARE_PATHS      管理员在「应用设置 → 授权目录」里授权的共享目录
#   TRIM_DATA_ACCESSIBLE_PATHS 应用实际可访问的路径
# 网关查不动时，它们就是授权状态最权威的兜底来源，两个都要读。
ACCESSIBLE_PATHS_ENV = "TRIM_DATA_ACCESSIBLE_PATHS"

DEFAULT_TIMEOUT = 3.0

# 这几类错误换了 appName / req 名也一样，不值得重试：
#   200001 参数错 / 200003 Forbidden / 200004 Unauthorized / 200005 Not Found
NO_RETRY_CODES = {200001, 200003, 200004, 200005}

# 查「管理员授权了哪些目录」的 req 名。文档写的是第一个，但真机上它稳定返回
# 200006 Internal Error；网关按 req 名分发，名字对不上就是内部错误，所以把
# 可能的别名都试一遍，哪个通就用哪个（只在环境变量没给答案时才需要查）。
SHARED_REQ_CANDIDATES = (
    "trim.file.getSharedAccessibleFolders",
    "trim.file.sharedAccess",
    "trim.file.getSharedFolders",
)

_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 60.0

# 最近一次「要不要查网关 / 查了没有 / 查成啥样」的状态，给诊断页展示用。
# skipped=True 表示系统已经通过环境变量给了授权目录，我们把网关跳过了。
_GATEWAY_LAST: dict[str, object] = {}


def gateway_last_state() -> dict:
    """最近一次网关处理状态（skipped / req / code / msg）。"""
    return dict(_GATEWAY_LAST)


def app_name() -> str:
    """当前应用名（后端 API 请求体需要 appName）。

    ⚠️ 真机实锤：``TRIM_APPNAME`` **并不总是被注入**。回退到硬编码 "fnmusicext"
    时，如果系统内部登记的应用名不是这个写法，网关就会返回
    ``code=200006 "Internal Error"``（业务模块内部错误）——因为它按 appName
    找不到对应的授权记录。
    """
    return candidate_app_names()[0]


def candidate_app_names() -> "list[str]":
    """按可信度从高到低列出系统可能认的应用名，逐个试。

    最权威的是**系统注入的应用数据目录**——形如 ``/vol1/@appdata/<appname>``，
    最后一段就是系统登记的应用名，比任何硬编码都可靠。
    """
    out: list[str] = []

    def _add(v: str) -> None:
        v = str(v or "").strip()
        if v and v not in out:
            out.append(v)

    for var in (APP_NAME_ENV, "TRIM_APP_NAME", "TRIM_APPID", "TRIM_APP_ID"):
        _add(os.environ.get(var) or "")
    # 系统注入的应用目录：末段即应用名
    for var in ("TRIM_PKGVAR", "TRIM_PKGMETA", "TRIM_PKGETC", "TRIM_PKGHOME"):
        base = str(os.environ.get(var) or "").strip().rstrip("/")
        if base:
            _add(os.path.basename(base))
    # 末段若带 "fnnas." 之类前缀，也试一下去掉前缀的写法
    for name in list(out):
        if "." in name:
            _add(name.rsplit(".", 1)[-1])
    _add("fnmusicext")
    return out


def _cached(key: str):
    hit = _CACHE.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return val


def _put(key: str, val: object) -> object:
    _CACHE[key] = (time.time(), val)
    return val


def invalidate_cache() -> None:
    """管理页点「刷新授权状态」时调用，强制下次重新查网关。"""
    _CACHE.clear()


# ---------------------------------------------------------------------------
# 底层：Unix Socket 上的 HTTP/1.1
# ---------------------------------------------------------------------------

def _http_post(payload: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """向开放网关发一个 POST /api/v1/trimapp，返回解析后的 JSON。

    失败一律返回 {"code": <非0>, "msg": "..."} 而不是抛异常——调用方只需要
    关心「拿到了什么 / 没拿到」，不需要处理网络异常。
    """
    token = str(os.environ.get(TOKEN_ENV) or "").strip()
    if not token:
        return {"code": -1, "msg": f"环境变量 {TOKEN_ENV} 未注入（非由系统脚本启动？）"}
    if not os.path.exists(GATEWAY_SOCKET):
        return {"code": -2, "msg": f"开放网关不存在: {GATEWAY_SOCKET}"}

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = (
        f"POST {GATEWAY_PATH} HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Authorization: Bearer {token}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("utf-8") + body

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(GATEWAY_SOCKET)
            sock.sendall(req)
            chunks: list[bytes] = []
            while True:
                try:
                    buf = sock.recv(65536)
                except socket.timeout:
                    break
                if not buf:
                    break
                chunks.append(buf)
    except FileNotFoundError:
        return {"code": -2, "msg": f"开放网关不存在: {GATEWAY_SOCKET}"}
    except PermissionError as exc:
        return {"code": -3, "msg": f"无权连接开放网关: {exc}"}
    except OSError as exc:
        return {"code": -4, "msg": f"{type(exc).__name__}: {exc}"}

    raw = b"".join(chunks)
    if not raw:
        _remember_raw("(空响应)")
        return {"code": -5, "msg": "网关返回空响应"}
    try:
        head, _, tail = raw.partition(b"\r\n\r\n")
        text = tail.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        _remember_raw(f"解码失败: {exc}")
        return {"code": -6, "msg": f"响应解码失败: {exc}"}
    # 状态行对排错很关键（Internal Error 可能是 200 也可能是 500），以前直接丢了
    status_line = head.split(b"\r\n", 1)[0].decode("utf-8", "replace").strip()
    _remember_raw(f"{status_line} | {text[:400]}")
    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001
        snippet = text[:160].replace("\n", " ")
        return {"code": -7, "msg": f"响应不是 JSON: {snippet}", "http_status": status_line}
    if isinstance(parsed, dict):
        parsed.setdefault("http_status", status_line)
        return parsed
    return {"code": -7, "msg": f"响应不是 JSON 对象: {str(parsed)[:160]}",
            "http_status": status_line}


def call(req: str, data: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """调用一个后端能力。req 形如 trim.file.getSharedAccessibleFolders。

    appName 可能有多种写法（TRIM_APPNAME 常不注入），逐个试到通为止——
    网关按 appName 查授权记录，名字对不上就 200006 Internal Error。
    只有"内部错误/未知错误"才值得换名字重试；参数错、scope 不足、token 无效
    这几类换了也一样，别浪费时间。
    """
    _NOT_WORTH_RETRY = NO_RETRY_CODES
    names = candidate_app_names()
    last: dict | None = None
    for idx, name in enumerate(names):
        payload = {
            "reqId": uuid.uuid4().hex[:16],
            "req": req,
            "appName": name,
            "data": data or {},
        }
        resp = _http_post(payload, timeout=timeout)
        code = int(resp.get("code") or 0)
        if code == 0:
            resp["app_name_used"] = name
            if idx:
                logger.info("开放网关调用成功：appName 用的是 %r（第 %d 个候选）", name, idx + 1)
            return resp
        resp["app_name_used"] = name
        last = resp
        if code in _NOT_WORTH_RETRY:
            break
        if idx:
            logger.debug("开放网关 appName=%r 失败(%s %s)，换下一个候选",
                         name, code, resp.get("msg") or "")
    if last is not None:
        return last
    return {"code": -1, "msg": "没有可用的 appName 候选"}


# ---------------------------------------------------------------------------
# 网关原始响应（排错用）
#
# Internal Error 是个笼统错误，光看 code/msg 完全无从下手。把状态行和响应体
# 原文留一份给诊断页，用户贴日志时就能一眼看到真实原因。
# ---------------------------------------------------------------------------

_LAST_RAW: dict[str, str] = {}


def _remember_raw(text: str) -> None:
    _LAST_RAW["raw"] = str(text or "")[:600]


def trim_env_report() -> dict:
    """系统实际注入了哪些 TRIM_* 环境变量（值默认不展示，避免泄露 token）。

    排 appName 全靠它：真机上 ``TRIM_APPNAME`` 常常压根不存在，只能从
    ``TRIM_PKGVAR`` / ``TRIM_PKGMETA`` 这类**应用数据目录**的末段去推。
    把清单列出来，一眼就能看出系统到底给了什么。
    """
    secret_markers = ("TOKEN", "SECRET", "PASS", "KEY", "CRED")
    shown: dict[str, str] = {}
    names: list[str] = []
    for k in sorted(os.environ):
        if not k.startswith("TRIM_"):
            continue
        names.append(k)
        v = str(os.environ.get(k) or "")
        if any(m in k.upper() for m in secret_markers):
            shown[k] = f"★ 已注入（{len(v)} 字符，不展示）" if v else "（空）"
        else:
            shown[k] = v[:200]
    return {"names": names, "values": shown}


def last_raw_response() -> str:
    """最近一次网关调用的原始响应（状态行 + 响应体片段）。"""
    return str(_LAST_RAW.get("raw") or "")


# ---------------------------------------------------------------------------
# 授权目录查询
# ---------------------------------------------------------------------------

def _split_paths(raw: str) -> list[str]:
    out: list[str] = []
    for part in str(raw or "").replace(";", ":").split(":"):
        p = part.strip()
        if p and p not in out:
            out.append(p)
    return out


def env_share_paths() -> list[str]:
    """兼容老版本/网关故障：环境变量里的管理员授权目录。"""
    return _split_paths(os.environ.get(SHARE_PATHS_ENV) or "") + \
        _split_paths(os.environ.get(ACCESSIBLE_PATHS_ENV) or "")


def config_share_paths() -> list[str]:
    """从应用自己的配置目录里读 share_paths。

    真机实锤：部分版本（或系统版本低于 1.2.0401）不会注入 TRIM_API_TOKEN，
    网关查不了，但管理员在「应用设置 → 授权目录」里加的目录会落在应用配置
    目录下的 share_paths 文件里。这时它就是授权状态的权威来源，必须认。
    """
    app = app_name()
    cands: list[str] = []
    for var in ("TRIM_PKGVAR", "TRIM_PKGETC", "TRIM_PKGHOME", "TRIM_PKGMETA"):
        base = str(os.environ.get(var) or "").strip()
        if base:
            cands.append(os.path.join(base, "share_paths"))
            cands.append(os.path.join(base, ".share_paths"))
    for pat in (f"/vol*/@appdata/{app}/share_paths",
                f"/vol*/@appconf/{app}/share_paths",
                f"/vol*/@appdata/{app}/.share_paths",
                f"/vol*/@apphome/{app}/.share_paths",
                f"/usr/local/apps/@appdata/{app}/share_paths"):
        try:
            cands.extend(sorted(glob.glob(pat)))
        except Exception:  # noqa: BLE001
            continue
    out: list[str] = []
    for path in cands:
        try:
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8") as fh:
                for p in _split_paths(fh.read()):
                    if p not in out:
                        out.append(p)
        except Exception:  # noqa: BLE001
            continue
    return out


def _shared_req_attempts() -> "list[tuple[str, dict]]":
    """(req, data) 尝试序列。

    getSharedAccessibleFolders 真机上回报 200006，除了换 req 名，也试一次带
    uid 的调用（飞牛很多文件类接口都强制要 uid），万一就是这个差别呢。
    """
    out: list[tuple[str, dict]] = []
    uid = str(os.environ.get("TRIM_UID") or "").strip()
    for req in SHARED_REQ_CANDIDATES:
        out.append((req, {}))
    if uid.isdigit():
        out.insert(1, (SHARED_REQ_CANDIDATES[0], {"uid": int(uid)}))
    return out


def probe_shared_via_gateway() -> dict:
    """真去查一次网关（不查缓存）。返回最后一次响应，成功时带 req_used。"""
    last: dict = {"code": -1, "msg": "网关不可用"}
    for idx, (req, data) in enumerate(_shared_req_attempts()):
        resp = call(req, data or None)
        code = int(resp.get("code") or 0)
        if code == 0:
            resp["req_used"] = req
            if idx:
                logger.info("开放网关查询授权目录成功：req 用的是 %r（第 %d 个候选）", req, idx + 1)
            return resp
        last = resp
        if code in NO_RETRY_CODES:
            break
    return last


def shared_accessible_folders(force: bool = False) -> tuple[list[str], str]:
    """管理员为应用授权的共享目录。

    返回 (路径列表, 错误信息)。列表为空 + 错误信息不空 = 查询失败/未授权。

    **v2.9.13 的关键认知**：系统把 ACL 授权结果**直接写进了进程环境变量**
    ``TRIM_DATA_ACCESSIBLE_PATHS``（真机实测 =
    ``/vol1/1000/存储空间1/汇总音乐``，与管理员在「应用设置 → 授权目录」里
    勾选的完全一致）。这**就是官方授权的权威结果**，不是什么降级兜底——
    既然系统已经给了答案，就没必要再去打那个稳定 500 的网关，也省得诊断页
    天天挂着一行吓人的 Internal Error。
    """
    key = "shared"
    if not force:
        hit = _cached(key)
        if hit is not None:
            return hit  # type: ignore[return-value]

    env_paths = env_share_paths()
    fallback: list[str] = []
    for p in env_paths + config_share_paths():
        if p not in fallback:
            fallback.append(p)

    _GATEWAY_LAST.clear()
    if env_paths:
        # 系统已下发授权目录 → 官方授权成立，跳过网关
        _GATEWAY_LAST.update({
            "skipped": True,
            "reason": "系统已通过 " + ACCESSIBLE_PATHS_ENV + " 下发授权目录，无需查询网关",
        })
        return _put(key, (fallback, ""))  # type: ignore[return-value]

    resp = probe_shared_via_gateway()
    _GATEWAY_LAST.update({
        "skipped": False,
        "req": str(resp.get("req_used") or SHARED_REQ_CANDIDATES[0]),
        "code": int(resp.get("code") or 0),
        "msg": str(resp.get("msg") or ""),
    })
    if int(resp.get("code") or 0) != 0:
        # 网关查不动（没 token / 系统版本低）时，退到 share_paths 文件：
        # 管理员已经授权过的话这里能查到，不能因为查不了网关就报"未授权"。
        res: tuple[list[str], str] = (fallback, str(resp.get("msg") or "未知错误"))
    else:
        data = resp.get("data") or {}
        paths = [str(p) for p in (data.get("paths") or []) if p]
        for p in fallback:
            if p not in paths:
                paths.append(p)
        res = (paths, "")
    return _put(key, res)  # type: ignore[return-value]


def user_accessible_folders(uid: int, force: bool = False) -> tuple[list[str], str]:
    """指定用户授权给应用的目录（trim.file.getUserAccessibleFolders）。"""
    key = f"user:{uid}"
    if not force:
        hit = _cached(key)
        if hit is not None:
            return hit  # type: ignore[return-value]
    resp = call("trim.file.getUserAccessibleFolders", {"uid": int(uid)})
    if int(resp.get("code") or 0) != 0:
        res: tuple[list[str], str] = ([], str(resp.get("msg") or "未知错误"))
    else:
        data = resp.get("data") or {}
        res = ([str(p) for p in (data.get("paths") or []) if p], "")
    return _put(key, res)  # type: ignore[return-value]


def _existing(paths: list[str]) -> list[str]:
    return [p for p in paths if p and os.path.isdir(p)]


def authorized_report(force: bool = False) -> dict:
    """给管理页/诊断页用的一张授权状态快照。"""
    shared, shared_err = shared_accessible_folders(force=force)
    shared = _existing(shared)
    token_present = bool(str(os.environ.get(TOKEN_ENV) or "").strip())
    gateway_present = os.path.exists(GATEWAY_SOCKET)
    env_paths = _existing(env_share_paths())
    cfg_paths = _existing(config_share_paths())
    # 授权来源（权威度从高到低）：
    #   env     = 系统把 ACL 结果写进了 TRIM_DATA_ACCESSIBLE_PATHS —— **官方授权**，非降级
    #   gateway = 开放网关 trim.file.getSharedAccessibleFolders 查到的
    #   config  = 只从 share_paths 文件读到的（没有 env 也没有网关答案）
    #   none    = 完全查不到
    if env_paths:
        source = "env"
    elif token_present and not shared_err:
        source = "gateway"
    else:
        source = "config" if shared else "none"
    # 只有「连系统都没给授权目录」才算降级；env 是官方授权结果，不算。
    degraded = source in ("config", "none")

    report = {
        "gateway": {
            "socket": GATEWAY_SOCKET,
            "exists": gateway_present,
            "token_present": token_present,
            "app_name": app_name(),
        },
        "shared_paths": shared,
        "shared_error": shared_err,
        "source": source,
        "env_paths": env_paths,
        "config_paths": cfg_paths,
        "degraded": degraded,
        "authorized": bool(shared),
        "app_names": candidate_app_names(),
        "last_raw": last_raw_response(),
        "gateway_last": gateway_last_state(),
        "trim_env": trim_env_report(),
        "hint": _hint(shared, shared_err, gateway_present, token_present),
        "note": _note(source, token_present, gateway_present, shared_err),
    }
    # 环境变量已经给了答案时，用户点「刷新状态」仍然主动探一次网关，把真实
    # 结果摆出来（纯参考，不参与上面的来源判定，所以不会把已授权显示成故障）。
    if force and env_paths and gateway_present and token_present:
        resp = probe_shared_via_gateway()
        report["gateway_probe"] = {
            "req": str(resp.get("req_used") or SHARED_REQ_CANDIDATES[0]),
            "code": int(resp.get("code") or 0),
            "msg": str(resp.get("msg") or ""),
            "raw": last_raw_response(),
        }
    return report


def _note(source: str, token_present: bool, gateway_present: bool, shared_err: str = "") -> str:
    """非「网关直查」时的解释性说明。降级不该被当成故障报错——曲库照样读得动。"""
    if source == "gateway":
        return ""
    if source == "env":
        return (f"系统已把授权结果直接下发到本进程的环境变量 {ACCESSIBLE_PATHS_ENV}"
                f"（= {', '.join(_existing(env_share_paths())) or '（空）'}），"
                "这就是飞牛官方授权的权威结果，不是降级、功能完全正常，因此不再查询网关。")
    if source == "config":
        return ("已从应用配置（share_paths）读到授权目录，功能正常。"
                + ("" if token_present else
                   "系统未向本进程注入 TRIM_API_TOKEN（系统版本较低或需重装应用以注册 api-scope），"
                   "因此改用配置文件判定授权，不影响使用。"))
    if not token_present:
        return ("系统未向本进程注入 TRIM_API_TOKEN（系统版本较低或需重装应用以注册 api-scope），"
                "无法自动查询授权目录；若你已在应用设置里授权，请重启本应用后重试。")
    if shared_err and gateway_present:
        return (f"网关已连通、token 已注入，但查询授权目录失败：{shared_err}。"
                f"最常见的原因是 appName 与系统登记的不一致（已自动尝试 "
                f"{'/'.join(candidate_app_names()[:3])} 等候选）。"
                "若你在应用设置里已授权，可在管理页直接填写「本地曲库目录」，功能不受影响。")
    return ""


def _hint(shared: list[str], err: str, gateway_present: bool, token_present: bool) -> str:
    """只有在**确实一个授权目录都没有**时才给指引；能读到就不打扰用户。"""
    if shared:
        return ""
    if not gateway_present and not token_present:
        return ("本机未发现飞牛开放网关/TRIM_API_TOKEN（系统版本低于 1.2.0401 时无此能力）。"
                "请到「应用设置 → 授权目录」添加音乐目录，或在管理页手动填写「本地曲库目录」。")
    if not token_present:
        return ("进程环境里没有 TRIM_API_TOKEN，无法查询授权目录。"
                "请在「应用设置 → 授权目录」添加你的音乐目录；若已添加，重启本应用后再刷新，"
                "或直接在管理页填写「本地曲库目录」。")
    if err and "仅管理员" in err:
        return "需管理员操作：在应用设置→授权目录里添加曲库目录后重试。"
    return "尚未授权任何目录。请到「应用设置 → 授权目录」添加你的音乐目录（或在管理页手动填写「本地曲库目录」）。"


def pick_library_from_authorized(candidates: list[str], authorized: list[str]) -> str:
    """在候选目录里挑一个**已被授权**的。

    规则：候选路径本身等于某个授权目录，或是某个授权目录的子路径。
    挑不到就返回空串——调用方据此回退并明确告警，绝不静默使用未授权路径。
    """
    auth = [os.path.abspath(p) for p in authorized if p]
    for cand in candidates:
        if not cand or not os.path.isdir(cand):
            continue
        ap = os.path.abspath(cand)
        for a in auth:
            if ap == a or ap.startswith(a.rstrip(os.sep) + os.sep):
                return cand
    return ""
