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
        "status_seconds": 1.0,
        "started_at": "2026-08-24T00:00:00.000Z",
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


# ---------- 父分支已关闭 ----------

def test_absent_fork_parent_shows_as_closed_node():
    """父分支关掉之后,fork 关系仍要在树里看得见,并且能一键恢复。

    用户实报:本对话明明是 fork 出来的,悬停却看不到树 —— 因为父会话进程已退出,
    组里只剩一个成员就不弹树了。但 fork 关系写在作业记录里,是这个会话的固有
    属性,不随父进程存活与否消失;父分支关掉之后反而更需要一个找回去的入口。
    """
    child = sess("c", tag="AA")
    jobs = [{"session_id": "c", "fork_parent_session_id": "gone"}]
    g = build_groups([child], jobs=jobs, absent_parents={"gone": {"label": "ClaudeDeck開發"}})[0]
    assert [m["session_id"] for m in g["members"]] == ["gone", "c"]
    ghost = g["members"][0]
    assert ghost["present"] is False
    assert ghost["action"] == "resume"      # 点它就把那条分支拉回来
    assert ghost["label"] == "ClaudeDeck開發"


def test_absent_parent_does_not_hijack_the_lamp():
    """已关闭的父不该影响格子的灯 —— 灯只反映还在跑的东西。"""
    child = sess("c", status="busy")
    jobs = [{"session_id": "c", "fork_parent_session_id": "gone"}]
    g = build_groups([child], jobs=jobs, absent_parents={"gone": {"label": "父"}})[0]
    assert g["status"] == "busy"


def test_absent_parent_keeps_window_and_focus_on_the_living_child():
    """幽灵节点没有进程,格子的跳转目标必须仍是活着的子。"""
    child = sess("c", tag="AA", has_terminal=True)
    jobs = [{"session_id": "c", "fork_parent_session_id": "gone"}]
    g = build_groups([child], jobs=jobs, absent_parents={"gone": {"label": "父"}})[0]
    assert g["has_window"] is True
    assert g["focus_session_id"] == "c"
    assert g["rename_session_id"] == "c"    # 重命名仍落在叉子后面那截


def test_absent_parent_restores_the_full_label():
    """格子名恢复成完整的「父 ⑂ 子」,而不是只剩子自己的名字。"""
    child = sess("c", tag="AA")
    jobs = [{"session_id": "c", "fork_parent_session_id": "gone"}]
    g = build_groups([child], jobs=jobs, absent_parents={"gone": {"label": "ClaudeDeck開發"}})[0]
    assert g["full_label"] == "ClaudeDeck開發 ⑂ AA"


def test_absent_parent_ignored_when_parent_is_actually_running():
    """父还活着就走原路径,不该凭空多出一个幽灵。"""
    parent = sess("p", tag="父")
    child = sess("c", kind="bg", has_terminal=False, tag="AA")
    jobs = [{"session_id": "c", "fork_parent_session_id": "p"}]
    g = build_groups([parent, child], jobs=jobs, absent_parents={"p": {"label": "不该用到"}})[0]
    assert [m["session_id"] for m in g["members"]] == ["p", "c"]
    assert g["members"][0].get("present") is not False


def test_absent_parent_without_label_is_skipped():
    """查不到名字的缺席父不造幽灵 —— 树里出现一行没名字的东西更糟。"""
    child = sess("c", tag="AA")
    jobs = [{"session_id": "c", "fork_parent_session_id": "gone"}]
    g = build_groups([child], jobs=jobs, absent_parents={})[0]
    assert [m["session_id"] for m in g["members"]] == ["c"]


# ---------- 同一会话多实例 ----------

def test_same_session_two_instances_collapse_to_one():
    """一个会话可能同时开着两个窗口,树里只该出现一条。

    用户实报:悬停时树上有三条 —— 两个同名的父 + 一个子。因为点 fork 父节点
    resume 之后,父会话就有了两个实例(原窗口那个 + 新恢复的),而分组是逐条收集
    成员的。这个状态会常态化,不是偶发。
    """
    old = sess("p", pid=1, status_seconds=21008)
    new = sess("p", pid=2, status_seconds=186)
    groups = build_groups([old, new], jobs=[])
    assert len(groups) == 1
    assert len(groups[0]["members"]) == 1
    assert groups[0]["members"][0]["pid"] == 2   # 留最近活跃的那个窗口


def test_dedup_keeps_the_one_with_known_freshness():
    """有的条目没有 status_seconds(从未上报过状态),不能让它盖掉有数据的。"""
    unknown = sess("p", pid=1, status_seconds=None)
    fresh = sess("p", pid=2, status_seconds=10)
    g = build_groups([unknown, fresh], jobs=[])[0]
    assert g["members"][0]["pid"] == 2


def test_fork_group_with_duplicated_parent_still_has_two_rows():
    """回归用户实报的那一幕:两个父实例 + 一个 fork 子 → 树里应是两行。"""
    p_old = sess("p", pid=1, status_seconds=21008, tag="ClaudeDeck開發")
    p_new = sess("p", pid=2, status_seconds=186, tag="ClaudeDeck開發")
    child = sess("c", kind="bg", has_terminal=False, tag="AA")
    jobs = [{"session_id": "c", "fork_parent_session_id": "p"}]
    g = build_groups([p_old, p_new, child], jobs=jobs)[0]
    assert [m["session_id"] for m in g["members"]] == ["p", "c"]


# ---------- 每个成员点下去做什么 ----------

def test_fork_family_actions_follow_registry_truth():
    """带终端的父=跳转,无窗守护子=恢复/接管。

    2026-08-26 实测推翻了 8/23 的「fork 就地发生、窗口跟子走」模型:/fork
    默认把子分支直接送进后台守护,窗口留在父这边。注册表条目(PID→sessionId)
    就是"窗口在跑谁"的直接事实:有终端的成员窗口显示的就是它自己。旧模型下
    点子分支被判成 focus,只会跳去父的窗口、永远弹不出新窗(用户实报
    "通过树点分支拉不起对话框")。
    """
    parent = sess("p", tag="父", has_terminal=True)
    child = sess("c", kind="bg", has_terminal=False)
    jobs = [{"session_id": "c", "fork_parent_session_id": "p"}]
    g = build_groups([parent, child], jobs=jobs)[0]
    acts = {m["session_id"]: m["action"] for m in g["members"]}
    assert acts["p"] == "focus"    # 父:窗口就是它的,跳过去
    assert acts["c"] == "resume"   # 子:无窗守护且无可接管作业,恢复=弹新窗


def test_fork_child_with_running_job_is_attach():
    """无窗守护子带着还在跑的作业:接管(新终端里打开),同样能弹出窗口。"""
    parent = sess("p", tag="父", has_terminal=True)
    child = sess("c", kind="bg", has_terminal=False, job_id="jc")
    jobs = [{"session_id": "c", "job_id": "jc", "state": "working",
             "fork_parent_session_id": "p"}]
    g = build_groups([parent, child], jobs=jobs)[0]
    assert {m["session_id"]: m["action"] for m in g["members"]}["c"] == "attach"


def test_dedup_prefers_recently_active_among_window_owners():
    """同一会话两个带窗实例并存:都真持有窗口(注册表事实),留最近活跃的
    那个 —— 那是用户正用着的。"""
    active = sess("p", pid=1, status_seconds=5)
    stale = sess("p", pid=2, status_seconds=900)
    child = sess("c", kind="bg", has_terminal=False)
    jobs = [{"session_id": "c", "fork_parent_session_id": "p"}]
    g = build_groups([active, stale, child], jobs=jobs)[0]
    parent_row = next(m for m in g["members"] if m["session_id"] == "p")
    assert parent_row["pid"] == 1
    assert parent_row["action"] == "focus"


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


# ---------- 作业链接:注册表 jobId 是权威 ----------

def test_job_found_via_registry_job_id_when_worker_sid_evolved():
    """fork 作业的 worker 每次 fork-resume 都换新 session id,而作业 state.json
    里的 session_id 停在创建时那一个(2026-08-25 实测:作业 9877837f 记着
    9877837f-…,活着的 worker 已是 ed2bd59a-…)。注册表条目的 jobId 才是权威
    链接:按它也必须查得到作业,否则 worker 算不出 attach、组的 attach_job_id
    是空 —— 顶栏格子点了没反应(用户实报:「Goal达到了吗」点不了)。"""
    worker = sess("ed2bd59a", kind="bg", has_terminal=False, job_id="9877837f")
    jobs = [{"job_id": "9877837f", "session_id": "9877837f-3296", "state": "working"}]
    g = build_groups([worker], jobs=jobs)[0]
    assert g["has_window"] is False
    assert g["residual"] is False
    assert g["attach_job_id"] == "9877837f"
    assert g["members"][0]["action"] == "attach"


def test_terminal_job_group_is_residual_not_attachable():
    """僵尸残留:无窗口 + 作业已终态(进程却还活着,否则注册表早剔除)。
    顶栏据 residual 隐藏(2026-08-25 用户拍板);attach 不再提供 —— 对终态
    作业 attach 等于重启,撞上残留进程启动即死(实报 exit 1 before init);
    动作给 resume,那才是"继续这段对话"的正路。"""
    worker = sess("w1", kind="bg", has_terminal=False, job_id="j1")
    jobs = [{"job_id": "j1", "session_id": "w1-old", "state": "stopped"}]
    g = build_groups([worker], jobs=jobs)[0]
    assert g["residual"] is True
    assert g["attach_job_id"] is None
    assert g["members"][0]["action"] == "resume"


def test_windowed_group_is_never_residual():
    g = build_groups([sess("a")], jobs=[])[0]
    assert g["residual"] is False


def test_running_bg_job_stays_visible_not_residual():
    """还在跑/卡住的后台作业绝不算残留 ——「在等你的必须看得见」戒律。"""
    worker = sess("w2", kind="bg", has_terminal=False, job_id="j2", status="idle")
    jobs = [{"job_id": "j2", "session_id": "w2", "tempo": "blocked", "needs": "要授权"}]
    g = build_groups([worker], jobs=jobs)[0]
    assert g["residual"] is False
    assert g["status"] == "waiting"


def test_blocked_job_maps_to_waiting_via_registry_job_id():
    """状态映射同样要走 jobId 链接:worker 的 session id 演化之后,它的作业
    blocked 也得亮红灯,不能因为查不到作业就绿灯装没事。"""
    worker = sess("w-live", kind="bg", has_terminal=False, job_id="j1", status="idle")
    jobs = [{"job_id": "j1", "session_id": "w-created", "tempo": "blocked"}]
    g = build_groups([worker], jobs=jobs)[0]
    assert g["status"] == "waiting"


def test_registry_job_link_does_not_override_direct_session_match():
    """session_id 直接对得上的作业不受影响:jobId 补挂只填空,不覆盖。"""
    a = sess("a", kind="bg", has_terminal=False, job_id="ja")
    jobs = [
        {"job_id": "ja", "session_id": "a", "tempo": "blocked"},
        {"job_id": "jb", "session_id": "b-gone", "tempo": "active"},
    ]
    g = build_groups([a], jobs=jobs)[0]
    assert g["status"] == "waiting"
    assert g["attach_job_id"] == "ja"


def test_ghost_root_keeps_attach_job_of_living_child():
    """幽灵父成根后 attach_job_id 不能算丢:根(幽灵)没有作业,接管入口要落
    到活着的 bg 子成员的非终态作业上 —— 否则无窗口 fork 组的格子点击两条
    分支都进不去,又回到"点了没反应"(38bd36c 审阅发现的回归)。"""
    child = sess("c1", kind="bg", has_terminal=False, job_id="j1")
    jobs = [{"job_id": "j1", "session_id": "c1", "state": "working",
             "fork_parent_session_id": "p-gone"}]
    g = build_groups([child], jobs, absent_parents={"p-gone": {"label": "父分支"}})[0]
    assert g["key"] == "p-gone"        # 幽灵确实成了根
    assert g["has_window"] is False
    assert g["attach_job_id"] == "j1"  # 接管入口落到活子的作业
    assert g["residual"] is False


def test_attach_viewer_gives_daemon_a_window():
    """被 `claude attach` 查看器显示的守护:自己无终端祖先,窗口却真实存在
    (2026-08-26 实测:/fork 交接后原窗口里跑的就是查看器,而查看器不进
    会话注册表 —— 不认它,一扇开着的活会话窗口就被当成不存在)。
    有查看器 → 有窗、owns、动作=跳转。"""
    d = sess("d1", kind="bg", has_terminal=False, job_id="jd", viewer_pid=3904)
    jobs = [{"job_id": "jd", "session_id": "d1", "state": "done"}]
    g = build_groups([d], jobs=jobs)[0]
    assert g["has_window"] is True
    assert g["residual"] is False
    assert g["members"][0]["owns_window"] is True
    assert g["members"][0]["action"] == "focus"


def test_daemon_without_viewer_stays_windowless():
    d = sess("d2", kind="bg", has_terminal=False, job_id="jd2")
    jobs = [{"job_id": "jd2", "session_id": "d2", "state": "working"}]
    g = build_groups([d], jobs=jobs)[0]
    assert g["has_window"] is False
    assert g["members"][0]["action"] == "attach"


def test_ghost_root_residual_follows_living_members():
    """残留判定同理按活着成员算:幽灵父 + 终态作业的僵尸子 → 该隐藏的还是
    要隐藏,不能因为根(幽灵)没作业就当正常组放出来。"""
    child = sess("c2", kind="bg", has_terminal=False, job_id="j2")
    jobs = [{"job_id": "j2", "session_id": "c2", "state": "failed",
             "fork_parent_session_id": "p-gone"}]
    g = build_groups([child], jobs, absent_parents={"p-gone": {"label": "父分支"}})[0]
    assert g["residual"] is True
    assert g["attach_job_id"] is None
