import uuid
from datetime import datetime, timezone
from typing import Optional, List
from transitions import Machine
from sqlmodel import Session, select
from storage.db import provide_session, get_or_create_codex_quota
from storage.models import PendingCodexAudit, CodexQuotaRecord, Task
from agents.codex_expert import CodexExpert, CodexQuotaExhaustedError
from config.settings import CodexConfig



class CodexQuotaGuard:
    """
    Codex 额度守护状态机与延期补审管理器：
    管理状态：READY <-> RESERVED <-> EXHAUSTED <-> RECOVERING
    核心策略：
    - 当 Codex 周额度耗尽时，不直接跳过或中止任务，而是持久化至 PendingCodexAudit 待补审队列；
    - 让 Gemini CLI 与主力 Flash 引擎先行完成安全可控的工作；
    - 当额度恢复后，由补审引擎自动重开审查并完成闭环。
    """

    STATES = ["READY", "RESERVED", "EXHAUSTED", "RECOVERING"]

    def __init__(self, codex_cfg: CodexConfig, session: Optional[Session] = None):
        self.cfg = codex_cfg
        self.session = session

        # 初始化 transitions 状态机
        self.machine = Machine(
            model=self,
            states=CodexQuotaGuard.STATES,
            initial="READY",
            auto_transitions=False
        )

        # 状态流转规则
        self.machine.add_transition(trigger="reserve", source="READY", dest="RESERVED")
        self.machine.add_transition(trigger="release", source="RESERVED", dest="READY")
        self.machine.add_transition(trigger="mark_exhausted", source=["READY", "RESERVED", "RECOVERING"], dest="EXHAUSTED")
        self.machine.add_transition(trigger="start_recovery", source="EXHAUSTED", dest="RECOVERING")
        self.machine.add_transition(trigger="recovered", source=["RECOVERING", "EXHAUSTED"], dest="READY")

        self._sync_state_from_db()

    def _provide_session(self):
        return provide_session(self.session)

    def _sync_state_from_db(self):
        """从 SQLite 同步最新的 Codex 额度状态"""
        with self._provide_session() as s:
            record = get_or_create_codex_quota(s)
            if record.state in self.STATES:
                self.state = record.state

    def _save_state_to_db(self, reason: Optional[str] = None):
        with self._provide_session() as s:
            record = get_or_create_codex_quota(s)
            record.state = self.state
            record.last_checked_at = datetime.now(timezone.utc)
            if reason:
                record.exhausted_reason = reason
            s.add(record)
            s.commit()


    def is_available(self) -> bool:
        self._sync_state_from_db()
        return self.state in ["READY", "RECOVERING"]

    def handle_quota_exhausted(
        self,
        task_id: str,
        commit_sha: str,
        severity: str,
        issue_description: str,
        git_diff_summary: str,
        gemini_review_json: Optional[str] = None,
        reason: Optional[str] = None
    ) -> PendingCodexAudit:
        """
        当检测到 Codex 额度耗尽时：
        1. 状态机切入 EXHAUSTED 并持久化
        2. 生成 PendingCodexAudit 待补审单
        3. 标记对应 Task 为 has_pending_codex_audit = True
        """
        self.mark_exhausted()
        self._save_state_to_db(reason=reason or "Quota limit detected from CLI")

        audit_id = f"audit_{uuid.uuid4().hex[:12]}"
        pending_audit = PendingCodexAudit(
            audit_id=audit_id,
            task_id=task_id,
            commit_sha=commit_sha,
            severity=severity,
            issue_description=issue_description,
            git_diff_summary=git_diff_summary,
            gemini_review_json=gemini_review_json,
            status="QUEUED"
        )

        with self._provide_session() as s:
            s.add(pending_audit)

            # 更新任务标记
            stmt = select(Task).where(Task.task_id == task_id)
            task = s.exec(stmt).first()
            if task:
                task.has_pending_codex_audit = True
                s.add(task)

            s.commit()
            s.refresh(pending_audit)

        return pending_audit

    def get_pending_audits(self, status: str = "QUEUED") -> List[PendingCodexAudit]:
        """获取所有待 Codex 补审的记录（最早的优先）"""
        with self._provide_session() as s:
            stmt = (
                select(PendingCodexAudit)
                .where(PendingCodexAudit.status == status)
                .order_by(PendingCodexAudit.created_at.asc())
            )
            return list(s.exec(stmt).all())

    async def process_backlog(self, codex_expert: CodexExpert) -> List[dict]:
        """
        当额度恢复或用户手动触发时，逐项补审历史遗留的重要问题
        """
        audits = self.get_pending_audits(status="QUEUED")
        results = []

        if not audits:
            return results

        # 先做一次轻量 probe 探查是否已恢复
        try:
            self.start_recovery()
        except Exception:
            pass

        for audit in audits:
            try:
                prompt = (
                    f"Post-Recovery Audit for Task {audit.task_id} (Commit {audit.commit_sha[:7]}):\n"
                    f"Issue: {audit.issue_description}\n"
                    f"Diff Summary: {audit.git_diff_summary[:500]}\n"
                    f"Gemini Review: {audit.gemini_review_json or 'None'}\n"
                    f"Please verify if this solution has any remaining architecture or security risks."
                )
                
                res = await codex_expert.execute(prompt)
                
                # 成功补审，标记状态并恢复 READY
                self.recovered()
                self._save_state_to_db()

                with self._provide_session() as s:
                    stmt = select(PendingCodexAudit).where(PendingCodexAudit.audit_id == audit.audit_id)
                    item = s.exec(stmt).first()
                    if item:
                        item.status = "RESOLVED"
                        item.codex_feedback = res.content
                        item.resolved_at = datetime.now(timezone.utc)
                        s.add(item)
                        s.commit()

                results.append({
                    "audit_id": audit.audit_id,
                    "task_id": audit.task_id,
                    "status": "RESOLVED",
                    "feedback": res.content[:200]
                })

            except CodexQuotaExhaustedError:
                # 仍未恢复，保持 EXHAUSTED 并停止后续处理
                self.mark_exhausted()
                self._save_state_to_db(reason="Still exhausted during backlog processing")
                results.append({
                    "audit_id": audit.audit_id,
                    "task_id": audit.task_id,
                    "status": "STILL_EXHAUSTED",
                    "feedback": "Quota is still exhausted."
                })
                break

        return results
