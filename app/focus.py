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
from concurrent.futures import ThreadPoolExecutor

from .consolemark import host_kind, mark_console, marker_for, restore_console, wt_host_of

_GLYPH_RE = re.compile(r"^[^0-9A-Za-z一-鿿]+")
_ELLIPSIS = ("…", "...")

# 管理员权限运行的终端会在标签标题前加一截前缀,且随系统语言变化
# (2026-08-23 本机同时抓到英文 'Administrator: ' 与中文 '管理员: ')。
# 只认这份白名单,不能泛化成「剥掉第一个冒号之前」—— 那会误伤
# 「TODO: 修好色带」这类本身就带冒号的正常标题。其他语言按需补进来。
_ADMIN_PREFIX_RE = re.compile(r"^\s*(?:Administrator|管理员|管理員|管理者)\s*[:：]\s*", re.I)

WT_CLASS = "CASCADIA_HOSTING_WINDOW_CLASS"

# 标记出现在标签上的等待上限。归因实验(2026-08-24 二轮)推翻了「WT 转发节流」
# 的第一印象:helper 一返回标记就已在标签上(0ms),之前量到的 ~2.5s 全部是
# UIA 从桌面根全量遍历的成本(单轮 2.2s,本机 6 个 WT 窗口 9 标签)。
# 现在用缓存引用轮询(.Name 是 live 调用,毫秒级),等待上限只需覆盖竞态。
MARKER_WAIT_S = 2.5
_POLL_S = 0.05
# busy 会话的 spinner 动画(◐/◑ 轮转)会高频重写标题、盖掉标记(一轮诊断实测
# 三通道全 miss)。未命中时隔这么久把标记再压回去一次,和动画抢写。
_REMARK_EVERY_S = 0.6

# 聚焦请求全局串行:标记法临时改控制台标题,两个请求交叉会互认对方的标记。
FOCUS_LOCK = threading.Lock()

# UIA COM 引用绑定创建线程 —— 全部 UIA 操作固定在这一个常驻工作线程上,
# 标签引用缓存(见 _tabs)才能跨请求复用;FastAPI 的请求线程池不保证同线程。
# 常驻 COM 初始化器的强引用。UIAutomationInitializerInThread.__del__ 会
# CoUninitialize,不存引用就是创建即销毁 —— 之前能跑通全靠 comtypes 恰好在
# 本线程首次 import 时自带一次进程级 CoInitializeEx(import 顺序的巧合,
# 任何模块先在别的线程 import comtypes 就破功)。存住引用把意图变成事实。
_UIA_INIT: object | None = None


def _focus_thread_init() -> None:
    global _UIA_INIT
    import uiautomation as auto

    # 常驻初始化,进程存活期间不 CoUninitialize —— 缓存引用要一直可用
    _UIA_INIT = auto.UIAutomationInitializerInThread()


_EXECUTOR = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="focus", initializer=_focus_thread_init
)

# 标签引用缓存:全量扫描(2.2s)只在冷缓存/显式刷新时做,命中后读 .Name 毫秒级。
# .Name 每次是 live COM 调用,不会 stale;失效只发生在标签被关(读时抛异常)。
_TAB_CACHE: list[tuple[object, object]] = []
_TAB_CACHE_AT: float = 0.0
_TAB_CACHE_TTL_S = 300.0


def _invalidate_tab_cache() -> None:
    """清空标签引用缓存(测试隔离/显式刷新用)。"""
    global _TAB_CACHE, _TAB_CACHE_AT
    _TAB_CACHE = []
    _TAB_CACHE_AT = 0.0


def _tabs(fresh: bool = False) -> list[tuple[object, object]]:
    """标签 (窗口, 标签) 对,优先走缓存。fresh=True 强制全量重扫。"""
    global _TAB_CACHE, _TAB_CACHE_AT
    now = time.monotonic()
    if not fresh and _TAB_CACHE and now - _TAB_CACHE_AT < _TAB_CACHE_TTL_S:
        return _TAB_CACHE
    _TAB_CACHE = _collect_tabs()
    _TAB_CACHE_AT = time.monotonic()
    return _TAB_CACHE


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


def _collect_windows() -> list[dict]:
    """全量枚举 WT 窗口及各自标签。单轮 ~2.2s(桌面根遍历),只在冷缓存/
    显式刷新时调用;引用本身可跨请求复用(见 _tabs 缓存)。

    保留 0 标签的窗口:WT 窗口必有 ≥1 个标签,枚举出 0 个 = UIA 读不到它的
    标签子树(如提权 WT 对普通权限进程;2026-08-25 实测本机普通权限反而读得到,
    此路径为环境差异的休眠保护)——排除法要按它做两边剔除。
    """
    import uiautomation as auto

    out: list[dict] = []
    with auto.UIAutomationInitializerInThread():
        for w in auto.GetRootControl().GetChildren():
            if w.ClassName != WT_CLASS:
                continue
            tabs: list[object] = []

            def walk(c, depth=0):
                for ch in c.GetChildren():
                    if ch.ControlTypeName == "TabItemControl":
                        tabs.append(ch)
                    elif depth < 5:
                        walk(ch, depth + 1)

            walk(w)
            out.append({"window": w, "wt_pid": w.ProcessId, "tabs": tabs})
    return out


def _collect_tabs() -> list[tuple[object, object]]:
    """(窗口, 标签) 对的平铺视图,快路径/标记扫描用。"""
    return [(win["window"], t) for win in _collect_windows() for t in win["tabs"]]


def _select_tab(w, tab) -> dict:
    _force_foreground(w.NativeWindowHandle)
    # 选中通道按实测速度排序(2026-08-24 本机 WT 1.24,GetWindowText 轮询计时):
    #   LegacyIAccessible.DoDefaultAction —— 调用返回 ~505ms 且返回时切换已完成;
    #   SelectionItemPattern.Select       —— 动作本身 ~2.0s、返回 ~2.5s(WT 的
    #                                        XAML UIA provider 慢,不是干等确认);
    #   Click                             —— 最后兜底,会动真实鼠标。
    # 键盘 Ctrl+Alt+N 曾是候选,本机 WT 键绑定对它无响应,弃。
    try:
        tab.GetLegacyIAccessiblePattern().DoDefaultAction()
    except Exception:
        try:
            tab.GetSelectionItemPattern().Select()
        except Exception:
            tab.Click(simulateMove=False)
    return {"ok": True, "matched_tab": tab.Name, "window": w.Name}


def _scan_cached_names(marker: str) -> tuple[object, object] | None:
    """在缓存引用上找含标记的标签;引用失效(标签已关)→ 清缓存返回 None。"""
    try:
        for w, tab in _tabs():
            if marker in (tab.Name or ""):
                return w, tab
    except Exception:
        _invalidate_tab_cache()
    return None


def focus_session_by_pid(pid: int | None) -> dict | None:
    """第 0+1 层:按进程身份聚焦。

    返回语义:None 仅表示这里没有可聚焦的东西(无效 pid / headless);
    确知在 WT 标签里却拿不到标记时返回失败 dict(ok=False, wt_tab=True,
    reason="marker-suppressed"|"attach-failed")—— 这是排除法(第 2.5 层)
    的触发证据,不能塌缩成 None。
    调用方需持有 FOCUS_LOCK 并在 focus 工作线程上执行(见 focus_group)。
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
        # AttachConsole 失败(典型:目标 CC 提权而本服务普通权限 ——
        # limited 查询能过、attach 过不了)。确知在 WT 标签里,交排除法。
        return {"ok": False, "tier": "marker", "verified": False,
                "matched_tab": None, "window": None,
                "wt_tab": True, "reason": "attach-failed", "console_title": None}
    try:
        t0 = time.monotonic()
        deadline = t0 + MARKER_WAIT_S
        next_remark = t0 + _REMARK_EVERY_S
        refreshed = False
        while time.monotonic() < deadline:
            hit = _scan_cached_names(marker)
            if hit is not None:
                res = _select_tab(*hit)
                res.update({"tier": "marker", "verified": True})
                return res
            now = time.monotonic()
            # 缓存读遍没有:可能目标是缓存后新开的标签 —— 中段强制重扫一次(2.2s)
            if not refreshed and now - t0 > 0.5:
                _tabs(fresh=True)
                refreshed = True
                continue
            # busy spinner 会盖掉标记,隔段时间把标记再压回去
            if now >= next_remark:
                restore_console(pid, marker)  # 语义即 SetConsoleTitle(任意串)
                next_remark = now + _REMARK_EVERY_S
            time.sleep(_POLL_S)
        # 标记写进控制台了(mark 成功)却始终没上标签:WT 对这个标签停止了
        # 应用标题转发 —— 手动重命名会锁题(2026-08-25 实报:标签定格在旧
        # ai-title,控制台内部标题早已换新),或 profile 开了
        # suppressApplicationTitle。确知在 WT 标签里,带回诊断与控制台
        # 真实标题交排除法/路由,不能塌缩成 None。
        return {"ok": False, "tier": "marker", "verified": False,
                "matched_tab": None, "window": None,
                "wt_tab": True, "reason": "marker-suppressed",
                "console_title": strip_glyph(old)}
    finally:
        restore_console(pid, old)


def focus_by_title(candidates: list[str]) -> dict:
    """第 2 层兜底/快路径:标题匹配。多命中如实报歧义,零命中如实报没找到。

    调用方需持有 FOCUS_LOCK 并在 focus 工作线程上执行(见 focus_group)。
    """
    try:
        pairs = _tabs()
    except Exception:
        _invalidate_tab_cache()
        pairs = _tabs(fresh=True)
    try:
        hits = [(w, t) for w, t in pairs if match_tab(t.Name, candidates)]
    except Exception:
        # 缓存引用失效(有标签被关):重扫一次再试
        _invalidate_tab_cache()
        hits = [(w, t) for w, t in _tabs(fresh=True) if match_tab(t.Name, candidates)]
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


def _deduce_unowned(tab_names: list[str], unit_names: list[list[str]]) -> dict:
    """排除法的全部演绎,纯函数零 mock 可测。

    前提(调用方保证输入口径,这里逐条机器查验):
      1. 目标确在某个标签里(host_kind 已证,不在本函数);
      2. 闭世界:标签数 == 宿主单元数(其他单元 + 目标单元一个);
      3. 单射:每个其他单元的候选名在全部标签里恰好唯一命中,且互不重复。
    结论:唯一没被认领的标签必属目标。任一前提不成立 → 失败,绝不猜。

    匹配是全局唯一制(不做先到先得的贪心认领):顺序无关、可复现;贪心在
    可解的多重匹配下会随顺序给出不同结果。互换认领(两会话候选恰好各自
    唯一命中对方的标签)照样通过 —— 结论只依赖"无主集合",不依赖认领分配
    本身正确(对抗性审查 2026-08-25 论证)。
    """
    if len(tab_names) != len(unit_names) + 1:
        return {"fail": "count",
                "detail": [f"{len(tab_names)} 个标签 vs {len(unit_names) + 1} 个会话宿主"]}
    claimed: dict[int, int] = {}
    for ui, names in enumerate(unit_names):
        hits = [i for i, t in enumerate(tab_names) if match_tab(t, names)]
        if len(hits) != 1:
            return {"fail": "injective",
                    "detail": [f"候选 {names[:2]} 命中 {len(hits)} 个标签"]}
        if hits[0] in claimed:
            return {"fail": "injective",
                    "detail": [f"标签「{tab_names[hits[0]]}」被两个会话认领"]}
        claimed[hits[0]] = ui
    unowned = [i for i in range(len(tab_names)) if i not in claimed]
    if len(unowned) != 1:
        # 计数与单射都过时这里必然恰好剩 1;纯防御分支
        return {"fail": "multi-unowned" if unowned else "zero-unowned",
                "detail": [tab_names[i] for i in unowned]}
    return {"index": unowned[0]}


def focus_by_elimination(target_pids: set[int], roster: list[dict]) -> dict:
    """第 2.5 层:排除法 —— 前提可查验的演绎,不是猜(设计宗旨"每层诚实")。

    锁题标签(手动重命名后 WT 停转发)对标记法免疫,但其他每个活会话都能按
    名字认领自己的标签;全部认领完后唯一无主的那个,演绎上必属目标。
    roster 是全量注册表会话 [{"pid", "names"}](含 spare/sdk-cli,不预过滤
    —— 闭世界计数要数"每一个占着标签的控制台")。

    调用方需持有 FOCUS_LOCK 并在 focus 工作线程上执行。
    """
    # 会话 → 宿主单元(OpenConsole 粒度):fork 就地共窗等多会话一宿主的
    # 场景在这里天然合并,计数口径才与"标签"对得上
    units: dict[int, dict] = {}
    for r in roster:
        pid = r.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            continue
        hw = wt_host_of(pid)
        if hw is None:
            continue  # headless / 经典 conhost / 第三方终端:不占 WT 标签
        host, wt_pid = hw
        u = units.setdefault(host, {"wt": wt_pid, "names": [], "target": False})
        for n in r.get("names") or []:
            if n and n not in u["names"]:
                u["names"].append(n)
        if pid in target_pids:
            u["target"] = True
    targets = [u for u in units.values() if u["target"]]
    if len(targets) != 1:
        return {"ok": False, "elimination_fail": "target-unit",
                "unaccounted": [f"目标宿主单元数 {len(targets)}"]}

    # fresh 全量扫描是正确性要求,不是性能项:缓存少一个新开标签会把无主
    # 集合从 2 缩成 1,制造假唯一 → 选错。顺带把 pair 缓存刷新成这份结果。
    global _TAB_CACHE, _TAB_CACHE_AT
    wins = _collect_windows()
    _TAB_CACHE = [(w["window"], t) for w in wins for t in w["tabs"]]
    _TAB_CACHE_AT = time.monotonic()

    # UIA 读不到的 WT 进程两边剔除,闭世界在"可枚举宇宙"内重建,演绎仍严密。
    # 两种读不到(2026-08-25 实测):提权 WT 对普通权限服务是**整个窗口不出现
    # 在枚举里**(计划任务语境实证 —— runas 降权反而看得到,不作数);留着
    # 0 标签窗口的判定作防御(WT 窗口必有 ≥1 标签,0 = 子树读不到)。
    visible_wt = {w["wt_pid"] for w in wins}
    dead_wt = {w["wt_pid"] for w in wins if not w["tabs"]}

    def _unreadable(u: dict) -> bool:
        return u["wt"] in dead_wt or u["wt"] not in visible_wt

    if _unreadable(targets[0]):
        return {"ok": False, "elimination_fail": "target-window-unreadable",
                "unaccounted": []}
    pairs = [(w["window"], t) for w in wins if w["tabs"] for t in w["tabs"]]
    others = [u for u in units.values() if not u["target"] and not _unreadable(u)]

    try:
        tab_names = [t.Name or "" for _, t in pairs]
    except Exception:
        _invalidate_tab_cache()
        return {"ok": False, "elimination_fail": "tabs-changed", "unaccounted": []}
    verdict = _deduce_unowned(tab_names, [u["names"] for u in others])
    if "index" not in verdict:
        return {"ok": False, "elimination_fail": verdict["fail"],
                "unaccounted": verdict.get("detail") or []}
    w, t = pairs[verdict["index"]]
    res = _select_tab(w, t)
    res.update({"tier": "elimination", "verified": False, "ambiguous": None})
    return res


def focus_group(
    pids: list[int | None], candidates: list[str], roster: list[dict] | None = None
) -> dict:
    """完整编排:快路径标题匹配 → 逐 pid 标记法 → 排除法 → 携诊断的失败。

    整体提交到常驻 focus 工作线程执行(UIA COM 引用绑定线程,缓存才能复用),
    并在线程内持 FOCUS_LOCK 全局串行。
    """

    def _run() -> dict:
        with FOCUS_LOCK:
            # 快路径:缓存热时亚秒。唯一命中直接用;零命中/歧义升级标记法。
            fast = focus_by_title(candidates)
            if fast["ok"]:
                return fast
            evidence: dict | None = None
            for pid in pids:
                res = focus_session_by_pid(pid)
                if res is None:
                    continue
                if res.get("ok"):
                    return res
                # 确知在 WT 标签里却拿不到标记:记为排除法的触发证据,
                # 余下成员继续试(它们可能在没锁题的标签里)
                evidence = evidence or res
            if evidence is not None:
                if roster:
                    elim = focus_by_elimination(
                        {p for p in pids if isinstance(p, int) and p > 0}, roster
                    )
                    if elim.get("ok"):
                        return elim
                    evidence["elimination_fail"] = elim.get("elimination_fail")
                    evidence["unaccounted"] = elim.get("unaccounted")
                evidence["ambiguous"] = fast.get("ambiguous")
                return evidence
            return fast  # 全败:快路径的歧义名单比笼统 404 有用

    return _EXECUTOR.submit(_run).result(timeout=60)
