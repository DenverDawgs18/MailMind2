from flask import Flask, render_template, url_for, request, redirect, session, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from sqlalchemy.orm import DeclarativeBase
from datetime import datetime, timedelta, timezone, date
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
import os
from functions.production import production
PRODUCTION = production()
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
from models import User, Link, Unsubscribe, Todo
from functions.refresh_token import refresh
from functions.users import get_last_login, get_user, update_last_login, create_user
from functions.linkify import linkify_text
from functions.reply import reply
from functions.get_one_action import get_an_action
from functions.encryption import encrypt_token, decrypt_token
from functions.selenium import automated_unsubscribe
from functions.get_emails import get_emails
import re
import short_url
import secrets
import urllib.parse
from werkzeug.utils import secure_filename
import time
import markdown    
import copy
from dateutil import parser
import stripe 
stripe.api_key = os.getenv("STRIPE_API_KEY")
import logging 
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
from flask_login import AnonymousUserMixin
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

from functools import wraps
from functions.scheduler import get_users_to_process, send_email_summary_for_user, trigger_email_check
        

def require_admin_key(f):
    """Decorator to require admin key for scheduled tasks"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        admin_key = os.environ.get('ADMIN_KEY', 'your-default-key-here')
        
        # Check for key in header, query param, or form data
        provided_key = (
            request.headers.get('X-Admin-Key') or 
            request.args.get('key') or 
            request.form.get('key')
        )
        
        if not provided_key or provided_key != admin_key:
            return jsonify({
                "error": "Unauthorized",
                "message": "Invalid or missing admin key"
            }), 401
        
        return f(*args, **kwargs)
    return decorated_function



@app.route('/cron/email-scheduler', methods=['POST', 'GET'])
@require_admin_key
def cron_email_scheduler():
    """Endpoint for EasyCron to trigger email scheduler"""
    try:
        
        start_time = datetime.now()
        
        # Get users who need email summaries
        users_to_process = get_users_to_process()
        
        if not users_to_process:
            return jsonify({
                "success": True,
                "message": "No users scheduled for email summaries at this time",
                "users_processed": 0,
                "emails_sent": 0,
                "processing_time": f"{(datetime.now() - start_time).total_seconds():.2f}s",
                "timestamp": datetime.now().isoformat()
            }), 200
        
        # Process each user
        results = []
        for user in users_to_process:
            try:
                result = send_email_summary_for_user(user)
                results.append(result)
                
                # Log the result
                if result["success"]:
                    print(f"✓ Email sent to {user.email}: {result['message']}")
                else:
                    print(f"✗ Failed for {user.email}: {result['message']}")
                    
            except Exception as e:
                error_result = {
                    "success": False,
                    "user": user.email,
                    "message": str(e)
                }
                results.append(error_result)
                print(f"✗ Error processing {user.email}: {str(e)}")
        
        # Calculate summary
        successful = sum(1 for r in results if r["success"])
        processing_time = (datetime.now() - start_time).total_seconds()
        
        return jsonify({
            "success": True,
            "message": f"Processed {len(users_to_process)} users",
            "users_processed": len(users_to_process),
            "emails_sent": successful,
            "failed": len(results) - successful,
            "processing_time": f"{processing_time:.2f}s",
            "results": results,
            "timestamp": datetime.now().isoformat()
        }), 200
        
    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"Error running scheduler: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }), 500

@app.route('/admin/trigger-scheduler', methods=['POST'])
def trigger_scheduler():
    """Manually trigger the email scheduler"""
    try:
        trigger_email_check()
        return jsonify({
            "success": True,
            "message": "Scheduler triggered successfully",
            "timestamp": datetime.now().isoformat()
        }), 200
    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"Error triggering scheduler: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }), 500

@login_manager.user_loader 
def load_user(id):
    return User.query.get(int(id))



@app.route("/login")
def login():
    return render_template("login.html")

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
    if PRODUCTION:
        return render_template("index.html")
    
    trigger_email_check()
    return render_template("index.html")



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
        temp_code = os.getenv("TEMPCODE")
        if str(code) == str(temp_code):
            logger.info(f"TEMP CODE GRANTED TO {current_user.email}")
            current_user.subscribed = True
            current_user.temp = True
            db.session.commit()
            return render_template("code.html", message="Code valid. Subscription granted until August 1st. Click on Inbox to load your to-do list!")
        if str(code) == str(real_code) and str(real_code) != "DISABLED":
            logger.info(f"CODE GRANTED TO {current_user.email}")
            current_user.subscribed = True
            db.session.commit()
            return render_template("code.html", message="Code valid. Subscription granted. Click on Inbox to load your to-do list!")
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
    auth_url, _ = flow.authorization_url(prompt="consent")
    
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
    session['user_email'] = user_info.get('email')
    user = User.query.filter_by(email=session['user_email']).first()
    if not user:
        user = create_user(session['user_email'], encrypt_token(credentials.refresh_token), provider="google")
    else:
        user.oauth_token = encrypt_token(credentials.refresh_token)
        user.provider = "google"
    db.session.commit()
    login_user(user, remember = True)
    if user.time and user.timezone:
        return redirect(url_for("summary"))
    else:
        return redirect(url_for("set_time"))

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
    session['user_email'] = user_info.get('mail') or user_info.get('userPrincipalName')
    
    # Handle user creation/update (similar to Google implementation)
    user = User.query.filter_by(email=session['user_email']).first()
    if not user:
        user = create_user(session['user_email'], encrypt_token(token_info.get("refresh_token")), provider="microsoft")
    else:
        user.oauth_token = encrypt_token(token_info.get("refresh_token"))
    
    db.session.commit()
    login_user(user, remember=True)
    
    # Clean up session
    session.pop('oauth_state', None)
    
    if user.time and user.timezone:
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
    access_token = refresh(current_user)
    
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
        new_emails = get_emails(current_user.provider, current_user.email, access_token, 
                             after_date=after_date, since_time=since_time)
        
        print(f"Found {len(new_emails)} new emails in refresh")
        
        # Process unsubscribe links for new emails
        process_unsubscribe_links(new_emails, current_user)
        
        # Add action items placeholder and merge with existing emails
        final_emails = []
        for email in new_emails:
            email["action_items"] = "Generating ..."
            email["calendar"] = False
            final_emails.append(email)
            
        emails = session.get("final_emails")
        for email in emails:
            if "meeting" in email["action_items"].lower() or "conference call" in email["action_items"].lower() or "calendar" in email["action_items"].lower():
                email["calendar"] = True
            else:
                email["calendar"] = False
            final_emails.append(email)
            
        
        session['final_emails'] = final_emails
        current_datetime = datetime.now(timezone.utc)
        session['last_load'] = current_datetime.isoformat()
        return render_template('emails.html', emails=final_emails)

    # Initial load - get emails from the last 24 hours
    current_datetime = datetime.now(timezone.utc)
    session['last_load'] = current_datetime.isoformat()

    # Get emails from exactly 24 hours ago (no parameters = use default 24h window)
    # This will fetch using an IMAP date filter 2 days back, then filter precisely in Python
    emails = get_emails(current_user.provider, current_user.email, access_token)
    
    # Reverse order for newest first
    emails = list(reversed(emails))
    
    # Process unsubscribe links
    process_unsubscribe_links(emails, current_user)
    
    # Sort by priority and add action items placeholder
    final_emails = []
    high_priority = [email for email in emails if email['from'] in current_user.high_priority] 
    
    for email in high_priority:
        email["action_items"] = "Generating ..."
        final_emails.append(email)
        
    for email in emails: 
        if email not in final_emails: 
            email["action_items"] = "Generating ..."
            final_emails.append(email)
            
    session["final_emails"] = final_emails
            
    return render_template('emails.html', emails=final_emails)

from dateutil.tz import tzlocal  

@app.route("/add_to_calendar", methods=["POST"])
@login_required
def add_to_calendar():
    try:
        start = request.form.get("start")
        end = request.form.get("end")
        name = request.form.get("name")
        
        if not start or not end or not name:
            return jsonify({"success": False, "error": "Missing required fields"})
        
        start_dt = datetime.strptime(start, "%Y-%m-%dT%H:%M").replace(tzinfo=tzlocal())
        end_dt = datetime.strptime(end, "%Y-%m-%dT%H:%M").replace(tzinfo=tzlocal())

        if end_dt <= start_dt:
            return jsonify({"success": False, "error": "End time must be after start time"})
        
        # Branch based on user's calendar provider
        if current_user.provider == "google":
            return create_google_calendar_event(start_dt, end_dt, name)
        elif current_user.provider == "microsoft":
            return create_microsoft_calendar_event(start_dt, end_dt, name)
        else:
            return jsonify({"success": False, "error": "Unsupported calendar provider"})
            
    except ValueError as e:
        return jsonify({"success": False, "error": "Invalid date format"})
    except Exception as e:
        print(f"Calendar API error: {str(e)}")
        return jsonify({"success": False, "error": "Failed to create calendar event"})

def create_google_calendar_event(start_dt, end_dt, name):
    """Create event in Google Calendar"""
    try:
        access_token = refresh(current_user)
        creds = Credentials(token=access_token)
        service = build("calendar", "v3", credentials=creds)
        
        # Create event
        event = {
            "summary": name,
            "start": {
                'dateTime': start_dt.isoformat()
            },
            "end": {
                "dateTime": end_dt.isoformat()
            }
        }
        
        # Insert event into calendar
        event_result = service.events().insert(calendarId='primary', body=event).execute()
        
        return jsonify({
            "success": True, 
            "event_id": event_result.get('id'),
            "provider": "google"
        })
        
    except Exception as e:
        print(f"Google Calendar API error: {str(e)}")
        return jsonify({"success": False, "error": "Failed to create Google calendar event"})

def create_microsoft_calendar_event(start_dt, end_dt, name):
    """Create event in Microsoft Calendar (Outlook)"""
    try:
        access_token = refresh(current_user)  # Assuming you have a Microsoft refresh function
        
        # Microsoft Graph API endpoint
        url = "https://graph.microsoft.com/v1.0/me/events"
        
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        
        # Create event payload for Microsoft Graph
        event_data = {
            "subject": name,
            "start": {
                "dateTime": start_dt.isoformat(),
                "timeZone": str(start_dt.tzinfo) if start_dt.tzinfo else "UTC"
            },
            "end": {
                "dateTime": end_dt.isoformat(),
                "timeZone": str(end_dt.tzinfo) if end_dt.tzinfo else "UTC"
            }
        }
        
        response = requests.post(url, json=event_data, headers=headers)
        
        if response.status_code == 201:
            event_result = response.json()
            return jsonify({
                "success": True, 
                "event_id": event_result.get('id'),
                "provider": "microsoft"
            })
        else:
            print(f"Microsoft Graph API error: {response.status_code} - {response.text}")
            return jsonify({
                "success": False, 
                "error": f"Microsoft API error: {response.status_code}"
            })
            
    except Exception as e:
        print(f"Microsoft Calendar API error: {str(e)}")
        return jsonify({"success": False, "error": "Failed to create Microsoft calendar event"})

def process_unsubscribe_links(emails, current_user):
    """Helper function to process unsubscribe links in emails"""
    changes_made = False
    
    for email in emails:
        unsubscribe_match = re.search(r"(?i)unsubscribe", email['body'])
                 
        if unsubscribe_match:
            unsubscribe_pos = unsubscribe_match.start()
            remaining_text = email['body'][unsubscribe_pos:]
            link_match = re.search(r"\[LINK:\s*([^\]]+)\]", remaining_text)
                         
            if link_match:
                code = link_match.group(1)
                try:
                    link_id = short_url.decode_url(code)
                    link_obj = Link.query.filter_by(id=link_id).first()
                                         
                    if link_obj and link_obj.link:
                        real_link = link_obj.link
                        unsubj = Unsubscribe.query.filter_by(sender=email["from"]).first()
                                                 
                        if not unsubj:
                            new_unsub = Unsubscribe(sender=email['from'], link=real_link, user=current_user.id)
                            db.session.add(new_unsub)
                            print(f"Added unsubscribe link for {email['from']}: {real_link}")
                            changes_made = True
                except Exception as e:
                    print(f"Error processing unsubscribe link: {str(e)}")
                    continue
    
    # Only commit if there were changes made
    if changes_made:
        try:
            db.session.commit()
            print("Successfully committed all unsubscribe link changes")
        except Exception as e:
            print(f"Error committing unsubscribe link changes: {str(e)}")
            db.session.rollback()

@app.route("/get_one_action", methods=["POST"])
@login_required 
def get_one_action():
    data = request.get_json()
    body = data.get("body")
    index = int(data.get("index"))
    print(index)
    calendar = False
    action = get_an_action(body)
    if "meeting" in action.lower() or "conference call" in action.lower() or "calendar" in action.lower():
        calendar = True
    final_emails = session.get("final_emails")
    if action.lower() != "no action." and action.lower() != "no action" and action != "" and action is not None:
        new_todo = Todo(user=current_user.id, item=action, done=False)
        db.session.add(new_todo)
        db.session.commit()
        todo_emails = session.get("todo_emails", [])
        todo_email = final_emails[index]
        todo_email.pop("action_items", None)
        todo_email["todo"] = action
        todo_email["id"] = new_todo.id 
        todo_emails.append(todo_email)
        session["todo_emails"] = todo_emails

    final_emails[index]["action_items"] = action
    session["final_emails"] = final_emails
    return jsonify({"action_item": action, "calendar": calendar})

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


'''
@app.route('/load_more', methods=["POST"])
@login_required
def load_more():
    print('loading more')
    final_emails = session.get('final_emails', [])
 
    final_final = final_emails.copy()  
    access_token = refresh(current_user)
    prev = session['since']
    prev = prev.strftime("%m-%d-%y")
    day = session['since'] - timedelta(days=1)
    session['since'] = day
    day = day.strftime("%m-%d-%y")
    new_emails = get_emails("gmail", current_user.email, access_token, after_date=day, since_time=session['time'], old=final_emails)
    new_emails.reverse()
    to_process = []
    for email in new_emails:
        if email not in final_emails:
            to_process.append(email)
    if to_process:
        action_items = batch_get_action_items(to_process)
        for j, email in enumerate(to_process):
            email["action_items"] = action_items[j]
            final_emails.append(email)
        
    session['final_emails'] = final_final

    new_emails = [email for email in final_final if email not in final_emails]
    rendered_emails = render_template("email_snippet.html", emails=new_emails)

    return jsonify({"html": rendered_emails})
'''
            


@app.route('/mark_high_priority', methods=["POST"])
@login_required
def mark_high_priority():
    data = request.get_json()
    sender = data.get("sender")
    if not sender:
        return jsonify({'success': False})
    if current_user.high_priority is None:
            current_user.high_priority = []
    if sender not in current_user.high_priority:
        updated_list = current_user.high_priority + [sender]  
        current_user.high_priority = updated_list 
        db.session.commit()

    print(current_user.high_priority) 

    return jsonify({'success': True})

@app.route('/unmark_high_priority', methods=["POST"])
@login_required 
def unmark_high_priority():
    data = request.get_json()
    sender = data.get('sender')
    if not sender:
        return jsonify({'success': False})
    if sender in current_user.high_priority:
        updated_list = [s for s in current_user.high_priority if s != sender]  
        current_user.high_priority = updated_list  
        db.session.commit()

    print(current_user.high_priority) 

    return jsonify({'success': True})

@app.route('/reply', methods=["POST"])
@login_required
def reply_view():
    data = request.get_json()
    original_from = data.get('from')
    cc = data.get('cc')
    bcc = data.get('bcc')
    body = data.get('body')
    subject = data.get('subject')
    return reply(
            user_email=current_user.email,
            oauth_token=refresh(current_user),
            to_email = original_from,
            subject=subject,
            body=body,
            reply=True,
            cc=cc,
            bcc=bcc,
            provider=current_user.provider,  
        )


@app.route('/send', methods=["POST"])
@login_required
def send():
    data = request.get_json()
    to = data.get('to')
    subject = data.get('subject')
    body = data.get('body')
    cc = data.get('cc')
    bcc = data.get('bcc')
    return reply(
            user_email=current_user.email,
            oauth_token=refresh(current_user),
            to_email = to,
            subject=subject,
            body=body,
            reply=False,
            cc=cc,
            bcc=bcc,
            provider=current_user.provider,  
        )

@app.route("/termsandprivacy")
def terms_and_privacy():
    return render_template("termsandprivacy.html")

@app.route('/unsubs')
@login_required
def all_unsubs():
    if not current_user.subscribed:
        return render_template("subscribe.html")
    unsubs = Unsubscribe.query.filter_by(user=current_user.id).all()
    if not unsubs:
        return render_template("unsubs.html", unsubs=[], number = 0)
    else:
        return render_template('unsubs.html', unsubs=unsubs, number = len(unsubs))

@app.route('/summary')
@login_required
def summary():
    if not current_user.subscribed:
        return render_template("subscribe.html")
    
    access_token = refresh(current_user)
    
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
        new_emails = get_emails(current_user.provider, current_user.email, access_token, 
                             after_date=after_date, since_time=since_time)
        
        print(f"Found {len(new_emails)} new emails in refresh")
        
        # Process unsubscribe links for new emails
        process_unsubscribe_links(new_emails, current_user)
        
        # Add action items placeholder and merge with existing emails
        final_emails = []
        for email in new_emails:
            email["action_items"] = "Generating ..."
            email["calendar"] = False
            final_emails.append(email)
            
        emails = session.get("final_emails")
        for email in emails:
            if "meeting" in email["action_items"].lower() or "conference call" in email["action_items"].lower() or "calendar" in email["action_items"].lower():
                email["calendar"] = True
            else:
                email["calendar"] = False
            final_emails.append(email)
            
        session['final_emails'] = final_emails
        current_datetime = datetime.now(timezone.utc)
        session['last_load'] = current_datetime.isoformat()
        
    else:
        # Initial load - get emails from the last 24 hours
        current_datetime = datetime.now(timezone.utc)
        session['last_load'] = current_datetime.isoformat()

        # Get emails from exactly 24 hours ago (no parameters = use default 24h window)
        # This will fetch using an IMAP date filter 2 days back, then filter precisely in Python
        emails = get_emails(current_user.provider, current_user.email, access_token)
        
        # Reverse order for newest first
        emails = list(reversed(emails))
        
        # Process unsubscribe links
        process_unsubscribe_links(emails, current_user)
        
        # Sort by priority and add action items placeholder
        final_emails = []
        high_priority = [email for email in emails if email['from'] in current_user.high_priority] 
        
        for email in high_priority:
            email["action_items"] = "Generating ..."
            final_emails.append(email)
            
        for email in emails: 
            if email not in final_emails: 
                email["action_items"] = "Generating ..."
                final_emails.append(email)
                
        session["final_emails"] = final_emails
    
    # Get existing todo emails from database
    todos = Todo.query.filter_by(user=current_user.id, done=False).all()
    
    # Convert todos to email format for display
    todo_emails = []
    for todo in todos:
        # Find the corresponding email in final_emails
        corresponding_email = None
        for email in final_emails:
            if email.get("action_items") == todo.item:
                corresponding_email = email
                break
                
        if corresponding_email:
            # Use the full email data
            todo_email = corresponding_email.copy()
            todo_email["todo"] = todo.item
            todo_email["id"] = todo.id
            
            # Check if it's calendar-worthy
            if any(keyword in todo.item.lower() for keyword in ["meeting", "conference call", "calendar", "appointment", "call"]):
                todo_email["calendar"] = True
            else:
                todo_email["calendar"] = False
                
            todo_emails.append(todo_email)
        else:
            db.session.delete(todo)
    db.session.commit()
    
    # Check for pending action items (emails that need processing)
    pending_count = 0
    for email in final_emails:
        if email.get("action_items") == "Generating ..." or not email.get("action_items"):
            pending_count += 1
    
    # Set appropriate message
    if pending_count > 0:
        text = f"Processing {pending_count} emails for action items..."
    elif not todo_emails:
        text = "No action items found from recent emails."
    else:
        text = f"Found {len(todo_emails)} action items"
    
    return render_template('summary.html', emails=todo_emails, text=text, pending_count=pending_count)


@app.route('/generate_pending_actions', methods=['POST'])
@login_required
def generate_pending_actions():
    """Generate action items for emails that don't have them yet"""
    final_emails = session.get("final_emails", [])
    if not final_emails:
        return jsonify({
            "success": False,
            "message": "No emails found to process"
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
                existing_todo = Todo.query.filter_by(user=current_user.id, item=action, done=False).first()
                
                if not existing_todo:
                    # Save to database
                    new_todo = Todo(user=current_user.id, item=action, done=False)
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


@app.route('/get_summary_status', methods=['GET'])
@login_required
def get_summary_status():
    """Get current status of action item generation"""
    final_emails = session.get("final_emails", [])
    
    pending_count = 0
    for email in final_emails:
        if email.get("action_items") == "Generating ..." or not email.get("action_items"):
            pending_count += 1
    
    return jsonify({
        "pending_count": pending_count,
        "total_emails": len(final_emails)
    })


from functions.cleaner import (
    get_email_service_type,
    fetch_emails_batch_unified,
    delete_all_senders_from_service,
    update_senders_cache_remove,
    update_senders_cache_restore
)

def get_redis_client():
    if PRODUCTION:
        return Redis(
            host='fly-mailmind-redis.upstash.io',
            port=6379,
            password=os.getenv("REDIS_PASSWORD")
        )
    else:
        return Redis(host="localhost", port=6379)

# Get the Redis client for progress tracking
r = get_redis_client()

@app.route('/email_cleaner', methods=["GET", "POST"])
@login_required
def email_cleaner():
    if not current_user.subscribed:
        return render_template("subscribe.html")
    
    service_type = get_email_service_type()
    if not service_type:
        return redirect(url_for('login'))
    
    # Initialize session variables if they don't exist
    if "senders_cache" not in session:
        session["senders_cache"] = []
    
    if "next_page_token" not in session:
        session["next_page_token"] = None
    
    if "processed_count" not in session:
        session["processed_count"] = 0
    
    # Handle AJAX request to load more emails
    if request.method == "POST" and request.headers.get("X-Requested-With") == "XMLHttpRequest":
        yes = request.json.get("all")
        print(yes)
        if yes == "yes":
            batch_size = 0
        else:
            batch_size = 1000 
        print(batch_size)

        def update_progress(data):
            progress_key = f"email_progress_{current_user.id}"
            r.setex(progress_key, 3600, json.dumps(data))
        
        senders, next_page_token, processed_count = fetch_emails_batch_unified(
            service_type=service_type,
            progress_callback=update_progress,
            batch_size=batch_size,
            page_token=session.get("next_page_token"),
            current_count=session.get("processed_count", 0)
        )
        
        # Update session data
        session["next_page_token"] = next_page_token
        session["processed_count"] = processed_count
        
        current_senders = {s["sender"]: s["number"] for s in session.get("senders_cache", [])}
        
        # Merge new senders with existing ones
        for sender, count in senders:
            if sender in current_senders:
                current_senders[sender] += count
            else:
                current_senders[sender] = count
        
        final = [{"sender": sender, "number": count} for sender, count in 
                sorted(current_senders.items(), key=lambda x: x[1], reverse=True)]
        
        session["senders_cache"] = copy.deepcopy(final)
        
        return jsonify({
            "text": final,
            "processed_count": processed_count,
            "has_more": next_page_token is not None,
            "deleted": session.get("deleted", [])
        })
    
    # Initial page load
    return render_template(
        'upload.html', 
        text=session.get("senders_cache", []), 
        processed_count=session.get("processed_count", 0),
        has_more=session.get("next_page_token") is not None,
        to_delete=session.get("deleted", []),
        service_type=service_type
    )

@app.route('/email_progress')
@login_required
def get_email_progress():
    progress_key = f"email_progress_{current_user.id}"
    data = r.get(progress_key)
    if data:
        return jsonify(json.loads(data))
    return jsonify({"count": 0, "status": "idle"})

@app.route('/delete_count')
@login_required
def delete_count():
    progress_key = f"delete_count{current_user.id}"
    data = r.get(progress_key)
    print("delete count")
    if data:
        return jsonify(json.loads(data))
    return jsonify({"status": "error"})


@app.route('/remove_all_senders', methods=["POST"])
def remove_all_senders():
    """Remove all emails from senders in the deleted list - works with both Gmail and Outlook"""
    senders_to_delete = session.get('deleted', [])
    if not senders_to_delete:
        return jsonify({"message": "No senders specified for deletion"}), 400
    
    def update_delete_count(data):
        print("updating")
        progress_key = f"delete_count{current_user.id}"
        r.setex(progress_key, 3600, json.dumps(data))
    
    deleted_count, failed_senders = delete_all_senders_from_service(senders_to_delete, update_delete_count)
    
    # Only remove successfully processed senders from the deleted list
    if failed_senders:
        session["deleted"] = failed_senders
    else:
        session["deleted"] = []
    
    result = {
        "message": f"Successfully deleted {deleted_count} emails from {len(senders_to_delete) - len(failed_senders)} senders",
    }
    
    if failed_senders:
        result["warning"] = f"Failed to process {len(failed_senders)} senders"
        result["failed_senders"] = failed_senders
    
    return jsonify(result)

@app.route("/remove_unsubscribe", methods=["POST"])
def remove_unsubscribe():
    sender = request.json.get("sender")
    manual = request.json.get("manual")
    print(manual, type(manual))
    print(sender)
    unsubscribe = Unsubscribe.query.filter_by(sender=sender, user=current_user.id).first()
    if unsubscribe:
        link = unsubscribe.link
        if manual == "false":
            auto = automated_unsubscribe(link, current_user.email)
            print(auto["success"])
            if auto["success"]:
                db.session.delete(unsubscribe)
                db.session.commit()
                return jsonify({
                    "status": "success",
                    "message": f"Removed {sender}"
                }), 200
            else:
                return jsonify({
                    "status": "failure",
                    "message": f"Auto unsubscribe failed for {sender}, try manually doing it. Link could also be broken.",
                })
        else:
            db.session.delete(unsubscribe)
            db.session.commit()
            return jsonify({
                    "status": "success",
                    "message": f"Removed {sender}"
                }), 200
        
    else:
        return jsonify({"message": "No unsubscribe entry found.", "status": "failure"}), 404

@app.route('/delete_sender', methods=["POST"])
def delete_sender():
    """Remove a sender from the tracked list without re-fetching emails from service."""
    sender_name = request.json.get("sender_name")
    if not sender_name:
        return jsonify({"error": "No sender specified"}), 400

    result = update_senders_cache_remove(sender_name)
    return jsonify(result)

@app.route('/restore_sender', methods=["POST"])
def restore_sender():
    """Restore a sender back to the tracked list if it was previously removed."""
    sender_name = request.json.get("sender_name")
    if not sender_name:
        return jsonify({"error": "No sender specified"}), 400

    result = update_senders_cache_restore(sender_name)
    
    if "error" in result:
        return jsonify(result), 400
    
    return jsonify(result)


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
                email=current_user.email,
                name=current_user.name if hasattr(current_user, 'name') else None,
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
        return User.query.filter_by(stripe_customer_id=customer_id).first()

    if event_type == 'checkout.session.completed':
        print('🔔 Payment succeeded!')
        customer_id = data_object.get('customer')
        user = find_user_by_customer_id(customer_id)
        if user:
            user.subscribed = True
            db.session.commit()
            print(f"User {user.email} subscription activated")

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
    
@app.route('/mail')
@login_required
def send_email_summary():
    """Send email summary of last 24 hours action items"""
    try:
        # Get user's access token
        access_token = refresh(current_user)
        
        # Get emails from last 24 hours
        emails = get_emails(current_user.provider, current_user.email, access_token)
        
        if not emails:
            return jsonify({
                "success": False,
                "message": "No emails found in the last 24 hours"
            })
        
        # Process emails for action items
        emails = list(reversed(emails))  # Newest first
        process_unsubscribe_links(emails, current_user)
        
        # Generate action items for all emails
        action_items = []
        for email in emails:
            try:
                body = email.get("body", "")
                if body:
                    action = get_an_action(body)
                    if action and action.lower() not in ["no action.", "no action", "no action required.", ""]:
                        email["action_items"] = action
                        email["calendar"] = is_calendar_worthy(action)
                        action_items.append(email)
                    else:
                        email["action_items"] = "No action"
                        email["calendar"] = False
                else:
                    email["action_items"] = "No action"
                    email["calendar"] = False
            except Exception as e:
                logger.error(f"Error generating action for email: {e}")
                email["action_items"] = "Error generating action"
                email["calendar"] = False
        
        # Send email summary
        if action_items:
            email_sent = send_reply_email(action_items, current_user.email)
            if email_sent:
                return jsonify({
                    "success": True,
                    "message": f"Email summary sent with {len(action_items)} action items",
                    "action_count": len(action_items)
                })
            else:
                return jsonify({
                    "success": False,
                    "message": "Failed to send email summary"
                })
        else:
            return jsonify({
                "success": True,
                "message": "No action items found to send",
                "action_count": 0
            })
            
    except Exception as e:
        logger.error(f"Error in send_email_summary: {str(e)}")
        return jsonify({
            "success": False,
            "message": "An error occurred while processing email summary"
        })

def is_calendar_worthy(action_text):
    """Check if action item is calendar-worthy"""
    calendar_keywords = ["meeting", "conference call", "calendar", "appointment", "call", "schedule", "event"]
    return any(keyword in action_text.lower() for keyword in calendar_keywords)

def send_reply_email(action_items, user_email):
    """Send email using the reply function"""
    try:
        # Generate HTML email content
        html_content = generate_email_html(action_items, user_email)
        
        # Prepare email data
        subject = f"Daily To Do List from MailMind for {datetime.now().strftime('%B %d, %Y')}"
        
        # Send email via reply function as HTML
        result = reply(
            user_email=current_user.email,
            oauth_token=refresh(current_user),
            to_email=current_user.email,
            subject=subject,
            body=html_content,
            reply=False,
            cc=None,
            bcc=None,
            provider=current_user.provider,  # Pass the provider from user object
            content_type='html'
        )
        
        if result:  # Assuming reply function returns True on success
            logger.info(f"Email sent successfully to {current_user.email}")
            return True
        else:
            logger.error(f"Failed to send email using reply function")
            return False
            
    except Exception as e:
        logger.error(f"Error sending email via reply function: {str(e)}")
        return False

def generate_email_html(action_items, user_email):
    """Generate rich HTML email content"""
    
    # HTML email template matching your summary page style
    html_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
    <meta charset="UTF-8" />
    <title>MailMind Daily Summary</title>
    <style>
        body {
        background: #171524;
        color: #f0f0f0;
        font-family: Inter, system-ui, sans-serif;
        margin: 0;
        padding: 20px;
        }
        .container {
        max-width: 600px;
        margin: 0 auto;
        background: linear-gradient(145deg, #514b7a, #5a5488);
        border: 2px solid black;
        border-radius: 20px;
        padding: 30px;
        box-shadow: 4px 4px 10px rgba(0, 0, 0, 0.4);
        }
        h1 {
        text-align: center;
        font-size: 24px;
        color: #e6d7a3;
        margin-bottom: 10px;
        }
        .top-link {
        text-align: center;
        margin-bottom: 20px;
        }
        .top-link a {
        color: #cc8400;
        font-weight: bold;
        text-decoration: none;
        font-size: 14px;
        }
        .summary-info {
        text-align: center;
        font-size: 14px;
        margin-bottom: 30px;
        color: #f0f0f0;
        }
        .item {
        background: #3a3560;
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-left: 4px solid #cc8400;
        border-radius: 12px;
        padding: 15px 20px;
        margin-bottom: 20px;
        }
        .item p {
        margin: 4px 0;
        font-size: 14px;
        }
        .item .action {
        color: #e6d7a3;
        font-weight: 600;
        }
        .footer {
        text-align: center;
        font-size: 12px;
        color: #ccc;
        margin-top: 30px;
        }
        .footer a {
        color: #e6d7a3;
        text-decoration: none;
        }
        .footer a:hover {
        color: #cc8400;
        }
    </style>
    </head>
    <body>
    <div class="container">
        <h1>📬 Your Daily Action Summary</h1>

        <div class="top-link">
        <a href="https://mailmind.fly.dev/summary">View Full List →</a>
        </div>

        <div class="summary-info">
        <p>{{ action_count }} action items • {{ current_date }}</p>
        <p>{{ user_email }}</p>
        </div>

        {% for email in action_items%}
        <div class="item">
        <p class="action">• {{ email.action_items | linkify_text }}</p>
        <p><strong>From:</strong> {{ email.from }}</p>
        <p><strong>Subject:</strong> {{ email.subject }}</p>
        </div>
        {% endfor %}

        <div class="footer">
        <p>
            Sent by <a href="https://mailmind.fly.dev">MailMind</a> • 
            <a href="mailto:pautomas55@gmail.com">Support</a> • 
        </p>
        </div>
    </div>
    </body>
    </html>

    """
    
    # Render the template with data
    from jinja2 import Template, Environment
    
    # Create Jinja2 environment with custom filter
    env = Environment()
    
    # Register the linkify_text filter (assuming it's available in your app context)
    # If you need to import it, add: from your_module import linkify_text
    env.filters['linkify_text'] = linkify_text
    
    template = env.from_string(html_template)
    
    return template.render(
        action_items=action_items,
        user_email=user_email,
        current_date=datetime.now().strftime('%B %d, %Y'),
        action_count=len(action_items)
    )

# Alternative route for testing email template
@app.route('/test_email_template')
@login_required
def test_email_template():
    """Test route to preview email template in browser"""
    try:
        # Get sample data
        access_token = refresh(current_user)
        emails = get_emails(current_user.provider, current_user.email, access_token)
        
        if not emails:
            sample_data = [{
                "from": "example@company.com",
                "subject": "Project Update Required",
                "body": "Hi there, we need to discuss the project timeline and deliverables. Please review the attached documents and let me know your thoughts by end of week. Visit https://example.com for more details.",
                "action_items": "Review project documents and provide feedback by end of week. Check https://example.com for updates",
                "calendar": True
            }]
        else:
            # Process first few emails as sample
            sample_data = []
            for email in emails:  # Take first 3 emails
                body = email.get("body", "")
                if body:
                    action = get_an_action(body)
                    if action and action.lower() not in ["no action.", "no action", "no action required.", ""]:
                        email["action_items"] = action
                        email["calendar"] = is_calendar_worthy(action)
                        sample_data.append(email)
        
        # Generate HTML and return it directly for preview
        html_content = generate_email_html(sample_data, current_user.email)
        return html_content
        
    except Exception as e:
        logger.error(f"Error in test_email_template: {str(e)}")
        return f"<h1>Error</h1><p>{str(e)}</p>"

# Route to manually trigger email sending (for testing)
@app.route('/send_test_email')
@login_required
def send_test_email():
    """Manual trigger for testing email sending"""
    try:
        # Create sample data
        sample_data = [{
            "from": "test@example.com",
            "subject": "Test Email Action Item",
            "body": "This is a test email to verify the mailing functionality works correctly. Visit https://example.com for more info.",
            "action_items": "Test the email mailing system and verify delivery. Check https://example.com",
            "calendar": False
        }]
        
        email_sent = send_reply_email(sample_data, current_user.email)
        
        if email_sent:
            return jsonify({
                "success": True,
                "message": f"Test email sent successfully to {current_user.email}"
            })
        else:
            return jsonify({
                "success": False,
                "message": "Failed to send test email"
            })
            
    except Exception as e:
        logger.error(f"Error in send_test_email: {str(e)}")
        return jsonify({
            "success": False,
            "message": str(e)
        })
    
@app.route("/beta")
def beta():
    return render_template("beta.html")