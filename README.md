# AgentCooperation

Python + SQLite 编排器：每个任务选择 **GLM Flash 或 DeepSeek Flash 其中一个**，由同一引擎负责规划、开发和修复；Gemini CLI 独立验收，Codex CLI 处理重要问题及延期补审。

单任务 Flash 二选一是用户指定的架构。所有阶段复用该实例，禁止中途串联另一 Flash 引擎。

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

## 执行边界

各任务使用验证过的 Git worktree、`codex/task_<任务ID>` 分支和固定基础提交；创建或检查点失败立即停止。生成文件及上下文读取拒绝越界、链接及敏感目录。

子进程过滤继承环境中的 API 凭据，超时及取消会回收进程。Git worktree 隔离的是文件版本，目标测试仍在本机执行。无人值守处理不受信任项目前，需要部署操作系统或容器权限、网络和凭据隔离；仓库尚未提供 Docker/systemd 部署。

当前入口为 CLI，OpenCode 为独立适配器，Web/Telegram 控制台和任意任务中断后的自动续跑尚未提供。

## 验证

```bash
python -m pytest tests/ -q -p no:cacheprovider
```

测试包含故障回归、内存/独立数据库、真实临时 Git 仓库和真实进程清理；模型 API 和模型 CLI 用模拟返回验证，测试不产生模型费用。实际符号链接测试在 Windows 缺少权限时跳过，目录联接保护仍有真实 Windows 回归。

当前开发环境为 Windows / Python 3.14。Python 3.11 语法已检查；较旧版本实际运行和真实服务联调仍待验证。

## 开源参考

`references/` 记录 transitions、awslabs/cli-agent-orchestrator 和 multi-agent-code-review。运行时实际使用 `transitions` 依赖；另两个项目是代码参考。需要拉取参考代码时使用 Git 子模块初始化。
