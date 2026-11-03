# Agent 运行台：需求与实施方案

> 状态：M0 已确认，M1、M2、M3 已完成，待 M4
> 确认日期：2026-10-18
> 关联：`.planning/ROADMAP.md` 阶段七、`.planning/STATE.md`

## 1. 已确认需求

| # | 需求 | 决定 |
| --- | --- | --- |
| 1 | 不同 Agent 运行时可视化 | 本地 Web 控制台；首期包含实时输出流、步骤/状态时间线、文件 diff 预览、成本/Token/耗时 |
| 2 | 简略交互流程 | 单输入框 + 智能默认；高级选项折叠，不要求用户输入前缀和参数 |
| 3 | 安全命令范围拓宽 | 仅放行测试、构建、类型检查；格式化自动修复、依赖安装、写 Git 暂不放行 |
| 4 | 最终迭代成品 | 浏览器访问 localhost 的 GUI，交互形态参考 Codex / Antigravity / DS harness；后续可选桌面壳 |

## 2. 已确认假设

1. GUI 顶部按白名单切换多个仓库。
2. P0 任务审批在 GUI 内完成。
3. 运行历史保留最近 50 次并支持回放。
4. 首期命令支持 Python 与 Node，其他技术栈通过配置扩展。
5. Linux 上继续通过浏览器访问同一控制台。
6. 保留 `/coop loop` 旧入口，新入口与其兼容。

## 3. 现状与差距

| 差距 | 位置 | 影响 |
| --- | --- | --- |
| 子进程输出结束后才返回 | `execution/subproc.py` `run_async_command` | 无法实时展示 agent 运行过程 |
| Web 只有轮询看板与 JobManager | `web/runner.py`、`web/api.py` | 无法订阅运行事件 |
| Gemini Dev 已内部流式读取但最终一次性返回 | `agents/gemini_dev_agent.py` | 文本增量被吞掉，原生工具步骤结束后才可见 |
| 复核走非流式 API 或阻塞子进程 | `agents/flash_reviewer.py`、`agents/antigravity_reviewer.py` | 复核阶段只能展示进行中状态与耗时 |
| `/coop loop` 参数多且默认值分散 | `hermes-agent/plugins/agent_cooperation/cli.py` | 与“直接输入需求”目标冲突 |
| TestRunner 仅允许 pytest/python | `execution/test_runner.py` | 无法覆盖构建、类型检查与 Node 项目 |

## 4. 总体架构

```
Web UI（运行台）
  │  REST + SSE
  ▼
Web API（run 管理、默认值解析、审批）
  │
  ▼
RunService（启动闭环、订阅事件）
  │
  ▼
EventBus / EventStore（seq + JSONL + 脱敏）
  │
  ▼
Instrumented Orchestrator / Agents
  │
  ▼
run_async_command（on_line 流式回调） / Antigravity stream-json
```

### 4.1 事件模型

新增 `execution/events.py`：

- 字段：`seq`、`task_id`、`agent`、`phase`、`type`、`timestamp`、`payload`
- 类型：`status`、`output`、`tool_call`、`tool_result`、`test_line`、`diff`、`usage`、`approval_required`、`result`
- 存储：SQLite 任务/步骤账库保持不变；事件全文写入 `.runtime/events/<task_id>.jsonl`
- 脱敏：所有字符串载荷先经 `execution.security.redact_and_truncate_evidence`
- 序号：由 EventStore 按任务单调递增，SSE 支持 `Last-Event-ID` 与 `?after=seq` 续传

### 4.2 流式采集

- `run_async_command` 增加可选 `on_line` 回调，边读边发事件，同时保留最终完整结果
- `GeminiDevAgent` 消费 `stream=True` 的文本增量；原生工具步骤通过增量读取 `last_native_tool_results` 上报
- 不修改 `hermes-agent` 核心；仅在 AgentCooperation 侧包装
- 测试命令逐行上报 `test_line`，构建与类型检查复用同一通道
- 复核阶段上报“证据交接、请求发出、结论返回”三个节点；是否逐字流取决于渠道能力

### 4.3 Web API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/runs` | 提交需求，服务端解析智能默认值 |
| GET | `/api/runs` | 运行历史列表 |
| GET | `/api/runs/<task_id>` | 运行详情与摘要 |
| GET | `/api/runs/<task_id>/events` | 增量事件查询（轮询兜底） |
| GET | `/api/runs/<task_id>/events/stream` | SSE 实时事件流 |
| POST | `/api/runs/<task_id>/approve` | P0 批准 |
| POST | `/api/runs/<task_id>/reject` | P0 拒绝 |
| GET | `/api/runs/<task_id>/diff` | 工作树 diff 预览（未实现，当前通过闭环 `diff` 事件预览） |

### 4.4 UI 布局

```
┌ 项目: NAILONG ▾   模式: 真实   预算: ¥12.3/200 ──────────────┐
│ [ 输入研发需求...                                   ]  运行  │
│ 高级选项 ▸ 复核 GLM | 测试路径 自动 | 最大修复 3             │
├──────────────┬──────────────┬──────────────┬───────────────┤
│ 开发 Gemini  │ 复核 GLM     │ 测试 pytest  │ 时间线         │
│ 实时输出...  │ 进行中 2m    │ 实时输出...  │ ● 规划          │
│ 工具调用...  │              │              │ ● 开发          │
├──────────────┴──────────────┴──────────────┴───────────────┤
│ [Diff] [证据] [审批]   SUCCESS | ¥0.42 | 12m | 3 次修复      │
└─────────────────────────────────────────────────────────────┘
```

- 现有看板保留，新增“运行台”Tab
- 任务历史列表支持打开历史运行并回放事件
- 审批按钮直接操作 P0 任务
- diff 每轮开发后刷新，敏感内容脱敏后展示

### 4.5 交互简化

- GUI：只填需求；项目取顶部选择，其余参数使用服务端默认，高级选项可覆盖
- CLI：`coop "需求"` 等价于在当前目录执行闭环；`coop loop ...` 保留兼容
- Hermes 入口：`/coop <需求>`，非已知子命令时按需求处理

### 4.6 安全命令范围

- 仓库级配置声明 `allowed_commands`，按 `test / build / typecheck` 分类
- 只使用 argv 数组，不经过 shell；cwd 强制为任务工作树
- 沿用超时、进程树清理、环境变量白名单、输出脱敏
- 未声明命令一律拒绝并写入审计事件，不提供任意命令逃生口

## 5. 里程碑与验收

| 阶段 | 交付物 | 验收 |
| --- | --- | --- |
| M0 | 方案、事件模型、配置 schema、UI 线框、测试清单 | 本文档确认 |
| M1 | 事件总线 + 流式子进程 + SSE + CLI 直接输入 | 单元测试覆盖事件顺序、续传、超时、CLI 默认动作 |
| M2 | 闭环埋点 + 运行 API + 实时运行台 | 开发/测试输出 1 秒内可见，时间线与费用正确（已完成：SSE 实时输出/时间线/diff/费用，历史回放，暂停态续流；无头浏览器验收 SUCCESS，全量 704 passed） |
| M3 | diff 预览 + GUI 审批 + 历史回放 + 多项目切换 | P0 可在 GUI 内批准/拒绝，历史可回放（已完成：审批/拒绝 API 与并发保护、GUI 审批备注与按钮、证据摘要面板、按仓库过滤历史；无头浏览器验收批准→SUCCESS、拒绝→FAILED） |
| M4 | 命令范围拓宽 + 注入/越权回归 | 非白名单命令全部拒绝，全量回归通过 |
| M5（可选） | 桌面壳、Linux 远程访问验证 | Windows/Linux 浏览器均可使用 |

## 6. 测试计划

- 新增 `tests/test_events.py`：事件序号、JSONL 持久化、脱敏、SSE 续传
- 扩展 `tests/test_execution.py`：`on_line` 回调、超时清理、输出完整性
- 新增 `tests/test_web_runs.py`：运行 API、默认值解析、审批、事件查询
- 扩展 `tests/test_cli.py`：`coop "需求"` 默认动作与旧入口兼容
- 安全回归：命令白名单、参数注入、路径穿越、敏感文件写入拦截
- 全量入口：`python -m pytest tests/ -q -p no:cacheprovider`

## 7. 风险与非目标

风险：

- Antigravity CLI 原生工具事件只有完成态，无法展示调用中状态；首期按增量检测上报
- 复核渠道不保证 token 流，首期展示阶段进度与最终结论
- Windows 与 Linux 的进程树清理、编码差异需分别验证

非目标：

- 多用户、多租户与远程公网部署
- 完全替代 Codex / Antigravity 的 IDE 能力
- 首期不支持格式化自动修复、依赖安装、写 Git
