import pytest
from sqlmodel import create_engine, Session, SQLModel
from orchestrator.codex_guard import CodexQuotaGuard
from config.settings import CodexConfig
from storage.models import Task, PendingCodexAudit
from agents.codex_expert import CodexExpert


@pytest.fixture
def in_memory_session():
    test_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as session:
        yield session


def test_codex_quota_fsm_and_exhaustion(in_memory_session):
    cfg = CodexConfig()
    guard = CodexQuotaGuard(cfg, in_memory_session)

    # 初始状态
    assert guard.state == "READY"
    assert guard.is_available() is True

    # 模拟任务
    task = Task(task_id="t_001", title="Task 1", requirement="Req")
    in_memory_session.add(task)
    in_memory_session.commit()

    # 触发额度耗尽
    audit = guard.handle_quota_exhausted(
        task_id="t_001",
        commit_sha="abcdef1",
        severity="P1",
        issue_description="Complex recursive lock conflict",
        git_diff_summary="diff --git a/lock.py",
        reason="429 Rate limit exceeded"
    )

    assert guard.state == "EXHAUSTED"
    assert guard.is_available() is False
    assert audit.status == "QUEUED"
    assert audit.severity == "P1"

    # 验证任务标记被更新
    in_memory_session.refresh(task)
    assert task.has_pending_codex_audit is True

    # 验证待补审队列
    pending_list = guard.get_pending_audits()
    assert len(pending_list) == 1
    assert pending_list[0].audit_id == audit.audit_id


@pytest.mark.asyncio
async def test_codex_backlog_recovery(in_memory_session):
    cfg = CodexConfig()
    guard = CodexQuotaGuard(cfg, in_memory_session)

    task = Task(task_id="t_002", title="Task 2", requirement="Req")
    in_memory_session.add(task)
    in_memory_session.commit()

    guard.handle_quota_exhausted(
        task_id="t_002",
        commit_sha="abcdef2",
        severity="P1",
        issue_description="Severe memory leak",
        git_diff_summary="diff --git a/mem.py"
    )

    assert guard.state == "EXHAUSTED"

    # 模拟额度恢复后运行补审
    mock_expert = CodexExpert(mock_mode=True)
    results = await guard.process_backlog(mock_expert)

    assert len(results) == 1
    assert results[0]["status"] == "RESOLVED"
    assert guard.state == "READY"
    assert len(guard.get_pending_audits()) == 0
