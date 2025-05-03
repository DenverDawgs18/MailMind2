
from flask import Flask, render_template, url_for, request, redirect, session, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from sqlalchemy.orm import DeclarativeBase
from datetime import datetime, timedelta, timezone, date
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
app = Flask(__name__, static_url_path='/static')
app.config.from_pyfile('config.py')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
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
app.config["SESSION_REDIS"] = Redis(host="localhost", port=6379)
Session(app)
from functions.get_emails import get_emails
from functions.old.process_emails import process_email_batch
import requests
import os
import json
import base64
from google_auth_oauthlib.flow import Flow
import markdown
from flask_login import login_required, LoginManager, login_user, logout_user, current_user
from models import User, Link, Unsubscribe
from functions.refresh_token import refresh
from functions.users import get_last_login, get_user, update_last_login, create_user
from functions.linkify import linkify_text
from functions.reply import reply
from functions.get_action_items import batch_get_action_items
from functions.get_one_action import get_an_action
import re
import short_url
import textwrap
from werkzeug.utils import secure_filename
import mailbox
from functions.get_emails import safe_decode_header, extract_email_content
import time
import markdown    
import copy
from sortedcontainers import SortedList
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "google_login"
with app.app_context():
    db.create_all()

GOOGLE_CLIENT_SECRETS = 'secret.json'
GOOGLE_SCOPES = ['https://mail.google.com/', 
                 'https://www.googleapis.com/auth/userinfo.email', 
                 'openid']
GOOGLE_REDIRECT_URI = "http://localhost:5000/google/callback"

@login_manager.user_loader 
def load_user(id):
    return User.query.get(int(id))


@app.template_filter("markdown")
def markdown_filter(text):
    return markdown.markdown(text, output_format="html", extensions=["extra"])


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/')
def index():
    return render_template('index.html')

@app.route("/google/login")
def google_login():
    flow = Flow.from_client_secrets_file(
        GOOGLE_CLIENT_SECRETS, 
        scopes=GOOGLE_SCOPES,
        redirect_uri = GOOGLE_REDIRECT_URI
    )
    auth_url, _ = flow.authorization_url(prompt="consent")
    
    return redirect(auth_url)

@app.route('/google/callback')
def google_callback():
    flow = Flow.from_client_secrets_file(
        GOOGLE_CLIENT_SECRETS, 
        scopes=GOOGLE_SCOPES,
        redirect_uri = GOOGLE_REDIRECT_URI,
    )
    authorization_response = request.url.replace('http', 'https')
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
        user = create_user(session['user_email'], credentials.refresh_token)
    else:
        user.last_login = datetime.now(timezone.utc)
    login_user(user, remember = True)
    return redirect(url_for("index"))
@app.template_filter('markdown')
def markdown_filter(text):
    return markdown.markdown(text)
app.jinja_env.filters['markdown'] = markdown_filter
app.jinja_env.filters['linkify_text'] = linkify_text

@app.route('/emails')
@login_required
def emails():
    access_token = refresh(current_user)
    if session.get('final_emails', False):
        print('refresh')
        today = date.today()
        last_load = session.get('last_load', datetime.now(timezone.utc))
        emails = session['final_emails']
        after_date = last_load.strftime("%m-%d-%y")  
        since_time = last_load.strftime("%H:%M:%S")  
        new_emails = get_emails("gmail", current_user.email, access_token, after_date=after_date, 
                            since_time=since_time)
        for email in new_emails:
            pattern = r"(?i)Unsubscribe[^a-z0-9\s]?\s*\[LINK:\s*([^\]]+)\]"
            match = re.search(pattern, email['body'])
            
            if match:
                code = match.group(1)
                link_id = short_url.decode_url(code)
                # Look up the corresponding Link object.
                link_obj = Link.query.filter_by(id=link_id).first()
                if link_obj:
                    real_link = link_obj.link
                    unsubj = Unsubscribe.query.filter_by(link=real_link).first()
                    if not unsubj:
                        new_unsub = Unsubscribe(sender=email['from'], user=current_user.id)
                        db.session.add(new_unsub)
                        print(new_unsub)
                        db.session.commit()
                else:
                    break
        final_emails = []
        for email in emails:
            final_emails.append(email)

        for email in new_emails:
            if email not in final_emails:
                email["action_item"] = "Generating ..."
                final_emails.append(email)
                
                    
        session['final_emails'] = final_emails 
        session['last_load'] = datetime.now(timezone.utc)
        return render_template('emails.html', emails=final_emails)

    # First-time loading emails
    current_datetime = datetime.now(timezone.utc)
    current_time_formatted = current_datetime.strftime("%H:%M:%S")
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    session['since'] = yesterday
    yesterday = yesterday.strftime("%m-%d-%y")
    session['last_load'] = current_datetime
        
    # Fetch emails from Gmail
    emails = get_emails("gmail", current_user.email, access_token, after_date=yesterday, 
                        since_time=current_time_formatted, old=None)
    
    emails = list(reversed(emails))
    for email in emails:
        pattern = r"(?i)Unsubscribe[^a-z0-9\s]?\s*\[LINK:\s*([^\]]+)\]"
        match = re.search(pattern, email['body'])
        
        if match:
            code = match.group(1)
            link_id = short_url.decode_url(code)
            # Look up the corresponding Link object.
            link_obj = Link.query.filter_by(id=link_id).first()
            if link_obj:
                real_link = link_obj.link
                unsubj = Unsubscribe.query.filter_by(link=real_link).first()
                if not unsubj:
                    new_unsub = Unsubscribe(sender=email['from'], user=current_user.id)
                    db.session.add(new_unsub)
                    print(new_unsub)
                    db.session.commit()
            else:
                continue
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
            
    return render_template('emails.html', emails=emails)

@app.route("/get_one_action", methods=["POST"])
@login_required 
def get_one_action():
    data = request.get_json()
    body = data.get("body")
    index = int(data.get("index"))
    print(index)
    action = get_an_action(body)
    final_emails = session.get("final_emails")
    final_emails[index]["action_items"] = action
    return jsonify({"action_item": action})


    


@app.route('/load_more', methods=["POST"])
@login_required
def load_more():
    print('loading more')
    final_emails = session.get('final_emails', [])
 
    final_final = final_emails.copy()  # Copy to avoid modifying session mid-loop
    access_token = refresh(current_user)
    prev = session['since']
    prev = prev.strftime("%m-%d-%y")
    day = session['since'] - timedelta(days=1)
    session['since'] = day
    day = day.strftime("%m-%d-%y")
    new_emails = get_emails("gmail", current_user.email, access_token, after_date=day, since_time=session['time'], before_date=prev, old=final_emails)
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
    unsubs = Unsubscribe.query.filter_by(user=current_user.id).all()
    if not unsubs:
        return render_template('index.html')
    else:
        return render_template('unsubs.html', unsubs=unsubs)
    
@app.route('/analyze', methods=["POST"])
@login_required
def analyze():
    print('analyzing')
    access_token = refresh(current_user)
    emails = get_emails("gmail", current_user.email, access_token, after_date='2-3-25', 
                        since_time="00:00:00", old=None)
    
    emails = list(reversed(emails))
    unsubs = Unsubscribe.query.filter_by(user=current_user.id).all()
    all_unsubs = []
    if unsubs:
        for unsub in unsubs:
            all_unsubs.append(unsub)
    for email in emails:
        pattern = r"(?i)Unsubscribe[^a-z0-9\s]?\s*\[LINK:\s*([^\]]+)\]"
        match = re.search(pattern, email['body'])
        
        if match:
            code = match.group(1)
            link_id = short_url.decode_url(code)
            link_obj = Link.query.filter_by(id=link_id).first()
            if link_obj:
                real_link = link_obj.link
                unsubj = Unsubscribe.query.filter_by(sender=email['from']).first()
                if not unsubj:
                    new_unsub = Unsubscribe(sender=email['from'], user=current_user.id)
                    db.session.add(new_unsub)
                    print(new_unsub)
                    db.session.commit()
                    all_unsubs.append(new_unsub)
            else:
                continue
    


    return render_template('unsubs.html', unsubs=unsubs)

@app.route('/summary') 
@login_required     
def summary():
    access_token = refresh(current_user)
    if session.get('final_emails', False):
        print("using cached emails")
        last_load = session.get('last_load', datetime.now(timezone.utc))
        emails = session['final_emails']
        after_date = last_load.strftime("%m-%d-%y")  
        since_time = last_load.strftime("%H:%M:%S")  
        new_emails = get_emails("gmail", current_user.email, access_token, after_date=after_date, 
                since_time=since_time)
        
        batch_action_items = batch_get_action_items(new_emails)
        
        for i, email in enumerate(new_emails):
            if i < len(batch_action_items):
                email['action_items'] = batch_action_items[i]
        
        # Filter emails from the last day
        one_day = datetime.now(timezone.utc) - timedelta(days=1)
        recent_emails = []
        for email in emails:
            email_date = datetime.fromisoformat(email['utc'])
            if email_date.tzinfo is None:
                email_date = email_date.replace(tzinfo=timezone.utc)
            if email_date >= one_day:
                recent_emails.append(email)
        
        # Combine recent emails with new emails
        final_emails = recent_emails + new_emails
        session['final_emails'] = final_emails
        session['last_load'] = datetime.now(timezone.utc)
        
        all_items = []

        for email in final_emails:
            all_items.append(email["action_items"])
        
        return render_template('summary.html', summary = all_items)
    else:
        current_datetime = datetime.now()
        current_time_formatted = current_datetime.strftime("%H:%M:%S")
        today = date.today()
        yesterday = today - timedelta(days=1)
        yesterday = yesterday.strftime("%m-%d-%y")
        emails = get_emails("gmail", current_user.email, access_token, after_date=yesterday, 
                            since_time=current_time_formatted, old=None)
        emails = list(reversed(emails))
        
        # Process unsubscribe links
        for email in emails:
            pattern = r"(?i)Unsubscribe[^a-z0-9\s]?\s*\[LINK:\s*([^\]]+)\]"
            match = re.search(pattern, email['body'])
            
            if match:
                code = match.group(1)
                link_id = short_url.decode_url(code)
                # Look up the corresponding Link object.
                link_obj = Link.query.filter_by(id=link_id).first()
                if link_obj:
                    real_link = link_obj.link
                    unsubj = Unsubscribe.query.filter_by(link=real_link).first()
                    if not unsubj:
                        new_unsub = Unsubscribe(sender=email['from'], user=current_user.id)
                        db.session.add(new_unsub)
                        print(new_unsub)
                        db.session.commit()
                else:
                    break
        
        # Separate high priority emails
        high_priority_batch = [email for email in emails if email['from'] in current_user.high_priority]
        regular_emails = [email for email in emails if email['from'] not in current_user.high_priority]
        
        if high_priority_batch:
            high_priority_action_items = batch_get_action_items(high_priority_batch)
            for i, email in enumerate(high_priority_batch):
                if i < len(high_priority_action_items):
                    email['action_items'] = high_priority_action_items[i]
        
        # Process regular emails in batches
        final_emails = high_priority_batch.copy()
        
        if regular_emails:
            for i in range(0, len(regular_emails), 5):
                batch = regular_emails[i:i+5]
                batch_get_action_items = batch_get_action_items(batch)
                for j, email in enumerate(batch):
                    if j < len(batch_get_action_items):
                        email['action_items'] = batch_get_action_items[j]
                    final_emails.append(email)
        
        session['final_emails'] = final_emails
        session['last_load'] = datetime.now(timezone.utc)
        
        all_items = []
        for email in final_emails:
            all_items.append(email["action_items"])
        
        return render_template('summary.html', summary = all_items)

@app.route('/email_cleaner', methods=["GET", "POST"])
@login_required
def email_cleaner():
    """Process emails directly from Gmail"""
    if 'google_credentials' not in session:
        # Redirect to Google OAuth flow
        return redirect(url_for('google_login'))
        
    senders = fetch_all_gmail_emails(0)
    final = [{"sender": sender, "number": count} for sender, count in senders]
    session["final_inbox"] = copy.deepcopy(final)
        
    return render_template('upload.html', text=final, to_delete=session.get("deleted", False))


def fetch_all_gmail_emails(max_results=0, force_refresh=False):
    """Fetch all Gmail emails with caching and search for unsubscribe links if sender is new."""
    if not force_refresh and "senders_cache" in session:
        return session["senders_cache"]

    creds = Credentials.from_authorized_user_info(session["google_credentials"])
    service = build("gmail", "v1", credentials=creds)

    senders = {}
    processed_count = 0
    page_token = None
    batch_size = 500  # Gmail API limit per request

    while True:
        if max_results > 0 and processed_count >= max_results:
            break

        results = service.users().messages().list(
            userId='me',
            maxResults=batch_size,
            pageToken=page_token
        ).execute()

        messages = results.get('messages', [])
        if not messages:
            break

        for message in messages:
            msg = service.users().messages().get(userId='me', id=message['id']).execute()
            headers = {header['name'].lower(): header['value'] for header in msg['payload']['headers']}
            sender = headers.get('from', '')
            
            if sender:
                if sender not in senders:
                    unsubscribe_link = headers.get('list-unsubscribe', '').strip('<>')
                    if not unsubscribe_link:
                        email_body = get_message_body(msg['payload'].get('parts', []))
                        unsubscribe_link = find_unsubscribe_link(email_body)
                    
                    if unsubscribe_link:
                        process_unsubscribe_link(unsubscribe_link, sender)
                
                senders[sender] = senders.get(sender, 0) + 1

            processed_count += 1
            print(processed_count)
            if processed_count % 100 == 0:
                time.sleep(1)  # Rate limiting to avoid quota issues

        page_token = results.get('nextPageToken')
        if not page_token:
            break

    sorted_senders = sorted(senders.items(), key=lambda x: x[1], reverse=True)
    
    # Cache result in session
    session["senders_cache"] = copy.deepcopy(sorted_senders)
    return sorted_senders

def get_message_body(parts):
    """Extract the email body from message parts recursively."""
    body = ""
    for part in parts:
        if part.get('mimeType') in ['text/plain', 'text/html'] and 'data' in part.get('body', {}):
            data = part['body']['data']
            text = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
            body += text
        elif 'parts' in part:
            body += get_message_body(part['parts'])
    return body

def find_unsubscribe_link(email_body):
    """Find an unsubscribe link in the email body using regex pattern."""
    pattern = r"(?i)Unsubscribe[^a-z0-9\s]?\s*\[LINK:\s*([^\]]+)\]"
    match = re.search(pattern, email_body)
    return match.group(1) if match else ''

def process_unsubscribe_link(link, sender):
    """Process and store the unsubscribe link in the database."""
    if not link:
        return
    
    link_obj = Link.query.filter_by(link=link).first()
    if link_obj:
        unsubj = Unsubscribe.query.filter_by(link=link_obj.link).first()
        if not unsubj:
            new_unsub = Unsubscribe(sender=sender, user=current_user.id, link=link_obj.link)
            db.session.add(new_unsub)
            print(new_unsub)
            db.session.commit()


@app.route('/delete_sender', methods=["POST"])
def delete_sender():
    """Remove a sender from the tracked list without re-fetching emails from Gmail."""
    sender_name = request.json.get("sender_name")

    if not sender_name:
        return jsonify({"error": "No sender specified"}), 400

    # Store the deleted sender in session
    deleted_senders = session.get("deleted", [])

    if sender_name not in deleted_senders:
        deleted_senders.append(sender_name)
        session["deleted"] = deleted_senders

    # Filter the cached senders list instead of re-fetching
    if "senders_cache" in session:
        session["senders_cache"] = [
            (sender, count) for sender, count in session["senders_cache"] if sender != sender_name
        ]
    session.modified = True

    return jsonify({
        "message": f"Removed {sender_name} from the list.",
        "senders": session["senders_cache"],
        "deleted": deleted_senders,
    })

@app.route('/restore_sender', methods=["POST"])
def restore_sender():
    """Restore a sender back to the tracked list if it was previously removed."""
    sender_name = request.json.get("sender_name")
    
    if not sender_name:
        return jsonify({"error": "No sender specified"}), 400
    
    # Retrieve deleted senders
    deleted_senders = session.get("deleted", [])
    
    if sender_name not in deleted_senders:
        return jsonify({"error": f"{sender_name} was not previously deleted"}), 400
    
    # Remove sender from deleted list
    deleted_senders.remove(sender_name)
    session["deleted"] = deleted_senders
    
    # Restore the sender to the cached senders list if available
    if "senders_cache" in session and session.get("final_inbox", False):
        print(session["final_inbox"])
        
        # For reverse sorting, we need to negate the key value
        if not isinstance(session["senders_cache"], SortedList):
            # Create a new SortedList with a custom key function to sort by number in descending order
            session["senders_cache"] = SortedList(session["senders_cache"], key=lambda x: -x[1])
        
        # Process the inbox as before
        for sender in session["final_inbox"]:
            print(sender["sender"], sender["number"], sender["sender"] == sender_name)
            if sender["sender"] == sender_name:
                # This will automatically insert at the correct position (reverse sorted)
                session["senders_cache"].add((sender["sender"], sender["number"]))
        
        # Convert SortedList back to a regular list for JSON serialization
        session["senders_cache"] = list(session["senders_cache"])
    
    return jsonify({
        "message": f"Restored {sender_name} to the list.",
        "senders": session["senders_cache"],
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
    
    print(senders_to_delete)
    
    deleted_count = 0
    for sender in senders_to_delete:
        try:
            query = f"from:{sender}"
            page_token = None
            
            while True:
                response = service.users().messages().list(
                    userId="me", 
                    q=query,
                    maxResults=500,
                    pageToken=page_token
                ).execute()
                
                if "messages" not in response:
                    break

                print(response["messages"])
                    
                for msg in response["messages"]:
                    service.users().messages().trash(userId="me", id=msg["id"]).execute()
                    deleted_count += 1
                    
                    # Rate limiting
                    if deleted_count % 50 == 0:
                        time.sleep(1)
                
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
                    
        except Exception as e:
            return jsonify({"error": f"Failed while processing {sender}: {str(e)}"}), 500
    
    # Clear the deleted senders list
    session["deleted"] = []
    
    return jsonify({
        "message": f"Successfully moved {deleted_count} emails from {len(senders_to_delete)} senders to trash"
    })

