"""Decoded review decisions retain their types while credential echoes disappear."""

import json
from unittest.mock import AsyncMock

import pytest

from execution.subproc import SubprocResult


@pytest.mark.parametrize("provider", ["gemini", "codex", "antigravity"])
@pytest.mark.parametrize("password", ["true", 'escaped"password\\value', "p%40ss"])
@pytest.mark.asyncio
async def test_success_review_redacts_content_and_metadata(monkeypatch, tmp_path, provider, password):
    from urllib.parse import quote, unquote
    from agents.codex_expert import CodexExpert
    from config.settings import settings
    decoded_password = unquote(password)
    proxy_url = f"http://proxy-user:{quote(decoded_password, safe='')}@127.0.0.1:7890"
    api_key = 'fixture-api-key-"\\secret'
    echoed = f"Proxy {proxy_url}; password {decoded_password}"
    if provider == "gemini":
        echoed += f"; key {api_key}"
    verdict = {"approved": True, "severity": "P3", "summary": echoed,
               "critical_issues": [echoed], "suggestions": [echoed]}
    monkeypatch.setattr(CodexExpert, "_verify_expected_head", AsyncMock())
    monkeypatch.setattr(settings, "gemini_api_key", api_key)
    proxy_env = {"HTTPS_PROXY": proxy_url}
    if provider == "antigravity":
        import agents.antigravity_reviewer as module
        agent = module.AntigravityReviewer(cli_cmd="fixture")
        raw = "\n".join(json.dumps(event) for event in [
            {"event": "init", "init": {"agent": module.REVIEW_AGENT_NAME,
                                       "tools": [], "model": settings.antigravity_review_model}},
            {"event": "result", "result": {"status": "SUCCESS", "structured_output": verdict}},
        ])
        context = {"worktree_path": tmp_path, "expected_commit_sha": "a" * 40}
    elif provider == "gemini":
        import agents.gemini_reviewer as module
        agent = module.GeminiReviewer(cli_cmd="fixture")
        raw = json.dumps({"response": json.dumps(verdict)})
        context = {"worktree_path": tmp_path}
    else:
        import agents.codex_expert as module
        agent = module.CodexExpert(cli_cmd="fixture")
        raw = json.dumps(verdict)
        context = {"worktree_path": tmp_path, "expected_commit_sha": "a" * 40, "structured_review": True}
    monkeypatch.setattr(module, "get_cli_proxy_env", lambda: proxy_env)
    async def cli(command, **kwargs):
        response = raw
        if provider == "antigravity":
            events = [json.loads(line) for line in raw.splitlines()]
            events[0]["init"]["cwd"] = str(kwargs["cwd"])
            response = "\n".join(json.dumps(event) for event in events)
        return SubprocResult(command, 0, response, "", 0)
    monkeypatch.setattr(module, "run_async_command", cli)
    result = await agent.execute("Review", context)
    assert result.success, result.error
    review = result.metadata["review"]
    assert review["approved"] is True and review["severity"] == "P3"
    for field in (review["summary"], *review["critical_issues"], *review["suggestions"]):
        assert decoded_password not in field and proxy_url not in field
        assert api_key not in field and "proxy-user" not in field
        assert "[redacted]" in field
    assert json.loads(result.content) == review
