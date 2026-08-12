from __future__ import annotations

import json
import types

from fastapi.testclient import TestClient

from app import launcher
from app.main import create_app
from app.routes import actions as actions_mod

from conftest import make_old
from factory import SID, ai_title_line, user_line, write_jsonl

PROJ = "C--Work-proj"


def make_home(cfg):
    return write_jsonl(
        cfg.projects_root / PROJ / f"{SID}.jsonl",
        user_line(text="待归档的会话", ts="2026-08-10T00:00:00.000Z"),
        ai_title_line("动作测试会话"),
    )


def test_resume_route(cfg, monkeypatch, tmp_path):
    make_home(cfg)
    calls = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(launcher.shutil, "which", lambda name: r"C:\fake\wt.exe")
    app = create_app(cfg)
    with TestClient(app) as c:
        app.state.indexer.scan_once()
        # cwd(C:\Work\proj)不存在 → 409 cwd_missing
        r = c.post(f"/api/sessions/{SID}/resume", json={})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "cwd_missing"
        assert calls == []
        # 降级到主目录 → 拉起
        r = c.post(f"/api/sessions/{SID}/resume", json={"use_home_fallback": True})
        assert r.status_code == 200 and r.json()["used_wt"] is True
        assert len(calls) == 1


def test_resume_blocked_when_session_running(cfg, monkeypatch):
    """已在窗口中打开的会话禁止 resume(CC 检测并发后会直接退出,2026-08-12 实测)。"""
    import os
    import time

    import psutil

    make_home(cfg)
    calls = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(launcher.shutil, "which", lambda name: r"C:\fake\wt.exe")

    me = os.getpid()
    create = psutil.Process(me).create_time()
    ft = str(int((create + 11_644_473_600) * 1e7))
    sess_dir = cfg.claude_home_path / "sessions"
    sess_dir.mkdir(exist_ok=True)
    now_ms = int(time.time() * 1000)
    (sess_dir / f"{me}.json").write_text(
        json.dumps(
            {
                "pid": me, "sessionId": SID, "cwd": r"C:\Work\proj", "procStart": ft,
                "status": "idle", "name": "open-window", "updatedAt": now_ms,
                "statusUpdatedAt": now_ms,
            }
        ),
        encoding="utf-8",
    )
    app = create_app(cfg)
    with TestClient(app) as c:
        app.state.indexer.scan_once()
        r = c.post(f"/api/sessions/{SID}/resume", json={"use_home_fallback": True})
        assert r.status_code == 409
        assert "open-window" in r.json()["detail"]
        assert calls == []  # 绝不拉起


def test_archive_restore_roundtrip_via_api(cfg):
    p = make_home(cfg)
    app = create_app(cfg)
    with TestClient(app) as c:
        app.state.indexer.scan_once()
        # 手动立即归档(无视安静期)
        r = c.post(f"/api/sessions/{SID}/archive", json={})
        assert r.status_code == 200
        archived = cfg.archive_projects_root / PROJ / f"{SID}.jsonl"
        assert archived.is_file()

        # 模拟官方清理删源 → 详情走归档副本
        p.unlink()
        app.state.indexer.scan_once()
        r = c.get(f"/api/sessions/{SID}/messages")
        assert r.json()["source"] == "archive"

        # 还原:文件字节一致,再 resume 可用
        r = c.post(f"/api/sessions/{SID}/restore", json={"confirm": True})
        assert r.status_code == 200
        assert p.read_bytes() == archived.read_bytes()
        # 还原后源在,重复还原被拒
        r = c.post(f"/api/sessions/{SID}/restore", json={"confirm": True})
        assert r.status_code == 409

        # source_missing 状态下 resume 被拒(用另一个从未有源的假会话验证 404 分支足矣)
        r = c.post("/api/sessions/00000000-0000-0000-0000-000000000000/resume", json={})
        assert r.status_code == 404


def test_purge_archives_first_then_calls_official(cfg, monkeypatch):
    make_home(cfg)
    make_old(cfg.projects_root / PROJ / f"{SID}.jsonl", minutes=1)
    ran = {}

    def fake_run(args, **kw):
        ran["args"] = args
        # 模拟官方 purge:删掉项目目录下的 transcript
        for f in (cfg.projects_root / PROJ).glob("*.jsonl"):
            f.unlink()
        return types.SimpleNamespace(returncode=0, stdout="Purged 1 session\n", stderr="")

    monkeypatch.setattr(actions_mod.subprocess, "run", fake_run)
    app = create_app(cfg)
    with TestClient(app) as c:
        app.state.indexer.scan_once()
        # 确认名不匹配 → 400,官方命令未跑
        r = c.post("/api/projects/purge", json={"path": r"C:\Work\proj", "confirm_name": "wrong"})
        assert r.status_code == 400 and "args" not in ran

        r = c.post("/api/projects/purge", json={"path": r"C:\Work\proj", "confirm_name": "proj"})
        assert r.status_code == 200
        body = r.json()
        assert body["archived_before_purge"] == 1  # 先归档成功才执行
        assert (cfg.archive_projects_root / PROJ / f"{SID}.jsonl").is_file()
        assert ran["args"][0] == cfg.claude_exe
        assert ran["args"][1:4] == ["project", "purge", r"C:\Work\proj"]
        assert "--yes" in ran["args"]


def test_config_put_and_rebuild(cfg):
    make_home(cfg)
    app = create_app(cfg)
    with TestClient(app) as c:
        app.state.indexer.scan_once()
        r = c.put("/api/config", json={"scan_interval_seconds": 120, "index_thinking": True})
        assert r.status_code == 200
        body = r.json()
        assert set(body["changed"]) == {"scan_interval_seconds", "index_thinking"}
        assert "重启" in body["note"] and "重建索引" in body["note"]
        saved = json.loads(open(cfg.config_path, encoding="utf-8").read())
        assert saved["scan_interval_seconds"] == 120
        assert "config_path" not in saved

        r = c.put("/api/config", json={"claude_home": "C:\\evil"})
        assert r.status_code == 400

        r = c.post("/api/index/rebuild", json={"confirm": True})
        assert r.status_code == 200
        # 重建后台重扫;拿锁同步再扫一轮,数据应回来
        app.state.indexer.scan_once()
        assert c.get("/api/sessions").json()["total"] >= 1
