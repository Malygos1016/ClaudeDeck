from __future__ import annotations

from app import db as db_mod
from app.indexer import Indexer
from app.search import MARK_L, MARK_R, effective_len, search

from factory import (
    SID,
    ai_title_line,
    assistant_line,
    last_prompt_line,
    user_line,
    write_jsonl,
)

SID2 = "22222222-3333-4444-5555-666666666666"


def setup_index(cfg):
    write_jsonl(
        cfg.projects_root / "C--Work-proj" / f"{SID}.jsonl",
        user_line(text="托卡马克失超检测方案讨论", ts="2026-08-10T00:00:00.000Z"),
        assistant_line(texts=("AI 失超检测复核意见",), ts="2026-08-10T00:01:00.000Z"),
        ai_title_line("失超会话"),
    )
    write_jsonl(
        cfg.projects_root / "C--Other-proj" / f"{SID2}.jsonl",
        user_line(
            text="a_b 记号说明",
            sid=SID2,
            cwd=r"C:\Other\proj",
            ts="2026-08-11T00:00:00.000Z",
        ),
        user_line(
            text="axb 不该被下划线查询命中",
            uuid="u-2",
            sid=SID2,
            cwd=r"C:\Other\proj",
            ts="2026-08-11T00:01:00.000Z",
        ),
        last_prompt_line("流量计选型备忘", sid=SID2),
        ai_title_line("流量计会话", sid=SID2),
    )
    con = db_mod.connect(cfg.db_path)
    Indexer(cfg, con).scan_once()
    return con


def test_effective_len_counts_cjk():
    assert effective_len("流量") == 2
    assert effective_len("托卡马克") == 4
    assert effective_len("a_b") == 2
    assert effective_len("%") == 0


def test_fts_chinese_hit_with_snippet(cfg):
    con = setup_index(cfg)
    res = search(con, "托卡马克")
    assert res["fallback"] is False
    assert res["groups"]
    g = res["groups"][0]
    assert g["session"]["session_id"] == SID
    assert MARK_L in g["hits"][0]["snippet"]


def test_fts_long_token_with_short_token_and(cfg):
    con = setup_index(cfg)
    res = search(con, "失超检测 AI")
    assert res["fallback"] is False
    texts = [h["snippet"] for g in res["groups"] for h in g["hits"]]
    assert texts  # 只有同时含两词的行命中
    assert all("AI" in t for t in texts)


def test_short_query_falls_back_to_like(cfg):
    con = setup_index(cfg)
    res = search(con, "流量")
    assert res["fallback"] is True
    assert res["groups"]
    assert res["groups"][0]["session"]["session_id"] == SID2  # 标题/末次输入命中排最前


def test_like_escapes_underscore(cfg):
    con = setup_index(cfg)
    res = search(con, "_b")
    assert res["fallback"] is True
    # 高亮哨兵插在命中词两侧,先剥掉再做包含判断
    snippets = [
        h["snippet"].replace(MARK_L, "").replace(MARK_R, "")
        for g in res["groups"]
        for h in g["hits"]
    ]
    assert any("a_b" in s for s in snippets)
    assert not any("axb" in s for s in snippets)


def test_empty_query(cfg):
    con = setup_index(cfg)
    assert search(con, "  ") == {"fallback": False, "total_hits": 0, "groups": []}


def test_title_meta_rows_searchable(cfg):
    con = setup_index(cfg)
    res = search(con, "流量计会话")  # ai-title 进了 FTS
    assert res["fallback"] is False
    assert any(g["session"]["session_id"] == SID2 for g in res["groups"])
