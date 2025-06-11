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
from functions.get_emails import get_emails
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
import re
import short_url
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
                 'openid']
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
    current_user.subscribed = False
    db.session.commit()
    return render_template("index.html")

@app.route("/code", methods=["GET", "POST"])
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
        user = create_user(session['user_email'], encrypt_token(credentials.refresh_token))
    else:
        user.oauth_token = encrypt_token(credentials.refresh_token)
    db.session.commit()
    login_user(user, remember = True)
    return redirect(url_for("index"))

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
        new_emails = get_emails("gmail", current_user.email, access_token, 
                             after_date=after_date, since_time=since_time)
        
        print(f"Found {len(new_emails)} new emails in refresh")
        
        # Process unsubscribe links for new emails
        process_unsubscribe_links(new_emails, current_user)
        
        # Add action items placeholder and merge with existing emails
        final_emails = []
        for email in new_emails:
            email["action_items"] = "Generating ..."
            final_emails.append(email)
            
        emails = session.get("final_emails")
        for email in emails:
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
    emails = get_emails("gmail", current_user.email, access_token)
    
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
    action = get_an_action(body)
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
    return jsonify({"action_item": action})

@app.route("/remove_todo", methods=["POST"])
@login_required
def remove_todo():
    data = request.get_json()
    todo_id = data.get("id")  
    
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
    print(body)
    return reply(user_email=current_user.email,
    oauth_token=refresh(current_user),
    to_email=original_from,
    subject=subject,
    body=body,
    reply=True,
    cc=cc, 
    bcc=bcc,
    smtp_server="smtp.gmail.com",
    smtp_port=587)

@app.route('/send', methods=["POST"])
@login_required
def send():
    data = request.get_json()
    to = data.get('to')
    subject = data.get('subject')
    body = data.get('body')
    cc = data.get('cc')
    bcc = data.get('bcc')
    return reply(user_email=current_user.email,
    oauth_token=refresh(current_user),
    to_email=to,
    subject=subject,
    body=body,
    reply=False,
    cc=cc, 
    bcc=bcc,
    smtp_server="smtp.gmail.com",
    smtp_port=587)

@app.route('/unsubs')
@login_required
def all_unsubs():
    if not current_user.subscribed:
        return render_template("subscribe.html")
    unsubs = Unsubscribe.query.filter_by(user=current_user.id).all()
    if not unsubs:
        return render_template('index.html')
    else:
        return render_template('unsubs.html', unsubs=unsubs)

@app.route('/summary') 
@login_required     
def summary():
    if not current_user.subscribed:
        return render_template("subscribe.html")
    emails = []
    text = ""
    final_emails = session.get("final_emails", False)
    if final_emails:
        for email in final_emails: 
            if email["action_items"] == "Generating ...":
                print('text change')
                text = "More action items to generate on inbox page"
    todo_emails = session.get("todo_emails", False)
    if todo_emails:
        emails = todo_emails
    else:
        text = "More action items to generate on inbox page"
    return render_template('summary.html', emails=emails, text = text)

@app.route('/email_cleaner', methods=["GET", "POST"])
@login_required
def email_cleaner():
    if not current_user.subscribed:
        return render_template("subscribe.html")
    """Process emails directly from Gmail"""
    if 'google_credentials' not in session:
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
        senders, next_page_token, processed_count = fetch_gmail_emails_batch(
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
        to_delete=session.get("deleted", [])
    )

# Add this global variable to track progress
current_progress = {"count": 0, "status": "idle"}

# Add this new route to get progress updates
@app.route('/email_progress')
@login_required
def get_email_progress():
    return jsonify(current_progress)

# Modify your existing fetch_gmail_emails_batch function
def fetch_gmail_emails_batch(batch_size=1000, page_token=None, current_count=0):
    """Fetch a batch of Gmail emails and process senders"""
    global current_progress
    
    creds = Credentials.from_authorized_user_info(session["google_credentials"])
    service = build("gmail", "v1", credentials=creds)
    
    senders = {}
    processed_count = current_count
    next_page_token = page_token
    process_all = batch_size == 0
    remaining_emails = batch_size if not process_all else float('inf')
    pending_unsubscribes = []
    
    # Initialize progress
    current_progress = {"count": processed_count, "status": "loading"}
    
    try:
        while remaining_emails > 0:
            request_size = min(500, remaining_emails) if not process_all else 500
            
            results = service.users().messages().list(
                userId='me',
                maxResults=request_size,
                pageToken=next_page_token
            ).execute()
            
            messages = results.get('messages', [])
            if not messages:
                next_page_token = None
                break
            
            next_page_token = results.get('nextPageToken')
            
            for message in messages:
                try:
                    msg = service.users().messages().get(userId='me', id=message['id']).execute()
                    headers = {header['name'].lower(): header['value'] for header in msg['payload']['headers']}
                    sender = headers.get('from', '')
                    
                    if sender:
                        if sender not in senders:
                            unsubscribe_link = headers.get('list-unsubscribe', '').strip('<>')
                            
                            if not unsubscribe_link:
                                email_body = get_message_body(msg)
                                unsubscribe_link = find_unsubscribe_link(email_body)
                            
                            if unsubscribe_link:
                                pending_unsubscribes.append({
                                    'link': unsubscribe_link,
                                    'sender': sender
                                })
                        
                        senders[sender] = senders.get(sender, 0) + 1
                    
                    processed_count += 1
                    remaining_emails -= 1
                    
                    if processed_count % 100 == 0:
                        print(f"Processed {processed_count} emails")
                        # Update global progress
                        current_progress = {"count": processed_count, "status": "loading"}
                    
                    if remaining_emails <= 0 and not process_all:
                        break
                        
                except Exception as e:
                    print(f"Error processing message {message['id']}: {str(e)}")
                    continue
            
            if not next_page_token or (remaining_emails <= 0 and not process_all):
                break
        
        if pending_unsubscribes:
            process_unsubscribe_links_batch(pending_unsubscribes)
        
        # Mark as complete
        current_progress = {"count": processed_count, "status": "complete"}
                
    except Exception as e:
        print(f"Error fetching emails: {str(e)}")
        current_progress = {"count": processed_count, "status": "error"}
    
    sorted_senders = sorted(senders.items(), key=lambda x: x[1], reverse=True)
    return sorted_senders, next_page_token, processed_count

def process_unsubscribe_links_batch(unsubscribe_data):
    """Process and store multiple unsubscribe links in a single database transaction."""
    if not unsubscribe_data:
        return
    
    changes_made = False
    
    try:
        for data in unsubscribe_data:
            link = data['link']
            sender = data['sender']
            
            if not link:
                continue
            
            try:
                # Handle encoded links
                if link.startswith('[LINK:') and link.endswith(']'):
                    code = link.strip('[LINK:').strip(']').strip()
                    try:
                        link_id = short_url.decode_url(code)
                        link_obj = Link.query.filter_by(id=link_id).first()
                        if link_obj:
                            link = link_obj.link
                        else:
                            continue
                    except Exception as e:
                        print(f"Error decoding short URL: {str(e)}")
                        continue

                # Check if unsubscribe entry already exists
                unsubj = Unsubscribe.query.filter_by(sender=sender).first()
                if not unsubj:
                    new_unsub = Unsubscribe(sender=sender, link=link, user=current_user.id)
                    db.session.add(new_unsub)
                    print(f"Added unsubscribe link for {sender}: {link}")
                    changes_made = True
                    
            except Exception as e:
                print(f"Error processing unsubscribe link for {sender}: {str(e)}")
                continue
        
        # Single commit for all changes
        if changes_made:
            db.session.commit()
            print(f"Successfully committed {len([d for d in unsubscribe_data if d['link']])} unsubscribe link changes")
            
    except Exception as e:
        print(f"Error committing unsubscribe link changes: {str(e)}")
        db.session.rollback()


def get_message_body(msg):
    """Extract the email body from a Gmail message."""
    if 'payload' not in msg:
        return ""
    
    parts = msg['payload'].get('parts', [])
    
    if not parts and 'body' in msg['payload'] and 'data' in msg['payload']['body']:
        data = msg['payload']['body']['data']
        return base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')

    return extract_body_from_parts(parts)


def extract_body_from_parts(parts):
    """Extract the email body from message parts recursively."""
    body = ""
    html_content = ""
    plain_content = ""
    
    for part in parts:
        mime_type = part.get('mimeType', '')
        
        if mime_type == 'text/plain' and 'data' in part.get('body', {}):
            data = part['body']['data']
            plain_content += base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
        
        elif mime_type == 'text/html' and 'data' in part.get('body', {}):
            data = part['body']['data']
            html_content += base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
        
        elif 'parts' in part:
            part_content = extract_body_from_parts(part['parts'])
            body += part_content

    if html_content:
        return html_content
    elif plain_content:
        return plain_content
    
    return body


def find_unsubscribe_link(email_body):
    """Find an unsubscribe link in the email body."""
    if not email_body:
        return ''
    
    unsubscribe_match = re.search(r"(?i)unsubscribe", email_body)
    
    if unsubscribe_match:
        unsubscribe_pos = unsubscribe_match.start()
        
        remaining_text = email_body[unsubscribe_pos:]
        
        link_match = re.search(r"\[LINK:\s*([^\]]+)\]", remaining_text)
        
        if link_match:
            return link_match.group(1)
            
        html_link_match = re.search(r'href=["\'](https?://[^"\'>]+)["\']', remaining_text)
        if html_link_match:
            return html_link_match.group(1)
    
    return ''


@app.route('/delete_sender', methods=["POST"])
def delete_sender():
    """Remove a sender from the tracked list without re-fetching emails from Gmail."""
    sender_name = request.json.get("sender_name")
    if not sender_name:
        return jsonify({"error": "No sender specified"}), 400

    deleted_senders = session.get("deleted", [])
    removed_senders_cache = session.get("removed_senders_cache", [])

    if sender_name not in deleted_senders:
        deleted_senders.append(sender_name)
        session["deleted"] = deleted_senders

    if "senders_cache" in session:
        updated_cache = []
        for entry in session["senders_cache"]:
            if entry.get("sender") == sender_name:
                # Save removed sender to removed_senders_cache if not already there
                if not any(d.get("sender") == sender_name for d in removed_senders_cache):
                    removed_senders_cache.append(entry)
            else:
                updated_cache.append(entry)

        session["senders_cache"] = updated_cache
        session["removed_senders_cache"] = removed_senders_cache
    print("Length:", len(session["senders_cache"]))

    session.modified = True
    return jsonify({
        "message": f"Removed {sender_name} from the list.",
        "text": session.get("senders_cache", []),
        "deleted": deleted_senders,
    })



@app.route('/restore_sender', methods=["POST"])
def restore_sender():
    """Restore a sender back to the tracked list if it was previously removed."""
    sender_name = request.json.get("sender_name")
    if not sender_name:
        return jsonify({"error": "No sender specified"}), 400

    deleted_senders = session.get("deleted", [])
    removed_senders_cache = session.get("removed_senders_cache", [])
    senders_cache = session.get("senders_cache", [])

    if sender_name not in deleted_senders:
        return jsonify({"error": f"{sender_name} was not previously deleted"}), 400

    # Remove from deleted list
    deleted_senders.remove(sender_name)
    session["deleted"] = deleted_senders

    # Find the removed sender entry
    restored_entry = None
    updated_removed_cache = []
    for entry in removed_senders_cache:
        if entry.get("sender") == sender_name and not restored_entry:
            restored_entry = entry
        else:
            updated_removed_cache.append(entry)



    # Restore the sender in the original position to preserve order
    if restored_entry:
        # Insert sender back where it was originally:
        # If you want strict original index, you'd need to store it in removed_senders_cache.
        # Here, we'll append and then sort by count descending to mimic original order
        senders_cache.append(restored_entry)
        senders_cache.sort(key=lambda x: x.get("count", 0), reverse=True)

    session["senders_cache"] = senders_cache
    session["removed_senders_cache"] = updated_removed_cache
    session.modified = True

    print(deleted_senders)
    return jsonify({
        "message": f"Restored {sender_name} to the list.",
        "text": session["senders_cache"],
        "deleted": deleted_senders,
    })



@app.route('/remove_all_senders', methods=["POST"])
def remove_all_senders():
    """Remove all emails from senders in the deleted list"""
    if 'google_credentials' not in session:
        return jsonify({"error": "Not authenticated with Gmail"}), 401
    
    creds = Credentials.from_authorized_user_info(session["google_credentials"])
    service = build("gmail", "v1", credentials=creds)
    
    senders_to_delete = session.get('deleted', [])
    if not senders_to_delete:
        return jsonify({"message": "No senders specified for deletion"}), 400
    
    print(f"Removing emails from these senders: {senders_to_delete}")
    
    deleted_count = 0
    failed_senders = []
    
    for sender in senders_to_delete:
        try:
            query = f"from:{sender}"
            page_token = None
            sender_deleted_count = 0
            
            while True:
                response = service.users().messages().list(
                    userId="me", 
                    q=query,
                    maxResults=500,
                    pageToken=page_token
                ).execute()
                
                messages = response.get("messages", [])
                if not messages:
                    break
                
                print(f"Found {len(messages)} emails from {sender}")
                
                for msg in messages:
                    try:
                        service.users().messages().trash(userId="me", id=msg["id"]).execute()
                        deleted_count += 1
                        sender_deleted_count += 1
                        
                        # Throttle requests to avoid rate limiting
                        if deleted_count % 50 == 0:
                            print(f"Deleted {deleted_count} emails so far...")
                            time.sleep(1)
                    except Exception as msg_error:
                        print(f"Error trashing message {msg['id']}: {str(msg_error)}")
                        # Continue with the next message even if one fails
                
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
            
            print(f"Deleted {sender_deleted_count} emails from {sender}")
            
        except Exception as e:
            print(f"Failed to process sender {sender}: {str(e)}")
            failed_senders.append(sender)
    
    # Only remove successfully processed senders from the deleted list
    if failed_senders:
        session["deleted"] = failed_senders
    else:
        session["deleted"] = []
    
    result = {
        "message": f"Successfully moved {deleted_count} emails from {len(senders_to_delete) - len(failed_senders)} senders to trash",
    }
    
    if failed_senders:
        result["warning"] = f"Failed to process {len(failed_senders)} senders"
        result["failed_senders"] = failed_senders
    
    return jsonify(result)

@app.route("/remove_unsubscribe", methods=["POST"])
def remove_unsubscribe():
    sender = request.json.get("sender")
    print(sender)
    unsubscribe = Unsubscribe.query.filter_by(sender=sender, user=current_user.id).first()
    if unsubscribe:
        db.session.delete(unsubscribe)
        db.session.commit()
        return jsonify({
            "status": "success",
            "message": f"Removed {sender}"
        }), 200
    else:
        return jsonify({"message": "No unsubscribe entry found."}), 404



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
