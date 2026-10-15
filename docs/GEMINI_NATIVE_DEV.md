# Gemini 原生研发入口

当前订阅下采用“用户直接启动 Antigravity CLI → Gemini 研发 → 提交 diff 和测试证据 → 独立复核”的方式使用 Gemini。无需配置 Gemini API 密钥，也不需要将 Antigravity 登录转换为第三方模型凭据。

[官方 FAQ](https://antigravity.google/docs/faq) 区分了原生 CLI / 直接脚本工作流与第三方 Agent 包装调用。因此本入口只启动用户自己的前台原生会话：没有 Hermes 推理循环、模型响应转换或工具代理。Hermes `antigravity-cli` Provider 的限制保持单独报告，不用环境变量伪造授权。

## 启动

在 PowerShell 7.3 或更新版本中运行（`pwsh`；旧版 Windows PowerShell 不支持本入口要求的原生参数传递方式）：

```powershell
# 默认工作目录为 AgentCooperation 项目目录，进入后自行输入任务
./scripts/hermes_bots.ps1 direct coop-dev

# 等价的独立入口，不要求安装 Hermes 或存在 Bot Profile
./scripts/gemini_dev.ps1

# 指定已有工作目录和 UTF-8 任务文件
./scripts/hermes_bots.ps1 direct coop-dev -Workspace 'E:\项目工作区' -TaskFile 'E:\任务说明.md'

# 只检查路径、参数和认证模式配置，不启动 CLI 或发送任务
./scripts/hermes_bots.ps1 direct coop-dev -DryRun
```

如果当前终端是旧版 Windows PowerShell，可运行 `pwsh -NoProfile -File .\scripts\gemini_dev.ps1` 进入原生会话。

启动参数指定 `--model gemini-3.8-flash-high --effort high`。没有 TaskFile 时只打开交互界面；提供文件时使用原生 `--prompt-interactive` 携带任务并继续会话。脚本没有任意额外参数接口，不开启跳过权限，也不自动选择另一个模型。用户仍可在原生 CLI 中修改会话设置，因此这是启动参数固定，不是运行期不可更改的模型锁。

默认从 PATH 查找原生 `agy.exe`，再检查本机既有 `E:\Tools\AntigravityCLI\bin\agy.exe`。其他安装位置可通过 `-AgyExecutable '完整路径\agy.exe'` 指定。登录、权限提示、停止、恢复对话和额度提示均由原生 CLI 管理。

工作目录必须是已有文件系统目录。脚本不创建工作树，也不覆盖用户文件；需要独立工作树时先使用已有任务工作区，再把路径传入。工作目录选择不是文件访问隔离的保证。

## 无 API 密钥的认证路径

沿用本机用户目录与原生 keyring 登录，不读取或复制 OAuth 文件、不加载项目 .env。执行期间清除当前进程继承的 Gemini/Google API 密钥和 Gemini 自定义 API 地址，结束后恢复原环境。

按照[官方认证说明](https://antigravity.google/docs/cli/install/)，原生设置中的 `modelProvider: gemini` 选择 API 密钥模式。如果检测到该模式、未知显式 Provider 或无法解析的设置，入口明确失败；不会擅自修改个人配置或回退到 API。用户应在原生 CLI 中恢复账号登录模式后再次启动。

这是现有订阅的在线使用，会占用该账号的订阅额度；“无需 API 密钥”不等于离线或无限额度。脚本不会检查或购买额度。

## 与复核交接

1. 在原生 Gemini 会话中提供具体任务、验收条件和授权工作目录，要求保留已有改动。
2. 研发完成后保存实际 diff、测试命令与结果、相关提交 SHA，以及仍未解决的问题。不要把密钥、.env 或登录文件放进交接内容。
3. 把这些证据交给独立复核角色。GLM FlashX / DeepSeek Flash 的现有复核通道保持不变；这些通道可能仍使用各自 API，本入口不会自动调用它们。如果要求整个流程都不使用 API，则需另行提供复核的原生订阅入口。
4. 复核发现问题后，由用户在 Gemini 原生会话中继续修复，再重新提交证据。

CLI 退出码只代表会话进程退出状态，不能代替任务测试通过或独立复核批准。本入口不自动标记编排任务 SUCCESS。

## 验证边界

本机 `agy --help` 已确认支持 model、effort 和 prompt-interactive 参数，个人设置检查未发现 API Provider。离线测试使用隔离配置和假进程验证启动、参数、目录、环境恢复与失败路径；不会调用真实模型。当前账号的登录有效性、模型授权及真实研发请求仍需用户在原生会话中确认。

2026-10-14 独立复跑：新入口 27 项测试与已有 Bot / CLI 通道 29 项测试，共 **56 passed**（40.92 秒）。新测试包含本地编译的假 `agy.exe`，实测独立入口与项目 direct 入口的原生参数传递；中文、多行、引号和 `$()` 原样保留，非零退出码正确返回。代码与 Python 测试的独立审查均通过。

```powershell
python -m pytest tests/test_gemini_dev_launcher.py tests/test_hermes_bot_setup.py tests/test_hermes_cli_channels.py -q -p no:cacheprovider
```
