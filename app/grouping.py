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

from typing import Any, Iterable

FORK_MARK = "⑂"

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


def _job_index(jobs: Iterable[dict]) -> dict[str, dict]:
    return {j["session_id"]: j for j in jobs if j.get("session_id")}


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


def build_groups(sessions: list[dict], jobs: list[dict]) -> list[dict]:
    """把活跃会话按「所属窗口」聚成组。

    sessions 取 ``read_live_sessions()`` 的形状,jobs 取 ``read_jobs()`` 的形状。
    返回的每一组就是顶栏上的一个格子,``members`` 是悬停展开的那棵树。
    """
    by_job = _job_index(jobs)

    # 1. 丢掉空壳:它没有对话内容,点了也没有意义
    live = [s for s in sessions if not s.get("spare")]
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
        # 持有窗口的是 interactive 的那个;fork 出的守护进程没有自己的窗口
        window_owner = next(
            (m for m in members if m.get("kind") != "bg"),
            None,
        )
        has_window = window_owner is not None

        statuses = [_member_status(m, by_job.get(m.get("session_id"))) for m in members]
        status = min(statuses, key=_rank) if statuses else "idle"

        # 重命名落到「叉子后面那截」所属的会话:有 fork 子就是最后一个子,
        # 否则是自己。前半截是父的 tag,属于引用,不能在 fork 会话里改动。
        forked = _forked_members(root, members, by_job)
        rename_target = forked[-1] if forked else root
        full_label = _group_label(root, members, by_job)
        attach_job_id = None
        if not has_window:
            job = by_job.get(root_sid)
            attach_job_id = (job or {}).get("job_id")

        groups.append(
            {
                "key": root_sid,
                "label": truncate_label(full_label),
                "full_label": full_label,
                "status": status,
                "has_window": has_window,
                "focus_session_id": (window_owner or root).get("session_id"),
                "rename_session_id": rename_target.get("session_id"),
                "rename_hint": _fork_suffix(display_label(rename_target)),
                "attach_job_id": attach_job_id,
                "members": members,
            }
        )

    groups.sort(key=lambda g: (_rank(g["status"]), g["label"]))
    return groups


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
