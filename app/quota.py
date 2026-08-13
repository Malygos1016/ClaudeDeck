"""5 小时配额窗口:切分 / 燃烧率 / 耗尽外推。

窗口语义对齐 ccusage blocks:锚点 = 首条活动所在的整点(UTC),窗口长 5 小时,
锚点+5h 之后的活动开新窗。官方额度没有公开数字,参照物是"你自己历史窗口的
最大用量"——预测是和过去比,不冒充官方配额,UI 必须如实标注。
只统计 provider='claude' 的用量(配额是 Anthropic 的事,Codex 不掺和)。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

BLOCK_HOURS = 5
HISTORY_DAYS = 40
MIN_SAMPLE_BLOCKS = 5


def _parse_hour(h: str) -> datetime:
    return datetime.strptime(h, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_blocks(hour_rows: list[tuple[str, int, int, int, int]], now: datetime) -> list[dict]:
    """小时聚合行 → 5h 窗口列表(升序)。行形如 (hour, in, out, cache_read, cache_write)。"""
    blocks: list[dict] = []
    cur: dict | None = None
    for h, i, o, cr, cw in sorted(hour_rows):
        try:
            t = _parse_hour(h)
        except ValueError:
            continue
        if cur is None or t >= cur["_start"] + timedelta(hours=BLOCK_HOURS):
            cur = {"_start": t, "in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
            blocks.append(cur)
        cur["in"] += i
        cur["out"] += o
        cur["cache_read"] += cr
        cur["cache_write"] += cw

    out = []
    for b in blocks:
        start: datetime = b.pop("_start")
        end = start + timedelta(hours=BLOCK_HOURS)
        total = b["in"] + b["out"] + b["cache_read"] + b["cache_write"]
        out.append(
            {
                "start": _iso(start),
                "end": _iso(end),
                **b,
                "total": total,
                "noncache": b["in"] + b["out"],
                "active": start <= now < end,
            }
        )
    return out


def quota_report(con: sqlite3.Connection, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%dT%H")
    rows = con.execute(
        "SELECT u.hour, SUM(u.in_tokens), SUM(u.out_tokens),"
        " SUM(u.cache_read_tokens), SUM(u.cache_write_tokens)"
        " FROM usage_hourly u JOIN sessions s USING(session_id)"
        " WHERE u.hour >= ? AND COALESCE(s.provider, 'claude') = 'claude'"
        " GROUP BY u.hour",
        (since,),
    ).fetchall()
    blocks = compute_blocks([tuple(r) for r in rows], now)

    current = blocks[-1] if blocks and blocks[-1]["active"] else None
    completed = [b for b in blocks if not b["active"]]
    limit_est = max((b["total"] for b in completed), default=None)

    cur_out = None
    if current is not None:
        start = datetime.strptime(current["start"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        end = start + timedelta(hours=BLOCK_HOURS)
        elapsed_min = max(1.0, (now - start).total_seconds() / 60)
        remaining_min = max(0.0, (end - now).total_seconds() / 60)
        burn = current["total"] / elapsed_min
        depleted_at = None
        over_history = bool(limit_est and current["total"] >= limit_est)
        if limit_est and burn > 0 and not over_history:
            eta_min = (limit_est - current["total"]) / burn
            if eta_min <= remaining_min:
                depleted_at = _iso(now + timedelta(minutes=eta_min))
        cur_out = {
            **current,
            "elapsed_minutes": round(elapsed_min),
            "remaining_minutes": round(remaining_min),
            "burn_per_min": round(burn),
            "projected_total": round(current["total"] + burn * remaining_min),
            "vs_limit_pct": round(current["total"] / limit_est * 100) if limit_est else None,
            "depleted_at": depleted_at,
            "over_history_max": over_history,
        }

    return {
        "now": _iso(now),
        "current": cur_out,
        "limit_estimate": {
            "tokens": limit_est,
            "blocks_sampled": len(completed),
            "sample_ok": len(completed) >= MIN_SAMPLE_BLOCKS,
        },
        "recent_blocks": blocks[-12:],
    }
