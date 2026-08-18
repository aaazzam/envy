"""FastMCP transforms for publishing Envy workspaces through GitHub tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from anyio import to_thread
from fastmcp.server.dependencies import get_context
from fastmcp.server.transforms import GetToolNext, Transform, VersionSpec
from fastmcp.tools import Tool
from fastmcp.tools.tool_transform import forward

Publisher = Callable[[str], object]


def _sandbox_id_from_request() -> str | None:
    try:
        request_context = get_context().request_context
    except RuntimeError:
        return None
    if request_context is None:
        return None

    metadata = request_context.meta
    if isinstance(metadata, Mapping):
        values = cast(dict[str, object], metadata)
    else:
        extras = getattr(metadata, "model_extra", None)
        if not isinstance(extras, Mapping):
            return None
        values = cast(dict[str, object], extras)
    for key in ("envy.sandbox_id", "sandbox_id"):
        value = values.get(key)
        if isinstance(value, str) and value:
            return value
    return None


class WorkspacePublishTransform(Transform):
    """Push the current Envy workspace before branch-publication calls.

    The workspace handle is carried in per-invocation metadata rather than
    added to the GitHub tool schemas. Calls without that metadata remain
    ordinary GitHub API calls, which is useful for already-pushed branches and
    for PR metadata updates that do not involve a local checkout.
    """

    _BRANCH_PUBLISH_TOOLS = ("create_pull_request", "update_pull_request")

    def __init__(self, publisher: Publisher) -> None:
        super().__init__()
        self.publisher = publisher

    def _should_wrap(self, name: str) -> bool:
        return any(
            name == tool or name.endswith(f"_{tool}")
            for tool in self._BRANCH_PUBLISH_TOOLS
        )

    def _wrap(self, tool: Tool) -> Tool:
        if not self._should_wrap(tool.name):
            return tool

        async def publish_then_forward(**kwargs: Any) -> Any:
            sandbox_id = _sandbox_id_from_request()
            if sandbox_id is not None:
                await to_thread.run_sync(self.publisher, sandbox_id)
            return await forward(**kwargs)

        return Tool.from_tool(tool, transform_fn=publish_then_forward)

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [self._wrap(tool) for tool in tools]

    async def get_tool(
        self,
        name: str,
        call_next: GetToolNext,
        *,
        version: VersionSpec | None = None,
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        return self._wrap(tool) if tool is not None else None
