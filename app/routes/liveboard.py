"""状态看板:运行中会话 / 后台作业 / 磁盘 / token 曲线 / plans。"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..focus import focus_session
from ..grouping import build_groups
from ..launcher import launch_attach
from ..live import read_jobs, read_live_sessions
from ..quota import quota_report
from ..stats import cost_curve, disk_stats, plans_list, project_disk, usage_curve
from ..tags import load_tags
from . import request_db

router = APIRouter(prefix="/api")


@router.get("/live")
def live(
    request: Request,
    show_stale: bool = False,
    con: sqlite3.Connection = Depends(request_db),
):
    cfg = request.app.state.cfg
    data = read_live_sessions(cfg, include_stale=show_stale)
    tags = load_tags(cfg)
    for s in data["sessions"]:
        sid = (s.get("session_id") or "").lower()
        row = con.execute("SELECT title FROM sessions WHERE session_id=?", (sid,)).fetchone()
        s["title"] = row["title"] if row else None
        s["tag"] = tags.get(sid)
    # 一个窗口一个格子:丢掉 spare 空壳、把 fork 父子合成一组(见 app/grouping.py)
    data["groups"] = build_groups(data["sessions"], read_jobs(cfg)["jobs"])
    return data


@router.post("/live/{sid}/focus")
def focus_live_session(
    sid: str,
    request: Request,
    con: sqlite3.Connection = Depends(request_db),
):
    """把该会话所在的 Windows Terminal 标签拉到前台。

    不再按 kind=="bg" 一刀拦掉:bg 只说明它由守护进程驱动,不代表没有窗口。
    fork 出的会话就是 bg,却与父会话共用一个真实窗口(2026-08-23 实测)。
    改为按分组判断有没有窗口,并拿全组的名字去匹配 —— fork 之后父会话的窗口
    标题会被改成子会话的名字,只拿自己的名字必然匹配不上。
    """
    cfg = request.app.state.cfg
    group = _find_group(cfg, con, sid)
    if group is None:
        raise HTTPException(404, "该会话不在运行中。")
    if not group["has_window"]:
        raise HTTPException(
            409, "这是没有窗口的后台作业,用「接管」在新终端里打开它。"
        )
    res = focus_session(_name_candidates(group))
    if not res["ok"]:
        raise HTTPException(
            404, "没找到对应的终端标签(标题可能刚刚变化或窗口已关),稍后再点一次。"
        )
    return res


def _hydrate(cfg, con) -> list[dict]:
    """给活跃会话补上 title / tag,分组与窗口匹配都要用。"""
    data = read_live_sessions(cfg)
    tags = load_tags(cfg)
    for s in data["sessions"]:
        key = (s.get("session_id") or "").lower()
        row = con.execute("SELECT title FROM sessions WHERE session_id=?", (key,)).fetchone()
        s["title"] = row["title"] if row else None
        s["tag"] = tags.get(key)
    return data["sessions"]


def _find_group(cfg, con, sid: str) -> dict | None:
    groups = build_groups(_hydrate(cfg, con), read_jobs(cfg)["jobs"])
    want = sid.lower()
    for g in groups:
        if any((m.get("session_id") or "").lower() == want for m in g["members"]):
            return g
    return None


def _name_candidates(group: dict) -> list[str]:
    """全组的名字变体。窗口标题可能是其中任意一个。"""
    out: list[str] = []
    for m in group["members"]:
        for key in ("tag", "title", "name"):
            v = (m.get(key) or "").strip()
            if v and v not in out:
                out.append(v)
    return out


@router.get("/jobs")
def jobs(request: Request):
    return read_jobs(request.app.state.cfg)


@router.post("/jobs/{job_id}/attach")
def attach_job(job_id: str, request: Request):
    """在新终端标签里接管一个没有窗口的后台作业(claude attach)。"""
    cfg = request.app.state.cfg
    job = next(
        (j for j in read_jobs(cfg)["jobs"] if j.get("job_id") == job_id),
        None,
    )
    if job is None:
        raise HTTPException(404, "没有这个后台作业。")
    return launch_attach(cfg, job.get("cwd"), job_id)


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
