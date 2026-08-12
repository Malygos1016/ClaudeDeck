"""磁盘 / token 用量 / plans 统计。重扫描类结果带 10 分钟进程内缓存。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import date, timedelta
from pathlib import Path

from .config import Config

_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()
CACHE_TTL_S = 600


def _cached(key: str, builder):
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_S:
            return hit[1]
    val = builder()
    with _cache_lock:
        _cache[key] = (time.monotonic(), val)
    return val


def _du(root: Path) -> tuple[int, int]:
    total = files = 0
    if not root.is_dir():
        return 0, 0
    for f in root.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
                files += 1
        except OSError:
            continue
    return total, files


def disk_stats(cfg: Config) -> dict:
    def build():
        home = cfg.claude_home_path
        dirs = []
        top_files = 0
        if home.is_dir():
            for entry in sorted(home.iterdir()):
                try:
                    if entry.is_dir():
                        size, files = _du(entry)
                        dirs.append({"name": entry.name, "bytes": size, "files": files})
                    elif entry.is_file():
                        top_files += entry.stat().st_size
                except OSError:
                    continue
        dirs.sort(key=lambda d: -d["bytes"])
        a_size, a_files = _du(cfg.archive_dir_path)
        return {
            "claude_home": str(home),
            "dirs": dirs,
            "top_level_files_bytes": top_files,
            "total_bytes": sum(d["bytes"] for d in dirs) + top_files,
            "archive": {"path": str(cfg.archive_dir_path), "bytes": a_size, "files": a_files},
        }

    return _cached(f"disk:{cfg.claude_home}:{cfg.archive_dir}", build)


def project_disk(con: sqlite3.Connection) -> list[dict]:
    """按项目占用(来自索引,零额外 IO):清理决策的主视角。"""
    rows = con.execute(
        "SELECT cwd, COUNT(*) AS sessions, COALESCE(SUM(file_size),0) AS bytes,"
        " MAX(last_ts) AS last_ts FROM sessions WHERE source_missing=0"
        " GROUP BY cwd ORDER BY bytes DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def usage_curve(con: sqlite3.Connection, days: int) -> list[dict]:
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    rows = con.execute(
        "SELECT date, SUM(in_tokens) AS in_tokens, SUM(out_tokens) AS out_tokens,"
        " SUM(cache_read_tokens) AS cache_read, SUM(cache_write_tokens) AS cache_write"
        " FROM usage_daily WHERE date >= ? GROUP BY date ORDER BY date",
        (since,),
    ).fetchall()
    return [dict(r) for r in rows]


def _token_log_daily(cfg: Config) -> dict[str, dict[str, float]]:
    """token_log.jsonl(statusline 产物,累计值)→ 按日增量。

    每 (session, date) 取当日最大累计值,按会话跨日差分后汇总到日。
    会话跨采集窗的第一天含此前累计,曲线边缘略有高估——趋势图可接受。
    """

    def build():
        path = cfg.claude_home_path / "token_log.jsonl"
        per: dict[tuple[str, str], dict[str, float]] = {}
        if path.is_file():
            with open(path, "rb") as fp:
                for line in fp:
                    if not line.endswith(b"\n"):
                        break  # 半行不消费
                    try:
                        d = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    sid, day = d.get("session_id"), d.get("date")
                    if not sid or not day:
                        continue
                    slot = per.setdefault((sid, day), {"tokens": 0.0, "cost": 0.0})
                    slot["tokens"] = max(slot["tokens"], float(d.get("tokens") or 0))
                    slot["cost"] = max(slot["cost"], float(d.get("cost_usd") or 0))

        by_sid: dict[str, list[tuple[str, dict]]] = {}
        for (sid, day), v in per.items():
            by_sid.setdefault(sid, []).append((day, v))
        daily: dict[str, dict[str, float]] = {}
        for sid, seq in by_sid.items():
            seq.sort()
            prev = {"tokens": 0.0, "cost": 0.0}
            for day, v in seq:
                slot = daily.setdefault(day, {"tokens": 0.0, "cost": 0.0})
                slot["tokens"] += max(0.0, v["tokens"] - prev["tokens"])
                slot["cost"] += max(0.0, v["cost"] - prev["cost"])
                prev = v
        return daily

    return _cached(f"token_log:{cfg.claude_home}", build)


def cost_curve(cfg: Config, days: int) -> list[dict]:
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    daily = _token_log_daily(cfg)
    out = [
        {"date": day, "tokens": round(v["tokens"]), "cost_usd": round(v["cost"], 4)}
        for day, v in sorted(daily.items())
        if day >= since
    ]
    return out


def plans_list(cfg: Config, con: sqlite3.Connection) -> list[dict]:
    plans_dir = cfg.claude_home_path / "plans"
    out = []
    if plans_dir.is_dir():
        for f in sorted(plans_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
            slug = f.stem
            sessions = [
                dict(r)
                for r in con.execute(
                    "SELECT session_id, title FROM sessions WHERE slug=?", (slug,)
                ).fetchall()
            ]
            st = f.stat()
            out.append(
                {
                    "slug": slug,
                    "bytes": st.st_size,
                    "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(st.st_mtime)) + "Z",
                    "sessions": sessions,
                }
            )
    return out


def invalidate_cache() -> None:
    with _cache_lock:
        _cache.clear()
