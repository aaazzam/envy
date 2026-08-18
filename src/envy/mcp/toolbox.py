"""Shared Modal sandbox primitives for Envy's MCP tools."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from shlex import join as shlex_join
from typing import Any, Protocol, cast

import modal

from ..env import Env
from ..modal import Envy
from .errors import NotOurSandboxError, SandboxCommandError

APP_TAG = "envy.app"
ENV_TAG = "envy.env"
READ_MAX_LINES = 2000
READ_MAX_LINE_LENGTH = 2000

_envies: list[Envy] = []


class WorkspaceStore(Protocol):
    """Small persistence interface for logical workspace handles."""

    def get(self, workspace_id: str) -> Mapping[str, Any] | None: ...

    def put(self, workspace_id: str, value: Mapping[str, Any]) -> None: ...

    def delete(self, workspace_id: str) -> None: ...


class InMemoryWorkspaceStore:
    """Process-local store used by tests and non-Modal callers."""

    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    def get(self, workspace_id: str) -> Mapping[str, Any] | None:
        return self.values.get(workspace_id)

    def put(self, workspace_id: str, value: Mapping[str, Any]) -> None:
        self.values[workspace_id] = dict(value)

    def delete(self, workspace_id: str) -> None:
        self.values.pop(workspace_id, None)


class ModalWorkspaceStore:
    """Persist logical handles in a Modal Dict when the SDK supports it."""

    def __init__(self, runner: Any, *, name: str, environment_name: str | None) -> None:
        self.runner = runner
        self.name = name
        self.environment_name = environment_name
        self._registry: Any | None = None
        self._fallback = InMemoryWorkspaceStore()

    @property
    def registry(self) -> Any | None:
        if self._registry is not None:
            return self._registry
        try:
            modal_api = self.runner.modal
        except (AttributeError, RuntimeError):
            return None
        dict_type = getattr(modal_api, "Dict", None)
        if dict_type is None:
            return None
        self._registry = dict_type.from_name(
            self.name,
            create_if_missing=True,
            environment_name=self.environment_name,
        )
        return self._registry

    def get(self, workspace_id: str) -> Mapping[str, Any] | None:
        registry = self.registry
        if registry is None:
            return self._fallback.get(workspace_id)
        value: Any = registry.get(workspace_id)
        return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else None

    def put(self, workspace_id: str, value: Mapping[str, Any]) -> None:
        registry = self.registry
        if registry is None:
            self._fallback.put(workspace_id, value)
            return
        registry[workspace_id] = dict(value)

    def delete(self, workspace_id: str) -> None:
        registry = self.registry
        if registry is None:
            self._fallback.delete(workspace_id)
            return
        del registry[workspace_id]


class WorkspaceRunner(Protocol):
    def launch_from_snapshot(
        self,
        environment: str,
        snapshot: object,
        *,
        name: str | None = None,
        tags: dict[str, str] | None = None,
        secrets: Sequence[object] | None = None,
    ) -> modal.Sandbox: ...


@dataclass(slots=True)
class WorkspaceRecord:
    """Stable logical handle and the currently attached physical sandbox."""

    workspace_id: str
    physical_id: str
    sandbox: modal.Sandbox
    envy: Envy
    environment: Env[Any]
    runner: WorkspaceRunner
    store: WorkspaceStore
    tags: dict[str, str]

    def state(self) -> dict[str, Any]:
        return {
            "physical_id": self.physical_id,
            "environment": str(self.environment.name),
            "workdir": self.environment.workdir,
            "tags": dict(self.tags),
        }

    def remember(self) -> None:
        self.store.put(self.workspace_id, self.state())

    def capture_exit_snapshot(self, *, timeout: float | None = 60) -> object:
        getter = getattr(self.sandbox, "_experimental_get_exit_snapshot", None)
        if not callable(getter):
            raise RuntimeError(
                "Modal exit snapshots are unavailable for this sandbox; "
                "use a Modal SDK with exit snapshot support"
            )
        return getter(timeout=timeout)

    def replace_from_snapshot(
        self,
        snapshot: object,
        *,
        secrets: Sequence[object] | None = None,
        tags: dict[str, str] | None = None,
    ) -> modal.Sandbox:
        replacement = self.runner.launch_from_snapshot(
            str(self.environment.name),
            snapshot,
            tags=tags if tags is not None else dict(self.tags),
            secrets=secrets,
        )
        self.sandbox = replacement
        self.physical_id = replacement.object_id
        self.remember()
        return replacement


_workspaces: dict[str, WorkspaceRecord] = {}
_workspace_stores: dict[str, WorkspaceStore] = {}
_workspace_runners: dict[str, WorkspaceRunner] = {}


def _is_logical_workspace_id(workspace_id: str) -> bool:
    return workspace_id.startswith("ws-")


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


def register_workspace_runtime(
    envy: Envy, runner: WorkspaceRunner, store: WorkspaceStore
) -> None:
    """Register the runtime needed to reopen persisted workspace handles."""
    _workspace_stores[envy.name] = store
    _workspace_runners[envy.name] = runner


def unregister_envy(envy: Envy) -> None:
    """Undo a prior registration."""
    _envies[:] = [existing for existing in _envies if existing is not envy]


def set_envy(envy: Envy | None) -> None:
    """Replace registered apps; primarily useful for isolated tests."""
    _envies.clear()
    if envy is not None:
        _envies.append(envy)


def register_workspace(
    workspace_id: str,
    *,
    sandbox: modal.Sandbox,
    envy: Envy,
    environment: Env[Any],
    runner: WorkspaceRunner,
    store: WorkspaceStore,
    tags: Mapping[str, str],
) -> WorkspaceRecord:
    """Register a logical workspace and persist its physical attachment."""
    record = WorkspaceRecord(
        workspace_id=workspace_id,
        physical_id=sandbox.object_id,
        sandbox=sandbox,
        envy=envy,
        environment=environment,
        runner=runner,
        store=store,
        tags=dict(tags),
    )
    _workspaces[workspace_id] = record
    record.remember()
    return record


def unregister_workspace(workspace_id: str) -> None:
    """Forget a logical workspace after its sandbox has been terminated."""
    record = _workspaces.pop(workspace_id, None)
    if record is not None:
        record.store.delete(workspace_id)


def _record_from_persisted_state(
    workspace_id: str,
    *,
    envy: Envy,
    state: Mapping[str, Any],
) -> WorkspaceRecord | None:
    physical_id = state.get("physical_id")
    environment_name = state.get("environment")
    store = _workspace_stores.get(envy.name)
    runner = _workspace_runners.get(envy.name)
    if (
        not isinstance(physical_id, str)
        or not isinstance(environment_name, str)
        or store is None
        or runner is None
        or environment_name not in envy.environments
    ):
        return None
    sandbox = modal.Sandbox.from_id(physical_id)
    raw_tags = state.get("tags")
    typed_tags = cast(Mapping[object, object], raw_tags)
    tags = dict(typed_tags) if isinstance(raw_tags, Mapping) else {}
    record = WorkspaceRecord(
        workspace_id=workspace_id,
        physical_id=physical_id,
        sandbox=sandbox,
        envy=envy,
        environment=envy.environment(environment_name),
        runner=runner,
        store=store,
        tags={str(key): str(value) for key, value in tags.items()},
    )
    _workspaces[workspace_id] = record
    return record


def get_workspace(workspace_id: str, *, envy: Envy | None = None) -> WorkspaceRecord:
    """Return a registered workspace, restoring its record after a restart."""
    record = _workspaces.get(workspace_id)
    if record is not None:
        if envy is not None and record.envy is not envy:
            raise NotOurSandboxError(
                f"sandbox {workspace_id!r} was not created by this Envy app"
            )
        return record

    if not _is_logical_workspace_id(workspace_id):
        raise NotOurSandboxError(
            f"sandbox {workspace_id!r} is not a registered workspace"
        )
    registered = [envy] if envy is not None else _envies
    for candidate in registered:
        store = _workspace_stores.get(candidate.name)
        if store is None:
            continue
        state = store.get(workspace_id)
        if state is not None:
            restored = _record_from_persisted_state(
                workspace_id, envy=candidate, state=state
            )
            if restored is not None:
                return restored
    raise NotOurSandboxError(f"sandbox {workspace_id!r} is not a registered workspace")


def _refresh_workspace(record: WorkspaceRecord) -> WorkspaceRecord:
    poll = getattr(record.sandbox, "poll", None)
    if not callable(poll):
        return record
    if poll() is None:
        return record
    snapshot = record.capture_exit_snapshot()
    record.replace_from_snapshot(snapshot)
    return record


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
    """Resolve and ownership-check a logical or legacy physical sandbox id."""
    registered = _envies if envies is None else envies

    record = _workspaces.get(sandbox_id)
    if record is None and _is_logical_workspace_id(sandbox_id):
        for envy in registered:
            store = _workspace_stores.get(envy.name)
            if store is None:
                continue
            state = store.get(sandbox_id)
            if state is not None:
                record = _record_from_persisted_state(
                    sandbox_id, envy=envy, state=state
                )
                if record is not None:
                    break
    if record is not None:
        if not any(candidate is record.envy for candidate in registered):
            raise NotOurSandboxError(
                f"sandbox {sandbox_id!r} was not created by a registered Envy app"
            )
        record = _refresh_workspace(record)
        return OwnedSandbox(
            sandbox=record.sandbox,
            envy=record.envy,
            environment=record.environment,
        )

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
    sandbox: modal.Sandbox,
    *args: str,
    timeout: int | None = None,
    workdir: str | None = None,
    env: Mapping[str, str | None] | None = None,
) -> CommandResult:
    """Run a command in a sandbox and capture stdout, stderr, and return code."""
    exec_kwargs: dict[str, object] = {"timeout": timeout}
    if workdir is not None:
        exec_kwargs["workdir"] = workdir
    if env is not None:
        exec_kwargs["env"] = env
    proc = cast(Any, sandbox).exec(*args, **exec_kwargs)
    stdout = proc.stdout.read()
    stderr = proc.stderr.read()
    proc.wait()
    return CommandResult(
        stdout=stdout, stderr=stderr, returncode=proc.returncode, command=args
    )
