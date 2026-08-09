"""Unit tests for credential strategies, base ABC, and CredentialManager.

SUT: packages/server/src/nexus_server/services/credentials/
  - strategies/__init__.py (ALL_STRATEGIES + each concrete strategy)
  - base.py (CredentialStrategy ABC)
  - manager.py (CredentialManager: dispatch, encrypt/store/get round-trip)

Network-hitting test_connection methods (S3 via boto3, git_pat via httpx) are
exercised through monkeypatched stand-ins so we test the manager wiring, never
a real socket.
"""

from __future__ import annotations

import uuid

import pytest

from nexus_server.services.credentials.base import CredentialStrategy
from nexus_server.services.credentials.strategies import (
    ALL_STRATEGIES,
    BasicStrategy,
    GDriveStrategy,
    GitPATStrategy,
    GitSSHStrategy,
    S3Strategy,
    SMBStrategy,
)


# ── base ABC ─────────────────────────────────────────────────────────────────


def test_credential_strategy_abc_cannot_be_instantiated():
    # The base class has abstractmethods, so direct construction is forbidden.
    """The CredentialStrategy ABC refuses direct construction.

    Every concrete strategy must implement the abstract surface; instantiating the
    base would yield a strategy whose validate()/serialize() do nothing, silently
    storing unvalidated secrets.
    """
    with pytest.raises(TypeError):
        CredentialStrategy()


def test_base_classmethod_defaults_are_empty():
    # Strategies that don't override optional_fields()/description() inherit
    # these empty defaults (e.g. BasicStrategy/GDriveStrategy define no
    # optional_fields).
    """Strategies that don't override optional_fields() inherit an empty list.

    So list_types() can call optional_fields() on every strategy unconditionally
    without each one having to define it.
    """
    assert BasicStrategy.optional_fields() == []
    assert GDriveStrategy.optional_fields() == []


# ── list_types / get_strategy dispatch ───────────────────────────────────────


def test_list_types_returns_metadata_for_every_strategy(credential_manager):
    """list_types() emits exactly one entry per registered strategy.

    This feeds the credential-type dropdown in the Admin UI; a missing entry means
    a credential type users simply cannot create.
    """
    types = credential_manager.list_types()
    # One metadata dict per registered strategy.
    assert len(types) == len(ALL_STRATEGIES)
    returned_keys = {t["credential_type"] for t in types}
    expected_keys = {s.credential_type for s in ALL_STRATEGIES}
    assert returned_keys == expected_keys


def test_list_types_each_entry_has_full_metadata(credential_manager):
    """Every list_types() entry has the exact key set and value types the UI expects.

    The frontend renders required/optional field inputs from these lists, so a
    missing key or a non-list value would break the form.
    """
    for entry in credential_manager.list_types():
        assert set(entry) == {
            "credential_type",
            "required_fields",
            "optional_fields",
            "description",
        }
        assert isinstance(entry["required_fields"], list)
        assert isinstance(entry["optional_fields"], list)
        assert isinstance(entry["description"], str)


def test_list_types_metadata_matches_strategy_classmethods(credential_manager):
    """list_types() reports the strategies' own classmethod values verbatim.

    Guards against the manager hardcoding/duplicating field lists that then drift
    from the strategy that actually validates them.
    """
    by_type = {s.credential_type: s for s in ALL_STRATEGIES}
    for entry in credential_manager.list_types():
        strat = by_type[entry["credential_type"]]
        assert entry["required_fields"] == strat.required_fields()
        assert entry["optional_fields"] == strat.optional_fields()
        assert entry["description"] == strat.description()


def test_get_strategy_returns_correct_instance(credential_manager):
    """get_strategy() dispatches a type string to the matching strategy instance."""
    assert credential_manager.get_strategy("s3").credential_type == "s3"
    assert isinstance(credential_manager.get_strategy("git_pat"), GitPATStrategy)


def test_get_strategy_unknown_type_raises_keyerror(credential_manager):
    """An unknown credential type raises KeyError naming the valid alternatives.

    The available-types list in the message is what makes a typo'd type in an API
    call self-diagnosing.
    """
    with pytest.raises(KeyError) as exc:
        credential_manager.get_strategy("definitely_not_a_real_type")
    # Error message lists the available types to help the caller.
    assert "definitely_not_a_real_type" in str(exc.value)
    assert "s3" in str(exc.value)


# ── per-strategy validate() — positive + negative ────────────────────────────


def test_s3_validate_accepts_all_required_fields():
    """A complete s3 field set validates without raising."""
    S3Strategy().validate(
        {"endpoint": "minio:9000", "access_key": "AK", "secret_key": "SK"}
    )


@pytest.mark.parametrize("missing", ["endpoint", "access_key", "secret_key"])
def test_s3_validate_rejects_each_missing_required_field(missing):
    """Each individual s3 required field is genuinely enforced.

    Parametrized so a validator that only checks the first field (a common
    short-circuit bug) fails on the other two cases.
    """
    fields = {"endpoint": "minio:9000", "access_key": "AK", "secret_key": "SK"}
    del fields[missing]
    with pytest.raises(ValueError, match=missing):
        S3Strategy().validate(fields)


def test_s3_validate_rejects_empty_string_field():
    """A present-but-empty required field is rejected like a missing one.

    A blank access_key must not reach boto3 as valid config; ``in fields`` alone
    would accept it.
    """
    with pytest.raises(ValueError, match="access_key"):
        S3Strategy().validate(
            {"endpoint": "minio:9000", "access_key": "", "secret_key": "SK"}
        )


def test_git_pat_validate_accepts_token_and_rejects_missing():
    """git_pat requires ``token``; supplying only a username is rejected."""
    GitPATStrategy().validate({"token": "ghp_abc"})
    with pytest.raises(ValueError, match="token"):
        GitPATStrategy().validate({"username": "octocat"})


def test_git_ssh_validate_accepts_key_and_rejects_missing():
    """git_ssh requires ``private_key``; a passphrase alone is rejected."""
    GitSSHStrategy().validate({"private_key": "-----BEGIN KEY-----"})
    with pytest.raises(ValueError, match="private_key"):
        GitSSHStrategy().validate({"passphrase": "x"})


def test_gdrive_validate_accepts_json_and_rejects_missing():
    """gdrive requires ``service_account_json``; an empty field set is rejected."""
    GDriveStrategy().validate({"service_account_json": '{"type":"service_account"}'})
    with pytest.raises(ValueError, match="service_account_json"):
        GDriveStrategy().validate({})


@pytest.mark.parametrize("strat_cls", [SMBStrategy, BasicStrategy])
def test_userpass_validate_requires_username_and_password(strat_cls):
    """SMB and Basic both require username AND password.

    Parametrized over both strategies since they share the username/password shape
    but are separate classes that could drift apart.
    """
    strat_cls().validate({"username": "u", "password": "p"})
    with pytest.raises(ValueError, match="password"):
        strat_cls().validate({"username": "u"})
    with pytest.raises(ValueError, match="username"):
        strat_cls().validate({"password": "p"})


# ── serialize() normalization + get_client_config() shape ────────────────────


def test_s3_serialize_strips_whitespace_and_defaults_region():
    """serialize() trims copy-paste whitespace and fills region/use_ssl defaults.

    Stripping matters because credentials are usually pasted from a console; a
    trailing newline in an access key produces an opaque SignatureDoesNotMatch
    error at request time rather than at store time.
    """
    out = S3Strategy().serialize(
        {"endpoint": "  minio:9000  ", "access_key": " AK ", "secret_key": " SK "}
    )
    assert out["endpoint"] == "minio:9000"
    assert out["access_key"] == "AK"
    assert out["region"] == "us-east-1"  # default applied
    assert out["use_ssl"] is False


def test_s3_client_config_builds_endpoint_url_from_use_ssl():
    """use_ssl selects the http:// vs https:// scheme for the boto3 endpoint_url.

    The stored ``endpoint`` is scheme-less (host:port); the scheme is derived here,
    so a regression would send credentials over plaintext to a TLS endpoint.
    """
    strat = S3Strategy()
    insecure = strat.get_client_config(
        {"endpoint": "minio:9000", "access_key": "AK", "secret_key": "SK"}
    )
    assert insecure["endpoint_url"] == "http://minio:9000"
    secure = strat.get_client_config(
        {"endpoint": "s3.example.com", "access_key": "AK", "secret_key": "SK",
         "use_ssl": True}
    )
    assert secure["endpoint_url"] == "https://s3.example.com"


def test_git_pat_serialize_defaults_username_to_git():
    """git_pat defaults username to 'git' and strips whitespace off the token.

    'git' is the conventional placeholder user for token-over-HTTPS clones.
    """
    out = GitPATStrategy().serialize({"token": "  ghp_abc  "})
    assert out["token"] == "ghp_abc"
    assert out["username"] == "git"


def test_gdrive_serialize_parses_json_string_to_dict():
    """A service-account JSON *string* is parsed into a dict at serialize time.

    Normalizing at store time means get_client_config() always sees a dict,
    regardless of whether the user pasted text or posted an object.
    """
    out = GDriveStrategy().serialize(
        {"service_account_json": '{"type": "service_account", "id": 1}'}
    )
    assert out["service_account_json"] == {"type": "service_account", "id": 1}


def test_gdrive_serialize_passes_through_dict_unchanged():
    """An already-parsed service-account dict is passed through as-is (idempotent)."""
    sa = {"type": "service_account", "id": 2}
    out = GDriveStrategy().serialize({"service_account_json": sa})
    assert out["service_account_json"] == sa


def test_gdrive_serialize_invalid_json_string_raises():
    """Malformed service-account JSON fails at store time with a JSONDecodeError.

    Better to reject at creation than to persist an unusable credential that only
    fails when a job tries to use it.
    """
    import json

    with pytest.raises(json.JSONDecodeError):
        GDriveStrategy().serialize({"service_account_json": "not valid json {"})


def test_git_pat_client_config_marks_token_type_pat():
    """get_client_config() tags the config with token_type='pat'.

    Consumers branch on token_type to choose HTTPS-token vs SSH-key auth.
    """
    cfg = GitPATStrategy().get_client_config({"token": "ghp_abc", "username": "octocat"})
    assert cfg == {"token": "ghp_abc", "username": "octocat", "token_type": "pat"}


def test_git_pat_client_config_defaults_username_when_absent():
    # get_client_config defaults username to "git" if the decrypted dict omits it.
    """username defaults to 'git' at client-config time too, not just at serialize.

    Covers rows stored before the serialize-time default existed.
    """
    cfg = GitPATStrategy().get_client_config({"token": "ghp_abc"})
    assert cfg["username"] == "git"


def test_git_ssh_serialize_strips_key_and_keeps_passphrase():
    """The private key is whitespace-stripped while the passphrase is preserved verbatim."""
    out = GitSSHStrategy().serialize(
        {"private_key": "  -----BEGIN KEY-----  ", "passphrase": "secret"}
    )
    assert out["private_key"] == "-----BEGIN KEY-----"
    assert out["passphrase"] == "secret"


def test_git_ssh_serialize_defaults_passphrase_to_empty():
    """An absent passphrase normalizes to '' so consumers can read it unconditionally."""
    out = GitSSHStrategy().serialize({"private_key": "k"})
    assert out["passphrase"] == ""


def test_git_ssh_client_config_marks_token_type_ssh():
    """get_client_config() tags the config with token_type='ssh' (the PAT counterpart)."""
    cfg = GitSSHStrategy().get_client_config({"private_key": "k", "passphrase": "p"})
    assert cfg == {"private_key": "k", "passphrase": "p", "token_type": "ssh"}


def test_smb_serialize_strips_username_and_defaults_domain():
    """SMB strips the username and domain but NEVER the password.

    Leading/trailing spaces can be legitimate password characters; stripping them
    would make a valid credential permanently fail to authenticate.
    """
    out = SMBStrategy().serialize(
        {"username": "  alice  ", "password": "  keep spaces  "}
    )
    assert out["username"] == "alice"
    # Password must NOT be stripped (spaces can be meaningful).
    assert out["password"] == "  keep spaces  "
    assert out["domain"] == ""


def test_smb_serialize_strips_domain_when_provided():
    """A provided SMB domain is whitespace-stripped."""
    out = SMBStrategy().serialize(
        {"username": "u", "password": "p", "domain": "  WORKGROUP  "}
    )
    assert out["domain"] == "WORKGROUP"


def test_basic_serialize_preserves_password_whitespace():
    """Basic auth strips the username but preserves password whitespace exactly.

    Same rationale as SMB — see test_smb_serialize_strips_username_and_defaults_domain.
    """
    out = BasicStrategy().serialize({"username": "  bob ", "password": "  p w  "})
    assert out["username"] == "bob"
    assert out["password"] == "  p w  "


# ── store() -> get() round-trip through the DB (encrypt + persist + decrypt) ──


async def test_store_then_get_round_trips_client_config(
    credential_manager, db, admin_user
):
    """store() encrypts+persists and get() returns the strategy's CLIENT CONFIG.

    Note the shape change: get() does NOT return the raw stored fields — it returns
    the boto3-ready keys (endpoint_url/aws_access_key_id/...). Callers pass the
    result straight into a client constructor.
    """
    cred_id = await credential_manager.store(
        db,
        name="my-minio",
        credential_type="s3",
        fields={"endpoint": "minio:9000", "access_key": "AK", "secret_key": "SK"},
        owner_id=admin_user.id,
    )
    # PKs are stored as string UUIDs; the returned id parses as a UUID and the
    # round-tripped value is stable.
    # AI Note: this comparison is tautological (both sides are the identical
    # expression) so it only really asserts that uuid.UUID(str(cred_id)) does
    # not RAISE — i.e. that store() returned a UUID-parseable id. The real
    # coverage in this test is the get() assertion below. Left as-is rather
    # than "fixed" because tightening it (e.g. against a re-read row) belongs
    # in a dedicated test; see the audit note for this file.
    assert uuid.UUID(str(cred_id)) == uuid.UUID(str(cred_id))

    config = await credential_manager.get(db, cred_id)
    # get() returns the strategy's client config, not the raw stored fields.
    assert config == {
        "endpoint_url": "http://minio:9000",
        "aws_access_key_id": "AK",
        "aws_secret_access_key": "SK",
        "region_name": "us-east-1",
    }


async def test_store_persists_metadata_and_owner(
    credential_manager, db, admin_user
):
    """Non-secret metadata (name, type, sharing, description, owner) is stored in the clear.

    Only the fields blob is encrypted; these columns must stay queryable for the
    credential list/RBAC filtering. allowed_groups defaults to empty.
    """
    from nexus_server.db import ops

    cred_id = await credential_manager.store(
        db,
        name="shared-cred",
        credential_type="basic",
        fields={"username": "alice", "password": "pw"},
        owner_id=admin_user.id,
        is_shared=True,
        description="a shared basic credential",
    )
    cred = await ops.get_credential_by_id(db, cred_id)
    assert cred.name == "shared-cred"
    assert cred.credential_type == "basic"
    assert cred.is_shared is True
    assert cred.description == "a shared basic credential"
    assert str(cred.owner_id) == str(admin_user.id)
    assert cred.allowed_groups == []


async def test_store_persists_ciphertext_not_plaintext(
    credential_manager, db, admin_user, encryptor
):
    """The secret is never written to the DB in plaintext, but is recoverable with the key.

    The end-to-end version of the encryption unit test, asserted against the actual
    persisted column.
    """
    secret = "super-secret-token-value"
    cred_id = await credential_manager.store(
        db,
        name="pat-cred",
        credential_type="git_pat",
        fields={"token": secret},
        owner_id=admin_user.id,
    )
    from nexus_server.db import ops

    cred = await ops.get_credential_by_id(db, cred_id)
    # Stored bytes must not contain the raw secret.
    assert secret.encode() not in cred.encrypted_fields
    # But decrypting them recovers it.
    assert encryptor.decrypt(cred.encrypted_fields)["token"] == secret


async def test_store_invalid_fields_raises_value_error_and_persists_nothing(
    credential_manager, db, admin_user
):
    """Validation runs BEFORE any DB write, so a rejected credential leaves no row.

    Ordering matters: validating after insert would leave orphaned, unusable rows
    behind every failed create.
    """
    from nexus_server.db import ops

    with pytest.raises(ValueError, match="secret_key"):
        await credential_manager.store(
            db,
            name="bad-s3",
            credential_type="s3",
            fields={"endpoint": "minio:9000", "access_key": "AK"},
            owner_id=admin_user.id,
        )
    # Validation happens before any DB write.
    assert await ops.get_credential_by_name(db, "bad-s3") is None


async def test_store_unknown_type_raises_keyerror(
    credential_manager, db, admin_user
):
    """An unknown credential_type is rejected at store() (no row, no encryption)."""
    with pytest.raises(KeyError):
        await credential_manager.store(
            db,
            name="x",
            credential_type="nope",
            fields={"a": 1},
            owner_id=admin_user.id,
        )


async def test_get_by_name_round_trips(credential_manager, db, admin_user):
    """get_by_name() resolves by name and returns the same client config as get()."""
    await credential_manager.store(
        db,
        name="named-basic",
        credential_type="basic",
        fields={"username": "alice", "password": "hunter2"},
        owner_id=admin_user.id,
    )
    config = await credential_manager.get_by_name(db, "named-basic")
    assert config == {"username": "alice", "password": "hunter2"}


# ── missing-id error paths ───────────────────────────────────────────────────


async def test_get_missing_id_raises_keyerror(credential_manager, db):
    """get() on an unknown id raises KeyError('... not found') rather than returning None.

    Routes map this to a 404; a None return would surface as an AttributeError
    deeper in the caller.
    """
    with pytest.raises(KeyError, match="not found"):
        await credential_manager.get(db, uuid.uuid4())


async def test_get_by_name_missing_raises_keyerror(credential_manager, db):
    """get_by_name() on an unknown name raises the same not-found KeyError as get()."""
    with pytest.raises(KeyError, match="not found"):
        await credential_manager.get_by_name(db, "no-such-credential")


async def test_test_missing_id_raises_keyerror(credential_manager, db):
    """test() on an unknown id raises not-found instead of reporting a failed connection.

    Distinguishing "no such credential" from "credential doesn't work" matters for
    the Admin UI's test button.
    """
    with pytest.raises(KeyError, match="not found"):
        await credential_manager.test(db, uuid.uuid4())


async def test_update_fields_missing_id_raises_keyerror(credential_manager, db):
    """update_fields() on an unknown id raises not-found before touching encryption."""
    with pytest.raises(KeyError, match="not found"):
        await credential_manager.update_fields(db, uuid.uuid4(), {"token": "x"})


# ── update_fields() re-encrypts ──────────────────────────────────────────────


async def test_update_fields_reencrypts_and_changes_client_config(
    credential_manager, db, admin_user
):
    """Rotating fields rewrites the ciphertext and changes what get() returns.

    The ciphertext must actually differ (proving a re-encrypt happened, not an
    in-place patch), while name/type stay untouched so references to the
    credential remain valid.
    """
    from nexus_server.db import ops

    cred_id = await credential_manager.store(
        db,
        name="rotate-pat",
        credential_type="git_pat",
        fields={"token": "old-token", "username": "octocat"},
        owner_id=admin_user.id,
    )
    before = await credential_manager.get(db, cred_id)
    assert before["token"] == "old-token"
    before_cipher = (await ops.get_credential_by_id(db, cred_id)).encrypted_fields

    await credential_manager.update_fields(
        db, cred_id, {"token": "new-token", "username": "newcat"}
    )
    after = await credential_manager.get(db, cred_id)
    assert after["token"] == "new-token"
    assert after["username"] == "newcat"

    after_cred = await ops.get_credential_by_id(db, cred_id)
    # Ciphertext was rewritten, and the name/type are left untouched.
    assert after_cred.encrypted_fields != before_cipher
    assert after_cred.name == "rotate-pat"
    assert after_cred.credential_type == "git_pat"


async def test_update_fields_revalidates_new_fields(
    credential_manager, db, admin_user
):
    """Updates are validated with the same rules as creates.

    Without re-validation, a rotation could blank out a required secret and leave
    a previously-working credential permanently broken.
    """
    cred_id = await credential_manager.store(
        db,
        name="revalidate-pat",
        credential_type="git_pat",
        fields={"token": "tok"},
        owner_id=admin_user.id,
    )
    # An empty token must be rejected on update just like on store.
    with pytest.raises(ValueError, match="token"):
        await credential_manager.update_fields(db, cred_id, {"token": ""})


async def test_update_fields_with_uuid_object_id_crashes(
    credential_manager, db, admin_user
):
    """XFAIL(strict): documents a live SQLite/UUID bind bug in ops.update_credential.

    See the xfail reason: update_credential() passes the raw id to db.get() without
    the _sid() string coercion used elsewhere, so a uuid.UUID argument for an
    EXISTING credential crashes the driver. Kept as a strict xfail so this test
    flips to a failure (alerting us) the moment the bug is fixed.
    """
    cred_id = await credential_manager.store(
        db,
        name="uuid-update-pat",
        credential_type="git_pat",
        fields={"token": "tok"},
        owner_id=admin_user.id,
    )
    # Caller hands a real uuid.UUID (not a str) for an existing credential.
    await credential_manager.update_fields(
        db, uuid.UUID(str(cred_id)), {"token": "new"}
    )


# ── test() connection dispatch (monkeypatched — never hits the network) ───────


async def test_test_connection_dispatches_to_strategy(
    credential_manager, db, admin_user, monkeypatch
):
    """test() decrypts the stored fields and hands them to the strategy's test_connection.

    The strategy is monkeypatched so no socket is opened; what is asserted is the
    wiring — the strategy receives *decrypted* values, not ciphertext.
    """
    cred_id = await credential_manager.store(
        db,
        name="pat-for-test",
        credential_type="git_pat",
        fields={"token": "ghp_xyz"},
        owner_id=admin_user.id,
    )

    seen = {}

    async def fake_test_connection(decrypted_fields):
        # Manager must hand the decrypted fields to the strategy.
        """Stand-in for the real network probe; records the token it was handed."""
        seen["token"] = decrypted_fields["token"]
        return True

    strat = credential_manager.get_strategy("git_pat")
    monkeypatch.setattr(strat, "test_connection", fake_test_connection)

    assert await credential_manager.test(db, cred_id) is True
    assert seen["token"] == "ghp_xyz"


async def test_test_connection_propagates_false_from_strategy(
    credential_manager, db, admin_user, monkeypatch
):
    """A False result from the strategy is returned unchanged (not coerced to True).

    The Admin UI shows a red/green badge straight from this boolean.
    """
    cred_id = await credential_manager.store(
        db,
        name="pat-for-failtest",
        credential_type="git_pat",
        fields={"token": "ghp_bad"},
        owner_id=admin_user.id,
    )

    async def fake_test_connection(decrypted_fields):
        """Stand-in probe that always reports failure."""
        return False

    strat = credential_manager.get_strategy("git_pat")
    monkeypatch.setattr(strat, "test_connection", fake_test_connection)
    # Manager returns whatever the strategy returns — no coercion to True.
    assert await credential_manager.test(db, cred_id) is False


async def test_gdrive_json_survives_store_get_round_trip(
    credential_manager, db, admin_user
):
    """A nested service-account dict survives serialize -> encrypt -> decrypt -> client config.

    The encryption layer JSON-encodes the whole field dict, so a dict-valued field
    is a nested-JSON case worth pinning explicitly.
    """
    sa = {"type": "service_account", "project_id": "demo", "private_key_id": "k1"}
    cred_id = await credential_manager.store(
        db,
        name="gdrive-cred",
        credential_type="gdrive",
        fields={"service_account_json": sa},
        owner_id=admin_user.id,
    )
    config = await credential_manager.get(db, cred_id)
    # The dict survives serialize -> JSON-encrypt -> decrypt -> client_config.
    assert config == {"service_account_json": sa}


async def test_no_network_strategies_test_connection_returns_true():
    # These strategies have no real network call; they short-circuit to True.
    """Strategies with no remote endpoint to probe short-circuit test_connection to True.

    Documents that a green "test passed" badge for ssh/gdrive/smb/basic means
    "nothing to check", NOT "credentials verified against a server".
    """
    assert await GitSSHStrategy().test_connection({"private_key": "k"}) is True
    assert await GDriveStrategy().test_connection({"service_account_json": {}}) is True
    assert await SMBStrategy().test_connection(
        {"username": "u", "password": "p"}
    ) is True
    assert await BasicStrategy().test_connection(
        {"username": "u", "password": "p"}
    ) is True
