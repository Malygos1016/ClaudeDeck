# ClaudeDeck 开发日志(续作指南)

> 写给下一次会话的 Claude(以及未来的自己)。目标:读完本文即可在零上下文的情况下
> 继续扩展功能。最后更新:2026-08-21,HEAD=0ac9368。

## 0. 快速上手(续作先读这节)

- 代码:`C:\CoreWork\ClaudeDeck`(开发机)。GitHub:`Malygos1016/ClaudeDeck`(public,MIT)。
- 启动:双击 `start_claudedeck.bat`(首跑自动建 venv;之后无窗托盘)。服务 `http://127.0.0.1:8737`。
- 跑测试:`.venv\Scripts\python.exe -m pytest -q`(82 项,秒级)。
- 改了 Python → 重启托盘(杀 `pythonw -m app.tray` 进程再跑 bat);改了 web/ 静态文件 → 刷新页面即可(服务端已带 no-cache)。
- 改了 topbar → 单独重启:杀 `pythonw -m app.topbar` 进程,再 `Start-Process .venv\Scripts\pythonw.exe -ArgumentList '-m','app.topbar' -WorkingDirectory <项目根> -WindowStyle Hidden`。
- **验收 topbar 不要用 GDI 截屏**(拍不到,见 §8),用 PrintWindow 取帧,脚本模板在 §8。
- 用户(宁宁)的沟通偏好:完整自然语言,不要缩写记号;征求评价要给真实强弱对比;产品直觉极好,UI/动效细节会逐项验收,照做即可。

## 1. 项目定位与现状

Claude Code 本地会话管理器,解决三痛点:找回对话(中文全文搜索+轮次深链)、
看清窗口(busy/waiting/idle 实时灯)、数据兜底(官方 30 天清理前的封存/还原)。
竞品调研结论(2026-08-13,实时核实过星数):全生态四个独有点=清理兜底/轮次跳转/
中文 FTS5/Windows 状态托盘三件套;最直接竞品 jhlee0409/claude-code-history-viewer。

当前功能全景:
- 五个网页:会话(搜索/恢复/复制命令/provider 筛选/tag 徽章)、看板(实时灯+配额窗口
  +后台作业+用量曲线+磁盘+plans)、详情(聊天视图/深链/子agent/导出MD)、归档、设置(purge等)。
- 多 provider:claude / codex / pi / crush(只读,目录存在即自动索引)。
- 5h 配额窗口:ccusage 语义,参照自己历史峰值,燃烧率+触顶外推。
- 点击聚焦:网页灯条/看板卡/顶栏格子 → 对应 Windows Terminal 标签置前。
- 会话打标:tags.json,网页就地编辑(hover 铅笔)+顶栏右键菜单,四处联动显示。
- 系统托盘 + CCTopBar 桌面顶栏(WebView2 壳,GLSL 动效,详见 §7)。
- 开机自启(autostart.ps1,计划任务)。

## 2. 架构总览(文件地图)

    app/
      config.py      dataclass 配置,config.json 原子读写;utf-8-sig 容 BOM;
                     字段: port/claude_home/archive_dir/data_dir/scan_interval_seconds/
                     archive_quiet_minutes/live_poll_ms/claude_exe/index_thinking/
                     index_tool_results/codex_home/pi_home/crush_projects_json/topbar_enabled
      db.py          SQLite 纯缓存(损坏/版本不符→删库重建,SCHEMA_VERSION="3")。
                     表: sessions(含 provider 列)/messages/messages_fts(trigram)/files/
                     usage_daily/usage_hourly/subagents/meta
      transcript.py  Claude JSONL 流式解析;iter_jsonl 行框架(半行不消费/超长行丢弃)
                     供各 provider 复用;usage 按日+按小时双桶
      codex.py       Codex rollout 解析(user=event_msg 免注入噪音,assistant=response_item,
                     usage=last_token_usage 无状态;seq 规则与视图解析器严格一致)
      pi.py          pi agent JSONL(session 头行有原样 cwd;usage 字段全映射)
      crush.py       crush.db 只读 SQLite(projects.json 名册发现;时间戳秒/毫秒自适应;
                     $$call 子会话一并索引)
      scanner.py     枚举各 provider 文件;munge_path 是 CC 目录名编码(不可逆)
      indexer.py     唯一常态写库者;增量按 (mtime,size,parsed_offset);
                     crush 走虚拟路径 <db>::<sid> 记账;archive/history 摄取;PARSERS 表
      quota.py       5h 窗口切分(整点锚定)/燃烧率/历史最大窗外推;只算 claude
      live.py        sessions/<PID>.json 注册表读取;pid+procStart(FILETIME) 双因子验活;
                     全体失配→「泛状态追踪模式」降级并打 degraded 标
      focus.py       UIA 找 WT 标签(剥状态符号后与 name/title 双候选匹配,含截断省略号),
                     敲 Alt 绕前台锁;WT 窗口类 CASCADIA_HOSTING_WINDOW_CLASS
      tags.py        会话标签 data/tags.json(用户数据,不进可重建 DB;mtime 缓存)
      launcher.py    resume 拉起(wt 新标签/无 wt 回退);trust 单键预写+备份;
                     环境净化(剥 NO_COLOR/CLAUDECODE/CLAUDE_CODE_*);多 provider 命令
      archive.py     封存/还原(手动触发,扫描循环不自动镜像——用户拍板)
      render.py      claude 聊天视图解析(带缓存)+窗口分页+导出 MD
      stats.py       磁盘/用量曲线/token_log 成本/plans(10 分钟进程内缓存)
      main.py        FastAPI 工厂;非 /api 响应统一 Cache-Control: no-cache(重要!)
      serve.py       控制台入口(python -m app.serve,调试用)
      tray.py        托盘常驻(pythonw);端口绑定确认后才驻留;CCTopBar 子进程管理
                     (WM_CLOSE 优雅关→terminate 兜底);菜单含 CCTopBar 开关
      topbar.py      CCTopBar 原生壳,详见 §7
      routes/        sessions(列表/搜索/详情/messages/export/command)、actions(resume/
                     archive/restore/purge/tag/config/rebuild)、liveboard(live/focus/jobs/
                     quota/stats/plans)、indexctl(status/scan/healthz)
    web/
      index/live/session/archive/settings.html + assets/*.js + app.css(玻璃设计系统)
      topbar.html    CCTopBar 页面本体(shader+格子+全部动效逻辑)
      topbar_menu.html  顶栏右键菜单的独立小窗页面
    tests/           82 项;conftest 的 cfg fixture 隔离所有 provider 目录(勿删!)
    docs/USAGE.md    用户教程;README.md 开源门面

## 3. 数据事实(各 provider 磁盘格式,全部本机实测)

**Claude Code (CLI 2.1.22x)**
- `~/.claude/projects/<munged-cwd>/<sid>.jsonl`:带 uuid 行=消息,无 uuid=控制行。
  标题=`ai-title` 控制行(无 type=summary);末活跃=末条带 timestamp 行(mtime 不可信,
  实测可差 4.7 天);`bridge-session` 行 cse_<ULID> ↔ claude.ai/code/session_<ULID>。
- `~/.claude/sessions/<PID>.json` 运行注册表:status 三态 busy/idle/**waiting**,
  kind=interactive|bg,procStart=FILETIME(epoch秒=ticks/1e7−11644473600)。
  bg=daemon 驻留(fork ⑂),无窗口,父进程是 `claude.exe --bg-pty-host \\.\pipe\cc-daemon-*`。
- 运行中的会话再 resume 会被并发检测杀新实例(服务端已拦截)。
- `history.jsonl` 全局输入历史→为已消失会话合成最小条目。
- 官方删除:`claude project purge <path> --yes`;后台 agent 管理:`claude agents`。
- 大会话 resume 弹「摘要/完整」选择,按 2 回车,无自动化开关。

**Codex (0.144)** `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`
- 行={timestamp,type,payload}。session_meta(id/cwd/cli_version/git.branch);
  user 取 event_msg.user_message(response_item 的 user 混 <user_instructions> 噪音);
  assistant 取 response_item(event_msg.agent_message 是重复);
  usage=token_count.info.last_token_usage(增量,无状态),in=input−cached。
- resume:`codex resume <sid>`。

**pi (format v3)** `~/.pi/agent/sessions/<munged-cwd>/<ISO>_<uuid>.jsonl`
- 头行 type=session 带**原样 cwd**(目录名的 munge 保留中文但不用管);
  message.role=user/assistant/toolResult;usage={input,output,cacheRead,cacheWrite}。
- resume:`pi --session <uuid>`(cd 到项目目录后按 id 解析;还有 --fork)。

**Crush (0.13x)** 每项目 `.crush/crush.db`(SQLite),名册 `%LOCALAPPDATA%\crush\projects.json`
- sessions 表自带 title 和 prompt/completion tokens;created/updated **实测是秒**
  (schema 注释写 ms 是错的,代码按量级自适应);messages.parts JSON 数组,text 在
  data.text;id 带 `$$call_` 后缀=工具派生子会话(一并索引)。
- 无命令行级 resume:拉起=cd 项目目录开 TUI 自选(按钮有提示);聊天视图 source=index
  (库型无文件可重读,从自家 messages 表构建)。

## 4-6. 服务端/网页/托盘要点(精简,细节看代码注释)

- FTS5 trigram 中文全文;1-2 字自动回退 LIKE。搜索命中带 seq 深链到详情页轮次。
- 详情页 seq 编号规则:claude=所有带 uuid 行序号;codex/pi=实际入索引的 user/assistant
  行序号——**索引器与视图解析器必须同规则**,否则深链错位(有 seq-parity 测试)。
- 归档=手动封存(唯一备份途径);purge 前强制全量封存;还原后官方清理器下次启动仍会跑。
- trust 预写:只改 ~/.claude.json 的单键,备份到 ~/.claude/backups/,异常放弃。
- 环境净化:从 CC 工具 shell 启动服务时 NO_COLOR/CLAUDECODE/CLAUDE_CODE_* 会穿透到
  被拉起的新 claude(白字+自认嵌套),launcher Popen 前剥离。
- 玻璃设计系统:app.css 顶部 --glass 体系;`[hidden]:not(.toast){display:none!important}`
  兜底(display 类会顶掉 hidden 属性,.toast 靠 hidden 做淡出故豁免)。
- 标签就地编辑器:body 级浮层(免疫容器 2s 轮询重渲染),hover 铅笔展开。
- 托盘:单实例(healthz 探测);端口绑定确认后才驻留(否则成攥着 DB 的僵尸);
  topbar 子进程 WM_CLOSE 优雅关(让 AppBar 归还屏幕空间)。

## 7. CCTopBar 全解(最复杂子系统,重点)

**原生壳(topbar.py)**
- pywebview(EdgeChromium/WebView2)frameless+on_top 窗口,加载 /topbar.html;
  WebView2 不可用自动回退 tkinter 素版(_tk_main,功能不缺)。
- AppBar:SHAppBarMessage ABM_NEW/QUERYPOS/SETPOS 占据屏幕顶端(最大化窗口让位);
  退出必须 ABM_REMOVE(closing 事件+finally 双保险)。
- 关键修正史:min_size 默认(200,100)会把 32px 条撑成 100px+(挡屏事故);
  WS_EX_TOOLWINDOW 移出任务栏;DwmSetWindowAttribute 关 Win11 自动圆角(33=DONOTROUND)
  与 1px 描边(34=COLOR_NONE);shown 后 SetWindowPos 压回精确矩形并回读验证。
- 两行模式:api.set_rows(1|2)——窗口高与 AppBar 预留同步倍高;页面按列填充网格
  (红左黄中绿右的纵排);行数由页面每 2s 兜底重发(首渲染可能早于桥注入被 ?. 吞)。
- 右键菜单=**独立小窗**(topbar_menu.html,常驻隐藏,menu() 时注入上下文+定位+show)。
  弃案存档:窗口加高+SetWindowRgn 区域裁剪——WebView2 走 DirectComposition,
  **区域裁剪对渲染无效**(只裁点击),表现为全宽黑带,此路不通。
- 菜单定位三坑:show() 与手动 SetWindowPos 竞态(改用同 GUI 队列的 move());
  move() 收**逻辑像素**(内部自乘 DPI,传物理值会跑到 1.25 倍位置);
  show 过程焦点抖动会触发 blur 自藏(600ms 宽限期)。
- 环境变量 CCTOPBAR_MOCK=rXyYgZ 直通真实实例(健壮性测试用);独立 topbar.log。

**页面动效(topbar.html,全部参数都在这一个文件)**
- 状态对象 S:目标值(redFrac/yelFrac/fadeEnd ← 每 2s 轮询)与显示值(dispRed/dispYel/
  dispFade ← 每帧指数平滑 τ=0.5s)分离——色带缓动涨跌的机制,中途变化天然平滑改道。
- 帧率:动效期不节流(rAF 垂直同步=屏幕刷新率,180Hz 屏即 180fps);全静止降 5fps。
  平滑按 dt 计算,帧率无关。
- 分区底色(shader):首个有色带从 x=0 铺过 logo(无接缝);红带宽=红占比,黄带随后,
  过渡 smoothstep 半宽 0.025;色值 redC=(0.42,0.11,0.10) yelC=(0.40,0.28,0.07)。
- 飞块:9 粒子程序化(hash 无状态),圆角方块(sdRoundBox SDF+1px 抗锯齿),
  生成纵坐标随机、水平直线飞行、ease-out 减速、尾段才渐隐;
  终点=最右 tag 右缘+15% 屏宽(S.fadeEnd);
  **独立生命通道**:强度 uSq 单独平滑(τ=0.3s,黄清零时原地淡出不瞬灭),
  生成区间 uSqB0/B1 黄存续期跟随、清零后冻结。
- 全绿转场「绿条收卷」:触发=红+黄全部清零的边沿;拍快照 sweepEnd(红+黄总带右界)
  与 sweepRed(红黄分界);亮绿竖条(核心 0.62,1.0,0.78+柔光)从最左扫到快照末端,
  **残带按原色分段绘制**(红段红黄段黄,被推着收走);飞行曲线 flightP(u,m):
  快起(35%时间)→黄带末端「缓速掠过」(5%路程/20%时间≈0.31s,vc=0.25)→
  Hermite S 形提速再缓停(45%时间),三段速度精确连续(有 python 数值验证);
  m=快照末端归一化路程;之后右端停留→闪绿(1.55~3.0s,色 0.10,0.62,0.34)→回深。
- 红脉冲:红数 0→n 边沿,整条 0.3s 短促闪红(uRedPulse);红清零不做仪式(设计决定)。
- **粘性排序**:order 数组持久;提升(变红/黄)才移动——红插红块末尾、黄插活动块末尾;
  变绿原地留守;新会话空闲进队尾。语义:越靠左的绿=越近完成。五场景浏览器单测过。
- 调试:?mock=rXyYgZ 页面级伪造;javascript_tool 可直接改 S 触发 finale/脉冲。

**shader uniform 清单**:t,r,uLogo,uRed,uYel,uFadeEnd,uFinale,uSweepEnd,uSweepRed,
uRedPulse,uSq,uSqB0,uSqB1。JS 帧循环上传,全在 topbar.html。

## 8. 调试与验收方法论(血泪版)

- **GDI 截屏(PIL ImageGrab)拍不到 WebView2/DComp 内容**——会拍到"壁纸"以为窗口
  不存在。正解:PrintWindow(hwnd, dc, PW_RENDERFULLCONTENT=2) 取帧。模板:
  见 git 历史或 scratchpad;核心=GetWindowDC→CreateCompatibleBitmap→PrintWindow→GetDIBits。
- **Chrome 后台/未激活标签 rAF 完全冻结**——探针会测出"循环死了"的假象
  (visibilityState=hidden)。页面逻辑单测用 javascript_tool 纯求值不受影响;
  动画视觉验收要么让标签真可见,要么在真实顶栏(永远置顶)上 PrintWindow。
- 曲线/算法类改动:先用 python 数值验证(端点/连续性/单调性),再上 shader。
- 深色特效叠深色背景的截图无法区分假设——验收截图要选能证伪的背景/放大倍数。
- 观测工具本身要先验收(本项目三次被骗:SetWindowRgn 假成功、rAF 假死、GDI 假失踪)。

## 9. 踩坑档案(按主题,全部实测)

- pywebview:pythonnet 3.1.0 在 Python 3.14 可用;min_size 默认值陷阱;move() 逻辑像素;
  show() 异步队列;js_api 首渲染早于桥注入(pywebviewready 事件+周期兜底重发)。
- uv:venv 的 pythonw.exe 是垫片,部分 uv 版本编成**控制台子系统**(黑窗 title=pythonw.exe,
  关窗杀进程)——无窗启动一律 Start-Process -WindowStyle Hidden 包裹;
  垫片会驻留并拉起真解释器,同命令行父子两进程,数实例按对折算。
- PowerShell 5.1:写文件默认带 BOM(Config.load 用 utf-8-sig);Invoke-RestMethod 字符串
  body 中文变问号(要 UTF8.GetBytes);内嵌双引号劈参数(commit message 别用");
  控制台中文乱码是显示层,数据往往没坏,用文件/python 验真值。
- Windows:PID 复用一天内真实发生(杀进程前必核 cmdline+创建时间;win-env #14/23);
  Win11 24H2 所有控制台宿主在 WindowsTerminal 进程里,杀它=杀全部终端(惨案实录);
  浏览器无 Cache-Control 会启发式缓存旧页面(服务端已统一 no-cache)。
- WT 聚焦:标签标题=状态符号(✳◑等)+「注册表name 或 ai-title」二选一,剥符号双候选
  匹配;SetForegroundWindow 前敲一下 Alt 绕前台锁。
- CC 并发:同会话二次 resume 被杀;bg 会话无窗不可聚焦(接口 409)。

## 10. 待办与路线

- **P1 远程只读**:手机看状态灯/搜索(happy 23k★ 证明的需求)。FastAPI 只差 token 鉴权
  +隧道;本机有 ngrok(V:\Tools\ngrok,CC 内跑要先清 HTTP_PROXY)。建议只读模式。
- **P1 HTML 导出**:详情页已有 MD,补自包含 HTML 单文件分享。
- P2:错误模式分析(sniffly 的独家点);Gemini CLI provider(本机没装,格式未采);
  配额窗口按模型分线;topbar 点击格子聚焦后高亮反馈。
- 明确不做:GUI 内聊天(opcode 之死)、worktree 编排(Nimbalyst/vibe-kanban 的仗)、
  红清零仪式、finale 被打断即中止(用户判为伪需求,现状=播完)。
- 用户口头提过可能改的:topbar 美术继续迭代(改 topbar.html 即可,shader 参数集中)。

## 11. 多机部署实况

- 开发机(工作电):C:\CoreWork\ClaudeDeck;归档 C:\CoreWork\ClaudeArchive;
  自启已挂(计划任务 ClaudeDeckTray,经 -WindowStyle Hidden 包裹)。
- Malygos Workbench(主机):**A:\ClaudeDeck**(A 盘=应用盘),archive_dir 已指
  V:\ClaudeArchive;该机 winget 版 uv 有 pythonw 垫片控制台 bug(已被隐藏启动根治)。
- 同步:git pull 即可;config.json/data/ 不入库,各机独立。

## 12. 今日(08-20~21)提交速览

goal 三件套(聚焦/CCTopBar/打标)→ 打标入口重做(就地编辑)→ WebView2 壳+shader
→ 挡屏修复+贴边(DWM)→ 抽屉→独立菜单窗(区域裁剪弃案)→ 菜单定位三连修
→ 状态可视化(分区/飞块/转场/两行纵排)→ logo 融合 → 飞块参数终稿+两行原生实测
→ 圆角 SDF → 转场提亮+停留 → 三段速度曲线 → 匀速段 5% → 动态锚定黄末端
→ 缓速掠过(可感知)→ 刷新率自适应 → 飞块瞬灭修复(独立生命通道)
→ 红→绿收卷+粘性排序。全部有对应 commit,message 写得很细,可当细粒度日志用。
