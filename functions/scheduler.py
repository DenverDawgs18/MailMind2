"""
Daily digest scheduler.

Every 15 minutes we scan all subscribed users, check if the current time
matches one of their configured send times (±7.5 min), and if so:

1. Fetch the last 24 hours of email from each linked account.
2. Ask the fine-tuned model for the action item, if any.
3. Render an HTML digest with per-item calendar deep-links.
4. Send it via SMTP XOAUTH2 as the user's own primary account.

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
from datetime import datetime, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

import pytz
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from jinja2 import Environment
from markupsafe import escape
from sqlalchemy import and_

from app import app, db
from functions.get_emails import get_emails
from functions.get_one_action import get_an_action
from functions.refresh_token import refresh
from models import EmailAccount, Master

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

_DIGEST_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>MailMind Daily Summary</title>
</head>
<body style="background:#171524;color:#f0f0f0;font-family:Inter,system-ui,sans-serif;margin:0;padding:20px;">
  <div style="max-width:600px;margin:0 auto;background:linear-gradient(145deg,#514b7a,#5a5488);border:2px solid black;border-radius:20px;padding:30px;box-shadow:4px 4px 10px rgba(0,0,0,0.4);">
    <h1 style="text-align:center;font-size:24px;color:#e6d7a3;margin-bottom:10px;">Your Daily To-Do List</h1>
    <div style="text-align:center;font-size:14px;margin-bottom:30px;color:#f0f0f0;">
      <p>{{ action_count }} action item{{ 's' if action_count != 1 else '' }} - {{ current_date }}</p>
      <p>{{ primary_email }}</p>
    </div>

    {% for item in items %}
      {% if item.divider %}
      <div style="background:#3a3560;border:1px solid rgba(255,255,255,0.1);border-left:4px solid #cc8400;border-radius:12px;padding:15px 20px;margin-bottom:20px;">
        <p style="color:#e6d7a3;font-weight:600;margin:0;">Action items from {{ item.account_email }}</p>
      </div>
      {% else %}
      <div style="background:#3a3560;border:1px solid rgba(255,255,255,0.1);border-left:4px solid #cc8400;border-radius:12px;padding:15px 20px;margin-bottom:20px;">
        <p style="color:#e6d7a3;font-weight:600;margin:4px 0;">- {{ item.action }}</p>
        <p style="color:white;margin:4px 0;font-size:14px;"><strong>From:</strong> {{ item['from'] }}</p>
        <p style="color:white;margin:4px 0;font-size:14px;"><strong>Subject:</strong> {{ item.subject }}</p>
        {% if item.calendar_url %}
        <p style="margin:8px 0 0 0;">
          <a href="{{ item.calendar_url }}"
             style="display:inline-block;padding:6px 12px;background:#cc8400;color:#171524;text-decoration:none;border-radius:6px;font-size:13px;font-weight:600;">
            Add to calendar
          </a>
        </p>
        {% endif %}
      </div>
      {% endif %}
    {% endfor %}

    <div style="text-align:center;font-size:12px;color:#ccc;margin-top:30px;">
      <p>
        Sent by <a href="{{ site_url }}" style="text-decoration:none;color:darkorange;">MailMind</a> -
        <a href="mailto:pautomas55@gmail.com" style="text-decoration:none;color:darkorange;">Support</a> -
        <a href="{{ site_url }}/settings" style="text-decoration:none;color:darkorange;">Settings</a>
      </p>
    </div>
  </div>
</body>
</html>
"""


def _render_digest(items: List[dict], primary_email: str, site_url: str) -> str:
    env = Environment(autoescape=True)
    template = env.from_string(_DIGEST_TEMPLATE)
    action_count = sum(1 for i in items if not i.get('divider'))
    return template.render(
        items=items,
        primary_email=primary_email,
        current_date=datetime.now().strftime('%B %d, %Y'),
        action_count=action_count,
        site_url=site_url,
    )


# ---------------------------------------------------------------------------
# SMTP send (folded in from the old functions/reply.py)
# ---------------------------------------------------------------------------

_SMTP_HOSTS = {
    "google": ("smtp.gmail.com", 587),
    "gmail": ("smtp.gmail.com", 587),
    "microsoft": ("smtp.office365.com", 587),
}


def _clean_address(addr: str) -> Optional[str]:
    if not addr:
        return None
    m = re.search(r'<(.*?)>', addr)
    return m.group(1) if m else addr


def _send_html_email(sender_email: str, access_token: str, provider: str,
                     subject: str, html_body: str) -> bool:
    smtp_host, smtp_port = _SMTP_HOSTS.get((provider or "").lower(), _SMTP_HOSTS["google"])

    clean_to = _clean_address(sender_email)
    if not clean_to:
        logger.error("Invalid sender email %r", sender_email)
        return False

    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = clean_to
    msg['Subject'] = Header(subject, 'utf-8')
    msg['Date'] = email.utils.formatdate(localtime=True)
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))

    auth_string = f"user={sender_email}\x01auth=Bearer {access_token}\x01\x01"
    auth_b64 = base64.b64encode(auth_string.encode()).decode()

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo_or_helo_if_needed()
            if server.has_extn('STARTTLS'):
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            server.docmd("AUTH", "XOAUTH2 " + auth_b64)
            server.send_message(msg)
        return True
    except (smtplib.SMTPException, socket.error, ssl.SSLError) as exc:
        logger.exception("SMTP send failed to %s: %s", clean_to, exc)
        return False


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
    global scheduler
    if scheduler:
        scheduler.shutdown()
        logger.info("Email scheduler stopped")


def get_scheduler_status():
    global scheduler
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
                logger.info("User %s (%s) queued for %s (%s)",
                            user.id, user.primary_email, user_time, user.timezone)
                break
    return result


# ---------------------------------------------------------------------------
# Digest generation + send per user
# ---------------------------------------------------------------------------

def send_email_summary_for_user(user: Master, site_url: str) -> Dict[str, Any]:
    if not _acquire_send_lock(user.id):
        return {"success": True, "message": "lock held", "user": user.primary_email}

    try:
        items: List[dict] = []

        for account in user.email_accounts:
            try:
                access_token = refresh(account)
                emails = get_emails(account.provider, account.email, access_token)
            except Exception as exc:
                logger.exception("Fetch failed for %s / %s: %s",
                                 user.primary_email, account.email, exc)
                continue

            first = True
            for msg in reversed(emails):
                body = msg.get("body", "")
                if not body:
                    continue
                try:
                    action = get_an_action(body)
                except Exception as exc:
                    logger.exception("get_an_action failed: %s", exc)
                    continue

                if not action or action.lower() in ("no action.", "no action", "no action required.", ""):
                    continue

                if first:
                    items.append({"divider": True, "account_email": account.email})
                    first = False

                items.append({
                    "divider": False,
                    "action": action,
                    "from": msg.get("from", ""),
                    "subject": msg.get("subject", ""),
                    "calendar_url": (
                        _calendar_link(account.provider, action)
                        if _is_calendar_worthy(action) else None
                    ),
                })

        action_count = sum(1 for i in items if not i.get("divider"))
        if action_count == 0:
            return {"success": True, "message": "no action items", "user": user.primary_email}

        # Send from the user's primary account.
        primary = next(
            (a for a in user.email_accounts if a.email == user.primary_email),
            user.email_accounts[0] if user.email_accounts else None,
        )
        if primary is None:
            return {"success": False, "message": "no email account", "user": user.primary_email}

        html = _render_digest(items, user.primary_email, site_url)
        subject = f"Daily To-Do List from MailMind for {datetime.now().strftime('%B %d, %Y')}"
        access_token = refresh(primary)
        sent = _send_html_email(primary.email, access_token, primary.provider, subject, html)
        if sent:
            return {"success": True, "message": f"sent {action_count} items", "user": user.primary_email}
        return {"success": False, "message": "send failed", "user": user.primary_email}
    except Exception as exc:
        logger.exception("digest failed for %s: %s", user.primary_email, exc)
        return {"success": False, "message": "unexpected error", "user": user.primary_email}
    finally:
        _release_send_lock(user.id)


def check_and_send_emails():
    """Called by the cron trigger every 15 minutes."""
    import os
    site_url = os.getenv("DOMAIN", "https://mailmind.fly.dev")

    if not flask_app:
        logger.error("Flask app not available")
        return

    with flask_app.app_context():
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
