# ClaudeDeck

Claude Code 本地会话管理界面:找回对话、看清状态、在 30 天自动清理前留住数据。

- **会话中心**:跨项目列表、中文全文搜索(SQLite FTS5 trigram,1-2 字短词自动回退 LIKE)、
  聊天视图预览、一键 `--resume` 拉起、导出 Markdown、claude.ai 网页链接。
- **状态看板**:轮询 `~/.claude/sessions/<PID>.json` 的运行中窗口 busy/idle 实时看板
  (不依赖终端选项卡标题,重命名随意)、后台作业、磁盘占用、token 用量曲线。
- **归档**:官方 `cleanupPeriodDays`(默认 30 天)不动;索引器每轮扫描把安静期
  transcript 镜像到独立归档目录(`config.json` 的 `archive_dir`,可指向任意盘)。
  已清理的会话仍可搜可看,并可一键还原回 `projects/` 重新 resume。

数据真相源永远是 `~/.claude` 下的文件;SQLite(`data/claudedeck.db`)只是可随时重建的
缓存索引;归档目录是唯一额外持久数据,程序只增不删。

## 快速开始

```
install.bat            # uv venv + 依赖(仅项目内,不污染全局)
start_claudedeck.bat   # 启动服务并打开 http://127.0.0.1:8737 (S2 起可用)
```

命令行(S1 起可用):

```
.venv\Scripts\python.exe -m app.cli scan          # 全量/增量索引 + 归档快照
.venv\Scripts\python.exe -m app.cli search 托卡马克
.venv\Scripts\python.exe -m app.cli title <session_id>
```

服务只绑 127.0.0.1,无外网面;破坏性操作(还原/purge)需确认,删除复用官方
`claude project purge`,绝不自行 rm。

## 环境

Windows 11 / Python 3.14(uv 管理)/ Claude Code 2.1.22x 的数据布局
(`ai-title` 控制行、`sessions/<PID>.json` 注册表、`cse_↔session_` 网页映射等,
详见 `IMPLEMENTATION_PLAN.md` 引用的计划文件附录)。
