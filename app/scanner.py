"""枚举 ~/.claude/projects 与归档区的会话文件。只列文件,不读内容。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


@dataclass
class MainFile:
    """一个主 transcript(projects\\<proj_dir>\\<session_id>.jsonl)。"""

    session_id: str
    proj_dir: str
    path: Path
    mtime_ns: int
    size: int

    @property
    def companion_dir(self) -> Path:
        # 伴生目录 projects\<proj_dir>\<session_id>\(subagents/ tool-results/)
        return self.path.parent / self.session_id


@dataclass
class SubagentMeta:
    agent_id: str
    meta_path: Path
    jsonl_path: Path | None
    jsonl_size: int


def munge_path(cwd: str) -> str:
    """Claude Code 的目录名编码:每个非字母数字字符→'-'(中文全塌缩,不可逆)。"""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def list_main_files(projects_root: Path) -> list[MainFile]:
    out: list[MainFile] = []
    if not projects_root.is_dir():
        return out
    for proj in sorted(projects_root.iterdir()):
        if not proj.is_dir():
            continue
        for f in sorted(proj.glob("*.jsonl")):
            if not UUID_RE.match(f.stem):
                continue  # 非会话文件,忽略
            try:
                st = f.stat()
            except OSError:
                continue
            out.append(
                MainFile(
                    session_id=f.stem.lower(),
                    proj_dir=proj.name,
                    path=f,
                    mtime_ns=st.st_mtime_ns,
                    size=st.st_size,
                )
            )
    return out


def walk_companion(companion_dir: Path) -> tuple[int, list[tuple[str, int, int]]]:
    """返回 (总字节数, [(相对路径, mtime_ns, size), ...])。目录不存在返回 (0, [])。"""
    total = 0
    entries: list[tuple[str, int, int]] = []
    if not companion_dir.is_dir():
        return 0, entries
    for f in sorted(companion_dir.rglob("*")):
        if not f.is_file():
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        total += st.st_size
        entries.append((f.relative_to(companion_dir).as_posix(), st.st_mtime_ns, st.st_size))
    return total, entries


def list_subagent_metas(companion_dir: Path) -> list[SubagentMeta]:
    """子 agent 卡片只需 ~100B 的 .meta.json;agent jsonl 本体不在索引期读取。"""
    out: list[SubagentMeta] = []
    sub_root = companion_dir / "subagents"
    if not sub_root.is_dir():
        return out
    for meta in sorted(sub_root.rglob("agent-*.meta.json")):
        agent_id = meta.name[: -len(".meta.json")]
        jsonl = meta.with_name(agent_id + ".jsonl")
        size = 0
        jsonl_path: Path | None = None
        if jsonl.is_file():
            jsonl_path = jsonl
            try:
                size = jsonl.stat().st_size
            except OSError:
                size = 0
        out.append(
            SubagentMeta(agent_id=agent_id, meta_path=meta, jsonl_path=jsonl_path, jsonl_size=size)
        )
    return out
