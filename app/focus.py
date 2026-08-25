"""把某个会话所在的终端窗口/标签拉到前台。

定位不再靠「猜 CC 往标签上写了什么」—— 那条路已三次失效(CC 2.1.245 把标签
改成通用名 Claude Code / resume 用 --name 设的标签不在候选里 / 同名标签盲取
第一个跳错窗口)。现在按三层走,层层降级、每层诚实:

第 0 层 身份查询   consolemark.host_kind(pid):OS 直接回答「在 WT 标签里 /
                  有自己的窗口 / 没有窗口」,零猜测。
第 1 层 标记定位   宿主在 WT 里时,把该会话的控制台标题临时换成唯一标记,
                  ConPTY 转发到标签,UIA 按标记找到即选中,再恢复原题。
                  标记是我们自己写的,与 CC 版本、fork、tag 全部解耦。
第 2 层 标题兜底   前两层走不通(权限/第三方终端/标记被 suppressApplicationTitle
                  吞掉)才回到老的标题匹配,且多命中如实报歧义,不再盲取第一个。

选中标签用 UIA SelectionItemPattern;置前窗口前先敲一下 Alt 绕过 Windows
前台锁(后台进程默认无权抢焦点)。并发聚焦请求用 FOCUS_LOCK 串行 —— 标记法
改的是全局控制台标题,交叉执行会互相污染。
"""
from __future__ import annotations

import ctypes
import re
import threading
import time

from .consolemark import host_kind, mark_console, marker_for, restore_console

_GLYPH_RE = re.compile(r"^[^0-9A-Za-z一-鿿]+")
_ELLIPSIS = ("…", "...")

# 管理员权限运行的终端会在标签标题前加一截前缀,且随系统语言变化
# (2026-08-23 本机同时抓到英文 'Administrator: ' 与中文 '管理员: ')。
# 只认这份白名单,不能泛化成「剥掉第一个冒号之前」—— 那会误伤
# 「TODO: 修好色带」这类本身就带冒号的正常标题。其他语言按需补进来。
_ADMIN_PREFIX_RE = re.compile(r"^\s*(?:Administrator|管理员|管理員|管理者)\s*[:：]\s*", re.I)

WT_CLASS = "CASCADIA_HOSTING_WINDOW_CLASS"

# 标记出现在标签上的等待上限。Spike 实测(2026-08-24)约 2.5s —— WT 对
# headless ConPTY 的标题转发有节流,不是毫秒级;留出余量但别让点击无限挂。
MARKER_WAIT_S = 4.0
_POLL_S = 0.12

# 聚焦请求全局串行:标记法临时改控制台标题,两个请求交叉会互认对方的标记。
FOCUS_LOCK = threading.Lock()


def strip_glyph(tab_text: str) -> str:
    """剥掉标签文本的装饰:先去管理员前缀,再去开头的状态符号(✳/◑/⏺ 等)。"""
    t = _ADMIN_PREFIX_RE.sub("", tab_text or "")
    return _GLYPH_RE.sub("", t).strip()


def match_tab(tab_text: str, candidates: list[str]) -> bool:
    """标签文本(已剥符号)与候选(name/title/tag)匹配:精确 > 截断前缀。"""
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


def _collect_tabs() -> list[tuple[object, object]]:
    """收集所有 WT 窗口的 (窗口, 标签) 对。每次全量重扫 —— UIA 引用跨轮询不可靠。"""
    import uiautomation as auto

    out: list[tuple[object, object]] = []
    with auto.UIAutomationInitializerInThread():
        for w in auto.GetRootControl().GetChildren():
            if w.ClassName != WT_CLASS:
                continue

            def walk(c, depth=0):
                for ch in c.GetChildren():
                    if ch.ControlTypeName == "TabItemControl":
                        out.append((w, ch))
                    elif depth < 5:
                        walk(ch, depth + 1)

            walk(w)
    return out


def _select_tab(w, tab) -> dict:
    _force_foreground(w.NativeWindowHandle)
    try:
        tab.GetSelectionItemPattern().Select()
    except Exception:
        tab.Click(simulateMove=False)  # 个别版本 pattern 不可用,退回点击
    return {"ok": True, "matched_tab": tab.Name, "window": w.Name}


def focus_session_by_pid(pid: int | None) -> dict | None:
    """第 0+1 层:按进程身份聚焦。返回成功结果;这个 pid 走不通 → None(调用方降级)。

    调用方需持有 FOCUS_LOCK。
    """
    if not isinstance(pid, int) or pid <= 0:
        return None
    kind, hwnd = host_kind(pid)
    if kind == "own-window":
        _force_foreground(hwnd)
        return {"ok": True, "tier": "window", "verified": True,
                "matched_tab": None, "window": None}
    if kind != "wt-tab":
        return None  # headless:fork daemon / 第三方终端,这里没有可聚焦的窗口
    marker = marker_for(pid)
    old = mark_console(pid)
    if old is None:
        return None  # AttachConsole 失败(权限/竞态),降级
    try:
        deadline = time.monotonic() + MARKER_WAIT_S
        while time.monotonic() < deadline:
            for w, tab in _collect_tabs():
                if marker in (tab.Name or ""):
                    res = _select_tab(w, tab)
                    res.update({"tier": "marker", "verified": True})
                    return res
            time.sleep(_POLL_S)
        return None  # 标记没出现(如 suppressApplicationTitle),降级
    finally:
        restore_console(pid, old)


def focus_by_title(candidates: list[str]) -> dict:
    """第 2 层兜底:标题匹配。多命中如实报歧义,零命中如实报没找到。

    调用方需持有 FOCUS_LOCK。
    """
    hits = [(w, t) for w, t in _collect_tabs() if match_tab(t.Name, candidates)]
    if not hits:
        return {"ok": False, "matched_tab": None, "window": None,
                "tier": "title", "ambiguous": None}
    if len(hits) > 1:
        return {"ok": False, "matched_tab": None, "window": None,
                "tier": "title", "ambiguous": [t.Name for _, t in hits]}
    w, t = hits[0]
    res = _select_tab(w, t)
    res.update({"tier": "title", "verified": False, "ambiguous": None})
    return res
