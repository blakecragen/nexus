"""CredentialStrategy ABC — each credential type implements this."""

from __future__ import annotations

from abc import ABC, abstractmethod


class CredentialStrategy(ABC):
    """Strategy pattern for credential type handling.

    Each credential type (S3, Git PAT, Google Drive, etc.) implements this
    interface to handle validation, serialization, and connection testing.
    """

    credential_type: str

    @abstractmethod
    def validate(self, raw_fields: dict) -> None:
        """Validate that raw_fields contains all required fields.
        Raises ValueError with a descriptive message if invalid.
        """

    @abstractmethod
    def serialize(self, raw_fields: dict) -> dict:
        """Normalize and prepare fields for encrypted storage.
        Strip whitespace, set defaults, etc.
        """

    @abstractmethod
    def get_client_config(self, decrypted_fields: dict) -> dict:
        """Transform decrypted fields into the config dict consumers need.
        e.g., for S3: {"endpoint": ..., "access_key": ..., "secret_key": ...}
        """

    @abstractmethod
    async def test_connection(self, decrypted_fields: dict) -> bool:
        """Verify the credential works by attempting authentication.
        Returns True on success, raises on failure.
        """

    @classmethod
    def required_fields(cls) -> list[str]:
        """Return list of required field names for this credential type."""
        return []

    @classmethod
    def optional_fields(cls) -> list[str]:
        """Return list of optional field names for this credential type."""
        return []

    @classmethod
    def description(cls) -> str:
        """Human-readable description of this credential type."""
        return ""
