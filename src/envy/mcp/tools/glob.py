from typing import Annotated

from fastmcp.tools import tool
from mcp.types import ToolAnnotations
from pydantic import Field

from envy.mcp.toolbox import load_tool_description, resolve_sandbox, run_command

DESCRIPTION = load_tool_description(__file__)


@tool(
    name="glob",
    description=DESCRIPTION,
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def glob(
    sandbox_id: Annotated[str, Field(description="The sandbox_id to search in.")],
    pattern: Annotated[
        str, Field(description="The glob pattern to match files against")
    ],
    path: Annotated[
        str | None,
        Field(description="Directory to search; defaults to the sandbox workdir"),
    ] = None,
) -> str:
    root = path or "."
    result = run_command(
        resolve_sandbox(sandbox_id).sandbox,
        "rg",
        "--files",
        "--glob",
        pattern,
        root,
    )
    if not result.ok:
        return result.stderr.strip() or "(glob failed)"
    return result.stdout.strip() or "No files found"
