"""
OAuth token lifecycle for linked inboxes: scopes, refresh, and revocation.

Only refresh tokens are stored (Fernet-encrypted, see functions.encryption).
Access tokens are minted on demand and never persisted.

``refresh(account)`` returns a fresh access token or raises TokenRefreshError.
When the provider says the grant is dead (revoked, expired, password reset) or
the stored token can't be decrypted, the account is flagged ``needs_reauth`` so
the settings page can ask the user to reconnect and the scheduler stops trying.
"""
import logging
import os

import requests

from app import db
from functions.encryption import TokenDecryptionError, decrypt_token, encrypt_token
from functions.production import production

logger = logging.getLogger(__name__)

if not production():
    from dotenv import load_dotenv
    load_dotenv()

_TIMEOUT = 15

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_SCOPES = [
    # IMAP read + SMTP send via XOAUTH2 both require the full Gmail scope.
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

MICROSOFT_AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MICROSOFT_SCOPES = [
    # Least privilege: read mail, send the digest through Graph, read the profile.
    "https://graph.microsoft.com/Mail.Read",
    "https://graph.microsoft.com/Mail.Send",
    "https://graph.microsoft.com/User.Read",
    "offline_access",
    "openid",
    "profile",
    "email",
]

# Token-endpoint error codes that mean "this grant will never work again".
_DEAD_GRANT_ERRORS = {"invalid_grant", "unauthorized_client", "invalid_client", "interaction_required"}


class TokenRefreshError(Exception):
    def __init__(self, message: str, reauth_required: bool = False):
        super().__init__(message)
        self.reauth_required = reauth_required


def _mark_needs_reauth(account, reason: str) -> None:
    logger.warning("Account %s (%s) needs reauth: %s", account.id, account.provider, reason)
    if not account.needs_reauth:
        account.needs_reauth = True
        db.session.commit()


def refresh(account) -> str:
    """Exchange the stored refresh token for a new access token."""
    if account.needs_reauth:
        raise TokenRefreshError("account needs to be reconnected", reauth_required=True)

    try:
        refresh_token = decrypt_token(account.oauth_token)
    except TokenDecryptionError:
        _mark_needs_reauth(account, "stored token undecryptable")
        raise TokenRefreshError("stored token undecryptable", reauth_required=True)

    provider = (account.provider or "google").lower()
    if provider == "microsoft":
        data = {
            "client_id": os.getenv("MICROSOFT_CLIENT_ID"),
            "client_secret": os.getenv("MICROSOFT_CLIENT_SECRET"),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": " ".join(MICROSOFT_SCOPES),
        }
        url = MICROSOFT_TOKEN_URL
    else:
        data = {
            "client_id": os.getenv("GOOGLE_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        url = GOOGLE_TOKEN_URL

    try:
        response = requests.post(url, data=data, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise TokenRefreshError(f"token endpoint unreachable: {exc.__class__.__name__}") from exc

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    if response.status_code != 200 or "access_token" not in payload:
        error = payload.get("error", f"http_{response.status_code}")
        if error in _DEAD_GRANT_ERRORS:
            _mark_needs_reauth(account, error)
            raise TokenRefreshError(error, reauth_required=True)
        raise TokenRefreshError(f"refresh failed: {error}")

    # Microsoft rotates refresh tokens on every use; Google usually doesn't.
    new_refresh = payload.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        account.oauth_token = encrypt_token(new_refresh)
        db.session.commit()

    return payload["access_token"]


def revoke(account) -> bool:
    """
    Best-effort revocation when an inbox is unlinked or the account deleted.
    Google supports revoking a refresh token directly. Microsoft has no
    per-token revoke endpoint for this flow, so we just drop the token; the
    user can remove the app at https://account.live.com/consent/Manage or
    https://myapps.microsoft.com.
    """
    if (account.provider or "").lower() != "google":
        return False
    try:
        token = decrypt_token(account.oauth_token)
    except TokenDecryptionError:
        return False
    try:
        resp = requests.post(GOOGLE_REVOKE_URL, data={"token": token}, timeout=_TIMEOUT)
        return resp.status_code == 200
    except requests.RequestException:
        logger.warning("Google token revocation failed for account %s", account.id)
        return False
