import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

try:
    import modal
except ImportError:  # The Modal backend is optional.
    modal = None

from envy import Env, LocalSource, ModalRunner


@unittest.skipUnless(
    modal is not None and os.environ.get("ENVY_RUN_MODAL_INTEGRATION") == "1",
    "set ENVY_RUN_MODAL_INTEGRATION=1 with a configured Modal profile",
)
class ModalIntegrationTests(unittest.TestCase):
    def test_local_refresh_and_managed_cleanup_against_modal(self):
        assert modal is not None
        with TemporaryDirectory() as directory:
            root = Path(directory)
            message = root / "message.txt"
            obsolete = root / "obsolete.txt"
            message.write_text("before")
            obsolete.write_text("remove me")

            source = LocalSource(directory, workdir="/workspace")
            environment = Env(
                "integration",
                base=modal.Image.debian_slim(),
                source=source,
            )
            routed = []
            environment.on_change("message.txt")(
                lambda _sandbox, changes: routed.extend(changes.matched)
            )

            app = modal.App("envy-integration")
            with app.run():
                runner = ModalRunner(app, timeout=300)
                name = f"envy-integration-{uuid4().hex[:8]}"
                with runner.managed_run(environment, name=name) as sandbox:
                    initial = sandbox.exec("cat", "/workspace/message.txt")
                    self.assertEqual(initial.wait(), 0)
                    self.assertEqual(initial.stdout.read(), "before")

                    message.write_text("after")
                    obsolete.unlink()
                    tool = root / "tool.sh"
                    tool.write_text("#!/bin/sh\necho refreshed\n")
                    tool.chmod(0o755)

                    environment.refresh(sandbox)

                    refreshed = sandbox.exec("cat", "/workspace/message.txt")
                    self.assertEqual(refreshed.wait(), 0)
                    self.assertEqual(refreshed.stdout.read(), "after")
                    executable = sandbox.exec("/workspace/tool.sh")
                    self.assertEqual(executable.wait(), 0)
                    self.assertEqual(executable.stdout.read().strip(), "refreshed")
                    removed = sandbox.exec("test", "!", "-e", "/workspace/obsolete.txt")
                    self.assertEqual(removed.wait(), 0)
                    self.assertEqual(tuple(map(str, routed)), ("message.txt",))


if __name__ == "__main__":
    unittest.main()
