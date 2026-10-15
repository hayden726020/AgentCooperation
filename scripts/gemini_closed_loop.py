"""CLI launcher for Gemini Development + GLM/DeepSeek Review closed-loop workflow.

Usage:
    python -m scripts.gemini_closed_loop --requirement "Fix calculation bug" --reviewer glm --mock
    python -m scripts.gemini_closed_loop --requirement "Add slugify function" --reviewer deepseek --mock
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import logging
from pathlib import Path
import sys

# Ensure repository root and hermes-agent runtime are on sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
HERMES_ROOT = PROJECT_ROOT / "hermes-agent"
for path in (PROJECT_ROOT, HERMES_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from orchestrator.gemini_closed_loop import GeminiClosedLoopOrchestrator
from storage.db import get_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("gemini_closed_loop")


async def run_pipeline(
    requirement: str,
    reviewer: str,
    mock: bool,
    max_retries: int,
    repo_root: Path,
    test_paths: list[str] | None = None,
    allow_untrusted_tests: bool = False,
) -> int:
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=repo_root,
        session=None,
        reviewer_choice=reviewer,
        max_repair_attempts=max_retries,
    )
    logger.info(
        "Starting Gemini closed-loop: reviewer=%s, mock=%s, max_retries=%d",
        reviewer, mock, max_retries
    )
    result = await orchestrator.run(
        requirement=requirement,
        mock_mode=mock,
        test_paths=test_paths,
        allow_untrusted_tests=allow_untrusted_tests,
    )

    output_data = {
        "task_id": result.task_id,
        "success": result.success,
        "state": result.state,
        "result_summary": result.result_summary,
        "repair_attempts": result.repair_attempts,
        "final_commit_sha": result.final_commit_sha,
        "verdict": asdict(result.verdict) if result.verdict else None,
        "history_count": len(result.history),
    }
    print(json.dumps(output_data, ensure_ascii=False, indent=2))
    return 0 if result.success else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--requirement",
        "-r",
        type=str,
        required=True,
        help="Task requirement description",
    )
    parser.add_argument(
        "--reviewer",
        choices=["glm", "deepseek"],
        default="glm",
        help="Independent reviewer model choice (default: glm)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run in explicit simulation mode without live model calls",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum repair attempts before failing (default: 3)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Root path of target repository (default: current project root)",
    )
    parser.add_argument(
        "--test-path",
        action="append",
        dest="test_paths",
        default=None,
        help="Optional safe relative test file path within worktree (can be repeated)",
    )
    parser.add_argument(
        "--allow-untrusted-tests",
        action="store_true",
        default=False,
        help="Allow executing newly generated test code without pausing for approval",
    )

    args = parser.parse_args()
    return asyncio.run(
        run_pipeline(
            requirement=args.requirement,
            reviewer=args.reviewer,
            mock=args.mock,
            max_retries=args.max_retries,
            repo_root=args.repo_root,
            test_paths=args.test_paths,
            allow_untrusted_tests=args.allow_untrusted_tests,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
