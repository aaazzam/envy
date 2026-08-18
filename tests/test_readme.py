from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path
from typing import Any, cast

from envy import (
    Envy,
    GitSource,
    Layer,
    Resources,
    apt_install,
    pip_install,
    run_commands,
)

try:
    import fastmcp  # noqa: F401
    import modal
except ImportError:
    modal = None


README = Path(__file__).parents[1] / "README.md"


class RecordingImage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(self, name: str, *args: Any, **kwargs: Any) -> RecordingImage:
        self.calls.append((name, args, kwargs))
        return self

    def apt_install(self, *packages: str) -> RecordingImage:
        return self._record("apt_install", *packages)

    def pip_install(self, *packages: str) -> RecordingImage:
        return self._record("pip_install", *packages)

    def run_commands(self, *commands: str, **kwargs: Any) -> RecordingImage:
        return self._record("run_commands", *commands, **kwargs)

    def workdir(self, path: str) -> RecordingImage:
        return self._record("workdir", path)


class FakeStream:
    def read(self) -> str:
        return ""


class FakeProcess:
    returncode = 0
    stdout = FakeStream()
    stderr = FakeStream()

    def __init__(self) -> None:
        self.waited = False

    def wait(self) -> int:
        self.waited = True
        return self.returncode


class FakeSandbox:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.process = FakeProcess()

    def exec(self, *command: str) -> FakeProcess:
        self.commands.append(command)
        return self.process


class ReadmeDeclarationTests(unittest.TestCase):
    def test_documented_devbox_declaration_compiles_and_routes_hooks(self) -> None:
        image = RecordingImage()
        app = Envy("acme-devboxes", stamp="git-commit-or-release-id")
        api = app.env(
            "api",
            base=image,
            source=GitSource.github("acme/api", ref="main"),
            build=[apt_install("curl"), pip_install("uv")],
            setup=[run_commands("uv sync")],
            env={"ENV": "dev"},
            ports=[8000],
            resources=Resources(cpu=2, memory=4096),
        )

        @api.ready
        def boot(sandbox: FakeSandbox) -> FakeProcess:
            return sandbox.exec("uv", "run", "alembic", "upgrade", "head")

        @api.on_change("pyproject.toml", "uv.lock")
        def deps_changed(sandbox: FakeSandbox, _changes: object) -> None:
            sandbox.exec("uv", "sync")

        @api.on_change("migrations/*")
        def schema_changed(sandbox: FakeSandbox, _changes: object) -> None:
            sandbox.exec("uv", "run", "alembic", "upgrade", "head")

        spec = api.spec(stamp=app.stamp)
        self.assertEqual(spec.env, {"ENV": "dev"})
        self.assertEqual(spec.ports, (8000,))
        self.assertEqual(spec.resources, Resources(cpu=2, memory=4096))
        self.assertEqual(spec.workdir, "/tmp/api")
        self.assertEqual(
            [name for name, _args, _kwargs in image.calls],
            [
                "apt_install",
                "apt_install",
                "pip_install",
                "run_commands",
                "workdir",
                "run_commands",
                "workdir",
            ],
        )

        sandbox = FakeSandbox()
        api.start(sandbox)
        api.route_changes(
            sandbox,
            ["pyproject.toml", "uv.lock", "migrations/001.sql"],
        )
        self.assertTrue(sandbox.process.waited)
        self.assertEqual(
            sandbox.commands,
            [
                ("uv", "run", "alembic", "upgrade", "head"),
                ("uv", "sync"),
                ("uv", "run", "alembic", "upgrade", "head"),
            ],
        )

    def test_documented_layer_composes_its_source_and_build_step(self) -> None:
        image = RecordingImage()
        app = Envy("acme-devboxes")
        api = app.env(
            "api",
            base=image,
            source=GitSource.github("acme/api"),
        )
        docs = Layer(
            "docs",
            source=GitSource.github("acme/docs"),
            build=[pip_install("mkdocs")],
        )

        api.include(docs)
        spec = api.spec()

        self.assertEqual(tuple(layer.name for layer in api.layers), ("api", "docs"))
        self.assertEqual(spec.workdir, "/tmp/api")
        self.assertEqual(
            [name for name, _args, _kwargs in image.calls],
            [
                "apt_install",
                "pip_install",
                "run_commands",
                "run_commands",
                "workdir",
            ],
        )
        self.assertEqual(image.calls[0][1], ("git",))
        self.assertEqual(image.calls[1][1], ("mkdocs",))
        self.assertIn("acme/api", image.calls[2][1][1])
        self.assertIn("acme/docs", image.calls[3][1][1])


@unittest.skipIf(modal is None, "Modal and MCP extras are not installed")
class ReadmeExampleTests(unittest.TestCase):
    def test_documented_optional_install_commands_have_declared_extras(self) -> None:
        project = tomllib.loads(
            (README.parent / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        extras = project["optional-dependencies"]
        self.assertIn("modal", extras)
        self.assertIn("mcp", extras)
        self.assertTrue(
            any(dependency.startswith("modal") for dependency in extras["modal"])
        )
        self.assertTrue(
            any(dependency.startswith("fastmcp") for dependency in extras["mcp"])
        )

    def test_documented_python_examples_compile_and_register(self) -> None:
        blocks = re.findall(r"```python\n(.*?)```", README.read_text(), re.DOTALL)
        self.assertEqual(len(blocks), 5)

        namespace: dict[str, object] = {"__name__": "readme_examples"}
        declaration_api: Any | None = None
        for index in range(4):
            exec(compile(blocks[index], f"<README block {index}>", "exec"), namespace)
            if index == 0:
                declaration_api = namespace["api"]

        api = cast(Any, namespace["api"])
        runner = cast(Any, namespace["runner"])
        self.assertIsNotNone(declaration_api)
        self.assertIsInstance(
            cast(Any, declaration_api).spec(stamp="git-commit-or-release-id").image,
            modal.Image,
        )
        self.assertIsNot(namespace["control_plane_image"], api.spec().image)
        self.assertIs(runner.app, namespace["modal_app"])

    def test_documented_launch_example_has_persistent_session_semantics(self) -> None:
        self.assertIn("with runner.session(api) as session:", README.read_text())
        self.assertIn(
            "with runner.session(api, sandbox_id=session.sandbox_id) as session:",
            README.read_text(),
        )
        self.assertIn("api.refresh(session.sandbox)", README.read_text())

    def test_documented_step_and_source_names_are_public(self) -> None:
        import envy

        for name in (
            "apt_install",
            "pip_install",
            "run_commands",
            "setenv",
            "workdir",
            "dockerfile_commands",
            "add_local_file",
            "add_local_dir",
            "run_function",
        ):
            with self.subTest(name=name):
                self.assertIn(name, envy.__all__)
                self.assertIn(f"`{name}`", README.read_text())

        for name in ("GitSource", "LocalSource"):
            with self.subTest(name=name):
                self.assertIn(name, envy.__all__)
                self.assertIn(f"`{name}`", README.read_text())


if __name__ == "__main__":
    unittest.main()
