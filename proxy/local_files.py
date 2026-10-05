"""本地曲库文件索引 + 标签/内嵌封面读取（v2.9.5）。

为什么要这个模块
----------------
本地每日推荐的曲目 guid 是我们自己造的 ``local:file:<sha1>``（路径指纹）。
官方后端不认识这个 guid，于是早先的实现里：

  * ``/track/metadata?guid=local:file:...`` → 转发官方后端 → 返回一堆空值，
    客户端拿到「有这首歌但没信息」的条目；
  * ``/static/cover?coverId=local:file:...&size=160`` → 转发官方后端 → **400**，
    表现为整张歌单没封面；
  * duration / size 恒为 0，客户端据此判定不可播 → 点了没反应。

根子是「曲目是我们造的，元数据却指望官方后端给」。正确做法：既然文件就在
本地磁盘上，标题/艺术家/时长/体积/内嵌封面全都自己从文件里读出来，代理直接
应答，一次都不用转发。

索引落盘的原因：guid 是 sha1 指纹，无法反推路径，必须记住 sha1 → path。
扫描时写入，metadata / cover / stream 三处共用。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time

logger = logging.getLogger("app")

INDEX_NAME = "local_files.json"
_COVER_DIRNAME = "localcover"

_LOCK = threading.Lock()
_MEM: dict[str, dict] | None = None      # sha1 -> {path,title,artist,ext}
_MEM_MTIME = 0.0
_PROBE_CACHE: dict[str, tuple[float, dict]] = {}
_PROBE_TTL = 3600.0

_SIBLING_COVER_NAMES = (
    "cover", "folder", "front", "album", "artwork", "back",
)


def sha1_of_path(path: str) -> str:
    return hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()


def _base_dir() -> str:
    """运行根目录：与 recommend.home_dir() 同源（FNMUSIC_HOME 可覆盖）。"""
    env = (os.environ.get("FNMUSIC_HOME") or "").strip()
    if env:
        return env
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def index_path() -> str:
    """索引文件位置：与代理的 cache_dir 同源，保证 metadata/cover/stream 三处
    读到的是同一份 sha1 → path 映射。"""
    cache = (os.environ.get("FNMUSIC_CACHE_DIR") or "").strip()
    base = cache or os.path.join(_base_dir(), "cache")
    return os.path.join(base, INDEX_NAME)


def _cover_dir() -> str:
    cache = (os.environ.get("FNMUSIC_CACHE_DIR") or "").strip()
    base = cache or os.path.join(_base_dir(), "cache")
    return os.path.join(base, _COVER_DIRNAME)


# ---------------------------------------------------------------------------
# 索引读写
# ---------------------------------------------------------------------------

def _load(force: bool = False) -> dict[str, dict]:
    global _MEM, _MEM_MTIME
    path = index_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        with _LOCK:
            _MEM = _MEM or {}
        return _MEM or {}
    with _LOCK:
        if _MEM is None or mtime != _MEM_MTIME or force:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                _MEM = data if isinstance(data, dict) else {}
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取本地文件索引失败: %s: %s", type(exc).__name__, exc)
                _MEM = _MEM or {}
            _MEM_MTIME = mtime
        return _MEM


def record_files(files: "list[dict]") -> None:
    """把一次曲库扫描的结果并入索引（幂等，线程安全）。"""
    if not files:
        return
    idx = dict(_load())
    for f in files:
        path = str(f.get("path") or "")
        if not path:
            continue
        idx[sha1_of_path(path)] = {
            "path": path,
            "title": str(f.get("title") or ""),
            "artist": str(f.get("artist") or ""),
            "ext": str(f.get("ext") or "").lstrip(".").lower(),
        }
    _write(idx)


def _write(idx: dict) -> None:
    global _MEM, _MEM_MTIME
    path = index_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(idx, fh, ensure_ascii=False)
        os.replace(tmp, path)
        with _LOCK:
            _MEM = idx
            _MEM_MTIME = os.path.getmtime(path)
    except Exception as exc:  # noqa: BLE001 - 索引写不进去只影响本地日推，不能拖垮代理
        logger.warning("写入本地文件索引失败: %s: %s", type(exc).__name__, exc)


def lookup(guid: str) -> dict | None:
    """guid（local:file:<sha1>）→ 条目。文件已被移走/删除则返回 None。"""
    sha = str(guid or "").split("local:file:", 1)[-1].strip()
    if not sha:
        return None
    ent = _load().get(sha)
    if not ent:
        return None
    path = str(ent.get("path") or "")
    if not path or not os.path.isfile(path):
        return None
    return ent


def known(guid: str) -> bool:
    """索引里有没有这个 guid（不碰文件系统，用于快速判断要不要补写）。"""
    sha = str(guid or "").split("local:file:", 1)[-1].strip()
    return bool(sha) and sha in _load()


def record_tracks(tracks: "list[dict]") -> None:
    """从已构建好的曲目列表（带 _local_path）补写索引，已有则跳过。"""
    if not tracks:
        return
    first = str((tracks[0] or {}).get("guid") or "")
    if known(first):
        return
    record_files([{
        "path": str(t.get("_local_path") or ""),
        "title": str(t.get("title") or ""),
        "artist": str(t.get("artist") or ""),
        "ext": str(t.get("ext") or t.get("format") or "").lstrip(".").lower(),
    } for t in tracks if t.get("_local_path")])


def resolve(guid: str) -> str | None:
    ent = lookup(guid)
    return str(ent.get("path") or "") if ent else None


# ---------------------------------------------------------------------------
# 标签探测
# ---------------------------------------------------------------------------

def probe(path: str) -> dict:
    """读音频文件的技术信息（时长/体积/码率/专辑/标题/艺术家）。

     mutagen 不可用或文件损坏时返回尽量可用的兜底值，绝不抛异常。
    """
    now = time.time()
    hit = _PROBE_CACHE.get(path)
    if hit and now - hit[0] < _PROBE_TTL:
        return hit[1]

    out = {
        "duration": 0, "duration_ms": 0, "size": 0, "bitrate": 0,
        "album": "", "title": "", "artist": "", "sample_rate": 0, "channels": 0,
    }
    try:
        out["size"] = os.path.getsize(path)
    except OSError:
        pass
    try:
        from mutagen import File as MutagenFile  # noqa: PLC0415

        audio = MutagenFile(path)
        if audio is not None:
            info = getattr(audio, "info", None)
            if info is not None:
                length = float(getattr(info, "length", 0) or 0)
                out["duration"] = int(length)
                out["duration_ms"] = int(length * 1000)
                out["bitrate"] = int(getattr(info, "bitrate", 0) or 0)
                out["sample_rate"] = int(getattr(info, "sample_rate", 0) or 0)
                out["channels"] = int(getattr(info, "channels", 0) or 0)
            tags = getattr(audio, "tags", None)
            if tags is not None:
                def _first(*keys):
                    for k in keys:
                        try:
                            v = tags[k]
                        except Exception:  # noqa: BLE001
                            continue
                        if isinstance(v, (list, tuple)):
                            v = v[0] if v else None
                        if v is None:
                            continue
                        try:
                            s = str(v)
                        except Exception:  # noqa: BLE001
                            continue
                        if s.strip():
                            return s.strip()
                    return ""

                out["title"] = _first("title", "TIT2", "\xa9nam", "songname")
                out["artist"] = _first("artist", "TPE1", "\xa9ART", "albumartist", "TPE2")
                out["album"] = _first("album", "TALB", "\xa9alb")
    except Exception as exc:  # noqa: BLE001
        logger.debug("probe tags failed for %s: %s: %s", path, type(exc).__name__, exc)

    _PROBE_CACHE[path] = (now, out)
    return out


def entry_with_probe(guid: str) -> dict | None:
    """条目 + 技术信息，metadata 应答用。"""
    ent = lookup(guid)
    if not ent:
        return None
    info = probe(str(ent.get("path") or ""))
    return {**ent, **info}


# ---------------------------------------------------------------------------
# 封面
# ---------------------------------------------------------------------------

def _embedded_cover(path: str) -> tuple[bytes, str] | None:
    """从音频文件里取内嵌封面：FLAC/ogg 图片块、ID3 APIC、MP4 covr、ASF 图片。"""
    try:
        from mutagen import File as MutagenFile  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        audio = MutagenFile(path)
    except Exception:  # noqa: BLE001
        return None
    if audio is None:
        return None

    # FLAC / Ogg 系列：pictures
    try:
        pics = getattr(audio, "pictures", None)
        if pics:
            for p in pics:
                data = getattr(p, "data", b"") or b""
                if data:
                    return data, (getattr(p, "mime", "") or "image/jpeg")
    except Exception:  # noqa: BLE001
        pass

    tags = getattr(audio, "tags", None)
    if tags is None:
        return None

    # MP3 / ID3：APIC
    try:
        getall = getattr(tags, "getall", None)
        if callable(getall):
            for pic in getall("APIC"):
                data = getattr(pic, "data", b"") or b""
                if data:
                    return data, (getattr(pic, "mime", "") or "image/jpeg")
    except Exception:  # noqa: BLE001
        pass

    # MP4/M4A：covr
    try:
        covr = tags.get("covr", None)
        if covr:
            first = covr[0] if isinstance(covr, (list, tuple)) else covr
            data = bytes(first)
            if data:
                fmt = int(getattr(first, "imageformat", 0) or 0)
                mime = "image/png" if fmt in (14, 4) else "image/jpeg"
                return data, mime
    except Exception:  # noqa: BLE001
        pass

    # WMA/ASF：WM/Picture
    try:
        pics = tags.get("WM/Picture", None)
        if pics:
            first = pics[0] if isinstance(pics, (list, tuple)) else pics
            data = getattr(first, "value", b"") or b""
            if data:
                return bytes(data), "image/jpeg"
    except Exception:  # noqa: BLE001
        pass
    return None


def _sibling_cover(path: str) -> tuple[bytes, str] | None:
    """同目录下的封面图：cover.jpg / folder.jpg / front.jpg / 同名图 等。"""
    try:
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            return None
        stem = os.path.splitext(os.path.basename(path))[0]
        wanted = [n + e for n in _SIBLING_COVER_NAMES
                  for e in (".jpg", ".jpeg", ".png")]
        wanted = [stem + e for e in (".jpg", ".jpeg", ".png")] + wanted
        for name in wanted:
            cand = os.path.join(folder, name)
            if os.path.isfile(cand):
                try:
                    with open(cand, "rb") as fh:
                        data = fh.read()
                except OSError:
                    continue
                if data:
                    mime = "image/png" if cand.lower().endswith(".png") else "image/jpeg"
                    return data, mime
    except Exception:  # noqa: BLE001
        return None
    return None


def _cache_file(sha: str) -> str:
    return os.path.join(_cover_dir(), sha[:2], sha + ".img")


def cover(guid: str) -> tuple[bytes, str] | None:
    """取曲目封面：内嵌 → 同目录图片 → None（调用方再决定占位图）。

    抽出来的图按 sha1 落盘缓存，避免列表滚动时反复解 flac 标签。
    """
    ent = lookup(guid)
    if not ent:
        return None
    path = str(ent.get("path") or "")
    sha = str(guid or "").split("local:file:", 1)[-1].strip()

    cached_file = _cache_file(sha)
    try:
        if os.path.isfile(cached_file) and os.path.getsize(cached_file) > 0:
            with open(cached_file, "rb") as fh:
                data = fh.read()
            with open(cached_file + ".mime", "r", encoding="utf-8") as fh:
                mime = fh.read().strip() or "image/jpeg"
            return data, mime
    except OSError:
        pass

    found = _embedded_cover(path) or _sibling_cover(path)
    if not found:
        # 记一个空标记，下次别再解一遍标签（曲库里大量无封面文件时会很明显）
        try:
            os.makedirs(os.path.dirname(cached_file), exist_ok=True)
            with open(cached_file, "wb") as fh:
                fh.write(b"")
        except OSError:
            pass
        return None

    data, mime = found
    try:
        os.makedirs(os.path.dirname(cached_file), exist_ok=True)
        with open(cached_file, "wb") as fh:
            fh.write(data)
        with open(cached_file + ".mime", "w", encoding="utf-8") as fh:
            fh.write(mime)
    except OSError as exc:
        logger.debug("缓存本地封面失败: %s", exc)
    return data, mime


def reset_for_test() -> None:
    global _MEM, _MEM_MTIME
    with _LOCK:
        _MEM = None
        _MEM_MTIME = 0.0
    _PROBE_CACHE.clear()
