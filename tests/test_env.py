import unittest
from pathlib import PurePosixPath

from envy.env import Env, Layer, SyncRule
from envy.errors import ConfigurationError, HookError, LifecycleError
from envy.sandbox import Resources
from envy.steps import run_commands


class FakeImage:
    def __init__(self):
        self.calls = []

    def workdir(self, path):
        self.calls.append(("workdir", path))
        return self

    def run_commands(self, *commands, secrets=()):
        self.calls.append(("run_commands", commands, secrets))
        return self


class FakeSource:
    def __init__(self, workdir, changed=()):
        self.workdir = workdir
        self.secrets = ()
        self.changed = tuple(PurePosixPath(path) for path in changed)
        self.pull_count = 0

    def fetch(self, image, *, stamp):
        image.calls.append(("fetch", self.workdir, stamp))
        return image

    def pull(self, sandbox):
        del sandbox
        self.pull_count += 1
        return self.changed


class FakeStream:
    def __init__(self, value=""):
        self.value = value

    def read(self):
        return self.value


class FakeProcess:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stdout = FakeStream()
        self.stderr = FakeStream(stderr)
        self.waited = False

    def wait(self):
        self.waited = True
        return self.returncode


class EnvTests(unittest.TestCase):
    def test_each_layer_setup_runs_in_its_source_workdir(self):
        image = FakeImage()
        env = Env(
            "api",
            base=image,
            source=FakeSource("/api"),
            setup=[run_commands("setup api")],
        )
        env.include(
            Layer(
                "docs",
                source=FakeSource("/docs"),
                setup=[run_commands("setup docs")],
            )
        )

        env.image(stamp="revision")

        self.assertEqual(
            image.calls,
            [
                ("fetch", "/api", "revision"),
                ("fetch", "/docs", "revision"),
                ("workdir", "/api"),
                ("run_commands", ("setup api",), ()),
                ("workdir", "/docs"),
                ("run_commands", ("setup docs",), ()),
                ("workdir", "/api"),
            ],
        )

    def test_ready_hook_waits_and_checks_the_process(self):
        process = FakeProcess()
        env = Env("api", base=FakeImage())
        env.ready(lambda _sandbox: process)

        env.start(object())

        self.assertTrue(process.waited)

    def test_ready_hook_failure_includes_stderr(self):
        env = Env("api", base=FakeImage())
        env.ready(lambda _sandbox: FakeProcess(2, "migration failed"))

        with self.assertRaisesRegex(HookError, "migration failed"):
            env.start(object())

    def test_ready_hook_must_return_a_process(self):
        env = Env("api", base=FakeImage())
        env.ready(lambda _sandbox: None)

        with self.assertRaisesRegex(HookError, "must return"):
            env.start(object())

    def test_start_hooks_preserve_registration_order(self):
        calls = []
        env = Env("api", base=FakeImage())
        env.on_start(lambda _sandbox: calls.append("one"))
        env.on_start(lambda _sandbox: calls.append("two"))

        env.start(object())

        self.assertEqual(calls, ["one", "two"])

    def test_freeze_prevents_all_registration_paths(self):
        env = Env("api", base=FakeImage())
        layer = Layer("docs")
        env.include(layer)
        env.freeze()

        self.assertTrue(env.is_frozen)
        self.assertTrue(layer.is_frozen)
        for action in (
            lambda: env.include(Layer("worker")),
            lambda: env.on_start(lambda _sandbox: None),
            lambda: layer.on_change("*.py"),
        ):
            with self.subTest(action=action), self.assertRaises(LifecycleError):
                action()

    def test_route_changes_uses_pathlib_glob_semantics(self):
        routed = []
        env = Env("api", base=FakeImage())

        @env.on_change("migrations/*")
        def changed(_sandbox, changes):
            routed.append(changes)

        env.route_changes(
            object(), ["migrations/one.py", "migrations/nested/two.py", "README.md"]
        )

        self.assertEqual(tuple(map(str, routed[0].matched)), ("migrations/one.py",))
        self.assertEqual(len(routed[0].all), 3)

    def test_refresh_pulls_shared_source_once_and_routes_to_each_layer(self):
        source = FakeSource("/api", ["pyproject.toml"])
        calls = []
        env = Env("api", base=FakeImage(), source=source)
        layer = Layer("tooling")
        env.include(layer)
        env.on_change("*")(lambda _sandbox, _changes: calls.append("env"))
        layer.on_change("*")(lambda _sandbox, _changes: calls.append("layer"))

        env.refresh(object())

        self.assertEqual(source.pull_count, 1)
        self.assertEqual(calls, ["env", "layer"])

    def test_spec_merges_layers_and_environment_precedence(self):
        env = Env(
            "api",
            base=FakeImage(),
            env={"SHARED": "env"},
            ports=[8000],
            mounts={"/env": "env-volume"},
            metadata={"owner": "env"},
            resources=Resources(cpu=2),
        )
        env.include(
            Layer(
                "docs",
                env={"SHARED": "layer", "DOCS": "1"},
                ports=[8000, 9000],
                mounts={"/docs": "docs-volume"},
                metadata={"team": "docs"},
            )
        )

        spec = env.spec()

        self.assertEqual(spec.env, {"SHARED": "env", "DOCS": "1"})
        self.assertEqual(spec.ports, (8000, 9000))
        self.assertEqual(spec.mounts, {"/docs": "docs-volume", "/env": "env-volume"})
        self.assertEqual(spec.metadata, {"team": "docs", "owner": "env"})
        self.assertEqual(spec.resources, Resources(cpu=2))

    def test_invalid_configuration_fails_at_declaration_time(self):
        invalid_factories = (
            lambda: Env("", base=FakeImage()),
            lambda: Env("api", base=FakeImage(), workdir="relative"),
            lambda: Layer(env={"VALID": 1}),
            lambda: Layer(ports=[0]),
            lambda: Layer(ports=[8000, 8000]),
            lambda: Layer(mounts={"relative": object()}),
            lambda: Resources(cpu=0),
            lambda: Resources(memory=-1),
        )
        for factory in invalid_factories:
            with (
                self.subTest(factory=factory),
                self.assertRaises((ConfigurationError, ValueError)),
            ):
                factory()

    def test_sync_alias_warns_and_routes(self):
        calls = []
        env = Env("api", base=FakeImage())
        env.on_change("*")(lambda _sandbox, changes: calls.append(changes))

        with self.assertWarns(DeprecationWarning):
            env.sync(object(), ["file.py"])

        self.assertEqual(len(calls), 1)


class SyncRuleTests(unittest.TestCase):
    def test_empty_change_set_does_not_call_handler(self):
        called = []
        rule = SyncRule(("*",), lambda *_args: called.append(True))

        self.assertEqual(rule.matched(()), ())
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
