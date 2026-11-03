# Hermes 手动 Bot 团队配置与 CLI 通道接入说明

**当前更新（2026-10-14）：本机 `coop-dev` 已显式启用 Gemini 订阅 CLI 实验对话桥接，真实 Hermes Agent 调用已通过。** 使用 `./scripts/hermes_bots.ps1 chat coop-dev`；不使用 Gemini API 密钥。当前文本对话可用，Hermes 文件/终端工具尚不支持，不能按完整研发 Agent 使用。以下旧版适配记录与原生入口仍保留；当前配置和验证结果以 [连接记录](HERMES_GEMINI_CONNECTION_2026-10-14.md) 为准。

本项目通过 Hermes 原生独立 Profiles 与官方模型插件机制，完成 **Antigravity CLI (agy)** 与 **Codex CLI (app-server)** 的模型通道接入准备与实现。保留独立复核 Bot (`coop-review`)，不执行自动研发编排、不消耗付费额度、不泄露或复制 OAuth 凭据。

本机 Profiles 位于 `C:\Users\SPcuteAG\AppData\Local\hermes\profiles`。最新 Gemini 实验桥接修复的 106 项相关离线测试通过，且真实 Hermes Agent 短回复验证通过；历史 29 项记录仅代表之前的离线适配阶段。

现有 Antigravity 订阅的 Gemini 研发入口已另设为 `./scripts/hermes_bots.ps1 direct coop-dev`：用户直接操作原生 CLI，启动参数指定 Gemini High，复用原生账号登录，无需 Gemini API 密钥。此入口不经过 Hermes 模型 Provider；Provider 的受限状态与原生会话可启动是两件事。具体任务文件、工作目录与复核交接方式见 [Gemini 原生研发入口](GEMINI_NATIVE_DEV.md)。

---

## 一、Bot 角色与通道一览

| Profile | 角色定位 | 模型与参数 | 通道与 Provider | 权限边界与当前状态 |
| --- | --- | --- | --- | --- |
| `coop-dev` | Gemini 研发对话；执行工具待接入 | `gemini-3.8-flash-high`<br>启动推理参数 `--effort high` | `antigravity-cli`<br>本机原生 `agy` 1.3.4 | **本机已显式启用实验对话，真实短回复通过**。每请求隔离原生 HOME / 工作目录和无工具 Agent，不使用 Gemini API 密钥；Hermes 文件、终端工具尚未支持。服务商消费级支持范围与实际连通结果分别说明，见 [连接记录](HERMES_GEMINI_CONNECTION_2026-10-14.md)。 |
| `coop-review` | 独立复核 | GLM 5.3 FlashX<br>`glm-5.3-flashx` | `zai`<br>端点: `https://open.bigmodel.cn/api/paas/v4` | **只读复核**。<br>工具列表为空，仅根据需求、diff 和真实测试报告独立审查；密钥单独写入 `coop-review/.env`。 |
| `coop-expert` | 架构专家与补审 | GPT Sol 6.1<br>`gpt-6.1-sol` (固定不可换) | `codex-cli`<br>(插件: `hermes-agent/plugins/model-providers/codex-cli`)<br>本机二进制: `codex.exe` 0.162.0 (`app-server`) | **三重安全只读隔离已生效**。<br>1. CLI 参数注入 `-c sandbox_mode="read-only"`、`--disable plugins` 与 `-c approval_policy="never"`；<br>2. 自动探测并禁用本机所有宿主 MCP (`-c mcp_servers.<name>.enabled=false`)；<br>3. 协议层精确匹配拒绝审批（`decline` / `turn` 权限为空），拦截非法工具调用；<br>真实流式逐步产出增量；认证复用本机 `~/.codex/auth.json`，不向 Hermes 复制 OAuth token。真实订阅尚未消耗额度验证。 |

---

## 二、认证、权限与安全隔离机制

1. **凭据边界**：
   - 外部 CLI 的认证完全由各自客户端自行管理（Codex 在 `~/.codex/auth.json`，Antigravity 在 `~/.gemini/`）。
   - Hermes Profile 内不创建 `auth.json`，`.env` 不包含 OpenAI/Gemini 密钥，日志和错误信息对 Token/Key 自动脱敏（`redact_sensitive_text(..., force=True)`）。
2. **只读保护与宿主 MCP 隔离**：
   - 复核与专家 Bot 的所有平台工具列表为空 (`platform_toolsets.cli = []`)，自动 coding context 关闭，MCP 为空。
   - 专家 Bot (`coop-expert`) 在 CLI 层开启 `sandbox_mode="read-only"`，并在 CLI 启动参数中枚举本机 `~/.codex/config.toml` 中配置的所有宿主 MCP 并注入 `-c mcp_servers.<name>.enabled=false`。
   - 在 JSON-RPC 协议层按官方 Schema 拦截所有审批与工具调用：文件与命令审批下发 `{"decision": "decline"}`，elicitation 审批下发 `{"action": "decline", ...}`，权限审批下发空 scope，若收到非法工具执行主动触发安全违规异常。
3. **会话工作目录（Session CWD）动态绑定**：
   - 两个 Provider 均绑定到 Hermes 当前 Agent 会话的 `runtime_cwd`，优先使用 `agent.runtime_cwd.resolve_agent_cwd()`，确保多会话与并发任务在各自授权目录运行，支持路径含空格与跨盘符。
4. **实时增量流式输出**：
   - 两个 Provider 废弃整轮结束后再包装流的方式，在进程运行过程中实时解析事件（Codex 的 `item/agentMessage/delta`，Antigravity 的 `step_update`）并即时 `yield` `ChatCompletionChunk`，并在生成器终止时统一回收资源。
5. **原生 Gemini API 禁用**：
   - `providers.gemini.enabled: false`，即使系统环境变量存在 `GEMINI_API_KEY`，Hermes 的 provider guard 也会在网络传输前直接拦截，禁止隐式计费兜底。
6. **子进程生命周期管理**：
   - 超时（默认 300s）或用户取消时，采用 `try...finally:` 自动调用 `kill_process_tree` 级联回收子进程树，并清除 `_active_processes` 注册记录，杜绝孤儿 CLI 进程常驻后台。
7. **项目执行命令白名单**：
   - `coop-dev` 显式启用 `project_execution` 后，可通过 `antigravity_cli.project_execution.allowed_commands` 按 `test / build / typecheck` 声明 argv 数组；未声明时仅允许 `python -m pytest`。
   - 自动闭环由 AgentCooperation `config/policy.yaml` 按仓库相对路径解析策略并覆盖 Profile 默认值；策略同时写入生成的 PreToolUse 钩子与运行时校验器，命令工作目录必须等于工作区根。
   - 双层校验拒绝 shell 组合、未声明命令、绝对路径/盘符/`..`/受保护路径、工作区遮蔽的可执行文件（`PATH`、`PATHEXT`、工作区根或工作区内符号链接解析到工作区内，含 `git status`），以及 `--rootdir`、`-c`/`-o`（含 `-qc`/`-qo` 组合短参数）、`--script-shell`、`--package`、`--prefix` 等覆盖选项；钩子与运行时同时接受 `CommandLine`/`command`、`Cwd`/`cwd` 及大小写不敏感的路径字段，并对同一参数的大小写变体冲突直接拒绝。运行结束后复核 `settings.json`、项目 `permissionGrants`、全局 `globalPermissionGrants` 与钩子源码。拒绝记录写入本次请求的 `native/policy-audit.jsonl`，并以 `tool_result(policy_rejected=true)` 显示在运行台。


---

## 三、常用运维与检查命令

### 1. 离线安装与幂等性检查

```powershell
# 仅检查状态（不修改任何文件）
python scripts/configure_hermes_bots.py --check --home "C:\Users\SPcuteAG\AppData\Local\hermes"

# 试运行预览
python scripts/configure_hermes_bots.py --dry-run --home "C:\Users\SPcuteAG\AppData\Local\hermes"

# 同步项目 GLM 密钥并重新配置
python scripts/configure_hermes_bots.py --sync-review-key --home "C:\Users\SPcuteAG\AppData\Local\hermes"
```

### 2. 交互启动器与状态查看

```powershell
# 查看所有 Bot 状态摘要
./scripts/hermes_bots.ps1 list

# 查看特定 Bot 详细元数据与 SOUL
./scripts/hermes_bots.ps1 show coop-dev
./scripts/hermes_bots.ps1 show coop-expert
./scripts/hermes_bots.ps1 show coop-review

# 用户直接启动原生 Gemini High 研发会话（无需 Hermes Profile）
./scripts/hermes_bots.ps1 direct coop-dev

# 离线预览原生入口，不启动 CLI 或提交任务
./scripts/hermes_bots.ps1 direct coop-dev -DryRun

# Hermes 内部实验对话（本机已启用；其他安装默认须显式 opt-in）
./scripts/hermes_bots.ps1 chat coop-dev

# 启动专家诊断（协议与沙箱双重只读）
./scripts/hermes_bots.ps1 chat coop-expert

# 启动独立复核
./scripts/hermes_bots.ps1 chat coop-review
```

### 3. 执行自动化测试套件

```powershell
# 运行全部 29 项离线契约与回归测试
pytest tests/test_hermes_bot_setup.py tests/test_hermes_cli_channels.py -v

```

---

## 四、关于 Antigravity CLI 政策条款说明

根据 [Google Antigravity 官方条款与 FAQ](https://antigravity.google/docs/faq)：
> "Antigravity CLI headless mode (agy -p) is intended for direct scripting and CI workflows. Wrapping agy inside third-party coding agents or applications, or automating account rotation, is not supported under consumer plans."

本项目遵循用户指令与服务商合规要求：
- **不绕过限制、不复制 token、不伪装客户端**。
- `antigravity-cli` Provider 插件已完成所有协议解构、流式解析、high 档位参数注入与单测验证；
- 默认安装不启用实验桥接；当前用户明确要求尝试接入后，本机 `coop-dev` 通过 Profile 实验开关启用。实际连通不等于服务商支持这种接入方式；
- 当前订阅使用 `direct coop-dev` 原生入口。`ANTIGRAVITY_ENTERPRISE_AUTHORIZED=1` 本身不构成已核验授权，也不作为此入口的解锁条件；企业通道须另行核验支持范围。
