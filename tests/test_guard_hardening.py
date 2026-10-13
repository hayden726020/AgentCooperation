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
        session.add(Task(task_id='task', title='test', requirement='test'))
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
    expert = ResultExpert(AgentResult(success=True, content='approved', metadata={'audit': {'approved': True}}))
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
