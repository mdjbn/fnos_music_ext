"""proxy/trimgw.py —— 飞牛开放网关（api-scope 授权目录）单元测试。

这里的每一条都对应一类「静默失效」：查授权失败、路径没被覆盖、token 没注入，
在真机上的表现统统是「本地每日推荐歌单不出现」，所以必须钉死。
"""

from __future__ import annotations

import os

import pytest

try:  # 作为包导入（从仓库根跑 pytest）
    from proxy import trimgw
except ImportError:  # 扁平导入（从 proxy/ 目录跑）
    import trimgw  # type: ignore


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    # 真机（飞牛 NAS）的 shell 会导出 TRIM_DATA_ACCESSIBLE_PATHS / TRIM_DATA_SHARE_PATHS，
    # 还可能带 TRIM_API_TOKEN；不隔离的话断言会随着「跑测试的机器」而变，
    # 所以这里统一切干净，让每条用例只依赖自己 setenv 的值。
    trimgw.invalidate_cache()
    monkeypatch.delenv(trimgw.TOKEN_ENV, raising=False)
    monkeypatch.delenv(trimgw.SHARE_PATHS_ENV, raising=False)
    monkeypatch.delenv(trimgw.ACCESSIBLE_PATHS_ENV, raising=False)
    monkeypatch.delenv("TRIM_UID", raising=False)
    yield
    trimgw.invalidate_cache()


# ---------------------------------------------------------------------------
# 环境变量兼容（旧版 fnOS 没有 apiscope 网关）
# ---------------------------------------------------------------------------

def test_split_paths_supports_colon_and_semicolon(monkeypatch):
    monkeypatch.setenv(trimgw.SHARE_PATHS_ENV, "/vol1/1000/music;/vol2/a:/vol1/1000/music")
    assert trimgw.env_share_paths() == ["/vol1/1000/music", "/vol2/a"]


def test_split_paths_empty_when_unset():
    assert trimgw.env_share_paths() == []


# ---------------------------------------------------------------------------
# 网关不可用时的退化行为：绝不能抛异常
# ---------------------------------------------------------------------------

def test_call_without_token_returns_error_not_raise(monkeypatch):
    resp = trimgw.call("trim.file.getSharedAccessibleFolders")
    assert resp["code"] != 0
    assert trimgw.TOKEN_ENV in resp["msg"]


def test_shared_folders_uses_env_paths_as_official_authorization(monkeypatch, tmp_path):
    """v2.9.13：系统把授权结果写进 TRIM_DATA_ACCESSIBLE_PATHS —— 这是官方授权。

    真机实测该变量形如 /vol1/1000/<共享空间名>/<音乐目录名>，与管理员在「应用设置 →
    授权目录」里勾选的完全一致。既然系统给了答案就不该再去打那个稳定 500 的
    网关，更不能因为它报错就把已授权显示成「降级」。

    注意 `force=True`：`authorized_report()` 在 env 已有答案且网关可用时**按设计**
    会额外探一次网关（见 `trimgw.py:505-514` 注释，纯参考、不参与来源判定）。
    真机上 `/var/run/trim_open_gateway_apiscope.socket` 确实存在，所以这里把
    「常规路径绝不查网关」与「force 探网关失败也不翻案」分开断言，否则用例结果
    会取决于跑测试的机器上有没有那个 socket。
    """
    lib = tmp_path / "music"
    lib.mkdir()
    monkeypatch.setenv(trimgw.ACCESSIBLE_PATHS_ENV, str(lib))
    monkeypatch.setenv(trimgw.SHARE_PATHS_ENV, "")
    monkeypatch.setenv(trimgw.TOKEN_ENV, "dummy")

    def boom(*a, **kw):  # 网关要是还被调用，就说明跳过逻辑没生效
        raise AssertionError("env 已给出授权目录，常规查询不该再去查网关")

    monkeypatch.setattr(trimgw, "call", boom)
    paths, err = trimgw.shared_accessible_folders()
    assert paths == [str(lib)]
    assert err == ""                                  # 不是故障，当然没有错误
    assert trimgw.gateway_last_state()["skipped"] is True

    rep = trimgw.authorized_report()
    assert rep["source"] == "env"
    assert rep["degraded"] is False                   # ★ 官方授权 ≠ 降级
    assert rep["authorized"] is True

    # force=True 只多一次参考性探测；网关报错也不能把官方授权说成降级。
    monkeypatch.setattr(trimgw, "call",
                        lambda *a, **kw: {"code": 500, "msg": "boom", "data": None})
    rep_forced = trimgw.authorized_report(force=True)
    assert rep_forced["source"] == "env"
    assert rep_forced["degraded"] is False
    assert rep_forced["authorized"] is True
    assert rep_forced["shared_paths"] == [str(lib)]


def test_shared_folders_falls_back_to_config_when_no_env(monkeypatch, tmp_path):
    """env 没给、网关查不动时：退到 share_paths 文件，并如实带回失败原因。"""
    lib = tmp_path / "music"
    lib.mkdir()
    monkeypatch.setenv(trimgw.ACCESSIBLE_PATHS_ENV, "")
    monkeypatch.setenv(trimgw.SHARE_PATHS_ENV, "")
    monkeypatch.setattr(trimgw, "GATEWAY_SOCKET", "/nonexistent/gateway.socket")
    monkeypatch.setenv(trimgw.TOKEN_ENV, "dummy")
    monkeypatch.setattr(trimgw, "config_share_paths", lambda: [str(lib)])
    paths, err = trimgw.shared_accessible_folders(force=True)
    assert paths == [str(lib)]
    assert err  # 同时如实带回失败原因
    rep = trimgw.authorized_report(force=True)
    assert rep["source"] == "config"
    assert rep["degraded"] is True


def test_probe_shared_tries_req_aliases(monkeypatch):
    """req 名对不上网关就是 200006；别名一个个试到通为止。"""
    tried = []

    def fake_call(req, data=None, timeout=0.0):
        tried.append((req, data))
        if req == "trim.file.sharedAccess":
            return {"code": 0, "msg": "", "data": {"paths": ["/vol1/music"]}}
        return {"code": 200006, "msg": "Internal Error", "data": None}

    monkeypatch.setattr(trimgw, "call", fake_call)
    resp = trimgw.probe_shared_via_gateway()
    assert resp.get("req_used") == "trim.file.sharedAccess"
    assert len(tried) >= 2


def test_authorized_report_without_gateway_is_safe(monkeypatch):
    monkeypatch.setattr(trimgw, "GATEWAY_SOCKET", "/nonexistent/gateway.socket")
    rep = trimgw.authorized_report(force=True)
    assert rep["authorized"] is False
    assert rep["shared_paths"] == []
    assert rep["hint"]  # 必须给出可操作的指引


def test_authorized_report_hint_mentions_admin_when_err_is_admin_only(monkeypatch, tmp_path):
    """非管理员操作时，提示里要明确指出「需管理员」，而不是笼统说没授权。"""
    gw = tmp_path / "gw.socket"
    gw.write_text("")
    monkeypatch.setattr(trimgw, "GATEWAY_SOCKET", str(gw))
    monkeypatch.setenv(trimgw.TOKEN_ENV, "dummy")
    monkeypatch.setenv(trimgw.SHARE_PATHS_ENV, "")
    monkeypatch.setenv(trimgw.ACCESSIBLE_PATHS_ENV, "")

    def fake_call(req, data=None, timeout=0.0):
        return {"code": 1, "msg": "仅管理员可进行此操作", "data": {}}

    monkeypatch.setattr(trimgw, "call", fake_call)
    rep = trimgw.authorized_report(force=True)
    assert "管理员" in rep["hint"]


# ---------------------------------------------------------------------------
# 授权覆盖判定
# ---------------------------------------------------------------------------

def test_pick_library_prefers_authorized_subpath(tmp_path):
    root = tmp_path / "vol1"
    (root / "1000" / "music").mkdir(parents=True)
    cand_root = str(root / "1000" / "music")
    assert trimgw.pick_library_from_authorized([cand_root], [str(root)]) == cand_root


def test_pick_library_rejects_unauthorized_path(tmp_path):
    inside = tmp_path / "inside"
    outside = tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    assert trimgw.pick_library_from_authorized([str(outside)], [str(inside)]) == ""


def test_pick_library_rejects_prefix_sibling(tmp_path):
    """/vol1/music2 不能因为字符串前缀匹配 /vol1/music 而蒙混过关。"""
    a = tmp_path / "music"
    b = tmp_path / "music2"
    a.mkdir()
    b.mkdir()
    assert trimgw.pick_library_from_authorized([str(b)], [str(a)]) == ""


def test_pick_library_exact_match(tmp_path):
    a = tmp_path / "music"
    a.mkdir()
    assert trimgw.pick_library_from_authorized([str(a)], [str(a)]) == str(a)


def test_pick_library_skips_missing_candidates(tmp_path):
    a = tmp_path / "music"
    a.mkdir()
    missing = str(tmp_path / "gone")
    assert trimgw.pick_library_from_authorized([missing, str(a)], [str(a)]) == str(a)


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------

def test_cache_is_used_until_invalidated(monkeypatch):
    calls = []

    def fake_call(req, data=None, timeout=0.0):
        calls.append(req)
        return {"code": 0, "msg": "", "data": {"paths": ["/vol1/x"]}}

    monkeypatch.setenv(trimgw.TOKEN_ENV, "dummy")
    monkeypatch.setenv(trimgw.ACCESSIBLE_PATHS_ENV, "")
    monkeypatch.setattr(trimgw, "call", fake_call)
    trimgw.shared_accessible_folders()
    trimgw.shared_accessible_folders()
    assert len(calls) == 1
    trimgw.invalidate_cache()
    trimgw.shared_accessible_folders()
    assert len(calls) == 2


def test_existing_dirs_only(tmp_path, monkeypatch):
    """已授权但目录已被删掉的，不该出现在清单里误导人。"""
    monkeypatch.setenv(trimgw.SHARE_PATHS_ENV, f"{tmp_path}/gone:{tmp_path}")
    monkeypatch.setenv(trimgw.ACCESSIBLE_PATHS_ENV, "")
    monkeypatch.setattr(trimgw, "GATEWAY_SOCKET", "/nonexistent/gateway.socket")
    monkeypatch.setenv(trimgw.TOKEN_ENV, "dummy")
    rep = trimgw.authorized_report(force=True)
    assert rep["shared_paths"] == [str(tmp_path)]


# ---------------------------------------------------------------------------
# appName 候选与 Internal Error 排错（v2.9.9）
#
# 真机实锤：TRIM_APPNAME 常常不注入，回退硬编码时如果系统登记的应用名不是
# 这个写法，网关就按 appName 找不到授权记录，回一句笼统的
# code=200006 "Internal Error"，光看 msg 完全无从下手。
# ---------------------------------------------------------------------------


@pytest.fixture()
def _clean_app_env(monkeypatch):
    for var in ("TRIM_APPNAME", "TRIM_APP_NAME", "TRIM_APPID", "TRIM_APP_ID",
                "TRIM_PKGVAR", "TRIM_PKGMETA", "TRIM_PKGETC", "TRIM_PKGHOME"):
        monkeypatch.delenv(var, raising=False)
    yield


def test_candidate_app_names_derives_from_system_pkgvar(_clean_app_env, monkeypatch):
    monkeypatch.setenv("TRIM_PKGVAR", "/vol1/@appdata/fnnas.fnmusicext")
    names = trimgw.candidate_app_names()
    assert names[0] == "fnnas.fnmusicext", "系统注入的应用目录末段最权威，排第一"
    assert "fnmusicext" in names, "带前缀的写法也要试，同时保留裸名"


def test_candidate_app_names_falls_back_to_hardcoded(_clean_app_env):
    names = trimgw.candidate_app_names()
    assert names == ["fnmusicext"]


def test_call_retries_next_app_name_on_internal_error(_clean_app_env, monkeypatch):
    monkeypatch.setenv("TRIM_PKGVAR", "/vol1/@appdata/fnnas.fnmusicext")
    tried: list[str] = []

    def fake_post(payload, timeout=None):
        tried.append(payload["appName"])
        if payload["appName"] == "fnmusicext":
            return {"code": 0, "data": {"paths": ["/vol1/1000/music"]}}
        return {"code": 200006, "msg": "Internal Error"}

    monkeypatch.setattr(trimgw, "_http_post", fake_post)
    resp = trimgw.call("trim.file.getSharedAccessibleFolders")
    assert resp.get("code") == 0
    assert (resp.get("data") or {}).get("paths") == ["/vol1/1000/music"]
    assert len(tried) >= 2 and tried[-1] == "fnmusicext", \
        "第一个候选 Internal Error 时必须换下一个再试"


def test_call_does_not_retry_on_scope_or_token_errors(_clean_app_env, monkeypatch):
    monkeypatch.setenv("TRIM_PKGVAR", "/vol1/@appdata/fnnas.fnmusicext")
    tried: list[str] = []

    def fake_post(payload, timeout=None):
        tried.append(payload["appName"])
        return {"code": 200003, "msg": "Forbidden"}

    monkeypatch.setattr(trimgw, "_http_post", fake_post)
    resp = trimgw.call("trim.file.getSharedAccessibleFolders")
    assert resp.get("code") == 200003
    assert len(tried) == 1, "Forbidden 换 appName 也没用，别浪费时间"


def test_http_post_records_status_line_and_raw_body(_clean_app_env, monkeypatch):
    """Internal Error 光看 code/msg 无从下手，状态行和原文必须留档。"""
    monkeypatch.setenv(trimgw.TOKEN_ENV, "tok")
    monkeypatch.setattr(trimgw.os.path, "exists", lambda p: True)
    body = b'{"code":200006,"msg":"Internal Error"}'
    raw = (b"HTTP/1.1 500 Internal Server Error\r\n"
           b"Content-Type: application/json\r\n\r\n" + body)

    class _FakeSock:
        def __init__(self):
            self._buf = raw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            pass

        def connect(self, addr):
            pass

        def sendall(self, data):
            pass

        def recv(self, n):
            chunk, self._buf = self._buf[:n], self._buf[n:]
            return chunk

    class _FakeSocket:
        AF_UNIX = 1
        SOCK_STREAM = 2
        timeout = OSError

        @staticmethod
        def socket(*a, **kw):
            return _FakeSock()

    monkeypatch.setattr(trimgw, "socket", _FakeSocket)
    resp = trimgw.call("trim.file.getSharedAccessibleFolders")
    assert int(resp.get("code") or 0) == 200006
    assert "500" in str(resp.get("http_status") or ""), "状态行以前被直接丢了"
    assert "Internal Error" in trimgw.last_raw_response()


def test_trim_env_report_lists_names_but_hides_secrets(monkeypatch):
    monkeypatch.setenv(trimgw.TOKEN_ENV, "super-secret-token")
    monkeypatch.setenv("TRIM_PKGVAR", "/vol1/@appdata/fnnas.fnmusicext")
    rep = trimgw.trim_env_report()
    assert "TRIM_PKGVAR" in rep["names"], "系统注入了什么必须列出来，否则只能靠猜应用名"
    assert rep["values"]["TRIM_PKGVAR"] == "/vol1/@appdata/fnnas.fnmusicext"
    assert "super-secret-token" not in str(rep["values"]["TRIM_API_TOKEN"]), "token 绝不能露"
    assert "已注入" in str(rep["values"]["TRIM_API_TOKEN"])
