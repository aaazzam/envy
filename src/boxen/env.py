from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Callable, Generic, Iterable, Mapping, Sequence

from .image import ImageT, ImageTransform
from .sandbox import Resources, Sandbox, SandboxSpec
from .source import Source

SandboxHook = Callable[[Sandbox], None]


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
        return tuple(
            path
            for path in changed_files
            if any(fnmatch(str(path), pattern) for pattern in self.patterns)
        )


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
        self.name = name
        self.source = source
        self.build_steps = list(build)
        self.setup_steps = list(setup)
        self.secrets: tuple[object, ...] = tuple(secrets)
        self.env: dict[str, str] = dict(env or {})
        self.ports: tuple[int, ...] = tuple(ports)
        self.mounts: dict[str, object] = dict(mounts or {})
        self.metadata: dict[str, object] = dict(metadata or {})
        self.sync_rules: list[SyncRule] = []
        self.ready_hooks: list[SandboxHook] = []

    @property
    def workdir(self) -> str | None:
        return self.source.workdir if self.source else None

    def on_change(self, *patterns: str) -> Callable[[SyncHandler], SyncHandler]:
        if not patterns:
            raise ValueError("on_change requires at least one glob (use '*' for any change)")

        def register(fn: SyncHandler) -> SyncHandler:
            self.sync_rules.append(SyncRule(patterns, fn))
            return fn

        return register

    def ready(self, fn: SandboxHook) -> SandboxHook:
        self.ready_hooks.append(fn)
        return fn


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
        resources: Resources = Resources(),
    ) -> None:
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
        self.resources = resources
        self._layers: list[Layer[ImageT]] = [self]

    @property
    def workdir(self) -> str:
        if self._workdir:
            return self._workdir
        if self.source:
            return self.source.workdir
        return f"/tmp/{self.name}"

    def include(self, layer: Layer[ImageT]) -> None:
        if layer.source is not None:
            taken = {l.source.workdir for l in self._layers if l.source is not None}
            if layer.source.workdir in taken:
                raise ValueError(f"workdir {layer.source.workdir!r} already claimed in this env")
        self._layers.append(layer)

    def image(self, *, stamp: str = "0") -> ImageT:
        image = self._base
        for layer in self._layers:
            for step in layer.build_steps:
                image = step(image)
        for layer in self._layers:
            if layer.source is not None:
                image = layer.source.fetch(image, stamp=stamp)
        image = image.workdir(self.workdir)
        for layer in self._layers:
            for step in layer.setup_steps:
                image = step(image)
        return image

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
        return SandboxSpec(
            image=self.image(stamp=stamp),
            workdir=self.workdir,
            env=env_vars,
            secrets=tuple(secrets),
            ports=tuple(p for layer in self._layers for p in layer.ports),
            mounts=mounts,
            resources=self.resources,
            metadata=metadata,
        )

    def start(self, sandbox: Sandbox) -> None:
        for layer in self._layers:
            for hook in layer.ready_hooks:
                hook(sandbox)

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

    def sync(
        self, sandbox: Sandbox, changed_files: Iterable[str | PurePosixPath]
    ) -> None:
        changed = tuple(PurePosixPath(path) for path in changed_files)
        for layer in self._layers:
            self._route(layer, sandbox, changed)

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
