import unittest
from contextlib import contextmanager

from envy.modal import Envy, ModalRunner
from envy.sandbox import Resources, SandboxSpec


class FakeImage:
    def __init__(self) -> None:
        self.built_with = None

    def build(self, app):
        self.built_with = app
        return self

    def workdir(self, _path):
        return self


class FakeSandbox:
    def __init__(
        self,
        object_id="sb-created",
        *,
        terminate_error=None,
        detach_error=None,
    ) -> None:
        self.object_id = object_id
        self.terminated = False
        self.detached = False
        self.terminate_error = terminate_error
        self.detach_error = detach_error

    def terminate(self, *, wait=False):
        self.terminated = wait
        if self.terminate_error:
            raise self.terminate_error

    def detach(self):
        self.detached = True
        if self.detach_error:
            raise self.detach_error


class FakeModal:
    def __init__(self) -> None:
        self.lookup_calls = []
        self.create_calls = []
        self.created_sandboxes = []
        self.from_id_calls = []
        self.output_enabled = 0
        self.terminate_error = None
        self.detach_error = None
        owner = self

        class AppApi:
            def lookup(self, name, **kwargs):
                owner.lookup_calls.append((name, kwargs))
                return "resolved-app"

        class SandboxApi:
            def create(self, **kwargs):
                owner.create_calls.append(kwargs)
                sandbox = FakeSandbox(
                    terminate_error=owner.terminate_error,
                    detach_error=owner.detach_error,
                )
                owner.created_sandboxes.append(sandbox)
                return sandbox

            def from_id(self, sandbox_id):
                owner.from_id_calls.append(sandbox_id)
                return FakeSandbox(
                    sandbox_id,
                    terminate_error=owner.terminate_error,
                    detach_error=owner.detach_error,
                )

        self.App = AppApi()
        self.Sandbox = SandboxApi()

    @contextmanager
    def enable_output(self):
        self.output_enabled += 1
        yield


def make_spec(image):
    return SandboxSpec(
        image=image,
        workdir="/repo",
        env={"MODE": "dev"},
        secrets=("secret",),
        ports=(8000,),
        mounts={"/cache": "volume"},
        resources=Resources(cpu=2, memory=4096, gpu="T4"),
        metadata={"team": "api", "revision": 7},
    )


class ModalRunnerTests(unittest.TestCase):
    def test_launch_builds_and_maps_the_spec(self):
        modal = FakeModal()
        image = FakeImage()
        envy = Envy("envy-test")
        runner = ModalRunner(
            envy,
            environment_name="staging",
            timeout=3600,
            idle_timeout=600,
            _modal=modal,
        )

        sandbox = runner.launch_spec(
            make_spec(image), name="api", tags={"owner": "adam"}
        )

        self.assertIsInstance(sandbox, FakeSandbox)
        self.assertEqual(image.built_with, "resolved-app")
        self.assertEqual(modal.output_enabled, 1)
        self.assertEqual(
            modal.lookup_calls,
            [
                (
                    "envy-test",
                    {"create_if_missing": True, "environment_name": "staging"},
                )
            ],
        )
        self.assertEqual(
            modal.create_calls,
            [
                {
                    "app": "resolved-app",
                    "name": "api",
                    "tags": {"team": "api", "revision": "7", "owner": "adam"},
                    "image": image,
                    "env": {"MODE": "dev"},
                    "secrets": ("secret",),
                    "timeout": 3600,
                    "idle_timeout": 600,
                    "workdir": "/repo",
                    "gpu": "T4",
                    "cpu": 2,
                    "memory": 4096,
                    "volumes": {"/cache": "volume"},
                    "encrypted_ports": (8000,),
                }
            ],
        )

    def test_run_fires_ready_hooks_after_launch(self):
        modal = FakeModal()
        image = FakeImage()
        started = []
        envy = Envy("envy-test")
        env = envy.env(
            "api",
            base=image,
            metadata={"team": "api", "revision": 7},
        )
        env.on_start(lambda sandbox: started.append(sandbox))

        sandbox = ModalRunner(envy, show_output=False, _modal=modal).run(
            env, stamp="abc"
        )

        self.assertEqual(started, [sandbox])
        self.assertEqual(
            modal.create_calls[0]["tags"],
            {"team": "api", "revision": "7", "envy.env": "api"},
        )

    def test_run_cleans_up_when_a_ready_hook_fails(self):
        modal = FakeModal()
        image = FakeImage()

        def fail(_sandbox):
            raise ValueError("ready failed")

        envy = Envy("envy-test")
        env = envy.env("api", base=image)
        env.on_start(fail)

        with self.assertRaisesRegex(ValueError, "ready failed"):
            ModalRunner(envy, _modal=modal).run(env)

        sandbox = modal.created_sandboxes[0]
        self.assertTrue(sandbox.terminated)
        self.assertTrue(sandbox.detached)

    def test_launch_builds_and_runs_a_named_environment(self):
        modal = FakeModal()
        envy = Envy("acme-devboxes", stamp="revision-1")
        environment = envy.env("api", base=FakeImage())
        started = []
        environment.on_start(lambda sandbox: started.append(sandbox))
        runner = ModalRunner(
            envy,
            environment_name="dev",
            timeout=1800,
            idle_timeout=300,
            _modal=modal,
        )

        sandbox = runner.launch("api", name="adam", tags={"branch": "main"})

        self.assertEqual(sandbox.object_id, "sb-created")
        self.assertEqual(
            modal.lookup_calls,
            [
                (
                    "acme-devboxes",
                    {"create_if_missing": True, "environment_name": "dev"},
                )
            ],
        )
        self.assertEqual(
            modal.create_calls,
            [
                {
                    "app": "resolved-app",
                    "name": "adam",
                    "tags": {"envy.env": "api", "branch": "main"},
                    "image": environment.spec(stamp="revision-1").image,
                    "env": None,
                    "secrets": None,
                    "timeout": 1800,
                    "idle_timeout": 300,
                    "workdir": "/tmp/api",
                    "gpu": None,
                    "cpu": None,
                    "memory": None,
                    "volumes": {},
                    "encrypted_ports": (),
                }
            ],
        )
        self.assertEqual(started, modal.created_sandboxes)
        self.assertEqual(modal.from_id_calls, [])

    def test_session_launches_new_sandbox_and_only_detaches_on_exit(self):
        modal = FakeModal()
        envy = Envy("acme-devboxes")
        environment = envy.env("api", base=FakeImage())
        runner = ModalRunner(envy, _modal=modal)

        session = runner.session(environment, name="adam-api", tags={"branch": "main"})

        self.assertEqual(session.sandbox_id, "sb-created")
        self.assertEqual(modal.from_id_calls, [])
        with session as entered:
            self.assertIs(entered, session)
            self.assertFalse(session.sandbox.terminated)
            self.assertFalse(session.sandbox.detached)

        self.assertFalse(session.sandbox.terminated)
        self.assertTrue(session.sandbox.detached)

    def test_session_reopens_existing_sandbox_by_id(self):
        modal = FakeModal()
        envy = Envy("acme-devboxes")
        environment = envy.env("api", base=FakeImage())
        runner = ModalRunner(envy, _modal=modal)

        session = runner.session(environment, sandbox_id="sb-existing")

        self.assertEqual(session.sandbox_id, "sb-existing")
        self.assertEqual(modal.from_id_calls, ["sb-existing"])
        self.assertEqual(modal.create_calls, [])

        with session:
            pass

        self.assertTrue(session.sandbox.detached)
        self.assertFalse(session.sandbox.terminated)

    def test_session_reopen_rejects_creation_options(self):
        modal = FakeModal()
        envy = Envy("acme-devboxes")
        environment = envy.env("api", base=FakeImage())
        runner = ModalRunner(envy, _modal=modal)

        with self.assertRaisesRegex(ValueError, "cannot be used when reopening"):
            runner.session(environment, sandbox_id="sb-existing", name="adam-api")

    def test_session_detach_does_not_mask_body_errors(self):
        modal = FakeModal()
        modal.detach_error = RuntimeError("detach failed")
        envy = Envy("acme-devboxes")
        environment = envy.env("api", base=FakeImage())
        runner = ModalRunner(envy, _modal=modal)

        with self.assertRaisesRegex(ValueError, "body failed") as raised:
            with runner.session(environment):
                raise ValueError("body failed")

        self.assertIn("detach failed", " ".join(raised.exception.__notes__))

    def test_managed_run_terminates_and_detaches_after_use(self):
        modal = FakeModal()
        image = FakeImage()
        envy = Envy("envy-test")
        env = envy.env("api", base=image)
        runner = ModalRunner(envy, show_output=False, _modal=modal)

        with runner.managed_run(env) as sandbox:
            self.assertFalse(sandbox.terminated)
            self.assertFalse(sandbox.detached)

        self.assertTrue(sandbox.terminated)
        self.assertTrue(sandbox.detached)

    def test_managed_launch_cleans_up_without_masking_body_error(self):
        modal = FakeModal()
        envy = Envy("acme-devboxes")
        envy.env("api", base=FakeImage())
        runner = ModalRunner(envy, _modal=modal)

        with self.assertRaisesRegex(ValueError, "body failed"):
            with runner.managed_launch("api") as sandbox:
                raise ValueError("body failed")

        self.assertTrue(sandbox.terminated)
        self.assertTrue(sandbox.detached)

    def test_managed_cleanup_surfaces_all_cleanup_failures(self):
        modal = FakeModal()
        modal.terminate_error = RuntimeError("terminate failed")
        modal.detach_error = RuntimeError("detach failed")
        image = FakeImage()
        envy = Envy("envy-test")
        env = envy.env("api", base=image)

        with self.assertRaisesRegex(RuntimeError, "terminate failed") as raised:
            with ModalRunner(envy, show_output=False, _modal=modal).managed_run(env):
                pass

        self.assertIn("detach failed", " ".join(raised.exception.__notes__))

    def test_managed_cleanup_does_not_mask_body_errors(self):
        modal = FakeModal()
        modal.terminate_error = RuntimeError("terminate failed")
        modal.detach_error = RuntimeError("detach failed")
        envy = Envy("acme-devboxes")
        envy.env("api", base=FakeImage())
        runner = ModalRunner(envy, _modal=modal)

        with self.assertRaisesRegex(ValueError, "body failed") as raised:
            with runner.managed_launch("api"):
                raise ValueError("body failed")

        notes = " ".join(raised.exception.__notes__)
        self.assertIn("terminate failed", notes)
        self.assertIn("detach failed", notes)


class EnvyTests(unittest.TestCase):
    def test_launch_requires_a_registered_environment(self):
        envy = Envy("acme-devboxes")

        with self.assertRaisesRegex(KeyError, "not registered"):
            ModalRunner(envy, _modal=FakeModal()).launch("api")

    def test_launch_freezes_and_runs_the_declared_environment(self):
        modal = FakeModal()
        envy = Envy("acme-devboxes", stamp="revision-1")
        environment = envy.env(
            "api",
            base=FakeImage(),
            env={"MODE": "dev"},
            ports=[8000],
            resources=Resources(cpu=2, memory=4096),
            metadata={"team": "api"},
        )
        started = []
        environment.on_start(lambda sandbox: started.append(sandbox))

        sandbox = ModalRunner(
            envy,
            timeout=1800,
            idle_timeout=300,
            _modal=modal,
        ).launch("api", name="adam", tags={"branch": "main"})
        self.assertTrue(environment.is_frozen)

        self.assertEqual(envy.environments, ("api",))
        self.assertEqual(sandbox.object_id, "sb-created")
        self.assertEqual(started, modal.created_sandboxes)
        self.assertEqual(
            modal.create_calls[0],
            {
                "app": "resolved-app",
                "name": "adam",
                "tags": {
                    "team": "api",
                    "envy.env": "api",
                    "branch": "main",
                },
                "image": environment.spec(stamp="revision-1").image,
                "env": {"MODE": "dev"},
                "secrets": None,
                "timeout": 1800,
                "idle_timeout": 300,
                "workdir": "/tmp/api",
                "gpu": None,
                "cpu": 2,
                "memory": 4096,
                "volumes": {},
                "encrypted_ports": (8000,),
            },
        )

    def test_registration_closes_when_first_launch_freezes_declaration(self):
        modal = FakeModal()
        envy = Envy("acme-devboxes")
        envy.env("api", base=FakeImage())
        ModalRunner(envy, _modal=modal).launch("api")

        with self.assertRaisesRegex(RuntimeError, "after declaration freeze"):
            envy.env("worker", base=FakeImage())

    def test_environment_configuration_freezes_with_first_launch(self):
        envy = Envy("acme-devboxes")
        environment = envy.env("api", base=FakeImage())
        ModalRunner(envy, _modal=FakeModal()).launch("api")

        with self.assertRaisesRegex(RuntimeError, "frozen"):
            environment.on_start(lambda _sandbox: None)

    def test_duplicate_environment_names_are_rejected(self):
        envy = Envy("acme-devboxes")
        envy.env("api", base=FakeImage())

        with self.assertRaisesRegex(ValueError, "already registered"):
            envy.env("api", base=FakeImage())


if __name__ == "__main__":
    unittest.main()
