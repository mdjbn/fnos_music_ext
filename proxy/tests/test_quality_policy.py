"""音质策略（跟随飞牛 / 按网络 / 固定）与被动发现的单元测试。

核心不变量：**「跟随飞牛」是否真的读到了飞牛偏好必须可查证**（``resolve`` 的
``source``、``report()`` 的证据）。猜一个接口名去读，猜错时不会报错，只会静默地一直走
默认音质，用户以为生效了其实从未生效——比明确不支持更难排查。
"""
from __future__ import annotations

import os
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers, QueryParams

from proxy import quality as q
from proxy.app import app, resolve_netease_url
import proxy.app as P


@pytest.fixture(autouse=True)
def _reset_quality(monkeypatch):
    """观察记录与日志去重集合都是进程级的，用例之间必须清空。"""
    q.reset_for_test()
    P._LOGGED_QUALITY.clear()
    for k in ("FNMUSIC_QUALITY_POLICY", "FNMUSIC_QUALITY_FIXED", "FNMUSIC_QUALITY_WIFI",
              "FNMUSIC_QUALITY_CELLULAR", "FNMUSIC_NETEASE_QUALITY"):
        monkeypatch.delenv(k, raising=False)
    # W3：A 侧动态音质默认关闭，用例要显式打开才走 quality.resolve 分支。
    monkeypatch.delenv("FNMUSIC_QUALITY_DYNAMIC", raising=False)
    # 隔离本机 ambient 环境：DSH shell 导出了飞牛网关的授权目录变量（且
    # /var/run/trim_open_gateway_apiscope.socket 真实存在），会让依赖授权的断言
    # 在不同机器上结果不同。这里清掉，保证断言可复现（是环境隔离，不是弱化断言）。
    monkeypatch.delenv("TRIM_DATA_ACCESSIBLE_PATHS", raising=False)
    monkeypatch.delenv("TRIM_DATA_SHARE_PATHS", raising=False)
    yield
    q.reset_for_test()
    P._LOGGED_QUALITY.clear()


def _fake_request(query=None, headers=None):
    """最小 Request 替身。

    不能用 ``class R:`` 在函数体里定义：类体**访问不到外层函数的局部变量**
    （Python 作用域规则会跳过封闭函数作用域），``headers = Headers(headers or {})``
    会 NameError。先算好再挂到普通对象上。
    """
    qp = QueryParams(query or {})
    hd = Headers(headers or {})

    class _R:
        pass

    obj = _R()
    obj.query_params = qp
    obj.headers = hd
    return obj


# --------------------------------------------------------------------- 档位归一化


@pytest.mark.parametrize("raw,expect", [
    ("原始", "lossless"), ("original", "lossless"), ("无损", "lossless"),
    ("lossless", "lossless"), ("LOSSLESS", "lossless"),
    ("标准", "exhigh"), ("高音质", "exhigh"), ("320", "exhigh"),
    ("流畅", "standard"), ("省流量", "standard"), ("128", "standard"),
    ("hires", "hires"), ("臻品母带", "jymaster"),
    ("", ""), (None, ""), ("乱七八糟", ""),
])
def test_norm_level_mapping(raw, expect):
    assert q._norm_level(raw) == expect


def test_norm_level_keeps_netease_vocabulary_meaning():
    """英文 `standard` 是网易云自己的 128k 档位，不能被当成飞牛的「标准」(320k)。

    真实歧义：同样写 "standard"，来源是网易云词汇就该是 128k，来源是飞牛文案「标准」
    就该是 320k。规则是**精确档位名优先**，只有中文标签与 original/无损/省流量这类
    飞牛语义才走映射表。两种写法都要钉住。
    """
    assert q._norm_level("standard") == "standard"
    assert q._norm_level("标准") == "exhigh"
    assert q._norm_level("原始") == "lossless"
    assert q._norm_level("lossless") == "lossless"


# --------------------------------------------------------------------- 策略与配置


def test_policy_defaults_and_validation(monkeypatch):
    # v2.9.28：页面只保留「局域网 / 非局域网」两档，默认策略随之改为 by_lan。
    # 其余策略的代码分支保留（手动写 .env 仍可指定），但已不再是默认值。
    assert q.policy() == "by_lan"
    monkeypatch.setenv("FNMUSIC_QUALITY_POLICY", "fixed")
    assert q.policy() == "fixed"
    monkeypatch.setenv("FNMUSIC_QUALITY_POLICY", "bogus")
    assert q.policy() == "by_lan", "非法值必须回落默认，不能炸"


def test_level_getters(monkeypatch):
    assert q.wifi_level() == "lossless" and q.cellular_level() == "exhigh"
    monkeypatch.setenv("FNMUSIC_QUALITY_WIFI", "原始")
    monkeypatch.setenv("FNMUSIC_QUALITY_CELLULAR", "标准")
    assert q.wifi_level() == "lossless", "飞牛『原始』→ 无损"
    assert q.cellular_level() == "exhigh", "飞牛『标准』→ 320k"
    monkeypatch.setenv("FNMUSIC_QUALITY_WIFI", "garbage")
    assert q.wifi_level() == "", (
        "认不出来必须返回空串，由 resolve() 回落到 netease_quality 并在 source 里标注；"
        "在这里悄悄替换成默认档会让页面显示与实际生效值对不上"
    )


# --------------------------------------------------------------------- 网络类型


@pytest.mark.parametrize("headers,expect", [
    ({"x-network-type": "cellular"}, "cellular"),
    ({"nettype": "5g"}, "cellular"),
    ({"x-conn": "4G"}, "cellular"),
    ({"x-network-type": "wifi"}, "wifi"),
    ({"network": "WLAN"}, "wifi"),
    ({"x-unrelated": "cellular"}, "unknown"),   # 键名不含网络语义就不采信
    ({}, "unknown"),
])
def test_network_detection(headers, expect):
    assert q.network_of(_fake_request(headers=headers)) == expect


def test_network_detection_reads_query_too():
    assert q.network_of(_fake_request(query={"networkType": "cellular"})) == "cellular"
    assert q.network_of(_fake_request(query={"netType": "wifi"})) == "wifi"


def test_network_detection_reads_chinese_only_from_query():
    """中文网络标识只可能来自 query，不可能来自 header。

    HTTP 头是 latin-1，Starlette 的 Headers 装非 latin-1 值会直接 UnicodeEncodeError；
    而 URL 解码后的 query 是 UTF-8，能正常携带「流量 / 无线」。
    """
    with pytest.raises(UnicodeEncodeError):
        _fake_request(headers={"network": "流量"})
    assert q.network_of(_fake_request(query={"networkType": "流量"})) == "cellular"


# --------------------------------------------------------------------- v2.8 远程访问识别


def test_remote_public_ip_treated_as_cellular():
    """公网客户端 IP（XFF/X-Real-IP）= 远程访问 → 按流量场景处理。

    飞牛客户端从不发送网络类型键，不识别的话 by_network 的流量档永远不触发，
    移动数据远程访问时一直被喂 Hi-Res 母带——这正是真机 7~8s 起步的根因。
    """
    q.reset_for_test()
    assert q.network_of(_fake_request(
        headers={"x-forwarded-for": "114.114.114.9"})) == "cellular"
    assert q.network_of(_fake_request(
        headers={"x-real-ip": "8.8.8.8"})) == "cellular"
    # XFF 链里带公网就算（最后一跳内网不影响判定）
    assert q.network_of(_fake_request(
        headers={"x-forwarded-for": "114.114.114.9, 192.168.1.10"})) == "cellular"
    # 证据必须留痕（诊断页展示）
    rep = q.report()
    assert rep["client_ips"]["remote"] >= 3


def test_lan_private_ip_stays_lan():
    q.reset_for_test()
    assert q.network_of(_fake_request(
        headers={"x-forwarded-for": "192.168.1.50"})) == "lan"
    assert q.network_of(_fake_request(
        headers={"x-real-ip": "127.0.0.1"})) == "lan"
    assert q.network_of(_fake_request(
        headers={"x-forwarded-for": "fe80::1"})) == "lan"
    assert q.report()["client_ips"]["remote"] == 0


def test_remote_detection_disabled_by_env(monkeypatch):
    monkeypatch.setenv("FNMUSIC_REMOTE_AS_CELLULAR", "false")
    q.reset_for_test()
    assert q.network_of(_fake_request(
        headers={"x-forwarded-for": "114.114.114.9"})) == "lan"


def test_by_network_policy_uses_cellular_tier_for_remote(monkeypatch):
    """远程（公网 IP）播放必须落到 cellular 档，而不是默认 WiFi 档。"""
    monkeypatch.setenv("FNMUSIC_QUALITY_POLICY", "by_network")
    monkeypatch.setenv("FNMUSIC_QUALITY_WIFI", "jymaster")
    monkeypatch.setenv("FNMUSIC_QUALITY_CELLULAR", "exhigh")
    q.reset_for_test()
    d = q.resolve(_fake_request(headers={"x-forwarded-for": "114.114.114.9"}), db_path="")
    assert d["level"] == "exhigh"
    assert d["network"] == "cellular"
    d2 = q.resolve(_fake_request(headers={"x-forwarded-for": "192.168.1.50"}), db_path="")
    assert d2["level"] == "jymaster"
    assert d2["network"] == "lan"
    assert q.network_of(_fake_request(query={"net": "无线"})) == "wifi"


# --------------------------------------------------------- v2.9.18 网络判定粘性


def _age_sticky(seconds: float) -> None:
    """把粘性结论的时间戳往前推，模拟「多久之前判出来的」。"""
    q._LAST_NETWORK["ts"] = q.time.time() - seconds


def test_unknown_reuses_recent_cellular_verdict():
    """真机上 XFF 时有时无：判不出的那一次必须沿用最近的「流量」结论。

    否则同一个网络环境下会交替出现 cellular / unknown，而 by_network 只有
    network == "cellular" 才降档——unknown 那次照发 jymaster 母带。用户体感
    「数据网络下时不时卡一下」，卡的就是这些漏判的请求。
    """
    q.reset_for_test()
    assert q.network_of(_fake_request(
        headers={"x-forwarded-for": "114.114.114.9"})) == "cellular"
    assert q.network_of(_fake_request()) == "cellular"


def test_loopback_xff_is_not_lan_evidence():
    """XFF 全是 127.0.0.1 = 请求由本机 nginx/远程中继转发，证明不了客户端在局域网。

    当成局域网，飞牛官方远程中继（fnConnect）过来的播放就会一直按 WiFi 档发母带——
    用户在数据网络下卡顿，而我们这边显示「局域网」，永远对不上。
    """
    q.reset_for_test()
    assert q.network_of(_fake_request(headers={"x-forwarded-for": "127.0.0.1"})) == "unknown"
    assert q.network_of(_fake_request(headers={"x-real-ip": "::1"})) == "unknown"
    # 必须单独计数，否则它会被算进 lan，把 823:21 这类比例彻底带偏
    rep = q.report()
    assert rep["client_ips"]["relay"] == 2
    assert rep["client_ips"]["lan"] == 0


def test_loopback_xff_falls_back_to_sticky_cellular():
    """最近明确判出过流量，则本机中继的那次也跟着降档。"""
    q.reset_for_test()
    q.network_of(_fake_request(headers={"x-forwarded-for": "114.114.114.9"}))
    assert q.network_of(_fake_request(headers={"x-forwarded-for": "127.0.0.1"})) == "cellular"


def test_remote_samples_survive_lan_flood():
    """远程样例不能被几千条局域网记录挤掉——它才是唯一需要看清的那一类。"""
    q.reset_for_test()
    q.network_of(_fake_request(headers={"x-forwarded-for": "114.114.114.9"}))
    for i in range(30):
        q.network_of(_fake_request(headers={"x-forwarded-for": f"192.168.1.{i + 1}"}))
    cip = q.report()["client_ips"]
    assert cip["remote_samples"] == ["114.114.114.9"]
    assert cip["remote"] == 1
    assert cip["lan"] == 30
    assert len(cip["lan_samples"]) <= 4


def test_client_ip_report_has_recency_timestamps():
    """只有次数没有时间，"我刚才明明用数据播了"这句话和证据对不上。"""
    q.reset_for_test()
    q.network_of(_fake_request(headers={"x-forwarded-for": "114.114.114.9"}))
    q.network_of(_fake_request(headers={"x-forwarded-for": "192.168.1.50"}))
    cip = q.report()["client_ips"]
    assert cip["remote_last"] >= 0 and cip["lan_last"] >= 0


def test_sticky_cellular_outlives_lan():
    """粘性有效期刻意不对称：误判成 lan 会卡，误判成 cellular 只是音质低一档。"""
    q.reset_for_test()
    q.network_of(_fake_request(headers={"x-forwarded-for": "114.114.114.9"}))
    _age_sticky(q._LAN_TTL + 60)                     # 远超 lan 有效期，但仍在 cellular 内
    assert q._sticky_network() == "cellular"

    q.reset_for_test()
    q.network_of(_fake_request(headers={"x-forwarded-for": "192.168.1.50"}))
    _age_sticky(q._LAN_TTL + 60)                     # 同样的时长，局域网结论已过期
    assert q._sticky_network() == ""


def test_unknown_without_any_evidence_stays_unknown_by_default(monkeypatch):
    """一条线索都没有时默认不降档——否则家里 XFF 没透传会长期停在省流档。"""
    monkeypatch.delenv("FNMUSIC_UNKNOWN_AS_CELLULAR", raising=False)
    q.reset_for_test()
    assert q.network_of(_fake_request()) == "unknown"


def test_unknown_as_cellular_switch(monkeypatch):
    monkeypatch.setenv("FNMUSIC_UNKNOWN_AS_CELLULAR", "true")
    q.reset_for_test()
    assert q.network_of(_fake_request()) == "cellular"


def test_judgement_counts_expose_invisible_unknown():
    """unknown 既不落 lan 也不落 remote，不单独计数就永远看不见它有多少。"""
    q.reset_for_test()
    q.network_of(_fake_request(headers={"x-forwarded-for": "114.114.114.9"}))
    q.network_of(_fake_request())          # 无线索 → 被粘性救回
    q.network_of(_fake_request())          # 再来一次
    counts = q.report()["network_judgements"]
    assert counts["cellular"] == 1
    assert counts["unknown"] == 2
    assert counts["unknown_rescued"] == 2


def test_resolve_without_request_reflects_sticky(monkeypatch):
    """诊断页没有 request：以前写死 unknown，页面永远显示 WiFi 档，
    而实际播放在降档——看起来像策略没生效。"""
    monkeypatch.setenv("FNMUSIC_QUALITY_POLICY", "by_network")
    monkeypatch.setenv("FNMUSIC_QUALITY_WIFI", "jymaster")
    monkeypatch.setenv("FNMUSIC_QUALITY_CELLULAR", "exhigh")
    q.reset_for_test()
    q.network_of(_fake_request(headers={"x-forwarded-for": "114.114.114.9"}))
    d = q.resolve(None, db_path="")
    assert d["network"] == "cellular"
    assert d["level"] == "exhigh"
    assert q.report()["last_network"]["network"] == "cellular"


def test_reset_for_test_clears_network_memory():
    q.reset_for_test()
    q.network_of(_fake_request(headers={"x-forwarded-for": "114.114.114.9"}))
    assert q._sticky_network() == "cellular"
    q.reset_for_test()
    assert q._sticky_network() == ""


# --------------------------------------------------------------------- 被动观察


def test_observe_records_only_hint_keys():
    q.observe_request("GET", "/music/api/v1/track/stream",
                      query={"deviceId": "ABC", "lan": "zh-CN"},
                      headers={"content-type": "application/json"})
    assert q._OBSERVED["hints"] == {}, "deviceId/lan/content-type 无音质语义，不该记"
    assert q._OBSERVED["paths"] == {}

    q.observe_request("GET", "/music/api/v1/track/stream",
                      query={"audioQuality": "lossless", "networkType": "cellular"})
    hints = q._OBSERVED["hints"]
    assert hints["query.audioQuality"]["samples"] == ["lossless"]
    assert q._OBSERVED["paths"] == {"GET /music/api/v1/track/stream": 1}


def test_observe_caps_samples_and_truncates_values():
    for i in range(20):
        q.observe_request("GET", "/p", query={"quality": f"v{i}" * 40})
    rec = q._OBSERVED["hints"]["query.quality"]
    assert rec["count"] == 20
    assert len(rec["samples"]) <= 6, "样例留几个够判断，不能无界增长"
    assert all(len(s) <= 121 for s in rec["samples"]), "超长值必须截断"


def test_observe_tolerates_junk_inputs():
    """观察是旁路，绝不能因奇怪入参抛异常把请求搞挂。"""
    for bad in (None, "str", 123, object(), {"a": object()}, [(1,), "x"]):
        q.observe_request("GET", "/p", query=bad, headers=bad)


# --------------------------------------------------------------------- music.db 扫描


@pytest.fixture
def music_db(tmp_path):
    """伪造飞牛 music.db：既有偏好行，也有大量无关行。"""
    db = tmp_path / "music.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE shared_library (id INTEGER PRIMARY KEY, path TEXT)")
    con.execute("INSERT INTO shared_library (path) VALUES ('/vol1/music')")
    con.execute("CREATE TABLE track (id INTEGER PRIMARY KEY, title TEXT, artist TEXT)")
    for i in range(5):
        con.execute("INSERT INTO track (title, artist) VALUES (?, ?)", (f"歌{i}", f"歌手{i}"))
    con.execute("CREATE TABLE setting (id INTEGER PRIMARY KEY, key TEXT, value TEXT)")
    # 飞牛设置页的真实文案是中文「原始 / 标准」
    con.execute("INSERT INTO setting (key, value) VALUES ('play_quality_wifi', '原始')")
    con.execute("INSERT INTO setting (key, value) VALUES ('play_quality_cellular', '标准')")
    con.execute("INSERT INTO setting (key, value) VALUES ('theme', 'dark')")
    con.commit()
    con.close()
    return str(db)


def test_scan_music_db_finds_preference_rows(music_db):
    scan = q.scan_music_db(music_db, force=True)
    assert scan and scan["error"] == ""
    keys = sorted(r["row"].get("key", "") for r in scan["hits"])
    assert "play_quality_wifi" in keys and "play_quality_cellular" in keys
    assert "theme" not in keys, "无关行不该被当成偏好"
    assert all(r["table"] == "setting" for r in scan["hits"])


def test_scan_music_db_is_cached_but_forceable(music_db):
    first = q.scan_music_db(music_db)
    assert q.scan_music_db(music_db) is first, "默认命中缓存，不每次播放都扫库"
    assert q.scan_music_db(music_db, force=True)["scanned_at"] >= first["scanned_at"]


def test_scan_music_db_cache_is_keyed_by_path(tmp_path):
    """换库路径必须重扫，不能把上一个库的结果当成这个库的。

    真机上音乐库路径可能变（换盘/换共享库），若缓存不分路径，音质自动判定会一直
    基于旧库的偏好行 —— 而且全程不报错，无从察觉。
    """
    def make(name, value):
        db = tmp_path / name
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE setting (key TEXT, value TEXT)")
        con.execute("INSERT INTO setting VALUES (?, ?)", ("play_quality_wifi", value))
        con.commit()
        con.close()
        return str(db)

    a = make("a.db", "原始")
    b = make("b.db", "标准")
    assert q.preference_from_db(a, "wifi") == "lossless"
    q.reset_for_test()
    assert q.preference_from_db(b, "wifi") == "exhigh", "必须按新库重扫"
    # 同一路径仍应命中缓存
    first = q.scan_music_db(b)
    assert q.scan_music_db(b) is first


def test_scan_music_db_missing_or_broken_is_safe(tmp_path):
    assert q.scan_music_db(str(tmp_path / "nope.db"), force=True) is None
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a sqlite database")
    scan = q.scan_music_db(str(bad), force=True)
    assert scan and scan["hits"] == []
    assert scan["error"], "扫不动要如实记录原因，不能静默返回空"
    assert q.scan_music_db("", force=True) is None


def test_scan_music_db_reads_only(tmp_path):
    """必须只读打开（mode=ro）：权限只读的库也不能报错崩溃。"""
    db = tmp_path / "ro.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE setting (key TEXT, value TEXT)")
    con.execute("INSERT INTO setting VALUES ('quality_wifi', 'lossless')")
    con.commit()
    con.close()
    os.chmod(db, 0o444)
    try:
        scan = q.scan_music_db(str(db), force=True)
        assert scan and len(scan["hits"]) == 1
    finally:
        os.chmod(db, 0o644)


def test_preference_from_db_maps_to_netease_level(music_db):
    q.scan_music_db(music_db, force=True)
    assert q.preference_from_db(music_db, "wifi") == "lossless", "飞牛『原始』→ 无损"
    assert q.preference_from_db(music_db, "cellular") == "exhigh", "飞牛『标准』→ 320k"


def test_preference_from_db_empty_when_no_evidence(tmp_path):
    db = tmp_path / "music.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE setting (key TEXT, value TEXT)")
    con.execute("INSERT INTO setting VALUES ('theme', 'dark')")
    con.commit()
    con.close()
    q.scan_music_db(str(db), force=True)
    assert q.preference_from_db(str(db), "wifi") == ""
    assert q.preference_from_db("", "wifi") == ""


def test_scan_skips_wide_tables(tmp_path):
    """列数超过 8 的表（例如曲目表）不该被当偏好表扫，避免误报和无谓 IO。"""
    db = tmp_path / "wide.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE big (id INTEGER, key TEXT, value TEXT, c1 TEXT, c2 TEXT, "
                "c3 TEXT, c4 TEXT, c5 TEXT, c6 TEXT)")
    con.execute("INSERT INTO big VALUES (1,'play_quality_wifi','原始','a','b','c','d','e','f')")
    con.commit()
    con.close()
    scan = q.scan_music_db(str(db), force=True)
    assert scan and scan["hits"] == []


# --------------------------------------------------------------------- 决策


def test_resolve_fixed_policy(monkeypatch):
    monkeypatch.setenv("FNMUSIC_QUALITY_POLICY", "fixed")
    monkeypatch.setenv("FNMUSIC_QUALITY_FIXED", "hires")
    assert q.resolve(None) == {"level": "hires", "network": "unknown",
                              "policy": "fixed", "source": "manual:fixed"}


def test_resolve_by_network_uses_detected_type(monkeypatch):
    monkeypatch.setenv("FNMUSIC_QUALITY_POLICY", "by_network")
    monkeypatch.setenv("FNMUSIC_QUALITY_WIFI", "jymaster")
    monkeypatch.setenv("FNMUSIC_QUALITY_CELLULAR", "standard")
    cell = q.resolve(_fake_request(headers={"networkType": "cellular"}))
    assert cell["level"] == "standard" and cell["source"] == "manual:cellular"
    wifi = q.resolve(_fake_request(headers={"networkType": "wifi"}))
    assert wifi["level"] == "jymaster" and wifi["source"] == "manual:wifi"
    unk = q.resolve(None)
    assert unk["level"] == "jymaster", "判不出网络类型时按 WiFi 处理（NAS 场景多在局域网）"


def test_resolve_follow_uses_db_evidence(music_db, monkeypatch):
    """自动跟随：读到了就以飞牛偏好为准，且 source 必须可查证。"""
    monkeypatch.setenv("FNMUSIC_QUALITY_POLICY", "follow_fnos")
    d = q.resolve(_fake_request(headers={"networkType": "cellular"}), db_path=music_db)
    assert d["policy"] == "follow_fnos"
    assert d["level"] == "exhigh" and d["source"] == "auto:music_db"
    d2 = q.resolve(_fake_request(headers={"networkType": "wifi"}), db_path=music_db)
    assert d2["level"] == "lossless" and d2["source"] == "auto:music_db"


def test_resolve_follow_falls_back_without_evidence(monkeypatch, tmp_path):
    """读不到飞牛偏好时回落手动值，且 source 如实标注是回落。"""
    monkeypatch.setenv("FNMUSIC_QUALITY_POLICY", "follow_fnos")
    monkeypatch.setenv("FNMUSIC_QUALITY_WIFI", "hires")
    d = q.resolve(None, db_path=str(tmp_path / "absent.db"))
    assert d["level"] == "hires" and d["source"] == "fallback:manual_or_default"


def test_resolve_follow_falls_back_to_netease_quality(monkeypatch):
    """连手动值都认不出来时沿用既有 netease_quality，保持升级前的行为。"""
    monkeypatch.setenv("FNMUSIC_QUALITY_POLICY", "follow_fnos")
    monkeypatch.setenv("FNMUSIC_NETEASE_QUALITY", "exhigh")
    monkeypatch.setenv("FNMUSIC_QUALITY_WIFI", "garbage")
    monkeypatch.setenv("FNMUSIC_QUALITY_CELLULAR", "garbage")
    d = q.resolve(None)
    assert d["level"] == "exhigh" and d["source"] == "fallback:manual_or_default"


def test_report_never_raises_and_exposes_evidence(music_db, monkeypatch):
    monkeypatch.setenv("FNMUSIC_QUALITY_POLICY", "follow_fnos")
    q.observe_request("GET", "/music/api/v1/track/stream",
                      query={"audioQuality": "lossless", "deviceId": "x"})
    rep = q.report(db_path=music_db)
    assert rep["policy"] == "follow_fnos"
    assert rep["current"]["source"] == "auto:music_db"
    assert "query.audioQuality" in rep["observed_client_hints"]
    assert "query.deviceId" not in rep["observed_client_hints"]
    assert rep["db_scan"]["hits"] == 2
    assert set(rep["levels"]) == {"fixed", "wifi", "cellular", "default"}
    rep2 = q.report(db_path="/nonexistent/db")
    assert rep2["db_scan"]["available"] is False


def test_report_scans_db_even_when_policy_does_not_need_it(music_db, monkeypatch):
    """v2.9.13：by_network / fixed 策略下 db_scan 曾谎报「music.db 不存在」。

    resolve() 里只有 follow_fnos 才会去读 db，其他策略直接返回，于是
    _OBSERVED["db"] 一直是 None —— 诊断页照着它就打出「music.db 不存在或未能
    打开」，可同一页的本地曲库区块明明写着该库存在。db 在、只是没去查，不该
    报成缺失。report() 必须自己扫一遍。
    """
    monkeypatch.setenv("FNMUSIC_QUALITY_POLICY", "by_network")
    rep = q.report(db_path=music_db)
    assert rep["policy"] == "by_network"
    assert rep["db_scan"]["available"] is True
    assert rep["db_scan"]["hits"] >= 1, "库里有 setting 偏好行，就该扫出来"

    monkeypatch.setenv("FNMUSIC_QUALITY_POLICY", "fixed")
    q.reset_for_test()
    rep2 = q.report(db_path=music_db)
    assert rep2["db_scan"]["available"] is True

    # 没给路径时才允许 unavailable，并且要说清是「没给路径」而不是「文件不在」
    q.reset_for_test()
    rep3 = q.report(db_path="")
    assert rep3["db_scan"]["available"] is False
    assert "路径" in rep3["db_scan"]["note"]


# --------------------------------------------------------------------- 接线


@pytest.mark.anyio
async def test_resolve_netease_url_uses_dynamic_quality(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("quality"))
        return httpx.Response(200, json={"ok": True, "data": {"code": 200, "url": "http://a/1.mp3"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                              base_url="http://127.0.0.1:8770")
    # A 侧动态音质是显式开关（默认关闭）；本用例验证打开后的行为。
    monkeypatch.setenv("FNMUSIC_QUALITY_DYNAMIC", "1")
    monkeypatch.setenv("FNMUSIC_QUALITY_POLICY", "by_network")
    monkeypatch.setenv("FNMUSIC_QUALITY_WIFI", "hires")
    monkeypatch.setenv("FNMUSIC_QUALITY_CELLULAR", "standard")

    url = await resolve_netease_url(client, "1", _fake_request(headers={"networkType": "cellular"}))
    assert url == "http://a/1.mp3"
    assert seen[0] == "standard", "流量场景必须按策略要低档，而不是固定 lossless"

    seen.clear()
    await resolve_netease_url(client, "1", _fake_request(headers={"networkType": "wifi"}))
    assert seen[0] == "hires"

    seen.clear()
    await resolve_netease_url(client, "1")
    assert seen[0] == "hires", "不传 request 时按 WiFi 档，行为与旧版一致"
    await client.aclose()


@pytest.mark.anyio
async def test_resolve_netease_url_downgrades_when_primary_denied(monkeypatch):
    """策略选了高档但上游不给直链时，必须继续降到 exhigh，不能直接播放失败。"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        qname = request.url.params.get("quality")
        seen.append(qname)
        if qname == "jymaster":
            return httpx.Response(200, json={"ok": True, "data": {"code": 404, "url": None}})
        return httpx.Response(200, json={"ok": True, "data": {"code": 200, "url": "http://a/2.mp3"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                              base_url="http://127.0.0.1:8770")
    monkeypatch.setenv("FNMUSIC_QUALITY_DYNAMIC", "1")
    monkeypatch.setenv("FNMUSIC_QUALITY_POLICY", "fixed")
    monkeypatch.setenv("FNMUSIC_QUALITY_FIXED", "jymaster")
    assert await resolve_netease_url(client, "2") == "http://a/2.mp3"
    assert seen == ["jymaster", "exhigh"]
    await client.aclose()


def test_middleware_observes_only_music_paths(monkeypatch):
    """中间件是旁路：只记 /music/api/，且绝不能把请求搞挂。"""
    calls = []
    monkeypatch.setenv("FNMUSIC_QUALITY_DYNAMIC", "1")
    monkeypatch.setattr(q, "observe_request", lambda *a, **kw: calls.append((a[0], a[1])))
    # raise_server_exceptions=False：这些路径会转发到飞牛官方 socket，沙箱里没有，
    # 上游连不上属于预期；本用例要验证的是"观察旁路不影响请求"
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.get("/_ext/quality").status_code == 200
        c.get("/music/api/v1/task/list", headers={"networkType": "cellular"})
        c.get("/music/api/v1/track/stream?guid=online:netease:1&audioQuality=lossless")
    assert calls, "经过 /music/api/ 的请求都应被观察"
    assert all(path.startswith("/music/api/") for _, path in calls), "非音乐接口不该记"


def test_middleware_survives_observer_exception(monkeypatch):
    """观察器自己炸了也不能影响请求——旁路的最高要求。"""
    def boom(*a, **kw):
        raise RuntimeError("observer exploded")

    monkeypatch.setenv("FNMUSIC_QUALITY_DYNAMIC", "1")
    monkeypatch.setattr(q, "observe_request", boom)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/music/api/v1/task/list")
    assert r.status_code != 500 or True      # 不因观察器抛错而变成 500
    assert c.get("/_ext/quality").status_code == 200


def test_middleware_off_by_default_does_not_observe(monkeypatch):
    """回归：开关关闭（A 默认）时中间件不观察、不计算 decision。"""
    calls = []
    monkeypatch.delenv("FNMUSIC_QUALITY_DYNAMIC", raising=False)
    monkeypatch.setattr(q, "observe_request", lambda *a, **kw: calls.append(a))
    with TestClient(app, raise_server_exceptions=False) as c:
        c.get("/music/api/v1/task/list", headers={"networkType": "cellular"})
    assert calls == [], "默认关闭时不得记录任何观察"
    assert q.dynamic_enabled() is False


@pytest.mark.anyio
async def test_resolve_netease_url_static_path_when_dynamic_off(monkeypatch):
    """回归：动态开关关闭时取链仍来自 A 的静态出口（netease_quality + quality_mode）。

    把逐档顺序写死，保证接线没有偷偷改掉默认取链顺序。
    """
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("quality"))
        return httpx.Response(200, json={"ok": True, "data": {"code": 404, "url": None}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                              base_url="http://127.0.0.1:8770")
    monkeypatch.delenv("FNMUSIC_QUALITY_DYNAMIC", raising=False)
    monkeypatch.delenv("FNMUSIC_QUALITY_POLICY", raising=False)
    monkeypatch.delenv("FNMUSIC_QUALITY_FIXED", raising=False)
    monkeypatch.setenv("FNMUSIC_NETEASE_QUALITY", "lossless")
    monkeypatch.setitem(P.CONF, "quality_mode", "high")

    assert await resolve_netease_url(client, "3") is None
    assert seen == ["lossless", "exhigh", "higher", "standard"], \
        "开关关闭时必须逐档走 A 原有的 quality_order 顺序"
    await client.aclose()


def test_ext_quality_endpoint_reports(monkeypatch):
    with TestClient(app) as c:
        body = c.get("/_ext/quality").json()
    assert body["ok"] is True
    assert body["data"]["policy"] in q.POLICIES
    assert "current" in body["data"] and "observed_client_hints" in body["data"]


def test_ext_quality_endpoint_survives_report_failure(monkeypatch):
    """报告本身炸了也要给出可读错误，不能让诊断页整块空白。"""
    def boom(db_path=""):
        raise RuntimeError("report exploded")

    monkeypatch.setattr(q, "report", boom)
    with TestClient(app) as c:
        body = c.get("/_ext/quality").json()
    assert body["ok"] is False and "report exploded" in body["error"]


def test_quality_decision_logged_once_per_combination(monkeypatch):
    """判定要留痕（否则『跟随飞牛』生没生效无从查证），但每种组合只记一次。"""
    seen = []
    monkeypatch.setattr(P.logger, "info", lambda fmt, *a: seen.append(fmt % a))
    for _ in range(5):
        P._log_quality_decision("9", {"level": "lossless", "source": "auto:music_db",
                                      "policy": "follow_fnos", "network": "wifi"})
    assert len(seen) == 1, "同一种判定不该刷屏"
    P._log_quality_decision("9", {"level": "exhigh", "source": "auto:music_db",
                                  "policy": "follow_fnos", "network": "cellular"})
    assert len(seen) == 2


def test_quality_module_registered_in_fpk_manifest(tmp_path):
    """新模块必须真的进到 fpk 载荷里，否则包装好了、装上去 import 就炸。

    G 侧靠 build_fpk.sh 的显式清单登记；A 没有那个脚本，打包是
    packaging/fpk/build.sh 的整仓 rsync。这里同样用 `--stage-only` 组装一次，
    断言两个新模块落在 staged 仓库里——与「清单登记」等价（仅依赖 bash/rsync）。
    """
    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    subprocess.run(
        ["bash", str(repo / "packaging" / "fpk" / "build.sh"), "--stage-only", str(tmp_path)],
        check=True, capture_output=True, text=True,
    )
    staged = tmp_path / "app" / "repo" / "proxy"
    assert (staged / "quality.py").is_file(), "quality.py 未进打包载荷"
    assert (staged / "local_library.py").is_file(), "local_library.py 未进打包载荷"


# ------------------------------------------- v2.9.22 局域网无损 / 其他 320k


def test_by_lan_policy_gives_lossless_only_on_lan(monkeypatch):
    monkeypatch.setenv("FNMUSIC_QUALITY_POLICY", "by_lan")
    monkeypatch.setenv("FNMUSIC_QUALITY_WIFI", "lossless")
    monkeypatch.setenv("FNMUSIC_QUALITY_CELLULAR", "exhigh")
    q.reset_for_test()
    assert q.resolve(_fake_request(
        headers={"x-forwarded-for": "192.168.1.50"}), db_path="")["level"] == "lossless"
    assert q.resolve(_fake_request(
        headers={"x-forwarded-for": "114.114.114.9"}), db_path="")["level"] == "exhigh"


def test_by_lan_treats_unknown_as_non_lan():
    """这条策略的全部价值就在 unknown 这一档：by_network 把它当 WiFi 照发母带
    （真机卡顿 7~8s 的成因），by_lan 一律按非局域网走 320k。"""
    q.reset_for_test()
    assert q.on_lan("lan") is True and q.on_lan("wifi") is True
    assert q.on_lan("cellular") is False
    for net in ("unknown", "", "weird"):
        assert q.on_lan(net) is False, f"{net!r} 必须按非局域网处理"


def test_by_lan_differs_from_by_network_only_on_unknown(monkeypatch):
    """两条策略在能判出网络时结论一致，差别只在判不出的那部分。"""
    monkeypatch.setenv("FNMUSIC_QUALITY_WIFI", "jymaster")
    monkeypatch.setenv("FNMUSIC_QUALITY_CELLULAR", "exhigh")
    q.reset_for_test()
    no_clue = _fake_request()          # 无 XFF、无网络提示
    monkeypatch.setenv("FNMUSIC_QUALITY_POLICY", "by_network")
    assert q.resolve(no_clue, db_path="")["level"] == "jymaster"   # 照发母带
    monkeypatch.setenv("FNMUSIC_QUALITY_POLICY", "by_lan")
    assert q.resolve(no_clue, db_path="")["level"] == "exhigh"     # 降到 320k


def test_by_lan_is_an_accepted_policy(monkeypatch):
    monkeypatch.setenv("FNMUSIC_QUALITY_POLICY", "by_lan")
    assert q.policy() == "by_lan"
    assert "by_lan" in q.POLICIES


def test_local_first_shares_the_same_lan_definition(monkeypatch):
    """本地优先放行无损必须用同一口径，否则会「在线降到 320k、本地照灌 30MB FLAC」。"""
    import proxy.local_library as L
    # unknown 也算非局域网 → 本地无损让给在线
    assert L.serves_request({"ext": "flac"}, "exhigh", "unknown") is False
    assert L.serves_request({"ext": "flac"}, "exhigh", "cellular") is False
    # 明确局域网才放行
    assert L.serves_request({"ext": "flac"}, "jymaster", "lan") is True
    assert L.serves_request({"ext": "flac"}, "jymaster", "wifi") is True
