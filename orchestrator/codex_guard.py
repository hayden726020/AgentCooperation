"""Persisted Codex quota state and resumable, evidence-based deferred audits."""
import asyncio
import json
import uuid
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, List

from transitions import Machine
from sqlmodel import Session, select

from agents.codex_expert import CodexExpert, CodexQuotaExhaustedError
from config.settings import CodexConfig
from storage.db import provide_session, locked_session
from storage.models import PendingCodexAudit, PendingAuditContext, CodexQuotaRecord, Task, utc_now


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


class CodexQuotaGuard:
    STATES = ['READY', 'RESERVED', 'EXHAUSTED', 'RECOVERING']
    AUDITABLE_TASK_STATES = {'SUCCESS', 'FAILED', 'WAITING_APPROVAL', 'WAITING_REVIEW',
                             'INTERRUPTED', 'BUDGET_STOPPED'}

    def __init__(self, codex_cfg: CodexConfig, session: Optional[Session] = None):
        self.cfg = codex_cfg
        self.session = session
        self._fsm = SimpleNamespace()
        self.machine = Machine(model=self._fsm, states=self.STATES, initial='READY',
                               auto_transitions=False)
        self.machine.add_transition('reserve', ['READY', 'RESERVED'], 'RESERVED')
        self.machine.add_transition('release', ['RESERVED', 'READY'], 'READY')
        self.machine.add_transition('mark_exhausted', self.STATES, 'EXHAUSTED')
        self.machine.add_transition('start_recovery', ['EXHAUSTED', 'RECOVERING'], 'RECOVERING')
        self.machine.add_transition('recovered', ['READY', 'RECOVERING', 'EXHAUSTED'], 'READY')
        self._sync_state_from_db()

    def _transition(self, event):
        """Validate the latest durable source state while holding the write lock."""
        with locked_session(self.session) as s:
            record = self._quota(s)
            self.state = record.state
            self._fsm.state = record.state
            self.machine.events[event].trigger(self._fsm)
            self.state = self._fsm.state
            record.state = self.state
            record.last_checked_at = utc_now()
            if self.state == 'READY':
                record.exhausted_reason = None
                record.estimated_recovery_at = None
            s.add(record)

    def reserve(self):
        self._transition('reserve')

    def release(self):
        self._transition('release')

    def mark_exhausted(self):
        self._transition('mark_exhausted')

    def start_recovery(self):
        self._transition('start_recovery')

    def recovered(self):
        self._transition('recovered')

    def _provide_session(self):
        return provide_session(self.session)

    @staticmethod
    def _quota(s):
        record = s.exec(select(CodexQuotaRecord).order_by(CodexQuotaRecord.id.desc())).first()
        if record is None:
            record = CodexQuotaRecord()
            s.add(record)
        return record

    def _sync_state_from_db(self):
        with locked_session(self.session) as s:
            self.state = self._quota(s).state

    def _save_state_to_db(self, reason: Optional[str] = None):
        with locked_session(self.session) as s:
            record = self._quota(s)
            self.state = record.state
            record.last_checked_at = utc_now()
            if reason:
                record.exhausted_reason = reason
            s.add(record)

    def set_quota_state(self, state: str, reason: Optional[str] = None,
                        recovery_at: Optional[datetime] = None):
        """Explicit operator override, including the official reset timestamp."""
        if state not in self.STATES:
            raise ValueError('Invalid Codex quota state')
        self.state = state
        with locked_session(self.session) as s:
            record = self._quota(s)
            record.state = state
            record.last_checked_at = utc_now()
            record.exhausted_reason = reason
            record.estimated_recovery_at = recovery_at
            s.add(record)

    def is_available(self) -> bool:
        self._sync_state_from_db()
        return self.cfg.enabled and self.state == 'READY'

    def handle_quota_exhausted(self, task_id: str, commit_sha: str, severity: str,
                              issue_description: str, git_diff_summary: str,
                              gemini_review_json: Optional[str] = None, reason: Optional[str] = None,
                              worktree_path: str = '', test_summary: str = '', issue_context: str = '') -> PendingCodexAudit:
        # State, evidence, and task marker commit together so crashes cannot lose the queue.
        with locked_session(self.session) as s:
            quota = self._quota(s)
            quota.state = 'EXHAUSTED'
            quota.last_checked_at = utc_now()
            quota.exhausted_reason = reason or 'Weekly quota exhausted'
            s.add(quota)
            self.state = 'EXHAUSTED'
            existing = s.exec(select(PendingCodexAudit).where(
                PendingCodexAudit.task_id == task_id, PendingCodexAudit.commit_sha == commit_sha,
                PendingCodexAudit.issue_description == issue_description,
                PendingCodexAudit.status.in_(['QUEUED', 'AUDITING', 'NEEDS_REPAIR']))).first()
            audit = existing or PendingCodexAudit(audit_id=f'audit_{uuid.uuid4().hex[:12]}',
                task_id=task_id, commit_sha=commit_sha, severity=severity,
                issue_description=issue_description, git_diff_summary=git_diff_summary,
                gemini_review_json=gemini_review_json)
            s.add(audit)
            evidence = s.exec(select(PendingAuditContext).where(PendingAuditContext.audit_id == audit.audit_id)).first()
            if evidence is None:
                evidence = PendingAuditContext(audit_id=audit.audit_id)
            task = s.exec(select(Task).where(Task.task_id == task_id)).first()
            evidence.worktree_path = str(worktree_path or (task.worktree_path if task else '') or '')
            evidence.test_summary = test_summary
            evidence.issue_context = issue_context
            s.add(evidence)
            if task:
                task.has_pending_codex_audit = True
                s.add(task)
            audit_id = audit.audit_id
        with self._provide_session() as s:
            return s.exec(select(PendingCodexAudit).where(PendingCodexAudit.audit_id == audit_id)).one()

    def update_pending_evidence(self, task_id: str, commit_sha: str, git_diff: str,
                                test_summary: str, gemini_review_json: Optional[str] = None):
        """Bind deferred findings to the final patch and its verification evidence."""
        with locked_session(self.session) as s:
            audits = s.exec(select(PendingCodexAudit).where(
                PendingCodexAudit.task_id == task_id,
                PendingCodexAudit.status.in_(['QUEUED', 'NEEDS_REPAIR']))).all()
            task = s.exec(select(Task).where(Task.task_id == task_id)).first()
            for audit in audits:
                audit.commit_sha = commit_sha
                audit.git_diff_summary = git_diff
                audit.gemini_review_json = gemini_review_json
                audit.status = 'QUEUED'
                s.add(audit)
                evidence = s.exec(select(PendingAuditContext).where(
                    PendingAuditContext.audit_id == audit.audit_id)).first()
                if evidence is None:
                    evidence = PendingAuditContext(audit_id=audit.audit_id)
                evidence.test_summary = test_summary
                if task and task.worktree_path:
                    evidence.worktree_path = task.worktree_path
                s.add(evidence)
            if audits and task:
                task.has_pending_codex_audit = True
                s.add(task)

    def get_pending_audits(self, status: str = 'QUEUED') -> List[PendingCodexAudit]:
        with self._provide_session() as s:
            return list(s.exec(select(PendingCodexAudit).where(PendingCodexAudit.status == status)
                        .order_by(PendingCodexAudit.created_at.asc())).all())

    async def probe_recovery(self, codex_expert: CodexExpert, force: bool = False,
                             worktree_path: Optional[Path] = None,
                             allow_simulated: bool = False) -> bool:
        if not self.cfg.enabled or (getattr(codex_expert, 'mock_mode', False) and not allow_simulated):
            return False
        with locked_session(self.session) as s:
            quota = self._quota(s)
            self.state = quota.state
            original_state = self.state
            if self.state == 'READY' and not force:
                return True
            reset = _utc(quota.estimated_recovery_at)
            if not force and (reset is None or reset > utc_now()):
                return False
            quota.state = 'RECOVERING'
            quota.last_checked_at = utc_now()
            s.add(quota)
            self.state = 'RECOVERING'
        try:
            cwd = Path(worktree_path or Path.cwd()).resolve(strict=True)
            result = await codex_expert.execute('Quota recovery check: reply READY. Do not inspect or edit files.',
                                               context={'worktree_path': cwd})
        except CodexQuotaExhaustedError as exc:
            self.set_quota_state('EXHAUSTED', reason=str(exc))
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._save_state_to_db(reason=f'Recovery probe failed: {type(exc).__name__}')
            return False
        if not result.success:
            self._save_state_to_db(reason=result.error or 'Recovery probe failed')
            return False
        if result.metadata.get('mock') and not allow_simulated:
            self._restore_simulated_state(original_state)
            return False
        self.recovered()
        return True

    def _restore_simulated_state(self, original_state):
        with locked_session(self.session) as s:
            quota = self._quota(s)
            if quota.state == 'RECOVERING':
                quota.state = original_state
                s.add(quota)
            self.state = quota.state

    @staticmethod
    def _approved(result) -> bool:
        review = result.metadata.get('audit') or result.metadata.get('review')
        if not isinstance(review, dict):
            try:
                review = json.loads(result.content)
            except (ValueError, TypeError):
                return False
        return (isinstance(review, dict) and review.get('approved') is True
                and review.get('severity') in ['P2', 'P3'])

    def _claim(self, audit_id):
        with locked_session(self.session) as s:
            audit = s.exec(select(PendingCodexAudit).where(PendingCodexAudit.audit_id == audit_id)).first()
            if audit is None or audit.status != 'QUEUED':
                return None
            task = s.exec(select(Task).where(Task.task_id == audit.task_id)).first()
            if task is None or task.state not in self.AUDITABLE_TASK_STATES:
                return None
            evidence = s.exec(select(PendingAuditContext).where(PendingAuditContext.audit_id == audit_id)).first()
            if evidence is None:
                evidence = PendingAuditContext(audit_id=audit_id, worktree_path=task.worktree_path)
            evidence.claimed_at = utc_now()
            audit.status = 'AUDITING'
            s.add(evidence)
            s.add(audit)
            values = {'task_id': audit.task_id, 'commit_sha': audit.commit_sha,
                      'issue': audit.issue_description, 'diff': audit.git_diff_summary,
                      'gemini': audit.gemini_review_json, 'worktree_path': evidence.worktree_path,
                      'test_summary': evidence.test_summary, 'issue_context': evidence.issue_context}
        return values

    def _finish(self, audit_id, status, feedback=''):
        with locked_session(self.session) as s:
            audit = s.exec(select(PendingCodexAudit).where(PendingCodexAudit.audit_id == audit_id)).one()
            audit.status = status
            audit.codex_feedback = feedback
            audit.resolved_at = utc_now() if status == 'RESOLVED' else None
            s.add(audit)
            s.flush()
            task = s.exec(select(Task).where(Task.task_id == audit.task_id)).first()
            if task:
                others = s.exec(select(PendingCodexAudit).where(PendingCodexAudit.task_id == audit.task_id,
                    PendingCodexAudit.status.in_(['QUEUED', 'AUDITING', 'NEEDS_REPAIR']))).first()
                task.has_pending_codex_audit = others is not None
                s.add(task)

    def recover_stale_audits(self):
        """Requeue abandoned claims after the expert timeout plus a safety margin."""
        cutoff = utc_now() - timedelta(minutes=15)
        with locked_session(self.session) as s:
            for audit in s.exec(select(PendingCodexAudit).where(PendingCodexAudit.status == 'AUDITING')):
                evidence = s.exec(select(PendingAuditContext).where(PendingAuditContext.audit_id == audit.audit_id)).first()
                if evidence is None or evidence.claimed_at is None or _utc(evidence.claimed_at) < cutoff:
                    audit.status = 'QUEUED'
                    s.add(audit)

    async def process_backlog(self, codex_expert: CodexExpert,
                              allow_simulated: bool = False) -> List[dict]:
        if not self.cfg.enabled:
            return []
        if getattr(codex_expert, 'mock_mode', False) and not allow_simulated:
            return [{'audit_id': audit.audit_id, 'task_id': audit.task_id,
                     'status': 'SIMULATED', 'feedback': 'Simulation cannot close real deferred audits.'}
                    for audit in self.get_pending_audits()]
        self.recover_stale_audits()
        audits = self.get_pending_audits()
        results = []
        self._sync_state_from_db()
        original_state = self.state
        if self.state == 'RESERVED':
            return results
        if audits and self.state == 'EXHAUSTED':
            self.start_recovery()
        for audit in audits:
            data = self._claim(audit.audit_id)
            if data is None:
                continue
            prompt = (f"Post-Recovery Audit for Task {data['task_id']} (Commit {data['commit_sha']}):\n"
                      f"Issue: {data['issue']}\nDiff:\n{data['diff']}\n"
                      f"Tests:\n{data['test_summary']}\nGemini Review:\n{data['gemini']}\n"
                      'Return JSON with approved (boolean), severity (P0/P1/P2/P3), issues. '
                      'Only approve after verifying the recorded changes and tests.')
            try:
                result = await codex_expert.execute(prompt, context={
                    'worktree_path': data['worktree_path'],
                    'issue_context': data['issue_context'],
                    'expected_commit_sha': data['commit_sha'],
                    'structured_review': True})
            except CodexQuotaExhaustedError as exc:
                self._finish(audit.audit_id, 'QUEUED', str(exc))
                self.set_quota_state('EXHAUSTED', reason=str(exc))
                results.append({'audit_id': audit.audit_id, 'task_id': data['task_id'],
                                'status': 'STILL_EXHAUSTED', 'feedback': str(exc)[:200]})
                break
            except asyncio.CancelledError:
                self._finish(audit.audit_id, 'QUEUED')
                raise
            except Exception as exc:
                self._finish(audit.audit_id, 'QUEUED', f'Audit failed: {type(exc).__name__}')
                results.append({'audit_id': audit.audit_id, 'task_id': data['task_id'],
                                'status': 'FAILED', 'feedback': type(exc).__name__})
                continue
            if not result.success:
                self._finish(audit.audit_id, 'QUEUED', result.error or 'Expert execution failed')
                results.append({'audit_id': audit.audit_id, 'task_id': data['task_id'],
                                'status': 'FAILED', 'feedback': (result.error or '')[:200]})
                continue
            if result.metadata.get('mock') and not allow_simulated:
                self._finish(audit.audit_id, 'QUEUED')
                self._restore_simulated_state(original_state)
                results.append({'audit_id': audit.audit_id, 'task_id': data['task_id'],
                                'status': 'SIMULATED', 'feedback': 'Simulation cannot close real deferred audits.'})
                continue
            self.recovered()  # A successful CLI call verifies availability, independent of approval.
            status = 'RESOLVED' if self._approved(result) else 'NEEDS_REPAIR'
            self._finish(audit.audit_id, status, result.content)
            results.append({'audit_id': audit.audit_id, 'task_id': data['task_id'],
                            'status': status, 'feedback': result.content[:200]})
        return results
