from pathlib import Path
from typing import Callable, Mapping, Protocol, Self, Sequence, TypeAlias, TypeVar

class Image(Protocol):
    """Structural surface that modal.Image already satisfies."""

    def pip_install(self, *packages: str) -> Self: ...
    def apt_install(self, *packages: str) -> Self: ...
    def run_commands(
        self, *commands: str, secrets: Sequence[object] = ()
    ) -> Self: ...
    def run_function(
        self,
        raw_f: Callable[..., object],
        *,
        args: Sequence[object] = (),
        kwargs: Mapping[str, object] = {},
        secrets: Sequence[object] = (),
        volumes: Mapping[str, object] = {},
        force_build: bool = False,
    ) -> Self: ...
    def add_local_file(self, local_path: str | Path, remote_path: str) -> Self: ...
    def add_local_dir(self, local_path: str | Path, remote_path: str) -> Self: ...
    def env(self, vars: dict[str, str]) -> Self: ...
    def workdir(self, path: str) -> Self: ...
    def dockerfile_commands(self, *commands: str) -> Self: ...

ImageT = TypeVar("ImageT", bound=Image)

ImageTransform: TypeAlias = Callable[[ImageT], ImageT]

def apply_transform(image: ImageT, transform: ImageTransform[ImageT]) -> ImageT:
    return transform(image)
