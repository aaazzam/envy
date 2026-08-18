from collections.abc import Sequence


class BoxenError(Exception):
    """Base class for errors raised by Boxen."""


class ConfigurationError(BoxenError, ValueError):
    """Raised when an environment declaration is invalid."""


class LifecycleError(BoxenError, RuntimeError):
    """Raised when an operation violates the environment lifecycle."""


class SourceError(BoxenError, RuntimeError):
    """Base class for source acquisition and refresh failures."""


class SourceRefreshUnsupported(SourceError, NotImplementedError):
    """Raised when a source cannot refresh an existing sandbox."""


class SourceCommandError(SourceError):
    """Raised when a source command fails in a sandbox."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        workdir: str,
        returncode: int,
        stderr: str = "",
    ) -> None:
        self.command = tuple(command)
        self.workdir = workdir
        self.returncode = returncode
        self.stderr = stderr.strip()
        rendered = " ".join(repr(part) for part in self.command)
        message = (
            f"source command failed with exit code {returncode} in {workdir}: "
            f"{rendered}"
        )
        if self.stderr:
            message = f"{message}\n{self.stderr}"
        super().__init__(message)


class HookError(BoxenError, RuntimeError):
    """Raised when a lifecycle hook fails."""
