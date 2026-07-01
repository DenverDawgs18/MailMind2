# scheduler.py
import logging
from datetime import datetime
from typing import Any, Dict, List

import pytz
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from jinja2 import Environment
from sqlalchemy import and_

from app import app, db
from functions.get_emails import get_emails
from functions.get_one_action import get_an_action
from functions.linkify import linkify_text
from functions.refresh_token import refresh
from functions.reply import reply
from models import EmailAccount, Master

logger = logging.getLogger(__name__)

# Global scheduler instance
scheduler = None
# Store Flask app reference for context
flask_app = app

# Optional Redis client for cross-process locking. When Redis is unavailable
# we fall back to a process-local lock so a single-process dev run still
# behaves sanely.
try:
    from app import _redis_client  # type: ignore
except Exception:
    _redis_client = None

_LOCK_TTL_SECONDS = 60 * 30  # 30 minutes


class _LocalLock:
    """Minimal in-process fallback when Redis isn't available."""

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
    """
    Return True iff this worker acquired the exclusive send lock for the user.

    Uses SET NX with a TTL when Redis is available; falls back to an in-process
    mutex otherwise.
    """
    key = f"mailmind:send-lock:{user_id}"
    if _redis_client is not None:
        try:
            return bool(_redis_client.set(key, "1", nx=True, ex=_LOCK_TTL_SECONDS))
        except Exception as exc:
            logger.warning("Redis lock failure, falling back to local lock: %s", exc)
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


def generate_email_html(action_items, user_email):
    """Render the daily summary email."""

    html_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
    <meta charset="UTF-8" />
    <title>MailMind Daily Summary</title>
    <style>
        body { background: #171524; color: #f0f0f0; font-family: Inter, system-ui, sans-serif; margin: 0; padding: 20px; }
        .container { max-width: 600px; margin: 0 auto; background: linear-gradient(145deg, #514b7a, #5a5488); border: 2px solid black; border-radius: 20px; padding: 30px; box-shadow: 4px 4px 10px rgba(0, 0, 0, 0.4); }
        h1 { text-align: center; font-size: 24px; color: #e6d7a3; margin-bottom: 10px; }
        .top-link { text-align: center; margin-bottom: 20px; }
        .top-link a { color: #cc8400; font-weight: bold; text-decoration: none; font-size: 14px; }
        .summary-info { text-align: center; font-size: 14px; margin-bottom: 30px; color: #f0f0f0; }
        .item { background: #3a3560; border: 1px solid rgba(255, 255, 255, 0.1); border-left: 4px solid #cc8400; border-radius: 12px; padding: 15px 20px; margin-bottom: 20px; }
        .item p { margin: 4px 0; font-size: 14px; }
        .item .action { color: #e6d7a3; font-weight: 600; }
        .footer { text-align: center; font-size: 12px; color: #ccc; margin-top: 30px; }
        p { color: white; }
        .footer a { text-decoration: none; color: darkorange; }
        .footer a:hover { color: #e6d7a3; }
    </style>
    </head>
    <body>
    <div class="container">
        <h1>Your Daily To Do List</h1>
        <div class="top-link">
        <a href="https://mailmind.fly.dev/summary">View Full List</a>
        </div>
        <div class="summary-info">
        <p>{{ action_count }} action items - {{ current_date }}</p>
        <p>{{ user_email }}</p>
        </div>

        {% for email in action_items %}
        {% if email.change %}
        <div class="item">
        <p class="action">Action items for {{ email.email }}</p>
        </div>
        {% endif %}
        <div class="item">
        <p class="action">- {{ email.action_items | linkify_text }}</p>
        <p><strong>From:</strong> {{ email['from'] }}</p>
        <p><strong>Subject:</strong> {{ email.subject }}</p>
        </div>
        {% endfor %}

        <div class="footer">
        <p>
            Sent by <a href="https://mailmind.fly.dev">MailMind</a> -
            <a href="mailto:pautomas55@gmail.com">Support</a>
        </p>
        </div>
    </div>
    </body>
    </html>
    """

    env = Environment(autoescape=True)
    env.filters['linkify_text'] = linkify_text
    template = env.from_string(html_template)

    return template.render(
        action_items=action_items,
        user_email=user_email,
        current_date=datetime.now().strftime('%B %d, %Y'),
        action_count=len(action_items),
    )


def is_calendar_worthy(action_text: str) -> bool:
    calendar_keywords = [
        "meeting", "conference call", "calendar", "appointment", "call", "schedule", "event",
    ]
    return any(keyword in action_text.lower() for keyword in calendar_keywords)


def send_reply_email_for_user(action_items, account):
    """Send the summary using the reply helper."""
    try:
        html_content = generate_email_html(action_items, account.email)
        subject = f"Daily To Do List from MailMind for {datetime.now().strftime('%B %d, %Y')}"

        result = reply(
            user_email=account.email,
            oauth_token=refresh(account),
            to_email=account.email,
            subject=subject,
            body=html_content,
            reply=False,
            cc=None,
            bcc=None,
            provider=account.provider,
            content_type='html',
        )
        if result:
            logger.info("Email sent successfully to %s", account.email)
            return True
        logger.error("Failed to send email using reply function")
        return False
    except Exception as exc:
        logger.exception("Error sending email via reply function: %s", exc)
        return False


def init_scheduler(app):
    """Initialize the email scheduler."""
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


def parse_user_times(time_string: str) -> List[str]:
    if not time_string:
        return []
    return [t.strip() for t in time_string.split(',') if t.strip()]


def convert_to_24hour(time_str: str):
    try:
        return datetime.strptime(time_str, '%I:%M %p').time().strftime('%H:%M')
    except ValueError:
        try:
            return datetime.strptime(time_str, '%I:%M%p').time().strftime('%H:%M')
        except ValueError:
            logger.error("Could not parse time string: %s", time_str)
            return None


def is_time_match(user_time: str, current_time: datetime, timezone_str: str) -> bool:
    try:
        user_timezone = pytz.timezone(timezone_str)
        current_local = current_time.astimezone(user_timezone)

        user_time_24 = convert_to_24hour(user_time)
        if not user_time_24:
            return False

        user_hour, user_minute = map(int, user_time_24.split(':'))
        target_time = current_local.replace(
            hour=user_hour, minute=user_minute, second=0, microsecond=0
        )
        time_diff = abs((current_local - target_time).total_seconds())
        # ±7.5 minutes so a single cron firing covers one 15-minute window
        return time_diff <= 450
    except Exception as exc:
        logger.error(
            "Error checking time match for user time %s, timezone %s: %s",
            user_time, timezone_str, exc,
        )
        return False


def get_users_to_process() -> List[Master]:
    current_time = datetime.now(pytz.UTC)
    users_to_process: List[Master] = []

    try:
        users = Master.query.filter(
            and_(Master.timezone.isnot(None), Master.time.isnot(None))
        ).all()

        for user in users:
            if not user.timezone or not user.time:
                continue
            for user_time in parse_user_times(user.time):
                if is_time_match(user_time, current_time, user.timezone):
                    users_to_process.append(user)
                    logger.info(
                        "User %s scheduled for email summary at %s (%s)",
                        user.username, user_time, user.timezone,
                    )
                    break
    except Exception as exc:
        logger.exception("Error getting users to process: %s", exc)
    return users_to_process


def send_email_summary_for_user(user: Master) -> Dict[str, Any]:
    """Send email summary for a specific user."""
    if not _acquire_send_lock(user.id):
        logger.info("Send lock held for user %s; skipping", user.username)
        return {
            "success": True,
            "message": "Another worker is already processing this user",
            "user": user.username,
        }

    try:
        action_items = []
        for account in user.email_accounts:
            try:
                access_token = refresh(account)
                emails = get_emails(
                    account.provider, account.email, access_token, master_id=user.id
                )
            except Exception as exc:
                logger.exception(
                    "Failed to fetch emails for %s / %s: %s", user.username, account.email, exc
                )
                continue

            first = True
            for email in list(reversed(emails)):
                try:
                    body = email.get("body", "")
                    if not body:
                        continue
                    action = get_an_action(body)
                    if action and action.lower() not in ["no action.", "no action", "no action required.", ""]:
                        email["action_items"] = action
                        email["calendar"] = is_calendar_worthy(action)
                        email["email"] = account.email
                        email["change"] = first
                        first = False
                        action_items.append(email)
                except Exception as exc:
                    logger.exception("Error generating action for email: %s", exc)

        if not action_items:
            return {
                "success": True,
                "message": "No action items found to send",
                "action_count": 0,
                "user": user.username,
            }

        if not user.email_accounts:
            return {
                "success": False,
                "message": "User has no email accounts to send from",
                "user": user.username,
            }

        # Send from the first account (that's the "primary" account today).
        email_sent = send_reply_email_for_user(action_items, user.email_accounts[0])
        if email_sent:
            return {
                "success": True,
                "message": f"Email summary sent with {len(action_items)} action items",
                "action_count": len(action_items),
                "user": user.username,
            }
        return {
            "success": False,
            "message": "Failed to send email summary",
            "user": user.username,
        }
    except Exception as exc:
        logger.exception("Error in send_email_summary for user %s: %s", user.username, exc)
        return {
            "success": False,
            "message": "An error occurred while processing email summary",
            "user": user.username,
        }
    finally:
        _release_send_lock(user.id)


def check_and_send_emails():
    """Main function that runs every 15 minutes to check and send emails."""
    logger.info("Checking for users to send email summaries")

    if not flask_app:
        logger.error("Flask app not available for scheduler context")
        return

    with flask_app.app_context():
        try:
            users_to_process = get_users_to_process()
            if not users_to_process:
                logger.info("No users scheduled for email summaries at this time")
                return

            logger.info("Processing %d users for email summaries", len(users_to_process))
            for user in users_to_process:
                try:
                    result = send_email_summary_for_user(user)
                    if result["success"]:
                        logger.info("Sent email summary to %s: %s", user.username, result['message'])
                    else:
                        logger.warning("Failed to send email summary to %s: %s", user.username, result['message'])
                except Exception as exc:
                    logger.exception("Error processing user %s: %s", user.username, exc)
        except Exception as exc:
            logger.exception("Error in check_and_send_emails: %s", exc)


def trigger_email_check():
    check_and_send_emails()


def get_scheduler_status():
    global scheduler
    if scheduler:
        jobs = scheduler.get_jobs()
        return {
            "scheduler_running": scheduler.running,
            "jobs": [
                {"id": job.id, "name": job.name, "next_run": str(job.next_run_time)}
                for job in jobs
            ],
        }
    return {"scheduler_running": False, "jobs": []}
