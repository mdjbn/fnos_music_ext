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

def _as_channels(v: Any) -> str:
    """口径勾选列表：逗号分隔，只接受已知 key，按固定顺序输出。"""
    picked: list[str] = []
    for part in str(v or "").replace(";", ",").split(","):
        key = part.strip().lower()
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
        key = part.strip().lower()
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
    "netease_channel_order": {"env": "FNMUSIC_NETEASE_CHANNEL_ORDER", "label": '歌单大类顺序', "help": '飞牛歌单列表里各大类的前后顺序，逗号分隔。可用值： daily(网易云每日推荐) / localdaily(本地每日推荐) / mine(我的歌单) / nrec(推荐歌单) / toplist(排行榜) / category(分类歌单) / newalbum(新碟上架) / fm(私人FM)。没列出来的排最后', "kind": "channel_order", "group": '更多口径歌单 / 账户歌单', "default": 'localdaily,daily,mine,nrec,toplist,category,newalbum,fm'},
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
