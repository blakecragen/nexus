"""Tests for FieldEncryptor (Fernet-based credential field encryption)."""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet, InvalidToken

from nexus_server.services.credentials.encryption import FieldEncryptor


# --- round trips ---------------------------------------------------------


def test_round_trip_simple_dict(encryptor: FieldEncryptor):
    """encrypt() -> decrypt() is lossless for a flat string dict.

    This is the baseline contract every stored credential depends on: if this
    breaks, every credential in the DB becomes unreadable.
    """
    fields = {"username": "alice", "password": "s3cret"}
    assert encryptor.decrypt(encryptor.encrypt(fields)) == fields


def test_round_trip_nested_dict(encryptor: FieldEncryptor):
    """Nested structures keep their JSON types through the round trip.

    FieldEncryptor serializes via json.dumps, so ints/bools/None/lists must come
    back as themselves rather than stringified. Guards against a regression where
    a naive str() based serializer would turn ``count: 3`` into ``"3"`` and break
    callers (e.g. S3 port numbers, use_ssl flags).
    """
    fields = {
        "auth": {"token": "abc", "scopes": ["read", "write"]},
        "meta": {"count": 3, "enabled": True, "extra": None},
    }
    out = encryptor.decrypt(encryptor.encrypt(fields))
    assert out == fields
    # JSON round-trip must preserve scalar types, not stringify them.
    assert out["meta"]["count"] == 3 and isinstance(out["meta"]["count"], int)
    assert out["meta"]["enabled"] is True
    assert out["meta"]["extra"] is None
    assert out["auth"]["scopes"] == ["read", "write"]


def test_round_trip_unicode(encryptor: FieldEncryptor):
    """Non-ASCII field values survive encryption.

    json.dumps + .encode()/.decode() must use a consistent UTF-8 path; a latin-1
    or ASCII-only encode would raise or mangle these values.
    """
    fields = {"name": "naïve café", "emoji": "🔐", "cjk": "密码"}
    assert encryptor.decrypt(encryptor.encrypt(fields)) == fields


def test_round_trip_empty_dict(encryptor: FieldEncryptor):
    """An empty field dict is a valid payload (encrypts and decrypts to {}).

    Credential types with no fields, or a cleared credential, must not blow up.
    """
    assert encryptor.decrypt(encryptor.encrypt({})) == {}


# --- ciphertext properties ----------------------------------------------


def test_ciphertext_differs_from_plaintext(encryptor: FieldEncryptor):
    """The secret never appears verbatim in the stored bytes.

    This is the actual security property the DB column relies on: a dump of
    ``credentials.encrypted_fields`` must not leak passwords to anyone who can
    read the table but not the Fernet key.
    """
    fields = {"password": "p@ssw0rd"}
    ciphertext = encryptor.encrypt(fields)
    # The secret value must not appear verbatim in the ciphertext.
    assert b"p@ssw0rd" not in ciphertext
    assert ciphertext != json.dumps(fields).encode()


def test_ciphertext_differs_across_calls(encryptor: FieldEncryptor):
    """Fernet's random IV makes repeated encryptions of the same input differ.

    Deterministic ciphertext would let an attacker with read access to the table
    correlate credentials that share a password. Both ciphertexts must still
    decrypt to the same plaintext.
    """
    fields = {"password": "p@ssw0rd"}
    # Fernet embeds a random IV, so two encryptions of the same input differ.
    c1 = encryptor.encrypt(fields)
    c2 = encryptor.encrypt(fields)
    assert c1 != c2
    # ...yet both decrypt back to the same plaintext.
    assert encryptor.decrypt(c1) == encryptor.decrypt(c2) == fields


def test_encrypt_returns_bytes(encryptor: FieldEncryptor):
    """encrypt() returns bytes, matching the LargeBinary DB column type.

    Returning str would silently break the SQLAlchemy bind for the
    ``encrypted_fields`` column on some drivers.
    """
    assert isinstance(encryptor.encrypt({"a": 1}), bytes)


# --- key generation ------------------------------------------------------


def test_generate_key_returns_usable_str_key():
    """generate_key() produces a str key that actually constructs a working encryptor.

    The key is meant to be pasted into CREDENTIAL_ENCRYPTION_KEY (an env var, i.e.
    a string), so returning raw bytes here would force every caller to decode.
    """
    key = FieldEncryptor.generate_key()
    assert isinstance(key, str)
    # A freshly generated key must construct a working encryptor.
    enc = FieldEncryptor(key)
    fields = {"k": "v"}
    assert enc.decrypt(enc.encrypt(fields)) == fields


def test_generate_key_unique():
    """Two generate_key() calls never collide.

    A constant/seeded key would mean every deployment shares one encryption key.
    """
    assert FieldEncryptor.generate_key() != FieldEncryptor.generate_key()


# --- key types -----------------------------------------------------------


def test_accepts_str_and_bytes_keys_equivalently():
    """str and bytes forms of the same key are interchangeable.

    The key arrives as a str from env config but as bytes in some tests/tools;
    both must produce interoperable encryptors or credentials encrypted by the
    server would be undecryptable by a CLI utility (and vice versa).
    """
    key_str = FieldEncryptor.generate_key()
    enc_str = FieldEncryptor(key_str)
    enc_bytes = FieldEncryptor(key_str.encode())
    fields = {"shared": "value"}
    # A ciphertext produced with the str-keyed encryptor must decrypt with the
    # bytes-keyed encryptor built from the same key, and vice versa.
    assert enc_bytes.decrypt(enc_str.encrypt(fields)) == fields
    assert enc_str.decrypt(enc_bytes.encrypt(fields)) == fields


# --- error paths ---------------------------------------------------------


def test_decrypt_with_different_key_raises():
    """Decrypting with the wrong key raises InvalidToken rather than returning junk.

    This is the failure mode after a key rotation: it must be a loud exception the
    caller can catch, not silent garbage handed to a storage backend.
    """
    enc_a = FieldEncryptor(FieldEncryptor.generate_key())
    enc_b = FieldEncryptor(FieldEncryptor.generate_key())
    ciphertext = enc_a.encrypt({"secret": "x"})
    with pytest.raises(InvalidToken):
        enc_b.decrypt(ciphertext)


def test_decrypt_garbage_raises(encryptor: FieldEncryptor):
    """Arbitrary non-Fernet bytes raise InvalidToken.

    Protects against a corrupted/truncated DB column being interpreted as valid.
    """
    with pytest.raises(InvalidToken):
        encryptor.decrypt(b"not-a-valid-fernet-token")


def test_decrypt_empty_bytes_raises(encryptor: FieldEncryptor):
    # An empty token is not a valid Fernet token.
    """An empty ciphertext is rejected instead of decoding to an empty dict.

    A NULL/empty column must surface as an error, not as "credential with no
    fields", which would produce a confusing downstream failure.
    """
    with pytest.raises(InvalidToken):
        encryptor.decrypt(b"")


def test_encrypt_non_json_serializable_raises(encryptor: FieldEncryptor):
    # encrypt() json.dumps the fields first; a non-serializable value (set)
    # must surface as a TypeError rather than producing a bogus token.
    """Non-JSON-serializable field values fail fast with TypeError.

    The caller should learn at store() time that a set/object can't be persisted,
    rather than writing a partially-formed token to the DB.
    """
    with pytest.raises(TypeError):
        encryptor.encrypt({"bad": {1, 2, 3}})


def test_invalid_key_rejected_by_constructor():
    # Fernet keys must be 32 url-safe base64-encoded bytes; junk is rejected
    # by Fernet's constructor with a ValueError.
    """A malformed key is rejected at construction, not at first use.

    Misconfiguring CREDENTIAL_ENCRYPTION_KEY must fail at app startup (when the
    encryptor is built) instead of at the first credential read in production.
    """
    with pytest.raises(ValueError):
        FieldEncryptor("too-short")


def test_uses_fernet_format():
    # Sanity: ciphertext is a token decodable by a raw Fernet with the same key.
    """Ciphertext is a plain Fernet token over the JSON payload.

    Pins the on-disk format so an out-of-band recovery tool (or a future
    reimplementation) can decrypt existing rows with stock ``cryptography``.
    """
    key = FieldEncryptor.generate_key()
    enc = FieldEncryptor(key)
    token = enc.encrypt({"a": "b"})
    raw = Fernet(key.encode())
    assert json.loads(raw.decrypt(token).decode()) == {"a": "b"}
