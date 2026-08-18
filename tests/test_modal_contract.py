import unittest

try:
    import modal
except ImportError:  # The Modal backend is optional.
    modal = None

from boxen import Env, apt_install, run_commands


@unittest.skipIf(modal is None, "Modal extra is not installed")
class ModalContractTests(unittest.TestCase):
    def test_modal_image_compiles_without_remote_calls(self):
        assert modal is not None
        image = modal.Image.debian_slim()
        env = Env(
            "contract",
            base=image,
            build=[apt_install("git")],
            setup=[run_commands("true")],
        )

        spec = env.spec()

        self.assertIsInstance(spec.image, modal.Image)
        self.assertEqual(spec.workdir, "/tmp/contract")


if __name__ == "__main__":
    unittest.main()
