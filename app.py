import base64
import hashlib
import json
import logging
import os
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone

import markdown
import requests
import stripe
from dateutil import parser as date_parser
from dateutil.tz import tzlocal
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
from werkzeug.security import check_password_hash, generate_password_hash

from functions.production import production, simple

# ---------------------------------------------------------------------------
# Environment / configuration
# ---------------------------------------------------------------------------

PRODUCTION = production()
SIMPLE = simple()

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
        # Local dev fallback so a fresh clone can run
        app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-do-not-use-in-prod")

# Cookies
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
if PRODUCTION:
    app.config['SESSION_COOKIE_SECURE'] = True

# Database
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

# Sessions (server-side by default; tests use filesystem to avoid Redis dependency)
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_USE_SIGNER"] = True

_TESTING = os.getenv("MAILMIND_TEST", "").strip() in ("1", "true", "yes")

if _TESTING:
    app.config["SESSION_TYPE"] = "filesystem"
    _redis_client = None
else:
    app.config["SESSION_TYPE"] = "redis"
    if PRODUCTION:
        redis_host = os.getenv("REDIS_HOST", "fly-mailmind-redis.upstash.io")
        redis_port = int(os.getenv("REDIS_PORT", "6379"))
        redis_password = os.getenv("REDIS_PASSWORD")
        _redis_client = Redis(host=redis_host, port=redis_port, password=redis_password)
    else:
        _redis_client = Redis(host=os.getenv("REDIS_HOST", "localhost"), port=6379)
    app.config["SESSION_REDIS"] = _redis_client

Session(app)

# CSRF
csrf = CSRFProtect(app)


@app.context_processor
def inject_csrf():
    """Expose a csrf-token meta tag helper for templates."""
    from flask_wtf.csrf import generate_csrf
    return {"csrf_token": generate_csrf}


# Stripe
stripe.api_key = os.getenv("STRIPE_API_KEY")

# Login manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


# ---------------------------------------------------------------------------
# Imports that need `app`/`db` to already exist
# ---------------------------------------------------------------------------

from functions.encryption import encrypt_token  # noqa: E402
from functions.get_emails import get_emails  # noqa: E402
from functions.get_one_action import get_an_action  # noqa: E402
from functions.linkify import linkify_text  # noqa: E402
from functions.refresh_token import refresh  # noqa: E402
from functions.scheduler import check_and_send_emails  # noqa: E402
from functions.users import create_email, create_master  # noqa: E402
from models import CachedEmail, EmailAccount, Master, Todo  # noqa: E402

# ---------------------------------------------------------------------------
# OAuth configuration
# ---------------------------------------------------------------------------

GOOGLE_SCOPES = [
    'https://mail.google.com/',
    'https://www.googleapis.com/auth/userinfo.email',
    'openid',
    'https://www.googleapis.com/auth/calendar.calendars.readonly',
    'https://www.googleapis.com/auth/calendar.events.owned',
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
    'https://graph.microsoft.com/Calendars.ReadWrite',
    'https://graph.microsoft.com/User.Read',
    'openid',
    'profile',
    'email',
    'offline_access',
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
# Auth
# ---------------------------------------------------------------------------

MIN_PASSWORD_LENGTH = 10


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not username or not password:
        return jsonify({"success": False, "message": "All fields are required."}), 400

    if mode == "register":
        if Master.query.filter_by(username=username).first():
            return jsonify({"success": False, "message": "Username already taken."}), 400
        if len(password) < MIN_PASSWORD_LENGTH:
            return jsonify({
                "success": False,
                "message": f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
            }), 400
        try:
            hashed_pw = generate_password_hash(password)
            user = create_master(username, hashed_pw)
            login_user(user)
            return jsonify({
                "success": True,
                "message": "Account created successfully!",
                "redirect": url_for("portal"),
            }), 201
        except Exception:
            logger.exception("Registration failed")
            return jsonify({"success": False, "message": "An error occurred during registration."}), 500

    if mode == "login":
        user = Master.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            return jsonify({
                "success": True,
                "message": "Login successful!",
                "redirect": url_for("summary"),
            }), 200
        return jsonify({"success": False, "message": "Invalid username or password."}), 401

    return jsonify({"success": False, "message": "Invalid mode."}), 400


@app.route("/check_username")
def check_username():
    username = request.args.get("username", "").strip()
    if not username:
        return jsonify({"exists": False}), 400
    exists = Master.query.filter_by(username=username).first() is not None
    return jsonify({"exists": exists}), 200


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))


@app.route("/sessionclear")
@login_required
def session_clear():
    session.clear()
    return redirect(url_for('index'))


# ---------------------------------------------------------------------------
# Marketing / static pages
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route("/special")
def special():
    return render_template("index.html")


@app.route("/request_access")
def request_access():
    return render_template("request_access.html")


@app.route("/contact")
def contact():
    return render_template("contact.html")


@app.route("/termsandprivacy")
def terms_and_privacy():
    return render_template("termsandprivacy.html")


@app.route("/beta")
def beta():
    return render_template("beta.html")


# ---------------------------------------------------------------------------
# Portal + account management
# ---------------------------------------------------------------------------

@app.route("/portal")
@login_required
def portal():
    if not current_user.subscribed:
        return redirect(url_for("index"))
    return render_template("portal.html", accounts=current_user.email_accounts)


@app.route("/delete_account", methods=["POST"])
@login_required
def delete_account():
    data = request.get_json(silent=True) or {}
    try:
        account_id = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid account id"}), 400

    account = EmailAccount.query.filter_by(id=account_id, master_id=current_user.id).first()
    if not account:
        return jsonify({"success": False, "error": "Not found"}), 404

    try:
        db.session.delete(account)
        db.session.commit()
        return jsonify({"success": True}), 200
    except Exception:
        logger.exception("Failed to delete account %s", account_id)
        db.session.rollback()
        return jsonify({"success": False, "error": "Delete failed"}), 500


# ---------------------------------------------------------------------------
# Admin / debug (auth-gated so it can't be abused)
# ---------------------------------------------------------------------------

@app.route("/check")
@login_required
def check():
    """Manually trigger a scheduler check; restricted to admins."""
    admin_users = {u.strip() for u in os.getenv("MAILMIND_ADMINS", "").split(",") if u.strip()}
    if current_user.username not in admin_users:
        return jsonify({"success": False, "error": "Not authorized"}), 403
    check_and_send_emails()
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Beta invite code
# ---------------------------------------------------------------------------

@app.route("/code", methods=["POST", "GET"])
@login_required
def code():
    if request.method != "POST":
        return render_template("code.html", message=False)

    submitted = request.form.get("code")
    real_code = os.getenv("CODE")
    temp_code = os.getenv("TEMP_CODE")

    if temp_code and str(submitted) == str(temp_code):
        logger.info("TEMP CODE GRANTED TO %s", current_user.username)
        current_user.subscribed = True
        current_user.temp = True
        db.session.commit()
        return render_template("code.html", message="Code valid. Subscription granted until launch.")

    if real_code and str(real_code) != "DISABLED" and str(submitted) == str(real_code):
        logger.info("CODE GRANTED TO %s", current_user.username)
        current_user.subscribed = True
        db.session.commit()
        return render_template("code.html", message="Code valid. Subscription granted.")

    return render_template("code.html", message="Invalid code.")


# ---------------------------------------------------------------------------
# Google OAuth
# ---------------------------------------------------------------------------

@app.route("/google/login")
@login_required
def google_login():
    flow = Flow.from_client_config(
        google_client_config, scopes=GOOGLE_SCOPES, redirect_uri=GOOGLE_REDIRECT_URI
    )
    state = secrets.token_urlsafe(32)
    session['google_oauth_state'] = state
    auth_url, _ = flow.authorization_url(
        prompt="consent", access_type='offline', state=state
    )
    return redirect(auth_url)


@app.route('/google/callback')
@login_required
def google_callback():
    stored_state = session.pop('google_oauth_state', None)
    received_state = request.args.get('state')
    if not stored_state or stored_state != received_state:
        logger.warning("Google OAuth state mismatch")
        return "Invalid state parameter", 400

    flow = Flow.from_client_config(
        google_client_config,
        scopes=GOOGLE_SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI,
        state=stored_state,
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
        logger.warning("Google returned no refresh_token; user must re-consent")
        return "OAuth failed: missing refresh token", 400

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

    existing = EmailAccount.query.filter_by(email=user_email).first()
    if existing and existing.master_id != current_user.id:
        logger.warning(
            "User %s tried to attach email %s already owned by %s",
            current_user.id, user_email, existing.master_id,
        )
        return "This email is already linked to another account", 409

    if not existing:
        create_email(
            user_email, encrypt_token(credentials.refresh_token),
            provider="google", master=current_user,
        )
    else:
        existing.oauth_token = encrypt_token(credentials.refresh_token)
        existing.provider = "google"
        db.session.commit()

    _invalidate_email_cache_for_user(current_user.id)

    if current_user.subscribed:
        if current_user.time and current_user.timezone:
            return redirect(url_for("summary"))
        return redirect(url_for("set_time"))
    return redirect(url_for("code"))


# ---------------------------------------------------------------------------
# Microsoft OAuth
# ---------------------------------------------------------------------------

@app.route("/microsoft/login")
@login_required
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
@login_required
def microsoft_callback():
    stored_state = session.pop('microsoft_oauth_state', None)
    received_state = request.args.get('state')
    if not stored_state or stored_state != received_state:
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
        MICROSOFT_TOKEN_URL,
        data=token_data,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=15,
    )
    if token_response.status_code != 200:
        logger.error("Microsoft token exchange failed: %s", token_response.text)
        return "Token exchange failed", 400

    token_info = token_response.json()
    user_response = requests.get(
        MICROSOFT_USERINFO_URL,
        headers={
            'Authorization': f"Bearer {token_info['access_token']}",
            'Content-Type': 'application/json',
        },
        timeout=15,
    )
    if user_response.status_code != 200:
        logger.error("Microsoft userinfo lookup failed: %s", user_response.text)
        return "Failed to get user info", 400

    user_info = user_response.json()
    user_email = user_info.get('mail') or user_info.get('userPrincipalName')
    if not user_email:
        return "OAuth failed: no email in userinfo", 400

    existing = EmailAccount.query.filter_by(email=user_email).first()
    if existing and existing.master_id != current_user.id:
        return "This email is already linked to another account", 409

    if not existing:
        create_email(
            user_email, encrypt_token(token_info.get("refresh_token")),
            provider="microsoft", master=current_user,
        )
    else:
        existing.oauth_token = encrypt_token(token_info.get("refresh_token"))
        existing.provider = "microsoft"
        db.session.commit()

    _invalidate_email_cache_for_user(current_user.id)

    if current_user.time and current_user.timezone:
        return redirect(url_for("summary"))
    return redirect(url_for("set_time"))


# ---------------------------------------------------------------------------
# Template filters
# ---------------------------------------------------------------------------

@app.template_filter('markdown')
def markdown_filter(text):
    return markdown.markdown(text)


@app.template_filter("remove_asterisks")
def remove_asterisks(text):
    if text is None:
        return ""
    return text.replace("*", "")


app.jinja_env.filters['markdown'] = markdown_filter
app.jinja_env.filters['linkify_text'] = linkify_text
app.jinja_env.filters["remove_asterisks"] = remove_asterisks


# ---------------------------------------------------------------------------
# Email cache helpers (replaces stashing full email bodies in session)
# ---------------------------------------------------------------------------

CACHE_MAX_AGE = timedelta(hours=48)


def _message_hash(sender: str, subject: str, utc: datetime) -> str:
    ts = utc.astimezone(timezone.utc).isoformat() if utc else ""
    digest = hashlib.sha256(
        f"{sender or ''}|{subject or ''}|{ts}".encode("utf-8")
    ).hexdigest()
    return digest


def _purge_stale_cache_rows():
    cutoff = datetime.now(timezone.utc) - CACHE_MAX_AGE
    CachedEmail.query.filter(CachedEmail.fetched_at < cutoff).delete()


def _invalidate_email_cache_for_user(master_id: int):
    CachedEmail.query.filter_by(master_id=master_id).delete()
    db.session.commit()


def _parse_last_load(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = date_parser.parse(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            logger.warning("Failed to parse last_load %r", value)
    return datetime.now(timezone.utc)


def _fetch_and_cache_emails(refresh_only: bool = False):
    """
    Fetch emails for every account belonging to `current_user`, dedupe against
    the CachedEmail table, and persist new rows. Returns the ordered list of
    cached-email dicts for template rendering.
    """
    _purge_stale_cache_rows()

    now = datetime.now(timezone.utc)
    after_date = since_time = None
    if refresh_only:
        last_load = _parse_last_load(session.get('last_load'))
        after_date = last_load.strftime("%m-%d-%y")
        since_time = last_load.strftime("%H:%M:%S")

    for account in current_user.email_accounts:
        try:
            new_emails = get_emails(
                account.provider, account.email, refresh(account),
                after_date=after_date, since_time=since_time,
                master_id=current_user.id,
            )
        except Exception:
            logger.exception("get_emails failed for %s", account.email)
            continue

        new_emails = list(reversed(new_emails))
        first = True
        for email in new_emails:
            msg_hash = _message_hash(
                email.get('from', ''), email.get('subject', ''), email.get('utc'),
            )
            existing = CachedEmail.query.filter_by(
                master_id=current_user.id,
                account_id=account.id,
                message_hash=msg_hash,
            ).first()
            if existing:
                continue

            row = CachedEmail(
                master_id=current_user.id,
                account_id=account.id,
                account_email=account.email,
                message_hash=msg_hash,
                sender=email.get('from', ''),
                subject=email.get('subject', ''),
                body=email.get('body', ''),
                received_utc=email.get('utc') or now,
                action_items="Generating ...",
                calendar=False,
                is_first_of_account=first,
            )
            first = False
            db.session.add(row)

    try:
        db.session.commit()
    except Exception:
        logger.exception("Failed to commit cached emails")
        db.session.rollback()

    session['last_load'] = now.isoformat()
    return _load_cached_emails()


def _load_cached_emails():
    """Return cached emails for current_user as a list of template-friendly dicts."""
    rows = (
        CachedEmail.query
        .filter_by(master_id=current_user.id)
        .order_by(CachedEmail.account_id.asc(), CachedEmail.received_utc.desc())
        .all()
    )
    result = []
    for row in rows:
        result.append({
            "id": row.id,
            "from": row.sender,
            "subject": row.subject,
            "body": row.body,
            "email": row.account_email,
            "utc": row.received_utc,
            "action_items": row.action_items,
            "calendar": row.calendar,
            "change": row.is_first_of_account,
            "todo_id": row.todo_id,
        })
    return result


# ---------------------------------------------------------------------------
# Email views
# ---------------------------------------------------------------------------

@app.route('/emails')
@login_required
def emails():
    if not current_user.subscribed:
        return render_template("subscribe.html")

    refresh_only = 'last_load' in session and CachedEmail.query.filter_by(
        master_id=current_user.id
    ).first() is not None
    final_emails = _fetch_and_cache_emails(refresh_only=refresh_only)
    return render_template('emails.html', emails=final_emails, accounts=current_user.email_accounts)


from functions.calendar import create_google_calendar_event, create_microsoft_calendar_event  # noqa: E402


@app.route("/add_to_calendar", methods=["POST"])
@login_required
def add_to_calendar():
    try:
        start = request.form.get("start")
        end = request.form.get("end")
        name = request.form.get("name")
        try:
            account_id = int(request.form.get("account"))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "Invalid account id"}), 400

        account = EmailAccount.query.filter_by(id=account_id, master_id=current_user.id).first()
        if not account:
            return jsonify({"success": False, "error": "Account not found"}), 404

        if not start or not end or not name:
            return jsonify({"success": False, "error": "Missing required fields"}), 400

        start_dt = datetime.strptime(start, "%Y-%m-%dT%H:%M").replace(tzinfo=tzlocal())
        end_dt = datetime.strptime(end, "%Y-%m-%dT%H:%M").replace(tzinfo=tzlocal())
        if end_dt <= start_dt:
            return jsonify({"success": False, "error": "End time must be after start time"}), 400

        if account.provider == "google":
            return create_google_calendar_event(start_dt, end_dt, name, account)
        if account.provider == "microsoft":
            return create_microsoft_calendar_event(start_dt, end_dt, name, account)
        return jsonify({"success": False, "error": "Unsupported calendar provider"}), 400
    except ValueError:
        return jsonify({"success": False, "error": "Invalid date format"}), 400
    except Exception:
        logger.exception("Calendar API error")
        return jsonify({"success": False, "error": "Failed to create calendar event"}), 500


@app.route("/get_one_action", methods=["POST"])
@login_required
def get_one_action():
    data = request.get_json(silent=True) or {}
    body = data.get("body") or ""
    try:
        index = int(data.get("index"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid index"}), 400

    cached = _load_cached_emails()
    if index < 0 or index >= len(cached):
        return jsonify({"success": False, "error": "Email index out of range"}), 400
    target = cached[index]

    action = get_an_action(body)
    calendar = any(
        kw in action.lower() for kw in ["meeting", "conference call", "calendar", "appointment", "call"]
    )

    row = db.session.get(CachedEmail, target["id"])
    if row is None:
        return jsonify({"success": False, "error": "Email not found"}), 404

    if action and action.lower() not in ("no action.", "no action", "no action required.", ""):
        row.action_items = action
        row.calendar = calendar
        # Only add a Todo when we don't already have one for this email
        if row.todo_id is None:
            new_todo = Todo(master=current_user.id, item=action, done=False)
            db.session.add(new_todo)
            db.session.flush()
            row.todo_id = new_todo.id
    else:
        row.action_items = "No action."
        row.calendar = False
        action = "No action."

    db.session.commit()

    accounts = [{"id": a.id, "email": a.email} for a in current_user.email_accounts]
    return jsonify({"action_item": action, "calendar": calendar, "accounts": accounts})


@app.route("/remove_todo", methods=["POST"])
@login_required
def remove_todo():
    data = request.get_json(silent=True) or {}
    try:
        todo_id = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid id"}), 400

    todo = Todo.query.filter_by(id=todo_id, master=current_user.id).first()
    if not todo:
        return jsonify({"success": False, "error": "Not found"}), 404

    # Detach from any cached emails that reference it before deleting
    CachedEmail.query.filter_by(master_id=current_user.id, todo_id=todo_id).update(
        {"todo_id": None}
    )
    db.session.delete(todo)
    db.session.commit()
    return jsonify({"success": True})


@app.route('/summary')
@login_required
def summary():
    if not current_user.subscribed:
        return render_template("subscribe.html")

    refresh_only = 'last_load' in session and CachedEmail.query.filter_by(
        master_id=current_user.id
    ).first() is not None
    final_emails = _fetch_and_cache_emails(refresh_only=refresh_only)

    pending = [i for i, e in enumerate(final_emails) if e.get("action_items") == "Generating ..."]
    if pending:
        text = f"Processing {len(pending)} emails for action items..."
    else:
        actionable = [
            e for e in final_emails
            if e.get("action_items")
            and e["action_items"] not in ("No action.", "Generating ...")
        ]
        text = (
            f"Found {len(actionable)} action items"
            if actionable else "No action items found from recent emails."
        )

    return render_template(
        'summary.html',
        emails=final_emails, text=text,
        accounts=current_user.email_accounts,
        pending_count=len(pending),
    )


@app.route('/generate_pending_actions', methods=['POST'])
@login_required
def generate_pending_actions():
    """Bulk-generate actions for cached emails that still say 'Generating ...'."""
    rows = CachedEmail.query.filter_by(
        master_id=current_user.id, action_items="Generating ..."
    ).all()
    if not rows:
        return jsonify({"success": False, "message": "No emails found to process"})

    generated = 0
    for row in rows:
        body = row.body or ""
        if not body:
            row.action_items = "No action."
            continue

        try:
            action = get_an_action(body)
        except Exception:
            logger.exception("get_an_action failed for cached email %s", row.id)
            row.action_items = "Error generating action"
            continue

        row.action_items = action or "No action."
        row.calendar = any(
            kw in row.action_items.lower()
            for kw in ["meeting", "conference call", "calendar", "appointment", "call"]
        )

        if action and action.lower() not in ("no action.", "no action", "no action required.", ""):
            existing = Todo.query.filter_by(
                master=current_user.id, item=action, done=False
            ).first()
            if not existing:
                new_todo = Todo(master=current_user.id, item=action, done=False)
                db.session.add(new_todo)
                db.session.flush()
                row.todo_id = new_todo.id
                generated += 1
            else:
                row.todo_id = existing.id

    db.session.commit()
    return jsonify({
        "success": True,
        "generated_count": generated,
        "message": f"Generated {generated} new action items",
    })


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

@app.route("/subscribe")
@login_required
def subscribe():
    if current_user.subscribed:
        return redirect(url_for('index'))
    return render_template("subscribe.html")


@app.route("/manage_subscription")
@login_required
def manage_subscription():
    if not current_user.subscribed:
        return render_template("subscribe.html")
    return render_template("manage.html")


@app.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    try:
        prices = stripe.Price.list(
            lookup_keys=[request.form['lookup_key']],
            expand=['data.product'],
        )

        if not current_user.stripe_customer_id:
            if not current_user.email_accounts:
                return "Please link an email account first", 400
            customer = stripe.Customer.create(
                email=current_user.email_accounts[0].email,
                name=current_user.username,
            )
            current_user.stripe_customer_id = customer.id
            db.session.commit()

        checkout_session = stripe.checkout.Session.create(
            customer=current_user.stripe_customer_id,
            line_items=[{'price': prices.data[0].id, 'quantity': 1}],
            mode='subscription',
            success_url=DOMAIN + '/emails',
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
            return_url=DOMAIN,
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
        logger.error("Stripe webhook secret not configured; rejecting request")
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
    logger.info("Stripe event: %s", event_type)

    def find_user(customer_id):
        return Master.query.filter_by(stripe_customer_id=customer_id).first()

    customer_id = data_object.get('customer') if isinstance(data_object, dict) else None

    if event_type == 'checkout.session.completed':
        user = find_user(customer_id)
        if user:
            user.subscribed = True
            db.session.commit()
    elif event_type == 'customer.subscription.created':
        user = find_user(customer_id)
        if user:
            user.subscribed = True
            db.session.commit()
    elif event_type == 'customer.subscription.updated':
        user = find_user(customer_id)
        if user:
            user.subscribed = data_object.get('status') in ['active', 'trialing']
            db.session.commit()
    elif event_type == 'customer.subscription.deleted':
        user = find_user(customer_id)
        if user:
            user.subscribed = False
            db.session.commit()

    return jsonify({'status': 'success'})


# ---------------------------------------------------------------------------
# Scheduling preferences
# ---------------------------------------------------------------------------

@app.route("/set_time", methods=["POST", "GET"])
@login_required
def set_time():
    if request.method == "POST":
        try:
            tz = request.form.get("timezone")
            times = request.form.getlist("time")

            if not tz:
                return render_template("time.html", message="Please select a timezone")
            if not times:
                return render_template("time.html", message="Please select at least one time")

            times = times[:3]
            times_str = ",".join(times)
            current_user.timezone = tz
            current_user.time = times_str
            db.session.commit()

            return render_template(
                "time.html",
                message=f"Successfully set time(s) to {times_str} and timezone to {tz}!",
            )
        except Exception:
            logger.exception("Error setting time and timezone")
            return render_template("time.html", message="Error setting time and timezone")

    time_val = current_user.time
    tz_val = current_user.timezone
    if time_val and tz_val:
        message = f"Update your time(s) from {time_val} in timezone {tz_val}"
    else:
        message = "Set your time and timezone for your daily to-do list to be sent to you!"
    return render_template("time.html", message=message)


# ---------------------------------------------------------------------------
# CSRF exemptions for JSON endpoints that JS already calls (until the frontend
# is updated to send X-CSRFToken headers).
# ---------------------------------------------------------------------------

csrf.exempt(login)
csrf.exempt(delete_account)
csrf.exempt(add_to_calendar)
csrf.exempt(get_one_action)
csrf.exempt(remove_todo)
csrf.exempt(generate_pending_actions)


# Expose the redis client for functions/scheduler.py to reach.
app._redis_client = _redis_client
