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


def test_marker_timeout_reports_suppressed_with_console_title(monkeypatch):
    """标记写进控制台了(mark 成功)却始终没上标签:不是"没找到",是 WT 对
    这个标签停止了应用标题转发(手动重命名锁题 / suppressApplicationTitle)。
    必须带回诊断与控制台真实标题让路由指路解锁,不能只回 None(用户实报:
    「CofeChat」格子点了没反应,查下来标签定格在旧 ai-title)。"""
    monkeypatch.setattr(focus_mod, "host_kind", lambda pid: ("wt-tab", 0))
    monkeypatch.setattr(focus_mod, "mark_console", lambda pid: "✳ 真实标题")
    restored: list[str] = []
    monkeypatch.setattr(
        focus_mod, "restore_console", lambda pid, t: (restored.append(t), True)[1]
    )
    monkeypatch.setattr(focus_mod, "_scan_cached_names", lambda marker: None)
    monkeypatch.setattr(focus_mod, "MARKER_WAIT_S", 0.05)

    res = focus_mod.focus_session_by_pid(4321)

    assert res is not None and res["ok"] is False
    assert res["wt_tab"] is True and res["reason"] == "marker-suppressed"
    assert res["console_title"] == "真实标题"  # strip_glyph 剥掉 ✳ 前缀
    assert restored[-1] == "✳ 真实标题"        # finally 里恢复了原题


def test_focus_group_carries_suppressed_diagnosis_over_pid_misses(monkeypatch):
    """锁题诊断不终止尝试:组里其他成员仍要逐个试(它们可能在没锁的标签里);
    全败时把诊断透传出去,路由靠 reason 给出解锁指引而不是笼统 404。"""
    fast_miss = {
        "ok": False, "matched_tab": None, "window": None,
        "tier": "title", "ambiguous": None,
    }
    monkeypatch.setattr(focus_mod, "focus_by_title", lambda candidates: fast_miss)
    suppressed = {
        "ok": False, "tier": "marker", "verified": False,
        "matched_tab": None, "window": None,
        "wt_tab": True, "reason": "marker-suppressed", "console_title": "真实标题",
    }
    seq = {111: None, 222: suppressed, 333: None}
    calls: list[int] = []

    def fake_focus_by_pid(pid):
        calls.append(pid)
        return seq[pid]

    monkeypatch.setattr(focus_mod, "focus_session_by_pid", fake_focus_by_pid)

    res = focus_mod.focus_group([111, 222, 333], ["x"])

    assert res["reason"] == "marker-suppressed"
    assert res["console_title"] == "真实标题"
    assert calls == [111, 222, 333]  # 诊断记下后,后面的成员照样都试过


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


# ---------------------------------------------------------------------------
# _deduce_unowned:排除法的演绎核心(第 2.5 层)。纯函数,零 mock。
# 前提逐条机器查验,任一不成立就失败 —— 绝不猜。
# ---------------------------------------------------------------------------


def test_deduce_unique_remainder():
    """闭世界 + 单射都成立:唯一无主的标签演绎上必属目标。"""
    res = focus_mod._deduce_unowned(
        ["✳ 锁死的旧标题", "✳ 会话A", "✳ 会话B"],
        [["会话A"], ["会话B"]],
    )
    assert res == {"index": 0}


def test_deduce_count_mismatch_fails():
    """标签数 ≠ 宿主单元数(有非 CC 标签/分屏/枚举竞态):闭世界破,诚实失败。"""
    res = focus_mod._deduce_unowned(["t1", "t2", "t3"], [["t1"]])
    assert res["fail"] == "count"


def test_deduce_unmatched_unit_fails():
    """某个其他会话一个标签都没命中(它的标题刚变、索引滞后):单射破,不硬猜。"""
    res = focus_mod._deduce_unowned(["锁死", "会话A"], [["已经改名的B"]])
    assert res["fail"] == "injective"


def test_deduce_ambiguous_unit_fails():
    """某个其他会话命中两个标签(同名标签):单射破。"""
    res = focus_mod._deduce_unowned(["同名", "同名", "锁死"], [["同名"], ["别的"]])
    assert res["fail"] == "injective"


def test_deduce_double_claim_fails():
    """两个会话认领同一个标签:单射破。"""
    res = focus_mod._deduce_unowned(["X", "锁死", "C"], [["X"], ["X"]])
    assert res["fail"] == "injective"


def test_deduce_swapped_claims_still_correct():
    """互换认领(甲命中乙的标签、乙命中甲的):照样通过 —— 结论只依赖无主
    集合,不依赖认领分配本身正确(对抗性审查 2026-08-25 论证)。"""
    res = focus_mod._deduce_unowned(["锁死", "甲", "乙"], [["乙"], ["甲"]])
    assert res == {"index": 0}


# ---------------------------------------------------------------------------
# focus_by_elimination:宿主归并 / 提权窗口两边剔除 / 唯一则选中。
# monkeypatch 掉 wt_host_of 与 _collect_windows,演绎本身走真代码。
# ---------------------------------------------------------------------------


def _fake_select(picked: list):
    def sel(w, t):
        picked.append(t)
        return {"ok": True, "matched_tab": t.Name, "window": w.Name}
    return sel


def test_elimination_unique_selects(monkeypatch):
    tabs = [_tab("✳ 会话A"), _tab("✳ 锁死旧题"), _tab("✳ 会话B")]
    w = _window("W")
    monkeypatch.setattr(
        focus_mod, "_collect_windows",
        lambda: [{"window": w, "wt_pid": 100, "tabs": tabs}],
    )
    hosts = {11: (211, 100), 22: (222, 100), 33: (233, 100)}
    monkeypatch.setattr(focus_mod, "wt_host_of", lambda pid: hosts.get(pid))
    picked: list = []
    monkeypatch.setattr(focus_mod, "_select_tab", _fake_select(picked))

    roster = [
        {"pid": 11, "names": ["会话A"]},
        {"pid": 22, "names": ["会话B"]},
        {"pid": 33, "names": ["CofeChat", "vibecoding"]},  # 目标:名字全对不上标签
    ]
    res = focus_mod.focus_by_elimination({33}, roster)

    assert res["ok"] is True
    assert res["tier"] == "elimination" and res["verified"] is False
    assert picked[0].Name == "✳ 锁死旧题"


def test_elimination_dead_window_excluded_both_sides(monkeypatch):
    """0 标签窗口(UIA 读不到,典型提权 WT)与宿主挂它下面的单元两边剔除,
    闭世界在可枚举宇宙内重建,演绎照常。"""
    tabs = [_tab("✳ 会话A"), _tab("✳ 锁死旧题")]
    monkeypatch.setattr(
        focus_mod, "_collect_windows",
        lambda: [
            {"window": _window("W"), "wt_pid": 100, "tabs": tabs},
            {"window": _window("admin"), "wt_pid": 999, "tabs": []},  # 读不到
        ],
    )
    hosts = {11: (211, 100), 22: (222, 999), 33: (233, 100)}  # 22 在提权窗口里
    monkeypatch.setattr(focus_mod, "wt_host_of", lambda pid: hosts.get(pid))
    picked: list = []
    monkeypatch.setattr(focus_mod, "_select_tab", _fake_select(picked))

    roster = [
        {"pid": 11, "names": ["会话A"]},
        {"pid": 22, "names": ["管理员会话"]},
        {"pid": 33, "names": ["目标"]},
    ]
    res = focus_mod.focus_by_elimination({33}, roster)

    assert res["ok"] is True
    assert res["matched_tab"] == "✳ 锁死旧题"


def test_elimination_invisible_wt_process_excluded_both_sides(monkeypatch):
    """提权 WT 在普通权限服务的 UIA 里**整个窗口都不出现**(2026-08-25 计划
    任务语境实证,不是"窗口在、0 标签")——宿主挂在无任何可枚举窗口的 WT
    进程下的单元,同样两边剔除。"""
    tabs = [_tab("✳ 会话A"), _tab("✳ 锁死旧题")]
    monkeypatch.setattr(
        focus_mod, "_collect_windows",
        lambda: [{"window": _window("W"), "wt_pid": 100, "tabs": tabs}],
        # 注意:没有 wt_pid=999 的窗口条目 —— 它整个不可见
    )
    hosts = {11: (211, 100), 22: (222, 999), 33: (233, 100)}  # 22 在提权 WT 里
    monkeypatch.setattr(focus_mod, "wt_host_of", lambda pid: hosts.get(pid))
    picked: list = []
    monkeypatch.setattr(focus_mod, "_select_tab", _fake_select(picked))

    roster = [
        {"pid": 11, "names": ["会话A"]},
        {"pid": 22, "names": ["管理员会话"]},
        {"pid": 33, "names": ["目标"]},
    ]
    res = focus_mod.focus_by_elimination({33}, roster)

    assert res["ok"] is True
    assert res["matched_tab"] == "✳ 锁死旧题"


def test_elimination_target_in_invisible_window_fails_honestly(monkeypatch):
    """目标自己在读不到的窗口里:没法演绎,如实失败,不硬猜。"""
    monkeypatch.setattr(
        focus_mod, "_collect_windows",
        lambda: [{"window": _window("W"), "wt_pid": 100, "tabs": [_tab("✳ 会话A")]}],
    )
    hosts = {11: (211, 100), 33: (233, 999)}  # 目标 33 在不可见的 999 里
    monkeypatch.setattr(focus_mod, "wt_host_of", lambda pid: hosts.get(pid))

    res = focus_mod.focus_by_elimination(
        {33}, [{"pid": 11, "names": ["会话A"]}, {"pid": 33, "names": ["目标"]}]
    )

    assert res["ok"] is False
    assert res["elimination_fail"] == "target-window-unreadable"


def test_elimination_shared_host_units_merge(monkeypatch):
    """fork 就地共窗:两个会话同一宿主 → 合并成一个单元,计数口径才与标签
    对得上(对抗性审查修正 1:计数单位是宿主,不是会话)。"""
    tabs = [_tab("✳ 父标题"), _tab("✳ 锁死旧题")]
    monkeypatch.setattr(
        focus_mod, "_collect_windows",
        lambda: [{"window": _window("W"), "wt_pid": 100, "tabs": tabs}],
    )
    hosts = {11: (211, 100), 12: (211, 100), 33: (233, 100)}  # 11/12 共宿主 211
    monkeypatch.setattr(focus_mod, "wt_host_of", lambda pid: hosts.get(pid))
    picked: list = []
    monkeypatch.setattr(focus_mod, "_select_tab", _fake_select(picked))

    roster = [
        {"pid": 11, "names": ["父标题"]},
        {"pid": 12, "names": ["子分支名"]},
        {"pid": 33, "names": ["目标"]},
    ]
    res = focus_mod.focus_by_elimination({33}, roster)

    assert res["ok"] is True
    assert res["matched_tab"] == "✳ 锁死旧题"


# ---------------------------------------------------------------------------
# focus_group 编排:wt-tab 失败证据触发排除法;全 headless 绝不触发。
# ---------------------------------------------------------------------------


def test_focus_group_evidence_triggers_elimination(monkeypatch):
    fast_miss = {
        "ok": False, "matched_tab": None, "window": None,
        "tier": "title", "ambiguous": None,
    }
    monkeypatch.setattr(focus_mod, "focus_by_title", lambda candidates: fast_miss)
    suppressed = {
        "ok": False, "tier": "marker", "verified": False,
        "matched_tab": None, "window": None,
        "wt_tab": True, "reason": "marker-suppressed", "console_title": "t",
    }
    monkeypatch.setattr(focus_mod, "focus_session_by_pid", lambda pid: suppressed)
    elim_hit = {
        "ok": True, "tier": "elimination", "verified": False,
        "matched_tab": "✳ 锁死旧题", "window": "W", "ambiguous": None,
    }
    elim_calls: list = []

    def fake_elim(pids, roster):
        elim_calls.append((pids, roster))
        return elim_hit

    monkeypatch.setattr(focus_mod, "focus_by_elimination", fake_elim)

    roster = [{"pid": 42, "names": ["目标"]}]
    res = focus_mod.focus_group([42], ["目标"], roster=roster)

    assert res == elim_hit
    assert elim_calls and elim_calls[0][0] == {42}


def test_focus_group_headless_only_never_runs_elimination(monkeypatch):
    """没有任何 wt-tab 证据(全 headless)时排除法不该被碰 —— 它的前提 1
    (目标确在某标签里)都不成立,跑了就是猜。"""
    fast_miss = {
        "ok": False, "matched_tab": None, "window": None,
        "tier": "title", "ambiguous": None,
    }
    monkeypatch.setattr(focus_mod, "focus_by_title", lambda candidates: fast_miss)
    monkeypatch.setattr(focus_mod, "focus_session_by_pid", lambda pid: None)

    def boom(*a):
        raise AssertionError("排除法不该被调用")

    monkeypatch.setattr(focus_mod, "focus_by_elimination", boom)

    res = focus_mod.focus_group([42], ["目标"], roster=[{"pid": 42, "names": ["目标"]}])

    assert res["ok"] is False and res["tier"] == "title"
