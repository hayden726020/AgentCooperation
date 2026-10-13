# References & Open-Source Upstreams

本目录存放了从 GitHub 精选克隆的三个核心架构参考项目，供后续调度器开发使用：

1. **`transitions` (`pytransitions/transitions`)**
   - **用途**：轻量级有限状态机引擎（FSM）。
   - **核心参考**：任务生命周期状态转移、Codex 额度守护状态机（READY / RESERVED / EXHAUSTED / RECOVERING）。

2. **`cli-agent-orchestrator` (`awslabs/cli-agent-orchestrator`)**
   - **用途**：多终端 CLI Coding Agent 调度与环境隔离。
   - **核心参考**：多 CLI 子进程调度、ANSI 字符清洗、防交互卡死设计。

3. **`multi-agent-code-review` (`alanchn31/multi-agent-code-review`)**
   - **用途**：多 Agent 代码审查范式。
   - **核心参考**：基于 Git Diff 的 JSON Schema 结构化审查结果定义。
