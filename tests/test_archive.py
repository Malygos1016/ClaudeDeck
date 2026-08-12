from __future__ import annotations

import time

import pytest

from app.archive import is_quiet, mirror_companion, restore_session, snapshot_main
from app.scanner import list_main_files

from conftest import make_old
from factory import SID, user_line, write_jsonl

PROJ = "C--Work-proj"


def make_live(cfg):
    p = cfg.projects_root / PROJ / f"{SID}.jsonl"
    write_jsonl(p, user_line())
    return p


def get_main(cfg):
    mains = list_main_files(cfg.projects_root)
    assert len(mains) == 1
    return mains[0]


def test_is_quiet():
    now = time.time_ns()
    assert is_quiet(now - 20 * 60 * 1_000_000_000, 15, now_ns=now) is True
    assert is_quiet(now - 5 * 60 * 1_000_000_000, 15, now_ns=now) is False


def test_snapshot_and_restore_roundtrip(cfg):
    p = make_live(cfg)
    mf = get_main(cfg)
    dest = snapshot_main(cfg, mf)
    assert dest.read_bytes() == p.read_bytes()

    p.unlink()
    live = restore_session(cfg, cfg.projects_root, PROJ, SID)
    assert live == p
    assert live.read_bytes() == dest.read_bytes()  # 字节一致


def test_restore_refuses_when_live_exists(cfg):
    make_live(cfg)
    mf = get_main(cfg)
    snapshot_main(cfg, mf)
    with pytest.raises(FileExistsError):
        restore_session(cfg, cfg.projects_root, PROJ, SID)


def test_restore_missing_archive(cfg):
    with pytest.raises(FileNotFoundError):
        restore_session(cfg, cfg.projects_root, PROJ, SID)


def test_mirror_companion_incremental(cfg):
    p = make_live(cfg)
    comp = p.parent / SID
    (comp / "tool-results").mkdir(parents=True)
    (comp / "tool-results" / "toolu_1.txt").write_text("result-1", encoding="utf-8")
    (comp / "subagents").mkdir()
    (comp / "subagents" / "agent-0123456789abcdef0.jsonl").write_text("{}", encoding="utf-8")
    mf = get_main(cfg)

    assert mirror_companion(cfg, mf) == 2
    assert mirror_companion(cfg, mf) == 0  # 无变化不重拷

    f = comp / "tool-results" / "toolu_1.txt"
    f.write_text("result-1-changed", encoding="utf-8")
    make_old(f, minutes=1)  # 确保 mtime 与归档侧不同
    assert mirror_companion(cfg, mf) == 1
