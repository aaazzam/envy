from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from re import fullmatch
from typing import TYPE_CHECKING, Generic, Protocol, cast, final

from .env import Env
from .errors import ConfigurationError, LifecycleError
from .image import ImageT, ImageTransform
from .sandbox import Resources, Sandbox, SandboxSpec
from .source import Source

if TYPE_CHECKING:
    import modal

_DEFAULT_RESOURCES = Resources()


class _AppApi(Protocol):
    def __call__(self, name: str) -> modal.App: ...

    def lookup(
        self,
        name: str,
        *,
        create_if_missing: bool,
        environment_name: str | None,
    ) -> modal.App: ...


class _SandboxApi(Protocol):
    def create(self, **kwargs: object) -> modal.Sandbox: ...

    def from_id(self, sandbox_id: str) -> modal.Sandbox: ...


class _RemoteFunction(Protocol):
    def remote(self, **kwargs: object) -> object: ...


class _FunctionApi(Protocol):
    def from_name(
        self,
        app_name: str,
        name: str,
        *,
        environment_name: str | None,
    ) -> _RemoteFunction: ...


class _BuildableImage(Protocol):
    def build(self, app: object) -> object: ...


class _ModalApi(Protocol):
    App: _AppApi
    Function: _FunctionApi
    Sandbox: _SandboxApi

    def enable_output(self) -> AbstractContextManager[None]: ...


class _SpecApi(Protocol):
    image: object
    workdir: str
    env: Mapping[str, str]
    secrets: tuple[object, ...]
    ports: tuple[int, ...]
    mounts: Mapping[str, object]
    resources: Resources
    metadata: Mapping[str, object]


class _EnvApi(Protocol):
    name: str | None

    def spec(self, *, stamp: str) -> _SpecApi: ...
    def start(self, sandbox: Sandbox) -> None: ...
    def freeze(self) -> None: ...


def _function_name(environment: str) -> str:
    return f"launch_{environment}"


def _cleanup_sandbox(
    sandbox: modal.Sandbox, error: BaseException | None = None
) -> None:
    cleanup_errors: list[Exception] = []
    try:
        _terminate_result: object = sandbox.terminate(wait=True)
    except Exception as cleanup_error:
        cleanup_errors.append(cleanup_error)
    try:
        sandbox.detach()
    except Exception as cleanup_error:
        cleanup_errors.append(cleanup_error)
    if error is not None:
        for cleanup_error in cleanup_errors:
            error.add_note(f"Modal Sandbox cleanup failed: {cleanup_error}")
        return
    if cleanup_errors:
        primary, *additional = cleanup_errors
        for cleanup_error in additional:
            primary.add_note(
                f"Additional Modal Sandbox cleanup failure: {cleanup_error}"
            )
        raise primary


@contextmanager
def _managed_sandbox(
    sandbox: modal.Sandbox,
) -> Generator[modal.Sandbox, None, None]:
    try:
        yield sandbox
    except BaseException as error:
        _cleanup_sandbox(sandbox, error)
        raise
    else:
        _cleanup_sandbox(sandbox)


def _detach_sandbox(
    sandbox: modal.Sandbox, error: BaseException | None = None
) -> None:
    try:
        sandbox.detach()
    except Exception as detach_error:
        if error is not None:
            error.add_note(f"Modal Sandbox detach failed: {detach_error}")
            return
        raise


@final
class ModalSession(Generic[ImageT], AbstractContextManager["ModalSession[ImageT]"]):
    """An environment declaration paired with an attached Modal Sandbox."""

    def __init__(self, environment: Env[ImageT], sandbox: modal.Sandbox) -> None:
        self.environment = environment
        self.sandbox = sandbox

    @property
    def sandbox_id(self) -> str:
        return self.sandbox.object_id

    def __enter__(self) -> ModalSession[ImageT]:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        _traceback: object | None,
    ) -> bool | None:
        _detach_sandbox(self.sandbox, exc_value)
        return False


@final
class Envy:
    """A collection of environments compiled into one deployable Modal App."""

    def __init__(
        self,
        name: str,
        *,
        stamp: str = "0",
        launch_timeout: int = 60 * 60,
        _modal: _ModalApi | None = None,
    ) -> None:
        if not name:
            raise ValueError("Envy requires a non-empty app name")
        self.name = name
        self.stamp = stamp
        self.launch_timeout = launch_timeout
        self._modal_module = _modal
        self._environments: dict[str, object] = {}
        self._app: modal.App | None = None

    @property
    def modal(self) -> _ModalApi:
        if self._modal_module is None:
            try:
                import modal as modal_module
            except ImportError as exc:
                raise RuntimeError(
                    "Envy deployment requires the Modal SDK; install envy[modal]"
                ) from exc
            module_object: object = modal_module
            self._modal_module = cast(  # pyright: ignore[reportInvalidCast]
                _ModalApi, module_object
            )
        return self._modal_module

    @property
    def environments(self) -> tuple[str, ...]:
        return tuple(self._environments)

    def env(
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
    ) -> Env[ImageT]:
        environment = Env(
            name,
            base=base,
            source=source,
            build=build,
            setup=setup,
            secrets=secrets,
            env=env,
            ports=ports,
            mounts=mounts,
            metadata=metadata,
            workdir=workdir,
            resources=resources,
        )
        return self.register(environment)

    def register(self, environment: Env[ImageT]) -> Env[ImageT]:
        if self._app is not None:
            raise LifecycleError(
                "cannot register an environment after envy.app is created"
            )
        name = str(environment.name)
        if not name:
            raise ConfigurationError("registered environments require a non-empty name")
        if not fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ConfigurationError(
                "environment names may contain only letters, numbers, '.', '_', and '-'"
            )
        if name in self._environments:
            raise ConfigurationError(f"environment {name!r} is already registered")
        self._environments[name] = environment
        return environment

    @property
    def app(self) -> modal.App:
        if self._app is None:
            if not self._environments:
                raise RuntimeError("cannot create envy.app without environments")
            app = self.modal.App(self.name)
            for environment_object in self._environments.values():
                environment = cast(_EnvApi, environment_object)
                environment.freeze()
                self._register_launcher(app, environment)
            self._app = app
        return self._app

    def _register_launcher(self, app: modal.App, environment: _EnvApi) -> None:
        spec = environment.spec(stamp=self.stamp)
        environment_name = str(environment.name)
        modal_api = self.modal
        image_object: object = spec.image
        modal_image = cast("modal.Image", image_object)

        @app.function(  # pyright: ignore[reportUnknownMemberType]
            image=modal_image,
            name=_function_name(environment_name),
            serialized=True,
            timeout=self.launch_timeout,
        )
        def _launch(
            *,
            name: str | None = None,
            tags: dict[str, str] | None = None,
            timeout: int = 300,
            idle_timeout: int | None = None,
        ) -> str:
            sandbox_tags = {key: str(value) for key, value in spec.metadata.items()}
            sandbox_tags["envy.env"] = environment_name
            sandbox_tags.update(tags or {})
            sandbox = modal_api.Sandbox.create(
                name=name,
                tags=sandbox_tags,
                image=spec.image,
                env=dict(spec.env) or None,
                secrets=spec.secrets or None,
                timeout=timeout,
                idle_timeout=idle_timeout,
                workdir=spec.workdir,
                gpu=spec.resources.gpu,
                cpu=spec.resources.cpu,
                memory=spec.resources.memory,
                volumes=dict(spec.mounts),
                encrypted_ports=spec.ports,
            )
            try:
                environment.start(sandbox)
            except BaseException as error:
                _cleanup_sandbox(sandbox, error)
                raise
            sandbox_id = sandbox.object_id
            try:
                sandbox.detach()
            except BaseException as error:
                _cleanup_sandbox(sandbox, error)
                raise
            return sandbox_id

        _ = _launch


@final
class ModalRunner:
    """Build and run Envy environments on Modal Sandboxes."""

    def __init__(
        self,
        app: str | modal.App = "envy",
        *,
        environment_name: str | None = None,
        show_output: bool = True,
        timeout: int = 300,
        idle_timeout: int | None = None,
        _modal: _ModalApi | None = None,
    ) -> None:
        self._app_ref = app
        self.environment_name = environment_name
        self.show_output = show_output
        self.timeout = timeout
        self.idle_timeout = idle_timeout
        self._modal_module = _modal
        self._resolved_app: modal.App | None = None

    @property
    def modal(self) -> _ModalApi:
        if self._modal_module is None:
            try:
                import modal as modal_module
            except ImportError as exc:
                raise RuntimeError(
                    "ModalRunner requires the Modal SDK; install envy[modal]"
                ) from exc
            module_object: object = modal_module
            self._modal_module = cast(  # pyright: ignore[reportInvalidCast]
                _ModalApi, module_object
            )
        return self._modal_module

    @property
    def app(self) -> modal.App:
        if self._resolved_app is None:
            if isinstance(self._app_ref, str):
                self._resolved_app = self.modal.App.lookup(
                    self._app_ref,
                    create_if_missing=True,
                    environment_name=self.environment_name,
                )
            else:
                self._resolved_app = self._app_ref
        return self._resolved_app

    def build(self, spec: SandboxSpec[ImageT]) -> ImageT:
        """Eagerly build and hydrate the image described by ``spec``."""
        output = self.modal.enable_output() if self.show_output else nullcontext()
        with output:
            image_object: object = spec.image
            image = cast(  # pyright: ignore[reportInvalidCast]
                _BuildableImage, image_object
            )
            return cast(ImageT, image.build(self.app))

    def launch(
        self,
        environment: str,
        *,
        name: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> modal.Sandbox:
        """Launch an environment registered in a deployed Envy App."""
        if not isinstance(self._app_ref, str):
            raise TypeError("deployed launches require a Modal App name")
        launcher = self.modal.Function.from_name(
            self._app_ref,
            _function_name(environment),
            environment_name=self.environment_name,
        )
        sandbox_id = launcher.remote(
            name=name,
            tags=tags,
            timeout=self.timeout,
            idle_timeout=self.idle_timeout,
        )
        if not isinstance(sandbox_id, str):
            raise TypeError("deployed Envy launcher returned an invalid Sandbox id")
        return self.modal.Sandbox.from_id(sandbox_id)

    def launch_spec(
        self,
        spec: SandboxSpec[ImageT],
        *,
        build: bool = True,
        name: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> modal.Sandbox:
        """Build ``spec`` and create its Modal Sandbox."""
        image = self.build(spec) if build else spec.image

        sandbox_tags = {key: str(value) for key, value in spec.metadata.items()}
        sandbox_tags.update(tags or {})

        return self.modal.Sandbox.create(
            app=self.app,
            name=name,
            tags=sandbox_tags or None,
            image=image,
            env=dict(spec.env) or None,
            secrets=spec.secrets or None,
            timeout=self.timeout,
            idle_timeout=self.idle_timeout,
            workdir=spec.workdir,
            gpu=spec.resources.gpu,
            cpu=spec.resources.cpu,
            memory=spec.resources.memory,
            volumes=dict(spec.mounts),
            encrypted_ports=spec.ports,
        )

    def managed_launch(
        self,
        environment: str,
        *,
        name: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> AbstractContextManager[modal.Sandbox]:
        """Launch a deployed environment and always terminate and detach it."""
        return _managed_sandbox(self.launch(environment, name=name, tags=tags))

    def session(
        self,
        env: Env[ImageT],
        *,
        sandbox_id: str | None = None,
        name: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> ModalSession[ImageT]:
        """Open a new or existing deployed sandbox for ``env``.

        When ``sandbox_id`` is omitted, the environment's deployed launcher
        creates a new sandbox. Otherwise, the existing Modal Sandbox is
        reopened by ID. Exiting the session detaches the local handle but does
        not terminate the remote sandbox. Refreshing remains explicit through
        ``env.refresh(session.sandbox)``.
        """
        if sandbox_id is None:
            if env.name is None:
                raise ValueError("sessions require a named environment")
            sandbox = self.launch(str(env.name), name=name, tags=tags)
        else:
            if name is not None or tags is not None:
                raise ValueError(
                    "name and tags cannot be used when reopening a sandbox"
                )
            sandbox = self.modal.Sandbox.from_id(sandbox_id)
        return ModalSession(env, sandbox)

    def run(
        self,
        env: Env[ImageT],
        *,
        stamp: str = "0",
        name: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> modal.Sandbox:
        """Compile, build, launch, and run the ready hooks for an environment."""
        spec = env.spec(stamp=stamp)
        env_tags = {"envy.env": str(env.name)}
        env_tags.update(tags or {})
        sandbox = self.launch_spec(spec, name=name, tags=env_tags)
        try:
            env.start(sandbox)
        except BaseException as error:
            _cleanup_sandbox(sandbox, error)
            raise
        return sandbox

    def managed_run(
        self,
        env: Env[ImageT],
        *,
        stamp: str = "0",
        name: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> AbstractContextManager[modal.Sandbox]:
        """Run an environment and always terminate and detach its sandbox."""
        return _managed_sandbox(self.run(env, stamp=stamp, name=name, tags=tags))
