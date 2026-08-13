"""Codex CLI rollout JSONL 解析(本机 codex 0.144 实测,2026-08-13)。

行结构:{timestamp, type, payload}。三类有用行:
- session_meta: payload = {id/session_id, cwd, cli_version, git?, ...}
- event_msg:    payload.type = user_message(真实用户输入,message 字段)
                | token_count(payload.info.last_token_usage = 本轮增量,无状态可累加)
- response_item: payload.type = message 且 role=assistant(权威正文,output_text)

user 行取 event_msg(response_item 的 user 混入 <user_instructions> 等注入噪音);
assistant 行取 response_item(event_msg.agent_message 是它的重复)。
seq 规则:仅对"实际入索引的 user/assistant 行"递增,视图解析用同一规则,深链恒等。
usage 映射:in = input−cached, cache_read = cached, out = output, cache_write = 0。
产出与 Claude 同构的 ChunkResult,索引层无需分支。
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


def _user_text(payload: dict) -> str | None:
    m = payload.get("message")
    if not isinstance(m, str):
        return None
    txt = m.strip()
    if not txt or txt.startswith("<"):
        return None  # <user_instructions>/<environment_context> 等注入块
    return txt


def _assistant_text(payload: dict) -> str | None:
    parts = [
        b["text"]
        for b in payload.get("content") or []
        if isinstance(b, dict) and b.get("type") == "output_text" and isinstance(b.get("text"), str)
    ]
    joined = "\n\n".join(parts).strip()
    return joined or None


def parse_codex_chunk(
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
        payload = d.get("payload") if isinstance(d.get("payload"), dict) else {}

        if ltype == "session_meta":
            if res.cwd is None and isinstance(payload.get("cwd"), str):
                res.cwd = payload["cwd"]
            v = payload.get("cli_version")
            if isinstance(v, str) and v:
                res.version = v
            git = payload.get("git")
            if isinstance(git, dict) and isinstance(git.get("branch"), str):
                res.git_branch = git["branch"]

        elif ltype == "event_msg":
            ptype = payload.get("type")
            if ptype == "user_message":
                txt = _user_text(payload)
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
            elif ptype == "token_count":
                info = payload.get("info")
                last = (info or {}).get("last_token_usage") if isinstance(info, dict) else None
                if isinstance(last, dict):
                    inp = _as_int(last.get("input_tokens"))
                    cached = _as_int(last.get("cached_input_tokens"))
                    vals = (max(0, inp - cached), _as_int(last.get("output_tokens")), cached, 0)
                    res.in_tokens += vals[0]
                    res.out_tokens += vals[1]
                    res.cache_read_tokens += vals[2]
                    if ts and len(ts) >= 13:
                        day = res.usage_daily.setdefault(ts[:10], [0, 0, 0, 0])
                        hour = res.usage_hourly.setdefault(ts[:13], [0, 0, 0, 0])
                        for i in range(4):
                            day[i] += vals[i]
                            hour[i] += vals[i]

        elif ltype == "response_item":
            if payload.get("type") == "message" and payload.get("role") == "assistant":
                txt = _assistant_text(payload)
                if txt:
                    res.msg_count += 1
                    res.rows.append(
                        TextRow(seq, None, ts, "assistant_text", txt[:max_text_chars], line_start)
                    )
                    seq += 1

        res.new_offset = offset

    res.seq_end = seq
    return res


# ---------- 聊天视图(与索引器同一 seq 规则) ----------

_cache_lock = threading.Lock()
_view_cache: dict[str, tuple[int, int, list[dict]]] = {}
_CACHE_MAX = 3


def parse_codex_view_items(path: Path) -> list[dict]:
    from .render import render_markdown  # 延迟导入,避免环

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
            if kind != "obj":
                continue
            ltype = d.get("type")
            payload = d.get("payload") if isinstance(d.get("payload"), dict) else {}
            ts = d.get("timestamp")
            if ltype == "event_msg" and payload.get("type") == "user_message":
                txt = _user_text(payload)
                if txt:
                    items.append(
                        {
                            "seq": seq, "uuid": None, "ts": ts, "role": "user",
                            "hidden_default": False,
                            "blocks": [{"kind": "md_html", "html": render_markdown(txt)}],
                        }
                    )
                    seq += 1
            elif ltype == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant":
                txt = _assistant_text(payload)
                if txt:
                    items.append(
                        {
                            "seq": seq, "uuid": None, "ts": ts, "role": "assistant",
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
