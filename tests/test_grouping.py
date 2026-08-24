"""会话分组:把「一个会话一个格子」改成「一个窗口一个格子」。

背景见 docs/2026-08-23-session-tree-design.md。核心事实:
- CC 会预热 spare=true 的空壳进程,里面一句话都没有,不该出现在界面上
- fork 出的会话是脱离终端的守护进程,但它与父会话共用同一个窗口
- 作业的 blocked/needs 与会话的 waiting 是两套词汇,需要映射到同一盏灯
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from app.grouping import (
    STATUS_RANK,
    build_groups,
    display_label,
    truncate_label,
)


def write_session(cfg, pid: int, sid: str, **over) -> None:
    sess_dir = cfg.claude_home_path / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    now_ms = int(time.time() * 1000)
    d = {
        "pid": pid,
        "sessionId": sid,
        "cwd": r"C:\Work\proj",
        "startedAt": now_ms - 60_000,
        "kind": "interactive",
        "name": f"w-{pid}",
        "status": "idle",
        "updatedAt": now_ms,
        "statusUpdatedAt": now_ms,
    }
    d.update(over)
    (sess_dir / f"{pid}.json").write_text(json.dumps(d), encoding="utf-8")


def write_job(cfg, job_id: str, sid: str, **over) -> None:
    jd = cfg.claude_home_path / "jobs" / job_id
    jd.mkdir(parents=True, exist_ok=True)
    d = {"sessionId": sid, "state": "working", "tempo": "active", "name": job_id}
    d.update(over)
    (jd / "state.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def sess(sid: str, **over) -> dict:
    """构造一条 read_live_sessions 风格的记录。"""
    d = {
        "session_id": sid,
        "pid": 1000,
        "kind": "interactive",
        "entrypoint": "cli",
        "status": "idle",
        "name": "w-1000",
        "title": None,
        "tag": None,
        "spare": False,
        "alive": True,
        # 进程链上有没有终端祖先。kind 不能当窗口判据(见对应测试)
        "has_terminal": True,
    }
    d.update(over)
    return d


# ---------- 空壳过滤 ----------

def test_spare_session_is_dropped():
    """CC 预热的备用进程(spare=true)是空壳,不产生任何格子。"""
    groups = build_groups([sess("a"), sess("b", spare=True)], jobs=[])
    ids = [m["session_id"] for g in groups for m in g["members"]]
    assert ids == ["a"]


def test_spare_only_yields_no_groups():
    groups = build_groups([sess("s", spare=True)], jobs=[])
    assert groups == []


# ---------- 脚本派生的一次性会话 ----------

def test_sdk_cli_session_is_dropped():
    """脚本/SDK 调起的一次性会话不进顶栏(用户拍板)。

    实例:EdgeTracer(QQ 收藏箱机器人)每处理一条收藏就派生一个
    `claude -p ... --output-format json`,entrypoint=sdk-cli、父进程是 python、
    连 status 字段都没有。用户没主动开它,也没有窗口可跳,显示出来只会不断闪现
    新格子;它本身通过 QQ 回执,不需要顶栏再提醒一遍。
    """
    groups = build_groups(
        [sess("a"), sess("bot", entrypoint="sdk-cli", status=None)], jobs=[]
    )
    ids = [m["session_id"] for g in groups for m in g["members"]]
    assert ids == ["a"]


def test_normal_cli_entrypoint_is_kept():
    groups = build_groups([sess("a", entrypoint="cli")], jobs=[])
    assert [g["key"] for g in groups] == ["a"]


def test_kind_interactive_alone_does_not_mean_it_has_a_window():
    """kind=interactive 只说明「不是 --bg 起的」,不等于有终端窗口。

    第一版拿 kind 当窗口判据,于是把 EdgeTracer 的脚本会话判成了有窗口,
    顶栏给它画了格子、点击必然失败(用户实报)。窗口归属以进程链为准。
    """
    g = build_groups(
        [sess("x", kind="interactive", has_terminal=False, entrypoint="cli")], jobs=[]
    )[0]
    assert g["has_window"] is False


def test_terminal_ancestry_decides_window_owner():
    parent = sess("p", kind="interactive", has_terminal=True)
    child = sess("c", kind="bg", has_terminal=False)
    jobs = [{"session_id": "c", "fork_parent_session_id": "p"}]
    g = build_groups([parent, child], jobs=jobs)[0]
    assert g["has_window"] is True
    assert g["focus_session_id"] == "p"


# ---------- fork 父子合并 ----------

def test_fork_child_merges_into_parent_group():
    """fork 是就地发生的,父子共用一个窗口,必须合成一个格子。"""
    parent = sess("parent", kind="interactive", status="idle")
    child = sess("child", kind="bg", status="busy")
    jobs = [{"session_id": "child", "fork_parent_session_id": "parent"}]
    groups = build_groups([parent, child], jobs=jobs)
    assert len(groups) == 1
    assert {m["session_id"] for m in groups[0]["members"]} == {"parent", "child"}


def test_fork_group_focuses_the_window_owner():
    """点击合并后的格子,要去激活真正持有窗口的那个(父),不是守护进程。"""
    parent = sess("parent", kind="interactive")
    child = sess("child", kind="bg")
    jobs = [{"session_id": "child", "fork_parent_session_id": "parent"}]
    g = build_groups([parent, child], jobs=jobs)[0]
    assert g["focus_session_id"] == "parent"
    assert g["has_window"] is True


def test_orphan_bg_group_has_no_window():
    """没有 fork 父的守护进程是真正的游离作业,没有窗口可跳。"""
    g = build_groups([sess("lonely", kind="bg", has_terminal=False)], jobs=[])[0]
    assert g["has_window"] is False
    assert g["attach_job_id"] is None  # 没有 job 记录时无从 attach


def test_orphan_bg_with_job_can_attach():
    jobs = [{"session_id": "lonely", "job_id": "job123", "fork_parent_session_id": None}]
    g = build_groups([sess("lonely", kind="bg", has_terminal=False)], jobs=jobs)[0]
    assert g["has_window"] is False
    assert g["attach_job_id"] == "job123"


# ---------- 每个成员点下去做什么 ----------

def test_fork_parent_action_is_resume():
    """点 fork 的父节点要 resume 开新窗口,不是激活窗口。

    fork 是在父会话的窗口里就地发生的,那个窗口现在跑的是子分支,父分支在里面
    回不去了。而 fork 的意义正是一个上下文分支成两条各自推进(用户原话),
    父分支必须能单独拉起来,否则 fork 就废了一半。
    """
    parent = sess("p", tag="父", has_terminal=True)
    child = sess("c", kind="bg", has_terminal=False)
    jobs = [{"session_id": "c", "fork_parent_session_id": "p"}]
    g = build_groups([parent, child], jobs=jobs)[0]
    acts = {m["session_id"]: m["action"] for m in g["members"]}
    assert acts["p"] == "resume"   # 父:窗口已被子占,只能另开
    assert acts["c"] == "focus"    # 子:窗口里跑的就是它,跳过去即可


def test_plain_session_action_is_focus():
    """没 fork 过的普通会话,窗口就是它自己的,点了跳过去,不该另开一个。"""
    g = build_groups([sess("a", has_terminal=True)], jobs=[])[0]
    assert g["members"][0]["action"] == "focus"


def test_orphan_bg_action_is_attach():
    jobs = [{"session_id": "lonely", "job_id": "j1", "fork_parent_session_id": None}]
    g = build_groups([sess("lonely", kind="bg", has_terminal=False)], jobs=jobs)[0]
    assert g["members"][0]["action"] == "attach"


def test_bg_without_job_falls_back_to_resume():
    """没有 job 记录就无从 attach,只能按会话 resume。"""
    g = build_groups([sess("x", kind="bg", has_terminal=False)], jobs=[])[0]
    assert g["members"][0]["action"] == "resume"


# ---------- 状态冒泡 ----------

def test_status_bubbles_to_most_urgent():
    """一个窗口一盏灯:有人等你就红,有人在忙就黄,都闲才绿。"""
    parent = sess("p", status="idle")
    child = sess("c", kind="bg", status="busy")
    jobs = [{"session_id": "c", "fork_parent_session_id": "p"}]
    assert build_groups([parent, child], jobs=jobs)[0]["status"] == "busy"


def test_waiting_beats_busy():
    parent = sess("p", status="waiting")
    child = sess("c", kind="bg", status="busy")
    jobs = [{"session_id": "c", "fork_parent_session_id": "p"}]
    assert build_groups([parent, child], jobs=jobs)[0]["status"] == "waiting"


def test_blocked_job_bubbles_red():
    """藏进树里的后台作业一旦卡住,必须让它所属的格子变红——用户明确要的。"""
    parent = sess("p", status="idle")
    child = sess("c", kind="bg", status="idle")
    jobs = [{"session_id": "c", "fork_parent_session_id": "p", "tempo": "blocked"}]
    assert build_groups([parent, child], jobs=jobs)[0]["status"] == "waiting"


def test_needs_field_also_bubbles_red():
    parent = sess("p", status="idle")
    child = sess("c", kind="bg", status="idle")
    jobs = [{"session_id": "c", "fork_parent_session_id": "p", "needs": "send a prompt"}]
    assert build_groups([parent, child], jobs=jobs)[0]["status"] == "waiting"


def test_status_rank_order():
    assert STATUS_RANK["waiting"] < STATUS_RANK["busy"] < STATUS_RANK["idle"]


# ---------- 标签 ----------

def test_truncate_keeps_head_and_marks_elision():
    long = "Claudedeck本地安裝 ⑂ 我現在fork了，如果我沒記錯的話，上面topbar會有兩個這個標題"
    out = truncate_label(long, limit=20)
    assert len(out) <= 21          # 20 + 省略号
    assert out.startswith("Claudedeck本地安裝")
    assert out.endswith("…")


def test_truncate_leaves_short_label_alone():
    assert truncate_label("AALab", limit=20) == "AALab"


def test_label_prefers_tag_over_name():
    """用户自己打的 tag 优先于 CC 派生的名字。"""
    assert display_label(sess("a", tag="ClaudeDeck開發", name="46953-77")) == "ClaudeDeck開發"


def test_label_falls_back_to_title_then_name():
    assert display_label(sess("a", title="某个标题", name="46953-77")) == "某个标题"
    assert display_label(sess("a", name="46953-77")) == "46953-77"


def test_fork_group_label_is_parent_tag_plus_fork_suffix():
    """命名规则:<父的 tag> ⑂ <fork 后可自定义的部分>。前半截来自父,是引用不是副本。"""
    parent = sess("p", tag="ClaudeDeck開發")
    child = sess("c", kind="bg", name="Claudedeck本地安裝 ⑂ 我現在fork了，如果我沒記錯的話")
    jobs = [{"session_id": "c", "fork_parent_session_id": "p"}]
    g = build_groups([parent, child], jobs=jobs)[0]
    assert g["full_label"].startswith("ClaudeDeck開發 ⑂ ")
    assert "我現在fork了" in g["full_label"]
    # 顶栏上必须是截断过的,否则一个格子撑爆整条顶栏并带偏色带几何
    assert len(g["label"]) < len(g["full_label"])


def test_rename_target_is_the_fork_child_not_the_parent():
    """在 fork 格子上重命名,改的必须是叉子后面那截 —— 即落到子会话身上。

    用户实报:打了「hi」结果把前面的「ClaudeDeck開發」整个替换掉了。
    原因是格子的 sid 用了 focus_session_id(父会话,拿来激活窗口的),
    重命名顺着它把 tag 打到了父身上。前半截是父的 tag,属于引用,
    要改得去父会话改,不能在 fork 会话里动。
    """
    parent = sess("p", tag="ClaudeDeck開發")
    child = sess("c", kind="bg", name="X ⑂ 我現在fork了")
    jobs = [{"session_id": "c", "fork_parent_session_id": "p"}]
    g = build_groups([parent, child], jobs=jobs)[0]
    assert g["focus_session_id"] == "p"      # 激活窗口找父
    assert g["rename_session_id"] == "c"     # 重命名落到子
    assert g["rename_hint"] == "我現在fork了"  # 编辑框里应带出的现值


def test_members_put_root_first_then_forks():
    """树里父在上、子缩进在下。members 若沿用活跃会话的排序(忙的在前),
    fork 子是 busy 就会跑到父前面,树画出来父子颠倒。"""
    parent = sess("p", status="idle", tag="父")
    child = sess("c", kind="bg", status="busy", tag="子")
    jobs = [{"session_id": "c", "fork_parent_session_id": "p"}]
    # 故意按「子在前」传入,模拟 read_live_sessions 的 busy 优先排序
    g = build_groups([child, parent], jobs=jobs)[0]
    assert [m["session_id"] for m in g["members"]] == ["p", "c"]


def test_rename_target_is_self_when_not_forked():
    g = build_groups([sess("a", tag="AALab")], jobs=[])[0]
    assert g["rename_session_id"] == "a"
    assert g["rename_hint"] == "AALab"


def test_renaming_fork_child_only_changes_the_suffix():
    """给子会话打了 tag 之后,前半截仍取自父,只有叉子后面变成新名字。"""
    parent = sess("p", tag="ClaudeDeck開發")
    child = sess("c", kind="bg", tag="hi", name="X ⑂ 我現在fork了")
    jobs = [{"session_id": "c", "fork_parent_session_id": "p"}]
    g = build_groups([parent, child], jobs=jobs)[0]
    assert g["full_label"] == "ClaudeDeck開發 ⑂ hi"


def test_plain_group_label_has_no_fork_marker():
    g = build_groups([sess("a", tag="AALab")], jobs=[])[0]
    assert g["label"] == "AALab"
    assert "⑂" not in g["label"]


# ---------- 排序 ----------

def test_groups_sort_urgent_first():
    a = sess("a", status="idle", tag="闲的")
    b = sess("b", status="waiting", tag="等你的")
    c = sess("c", status="busy", tag="忙的")
    labels = [g["label"] for g in build_groups([a, b, c], jobs=[])]
    assert labels == ["等你的", "忙的", "闲的"]
