# ClaudeDeck

Claude Code 本地会话管理界面:找回对话、看清状态、在 30 天自动清理前留住数据。

- **会话中心**:跨项目列表、中文全文搜索(SQLite FTS5 trigram,1-2 字短词自动回退 LIKE)、
  聊天视图预览、一键 `--resume` 拉起、导出 Markdown。
- **状态看板**:轮询 `~/.claude/sessions/<PID>.json` 的运行中窗口 busy/waiting/idle
  实时看板(不依赖终端选项卡标题,重命名随意)、后台作业、磁盘占用、token 用量曲线。
- **归档 = 手动封存**:事情做完,在详情页点「封存」才会镜像到归档目录
  (`config.json` 的 `archive_dir`,可指向任意盘),没有任何自动备份;官方
  `cleanupPeriodDays`(默认 30 天)不动,未封存的会话到期即被官方永久清理。
  封存过的会话被清理后仍可搜可看,并可一键还原回 `projects/` 重新 resume。

数据真相源永远是 `~/.claude` 下的文件;SQLite(`data/claudedeck.db`)只是可随时重建的
缓存索引;归档目录是唯一额外持久数据,程序只增不删。

## 快速开始

```
install.bat            # uv venv + 依赖(仅项目内,不污染全局)
start_claudedeck.bat   # 启动服务并打开 http://127.0.0.1:8737
```

五个页面:**会话**(列表/全文搜索/一键恢复/复制命令/provider 筛选)、**看板**(运行中窗口
busy/waiting/idle 实时灯、**5h 配额窗口与燃烧率预测**、后台作业、用量双曲线、磁盘)、
**详情**(聊天视图/深链/子 agent 抽屉/导出 MD)、**归档**(源已清理会话的浏览与还原)、
**设置**(配置/重建索引/项目 purge)。顶栏通告牌灯条在每一页都亮着。

多 provider:除 Claude Code 外自动索引 **Codex CLI**(`~/.codex/sessions` rollout,
存在即扫,浏览/搜索/详情/`codex resume` 一键拉起;归档与实时看板仍为 Claude 专属)。
配额窗口语义对齐 ccusage blocks(整点锚定 5h 窗),参照物是你自己历史窗口的最大用量,
不冒充官方配额。

命令行:

```
.venv\Scripts\python.exe -m app.cli scan          # 全量/增量索引(不产生归档)
.venv\Scripts\python.exe -m app.cli search 托卡马克
.venv\Scripts\python.exe -m app.cli title <session_id>
```

安全边界:服务只绑 127.0.0.1;运行中的会话禁止 resume(CC 并发检测会让新实例
直接退出);破坏性操作(还原/purge)需确认,purge 前强制归档并复用官方
`claude project purge`,绝不自行 rm;本工具唯一会写的用户数据是 resume 前对
`~/.claude.json` 单键的信任预写(自动备份到 `~/.claude/backups/`)。

## 环境

Windows 11 / Python 3.14(uv 管理)/ Claude Code 2.1.22x 的数据布局
(`ai-title` 控制行、`sessions/<PID>.json` 注册表含 busy/idle/waiting 三态、
`cse_↔session_` 网页映射、FILETIME procStart 验活等,数据事实附录见
`~/.claude/plans/ultrathink-claudecode-glittery-minsky.md`)。
