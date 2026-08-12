"""resume 拉起与信任预写。

全项目唯一会写用户配置的位置是 ensure_trusted() 对 ~/.claude.json 的单键修改:
读-改-写 + 备份到 ~/.claude/backups/ + 临时文件校验 + os.replace 原子替换,
绝不整体重生成、绝不动其他顶层键;任何异常 → 放弃预写照常拉起(用户手动点信任)。

拉起链(session-keeper 验证过的坑):
- Python subprocess 必须用 claude.exe 全路径(.cmd 无法被 CreateProcess 启动)
- 本机无 pwsh7,固定 powershell.exe
- "摘要/完整原样"弹窗无官方开关,只能提示用户按 2+回车(已证实死路)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .config import Config

CREATE_NEW_CONSOLE = 0x00000010

RESUME_NOTE = "若新窗口出现「摘要/完整原样」选择,按 2 后回车加载完整对话(大会话才弹,无法自动)。"


class CwdMissing(Exception):
    def __init__(self, cwd: str):
        super().__init__(f"项目目录已不存在: {cwd}")
        self.cwd = cwd


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


def claude_json_path(cfg: Config) -> Path:
    # ~/.claude.json 与 ~/.claude 同级(不在 .claude 目录内)
    return cfg.claude_home_path.parent / ".claude.json"


def _forward_slash(cwd: str) -> str:
    return cwd.replace("\\", "/")


def ensure_trusted(cfg: Config, cwd: str) -> bool:
    """把 cwd 的 hasTrustDialogAccepted 预写为 true。返回是否实际写入。

    只改这一个键;文件不存在/解析失败/任何异常都视为"没写成",不抛出。
    """
    path = claude_json_path(cfg)
    try:
        if not path.is_file():
            return False
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return False
        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            return False
        key = _forward_slash(cwd)
        entry = projects.get(key)
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            return False  # 已信任,零写入
        if not isinstance(entry, dict):
            projects[key] = {"hasTrustDialogAccepted": True}
        else:
            entry["hasTrustDialogAccepted"] = True

        backups = cfg.claude_home_path / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backups / f"claude.json.claudedeck.{int(time.time() * 1000)}.bak")

        tmp = path.with_name(path.name + ".claudedeck.tmp")
        text = json.dumps(data, ensure_ascii=False, indent=2)
        tmp.write_text(text, encoding="utf-8")
        json.loads(tmp.read_text(encoding="utf-8"))  # 写盘后校验
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def launch_resume(
    cfg: Config,
    cwd: str | None,
    session_id: str,
    *,
    fork: bool = False,
    use_home_fallback: bool = False,
) -> dict:
    """拉起 WT 新标签 resume 会话。返回 {ok, used_wt, trust_prewritten, effective_cwd, note}。"""
    effective_cwd = cwd
    if not effective_cwd or not os.path.isdir(effective_cwd):
        if not use_home_fallback:
            raise CwdMissing(effective_cwd or "(未知)")
        effective_cwd = str(Path.home())  # --resume 自 2.1.223 跨目录全局搜索,能找到会话

    trust_prewritten = ensure_trusted(cfg, effective_cwd)

    inner = f'& "{cfg.claude_exe}" --resume {session_id}'
    if fork:
        inner += " --fork-session"

    title = f"resume {session_id[:8]}"
    wt = shutil.which("wt.exe")
    if wt:
        args = [
            wt, "new-tab", "--title", title, "-d", effective_cwd,
            "powershell.exe", "-NoExit", "-Command", inner,
        ]
        subprocess.Popen(args)
        used_wt = True
    else:
        subprocess.Popen(
            ["powershell.exe", "-NoExit", "-Command", inner],
            cwd=effective_cwd,
            creationflags=CREATE_NEW_CONSOLE,
        )
        used_wt = False

    return {
        "ok": True,
        "used_wt": used_wt,
        "trust_prewritten": trust_prewritten,
        "effective_cwd": effective_cwd,
        "note": RESUME_NOTE,
    }
