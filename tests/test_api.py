from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app

from factory import SID, ai_title_line, assistant_line, user_line, write_jsonl


def make_home(cfg):
    write_jsonl(
        cfg.projects_root / "C--Work-proj" / f"{SID}.jsonl",
        user_line(text="托卡马克失超检测方案讨论", ts="2026-08-10T00:00:00.000Z"),
        assistant_line(texts=("AI 复核意见",), ts="2026-08-10T00:01:00.000Z"),
        ai_title_line("失超会话"),
    )


def test_api_smoke(cfg):
    make_home(cfg)
    app = create_app(cfg)
    with TestClient(app) as client:  # 进程内 ASGI,不经网络代理
        app.state.indexer.scan_once()  # 与后台线程的扫描互斥,保证就绪

        health = client.get("/healthz").json()
        assert health["ok"] is True and health["db_ok"] is True

        data = client.get("/api/sessions").json()
        assert data["total"] >= 1
        item = next(i for i in data["items"] if i["session_id"] == SID)
        assert item["title"] == "失超会话"
        assert item["archived"] is False

        projects = client.get("/api/projects").json()
        assert any(p["cwd"] == r"C:\Work\proj" for p in projects["items"])

        res = client.get("/api/search", params={"q": "托卡马克"}).json()
        assert res["fallback"] is False
        assert res["groups"][0]["session"]["session_id"] == SID
        assert "<mark>" in res["groups"][0]["hits"][0]["snippet_html"]

        res2 = client.get("/api/search", params={"q": "AI"}).json()
        assert res2["fallback"] is True  # 2 字符走 LIKE 回退
        assert res2["groups"]

        det = client.get(f"/api/sessions/{SID}").json()
        assert det["session"]["cwd"] == r"C:\Work\proj"
        assert det["subagents"] == []

        cmd = client.get(f"/api/sessions/{SID}/command").json()
        assert f"--resume {SID}" in cmd["command"]
        assert 'cd "C:\\Work\\proj"' in cmd["command"]
        fork = client.get(f"/api/sessions/{SID}/command", params={"fork": "true"}).json()
        assert "--fork-session" in fork["command"]

        assert client.get("/api/sessions/00000000-0000-0000-0000-000000000000").status_code == 404
        assert (
            client.get("/api/sessions/00000000-0000-0000-0000-000000000000/command").status_code
            == 404
        )

        st = client.get("/api/index/status").json()
        assert st["phase"] in ("idle", "scanning")
        assert client.post("/api/index/scan").status_code == 200

        page = client.get("/").text
        assert "ClaudeDeck" in page

        bad = client.get("/api/sessions", params={"sort": "evil; DROP"})
        assert bad.status_code == 400


def test_api_list_filters(cfg):
    make_home(cfg)
    app = create_app(cfg)
    with TestClient(app) as client:
        app.state.indexer.scan_once()
        only_missing = client.get("/api/sessions", params={"archived": "missing"}).json()
        assert all(i["source_missing"] for i in only_missing["items"])
        q = client.get("/api/sessions", params={"q": "失超"}).json()
        assert q["total"] == 1
        proj = client.get("/api/sessions", params={"project": r"C:\Nope"}).json()
        assert proj["total"] == 0
