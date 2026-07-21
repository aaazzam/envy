from dataclasses import dataclass
from typing import Generic, Protocol

from .image import ImageT


class Stream(Protocol):
    def read(self) -> str: ...


class Process(Protocol):
    def wait(self) -> int: ...

    @property
    def stdout(self) -> Stream: ...


class Sandbox(Protocol):
    def exec(self, *command: str) -> Process: ...


@dataclass(frozen=True)
class Resources:
    cpu: float | None = None
    memory: int | None = None
    gpu: str | None = None


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
