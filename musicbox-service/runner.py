"""Execute darknessomi/musicbox CLI with proxy env stripped (网易云需直连)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

PROXY_VARS = {
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
    "no_proxy",
    "NO_PROXY",
}


class MusicboxTimeoutError(Exception):
    pass


def ensure_xdg_dirs() -> None:
    """Ensure XDG related directories and netease-musicbox subdirectories exist."""
    cache_home = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    runtime_home = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"

    dir_bases = [
        cache_home,
        config_home,
        data_home,
        runtime_home,
        os.path.expanduser("~/.netease-musicbox"),
    ]

    for base in dir_bases:
        if not base:
            continue
        try:
            os.makedirs(base, exist_ok=True)
            if not base.endswith("netease-musicbox"):
                os.makedirs(os.path.join(base, "netease-musicbox"), exist_ok=True)
        except OSError:
            pass


ensure_xdg_dirs()


def get_clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in PROXY_VARS}


def run_musicbox(args: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    ensure_xdg_dirs()
    env = get_clean_env()
    try:
        proc = subprocess.run(
            ["musicbox", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        raise MusicboxTimeoutError(f"musicbox timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        return 127, "", f"musicbox executable not found: {exc}"


# ===========================================================================
# W5 增量移植：G（gzywd v2.9.30）的 CLI 解析能力（只增不改）。
#
# 背景：`musicbox` 是 pip 装进虚拟环境的 console script，位置在 <venv>/bin/musicbox。
# 本服务用绝对路径调 venv 里的 uvicorn 时，venv 的 bin/ 并不在 PATH 上，
# 于是裸命令名解析不到 → 127 → 所有走 CLI 的端点 502，而 /healthz 仍是 200。
#
# A 既有 run_musicbox() 保持原样不动（行为基线）；下面新增解析器与
# run_musicbox_resolved()，供新增的 /api/v1/selftest 诊断端点使用。
# ===========================================================================

CLI_NAME = "musicbox"
MODULE_FALLBACK = "NEMbox"


def module_fallback() -> str:
    """回退目标模块名。抽成函数是为了让测试能把它指向一个不存在的模块，
    从而验证「彻底找不到 CLI」这条分支——否则装了真实 NEMbox 的机器上
    模块回退总会命中，那条分支根本测不到。"""
    return MODULE_FALLBACK


def bin_dir() -> str:
    """当前解释器所在目录（venv 模式下即 <venv>/bin）。"""
    try:
        return os.path.dirname(os.path.abspath(sys.executable))
    except Exception:  # noqa: BLE001
        return ""


def candidate_paths() -> list[str]:
    """按优先级列出可能存在的 musicbox 可执行文件路径。"""
    out: list[str] = []
    dirs: list[str] = []
    b = bin_dir()
    if b:
        dirs.append(b)
    for p in (getattr(sys, "prefix", ""), getattr(sys, "base_prefix", "")):
        if p:
            dirs.append(os.path.join(p, "bin"))
            # Windows / 某些发行版布局
            dirs.append(os.path.join(p, "Scripts"))
    seen = set()
    for d in dirs:
        if not d or d in seen:
            continue
        seen.add(d)
        out.append(os.path.join(d, CLI_NAME))
        out.append(os.path.join(d, CLI_NAME + ".exe"))
    return out


def resolve_musicbox_cmd() -> tuple[list[str], str]:
    """解析出可用的 musicbox 调用方式。返回 (argv 前缀, 解析方式)。

    解析失败时返回 ([], "not_found:<已尝试的路径>")，调用方据此给出可读错误。
    """
    for path in candidate_paths():
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return [path], f"absolute:{path}"

    found = shutil.which(CLI_NAME)
    if found:
        return [found], f"which:{found}"

    # 最后回退到模块方式（等价于 console script 的入口）
    try:
        proc = subprocess.run(
            [sys.executable, "-c",
             f"import importlib.util,sys;"
             f"sys.exit(0 if importlib.util.find_spec('{module_fallback()}') else 1)"],
            capture_output=True, timeout=15,
        )
        if proc.returncode == 0:
            return [sys.executable, "-m", MODULE_FALLBACK], f"module:{module_fallback()}"
    except Exception:  # noqa: BLE001
        pass

    return [], "not_found:" + ",".join(candidate_paths())[:600]


_CMD_CACHE: "tuple[list[str], str] | None" = None


def musicbox_cmd() -> tuple[list[str], str]:
    """带缓存的解析结果（进程生命周期内 CLI 位置不会变）。"""
    global _CMD_CACHE
    if _CMD_CACHE is None:
        _CMD_CACHE = resolve_musicbox_cmd()
    return _CMD_CACHE


def reset_cmd_cache() -> None:
    """测试钩子 / selftest 用。"""
    global _CMD_CACHE
    _CMD_CACHE = None


def run_musicbox_resolved(args: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    """解析后再执行 CLI（A 既有 run_musicbox 的解析增强版，只增不改）。

    子进程 PATH 额外前置 venv 的 bin/：CLI 自身可能再派生子进程，且这样即便
    解析回退到 which() 也能命中 venv 里的脚本。解析不到时返回明确的诊断文本，
    而不是让上层只看到一个 127。
    """
    ensure_xdg_dirs()
    env = get_clean_env()
    bindir = bin_dir()
    if bindir:
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    cmd, how = musicbox_cmd()
    if not cmd:
        # 明确告诉调用方"去哪找过都没找到"，而不是含糊的 127
        return (
            127,
            "",
            f"musicbox CLI not found (tried: {how.split(':', 1)[-1]}). "
            f"Expected a console script next to the interpreter: {bindir}/{CLI_NAME}. "
            f"Reinstall dependencies into the service virtualenv.",
        )
    try:
        proc = subprocess.run(
            [*cmd, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        raise MusicboxTimeoutError(f"musicbox timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        return 127, "", f"musicbox executable not found ({how}): {exc}"
