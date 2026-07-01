"""Smoke tests covering the Sprint-4 minimal surface."""
import json


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

def test_public_pages_render(client):
    for path in ("/", "/contact", "/termsandprivacy"):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} returned {resp.status_code}"


def test_landing_shows_oauth_buttons(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Sign in with Google" in resp.data
    assert b"Sign in with Microsoft" in resp.data


def test_no_password_login_form_exists(client):
    """Login-with-password should be gone. Landing has no form."""
    resp = client.get("/")
    # Only the outer form (if any) — no password input anywhere.
    assert b'type="password"' not in resp.data


# ---------------------------------------------------------------------------
# Auth / OAuth
# ---------------------------------------------------------------------------

def test_settings_requires_login(client):
    resp = client.get("/settings", follow_redirects=False)
    # login_view is "index", so we get redirected there
    assert resp.status_code in (302, 401)


def test_oauth_callback_rejects_bad_state(client):
    resp = client.get("/google/callback?state=nope&code=whatever")
    assert resp.status_code == 400


def test_finish_oauth_creates_master(app):
    """First OAuth for an email creates a Master + primary EmailAccount."""
    from app import _finish_oauth, db as _db
    from models import EmailAccount, Master

    with app.test_request_context("/"):
        _finish_oauth("google", "new-user@example.com", "test-refresh-token")

        master = Master.query.filter_by(primary_email="new-user@example.com").one()
        assert master.subscribed is False
        assert len(master.email_accounts) == 1
        assert master.email_accounts[0].provider == "google"


def test_finish_oauth_reuses_existing_master(app):
    """Second OAuth with the same primary_email logs into the same Master."""
    from app import _finish_oauth
    from models import Master

    with app.test_request_context("/"):
        _finish_oauth("google", "reuse@example.com", "tok-1")
        first_id = Master.query.filter_by(primary_email="reuse@example.com").one().id

    with app.test_request_context("/"):
        _finish_oauth("google", "reuse@example.com", "tok-2")

    masters = Master.query.filter_by(primary_email="reuse@example.com").all()
    assert len(masters) == 1
    assert masters[0].id == first_id


# ---------------------------------------------------------------------------
# Settings CRUD
# ---------------------------------------------------------------------------

def _login_as(app, client, primary_email="me@example.com", subscribed=True):
    from app import db as _db
    from models import Master
    with app.app_context():
        m = Master(primary_email=primary_email, subscribed=subscribed, temp=False)
        _db.session.add(m)
        _db.session.commit()
        uid = m.id
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
    return uid


def test_settings_saves_time_and_timezone(app, client):
    _login_as(app, client)
    resp = client.post("/settings", data={
        "timezone": "America/New_York",
        "time": ["8:00 AM", "5:00 PM"],
    })
    assert resp.status_code == 200

    from app import db as _db
    from models import Master
    with app.app_context():
        m = Master.query.filter_by(primary_email="me@example.com").one()
        assert m.timezone == "America/New_York"
        assert m.time == "8:00 AM,5:00 PM"


def test_settings_caps_at_three_times(app, client):
    _login_as(app, client)
    client.post("/settings", data={
        "timezone": "UTC",
        "time": ["1:00 AM", "2:00 AM", "3:00 AM", "4:00 AM"],
    })
    from app import db as _db
    from models import Master
    with app.app_context():
        m = Master.query.filter_by(primary_email="me@example.com").one()
        assert m.time.count(",") == 2  # exactly 3 entries


def test_delete_secondary_account(app, client):
    from app import db as _db
    from functions.encryption import encrypt_token
    from models import EmailAccount, Master

    with app.app_context():
        m = Master(primary_email="owner@example.com", subscribed=True, temp=False)
        _db.session.add(m)
        _db.session.commit()
        primary = EmailAccount(email="owner@example.com", oauth_token=encrypt_token("x"),
                               provider="google", master=m)
        secondary = EmailAccount(email="second@example.com", oauth_token=encrypt_token("y"),
                                 provider="microsoft", master=m)
        _db.session.add_all([primary, secondary])
        _db.session.commit()
        uid = m.id
        secondary_id = secondary.id

    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)

    resp = client.post("/settings/delete_account", data={"id": str(secondary_id)})
    assert resp.status_code == 200
    with app.app_context():
        assert _db.session.get(EmailAccount,secondary_id) is None


def test_cannot_delete_primary_account(app, client):
    from app import db as _db
    from functions.encryption import encrypt_token
    from models import EmailAccount, Master

    with app.app_context():
        m = Master(primary_email="only@example.com", subscribed=True, temp=False)
        _db.session.add(m)
        _db.session.commit()
        primary = EmailAccount(email="only@example.com", oauth_token=encrypt_token("x"),
                               provider="google", master=m)
        _db.session.add(primary)
        _db.session.commit()
        uid = m.id
        primary_id = primary.id

    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)

    resp = client.post("/settings/delete_account", data={"id": str(primary_id)})
    assert resp.status_code == 400
    with app.app_context():
        assert _db.session.get(EmailAccount,primary_id) is not None


def test_delete_account_cannot_touch_other_user(app, client):
    """IDOR regression."""
    from app import db as _db
    from functions.encryption import encrypt_token
    from models import EmailAccount, Master

    with app.app_context():
        attacker = Master(primary_email="attacker@x.com", subscribed=True, temp=False)
        victim = Master(primary_email="victim@x.com", subscribed=True, temp=False)
        _db.session.add_all([attacker, victim])
        _db.session.commit()
        va = EmailAccount(email="victim-extra@x.com", oauth_token=encrypt_token("x"),
                          provider="google", master=victim)
        _db.session.add(va)
        _db.session.commit()
        attacker_id = attacker.id
        va_id = va.id

    with client.session_transaction() as sess:
        sess["_user_id"] = str(attacker_id)

    resp = client.post("/settings/delete_account", data={"id": str(va_id)})
    assert resp.status_code == 404
    with app.app_context():
        assert _db.session.get(EmailAccount,va_id) is not None


# ---------------------------------------------------------------------------
# Beta code
# ---------------------------------------------------------------------------

def test_code_route_grants_temp(app, client, monkeypatch):
    monkeypatch.setenv("TEMP_CODE", "letmein")
    from app import db as _db
    from models import Master

    with app.app_context():
        m = Master(primary_email="beta@x.com", subscribed=False, temp=False)
        _db.session.add(m)
        _db.session.commit()
        uid = m.id
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)

    resp = client.post("/code", data={"code": "letmein"})
    assert resp.status_code in (302, 200)
    with app.app_context():
        m = Master.query.filter_by(primary_email="beta@x.com").one()
        assert m.subscribed is True
        assert m.temp is True


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

def test_webhook_rejects_missing_signature(client):
    resp = client.post("/webhook", data=json.dumps({"type": "checkout.session.completed"}))
    assert resp.status_code in (400, 500)


def test_webhook_rejects_bad_signature(client):
    resp = client.post("/webhook",
                       data=json.dumps({"type": "checkout.session.completed"}),
                       headers={"stripe-signature": "t=1,v1=deadbeef"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Support code
# ---------------------------------------------------------------------------

def test_production_flag_respects_env(monkeypatch):
    from functions import production as prod_mod
    monkeypatch.setenv("MAILMIND_PRODUCTION", "1")
    assert prod_mod.production() is True
    monkeypatch.setenv("MAILMIND_PRODUCTION", "0")
    monkeypatch.delenv("FLASK_ENV", raising=False)
    assert prod_mod.production() is False


def test_encryption_roundtrip():
    from functions.encryption import decrypt_token, encrypt_token
    assert decrypt_token(encrypt_token("hello")) == "hello"


def test_calendar_deep_link_provider_split():
    from functions.scheduler import _calendar_link
    g = _calendar_link("google", "Meet Sarah Tuesday")
    m = _calendar_link("microsoft", "Meet Sarah Tuesday")
    assert "calendar.google.com" in g
    assert "text=Meet%20Sarah%20Tuesday" in g
    assert "outlook.live.com" in m
    assert "subject=Meet%20Sarah%20Tuesday" in m


def test_scheduler_time_match_boundaries():
    from datetime import datetime, timezone
    from functions.scheduler import _is_time_match
    now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    # 7 AM Eastern in January = 12:00 UTC
    assert _is_time_match("7:00 AM", now, "America/New_York") is True
    assert _is_time_match("9:00 AM", now, "America/New_York") is False
