"""Shared Modal sandbox primitives for Envy's MCP tools."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from shlex import join as shlex_join
from typing import Any

import modal

from ..env import Env
from ..modal import Envy
from .errors import NotOurSandboxError, SandboxCommandError

APP_TAG = "envy.app"
ENV_TAG = "envy.env"
READ_MAX_LINES = 2000
READ_MAX_LINE_LENGTH = 2000

_envies: list[Envy] = []


@dataclass(frozen=True, slots=True)
class OwnedSandbox:
    """A Modal sandbox together with the Envy environment that owns it."""

    sandbox: modal.Sandbox
    envy: Envy
    environment: Env[Any]


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured output from a command executed inside a sandbox."""

    stdout: str
    stderr: str
    returncode: int
    command: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def check(self) -> CommandResult:
        """Return the result if successful, otherwise raise a tool-friendly error."""
        if self.ok:
            return self
        rendered = shlex_join(self.command) or "<command>"
        detail = self.stderr.strip() or self.stdout.strip()
        message = f"{rendered!r} exited with status {self.returncode}"
        raise SandboxCommandError(f"{message}: {detail}" if detail else message)

    def merged_output(self) -> str:
        """Return stdout and stderr as one clean stream."""
        return "\n".join(
            part for part in (self.stdout.rstrip(), self.stderr.rstrip()) if part
        )

    def shell_output(self) -> str:
        """Render command output the way a terminal-facing tool should."""
        output = self.merged_output()
        if self.ok:
            return output
        exit_line = f"[exit code: {self.returncode}]"
        return f"{output}\n{exit_line}" if output else exit_line


def load_tool_description(module_file: str) -> str:
    """Load the Markdown-ish description next to a tool module."""
    return Path(module_file).with_suffix(".txt").read_text(encoding="utf-8")


def require_absolute_path(path: str, *, parameter: str = "file_path") -> str:
    """Return ``path`` if absolute, otherwise raise a tool-friendly error."""
    if not PurePosixPath(path).is_absolute():
        raise ValueError(f"{parameter} must be an absolute path")
    return path


def validate_line_window(*, offset: int | None, limit: int | None) -> tuple[int, int]:
    """Return zero-based start and count for a 1-indexed line window."""
    if offset is not None and offset < 1:
        raise ValueError("offset must be a 1-indexed line number")
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    return (offset - 1 if offset else 0, limit if limit is not None else READ_MAX_LINES)


def register_envy(envy: Envy) -> None:
    """Register an Envy app for shared file/shell tool ownership checks."""
    if not any(existing is envy for existing in _envies):
        _envies.append(envy)


def unregister_envy(envy: Envy) -> None:
    """Undo a prior registration."""
    _envies[:] = [existing for existing in _envies if existing is not envy]


def set_envy(envy: Envy | None) -> None:
    """Replace registered apps; primarily useful for isolated tests."""
    _envies.clear()
    if envy is not None:
        _envies.append(envy)


def _owned_environment(
    tags: Mapping[str, str], envies: Sequence[Envy]
) -> tuple[Envy, Env[Any]] | None:
    app_name = tags.get(APP_TAG)
    environment_name = tags.get(ENV_TAG)
    if app_name is None or environment_name is None:
        return None
    for envy in envies:
        if envy.name == app_name and environment_name in envy.environments:
            return envy, envy.environment(environment_name)
    return None


def resolve_sandbox(
    sandbox_id: str, *, envies: Sequence[Envy] | None = None
) -> OwnedSandbox:
    """Resolve and ownership-check a sandbox by id."""
    registered = _envies if envies is None else envies
    sandbox = modal.Sandbox.from_id(sandbox_id)
    owner = _owned_environment(sandbox.get_tags(), registered)
    if owner is None:
        raise NotOurSandboxError(
            f"sandbox {sandbox_id!r} was not created by a registered Envy app"
        )
    envy, environment = owner
    return OwnedSandbox(sandbox=sandbox, envy=envy, environment=environment)


def require_registered_sandbox(sandbox_id: str, envy: Envy) -> OwnedSandbox:
    """Resolve a sandbox and require ownership by one specific Envy app."""
    return resolve_sandbox(sandbox_id, envies=[envy])


def run_command(
    sandbox: modal.Sandbox, *args: str, timeout: int | None = None
) -> CommandResult:
    """Run a command in a sandbox and capture stdout, stderr, and return code."""
    proc = sandbox.exec(*args, timeout=timeout)
    stdout = proc.stdout.read()
    stderr = proc.stderr.read()
    proc.wait()
    return CommandResult(
        stdout=stdout, stderr=stderr, returncode=proc.returncode, command=args
    )
