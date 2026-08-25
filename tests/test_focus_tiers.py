"""三层聚焦入口(app/focus.py)的纯逻辑分支。

第 0/1 层(host_kind / 标记法)要真实 OS 窗口与 WT 才能验,人工验收。
这里只打两段能纯 monkeypatch 掉外部依赖来测的逻辑:

- focus_by_title 的三条判定路径(零命中 / 单命中 / 多命中如实报歧义),
  用 SimpleNamespace 伪造 _collect_tabs 的 (window, tab) 产出,tab 只需要
  一个 .Name 属性;_select_tab 整个顶掉并记录调用,既验证"该不该选"也
  验证"选没选"。
- focus_session_by_pid 对非法 pid(None/0/负数)的参数校验:这条分支在
  调用 host_kind 之前就返回,不涉及任何真实 OS 查询。

strip_glyph/match_tab 的测试留在 tests/test_focus_tags.py,不动。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import focus as focus_mod


def _tab(name: str):
    return SimpleNamespace(Name=name)


def _window(name: str = "窗口"):
    return SimpleNamespace(Name=name)


def test_focus_by_title_zero_hits_reports_not_found(monkeypatch):
    """零命中:如实报没找到({ok: False, ambiguous: None}),不该调用 _select_tab。"""
    monkeypatch.setattr(focus_mod, "_collect_tabs", lambda: [(_window(), _tab("✳ 别的会话"))])
    calls = []
    monkeypatch.setattr(focus_mod, "_select_tab", lambda w, t: calls.append((w, t)))

    res = focus_mod.focus_by_title(["目标会话"])

    assert res == {
        "ok": False,
        "matched_tab": None,
        "window": None,
        "tier": "title",
        "ambiguous": None,
    }
    assert calls == []


def test_focus_by_title_single_hit_selects_it(monkeypatch):
    """单命中:调用 _select_tab 完成选中,结果带 tier=='title'。"""
    win, tab = _window("W1"), _tab("✳ 目标会话")
    monkeypatch.setattr(focus_mod, "_collect_tabs", lambda: [(win, tab)])
    calls = []

    def fake_select(w, t):
        calls.append((w, t))
        return {"ok": True, "matched_tab": t.Name, "window": w.Name}

    monkeypatch.setattr(focus_mod, "_select_tab", fake_select)

    res = focus_mod.focus_by_title(["目标会话"])

    assert calls == [(win, tab)]
    assert res["ok"] is True
    assert res["tier"] == "title"
    assert res["ambiguous"] is None


def test_focus_by_title_multiple_hits_reports_ambiguous(monkeypatch):
    """多命中:不再盲取第一个 —— ok=False,ambiguous 列出全部命中的标签名,
    且不调用 _select_tab(没人授权选哪一个)。"""
    win = _window("W")
    tab1, tab2 = _tab("✳ 目标会话"), _tab("◑ 目标会话")
    monkeypatch.setattr(focus_mod, "_collect_tabs", lambda: [(win, tab1), (win, tab2)])
    calls = []
    monkeypatch.setattr(focus_mod, "_select_tab", lambda w, t: calls.append((w, t)))

    res = focus_mod.focus_by_title(["目标会话"])

    assert res["ok"] is False
    assert res["tier"] == "title"
    assert res["ambiguous"] == [tab1.Name, tab2.Name]
    assert calls == []


@pytest.mark.parametrize("pid", [None, 0, -1])
def test_focus_session_by_pid_rejects_invalid_pid(pid):
    """非法 pid 直接降级返回 None,不往下查 host_kind —— 纯参数校验,零副作用。"""
    assert focus_mod.focus_session_by_pid(pid) is None
