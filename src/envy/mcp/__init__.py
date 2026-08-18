"""Optional FastMCP integration for controlling Envy environments.

Install the optional dependency group before importing this package::

    pip install 'envy[mcp]'
"""

from .server import EnvyProvider, PullRequestInfo, SandboxInfo, create_server

__all__ = ["EnvyProvider", "PullRequestInfo", "SandboxInfo", "create_server"]
