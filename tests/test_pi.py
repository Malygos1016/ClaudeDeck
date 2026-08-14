"""pi provider:JSONL 解析 / 文件枚举 / 混合索引 / resume 命令。"""
from __future__ import annotations

import io
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.launcher import build_resume_command
from app.main import create_app
from app.pi import parse_pi_chunk, parse_pi_view_items
from app.scanner import list_pi_files

PI_SID = "019ff8c3-34bb-7407-a4c8-43dbde20ee7d"


def pi_lines() -> list[str]:
    def j(d):
        return json.dumps(d, ensure_ascii=False)

    return [
        j({"type": "session", "version": 3, "id": PI_SID,
           "timestamp": "2026-08-13T01:36:10.427Z", "cwd": "C:\\Work\\proj"}),
        j({"type": "model_change", "id": "x1", "timestamp": "2026-08-13T01:36:10.474Z",
           "provider": "deepseek", "modelId": "deepseek-v4-pro"}),
        j({"type": "message", "id": "m1", "timestamp": "2026-08-13T01:42:05.192Z",
           "message": {"role": "user", "content": [{"type": "text", "text": "帮我装一下 Crush"}]}}),
        j({"type": "message", "id": "m2", "timestamp": "2026-08-13T01:42:08.991Z",
           "message": {"role": "assistant",
                       "content": [{"type": "thinking", "thinking": "内心戏"},
                                   {"type": "text", "text": "好的,用 **winget** 装。"}],
                       "usage": {"input": 1913, "output": 136, "cacheRead": 10, "cacheWrite": 5}}}),
        j({"type": "message", "id": "m3", "timestamp": "2026-08-13T01:42:09.100Z",
           "message": {"role": "toolResult", "content": [{"type": "text", "text": "工具回执不入索引"}]}}),
    ]


def make_pi_session(cfg, sid: str = PI_SID) -> Path:
    d = Path(cfg.pi_home) / "agent" / "sessions" / "--C--Work-proj--"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"2026-08-13T01-36-10-427Z_{sid}.jsonl"
    f.write_text("\n".join(pi_lines()) + "\n", encoding="utf-8")
    return f


def test_parse_pi_chunk():
    res = parse_pi_chunk(io.BytesIO(("\n".join(pi_lines()) + "\n").encode("utf-8")))
    assert res.cwd == "C:\\Work\\proj"
    assert [r.kind for r in res.rows] == ["user_text", "assistant_text"]
    assert res.rows[1].text == "好的,用 **winget** 装。"  # thinking 不入正文
    assert res.first_user_text == "帮我装一下 Crush"
    assert res.msg_count == 2
    assert (res.in_tokens, res.out_tokens, res.cache_read_tokens, res.cache_write_tokens) == (
        1913, 136, 10, 5,
    )
    assert res.usage_hourly["2026-08-13T01"] == [1913, 136, 10, 5]
    assert res.first_ts == "2026-08-13T01:36:10.427Z"


def test_list_pi_files(cfg):
    make_pi_session(cfg)
    files = list_pi_files(cfg.pi_sessions_root)
    assert len(files) == 1
    assert files[0].session_id == PI_SID


def test_pi_api_and_view(cfg):
    f = make_pi_session(cfg)
    app = create_app(cfg)
    with TestClient(app) as c:
        app.state.indexer.scan_once()
        r = c.get("/api/sessions", params={"provider": "pi"}).json()
        assert r["total"] == 1
        assert r["items"][0]["provider"] == "pi"
        assert r["items"][0]["title"] == "帮我装一下 Crush"

        win = c.get(f"/api/sessions/{PI_SID}/messages").json()
        assert win["total_items"] == 2
        assert "winget" in win["items"][1]["blocks"][0]["html"]

        cmd = c.get(f"/api/sessions/{PI_SID}/command").json()["command"]
        assert cmd == f'cd "C:\\Work\\proj"; pi --session {PI_SID}'

    items = parse_pi_view_items(f)
    assert [it["seq"] for it in items] == [0, 1]


def test_build_resume_command_pi(cfg):
    assert build_resume_command(cfg, None, "abc", provider="pi") == "pi --session abc"
