"""Errors raised by Envy's optional MCP integration."""


class MCPError(Exception):
    """Base class for deliberate MCP integration errors."""


class NotOurSandboxError(MCPError, ValueError):
    """A sandbox id did not come from the Envy app served by this MCP server."""


class NotAFileError(MCPError, IsADirectoryError):
    """A file tool was given a directory path."""


class SandboxCommandError(MCPError, RuntimeError):
    """A command run inside a sandbox exited unsuccessfully."""


class InvalidRepoError(MCPError, ValueError):
    """A GitHub repo reference could not be normalized to ``owner/name``."""


class GitHubAppNotInstalledError(MCPError, LookupError):
    """The configured GitHub App is not installed on the requested account."""
