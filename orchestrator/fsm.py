from transitions import Machine
from typing import Optional


class TaskLifecycleFSM:
    """
    基于 pytransitions 的确定性任务生命周期状态机：
    保证任务在从规划、开发、测试、自修、独立审查、专家升级到完成的各状态间严格按规则流转。
    """

    STATES = [
        "INIT",
        "PLANNING",
        "DEVELOPING",
        "TESTING",
        "REPAIRING",
        "REVIEWING",
        "ESCALATING",
        "CODEX_BACKLOGGED",
        "SUCCESS",
        "FAILED",
        "BUDGET_STOPPED"
    ]

    def __init__(self, max_repair_attempts: int = 3, initial_state: str = "INIT"):
        self.max_repair_attempts = max_repair_attempts
        self.repair_attempts = 0
        self.escalation_count = 0

        self.machine = Machine(
            model=self,
            states=TaskLifecycleFSM.STATES,
            initial=initial_state,
            auto_transitions=False
        )

        # 1. 启动规划
        self.machine.add_transition(
            trigger="to_planning",
            source="INIT",
            dest="PLANNING"
        )

        # 2. 规划完成 -> 开发构建
        self.machine.add_transition(
            trigger="to_developing",
            source="PLANNING",
            dest="DEVELOPING"
        )

        # 3. 开发完成或自修/升级修复完成 -> 执行测试
        self.machine.add_transition(
            trigger="to_testing",
            source=["DEVELOPING", "REPAIRING", "ESCALATING"],
            dest="TESTING"
        )

        # 4. 测试失败且未达自修上限 -> 触发 Primary 自修循环 (<= 3 次)
        self.machine.add_transition(
            trigger="to_repairing",
            source="TESTING",
            dest="REPAIRING",
            conditions=["can_repair"],
            after="increment_repair_attempts"
        )

        # 5. 测试失败但已达自修上限 (3次) -> 升级至 Codex 专家
        self.machine.add_transition(
            trigger="to_escalating_from_test",
            source="TESTING",
            dest="ESCALATING",
            conditions=["exceeded_repair_limit"]
        )

        # 6. 测试通过 -> 提交 Gemini CLI 独立复核
        self.machine.add_transition(
            trigger="to_reviewing",
            source="TESTING",
            dest="REVIEWING"
        )

        # 7. Gemini 复核发现 P1/P0 架构争议 -> 升级至 Codex 专家
        self.machine.add_transition(
            trigger="to_escalating_from_review",
            source="REVIEWING",
            dest="ESCALATING"
        )

        # 8. Codex 专家参与时遇到周额度耗尽 -> 切入 CODEX_BACKLOGGED 状态
        self.machine.add_transition(
            trigger="to_codex_backlogged",
            source="ESCALATING",
            dest="CODEX_BACKLOGGED"
        )

        # 9. Codex 给出指导建议 -> 返回自修应用补丁
        self.machine.add_transition(
            trigger="to_repairing_from_codex",
            source="ESCALATING",
            dest="REPAIRING"
        )

        # 10. 复核通过或专家升级处理完毕 -> 顺利完成
        self.machine.add_transition(
            trigger="to_success",
            source=["REVIEWING", "CODEX_BACKLOGGED", "ESCALATING"],
            dest="SUCCESS"
        )


        # 11. 异常终止或预算熔断
        self.machine.add_transition(
            trigger="to_failed",
            source=TaskLifecycleFSM.STATES,
            dest="FAILED"
        )
        self.machine.add_transition(
            trigger="to_budget_stopped",
            source=TaskLifecycleFSM.STATES,
            dest="BUDGET_STOPPED"
        )

    # --- Guards ---
    def can_repair(self) -> bool:
        return self.repair_attempts < self.max_repair_attempts

    def exceeded_repair_limit(self) -> bool:
        return self.repair_attempts >= self.max_repair_attempts

    # --- Callbacks ---
    def increment_repair_attempts(self):
        self.repair_attempts += 1
