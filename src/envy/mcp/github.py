"""Small GitHub helpers used by the ``open_pr`` MCP tool."""

from .errors import InvalidRepoError


def normalize_repo(repo: str) -> str:
    """Normalize a GitHub repo URL or slug to ``owner/name``."""
    text = repo.strip().removesuffix(".git")
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if text.startswith(prefix):
            text = text.removeprefix(prefix)
            break
    owner, _, name = text.strip("/").partition("/")
    if not owner or not name or "/" in name:
        raise InvalidRepoError(
            f"expected a GitHub repo like 'owner/name', got {repo!r}"
        )
    return f"{owner}/{name}"
