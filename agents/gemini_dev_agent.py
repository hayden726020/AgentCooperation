"""Gemini developer agent executing implementation and repairs in an isolated worktree."""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from agents.base import AgentResult, BaseAgent

LineCallback = Callable[[str, str], Any]
_COMMAND_CATEGORIES = ("test", "build", "typecheck")


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
        self._command_policy_cache: dict[str, dict[str, list[list[str]]]] = {}

    def _resolve_allowed_commands(self, context: dict[str, Any]) -> dict[str, list[list[str]]]:
        explicit = context.get("allowed_commands")
        if explicit is not None:
            from config.settings import RepoCommandConfig

            return RepoCommandConfig.model_validate(explicit).to_mapping()
        cache_key = str(context.get("repo_root") or "")
        cached = self._command_policy_cache.get(cache_key)
        if cached is None:
            from config.settings import settings as app_settings

            cached = app_settings.load_policy().execution.resolve_allowed_commands(context.get("repo_root"))
            self._command_policy_cache[cache_key] = cached
        return cached

    async def execute(self, prompt: str, context: dict[str, Any] | None = None) -> AgentResult:
        """Run a development or repair prompt inside the task's isolated worktree."""
        start = time.monotonic()
        context = context or {}
        on_line: LineCallback | None = context.get("on_line")
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

            if on_line:
                outcome = on_line(
                    "stdout",
                    f"[Explicit Mock Gemini Dev] Simulated implementation applied to {worktree.name}.",
                )
                if inspect.isawaitable(outcome):
                    await outcome

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

        client = None
        try:
            # Real live execution via AntigravityCLIClient with project_execution enabled
            from hermes_cli.runtime_provider import resolve_runtime_provider
            from providers import get_provider_profile

            allowed_commands = self._resolve_allowed_commands(context)
            command_entries = [
                " ".join(argv)
                for category in _COMMAND_CATEGORIES
                for argv in allowed_commands.get(category, [])
            ]
            command_text = "; ".join(command_entries) or "none"
            profile = get_provider_profile("antigravity-cli")
            client = profile.create_client(
                cwd=str(worktree),
                subscription_bridge_enabled=True,
                project_execution={
                    "enabled": True,
                    "workspace": str(worktree),
                    "allowed_commands": allowed_commands,
                },
            )
            try:
                request = {
                    "model": self.model_name,
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                f"Work only within the active workspace: {worktree}\n"
                                f"Task Prompt:\n{prompt}\n\n"
                                "Use view_file/grep_search to inspect and write_to_file/replace_file_content to edit. "
                                f"Declared run_command argv prefixes: {command_text}. "
                                "Append a specific test file when running tests; never run the entire test suite "
                                "without arguments. Commands run from the worktree root, and every argument must "
                                "stay inside the worktree. Shell composition, absolute paths, and '..' are rejected. "
                                "The exact read-only command `git status` is also permitted; do not run any other "
                                "git command. Only use native tools."
                            ),
                        }
                    ],
                    "timeout": self.timeout,
                }
                if on_line:
                    text_parts: list[str] = []
                    for chunk in client.chat.completions.create(**request, stream=True):
                        try:
                            delta = chunk.choices[0].delta
                        except (AttributeError, IndexError, TypeError):
                            continue
                        piece = getattr(delta, "content", None) or ""
                        if not piece:
                            continue
                        text_parts.append(piece)
                        outcome = on_line("stdout", piece)
                        if inspect.isawaitable(outcome):
                            await outcome
                    text = "".join(text_parts)
                else:
                    response = client.chat.completions.create(**request)
                    text = response.choices[0].message.content or ""
                tool_results = list(getattr(client, "last_native_tool_results", []))
                policy_rejections = list(getattr(client, "last_policy_rejections", []))
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
                        "policy_rejections": policy_rejections,
                        "mock": False,
                    },
                )
            finally:
                client.close()
        except Exception as exc:
            policy_rejections = []
            tool_results = []
            if client is not None:
                with contextlib.suppress(Exception):
                    policy_rejections = list(client.read_policy_audit())
                with contextlib.suppress(Exception):
                    tool_results = list(client.last_native_tool_results)
            return AgentResult(
                success=False,
                content="",
                duration_s=time.monotonic() - start,
                error=f"Gemini dev execution failed: {exc}",
                metadata={
                    "mock": False,
                    "tool_results": tool_results,
                    "native_tool_steps": tool_results,
                    "policy_rejections": policy_rejections,
                },
            )
