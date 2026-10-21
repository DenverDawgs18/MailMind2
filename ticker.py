"""
Entry point for the Fly `ticker` process: a small, always-on clock.

At every quarter-hour it checks the database for work: a list that's due and
not yet sent (same rules as the app, from functions/slots.py), or forwarded
mail waiting to be read. Only then does it call POST /cron/tick, which wakes
the app. So the app machine wakes a few times a day, around delivery times,
and is stopped the rest of the time.

Kept deliberately light (no Flask app import) so it fits a 256 MB machine.
"""
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text

from functions.slots import EARLY_SECONDS, latest_due_slot

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ticker")

TICK_URL = os.getenv("TICK_URL", os.getenv("DOMAIN", "https://mailmind.dev").rstrip("/") + "/cron/tick")
ATTEMPTS = 3

_INBOUND_SQL = text("SELECT 1 FROM inbound_email LIMIT 1")
_SCHEDULED_SQL = text("""
    SELECT id, time, timezone FROM master
     WHERE time IS NOT NULL AND time <> '' AND timezone IS NOT NULL
       AND (subscribed = :yes OR comp_until > :now)
""")
_SENT_SINCE_SQL = text("SELECT 1 FROM digest WHERE master_id = :mid AND created_at >= :since LIMIT 1")


def engine_from_env():
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)


def work_waiting(engine, now=None) -> bool:
    """True when a list is due and not yet sent, or forwarded mail is waiting."""
    now = now or datetime.now(timezone.utc)
    with engine.connect() as conn:
        if conn.execute(_INBOUND_SQL).first() is not None:
            return True
        naive = engine.dialect.name == "sqlite"  # SQLite stores UTC without an offset
        for mid, times, tz in conn.execute(_SCHEDULED_SQL, {"yes": True, "now": now}).all():
            slot = latest_due_slot(times, tz, now)
            if slot is None:
                continue
            since = slot - timedelta(seconds=EARLY_SECONDS)
            if naive:
                since = since.replace(tzinfo=None)
            if conn.execute(_SENT_SINCE_SQL, {"mid": mid, "since": since}).first() is None:
                return True
    return False


def next_quarter_hour(now: datetime) -> datetime:
    base = now.replace(second=0, microsecond=0)
    return base + timedelta(minutes=15 - base.minute % 15)


def call_tick():
    secret = os.environ["CRON_SECRET"]
    for attempt in range(1, ATTEMPTS + 1):
        req = urllib.request.Request(TICK_URL, method="POST", data=b"",
                                     headers={"Authorization": f"Bearer {secret}"})
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                body = resp.read().decode(errors="replace").strip()
            if "error" in body:
                logger.error("Tick reported an error")
            else:
                logger.info("Tick done: %s", body or "ok")
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning("Tick attempt %d failed: %s", attempt, exc)
            time.sleep(20)
    logger.error("Tick failed after %d attempts; the next one will catch up", ATTEMPTS)


def main():
    engine = engine_from_env()
    logger.info("Ticker started; calling %s when there's work", TICK_URL)
    while True:
        now = datetime.now(timezone.utc)
        time.sleep(max(0.0, (next_quarter_hour(now) - now).total_seconds()))
        try:
            needed = work_waiting(engine)
        except Exception:
            logger.exception("Couldn't check for work; ticking anyway")
            needed = True
        if needed:
            call_tick()
        else:
            logger.info("Nothing to do this tick")


if __name__ == "__main__":
    main()
