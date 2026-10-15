# 阶段四真实工具链探针与阶段五正式接入真实验收报告

更新时间：2026-10-15（北京时间）
执行主体：Gemini 3.8 Flash High（主力研发 Agent）

---

## 1. 验收概述

本报告记录 AgentCooperation 阶段四真实工具链探针与阶段五正式闭环接入的最终交付成果、安全加固与真实验收证据。
所有验收均在实际运行环境中使用真实模型完成，完全遵守：
- 不使用 Gemini API Key，不复制 OAuth 凭据，复用本机 Antigravity 订阅 CLI (`agy`)。
- 真实与 Mock 数据库严格物理隔离：Mock 运行无论是否传入 Session 均不得污染生产库。
- 零伪造模型 ID：DeepSeek 模型 ID 经由官方接口现场核验为 `deepseek-flash`。
- 交付门禁全面生效：真实文件写入、原子 SHA/diff 绑定、测试通过、测试后工作树洁净与树哈希核验、独立 Reviewer 结构化返回与预算物理结算。
- 安全隔离与边界披露：关闭任意命令执行参数漏洞，对生成测试增加沙箱边界审批门禁（`WAITING_APPROVAL`），脱敏敏感凭据并拦截敏感文件修改。

---

## 2. 实际模型 ID、调用渠道与外部依赖状态

| 角色 | 真实模型 ID | 调用渠道 / Provider | 认证机制 | 状态与可用性 |
| :--- | :--- | :--- | :--- | :--- |
| **主力研发 Agent** | `gemini-3.8-flash-high` | 本机 Antigravity CLI (`agy.exe`) | 本机用户订阅 Session，无需 API Key | **已验证 (HTTP/CLI OK)** |
| **默认独立复核** | `glm-5.3-flashx` | 智谱开放平台 API (`https://open.bigmodel.cn/api/paas/v4`) | `ZHIPU_API_KEY`，输入 ¥0.5/M，输出 ¥0.5/M | **已验证 (HTTP 200 OK，默认首选)** |
| **可选独立复核** | `deepseek-flash` | DeepSeek 官方 API (`https://api.deepseek.com/v1/models`) | `DEEPSEEK_API_KEY`，输入 ¥1.0/M，输出 ¥2.0/M | **历史真实跑通；当日因服务商故障记录为 BLOCKED_EXTERNAL / DEGRADED** |
| **疑难专家** | `gpt-6.1-sol` | Codex / OpenAI 专家渠道 | 备用诊断与降级复核通道 | 保留 |

> **外部服务商状态真实披露（DeepSeek）**：  
> 2026-10-15 验收期间 DeepSeek 服务商出现外部 API 故障。系统事实记录为 `BLOCKED_EXTERNAL` / `DEGRADED`，绝不采用 Mock 冒充实时验收。历史已于 2026-10-15 06:33:23 真实跑通并结算 Task 32。当前生产与默认复核固定切换为 `glm-5.3-flashx`。

---

## 3. Hermes 调用入口与Profile集成

已通过 `scripts/configure_hermes_bots.py` 为本机 `coop-dev` Profile 启用 `agent_cooperation` 插件：
- `plugins.enabled`: `["agent_cooperation"]`
- `plugins.disabled`: `[]`
- 工作区锁定为 `E:\AgentCooperation`，保留原生 Antigravity CLI 安全边界。

提供以下四个统一入口并全部完成分发校验：

1. **Hermes 斜杠命令 (Slash Command)**:
   - 命令：`/coop loop <需求描述> [--repo <path>] [--test-path <rel_path>] [--mock]`
   - 说明：Windows 平台支持无须转义的反斜杠路径，直接调用 Bridge。
2. **Hermes 命令行 (CLI Command)**:
   - 命令：`hermes coop loop <需求描述> [--repo <path>] [--test-path <rel_path>] [--mock]`
   - 说明：由本地命令处理器直接调用阶段五闭环，已解决事件循环冲突。
3. **Hermes MCP Server**:
   - 工具名：`coop_run_gemini_closed_loop`
   - 参数：`requirement`, `repo_root`, `reviewer_choice`, `test_paths`, `mock_mode`, `max_repair_attempts`
   - 分发位置：`integrations/hermes/mcp_server.py`
4. **独立自动化脚本 (Python CLI)**:
   - 命令：`python scripts/gemini_closed_loop.py "需求描述" [--reviewer glm|deepseek] [--test-path tests/test_x.py] [--mock]`
   - 具备环境感知：自动解析并加载本地 `hermes-agent` 运行时包，无需依赖宿主偶然存在的 `PYTHONPATH`。

---

## 4. 安全防护与交付门禁实现

### 4.1 关闭任意命令执行漏洞
- 从 Hermes Plugin、MCP 和模型可控参数中彻底废除 `custom_test_cmd` / `--test-cmd`。
- 替换为安全可重复参数 `--test-path` / `test_paths`。
- **路径校验器**：
  - 必须为工作树内相对路径，拒绝绝对路径（如 `C:/Windows/...` 或 `/etc/passwd`）。
  - 严厉拦截路径穿越（`..`）与符号链接/目录联接（Symlink / Junction）逃逸。
  - 拦截任意 pytest 选项参数（如 `-p`, `--rootdir`, `--confcutdir`）与 Shell 操作符（`;`, `&`, `|`, `$`, `` ` ``, `<`, `>` 等）。
  - 文件必须在隔离工作树中真实存在。
- 进程启动使用参数数组直接执行，不经过 Shell 解释器。

### 4.2 测试子进程隔离与环境清理
- **敏感凭据剥离**：彻底清除子进程环境变量中的敏感 Key、Token、Password、Credential（覆盖 `OPENAI`, `ZHIPU`, `DEEPSEEK`, `AWS`, `GITHUB` 等）。
- **网络代理清理**：清除子进程的 `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`。
- **Python / Pytest 隔离**：设置 `PYTHONNOUSERSITE=1`（阻止加载用户个人 site-packages），`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`（禁止自动加载外部不受信 pytest 插件）。
- **临时目录沙箱化**：将子进程的 `TEMP`, `TMP`, `USERPROFILE`, `HOME` 重定向到工作树下的隔离沙箱目录 `.test_sandbox`。

### 4.3 未受信测试代码的操作系统隔离边界与审批门禁
- **边界说明**：Windows 当前原生环境无法提供强隔离的轻量容器/OS-level Sandbox。系统**绝不虚假宣称**“可安全自动执行任意生成测试代码”。
- **审批门禁生效**：当 Gemini 生成或修改了测试用例代码，且未显式指定 `--allow-untrusted-tests` 时，系统自动中断并进入 `WAITING_APPROVAL` 状态，提示人工审核测试源码，防范生成代码包含潜在提权或越权行为。

### 4.4 敏感文件修改拦截与证据脱敏
- **敏感文件拦截**：工作树文件写入一旦触及 `.env`, `.git`, `.aws`, `.codex`, 证书私钥（`*.pem`, `*.key`）等，任务直接以 `FAILED` 终止并告警。
- **证据严格脱敏**：上报证据与 Reviewer 提示词前，自动脱敏 AWS Access Key、GitHub Token、RSA/EC 私钥、包含密码的数据库连接串。
- **长文本截断**：Diff 限制 40,000 字符，测试日志限制 80,000 字符，避免 Context 溢出。

### 4.5 原子提交与工作树树哈希绑定
- 交付门禁校验：
  1. 基线 Commit 与交付 Commit 必须不同（SHA 变化）。
  2. 工作树相对基线有且仅有非空 Git Diff。
  3. 执行测试前与测试后分别核验 HEAD SHA 与 `git write-tree` 生成的 Tree Hash，确保测试执行过程未发生自修改或代码污染。
  4. 最终证据生成物理不可篡改的 `evidence_id`，严格绑定 Commit SHA 与 Tree Hash。

---

## 5. 真实生产数据库与历史记录审计

生产数据库位置：`E:\AgentCooperation\data\agent_orchestrator.db`

### 真实生产闭环记录（真实模型、真实扣费）
- **Task 31** (2026-10-15 06:32:21):
  - 研发模型：`gemini-3.8-flash-high`
  - 复核模型：`glm-5.3-flashx`
  - 基线 Commit：`be60ef23840ee8e4879cc43b8fe51e042790866c`
  - 产出 Commit：`b5e7a5c6f345a211d2b33e0fdb8150fe98231036`
  - Token 消耗：Prompt 519, Completion 442
  - 结算费用：¥0.00217
  - 结果：`SUCCESS`
- **Task 32** (2026-10-15 06:33:23):
  - 研发模型：`gemini-3.8-flash-high`
  - 复核模型：`deepseek-flash`
  - 基线 Commit：`be60ef23840ee8e4879cc43b8fe51e042790866c`
  - 产出 Commit：`3bc4dc548633ed7061bbf04f208ebc046b11c4ac`
  - Token 消耗：Prompt 552, Completion 350
  - 结算费用：¥0.00213
  - 结果：`SUCCESS`

### 历史数据审计披露
- **Task 22 至 Task 28**：为早期在数据库物理隔离机制实施前运行演练时写入的历史测试数据。按照用户规则与审计要求，保留作为历史演练证据，不作人为篡改或删除。
- **物理隔离生效**：当前版本 `_is_production_session` 与 `mock_sqlite_db_path` 全面隔离，所有后续 Mock 演练均仅写入 `data/mock_agent_orchestrator.db`，真实生产库不再受任何测试污染。

---

## 6. 测试与回归验收结果

- **阶段五专项测试集**：
  - 执行命令：`python -m pytest tests/test_gemini_closed_loop.py -q -p no:cacheprovider`
  - 结果：`24 passed in 91.76s` (0 failed)
  - 涵盖测试：工具探针、原子 Commit 绑定、敏感文件拦截、参数逃逸拒绝、环境变量凭据清理、沙箱审批门禁、Slash 命令分发、MCP 分发、CLI 分发、递归测试发现等 24 项全套用例。
- **全量项目离线回归**：
  - 执行命令：`python -m pytest tests/ -q -p no:cacheprovider`
  - 结果：`579 passed, 28 skipped in 188.16s` (0 failed)

---

## 7. 验收结论

阶段四真实工具链探针与阶段五正式闭环接入已全面完成硬化与真实验收。所有阻断项全部清除，安全边界明确披露，数据库物理隔离与原子提交门禁完全生效，系统已具备正式投入研发闭环使用的条件。
