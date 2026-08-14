"""crush provider:库读取 / 虚拟路径增量 / API / 视图出自索引 / 归档拒绝。"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.crush import list_crush_projects, read_crush_db
from app.launcher import build_resume_command
from app.main import create_app

CRUSH_SID = "64fec551-e278-40a3-9409-837cf639fc0a"


def make_crush_project(cfg, cwd: str = r"C:\Work\proj") -> Path:
    data_dir = Path(cfg.crush_projects_json).parent / "proj_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = data_dir / "crush.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT,
          title TEXT NOT NULL, message_count INTEGER DEFAULT 0,
          prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
          cost REAL DEFAULT 0.0, updated_at INTEGER NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
          role TEXT NOT NULL, parts TEXT NOT NULL default '[]', model TEXT,
          created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
          finished_at INTEGER, provider TEXT);
        """
    )
    t0 = 1786586239  # 秒(实测 crush 存秒,schema 注释才说 ms)
    con.execute(
        "INSERT INTO sessions(id, title, message_count, prompt_tokens, completion_tokens,"
        " created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
        (CRUSH_SID, "托卡马克线圈建模", 2, 204809, 750, t0, t0 + 100),
    )
    def parts(text):
        return json.dumps([{"type": "text", "data": {"text": text}},
                           {"type": "finish", "data": {"reason": "stop"}}], ensure_ascii=False)
    con.execute(
        "INSERT INTO messages(id, session_id, role, parts, created_at, updated_at)"
        " VALUES('m1',?,?,?,?,?)", (CRUSH_SID, "user", parts("帮我建 D 型线圈白模"), t0, t0),
    )
    con.execute(
        "INSERT INTO messages(id, session_id, role, parts, created_at, updated_at)"
        " VALUES('m2',?,?,?,?,?)",
        (CRUSH_SID, "assistant", parts("用 **Blender** 从贝塞尔曲线起。"), t0 + 5, t0 + 5),
    )
    con.commit()
    con.close()

    pj = Path(cfg.crush_projects_json)
    pj.parent.mkdir(parents=True, exist_ok=True)
    pj.write_text(
        json.dumps({"projects": [{"path": cwd, "data_dir": str(data_dir)}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return db


def test_read_crush_db(cfg):
    db = make_crush_project(cfg)
    assert list_crush_projects(Path(cfg.crush_projects_json)) == [(r"C:\Work\proj", db)]
    sessions = read_crush_db(db)
    assert len(sessions) == 1
    s = sessions[0]
    assert s["title"] == "托卡马克线圈建模"
    assert s["msg_count"] == 2
    assert s["in_tokens"] == 204809 and s["out_tokens"] == 750
    assert s["rows"][0][2] == "user_text" and s["rows"][1][2] == "assistant_text"
    assert s["first_ts"].startswith("2026-08-")  # 秒级时间被正确换算


def test_crush_api_and_incremental(cfg):
    db = make_crush_project(cfg)
    app = create_app(cfg)
    with TestClient(app) as c:
        idx = app.state.indexer
        idx.scan_once()  # 启动时后台线程可能已抢先扫过,结果以 API 状态为准

        r = c.get("/api/sessions", params={"provider": "crush"}).json()
        assert r["total"] == 1
        s = r["items"][0]
        assert s["provider"] == "crush" and s["title"] == "托卡马克线圈建模"
        assert s["cwd"] == r"C:\Work\proj"

        # 视图出自索引(库型 provider 无 transcript 文件)
        win = c.get(f"/api/sessions/{CRUSH_SID}/messages").json()
        assert win["total_items"] == 2
        assert "Blender" in win["items"][1]["blocks"][0]["html"]
        assert win["source"] == "index"

        # 搜索可命中 crush 标题与正文
        hit = c.get("/api/search", params={"q": "型线圈白模"}).json()
        assert any(g["session"]["session_id"] == CRUSH_SID for g in hit["groups"])

        # 未变更 → 第二轮跳过;库变更 → 重摄取
        st2 = idx.scan_once()
        assert st2["rows_indexed"] == 0
        con2 = sqlite3.connect(db)
        con2.execute("UPDATE sessions SET updated_at = updated_at + 1 WHERE id=?", (CRUSH_SID,))
        con2.commit(); con2.close()
        st3 = idx.scan_once()
        assert st3["rows_indexed"] == 2

        # resume 命令 = 打开 crush(无命令行级 resume),note 说明这一点
        cmd = c.get(f"/api/sessions/{CRUSH_SID}/command").json()
        assert cmd["command"] == 'cd "C:\\Work\\proj"; crush'
        assert "会话列表" in cmd["note"]

        # 归档拒绝非 claude
        assert c.post(f"/api/sessions/{CRUSH_SID}/archive").status_code == 409


def test_build_resume_command_crush(cfg):
    assert build_resume_command(cfg, r"C:\W", CRUSH_SID, provider="crush") == 'cd "C:\\W"; crush'
