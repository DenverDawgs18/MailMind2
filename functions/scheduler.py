"""
Daily digest scheduler.

Every 15 minutes we scan all subscribed users, check if the current time
matches one of their configured send times (±7.5 min), and if so:

1. Fetch the last 24 hours of email from each linked account.
2. Ask the fine-tuned model for the action items, if any.
3. Render an HTML digest with per-item calendar deep-links.
4. Send it to the user from their own primary inbox (Gmail SMTP XOAUTH2, or
   Microsoft Graph sendMail).

Inboxes whose OAuth grant has died are skipped and called out in the digest
so the user knows to reconnect them.

There is no in-app rendering path; this module is the only consumer of the
email-fetch and action-extraction functions.
"""
import base64
import email.utils
import logging
import re
import smtplib
import socket
import ssl
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

import pytz
import requests
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from jinja2 import Environment
from sqlalchemy import and_

from app import app, db
from functions.get_emails import DIGEST_HEADER, DIGEST_SUBJECT_PREFIX, get_emails
from functions.get_one_action import get_an_action
from functions.refresh_token import TokenRefreshError, refresh
from functions.forwarding import outbound_enabled, send_via_postmark
from models import (
    DIGEST_RETENTION_DAYS, INBOUND_MAX_AGE_HOURS, Digest, DigestItem, InboundEmail, Master, PendingItem,
)

logger = logging.getLogger(__name__)

scheduler = None
flask_app = app

try:
    from app import _redis_client  # type: ignore
except Exception:
    _redis_client = None

_LOCK_TTL_SECONDS = 60 * 30


class _LocalLock:
    """Fallback single-process lock when Redis isn't available."""

    def __init__(self):
        import threading
        self._held: dict = {}
        self._mutex = threading.Lock()

    def acquire(self, key: str) -> bool:
        with self._mutex:
            if key in self._held:
                return False
            self._held[key] = True
            return True

    def release(self, key: str) -> None:
        with self._mutex:
            self._held.pop(key, None)


_local_lock = _LocalLock()


def _acquire_send_lock(user_id: int) -> bool:
    key = f"mailmind:send-lock:{user_id}"
    if _redis_client is not None:
        try:
            return bool(_redis_client.set(key, "1", nx=True, ex=_LOCK_TTL_SECONDS))
        except Exception as exc:
            logger.warning("Redis lock failure, falling back to local: %s", exc)
    return _local_lock.acquire(key)


def _release_send_lock(user_id: int) -> None:
    key = f"mailmind:send-lock:{user_id}"
    if _redis_client is not None:
        try:
            _redis_client.delete(key)
            return
        except Exception as exc:
            logger.warning("Redis lock release failed: %s", exc)
    _local_lock.release(key)


# ---------------------------------------------------------------------------
# Calendar deep-links
# ---------------------------------------------------------------------------

_CALENDAR_KEYWORDS = ["meeting", "conference call", "calendar", "appointment", "call", "schedule", "event"]


def _is_calendar_worthy(action_text: str) -> bool:
    return any(k in action_text.lower() for k in _CALENDAR_KEYWORDS)


def _calendar_link(provider: str, title: str) -> str:
    """
    Return a URL that opens the user's calendar with a pre-filled event.

    Neither Google nor Outlook require auth for these deep-links — the user is
    already signed in in their browser.
    """
    title_q = urllib.parse.quote(title[:200])
    if provider and provider.lower() == "microsoft":
        return (
            "https://outlook.live.com/calendar/0/deeplink/compose"
            f"?subject={title_q}&path=/calendar/action/compose&rru=addevent"
        )
    return f"https://calendar.google.com/calendar/render?action=TEMPLATE&text={title_q}"


# ---------------------------------------------------------------------------
# Digest HTML
# ---------------------------------------------------------------------------

# Email-client-safe markup: tables and inline styles only (no SVG, no flexbox).
_DIGEST_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="color-scheme" content="light only" />
<title>Your MailMind list</title>
</head>
<body style="margin:0;padding:0;background:#f5f5f7;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Helvetica,Arial,sans-serif;color:#1d1d1f;">
  <div style="display:none;max-height:0;overflow:hidden;">{{ action_count }} thing{{ 's' if action_count != 1 else '' }} need{{ '' if action_count != 1 else 's' }} you today.</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f7;">
    <tr><td align="center" style="padding:32px 16px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;">
        <tr><td style="padding:0 8px 20px;">
          <table role="presentation" cellpadding="0" cellspacing="0"><tr>
            <td style="width:30px;height:30px;border:3px solid #1d1d1f;border-radius:9px;text-align:center;font-size:18px;line-height:24px;font-weight:700;color:#ff6a2b;">&#10003;</td>
            <td style="padding-left:10px;font-size:19px;font-weight:700;letter-spacing:-0.4px;">MailMind</td>
          </tr></table>
        </td></tr>

        <tr><td style="background:#ffffff;border-radius:24px;padding:36px 32px 28px;border:1px solid #e8e8ed;">
          <p style="margin:0;font-size:13px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:#d9480f;">{{ current_date }}</p>
          <h1 style="margin:8px 0 6px;font-size:34px;line-height:1.1;letter-spacing:-1.2px;font-weight:800;color:#1d1d1f;">Good morning.</h1>
          <p style="margin:0 0 24px;font-size:17px;color:#6e6e73;">{{ action_count }} thing{{ 's' if action_count != 1 else '' }} need{{ '' if action_count != 1 else 's' }} you today.</p>

          {% for notice in notices %}
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 16px;">
            <tr><td style="background:#fff1ea;border-radius:14px;padding:14px 16px;font-size:15px;color:#d9480f;">
              We couldn't read <strong>{{ notice }}</strong>. <a href="{{ site_url }}/settings" style="color:#d9480f;font-weight:700;">Reconnect it</a> so it's included tomorrow.
            </td></tr>
          </table>
          {% endfor %}

          {% for group in groups %}
            {% if groups|length > 1 %}
            <p style="margin:22px 0 4px;font-size:13px;font-weight:700;color:#86868b;">{{ group.account_email }}</p>
            {% endif %}
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
            {% for item in group['items'] %}
              <tr><td style="padding:14px 0;border-bottom:1px solid #f0f0f2;">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
                  <td valign="top" style="width:30px;padding-top:2px;">
                    <div style="width:18px;height:18px;border:2px solid #c7c7cc;border-radius:50%;"></div>
                  </td>
                  <td valign="top">
                    <p style="margin:0;font-size:17px;line-height:1.4;font-weight:600;color:#1d1d1f;">{{ item.action }}</p>
                    <p style="margin:4px 0 0;font-size:14px;line-height:1.4;color:#86868b;">{{ item['from'] }}{% if item.subject %} &middot; {{ item.subject }}{% endif %}</p>
                    {% if item.calendar_url %}
                    <p style="margin:10px 0 0;"><a href="{{ item.calendar_url }}" style="display:inline-block;padding:7px 14px;background:#1d1d1f;color:#ffffff;text-decoration:none;border-radius:999px;font-size:13px;font-weight:600;">Add to calendar</a></p>
                    {% endif %}
                  </td>
                </tr></table>
              </td></tr>
            {% endfor %}
            </table>
          {% endfor %}
        </td></tr>

        <tr><td style="padding:22px 8px;text-align:center;font-size:13px;line-height:1.6;color:#86868b;">
          Sent to {{ primary_email }} by <a href="{{ site_url }}" style="color:#1d1d1f;text-decoration:none;font-weight:600;">MailMind</a><br>
          <a href="{{ site_url }}/settings" style="color:#86868b;">Delivery settings</a> &middot;
          <a href="{{ site_url }}/contact" style="color:#86868b;">Contact</a>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
"""


def _render_digest(groups: List[dict], notices: List[str], primary_email: str,
                   site_url: str, current_date: str) -> str:
    env = Environment(autoescape=True)
    template = env.from_string(_DIGEST_TEMPLATE)
    action_count = sum(len(g["items"]) for g in groups)
    return template.render(
        groups=groups,
        notices=notices,
        primary_email=primary_email,
        current_date=current_date,
        action_count=action_count,
        site_url=site_url,
    )


_NO_ACTION = {"no action", "no action.", "no action required", "no action required.", "none", ""}
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


def _split_actions(raw: str) -> List[str]:
    """The model may answer with several bullet points; one item per bullet."""
    items = []
    for line in (raw or "").splitlines():
        text = _BULLET.sub("", line).strip()
        if text and text.lower() not in _NO_ACTION:
            items.append(text)
    return items


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

_GRAPH_SENDMAIL_URL = "https://graph.microsoft.com/v1.0/me/sendMail"


def _clean_address(addr: str) -> Optional[str]:
    if not addr:
        return None
    m = re.search(r'<(.*?)>', addr)
    return m.group(1) if m else addr


def _send_gmail(sender_email: str, access_token: str, subject: str, html_body: str) -> bool:
    """Gmail: SMTP with XOAUTH2 (the https://mail.google.com/ scope allows it)."""
    clean_to = _clean_address(sender_email)
    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = clean_to
    msg['Subject'] = Header(subject, 'utf-8')
    msg['Date'] = email.utils.formatdate(localtime=True)
    msg[DIGEST_HEADER] = "1"
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))

    auth_string = f"user={sender_email}\x01auth=Bearer {access_token}\x01\x01"
    auth_b64 = base64.b64encode(auth_string.encode()).decode()

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            code, _ = server.docmd("AUTH", "XOAUTH2 " + auth_b64)
            if code != 235:
                logger.error("Gmail XOAUTH2 rejected (code %s)", code)
                return False
            server.send_message(msg)
        return True
    except (smtplib.SMTPException, socket.error, ssl.SSLError) as exc:
        logger.exception("Gmail SMTP send failed: %s", exc)
        return False


def _send_graph(sender_email: str, access_token: str, subject: str, html_body: str) -> bool:
    """
    Microsoft: Graph sendMail. Graph access tokens aren't valid for
    smtp.office365.com (that needs a separate Outlook SMTP scope), so the
    digest goes out through the Mail.Send permission we already hold.
    """
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": _clean_address(sender_email)}}],
            "internetMessageHeaders": [{"name": DIGEST_HEADER, "value": "1"}],
        },
        "saveToSentItems": False,
    }
    try:
        resp = requests.post(_GRAPH_SENDMAIL_URL, json=payload,
                             headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    except requests.RequestException as exc:
        logger.error("Graph sendMail failed: %s", exc)
        return False
    if resp.status_code != 202:
        logger.error("Graph sendMail returned %s", resp.status_code)
        return False
    return True


def _send_html_email(sender_email: str, access_token: str, provider: str,
                     subject: str, html_body: str) -> bool:
    if (provider or "").lower() == "microsoft":
        return _send_graph(sender_email, access_token, subject, html_body)
    return _send_gmail(sender_email, access_token, subject, html_body)


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------

def init_scheduler(app):
    global scheduler, flask_app
    flask_app = app

    executors = {'default': ThreadPoolExecutor(max_workers=5)}
    job_defaults = {'coalesce': False, 'max_instances': 1, 'misfire_grace_time': 300}

    scheduler = BackgroundScheduler(executors=executors, job_defaults=job_defaults, timezone='UTC')
    scheduler.add_job(
        func=check_and_send_emails,
        trigger=CronTrigger(minute='0,15,30,45'),
        id='email_summary_checker',
        name='Check for users to send email summaries',
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Email scheduler started")

    import atexit
    atexit.register(shutdown_scheduler)
    return scheduler


def shutdown_scheduler():
    if scheduler:
        scheduler.shutdown()
        logger.info("Email scheduler stopped")


def get_scheduler_status():
    if scheduler:
        return {
            "scheduler_running": scheduler.running,
            "jobs": [
                {"id": j.id, "name": j.name, "next_run": str(j.next_run_time)}
                for j in scheduler.get_jobs()
            ],
        }
    return {"scheduler_running": False, "jobs": []}


# ---------------------------------------------------------------------------
# Time matching
# ---------------------------------------------------------------------------

def _parse_user_times(time_string: str) -> List[str]:
    if not time_string:
        return []
    return [t.strip() for t in time_string.split(',') if t.strip()]


def _convert_to_24hour(time_str: str) -> Optional[str]:
    for fmt in ('%I:%M %p', '%I:%M%p'):
        try:
            return datetime.strptime(time_str, fmt).time().strftime('%H:%M')
        except ValueError:
            continue
    logger.error("Could not parse time string: %s", time_str)
    return None


def _is_time_match(user_time: str, current_time: datetime, timezone_str: str) -> bool:
    try:
        user_tz = pytz.timezone(timezone_str)
        current_local = current_time.astimezone(user_tz)
        parsed = _convert_to_24hour(user_time)
        if not parsed:
            return False
        hour, minute = map(int, parsed.split(':'))
        target = current_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return abs((current_local - target).total_seconds()) <= 450
    except Exception as exc:
        logger.error("time match failed for %s / %s: %s", user_time, timezone_str, exc)
        return False


def _users_to_process() -> List[Master]:
    now = datetime.now(pytz.UTC)
    users = Master.query.filter(
        and_(
            Master.timezone.isnot(None),
            Master.time.isnot(None),
            Master.subscribed.is_(True),
        )
    ).all()

    result = []
    for user in users:
        if not user.timezone or not user.time:
            continue
        for user_time in _parse_user_times(user.time):
            if _is_time_match(user_time, now, user.timezone):
                result.append(user)
                logger.info("User %s queued for %s (%s)", user.id, user_time, user.timezone)
                break
    return result


# ---------------------------------------------------------------------------
# Digest generation + send per user
# ---------------------------------------------------------------------------

def _store_digest(user: Master, groups: List[dict]) -> Digest:
    """Save the list for the website and drop lists past the retention window."""
    digest = Digest(master_id=user.id)
    for group in groups:
        for item in group["items"]:
            digest.items.append(DigestItem(
                account_email=group["account_email"],
                action=item["action"],
                sender=(item.get("from") or "")[:512],
                subject=item.get("subject") or "",
                calendar_url=item.get("calendar_url"),
            ))
    db.session.add(digest)

    cutoff = datetime.now(timezone.utc) - timedelta(days=DIGEST_RETENTION_DAYS)
    for old in Digest.query.filter(Digest.master_id == user.id, Digest.created_at < cutoff).all():
        db.session.delete(old)
    db.session.commit()
    return digest


def send_email_summary_for_user(user: Master, site_url: str) -> Dict[str, Any]:
    if not _acquire_send_lock(user.id):
        return {"success": True, "message": "lock held", "user": user.id}

    try:
        tokens: Dict[int, str] = {}
        groups: List[dict] = []
        notices: List[str] = []

        for account in user.email_accounts:
            try:
                tokens[account.id] = refresh(account)
            except TokenRefreshError as exc:
                logger.warning("Skipping account %s: %s", account.id, exc)
                if exc.reauth_required:
                    notices.append(account.email)
                continue
            try:
                emails = get_emails(account.provider, account.email, tokens[account.id])
            except Exception as exc:
                logger.exception("Fetch failed for account %s: %s", account.id, exc)
                continue

            items = []
            for msg in reversed(emails):
                body = msg.get("body", "")
                if not body:
                    continue
                try:
                    actions = _split_actions(get_an_action(body))
                except Exception as exc:
                    logger.exception("get_an_action failed: %s", exc)
                    continue
                for action in actions:
                    items.append({
                        "action": action,
                        "from": msg.get("from", ""),
                        "subject": msg.get("subject", ""),
                        "calendar_url": (
                            _calendar_link(account.provider, action)
                            if _is_calendar_worthy(action) else None
                        ),
                    })
            if items:
                groups.append({"account_email": account.email, "items": items})

        # Items pulled from forwarded mail since the last list.
        pending = PendingItem.query.filter_by(master_id=user.id).order_by(PendingItem.id).all()
        for item in pending:
            group = next((g for g in groups if g["account_email"] == item.source_email), None)
            if group is None:
                group = {"account_email": item.source_email, "items": []}
                groups.append(group)
            group["items"].append({"action": item.action, "from": item.sender or "",
                                   "subject": item.subject or "", "calendar_url": item.calendar_url})
        for item in pending:
            db.session.delete(item)

        digest = _store_digest(user, groups)

        if not groups and not notices:
            return {"success": True, "message": "no action items", "user": user.id}

        # Send from the user's primary inbox when it's connected directly (so
        # the list arrives from themselves); otherwise, e.g. forwarding-only
        # users, send through Postmark from MailMind's address.
        primary = next((a for a in user.email_accounts if a.email == user.primary_email), None)
        if primary is None or primary.id not in tokens:
            primary = next((a for a in user.email_accounts if a.id in tokens), None)
        if primary is None and not outbound_enabled():
            return {"success": False, "message": "no way to deliver the list", "user": user.id}

        local_now = datetime.now(pytz.timezone(user.timezone or "UTC"))
        current_date = local_now.strftime('%A, %B %-d')
        count = sum(len(g["items"]) for g in groups)
        subject = (f"{DIGEST_SUBJECT_PREFIX}: {count} thing{'s' if count != 1 else ''} for "
                   f"{local_now.strftime('%A')}")
        html = _render_digest(groups, notices, user.primary_email, site_url, current_date)
        if primary is not None:
            sent = _send_html_email(primary.email, tokens[primary.id], primary.provider, subject, html)
        else:
            sent = send_via_postmark(user.primary_email, subject, html, DIGEST_HEADER)
        if sent:
            digest.delivered = True
            db.session.commit()
            return {"success": True, "message": f"sent {count} items", "user": user.id}
        return {"success": False, "message": "send failed", "user": user.id}
    except Exception as exc:
        logger.exception("digest failed for user %s: %s", user.id, exc)
        return {"success": False, "message": "unexpected error", "user": user.id}
    finally:
        _release_send_lock(user.id)


def process_inbound_queue(batch: int = 300) -> int:
    """
    Turn queued forwarded emails into pending action items and delete the
    email bodies. Runs every tick so bodies are held for minutes, not hours.
    Rows that keep failing (e.g. the model is down) are dropped after
    INBOUND_MAX_AGE_HOURS.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=INBOUND_MAX_AGE_HOURS)
    processed = 0
    for row in InboundEmail.query.order_by(InboundEmail.id).limit(batch).all():
        received = row.received_at if row.received_at.tzinfo else row.received_at.replace(tzinfo=timezone.utc)
        try:
            actions = _split_actions(get_an_action(row.body))
        except Exception as exc:
            if received < cutoff:
                logger.warning("Dropping inbound email %s after repeated failures", row.id)
                db.session.delete(row)
                db.session.commit()
            else:
                logger.warning("Action extraction failed for inbound email %s: %s", row.id, exc)
            continue
        for action in actions:
            db.session.add(PendingItem(
                master_id=row.master_id, source_email=row.source_email, action=action,
                sender=row.sender, subject=row.subject,
                calendar_url=_calendar_link("google", action) if _is_calendar_worthy(action) else None,
            ))
        db.session.delete(row)
        db.session.commit()
        processed += 1

    # Items for people who never get a list (no schedule, lapsed plan) don't pile up forever.
    stale = datetime.now(timezone.utc) - timedelta(days=DIGEST_RETENTION_DAYS)
    PendingItem.query.filter(PendingItem.created_at < stale).delete()
    db.session.commit()
    return processed


def check_and_send_emails():
    """Called by the cron trigger every 15 minutes."""
    import os
    site_url = os.getenv("DOMAIN", "https://mailmind.fly.dev")

    if not flask_app:
        logger.error("Flask app not available")
        return

    with flask_app.app_context():
        try:
            processed = process_inbound_queue()
            if processed:
                logger.info("Processed %d forwarded emails", processed)
        except Exception as exc:
            logger.exception("Inbound queue processing failed: %s", exc)
            db.session.rollback()

        try:
            users = _users_to_process()
            if not users:
                logger.info("No users to process this tick")
                return
            logger.info("Processing %d users", len(users))
            for user in users:
                try:
                    result = send_email_summary_for_user(user, site_url)
                    logger.info("%s: %s", "OK" if result["success"] else "FAIL", result)
                except Exception as exc:
                    logger.exception("Error processing user %s: %s", user.id, exc)
        except Exception as exc:
            logger.exception("check_and_send_emails failed: %s", exc)
