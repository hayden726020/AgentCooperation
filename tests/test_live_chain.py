import pytest

from execution.subproc import SubprocResult
from scripts import live_chain


@pytest.mark.asyncio
async def test_fixed_verifier_copies_only_implementation_and_filters_environment(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "slug.py").write_text("def slugify(value): return value\n")
    (source / "conftest.py").write_text("raise RuntimeError('must not import')\n")
    captured = {}

    async def execute(command, **kwargs):
        captured.update(kwargs)
        verifier = kwargs["cwd"]
        assert not (verifier / "conftest.py").exists()
        assert (verifier / "test_acceptance.py").read_text(encoding="utf-8") == live_chain.ACCEPTANCE
        return SubprocResult(command, 0, "26 passed in 0.1s", "", 0.1)

    monkeypatch.setattr(live_chain, "run_async_command", execute, raising=False)
    result = await live_chain.verify_acceptance(source, tmp_path / "verify")
    assert result["passed"] is True
    assert captured["env"] == {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    assert captured["timeout"] == 60.0


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", ["26 skipped in 0.1s", "1 passed, 25 skipped in 0.1s", "25 passed in 0.1s", "26 passed, 1 failed in 0.1s"])
async def test_fixed_verifier_requires_all_acceptance_cases(summary, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "slug.py").write_text("pass\n")

    async def execute(command, **kwargs):
        return SubprocResult(command, 0, summary, "", 0.1)

    monkeypatch.setattr(live_chain, "run_async_command", execute, raising=False)
    result = await live_chain.verify_acceptance(source, tmp_path / "verify")
    assert result["passed"] is False
