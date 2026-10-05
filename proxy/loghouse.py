"""日志保留策略：单文件超阈值就地截断保留尾部，过期文件按类型清理。

## 为什么不能直接 rename / delete

本项目的日志都是 shell 用 `>> file.log 2>&1` 重定向出来的，写入端（uvicorn / bash）
以 **O_APPEND** 持有一个打开的 fd。在 POSIX 上：

- `rename(file.log, file.log.1)` 之后，写入端的 fd 仍指向**被改名后的那个 inode**，
  于是新日志继续追加到 `file.log.1`，而 `file.log` 永远不会被重新创建；
- `unlink(file.log)` 更糟：进程继续往已删除的 inode 写，磁盘空间要等进程退出才释放，
  而用户从此再也看不到任何日志。

所以这里对**活跃日志一律就地截断保留尾部**：O_APPEND 语义下写入端下一次 write 会
落在新的文件末尾，fd 依然有效，空间立刻回收。

## 规则

对目录下的 `*.log`：

1. `size > max_bytes` → 保留最后 `keep_bytes`（默认 max_bytes 的一半），就地截断；
2. `mtime` 距今超过 `max_days` → 活跃日志就地清空（内容已过期且没人再看），
   轮转备份 `*.log.N` 直接删除。

轮转备份 `*.log.N` 额外按 `keep_backups` 限制份数，超出删最旧的。

所有判定都基于文件名后缀与 stat 结果，不做任何通配删除；`max_bytes<=0` 或
`max_days<=0` 表示关闭对应策略。
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger("fnmusic_proxy.loghouse")

DEFAULT_MAX_MB = 10.0
DEFAULT_MAX_DAYS = 30.0
DEFAULT_KEEP_BACKUPS = 1

_BACKUP_RE = re.compile(r"^(?P<base>.+\.log)\.(?P<n>\d+)$")
_LIVE_SUFFIXES = (".log",)


def _is_log_name(name: str) -> bool:
    return name.endswith(_LIVE_SUFFIXES) or bool(_BACKUP_RE.match(name))


def scan(
    log_dir: str,
    *,
    max_mb: float = DEFAULT_MAX_MB,
    max_days: float = DEFAULT_MAX_DAYS,
    keep_backups: int = DEFAULT_KEEP_BACKUPS,
    dry_run: bool = False,
) -> dict[str, Any]:
    """执行一次清理，返回动作报告（供管理页面展示）。

    绝不抛异常：清理失败只记日志，不影响任何主流程。
    """
    report: dict[str, Any] = {
        "dir": log_dir,
        "max_mb": max_mb,
        "max_days": max_days,
        "actions": [],
        "scanned": 0,
        "freed_bytes": 0,
        "errors": [],
    }
    if not log_dir or not os.path.isdir(log_dir):
        report["errors"].append(f"日志目录不存在：{log_dir or '(未配置)'}")
        return report

    max_bytes = int(max_mb * 1048576) if max_mb and max_mb > 0 else 0
    keep_bytes = max_bytes // 2 if max_bytes else 0
    max_age_s = max_days * 86400 if max_days and max_days > 0 else 0.0
    now = time.time()

    try:
        entries = sorted(os.listdir(log_dir))
    except OSError as exc:
        report["errors"].append(f"无法列举目录：{exc}")
        return report

    backups: dict[str, list[tuple[int, str, int]]] = {}

    for name in entries:
        path = os.path.join(log_dir, name)
        if not os.path.isfile(path) or not _is_log_name(name):
            continue
        try:
            st = os.stat(path)
        except OSError as exc:
            report["errors"].append(f"{name}: stat 失败 {exc}")
            continue
        report["scanned"] += 1
        size = st.st_size
        age_s = now - st.st_mtime
        freed = 0

        m = _BACKUP_RE.match(name)
        if m:
            backups.setdefault(m.group("base"), []).append((int(m.group("n")), name, size))

        acted = False

        # 1) 轮转备份：超龄直接删（没有进程持有它的 fd）
        if m and max_age_s and age_s > max_age_s:
            if not dry_run:
                try:
                    os.remove(path)
                    freed = size
                except OSError as exc:
                    report["errors"].append(f"{name}: 删除失败 {exc}")
                    continue
            acted = True
            report["actions"].append(
                {"file": name, "action": "deleted_stale_backup",
                 "age_days": round(age_s / 86400, 2), "bytes": size})

        # 2) 活跃日志：超过大小上限 → 就地截断，保留尾部
        elif max_bytes and size > max_bytes:
            if not dry_run:
                ok, err, new_size = _truncate_keep_tail(path, keep_bytes)
                if ok:
                    freed = max(0, size - new_size)
                else:
                    report["errors"].append(f"{name}: {err}")
                    continue
            else:
                new_size = keep_bytes
                freed = max(0, size - new_size)
            acted = True
            report["actions"].append(
                {"file": name, "action": "truncated_oversize",
                 "size_mb": round(size / 1048576, 2),
                 "kept_mb": round(new_size / 1048576, 2)})

        # 3) 活跃日志：超龄 → 清空内容但保留文件（可能有进程正持有 fd）
        elif max_age_s and age_s > max_age_s:
            if not dry_run:
                try:
                    with open(path, "r+b") as f:
                        f.truncate(0)
                    freed = size
                except OSError as exc:
                    report["errors"].append(f"{name}: 清空失败 {exc}")
                    continue
            else:
                freed = size
            acted = True
            report["actions"].append(
                {"file": name, "action": "cleared_stale_log",
                 "age_days": round(age_s / 86400, 2), "bytes": size})

        if acted:
            report["freed_bytes"] += freed

    # 4) 轮转备份份数上限：保留编号最小的 N 份（.1 最新）
    for base, items in backups.items():
        items.sort(key=lambda x: x[0])
        for _n, name, size in items[keep_backups:]:
            path = os.path.join(log_dir, name)
            if not dry_run:
                try:
                    os.remove(path)
                    report["freed_bytes"] += size
                except OSError as exc:
                    report["errors"].append(f"{name}: 删除失败 {exc}")
                    continue
            report["actions"].append(
                {"file": name, "action": "deleted_excess_backup",
                 "base": os.path.basename(base), "bytes": size})

    report["freed_mb"] = round(report["freed_bytes"] / 1048576, 2)
    if report["actions"]:
        logger.info(
            "日志清理：%d 个文件处理，回收 %.2f MB（dir=%s）",
            len(report["actions"]), report["freed_mb"], log_dir,
        )
    return report


def _truncate_keep_tail(path: str, keep_bytes: int) -> tuple[bool, str, int]:
    """就地截断，只保留文件末尾 keep_bytes。

    对 O_APPEND 的写入端是安全的：截断后再写会落在新的末尾，fd 依然有效。
    读取与写回之间窗口极短，最坏情况丢几行并发写入的日志——对排障日志可接受，
    换来的是不会把整个日志文件误删导致再也收不到任何输出。
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - keep_bytes))
            tail = f.read()
        # 丢掉可能被截断的首行残片，避免出现半行日志
        nl = tail.find(b"\n")
        if nl > 0:
            tail = tail[nl + 1:]
        if not tail:
            tail = "[loghouse] 日志因超过大小上限被清理\n".encode("utf-8")
        with open(path, "r+b") as f:
            f.seek(0)
            f.write(tail)
            f.truncate(len(tail))
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        return True, "", len(tail)
    except OSError as exc:
        return False, f"截断失败 {exc}", 0


def rotate_file(path: str, *, max_mb: float = DEFAULT_MAX_MB,
                keep_backups: int = DEFAULT_KEEP_BACKUPS) -> bool:
    """给单个文件做一次「另存备份再清空」。

    与 scan() 的就地截断不同：这里会真的产生 `.1` 备份，适合在**服务启动前**
    调用（此时没有进程持有旧 fd，rename 是安全的）。运行期请用 scan()。
    """
    if not path or not os.path.isfile(path):
        return False
    try:
        if max_mb > 0 and os.path.getsize(path) <= max_mb * 1048576:
            return False
    except OSError:
        return False

    try:
        for n in range(keep_backups, 0, -1):
            src = path if n == 1 else f"{path}.{n - 1}"
            dst = f"{path}.{n}"
            if os.path.exists(src) and src != dst:
                os.replace(src, dst)
        # 原文件已被改名成备份，这里必须重新建一个空的，否则调用方期望的
        # 「原路径仍存在且为空」不成立；后续以 >> 写入的进程也会重新创建，
        # 但显式建好可以立刻把权限设定为 0644（日志可能含路径等信息）。
        if not os.path.exists(path):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            os.close(fd)
        else:
            with open(path, "r+b") as f:
                f.truncate(0)
        return True
    except OSError as exc:
        logger.warning("rotate_file(%s) 失败: %s", path, exc)
        return False
