"""归档快照与还原。归档区是纯镜像:只增不删,本程序永不自动删除其中任何文件。

布局与 ~/.claude/projects 同构:
  <archive_dir>\\projects\\<proj_dir>\\<session_id>.jsonl        主 transcript 副本
  <archive_dir>\\projects\\<proj_dir>\\<session_id>\\...          伴生目录镜像(subagents/tool-results)
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from .config import Config
from .scanner import MainFile, walk_companion


def is_quiet(mtime_ns: int, quiet_minutes: int, now_ns: int | None = None) -> bool:
    """安静期:源文件最后修改距今超过 quiet_minutes 才值得快照(活跃会话即拷即过期)。"""
    now = time.time_ns() if now_ns is None else now_ns
    return (now - mtime_ns) > quiet_minutes * 60 * 1_000_000_000


def archived_main_path(cfg: Config, proj_dir: str, session_id: str) -> Path:
    return cfg.archive_projects_root / proj_dir / f"{session_id}.jsonl"


def archived_companion_dir(cfg: Config, proj_dir: str, session_id: str) -> Path:
    return cfg.archive_projects_root / proj_dir / session_id


def snapshot_main(cfg: Config, mf: MainFile) -> Path:
    """拷贝主 transcript 到归档区,返回归档路径。copy2 保留 mtime。"""
    dest = archived_main_path(cfg, mf.proj_dir, mf.session_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(mf.path, dest)
    return dest


def mirror_companion(cfg: Config, mf: MainFile) -> int:
    """伴生目录增量镜像:仅拷贝归档区缺失或 mtime/size 不一致的文件。返回拷贝数。"""
    src_dir = mf.companion_dir
    if not src_dir.is_dir():
        return 0
    dest_dir = archived_companion_dir(cfg, mf.proj_dir, mf.session_id)
    copied = 0
    _, entries = walk_companion(src_dir)
    for rel, mtime_ns, size in entries:
        src = src_dir / rel
        dest = dest_dir / rel
        try:
            st = dest.stat()
            if st.st_mtime_ns == mtime_ns and st.st_size == size:
                continue
        except OSError:
            pass  # 归档侧不存在 → 拷
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dest)
            copied += 1
        except OSError:
            continue  # 单文件失败不阻塞镜像,下轮重试
    return copied


def restore_session(cfg: Config, projects_root: Path, proj_dir: str, session_id: str) -> Path:
    """把归档副本还原回 projects 原路径,使 claude --resume 重新可用。

    仅用于源已被清理的会话;活文件仍在时抛 FileExistsError,绝不覆盖。
    """
    src = archived_main_path(cfg, proj_dir, session_id)
    if not src.is_file():
        raise FileNotFoundError(f"归档中没有该会话: {src}")
    live = projects_root / proj_dir / f"{session_id}.jsonl"
    if live.exists():
        raise FileExistsError(f"源文件仍在,无需还原: {live}")
    live.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, live)

    # 伴生目录:只补活侧缺失的文件
    src_comp = archived_companion_dir(cfg, proj_dir, session_id)
    if src_comp.is_dir():
        dest_comp = live.parent / session_id
        _, entries = walk_companion(src_comp)
        for rel, _mtime, _size in entries:
            dest = dest_comp / rel
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_comp / rel, dest)
    return live


def archive_total_size(cfg: Config) -> int:
    root = cfg.archive_dir_path
    if not root.is_dir():
        return 0
    total = 0
    for f in root.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total
