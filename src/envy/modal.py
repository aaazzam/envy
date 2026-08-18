from __future__ import annotations

import os
from collections.abc import Generator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from datetime import UTC, datetime
from hmac import compare_digest
from re import fullmatch
from typing import TYPE_CHECKING, Annotated, Any, Generic, Protocol, cast, final

from .env import Env
from .errors import ConfigurationError, LifecycleError
from .image import ImageT, ImageTransform
from .sandbox import Resources, Sandbox, SandboxSpec
from .source import Source

if TYPE_CHECKING:
    import modal

_DEFAULT_RESOURCES = Resources()


class _AppApi(Protocol):
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


class _BuildableImage(Protocol):
    object_id: str

    def build(self, app: object) -> object: ...


class _ModalApi(Protocol):
    App: _AppApi
    Sandbox: _SandboxApi
    Image: Any
    Dict: Any

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


def _registry_name(app_name: str) -> str:
    """Return a stable, Modal-compatible name for the image registry Dict."""
    normalized = "".join(
        character.lower() if character.isalnum() else "-" for character in app_name
    ).strip("-")
    return f"envy-{normalized or 'app'}-images"


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


def _detach_sandbox(sandbox: modal.Sandbox, error: BaseException | None = None) -> None:
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
    """A named declaration containing a collection of environments."""

    def __init__(
        self,
        name: str,
        *,
        stamp: str = "0",
    ) -> None:
        if not name:
            raise ValueError("Envy requires a non-empty app name")
        self.name = name
        self.stamp = stamp
        self._environments: dict[str, object] = {}
        self._frozen = False

    @property
    def environments(self) -> tuple[str, ...]:
        return tuple(self._environments)

    def environment(self, name: str) -> Env[Any]:
        """Return a registered environment by name.

        This read-only lookup is useful to integrations that need to associate
        a sandbox with its declared environment without reaching into Envy's
        private registry.
        """
        try:
            environment = self._environments[name]
        except KeyError as exc:
            raise KeyError(f"environment {name!r} is not registered") from exc
        return cast(Env[Any], environment)

    def mcp(self, **settings: Any) -> Any:
        """Create an MCP server exposing this app's environments.

        MCP support is optional. The import stays lazy so the core Envy package
        remains usable without FastMCP; call ``app.mcp()`` only after
        installing the ``envy[mcp]`` extra.
        """
        try:
            from .mcp import create_server
        except ModuleNotFoundError as exc:
            if exc.name not in {"fastmcp", "mcp", "modal"}:
                raise
            raise RuntimeError(
                "MCP support is optional; install it with `uv add 'envy[mcp]'`"
            ) from exc
        return create_server(self, **settings)

    def freeze(self) -> None:
        """Freeze this declaration and all registered environments."""
        if not self._environments:
            raise RuntimeError("cannot freeze an Envy declaration without environments")
        if self._frozen:
            return
        for environment_object in self._environments.values():
            cast(_EnvApi, environment_object).freeze()
        self._frozen = True

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
        if self._frozen:
            raise LifecycleError(
                "cannot register an environment after declaration freeze"
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


@final
class ModalRunner:
    """Build and run an Envy declaration on Modal Sandboxes."""

    def __init__(
        self,
        envy: Envy,
        *,
        environment_name: str | None = None,
        modal_app: object | None = None,
        show_output: bool = True,
        timeout: int = 300,
        idle_timeout: int | None = None,
        _modal: _ModalApi | None = None,
    ) -> None:
        self.envy = envy
        self.environment_name = environment_name
        self.show_output = show_output
        self.timeout = timeout
        self.idle_timeout = idle_timeout
        self._modal_module = _modal
        self._resolved_app: modal.App | None = cast("modal.App | None", modal_app)
        if modal_app is not None:
            self.install_rebake_schedule(modal_app)

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
            self._resolved_app = self.modal.App.lookup(
                self.envy.name,
                create_if_missing=True,
                environment_name=self.environment_name,
            )
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

    def _image_registry(self) -> Any | None:
        """Return the persistent image-id registry when supported."""
        dict_type = getattr(self.modal, "Dict", None)
        if dict_type is None:
            return None
        return dict_type.from_name(
            _registry_name(self.envy.name),
            create_if_missing=True,
            environment_name=self.environment_name,
        )

    def _stored_image_id(self, environment: str) -> str | None:
        registry = self._image_registry()
        if registry is None:
            return None
        value: Any = registry.get(environment)
        if isinstance(value, dict):
            image_id = cast(dict[str, object], value).get("image_id")
        else:
            image_id = value
        return image_id if isinstance(image_id, str) and image_id else None

    def _remember_image(self, environment: str, image: object, stamp: str) -> str:
        image_id = getattr(image, "object_id", None)
        registry = self._image_registry()
        if registry is None:
            return image_id if isinstance(image_id, str) else ""
        if not isinstance(image_id, str) or not image_id:
            raise RuntimeError("Modal returned a built image without an object id")
        registry[environment] = {
            "image_id": image_id,
            "stamp": stamp,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        return image_id

    def rebake(
        self,
        environment: str | None = None,
        *,
        stamp: str | None = None,
    ) -> dict[str, str]:
        """Eagerly build and record the latest image for one or all environments.

        A timestamp stamp is used by default so Git sources are re-cloned from
        their configured ref even when the declaration's normal stamp is static.
        """
        self.envy.freeze()
        names = (environment,) if environment is not None else self.envy.environments
        for name in names:
            self.envy.environment(name)
        resolved_stamp = stamp or datetime.now(UTC).isoformat()
        image_ids: dict[str, str] = {}
        for name in names:
            env = self.envy.environment(name)
            built = self.build(env.spec(stamp=resolved_stamp))
            image_ids[name] = self._remember_image(name, built, resolved_stamp)
        return image_ids

    def install_rebake_schedule(
        self,
        modal_app: Any,
        *,
        cron: str = "*/30 * * * *",
        timezone: str = "UTC",
        name: str = "envy-rebake",
        timeout: int = 60 * 60,
    ) -> Any:
        """Register a Modal cron function that rebakes all environments."""
        import modal

        raw_schedules = getattr(modal_app, "_envy_rebake_schedules", None)
        if isinstance(raw_schedules, dict):
            schedules = cast(dict[str, Any], raw_schedules)
        else:
            schedules: dict[str, Any] = {}
            modal_app._envy_rebake_schedules = schedules
        existing = schedules.get(name)
        if existing is not None:
            return existing

        runner = self

        @modal_app.function(
            schedule=modal.Cron(cron, timezone=timezone),
            name=name,
            timeout=timeout,
            serialized=True,
        )
        def rebake() -> dict[str, str]:
            return runner.rebake()

        schedules[name] = rebake
        return rebake

    def install_rebake_endpoint(
        self,
        modal_app: Any,
        *,
        token_secret: str,
        token_env: str = "ENVY_REBAKE_TOKEN",
        name: str = "envy-rebake-endpoint",
        label: str | None = None,
        timeout: int = 60 * 60,
        requires_proxy_auth: bool = True,
    ) -> Any:
        """Register an authenticated POST endpoint for manual rebakes.

        ``token_secret`` must contain ``token_env``. The endpoint accepts a
        bearer token and optionally also requires Modal proxy authentication.
        An optional JSON ``environment`` field limits the rebake to one
        declared environment.
        """
        import modal
        from fastapi import Depends, HTTPException
        from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

        if not fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
            raise ConfigurationError(
                "token_env must be a valid environment variable name"
            )

        runner = self
        bearer = HTTPBearer(auto_error=True)
        fastapi_endpoint = cast(Any, modal.fastapi_endpoint)

        @modal_app.function(
            name=name,
            secrets=[modal.Secret.from_name(token_secret, required_keys=[token_env])],
            timeout=timeout,
            serialized=True,
        )
        @fastapi_endpoint(
            method="POST",
            label=label,
            requires_proxy_auth=requires_proxy_auth,
        )
        def rebake_endpoint(
            credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
            data: dict[str, str] | None = None,
        ) -> dict[str, str]:
            expected = os.environ.get(token_env)
            if expected is None or not compare_digest(
                credentials.credentials, expected
            ):
                raise HTTPException(status_code=401, detail="Invalid bearer token")
            environment = data.get("environment") if data else None
            return runner.rebake(environment=environment)

        return rebake_endpoint

    def launch(
        self,
        environment: str,
        *,
        name: str | None = None,
        tags: dict[str, str] | None = None,
        experimental_options: Mapping[str, Any] | None = None,
    ) -> modal.Sandbox:
        """Build and launch a named environment from this Envy declaration."""
        env = self.envy.environment(environment)
        return self.run(
            env,
            stamp=self.envy.stamp,
            name=name,
            tags=tags,
            experimental_options=experimental_options,
        )

    def launch_spec(
        self,
        spec: SandboxSpec[ImageT],
        *,
        build: bool = True,
        image_key: str | None = None,
        name: str | None = None,
        tags: dict[str, str] | None = None,
        image_override: object | None = None,
        secrets_override: Sequence[object] | None = None,
        experimental_options: Mapping[str, Any] | None = None,
    ) -> modal.Sandbox:
        """Build ``spec`` and create its Modal Sandbox."""
        image: object
        if image_override is not None:
            image = image_override
        else:
            stored_image_id = self._stored_image_id(image_key) if image_key else None
            if stored_image_id is not None:
                image = self.modal.Image.from_id(stored_image_id)
            else:
                image = self.build(spec) if build else spec.image

        sandbox_tags = {key: str(value) for key, value in spec.metadata.items()}
        sandbox_tags.update(tags or {})

        create_kwargs: dict[str, object] = {
            "app": self.app,
            "name": name,
            "tags": sandbox_tags or None,
            "image": image,
            "env": dict(spec.env) or None,
            "secrets": (
                tuple(spec.secrets)
                if secrets_override is None
                else tuple(secrets_override)
            )
            or None,
            "timeout": self.timeout,
            "idle_timeout": self.idle_timeout,
            "workdir": spec.workdir,
            "gpu": spec.resources.gpu,
            "cpu": spec.resources.cpu,
            "memory": spec.resources.memory,
            "volumes": dict(spec.mounts),
            "encrypted_ports": spec.ports,
        }
        if experimental_options is not None:
            create_kwargs["experimental_options"] = dict(experimental_options)
        return self.modal.Sandbox.create(**create_kwargs)

    def launch_from_snapshot(
        self,
        environment: str,
        snapshot: object,
        *,
        name: str | None = None,
        tags: dict[str, str] | None = None,
        secrets: Sequence[object] | None = None,
    ) -> modal.Sandbox:
        """Launch a new sandbox from a prior sandbox exit snapshot.

        The snapshot already contains the prepared filesystem, so source
        refresh and start hooks are intentionally not run again. Secrets are
        supplied separately, which lets a privileged publisher receive a Git
        secret without adding it to the agent's canonical sandbox.
        """
        self.envy.freeze()
        env = self.envy.environment(environment)
        spec = env.spec(stamp=self.envy.stamp)
        env_tags = {"envy.env": str(env.name)}
        env_tags.update(tags or {})
        return self.launch_spec(
            spec,
            build=False,
            image_key=None,
            name=name,
            tags=env_tags,
            image_override=snapshot,
            secrets_override=secrets,
            experimental_options={"enable_exit_snapshot": True},
        )

    def managed_launch(
        self,
        environment: str,
        *,
        name: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> AbstractContextManager[modal.Sandbox]:
        """Launch an environment and always terminate and detach its sandbox."""
        return _managed_sandbox(self.launch(environment, name=name, tags=tags))

    def session(
        self,
        env: Env[ImageT],
        *,
        sandbox_id: str | None = None,
        name: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> ModalSession[ImageT]:
        """Open a new or existing sandbox for ``env``.

        When ``sandbox_id`` is omitted, this runner builds and creates a new
        sandbox for the named environment. Otherwise, the existing Modal
        Sandbox is reopened by ID. Exiting the session detaches the local
        handle but does not terminate the remote sandbox. Refreshing remains
        explicit through ``env.refresh(session.sandbox)``.
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
        stamp: str | None = None,
        name: str | None = None,
        tags: dict[str, str] | None = None,
        experimental_options: Mapping[str, Any] | None = None,
    ) -> modal.Sandbox:
        """Compile, build, launch, and run the ready hooks for an environment."""
        self.envy.freeze()
        resolved_stamp = self.envy.stamp if stamp is None else stamp
        spec = env.spec(stamp=resolved_stamp)
        env_tags = {"envy.env": str(env.name)}
        env_tags.update(tags or {})
        sandbox = self.launch_spec(
            spec,
            name=name,
            image_key=str(env.name),
            tags=env_tags,
            experimental_options=experimental_options,
        )
        try:
            env.refresh(sandbox)
            env.start(sandbox)
        except BaseException as error:
            _cleanup_sandbox(sandbox, error)
            raise
        return sandbox

    def managed_run(
        self,
        env: Env[ImageT],
        *,
        stamp: str | None = None,
        name: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> AbstractContextManager[modal.Sandbox]:
        """Run an environment and always terminate and detach its sandbox."""
        return _managed_sandbox(self.run(env, stamp=stamp, name=name, tags=tags))
