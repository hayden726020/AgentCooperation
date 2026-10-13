import pytest
from sqlmodel import create_engine, Session, SQLModel
from orchestrator.budget_guard import BudgetGuard
from config.settings import BudgetConfig
from storage.models import Task, MonthlySpending


@pytest.fixture
def in_memory_session():
    test_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as session:
        yield session


def test_budget_thresholds(in_memory_session):
    cfg = BudgetConfig(
        monthly_cny=200.0,
        warning_cny=160.0,
        pause_new_cny=190.0,
        hard_stop_cny=200.0,
        task_limit_cny=10.0
    )
    guard = BudgetGuard(cfg, in_memory_session)

    # 1. 初始状态允许
    can_start, msg = guard.check_can_start_new_task()
    assert can_start is True
    assert "Budget OK" in msg

    # 2. 达到警告线 (¥165)
    from sqlmodel import select
    spending = in_memory_session.exec(select(MonthlySpending)).first()
    spending.total_cost_cny = 165.0
    in_memory_session.add(spending)
    in_memory_session.commit()

    can_start, msg = guard.check_can_start_new_task()
    assert can_start is True
    assert "Budget Warning" in msg

    # 3. 达到暂停线 (¥192)
    spending.total_cost_cny = 192.0
    in_memory_session.add(spending)
    in_memory_session.commit()

    can_start, msg = guard.check_can_start_new_task()
    assert can_start is False
    assert "Pause New Tasks" in msg

    # 4. 达到硬停止线 (¥200.5)
    spending.total_cost_cny = 200.5
    in_memory_session.add(spending)
    in_memory_session.commit()


    can_start, msg = guard.check_can_start_new_task()
    assert can_start is False
    assert "Hard Stop" in msg


def test_task_budget_limit(in_memory_session):
    cfg = BudgetConfig(task_limit_cny=10.0)
    guard = BudgetGuard(cfg, in_memory_session)

    task = Task(
        task_id="test_001",
        title="Test Task",
        requirement="None",
        total_cost_cny=9.5
    )
    
    can_spend, _ = guard.check_task_budget(task, estimated_cost=0.4)
    assert can_spend is True

    can_spend, msg = guard.check_task_budget(task, estimated_cost=1.0)
    assert can_spend is False
    assert "Task limit exceeded" in msg
