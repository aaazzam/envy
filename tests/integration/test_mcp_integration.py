from __future__ import annotations

import asyncio
import os
import unittest
from typing import Any
from uuid import uuid4

try:
    import fastmcp

    try:
        import httpx2 as httpx
    except ImportError:
        import httpx
    import modal
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from starlette.applications import Starlette
    from starlette.routing import Mount
except ImportError:
    fastmcp = None
    httpx = None
    modal = None

from envy import Envy, apt_install


def result_text(result: Any) -> str:
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


@unittest.skipUnless(
    fastmcp is not None
    and httpx is not None
    and modal is not None
    and os.environ.get("ENVY_RUN_MODAL_MCP_INTEGRATION") == "1",
    "set ENVY_RUN_MODAL_MCP_INTEGRATION=1 with a configured Modal profile",
)
class MCPModalIntegrationTests(unittest.TestCase):
    def test_mcp_http_server_controls_a_real_modal_sandbox(self) -> None:
        asyncio.run(self._exercise_mcp_server())

    async def _exercise_mcp_server(self) -> None:
        assert fastmcp is not None
        assert httpx is not None
        assert modal is not None
        from envy.mcp import create_server

        app = Envy(f"envy-mcp-integration-{uuid4().hex[:8]}")
        environment = app.env(
            "integration",
            base=modal.Image.debian_slim(),
            build=[apt_install("ripgrep")],
        )
        server = create_server(app, timeout=300, idle_timeout=60)
        mcp_app = server.http_app(stateless_http=True, json_response=True)
        asgi_app = Starlette(
            routes=[Mount("/", app=mcp_app)],
            lifespan=mcp_app.lifespan,
        )

        def httpx_client_factory(**kwargs: Any) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                base_url="http://testserver",
                transport=httpx.ASGITransport(app=asgi_app),
                **kwargs,
            )

        transport = StreamableHttpTransport(
            "http://testserver/mcp",
            httpx_client_factory=httpx_client_factory,
        )

        sandbox_id: str | None = None
        async with mcp_app.lifespan(asgi_app), Client(transport) as client:
            names = {tool.name for tool in await client.list_tools()}
            self.assertTrue(
                {
                    "create_sandbox",
                    "kill_sandbox",
                    "bash",
                    "read",
                    "write",
                    "edit",
                    "glob",
                    "grep",
                }.issubset(names)
            )

            try:
                created = await client.call_tool(
                    "create_sandbox", {"environment": environment.name}
                )
                self.assertIsNotNone(created.data)
                sandbox_id = created.data.sandbox_id
                workdir = created.data.workdir

                await client.call_tool(
                    "write",
                    {
                        "sandbox_id": sandbox_id,
                        "file_path": f"{workdir}/message.txt",
                        "content": "hello modal\n",
                    },
                )
                read_before = await client.call_tool(
                    "read",
                    {
                        "sandbox_id": sandbox_id,
                        "file_path": f"{workdir}/message.txt",
                    },
                )
                self.assertEqual(result_text(read_before), "     1\thello modal")

                await client.call_tool(
                    "edit",
                    {
                        "sandbox_id": sandbox_id,
                        "file_path": f"{workdir}/message.txt",
                        "old_string": "hello modal",
                        "new_string": "hello from MCP",
                    },
                )
                bash_result = await client.call_tool(
                    "bash",
                    {
                        "sandbox_id": sandbox_id,
                        "command": "cat message.txt",
                        "workdir": workdir,
                    },
                )
                self.assertEqual(result_text(bash_result), "hello from MCP")

                glob_result = await client.call_tool(
                    "glob",
                    {
                        "sandbox_id": sandbox_id,
                        "pattern": "*.txt",
                        "path": workdir,
                    },
                )
                self.assertIn("message.txt", result_text(glob_result))

                grep_result = await client.call_tool(
                    "grep",
                    {
                        "sandbox_id": sandbox_id,
                        "pattern": "MCP",
                        "path": workdir,
                        "include": "*.txt",
                    },
                )
                self.assertIn("message.txt", result_text(grep_result))
                self.assertIn("hello from MCP", result_text(grep_result))
            finally:
                if sandbox_id is not None:
                    killed = await client.call_tool(
                        "kill_sandbox", {"sandbox_id": sandbox_id}
                    )
                    self.assertEqual(result_text(killed), sandbox_id)


if __name__ == "__main__":
    unittest.main()
