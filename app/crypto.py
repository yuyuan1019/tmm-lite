"""Encryption helpers for sensitive fields (connection credentials).

Uses Fernet (AES-128-CBC + HMAC) symmetric encryption.
Key persisted in ``<data_dir>/.keyfile``; generated on first startup.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

_KEY_FILENAME = ".keyfile"


def load_or_create_key(data_dir: Path) -> bytes:
    """Return the Fernet key, creating it if it doesn't exist."""
    key_path = data_dir / _KEY_FILENAME
    if key_path.exists():
        return key_path.read_bytes()
    key = Fernet.generate_key()
    key_path.write_bytes(key)
    # Restrict permissions (best-effort on Windows)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    logger.info("Generated new encryption key at %s", key_path)
    return key


def encrypt_str(plaintext: str, key: bytes) -> str:
    """Encrypt a plaintext string, returning a base64 token."""
    f = Fernet(key)
    token = f.encrypt(plaintext.encode("utf-8"))
    return base64.urlsafe_b64encode(token).decode("ascii")


def decrypt_str(token: str, key: bytes) -> str:
    """Decrypt a token back to plaintext."""
    f = Fernet(key)
    raw = base64.urlsafe_b64decode(token.encode("ascii"))
    return f.decrypt(raw).decode("utf-8")


def encrypt_dict(data: dict[str, object], key: bytes) -> str:
    """Encrypt a JSON-serialisable dict into a token."""
    return encrypt_str(json.dumps(data, ensure_ascii=False), key)


def decrypt_dict(token: str, key: bytes) -> dict[str, object]:
    """Decrypt a token back to a dict."""
    plain = decrypt_str(token, key)
    result = json.loads(plain)
    if not isinstance(result, dict):
        raise TypeError("Decrypted data is not a dict")
    return result
