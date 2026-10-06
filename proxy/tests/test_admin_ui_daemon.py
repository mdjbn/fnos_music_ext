"""管理界面独立进程（proxy/admin_ui_daemon.py）与它的网关子路径接线。

A 侧的进程模型：fnmusic-ext.service 主进程之外，用 ExecStartPost 拉起
webui_gateway.py（桌面网关 socket）和 admin_ui_daemon.py（管理界面）。
这里只验证接线与默认值，不真的起 uvicorn。
"""
from __future__ import annotations

import os
import signal
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


# --- 真机踩过的坑：systemd 用 /usr/bin/python3 执行本脚本，而它没有 fastapi/uvicorn ---

def test_candidate_pythons_puts_current_first_then_venv(monkeypatch, tmp_path):
    monkeypatch.delenv("FNMUSIC_ADMIN_UI_PYTHON", raising=False)
    cands = admin_ui_daemon.candidate_pythons(tmp_path)
    assert cands[0] == sys.executable
    assert cands[-1] == str(tmp_path / ".venv-proxy" / "bin" / "python")

    monkeypatch.setenv("FNMUSIC_ADMIN_UI_PYTHON", "/opt/py/bin/python")
    cands = admin_ui_daemon.candidate_pythons(tmp_path)
    assert cands[:3] == [sys.executable, "/opt/py/bin/python",
                         str(tmp_path / ".venv-proxy" / "bin" / "python")]


def test_resolve_interpreter_picks_first_working(monkeypatch, tmp_path):
    monkeypatch.delenv("FNMUSIC_ADMIN_UI_PYTHON", raising=False)
    venv = str(tmp_path / ".venv-proxy" / "bin" / "python")
    tried: list[str] = []

    def fake_ok(python):
        tried.append(python)
        return python == venv

    monkeypatch.setattr(admin_ui_daemon, "interpreter_ok", fake_ok)
    python, seen = admin_ui_daemon.resolve_interpreter(tmp_path)
    assert python == venv
    assert seen == [sys.executable, venv]      # 当前解释器先试，失败才轮到 venv
    assert tried == [sys.executable, venv]


def test_resolve_interpreter_returns_none_when_nothing_works(monkeypatch, tmp_path):
    monkeypatch.setattr(admin_ui_daemon, "interpreter_ok", lambda python: False)
    python, seen = admin_ui_daemon.resolve_interpreter(tmp_path)
    assert python is None and seen


def test_needs_reexec_skips_current_and_guards_recursion(monkeypatch, tmp_path):
    monkeypatch.delenv(admin_ui_daemon.REEXEC_FLAG, raising=False)
    assert admin_ui_daemon.needs_reexec(sys.executable) is False
    assert admin_ui_daemon.needs_reexec("/usr/bin/python3") is True
    # venv 的 bin/python 常是指向系统解释器的符号链接：必须按路径比较，
    # 否则会被误判成「已经在用这个解释器」而跳过切换（真机上就是缺依赖死掉）
    link = tmp_path / "python"
    link.symlink_to(sys.executable)
    assert admin_ui_daemon.needs_reexec(str(link)) is True
    # 已经 execv 过一次就不再切，避免死循环
    monkeypatch.setenv(admin_ui_daemon.REEXEC_FLAG, "1")
    assert admin_ui_daemon.needs_reexec("/usr/bin/python3") is False


def test_main_reports_missing_interpreter_instead_of_dying_silently(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(admin_ui_daemon, "resolve_interpreter",
                        lambda base: (None, ["/usr/bin/python3", str(tmp_path / "nope")]))
    monkeypatch.setenv("FNMUSIC_HOME", str(tmp_path))
    monkeypatch.setenv("FNMUSIC_ADMIN_PREFIX", "/app/fnmusic-ext/admin")
    monkeypatch.setenv("FNMUSIC_ADMIN_RESTART_SCRIPT", "/bin/true")
    rc = admin_ui_daemon.main([str(tmp_path)])
    assert rc == 2                                  # 非零退出，ExecStartPost 的 stderr 进 journald
    err = capsys.readouterr().err
    assert "fastapi" in err and "uvicorn" in err
    assert "FNMUSIC_ADMIN_UI_PYTHON" in err
    assert "/usr/bin/python3" in err


def test_log_path_defaults_under_base_cache(monkeypatch, tmp_path):
    monkeypatch.delenv("FNMUSIC_ADMIN_UI_LOG", raising=False)
    assert admin_ui_daemon.log_path(tmp_path) == tmp_path / "cache" / "admin-ui.log"
    monkeypatch.setenv("FNMUSIC_ADMIN_UI_LOG", str(tmp_path / "x.log"))
    assert admin_ui_daemon.log_path(tmp_path) == tmp_path / "x.log"


def test_daemonize_redirects_output_to_log_file_not_devnull():
    """daemonize 之后失败也要留痕：fd 0/1/2 指向 <base>/cache/admin-ui.log。"""
    src = (REPO / "proxy" / "admin_ui_daemon.py").read_text(encoding="utf-8")
    daemonize = src.split("def _daemonize")[1].split("def main")[0]
    assert "O_APPEND" in daemonize and "0o600" in daemonize
    assert "os.devnull" in daemonize          # 仅作为目录不可写时的兜底
    assert "_daemonize(log_file)" in src


def test_prepare_env_exports_home_prefix_and_restart_script(monkeypatch, tmp_path):
    for key in ("FNMUSIC_HOME", "FNMUSIC_ADMIN_PREFIX", "FNMUSIC_ADMIN_RESTART_SCRIPT"):
        monkeypatch.delenv(key, raising=False)
    admin_ui_daemon.prepare_env(tmp_path)
    assert os.environ["FNMUSIC_HOME"] == str(tmp_path)
    assert os.environ["FNMUSIC_ADMIN_PREFIX"] == "/app/fnmusic-ext/admin"
    # 脚本不存在时不导出，管理页才会如实提示「未配置」
    assert "FNMUSIC_ADMIN_RESTART_SCRIPT" not in os.environ

    script = tmp_path / "proxy" / "admin_restart.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    admin_ui_daemon.prepare_env(tmp_path)
    assert os.environ["FNMUSIC_ADMIN_RESTART_SCRIPT"] == str(script)
    assert admin_ui_daemon.restart_script(tmp_path) == script


def test_admin_restart_script_is_shipped_and_restarts_the_unit():
    script = REPO / "proxy" / "admin_restart.sh"
    assert script.is_file() and os.access(script, os.X_OK)   # 必须可执行，admin_ui 用 bash 跑
    text = script.read_text(encoding="utf-8")
    # 只重启代理是不可行的（takeover 不监督子进程），只能重启整个单元
    assert "systemctl --no-block restart" in text
    assert "fnmusic-ext.service" in text
    subprocess.run(["bash", "-n", str(script)], check=True)


# --- PID 文件：既别误杀被系统回收的 PID，也别把残留 PID 留给下一次启动 ---

def test_our_process_recognises_only_our_daemon():
    # pytest 自己的 cmdline 里没有 admin_ui_daemon.py
    assert admin_ui_daemon._our_process(os.getpid()) is False
    assert admin_ui_daemon._our_process(1) is False if Path("/proc/1/cmdline").exists() else True


def test_stop_previous_drops_stale_pid_without_killing_strangers(tmp_path, monkeypatch):
    pid_path = tmp_path / "admin-ui.pid"
    pid_path.write_text("1", encoding="utf-8")          # init/systemd：绝不是我们的进程
    killed: list[int] = []
    monkeypatch.setattr(admin_ui_daemon.os, "kill", lambda pid, sig: killed.append(pid))
    admin_ui_daemon._stop_previous(pid_path)
    assert killed == []                                  # 不能向别人的 PID 发信号
    assert not pid_path.exists()                         # 残留文件要清掉


def test_stop_previous_ignores_garbage_pid_file(tmp_path):
    pid_path = tmp_path / "admin-ui.pid"
    pid_path.write_text("not-a-pid", encoding="utf-8")
    admin_ui_daemon._stop_previous(pid_path)
    assert not pid_path.exists()


def test_pid_guard_removes_pid_on_signal(tmp_path, monkeypatch):
    """uvicorn 会把 SIGTERM 重抛给自己，finally 不会执行——清理必须挂在信号处理器上。"""
    pid_path = tmp_path / "admin-ui.pid"
    pid_path.write_text("4242", encoding="utf-8")
    sent: list[tuple[int, int]] = []
    installed: dict[int, object] = {}
    monkeypatch.setattr(admin_ui_daemon.signal, "signal",
                        lambda sig, handler: installed.__setitem__(sig, handler))
    monkeypatch.setattr(admin_ui_daemon.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    admin_ui_daemon._install_pid_guard(pid_path)
    assert set(installed) == {signal.SIGTERM, signal.SIGINT}

    installed[signal.SIGTERM](signal.SIGTERM, None)
    assert not pid_path.exists()
    assert sent == [(os.getpid(), signal.SIGTERM)]        # 清完 PID 再按默认动作退出


def test_daemon_installs_pid_guard_before_serving():
    src = (REPO / "proxy" / "admin_ui_daemon.py").read_text(encoding="utf-8")
    assert src.index("_install_pid_guard(pid_path)") < src.index("uvicorn.run(")
