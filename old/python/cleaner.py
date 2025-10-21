
# cleaner_optimized_fixed.py - Rate-limited email processing functions for Gmail and Outlook
from app import db, current_user
import base64
import re
import time
import copy
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from bs4 import BeautifulSoup
from flask import session, current_app
from functions.refresh_token import refresh
from models import Unsubscribe, Link
import short_url
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import random
from urllib.parse import urlencode

# Global rate limiting variables
gmail_last_request_time = 0
gmail_request_count = 0
gmail_request_window_start = 0
outlook_last_request_time = 0
outlook_request_count = 0
outlook_request_window_start = 0

# Rate limiting constants
GMAIL_REQUESTS_PER_SECOND = 5
GMAIL_REQUESTS_PER_MINUTE = 250
OUTLOOK_REQUESTS_PER_SECOND = 10
OUTLOOK_REQUESTS_PER_MINUTE = 1000

def rate_limit_gmail():
    """Implement rate limiting for Gmail API requests"""
    global gmail_last_request_time, gmail_request_count, gmail_request_window_start
    
    current_time = time.time()
    
    # Reset minute window if needed
    if current_time - gmail_request_window_start > 60:
        gmail_request_count = 0
        gmail_request_window_start = current_time
    
    # Check if we're hitting per-minute limit
    if gmail_request_count >= GMAIL_REQUESTS_PER_MINUTE:
        sleep_time = 60 - (current_time - gmail_request_window_start)
        if sleep_time > 0:
            print(f"Gmail rate limit: sleeping {sleep_time:.1f} seconds")
            time.sleep(sleep_time)
            gmail_request_count = 0
            gmail_request_window_start = time.time()
    
    # Check if we're hitting per-second limit
    time_since_last = current_time - gmail_last_request_time
    if time_since_last < (1.0 / GMAIL_REQUESTS_PER_SECOND):
        sleep_time = (1.0 / GMAIL_REQUESTS_PER_SECOND) - time_since_last
        time.sleep(sleep_time)
    
    gmail_last_request_time = time.time()
    gmail_request_count += 1

def rate_limit_outlook():
    """Implement rate limiting for Outlook API requests"""
    global outlook_last_request_time, outlook_request_count, outlook_request_window_start
    
    current_time = time.time()
    
    # Reset minute window if needed
    if current_time - outlook_request_window_start > 60:
        outlook_request_count = 0
        outlook_request_window_start = current_time
    
    # Check if we're hitting per-minute limit
    if outlook_request_count >= OUTLOOK_REQUESTS_PER_MINUTE:
        sleep_time = 60 - (current_time - outlook_request_window_start)
        if sleep_time > 0:
            print(f"Outlook rate limit: sleeping {sleep_time:.1f} seconds")
            time.sleep(sleep_time)
            outlook_request_count = 0
            outlook_request_window_start = time.time()
    
    # Check if we're hitting per-second limit
    time_since_last = current_time - outlook_last_request_time
    if time_since_last < (1.0 / OUTLOOK_REQUESTS_PER_SECOND):
        sleep_time = (1.0 / OUTLOOK_REQUESTS_PER_SECOND) - time_since_last
        time.sleep(sleep_time)
    
    outlook_last_request_time = time.time()
    outlook_request_count += 1

def get_email_service_type():
    """Determine which email service to use based on user's stored provider"""
    if hasattr(current_user, 'provider') and current_user.provider:
        return current_user.provider.lower()
    else:
        return None

def get_gmail_service():
    """Initialize Gmail service with fresh token and timeout settings"""
    access_token = refresh(current_user)
    creds = Credentials(token=access_token)
    
    # Build service with timeout settings
    service = build("gmail", "v1", credentials=creds)
    
    # Set timeout for all requests
    service._http.timeout = 30  # 30 second timeout
    
    return service

def get_outlook_headers():
    """Get headers for Outlook API requests with fresh token"""
    access_token = refresh(current_user)
    return {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

def get_messages_batch_gmail(service, batch_size, page_token=None):
    """Get a batch of Gmail messages with proper rate limiting"""
    try:
        rate_limit_gmail()
        
        request_size = min(500, batch_size) if batch_size > 0 else 500
        
        results = service.users().messages().list(
            userId='me',
            maxResults=request_size,
            pageToken=page_token
        ).execute()
        
        messages = results.get('messages', [])
        next_token = results.get('nextPageToken')
        
        return messages, next_token
    except HttpError as e:
        if e.resp.status == 429:  # Rate limit
            print("Gmail rate limit hit, waiting 30 seconds...")
            time.sleep(30)
            return get_messages_batch_gmail(service, batch_size, page_token)
        else:
            print(f"HTTP Error fetching Gmail messages: {str(e)}")
            return [], None
    except Exception as e:
        print(f"Error fetching Gmail messages: {str(e)}")
        return [], None

def get_messages_batch_outlook(batch_size, page_token=None):
    """Get a batch of Outlook messages with proper rate limiting"""
    try:
        rate_limit_outlook()
        
        headers = get_outlook_headers()
        top = min(999, batch_size) if batch_size > 0 else 999
        
        # Optimize by selecting only required fields
        select_fields = "id,from,body,internetMessageHeaders"
        
        url = f"https://graph.microsoft.com/v1.0/me/messages?$top={top}&$select={select_fields}"
        if page_token:
            url = page_token
        
        response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code == 429:  # Rate limit
            retry_after = max(int(response.headers.get('Retry-After', 30)))
            print(f"Outlook rate limit hit, waiting {retry_after} seconds...")
            time.sleep(retry_after)
            return get_messages_batch_outlook(batch_size, page_token)
        
        response.raise_for_status()
        data = response.json()
        
        messages = data.get('value', [])
        next_token = data.get('@odata.nextLink')
        
        return messages, next_token
    except Exception as e:
        print(f"Error fetching Outlook messages: {str(e)}")
        return [], None

def get_message_details_gmail_sequential(service, message_ids, progress_callback, current_count):
    """Get Gmail message details sequentially to avoid concurrent request limits"""
    if not message_ids:
        return {}
    
    results = {}
    
    for i, msg_id in enumerate(message_ids):
        try:
            rate_limit_gmail()
            
            msg = service.users().messages().get(
                userId='me',
                id=msg_id,
                format='metadata',
                metadataHeaders=['From', 'List-Unsubscribe']
            ).execute()
            
            headers = {header['name'].lower(): header['value'] 
                      for header in msg.get('payload', {}).get('headers', [])}
            sender = headers.get('from', '')
            unsubscribe_link = headers.get('list-unsubscribe', '').strip('<>')
            
            results[msg_id] = {
                'sender': sender,
                'unsubscribe_link': unsubscribe_link,
                'id': msg_id
            }
            
            # Progress indicator
            if (i + 1) % 10 == 0:
                progress_callback({"count": current_count + i + 1, "status": "loading"})
                
        except HttpError as e:
            if e.resp.status == 429:  # Rate limit
                print(f"Rate limit hit processing message {msg_id}, waiting...")
                time.sleep(random.uniform(10, 20))
                # Retry this message
                try:
                    rate_limit_gmail()
                    msg = service.users().messages().get(
                        userId='me',
                        id=msg_id,
                        format='metadata',
                        metadataHeaders=['From', 'List-Unsubscribe']
                    ).execute()
                    
                    headers = {header['name'].lower(): header['value'] 
                              for header in msg.get('payload', {}).get('headers', [])}
                    sender = headers.get('from', '')
                    unsubscribe_link = headers.get('list-unsubscribe', '').strip('<>')
                    
                    results[msg_id] = {
                        'sender': sender,
                        'unsubscribe_link': unsubscribe_link,
                        'id': msg_id
                    }
                except Exception as retry_error:
                    print(f"Retry failed for message {msg_id}: {str(retry_error)}")
            else:
                print(f"HTTP Error processing message {msg_id}: {str(e)}")
        except Exception as e:
            print(f"Error processing message {msg_id}: {str(e)}")
    
    return results

def get_message_details_outlook_sequential(messages, progress_callback, current_count):
    """Process multiple Outlook messages sequentially to avoid rate limits"""
    if not messages:
        return {}
    
    results = {}
    
    for i, message in enumerate(messages):
        try:
            # Rate limiting is handled at the API call level
            details = get_message_details_outlook_single(message)
            if details:
                results[message['id']] = details
            
            # Progress indicator
            if (i + 1) % 25 == 0:
                progress_callback({"count": current_count + i + i, "status": "loading"})
                
        except Exception as e:
            print(f"Error processing Outlook message {message.get('id')}: {str(e)}")
    
    return results

def get_message_details_outlook_single(message):
    """Get single Outlook message details with optimized processing"""
    try:
        # Extract sender information
        sender = ''
        from_data = message.get('from', {})
        if from_data and from_data.get('emailAddress'):
            sender = from_data['emailAddress'].get('address', '')
            if not sender and from_data['emailAddress'].get('name'):
                sender = from_data['emailAddress']['name']
        
        # Try to get unsubscribe link from headers first (more reliable)
        unsubscribe_link = ''
        headers = message.get('internetMessageHeaders', [])
        
        for header in headers:
            if header.get('name', '').lower() == 'list-unsubscribe':
                unsubscribe_link = header.get('value', '').strip('<>')
                break
        
        # If no header found, search in body content
        if not unsubscribe_link:
            body_content = message.get('body', {}).get('content', '')
            unsubscribe_link = find_unsubscribe_link_outlook(body_content)
        
        return {
            'sender': sender,
            'unsubscribe_link': unsubscribe_link,
            'id': message.get('id')
        }
    except Exception as e:
        print(f"Error getting Outlook message details: {str(e)}")
        return None

def find_unsubscribe_link_outlook(email_body):
    """Optimized unsubscribe link finder for Outlook email body"""
    if not email_body:
        return ''
    
    try:
        # Use faster regex search before BeautifulSoup parsing
        unsubscribe_pattern = r'(?i)unsubscribe[^<]*<[^>]*href=["\']([^"\']*unsubscribe[^"\']*)["\']'
        match = re.search(unsubscribe_pattern, email_body)
        if match:
            return match.group(1)
        
        # More comprehensive regex for various unsubscribe patterns
        url_patterns = [
            r'(?i)href=["\']([^"\']*unsubscribe[^"\']*)["\']',
            r'(?i)(https?://[^\s<>"\']*unsubscribe[^\s<>"\']*)',
            r'(?i)unsubscribe[^<]*<[^>]*href=["\']([^"\']*)["\']'
        ]
        
        for pattern in url_patterns:
            match = re.search(pattern, email_body)
            if match:
                return match.group(1)
        
        # Fallback to BeautifulSoup only if regex fails
        soup = BeautifulSoup(email_body, 'html.parser')
        
        # Look for unsubscribe links more efficiently
        unsubscribe_selectors = [
            'a[href*="unsubscribe"]',
            'a:contains("unsubscribe")',
            'a:contains("Unsubscribe")',
            'a[href*="opt-out"]',
            'a[href*="optout"]'
        ]
        
        for selector in unsubscribe_selectors:
            try:
                link = soup.select_one(selector)
                if link and link.get('href'):
                    return link.get('href')
            except:
                continue
        
    except Exception as e:
        print(f"Error parsing HTML: {str(e)}")
    
    return find_unsubscribe_link(email_body)

def find_unsubscribe_link(email_body):
    """Find an unsubscribe link in email body."""
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

def delete_messages_from_sender_gmail(service, sender, update_delete_count):
    """Delete all Gmail messages from a specific sender with sequential processing"""
    try:
        query = f"from:{sender}"
        page_token = None
        deleted_count = 0
        
        while True:
            rate_limit_gmail()
            
            response = service.users().messages().list(
                userId="me", 
                q=query,
                maxResults=100,  # Reduced batch size
                pageToken=page_token
            ).execute()
            
            messages = response.get("messages", [])
            if not messages:
                break
            
            # Process deletions sequentially to avoid rate limits
            for msg in messages:
                try:
                    rate_limit_gmail()
                    
                    service.users().messages().trash(userId="me", id=msg["id"]).execute()
                    deleted_count += 1
                    update_delete_count({"count": deleted_count, "sender": sender, "status": "success"})
                    
                except HttpError as e:
                    if e.resp.status == 429:  # Rate limit
                        print(f"Rate limit hit deleting message, waiting...")
                        time.sleep(random.uniform(15, 30))
                        # Retry this message
                        try:
                            rate_limit_gmail()
                            service.users().messages().trash(userId="me", id=msg["id"]).execute()
                            deleted_count += 1
                        except Exception as retry_error:
                            print(f"Retry failed for message {msg['id']}: {str(retry_error)}")
                    else:
                        print(f"Error trashing Gmail message {msg['id']}: {str(e)}")
                except Exception as e:
                    print(f"Error trashing Gmail message {msg['id']}: {str(e)}")
            
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        
        return deleted_count
    except Exception as e:
        print(f"Error deleting Gmail messages from {sender}: {str(e)}")
        return 0

def delete_messages_from_sender_outlook(sender, update_delete_count):
    """Delete Outlook messages from sender with sequential processing"""
    try:
        headers = get_outlook_headers()
        deleted_count = 0
        
        # Use more efficient filtering
        filter_param = f"from/emailAddress/address eq '{sender}'"
        search_url = f"https://graph.microsoft.com/v1.0/me/messages?$filter={filter_param}&$select=id&$top=100"
        
        while search_url:
            rate_limit_outlook()
            
            response = requests.get(search_url, headers=headers, timeout=30)
            
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 60))
                print(f"Rate limit hit, waiting {retry_after} seconds...")
                time.sleep(retry_after)
                continue
            
            response.raise_for_status()
            data = response.json()
            
            messages = data.get('value', [])
            if not messages:
                break
            
            # Process deletions sequentially
            for message in messages:
                try:
                    deleted = delete_single_outlook_message(message['id'], headers)
                    if deleted:
                        deleted_count += 1
                        
                        if deleted_count % 10 == 0:
                            update_delete_count({"count": deleted_count, "sender": sender, "status": "success"})
                except Exception as e:
                    print(f"Error deleting Outlook message {message['id']}: {str(e)}")
            
            search_url = data.get('@odata.nextLink')
            
            # Add delay between pages
            if search_url:
                time.sleep(random.uniform(1.0, 2.0))
        
        return deleted_count
    except Exception as e:
        print(f"Error deleting Outlook messages from {sender}: {str(e)}")
        return 0

def delete_single_outlook_message(message_id, headers):
    """Delete a single Outlook message with retry logic"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            rate_limit_outlook()
            
            delete_url = f"https://graph.microsoft.com/v1.0/me/messages/{message_id}"
            response = requests.delete(delete_url, headers=headers, timeout=30)
            
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 10))
                time.sleep(retry_after)
                continue
            
            response.raise_for_status()
            return True
            
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"Failed to delete message {message_id} after {max_retries} attempts: {str(e)}")
                return False
            time.sleep(random.uniform(1.0, 3.0))
    
    return False

def fetch_emails_batch_unified(service_type, progress_callback, batch_size=1000, page_token=None, current_count=0):
    """Optimized unified function to fetch emails with proper rate limiting"""
    
    senders = {}
    processed_count = current_count
    next_page_token = page_token
    process_all = batch_size == 0
    remaining_emails = batch_size if not process_all else float('inf')
    pending_unsubscribes = []
    
    progress_callback({"count": processed_count, "status": "loading"})
    
    try:
        if service_type == 'google':
            service = get_gmail_service()
            
            while remaining_emails > 0:
                messages, next_page_token = get_messages_batch_gmail(
                    service, 
                    min(250, remaining_emails) if not process_all else 250,  # Reduced batch size
                    next_page_token
                )
                
                if not messages:
                    next_page_token = None
                    break
                
                # Process messages sequentially
                message_ids = [msg['id'] for msg in messages]
                batch_details = get_message_details_gmail_sequential(service, message_ids, progress_callback, processed_count)
                
                for message_id, details in batch_details.items():
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
                    
                    if processed_count % 10 == 0:
                        progress_callback({"count": processed_count, "status": "loading"})
                    
                    if remaining_emails <= 0 and not process_all:
                        break
                
                if not next_page_token or (remaining_emails <= 0 and not process_all):
                    break
                
                # Add delay between batches
                time.sleep(random.uniform(2.0, 5.0))
        
        elif service_type == 'microsoft':
            while remaining_emails > 0:
                messages, next_page_token = get_messages_batch_outlook(
                    min(250, remaining_emails) if not process_all else 250,  # Reduced batch size
                    next_page_token
                )
                
                if not messages:
                    next_page_token = None
                    break
                
                # Process messages sequentially
                batch_details = get_message_details_outlook_sequential(messages, progress_callback, processed_count)
                
                for message_id, details in batch_details.items():
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
                    
                    if processed_count % 10 == 0:
                        progress_callback({"count": processed_count, "status": "loading"})
                    
                    if remaining_emails <= 0 and not process_all:
                        break
                
                if not next_page_token or (remaining_emails <= 0 and not process_all):
                    break
                
                # Add delay between batches
                time.sleep(random.uniform(2.0, 5.0))
        
        if pending_unsubscribes:
            process_unsubscribe_links_batch(pending_unsubscribes)
        
        progress_callback({"count": processed_count, "status": "complete"})
                
    except Exception as e:
        print(f"Error fetching emails: {str(e)}")
        progress_callback({"count": processed_count, "status": "error"})
    
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

                match = re.search(r'https?://[^\s]+', link)
                link = match.group(0) if match else False

                if link:
                    unsubj = Unsubscribe.query.filter_by(sender=sender).first()
                    if not unsubj:
                        new_unsub = Unsubscribe(sender=sender, link=link, user=current_user.id)
                        db.session.add(new_unsub)
                        print(f"Added unsubscribe link for {sender}: {link}")
                        changes_made = True
                    
            except Exception as e:
                print(f"Error processing unsubscribe link for {sender}: {str(e)}")
                continue
        
        if changes_made:
            db.session.commit()
            print(f"Successfully committed {len([d for d in unsubscribe_data if d['link']])} unsubscribe link changes")
            
    except Exception as e:
        print(f"Error committing unsubscribe link changes: {str(e)}")
        db.session.rollback()

def delete_all_senders_from_service(senders_to_delete, update_delete_count):
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
                sender_deleted_count = delete_messages_from_sender_gmail(service, sender, update_delete_count)
            elif service_type == 'microsoft':
                sender_deleted_count = delete_messages_from_sender_outlook(sender, update_delete_count)
            else:
                sender_deleted_count = 0
            
            deleted_count += sender_deleted_count
            print(f"Deleted {sender_deleted_count} emails from {sender}")
            
            # Add delay between senders to avoid rate limiting
            time.sleep(random.uniform(3.0, 7.0))
            
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
                if not any(d.get("sender") == sender_name for d in removed_senders_cache):
                    removed_senders_cache.append(entry)
            else:
                updated_cache.append(entry)

        session["senders_cache"] = updated_cache
        session["removed_senders_cache"] = removed_senders_cache
    
    print("Length:", len(session["senders_cache"]))
    
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

    deleted_senders.remove(sender_name)
    session["deleted"] = deleted_senders

    restored_entry = None
    updated_removed_cache = []
    for entry in removed_senders_cache:
        if entry.get("sender") == sender_name and not restored_entry:
            restored_entry = entry
        else:
            updated_removed_cache.append(entry)

    if restored_entry:
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

# Additional optimization functions for Outlook

def get_outlook_messages_with_headers(batch_size, page_token=None):
    """Get Outlook messages with internet headers included for better unsubscribe detection"""
    try:
        headers = get_outlook_headers()
        top = min(999, batch_size) if batch_size > 0 else 999
        
        # Include internet headers for better unsubscribe link detection
        select_fields = "id,from,body,internetMessageHeaders"
        expand_fields = "internetMessageHeaders"
        
        url = f"https://graph.microsoft.com/v1.0/me/messages?$top={top}&$select={select_fields}&$expand={expand_fields}"
        if page_token:
            url = page_token
        
        response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code == 429:
            retry_after = int(response.headers.get('Retry-After', 60))
            print(f"Rate limit hit, waiting {retry_after} seconds...")
            time.sleep(retry_after)
            return get_outlook_messages_with_headers(batch_size, page_token)
        
        response.raise_for_status()
        data = response.json()
        
        messages = data.get('value', [])
        next_token = data.get('@odata.nextLink')
        
        return messages, next_token
    except Exception as e:
        print(f"Error fetching Outlook messages with headers: {str(e)}")
        return [], None

def optimize_outlook_field_selection(fields_needed):
    """Optimize Outlook API field selection based on what's actually needed"""
    base_fields = ["id", "from"]
    
    if "unsubscribe" in fields_needed:
        base_fields.extend(["internetMessageHeaders", "body"])
    
    if "subject" in fields_needed:
        base_fields.append("subject")
    
    if "date" in fields_needed:
        base_fields.extend(["receivedDateTime", "sentDateTime"])
    
    return ",".join(base_fields)

def bulk_delete_outlook_messages(message_ids, headers):
    """Attempt to delete multiple Outlook messages more efficiently"""
    if not message_ids:
        return 0
    
    deleted_count = 0
    
    # Outlook doesn't support true batch delete, so we optimize with concurrent requests
    max_workers = min(10, len(message_ids))  # Limit concurrent requests
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all delete requests
        futures = {executor.submit(delete_single_outlook_message, msg_id, headers): msg_id 
                  for msg_id in message_ids}
        
        # Process results as they complete
        for future in as_completed(futures):
            try:
                if future.result():
                    deleted_count += 1
            except Exception as e:
                msg_id = futures[future]
                print(f"Error deleting message {msg_id}: {str(e)}")
    
    return deleted_count

def get_outlook_sender_statistics(progress_callback, sample_size=5000):
    """Get sender statistics from Outlook more efficiently by sampling"""
    try:
        headers = get_outlook_headers()
        senders = {}
        processed_count = 0
        
        # Get a sample of recent messages for faster analysis
        select_fields = "id,from,receivedDateTime"
        order_by = "receivedDateTime desc"
        
        url = f"https://graph.microsoft.com/v1.0/me/messages?$top=999&$select={select_fields}&$orderby={order_by}"
        
        while url and processed_count < sample_size:
            response = requests.get(url, headers=headers, timeout=30)
            
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 60))
                time.sleep(retry_after)
                continue
            
            response.raise_for_status()
            data = response.json()
            
            messages = data.get('value', [])
            if not messages:
                break
            
            # Process messages in this batch
            for message in messages:
                from_data = message.get('from', {})
                if from_data and from_data.get('emailAddress'):
                    sender = from_data['emailAddress'].get('address', '')
                    if sender:
                        senders[sender] = senders.get(sender, 0) + 1
                
                processed_count += 1
                
                if processed_count % 100 == 0:
                    progress_callback({"count": processed_count, "status": "analyzing"})
                
                if processed_count >= sample_size:
                    break
            
            url = data.get('@odata.nextLink') if processed_count < sample_size else None
            
            if url:
                time.sleep(random.uniform(0.3, 0.7))  # Rate limiting
        
        return senders, processed_count
    
    except Exception as e:
        print(f"Error getting Outlook sender statistics: {str(e)}")
        return {}, 0

def cache_outlook_auth_token():
    """Cache Outlook authentication token to reduce refresh calls"""
    if not hasattr(cache_outlook_auth_token, 'cached_token'):
        cache_outlook_auth_token.cached_token = None
        cache_outlook_auth_token.token_expiry = 0
    
    current_time = time.time()
    
    # Check if cached token is still valid (with 5 minute buffer)
    if (cache_outlook_auth_token.cached_token and 
        current_time < cache_outlook_auth_token.token_expiry - 300):
        return cache_outlook_auth_token.cached_token
    
    # Refresh token
    try:
        access_token = refresh(current_user)
        cache_outlook_auth_token.cached_token = access_token
        # Assume token is valid for 1 hour (3600 seconds)
        cache_outlook_auth_token.token_expiry = current_time + 3600
        return access_token
    except Exception as e:
        print(f"Error refreshing Outlook token: {str(e)}")
        return None

def get_outlook_headers_cached():
    """Get Outlook headers with cached authentication token"""
    access_token = cache_outlook_auth_token()
    if not access_token:
        return get_outlook_headers()  # Fallback to original method
    
    return {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

def preprocess_outlook_messages(messages):
    """Preprocess Outlook messages to extract common data efficiently"""
    processed_messages = []
    
    for message in messages:
        try:
            # Extract sender
            sender = ''
            from_data = message.get('from', {})
            if from_data and from_data.get('emailAddress'):
                sender = from_data['emailAddress'].get('address', '')
            
            # Extract unsubscribe from headers first
            unsubscribe_link = ''
            headers = message.get('internetMessageHeaders', [])
            for header in headers:
                if header.get('name', '').lower() == 'list-unsubscribe':
                    unsubscribe_link = header.get('value', '').strip('<>')
                    break
            
            processed_messages.append({
                'id': message.get('id'),
                'sender': sender,
                'unsubscribe_link': unsubscribe_link,
                'body': message.get('body', {}).get('content', '') if not unsubscribe_link else '',
                'raw_message': message
            })
            
        except Exception as e:
            print(f"Error preprocessing message: {str(e)}")
            continue
    
    return processed_messages

def validate_outlook_api_response(response):
    """Validate Outlook API response and handle common error cases"""
    if not response:
        return False, "Empty response"
    
    try:
        if response.status_code == 200:
            data = response.json()
            if 'value' in data:
                return True, data
            else:
                return False, "No 'value' field in response"
        elif response.status_code == 429:
            return False, "Rate limit exceeded"
        elif response.status_code == 401:
            return False, "Authentication failed"
        elif response.status_code == 403:
            return False, "Permission denied"
        else:
            return False, f"HTTP {response.status_code}: {response.text}"
    
    except Exception as e:
        return False, f"Error validating response: {str(e)}"

def monitor_outlook_rate_limits():
    """Monitor and track Outlook API rate limits"""
    if not hasattr(monitor_outlook_rate_limits, 'request_count'):
        monitor_outlook_rate_limits.request_count = 0
        monitor_outlook_rate_limits.last_reset = time.time()
    
    current_time = time.time()
    
    # Reset counter every hour
    if current_time - monitor_outlook_rate_limits.last_reset > 3600:
        monitor_outlook_rate_limits.request_count = 0
        monitor_outlook_rate_limits.last_reset = current_time
    
    monitor_outlook_rate_limits.request_count += 1
    
    # Log if approaching rate limits (assuming 10,000 requests per hour)
    if monitor_outlook_rate_limits.request_count > 8000:
        print(f"Warning: High API usage - {monitor_outlook_rate_limits.request_count} requests this hour")
    
    return monitor_outlook_rate_limits.request_count