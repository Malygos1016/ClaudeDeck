"""聊天视图:transcript 行 → 视图模型 + Markdown 服务端渲染。

真相源是 jsonl 原文件(活文件优先,源缺失落归档副本)。seq 编号与索引器同规则
(所有带 uuid 的行按行序编号),搜索命中的 seq 在这里恒等可寻。
解析结果按 (mtime_ns, size) 缓存,大文件只付一次全解析成本。
"""
from __future__ import annotations

import html
import json
import re
import threading
from pathlib import Path

from markdown_it import MarkdownIt

from . import archive as archive_mod
from .config import Config

_md = MarkdownIt("commonmark", {"html": False, "breaks": False}).enable("table").enable(
    "strikethrough"
)

TOOL_SUMMARY_CHARS = 200
TOOL_DETAIL_CHARS = 10_000
TOOL_RESULT_CHARS = 50_000
THINKING_CHARS = 50_000

_CMD_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_CMD_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_CMD_OUT_RE = re.compile(r"<local-command-stdout>(.*?)</local-command-stdout>", re.S)


def render_markdown(text: str) -> str:
    return _md.render(text)


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"\n…(截断,原长 {len(s):,} 字符)"


def _tool_result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text" and isinstance(b.get("text"), str):
                    parts.append(b["text"])
                else:
                    parts.append(json.dumps(b, ensure_ascii=False, indent=1))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False, indent=1)


def _line_to_item(d: dict, seq: int) -> dict | None:
    """一条带 uuid 的行 → 视图项。返回 None 表示这行没有可显示内容。"""
    mtype = d.get("type")
    ts = d.get("timestamp")
    base = {"seq": seq, "uuid": d.get("uuid"), "ts": ts, "role": mtype, "hidden_default": False}
    msg = d.get("message") if isinstance(d.get("message"), dict) else {}

    if mtype == "user":
        blocks: list[dict] = []
        content = msg.get("content")
        if isinstance(content, str):
            cmd = _CMD_NAME_RE.search(content)
            if cmd:
                args = _CMD_ARGS_RE.search(content)
                out = _CMD_OUT_RE.search(content)
                blocks.append(
                    {
                        "kind": "command",
                        "name": cmd.group(1).strip(),
                        "args": (args.group(1).strip() if args else "")[:400],
                        "stdout": (out.group(1).strip() if out else "")[:2000],
                    }
                )
            elif d.get("isCompactSummary"):
                blocks.append({"kind": "compact_summary", "text": _clip(content, TOOL_RESULT_CHARS)})
            else:
                blocks.append({"kind": "md_html", "html": render_markdown(content)})
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text" and isinstance(b.get("text"), str):
                    blocks.append({"kind": "md_html", "html": render_markdown(b["text"])})
                elif bt == "tool_result":
                    blocks.append(
                        {
                            "kind": "tool_result",
                            "is_error": bool(b.get("is_error")),
                            "text": _clip(_tool_result_text(b.get("content")), TOOL_RESULT_CHARS),
                        }
                    )
        if not blocks:
            return None
        # 纯工具回执行整体视作工具轨道(默认可见但折叠)
        base["role"] = "user" if any(x["kind"] in ("md_html", "command", "compact_summary") for x in blocks) else "tool"
        base["blocks"] = blocks
        return base

    if mtype == "assistant":
        blocks = []
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text" and isinstance(b.get("text"), str) and b["text"].strip():
                    blocks.append({"kind": "md_html", "html": render_markdown(b["text"])})
                elif bt == "thinking":
                    t = b.get("thinking")
                    if isinstance(t, str) and t.strip():
                        blocks.append({"kind": "thinking", "text": _clip(t, THINKING_CHARS)})
                elif bt == "tool_use":
                    inp = b.get("input")
                    try:
                        detail = json.dumps(inp, ensure_ascii=False, indent=1)
                    except (TypeError, ValueError):
                        detail = str(inp)
                    blocks.append(
                        {
                            "kind": "tool_use",
                            "name": str(b.get("name") or "?"),
                            "summary": detail.replace("\n", " ")[:TOOL_SUMMARY_CHARS],
                            "detail": _clip(detail, TOOL_DETAIL_CHARS),
                        }
                    )
        if not blocks:
            return None
        base["blocks"] = blocks
        return base

    if mtype == "system":
        subtype = d.get("subtype")
        if subtype == "compact_boundary":
            meta = d.get("compactMetadata") if isinstance(d.get("compactMetadata"), dict) else {}
            base["blocks"] = [
                {
                    "kind": "compact_boundary",
                    "pre": meta.get("preTokens"),
                    "post": meta.get("postTokens"),
                    "trigger": meta.get("trigger"),
                }
            ]
            return base  # 永远显示
        base["hidden_default"] = True
        content = d.get("content")
        base["blocks"] = [
            {
                "kind": "system",
                "subtype": str(subtype or ""),
                "text": _clip(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False), 2000),
            }
        ]
        return base

    if mtype == "attachment":
        att = d.get("attachment") if isinstance(d.get("attachment"), dict) else {}
        atype = str(att.get("type") or "")
        base["hidden_default"] = True
        text = ""
        if atype == "queued_command" and isinstance(att.get("prompt"), str):
            text = att["prompt"][:2000]
        base["blocks"] = [{"kind": "attachment", "subtype": atype, "text": text}]
        return base

    return None


# ---------- 全文件解析(带缓存) ----------

_cache_lock = threading.Lock()
_view_cache: dict[str, tuple[int, int, list[dict]]] = {}
_CACHE_MAX = 3


def parse_view_items(path: Path, max_line_bytes: int = 8 * 1024 * 1024) -> list[dict]:
    st = path.stat()
    key = str(path)
    with _cache_lock:
        hit = _view_cache.get(key)
        if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            return hit[2]

    items: list[dict] = []
    seq = 0
    with open(path, "rb") as fp:
        while True:
            line = fp.readline(max_line_bytes)
            if not line:
                break
            if not line.endswith(b"\n"):
                if len(line) >= max_line_bytes:
                    # 超长行:吞掉但保住 seq 对齐(indexer 同样丢弃此行,不计 seq——
                    # 它没有被 json 解析,拿不到 uuid,双方一致跳过)
                    while True:
                        part = fp.readline(max_line_bytes)
                        if not part or part.endswith(b"\n"):
                            break
                    continue
                break  # 尾部半行
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                continue
            if not isinstance(d, dict) or "uuid" not in d:
                continue
            item = _line_to_item(d, seq)
            seq += 1
            if item is not None:
                items.append(item)

    with _cache_lock:
        if len(_view_cache) >= _CACHE_MAX and key not in _view_cache:
            _view_cache.pop(next(iter(_view_cache)))
        _view_cache[key] = (st.st_mtime_ns, st.st_size, items)
    return items


def resolve_transcript_path(cfg: Config, proj_dir: str, session_id: str) -> tuple[Path, str] | None:
    """活文件优先;源缺失落归档副本。返回 (路径, 'live'|'archive')。"""
    live = cfg.projects_root / proj_dir / f"{session_id}.jsonl"
    if live.is_file():
        return live, "live"
    arch = archive_mod.archived_main_path(cfg, proj_dir, session_id)
    if arch.is_file():
        return arch, "archive"
    return None


def window(
    items: list[dict],
    *,
    limit: int = 80,
    around_seq: int | None = None,
    before_seq: int | None = None,
    after_seq: int | None = None,
    show_system: bool = False,
) -> dict:
    """在全量视图项上取一个窗口(默认取尾部=聊天最新一屏)。"""
    total = len(items)
    if total == 0:
        return {
            "items": [], "first_seq": None, "last_seq": None,
            "has_more_before": False, "has_more_after": False, "total_items": 0,
        }
    seqs = [it["seq"] for it in items]

    def locate(target: int) -> int:
        # 第一个 seq >= target 的下标
        lo, hi = 0, total
        while lo < hi:
            mid = (lo + hi) // 2
            if seqs[mid] < target:
                lo = mid + 1
            else:
                hi = mid
        return lo

    if around_seq is not None:
        c = min(locate(around_seq), total - 1)
        start = max(0, c - limit // 2)
        end = min(total, start + limit)
    elif before_seq is not None:
        end = locate(before_seq)
        start = max(0, end - limit)
    elif after_seq is not None:
        start = locate(after_seq + 1)
        end = min(total, start + limit)
    else:
        end = total
        start = max(0, end - limit)

    win = items[start:end]
    out = [it for it in win if show_system or not it["hidden_default"]]
    return {
        "items": out,
        "first_seq": win[0]["seq"] if win else None,
        "last_seq": win[-1]["seq"] if win else None,
        "has_more_before": start > 0,
        "has_more_after": end < total,
        "total_items": total,
    }


def export_markdown(items: list[dict], session: dict) -> str:
    """行序导出 Markdown:工具调用一行摘要,thinking 略。"""
    lines = [
        "---",
        f"title: {session.get('title') or '(无标题)'}",
        f"session_id: {session.get('session_id')}",
        f"cwd: {session.get('cwd')}",
        f"span: {session.get('first_ts')} → {session.get('last_ts')}",
        "exported_by: ClaudeDeck",
        "---",
        "",
    ]
    for it in items:
        role = it["role"]
        if it.get("hidden_default"):
            continue
        for b in it["blocks"]:
            k = b["kind"]
            if k == "md_html":
                who = "你" if role == "user" else "Claude"
                lines.append(f"## {who}({it.get('ts') or ''})")
                lines.append(_html_to_text(b["html"]))
                lines.append("")
            elif k == "tool_use":
                lines.append(f"> 🔧 {b['name']}: {b['summary'][:120]}")
            elif k == "tool_result":
                head = (b["text"] or "").split("\n", 1)[0][:120]
                lines.append(f"> ↳ 结果: {head}")
            elif k == "command":
                lines.append(f"> ⌘ {b['name']} {b['args']}")
            elif k == "compact_boundary":
                lines.append(f"---\n*(上下文已压缩 {b.get('pre')} → {b.get('post')} tokens)*\n---")
    return "\n".join(lines)


_TAG_STRIP_RE = re.compile(r"<[^>]+>")


def _html_to_text(h: str) -> str:
    return html.unescape(_TAG_STRIP_RE.sub("", h)).strip()
