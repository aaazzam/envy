from __future__ import annotations

import asyncio
import unittest
from importlib.util import find_spec
from typing import Any
from unittest.mock import patch

try:
    import fastmcp

    if find_spec("modal") is None:
        raise ImportError("Modal is not installed")

    from envy import Envy
    from envy.mcp import EnvyProvider, create_server
    from envy.mcp import toolbox as toolbox_module
except ImportError:
    fastmcp = None


class FakeImage:
    def workdir(self, _path: str) -> FakeImage:
        return self


class FakeSandbox:
    def __init__(
        self,
        object_id: str = "sb-created",
        tags: dict[str, str] | None = None,
    ) -> None:
        self.object_id = object_id
        self.tags = tags or {}
        self.terminated = False
        self.detached = False

    def get_tags(self) -> dict[str, str]:
        return self.tags

    def terminate(self, *, wait: bool = False) -> None:
        assert wait is True
        self.terminated = True

    def detach(self) -> None:
        self.detached = True


class FakeRunner:
    def __init__(self, sandbox: FakeSandbox):
        self.sandbox = sandbox
        self.calls: list[tuple[str, dict[str, str]]] = []

    def launch(
        self,
        environment: str,
        *,
        tags: dict[str, str] | None = None,
    ) -> FakeSandbox:
        self.calls.append((environment, tags or {}))
        self.sandbox.tags.update(tags or {})
        self.sandbox.tags["envy.env"] = environment
        return self.sandbox


def make_envy(*names: str) -> Envy:
    envy = Envy("test-app")
    for name in names:
        envy.env(name, base=FakeImage())
    return envy


def tool_names(mcp: Any) -> set[str]:
    return {tool.name for tool in asyncio.run(mcp.list_tools())}


@unittest.skipIf(fastmcp is None, "MCP dependencies are not installed")
class MCPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.envies = patch.object(toolbox_module, "_envies", [])
        self.envies.start()

    def tearDown(self) -> None:
        self.envies.stop()

    def test_server_exposes_lifecycle_and_custom_tools(self) -> None:
        mcp = create_server(make_envy("api"))

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
            }.issubset(tool_names(mcp))
        )

    def test_envy_mcp_method_creates_the_server(self) -> None:
        mcp = make_envy("api").mcp()

        self.assertIn("create_sandbox", tool_names(mcp))

    def test_create_sandbox_uses_environment_and_app_tag(self) -> None:
        envy = make_envy("api", "worker")
        sandbox = FakeSandbox()
        runner = FakeRunner(sandbox)
        provider = EnvyProvider(envy, runner=runner)  # type: ignore[arg-type]

        result = provider._create_sandbox()("worker")

        self.assertEqual(result.sandbox_id, "sb-created")
        self.assertEqual(result.environment, "worker")
        self.assertEqual(result.workdir, "/tmp/worker")
        self.assertEqual(runner.calls, [("worker", {"envy.app": "test-app"})])

    def test_kill_rejects_sandbox_from_another_app(self) -> None:
        envy = make_envy("api")
        sandbox = FakeSandbox(tags={"envy.app": "other-app", "envy.env": "api"})

        class FakeModal:
            class Sandbox:
                @staticmethod
                def from_id(_sandbox_id: str) -> FakeSandbox:
                    return sandbox

        with patch.object(toolbox_module, "modal", FakeModal):
            provider = EnvyProvider(envy, runner=FakeRunner(FakeSandbox()))  # type: ignore[arg-type]
            kill = provider._kill_sandbox()
            with self.assertRaisesRegex(ValueError, "not created"):
                kill("sb-foreign")
        self.assertFalse(sandbox.terminated)

    def test_create_sandbox_schema_lists_environments(self) -> None:
        mcp = create_server(make_envy("api", "worker"))

        async def schema() -> dict[str, Any]:
            tool = await mcp.get_tool("create_sandbox")
            return tool.parameters["properties"]["environment"]

        self.assertEqual(asyncio.run(schema())["enum"], ["api", "worker"])
