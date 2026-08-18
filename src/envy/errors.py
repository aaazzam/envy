from collections.abc import Sequence


class EnvyError(Exception):
    """Base class for errors raised by Envy."""


class ConfigurationError(EnvyError, ValueError):
    """Raised when an environment declaration is invalid."""


class LifecycleError(EnvyError, RuntimeError):
    """Raised when an operation violates the environment lifecycle."""


class SourceError(EnvyError, RuntimeError):
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


class HookError(EnvyError, RuntimeError):
    """Raised when a lifecycle hook fails."""
