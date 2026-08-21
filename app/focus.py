"""把某个会话所在的 Windows Terminal 标签拉到前台。

本机实测(2026-08-20):WT 标签标题 = 状态符号(✳/◑/⏺ 等)+「注册表 name 或
会话 ai-title」二选一。策略:剥掉开头的非文字符号,与 {name, title} 双候选
精确匹配,其次前缀匹配(标签可能截断)。选中标签用 UIA SelectionItemPattern,
置前窗口前先敲一下 Alt 绕过 Windows 前台锁(后台进程默认无权抢焦点)。
kind=bg 的会话没有窗口,调用方应拦截。
"""
from __future__ import annotations

import ctypes
import re

_GLYPH_RE = re.compile(r"^[^0-9A-Za-z一-鿿]+")
_ELLIPSIS = ("…", "...")

WT_CLASS = "CASCADIA_HOSTING_WINDOW_CLASS"


def strip_glyph(tab_text: str) -> str:
    return _GLYPH_RE.sub("", tab_text or "").strip()


def match_tab(tab_text: str, candidates: list[str]) -> bool:
    """标签文本(已剥符号)与候选(name/title)匹配:精确 > 截断前缀。"""
    t = strip_glyph(tab_text)
    if not t:
        return False
    for c in candidates:
        c = (c or "").strip()
        if not c:
            continue
        if t == c:
            return True
        for e in _ELLIPSIS:  # 标签截断:「很长的标题…」
            if t.endswith(e) and c.startswith(t[: -len(e)].rstrip()):
                return True
    return False


def _force_foreground(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    VK_MENU, KEYEVENTF_KEYUP, SW_RESTORE = 0x12, 0x0002, 9
    user32.keybd_event(VK_MENU, 0, 0, 0)  # 轻敲 Alt 解除前台锁
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)


def focus_session(candidates: list[str]) -> dict:
    """按候选名聚焦对应 WT 标签。返回 {ok, matched_tab, window}。"""
    import uiautomation as auto

    with auto.UIAutomationInitializerInThread():
        for w in auto.GetRootControl().GetChildren():
            if w.ClassName != WT_CLASS:
                continue
            hits: list = []

            def walk(c, depth=0):
                for ch in c.GetChildren():
                    if ch.ControlTypeName == "TabItemControl":
                        if match_tab(ch.Name, candidates):
                            hits.append(ch)
                    elif depth < 5:
                        walk(ch, depth + 1)

            walk(w)
            if not hits:
                continue
            tab = hits[0]
            _force_foreground(w.NativeWindowHandle)
            try:
                tab.GetSelectionItemPattern().Select()
            except Exception:
                tab.Click(simulateMove=False)  # 个别版本 pattern 不可用,退回点击
            return {"ok": True, "matched_tab": tab.Name, "window": w.Name}
    return {"ok": False, "matched_tab": None, "window": None}
