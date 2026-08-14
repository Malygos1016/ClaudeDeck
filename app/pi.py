"""pi agent 会话 JSONL 解析(本机 pi 0.4x 实测,2026-08-13,format version 3)。

行结构:
- {"type":"session", "id", "timestamp", "cwd"} —— 头行,cwd 原样保存
- {"type":"message", "timestamp", "message":{"role":"user"|"assistant"|"toolResult",
   "content":[{type:text|thinking|toolCall,...}], "usage":{input,output,cacheRead,cacheWrite}}}
- model_change / thinking_level_change 等控制行:忽略
seq 规则与 codex 相同:仅对实际入索引的 user/assistant 行递增,视图共用。
resume: `pi --session <uuid>`(cd 到项目目录后按 id 解析)。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import BinaryIO

from .transcript import (
    MAX_TEXT_CHARS,
    ChunkResult,
    TextRow,
    _as_int,
    clean_user_title,
    iter_jsonl,
)


def _texts(msg: dict) -> str | None:
    parts = [
        b["text"]
        for b in msg.get("content") or []
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    ]
    joined = "\n\n".join(p.strip() for p in parts if p.strip()).strip()
    return joined or None


def parse_pi_chunk(
    fp: BinaryIO,
    *,
    start_offset: int = 0,
    start_seq: int = 0,
    max_text_chars: int = MAX_TEXT_CHARS,
) -> ChunkResult:
    res = ChunkResult(new_offset=start_offset, seq_end=start_seq)
    seq = start_seq

    for kind, line_start, d, offset in iter_jsonl(fp, start_offset=start_offset):
        if kind != "obj":
            if kind == "bad":
                res.bad_lines += 1
            res.new_offset = offset
            continue

        ts = d.get("timestamp")
        if not isinstance(ts, str) or not ts:
            ts = None
        if ts:
            if res.first_ts is None:
                res.first_ts = ts
            res.last_ts = ts

        ltype = d.get("type")
        if ltype == "session":
            if res.cwd is None and isinstance(d.get("cwd"), str):
                res.cwd = d["cwd"]

        elif ltype == "message":
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            role = msg.get("role")
            if role == "user":
                txt = _texts(msg)
                if txt:
                    res.msg_count += 1
                    res.rows.append(
                        TextRow(seq, None, ts, "user_text", txt[:max_text_chars], line_start)
                    )
                    seq += 1
                    res.last_prompt = txt[:500]
                    if res.first_user_text is None:
                        cand = clean_user_title(txt)
                        if cand:
                            res.first_user_text = cand
            elif role == "assistant":
                txt = _texts(msg)
                if txt:
                    res.msg_count += 1
                    res.rows.append(
                        TextRow(seq, None, ts, "assistant_text", txt[:max_text_chars], line_start)
                    )
                    seq += 1
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    vals = (
                        _as_int(usage.get("input")),
                        _as_int(usage.get("output")),
                        _as_int(usage.get("cacheRead")),
                        _as_int(usage.get("cacheWrite")),
                    )
                    res.in_tokens += vals[0]
                    res.out_tokens += vals[1]
                    res.cache_read_tokens += vals[2]
                    res.cache_write_tokens += vals[3]
                    if ts and len(ts) >= 13:
                        day = res.usage_daily.setdefault(ts[:10], [0, 0, 0, 0])
                        hour = res.usage_hourly.setdefault(ts[:13], [0, 0, 0, 0])
                        for i in range(4):
                            day[i] += vals[i]
                            hour[i] += vals[i]
            # toolResult 等其余角色:不入索引

        res.new_offset = offset

    res.seq_end = seq
    return res


# ---------- 聊天视图(与索引器同一 seq 规则) ----------

_cache_lock = threading.Lock()
_view_cache: dict[str, tuple[int, int, list[dict]]] = {}
_CACHE_MAX = 3


def parse_pi_view_items(path: Path) -> list[dict]:
    from .render import render_markdown

    st = path.stat()
    key = str(path)
    with _cache_lock:
        hit = _view_cache.get(key)
        if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            return hit[2]

    items: list[dict] = []
    seq = 0
    with open(path, "rb") as fp:
        for kind, _line_start, d, _offset in iter_jsonl(fp):
            if kind != "obj" or d.get("type") != "message":
                continue
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            txt = _texts(msg)
            if not txt:
                continue
            items.append(
                {
                    "seq": seq, "uuid": None, "ts": d.get("timestamp"), "role": role,
                    "hidden_default": False,
                    "blocks": [{"kind": "md_html", "html": render_markdown(txt)}],
                }
            )
            seq += 1

    with _cache_lock:
        if len(_view_cache) >= _CACHE_MAX and key not in _view_cache:
            _view_cache.pop(next(iter(_view_cache)))
        _view_cache[key] = (st.st_mtime_ns, st.st_size, items)
    return items
