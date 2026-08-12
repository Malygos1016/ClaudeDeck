"""索引控制与健康检查。"""
from __future__ import annotations

import sqlite3
import threading

from fastapi import APIRouter, Depends, Request

from . import request_db

router = APIRouter()


@router.get("/api/index/status")
def index_status(request: Request):
    return request.app.state.indexer.status


@router.post("/api/index/scan")
def trigger_scan(request: Request):
    idx = request.app.state.indexer
    if idx.status["phase"] == "scanning":
        return {"started": False, "status": idx.status}
    threading.Thread(target=idx.scan_once, name="claudedeck-manual-scan", daemon=True).start()
    return {"started": True, "status": idx.status}


@router.get("/healthz")
def healthz(request: Request, con: sqlite3.Connection = Depends(request_db)):
    db_ok = con.execute("SELECT 1").fetchone()[0] == 1
    return {"ok": True, "db_ok": db_ok, "version": request.app.version}
