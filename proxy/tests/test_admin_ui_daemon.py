"""管理界面独立进程（proxy/admin_ui_daemon.py）与它的网关子路径接线。

A 侧的进程模型：fnmusic-ext.service 主进程之外，用 ExecStartPost 拉起
webui_gateway.py（桌面网关 socket）和 admin_ui_daemon.py（管理界面）。
这里只验证接线与默认值，不真的起 uvicorn。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from proxy import admin_ui_daemon, webui_gateway

REPO = Path(__file__).resolve().parents[2]


def test_default_paths(monkeypatch):
    for key in ("FNMUSIC_ADMIN_UI_SOCK", "FNMUSIC_ADMIN_UI_PID", "FNMUSIC_ADMIN_PREFIX"):
        monkeypatch.delenv(key, raising=False)
    assert admin_ui_daemon.ui_sock() == Path("/run/fnmusic-ext/admin-ui.sock")
    assert admin_ui_daemon.pid_file() == Path("/run/fnmusic-ext/admin-ui.pid")
    # 桌面图标挂在 /app/fnmusic-ext/，管理界面是它的 /admin 子路径
    assert admin_ui_daemon.admin_prefix() == "/app/fnmusic-ext/admin"


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("FNMUSIC_ADMIN_UI_SOCK", str(tmp_path / "a.sock"))
    monkeypatch.setenv("FNMUSIC_ADMIN_UI_PID", str(tmp_path / "a.pid"))
    monkeypatch.setenv("FNMUSIC_ADMIN_PREFIX", "/app/fnmusic-ext/admin/")
    assert admin_ui_daemon.ui_sock() == tmp_path / "a.sock"
    assert admin_ui_daemon.pid_file() == tmp_path / "a.pid"
    # 尾斜杠必须去掉，否则网关前缀匹配会错
    assert admin_ui_daemon.admin_prefix() == "/app/fnmusic-ext/admin"


def test_gateway_routes_only_admin_subpath(monkeypatch):
    monkeypatch.setattr(webui_gateway, "ADMIN_UI_PREFIX", "/app/fnmusic-ext/admin")
    yes = (
        b"GET /app/fnmusic-ext/admin/ HTTP/1.1\r\nHost: x\r\n\r\n",
        b"GET /app/fnmusic-ext/admin/api/health HTTP/1.1\r\nHost: x\r\n\r\n",
        b"POST /app/fnmusic-ext/admin/api/config HTTP/1.1\r\nHost: x\r\n\r\n",
    )
    no = (
        b"GET /app/fnmusic-ext/ HTTP/1.1\r\nHost: x\r\n\r\n",
        b"GET /app/fnmusic-ext/api/health HTTP/1.1\r\nHost: x\r\n\r\n",
        b"GET /app/fnmusic-ext/administrator HTTP/1.1\r\nHost: x\r\n\r\n",
    )
    for raw in yes:
        assert webui_gateway._is_admin_ui_request(raw) is True, raw
    for raw in no:
        assert webui_gateway._is_admin_ui_request(raw) is False, raw


def test_service_unit_starts_admin_ui_daemon():
    unit = (REPO / "fnmusic-ext.service").read_text(encoding="utf-8")
    assert 'proxy/admin_ui_daemon.py" "@BASE_DIR@"' in unit
    # 主代理先就绪、再挂网关、最后挂管理界面
    assert unit.index("takeover.py") < unit.index("webui_gateway.py") < unit.index("admin_ui_daemon.py")


def test_render_unit_includes_admin_ui_daemon():
    out = subprocess.check_output(
        [sys.executable, str(REPO / "proxy" / "takeover.py"),
         "render-unit", "--base", str(REPO)],
        text=True,
    )
    assert "proxy/admin_ui_daemon.py" in out
    assert "@BASE_DIR@" not in out


def test_daemon_never_imports_uvicorn_at_module_level():
    """ExecStartPost 里只做接线；uvicorn 在 fork 之后的子进程里才导入。"""
    src = (REPO / "proxy" / "admin_ui_daemon.py").read_text(encoding="utf-8")
    assert "\nimport uvicorn" not in src.split("def main")[0]
