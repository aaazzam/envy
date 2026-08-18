"""GitHub MCP provider helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.providers import ProxyProvider
from fastmcp.server.transforms import Namespace

_GITHUB_MCP_HEADERS = {
    "X-MCP-Toolsets": "pull_requests,repos",
}


@dataclass(frozen=True, slots=True)
class GitHubRepository:
    owner: str
    name: str


@dataclass(frozen=True, slots=True)
class PublishedBranch:
    """The branch and repository produced by a privileged workspace push."""

    repository: GitHubRepository
    branch: str
    commit: str


def parse_github_remote(remote: str) -> GitHubRepository:
    """Parse an HTTPS or SSH GitHub remote without retaining credentials."""
    value = remote.strip()
    if value.startswith("git@github.com:"):
        path = value.removeprefix("git@github.com:")
    else:
        parsed = urlparse(value)
        if parsed.hostname != "github.com":
            raise ValueError("the origin remote must point to github.com")
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git").strip("/")
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"could not parse GitHub repository from remote {remote!r}")
    return GitHubRepository(owner=parts[0], name=parts[1])


def github_provider(
    url: str,
    *,
    token_env: str = "GITHUB_TOKEN",
    namespace: str | None = "github",
) -> ProxyProvider:
    """Create a provider backed by GitHub's official MCP server.

    The returned object is a provider on the composed Envy server. Its
    transport is proxied because GitHub's official server is a separate MCP
    process/service; callers still see one combined Envy tool catalog.
    """

    def client_factory() -> Client[Any]:
        import os

        token = os.environ.get(token_env)
        transport = StreamableHttpTransport(
            url,
            headers=dict(_GITHUB_MCP_HEADERS),
            auth=BearerAuth(token) if token else None,
        )
        return Client(transport)

    provider = ProxyProvider(client_factory)
    if namespace:
        provider.add_transform(Namespace(namespace))
    return provider
