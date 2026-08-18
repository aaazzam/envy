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
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ..modal import Envy, ModalRunner
from .github import create_pull_request, parse_github_remote
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


class PullRequestInfo(BaseModel):
    """Result returned after a privileged branch push and PR creation."""

    sandbox_id: str
    branch: str
    commit: str
    pull_request_url: str


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
        github_api_url: str = "https://api.github.com",
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
        self.github_api_url = github_api_url
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
        local.tool(
            self._publish_pull_request(),
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=True, openWorldHint=True
            ),
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

    def _publish_pull_request(self) -> Callable[..., PullRequestInfo]:
        envy = self.envy
        runner = self.runner

        def publish_pull_request(
            sandbox_id: Annotated[
                str,
                Field(
                    description=(
                        "The stable sandbox_id returned by create_sandbox. "
                        "The working tree must already be committed and clean."
                    )
                ),
            ],
            title: Annotated[str, Field(description="Pull request title")],
            body: Annotated[str, Field(description="Pull request description")] = "",
            base: Annotated[
                str, Field(description="Target branch for the pull request")
            ] = "main",
            draft: Annotated[
                bool, Field(description="Create the pull request as a draft")
            ] = True,
        ) -> PullRequestInfo:
            """Push a clean committed branch and create a GitHub pull request.

            The canonical agent sandbox never receives the Git secret. It is
            terminated into an exit snapshot, restored without secrets, and a
            separate hidden sandbox receives the snapshot plus the configured
            secret only for the push.
            """
            if self.git_secret is None:
                raise ValueError(
                    "publish_pull_request requires git_secret on the MCP server"
                )
            token = os.environ.get(self.git_token_env)
            if not token:
                raise ValueError(
                    f"the MCP server must have {self.git_token_env} set "
                    "for GitHub pull request creation"
                )
            if not title.strip():
                raise ValueError("title must not be empty")
            if not base.strip():
                raise ValueError("base must not be empty")

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
                    "calling publish_pull_request"
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

            # Recreate the agent-visible workspace without the Git secret
            # before starting the privileged publisher.
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

            response = create_pull_request(
                repository,
                token=token,
                head=branch,
                base=base,
                title=title,
                body=body,
                draft=draft,
                api_url=self.github_api_url,
            )
            pull_request_url = response.get("html_url")
            if not isinstance(pull_request_url, str) or not pull_request_url:
                raise RuntimeError("GitHub did not return a pull request URL")
            return PullRequestInfo(
                sandbox_id=sandbox_id,
                branch=branch,
                commit=commit,
                pull_request_url=pull_request_url,
            )

        return publish_pull_request


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
    github_api_url: str = "https://api.github.com",
    workspace_store: WorkspaceStore | None = None,
    **fastmcp_settings: Any,
) -> FastMCP:
    """Create a FastMCP server exposing an Envy app's sandbox tools."""
    return FastMCP(
        name=name or envy.name,
        instructions=instructions,
        auth=auth,
        middleware=middleware,
        providers=[
            EnvyProvider(
                envy,
                timeout=timeout,
                idle_timeout=idle_timeout,
                git_secret=git_secret,
                git_token_env=git_token_env,
                github_api_url=github_api_url,
                workspace_store=workspace_store,
            ),
            *(providers or ()),
        ],
        **fastmcp_settings,
    )
