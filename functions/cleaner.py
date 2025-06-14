# cleaner.py - Email processing functions for Gmail and Outlook
import base64
import re
import time
import copy
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from bs4 import BeautifulSoup
from flask import session, current_app
from functions.refresh_token import refresh
from app import db, Unsubscribe, Link, short_url, current_user

def get_email_service_type():
    """Determine which email service to use based on user's stored provider"""
    if hasattr(current_user, 'provider') and current_user.provider:
        return current_user.provider.lower()
    else:
        return None

def get_gmail_service():
    """Initialize Gmail service with fresh token"""
    access_token = refresh(current_user)
    # Create credentials object with just the access token
    creds = Credentials(token=access_token)
    return build("gmail", "v1", credentials=creds)

def get_outlook_headers():
    """Get headers for Outlook API requests with fresh token"""
    access_token = refresh(current_user)
    return {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

def get_messages_batch_gmail(service, batch_size, page_token=None):
    """Get a batch of Gmail messages"""
    try:
        request_size = min(500, batch_size) if batch_size > 0 else 500
        
        results = service.users().messages().list(
            userId='me',
            maxResults=request_size,
            pageToken=page_token
        ).execute()
        
        messages = results.get('messages', [])
        next_token = results.get('nextPageToken')
        
        return messages, next_token
    except Exception as e:
        print(f"Error fetching Gmail messages: {str(e)}")
        return [], None

def get_messages_batch_outlook(batch_size, page_token=None):
    """Get a batch of Outlook messages"""
    try:
        headers = get_outlook_headers()
        top = min(999, batch_size) if batch_size > 0 else 999  # Outlook max is 999
        
        url = f"https://graph.microsoft.com/v1.0/me/messages?$top={top}"
        if page_token:
            url = page_token  # For Outlook, page_token is the full next URL
        
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        
        messages = data.get('value', [])
        next_token = data.get('@odata.nextLink')
        
        return messages, next_token
    except Exception as e:
        print(f"Error fetching Outlook messages: {str(e)}")
        return [], None

def get_message_details_gmail(service, message_id):
    """Get Gmail message details"""
    try:
        msg = service.users().messages().get(userId='me', id=message_id).execute()
        
        headers = {header['name'].lower(): header['value'] for header in msg['payload']['headers']}
        sender = headers.get('from', '')
        
        # Get unsubscribe link from headers
        unsubscribe_link = headers.get('list-unsubscribe', '').strip('<>')
        
        # If no unsubscribe in headers, check body
        if not unsubscribe_link:
            email_body = get_gmail_message_body(msg)
            unsubscribe_link = find_unsubscribe_link(email_body)
        
        return {
            'sender': sender,
            'unsubscribe_link': unsubscribe_link,
            'id': message_id
        }
    except Exception as e:
        print(f"Error getting Gmail message details: {str(e)}")
        return None

def get_message_details_outlook(message):
    """Get Outlook message details (message is already detailed from API)"""
    try:
        sender = message.get('from', {}).get('emailAddress', {}).get('address', '')
        if not sender and message.get('from', {}).get('emailAddress', {}).get('name'):
            sender = message.get('from', {}).get('emailAddress', {}).get('name', '')
        
        # Outlook doesn't typically provide unsubscribe headers in the basic message
        # We'd need to get the full message body to search for unsubscribe links
        unsubscribe_link = find_unsubscribe_link_outlook(message.get('body', {}).get('content', ''))
        
        return {
            'sender': sender,
            'unsubscribe_link': unsubscribe_link,
            'id': message.get('id')
        }
    except Exception as e:
        print(f"Error getting Outlook message details: {str(e)}")
        return None

def get_gmail_message_body(msg):
    """Extract the email body from a Gmail message."""
    if 'payload' not in msg:
        return ""
    
    parts = msg['payload'].get('parts', [])
    
    if not parts and 'body' in msg['payload'] and 'data' in msg['payload']['body']:
        data = msg['payload']['body']['data']
        return base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')

    return extract_body_from_parts(parts)

def extract_body_from_parts(parts):
    """Extract the email body from Gmail message parts recursively."""
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
    """Find an unsubscribe link in Gmail email body."""
    if not email_body:
        return ''
    
    unsubscribe_match = re.search(r"(?i)unsubscribe", email_body)
    
    if unsubscribe_match:
        unsubscribe_pos = unsubscribe_match.start()
        remaining_text = email_body[unsubscribe_pos:]
        
        # Check for encoded links first
        link_match = re.search(r"\[LINK:\s*([^\]]+)\]", remaining_text)
        if link_match:
            return link_match.group(1)
            
        # Check for HTML links
        html_link_match = re.search(r'href=["\'](https?://[^"\'>]+)["\']', remaining_text)
        if html_link_match:
            return html_link_match.group(1)
    
    return ''

def find_unsubscribe_link_outlook(email_body):
    """Find an unsubscribe link in Outlook email body."""
    if not email_body:
        return ''
    
    # Parse HTML content if it exists
    try:
        soup = BeautifulSoup(email_body, 'html.parser')
        
        # Look for unsubscribe links
        for link in soup.find_all('a', href=True):
            link_text = link.get_text().lower()
            link_href = link.get('href', '')
            
            if 'unsubscribe' in link_text or 'unsubscribe' in link_href.lower():
                return link_href
    except:
        pass
    
    # Fallback to regex search
    return find_unsubscribe_link(email_body)

def delete_messages_from_sender_gmail(service, sender):
    """Delete all Gmail messages from a specific sender"""
    try:
        query = f"from:{sender}"
        page_token = None
        deleted_count = 0
        
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
            
            for msg in messages:
                try:
                    service.users().messages().trash(userId="me", id=msg["id"]).execute()
                    deleted_count += 1
                    
                    if deleted_count % 50 == 0:
                        time.sleep(1)  # Rate limiting
                except Exception as msg_error:
                    print(f"Error trashing Gmail message {msg['id']}: {str(msg_error)}")
            
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        
        return deleted_count
    except Exception as e:
        print(f"Error deleting Gmail messages from {sender}: {str(e)}")
        return 0

def delete_messages_from_sender_outlook(sender):
    """Delete all Outlook messages from a specific sender"""
    try:
        headers = get_outlook_headers()
        deleted_count = 0
        
        # Search for messages from sender
        search_url = f"https://graph.microsoft.com/v1.0/me/messages?$filter=from/emailAddress/address eq '{sender}'"
        
        while search_url:
            response = requests.get(search_url, headers=headers)
            response.raise_for_status()
            data = response.json()
            
            messages = data.get('value', [])
            if not messages:
                break
            
            for message in messages:
                try:
                    delete_url = f"https://graph.microsoft.com/v1.0/me/messages/{message['id']}"
                    delete_response = requests.delete(delete_url, headers=headers)
                    delete_response.raise_for_status()
                    deleted_count += 1
                    
                    if deleted_count % 50 == 0:
                        time.sleep(1)  # Rate limiting
                except Exception as msg_error:
                    print(f"Error deleting Outlook message {message['id']}: {str(msg_error)}")
            
            search_url = data.get('@odata.nextLink')
        
        return deleted_count
    except Exception as e:
        print(f"Error deleting Outlook messages from {sender}: {str(e)}")
        return 0

def fetch_emails_batch_unified(service_type, batch_size=1000, page_token=None, current_count=0):
    """Unified function to fetch emails from either Gmail or Outlook"""
    
    senders = {}
    processed_count = current_count
    next_page_token = page_token
    process_all = batch_size == 0
    remaining_emails = batch_size if not process_all else float('inf')
    pending_unsubscribes = []
    
    # Initialize progress
    session["current_progress"] = {"count": processed_count, "status": "loading"}
    
    try:
        if service_type == 'google':
            service = get_gmail_service()
            
            while remaining_emails > 0:
                messages, next_page_token = get_messages_batch_gmail(
                    service, 
                    min(500, remaining_emails) if not process_all else 500, 
                    next_page_token
                )
                
                if not messages:
                    next_page_token = None
                    break
                
                for message in messages:
                    details = get_message_details_gmail(service, message['id'])
                    if details and details['sender']:
                        sender = details['sender']
                        senders[sender] = senders.get(sender, 0) + 1
                        
                        if details['unsubscribe_link'] and sender not in [u['sender'] for u in pending_unsubscribes]:
                            pending_unsubscribes.append({
                                'link': details['unsubscribe_link'],
                                'sender': sender
                            })
                    
                    processed_count += 1
                    remaining_emails -= 1
                    
                    if processed_count % 100 == 0:
                        session["current_progress"] = {"count": processed_count, "status": "loading"}
                    
                    if remaining_emails <= 0 and not process_all:
                        break
                
                if not next_page_token or (remaining_emails <= 0 and not process_all):
                    break
        
        elif service_type == 'microsoft':
            while remaining_emails > 0:
                messages, next_page_token = get_messages_batch_outlook(
                    min(999, remaining_emails) if not process_all else 999,
                    next_page_token
                )
                
                if not messages:
                    next_page_token = None
                    break
                
                for message in messages:
                    details = get_message_details_outlook(message)
                    if details and details['sender']:
                        sender = details['sender']
                        senders[sender] = senders.get(sender, 0) + 1
                        
                        if details['unsubscribe_link'] and sender not in [u['sender'] for u in pending_unsubscribes]:
                            pending_unsubscribes.append({
                                'link': details['unsubscribe_link'],
                                'sender': sender
                            })
                    
                    processed_count += 1
                    remaining_emails -= 1
                    
                    if processed_count % 100 == 0:
                        session["current_progress"] = {"count": processed_count, "status": "loading"}
                    
                    if remaining_emails <= 0 and not process_all:
                        break
                
                if not next_page_token or (remaining_emails <= 0 and not process_all):
                    break
        
        if pending_unsubscribes:
            process_unsubscribe_links_batch(pending_unsubscribes)
        
        # Mark as complete
        session["current_progress"] = {"count": processed_count, "status": "complete"}
                
    except Exception as e:
        print(f"Error fetching emails: {str(e)}")
        session["current_progress"] = {"count": processed_count, "status": "error"}
    
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

def delete_all_senders_from_service(senders_to_delete):
    """Delete all emails from specified senders using the appropriate service"""
    service_type = get_email_service_type()
    if not service_type:
        return 0, senders_to_delete
    
    print(f"Removing emails from these senders: {senders_to_delete}")
    
    deleted_count = 0
    failed_senders = []
    
    for sender in senders_to_delete:
        try:
            if service_type == 'google':
                service = get_gmail_service()
                sender_deleted_count = delete_messages_from_sender_gmail(service, sender)
            elif service_type == 'microsoft':
                sender_deleted_count = delete_messages_from_sender_outlook(sender)
            else:
                sender_deleted_count = 0
            
            deleted_count += sender_deleted_count
            print(f"Deleted {sender_deleted_count} emails from {sender}")
            
        except Exception as e:
            print(f"Failed to process sender {sender}: {str(e)}")
            failed_senders.append(sender)
    
    return deleted_count, failed_senders

def update_senders_cache_remove(sender_name):
    """Remove a sender from the tracked list without re-fetching emails"""
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
    
    return {
        "message": f"Removed {sender_name} from the list.",
        "text": session.get("senders_cache", []),
        "deleted": deleted_senders,
    }

def update_senders_cache_restore(sender_name):
    """Restore a sender back to the tracked list if it was previously removed"""
    deleted_senders = session.get("deleted", [])
    removed_senders_cache = session.get("removed_senders_cache", [])
    senders_cache = session.get("senders_cache", [])

    if sender_name not in deleted_senders:
        return {"error": f"{sender_name} was not previously deleted"}

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
        senders_cache.sort(key=lambda x: x.get("number", 0), reverse=True)

    session["senders_cache"] = senders_cache
    session["removed_senders_cache"] = updated_removed_cache
    session.modified = True

    print(deleted_senders)
    return {
        "message": f"Restored {sender_name} to the list.",
        "text": session["senders_cache"],
        "deleted": deleted_senders,
    }