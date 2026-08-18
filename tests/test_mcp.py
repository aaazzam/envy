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
    from envy.mcp import server as server_module
    from envy.mcp import toolbox as toolbox_module
    from envy.mcp.toolbox import CommandResult
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


class PublisherFile:
    def __init__(self, sandbox: PublisherSandbox) -> None:
        self.sandbox = sandbox

    def __enter__(self) -> PublisherFile:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def write(self, content: str) -> None:
        self.sandbox.files[self.sandbox.opened_path] = content


class PublisherSandbox(FakeSandbox):
    def __init__(self, object_id: str) -> None:
        super().__init__(object_id)
        self.files: dict[str, str] = {}
        self.opened_path = ""

    def _experimental_get_exit_snapshot(self, *, timeout: float) -> str:
        self.snapshot_timeout = timeout
        return "exit-snapshot"

    def open(self, path: str, _mode: str) -> PublisherFile:
        self.opened_path = path
        return PublisherFile(self)


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


class SnapshotRunner(FakeRunner):
    def launch(
        self,
        environment: str,
        *,
        tags: dict[str, str] | None = None,
        experimental_options: dict[str, object] | None = None,
    ) -> FakeSandbox:
        self.experimental_options = experimental_options
        return super().launch(environment, tags=tags)

    def launch_from_snapshot(
        self,
        environment: str,
        _snapshot: object,
        *,
        tags: dict[str, str] | None = None,
        secrets: tuple[object, ...] | None = None,
    ) -> FakeSandbox:
        self.snapshot_call = (environment, tags or {}, secrets)
        return self.sandbox


class PublisherRunner(SnapshotRunner):
    def __init__(
        self,
        sandbox: PublisherSandbox,
        canonical: PublisherSandbox,
        publisher: PublisherSandbox,
    ) -> None:
        super().__init__(sandbox)
        self.canonical = canonical
        self.publisher = publisher
        self.snapshot_calls: list[tuple[str, object, dict[str, str], object]] = []

    def launch_from_snapshot(
        self,
        environment: str,
        snapshot: object,
        *,
        tags: dict[str, str] | None = None,
        secrets: tuple[object, ...] | None = None,
    ) -> PublisherSandbox:
        self.snapshot_calls.append((environment, snapshot, tags or {}, secrets))
        return self.canonical if len(self.snapshot_calls) == 1 else self.publisher


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
                "publish_pull_request",
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

    def test_modal_runner_path_returns_stable_workspace_handle(self) -> None:
        envy = make_envy("api")
        sandbox = FakeSandbox()
        runner = SnapshotRunner(sandbox)
        store = toolbox_module.InMemoryWorkspaceStore()
        provider = EnvyProvider(
            envy,
            runner=runner,  # type: ignore[arg-type]
            workspace_store=store,
        )

        result = provider._create_sandbox()("api")

        self.assertTrue(result.workspace_id.startswith("ws-"))
        self.assertEqual(result.sandbox_id, result.workspace_id)
        self.assertEqual(runner.experimental_options, {"enable_exit_snapshot": True})
        self.assertEqual(store.get(result.workspace_id)["physical_id"], "sb-created")

    def test_publish_uses_a_hidden_secret_injected_sandbox(self) -> None:
        envy = make_envy("api")
        agent = PublisherSandbox("sb-agent")
        canonical = PublisherSandbox("sb-canonical")
        publisher = PublisherSandbox("sb-publisher")
        runner = PublisherRunner(agent, canonical, publisher)
        store = toolbox_module.InMemoryWorkspaceStore()
        provider = EnvyProvider(
            envy,
            runner=runner,  # type: ignore[arg-type]
            workspace_store=store,
            git_secret="git-secret",
        )
        workspace_id = provider._create_sandbox()("api").sandbox_id

        def command(
            _sandbox: PublisherSandbox,
            *args: str,
            **_kwargs: object,
        ) -> CommandResult:
            if args[:2] == ("git", "branch"):
                return CommandResult("feature\n", "", 0, args)
            if args[:2] == ("git", "rev-parse"):
                return CommandResult("abc123\n", "", 0, args)
            if args[:3] == ("git", "remote", "get-url"):
                return CommandResult(
                    "https://github.com/acme/project.git\n", "", 0, args
                )
            return CommandResult("", "", 0, args)

        with (
            patch.object(server_module, "run_command", side_effect=command),
            patch.object(
                server_module,
                "create_pull_request",
                return_value={"html_url": "https://github.com/acme/project/pull/1"},
            ),
            patch.dict(server_module.os.environ, {"GITHUB_TOKEN": "server-token"}),
        ):
            result = provider._publish_pull_request()(
                workspace_id, "Add feature", body="Details"
            )

        self.assertEqual(
            result.pull_request_url, "https://github.com/acme/project/pull/1"
        )
        self.assertEqual(store.get(workspace_id)["physical_id"], "sb-canonical")
        self.assertEqual(runner.snapshot_calls[0][3], None)
        self.assertEqual(runner.snapshot_calls[1][3], ("git-secret",))
        self.assertNotIn("git-secret", publisher.files["/tmp/envy-git-askpass"])
        self.assertIn("GITHUB_TOKEN", publisher.files["/tmp/envy-git-askpass"])

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
