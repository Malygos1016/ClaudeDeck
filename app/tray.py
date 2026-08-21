"""托盘模式:pythonw -m app.tray —— 无控制台窗口,服务常驻系统托盘。

pythonw 下 sys.stdout/stderr 是 None,任何 print/日志一写就炸(win-env 已知坑),
进场先重定向到项目根的 tray.log。已有实例在跑时只开浏览器,绝不抢端口。
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CREATE_NO_WINDOW = 0x08000000
WM_CLOSE = 0x0010

_topbar: list = [None]


def _topbar_alive() -> bool:
    p = _topbar[0]
    return p is not None and p.poll() is None


def _start_topbar() -> None:
    if _topbar_alive():
        return
    _topbar[0] = subprocess.Popen(
        [sys.executable, "-m", "app.topbar"], cwd=str(ROOT), creationflags=CREATE_NO_WINDOW
    )


def _stop_topbar() -> None:
    """先礼后兵:WM_CLOSE 让它优雅退出(归还 AppBar 空间),2s 不走再 terminate。"""
    p = _topbar[0]
    if p is None:
        return
    if p.poll() is None:
        user32 = ctypes.windll.user32

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def enum_cb(hwnd, _):
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == p.pid:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            return True

        user32.EnumWindows(enum_cb, None)
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            p.terminate()
    _topbar[0] = None


def _redirect_std() -> None:
    log = open(ROOT / "tray.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = log
    sys.stderr = log


def _already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def _icon_image():
    """深底圆角方块 + 琥珀指示灯,与界面的控制室语言一致。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, 60, 60], radius=14, fill=(20, 25, 32, 255), outline=(58, 70, 86, 255), width=2)
    d.ellipse([22, 22, 42, 42], fill=(226, 161, 60, 255))
    return img


def main() -> None:
    _redirect_std()
    from .config import Config

    cfg = Config.load()
    url = f"http://127.0.0.1:{cfg.port}/"
    if _already_running(cfg.port):
        webbrowser.open(url)
        return

    import uvicorn

    from .main import create_app

    server = uvicorn.Server(
        uvicorn.Config(create_app(cfg), host="127.0.0.1", port=cfg.port, log_level="warning")
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    # 必须等到真的绑上端口才常驻:双开竞态下抢端口失败的实例若继续挂托盘,
    # 会变成攥着数据库连接的僵尸(2026-08-13 实测)
    import time

    for _ in range(100):
        if server.started:
            break
        if not t.is_alive():
            print(f"uvicorn 线程退出(端口 {cfg.port} 被占?),本实例不驻留。")
            webbrowser.open(url)
            return
        time.sleep(0.1)
    else:
        print("10s 内未完成端口绑定,本实例退出。")
        return

    import pystray
    from pystray import Menu, MenuItem

    def on_open(icon, item):
        webbrowser.open(url)

    def on_log(icon, item):
        import os

        os.startfile(ROOT / "tray.log")

    def on_topbar(icon, item):
        cfg.topbar_enabled = not cfg.topbar_enabled
        cfg.save()
        if cfg.topbar_enabled:
            _start_topbar()
        else:
            _stop_topbar()

    def on_quit(icon, item):
        _stop_topbar()
        server.should_exit = True
        icon.stop()

    icon = pystray.Icon(
        "ClaudeDeck",
        _icon_image(),
        "ClaudeDeck — 会话管理",
        menu=Menu(
            MenuItem("打开界面", on_open, default=True),
            MenuItem("CCTopBar 顶栏", on_topbar, checked=lambda item: cfg.topbar_enabled),
            MenuItem("查看日志", on_log),
            MenuItem("退出", on_quit),
        ),
    )
    if cfg.topbar_enabled:
        _start_topbar()
    threading.Timer(1.2, webbrowser.open, args=(url,)).start()
    icon.run()


if __name__ == "__main__":
    main()
