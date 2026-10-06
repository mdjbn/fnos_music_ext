"""共享配置 schema 的护栏：必须与控制台字段表一一对应，且每种 kind 都能校验。

`proxy/config_schema.py` 是控制台与 A 自带音源页「扩展设置」共用的唯一校验表；
一旦它和 `admin_ui.CONFIG_FIELDS` 漂移，就会出现「一个页面能存、另一个存不进去」。
"""
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
