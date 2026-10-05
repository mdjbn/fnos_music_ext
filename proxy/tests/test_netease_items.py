"""网易云 song_info → 统一条目映射测试（搜索链路与日推链路共用）。"""
import pytest

from proxy import netease_items


def test_source_name_is_netease():
    assert netease_items.SOURCE_NAME == "netease"


def test_song_id_of_prefers_song_id_then_id():
    assert netease_items.song_id_of({"song_id": "123", "id": "999"}) == "123"
    assert netease_items.song_id_of({"id": 999}) == "999"
    assert netease_items.song_id_of({}) == ""
    assert netease_items.song_id_of({"song_id": None, "id": None}) == ""


@pytest.mark.parametrize("quality,expected", [
    ("SQ", True), ("sq", True), ("SQ 2.4M", True), ("HR 1.9G", True),
    ("无损", True), ("FLAC 无损", True), ("LD 128k", False), ("", False),
    (None, False), (123, False),
])
def test_has_lossless(quality, expected):
    assert netease_items.has_lossless({"quality": quality}) is expected


def test_map_netease_song_full():
    item = netease_items.map_netease_song({
        "song_id": "228908",
        "song_name": "晴天",
        "artist": "周杰伦",
        "album_name": "叶惠美",
        "duration": 269,
        "quality": "SQ",
    })
    assert item == {
        "id": "netease:228908",
        "source": "netease",
        "title": "晴天",
        "artist": "周杰伦",
        "album": "叶惠美",
        "duration_s": 269.0,
        "ext": "flac",
        "cover_url": "",
        "lyric": "",
    }


def test_map_netease_song_lossy_is_mp3():
    item = netease_items.map_netease_song({"song_id": "1", "song_name": "t", "quality": "LD 128k"})
    assert item["ext"] == "mp3"


def test_map_netease_song_reads_pic_url_and_sq_hr():
    """v2.7：musicbox song_info 已带 album_pic_url / has_sq / has_hr，必须直读，
    免得歌单/榜单打开还要再走一次 songs/detail 补封面（上游两次跨洋往返）。"""
    item = netease_items.map_netease_song({
        "song_id": "228908",
        "song_name": "晴天",
        "artist": "周杰伦",
        "album_name": "叶惠美",
        "duration": 269,
        "quality": "HD 320k",          # quality 字符串看不出无损
        "has_sq": True,                 # 但 song_info 带了无损标记
        "album_pic_url": "https://p1.music.126.net/abc.jpg",
    })
    assert item["ext"] == "flac", "has_sq 必须能推出无损扩展名"
    assert item["cover_url"] == "https://p1.music.126.net/abc.jpg"

    hr_item = netease_items.map_netease_song({
        "song_id": "2", "song_name": "t", "quality": "", "has_hr": True,
    })
    assert hr_item["ext"] == "flac"

    no_cover = netease_items.map_netease_song({
        "song_id": "3", "song_name": "t", "quality": "HD 320k", "album_pic_url": "",
    })
    assert no_cover["cover_url"] == ""
    assert no_cover["ext"] == "mp3"


def test_map_netease_song_requires_id():
    assert netease_items.map_netease_song({"song_name": "无 id"}) is None
    assert netease_items.map_netease_song({}) is None
    assert netease_items.map_netease_song({"song_id": ""}) is None
    assert netease_items.map_netease_song(None) is None
    assert netease_items.map_netease_song("not a dict") is None


@pytest.mark.parametrize("raw,expected", [
    # NEMbox 归一化后 duration 是秒
    (269, 269.0),
    ("269", 269.0),
    (269.5, 269.5),
    (0, 0.0),
    (None, 0.0),
    ("abc", 0.0),
    # 个别上游直接给毫秒，超过 1 万秒的一律还原
    (269000, 269.0),
    (151304, 151.304),
])
def test_map_netease_song_duration_normalization(raw, expected):
    item = netease_items.map_netease_song({"song_id": "1", "song_name": "t", "duration": raw})
    assert item["duration_s"] == pytest.approx(expected, rel=1e-6)


def test_map_netease_song_accepts_alternate_field_names():
    """标题取值优先级 song_name > title > name，与 v1.x 的搜索映射保持一致。"""
    item = netease_items.map_netease_song(
        {"id": "7", "song_name": "夜曲S", "name": "夜曲N", "title": "夜曲T",
         "artist": "周杰伦", "album": "十一月的萧邦"}
    )
    assert item["title"] == "夜曲S"
    assert item["album"] == "十一月的萧邦"

    # 只有 title
    assert netease_items.map_netease_song({"song_id": "8", "title": "只用 title"})["title"] == "只用 title"
    # 只有 name
    assert netease_items.map_netease_song({"song_id": "9", "name": "只用 name"})["title"] == "只用 name"
    # 专辑同理：album_name > album
    both = netease_items.map_netease_song({"song_id": "10", "album_name": "AN", "album": "A"})
    assert both["album"] == "AN"
    assert netease_items.map_netease_song({"song_id": "11", "album": "A"})["album"] == "A"
    # 三者全缺 → 空串（由上层 is_playable_online_track 拦掉，不在这里臆造）
    assert netease_items.map_netease_song({"song_id": "12"})["title"] == ""


def test_apply_song_detail_fills_cover_and_lossless():
    item = {"id": "netease:1", "source": "netease", "title": "t", "artist": "歌手A",
            "album": "专辑A", "duration_s": 100, "ext": "mp3", "cover_url": "", "lyric": ""}
    out = netease_items.apply_song_detail(item, {
        "album_pic_url": "http://img/c.jpg", "has_sq": True, "has_hr": False,
        "album_name": "专辑X", "artist": "歌手X",
    })
    assert out is item, "应原地更新并返回同一对象"
    assert out["cover_url"] == "http://img/c.jpg"
    assert out["ext"] == "flac", "详情说有 SQ 就该改判为无损"
    # 已有值一律不被详情覆盖
    assert out["album"] == "专辑A"
    assert out["artist"] == "歌手A"


def test_apply_song_detail_fills_missing_album_and_artist():
    item = {"id": "netease:1", "album": "", "artist": "", "ext": "mp3", "cover_url": ""}
    netease_items.apply_song_detail(item, {"album_name": "专辑Y", "artist": "歌手Y"})
    assert item["album"] == "专辑Y"
    assert item["artist"] == "歌手Y"


def test_apply_song_detail_hr_also_lossless():
    item = {"id": "netease:1", "ext": "mp3", "cover_url": "", "album": "", "artist": ""}
    netease_items.apply_song_detail(item, {"has_sq": False, "has_hr": True})
    assert item["ext"] == "flac"


def test_apply_song_detail_tolerates_empty_detail():
    item = {"id": "netease:1", "ext": "mp3", "cover_url": "", "album": "A", "artist": "B"}
    netease_items.apply_song_detail(item, {})
    assert item["ext"] == "mp3" and item["cover_url"] == ""


def test_apply_song_detail_ignores_non_dict():
    item = {"id": "netease:1", "ext": "mp3", "cover_url": ""}
    for junk in (None, "x", 123, []):
        assert netease_items.apply_song_detail(item, junk) is item
    assert item["ext"] == "mp3"


def test_apply_song_detail_never_downgrades_lossless():
    """详情没标 SQ/HR 时不应把已判定为无损的曲目改回 mp3。"""
    item = {"id": "netease:1", "ext": "flac", "cover_url": "", "album": "", "artist": ""}
    netease_items.apply_song_detail(item, {"has_sq": False, "has_hr": False,
                                           "album_pic_url": "http://c"})
    assert item["ext"] == "flac"
    assert item["cover_url"] == "http://c"


# ===========================================================================
# 代理侧试听判定 —— 与 musicbox 侧同源的这个坑
#
# proxy/app.py 曾用 item.get("freeTrialInfo") or item.get("freeTrialPrivilege")
# 判试听。freeTrialPrivilege 是网易云每条 song/url 响应都必带的结构体，恒为真理值，
# 于是任何喂进上游原始条目的调用方（is_playable_online_track 共 5 处调用，
# 其中之一直接吃 raw）都会把全部曲目杀光。
# ===========================================================================

from proxy.app import _has_trial_fragment, is_playable_online_track

_REAL_OK = {
    "title": "拉布拉多", "id": 1998849460,
    "download_url": "http://m701.music.126.net/x.mp3?vuutv=abc",
    "url": "http://m701.music.126.net/x.mp3?vuutv=abc",
    "fee": 8, "code": 200,
    "freeTrialInfo": None,            # None = 无试听
    "freeTrialPrivilege": {           # 恒存在，存在 ≠ 试听
        "resConsumable": False, "userConsumable": False,
        "listenType": None, "cannotListenReason": None,
        "playReason": None, "freeLimitTagType": None,
    },
}


def test_free_trial_privilege_presence_does_not_reject():
    """核心回归：带恒存在的 freeTrialPrivilege 的正常曲目必须放行。"""
    assert _has_trial_fragment(_REAL_OK) is False
    assert is_playable_online_track(_REAL_OK) is True, "修复前这里必被误杀"


@pytest.mark.parametrize("flag_key", ["resConsumable", "userConsumable"])
def test_trial_detected_via_inner_booleans(flag_key):
    bad = dict(_REAL_OK)
    bad["freeTrialPrivilege"] = dict(_REAL_OK["freeTrialPrivilege"], **{flag_key: True})
    assert _has_trial_fragment(bad) is True
    assert is_playable_online_track(bad) is False
    # 不同接口序列化不一致，字符串 "true" 也要认
    s = dict(_REAL_OK)
    s["freeTrialPrivilege"] = dict(_REAL_OK["freeTrialPrivilege"], **{flag_key: "true"})
    assert _has_trial_fragment(s) is True


def test_free_trial_info_non_empty_means_trial():
    """freeTrialInfo 语义相反：非空即真试听，None/空才是无试听。"""
    t = dict(_REAL_OK)
    t["freeTrialInfo"] = {"st": 0, "et": 60}
    assert _has_trial_fragment(t) is True
    assert is_playable_online_track(t) is False

    assert _has_trial_fragment({"freeTrialInfo": None}) is False
    assert _has_trial_fragment({"freeTrialInfo": {}}) is False


def test_is_trial_flag_still_honoured():
    """显式 is_trial 标记照旧拦截，别被这次修复顺手放宽。"""
    t = dict(_REAL_OK, is_trial=True)
    assert is_playable_online_track(t) is False


def test_has_trial_fragment_never_raises_on_junk():
    for junk in ({}, {"freeTrialPrivilege": None}, {"freeTrialPrivilege": "x"},
                 {"freeTrialInfo": []}, {"freeTrialPrivilege": 7}):
        assert _has_trial_fragment(junk) is False
    assert _has_trial_fragment({"freeTrialInfo": "y"}) is True
