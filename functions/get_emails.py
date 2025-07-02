import requests
from datetime import datetime, timezone, timedelta
import logging
import re
from imapclient import IMAPClient
from email import message_from_bytes
from email.header import decode_header
import html2text
import short_url
from bs4 import BeautifulSoup
from models import Unsubscribe, Link
from app import db, current_user

logger = logging.getLogger(__name__)

# Regex to match URLs
URL_PATTERN = re.compile(r'(https?://[^\s<>"\']+)')

# In-memory cache for newly created short links
_link_cache = {}

# Initialize HTML-to-text converter
html_converter = html2text.HTML2Text()
html_converter.ignore_images = True
html_converter.ignore_links  = False
html_converter.body_width    = 0  # do not wrap lines
html_converter.protect_links = True
html_converter.ignore_tables = False  # Handle tables better
html_converter.unicode_snob = True   # Use Unicode characters
html_converter.single_line_break = True  # Reduce excessive line breaks

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
        
        # Single commit for all changes
        if changes_made:
            db.session.commit()
            print(f"Successfully committed {len([d for d in unsubscribe_data if d['link']])} unsubscribe link changes")
            
    except Exception as e:
        print(f"Error committing unsubscribe link changes: {str(e)}")
        db.session.rollback()

def get_or_create_short(url: str) -> str:
    """
    Retrieve or create a shortened code for the given URL, caching within the session.
    """
    if url in _link_cache:
        return _link_cache[url]

    link = Link.query.filter_by(link=url).first()
    if not link:
        link = Link(link=url)
        db.session.add(link)
        db.session.flush()  # assign link.id without committing

    code = short_url.encode_url(link.id)
    link.short = code
    _link_cache[url] = code
    return code


def normalize_whitespace(text: str) -> str:
    """
    Collapse all whitespace (including newlines) to single spaces, strip non-breaking spaces.
    """
    text = text.replace('\u200c', '').replace('\xa0', ' ')
    return re.sub(r'\s+', ' ', text).strip()


def decode_part(part) -> str:
    """
    Decode an email part payload using a list of candidate encodings.
    """
    raw = part.get_payload(decode=True) or b""
    candidates = [part.get_content_charset(), 'utf-8', 'latin1']
    for enc in filter(None, candidates):
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode('utf-8', errors='replace')


def safe_decode_header(header_value: str) -> str:
    """
    Decode email headers safely, falling back to replacement on errors.
    """
    if not header_value:
        return ''
    decoded_parts = decode_header(header_value)
    parts = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            try:
                parts.append(part.decode(charset or 'utf-8', errors='replace'))
            except (LookupError, UnicodeDecodeError):
                parts.append(part.decode('utf-8', errors='replace'))
        else:
            parts.append(str(part))
    return ''.join(parts).strip()


def extract_content(msg) -> str:
    """
    Extract and clean the best available content from an email Message object.
    Prefers plain-text, falls back to HTML.
    """
    plain_text = None
    html_text = None

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == 'text/plain' and plain_text is None:
                plain_text = decode_part(part)
            elif ctype == 'text/html' and html_text is None:
                html_text = decode_part(part)
    else:
        ctype = msg.get_content_type()
        content = decode_part(msg)
        if ctype == 'text/plain':
            plain_text = content
        elif ctype == 'text/html':
            html_text = content

    # Choose plain text if available, else convert HTML -> markdown-style text
    if plain_text:
        text = plain_text
    elif html_text:
        text = html_converter.handle(html_text)
    else:
        return ''

    # Normalize and shorten links
    text = normalize_whitespace(text)
    text = URL_PATTERN.sub(lambda m: f"[LINK: {get_or_create_short(m.group(1))}]", text)
    return text

def get_emails(host: str,
                user_email: str,
                token: str,
               after_date: str = None,
               since_time: str = None) -> list:
    """
    Fetch and process emails via IMAP (Gmail) or Microsoft Graph API (Microsoft), 
    returning list of dicts with keys: 'from', 'subject', 'body', 'utc'.
         
    - Batches DB commits for link shortening.
    - Deduplicates based on existing 'old' list of dicts.
    - If after_date and since_time are provided, uses them as filter
    - Otherwise defaults to exactly 24 hours ago from now
    """
    msgs = []
    pending_unsubscribes = []
    try:
        # Set the time window for email fetching
        now = datetime.now(timezone.utc)
                 
        # Target cutoff time - either from parameters or default to 24 hours ago
        cutoff_datetime = None
                 
        if after_date and since_time:
            # Use provided date/time
            try:
                cutoff_datetime = datetime.strptime(f"{after_date} {since_time}", "%m-%d-%y %H:%M:%S")
                if cutoff_datetime.tzinfo is None:
                    cutoff_datetime = cutoff_datetime.replace(tzinfo=timezone.utc)
            except Exception as e:
                logger.warning(f"Error parsing datetime: {e}, will default to 24 hours ago")
                cutoff_datetime = now - timedelta(hours=24)
        else:
            # Default to 24 hours ago
            cutoff_datetime = now - timedelta(hours=24)
                 
        logger.info(f"Getting emails since: {cutoff_datetime}")
        
        # Handle different providers
        if host.lower() == 'gmail' or host.lower() == "google":
            msgs = _get_gmail_emails(user_email, token, cutoff_datetime)
            for msg in msgs:
                unsub_link = find_unsubscribe_link(msg["body"])
                if unsub_link and msg["from"] not in [u['sender'] for u in pending_unsubscribes]:
                    pending_unsubscribes.append({
                        'link': unsub_link,
                        'sender': msg['from']
                    })

        elif host.lower() == 'microsoft':
            msgs = _get_microsoft_emails(user_email, token, cutoff_datetime)
            for msg in msgs:
                unsub_link = find_unsubscribe_link_outlook(msg["body"])
                if unsub_link and msg["from"] not in [u['sender'] for u in pending_unsubscribes]:
                    pending_unsubscribes.append({
                        'link': unsub_link,
                        'sender': msg['from']
                    })
        else:
            raise ValueError(f'Unsupported host: {host}')


        if pending_unsubscribes:
            process_unsubscribe_links_batch(pending_unsubscribes)
                 
        # Commit all new links at once
        db.session.commit()
        _link_cache.clear()
        return msgs
         
    except Exception as e:
        logger.error(f"Failed to fetch emails: {e}")
        raise

def _get_gmail_emails(user_email: str, token: str, cutoff_datetime: datetime) -> list:
    """Fetch emails from Gmail via IMAP"""
    msgs = []
    imap_host = 'imap.gmail.com'
    folder = 'INBOX'
    
    # For IMAP SINCE query, we need to go back 2 days to ensure we don't miss any emails
    # because SINCE only works with dates, not times
    imap_since_date = (cutoff_datetime - timedelta(days=1)).strftime("%d-%b-%Y")
             
    # Connect and fetch
    with IMAPClient(imap_host) as client:
        client.oauth2_login(user_email, token)
        client.select_folder(folder)
                     
        logger.info(f"Searching for emails since {imap_since_date} (IMAP date filter)")
        criteria = ['SINCE', imap_since_date]
                     
        uids = client.search(criteria)
        logger.info(f"Found {len(uids)} messages matching date filter")
                     
        # Fetch in batches
        for i in range(0, len(uids), 50):
            batch = uids[i:i+50]
            resp = client.fetch(batch, ['RFC822', 'INTERNALDATE'])
            for uid, data in resp.items():
                internal_date = data[b'INTERNALDATE'].astimezone(timezone.utc)
                                     
                # Filter emails based on our precise cutoff time
                if internal_date < cutoff_datetime:
                    continue
                                     
                raw = data[b'RFC822']
                msg = message_from_bytes(raw)
                frm = safe_decode_header(msg['From'])
                subj = safe_decode_header(msg['Subject'])
                body = extract_content(msg)
                msgs.append({'from': frm, 'subject': subj, 'body': body, 'utc': internal_date})
                         
    logger.info(f"After time filtering, returning {len(msgs)} Gmail messages")
    return msgs

def _get_microsoft_emails(user_email: str, token: str, cutoff_datetime: datetime) -> list:
    """Fetch emails from Microsoft via Graph API"""
    msgs = []
    
    # Format datetime for Microsoft Graph API filter
    # Graph API expects ISO 8601 format: 2023-01-01T00:00:00Z
    filter_datetime = cutoff_datetime.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    
    # Microsoft Graph API endpoint for messages
    graph_url = "https://graph.microsoft.com/v1.0/me/messages"
    
    # Query parameters
    params = {
        '$filter': f"receivedDateTime ge {filter_datetime}",
        '$orderby': 'receivedDateTime desc',
        '$top': 999,  # Maximum per request
        '$select': 'from,subject,body,receivedDateTime,bodyPreview'
    }
    
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    
    logger.info(f"Searching Microsoft emails since {filter_datetime}")
    
    # Handle pagination
    while graph_url:
        response = requests.get(graph_url, headers=headers, params=params if graph_url == "https://graph.microsoft.com/v1.0/me/messages" else None)
        
        if response.status_code == 401:
            # Token might be expired, try to refresh
            logger.error("Microsoft Graph API returned 401 - token may be expired")
            raise Exception("Microsoft Graph authentication failed - token may be expired")
        
        if response.status_code != 200:
            logger.error(f"Microsoft Graph API error: {response.status_code} - {response.text}")
            raise Exception(f"Microsoft Graph API error: {response.status_code}")
        
        data = response.json()
        messages = data.get('value', [])
        
        logger.info(f"Retrieved {len(messages)} messages from Microsoft Graph")
        
        for message in messages:
            try:
                # Parse the received datetime
                received_dt_str = message.get('receivedDateTime')
                if received_dt_str:
                    # Parse ISO format datetime
                    received_dt = datetime.fromisoformat(received_dt_str.replace('Z', '+00:00'))
                    
                    # Double-check our filter (Graph API should handle this, but be safe)
                    if received_dt < cutoff_datetime:
                        continue
                else:
                    received_dt = datetime.now(timezone.utc)
                
                # Extract sender information using safe_decode_header
                from_field = message.get('from', {})
                sender_name = from_field.get('emailAddress', {}).get('name', '')
                sender_email = from_field.get('emailAddress', {}).get('address', '')
                from_str = f"{sender_name} <{sender_email}>" if sender_name else sender_email
                from_str = safe_decode_header(from_str)
                
                # Extract subject using safe_decode_header
                subject = safe_decode_header(message.get('subject', ''))
                
                # Extract and process body content using existing functions
                body_obj = message.get('body', {})
                body_content = ''
                
                if body_obj.get('content'):
                    content = body_obj.get('content', '')
                    content_type = body_obj.get('contentType', 'text').lower()
                    
                    if content_type == 'html':
                        # Convert HTML to text using html_converter
                        body_content = html_converter.handle(content)
                    else:
                        # Already plain text
                        body_content = content
                else:
                    # Fallback to bodyPreview
                    body_content = message.get('bodyPreview', '')
                
                # Apply the same processing as Gmail emails
                body_content = normalize_whitespace(body_content)
                body_content = URL_PATTERN.sub(lambda m: f"[LINK: {get_or_create_short(m.group(1))}]", body_content)

                
                msgs.append({
                    'from': from_str,
                    'subject': subject,
                    'body': body_content,
                    'utc': received_dt
                })
                
            except Exception as e:
                logger.error(f"Error processing Microsoft message: {e}")
                continue
        
        # Check for next page
        graph_url = data.get('@odata.nextLink')
        params = None  # Don't send params on subsequent requests
    
    logger.info(f"After time filtering, returning {len(msgs)} Microsoft messages")
    return msgs