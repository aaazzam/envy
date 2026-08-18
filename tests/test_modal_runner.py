import os
import unittest
from contextlib import contextmanager
from unittest.mock import call, patch

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from envy.modal import Envy, ModalRunner
from envy.sandbox import Resources, SandboxSpec


class FakeImage:
    def __init__(self, object_id=None) -> None:
        self.built_with = None
        self.object_id = object_id

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


class FakeImageRegistry:
    def __init__(self) -> None:
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def __setitem__(self, key, value):
        self.values[key] = value


class FakeModalWithImageRegistry(FakeModal):
    def __init__(self) -> None:
        super().__init__()
        self.registry = FakeImageRegistry()
        self.dict_calls = []
        self.image_from_id_calls = []
        owner = self

        class DictApi:
            @staticmethod
            def from_name(name, **kwargs):
                owner.dict_calls.append((name, kwargs))
                return owner.registry

        class ImageApi:
            @staticmethod
            def from_id(image_id):
                owner.image_from_id_calls.append(image_id)
                return ("image-ref", image_id)

        self.Dict = DictApi()
        self.Image = ImageApi()


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

    def test_run_refreshes_before_start_hooks(self):
        modal = FakeModal()
        events = []
        envy = Envy("envy-test")
        env = envy.env("api", base=FakeImage())
        env.on_start(lambda _sandbox: events.append("start"))

        with patch.object(
            env, "refresh", side_effect=lambda _sandbox: events.append("refresh")
        ):
            ModalRunner(envy, show_output=False, _modal=modal).run(env)

        self.assertEqual(events, ["refresh", "start"])

    def test_rebake_records_image_ids_and_launch_uses_latest_image(self):
        modal = FakeModalWithImageRegistry()
        envy = Envy("acme-devboxes")
        envy.env("api", base=FakeImage("img-api"))
        runner = ModalRunner(envy, show_output=False, _modal=modal)

        self.assertEqual(runner.rebake(stamp="commit-sha"), {"api": "img-api"})
        self.assertEqual(modal.registry.values["api"]["stamp"], "commit-sha")

        runner.launch("api")

        self.assertEqual(modal.image_from_id_calls, ["img-api"])
        self.assertEqual(modal.create_calls[0]["image"], ("image-ref", "img-api"))

    def test_install_rebake_schedule_registers_modal_cron(self):
        modal = FakeModal()
        envy = Envy("acme-devboxes")
        envy.env("api", base=FakeImage())
        runner = ModalRunner(envy, show_output=False, _modal=modal)

        class FakeModalApp:
            def __init__(self):
                self.function_options = None

            def function(self, **kwargs):
                self.function_options = kwargs

                def decorate(fn):
                    return fn

                return decorate

        modal_app = FakeModalApp()
        rebake = runner.install_rebake_schedule(modal_app)

        self.assertEqual(rebake.__name__, "rebake")
        self.assertEqual(modal_app.function_options["name"], "envy-rebake")
        self.assertEqual(
            modal_app.function_options["schedule"].proto_message.cron.cron_string,
            "*/30 * * * *",
        )
        self.assertTrue(modal_app.function_options["serialized"])
        with patch.object(
            runner, "rebake", return_value={"api": "image-id"}
        ) as rebake_mock:
            self.assertEqual(rebake(), {"api": "image-id"})
        rebake_mock.assert_called_once_with()
        self.assertIs(runner.install_rebake_schedule(modal_app), rebake)

    def test_modal_app_binding_registers_default_rebake_cron(self):
        modal = FakeModal()
        envy = Envy("acme-devboxes")
        envy.env("api", base=FakeImage())

        class FakeModalApp:
            def __init__(self):
                self.function_options = None

            def function(self, **kwargs):
                self.function_options = kwargs

                def decorate(fn):
                    return fn

                return decorate

        modal_app = FakeModalApp()
        ModalRunner(envy, show_output=False, modal_app=modal_app, _modal=modal)

        self.assertEqual(modal_app.function_options["name"], "envy-rebake")
        self.assertEqual(
            modal_app.function_options["schedule"].proto_message.cron.cron_string,
            "*/30 * * * *",
        )

    def test_install_rebake_endpoint_configures_auth_and_supports_one_or_all(self):
        import modal

        envy = Envy("acme-devboxes")
        envy.env("api", base=FakeImage())
        runner = ModalRunner(envy, show_output=False, _modal=FakeModal())

        class FakeModalApp:
            def __init__(self):
                self.function_options = []

            def function(self, **kwargs):
                self.function_options.append(kwargs)

                def decorate(fn):
                    return fn

                return decorate

        endpoint_options = []

        def endpoint_decorator(**kwargs):
            endpoint_options.append(kwargs)

            def decorate(fn):
                return fn

            return decorate

        modal_app = FakeModalApp()
        secret = object()
        with (
            patch.object(modal.Secret, "from_name", return_value=secret) as from_name,
            patch.object(modal, "fastapi_endpoint", endpoint_decorator),
        ):
            endpoint = runner.install_rebake_endpoint(
                modal_app,
                token_secret="acme-devbox-rebake-token",
            )
            endpoint_without_proxy_auth = runner.install_rebake_endpoint(
                modal_app,
                token_secret="acme-devbox-rebake-token",
                requires_proxy_auth=False,
            )

        from_name.assert_has_calls(
            [
                call(
                    "acme-devbox-rebake-token",
                    required_keys=["ENVY_REBAKE_TOKEN"],
                ),
                call(
                    "acme-devbox-rebake-token",
                    required_keys=["ENVY_REBAKE_TOKEN"],
                ),
            ]
        )
        self.assertEqual(modal_app.function_options[0]["secrets"], [secret])
        self.assertTrue(modal_app.function_options[0]["serialized"])
        self.assertEqual(
            endpoint_options,
            [
                {"method": "POST", "label": None, "requires_proxy_auth": True},
                {"method": "POST", "label": None, "requires_proxy_auth": False},
            ],
        )

        from fastapi.security import HTTPAuthorizationCredentials

        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="correct"
        )
        with (
            patch.dict(os.environ, {"ENVY_REBAKE_TOKEN": "correct"}),
            patch.object(
                runner,
                "rebake",
                side_effect=[
                    {"api": "image-id"},
                    {"api": "image-id", "worker": "worker-image-id"},
                ],
            ) as rebake_mock,
        ):
            self.assertEqual(
                endpoint(credentials, {"environment": "api"}),
                {"api": "image-id"},
            )
            self.assertEqual(
                endpoint_without_proxy_auth(credentials),
                {"api": "image-id", "worker": "worker-image-id"},
            )

        rebake_mock.assert_has_calls([call(environment="api"), call(environment=None)])

    def test_install_rebake_endpoint_requires_a_valid_token_variable(self):
        envy = Envy("acme-devboxes")
        envy.env("api", base=FakeImage())
        runner = ModalRunner(envy, show_output=False, _modal=FakeModal())

        with self.assertRaisesRegex(ValueError, "token_env"):
            runner.install_rebake_endpoint(
                object(), token_secret="secret", token_env="bad-name"
            )

    def test_install_rebake_endpoint_checks_bearer_token(self):
        envy = Envy("acme-devboxes")
        envy.env("api", base=FakeImage())
        runner = ModalRunner(envy, show_output=False, _modal=FakeModal())

        class FakeModalApp:
            def function(self, **_kwargs):
                def decorate(fn):
                    return fn

                return decorate

        endpoint = runner.install_rebake_endpoint(FakeModalApp(), token_secret="secret")
        raw_endpoint = endpoint._get_raw_f()

        with (
            patch.dict(os.environ, {"ENVY_REBAKE_TOKEN": "correct"}),
            patch.object(runner, "rebake", return_value={"api": "image-id"}),
        ):
            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer", credentials="correct"
            )
            self.assertEqual(
                raw_endpoint(credentials, {"environment": "api"}),
                {"api": "image-id"},
            )

            with self.assertRaises(HTTPException):
                raw_endpoint(
                    HTTPAuthorizationCredentials(scheme="Bearer", credentials="wrong"),
                    "api",
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

    def test_launch_can_enable_exit_snapshots(self):
        modal = FakeModal()
        envy = Envy("acme-devboxes")
        envy.env("api", base=FakeImage())
        runner = ModalRunner(envy, show_output=False, _modal=modal)

        runner.launch("api", experimental_options={"enable_exit_snapshot": True})

        self.assertEqual(
            modal.create_calls[0]["experimental_options"],
            {"enable_exit_snapshot": True},
        )

    def test_launch_from_snapshot_can_scope_secrets_to_publisher(self):
        modal = FakeModal()
        envy = Envy("acme-devboxes")
        envy.env("api", base=FakeImage(), secrets=("environment-secret",))
        runner = ModalRunner(envy, show_output=False, _modal=modal)
        snapshot = FakeImage("exit-snapshot")

        runner.launch_from_snapshot("api", snapshot, secrets=("git-secret",))

        self.assertEqual(modal.create_calls[0]["image"], snapshot)
        self.assertEqual(modal.create_calls[0]["secrets"], ("git-secret",))
        self.assertEqual(
            modal.create_calls[0]["experimental_options"],
            {"enable_exit_snapshot": True},
        )

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

    def test_readme_session_refreshes_new_sandboxes_and_reopens_by_id(self):
        modal = FakeModal()
        envy = Envy("acme-devboxes")
        environment = envy.env("api", base=FakeImage())
        runner = ModalRunner(envy, _modal=modal)
        refreshes = []

        with patch.object(
            environment, "refresh", side_effect=lambda _sandbox: refreshes.append(True)
        ):
            session = runner.session(environment)
            self.assertEqual(refreshes, [True])
            sandbox_id = session.sandbox_id
            with session as active:
                self.assertIs(active, session)

            reopened = runner.session(environment, sandbox_id=sandbox_id)
            self.assertEqual(refreshes, [True])
            with reopened as active:
                environment.refresh(active.sandbox)

        self.assertEqual(refreshes, [True, True])
        self.assertEqual(modal.from_id_calls, [sandbox_id])
        self.assertFalse(session.sandbox.terminated)
        self.assertTrue(session.sandbox.detached)
        self.assertTrue(reopened.sandbox.detached)

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
