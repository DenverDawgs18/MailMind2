
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
from functions.old.get_action_items import batch_get_action_items
from functions.get_one_action import get_an_action
import re
import short_url
import textwrap
from werkzeug.utils import secure_filename
import mailbox
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

from dateutil import parser
@app.route('/emails')
@login_required
def emails():
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
                            db.session.commit()
                except Exception as e:
                    print(f"Error processing unsubscribe link: {str(e)}")
                    continue

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
    session["final_emails"] = final_emails
    return jsonify({"action_item": action})

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
                            db.session.commit()
                except Exception as e:
                    print(f"Error processing unsubscribe link: {str(e)}")
                    continue
    


    return render_template('unsubs.html', unsubs=unsubs)

@app.route('/summary') 
@login_required     
def summary():
    emails = []
    text = ""
    final_emails = session.get("final_emails", False)
    if final_emails:
        for email in final_emails: 
            no_action = email["action_items"] != "No action" and email["action_items"] != "No action."
            print(no_action)
            if email["action_items"] != "Generating ..." and no_action:
                print("appending")
                emails.append(email)
            elif email["action_items"] == "Generating ...":
                print('text change')
                text = "More action items to generate on emails page"
    return render_template('summary.html', emails=emails, text = text)

@app.route('/email_cleaner', methods=["GET", "POST"])
@login_required
def email_cleaner():
    """Process emails directly from Gmail"""
    if 'google_credentials' not in session:
        return redirect(url_for('google_login'))
    
    # Initialize session variables if they don't exist
    if "senders_cache" not in session:
        session["senders_cache"] = []
    
    if "next_page_token" not in session:
        session["next_page_token"] = None
    
    if "processed_count" not in session:
        session["processed_count"] = 0
    
    # Handle AJAX request to load more emails
    if request.method == "POST" and request.headers.get("X-Requested-With") == "XMLHttpRequest":
        batch_size = 1000
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

def fetch_gmail_emails_batch(batch_size=1000, page_token=None, current_count=0):
    """Fetch a batch of Gmail emails and process senders
    
    Gmail API has a limit of 500 emails per request, so we'll process
    two batches of 500 to achieve the requested batch_size of 1000.
    """
    
    creds = Credentials.from_authorized_user_info(session["google_credentials"])
    service = build("gmail", "v1", credentials=creds)
    
    senders = {}
    processed_count = current_count
    next_page_token = page_token
    emails_to_process = batch_size
    gmail_api_limit = 500  # Gmail API's actual maximum per request
    
    try:
        # Process up to two batches to reach the requested batch_size
        for _ in range(2):  # Do at most 2 iterations to get ~1000 emails
            if emails_to_process <= 0:
                break
                
            # Request up to Gmail's API limit or remaining emails needed
            current_batch = min(gmail_api_limit, emails_to_process)
            
            results = service.users().messages().list(
                userId='me',
                maxResults=current_batch,
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
                                process_unsubscribe_link(unsubscribe_link, sender)
                        
                        senders[sender] = senders.get(sender, 0) + 1
                    
                    processed_count += 1
                    emails_to_process -= 1
                    
                    if processed_count % 100 == 0:
                        print(f"Processed {processed_count} emails")
                        time.sleep(0.1)  # Reduced sleep time for better UX
                
                except Exception as e:
                    print(f"Error processing message {message['id']}: {str(e)}")
                    continue
            
            # If there's no next page token or we've processed enough emails, break
            if not next_page_token or emails_to_process <= 0:
                break
    
    except Exception as e:
        print(f"Error fetching emails: {str(e)}")
    
    sorted_senders = sorted(senders.items(), key=lambda x: x[1], reverse=True)
    return sorted_senders, next_page_token, processed_count




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


def process_unsubscribe_link(link, sender):
    """Process and store the unsubscribe link in the database."""
    if not link:
        return
    
    try:
        if link.startswith('[LINK:') and link.endswith(']'):
            code = link.strip('[LINK:').strip(']').strip()
            try:
                link_id = short_url.decode_url(code)
                link_obj = Link.query.filter_by(id=link_id).first()
                if link_obj:
                    link = link_obj.link
            except Exception as e:
                print(f"Error decoding short URL: {str(e)}")
                return

        unsubj = Unsubscribe.query.filter_by(sender=sender).first()
        if not unsubj:

            new_unsub = Unsubscribe(sender=sender, link=link, user=current_user.id)
            db.session.add(new_unsub)
            print(f"Added unsubscribe link for {sender}: {link}")
            db.session.commit()
    except Exception as e:
        print(f"Error processing unsubscribe link: {str(e)}")


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
                    

                    if deleted_count % 50 == 0:
                        time.sleep(1)
                
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
                    
        except Exception as e:
            return jsonify({"error": f"Failed while processing {sender}: {str(e)}"}), 500
    
    session["deleted"] = []
    
    return jsonify({
        "message": f"Successfully moved {deleted_count} emails from {len(senders_to_delete)} senders to trash"
    })

