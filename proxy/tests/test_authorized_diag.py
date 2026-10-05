"""W13：飞牛授权目录 + 本地曲库优先诊断（移植 gzywd v2.9.4 / v2.9.14）。

覆盖三件事：
1. ``detect_library_dir()`` 的优先级：显式配置 → 飞牛授权目录 → music.db → cache 兜底；
2. ``/_ext/authorized`` 与 ``/_ext/localfirst`` 只读诊断端点始终返回 200 + ok 字段；
3. 掉线提醒（PushPlus）确实被接进 lifespan / healthz —— 用源码断言钉住，
   避免以后被"顺手删掉"而没有任何测试报警。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from proxy import app as P
from proxy import trimgw
from proxy.app import CONF, app, detect_library_dir


@pytest.fixture(autouse=True)
def _no_ambient_authorization(monkeypatch):
    """本机 fnOS 真的导出了 TRIM_DATA_* 且真有开放网关 socket，必须隔离掉。"""
    monkeypatch.delenv(trimgw.ACCESSIBLE_PATHS_ENV, raising=False)
    monkeypatch.delenv(trimgw.SHARE_PATHS_ENV, raising=False)
    monkeypatch.delenv(trimgw.TOKEN_ENV, raising=False)
    monkeypatch.delenv("FNMUSIC_STRICT_AUTHORIZATION", raising=False)
    trimgw.invalidate_cache()
    monkeypatch.delenv("TRIM_UID", raising=False)
    yield
    trimgw.invalidate_cache()


def _report(paths):
    def fake(force: bool = False) -> dict:
        return {"shared_paths": list(paths), "hint": "", "shared_error": "", "token_present": True,
                "gateway_present": False, "source": "env", "degraded": False, "force": force}
    return fake


def test_explicit_library_dir_wins(tmp_path, monkeypatch):
    lib = tmp_path / "explicit"
    lib.mkdir()
    monkeypatch.setitem(CONF, "library_dir", str(lib))
    monkeypatch.setattr(P.trimgw, "authorized_report", _report([str(tmp_path)]))
    assert detect_library_dir() == str(lib)


def test_authorized_dir_used_when_nothing_else(tmp_path, monkeypatch):
    auth = tmp_path / "music"
    auth.mkdir()
    monkeypatch.setitem(CONF, "library_dir", "")
    monkeypatch.setattr(P, "_db_library_dirs", lambda: [])
    monkeypatch.setattr(P.trimgw, "authorized_report", _report([str(auth)]))
    assert detect_library_dir() == str(auth)


def test_authorized_dir_picks_the_authorized_candidate(tmp_path, monkeypatch):
    auth = tmp_path / "music"
    lib = auth / "lossless"
    lib.mkdir(parents=True)
    monkeypatch.setitem(CONF, "library_dir", "")
    monkeypatch.setattr(P, "_db_library_dirs", lambda: [str(lib)])
    monkeypatch.setattr(P.trimgw, "authorized_report", _report([str(auth)]))
    assert detect_library_dir() == str(lib)


def test_falls_back_to_cache_dir_without_authorization(tmp_path, monkeypatch):
    monkeypatch.setitem(CONF, "library_dir", "")
    monkeypatch.setattr(P, "_db_library_dirs", lambda: [])
    monkeypatch.setattr(P.trimgw, "authorized_report", _report([]))
    assert detect_library_dir() == CONF["cache_dir"]


def test_strict_authorization_reads_env(monkeypatch):
    assert P._strict_authorization() is False
    monkeypatch.setenv("FNMUSIC_STRICT_AUTHORIZATION", "true")
    assert P._strict_authorization() is True


def test_library_authorization_state_covers_subdirs(tmp_path, monkeypatch):
    auth = tmp_path / "music"
    sub = auth / "flac"
    sub.mkdir(parents=True)
    monkeypatch.setattr(P.trimgw, "authorized_report", _report([str(auth)]))
    assert P.library_authorization_state(str(sub))["authorized"] is True
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert P.library_authorization_state(str(other))["authorized"] is False


def test_ext_authorized_endpoint(tmp_path, monkeypatch):
    auth = tmp_path / "music"
    auth.mkdir()
    monkeypatch.setitem(CONF, "library_dir", str(auth))
    monkeypatch.setattr(P.trimgw, "authorized_report", _report([str(auth)]))
    with TestClient(app) as client:
        resp = client.get("/_ext/authorized")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    data = payload["data"]
    assert data["library_dir"] == str(auth)
    assert data["library_is_cache_fallback"] is False
    assert data["strict"] is False
    assert data["env_share_paths"] == []
    assert data["shared_paths"] == [str(auth)]


def test_ext_authorized_endpoint_never_500s_on_gateway_failure(monkeypatch):
    def boom(force: bool = False):
        raise RuntimeError("gateway exploded")

    monkeypatch.setattr(P.trimgw, "authorized_report", boom)
    with TestClient(app) as client:
        resp = client.get("/_ext/authorized")
    # 查授权失败：端点仍然 200，只是如实回 ok=false + error（同 G 的行为），
    # 绝不把诊断页打成 500、也不影响播放主链路。
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is False
    assert "RuntimeError" in payload["error"]


def test_ext_localfirst_endpoint():
    with TestClient(app) as client:
        resp = client.get("/_ext/localfirst")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    data = payload["data"]
    assert "enabled" in data and "entries" in data and "empty_reason" in data
    assert isinstance(data["hint"], str)


def test_pushplus_alerts_are_wired_into_lifespan_and_healthz():
    """源码级断言：掉线提醒（PushPlus）必须挂在 lifespan 上并被 healthz 暴露。"""
    src = Path(P.__file__).read_text(encoding="utf-8")
    assert "netease_auth.start_watch(" in src
    assert "netease_auth.stop_watch()" in src
    assert '"pushplus": "enabled" if pushplus.enabled() else "disabled",' in src
    assert os.path.isfile(Path(P.__file__).with_name("pushplus.py"))
