"""会话自定义标签(用户改名)。存 data/tags.json——用户数据,绝不进可重建的 DB。"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .config import Config

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, str]]] = {}


def _path(cfg: Config) -> Path:
    return Path(cfg.data_dir) / "tags.json"


def load_tags(cfg: Config) -> dict[str, str]:
    p = _path(cfg)
    try:
        mtime = p.stat().st_mtime_ns
    except OSError:
        return {}
    with _lock:
        hit = _cache.get(str(p))
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        tags = {str(k).lower(): str(v) for k, v in data.items() if v}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    with _lock:
        _cache[str(p)] = (mtime, tags)
    return tags


def set_tag(cfg: Config, session_id: str, tag: str | None) -> dict[str, str]:
    """置/清一个标签并原子写回。返回最新全量表。"""
    p = _path(cfg)
    with _lock:
        try:
            data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}
        sid = session_id.lower()
        tag = (tag or "").strip()
        if tag:
            data[sid] = tag[:60]
        else:
            data.pop(sid, None)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
        _cache.pop(str(p), None)
    return {str(k).lower(): str(v) for k, v in data.items() if v}
