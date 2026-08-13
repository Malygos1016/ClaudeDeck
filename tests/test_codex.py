"""Codex provider:rollout 解析 / 混合索引 / provider 过滤 / resume 命令 / 详情视图。"""
from __future__ import annotations

import io
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.codex import parse_codex_chunk, parse_codex_view_items
from app.launcher import build_resume_command
from app.main import create_app
from app.scanner import list_codex_files

CODEX_SID = "019f40ee-40d9-7100-8934-013e6735d0c9"


def _line(ts: str, ltype: str, payload: dict) -> str:
    return json.dumps({"timestamp": ts, "type": ltype, "payload": payload}, ensure_ascii=False)


def rollout_lines() -> list[str]:
    return [
        _line(
            "2026-07-08T08:53:03.845Z",
            "session_meta",
            {
                "id": CODEX_SID,
                "session_id": CODEX_SID,
                "cwd": "C:\\Work\\proj",
                "cli_version": "0.144.2",
                "git": {"branch": "main"},
            },
        ),
        # 注入噪音:response_item 里的 user 指令块(必须被忽略)
        _line(
            "2026-07-08T08:53:03.900Z",
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "<user_instructions>x</user_instructions>"}],
            },
        ),
        _line(
            "2026-07-08T08:53:04.000Z",
            "event_msg",
            {"type": "user_message", "message": "共模抑制比是什么意思"},
        ),
        _line(
            "2026-07-08T08:53:10.000Z",
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "**CMRR** 是差分放大器抑制共模干扰能力的指标。"}],
            },
        ),
        _line(
            "2026-07-08T08:53:11.000Z",
            "event_msg",
            {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 300, "output_tokens": 50},
                    "last_token_usage": {"input_tokens": 1000, "cached_input_tokens": 300, "output_tokens": 50},
                },
            },
        ),
        # 应被忽略的 UI 重复行
        _line(
            "2026-07-08T08:53:12.000Z",
            "event_msg",
            {"type": "agent_message", "message": "**CMRR** 是差分放大器抑制共模干扰能力的指标。"},
        ),
    ]


def make_codex_rollout(cfg, sid: str = CODEX_SID) -> Path:
    d = Path(cfg.codex_home) / "sessions" / "2026" / "07" / "08"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"rollout-2026-07-08T16-53-03-{sid}.jsonl"
    f.write_text("\n".join(rollout_lines()) + "\n", encoding="utf-8")
    return f


def test_parse_codex_chunk():
    data = ("\n".join(rollout_lines()) + "\n").encode("utf-8")
    res = parse_codex_chunk(io.BytesIO(data))
    assert res.cwd == "C:\\Work\\proj"
    assert res.version == "0.144.2"
    assert res.git_branch == "main"
    assert res.first_ts == "2026-07-08T08:53:03.845Z"
    assert res.last_ts == "2026-07-08T08:53:12.000Z"
    # 两条正文:user(event_msg) + assistant(response_item);注入噪音与 agent_message 不入
    assert [r.kind for r in res.rows] == ["user_text", "assistant_text"]
    assert res.rows[0].seq == 0 and res.rows[1].seq == 1
    assert res.first_user_text == "共模抑制比是什么意思"
    assert res.msg_count == 2
    # usage: in = 1000-300, cache_read = 300
    assert res.in_tokens == 700
    assert res.cache_read_tokens == 300
    assert res.out_tokens == 50
    assert res.usage_hourly["2026-07-08T08"] == [700, 50, 300, 0]


def test_list_codex_files(cfg):
    make_codex_rollout(cfg)
    files = list_codex_files(cfg.codex_sessions_root)
    assert len(files) == 1
    assert files[0].session_id == CODEX_SID
    assert files[0].proj_dir == "2026/07/08"


def test_mixed_index_and_api(cfg):
    from tests.factory import ai_title_line, user_line, write_jsonl

    write_jsonl(  # claude 侧对照会话
        cfg.projects_root / "C--Work-proj" / "aaaaaaaa-bbbb-cccc-dddd-eeeeffff0000.jsonl",
        user_line(text="claude 对照", ts="2026-08-10T00:00:00.000Z",
                  sid="aaaaaaaa-bbbb-cccc-dddd-eeeeffff0000"),
        ai_title_line("对照会话", sid="aaaaaaaa-bbbb-cccc-dddd-eeeeffff0000"),
    )
    make_codex_rollout(cfg)
    app = create_app(cfg)
    with TestClient(app) as c:
        app.state.indexer.scan_once()

        r = c.get("/api/sessions", params={"provider": "codex"}).json()
        assert r["total"] == 1
        s = r["items"][0]
        assert s["session_id"] == CODEX_SID
        assert s["provider"] == "codex"
        assert s["title"] == "共模抑制比是什么意思"

        r_claude = c.get("/api/sessions", params={"provider": "claude"}).json()
        assert all(x["provider"] == "claude" for x in r_claude["items"])
        assert r_claude["total"] >= 1

        # 全文搜索能命中 codex 正文
        hit = c.get("/api/search", params={"q": "共模抑制比"}).json()
        assert any(g["session"]["session_id"] == CODEX_SID for g in hit["groups"])

        # 详情视图走 codex 解析器
        win = c.get(f"/api/sessions/{CODEX_SID}/messages").json()
        assert win["total_items"] == 2
        assert win["items"][0]["role"] == "user"
        assert "CMRR" in win["items"][1]["blocks"][0]["html"]

        # resume 命令
        cmd = c.get(f"/api/sessions/{CODEX_SID}/command").json()["command"]
        assert f"codex resume {CODEX_SID}" in cmd
        assert 'cd "C:\\Work\\proj"' in cmd

        # 归档拒绝 codex
        assert c.post(f"/api/sessions/{CODEX_SID}/archive").status_code == 409


def test_codex_view_items_seq_parity(cfg):
    f = make_codex_rollout(cfg)
    items = parse_codex_view_items(f)
    data = f.read_bytes()
    res = parse_codex_chunk(io.BytesIO(data))
    assert [it["seq"] for it in items] == [r.seq for r in res.rows]


def test_build_resume_command_codex(cfg):
    cmd = build_resume_command(cfg, r"C:\W", "abc", provider="codex")
    assert cmd == 'cd "C:\\W"; codex resume abc'
