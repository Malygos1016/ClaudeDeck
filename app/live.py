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


# 终端宿主进程名。会话进程往上追到其中之一,才算真的有窗口可跳。
TERMINAL_HOSTS = {
    "windowsterminal.exe",
    "conhost.exe",
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "alacritty.exe",
    "wezterm-gui.exe",
}
_ANCESTRY_MAX_DEPTH = 8


def has_terminal_ancestor(pid: Any) -> bool:
    """会话进程的祖先里有没有终端宿主。

    判断「有没有窗口」只能靠这个,不能靠 kind:kind=interactive 仅表示不是
    --bg 起的。EdgeTracer 用 `claude -p` 派生的会话同样是 interactive,
    却挂在 python.exe 下、压根没有终端(2026-08-23 用户实报的误判)。
    进程链是系统级事实,也不像窗口标题那样会被改写。
    """
    if not isinstance(pid, int):
        return False
    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    for _ in range(_ANCESTRY_MAX_DEPTH):
        try:
            proc = proc.parent()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        if proc is None:
            return False
        try:
            if proc.name().lower() in TERMINAL_HOSTS:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
    return False


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
                # cli=用户从终端开的;sdk-cli=脚本/SDK 派生的一次性会话
                "entrypoint": d.get("entrypoint"),
                # 有没有窗口以进程链为准,不是 kind(见 has_terminal_ancestor)
                "has_terminal": has_terminal_ancestor(d.get("pid")) if alive else False,
                # CC 预热的备用空壳:无对话内容,不该出现在任何界面上(2026-08-23 实测)
                "spare": bool(d.get("spare")),
                "job_id": d.get("jobId"),
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
                    "job_id": sub.name,   # attach 用的就是这个短 id(claude attach <id>)
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
                    # fork 发生的时刻:早于它启动的父实例,窗口已被子分支占用
                    "fork_boundary_at": d.get("forkBoundaryAt"),
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
