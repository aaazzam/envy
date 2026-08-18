from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Generic, cast
from warnings import warn

from .errors import ConfigurationError, HookError, LifecycleError
from .image import ImageT, ImageTransform
from .sandbox import Process, Resources, Sandbox, SandboxSpec
from .source import Source

StartHook = Callable[[Sandbox], None]
ReadyHook = Callable[[Sandbox], Process]
_DEFAULT_RESOURCES = Resources()


@dataclass(frozen=True)
class Changes:
    matched: tuple[PurePosixPath, ...]
    all: tuple[PurePosixPath, ...]


SyncHandler = Callable[[Sandbox, Changes], None]


@dataclass(frozen=True)
class SyncRule:
    patterns: tuple[str, ...]
    handler: SyncHandler

    def matched(
        self, changed_files: Iterable[PurePosixPath]
    ) -> tuple[PurePosixPath, ...]:
        """Return paths matching pathlib-style POSIX glob patterns."""
        return tuple(
            path
            for path in changed_files
            if any(path.match(pattern) for pattern in self.patterns)
        )


@dataclass(frozen=True)
class _LifecycleHook:
    handler: Callable[[Sandbox], object]
    wait: bool


class Layer(Generic[ImageT]):
    def __init__(
        self,
        name: str | None = None,
        *,
        source: Source[ImageT] | None = None,
        build: Sequence[ImageTransform[ImageT]] = (),
        setup: Sequence[ImageTransform[ImageT]] = (),
        secrets: Sequence[object] = (),
        env: Mapping[str, str] | None = None,
        ports: Sequence[int] = (),
        mounts: Mapping[str, object] | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        if name == "":
            raise ConfigurationError("layer name cannot be empty")
        self._validate_env(env or {})
        self._validate_ports(ports)
        self._validate_mounts(mounts or {})
        self._name = name
        self._source = source
        self._build_steps = tuple(build)
        self._setup_steps = tuple(setup)
        self._secrets = tuple(secrets)
        self._env = MappingProxyType(dict(env or {}))
        self._ports = tuple(ports)
        self._mounts = MappingProxyType(dict(mounts or {}))
        self._metadata = MappingProxyType(dict(metadata or {}))
        self._sync_rules: list[SyncRule] = []
        self._lifecycle_hooks: list[_LifecycleHook] = []
        self._frozen = False

    @staticmethod
    def _validate_env(env: Mapping[str, str]) -> None:
        if any(Layer._invalid_env_item(key, value) for key, value in env.items()):
            raise ConfigurationError(
                "environment variables must map strings to strings"
            )

    @staticmethod
    def _invalid_env_item(key: object, value: object) -> bool:
        return not isinstance(key, str) or not isinstance(value, str)

    @staticmethod
    def _validate_ports(ports: Sequence[int]) -> None:
        invalid = [
            port for port in ports if isinstance(port, bool) or not 1 <= port <= 65535
        ]
        if invalid:
            raise ConfigurationError(
                f"ports must be integers from 1 to 65535: {invalid!r}"
            )
        if len(set(ports)) != len(ports):
            raise ConfigurationError("ports cannot contain duplicates")

    @staticmethod
    def _validate_mounts(mounts: Mapping[str, object]) -> None:
        invalid = [path for path in mounts if not PurePosixPath(path).is_absolute()]
        if invalid:
            raise ConfigurationError(
                f"mount paths must be absolute POSIX paths: {invalid!r}"
            )

    @property
    def name(self) -> str | None:
        return self._name

    @property
    def source(self) -> Source[ImageT] | None:
        return self._source

    @property
    def build_steps(self) -> tuple[ImageTransform[ImageT], ...]:
        return self._build_steps

    @property
    def setup_steps(self) -> tuple[ImageTransform[ImageT], ...]:
        return self._setup_steps

    @property
    def secrets(self) -> tuple[object, ...]:
        return self._secrets

    @property
    def env(self) -> Mapping[str, str]:
        return self._env

    @property
    def ports(self) -> tuple[int, ...]:
        return self._ports

    @property
    def mounts(self) -> Mapping[str, object]:
        return self._mounts

    @property
    def metadata(self) -> Mapping[str, object]:
        return self._metadata

    @property
    def sync_rules(self) -> tuple[SyncRule, ...]:
        return tuple(self._sync_rules)

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    @property
    def workdir(self) -> str | None:
        return self.source.workdir if self.source else None

    def _ensure_mutable(self) -> None:
        if self._frozen:
            label = f" {self.name!r}" if self.name is not None else ""
            raise LifecycleError(f"cannot modify frozen layer{label}")

    def freeze(self) -> None:
        """Prevent further registration of hooks or rules on this layer."""
        self._frozen = True

    def on_change(self, *patterns: str) -> Callable[[SyncHandler], SyncHandler]:
        self._ensure_mutable()
        if not patterns or any(not pattern for pattern in patterns):
            raise ConfigurationError(
                "on_change requires non-empty globs (use '*' for any direct change)"
            )
        if any(PurePosixPath(pattern).is_absolute() for pattern in patterns):
            raise ConfigurationError("on_change patterns must be relative POSIX globs")

        def register(fn: SyncHandler) -> SyncHandler:
            self._ensure_mutable()
            self._sync_rules.append(SyncRule(tuple(patterns), fn))
            return fn

        return register

    def on_start(self, fn: StartHook) -> StartHook:
        """Run a callback after creation without awaiting returned processes."""
        self._ensure_mutable()
        self._lifecycle_hooks.append(
            _LifecycleHook(cast(Callable[[Sandbox], object], fn), False)
        )
        return fn

    def ready(self, fn: ReadyHook) -> ReadyHook:
        """Run a hook and wait for its returned process to succeed."""
        self._ensure_mutable()
        self._lifecycle_hooks.append(
            _LifecycleHook(cast(Callable[[Sandbox], object], fn), True)
        )
        return fn

    def _start(self, sandbox: Sandbox) -> None:
        for hook in self._lifecycle_hooks:
            result = hook.handler(sandbox)
            if not hook.wait:
                continue
            if result is None or not callable(getattr(result, "wait", None)):
                raise HookError("ready hooks must return a sandbox process")
            process = cast(Process, result)
            returncode = process.wait()
            if returncode != 0:
                stderr = process.stderr.read().strip()
                detail = f": {stderr}" if stderr else ""
                raise HookError(
                    f"ready hook process exited with code {returncode}{detail}"
                )


class Env(Layer[ImageT]):
    def __init__(
        self,
        name: str,
        *,
        base: ImageT,
        source: Source[ImageT] | None = None,
        build: Sequence[ImageTransform[ImageT]] = (),
        setup: Sequence[ImageTransform[ImageT]] = (),
        secrets: Sequence[object] = (),
        env: Mapping[str, str] | None = None,
        ports: Sequence[int] = (),
        mounts: Mapping[str, object] | None = None,
        metadata: Mapping[str, object] | None = None,
        workdir: str | None = None,
        resources: Resources = _DEFAULT_RESOURCES,
    ) -> None:
        if not name:
            raise ConfigurationError("environment name cannot be empty")
        if workdir is not None and not PurePosixPath(workdir).is_absolute():
            raise ConfigurationError(
                "environment workdir must be an absolute POSIX path"
            )
        super().__init__(
            name,
            source=source,
            build=build,
            setup=setup,
            secrets=secrets,
            env=env,
            ports=ports,
            mounts=mounts,
            metadata=metadata,
        )
        self._base = base
        self._workdir = workdir
        self._resources = resources
        self._layers: list[Layer[ImageT]] = [self]

    @property
    def resources(self) -> Resources:
        return self._resources

    @property
    def workdir(self) -> str:
        if self._workdir:
            return self._workdir
        if self.source:
            return self.source.workdir
        return f"/tmp/{self.name}"

    @property
    def layers(self) -> tuple[Layer[ImageT], ...]:
        return tuple(self._layers)

    def freeze(self) -> None:
        """Freeze this environment and all included layers."""
        for layer in self._layers:
            if layer is not self:
                layer.freeze()
        super().freeze()

    def include(self, layer: Layer[ImageT]) -> None:
        self._ensure_mutable()
        if layer in self._layers:
            raise ConfigurationError("cannot include the same layer more than once")
        if layer.name is not None and any(
            existing.name == layer.name for existing in self._layers
        ):
            raise ConfigurationError(f"layer name {layer.name!r} is already included")
        if layer.source is not None:
            taken = {
                existing.source.workdir
                for existing in self._layers
                if existing.source is not None
            }
            if layer.source.workdir in taken:
                raise ConfigurationError(
                    f"workdir {layer.source.workdir!r} already claimed in this env"
                )
        self._layers.append(layer)

    def image(self, *, stamp: str = "0") -> ImageT:
        image = self._base
        if any(
            bool(getattr(layer.source, "requires_git", False))
            for layer in self._layers
            if layer.source is not None
        ):
            image = image.apt_install("git")
        for layer in self._layers:
            for step in layer.build_steps:
                image = step(image)
        for layer in self._layers:
            if layer.source is not None:
                image = layer.source.fetch(image, stamp=stamp)
        for layer in self._layers:
            if not layer.setup_steps:
                continue
            image = image.workdir(layer.workdir or self.workdir)
            for step in layer.setup_steps:
                image = step(image)
        return image.workdir(self.workdir)

    def spec(self, *, stamp: str = "0") -> SandboxSpec[ImageT]:
        env_vars: dict[str, str] = {}
        mounts: dict[str, object] = {}
        metadata: dict[str, object] = {}
        secrets: list[object] = []
        for layer in self._layers[1:]:
            env_vars.update(layer.env)
            mounts.update(layer.mounts)
            metadata.update(layer.metadata)
        env_vars.update(self.env)
        mounts.update(self.mounts)
        metadata.update(self.metadata)
        for layer in self._layers:
            secrets.extend(layer.secrets)
            if layer.source is not None:
                secrets.extend(layer.source.secrets)
        ports = tuple(
            dict.fromkeys(port for layer in self._layers for port in layer.ports)
        )
        return SandboxSpec(
            image=self.image(stamp=stamp),
            workdir=self.workdir,
            env=env_vars,
            secrets=tuple(secrets),
            ports=ports,
            mounts=mounts,
            resources=self.resources,
            metadata=metadata,
        )

    def start(self, sandbox: Sandbox) -> None:
        for layer in self._layers:
            layer._start(sandbox)

    def refresh(self, sandbox: Sandbox) -> None:
        drift: dict[int, tuple[PurePosixPath, ...]] = {}
        for layer in self._layers:
            source = layer.source or self.source
            if source is None:
                continue
            key = id(source)
            if key not in drift:
                drift[key] = source.pull(sandbox)
            self._route(layer, sandbox, drift[key])

    def route_changes(
        self, sandbox: Sandbox, changed_files: Iterable[str | PurePosixPath]
    ) -> None:
        """Route paths that another mechanism has already synchronized."""
        changed = tuple(PurePosixPath(path) for path in changed_files)
        invalid = [path for path in changed if path.is_absolute() or ".." in path.parts]
        if invalid:
            raise ConfigurationError(
                f"changed files must be relative paths within a source: {invalid!r}"
            )
        for layer in self._layers:
            self._route(layer, sandbox, changed)

    def sync(
        self, sandbox: Sandbox, changed_files: Iterable[str | PurePosixPath]
    ) -> None:
        warn(
            "Env.sync() only routes changes and is deprecated; use route_changes()",
            DeprecationWarning,
            stacklevel=2,
        )
        self.route_changes(sandbox, changed_files)

    def _route(
        self,
        layer: Layer[ImageT],
        sandbox: Sandbox,
        changed: tuple[PurePosixPath, ...],
    ) -> None:
        if not changed:
            return
        for rule in layer.sync_rules:
            matched = rule.matched(changed)
            if matched:
                rule.handler(sandbox, Changes(matched=matched, all=changed))
