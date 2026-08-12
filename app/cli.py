"""命令行入口(S1 验收工具):

  python -m app.cli scan [--force]     全量/增量索引 + 归档快照
  python -m app.cli search <词>        全文搜索(FTS 或短词 LIKE 回退)
  python -m app.cli title <sid>        查看某会话的标题与元数据(调试)
"""
from __future__ import annotations

import argparse
import sys

from . import db as db_mod
from .config import Config
from .indexer import Indexer
from .search import MARK_L, MARK_R, search


def _fmt_bytes(n: int | None) -> str:
    n = n or 0
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n}B"


def cmd_scan(cfg: Config, force: bool) -> int:
    con = db_mod.connect(cfg.db_path)
    idx = Indexer(cfg, con)
    print(f"扫描 {cfg.projects_root}")
    print(f"归档 → {cfg.archive_projects_root}")

    def progress(done: int, total: int, name: str) -> None:
        if done % 10 == 0 or done == total:
            print(f"  [{done}/{total}] {name}")

    stats = idx.scan_once(force=force, progress_cb=progress)
    print(
        f"完成: 解析 {stats['files_parsed']} / 跳过 {stats['files_skipped']}"
        f" / 共 {stats['files_seen']} 个文件, 新索引文本 {stats['rows_indexed']} 条,"
        f" 坏行 {stats['bad_lines']}, 归档拷贝 {stats['archived_copies']},"
        f" 仅归档会话 {stats['archive_only_sessions']},"
        f" history 新行 {stats['history_new_lines']}, 耗时 {stats['elapsed_s']}s"
    )
    if stats["file_errors"]:
        print(f"文件级错误 {stats['file_errors']} 个:")
        for e in stats["errors"]:
            print(f"  ! {e}")
    n_sessions = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    n_msgs = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    print(f"库内: {n_sessions} 个会话, {n_msgs} 条可搜文本")
    return 0


def cmd_search(cfg: Config, query: str, limit: int) -> int:
    con = db_mod.connect(cfg.db_path)
    res = search(con, query, limit=limit)
    if res["fallback"]:
        print("(短词回退 LIKE 模糊匹配,较慢)")
    if not res["groups"]:
        print("无命中")
        return 1
    for g in res["groups"]:
        s = g["session"]
        title = s.get("title") or "(无标题)"
        print(f"\n◆ {title}")
        print(
            f"  {s.get('session_id')}  {s.get('cwd') or '?'}  "
            f"末活跃 {s.get('last_ts') or '?'}  {_fmt_bytes(s.get('file_size'))}"
            f"{'  [源已清理]' if s.get('source_missing') else ''}"
        )
        for h in g["hits"]:
            snip = h["snippet"].replace(MARK_L, "【").replace(MARK_R, "】")
            snip = snip.replace("\n", " ")
            print(f"    seq={h['seq']:>5} {h['kind']:<15} {snip}")
    print(f"\n共 {res['total_hits']} 条命中 / {len(res['groups'])} 个会话")
    return 0


def cmd_title(cfg: Config, sid: str) -> int:
    con = db_mod.connect(cfg.db_path)
    row = con.execute("SELECT * FROM sessions WHERE session_id=?", (sid.lower(),)).fetchone()
    if row is None:
        print(f"库中没有会话 {sid}(先跑 scan?)")
        return 1
    for k in row.keys():
        print(f"{k:>20}: {row[k]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # win-env: 中文输出纪律
    ap = argparse.ArgumentParser(prog="claudedeck")
    ap.add_argument("--config", default=None, help="config.json 路径(默认项目根)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_scan = sub.add_parser("scan", help="扫描并索引 + 归档快照")
    p_scan.add_argument("--force", action="store_true", help="无视增量状态全量重扫")
    p_search = sub.add_parser("search", help="全文搜索")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=200)
    p_title = sub.add_parser("title", help="查看会话元数据")
    p_title.add_argument("sid")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    if args.cmd == "scan":
        return cmd_scan(cfg, args.force)
    if args.cmd == "search":
        return cmd_search(cfg, args.query, args.limit)
    if args.cmd == "title":
        return cmd_title(cfg, args.sid)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
