"""SQLite 连接与 schema。数据库是纯缓存:损坏或版本不符直接删库重建,无数据损失。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = "1"

DDL = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  proj_dir TEXT NOT NULL,
  cwd TEXT,
  title TEXT,
  title_source TEXT,
  last_prompt TEXT,
  slug TEXT,
  first_ts TEXT,
  last_ts TEXT,
  msg_count INTEGER DEFAULT 0,
  file_size INTEGER DEFAULT 0,
  version TEXT,
  git_branch TEXT,
  bridge_session_id TEXT,
  in_tokens INTEGER DEFAULT 0,
  out_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0,
  cache_write_tokens INTEGER DEFAULT 0,
  subagent_count INTEGER DEFAULT 0,
  has_compact INTEGER DEFAULT 0,
  archived_at TEXT,
  source_missing INTEGER DEFAULT 0,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_last_ts ON sessions(last_ts DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_cwd ON sessions(cwd);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL,
  uuid TEXT,
  seq INTEGER NOT NULL,
  byte_offset INTEGER,
  ts TEXT,
  kind TEXT NOT NULL,
  text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_sid ON messages(session_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_meta ON messages(session_id, kind)
  WHERE kind IN ('ai_title', 'last_prompt');

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text,
  content='messages',
  content_rowid='id',
  tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  mtime_ns INTEGER,
  size INTEGER,
  parsed_offset INTEGER DEFAULT 0,
  seq_end INTEGER DEFAULT 0,
  bad_lines INTEGER DEFAULT 0,
  archived_mtime_ns INTEGER,
  archived_size INTEGER,
  last_scan_at TEXT
);

CREATE TABLE IF NOT EXISTS subagents (
  agent_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  agent_type TEXT,
  description TEXT,
  tool_use_id TEXT,
  spawn_depth INTEGER,
  file_path TEXT,
  file_size INTEGER
);
CREATE INDEX IF NOT EXISTS idx_subagents_sid ON subagents(session_id);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """打开(必要时重建)数据库。quick_check 失败或 schema 版本不符 → 删库重来。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = _open(db_path)
    if not _healthy(con):
        con.close()
        _remove_db_files(db_path)
        con = _open(db_path)
        _init_schema(con)
        return con
    _init_schema(con)
    return con


def _open(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def _healthy(con: sqlite3.Connection) -> bool:
    try:
        row = con.execute("PRAGMA quick_check").fetchone()
        if not row or row[0] != "ok":
            return False
    except sqlite3.DatabaseError:
        return False
    try:
        ver = con.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
    except sqlite3.OperationalError:
        return True  # 空库(还没建表)也算健康,走 _init_schema
    return ver is None or ver[0] == SCHEMA_VERSION


def _init_schema(con: sqlite3.Connection) -> None:
    con.executescript(DDL)
    con.execute(
        "INSERT INTO meta(k, v) VALUES('schema_version', ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (SCHEMA_VERSION,),
    )
    con.commit()


def _remove_db_files(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()


def rebuild(db_path: Path) -> sqlite3.Connection:
    """手动重建:删库并返回全新连接。调用方随后应触发全量扫描。"""
    _remove_db_files(Path(db_path))
    return connect(db_path)
