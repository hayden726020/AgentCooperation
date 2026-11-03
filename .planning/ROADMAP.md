# 阶段开发路线图 (ROADMAP)

---

## 阶段一：现有项目安全接管与只读审计 (Status: COMPLETED)
- [x] 建立 Git 备份分支 `backup/pre-vibe-coding-optimization-20261017`
- [x] 备份工作区未提交修改与未跟踪脚本（`README.md`, `test_nailong_ssh.py`, `nailong_ssh.ps1` 等）
- [x] 锁定 Python 3.14 与 118 个依赖包基线
- [x] 确认主测试套件通过（591 passed, 28 skipped）
- [x] 完成模块职责审计与功能完成矩阵（A-F 级）
- [x] 识别主要技术债务并排序

---

## 阶段二：项目规划与记忆体系重建 (Status: COMPLETED)
- [x] 建立 `.planning/` 目录结构（`PROJECT.md`, `ROADMAP.md`, `STATE.md`, `codebase/`）
- [x] 梳理系统架构与模块职责契约（`.planning/codebase/architecture.md`）
- [x] 整理已知问题与技术债务清单（`docs/known-issues.md`）
- [x] 规范记忆更新规则：任务完成后强制更新动态状态与提交证据

---

## 阶段三：AI 上下文工程与执行效率治理 (Status: COMPLETED)
- [x] 部署方案指定的 8 个核心正交 Skills 并定性为全局强制规范
- [x] 改进 `_project_context` 磁盘扫描机制，改为架构大图优先 + 按需定向 + 多目录轮询
- [x] 治理工具输出与日志截断，集中至 `execution/security.py` 防止凭据泄露与超长 Token 膨胀
- [x] 建立状态持久化规范：单任务独立上下文，完成后更新状态文件平滑换新

---

## 阶段四：分模块优化与核心稳定性建设 (Status: IN_PROGRESS)
- [x] **优化 1**：提炼 `orchestrator/core.py` 中重复的测试/自修/复核循环代码（缩减 100 行）
- [x] **优化 2**：实现工作树状态探针（`WorktreeStatusProbe`），增强任务超时状态确认，防重复执行与重复扣费
- [x] **优化 3**：集中敏感凭据与敏感文件安全防护（`execution/security.py`），核心编排拦截对 `.env`、私钥的非法写操作
- [x] **优化 4**：建立极速开发回归套件（`scripts/run_fast_tests.ps1`，42 测试 < 12s），提升 Vibe Coding 迭代速率
- [ ] **优化 5**：梳理并完成 Nailong SSH 客户端的剩余测试与实现

---

## 阶段五：多 Agent 协作机制优化 (Status: PENDING)
- [ ] 统一 Gemini Closed-Loop 与旧版编排器中的证据数据结构与调用入口
- [ ] 规范主 Agent、执行 Agent 与审查 Agent 的任务交接协议
- [ ] 建立多工作树并发隔离规则，避免文件修改冲突

---

## 阶段六：持续验证与防退化闭环 (Status: PENDING)
- [ ] 建立每次任务提交的十步闭环检查表
- [ ] 持续度量 Token 消耗、单任务执行时长与一次通过率
- [ ] 定期与 `.planning/STATE.md` 对齐代码事实

---

## 阶段七：Agent 运行台与交互简化 (Status: IN_PROGRESS)
详细方案见 `.planning/GUI_RUN_CONSOLE_PLAN.md`。

- [x] **M0**：确认需求与假设，固化事件模型、UI 布局、里程碑与验收标准
- [x] **M1**：事件总线 + 流式子进程 + SSE + CLI 直接输入
- [x] **M2**：闭环埋点 + 运行 API + 实时运行台（agent 三栏、时间线、成本）
- [x] **M3**：diff 预览 + GUI 审批 + 历史回放 + 多项目切换
- [x] **M4**：安全命令范围拓宽（测试/构建/类型检查）+ 注入与越权回归
- [ ] **M5（可选）**：桌面壳、Linux 远程访问验证
