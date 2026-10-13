import pytest
from orchestrator.fsm import TaskLifecycleFSM


def test_fsm_normal_lifecycle():
    fsm = TaskLifecycleFSM(max_repair_attempts=3)
    assert fsm.state == "INIT"

    fsm.to_planning()
    assert fsm.state == "PLANNING"

    fsm.to_developing()
    assert fsm.state == "DEVELOPING"

    fsm.to_testing()
    assert fsm.state == "TESTING"

    # 测试通过 -> 复核
    fsm.to_reviewing()
    assert fsm.state == "REVIEWING"

    # 复核通过 -> 成功
    fsm.to_success()
    assert fsm.state == "SUCCESS"


def test_fsm_repair_loop_and_escalate():
    fsm = TaskLifecycleFSM(max_repair_attempts=3)
    fsm.to_planning()
    fsm.to_developing()
    fsm.to_testing()

    # 修复 1
    assert fsm.can_repair() is True
    fsm.to_repairing()
    assert fsm.repair_attempts == 1
    assert fsm.state == "REPAIRING"

    fsm.to_testing()
    # 修复 2
    fsm.to_repairing()
    assert fsm.repair_attempts == 2

    fsm.to_testing()
    # 修复 3
    fsm.to_repairing()
    assert fsm.repair_attempts == 3

    fsm.to_testing()
    # 达到3次修复上限，不能再自修，必须升级
    assert fsm.can_repair() is False
    assert fsm.exceeded_repair_limit() is True

    fsm.to_escalating_from_test()
    assert fsm.state == "ESCALATING"

    # Codex 遇到额度耗尽
    fsm.to_codex_backlogged()
    assert fsm.state == "CODEX_BACKLOGGED"

    # 优雅收敛至成功（打待补审标记）
    fsm.to_success()
    assert fsm.state == "SUCCESS"
