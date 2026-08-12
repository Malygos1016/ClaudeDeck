from __future__ import annotations

from pathlib import Path

from app import db as db_mod
from app.archive import restore_session
from app.indexer import Indexer
from app.scanner import munge_path
from app.search import search

from conftest import make_old, write_meta_json
from factory import (
    SID,
    ai_title_line,
    append_jsonl,
    assistant_line,
    bridge_line,
    jsonl_bytes,
    mode_line,
    permission_mode_line,
    user_line,
    write_jsonl,
)

PROJ = "C--Work-proj"
ORPHAN_SID = "99999999-8888-7777-6666-555555555555"


def make_session_file(cfg, objs, sid: str = SID, proj: str = PROJ) -> Path:
    p = cfg.projects_root / proj / f"{sid}.jsonl"
    return write_jsonl(p, *objs)


def new_indexer(cfg):
    con = db_mod.connect(cfg.db_path)
    return Indexer(cfg, con), con


def base_lines():
    return [
        user_line(text="托卡马克失超检测方案", ts="2026-08-11T06:14:06.622Z"),
        assistant_line(texts=("方案正文",), ts="2026-08-11T06:15:00.000Z"),
        ai_title_line("失超检测讨论"),
        mode_line(),
    ]


def test_full_then_incremental_scan(cfg):
    p = make_session_file(cfg, base_lines())
    idx, con = new_indexer(cfg)

    st = idx.scan_once()
    assert st["files_parsed"] == 1 and st["file_errors"] == 0
    row = con.execute("SELECT * FROM sessions WHERE session_id=?", (SID,)).fetchone()
    assert row["title"] == "失超检测讨论"
    assert row["title_source"] == "ai-title"
    assert row["cwd"] == r"C:\Work\proj"
    assert row["msg_count"] == 2
    assert row["last_ts"] == "2026-08-11T06:15:00.000Z"
    assert row["in_tokens"] == 10 and row["cache_write_tokens"] == 40

    st2 = idx.scan_once()
    assert st2["files_parsed"] == 0 and st2["files_skipped"] == 1

    append_jsonl(p, user_line(text="追加的问题", uuid="u-9", ts="2026-08-12T00:00:00.000Z"))
    st3 = idx.scan_once()
    assert st3["files_parsed"] == 1
    row = con.execute("SELECT * FROM sessions WHERE session_id=?", (SID,)).fetchone()
    assert row["msg_count"] == 3
    assert row["last_ts"] == "2026-08-12T00:00:00.000Z"
    # 追加不重扫:messages 无重复
    n = con.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id=? AND kind='user_text'", (SID,)
    ).fetchone()[0]
    assert n == 2


def test_control_only_append_keeps_last_ts(cfg):
    p = make_session_file(cfg, base_lines())
    idx, con = new_indexer(cfg)
    idx.scan_once()

    append_jsonl(p, mode_line(), permission_mode_line(), bridge_line("cse_01NEWID"))
    st = idx.scan_once()
    assert st["files_parsed"] == 1  # mtime/size 变了,确实重读
    row = con.execute("SELECT * FROM sessions WHERE session_id=?", (SID,)).fetchone()
    assert row["last_ts"] == "2026-08-11T06:15:00.000Z"  # 核心:纯控制行不改末活跃
    assert row["bridge_session_id"] == "cse_01NEWID"
    assert row["msg_count"] == 2


def test_truncate_triggers_full_rescan(cfg):
    p = make_session_file(cfg, base_lines())
    idx, con = new_indexer(cfg)
    idx.scan_once()

    write_jsonl(p, user_line(text="重写后只剩这一条", ts="2026-08-13T00:00:00.000Z"))
    idx.scan_once()
    row = con.execute("SELECT * FROM sessions WHERE session_id=?", (SID,)).fetchone()
    assert row["msg_count"] == 1
    assert row["title_source"] == "first-user"
    assert row["title"].startswith("重写后")
    kinds = {
        r["kind"]
        for r in con.execute(
            "SELECT kind FROM messages WHERE session_id=?", (SID,)
        ).fetchall()
    }
    assert "ai_title" not in kinds  # 旧标题行随全量重扫清掉


def test_history_orphan_synth_then_real_transcript(cfg):
    make_session_file(cfg, base_lines())
    hist = cfg.claude_home_path / "history.jsonl"
    hist.write_bytes(
        jsonl_bytes(
            {
                "display": "有 transcript 的输入,不该合成",
                "pastedContents": {},
                "timestamp": 1786500000000,
                "project": r"C:\Work\proj",
                "sessionId": SID,
            },
            {
                "display": "查一下量化因子采集",
                "pastedContents": {},
                "timestamp": 1786500100000,
                "project": r"C:\Other",
                "sessionId": ORPHAN_SID,
            },
        )
    )
    idx, con = new_indexer(cfg)
    idx.scan_once()

    row = con.execute("SELECT * FROM sessions WHERE session_id=?", (ORPHAN_SID,)).fetchone()
    assert row is not None
    assert row["title_source"] == "history"
    assert row["source_missing"] == 1
    assert row["cwd"] == r"C:\Other"
    assert row["title"] == "查一下量化因子采集"
    n = con.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id=? AND kind='history_prompt'", (SID,)
    ).fetchone()[0]
    assert n == 0  # 有 transcript 的会话不合成

    res = search(con, "量化因子")
    assert any(g["session"]["session_id"] == ORPHAN_SID for g in res["groups"])

    # 二次扫描幂等:不重复合成
    idx.scan_once()
    n = con.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id=? AND kind='history_prompt'",
        (ORPHAN_SID,),
    ).fetchone()[0]
    assert n == 1

    # 该会话后来有了真 transcript → history 合成让位
    make_session_file(
        cfg,
        [user_line(text="孤儿会话回来了", sid=ORPHAN_SID, cwd=r"C:\Other")],
        sid=ORPHAN_SID,
        proj=munge_path(r"C:\Other"),
    )
    idx.scan_once()
    row = con.execute("SELECT * FROM sessions WHERE session_id=?", (ORPHAN_SID,)).fetchone()
    assert row["title_source"] == "first-user"
    assert row["source_missing"] == 0
    n = con.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id=? AND kind='history_prompt'",
        (ORPHAN_SID,),
    ).fetchone()[0]
    assert n == 0


def test_archive_quiet_then_source_missing_then_restore(cfg):
    p = make_session_file(cfg, base_lines())
    idx, con = new_indexer(cfg)

    st = idx.scan_once()
    assert st["archived_copies"] == 0  # 文件还新鲜,安静期内不拷

    make_old(p, minutes=30)
    st = idx.scan_once()
    assert st["archived_copies"] == 1
    archived = cfg.archive_projects_root / PROJ / f"{SID}.jsonl"
    assert archived.is_file()
    row = con.execute("SELECT archived_at FROM sessions WHERE session_id=?", (SID,)).fetchone()
    assert row["archived_at"] is not None

    # 官方清理把源删了 → 归档副本兜底,可搜可看
    p.unlink()
    idx.scan_once()
    row = con.execute("SELECT * FROM sessions WHERE session_id=?", (SID,)).fetchone()
    assert row["source_missing"] == 1
    res = search(con, "托卡马克")
    assert any(g["session"]["session_id"] == SID for g in res["groups"])

    # 还原回 projects → 可再 resume
    live = restore_session(cfg, cfg.projects_root, PROJ, SID)
    assert live.is_file()
    idx.scan_once()
    row = con.execute("SELECT * FROM sessions WHERE session_id=?", (SID,)).fetchone()
    assert row["source_missing"] == 0


def test_subagents_meta_and_file_size(cfg):
    p = make_session_file(cfg, base_lines())
    comp = p.parent / SID / "subagents"
    agent_jsonl = write_jsonl(
        comp / "agent-0123456789abcdef0.jsonl",
        user_line(uuid="side-1", isSidechain=True),
    )
    write_meta_json(comp / "agent-0123456789abcdef0.meta.json", "agent-0123456789abcdef0")
    idx, con = new_indexer(cfg)
    idx.scan_once()

    arow = con.execute("SELECT * FROM subagents WHERE session_id=?", (SID,)).fetchone()
    assert arow is not None
    assert arow["agent_type"] == "Explore"
    assert arow["description"] == "盘点数据"
    assert arow["file_path"] == str(agent_jsonl)

    srow = con.execute("SELECT * FROM sessions WHERE session_id=?", (SID,)).fetchone()
    assert srow["subagent_count"] == 1
    comp_total = sum(f.stat().st_size for f in comp.rglob("*") if f.is_file())
    assert srow["file_size"] == p.stat().st_size + comp_total
