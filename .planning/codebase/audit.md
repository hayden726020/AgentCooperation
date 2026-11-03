# AgentCooperation 现状审计与功能完成矩阵

> **更新时间**：2026-10-17  
> **执行环境**：Windows 11 (NT) | Python 3.14.0 | Git 2.4x  
> **基线分支**：`backup/pre-vibe-coding-optimization-20261017`（HEAD: `fcee2cb`）  
> **测试基线**：主套件 **591 passed, 28 skipped, 0 failed**（耗时 241.92s）  

---

## 1. 项目安全接管基线

已于 2026-10-17 建立本地安全恢复点：
1. **Git 快照分支**：`backup/pre-vibe-coding-optimization-20261017`。
2. **工作区改动备份**：用户未提交的代码（`README.md`、`tests/test_nailong_ssh.py`）及未跟踪文件（`docs/NAILONG_SSH.md`、`scripts/nailong_ssh.ps1`、`scripts/install_nailong_ssh.ps1`）完整备份至 `.runtime/backup/pre_optimization_20261017/`。
3. **依赖与环境清单**：Python 3.14.0、118 个 pip 依赖包（含 `fastapi==0.143.0`、`sqlmodel==0.0.48`、`pytest==9.0.3`、`openai==2.36.0`、`psutil==7.2.2` 等）。
4. **测试基线验证**：全量测试 591 个 passed，核心调度、预算防线、测试沙箱、闭环流转均处于健康状态。

---

## 2. 功能完成矩阵 (A-F 状态评级)

| 功能模块 | 关键源文件 | 状态 | 验证证据 | 风险与说明 |
| :--- | :--- | :---: | :--- | :--- |
| **单 Flash 互斥选择器** | `orchestrator/engine_selector.py` | **A** | `test_engine_selector.py` | 严格约束单任务 GLM/DeepSeek 二选一 |
| **月度预算预扣与物理结算** | `orchestrator/budget_guard.py` | **A** | `test_budget_guard.py` | ¥200/月硬限额、预扣、结算与退回逻辑完备 |
| **Codex 周额度状态机与补审** | `orchestrator/codex_guard.py` | **A** | `test_codex_guard.py`, `test_codex_recovery_ui.py` | 额度耗尽时转入 `CODEX_BACKLOGGED`，证据暂存并在恢复后补审 |
| **Git 工作树生命周期与检查点** | `execution/worktree.py` | **A** | `test_execution.py` | 支持原子创建、提交检查点、diff 捕获与安全清理 |
| **无凭据测试沙箱与看门狗** | `execution/test_runner.py` | **A** | `test_execution.py` | 敏感环境变量隔离、超时看门狗、跨平台路径注入防范 |
| **异步子进程与进程树清理** | `execution/subproc.py` | **A** | `test_execution.py` | Windows `psutil` 递归杀死进程树、ANSI 转义清洗 |
| **Gemini 研发与独立复核闭环** | `orchestrator/gemini_closed_loop.py` | **A** | `test_gemini_closed_loop.py`, `test_hermes_gemini_probe.py` | 阶段五正式验收，支持证据只读移交、P0 拦截审批、有界自修 |
| **Antigravity / Codex 审查脱敏** | `agents/antigravity_reviewer.py`, `agents/codex_reviewer.py` | **A** | `test_antigravity_reviewer.py`, `test_cli_review_redaction.py` | 敏感凭据正则清洗与输出截断 |
| **Multica 看板集成与轮询** | `integrations/multica/worker.py` | **A** | `test_multica_bridge.py` | 状态双向映射、异常断线恢复 |
| **Web 仪表盘控制台** | `web/app.py`, `web/api.py` | **A** | `test_web.py` | 任务看板、日志流、审批交互界面 |
| **运行事件层与 SSE** | `execution/events.py`, `web/api.py` | **A** | `test_events.py`, `test_web_runs.py` | JSONL 事件、敏感键整棵子树脱敏、跨进程序号锁、增量查询、心跳与 `Last-Event-ID` 续传、暂停态非终态、终态 204、断开 `stop_event` |
| **实时运行台（单输入 + 可视化 + 回放 + 审批）** | `web/run_service.py`, `web/api.py`, `web/static/js/run_console.js`, `web/templates/index.html`, `orchestrator/gemini_closed_loop.py` | **A** | `test_web_runs.py`, `test_gemini_closed_loop.py`, 无头浏览器端到端验收（批准→SUCCESS，拒绝→FAILED） | 单需求输入 + 智能默认、Agent 输出/时间线/diff/费用实时展示、SSE + 轮询兜底、历史回放、暂停/恢复续流、GUI 审批备注与并发保护、证据摘要、按仓库过滤历史 |
| **CLI 直接需求入口** | `hermes-agent/plugins/agent_cooperation/cli.py`, `commands.py` | **A** | `test_hermes_integration.py`, `test_gemini_closed_loop.py` | `hermes coop "<需求>"` 与 `/coop <需求>` 默认走闭环；近似子命令防误触发；旧子命令兼容 |
| **Hermes MCP 协议与 Bot 桥接** | `integrations/hermes/mcp_server.py` | **B** | `test_hermes_integration.py` | 本地协议测试通过，长运行会话待实测 |
| **超时期任务远端状态确认** | `orchestrator/core.py` | **B** | 依赖 subprocess 超时捕获 | 发生超时直接置 FAILED，缺乏对远端可能已生效的二次确认 |
| **项目上下文提取与 Token 对齐** | `orchestrator/core.py` (`_project_context`) | **C** | 单元测试通过 | 采用 24,000 字符暴力扫描截断，未接入真实的 Token 计算与按需索引 |
| **跨会话持久化记忆与任务交接** | 项目全局 | **C** | 仅依赖 SQLite 任务表与执行日志 | 缺乏文件化项目状态（如 `.planning/`）、决策日志和模块接口索引 |
| **奶龙 SSH 客户端交互** | `scripts/nailong_ssh.ps1` | **C** | 4 failed (TDD 未完成) | 用户正在开发的脚本与测试，逻辑尚未全部实现 |
| **GSD 阶段化规划与断点恢复** | 待引入 | **D** | 尚未集成 | 缺乏结构化任务拆解与断点恢复框架 |
| **Serena 符号语义检索** | 待引入 | **D** | 尚未集成 | 当前代码检索主要依赖文本扫描，缺少精确符号跳转 |
| **双流水线代码冗余** | `orchestrator/core.py` | **E** | 有测试 | `run_task` 与 `resume_task_after_approval` 存在 150+ 行重复代码 |

状态说明：
- **A**：已实现，且有测试或运行证据证明正常工作。
- **B**：代码已实现，但尚未充分验证。
- **C**：部分实现，存在未完成逻辑。
- **D**：尚未开发。
- **E**：已有实现存在缺陷或重复，需要修复。
- **F**：实现方向存在架构问题，需重新评估。
