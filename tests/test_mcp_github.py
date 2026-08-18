import asyncio
import unittest
from unittest.mock import patch

from fastmcp import FastMCP

from envy.mcp.github import GitHubRepository, github_provider, parse_github_remote
from envy.mcp.github_transform import WorkspacePublishTransform


class GitHubTests(unittest.TestCase):
    def test_parse_https_and_ssh_remotes(self) -> None:
        self.assertEqual(
            parse_github_remote("https://github.com/acme/project.git"),
            GitHubRepository(owner="acme", name="project"),
        )
        self.assertEqual(
            parse_github_remote("git@github.com:acme/project.git"),
            GitHubRepository(owner="acme", name="project"),
        )

    def test_parse_rejects_non_github_and_malformed_remotes(self) -> None:
        with self.assertRaisesRegex(ValueError, "github.com"):
            parse_github_remote("https://gitlab.com/acme/project.git")
        with self.assertRaisesRegex(ValueError, "could not parse"):
            parse_github_remote("https://github.com/acme")

    def test_provider_can_namespace_github_tools(self) -> None:
        provider = github_provider(
            "https://api.githubcopilot.com/mcp/",
            namespace="github",
        )

        self.assertEqual(len(provider.transforms), 1)
        with patch.dict("os.environ", {"GITHUB_TOKEN": "token"}, clear=True):
            client = provider.client_factory()
        self.assertEqual(
            client.transport.headers,
            {"X-MCP-Toolsets": "pull_requests,repos"},
        )

    def test_provider_can_leave_github_tool_names_unprefixed(self) -> None:
        provider = github_provider("https://api.githubcopilot.com/mcp/", namespace=None)

        self.assertEqual(provider.transforms, [])

    def test_pull_request_tool_publishes_workspace_from_request_metadata(self) -> None:
        async def exercise() -> tuple[str, list[str]]:
            backend = FastMCP("github")

            @backend.tool(name="create_pull_request")
            def create_pull_request(owner: str, title: str) -> str:
                return f"{owner}: {title}"

            parent = await backend.get_tool("create_pull_request")
            calls: list[str] = []
            transform = WorkspacePublishTransform(calls.append)

            async def get_tool(_name: str, *, version=None):
                return parent

            with patch(
                "envy.mcp.github_transform._sandbox_id_from_request",
                return_value="ws-123",
            ):
                wrapped = await transform.get_tool(
                    "create_pull_request", get_tool, version=None
                )
                assert wrapped is not None
                result = await wrapped.run({"owner": "acme", "title": "Improve"})

            return result.structured_content["result"], calls

        result, calls = asyncio.run(exercise())
        self.assertEqual(result, "acme: Improve")
        self.assertEqual(calls, ["ws-123"])

    def test_pull_request_tool_forwards_without_workspace_metadata(self) -> None:
        async def exercise() -> tuple[str, list[str]]:
            backend = FastMCP("github")

            @backend.tool(name="update_pull_request")
            def update_pull_request(title: str) -> str:
                return title

            parent = await backend.get_tool("update_pull_request")
            calls: list[str] = []
            transform = WorkspacePublishTransform(calls.append)

            async def get_tool(_name: str, *, version=None):
                return parent

            with patch(
                "envy.mcp.github_transform._sandbox_id_from_request",
                return_value=None,
            ):
                wrapped = await transform.get_tool(
                    "update_pull_request", get_tool, version=None
                )
                assert wrapped is not None
                result = await wrapped.run({"title": "Already pushed"})

            return result.structured_content["result"], calls

        result, calls = asyncio.run(exercise())
        self.assertEqual(result, "Already pushed")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
