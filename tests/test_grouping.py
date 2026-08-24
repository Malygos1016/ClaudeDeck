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
        "status": "idle",
        "name": "w-1000",
        "title": None,
        "tag": None,
        "spare": False,
        "alive": True,
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
    g = build_groups([sess("lonely", kind="bg")], jobs=[])[0]
    assert g["has_window"] is False
    assert g["attach_job_id"] is None  # 没有 job 记录时无从 attach


def test_orphan_bg_with_job_can_attach():
    jobs = [{"session_id": "lonely", "job_id": "job123", "fork_parent_session_id": None}]
    g = build_groups([sess("lonely", kind="bg")], jobs=jobs)[0]
    assert g["has_window"] is False
    assert g["attach_job_id"] == "job123"


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
