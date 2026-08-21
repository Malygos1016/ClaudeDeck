# 聚焦/CCTopBar/会话打标 三件套

## Stage F1: 聚焦服务端能力
**Goal**: POST /api/live/{sid}/focus——UIA 找到该会话所在的 Windows Terminal 标签并选中+置前;找不到 tab 时降级置前 WT 窗口
**Success Criteria**: 本机实测点击后对应 CLI 窗口/标签到前台;绕过前台锁
**Tests**: 纯映射逻辑单测(标题匹配);GUI 动作人工验收
**Status**: Complete

## Stage F2: 会话打标(tag)
**Goal**: tags.json 持久存储(不进可重建的 DB)+ PUT /api/sessions/{sid}/tag;看板卡/灯条/列表显示 tag,看板卡上可编辑
**Success Criteria**: 打标后三处显示一致,重建索引不丢
**Tests**: tag API 读写/live 合并
**Status**: Complete

## Stage F3: 网页灯条点击聚焦
**Goal**: 灯条 tag 与看板窗口卡点击 → 调 focus API
**Success Criteria**: 网页任意页点灯条格子,对应窗口到前台
**Tests**: 人工验收
**Status**: Complete

## Stage F4: CCTopBar 桌面常驻条
**Goal**: python -m app.topbar——tkinter 无边框条,AppBar 注册占据主屏顶端(最大化窗口让位),2s 轮询 live,tag 灯+点击聚焦;托盘菜单可开关,config 记忆
**Success Criteria**: 常驻不被最大化窗口盖住;点击聚焦;随托盘退出
**Tests**: 人工验收(GUI)
**Status**: Complete
