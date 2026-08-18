from typing import Annotated

from fastmcp.tools import tool
from mcp.types import ToolAnnotations
from modal.file_io import FileIO
from pydantic import Field

from envy.mcp.toolbox import (
    load_tool_description,
    require_absolute_path,
    resolve_sandbox,
)

DESCRIPTION = load_tool_description(__file__)


@tool(
    name="write",
    description=DESCRIPTION,
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, openWorldHint=False
    ),
)
def write(
    sandbox_id: Annotated[str, Field(description="The sandbox_id to write to.")],
    file_path: Annotated[
        str, Field(description="The absolute path to the file to write")
    ],
    content: Annotated[str, Field(description="The content to write to the file")],
) -> str:
    file_path = require_absolute_path(file_path)
    file: FileIO[str] = resolve_sandbox(sandbox_id).sandbox.open(file_path, "w")
    with file:
        file.write(content)
    return f"File written successfully to {file_path}"
