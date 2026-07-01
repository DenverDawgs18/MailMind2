"""
MailMind is a productivity service, not an email client. This module wires up
the tiny public surface:

    /                      -- landing page + OAuth sign-in buttons
    /google/login          -- kicks off Google OAuth (sign-in + link)
    /google/callback
    /microsoft/login       -- Microsoft OAuth
    /microsoft/callback
    /settings              -- the only page a normal subscriber ever visits
    /settings/delete_account
    /subscribe             -- shown when the user isn't subscribed
    /create-checkout-session
    /create-portal-session
    /webhook               -- Stripe events
    /code                  -- TEMP_CODE / CODE beta grant during testing
    /contact
    /termsandprivacy
    /logout
"""
import logging
import os
import secrets
import urllib.parse
from datetime import datetime, timezone

import requests
import stripe
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
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
from flask_wtf.csrf import CSRFProtect
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
else:
    DOMAIN = os.getenv("DOMAIN", "https://mailmind.fly.dev")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, static_url_path='/static')

# Secret key
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
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///database.db"


class Base(DeclarativeBase):
    pass


db = SQLAlchemy(model_class=Base)
db.init_app(app)
migrate = Migrate(app, db)

# Sessions
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


@app.context_processor
def inject_csrf():
    from flask_wtf.csrf import generate_csrf
    return {"csrf_token": generate_csrf}


stripe.api_key = os.getenv("STRIPE_API_KEY")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "index"

app._redis_client = _redis_client

# ---------------------------------------------------------------------------
# Imports that need `app` / `db` first
# ---------------------------------------------------------------------------

from functions.encryption import encrypt_token  # noqa: E402
from functions.refresh_token import refresh  # noqa: E402
from functions.users import create_email, create_master  # noqa: E402
from models import EmailAccount, Master  # noqa: E402

# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

GOOGLE_SCOPES = [
    'https://mail.google.com/',
    'https://www.googleapis.com/auth/userinfo.email',
    'openid',
]
GOOGLE_REDIRECT_URI = f"{DOMAIN}/google/callback"

google_client_config = {
    "web": {
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [GOOGLE_REDIRECT_URI],
    }
}

OUTLOOK_SCOPES = [
    'https://graph.microsoft.com/Mail.ReadWrite',
    'https://graph.microsoft.com/Mail.Send',
    'https://graph.microsoft.com/User.Read',
    'openid', 'profile', 'email', 'offline_access',
]
OUTLOOK_REDIRECT_URI = f"{DOMAIN}/microsoft/callback"

MICROSOFT_AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MICROSOFT_USERINFO_URL = "https://graph.microsoft.com/v1.0/me"


@login_manager.user_loader
def load_user(id):
    try:
        return db.session.get(Master, int(id))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('settings'))
    return render_template('index.html')


@app.route("/contact")
def contact():
    return render_template("contact.html")


@app.route("/termsandprivacy")
def terms_and_privacy():
    return render_template("termsandprivacy.html")


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))


# ---------------------------------------------------------------------------
# OAuth: Google
# ---------------------------------------------------------------------------

@app.route("/google/login")
def google_login():
    """
    Kick off Google OAuth. This is BOTH sign-in and account-linking:

    - If the visitor isn't logged in yet, the callback creates or finds a
      Master keyed by their Google email.
    - If they are logged in, the callback attaches the (possibly different)
      Google email as an additional EmailAccount on their existing Master.
    """
    flow = Flow.from_client_config(
        google_client_config, scopes=GOOGLE_SCOPES, redirect_uri=GOOGLE_REDIRECT_URI,
    )
    state = secrets.token_urlsafe(32)
    session['google_oauth_state'] = state
    auth_url, _ = flow.authorization_url(
        prompt="consent", access_type='offline', state=state,
    )
    return redirect(auth_url)


@app.route('/google/callback')
def google_callback():
    stored_state = session.pop('google_oauth_state', None)
    if not stored_state or stored_state != request.args.get('state'):
        return "Invalid state parameter", 400

    flow = Flow.from_client_config(
        google_client_config, scopes=GOOGLE_SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI, state=stored_state,
    )

    if PRODUCTION and request.url.startswith("http://"):
        authorization_response = "https://" + request.url[len("http://"):]
    else:
        authorization_response = request.url

    try:
        flow.fetch_token(authorization_response=authorization_response)
    except Exception:
        logger.exception("Google OAuth token exchange failed")
        return "OAuth failed", 400

    credentials = flow.credentials
    if not credentials.refresh_token:
        return "OAuth failed: missing refresh token — try again and grant offline access", 400

    try:
        user_info = requests.get(
            'https://www.googleapis.com/oauth2/v2/userinfo',
            headers={'Authorization': f"Bearer {credentials.token}"},
            timeout=15,
        ).json()
    except Exception:
        logger.exception("Google userinfo lookup failed")
        return "OAuth failed", 400

    user_email = user_info.get('email')
    if not user_email:
        return "OAuth failed: no email in userinfo", 400

    return _finish_oauth("google", user_email, credentials.refresh_token)


# ---------------------------------------------------------------------------
# OAuth: Microsoft
# ---------------------------------------------------------------------------

@app.route("/microsoft/login")
def microsoft_login():
    state = secrets.token_urlsafe(32)
    session['microsoft_oauth_state'] = state
    auth_params = {
        'client_id': os.getenv("MICROSOFT_CLIENT_ID"),
        'response_type': 'code',
        'redirect_uri': OUTLOOK_REDIRECT_URI,
        'scope': ' '.join(OUTLOOK_SCOPES),
        'state': state,
        'response_mode': 'query',
    }
    return redirect(MICROSOFT_AUTH_URL + '?' + urllib.parse.urlencode(auth_params))


@app.route('/microsoft/callback')
def microsoft_callback():
    stored_state = session.pop('microsoft_oauth_state', None)
    if not stored_state or stored_state != request.args.get('state'):
        return "Invalid state parameter", 400

    auth_code = request.args.get('code')
    if not auth_code:
        return "Authorization code not found", 400

    token_data = {
        'client_id': os.getenv("MICROSOFT_CLIENT_ID"),
        'client_secret': os.getenv("MICROSOFT_CLIENT_SECRET"),
        'code': auth_code,
        'redirect_uri': OUTLOOK_REDIRECT_URI,
        'grant_type': 'authorization_code',
        'scope': ' '.join(OUTLOOK_SCOPES),
    }
    token_response = requests.post(
        MICROSOFT_TOKEN_URL, data=token_data,
        headers={'Content-Type': 'application/x-www-form-urlencoded'}, timeout=15,
    )
    if token_response.status_code != 200:
        logger.error("Microsoft token exchange failed: %s", token_response.text)
        return "Token exchange failed", 400

    token_info = token_response.json()
    user_response = requests.get(
        MICROSOFT_USERINFO_URL,
        headers={'Authorization': f"Bearer {token_info['access_token']}"},
        timeout=15,
    )
    if user_response.status_code != 200:
        logger.error("Microsoft userinfo lookup failed: %s", user_response.text)
        return "Failed to get user info", 400

    user_info = user_response.json()
    user_email = user_info.get('mail') or user_info.get('userPrincipalName')
    if not user_email:
        return "OAuth failed: no email in userinfo", 400

    return _finish_oauth("microsoft", user_email, token_info.get("refresh_token"))


# ---------------------------------------------------------------------------
# Shared OAuth resolution
# ---------------------------------------------------------------------------

def _finish_oauth(provider: str, user_email: str, refresh_token: str):
    """
    Land the OAuth flow: create-or-attach an EmailAccount, log the user in,
    then send them to /settings (or to /code if they still need to unlock beta).
    """
    if not refresh_token:
        return "OAuth failed: missing refresh token", 400

    encrypted = encrypt_token(refresh_token)
    existing_account = EmailAccount.query.filter_by(email=user_email).first()

    if current_user.is_authenticated:
        # Adding an additional email account to an existing session.
        if existing_account and existing_account.master_id != current_user.id:
            return "This email is already linked to another MailMind account", 409

        if existing_account:
            existing_account.oauth_token = encrypted
            existing_account.provider = provider
        else:
            create_email(user_email, encrypted, provider=provider, master=current_user)

        current_user.last_login = datetime.now(timezone.utc)
        db.session.commit()
        return redirect(url_for('settings'))

    # Not logged in yet — this OAuth *is* the sign-in.
    master = Master.query.filter_by(primary_email=user_email).first()
    if master is None and existing_account:
        # An account with this email was linked before as a secondary — reuse
        # its Master rather than creating a duplicate.
        master = db.session.get(Master, existing_account.master_id)

    if master is None:
        master = create_master(user_email)

    if existing_account is None:
        create_email(user_email, encrypted, provider=provider, master=master)
    else:
        existing_account.oauth_token = encrypted
        existing_account.provider = provider

    master.last_login = datetime.now(timezone.utc)
    db.session.commit()
    login_user(master)

    if not master.subscribed:
        return redirect(url_for('code'))
    return redirect(url_for('settings'))


# ---------------------------------------------------------------------------
# Settings — the only routine page
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

_TIME_OPTIONS = [
    f"{h}:{m:02d} {ampm}"
    for ampm in ("AM", "PM")
    for h in list(range(1, 13))
    for m in (0, 15, 30, 45)
]


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if not current_user.subscribed:
        return redirect(url_for('subscribe'))

    message = None

    if request.method == "POST":
        tz = request.form.get("timezone")
        times = request.form.getlist("time")

        if not tz:
            message = "Please select a timezone."
        elif not times:
            message = "Please select at least one time."
        else:
            current_user.timezone = tz
            current_user.time = ",".join(times[:3])
            db.session.commit()
            message = "Settings saved."

    return render_template(
        "settings.html",
        message=message,
        accounts=current_user.email_accounts,
        current_times=(current_user.time or "").split(",") if current_user.time else [],
        current_timezone=current_user.timezone or "",
        timezones=_TIMEZONES,
        time_options=_TIME_OPTIONS,
    )


@app.route("/settings/delete_account", methods=["POST"])
@login_required
def delete_email_account():
    try:
        account_id = int(request.form.get("id"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid account id"}), 400

    account = EmailAccount.query.filter_by(id=account_id, master_id=current_user.id).first()
    if not account:
        return jsonify({"success": False, "error": "Not found"}), 404

    if account.email == current_user.primary_email:
        return jsonify({
            "success": False,
            "error": "Can't delete your primary email — this is your login. Log out first, or contact support.",
        }), 400

    try:
        db.session.delete(account)
        db.session.commit()
        return jsonify({"success": True}), 200
    except Exception:
        logger.exception("Failed to delete account %s", account_id)
        db.session.rollback()
        return jsonify({"success": False, "error": "Delete failed"}), 500


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------

@app.route("/subscribe")
@login_required
def subscribe():
    if current_user.subscribed:
        return redirect(url_for('settings'))
    return render_template("subscribe.html")


@app.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    try:
        prices = stripe.Price.list(
            lookup_keys=[request.form['lookup_key']],
            expand=['data.product'],
        )

        if not current_user.stripe_customer_id:
            customer = stripe.Customer.create(
                email=current_user.primary_email,
                name=current_user.primary_email,
            )
            current_user.stripe_customer_id = customer.id
            db.session.commit()

        checkout_session = stripe.checkout.Session.create(
            customer=current_user.stripe_customer_id,
            line_items=[{'price': prices.data[0].id, 'quantity': 1}],
            mode='subscription',
            success_url=DOMAIN + url_for('settings'),
            cancel_url=DOMAIN,
            subscription_data={'trial_period_days': 7},
        )
        return redirect(checkout_session.url, code=303)
    except Exception:
        logger.exception("Checkout session creation failed")
        return "Server error", 500


@app.route('/create-portal-session', methods=['POST'])
@login_required
def customer_portal():
    try:
        if not current_user.stripe_customer_id:
            return "No subscription found", 400
        portal_session = stripe.billing_portal.Session.create(
            customer=current_user.stripe_customer_id,
            return_url=DOMAIN + url_for('settings'),
        )
        return redirect(portal_session.url, code=303)
    except Exception:
        logger.exception("Portal session creation failed")
        return "Server error", 500


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
    customer_id = data_object.get('customer') if isinstance(data_object, dict) else None
    user = Master.query.filter_by(stripe_customer_id=customer_id).first() if customer_id else None

    if user and event_type in ('checkout.session.completed', 'customer.subscription.created'):
        user.subscribed = True
    elif user and event_type == 'customer.subscription.updated':
        user.subscribed = data_object.get('status') in ('active', 'trialing')
    elif user and event_type == 'customer.subscription.deleted':
        user.subscribed = False

    if user:
        db.session.commit()

    return jsonify({'status': 'success'})


# ---------------------------------------------------------------------------
# Beta grant (kept during testing)
# ---------------------------------------------------------------------------

@app.route("/code", methods=["POST", "GET"])
@login_required
def code():
    if request.method != "POST":
        return render_template("code.html", message=None)

    submitted = request.form.get("code")
    real_code = os.getenv("CODE")
    temp_code = os.getenv("TEMP_CODE")

    if temp_code and str(submitted) == str(temp_code):
        logger.info("TEMP CODE granted to %s", current_user.primary_email)
        current_user.subscribed = True
        current_user.temp = True
        db.session.commit()
        return redirect(url_for('settings'))

    if real_code and str(real_code) != "DISABLED" and str(submitted) == str(real_code):
        logger.info("CODE granted to %s", current_user.primary_email)
        current_user.subscribed = True
        db.session.commit()
        return redirect(url_for('settings'))

    return render_template("code.html", message="Invalid code.")


# ---------------------------------------------------------------------------
# CSRF exemptions for JSON API endpoints (until the frontend sends the header)
# ---------------------------------------------------------------------------

csrf.exempt(delete_email_account)
