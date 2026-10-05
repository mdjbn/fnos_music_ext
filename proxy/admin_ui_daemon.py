#!/usr/bin/env python3
"""把管理界面（proxy/admin_ui.py）作为独立进程常驻。

A（javycoder）侧的进程模型是「一个 systemd 单元 + 若干 ExecStartPost 守护进程」：
`fnmusic-ext.service` 起主代理后，先由 `proxy/webui_gateway.py` 挂桌面网关 socket，
再由本脚本把管理界面挂到管理用 Unix socket 上。

为什么不让主代理直接托管管理界面：
- 管理界面要能改 .env、重启服务、看日志——这些动作在同一个进程里做会互相牵制；
- 主代理承担飞牛音乐的接管 socket，任何管理面的异常都不该让它掉线。

与 webui_gateway.py 一致：双重 fork 脱离 systemd 的 ExecStartPost 等待，
写 PID 文件、启动前先停掉上一份实例、退出时清理 PID 文件。

管理界面自己会校验网关注入的 X-Trim-Isadmin 头，所以 socket 权限沿用
单元里的 UMask（0077，仅 root 可连），由同单元的网关进程转发请求。
"""
from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

DEFAULT_SOCK = "/run/fnmusic-ext/admin-ui.sock"
DEFAULT_PID = "/run/fnmusic-ext/admin-ui.pid"
# 桌面图标（网关 socket）挂在 /app/fnmusic-ext/，管理界面挂在它的 /admin 子路径下。
# admin_ui.py 用同一个值做前缀归一化，必须与网关转发的前缀一致。
DEFAULT_PREFIX = "/app/fnmusic-ext/admin"


def ui_sock() -> Path:
    return Path(os.environ.get("FNMUSIC_ADMIN_UI_SOCK") or DEFAULT_SOCK)


def pid_file() -> Path:
    return Path(os.environ.get("FNMUSIC_ADMIN_UI_PID") or DEFAULT_PID)


def admin_prefix() -> str:
    return (os.environ.get("FNMUSIC_ADMIN_PREFIX") or DEFAULT_PREFIX).rstrip("/")


def _stop_previous(pid_path: Path) -> None:
    if not pid_path.is_file():
        return
    try:
        pid = int(pid_path.read_text().strip())
    except (OSError, ValueError):
        return
    if pid == os.getpid():
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def _daemonize() -> None:
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.chdir("/")
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        try:
            os.dup2(devnull, fd)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    base = Path(args[0]) if args else Path(__file__).resolve().parent.parent
    base = base.resolve()
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    os.environ.setdefault("FNMUSIC_ADMIN_PREFIX", admin_prefix())
    sock = ui_sock()
    pid_path = pid_file()
    sock.parent.mkdir(parents=True, exist_ok=True)
    try:
        sock.unlink()
    except FileNotFoundError:
        pass

    _stop_previous(pid_path)
    _daemonize()
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        import uvicorn

        from proxy import admin_ui

        uvicorn.run(
            admin_ui.app,
            uds=str(sock),
            log_level=os.environ.get("FNMUSIC_ADMIN_UI_LOG_LEVEL", "info"),
        )
    finally:
        try:
            pid_path.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
