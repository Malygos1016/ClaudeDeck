"""启动入口:python -m app.serve —— 读 config,绑 127.0.0.1,开浏览器。"""
from __future__ import annotations

import sys
import threading
import webbrowser

import uvicorn

from .config import Config
from .main import create_app


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cfg = Config.load()
    app = create_app(cfg)
    url = f"http://127.0.0.1:{cfg.port}/"
    print(f"ClaudeDeck → {url}")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, webbrowser.open, args=(url,)).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=cfg.port, log_level="warning")
    except SystemExit as e:  # 端口被占等启动失败:明确报错,不自动漂移
        print(f"启动失败(端口 {cfg.port} 被占用?)。改 config.json 的 port 后重试。")
        return int(e.code or 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
