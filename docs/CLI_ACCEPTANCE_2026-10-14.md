# Hermes CLI 接入验收与修复记录（2026-10-14）

结论：**离线接入契约与边界修复验收通过，真实调用待验证**。
所有 6 项阻断性问题（真实 Codex 协议解析、专家 Bot 宿主 MCP 隔离、Antigravity 进程统一回收与事件校验、真实增量流式输出、会话工作目录绑定、敏感信息脱敏与显式 mock 安全）已全部修复并通过离线回归覆盖。
根据 [Google Antigravity 官方 FAQ](https://antigravity.google/docs/faq)，消费级套餐不支持第三方应用包装 `agy`，研发 Bot (`coop-dev`) 保持政策性拦截；Codex 专家 Bot (`coop-expert`) 与 GLM 复核 Bot (`coop-review`) 均维持严格只读，真实订阅与模型调用未发起、未消耗额度。

## 一、验收与测试结果

- **测试套件**：`tests/test_hermes_bot_setup.py` (10 项 Profile 安装与基线安全) + `tests/test_hermes_cli_channels.py` (19 项 CLI 协议/流式/隔离/目录专项测试) 全部通过（**29 passed**，耗时 ~6.8 秒）。
- **宿主配置目录**：`C:\Users\SPcuteAG\AppData\Local\hermes`。使用 `scripts/configure_hermes_bots.py --check` 校验完全一致（三 Bot 配置、元数据、工具隔离均与本地文件完全一致）。
- **未发起任何外部网络请求与模型调用**：不消耗 OpenAI/Gemini/GLM 真实额度，不接触或复制 OAuth 凭据，符合离线安全红线。

## 二、六项阻断问题的修复与落地

### 1. Codex app-server 真实协议解析（已修复）
- **依据**：本机 `codex.exe 0.162.0` 生成的标准 JSON Schema 以及 Hermes 原生客户端事件结构。
- **改动**：
  - 处理 `item/agentMessage/delta`：直接读取 `params.delta`（字符串），废弃旧代码中的 `delta.get("text")`。
  - 处理 `item/completed`：正确提取 `params.item.text`，并标记 `final_text_seen`，避免增量文本与最终文本重复拼接。
  - 处理 `turn/completed`：校验 `params.turn.status`（若为 `"failed"` 或包含 `error` 则抛出 `RuntimeError`），拒绝空响应或失败轮次包装成成功。
  - 覆盖失败轮次、取消、空响应拦截，确保不产生误导性空输出。

### 2. 专家 Bot 只读与宿主 MCP 权限隔离（已修复）
- **改动**：
  - 动态读取本机 `~/.codex/config.toml`（或 `$CODEX_HOME/config.toml`）中配置的 MCP 列表（如本机存在的 `node_repl`、`cua_repl`）。
  - 在 CLI 启动参数中为每个宿主 MCP 强制注入 `-c mcp_servers.<name>.enabled=false`，并附加 `--disable plugins`、`-c sandbox_mode="read-only"` 与 `-c approval_policy="never"`。
  - 协议层精确匹配响应结构：
    - `item/commandExecution/requestApproval`、`item/fileChange/requestApproval`、`execCommandApproval`、`applyPatchApproval`：返回 `{"decision": "decline"}`；
    - `mcpServer/elicitation/request`：返回 `{"action": "decline", "content": None, "_meta": None}`；
    - `item/permissions/requestApproval`：返回 `{"permissions": {"fileSystem": None, "network": None}, "scope": "turn"}`；
    - 其他 tool_call 请求：返回 JSON-RPC MethodNotFound (-32601)；
    - 收到 `mcpToolCall` 或其他副作用工具启动/完成事件时，主动抛出 `CodexSecurityViolationError`。
- **验证**：离线单测构造宿主 `config.toml` 中包含 `node_repl` 与 `filesystem_mcp`，断言生成的启动参数中包含显式禁用参数，且审批与调用请求全部被安全拦截。

### 3. Antigravity 事件判定、统一回收与退出等待（已修复）
- **改动**：
  - 事件处理循环加入 `isinstance(ev, dict)` 校验，安全忽略 JSON 数组（`[]`）和非字典畸形事件。
  - 严格校验 `result` 事件的 `status` 与 `error` 字段；即使进程返回退出码 0，只要 `result.status == "error"` 或缺少有效结果，立即抛出分类异常。
  - 若整个进程执行完毕未产出任何有效事件，即使退出码为 0 也抛出 `RuntimeError`，杜绝空响应冒充成功。
  - 收到有效 `result` 后，给予进程有界优雅退出等待（`proc.wait(timeout=5.0)`），若超时才调用 `kill_process_tree`，避免因进程尚未立即退出而误报超时。
  - 全局加入 `try...finally:` 确保活动进程句柄无论在异常、取消或超时下均被级联终止，并从 `_active_processes` 字典中清除。

### 4. 真实增量流式输出与中途取消（已修复）
- **改动**：
  - 两端 Provider 的 `stream=True` 实现均废弃了旧的“等整轮收集完再用 helper 包装成流”的做法。
  - `codex-cli`：在 JSON-RPC 消息循环中，一旦收到 `item/agentMessage/delta` 即刻生成 `ChatCompletionChunk` 并 `yield`；在生成器的 `finally` 中保证关闭 app-server 客户端并释放资源。
  - `antigravity-cli`：在接收 `step_update` 中的增量文本时，实时从队列获取并 `yield` `ChatCompletionChunk`。
  - 测试用例使用可挂起的事件生成器/延迟写入，严格断言首个 chunk 在后续事件送达前已被消费端接收，并断言取消后子进程被正确回收。

### 5. 会话工作目录（Session CWD）动态绑定（已修复）
- **改动**：
  - 实现 `resolve_effective_cwd()`，读取 Hermes 当前 Agent 会话的 `runtime_cwd`（优先读取 `agent.runtime_cwd.resolve_agent_cwd()`，其次读取 `kwargs` / `extra_params` 中的会话目录）。
  - 支持带空格目录（如 `E:\My Workspaces\Agent Repo`）及跨磁盘路径。
  - 验证多会话并行执行时各自固定独立工作区，杜绝使用全局默认宿主当前目录或相互污染。

### 6. 脱敏与显式 Mock 安全（已修复）
- **改动**：
  - 移除环境隐式 mock 判定，严格要求调用方显式传递 `mock_mode=True` 或特定测试构造对象，防止正式运行时被遗留环境变量意外劫持。
  - Codex 错误和异常抛出前强制执行 `redact_sensitive_text(..., force=True)`，即使 stderr 夹带密钥，在日志与异常消息中也会被自动遮罩（如 `sk-pro...6789`）。
  - 更新启动脚本与文档提示：明确指出消费级套餐阻断原因，不误导为“设置环境变量即代表合规通过”。

## 三、当前状态矩阵

| 模块 / 角色 | 离线契约与测试 | 权限隔离与安全 | 生产/真实模型调用 | 状态说明 |
| --- | --- | --- | --- | --- |
| `coop-dev` (Antigravity CLI) | 通过 (19+10 测试) | 工作区受限，进程树自动回收 | 保持拦截 | 消费级条款限制第三方包装；原生 Gemini API 已禁用。 |
| `coop-expert` (Codex CLI) | 通过 (19+10 测试) | 双重只读 + 宿主 MCP 禁用 | 待验证 | 离线契约完备；真实订阅额度与调用未接入。 |
| `coop-review` (GLM 5.3 FlashX) | 通过 (10 测试基线) | 工具列表为空，独立只读 | 已配置待触发 | 密钥独立配置在 profile 中，本轮离线测试未消耗额度。 |

当前阶段：**离线接入验收通过，真实调用待验证**。

