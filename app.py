from flask import Flask, render_template, url_for, request, redirect, session, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from sqlalchemy.orm import DeclarativeBase
from datetime import datetime, timedelta, timezone, date
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
import os
from functions.production import production, simple
PRODUCTION = production()
SIMPLE = simple()
if not PRODUCTION:
    from dotenv import load_dotenv
    load_dotenv()
    DOMAIN = "http://localhost:5000"
else:
    DOMAIN = os.getenv("DOMAIN", "https://mailmind.fly.dev")
app = Flask(__name__, static_url_path='/static')
if PRODUCTION:
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY")
else:
    app.config.from_pyfile('config.py')
if PRODUCTION:
    app.config['SESSION_COOKIE_SECURE'] = True  
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    DATABASE_URL = os.getenv('DATABASE_URL')
    if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
        print(f"Fixed DATABASE_URL scheme: {DATABASE_URL[:50]}...")
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///database.db"
class Base(DeclarativeBase):
    pass
db = SQLAlchemy(model_class=Base)
db.init_app(app)
migrate = Migrate(app, db)
from flask_session import Session
from redis import Redis
app.config["SESSION_TYPE"] = "redis"
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_USE_SIGNER"] = True
if PRODUCTION:
    app.config["SESSION_REDIS"] = Redis(
    host='fly-mailmind-redis.upstash.io',
    port=6379,
    password=os.getenv("REDIS_PASSWORD")
    )
else:
    app.config["SESSION_REDIS"] = Redis(host="localhost", port=6379)
Session(app)
import requests
import json
import base64
from google_auth_oauthlib.flow import Flow
import markdown
from flask_login import login_required, LoginManager, login_user, logout_user, current_user
from models import EmailAccount, Master, Link, Unsubscribe, Todo
from functions.refresh_token import refresh
from functions.users import create_email, create_master
from functions.linkify import linkify_text
from functions.get_one_action import get_an_action
from functions.encryption import encrypt_token, decrypt_token
from functions.get_emails import get_emails
from functions.scheduler import check_and_send_emails
import re
import short_url
import secrets
import urllib.parse
from werkzeug.security import generate_password_hash, check_password_hash
import time
import markdown    
import copy
from dateutil import parser
import stripe 
stripe.api_key = os.getenv("STRIPE_API_KEY")
import logging 
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
from flask_login import AnonymousUserMixin, login_user, logout_user
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
with app.app_context():
    db.create_all()

GOOGLE_SCOPES = ['https://mail.google.com/', 
                 'https://www.googleapis.com/auth/userinfo.email', 
                 'openid',
                 'https://www.googleapis.com/auth/calendar.calendars.readonly',
                'https://www.googleapis.com/auth/calendar.events.owned',
                 ]
GOOGLE_REDIRECT_URI = f"{DOMAIN}/google/callback"

client_config = {
    "web": {
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": "GOOGLE_REDIRECT_URI",
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

# Microsoft Graph endpoints
MICROSOFT_AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MICROSOFT_USERINFO_URL = "https://graph.microsoft.com/v1.0/me"

@login_manager.user_loader 
def load_user(id):
    return Master.query.get(int(id))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")
    
    if request.method == "POST":
        data = request.get_json()
        mode = data.get("mode")
        username = data.get("username")
        password = data.get("password")

        # Validation
        if not username or not password:
            return jsonify({"success": False, "message": "All fields are required."}), 400

        if mode == "register":
            # Check if username exists
            if Master.query.filter_by(username=username).first():
                return jsonify({"success": False, "message": "Username already taken."}), 400
            
            # Validate password length
            if len(password) < 6:
                return jsonify({"success": False, "message": "Password must be at least 6 characters."}), 400
            
            try:
                hashed_pw = generate_password_hash(password)
                user = create_master(username, hashed_pw)
                login_user(user)
                return jsonify({"success": True, "message": "Account created successfully!", "redirect": url_for("portal")}), 201
            except Exception as e:
                return jsonify({"success": False, "message": "An error occurred during registration."}), 500

        elif mode == "login":
            user = Master.query.filter_by(username=username).first()
            if user and check_password_hash(user.password, password):
                login_user(user)
                return jsonify({"success": True, "message": "Login successful!", "redirect": url_for("summary")}), 200
            else:
                return jsonify({"success": False, "message": "Invalid username or password."}), 401
        
        else:
            return jsonify({"success": False, "message": "Invalid mode."}), 400

@app.route("/check_username")
def check_username():
    username = request.args.get("username", "").strip()
    if not username:
        return jsonify({"exists": False}), 400
    exists = Master.query.filter_by(username=username).first() is not None
    return jsonify({"exists": exists}), 200

@app.route("/portal")
def portal():
    if not current_user.subscribed:
        return redirect(url_for("index"))
    return render_template("portal.html", accounts=current_user.email_accounts)


@app.route("/delete_account", methods=["GET", "POST"])
def delete_account():
    try:
        data = request.get_json()
        id = data["id"]
        account = EmailAccount.query.filter_by(id=id).first()
        print(account)
        db.session.delete(account)
        db.session.commit()
        return jsonify({"success": True}, 200)
    
    except Exception as e:
        print(e)
        return jsonify({"success": False}, 200)
    


@app.route("/check")
def check():
    check_and_send_emails()
    return render_template("index.html")


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/')
def index():
    return render_template('index.html')

@app.route("/special")
def special():
    return render_template("index.html")


@app.route("/code", methods=["POST", "GET"])
@login_required
def code():
    if request.method == "POST":
        code = request.form.get("code")
        if PRODUCTION:
            real_code = os.getenv("CODE")
        else:
            from dotenv import load_dotenv 
            load_dotenv()
        real_code = os.getenv("CODE")
        temp_code = os.getenv("TEMP_CODE")
        print(code, temp_code)
        if str(code) == str(temp_code):
            logger.info(f"TEMP CODE GRANTED TO {current_user.username}")
            current_user.subscribed = True
            current_user.temp = True
            db.session.commit()
            return render_template("code.html", message="Code valid. Subscription granted until launch. Click on To-Do List to load your to-do list!")
        elif str(code) == str(real_code) and str(real_code) != "DISABLED":
            logger.info(f"CODE GRANTED TO {current_user.username}")
            current_user.subscribed = True
            db.session.commit()
            return render_template("code.html", message="Code valid. Subscription granted. Click on To-Do List to load your to-do list!")
        else:
            return render_template("code.html", message="Invalid code.")

    return render_template("code.html", message=False)

@app.route("/google/login")
def google_login():
    flow = Flow.from_client_config(
        client_config,
        scopes=GOOGLE_SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI
    )
    auth_url, _ = flow.authorization_url(prompt="consent", access_type='offline')
    
    return redirect(auth_url)

@app.route('/google/callback')
def google_callback():
    flow = Flow.from_client_config(
        client_config,
        scopes=GOOGLE_SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI,
    )
    authorization_response = request.url.replace("http", "https")
    flow.fetch_token(authorization_response = authorization_response)
    credentials = flow.credentials
    session["google_credentials"] = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": credentials.scopes
    }
    userinfo_endpoint = 'https://www.googleapis.com/oauth2/v2/userinfo'
    response = requests.get(
        userinfo_endpoint,
        headers={'Authorization': f"Bearer {credentials.token}"}
    )
    user_info = response.json()
    user_email = user_info.get('email')
    user = EmailAccount.query.filter_by(email=user_email).first()
    if not user:
        user = create_email(user_email, encrypt_token(credentials.refresh_token), provider="google", master=current_user)
    else:
        user.oauth_token = encrypt_token(credentials.refresh_token)
        user.provider = "google"
    db.session.commit()
    if current_user.subscribed:
        if current_user.time and current_user.timezone:
            return redirect(url_for("summary"))
        else:
            return redirect(url_for("set_time"))
    else:
        return redirect(url_for("code"))

@app.route("/microsoft/login")
def microsoft_login():
    # Generate state parameter for security
    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state
    
    # Build authorization URL
    auth_params = {
        'client_id': os.getenv("MICROSOFT_CLIENT_ID"),
        'response_type': 'code',
        'redirect_uri': OUTLOOK_REDIRECT_URI,
        'scope': ' '.join(OUTLOOK_SCOPES),
        'state': state,
        'response_mode': 'query'
    }
    
    auth_url = MICROSOFT_AUTH_URL + '?' + urllib.parse.urlencode(auth_params)
    return redirect(auth_url)

@app.route('/microsoft/callback')
def microsoft_callback():
    # Verify state parameter
    if request.args.get('state') != session.get('oauth_state'):
        return "Invalid state parameter", 400
    
    # Get authorization code
    auth_code = request.args.get('code')
    if not auth_code:
        return "Authorization code not found", 400
    
    # Exchange code for tokens
    token_data = {
        'client_id': os.getenv("MICROSOFT_CLIENT_ID"),
        'client_secret': os.getenv("MICROSOFT_CLIENT_SECRET"),
        'code': auth_code,
        'redirect_uri': OUTLOOK_REDIRECT_URI,
        'grant_type': 'authorization_code',
        'scope': ' '.join(OUTLOOK_SCOPES)
    }
    
    token_response = requests.post(
        MICROSOFT_TOKEN_URL,
        data=token_data,
        headers={'Content-Type': 'application/x-www-form-urlencoded'}
    )
    
    if token_response.status_code != 200:
        return f"Token exchange failed: {token_response.text}", 400
    
    token_info = token_response.json()
    
    # Store credentials in session
    session["microsoft_credentials"] = {
        "access_token": token_info.get("access_token"),
        "refresh_token": token_info.get("refresh_token"),
        "token_type": token_info.get("token_type", "Bearer"),
        "expires_in": token_info.get("expires_in"),
        "scope": token_info.get("scope")
    }
    
    # Get user info from Microsoft Graph
    headers = {
        'Authorization': f"Bearer {token_info['access_token']}",
        'Content-Type': 'application/json'
    }
    user_response = requests.get(MICROSOFT_USERINFO_URL, headers=headers)
    
    if user_response.status_code != 200:
        return f"Failed to get user info: {user_response.text}", 400
    
    user_info = user_response.json()
    user_email = user_info.get('mail') or user_info.get('userPrincipalName')
    
    # Handle user creation/update (similar to Google implementation)
    user = EmailAccount.query.filter_by(email=user_email).first()
    if not user:
        user = create_email(user_email, encrypt_token(token_info.get("refresh_token")), provider="microsoft", master=current_user)
    else:
        user.oauth_token = encrypt_token(token_info.get("refresh_token"))
    
    db.session.commit()
    
    # Clean up session
    session.pop('oauth_state', None)
    
    if current_user.time and current_user.timezone:
        return redirect(url_for("summary"))
    else:
        return redirect(url_for("set_time"))

# Helper function to refresh Outlook tokens
def refresh_outlook_token(refresh_token):
    """Refresh an expired Outlook access token"""
    token_data = {
        'client_id': os.getenv("MICROSOFT_CLIENT_ID"),
        'client_secret': os.getenv("MICROSOFT_CLIENT_SECRET"),
        'refresh_token': refresh_token,
        'grant_type': 'refresh_token',
        'scope': ' '.join(OUTLOOK_SCOPES)
    }
    
    response = requests.post(
        MICROSOFT_TOKEN_URL,
        data=token_data,
        headers={'Content-Type': 'application/x-www-form-urlencoded'}
    )
    
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"Token refresh failed: {response.text}")



@app.template_filter('markdown')
def markdown_filter(text):
    return markdown.markdown(text)

@app.template_filter("remove_asterisks")
def remove_asterisks(text):
    return text.replace("*", "")

app.jinja_env.filters['markdown'] = markdown_filter
app.jinja_env.filters['linkify_text'] = linkify_text
app.jinja_env.filters["remove_asterisks"] = remove_asterisks

@app.route("/sessionclear")
def session_clear():
    session.clear()
    return render_template("index.html")

@app.route("/request_access")
def request_access():
    return render_template("request_access.html")

@app.route("/contact")
def contact():
    return render_template("contact.html")

@app.route('/emails')
@login_required
def emails():
    if not current_user.subscribed:
        return render_template("subscribe.html")

    
    # Handle refresh case - where we're adding new emails to existing ones
    if session.get('final_emails', False):
        print('refresh')
        
        # Get the last load time from session
        if 'last_load' in session:
            last_load_val = session.get('last_load')
            print(f"Raw last_load from session: {last_load_val}")
            
            # Parse the last_load value
            if isinstance(last_load_val, str):
                try:
                    last_load = parser.parse(last_load_val)
                    # Ensure it has timezone info
                    if last_load.tzinfo is None:
                        last_load = last_load.replace(tzinfo=timezone.utc)
                except Exception as e:
                    print(f"Error parsing date: {str(e)}")
                    last_load = datetime.now(timezone.utc)
            elif isinstance(last_load_val, datetime):
                last_load = last_load_val
                # Ensure it has timezone info
                if last_load.tzinfo is None:
                    last_load = last_load.replace(tzinfo=timezone.utc)
            else:
                print(f"Unexpected type for last_load: {type(last_load_val)}")
                last_load = datetime.now(timezone.utc)
        else:
            last_load = datetime.now(timezone.utc)
            print(f"No last_load in session, using current time: {last_load}")
        
        # Format date and time for get_emails
        after_date = last_load.strftime("%m-%d-%y")
        since_time = last_load.strftime("%H:%M:%S")
        
        # Get new emails since last load
        print("Calling get_emails for refresh...")
        final_emails = session.get("final_emails", [])
        for email_account in current_user.email_accounts:
            new_emails = get_emails(email_account.provider, email_account.email, refresh(email_account), 
                                after_date=after_date, since_time=since_time)
            
            print(f"Found {len(new_emails)} new emails in refresh for {current_user}'s email: {email_account.email}")
            
            first = True
            for email in new_emails:
                email["action_items"] = "Generating ..."
                email["calendar"] = False
                email["email"] = email_account.email
                if first:
                    email["change"] = True
                    first = False 
                else:
                    email["change"] = False

                final_emails.append(email)

            for email in final_emails:
                # Check if action item already has calendar keywords
                if email.get("action_items") and isinstance(email["action_items"], str):
                    if any(keyword in email["action_items"].lower() for keyword in ["meeting", "conference call", "calendar", "appointment", "call"]):
                        email["calendar"] = True
                    else:
                        email["calendar"] = False
                else:
                    email["calendar"] = False
                
            
        session['final_emails'] = final_emails
        current_datetime = datetime.now(timezone.utc)
        session['last_load'] = current_datetime.isoformat()
        return render_template('emails.html', emails=final_emails)

    # Initial load - get emails from the last 24 hours
    current_datetime = datetime.now(timezone.utc)
    session['last_load'] = current_datetime.isoformat()

    # Get emails from exactly 24 hours ago (no parameters = use default 24h window)
    # This will fetch using an IMAP date filter 2 days back, then filter precisely in Python
    final_emails = []
    for email_account in current_user.email_accounts:
        emails = get_emails(email_account.provider, email_account.email, refresh(email_account))
    
        # Reverse order for newest first
        emails = list(reversed(emails))
        
        print("Length: ", len(emails))
        # Process unsubscribe links

        first = True    
        for email in emails: 
            if email not in final_emails: 
                email["action_items"] = "Generating ..."
                email["email"] = email_account.email
                email["calendar"] = False
                final_emails.append(email)
                if first:
                    email["change"] = True 
                    first = False 
                else:
                    email["change"] = False
                
        session["final_emails"] = final_emails
            
    return render_template('emails.html', emails=final_emails)

from dateutil.tz import tzlocal  

from functions.calendar import create_google_calendar_event, create_microsoft_calendar_event

@app.route("/add_to_calendar", methods=["POST"])
@login_required
def add_to_calendar():
    try:
        start = request.form.get("start")
        end = request.form.get("end")
        name = request.form.get("name")
        account = EmailAccount.query.filter_by(id = int(request.form.get("account"))).first()

        if not start or not end or not name:
            return jsonify({"success": False, "error": "Missing required fields"})
        
        start_dt = datetime.strptime(start, "%Y-%m-%dT%H:%M").replace(tzinfo=tzlocal())
        end_dt = datetime.strptime(end, "%Y-%m-%dT%H:%M").replace(tzinfo=tzlocal())

        if end_dt <= start_dt:
            return jsonify({"success": False, "error": "End time must be after start time"})
        
        # Branch based on user's calendar provider
        if account.provider == "google":
            return create_google_calendar_event(start_dt, end_dt, name, account)
        elif account.provider == "microsoft":
            return create_microsoft_calendar_event(start_dt, end_dt, name, account)
        else:
            return jsonify({"success": False, "error": "Unsupported calendar provider"})
            
    except ValueError as e:
        return jsonify({"success": False, "error": "Invalid date format"})
    except Exception as e:
        print(f"Calendar API error: {str(e)}")
        return jsonify({"success": False, "error": "Failed to create calendar event"})


@app.route("/get_one_action", methods=["POST"])
@login_required 
def get_one_action():
    data = request.get_json()
    body = data.get("body")
    index = int(data.get("index"))
    print(index)
    calendar = False
    action = get_an_action(body)
    print(action)
    if "meeting" in action.lower() or "conference call" in action.lower() or "calendar" in action.lower():
        calendar = True
    final_emails = session.get("final_emails")
    if action.lower() != "no action." and action.lower() != "no action" and action != "" and action is not None:
        new_todo = Todo(master=current_user.id, item=action, done=False)
        db.session.add(new_todo)
        db.session.commit()
        todo_emails = session.get("todo_emails", [])
        todo_email = final_emails[index]
        todo_email.pop("action_items", None)
        todo_email["todo"] = action
        todo_email["id"] = new_todo.id 
        todo_emails.append(todo_email)
        session["todo_emails"] = todo_emails
    else:
        action = "No action."

    final_emails[index]["action_items"] = action
    session["final_emails"] = final_emails
    accounts = []
    for account in current_user.email_accounts:
        accounts.append({"id": account.id, "email": account.email})
    return jsonify({"action_item": action, "calendar": calendar, "accounts": accounts})

@app.route("/remove_todo", methods=["POST"])
@login_required
def remove_todo():
    data = request.get_json()
    todo_id = int(data.get("id"))  
    
    todo_emails = session.get("todo_emails", [])
    if todo_emails:
        todo = Todo.query.filter_by(id=todo_id).first()
        if todo:
            db.session.delete(todo)
            db.session.commit()
        
        todo_emails = [email for email in todo_emails if email["id"] != todo_id]
        session["todo_emails"] = todo_emails
    
    return jsonify({"success": True})

@app.route("/termsandprivacy")
def terms_and_privacy():
    return render_template("termsandprivacy.html")

@app.route('/summary')
@login_required
def summary():
    if not current_user.subscribed:
        return render_template("subscribe.html")
    
    
    # Handle refresh case - where we're adding new emails to existing ones
    if session.get('final_emails', False):
        print('refresh in summary')
        
        # Get the last load time from session
        if 'last_load' in session:
            last_load_val = session.get('last_load')
            print(f"Raw last_load from session: {last_load_val}")
            
            # Parse the last_load value
            if isinstance(last_load_val, str):
                try:
                    last_load = parser.parse(last_load_val)
                    # Ensure it has timezone info
                    if last_load.tzinfo is None:
                        last_load = last_load.replace(tzinfo=timezone.utc)
                except Exception as e:
                    print(f"Error parsing date: {str(e)}")
                    last_load = datetime.now(timezone.utc)
            elif isinstance(last_load_val, datetime):
                last_load = last_load_val
                # Ensure it has timezone info
                if last_load.tzinfo is None:
                    last_load = last_load.replace(tzinfo=timezone.utc)
            else:
                print(f"Unexpected type for last_load: {type(last_load_val)}")
                last_load = datetime.now(timezone.utc)
        else:
            last_load = datetime.now(timezone.utc)
            print(f"No last_load in session, using current time: {last_load}")
        
        # Format date and time for get_emails
        after_date = last_load.strftime("%m-%d-%y")
        since_time = last_load.strftime("%H:%M:%S")
        
        # Get new emails since last load
        print("Calling get_emails for refresh in summary...")
        final_emails = session.get("final_emails", [])
        for email_account in current_user.email_accounts:
            new_emails = get_emails(email_account.provider, email_account.email, refresh(email_account), 
                                after_date=after_date, since_time=since_time)
            
            print(f"Found {len(new_emails)} new emails in refresh")
            
            
            first = True 
            for email in new_emails:
                email["action_items"] = "Generating ..."
                email["calendar"] = False

                email["email"] = email_account.email
                if first:
                    email["change"] = True 
                    first = False 
                else:
                    email["change"] = False
                final_emails.append(email)

            for email in final_emails:
                # Check if action item already has calendar keywords
                if email.get("action_items") and isinstance(email["action_items"], str):
                    if any(keyword in email["action_items"].lower() for keyword in ["meeting", "conference call", "calendar", "appointment", "call"]):
                        email["calendar"] = True
                    else:
                        email["calendar"] = False
                else:
                    email["calendar"] = False
                
            session['final_emails'] = final_emails
            current_datetime = datetime.now(timezone.utc)
            session['last_load'] = current_datetime.isoformat()
        
    else:
        # Initial load - get emails from the last 24 hours
        current_datetime = datetime.now(timezone.utc)
        session['last_load'] = current_datetime.isoformat()

        final_emails = []
        # Get emails from exactly 24 hours ago (no parameters = use default 24h window)
        for index, email_account in enumerate(current_user.email_accounts):
            emails = get_emails(email_account.provider, email_account.email, refresh(email_account))
            
            # Reverse order for newest first
            emails = list(reversed(emails))
            
            # Process unsubscribe links

            
            first = True   
            for email in emails: 
                if email not in final_emails: 
                    email["action_items"] = "Generating ..."
                    email["calendar"] = False
                    email["email"] = email_account.email
                    if first:
                        email["change"] = True 
                        first = False 
                    else:
                        email["change"] = False
                    final_emails.append(email)
                    
            session["final_emails"] = final_emails
        
    print("out of if block")
    # Get existing todo emails from database
    todos = Todo.query.filter_by(master=current_user.id, done=False).all()
    print(len(todos))
    # Create a map of existing todos by email content for faster lookup
    existing_todos = {}
    for todo in todos:
        # Try to find matching email in final_emails
        for email in final_emails:
            if email.get("action_items") == todo.item:
                existing_todos[id(email)] = {
                    'todo': todo.item,
                    'id': todo.id,
                    'calendar': any(keyword in todo.item.lower() for keyword in ["meeting", "conference call", "calendar", "appointment", "call"])
                }
                # Update the email with the saved action item
                email["action_items"] = todo.item
                email["calendar"] = existing_todos[id(email)]['calendar']
                break
    
    # Clean up orphaned todos (todos that don't match any current emails)
    for todo in todos:
        found_match = False
        for email in final_emails:
            if email.get("action_items") == todo.item:
                found_match = True
                break
        if not found_match:
            db.session.delete(todo)
    db.session.commit()
    
    # Count emails that still need processing
    emails_needing_processing = []
    for i, email in enumerate(final_emails):
        if email.get("action_items") == "Generating ..." or not email.get("action_items"):
            emails_needing_processing.append({
                'index': i,
                'email': email
            })
    
    # Set appropriate message
    if emails_needing_processing:
        text = f"Processing {len(emails_needing_processing)} emails for action items..."
    else:
        # Filter emails that have actual action items (not just "No action.")
        actionable_emails = [email for email in final_emails 
                           if email.get("action_items") and 
                           email["action_items"] != "No action." and
                           email["action_items"] != "Generating ..."]
        if not actionable_emails:
            text = "No action items found from recent emails."
        else:
            text = f"Found {len(actionable_emails)} action items"
    
    return render_template('summary.html', emails=final_emails, text=text, accounts = current_user.email_accounts, 
                         pending_count=len(emails_needing_processing))



@app.route('/generate_pending_actions', methods=['POST'])
@login_required
def generate_pending_actions():
    """Generate action items for emails that don't have them yet"""
    final_emails = session.get("final_emails", [])
    if not final_emails:
        return jsonify({
            "success": False,
            "message": "No emaiils found to process"
        })
    
    generated_count = 0
    
    for index, email in enumerate(final_emails):
        # Skip if already has action items
        if email.get("action_items") and email.get("action_items") != "Generating ...":
            continue
            
        try:
            # Get action item for this email
            body = email.get("body", "")
            if not body:
                final_emails[index]["action_items"] = "No action"
                continue
                
            action = get_an_action(body)  # Your existing function
            
            # Update the email with action item
            final_emails[index]["action_items"] = action
            
            # Add to database if it's a real action
            if action and action.lower() not in ["no action.", "no action", "no action required.", ""]:
                # Check if this action already exists for this user
                existing_todo = Todo.query.filter_by(master=current_user.id, item=action, done=False).first()
                
                if not existing_todo:
                    # Save to database
                    new_todo = Todo(master=current_user.id, item=action, done=False)
                    db.session.add(new_todo)
                    db.session.commit()
                    generated_count += 1
                    
        except Exception as e:
            print(f"Error generating action for email {index}: {e}")
            final_emails[index]["action_items"] = "Error generating action"
            continue
    
    # Update session
    session["final_emails"] = final_emails
    
    return jsonify({
        "success": True,
        "generated_count": generated_count,
        "message": f"Generated {generated_count} new action items"
    })

@app.route("/subscribe")
@login_required
def subscribe():
    if type(current_user) == AnonymousUserMixin:
        print("redirecting")
        return redirect(url_for('login'))
    if current_user.subscribed:
        return redirect(url_for('index'))
    return render_template("subscribe.html")
 

@app.route("/manage_subscription")
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
            expand=['data.product']
        )

        # Create or get Stripe customer for current user
        if not current_user.stripe_customer_id:
            # Create new Stripe customer
            customer = stripe.Customer.create(
                email=current_user.email_accounts[0].email,
                name=current_user.username
            )
            # Save customer ID to user
            current_user.stripe_customer_id = customer.id
            db.session.commit()

        checkout_session = stripe.checkout.Session.create(
            customer=current_user.stripe_customer_id,  # Link to existing customer
            line_items=[
                {
                    'price': prices.data[0].id,
                    'quantity': 1,
                },
            ],
            mode='subscription',
            success_url=DOMAIN + '/emails',
            cancel_url=DOMAIN,
            subscription_data={
                'trial_period_days': 7
            },
        )
        return redirect(checkout_session.url, code=303)
    except Exception as e:
        print(e)
        return "Server error", 500
    
@login_required
@app.route('/create-portal-session', methods=['POST'])
def customer_portal():
    try:
        # Use the customer ID from the current user's database record
        if not current_user.stripe_customer_id:
            return "No subscription found", 400
            
        return_url = DOMAIN

        portalSession = stripe.billing_portal.Session.create(
            customer=current_user.stripe_customer_id,
            return_url=return_url,
        )
        return redirect(portalSession.url, code=303)
    except Exception as e:
        print(f"Portal session error: {e}")
        return "Server error", 500

@app.route('/webhook', methods=['POST'])
def webhook_received():
    if PRODUCTION:
        webhook_secret = os.getenv("WEBHOOK_SECRET")
    else:
        webhook_secret = os.getenv("TEST_WEBHOOK")
    
    if webhook_secret:
        signature = request.headers.get('stripe-signature')
        try:
            event = stripe.Webhook.construct_event(
                payload=request.data, 
                sig_header=signature, 
                secret=webhook_secret
            )
        except Exception as e:
            print(f"Webhook signature verification failed: {e}")
            return "Bad signature", 400
    else:
        event = json.loads(request.data)

    data = event['data']
    event_type = event['type']
    data_object = data['object']

    print(f"EVENT: {event_type}")

    # Helper function to find user by Stripe customer ID
    def find_user_by_customer_id(customer_id):
        print(customer_id)
        return Master.query.filter_by(stripe_customer_id=customer_id).first()

    if event_type == 'checkout.session.completed':
        print('🔔 Payment succeeded!')
        customer_id = data_object.get('customer')
        user = find_user_by_customer_id(customer_id)
        if user:
            user.subscribed = True
            db.session.commit()
            print(f"User {user.username} subscription activated")

    elif event_type == 'customer.subscription.trial_will_end':
        print('Subscription trial will end')
        customer_id = data_object.get('customer')
        user = find_user_by_customer_id(customer_id)
        if user:
            print(f"Trial ending for user {user.email}")

    elif event_type == 'customer.subscription.created':
        print(f'Subscription created {event["id"]}')
        customer_id = data_object.get('customer')
        user = find_user_by_customer_id(customer_id)
        if user:
            user.subscribed = True
            db.session.commit()
            print(f"Subscription created for user {user.email}")

    elif event_type == 'customer.subscription.updated':
        print(f'Subscription updated {event["id"]}')
        customer_id = data_object.get('customer')
        user = find_user_by_customer_id(customer_id)
        if user:
            # Check subscription status
            subscription_status = data_object.get('status')
            user.subscribed = subscription_status in ['active', 'trialing']
            db.session.commit()
            print(f"Subscription updated for user {user.email}, status: {subscription_status}")

    elif event_type == 'customer.subscription.deleted':
        print(f'Subscription canceled {event["id"]}')
        customer_id = data_object.get('customer')
        user = find_user_by_customer_id(customer_id)
        if user:
            user.subscribed = False
            db.session.commit()
            print(f"Subscription canceled for user {user.email}")

    return jsonify({'status': 'success'})



@app.route("/set_time", methods=["POST", "GET"])
@login_required
def set_time():
    if request.method == "POST":
        try:
            # Get form data
            timezone = request.form.get("timezone")
            times = request.form.getlist("time")  # getlist for multiple selections
            
            # Validate that both timezone and at least one time are provided
            if not timezone:
                return render_template("time.html", message="Please select a timezone")
            
            if not times or len(times) == 0:
                return render_template("time.html", message="Please select at least one time")
            
            # Limit to maximum 3 times (additional safety check)
            if len(times) > 3:
                times = times[:3]
            
            # Store as comma-separated string
            times_str = ",".join(times)
            
            # Update user attributes
            current_user.timezone = timezone
            current_user.time = times_str
            
            db.session.commit()
            
            return render_template("time.html", message= f"Successfully set time(s) to {times_str} and timezone to {timezone}!")
            
        except Exception as e:
            return render_template("time.html", message= f"Error setting time and timezone")
    else:
        time = current_user.time 
        timezone = current_user.timezone 
        if time and timezone:
            return render_template("time.html", message= f"Update your time(s) from {time} in timezone {timezone}")
        else:
            return render_template("time.html", message="Set your time and timezone for your daily to-do list to be sent to you!")
    
@app.route("/beta")
def beta():
    return render_template("beta.html")




