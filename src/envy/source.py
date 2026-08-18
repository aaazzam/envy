from dataclasses import dataclass, field
from hashlib import sha256
from json import JSONDecodeError, dumps, loads
from os import walk
from pathlib import Path, PurePosixPath
from re import fullmatch
from shlex import join as shell_join
from shlex import quote as shell_quote
from typing import Protocol, TypedDict, Unpack, cast

from .errors import (
    ConfigurationError,
    SourceCommandError,
    SourceError,
    SourceRefreshUnsupported,
)
from .image import ImageT
from .sandbox import Process, Sandbox

_CHUNK_SIZE = 1024 * 1024
_Manifest = dict[str, tuple[str, int]]


class _RemoteFilesystem(Protocol):
    def copy_from_local(self, local_path: str | Path, remote_path: str) -> None: ...
    def read_text(self, remote_path: str) -> str: ...
    def remove(self, remote_path: str, *, recursive: bool = False) -> None: ...
    def write_text(self, data: str, remote_path: str) -> None: ...


class _FilesystemSandbox(Sandbox, Protocol):
    @property
    def filesystem(self) -> _RemoteFilesystem: ...


class Source(Protocol[ImageT]):
    @property
    def workdir(self) -> str: ...

    @property
    def secrets(self) -> tuple[object, ...]: ...

    def fetch(self, image: ImageT, *, stamp: str) -> ImageT: ...

    def pull(self, sandbox: Sandbox) -> tuple[PurePosixPath, ...]: ...


class _GitSourceOptions(TypedDict, total=False):
    workdir: str
    ref: str
    depth: int
    tags: bool
    submodules: bool
    lfs: bool
    secrets: tuple[object, ...]
    token_env: str
    token_user: str


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

    @property
    def requires_git(self) -> bool:
        """Whether the environment image needs Git before this source is fetched."""
        return True

    @classmethod
    def github(cls, repo: str, **kwargs: Unpack[_GitSourceOptions]) -> "GitSource":
        if not fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ConfigurationError("GitHub repositories must use 'owner/name' form")
        kwargs.setdefault("token_env", "GITHUB_TOKEN")
        kwargs.setdefault("token_user", "x-access-token")
        return cls(url=f"https://github.com/{repo}.git", **kwargs)

    def __post_init__(self) -> None:
        if not self.url:
            raise ConfigurationError("GitSource requires a non-empty URL")
        if not self.ref:
            raise ConfigurationError("GitSource requires a non-empty ref")
        if self.depth < 0:
            raise ConfigurationError("GitSource depth cannot be negative")
        if not fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.token_env):
            raise ConfigurationError(
                "GitSource token_env must be a valid environment variable name"
            )
        if not self.workdir:
            short_name = self.url.rstrip("/").split("/")[-1].removesuffix(".git")
            if not short_name:
                raise ConfigurationError(
                    "GitSource could not derive a workdir from URL"
                )
            object.__setattr__(self, "workdir", f"/tmp/{short_name}")
        if not PurePosixPath(self.workdir).is_absolute():
            raise ConfigurationError("GitSource workdir must be an absolute POSIX path")

    @property
    def credential_helper(self) -> str:
        username = shell_quote(f"username={self.token_user}")
        helper = (
            f"!f() {{ printf '%s\\n' {username}; "
            f"printf '%s\\n' \"password=${self.token_env}\"; }}; f"
        )
        return shell_join(["git", "config", "--global", "credential.helper", helper])

    def fetch(self, image: ImageT, *, stamp: str) -> ImageT:
        clone = ["git", "clone", "--branch", self.ref]
        if self.depth:
            clone += ["--depth", str(self.depth)]
        if not self.tags:
            clone += ["--no-tags"]
        if self.submodules:
            clone += ["--recurse-submodules"]
        clone += ["--", self.url, self.workdir]
        commands = [shell_join(["echo", f"envy source stamp: {stamp}"])]
        if self.secrets:
            commands.append(self.credential_helper)
        commands.append(shell_join(clone))
        if self.lfs:
            commands.append(shell_join(["git", "-C", self.workdir, "lfs", "pull"]))
        if self.secrets:
            return image.run_commands(*commands, secrets=self.secrets)
        return image.run_commands(*commands)

    def pull(self, sandbox: Sandbox) -> tuple[PurePosixPath, ...]:
        before = self._run(sandbox, "git", "-C", self.workdir, "rev-parse", "HEAD")
        baseline = before.stdout.read().strip()
        if not baseline:
            raise SourceError(
                f"git rev-parse HEAD returned an empty revision in {self.workdir}"
            )
        self._run(sandbox, "git", "-C", self.workdir, "pull", "--ff-only")
        diff = self._run(
            sandbox,
            "git",
            "-C",
            self.workdir,
            "diff",
            "--name-only",
            f"{baseline}..HEAD",
        )
        return tuple(
            PurePosixPath(line)
            for line in diff.stdout.read().splitlines()
            if line.strip()
        )

    def _run(self, sandbox: Sandbox, *command: str) -> Process:
        process = sandbox.exec(*command)
        returncode = process.wait()
        if returncode != 0:
            raise SourceCommandError(
                command,
                workdir=self.workdir,
                returncode=returncode,
                stderr=process.stderr.read(),
            )
        return process


@dataclass(frozen=True)
class LocalSource:
    path: str
    workdir: str = ""
    ignore: tuple[str, ...] = field(default=())
    secrets: tuple[object, ...] = ()

    @property
    def requires_git(self) -> bool:
        return False

    def __post_init__(self) -> None:
        if not self.path:
            raise ConfigurationError("LocalSource requires a non-empty local path")
        if not self.workdir:
            object.__setattr__(self, "workdir", f"/tmp/{Path(self.path).name}")
        if not PurePosixPath(self.workdir).is_absolute():
            raise ConfigurationError(
                "LocalSource workdir must be an absolute POSIX path"
            )
        if any(
            not pattern or PurePosixPath(pattern).is_absolute()
            for pattern in self.ignore
        ):
            raise ConfigurationError(
                "LocalSource ignore patterns must be non-empty relative globs"
            )

    @property
    def _manifest_path(self) -> str:
        source_id = sha256(self.workdir.encode()).hexdigest()[:16]
        return f"/tmp/.envy/manifests/{source_id}.json"

    def fetch(self, image: ImageT, *, stamp: str) -> ImageT:
        del stamp
        manifest = self._local_manifest()
        image = image.add_local_dir(
            self.path,
            self.workdir,
            copy=True,
            ignore=self.ignore,
        )
        manifest_json = self._encode_manifest(manifest)
        manifest_parent = str(PurePosixPath(self._manifest_path).parent)
        script = 'mkdir -p "$1" && printf "%s" "$2" > "$3"'
        command = shell_join(
            [
                "sh",
                "-c",
                script,
                "envy-local-source",
                manifest_parent,
                manifest_json,
                self._manifest_path,
            ]
        )
        return image.run_commands(command)

    def pull(self, sandbox: Sandbox) -> tuple[PurePosixPath, ...]:
        filesystem = cast(_FilesystemSandbox, cast(object, sandbox))
        previous = self._read_manifest(filesystem)
        current = self._local_manifest()
        updated = sorted(
            path for path, state in current.items() if previous.get(path) != state
        )
        deleted = sorted(path for path in previous if path not in current)

        for relative_path in updated:
            self._upload(filesystem, relative_path, current[relative_path][1])
        for relative_path in deleted:
            self._remove(filesystem, relative_path)

        if updated or deleted:
            self._write_manifest(filesystem, current)
        return tuple(PurePosixPath(path) for path in sorted((*updated, *deleted)))

    def _local_manifest(self) -> _Manifest:
        root = Path(self.path).expanduser().resolve()
        if not root.is_dir():
            raise ConfigurationError(
                f"LocalSource path must be an existing directory: {self.path!r}"
            )
        manifest: _Manifest = {}
        for directory, directory_names, file_names in walk(root):
            local_directory = Path(directory)
            kept_directories: list[str] = []
            for name in sorted(directory_names):
                local_path = local_directory / name
                relative = PurePosixPath(local_path.relative_to(root).as_posix())
                if self._is_ignored(relative):
                    continue
                if local_path.is_symlink():
                    raise ConfigurationError(
                        f"LocalSource refresh does not support symlinks: {relative}"
                    )
                kept_directories.append(name)
            directory_names[:] = kept_directories

            for name in sorted(file_names):
                local_path = local_directory / name
                relative = PurePosixPath(local_path.relative_to(root).as_posix())
                if self._is_ignored(relative):
                    continue
                if local_path.is_symlink():
                    raise ConfigurationError(
                        f"LocalSource refresh does not support symlinks: {relative}"
                    )
                digest = sha256()
                with local_path.open("rb") as local_file:
                    while chunk := local_file.read(_CHUNK_SIZE):
                        digest.update(chunk)
                mode = local_path.stat().st_mode & 0o777
                manifest[str(relative)] = (digest.hexdigest(), mode)
        return manifest

    def _is_ignored(self, relative: PurePosixPath) -> bool:
        candidates = (
            relative,
            *(parent for parent in relative.parents if parent.parts),
        )
        return any(
            candidate.match(pattern.rstrip("/"))
            for pattern in self.ignore
            for candidate in candidates
        )

    @staticmethod
    def _encode_manifest(manifest: _Manifest) -> str:
        return dumps(manifest, sort_keys=True, separators=(",", ":"))

    def _read_manifest(self, sandbox: _FilesystemSandbox) -> _Manifest:
        try:
            raw_manifest = sandbox.filesystem.read_text(self._manifest_path)
        except Exception as exc:
            raise SourceRefreshUnsupported(
                "LocalSource cannot refresh this sandbox because its build manifest "
                "is missing; rebuild the environment with the current Envy version"
            ) from exc
        try:
            decoded_object: object = loads(raw_manifest)
            if not isinstance(decoded_object, dict):
                raise TypeError("manifest root is not an object")
            decoded = cast(dict[object, object], decoded_object)
            manifest: _Manifest = {}
            for path, state_object in decoded.items():
                if not isinstance(path, str) or not isinstance(state_object, list):
                    raise TypeError("manifest entry has invalid types")
                state = cast(list[object], state_object)
                if len(state) != 2:
                    raise TypeError("manifest entry has invalid length")
                digest, mode = state
                if not isinstance(digest, str) or not isinstance(mode, int):
                    raise TypeError("manifest state has invalid types")
                manifest[path] = (digest, mode)
            return manifest
        except (JSONDecodeError, TypeError, ValueError, IndexError) as exc:
            raise SourceError("LocalSource manifest is malformed") from exc

    def _upload(
        self, sandbox: _FilesystemSandbox, relative_path: str, mode: int
    ) -> None:
        local_path = Path(self.path) / relative_path
        remote_path = str(PurePosixPath(self.workdir) / relative_path)
        sandbox.filesystem.copy_from_local(local_path, remote_path)
        process = sandbox.exec("chmod", f"{mode:o}", remote_path)
        returncode = process.wait()
        if returncode != 0:
            raise SourceCommandError(
                ("chmod", f"{mode:o}", remote_path),
                workdir=self.workdir,
                returncode=returncode,
                stderr=process.stderr.read(),
            )

    def _remove(self, sandbox: _FilesystemSandbox, relative_path: str) -> None:
        remote_path = str(PurePosixPath(self.workdir) / relative_path)
        try:
            sandbox.filesystem.remove(remote_path)
        except Exception as exc:
            if isinstance(exc, FileNotFoundError) or type(exc).__name__.endswith(
                "NotFoundError"
            ):
                return
            raise

    def _write_manifest(self, sandbox: _FilesystemSandbox, manifest: _Manifest) -> None:
        sandbox.filesystem.write_text(
            self._encode_manifest(manifest), self._manifest_path
        )
