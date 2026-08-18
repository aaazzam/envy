from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol, Self, TypeAlias, TypeVar


class Image(Protocol):
    """Backend image operations used by Boxen transforms."""

    def pip_install(self, *packages: str) -> Self: ...
    def apt_install(self, *packages: str) -> Self: ...
    def run_commands(self, *commands: str, secrets: Any = None) -> Self: ...
    def run_function(
        self,
        raw_f: Callable[..., object],
        *,
        args: Sequence[object] = (),
        kwargs: Any = None,
        secrets: Any = None,
        volumes: Any = None,
        force_build: bool = False,
    ) -> Self: ...
    def add_local_file(
        self, local_path: str | Path, remote_path: str, *, copy: bool = False
    ) -> Self: ...
    def add_local_dir(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        copy: bool = False,
        ignore: Sequence[str] = (),
    ) -> Self: ...
    def env(self, vars: dict[str, str]) -> Self: ...
    def workdir(self, path: str) -> Self: ...
    def dockerfile_commands(self, *commands: str) -> Self: ...


ImageT = TypeVar("ImageT", bound=Image)

ImageTransform: TypeAlias = Callable[[ImageT], ImageT]


def apply_transform(image: ImageT, transform: ImageTransform[ImageT]) -> ImageT:
    return transform(image)
