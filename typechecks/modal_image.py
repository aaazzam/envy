import modal

from envy.image import Image
from envy.sandbox import Sandbox


def modal_image_satisfies_envy_protocol(image: modal.Image) -> Image:
    return image


def modal_sandbox_satisfies_envy_protocol(sandbox: modal.Sandbox) -> Sandbox:
    return sandbox
