# Hermes Gemini 订阅 CLI 连接记录

当前结论：**Hermes 内部无工具文本对话已真实连通，本机 coop-dev 已启用实验配置。** 不使用 Gemini API 密钥，不复制个人 OAuth 文件。自动研发、Hermes 文件/终端工具、多角色自动编排尚未完成。

## 本机使用

在 PowerShell 7.3+ 中运行：

```powershell
pwsh -NoProfile -File E:\AgentCooperation\scripts\hermes_bots.ps1 chat coop-dev
```

本机 Profile 位于 `C:\Users\SPcuteAG\AppData\Local\hermes\profiles\coop-dev`。配置指定 `antigravity-cli / gemini-3.8-flash-high`，CLI 启动参数指定 high。运行时不静默切换模型或 API；原生事件缺少型号字段时，只能证明请求参数固定，不能把它当作服务端型号证明。

用户提供文本需求即可进行对话分析。当前不能要求该 Hermes Bot 读取或修改项目文件、执行命令、运行测试，不能把回复当作实际研发证据。需要研发操作时，保留 `direct coop-dev` 原生入口。

## 真实验证

真实路径为：`resolve_runtime_provider → AIAgent → 注册的 antigravity-cli Provider → 原生 CLI → Hermes 返回结果`。

- 最终新版真实请求返回 `HERMES_GEMINI_OK`，`completed=true`、`error_present=false`。验证时间：2026-10-14 22:01:13 北京时间。
- 安装到本机后，Profile 再次通过真实 Provider 构造检查：`AntigravityCLIClient`、`enabled=true`、`mock=false`。
- 106 项相关离线测试通过（68.37 秒），覆盖配置、原生入口、桥接协议、进程回收和真实 Hermes 初始化路径的模拟探针。
- 通用代码独立审查通过；Python 独立审查解除阻断。未声称所有服务端身份字段必然存在或缓存清理已经实现。

首次隔离请求因未继承系统代理收到 HTTP 400 地区错误。电脑已有启用的本地手动代理；桥接继承这项已有网络设置后连通。没有选择新代理服务器、修改系统代理或改变账号地区。

此前一次探测的 Windows 日志文件锁覆盖了退出结果；现在探测记录保存在 `.integration/hermes-gemini/probes/`，不在日志生命周期内递归删除，避免清理错误覆盖调用结果。探测只请求一次，关闭自动恢复，设置有界运行时间，结果文件仅记录验证标记与状态。

## 本轮变更

- Profile 严格布尔开关：`antigravity_cli.experimental_subscription_bridge: true`。旧的企业环境变量不再作为授权或启用依据。
- 单次消息写入 NDJSON 后关闭 stdin，读取到 EOF；检查唯一初始化、唯一 SUCCESS 结果和有效正文。
- 实时输出正文增量，依据最终正文补发缺失后缀；不一致、重复结果、后续错误、缺初始化均失败。
- 拒绝未支持的 Hermes 工具与非文本输入，不把 CLI 工具事件伪装成 Hermes 工具调用。
- 每请求在当前 Profile cache 创建独立 native HOME、工作目录和受限 Agent，写入 deny-all 权限；不修改原生个人配置，也不复制登录文件，认证由同一 Windows 用户的原生 Keyring 管理。
- 原 Hermes 会话目录仍保留为上下文信息；CLI 实际工作目录是隔离目录。当前对话不具有项目文件访问能力。
- 子进程不继承 Gemini API 密钥或自定义 Gemini API 地址；仅继承已有代理，日志脱敏代理凭据。
- 请求缓存保留于 Profile 的 `cache/antigravity-cli/`。本轮未实现容量或清理策略，不能宣称适合长期无人值守运行。

## 配置与恢复

只配置研发 Bot，不更新其他角色：

```powershell
python scripts/configure_hermes_bots.py --home 'C:\Users\SPcuteAG\AppData\Local\hermes' --profile coop-dev --enable-gemini-bridge

# 关闭实验桥接
python scripts/configure_hermes_bots.py --home 'C:\Users\SPcuteAG\AppData\Local\hermes' --profile coop-dev --disable-gemini-bridge
```

本次更新前已备份该 Bot 的配置及身份文件，位置：`.integration/hermes-gemini/profile-backups/20261014T140253Z/`。通过内容哈希确认其他 Bot 和 Hermes 根配置未被更新。不复制 .env 或认证文件进行备份。

Gemini CLI 仍使用现有订阅额度；本轮没有购买额度或请求 Gemini API 密钥。GLM / DeepSeek 的现有复核入口保持原配置，本次未调用它们。

[官方 FAQ](https://antigravity.google/docs/faq) 仍不支持消费级登录被第三方 Agent 包装调用。本机启用是用户明确要求下的实验接入，不构成企业授权证明或官方支持声明；实际连通结果与支持范围分别记录。
