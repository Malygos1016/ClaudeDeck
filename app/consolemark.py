"""控制台身份链与标题标记 —— focus 的 OS 层机制,不再猜标题。

为什么存在:聚焦靠字符串猜「CC 会往标签上写什么」已三次失效(2.1.245 改成
通用名 Claude Code / --name 设的标签不在候选里 / 同名标签盲取第一个)。
定位职责移交给 OS:

第 0 层(纯只读):会话 PID → NtQueryInformationProcess(49) → 控制台宿主 PID。
宿主是 WT 的 OpenConsole → 会话在某个 WT 标签里;独立 conhost 有可见窗口 →
经典控制台窗口;都不是 → 无窗口(fork daemon / 第三方终端),交兜底层。

第 1 层(标记法):helper 子进程 AttachConsole(pid) → 存原题 → SetConsoleTitle
(唯一标记) → ConPTY 把标题变化转发给 WT 标签 → UIA 按标记找 → 恢复原题。
标记是我们自己写的,与 CC 版本、fork、tag 全部解耦。

Spike 实测(2026-08-24,本机 WT 1.24):标记约 2.5s 内出现在标签上(WT 对
headless ConPTY 的标题转发有节流),恢复无残留;管理员权限 CC 的 AttachConsole
会因完整性级别失败 → 返回 None,调用方降级。

helper 必须是子进程:本服务可能以 CREATE_NO_WINDOW 启动、自带隐藏控制台,
在主进程 FreeConsole 会弄坏自身 stdio;子进程一个控制台只附加一次,用完即弃。
标题经 base64 走 argv/stdout,任意字符(引号/换行/emoji)不破坏协议。
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ProcessConsoleHostProcess = 49  # ntdll 信息类:控制台宿主 PID(低 2 位是标志位)

HELPER_TIMEOUT_S = 6


def marker_for(pid: int) -> str:
    """全局唯一的标签标记。纯 ASCII,避开字体与编码变数。"""
    return f"[CD#{pid}]"


def console_host_of(pid: int) -> int | None:
    """查 pid 的控制台宿主(conhost/OpenConsole)PID;无控制台或查询失败 → None。

    纯只读,毫秒级。这一步把「会话在哪个终端里」从猜测变成查询。
    """
    kernel32 = ctypes.windll.kernel32
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        val = ctypes.c_size_t(0)
        status = ctypes.windll.ntdll.NtQueryInformationProcess(
            h, _ProcessConsoleHostProcess, ctypes.byref(val), ctypes.sizeof(val), None
        )
        if status != 0:
            return None
        return (val.value & ~3) or None
    finally:
        kernel32.CloseHandle(h)


def _visible_window_of(owner_pid: int) -> int:
    """owner_pid 的可见顶层窗口句柄;没有 → 0。经典 conhost 的控制台窗口属于它。"""
    user32 = ctypes.windll.user32
    hits: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == owner_pid and user32.IsWindowVisible(hwnd):
            hits.append(int(hwnd))
            return False  # 找到即停
        return True

    user32.EnumWindows(_cb, 0)
    return hits[0] if hits else 0


def _wt_parent_of(host_pid: int) -> int | None:
    """host_pid 若是 WindowsTerminal 的 OpenConsole,返回 WT 进程 pid;否则 None。"""
    try:
        import psutil

        p = psutil.Process(host_pid)
        if (p.name() or "").lower() != "openconsole.exe":
            return None
        parent = p.parent()
        if parent and (parent.name() or "").lower() == "windowsterminal.exe":
            return parent.pid
    except Exception:
        return None
    return None


def wt_host_of(pid: int) -> tuple[int, int] | None:
    """pid 的控制台宿主若是 WT 的 OpenConsole → (宿主 pid, WT 进程 pid);否则 None。

    排除法的计数单位:多个会话可能共享同一个宿主(fork 就地共窗),按宿主
    去重才是"一个标签一个单元"的正确口径。WT 进程 pid 用于提权窗口剔除
    (提权 WT 是独立进程,见 focus.focus_by_elimination)。
    """
    host = console_host_of(pid)
    if not host:
        return None
    wt = _wt_parent_of(host)
    return (host, wt) if wt else None


def host_kind(pid: int) -> tuple[str, int]:
    """判定会话的窗口形态。返回 (kind, hwnd):

    - ("wt-tab", 0)      宿主是 WindowsTerminal 的 OpenConsole → 走标记法定位标签
    - ("own-window", h)  宿主自带可见窗口(经典 conhost) → 直接置前 h
    - ("headless", 0)    无控制台 / 隐藏 conhost(fork daemon、第三方终端 ConPTY)
    """
    host = console_host_of(pid)
    if not host:
        return "headless", 0
    if _wt_parent_of(host):
        return "wt-tab", 0
    hwnd = _visible_window_of(host)
    if hwnd:
        return "own-window", hwnd
    return "headless", 0


def _run_helper(*args: str) -> str | None:
    """跑一次性 helper 子进程,返回 stdout 首行;失败/超时 → None。"""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "app.consolemark", *args],
            capture_output=True, text=True, encoding="utf-8",
            timeout=HELPER_TIMEOUT_S, creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return None
    line = (r.stdout or "").strip().splitlines()
    return line[0] if line else None


def mark_console(pid: int) -> str | None:
    """把 pid 的控制台标题换成唯一标记。返回原标题(可为空串);失败 → None。"""
    out = _run_helper("mark", str(pid), marker_for(pid))
    if out is not None and out.startswith("OK"):
        b64 = out[2:].strip()
        try:
            return base64.b64decode(b64).decode("utf-8", "replace") if b64 else ""
        except Exception:
            return ""
    return None


def restore_console(pid: int, title: str) -> bool:
    """恢复 pid 的控制台标题。尽力而为:失败也只是标签上残留标记,CC 稍后会自己重写。"""
    b64 = base64.b64encode(title.encode("utf-8")).decode("ascii")
    out = _run_helper("restore", str(pid), b64)
    return out is not None and out.startswith("OK")


def _helper_main(argv: list[str]) -> int:
    """helper 子进程入口:python -m app.consolemark mark|restore <pid> <arg>。

    mark    <pid> <marker明文>  → 'OK <base64原题>' / 'ERR <winerr>'
    restore <pid> <base64标题>  → 'OK' / 'ERR <winerr>'
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if len(argv) != 3:
        print("ERR usage")
        return 2
    mode, pid_s, arg = argv
    k = ctypes.windll.kernel32
    k.FreeConsole()  # CREATE_NO_WINDOW 可能带隐藏控制台,先脱离才能附加目标
    if not k.AttachConsole(int(pid_s)):
        print(f"ERR {k.GetLastError()}")
        return 2
    try:
        if mode == "mark":
            buf = ctypes.create_unicode_buffer(4096)
            n = k.GetConsoleTitleW(buf, 4096)
            old = buf.value if n else ""
            if not k.SetConsoleTitleW(arg):
                print(f"ERR {k.GetLastError()}")
                return 2
            print("OK " + base64.b64encode(old.encode("utf-8")).decode("ascii"))
            return 0
        if mode == "restore":
            title = base64.b64decode(arg).decode("utf-8", "replace")
            if not k.SetConsoleTitleW(title):
                print(f"ERR {k.GetLastError()}")
                return 2
            print("OK")
            return 0
        print("ERR mode")
        return 2
    finally:
        k.FreeConsole()


if __name__ == "__main__":
    raise SystemExit(_helper_main(sys.argv[1:]))
