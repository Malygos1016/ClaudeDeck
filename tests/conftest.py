from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Config  # noqa: E402


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """指向临时 fake home 的配置。claude_home 可配是全项目可测性的关键。"""
    home = tmp_path / "claude_home"
    (home / "projects").mkdir(parents=True)
    return Config(
        claude_home=str(home),
        archive_dir=str(tmp_path / "archive"),
        data_dir=str(tmp_path / "data"),
        archive_quiet_minutes=15,
        config_path=str(tmp_path / "config.json"),
    )


def make_old(path: Path, minutes: int = 30) -> None:
    """把文件 mtime 拨到 minutes 分钟前(触发归档安静期)。"""
    ns = time.time_ns() - minutes * 60 * 1_000_000_000
    os.utime(path, ns=(ns, ns))


def write_meta_json(path: Path, agent_id: str, desc: str = "盘点数据") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "agentType": "Explore",
                "description": desc,
                "toolUseId": "toolu_01X",
                "spawnDepth": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
