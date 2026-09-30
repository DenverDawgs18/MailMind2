"""Run the real Alembic migrations against fresh and legacy-shaped databases."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent

# The schema production was created with (db.create_all() on the pre-Sprint-4 models).
LEGACY_SCHEMA = """
CREATE TABLE master (
    id INTEGER PRIMARY KEY, username TEXT, password TEXT, stripe_customer_id VARCHAR UNIQUE,
    last_login DATETIME, time TEXT, temp BOOLEAN, subscribed BOOLEAN, timezone TEXT
);
CREATE TABLE email_account (
    id INTEGER PRIMARY KEY, email VARCHAR(120) NOT NULL UNIQUE, oauth_token TEXT NOT NULL,
    high_priority JSON, provider TEXT, master_id INTEGER NOT NULL REFERENCES master(id) ON DELETE CASCADE
);
CREATE TABLE link (id INTEGER PRIMARY KEY, link TEXT, short VARCHAR(1000));
CREATE TABLE unsubscribe (id INTEGER PRIMARY KEY, link TEXT, user INTEGER REFERENCES master(id), sender VARCHAR(1000));
CREATE TABLE todo (id INTEGER PRIMARY KEY, item TEXT, master INTEGER REFERENCES master(id), done BOOLEAN);
"""


def _upgrade(db_path):
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{db_path}", MAILMIND_TEST="1",
               MAILMIND_PRODUCTION="0", ENCRYPTION_KEY=Fernet.generate_key().decode(),
               SECRET_KEY="x", FLASK_APP="app.py")
    result = subprocess.run([sys.executable, "-m", "flask", "db", "upgrade"], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_fresh_database(tmp_path):
    db = tmp_path / "fresh.db"
    _upgrade(db)
    conn = sqlite3.connect(db)
    assert {"primary_email", "subscribed", "temp", "created_at"} <= _columns(conn, "master")
    assert {"provider_subject", "needs_reauth", "created_at"} <= _columns(conn, "email_account")
    assert {"master_id", "created_at", "delivered"} <= _columns(conn, "digest")
    assert {"digest_id", "action", "done"} <= _columns(conn, "digest_item")
    assert {"provider", "subject", "master_id"} <= _columns(conn, "identity")
    assert {"token", "confirmation_code", "last_received_at"} <= _columns(conn, "forwarding_address")
    assert {"body", "source_email", "message_id"} <= _columns(conn, "inbound_email")
    assert {"action", "source_email"} <= _columns(conn, "pending_item")
    assert {"code", "max_uses", "uses", "access_days", "expires_at", "active"} <= _columns(conn, "access_code")
    assert {"comp_until", "access_code_id"} <= _columns(conn, "master")
    assert {"signup_source", "signup_referrer", "signup_landing"} <= _columns(conn, "master")
    assert {"day", "source", "landing", "visits"} <= _columns(conn, "source_visit")


def test_legacy_database_is_adopted_without_data_loss(tmp_path):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(LEGACY_SCHEMA)
    conn.executescript("""
        INSERT INTO master (id, username, password, subscribed, temp, stripe_customer_id)
            VALUES (1, 'alice', 'hash', NULL, NULL, 'cus_A'),
                   (2, 'bob@example.com', 'hash', 1, 0, NULL),
                   (3, 'carol', 'hash', 1, 1, NULL);
        INSERT INTO email_account (id, email, oauth_token, provider, master_id)
            VALUES (10, 'alice@gmail.com', 'tok', NULL, 1),
                   (11, 'alice@work.com', 'tok', 'microsoft', 1);
        INSERT INTO todo (item, master, done) VALUES ('keep me', 1, 0);
    """)
    conn.commit()
    conn.close()

    _upgrade(db)
    _upgrade(db)  # idempotent

    conn = sqlite3.connect(db)
    masters = dict(conn.execute("SELECT id, primary_email FROM master"))
    assert masters == {1: "alice@gmail.com", 2: "bob@example.com", 3: "legacy-3@users.mailmind.invalid"}
    assert conn.execute("SELECT subscribed, temp, stripe_customer_id FROM master WHERE id=1").fetchone() == (0, 0, "cus_A")
    accounts = conn.execute("SELECT id, provider, needs_reauth, provider_subject FROM email_account ORDER BY id").fetchall()
    assert accounts == [(10, "google", 0, None), (11, "microsoft", 0, None)]
    assert conn.execute("SELECT item FROM todo").fetchone() == ("keep me",)
    assert "username" in _columns(conn, "master")  # legacy columns kept, just unused
    # People let in by the old shared beta codes keep access as comped-forever;
    # `subscribed` is left to Stripe.
    access = {row[0]: row[1:] for row in conn.execute("SELECT id, subscribed, comp_until IS NOT NULL FROM master")}
    assert access == {1: (0, 0), 2: (0, 1), 3: (0, 1)}
