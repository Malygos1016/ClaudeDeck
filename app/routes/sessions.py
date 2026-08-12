"""会话列表 / 搜索 / 详情 / resume 命令。"""
from __future__ import annotations

import html
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..launcher import build_resume_command
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
    q: str | None = None,
    project: str | None = None,
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
    return {"total": total, "page": page, "page_size": page_size, "items": [_session_out(r) for r in rows]}


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
    return {"session": _session_out(row), "subagents": subagents, "plan": plan}


@router.get("/sessions/{sid}/command")
def resume_command(
    sid: str,
    request: Request,
    fork: bool = False,
    con: sqlite3.Connection = Depends(request_db),
):
    row = con.execute(
        "SELECT cwd, source_missing FROM sessions WHERE session_id=?", (sid.lower(),)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "库中没有该会话")
    cmd = build_resume_command(request.app.state.cfg, row["cwd"], sid.lower(), fork=fork)
    return {
        "command": cmd,
        "source_missing": bool(row["source_missing"]),
        "note": "源已被清理的会话需先还原才能 resume" if row["source_missing"] else None,
    }
