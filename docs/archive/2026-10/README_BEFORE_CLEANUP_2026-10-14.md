# AgentCooperation

Python + SQLite 编排器：每个任务选择 **GLM Flash 或 DeepSeek Flash 其中一个**，由同一引擎负责规划、开发和修复；Gemini CLI 独立验收，Codex CLI 处理重要问题及延期补审。

单任务 Flash 二选一是用户指定的架构。所有阶段复用该实例，禁止中途串联另一 Flash 引擎。

## 固定模型配置

系统统一落实以下固定模型与推理档位配置，所有实际调用入口均强制传递指定模型与档位；指定模型或档位不可用时明确报错，不自动降级或换型号：

| 角色 / 引擎 | 固定配置 | 正式模型 ID / CLI 形式 | 说明 |
|---|---|---|---|
| **GPT/Codex 专家、独立复核、补审** | `gpt-6.1-sol` (Sol 6.1) | `gpt-6.1-sol` (`codex exec --model gpt-6.1-sol`) | 负责 P1 疑难故障专家修复升级与 Codex 延期补审 |
| **Gemini (含 Antigravity 独立复核)** | `high` 推理档位 | `gemini-3.8-flash-high` (`--effort high`) | 独立代码审查与客观验收判定，必须维持 high 档位 |
| **DeepSeek 主力开发引擎** | `v4flash` (用户指定) | `deepseek-flash` | 主力研发二选一，负责单任务规划、开发与常规修复 |
| **GLM 主力开发引擎** | `flashX` (用户指定) | `glm-5.3-flashx` | 主力研发二选一，负责单任务规划、开发与常规修复 |

- **单引擎生命周期复用**：每个研发任务在 GLM Flash 与 DeepSeek Flash 中严格二选一，任务全生命周期（需求拆解、代码生成、常规修复）复用同一引擎实例，禁止中途串联或切换。
- **角色流转与升级解耦**：测试三轮失败升级至 Codex 专家（`gpt-6.1-sol`）、提交独立复核（Gemini `high` 档位）按现有策略执行，不改变主力开发引擎。
- **无静默降级**：配置、默认值与环境变量示例已全面移除旧模型及 `low` 档位残留默认值；若服务商或命令行不可用直接抛出硬异常。
- **行为测试覆盖**：使用替换模型调用边界的行为测试覆盖实际请求参数与配置优先级，不擅自产生真实付费调用。

## 当前交付规则

- 每次模型调用检查成功状态、实际文件输出与预算；缺密钥立即报错，模拟必须显式开启。
- 测试失败最多进行三轮常规修复。审查拒绝同样需要修复和重新测试。
- 专家补丁和额度耗尽后的替代修复都返回测试、独立复核；测试及审查通过才能进入 `SUCCESS`。
- P0 进入 `WAITING_APPROVAL`，保留严重级别和专家意见，停止自动应用专家补丁。
- Codex 禁用、保留状态、恢复中、调用次数耗尽与确认用量耗尽分别处理。一般 429/网络故障不标为周额度耗尽。
- Codex 耗尽时保存完整问题、最终提交、diff、测试和 Gemini 结果；通过验证的 P1 成果可带补审标记完成。
- 补审仅领取已完成或暂停任务。真实审查在调用前后验证完整提交 SHA 及干净工作区；失败保留记录，发现问题标为 `NEEDS_REPAIR`，明确批准后才关闭。

## 安装与配置

需要 Python 3.11+、Git；真实审查需要已安装并登录的 Gemini CLI 和 Codex CLI。

```bash
pip install -r requirements.txt
```

在项目根目录的 `.env` 中配置选定引擎的密钥：

```ini
GLM_API_KEY="your-key"
DEEPSEEK_API_KEY="your-key"
GEMINI_CLI_CMD="gemini"
CODEX_CLI_CMD="codex"
```

也支持 `FLASH_API_KEY` / `FLASH_BASE_URL` 通用覆盖。密钥文件不会由模型生成流程修改。

模型 ID、端点、费率、输出上限和预算位于 `config/policy.yaml`。发布环境必须验证账号可用模型与实际费率。当前价格是可配置的估算参数；DeepSeek 按高峰上界保守记账，缓存折扣没有抵扣预留。

目标 Git 项目必须已经有初始提交，并已配置本地提交身份。程序不会自动暂存并初始化用户主仓库。

## 使用

在编排器目录运行，指定要开发的目标项目：

```bash
python -m orchestrator.cli run --repo-root /path/to/target-repo --task "实现缓存模块并编写行为测试" --model glm
python -m orchestrator.cli run --repo-root /path/to/target-repo --task "实现二叉搜索树并测试" --model deepseek
```

不指定 `--model` 时使用策略中的 `agents.primary`。

```bash
python -m orchestrator.cli run --repo-root /path/to/target-repo --task "演练调度流程" --mock
python -m orchestrator.cli status
python -m orchestrator.cli tasks
python -m orchestrator.cli status --mock
python -m orchestrator.cli tasks --mock
```

模拟运行生成固定演练代码，只验证调度链路。模拟账库为原数据库名加 `.mock`，例如 `data/agent_orchestrator.mock.db`；模拟不能恢复真实额度或关闭真实审单。历史旧版本账本可能含有模拟费用，升级不会删除历史记录。

任务失败、预算停止、等待批准或等待审查时，命令返回非零退出码。

默认测试纳入目标已有 `tests/`、根目录 `test_*.py` / `*_test.py` 及新生成的测试。支持 pytest、unittest、Jest/Vitest、Mocha、Node TAP 统计。零测试、全跳过、全预期失败、无法确认测试执行的输出不会通过验收。目标有特殊测试入口时可以指定：

```bash
python -m orchestrator.cli run --repo-root /path/to/target-repo --task "修复边界错误" --test-command "npm test"
```

## Multica 任务看板与协作集成

支持作为执行引擎无缝接入开源 AI 团队看板平台 [Multica](https://github.com/multica-ai/multica)。实现 Issue 自动认领、工作树隔离研发、真测试验证、Gemini 独立复核及结果/评论回写：

```bash
# 1. 运行单个 Multica 任务并回写状态与评论报告
python -m orchestrator.cli multica-run --issue-id ISS-101 --title "缓存模块实现" --task "实现带有TTL的缓存" --mock

# 2. 查看看板当前任务状态与执行人
python -m orchestrator.cli multica-board

# 3. 启动后台 Worker 自动轮询认领待办 Issue
python -m orchestrator.cli multica-worker --interval 10 --mock
```

支持内存/本地文件看板模式（`MemoryMulticaClient`）、官方 CLI 调用（`CliMulticaClient`）及远程服务 REST API 模式（`HttpMulticaClient`）。

## Hermes Agent 宿主与多 Bot 协作集成

本项目深度接入 [Hermes Agent](https://github.com/NousResearch/hermes-agent)，以 Hermes 作为多 Bot 宿主与用户交互统一入口。在 Hermes 侧提供原生插件（`hermes-agent/plugins/agent_cooperation/`）、会话 Slash 命令、Operator CLI 及标准 MCP 服务（`integrations.hermes.mcp_server`），完整复用 AgentCooperation 现有核心编排能力。

### 1. 多 Bot 角色与能力分工

- **Dev Bot (主力研发)**：单任务二选一（`glm-5.3-flashx` 或 `deepseek-flash`），单任务生命周期复用同一引擎执行规划、代码编写与常规修复。
- **Reviewer Bot (独立复核)**：基于 Google Antigravity / Gemini CLI（`gemini-3.8-flash-high`，`--effort high`），对变更代码进行客观独立审查与严格验收判定（P0/P1/P2/P3）。
- **Expert Bot (Codex 架构专家)**：基于 `gpt-6.1-sol`，承接复杂 P1 架构故障与升级修复，配合额度状态机支持优雅降级与延期补审。
- **Coordinator (编排器适配层)**：桥接受权 Git Worktree 隔离、自动化测试验收、调用前硬预算预留与实际用量结算。

### 2. 六大架构加固与生产级保障

为保证生产环境可靠性，集成了 6 项核心架构安全防护：

1. **P0 审批严密受控，不绕过质量验收**：
   - 当独立复核发现 P0 级别严重风险时，任务状态置为 `WAITING_APPROVAL`；
   - 人工调用 `approve_task` 批准仅代表流程授权，状态机受控转移至 `REPAIRING` / `TESTING`，必须继续执行必要修复、真实测试执行与独立复核；
   - 只有测试通过且审查合格才能最终进入 `SUCCESS`；人工拒绝则直接标记为 `FAILED`，绝不直接将数据库状态改为成功。
2. **模拟与正式请求去重隔离及幂等安全**：
   - 自动生成去重键时严格加入 `mock_mode` 标识（`mock:hash` 与 `live:hash` 物理隔离），杜绝模拟成功记录被正式任务复用；
   - 自定义去重键验证请求指纹（仓库根目录、任务描述、指定引擎、模拟模式），指纹不匹配立即拒绝；
   - 提交时预分配唯一的真实 UUID `task_id` 并先行落库，避免 `"pending"` 占位导致的唯一键冲突、无法并发查询与进程重启后丢失；
   - 预算拒绝的记录在账户额度恢复后自动清理，允许后续发起合理重试。
3. **配置与数据库的启动目录完全无关 (CWD Independence)**：
   - 通过 `PROJECT_ROOT = Path(__file__).resolve().parents[1]` 绝对锚定主项目根目录；
   - `.env` 凭据、`config/policy.yaml` 策略文件与 SQLite 数据库（`data/agent_orchestrator.db`）无论在主项目根目录还是从 `hermes-agent` 目录启动均一致解析；
   - 允许通过 `AGENT_COOPERATION_ENV_FILE`、`AGENT_COOPERATION_POLICY_PATH`、`AGENT_COOPERATION_DB_PATH` 显式覆盖，不静默使用错误目录，不打印明文密钥。
4. **独立复核严格绑定完整 Git Commit SHA**：
   - `run_role_reviewer` 在进入审查前调用 `git rev-parse HEAD` 获取 40 位标准 Git SHA，满足 Antigravity / Codex 审查器的严格 SHA 校验；
   - 保持审查调用前后的提交一致性与工作区干净检查。
5. **直调角色安全控制边界与预算边界明确**：
   - Reviewer / Expert 直调入口强制执行 `validate_repo_root()` 白名单校验；
   - Expert 直调受 `policy.codex.enabled`、额度状态机与调用次数硬控制；
   - Dev Plan 直调自动在隔离的 Git Worktree 中执行，使用防冲突唯一任务 ID，并在完成后记录终态（`SUCCESS` / `FAILED`），绝不污染主仓库；
   - **预算覆盖边界声明**：AgentCooperation 的 `BudgetGuard` 在 API 调用前在 SQLite 事务中完成硬预算预留与用量结算，覆盖编排流水线与直调角色；Hermes 自身的原生单聊、群聊、子代理、上下文压缩及后台定时任务属于 Hermes 内部运行时，不计入 AgentCooperation 的模型预算。系统不依赖易被忽略的 observer/middleware 回调抛错，而是在桥接层入口实施硬预算拦截。
6. **Slash 与 CLI 命令鲁棒性**：
   - `commands.py` 补齐 logger 实例；
   - 捕获未闭合引号异常，使用 `SafeArgumentParser` 拦截 `argparse` 的 `SystemExit`，防止异常输入导致 Hermes 宿主退出；
   - 修正 `list --limit <n>` 参数解析，异常均返回友好提示。

### 3. 操作与调度指南

可以在任意工作目录下（包括 `E:\AgentCooperation` 或 `E:\AgentCooperation\hermes-agent`）运行：

```bash
# --- 方式 1: 在 Hermes 终端使用 Operator CLI ---
# 模拟演练运行
python -m hermes_cli.main coop run "实现二叉搜索树及单元测试" --model glm --mock
python -m hermes_cli.main coop status <task_id>
python -m hermes_cli.main coop list --limit 10
python -m hermes_cli.main coop pending
python -m hermes_cli.main coop approve <task_id> --notes "人工审核授权继续验证"
python -m hermes_cli.main coop budget

# --- 方式 2: 在 Hermes 聊天会话 / Gateway (Discord/Telegram) 使用 Slash 命令 ---
/coop run 实现高并发限流器与行为测试 --model deepseek --mock
/coop status <task_id>
/coop list --limit 5
/coop pending
/coop approve <task_id> --notes 批准
/coop budget

# --- 方式 3: 启动标准 stdio MCP 服务 ---
python -m integrations.hermes.mcp_server
```

### 4. 验证、真实联调与长期部署说明

- **当前验证状态**：
  - 核心编排与 Hermes 插件集成测试已全部通过（22 项专项集成测试 + 完整测试集 369 项全绿）；
  - 测试在替换模型调用边界与真实本地 Git/SQLite 环境下执行，严格验证了参数传递、状态机流转、SHA 校验及预算拦截，**未产生真实模型费用**。
- **真实模型联调前置条件**：
  1. 在主项目根目录 `.env` 中填写真实有效的 `GLM_API_KEY`（对应 `glm-5.3-flashx`）或 `DEEPSEEK_API_KEY`（对应 `deepseek-flash`）；
  2. 本地已安装并完成认证登录的 `gemini` CLI（推理档位 `high`）与 `codex` CLI（绑定模型 `gpt-6.1-sol`）；
  3. 执行正式任务时移除 `--mock` 标记。
- **长期多 Bot 团队与生产 Gateway 部署所需配置**：
  1. 在系统环境中配置 `hermes` CLI 执行环境与用户配置目录（`~/.hermes/config.yaml`）；
  2. 根据多 Bot 分工创建或引入独立的 Bot Profile（如 DevBot、ReviewerBot、ExpertBot）；
  3. 配置 Gateway 平台凭据（Discord Token、Telegram Bot Token 或 Slack App Token）；
  4. 生产无人值守执行时需部署操作系统级沙箱（如 Docker 容器、独立普通用户权限与网络出口隔离）。

## 预算

默认月度 ¥200、¥160 告警、¥190 暂停新任务、单任务 ¥10。

每次 API 调用根据输入字节上界、输出 Token 限制和保守费率，先在 SQLite 事务中预留；任务与月预算均计入并发预留。调用后幂等结算，专家补丁也记账。未知用量或中断按预留额保守处理。模拟费用只记模拟任务，不进入真实月支出。

本地估算无法替代服务商账单和账户消费上限。¥160 当前提供告警；输出上限可通过策略调整。

## Codex 补审恢复

```bash
python -m orchestrator.cli codex-backlog
python -m orchestrator.cli codex-backlog --mock
```

人工根据官方用量信息设置本地状态和恢复时间：

```bash
python -m orchestrator.cli codex-quota --state EXHAUSTED --reset-at "2026-10-20T12:00:00+08:00"
python -m orchestrator.cli codex-watch --interval 300
```

没有时区的时间按北京时间解释。监控进程保持运行时，在记录的恢复时间后轻量探测，成功才处理队列；未设置恢复时间时等待人工设置或手动补审。CLI 监控不是已安装的 systemd 服务。

`NEEDS_REPAIR` 保留人工可见反馈和任务标记。后续修复提交重新更新证据后可以再次排队；当前不自动新建另一编程任务。

## Web 可视化控制台 (Web Console)

内置轻量级现代化 Web 控制台与 REST API，提供全链路可视化与交互操作：

```bash
# 启动可视化控制台 (默认访问 http://127.0.0.1:5000)
python -m orchestrator.cli web

# 或指定端口和默认模拟演练模式
python -m orchestrator.cli web --port 5000 --mock
```

- **仪表盘概览**：实时月度 API 保守支出仪表盘、Token 消耗分布图、任务执行成功/失败率、Codex 额度状态及一键切换 Live/Mock 账库。
- **任务编排与生命周期流转**：任务筛选检索、可视化 FSM 确定性状态机节点流转图、多智能体协同（Primary/Reviewer/Expert/Runner）时序执行日志。
- **一键新建任务**：支持 Web 界面输入需求规格、单任务 Flash 二选一（GLM Flash / DeepSeek Flash）、模拟模式及自定义测试指令。
- **Multica 团队看板**：集成 5 泳道（Todo / In Progress / In Review / Done / Blocked）看板视图，支持在线认领、触发执行与评论报告查看。
- **Codex 补审与额度中心**：查看本地额度状态机、挂起补审队列，支持人工调整状态与设置恢复时间。
- **预算守护策略**：查看月度预算预警线（¥160/¥190/¥200）与并发预留流水（Budget Reservations）。

## 执行边界

各任务使用验证过的 Git worktree、`codex/task_<任务ID>` 分支和固定基础提交；创建或检查点失败立即停止。生成文件及上下文读取拒绝越界、链接及敏感目录。

子进程过滤继承环境中的 API 凭据，超时及取消会回收进程。Git worktree 隔离的是文件版本，目标测试仍在本机执行。无人值守处理不受信任项目前，需要部署操作系统或容器权限、网络和凭据隔离；仓库尚未提供 Docker/systemd 部署。

当前入口支持 CLI 与 Web 控制台，OpenCode 为独立适配器，任意任务中断后的自动续跑尚未提供。

## 验证

```bash
python -m pytest tests/ -q -p no:cacheprovider
```

测试包含故障回归、内存/独立数据库、真实临时 Git 仓库和真实进程清理；模型 API 和模型 CLI 用模拟返回验证，测试不产生模型费用。实际符号链接测试在 Windows 缺少权限时跳过，目录联接保护仍有真实 Windows 回归。

当前开发环境为 Windows / Python 3.14。Python 3.11 语法已检查；较旧版本实际运行和真实服务联调仍待验证。

## 开源参考

`references/` 记录 transitions、awslabs/cli-agent-orchestrator 和 multi-agent-code-review。运行时实际使用 `transitions` 依赖；另两个项目是代码参考。需要拉取参考代码时使用 Git 子模块初始化。
