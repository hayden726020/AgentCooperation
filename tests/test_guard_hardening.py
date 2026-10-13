import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import SQLModel, Session, create_engine, select

from agents.base import AgentResult
from agents.codex_expert import CodexExpert
from config.settings import BudgetConfig, CodexConfig, ModelPricing
from orchestrator.budget_guard import BudgetGuard
from orchestrator.codex_guard import CodexQuotaGuard
from storage.models import Task, MonthlySpending, PendingCodexAudit


@pytest.fixture
def session():
    engine = create_engine('sqlite:///:memory:')
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Task(task_id='task', title='test', requirement='test', state='SUCCESS'))
        session.commit()
        yield session


def test_reservations_limit_task_and_month_and_settle_once(session):
    guard = BudgetGuard(BudgetConfig(), session)
    first = guard.reserve_call('task', 6)
    with pytest.raises(RuntimeError, match='Task'):
        guard.reserve_call('task', 5)
    guard.settle_call(first, 12, 6, 4)
    guard.settle_call(first, 12, 6, 4)
    assert session.exec(select(MonthlySpending)).one().total_cost_cny == 4
    second = guard.reserve_call('task', 6)
    guard.release_call(second)
    guard.release_call(second)
    assert guard.reserve_call('task', 6)


def test_monthly_inflight_prevents_overspend(session):
    session.add(MonthlySpending(year_month=datetime.now(timezone.utc).strftime('%Y-%m'), total_cost_cny=198))
    session.commit()
    guard = BudgetGuard(BudgetConfig(), session)
    guard.reserve_call('task', 1.5)
    with pytest.raises(RuntimeError, match='Monthly'):
        guard.reserve_call('task', 1)


def test_simulated_settlement_does_not_charge_real_month(session):
    guard = BudgetGuard(BudgetConfig(), session)
    reservation = guard.reserve_call('task', 1, simulated=True)
    guard.settle_call(reservation, 10, 10, 0.5)
    assert session.exec(select(MonthlySpending)).first() is None
    session.expire_all()
    assert session.exec(select(Task)).one().total_cost_cny == 0.5


def test_worst_case_estimate_uses_peak_prices(session):
    guard = BudgetGuard(BudgetConfig(), session)
    pricing = ModelPricing(model_id='test', input_price_per_m_usd=.15, output_price_per_m_usd=.6,
                           peak_input_price_per_m_usd=.3, peak_output_price_per_m_usd=1.2)
    assert guard.estimate_call_cost(pricing, 1_000_000, 1_000_000) == pytest.approx(1.5 * 7.25)


def test_reservations_serialize_competing_sessions(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "budget.db"}', connect_args={'check_same_thread': False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Task(task_id='task', title='test', requirement='test'))
        s.commit()
    def reserve(_):
        with Session(engine) as s:
            try:
                return BudgetGuard(BudgetConfig(), s).reserve_call('task', 6)
            except RuntimeError:
                return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(item is not None for item in pool.map(reserve, range(2))) == 1


def queue(guard, count=1):
    return [guard.handle_quota_exhausted('task', 'sha', 'P1', f'issue {i}', 'full diff' * 500,
             worktree_path='/isolated/task', test_summary='tests passed', issue_context='complete evidence')
            for i in range(count)]


def test_reserved_survives_reload(session):
    guard = CodexQuotaGuard(CodexConfig(), session)
    guard.reserve()
    assert guard.is_available() is False
    assert CodexQuotaGuard(CodexConfig(), session).state == 'RESERVED'
    guard.release()
    assert CodexQuotaGuard(CodexConfig(), session).state == 'READY'


class ResultExpert:
    def __init__(self, result):
        self.result = result
        self.contexts = []
    async def execute(self, prompt, context=None):
        self.contexts.append((prompt, context))
        return self.result


@pytest.mark.asyncio
async def test_failed_backlog_stays_open(session):
    guard = CodexQuotaGuard(CodexConfig(), session)
    queue(guard)
    expert = ResultExpert(AgentResult(success=False, content='', error='login expired'))
    result = await guard.process_backlog(expert)
    assert result[0]['status'] == 'FAILED'
    assert len(guard.get_pending_audits()) == 1
    assert guard.state != 'READY'


@pytest.mark.asyncio
async def test_multiple_audits_resolve_and_clear_task_flag(session):
    guard = CodexQuotaGuard(CodexConfig(), session)
    queue(guard, 2)
    expert = ResultExpert(AgentResult(success=True, content='approved', metadata={'audit': {'approved': True, 'severity': 'P3'}}))
    results = await guard.process_backlog(expert)
    assert [r['status'] for r in results] == ['RESOLVED', 'RESOLVED']
    assert expert.contexts[0][1]['worktree_path'] == '/isolated/task'
    assert 'full diff' * 500 in expert.contexts[0][0]
    session.expire_all()
    assert session.exec(select(Task)).one().has_pending_codex_audit is False
    assert guard.state == 'READY'


@pytest.mark.asyncio
async def test_backlog_findings_require_repair(session):
    guard = CodexQuotaGuard(CodexConfig(), session)
    queue(guard)
    expert = ResultExpert(AgentResult(success=True, content='unsafe', metadata={'audit': {'approved': False}}))
    results = await guard.process_backlog(expert)
    assert results[0]['status'] == 'NEEDS_REPAIR'
    assert session.exec(select(PendingCodexAudit)).one().status == 'NEEDS_REPAIR'
    assert session.exec(select(Task)).one().has_pending_codex_audit is True


@pytest.mark.asyncio
async def test_probe_waits_until_reset_and_checks_success(session):
    guard = CodexQuotaGuard(CodexConfig(), session)
    queue(guard)
    guard.set_quota_state('EXHAUSTED', recovery_at=datetime.now(timezone.utc) + timedelta(hours=1))
    expert = ResultExpert(AgentResult(success=True, content='ready'))
    assert await guard.probe_recovery(expert) is False
    assert not expert.contexts
    assert await guard.probe_recovery(expert, force=True) is True
    assert CodexQuotaGuard(CodexConfig(), session).state == 'READY'

@pytest.mark.asyncio
async def test_default_sessions_support_detached_audits(tmp_path, monkeypatch):
    import storage.db
    engine = create_engine(f'sqlite:///{tmp_path / "sessions.db"}')
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(storage.db, 'engine', engine)
    try:
        with Session(engine) as isolated:
            isolated.add(Task(task_id='task', title='test', requirement='test', state='SUCCESS'))
            isolated.commit()
        guard = CodexQuotaGuard(CodexConfig())
        audit = guard.handle_quota_exhausted('task', 'sha', 'P1', 'issue', 'diff')
        assert audit.audit_id
        result = await guard.process_backlog(ResultExpert(AgentResult(
            success=True, content='approved', metadata={'review': {'approved': True, 'severity': 'P3'}})))
        assert result[0]['status'] == 'RESOLVED'
    finally:
        engine.dispose()

@pytest.mark.parametrize('changes', [
    {'monthly_cny': -1}, {'task_limit_cny': float('nan')},
    {'usd_to_cny_rate': 0}, {'warning_cny': 195, 'pause_new_cny': 190},
])
def test_budget_configuration_rejects_unsafe_values(changes):
    with pytest.raises(ValueError):
        BudgetConfig(**changes)


def test_model_price_rejects_nonfinite_and_negative():
    with pytest.raises(ValueError):
        ModelPricing(model_id='test', peak_input_price_per_m_usd=float('nan'))
    with pytest.raises(ValueError):
        ModelPricing(model_id='test', output_price_per_m_usd=-1)

@pytest.mark.asyncio
async def test_nonweekly_probe_failure_preserves_recovering(session):
    guard = CodexQuotaGuard(CodexConfig(), session)
    queue(guard)
    expert = ResultExpert(AgentResult(success=False, content='', error='timeout'))
    assert await guard.probe_recovery(expert, force=True) is False
    assert CodexQuotaGuard(CodexConfig(), session).state == 'RECOVERING'
    assert len(guard.get_pending_audits()) == 1


@pytest.mark.asyncio
async def test_weekly_failure_during_backlog_keeps_all_pending(session):
    from agents.codex_expert import CodexQuotaExhaustedError
    class Exhausted:
        async def execute(self, prompt, context=None):
            raise CodexQuotaExhaustedError('weekly limit')
    guard = CodexQuotaGuard(CodexConfig(), session)
    queue(guard, 2)
    result = await guard.process_backlog(Exhausted())
    assert result[0]['status'] == 'STILL_EXHAUSTED'
    assert len(guard.get_pending_audits()) == 2
    assert guard.state == 'EXHAUSTED'
    assert await guard.probe_recovery(Exhausted(), force=True) is False
    assert guard.state == 'EXHAUSTED'


@pytest.mark.asyncio
async def test_cancelled_audit_returns_to_queue(session):
    class Cancelled:
        async def execute(self, prompt, context=None):
            raise asyncio.CancelledError
    guard = CodexQuotaGuard(CodexConfig(), session)
    queue(guard)
    with pytest.raises(asyncio.CancelledError):
        await guard.process_backlog(Cancelled())
    assert len(guard.get_pending_audits()) == 1


@pytest.mark.asyncio
async def test_unstructured_advice_does_not_resolve(session):
    guard = CodexQuotaGuard(CodexConfig(), session)
    queue(guard)
    result = await guard.process_backlog(ResultExpert(AgentResult(success=True, content='Looks fine')))
    assert result[0]['status'] == 'NEEDS_REPAIR'


def test_exhaustion_idempotence_and_abandoned_claim_recovery(session):
    from storage.models import PendingAuditContext
    guard = CodexQuotaGuard(CodexConfig(), session)
    first = queue(guard)[0]
    second = queue(guard)[0]
    assert first.audit_id == second.audit_id
    assert len(guard.get_pending_audits()) == 1
    assert guard._claim(first.audit_id)
    assert guard._claim(first.audit_id) is None
    evidence = session.exec(select(PendingAuditContext)).one()
    evidence.claimed_at = datetime.now(timezone.utc) - timedelta(hours=1)
    session.add(evidence)
    session.commit()
    guard.recover_stale_audits()
    assert len(guard.get_pending_audits()) == 1


def test_budget_uncertain_spend_is_charged_reserved_bound(session):
    guard = BudgetGuard(BudgetConfig(), session)
    reservation = guard.reserve_call('task', 2)
    guard.settle_call(reservation, 0, 0)
    assert session.exec(select(MonthlySpending)).one().total_cost_cny == 2
    for invalid in [-1, float('nan'), float('inf')]:
        with pytest.raises(ValueError):
            guard.reserve_call('task', invalid)
    with pytest.raises(ValueError):
        guard.reserve_call('unknown', 1)
    with pytest.raises(ValueError):
        guard.settle_call('unknown', 0, 0, 1)
    with pytest.raises(ValueError):
        guard.release_call('unknown')
    released = guard.reserve_call('task', 1)
    guard.release_call(released)
    with pytest.raises(ValueError):
        guard.settle_call(released, 0, 0, 1)


def test_new_tables_do_not_require_altering_old_task_schema(tmp_path):
    from storage.db import init_db
    engine = create_engine(f'sqlite:///{tmp_path / "old.db"}')
    Task.__table__.create(engine)
    with engine.begin() as connection:
        original = connection.exec_driver_sql('PRAGMA table_info(tasks)').all()
    init_db(engine)
    with engine.begin() as connection:
        assert connection.exec_driver_sql('PRAGMA table_info(tasks)').all() == original
        assert connection.exec_driver_sql('SELECT COUNT(*) FROM budget_reservations').scalar() == 0
    engine.dispose()

@pytest.mark.asyncio
async def test_incomplete_structured_approval_does_not_close_audit(session):
    guard = CodexQuotaGuard(CodexConfig(), session)
    queue(guard)
    result = await guard.process_backlog(ResultExpert(AgentResult(
        success=True, content='ok', metadata={'review': {'approved': True}})))
    assert result[0]['status'] == 'NEEDS_REPAIR'

@pytest.mark.asyncio
async def test_probe_uses_explicit_cwd_and_backlog_requests_schema(session, tmp_path):
    from pathlib import Path
    class ProbeExpert:
        async def execute(self, prompt, context=None):
            cwd = Path(context['worktree_path'])
            assert cwd.is_dir()
            assert cwd == tmp_path
            assert cwd != Path.cwd()
            return AgentResult(success=True, content='READY')
    guard = CodexQuotaGuard(CodexConfig(), session)
    queue(guard)
    assert await guard.probe_recovery(ProbeExpert(), force=True, worktree_path=tmp_path)
    expert = ResultExpert(AgentResult(success=True, content='approved', metadata={
        'review': {'approved': True, 'severity': 'P3'}}))
    await guard.process_backlog(expert)
    assert expert.contexts[0][1]['structured_review'] is True

@pytest.mark.asyncio
async def test_reserved_state_does_not_start_background_audit(session):
    guard = CodexQuotaGuard(CodexConfig(), session)
    queue(guard)
    guard.set_quota_state('RESERVED')
    expert = ResultExpert(AgentResult(success=True, content='approved', metadata={
        'review': {'approved': True, 'severity': 'P3'}}))
    assert await guard.process_backlog(expert) == []
    assert not expert.contexts
    assert len(guard.get_pending_audits()) == 1


def test_stale_guard_cannot_overwrite_exhaustion(session):
    from transitions import MachineError
    first = CodexQuotaGuard(CodexConfig(), session)
    stale = CodexQuotaGuard(CodexConfig(), session)
    first.mark_exhausted()
    with pytest.raises(MachineError):
        stale.reserve()
    assert CodexQuotaGuard(CodexConfig(), session).state == 'EXHAUSTED'


@pytest.mark.asyncio
async def test_pending_evidence_tracks_final_patch(session):
    from storage.models import PendingAuditContext
    guard = CodexQuotaGuard(CodexConfig(), session)
    queue(guard)
    guard.update_pending_evidence('task', 'final-sha', 'final complete diff', 'final tests', 'final review')
    audit = session.exec(select(PendingCodexAudit)).one()
    assert audit.commit_sha == 'final-sha'
    assert audit.issue_description == 'issue 0'
    expert = ResultExpert(AgentResult(success=True, content='approved', metadata={
        'review': {'approved': True, 'severity': 'P3'}}))
    await guard.process_backlog(expert)
    prompt, context = expert.contexts[0]
    assert context['expected_commit_sha'] == 'final-sha'
    assert 'final complete diff' in prompt
    assert 'final tests' in prompt

@pytest.mark.asyncio
async def test_mock_results_cannot_verify_real_quota_or_close_real_audit(session):
    guard = CodexQuotaGuard(CodexConfig(), session)
    queue(guard)
    result = await guard.process_backlog(CodexExpert(mock_mode=True))
    assert result[0]['status'] == 'SIMULATED'
    assert guard.state == 'EXHAUSTED'
    assert len(guard.get_pending_audits()) == 1
    assert session.exec(select(Task)).one().has_pending_codex_audit is True
    assert await guard.probe_recovery(CodexExpert(mock_mode=True), force=True) is False
    assert guard.state == 'EXHAUSTED'

@pytest.mark.asyncio
async def test_explicit_simulation_on_isolated_fixture_can_complete(session):
    guard = CodexQuotaGuard(CodexConfig(), session)
    queue(guard)
    assert await guard.probe_recovery(CodexExpert(mock_mode=True), force=True, allow_simulated=True)
    result = await guard.process_backlog(CodexExpert(mock_mode=True), allow_simulated=True)
    assert result[0]['status'] == 'RESOLVED'


@pytest.mark.asyncio
async def test_metadata_mock_cannot_bypass_simulation_guard(session):
    guard = CodexQuotaGuard(CodexConfig(), session)
    queue(guard)
    expert = ResultExpert(AgentResult(success=True, content='simulated approval', metadata={
        'mock': True, 'review': {'approved': True, 'severity': 'P3'}}))
    result = await guard.process_backlog(expert)
    assert result[0]['status'] == 'SIMULATED'
    assert guard.state == 'EXHAUSTED'
    assert len(guard.get_pending_audits()) == 1
    assert await guard.probe_recovery(expert, force=True) is False
    assert guard.state == 'EXHAUSTED'

@pytest.mark.asyncio
async def test_running_task_is_not_claimed_by_backlog_watcher(session):
    guard = CodexQuotaGuard(CodexConfig(), session)
    queue(guard)
    task = session.exec(select(Task)).one()
    task.state = 'REPAIRING'
    session.add(task)
    session.commit()
    expert = ResultExpert(AgentResult(success=True, content='approved', metadata={
        'review': {'approved': True, 'severity': 'P3'}}))
    assert await guard.process_backlog(expert) == []
    assert not expert.contexts
    assert len(guard.get_pending_audits()) == 1
