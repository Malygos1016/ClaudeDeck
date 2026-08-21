"""CCTopBar:桌面顶端常驻状态条(pythonw -m app.topbar)。

像任务栏一样用 Win32 AppBar 机制占据主屏顶端一条(最大化窗口会让位),
2 秒轮询 ClaudeDeck 服务的 /api/live,把每个运行中会话画成一个可点击的
状态格(tag 或名字 + 三色灯);点击 = 调 /api/live/{sid}/focus 聚焦对应
Windows Terminal 标签。右键菜单:打开 ClaudeDeck / 关闭 CCTopBar。

退出时必须 ABM_REMOVE 归还屏幕空间(finally + WM_DELETE 双保险)。
互斥体防双开。服务没起时显示提示并继续等。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ABM_NEW, ABM_REMOVE, ABM_QUERYPOS, ABM_SETPOS = 0, 1, 2, 3
ABE_TOP = 1
BAR_LOGICAL_H = 26

BG = "#12161d"
FG_DIM = "#8d97a7"
COLORS = {"busy": "#e2a13c", "waiting": "#d97060", "idle": "#58b884"}


class APPBARDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uCallbackMessage", wt.UINT),
        ("uEdge", wt.UINT),
        ("rc", wt.RECT),
        ("lParam", ctypes.c_long),
    ]


def _appbar(hwnd: int, screen_w: int, bar_h: int) -> APPBARDATA:
    abd = APPBARDATA()
    abd.cbSize = ctypes.sizeof(APPBARDATA)
    abd.hWnd = hwnd
    ctypes.windll.shell32.SHAppBarMessage(ABM_NEW, ctypes.byref(abd))
    abd.uEdge = ABE_TOP
    abd.rc = wt.RECT(0, 0, screen_w, bar_h)
    ctypes.windll.shell32.SHAppBarMessage(ABM_QUERYPOS, ctypes.byref(abd))
    abd.rc.bottom = abd.rc.top + bar_h
    ctypes.windll.shell32.SHAppBarMessage(ABM_SETPOS, ctypes.byref(abd))
    return abd


def _appbar_remove(abd: APPBARDATA) -> None:
    try:
        ctypes.windll.shell32.SHAppBarMessage(ABM_REMOVE, ctypes.byref(abd))
    except Exception:
        pass


def _fetch_live(port: int) -> list[dict] | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/live", timeout=1.5) as r:
            return json.load(r).get("sessions", [])
    except Exception:
        return None


def _post_focus(port: int, sid: str) -> None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/live/{sid}/focus",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=4)
    except Exception:
        pass  # 失败无处提示,静默(网页端同操作会有 toast)


def _put_tag(port: int, sid: str, tag: str) -> None:
    body = json.dumps({"tag": tag}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/sessions/{sid}/tag",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="PUT",
    )
    try:
        urllib.request.urlopen(req, timeout=4)
    except Exception:
        pass


def main() -> None:
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, "Local\\ClaudeDeckTopBar")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    from .config import Config

    cfg = Config.load()
    port = cfg.port

    import tkinter as tk
    from tkinter import font as tkfont

    user32 = ctypes.windll.user32
    screen_w = user32.GetSystemMetrics(0)
    try:
        dpi = user32.GetDpiForSystem()
    except Exception:
        dpi = 96
    bar_h = int(BAR_LOGICAL_H * dpi / 96)

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=BG)
    root.geometry(f"{screen_w}x{bar_h}+0+0")
    root.update_idletasks()
    hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()

    abd = _appbar(hwnd, screen_w, bar_h)
    root.geometry(f"{abd.rc.right - abd.rc.left}x{abd.rc.bottom - abd.rc.top}+{abd.rc.left}+{abd.rc.top}")

    f = tkfont.Font(family="Microsoft YaHei UI", size=9)
    strip = tk.Frame(root, bg=BG)
    strip.pack(fill="both", expand=True)

    latest: dict = {"sessions": None}

    def poller():
        import time

        while True:
            latest["sessions"] = _fetch_live(port)
            time.sleep(2)

    threading.Thread(target=poller, daemon=True).start()

    def quit_bar():
        _appbar_remove(abd)
        root.destroy()

    def _menu_base() -> "tk.Menu":
        m = tk.Menu(root, tearoff=0, bg=BG, fg="#dbe2ec",
                    activebackground="#1f2733", activeforeground="#e2a13c")
        m.add_command(label="打开 ClaudeDeck",
                      command=lambda: webbrowser.open(f"http://127.0.0.1:{port}/live.html"))
        m.add_command(label="关闭 CCTopBar", command=quit_bar)
        return m

    def edit_tag(sid: str, label: str, cur: str) -> None:
        from tkinter import simpledialog

        v = simpledialog.askstring(
            "重命名", f"给「{label}」起个名字(留空=清除):", initialvalue=cur, parent=root
        )
        if v is None:
            return
        threading.Thread(target=_put_tag, args=(port, sid, v), daemon=True).start()

    def cell_menu(e, sid: str, label: str, cur: str) -> None:
        m = tk.Menu(root, tearoff=0, bg=BG, fg="#dbe2ec",
                    activebackground="#1f2733", activeforeground="#e2a13c")
        m.add_command(label=f"重命名「{label}」…", command=lambda: edit_tag(sid, label, cur))
        m.add_separator()
        m.add_command(label="打开 ClaudeDeck",
                      command=lambda: webbrowser.open(f"http://127.0.0.1:{port}/live.html"))
        m.add_command(label="关闭 CCTopBar", command=quit_bar)
        m.tk_popup(e.x_root, e.y_root)

    root.bind("<Button-3>", lambda e: _menu_base().tk_popup(e.x_root, e.y_root))

    last_sig: list = [None]

    def redraw():
        sessions = latest["sessions"]
        sig = json.dumps(sessions, ensure_ascii=False, sort_keys=True) if sessions is not None else "down"
        if sig == last_sig[0]:
            root.after(500, redraw)
            return
        last_sig[0] = sig
        for ch in strip.winfo_children():
            ch.destroy()

        brand = tk.Label(strip, text="CC", font=f, bg=BG, fg=FG_DIM, padx=8)
        brand.pack(side="left")
        brand.bind("<Button-1>", lambda e: webbrowser.open(f"http://127.0.0.1:{port}/live.html"))

        if sessions is None:
            tk.Label(strip, text="ClaudeDeck 服务未运行", font=f, bg=BG, fg=FG_DIM).pack(side="left")
        elif not sessions:
            tk.Label(strip, text="没有运行中的会话", font=f, bg=BG, fg=FG_DIM).pack(side="left")
        else:
            for s in sessions:
                is_bg = s.get("kind") == "bg"
                color = COLORS.get(s.get("status"), FG_DIM)
                sid = s.get("session_id") or ""
                label = s.get("tag") or s.get("name") or sid[:8] or "?"
                cur_tag = s.get("tag") or ""
                text = f"● {label}"
                cell_bg = "#2a1c1c" if s.get("status") == "waiting" else BG
                cell = tk.Label(strip, text=text, font=f, bg=cell_bg, fg=color, padx=8)
                cell.pack(side="left", padx=2, pady=1)
                if not is_bg:
                    cell.configure(cursor="hand2")
                    cell.bind(
                        "<Button-1>",
                        lambda e, x=sid: threading.Thread(
                            target=_post_focus, args=(port, x), daemon=True
                        ).start(),
                    )
                cell.bind(
                    "<Button-3>",
                    lambda e, x=sid, l=label, c=cur_tag: cell_menu(e, x, l, c),
                )
        root.after(500, redraw)

    root.protocol("WM_DELETE_WINDOW", quit_bar)
    root.after(200, redraw)
    try:
        root.mainloop()
    finally:
        _appbar_remove(abd)


if __name__ == "__main__":
    main()
