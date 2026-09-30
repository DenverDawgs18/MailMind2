"""
Email forwarding: users forward mail to a private MailMind address instead of
granting Gmail API access (whose restricted scopes need Google's security
assessment).

Mail is received and sent through Mailgun. Configuration (all env):

    INBOUND_ADDRESS_TEMPLATE      e.g. "{token}@mail.yourdomain.com" (a Mailgun
                                  receiving domain; MX records point at Mailgun)
    MAILGUN_WEBHOOK_SIGNING_KEY   verifies inbound route forwards
    MAILGUN_API_KEY               sending API key
    MAILGUN_DOMAIN                sending domain, e.g. "mail.yourdomain.com"
    MAILGUN_API_BASE              optional; "https://api.eu.mailgun.net" for EU accounts
    DIGEST_FROM_ADDRESS           e.g. "MailMind <list@mail.yourdomain.com>"

Mailgun route: match_recipient(".*@mail.yourdomain.com") ->
forward("https://mailmind.dev/inbound/mailgun"), stop().

Forwarding is enabled once the inbound settings exist; digests for users with
no directly-connected inbox go out through Mailgun once the sending settings
exist.
"""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from email.utils import getaddresses, parseaddr

import requests

from functions.get_emails import _normalize_whitespace, html_converter, is_digest

logger = logging.getLogger(__name__)

MAX_BODY_CHARS = 20000
# Mailgun retries failed forwards for up to 8 hours; accept signatures within
# a generous window and rely on token de-duplication against replays.
SIGNATURE_MAX_AGE_SECONDS = 12 * 3600
_TOKEN_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # lowercase, no look-alikes

GMAIL_FORWARDING_SENDER = "forwarding-noreply@google.com"


def _env(name):
    return (os.getenv(name) or "").strip()


def forwarding_enabled() -> bool:
    return bool(_env("INBOUND_ADDRESS_TEMPLATE") and _env("MAILGUN_WEBHOOK_SIGNING_KEY"))


def outbound_enabled() -> bool:
    return bool(_env("MAILGUN_API_KEY") and _env("MAILGUN_DOMAIN") and _env("DIGEST_FROM_ADDRESS"))


def new_token() -> str:
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(12))


def address_for(token: str) -> str:
    return _env("INBOUND_ADDRESS_TEMPLATE").replace("{token}", token)


def _address_pattern():
    template = _env("INBOUND_ADDRESS_TEMPLATE").lower()
    if "{token}" not in template:
        return None
    before, after = (re.escape(part) for part in template.split("{token}", 1))
    return re.compile(f"^{before}([a-z0-9]+){after}$")


def is_our_address(address: str) -> bool:
    pattern = _address_pattern()
    return bool(pattern and pattern.match((address or "").strip().lower()))


# ---------------------------------------------------------------------------
# Verifying Mailgun's signature
# ---------------------------------------------------------------------------

def verify_mailgun_signature(timestamp: str, token: str, signature: str, now: float | None = None) -> bool:
    key = _env("MAILGUN_WEBHOOK_SIGNING_KEY")
    if not (key and timestamp and token and signature):
        return False
    try:
        age = (now or time.time()) - int(timestamp)
    except ValueError:
        return False
    if abs(age) > SIGNATURE_MAX_AGE_SECONDS:
        return False
    expected = hmac.new(key.encode(), f"{timestamp}{token}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Normalising Mailgun's inbound form
# ---------------------------------------------------------------------------

def from_mailgun(form) -> dict:
    """Turn Mailgun's route-forward form into the provider-neutral shape used below."""
    try:
        header_pairs = json.loads(form.get("message-headers") or "[]")
    except ValueError:
        header_pairs = []
    headers = {}
    for pair in header_pairs:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            headers.setdefault(str(pair[0]).lower(), str(pair[1]))

    name, addr = parseaddr(form.get("from") or "")
    recipients = [a for _, a in getaddresses([form.get("recipient") or "", form.get("To") or ""]) if a]
    return {
        "recipients": recipients,
        "sender_email": (addr or form.get("sender") or "").strip(),
        "sender_name": name.strip(),
        "subject": form.get("subject") or "",
        "text": form.get("body-plain") or "",
        "html": form.get("body-html") or "",
        "headers": headers,
    }


def extract_token(msg) -> str | None:
    """Which user this message is for, from the recipient address."""
    pattern = _address_pattern()
    if pattern is None:
        return None
    for address in msg["recipients"]:
        m = pattern.match(address.strip().lower())
        if m:
            return m.group(1)
    return None


def source_address(msg, fallback: str) -> str:
    """
    The inbox the mail was forwarded from. Gmail auto-forwarding adds
    X-Forwarded-For: <user's address> <our address>.
    """
    headers = msg["headers"]
    for candidate in (headers.get("x-forwarded-for", "") or "").split():
        _, addr = parseaddr(candidate)
        if "@" in addr and not is_our_address(addr):
            return addr.lower()
    for _, addr in getaddresses([headers.get("delivered-to", "")]):
        if "@" in addr and not is_our_address(addr):
            return addr.lower()
    return fallback


def gmail_confirmation(msg):
    """
    If this is Gmail's forwarding confirmation, return (code, from_address).
    Subject: "(#123456789) Gmail Forwarding Confirmation - Receive Mail from you@gmail.com"
    """
    if GMAIL_FORWARDING_SENDER not in msg["sender_email"].lower():
        return None
    subject = msg["subject"]
    code = re.search(r"\(#(\d{6,12})\)", subject) or re.search(r"[Cc]onfirmation code:\s*(\d{6,12})", msg["text"])
    if not code:
        return None
    who = re.search(r"from\s+([^\s]+@[^\s]+)", subject)
    return code.group(1), (who.group(1).strip(".,") if who else "")


def parse_message(msg, fallback_source: str) -> dict | None:
    """Prepare a forwarded email for the queue; None if it should be ignored."""
    headers = msg["headers"]
    subject = msg["subject"]
    if is_digest(subject, {"X-MailMind-Digest": headers.get("x-mailmind-digest")}):
        return None

    body = msg["text"]
    if not body.strip() and msg["html"]:
        body = html_converter.handle(msg["html"])
    body = _normalize_whitespace(body)[:MAX_BODY_CHARS]
    if not body:
        return None

    name, addr = msg["sender_name"], msg["sender_email"]
    sender = f"{name} <{addr}>" if name else addr

    return {
        "source_email": source_address(msg, fallback_source),
        "sender": sender[:512],
        "subject": subject,
        "body": body,
        "message_id": (headers.get("message-id") or "")[:998] or None,
    }


# ---------------------------------------------------------------------------
# Sending digests through Mailgun
# ---------------------------------------------------------------------------

def send_digest_email(to_address: str, subject: str, html_body: str, digest_header: str) -> bool:
    if not outbound_enabled():
        logger.error("Mailgun sending isn't configured; can't deliver digest")
        return False
    base = (_env("MAILGUN_API_BASE") or "https://api.mailgun.net").rstrip("/")
    try:
        resp = requests.post(
            f"{base}/v3/{_env('MAILGUN_DOMAIN')}/messages",
            auth=("api", _env("MAILGUN_API_KEY")),
            data={
                "from": _env("DIGEST_FROM_ADDRESS"),
                "to": to_address,
                "subject": subject,
                "html": html_body,
                f"h:{digest_header}": "1",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        logger.error("Mailgun send failed: %s", exc)
        return False
    if resp.status_code != 200:
        logger.error("Mailgun send returned %s", resp.status_code)
        return False
    return True
