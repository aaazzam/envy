import unittest

from envy.steps import (
    add_local_dir,
    add_local_file,
    apt_install,
    dockerfile_commands,
    pip_install,
    run_commands,
    run_function,
    setenv,
    workdir,
)


class RecordingImage:
    def __init__(self):
        self.calls = []

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        return self

    def apt_install(self, *args, **kwargs):
        return self._record("apt_install", *args, **kwargs)

    def pip_install(self, *args, **kwargs):
        return self._record("pip_install", *args, **kwargs)

    def run_commands(self, *args, **kwargs):
        return self._record("run_commands", *args, **kwargs)

    def env(self, *args, **kwargs):
        return self._record("env", *args, **kwargs)

    def workdir(self, *args, **kwargs):
        return self._record("workdir", *args, **kwargs)

    def dockerfile_commands(self, *args, **kwargs):
        return self._record("dockerfile_commands", *args, **kwargs)

    def add_local_file(self, *args, **kwargs):
        return self._record("add_local_file", *args, **kwargs)

    def add_local_dir(self, *args, **kwargs):
        return self._record("add_local_dir", *args, **kwargs)

    def run_function(self, *args, **kwargs):
        return self._record("run_function", *args, **kwargs)


class StepTests(unittest.TestCase):
    def test_every_step_delegates_without_hiding_arguments(self):
        def build():
            return None

        steps = (
            apt_install("git", "curl"),
            pip_install("uv"),
            run_commands("uv sync", secrets=("secret",)),
            setenv({"MODE": "dev"}),
            workdir("/repo"),
            dockerfile_commands("RUN true"),
            add_local_file("pyproject.toml", "/repo/pyproject.toml", copy=True),
            add_local_dir("src", "/repo/src", ignore=("*.pyc",)),
            run_function(
                build,
                args=(1,),
                kwargs={"mode": "fast"},
                secrets=("secret",),
                mounts={"/cache": "volume"},
                always=True,
            ),
        )
        image = RecordingImage()

        for step in steps:
            self.assertIs(step(image), image)

        self.assertEqual(
            [call[0] for call in image.calls],
            [
                "apt_install",
                "pip_install",
                "run_commands",
                "env",
                "workdir",
                "dockerfile_commands",
                "add_local_file",
                "add_local_dir",
                "run_function",
            ],
        )
        self.assertEqual(image.calls[2][2], {"secrets": ("secret",)})
        self.assertEqual(image.calls[-1][2]["force_build"], True)


if __name__ == "__main__":
    unittest.main()
