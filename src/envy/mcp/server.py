"""FastMCP server and provider for Envy-managed Modal sandboxes."""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

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
from .toolbox import APP_TAG, register_envy, require_registered_sandbox

if TYPE_CHECKING:
    from fastmcp.server.auth import AuthProvider
    from fastmcp.server.middleware import Middleware

TOOLS_DIR = Path(__file__).parent / "tools"


class SandboxInfo(BaseModel):
    """What ``create_sandbox`` returns to the MCP client."""

    sandbox_id: str
    environment: str
    workdir: str


class EnvyProvider(AggregateProvider):
    """Expose one :class:`envy.Envy` app as FastMCP tools.

    The provider launches directly from the Envy declaration. File and shell
    tools resolve only sandboxes tagged as belonging to this app, so an
    arbitrary Modal sandbox id cannot be used as a handle.
    """

    def __init__(
        self,
        envy: Envy,
        *,
        timeout: int = 60 * 60,
        idle_timeout: int | None = 15 * 60,
        runner: ModalRunner | None = None,
    ) -> None:
        self.envy = envy
        self.runner = runner or ModalRunner(
            envy, timeout=timeout, idle_timeout=idle_timeout
        )
        register_envy(envy)

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
            sandbox = runner.launch(environment, tags={APP_TAG: envy.name})
            sandbox_id = sandbox.object_id
            return SandboxInfo(
                sandbox_id=sandbox_id,
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
            return sandbox_id

        return kill_sandbox


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
    **fastmcp_settings: Any,
) -> FastMCP:
    """Create a FastMCP server exposing an Envy app's sandbox tools."""
    return FastMCP(
        name=name or envy.name,
        instructions=instructions,
        auth=auth,
        middleware=middleware,
        providers=[
            EnvyProvider(envy, timeout=timeout, idle_timeout=idle_timeout),
            *(providers or ()),
        ],
        **fastmcp_settings,
    )
