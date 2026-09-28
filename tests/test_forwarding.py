"""Email forwarding: inbound webhook, queue processing, delivery, and basic sign-in."""
import base64
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_smoke import _login_as, anon

TEMPLATE = "hash123+{token}@inbound.postmarkapp.com"
AUTH = {"Authorization": "Basic " + base64.b64encode(b"postmark:s3cret").decode()}


@pytest.fixture
def forwarding_env(monkeypatch):
    monkeypatch.setenv("INBOUND_ADDRESS_TEMPLATE", TEMPLATE)
    monkeypatch.setenv("INBOUND_WEBHOOK_USER", "postmark")
    monkeypatch.setenv("INBOUND_WEBHOOK_PASSWORD", "s3cret")


def _forwarding_user(app, email="fwd@example.com", token="abc123def456"):
    from app import db as _db
    from models import ForwardingAddress, Master
    m = Master(primary_email=email, subscribed=True, temp=False, timezone="UTC", time="7:00 AM")
    _db.session.add(m)
    _db.session.commit()
    _db.session.add(ForwardingAddress(master=m, token=token))
    _db.session.commit()
    return m


def _payload(token="abc123def456", **overrides):
    p = {
        "From": "sarah@client.com", "FromFull": {"Email": "sarah@client.com", "Name": "Sarah Chen"},
        "To": f"hash123+{token}@inbound.postmarkapp.com", "MailboxHash": token,
        "Subject": "Proposal feedback", "TextBody": "Could you send the final version by Thursday?",
        "Headers": [{"Name": "X-Forwarded-For", "Value": f"me@gmail.com hash123+{token}@inbound.postmarkapp.com"},
                    {"Name": "Message-ID", "Value": "<m1@client.com>"}],
    }
    p.update(overrides)
    return p


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

def test_webhook_is_off_until_configured(client):
    assert client.post("/inbound/postmark", json=_payload()).status_code == 404


def test_webhook_requires_basic_auth(client, forwarding_env):
    assert client.post("/inbound/postmark", json=_payload()).status_code == 401
    bad = {"Authorization": "Basic " + base64.b64encode(b"postmark:nope").decode()}
    assert client.post("/inbound/postmark", json=_payload(), headers=bad).status_code == 401


def test_webhook_queues_forwarded_mail(app, client, forwarding_env):
    from models import InboundEmail
    m = _forwarding_user(app)
    resp = client.post("/inbound/postmark", json=_payload(), headers=AUTH)
    assert resp.get_json() == {"status": "queued"}
    row = InboundEmail.query.filter_by(master_id=m.id).one()
    assert row.source_email == "me@gmail.com"
    assert row.sender == "Sarah Chen <sarah@client.com>"
    assert "final version" in row.body
    assert m.forwarding.last_received_at is not None

    # Same Message-ID again (e.g. a Postmark retry) isn't queued twice.
    client.post("/inbound/postmark", json=_payload(), headers=AUTH)
    assert InboundEmail.query.filter_by(master_id=m.id).count() == 1


def test_webhook_matches_recipient_without_mailbox_hash(app, client, monkeypatch, forwarding_env):
    monkeypatch.setenv("INBOUND_ADDRESS_TEMPLATE", "{token}@in.mailmind.test")
    from models import InboundEmail
    m = _forwarding_user(app, token="zzz999yyy888")
    payload = _payload(token="zzz999yyy888", MailboxHash="", To="zzz999yyy888@in.mailmind.test",
                       ToFull=[{"Email": "ZZZ999YYY888@in.mailmind.test"}], Headers=[])
    assert client.post("/inbound/postmark", json=payload, headers=AUTH).get_json()["status"] == "queued"
    assert InboundEmail.query.filter_by(master_id=m.id).one().source_email == "fwd@example.com"


def test_webhook_ignores_unknown_tokens_and_digests(app, client, forwarding_env):
    from models import InboundEmail
    _forwarding_user(app)
    assert client.post("/inbound/postmark", json=_payload(token="nobody"), headers=AUTH).get_json()["status"] == "ignored"
    digest = _payload(Subject="Your MailMind list: 3 things for Tuesday")
    assert client.post("/inbound/postmark", json=digest, headers=AUTH).get_json()["status"] == "ignored"
    assert InboundEmail.query.count() == 0


def test_gmail_confirmation_code_is_captured_and_shown(app, client, forwarding_env):
    m = _forwarding_user(app)
    payload = _payload(
        From="forwarding-noreply@google.com", FromFull={"Email": "forwarding-noreply@google.com"},
        Subject="(#482913657) Gmail Forwarding Confirmation - Receive Mail from me@gmail.com",
        TextBody="me@gmail.com has requested to automatically forward mail...\nConfirmation code: 482913657",
    )
    assert client.post("/inbound/postmark", json=payload, headers=AUTH).get_json()["status"] == "confirmation"
    assert m.forwarding.confirmation_code == "482913657"
    assert m.forwarding.confirmation_for == "me@gmail.com"

    with client.session_transaction() as sess:
        sess["_user_id"] = str(m.id)
    html = client.get("/settings").data.decode()
    assert "482913657" in html and "hash123+abc123def456@inbound.postmarkapp.com" in html


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_settings_creates_address_and_rotates_it(app, client, forwarding_env):
    uid, _ = _login_as(app, client)
    html = client.get("/settings").data.decode()
    from app import db as _db
    from models import Master
    token = _db.session.get(Master, uid).forwarding.token
    assert f"hash123+{token}@inbound.postmarkapp.com" in html

    assert client.post("/settings/forwarding/rotate").status_code == 303
    assert _db.session.get(Master, uid).forwarding.token != token


def test_settings_hides_forwarding_when_off(app, client):
    _login_as(app, client)
    assert b'id="forwarding"' not in client.get("/settings").data


def test_list_prompts_setup_for_new_forwarding_users(app, client, forwarding_env):
    _login_as(app, client)
    assert b"Set up forwarding" in client.get("/list").data


# ---------------------------------------------------------------------------
# Queue processing and delivery
# ---------------------------------------------------------------------------

def _queue(app, master, body="Please send the deck", received_at=None, source="me@gmail.com"):
    from app import db as _db
    from models import InboundEmail
    row = InboundEmail(master_id=master.id, source_email=source, sender="Sam", subject="Deck", body=body,
                       received_at=received_at or datetime.now(timezone.utc))
    _db.session.add(row)
    _db.session.commit()
    return row.id


def test_queue_turns_mail_into_pending_items_and_deletes_bodies(app, monkeypatch):
    from functions import scheduler
    from models import InboundEmail, PendingItem
    m = _forwarding_user(app)
    _queue(app, m)
    monkeypatch.setattr(scheduler, "get_an_action", lambda body: "- Send Sam the deck\n- Book a meeting")
    assert scheduler.process_inbound_queue() == 1
    assert InboundEmail.query.count() == 0
    items = PendingItem.query.filter_by(master_id=m.id).order_by(PendingItem.id).all()
    assert [i.action for i in items] == ["Send Sam the deck", "Book a meeting"]
    assert items[1].calendar_url  # meetings get a calendar link


def test_queue_retries_failures_then_drops_old_rows(app, monkeypatch):
    from functions import scheduler
    from models import InboundEmail

    def boom(body):
        raise RuntimeError("model down")

    m = _forwarding_user(app)
    fresh = _queue(app, m)
    old = _queue(app, m, received_at=datetime.now(timezone.utc) - timedelta(hours=30))
    monkeypatch.setattr(scheduler, "get_an_action", boom)
    scheduler.process_inbound_queue()
    remaining = [r.id for r in InboundEmail.query.all()]
    assert remaining == [fresh] and old not in remaining


def test_forwarding_only_user_gets_list_via_postmark(app, monkeypatch):
    from app import db as _db
    from functions import scheduler
    from models import Digest, PendingItem
    m = _forwarding_user(app)
    _db.session.add(PendingItem(master_id=m.id, source_email="me@gmail.com", action="Send the deck",
                                sender="Sam", subject="Deck"))
    _db.session.commit()

    sent = {}
    monkeypatch.setenv("POSTMARK_SERVER_TOKEN", "pm-token")
    monkeypatch.setenv("DIGEST_FROM_ADDRESS", "MailMind <list@mailmind.test>")
    monkeypatch.setattr(scheduler, "send_via_postmark", lambda to, subject, html, header: sent.update(to=to) or True)
    result = scheduler.send_email_summary_for_user(m, "https://mailmind.test")

    assert result["success"] and sent["to"] == "fwd@example.com"
    digest = Digest.query.filter_by(master_id=m.id).one()
    assert digest.delivered and [i.action for i in digest.items] == ["Send the deck"]
    assert digest.items[0].account_email == "me@gmail.com"
    assert PendingItem.query.filter_by(master_id=m.id).count() == 0


def test_postmark_send_payload(monkeypatch):
    from functions import forwarding
    monkeypatch.setenv("POSTMARK_SERVER_TOKEN", "pm-token")
    monkeypatch.setenv("DIGEST_FROM_ADDRESS", "MailMind <list@mailmind.test>")
    calls = {}

    class Resp:
        status_code = 200

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.update(url=url, headers=headers, json=json)
        return Resp()

    monkeypatch.setattr(forwarding.requests, "post", fake_post)
    assert forwarding.send_via_postmark("me@x.com", "Subj", "<p>hi</p>", "X-MailMind-Digest")
    assert calls["headers"]["X-Postmark-Server-Token"] == "pm-token"
    assert calls["json"]["To"] == "me@x.com" and calls["json"]["From"] == "MailMind <list@mailmind.test>"


# ---------------------------------------------------------------------------
# Sign-in without mailbox access
# ---------------------------------------------------------------------------

def test_google_signin_uses_basic_scopes_when_forwarding_is_on(client, forwarding_env):
    resp = client.get("/google/login")
    scope = urllib.parse.parse_qs(urllib.parse.urlparse(resp.headers["Location"]).query)["scope"][0]
    assert "mail.google.com" not in scope and "userinfo.email" in scope
    connect = client.get("/google/login?connect=1")
    assert "mail.google.com" in urllib.parse.parse_qs(urllib.parse.urlparse(connect.headers["Location"]).query)["scope"][0]


def test_google_signin_requests_mailbox_when_forwarding_is_off(client):
    resp = client.get("/google/login")
    assert "mail.google.com" in urllib.parse.unquote(resp.headers["Location"])


def test_finish_signin_creates_account_and_reuses_identity(app):
    from app import _finish_signin
    from models import Identity, Master
    with anon(app):
        resp = _finish_signin("google", "g-new", "new@example.com")
        assert resp.headers["Location"].endswith("/code")
    with anon(app):
        _finish_signin("google", "g-new", "new@example.com")
    master = Master.query.filter_by(primary_email="new@example.com").one()
    assert Identity.query.filter_by(master_id=master.id).count() == 1
    assert master.email_accounts == []


def test_finish_signin_finds_existing_mailbox_user(app):
    from app import _finish_oauth, _finish_signin
    from models import Master
    with anon(app):
        _finish_oauth("google", "g-old", "old@example.com", "rt")
    with anon(app):
        _finish_signin("google", "g-old", "old@example.com")
    assert Master.query.filter_by(primary_email="old@example.com").count() == 1


def test_finish_signin_refuses_email_owned_by_other_provider(app):
    from app import OAuthFlowError, _finish_oauth, _finish_signin
    with anon(app):
        _finish_oauth("microsoft", "ms-1", "shared@corp.com", "rt")
    with anon(app):
        with pytest.raises(OAuthFlowError) as err:
            _finish_signin("google", "g-2", "shared@corp.com")
    assert err.value.status == 409


def test_deleting_account_removes_forwarding_data(app, client, monkeypatch):
    import app as app_module
    from models import ForwardingAddress, InboundEmail, PendingItem
    monkeypatch.setattr(app_module, "revoke", lambda a: None)
    m = _forwarding_user(app, email="gone@x.com")
    _queue(app, m)
    from app import db as _db
    _db.session.add(PendingItem(master_id=m.id, source_email="a@x.com", action="x"))
    _db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(m.id)
    assert client.post("/settings/delete", data={"confirm": "gone@x.com"}).status_code == 303
    assert ForwardingAddress.query.count() == 0
    assert InboundEmail.query.count() == 0 and PendingItem.query.count() == 0
