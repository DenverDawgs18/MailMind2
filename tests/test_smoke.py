"""Smoke and regression tests for the MailMind web app and digest pipeline."""
import contextlib
import json
import urllib.parse

import pytest


@contextlib.contextmanager
def anon(app):
    """A request context with nobody signed in.

    The conftest `app` fixture keeps one app context open for the whole test,
    and flask-login caches the current user on it, so clear that first.
    """
    from flask import g
    g.pop("_login_user", None)
    with app.test_request_context("/"):
        yield


def _login_as(app, client, primary_email="me@example.com", subscribed=True, accounts=()):
    """Create a Master (plus optional EmailAccounts) and log the client in as it."""
    from app import db as _db
    from functions.encryption import encrypt_token
    from models import EmailAccount, Master
    with app.app_context():
        m = Master(primary_email=primary_email, subscribed=subscribed, temp=False)
        _db.session.add(m)
        _db.session.commit()
        ids = []
        for email, provider in accounts:
            a = EmailAccount(email=email, oauth_token=encrypt_token("rt-" + email),
                             provider=provider, provider_subject="sub-" + email, master=m)
            _db.session.add(a)
            _db.session.commit()
            ids.append(a.id)
        uid = m.id
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
    from flask import g
    g.pop("_login_user", None)  # flask-login caches the user on the shared app context
    return uid, ids


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/login", "/request_access", "/beta", "/contact", "/termsandprivacy"])
def test_public_pages_render(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} returned {resp.status_code}"


def test_landing_ctas(client):
    resp = client.get("/")
    assert b"Join the beta" in resp.data
    assert b'href="/request_access"' in resp.data
    assert b'href="/login"' in resp.data


def test_login_page_offers_both_providers(client):
    resp = client.get("/login")
    assert b'href="/google/login"' in resp.data
    assert b'href="/microsoft/login"' in resp.data
    assert b'type="password"' not in resp.data


def test_forms_redirect_back_to_this_site(client):
    for path, target in (("/request_access", "/beta"), ("/contact", "/contact?sent=1")):
        html = client.get(path).data.decode()
        assert f'value="http://localhost:5000{target}"' in html
        assert "mywebsite.com" not in html


def test_404_page(client):
    resp = client.get("/definitely-not-here")
    assert resp.status_code == 404
    assert b"Page not found" in resp.data


def test_logout_requires_post(app, client):
    _login_as(app, client)
    assert client.get("/logout").status_code == 405
    assert client.post("/logout").status_code == 302


# ---------------------------------------------------------------------------
# OAuth: flows
# ---------------------------------------------------------------------------

def test_settings_requires_login(client):
    resp = client.get("/settings", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_oauth_callback_rejects_bad_state(client):
    resp = client.get("/google/callback?state=nope&code=whatever")
    assert resp.status_code == 400
    assert b"expired" in resp.data


def test_oauth_callback_handles_user_cancel(client):
    resp = client.get("/microsoft/callback?error=access_denied&state=x")
    assert resp.status_code == 400
    assert b"cancelled" in resp.data


def test_google_login_uses_pkce_and_keeps_verifier(client):
    resp = client.get("/google/login")
    assert resp.status_code == 302
    query = urllib.parse.parse_qs(urllib.parse.urlparse(resp.headers["Location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"]
    with client.session_transaction() as sess:
        stored = sess["google_oauth"]
    assert stored["state"] == query["state"][0]
    assert stored["verifier"]


def test_google_callback_sends_stored_verifier(app, client, monkeypatch):
    """Regression: the callback Flow must carry the verifier from /google/login."""
    import app as app_module
    from google_auth_oauthlib.flow import Flow

    client.get("/google/login")
    with client.session_transaction() as sess:
        state, verifier = sess["google_oauth"]["state"], sess["google_oauth"]["verifier"]

    seen = {}

    def fake_fetch_token(self, **kwargs):
        seen["verifier"] = self.code_verifier
        self.oauth2session.token = {
            "access_token": "at", "refresh_token": "rt", "token_type": "Bearer",
            "expires_at": 9999999999, "scope": " ".join(app_module.GOOGLE_SCOPES),
        }
        return self.oauth2session.token

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"sub": "g-123", "email": "Person@Gmail.com", "email_verified": True}

    monkeypatch.setattr(Flow, "fetch_token", fake_fetch_token)
    monkeypatch.setattr(app_module.requests, "get", lambda *a, **k: FakeResp())

    resp = client.get(f"/google/callback?state={state}&code=abc")
    assert seen["verifier"] == verifier
    assert resp.status_code == 302
    from models import EmailAccount
    with app.app_context():
        acct = EmailAccount.query.filter_by(provider="google", provider_subject="g-123").one()
        assert acct.email == "person@gmail.com"


def test_microsoft_login_uses_pkce(client):
    resp = client.get("/microsoft/login")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(resp.headers["Location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert "Mail.Read" in query["scope"][0] and "Mail.ReadWrite" not in query["scope"][0]


# ---------------------------------------------------------------------------
# OAuth: identity resolution
# ---------------------------------------------------------------------------

def test_finish_oauth_creates_master(app):
    from app import _finish_oauth
    from models import Master

    with anon(app):
        resp = _finish_oauth("google", "sub-1", "new-user@example.com", "refresh-1")
        assert resp.status_code == 302 and resp.headers["Location"].endswith("/code")

        master = Master.query.filter_by(primary_email="new-user@example.com").one()
        assert master.subscribed is False
        assert [(a.provider, a.provider_subject) for a in master.email_accounts] == [("google", "sub-1")]


def test_finish_oauth_reuses_identity_and_keeps_token_without_new_one(app):
    from app import _finish_oauth
    from functions.encryption import decrypt_token
    from models import Master

    with anon(app):
        _finish_oauth("google", "sub-2", "reuse@example.com", "tok-1")
    with anon(app):
        # Returning users sign in without the consent screen: no refresh token.
        _finish_oauth("google", "sub-2", "reuse@example.com", None)

    masters = Master.query.filter_by(primary_email="reuse@example.com").all()
    assert len(masters) == 1
    assert decrypt_token(masters[0].email_accounts[0].oauth_token) == "tok-1"


def test_finish_oauth_new_identity_without_refresh_token_forces_consent(app):
    from app import _finish_oauth
    from models import Master

    with anon(app):
        resp = _finish_oauth("google", "sub-3", "nort@example.com", None)
    assert resp.status_code == 302
    assert "consent=1" in resp.headers["Location"]
    assert Master.query.filter_by(primary_email="nort@example.com").first() is None


def test_microsoft_identity_cannot_claim_google_account_by_email(app, client):
    """nOAuth regression: a Microsoft tenant can set `mail` to any address."""
    from app import OAuthFlowError, _finish_oauth
    from models import EmailAccount

    with anon(app):
        _finish_oauth("google", "g-victim", "victim@gmail.com", "victim-token")

    with anon(app):
        with pytest.raises(OAuthFlowError) as err:
            _finish_oauth("microsoft", "ms-attacker", "victim@gmail.com", "attacker-token")
    assert err.value.status == 409
    assert EmailAccount.query.filter_by(provider="microsoft").count() == 0


def test_legacy_account_without_subject_is_adopted(app):
    from app import _finish_oauth, db as _db
    from functions.encryption import encrypt_token
    from models import EmailAccount, Master

    with app.app_context():
        m = Master(primary_email="legacy@example.com", subscribed=True, temp=False)
        _db.session.add(m)
        _db.session.commit()
        _db.session.add(EmailAccount(email="legacy@example.com", oauth_token=encrypt_token("old"),
                                     provider="google", master=m))
        _db.session.commit()
        mid = m.id

    with anon(app):
        resp = _finish_oauth("google", "g-legacy", "legacy@example.com", "new")
        assert resp.headers["Location"].endswith("/list")
    acct = EmailAccount.query.filter_by(email="legacy@example.com").one()
    assert acct.provider_subject == "g-legacy" and acct.master_id == mid


def test_linking_inbox_owned_by_someone_else_is_refused(app, client):
    from app import OAuthFlowError, _finish_oauth
    from flask_login import login_user
    from models import Master

    for sub, email, tok in (("g-owner", "owner@example.com", "t1"), ("g-other", "other@example.com", "t2")):
        with anon(app):
            _finish_oauth("google", sub, email, tok)

    with anon(app):
        login_user(Master.query.filter_by(primary_email="other@example.com").one())
        with pytest.raises(OAuthFlowError) as err:
            _finish_oauth("google", "g-owner", "owner@example.com", "t3")
    assert err.value.status == 409


def test_reconnect_clears_needs_reauth(app):
    from app import _finish_oauth, db as _db
    from models import EmailAccount

    with anon(app):
        _finish_oauth("microsoft", "ms-1", "work@corp.com", "t1")
    acct = EmailAccount.query.filter_by(provider_subject="ms-1").one()
    acct.needs_reauth = True
    _db.session.commit()

    with anon(app):
        resp = _finish_oauth("microsoft", "ms-1", "work@corp.com", None)
        assert "consent=1" in resp.headers["Location"]
    with anon(app):
        _finish_oauth("microsoft", "ms-1", "work@corp.com", "t2")
    assert EmailAccount.query.filter_by(provider_subject="ms-1").one().needs_reauth is False


# ---------------------------------------------------------------------------
# Token lifecycle
# ---------------------------------------------------------------------------

class _FakeTokenResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def _make_account(app, provider="google", token="rt"):
    from app import db as _db
    from functions.encryption import encrypt_token
    from models import EmailAccount, Master
    m = Master(primary_email=f"{provider}-{token}@example.com", subscribed=True, temp=False)
    _db.session.add(m)
    _db.session.commit()
    a = EmailAccount(email=m.primary_email, oauth_token=encrypt_token(token), provider=provider, master=m)
    _db.session.add(a)
    _db.session.commit()
    return a


def test_refresh_dead_grant_marks_needs_reauth(app, monkeypatch):
    from functions import refresh_token as rt
    acct = _make_account(app)
    monkeypatch.setattr(rt.requests, "post", lambda *a, **k: _FakeTokenResponse(400, {"error": "invalid_grant"}))
    with pytest.raises(rt.TokenRefreshError) as err:
        rt.refresh(acct)
    assert err.value.reauth_required and acct.needs_reauth is True


def test_refresh_transient_failure_does_not_mark_reauth(app, monkeypatch):
    from functions import refresh_token as rt

    def boom(*a, **k):
        raise rt.requests.ConnectionError("down")

    acct = _make_account(app, token="t2")
    monkeypatch.setattr(rt.requests, "post", boom)
    with pytest.raises(rt.TokenRefreshError) as err:
        rt.refresh(acct)
    assert not err.value.reauth_required and acct.needs_reauth is False


def test_refresh_stores_rotated_microsoft_token(app, monkeypatch):
    from functions import refresh_token as rt
    from functions.encryption import decrypt_token
    acct = _make_account(app, provider="microsoft", token="old")
    monkeypatch.setattr(rt.requests, "post", lambda *a, **k: _FakeTokenResponse(
        200, {"access_token": "at", "refresh_token": "new"}))
    assert rt.refresh(acct) == "at"
    assert decrypt_token(acct.oauth_token) == "new"


def test_undecryptable_token_marks_needs_reauth(app):
    from functions import refresh_token as rt
    acct = _make_account(app, token="t3")
    acct.oauth_token = "not-a-fernet-token"
    with pytest.raises(rt.TokenRefreshError):
        rt.refresh(acct)
    assert acct.needs_reauth is True


def test_encryption_roundtrip_and_rotation(monkeypatch):
    from cryptography.fernet import Fernet, MultiFernet
    from functions import encryption
    assert encryption.decrypt_token(encryption.encrypt_token("hello")) == "hello"

    old = encryption.encrypt_token("secret")
    new_key = Fernet(Fernet.generate_key())
    monkeypatch.setattr(encryption, "fernet", MultiFernet([new_key, *encryption.fernet._fernets]))
    rotated = encryption.rotate_token(old)
    assert new_key.decrypt(rotated.encode()) == b"secret"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_settings_page_renders(app, client):
    _login_as(app, client, accounts=[("me@example.com", "google"), ("work@corp.com", "microsoft")])
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert b"work@corp.com" in resp.data and b"Delivery" in resp.data


def test_settings_saves_time_and_timezone(app, client):
    _login_as(app, client)
    resp = client.post("/settings", data={"timezone": "America/New_York", "time": ["8:00 AM", "5:00 PM"]})
    assert resp.status_code == 302

    from models import Master
    with app.app_context():
        m = Master.query.filter_by(primary_email="me@example.com").one()
        assert m.timezone == "America/New_York"
        assert m.time == "8:00 AM,5:00 PM"


def test_settings_caps_at_three_times_and_rejects_junk(app, client):
    _login_as(app, client)
    client.post("/settings", data={"timezone": "UTC",
                                   "time": ["1:00 AM", "25:99 XM", "2:00 AM", "3:00 AM", "4:00 AM"]})
    from models import Master
    with app.app_context():
        m = Master.query.filter_by(primary_email="me@example.com").one()
        assert m.time == "1:00 AM,2:00 AM,3:00 AM"


def test_settings_rejects_unknown_timezone(app, client):
    _login_as(app, client)
    client.post("/settings", data={"timezone": "Mars/Olympus", "time": ["1:00 AM"]})
    from models import Master
    with app.app_context():
        assert Master.query.filter_by(primary_email="me@example.com").one().timezone is None


def test_remove_secondary_account_revokes(app, client, monkeypatch):
    import app as app_module
    revoked = []
    monkeypatch.setattr(app_module, "revoke", lambda a: revoked.append(a.email))
    _, (primary_id, secondary_id) = _login_as(
        app, client, primary_email="owner@example.com",
        accounts=[("owner@example.com", "google"), ("second@example.com", "microsoft")])

    resp = client.post(f"/settings/accounts/{secondary_id}/remove")
    assert resp.status_code == 303
    assert revoked == ["second@example.com"]
    from app import db as _db
    from models import EmailAccount
    with app.app_context():
        assert _db.session.get(EmailAccount, secondary_id) is None


def test_cannot_remove_primary_account(app, client):
    _, (primary_id,) = _login_as(app, client, primary_email="only@example.com",
                                 accounts=[("only@example.com", "google")])
    client.post(f"/settings/accounts/{primary_id}/remove")
    from app import db as _db
    from models import EmailAccount
    with app.app_context():
        assert _db.session.get(EmailAccount, primary_id) is not None


def test_remove_account_cannot_touch_other_user(app, client):
    """IDOR regression."""
    from app import db as _db
    from functions.encryption import encrypt_token
    from models import EmailAccount, Master

    with app.app_context():
        victim = Master(primary_email="victim@x.com", subscribed=True, temp=False)
        _db.session.add(victim)
        _db.session.commit()
        va = EmailAccount(email="victim-extra@x.com", oauth_token=encrypt_token("x"),
                          provider="google", master=victim)
        _db.session.add(va)
        _db.session.commit()
        va_id = va.id

    _login_as(app, client, primary_email="attacker@x.com")
    resp = client.post(f"/settings/accounts/{va_id}/remove")
    assert resp.status_code == 404
    with app.app_context():
        assert _db.session.get(EmailAccount, va_id) is not None


def test_state_changing_posts_require_csrf(app, client):
    _, (acct_id,) = _login_as(app, client, primary_email="c@x.com",
                              accounts=[("extra@x.com", "google")])
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        assert client.post(f"/settings/accounts/{acct_id}/remove").status_code == 400
        assert client.post("/settings/delete", data={"confirm": "c@x.com"}).status_code == 400
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_delete_account_requires_typed_confirmation(app, client):
    uid, _ = _login_as(app, client, primary_email="keep@x.com")
    client.post("/settings/delete", data={"confirm": "wrong"})
    from app import db as _db
    from models import Master
    with app.app_context():
        assert _db.session.get(Master, uid) is not None


def test_delete_account_removes_everything(app, client, monkeypatch):
    import app as app_module
    revoked = []
    monkeypatch.setattr(app_module, "revoke", lambda a: revoked.append(a.email))
    uid, ids = _login_as(app, client, primary_email="bye@x.com",
                         accounts=[("bye@x.com", "google"), ("bye2@x.com", "microsoft")])
    resp = client.post("/settings/delete", data={"confirm": "BYE@x.com"})
    assert resp.status_code == 303
    assert sorted(revoked) == ["bye2@x.com", "bye@x.com"]
    from app import db as _db
    from models import EmailAccount, Master
    with app.app_context():
        assert _db.session.get(Master, uid) is None
        assert all(_db.session.get(EmailAccount, i) is None for i in ids)


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------

def _add_digest(app, master_id, items, days_ago=0):
    from datetime import datetime, timedelta, timezone
    from app import db as _db
    from models import Digest, DigestItem
    d = Digest(master_id=master_id, created_at=datetime.now(timezone.utc) - timedelta(days=days_ago))
    for action, done in items:
        d.items.append(DigestItem(account_email="me@example.com", action=action, sender="Sam",
                                  subject="Hi", done=done))
    _db.session.add(d)
    _db.session.commit()
    return [i.id for i in d.items]


def test_landing_is_reachable_when_signed_in(app, client):
    _login_as(app, client)
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Open your list" in resp.data and b"Join the beta" not in resp.data


def test_list_empty_state(app, client):
    uid, _ = _login_as(app, client)
    from app import db as _db
    from models import Master
    m = _db.session.get(Master, uid)
    m.time, m.timezone = "7:00 AM", "America/Denver"
    _db.session.commit()
    resp = client.get("/list")
    assert resp.status_code == 200
    assert b"on its way" in resp.data and b"at 7:00 AM" in resp.data


def test_list_shows_latest_and_open_earlier_items(app, client):
    uid, _ = _login_as(app, client)
    _add_digest(app, uid, [("Old open thing", False), ("Old done thing", True)], days_ago=2)
    _add_digest(app, uid, [("Send the deck", False), ("Book the room", True)])
    html = client.get("/list").data.decode()
    assert "Send the deck" in html and "1 of 2 done" in html
    assert "Still open from earlier" in html and "Old open thing" in html
    assert "Old done thing" not in html


def test_toggle_item(app, client):
    uid, _ = _login_as(app, client)
    (item_id,) = _add_digest(app, uid, [("Call Sam", False)])
    resp = client.post(f"/list/items/{item_id}/toggle", data={"done": "1"},
                       headers={"Accept": "application/json"})
    assert resp.get_json() == {"id": item_id, "done": True}
    resp = client.post(f"/list/items/{item_id}/toggle", data={"done": "0"})
    assert resp.status_code == 303
    from app import db as _db
    from models import DigestItem
    assert _db.session.get(DigestItem, item_id).done is False


def test_toggle_other_users_item_is_404(app, client):
    from app import db as _db
    from models import Master
    victim = Master(primary_email="v@x.com", subscribed=True, temp=False)
    _db.session.add(victim)
    _db.session.commit()
    (item_id,) = _add_digest(app, victim.id, [("Private", False)])
    _login_as(app, client, primary_email="a@x.com")
    assert client.post(f"/list/items/{item_id}/toggle", data={"done": "1"}).status_code == 404
    from models import DigestItem
    assert _db.session.get(DigestItem, item_id).done is False


def test_scheduler_stores_list_and_purges_old(app, monkeypatch):
    from app import db as _db
    from functions import scheduler
    from functions.encryption import encrypt_token
    from models import Digest, EmailAccount, Master

    m = Master(primary_email="s@x.com", subscribed=True, temp=False, timezone="UTC", time="7:00 AM")
    _db.session.add(m)
    _db.session.commit()
    _db.session.add(EmailAccount(email="s@x.com", oauth_token=encrypt_token("rt"), provider="google", master=m))
    _db.session.commit()
    _add_digest(app, m.id, [("Ancient", False)], days_ago=30)

    monkeypatch.setattr(scheduler, "refresh", lambda a: "at")
    monkeypatch.setattr(scheduler, "get_emails", lambda *a, **k: [
        {"from": "Sam <sam@x.com>", "subject": "Deck", "body": "Please send the deck"}])
    monkeypatch.setattr(scheduler, "get_an_action", lambda body: "- Send Sam the deck")
    monkeypatch.setattr(scheduler, "_send_html_email", lambda *a, **k: True)

    result = scheduler.send_email_summary_for_user(m, "https://mailmind.test")
    assert result["success"]
    digests = Digest.query.filter_by(master_id=m.id).all()
    assert len(digests) == 1  # the 30-day-old one was purged
    assert digests[0].delivered is True
    assert [i.action for i in digests[0].items] == ["Send Sam the deck"]


# ---------------------------------------------------------------------------
# Beta code & billing
# ---------------------------------------------------------------------------

def _make_code(app, code="FRIENDS1", **kw):
    from app import db as _db
    from models import AccessCode
    with app.app_context():
        c = AccessCode(code=code, **kw)
        _db.session.add(c)
        _db.session.commit()
        return c.id


def _master(app, email):
    from models import Master
    return Master.query.filter_by(primary_email=email).one()


def test_invite_code_grants_free_access_forever(app, client):
    _make_code(app, "FRIENDS1", max_uses=2)
    _login_as(app, client, primary_email="beta@x.com", subscribed=False)
    assert client.get("/list").status_code == 302  # no access yet

    resp = client.post("/code", data={"code": "frie-nds1 "})  # case, spaces and dashes don't matter
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/settings")
    m = _master(app, "beta@x.com")
    assert m.comped_forever and m.has_access and not m.subscribed
    assert m.access_code.uses == 1
    assert client.get("/list").status_code == 200
    assert b"Free access" in client.get("/settings").data


def test_used_up_off_and_unknown_codes_are_rejected(app, client):
    from datetime import datetime, timedelta, timezone
    _make_code(app, "ONCEONLY", max_uses=1, uses=1)
    _make_code(app, "SWITCHEDOFF", active=False)
    _make_code(app, "OLDCODE", expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    _login_as(app, client, primary_email="nope@x.com", subscribed=False)
    for code, words in (("ONCEONLY", b"already been used"), ("SWITCHEDOFF", b"switched off"),
                        ("OLDCODE", b"expired"), ("guess", b"didn&#39;t work")):
        resp = client.post("/code", data={"code": code})
        assert resp.status_code == 400 and words in resp.data, code
    assert not _master(app, "nope@x.com").has_access


def test_timed_code_expires(app, client):
    from datetime import datetime, timedelta, timezone
    from app import db as _db
    _make_code(app, "TRIAL30", access_days=30)
    _login_as(app, client, primary_email="t@x.com", subscribed=False)
    client.post("/code", data={"code": "TRIAL30"})
    m = _master(app, "t@x.com")
    left = m.comp_until.replace(tzinfo=m.comp_until.tzinfo or timezone.utc) - datetime.now(timezone.utc)
    assert timedelta(days=29) < left <= timedelta(days=30) and not m.comped_forever

    m.comp_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    _db.session.commit()
    assert not m.has_access
    assert client.get("/list").headers["Location"].endswith("/subscribe")


def test_invite_link_survives_sign_in(app, client):
    _make_code(app, "LINKCODE")
    resp = client.get("/i/linkcode")
    assert resp.headers["Location"].endswith("/login")
    assert b"invited" in client.get("/login").data
    _login_as(app, client, primary_email="link@x.com", subscribed=False)
    page = client.get("/code").data
    assert b'value="LINK-CODE"' in page and b"invite is" in page


def test_stripe_cancellation_leaves_free_access_alone(app, client, monkeypatch):
    import app as app_module
    from app import db as _db
    from models import FOREVER, Master
    m = Master(primary_email="both@x.com", subscribed=True, temp=False, stripe_customer_id="cus_9",
               comp_until=FOREVER)
    _db.session.add(m)
    _db.session.commit()
    event = {"type": "customer.subscription.deleted", "data": {"object": {"customer": "cus_9", "status": "canceled"}}}
    monkeypatch.setattr(app_module.stripe.Webhook, "construct_event", lambda **k: event)
    assert client.post("/webhook", data="{}", headers={"stripe-signature": "t=1,v1=x"}).status_code == 200
    _db.session.refresh(m)
    assert not m.subscribed and m.has_access


def test_admin_codes_page_is_admin_only(app, client, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@x.com")
    _login_as(app, client, primary_email="someone@x.com")
    assert client.get("/admin/codes").status_code == 404
    assert client.post("/admin/codes", data={"note": "x"}).status_code == 404


def test_admin_creates_toggles_and_revokes(app, client, monkeypatch):
    from app import db as _db
    from models import AccessCode
    monkeypatch.setenv("ADMIN_EMAILS", "Boss@x.com, other@x.com")
    _login_as(app, client, primary_email="boss@x.com")
    assert b"Invite codes" in client.get("/settings").data

    resp = client.post("/admin/codes", data={"note": "Sam", "max_uses": "3", "access_days": "", "valid_days": "7"})
    assert resp.status_code == 303
    code = AccessCode.query.one()
    assert len(code.code) == 8 and code.max_uses == 3 and code.access_days is None and code.expires_at
    page = client.get("/admin/codes").data
    assert f"/i/{code.code}".encode() in page and b"Sam" in page

    assert client.post("/admin/codes", data={"code": "no!"}).status_code == 303
    assert client.post("/admin/codes", data={"max_uses": "lots"}).status_code == 303
    client.post("/admin/codes", data={"code": "vip-2026"})
    assert AccessCode.query.filter_by(code="VIP2026").one()
    assert AccessCode.query.count() == 2

    client.post(f"/admin/codes/{code.id}/toggle")
    _db.session.refresh(code)
    assert code.active is False

    code.active = True
    _db.session.commit()
    guest_client = app.test_client()
    uid, _ = _login_as(app, guest_client, primary_email="guest@x.com", subscribed=False)
    guest_client.post("/code", data={"code": code.code})
    assert _master(app, "guest@x.com").comped
    from flask import g
    g.pop("_login_user", None)
    assert client.post(f"/admin/people/{uid}/revoke").status_code == 303
    guest = _master(app, "guest@x.com")
    _db.session.refresh(guest)
    assert not guest.has_access


def test_checkout_ignores_client_supplied_price(app, client, monkeypatch):
    import app as app_module
    seen = {}

    class Prices:
        data = [type("P", (), {"id": "price_1"})()]

    def fake_list(lookup_keys, **kw):
        seen["keys"] = lookup_keys
        return Prices()

    monkeypatch.setattr(app_module.stripe.Price, "list", fake_list)
    monkeypatch.setattr(app_module.stripe.Customer, "create", lambda **k: type("C", (), {"id": "cus_1"})())
    monkeypatch.setattr(app_module.stripe.checkout.Session, "create",
                        lambda **k: type("S", (), {"url": "https://stripe.test/pay"})())
    _login_as(app, client, primary_email="pay@x.com", subscribed=False)
    resp = client.post("/create-checkout-session", data={"lookup_key": "cheap_price", "accept_tos": "1"})
    assert resp.status_code == 303
    assert seen["keys"] == [app_module.STRIPE_PRICE_LOOKUP_KEY]


def test_webhook_rejects_missing_signature(client):
    resp = client.post("/webhook", data=json.dumps({"type": "checkout.session.completed"}))
    assert resp.status_code in (400, 500)


def test_webhook_rejects_bad_signature(client):
    resp = client.post("/webhook",
                       data=json.dumps({"type": "checkout.session.completed"}),
                       headers={"stripe-signature": "t=1,v1=deadbeef"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Digest pipeline
# ---------------------------------------------------------------------------

def test_digest_emails_are_never_read_back():
    from functions.get_emails import DIGEST_SUBJECT_PREFIX, is_digest
    assert is_digest(f"{DIGEST_SUBJECT_PREFIX}: 3 things for Tuesday")
    # The subject the old app actually sent (note the hyphen).
    assert is_digest("Daily To-Do List from MailMind for October 06, 2026")
    assert is_digest("anything", {"X-MailMind-Digest": "1"})
    assert not is_digest("Proposal feedback")


def test_split_actions_handles_bullets_and_no_action():
    from functions.scheduler import _split_actions
    assert _split_actions("- Send the deck\n- Book the room") == ["Send the deck", "Book the room"]
    assert _split_actions("No action") == []
    assert _split_actions("1. Call Sam") == ["Call Sam"]


def test_digest_render_escapes_email_content():
    from functions.scheduler import _render_digest
    html = _render_digest(
        [{"account_email": "a@x.com",
          "items": [{"action": "<script>x</script>", "from": "Sam", "subject": "Hi", "calendar_url": None}]}],
        ["b@x.com"], "a@x.com", "https://mailmind.test", "Tuesday, October 6")
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html
    assert "Reconnect it" in html and "1 thing needs you." in html and "Here&rsquo;s your list." in html and "morning" not in html


def test_microsoft_digest_is_sent_through_graph(monkeypatch):
    from functions import scheduler
    calls = {}

    class Resp:
        status_code = 202

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.update(url=url, json=json, headers=headers)
        return Resp()

    monkeypatch.setattr(scheduler.requests, "post", fake_post)
    monkeypatch.setattr(scheduler.smtplib, "SMTP", lambda *a, **k: pytest.fail("SMTP used for Microsoft"))
    assert scheduler._send_html_email("me@corp.com", "at", "microsoft", "Subj", "<p>hi</p>")
    assert calls["url"].endswith("/me/sendMail")
    assert calls["json"]["message"]["toRecipients"][0]["emailAddress"]["address"] == "me@corp.com"


def test_calendar_deep_link_provider_split():
    from functions.scheduler import _calendar_link
    g = _calendar_link("google", "Meet Sarah Tuesday")
    m = _calendar_link("microsoft", "Meet Sarah Tuesday")
    assert "calendar.google.com" in g and "text=Meet%20Sarah%20Tuesday" in g
    assert "outlook.live.com" in m and "subject=Meet%20Sarah%20Tuesday" in m


def test_scheduler_time_match_boundaries():
    from datetime import datetime, timezone
    from functions.scheduler import _is_time_match
    now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    assert _is_time_match("7:00 AM", now, "America/New_York") is True
    assert _is_time_match("9:00 AM", now, "America/New_York") is False


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


def test_alias_hosts_redirect_to_canonical_domain(client, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "PRODUCTION", True)
    monkeypatch.setattr(app_module, "DOMAIN", "https://mailmind.dev")
    monkeypatch.setattr(app_module, "CANONICAL_HOST", "mailmind.dev")

    resp = client.get("/login?next=%2Flist", base_url="https://mailmind.fly.dev")
    assert resp.status_code == 301
    assert resp.headers["Location"] == "https://mailmind.dev/login?next=%2Flist"
    assert client.get("/", base_url="https://www.mailmind.dev").headers["Location"] == "https://mailmind.dev/"

    # Canonical host, machine-address health checks, and POSTs are untouched.
    assert client.get("/", base_url="https://mailmind.dev").status_code == 200
    assert client.get("/", base_url="http://172.19.0.2:8080").status_code == 200
    assert client.post("/inbound/mailgun", base_url="https://mailmind.fly.dev").status_code != 301
