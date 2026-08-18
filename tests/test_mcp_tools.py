from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import fastmcp

    from envy import Envy
    from envy.mcp import toolbox
    from envy.mcp.errors import NotAFileError, NotOurSandboxError, SandboxCommandError
    from envy.mcp.tools.bash import MAX_TIMEOUT_MS, _timeout_seconds, bash
    from envy.mcp.tools.edit import edit
    from envy.mcp.tools.glob import glob
    from envy.mcp.tools.grep import grep
    from envy.mcp.tools.read import read
    from envy.mcp.tools.write import write
except ImportError:
    fastmcp = None


class FakeImage:
    def workdir(self, _path: str) -> FakeImage:
        return self


class FakeStream:
    def __init__(self, content: str) -> None:
        self.content = content

    def read(self) -> str:
        return self.content


class FakeProcess:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self.stdout = FakeStream(stdout)
        self.stderr = FakeStream(stderr)
        self.returncode = returncode

    def wait(self) -> int:
        return self.returncode


class FakeFile:
    def __init__(self, sandbox: FakeSandbox, mode: str) -> None:
        self.sandbox = sandbox
        self.mode = mode

    def __enter__(self) -> FakeFile:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def read(self) -> str:
        return self.sandbox.content

    def write(self, content: str) -> None:
        self.sandbox.content = content


class FakeSandbox:
    def __init__(
        self,
        *,
        tags: dict[str, str] | None = None,
        content: str = "",
        process: FakeProcess | None = None,
        open_error: BaseException | None = None,
    ) -> None:
        self.tags = tags or {}
        self.content = content
        self.process = process or FakeProcess()
        self.open_error = open_error
        self.calls: list[tuple[str, ...]] = []
        self.timeouts: list[int | None] = []
        self.opens: list[tuple[str, str]] = []

    def get_tags(self) -> dict[str, str]:
        return self.tags

    def exec(self, *args: str, timeout: int | None = None) -> FakeProcess:
        self.calls.append(args)
        self.timeouts.append(timeout)
        return self.process

    def open(self, path: str, mode: str) -> FakeFile:
        self.opens.append((path, mode))
        if self.open_error is not None:
            raise self.open_error
        return FakeFile(self, mode)


def make_envy(*names: str) -> Envy:
    envy = Envy("test-app")
    for name in names:
        envy.env(name, base=FakeImage())
    return envy


def modal_for(sandbox: FakeSandbox) -> SimpleNamespace:
    return SimpleNamespace(Sandbox=SimpleNamespace(from_id=lambda _sandbox_id: sandbox))


@unittest.skipIf(fastmcp is None, "MCP dependencies are not installed")
class MCPToolboxTests(unittest.TestCase):
    def resolve(self, sandbox: FakeSandbox, *names: str):
        envy = make_envy(*(names or ("api",)))
        return patch.multiple(
            toolbox,
            _envies=[envy],
            modal=modal_for(sandbox),
        )

    def test_command_result_rendering_and_checking(self) -> None:
        self.assertTrue(toolbox.CommandResult("out", "", 0).ok)
        self.assertFalse(toolbox.CommandResult("out", "", 1).ok)
        self.assertEqual(
            toolbox.CommandResult("out\n", "err\n", 0).merged_output(), "out\nerr"
        )
        self.assertEqual(toolbox.CommandResult("out", "", 0).shell_output(), "out")
        self.assertEqual(
            toolbox.CommandResult("boom", "", 2).shell_output(),
            "boom\n[exit code: 2]",
        )
        self.assertEqual(
            toolbox.CommandResult("", "", 3).shell_output(), "[exit code: 3]"
        )
        result = toolbox.CommandResult("out", "", 0)
        self.assertIs(result.check(), result)
        with self.assertRaisesRegex(SandboxCommandError, "'git push'.*boom"):
            toolbox.CommandResult("", "boom", 1, command=("git", "push")).check()
        with self.assertRaisesRegex(SandboxCommandError, "'<command>'"):
            toolbox.CommandResult("", "", 1).check()

    def test_validation_and_description_helpers(self) -> None:
        self.assertEqual(toolbox.require_absolute_path("/repo/a.txt"), "/repo/a.txt")
        with self.assertRaisesRegex(ValueError, "absolute path"):
            toolbox.require_absolute_path("repo/a.txt", parameter="file")
        self.assertEqual(toolbox.validate_line_window(offset=3, limit=4), (2, 4))
        self.assertEqual(
            toolbox.validate_line_window(offset=None, limit=None), (0, 2000)
        )
        with self.assertRaisesRegex(ValueError, "1-indexed"):
            toolbox.validate_line_window(offset=0, limit=None)
        with self.assertRaisesRegex(ValueError, "non-negative"):
            toolbox.validate_line_window(offset=None, limit=-1)

        with tempfile.TemporaryDirectory() as directory:
            tool_path = Path(directory) / "tool.py"
            tool_path.with_suffix(".txt").write_text("description", encoding="utf-8")
            self.assertEqual(
                toolbox.load_tool_description(str(tool_path)), "description"
            )

    def test_registration_helpers_preserve_identity(self) -> None:
        first = make_envy("api")
        second = make_envy("worker")
        with patch.object(toolbox, "_envies", []):
            toolbox.register_envy(first)
            toolbox.register_envy(first)
            toolbox.register_envy(second)
            self.assertEqual(toolbox._envies, [first, second])
            toolbox.unregister_envy(first)
            self.assertEqual(toolbox._envies, [second])
            toolbox.unregister_envy(make_envy("other"))
            self.assertEqual(toolbox._envies, [second])
            toolbox.set_envy(first)
            self.assertEqual(toolbox._envies, [first])
            toolbox.set_envy(None)
            self.assertEqual(toolbox._envies, [])

    def test_resolve_and_require_check_sandbox_ownership(self) -> None:
        sandbox = FakeSandbox(
            tags={toolbox.APP_TAG: "test-app", toolbox.ENV_TAG: "api"}
        )
        with self.resolve(sandbox):
            owned = toolbox.resolve_sandbox("sb-1")
            self.assertIs(owned.sandbox, sandbox)
            self.assertEqual(owned.environment.name, "api")
            self.assertIs(
                toolbox.require_registered_sandbox("sb-1", owned.envy).sandbox,
                sandbox,
            )

        for tags in ({}, {toolbox.APP_TAG: "other", toolbox.ENV_TAG: "api"}):
            with (
                self.subTest(tags=tags),
                patch.object(
                    toolbox,
                    "_envies",
                    [make_envy("api")],
                ),
                patch.object(toolbox, "modal", modal_for(FakeSandbox(tags=tags))),
            ):
                with self.assertRaises(NotOurSandboxError):
                    toolbox.resolve_sandbox("sb-1")

    def test_run_command_captures_streams_and_timeout(self) -> None:
        sandbox = FakeSandbox(process=FakeProcess(stdout="out", stderr="err"))
        result = toolbox.run_command(sandbox, "echo", "hi", timeout=7)
        self.assertEqual(result.stdout, "out")
        self.assertEqual(result.stderr, "err")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.command, ("echo", "hi"))
        self.assertEqual(sandbox.timeouts, [7])


@unittest.skipIf(fastmcp is None, "MCP dependencies are not installed")
class MCPToolTests(unittest.TestCase):
    def run_with(self, sandbox: FakeSandbox, function, **kwargs):
        envy = make_envy("api")
        sandbox.tags.update({toolbox.APP_TAG: envy.name, toolbox.ENV_TAG: "api"})
        with patch.multiple(toolbox, _envies=[envy], modal=modal_for(sandbox)):
            return function(sandbox_id="sb-1", **kwargs)

    def test_bash_timeout_and_output(self) -> None:
        self.assertIsNone(_timeout_seconds(None))
        self.assertEqual(_timeout_seconds(1500), 2)
        self.assertEqual(_timeout_seconds(MAX_TIMEOUT_MS * 2), 600)
        with self.assertRaisesRegex(ValueError, "positive"):
            _timeout_seconds(0)
        with self.assertRaisesRegex(ValueError, "positive"):
            _timeout_seconds(-1)

        sandbox = FakeSandbox(process=FakeProcess(stdout="hi\n"))
        self.assertEqual(
            self.run_with(sandbox, bash, command="echo hi", workdir="/repo"), "hi"
        )
        self.assertEqual(sandbox.calls, [("bash", "-c", "cd /repo && echo hi")])
        self.assertEqual(
            self.run_with(
                FakeSandbox(process=FakeProcess(stdout="boom", returncode=1)),
                bash,
                command="false",
            ),
            "boom\n[exit code: 1]",
        )

    def test_read_formats_windows_and_handles_directories(self) -> None:
        sandbox = FakeSandbox(content="one\ntwo\nthree")
        self.assertEqual(
            self.run_with(sandbox, read, file_path="/repo/a.txt", offset=2, limit=1),
            "     2\ttwo",
        )
        long_line = FakeSandbox(content="x" * 3000)
        rendered = self.run_with(long_line, read, file_path="/repo/a.txt")
        self.assertEqual(len(rendered.split("\t", 1)[1]), 2000)
        with self.assertRaisesRegex(ValueError, "absolute path"):
            self.run_with(sandbox, read, file_path="a.txt")
        with self.assertRaises(NotAFileError):
            self.run_with(
                FakeSandbox(open_error=IsADirectoryError("directory")),
                read,
                file_path="/repo/dir",
            )

    def test_write_and_edit_modify_files(self) -> None:
        sandbox = FakeSandbox(content="hello world")
        self.assertIn(
            "/repo/a.txt",
            self.run_with(sandbox, write, file_path="/repo/a.txt", content="new"),
        )
        self.assertEqual(sandbox.content, "new")
        with self.assertRaisesRegex(ValueError, "absolute path"):
            self.run_with(sandbox, write, file_path="a.txt", content="x")

        sandbox.content = "a a a"
        with self.assertRaisesRegex(ValueError, "not unique"):
            self.run_with(
                sandbox,
                edit,
                file_path="/repo/a.txt",
                old_string="a",
                new_string="b",
            )
        result = self.run_with(
            sandbox,
            edit,
            file_path="/repo/a.txt",
            old_string="a",
            new_string="b",
            replace_all=True,
        )
        self.assertIn("3 replacement", result)
        self.assertEqual(sandbox.content, "b b b")
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            self.run_with(
                sandbox,
                edit,
                file_path="/repo/a.txt",
                old_string="",
                new_string="x",
            )
        with self.assertRaisesRegex(ValueError, "must be different"):
            self.run_with(
                sandbox,
                edit,
                file_path="/repo/a.txt",
                old_string="b",
                new_string="b",
            )
        with self.assertRaisesRegex(ValueError, "not found"):
            self.run_with(
                sandbox,
                edit,
                file_path="/repo/a.txt",
                old_string="z",
                new_string="x",
            )

    def test_glob_and_grep_render_search_results(self) -> None:
        self.assertEqual(
            self.run_with(
                FakeSandbox(process=FakeProcess(stdout="a.py\nb.py\n")),
                glob,
                pattern="*.py",
            ),
            "a.py\nb.py",
        )
        self.assertEqual(
            self.run_with(FakeSandbox(process=FakeProcess()), glob, pattern="*.none"),
            "No files found",
        )
        self.assertEqual(
            self.run_with(
                FakeSandbox(process=FakeProcess(stderr="failed", returncode=2)),
                glob,
                pattern="*.py",
                path="/missing",
            ),
            "failed",
        )
        self.assertEqual(
            self.run_with(
                FakeSandbox(process=FakeProcess(stdout="a.py:1:hit\n")),
                grep,
                pattern="hit",
                include="*.py",
            ),
            "a.py:1:hit",
        )
        self.assertEqual(
            self.run_with(
                FakeSandbox(process=FakeProcess(returncode=1)),
                grep,
                pattern="none",
            ),
            "No matches found",
        )
        self.assertEqual(
            self.run_with(
                FakeSandbox(process=FakeProcess(returncode=2)),
                grep,
                pattern="(",
            ),
            "(grep failed, exit 2)",
        )
        with self.assertRaisesRegex(ValueError, "pattern is required"):
            self.run_with(FakeSandbox(), grep, pattern="")


if __name__ == "__main__":
    unittest.main()
