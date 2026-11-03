# AgentCooperation

Python + SQLite 研发任务编排器，提供 CLI、Web 和 Hermes / Multica 适配入口。

自动流水线每个任务选定 GLM 或 DeepSeek 一个 Flash 引擎，复用它完成规划、开发和常规修复；默认 Antigravity CLI 独立复核，Codex CLI 处理专家修复和延期补审。测试与复核通过才能交付。配置以 `config/policy.yaml` 为准。

## 快速使用

需要 Python 3.11+、Git。真实模型和复核需有效凭据及已配置的 CLI；当前使用边界见 [项目上下文](docs/CONTEXT.md)。

```powershell
pip install -r requirements.txt
```

参考 `.env.example` 在本地 `.env` 配置选定引擎的密钥。目标仓库应已有初始提交和 Git 提交身份。

```powershell
# 显式模拟演练：不调用真实模型
python -m orchestrator.cli run --repo-root /path/to/target-repo --task "实现缓存并测试" --mock

# 真实研发：按配置使用模型、预算和独立复核
python -m orchestrator.cli run --repo-root /path/to/target-repo --task "实现缓存并测试" --model glm

python -m orchestrator.cli status
python -m orchestrator.cli tasks
python -m orchestrator.cli web --port 5000 --mock
```

特殊测试入口可加 `--test-command "npm test"`。目标测试在本机执行，工作树负责文件版本隔离。模拟与正式账库分开；任务失败或等待验收时 CLI 返回非零状态。

Web 控制台仅接受直接来自本机的连接，不提供身份认证。不要通过未配置身份认证的本机反向代理对外暴露；代理可能使远程请求看起来来自本机。工作树和测试进程也不是文件系统安全沙箱，不应执行不可信测试。

已有 Antigravity 登录时，可在 PowerShell 7.3+ 中直接打开 Gemini High 原生研发会话，无需 Gemini API 密钥：

```powershell
./scripts/hermes_bots.ps1 direct coop-dev
# 在已有任务工作目录中，携带自己的 UTF-8 任务文件：
./scripts/hermes_bots.ps1 direct coop-dev -Workspace 'E:\目标项目' -TaskFile 'E:\任务.md'
```

这是用户直接操作的原生 CLI 会话；退出后把修改 diff 和测试证据交给独立复核。它不经过 Hermes 模型 Provider，也不更改自动流水线。详见 [Gemini 原生研发入口](docs/GEMINI_NATIVE_DEV.md)。

本机 `coop-dev` 现已启用 Hermes 内部 Gemini 订阅 CLI **实验对话桥接**，通过真实 Agent 调用收到短回复，未使用 Gemini API 密钥：

```powershell
./scripts/hermes_bots.ps1 chat coop-dev
```

当前仅支持文本对话；Hermes 文件、终端工具和自动研发闭环尚未接通。详情及验证边界见 [Hermes Gemini 连接记录](docs/HERMES_GEMINI_CONNECTION_2026-10-14.md)。

## 文档与目录

| 入口 | 用途 |
| --- | --- |
| [当前上下文](docs/CONTEXT.md) | 角色配置、源码地图、待办和验收边界 |
| [奶龙 SSH 登录动画](docs/NAILONG_SSH.md) | Windows 客户端动画、安装和安全边界 |
| [Hermes Bot 使用](docs/HERMES_BOTS.md) | 手动 Profiles 和运维说明；与自动流水线区分 |
| [历史文档](docs/archive/README.md) | 历史验收、选型、修复记录和整理前完整 README |
| [参考项目](references/README.md) | 已登记的 Git 子模块 |
| `agents/`、`orchestrator/`、`execution/` | 模型适配、调度、工作树与测试 |
| `config/`、`storage/` | 策略与账库 |
| `integrations/`、`web/`、`scripts/` | Hermes / Multica、控制台和工具 |

`hermes-agent/`、`DeepSeekBot/` 是独立本地仓库，由各自 Git 管理。`.runtime/` 保存本地 CLI 依赖，`.integration/` 保存联调工作区，`data/` 保存实际账库，`.worktrees/` 保存任务工作树；这些目录不属于可随意删除的缓存。

## 验证与当前边界

```powershell
python -m pytest tests/ -q -p no:cacheprovider
```

Codex 恢复 UI 回归已归入 `tests/`，会随上述命令执行。测试使用隔离账库及模型模拟；不代表真实服务联调通过。

历史 Hermes 正式使用验收存在数据库升级、审批模式和调用边界等缺口，见 [当前上下文](docs/CONTEXT.md)。本次整理未关闭这些缺口。未提交的变更不会自动进入从 HEAD 创建的任务工作树。
