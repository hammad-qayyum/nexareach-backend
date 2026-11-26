"""AES-GCM encryption for secrets stored in the database.

Stored format: "v1:" + base64(nonce || ciphertext). The version prefix leaves room for key rotation.
"""

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

_PREFIX = "v1:"


def _key() -> bytes:
    raw = get_settings().encryption_key
    if not raw:
        raise RuntimeError("ENCRYPTION_KEY is not set")
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise RuntimeError("ENCRYPTION_KEY must be 32 bytes, base64-encoded")
    return key


def encrypt(plaintext: str) -> str:
    nonce = os.urandom(12)
    ciphertext = AESGCM(_key()).encrypt(nonce, plaintext.encode(), None)
    return _PREFIX + base64.b64encode(nonce + ciphertext).decode()


class WrongKey(ValueError):
    """The value was encrypted with a different ENCRYPTION_KEY (the key changed since it was saved)."""


def decrypt(token: str) -> str:
    if not token.startswith(_PREFIX):
        raise ValueError("Unknown ciphertext version")
    blob = base64.b64decode(token[len(_PREFIX) :])
    try:
        return AESGCM(_key()).decrypt(blob[:12], blob[12:], None).decode()
    except InvalidTag as e:
        raise WrongKey("Saved with a different ENCRYPTION_KEY; it can't be read with the current one.") from e


def generate_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()
