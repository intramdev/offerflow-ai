"""Symmetric encryption for provider credentials stored in the database.

The API key lives in Django admin so every company shares one model
account; at rest it is Fernet-encrypted with a key derived from
SECRET_KEY. Decryption happens only inside the dispatch path, and the
plaintext is never logged, serialized, or rendered. Rotating SECRET_KEY
invalidates stored ciphertext by design: re-enter the key afterwards.
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings


class StoredKeyError(Exception):
    """A stored credential cannot be used (missing or undecryptable)."""


def _fernet() -> Fernet:
    secret = (settings.SECRET_KEY or "").encode()
    if not secret:
        raise StoredKeyError("No SECRET_KEY configured.")
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"offerflow-ai-provider-key",
        info=b"provider-api-key",
    ).derive(secret)
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_key(plaintext: str) -> str:
    """Encrypt a provider API key for storage. Empty stays empty."""
    text = (plaintext or "").strip()
    if not text:
        return ""
    return _fernet().encrypt(text.encode()).decode()


def decrypt_key(ciphertext: str) -> str:
    """Decrypt a stored key, failing closed on any tampering or rotation."""
    if not ciphertext:
        raise StoredKeyError("No stored API key.")
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise StoredKeyError(
            "Stored API key cannot be decrypted; re-enter it in admin."
        ) from exc
