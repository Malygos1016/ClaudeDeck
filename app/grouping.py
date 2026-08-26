"""会话分组:把「一个会话一个格子」改成「一个窗口一个格子」。

设计见 docs/2026-08-23-session-tree-design.md。三条实测事实决定了这里的规则:

1. CC 会预热 ``spare: true`` 的空壳进程等着接下一轮活,里面一句话都没有。
   顶栏把它画成格子,就是用户看到的「多出来的那个块」。直接丢弃。
2. fork 出的会话是脱离终端的守护进程(进程链挂在 services.exe 下),但 fork
   是在父会话的窗口里就地发生的,窗口标题被改成了子会话的名字。父子物理上
   共用一个窗口,因此必须合成一个格子,且点击要去激活持有窗口的父。
3. 作业的 ``tempo=blocked`` / ``needs`` 与会话注册表的 ``waiting`` 是两套词汇,
   都表示「在等用户」,要映射到同一盏红灯,否则卡住的后台作业亮的是绿灯。

本模块只做纯数据变换,不碰文件系统、不碰 UI —— 便于单测覆盖全部分支。
"""
from __future__ import annotations

from typing import Iterable

FORK_MARK = "⑂"

# 作业的终态。终态作业不可 attach(那是重启尝试,撞上残留进程即死),
# 其无窗口的残留会话也不再占顶栏格子(2026-08-25 用户拍板:顶栏隐藏,
# deck 看板页仍可见、可清理)。
TERMINAL_JOB_STATES = {"stopped", "done", "failed"}

# 一个窗口一盏灯:取窗口内最紧急的状态。数字越小越紧急。
STATUS_RANK = {"waiting": 0, "busy": 1, "idle": 2}
_UNKNOWN_RANK = 3

LABEL_LIMIT = 24        # 顶栏格子上的字数上限;超出撑爆顶栏并带偏色带几何
ELLIPSIS = "…"


def truncate_label(text: str, limit: int = LABEL_LIMIT) -> str:
    """超长名字截断。CC 给 fork 会话的 name 直接是用户输入的整句 prompt。"""
    t = (text or "").strip()
    return t if len(t) <= limit else t[:limit] + ELLIPSIS


def display_label(session: dict) -> str:
    """单个会话的显示名:用户打的 tag > 索引出的标题 > CC 派生的名字。"""
    for key in ("tag", "title", "name"):
        v = (session.get(key) or "").strip()
        if v:
            return v
    return (session.get("session_id") or "?")[:8]


def _fork_suffix(name: str) -> str:
    """取 fork 名字里叉子后面那一截(可自定义的部分)。"""
    if FORK_MARK in name:
        return name.split(FORK_MARK, 1)[1].strip()
    return name.strip()


def _dedup_by_session(sessions: list[dict]) -> list[dict]:
    """同一个会话只留一条实例。

    一个会话可以同时开着多个窗口 —— 点 fork 父节点恢复之后就是两个
    (原窗口那个 + 新恢复的),这个状态是常态不是偶发。不去重的话树里会出现
    两条同名的父(用户实报:悬停时树上有三条)。

    优先留「窗口显示的就是它自己」的那个(见 _owns_window),其次才比谁更近活跃。
    只按活跃度挑会不稳:用户在旧窗口(跑着子分支)里一动,旧实例就成了最近活跃,
    父节点的动作会在 跳转/恢复 之间来回跳。
    """
    best: dict[str, dict] = {}
    for s in sessions:
        sid = s.get("session_id")
        prev = best.get(sid)
        if prev is None or _instance_rank(s) < _instance_rank(prev):
            best[sid] = s          # dict 替换值不改变插入顺序,原有次序得以保留
    return list(best.values())


def _instance_rank(s: dict) -> tuple[int, float]:
    return (0 if s.get("owns_window") else 1, _freshness(s))


def _mark_window_ownership(sessions: list[dict]) -> None:
    """标记每个实例的窗口是不是在显示它自己。

    注册表直接事实:sessions/<PID>.json 的 sessionId 写着这个终端进程当前在跑
    哪个会话 —— 有终端祖先的成员,窗口显示的就是它自己,不需要推断。

    2026-08-26 实测推翻了 8/23 的「fork 就地发生、窗口跟子走」模型:/fork
    默认把子分支直接送进后台守护,窗口留在父这边。旧的"按启动时刻与 fork
    时刻比"启发式在新行为下会把父的窗口错判给守护子(点父=另开重复窗、
    点子=跳去父窗且永远弹不出新窗,用户实报),废除;若日后再现"窗口跟子走"
    的模式,注册表条目的 sessionId 会自己切到子 sid,本判据依然成立。
    """
    for s in sessions:
        s["owns_window"] = bool(s.get("has_terminal"))


def _freshness(s: dict) -> float:
    """距上次状态更新的秒数,越小越新。缺数据的排在有数据的后面。"""
    v = s.get("status_seconds")
    return float(v) if isinstance(v, (int, float)) else float("inf")


def _job_index(jobs: Iterable[dict]) -> dict[str, dict]:
    return {j["session_id"]: j for j in jobs if j.get("session_id")}


def _reconcile_job_links(live: list[dict], jobs: list[dict], by_job: dict[str, dict]) -> None:
    """用会话注册表的 jobId 修正作业索引。

    fork 作业的 worker 会话每次 fork-resume 都换新 session id,而作业 state.json
    里的 session_id 停在创建时那一个(2026-08-25 实测:作业 9877837f 记着
    9877837f-…,活着的 worker 已是 ed2bd59a-…)。注册表条目的 jobId 才是权威
    链接 —— 按它把作业补挂到 worker 当前的 session id 下。不修的话该 worker
    查不到自己的作业:blocked/needs 映射不成 waiting、动作算不出 attach、
    组的 attach_job_id 是空(用户实报:格子点了没反应)。
    """
    by_jid = {j.get("job_id"): j for j in jobs if j.get("job_id")}
    for s in live:
        sid = s.get("session_id")
        job = by_jid.get(s.get("job_id") or "")
        if job is None or sid in by_job:
            continue
        # 同一个 dict 挂两个键即可,重复引用是幂等的;把 session_id 字段
        # 一并改正,让后续按 j.session_id 读作业的逻辑认得这个活 worker。
        job["session_id"] = sid
        by_job[sid] = job


def _add_absent_parents(
    live: list[dict], by_job: dict[str, dict], absent_parents: dict[str, dict]
) -> list[dict]:
    """为「已关闭的 fork 父分支」补一个幽灵成员,让 fork 关系不随父进程消失。

    幽灵没有进程,故 has_terminal/owns_window 皆假、状态记 closed(不参与灯的
    冒泡,STATUS_RANK 里没有它即排最末),动作自然落到 resume —— 点它就把那条
    分支拉回来。查不到名字的不造:树里多一行没名字的东西比不显示更糟。
    """
    present = {s.get("session_id") for s in live}
    ghosts: list[dict] = []
    seen: set[str] = set()
    for s in live:
        job = by_job.get(s.get("session_id"))
        parent = (job or {}).get("fork_parent_session_id")
        if not parent or parent in present or parent in seen:
            continue
        label = ((absent_parents.get(parent) or {}).get("label") or "").strip()
        if not label:
            continue
        seen.add(parent)
        ghosts.append({
            "session_id": parent,
            "pid": None,
            "kind": "absent",
            "status": "closed",
            "name": label,
            "title": None,
            "tag": None,
            "present": False,
            "alive": False,
            "has_terminal": False,
            "owns_window": False,
        })
    return ghosts + live if ghosts else live


def _job_wants_attention(job: dict | None) -> bool:
    """作业是否在等用户。blocked 与 needs 都算,两者 CC 会各用一个。"""
    if not job:
        return False
    return bool(job.get("tempo") == "blocked" or (job.get("needs") or "").strip())


def _member_status(session: dict, job: dict | None) -> str:
    if _job_wants_attention(job):
        return "waiting"
    return session.get("status") or "idle"


def _rank(status: str) -> int:
    return STATUS_RANK.get(status, _UNKNOWN_RANK)


def build_groups(
    sessions: list[dict],
    jobs: list[dict],
    absent_parents: dict[str, dict] | None = None,
) -> list[dict]:
    """把活跃会话按「所属窗口」聚成组。

    sessions 取 ``read_live_sessions()`` 的形状,jobs 取 ``read_jobs()`` 的形状。
    返回的每一组就是顶栏上的一个格子,``members`` 是悬停展开的那棵树。

    absent_parents 给出「已经关掉的 fork 父分支」的名字({sid: {"label": ...}}),
    由调用方从索引/标签里查好。fork 关系写在作业记录里,是子会话的固有属性,
    不随父进程存活与否消失,所以父关掉之后仍在树里列一个可恢复的灰节点。
    """
    by_job = _job_index(jobs)

    # 1. 丢掉不该占格子的:
    #    - spare 空壳:CC 预热的备用进程,没有对话内容,点了也没有意义
    #    - 脚本/SDK 派生的一次性会话(entrypoint=sdk-cli):用户没主动开、没有窗口
    #      可跳、跑完即退。实例是 EdgeTracer 每处理一条收藏派生一个 `claude -p`,
    #      显示出来只会不断闪现新格子(用户拍板不显示)。
    live = [
        s for s in sessions
        if not s.get("spare") and s.get("entrypoint", "cli") != "sdk-cli"
    ]
    _reconcile_job_links(live, jobs, by_job)
    _mark_window_ownership(live)
    live = _dedup_by_session(live)
    live = _add_absent_parents(live, by_job, absent_parents or {})
    by_sid = {s.get("session_id"): s for s in live}

    # 2. 认领:fork 子挂到父所在的组;父不在场时(已退出)自己独立成组
    parent_of: dict[str, str] = {}
    for s in live:
        sid = s.get("session_id")
        job = by_job.get(sid)
        pid_sid = (job or {}).get("fork_parent_session_id")
        if pid_sid and pid_sid in by_sid:
            parent_of[sid] = pid_sid

    def root_of(sid: str) -> str:
        seen = set()
        cur = sid
        while cur in parent_of and cur not in seen:
            seen.add(cur)
            cur = parent_of[cur]
        return cur

    buckets: dict[str, list[dict]] = {}
    for s in live:
        buckets.setdefault(root_of(s.get("session_id")), []).append(s)

    groups: list[dict] = []
    for root_sid, members in buckets.items():
        root = by_sid[root_sid]
        # 树里父在上、子缩进在下。入参沿用活跃会话的排序(忙的在前),
        # fork 子多半是 busy,不重排就会画成父子颠倒。
        members.sort(key=lambda m: 0 if m.get("session_id") == root_sid else 1)
        # 持有窗口的判据是「进程链上有终端祖先」,不是 kind。
        # kind=interactive 只说明不是 --bg 起的:EdgeTracer 的脚本会话也是
        # interactive,却挂在 python.exe 下、根本没有终端(用户实报的误判)。
        window_owner = next((m for m in members if m.get("has_terminal")), None)
        has_window = window_owner is not None

        statuses = [_member_status(m, by_job.get(m.get("session_id"))) for m in members]
        status = min(statuses, key=_rank) if statuses else "idle"

        # 重命名落到「叉子后面那截」所属的会话:有 fork 子就是最后一个子,
        # 否则是自己。前半截是父的 tag,属于引用,不能在 fork 会话里改动。
        forked = _forked_members(root, members, by_job)
        rename_target = forked[-1] if forked else root

        for m in members:
            m["action"] = _member_action(m, root_sid, bool(forked), has_window, by_job)
            # 树里每行的显示名后端算好,前端不再各算各的(幽灵节点也就自然有名字)
            m["label"] = display_label(m)

        full_label = _group_label(root, members, by_job)
        # 接管入口与残留判定都按「活着成员的作业」算,不能只看根:根可能是
        # 已关闭父分支的幽灵(没有进程也没有作业),只看根会把无窗口 fork 组的
        # attach_job_id 算丢 —— 格子点击两条分支都进不去,又回到"点了没反应"
        # (38bd36c 审阅发现的回归)。members 根在首位,顺序天然是根优先。
        living_jobs = [
            j for j in (
                by_job.get(m.get("session_id"))
                for m in members if m.get("present", True)
            ) if j
        ]
        attach_job_id = None
        if not has_window:
            attach_job_id = next(
                (j.get("job_id") for j in living_jobs
                 if j.get("job_id") and j.get("state") not in TERMINAL_JOB_STATES),
                None,
            )
        # 僵尸残留:无窗口 + 活着成员的作业全是终态,但进程还活着(否则注册表
        # 条目早被验活剔除)。它永远不会"等你",不占顶栏格子;数据保持诚实地
        # 返回,由前端各自决定画不画(deck 看板页显示并给清理入口)。
        residual = bool(
            not has_window and living_jobs
            and all(j.get("state") in TERMINAL_JOB_STATES for j in living_jobs)
        )

        groups.append(
            {
                "key": root_sid,
                "label": truncate_label(full_label),
                "full_label": full_label,
                "status": status,
                "has_window": has_window,
                "residual": residual,
                "focus_session_id": (window_owner or root).get("session_id"),
                "rename_session_id": rename_target.get("session_id"),
                "rename_hint": _fork_suffix(display_label(rename_target)),
                "attach_job_id": attach_job_id,
                "members": members,
            }
        )

    groups.sort(key=lambda g: (_rank(g["status"]), g["label"]))
    return groups


def _member_action(
    m: dict, root_sid: str, has_fork_child: bool, has_window: bool, by_job: dict[str, dict]
) -> str:
    """这一条点下去该做什么:focus(激活窗口) / resume(另开窗口) / attach(接管作业)。

    fork 的父节点必须是 resume:fork 在父会话的窗口里就地发生,那个窗口现在跑的是
    子分支,父分支在里面回不去了。而 fork 的意义正是一个上下文分支成两条各自推进,
    父分支得能单独拉起来。

    fork 的子节点仍是 focus —— 它自己是守护进程、没有终端祖先,但组里那个窗口
    显示的正是它,所以判据要看「组有没有窗口」,不能只看成员自己。
    """
    sid = m.get("session_id")
    if sid == root_sid and has_fork_child:
        # 恢复过一次之后父分支就有自己的窗口了,这时该跳过去而不是又开一个
        return "focus" if m.get("owns_window") else "resume"
    if m.get("has_terminal"):
        return "focus"
    if sid != root_sid and has_window and m.get("owns_window"):
        # 只有真正持有窗口的成员才是"跳转"。/fork 默认后台(2026-08-26 实测):
        # 子分支是无窗守护,窗口留在父 —— 点子分支该 接管/恢复(能弹出窗口),
        # 不是跳去父的窗口干瞪眼(用户实报"通过树点分支拉不起对话框")。
        return "focus"
    job = by_job.get(sid)
    if job and job.get("job_id") and job.get("state") not in TERMINAL_JOB_STATES:
        return "attach"
    # 终态作业不给 attach(重启尝试撞残留即死);resume 才是"继续这段对话"
    return "resume"


def _forked_members(root: dict, members: list[dict], by_job: dict[str, dict]) -> list[dict]:
    """组内由 fork 产生的成员(不含根)。"""
    return [
        m for m in members
        if m.get("session_id") != root.get("session_id")
        and (by_job.get(m.get("session_id")) or {}).get("fork_parent_session_id")
    ]


def _group_label(root: dict, members: list[dict], by_job: dict[str, dict]) -> str:
    """组的完整名字。

    命名规则(用户拍板):``<父的 tag> ⑂ <fork 后可自定义的部分>``。
    前半截取自父会话,是引用不是副本 —— 改父会话的 tag,所有子会话跟着变。
    """
    base = display_label(root)
    forked = _forked_members(root, members, by_job)
    if not forked:
        return base
    # 多重分叉时只展示最近一条的自定义部分,树里才逐条列全
    suffix = _fork_suffix(display_label(forked[-1]))
    return f"{base} {FORK_MARK} {suffix}" if suffix else f"{base} {FORK_MARK}"
