from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .image import ImageT


@dataclass(frozen=True)
class Apt:
    packages: tuple[str, ...]

    def __call__(self, image: ImageT) -> ImageT:
        return image.apt_install(*self.packages)


def apt(*packages: str) -> Apt:
    return Apt(packages)


@dataclass(frozen=True)
class Pip:
    packages: tuple[str, ...]

    def __call__(self, image: ImageT) -> ImageT:
        return image.pip_install(*self.packages)


def pip(*packages: str) -> Pip:
    return Pip(packages)


@dataclass(frozen=True)
class Run:
    commands: tuple[str, ...]
    secrets: tuple[object, ...] = ()

    def __call__(self, image: ImageT) -> ImageT:
        return image.run_commands(*self.commands, secrets=self.secrets)


def run(*commands: str, secrets: Sequence[object] = ()) -> Run:
    return Run(commands, tuple(secrets))


@dataclass(frozen=True)
class SetEnv:
    vars: tuple[tuple[str, str], ...]

    def __call__(self, image: ImageT) -> ImageT:
        return image.env(dict(self.vars))


def setenv(vars: Mapping[str, str]) -> SetEnv:
    return SetEnv(tuple(vars.items()))


@dataclass(frozen=True)
class Workdir:
    path: str

    def __call__(self, image: ImageT) -> ImageT:
        return image.workdir(self.path)


def workdir(path: str) -> Workdir:
    return Workdir(path)


@dataclass(frozen=True)
class Dockerfile:
    commands: tuple[str, ...]

    def __call__(self, image: ImageT) -> ImageT:
        return image.dockerfile_commands(*self.commands)


def dockerfile(*commands: str) -> Dockerfile:
    return Dockerfile(commands)


@dataclass(frozen=True)
class LocalFile:
    local_path: str
    remote_path: str

    def __call__(self, image: ImageT) -> ImageT:
        return image.add_local_file(self.local_path, self.remote_path)


def local_file(local_path: str | Path, remote_path: str) -> LocalFile:
    return LocalFile(str(local_path), remote_path)


@dataclass(frozen=True)
class LocalDir:
    local_path: str
    remote_path: str

    def __call__(self, image: ImageT) -> ImageT:
        return image.add_local_dir(self.local_path, self.remote_path)


def local_dir(local_path: str | Path, remote_path: str) -> LocalDir:
    return LocalDir(str(local_path), remote_path)


@dataclass(frozen=True)
class Call:
    fn: Callable[..., object]
    args: tuple[object, ...] = ()
    kwargs: tuple[tuple[str, object], ...] = ()
    secrets: tuple[object, ...] = ()
    mounts: tuple[tuple[str, object], ...] = ()
    always: bool = False

    def __call__(self, image: ImageT) -> ImageT:
        return image.run_function(
            self.fn,
            args=self.args,
            kwargs=dict(self.kwargs),
            secrets=self.secrets,
            volumes=dict(self.mounts),
            force_build=self.always,
        )


def call(
    fn: Callable[..., object],
    *,
    args: Sequence[object] = (),
    kwargs: Mapping[str, object] | None = None,
    secrets: Sequence[object] = (),
    mounts: Mapping[str, object] | None = None,
    always: bool = False,
) -> Call:
    return Call(
        fn,
        tuple(args),
        tuple((kwargs or {}).items()),
        tuple(secrets),
        tuple((mounts or {}).items()),
        always,
    )
