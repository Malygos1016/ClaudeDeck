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
    # pythonw + CREATE_NO_WINDOW 下 stdout/stderr 为 None,不重定向就无处喊冤
    log = open(ROOT / "topbar.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = log
    sys.stderr = log

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
    if _run_webview(cfg):
        return
    print("webview 壳不可用,回退 tkinter 版")
    _tk_main(cfg)


def _run_webview(cfg) -> bool:
    """WebView2 壳:顶栏本体是本服务的 /topbar.html(WebGL shader 背景+玻璃格子)。

    WebView2 运行时缺失或 pythonnet 在本 Python 版本罢工时返回 False,
    回退 tkinter 素版,功能不缺席。
    """
    try:
        import webview
    except Exception:
        return False

    user32 = ctypes.windll.user32
    port = cfg.port
    screen_w = user32.GetSystemMetrics(0)
    try:
        dpi = user32.GetDpiForSystem()
    except Exception:
        dpi = 96
    bar_h = int(BAR_LOGICAL_H * dpi / 96)
    state: dict = {"abd": None}

    # 菜单是独立小窗口:主条永远 32px。此前的"加高+SetWindowRgn 裁剪"方案已废——
    # WebView2 走 DirectComposition,区域裁剪对渲染无效,表现为全宽黑带(用户实报)。
    MENU_W, MENU_H = 300, 152  # 逻辑像素

    def _menu_hwnd() -> int:
        return user32.FindWindowW(None, "CCTopBarMenu")

    class Api:
        def open_deck(self):
            webbrowser.open(f"http://127.0.0.1:{port}/live.html")

        def quit(self):
            for w in list(webview.windows):
                w.destroy()

        def menu(self, sid, label, tag, anchor_x_logical):
            """右键弹出:定位并显示菜单窗口,注入上下文。"""
            mw = state.get("menu_win")
            hwnd = _menu_hwnd()
            if mw is None or not hwnd:
                return
            k = dpi / 96
            # move() 收逻辑像素(内部自乘 DPI,实测),全程逻辑坐标计算
            x = int(max(0, min(float(anchor_x_logical) - 16, screen_w / k - MENU_W - 4)))
            GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x00000080, 0x00040000
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
            payload = json.dumps(
                {"sid": sid, "label": label, "tag": tag}, ensure_ascii=False
            )
            mw.evaluate_js(f"showMenu({payload})")
            # 定位/显示走 pywebview 自己的调用(同一 GUI 线程队列,保序);
            # 直接 SetWindowPos 会与异步的 show() 竞态,表现为永远停在创建位置
            mw.move(x, BAR_LOGICAL_H * state.get("rows", 1) + 2)
            mw.show()

            def _front():
                h2 = _menu_hwnd()
                if h2:
                    user32.SetForegroundWindow(h2)

            threading.Timer(0.15, _front).start()
            print(f"menu sid={sid} x={x}")

        def hide_menu(self):
            print("hide_menu")
            mw = state.get("menu_win")
            if mw is not None:
                mw.hide()

        def set_rows(self, rows):
            """标签一行放不下时换两行:窗口与 AppBar 占位同步倍高。"""
            rows = 2 if int(rows) >= 2 else 1
            if state.get("rows", 1) == rows:
                return
            state["rows"] = rows
            h = bar_h * rows
            abd = state.get("abd")
            if abd is not None:
                abd.rc.bottom = abd.rc.top + h
                ctypes.windll.shell32.SHAppBarMessage(ABM_SETPOS, ctypes.byref(abd))
            hwnd = state.get("hwnd")
            if hwnd:
                SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
                user32.SetWindowPos(hwnd, 0, 0, 0, screen_w, h, SWP_NOZORDER | SWP_NOACTIVATE)
            print(f"rows={rows}")

    def on_shown():
        hwnd = user32.FindWindowW(None, "CCTopBar")
        if not hwnd:
            print("找不到 CCTopBar 窗口句柄,AppBar 未注册")
            return
        # 从任务栏隐藏(真挂件):加 TOOLWINDOW、去 APPWINDOW
        GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x00000080, 0x00040000
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
        state["hwnd"] = hwnd
        # Win11 DWM 默认给顶级窗口自动圆角+1px 描边 → 四角漏壁纸(用户实报)。显式关闭。
        try:
            dwm = ctypes.windll.dwmapi
            pref = ctypes.c_int(1)  # DWMWCP_DONOTROUND
            dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
            border = ctypes.c_uint(0xFFFFFFFE)  # DWMWA_BORDER_COLOR = COLOR_NONE
            dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), 4)
        except Exception:
            pass
        abd = _appbar(hwnd, screen_w, bar_h)
        state["abd"] = abd
        # 强制压回 AppBar 矩形(物理像素;min_size 已放开,不会再被撑到 100px)
        SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0004, 0x0010, 0x0020
        user32.SetWindowPos(
            hwnd, 0, abd.rc.left, abd.rc.top,
            abd.rc.right - abd.rc.left, abd.rc.bottom - abd.rc.top,
            SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        print(f"CCTopBar 窗口就位: {rect.left},{rect.top} {rect.right - rect.left}x{rect.bottom - rect.top}")

    def on_closing():
        if state["abd"] is not None:
            _appbar_remove(state["abd"])
            state["abd"] = None

    import os

    mock = os.environ.get("CCTOPBAR_MOCK", "")
    suffix = f"?mock={mock}" if mock else ""
    try:
        api = Api()
        win = webview.create_window(
            "CCTopBar",
            url=f"http://127.0.0.1:{port}/topbar.html{suffix}",
            x=0, y=0, width=screen_w, height=bar_h,
            min_size=(100, 10),  # 默认 (200,100) 会把 32px 的条强撑成 100px+(2026-08-21 实翻车)
            frameless=True, on_top=True, easy_drag=False,
            js_api=api, background_color="#0e1218",
        )
        state["menu_win"] = webview.create_window(
            "CCTopBarMenu",
            url=f"http://127.0.0.1:{port}/topbar_menu.html",
            x=0, y=bar_h, width=MENU_W, height=MENU_H,
            min_size=(100, 10), frameless=True, on_top=True,
            hidden=True, js_api=api, background_color="#10151d",
        )
        win.events.shown += on_shown
        win.events.closing += on_closing

        def _probe():
            try:
                print("bar api keys:", win.evaluate_js(
                    "window.pywebview && window.pywebview.api ? Object.keys(window.pywebview.api).join(',') : '(无 api)'"
                ))
                print("menu 页 showMenu:", state["menu_win"].evaluate_js("typeof showMenu"))
            except Exception as e:
                print(f"probe err: {e!r}")

        threading.Timer(6.0, _probe).start()
        webview.start()
    except Exception as e:
        print(f"webview 壳失败: {e!r}")
        on_closing()
        return False
    on_closing()
    return True


def _tk_main(cfg) -> None:
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
