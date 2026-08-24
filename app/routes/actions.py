"""动作类路由:拉起 / 立即归档 / 还原 / purge / 配置读写 / 索引重建。破坏性操作要求确认字段。"""
from __future__ import annotations

import dataclasses
import sqlite3
import subprocess
import threading
from pathlib import PureWindowsPath

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from .. import archive as archive_mod
from .. import stats as stats_mod
from ..launcher import CwdMissing, launch_resume
from ..grouping import FORK_MARK
from ..live import read_jobs, read_live_sessions
from ..tags import load_tags
from . import request_db

router = APIRouter(prefix="/api")


def _has_running_fork_child(cfg, sid: str) -> bool:
    """该会话有没有正在运行的 fork 子分支。

    有的话说明它的窗口已经被子分支占用,父分支在那个窗口里回不去,
    resume 另开一个窗口是唯一出路,不能按「已在窗口里打开」拦掉。
    """
    want = (sid or "").lower()
    live = {
        (s.get("session_id") or "").lower()
        for s in read_live_sessions(cfg)["sessions"]
    }
    for j in read_jobs(cfg)["jobs"]:
        parent = (j.get("fork_parent_session_id") or "").lower()
        child = (j.get("session_id") or "").lower()
        if parent == want and child in live:
            return True
    return False


def _recovered_branch_name(cfg, con: sqlite3.Connection, sid: str) -> str | None:
    """恢复 fork 父分支时给窗口起的显示名;非 fork 父分支返回 None(不改名)。

    fork 会把父会话的 ai-title 也改成带 ⑂ 的,不给个自己的名字,恢复出来的窗口
    就与子分支的窗口同名,聚焦按标题匹配必然跳错(用户实报)。取用户打的 tag,
    没有就取标题里叉子之前那截 —— 那才是这条分支本来的名字。
    """
    if not _has_running_fork_child(cfg, sid):
        return None
    key = sid.lower()
    name = (load_tags(cfg).get(key) or "").strip()
    if not name:
        row = con.execute("SELECT title FROM sessions WHERE session_id=?", (key,)).fetchone()
        name = ((row["title"] if row else "") or "").split(FORK_MARK)[0].strip()
    return name or None


def _srow(con: sqlite3.Connection, sid: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM sessions WHERE session_id=?", (sid.lower(),)).fetchone()
    if row is None:
        raise HTTPException(404, "库中没有该会话")
    return row


@router.post("/sessions/{sid}/resume")
def resume_session(
    sid: str,
    request: Request,
    fork: bool = Body(False, embed=True),
    use_home_fallback: bool = Body(False, embed=True),
    con: sqlite3.Connection = Depends(request_db),
):
    row = _srow(con, sid)
    provider = row["provider"] or "claude"
    if row["source_missing"]:
        raise HTTPException(
            409, "源 transcript 已被清理,resume 会找不到会话——先在归档页还原,再恢复。"
        )
    if provider == "claude" and not fork and not _has_running_fork_child(request.app.state.cfg, sid):
        # 已在某个窗口里打开的会话通常不必再 resume:那个窗口就在,直接用即可。
        # 例外是 fork 过的父会话 —— fork 在它的窗口里就地发生,窗口现在跑的是
        # 子分支,父分支在里面回不去了,必须允许另开一个(fork 的意义正是两条
        # 分支各自推进)。此时放行,判断见 _has_running_fork_child。
        # 注:旧注释称「CC 并发检测会让新实例直接退出」,2026-08-24 在 CC 2.1.241
        # 上实测已不复现,两个实例可以共存;此拦截现在只为避免无谓的重复窗口。
        for s in read_live_sessions(request.app.state.cfg)["sessions"]:
            if (s.get("session_id") or "").lower() == sid.lower():
                raise HTTPException(
                    409,
                    f"该会话已在窗口「{s.get('name') or s.get('pid')}」中打开(状态 {s.get('status')}),"
                    "直接用那个窗口即可;要并行探索可用 fork。",
                )
    try:
        return launch_resume(
            request.app.state.cfg,
            row["cwd"],
            sid.lower(),
            fork=fork,
            name=_recovered_branch_name(request.app.state.cfg, con, sid),
            use_home_fallback=use_home_fallback,
            provider=provider,
        )
    except FileNotFoundError as e:
        raise HTTPException(409, str(e))
    except CwdMissing as e:
        raise HTTPException(
            409,
            detail={"code": "cwd_missing", "cwd": e.cwd,
                    "message": "项目目录已不存在。可改在用户主目录打开(跨目录 resume 能找到会话,但工作目录不再指向项目)。"},
        )


@router.post("/sessions/{sid}/archive")
def archive_now(sid: str, request: Request, con: sqlite3.Connection = Depends(request_db)):
    row = _srow(con, sid)
    if (row["provider"] or "claude") != "claude":
        raise HTTPException(409, "Codex 会话不参与 ClaudeDeck 归档(其目录由 codex 自行管理)。")
    try:
        res = request.app.state.indexer.force_archive(sid.lower())
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    stats_mod.invalidate_cache()
    return res


@router.post("/sessions/{sid}/restore")
def restore_session_route(
    sid: str,
    request: Request,
    confirm: bool = Body(..., embed=True),
    con: sqlite3.Connection = Depends(request_db),
):
    if not confirm:
        raise HTTPException(400, "需要 confirm: true")
    row = _srow(con, sid)
    cfg = request.app.state.cfg
    try:
        live = archive_mod.restore_session(cfg, cfg.projects_root, row["proj_dir"], sid.lower())
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    threading.Thread(
        target=request.app.state.indexer.scan_once, name="claudedeck-post-restore", daemon=True
    ).start()
    return {
        "ok": True,
        "restored_to": str(live),
        "note": "已还原。注意:官方 30 天清理在 CC 下次启动仍会执行,尽快 resume 或调大 cleanupPeriodDays。",
    }


@router.post("/projects/purge")
def purge_project(
    request: Request,
    path: str = Body(..., embed=True),
    confirm_name: str = Body(..., embed=True),
    con: sqlite3.Connection = Depends(request_db),
):
    cfg = request.app.state.cfg
    leaf = PureWindowsPath(path).name or path
    if confirm_name.strip() != leaf:
        raise HTTPException(400, f"确认名不匹配:请输入项目目录名「{leaf}」")

    rows = con.execute(
        "SELECT session_id FROM sessions WHERE cwd=? AND source_missing=0", (path,)
    ).fetchall()
    # 先强制归档全部会话,任何一个失败都中止 purge
    archived = []
    for r in rows:
        try:
            archived.append(request.app.state.indexer.force_archive(r["session_id"]))
        except FileNotFoundError:
            continue  # 索引滞后:文件已不在,无需归档
        except Exception as e:
            raise HTTPException(500, f"归档 {r['session_id'][:8]} 失败,已中止 purge: {e!r}")

    proc = subprocess.run(
        [cfg.claude_exe, "project", "purge", path, "--yes"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    threading.Thread(
        target=request.app.state.indexer.scan_once, name="claudedeck-post-purge", daemon=True
    ).start()
    stats_mod.invalidate_cache()
    if proc.returncode != 0:
        raise HTTPException(
            500, f"claude project purge 退出码 {proc.returncode}: {proc.stderr[:2000]}"
        )
    return {
        "ok": True,
        "archived_before_purge": len(archived),
        "stdout": proc.stdout[-4000:],
    }


@router.put("/sessions/{sid}/tag")
def put_tag(
    sid: str,
    request: Request,
    tag: str | None = Body(None, embed=True),
    con: sqlite3.Connection = Depends(request_db),
):
    """给会话打自定义标签(空值=清除)。存 tags.json,重建索引不丢。"""
    _srow(con, sid)
    from ..tags import set_tag

    set_tag(request.app.state.cfg, sid, tag)
    clean = (tag or "").strip()[:60]
    return {"ok": True, "tag": clean or None}


# ---------- 配置 ----------

MUTABLE_KEYS = {
    "archive_dir", "scan_interval_seconds", "archive_quiet_minutes",
    "live_poll_ms", "port", "claude_exe", "index_thinking", "index_tool_results",
}


@router.get("/config")
def get_config(request: Request):
    return dataclasses.asdict(request.app.state.cfg)


@router.put("/config")
def put_config(request: Request, body: dict = Body(...)):
    cfg = request.app.state.cfg
    unknown = set(body) - MUTABLE_KEYS
    if unknown:
        raise HTTPException(400, f"不可修改的键: {sorted(unknown)}")
    changed = []
    for k, v in body.items():
        if getattr(cfg, k) != v:
            setattr(cfg, k, v)
            changed.append(k)
    cfg.save()
    notes = []
    if {"archive_dir", "scan_interval_seconds", "archive_quiet_minutes", "port"} & set(changed):
        notes.append("扫描/端口类改动需重启 ClaudeDeck 生效。")
    if {"index_thinking", "index_tool_results"} & set(changed):
        notes.append("索引范围改动需「重建索引」后生效。")
    if "archive_dir" in changed:
        notes.append("旧归档目录不会自动迁移,请手动移动后再删除。")
    return {"ok": True, "changed": changed, "note": " ".join(notes) or None}


@router.post("/index/rebuild")
def rebuild_index(request: Request, confirm: bool = Body(..., embed=True)):
    if not confirm:
        raise HTTPException(400, "需要 confirm: true")
    idx = request.app.state.indexer
    idx.rebuild()
    threading.Thread(target=idx.scan_once, name="claudedeck-rebuild-scan", daemon=True).start()
    return {"ok": True, "note": "索引已清空,正在后台全量重扫。"}
