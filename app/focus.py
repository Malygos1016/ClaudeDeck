"""把某个会话所在的终端窗口/标签拉到前台。

架构(2026-08-25 三改定稿):**窗口通道** —— 身份全部从 OS 拿,点击不做任何
全局搜索。目标指标:亚秒级、无报错、聚焦正确。

会话 → 窗口(全部毫秒级、跨完整性级别):
  1. 控制台宿主查询   NtQueryInformationProcess(49) → OpenConsole → WT 进程 pid
  2. 窗口解析         该 WT 进程只有一扇窗 → 就是它;多扇窗 → ConPTY 伪窗口的
                     GW_OWNER 指向真实 WT 窗口(2026-08-25 八会话全对实证,
                     改名/锁题/提权全免疫);owner 拿不到(提权目标)再按窗口
                     标题唯一匹配兜底。
  3. 窗内定标签       只扫这一扇窗的 UIA 子树(百毫秒级):单标签即完成 →
                     标题匹配 → 窗内减法(本窗其余会话按 owner 实证成员资格、
                     按名字各自认领,剩下那个就是目标的)→ 窗内短标记
                     (WT 转发是毫秒级,0.5s 封顶)。
  全部不中也**不报错**:正确的窗口置前(窗口层由 OS 保证),如实标注
  tab_selected=False。UIA 读不到的窗口(提权 WT 对普通权限服务,2026-08-25
  计划任务语境实证:整扇窗不在 UIA 枚举里,但 Win32 EnumWindows 看得见、
  标题读得到)同样置前完成。

历史包袱的墓碑:全局标题匹配(三次失效)、全局标记扫描(2.2s 桌面遍历)、
全局排除法(闭世界前提脆、慢、还要报错)已全部废除 —— 它们都是"点击时
现场搜索"的产物,病根一致。经过教训换来的领域事实(管理员前缀白名单、
spinner 抢写标题、锁题标签停转发)保留在下面各自的用武之地。

选中标签用 LegacyIAccessible.DoDefaultAction(实测 ~0.5s,Select 要 2.5s);
置前窗口前先敲一下 Alt 绕过 Windows 前台锁。并发聚焦用 FOCUS_LOCK 串行 ——
标记与进程内 AttachConsole 都是进程级全局状态,交叉执行互相污染。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as _wt
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .consolemark import (
    console_window_owner,
    host_kind,
    mark_console,
    marker_for,
    restore_console,
    wt_host_of,
)

_GLYPH_RE = re.compile(r"^[^0-9A-Za-z一-鿿]+")
_ELLIPSIS = ("…", "...")

# 管理员权限运行的终端会在标签标题前加一截前缀,且随系统语言变化
# (2026-08-23 本机同时抓到英文 'Administrator: ' 与中文 '管理员: ')。
# 只认这份白名单,不能泛化成「剥掉第一个冒号之前」—— 那会误伤
# 「TODO: 修好色带」这类本身就带冒号的正常标题。其他语言按需补进来。
_ADMIN_PREFIX_RE = re.compile(r"^\s*(?:Administrator|管理员|管理員|管理者)\s*[:：]\s*", re.I)

WT_CLASS = "CASCADIA_HOSTING_WINDOW_CLASS"

# 窗内标记的等待上限。WT 把控制台标题转发到标签是毫秒级(2026-08-24 归因
# 实验:helper 一返回标记就已在标签上),这里只需覆盖竞态;锁题标签永远
# 不显示标记,所以上限同时是锁题场景的最大浪费,必须短。
MARKER_WAIT_S = 0.5
_POLL_S = 0.05
# busy 会话的 spinner 动画(◐/◑ 轮转)会高频重写标题、盖掉标记(实测三通道
# 全 miss)。未命中时隔这么久把标记再压回去一次,和动画抢写。
_REMARK_EVERY_S = 0.25

# 聚焦请求全局串行:标记法与进程内 AttachConsole 都是进程级全局状态。
FOCUS_LOCK = threading.Lock()

# UIA COM 引用绑定创建线程 —— 全部 UIA 操作固定在这一个常驻工作线程上。
# 常驻 COM 初始化器的强引用:UIAutomationInitializerInThread.__del__ 会
# CoUninitialize,不存引用就是创建即销毁(之前能跑通全靠 comtypes 恰好在
# 本线程首次 import 时自带一次进程级 CoInitializeEx,import 顺序的巧合)。
_UIA_INIT: object | None = None


def _focus_thread_init() -> None:
    global _UIA_INIT
    import uiautomation as auto

    # 常驻初始化,进程存活期间不 CoUninitialize
    _UIA_INIT = auto.UIAutomationInitializerInThread()


_EXECUTOR = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="focus", initializer=_focus_thread_init
)


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


def _enum_wt_windows() -> list[dict]:
    """全部 WT 顶层窗口 {hwnd, pid, title}。纯 Win32,~2ms,跨完整性级别可见
    (2026-08-25 计划任务语境实证:UIA 看不见提权 WT 窗口,EnumWindows 看得见,
    GetWindowText 也读得到 —— 那是活动标签的标题)。"""
    user32 = ctypes.windll.user32
    out: list[dict] = []

    @ctypes.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
    def _cb(h, _l):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, buf, 256)
        if buf.value == WT_CLASS and user32.IsWindowVisible(h):
            p = _wt.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(p))
            t = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(h, t, 512)
            out.append({"hwnd": int(h), "pid": p.value, "title": t.value})
        return True

    user32.EnumWindows(_cb, 0)
    return out


def _window_tabs(hwnd: int) -> list[object] | None:
    """指定 WT 窗口的 TabItem 引用。UIA 从句柄进、只扫这一扇窗(百毫秒级,
    对比桌面根全量遍历的 2.2s)。读不到(提权窗口对普通权限服务)→ None
    —— WT 窗口必有 ≥1 个标签,拿到 0 个就是子树不可读。"""
    try:
        import uiautomation as auto

        w = auto.ControlFromHandle(hwnd)
        tabs: list[object] = []

        def walk(c, depth=0):
            for ch in c.GetChildren():
                if ch.ControlTypeName == "TabItemControl":
                    tabs.append(ch)
                elif depth < 5:
                    walk(ch, depth + 1)

        walk(w)
        return tabs or None
    except Exception:
        return None


def _select_tab(hwnd: int, tab) -> None:
    _force_foreground(hwnd)
    # 通道按实测速度排序(2026-08-24):DoDefaultAction 返回 ~505ms 且返回时
    # 切换已完成;SelectionItemPattern.Select 要 ~2.5s;Click 动真实鼠标,兜底。
    try:
        tab.GetLegacyIAccessiblePattern().DoDefaultAction()
    except Exception:
        try:
            tab.GetSelectionItemPattern().Select()
        except Exception:
            tab.Click(simulateMove=False)


def _resolve_window(pid: int, wt_pid: int, candidates: list[str]) -> int | None:
    """会话 → WT 窗口句柄。进程唯一窗直接定;多窗用 ConPTY 伪窗口 owner
    (OS 权威);owner 拿不到(提权目标 + 提权 WT 还开了多扇窗的罕见组合)
    按窗口标题唯一匹配兜底;都不行 → None。"""
    wins = [w for w in _enum_wt_windows() if w["pid"] == wt_pid]
    if not wins:
        return None
    if len(wins) == 1:
        return wins[0]["hwnd"]
    owner = console_window_owner(pid)
    if owner and any(w["hwnd"] == owner for w in wins):
        return owner
    hits = [w for w in wins if match_tab(w["title"], candidates)]
    return hits[0]["hwnd"] if len(hits) == 1 else None


def _locate_tab(hwnd: int, pid: int, candidates: list[str],
                roster: list[dict] | None) -> tuple[object | None, str]:
    """窗内定标签。返回 (tab, how);(None, 原因) 表示定不出 —— 调用方置前
    窗口即可,不报错。顺序按成本与确定性排:
    single(必对) → title(快) → subtract(窗内减法,毫秒级) → marker(0.5s 封顶)。
    """
    tabs = _window_tabs(hwnd)
    if tabs is None:
        return None, "uia-unreadable"
    if len(tabs) == 1:
        return tabs[0], "single"

    try:
        names = [t.Name or "" for t in tabs]
    except Exception:
        return None, "tabs-changed"
    hits = [i for i, n in enumerate(names) if match_tab(n, candidates)]
    if len(hits) == 1:
        return tabs[hits[0]], "title"

    # 窗内减法:本窗其余会话的成员资格由 owner 通道 OS 实证,各自按名字唯一
    # 认领标签,恰好剩一个没认领的必是目标的(锁题标签就栽在这) —— 宇宙只有
    # 这一扇窗的两三个标签,任何一步不干净就放弃,绝不猜。
    tab = _subtract_in_window(hwnd, pid, tabs, names, roster)
    if tab is not None:
        return tab, "subtract"

    # 窗内短标记:锁题以外的疑难(标题刚变、索引滞后)。WT 转发毫秒级,
    # 0.5s 封顶;spinner 抢写就再压回去。
    old = mark_console(pid)
    if old is not None:
        marker = marker_for(pid)
        try:
            deadline = time.monotonic() + MARKER_WAIT_S
            next_remark = time.monotonic() + _REMARK_EVERY_S
            while time.monotonic() < deadline:
                try:
                    for t in tabs:
                        if marker in (t.Name or ""):
                            return t, "marker"
                except Exception:
                    break
                now = time.monotonic()
                if now >= next_remark:
                    restore_console(pid, marker)
                    next_remark = now + _REMARK_EVERY_S
                time.sleep(_POLL_S)
        finally:
            restore_console(pid, old)
    return None, "ambiguous"


def _subtract_in_window(hwnd: int, target_pid: int, tabs: list[object],
                        names: list[str], roster: list[dict] | None) -> object | None:
    if not roster:
        return None
    others: list[list[str]] = []
    for r in roster:
        rp = r.get("pid")
        if not isinstance(rp, int) or rp <= 0 or rp == target_pid:
            continue
        if console_window_owner(rp) == hwnd:
            others.append(r.get("names") or [])
    if len(tabs) != len(others) + 1:
        return None
    claimed: set[int] = set()
    for ns in others:
        hits = [i for i, n in enumerate(names) if match_tab(n, ns)]
        if len(hits) != 1 or hits[0] in claimed:
            return None
        claimed.add(hits[0])
    left = [i for i in range(len(tabs)) if i not in claimed]
    return tabs[left[0]] if len(left) == 1 else None


def _scan_marker_across_wt(pid: int) -> tuple[int, object] | None:
    """跨所有 WT 窗口用标记法找这个会话的标签。返回 (hwnd, tab)。

    给 defterm 模式用:那时会话与 WT 之间没有进程关系,无从先定窗口再定标签,
    只能反过来 —— 注入唯一标记,谁显示出来就是谁。标记走的是控制台标题通道
    (ConPTY 转发给 WT),与进程归属无关,故此路可通。
    找不到就是真没有窗口(fork daemon / 第三方终端),照常返回 None。
    """
    old = mark_console(pid)
    if old is None:
        return None
    marker = marker_for(pid)
    try:
        deadline = time.monotonic() + MARKER_WAIT_S
        next_remark = time.monotonic() + _REMARK_EVERY_S
        while time.monotonic() < deadline:
            for win in _enum_wt_windows():
                hwnd = win["hwnd"]
                tabs = _window_tabs(hwnd)
                if not tabs:
                    continue
                try:
                    for t in tabs:
                        if marker in (t.Name or ""):
                            return hwnd, t
                except Exception:
                    continue
            now = time.monotonic()
            if now >= next_remark:      # spinner 抢写标题就再压回去
                restore_console(pid, marker)
                next_remark = now + _REMARK_EVERY_S
            time.sleep(_POLL_S)
        return None
    finally:
        restore_console(pid, old)


def focus_session_by_pid(pid: int | None, candidates: list[str],
                         roster: list[dict] | None = None) -> dict | None:
    """按进程身份聚焦一个会话。返回成功结果;这里没有可聚焦的东西(无效
    pid / headless / 第三方终端)→ None,调用方换下一个成员或如实报没有窗口。

    只要窗口解析成功就**必定 ok**:窗口层由 OS 通道保证正确;标签层定不出
    时置前窗口、如实标 tab_selected=False —— "带到正确的窗口前"永远好于报错。
    调用方需持有 FOCUS_LOCK 并在 focus 工作线程上执行(见 focus_group)。
    """
    if not isinstance(pid, int) or pid <= 0:
        return None
    hw = wt_host_of(pid)
    if hw is None:
        kind, hwnd = host_kind(pid)
        if kind == "own-window":
            _force_foreground(hwnd)
            return {"ok": True, "tier": "window", "verified": True,
                    "matched_tab": None, "window": None, "tab_selected": True}
        # defterm:Windows「默认终端应用」设为 WT 时,从 explorer/cmd 启动的
        # 控制台由 WT 接管显示,但进程链上 WT 不是任何人的父 —— wt_host_of 认不出,
        # 而那个控制台窗口本身不可见,于是 host_kind 判 headless。此时不能就此
        # 放弃(用户实报"点击聚焦又不行了"):标记法不依赖进程关系,横扫 WT 窗口
        # 即可命中。真正无窗口的 fork daemon 扫不到,照样返回 None。
        found = _scan_marker_across_wt(pid)
        if found is None:
            return None  # headless:fork daemon / 第三方终端
        hwnd, tab = found
        _force_foreground(hwnd)
        _select_tab(hwnd, tab)
        try:
            name = tab.Name
        except Exception:
            name = None
        return {"ok": True, "tier": "defterm/marker", "verified": True,
                "matched_tab": name, "window": None, "tab_selected": True}
    _host, wt_pid = hw
    hwnd = _resolve_window(pid, wt_pid, candidates)
    if hwnd is None:
        return None  # WT 窗口竟然没枚举到(刚关闭/极端竞态),让上层如实报
    # 窗口一定,立刻置前 —— 定标签还要几百毫秒(UIA 子树 + DoDefaultAction),
    # 先把正确的窗户递到用户眼前,感知延迟从 ~1s 缩到 ~0.2s。
    _force_foreground(hwnd)
    tab, how = _locate_tab(hwnd, pid, candidates, roster)
    if tab is None:
        # 窗口是 OS 保证的正确窗口:置前,如实标注标签未选中(提权窗口的
        # 标签子树读不到 / 罕见歧义)。不报错 —— 用户已经在正确的窗前了。
        _force_foreground(hwnd)
        return {"ok": True, "tier": "window-channel", "verified": True,
                "matched_tab": None, "window": None,
                "tab_selected": False, "tab_reason": how}
    try:
        name = tab.Name
    except Exception:
        name = None
    _select_tab(hwnd, tab)
    return {"ok": True, "tier": f"window-channel/{how}",
            "verified": how in ("single", "marker", "subtract"),
            "matched_tab": name, "window": None, "tab_selected": True}


def focus_group(
    pids: list[int | None], candidates: list[str], roster: list[dict] | None = None
) -> dict:
    """编排:组内成员逐个走窗口通道(先点击的 sid 自己的实例),第一个解析
    出窗口的即完成。整体提交到常驻 focus 工作线程执行(UIA COM 引用绑定
    线程),线程内持 FOCUS_LOCK 全局串行。"""

    def _run() -> dict:
        with FOCUS_LOCK:
            for pid in pids:
                res = focus_session_by_pid(pid, candidates, roster)
                if res is not None:
                    return res
            return {"ok": False, "matched_tab": None, "window": None,
                    "tier": "none", "tab_selected": False}

    return _EXECUTOR.submit(_run).result(timeout=30)
