"""FastMCP server and provider for Envy-managed Modal sandboxes."""

import inspect
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from re import fullmatch
from typing import TYPE_CHECKING, Annotated, Any, cast
from uuid import uuid4

from fastmcp import FastMCP
from fastmcp.server.providers import (
    AggregateProvider,
    FileSystemProvider,
    LocalProvider,
    Provider,
)
from fastmcp.server.transforms import Transform
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ..modal import Envy, ModalRunner
from .github import PublishedBranch, github_provider, parse_github_remote
from .github_transform import WorkspacePublishTransform
from .toolbox import (
    APP_TAG,
    ENV_TAG,
    ModalWorkspaceStore,
    WorkspaceStore,
    get_workspace,
    register_envy,
    register_workspace,
    register_workspace_runtime,
    require_registered_sandbox,
    run_command,
    unregister_workspace,
)

if TYPE_CHECKING:
    from fastmcp.server.auth import AuthProvider
    from fastmcp.server.middleware import Middleware

TOOLS_DIR = Path(__file__).parent / "tools"


class SandboxInfo(BaseModel):
    """What ``create_sandbox`` returns to the MCP client."""

    sandbox_id: str
    workspace_id: str
    environment: str
    workdir: str


def _workspace_registry_name(app_name: str) -> str:
    normalized = "".join(
        character.lower() if character.isalnum() else "-" for character in app_name
    ).strip("-")
    return f"envy-{normalized or 'app'}-workspaces"


def _accepts_keyword(function: Callable[..., object], keyword: str) -> bool:
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == keyword or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


class EnvyProvider(AggregateProvider):
    """Expose one :class:`envy.Envy` app as FastMCP tools.

    ``create_sandbox`` returns a stable logical workspace handle. The handle
    is persisted independently from the physical Modal Sandbox ID, allowing
    an exited sandbox to be replaced from its exit snapshot. Git publishing
    uses a second, unregistered sandbox that receives the Git secret.
    """

    def __init__(
        self,
        envy: Envy,
        *,
        timeout: int = 60 * 60,
        idle_timeout: int | None = 15 * 60,
        runner: ModalRunner | None = None,
        workspace_store: WorkspaceStore | None = None,
        git_secret: object | None = None,
        git_token_env: str = "GITHUB_TOKEN",
    ) -> None:
        if not fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", git_token_env):
            raise ValueError("git_token_env must be a valid environment variable name")
        self.envy = envy
        self.runner = runner or ModalRunner(
            envy, timeout=timeout, idle_timeout=idle_timeout
        )
        self.workspace_store = workspace_store or ModalWorkspaceStore(
            self.runner,
            name=_workspace_registry_name(envy.name),
            environment_name=getattr(self.runner, "environment_name", None),
        )
        self.git_secret = git_secret
        self.git_token_env = git_token_env
        register_envy(envy)
        register_workspace_runtime(envy, self.runner, self.workspace_store)

        local = LocalProvider()
        local.tool(
            self._create_sandbox(),
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, openWorldHint=True
            ),
        )
        local.tool(
            self._kill_sandbox(),
            annotations=ToolAnnotations(destructiveHint=True),
        )
        super().__init__(providers=[local, FileSystemProvider(TOOLS_DIR)])

    def _create_sandbox(self) -> Callable[..., SandboxInfo]:
        envy = self.envy
        runner = self.runner

        def create_sandbox(
            environment: Annotated[
                str,
                Field(
                    description="Registered Envy environment to launch.",
                    json_schema_extra={"enum": list(envy.environments)},
                ),
            ],
        ) -> SandboxInfo:
            """Create a sandbox for a registered Envy environment."""
            env = envy.environment(environment)
            launch_kwargs: dict[str, object] = {"tags": {APP_TAG: envy.name}}
            if _accepts_keyword(runner.launch, "experimental_options"):
                launch_kwargs["experimental_options"] = {"enable_exit_snapshot": True}
            sandbox = runner.launch(environment, **launch_kwargs)  # type: ignore[call-arg]
            physical_id = sandbox.object_id
            # The fallback keeps custom legacy runners source-compatible. The
            # ModalRunner path always uses the stable logical handle.
            workspace_id = (
                f"ws-{uuid4().hex}"
                if "experimental_options" in launch_kwargs
                else physical_id
            )
            tags = {APP_TAG: envy.name, ENV_TAG: environment}
            get_tags = getattr(sandbox, "get_tags", None)
            if callable(get_tags):
                raw_tags = get_tags()
                if isinstance(raw_tags, Mapping):
                    typed_tags = cast(Mapping[object, object], raw_tags)
                    tags.update(
                        {str(key): str(value) for key, value in typed_tags.items()}
                    )
            register_workspace(
                workspace_id,
                sandbox=sandbox,
                envy=envy,
                environment=env,
                runner=runner,
                store=self.workspace_store,
                tags=tags,
            )
            return SandboxInfo(
                sandbox_id=workspace_id,
                workspace_id=workspace_id,
                environment=environment,
                workdir=env.workdir,
            )

        return create_sandbox

    def _kill_sandbox(self) -> Callable[..., str]:
        envy = self.envy

        def kill_sandbox(
            sandbox_id: Annotated[str, Field(description="The sandbox_id to kill.")],
        ) -> str:
            """Terminate a sandbox created by this Envy app and return its id."""
            owned = require_registered_sandbox(sandbox_id, envy)
            owned.sandbox.terminate(wait=True)
            owned.sandbox.detach()
            unregister_workspace(sandbox_id)
            return sandbox_id

        return kill_sandbox

    def publish_workspace_branch(self, sandbox_id: str) -> PublishedBranch:
        """Push a committed workspace branch from an isolated publisher.

        This is intentionally a Python helper rather than an MCP tool. A
        GitHub PR tool can invoke it through ``WorkspacePublishTransform``
        when the request carries ``envy.sandbox_id`` metadata.
        """

        envy = self.envy
        runner = self.runner
        if self.git_secret is None:
            raise ValueError(
                "workspace publishing requires git_secret on the MCP server"
            )
        if not os.environ.get(self.git_token_env):
            raise ValueError(
                f"the MCP server must have {self.git_token_env} set "
                "for GitHub workspace publishing"
            )

        record = get_workspace(sandbox_id, envy=envy)
        owned = require_registered_sandbox(sandbox_id, envy)
        workdir = record.environment.workdir
        status = run_command(
            owned.sandbox,
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            workdir=workdir,
        ).check()
        if status.stdout.strip():
            raise ValueError(
                "working tree is dirty; stage and commit all changes before "
                "calling a GitHub pull request write tool"
            )
        branch = (
            run_command(
                owned.sandbox, "git", "branch", "--show-current", workdir=workdir
            )
            .check()
            .stdout.strip()
        )
        if not branch:
            raise ValueError("cannot publish a detached HEAD")
        if branch in {"main", "master", "trunk"}:
            raise ValueError(
                f"refusing to open a pull request from protected branch {branch!r}"
            )
        commit = (
            run_command(owned.sandbox, "git", "rev-parse", "HEAD", workdir=workdir)
            .check()
            .stdout.strip()
        )
        remote = (
            run_command(
                owned.sandbox,
                "git",
                "remote",
                "get-url",
                "origin",
                workdir=workdir,
            )
            .check()
            .stdout.strip()
        )
        repository = parse_github_remote(remote)

        original = record.sandbox
        original.terminate(wait=True)
        try:
            snapshot = record.capture_exit_snapshot()
        finally:
            original.detach()

        # Recreate the agent-visible workspace without the Git secret before
        # starting the privileged publisher.
        record.replace_from_snapshot(snapshot)

        hidden = runner.launch_from_snapshot(
            str(record.environment.name),
            snapshot,
            tags={"envy.publisher": "true"},
            secrets=(self.git_secret,),
        )
        askpass_path = "/tmp/envy-git-askpass"
        askpass = (
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
            f"  *Password*) printf '%s\\n' \"${self.git_token_env}\" ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n"
        )
        try:
            file = hidden.open(askpass_path, "w")
            with file:
                file.write(askpass)
            run_command(hidden, "chmod", "700", askpass_path).check()
            run_command(
                hidden,
                "git",
                "remote",
                "set-url",
                "origin",
                f"https://github.com/{repository.owner}/{repository.name}.git",
                workdir=workdir,
            ).check()
            run_command(
                hidden,
                "git",
                "push",
                "--set-upstream",
                "origin",
                branch,
                workdir=workdir,
                env={
                    "GIT_ASKPASS": askpass_path,
                    "GIT_TERMINAL_PROMPT": "0",
                },
                timeout=10 * 60,
            ).check()
        finally:
            try:
                run_command(hidden, "rm", "-f", askpass_path).check()
            finally:
                hidden.terminate(wait=True)
                hidden.detach()

        return PublishedBranch(repository=repository, branch=branch, commit=commit)


def create_server(
    envy: Envy,
    *,
    name: str | None = None,
    instructions: str | None = None,
    auth: "AuthProvider | None" = None,
    middleware: "Sequence[Middleware] | None" = None,
    providers: Sequence[Provider] | None = None,
    timeout: int = 60 * 60,
    idle_timeout: int | None = 15 * 60,
    git_secret: object | None = None,
    git_token_env: str = "GITHUB_TOKEN",
    github_mcp_url: str | None = None,
    github_mcp_namespace: str | None = "github",
    tool_search: bool = True,
    tool_search_max_results: int = 5,
    tool_search_always_visible: Sequence[str] = ("create_sandbox", "kill_sandbox"),
    workspace_store: WorkspaceStore | None = None,
    **fastmcp_settings: Any,
) -> FastMCP:
    """Create a FastMCP server exposing an Envy app's sandbox tools."""
    envy_provider = EnvyProvider(
        envy,
        timeout=timeout,
        idle_timeout=idle_timeout,
        git_secret=git_secret,
        git_token_env=git_token_env,
        workspace_store=workspace_store,
    )
    composed_providers: list[Provider] = [envy_provider]
    if github_mcp_url:
        composed_providers.append(
            github_provider(
                github_mcp_url,
                token_env=git_token_env,
                namespace=github_mcp_namespace,
            )
        )
    composed_providers.extend(providers or ())

    transforms: list[Transform] = list(fastmcp_settings.pop("transforms", ()))
    transforms.insert(
        0, WorkspacePublishTransform(envy_provider.publish_workspace_branch)
    )
    if tool_search:
        from fastmcp.server.transforms.search import BM25SearchTransform

        transforms.append(
            BM25SearchTransform(
                max_results=tool_search_max_results,
                always_visible=list(tool_search_always_visible),
            )
        )
    return FastMCP(
        name=name or envy.name,
        instructions=instructions,
        auth=auth,
        middleware=middleware,
        providers=composed_providers,
        transforms=transforms,
        **fastmcp_settings,
    )
