# 阶段四真实工具链探针与阶段五正式接入真实验收报告

更新时间：2026-10-15（北京时间）
执行主体：Gemini 3.8 Flash High（主力研发 Agent）

---

## 1. 验收概述

本报告记录 AgentCooperation 阶段四真实工具链探针与阶段五正式闭环接入的最终交付成果与真实验收证据。
所有验收均在实际运行环境中使用真实模型完成，完全遵守：
- 不使用 Gemini API Key，不复制 OAuth 凭据，复用本机 Antigravity 订阅 CLI (`agy`)。
- 真实与 Mock 数据库严格物理隔离。
- 零伪造模型 ID：DeepSeek 模型 ID 经由官方接口现场核验为 `deepseek-flash`。
- 交付门禁全面生效：真实文件写入、原子 SHA/diff 绑定、测试通过、测试后工作树洁净核验、独立 Reviewer 结构化返回与预算结算。

---

## 2. 实际模型 ID 与调用渠道

| 角色 | 真实模型 ID | 调用渠道 / Provider | 认证机制 | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| **主力研发 Agent** | `gemini-3.8-flash-high` | 本机 Antigravity CLI (`agy.exe`) | 本机用户订阅 Session，无需 API Key | **已验证 (HTTP/CLI OK)** |
| **默认独立复核** | `glm-5.3-flashx` | 智谱开放平台 API (`https://open.bigmodel.cn/api/paas/v4`) | `ZHIPU_API_KEY`，输入 ¥0.5/M，输出 ¥0.5/M | **已验证 (HTTP 200 OK)** |
| **可选独立复核** | `deepseek-flash` | DeepSeek 官方 API (`https://api.deepseek.com/v1/models`) | `DEEPSEEK_API_KEY`，输入 ¥1.0/M，输出 ¥2.0/M | **已验证 (HTTP 200 OK)** |
| **疑难专家** | `gpt-6.1-sol` | Codex / OpenAI 专家渠道 | 备用诊断与降级复核通道 | 保留 |

> **注：关于 DeepSeek 模型 ID 核验**  
> 经调用 `https://api.deepseek.com/v1/models` 实测验证，DeepSeek API 当前暴露的准确官方模型 ID 为 `deepseek-flash` 与 `deepseek-v4-pro`。系统已在 `agents/flash_reviewer.py` 中更新映射，彻底消除了历史未经验证的名称隐患。

---

## 3. Hermes 调用入口与集成

阶段五已正式集成至 Hermes Agent 生态，提供以下四个统一入口：

1. **Hermes 插件工具 (Plugin Tool)**:
   - 函数名：`coop_run_gemini_closed_loop`
   - 参数：`prompt` (str), `reviewer_model` (str, 默认 `glm-5.3-flashx`), `run_tests` (bool, 默认 true), `auto_commit` (bool, 默认 true), `mock_mode` (bool, 默认 false), `timeout_seconds` (int, 默认 300)
   - 注册位置：`hermes-agent/plugins/agent_cooperation/__init__.py`
2. **Hermes 命令行与斜杠命令 (Slash Command)**:
   - 命令：`/coop loop <需求描述>`
   - 交互：直接在 Hermes CLI 对话中触发阶段五研发闭环。
3. **Hermes MCP Server**:
   - 工具名：`coop_run_gemini_closed_loop`
   - 分发位置：`integrations/hermes/mcp_server.py`
4. **独立自动化脚本 (Python CLI)**:
   - 命令：`python scripts/gemini_closed_loop.py --prompt "..." [--reviewer-model glm-5.3-flashx|deepseek-flash]`
   - 具备环境感知：自动解析并加载本地 `hermes-agent` 运行时包，无需依赖宿主偶然存在的 `PYTHONPATH`。

---

## 4. 三次最小真实验收全量记录

### 验收 1：阶段四 Antigravity 真实工具链探针
- **执行脚本**：`scripts/probe_hermes_gemini_tools.py`
- **执行模型**：`gemini-3.8-flash-high` (原生 Antigravity CLI)
- **执行结果**：`passed: true`, 退出码 0, 耗时 36.37s
- **原生工具执行序列 (5 步全部真实触发并记录)**：
  1. `view_file`: 读取隔离工作树内的哨兵目标文件
  2. `view_file`: 读取伴随测试用例
  3. `replace_file_content`: 原生就地修改哨兵文件中的计算函数
  4. `view_file`: 原生复核写入成果
  5. `run_command`: 在隔离工作树内真实运行 `pytest`
- **探针标记**：收到预期的 `HERMES_GEMINI_PROJECT_OK`
- **安全检查**：工作树环境、PreToolUse Hook 拦截策略及敏感路径（`.env`, `.git` 等）保护全部生效。

---

### 验收 2：Gemini 主力研发 + GLM FlashX 独立复核闭环
- **任务 ID**：`task_gemini_20261015_063221_1dd44209`
- **主力研发模型**：`gemini-3.8-flash-high`
- **独立复核模型**：`glm-5.3-flashx`
- **任务目标**：在隔离工作树中为哨兵计算模块新增 `calculate_efficiency` 函数并配套单元测试。
- **闭环运行状态**：`SUCCESS` (一次通过，0 次重试修复)
- **代码提交演进**：
  - 基线 Commit SHA：`4a3325cfa79a1f10ecb29bf927b20468301540bf`
  - 最终 Commit SHA：`b5e7a5c6f345a211d2b33e0fdb8150fe98231036`
- **测试命令与结果**：
  - 命令：`python -m pytest tests/test_sentinel_calc.py -q`
  - 结果：`1 passed in 0.08s`
  - 测试后工作树状态：洁净核验通过 (`is_clean == True`)
- **GLM FlashX 独立复核结论 (结构化 JSON)**：
  - `approved`: `true`
  - `severity`: `P3`
  - `summary`: "The change correctly adds `calculate_efficiency` with comprehensive test coverage and docstrings."
  - `suggestions`: ["Consider typing `returns float` explicitly if strict mypy is used."]
- **预算与存储记录**：
  - 扣除预留并实际结算至 `data/agent_orchestrator.db`
  - 月度预算支出更新正常。

---

### 验收 3：Gemini 主力研发 + DeepSeek Flash 独立复核闭环
- **任务 ID**：`task_gemini_20261015_063323_38e0d81f`
- **主力研发模型**：`gemini-3.8-flash-high`
- **独立复核模型**：`deepseek-flash`
- **任务目标**：在隔离工作树中实现 `calculate_efficiency` 算法并验证测试。
- **闭环运行状态**：`SUCCESS` (一次通过，0 次重试修复)
- **代码提交演进**：
  - 基线 Commit SHA：`4a3325cfa79a1f10ecb29bf927b20468301540bf`
  - 最终 Commit SHA：`3bc4dc548633ed7061bbf04f208ebc046b11c4ac`
- **测试命令与结果**：
  - 命令：`python -m pytest tests/test_sentinel_calc.py -q`
  - 结果：`1 passed in 0.08s`
  - 测试后工作树状态：洁净核验通过 (`is_clean == True`)
- **DeepSeek Flash 独立复核结论 (结构化 JSON)**：
  - `approved`: `true`
  - `severity`: `P3`
  - `summary`: "The patch successfully implements `calculate_efficiency` and corresponding unit tests."
  - `suggestions`: ["Edge case handling for negative output or zero input is well-covered."]
- **预算与存储记录**：
  - 扣除预留并实际结算至 `data/agent_orchestrator.db`
  - 数据库字段 `reviewer_model: deepseek-flash`，`mock_mode: 0`。

---

## 5. 数据库隔离与物理存储位置

系统对测试运行与生产运行进行了严格的物理库隔离：

| 运行模式 | 对应 SQLite 数据库路径 | 说明 |
| :--- | :--- | :--- |
| **真实生产任务** | `E:\AgentCooperation\data\agent_orchestrator.db` | 仅记录真实模型调用的任务，包含完整的 commit_sha、reviewer_model、test_passed 与预算结算记录。 |
| **Mock 测试任务** | `E:\AgentCooperation\data\mock_agent_orchestrator.db` | 自动化单元测试与 `--mock` 参数任务专属，绝不污染真实生产历史与预算统计。 |

---

## 6. 离线回归测试状态

全量离线回归测试已全部通过：
- **执行命令**：`python -m pytest tests/ -q -p no:cacheprovider`
- **执行结果**：`571 passed, 28 skipped in 122.72s` (0 failed)
- **阶段五专项测试集**：`tests/test_gemini_closed_loop.py` 全部 16 项覆盖测试通过。

---

## 7. 尚存限制与运维建议

1. **测试用例路径指定**：全量测试集当前包含 571 个测试，单次全量执行约需 2 分钟。为保证研发与修复闭环的高响应性，Prompt 或自动编排器应优先指定修改模块对应的局部测试文件（如 `tests/test_xyz.py`），避免无差别触发全量回归导致超时。
2. **P0 任务人工干预**：当独立 Reviewer 判定为 `P0` 时，系统按策略进入 `WAITING_APPROVAL` 挂起状态。必须由运维人员通过 Hermes `/coop approve <task_id>` 或 Bridge `approve_task` 触发恢复。
3. **原生 Antigravity CLI 依赖**：Gemini 研发 Agent 深度绑定宿主机 Antigravity 订阅 CLI 环境，后台守护进程需保持激活状态。
