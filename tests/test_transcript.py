from __future__ import annotations

import io
import json

from app.transcript import bridge_url, clean_user_title, parse_chunk

from factory import (
    ai_title_line,
    assistant_line,
    attachment_line,
    bridge_line,
    compact_boundary_line,
    compact_summary_line,
    jsonl_bytes,
    last_prompt_line,
    mode_line,
    permission_mode_line,
    queue_operation_line,
    tool_result_user_line,
    user_line,
)


def parse_bytes(data: bytes, **kw):
    return parse_chunk(io.BytesIO(data), **kw)


def test_user_string_content_indexed():
    res = parse_bytes(jsonl_bytes(user_line(text="设计77K超导带材通流实验")))
    assert res.msg_count == 1
    assert len(res.rows) == 1
    r = res.rows[0]
    assert r.kind == "user_text"
    assert "77K" in r.text
    assert r.seq == 0
    assert r.byte_offset == 0
    assert res.first_user_text.startswith("设计77K")
    assert res.cwd == r"C:\Work\proj"


def test_command_and_compact_summary_not_indexed():
    res = parse_bytes(
        jsonl_bytes(
            user_line(text="<command-name>/login</command-name>", uuid="u-cmd"),
            compact_summary_line(),
        )
    )
    assert res.rows == []
    assert res.msg_count == 2  # 行数照计,只是不进索引
    assert res.first_user_text is None


def test_user_array_content_tool_result_skipped():
    line = tool_result_user_line()
    res = parse_bytes(jsonl_bytes(line))
    assert res.rows == []
    # 带 text block 的数组 content 要进索引
    line2 = user_line(uuid="u-arr")
    line2["message"]["content"] = [
        {"type": "text", "text": "粘贴的第一段"},
        {"type": "tool_result", "tool_use_id": "t", "content": "x"},
    ]
    res2 = parse_bytes(jsonl_bytes(line2))
    assert [r.text for r in res2.rows] == ["粘贴的第一段"]


def test_assistant_text_joined_thinking_dropped_usage_summed():
    res = parse_bytes(
        jsonl_bytes(
            assistant_line(texts=("第一段", "第二段"), thinking="内心戏不进索引"),
            assistant_line(texts=("再来一条",), uuid="a-0002"),
        )
    )
    assert len(res.rows) == 2
    assert res.rows[0].kind == "assistant_text"
    assert res.rows[0].text == "第一段\n\n第二段"
    assert "内心戏" not in res.rows[0].text
    assert res.in_tokens == 20 and res.out_tokens == 40
    assert res.cache_read_tokens == 60 and res.cache_write_tokens == 80


def test_control_lines_no_seq_and_last_wins():
    res = parse_bytes(
        jsonl_bytes(
            ai_title_line("旧标题"),
            user_line(),
            ai_title_line("新标题"),
            last_prompt_line("最后一问"),
            bridge_line("cse_01ABC"),
            mode_line(),
            permission_mode_line(),
        )
    )
    assert res.ai_title == "新标题"
    assert res.last_prompt == "最后一问"
    assert res.bridge_session_id == "cse_01ABC"
    assert res.seq_end == 1  # 只有 user 行带 uuid


def test_compact_boundary_sets_flag():
    res = parse_bytes(jsonl_bytes(user_line(), compact_boundary_line()))
    assert res.has_compact is True


def test_bad_line_skipped_and_counted():
    data = jsonl_bytes(user_line()) + b"{not json}\n" + jsonl_bytes(assistant_line())
    res = parse_bytes(data)
    assert res.bad_lines == 1
    assert res.msg_count == 2
    assert res.seq_end == 2


def test_oversized_line_discarded():
    huge = b'{"type":"user","uuid":"u-big","message":{"content":"' + b"x" * 5000 + b'"}}\n'
    data = jsonl_bytes(user_line()) + huge + jsonl_bytes(assistant_line())
    res = parse_bytes(data, max_line_bytes=1024)
    assert res.bad_lines == 1
    assert res.msg_count == 2  # 前后两条照常
    assert res.new_offset == len(data)


def test_oversized_line_still_being_written_not_consumed():
    prefix = jsonl_bytes(user_line())
    partial_huge = b'{"type":"user","uuid":"u-big","message":{"content":"' + b"x" * 5000
    res = parse_bytes(prefix + partial_huge, max_line_bytes=1024)
    assert res.new_offset == len(prefix)
    assert res.bad_lines == 0


def test_trailing_half_line_not_consumed_then_continued():
    full = jsonl_bytes(user_line(text="完整的一条"))
    half = json.dumps(user_line(text="正在写入的半条", uuid="u-half"), ensure_ascii=False).encode(
        "utf-8"
    )[:40]
    res1 = parse_bytes(full + half)
    assert res1.new_offset == len(full)
    assert res1.msg_count == 1
    assert res1.seq_end == 1

    # CC 写完了这行,再追加一条 → 从上次 offset/seq 续读,seq 连续
    rest = json.dumps(user_line(text="正在写入的半条", uuid="u-half"), ensure_ascii=False).encode(
        "utf-8"
    )[40:] + b"\n"
    data = full + half + rest + jsonl_bytes(assistant_line())
    res2 = parse_bytes(data, start_offset=res1.new_offset, start_seq=res1.seq_end)
    assert res2.msg_count == 2
    assert [r.seq for r in res2.rows] == [1, 2]
    assert res2.new_offset == len(data)


def test_last_ts_ignores_control_trailer():
    res = parse_bytes(
        jsonl_bytes(
            user_line(ts="2026-08-01T00:00:00.000Z"),
            mode_line(),
            permission_mode_line(),
            bridge_line(),
        )
    )
    assert res.last_ts == "2026-08-01T00:00:00.000Z"
    # 但带 timestamp 的控制行(queue-operation)算真实活动
    res2 = parse_bytes(
        jsonl_bytes(
            user_line(ts="2026-08-01T00:00:00.000Z"),
            queue_operation_line("2026-08-02T12:00:00.000Z"),
        )
    )
    assert res2.last_ts == "2026-08-02T12:00:00.000Z"
    assert res2.first_ts == "2026-08-01T00:00:00.000Z"


def test_attachment_not_indexed_but_counts_seq():
    res = parse_bytes(jsonl_bytes(user_line(), attachment_line(), assistant_line()))
    assert res.seq_end == 3
    assert len(res.rows) == 2


def test_clean_user_title():
    raw = (
        "Caveat: The messages below were generated by the user while running local"
        " commands. DO NOT respond to these messages or otherwise consider them in your"
        " response unless the user explicitly asks you to.\n"
        "<system-reminder>xx</system-reminder> 帮我看看  这个问题"
    )
    assert clean_user_title(raw) == "xx 帮我看看 这个问题"  # 标签去壳后内容保留
    assert clean_user_title("a" * 200) == "a" * 80


def test_bridge_url_prefix_swap():
    assert bridge_url("cse_01ABC") == "https://claude.ai/code/session_01ABC"
    assert bridge_url("session_01ABC") == "https://claude.ai/code/session_01ABC"
    assert bridge_url(None) is None
