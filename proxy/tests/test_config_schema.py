"""共享配置 schema 的护栏：必须与控制台字段表一一对应，且每种 kind 都能校验。

`proxy/config_schema.py` 是控制台与 A 自带音源页（音乐源/播放与推荐/搜索/边听边存/通知/日志）共用的唯一校验表；
一旦它和 `admin_ui.CONFIG_FIELDS` 漂移，就会出现「一个页面能存、另一个存不进去」。
"""
import re

import pytest

from proxy import admin_ui
from proxy import config_schema as cs


def test_schema_covers_admin_fields_exactly():
    assert set(cs.FIELDS) == set(admin_ui.CONFIG_FIELDS), "config_schema 与控制台字段表漂移"
    for field, meta in cs.FIELDS.items():
        env, _coerce, secret = admin_ui.CONFIG_FIELDS[field]
        assert meta["env"] == env
        assert bool(meta.get("secret")) is bool(secret)
        assert meta["label"] and meta["group"] and meta["kind"]
        assert meta["group"] in cs.GROUP_ORDER
        assert meta["default"] == admin_ui.DEFAULTS.get(field, "")


def test_every_group_is_used():
    used = {m["group"] for m in cs.FIELDS.values()}
    assert used == set(cs.GROUP_ORDER)


@pytest.mark.parametrize("field,raw,expect", [
    ("log_quiet", "yes", "true"),
    ("log_quiet", "0", "false"),
    ("daily_limit", "42", "42"),
    ("lx_sync_writeback", "TRACKS", "tracks"),
    ("lx_sync_url", "https://host:9528/admin", "https://host:9528/admin"),
    ("lx_sync_refresh_s", "600", "600"),
])
def test_coerce_ok(field, raw, expect):
    assert cs.coerce(field, raw) == expect


@pytest.mark.parametrize("field,raw,needle", [
    ("daily_limit", "999", "1..100"),
    ("lx_sync_writeback", "nope", "off/tracks/all"),
    ("lx_sync_url", "ftp://x", "http://"),
    ("lx_sync_refresh_s", "5", "30..86400"),
])
def test_coerce_errors_are_user_readable(field, raw, needle):
    with pytest.raises(ValueError) as err:
        cs.coerce(field, raw)
    assert needle in str(err.value)


def test_coerce_unknown_field():
    with pytest.raises(ValueError):
        cs.coerce("no_such_field", "1")


def test_path_kind_shares_download_rule(tmp_path, monkeypatch):
    """download_dir 必须与 download.validate_dir 同一条规则（黑名单/必须已存在）。"""
    target = tmp_path / "archive"
    target.mkdir()
    assert cs.coerce("download_dir", str(target)) == str(target)
    with pytest.raises(ValueError):
        cs.coerce("download_dir", "/etc")
    with pytest.raises(ValueError):
        cs.coerce("download_dir", str(tmp_path / "not-there"))


# ------------------------------------------------- 界面元数据（翻译 + 分页）----

def test_every_field_has_chinese_label_help_and_page():
    for field, meta in cs.FIELDS.items():
        assert meta["page"] in cs.PAGES, f"{field} 的 page 非法"
        label = meta.get("label") or ""
        assert not re.fullmatch(r"[a-z_0-9]+", label), f"{field} 的标题还是英文键名：{label}"
        assert meta.get("help"), f"{field} 缺中文说明"


def test_choices_all_have_chinese_display_labels():
    for field, meta in cs.FIELDS.items():
        if "choices" not in meta:
            continue
        labels = meta.get("choice_labels") or {}
        missing = [c for c in meta["choices"] if c not in labels]
        assert not missing, f"{field} 的枚举缺中文显示名：{missing}"


def test_pages_cover_all_visible_fields_exactly_once():
    covered = []
    for page in cs.PAGES:
        covered += cs.fields_of_page(page)
    visible = [f for f in cs.FIELDS if f not in cs.hidden_fields()]
    assert sorted(covered) == sorted(visible), "有字段没被任何页面收走"
    assert len(covered) == len(set(covered)), "同一字段出现在多个页面"


def test_hidden_fields_are_the_known_dead_switches():
    """A 侧没实现 G 的本地每日推荐/每日推荐条数：这些开关不能展示，否则用户白调。

    它们仍留在 FIELDS 里（控制台的字段表/校验保持不变），只是不进旧音源页的界面。
    """
    assert set(cs.hidden_fields()) == {"daily_enabled", "daily_limit",
                                     "local_daily_enabled", "local_daily_limit"}
    for field in cs.hidden_fields():
        assert field in cs.FIELDS and cs.FIELDS[field].get("hidden") is True
        assert field not in cs.fields_of_page(cs.FIELDS[field]["page"])


def test_groups_of_page_are_consistent():
    for page in cs.PAGES:
        groups = cs.groups_of_page(page)
        for field in cs.fields_of_page(page):
            assert cs.FIELDS[field]["group"] in groups


def test_requested_placements():
    """用户点名的归位：洛雪→音乐源、音质/推荐/播放→播放与推荐、PushPlus→通知、
    红心同步→音乐源（网易账号歌单组）、收藏自动下载→边听边存。"""
    page_of = {f: m["page"] for f, m in cs.FIELDS.items()}
    for field in ("lx_sync_enabled", "lx_sync_url", "lx_sync_password", "lx_sync_writeback"):
        assert page_of[field] == "source"
    assert cs.FIELDS["fav_sync_like"]["group"] == "网易账号歌单"
    assert page_of["fav_sync_like"] == "source"
    for field in ("netease_quality", "quality_wifi", "quality_cellular", "daily_enabled",
                  "daily_limit", "local_daily_enabled", "local_first", "prefetch_next"):
        assert page_of[field] == "quality", field
    for field in ("pushplus_enabled", "pushplus_token", "pushplus_topic", "pushplus_template",
                  "pushplus_url"):
        assert page_of[field] == "notify", field
    assert page_of["download_on_favorite"] == "tee"
    assert page_of["download_dir"] == "tee"
    # 登录态巡检驱动的是 PushPlus 掉线提醒 ⇒ 归到通知页；日志页只剩日志三项
    assert page_of["login_check_interval_h"] == "notify"
    assert cs.PAGES["extended"] == "日志"
    assert [f for f in cs.fields_of_page("extended")] == ["log_max_mb", "log_max_days", "log_quiet"]
