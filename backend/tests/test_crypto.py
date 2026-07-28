"""Encryption tests."""

from __future__ import annotations

import json
import os

import pytest
from cryptography.fernet import Fernet

from app.core import crypto
from app.core.config import get_settings


def test_round_trip():
    assert crypto.decrypt(crypto.encrypt("access-sandbox-abc")) == "access-sandbox-abc"


def test_ciphertext_does_not_contain_the_plaintext():
    """The point of the exercise, stated as a test."""
    secret = "access-sandbox-super-secret-value"
    assert secret not in crypto.encrypt(secret)


def test_encrypting_twice_produces_different_ciphertext():
    """Fernet includes a random IV.

    Identical plaintexts therefore encrypt to different ciphertexts, so an
    attacker holding the database cannot tell which users share a value.
    Deterministic encryption would leak exactly that.
    """
    assert crypto.encrypt("same-value") != crypto.encrypt("same-value")


def test_tampered_ciphertext_is_rejected():
    """Fernet is *authenticated* encryption.

    Flipping a byte causes a loud failure rather than silently returning
    garbage that later code would treat as a real access token.
    """
    ciphertext = crypto.encrypt("access-sandbox-abc")
    tampered = ciphertext[:-4] + ("AAAA" if not ciphertext.endswith("AAAA") else "BBBB")

    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(tampered)


def test_ciphertext_from_an_unknown_key_is_rejected():
    stranger = Fernet(Fernet.generate_key())
    foreign = stranger.encrypt(b"not ours").decode()

    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(foreign)


def test_error_message_leaks_neither_key_nor_ciphertext():
    """Error messages end up in logs, which have weaker access control than
    the database does."""
    stranger = Fernet(Fernet.generate_key())
    foreign = stranger.encrypt(b"not ours").decode()

    with pytest.raises(crypto.DecryptionError) as exc_info:
        crypto.decrypt(foreign)

    message = str(exc_info.value)
    assert foreign not in message
    for key in get_settings().ENCRYPTION_KEYS:
        assert key not in message


def test_empty_values_are_refused():
    with pytest.raises(crypto.EncryptionError):
        crypto.encrypt("")


def test_key_rotation_keeps_old_data_readable():
    """Proves rotation works BEFORE it is ever needed in anger.

    Write under the old key, prepend a new one, and confirm the old data still
    decrypts and can be re-encrypted under the new key. Discovering this does
    not work while responding to a leaked key is the wrong time.
    """
    original_keys = get_settings().ENCRYPTION_KEYS
    ciphertext = crypto.encrypt("token-written-under-the-old-key")

    new_key = Fernet.generate_key().decode()
    os.environ["ENCRYPTION_KEYS"] = json.dumps([new_key, *original_keys])
    get_settings.cache_clear()
    crypto._cipher.cache_clear()

    try:
        # Old ciphertext still readable, because the old key is still listed.
        assert crypto.decrypt(ciphertext) == "token-written-under-the-old-key"

        # Re-encrypt under the new primary key.
        rotated = crypto.rotate(ciphertext)
        assert rotated != ciphertext
        assert crypto.decrypt(rotated) == "token-written-under-the-old-key"

        # Once only the new key remains, the rotated value still works...
        os.environ["ENCRYPTION_KEYS"] = json.dumps([new_key])
        get_settings.cache_clear()
        crypto._cipher.cache_clear()
        assert crypto.decrypt(rotated) == "token-written-under-the-old-key"

        # ...and the un-rotated original no longer does, which is exactly why
        # you must re-encrypt everything before dropping the old key.
        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt(ciphertext)
    finally:
        os.environ["ENCRYPTION_KEYS"] = json.dumps(original_keys)
        get_settings.cache_clear()
        crypto._cipher.cache_clear()
