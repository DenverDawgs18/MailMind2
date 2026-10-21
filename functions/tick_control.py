"""
Keep the GitHub Actions tick (.github/workflows/tick.yml) running only while
there's work for it: someone with a delivery time and an active plan, or
forwarded mail waiting to be read.

The workflow disables itself when a tick reports "idle"; the app re-enables
it here when that changes (a schedule is saved, a plan starts, a code is
redeemed). Re-enabling needs GITHUB_WORKFLOW_TOKEN, a fine-grained token with
Actions read/write on the repository. Without it the app never reports idle,
so the tick simply keeps running every 15 minutes.
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

REPO = os.getenv("GITHUB_REPOSITORY", "DenverDawgs18/MailMind2")
WORKFLOW = "tick.yml"
RE_ENABLE_AFTER_SECONDS = 90


def _token():
    return (os.getenv("GITHUB_WORKFLOW_TOKEN") or "").strip()


def can_resume() -> bool:
    """Only let the tick pause itself if we're able to turn it back on."""
    return bool(_token())


def tick_needed() -> bool:
    from app import db
    from models import InboundEmail, Master
    from sqlalchemy import or_
    now = datetime.now(timezone.utc)
    if db.session.query(InboundEmail.id).first() is not None:
        return True
    return db.session.query(Master.id).filter(
        Master.time.isnot(None), Master.time != "", Master.timezone.isnot(None),
        or_(Master.subscribed.is_(True), Master.comp_until > now),
    ).first() is not None


def _enable():
    try:
        resp = requests.put(
            f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/enable",
            headers={"Authorization": f"Bearer {_token()}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            timeout=10,
        )
        if resp.status_code != 204:
            logger.error("Couldn't enable the tick workflow: HTTP %s", resp.status_code)
    except requests.RequestException as exc:
        logger.error("Couldn't enable the tick workflow: %s", exc)


def ensure_running():
    """Turn the tick back on if there's work for it. Never raises; doesn't block the request."""
    if not can_resume():
        return
    try:
        needed = tick_needed()
    except Exception:
        logger.warning("tick_needed check failed; enabling the tick to be safe", exc_info=True)
        needed = True
    if needed:
        threading.Thread(target=_enable_twice, daemon=True).start()


def _enable_twice():
    # A tick that just reported "idle" may disable the workflow a few seconds
    # after this; enabling again shortly afterwards closes that window.
    _enable()
    time.sleep(RE_ENABLE_AFTER_SECONDS)
    _enable()
