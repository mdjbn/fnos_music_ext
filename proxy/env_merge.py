"""fnmusic-ext .env 安全合并工具（防覆盖 / 平滑升级）.

install.sh 在写入 .env 前先收集“本次安装期望的配置”，再调用本模块与
已有 .env 做增量合并：

- 已存在的配置项一律保留用户现有值（密钥、自定义路径、ONLINE_SOURCES 等），
  除非该键出现在 explicit（用户本次明确提供了新值）列表中；
- 新版本引入的新配置项 / 缺失配置项自动安全补齐；
- 用户手工添加的自定义键原样保留；
- 用户手写的注释/非赋值行按原顺序去重后保留在文件末尾（合并多次不堆积）；
- 合并结果原子写入并保持 0600 权限，合并前由调用方负责备份。

CLI:
    python3 proxy/env_merge.py --existing .env --desired desired.env \
        --output .env --explicit KEY1,KEY2 [--quiet]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

# 第三音源 lxmusic（洛雪音乐源）默认配置：合并时自动识别并安全补齐
LX_COMMENT = "洛雪音乐源 lxmusic（第三音源，宿主机端口 8772 -> 容器 8000）"
LX_DEFAULTS: "list[tuple[str, str]]" = [
    ("FNMUSIC_LX_ENABLED", "true"),
    ("FNMUSIC_LX_URL", "http://127.0.0.1:8772"),
]
LX_PREFIX = "FNMUSIC_LX_"

# 新配置项（移植自 gzywd v2.9.x 的功能模块：预取 / 音质策略 / 本地优先 /
# 在线歌单 / 网易登录态 / PushPlus / 下载 / 日志治理）默认值。
# 合并时按前缀自动安全补齐缺失键；已存在的键一律保留用户现有值。
NEW_KEYS_COMMENT = "v2.7 起新增配置项（在线歌单 / 预取 / 音质策略 / 本地优先 / 日志）"
DEFAULT_PUSHPLUS_URL = "https://www.pushplus.plus/send"

NEW_DEFAULTS: "list[tuple[str, str]]" = [
    ("FNMUSIC_FREE_ONLY_ON_LOGOUT", "true"),
    ("FNMUSIC_DAILY_ENABLED", "true"),
    ("FNMUSIC_DAILY_LIMIT", "20"),
    ("FNMUSIC_LOCAL_DAILY_ENABLED", "true"),
    ("FNMUSIC_LOCAL_DAILY_LIMIT", "50"),
    ("FNMUSIC_LOCAL_FIRST", "true"),
    ("FNMUSIC_LOCAL_FIRST_ANY_CLASS", "true"),
    ("FNMUSIC_LOCAL_FIRST_CELLULAR_LOSSY_ONLY", "true"),
    ("FNMUSIC_PREFETCH_NEXT", "true"),
    ("FNMUSIC_PREFETCH_MAX_QUEUE", "2"),
    ("FNMUSIC_NETEASE_CHANNELS", "mine,toplist,category"),
    ("FNMUSIC_NETEASE_CHANNEL_LIMIT", "8"),
    ("FNMUSIC_NETEASE_CATEGORY", "华语"),
    ("FNMUSIC_NETEASE_CHANNEL_ORDER", "localdaily,daily,mine,nrec,toplist,category,newalbum,fm"),
    ("FNMUSIC_NETEASE_PLAYLIST_ORDER", ""),
    ("FNMUSIC_PLAYLIST_TRACK_CACHE_TTL", "21600"),
    ("FNMUSIC_PLAYLIST_REFRESH_AT", "04:30"),
    ("FNMUSIC_PLAYLIST_TRACK_LIMIT", "300"),
    ("FNMUSIC_PLAYLIST_CACHE_DIR", ""),
    ("FNMUSIC_URL_CACHE_TTL", "600"),
    ("FNMUSIC_CHANNEL_LIST_CACHE_TTL", "300"),
    ("FNMUSIC_REMOTE_AS_CELLULAR", "true"),
    ("FNMUSIC_UNKNOWN_AS_CELLULAR", "false"),
    ("FNMUSIC_LOCAL_INDEX_TTL", "300"),
    ("FNMUSIC_DOWNLOAD_DIR", ""),
    # A 侧决策：收藏自动下载默认关闭（FNMUSIC_DOWNLOAD_ON_FAVORITE 只是开关，
    # 开与不开都不影响 A 既有的 _register_fav_autobind 行为）
    ("FNMUSIC_DOWNLOAD_ON_FAVORITE", "false"),
    # A 侧决策：往**用户网易云账号**写红心是对账号的写操作，默认关，要用户自己开
    ("FNMUSIC_FAV_SYNC_LIKE", "false"),
    ("FNMUSIC_QUALITY_WIFI", "lossless"),
    ("FNMUSIC_QUALITY_CELLULAR", "exhigh"),
    ("FNMUSIC_QUALITY_DB_RESCAN", "300"),
    ("FNMUSIC_LOGIN_STATE_TTL", "300"),
    ("FNMUSIC_LOGIN_CHECK_INTERVAL", "3600"),
    ("FNMUSIC_PUSHPLUS_ENABLED", "true"),
    ("FNMUSIC_PUSHPLUS_TOKEN", ""),
    ("FNMUSIC_PUSHPLUS_TOPIC", ""),
    ("FNMUSIC_PUSHPLUS_TEMPLATE", "markdown"),
    ("FNMUSIC_PUSHPLUS_URL", DEFAULT_PUSHPLUS_URL),
    ("FNMUSIC_LOG_MAX_MB", "10"),
    ("FNMUSIC_LOG_MAX_DAYS", "30"),
    ("FNMUSIC_LOG_SCAN_INTERVAL", "3600"),
    ("FNMUSIC_LOG_QUIET", "true"),
]

# 补齐新配置项时允许的前缀集合（必须包含精确元素 "FNMUSIC_PREFETCH_"）
NEW_PREFIXES: "tuple[str, ...]" = (
    "FNMUSIC_FREE_ONLY",
    "FNMUSIC_DAILY",
    "FNMUSIC_LOCAL_DAILY",
    "FNMUSIC_LOGIN_",
    "FNMUSIC_VIP_",
    "FNMUSIC_PUSHPLUS_",
    "FNMUSIC_LOG_",
    "FNMUSIC_SEARCH_",
    "FNMUSIC_NETEASE_CHANNEL",
    "FNMUSIC_NETEASE_CATEGOR",
    "FNMUSIC_NETEASE_PLAYLIST_",
    "FNMUSIC_PLAYLIST_",
    "FNMUSIC_WARM_",
    "FNMUSIC_DOWNLOAD_",
    "FNMUSIC_FAV_",
    "FNMUSIC_QUALITY_",
    "FNMUSIC_WATCHDOG_",
    "FNMUSIC_URL_CACHE_",
    "FNMUSIC_CHANNEL_LIST_",
    "FNMUSIC_REMOTE_AS_",
    "FNMUSIC_LOCAL_",
    "FNMUSIC_PREFETCH_",
    "FNMUSIC_COVER_",
    "FNMUSIC_UNKNOWN_AS_",
    "FNMUSIC_HLS_",
    "FNMUSIC_CDN_",
    "FNMUSIC_PLAY_",
)

# 废弃键清理：A（javycoder）侧删除集**刻意保持为空**。
# gzywd 把 FNMUSIC_ONLINE_SOURCES / FNMUSIC_MUSICDL_* / FNMUSIC_LX_* / FNMUSIC_LLM_* /
# FNMUSIC_QUALITY_POLICY / FNMUSIC_QUALITY_FIXED 等键列为废弃并在升级时删除；
# 但在 A 里这些键正是三音源选择与音质策略的配置入口（proxy/app.py、install.sh 均在读），
# 照搬会在升级时静默抹掉用户配置并废掉两个音源。故只保留 API 供 admin_ui 调用。
OBSOLETE_EXACT: "set[str]" = set()
OBSOLETE_PREFIXES: "tuple[str, ...]" = ()


def escape_single_quoted(value: str) -> str:
    """转义 .env 单引号包裹值中的单引号（与 install.sh dotenv_escape 一致）。"""
    return value.replace("'", "'\\''")


def unquote_env_value(raw: str) -> str:
    """去掉 KEY=VALUE 行 VALUE 部分的引号并还原转义的单引号。"""
    v = raw.strip()
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        inner = v[1:-1]
        return inner.replace("'\\''", "'")
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        inner = v[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    return v


def quote_env_value(value: str) -> str:
    return "'" + escape_single_quoted(value) + "'"


def parse_env_file(path: str | Path) -> "tuple[list[tuple[str, str]], list[str]]":
    """解析 .env 文件。

    返回 (kv_list, other_lines)：kv_list 为按出现顺序的 (key, value) 元组，
    other_lines 为注释/空行等非赋值行（仅记录内容，位置信息不保留）。
    """
    kv: list[tuple[str, str]] = []
    others: list[str] = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return kv, others
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if m:
            kv.append((m.group(1), unquote_env_value(m.group(2))))
        elif line.strip():
            others.append(line.rstrip())
    return kv, others


def merge_env(
    existing: "list[tuple[str, str]]",
    desired: "list[tuple[str, str]]",
    explicit: "set[str] | None" = None,
) -> "tuple[list[tuple[str, str]], dict[str, list[str]]":
    """增量合并配置。

    规则：
    1. desired 中存在、existing 中不存在的键 -> 安全补齐（added）；
    2. explicit 中的键 -> 采用 desired 新值（updated，用户本次明确提供）；
    3. 其余已有键 -> 保留用户现有值（preserved）；
    4. existing 中多出的自定义键 -> 原样保留（custom_kept）。
    """
    explicit = set(explicit or ())
    existing_map = dict(existing)
    desired_map = dict(desired)
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    summary = {"added": [], "updated": [], "preserved": [], "custom_kept": []}

    for key, _val in existing:
        if key in seen:
            continue
        seen.add(key)
        if key in desired_map:
            if key in explicit:
                result.append((key, desired_map[key]))
                summary["updated"].append(key)
            else:
                result.append((key, existing_map[key]))
                summary["preserved"].append(key)
        else:
            result.append((key, existing_map[key]))
            summary["custom_kept"].append(key)

    for key, val in desired:
        if key in seen:
            continue
        seen.add(key)
        result.append((key, val))
        summary["added"].append(key)

    return result, summary


def is_obsolete_key(key: str) -> bool:
    """判断某键是否属于“已无代码读取”的废弃配置项。

    A 侧删除集为空（见 OBSOLETE_EXACT 说明），故当前恒为 False；
    保留该函数与 drop_obsolete 是为了与 admin_ui / 升级流程的调用契约一致。
    """
    if key in OBSOLETE_EXACT:
        return True
    return any(key.startswith(p) for p in OBSOLETE_PREFIXES)


def drop_obsolete(kv: "list[tuple[str, str]]") -> "tuple[list[tuple[str, str]], list[str]]":
    """清理废弃配置项；返回 (保留列表, 被删除的键列表)。"""
    kept: "list[tuple[str, str]]" = []
    removed: "list[str]" = []
    for key, val in kv:
        if is_obsolete_key(key):
            removed.append(key)
            continue
        kept.append((key, val))
    return kept, removed


def ensure_prefix_defaults(
    kv: "list[tuple[str, str]]",
    defaults: "list[tuple[str, str]]" = None,
    prefix: str = LX_PREFIX,
    prefixes: "tuple[str, ...] | None" = None,
) -> "tuple[list[tuple[str, str]], list[str]]":
    """自动识别并补齐指定前缀的缺失配置项。

    不传参时保持 A 原有语义：只补齐第三音源 FNMUSIC_LX_*（install.sh 依赖）。
    传入 ``defaults=NEW_DEFAULTS, prefixes=NEW_PREFIXES`` 时按多前缀补齐新配置项
    （admin_ui 与 CLI 合并走这条路径）。

    已存在的键一律不动（保留用户现有值，包括 false / 自定义 URL / 空 token），
    仅追加缺失键；返回 (新列表, 追加的键列表)。
    """
    if defaults is None:
        defaults = LX_DEFAULTS
    enabled: "tuple[str, ...]" = tuple(prefixes) if prefixes else (prefix,)
    known = {k for k, _ in kv}
    out = list(kv)
    added: list[str] = []
    for key, val in defaults:
        if key in known:
            continue
        if enabled and not key.startswith(enabled):
            continue
        out.append((key, val))
        added.append(key)
    return out, added


def render_env(
    kv: "list[tuple[str, str]]",
    header: str = "",
    comments: "dict[str, str] | None" = None,
    trailing: "list[str] | None" = None,
) -> str:
    lines = []
    if header:
        lines.append(f"# {header}")
    for key, val in kv:
        if comments and key in comments:
            lines.append(f"# {comments[key]}")
        lines.append(f"{key}={quote_env_value(val)}")
    if trailing:
        lines.append("")
        lines.extend(trailing)
    return "\n".join(lines) + "\n"


def preserve_user_comments(
    lines: "list[str]",
    header: str = "",
) -> "list[str]":
    """筛出应保留的用户手写注释/非赋值行。

    剔除两类工具自生成行（避免合并多次后堆积）：
    - render_env 写出的文件头注释 ``# {header}``；
    - FNMUSIC_LX_* 键上方的 LX_COMMENT 注释（渲染时会重新生成）。
    其余行按原顺序去重（用户写了两遍相同注释也只保留一份）。
    """
    auto = set()
    if header:
        auto.add(f"# {header}".strip())
    out: "list[str]" = []
    seen: "set[str]" = set()
    for ln in lines:
        stripped = ln.strip()
        if not stripped or stripped in auto:
            continue
        if stripped.lstrip("#").strip() == LX_COMMENT:
            continue
        if stripped.lstrip("#").strip() == NEW_KEYS_COMMENT:
            continue
        if stripped in seen:
            continue
        seen.add(stripped)
        out.append(stripped)
    return out


def write_env_atomic(path: str | Path, content: str, mode: int = 0o600) -> None:
    path = Path(path)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent or "."), prefix=".env.merge.")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, str(path))
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def read_installed_version(env_path: str | Path) -> str:
    """读取现有 .env 中记录的已安装版本（FNMUSIC_VERSION）。"""
    kv, _ = parse_env_file(env_path)
    return dict(kv).get("FNMUSIC_VERSION", "")


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="fnmusic-ext .env safe merge")
    parser.add_argument("--existing", required=True, help="现有 .env 路径（可不存在）")
    parser.add_argument("--desired", required=True, help="本次安装期望的 .env 内容")
    parser.add_argument("--output", required=True, help="合并结果输出路径")
    parser.add_argument("--explicit", default="", help="用户本次明确提供新值的键，逗号分隔")
    parser.add_argument("--header", default="generated by install.sh — do not commit")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    existing_kv, existing_others = parse_env_file(args.existing)
    desired_kv, _ = parse_env_file(args.desired)
    explicit = {k.strip() for k in args.explicit.split(",") if k.strip()}
    merged, summary = merge_env(existing_kv, desired_kv, explicit)

    # 废弃键清理：A 侧删除集为空（见 OBSOLETE_EXACT 说明），此处仅保持升级流程契约一致
    merged, obsolete_removed = drop_obsolete(merged)
    if obsolete_removed:
        summary["removed"] = obsolete_removed
        dropped = set(obsolete_removed)
        summary["custom_kept"] = [k for k in summary.get("custom_kept", []) if k not in dropped]

    # 第三音源 FNMUSIC_LX_* 自动识别：缺失时安全补齐（带注释）
    merged, lx_added = ensure_prefix_defaults(merged)
    summary["added"].extend(lx_added)

    # 新配置项（预取 / 音质 / 本地优先 / 在线歌单 / 日志等）：缺失时安全补齐
    merged, new_added = ensure_prefix_defaults(merged, NEW_DEFAULTS, prefixes=NEW_PREFIXES)
    summary["added"].extend(new_added)

    # 用户手写注释不能在升级合并时被静默丢弃：去重后保留在文件末尾
    kept_comments = preserve_user_comments(existing_others, header=args.header)

    comments = {k: LX_COMMENT for k, _ in LX_DEFAULTS}
    comments[NEW_DEFAULTS[0][0]] = NEW_KEYS_COMMENT

    write_env_atomic(
        args.output,
        render_env(
            merged,
            args.header,
            comments=comments,
            trailing=kept_comments,
        ),
    )
    if not args.quiet:
        for action in ("removed", "added", "updated", "preserved", "custom_kept"):
            keys = summary.get(action) or []
            if keys:
                print(f"{action}: {','.join(sorted(keys))}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
