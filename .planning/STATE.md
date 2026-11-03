# 当前项目状态 (STATE)

## 2026-11-03：持续型 Agent P0 进行中

- 审批恢复采用数据库条件领取与所有者令牌；执行开始后中断进入 `INTERRUPTED`，进程硬崩溃遗留领取须人工核查，不能自动重试。双进程竞争及旧库并发迁移已有离线测试；全新库并发建表尚未验证。
- Web 仅接受直接环回连接，并校验 Host 与写请求 Origin；禁止经无认证的本机反向代理对外暴露。此边界不提供身份认证。
- 授权的真实 CLI 探针已执行两次：保留证据显示禁用命令存在审计记录且未发现测试标记；允许侧记录中出现命令、测试通过文本和标记文件，工具结果关联仍待核实。尚未独立归因特定钩子，也未验证运行台事件一致性。
- 测试执行仍无文件系统沙箱；恶意测试隔离验收未完成。P0 未完成，不应据此开启自动触发代码修改。
- 全量离线回归：`924 passed, 1 skipped`（2026-11-03）；启动后探针测试有调整，最终审批、Web 与探针靶向复跑 `166 passed`。

> **最后更新**：2026-10-19 (UTC+8)  
> **当前执行阶段**：阶段七：Agent 运行台与交互简化（M1、M2、M3、M4 完成，待 M5 部署验证）  
> **当前工作会话**：M4 安全命令范围拓宽、注入回归与收尾会话  

---

## 1. 当前进展概览

* [x] **阶段一：安全接管与现状审计**：已全部完成。
  - Git 安全备份分支已建：`backup/pre-vibe-coding-optimization-20261017`
  - 用户工作区现有改动已物理备份至 `.runtime/backup/pre_optimization_20261017/`
  - 591 个主线回归测试通过，环境基线（Python 3.14.0）已锁定
  - 核心功能矩阵已划分为 A-F 级，技术债务已明确
* [x] **阶段二：规划与记忆体系建设**：核心文档骨架已搭建。
  - `.planning/PROJECT.md`（项目目标与原则）
  - `.planning/ROADMAP.md`（六阶段路线图）
  - `.planning/STATE.md`（动态工作状态）
  - `.planning/codebase/audit.md`（现状审计）
  - `.planning/codebase/architecture.md`（系统架构与模块职责）
  - `docs/known-issues.md`（技术债务清单）
* [x] **阶段三：Skills 体系全面部署与全局定性**：8 大正交技能体系已定性为全局强制标准（Mandatory Skills）。
* [x] **阶段四优化 A：消除 `orchestrator/core.py` 中 150+ 行高危重复代码**（COMPLETED）
  - 成果：提炼 `_run_test_review_repair_loop()` 与 `_handle_execution_exception()`，消除 160 行重复代码，源码缩减 100 行。
  - 验证证据：`tests/test_core.py`, `tests/test_core_safety.py`（27 passed），全量编排测试全绿通过。
* [x] **阶段四优化 B：`_project_context` 上下文均衡与按需定向加载**（COMPLETED）
  - 成果：优先注入 `.planning/codebase/architecture.md` 架构蓝图，支持 `target_modules` 定向按需加载；无目标时采用多目录轮询（Interleaved Round-Robin Balancing），彻底解决前置目录（如 `agents/`）吃光 24,000 字符预算导致的严重上下文偏置问题。
  - 验证证据：`tests/test_core_safety.py` 中新增 `test_project_context_injects_architecture_map` 与 `test_project_context_balances_modules_and_honors_target_modules`。
* [x] **阶段四优化 C：任务超时状态确认探测器 (Status Confirmation Probe)**（COMPLETED）
  - 成果：在 `execution/worktree.py` 中实现 `WorktreeStatusProbe` 与 `probe_worktree_status(worktree, base_ref)`。精准保留 `git status --porcelain=v1` 列对齐，检测 commits ahead、staged、unstaged 和 untracked 状态；并在超时或执行异常时探测工作区真实进展，注入错误日志，杜绝超时盲目重试导致重复提交与扣费。
  - 验证证据：`tests/test_status_probe.py` 4 passed in 15.57s。
* [x] **阶段四优化 D：跨流水线证据脱敏与敏感文件拦截归一**（COMPLETED）
  - 成果：新建单一真理源 `execution/security.py`，集中管理敏感凭据正则、敏感文件正则、`contains_sensitive_files` 与 `redact_and_truncate_evidence`。在 `orchestrator/core.py` 与 `orchestrator/gemini_closed_loop.py` 中实现敏感文件写入拦截（如禁止修改 `.env`、私钥、证书）以及审查/专家证据脱敏截断。
  - 验证证据：`tests/test_execution_security.py` 4 passed in 0.05s，`test_core_safety.py` 27 passed。
* [x] **阶段四优化 E：测试套件分级与极速开发反馈**（COMPLETED）
  - 成果：创建 `scripts/run_fast_tests.ps1`，将单元逻辑与核心安全门禁聚合为极速测试套件（42 个核心测试可在 11 秒内执行完毕），为日常 AI Vibe Coding 迭代提供秒级反馈，同时保留全量离线回归作为最终验收标准。
* [x] **阶段七 M0：Agent 运行台需求确认与方案固化**（COMPLETED）
  - 成果：确认 4 项需求与 6 条默认假设；方案落盘至 `.planning/GUI_RUN_CONSOLE_PLAN.md`；路线图新增阶段七。
  - 决策：本地 Web 控制台；实时输出流 + 时间线 + diff + 成本；单输入框 + 智能默认；命令范围仅放宽测试/构建/类型检查。
* [x] **阶段七 M1：事件总线 + 流式子进程 + SSE + CLI 直接输入**（COMPLETED）
  - 新增 `execution/events.py`：`RunEvent`、JSONL 事件存储、脱敏、增量读取、tail 续传。
  - `execution/subproc.py`：新增可选 `on_line` 回调，逐行推送 stdout/stderr，保留完整输出、超时与取消清理。
  - `web/api.py`：新增 `GET /api/runs/<task_id>/events` 与 `GET /api/runs/<task_id>/events/stream`（SSE，支持 `Last-Event-ID`）。
  - CLI：`hermes coop "<需求>"` 与 `/coop <需求>` 直接进入闭环；旧子命令保持兼容，近似拼写子命令会被拦截以避免误触发付费任务。
  - 加固：事件序号改为“文件即真理源”+ 进程内/跨进程锁，跳过损坏尾部并自动补行；结构化 payload 按键名脱敏，正则避免灾难性回溯；SSE 增加心跳、事件名清洗与终态 204；流式子进程支持任意长度行与超时约束下的 stdin 写入。
  - 独立只读复核两轮，发现的 3 个 P1（stdin 超时、序号碰撞、脱敏遗漏）与后续 2 个 P1（超长行截断、正则回溯）均已修复并补充回归测试。
  - 验证证据：`tests/test_events.py` 24 passed；`tests/test_execution.py` 47 passed；`tests/test_web_runs.py` 20 passed；`tests/test_execution_security.py` 6 passed；全量 `681 passed, 28 skipped`（275.88s，最终代码复跑）。
* [x] **阶段七 M2：闭环埋点 + 运行 API + 实时运行台**（COMPLETED）
  - 闭环埋点：`orchestrator/gemini_closed_loop.py` 接入 `RunEventEmitter`，覆盖 INIT/DEVELOPING/TESTING/REVIEWING/REPAIRING/审批/终态；上报 `status`、`output`、`tool_call`、`tool_result`、`test_line`、`diff`、`usage`、`approval_required`、`result`，终态包含测试/复核结论、修复轮次、commit、耗时与 Token/费用。
  - 运行服务：新增 `web/run_service.py`，支持仓库白名单祖先/通配符授权（`config/policy.yaml` 新增 `workplace/NAILONG`）、智能默认（默认仓库、GLM 复核、策略修复轮次）、严格参数校验与 JobManager 异步派发。
  - 运行 API：`GET /api/runs/options`、`GET /api/runs`、`GET /api/runs/<task_id>`、`POST /api/runs`；复用既有事件查询与 SSE 接口，Mock/Live 账库隔离。
  - 前端运行台：新增“实时运行台”Tab（单需求输入 + 折叠高级选项）、Agent 输出流、步骤时间线、diff 预览、耗时/费用/Token/修复轮次指标、SSE 按 `seq` 去重 + 轮询兜底、终态提示与历史回放。
  - 修复：`TestRunner` 的逐行回调参数顺序为 `(line, stream)`，闭环三处测试回调已纠正，运行台现可显示真实 pytest 输出（此前仅显示 `stdout`）。
  - 收尾复核修复：暂停态 `result(terminal=false)` 不再终止 SSE/前端流并支持恢复后继续；resume 从 Task 累计用量；POST 字段类型错误返回 400；Web 端拒绝 `allow_untrusted_tests=true`（403）并始终传 False；敏感嵌套容器与 Google API Key 脱敏；状态时间线无条件记录；状态徽章转义；SSE 断开时释放 worker。
  - 复核发现并修复 P1：审查调用用量被 `BudgetGuard.settle_call` 与 `_record_usage` 重复写入 Task 账本，新增 `test_reviewer_usage_is_not_double_counted` 回归。
  - 验证证据：`tests/test_events.py`、`tests/test_web_runs.py`、`tests/test_gemini_closed_loop.py`、`tests/test_execution_security.py` 共 106 passed；无头 Chromium 真实页面验收：Mock 闭环 SUCCESS（`4 passed` 真实 pytest 行）、时间线、9 行 diff、耗时/费用正确，历史回放与暂停态/恢复态一致；全量离线回归 `704 passed, 28 skipped`（241.71s，0 failed）。
  - 独立只读复核两轮：首轮 4 个 P2 与次轮 1 个 P1 + 3 个 P2/P3 均已修复并补测试；无未解决 P0/P1。
  - 本轮度量：目标测试 106 passed（80.77s）；全量回归 704 passed, 28 skipped（241.71s）；复核返工 2 轮；真实模型调用 0（全部使用显式 Mock）。
* [x] **阶段七 M3：GUI 审批 + 多项目切换 + 证据面板**（COMPLETED）
  - 审批 API：`POST /api/runs/<task_id>/approve|reject`，仅允许 `WAITING_APPROVAL` 闭环任务；备注 ≤2000 字符；同源 Origin 校验；MOCK/LIVE 账库模式必须一致；仓库为空、状态不符、并发恢复均返回明确 409/400。
  - 恢复服务：`web/run_service.py` 增加 `_APPROVALS_INFLIGHT` 进程内并发保护，派发失败与协程异常均释放占位；`GeminiClosedLoopOrchestrator.resume_after_approval` 在 LIVE 模式恢复前执行预算预检。
  - 恢复上下文：创建 Task 时持久化 `reviewer_model`、`max_repair_attempts`、`test_paths`，恢复时优先复用；不可信测试门禁覆盖初始开发、普通修复与恢复路径，并将元数据、未跟踪/忽略文件、已提交变更与修复后重算的 `base..HEAD` 并集作为检查输入，阻止 `run_command` 写测试、dev 自行 commit、gitignored 测试及删除/重命名测试绕过；测试配置（`conftest.py`、`pytest.ini`、`tox.ini`、`setup.cfg`、`pyproject.toml`）与任意 `tests` 目录一并纳入门禁。恢复入口在模型调用前检查暂停段遗留测试，未显式授权时保持 `WAITING_APPROVAL` 并重新发出审批事件；GUI 提供默认关闭的显式执行测试文件开关。
  - 前端运行台：审批横幅支持备注、批准/拒绝、LIVE 费用确认、历史回放只读提示、30 秒无事件自动解锁；新增证据摘要面板（测试、复核结论、严重度、commit、diff、审批）；历史列表支持按仓库过滤。
  - 独立只读复核三轮：首轮 1 个 P1（恢复绕过测试门禁）与 5 个 P2；第二轮发现 1 个 P1（`run_command` 写测试绕过元数据门禁）与 3 个 P2（无仓库任务无法拒绝、恢复早期失败状态不一致、审批后测试死路）；第三轮发现 1 个 P1（普通修复循环未合并 dev 自行 commit 的测试）与 2 个 P2（恢复入口遗漏 ignored 测试、工作树重命名隐藏旧测试路径）以及 1 个跨入口并发恢复 P2。P1 与门禁类 P2 已修复并补回归；跨入口 CAS 记为已知 P2。DNS rebinding/CSRF 记为 P3。
  - 验证证据：`tests/test_web_runs.py` 55 passed；`tests/test_gemini_closed_loop.py` 36 passed；无头浏览器验收批准 → SUCCESS、拒绝 → FAILED；全量离线回归 `752 passed, 28 skipped`（227.50s，0 failed，第三轮修复后最终代码复跑）。
  - 本轮度量：目标测试 117 passed（82.43s）；全量回归 752 passed, 28 skipped（227.50s）；复核返工 3 轮；真实模型调用 0（全部使用显式 Mock）。
* [x] **阶段七 M4：安全命令范围拓宽与注入回归**（COMPLETED）
  - 命令策略：`config/policy.yaml` 新增仓库级 `allowed_commands`（test/build/typecheck 的 argv 数组），`config/settings.py` 新增 `RepoCommandConfig` 与默认策略（仅 `python -m pytest`）；argv-only、无 shell 组合、强制工作区根为 cwd，仅额外放行精确 `git status`。
  - 双重执行：`project_mode.py` 生成 PreToolUse 钩子并在运行时桥接再次校验；拒绝 pytest `-c`/`-o`/`--config-file` 及组合短参数、`--rootdir`/`--basetemp`/`--pyargs`、npm `--workspace`/`--prefix`/`-w`、`make -C` 等工作区覆盖选项；拒绝路径越权、受保护文件、大小写变体/重复参数键歧义与工作区遮蔽的可执行文件（PATH、PATHEXT、工作区根、符号链接）。
  - 运行后复核：`settings.json`、项目 `default-cli-project.json` 的 `permissionGrants`、全局 `config.json` 的 `globalPermissionGrants`/自动执行策略，以及钩子内容字节一致性；任一篡改即终止并报错。
  - 审计与可视化：拒绝写入 `native/policy-audit.jsonl`（命令/原因截断 2000 字符，最多读取最近 1000 条），并以 `tool_result(policy_rejected=true)` 进入运行事件流。
  - Web 后台静默：`execute_task` / `execute_multica_issue` 新增 `quiet` 关键字（默认 False 保持 CLI 行为），Web 派发统一传 `quiet=True`，消除后台 Rich 面板写入服务端 stdout 造成的测试捕获解码抖动。
  - 验证证据：`tests/test_hermes_gemini_project_tools.py` 141 passed；项目工具 + 命令策略 180 passed；M1-M4 相关三模块 241 passed（143.33s）；`tests/test_web.py` 16 passed；全量离线回归 `855 passed, 28 skipped`（235.52s，0 failed，最终代码复跑）。
  - 独立只读复核：无 P0/P1；已修复 P2（运行时 cwd 键名大小写不一致、项目/全局授权文件未复核、Web 后台 stdout 捕获抖动）与 P3（PATHEXT 遮蔽、符号链接、重复参数键、钩子结构脆弱、审计读取无上限、遮蔽测试覆盖假阳性）。登记的残余风险：白名单执行器仍会执行模型可写的工作区代码、生成钩子尚未对真实 `agy` 端到端验证、审计写入失败静默，均纳入 M5 发布门禁。
  - 本轮度量：目标测试 241 passed（143.33s）；全量回归 855 passed, 28 skipped（235.52s）；复核返工 1 轮；真实模型调用 0（全部使用显式 Mock）。

---

## 2. 阻塞与注意项

* **双编排器共存过渡**：`orchestrator/core.py` 与 `orchestrator/gemini_closed_loop.py` 共存阶段，公用逻辑（凭据脱敏、敏感路径防御、工作树状态探针）已完全归一至 `execution/`，后续可根据需要进一步合并两者外部调用入口。

---

## 3. 下一步工作 (Next Actions)

1. 评估将事件 `diff` 的大补丁改为产物引用，避免 8000 字符截断影响超大变更预览；并补齐独立的 `GET /api/runs/<task_id>/diff` 端点。
2. M5 部署验证：真实 `agy` 端到端钩子验收（放行/拒绝/审计），统一写接口 Origin/Host 白名单，确认 Linux 浏览器访问与 SSE worker 配置。
3. M5 前评估白名单执行器的隔离方案（容器、只读依赖目录或独立执行用户），并处理跨入口审批 CAS 的已知 P2。
