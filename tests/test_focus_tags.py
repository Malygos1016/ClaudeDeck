"""聚焦匹配(纯逻辑)与会话打标。GUI 动作本身人工验收。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.focus import match_tab, strip_glyph
from app.main import create_app
from app.tags import load_tags, set_tag


def test_strip_glyph_real_samples():
    # 2026-08-20 本机 WT 实测样本
    assert strip_glyph("✳ 设计非代码能力培养平台架构") == "设计非代码能力培养平台架构"
    assert strip_glyph("◑ claudedeck-local-session-manager") == "claudedeck-local-session-manager"
    assert strip_glyph("✳ 46953-e6") == "46953-e6"
    assert strip_glyph("") == ""


def test_strip_glyph_admin_prefix():
    """管理员权限运行的终端,标签标题会多一截前缀,而且中英文两种都会出现。

    2026-08-23 本机同时抓到:
      'Administrator: ◐ Claudedeck本地安裝 ⑂ 我現在fork了…'
      '管理员: ATTACH-TEST'
    旧实现只剥开头的装饰符号,这两条开头都是文字,一个字符都剥不掉,
    于是点击聚焦必然 404 —— 用户实报的「点击跳转完全失效」就有这一份。
    """
    assert strip_glyph("Administrator: ◐ Claudedeck本地安裝") == "Claudedeck本地安裝"
    assert strip_glyph("管理员: ATTACH-TEST") == "ATTACH-TEST"
    assert strip_glyph("管理員: ✳ 大書庫") == "大書庫"
    assert strip_glyph("Administrator: ✳ 46953-e6") == "46953-e6"
    # 不能误伤正常标题里本来就有的冒号
    assert strip_glyph("✳ TODO: 修好色带") == "TODO: 修好色带"


def test_match_tab_with_admin_prefix():
    cands = ["Claudedeck本地安裝 ⑂ 我現在fork了", "46953-77"]
    assert match_tab("Administrator: ◐ Claudedeck本地安裝 ⑂ 我現在fork了", cands)
    assert match_tab("管理员: ◐ 46953-77", cands)


def test_match_tab():
    cands = ["46953-e6", "设计非代码能力培养平台架构"]
    assert match_tab("✳ 46953-e6", cands)
    assert match_tab("◑ 设计非代码能力培养平台架构", cands)
    assert not match_tab("✳ 别的窗口", cands)
    # 截断省略号
    assert match_tab("✳ 设计非代码能力培养平…", cands)
    assert not match_tab("✳ ", cands)
    assert not match_tab("✳ 46953", cands)  # 前缀不足以匹配(只认截断形)


def test_tags_roundtrip(cfg):
    assert load_tags(cfg) == {}
    set_tag(cfg, "AAAA0000-0000-0000-0000-000000000000", "流量计选型")
    assert load_tags(cfg)["aaaa0000-0000-0000-0000-000000000000"] == "流量计选型"
    set_tag(cfg, "aaaa0000-0000-0000-0000-000000000000", "")  # 清除
    assert load_tags(cfg) == {}


def test_tag_api_and_list_merge(cfg):
    from tests.factory import SID, ai_title_line, user_line, write_jsonl

    write_jsonl(
        cfg.projects_root / "C--Work-proj" / f"{SID}.jsonl",
        user_line(text="打标测试", ts="2026-08-19T00:00:00.000Z"),
        ai_title_line("打标会话"),
    )
    app = create_app(cfg)
    with TestClient(app) as c:
        app.state.indexer.scan_once()
        r = c.put(f"/api/sessions/{SID}/tag", json={"tag": "  核聚变采购  "})
        assert r.json() == {"ok": True, "tag": "核聚变采购"}
        items = c.get("/api/sessions").json()["items"]
        me = next(x for x in items if x["session_id"] == SID)
        assert me["tag"] == "核聚变采购"
        det = c.get(f"/api/sessions/{SID}").json()["session"]
        assert det["tag"] == "核聚变采购"
        # 清除
        c.put(f"/api/sessions/{SID}/tag", json={"tag": None})
        assert load_tags(cfg) == {}
        # 不存在的会话 404
        assert c.put("/api/sessions/ffffffff-1111-2222-3333-444444444444/tag",
                     json={"tag": "x"}).status_code == 404
