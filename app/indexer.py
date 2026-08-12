"""索引编排:扫描 → 解析 → 写库 → 归档快照。

索引线程是数据库唯一常态写入者;scan_once 由锁串行化(定时循环与手动触发并发安全)。
数据真相源永远是 ~/.claude 下的文件,本库只是缓存;归档目录是唯一额外持久数据。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import archive as archive_mod
from .config import Config
from .scanner import (
    MainFile,
    list_main_files,
    list_subagent_metas,
    munge_path,
    walk_companion,
)
from .transcript import ChunkResult, parse_chunk

HISTORY_SESSION_KEY = "<history>"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ms_to_iso(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z"
    )


class Indexer:
    def __init__(self, cfg: Config, con: sqlite3.Connection):
        self.cfg = cfg
        self.con = con
        self.lock = threading.Lock()
        self.status: dict = {
            "phase": "idle",
            "files_total": 0,
            "files_done": 0,
            "current": None,
            "started_at": None,
            "finished_at": None,
            "last_stats": None,
        }

    # ---------- 对外入口 ----------

    def scan_once(
        self, force: bool = False, progress_cb: Callable[[int, int, str], None] | None = None
    ) -> dict:
        with self.lock:
            t0 = time.monotonic()
            stats: dict = {
                "files_seen": 0,
                "files_parsed": 0,
                "files_skipped": 0,
                "rows_indexed": 0,
                "bad_lines": 0,
                "archived_copies": 0,
                "archive_only_sessions": 0,
                "history_new_lines": 0,
                "file_errors": 0,
                "errors": [],
            }
            mains = list_main_files(self.cfg.projects_root)
            stats["files_seen"] = len(mains)
            self.status.update(
                phase="scanning",
                files_total=len(mains),
                files_done=0,
                started_at=now_iso(),
                finished_at=None,
            )
            live_sids = {m.session_id for m in mains}

            for mf in mains:
                self.status["current"] = f"{mf.proj_dir}/{mf.path.name}"
                try:
                    self._index_main(mf, force=force, stats=stats, archived=False)
                    self.con.commit()
                except Exception as e:  # 单文件失败不拖垮整轮,计数并保留错误样本
                    self.con.rollback()
                    stats["file_errors"] += 1
                    if len(stats["errors"]) < 10:
                        stats["errors"].append(f"{mf.path.name}: {e!r}")
                self.status["files_done"] += 1
                if progress_cb:
                    progress_cb(self.status["files_done"], len(mains), mf.path.name)

            try:
                archived_paths = self._index_archive_only(live_sids, force=force, stats=stats)
                self._ingest_history(stats)
                self._prune_stale_files(
                    {str(m.path) for m in mains} | archived_paths
                )
                self.con.commit()
            except Exception as e:
                self.con.rollback()
                stats["file_errors"] += 1
                if len(stats["errors"]) < 10:
                    stats["errors"].append(f"archive/history: {e!r}")

            stats["elapsed_s"] = round(time.monotonic() - t0, 3)
            self.status.update(
                phase="idle", current=None, finished_at=now_iso(), last_stats=stats
            )
            return stats

    def force_archive(self, session_id: str) -> dict:
        """手动"立即归档":无视安静期。返回 {archived_path, companion_copied}。"""
        with self.lock:
            mf = self._find_live_main(session_id)
            if mf is None:
                raise FileNotFoundError(f"没有该会话的源文件: {session_id}")
            dest = archive_mod.snapshot_main(self.cfg, mf)
            copied = archive_mod.mirror_companion(self.cfg, mf)
            st = mf.path.stat()
            self.con.execute(
                "UPDATE files SET archived_mtime_ns=?, archived_size=? WHERE path=?",
                (st.st_mtime_ns, st.st_size, str(mf.path)),
            )
            self.con.execute(
                "UPDATE sessions SET archived_at=? WHERE session_id=?",
                (now_iso(), session_id),
            )
            self.con.commit()
            return {"archived_path": str(dest), "companion_copied": copied}

    # ---------- 主 transcript ----------

    def _find_live_main(self, session_id: str) -> MainFile | None:
        for mf in list_main_files(self.cfg.projects_root):
            if mf.session_id == session_id:
                return mf
        return None

    def _index_main(self, mf: MainFile, *, force: bool, stats: dict, archived: bool) -> None:
        row = self.con.execute("SELECT * FROM files WHERE path=?", (str(mf.path),)).fetchone()
        fresh = force or row is None
        if row is not None and not force:
            if row["mtime_ns"] == mf.mtime_ns and row["size"] == mf.size:
                stats["files_skipped"] += 1
                if not archived:
                    self._refresh_companion(mf)
                    self._maybe_archive(mf, stats)
                return
            if mf.size < (row["parsed_offset"] or 0):
                fresh = True  # 文件被重写/截断 → 全量重扫

        start_offset = 0 if fresh else (row["parsed_offset"] or 0)
        start_seq = 0 if fresh else (row["seq_end"] or 0)
        with open(mf.path, "rb") as fp:
            chunk = parse_chunk(fp, start_offset=start_offset, start_seq=start_seq)

        self._apply_chunk(mf, chunk, fresh=fresh, prev=row, archived=archived)
        stats["files_parsed"] += 1
        stats["rows_indexed"] += len(chunk.rows)
        stats["bad_lines"] += chunk.bad_lines
        if not archived:
            self._refresh_companion(mf)
            self._maybe_archive(mf, stats)

    def _apply_chunk(
        self,
        mf: MainFile,
        chunk: ChunkResult,
        *,
        fresh: bool,
        prev: sqlite3.Row | None,
        archived: bool,
    ) -> None:
        sid = mf.session_id
        srow = self.con.execute(
            "SELECT * FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()

        if fresh:
            self._delete_session_messages(sid)
            self.con.execute("DELETE FROM usage_daily WHERE session_id=?", (sid,))

        # 正文行入库 + FTS
        cur = self.con.cursor()
        pending_fts: list[tuple[int, str]] = []
        for r in chunk.rows:
            cur.execute(
                "INSERT INTO messages(session_id, uuid, seq, byte_offset, ts, kind, text) "
                "VALUES(?,?,?,?,?,?,?)",
                (sid, r.uuid, r.seq, r.byte_offset, r.ts, r.kind, r.text),
            )
            pending_fts.append((cur.lastrowid, r.text))
        cur.executemany("INSERT INTO messages_fts(rowid, text) VALUES(?,?)", pending_fts)

        # 会话元数据合并(旧值 + 本段增量)
        old = srow if (srow is not None and not fresh) else None
        title, title_source = self._merge_title(old, chunk)
        merged = {
            "session_id": sid,
            "proj_dir": mf.proj_dir,
            "cwd": (old["cwd"] if old and old["cwd"] else None) or chunk.cwd,
            "title": title,
            "title_source": title_source,
            "last_prompt": chunk.last_prompt or (old["last_prompt"] if old else None),
            "slug": chunk.slug or (old["slug"] if old else None),
            "first_ts": (old["first_ts"] if old and old["first_ts"] else None) or chunk.first_ts,
            "last_ts": chunk.last_ts or (old["last_ts"] if old else None),
            "msg_count": chunk.msg_count + (old["msg_count"] if old else 0),
            "version": chunk.version or (old["version"] if old else None),
            "git_branch": chunk.git_branch or (old["git_branch"] if old else None),
            "bridge_session_id": chunk.bridge_session_id
            or (old["bridge_session_id"] if old else None),
            "in_tokens": chunk.in_tokens + (old["in_tokens"] if old else 0),
            "out_tokens": chunk.out_tokens + (old["out_tokens"] if old else 0),
            "cache_read_tokens": chunk.cache_read_tokens
            + (old["cache_read_tokens"] if old else 0),
            "cache_write_tokens": chunk.cache_write_tokens
            + (old["cache_write_tokens"] if old else 0),
            "has_compact": 1 if (chunk.has_compact or (old and old["has_compact"])) else 0,
            "source_missing": 1 if archived else 0,
            "updated_at": now_iso(),
        }
        self._upsert_session(merged)

        # 按日 usage(续读为增量累加;全量重扫上面已清零)
        for day, v in chunk.usage_daily.items():
            self.con.execute(
                "INSERT INTO usage_daily(session_id, date, in_tokens, out_tokens,"
                " cache_read_tokens, cache_write_tokens) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(session_id, date) DO UPDATE SET"
                " in_tokens=in_tokens+excluded.in_tokens,"
                " out_tokens=out_tokens+excluded.out_tokens,"
                " cache_read_tokens=cache_read_tokens+excluded.cache_read_tokens,"
                " cache_write_tokens=cache_write_tokens+excluded.cache_write_tokens",
                (sid, day, v[0], v[1], v[2], v[3]),
            )

        # 标题与末次输入进 FTS(kind 唯一,upsert)
        if title_source == "ai-title" and title:
            self._upsert_meta_row(sid, "ai_title", title, seq=-1)
        if merged["last_prompt"]:
            self._upsert_meta_row(sid, "last_prompt", merged["last_prompt"], seq=-2)

        # files 状态行
        bad_total = chunk.bad_lines + (0 if fresh or prev is None else (prev["bad_lines"] or 0))
        self.con.execute(
            "INSERT INTO files(path, session_id, mtime_ns, size, parsed_offset, seq_end,"
            " bad_lines, last_scan_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET session_id=excluded.session_id,"
            " mtime_ns=excluded.mtime_ns, size=excluded.size,"
            " parsed_offset=excluded.parsed_offset, seq_end=excluded.seq_end,"
            " bad_lines=excluded.bad_lines, last_scan_at=excluded.last_scan_at",
            (
                str(mf.path),
                sid,
                mf.mtime_ns,
                mf.size,
                chunk.new_offset,
                chunk.seq_end,
                bad_total,
                now_iso(),
            ),
        )

    def _merge_title(
        self, old: sqlite3.Row | None, chunk: ChunkResult
    ) -> tuple[str | None, str | None]:
        """优先级: ai-title > last-prompt(新者胜) > first-user(旧者胜) > history。"""
        if chunk.ai_title:
            return chunk.ai_title, "ai-title"
        if old is not None and old["title_source"] == "ai-title":
            return old["title"], "ai-title"
        if chunk.last_prompt:
            return chunk.last_prompt[:200], "last-prompt"
        if old is not None and old["title_source"] == "last-prompt":
            return old["title"], "last-prompt"
        if old is not None and old["title_source"] == "first-user":
            return old["title"], "first-user"
        if chunk.first_user_text:
            return chunk.first_user_text, "first-user"
        if old is not None and old["title"]:
            return old["title"], old["title_source"]
        return None, None

    def _upsert_session(self, vals: dict) -> None:
        # archived_at 不在 vals 中,UPDATE 不触碰 → 自然保留
        cols = ", ".join(vals.keys())
        ph = ", ".join("?" for _ in vals)
        updates = ", ".join(f"{k}=excluded.{k}" for k in vals if k != "session_id")
        self.con.execute(
            f"INSERT INTO sessions({cols}) VALUES({ph}) "
            f"ON CONFLICT(session_id) DO UPDATE SET {updates}",
            tuple(vals.values()),
        )

    def _upsert_meta_row(self, sid: str, kind: str, text: str, *, seq: int) -> None:
        row = self.con.execute(
            "SELECT id, text FROM messages WHERE session_id=? AND kind=?", (sid, kind)
        ).fetchone()
        if row is not None:
            if row["text"] == text:
                return
            self.con.execute(
                "INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', ?, ?)",
                (row["id"], row["text"]),
            )
            self.con.execute("UPDATE messages SET text=? WHERE id=?", (text, row["id"]))
            self.con.execute(
                "INSERT INTO messages_fts(rowid, text) VALUES(?, ?)", (row["id"], text)
            )
        else:
            cur = self.con.execute(
                "INSERT INTO messages(session_id, uuid, seq, byte_offset, ts, kind, text) "
                "VALUES(?, NULL, ?, NULL, NULL, ?, ?)",
                (sid, seq, kind, text),
            )
            self.con.execute(
                "INSERT INTO messages_fts(rowid, text) VALUES(?, ?)", (cur.lastrowid, text)
            )

    def _delete_session_messages(self, sid: str, kind: str | None = None) -> None:
        """删除会话消息并同步 FTS(外部内容表必须携带旧文本执行 'delete')。"""
        if kind is None:
            rows = self.con.execute(
                "SELECT id, text FROM messages WHERE session_id=?", (sid,)
            ).fetchall()
        else:
            rows = self.con.execute(
                "SELECT id, text FROM messages WHERE session_id=? AND kind=?", (sid, kind)
            ).fetchall()
        for r in rows:
            self.con.execute(
                "INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', ?, ?)",
                (r["id"], r["text"]),
            )
        if kind is None:
            self.con.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        else:
            self.con.execute(
                "DELETE FROM messages WHERE session_id=? AND kind=?", (sid, kind)
            )

    # ---------- 伴生目录 / 子 agent ----------

    def _refresh_companion(self, mf: MainFile) -> None:
        total, _ = walk_companion(mf.companion_dir)
        metas = list_subagent_metas(mf.companion_dir)
        for m in metas:
            info = {}
            try:
                info = json.loads(m.meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
            existing = self.con.execute(
                "SELECT file_size FROM subagents WHERE agent_id=?", (m.agent_id,)
            ).fetchone()
            if existing is not None and existing["file_size"] == m.jsonl_size:
                continue
            self.con.execute(
                "INSERT INTO subagents(agent_id, session_id, agent_type, description,"
                " tool_use_id, spawn_depth, file_path, file_size) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(agent_id) DO UPDATE SET file_path=excluded.file_path,"
                " file_size=excluded.file_size",
                (
                    m.agent_id,
                    mf.session_id,
                    info.get("agentType"),
                    info.get("description"),
                    info.get("toolUseId"),
                    info.get("spawnDepth"),
                    str(m.jsonl_path) if m.jsonl_path else None,
                    m.jsonl_size,
                ),
            )
        file_size = mf.size + total
        self.con.execute(
            "UPDATE sessions SET file_size=?, subagent_count=? "
            "WHERE session_id=? AND (file_size<>? OR subagent_count<>?)",
            (file_size, len(metas), mf.session_id, file_size, len(metas)),
        )

    # ---------- 归档 ----------

    def _maybe_archive(self, mf: MainFile, stats: dict) -> None:
        try:
            st = mf.path.stat()
        except OSError:
            return
        if not archive_mod.is_quiet(st.st_mtime_ns, self.cfg.archive_quiet_minutes):
            return
        row = self.con.execute(
            "SELECT archived_mtime_ns, archived_size FROM files WHERE path=?", (str(mf.path),)
        ).fetchone()
        if (
            row is not None
            and row["archived_mtime_ns"] == st.st_mtime_ns
            and row["archived_size"] == st.st_size
        ):
            archive_mod.mirror_companion(self.cfg, mf)  # 伴生目录仍可能有新文件
            return
        archive_mod.snapshot_main(self.cfg, mf)
        archive_mod.mirror_companion(self.cfg, mf)
        stats["archived_copies"] += 1
        self.con.execute(
            "UPDATE files SET archived_mtime_ns=?, archived_size=? WHERE path=?",
            (st.st_mtime_ns, st.st_size, str(mf.path)),
        )
        self.con.execute(
            "UPDATE sessions SET archived_at=? WHERE session_id=?",
            (now_iso(), mf.session_id),
        )

    def _index_archive_only(self, live_sids: set[str], *, force: bool, stats: dict) -> set[str]:
        """源已被官方清理、仅存归档副本的会话:照常入索引,标记 source_missing。

        返回归档区所有主文件路径(供 stale files 行清理)。
        """
        paths: set[str] = set()
        for amf in list_main_files(self.cfg.archive_projects_root):
            paths.add(str(amf.path))
            if amf.session_id in live_sids:
                # 活文件权威;清掉还原后遗留的归档路径状态行
                self.con.execute(
                    "DELETE FROM files WHERE path=? AND session_id=?",
                    (str(amf.path), amf.session_id),
                )
                continue
            self._index_main(amf, force=force, stats=stats, archived=True)
            self.con.execute(
                "UPDATE sessions SET archived_at=COALESCE(archived_at, ?) WHERE session_id=?",
                (now_iso(), amf.session_id),
            )
            stats["archive_only_sessions"] += 1
        return paths

    def _prune_stale_files(self, existing_paths: set[str]) -> None:
        """清掉源与归档都已消失的 files 状态行,并把失去全部文件的会话标记 source_missing。"""
        rows = self.con.execute(
            "SELECT path, session_id FROM files WHERE session_id <> ?", (HISTORY_SESSION_KEY,)
        ).fetchall()
        for r in rows:
            if r["path"] in existing_paths:
                continue
            self.con.execute("DELETE FROM files WHERE path=?", (r["path"],))
            left = self.con.execute(
                "SELECT 1 FROM files WHERE session_id=? LIMIT 1", (r["session_id"],)
            ).fetchone()
            if left is None:
                self.con.execute(
                    "UPDATE sessions SET source_missing=1, updated_at=? WHERE session_id=?",
                    (now_iso(), r["session_id"]),
                )

    # ---------- history.jsonl ----------

    def _ingest_history(self, stats: dict) -> None:
        """全局输入历史:为已无 transcript 也无归档的会话合成最小可搜条目。"""
        hist = self.cfg.claude_home_path / "history.jsonl"
        if not hist.is_file():
            return
        row = self.con.execute("SELECT * FROM files WHERE path=?", (str(hist),)).fetchone()
        st = hist.stat()
        offset = row["parsed_offset"] if row is not None else 0
        if st.st_size < (offset or 0):
            offset = 0  # 被重写,重来
            self._delete_all_history_rows()
        if row is not None and st.st_size == offset:
            return

        # transcript(活或归档)权威 —— 即使标题为空(0 字节文件)也不许 history 覆盖
        known = {
            r["session_id"]
            for r in self.con.execute(
                "SELECT session_id FROM sessions"
                " WHERE title_source IS NULL OR title_source <> 'history'"
            ).fetchall()
        }
        new_lines = 0
        agg: dict[str, dict] = {}
        with open(hist, "rb") as fp:
            fp.seek(offset or 0)
            pos = offset or 0
            while True:
                line = fp.readline(1024 * 1024)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    break  # 半行不消费
                pos += len(line)
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(d, dict):
                    continue
                sid = str(d.get("sessionId") or "").lower()
                display = d.get("display")
                if not sid or not isinstance(display, str) or not display.strip():
                    continue
                new_lines += 1
                if sid in known:
                    continue  # transcript(或归档)权威,不合成
                display = display.strip()
                parts = display.split(maxsplit=1)
                if display.startswith("/") and len(parts) == 1:
                    continue  # 纯斜杠命令(/login 等)是噪音
                a = agg.setdefault(
                    sid,
                    {"prompts": [], "project": None, "first_ms": None, "last_ms": None},
                )
                ts_ms = d.get("timestamp")
                if isinstance(ts_ms, (int, float)):
                    ts_ms = int(ts_ms)
                    a["first_ms"] = ts_ms if a["first_ms"] is None else min(a["first_ms"], ts_ms)
                    a["last_ms"] = ts_ms if a["last_ms"] is None else max(a["last_ms"], ts_ms)
                else:
                    ts_ms = None
                proj = d.get("project")
                if isinstance(proj, str) and proj:
                    a["project"] = proj
                a["prompts"].append((display, ts_ms))

        for sid, a in agg.items():
            old = self.con.execute(
                "SELECT * FROM sessions WHERE session_id=?", (sid,)
            ).fetchone()
            if old is not None and old["title_source"] != "history":
                continue  # 任何 transcript 支撑的会话(含 title 为空者)都不合成
            last_display = a["prompts"][-1][0] if a["prompts"] else None
            vals = {
                "session_id": sid,
                "proj_dir": munge_path(a["project"]) if a["project"] else "",
                "cwd": a["project"] or (old["cwd"] if old else None),
                "title": (last_display or (old["title"] if old else None) or "")[:80] or None,
                "title_source": "history",
                "first_ts": ms_to_iso(a["first_ms"])
                if a["first_ms"] is not None
                else (old["first_ts"] if old else None),
                "last_ts": ms_to_iso(a["last_ms"])
                if a["last_ms"] is not None
                else (old["last_ts"] if old else None),
                "msg_count": len(a["prompts"]) + (old["msg_count"] if old else 0),
                "source_missing": 1,
                "updated_at": now_iso(),
            }
            self._upsert_session(vals)
            for display, ts_ms in a["prompts"]:
                cur = self.con.execute(
                    "INSERT INTO messages(session_id, uuid, seq, byte_offset, ts, kind, text) "
                    "VALUES(?, NULL, -3, NULL, ?, 'history_prompt', ?)",
                    (sid, ms_to_iso(ts_ms) if ts_ms is not None else None, display),
                )
                self.con.execute(
                    "INSERT INTO messages_fts(rowid, text) VALUES(?, ?)",
                    (cur.lastrowid, display),
                )

        stats["history_new_lines"] += new_lines
        self.con.execute(
            "INSERT INTO files(path, session_id, mtime_ns, size, parsed_offset, seq_end,"
            " bad_lines, last_scan_at) VALUES(?,?,?,?,?,0,0,?) "
            "ON CONFLICT(path) DO UPDATE SET mtime_ns=excluded.mtime_ns,"
            " size=excluded.size, parsed_offset=excluded.parsed_offset,"
            " last_scan_at=excluded.last_scan_at",
            (str(hist), HISTORY_SESSION_KEY, st.st_mtime_ns, st.st_size, pos, now_iso()),
        )

    def _delete_all_history_rows(self) -> None:
        rows = self.con.execute(
            "SELECT id, text FROM messages WHERE kind='history_prompt'"
        ).fetchall()
        for r in rows:
            self.con.execute(
                "INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', ?, ?)",
                (r["id"], r["text"]),
            )
        self.con.execute("DELETE FROM messages WHERE kind='history_prompt'")
