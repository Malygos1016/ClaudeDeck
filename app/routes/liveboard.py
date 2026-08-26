"""状态看板:运行中会话 / 后台作业 / 磁盘 / token 曲线 / plans。"""
from __future__ import annotations

import sqlite3
import subprocess

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..focus import focus_group
from ..grouping import TERMINAL_JOB_STATES, build_groups
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
    sessions = _hydrate(cfg, con)
    groups = build_groups(sessions, read_jobs(cfg)["jobs"])
    want = sid.lower()
    group = next(
        (g for g in groups
         if any((m.get("session_id") or "").lower() == want for m in g["members"])),
        None,
    )
    if group is None:
        raise HTTPException(404, "该会话不在运行中。")
    if not group["has_window"]:
        raise HTTPException(
            409, "这是没有窗口的后台作业,用「接管」在新终端里打开它。"
        )
    # 主序:点谁先试谁。次序:其余成员新进程优先 —— 纯防御,build_groups 里
    # _dedup_by_session 已把同 sid 去重到一条,这里通常只是稳定排序的兜底。
    members = sorted(
        group["members"],
        key=lambda m: (
            (m.get("session_id") or "").lower() != want,
            -_started_epoch_s(m),
        ),
    )
    # roster = 全量注册表会话(含 spare/sdk-cli):窗内减法要知道"这扇窗里
    # 还有谁",按 owner 通道逐个实证成员资格(见 focus._subtract_in_window)。
    roster = [{"pid": s.get("pid"), "names": _session_names(s)} for s in sessions]
    # 窗口通道(见 app/focus.py 模块注释):会话 → WT 窗口是 OS 权威查询,
    # 窗口解析成功就必定 ok —— 最坏也是正确窗口置前 + tab_selected=False。
    res = focus_group([m.get("pid") for m in members], _name_candidates(group), roster)
    if res["ok"]:
        return res
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


def _session_names(s: dict) -> list[str]:
    """单个会话的名字变体(tag/title/name 非空去重)。窗口标题可能是其中任意一个。"""
    out: list[str] = []
    for key in ("tag", "title", "name"):
        v = (s.get(key) or "").strip()
        if v and v not in out:
            out.append(v)
    return out


def _name_candidates(group: dict) -> list[str]:
    """全组的名字变体,规则与 _session_names 同一份。"""
    out: list[str] = []
    for m in group["members"]:
        for v in _session_names(m):
            if v not in out:
                out.append(v)
    return out


@router.get("/jobs")
def jobs(request: Request):
    return read_jobs(request.app.state.cfg)


@router.post("/jobs/{job_id}/attach")
def attach_job(job_id: str, request: Request):
    """在新终端标签里接管一个没有窗口的后台作业(claude attach)。

    只对还在运行的作业开放:attach 对已终态的作业是"重启尝试",若有残留
    进程占着会话/管道,重生实例启动即死(2026-08-25 实报:对 state=stopped
    的作业 attach 弹出 "can't start — exit 1 before init",作业被改判 failed)。
    """
    cfg = request.app.state.cfg
    job = next(
        (j for j in read_jobs(cfg)["jobs"] if j.get("job_id") == job_id),
        None,
    )
    if job is None:
        raise HTTPException(404, "没有这个后台作业。")
    if (job.get("state") or "") in TERMINAL_JOB_STATES:
        raise HTTPException(
            409, f"该作业已结束(state={job.get('state')}),接管等于重启,"
                 "不在这里做。要继续这段对话请在会话页 resume;"
                 "有进程残留就用「清理」。"
        )
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
    # 官方停完复查:僵尸残留(作业已终态、worker 进程仍活)官方 stop 不会
    # 处理 —— 它眼里作业早停了。仍有活 worker 就升级强杀。
    killed = _kill_job_residue(cfg, job_id)
    if proc.returncode != 0 and not killed:
        raise HTTPException(
            500, f"claude stop 退出码 {proc.returncode}: {(proc.stderr or proc.stdout)[:500]}"
        )
    return {"ok": True, "stopped": job_id, "killed": killed,
            "official": (proc.stdout or proc.stderr or "").strip()[:200]}


def _kill_job_residue(cfg, job_id: str) -> list[int]:
    """杀掉作业的残留进程(worker + 它的 --bg-pty-host 父),返回杀掉的 pid。

    身份三重校验,宁可漏杀不可错杀:worker 必须是注册表里 jobId 对得上、
    且 pid+procStart 出生时间验活通过的(read_live_sessions 已做);父进程
    只有 cmdline 同时含 --bg-pty-host 与本 job id 才连带。transcript 不动,
    之后随时可正常 resume。
    """
    import psutil

    killed: list[int] = []
    for s in read_live_sessions(cfg)["sessions"]:
        if s.get("job_id") != job_id or not s.get("alive"):
            continue
        pid = s.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            continue
        try:
            p = psutil.Process(pid)
            parent = p.parent()
        except Exception:
            continue
        if _terminate(p):
            killed.append(pid)
        try:
            cl = " ".join(parent.cmdline() or []) if parent else ""
        except Exception:
            cl = ""
        if "--bg-pty-host" in cl and job_id in cl and _terminate(parent):
            killed.append(parent.pid)
    return killed


def _terminate(p) -> bool:
    """terminate → 3s → kill → 3s;确认死亡才算数。"""
    import psutil

    try:
        p.terminate()
        p.wait(timeout=3)
        return True
    except psutil.NoSuchProcess:
        return False
    except psutil.TimeoutExpired:
        try:
            p.kill()
            p.wait(timeout=3)
            return True
        except Exception:
            return False
    except Exception:
        return False


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
