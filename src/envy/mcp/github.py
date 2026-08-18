"""Small GitHub REST helpers for the privileged MCP publisher."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class GitHubRepository:
    owner: str
    name: str


class _Response(Protocol):
    def read(self) -> bytes: ...


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


def create_pull_request(
    repository: GitHubRepository,
    *,
    token: str,
    head: str,
    base: str,
    title: str,
    body: str,
    draft: bool,
    api_url: str = "https://api.github.com",
    opener: Callable[..., _Response] = urlopen,
) -> dict[str, object]:
    """Create a GitHub pull request and return its JSON response."""
    if not token:
        raise ValueError("the GitHub token is empty")
    endpoint = f"{api_url.rstrip('/')}/repos/{repository.owner}/{repository.name}/pulls"
    request = Request(
        endpoint,
        data=json.dumps(
            {
                "title": title,
                "head": head,
                "base": base,
                "body": body,
                "draft": draft,
            }
        ).encode("utf-8"),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "envy-mcp",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    try:
        response = opener(request, timeout=30)
        raw: bytes = response.read()
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub rejected pull request creation: {detail}"
        ) from error
    except URLError as error:
        raise RuntimeError(f"could not reach GitHub: {error.reason}") from error
    try:
        decoded: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "GitHub returned an invalid pull request response"
        ) from error
    if not isinstance(decoded, dict):
        raise RuntimeError("GitHub returned an unexpected pull request response")
    payload = cast(dict[object, object], decoded)
    return {str(key): value for key, value in payload.items()}
