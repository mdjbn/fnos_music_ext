"""网易云 song_info → 扩展统一条目的映射。

musicbox 服务把搜索结果（``/api/v1/search``）与每日推荐（``/api/v1/recommend/daily``）
都归一化成 NEMbox 的 song_info 结构，字段名一致，因此这里只写一份映射，
被 proxy/app.py（搜索聚合）与 proxy/recommend.py（每日推荐）共用。
"""
from __future__ import annotations

from typing import Any

SOURCE_NAME = "netease"

# 无损判定词汇。必须同时覆盖两套来源：
#  - NEMbox CLI / Parse.song_url 的真实输出："LOSSLESS FLAC"、"HIRES FLAC"、
#    "JYMASTER FLAC"、"EXHIGH FLAC"、"HD 320k"、"LD 128k"
#  - 早期测试与部分上游版本用的缩写："SQ"、"HR"、"无损"
# 只认 SQ/HR 的话，线上真实数据永远是 mp3，用户拿不到无损格式声明。
LOSSLESS_QUALITY_MARKERS = (
    "SQ", "HR", "无损",
    "LOSSLESS", "HIRES", "JYMASTER", "FLAC",
)


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def song_id_of(raw: dict) -> str:
    return str(raw.get("song_id") or raw.get("id") or "")


def has_lossless(raw: dict) -> bool:
    """判断该曲目是否无损/Hi-Res。

    注意 999000+ 的码率上游会直接标成 "LOSSLESS"（无 FLAC 字样），
    纯靠子串匹配即可覆盖；"HD 320k"、"LD 128k" 属于有损，不会被误判。
    """
    quality = str(raw.get("quality") or "").upper()
    if not quality:
        return False
    return any(marker in quality for marker in LOSSLESS_QUALITY_MARKERS)


def map_netease_song(raw: dict) -> dict | None:
    """把一条 musicbox song_info 转成扩展内部条目；缺 id 时返回 None。

    musicbox 的搜索 / 每日推荐 / 歌单曲目 / 私人FM 返回的都是 ``_song_info_from_raw``
    归一化后的 song_info，其中**已经带有** ``album_pic_url`` 封面与 ``has_sq`` /
    ``has_hr`` 无损标记。原先这里一律丢弃（``cover_url=""``、仅看 quality 字符串），
    迫使调用方再走一次 ``/api/v1/songs/detail`` 补封面——那在上游要多做
    songs_detail + songs_url 两次跨洋往返，正是「打开歌单/榜单慢」的主要构成之一。
    能直读的就直读，enrich 只补真正缺的字段。
    """
    if not isinstance(raw, dict):
        return None
    sid = song_id_of(raw)
    if not sid:
        return None

    duration = _to_float(raw.get("duration"))
    # NEMbox 的 duration 是秒，但个别上游版本会直接给毫秒，超过 1 万秒的一律按毫秒还原
    if duration > 10_000:
        duration /= 1000.0

    lossless = has_lossless(raw) or bool(raw.get("has_sq")) or bool(raw.get("has_hr"))
    return {
        "id": f"{SOURCE_NAME}:{sid}",
        "source": SOURCE_NAME,
        "title": str(raw.get("song_name") or raw.get("title") or raw.get("name") or ""),
        "artist": str(raw.get("artist") or ""),
        "album": str(raw.get("album_name") or raw.get("album") or ""),
        "duration_s": duration,
        "ext": "flac" if lossless else "mp3",
        "cover_url": str(raw.get("album_pic_url") or raw.get("cover_url") or ""),
        "lyric": "",
    }


def apply_song_detail(item: dict, detail: dict) -> dict:
    """用 /api/v1/songs/detail 的结果补齐封面与音质判定（原地更新并返回）。"""
    if not isinstance(detail, dict):
        return item
    cover = str(detail.get("album_pic_url") or "")
    if cover:
        item["cover_url"] = cover
    if detail.get("has_sq") or detail.get("has_hr"):
        item["ext"] = "flac"
    if not item.get("album"):
        item["album"] = str(detail.get("album_name") or "")
    if not item.get("artist"):
        item["artist"] = str(detail.get("artist") or "")
    return item
