---
trigger: always_on
description: 项目核心 Skills 强制执行规则（Mandatory Skills Policy）
---

# 项目核心 Skills 强制执行规则

在 AgentCooperation 项目中，所有开发任务与 AI 协作必须严格遵循以下 8 个核心技能规范：

1. **工作流与代码质量**：严格执行 `gsd-workflow`（以 `.planning/` 统领规划、原子任务拆解、TDD 铁律、根因调试、十步闭环清单）。
2. **上下文治理**：出现衰减时严格执行 `context-degradation` 排毒；长会话严格执行 `context-compression` 压缩并沉淀至 `.planning/STATE.md`。
3. **加载与检索优化**：严格执行 `context-optimization`（三级递进加载，严禁暴力扫描全库）与 `serena-retrieval`（符号级精准定位）。
4. **记忆与文件隔离**：严格执行 `memory-systems`（外部持久化长期记忆）与 `filesystem-context`（长日志外置、工作树物理隔离、凭据防泄露）。
5. **多 Agent 协作**：严格执行 `multiagent-patterns`（调度/研发/审查三权分立、只读证据交接、P0 风险人工审批阻断、超时防重确认）。
