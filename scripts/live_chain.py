"""Run a real, isolated model task against fixed acceptance tests.

Usage: python -m scripts.live_chain --model deepseek --reviewer codex_cli
The script never prints credentials and never deploys a service.
"""

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict

from sqlmodel import select

from execution.subproc import run_async_command
from orchestrator.core import OrchestratorCore
from storage.db import get_session
from storage.models import ExecutionLog


ACCEPTANCE = '''import pytest
from slug import slugify

@pytest.mark.parametrize("value,expected", [
    ("Hello World", "hello-world"), ("  A---B__C  ", "a-b-c"),
    ("Café déjà vu", "cafe-deja-vu"), ("Straße", "strasse"),
    ("ＦＯＯ１２３", "foo123"), ("你好 world", "world"),
    ("a💡b", "a-b"), ("", ""), (" --- ", ""),
    ("a\\nb\\tc", "a-b-c"), ("123", "123"), ("Ångström", "angstrom"),
])
def test_slug(value, expected):
    assert slugify(value) == expected

@pytest.mark.parametrize("limit,expected", [(1,"a"),(2,"a"),(3,"a-b"),(4,"a-b"),(5,"a-b-c")])
def test_limit(limit, expected):
    assert slugify("a b c", max_length=limit) == expected

@pytest.mark.parametrize("value", [None, 3, [], b"hello"])
def test_input_type(value):
    with pytest.raises(TypeError):
        slugify(value)

@pytest.mark.parametrize("limit", [0, -1])
def test_nonpositive_limit(limit):
    with pytest.raises(ValueError):
        slugify("hello", max_length=limit)

@pytest.mark.parametrize("limit", [True, 1.5, "3"])
def test_limit_type(limit):
    with pytest.raises(TypeError):
        slugify("hello", max_length=limit)
'''

REQUIREMENT = """Implement slug.py exposing slugify(value: str, max_length: int | None = None) -> str.
Reject non-string value with TypeError. Normalize NFKD, casefold (ß -> ss), remove combining
marks entirely. Keep ASCII a-z/0-9 and replace runs of every remaining other character
(including non-ASCII characters and emoji) with one hyphen and strip
edge hyphens. Optional max_length must be a positive int (bool is invalid): wrong type raises
TypeError, nonpositive raises ValueError. Truncate to max_length then strip trailing hyphens.
Do not modify tests/test_acceptance.py or .gitignore; those are fixed external acceptance
tests. Add your own regression tests if useful. Emit each relative file exactly once per
response, with its final complete contents. Keep documentation brief.
"""


class AcceptanceVerification(TypedDict):
    passed: bool
    exit_code: int
    output: str
    tests_unchanged: bool


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True, timeout=30)
    return result.stdout.strip()


async def verify_acceptance(source: Path, directory: Path) -> AcceptanceVerification:
    """Load only the implementation and trusted tests in a fresh verifier."""
    source_file = source / "slug.py"
    if source_file.is_symlink() or source_file.resolve(strict=True).parent != source.resolve(strict=True):
        raise ValueError("Implementation must be a regular file inside the task checkout")
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "slug.py").write_bytes(source_file.read_bytes())
    acceptance = directory / "test_acceptance.py"
    acceptance.write_text(ACCEPTANCE, encoding="utf-8")
    config = directory / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    original = acceptance.read_bytes()
    result = await run_async_command(
        [sys.executable, "-I", "-m", "pytest", "-c", str(config), "test_acceptance.py", "-q", "-p", "no:cacheprovider",
         f"--rootdir={directory}", f"--confcutdir={directory}"],
        cwd=directory, env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}, timeout=60.0,
    )
    output = result.stdout + result.stderr
    summary = next((line.strip() for line in reversed(output.splitlines())
                    if re.search(r"\b\d+ (?:passed|failed|skipped|error)", line)), "")
    unchanged = acceptance.is_file() and acceptance.read_bytes() == original
    return {"passed": result.is_success and unchanged
            and bool(re.fullmatch(r"26 passed in [0-9.]+s(?: \(.*\))?", summary)),
            "exit_code": result.exit_code, "output": output, "tests_unchanged": unchanged}


async def run(model: Literal["deepseek", "glm"],
              reviewer: Literal["antigravity_cli", "codex_cli"]) -> bool:
    project = Path(__file__).resolve().parents[1]
    root = project / ".integration" / f"live-{model}-{uuid.uuid4().hex[:8]}"
    (root / "tests").mkdir(parents=True)
    (root / "tests/test_acceptance.py").write_text(ACCEPTANCE, encoding="utf-8")
    (root / "slug.py").write_text("def slugify(value, max_length=None):\n    raise NotImplementedError\n", encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n.worktrees/\n", encoding="utf-8")
    for args in [("init",), ("config", "user.name", "Integration Probe"),
                 ("config", "user.email", "probe@example.invalid"), ("add", "."),
                 ("commit", "-m", "test: fixed acceptance fixture before real model generation")]:
        git(root, *args)
    fixture_paths = git(root, "ls-files").splitlines()
    if fixture_paths != [".gitignore", "slug.py", "tests/test_acceptance.py"]:
        raise RuntimeError("Synthetic fixture contains unexpected source files")
    fixture_base = git(root, "rev-parse", "HEAD")
    baseline = await run_async_command(
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider",
         f"--rootdir={root}", f"--confcutdir={root}"], cwd=root,
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}, timeout=60.0,
    )
    if baseline.is_success or baseline.timed_out or "26 failed" not in baseline.stdout:
        raise RuntimeError("Acceptance fixture did not demonstrate the intended RED state")
    print(f"Fixture RED validated: {root}", flush=True)
    with get_session() as session:
        core = OrchestratorCore(repo_root=root, session=session)
        core.policy.reviewer_agent = reviewer
        task = await core.run_task(REQUIREMENT, title=f"Live {model} full chain", chosen_model=model)
        logs = session.exec(select(ExecutionLog).where(ExecutionLog.task_id == task.task_id)
                            .order_by(ExecutionLog.id)).all()
        worktree = Path(task.worktree_path) if task.worktree_path else None
        unchanged = bool(worktree and (worktree / "tests/test_acceptance.py").is_file()
                         and (worktree / "tests/test_acceptance.py").read_bytes()
                         == (root / "tests/test_acceptance.py").read_bytes())
        final_check = await verify_acceptance(worktree, root / "verifier") if worktree else None
        verified_success = task.state == "SUCCESS" and unchanged and bool(final_check and final_check["passed"])
        report = {
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "task_id": task.task_id, "model": model, "reviewer": reviewer, "mock": False,
            "state": task.state, "result_summary": task.result_summary,
            "verified_success": verified_success,
            "branch": task.branch_name, "worktree": task.worktree_path,
            "fixture_initial_paths": fixture_paths, "fixture_base_commit": fixture_base,
            "commit": git(worktree, "rev-parse", "HEAD") if worktree else None,
            "acceptance_unchanged": unchanged,
            "final_acceptance_exit_code": final_check["exit_code"] if final_check else None,
            "final_acceptance_output": final_check["output"] if final_check else "",
            "acceptance_sha256": hashlib.sha256((root / "tests/test_acceptance.py").read_bytes()).hexdigest(),
            "repair_attempts": task.repair_attempts, "codex_calls": task.codex_calls,
            "prompt_tokens": task.total_prompt_tokens, "completion_tokens": task.total_completion_tokens,
            "estimated_api_cost_cny": task.total_cost_cny,
            "steps": [{"step": log.step_name, "role": log.agent_role, "model": log.agent_model,
                       "status": log.status, "prompt_tokens": log.prompt_tokens,
                       "completion_tokens": log.completion_tokens, "cost_cny": log.cost_cny,
                       "output": log.output_snippet, "error": log.error_snippet} for log in logs],
        }
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
        return verified_success


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=["deepseek", "glm"], required=True)
    parser.add_argument("--reviewer", choices=["antigravity_cli", "codex_cli"], required=True)
    arguments = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(arguments.model, arguments.reviewer)) else 2)
