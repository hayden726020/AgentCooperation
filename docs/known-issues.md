# 已知问题与技术债务清单 (Known Issues & Technical Debt)

> **建立时间**：2026-10-17  
> **状态跟踪**：与 `.planning/ROADMAP.md` 联动，按严重度排序。

---

## 1. 核心技术债务

### [Resolved / P1] `orchestrator/core.py` 循环自修逻辑存在 150+ 行高危重复
* **位置**：`orchestrator/core.py`
* **解决方案**：已提炼公共私有方法 `_run_test_review_repair_loop()` 与公共异常处理 `_handle_execution_exception()`，单点消除 160 行冗余重复，相关测试（25 passed）与集成测试（45 passed）全部通过。


### [High / P1] `_project_context` 全量扫描仓库磁盘导致上下文膨胀与 I/O 浪费
* **位置**：`orchestrator/core.py` L100-L154
* **表现**：调用 `os.walk(root)` 遍历所有目录并加载前 24,000 字符，缺乏基于任务意图的代码索引。
* **风险**：随着工程扩大，挤占 LLM 有效上下文，导致注意力稀释与无意义 Token 消耗。
* **计划**：在阶段三/四结合 `.planning/codebase/` 索引实现按需精准上下文构建。

### [Medium / P2] 双编排流水线共存且缺少统一证据数据结构
* **位置**：`orchestrator/core.py` 与 `orchestrator/gemini_closed_loop.py`
* **表现**：两者分别维护了一套敏感路径过滤、证据脱敏逻辑与检查点提交逻辑。
* **风险**：两个入口概念未完全归一，增加维护负担。
* **计划**：保持外部调用接口兼容的前提下，将通用过滤脱敏抽取到公共模块。

### [Medium / P2] 任务超时后的远端执行状态确认机制不足
* **位置**：`execution/subproc.py` 与 `orchestrator/core.py`
* **表现**：命令执行超时直接抛出异常并置任务 FAILED。
* **风险**：若网络请求或远端命令实际已生效但回报慢，重试时容易造成重复操作。
* **计划**：增加状态探针规范，在重试前探测状态。

### [Resolved / P2] 运行台事件层尚未接入闭环埋点
* **位置**：`execution/events.py`、`orchestrator/gemini_closed_loop.py`、`web/run_service.py`、`web/api.py`、`web/static/js/run_console.js`
* **解决方案**：M2 已完成闭环埋点（开发/测试/复核/修复/审批/终态事件）、运行提交与查询 API、SSE 实时运行台与历史回放；TestRunner 逐行回调参数顺序缺陷已修复，实时输出可正确显示 pytest 行。

### [Resolved / P2] M2 收尾复核发现的运行台边界问题
* **位置**：`orchestrator/gemini_closed_loop.py`、`web/run_service.py`、`web/api.py`、`web/static/js/run_console.js`、`web/static/js/app.js`、`execution/events.py`
* **解决方案**：暂停态 `result(terminal=false)` 不再终止 SSE/前端流，恢复后自动回到实时状态；resume 从 Task 累计用量；POST 字段类型错误返回 400；Web 端拒绝 `allow_untrusted_tests=true`（403）并始终传 False；敏感嵌套容器（credentials/headers/Cookie）整棵子树脱敏并新增 Google API Key 模式；状态时间线无条件记录；状态徽章转义；SSE 客户端断开时设置 `stop_event` 释放 worker。

### [Resolved / P2] 运行台 GUI 内 P0 审批尚未接入
* **位置**：`web/api.py`、`web/run_service.py`、`web/static/js/run_console.js`、`web/templates/index.html`
* **解决方案**：M3 新增 `POST /api/runs/<task_id>/approve|reject`，仅允许 `WAITING_APPROVAL` 的闭环任务；审批备注可选（≤2000 字符，超长或非字符串返回 400），写入审批记录与修复指令。`start_approval` 使用进程内 in-flight 集合阻止同一任务并发恢复；GUI 审批面板提供备注输入、批准/拒绝按钮、MOCK/LIVE 费用确认、历史回放只读提示与证据摘要（测试、复核结论、严重度、commit、diff、审批原因）。
* **验证**：`tests/test_web_runs.py` 覆盖批准/拒绝派发、404、非闭环任务 400、错误状态 409、备注类型/长度校验、并发冲突与仓库过滤；无头浏览器验收批准 → SUCCESS、拒绝 → FAILED，审批横幅与历史状态一致。

### [Resolved / P1] 审批后的修复轮次可绕过不可信测试门禁
* **位置**：`orchestrator/gemini_closed_loop.py` `_run_impl`/`_resume_impl`、`execution/worktree.py`
* **表现**：首次开发在 LIVE 模式会因模型生成/修改测试文件而暂停审批，但恢复路径 `allow_untrusted_tests` 默认 True 且未检查 `_detect_modified_or_new_tests`，批准后的修复可在无沙箱隔离下执行模型自写测试。复核进一步发现，门禁只读取 `metadata["files_written"]`，模型可通过允许的 `run_command` 写测试文件绕过。
* **解决方案**：新增 `WorktreeManager.get_changed_paths`（`git status --porcelain=v1 -z --untracked-files=all`，含重命名/复制两侧路径与 `--ignored=matching` 可选模式）与 `get_changed_paths_since`（`git diff --name-only -z --no-renames base HEAD`，含删除）。初始开发、普通修复、审批恢复三处均将元数据、未跟踪/忽略文件、已提交变更及修复后重新计算的 `base..HEAD` 取并集，再做敏感文件与不可信测试检查；测试检测同时覆盖 `conftest.py`、`pytest.ini`、`tox.ini`、`setup.cfg`、`pyproject.toml`、任意 `tests` 目录及大小写变体。恢复路径默认 `allow_untrusted_tests=False`；LIVE 修复发现不可信测试立即置 FAILED。恢复入口在调用模型前先检查暂停段已提交/未提交/忽略的测试或配置，未显式授权时保持 `WAITING_APPROVAL` 并重新发出 `approval_required`。GUI 审批提供显式勾选“允许本次修复执行新生成或修改的测试文件”，默认关闭并仅对批准生效。新增前述门禁的 8 项回归，包括 dev 自行 `git commit`、gitignored 测试、删除/重命名测试与暂停段遗留测试。

### [Resolved / P2] M3 复核发现的恢复元数据与清理问题
* **位置**：`orchestrator/gemini_closed_loop.py`、`storage/models.py`、`storage/db.py`、`web/api.py`、`web/run_service.py`、`web/static/js/run_console.js`
* **解决方案**：创建 Task 时持久化 `reviewer_model`、`max_repair_attempts` 与 `test_paths`（JSON），恢复时优先复用并校验修复轮次范围，避免静默回退 GLM 或丢失测试路径；`init_db` 自动补齐两列。LIVE 恢复前执行 `check_can_start_new_task` 预算预检，预算不足时以 `WAITING_APPROVAL` 非终态结果返回，任务保持可重试。恢复过程中 `REPAIRING` 状态异常时自动回退 `WAITING_APPROVAL`，事件终态与 Task 状态保持一致。显式 `test_paths` 在修复阶段优先于自动发现。GUI 审批提交后 30 秒无新事件会重新启用按钮并提示检查日志；请求增加 15 秒超时。审批 API 仅在批准时要求 `repo_root`，拒绝仍可操作无仓库任务。补充同源 Origin 正例、2000 字符边界、不可信测试显式开关、派发失败与协程失败清理、预算阻断与恢复失败回退、空 repo_root 回归测试。

### [Medium / P2] GUI 与 Hermes 双入口并发恢复缺少原子占用
* **位置**：`orchestrator/gemini_closed_loop.py` `resume_after_approval`、`web/run_service.py`、`integrations/hermes/bridge.py`
* **表现**：入口状态检查与 `REPAIRING` 写入不是同一个原子操作。Web 端有进程内 `_APPROVALS_INFLIGHT` 保护，但 Hermes bridge/CLI 直接调用 `resume_after_approval`；两个进程若在状态写入前同时读到 `WAITING_APPROVAL`，可能各自启动一次修复。
* **风险**：同一 worktree 上出现重复付费调用或状态竞争。Web 单入口内已由 in-flight 集合阻止；跨入口并发概率低但后果明确。
* **计划**：将入口占用改为数据库条件更新（`WHERE state='WAITING_APPROVAL'` 的 CAS，使用 `RESUMING` 或等价租约字段），或引入跨进程文件锁；完成前避免对同一任务同时使用 GUI 与 bot 审批。

### [Low / P3] 运行台仅做同源 Origin 校验，未覆盖 DNS rebinding
* **位置**：`web/api.py` 审批接口、`POST /api/runs`
* **表现**：审批接口比较 `Origin` 与请求 `Host`，可拦截普通跨站请求；但若攻击者通过 DNS rebinding 让恶意域名解析到本机，二者仍会一致。`POST /api/runs` 尚未加入同等 Origin 校验。
* **风险**：控制台默认面向本机/可信局域网且无多用户鉴权，公网暴露时风险升高。
* **计划**：M5 部署验证时增加 Host 白名单（可配置局域网地址）并为所有写接口统一校验 Origin；不在公网直接暴露控制台。

### [Resolved / P3] 恢复派发早期失败时事件终态与 Task 状态可能短暂不一致
* **位置**：`orchestrator/gemini_closed_loop.py` `resume_after_approval`
* **解决方案**：LIVE 预算阻断改为返回 `WAITING_APPROVAL` 的非终态结果；恢复异常时读取 Task 状态，若停留在 `REPAIRING` 则回退 `WAITING_APPROVAL`，事件层按实际状态输出 `terminal=false`，GUI 审批面板保持可重试。

### [Low / P3] 运行台未提供独立 diff 拉取端点
* **位置**：`web/api.py`
* **表现**：计划表中的 `GET /api/runs/<task_id>/diff` 尚未实现；diff 预览依赖闭环上报的 `diff` 事件。
* **风险**：未上报 diff 事件的旧任务无法在运行台按需补拉工作树 diff。
* **计划**：需要时基于 `WorktreeManager.get_diff` 增加只读端点，并对输出做脱敏与大小限制。

### [Low / P3] 事件字符串 8000 字符截断影响超大 diff/输出预览
* **位置**：`execution/events.py` (`_MAX_PAYLOAD_STRING`)
* **表现**：事件 payload 中单个字符串最多保留 8000 字符，超大 diff 在运行台仅显示前缀；前端另设 1200 行 diff 展示上限。
* **风险**：大变更的实时预览不完整，但完整证据仍在工作树提交与任务日志中。
* **计划**：后续将大 diff 落盘为产物并以引用/分页方式展示。

### [Low / P3] 流式凭据跨事件分片时无法在事件层完全脱敏
* **位置**：`execution/events.py`、`execution/security.py`
* **表现**：脱敏按单条事件 payload 执行；若密钥被模型输出拆成多个 chunk，单个 chunk 不匹配完整模式。已覆盖敏感键整棵子树、Cookie/Authorization、`sk-`/`AIza`/GitHub/JWT 等完整模式。
* **风险**：极端分片下可能残留部分密钥片段。
* **计划**：如真实运行出现，改为事件写入前短缓冲或落盘后统一扫描。

### [Low / P3] SSE 事件接口的多进程部署限制
* **位置**：`web/api.py`、`execution/events.py`
* **表现**：事件通过 JSONL 文件 tail 推送，默认 `idle_timeout=30s`，已加入心跳注释（默认 15s）、事件名清洗、终态 204 短路与客户端断开 `stop_event`；Flask 线程模式可用，同步多 worker 网关下长连接仍会占用 worker。
* **风险**：Linux 生产部署若使用同步 Gunicorn 多 worker，可能出现连接占用或推送延迟。
* **计划**：M5 部署验证时改为线程/异步 worker，或使用带超时的短轮询兜底。

### [Low / P3] 命令参数只接受 POSIX 风格分隔符
* **位置**：`config/settings.py`、`hermes-agent/plugins/model-providers/antigravity-cli/project_mode.py`
* **表现**：为阻断反斜杠转义和 Windows 设备路径，命令参数中出现 `\` 会被直接拒绝；测试路径须写成 `tests/test_x.py`，不能使用 `tests\test_x.py`。
* **风险**：模型若坚持使用 Windows 原生路径会得到策略拒绝，需要一次重试或提示纠正。
* **计划**：如真实运行频繁出现，可在参数分词后做路径规范化，而不是放开原始反斜杠。

### [Info / P3] 构建/类型检查命令的实际行为仍取决于仓库脚本
* **位置**：`config/policy.yaml` `execution.allowed_commands`
* **表现**：白名单只约束 argv 前缀与参数边界，不审计 `package.json`、`Makefile` 等构建脚本内容；模型在允许的工作区内仍可修改这些文件。
* **风险**：`npm run build` 等命令的最终行为仍来自仓库脚本，命令白名单不等于构建脚本沙箱。
* **计划**：维持 diff 复核、不可信测试门禁与 P0 人工审批作为最终防线；如需要更强隔离，应在 M5 评估容器/只读依赖目录方案。

### [High / P1] 白名单执行器仍会执行工作区内模型可写的代码
* **位置**：`config/settings.py`、`hermes-agent/plugins/model-providers/antigravity-cli/project_mode.py`
* **表现**：`pytest` 会加载仓库内 `conftest.py` 与插件，`npm run build` 会执行 `package.json` 脚本；模型可在同一工作区修改这些文件后经白名单命令触发执行。
* **风险**：命令白名单限定的是 argv 入口，不等价于代码执行沙箱。放宽 build/typecheck 会同步扩大该风险。
* **处置（接受为残余风险）**：默认策略仅放行 `python -m pytest`，build/typecheck 需显式配置；不可信测试门禁、diff 复核与 P0 人工审批仍是最终防线。M5 评估容器、只读依赖目录或独立执行用户。

### [High / P1] 生成式 PreToolUse 策略钩子尚未在真实 Antigravity CLI 上验证
* **位置**：`hermes-agent/plugins/model-providers/antigravity-cli/project_mode.py` `_hook_source`
* **表现**：钩子内容、拒绝决策与审计 JSONL 已由离线测试和直接调用验证；尚未在真实 `agy` 二进制上做端到端验证（避免未授权付费调用）。
* **风险**：上游 hooks 事件字段或调用时机若变化，钩子可能不生效；运行时桥接校验仍是第二道防线，但仅覆盖已上报的工具步骤。
* **计划**：作为 M5 发布门禁，在授权环境中对放行、拒绝、审计三项做真实 CLI 验收；未通过前不得默认开启 `project_execution`。

### [Medium / P2] 策略审计写入失败会被静默忽略
* **位置**：`hermes-agent/plugins/model-providers/antigravity-cli/project_mode.py` 钩子 `deny()`
* **表现**：审计 JSONL 的 `OSError` 被吞掉，拒绝决策仍会返回，但运行台可能缺少该条拒绝事件。
* **风险**：仅影响可观测性，不影响阻断本身。
* **计划**：将审计失败写入 stderr，或由运行时以 `issue` 事件上报。

### [Resolved / P2] M4 复核发现的钩子与运行时边界问题
* **位置**：`hermes-agent/plugins/model-providers/antigravity-cli/project_mode.py`、`tests/test_hermes_gemini_project_tools.py`
* **解决方案**：钩子与运行时统一接受 `CommandLine`/`command`、`Cwd`/`cwd`，路径字段大小写不敏感；新增可执行文件遮蔽防护，`PATH` 或工作区当前目录解析到工作区内时拒绝（含 `git status`）；补充审计 JSONL → `read_policy_audit()` 映射、畸形行容错、小写载荷与遮蔽可执行文件的回归测试。

### [Resolved / P2] M4 参数注入与工作区覆盖回归
* **位置**：`config/settings.py`、`tests/test_command_policy.py`、`tests/test_hermes_gemini_project_tools.py`
* **解决方案**：拒绝 pytest 的 `-c`/`--config-file`/`-o` 及组合短参数（`-qc`、`-qo`、`-qoc`），同时保留 `-kfoo` 等附加值参数；拒绝 `--rootdir`、`--basetemp`、`--prefix`、`--script-shell`、`--workspace`、`--package`、`--call` 与 `-C`/`-g`/`-w` 等工作区覆盖选项；所有命令强制以工作区根为 cwd。

### [Low / P3] 命令策略缓存与请求目录缺少失效/清理
* **位置**：`agents/gemini_dev_agent.py` `_command_policy_cache`、`hermes-agent/plugins/model-providers/antigravity-cli/project_mode.py` `create_project_request_scope`
* **表现**：同一进程内按仓库解析的白名单会缓存到进程结束，修改 `config/policy.yaml` 后需重启；每次 project execution 创建的 `native/` 请求目录不会自动清理。
* **风险**：策略变更需重启才生效；请求目录随运行次数增长占用磁盘（含审计 JSONL）。
* **计划**：按配置文件 mtime 失效缓存；为请求目录增加保留策略或启动时清理旧目录。

### [Low / P3] 策略审计记录的命令长度未设上限
* **状态**：已解决。
* **解决方案**：钩子写入审计前将命令截断至 2000 字符；`read_policy_audit()` 对命令/原因同样截断，并最多返回最近 1000 条记录，避免无界读取。

### [Resolved / P2] 项目级与全局授权文件未参与运行后复核
* **位置**：`hermes-agent/plugins/model-providers/antigravity-cli/project_mode.py` `verify_project_settings`
* **表现**：运行前写入的 `default-cli-project.json`（`permissionGrants`）与 `config.json`（`globalPermissionGrants`、自动执行策略）在运行后未复核，被 CLI 持久化放宽后可绕过仅检查 `settings.json` 的校验。
* **解决方案**：运行后同时校验项目文件与全局配置的 allow 集合完全一致、deny 为超集、ask 为空，并校验项目工作区 URI 与 `CASCADE_COMMANDS_AUTO_EXECUTION_EAGER`；钩子结构异常统一转为 `RuntimeError`。新增四种篡改场景回归。

### [Resolved / P2] 钩子与运行时对 cwd 键名和遮蔽执行的判断不一致
* **位置**：`hermes-agent/plugins/model-providers/antigravity-cli/project_mode.py`
* **解决方案**：运行时改为与钩子一致的大小写不敏感查找（`Cwd`/`cwd`、`CommandLine`/`command`）；同一参数出现大小写变体且值不同（含路径字段）直接拒绝；遮蔽检查覆盖 `PATHEXT` 扩展名、工作区根回退与工作区内符号链接（比较未解析路径与解析后路径），测试分别固定 PATH 与工作区根两条路径。

### [Resolved / P2] Web 后台任务把 CLI 面板写入服务端 stdout，导致全量回归捕获偶发解码失败
* **位置**：`web/api.py`、`orchestrator/cli.py`
* **表现**：`POST /api/tasks` 与 Multica 派发的后台任务直接调用 CLI 的 `execute_task` / `execute_multica_issue`，在后台线程向 stdout 打印 Rich 面板；pytest 全量运行时该输出偶发落入捕获文件，Windows 中文编码触发 `UnicodeDecodeError` 并连锁影响后续用例。
* **解决方案**：两个函数新增 `quiet` 关键字（默认 False，保持 CLI 行为不变），Web 派发统一传 `quiet=True`，服务端不再输出 CLI 面板；新增 Web 派发静默回归测试。

### [Low / P3] 默认策略下 `workplace/NAILONG` 仍继承默认 pytest 白名单
* **位置**：`config/policy.yaml`
* **表现**：该仓库未单独声明 `allowed_commands`，解析时回退到 `default`（仅 `python -m pytest`）。
* **风险**：Node 仓库的 build/typecheck 仍会被拒绝，需要显式配置后才能放宽；不会越权放行。
* **计划**：按实际仓库技术栈补充 `npm`/`npx` 条目，并在 M5 用真实任务验证。

### [Low / P3] 事件增量读取按文件全量扫描
* **位置**：`execution/events.py` `tail()`
* **表现**：每次轮询调用 `read()` 重新扫描 JSONL 文件，事件量大时存在 O(n) 重复读取。
* **风险**：长时间任务或高频测试输出下有一定 I/O 开销。
* **计划**：事件量增长后改为按字节偏移增量读取。

### [Resolved / P3] CLI 未知子命令会被当作需求
* **位置**：`hermes-agent/plugins/agent_cooperation/cli.py`、`commands.py`
* **解决方案**：加入 `difflib` 近似子命令拦截（cutoff 0.7）；拼写近似（如 `stauts`、`appr`、`budg`）会提示/报错而不会启动付费闭环。普通文本仍按设计默认进入闭环，并已补充真实根解析器测试、引号剥离与旧版字符串需求兼容测试。

### [Low / P3] 全量离线回归测试耗时偏长（约 3-4 分钟）
* **位置**：`tests/` 下 31 个测试模块
* **表现**：855 个测试全量运行约 236 秒（2026-10-19 实测 235.52s，受本机 I/O 波动影响）。
* **风险**：每次小改动全量回归阻碍敏捷迭代。
* **计划**：已提供 `scripts/run_fast_tests.ps1` 快速门禁；后续继续用 pytest 标记区分快速单元测试与完整集成回归。

---

## 2. 刚刚完成与验证的功能

### [Resolved / P1] 审查用量在预算结算与事件记账中重复写入
* **位置**：`orchestrator/gemini_closed_loop.py`
* **表现**：M2 新增的 `_record_usage` 在审查调用后再次累加 Task 用量，而 `BudgetGuard.settle_call` 已经写入同一笔 Token/费用，导致任务成本约 2 倍并可能提前触发预算上限。
* **解决方案**：`_record_usage(persist_task=False)` 用于已由预算守卫结算的审查调用；开发/修复调用仍由事件层持久化。新增 `test_reviewer_usage_is_not_double_counted` 断言 Task 总量只含一次审查用量。
* **验证**：全量离线回归 752 passed, 28 skipped。

### [Resolved / P2] 奶龙 SSH 登录交互终端脚本与测试
* **位置**：`scripts/nailong_ssh.ps1`, `tests/test_nailong_ssh.py`, `docs/NAILONG_SSH.md`
* **状态**：已由提交 `0acda0f` 交付，本地测试通过（16 passed in 8.28s）。

