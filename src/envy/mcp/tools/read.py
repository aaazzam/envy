from typing import Annotated

from fastmcp.tools import tool
from mcp.types import ToolAnnotations
from modal.file_io import FileIO
from pydantic import Field

from envy.mcp.errors import NotAFileError
from envy.mcp.toolbox import (
    READ_MAX_LINE_LENGTH,
    load_tool_description,
    require_absolute_path,
    resolve_sandbox,
    validate_line_window,
)

DESCRIPTION = load_tool_description(__file__)


@tool(
    name="read",
    description=DESCRIPTION,
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def read(
    sandbox_id: Annotated[str, Field(description="The sandbox_id to read from.")],
    file_path: Annotated[
        str, Field(description="The absolute path to the file to read")
    ],
    offset: Annotated[
        int | None, Field(description="The 1-indexed line number to start reading from")
    ] = None,
    limit: Annotated[
        int | None, Field(description="Maximum lines to read; defaults to 2000")
    ] = None,
) -> str:
    file_path = require_absolute_path(file_path)
    start, count = validate_line_window(offset=offset, limit=limit)
    sandbox = resolve_sandbox(sandbox_id).sandbox
    try:
        file: FileIO[str] = sandbox.open(file_path, "r")
        with file:
            content = file.read()
    except IsADirectoryError as exc:
        raise NotAFileError(
            f"{file_path} is a directory; use glob or grep to inspect it"
        ) from exc

    lines = content.splitlines()
    selected = lines[start : start + count]
    rendered: list[str] = []
    for number, line in enumerate(selected, start=start + 1):
        rendered.append(f"{number:>6}\t{line[:READ_MAX_LINE_LENGTH]}")
    return "\n".join(rendered)
