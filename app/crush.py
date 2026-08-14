"""Crush(charmbracelet)会话读取(本机 crush 0.13x 实测,2026-08-13)。

数据形态与其他 provider 不同:每个项目一个 SQLite 库(<项目>\\.crush\\crush.db),
项目总名册在 %LOCALAPPDATA%\\crush\\projects.json。只读访问(mode=ro URI),
绝不写它的库。

- sessions 表自带 title(crush 自己生成)与 prompt/completion tokens;
  created_at/updated_at 实测是"秒"(schema 注释写 ms,是它注释错了,按量级自适应)。
- messages.parts 是 JSON 数组,text 部件在 data.text。
- id 含 "$$call_..." 后缀的是工具调用派生的子会话,一并索引(有独立标题与正文)。
- resume: crush 没有命令行级 resume,只能 cd 到项目目录开 crush 后在列表里选。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def list_crush_projects(projects_json: Path) -> list[tuple[str, Path]]:
    """[(项目 cwd, crush.db 路径)]。名册不存在/坏掉返回空。"""
    try:
        data = json.loads(projects_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    out: list[tuple[str, Path]] = []
    for p in data.get("projects") or []:
        if not isinstance(p, dict):
            continue
        cwd, data_dir = p.get("path"), p.get("data_dir")
        if isinstance(cwd, str) and isinstance(data_dir, str):
            db = Path(data_dir) / "crush.db"
            if db.is_file():
                out.append((cwd, db))
    return out


def _ts_iso(v) -> str | None:
    """秒或毫秒的 Unix 时间戳 → ISO;量级自适应(它的 schema 注释与实测不符)。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v > 1e12:
        v /= 1000.0
    return (
        datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    )


def _parts_text(parts_json: str) -> str | None:
    try:
        parts = json.loads(parts_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parts, list):
        return None
    texts = [
        p["data"]["text"]
        for p in parts
        if isinstance(p, dict)
        and p.get("type") == "text"
        and isinstance(p.get("data"), dict)
        and isinstance(p["data"].get("text"), str)
    ]
    joined = "\n\n".join(t.strip() for t in texts if t.strip()).strip()
    return joined or None


def read_crush_db(db_path: Path) -> list[dict]:
    """一个 crush.db → 会话列表(含消息行)。锁冲突/损坏交由调用方按文件计错。

    返回元素: {session_id, title, updated_at_raw, first_ts, last_ts, msg_count,
               in_tokens, out_tokens, rows: [(seq, ts, kind, text), ...]}
    """
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=2000")
    try:
        sessions = con.execute(
            "SELECT id, title, message_count, prompt_tokens, completion_tokens,"
            " created_at, updated_at FROM sessions"
        ).fetchall()
        out = []
        for s in sessions:
            rows: list[tuple[int, str | None, str, str]] = []
            seq = 0
            for m in con.execute(
                "SELECT role, parts, created_at FROM messages WHERE session_id=?"
                " ORDER BY created_at, id",
                (s["id"],),
            ):
                if m["role"] not in ("user", "assistant"):
                    continue
                txt = _parts_text(m["parts"])
                if not txt:
                    continue
                kind = "user_text" if m["role"] == "user" else "assistant_text"
                rows.append((seq, _ts_iso(m["created_at"]), kind, txt))
                seq += 1
            out.append(
                {
                    "session_id": s["id"],
                    "title": (s["title"] or "").strip() or None,
                    "updated_at_raw": int(s["updated_at"] or 0),
                    "first_ts": _ts_iso(s["created_at"]),
                    "last_ts": _ts_iso(s["updated_at"]),
                    "msg_count": len(rows),
                    "in_tokens": int(s["prompt_tokens"] or 0),
                    "out_tokens": int(s["completion_tokens"] or 0),
                    "rows": rows,
                }
            )
        return out
    finally:
        con.close()
