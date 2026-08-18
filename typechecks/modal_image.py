import modal

from boxen.image import Image
from boxen.sandbox import Sandbox


def modal_image_satisfies_boxen_protocol(image: modal.Image) -> Image:
    return image


def modal_sandbox_satisfies_boxen_protocol(sandbox: modal.Sandbox) -> Sandbox:
    return sandbox
