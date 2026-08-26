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
- 分区底色(shader):首个有色带从 x=0 铺过 logo(无接缝);过渡 smoothstep 半宽 0.025;
  色值 redC=(0.42,0.11,0.10) yelC=(0.40,0.28,0.07)。
  **边界口径(2026-08-23 修正,曾是本项目最隐蔽的一个 bug)**:色带宽度是
  「该类格子的实际像素长度 ÷ 屏幕宽」,由 render() 直接量 DOM
  (该类最后一个格子 getBoundingClientRect().right + 半个 gap),
  经 uRedEnd/uYelEnd 直接送 shader,**不再由窗口数量占比推算**。
  旧版按 nRed/n、nYel/n 算占比再乘可用宽,与飞块终点(像素布局)是两套坐标:
  黄带按数量能铺满全屏而几个格子只占左边一小条,飞块遂生成在终点右侧倒着往左飞
  ——枚举 400 种配比有 381 种(95%)会倒飞。前提是分组排序保证同色连续(见下)。
- 飞块:9 粒子程序化(hash 无状态),圆角方块(sdRoundBox SDF+1px 抗锯齿),
  纵坐标随机、水平直线右飞、ease-out 减速、尾段才渐隐;
  终点=最右格子右缘+15% 屏宽(S.fadeEnd),**跟着标签区走,正常不出屏**;
  **出生点固定**(2026-08-23 加):锚在黄带起点,宽度写死 SPAWN_W=0.04,不随黄带
  宽窄伸缩——出生点越靠右飞行路程越短、特效量越少,锚在起点才保证够长一段路。
  黄带比 SPAWN_W 还窄就收到黄带右界;黄带整个落在 logo 区内则不生成(无出生空间)。
  shader 内 per-particle 保底 dest=max(uFadeEnd, x0+0.12):枚举 22224 组参数,
  倒飞 0 例、出生点越界 0 例,路程中位 0.56 屏宽,只有 1.9% 的极端配比会让终点
  溢出屏幕(此时照常飞出去,是设计允许的例外而非常态)。
  **独立生命通道**:强度 uSq 单独平滑(τ=0.3s,黄清零时原地淡出不瞬灭),
  出生区间 uSqB0/B1 黄存续期跟随、清零后冻结。
- 全绿转场「绿条收卷」:触发=红+黄全部清零的边沿;拍快照 sweepEnd(红+黄总带右界)
  与 sweepRed(红黄分界);亮绿竖条(核心 0.62,1.0,0.78+柔光)从最左扫到快照末端,
  **残带按原色分段绘制**(红段红黄段黄,被推着收走);飞行曲线 flightP(u,m):
  快起(35%时间)→黄带末端「缓速掠过」(5%路程/20%时间≈0.31s,vc=0.25)→
  Hermite S 形提速再缓停(45%时间),三段速度精确连续(有 python 数值验证);
  m=快照末端归一化路程;之后右端停留→闪绿(1.55~3.0s,色 0.10,0.62,0.34)→回深。
- 红脉冲:红数 0→n 边沿,整条 0.3s 短促闪红(uRedPulse);红清零不做仪式(设计决定)。
- **分组排序**(2026-08-23 改,原为「粘性排序·变绿原地留守」):每次状态变更就重排,
  严格 红→黄→绿 三段,黄色继续往前挤,**绿色不留在原地**。
  原因:色带按像素贴合格子的前提是同色连续,变绿留守会把绿夹在黄中间。
  组内稳定:红/黄按首次出现先后(Array.sort 稳定性),绿按变绿时刻倒序
  (greenAt Map 记时刻),保住「越靠左的绿=越近完成」的原语义;
  首次出现就是空闲的不记时刻,排绿组末尾。node 抽真身函数跑 8 场景+200 轮随机压测。
- **几何测量必须与数据渲染解耦**(2026-08-23 血泪):色带边界与飞块终点改按像素
  量之后,它们强依赖 innerWidth 与格子布局,而 render() 只在会话数据变化时才跑。
  只在 render() 末尾量一次的话:窗口在 pywebview 建好后还会被 on_shown 的
  SetWindowPos 改一次尺寸、中文字形度量也会事后改变格子宽度,数据不变则永不重算
  ——实测多会话时边界会永久停在「只量到第一个格子」的错值(redEnd 差了整整一倍),
  而单会话因格子少、布局早稳定,反而看不出问题,极易误判为已修好。
  现在抽成可重复调用的 measure()(状态从 DOM 的 .dot class 读,不依赖调用方传参),
  render() 末尾 + resize 事件 + canvas 尺寸变化 + 帧循环每 250ms 兜底各调一次。
  目标值改了不会跳变,dispRedEnd/dispYelEnd/dispFade 会平滑追过去。
- 两行模式的已知限制:格子按列填充,分组交界可能落在同一列的上下两行,
  该列下行的格子会被划进相邻色带(实测 waiting/busy 交界处可见)。
  除非改按行填充,否则无法根除;比旧口径(色带与格子完全无关)已大幅改善。
- 调试:?mock=rXyYgZ 页面级伪造;javascript_tool 可直接改 S 触发 finale/脉冲。

**shader uniform 清单**:t,r,uRedEnd,uYelEnd,uFadeEnd,uFinale,uSweepEnd,uSweepRed,
uRedPulse,uSq,uSqB0,uSqB1。JS 帧循环上传,全在 topbar.html。
(uLogo 已随占比口径一并废除;uRed/uYel 更名 uRedEnd/uYelEnd 且语义由占比改为位置。)

## 7.5 聚焦机制:三层身份链(2026-08-24 重构,废除"猜标题")

**为什么废除标题匹配**——同一机制三次以三种方式失效,全部本机实测:
1. CC 2.1.245 把标签标题从 ai-title 改成通用名 `Claude Code`(2.1.241 的
   DEVLOG 数据事实随一次小版本作废),「名詞解釋」格子必然 404;
2. resume 传 `--name` 把标签设成 tag 后,focus 候选 `[name, title]` 里没有 tag,
   自家拉起的窗口自己找不到;
3. 同名标签多命中时 `hits[0]` 盲取,表现为跳到别人的窗口。
病根同一个:focus 靠字符串猜「CC 会往标签上写什么」,而那是上游随时会变的行为。

**现行架构**(app/consolemark.py + app/focus.py,三层,逐层降级,每层诚实):
- 第 0 层 身份查询:会话 PID → `NtQueryInformationProcess(hProc, 49)` → 控制台
  宿主 PID(纯只读,毫秒级)。宿主是 WT 子进程 OpenConsole → 在某个 WT 标签里;
  独立 conhost 带可见窗口 → 经典控制台,直接置前;都不是 → headless
  (fork daemon / 第三方终端),这里没有可聚焦的窗口。
  本机实测:9 个 WT 标签对应 9 个 OpenConsole 全挂在唯一 WindowsTerminal 下,
  10 个注册表会话逐一查询全部正确分类(fork daemon 正确落 headless)。
- 第 1 层 标记定位:helper 子进程 `AttachConsole(pid)` → 存原题 →
  `SetConsoleTitle("[CD#<pid>]")` → ConPTY 把标题变化转发给 WT 标签 → UIA 按
  标记找到即选中 → 恢复原题。标记是自己写的,与 CC 版本/fork/tag 全部解耦。
  **延迟归因(2026-08-24 二轮实验,第一印象是冤案)**:spike 量到的"标记 2.5s
  才出现"曾归因为 WT 转发节流 —— 错。用缓存好的 UIA 引用读 .Name,标记在
  helper 返回瞬间(0ms)就已在标签上,**WT 转发是毫秒级**;2.5s 全部是 UIA 从
  桌面根全量遍历的成本(单轮 2.2s,本机 6 个 WT 窗口 9 标签,XAML 岛 UIA 树
  出名地慢)。对策:标签引用缓存(_tabs,TTL 300s)——全量扫描只在冷缓存时付
  一次,之后 .Name 是毫秒级 live 调用,不会 stale(引用失效仅发生在标签被关,
  读时抛异常触发重扫)。UIA COM 引用绑定创建线程,故 focus 全部操作固定在
  常驻单线程执行器上跑,缓存才能跨请求复用。
  另一坑:**busy 会话的 spinner 动画(◐/◑)高频重写标题会盖掉标记**(一轮
  诊断实测三通道全 miss)——对策:未命中时每 0.6s 把标记重新压回去,和动画
  抢写;缓存读取本身毫秒级轮询,标记只需存活一个轮询周期。
  第三层地板:扫描优化后压测暴露 **SelectionItemPattern.Select() 本身 ~2.5s**
  (副线程 GetWindowText 轮询证实动作就要 2.0s 才发生,不是干等返回,
  fire-and-forget 无用)。同元素换 **LegacyIAccessible.DoDefaultAction():
  调用返回 ~505ms 且返回时切换已完成**,5 倍提速、零新依赖,现为首选通道,
  Select/Click 依次兜底。键盘 Ctrl+Alt+N 曾是候选,本机 WT 键绑定无响应,弃。
  端到端:热缓存快路径 ~0.6s,标记法 ~1s;冷缓存首次仍付一次 2.2s 扫描。
- 第 2 层 标题兜底:AttachConsole 失败(管理员 CC 的完整性级别/竞态)或标记被
  `suppressApplicationTitle` 吞掉时才回到 match_tab,且**多命中返回歧义列表**,
  不再盲取第一个;零命中如实 404 并指路"复制 resume 命令"。

**实现要点/坑**:
- helper 必须是子进程:服务可能以 CREATE_NO_WINDOW 启动、自带隐藏控制台,
  主进程 FreeConsole 会弄坏自身 stdio;一个进程同时只能附加一个控制台。
- 标题经 base64 走 helper 的 argv/stdout,任意字符(引号/换行/emoji)不破协议。
- 并发聚焦用 FOCUS_LOCK 全局串行:标记法改的是全局控制台标题,交叉执行互相污染。
- 编排(focus_group,跑在专用 UIA 线程):**快路径先行** —— 标题精确匹配一次
  (缓存热时亚秒),唯一命中直接用;零命中或多命中(歧义)才升级标记法拿
  零歧义定位;标记法按组内成员逐个尝试(先点击的 sid 自己的实例,同 sid
  多实例按 startedAt 新进程优先),全败沿用快路径的歧义名单报错。
- OpenConsole 命令行只有 WT 进程内句柄(`--signal 0x1d38`),外部解引用不了,
  所以"宿主→具体标签"只能靠标记法,没有纯只读通路。

**2026-08-25 三轮定稿:「窗口通道」—— 亚秒/无报错/聚焦正确**(用户三连实报后
把"点击时现场搜索"的架构整个换掉;全局标题匹配、全局标记扫描、以及只活了
半天的全局排除法一并废除 —— 排除法慢[~4s]、有失败分支、置信度还打折,被用户
正确否决。搜索类方案的病根一致:身份不该搜,该问 OS):

- **两个关键 OS 发现(spike 实证,2026-08-25)**:
  1. `AttachConsole → GetConsoleWindow` 拿到的 PseudoConsoleWindow,其
     **GW_OWNER 精确指向托管它的真实 WT 窗口**(八会话全对,含锁题与提权),
     改名/锁题全免疫 —— "会话在哪扇窗"是毫秒级 OS 权威查询(consolemark.
     console_window_owner;服务是 pythonw 无控制台,进程内 attach 直查 ~1ms,
     自带控制台的进程走 helper 子进程)。
  2. **UIA 看不见提权 WT 窗口(整扇窗不在枚举里),但纯 Win32 EnumWindows
     看得见、GetWindowText 读得到**(计划任务语境实证;runas /trustlevel 降权
     反而看得到,那继承自提权 shell,不代表服务语境)。窗口枚举必须走 Win32。
- **现役架构(app/focus.py)**:宿主查询(NtQueryInformationProcess→OpenConsole
  →WT 进程 pid,跨完整性可查)→ 窗口解析(_resolve_window:进程唯一窗直接定;
  多窗用 owner;owner 拿不到按窗口标题唯一匹配)→ **窗口一定立刻置前**(感知
  延迟 ~0.2s,定标签的几百毫秒在用户看着正确窗口时发生)→ 窗内定标签
  (_locate_tab,只扫这一扇窗:单标签必对 → 标题匹配 → 窗内减法[本窗其余会话
  按 owner 实证成员资格、各自唯一认领,剩下即目标;宇宙只有两三个标签,任何
  一步不干净就放弃] → 窗内短标记[0.5s 封顶,WT 转发毫秒级,spinner 抢写就
  再压回去])。**窗口解析成功必定 ok**:标签定不出时正确窗口已在前台,
  如实标 tab_selected=False —— 不存在 404/409 弹给真实窗口的会话。
- 真机验收:锁题 CofeChat 0.75s(subtract 正确选中)/ 提权 ClaudeDeck开发
  0.18s(窗口置前,单标签即完成;跨完整性 SetForegroundWindow 实证生效)/
  常规 0.82s(窗内标题命中)。零报错。
- 压测(两轮 ×26 请求:同格 8 连击 / 四路并发 ×2 / 提权锁题常规混合交替 10):
  52/52 成功零失败,单次中位 0.72~0.74s;并发 p90 ~2.4s 是 FOCUS_LOCK 排队
  (UIA/控制台是全局资源,串行是正确性要求;真实使用是单击,无此长尾)。
  tier 分布与设计一致(title 17/subtract 5/window-only 4 每轮)。句柄
  372→压后 395→静置回落 381(+9 为一次性暖机,GC 有回收,无单调增长,
  无泄漏);压后 healthz ok、groups 完整。
- 保留的领域事实:管理员前缀白名单、spinner 抢写、锁题停转发、DoDefaultAction
  ~0.5s 是标签切换的地板(Select 要 2.5s)。第三方终端(宿主非 WT 的
  OpenConsole)仍如实报"没有可聚焦窗口"。

**2026-08-26 defterm 边界(A 机实测,38bd36c)**:Windows「默认终端应用」设为
WT 时,从 explorer/cmd 启动的控制台由 WT 接管显示,但**进程链上 WT 不是任何人
的父**(宿主是 conhost,父是 cmd/explorer;WindowsTerminal 零子进程)——
窗口通道的第一跳(宿主→WT 进程)在此环境不成立,且 conhost 窗口不可见,
会被误判 headless。兜底:`_scan_marker_across_wt` 反向走标记法(注入唯一标记
→ 横扫全部 WT 窗口找谁显示了它),标记走 ConPTY 标题通道、与进程归属无关。
真 headless(fork daemon)扫不到照旧返 None。**压测注脚:8-25 的 52/52 压测
全在"WT 自启标签"环境,恰好没覆盖 defterm —— 环境矩阵要记这条。**
同提交:已关闭 fork 父分支在树里补**幽灵节点**(置灰/已关闭/点击 resume,
名字从 tags/索引取叉前段;fork 关系写在作业记录里,是子会话固有属性,不随
父进程退出消失)。审阅修复(本机):接管入口与残留判定改按「活着成员的作业」
算 —— 幽灵成根后只看根会把无窗口 fork 组的 attach_job_id 算丢,格子点击
两条分支都进不去(Goal 病的新形态复发);僵尸子+幽灵父的组也照常该藏就藏。

- **僵尸残留治理**:作业已终态(stopped/done/failed)而 worker 进程仍活 =
  残留(实证:作业 9877837f state=stopped、worker+pty-host 挂 19h、心跳冻结;
  attach 对终态作业是重启尝试,撞残留即 "can't start — exit 1 before init",
  作业被 CC 改判 failed)。处置(用户拍板):
  - grouping 标 residual=True,**顶栏隐藏**(摄入口过滤,shader 计数同口径);
    deck 看板页仍可见;"在等你的必须看得见"戒律不变 —— running/blocked 绝不算残留;
  - attach 对终态作业 409 拒绝;成员动作降为 resume(继续对话的正路);
  - stop 端点升级清理链:官方 `claude stop` → 复查注册表 jobId 对应 worker
    (pid+procStart 验活)→ 仍活强杀,父进程仅当 cmdline 含 --bg-pty-host 与
    本 job id 才连带(三重身份校验,宁可漏杀不可错杀);看板页作业行挂
    「清理残留」按钮。transcript 永远不动。

**2026-08-25 补丁(用户实报两个格子点了没反应,排查出四件事)**:
- **WT 手动重命名会锁题,标记法对锁死的标签无解**:实证是标签定格在旧
  ai-title(`设计非代码能力培养平台架构`)而控制台内部标题早已换新
  (`vibecoding-v2-platform-design`)——两者脱节即锁题铁证。mark 成功 +
  标记始终不上屏 ⇒ 判定 marker_suppressed,路由回 409 指路解锁(双击标签
  清空名字回车恢复自动标题),并附控制台真实标题帮用户认标签。不装"没找到"。
- **fork 作业的 worker 会话 id 会演化,state.json 的 session_id 停在创建时**
  (作业 9877837f 记着 9877837f-…,活 worker 已是 ed2bd59a-…)。注册表条目的
  `jobId` 才是权威链接,grouping 按它把作业补挂到 worker 当前 sid 下
  (_reconcile_job_links),否则 blocked 映射/attach 动作/组 attach_job_id 全断。
- **treePayload 一直没转发后端算好的 member.action**,树端全部退化成 focus,
  「恢复/接管」从未生效过 —— 分支首版就漏了,不是合并事故。已补。
- **点击失败原先静默吞掉**(post 不看响应),用户分不清"没点上"还是"去不成"。
  现在格子红闪半秒(.cell.deny),无窗口且无作业的死格子点击同样红闪。

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
- **窗口标题不可信,尤其在 fork 之后**(2026-08-24 血泪):fork 会把**父会话的
  ai-title 也改写**成带 ⑂ 的名字,于是「恢复出来的父分支窗口」与「跑着子分支的
  原窗口」顶着同一个标题,靠标题匹配聚焦必然跳错(用户实报点父节点跳到了子分支)。
  解法是恢复时传 `--name`,给那个实例一个独立显示名(取用户 tag,没有就取标题里
  叉子之前那截)。CC 的 `-n/--name` 会同时改注册表 name 与终端标题,标题遂能区分。
  另注:WT 标签的 UIA 里,`TabItemControl.Name` 是 CC 动态设置的标题(会被改写),
  而 `TermControl.Name` 保留着建标签时 `wt --title` 传的值(不被 CC 覆盖)——
  看似是个稳定锚点,但**只有当前活动标签才有 TermControl 节点**,非活动标签取不到,
  故不能用来定位。想彻底摆脱标题匹配,得另找 shell 进程号到标签的映射。
- **同一会话可以有多个实例**:恢复 fork 父分支之后,父会话就同时开着两个窗口
  (原窗口那个已被子分支接管 + 新恢复的)。分组要按 session_id 去重,否则树里出现
  重复条目;判断「哪个实例真正拥有窗口」用启动时刻与 forkBoundaryAt 比,
  早于 fork 的那个不算(它的窗口已经切去跑子分支了)。
- **别靠目测截图判几何**:截图在对话/查看器里是缩放显示的,估出来的归一化坐标能
  错到 0.65 vs 0.83。要么脚本扫像素(用已知的 brand 宽度定标),要么开个隐藏不了的
  探针窗口 evaluate_js 直接读 S 的真值——后者最省事,写法见 scratchpad/probe_state.py:
  另起一个 pywebview 窗口加载同一个 topbar.html,读 S.fadeEnd / sqB0 / 各格子 right。
  两个前提:必须 SetProcessDpiAwareness(2)(否则窗口被 DPI 虚拟化,innerWidth 对不上),
  且**不能 hidden**(隐藏窗口 rAF 冻结,帧循环里的 dispFade/sqB0/sqB1 会停在初值,
  看着像「平滑器坏了」)。判断「是逻辑错还是时机错」的招:探针里当场再调一次
  measure() 比对前后值,变对了就是时机问题。
- 追踪飞块方向要高速连拍(先连拍存原始位图、再离线分析),帧隔压到位移远小于块间距:
  0.18s 间隔下最近邻配对会把「新进入的块」错配成「旧块左移」,得出 8:8 的假结果;
  5.6ms 间隔下同一判据给出 43:0。另注意飞块在终点附近速度趋零且渐隐,
  质心会抖 ±3px,别把这当成倒飞。

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
