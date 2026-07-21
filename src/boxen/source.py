from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol

from .image import ImageT
from .sandbox import Sandbox


class Source(Protocol[ImageT]):
    @property
    def workdir(self) -> str: ...

    @property
    def secrets(self) -> tuple[object, ...]: ...

    def fetch(self, image: ImageT, *, stamp: str) -> ImageT: ...

    def pull(self, sandbox: Sandbox) -> tuple[PurePosixPath, ...]: ...


@dataclass(frozen=True)
class GitSource:
    url: str
    workdir: str = ""
    ref: str = "main"
    depth: int = 0
    tags: bool = True
    submodules: bool = True
    lfs: bool = False
    secrets: tuple[object, ...] = ()
    token_env: str = "GIT_TOKEN"
    token_user: str = "token"

    @classmethod
    def github(cls, repo: str, **kwargs: object) -> "GitSource":
        kwargs.setdefault("token_env", "GITHUB_TOKEN")
        kwargs.setdefault("token_user", "x-access-token")
        return cls(url=f"https://github.com/{repo}.git", **kwargs)

    def __post_init__(self) -> None:
        if not self.workdir:
            short_name = self.url.rstrip("/").split("/")[-1].removesuffix(".git")
            object.__setattr__(self, "workdir", f"/tmp/{short_name}")

    @property
    def credential_helper(self) -> str:
        return (
            "git config --global credential.helper "
            f"'!f() {{ echo username={self.token_user}; echo password=${self.token_env}; }}; f'"
        )

    def fetch(self, image: ImageT, *, stamp: str) -> ImageT:
        clone = ["git", "clone", "--branch", self.ref]
        if self.depth:
            clone += ["--depth", str(self.depth)]
        if not self.tags:
            clone += ["--no-tags"]
        if self.submodules:
            clone += ["--recurse-submodules"]
        clone += [self.url, self.workdir]
        commands = [f"echo 'boxen source stamp: {stamp}'"]
        if self.secrets:
            commands.append(self.credential_helper)
        commands.append(" ".join(clone))
        if self.lfs:
            commands.append(f"git -C {self.workdir} lfs pull")
        if self.secrets:
            return image.run_commands(*commands, secrets=self.secrets)
        return image.run_commands(*commands)

    def pull(self, sandbox: Sandbox) -> tuple[PurePosixPath, ...]:
        before = sandbox.exec("git", "-C", self.workdir, "rev-parse", "HEAD")
        if before.wait() != 0:
            raise RuntimeError(f"git rev-parse HEAD failed in {self.workdir}")
        baseline = before.stdout.read().strip()
        pulled = sandbox.exec("git", "-C", self.workdir, "pull", "--ff-only")
        if pulled.wait() != 0:
            raise RuntimeError(f"git pull failed in {self.workdir}")
        diff = sandbox.exec(
            "git", "-C", self.workdir, "diff", "--name-only", f"{baseline}..HEAD"
        )
        if diff.wait() != 0:
            raise RuntimeError(f"git diff failed in {self.workdir}")
        return tuple(
            PurePosixPath(line)
            for line in diff.stdout.read().splitlines()
            if line.strip()
        )


@dataclass(frozen=True)
class LocalSource:
    path: str
    workdir: str = ""
    ignore: tuple[str, ...] = field(default=())
    secrets: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        if not self.workdir:
            object.__setattr__(self, "workdir", f"/tmp/{Path(self.path).name}")

    def fetch(self, image: ImageT, *, stamp: str) -> ImageT:
        return image.add_local_dir(self.path, self.workdir)

    def pull(self, sandbox: Sandbox) -> tuple[PurePosixPath, ...]:
        return ()
