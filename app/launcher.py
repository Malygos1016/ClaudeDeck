"""resume 命令构造与拉起。S2 只有纯命令构造;窗口拉起与 trust 预写在 S5 实装。"""
from __future__ import annotations

from .config import Config


def build_resume_command(cfg: Config, cwd: str | None, session_id: str, fork: bool = False) -> str:
    """官方推荐形式:先 cd 再 resume,Windows 分隔符用 ';'(PowerShell)。"""
    exe = cfg.claude_exe
    parts = []
    if cwd:
        parts.append(f'cd "{cwd}"')
    resume = f'& "{exe}" --resume {session_id}'
    if fork:
        resume += " --fork-session"
    parts.append(resume)
    return "; ".join(parts)
