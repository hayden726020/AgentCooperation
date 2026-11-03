"""Offline contracts for repository-scoped command allowlists."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.settings import (
    PROJECT_ROOT,
    ExecutionConfig,
    RepoCommandConfig,
    settings,
)

DEFAULT_POLICY = {
    "test": [["python", "-m", "pytest"]],
    "build": [],
    "typecheck": [],
}


def test_default_policy_allows_only_the_local_pytest_suite():
    config = ExecutionConfig()
    assert config.resolve_allowed_commands(None) == DEFAULT_POLICY


def test_repo_specific_policy_overrides_default():
    config = ExecutionConfig(allowed_commands={
        "default": {"test": [["python", "-m", "pytest"]]},
        "workplace/site": {
            "test": [["npm", "test"]],
            "build": [["npm", "run", "build"]],
            "typecheck": [["npx", "tsc", "--noEmit"]],
        },
    })
    assert config.resolve_allowed_commands(PROJECT_ROOT / "workplace/site") == {
        "test": [["npm", "test"]],
        "build": [["npm", "run", "build"]],
        "typecheck": [["npx", "tsc", "--noEmit"]],
    }


def test_repo_without_specific_entry_uses_default():
    config = ExecutionConfig(allowed_commands={
        "default": {"test": [["make", "test"]]},
        "workplace/site": {"build": [["make", "build"]]},
    })
    assert config.resolve_allowed_commands(PROJECT_ROOT / "workplace/other") == {
        "test": [["make", "test"]],
        "build": [],
        "typecheck": [],
    }


def test_policy_without_default_disables_undeclared_repositories():
    config = ExecutionConfig(allowed_commands={
        "workplace/site": {"build": [["make", "build"]]},
    })
    assert config.resolve_allowed_commands(PROJECT_ROOT / "workplace/other") == {
        "test": [],
        "build": [],
        "typecheck": [],
    }


def test_loaded_policy_exposes_the_default_command_allowlist():
    loaded = settings.load_policy().execution.resolve_allowed_commands(None)
    assert loaded["test"] == [["python", "-m", "pytest"]]
    assert loaded["build"] == []
    assert loaded["typecheck"] == []


@pytest.mark.parametrize("command", [
    [],
    [123],
    ["python"],
    ["pwsh", "-Command", "whoami"],
    ["bash", "-lc", "whoami"],
    ["python", "-m", "pytest", "../outside"],
    ["python", "-m", "pytest", "C:/outside"],
    ["python", "-m", "pytest", "/etc/passwd"],
    ["python", "-m", "pytest", ".env"],
    ["python", "-m", "pytest", ".git/config"],
    ["python", "-m", "pytest", "keys/id_rsa"],
    ["python", "-m", "pytest", "tests/test_x.py;whoami"],
    ["python", "-m", "pytest", "tests/test_x.py && whoami"],
    ["python", "-m", "pytest", "--output=../outside"],
    ["python", "-m", "pytest", "-c", "evil.ini"],
    ["python", "-m", "pytest", "--config-file", "evil.ini"],
    ["python", "-m", "pytest", "-qc", "evil.ini"],
    ["python", "-m", "pytest", "-qo", "addopts=x"],
    ["python", "-m", "pytest", "--rootdir", "src"],
    ["python", "-m", "pytest", "--basetemp", "../tmp"],
    ["npm", "run", "build", "--workspace", "other"],
    ["npm", "run", "build", "--prefix", "src"],
    ["npm", "run", "build", "-w", "other"],
    ["make", "-C", "src", "test"],
    ["./scripts/build.sh"],
])
def test_invalid_command_entries_are_rejected(command):
    with pytest.raises(ValueError):
        RepoCommandConfig(test=[command])


@pytest.mark.parametrize("policy", [
    {"test": "python -m pytest"},
    {"test": [["python", "-m", "pytest"]], "shell": [["make"]]},
    {"test": [["python", "-m", "pytest"]], "build": [{}]},
])
def test_invalid_category_shapes_are_rejected(policy):
    with pytest.raises(ValueError):
        RepoCommandConfig.model_validate(policy)


@pytest.mark.parametrize("repo_key", [
    "../outside",
    "/absolute",
    "workplace/../outside",
    "workplace/NAILONG ",
])
def test_repository_keys_must_be_relative_paths(repo_key):
    with pytest.raises(ValueError):
        ExecutionConfig(allowed_commands={repo_key: {"test": [["make", "test"]]}})


def test_valid_node_toolchain_policy_is_accepted():
    policy = RepoCommandConfig(
        test=[["npm", "test"]],
        build=[["npm", "run", "build"]],
        typecheck=[["npx", "tsc", "--noEmit"]],
    )
    assert policy.to_mapping() == {
        "test": [["npm", "test"]],
        "build": [["npm", "run", "build"]],
        "typecheck": [["npx", "tsc", "--noEmit"]],
    }


def test_non_pytest_short_flags_and_attached_pytest_values_are_allowed():
    policy = RepoCommandConfig(
        test=[["python", "-m", "pytest", "-kfoo", "-p", "no:cacheprovider"]],
        build=[["dotnet", "build", "-c", "Release"]],
    )
    assert policy.to_mapping()["build"] == [["dotnet", "build", "-c", "Release"]]
