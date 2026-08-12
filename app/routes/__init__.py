from __future__ import annotations

import sqlite3
from typing import Iterator

from fastapi import APIRouter, Request

from .. import db as db_mod


def request_db(request: Request) -> Iterator[sqlite3.Connection]:
    """每请求一个只读用途的短连接(WAL 下与索引线程并发无碍)。

    schema 由索引器启动时初始化,这里不再跑 quick_check/DDL。
    """
    con = db_mod._open(request.app.state.cfg.db_path)
    try:
        yield con
    finally:
        con.close()


def build_api_router() -> APIRouter:
    from . import indexctl, liveboard, sessions

    router = APIRouter()
    router.include_router(sessions.router)
    router.include_router(indexctl.router)
    router.include_router(liveboard.router)
    return router
