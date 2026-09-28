"""
Email forwarding: users forward mail to a private MailMind address instead of
granting Gmail API access (whose restricted scopes need Google's security
assessment).

Inbound mail arrives via a Postmark inbound webhook. Configuration (all env):

    INBOUND_ADDRESS_TEMPLATE  e.g. "a1b2c3+{token}@inbound.postmarkapp.com"
                              or "{token}@in.yourdomain.com" with a custom domain
    INBOUND_WEBHOOK_USER      basic-auth credentials Postmark sends with each
    INBOUND_WEBHOOK_PASSWORD  webhook (put them in the webhook URL)
    POSTMARK_SERVER_TOKEN     outbound API token, for sending digests
    DIGEST_FROM_ADDRESS       e.g. "MailMind <list@yourdomain.com>" (verified in Postmark)

Forwarding is enabled once the inbound settings exist; digests for users with
no directly-connected inbox go out through Postmark once the outbound
settings exist.
"""
import hmac
import logging
import os
import re
import secrets
from email.utils import getaddresses, parseaddr

import requests

from functions.get_emails import _normalize_whitespace, html_converter, is_digest

logger = logging.getLogger(__name__)

POSTMARK_SEND_URL = "https://api.postmarkapp.com/email"
MAX_BODY_CHARS = 20000
_TOKEN_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # lowercase, no look-alikes

GMAIL_FORWARDING_SENDER = "forwarding-noreply@google.com"


def _env(name):
    return (os.getenv(name) or "").strip()


def forwarding_enabled() -> bool:
    return bool(_env("INBOUND_ADDRESS_TEMPLATE") and _env("INBOUND_WEBHOOK_PASSWORD"))


def outbound_enabled() -> bool:
    return bool(_env("POSTMARK_SERVER_TOKEN") and _env("DIGEST_FROM_ADDRESS"))


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


def check_webhook_auth(authorization) -> bool:
    """Constant-time check of the webhook's HTTP basic-auth credentials."""
    expected_user = _env("INBOUND_WEBHOOK_USER")
    expected_pass = _env("INBOUND_WEBHOOK_PASSWORD")
    if not authorization or not expected_pass:
        return False
    user_ok = hmac.compare_digest(authorization.username or "", expected_user)
    pass_ok = hmac.compare_digest(authorization.password or "", expected_pass)
    return user_ok and pass_ok


# ---------------------------------------------------------------------------
# Parsing Postmark's inbound JSON
# ---------------------------------------------------------------------------

def _headers(payload) -> dict:
    return {h.get("Name", "").lower(): h.get("Value", "") for h in payload.get("Headers") or []}


def extract_token(payload) -> str | None:
    """Which user this message is for: Postmark's MailboxHash, else a recipient match."""
    mailbox_hash = (payload.get("MailboxHash") or "").strip().lower()
    if mailbox_hash and re.fullmatch(r"[a-z0-9]+", mailbox_hash):
        return mailbox_hash

    pattern = _address_pattern()
    if pattern is None:
        return None
    recipients = [payload.get("OriginalRecipient") or ""]
    for field in ("ToFull", "CcFull", "BccFull"):
        recipients += [r.get("Email", "") for r in payload.get(field) or []]
    for address in recipients:
        m = pattern.match(address.strip().lower())
        if m:
            return m.group(1)
    return None


def source_address(payload, fallback: str) -> str:
    """
    The inbox the mail was forwarded from. Gmail auto-forwarding adds
    X-Forwarded-For: <user's address> <our address>.
    """
    headers = _headers(payload)
    for candidate in (headers.get("x-forwarded-for", "") or "").split():
        _, addr = parseaddr(candidate)
        if "@" in addr and not is_our_address(addr):
            return addr.lower()
    for _, addr in getaddresses([headers.get("delivered-to", "")]):
        if "@" in addr and not is_our_address(addr):
            return addr.lower()
    return fallback


def gmail_confirmation(payload):
    """
    If this is Gmail's forwarding confirmation, return (code, from_address).
    Subject: "(#123456789) Gmail Forwarding Confirmation - Receive Mail from you@gmail.com"
    """
    sender = (payload.get("FromFull") or {}).get("Email") or payload.get("From") or ""
    if GMAIL_FORWARDING_SENDER not in sender.lower():
        return None
    subject = payload.get("Subject") or ""
    body = payload.get("TextBody") or ""
    code = re.search(r"\(#(\d{6,12})\)", subject) or re.search(r"[Cc]onfirmation code:\s*(\d{6,12})", body)
    if not code:
        return None
    who = re.search(r"from\s+([^\s]+@[^\s]+)", subject)
    return code.group(1), (who.group(1).strip(".,") if who else "")


def parse_message(payload, fallback_source: str) -> dict | None:
    """Normalise a forwarded email; None if it should be ignored."""
    headers = _headers(payload)
    subject = payload.get("Subject") or ""
    if is_digest(subject, {"X-MailMind-Digest": headers.get("x-mailmind-digest")}):
        return None

    body = payload.get("TextBody") or ""
    if not body.strip() and payload.get("HtmlBody"):
        body = html_converter.handle(payload["HtmlBody"])
    body = _normalize_whitespace(body)[:MAX_BODY_CHARS]
    if not body:
        return None

    from_full = payload.get("FromFull") or {}
    name, addr = from_full.get("Name") or "", from_full.get("Email") or payload.get("From") or ""
    sender = f"{name} <{addr}>" if name else addr

    return {
        "source_email": source_address(payload, fallback_source),
        "sender": sender[:512],
        "subject": subject,
        "body": body,
        "message_id": (headers.get("message-id") or payload.get("MessageID") or "")[:998] or None,
    }


# ---------------------------------------------------------------------------
# Sending digests through Postmark
# ---------------------------------------------------------------------------

def send_via_postmark(to_address: str, subject: str, html_body: str, digest_header: str) -> bool:
    if not outbound_enabled():
        logger.error("Postmark outbound isn't configured; can't deliver digest")
        return False
    try:
        resp = requests.post(
            POSTMARK_SEND_URL,
            headers={"X-Postmark-Server-Token": _env("POSTMARK_SERVER_TOKEN"), "Accept": "application/json"},
            json={
                "From": _env("DIGEST_FROM_ADDRESS"),
                "To": to_address,
                "Subject": subject,
                "HtmlBody": html_body,
                "MessageStream": "outbound",
                "Headers": [{"Name": digest_header, "Value": "1"}],
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        logger.error("Postmark send failed: %s", exc)
        return False
    if resp.status_code != 200:
        logger.error("Postmark send returned %s", resp.status_code)
        return False
    return True
