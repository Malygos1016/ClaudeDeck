from __future__ import annotations

import json
import os
import time

import psutil

from app.live import filetime_to_epoch, read_jobs, read_live_sessions

FT_DELTA = 11_644_473_600


def ft(epoch_s: float) -> str:
    return str(int((epoch_s + FT_DELTA) * 1e7))


def write_entry(cfg, pid, *, proc_start=None, status="busy", updated_ms=None, name="w-1"):
    sess_dir = cfg.claude_home_path / "sessions"
    sess_dir.mkdir(exist_ok=True)
    now_ms = int(time.time() * 1000)
    d = {
        "pid": pid,
        "sessionId": f"00000000-0000-0000-0000-{pid:012d}",
        "cwd": r"C:\Work\proj",
        "startedAt": now_ms - 60_000,
        "procStart": proc_start,
        "version": "2.1.228",
        "kind": "interactive",
        "name": name,
        "nameSource": "derived",
        "status": status,
        "updatedAt": updated_ms or now_ms,
        "statusUpdatedAt": updated_ms or now_ms,
        "bridgeSessionId": "session_01TEST",
    }
    (sess_dir / f"{pid}.json").write_text(json.dumps(d), encoding="utf-8")
    return d


def test_filetime_roundtrip():
    now = time.time()
    assert abs(filetime_to_epoch(int(ft(now))) - now) < 0.001


def test_alive_by_procstart_match(cfg):
    me = os.getpid()
    create = psutil.Process(me).create_time()
    write_entry(cfg, me, proc_start=ft(create), name="alive-w")
    res = read_live_sessions(cfg)
    assert res["degraded"] is False
    assert len(res["sessions"]) == 1
    s = res["sessions"][0]
    assert s["alive"] and s["alive_check"] == "procstart-match"
    assert s["name"] == "alive-w"
    assert s["bridge_url"] == "https://claude.ai/code/session_01TEST"


def test_pid_reused_and_exited_are_stale(cfg):
    me = os.getpid()
    create = psutil.Process(me).create_time()
    write_entry(cfg, me, proc_start=ft(create), name="good")
    write_entry(cfg, 4, proc_start=ft(create - 9999), name="reused")  # pid 4=System,必不匹配
    write_entry(cfg, 999_999_999, proc_start=ft(create), name="gone")
    res = read_live_sessions(cfg)
    assert [s["name"] for s in res["sessions"]] == ["good"]
    assert res["stale_count"] == 2
    res2 = read_live_sessions(cfg, include_stale=True)
    assert len(res2["sessions"]) == 3
    checks = {s["name"]: s["alive_check"] for s in res2["sessions"]}
    assert checks["gone"] == "exited"
    assert checks["reused"] in ("pid-reused", "access-denied")


def test_degraded_mode_falls_back_to_freshness(cfg):
    me = os.getpid()
    create = psutil.Process(me).create_time()
    # 唯一可判定条目换算不匹配 → 降级;新鲜度兜底判活
    write_entry(cfg, me, proc_start=ft(create + 3600), name="fresh", updated_ms=int(time.time() * 1000))
    res = read_live_sessions(cfg)
    assert res["degraded"] is True
    assert len(res["sessions"]) == 1 and res["sessions"][0]["alive"]
    # 降级下,状态陈旧(>24h)的条目不算活
    write_entry(cfg, me, proc_start=ft(create + 3600), name="old", updated_ms=int(time.time() * 1000) - 25 * 3600 * 1000)
    res2 = read_live_sessions(cfg)
    names = [s["name"] for s in res2["sessions"]]
    assert "old" not in names


def test_waiting_sorted_first(cfg):
    me = os.getpid()
    create = psutil.Process(me).create_time()
    sess_dir = cfg.claude_home_path / "sessions"
    sess_dir.mkdir(exist_ok=True)
    now_ms = int(time.time() * 1000)
    for i, (status, name) in enumerate([("idle", "i-1"), ("busy", "b-1"), ("waiting", "w-1")]):
        d = {
            "pid": me,
            "sessionId": f"00000000-0000-0000-0000-{i:012d}",
            "cwd": r"C:\Work\proj",
            "procStart": ft(create),
            "status": status,
            "name": name,
            "updatedAt": now_ms,
            "statusUpdatedAt": now_ms,
        }
        (sess_dir / f"9000{i}.json").write_text(json.dumps(d), encoding="utf-8")
    res = read_live_sessions(cfg)
    assert [s["status"] for s in res["sessions"]] == ["waiting", "busy", "idle"]


def test_jobs_blocked_first(cfg):
    jobs = cfg.claude_home_path / "jobs"
    (jobs / "aaa11111").mkdir(parents=True)
    (jobs / "bbb22222").mkdir(parents=True)
    (jobs / "aaa11111" / "state.json").write_text(
        json.dumps(
            {
                "state": "working",
                "name": "画图作业",
                "updatedAt": "2026-08-11T10:00:00.000Z",
                "sessionId": "s-a",
            }
        ),
        encoding="utf-8",
    )
    (jobs / "bbb22222" / "state.json").write_text(
        json.dumps(
            {
                "state": "blocked",
                "name": "等待裁决",
                "needs": "V5.0 needs ~275 words cut",
                "updatedAt": "2026-08-10T10:00:00.000Z",
                "sessionId": "s-b",
                "forkParentSessionId": "s-parent",
            }
        ),
        encoding="utf-8",
    )
    res = read_jobs(cfg)
    assert [j["state"] for j in res["jobs"]] == ["blocked", "working"]
    assert res["jobs"][0]["needs"].startswith("V5.0")
    assert res["jobs"][0]["fork_parent_session_id"] == "s-parent"


def test_attach_viewers_scans_cmdline(monkeypatch):
    """attach 查看器识别:claude.exe + argv[1]=='attach' + 有终端祖先。
    查看器不进会话注册表(2026-08-26 实测),只能按进程表扫。"""
    from app import live as live_mod

    class P:
        def __init__(self, pid, name, cl):
            self.pid = pid
            self.info = {"name": name, "pid": pid}
            self._cl = cl

        def cmdline(self):
            return self._cl

    procs = [
        P(1, "claude.exe", ["claude.exe", "attach", "abc12345"]),
        P(2, "claude.exe", ["claude.exe", "--resume", "x"]),
        P(3, "claude.exe", ["claude.exe", "attach", "noterm00"]),  # 无终端,不算
        P(4, "other.exe", ["other.exe", "attach", "zzzz1111"]),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: procs)
    monkeypatch.setattr(live_mod, "has_terminal_ancestor", lambda pid: pid == 1)
    assert live_mod.attach_viewers() == {"abc12345": 1}


def test_has_terminal_ancestor_is_cached_per_process_lifetime(monkeypatch):
    """同一 (pid, 创建时间) 只爬一次祖先链;PID 复用给新进程(创建时间不同)重算。
    每次轮询逐会话爬链 ≈300ms,是服务常驻高 CPU 的元凶(2026-08-26 实测)。"""
    from app import live as live_mod

    live_mod._TERMINAL_CACHE.clear()
    walks: list[int] = []

    class FakeProc:
        def __init__(self, pid, ct):
            self.pid, self._ct = pid, ct

        def create_time(self):
            return self._ct

    ct = {"v": 100.0}
    monkeypatch.setattr(psutil, "Process", lambda pid: FakeProc(pid, ct["v"]))
    monkeypatch.setattr(
        live_mod, "_walk_terminal_ancestry", lambda proc: (walks.append(proc.pid), True)[1]
    )
    assert live_mod.has_terminal_ancestor(4242) is True
    assert live_mod.has_terminal_ancestor(4242) is True
    assert walks == [4242]                      # 第二次命中缓存,没再爬
    ct["v"] = 200.0                             # 同 pid 被新进程复用
    assert live_mod.has_terminal_ancestor(4242) is True
    assert walks == [4242, 4242]                # 创建时间变了,重算
