"""Encryption for secrets held at rest.

===========================================================================
What this protects against
===========================================================================

A Plaid access token grants ongoing read access to somebody's real bank
accounts. If our database is ever exposed -- a leaked backup, a misconfigured
snapshot, a SQL injection, a stolen laptop with a dump on it -- plaintext
tokens would let the attacker read every connected account of every user.

Encrypting them means the database alone is not enough. The attacker also
needs the key, which lives in the environment (and in production, in a
secrets manager), never in the database and never in Git.

This is called *encryption at rest*, and it is the standard control for any
credential you must be able to read back. Note it is NOT the same as password
hashing: a password is hashed one-way because you never need the original.
An access token must be recoverable to be used, so it is encrypted, and that
means key management matters.

===========================================================================
Fernet, and why MultiFernet
===========================================================================

Fernet is a well-reviewed authenticated encryption format from the
`cryptography` library. "Authenticated" means tampering is detected: flipping
a byte of ciphertext causes decryption to fail loudly rather than silently
returning garbage.

`MultiFernet` accepts a LIST of keys. It always encrypts with the first, and
decrypts by trying each in turn. That is what makes key rotation possible
without downtime:

    1. Prepend a new key:  ENCRYPTION_KEYS=["new", "old"]
       New writes use the new key; existing data still decrypts with the old.
    2. Re-encrypt existing rows at your leisure (`rotate()` below).
    3. Drop the old key:   ENCRYPTION_KEYS=["new"]

Designing for rotation on day one costs nothing. Retrofitting it after a key
leaks, while under pressure, is genuinely painful -- and by then you cannot
tell which rows are safe.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class EncryptionError(Exception):
    """Raised when a value cannot be encrypted or decrypted."""


class DecryptionError(EncryptionError):
    """Raised when ciphertext cannot be decrypted with any configured key.

    Usually means one of three things, in decreasing order of likelihood:
      * the key was rotated and the old one was removed too early;
      * ENCRYPTION_KEYS is not set to what it was when the row was written;
      * the stored value was corrupted or tampered with.

    All three are emergencies, because the affected bank connections can no
    longer be used and must be re-linked by the user.
    """


@lru_cache
def _cipher() -> MultiFernet:
    settings = get_settings()

    if not settings.ENCRYPTION_KEYS:
        raise EncryptionError(
            "ENCRYPTION_KEYS is not configured. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"'
        )

    try:
        keys = [Fernet(key.encode()) for key in settings.ENCRYPTION_KEYS]
    except (ValueError, TypeError) as exc:
        raise EncryptionError(
            "ENCRYPTION_KEYS contains an invalid Fernet key. Each key must be "
            "32 url-safe base64-encoded bytes."
        ) from exc

    return MultiFernet(keys)


def encrypt(plaintext: str) -> str:
    """Encrypt a secret for storage. Returns url-safe base64 text."""
    if not plaintext:
        raise EncryptionError("Refusing to encrypt an empty value")

    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a stored secret.

    The error message deliberately contains no fragment of the ciphertext or
    key. Error messages end up in logs, and logs end up in places with much
    weaker access control than the database.
    """
    try:
        return _cipher().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        logger.error("Failed to decrypt a stored secret with any configured key")
        raise DecryptionError(
            "Could not decrypt stored secret. Check ENCRYPTION_KEYS."
        ) from exc


def rotate(ciphertext: str) -> str:
    """Re-encrypt an existing value under the current primary key.

    Used by the key-rotation routine: read every row, `rotate()` it, write it
    back. Fernet does this without ever exposing the plaintext to caller code.
    """
    return _cipher().rotate(ciphertext.encode()).decode()


def generate_key() -> str:
    """Convenience for `python -c 'from app.core.crypto import generate_key; ...'`."""
    return Fernet.generate_key().decode()
