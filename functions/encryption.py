import os

from cryptography.fernet import Fernet

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

fernet = Fernet(_ENCRYPTION_KEY.encode())


def encrypt_token(token: str) -> str:
    return fernet.encrypt(token.encode()).decode()


def decrypt_token(token: str) -> str:
    return fernet.decrypt(token.encode()).decode()
