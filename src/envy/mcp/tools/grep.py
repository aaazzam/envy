from typing import Annotated

from fastmcp.tools import tool
from mcp.types import ToolAnnotations
from pydantic import Field

from envy.mcp.toolbox import load_tool_description, resolve_sandbox, run_command

DESCRIPTION = load_tool_description(__file__)


@tool(
    name="grep",
    description=DESCRIPTION,
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def grep(
    sandbox_id: Annotated[str, Field(description="The sandbox_id to search in.")],
    pattern: Annotated[str, Field(description="The regex pattern to search for")],
    path: Annotated[
        str | None, Field(description="Directory to search; defaults to the workdir")
    ] = None,
    include: Annotated[
        str | None, Field(description="Optional ripgrep file glob filter")
    ] = None,
) -> str:
    if not pattern:
        raise ValueError("pattern is required")
    args = ["rg", "--color=never", "--no-heading", "--line-number"]
    if include:
        args += ["--glob", include]
    args += [pattern, path or "."]
    result = run_command(resolve_sandbox(sandbox_id).sandbox, *args)
    if result.returncode not in (0, 1):
        return result.stderr.strip() or f"(grep failed, exit {result.returncode})"
    return result.stdout.strip() or "No matches found"
