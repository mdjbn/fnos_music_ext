"""日志保留策略（proxy/loghouse.py）测试。

重点覆盖 fd 安全性：本项目日志都由 shell 的 `>> file.log 2>&1` 重定向产生，
写入端以 O_APPEND 持有 inode。rename/unlink 会让进程继续写幽灵 inode（日志丢失
且磁盘不释放），因此活跃日志必须「就地截断保留尾部」。
"""
import os
import subprocess
import time

import pytest

from proxy import loghouse

MB = 1048576


@pytest.fixture
def logdir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    return str(d)


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def age(path, days):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


# ------------------------------------------------------------- 大小截断 ----

def test_oversize_log_truncated_in_place_keeping_tail(logdir):
    p = os.path.join(logdir, "proxy.log")
    write(p, "".join(f"line {i:06d} " + "x" * 40 + "\n" for i in range(60000)))
    before = os.path.getsize(p)

    rep = loghouse.scan(logdir, max_mb=1.0, max_days=30)
    after = os.path.getsize(p)

    assert any(a["action"] == "truncated_oversize" for a in rep["actions"])
    assert after < before
    assert after <= 1.0 * MB
    with open(p, "rb") as f:
        body = f.read()
    assert b"line 0599" in body, "必须保留最近的内容"
    assert b"line 000001" not in body, "不该保留最旧的内容"
    assert rep["freed_mb"] > 0


def test_truncation_lands_on_line_boundary(logdir):
    """截断后首行不能是半行残片，否则日志看起来像乱码。"""
    p = os.path.join(logdir, "proxy.log")
    write(p, "".join(f"MARKER-{i:05d}-{'y'*60}\n" for i in range(40000)))
    loghouse.scan(logdir, max_mb=0.5, max_days=30)
    first = open(p, encoding="utf-8").readline()
    assert first.startswith("MARKER-"), first[:40]
    assert first.endswith("\n")


def test_truncation_never_leaves_zero_byte_file(logdir):
    p = os.path.join(logdir, "tiny.log")
    write(p, "z" * 3 * MB)
    # keep_bytes 比单行长度还小的极端情况下也要留下可读内容
    loghouse.scan(logdir, max_mb=0.0000001, max_days=0)
    assert os.path.getsize(p) > 0


def test_zero_max_mb_disables_size_policy(logdir):
    p = os.path.join(logdir, "proxy.log")
    write(p, "z" * 3 * MB)
    before = os.path.getsize(p)
    rep = loghouse.scan(logdir, max_mb=0, max_days=0)
    assert rep["actions"] == []
    assert os.path.getsize(p) == before


# ------------------------------------------------------ fd 安全（核心）----

def test_writer_keeps_appending_after_truncation(logdir):
    """截断后 O_APPEND 写入端必须仍能继续写，且写在文件末尾、不产生空洞。"""
    p = os.path.join(logdir, "live.log")
    proc = subprocess.Popen(
        ["bash", "-c", f'exec >> "{p}" 2>&1; while true; do echo "tick $(date +%s%N)"; sleep 0.2; done'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.0)
        with open(p, "a", encoding="utf-8") as f:
            f.write("pad\n" * 400000)
        oversize = os.path.getsize(p)

        loghouse.scan(logdir, max_mb=1.0, max_days=30)
        time.sleep(0.7)

        lines = open(p, encoding="utf-8").read().splitlines()
        assert os.path.getsize(p) < oversize
        ticks = [ln for ln in lines if ln.startswith("tick")]
        assert len(ticks) >= 2, f"截断后写入端应继续产出新行: {lines[-3:]}"
        assert lines[-1].startswith("tick"), "最新一行必须在文件末尾（fd 未失效）"
        assert b"\0" not in open(p, "rb").read(), "O_APPEND 偏移必须正确，不能出现空洞"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_scan_never_renames_live_logs(logdir):
    """scan 期间不得产生 .1 备份——那会让写入端跟着 inode 跑掉。"""
    p = os.path.join(logdir, "info.log")
    write(p, "z" * 3 * MB)
    loghouse.scan(logdir, max_mb=1.0, max_days=30)
    assert os.path.exists(p)
    assert not os.path.exists(p + ".1")


# ---------------------------------------------------------------- 超龄 ----

def test_stale_backup_deleted_but_live_log_only_cleared(logdir):
    live = os.path.join(logdir, "info.log")
    bak = os.path.join(logdir, "info.log.1")
    write(live, "old\n")
    write(bak, "old\n")
    age(live, 40)
    age(bak, 40)

    rep = loghouse.scan(logdir, max_mb=10, max_days=30)
    acts = {a["file"]: a["action"] for a in rep["actions"]}

    # 备份没有进程持有 fd，直接删
    assert not os.path.exists(bak)
    assert acts["info.log.1"] == "deleted_stale_backup"
    # 活跃日志可能有进程正持有 fd：清空内容但保留文件
    assert os.path.exists(live)
    assert os.path.getsize(live) == 0
    assert acts["info.log"] == "cleared_stale_log"


def test_recent_files_untouched_by_age_policy(logdir):
    fresh = os.path.join(logdir, "musicbox.log")
    write(fresh, "fresh content\n")
    stale = os.path.join(logdir, "old.log")
    write(stale, "old\n")
    age(stale, 40)

    loghouse.scan(logdir, max_mb=10, max_days=30)
    assert os.path.getsize(fresh) > 0, "未超龄文件不该被动"
    assert os.path.getsize(stale) == 0


def test_zero_max_days_disables_age_policy(logdir):
    p = os.path.join(logdir, "info.log")
    write(p, "old\n")
    age(p, 400)
    rep = loghouse.scan(logdir, max_mb=0, max_days=0)
    assert rep["actions"] == []
    assert os.path.getsize(p) > 0


# ------------------------------------------------------------ 备份份数 ----

def test_backup_count_capped(logdir):
    write(os.path.join(logdir, "x.log"), "a")
    for n in (1, 2, 3, 4):
        write(os.path.join(logdir, f"x.log.{n}"), "b" * n)

    rep = loghouse.scan(logdir, max_mb=10, max_days=30, keep_backups=2)
    left = sorted(os.listdir(logdir))
    assert left == ["x.log", "x.log.1", "x.log.2"]
    assert any(a["action"] == "deleted_excess_backup" for a in rep["actions"])


def test_keep_zero_backups_removes_all(logdir):
    write(os.path.join(logdir, "x.log"), "a")
    write(os.path.join(logdir, "x.log.1"), "b")
    loghouse.scan(logdir, max_mb=10, max_days=30, keep_backups=0)
    assert sorted(os.listdir(logdir)) == ["x.log"]


# ---------------------------------------------------------------- 边界 ----

def test_missing_dir_reports_error_not_raises():
    rep = loghouse.scan("/nonexistent/dir/xyz")
    assert rep["errors"] and rep["actions"] == []
    assert rep["scanned"] == 0


def test_empty_dir_no_actions(logdir):
    rep = loghouse.scan(logdir)
    assert rep["actions"] == [] and rep["errors"] == []


def test_non_log_files_never_touched(logdir):
    keep = os.path.join(logdir, "keep.txt")
    data = os.path.join(logdir, "data.json")
    env = os.path.join(logdir, ".env")
    for p in (keep, data, env):
        write(p, "x" * 200)
    loghouse.scan(logdir, max_mb=0.0001, max_days=0)
    for p in (keep, data, env):
        assert os.path.getsize(p) == 200


def test_subdirectories_ignored(logdir):
    sub = os.path.join(logdir, "sub")
    os.makedirs(sub)
    write(os.path.join(sub, "deep.log"), "z" * 3 * MB)
    loghouse.scan(logdir, max_mb=1.0, max_days=0)
    assert os.path.getsize(os.path.join(sub, "deep.log")) == 3 * MB


def test_dry_run_reports_without_touching(logdir):
    p = os.path.join(logdir, "proxy.log")
    write(p, "z" * 3 * MB)
    before = os.path.getsize(p)
    rep = loghouse.scan(logdir, max_mb=1.0, max_days=0, dry_run=True)
    assert rep["actions"], "dry_run 也应报告将要做什么"
    assert os.path.getsize(p) == before, "dry_run 绝不能改动文件"


# ---------------------------------------------------------- rotate_file ----

def test_rotate_file_creates_backup_and_empty_original(logdir):
    p = os.path.join(logdir, "setup.log")
    write(p, "y" * 3 * MB)
    assert loghouse.rotate_file(p, max_mb=1.0) is True
    assert os.path.exists(p + ".1")
    assert os.path.getsize(p + ".1") == 3 * MB
    assert os.path.exists(p), "原路径必须仍存在（契约：另存备份再清空）"
    assert os.path.getsize(p) == 0
    assert oct(os.stat(p).st_mode & 0o777) == "0o644"


def test_rotate_file_noop_under_threshold(logdir):
    p = os.path.join(logdir, "setup.log")
    write(p, "small")
    assert loghouse.rotate_file(p, max_mb=1.0) is False
    assert not os.path.exists(p + ".1")


def test_rotate_file_missing_path_is_false(logdir):
    assert loghouse.rotate_file(os.path.join(logdir, "gone.log")) is False
    assert loghouse.rotate_file("") is False
