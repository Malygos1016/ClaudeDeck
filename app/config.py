"""运行时配置。config.json 在项目根目录,首启自动生成默认值,原子写回。"""
from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"


def _default_claude_exe() -> str:
    appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
    return str(
        Path(appdata) / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    )


@dataclass
class Config:
    port: int = 8737
    claude_home: str = field(default_factory=lambda: str(Path.home() / ".claude"))
    archive_dir: str = r"C:\CoreWork\ClaudeArchive"
    data_dir: str = field(default_factory=lambda: str(PROJECT_ROOT / "data"))
    scan_interval_seconds: int = 60
    archive_quiet_minutes: int = 15
    live_poll_ms: int = 2000
    claude_exe: str = field(default_factory=_default_claude_exe)
    index_thinking: bool = False
    index_tool_results: bool = False

    @property
    def claude_home_path(self) -> Path:
        return Path(self.claude_home)

    @property
    def projects_root(self) -> Path:
        return self.claude_home_path / "projects"

    @property
    def archive_dir_path(self) -> Path:
        return Path(self.archive_dir)

    @property
    def archive_projects_root(self) -> Path:
        return self.archive_dir_path / "projects"

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "claudedeck.db"

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        p = Path(path) if path else DEFAULT_CONFIG_PATH
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            known = {f.name for f in dataclasses.fields(cls)}
            cfg = cls(**{k: v for k, v in data.items() if k in known})
        else:
            cfg = cls()
            cfg.save(p)
        return cfg

    def save(self, path: str | Path | None = None) -> None:
        p = Path(path) if path else DEFAULT_CONFIG_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(
            json.dumps(dataclasses.asdict(self), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, p)
