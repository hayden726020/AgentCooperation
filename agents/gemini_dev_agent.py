"""Gemini developer agent executing implementation and repairs in an isolated worktree."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from agents.base import AgentResult, BaseAgent


class GeminiDevAgent(BaseAgent):
    """Developer role driving Gemini 3.8 Flash High via native Antigravity CLI project execution."""

    def __init__(
        self,
        model_name: str = "gemini-3.8-flash-high",
        effort: str = "high",
        mock_mode: bool = False,
        timeout: float = 900.0,
    ):
        super().__init__(role_name="PRIMARY_DEVELOPER", model_name=model_name)
        self.effort = effort
        self.mock_mode = mock_mode
        self.timeout = timeout

    async def execute(self, prompt: str, context: dict[str, Any] | None = None) -> AgentResult:
        """Run a development or repair prompt inside the task's isolated worktree."""
        start = time.monotonic()
        context = context or {}
        worktree_path = context.get("worktree_path")
        if not worktree_path:
            return AgentResult(
                success=False,
                content="",
                duration_s=time.monotonic() - start,
                error="GeminiDevAgent requires an explicit worktree_path in context",
            )
        worktree = Path(worktree_path).resolve()
        if not worktree.is_dir():
            return AgentResult(
                success=False,
                content="",
                duration_s=time.monotonic() - start,
                error=f"Worktree directory does not exist: {worktree}",
            )

        if self.mock_mode:
            # Explicit simulation for offline unit tests
            files_written = []
            mock_files = context.get("mock_files")
            if mock_files and isinstance(mock_files, dict):
                for rel_path, content in mock_files.items():
                    target = worktree / rel_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                    files_written.append(str(target))
            else:
                # Default mock behavior if no explicit files are provided:
                # Ensure at least one implementation file exists so checkpointing works.
                sample_file = worktree / "solution.py"
                count = 1
                if sample_file.exists():
                    try:
                        content = sample_file.read_text(encoding="utf-8")
                        count = len(content.splitlines()) + 1
                    except Exception:
                        count = 2
                sample_file.write_text(
                    f"# Simulated implementation\nVALUE = {42 + count}\n# Iteration {count}\n",
                    encoding="utf-8",
                )
                files_written.append(str(sample_file))

            return AgentResult(
                success=True,
                content=f"[Explicit Mock Gemini Dev] Successfully applied implementation to {worktree.name}.",
                duration_s=time.monotonic() - start,
                metadata={
                    "files_written": files_written,
                    "mock": True,
                    "model": self.model_name,
                },
            )

        try:
            # Real live execution via AntigravityCLIClient with project_execution enabled
            from hermes_cli.runtime_provider import resolve_runtime_provider
            from providers import get_provider_profile

            profile = get_provider_profile("antigravity-cli")
            client = profile.create_client(
                cwd=str(worktree),
                subscription_bridge_enabled=True,
                project_execution={"enabled": True, "workspace": str(worktree)},
            )
            try:
                response = client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                f"Work only within the active workspace: {worktree}\n"
                                f"Task Prompt:\n{prompt}\n\n"
                                "Use view_file/grep_search to inspect, write_to_file/replace_file_content to edit, "
                                "and run python -m pytest <test_file> -q to test. "
                                "Do NOT run the entire test suite without arguments. If you need a final workspace "
                                "check, only the exact read-only command `git status` is permitted; do not run any "
                                "other git command. Only use native tools."
                            ),
                        }
                    ],
                    timeout=self.timeout,
                )
                text = response.choices[0].message.content or ""
                tool_results = list(getattr(client, "last_native_tool_results", []))
                files_written = []
                for tr in tool_results:
                    if tr.get("name") in ("write_to_file", "replace_file_content") and tr.get("succeeded"):
                        params = tr.get("parameters", {})
                        target = params.get("TargetFile") or params.get("target_file")
                        if target:
                            files_written.append(str(target))
                return AgentResult(
                    success=True,
                    content=text,
                    duration_s=time.monotonic() - start,
                    metadata={
                        "model": self.model_name,
                        "tool_results": tool_results,
                        "native_tool_steps": tool_results,
                        "files_written": files_written,
                        "mock": False,
                    },
                )
            finally:
                client.close()
        except Exception as exc:
            return AgentResult(
                success=False,
                content="",
                duration_s=time.monotonic() - start,
                error=f"Gemini dev execution failed: {exc}",
                metadata={"mock": False},
            )
