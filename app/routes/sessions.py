"""会话列表 / 搜索 / 详情 / 聊天视图消息 / 导出 / resume 命令。"""
from __future__ import annotations

import html
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from ..codex import parse_codex_view_items
from ..launcher import build_resume_command
from ..live import read_live_sessions
from ..render import export_markdown, parse_view_items, resolve_transcript_path, window
from ..search import MARK_L, MARK_R, search as do_search
from ..transcript import bridge_url
from . import request_db

router = APIRouter(prefix="/api")

ALLOWED_SORT = {"last_ts", "first_ts", "file_size", "msg_count", "title"}


def _like_quick(tok: str) -> str:
    esc = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def snippet_to_html(snip: str) -> str:
    """先整体转义,再把哨兵换成 <mark>——除 mark 外不放行任何 HTML。"""
    return html.escape(snip, quote=False).replace(MARK_L, "<mark>").replace(MARK_R, "</mark>")


def _session_out(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["bridge_url"] = bridge_url(d.get("bridge_session_id"))
    d["archived"] = bool(d.get("archived_at"))
    return d


@router.get("/sessions")
def list_sessions(
    request: Request,
    q: str | None = None,
    project: str | None = None,
    provider: str = Query("all", pattern="^(all|claude|codex)$"),
    archived: str = Query("all", pattern="^(all|live|missing)$"),
    sort: str = "last_ts",
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    con: sqlite3.Connection = Depends(request_db),
):
    if sort not in ALLOWED_SORT:
        raise HTTPException(400, f"不支持的排序字段: {sort}")
    where, params = ["1=1"], []
    if q and q.strip():
        pat = _like_quick(q.strip())
        where.append(
            "(title LIKE ? ESCAPE '\\' OR last_prompt LIKE ? ESCAPE '\\'"
            " OR session_id LIKE ? ESCAPE '\\' OR cwd LIKE ? ESCAPE '\\')"
        )
        params += [pat, pat, pat, pat]
    if project:
        where.append("cwd = ?")
        params.append(project)
    if provider != "all":
        where.append("COALESCE(provider, 'claude') = ?")
        params.append(provider)
    if archived == "live":
        where.append("source_missing = 0")
    elif archived == "missing":
        where.append("source_missing = 1")
    cond = " AND ".join(where)

    total = con.execute(f"SELECT COUNT(*) FROM sessions WHERE {cond}", params).fetchone()[0]
    rows = con.execute(
        f"SELECT * FROM sessions WHERE {cond} "
        f"ORDER BY ({sort} IS NULL), {sort} {'ASC' if order == 'asc' else 'DESC'} "
        "LIMIT ? OFFSET ?",
        params + [page_size, (page - 1) * page_size],
    ).fetchall()
    running = _running_sids(request)
    items = []
    for r in rows:
        d = _session_out(r)
        d["running"] = d["session_id"] in running
        items.append(d)
    return {"total": total, "page": page, "page_size": page_size, "items": items}


def _running_sids(request: Request) -> set[str]:
    try:
        data = read_live_sessions(request.app.state.cfg)
        return {(s.get("session_id") or "").lower() for s in data["sessions"]}
    except Exception:
        return set()


@router.get("/projects")
def list_projects(con: sqlite3.Connection = Depends(request_db)):
    rows = con.execute(
        "SELECT cwd, COUNT(*) AS sessions, COALESCE(SUM(file_size),0) AS bytes,"
        " MAX(last_ts) AS last_ts FROM sessions GROUP BY cwd ORDER BY last_ts DESC"
    ).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.get("/search")
def search_endpoint(
    q: str,
    project: str | None = None,
    limit: int = Query(200, ge=1, le=500),
    con: sqlite3.Connection = Depends(request_db),
):
    res = do_search(con, q, project=project, limit=limit)
    for g in res["groups"]:
        for h in g["hits"]:
            h["snippet_html"] = snippet_to_html(h.pop("snippet"))
    return res


@router.get("/sessions/{sid}")
def session_detail(
    sid: str, request: Request, con: sqlite3.Connection = Depends(request_db)
):
    row = con.execute("SELECT * FROM sessions WHERE session_id=?", (sid.lower(),)).fetchone()
    if row is None:
        raise HTTPException(404, "库中没有该会话,先扫描?")
    subagents = [
        dict(r)
        for r in con.execute(
            "SELECT agent_id, agent_type, description, tool_use_id, spawn_depth, file_size"
            " FROM subagents WHERE session_id=? ORDER BY agent_id",
            (sid.lower(),),
        ).fetchall()
    ]
    cfg = request.app.state.cfg
    plan = None
    if row["slug"]:
        p = cfg.claude_home_path / "plans" / f"{row['slug']}.md"
        if p.is_file():
            plan = {"slug": row["slug"], "path": str(p)}
    out = _session_out(row)
    out["running"] = out["session_id"] in _running_sids(request)
    return {"session": out, "subagents": subagents, "plan": plan}


def _session_row(con: sqlite3.Connection, sid: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM sessions WHERE session_id=?", (sid.lower(),)).fetchone()
    if row is None:
        raise HTTPException(404, "库中没有该会话,先扫描?")
    return row


def _resolved_path(
    request: Request, row: sqlite3.Row, con: sqlite3.Connection
) -> tuple[Path, str]:
    if (row["provider"] or "claude") == "codex":
        f = con.execute(
            "SELECT path FROM files WHERE session_id=? LIMIT 1", (row["session_id"],)
        ).fetchone()
        if f is not None and Path(f["path"]).is_file():
            return Path(f["path"]), "live"
        raise HTTPException(410, "Codex 源文件已不存在(可能被 codex 自身归档或清理)")
    res = resolve_transcript_path(request.app.state.cfg, row["proj_dir"], row["session_id"])
    if res is None:
        raise HTTPException(410, "该会话的源文件与归档副本都不存在(可能被清理且未及归档)")
    return res


def _view_items(row: sqlite3.Row, path: Path) -> list[dict]:
    if (row["provider"] or "claude") == "codex":
        return parse_codex_view_items(path)
    return parse_view_items(path)


@router.get("/sessions/{sid}/messages")
def session_messages(
    sid: str,
    request: Request,
    limit: int = Query(80, ge=1, le=400),
    around_seq: int | None = None,
    before_seq: int | None = None,
    after_seq: int | None = None,
    show_system: bool = False,
    con: sqlite3.Connection = Depends(request_db),
):
    row = _session_row(con, sid)
    path, source = _resolved_path(request, row, con)
    items = _view_items(row, path)
    win = window(
        items,
        limit=limit,
        around_seq=around_seq,
        before_seq=before_seq,
        after_seq=after_seq,
        show_system=show_system,
    )
    win["source"] = source
    return win


@router.get("/sessions/{sid}/subagents/{agent_id}/messages")
def subagent_messages(
    sid: str,
    agent_id: str,
    request: Request,
    limit: int = Query(200, ge=1, le=400),
    before_seq: int | None = None,
    show_system: bool = False,
    con: sqlite3.Connection = Depends(request_db),
):
    arow = con.execute(
        "SELECT * FROM subagents WHERE agent_id=? AND session_id=?", (agent_id, sid.lower())
    ).fetchone()
    if arow is None or not arow["file_path"]:
        raise HTTPException(404, "没有该子 agent 的记录")
    cfg = request.app.state.cfg
    path = Path(arow["file_path"])
    source = "live"
    if not path.is_file():
        # 源被清理:把 projects 根前缀换成归档根,尝试归档副本
        try:
            rel = path.relative_to(cfg.projects_root)
            path = cfg.archive_projects_root / rel
            source = "archive"
        except ValueError:
            pass
    if not path.is_file():
        raise HTTPException(410, "子 agent transcript 的源文件与归档副本都不存在")
    items = parse_view_items(path)
    win = window(items, limit=limit, before_seq=before_seq, show_system=show_system)
    win["source"] = source
    return win


@router.get("/sessions/{sid}/export")
def export_session(
    sid: str,
    request: Request,
    con: sqlite3.Connection = Depends(request_db),
):
    row = _session_row(con, sid)
    path, _source = _resolved_path(request, row, con)
    items = _view_items(row, path)
    md = export_markdown(items, dict(row))
    fname = f"claude-session-{sid.lower()[:8]}.md"
    return PlainTextResponse(
        md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/sessions/{sid}/command")
def resume_command(
    sid: str,
    request: Request,
    fork: bool = False,
    con: sqlite3.Connection = Depends(request_db),
):
    row = con.execute(
        "SELECT cwd, source_missing, provider FROM sessions WHERE session_id=?", (sid.lower(),)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "库中没有该会话")
    cmd = build_resume_command(
        request.app.state.cfg,
        row["cwd"],
        sid.lower(),
        fork=fork,
        provider=row["provider"] or "claude",
    )
    return {
        "command": cmd,
        "source_missing": bool(row["source_missing"]),
        "note": "源已被清理的会话需先还原才能 resume" if row["source_missing"] else None,
    }
