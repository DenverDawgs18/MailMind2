import os


def production() -> bool:
    """
    True when the app is running in production.

    Prefer the explicit MAILMIND_PRODUCTION env var; fall back to FLASK_ENV.
    """
    explicit = os.getenv("MAILMIND_PRODUCTION")
    if explicit is not None:
        return explicit.strip().lower() in ("1", "true", "yes", "on")
    return os.getenv("FLASK_ENV", "").strip().lower() == "production"


def simple() -> bool:
    return True
