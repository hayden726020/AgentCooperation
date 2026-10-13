import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Literal
from sqlmodel import Session, select
import uuid

from config.settings import settings, PolicyConfig
from storage.db import get_session, init_db
from storage.models import Task, ExecutionLog
from execution.worktree import WorktreeManager
from execution.test_runner import TestRunner, TestResult
from orchestrator.fsm import TaskLifecycleFSM
from orchestrator.engine_selector import SingleEngineSlotEnforcer
from orchestrator.budget_guard import BudgetGuard
from orchestrator.codex_guard import CodexQuotaGuard
from agents.gemini_reviewer import GeminiReviewer
from agents.codex_expert import CodexExpert, CodexQuotaExhaustedError


class OrchestratorCore:
    """
    多 Agent 全自动协同中枢：
    负责将有限状态机、单引擎槽位守护、预算控制、独立审查与专家升级无缝结合。
    """

    def __init__(self, repo_root: Optional[Path] = None, session: Optional[Session] = None):
        self.repo_root = repo_root or Path.cwd()
        self.session = session
        self.policy: PolicyConfig = settings.load_policy()
        init_db()

        self.budget_guard = BudgetGuard(self.policy.budget, self.session)
        self.codex_guard = CodexQuotaGuard(self.policy.codex, self.session)
        self.worktree_mgr = WorktreeManager(self.repo_root, self.policy.execution.git_worktree_base)

    def _get_session(self) -> Session:
        return self.session if self.session else get_session()

    def _log_step(
        self,
        task_id: str,
        step_name: str,
        role: str,
        model: str,
        p_tokens: int = 0,
        c_tokens: int = 0,
        cost_cny: float = 0.0,
        status: str = "SUCCESS",
        output_snippet: str = None,
        error_snippet: str = None
    ):
        with self._get_session() as s:
            log = ExecutionLog(
                task_id=task_id,
                step_name=step_name,
                agent_role=role,
                agent_model=model,
                prompt_tokens=p_tokens,
                completion_tokens=c_tokens,
                cost_cny=cost_cny,
                status=status,
                output_snippet=(output_snippet or "")[:500],
                error_snippet=(error_snippet or "")[:500]
            )
            s.add(log)
            s.commit()

    async def run_task(
        self,
        requirement: str,
        title: Optional[str] = None,
        chosen_model: Optional[Literal["glm", "deepseek"]] = None,
        mock_mode: bool = False,
        custom_test_cmd: Optional[str] = None
    ) -> Task:
        # 1. 预算准入检查
        can_start, budget_msg = self.budget_guard.check_can_start_new_task()
        if not can_start:
            raise RuntimeError(f"Task rejected by BudgetGuard: {budget_msg}")

        # 2. 创建任务持久化实体
        task_uuid = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        task_title = title or requirement[:40].replace("\n", " ")
        model_name = chosen_model or self.policy.primary_agent

        task = Task(
            task_id=task_uuid,
            title=task_title,
            requirement=requirement,
            primary_model=model_name,
            state="INIT",
            branch_name=f"task/{task_uuid}"
        )

        with self._get_session() as s:
            s.add(task)
            s.commit()
            s.refresh(task)

        # 3. 严格单引擎槽位激活（GLM 或 DeepSeek 二选一）
        slot_enforcer = SingleEngineSlotEnforcer(self.policy)
        primary_agent = slot_enforcer.get_primary_agent(
            chosen_model=model_name,
            mock_mode=mock_mode
        )

        gemini_reviewer = GeminiReviewer(mock_mode=mock_mode)
        codex_expert = CodexExpert(mock_mode=mock_mode)

        # 4. 创建独立 Git Worktree 环境隔离
        worktree_path = await self.worktree_mgr.create_worktree(task_uuid)
        task.worktree_path = str(worktree_path)
        with self._get_session() as s:
            s.add(task)
            s.commit()
            s.refresh(task)

        # 5. 初始化状态机

        fsm = TaskLifecycleFSM(max_repair_attempts=self.policy.repair.max_normal_attempts)

        def sync_task_state():
            with self._get_session() as s:
                stmt = select(Task).where(Task.task_id == task_uuid)
                t = s.exec(stmt).first()
                if t:
                    t.state = fsm.state
                    t.repair_attempts = fsm.repair_attempts
                    t.updated_at = datetime.now(timezone.utc)
                    s.add(t)
                    s.commit()

        try:
            # === 阶段 1: 需求规划 (PLANNING) ===
            fsm.to_planning()
            sync_task_state()

            plan_prompt = (
                f"You are the Primary Architect. Create a detailed implementation plan for:\n{requirement}\n\n"
                f"Structure your response with TASK.md and SPEC.json files."
            )
            plan_res = await primary_agent.execute(
                plan_prompt,
                context={"worktree_path": worktree_path}
            )

            self.budget_guard.record_spending(
                task_uuid, plan_res.prompt_tokens, plan_res.completion_tokens, plan_res.cost_cny
            )
            self._log_step(
                task_uuid, "Planning", "PRIMARY", primary_agent.model_name,
                plan_res.prompt_tokens, plan_res.completion_tokens, plan_res.cost_cny,
                output_snippet=plan_res.content
            )

            # === 阶段 2: 代码实现与单元测试构建 (DEVELOPING) ===
            fsm.to_developing()
            sync_task_state()

            dev_prompt = (
                f"Requirement: {requirement}\n\n"
                f"Implement the complete, runnable solution and all corresponding unit tests. "
                f"Ensure tests can run with pytest or node."
            )
            dev_res = await primary_agent.execute(
                dev_prompt,
                context={"worktree_path": worktree_path}
            )

            self.budget_guard.record_spending(
                task_uuid, dev_res.prompt_tokens, dev_res.completion_tokens, dev_res.cost_cny
            )
            self._log_step(
                task_uuid, "Developing", "PRIMARY", primary_agent.model_name,
                dev_res.prompt_tokens, dev_res.completion_tokens, dev_res.cost_cny,
                output_snippet=dev_res.content
            )

            # 创建开发完成检查点
            checkpoint_sha = await self.worktree_mgr.commit_checkpoint(
                worktree_path, "feat: primary model generated code and tests"
            )

            # === 阶段 3: 本地自动化测试与自修循环 (TESTING <-> REPAIRING) ===
            test_runner = TestRunner(worktree_path, custom_test_cmd=custom_test_cmd)
            
            while True:
                fsm.to_testing()
                sync_task_state()

                test_res: TestResult = await test_runner.run()
                self._log_step(
                    task_uuid, f"Testing (attempt {fsm.repair_attempts})", "TEST_RUNNER", "LocalRunner",
                    status="SUCCESS" if test_res.passed else "FAILED",
                    output_snippet=test_res.summary,
                    error_snippet=test_res.raw_output
                )

                if test_res.passed:
                    break

                # 测试失败，检查是否还能自修
                if fsm.can_repair():
                    fsm.to_repairing()
                    sync_task_state()

                    repair_prompt = (
                        f"Tests failed on attempt {fsm.repair_attempts}/{fsm.max_repair_attempts}!\n"
                        f"=== Test Output ===\n{test_res.raw_output[:2000]}\n\n"
                        f"Analyze the failures and fix the code/tests."
                    )
                    repair_res = await primary_agent.execute(
                        repair_prompt,
                        context={"worktree_path": worktree_path}
                    )

                    self.budget_guard.record_spending(
                        task_uuid, repair_res.prompt_tokens, repair_res.completion_tokens, repair_res.cost_cny
                    )
                    self._log_step(
                        task_uuid, f"Repair #{fsm.repair_attempts}", "PRIMARY", primary_agent.model_name,
                        repair_res.prompt_tokens, repair_res.completion_tokens, repair_res.cost_cny,
                        output_snippet=repair_res.content
                    )

                    await self.worktree_mgr.commit_checkpoint(
                        worktree_path, f"fix: auto-repair attempt #{fsm.repair_attempts}"
                    )
                else:
                    # 超过3次自修上限，升级到 Codex 专家
                    fsm.to_escalating_from_test()
                    sync_task_state()
                    break

            # === 阶段 4: 独立复核 (REVIEWING) 或 专家升级 (ESCALATING) ===
            if fsm.state == "TESTING":
                # 测试已通过，拉起 Gemini CLI 做独立代码审查
                fsm.to_reviewing()
                sync_task_state()

                git_diff = await self.worktree_mgr.get_diff(worktree_path)
                review_res = await gemini_reviewer.execute(
                    prompt="Review changes for architecture, performance and boundary cases",
                    context={
                        "git_diff": git_diff,
                        "test_summary": test_res.summary
                    }
                )

                self._log_step(
                    task_uuid, "Gemini Review", "REVIEWER", "Gemini-CLI",
                    output_snippet=review_res.content
                )

                review_meta = review_res.metadata.get("review", {})
                severity = review_meta.get("severity", "P3")
                approved = review_meta.get("approved", True)

                if (severity in ["P0", "P1"]) or (not approved and "conflict" in review_res.content.lower()):
                    # 发现严重隐患，升级到 Codex
                    fsm.to_escalating_from_review()
                    sync_task_state()
                else:
                    fsm.to_success()
                    sync_task_state()

            # === 阶段 5: 处理专家升级与额度守护 (ESCALATING) ===
            if fsm.state == "ESCALATING":
                git_diff = await self.worktree_mgr.get_diff(worktree_path)
                try:
                    codex_res = await codex_expert.execute(
                        prompt="Provide authoritative architecture guidance and risk resolution.",
                        context={"issue_context": f"Tests passed={test_res.passed}. Git diff:\n{git_diff[:2000]}"}
                    )
                    self._log_step(
                        task_uuid, "Codex Escalation", "EXPERT", "Codex-CLI",
                        output_snippet=codex_res.content
                    )

                    # Codex 给出方案后，由 Primary Flash 引擎完成补丁应用
                    patch_prompt = f"Expert advice from Codex:\n{codex_res.content}\nPlease apply the recommended fix."
                    patch_res = await primary_agent.execute(patch_prompt, context={"worktree_path": worktree_path})
                    await self.worktree_mgr.commit_checkpoint(worktree_path, "fix: applied codex expert advice")
                    
                    fsm.to_success()
                    sync_task_state()

                except CodexQuotaExhaustedError as quota_err:
                    # Codex 周额度耗尽！启动优雅降级：
                    # 1. 记录 PendingCodexAudit 到数据库
                    # 2. 状态机切入 CODEX_BACKLOGGED
                    # 3. 允许任务顺利完成并打上待补审标记
                    self.codex_guard.handle_quota_exhausted(
                        task_id=task_uuid,
                        commit_sha=checkpoint_sha or "HEAD",
                        severity="P1",
                        issue_description=f"Auto-escalated issue after attempts={fsm.repair_attempts}",
                        git_diff_summary=git_diff[:1000],
                        reason=str(quota_err)
                    )

                    fsm.to_codex_backlogged()
                    sync_task_state()

                    # 标记任务收敛完成
                    fsm.to_success()
                    sync_task_state()

            # 最终结算任务状态
            with self._get_session() as s:
                stmt = select(Task).where(Task.task_id == task_uuid)
                final_task = s.exec(stmt).first()
                if final_task:
                    final_task.state = "SUCCESS" if fsm.state == "SUCCESS" else fsm.state
                    final_task.completed_at = datetime.now(timezone.utc)
                    final_task.result_summary = f"Task completed with final state: {fsm.state}"
                    s.add(final_task)
                    s.commit()
                    s.refresh(final_task)
                    return final_task

        except Exception as e:
            fsm.to_failed()
            sync_task_state()
            self._log_step(
                task_uuid, "Fatal Exception", "SYSTEM", "Orchestrator",
                status="FAILED",
                error_snippet=str(e)
            )
            with self._get_session() as s:
                stmt = select(Task).where(Task.task_id == task_uuid)
                err_task = s.exec(stmt).first()
                if err_task:
                    err_task.state = "FAILED"
                    err_task.result_summary = f"Execution failed: {str(e)}"
                    s.add(err_task)
                    s.commit()
                    s.refresh(err_task)
                    return err_task

        return task
