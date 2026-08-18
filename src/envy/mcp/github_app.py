"""GitHub App token minting for the ``open_pr`` MCP tool."""

from __future__ import annotations

import time

import jwt
import requests

from .errors import GitHubAppNotInstalledError

_JWT_CLOCK_SKEW_SECONDS = 60
_JWT_TTL_SECONDS = 540


def _app_jwt(app_id: str, private_key: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iat": now - _JWT_CLOCK_SKEW_SECONDS,
            "exp": now + _JWT_TTL_SECONDS,
            "iss": app_id,
        },
        private_key,
        algorithm="RS256",
    )


def mint_installation_token(
    app_id: str, private_key: str, account: str, repo: str | None = None
) -> str:
    """Mint a short-lived installation token for one account/repository."""
    headers = {
        "Authorization": f"Bearer {_app_jwt(app_id, private_key)}",
        "Accept": "application/vnd.github+json",
    }
    response = requests.get(
        "https://api.github.com/app/installations", headers=headers, timeout=30
    )
    response.raise_for_status()
    installation = next(
        (item for item in response.json() if item["account"]["login"] == account), None
    )
    if installation is None:
        raise GitHubAppNotInstalledError(
            f"GitHub App is not installed on account {account!r}"
        )
    token_response = requests.post(
        f"https://api.github.com/app/installations/{installation['id']}/access_tokens",
        headers=headers,
        json={"repositories": [repo]} if repo else None,
        timeout=30,
    )
    token_response.raise_for_status()
    return token_response.json()["token"]
