"""按飞牛「音质偏好 / 网络类型」动态决定向网易云要哪一档音质。

## 为什么不直接读飞牛的设置接口

飞牛的音质偏好（WiFi 用原始/标准、流量用原始/标准）落在哪个接口、哪个库表、哪个字段，
**我们没有可靠证据**：没有公开契约文档，而猜一个接口名或字段名去读，猜错时不会报错，
只会静默地一直走默认音质——用户以为"跟随飞牛"生效了，其实从来没生效过。这类静默失效
比明确不支持更难排查。

所以本模块采取「**可发现 + 可报告 + 有手动兜底**」：

1. **手动策略**（管理页）永远可用，是确定生效的那条路；
2. **被动发现**只依据我们真正观察到的东西——客户端请求里出现过的 query/header 键、
   ``music.db`` 里 schema 容错扫描出来的偏好行——**不预设任何接口名或字段名**；
3. 观察到的证据全部如实上报（``report()`` → 诊断页的 quality 段），因此「有没有读到、
   从哪儿读到的、原始值是什么」在真机上一眼可见，需要进一步适配时有据可依。

## 飞牛档位 → 网易云档位

飞牛只有「原始 / 标准」两档，网易云有 6 档，不是一一对应。映射刻意保守：
原始＝尽可能高（lossless），标准＝320k（exhigh）。把「标准」映射成 128k（standard）
会掉得太狠；映射成无损又失去省流量的意义。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Any

logger = logging.getLogger("fnmusic_proxy")

# 与 musicbox 侧 QUALITY_WHITELIST 一致，顺序由高到低
LEVELS = ("jymaster", "hires", "lossless", "exhigh", "higher", "standard")

LEVEL_HINTS = {
    "jymaster": ("臻品", "母带", "master", "jymaster"),
    "hires": ("hires", "hi-res", "高清无损"),
    "lossless": ("lossless", "无损", "flac", "原始", "original", "highest"),
    "exhigh": ("exhigh", "极高", "320", "高音质", "标准", "standard_quality"),
    "higher": ("higher", "较高", "192"),
    "standard": ("standard", "流畅", "128", "省流量", "low"),
}
# ⚠️ 这里有一处**真实的语义歧义**，取舍是刻意的：
# 飞牛的「标准音质」映射到 exhigh(320k)，而网易云自己也有一个叫 `standard` 的档位，
# 含义是 128k —— 二者字面相近却差两档。处理规则：
#   - **精确等于**网易云档位名时按网易云原义解析（``_norm_level`` 先查 LEVELS）：
#     来源若用的就是网易云词汇，照搬其含义最不容易出错；
#   - 只有中文标签「原始 / 标准」以及 original / 无损 / 省流量这类**飞牛语义**，
#     才按上面的映射表落到 lossless / exhigh。
# 飞牛设置页的实际文案是中文（原始音质 / 标准音质），走的是第二条，符合用户预期。

_CELLULAR = ("cellular", "cell", "4g", "5g", "mobile", "wwan", "lte", "流量", "蜂窝")
_WIFI = ("wifi", "wi-fi", "wlan", "wireless", "无线")

# 请求里可能承载"音质/网络"语义的键名子串。**不预设完整键名**，命中就记录原值，
# 用来在真机上发现飞牛到底传了什么。
_HINT_KEYS = ("quality", "bitrate", "network", "nettype", "net_type",
              "audiotype", "audio_type", "prefer", "transcode")

DEFAULT_POLICY = "by_lan"
POLICIES = ("follow_fnos", "fixed", "by_network", "by_lan")


def on_lan(network: str) -> bool:
    """是否**明确**处于局域网；判不出来（unknown）一律不算。

    ``by_lan`` 这条策略的全部价值就落在 unknown 这一档上：

    - ``by_network`` 把 unknown 当 WiFi 处理（只有 ``cellular`` 才降档），于是在
      数据网络下照发母带——真机实测卡顿 7~8 秒，这正是前面几轮抱怨的来源；
    - ``by_lan`` 反过来：**只有真的看见私网 IP 才给无损**，其余（含判不出的、
      远程公网的）一律走省流档。

    宁可在判不出时少给一档音质（用户多半听不出来），也不能在窄管道上灌母带
    （用户立刻感觉到卡）。两种错误的代价不对等。
    """
    return str(network or "").strip().lower() in ("lan", "wifi")

# 进程内观察记录（用于诊断与自动策略）
_OBSERVED: dict[str, Any] = {
    "paths": {}, "hints": {}, "db": None, "db_path": "", "db_scanned_at": 0.0,
}
_DB_RESCAN_S = float(os.environ.get("FNMUSIC_QUALITY_DB_RESCAN", "300") or 300)


# ---------------------------------------------------------------------------
# 归一化与配置
# ---------------------------------------------------------------------------


def _norm_level(value: Any) -> str:
    """把各种写法归一化成网易云档位；认不出来返回空串（**不瞎猜**）。"""
    s = str(value or "").strip().lower()
    if not s:
        return ""
    if s in LEVELS:
        return s
    for level, hints in LEVEL_HINTS.items():
        if any(h in s for h in hints):
            return level
    return ""


def policy() -> str:
    p = str(os.environ.get("FNMUSIC_QUALITY_POLICY", DEFAULT_POLICY)
            or DEFAULT_POLICY).strip().lower()
    return p if p in POLICIES else DEFAULT_POLICY


# ---------------------------------------------------------------------------
# 接线开关（A 侧新增，W3）
#
# 本模块在 G 里默认生效。移植到三音源架构的 A 后，默认行为必须与 A 现状逐字节
# 一致，所以动态决策改为显式开关：``FNMUSIC_QUALITY_DYNAMIC`` 默认 false。
# 关闭时 app.py 完全不走 ``resolve()``：音质仍由 A 原有的
# ``netease_quality`` + ``quality_mode`` 静态出口决定，连 decision 都不计算。
# ---------------------------------------------------------------------------


def dynamic_enabled() -> bool:
    """动态音质决策总开关；默认关闭以保持 A 现状不变。"""
    raw = str(os.environ.get("FNMUSIC_QUALITY_DYNAMIC", "false") or "false")
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _configured(name: str, default: str = "") -> str:
    """读一个档位配置。

    环境变量缺失 → 用该字段默认值；值写了但**认不出来** → 返回空串，交给 ``resolve()``
    统一回落到既有 ``netease_quality``（再不行才是 lossless）。不在这里悄悄替换成默认档：
    那样用户填个错别字，页面显示与实际生效的就会对不上，而回落链会让 ``source``
    如实标注是回落来的。
    """
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default if default in LEVELS else ""
    return _norm_level(raw)


def fixed_level() -> str:
    return _configured("FNMUSIC_QUALITY_FIXED", "lossless")


def wifi_level() -> str:
    return _configured("FNMUSIC_QUALITY_WIFI", "lossless")


def cellular_level() -> str:
    """流量场景默认 320k，对应飞牛「标准音质」省流量的语义。"""
    return _configured("FNMUSIC_QUALITY_CELLULAR", "exhigh")


def _default_level() -> str:
    """跟随不了、也没配手动策略时的最后兜底：沿用既有 netease_quality。"""
    return _configured("FNMUSIC_NETEASE_QUALITY", "lossless") or "lossless"


# ---------------------------------------------------------------------------
# 被动发现之一：客户端请求线索
# ---------------------------------------------------------------------------


def _kv(obj: Any) -> list[tuple[str, Any]]:
    """把 QueryParams / Headers / dict / 任意怪东西统一成 (键, 值) 列表。

    这是旁路观察，任何入参都不能让它抛异常——否则会把正常请求搞挂。
    """
    if obj is None:
        return []
    for attr in ("multi_items", "items"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return [(str(k), v) for k, v in fn()]
            except Exception:  # noqa: BLE001
                break
    if isinstance(obj, (list, tuple)):
        out = []
        for pair in obj:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                out.append((str(pair[0]), pair[1]))
        return out
    return []


def observe_request(method: str, path: str, query: Any = None, headers: Any = None) -> None:
    """记录请求里出现的音质/网络线索。**只记录**，不推断、不改行为。

    这是发现"飞牛到底怎么表达音质偏好"的唯一可靠途径：我们的代理就架在飞牛 socket 上，
    客户端每个请求都经过这里。命中的键与值样例会进 report()，真机跑一会儿就能看到
    飞牛实际传了什么，比猜接口名诚实得多。
    """
    found: dict[str, str] = {}
    for source, items in (("query", _kv(query)), ("header", _kv(headers))):
        for key, val in items:
            low = str(key).lower()
            if not any(h in low for h in _HINT_KEYS):
                continue
            val_s = str(val)
            if len(val_s) > 120:
                val_s = val_s[:120] + "…"
            found[f"{source}.{key}"] = val_s
    if not found:
        return
    key = f"{str(method).upper()} {str(path).split('?')[0]}"
    _OBSERVED["paths"][key] = _OBSERVED["paths"].get(key, 0) + 1
    for name, val in found.items():
        rec = _OBSERVED["hints"].setdefault(name, {"samples": [], "count": 0, "last": 0.0})
        rec["count"] += 1
        rec["last"] = time.time()
        if val not in rec["samples"]:
            rec["samples"].insert(0, val)
            del rec["samples"][6:]      # 样例留几个够判断，不能无界增长


def network_of(request: Any) -> str:
    """判断本次播放走 WiFi 还是流量计费网络；判不出来返回 ``unknown``。

    只依据请求里真实出现的值（含中文「流量 / 无线」），不猜键名也不猜客户端行为。
    注意：中文只可能出现在 **query**（URL 解码后是 UTF-8），不可能出现在 header
    ——HTTP 头是 latin-1，Starlette 的 Headers 装非 latin-1 值会直接 UnicodeEncodeError。

    v2.8 新增：飞牛客户端实测**从不发送**任何网络类型键（真机诊断证据：
    「暂未观察到任何带音质或网络语义的键」），于是「流量档」从未触发过——
    移动数据远程访问时也一直按 WiFi 档发 jymaster（Hi-Res 母带），窄管道上
    起步缓冲好几秒。现在当客户端无显式提示时，从 ``X-Forwarded-For`` /
    ``X-Real-IP`` 等头里读真实客户端 IP：

    - 出现**公网 IP** = 远程访问（移动数据 / 异地），按 ``FNMUSIC_REMOTE_AS_CELLULAR``
      （默认开）视同流量场景，走省流档；
    - 只有**私网 IP** = 局域网直连，返回 ``lan``。

    采信的 IP 与判定结果全部进 report() 证据区——nginx 是否透传这些头在真机上
    一眼可见，透传不了也知道该换 fixed 策略而不是瞎猜。

    **v2.9.18 加「粘性」**：XFF 在真机上是时有时无的（走官方后端/组网通道转发过来
    的那次就没有），于是同一个网络环境下会交替出现 ``cellular`` 和 ``unknown``。
    而 ``by_network`` 策略里只有 ``network == "cellular"`` 才降档——**判成 unknown
    的那一次会照旧发 jymaster（Hi-Res 母带）**。用户体感就是「数据网络下时不时卡
    一下」：卡顿的就是这些漏判的请求。

    现在明确判出 cellular/lan 就记下来，后续 unknown 沿用最近一次结论。有效期刻意
    **不对称**：判成 cellular 记 30 分钟，判成 lan 只记 5 分钟——因为两种误判的代价
    不对等（见 `_LAN_TTL` 处注释）。宁可在判不出时多降一档（音质低一点），也不能在
    窄管道上发母带。

    **v2.9.18 补**：判定结果按类计数（含 unknown 与「被粘性救回」的次数）进证据区。
    没有这个计数，unknown 是隐形的——它既不落到 lan 也不落到 remote，于是「到底有
    多少请求压根判不出网络」这个问题永远没有答案，只能靠猜。
    """
    parts: list[str] = []
    for items in (_kv(getattr(request, "query_params", None)),
                  _kv(getattr(request, "headers", None))):
        for key, val in items:
            low = str(key).lower()
            if any(h in low for h in ("network", "net", "cellular", "wifi", "conn")):
                parts.append(str(val).lower())
    text = " | ".join(parts)
    if text:
        if any(k in text for k in _CELLULAR):
            return _remember_network("cellular")
        if any(k in text for k in _WIFI):
            return _remember_network("wifi")

    ips = forwarded_client_ips(request)
    if ips:
        public = [ip for ip in ips if is_public_ip(ip)]
        # XFF 全是回环地址 = 请求由**本机**（nginx / 飞牛远程中继）转发过来：这个 IP
        # 说明的是"转发者在本机"，**不是**"客户端在局域网"。当成局域网，数据网络下
        # 就会照发母带。这类请求本质是"来源不明"，走与 unknown 相同的兜底逻辑。
        relay = (not public) and all(is_loopback_ip(ip) for ip in ips)
        _record_client_ip_evidence(ips, bool(public), relay=relay)
        if public and remote_as_cellular():
            return _remember_network("cellular")
        if relay:
            return _unknown_verdict()
        return _remember_network("lan")
    return _unknown_verdict()


def _unknown_verdict() -> str:
    """判不出网络时的统一兜底，并把 unknown 计进证据（见 _count_judgement）。"""
    # 先沿用最近的明确结论（并记一笔「被粘性救回」，否则 unknown 的真实占比看不见）；
    # 再退到「未知当流量」开关；最后才是真的 unknown。
    _count_judgement("unknown")
    return _sticky_network() or ("cellular" if unknown_as_cellular() else "unknown")


# ---------------------------------------------------------------------------
# 网络判定的粘性（v2.9.18）
# ---------------------------------------------------------------------------

_LAST_NETWORK: dict[str, Any] = {"network": "", "ts": 0.0}
# 粘性有效期**刻意不对称**：
#   - 误判成 cellular：只是音质低一档（320k），用户可能根本听不出来；
#   - 误判成 lan：窄管道上照发 jymaster 母带（≈1MB/s），直接卡成幻灯片。
# 两种错误的代价完全不对等，所以「上次是流量」记 30 分钟，而「上次是局域网」只记
# 5 分钟——用户在家里播得好好的，出门用流量，5 分钟后就能自动降档，不用等半小时。
_LAN_TTL = 300.0
_CELLULAR_TTL = 1800.0


def unknown_as_cellular() -> bool:
    """完全判不出网络时，是否按流量场景处理（宁可音质低一档也不卡）。

    默认关：家里若恰好一次 XFF 都没透传，开着会让 WiFi 也长期停在省流档。
    真机上如果诊断页显示「判不出来」的次数很多，把它打开即可立刻见效。
    """
    return str(os.environ.get("FNMUSIC_UNKNOWN_AS_CELLULAR", "false") or "false") \
        .strip().lower() in ("true", "1", "yes", "on")


def _ttl_for(network: str) -> float:
    return _CELLULAR_TTL if network == "cellular" else _LAN_TTL


def _remember_network(network: str) -> str:
    """记下这次明确判出的网络环境，供判不出来的请求沿用。"""
    n = str(network or "").strip().lower()
    if n in ("cellular", "lan", "wifi"):
        _LAST_NETWORK["network"] = n
        _LAST_NETWORK["ts"] = time.time()
        _count_judgement(n)
    return str(network or "")


def _count_judgement(network: str) -> None:
    """统计各类判定出现的次数——「判不出来」到底占多少，必须能看出来。

    没有这个计数，诊断页只能显示「局域网 823 / 远程 21」，而真正的元凶（unknown）
    是隐形的：它既不落 lan 也不落 remote，于是我们永远不知道该不该修。
    """
    rec = _OBSERVED.setdefault("network_judgements", {})
    key = str(network or "unknown")
    rec[key] = int(rec.get(key, 0)) + 1
    if key == "unknown" and _sticky_network():
        rec["unknown_rescued"] = int(rec.get("unknown_rescued", 0)) + 1


def _sticky_network() -> str:
    """最近一次明确判出的网络环境（过期返回空串）。"""
    n = str(_LAST_NETWORK.get("network") or "")
    ts = float(_LAST_NETWORK.get("ts") or 0.0)
    if n and ts and (time.time() - ts) < _ttl_for(n):
        return n
    return ""


def reset_network_memory() -> None:
    """忘掉粘性结论（测试用；也可在切换网络后由外部调用）。"""
    _LAST_NETWORK["network"] = ""
    _LAST_NETWORK["ts"] = 0.0


def last_network_evidence() -> dict[str, Any]:
    """诊断用：粘性判定当前认为是什么网络、多久前判出来的。"""
    n = _sticky_network()
    if not n:
        return {"network": "", "age_s": 0}
    return {"network": n, "age_s": int(time.time() - float(_LAST_NETWORK.get("ts") or 0.0))}


# ---------------------------------------------------------------------------
# 客户端 IP 线索（v2.8）：远程访问识别
# ---------------------------------------------------------------------------

_IP_HEADER_KEYS = ("x-forwarded-for", "x-real-ip", "x-client-ip", "cf-connecting-ip")


def forwarded_client_ips(request: Any) -> list[str]:
    """从代理链头里取出候选客户端 IP（不去重、保持出现顺序）。"""
    ips: list[str] = []
    for key, val in _kv(getattr(request, "headers", None)):
        low = str(key).lower()
        if low == "x-forwarded-for":
            ips.extend(p.strip() for p in str(val).split(",") if p.strip())
        elif low in _IP_HEADER_KEYS[1:]:
            v = str(val).strip()
            if v:
                ips.append(v)
    return ips


def is_public_ip(raw: str) -> bool:
    """该 IP 是否公网地址（解析失败按非公网处理，宁缺毋滥）。"""
    import ipaddress

    try:
        addr = ipaddress.ip_address(str(raw).strip())
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_unspecified or addr.is_multicast)


def remote_as_cellular() -> bool:
    """远程访问（公网客户端 IP）是否按流量场景处理。默认开，可关。"""
    return str(os.environ.get("FNMUSIC_REMOTE_AS_CELLULAR", "true") or "true") \
        .strip().lower() in ("true", "1", "yes", "on")


def is_loopback_ip(raw: str) -> bool:
    """是否回环 / 本机地址（127.0.0.1、::1）。"""
    import ipaddress

    try:
        return bool(ipaddress.ip_address(str(raw).strip()).is_loopback)
    except ValueError:
        return False


def _record_client_ip_evidence(ips: list[str], has_public: bool, relay: bool = False) -> None:
    """记录一次 IP 判定证据。

    样例**必须按类分开存**：局域网请求一天几千次、远程几十次，混在一个最多 6 条的
    列表里（还先到先得），远程样例必然被局域网的挤掉——而远程才是我们唯一需要看清
    的那 21 次。真机上因此长期看不到"远程 IP 到底长什么样"，也就无从判断飞牛的
    远程访问到底有没有把客户端真实 IP 透传过来。

    时间戳同理：只有次数没有时间，"我刚才明明用数据播了"这句话无法和证据对上。
    """
    rec = _OBSERVED.setdefault(
        "client_ips",
        {"lan": 0, "remote": 0, "relay": 0, "lan_samples": [],
         "remote_samples": [], "lan_last": 0.0, "remote_last": 0.0},
    )
    now = time.time()
    bucket = "relay" if relay else ("remote" if has_public else "lan")
    rec[bucket] = int(rec.get(bucket, 0)) + 1
    rec[f"{bucket}_last" if bucket in ("lan", "remote") else "relay_last"] = now
    sample = str(ips[0])[:64] if ips else ""
    if sample:
        key = f"{bucket}_samples"
        samples = rec.setdefault(key, [])
        if sample not in samples:
            samples.insert(0, sample)
            del samples[4:]


# ---------------------------------------------------------------------------
# 被动发现之二：music.db 里的偏好行
# ---------------------------------------------------------------------------


def scan_music_db(db_path: str, force: bool = False) -> dict | None:
    """在飞牛 music.db 里做 **schema 容错**的偏好扫描。

    不预设表名/列名（那是猜测）。做法是枚举所有表，只扫"像 key-value 偏好"的表
    （列数 2..8 且含 key/name/setting/code/type/id 之类的列），再看整行文本是否命中
    音质/网络语义词；命中的整行原样记进证据。只读打开（``mode=ro``），扫描失败只记
    日志，绝不影响播放。默认缓存 5 分钟，避免每次播放都扫库。
    """
    now = time.time()
    cached = _OBSERVED.get("db")
    # 缓存必须**按路径**判断：只看时间戳的话，换一个 db 路径会直接拿回上一个库的
    # 扫描结果（真机上音乐库路径变化后，报告与自动判定都会是错的且无人察觉）。
    if (cached is not None and not force
            and _OBSERVED.get("db_path") == db_path
            and now - float(_OBSERVED.get("db_scanned_at") or 0) < _DB_RESCAN_S):
        return cached
    if not db_path or not os.path.exists(db_path):
        _OBSERVED["db"] = None
        _OBSERVED["db_path"] = db_path
        _OBSERVED["db_scanned_at"] = now
        return None

    hits: list[dict[str, Any]] = []
    err = ""
    sem = ("quality", "bitrate", "network", "wifi", "cellular", "transcode",
           "音质", "流量", "原始", "标准")
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for table in tables:
                if len(hits) >= 40:
                    break
                try:
                    cols = [c[1] for c in con.execute(f'PRAGMA table_info("{table}")').fetchall()]
                except sqlite3.Error:
                    continue
                if not 2 <= len(cols) <= 8:
                    continue
                if not any(str(c).lower() in ("key", "name", "setting", "code", "type", "id")
                           for c in cols):
                    continue
                try:
                    rows = con.execute(f'SELECT * FROM "{table}" LIMIT 400').fetchall()
                except sqlite3.Error:
                    continue
                for row in rows:
                    text = " ".join(str(v) for v in row if v is not None).lower()
                    if not any(h in text for h in sem):
                        continue
                    hits.append({"table": str(table), "row": _jsonable(dict(zip(cols, row)))})
                    if len(hits) >= 40:
                        break
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - 扫不到就如实说明，不能影响播放
        err = f"{type(exc).__name__}: {exc}"[:200]
        logger.info("music.db quality scan unavailable: %s", err)

    result = {"scanned_at": now, "path": db_path, "hits": hits, "error": err}
    _OBSERVED["db"] = result
    _OBSERVED["db_path"] = db_path
    _OBSERVED["db_scanned_at"] = now
    return result


def _jsonable(pairs: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in pairs.items():
        if isinstance(v, (bytes, bytearray)):
            v = f"<{len(v)} bytes>"
        out[str(k)] = v
    return out


def preference_from_db(db_path: str, network: str = "wifi") -> str:
    """从 db 扫描结果里尽力解析出一个网易云档位；解析不出来返回空串（不瞎猜）。

    优先取"既提到网络类型、又提到音质"的行，否则退而取任意提到音质的行。
    """
    scan = scan_music_db(db_path)
    if not scan or not scan.get("hits"):
        return ""
    want = _CELLULAR if network == "cellular" else _WIFI

    def text_of(hit: dict) -> str:
        row = hit.get("row") or {}
        return " ".join(str(v) for v in row.values() if v is not None).lower()

    for hit in scan["hits"]:
        text = text_of(hit)
        if any(k in text for k in want):
            level = _norm_level(text)
            if level:
                return level
    for hit in scan["hits"]:
        level = _norm_level(text_of(hit))
        if level:
            return level
    return ""


# ---------------------------------------------------------------------------
# 决策
# ---------------------------------------------------------------------------


def resolve(request: Any = None, db_path: str = "") -> dict[str, str]:
    """决定本次该向网易云要哪一档音质。返回 ``{level, network, policy, source}``。

    ``source`` 说明这个决定**是怎么来的**——「跟随飞牛」是否真的读到了偏好必须可查证，
    否则那个策略可能是从未生效过的空话。``auto:*`` = 读到了；``fallback:*`` = 没读到。
    """
    pol = policy()
    if request is not None:
        network = network_of(request)
    else:
        # 诊断页这种没有 request 的场景也要反映真实判定——否则页面永远显示
        # unknown + wifi 档，而实际播放在降档，看起来像策略没生效。
        network = _sticky_network() or "unknown"
    fallback = _default_level()

    if pol == "fixed":
        return {"level": fixed_level() or fallback, "network": network,
                "policy": pol, "source": "manual:fixed"}
    if pol == "by_lan":
        # 「局域网无损 / 其他 320k」：只有**明确**判出局域网才给 WiFi 档，
        # 其余一律省流档（含判不出的 unknown——见 on_lan 的说明）。
        lan = on_lan(network)
        level = (wifi_level() if lan else cellular_level()) or fallback
        return {"level": level, "network": network, "policy": pol,
                "source": f"manual:{'lan' if lan else 'non-lan'}"}
    if pol == "by_network":
        on_cellular = network == "cellular"
        level = (cellular_level() if on_cellular else wifi_level()) or fallback
        return {"level": level, "network": network, "policy": pol,
                "source": f"manual:{'cellular' if on_cellular else 'wifi'}"}

    # follow_fnos：先试自动发现，读不到就回落手动值（仍按网络类型选）
    auto = preference_from_db(db_path, network) if db_path else ""
    if auto:
        return {"level": auto, "network": network, "policy": pol, "source": "auto:music_db"}
    on_cellular = network == "cellular"
    manual = (cellular_level() if on_cellular else wifi_level()) or fallback
    return {"level": manual or fallback, "network": network, "policy": pol,
            "source": "fallback:manual_or_default"}


def report(db_path: str = "") -> dict[str, Any]:
    """诊断用：策略、已发现的证据、当前判定一次给全。

    「跟随飞牛」到底生效没有，不能靠感觉——这里直接给出判定来源，以及我们实际观察到
    的线索（客户端传了什么键、db 里扫到了什么行），需要进一步适配时这就是依据。
    """
    decision = resolve(None, db_path)
    # ★ 这里必须自己扫一遍，不能只读 _OBSERVED：fixed / by_network 策略在
    # resolve() 里根本走不到 preference_from_db（读 db 只有 follow_fnos 需要），
    # 于是 _OBSERVED["db"] 永远是 None，诊断页就会显示「music.db 不存在或未能
    # 打开」——db 明明好好地在那里，纯粹是我们没去查。这种假警报最坑人。
    scan = scan_music_db(db_path) if db_path else _OBSERVED.get("db")
    hints = _OBSERVED["hints"]
    return {
        "policy": decision["policy"],
        "levels": {"fixed": fixed_level(), "wifi": wifi_level(),
                   "cellular": cellular_level(), "default": _default_level()},
        "current": decision,
        "observed_client_hints": {
            k: {"count": v.get("count", 0), "samples": list(v.get("samples") or [])[:3]}
            for k, v in sorted(hints.items(), key=lambda kv: -(kv[1].get("count") or 0))[:12]
        },
        "observed_paths_with_hints": dict(sorted(_OBSERVED["paths"].items(),
                                                 key=lambda kv: -kv[1])[:8]),
        "client_ips": _client_ip_report(),
        "last_network": last_network_evidence(),
        "network_judgements": dict(_OBSERVED.get("network_judgements") or {}),
        "unknown_as_cellular": unknown_as_cellular(),
        "db_scan": ({
            "available": True,
            "path": scan.get("path", db_path),
            "hits": len(scan.get("hits") or []),
            "sample_hits": (scan.get("hits") or [])[:5],
            "error": scan.get("error", ""),
        } if scan else {
            "available": False,
            "hits": 0,
            "path": db_path,
            # 区分「压根没给路径」和「给了但文件不在」：前者是调用方的问题，
            # 后者才是真的没装/移库，两者的处理方式完全不同。
            "note": ("未传入 music.db 路径（无法扫描）" if not db_path
                     else f"music.db 不存在或未能打开: {db_path}"),
        }),
    }


def _client_ip_report() -> dict[str, Any]:
    """客户端 IP 证据（远程识别的判定依据，诊断页展示）。

    样例按类分开（局域网/远程各自留 4 条）并带"最近一次"的时间戳——混在一起时
    远程样例会被几千条局域网记录挤掉，而远程恰恰是唯一需要看清的那一类。
    """
    rec = _OBSERVED.get("client_ips")
    if not rec:
        return {"lan": 0, "remote": 0, "relay": 0, "samples": [],
                "lan_samples": [], "remote_samples": [],
                "lan_last": 0, "remote_last": 0,
                "note": "未观察到 X-Forwarded-For / X-Real-IP（可能 nginx 未透传，远程识别不可用）"}
    now = time.time()

    def age(ts: float) -> int:
        return int(now - float(ts or 0.0)) if ts else 0

    return {
        "lan": int(rec.get("lan", 0)),
        "remote": int(rec.get("remote", 0)),
        "relay": int(rec.get("relay", 0)),
        "lan_samples": list(rec.get("lan_samples") or [])[:4],
        "remote_samples": list(rec.get("remote_samples") or [])[:4],
        "lan_last": age(rec.get("lan_last", 0.0)),
        "remote_last": age(rec.get("remote_last", 0.0)),
        # 老字段保留：只给合并样例，兼容旧版诊断页/脚本
        "samples": (list(rec.get("remote_samples") or [])
                    + list(rec.get("lan_samples") or []))[:6],
    }


def reset_for_test() -> None:
    """测试钩子：清空观察记录，避免用例之间互相污染。"""
    _OBSERVED["paths"].clear()
    _OBSERVED["hints"].clear()
    _OBSERVED["db"] = None
    _OBSERVED["db_path"] = ""
    _OBSERVED["db_scanned_at"] = 0.0
    _OBSERVED.pop("client_ips", None)
    _OBSERVED.pop("network_judgements", None)
    reset_network_memory()
