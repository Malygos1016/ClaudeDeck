"""状态看板:运行中会话 / 后台作业 / 磁盘 / token 曲线 / plans。"""
from __future__ import annotations

import sqlite3
import subprocess

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..focus import focus_group
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
    """把该会话所在的终端窗口/标签拉到前台。

    三层定位(见 app/focus.py 模块注释):优先按进程身份(OS 查询 + 标题标记),
    与 CC 版本、fork、tag 全部解耦;身份链走不通才落标题匹配兜底,且多命中
    如实报歧义。组内成员逐个尝试:先点击的 sid 自己的实例(同 sid 多实例时
    新进程优先 —— fork 后 resume 出的那个),再组里其他成员。
    """
    cfg = request.app.state.cfg
    group = _find_group(cfg, con, sid)
    if group is None:
        raise HTTPException(404, "该会话不在运行中。")
    if not group["has_window"]:
        raise HTTPException(
            409, "这是没有窗口的后台作业,用「接管」在新终端里打开它。"
        )
    want = sid.lower()
    # 主序:点谁先试谁。次序:其余成员新进程优先 —— 纯防御,build_groups 里
    # _dedup_by_session 已把同 sid 去重到一条,这里通常只是稳定排序的兜底。
    members = sorted(
        group["members"],
        key=lambda m: (
            (m.get("session_id") or "").lower() != want,
            -_started_epoch_s(m),
        ),
    )
    # 编排全在 focus_group(专用 UIA 线程):快路径标题匹配(缓存热时亚秒,
    # 唯一命中直接用)→ 零命中/歧义升级到标记法(零歧义精确定位)→ 全败时
    # 沿用快路径的失败原因 —— 歧义名单比笼统的"没找到"有用。
    res = focus_group([m.get("pid") for m in members], _name_candidates(group))
    if res["ok"]:
        return res
    if res.get("ambiguous"):
        raise HTTPException(
            409, "多个终端标签同名,无法确定该跳哪个: " + " / ".join(res["ambiguous"])
        )
    if res.get("marker_suppressed"):
        # 会话找到了、标记也写进控制台了,是 WT 标签不显示应用标题(手动
        # 重命名会锁题)。指路解锁而不是装作没找到。
        hint = (res.get("console_title") or "").strip()
        raise HTTPException(
            409, "找到了该会话的终端,但它的标签被手动重命名锁定,程序改不动"
                 "标题、也就定位不到具体标签。解锁:在 WT 里双击那个标签,"
                 "清空名字后回车,恢复自动标题即可一键跳转。"
                 + (f"(该会话想显示的标题是「{hint}」)" if hint else "")
        )
    raise HTTPException(
        404, "没找到该会话的终端窗口(可能已关闭,或跑在第三方终端里);"
             "可在详情页复制 resume 命令手动打开。"
    )


def _started_epoch_s(m: dict) -> float:
    """成员的启动时刻,epoch 秒(仅作排序 key);解析不了当 0。"""
    v = m.get("started_at")
    if not v:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


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


@router.post("/jobs/{job_id}/stop")
def stop_job(job_id: str, request: Request):
    """停止一个后台作业(官方 `claude stop`)。

    attach 只是观看,关掉观看窗口守护进程照常活着(实测,by design)——
    要让它真正退场只有 stop。transcript 完好保留,之后随时可正常 resume。
    job_id 必须在当前作业列表里,顺带杜绝任意参数拼进命令行。
    """
    cfg = request.app.state.cfg
    job = next(
        (j for j in read_jobs(cfg)["jobs"] if j.get("job_id") == job_id),
        None,
    )
    if job is None:
        raise HTTPException(404, "没有这个后台作业。")
    proc = subprocess.run(
        [cfg.claude_exe, "stop", job_id],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    if proc.returncode != 0:
        raise HTTPException(
            500, f"claude stop 退出码 {proc.returncode}: {(proc.stderr or proc.stdout)[:500]}"
        )
    return {"ok": True, "stopped": job_id, "stdout": (proc.stdout or "").strip()[:200]}


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
