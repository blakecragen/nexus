"""Fernet-based field encryption for credential storage."""

from __future__ import annotations

import json

from cryptography.fernet import Fernet


class FieldEncryptor:
    """Encrypts and decrypts credential field dicts using Fernet symmetric encryption."""

    def __init__(self, key: str | bytes):
        if isinstance(key, str):
            key = key.encode()
        self._fernet = Fernet(key)

    @staticmethod
    def generate_key() -> str:
        """Generate a new Fernet encryption key."""
        return Fernet.generate_key().decode()

    def encrypt(self, fields: dict) -> bytes:
        """Serialize fields to JSON and encrypt."""
        plaintext = json.dumps(fields).encode()
        return self._fernet.encrypt(plaintext)

    def decrypt(self, ciphertext: bytes) -> dict:
        """Decrypt and deserialize fields from JSON."""
        plaintext = self._fernet.decrypt(ciphertext)
        return json.loads(plaintext.decode())
