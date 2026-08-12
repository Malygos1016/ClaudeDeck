from __future__ import annotations

import shutil

from app.render import (
    export_markdown,
    parse_view_items,
    render_markdown,
    resolve_transcript_path,
    window,
)

from factory import (
    SID,
    ai_title_line,
    assistant_line,
    attachment_line,
    compact_boundary_line,
    compact_summary_line,
    system_line,
    tool_result_user_line,
    user_line,
    write_jsonl,
)

PROJ = "C--Work-proj"


def make_file(cfg, objs):
    p = cfg.projects_root / PROJ / f"{SID}.jsonl"
    return write_jsonl(p, *objs)


def full_fixture():
    return [
        user_line(text="# 标题\n\n有 `code` 与 <script>alert(1)</script>", uuid="u-1"),
        assistant_line(
            texts=("回答**加粗**",), thinking="内心活动", uuid="a-1", ts="2026-08-11T06:15:00.000Z"
        ),
        tool_result_user_line(uuid="u-2"),
        system_line(uuid="s-1"),
        attachment_line(uuid="at-1"),
        compact_boundary_line(),
        compact_summary_line(),
        user_line(text="<command-name>/export</command-name><command-args>x.md</command-args>", uuid="u-3"),
        ai_title_line("标题行不占 seq"),
    ]


def test_view_items_and_kinds(cfg):
    p = make_file(cfg, full_fixture())
    items = parse_view_items(p)
    by_seq = {it["seq"]: it for it in items}

    # seq 0: 用户 markdown,raw HTML 被转义
    md = by_seq[0]["blocks"][0]
    assert md["kind"] == "md_html"
    assert "<h1>" in md["html"]
    assert "<script>" not in md["html"] and "&lt;script&gt;" in md["html"]

    # seq 1: assistant:text 渲染 + thinking 折叠 + tool_use 折叠
    kinds = [b["kind"] for b in by_seq[1]["blocks"]]
    assert kinds == ["thinking", "md_html", "tool_use"]
    assert by_seq[1]["blocks"][2]["name"] == "Read"

    # seq 2: 纯工具回执 → role=tool
    assert by_seq[2]["role"] == "tool"
    assert by_seq[2]["blocks"][0]["kind"] == "tool_result"

    # seq 3/4: system/attachment 默认隐藏
    assert by_seq[3]["hidden_default"] is True
    assert by_seq[4]["hidden_default"] is True

    # seq 5: compact_boundary 永远显示
    assert by_seq[5]["hidden_default"] is False
    assert by_seq[5]["blocks"][0]["kind"] == "compact_boundary"
    assert by_seq[5]["blocks"][0]["pre"] == 1000

    # seq 6: 压缩摘要折叠条
    assert by_seq[6]["blocks"][0]["kind"] == "compact_summary"

    # seq 7: 斜杠命令徽章
    cmd = by_seq[7]["blocks"][0]
    assert cmd["kind"] == "command" and cmd["name"] == "/export" and cmd["args"] == "x.md"

    # 控制行不占 seq:最大 seq == 7
    assert max(by_seq) == 7


def test_window_paging_and_filter(cfg):
    objs = [user_line(text=f"消息 {i}", uuid=f"u-{i}") for i in range(10)]
    objs.insert(5, system_line(uuid="s-x"))  # seq=5,默认隐藏
    p = make_file(cfg, objs)
    items = parse_view_items(p)

    tail = window(items, limit=4)
    assert tail["last_seq"] == 10 and tail["has_more_before"] is True
    assert tail["has_more_after"] is False

    mid = window(items, limit=4, around_seq=5, show_system=False)
    seqs = [it["seq"] for it in mid["items"]]
    assert 5 not in seqs  # 系统行被过滤
    mid2 = window(items, limit=4, around_seq=5, show_system=True)
    assert 5 in [it["seq"] for it in mid2["items"]]

    before = window(items, limit=3, before_seq=tail["first_seq"])
    assert before["last_seq"] < tail["first_seq"]
    after = window(items, limit=3, after_seq=before["last_seq"])
    assert after["first_seq"] == before["last_seq"] + 1


def test_view_cache_invalidation(cfg):
    p = make_file(cfg, [user_line(text="第一版")])
    items1 = parse_view_items(p)
    assert len(items1) == 1
    import time

    time.sleep(0.01)
    write_jsonl(p, user_line(text="重写"), user_line(text="第二条", uuid="u-9"))
    items2 = parse_view_items(p)
    assert len(items2) == 2  # mtime/size 变化后缓存失效


def test_resolve_falls_back_to_archive(cfg):
    p = make_file(cfg, [user_line(text="要归档的")])
    arch = cfg.archive_projects_root / PROJ / f"{SID}.jsonl"
    arch.parent.mkdir(parents=True)
    shutil.copy2(p, arch)

    path, src = resolve_transcript_path(cfg, PROJ, SID)
    assert src == "live" and path == p
    p.unlink()
    path, src = resolve_transcript_path(cfg, PROJ, SID)
    assert src == "archive" and path == arch
    arch.unlink()
    assert resolve_transcript_path(cfg, PROJ, SID) is None


def test_export_markdown(cfg):
    p = make_file(cfg, full_fixture())
    items = parse_view_items(p)
    md = export_markdown(items, {"title": "测试会话", "session_id": SID, "cwd": "C:\\x", "first_ts": "a", "last_ts": "b"})
    assert "title: 测试会话" in md
    assert "## 你(" in md and "## Claude(" in md
    assert "🔧 Read" in md
    assert "内心活动" not in md  # thinking 不导出
    assert "上下文已压缩" in md


def test_markdown_table_enabled():
    h = render_markdown("| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<table>" in h
