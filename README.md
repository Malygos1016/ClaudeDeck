# ClaudeDeck

**A local, Windows-first session manager for Claude Code (and Codex / pi / Crush): full-text search across all your conversations, a live status board, and automatic archiving before the 30-day cleanup deletes your history.**

ClaudeDeck 是一个跑在本机的 Claude Code 会话管理界面。它解决三件官方没管的事:

1. **找回对话**——"三个月前那段讨论在哪个会话里?" 全部历史会话进 SQLite FTS5 全文索引(对中文检索做了专门处理),命中直接跳到那一轮对话。
2. **看清窗口**——开了八个终端,哪个在忙、哪个在等你输入?看板用 busy/waiting/idle 三色灯实时显示,不用一个个切窗口。
3. **数据兜底**——Claude Code 默认 **30 天后自动删除**会话记录(`cleanupPeriodDays`)。ClaudeDeck 提供一键封存到独立归档区,被清理后仍可搜索、阅读、还原、重新 resume。据我们对同类开源工具的调研,这是目前唯一做了清理兜底的项目。

界面语言为中文。仅监听 `127.0.0.1`,无外网面,数据不出机器。

## 功能一览

| 页面 | 内容 |
|---|---|
| 会话 | 列表/中文全文搜索/按项目与 provider 筛选/一键 resume 到新终端/复制 resume 命令 |
| 看板 | 运行中窗口三色灯(含"等待你输入"状态)、**5h 配额窗口与燃烧率预测**、后台驻留 agent 识别、用量与成本曲线、磁盘占用 |
| 详情 | 聊天视图(搜索命中深链到具体轮次)、子 agent 抽屉、导出 Markdown |
| 归档 | 源已被官方清理的会话:浏览、搜索、一键还原回 projects |
| 设置 | 配置修改、重建索引、项目级 purge(走官方 `claude project purge`,绝不自行删文件) |

另有:系统托盘常驻(无控制台窗口)、开机自启(可选)、顶栏窗口状态灯条(每页可见)。

### 多 provider

| Provider | 数据来源 | 浏览/搜索/详情 | 一键恢复 |
|---|---|---|---|
| Claude Code | `~/.claude/projects` | ✅ | `claude --resume`(新终端标签) |
| Codex CLI | `~/.codex/sessions` rollout | ✅ | `codex resume <id>` |
| pi agent | `~/.pi/agent/sessions` | ✅ | `pi --session <id>` |
| Crush | 各项目 `.crush/crush.db`(只读) | ✅ | 打开 TUI 后在列表选择(其无命令行级 resume) |

对应目录存在即自动纳入索引,未安装的机器零成本。归档、实时看板、配额窗口为 Claude 专属(其余工具没有对应机制)。

## 安装

要求:Windows 10/11,[uv](https://docs.astral.sh/uv/),Python 3.14(uv 自动装),Claude Code CLI。

```bat
git clone https://github.com/Malygos1016/ClaudeDeck.git
cd ClaudeDeck
start_claudedeck.bat   :: 首跑自动装环境(项目内 venv),之后无窗启动,右下角出现琥珀托盘图标
```

浏览器打开 **http://127.0.0.1:8737**(双击托盘图标同效)。首跑会出现一个安装进度窗口,装完自动转入无窗托盘模式;首次索引全量扫描,视历史体量约数秒到一两分钟。(`install.bat` 仍保留,想单独装环境时用。)

可选开机自启:

```powershell
.\autostart.ps1 -Enable    # 登录时静默拉起托盘;-Disable 移除;-Status 查看
```

完整的页面讲解、典型工作流与故障排查见 **[docs/USAGE.md](docs/USAGE.md)**。

## 安全边界

- 服务只绑 `127.0.0.1`,无鉴权因此也绝不该暴露到外网。
- 对 Claude/Codex/pi/Crush 的数据目录**只读**;唯一的写入是三处自有数据:本工具的 SQLite 索引(纯缓存,可随时重建)、归档目录、`config.json`。
- 例外一处:resume 前对 `~/.claude.json` 的项目信任键做单键预写(免去新窗口的信任弹窗),写前自动备份到 `~/.claude/backups/`,任何异常放弃写入。
- 删除类操作只有项目 purge,要求输入项目名确认,先强制归档全部会话,再调用官方 `claude project purge --yes`——本工具自身从不删除用户数据文件。
- 运行中的会话禁止再 resume(Claude Code 并发检测会让新实例直接退出,界面直接拦截)。

## 数据事实(实测口径)

基于 Claude Code CLI 2.1.22x、codex 0.144、pi format v3、crush 0.13x 的本机实测:标题取 `ai-title` 控制行;末次活跃取最后一条带时间戳的行(文件 mtime 会被后台扫描刷新,不可信);运行状态来自 `~/.claude/sessions/<PID>.json` 注册表(busy/idle/waiting 三态),以"进程号+进程创建时间"双因子防 PID 复用误判;配额窗口语义对齐 ccusage blocks(整点锚定 5h),参照物是**你自己历史窗口的最大用量**,不是官方配额。这些格式随上游版本可能变化,解析器按"坏行跳过并计数"的纪律设计,不会因个别格式漂移整体罢工。

## License

MIT
