import subprocess
from pathlib import Path
import pytest
from sqlmodel import create_engine, Session, SQLModel
from orchestrator.core import OrchestratorCore
from storage.models import Task


@pytest.fixture
def test_core_env(tmp_path):
    temp_dir = tmp_path
    repo = temp_dir / "target"
    repo.mkdir()
    (repo / "README.md").write_text("# Isolated target project\n", encoding="utf-8")
    for args in (["init"], ["config", "user.name", "Test"],
                 ["config", "user.email", "test@example.invalid"], ["add", "README.md"],
                 ["-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                  "commit", "-m", "initial target"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    test_db = temp_dir / "test.db"
    test_engine = create_engine(f"sqlite:///{test_db.as_posix()}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(test_engine)
    session = Session(test_engine)

    core = OrchestratorCore(repo_root=repo, session=session)
    yield core, session

    session.close()
    test_engine.dispose()



@pytest.mark.asyncio
async def test_full_orchestration_cycle(test_core_env):
    core, session = test_core_env

    # 运行模拟端到端任务
    task: Task = await core.run_task(
        requirement="Develop a reliable LRU cache module with automated tests.",
        title="LRU Cache Development",
        chosen_model="glm",
        mock_mode=True
    )

    assert task is not None
    assert task.state == "SUCCESS", task.result_summary
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
        mock_mode=True
    )

    assert task is not None
    assert task.state == "SUCCESS", task.result_summary
    assert task.primary_model == "deepseek"
