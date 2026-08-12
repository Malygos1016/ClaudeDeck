"""运行中会话注册表(~/.claude/sessions/<PID>.json)与后台作业(jobs/)读取。

只读,绝不删除注册表文件(陈旧条目由 Claude Code 下次启动自清)。
procStart 是 Windows FILETIME(1601 纪元 100ns):
  epoch_s = ticks/1e7 - 11_644_473_600
本机双样本验证(与同文件 startedAt 差 <2s)。若换算假设在某环境失效
(所有活 pid 全部对不上),自动降级为「pid 存在 + 状态 24h 内新鲜」并打 degraded 标。
"""
from __future__ import annotations

import json
import time
from typing import Any

import psutil

from .config import Config
from .indexer import ms_to_iso
from .transcript import bridge_url

FILETIME_EPOCH_DELTA_S = 11_644_473_600
PROC_START_TOLERANCE_S = 5
STALE_FRESH_WINDOW_S = 24 * 3600


def filetime_to_epoch(ticks: int) -> float:
    return ticks / 1e7 - FILETIME_EPOCH_DELTA_S


def _probe_process(pid: Any, proc_start: Any) -> tuple[bool, bool | None, str]:
    """返回 (进程存在, procStart 匹配(None=无法判定), 说明)。"""
    if not isinstance(pid, int):
        return False, None, "no-pid"
    try:
        proc = psutil.Process(pid)
        create = proc.create_time()
    except psutil.NoSuchProcess:
        return False, None, "exited"
    except psutil.AccessDenied:
        return True, None, "access-denied"
    ticks_s = str(proc_start) if proc_start is not None else ""
    if not ticks_s.isdigit():
        return True, None, "no-procstart"
    expected = filetime_to_epoch(int(ticks_s))
    if abs(expected - create) <= PROC_START_TOLERANCE_S:
        return True, True, "procstart-match"
    return True, False, "pid-reused"


def read_live_sessions(cfg: Config, *, include_stale: bool = False) -> dict:
    sess_dir = cfg.claude_home_path / "sessions"
    entries: list[dict] = []
    if sess_dir.is_dir():
        for f in sorted(sess_dir.glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(d, dict):
                continue
            exists, matched, check = _probe_process(d.get("pid"), d.get("procStart"))
            entries.append({"raw": d, "exists": exists, "matched": matched, "check": check})

    # 降级判定:凡是"进程在且能读 procStart"的条目全都不匹配 → 换算假设可疑
    verdicts = [e["matched"] for e in entries if e["exists"] and e["matched"] is not None]
    degraded = bool(verdicts) and not any(verdicts)

    now_ms = time.time() * 1000
    sessions: list[dict] = []
    stale_count = 0
    for e in entries:
        d = e["raw"]
        updated = d.get("statusUpdatedAt") or d.get("updatedAt") or 0
        if degraded:
            alive = e["exists"] and (now_ms - updated) < STALE_FRESH_WINDOW_S * 1000
        else:
            alive = e["exists"] and e["matched"] is not False
        if not alive:
            stale_count += 1
            if not include_stale:
                continue
        started = d.get("startedAt")
        sessions.append(
            {
                "pid": d.get("pid"),
                "session_id": d.get("sessionId"),
                "name": d.get("name"),
                "name_source": d.get("nameSource"),
                "cwd": d.get("cwd"),
                "kind": d.get("kind"),
                "status": d.get("status"),
                "status_seconds": max(0.0, (now_ms - updated) / 1000) if updated else None,
                "started_at": ms_to_iso(started) if isinstance(started, (int, float)) else None,
                "version": d.get("version"),
                "bridge_url": bridge_url(d.get("bridgeSessionId")),
                "alive": alive,
                "alive_check": e["check"],
            }
        )
    # waiting=等待用户输入/授权,最需要被看见;实测 status 取值:busy/idle/waiting
    order = {"waiting": 0, "busy": 1, "idle": 2}
    sessions.sort(key=lambda s: (not s["alive"], order.get(s["status"], 3), s["name"] or ""))
    return {"sessions": sessions, "stale_count": stale_count, "degraded": degraded}


def read_jobs(cfg: Config) -> dict:
    jobs_dir = cfg.claude_home_path / "jobs"
    items: list[dict] = []
    if jobs_dir.is_dir():
        for sub in sorted(jobs_dir.iterdir()):
            state_file = sub / "state.json"
            if not sub.is_dir() or not state_file.is_file():
                continue
            try:
                d = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(d, dict):
                continue
            items.append(
                {
                    "id": sub.name,
                    "name": d.get("name"),
                    "state": d.get("state"),
                    "tempo": d.get("tempo"),
                    "detail": d.get("detail"),
                    "needs": d.get("needs"),
                    "intent": d.get("intent"),
                    "tokens": d.get("tokens"),
                    "cwd": d.get("cwd"),
                    "session_id": d.get("sessionId"),
                    "fork_parent_session_id": d.get("forkParentSessionId"),
                    "created_at": d.get("createdAt"),
                    "updated_at": d.get("updatedAt"),
                }
            )
    # blocked 置顶,其余按更新时间倒序
    blocked = [j for j in items if j["state"] == "blocked"]
    rest = sorted(
        (j for j in items if j["state"] != "blocked"),
        key=lambda j: j["updated_at"] or "",
        reverse=True,
    )
    return {"jobs": blocked + rest}
