import json
import shlex
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from envy.errors import (
    ConfigurationError,
    SourceCommandError,
    SourceRefreshUnsupported,
)
from envy.source import GitSource, LocalSource


class FakeImage:
    def __init__(self):
        self.calls = []

    def add_local_dir(self, local_path, remote_path, **kwargs):
        self.calls.append(("add_local_dir", local_path, remote_path, kwargs))
        return self

    def run_commands(self, *commands, **kwargs):
        self.calls.append(("run_commands", commands, kwargs))
        return self


class FakeStream:
    def __init__(self, value=""):
        self.value = value

    def read(self):
        return self.value


class FakeProcess:
    def __init__(self, returncode=0, *, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = FakeStream(stdout)
        self.stderr = FakeStream(stderr)

    def wait(self):
        return self.returncode


class FakeSandbox:
    def __init__(self, processes):
        self.processes = list(processes)
        self.commands = []

    def exec(self, *command):
        self.commands.append(command)
        return self.processes.pop(0)


class FakeFilesystemSandbox:
    def __init__(self, files=None):
        self.files = dict(files or {})
        self.modes = {}
        self.commands = []
        self.filesystem = self
        self.chmod_returncode = 0

    def copy_from_local(self, local_path, remote_path):
        self.files[remote_path] = Path(local_path).read_bytes()

    def read_text(self, remote_path):
        value = self.files[remote_path]
        return value.decode() if isinstance(value, bytes) else value

    def remove(self, path, recursive=False):
        del recursive
        if path not in self.files:
            raise FileNotFoundError(path)
        del self.files[path]

    def write_text(self, data, remote_path):
        self.files[remote_path] = data

    def exec(self, *command):
        self.commands.append(command)
        if command[0] == "chmod":
            self.modes[command[2]] = int(command[1], 8)
        return FakeProcess(self.chmod_returncode, stderr="chmod failed")


class LocalSourceTests(unittest.TestCase):
    def test_local_source_is_baked_into_the_image(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("print('hello')")
            (root / ".venv").mkdir()
            (root / ".venv" / "ignored.py").write_text("ignored")
            (root / "ignored.tmp").write_text("ignored")
            image = FakeImage()
            source = LocalSource(
                directory,
                workdir="/repo",
                ignore=(".git", ".venv", "*.tmp"),
            )

            result = source.fetch(image, stamp="revision-1")

        self.assertIs(result, image)
        self.assertEqual(
            image.calls[0],
            (
                "add_local_dir",
                directory,
                "/repo",
                {"copy": True, "ignore": (".git", ".venv", "*.tmp")},
            ),
        )
        _, commands, kwargs = image.calls[1]
        self.assertEqual(kwargs, {})
        manifest = json.loads(shlex.split(commands[0])[5])
        self.assertEqual(tuple(manifest), ("app.py",))

    def test_local_source_refresh_updates_creates_and_deletes_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            kept = root / "keep.txt"
            deleted = root / "delete.txt"
            kept.write_text("before")
            deleted.write_text("remove me")
            source = LocalSource(directory, workdir="/repo")
            image = FakeImage()
            source.fetch(image, stamp="one")
            manifest = shlex.split(image.calls[1][1][0])[5]
            sandbox = FakeFilesystemSandbox(
                {
                    source._manifest_path: manifest,
                    "/repo/keep.txt": b"before",
                    "/repo/delete.txt": b"remove me",
                }
            )

            kept.write_text("after")
            deleted.unlink()
            nested = root / "bin"
            nested.mkdir()
            executable = nested / "tool.sh"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)

            changed = source.pull(sandbox)
            unchanged = source.pull(sandbox)

        self.assertEqual(
            tuple(map(str, changed)), ("bin/tool.sh", "delete.txt", "keep.txt")
        )
        self.assertEqual(unchanged, ())
        self.assertEqual(sandbox.files["/repo/keep.txt"], b"after")
        self.assertEqual(sandbox.files["/repo/bin/tool.sh"], b"#!/bin/sh\nexit 0\n")
        self.assertNotIn("/repo/delete.txt", sandbox.files)
        self.assertEqual(sandbox.modes["/repo/bin/tool.sh"], 0o755)

    def test_local_source_refresh_requires_a_build_manifest(self):
        with TemporaryDirectory() as directory:
            source = LocalSource(directory)
            with self.assertRaisesRegex(SourceRefreshUnsupported, "rebuild"):
                source.pull(FakeFilesystemSandbox())

    def test_local_source_rejects_missing_paths_and_invalid_ignores(self):
        with TemporaryDirectory() as directory:
            missing = str(Path(directory) / "missing")
            with self.assertRaisesRegex(ConfigurationError, "existing directory"):
                LocalSource(missing).fetch(FakeImage(), stamp="one")
        for pattern in ("", "/absolute"):
            with self.subTest(pattern=pattern), self.assertRaises(ConfigurationError):
                LocalSource("./project", ignore=(pattern,))

    def test_local_source_rejects_symlinks(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.txt"
            target.write_text("target")
            (root / "link.txt").symlink_to(target)

            with self.assertRaisesRegex(ConfigurationError, "symlinks"):
                LocalSource(directory).fetch(FakeImage(), stamp="one")

    def test_local_source_rejects_malformed_manifests(self):
        malformed = ("[]", '{"path":"invalid"}', '{"path":[]}', '{"path":[1,2]}')
        with TemporaryDirectory() as directory:
            source = LocalSource(directory)
            for manifest in malformed:
                sandbox = FakeFilesystemSandbox({source._manifest_path: manifest})
                with (
                    self.subTest(manifest=manifest),
                    self.assertRaisesRegex(RuntimeError, "malformed"),
                ):
                    source.pull(sandbox)

    def test_local_source_reports_permission_update_failures(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "app.py"
            path.write_text("before")
            source = LocalSource(directory)
            image = FakeImage()
            source.fetch(image, stamp="one")
            manifest = shlex.split(image.calls[1][1][0])[5]
            sandbox = FakeFilesystemSandbox({source._manifest_path: manifest})
            sandbox.chmod_returncode = 1
            path.write_text("after")

            with self.assertRaisesRegex(SourceCommandError, "chmod failed"):
                source.pull(sandbox)

    def test_local_source_deletion_is_retry_safe(self):
        with TemporaryDirectory() as directory:
            source = LocalSource(directory)
            manifest = json.dumps({"already-gone.txt": ["digest", 420]})
            sandbox = FakeFilesystemSandbox({source._manifest_path: manifest})

            changed = source.pull(sandbox)

        self.assertEqual(tuple(map(str, changed)), ("already-gone.txt",))

    def test_local_source_requires_an_absolute_remote_workdir(self):
        with self.assertRaises(ConfigurationError):
            LocalSource("./project", workdir="relative")


class GitSourceTests(unittest.TestCase):
    def test_fetch_quotes_every_shell_controlled_value(self):
        image = FakeImage()
        source = GitSource(
            "https://example.com/repo.git; touch /tmp/pwned",
            ref="main; echo injected",
            workdir="/tmp/a dir",
        )

        source.fetch(image, stamp="x'; echo stamp-injected; '")

        _, commands, kwargs = image.calls[0]
        self.assertEqual(kwargs, {})
        self.assertEqual(
            shlex.split(commands[0]),
            ["echo", "envy source stamp: x'; echo stamp-injected; '"],
        )
        self.assertEqual(
            shlex.split(commands[1]),
            [
                "git",
                "clone",
                "--branch",
                "main; echo injected",
                "--recurse-submodules",
                "--",
                "https://example.com/repo.git; touch /tmp/pwned",
                "/tmp/a dir",
            ],
        )

    def test_fetch_injects_secrets_only_into_commands_that_need_them(self):
        image = FakeImage()
        secret = object()
        source = GitSource.github(
            "acme/private",
            secrets=(secret,),
            depth=1,
            tags=False,
            lfs=True,
        )

        source.fetch(image, stamp="abc")

        _, commands, kwargs = image.calls[0]
        self.assertEqual(kwargs, {"secrets": (secret,)})
        self.assertIn("credential.helper", commands[1])
        self.assertEqual(shlex.split(commands[2])[-2:], [source.url, source.workdir])
        self.assertEqual(
            shlex.split(commands[3]),
            ["git", "-C", source.workdir, "lfs", "pull"],
        )

    def test_pull_returns_changed_paths(self):
        sandbox = FakeSandbox(
            [
                FakeProcess(stdout="abc123\n"),
                FakeProcess(),
                FakeProcess(stdout="pyproject.toml\nsrc/envy/env.py\n"),
            ]
        )

        changed = GitSource.github("acme/api").pull(sandbox)

        self.assertEqual(
            tuple(map(str, changed)), ("pyproject.toml", "src/envy/env.py")
        )
        self.assertEqual(sandbox.commands[1][-2:], ("pull", "--ff-only"))

    def test_pull_failure_preserves_command_exit_code_and_stderr(self):
        sandbox = FakeSandbox([FakeProcess(128, stderr="fatal: not a repository")])

        with self.assertRaises(SourceCommandError) as raised:
            GitSource.github("acme/api").pull(sandbox)

        self.assertEqual(raised.exception.returncode, 128)
        self.assertIn("fatal: not a repository", str(raised.exception))

    def test_git_source_validates_configuration(self):
        for kwargs in (
            {"url": ""},
            {"url": "https://example.com/a.git", "ref": ""},
            {"url": "https://example.com/a.git", "depth": -1},
            {"url": "https://example.com/a.git", "token_env": "bad-name"},
            {"url": "https://example.com/a.git", "workdir": "relative"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ConfigurationError):
                GitSource(**kwargs)

        with self.assertRaisesRegex(ConfigurationError, "owner/name"):
            GitSource.github("not-an-owner-repository-pair")


if __name__ == "__main__":
    unittest.main()
