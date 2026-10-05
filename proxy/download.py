"""点了收藏就落盘：按歌手建子文件夹，存账号可得的最高品质音频 + 同名 .lrc。

与「边播边存」的关系：那条路径是**播放时**顺手缓存到飞牛曲库目录（为了让重播不再
走网络）。这里是**用户显式收藏**时的主动归档，两者目录不同、语义不同：
归档目录由用户在管理页自定义，可以指向任意一个飞牛会扫描的音乐文件夹，
落盘的是该账号能拿到的最高品质（jymaster → hires → lossless → exhigh 逐档降级）。

失败一律不抛给调用方：收藏动作本身必须成功，下载失败只能记日志并在返回里带
``download_status``，否则用户点一下收藏就报错，反而把本来能用的收藏功能弄坏。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any
from uuid import uuid4

logger = logging.getLogger("fnmusic_proxy")

# 逐档降级顺序：与 musicbox 侧 BEST_QUALITY_CHAIN 保持一致，用于记录实际拿到的档位
QUALITY_RANK = {"jymaster": 4, "hires": 3, "lossless": 2, "exhigh": 1,
                "higher": 0, "standard": 0}
# 上游 QUALITY_LEVELS 里还有 sky / jyeffect 两档（不在降级 rate_map、也不在音质白名单里），
# 万一响应带回它们的 level，按"未知名次"处理即落到 -1，不会因此误判成"更高品质"而反复重下。

# 归档路径黑名单：宁可拒绝，也绝不在这些位置里递归建目录写文件。
# 用户填错路径的后果是往系统目录里写大量音频文件，清理起来极其麻烦。
FORBIDDEN_DIRS = (
    "/", "/etc", "/bin", "/sbin", "/usr", "/var", "/boot", "/proc", "/sys", "/dev",
    "/vol1/@appdata", "/vol1/@appcenter", "/vol1/@appvar", "/root",
)

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _flag(name: str, default: str) -> bool:
    return str(os.environ.get(name, default) or default).strip().lower() in ("1", "true", "yes", "on")


def download_dir() -> str:
    """归档目录（未配置则为空字符串，等于关闭该功能）。"""
    return str(os.environ.get("FNMUSIC_DOWNLOAD_DIR", "") or "").strip()


def download_enabled() -> bool:
    """是否启用「收藏时自动下载」。目录没配就视为关闭。"""
    return bool(download_dir()) and _flag("FNMUSIC_DOWNLOAD_ON_FAVORITE", "true")


def like_sync_enabled() -> bool:
    return _flag("FNMUSIC_FAV_SYNC_LIKE", "false")


# ---------------------------------------------------------------------------
# 路径安全与命名
# ---------------------------------------------------------------------------


def validate_dir(path: str) -> tuple[bool, str]:
    """校验归档目录。返回 (可用, 原因)。

    必须绝对路径、必须已经存在（不自动创建用户填错的深层路径）、不得是黑名单里的
    系统目录本身、必须可写。
    """
    p = str(path or "").strip()
    if not p:
        return False, "未配置归档目录"
    if not os.path.isabs(p):
        return False, f"必须是绝对路径：{p}"
    norm = os.path.normpath(p)
    if norm in FORBIDDEN_DIRS or norm.rstrip("/") in FORBIDDEN_DIRS:
        return False, f"拒绝使用系统目录 {norm}"
    if not os.path.isdir(norm):
        return False, f"目录不存在：{norm}（请先在飞牛文件管理里建好）"
    if not os.access(norm, os.W_OK):
        return False, f"目录不可写：{norm}"
    return True, "ok"


def safe_component(name: str, max_len: int = 80) -> str:
    """把歌手名/歌名清洗成安全的文件名分量。

    网易云的歌手名里常有 ``/``（多个歌手以 ``/`` 分隔）、``:`` 等非法字符，
    直接当目录名会创建出错误的层级或失败。
    """
    s = str(name or "").strip()
    s = _ILLEGAL.sub("_", s)
    s = re.sub(r"\s+", " ", s).strip(" ._")
    if not s:
        s = "未知"
    return s[:max_len].strip(" ._") or "未知"


def target_paths(artist: str, title: str, ext: str) -> tuple[str, str]:
    """``{dir}/{歌手}/{歌手} - {歌名}.{ext}`` 与同名 ``.lrc``。"""
    base = download_dir()
    artist_dir = safe_component(artist)
    stem = f"{artist_dir} - {safe_component(title, max_len=120)}"
    folder = os.path.join(base, artist_dir)
    audio = os.path.join(folder, f"{stem}.{ext.lstrip('.')}")
    lrc = os.path.join(folder, f"{stem}.lrc")
    return audio, lrc


def _ext_for(info: dict) -> str:
    """按上游返回的 type / encodeType / level 决定扩展名。"""
    t = str(info.get("type") or info.get("encodeType") or "").lower()
    if t in ("flac", "ape", "wav", "alac"):
        return t if t != "alac" else "m4a"
    level = str(info.get("level") or "").lower()
    if level in ("lossless", "hires", "jymaster"):
        return "flac"
    return "mp3"


# ---------------------------------------------------------------------------
# 归档任务队列
# ---------------------------------------------------------------------------

_TASKS: dict[str, asyncio.Task] = {}


def pending_count() -> int:
    return len([t for t in _TASKS.values() if t is not None and not t.done()])


def _quality_rank_of(name: str) -> int:
    return QUALITY_RANK.get(str(name or "").lower(), -1)


async def _fetch_best_url(client, song_id: str) -> dict:
    r = await client.get(f"/api/v1/song/{song_id}/best_url", timeout=25.0)
    if r.status_code != 200:
        return {}
    body = r.json()
    if not isinstance(body, dict) or body.get("ok") is not True:
        return {}
    data = body.get("data")
    return data if isinstance(data, dict) else {}


async def _fetch_lyric(client, song_id: str) -> str:
    try:
        r = await client.get(f"/api/v1/song/{song_id}/lyric", timeout=15.0)
        if r.status_code != 200:
            return ""
        body = r.json()
        if isinstance(body, dict) and body.get("ok") is not False:
            data = body.get("data") or {}
            if isinstance(data, dict):
                return str(data.get("lyric") or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("archive lyric fetch failed for %s: %s: %s", song_id, type(exc).__name__, exc)
    return ""


def _write_tags(path: str, meta: dict) -> None:
    """写 id3/flac 标签，让飞牛与本地播放器都能正确显示歌名歌手专辑。"""
    try:
        import mutagen
    except Exception:  # noqa: BLE001 - 标签写不上不影响文件可用
        return
    title = str(meta.get("title") or "")
    artist = str(meta.get("artist") or "")
    album = str(meta.get("album") or "")
    try:
        audio = mutagen.File(path)
        if audio is None:
            return
        ext = os.path.splitext(path)[1].lower()
        if ext == ".flac":
            audio["title"] = title
            audio["artist"] = artist
            audio["album"] = album
        elif ext == ".mp3":
            from mutagen.id3 import TIT2, TPE1, TALB

            audio.add_tags() if audio.tags is None else None
            audio.tags.add(TIT2(encoding=3, text=title))
            audio.tags.add(TPE1(encoding=3, text=artist))
            audio.tags.add(TALB(encoding=3, text=album))
        audio.save()
    except Exception as exc:  # noqa: BLE001
        logger.warning("tag write failed for %s: %s: %s", path, type(exc).__name__, exc)


def _write_marker(audio_path: str, song_id: str, quality: str, size: int) -> None:
    """同名 ``.fnmusic.json`` 记录来源与品质，用于卸载时精确清理与重复下载判定。

    刻意用 sidecar 而不是把信息塞进音频文件名：文件名要给人看（歌手 - 歌名），
    元信息放这里，卸载脚本按 sidecar 删，绝不用通配符扫目录。
    """
    try:
        with open(audio_path + ".fnmusic.json", "w", encoding="utf-8") as fh:
            fh.write("{\"song_id\":\"%s\",\"quality\":\"%s\",\"size\":%d,\"ts\":%d}\n"
                     % (song_id, quality, int(size), int(time.time())))
    except OSError as exc:
        logger.warning("archive marker write failed: %s: %s", type(exc).__name__, exc)


def read_marker(audio_path: str) -> dict:
    try:
        with open(audio_path + ".fnmusic.json", "r", encoding="utf-8") as fh:
            import json

            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


async def archive_song(
    client,
    song_id: str,
    meta: dict,
    ref_writer: Any = None,
) -> dict:
    """归档单曲：取最高品质直链 → 流式写盘 → 写歌词与标签 → 记录 sidecar。

    ``ref_writer(audio_path)`` 由调用方注入，用于把归档文件登记进飞牛扩展的
    ``.ref`` 记录（卸载时可以精确删除，绝不通配符扫目录）。
    """
    ok_dir, reason = validate_dir(download_dir())
    if not ok_dir:
        return {"ok": False, "reason": reason}

    info = await _fetch_best_url(client, song_id)
    url = str(info.get("url") or "").strip()
    if not url:
        return {"ok": False, "reason": "no_playable_quality"}
    quality = str(info.get("best_quality") or info.get("level") or "")
    ext = _ext_for(info)

    title = str(meta.get("title") or meta.get("song_name") or f"track_{song_id}")
    artist = str(meta.get("artist") or "未知歌手")
    album = str(meta.get("album") or "")
    audio_path, lrc_path = target_paths(artist, title, ext)

    # 已有同曲目、且品质不低于本次的归档 → 跳过（换首歌名重下会留两份垃圾文件）
    if os.path.isfile(audio_path):
        existing = read_marker(audio_path)
        if _quality_rank_of(str(existing.get("quality") or quality)) >= _quality_rank_of(quality):
            return {"ok": True, "skipped": "already_archived", "path": audio_path,
                    "quality": str(existing.get("quality") or quality)}
        # 拿到了更高品质：先写新文件再替换，避免中途失败留下半截文件
    os.makedirs(os.path.dirname(audio_path), exist_ok=True)
    tmp = f"{audio_path}.{uuid4().hex[:8]}.part"
    written = 0
    try:
        async with client.stream("GET", url, timeout=120.0, follow_redirects=True) as resp:
            if resp.status_code != 200:
                return {"ok": False, "reason": f"upstream_http_{resp.status_code}"}
            declared = int(resp.headers.get("content-length") or 0)
            with open(tmp, "wb") as fh:
                async for chunk in resp.aiter_bytes(65536):
                    if chunk:
                        fh.write(chunk)
                        written += len(chunk)
    except Exception as exc:  # noqa: BLE001
        _discard(tmp)
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:160]}

    # 半截文件绝不能当归档留下：宁可没有，也不能让飞牛扫到一段坏音频
    if written < 1024 or (declared and written < declared):
        _discard(tmp)
        return {"ok": False, "reason": f"incomplete_download:{written}/{declared or '?'}"}

    try:
        os.replace(tmp, audio_path)
    except OSError as exc:
        _discard(tmp)
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:160]}

    lyric = await _fetch_lyric(client, song_id)
    if lyric.strip():
        try:
            with open(lrc_path, "w", encoding="utf-8") as fh:
                fh.write(lyric if lyric.endswith("\n") else lyric + "\n")
        except OSError as exc:
            logger.warning("archive lrc write failed: %s: %s", type(exc).__name__, exc)
    _write_tags(audio_path, {"title": title, "artist": artist, "album": album})
    _write_marker(audio_path, str(song_id), quality, written)
    if ref_writer is not None:
        try:
            ref_writer(audio_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("archive ref write failed: %s: %s", type(exc).__name__, exc)
    logger.info("archived song %s -> %s (%s, %.1f MB)", song_id, audio_path, quality,
                written / 1048576.0)
    return {"ok": True, "path": audio_path, "quality": quality, "size": written,
            "lrc": bool(lyric.strip())}


def _discard(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def enqueue(client, song_id: str, meta: dict, ref_writer: Any = None) -> str:
    """把归档任务丢到后台，立即返回（收藏接口绝不能等下载完）。

    同一首歌已有在跑的任务时直接复用，避免用户连点收藏造成重复下载。
    """
    prev = _TASKS.get(song_id)
    if prev is not None and not prev.done():
        return "in_progress"

    async def _run():
        try:
            await archive_song(client, song_id, meta, ref_writer=ref_writer)
        except Exception as exc:  # noqa: BLE001
            logger.warning("archive task failed for %s: %s: %s", song_id, type(exc).__name__, exc)
        finally:
            _TASKS.pop(song_id, None)

    try:
        _TASKS[song_id] = asyncio.get_event_loop().create_task(_run())
    except RuntimeError:
        # 没有运行中的事件循环（例如同步上下文里误调）——如实记下来，不静默丢弃
        logger.warning("archive enqueue skipped for %s: no running event loop", song_id)
        return "not_enqueued"
    return "enqueued"
