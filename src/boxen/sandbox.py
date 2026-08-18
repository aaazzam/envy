from dataclasses import dataclass
from typing import Generic, Protocol

from .errors import ConfigurationError
from .image import ImageT


class Stream(Protocol):
    def read(self) -> str: ...


class Process(Protocol):
    def wait(self) -> int: ...

    @property
    def stdout(self) -> Stream: ...

    @property
    def stderr(self) -> Stream: ...


class Sandbox(Protocol):
    def exec(self, *command: str) -> Process: ...


@dataclass(frozen=True)
class Resources:
    cpu: float | None = None
    memory: int | None = None
    gpu: str | None = None

    def __post_init__(self) -> None:
        if self.cpu is not None and self.cpu <= 0:
            raise ConfigurationError("cpu must be greater than zero")
        if self.memory is not None and self.memory <= 0:
            raise ConfigurationError("memory must be greater than zero")
        if self.gpu == "":
            raise ConfigurationError("gpu cannot be empty")


@dataclass(frozen=True)
class SandboxSpec(Generic[ImageT]):
    image: ImageT
    workdir: str
    env: dict[str, str]
    secrets: tuple[object, ...]
    ports: tuple[int, ...]
    mounts: dict[str, object]
    resources: Resources
    metadata: dict[str, object]
