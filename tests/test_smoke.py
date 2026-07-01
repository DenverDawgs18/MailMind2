"""Smoke tests covering the highest-risk security surfaces."""
import json


def test_public_pages_render(client):
    for path in ("/", "/login", "/contact", "/request_access", "/termsandprivacy"):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} returned {resp.status_code}"


def test_login_rejects_short_password(client):
    resp = client.post(
        "/login",
        data=json.dumps({"mode": "register", "username": "alice", "password": "short"}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert b"characters" in resp.data.lower()


def test_login_registers_then_logs_in(client):
    strong = "hunter2hunter2"
    resp = client.post(
        "/login",
        data=json.dumps({"mode": "register", "username": "bob", "password": strong}),
        content_type="application/json",
    )
    assert resp.status_code == 201

    client.get("/logout")

    resp = client.post(
        "/login",
        data=json.dumps({"mode": "login", "username": "bob", "password": strong}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True


def test_login_wrong_password_401(client):
    strong = "correct-horse-battery"
    client.post(
        "/login",
        data=json.dumps({"mode": "register", "username": "eve", "password": strong}),
        content_type="application/json",
    )
    client.get("/logout")
    resp = client.post(
        "/login",
        data=json.dumps({"mode": "login", "username": "eve", "password": "wrong"}),
        content_type="application/json",
    )
    assert resp.status_code == 401


def test_delete_account_requires_login(client):
    resp = client.post(
        "/delete_account",
        data=json.dumps({"id": 1}),
        content_type="application/json",
    )
    # Flask-Login sends unauthenticated requests to the login view.
    assert resp.status_code in (302, 401)


def test_delete_account_cannot_touch_other_user(app, client):
    """IDOR regression: verify /delete_account enforces ownership."""
    from app import db as _db
    from functions.encryption import encrypt_token
    from models import EmailAccount, Master
    from werkzeug.security import generate_password_hash

    with app.app_context():
        attacker = Master(
            username="attacker",
            password=generate_password_hash("attackerpass"),
            subscribed=True,
        )
        victim = Master(
            username="victim",
            password=generate_password_hash("victimpass"),
            subscribed=True,
        )
        _db.session.add_all([attacker, victim])
        _db.session.commit()

        victim_account = EmailAccount(
            email="victim@example.com",
            oauth_token=encrypt_token("dummy-token"),
            provider="google",
            master=victim,
        )
        _db.session.add(victim_account)
        _db.session.commit()
        victim_account_id = victim_account.id
        attacker_id = attacker.id

    # Log in as attacker
    with client.session_transaction() as sess:
        sess["_user_id"] = str(attacker_id)

    resp = client.post(
        "/delete_account",
        data=json.dumps({"id": victim_account_id}),
        content_type="application/json",
    )
    assert resp.status_code == 404

    # Victim's account should still exist
    with app.app_context():
        assert _db.session.get(EmailAccount, victim_account_id) is not None


def test_add_to_calendar_rejects_foreign_account(app, client):
    from app import db as _db
    from functions.encryption import encrypt_token
    from models import EmailAccount, Master
    from werkzeug.security import generate_password_hash

    with app.app_context():
        u1 = Master(username="u1", password=generate_password_hash("u1passu1pass"), subscribed=True)
        u2 = Master(username="u2", password=generate_password_hash("u2passu2pass"), subscribed=True)
        _db.session.add_all([u1, u2])
        _db.session.commit()
        u2_account = EmailAccount(
            email="u2@example.com",
            oauth_token=encrypt_token("x"),
            provider="google",
            master=u2,
        )
        _db.session.add(u2_account)
        _db.session.commit()
        u1_id = u1.id
        u2_account_id = u2_account.id

    with client.session_transaction() as sess:
        sess["_user_id"] = str(u1_id)

    resp = client.post(
        "/add_to_calendar",
        data={
            "account": str(u2_account_id),
            "name": "hostile-event",
            "start": "2026-01-01T10:00",
            "end": "2026-01-01T11:00",
        },
    )
    assert resp.status_code == 404


def test_webhook_rejects_missing_signature(client):
    resp = client.post("/webhook", data=json.dumps({"type": "checkout.session.completed"}))
    # No signature header -> either signature-missing rejection or bad signature.
    assert resp.status_code in (400, 500)


def test_webhook_rejects_bad_signature(client):
    resp = client.post(
        "/webhook",
        data=json.dumps({"type": "checkout.session.completed"}),
        headers={"stripe-signature": "t=1,v1=deadbeef"},
    )
    assert resp.status_code == 400


def test_oauth_callback_rejects_bad_state(client, app):
    from models import Master
    from werkzeug.security import generate_password_hash
    from app import db as _db

    with app.app_context():
        u = Master(username="oauthy", password=generate_password_hash("passpasspass"), subscribed=True)
        _db.session.add(u)
        _db.session.commit()
        uid = u.id

    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["google_oauth_state"] = "state-A"

    resp = client.get("/google/callback?state=state-B&code=whatever")
    assert resp.status_code == 400


def test_production_flag_respects_env(monkeypatch):
    from functions import production as prod_mod

    monkeypatch.setenv("MAILMIND_PRODUCTION", "1")
    assert prod_mod.production() is True

    monkeypatch.setenv("MAILMIND_PRODUCTION", "0")
    monkeypatch.delenv("FLASK_ENV", raising=False)
    assert prod_mod.production() is False

    monkeypatch.delenv("MAILMIND_PRODUCTION", raising=False)
    monkeypatch.setenv("FLASK_ENV", "production")
    assert prod_mod.production() is True


def test_scheduler_time_match_boundaries():
    from datetime import datetime, timezone
    from functions.scheduler import is_time_match

    now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    # 7:00 AM Eastern == 12:00 UTC in January (EST = UTC-5)
    assert is_time_match("7:00 AM", now, "America/New_York") is True
    # Two hours off should not match
    assert is_time_match("9:00 AM", now, "America/New_York") is False


def test_encryption_roundtrip():
    from functions.encryption import decrypt_token, encrypt_token

    token = "hello, world!"
    assert decrypt_token(encrypt_token(token)) == token


def test_linkify_escapes_hostile_text():
    from functions.linkify import linkify_text

    hostile = '<script>alert(1)</script>'
    rendered = str(linkify_text(hostile))
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
