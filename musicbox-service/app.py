"""HTTP wrapper for https://github.com/darknessomi/musicbox (NetEase-MusicBox CLI)."""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import threading
from typing import Any

from fastapi import FastAPI, HTTPException, Path, Query, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from netease_ext import (
    BEST_QUALITY_CHAIN,
    album_songs as ne_album_songs,
    auth_detail as ne_auth_detail,
    batch_song_details,
    best_url_info as ne_best_url_info,
    category_playlists as ne_category_playlists,
    check_is_logged_in,
    check_is_logged_in as ne_check_is_logged_in,
    daily_songs as ne_daily_songs,
    filter_playable_song_ids,
    get_song_url,
    new_albums as ne_new_albums,
    personal_fm as ne_personal_fm,
    playlist_categories as ne_playlist_categories,
    playlist_track_ids,
    playlist_track_ids_limited as ne_playlist_track_ids,
    recommend_playlists as ne_recommend_playlists,
    reset_api,
    reset_api_instance,
    search_songs as ne_search_songs,
    search_web_fallback,
    song_like as ne_song_like,
    song_lyric_pair,
    song_raw_detail,
    song_url_info,
    songs_by_ids as ne_songs_by_ids,
    toplists as ne_toplists,
    user_playlists as fetch_user_playlists,
    user_playlists_for_uid as ne_user_playlists,
)
import runner
from runner import MusicboxTimeoutError, ensure_xdg_dirs

logger = logging.getLogger("musicbox_service.app")

ensure_xdg_dirs()

SEARCH_TYPES = {"song", "album", "artist", "playlist"}
QUALITY_WHITELIST = {"exhigh", "higher", "standard", "lossless", "hires", "jymaster"}


class UpstreamException(Exception):
    def __init__(self, exit_code: int, stderr: str):
        self.exit_code = exit_code
        self.stderr = (stderr or "")[:2000]


app = FastAPI(title="fnmusic-musicbox", version="1.0.0")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": exc.errors()})


@app.exception_handler(UpstreamException)
async def upstream_exception_handler(request, exc: UpstreamException):
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"error": "upstream_error", "exit_code": exc.exit_code, "stderr": exc.stderr},
    )


@app.exception_handler(MusicboxTimeoutError)
async def timeout_exception_handler(request, exc: MusicboxTimeoutError):
    return JSONResponse(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        content={"detail": "Upstream musicbox command timed out"},
    )


def exec_musicbox(args: list[str], timeout: float = 30.0) -> Any:
    code, stdout, stderr = runner.run_musicbox(args, timeout=timeout)
    if code != 0:
        raise UpstreamException(exit_code=code, stderr=stderr or stdout or "")
    try:
        return json.loads(stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise UpstreamException(exit_code=code, stderr=stderr or stdout or "") from exc


def _extract_payload(payload: Any) -> Any:
    if isinstance(payload, dict) and payload.get("ok") is True and "data" in payload:
        return payload["data"]
    return payload


def _parse_ids(ids_str: str | None) -> list[int]:
    if not ids_str or not ids_str.strip():
        raise HTTPException(status_code=422, detail="ids parameter is required")
    ids: list[int] = []
    for token in ids_str.split(","):
        token = token.strip()
        if not token:
            raise HTTPException(status_code=422, detail="Empty id in ids list")
        try:
            val = int(token)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid id {token!r}") from None
        if val <= 0:
            raise HTTPException(status_code=422, detail=f"Invalid id {token!r}")
        ids.append(val)
    if not 1 <= len(ids) <= 100:
        raise HTTPException(status_code=422, detail="ids count must be 1..100")
    return ids


@app.get("/healthz")
def healthz():
    return {"status": "ok", "source": "https://github.com/darknessomi/musicbox"}


@app.get("/api/v1/search")
def search(
    keyword: str = Query(...),
    type: str = Query("song"),
    limit: int = Query(20, ge=1, le=100),
):
    if not keyword.strip():
        raise HTTPException(status_code=400, detail="keyword cannot be empty")
    if type not in SEARCH_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid type {type!r}")

    res = None
    fallback_needed = False

    try:
        res = exec_musicbox(["search", keyword, "--type", type, "--limit", str(limit), "--json"])
        if isinstance(res, dict):
            code = res.get("code")
            msg = str(res.get("message") or res.get("msg") or "")
            if code == 405 or res.get("ok") is False or "405" in msg or "频繁" in msg:
                fallback_needed = True
            elif type == "song":
                raw_list = res.get("data")
                if isinstance(raw_list, list):
                    def _song_id(item: dict) -> int:
                        try:
                            return int(item.get("song_id") or item.get("id") or 0)
                        except (ValueError, TypeError):
                            return 0

                    song_ids = [sid for it in raw_list if isinstance(it, dict) for sid in [_song_id(it)] if sid]
                    if song_ids:
                        playable = filter_playable_song_ids(song_ids)
                        res["data"] = [it for it in raw_list if isinstance(it, dict) and _song_id(it) in playable]
                    else:
                        res["data"] = []
                    if not res["data"]:
                        fallback_needed = True
                else:
                    fallback_needed = True
            elif not res.get("data"):
                fallback_needed = True
        else:
            fallback_needed = True
    except (UpstreamException, MusicboxTimeoutError, Exception) as exc:
        logger.warning("exec_musicbox search failed or blocked: %s, falling back to web endpoint", exc)
        fallback_needed = True

    if fallback_needed:
        try:
            fallback_items = search_web_fallback(keyword, stype=type, limit=limit)
            if fallback_items:
                if type == "song":
                    song_ids = [it["song_id"] for it in fallback_items if it.get("song_id")]
                    if song_ids:
                        playable = filter_playable_song_ids(song_ids)
                        filtered = [it for it in fallback_items if it.get("song_id") in playable]
                        if filtered:
                            fallback_items = filtered
                return {"ok": True, "code": 200, "data": fallback_items}
        except Exception as exc:
            logger.warning("fallback search failed: %s", exc)

    if res is not None and isinstance(res, dict) and "data" in res:
        return res
    if res is not None:
        return res
    return {"ok": True, "code": 200, "data": []}


@app.get("/api/v1/song/{song_id}/url")
def song_url(song_id: int = Path(..., ge=1), quality: str = Query("exhigh")):
    if quality not in QUALITY_WHITELIST:
        raise HTTPException(status_code=400, detail=f"Invalid quality {quality!r}")
    # 进程内复用常驻实例解析（毫秒级，免 CLI 子进程冷启动）；失败再降级 CLI
    # 兜底，保留 not_logged_in 等结构化错误语义
    item = get_song_url(song_id, quality)
    if item is not None:
        return {"ok": True, "data": item}
    return exec_musicbox(["song", "url", str(song_id), "--quality", quality, "--json"])


@app.get("/api/v1/song/{song_id}/info")
def song_info(song_id: int = Path(..., ge=1)):
    return exec_musicbox(["song", "info", str(song_id), "--json"])


@app.get("/api/v1/songs/detail")
def songs_detail(ids: str = Query(None)):
    parsed = _parse_ids(ids)
    try:
        return {"ok": True, "data": batch_song_details(parsed)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/v1/song/{song_id}/lyric")
def song_lyric(song_id: int = Path(..., ge=1)):
    try:
        return {"ok": True, "data": song_lyric_pair(song_id)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/v1/artist/{artist_id}")
def artist(artist_id: int = Path(..., ge=1), limit: int = Query(20, ge=1, le=100)):
    return exec_musicbox(["artist", str(artist_id), "--limit", str(limit), "--json"])


@app.get("/api/v1/album/{album_id}")
def album(album_id: int = Path(..., ge=1)):
    return exec_musicbox(["album", str(album_id), "--json"])


@app.get("/api/v1/playlist/{playlist_id}")
def playlist(playlist_id: int = Path(..., ge=1)):
    return exec_musicbox(["playlist", "show", str(playlist_id), "--json"])


@app.get("/api/v1/user/playlists")
def user_playlists(limit: int = Query(100, ge=1, le=100)):
    """网易账号歌单列表（需扫码登录；自建/收藏的判定字段随行下发，由调用方过滤）。"""
    res = fetch_user_playlists(limit)
    if res is None:
        return {"ok": False, "error": "not_logged_in", "logged_in": False}
    return {"ok": True, "data": res["playlists"], "account_uid": res["uid"], "logged_in": True}


@app.get("/api/v1/user/playlists/{playlist_id}/tracks")
def user_playlist_tracks(playlist_id: int = Path(..., ge=1)):
    """网易歌单曲目：trackIds -> 批量详情 + 可播过滤（形状对齐 recommend rows）。"""
    ids = playlist_track_ids(playlist_id)
    if ids is None:
        return {"ok": False, "error": "not_logged_in", "logged_in": False}
    return {"ok": True, "data": batch_song_details(ids[:1000])}


def _cli_error_or_raise(exc: UpstreamException) -> Any:
    """CLI 非零退出时，若 stderr 带结构化 JSON 错误（如 not_logged_in）原样透传。"""
    try:
        parsed = json.loads(exc.stderr or "")
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(parsed, dict) and parsed.get("ok") is False:
        return parsed
    return None


def _playable_recommendation_rows(rows: Any, limit: int) -> list[dict]:
    """CLI 推荐输出 -> 批量详情 + 可播过滤（未登录剔除 VIP/试听片段）。"""
    ids: list[int] = []
    if isinstance(rows, list):
        for it in rows:
            if not isinstance(it, dict):
                continue
            try:
                sid = int(it.get("song_id") or it.get("id") or 0)
            except (ValueError, TypeError):
                sid = 0
            if sid > 0:
                ids.append(sid)
    return batch_song_details(ids[:100])[:limit]


@app.get("/api/v1/recommend/songs")
def recommend_songs(limit: int = Query(30, ge=10, le=60)):
    """网易每日推荐（已登录为个性化推荐；匿名设备返回平台通用推荐）。"""
    try:
        data = exec_musicbox(["recommend", "songs", "--limit", str(limit), "--json"])
    except UpstreamException as exc:
        parsed = _cli_error_or_raise(exc)
        if parsed is not None:
            return parsed
        raise
    rows = _playable_recommendation_rows(_extract_payload(data), limit)
    return {"ok": True, "data": rows, "logged_in": check_is_logged_in()}


@app.get("/api/v1/toplist")
def toplist(index: int = Query(-1), limit: int = Query(60, ge=1, le=100)):
    """网易榜单：不带 index 返回榜单列表；带 index 返回该榜单可播曲目。"""
    if index < 0:
        return exec_musicbox(["toplist", "--json"])
    try:
        data = exec_musicbox(["toplist", "--index", str(index), "--json"])
    except UpstreamException as exc:
        parsed = _cli_error_or_raise(exc)
        if parsed is not None:
            return parsed
        raise
    rows = _playable_recommendation_rows(_extract_payload(data), limit)
    return {"ok": True, "data": rows, "index": index}


@app.get("/api/v1/auth/status")
def auth_status():
    return exec_musicbox(["auth", "status", "--json"])


@app.post("/api/v1/auth/login")
def auth_login():
    data = exec_musicbox(["auth", "login", "--no-wait", "--json"])
    payload = _extract_payload(data)
    unikey = ""
    if isinstance(payload, dict):
        unikey = str(payload.get("unikey") or payload.get("codekey") or "")
    if unikey and isinstance(payload, dict):
        payload["qr_url"] = f"https://music.163.com/login?codekey={unikey}"
    return data


@app.get("/api/v1/auth/login/check")
def auth_login_check(unikey: str = Query(...)):
    if not unikey.strip():
        raise HTTPException(status_code=400, detail="unikey cannot be empty")
    data = exec_musicbox(["auth", "login", "--check", unikey, "--json"])
    # 扫码成功（803）时 CLI 子进程已把新 cookie 写盘，而常驻实例只在构造时
    # 读过盘：立即丢弃实例，让后续查询重建并读到新登录态
    payload = data.get("data") if isinstance(data, dict) else None
    if isinstance(payload, dict) and payload.get("code") == 803:
        reset_api("qr login success")
    return data


@app.get("/api/v1/auth/login/qr.png")
@app.get("/api/v1/auth/qr.png")
def auth_login_qr():
    try:
        import qrcode
    except ImportError as exc:
        raise HTTPException(status_code=501, detail="qrcode extra not installed") from exc
    data = exec_musicbox(["auth", "login", "--no-wait", "--json"])
    payload = _extract_payload(data)
    unikey = ""
    if isinstance(payload, dict):
        unikey = str(payload.get("unikey") or payload.get("codekey") or "")
    if not unikey:
        raise UpstreamException(0, "Missing unikey in auth login response")
    qr_url = f"https://music.163.com/login?codekey={unikey}"
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(qr_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/api/v1/auth/login/qr", response_class=Response)
@app.get("/api/v1/auth/qr", response_class=Response)
def auth_login_qr_text():
    data = exec_musicbox(["auth", "login", "--no-wait", "--json"])
    payload = _extract_payload(data)
    qr_ascii = ""
    unikey = ""
    if isinstance(payload, dict):
        qr_ascii = str(payload.get("qr_ascii") or "")
        unikey = str(payload.get("unikey") or payload.get("codekey") or "")
    if not qr_ascii:
        if not unikey:
            raise UpstreamException(0, "Missing unikey or qr_ascii in auth login response")
        qr_url = f"https://music.163.com/login?codekey={unikey}"
        try:
            import qrcode

            qr = qrcode.QRCode()
            qr.add_data(qr_url)
            qr.make(fit=True)
            f = io.StringIO()
            qr.print_ascii(out=f)
            qr_ascii = f.getvalue()
        except ImportError as exc:
            raise HTTPException(status_code=501, detail="qrcode extra not installed") from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to render QR ascii: {exc}") from exc
    if not qr_ascii.endswith("\n"):
        qr_ascii += "\n"
    return Response(content=qr_ascii, media_type="text/plain; charset=utf-8")


# ===========================================================================
# W5 增量移植：G（gzywd v2.9.30）独有的端点（只增不改）。
#
# 下面每条路由、每个响应信封都逐行对齐 G/musicbox-service/app.py，只是把依赖
# 换成 A 的现有实现（A 的 _get_api/check_is_logged_in/reset_api 等）。A 既有
# 路由与函数一律未动，新旧端点并存。
#
# 登录门槛：只有**内容确实与账号绑定**的口径才要求登录（账户歌单、推荐歌单、
# 私人FM、红心、每日推荐）；排行榜/分类歌单/新碟是无登录语义的公共内容。
# ===========================================================================

# musicbox CLI 退出码（NEMbox/cli.py）：3 表示未登录
CLI_EXIT_NOT_LOGGED_IN = 3


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _login_or_error(action: str, logged_in: bool | None = None):
    """统一的未登录应答。返回 None 表示已登录可继续。"""
    if logged_in is None:
        try:
            logged_in = ne_check_is_logged_in()
        except Exception as exc:  # noqa: BLE001
            logger.warning("login check failed for %s: %s: %s", action, type(exc).__name__, exc)
            logged_in = False
    if not logged_in:
        return {"ok": False, "error": "not_logged_in", "data": []}
    return None


# ---------------------------------------------------------------------------
# CLI 探测结果缓存（供 /api/v1/selftest 立即复用）
#
# G 版在 lifespan 里用后台线程预热 CLI；A 版不引入 lifespan（避免改动既有
# FastAPI(...) 构造与序列化/线程行为），只保留带缓存的探测，语义仍与 G 一致：
# 命中缓存立即返回，冷启动也不会把响应拖到管理页 10s 超时之外。
# ---------------------------------------------------------------------------

_CLI_PROBE: dict[str, Any] | None = None
_CLI_PROBE_LOCK = threading.Lock()


def reset_cli_probe_for_test() -> None:
    """测试钩子：清空 CLI 探测缓存。"""
    global _CLI_PROBE
    with _CLI_PROBE_LOCK:
        _CLI_PROBE = None


def probe_cli_exec(timeout_s: float = 3.0) -> tuple[bool, str]:
    """跑一次 ``musicbox --version`` 验证 CLI 真的可执行；确定性结果缓存后复用。

    超时不缓存：它多半意味着 CLI 还在冷启动，稍后重试会拿到真实结果。
    """
    global _CLI_PROBE
    with _CLI_PROBE_LOCK:
        if _CLI_PROBE is not None:
            return bool(_CLI_PROBE["ok"]), str(_CLI_PROBE["detail"])
    try:
        code, stdout, stderr = runner.run_musicbox_resolved(["--version"], timeout=timeout_s)
        ok = code == 0
        detail = ((stdout or stderr or "").strip()[:200]) or f"exit={code}"
        with _CLI_PROBE_LOCK:
            _CLI_PROBE = {"ok": ok, "detail": detail}
        return ok, detail
    except MusicboxTimeoutError as exc:
        # 带上异常类型名：超时是这里最可能的故障形态，日志必须可 grep
        return False, (
            f"{type(exc).__name__}: CLI 冷启动 timeout，未能在 {timeout_s}s 内完成"
            f"（首次执行需生成 deviceId，实测可达 ~47s）；稍后重试即可"
        )
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"{type(exc).__name__}: {exc}"[:200]
        with _CLI_PROBE_LOCK:
            _CLI_PROBE = {"ok": ok, "detail": detail}
        return ok, detail


@app.get("/api/v1/selftest")
def selftest():
    """自检：报告 musicbox CLI 是怎么解析到的、能不能真的跑起来。

    这个端点存在的理由是一个真实事故：服务用绝对路径的 venv uvicorn 启动时，
    venv 的 bin/ 不在 PATH 上，裸命令名 `musicbox` 解析不到，导致 12 个走 CLI
    的端点全部 502，而 /healthz 依然返回 200 —— 看起来"服务是好的"。
    """
    cmd, how = runner.musicbox_cmd()
    result: dict[str, Any] = {
        "cli_found": bool(cmd),
        "cli_cmd": cmd,
        "resolved_by": how,
        "interpreter": sys.executable,
        "venv_bin_dir": runner.bin_dir(),
        "python_version": sys.version.split()[0],
        "xdg": {
            "XDG_DATA_HOME": os.environ.get("XDG_DATA_HOME", ""),
            "XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME", ""),
            "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME", ""),
        },
        "running_as": os.environ.get("USER") or "?",
        "uid": os.getuid() if hasattr(os, "getuid") else None,
        "nembox_importable": False,
        "cli_exec_ok": False,
        "cli_exec_detail": "",
    }
    try:
        import NEMbox  # noqa: F401

        result["nembox_importable"] = True
    except Exception as exc:  # noqa: BLE001
        result["nembox_importable"] = False
        result["nembox_error"] = f"{type(exc).__name__}: {exc}"[:200]

    if cmd:
        # 用带缓存的短超时探测：命中缓存时立即返回，冷启动时也不会把响应拖到
        # 管理页面 10s 超时之外（那会导致整个 CLI 区块显示 undefined）。
        ok, detail = probe_cli_exec(timeout_s=3.0)
        result["cli_exec_ok"] = ok
        result["cli_exec_detail"] = detail
    return {"ok": True, "data": result}


@app.get("/api/v1/auth/detail")
def auth_detail_endpoint():
    """登录态详情（含 VIP 类型与到期时间），供代理层做降级门控与 PushPlus 提醒。

    走 NEMbox 已缓存的账号信息，不额外请求网易云；任何异常都降级为"未登录"，
    绝不让探测失败拖垮音源服务。
    """
    try:
        detail = ne_auth_detail()
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "data": {"logged_in": False, "error": str(exc)[:200]}}
    return {"ok": True, "data": detail}


@app.get("/api/v1/recommend/daily")
def recommend_daily(limit: int = Query(20, ge=1, le=100)):
    """网易云官方「每日推荐」歌曲（需登录扫码的私人账号）。

    **进程内实现，不走 ``musicbox recommend songs`` CLI。** CLI 内部调 dig_info，
    而 dig_info 在任意一首歌取不到直链时会 ``return []``，把整份日推清空。
    这里逐首判定：只有真正拿不到直链的那几首被剔除。

    返回：
      - 未登录 → HTTP 200 + {"ok": false, "error": "not_logged_in"}
      - 成功   → HTTP 200 + {"ok": true, "data": [...], "engine": "..."}
    """
    # 先判登录：未登录时上游 v3 接口会返回一份与账号画像无关的热门填充，
    # 名不副实，宁可不给。
    try:
        logged_in = ne_check_is_logged_in()
    except Exception as exc:  # noqa: BLE001
        logger.warning("login check failed for daily rec: %s", exc)
        logged_in = False
    if not logged_in:
        return {"ok": False, "error": "not_logged_in", "data": []}

    rows: list[dict] | None = None
    try:
        rows = ne_daily_songs(limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("in-process daily rec failed, will fall back to CLI: %s", exc)
        rows = None

    if rows:
        return {"ok": True, "data": rows[:limit], "engine": "in-process"}

    # 进程内拿到空结果或异常 → 回退 CLI（至少不比原来差），并如实标注来源
    try:
        code, stdout, stderr = runner.run_musicbox(
            ["recommend", "songs", "--limit", str(limit), "--json"], timeout=40.0
        )
    except MusicboxTimeoutError as exc:
        return JSONResponse(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            content={"ok": False, "error": "timeout", "detail": str(exc)[:200]},
        )

    # musicbox CLI 退出码约定：3 = 未登录（EXIT_NOT_LOGGED_IN）
    if code == CLI_EXIT_NOT_LOGGED_IN:
        return {"ok": False, "error": "not_logged_in", "data": []}
    if code != 0:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"ok": False, "error": "upstream_error", "exit_code": code,
                     "detail": (stderr or stdout or "")[:500]},
        )
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"ok": False, "error": "bad_upstream_json", "detail": str(exc)[:200]},
        )

    data = _extract_payload(payload)
    songs = [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []
    ids = [i for i in (_safe_int(s.get("song_id") or s.get("id")) for s in songs) if i]
    if ids:
        playable = filter_playable_song_ids(ids)
        songs = [s for s in songs if _safe_int(s.get("song_id") or s.get("id")) in playable]

    if not songs:
        logger.info(
            "daily rec empty: 进程内=%s 条, CLI 回退=%s 条（上游 dig_info 可能已清空结果）",
            0 if rows is None else len(rows or []), len(songs),
        )
        return {"ok": True, "data": [], "engine": "cli-fallback",
                "note": "上游返回空列表；可能是网络波动或该账号今日无日推"}

    return {"ok": True, "data": songs[:limit], "engine": "cli-fallback"}


@app.get("/api/v1/playlists/user")
def playlists_user(uid: int = Query(0), limit: int = Query(100, ge=1, le=200)):
    """账户歌单（自建 + 收藏）。需登录。

    uid 传 0 时自动用当前登录账号的 id —— 让代理侧不必自己去解析账号。
    """
    gate = _login_or_error("playlists/user")
    if gate:
        return gate
    if not uid:
        try:
            # ⚠️ 必须用改名过的 ne_auth_detail()：裸写 auth_detail() 会 NameError，
            # 被 except 吞掉后 uid 恒为 0 → 返回 uid_unavailable。
            uid = int(ne_auth_detail().get("user_id") or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolve uid failed: %s: %s", type(exc).__name__, exc)
            uid = 0
    if not uid:
        return {"ok": False, "error": "uid_unavailable", "data": [],
                "hint": "无法确定当前账号 uid，请显式传 uid"}
    try:
        rows = ne_user_playlists(uid, offset=0, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("user_playlists failed uid=%s: %s: %s", uid, type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "data": []}
    return {"ok": True, "data": rows, "uid": uid, "engine": "in-process"}


@app.get("/api/v1/playlists/recommend")
def playlists_recommend():
    """网易云按账号口味的推荐歌单列表。需登录。"""
    gate = _login_or_error("playlists/recommend")
    if gate:
        return gate
    try:
        rows = ne_recommend_playlists()
    except Exception as exc:  # noqa: BLE001
        logger.warning("recommend_resource failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "data": []}
    return {"ok": True, "data": rows, "engine": "in-process"}


@app.get("/api/v1/playlists/toplists")
def playlists_toplists():
    """排行榜清单（63 个左右）。无需登录。"""
    try:
        rows = ne_toplists()
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_toplists failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "data": []}
    return {"ok": True, "data": rows, "count": len(rows), "engine": "in-process"}


@app.get("/api/v1/playlists/category")
def playlists_category(cat: str = Query("华语"), order: str = Query("hot"),
                       limit: int = Query(20, ge=1, le=50)):
    """分类歌单。无需登录。order ∈ {hot,new}，非法值一律按 hot。"""
    try:
        rows = ne_category_playlists(cat, order, limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("top_playlists failed cat=%s: %s: %s", cat, type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "data": []}
    return {"ok": True, "data": rows, "cat": cat, "order": order, "engine": "in-process"}


@app.get("/api/v1/playlists/categories")
def playlists_categories():
    """歌单分类目录 {大类: [子类]}，供管理页做下拉选择。无需登录。"""
    try:
        return {"ok": True, "data": ne_playlist_categories()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("playlist_catelogs failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "data": {}}


@app.get("/api/v1/playlists/newalbums")
def playlists_newalbums(limit: int = Query(20, ge=1, le=50)):
    """新碟上架（专辑维度）。无需登录。"""
    try:
        return {"ok": True, "data": ne_new_albums(limit), "engine": "in-process"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("new_albums failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "data": []}


@app.get("/api/v1/radio/fm")
def radio_fm(limit: int = Query(10, ge=1, le=20)):
    """私人FM 曲目（一次一批，需登录）。"""
    gate = _login_or_error("radio/fm")
    if gate:
        return gate
    try:
        rows = ne_personal_fm()
    except Exception as exc:  # noqa: BLE001
        logger.warning("personal_fm failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "data": []}
    return {"ok": True, "data": rows[:limit], "engine": "in-process"}


@app.get("/api/v1/playlist/{playlist_id}/tracks")
def playlist_tracks(playlist_id: int = Path(..., ge=1), limit: int = Query(300, ge=1, le=1000)):
    """歌单内的可播曲目。

    不用 CLI ``playlist show``：它经 dig_info，任一首取不到直链就把整个歌单清空。
    上游 playlist_songlist 返回的 trackIds 是 **dict 列表**（含 id/v/at），
    已在 netease_ext.playlist_track_ids_limited 里做了兼容。
    """
    try:
        ids = ne_playlist_track_ids(playlist_id, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("playlist_songlist failed id=%s: %s: %s",
                       playlist_id, type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "data": []}
    if not ids:
        return {"ok": True, "data": [], "playlist_id": playlist_id, "total_ids": 0,
                "engine": "in-process"}
    try:
        rows = ne_songs_by_ids(ids, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("songs_by_ids failed id=%s: %s: %s",
                       playlist_id, type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "data": []}
    return {"ok": True, "data": rows, "playlist_id": playlist_id,
            "total_ids": len(ids), "playable": len(rows), "engine": "in-process"}


@app.get("/api/v1/album/{album_id}/tracks")
def album_tracks(album_id: int = Path(..., ge=1), limit: int = Query(200, ge=1, le=1000)):
    """专辑内可播曲目（新碟口径用）。不用 CLI ``album``（同样经 dig_info）。"""
    try:
        rows = ne_album_songs(album_id, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("album_songs failed id=%s: %s: %s", album_id, type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "data": []}
    return {"ok": True, "data": rows, "album_id": album_id, "engine": "in-process"}


@app.get("/api/v1/song/{song_id}/best_url")
def song_best_url(song_id: int = Path(..., ge=1)):
    """该账号能拿到的**最高品质**直链。

    按 jymaster → hires → lossless → exhigh 逐档降级，命中的第一档即返回，
    并附 best_quality 说明实际档位。收藏落盘下载走这个端点。
    """
    try:
        info = ne_best_url_info(song_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("best_url_info failed id=%s: %s: %s", song_id, type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "data": {}}
    if not info:
        return {"ok": False, "error": "no_playable_quality", "data": {},
                "tried": list(BEST_QUALITY_CHAIN)}
    return {"ok": True, "data": info, "best_quality": info.get("best_quality"),
            "tried": list(BEST_QUALITY_CHAIN), "engine": "in-process"}


@app.post("/api/v1/song/{song_id}/like")
@app.get("/api/v1/song/{song_id}/like")
def song_like_toggle(song_id: int = Path(..., ge=1), like: bool = Query(True)):
    """收藏同步回网易云：加红心 / 取消红心。需登录。

    **这是对用户账号的写操作**，因此不静默失败：上游返回 False 或抛异常都如实
    回传 error 字段，让代理侧能记日志并告知用户。
    ``like=False``（取消红心）在 NEMbox 源码里从未被调用过，属未验证分支，
    这里同样如实回传结果而不做乐观假设。
    """
    gate = _login_or_error("song/like")
    if gate:
        return gate
    res = ne_song_like(song_id, like=like)
    if not res.get("ok"):
        logger.warning("song_like failed id=%s like=%s -> %s", song_id, like, res)
    return {"ok": bool(res.get("ok")), "data": res, "engine": "in-process"}


@app.get("/api/v1/channels/selftest")
def channels_selftest():
    """逐口径自检：每个推荐/歌单口径各自能不能取到数据、取到几条。

    这些口径分散在上游不同接口，登录要求与返回字段都不一致，出问题时必须能一眼
    看出**是哪一路挂了**，而不是只看到"歌单没出来"。管理页与诊断页共用。
    """
    logged_in = False
    try:
        logged_in = ne_check_is_logged_in()
    except Exception:  # noqa: BLE001
        logged_in = False

    report: dict[str, Any] = {"logged_in": logged_in, "best_quality_chain": list(BEST_QUALITY_CHAIN)}

    def probe(name, fn, needs_login=False):
        if needs_login and not logged_in:
            report[name] = {"skipped": "not_logged_in"}
            return
        try:
            rows = fn()
            n = len(rows) if isinstance(rows, (list, dict)) else 0
            extra = ""
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                first = rows[0]
                extra = str(first.get("name") or first.get("song_name") or "")[:24]
            report[name] = {"ok": True, "count": n, "sample": extra}
        except Exception as exc:  # noqa: BLE001
            report[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}

    def _probe_user_playlists():
        # uid 必须解析成真实账号 id，传 0 上游只会给出无意义结果
        uid = int((ne_auth_detail() or {}).get("user_id") or 0)
        if not uid:
            raise RuntimeError("uid_unavailable: 无法确定当前登录账号 id")
        return ne_user_playlists(uid)

    probe("toplists", ne_toplists)
    probe("category", lambda: ne_category_playlists("华语", "hot", 5))
    probe("categories", ne_playlist_categories)
    probe("new_albums", lambda: ne_new_albums(5))
    probe("user_playlists", _probe_user_playlists, needs_login=True)
    probe("recommend_playlists", ne_recommend_playlists, needs_login=True)
    probe("personal_fm", ne_personal_fm, needs_login=True)
    return {"ok": True, "data": report}
