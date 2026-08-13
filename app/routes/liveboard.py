"""状态看板:运行中会话 / 后台作业 / 磁盘 / token 曲线 / plans。"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query, Request

from ..live import read_jobs, read_live_sessions
from ..quota import quota_report
from ..stats import cost_curve, disk_stats, plans_list, project_disk, usage_curve
from . import request_db

router = APIRouter(prefix="/api")


@router.get("/live")
def live(
    request: Request,
    show_stale: bool = False,
    con: sqlite3.Connection = Depends(request_db),
):
    data = read_live_sessions(request.app.state.cfg, include_stale=show_stale)
    for s in data["sessions"]:
        sid = (s.get("session_id") or "").lower()
        row = con.execute("SELECT title FROM sessions WHERE session_id=?", (sid,)).fetchone()
        s["title"] = row["title"] if row else None
    return data


@router.get("/jobs")
def jobs(request: Request):
    return read_jobs(request.app.state.cfg)


@router.get("/quota")
def quota(con: sqlite3.Connection = Depends(request_db)):
    return quota_report(con)


@router.get("/stats/disk")
def stats_disk(request: Request, con: sqlite3.Connection = Depends(request_db)):
    return {**disk_stats(request.app.state.cfg), "projects": project_disk(con)}


@router.get("/stats/tokens")
def stats_tokens(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    con: sqlite3.Connection = Depends(request_db),
):
    return {
        "days": days,
        "usage": usage_curve(con, days),
        "cost": cost_curve(request.app.state.cfg, days),
    }


@router.get("/plans")
def plans(request: Request, con: sqlite3.Connection = Depends(request_db)):
    return {"items": plans_list(request.app.state.cfg, con)}
