# 系统架构与模块职责说明 (Architecture & Modules)

> **版本**：2026-10-17  
> **设计原则**：严格单模型执行槽位、独立只读复核、工作树物理隔离、¥200/月预算守卫、确定性交付。

---

## 1. 核心分层架构

```
┌─────────────────────────────────────────────────────────────┐
│                       Entrypoints                           │
│   orchestrator.cli  |  web.app (FastAPI)  |  hermes bridge  │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                    Orchestration Layer                      │
│   SingleEngineSlotEnforcer (GLM / DeepSeek Flash 互斥槽)     │
│   TaskLifecycleFSM (13状态确定性流转)                        │
│   GeminiClosedLoopOrchestrator (阶段五闭环：Gemini + 复核)   │
│   BudgetGuard (¥200/月预扣结算) | CodexQuotaGuard (周额度)  │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                      Execution Layer                        │
│   WorktreeManager (Git 独立分支/工作树/diff/checkpoint)      │
│   TestRunner (无凭据隔离沙箱 + 超时看门狗 + 结果正则解析)      │
│   Subproc (异步进程执行 + Windows 进程树递归清理 + ANSI清洗)  │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                       Storage Layer                         │
│   SQLModel / SQLite (生产库 / Mock 库物理隔离)              │
│   Task, ExecutionLog, PendingCodexAudit, MonthlySpending    │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. 模块职责清单

| 模块目录 | 核心文件 | 核心职责 | 依赖关系 |
| :--- | :--- | :--- | :--- |
| **`orchestrator/`** | `core.py` | 旧版自动流水线调度器：规划、开发、测试、自修与 Codex 升级 | 依赖 `fsm.py`、`execution/`、`storage/`、`agents/` |
| | `fsm.py` | 任务 13 状态有限状态机（INIT -> PLANNING -> DEVELOPING -> TESTING -> REPAIRING -> REVIEWING -> ESCALATING -> CODEX_BACKLOGGED -> SUCCESS / FAILED / WAITING_APPROVAL / BUDGET_STOPPED / INTERRUPTED） | 无外部业务依赖（基于 transitions 库） |
| | `engine_selector.py`| 单引擎槽位强制器：保证单任务内 GLM 与 DeepSeek 严格互斥 | 依赖 `config/settings.py`、`agents/` |
| | `gemini_closed_loop.py`| 阶段五闭环调度器：Gemini 在工作树内执行开发，GLM/DeepSeek 担任只读独立复核，结构化证据交接与 P0 拦截 | 依赖 `execution/`、`storage/`、`agents/` |
| | `budget_guard.py` | 预算守卫：管理每月 ¥200 硬预算，执行调用预扣（reserve）、结算（settle）与退回 | 依赖 `storage/`、`config/settings.py` |
| | `codex_guard.py` | Codex 周额度守卫：监测额度耗尽（EXHAUSTED），管理补审挂起队列 | 依赖 `storage/` |
| **`agents/`** | `flash_api.py` | 单 Flash 模型 API 适配器：Token 上舍入计费、文件提取与目标安全路径校验 | 依赖 `openai` SDK |
| | `gemini_dev_agent.py` | Gemini 研发代理：调用原生 Antigravity CLI 工具执行工作树内开发 | 依赖 `execution/subproc.py` |
| | `flash_reviewer.py` | GLM / DeepSeek 独立只读复核代理：接收 diff 与 test summary 产出 ReviewVerdict | 依赖 `agents/flash_api.py` |
| | `antigravity_reviewer.py` | Antigravity CLI 独立只读审查器：脱敏与结构化审查 | 依赖 `execution/cli_launcher.py` |
| | `codex_expert.py` | Codex 专家升级代理：在自修失败后提供诊断与修复建议 | 依赖 `execution/cli_launcher.py` |
| **`execution/`** | `worktree.py` | Git 工作树管理器：为每个任务创建独立分支和隔离物理目录，管理 checkpoint 与 diff | 依赖 git CLI |
| | `test_runner.py` | 测试运行器：自动探测 pytest、移除敏感环境变量、超时看门狗、多测试格式解析 | 依赖 `subproc.py` |
| | `subproc.py` | 异步子进程驱动：注入非交互环境变量、Windows 进程树递归清理、ANSI 转义清洗 | 依赖 `psutil` |
| **`storage/`** | `db.py` | 数据库会话提供器：区分生产 SQLite 库与测试/Mock 隔离库 | 依赖 SQLModel / SQLAlchemy |
| | `models.py` | 数据库表模型：`Task`、`ExecutionLog`、`PendingCodexAudit`、`MonthlySpending` 等 | SQLModel 表定义 |
| **`integrations/`**| `hermes/` | Hermes Agent 与 MCP 协议桥接：对外提供任务调度能力 | 依赖 `orchestrator/` |
| | `multica/` | Multica 协同看板：Issue 状态双向映射与轮询 worker | 依赖 `orchestrator/` |
| **`web/`** | `app.py`, `api.py` | FastAPI 仪表盘：提供 Web UI 交互、审批控制台与任务状态查询 | 依赖 `orchestrator/`、`storage/` |

---

## 3. 核心接口与数据流契约

### 3.1 研发产物传递契约 (`DevelopmentArtifacts`)
研发 Agent 完成工作后，只允许向独立复核传递只读结构化证据：
- `task_id`: 任务唯一标识符
- `commit_sha`: 本次开发的 Git 检查点 SHA
- `base_ref`: 工作树拉出基线 SHA
- `git_diff`: 经过安全脱敏后的变更 diff
- `test_passed`: 本地测试运行结果（布尔值）
- `test_summary`: 测试输出详情（已脱敏与截断）
- `tree_hash`: 验证测试未篡改工作区状态的树哈希

### 3.2 独立复核裁决契约 (`ReviewVerdict`)
独立复核 Agent 仅以只读方式审查上述证据，返回结构化裁决：
- `approved`: 是否批准通过（`True` / `False`）
- `severity`: 风险等级（`"P0"`, `"P1"`, `"P2"`, `"P3"`）
  * `P0`：严重安全违规、破坏性改动或数据丢失风险 -> **立即阻断，挂起等待人工授权审批**
  * `P1`：核心功能缺陷、测试未覆盖关键分支 -> **触发有界自修或升级**
  * `P2` / `P3`：普通代码风格或次要优化 -> 批准通过或作为建议记录
- `summary`: 裁决原因摘要
- `critical_issues`: 严重问题清单
