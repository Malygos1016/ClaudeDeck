"""窗口通道聚焦(app/focus.py)的纯逻辑分支。

OS 原语(EnumWindows / ConPTY owner / UIA 子树)要真实窗口才能验,真机验收;
这里 monkeypatch 掉它们,测编排与判定逻辑:

- _resolve_window:进程唯一窗直接定 / 多窗走 owner / owner 拿不到按窗口标题
  唯一匹配 / 歧义放弃。
- _locate_tab:单标签必对 / 标题唯一 / 窗内减法 / 窗内短标记 / 读不到与歧义
  都不报错(调用方置前窗口)。
- _subtract_in_window:成员资格由 owner 通道实证,任何一步不干净就放弃。
- focus_session_by_pid / focus_group:编排与"窗口解析成功必定 ok"的承诺。

strip_glyph/match_tab 的测试在 tests/test_focus_tags.py,不动。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import focus as focus_mod


def _tab(name: str):
    return SimpleNamespace(Name=name)


# ---------------------------------------------------------------------------
# _resolve_window
# ---------------------------------------------------------------------------


def test_resolve_window_single_window_of_process(monkeypatch):
    """WT 进程只有一扇窗:不需要 owner,直接就是它(提权 WT 的常态)。"""
    monkeypatch.setattr(
        focus_mod, "_enum_wt_windows",
        lambda: [{"hwnd": 0x111, "pid": 10728, "title": "管理员: ◐ x"},
                 {"hwnd": 0x222, "pid": 28420, "title": "y"}],
    )
    monkeypatch.setattr(
        focus_mod, "console_window_owner",
        lambda pid: (_ for _ in ()).throw(AssertionError("单窗不该查 owner")),
    )
    assert focus_mod._resolve_window(99, 10728, ["x"]) == 0x111


def test_resolve_window_multi_window_uses_owner(monkeypatch):
    """同一 WT 进程多扇窗:ConPTY 伪窗口的 GW_OWNER 是 OS 权威答案。"""
    monkeypatch.setattr(
        focus_mod, "_enum_wt_windows",
        lambda: [{"hwnd": 0xA, "pid": 28420, "title": "t1"},
                 {"hwnd": 0xB, "pid": 28420, "title": "t2"}],
    )
    monkeypatch.setattr(focus_mod, "console_window_owner", lambda pid: 0xB)
    assert focus_mod._resolve_window(99, 28420, ["别的"]) == 0xB


def test_resolve_window_owner_unavailable_falls_to_title(monkeypatch):
    """owner 拿不到(提权目标 attach 不了):窗口标题唯一匹配兜底 ——
    GetWindowText 跨完整性级别可读(2026-08-25 实证),标题=活动标签的标题。"""
    monkeypatch.setattr(
        focus_mod, "_enum_wt_windows",
        lambda: [{"hwnd": 0xA, "pid": 1, "title": "✳ 别的"},
                 {"hwnd": 0xB, "pid": 1, "title": "◐ 目标会话"}],
    )
    monkeypatch.setattr(focus_mod, "console_window_owner", lambda pid: None)
    assert focus_mod._resolve_window(99, 1, ["目标会话"]) == 0xB


def test_resolve_window_ambiguous_gives_none(monkeypatch):
    monkeypatch.setattr(
        focus_mod, "_enum_wt_windows",
        lambda: [{"hwnd": 0xA, "pid": 1, "title": "同名"},
                 {"hwnd": 0xB, "pid": 1, "title": "同名"}],
    )
    monkeypatch.setattr(focus_mod, "console_window_owner", lambda pid: None)
    assert focus_mod._resolve_window(99, 1, ["同名"]) is None


def test_resolve_window_no_window_of_process(monkeypatch):
    monkeypatch.setattr(focus_mod, "_enum_wt_windows", lambda: [])
    assert focus_mod._resolve_window(99, 1, ["x"]) is None


# ---------------------------------------------------------------------------
# _locate_tab
# ---------------------------------------------------------------------------


def _no_marker(monkeypatch):
    """短标记通道整个断开(mark 失败),测试其余分支时零等待。"""
    monkeypatch.setattr(focus_mod, "mark_console", lambda pid: None)


def test_locate_tab_unreadable_window(monkeypatch):
    """UIA 读不到(提权窗口对普通权限服务):不报错,交调用方置前窗口。"""
    monkeypatch.setattr(focus_mod, "_window_tabs", lambda hwnd: None)
    tab, how = focus_mod._locate_tab(0xA, 99, ["x"], None)
    assert tab is None and how == "uia-unreadable"


def test_locate_tab_single_tab_wins_immediately(monkeypatch):
    only = _tab("随便什么标题,锁没锁都无所谓")
    monkeypatch.setattr(focus_mod, "_window_tabs", lambda hwnd: [only])
    tab, how = focus_mod._locate_tab(0xA, 99, ["对不上的候选"], None)
    assert tab is only and how == "single"


def test_locate_tab_title_unique_match(monkeypatch):
    t1, t2 = _tab("✳ 目标会话"), _tab("✳ 别的")
    monkeypatch.setattr(focus_mod, "_window_tabs", lambda hwnd: [t1, t2])
    tab, how = focus_mod._locate_tab(0xA, 99, ["目标会话"], None)
    assert tab is t1 and how == "title"


def test_locate_tab_subtract_beats_marker(monkeypatch):
    """锁题标签:标题对不上,但同窗另一会话认领了自己的标签 → 减法直接出,
    不必等标记(标记对锁题无效,白等)。"""
    locked, other = _tab("✳ 锁死的旧标题"), _tab("✳ 邻居会话")
    monkeypatch.setattr(focus_mod, "_window_tabs", lambda hwnd: [locked, other])
    monkeypatch.setattr(focus_mod, "console_window_owner",
                        lambda pid: 0xA if pid == 22 else None)
    monkeypatch.setattr(
        focus_mod, "mark_console",
        lambda pid: (_ for _ in ()).throw(AssertionError("减法已出,不该走标记")),
    )
    roster = [{"pid": 99, "names": ["目标"]}, {"pid": 22, "names": ["邻居会话"]}]
    tab, how = focus_mod._locate_tab(0xA, 99, ["目标"], roster)
    assert tab is locked and how == "subtract"


def test_locate_tab_marker_scoped(monkeypatch):
    """减法不干净(邻居也失配)时走窗内短标记:只轮询本窗标签引用。"""
    t1, t2 = _tab("✳ 甲"), _tab("✳ 乙")
    monkeypatch.setattr(focus_mod, "_window_tabs", lambda hwnd: [t1, t2])
    monkeypatch.setattr(focus_mod, "console_window_owner", lambda pid: None)

    def fake_mark(pid):
        t2.Name = focus_mod.marker_for(pid)  # 标记瞬时上屏(WT 转发毫秒级)
        return "✳ 乙"

    restored: list[str] = []
    monkeypatch.setattr(focus_mod, "mark_console", fake_mark)
    monkeypatch.setattr(
        focus_mod, "restore_console", lambda pid, s: (restored.append(s), True)[1]
    )
    tab, how = focus_mod._locate_tab(0xA, 4321, ["对不上"], None)
    assert tab is t2 and how == "marker"
    assert restored[-1] == "✳ 乙"  # finally 恢复原题


def test_locate_tab_all_miss_is_ambiguous_not_error(monkeypatch):
    t1, t2 = _tab("✳ 甲"), _tab("✳ 乙")
    monkeypatch.setattr(focus_mod, "_window_tabs", lambda hwnd: [t1, t2])
    monkeypatch.setattr(focus_mod, "console_window_owner", lambda pid: None)
    _no_marker(monkeypatch)
    tab, how = focus_mod._locate_tab(0xA, 99, ["对不上"], None)
    assert tab is None and how == "ambiguous"


# ---------------------------------------------------------------------------
# _subtract_in_window
# ---------------------------------------------------------------------------


def test_subtract_count_mismatch_gives_none(monkeypatch):
    """窗里标签数 ≠ 会话数(有非 CC 标签/分屏):放弃,不猜。"""
    tabs = [_tab("a"), _tab("b"), _tab("c")]
    monkeypatch.setattr(focus_mod, "console_window_owner",
                        lambda pid: 0xA if pid == 22 else None)
    roster = [{"pid": 22, "names": ["a"]}]  # 本窗只有 1 个邻居,标签却有 3 个
    assert focus_mod._subtract_in_window(
        0xA, 99, tabs, [t.Name for t in tabs], roster
    ) is None


def test_subtract_neighbor_ambiguous_gives_none(monkeypatch):
    tabs = [_tab("同名"), _tab("同名")]
    monkeypatch.setattr(focus_mod, "console_window_owner",
                        lambda pid: 0xA if pid == 22 else None)
    roster = [{"pid": 22, "names": ["同名"]}]
    assert focus_mod._subtract_in_window(
        0xA, 99, tabs, [t.Name for t in tabs], roster
    ) is None


def test_subtract_only_counts_sessions_of_this_window(monkeypatch):
    """成员资格由 owner 通道实证:别的窗的会话不掺和本窗的减法。"""
    locked, other = _tab("锁死"), _tab("邻居")
    owners = {22: 0xA, 33: 0xB}  # 33 在别的窗
    monkeypatch.setattr(focus_mod, "console_window_owner",
                        lambda pid: owners.get(pid))
    roster = [
        {"pid": 22, "names": ["邻居"]},
        {"pid": 33, "names": ["别窗会话"]},
        {"pid": 99, "names": ["目标"]},
    ]
    got = focus_mod._subtract_in_window(
        0xA, 99, [locked, other], ["锁死", "邻居"], roster
    )
    assert got is locked


# ---------------------------------------------------------------------------
# focus_session_by_pid / focus_group 编排
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pid", [None, 0, -1])
def test_focus_session_by_pid_rejects_invalid_pid(pid):
    assert focus_mod.focus_session_by_pid(pid, ["x"]) is None


def test_focus_session_own_window(monkeypatch):
    monkeypatch.setattr(focus_mod, "wt_host_of", lambda pid: None)
    monkeypatch.setattr(focus_mod, "host_kind", lambda pid: ("own-window", 0x77))
    fg: list[int] = []
    monkeypatch.setattr(focus_mod, "_force_foreground", lambda h: fg.append(h))
    res = focus_mod.focus_session_by_pid(99, ["x"])
    assert res["ok"] is True and res["tier"] == "window" and fg == [0x77]


def test_focus_session_headless_gives_none(monkeypatch):
    monkeypatch.setattr(focus_mod, "wt_host_of", lambda pid: None)
    monkeypatch.setattr(focus_mod, "host_kind", lambda pid: ("headless", 0))
    assert focus_mod.focus_session_by_pid(99, ["x"]) is None


def test_focus_session_full_channel_selects_tab(monkeypatch):
    monkeypatch.setattr(focus_mod, "wt_host_of", lambda pid: (211, 28420))
    monkeypatch.setattr(focus_mod, "_resolve_window", lambda pid, wt, c: 0xA)
    target = _tab("✳ 目标会话")
    monkeypatch.setattr(focus_mod, "_locate_tab",
                        lambda hwnd, pid, c, r: (target, "title"))
    sel: list = []
    monkeypatch.setattr(focus_mod, "_select_tab", lambda h, t: sel.append((h, t)))
    res = focus_mod.focus_session_by_pid(99, ["目标会话"])
    assert res["ok"] is True and res["tab_selected"] is True
    assert res["tier"] == "window-channel/title"
    assert sel == [(0xA, target)]


def test_focus_session_window_only_is_still_ok(monkeypatch):
    """标签定不出(提权窗口读不到子树):窗口置前照样 ok —— 窗口层由 OS 保证,
    "带到正确的窗前"永远好于报错(亚秒/无报错/聚焦正确的承诺)。"""
    monkeypatch.setattr(focus_mod, "wt_host_of", lambda pid: (211, 10728))
    monkeypatch.setattr(focus_mod, "_resolve_window", lambda pid, wt, c: 0x111)
    monkeypatch.setattr(focus_mod, "_locate_tab",
                        lambda hwnd, pid, c, r: (None, "uia-unreadable"))
    fg: list[int] = []
    monkeypatch.setattr(focus_mod, "_force_foreground", lambda h: fg.append(h))
    res = focus_mod.focus_session_by_pid(99, ["x"])
    assert res["ok"] is True and res["tab_selected"] is False
    assert res["tier"] == "window-channel" and res["tab_reason"] == "uia-unreadable"
    # 解析出窗口先置前一次(感知延迟),定不出标签后维持在前台即可
    assert fg and set(fg) == {0x111}


def test_focus_group_first_member_with_window_wins(monkeypatch):
    hit = {"ok": True, "tier": "window-channel/title", "verified": False,
           "matched_tab": "t", "window": None, "tab_selected": True}
    seq = {111: None, 222: hit}
    calls: list[int] = []

    def fake(pid, candidates, roster=None):
        calls.append(pid)
        return seq[pid]

    monkeypatch.setattr(focus_mod, "focus_session_by_pid", fake)
    res = focus_mod.focus_group([111, 222, 333], ["x"])
    assert res == hit
    assert calls == [111, 222]  # 命中即停


def test_focus_group_all_headless_reports_not_ok(monkeypatch):
    monkeypatch.setattr(
        focus_mod, "focus_session_by_pid", lambda pid, c, roster=None: None
    )
    res = focus_mod.focus_group([111, 222], ["x"])
    assert res["ok"] is False and res["tier"] == "none"
