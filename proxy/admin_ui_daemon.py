#!/usr/bin/env python3
"""把管理界面（proxy/admin_ui.py）作为独立进程常驻。

A（javycoder）侧的进程模型是「一个 systemd 单元 + 若干 ExecStartPost 守护进程」：
`fnmusic-ext.service` 起主代理后，先由 `proxy/webui_gateway.py` 挂桌面网关 socket，
再由本脚本把管理界面挂到管理用 Unix socket 上。

为什么不让主代理直接托管管理界面：
- 管理界面要能改 .env、重启服务、看日志——这些动作在同一个进程里做会互相牵制；
- 主代理承担飞牛音乐的接管 socket，任何管理面的异常都不该让它掉线。

解释器（真机踩过的坑）：systemd 只会用 `/usr/bin/python3` 执行本脚本，而飞牛的
系统解释器没有 fastapi/uvicorn（依赖装在 `<base>/.venv-proxy`，takeover.py 也是
硬编码这个路径）。旧的写法在 daemonize 之后才 `import uvicorn`，而 daemonize 已把
stdout/stderr 指向 /dev/null，于是子进程静默死掉、ExecStartPost 仍然返回成功。
现在：fork 之前先探测并（必要时）`os.execv` 切到可用解释器，选中的解释器/路径先写
stderr（进 journald），daemonize 之后 fd 0/1/2 指向 `<base>/cache/admin-ui.log`，
任何启动异常都会留在那个文件里。

与 webui_gateway.py 一致：双重 fork 脱离 systemd 的 ExecStartPost 等待，
写 PID 文件、启动前先停掉上一份实例、退出时清理 PID 文件（uvicorn 收 SIGTERM 后
会把信号恢复默认并重抛给自己，所以清理挂在信号处理器上而不是 finally）。

管理界面自己会校验网关注入的 X-Trim-Isadmin 头，所以 socket 权限沿用
单元里的 UMask（0077，仅 root 可连），由同单元的网关进程转发请求。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_SOCK = "/run/fnmusic-ext/admin-ui.sock"
DEFAULT_PID = "/run/fnmusic-ext/admin-ui.pid"
# 桌面图标（网关 socket）挂在 /app/fnmusic-ext/，管理界面挂在它的 /admin 子路径下。
# admin_ui.py 用同一个值做前缀归一化，必须与网关转发的前缀一致。
DEFAULT_PREFIX = "/app/fnmusic-ext/admin"
# 主代理依赖所在的虚拟环境（相对 <base>），与 takeover.py / ensure_proxy_deps.sh 一致。
VENV_PYTHON = ".venv-proxy/bin/python"
# 管理页「重启」按钮要跑的脚本（相对 <base>）；存在才导出给 admin_ui。
RESTART_SCRIPT = "proxy/admin_restart.sh"
# daemonize 之后 stdout/stderr 的去处（相对 <base>）。
LOG_FILE = "cache/admin-ui.log"
# 防止 execv 之后再次 execv 的死循环标记。
REEXEC_FLAG = "FNMUSIC_ADMIN_UI_REEXEC"
DEPS = ("uvicorn", "fastapi")


def ui_sock() -> Path:
    return Path(os.environ.get("FNMUSIC_ADMIN_UI_SOCK") or DEFAULT_SOCK)


def pid_file() -> Path:
    return Path(os.environ.get("FNMUSIC_ADMIN_UI_PID") or DEFAULT_PID)


def admin_prefix() -> str:
    return (os.environ.get("FNMUSIC_ADMIN_PREFIX") or DEFAULT_PREFIX).rstrip("/")


def log_path(base: Path) -> Path:
    override = os.environ.get("FNMUSIC_ADMIN_UI_LOG")
    return Path(override) if override else base / LOG_FILE


def restart_script(base: Path) -> Path:
    return base / RESTART_SCRIPT


def candidate_pythons(base: Path) -> list[str]:
    """按优先级列出候选解释器：当前解释器 → 显式覆盖 → 主代理 venv。"""
    found: list[str] = []
    for cand in (sys.executable, os.environ.get("FNMUSIC_ADMIN_UI_PYTHON"),
                 str(base / VENV_PYTHON)):
        if not cand:
            continue
        text = str(Path(cand).expanduser())
        if text not in found:
            found.append(text)
    return found


def interpreter_ok(python: str) -> bool:
    """真的跑一遍 import，避免选到「存在但缺依赖」的解释器。"""
    try:
        if not os.access(python, os.X_OK):
            return False
    except OSError:
        return False
    try:
        result = subprocess.run(
            [python, "-c", "import " + ", ".join(DEPS)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def resolve_interpreter(base: Path) -> tuple[str | None, list[str]]:
    tried = candidate_pythons(base)
    for python in tried:
        if interpreter_ok(python):
            return python, tried
    return None, tried


def needs_reexec(python: str) -> bool:
    """已经 execv 过一次就不再切，避免选到同一个解释器时反复 exec。

    只按路径比较，**不能**用 realpath：venv 的 `bin/python` 通常是指向系统解释器的
    符号链接，realpath 相等会被误判成「已经在用这个解释器」而跳过切换，结果仍然用
    没有 fastapi/uvicorn 的系统解释器启动。
    """
    if os.environ.get(REEXEC_FLAG):
        return False
    try:
        return os.path.abspath(python) != os.path.abspath(sys.executable)
    except OSError:
        return True


def prepare_env(base: Path) -> None:
    """补齐管理界面需要的默认值，与 takeover.py 给子进程的环境保持一致。"""
    os.environ.setdefault("FNMUSIC_HOME", str(base))
    os.environ.setdefault("FNMUSIC_ADMIN_PREFIX", admin_prefix())
    script = restart_script(base)
    if script.is_file():
        os.environ.setdefault("FNMUSIC_ADMIN_RESTART_SCRIPT", str(script))


def _our_process(pid: int) -> bool:
    """PID 会被系统回收：确认这个 PID 确实还是我们的管理界面进程，别误杀别人。

    拿不到 /proc（非 Linux 或权限不足）时按旧行为处理，宁可多杀一次也不能漏掉旧实例。
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True
    cmdline = raw.replace(b"\0", b" ").decode("utf-8", "replace")
    # 必须带路径分隔符：`pytest proxy/tests/test_admin_ui_daemon.py` 这种命令行里也有
    # 同名子串，但它不是我们的进程。
    return "/admin_ui_daemon.py" in cmdline


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _stop_previous(pid_path: Path) -> None:
    if not pid_path.is_file():
        return
    try:
        pid = int(pid_path.read_text().strip())
    except (OSError, ValueError):
        pid = 0
    if pid <= 1 or pid == os.getpid():
        _forget_pid(pid_path)
        return
    if not _our_process(pid):
        _forget_pid(pid_path)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        _forget_pid(pid_path)
        return
    # 旧实例要先松开 socket，新实例才好接管
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.1)


def _forget_pid(pid_path: Path) -> None:
    try:
        pid_path.unlink()
    except OSError:
        pass


def _install_pid_guard(pid_path: Path) -> None:
    """退出时清掉 PID 文件。

    不能只靠 finally：uvicorn 收到 SIGTERM 后自己优雅退出，然后把信号恢复默认并
    **重新抛给自己**（uvicorn 的 capture_signals），进程被信号打死，Python 的
    finally 根本不会再执行，PID 文件就会残留。这里在 uvicorn 之前装好处理器——
    uvicorn 退出时会把它恢复回来并重抛信号，于是残留的 PID 文件由这里清掉。
    """
    def handler(signum, frame):
        _forget_pid(pid_path)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except (OSError, ValueError):
            pass


def _daemonize(log_file: Path) -> None:
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.chdir("/")
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        # 目录不可写也要能起来：退回 /dev/null（此时只能靠 journald 的那行预检）。
        fd = os.open(os.devnull, os.O_RDWR)
    for target in (0, 1, 2):
        try:
            os.dup2(fd, target)
        except OSError:
            pass
    if fd > 2:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    base = Path(args[0]) if args else Path(__file__).resolve().parent.parent
    base = base.resolve()
    script = Path(__file__).resolve()
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    prepare_env(base)

    python, tried = resolve_interpreter(base)
    if python is None:
        sys.stderr.write(
            "[admin-ui] 找不到带 " + "/".join(DEPS) + " 的 Python 解释器，管理界面不会启动。\n"
            "[admin-ui] 已尝试: " + ", ".join(tried) + "\n"
            "[admin-ui] 请确认主代理虚拟环境存在（ensure_proxy_deps.sh / extend.sh 会创建它），"
            "或用 FNMUSIC_ADMIN_UI_PYTHON 指定解释器。\n"
        )
        sys.stderr.flush()
        return 2
    if needs_reexec(python):
        os.environ[REEXEC_FLAG] = "1"
        sys.stderr.write(
            f"[admin-ui] {sys.executable} 缺少 {'/'.join(DEPS)}，改用 {python} 重新执行。\n"
        )
        sys.stderr.flush()
        os.execv(python, [python, str(script), str(base)])
        return 2

    sock = ui_sock()
    pid_path = pid_file()
    log_file = log_path(base)
    sock.parent.mkdir(parents=True, exist_ok=True)
    try:
        sock.unlink()
    except FileNotFoundError:
        pass

    _stop_previous(pid_path)
    sys.stderr.write(
        f"[admin-ui] base={base} python={python} sock={sock} pid={pid_path} log={log_file}\n"
    )
    sys.stderr.flush()

    _daemonize(log_file)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    _install_pid_guard(pid_path)
    try:
        import uvicorn

        from proxy import admin_ui

        print(f"[admin-ui] serving {admin_prefix()} on {sock} (pid={os.getpid()})", flush=True)
        uvicorn.run(
            admin_ui.app,
            uds=str(sock),
            log_level=os.environ.get("FNMUSIC_ADMIN_UI_LOG_LEVEL", "info"),
        )
    except BaseException as exc:  # 启动失败必须留在日志文件里，不能静默
        print(f"[admin-ui] 启动失败: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
    finally:
        _forget_pid(pid_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
