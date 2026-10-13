"""Resolve trusted CLI argv without routing prompts through a command shell."""

import json
import shutil
from pathlib import Path, PureWindowsPath
from typing import Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SHELL_NAMES = {
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
    "sh", "bash", "zsh", "dash", "fish", "sh.exe", "bash.exe",
}
SHELL_SUFFIXES = {".cmd", ".bat", ".ps1"}


def _validate_executable(command: str) -> None:
    """Reject batch shims, including ones selected implicitly through PATH."""
    resolved = shutil.which(command) or command
    for candidate in (command, resolved):
        name = candidate.replace("\\", "/").rsplit("/", 1)[-1].casefold()
        if name in SHELL_NAMES or Path(name).suffix in SHELL_SUFFIXES:
            raise ValueError(
                "CLI shell launchers are unsupported; configure an argv prefix "
                "with the native executable and its script entry point"
            )


def _local_gemini_argv(repository_root: Path) -> list[str] | None:
    package = repository_root / ".runtime/gemini/node_modules/@google/gemini-cli"
    manifest_path = package / "package.json"
    if not manifest_path.exists():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("name") != "@google/gemini-cli":
        raise ValueError("Local Gemini package manifest has an unexpected identity")
    bin_entry = manifest.get("bin")
    if isinstance(bin_entry, dict):
        bin_entry = bin_entry.get("gemini")
    if not isinstance(bin_entry, str) or not bin_entry.strip():
        raise ValueError("Local Gemini package has no valid gemini bin entry")
    relative_entry = Path(bin_entry)
    if relative_entry.is_absolute() or PureWindowsPath(bin_entry).drive:
        raise ValueError("Local Gemini bin entry must stay inside its package")
    package_root = package.resolve()
    entry = (package / relative_entry).resolve()
    if not entry.is_relative_to(package_root) or not entry.is_file():
        raise ValueError("Local Gemini bin entry is missing or outside its package")
    node = shutil.which("node")
    if not node:
        raise ValueError("Local Gemini package requires a Node executable on PATH")
    _validate_executable(node)
    return [node, str(entry)]


def build_cli_argv(
    cli_cmd: str,
    argv_prefix: Sequence[str] | None = None,
    *,
    repository_root: Path | None = None,
) -> list[str]:
    """Return a literal argv prefix; explicit configuration wins over discovery."""
    if argv_prefix is not None and not isinstance(argv_prefix, (list, tuple)):
        raise ValueError("CLI argv prefix must be a list of literal strings")
    values = list(argv_prefix) if argv_prefix else [cli_cmd]
    if any(not isinstance(value, str) or not value.strip() or "\0" in value for value in values):
        raise ValueError("CLI argv prefix entries must be nonempty strings without NUL")
    if not argv_prefix and cli_cmd == "gemini":
        local = _local_gemini_argv(repository_root or REPOSITORY_ROOT)
        if local:
            return local
    _validate_executable(values[0])
    return values
