# 当前项目上下文

更新：2026-10-15（北京时间）。本页是本地源码摘要；模型、价格、账号和服务商条款不在本次整理中重新核验。

## 两种运行方式

| 入口 | 当前实现 |
| --- | --- |
| 自动编排 CLI / Web / Hermes bridge | 每任务 GLM 或 DeepSeek 一个 Flash 引擎负责规划、开发、修复；默认 Antigravity CLI 复核；Codex CLI 专家和补审。以 `config/policy.yaml`、`orchestrator/engine_selector.py` 为准 |
| 手动 Hermes Profiles | `scripts/configure_hermes_bots.py` 配置 Gemini dev、GLM review、Codex expert；关闭 agent_cooperation 插件和 MCP。这不代表自动流水线已迁移角色 |
| Hermes 内部 Gemini 实验对话 | `chat coop-dev` 经注册 Provider / AIAgent 调用订阅 CLI；本机已显式启用，真实短回复通过。每请求隔离原生 HOME / 工作目录、禁用工具，继承现有系统代理；不使用 Gemini API 密钥。仅文本对话，研发工具闭环未实现 |
| Gemini 研发 + 独立复核闭环（阶段五） | `scripts/gemini_closed_loop.py` / `orchestrator/gemini_closed_loop.py` / Hermes 插件 `coop_run_gemini_closed_loop`：Gemini 负责工作树内开发与修复；GLM / DeepSeek 担任只读独立复核；实现任务交接、产物传递（commit SHA / diff / test）、结论回传与有界失败重试（P0 拦截审批）。已完成阶段四真实工具探针与阶段五正式接入真实验收 |
| 用户直接使用 Gemini 研发 | `scripts/hermes_bots.ps1 direct coop-dev` 前台启动原生 Antigravity CLI，启动参数指定 `gemini-3.8-flash-high / high`，复用原生登录；任务文件与 diff / 测试证据人工交接，不经过 Hermes 模型 Provider，不自动调用复核 |


测试和独立复核均通过才能 SUCCESS。P0 保留审批门槛；真实与 Mock 账库分开 (`data/agent_orchestrator.db` 与 `data/mock_agent_orchestrator.db`)。Hermes 原生对话调用未统一纳入编排器 BudgetGuard。

## 按任务读源码

| 任务 | 入口 |
| --- | --- |
| 生命周期、研发上下文 | `orchestrator/core.py`、`orchestrator/fsm.py` |
| 引擎与独立复核 | `agents/`、`orchestrator/engine_selector.py` |
| 策略、预算、Codex 额度 | `config/policy.yaml`、`config/settings.py`、`orchestrator/budget_guard.py`、`orchestrator/codex_guard.py` |
| 工作树、测试、进程 | `execution/worktree.py`、`execution/test_runner.py`、`execution/subproc.py` |
| 账库 | `storage/db.py`、`storage/models.py` |
| Hermes、Multica、Web | `integrations/hermes/`、`integrations/multica/`、`web/` |
| Bot 设置 | `scripts/configure_hermes_bots.py`、`scripts/hermes_bots.ps1`、`docs/HERMES_BOTS.md` |
| Gemini 研发与复核闭环 | `orchestrator/gemini_closed_loop.py`、`agents/flash_reviewer.py`、`agents/gemini_dev_agent.py`、`scripts/gemini_closed_loop.py` |


## 验收边界与待办

- **阶段四真实工具链探针**：2026-10-15 通过 `scripts/probe_hermes_gemini_tools.py` 验证完成（5 原生工具步骤，`HERMES_GEMINI_PROJECT_OK`）。
- **阶段五正式闭环接入**：2026-10-15 完成三次真实验收（真实工具探针、Gemini + GLM FlashX、Gemini + DeepSeek Flash，全部 SUCCESS 并物理结算）。
- **模型渠道与验证**：研发模型固定为 `gemini-3.8-flash-high`；复核模型固定为 `glm-5.3-flashx`；可选复核 `deepseek-flash` 已经官方模型列表核验通过；专家保留 `gpt-6.1-sol`。
- **全量离线回归**：571 passed, 28 skipped (0 failed)。
- 历史旧库与旧流水线说明见归档验收报告；阶段五最新真实数据详见 [阶段五真实验收报告](STAGE5_ACCEPTANCE_2026-10-15.md)。

## 上下文读取规则

`_project_context` 总上限 24,000 字符，单文件最多 8,000 字符，超过 128,000 字节的文件跳过；先 AGENTS、本页、README，再源码和配置，最后补充 Markdown。排除点目录、参考仓库、`docs/archive/`、嵌套 Git 仓库及链接；保留凭据文件过滤。

`execution.max_context_tokens` 尚未接入该字符预算。新任务工作树从 HEAD 创建，未提交整理不会自动进入新工作树，已有工作树也不会自动更新。

## 文档入口

- [快速使用](../README.md)
- [阶段五真实验收报告 (2026-10-15)](STAGE5_ACCEPTANCE_2026-10-15.md)
- [Hermes Bot 说明](HERMES_BOTS.md)
- [历史报告及整理前 README](archive/README.md)

后续更新本页时记录源码变化；不要复制历史修复清单或宣称未验证的生产能力。
