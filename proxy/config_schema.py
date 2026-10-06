"""共享配置 schema：飞牛扩展可配置项的**唯一**校验表。

两个前端共用：A 自带音源页新增的「扩展设置」页（webui-service）按 `FIELDS` 动态渲染，
proxy/admin_ui.py 的控制台从同一模块导入校验函数。校验只留一份，避免两处规则漂移；
`label`/`help` 取自控制台页面原文（有测试保证字段集合与页面一致）。
"""
from __future__ import annotations

import os
import re
from typing import Any

try:  # 与其它模块同款双形态导入
    from . import download          # `_as_path` 复用 download.validate_dir（规则只此一份）
except ImportError:  # uvicorn --app-dir proxy
    import download  # type: ignore

# --------------------------------------------------------------- 校验 ----
# 由 proxy/admin_ui.py 原样搬来（用 ast 抽取，逐字一致）；控制台仍从这里导入。

def _as_bool(v: Any) -> str:
    return "true" if str(v).strip().lower() in ("true", "1", "yes", "on") else "false"

def _int_range(lo: int, hi: int):
    def check(v: Any) -> str:
        s = str(v).strip()
        if not re.fullmatch(r"\d+", s):
            raise ValueError(f"必须是 {lo}..{hi} 的整数，收到 {v!r}")
        n = int(s)
        if not lo <= n <= hi:
            raise ValueError(f"必须在 {lo}..{hi} 之间，收到 {n}")
        return str(n)

    return check

def _in_choices(*choices: str):
    def check(v: Any) -> str:
        s = str(v).strip().lower()
        if s not in choices:
            raise ValueError(f"取值必须是 {'/'.join(choices)} 之一，收到 {s!r}")
        return s

    return check

def _http_url(v: Any) -> str:
    s = str(v).strip()
    if not s:
        return ""
    if not re.match(r"^https?://[^\s]+$", s):
        raise ValueError(f"必须以 http:// 或 https:// 开头，收到 {s!r}")
    return s

def _free_text(maxlen: int = 200):
    def check(v: Any) -> str:
        s = str(v).strip()
        if len(s) > maxlen:
            raise ValueError(f"长度不能超过 {maxlen}")
        # 这些值会写进单引号包裹的 .env，dotenv_escape 已处理引号；
        # 这里再挡掉换行与控制字符，避免破坏 .env 的行结构
        if any(ord(c) < 32 for c in s):
            raise ValueError("不能包含换行或控制字符")
        return s

    return check

def _token(v: Any) -> str:
    s = str(v).strip()
    if not s:
        return ""
    if len(s) < 8:
        raise ValueError("token 长度异常（少于 8 位），请到 pushplus.plus 个人中心重新复制")
    if len(s) > 200:
        raise ValueError("token 过长")
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", s):
        raise ValueError("token 只能包含字母、数字、下划线与连字符")
    return s

def _as_library_dir(v: Any) -> str:
    """本地曲库目录：留空 = 交给自动探测；填了就必须是**已存在的目录**。

    这里刻意只校验「存在且是目录」，不要求可写——曲库是只读的，要求可写会把
    飞牛自己的共享目录挡在门外。同时也不接受相对路径（进程工作目录不固定）。
    """
    path = str(v or "").strip()
    if not path:
        return ""          # 空 = 自动探测
    if not os.path.isabs(path):
        raise ValueError("必须填绝对路径（例如 /vol1/1000/music）")
    if not os.path.isdir(path):
        raise ValueError("目录不存在：请先在飞牛音乐里确认曲库位置，或到文件管理里复制完整路径")
    return path

def _as_path(v: Any) -> str:
    """归档目录：必须是已存在的可写绝对路径，且不能是系统目录。

    这里就地把关，而不是等到用户点收藏时才发现路径不对——那时文件可能已经
    写进错误位置。校验规则与 proxy/download.validate_dir 同源，只此一份。
    """
    path = str(v or "").strip()
    if not path:
        return ""          # 空 = 关闭自动归档

    # ⚠️ 必须用模块顶部导入好的 download，不能在这里写 `from . import download`：
    # 管理页常以 `uvicorn --app-dir proxy` 启动，此时 admin_ui 是**顶层模块**、
    # 没有父包，相对导入必抛 ImportError。真机上这会被外层包成
    # 「取值非法（attempted relative import with no known parent package）」——
    # 明明是程序错误，却显示成用户的输入不合法，用户改一万遍路径也过不去。
    try:
        ok, why = download.validate_dir(path)
    except Exception as exc:  # noqa: BLE001 - 我们的错要如实标注，不能赖到用户输入上
        raise ValueError(f"内部校验出错（非路径问题）: {type(exc).__name__}: {exc}"[:200]) from exc
    if not ok:
        raise ValueError(why)
    return path

def _as_time_of_day(v: Any) -> str:
    """每日定时刷新时间：HH:MM（24h）或留空关闭。"""
    s = str(v or "").strip()
    if not s:
        return ""
    m = re.fullmatch(r"(\d{1,2}):(\d{1,2})", s)
    if not m:
        raise ValueError(f"时间格式应为 HH:MM（如 04:30），收到 {s!r}")
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        raise ValueError(f"时间超出范围（00:00–23:59），收到 {s!r}")
    return f"{h:02d}:{mi:02d}"

# 歌单口径/大类顺序的合法 key。**单一定义在这里**：admin_ui 与音源页共用同一套校验，
# 早先常量只写在 admin_ui 里，coercer 搬过来后引用不到，改「歌单大类顺序」就 NameError ⇒ HTTP 500。
CHANNEL_KEYS = ("mine", "nrec", "toplist", "category", "newalbum", "fm")
# 大类顺序额外允许 daily / localdaily（它们不在勾选框里，由各自独立开关控制）
_ORDER_KEYS = ("daily", "localdaily") + CHANNEL_KEYS
# 历史别名：旧说明文案把这几项写成 hot/my/newalbums/radio（G 时代的叫法），用户照抄进
# 配置会被静默丢掉顺序。这里统一归一成现名——认下来，但不写回旧名。
CHANNEL_ALIASES = {"hot": "toplist", "my": "mine", "newalbums": "newalbum", "radio": "fm"}


def _canon_channel(key: str) -> str:
    key = (key or "").strip().lower()
    return CHANNEL_ALIASES.get(key, key)


def _as_channels(v: Any) -> str:
    """口径勾选列表：逗号分隔，只接受已知 key，按固定顺序输出。"""
    picked: list[str] = []
    for part in str(v or "").replace(";", ",").split(","):
        key = _canon_channel(part)
        if key and key in CHANNEL_KEYS and key not in picked:
            picked.append(key)
    if not picked:
        raise ValueError("至少勾选一个歌单口径（全不勾等于关掉这个功能）")
    return ",".join(sorted(picked, key=CHANNEL_KEYS.index))

def _as_channel_order(v: Any) -> str:
    """歌单大类顺序：逗号分隔，只接受已知 key，按用户给的顺序原样输出。

    与 _as_channels 不同，这里**不**做规范排序——顺序本身就是用户要配的东西。
    空值回落到默认全序。
    """
    picked: list[str] = []
    for part in str(v or "").replace(";", ",").split(","):
        key = _canon_channel(part)
        if key and key in _ORDER_KEYS and key not in picked:
            picked.append(key)
    if not picked:
        return ",".join(_ORDER_KEYS)
    return ",".join(picked)

def _as_playlist_order(v: Any) -> str:
    """手动歌单顺序（v2.5）：逗号分隔 token。

    token 只允许 ``daily``（每日推荐的稳定别名，其真实 guid 含日期与用户 id）
    或 ``online:playlist:...`` 形式的完整 guid。空 = 不启用手动顺序（按大类排）。
    值来自管理页「歌单顺序」卡片，由前端按当前清单拼好，这里只做防注入校验。
    """
    tokens: list[str] = []
    for part in str(v or "").replace(";", ",").split(","):
        t = part.strip()
        if not t:
            continue
        if t != "daily" and not re.fullmatch(r"online:playlist:[A-Za-z0-9_.:\-]+", t):
            raise ValueError(f"顺序项必须是 daily 或 online:playlist:… 的 guid，收到 {t[:60]!r}")
        if t not in tokens:
            tokens.append(t)
    return ",".join(tokens)

# -------------------------------------------------------------- 字段表 ----
# kind: bool/int/choices/text/path/dir/url/time/token/channels/channel_order/playlist_order
# secret=True 的字段前端只回掩码，留空表示保持原值。
FIELDS: dict[str, dict] = {
    "netease_quality": {"env": "FNMUSIC_NETEASE_QUALITY", "label": '音质', "help": '账号无对应权益时自动回退，不会因此播放失败', "kind": "choices", "group": '基础', "default": 'lossless', "choices": ['lossless', 'exhigh', 'higher', 'standard']},
    "free_only_on_logout": {"env": "FNMUSIC_FREE_ONLY_ON_LOGOUT", "label": 'free_only_on_logout', "help": '', "kind": "bool", "group": '基础', "default": 'true'},
    "daily_enabled": {"env": "FNMUSIC_DAILY_ENABLED", "label": '未登录时降级为只播免费曲目', "help": '关闭则未登录时完全不提供在线播放，搜索结果只剩本地曲库', "kind": "bool", "group": '基础', "default": 'true'},
    "daily_limit": {"env": "FNMUSIC_DAILY_LIMIT", "label": '每日推荐曲目数', "help": '1–100，抓取网易云官方每日推荐', "kind": "int", "group": '基础', "default": '20', "min": 1, "max": 100},
    "local_daily_enabled": {"env": "FNMUSIC_LOCAL_DAILY_ENABLED", "label": 'local_daily_enabled', "help": '', "kind": "bool", "group": '本地每日推荐（v2.9）：每天从本地曲库随机抽 N 首', "default": 'true'},
    "local_daily_limit": {"env": "FNMUSIC_LOCAL_DAILY_LIMIT", "label": 'local_daily_limit', "help": '', "kind": "int", "group": '本地每日推荐（v2.9）：每天从本地曲库随机抽 N 首', "default": '50', "min": 1, "max": 500},
    "library_dir": {"env": "FNMUSIC_LIBRARY_DIR", "label": '本地曲库目录（留空=自动探测）', "help": '自动探测依赖飞牛的 music.db；各版本目录布局不统一，猜不中时本地每日推荐 会一首歌都扫不到（界面上表现为「不出现」）。此时在这里直接填曲库目录即可， 例如 /vol1/1000/music。填错会在保存时直接报错，不会静默失败。', "kind": "dir", "group": '本地每日推荐（v2.9）：每天从本地曲库随机抽 N 首', "default": ''},
    "local_first": {"env": "FNMUSIC_LOCAL_FIRST", "label": 'local_first', "help": '', "kind": "bool", "group": '本地曲库优先（v2.8 引入 / v2.9.14 修好）：播网易云歌单时优先读本地同名文件', "default": 'true'},
    "local_first_any_class": {"env": "FNMUSIC_LOCAL_FIRST_ANY_CLASS", "label": 'local_first_any_class', "help": '', "kind": "bool", "group": '本地曲库优先（v2.8 引入 / v2.9.14 修好）：播网易云歌单时优先读本地同名文件', "default": 'true'},
    "prefetch_next": {"env": "FNMUSIC_PREFETCH_NEXT", "label": 'prefetch_next', "help": '', "kind": "bool", "group": '下一首预热（v2.9.14 T1）：提前取回下一首的直链与元数据', "default": 'true'},
    "prefetch_lookahead": {"env": "FNMUSIC_PREFETCH_LOOKAHEAD", "label": '预热首数（1–5）', "help": '一次预热当前曲之后的几首。只取 JSON、不下载音频，多预热几首几乎不额外 耗流量；但每台 NAS 的 musicbox 并发能力不同，卡的话调回 1。', "kind": "int", "group": '下一首预热（v2.9.14 T1）：提前取回下一首的直链与元数据', "default": '3', "min": 1, "max": 5},
    "pushplus_enabled": {"env": "FNMUSIC_PUSHPLUS_ENABLED", "label": 'pushplus_enabled', "help": '', "kind": "bool", "group": '下一首预热（v2.9.14 T1）：提前取回下一首的直链与元数据', "default": 'true'},
    "pushplus_token": {"env": "FNMUSIC_PUSHPLUS_TOKEN", "label": '启用 PushPlus 推送提醒', "help": '登录失效 / 首次未登录 / 登录成功 / VIP 临期', "kind": "token", "group": '下一首预热（v2.9.14 T1）：提前取回下一首的直链与元数据', "default": '', "secret": True},
    "pushplus_topic": {"env": "FNMUSIC_PUSHPLUS_TOPIC", "label": 'PushPlus 群组编码', "help": '填了就推送到该群组（一对多）', "kind": "text", "group": '下一首预热（v2.9.14 T1）：提前取回下一首的直链与元数据', "default": '', "secret": True, "max_len": 64},
    "pushplus_template": {"env": "FNMUSIC_PUSHPLUS_TEMPLATE", "label": '消息模板', "help": '', "kind": "choices", "group": '下一首预热（v2.9.14 T1）：提前取回下一首的直链与元数据', "default": 'markdown', "choices": ['markdown', 'html', 'txt', 'json']},
    "pushplus_url": {"env": "FNMUSIC_PUSHPLUS_URL", "label": 'PushPlus 接口地址', "help": '一般不用改，除非你自建了转发', "kind": "url", "group": '下一首预热（v2.9.14 T1）：提前取回下一首的直链与元数据', "default": 'https://www.pushplus.plus/send'},
    "netease_search_limit": {"env": "FNMUSIC_NETEASE_SEARCH_LIMIT", "label": '单次搜索请求条数', "help": '1–100，向网易云请求的候选数量', "kind": "int", "group": '下一首预热（v2.9.14 T1）：提前取回下一首的直链与元数据', "default": '50', "min": 1, "max": 100},
    "online_limit": {"env": "FNMUSIC_ONLINE_LIMIT", "label": '搜索结果并入上限', "help": '1–100，最终显示在飞牛搜索列表里的在线条数', "kind": "int", "group": '下一首预热（v2.9.14 T1）：提前取回下一首的直链与元数据', "default": '30', "min": 1, "max": 100},
    "search_cache_ttl_days": {"env": "FNMUSIC_SEARCH_CACHE_TTL", "label": '搜索缓存有效期（天）', "help": '0 表示不缓存', "kind": "int", "group": '下一首预热（v2.9.14 T1）：提前取回下一首的直链与元数据', "default": '7', "min": 0, "max": 365},
    "login_check_interval_h": {"env": "FNMUSIC_LOGIN_CHECK_INTERVAL", "label": '登录态巡检间隔（小时）', "help": '0 表示只在请求时按需探测', "kind": "int", "group": '下一首预热（v2.9.14 T1）：提前取回下一首的直链与元数据', "default": '1', "min": 0, "max": 168},
    "log_max_mb": {"env": "FNMUSIC_LOG_MAX_MB", "label": '单文件日志上限（MB）', "help": '超过即就地截断保留最近一半，0 表示不限制', "kind": "int", "group": '下一首预热（v2.9.14 T1）：提前取回下一首的直链与元数据', "default": '10', "min": 0, "max": 1024},
    "log_max_days": {"env": "FNMUSIC_LOG_MAX_DAYS", "label": '日志保留天数', "help": '超期的备份日志直接删除、超期的活跃日志清空，0 表示永久保留', "kind": "int", "group": '下一首预热（v2.9.14 T1）：提前取回下一首的直链与元数据', "default": '30', "min": 0, "max": 3650},
    "log_quiet": {"env": "FNMUSIC_LOG_QUIET", "label": '日志降噪（不记封面 / 心跳 / 轮询）', "help": '默认开。日志里一大半是封面图、5 秒保活心跳、客户端状态轮询这类访问行—— 它们成功与否都不影响播放，却把 proxy.log 撑到 10MB 触发截断， 真正有用的播放日志反而留不住（「播不出来时日志里一行都没有」，一半就是这个原因）。 打开后这些行不再记录，<b>播放、取链失败、慢分片等排障日志一字不减</b>； 要抓完整原始日志时关掉它，重启服务生效', "kind": "bool", "group": '下一首预热（v2.9.14 T1）：提前取回下一首的直链与元数据', "default": 'true'},
    "netease_channels": {"env": "FNMUSIC_NETEASE_CHANNELS", "label": 'netease_channels', "help": '', "kind": "channels", "group": '更多口径歌单 / 账户歌单', "default": 'mine,toplist,category'},
    "netease_channel_limit": {"env": "FNMUSIC_NETEASE_CHANNEL_LIMIT", "label": '每口径注入上限', "help": '1–50。排行榜上游有 63 个，不限就会把你自己的本地歌单淹掉', "kind": "int", "group": '更多口径歌单 / 账户歌单', "default": '8', "min": 1, "max": 50},
    "netease_category": {"env": "FNMUSIC_NETEASE_CATEGORY", "label": '分类歌单的分类', "help": '华语 / 欧美 / 日语 / 韩语 / 粤语 / 流行 / 摇滚 / 民谣 / 电子 ……', "kind": "text", "group": '更多口径歌单 / 账户歌单', "default": '华语', "max_len": 32},
    "netease_channel_order": {"env": "FNMUSIC_NETEASE_CHANNEL_ORDER", "label": '歌单大类顺序', "help": '飞牛歌单列表里各大类的前后顺序，逗号分隔。可用值： daily(每日推荐) / localdaily(本地每日推荐) / mine(我的歌单) / nrec(推荐歌单) / toplist(排行榜) / category(分类歌单) / newalbum(新碟上架) / fm(私人FM)。没列出来的排最后；旧名 hot/my/newalbums/radio 也认', "kind": "channel_order", "group": '更多口径歌单 / 账户歌单', "default": 'localdaily,daily,mine,nrec,toplist,category,newalbum,fm'},
    "netease_playlist_order": {"env": "FNMUSIC_NETEASE_PLAYLIST_ORDER", "label": 'netease_playlist_order', "help": '', "kind": "playlist_order", "group": '更多口径歌单 / 账户歌单', "default": ''},
    "playlist_track_limit": {"env": "FNMUSIC_PLAYLIST_TRACK_LIMIT", "label": '歌单曲目上限', "help": '1–1000，点开歌单时最多解析多少首（越多越慢）', "kind": "int", "group": '更多口径歌单 / 账户歌单', "default": '300', "min": 1, "max": 1000},
    "playlist_cache_ttl_h": {"env": "FNMUSIC_PLAYLIST_TRACK_CACHE_TTL", "label": '歌单缓存有效期（小时）', "help": '缓存期内点开歌单直接读本地（秒开）；超期后先返回缓存、后台自动刷新，你看到的永远是上一次的结果', "kind": "int", "group": '歌单曲目缓存（v2.6）：打开秒开', "default": '6', "min": 1, "max": 168},
    "playlist_refresh_at": {"env": "FNMUSIC_PLAYLIST_REFRESH_AT", "label": '每日定时刷新歌单缓存', "help": '每天在这个时间后台刷新全部歌单曲目，第二天打开就是最新内容', "kind": "time", "group": '歌单曲目缓存（v2.6）：打开秒开', "default": '04:30'},
    "download_dir": {"env": "FNMUSIC_DOWNLOAD_DIR", "label": '收藏归档目录', "help": '必须是已存在的可写<b>绝对路径</b>；系统目录会被拒绝。按 <code>歌手/歌手 - 歌名.flac</code> 落盘并配同名 .lrc', "kind": "path", "group": '收藏归档与红心同步', "default": '', "secret": True},
    "download_on_favorite": {"env": "FNMUSIC_DOWNLOAD_ON_FAVORITE", "label": '点收藏时自动下载', "help": '取账号能拿到的最高品质（jymaster→hires→lossless→exhigh 逐档降级），配歌词', "kind": "bool", "group": '收藏归档与红心同步', "default": 'false'},
    "fav_sync_like": {"env": "FNMUSIC_FAV_SYNC_LIKE", "label": '收藏同步到网易云红心', "help": '点收藏/取消收藏时同步写你网易云账号的红心（双向）。这是对账号的写操作，默认关闭；需要登录态可用', "kind": "bool", "group": '收藏归档与红心同步', "default": 'false'},
    "lx_sync_enabled": {"env": "FNMUSIC_LX_SYNC_ENABLED", "label": '同步洛雪歌单', "help": '把 lx-music-sync-server（洛雪客户端的同步服务）里的歌单，作为<b>只读</b>歌单注入飞牛音乐的歌单列表。 只读：在飞牛这边加/删歌不会写回服务端，要改请去洛雪客户端改，改完等下一次同步自动生效', "kind": "bool", "group": '洛雪音乐同步服务器歌单（只读注入飞牛歌单列表）', "default": 'false'},
    "lx_sync_url": {"env": "FNMUSIC_LX_SYNC_URL", "label": '洛雪同步服务地址', "help": '就是洛雪客户端「设置 → 同步 → 同步服务地址」里填的那个（默认端口 9527）。填错会在保存时直接报错', "kind": "url", "group": '洛雪音乐同步服务器歌单（只读注入飞牛歌单列表）', "default": ''},
    "lx_sync_password": {"env": "FNMUSIC_LX_SYNC_PASSWORD", "label": '洛雪同步服务密码', "help": '服务端 config.js 里 <code>users[].password</code>（或环境变量 <code>LX_USER_&lt;用户名&gt;</code>）的那个密码。 用户名不用填：服务端是拿密码去匹配账号的。<b>留空 = 保持原值</b>', "kind": "text", "group": '洛雪音乐同步服务器歌单（只读注入飞牛歌单列表）', "default": '', "secret": True, "max_len": 200},
    "lx_sync_refresh_s": {"env": "FNMUSIC_LX_SYNC_REFRESH_S", "label": '洛雪歌单刷新间隔（秒）', "help": '默认 300。第一次打开歌单列表会等一次同步（最多 8 秒），之后都读缓存、后台按这个间隔刷新； 服务端连不上时继续显示上一次的结果，不会让歌单列表变空', "kind": "int", "group": '洛雪音乐同步服务器歌单（只读注入飞牛歌单列表）', "default": '300', "min": 30, "max": 86400},
    "lx_sync_device": {"env": "FNMUSIC_LX_SYNC_DEVICE", "label": '本机在洛雪同步里的设备名', "help": '只影响同步服务「设备列表」里显示的名字，方便你认出这是飞牛插件而不是手机/电脑客户端', "kind": "text", "group": '洛雪音乐同步服务器歌单（只读注入飞牛歌单列表）', "default": 'fnmusic-ext', "max_len": 64},
    "lx_sync_insecure_tls": {"env": "FNMUSIC_LX_SYNC_INSECURE_TLS", "label": '跳过同步服务的证书校验', "help": '只在服务端用<b>自签 https 证书</b>时才需要勾（勾了之后到该地址的流量不再校验身份，能被人中间人替换； 域名证书正常时不要勾）', "kind": "bool", "group": '洛雪音乐同步服务器歌单（只读注入飞牛歌单列表）', "default": 'false'},
    "lx_sync_writeback": {"env": "FNMUSIC_LX_SYNC_WRITEBACK", "label": '洛雪歌单回写档位', "help": '<b>off</b>：飞牛里加/删歌只是本地动作，洛雪那边不变； <b>tracks</b>：把歌加入/移出洛雪歌单会真的写回同步服务（电脑手机会看到）； <b>all</b>：额外允许在飞牛里删掉整张洛雪歌单——<b>对所有设备生效</b>（服务端保留快照，误删可在管理控制台还原）。 从搜索里新加的歌缺少洛雪的品质档字段，客户端播放时会自行重解析（能播，属降级写入）', "kind": "choices", "group": '洛雪音乐同步服务器歌单（只读注入飞牛歌单列表）', "default": 'off', "choices": ['off', 'tracks', 'all']},
    "quality_wifi": {"env": "FNMUSIC_QUALITY_WIFI", "label": '音质：局域网（家里 WiFi / 内网）', "help": '档位与网易云音乐一致，由低到高。账号无对应权益时上游自动降级，不会因此播放失败', "kind": "choices", "group": '音质：局域网一档 / 非局域网一档（v2.9.28 起只有这两档）', "default": 'lossless', "choices": ['jymaster', 'hires', 'lossless', 'exhigh', 'higher', 'standard']},
    "quality_cellular": {"env": "FNMUSIC_QUALITY_CELLULAR", "label": '音质：非局域网（流量 / 异地远程）', "help": '<b>看不出是不是局域网的请求也按这一档处理</b>——判不出时宁可少给一档音质 （多半听不出来），也不能在窄管道上灌母带（立刻就卡）。诊断页「网络判定计数」可确认透传是否正常', "kind": "choices", "group": '音质：局域网一档 / 非局域网一档（v2.9.28 起只有这两档）', "default": 'exhigh', "choices": ['jymaster', 'hires', 'lossless', 'exhigh', 'higher', 'standard']},
}

GROUP_ORDER: list[str] = [
    '基础',
    '本地每日推荐（v2.9）：每天从本地曲库随机抽 N 首',
    '本地曲库优先（v2.8 引入 / v2.9.14 修好）：播网易云歌单时优先读本地同名文件',
    '下一首预热（v2.9.14 T1）：提前取回下一首的直链与元数据',
    '更多口径歌单 / 账户歌单',
    '歌单曲目缓存（v2.6）：打开秒开',
    '收藏归档与红心同步',
    '洛雪音乐同步服务器歌单（只读注入飞牛歌单列表）',
    '音质：局域网一档 / 非局域网一档（v2.9.28 起只有这两档）',
]


def coerce(field: str, raw: Any) -> str:
    """按字段定义校验并归一化；不合法抛 ValueError（消息可直接给用户看）。"""
    meta = FIELDS.get(field)
    if meta is None:
        raise ValueError(f"未知配置项：{field}")
    kind = meta["kind"]
    if kind == "bool":
        return _as_bool(raw)
    if kind == "int":
        return _int_range(meta["min"], meta["max"])(raw)
    if kind == "choices":
        return _in_choices(*meta["choices"])(raw)
    if kind == "path":
        return _as_path(raw)
    if kind == "dir":
        return _as_library_dir(raw)
    if kind == "url":
        return _http_url(raw)
    if kind == "time":
        return _as_time_of_day(raw)
    if kind == "token":
        return _token(raw)
    if kind == "channels":
        return _as_channels(raw)
    if kind == "channel_order":
        return _as_channel_order(raw)
    if kind == "playlist_order":
        return _as_playlist_order(raw)
    return _free_text(meta.get("max_len", 200))(raw)

# ---------------------------------------------------------------------------
# 界面元数据（人工维护）：中文文案 + 分组 + **归属页面** + 枚举的显示名
# ---------------------------------------------------------------------------
# 为什么单独一张表：FIELDS 的 label/help 是从控制台 HTML 抽取的，缺控件的字段会退化成
# 英文键名（free_only_on_logout 等），个别还抽错了配对（daily_enabled 拿到了别人家的说明）。
# 这里逐项覆盖，并给每个字段指定它出现在 A 自带音源页的哪一页：
#   source   音乐源（洛雪同步 / 网易账号歌单 / 歌单与频道）
#   quality  播放与推荐（音质 / 推荐 / 播放 / 本地曲库）
#   search   搜索
#   tee      边听边存（收藏归档）
#   notify   通知（PushPlus + 登录态巡检）
#   extended 日志（日志上限/保留/降噪）
PAGES: dict[str, str] = {
    "source": "音乐源",
    "quality": "播放与推荐",
    "search": "搜索",
    "tee": "边听边存",
    "notify": "通知",
    "extended": "日志",
}

_QUALITY_ZH = {"standard": "标准 128k", "higher": "较高 192k", "exhigh": "极高 320k",
               "lossless": "无损 FLAC", "hires": "高清无损 Hi-Res", "jymaster": "臻品母带"}

FIELD_UI: dict[str, dict] = {
    # ---- 音乐源 · 洛雪歌单同步 ----
    "lx_sync_enabled": {"page": "source", "group": "洛雪歌单同步", "label": "同步洛雪歌单",
                        "help": "把 lx-music-sync-server（洛雪客户端「设置 → 同步」里那个服务）里的歌单，"
                                "作为只读歌单注入飞牛音乐的歌单列表。"
                                "当前音源是「网易云音乐盒子」时，歌单里的<b>网易云（wy）曲目</b>会自动借"
                                "网易云链路播放；QQ/酷狗/酷我平台的曲目只列在洛雪音源下，其它音源下不会出现"},
    "lx_sync_url": {"page": "source", "group": "洛雪歌单同步", "label": "洛雪同步服务地址",
                    "help": "例如 http://192.168.1.10:9527 或 https://域名:9528/用户名"
                            "（增强版把用户名放在路径里）。填错会在保存时直接报错"},
    "lx_sync_password": {"page": "source", "group": "洛雪歌单同步", "label": "洛雪同步服务密码",
                         "help": "服务端 config.js 里 users[].password（或环境变量 LX_USER_&lt;用户名&gt;）。"
                                 "用户名不用填：服务端是拿密码匹配账号的。留空 = 保持原值"},
    "lx_sync_refresh_s": {"page": "source", "group": "洛雪歌单同步", "label": "洛雪歌单刷新间隔（秒）",
                          "help": "默认 300。第一次打开歌单列表会等一次同步（最多 8 秒），之后读缓存、"
                                  "后台按这个间隔刷新；服务端连不上时继续显示上一次的结果"},
    "lx_sync_device": {"page": "source", "group": "洛雪歌单同步", "label": "本机在洛雪同步里的设备名",
                       "help": "只影响同步服务「设备列表」里显示的名字，方便认出这是飞牛插件"},
    "lx_sync_insecure_tls": {"page": "source", "group": "洛雪歌单同步", "label": "跳过证书校验（自签证书才勾）",
                             "help": "只在服务端用自签 https 证书时需要。勾了之后到该地址的流量不再校验身份，"
                                     "域名证书正常时不要勾"},
    "lx_sync_writeback": {"page": "source", "group": "洛雪歌单同步", "label": "洛雪歌单回写档位",
                          "help": "只读：在飞牛里加/删歌只是本地动作；可增删歌：会真写回同步服务"
                                  "（电脑手机会看到）；可删歌单：额外允许在飞牛里删掉整张洛雪歌单，"
                                  "<b>对所有设备生效</b>（服务端保留快照，误删可在管理控制台还原）",
                          "choice_labels": {"off": "只读（不写回）", "tracks": "可增删歌",
                                            "all": "可增删歌 + 可删歌单"}},
    # ---- 音乐源 · 网易账号歌单 ----
    "fav_sync_like": {"page": "source", "group": "网易账号歌单", "label": "收藏同步到网易云红心",
                      "help": "点收藏/取消收藏时同步写你网易云账号的红心（双向）。这是对账号的写操作，"
                              "默认关闭；需要登录态可用"},
    # ---- 音乐源 · 歌单与频道 ----
    "netease_channels": {"page": "source", "group": "歌单与频道", "label": "频道歌单",
                         "help": "要注入飞牛歌单列表的网易云频道，逗号分隔；留空 = 全用默认。"
                                 "mine 我的歌单 / nrec 推荐歌单 / toplist 排行榜 / category 分类歌单 / "
                                 "newalbum 新碟上架 / fm 私人FM。"
                                 "mine（我的歌单）需要先打开「音乐页显示网易账号歌单」——"
                                 "否则即使勾了也不会注入账号歌单。"
                                 "另外这些频道歌单由「网易云音乐盒子」提供：当前音源不是它时"
                                 "（选洛雪/网盘）不会注入，切回网易云音源才出现"},
    "netease_channel_limit": {"page": "source", "group": "歌单与频道", "label": "频道歌单数量上限",
                              "help": "1–50，每个频道最多注入多少张"},
    "netease_category": {"page": "source", "group": "歌单与频道", "label": "分类歌单的分类",
                         "help": "华语 / 欧美 / 日语 / 韩语 / 粤语 / 流行 / 摇滚 / 民谣 / 电子 ……"},
    "netease_channel_order": {"page": "source", "group": "歌单与频道", "label": "歌单大类顺序",
                              "help": "飞牛歌单列表里各大类的前后顺序，逗号分隔。可用值（写 key）："
                                      "daily 每日推荐 / localdaily 本地每日推荐 / mine 我的歌单 / "
                                      "nrec 推荐歌单 / toplist 排行榜 / category 分类歌单 / "
                                      "newalbum 新碟上架 / fm 私人FM。没列出来的排最后；"
                                      "旧名 hot/my/newalbums/radio 也认（等同 toplist/mine/newalbum/fm）"},
    "netease_playlist_order": {"page": "source", "group": "歌单与频道", "label": "歌单展示顺序",
                               "help": "按 guid 精确排序，逗号分隔（一般不用手填，页面上的拖拽会写这个值）"},
    "playlist_track_limit": {"page": "source", "group": "歌单与频道", "label": "歌单曲目上限",
                             "help": "1–1000，点开歌单时最多解析多少首（越多越慢）"},
    "playlist_cache_ttl_h": {"page": "source", "group": "歌单与频道", "label": "歌单缓存有效期（小时）",
                             "help": "缓存期内点开歌单直接读本地（秒开）；超期后先返回缓存、后台自动刷新"},
    "playlist_refresh_at": {"page": "source", "group": "歌单与频道", "label": "每日定时刷新歌单缓存",
                            "help": "每天在这个时间后台刷新全部歌单曲目，格式 HH:MM（如 04:30），留空关闭"},
    # ---- 播放与推荐 ----
    "netease_quality": {"page": "quality", "group": "音质", "label": "网易云音质（登录账号）",
                        "help": "账号无对应权益时上游会自动回退，不会因此播放失败", "choice_labels": _QUALITY_ZH},
    "quality_wifi": {"page": "quality", "group": "音质", "label": "音质：局域网（家里 WiFi / 内网）",
                     "help": "档位与网易云音乐一致，由低到高；账号无对应权益时上游自动降级",
                     "choice_labels": _QUALITY_ZH},
    "quality_cellular": {"page": "quality", "group": "音质", "label": "音质：非局域网（流量 / 异地远程）",
                         "help": "同上；出门用流量时建议选较高或极高，避免一首歌几十 MB",
                         "choice_labels": _QUALITY_ZH},
    # A 里同样没有调用方（netease_auth.daily_enabled() 无人使用），而且它的名字与旧页
    # 已有的「每日推荐」开关撞车，展示出来只会让人以为有两个一样的开关。
    "daily_enabled": {"page": "quality", "group": "推荐", "label": "每日推荐（未实现，不展示）",
                      "help": "抓取网易云官方每日推荐，生成「每日推荐」歌单", "hidden": True},
    # A 侧未实现对应功能（G 的本地每日推荐 / 每日推荐条数），界面上不展示这些死开关：
    # 展示了也调不出效果，只会让人以为「设了没用」。
    "daily_limit": {"page": "quality", "group": "推荐", "label": "每日推荐曲目数",
                    "help": "1–100，抓取网易云官方每日推荐", "hidden": True},
    "local_daily_enabled": {"page": "quality", "group": "推荐", "label": "本地每日推荐",
                            "help": "每天从本地曲库随机抽 N 首，生成「本地每日推荐」歌单", "hidden": True},
    "local_daily_limit": {"page": "quality", "group": "推荐", "label": "本地每日推荐曲目数",
                          "help": "1–500，从本地曲库随机抽取的曲目数", "hidden": True},
    "free_only_on_logout": {"page": "quality", "group": "播放", "label": "未登录时只播免费曲目",
                            "help": "开启（默认）：未登录也能试听免费片段；关闭：未登录时完全不提供在线播放，"
                                    "搜索结果只剩本地曲库"},
    "prefetch_next": {"page": "quality", "group": "播放", "label": "预取下一首",
                      "help": "提前取回下一首的直链与元数据，切歌更快；只取 JSON、不下载音频"},
    "prefetch_lookahead": {"page": "quality", "group": "播放", "label": "预取首数（1–5）",
                           "help": "一次预取当前曲之后的几首。多预取几首几乎不额外耗流量"},
    "library_dir": {"page": "quality", "group": "本地曲库", "label": "本地曲库目录（留空=自动探测）",
                    "help": "自动探测依赖飞牛的 music.db；各版本目录布局不统一，猜不中时本地每日推荐/本地优先"
                            "会一首歌都扫不到。此时直接填曲库目录，例如 /vol1/1000/music"},
    "local_first": {"page": "quality", "group": "本地曲库", "label": "本地曲库优先",
                    "help": "播网易云歌单时优先读本地同名文件，命中就不走网络"},
    "local_first_any_class": {"page": "quality", "group": "本地曲库", "label": "本地优先不限制音质档",
                              "help": "开启后任何音质档都优先用本地文件；关闭时只在高音质档优先"},
    # ---- 搜索 ----
    "netease_search_limit": {"page": "search", "group": "搜索", "label": "单次搜索请求条数",
                             "help": "1–100，向网易云请求的候选数量"},
    "online_limit": {"page": "search", "group": "搜索", "label": "搜索结果并入上限",
                     "help": "1–100，最终显示在飞牛搜索列表里的在线条数"},
    "search_cache_ttl_days": {"page": "search", "group": "搜索", "label": "搜索缓存有效期（天）",
                              "help": "0 表示不缓存"},
    # ---- 边听边存 ----
    "download_dir": {"page": "tee", "group": "收藏归档", "label": "收藏归档目录",
                     "help": "必须是已存在的可写<b>绝对路径</b>；系统目录会被拒绝。"
                             "按 <code>歌手/歌手 - 歌名.flac</code> 落盘并配同名 .lrc；留空 = 不归档"},
    "download_on_favorite": {"page": "tee", "group": "收藏归档", "label": "点收藏时自动下载",
                             "help": "取账号能拿到的最高品质（jymaster→hires→lossless→exhigh 逐档降级），配歌词"},
    # ---- 通知 ----
    "pushplus_enabled": {"page": "notify", "group": "推送与巡检", "label": "启用 PushPlus 推送提醒",
                         "help": "登录失效 / 首次未登录 / 登录成功 / VIP 临期时推送到微信"},
    "pushplus_token": {"page": "notify", "group": "推送与巡检", "label": "PushPlus token",
                       "help": "到 pushplus.plus 个人中心复制；该服务需实名认证，否则收不到推送。"
                               "<b>留空 = 保持原值</b>"},
    "pushplus_topic": {"page": "notify", "group": "推送与巡检", "label": "PushPlus 群组编码",
                       "help": "填了就推送到该群组（一对多）"},
    "pushplus_template": {"page": "notify", "group": "推送与巡检", "label": "消息模板",
                          "help": "PushPlus 支持的消息格式",
                          "choice_labels": {"markdown": "Markdown（推荐）", "html": "HTML",
                                            "txt": "纯文本", "json": "JSON"}},
    "pushplus_url": {"page": "notify", "group": "推送与巡检", "label": "PushPlus 接口地址",
                     "help": "一般不用改，除非你自建了转发"},
    # ---- 扩展设置（剩下的）----
    "login_check_interval_h": {"page": "notify", "group": "推送与巡检", "label": "登录态巡检间隔（小时）",
                               "help": "每隔多久检查一次网易云登录态（掉线/VIP 临期会推 PushPlus）。"
                                       "0 表示只在有请求时按需探测"},
    "log_max_mb": {"page": "extended", "group": "日志", "label": "单个日志文件上限（MB）",
                   "help": "超过即就地截断保留最近一半，0 表示不限制"},
    "log_max_days": {"page": "extended", "group": "日志", "label": "日志保留天数",
                     "help": "超期的备份日志直接删除、超期的活跃日志清空，0 表示永久保留"},
    "log_quiet": {"page": "extended", "group": "日志", "label": "日志降噪",
                  "help": "默认开：不记封面、5 秒保活心跳、客户端状态轮询这类高频访问行。"
                          "排障时可关掉看完整原始日志（代价是日志涨得快）"},
}

for _field, _ui in FIELD_UI.items():
    if _field not in FIELDS:
        raise KeyError(f"FIELD_UI 里的 {_field} 不在 FIELDS 中")
    FIELDS[_field].update(_ui)

# 分组顺序按「页面内的展示顺序」排（页面由 page 决定，组只影响页内分块）
GROUP_ORDER = [
    "洛雪歌单同步", "网易账号歌单", "歌单与频道",
    "音质", "推荐", "播放", "本地曲库",
    "搜索", "收藏归档", "推送与巡检", "日志",
]


def fields_of_page(page: str) -> list[str]:
    """某页面要渲染的字段（保持 FIELDS 的声明顺序；`hidden` 的死开关不展示）。"""
    return [f for f, meta in FIELDS.items() if meta.get("page") == page and not meta.get("hidden")]


def hidden_fields() -> list[str]:
    """A 侧没有实现对应功能、因此不在界面上展示的开关（避免用户白调）。"""
    return [f for f, meta in FIELDS.items() if meta.get("hidden")]


def groups_of_page(page: str) -> list[str]:
    """某页面内的分组顺序（保持 GROUP_ORDER 的顺序）。"""
    used = {FIELDS[f].get("group") for f in fields_of_page(page)}
    return [g for g in GROUP_ORDER if g in used]
