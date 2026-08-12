"""测试用 transcript 行构造器。字段形状照抄本机 2.1.228 实测样本(内容脱敏)。"""
from __future__ import annotations

import json
from pathlib import Path

SID = "11111111-2222-3333-4444-555555555555"
CWD = r"C:\Work\proj"


def _j(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False)


def user_line(
    text: str = "你好",
    ts: str = "2026-08-11T06:14:06.622Z",
    uuid: str = "u-0001",
    sid: str = SID,
    cwd: str = CWD,
    **over,
) -> dict:
    d = {
        "parentUuid": None,
        "isSidechain": False,
        "promptId": "p-0001",
        "type": "user",
        "message": {"role": "user", "content": text},
        "uuid": uuid,
        "timestamp": ts,
        "permissionMode": "default",
        "userType": "external",
        "entrypoint": "cli",
        "cwd": cwd,
        "sessionId": sid,
        "version": "2.1.228",
        "gitBranch": "HEAD",
        "slug": "test-slug",
    }
    d.update(over)
    return d


def tool_result_user_line(uuid: str = "u-0002", sid: str = SID, **over) -> dict:
    d = user_line(uuid=uuid, sid=sid, **over)
    d["message"]["content"] = [
        {"type": "tool_result", "tool_use_id": "toolu_x", "content": "raw tool output"}
    ]
    d["toolUseResult"] = {"stdout": "raw tool output"}
    return d


def assistant_line(
    texts: tuple[str, ...] = ("回答正文",),
    ts: str = "2026-08-11T06:15:00.000Z",
    uuid: str = "a-0001",
    sid: str = SID,
    thinking: str | None = None,
    usage: dict | None = None,
    **over,
) -> dict:
    content: list[dict] = []
    if thinking is not None:
        content.append({"type": "thinking", "thinking": thinking, "signature": "SIG" * 100})
    for t in texts:
        content.append({"type": "text", "text": t})
    content.append({"type": "tool_use", "id": "toolu_x", "name": "Read", "input": {"f": "x"}})
    if usage is None:
        usage = {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 40,
        }
    d = {
        "parentUuid": "u-0001",
        "isSidechain": False,
        "type": "assistant",
        "message": {
            "model": "claude-fable-5",
            "id": "msg_x",
            "type": "message",
            "role": "assistant",
            "content": content,
            "usage": usage,
        },
        "requestId": "req_x",
        "uuid": uuid,
        "timestamp": ts,
        "userType": "external",
        "entrypoint": "cli",
        "cwd": CWD,
        "sessionId": sid,
        "version": "2.1.228",
        "gitBranch": "HEAD",
        "slug": "test-slug",
    }
    d.update(over)
    return d


def system_line(subtype: str = "turn_duration", uuid: str = "s-0001", sid: str = SID, **over) -> dict:
    d = {
        "parentUuid": "a-0001",
        "isSidechain": False,
        "type": "system",
        "subtype": subtype,
        "content": "x",
        "level": "info",
        "uuid": uuid,
        "timestamp": "2026-08-11T06:16:00.000Z",
        "userType": "external",
        "entrypoint": "cli",
        "cwd": CWD,
        "sessionId": sid,
        "version": "2.1.228",
        "gitBranch": "HEAD",
    }
    d.update(over)
    return d


def compact_boundary_line(sid: str = SID) -> dict:
    return system_line(
        subtype="compact_boundary",
        uuid="s-compact",
        sid=sid,
        parentUuid=None,
        logicalParentUuid="a-0001",
        compactMetadata={"trigger": "auto", "preTokens": 1000, "postTokens": 100},
    )


def compact_summary_line(sid: str = SID) -> dict:
    d = user_line(
        text="This session is being continued from a previous conversation..." + "汇总" * 50,
        uuid="u-compact",
        sid=sid,
    )
    d["isCompactSummary"] = True
    d["isVisibleInTranscriptOnly"] = True
    return d


def attachment_line(atype: str = "task_reminder", uuid: str = "at-0001", sid: str = SID) -> dict:
    return {
        "parentUuid": "u-0001",
        "isSidechain": False,
        "attachment": {"type": atype},
        "type": "attachment",
        "uuid": uuid,
        "timestamp": "2026-08-11T06:16:30.000Z",
        "cwd": CWD,
        "sessionId": sid,
        "version": "2.1.228",
        "slug": "test-slug",
    }


# ---- 控制行(无 uuid / 无 timestamp) ----

def ai_title_line(title: str, sid: str = SID) -> dict:
    return {"type": "ai-title", "aiTitle": title, "sessionId": sid}


def last_prompt_line(prompt: str, sid: str = SID) -> dict:
    return {"type": "last-prompt", "lastPrompt": prompt, "leafUuid": "u-0001", "sessionId": sid}


def bridge_line(bid: str = "cse_01TESTULID000000000000", sid: str = SID) -> dict:
    return {
        "type": "bridge-session",
        "sessionId": sid,
        "bridgeSessionId": bid,
        "lastSequenceNum": 0,
        "ownerAccountUuid": "acc",
        "ownerOrganizationUuid": "org",
    }


def mode_line(sid: str = SID) -> dict:
    return {"type": "mode", "mode": "normal", "sessionId": sid}


def permission_mode_line(sid: str = SID) -> dict:
    return {"type": "permission-mode", "permissionMode": "default", "sessionId": sid}


def queue_operation_line(ts: str, sid: str = SID) -> dict:
    return {
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": ts,
        "sessionId": sid,
        "content": "queued",
    }


def file_history_snapshot_line(sid: str = SID) -> dict:
    return {
        "type": "file-history-snapshot",
        "messageId": "u-0001",
        "snapshot": {"messageId": "u-0001", "trackedFileBackups": {}},
        "isSnapshotUpdate": False,
    }


# ---- 文件写入 ----

def jsonl_bytes(*objs: dict) -> bytes:
    return "".join(_j(o) + "\n" for o in objs).encode("utf-8")


def write_jsonl(path: Path, *objs: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(jsonl_bytes(*objs))
    return path


def append_jsonl(path: Path, *objs: dict) -> None:
    with open(path, "ab") as f:
        f.write(jsonl_bytes(*objs))
