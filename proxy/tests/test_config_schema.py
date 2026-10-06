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


def test_every_field_default_coerces_without_bug():
    """回归：coercer 从 admin_ui 搬到 config_schema 时漏了模块级常量（CHANNEL_KEYS/_ORDER_KEYS），
    于是改「歌单大类顺序」「歌单口径」时 NameError → /api/extended 直接 HTTP 500。
    这里把每个字段的默认值都过一遍校验器：ValueError=默认值本身不合法（要修默认值），
    其它异常=代码缺陷（就是这次的问题）。
    """
    broken = []
    for field, meta in cs.FIELDS.items():
        try:
            cs.coerce(field, meta.get("default", ""))
        except ValueError as exc:
            broken.append((field, f"默认值不合法: {exc}"))
        except Exception as exc:  # noqa: BLE001
            broken.append((field, f"{type(exc).__name__}: {exc}"))
    assert not broken, broken


def test_channel_keys_are_single_source_of_truth():
    """常量只在 config_schema 定义，控制台引用同一份，避免两处漂移。"""
    from proxy import admin_ui

    assert cs.CHANNEL_KEYS == ("mine", "nrec", "toplist", "category", "newalbum", "fm")
    assert cs._ORDER_KEYS == ("daily", "localdaily") + cs.CHANNEL_KEYS
    assert admin_ui.CHANNEL_KEYS is cs.CHANNEL_KEYS or admin_ui.CHANNEL_KEYS == cs.CHANNEL_KEYS
    assert admin_ui._ORDER_KEYS == cs._ORDER_KEYS
    assert cs.coerce("netease_channel_order", "mine,daily") == "mine,daily"   # 顺序原样保留


def test_channel_aliases_are_accepted_and_canonicalized():
    """旧文案里的 hot/my/newalbums/radio 归一成 toplist/mine/newalbum/fm。"""
    assert cs.coerce("netease_channel_order", "hot,radio,daily,my") == "toplist,fm,daily,mine"
    assert cs.coerce("netease_channels", "my,hot") == "mine,toplist"
    assert cs.CHANNEL_ALIASES == {"hot": "toplist", "my": "mine",
                                  "newalbums": "newalbum", "radio": "fm"}


def test_channel_order_help_lists_real_keys():
    """回归：音源页那段说明写的是 G 时代的 key（hot/my/newalbums/radio），
    而运行时只认 mine/nrec/toplist/category/newalbum/fm ⇒ 照抄的配置会被静默丢掉。"""
    help_text = cs.FIELD_UI["netease_channel_order"]["help"]
    for key in ("daily", "localdaily", "mine", "nrec", "toplist", "category", "newalbum", "fm"):
        assert key in help_text, key
    assert "hot（热门）" not in help_text
    assert "旧名 hot/my/newalbums/radio 也认" in help_text

