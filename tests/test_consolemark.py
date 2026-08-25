"""控制台身份链与标题标记(app/consolemark.py)。

只测能安全测的部分:
- marker_for 是纯函数,直接断言。
- console_host_of / host_kind 是只读查询,对真实 pid(自己 + 一个子进程)调用,
  只断言"不抛异常、返回形状对",不断言具体宿主类型 —— 那由运行本测试的
  终端形态决定(mintty/cmd/CI 容器都不一样),断言死了就是测试环境本身,不是代码。
- mark_console/restore_console 的 base64 编解码协议用 monkeypatch 顶掉
  _run_helper 来验,不让它真的起子进程 AttachConsole —— 那会动真格改
  目标 pid 的控制台标题,是要人工验收的副作用。
- _helper_main 只测参数个数校验分支:这条分支在 AttachConsole 之前就返回,
  是唯一能在不碰真实控制台的前提下直接调用的路径;参数个数对但 pid/mode
  不对的分支会先 FreeConsole() 本测试进程自己的控制台,不安全,不测。
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys

import pytest

from app import consolemark
from app.consolemark import _helper_main, console_host_of, host_kind, marker_for

# ---------- marker_for ----------


def test_marker_for_format():
    assert marker_for(4242) == "[CD#4242]"


def test_marker_for_is_unique_per_pid():
    assert marker_for(1) != marker_for(2)


def test_marker_for_is_pure_ascii():
    """docstring 承诺纯 ASCII,避开字体与编码变数 —— 非 ASCII 字符在某些字体
    下缺字形,UIA 按标记子串查找会连着标记本身一起找不到。"""
    assert marker_for(999).isascii()


# ---------- console_host_of ----------


def test_console_host_of_self_pid_shape():
    """对本测试进程自己查询:不管有没有控制台,只要求不抛异常、返回类型对。"""
    result = console_host_of(os.getpid())
    assert result is None or isinstance(result, int)


def test_console_host_of_child_process_shape():
    """子进程不带 CREATE_NO_WINDOW,继承本进程的控制台;只验证查询本身
    不抛异常 —— 是否真查得到宿主因运行环境(pytest 从 mintty/cmd/CI 启动
    时控制台形态不同)而异,这里不断言具体值。"""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        result = console_host_of(proc.pid)
        assert result is None or isinstance(result, int)
    finally:
        proc.kill()
        proc.wait(timeout=5)


# ---------- host_kind ----------


def test_host_kind_self_pid_shape():
    kind, hwnd = host_kind(os.getpid())
    assert isinstance(kind, str)
    assert isinstance(hwnd, int)
    assert kind in {"wt-tab", "own-window", "headless"}


# ---------- mark_console / restore_console:base64 协议(monkeypatch,零真实 attach) ----------


def test_mark_console_decodes_ok_response(monkeypatch):
    """mark_console 老实解码 helper 回传的 base64 原标题,不自己再加工。"""
    old_title = "旧标题 with emoji 🎯"
    payload = "OK " + base64.b64encode(old_title.encode("utf-8")).decode("ascii")
    monkeypatch.setattr(consolemark, "_run_helper", lambda *a: payload)
    assert consolemark.mark_console(123) == old_title


def test_mark_console_handles_empty_old_title(monkeypatch):
    """原标题是空串时 base64 段也是空的,不能因为"空"被误判成失败。"""
    captured: dict = {}

    def fake_run_helper(*args):
        captured["args"] = args
        return "OK "

    monkeypatch.setattr(consolemark, "_run_helper", fake_run_helper)
    assert consolemark.mark_console(123) == ""
    assert captured["args"] == ("mark", "123", marker_for(123))


def test_mark_console_returns_none_when_helper_unreachable(monkeypatch):
    """helper 子进程超时/崩溃(_run_helper 返回 None)时如实传递失败,不能编造标题。"""
    monkeypatch.setattr(consolemark, "_run_helper", lambda *a: None)
    assert consolemark.mark_console(123) is None


def test_mark_console_returns_none_on_err_response(monkeypatch):
    """AttachConsole 失败(权限/竞态)helper 回 'ERR <code>',mark_console 必须传递为 None
    让调用方降级,不能把 'ERR 5' 误当成合法标题返回。"""
    monkeypatch.setattr(consolemark, "_run_helper", lambda *a: "ERR 5")
    assert consolemark.mark_console(123) is None


def test_restore_console_encodes_title_and_reports_success(monkeypatch):
    """restore_console 必须把标题原样 base64 编码后传给 helper,任意字符不破坏协议。"""
    captured: dict = {}

    def fake_run_helper(*args):
        captured["args"] = args
        return "OK"

    monkeypatch.setattr(consolemark, "_run_helper", fake_run_helper)
    title = '带引号"与换行\n和emoji🎉的标题'
    assert consolemark.restore_console(123, title) is True
    mode, pid_s, b64 = captured["args"]
    assert mode == "restore"
    assert pid_s == "123"
    assert base64.b64decode(b64).decode("utf-8") == title


def test_restore_console_returns_false_on_err(monkeypatch):
    """尽力而为:恢复失败只返回 False,标签上残留标记,不抛异常打断调用方的 finally。"""
    monkeypatch.setattr(consolemark, "_run_helper", lambda *a: "ERR 5")
    assert consolemark.restore_console(123, "x") is False


def test_restore_console_returns_false_when_helper_unreachable(monkeypatch):
    monkeypatch.setattr(consolemark, "_run_helper", lambda *a: None)
    assert consolemark.restore_console(123, "x") is False


# ---------- _helper_main:只测参数校验分支 ----------


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["mark"],
        ["mark", "123"],
        ["mark", "123", "marker", "extra"],
    ],
    ids=["0-args", "1-arg", "2-args", "4-args"],
)
def test_helper_main_rejects_wrong_arg_count(argv, capsys):
    """参数个数不对时在 FreeConsole/AttachConsole 之前就返回 —— 这是唯一不会
    动到本测试进程自己控制台的分支,返回码 2、stdout 打印 ERR。"""
    assert _helper_main(argv) == 2
    out = capsys.readouterr().out
    assert out.startswith("ERR")
