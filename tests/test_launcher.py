from __future__ import annotations

import json

import pytest

from app import launcher
from app.launcher import (
    CwdMissing,
    build_resume_command,
    claude_json_path,
    ensure_trusted,
    launch_resume,
)

from factory import SID


def make_claude_json(cfg, projects=None):
    """伪 ~/.claude.json:带大体积无关键,验证读改写只动单键。"""
    data = {
        "installMethod": "npm",
        "cachedBlob": "A" * 5000,  # 模拟 cachedGrowthBookFeatures 之类的大键
        "oauthAccount": {"uuid": "acc-1"},
        "projects": projects if projects is not None else {
            "C:/Other": {"hasTrustDialogAccepted": False, "allowedTools": ["Bash"]},
        },
    }
    p = claude_json_path(cfg)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p, data


def test_attach_command_uses_attach_not_resume(cfg):
    """接管无窗口的后台作业必须用 attach。

    CC 有并发检测,对还活着的会话用 --resume 会让新实例直接退出;
    attach 是把已在跑的界面接到终端上,不重启不打断(2026-08-23 实测)。
    """
    cmd = launcher.build_attach_command(cfg, r"C:\Work\proj", "f8d92a69")
    assert "attach f8d92a69" in cmd
    assert "--resume" not in cmd
    assert "--fork-session" not in cmd
    assert 'cd "C:\\Work\\proj"' in cmd


def test_attach_command_without_cwd(cfg):
    cmd = launcher.build_attach_command(cfg, None, "f8d92a69")
    assert cmd.strip().endswith("attach f8d92a69")
    assert "cd " not in cmd


def test_ensure_trusted_touches_single_key_only(cfg):
    p, before = make_claude_json(cfg)
    assert ensure_trusted(cfg, r"C:\Work\proj") is True
    after = json.loads(p.read_text(encoding="utf-8"))
    # 新键写入,用正斜杠
    assert after["projects"]["C:/Work/proj"] == {"hasTrustDialogAccepted": True}
    # 其余全部原样
    assert after["cachedBlob"] == before["cachedBlob"]
    assert after["oauthAccount"] == before["oauthAccount"]
    assert after["projects"]["C:/Other"] == before["projects"]["C:/Other"]
    # 备份产生且可解析
    backups = list((cfg.claude_home_path / "backups").glob("claude.json.claudedeck.*.bak"))
    assert len(backups) == 1
    json.loads(backups[0].read_text(encoding="utf-8"))


def test_ensure_trusted_existing_entry_and_idempotent(cfg):
    p, _ = make_claude_json(
        cfg, {"C:/Work/proj": {"hasTrustDialogAccepted": False, "history": [1, 2]}}
    )
    assert ensure_trusted(cfg, r"C:\Work\proj") is True
    after = json.loads(p.read_text(encoding="utf-8"))
    assert after["projects"]["C:/Work/proj"]["hasTrustDialogAccepted"] is True
    assert after["projects"]["C:/Work/proj"]["history"] == [1, 2]  # 条目内其他键保留
    # 已信任 → 零写入零备份
    n_backups = len(list((cfg.claude_home_path / "backups").glob("*.bak")))
    assert ensure_trusted(cfg, r"C:\Work\proj") is False
    assert len(list((cfg.claude_home_path / "backups").glob("*.bak"))) == n_backups


def test_ensure_trusted_never_raises(cfg):
    # 文件不存在 / 内容损坏都只返回 False
    assert ensure_trusted(cfg, r"C:\X") is False
    claude_json_path(cfg).write_text("{broken", encoding="utf-8")
    assert ensure_trusted(cfg, r"C:\X") is False


def test_build_resume_command(cfg):
    cmd = build_resume_command(cfg, r"C:\Work\proj", SID, fork=True)
    assert cmd.startswith('cd "C:\\Work\\proj"; ')
    assert f"--resume {SID} --fork-session" in cmd
    assert cfg.claude_exe in cmd
    # 无 cwd 时不带 cd
    assert build_resume_command(cfg, None, SID).startswith("& ")


def test_launch_resume_wt_args(cfg, tmp_path, monkeypatch):
    make_claude_json(cfg)
    calls = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    monkeypatch.setattr(launcher.shutil, "which", lambda name: r"C:\fake\wt.exe")
    # 模拟从 Claude Code 工具 shell 启动 ClaudeDeck 的污染环境
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-session")
    monkeypatch.setenv("KEEP_ME", "yes")
    cwd = tmp_path / "projdir"
    cwd.mkdir()

    res = launch_resume(cfg, str(cwd), SID)
    env = calls[0][1]["env"]
    assert "NO_COLOR" not in env and "CLAUDECODE" not in env  # 白字/嵌套标记必须净化
    assert not any(k.startswith("CLAUDE_CODE_") for k in env)
    assert env["KEEP_ME"] == "yes"
    assert res["ok"] and res["used_wt"] and res["trust_prewritten"] is True
    args, _ = calls[0]
    assert args[0] == r"C:\fake\wt.exe"
    assert args[1] == "new-tab" and "-d" in args and str(cwd) in args
    assert args[-4] == "powershell.exe" and args[-3] == "-NoExit" and args[-2] == "-Command"
    assert f'--resume {SID}' in args[-1] and cfg.claude_exe in args[-1]
    assert ".cmd" not in args[-1]  # 必须 exe 全路径


def test_launch_resume_no_wt_fallback_and_cwd_missing(cfg, tmp_path, monkeypatch):
    make_claude_json(cfg)
    calls = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)

    with pytest.raises(CwdMissing):
        launch_resume(cfg, str(tmp_path / "gone"), SID)

    res = launch_resume(cfg, str(tmp_path / "gone"), SID, use_home_fallback=True)
    assert res["used_wt"] is False
    assert res["trust_prewritten"] is True  # 降级到主目录时,主目录也被预写信任
    args, kw = calls[0]
    assert args[0] == "powershell.exe"
    assert kw["creationflags"] == launcher.CREATE_NEW_CONSOLE
    assert kw["cwd"] == res["effective_cwd"]
