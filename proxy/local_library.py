"""本地曲库优先播放（v2.8）：在线曲目先匹配本地音乐库，命中且音质档位
符合策略时直接读本地文件。

## 背景

播放网易云歌单里的歌，即使 NAS 上早就有同一首歌（用户自己抓轨/下载的），
原先也一定要走「取直链 → CDN 下载」的完整链路：一次 musicbox 往返 +
CDN 首字节，远程访问（移动数据）时再加一整段窄管道传输。若本地有同名
歌曲，直接读本地文件起步最快，且完全不出外网。

## 匹配规则

- 数据源：飞牛官方 ``music.db``（schema 容错扫描——不预设表名列名，与
  quality.scan_music_db 同一套哲学：猜错不会报错，只会静默失效，所以只信
  真实观察到的列）。索引按 (归一化标题) 分组、内存缓存、TTL 过期重建。
- 标题：去空白与标点后**全等**（大小写不敏感）。保守匹配，宁可不命中
  也不能放错歌。
- 艺术家：任一侧缺失视为匹配；双侧都有时**主艺术家**（第一个，按
  ``/ ; ; , &`` 切）归一化后相等才算。

## 音质档位约束（用户要求：仍按已选音质策略决定是否降码率播放）

本地文件与请求档位各归为一个「音质类」：

- lossless 类：flac / wav / ape / wv / dsf / dff / aiff / alac 文件，
  或 jymaster / hires / lossless 档位；
- lossy 类：mp3 / m4a / aac / ogg / opus / wma 等文件，
  或 exhigh / higher / standard 档位。

**仅当两边同类时才用本地文件**：

- 策略要 lossless、本地是 Hi-Res/无损 → 读本地（本地比请求只高不低，可接受）；
- 策略要 exhigh（省流量）、本地是 Hi-Res → **不用本地**，仍按策略去网易云
  要 320k——这正是「根据音质策略决定是否降码率」：不能因为在本地摸到了
  母带就把省流量的意图顶掉；
- 策略要 lossless、本地只有 320k mp3 → 不用本地（拿不到想要的质量）；
- 网易云取链失败/无权益时，若本地有匹配（不论档位类）→ 兜底读本地，
  「能播」优先于「档位精确」。

想要「只要本地有就播、不管档位」，把 ``FNMUSIC_LOCAL_FIRST_ANY_CLASS=true``。

## 索引来源（v2.9.14 修正）

v2.8~v2.9.13 只从飞牛 ``music.db`` 建索引。真机上**这个库里未必有曲目表**——
诊断里能读到的只有 ``shared_library``（一排曲库目录），根本没有
(title, artist, path) 这样的行。于是索引恒为空、本地优先一次都不会命中，
而界面上没有任何迹象，看起来就是「这功能没做」。

现在改为 **music.db + 曲库目录文件系统扫描** 双来源合并：

- 文件系统扫描复用「本地每日推荐」已验证可行的做法（目录深度/文件数上限、
  扩展名白名单），标题/艺术家从文件名 ``歌手 - 歌名.ext`` 解析，
  文件名里没有分隔符时退用**父目录名**当艺术家（``/许嵩/庐州月.flac``）；
- 两边都有时按 path 去重，music.db 的条目优先（它带真实标签）。
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from typing import Any

# 与 quality 模块保持同一套「局域网」口径（只有明确判出 lan/wifi 才算局域网）。
# 两边各写一份判断必然漂移——本地优先放行无损、在线却降到 320k 这种自相矛盾
# 最难查。相对导入必须兜底：真机是 uvicorn --app-dir proxy 的扁平运行形态。
try:
    from . import quality  # type: ignore
except ImportError:  # pragma: no cover - uvicorn --app-dir proxy
    import quality  # type: ignore # noqa: F401

logger = logging.getLogger("fnmusic_proxy")

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def local_first_enabled() -> bool:
    # A 侧（W3）：G 里默认 true，移植到三音源架构的 A 后默认必须为 false，
    # 否则本地曲库优先会悄悄改变 A 现有的在线播放行为。要启用请显式设置
    # FNMUSIC_LOCAL_FIRST=true（同时还需 FNMUSIC_QUALITY_DYNAMIC 之外无需其他开关）。
    return str(os.environ.get("FNMUSIC_LOCAL_FIRST", "false") or "false") \
        .strip().lower() in ("true", "1", "yes", "on")


def any_class_allowed() -> bool:
    """true = 只要本地有同名曲就播，不看音质档位是否同类。**默认就是 true**。

    为什么改默认（v2.9.16）：真机 `level=jymaster`（无损类）+ 本地是 mp3 时，
    严格同类会把本地 mp3 一刀切拒掉、转头去网易云要无损——**而网易云给的其实
    也是 MP3**（proxy.log 实锤：`CDN 回的是 audio/mpeg，曲目却声明无损`）。
    绕一圈出了外网、多花几百毫秒，拿到的还是同样的 MP3。

    所以「本地有就播」才是合理解：零外网、起步最快，且同名多首时仍然优先
    取无损那条（见 find_local_match 的排序）。真正坚持「非无损不播」的用户
    把它设成 false 即可。
    """
    return str(os.environ.get("FNMUSIC_LOCAL_FIRST_ANY_CLASS", "true") or "true") \
        .strip().lower() in ("true", "1", "yes", "on")


def _fs_scan_max_files() -> int:
    try:
        return max(0, int(float(os.environ.get("FNMUSIC_LOCAL_FS_MAX_FILES", "20000"))))
    except (TypeError, ValueError):
        return 20000


def _fs_scan_max_depth() -> int:
    try:
        return max(0, int(float(os.environ.get("FNMUSIC_LOCAL_FS_MAX_DEPTH", "6"))))
    except (TypeError, ValueError):
        return 6


def _index_ttl() -> float:
    try:
        return max(0.0, float(os.environ.get("FNMUSIC_LOCAL_INDEX_TTL", "300") or 300))
    except (TypeError, ValueError):
        return 300.0


# ---------------------------------------------------------------------------
# 音质类
# ---------------------------------------------------------------------------

LOSSLESS_EXTS = {"flac", "wav", "ape", "wv", "dsf", "dff", "aiff", "aif", "alac"}
LOSSLESS_LEVELS = {"jymaster", "hires", "lossless"}

# 只索引这些扩展名的文件（cue/歌词/封面等一律忽略）
AUDIO_EXTS = LOSSLESS_EXTS | {"mp3", "m4a", "aac", "ogg", "opus", "wma", "tta", "tak"}


def klass_of_ext(ext: str) -> str:
    return "lossless" if str(ext or "").strip().lstrip(".").lower() in LOSSLESS_EXTS else "lossy"


def klass_of_level(level: str) -> str:
    return "lossless" if str(level or "").strip().lower() in LOSSLESS_LEVELS else "lossy"


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

_STRIP_RE = re.compile(r"[\s\-–—·・_.,，、。.!！?？:：;；'\"`~@#$%^&*()（）\[\]【】{}<>《》/\\|+…]+")
_ARTIST_SPLIT_RE = re.compile(r"\s*[/;,，、&\+]\s*")
# 括号尾巴：「晴天 (Live)」「演员（伴奏）」「千本樱[MMD]」→ 主体名
_PAREN_RE = re.compile(r"[（(\[【][^）)\]】]{0,20}[）)\]】]\s*$")


def _norm_text(s: Any) -> str:
    return _STRIP_RE.sub("", str(s or "").strip().lower())


def norm_title(s: Any) -> str:
    return _norm_text(s)


def primary_artist(s: Any) -> str:
    parts = [p for p in _ARTIST_SPLIT_RE.split(str(s or "").strip()) if p.strip()]
    return _norm_text(parts[0]) if parts else ""


def artist_compatible(local: Any, remote: Any) -> bool:
    """艺术家是否算「同一批人」。

    比全等宽松一点，但仍是保守方向：飞牛/网易云两侧的艺术家写法差异极大
    （``许嵩 _ 何曼婷`` vs ``许嵩 / 何曼婷``、``周杰伦`` vs ``周杰伦&xxx``），
    标题已经全等了，再要求艺术家字符串逐字相同会把大量真命中挡掉——这在真机
    上的表现就是「本地明明有这首歌，却还是走了网易云」。

    规则：任一侧缺失 → 兼容；归一化后相等 → 兼容；**一侧包含另一侧** → 兼容
    （``许嵩何曼婷`` 含 ``许嵩``）。都不满足才判不兼容。
    """
    a = primary_artist(local)
    b = primary_artist(remote)
    if not a or not b:
        return True
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 2 and short in long_


# ---------------------------------------------------------------------------
# music.db 索引（schema 容错）
# ---------------------------------------------------------------------------

_INDEX_CACHE: dict[str, tuple[float, dict[str, list[dict]]]] = {}
# 索引的来源构成（music.db 多少首 / 目录扫描多少首），诊断页要靠它回答
# 「本地优先到底有没有歌可匹配」——v2.9.13 之前这个问题完全无法查证。
_INDEX_META: dict[str, dict] = {}
# 最近若干次匹配尝试（命中/未命中 + 原因）。功能「看起来没生效」时，
# 这就是第一现场：是没索引、还是索引里有但标题没对上。
_LOOKUP_LOG: list[dict] = []
_LOOKUP_LOG_MAX = 30
# 流量网络下「本地有无损但让给在线」的次数——不记就只能靠用户描述快慢，
# 没法确认这条规则到底生效了几次。
_CELL_SKIPS: dict[str, int] = {"n": 0}

_TITLE_COLS = ("title", "song_name", "name", "track_title", "songtitle")
_ARTIST_COLS = ("artist", "artists", "singer", "singers", "artist_name", "author")
_PATH_COLS = ("path", "file_path", "filepath", "url", "file", "location", "filename")


def _pick_col(cols: list[str], candidates: tuple[str, ...]) -> str | None:
    low = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand in low:
            return low[cand]
    for c in cols:
        cl = c.lower()
        for cand in candidates:
            if cand in cl:
                return c
    return None


def entry_variants(title: Any, artist: Any, path: Any) -> list[tuple[str, str]]:
    """一行 music.db 记录可能对应的 (标题, 艺术家) 候选。

    **这是 v2.9.17 修的核心 bug。** 真机 music.db 的 `title` 字段存的不是标题，
    而是**完整文件名**，artist 是空的：

        title = "Beyond - 光辉岁月.flac"    artist = None

    之前的索引直接拿它当标题归一化 → ``beyond光辉岁月flac``；而查询用的是
    网易云给的纯标题 ``光辉岁月`` —— 两个键永远对不上。索引看着有 915 首，
    实际一首都匹配不了（自测之所以「能匹配上自己」，是因为它拿索引里自己的
    title 去查自己，当然中）。

    music.db 的字段语义各版本不一，所以不猜、而是把候选都登记：

    1. title 字段去掉音频扩展名后的原样；
    2. 上面这个再按 ``歌手 - 歌名`` 拆开（artist 也由此补上真实值）；
    3. **path 的文件名**（最可靠——它一定是真实文件名）；
    4. 文件名同样拆开。

    代价只是索引大一点（900 首 × 4），换来的是不再静默失配。
    """
    out: list[tuple[str, str]] = []
    t = str(title or "").strip()
    a = str(artist or "").strip()

    def _strip_ext(s: str) -> str:
        stem, ext = os.path.splitext(s)
        return stem if (ext and ext.lstrip(".").lower() in AUDIO_EXTS) else s

    if t:
        t2 = _strip_ext(t)
        out.append((t2, a))
        na, nt = _split_name(t2, a)
        if nt and nt != t2:
            out.append((nt, na or a))

    p = str(path or "").strip()
    if p:
        fname = _strip_ext(os.path.basename(p)).strip()
        if fname:
            out.append((fname, a))
            na2, nt2 = _split_name(fname, a)
            if nt2 and nt2 != fname:
                out.append((nt2, na2 or a))

    seen: set[tuple[str, str]] = set()
    res: list[tuple[str, str]] = []
    for item in out:
        if item[0] and item not in seen:
            seen.add(item)
            res.append(item)
    return res


def title_keys(title: Any) -> list[str]:
    """一个标题在索引里可能出现的归一化键。

    除了原样，还登记**剥掉括号尾巴**的版本——网易云的「晴天 (Live)」「演员（伴奏）」
    在本地库里往往就叫「晴天」「演员」，反之亦然。只在建索引和查询两端都用同一
    套键，两边才对得上。注意必须在归一化**之前**剥：_STRIP_RE 会把括号字符去掉
    但留下里面的词（"晴天 (Live)" → "晴天live"），那仍然对不上。
    """
    raw = str(title or "").strip()
    keys: list[str] = []
    k = norm_title(raw)
    if k:
        keys.append(k)
    stripped = _PAREN_RE.sub("", raw).strip()
    if stripped and stripped != raw:
        k2 = norm_title(stripped)
        if k2 and k2 not in keys:
            keys.append(k2)
    return keys


def build_index(db_path: str) -> dict[str, list[dict]]:
    """扫描 music.db，构建 {归一化标题: [条目…]}。

    任何失败都返回空索引（缓存住，TTL 后重试），绝不抛异常影响播放。
    """
    index: dict[str, list[dict]] = {}
    if not db_path or not os.path.exists(db_path):
        return index
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for table in tables:
                try:
                    cols = [c[1] for c in con.execute(
                        f'PRAGMA table_info("{table}")').fetchall()]
                except sqlite3.Error:
                    continue
                title_col = _pick_col(cols, _TITLE_COLS)
                artist_col = _pick_col(cols, _ARTIST_COLS)
                path_col = _pick_col(cols, _PATH_COLS)
                if not (title_col and path_col):
                    continue
                try:
                    rows = con.execute(
                        f'SELECT "{title_col}", "{artist_col}", "{path_col}" '
                        f'FROM "{table}" LIMIT 200000').fetchall()
                except sqlite3.Error:
                    continue
                for title, artist, path in rows:
                    path = str(path or "").strip()
                    if not path or len(path) < 3:
                        continue
                    ext = os.path.splitext(path)[1].lstrip(".").lower()
                    if ext not in AUDIO_EXTS:
                        continue
                    for vt, va in entry_variants(title, artist, path):
                        for key in title_keys(vt):
                            index.setdefault(key, []).append({
                                "title": vt,
                                "artist": va,
                                "path": path,
                                "ext": ext,
                                "klass": klass_of_ext(ext),
                            })
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - 索引失败不影响播放，只是不启用本地优先
        logger.info("local library index unavailable (%s): %s: %s",
                    db_path, type(exc).__name__, exc)
        return {}
    if index:
        logger.info("本地曲库索引就绪：%d 首（db=%s）", sum(len(v) for v in index.values()), db_path)
    else:
        # 索引为空 = 本地优先将永远不命中。必须留痕说明是「库没扫到东西」，
        # 否则用户只看到功能没生效、却不知道该往哪儿查（music.db 路径不对？
        # 列名没匹配上？）。真机排障时这是第一现场。
        logger.info("本地曲库索引为空（db=%s）——music.db 可能路径不对或表结构未匹配，"
                    "本地曲库优先不会生效；请把 music.db 的 .schema 发给开发者适配", db_path)
    return index


def _split_name(stem: str, fallback_artist: str = "") -> tuple[str, str]:
    """``歌手 - 歌名`` → (artist, title)；没有分隔符就整段当标题。

    真机曲库里既有 ``许嵩 - 庐州月`` 也有 ``许嵩 _ 何曼婷 - 素颜``（分隔符前后
    带空格），还有直接以父目录名当歌手的 ``/许嵩/庐州月.flac``。
    """
    for sep in (" - ", " – ", " — ", " _ ", "-"):
        if sep in stem:
            a, t = stem.split(sep, 1)
            if a.strip() and t.strip():
                return a.strip(), t.strip()
    return fallback_artist, stem.strip()


def build_fs_index(library_dir: str) -> dict[str, list[dict]]:
    """扫曲库目录建 {归一化标题: [条目…]}。

    与「本地每日推荐」的扫描同源同参数（目录深度 / 文件数 / 扩展名），那边能
    扫出歌、这边就一定能建出索引。任何异常都返回已收集到的部分，绝不抛。
    """
    index: dict[str, list[dict]] = {}
    root = str(library_dir or "").strip()
    if not root or not os.path.isdir(root):
        return index
    max_files = _fs_scan_max_files()
    max_depth = _fs_scan_max_depth()
    try:
        for base, dirs, files in os.walk(root):
            depth = os.path.relpath(base, root).count(os.sep)
            if depth > max_depth:
                dirs[:] = []
                continue
            # 目录名当兜底艺术家：/曲库/许嵩/庐州月.flac → 许嵩
            fallback = "" if depth == 0 else os.path.basename(base)
            for name in files:
                ext = os.path.splitext(name)[1].lstrip(".").lower()
                if ext not in AUDIO_EXTS:
                    continue
                path = os.path.join(base, name)
                try:
                    if os.path.getsize(path) <= 0:
                        continue
                except OSError:
                    continue
                artist, title = _split_name(os.path.splitext(name)[0].strip(), fallback)
                keys = title_keys(title)
                if not keys:
                    continue
                for key in keys:
                    index.setdefault(key, []).append({
                        "title": title,
                        "artist": artist,
                        "path": path,
                        "ext": ext,
                        "klass": klass_of_ext(ext),
                        "src": "fs",
                    })
                if sum(len(v) for v in index.values()) >= max_files:
                    return index
    except Exception as exc:  # noqa: BLE001 - 扫不动就只用 music.db 的部分
        logger.warning("本地曲库目录扫描失败（%s）：%s: %s", root, type(exc).__name__, exc)
    return index


def _get_index(db_path: str, library_dir: str = "") -> dict[str, list[dict]]:
    key = f"{db_path}|{library_dir}" if library_dir else str(db_path)
    now = time.time()
    hit = _INDEX_CACHE.get(key)
    if hit is not None and (now - hit[0]) < _index_ttl():
        return hit[1]

    raw = build_index(db_path)
    # music.db 里常有已经搬走 / 删掉的旧记录。它们有两个坏处，而且第二个是隐性的：
    #   1. 匹配上了却 os.path.isfile() 失败 → 诊断里那条看不懂的 file-missing；
    #   2. 更要命——`seen` 是按 path 去重的，失效记录的 path 会把**目录扫描到的同名
    #      真文件**误判成「已在库里」而跳过，于是 fs 补充恒为 0，坏记录永远没人顶替。
    # 建索引时先按可达性过滤一次，两个问题一起解决（stat 有 TTL 缓存，不是每次播放）。
    index: dict[str, list[dict]] = {}
    ok_paths: set[str] = set()
    bad_paths: set[str] = set()

    def _reachable(p: str) -> bool:
        if not p:
            return False
        if p in ok_paths:
            return True
        if p in bad_paths:
            return False
        try:
            good = os.path.isfile(p) and os.path.getsize(p) > 0
        except OSError:
            good = False
        (ok_paths if good else bad_paths).add(p)
        return good

    for k, v in raw.items():
        kept = []
        for item in v:
            item.setdefault("src", "db")
            if _reachable(str(item.get("path") or "")):
                kept.append(item)
        if kept:
            index[k] = kept
    db_broken = len({p for p in bad_paths if p})

    fs_n = 0
    fs_scanned = 0
    if library_dir:
        seen = {str(i.get("path") or "") for e in index.values() for i in e}
        for k, entries in build_fs_index(library_dir).items():
            for e in entries:
                fs_scanned += 1
                if str(e.get("path") or "") in seen:
                    continue
                index.setdefault(k, []).append(e)
                fs_n += 1
    if fs_scanned:
        logger.info("本地曲库目录扫描：%d 个音频文件，其中 %d 首已是 music.db 之外的补充",
                    fs_scanned, fs_n)
    db_n = len({str(i.get("path") or "")
                for e in index.values() for i in e if i.get("src") == "db"})
    _INDEX_META[key] = {
        "db": db_n, "fs": fs_n, "fs_scanned": fs_scanned, "dir": library_dir,
        "db_broken": db_broken,
        "ts": now, "key": key,
        "empty_reason": ("" if (db_n or fs_n) else
                         ("music.db 没有可索引的曲目表" if not library_dir else
                          "music.db 无曲目表且曲库目录未扫到音频文件")),
    }
    if db_broken:
        logger.info("本地曲库索引：忽略 %d 条 music.db 失效记录（文件已不存在）", db_broken)
    if db_n or fs_n:
        logger.info("本地曲库索引：music.db %d 首 + 目录扫描 %d 首（dir=%s）",
                    db_n, fs_n, library_dir or "-")
    _INDEX_CACHE[key] = (now, index)
    return index


def find_local_match(title: str, artist: str, db_path: str,
                     library_dir: str = "") -> dict | None:
    """找本地同名曲；命中返回 {path, ext, klass, …}，文件必须真实存在。

    同名多首（翻唱/伴奏等）时按「艺术家匹配 > 无损优先」排序取第一。
    ``library_dir`` 给了就把曲库目录的文件系统索引并进来一起匹配——真机上
    music.db 与目录扫描常常各管一段，缺一边就会漏掉真命中。
    """
    if not title_keys(title):
        return None
    idx = _get_index(db_path, library_dir)
    hit: dict | None = None
    reason = "title-not-in-index"
    miss_path = ""
    for ki, key in enumerate(title_keys(title)):
        entries = idx.get(key)
        if not entries:
            continue
        reason = "title-not-in-index"

        def _rank(e: dict) -> tuple[int, int]:
            return (0 if artist_compatible(e.get("artist"), artist) else 1,
                    0 if e.get("klass") == "lossless" else 1)

        for e in sorted(entries, key=_rank):
            if _rank(e)[0] != 0:
                reason = "artist-mismatch"
                break
            path = str(e.get("path") or "")
            try:
                if os.path.isfile(path) and os.path.getsize(path) > 0:
                    hit = e
                    reason = "hit" if ki == 0 else "hit-stripped"
                    break
            except OSError:
                continue
            reason = "file-missing"
            # 记下第一个打不开的路径：不写出来，file-missing 就是一句没有主语的
            # 废话——到底是路径拼错了、还是文件真被删了，永远查不下去。
            if not miss_path:
                miss_path = path
        if hit is not None:
            break
    _LOOKUP_LOG.append({
        "ts": time.time(), "title": str(title or ""), "artist": str(artist or ""),
        "hit": hit is not None, "reason": reason,
        "path": str((hit or {}).get("path") or ""),
        "miss_path": "" if hit is not None else miss_path,
        # 未命中时给出索引里最像的标题：区分「真没有」和「名字对不上」
        "near": [] if hit is not None else suggest_similar(title, db_path, library_dir),
    })
    del _LOOKUP_LOG[:-_LOOKUP_LOG_MAX]
    if hit is None:
        near = suggest_similar(str(title or ""), db_path, library_dir)
        logger.debug("local-first miss (%s): title=%r artist=%r 相近标题=%s",
                     reason, title, artist, near or "（无）")
    return hit


def suggest_similar(title: str, db_path: str, library_dir: str = "",
                    n: int = 3) -> list[str]:
    """索引里跟这个标题最像的 n 个真实标题。

    「本地明明有这首歌为什么不走本地」只有两种可能：真没有，或者名字对不上。
    没有这个提示，诊断里一行 `title-not-in-index` 两种都可能，只能靠猜。

    中文歌名普遍很短（「出山」两个字的相似度很难过 difflib 的 0.5 门槛），
    所以先用**包含关系**捞，再用 difflib 补，两条路都给。
    """
    import difflib

    raw = str(title or "").strip()
    key = norm_title(raw)
    if not key:
        return []
    titles = sorted({str(e.get("title") or "")
                     for es in _get_index(db_path, library_dir).values() for e in es} - {""})
    if not titles:
        return []

    near: list[str] = []
    # 1) 包含：查询词在标题里，或标题在查询词里（「出山」↔「出山 (Live)」）
    for t in titles:
        tk = norm_title(t)
        if not tk:
            continue
        if (len(key) >= 2 and key in tk) or (len(tk) >= 2 and tk in key):
            if t not in near:
                near.append(t)
        if len(near) >= n * 3:
            break
    # 2) difflib 兜底（放宽门槛，短标题也能给一点线索）
    for t in difflib.get_close_matches(raw, titles, n=n * 3, cutoff=0.3):
        if t not in near:
            near.append(t)
    return near[:n]


def cellular_lossy_only() -> bool:
    """流量网络下，本地优先是否只吃**有损**文件（默认开）。

    这条规则是 2.9.20 真机数据逼出来的。本地优先做到 14 查 10 中之后，用户反而反馈
    「比之前还慢，播放《最后一首情歌》要等 7~8 秒」——诊断里命中的**全是 .flac**：

        本地 FLAC 一首 30~40MB   在线 exhigh(320k) 一首 ~9MB

    数据都从 NAS 传到手机，走的是同一条窄管道，**体积才是决定因素**。无条件「本地有
    就播」在数据网络下等于把省流档的意图整个顶掉：以前本地优先几乎不命中，全都走
    在线 320k；命中率修好之后反而开始灌 30MB 的本地无损，于是更慢了。

    有损文件（mp3/m4a）体积与在线 320k 相当，本地仍然更快（省掉外网往返），所以
    只在**本地是无损**时让给在线。WiFi / 局域网不受任何影响。
    """
    return str(os.environ.get("FNMUSIC_LOCAL_FIRST_CELLULAR_LOSSY_ONLY", "true")
               or "true").strip().lower() in ("true", "1", "yes", "on")


def serves_request(entry: dict, level: str, network: str = "") -> bool:
    """本地文件的音质类是否满足请求档位。

    默认要求**同类**：策略要 lossless 就只吃本地无损；策略要 320k 就不喂本地
    母带（那会把「省流量」的意图顶掉）。``FNMUSIC_LOCAL_FIRST_ANY_CLASS=true``
    时放开——只要本地有这首就播，不看档位。

    **唯一例外**：流量网络下无损文件仍要让给在线（见 ``cellular_lossy_only``）。
    这里不看档位、只看体积量级——档位说的是"想要多好"，而流量场景说的是"最多
    能承受多大"，后者是硬约束。
    """
    if any_class_allowed():
        # 口径与 quality.on_lan 保持一致：只有**明确**判出局域网才放行本地无损，
        # unknown 一律按非局域网处理。否则会出现「在线降到 320k、本地却照灌
        # 30MB FLAC」这种自相矛盾——正是 2.9.21 修掉的「越修越慢」。
        if (not quality.on_lan(network) and cellular_lossy_only()
                and klass_of_ext(entry.get("ext") or "") == "lossless"):
            _CELL_SKIPS["n"] += 1
            return False
        return True
    return klass_of_ext(entry.get("ext") or "") == klass_of_level(level)


def status(db_path: str, library_dir: str = "") -> dict:
    """诊断页用的本地优先状态快照。

    这个模块之前最大的问题不是逻辑错，而是**无法自证**：索引空了、匹配没命中，
    谁也说不清。这里把「索引有多少首、从哪来的、最近查了什么、结果如何」
    全部摊开，功能没生效时能一眼看出卡在哪一环。
    """
    idx = _get_index(db_path, library_dir)
    meta = dict(_INDEX_META.get(
        f"{db_path}|{library_dir}" if library_dir else str(db_path), {}) or {})
    selftest: dict = {}
    for _k, entries in list(idx.items())[:1]:
        for e in entries[:1]:
            found = find_local_match(str(e.get("title") or ""),
                                     str(e.get("artist") or ""),
                                     db_path, library_dir)
            selftest = {"title": str(e.get("title") or ""),
                        "artist": str(e.get("artist") or ""),
                        "ok": found is not None,
                        "path": str((found or {}).get("path") or "")}
    return {
        "enabled": local_first_enabled(),
        "any_class": any_class_allowed(),
        "cellular_lossy_only": cellular_lossy_only(),
        "cellular_skips": int(_CELL_SKIPS.get("n") or 0),
        "db_path": db_path,
        "library_dir": library_dir,
        # entries 按**唯一 path** 统计：一首歌会登记好几个键（标题变体），
        # 直接数列表长度会翻好几倍，看着像索引爆炸了。titles 才是键的数量。
        "titles": len(idx),
        "entries": len({str(i.get("path") or "")
                        for es in idx.values() for i in es if i.get("path")}),
        "from_db": int(meta.get("db") or 0),
        "from_fs": int(meta.get("fs") or 0),
        # music.db 里文件已不存在的失效记录数（已剔除，不再挡住目录扫描的真文件）
        "db_broken": int(meta.get("db_broken") or 0),
        # fs_scanned 必须单独给：fs（新增）为 0 常常不是「没扫到」而是
        # 「扫到的全在 music.db 里已有」——真机第一次看到 0 会以为扫描坏了。
        "fs_scanned": int(meta.get("fs_scanned") or 0),
        "built_at": float(meta.get("ts") or 0.0),
        "empty_reason": str(meta.get("empty_reason") or ""),
        "lookups": len(_LOOKUP_LOG),
        "lookup_hits": sum(1 for r in _LOOKUP_LOG if r.get("hit")),
        "recent": [dict(r) for r in _LOOKUP_LOG[-10:]],
        "selftest": selftest,
    }


def reset_for_test() -> None:
    _INDEX_CACHE.clear()
    _INDEX_META.clear()
    _LOOKUP_LOG.clear()
    _CELL_SKIPS["n"] = 0
