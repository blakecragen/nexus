"""Built-in credential strategies for common service types."""

from __future__ import annotations

from nexus_server.services.credentials.base import CredentialStrategy


class S3Strategy(CredentialStrategy):
    credential_type = "s3"

    def validate(self, raw_fields: dict) -> None:
        for field in ("endpoint", "access_key", "secret_key"):
            if not raw_fields.get(field):
                raise ValueError(f"Missing required field: {field}")

    def serialize(self, raw_fields: dict) -> dict:
        return {
            "endpoint": raw_fields["endpoint"].strip(),
            "access_key": raw_fields["access_key"].strip(),
            "secret_key": raw_fields["secret_key"].strip(),
            "region": raw_fields.get("region", "").strip() or "us-east-1",
            "use_ssl": raw_fields.get("use_ssl", False),
        }

    def get_client_config(self, decrypted_fields: dict) -> dict:
        return {
            "endpoint_url": f"{'https' if decrypted_fields.get('use_ssl') else 'http'}://{decrypted_fields['endpoint']}",
            "aws_access_key_id": decrypted_fields["access_key"],
            "aws_secret_access_key": decrypted_fields["secret_key"],
            "region_name": decrypted_fields.get("region", "us-east-1"),
        }

    async def test_connection(self, decrypted_fields: dict) -> bool:
        import boto3
        config = self.get_client_config(decrypted_fields)
        client = boto3.client("s3", **config)
        client.list_buckets()
        return True

    @classmethod
    def required_fields(cls) -> list[str]:
        return ["endpoint", "access_key", "secret_key"]

    @classmethod
    def optional_fields(cls) -> list[str]:
        return ["region", "use_ssl"]

    @classmethod
    def description(cls) -> str:
        return "S3-compatible storage (MinIO, AWS S3, Backblaze B2)"


class GitPATStrategy(CredentialStrategy):
    credential_type = "git_pat"

    def validate(self, raw_fields: dict) -> None:
        if not raw_fields.get("token"):
            raise ValueError("Missing required field: token")

    def serialize(self, raw_fields: dict) -> dict:
        return {
            "token": raw_fields["token"].strip(),
            "username": raw_fields.get("username", "").strip() or "git",
        }

    def get_client_config(self, decrypted_fields: dict) -> dict:
        return {
            "token": decrypted_fields["token"],
            "username": decrypted_fields.get("username", "git"),
            "token_type": "pat",
        }

    async def test_connection(self, decrypted_fields: dict) -> bool:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {decrypted_fields['token']}"},
            )
            return resp.status_code == 200

    @classmethod
    def required_fields(cls) -> list[str]:
        return ["token"]

    @classmethod
    def optional_fields(cls) -> list[str]:
        return ["username"]

    @classmethod
    def description(cls) -> str:
        return "Git Personal Access Token (HTTPS authentication)"


class GitSSHStrategy(CredentialStrategy):
    credential_type = "git_ssh"

    def validate(self, raw_fields: dict) -> None:
        if not raw_fields.get("private_key"):
            raise ValueError("Missing required field: private_key")

    def serialize(self, raw_fields: dict) -> dict:
        return {
            "private_key": raw_fields["private_key"].strip(),
            "passphrase": raw_fields.get("passphrase", ""),
        }

    def get_client_config(self, decrypted_fields: dict) -> dict:
        return {
            "private_key": decrypted_fields["private_key"],
            "passphrase": decrypted_fields.get("passphrase", ""),
            "token_type": "ssh",
        }

    async def test_connection(self, decrypted_fields: dict) -> bool:
        return True

    @classmethod
    def required_fields(cls) -> list[str]:
        return ["private_key"]

    @classmethod
    def optional_fields(cls) -> list[str]:
        return ["passphrase"]

    @classmethod
    def description(cls) -> str:
        return "Git SSH private key"


class GDriveStrategy(CredentialStrategy):
    credential_type = "gdrive"

    def validate(self, raw_fields: dict) -> None:
        if not raw_fields.get("service_account_json"):
            raise ValueError("Missing required field: service_account_json")

    def serialize(self, raw_fields: dict) -> dict:
        import json
        sa = raw_fields["service_account_json"]
        if isinstance(sa, str):
            sa = json.loads(sa)
        return {"service_account_json": sa}

    def get_client_config(self, decrypted_fields: dict) -> dict:
        return {"service_account_json": decrypted_fields["service_account_json"]}

    async def test_connection(self, decrypted_fields: dict) -> bool:
        return True

    @classmethod
    def required_fields(cls) -> list[str]:
        return ["service_account_json"]

    @classmethod
    def description(cls) -> str:
        return "Google Drive (service account JSON)"


class SMBStrategy(CredentialStrategy):
    credential_type = "smb"

    def validate(self, raw_fields: dict) -> None:
        for field in ("username", "password"):
            if not raw_fields.get(field):
                raise ValueError(f"Missing required field: {field}")

    def serialize(self, raw_fields: dict) -> dict:
        return {
            "username": raw_fields["username"].strip(),
            "password": raw_fields["password"],
            "domain": raw_fields.get("domain", "").strip(),
        }

    def get_client_config(self, decrypted_fields: dict) -> dict:
        return {
            "username": decrypted_fields["username"],
            "password": decrypted_fields["password"],
            "domain": decrypted_fields.get("domain", ""),
        }

    async def test_connection(self, decrypted_fields: dict) -> bool:
        return True

    @classmethod
    def required_fields(cls) -> list[str]:
        return ["username", "password"]

    @classmethod
    def optional_fields(cls) -> list[str]:
        return ["domain"]

    @classmethod
    def description(cls) -> str:
        return "SMB/CIFS credentials for NAS storage"


class BasicStrategy(CredentialStrategy):
    credential_type = "basic"

    def validate(self, raw_fields: dict) -> None:
        for field in ("username", "password"):
            if not raw_fields.get(field):
                raise ValueError(f"Missing required field: {field}")

    def serialize(self, raw_fields: dict) -> dict:
        return {"username": raw_fields["username"].strip(), "password": raw_fields["password"]}

    def get_client_config(self, decrypted_fields: dict) -> dict:
        return {"username": decrypted_fields["username"], "password": decrypted_fields["password"]}

    async def test_connection(self, decrypted_fields: dict) -> bool:
        return True

    @classmethod
    def required_fields(cls) -> list[str]:
        return ["username", "password"]

    @classmethod
    def description(cls) -> str:
        return "Generic username + password"


ALL_STRATEGIES = [
    S3Strategy(), GitPATStrategy(), GitSSHStrategy(),
    GDriveStrategy(), SMBStrategy(), BasicStrategy(),
]
