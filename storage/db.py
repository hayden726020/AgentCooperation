from pathlib import Path
from sqlmodel import SQLModel, create_engine, Session, select
from datetime import datetime, timezone
from storage.models import (
    Task,
    ExecutionLog,
    PendingCodexAudit,
    CodexQuotaRecord,
    MonthlySpending
)
from config.settings import settings


def get_engine(db_path: str = None):
    target_path = db_path or settings.sqlite_db_path
    path_obj = Path(target_path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    sqlite_url = f"sqlite:///{path_obj.as_posix()}"
    return create_engine(sqlite_url, connect_args={"check_same_thread": False})


engine = get_engine()


def init_db(target_engine=None):
    use_engine = target_engine or engine
    SQLModel.metadata.create_all(use_engine)


def get_session(target_engine=None) -> Session:
    use_engine = target_engine or engine
    return Session(use_engine)


from contextlib import contextmanager


@contextmanager
def provide_session(session: Optional[Session] = None):
    if session is not None:
        yield session
    else:
        with get_session() as s:
            yield s


def get_or_create_monthly_spending(session: Session, year_month: str = None) -> MonthlySpending:
    if not year_month:
        now = datetime.now(timezone.utc)
        year_month = f"{now.year}-{now.month:02d}"

    stmt = select(MonthlySpending).where(MonthlySpending.year_month == year_month)
    record = session.exec(stmt).first()
    if not record:
        record = MonthlySpending(
            year_month=year_month,
            total_prompt_tokens=0,
            total_completion_tokens=0,
            total_cost_cny=0.0
        )
        session.add(record)
        session.commit()
        session.refresh(record)
    return record


def get_or_create_codex_quota(session: Session) -> CodexQuotaRecord:
    stmt = select(CodexQuotaRecord).order_by(CodexQuotaRecord.id.desc())
    record = session.exec(stmt).first()
    if not record:
        record = CodexQuotaRecord(state="READY")
        session.add(record)
        session.commit()
        session.refresh(record)
    return record

