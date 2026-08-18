from math import ceil
from shlex import quote
from typing import Annotated

from fastmcp.tools import tool
from mcp.types import ToolAnnotations
from pydantic import Field

from envy.mcp.toolbox import load_tool_description, resolve_sandbox, run_command

DESCRIPTION = load_tool_description(__file__)
MAX_TIMEOUT_MS = 600_000


def _timeout_seconds(timeout_ms: int | None) -> int | None:
    if timeout_ms is None:
        return None
    if timeout_ms < 1:
        raise ValueError("timeout must be a positive number of milliseconds")
    return ceil(min(timeout_ms, MAX_TIMEOUT_MS) / 1000)


@tool(
    name="bash",
    description=DESCRIPTION,
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, openWorldHint=True
    ),
)
def bash(
    sandbox_id: Annotated[
        str, Field(description="The sandbox_id to run the command in.")
    ],
    command: Annotated[str, Field(description="The command to execute")],
    timeout: Annotated[
        int | None,
        Field(description="Optional timeout in milliseconds", ge=1, le=MAX_TIMEOUT_MS),
    ] = None,
    workdir: Annotated[
        str | None,
        Field(description="Working directory; use this instead of a cd command."),
    ] = None,
) -> str:
    sandbox = resolve_sandbox(sandbox_id).sandbox
    if workdir:
        command = f"cd {quote(workdir)} && {command}"
    result = run_command(
        sandbox,
        "bash",
        "-c",
        command,
        timeout=_timeout_seconds(timeout),
    )
    return result.shell_output()
