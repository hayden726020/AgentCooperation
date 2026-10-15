# AgentCooperation 工作指南

- 默认使用中文。保留用户已有改动，遵循会话中的授权和全局 ECC 规则；复杂任务按领域委派，代码改动安排独立复核。
- 首先按需阅读 `docs/CONTEXT.md`，再读取任务涉及的模块；不要预加载历史报告或完整第三方仓库。
- 主项目：`agents/`、`config/`、`execution/`、`orchestrator/`、`storage/`、`integrations/`、`web/`、`tests/`、`scripts/`。
- `hermes-agent/`、`DeepSeekBot/` 是独立本地仓库，`references/` 是参考子模块。只有任务涉及它们时才定向检查，并遵守各自规则。
- 自动流水线的单任务 GLM / DeepSeek 二选一规则仍有效；手动 Hermes Bot 的角色配置是另一入口。未经用户指示不迁移角色。
- 测试优先；代码改动运行相关离线回归。全量入口：`python -m pytest tests/ -q -p no:cacheprovider`。
- Mock 必须显式指定；模拟验证不能代表真实模型或生产验收。不得虚构测试结果、型号锁定或预算覆盖范围。
- `.env`、`data/`、`.worktrees/`、`.runtime/`、`.integration/` 和本机 Bot Profiles 有实际用途。清理只删除确认可再生的缓存；历史证据归档。
- 不输出凭据。涉及 API、外部消息、长期服务或费用时按用户授权执行，文档整理不触发这些操作。
- 当前摘要在 `docs/CONTEXT.md`；Bot 使用见 `docs/HERMES_BOTS.md`；历史证据在 `docs/archive/`，不作为当前结论。
