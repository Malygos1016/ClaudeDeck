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


# 若 ClaudeDeck 本身是从 Claude Code 的工具 shell 里启动的,这些变量会一路穿透到
# 被拉起的新 claude:NO_COLOR 让它整屏白字,CLAUDECODE/CLAUDE_CODE_* 让它自认嵌套
# 会话(2026-08-12 实测)。拉起前必须净化。
_ENV_STRIP_EXACT = {"NO_COLOR", "FORCE_COLOR", "CLAUDECODE", "CLAUDE_PID", "GIT_TERMINAL_PROMPT"}
_ENV_STRIP_PREFIX = ("CLAUDE_CODE_",)


def _clean_child_env() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if k not in _ENV_STRIP_EXACT and not k.startswith(_ENV_STRIP_PREFIX)
    }


# 非 claude provider 的可执行名(npm shim / winget link,均靠 PATH 解析)与提示语
PROVIDER_EXES = {"codex": "codex", "pi": "pi", "crush": "crush"}
PROVIDER_NOTES = {
    "crush": "Crush 没有命令行级 resume:已在项目目录打开 crush,请在它的会话列表里选择该会话。",
}


def _safe_name(name: str) -> str:
    """名字用于拼进 PowerShell 命令行,引号必须中和掉,否则命令被拆断。"""
    return (name or "").replace('"', "").replace("`", "").strip()


def _resume_inner(
    cfg: Config, session_id: str, *, fork: bool, provider: str, name: str | None = None
) -> str:
    if provider == "codex":
        return f"codex resume {session_id}"  # fork 概念不适用
    if provider == "pi":
        return f"pi --session {session_id}"  # cd 到项目目录后按 uuid 解析
    if provider == "crush":
        return "crush"  # 无命令行级 resume,开 TUI 后在列表里选
    inner = f'& "{cfg.claude_exe}" --resume {session_id}'
    if fork:
        inner += " --fork-session"
    # 给这个实例一个显示名。恢复 fork 父分支时必须传:fork 会把父会话的标题也
    # 改成带 ⑂ 的,不传的话恢复出来的窗口与子分支的窗口同名,聚焦按标题匹配
    # 必然跳错(用户实报:点父节点跳到了子分支的窗口)。
    safe = _safe_name(name) if name else ""
    if safe:
        inner += f' --name "{safe}"'
    return inner


def build_resume_command(
    cfg: Config,
    cwd: str | None,
    session_id: str,
    fork: bool = False,
    provider: str = "claude",
    name: str | None = None,
) -> str:
    """官方推荐形式:先 cd 再 resume,Windows 分隔符用 ';'(PowerShell)。"""
    parts = []
    if cwd:
        parts.append(f'cd "{cwd}"')
    parts.append(_resume_inner(cfg, session_id, fork=fork, provider=provider, name=name))
    return "; ".join(parts)


def build_attach_command(cfg: Config, cwd: str | None, job_id: str) -> str:
    """接管一个没有窗口的后台作业:``claude attach <jobId>``。

    不能用 --resume:CC 的并发检测会让第二个实例直接退出。attach 是把守护进程
    里已经在跑的界面接到当前终端上显示,作业本身不重启、不中断(2026-08-23 实测)。
    该子命令没有出现在 `claude --help` 的命令列表里,属隐藏命令。
    """
    parts = []
    if cwd:
        parts.append(f'cd "{cwd}"')
    parts.append(f'& "{cfg.claude_exe}" attach {job_id}')
    return "; ".join(parts)


def launch_attach(cfg: Config, cwd: str | None, job_id: str) -> dict:
    """开一个 WT 新标签接管后台作业。复用 resume 那套坑已踩平的拉起链。"""
    effective_cwd = cwd if cwd and os.path.isdir(cwd) else str(Path.home())
    inner = build_attach_command(cfg, None, job_id)
    env = _clean_child_env()
    wt = shutil.which("wt.exe")
    if wt:
        args = [
            wt, "new-tab", "--title", f"attach {job_id}", "-d", effective_cwd,
            "powershell.exe", "-NoExit", "-Command", inner,
        ]
        subprocess.Popen(args, env=env)
        used_wt = True
    else:
        subprocess.Popen(
            ["powershell.exe", "-NoExit", "-Command", inner],
            cwd=effective_cwd,
            creationflags=CREATE_NEW_CONSOLE,
            env=env,
        )
        used_wt = False
    return {"ok": True, "used_wt": used_wt, "effective_cwd": effective_cwd, "job_id": job_id}


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
    provider: str = "claude",
    name: str | None = None,
) -> dict:
    """拉起 WT 新标签 resume 会话。返回 {ok, used_wt, trust_prewritten, effective_cwd, note}。"""
    is_claude = provider == "claude"
    effective_cwd = cwd
    if not effective_cwd or not os.path.isdir(effective_cwd):
        if not use_home_fallback or not is_claude:
            raise CwdMissing(effective_cwd or "(未知)")
        effective_cwd = str(Path.home())  # --resume 自 2.1.223 跨目录全局搜索,能找到会话

    if not is_claude:
        exe = PROVIDER_EXES.get(provider)
        if exe is None or shutil.which(exe) is None:
            raise FileNotFoundError(f"{exe or provider} 不在 PATH,无法拉起;用「复制命令」手动跑。")

    trust_prewritten = ensure_trusted(cfg, effective_cwd) if is_claude else False

    inner = _resume_inner(cfg, session_id, fork=fork, provider=provider, name=name)
    title = f"{'resume' if is_claude else 'codex'} {session_id[:8]}"
    env = _clean_child_env()
    wt = shutil.which("wt.exe")
    if wt:
        args = [
            wt, "new-tab", "--title", title, "-d", effective_cwd,
            "powershell.exe", "-NoExit", "-Command", inner,
        ]
        subprocess.Popen(args, env=env)
        used_wt = True
    else:
        subprocess.Popen(
            ["powershell.exe", "-NoExit", "-Command", inner],
            cwd=effective_cwd,
            creationflags=CREATE_NEW_CONSOLE,
            env=env,
        )
        used_wt = False

    return {
        "ok": True,
        "used_wt": used_wt,
        "trust_prewritten": trust_prewritten,
        "effective_cwd": effective_cwd,
        "note": PROVIDER_NOTES.get(provider, RESUME_NOTE if is_claude else None),
    }
