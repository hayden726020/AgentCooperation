"""Atomic API budget reservations and accounting (SQLite, CNY)."""
import math
import uuid
from typing import Optional, Tuple

from sqlmodel import Session, select

from config.settings import BudgetConfig, ModelPricing
from storage.db import get_or_create_monthly_spending, provide_session, locked_session
from storage.models import Task, MonthlySpending, BudgetReservation, utc_now


class BudgetLimitError(RuntimeError):
    pass


class BudgetGuard:
    def __init__(self, budget_cfg: BudgetConfig, session: Optional[Session] = None):
        self.cfg = budget_cfg
        self.session = session

    def _provide_session(self):
        return provide_session(self.session)

    @staticmethod
    def _amount(value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError('Cost must be finite and nonnegative')
        return value

    @staticmethod
    def _pending(s, *, task_id=None, year_month=None, simulated=False):
        stmt = select(BudgetReservation).where(
            BudgetReservation.status == 'RESERVED', BudgetReservation.simulated == simulated)
        if task_id is not None:
            stmt = stmt.where(BudgetReservation.task_id == task_id)
        if year_month is not None:
            stmt = stmt.where(BudgetReservation.year_month == year_month)
        return sum(item.estimated_cost_cny for item in s.exec(stmt))

    def check_can_start_new_task(self, urgent: bool = False) -> Tuple[bool, str]:
        with self._provide_session() as s:
            spending = get_or_create_monthly_spending(s)
            cost = spending.total_cost_cny + self._pending(s, year_month=spending.year_month)
            if cost >= self.cfg.hard_stop_cny:
                return False, f'Hard Stop: Monthly spending/reservations ¥{cost:.2f}'
            if not urgent and cost >= self.cfg.pause_new_cny:
                return False, f'Pause New Tasks: Monthly spending/reservations ¥{cost:.2f}'
            if cost >= self.cfg.warning_cny:
                return True, f'Budget Warning: Monthly spending/reservations ¥{cost:.2f}'
            return True, f'Budget OK: Monthly spending/reservations ¥{cost:.2f}'

    def check_task_budget(self, task: Task, estimated_cost: float = 0.0) -> Tuple[bool, str]:
        self._amount(estimated_cost)
        if task.total_cost_cny + estimated_cost > self.cfg.task_limit_cny:
            return False, f'Task limit exceeded: Task {task.task_id}'
        return True, 'OK'

    def estimate_call_cost(self, pricing: ModelPricing, prompt_tokens: int, max_output_tokens: int = 8192) -> float:
        if prompt_tokens < 0 or max_output_tokens < 0:
            raise ValueError('Token counts must be nonnegative')
        # Cache discounts never reduce the pre-call upper bound.
        input_rate = max(pricing.input_price_per_m_usd, pricing.peak_input_price_per_m_usd or 0)
        output_rate = max(pricing.output_price_per_m_usd, pricing.peak_output_price_per_m_usd or 0)
        return self._amount((prompt_tokens * input_rate + max_output_tokens * output_rate)
                            / 1_000_000 * self.cfg.usd_to_cny_rate)

    def reserve_call(self, task_id: str, estimated_cost: float, *, simulated: bool = False) -> str:
        self._amount(estimated_cost)
        with locked_session(self.session) as s:
            task = s.exec(select(Task).where(Task.task_id == task_id)).first()
            if task is None:
                raise ValueError(f'Unknown task {task_id}')
            task_projected = task.total_cost_cny + self._pending(s, task_id=task_id, simulated=simulated) + estimated_cost
            if task_projected > self.cfg.task_limit_cny:
                raise BudgetLimitError(f'Task limit exceeded: {task_id} projected ¥{task_projected:.5f}')
            month = utc_now().strftime('%Y-%m')
            if not simulated:
                spending = s.exec(select(MonthlySpending).where(MonthlySpending.year_month == month)).first()
                current = spending.total_cost_cny if spending else 0
                projected = current + self._pending(s, year_month=month) + estimated_cost
                if projected > min(self.cfg.monthly_cny, self.cfg.hard_stop_cny) or current >= self.cfg.hard_stop_cny:
                    raise BudgetLimitError(f'Monthly limit exceeded: projected ¥{projected:.5f}')
            reservation_id = uuid.uuid4().hex
            s.add(BudgetReservation(reservation_id=reservation_id, task_id=task_id, year_month=month,
                                   estimated_cost_cny=estimated_cost, simulated=simulated))
        return reservation_id

    def settle_call(self, reservation_id: str, prompt_tokens: int, completion_tokens: int,
                    cost_cny: Optional[float] = None):
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ValueError('Token counts must be nonnegative')
        if cost_cny is not None:
            self._amount(cost_cny)
        with locked_session(self.session) as s:
            reservation = s.exec(select(BudgetReservation).where(
                BudgetReservation.reservation_id == reservation_id)).first()
            if reservation is None:
                raise ValueError('Unknown budget reservation')
            if reservation.status == 'SETTLED':
                return reservation.actual_cost_cny
            if reservation.status != 'RESERVED':
                raise ValueError('Cannot settle a released budget reservation')
            actual = reservation.estimated_cost_cny if cost_cny is None else cost_cny
            self._record(s, reservation.task_id, prompt_tokens, completion_tokens, actual,
                         reservation.year_month, reservation.simulated)
            reservation.actual_cost_cny = actual
            reservation.status = 'SETTLED'
            s.add(reservation)
        return actual

    def release_call(self, reservation_id: str):
        """Only release when no billable call was made; uncertain calls must settle."""
        with locked_session(self.session) as s:
            reservation = s.exec(select(BudgetReservation).where(
                BudgetReservation.reservation_id == reservation_id)).first()
            if reservation is None:
                raise ValueError('Unknown budget reservation')
            if reservation.status == 'RESERVED':
                reservation.status = 'RELEASED'
                s.add(reservation)

    @staticmethod
    def _record(s, task_id, prompt_tokens, completion_tokens, cost, month, simulated):
        if not simulated:
            spending = s.exec(select(MonthlySpending).where(MonthlySpending.year_month == month)).first()
            if spending is None:
                spending = MonthlySpending(year_month=month)
            spending.total_prompt_tokens += prompt_tokens
            spending.total_completion_tokens += completion_tokens
            spending.total_cost_cny = round(spending.total_cost_cny + cost, 8)
            spending.updated_at = utc_now()
            s.add(spending)
        task = s.exec(select(Task).where(Task.task_id == task_id)).first()
        if task:
            task.total_prompt_tokens += prompt_tokens
            task.total_completion_tokens += completion_tokens
            task.total_cost_cny = round(task.total_cost_cny + cost, 8)
            task.updated_at = utc_now()
            s.add(task)

    def record_spending(self, task_id: str, prompt_tokens: int, completion_tokens: int,
                        cost_cny: float, *, simulated: bool = False):
        """Compatibility accounting; paid execution should use reservations instead."""
        self._amount(cost_cny)
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ValueError('Token counts must be nonnegative')
        with locked_session(self.session) as s:
            self._record(s, task_id, prompt_tokens, completion_tokens, cost_cny,
                         utc_now().strftime('%Y-%m'), simulated)
