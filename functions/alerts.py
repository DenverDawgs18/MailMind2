"""
Operator alerts: email the people in ADMIN_EMAILS when something needs the
owner's attention (e.g. the OpenAI account is out of credits), at most once
per alert kind per ALERT_INTERVAL_SECONDS.

Alerts go out through Mailgun (functions/forwarding.send_digest_email) and
carry the MailMind digest header, so MailMind never reads its own alerts as
mail that needs action.
"""
import html
import logging
import os
import time

from functions.get_emails import DIGEST_HEADER

logger = logging.getLogger(__name__)

ALERT_INTERVAL_SECONDS = 6 * 3600

# Fallback de-duplication when Redis is unavailable (per process).
_last_sent: dict = {}


def admin_emails() -> list:
    return [e.strip() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()]


def _claim(kind: str) -> bool:
    """True if this alert kind hasn't gone out within the interval (and claims it)."""
    try:
        from app import _redis_client
    except Exception:
        _redis_client = None
    if _redis_client is not None:
        try:
            return bool(_redis_client.set(f"mailmind:alert:{kind}", "1", nx=True, ex=ALERT_INTERVAL_SECONDS))
        except Exception:
            logger.warning("Redis unavailable for alert de-duplication", exc_info=True)
    now = time.time()
    if now - _last_sent.get(kind, 0) < ALERT_INTERVAL_SECONDS:
        return False
    _last_sent[kind] = now
    return True


def alert_admins(kind: str, subject: str, message: str) -> bool:
    """Email every admin about `kind`, unless already alerted recently. Never raises."""
    recipients = admin_emails()
    if not recipients:
        logger.error("ALERT (%s) with no ADMIN_EMAILS set: %s", kind, message)
        return False
    if not _claim(kind):
        return False
    from functions.forwarding import send_digest_email

    body = f'<p style="white-space:pre-wrap">{html.escape(message)}</p>'
    sent = False
    for to in recipients:
        try:
            sent = send_digest_email(to, f"[MailMind alert] {subject}", body, DIGEST_HEADER) or sent
        except Exception:
            logger.exception("Couldn't send alert to an admin")
    logger.error("ALERT (%s) %s: %s", kind, "sent" if sent else "NOT sent", message)
    return sent


def model_unavailable(exc: Exception) -> None:
    """Alert about an OpenAI failure that stops lists from being made."""
    text = str(exc)
    if "insufficient_quota" in text or "credit_balance" in text:
        alert_admins(
            "openai-credits", "OpenAI is out of credits",
            "MailMind's OpenAI account has run out of credits, so lists can't be made and none "
            "are being sent. Add credits at https://platform.openai.com/settings/organization/billing/ "
            "(turning on auto recharge there prevents this). Lists due in the last 2 hours "
            f"go out automatically once credits are back.\n\nError: {text[:500]}",
        )
    else:
        alert_admins(
            "openai-down", "OpenAI calls are failing",
            "MailMind can't reach OpenAI, so lists aren't being made. They'll retry every "
            f"15 minutes for up to 2 hours.\n\nError: {text[:500]}",
        )
