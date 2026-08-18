from __future__ import annotations

import os
from typing import Annotated

import requests
from fastmcp.tools import tool
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from envy.mcp.github import normalize_repo
from envy.mcp.github_app import mint_installation_token
from envy.mcp.toolbox import (
    load_tool_description,
    require_absolute_path,
    resolve_sandbox,
    run_command,
)

DESCRIPTION = load_tool_description(__file__)


class PullRequestInfo(BaseModel):
    """The opened pull request's number and URL."""

    number: int
    url: str


@tool(
    name="open_pr",
    description=DESCRIPTION,
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, openWorldHint=True
    ),
)
def open_pr(
    sandbox_id: Annotated[
        str, Field(description="The sandbox_id whose checkout to push.")
    ],
    repo: Annotated[
        str, Field(description="The GitHub repo, as owner/name or a GitHub URL.")
    ],
    head: Annotated[str, Field(description="The branch to push and open the PR from.")],
    base: Annotated[str, Field(description="The branch to open the PR against.")],
    title: Annotated[str, Field(description="The pull request title.")],
    body: Annotated[str | None, Field(description="The pull request body.")] = None,
    workdir: Annotated[
        str | None,
        Field(description="Checkout path; defaults to the environment workdir."),
    ] = None,
) -> PullRequestInfo:
    normalized = normalize_repo(repo)
    owned = resolve_sandbox(sandbox_id)
    checkout = workdir or owned.environment.workdir
    if workdir:
        checkout = require_absolute_path(workdir, parameter="workdir")

    push = run_command(
        owned.sandbox, "git", "-C", checkout, "push", "origin", f"HEAD:{head}"
    )
    push.check()

    account, name = normalized.split("/", 1)
    installation_token = mint_installation_token(
        app_id=os.environ["GITHUB_APP_ID"],
        private_key=os.environ["GITHUB_APP_PRIVATE_KEY"],
        account=account,
        repo=name,
    )
    response = requests.post(
        f"https://api.github.com/repos/{normalized}/pulls",
        headers={
            "Authorization": f"Bearer {installation_token}",
            "Accept": "application/vnd.github+json",
        },
        json={"title": title, "head": head, "base": base, "body": body},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    return PullRequestInfo(number=payload["number"], url=payload["html_url"])
