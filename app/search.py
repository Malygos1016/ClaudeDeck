"""搜索路由:有效字符 ≥3 走 FTS5 trigram;1-2 字短词回退 LIKE(已实测 trigram 两字中文零命中)。

snippet 用 \\x02/\\x03 作高亮哨兵,由展示层各自替换(CLI→【】,Web→转义后 <mark>)。
"""
from __future__ import annotations

import sqlite3

from .transcript import bridge_url

MARK_L = "\x02"
MARK_R = "\x03"
MAX_HITS_PER_SESSION = 5


def effective_len(s: str) -> int:
    """有效字符数:字母/数字/汉字(str.isalnum 对 CJK 为 True),忽略标点空白。"""
    return sum(1 for ch in s if ch.isalnum())


def _fts_phrase(tok: str) -> str:
    return '"' + tok.replace('"', '""') + '"'


def _like_pattern(tok: str) -> str:
    esc = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _manual_snippet(text: str, tokens: list[str], width: int = 44) -> str:
    """LIKE 命中的手工摘录:定位第一个命中词,取前后文并打哨兵。"""
    low = text.casefold()
    pos, hit = -1, ""
    for t in tokens:
        p = low.find(t.casefold())
        if p >= 0 and (pos < 0 or p < pos):
            pos, hit = p, t
    if pos < 0:
        return text[: width * 2]
    start = max(0, pos - width)
    end = min(len(text), pos + len(hit) + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    seg = text[start:end]
    rel = pos - start
    seg = seg[:rel] + MARK_L + seg[rel : rel + len(hit)] + MARK_R + seg[rel + len(hit) :]
    return prefix + seg + suffix


def search(
    con: sqlite3.Connection, q: str, *, project: str | None = None, limit: int = 200
) -> dict:
    q = (q or "").strip()
    if not q:
        return {"fallback": False, "total_hits": 0, "groups": []}
    tokens = [t for t in q.split() if effective_len(t) > 0]
    if not tokens:
        return {"fallback": False, "total_hits": 0, "groups": []}
    long_toks = [t for t in tokens if effective_len(t) >= 3]
    short_toks = [t for t in tokens if effective_len(t) < 3]

    if long_toks:
        hits = _fts_search(con, long_toks, short_toks, project, limit)
        fallback = False
    else:
        hits = _like_search(con, tokens, project, limit)
        fallback = True

    groups = _group(con, hits)
    total = sum(len(g["hits"]) for g in groups)
    return {"fallback": fallback, "total_hits": total, "groups": groups}


def _fts_search(
    con: sqlite3.Connection,
    long_toks: list[str],
    short_toks: list[str],
    project: str | None,
    limit: int,
) -> list[dict]:
    match = " AND ".join(_fts_phrase(t) for t in long_toks)
    sql = (
        "SELECT m.session_id AS sid, m.seq, m.kind, m.ts, "
        f"snippet(messages_fts, 0, '{MARK_L}', '{MARK_R}', '…', 16) AS snip, "
        "bm25(messages_fts) AS rank "
        "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
    )
    params: list = []
    where = ["messages_fts MATCH ?"]
    params.append(match)
    if project:
        sql += "JOIN sessions s ON s.session_id = m.session_id "
        where.append("s.cwd = ?")
        params.append(project)
    for t in short_toks:  # trigram 吃不下的短词,在候选集上用 LIKE 收窄
        where.append("m.text LIKE ? ESCAPE '\\'")
        params.append(_like_pattern(t))
    sql += "WHERE " + " AND ".join(where) + " ORDER BY rank LIMIT ?"
    params.append(limit)
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def _like_search(
    con: sqlite3.Connection, tokens: list[str], project: str | None, limit: int
) -> list[dict]:
    hits: list[dict] = []
    pats = [_like_pattern(t) for t in tokens]

    # 第一段:标题/末次输入(毫秒级)
    sql = "SELECT session_id, title, last_prompt FROM sessions WHERE 1=1"
    params: list = []
    for p in pats:
        sql += " AND (title LIKE ? ESCAPE '\\' OR last_prompt LIKE ? ESCAPE '\\')"
        params.extend([p, p])
    if project:
        sql += " AND cwd = ?"
        params.append(project)
    sql += " ORDER BY last_ts DESC LIMIT ?"
    params.append(limit)
    for r in con.execute(sql, params).fetchall():
        text = r["title"] or r["last_prompt"] or ""
        hits.append(
            {
                "sid": r["session_id"],
                "seq": -1,
                "kind": "title",
                "ts": None,
                "snip": _manual_snippet(text, tokens),
                "rank": -100.0,  # 标题命中排最前
            }
        )

    # 第二段:全文 LIKE(几十~200MB 提取文本,约 1-3 秒,LIMIT 兜底)
    sql = "SELECT m.session_id AS sid, m.seq, m.kind, m.ts, m.text FROM messages m"
    params = []
    where = []
    for p in pats:
        where.append("m.text LIKE ? ESCAPE '\\'")
        params.append(p)
    if project:
        sql += " JOIN sessions s ON s.session_id = m.session_id"
        where.append("s.cwd = ?")
        params.append(project)
    sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY (m.ts IS NULL), m.ts DESC LIMIT ?"
    params.append(limit)
    for r in con.execute(sql, params).fetchall():
        hits.append(
            {
                "sid": r["sid"],
                "seq": r["seq"],
                "kind": r["kind"],
                "ts": r["ts"],
                "snip": _manual_snippet(r["text"], tokens),
                "rank": 0.0,
            }
        )
    return hits


def _group(con: sqlite3.Connection, hits: list[dict]) -> list[dict]:
    """按会话分组,保持命中顺序;每会话最多 MAX_HITS_PER_SESSION 条。"""
    order: list[str] = []
    by_sid: dict[str, list[dict]] = {}
    for h in hits:
        sid = h["sid"]
        if sid not in by_sid:
            by_sid[sid] = []
            order.append(sid)
        if len(by_sid[sid]) < MAX_HITS_PER_SESSION:
            by_sid[sid].append(
                {"seq": h["seq"], "kind": h["kind"], "ts": h["ts"], "snippet": h["snip"]}
            )
    groups = []
    for sid in order:
        srow = con.execute(
            "SELECT session_id, title, title_source, cwd, first_ts, last_ts, msg_count,"
            " file_size, slug, archived_at, source_missing, bridge_session_id"
            " FROM sessions WHERE session_id=?",
            (sid,),
        ).fetchone()
        session = dict(srow) if srow is not None else {"session_id": sid}
        session["bridge_url"] = bridge_url(session.get("bridge_session_id"))
        groups.append({"session": session, "hits": by_sid[sid]})
    return groups
