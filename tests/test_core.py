import shutil
import uuid
from pathlib import Path
import pytest
from sqlmodel import create_engine, Session, SQLModel
from orchestrator.core import OrchestratorCore
from storage.models import Task


@pytest.fixture
def test_core_env():
    temp_dir = Path(".pytest_temp") / f"run_{uuid.uuid4().hex[:8]}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    test_db = temp_dir / "test.db"
    test_engine = create_engine(f"sqlite:///{test_db.as_posix()}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(test_engine)
    session = Session(test_engine)

    core = OrchestratorCore(repo_root=Path("."), session=session)
    yield core, session

    session.close()
    try:
        shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception:
        pass



@pytest.mark.asyncio
async def test_full_orchestration_cycle(test_core_env):
    core, session = test_core_env

    # 运行模拟端到端任务
    task: Task = await core.run_task(
        requirement="Develop a reliable LRU cache module with automated tests.",
        title="LRU Cache Development",
        chosen_model="glm",
        mock_mode=True,
        custom_test_cmd="python -c \"print('All mock tests passed'); exit(0)\""
    )

    assert task is not None
    assert task.state == "SUCCESS"
    assert task.primary_model == "glm"
    assert task.total_cost_cny > 0.0
    assert task.total_prompt_tokens > 0
    assert Path(task.worktree_path).exists()


@pytest.mark.asyncio
async def test_deepseek_as_primary(test_core_env):
    core, session = test_core_env

    task: Task = await core.run_task(
        requirement="Implement a binary search tree in Python.",
        title="BST Implementation",
        chosen_model="deepseek",
        mock_mode=True,
        custom_test_cmd="python -c \"print('All mock tests passed'); exit(0)\""
    )

    assert task is not None
    assert task.state == "SUCCESS"
    assert task.primary_model == "deepseek"
