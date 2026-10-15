# AI Bot 团队基础选型

评估日期：2026-10-14（北京时间）。

## 结论

选择 **NousResearch/Hermes Agent 作为 Bot 团队宿主**，把现有 AgentCooperation 接成受控研发后端。

目标是延续当前项目，建立多个长期 Bot 的分工、记忆和协作。此结论基于现有项目源码及候选官方文档；两个候选尚未进行实际安装、模型调用或端到端联调，不代表性能或稳定性测试结果。

## 对比

| 选择因素 | DeepSeekBot | Hermes Agent |
| --- | --- | --- |
| 团队体验 | 持久 Bot 身份、群聊、Inbox、独立 Assignment | 独立 Profile Bot、名册、群聊、私信、例行任务 |
| 运行基础 | DeepSeek Harness 插件；TypeScript / Cordis / DSH Session | Python Agent 核心；CLI、Gateway、桌面与插件 |
| 长期记忆 | 普通 Git 工作树，文件及变更历史可检查 | 核心记忆文件、会话检索、可扩展记忆 Provider、技能积累 |
| 当前项目的接入 | 需要 DSH 侧插件或工具与 Python 后端桥接 | 可通过 Python 插件工具或 MCP 接入 Python 后端 |
| 本次判断 | 适合采用 DSH 产品体系，Git Memory 很有价值 | 更适合延续当前 Python 项目，同时建立长期 Bot 团队 |

Hermes 也已有多 Bot 协作，不能把它描述为只有单助手。语言一致有利于集成，但不意味着可以直接替换现有编排器。

## 建议分工

- Hermes 管理队长、需求、研究等长期 Bot，以及聊天、记忆、技能和日常例行任务。
- AgentCooperation 管理研发任务、授权仓库、任务 worktree、费用预留、测试、独立审查、P0 等待批准和 Codex 补审。
- Bot 通过受限工具提交任务、查询状态和读取产物。任务是否完成以编排器状态和验收证据为准。
- 每个研发任务固定使用 GLM 或 DeepSeek 其中一个；规划、开发、修复复用该引擎。群聊和自动 fallback 不能绕过此规则。

## 第一轮接入必须验证

1. **预算覆盖全部实际调用。** 现有调用前预留继续保留。Hermes 队长、群聊、子代理、压缩及后台学习可能产生额外调用，也需要纳入预算。官方文档明确 observer / middleware 回调异常会记录警告并继续运行，因此不能把“回调抛错”当作硬预算拦截。需要在实际模型请求边界或受控代理验证：预算拒绝后没有真实模型请求。
2. **提交幂等且可以恢复查询。** 当前 `run_task` 每次生成新任务 UUID；Web job 映射是内存状态。增加持久的外部请求 ID 到任务 ID 映射，避免 Bot 重试导致重复任务及重复费用。
3. **仓库与执行策略受控。** 给 Bot 的工具限定授权仓库及测试策略，不直接暴露任意 `repo_root`、`test_command` 输入。
4. **聊天协作与研发验收一致。** Bot 回复“完成”不改变研发任务状态；只有现有测试与独立审查通过，才能进入 SUCCESS。

当前策略默认独立审查引擎为 `antigravity_cli`，README 的 Gemini 默认描述需另外核对；本次选型未修改该策略或说明。

## 本地证据入口

- `orchestrator/core.py`：任务执行、调用预算、测试及审查流程。
- `orchestrator/engine_selector.py`：单任务 Flash 引擎选择。
- `orchestrator/budget_guard.py`：费用预留与预算边界。
- `orchestrator/fsm.py`：任务状态及验收规则。
- `execution/worktree.py`：隔离任务工作树。
- `integrations/multica/bridge.py`：可参考的外部任务桥接模式。
- `web/api.py`、`web/runner.py`：任务提交、查询与当前 job 生命周期。

## 候选官方资料

- [DeepSeekBot 项目说明](https://github.com/BotHarness/DeepSeekBot)
- [DeepSeekBot 架构](https://github.com/BotHarness/DeepSeekBot/blob/main/docs/architecture/botharness-architecture.md)
- [Hermes Agent 仓库](https://github.com/NousResearch/hermes-agent)
- [Hermes Bot Mode](https://hermes-agent.nousresearch.com/docs/user-guide/bot-mode)
- [Hermes 架构](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)
- [Hermes 插件接口](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)
- [Hermes 记忆](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
- [Hermes middleware 异常行为](https://hermes-agent.nousresearch.com/docs/developer-guide/middleware)

本轮完成选型评估，尚未实施 Hermes 接入或迁移。
