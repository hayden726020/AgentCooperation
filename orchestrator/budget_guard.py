from storage.db import get_or_create_monthly_spending, provide_session
from storage.models import Task
from config.settings import BudgetConfig


class BudgetGuard:
    """
    预算守护器：
    执行精准的两级预算检查（单任务 ¥10 上限，月度 ¥200 上限），
    在支出接近阈值时触发阶梯式防御：
    - ¥160: 告警（日志与提醒）
    - ¥190: 暂停新建非紧急 API 任务
    - ¥200: 硬停止，禁止产生任何 API 费用
    """

    def __init__(self, budget_cfg: BudgetConfig, session: Session = None):
        self.cfg = budget_cfg
        self.session = session

    def _provide_session(self):
        return provide_session(self.session)

    def check_can_start_new_task(self) -> Tuple[bool, str]:
        """检查月度预算是否允许开启新任务"""
        with self._provide_session() as s:
            spending = get_or_create_monthly_spending(s)
            current_cost = spending.total_cost_cny


            if current_cost >= self.cfg.hard_stop_cny:
                return (
                    False,
                    f"Hard Stop: Monthly spending (¥{current_cost:.2f}) reached or exceeded limit (¥{self.cfg.hard_stop_cny:.2f})."
                )

            if current_cost >= self.cfg.pause_new_cny:
                return (
                    False,
                    f"Pause New Tasks: Monthly spending (¥{current_cost:.2f}) reached safety threshold (¥{self.cfg.pause_new_cny:.2f})."
                )

            if current_cost >= self.cfg.warning_cny:
                # 仍允许执行，但带警告
                return (
                    True,
                    f"Budget Warning: Monthly spending (¥{current_cost:.2f}) is in economy range (>= ¥{self.cfg.warning_cny:.2f})."
                )

            return (True, f"Budget OK: Monthly spending is ¥{current_cost:.2f} / ¥{self.cfg.monthly_cny:.2f}.")

    def check_task_budget(self, task: Task, estimated_cost: float = 0.0) -> Tuple[bool, str]:
        """检查单个任务累计支出是否超过单任务限额"""
        projected = task.total_cost_cny + estimated_cost
        if projected > self.cfg.task_limit_cny:
            return (
                False,
                f"Task limit exceeded: Task {task.task_id} projected cost ¥{projected:.2f} exceeds limit ¥{self.cfg.task_limit_cny:.2f}."
            )
        return (True, "OK")

    def record_spending(
        self,
        task_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_cny: float
    ):
        """将一次 API 调用的消耗持久化计入数据库"""
        with self._provide_session() as s:
            # 更新月度统计
            spending = get_or_create_monthly_spending(s)
            spending.total_prompt_tokens += prompt_tokens
            spending.total_completion_tokens += completion_tokens
            spending.total_cost_cny = round(spending.total_cost_cny + cost_cny, 5)
            s.add(spending)


            # 更新任务统计
            from sqlmodel import select
            stmt = select(Task).where(Task.task_id == task_id)
            task = s.exec(stmt).first()
            if task:
                task.total_prompt_tokens += prompt_tokens
                task.total_completion_tokens += completion_tokens
                task.total_cost_cny = round(task.total_cost_cny + cost_cny, 5)
                s.add(task)

            s.commit()
