"""Background job manager for running asynchronous orchestration tasks."""

import asyncio
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class JobManager:
    """Manages asynchronous background jobs dispatched from the web interface."""

    def __init__(self):
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create_job(self, job_type: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "type": job_type,
                "status": "running",
                "metadata": metadata or {},
                "result": None,
                "error": None,
                "created_at": now,
                "completed_at": None,
            }
        return job_id

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                return dict(job)
            return None

    def list_jobs(self, limit: int = 20) -> list[Dict[str, Any]]:
        with self._lock:
            items = list(self._jobs.values())
            items.sort(key=lambda x: x["created_at"], reverse=True)
            return [dict(i) for i in items[:limit]]

    def run_async(self, job_id: str, async_func: Callable[[], Any]):
        """Run an async function inside a dedicated worker thread with its own event loop."""
        def _worker():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                res = loop.run_until_complete(async_func())
                with self._lock:
                    if job_id in self._jobs:
                        self._jobs[job_id]["status"] = "completed"
                        self._jobs[job_id]["result"] = res
                        self._jobs[job_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
            except Exception as e:
                logger.exception("Background job %s failed: %s", job_id, e)
                with self._lock:
                    if job_id in self._jobs:
                        self._jobs[job_id]["status"] = "failed"
                        self._jobs[job_id]["error"] = str(e)
                        self._jobs[job_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        t = threading.Thread(target=_worker, daemon=True, name=f"AgentJob-{job_id}")
        t.start()
        return job_id


job_manager = JobManager()
