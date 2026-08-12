# ClaudeDeck 实施计划

总纲见 `~/.claude/plans/ultrathink-claudecode-glittery-minsky.md`(数据 schema 附录也在那里)。

## Stage 1: 解析器 + 索引 + 归档快照 + CLI
**Goal**: transcript/scanner/indexer/db/config/archive/search/cli 模块;`python -m app.cli scan|search|title` 可用;history.jsonl 补索引;归档镜像随扫描生效。
**Success Criteria**: 真实数据首扫 ≤5 分钟;二轮增量 <2s;「托卡马克」FTS 命中、「流量」LIKE 回退命中;归档目录出现全部安静期会话镜像;抽查 last_ts≠mtime 会话正确。
**Tests**: test_transcript / test_indexer / test_search / test_archive(32 例)。
**Status**: Complete(2026-08-12:首扫 2.4s、增量 0.075s、归档 668.5MB/371 文件、900dd7ec 的 21h mtime 失真正确规避、32/32 绿)

## Stage 2: API 服务 + 列表/搜索页
**Goal**: main.py + routes(sessions/search/index/healthz) + index.html + 启动 bat;后台扫描线程 + 进度接口;列表页含复制 resume 命令按钮。
**Success Criteria**: 双击 bat 打开列表页;首扫途中列表渐进出现;两种搜索走通且短词带 fallback 提示;仅监听 127.0.0.1(netstat 验证)。
**Tests**: test_api(TestClient 全路由冒烟,httpx trust_env=False)。
**Status**: Not Started

## Stage 3: 状态看板
**Goal**: live.py/stats.py + live.html:运行中窗口实时卡片(FILETIME 验活+降级)、jobs、磁盘只读统计、token 双曲线、plans 关联。
**Success Criteria**: 3 个真实 CC 窗口(其一重命名选项卡)2s 内 busy/idle 全对;关窗 ≤4s 消失;blocked job 的 needs 显示;磁盘统计误差 <1%;token 曲线近 30 天非空。
**Tests**: test_live(FILETIME 换算/pid 复用/降级)、test_stats(日差分聚合)。
**Status**: Not Started

## Stage 4: 会话详情聊天视图
**Goal**: render.py + messages 分页接口 + session.html:折叠全套、深链高亮、子 agent 抽屉、源缺失落归档副本。
**Success Criteria**: 最大会话首屏 <3s;compact 分隔线正确;搜索命中深链跳转高亮;系统事件开关不破坏锚点。
**Tests**: test_render(每种折叠 kind、HTML 转义、around_seq 边界)。
**Status**: Not Started

## Stage 5: 动作与管理
**Goal**: launcher.py + actions 路由 + archive.html + settings.html:一键拉起(trust 预写)、导出 MD、归档管理/还原、purge、设置页。
**Success Criteria**: 一键恢复拉起 WT 新标签成功;大会话提示按 2+回车;cwd 缺失降级;fixture 上归档→删源→可看→还原字节一致→purge 演练;导出 MD 可读。
**Tests**: test_launcher(命令构造/trust 读改写/异常不阻断)、test_archive 还原。
**Status**: Not Started

纪律:每阶段 pytest 全绿才进下一阶段;UI 阶段动手前过 frontend-design + motion skill(用户指定),token 曲线前过 dataviz;全程 git 增量提交;完工删除本文件。
