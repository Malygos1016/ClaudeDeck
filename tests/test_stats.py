from __future__ import annotations

import json

from app import db as db_mod
from app.indexer import Indexer
from app.stats import cost_curve, disk_stats, invalidate_cache, plans_list, usage_curve

from factory import SID, assistant_line, jsonl_bytes, user_line, write_jsonl


def test_usage_daily_curve(cfg):
    write_jsonl(
        cfg.projects_root / "C--Work-proj" / f"{SID}.jsonl",
        user_line(ts="2026-08-10T01:00:00.000Z"),
        assistant_line(ts="2026-08-10T01:01:00.000Z"),  # usage 10/20/30/40
        assistant_line(ts="2026-08-10T02:00:00.000Z", uuid="a-2"),
        assistant_line(ts="2026-08-11T01:00:00.000Z", uuid="a-3"),
    )
    con = db_mod.connect(cfg.db_path)
    Indexer(cfg, con).scan_once()
    rows = usage_curve(con, days=3650)
    assert [r["date"] for r in rows] == ["2026-08-10", "2026-08-11"]
    d1 = rows[0]
    assert d1["in_tokens"] == 20 and d1["out_tokens"] == 40
    assert rows[1]["cache_read"] == 30 and rows[1]["cache_write"] == 40


def test_cost_curve_daily_diff(cfg):
    invalidate_cache()
    lines = [
        {"date": "2026-08-01", "session_id": "X", "tokens": 60, "cost_usd": 0.6},
        {"date": "2026-08-01", "session_id": "X", "tokens": 100, "cost_usd": 1.0},
        {"date": "2026-08-02", "session_id": "X", "tokens": 250, "cost_usd": 2.5},
        {"date": "2026-08-02", "session_id": "Y", "tokens": 50, "cost_usd": 0.5},
    ]
    (cfg.claude_home_path / "token_log.jsonl").write_bytes(jsonl_bytes(*lines))
    out = cost_curve(cfg, days=3650)
    by_date = {r["date"]: r for r in out}
    assert by_date["2026-08-01"]["tokens"] == 100
    assert by_date["2026-08-01"]["cost_usd"] == 1.0
    assert by_date["2026-08-02"]["tokens"] == 200  # X: 250-100=150, Y: 50
    assert by_date["2026-08-02"]["cost_usd"] == 2.0


def test_disk_and_plans(cfg):
    invalidate_cache()
    write_jsonl(cfg.projects_root / "C--Work-proj" / f"{SID}.jsonl", user_line())
    plans = cfg.claude_home_path / "plans"
    plans.mkdir()
    (plans / "test-slug.md").write_text("# 计划", encoding="utf-8")

    con = db_mod.connect(cfg.db_path)
    Indexer(cfg, con).scan_once()

    d = disk_stats(cfg)
    names = {x["name"] for x in d["dirs"]}
    assert "projects" in names and "plans" in names
    assert d["archive"]["files"] >= 0

    pl = plans_list(cfg, con)
    assert pl[0]["slug"] == "test-slug"
    assert pl[0]["sessions"][0]["session_id"] == SID  # factory 行的 slug=test-slug
