"""三层聚焦入口(app/focus.py)的纯逻辑分支。

第 0/1 层(host_kind / 标记法)要真实 OS 窗口与 WT 才能验,人工验收。
这里打三段能纯 monkeypatch 掉外部依赖来测的逻辑:

- focus_by_title 的三条判定路径(零命中 / 单命中 / 多命中如实报歧义),
  用 SimpleNamespace 伪造 _collect_tabs 的 (window, tab) 产出,tab 只需要
  一个 .Name 属性;_select_tab 整个顶掉并记录调用,既验证"该不该选"也
  验证"选没选"。
- focus_session_by_pid 对非法 pid(None/0/负数)的参数校验:这条分支在
  调用 host_kind 之前就返回,不涉及任何真实 OS 查询。
- focus_group 的编排分支(快路径命中直接返回 / 零命中按序逐 pid 尝试 /
  全败沿用快路径结果):monkeypatch focus_by_title 与 focus_session_by_pid
  这两个模块级名字,经真实 _EXECUTOR 跑一轮——worker 线程查的也是模块
  globals,提交前打的补丁生效;_focus_thread_init 里的
  UIAutomationInitializerInThread 会真的执行一次 COM 初始化,但不碰任何
  窗口,已用真实 uiautomation 包验证可行。

focus_by_title 内部改走 _tabs() 缓存(全量扫描只在冷缓存时才做一次),
monkeypatch 掉的是更底层的 _collect_tabs——缓存热的时候根本不会被调用。
测试之间必须靠下面这个 autouse fixture 每次清缓存,否则会读到上一个
测试遗留的假数据(重构前 148 个断言里,有两个就是这样被缓存串没的:
_collect_tabs 换了新的假数据,但 _tabs() 命中热缓存直接把上一个测试的
旧数据发回去,判定路径全错)。

strip_glyph/match_tab 的测试留在 tests/test_focus_tags.py,不动。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import focus as focus_mod


@pytest.fixture(autouse=True)
def _reset_tab_cache():
    """每个测试前后都清一次标签引用缓存,防止假数据跨测试串味(见模块文档)。"""
    focus_mod._invalidate_tab_cache()
    yield
    focus_mod._invalidate_tab_cache()


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


# ---------------------------------------------------------------------------
# focus_group:顶层编排(快路径标题匹配 → 逐 pid 标记法 → 全败沿用快路径)。
# monkeypatch focus_by_title / focus_session_by_pid 这两个模块级名字本身,
# 而不是它们各自内部依赖的 _collect_tabs/host_kind —— focus_group 经真实
# _EXECUTOR 提交到 worker 线程执行,worker 线程查的也是 app.focus 模块的
# globals,提交前(主线程)打好的补丁一样生效。
# ---------------------------------------------------------------------------


def test_focus_group_fast_path_hit_skips_pid_attempts(monkeypatch):
    """快路径命中(ok=True):直接返回该结果,focus_session_by_pid 零调用。"""
    fast_hit = {
        "ok": True, "matched_tab": "✳ 目标会话", "window": "W1",
        "tier": "title", "verified": False, "ambiguous": None,
    }
    monkeypatch.setattr(focus_mod, "focus_by_title", lambda candidates: fast_hit)
    pid_calls: list[int | None] = []

    def fake_focus_by_pid(pid):
        pid_calls.append(pid)
        return {"ok": True, "tier": "marker"}

    monkeypatch.setattr(focus_mod, "focus_session_by_pid", fake_focus_by_pid)

    res = focus_mod.focus_group([111, 222], ["目标会话"])

    assert res == fast_hit
    assert pid_calls == []


def test_focus_group_zero_hits_tries_pids_in_order_until_hit(monkeypatch):
    """快路径零命中:按传入 pids 的顺序逐个试 focus_session_by_pid,第一个
    非 None 的结果即返回,排在后面的 pid 不该再被尝试。"""
    fast_miss = {
        "ok": False, "matched_tab": None, "window": None,
        "tier": "title", "ambiguous": None,
    }
    monkeypatch.setattr(focus_mod, "focus_by_title", lambda candidates: fast_miss)
    pid_calls: list[int | None] = []
    marker_hit = {
        "ok": True, "tier": "marker", "verified": True,
        "matched_tab": None, "window": None,
    }

    def fake_focus_by_pid(pid):
        pid_calls.append(pid)
        return marker_hit if pid == 222 else None

    monkeypatch.setattr(focus_mod, "focus_session_by_pid", fake_focus_by_pid)

    res = focus_mod.focus_group([111, 222, 333], ["目标会话"])

    assert res == marker_hit
    assert pid_calls == [111, 222]  # 命中即停,333 不该被尝试


def test_focus_group_all_fail_returns_fast_path_with_ambiguous(monkeypatch):
    """两层都没找到:回落到快路径的结果 —— ambiguous 字段原样透传,比逐 pid
    都失败后再造一个笼统的失败更有用(路由层靠这个字段区分 404 与 409)。"""
    fast_ambiguous = {
        "ok": False, "matched_tab": None, "window": None,
        "tier": "title", "ambiguous": ["✳ 目标会话", "◑ 目标会话"],
    }
    monkeypatch.setattr(focus_mod, "focus_by_title", lambda candidates: fast_ambiguous)
    pid_calls: list[int | None] = []

    def fake_focus_by_pid_always_none(pid):
        pid_calls.append(pid)
        return None

    monkeypatch.setattr(focus_mod, "focus_session_by_pid", fake_focus_by_pid_always_none)

    res = focus_mod.focus_group([111, 222], ["目标会话"])

    assert res == fast_ambiguous
    assert res["ambiguous"] == ["✳ 目标会话", "◑ 目标会话"]
    assert pid_calls == [111, 222]  # 两个都试过、都没戏,才回落快路径结果
