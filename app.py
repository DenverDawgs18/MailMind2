"""
MailMind is a productivity service, not an email client. This module wires up
the public surface:

    /                         landing page
    /login                    sign in with Google or Microsoft
    /request_access, /beta    beta waitlist form + thank-you page
    /google/login|callback    Google OAuth (sign-in, linking, reconnecting)
    /microsoft/login|callback Microsoft OAuth
    /list                     today's list (check items off)
    /settings                 forwarding, inboxes, delivery schedule, billing, account
    /inbound/mailgun          forwarded mail (Mailgun route forward)
    /settings/accounts/<id>/remove
    /settings/delete          delete the MailMind account
    /subscribe, /create-checkout-session, /create-portal-session, /webhook
    /code                     beta access code
    /contact, /termsandprivacy
    /logout                   (POST)
"""
import base64
import functools
import hashlib
import hmac
import logging
import os
import re
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytz
import requests
import stripe
from flask import (
    Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for,
)
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_migrate import Migrate
from flask_session import Session
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFError, CSRFProtect
from google_auth_oauthlib.flow import Flow
from redis import Redis
from sqlalchemy.orm import DeclarativeBase

from functions.production import production

# ---------------------------------------------------------------------------
# Environment / configuration
# ---------------------------------------------------------------------------

PRODUCTION = production()

if not PRODUCTION:
    from dotenv import load_dotenv
    load_dotenv()
    DOMAIN = os.getenv("DOMAIN", "http://localhost:5000")
    if DOMAIN.startswith("http://"):
        # Allow the OAuth libraries to run against a plain-http localhost.
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
else:
    DOMAIN = os.getenv("DOMAIN", "https://mailmind.dev")
DOMAIN = DOMAIN.rstrip("/")
CANONICAL_HOST = urllib.parse.urlsplit(DOMAIN).netloc

# Google's granular consent lets people untick scopes; we check what was
# actually granted ourselves instead of letting oauthlib raise.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, static_url_path='/static')

if PRODUCTION:
    secret_key = os.getenv("SECRET_KEY")
    if not secret_key:
        raise RuntimeError("SECRET_KEY env var is required in production")
    app.config["SECRET_KEY"] = secret_key
else:
    try:
        app.config.from_pyfile('config.py')
    except (FileNotFoundError, OSError):
        app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-do-not-use-in-prod")

app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
if PRODUCTION:
    app.config['SESSION_COOKIE_SECURE'] = True

if PRODUCTION:
    DATABASE_URL = os.getenv('DATABASE_URL')
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL env var is required in production")
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///database.db")


class Base(DeclarativeBase):
    pass


db = SQLAlchemy(model_class=Base)
db.init_app(app)
migrate = Migrate(app, db)

app.config["SESSION_PERMANENT"] = False
app.config["SESSION_USE_SIGNER"] = True

_TESTING = os.getenv("MAILMIND_TEST", "").strip() in ("1", "true", "yes")

if _TESTING:
    app.config["SESSION_TYPE"] = "filesystem"
    _redis_client = None
else:
    app.config["SESSION_TYPE"] = "redis"
    if PRODUCTION:
        _redis_client = Redis(
            host=os.getenv("REDIS_HOST", "fly-mailmind-redis.upstash.io"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD"),
        )
    else:
        _redis_client = Redis(host=os.getenv("REDIS_HOST", "localhost"), port=6379)
    app.config["SESSION_REDIS"] = _redis_client

Session(app)

csrf = CSRFProtect(app)


@app.before_request
def redirect_to_canonical_host():
    """
    Send page views on other hostnames (mailmind.fly.dev, www.) to DOMAIN, so
    sessions and OAuth callbacks all live on one host. POSTs (Stripe and
    Mailgun webhooks, forms) are left alone: a redirect would drop the body.
    """
    if not PRODUCTION or request.method not in ("GET", "HEAD"):
        return None
    host = request.host.lower()
    # Only known aliases: health checks arrive on the machine's own address.
    if host != CANONICAL_HOST and (host.endswith(".fly.dev") or host == "www." + CANONICAL_HOST):
        return redirect(DOMAIN + request.full_path.rstrip("?"), code=301)
    return None


@app.context_processor
def inject_globals():
    from flask_wtf.csrf import generate_csrf
    from functions.forwarding import forwarding_enabled as _fwd_on
    return {"csrf_token": generate_csrf, "DOMAIN": DOMAIN, "forwarding_on": _fwd_on(),
            "is_admin": is_admin(current_user)}


stripe.api_key = os.getenv("STRIPE_API_KEY")
STRIPE_PRICE_LOOKUP_KEY = os.getenv("STRIPE_PRICE_LOOKUP_KEY", "One_Month_of_MailMind-ae39e51")

from functions.stripe_setup import register as _register_stripe_setup  # noqa: E402
_register_stripe_setup(app, DOMAIN, STRIPE_PRICE_LOOKUP_KEY)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = None

app._redis_client = _redis_client

# ---------------------------------------------------------------------------
# Imports that need `app` / `db` first
# ---------------------------------------------------------------------------

from functions.encryption import encrypt_token  # noqa: E402
from functions.refresh_token import (  # noqa: E402
    GOOGLE_SCOPES, MICROSOFT_AUTH_URL, MICROSOFT_SCOPES, MICROSOFT_TOKEN_URL, revoke,
)
from functions.users import create_email, create_master  # noqa: E402
from functions.forwarding import (  # noqa: E402
    SIGNATURE_MAX_AGE_SECONDS, address_for, extract_token, forwarding_enabled, from_mailgun,
    gmail_confirmation, new_token, parse_message, verify_mailgun_signature,
)
from models import (  # noqa: E402
    FOREVER, AccessCode, Digest, DigestItem, EmailAccount, ForwardingAddress, Identity, InboundEmail,
    Master,
)

# ---------------------------------------------------------------------------
# OAuth configuration
# ---------------------------------------------------------------------------

GOOGLE_REDIRECT_URI = f"{DOMAIN}/google/callback"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_MAIL_SCOPE = "https://mail.google.com/"
# Sign-in only: non-sensitive scopes, no Google security assessment needed.
GOOGLE_SIGNIN_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email",
                        "https://www.googleapis.com/auth/userinfo.profile"]

google_client_config = {
    "web": {
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [GOOGLE_REDIRECT_URI],
    }
}

OUTLOOK_REDIRECT_URI = f"{DOMAIN}/microsoft/callback"
MICROSOFT_USERINFO_URL = "https://graph.microsoft.com/v1.0/me"
MICROSOFT_REQUIRED_SCOPES = {"mail.read", "mail.send"}

PROVIDER_NAMES = {"google": "Google", "microsoft": "Microsoft"}


@app.template_filter("provider_name")
def provider_name(provider):
    return PROVIDER_NAMES.get((provider or "").lower(), (provider or "").title())


@login_manager.user_loader
def load_user(id):
    try:
        return db.session.get(Master, int(id))
    except (TypeError, ValueError):
        return None


class OAuthFlowError(Exception):
    """A user-facing OAuth failure, rendered by the auth error page."""

    def __init__(self, title, message, provider=None, retry_consent=False, status=400, connect=False):
        super().__init__(message)
        self.title = title
        self.message = message
        self.provider = provider
        self.retry_consent = retry_consent
        self.status = status
        self.connect = connect


def _consent_url(provider):
    """Re-run a provider's mailbox connection with the consent screen forced."""
    return url_for(f"{provider}_login", consent=1, connect=1 if provider == "google" else None)


@app.errorhandler(OAuthFlowError)
def handle_oauth_error(err):
    retry_url = None
    if err.provider:
        retry_url = url_for(f"{err.provider}_login", consent=1 if err.retry_consent else None,
                            connect=1 if (err.connect or err.retry_consent) and err.provider == "google" else None)
    return render_template("auth_error.html", title=err.title, message=err.message,
                           provider=err.provider, retry_url=retry_url), err.status


def _pkce_pair():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _start_session_for(master):
    """Log in with a fresh session id (prevents session fixation)."""
    regenerate = getattr(app.session_interface, "regenerate", None)
    if callable(regenerate):
        try:
            regenerate(session)
        except Exception:  # pragma: no cover - best effort
            logger.debug("session regenerate unavailable", exc_info=True)
    login_user(master)


def _pop_oauth_state(key):
    stored = session.pop(key, None) or {}
    if not stored.get("state") or not hmac.compare_digest(stored["state"], request.args.get("state", "")):
        raise OAuthFlowError("That sign-in link expired",
                             "The sign-in request didn't match this browser session. Please try again.",
                             provider=key.split("_")[0])
    return stored


def _check_provider_error(provider):
    error = request.args.get("error")
    if not error:
        return
    if error in ("access_denied", "consent_required"):
        raise OAuthFlowError("Sign-in cancelled",
                             f"You didn't finish connecting your {PROVIDER_NAMES[provider]} account, "
                             "so nothing was changed.", provider=provider)
    logger.warning("%s OAuth error: %s", provider, error)
    raise OAuthFlowError("Couldn't connect", f"{PROVIDER_NAMES[provider]} returned an error. Please try again.",
                         provider=provider)


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/login')
def login():
    if current_user.is_authenticated:
        return redirect(url_for('todo_list'))
    return render_template('login.html', invite=session.get("invite_code"))


@app.route("/request_access")
def request_access():
    return render_template("request_access.html")


@app.route("/beta")
def beta():
    return render_template("beta.html")


@app.route("/contact")
def contact():
    return render_template("contact.html", sent=request.args.get("sent") == "1")


@app.route("/termsandprivacy")
def terms_and_privacy():
    return render_template("termsandprivacy.html")


@app.route('/logout', methods=["POST"])
@login_required
def logout():
    logout_user()
    session.clear()
    return redirect(url_for('index'))


# ---------------------------------------------------------------------------
# OAuth: Google
# ---------------------------------------------------------------------------

def _google_flow(scopes=GOOGLE_SCOPES, **kwargs):
    return Flow.from_client_config(
        google_client_config, scopes=scopes, redirect_uri=GOOGLE_REDIRECT_URI, **kwargs,
    )


def _google_mode():
    """
    "signin": name + email only (non-sensitive scopes, no Google security
    assessment); used when mail arrives by forwarding.
    "mailbox": full Gmail access to read and send directly.
    """
    if request.args.get("connect") or not forwarding_enabled():
        return "mailbox"
    return "signin"


@app.route("/google/login")
def google_login():
    """
    Sign in, connect a Gmail inbox directly (``?connect=1``), or reconnect.
    ``?consent=1`` forces Google's consent screen, which is the only way to
    get a new refresh token for an account that already granted access.
    """
    mode = _google_mode()
    flow = _google_flow(GOOGLE_SCOPES if mode == "mailbox" else GOOGLE_SIGNIN_SCOPES,
                        autogenerate_code_verifier=True)
    state = secrets.token_urlsafe(32)
    params = {"prompt": "consent" if request.args.get("consent") else "select_account", "state": state}
    if mode == "mailbox":
        params.update(access_type="offline", include_granted_scopes="true")
    else:
        params.update(access_type="online")
    auth_url, _ = flow.authorization_url(**params)
    # PKCE: the callback's Flow must present the same verifier.
    session["google_oauth"] = {"state": state, "verifier": flow.code_verifier, "mode": mode}
    return redirect(auth_url)


@app.route('/google/callback')
def google_callback():
    _check_provider_error("google")
    stored = _pop_oauth_state("google_oauth")
    mode = stored.get("mode", "mailbox")
    connect = mode == "mailbox"

    flow = _google_flow(GOOGLE_SCOPES if connect else GOOGLE_SIGNIN_SCOPES, state=stored["state"])
    flow.code_verifier = stored.get("verifier")

    authorization_response = request.url
    if PRODUCTION and authorization_response.startswith("http://"):
        authorization_response = "https://" + authorization_response[len("http://"):]

    try:
        flow.fetch_token(authorization_response=authorization_response)
    except Exception:
        logger.exception("Google OAuth token exchange failed")
        raise OAuthFlowError("Couldn't connect Google", "Google didn't accept the sign-in. Please try again.",
                             provider="google", connect=connect)

    credentials = flow.credentials
    if connect:
        granted = credentials.granted_scopes or []
        granted = set(granted.split() if isinstance(granted, str) else granted)
        if granted and GOOGLE_MAIL_SCOPE not in granted:
            raise OAuthFlowError(
                "MailMind needs Gmail access",
                "To read your inbox directly, MailMind needs permission to read your email and send "
                "you the digest. Please try again and leave the Gmail box ticked.",
                provider="google", retry_consent=True,
            )

    try:
        resp = requests.get(GOOGLE_USERINFO_URL,
                            headers={'Authorization': f"Bearer {credentials.token}"}, timeout=15)
        resp.raise_for_status()
        info = resp.json()
    except Exception:
        logger.exception("Google userinfo lookup failed")
        raise OAuthFlowError("Couldn't connect Google", "We couldn't read your Google profile. Please try again.",
                             provider="google", connect=connect)

    if not info.get("sub") or not info.get("email") or not info.get("email_verified"):
        raise OAuthFlowError("Unverified email",
                             "Your Google account's email address isn't verified, so MailMind can't use it.",
                             provider="google", connect=connect)

    if connect:
        return _finish_oauth("google", info["sub"], info["email"].lower(), credentials.refresh_token)
    return _finish_signin("google", info["sub"], info["email"].lower())


# ---------------------------------------------------------------------------
# OAuth: Microsoft
# ---------------------------------------------------------------------------

@app.route("/microsoft/login")
def microsoft_login():
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    session["microsoft_oauth"] = {"state": state, "verifier": verifier}
    auth_params = {
        'client_id': os.getenv("MICROSOFT_CLIENT_ID"),
        'response_type': 'code',
        'redirect_uri': OUTLOOK_REDIRECT_URI,
        'scope': ' '.join(MICROSOFT_SCOPES),
        'state': state,
        'response_mode': 'query',
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
        'prompt': 'consent' if request.args.get("consent") else 'select_account',
    }
    return redirect(MICROSOFT_AUTH_URL + '?' + urllib.parse.urlencode(auth_params))


@app.route('/microsoft/callback')
def microsoft_callback():
    _check_provider_error("microsoft")
    stored = _pop_oauth_state("microsoft_oauth")

    auth_code = request.args.get('code')
    if not auth_code:
        raise OAuthFlowError("Couldn't connect Microsoft", "Microsoft didn't send back a sign-in code.",
                             provider="microsoft")

    try:
        token_response = requests.post(MICROSOFT_TOKEN_URL, data={
            'client_id': os.getenv("MICROSOFT_CLIENT_ID"),
            'client_secret': os.getenv("MICROSOFT_CLIENT_SECRET"),
            'code': auth_code,
            'redirect_uri': OUTLOOK_REDIRECT_URI,
            'grant_type': 'authorization_code',
            'scope': ' '.join(MICROSOFT_SCOPES),
            'code_verifier': stored.get("verifier"),
        }, timeout=15)
        token_info = token_response.json()
    except Exception:
        logger.exception("Microsoft token exchange failed")
        raise OAuthFlowError("Couldn't connect Microsoft", "Microsoft didn't accept the sign-in. Please try again.",
                             provider="microsoft")
    if token_response.status_code != 200 or "access_token" not in token_info:
        logger.error("Microsoft token exchange failed: %s", token_info.get("error"))
        raise OAuthFlowError("Couldn't connect Microsoft", "Microsoft didn't accept the sign-in. Please try again.",
                             provider="microsoft")

    granted = {s.rsplit("/", 1)[-1].lower() for s in token_info.get("scope", "").split()}
    if not MICROSOFT_REQUIRED_SCOPES <= granted:
        raise OAuthFlowError(
            "MailMind needs mail access",
            "To build your list, MailMind needs permission to read your mail and send you the "
            "digest. Please try again and accept the requested permissions.",
            provider="microsoft", retry_consent=True,
        )

    try:
        user_response = requests.get(
            MICROSOFT_USERINFO_URL,
            headers={'Authorization': f"Bearer {token_info['access_token']}"}, timeout=15,
        )
        user_response.raise_for_status()
        info = user_response.json()
    except Exception:
        logger.exception("Microsoft profile lookup failed")
        raise OAuthFlowError("Couldn't connect Microsoft", "We couldn't read your Microsoft profile.",
                             provider="microsoft")

    # The object id is immutable and tenant-controlled attributes like `mail`
    # can be set to anything by a tenant admin, so identity is the id and the
    # UPN (which must sit on a verified domain) is preferred for the address.
    subject = info.get("id")
    upn = info.get("userPrincipalName") or ""
    user_email = (upn if "@" in upn and "#EXT#" not in upn else info.get("mail") or "").lower()
    if not subject or not user_email:
        raise OAuthFlowError("Couldn't connect Microsoft", "Your Microsoft account has no usable email address.",
                             provider="microsoft")

    return _finish_oauth("microsoft", subject, user_email, token_info.get("refresh_token"))


# ---------------------------------------------------------------------------
# Shared OAuth resolution
# ---------------------------------------------------------------------------

def _find_identity(provider, subject, email):
    """Find the linked inbox for this provider identity (adopting legacy rows)."""
    account = EmailAccount.query.filter_by(provider=provider, provider_subject=subject).first()
    if account:
        return account
    # Rows created before identities were stored: claim them only for the same
    # provider and address.
    legacy = EmailAccount.query.filter_by(provider=provider, email=email, provider_subject=None).first()
    if legacy:
        legacy.provider_subject = subject
    return legacy


def _store_token(account, refresh_token):
    if refresh_token:
        account.oauth_token = encrypt_token(refresh_token)
        account.needs_reauth = False


def _finish_oauth(provider: str, subject: str, user_email: str, refresh_token):
    """
    Land an OAuth flow. Signed-in users link (or reconnect) an inbox; everyone
    else signs in, creating an account on first use. Identities are matched on
    the provider's immutable subject id, never on the email alone.
    """
    account = _find_identity(provider, subject, user_email)
    other_with_email = EmailAccount.query.filter_by(email=user_email).first()
    if other_with_email is account:
        other_with_email = None

    if current_user.is_authenticated:
        if account and account.master_id != current_user.id:
            raise OAuthFlowError("Already connected elsewhere",
                                 f"{user_email} is connected to a different MailMind account.", status=409)
        if other_with_email:
            raise OAuthFlowError("Already connected",
                                 f"{user_email} is already connected through "
                                 f"{provider_name(other_with_email.provider)}.", status=409)
        if account is None:
            if not refresh_token:
                return redirect(_consent_url(provider))
            create_email(user_email, encrypt_token(refresh_token), provider=provider,
                         master=current_user, provider_subject=subject)
            flash(f"Connected {user_email}.", "success")
        else:
            if not refresh_token and account.needs_reauth:
                return redirect(_consent_url(provider))
            _store_token(account, refresh_token)
            flash(f"Reconnected {user_email}.", "success")
        db.session.commit()
        return redirect(url_for('settings'))

    # Signing in.
    if account is not None:
        master = account.master
        if not refresh_token and account.needs_reauth:
            db.session.commit()
            return redirect(_consent_url(provider))
        _store_token(account, refresh_token)
    else:
        if other_with_email:
            raise OAuthFlowError(
                "Use your original sign-in",
                f"{user_email} is already connected through {provider_name(other_with_email.provider)}. "
                f"Sign in with {provider_name(other_with_email.provider)} instead.",
                provider=other_with_email.provider, status=409,
            )
        if not refresh_token:
            return redirect(_consent_url(provider))
        master = Master.query.filter_by(primary_email=user_email).first()
        if master is not None and provider != "google":
            # Only a verified Google address may claim an existing account by email.
            raise OAuthFlowError("Use your original sign-in",
                                 f"{user_email} already has a MailMind account. Sign in with Google instead.",
                                 provider="google", status=409)
        if master is None:
            master = create_master(user_email)
        create_email(user_email, encrypt_token(refresh_token), provider=provider,
                     master=master, provider_subject=subject)

    master.last_login = datetime.now(timezone.utc)
    db.session.commit()
    _start_session_for(master)

    if not master.has_access:
        return redirect(url_for('code'))
    return redirect(url_for('todo_list'))


def _finish_signin(provider: str, subject: str, user_email: str):
    """
    Sign in with an identity that grants no mailbox access. Matching mirrors
    _finish_oauth: the provider's immutable subject first, then accounts made
    before identities existed, and only a verified Google address may claim
    an existing account by email.
    """
    if current_user.is_authenticated:
        return redirect(url_for('settings'))

    identity = Identity.query.filter_by(provider=provider, subject=subject).first()
    if identity is not None:
        master = identity.master
    else:
        account = _find_identity(provider, subject, user_email)
        other_with_email = EmailAccount.query.filter_by(email=user_email).first()
        if account is not None:
            master = account.master
        elif other_with_email is not None:
            raise OAuthFlowError(
                "Use your original sign-in",
                f"{user_email} is already connected through {provider_name(other_with_email.provider)}. "
                f"Sign in with {provider_name(other_with_email.provider)} instead.",
                provider=other_with_email.provider, status=409,
            )
        else:
            master = Master.query.filter_by(primary_email=user_email).first()
            if master is not None and provider != "google":
                raise OAuthFlowError("Use your original sign-in",
                                     f"{user_email} already has a MailMind account. Sign in with Google instead.",
                                     provider="google", status=409)
            if master is None:
                master = create_master(user_email)
        db.session.add(Identity(provider=provider, subject=subject, email=user_email, master=master))

    master.last_login = datetime.now(timezone.utc)
    db.session.commit()
    _start_session_for(master)

    if not master.has_access:
        return redirect(url_for('code'))
    return redirect(url_for('todo_list'))


# ---------------------------------------------------------------------------
# Forwarding
# ---------------------------------------------------------------------------

def _forwarding_for(master):
    """The user's forwarding address, created on first use (None if forwarding is off)."""
    if not forwarding_enabled():
        return None
    if master.forwarding is None:
        db.session.add(ForwardingAddress(master=master, token=new_token()))
        db.session.commit()
    return master.forwarding


def _seen_webhook_token(token):
    """Replay protection: remember Mailgun tokens for the signature window."""
    if _redis_client is None:
        return False
    try:
        return not _redis_client.set(f"mailmind:mg-token:{token}", "1", nx=True, ex=SIGNATURE_MAX_AGE_SECONDS)
    except Exception:
        logger.warning("Redis unavailable for webhook replay check", exc_info=True)
        return False


@app.route("/inbound/mailgun", methods=["POST"])
@csrf.exempt
def inbound_mailgun():
    """Mailgun route forward: one forwarded email per request."""
    if not forwarding_enabled():
        return "Not found", 404
    form = request.form
    token_field = form.get("token", "")
    if not verify_mailgun_signature(form.get("timestamp", ""), token_field, form.get("signature", "")):
        # 406 tells Mailgun not to retry.
        return "Bad signature", 406
    if _seen_webhook_token(token_field):
        return jsonify({"status": "duplicate"})

    msg = from_mailgun(form)
    token = extract_token(msg)
    fwd = ForwardingAddress.query.filter_by(token=token).first() if token else None
    if fwd is None:
        # Unknown address: accept and drop so Mailgun doesn't retry.
        logger.info("Inbound mail for unknown forwarding token")
        return jsonify({"status": "ignored"})

    fwd.last_received_at = datetime.now(timezone.utc)

    confirmation = gmail_confirmation(msg)
    if confirmation:
        fwd.confirmation_code, fwd.confirmation_for = confirmation
        fwd.confirmation_at = datetime.now(timezone.utc)
        db.session.commit()
        return jsonify({"status": "confirmation"})

    message = parse_message(msg, fallback_source=fwd.master.primary_email)
    if message is None:
        db.session.commit()
        return jsonify({"status": "ignored"})

    duplicate = message["message_id"] and InboundEmail.query.filter_by(
        master_id=fwd.master_id, message_id=message["message_id"]).first()
    if not duplicate:
        db.session.add(InboundEmail(master_id=fwd.master_id, **message))
    db.session.commit()
    return jsonify({"status": "queued"})


@app.route("/settings/forwarding/rotate", methods=["POST"])
@login_required
def rotate_forwarding():
    fwd = _forwarding_for(current_user)
    if fwd is None:
        return redirect(url_for('settings')), 303
    fwd.token = new_token()
    fwd.confirmation_code = fwd.confirmation_for = fwd.confirmation_at = None
    db.session.commit()
    flash("New forwarding address created. Update the forwarding rule in your email settings.", "success")
    return redirect(url_for('settings') + "#forwarding"), 303


@app.route("/settings/forwarding/dismiss", methods=["POST"])
@login_required
def dismiss_forwarding_code():
    fwd = current_user.forwarding
    if fwd is not None:
        fwd.confirmation_code = fwd.confirmation_for = fwd.confirmation_at = None
        db.session.commit()
    return redirect(url_for('settings') + "#forwarding"), 303


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------

def _next_delivery(master):
    """The next scheduled delivery as an aware datetime in the user's timezone, or None."""
    if not master.time or not master.timezone:
        return None
    try:
        tz = pytz.timezone(master.timezone)
    except pytz.UnknownTimeZoneError:
        return None
    now = datetime.now(tz)
    upcoming = []
    for t in master.time.split(","):
        try:
            clock = datetime.strptime(t.strip(), "%I:%M %p").time()
        except ValueError:
            continue
        for day in (0, 1):
            date = (now + timedelta(days=day)).date()
            candidate = tz.localize(datetime.combine(date, clock))
            if candidate > now:
                upcoming.append(candidate)
                break
    return min(upcoming) if upcoming else None


def _local(dt, tz_name):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone(pytz.timezone(tz_name or "UTC"))
    except pytz.UnknownTimeZoneError:
        return dt


def _group_items(items):
    groups = {}
    for item in items:
        groups.setdefault(item.account_email, []).append(item)
    return list(groups.items())


def _needs_inbox_setup(master):
    """True when nothing feeds MailMind yet: no connected inbox and no forwarded mail."""
    if master.email_accounts:
        return False
    fwd = master.forwarding
    return fwd is None or fwd.last_received_at is None


@app.template_filter("ago")
def ago_filter(dt):
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = max(0, (datetime.now(timezone.utc) - dt).total_seconds())
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


@app.template_filter("clock")
def clock_filter(dt):
    return dt.strftime("%-I:%M %p") if dt else ""


@app.route("/list")
@login_required
def todo_list():
    if not current_user.has_access:
        return redirect(url_for('subscribe'))

    latest = (Digest.query.filter_by(master_id=current_user.id)
              .order_by(Digest.created_at.desc(), Digest.id.desc()).first())
    earlier = []
    if latest is not None:
        week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        earlier = (DigestItem.query.join(Digest)
                   .filter(Digest.master_id == current_user.id, Digest.id != latest.id,
                           Digest.created_at >= week_ago, DigestItem.done.is_(False))
                   .order_by(Digest.created_at.desc(), DigestItem.id).all())

    items = latest.items if latest else []
    return render_template(
        "list.html",
        digest=latest,
        generated_at=_local(latest.created_at, current_user.timezone) if latest else None,
        groups=_group_items(items),
        done_count=sum(1 for i in items if i.done),
        total=len(items),
        earlier=earlier,
        next_delivery=_next_delivery(current_user),
        needs_reauth=[a.email for a in current_user.email_accounts if a.needs_reauth],
        needs_setup=_needs_inbox_setup(current_user),
    )


@app.route("/list/items/<int:item_id>/toggle", methods=["POST"])
@login_required
def toggle_item(item_id):
    item = (DigestItem.query.join(Digest)
            .filter(DigestItem.id == item_id, Digest.master_id == current_user.id).first())
    if item is None:
        if request.accept_mimetypes.best == "application/json":
            return jsonify({"error": "not found"}), 404
        return render_template("error.html", code=404, title="Not found",
                               message="That item isn't on your list."), 404

    item.done = request.form.get("done", "1" if not item.done else "0") == "1"
    item.done_at = datetime.now(timezone.utc) if item.done else None
    db.session.commit()

    if request.accept_mimetypes.best == "application/json":
        return jsonify({"id": item.id, "done": item.done})
    return redirect(url_for('todo_list')), 303


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_TIMEZONES = [
    ("America/New_York", "Eastern (US)"),
    ("America/Chicago", "Central (US)"),
    ("America/Denver", "Mountain (US)"),
    ("America/Phoenix", "Arizona (US)"),
    ("America/Los_Angeles", "Pacific (US)"),
    ("America/Anchorage", "Alaska"),
    ("Pacific/Honolulu", "Hawaii"),
    ("Europe/London", "London"),
    ("Europe/Paris", "Paris"),
    ("Europe/Berlin", "Berlin"),
    ("Europe/Athens", "Athens"),
    ("Asia/Dubai", "Dubai"),
    ("Asia/Kolkata", "India"),
    ("Asia/Singapore", "Singapore"),
    ("Asia/Tokyo", "Tokyo"),
    ("Australia/Sydney", "Sydney"),
    ("UTC", "UTC"),
]
_TIMEZONE_VALUES = {tz for tz, _ in _TIMEZONES}

_TIME_OPTIONS = [
    f"{h}:{m:02d} {ampm}"
    for ampm in ("AM", "PM")
    for h in [12] + list(range(1, 12))
    for m in (0, 15, 30, 45)
]
_TIME_VALUES = set(_TIME_OPTIONS)
# 24 rows of four quarter-hours, for the settings grid.
_HOUR_ROWS = [(f"{opts[0].split(':')[0]} {opts[0][-2:]}", opts)
              for opts in (_TIME_OPTIONS[i:i + 4] for i in range(0, len(_TIME_OPTIONS), 4))]
MAX_DELIVERY_TIMES = 3


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if not current_user.has_access:
        return redirect(url_for('subscribe'))

    if request.method == "POST":
        tz = request.form.get("timezone")
        times = [t for t in request.form.getlist("time") if t in _TIME_VALUES]

        if tz not in _TIMEZONE_VALUES:
            flash("Please choose a timezone.", "error")
        elif not times:
            flash("Please choose at least one delivery time.", "error")
        else:
            current_user.timezone = tz
            current_user.time = ",".join(times[:MAX_DELIVERY_TIMES])
            db.session.commit()
            flash("Delivery schedule saved.", "success")
        return redirect(url_for('settings'))

    fwd = _forwarding_for(current_user)
    return render_template(
        "settings.html",
        forwarding=fwd,
        forward_address=address_for(fwd.token) if fwd else None,
        accounts=current_user.email_accounts,
        current_times=(current_user.time or "").split(",") if current_user.time else [],
        current_timezone=current_user.timezone or "",
        timezones=_TIMEZONES,
        hour_rows=_HOUR_ROWS,
        max_times=MAX_DELIVERY_TIMES,
    )


@app.route("/settings/accounts/<int:account_id>/remove", methods=["POST"])
@login_required
def remove_email_account(account_id):
    account = EmailAccount.query.filter_by(id=account_id, master_id=current_user.id).first()
    if not account:
        return render_template("error.html", code=404, title="Not found",
                               message="That inbox isn't connected to your account."), 404

    if account.email == current_user.primary_email:
        flash("That's the address you sign in with, so it can't be removed.", "error")
        return redirect(url_for('settings')), 303

    revoke(account)
    email = account.email
    db.session.delete(account)
    db.session.commit()
    flash(f"Disconnected {email}.", "success")
    return redirect(url_for('settings')), 303


def _cancel_billing(master):
    """Cancel any live Stripe subscription. Raises on Stripe errors."""
    if not master.stripe_customer_id:
        return
    subs = stripe.Subscription.list(customer=master.stripe_customer_id, status="all", limit=20)
    for sub in subs.auto_paging_iter():
        if sub.status in ("active", "trialing", "past_due", "unpaid", "incomplete"):
            stripe.Subscription.cancel(sub.id)


@app.route("/settings/delete", methods=["POST"])
@login_required
def delete_mailmind_account():
    if request.form.get("confirm", "").strip().lower() != current_user.primary_email.lower():
        flash("Type your email address exactly to confirm deleting your account.", "error")
        return redirect(url_for('settings') + "#danger"), 303

    master = current_user._get_current_object()
    try:
        _cancel_billing(master)
    except Exception:
        logger.exception("Stripe cancellation failed for master %s", master.id)
        flash("We couldn't cancel your subscription, so nothing was deleted. Please try again or contact us.",
              "error")
        return redirect(url_for('settings') + "#danger"), 303

    for account in list(master.email_accounts):
        revoke(account)
    db.session.delete(master)
    db.session.commit()
    logout_user()
    session.clear()
    flash("Your MailMind account and connected inboxes were deleted.", "success")
    return redirect(url_for('index')), 303


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------

@app.route("/subscribe")
@login_required
def subscribe():
    if current_user.subscribed or current_user.comped_forever:
        return redirect(url_for('settings'))
    return render_template("subscribe.html")


@app.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    if not request.form.get("accept_tos"):
        flash("Please accept the Terms of Service to continue.", "error")
        return redirect(url_for('subscribe')), 303
    if current_user.subscribed:
        return redirect(url_for('settings')), 303
    try:
        prices = stripe.Price.list(lookup_keys=[STRIPE_PRICE_LOOKUP_KEY], expand=['data.product'])
        if not prices.data:
            raise RuntimeError(f"no Stripe price for lookup key {STRIPE_PRICE_LOOKUP_KEY}")

        if not current_user.stripe_customer_id:
            customer = stripe.Customer.create(email=current_user.primary_email)
            current_user.stripe_customer_id = customer.id
            db.session.commit()

        checkout_session = stripe.checkout.Session.create(
            customer=current_user.stripe_customer_id,
            line_items=[{'price': prices.data[0].id, 'quantity': 1}],
            mode='subscription',
            success_url=DOMAIN + url_for('settings'),
            cancel_url=DOMAIN + url_for('subscribe'),
            subscription_data={'trial_period_days': 7},
            allow_promotion_codes=True,
        )
        return redirect(checkout_session.url, code=303)
    except Exception:
        logger.exception("Checkout session creation failed")
        flash("We couldn't start checkout. Please try again in a moment.", "error")
        return redirect(url_for('subscribe')), 303


@app.route('/create-portal-session', methods=['POST'])
@login_required
def customer_portal():
    if not current_user.stripe_customer_id:
        flash("There's no billing account to manage yet.", "error")
        return redirect(url_for('settings')), 303
    try:
        portal_session = stripe.billing_portal.Session.create(
            customer=current_user.stripe_customer_id,
            return_url=DOMAIN + url_for('settings'),
        )
        return redirect(portal_session.url, code=303)
    except Exception:
        logger.exception("Portal session creation failed")
        flash("We couldn't open billing right now. Please try again in a moment.", "error")
        return redirect(url_for('settings')), 303


_ACTIVE_SUB_STATUSES = ('active', 'trialing')


@app.route('/webhook', methods=['POST'])
@csrf.exempt
def webhook_received():
    webhook_secret = os.getenv("WEBHOOK_SECRET") if PRODUCTION else os.getenv("TEST_WEBHOOK")
    if not webhook_secret:
        logger.error("Stripe webhook secret not configured; rejecting")
        return "Webhook secret not configured", 500

    signature = request.headers.get('stripe-signature')
    if not signature:
        return "Missing signature", 400

    try:
        event = stripe.Webhook.construct_event(
            payload=request.data, sig_header=signature, secret=webhook_secret,
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        logger.warning("Stripe webhook signature verification failed")
        return "Bad signature", 400

    data_object = event['data']['object']
    event_type = event['type']
    customer_id = data_object.get('customer') if hasattr(data_object, 'get') else None
    user = Master.query.filter_by(stripe_customer_id=customer_id).first() if customer_id else None

    if user:
        if event_type == 'checkout.session.completed':
            user.subscribed = data_object.get('status') == 'complete'
        elif event_type in ('customer.subscription.created', 'customer.subscription.updated'):
            user.subscribed = data_object.get('status') in _ACTIVE_SUB_STATUSES
        elif event_type == 'customer.subscription.deleted':
            user.subscribed = False
        db.session.commit()

    return jsonify({'status': 'success'})


# ---------------------------------------------------------------------------
# Invite codes
# ---------------------------------------------------------------------------

_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
_CUSTOM_CODE_RE = re.compile(r"^[A-Z0-9]{4,32}$")


def _admin_emails():
    return {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}


def is_admin(user) -> bool:
    return bool(getattr(user, "is_authenticated", False)) and user.primary_email.lower() in _admin_emails()


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not is_admin(current_user):
            abort(404)
        return view(*args, **kwargs)
    return wrapped


def normalize_code(raw) -> str:
    return re.sub(r"[\s\-]", "", str(raw or "")).upper()


def new_access_code() -> str:
    while True:
        candidate = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
        if AccessCode.query.filter_by(code=candidate).first() is None:
            return candidate


def pretty_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}" if len(code) == 8 else code


app.jinja_env.filters["pretty_code"] = pretty_code


def redeem_access_code(master, raw):
    """Apply an invite code to `master`. Returns (ok, message)."""
    wanted = normalize_code(raw)
    code = AccessCode.query.filter_by(code=wanted).with_for_update().first() if wanted else None
    if code is None:
        return False, "That code didn't work. Check it and try again."
    reason = code.unusable_reason()
    if reason:
        return False, reason
    if master.access_code_id == code.id and master.comped:
        return True, "That code is already active on your account."

    now = datetime.now(timezone.utc)
    if code.access_days is None or master.comped_forever:
        until = FOREVER
    else:
        start = master.comp_until if master.comped else now
        start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        until = start + timedelta(days=code.access_days)
    master.comp_until = until
    master.access_code = code
    code.uses += 1
    db.session.commit()
    logger.info("Access code %s redeemed by master %s", code.id, master.id)
    return True, "You're in. Pick when you'd like your list delivered."


@app.route("/i/<raw_code>")
def invite_link(raw_code):
    """Shareable invite link: remembers the code through sign-in, then offers to apply it."""
    session["invite_code"] = normalize_code(raw_code)[:64]
    if current_user.is_authenticated:
        return redirect(url_for('code'))
    return redirect(url_for('login'))


@app.route("/code", methods=["POST", "GET"])
@login_required
def code():
    if current_user.subscribed or current_user.comped_forever:
        session.pop("invite_code", None)
        return redirect(url_for('settings'))
    if request.method != "POST":
        return render_template("code.html", message=None, prefill=pretty_code(session.get("invite_code") or ""))

    ok, message = redeem_access_code(current_user, request.form.get("code"))
    if ok:
        session.pop("invite_code", None)
        flash(message, "success")
        return redirect(url_for('settings'))
    return render_template("code.html", message=message, prefill=request.form.get("code", "")), 400


# ---------------------------------------------------------------------------
# Admin: invite codes and who has access
# ---------------------------------------------------------------------------

def _optional_int(name, lo, hi):
    raw = (request.form.get(name) or "").strip()
    if not raw:
        return None
    value = int(raw)  # ValueError handled by caller
    if not lo <= value <= hi:
        raise ValueError(name)
    return value


@app.route("/admin/codes", methods=["GET", "POST"])
@admin_required
def admin_codes():
    if request.method == "POST":
        try:
            max_uses = _optional_int("max_uses", 1, 100000)
            access_days = _optional_int("access_days", 1, 3650)
            valid_days = _optional_int("valid_days", 1, 3650)
        except ValueError:
            flash("Uses and days must be whole numbers (leave blank for no limit).", "error")
            return redirect(url_for('admin_codes')), 303

        custom = normalize_code(request.form.get("code"))
        if custom and not _CUSTOM_CODE_RE.match(custom):
            flash("Custom codes are 4–32 letters and numbers.", "error")
            return redirect(url_for('admin_codes')), 303
        if custom and AccessCode.query.filter_by(code=custom).first():
            flash(f"{custom} already exists.", "error")
            return redirect(url_for('admin_codes')), 303

        code_row = AccessCode(
            code=custom or new_access_code(),
            note=(request.form.get("note") or "").strip()[:255] or None,
            max_uses=max_uses,
            access_days=access_days,
            expires_at=(datetime.now(timezone.utc) + timedelta(days=valid_days)) if valid_days else None,
        )
        db.session.add(code_row)
        db.session.commit()
        flash(f"Created {pretty_code(code_row.code)}.", "success")
        return redirect(url_for('admin_codes', new=code_row.id)), 303

    codes = AccessCode.query.order_by(AccessCode.created_at.desc(), AccessCode.id.desc()).all()
    people = Master.query.order_by(Master.created_at.desc(), Master.id.desc()).all()
    return render_template("admin_codes.html", codes=codes, people=people,
                           new_id=request.args.get("new", type=int), now=datetime.now(timezone.utc))


@app.route("/admin/codes/<int:code_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_code(code_id):
    code_row = db.session.get(AccessCode, code_id) or abort(404)
    code_row.active = not code_row.active
    db.session.commit()
    flash(f"{pretty_code(code_row.code)} is {'on' if code_row.active else 'off'}.", "success")
    return redirect(url_for('admin_codes')), 303


@app.route("/admin/people/<int:master_id>/revoke", methods=["POST"])
@admin_required
def admin_revoke_access(master_id):
    master = db.session.get(Master, master_id) or abort(404)
    master.comp_until = None
    db.session.commit()
    flash(f"Removed free access for {master.primary_email}.", "success")
    return redirect(url_for('admin_codes') + "#people"), 303


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

@app.errorhandler(CSRFError)
def handle_csrf_error(err):
    return render_template("error.html", code=400, title="Page expired",
                           message="This form was open too long. Go back, refresh, and try again."), 400


@app.errorhandler(404)
def not_found(err):
    return render_template("error.html", code=404, title="Page not found",
                           message="That page doesn't exist, or it moved."), 404


@app.errorhandler(405)
def method_not_allowed(err):
    return render_template("error.html", code=405, title="Not allowed",
                           message="That page can't be opened this way."), 405


@app.errorhandler(500)
def server_error(err):
    return render_template("error.html", code=500, title="Something went wrong",
                           message="We hit an unexpected error. Please try again in a moment."), 500
