"""Central Credential Manager — all subsystems access credentials through here."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from nexus_server.db import ops
from nexus_server.services.credentials.base import CredentialStrategy
from nexus_server.services.credentials.encryption import FieldEncryptor
from nexus_server.services.credentials.strategies import ALL_STRATEGIES


class CredentialManager:
    """Central credential registry and accessor.

    All subsystems (storage backends, git steps, etc.) request credentials
    through this manager. It handles encryption, strategy dispatch, and
    access control.
    """

    def __init__(self, encryption_key: str):
        self._encryptor = FieldEncryptor(encryption_key)
        self._strategies: dict[str, CredentialStrategy] = {}
        for strategy in ALL_STRATEGIES:
            self.register_strategy(strategy)

    def register_strategy(self, strategy: CredentialStrategy) -> None:
        """Register a credential type strategy."""
        self._strategies[strategy.credential_type] = strategy

    def get_strategy(self, credential_type: str) -> CredentialStrategy:
        """Look up strategy by type. Raises KeyError if unknown."""
        if credential_type not in self._strategies:
            available = sorted(self._strategies.keys())
            raise KeyError(f"Unknown credential type '{credential_type}'. Available: {available}")
        return self._strategies[credential_type]

    def list_types(self) -> list[dict]:
        """Return metadata for all registered credential types."""
        return [
            {
                "credential_type": s.credential_type,
                "required_fields": s.required_fields(),
                "optional_fields": s.optional_fields(),
                "description": s.description(),
            }
            for s in self._strategies.values()
        ]

    async def store(
        self, db: AsyncSession, name: str, credential_type: str, fields: dict,
        owner_id: UUID, is_shared: bool = False, allowed_groups: list | None = None,
        description: str | None = None,
    ) -> UUID:
        """Validate, encrypt, and store a credential. Returns credential ID."""
        strategy = self.get_strategy(credential_type)
        strategy.validate(fields)
        serialized = strategy.serialize(fields)
        encrypted = self._encryptor.encrypt(serialized)

        cred = await ops.create_credential(
            db, name=name, credential_type=credential_type,
            encrypted_fields=encrypted, owner_id=owner_id,
            is_shared=is_shared, allowed_groups=allowed_groups or [],
            description=description,
        )
        return cred.id

    async def get(self, db: AsyncSession, credential_id: UUID) -> dict:
        """Decrypt credential and return client config via its strategy."""
        cred = await ops.get_credential_by_id(db, credential_id)
        if not cred:
            raise KeyError(f"Credential {credential_id} not found")

        strategy = self.get_strategy(cred.credential_type)
        decrypted = self._encryptor.decrypt(cred.encrypted_fields)
        return strategy.get_client_config(decrypted)

    async def get_by_name(self, db: AsyncSession, name: str) -> dict:
        """Look up credential by name and return client config."""
        cred = await ops.get_credential_by_name(db, name)
        if not cred:
            raise KeyError(f"Credential '{name}' not found")

        strategy = self.get_strategy(cred.credential_type)
        decrypted = self._encryptor.decrypt(cred.encrypted_fields)
        return strategy.get_client_config(decrypted)

    async def test(self, db: AsyncSession, credential_id: UUID) -> bool:
        """Test that a stored credential works."""
        cred = await ops.get_credential_by_id(db, credential_id)
        if not cred:
            raise KeyError(f"Credential {credential_id} not found")

        strategy = self.get_strategy(cred.credential_type)
        decrypted = self._encryptor.decrypt(cred.encrypted_fields)
        return await strategy.test_connection(decrypted)

    async def update_fields(
        self, db: AsyncSession, credential_id: UUID, fields: dict,
    ) -> None:
        """Re-validate, re-encrypt, and update credential fields."""
        cred = await ops.get_credential_by_id(db, credential_id)
        if not cred:
            raise KeyError(f"Credential {credential_id} not found")

        strategy = self.get_strategy(cred.credential_type)
        strategy.validate(fields)
        serialized = strategy.serialize(fields)
        encrypted = self._encryptor.encrypt(serialized)

        await ops.update_credential(db, credential_id, encrypted_fields=encrypted)
