"""
Encryption for OAuth refresh tokens at rest.

``ENCRYPTION_KEY`` holds one Fernet key, or several separated by commas for
rotation: the first key encrypts, every key is tried when decrypting. To rotate,
prepend a new key, deploy, let tokens re-encrypt as they refresh (or call
``rotate_token``), then drop the old key.
"""
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from functions.production import production

PRODUCTION = production()

if not PRODUCTION:
    from dotenv import load_dotenv
    load_dotenv()


_ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
if not _ENCRYPTION_KEY:
    raise RuntimeError(
        "ENCRYPTION_KEY is not set. Generate one with "
        "`python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` "
        "and export it before starting the app."
    )

fernet = MultiFernet([Fernet(k.strip().encode()) for k in _ENCRYPTION_KEY.split(",") if k.strip()])


class TokenDecryptionError(Exception):
    """The stored token can't be decrypted with any configured key."""


def encrypt_token(token: str) -> str:
    return fernet.encrypt(token.encode()).decode()


def decrypt_token(token: str) -> str:
    try:
        return fernet.decrypt(token.encode()).decode()
    except (InvalidToken, AttributeError) as exc:
        raise TokenDecryptionError("stored OAuth token could not be decrypted") from exc


def rotate_token(token: str) -> str:
    """Re-encrypt a stored token with the current primary key."""
    return fernet.rotate(token.encode()).decode()
