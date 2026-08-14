"""FastAPI 应用工厂:API 路由 + 静态页 + 后台索引线程。仅供 127.0.0.1 使用。"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import db as db_mod
from .config import PROJECT_ROOT, Config
from .indexer import Indexer
from .routes import build_api_router

VERSION = "0.1.0"
WEB_DIR = PROJECT_ROOT / "web"


def _scan_loop(idx: Indexer, interval_s: int, stop: threading.Event) -> None:
    while True:
        try:
            idx.scan_once()
        except Exception:
            pass  # scan_once 内部已按文件兜错;此处仅保线程不死
        if stop.wait(interval_s):
            return


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config.load()
    con = db_mod.connect(cfg.db_path)  # 启动时做一次健康检查/建表
    idx = Indexer(cfg, con)
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        t = threading.Thread(
            target=_scan_loop,
            args=(idx, cfg.scan_interval_seconds, stop),
            name="claudedeck-indexer",
            daemon=True,
        )
        t.start()
        yield
        stop.set()

    app = FastAPI(title="ClaudeDeck", version=VERSION, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.indexer = idx

    # 静态资源必须 no-cache:不给缓存策略时浏览器会"启发式缓存",改版后用户
    # 可能一直卡在旧页面(2026-08-13 实测,徽章修复到不了浏览器)。
    # no-cache = 可缓存但每次回源校验,配 StaticFiles 的 ETag 走 304,代价极小。
    @app.middleware("http")
    async def _no_stale_static(request, call_next):
        resp = await call_next(request)
        if not request.url.path.startswith("/api"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    app.include_router(build_api_router())
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    return app
