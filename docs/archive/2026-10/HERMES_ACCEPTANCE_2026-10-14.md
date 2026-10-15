# Hermes 集成交付验收

验收日期：2026-10-14，北京时间。

## 结论

**正式使用验收不通过；模拟回归和基础入口通过。**

本次仅检查、运行隔离验证及编写本报告，未修改业务源码，未调用真实模型、发送外部消息或启动常驻服务。验收依据为当前工作区和用户提供的交付记录，不把交付声明直接作为证据。

## 已验证通过

- 全量回归：`python -m pytest tests/ -q -p no:cacheprovider`，369 passed、1 skipped，70.78 秒。
- 使用隔离测试账库、显式 PYTHONPATH，在主项目目录及 hermes-agent 目录执行实际 `coop budget`，均退出 0。
- 实际 MCP stdio initialize 和 tools/list 均成功，返回 9 项工具，进程退出 0。
- 源码已加入自动请求 mock/live 区分、自定义 key 部分指纹验证、预分配真实任务 ID、复核 SHA 解析、规划 worktree 隔离及 Slash 错误处理。

这些结果不覆盖旧账库升级、真实模型请求、长期 Bot profiles 或生产 Gateway。

## 阻断项及证据

### 1. 实际旧账库缺少新增列【实测】

`storage/models.py:128` 增加 `idempotency_records.mock_mode`，但 `storage/db.py:30` 仍只调用 create_all，无法升级已存在的表。

现有 `data/agent_orchestrator.db` 和 `data/agent_orchestrator.mock.db` 均缺该列。只读 ORM 查询复现：`OperationalError: no such column: idempotency_records.mock_mode`。正式提交及默认审批模式查询受影响。新建临时库测试无法覆盖这一问题。

应提供幂等迁移、历史执行模式的可信判定，以及保留任务、费用和审单的升级测试。历史模式不明时不能默认认定为正式任务。

### 2. 相对数据库覆盖配置失败【实测】

`storage/db.py:19` 引用 `settings.PROJECT_ROOT`，但 PROJECT_ROOT 是模块常量，不是 AppSettings 实例属性。

调用 `get_engine('.pytest_temp/acceptance_relative.db')` 复现 `AttributeError: 'AppSettings' object has no attribute 'PROJECT_ROOT'`。

### 3. 审批仍可把真实任务切换为模拟【源码确认】

`integrations/hermes/bridge.py:492` 优先采用审批请求的 mock_mode，只有未传时才读取持久记录；`orchestrator/core.py:442` 按该值创建开发和审查实例。

真实 P0 任务可以通过显式 mock_mode=True 使用模拟修复与模拟复核，仍写入 SUCCESS。任务执行模式必须在创建时持久绑定，审批不能修改。

### 4. 审批初次失败未进入统一异常处理【源码确认】

`orchestrator/core.py:438` 已保存 REPAIRING，而初次修复在 :467，完整循环的 try 到 :477 才开始。预算、模型、worktree 或取消异常可能留下 REPAIRING。

`integrations/hermes/bridge.py:535` 异常响应宣称 FAILED，但未保证数据库实际是该状态。应统一处理整个恢复过程，并返回持久状态。

### 5. 授权路径和执行路径不一致【源码确认】

`integrations/hermes/bridge.py:681-682` 与 :727-728 校验相对路径后，丢弃规范化结果，再按进程 CWD 解析路径。不同启动目录可能审核错仓库或越过已校验边界。实际执行必须使用 validate_repo_root 返回的 Path。

### 6. 直调 Codex 控制尚不完整【源码确认】

Expert 直调只检查 enabled/is_available，没有完整关联任务的次数控制、额度预占与释放、实际耗尽后的持久状态更新。选择 Codex Reviewer 时也未统一经过这些入口控制。不能声称已实现所有角色的调用次数硬控制。

### 7. 去重尚未实现进程恢复，指纹遗漏测试命令【源码确认】

`integrations/hermes/bridge.py:241` 对遗留 IN_PROGRESS 返回原任务，没有 worker 所有权、租约或中断识别；预分配 ID 不等于进程重启恢复。

自动 key 和自定义 key 校验遗漏 custom_test_cmd。改变验收命令仍可能返回旧 SUCCESS。应持久化影响验收语义的完整请求指纹，并定义中断、恢复及未知费用预留处理。

### 8. Gemini CLI 参数不兼容【本机解析源码确认】

`agents/gemini_reviewer.py:75` 添加 `--effort high`。项目优先使用的本机 Gemini CLI 为 0.63.0，其严格参数解析器没有 effort 选项；该路径会拒绝未知参数。

本机 Antigravity 支持 `--effort high`。不能把两个调用入口的参数当作同一契约；Gemini high 要通过实际入口支持的配置实现，并验证生效。

### 9. 固定型号和定价声明不完整【配置复现、官方资料核对】

- CODEX_EXPERT_MODEL 可以覆盖任意字符串；调用只要求非空，不能保证始终是 gpt-6.1-sol。
- GLM 模型改为 FlashX，但配置仍按 $0.15/$0.50 每百万 Tokens，默认汇率下是输入 1.0875 元、输出 3.625 元；国内官方 FlashX 单价为输入 2 元、输出 7 元，费用预留与结算均会低估。
- `deepseek-flash` 当前对应 V4.1 Flash；旧 V4 Flash 已退休，旧 ID 也路由至 V4.1，不能表述为严格锁定旧 V4。

资料核对日期：2026-10-14。

- [GLM 官方模型页](https://docs.z.ai/guides/vlm/glm-5.3-flash)
- [BigModel 官方定价](https://docs.bigmodel.cn/cn/guide/start/pricing)
- [DeepSeek 官方型号和定价](https://api-docs.deepseek.com/quick_start/pricing/)

## 下一阶段：Gemini 研发＋Flash 独立复核

先修复以上验收缺口，再迁移角色。Gemini high 负责规划、实现及全部修复；GLM FlashX 或 DeepSeek Flash 每任务选择一个，只读独立复核；Codex gpt-6.1-sol 保留专家和补审职责。

不能直接把 FlashApiAgent 的开发 execute 用作 reviewer，因为其会应用返回的文件块。复核适配器必须禁止写文件和工具执行，绑定完整提交 SHA、真实测试及可靠的变更证据，并严格验证结构化结论。

新增付费 Flash 复核必须纳入事前预算预留和事后结算。Gemini 订阅调用应单独记录用量、次数及额度，不能虚构 API 费用，也不能静默切换至 API 密钥计费。

所有入口、审批恢复、历史任务兼容、日志和 UI 必须同步迁移，不能只交换配置中的 primary/reviewer 标签。
