"""配额窗口:切分语义 / 燃烧率 / 外推。纯函数直测,不依赖真实数据。"""
from datetime import datetime, timezone

from app.quota import compute_blocks

NOW = datetime(2026, 8, 13, 10, 30, tzinfo=timezone.utc)


def _h(s, i=100, o=50, cr=0, cw=0):
    return (s, i, o, cr, cw)


def test_single_block_within_5h():
    blocks = compute_blocks([_h("2026-08-13T06"), _h("2026-08-13T08")], NOW)
    assert len(blocks) == 1
    b = blocks[0]
    assert b["start"] == "2026-08-13T06:00:00Z"
    assert b["end"] == "2026-08-13T11:00:00Z"
    assert b["in"] == 200 and b["out"] == 100
    assert b["total"] == 300 and b["noncache"] == 300
    assert b["active"] is True  # 10:30 落在 06~11 窗口内


def test_gap_opens_new_block():
    # 00 点活动 → 窗口 00~05;06 点活动在窗口外 → 新窗 06~11
    blocks = compute_blocks([_h("2026-08-13T00"), _h("2026-08-13T06")], NOW)
    assert len(blocks) == 2
    assert blocks[0]["start"] == "2026-08-13T00:00:00Z"
    assert blocks[0]["active"] is False
    assert blocks[1]["start"] == "2026-08-13T06:00:00Z"


def test_boundary_hour_starts_new_block():
    # 锚点+5h 恰好整点活动:属于新窗(区间左闭右开)
    blocks = compute_blocks([_h("2026-08-13T00"), _h("2026-08-13T05")], NOW)
    assert len(blocks) == 2


def test_cache_split():
    blocks = compute_blocks([_h("2026-08-13T09", i=10, o=20, cr=1000, cw=70)], NOW)
    b = blocks[0]
    assert b["total"] == 1100
    assert b["noncache"] == 30


def test_empty():
    assert compute_blocks([], NOW) == []


def test_quota_report_prediction(cfg):
    """report 装配:当前窗口燃烧率与触顶外推,codex 用量不计入。"""
    from app import db as db_mod
    from app.quota import quota_report

    con = db_mod.connect(cfg.db_path)
    con.execute(
        "INSERT INTO sessions(session_id, proj_dir, provider) VALUES('s1','p','claude')"
    )
    con.execute(
        "INSERT INTO sessions(session_id, proj_dir, provider) VALUES('sx','p','codex')"
    )
    # 历史窗口(昨天):总量 1000 → 参照上限
    con.execute(
        "INSERT INTO usage_hourly(session_id, hour, in_tokens, out_tokens) VALUES('s1','2026-08-12T00',600,400)"
    )
    # 当前窗口:锚点 09 点,NOW=10:30 → 已用 90 分钟,已用 300
    con.execute(
        "INSERT INTO usage_hourly(session_id, hour, in_tokens, out_tokens) VALUES('s1','2026-08-13T09',200,100)"
    )
    # codex 大额用量,若被算进来断言必炸
    con.execute(
        "INSERT INTO usage_hourly(session_id, hour, in_tokens, out_tokens) VALUES('sx','2026-08-13T09',99999,0)"
    )
    con.commit()

    rep = quota_report(con, now=NOW)
    cur = rep["current"]
    assert cur is not None
    assert cur["total"] == 300  # codex 未计入
    assert cur["elapsed_minutes"] == 90
    assert cur["burn_per_min"] == round(300 / 90)
    assert rep["limit_estimate"]["tokens"] == 1000
    # 燃烧率 3.33/min,剩 700 → 210 分钟后触顶;窗口只剩 210 分钟 → 恰好在窗口内
    assert cur["depleted_at"] is not None
    assert rep["limit_estimate"]["sample_ok"] is False  # 只有 1 个完成窗口
