"""Errors raised by Envy's optional MCP integration."""


class MCPError(Exception):
    """Base class for deliberate MCP integration errors."""


class NotOurSandboxError(MCPError, ValueError):
    """A sandbox id did not come from the Envy app served by this MCP server."""


class NotAFileError(MCPError, IsADirectoryError):
    """A file tool was given a directory path."""


class SandboxCommandError(MCPError, RuntimeError):
    """A command run inside a sandbox exited unsuccessfully."""
