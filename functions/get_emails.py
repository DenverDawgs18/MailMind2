import os
import email
import re
import hashlib
from datetime import datetime
from imapclient import IMAPClient
from email.header import decode_header
from bs4 import BeautifulSoup
import short_url
from app import db
from models import Link
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Precompiled regex patterns for better performance
URL_PATTERN = re.compile(r'https?://[^\s<>"\']+=|&')
WHITESPACE_PATTERN = re.compile(r'[ ]+')
NEWLINE_PATTERN = re.compile(r'\s*\n\s*')
MULTI_NEWLINE_PATTERN = re.compile(r'\n+')
TEMPLATE_ARTIFACTS = [
    (re.compile(r'\s*raw\s*'), ' '),
    (re.compile(r'\{%\s*endraw\s*%}'), ' '),
    (re.compile(r'%}'), ' ')
]

def format_link(url):

    # Remove common trailing punctuation
    url = url.strip('.,;:()[]{}\'"')
    
    # Use database lookup with caching
    link = Link.query.filter_by(link=url).first()
    if not link:
        link = Link(link=url)
        db.session.add(link)
        db.session.commit()
    
    short_code = short_url.encode_url(link.id)
    link.short = short_code
    return short_code

def decode_email_body(part):
    """Attempt to decode email body with multiple encodings."""
    if not hasattr(part, 'get_payload'):
        return None

    payload = part.get_payload(decode=True)
    if not payload:
        return None

    # Try encodings in order of likelihood
    encodings = [part.get_content_charset(), 'utf-8', 'iso-8859-1', 'windows-1252', 'ascii', 'latin1']
    encodings = [e for e in encodings if e]

    for encoding in encodings:
        try:
            return payload.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue

    # Fallback with error replacement
    return payload.decode('utf-8', errors='replace')

def safe_decode_header(header_value):
    """Safely decode email headers."""
    if not header_value:
        return ""

    try:
        decoded_parts = decode_header(header_value)
        decoded_string = ""
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                try:
                    decoded_string += part.decode(charset or 'utf-8', errors='replace')
                except (UnicodeDecodeError, LookupError):
                    decoded_string += part.decode('utf-8', errors='replace')
            else:
                decoded_string += str(part)
        return decoded_string.strip()
    except Exception as e:
        logger.warning(f"Header decode error: {str(e)}")
        return ""

def clean_email_text(text):
    """Clean and format email text content."""
    if not text:
        return ""

    # Remove extraneous Unicode whitespace characters
    text = text.replace('\u200c', '').replace('\xa0', ' ')
    
    # Remove templating artifacts
    for pattern, replacement in TEMPLATE_ARTIFACTS:
        text = pattern.sub(replacement, text)
    
    # Normalize whitespace
    text = NEWLINE_PATTERN.sub('\n', text)
    text = MULTI_NEWLINE_PATTERN.sub('\n', text)
    text = WHITESPACE_PATTERN.sub(' ', text)
    text = text.strip()

    # Replace raw URLs with short links
    text = URL_PATTERN.sub(lambda match: f"[LINK: {format_link(match.group(0))}]", text)

    # Keep only ASCII characters
    text = ''.join(char for char in text if ord(char) < 128)

    return text

def process_html_content(html_content):
    """Process HTML content into plain text format."""
    soup = BeautifulSoup(html_content, "html.parser")
    
    # Remove non-content elements
    for tag in soup(["script", "style", "head", "meta"]):
        tag.decompose()

    # Process images
    for img in soup.find_all("img"):
        alt_text = img.get("alt", "").strip()
        src = img.get("src", "").strip()
        replacement = f"[IMAGE: {alt_text or src or 'N/A'}]"
        img.replace_with(replacement)

    # Replace <br> with newlines
    for br in soup.find_all("br"):
        br.replace_with("\n")
        
    # Add line breaks for block elements
    for tag in soup.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6"]):
        text = tag.get_text(separator=" ", strip=True)
        tag.clear()
        tag.append(text + "\n")
        
    # Process links
    for a in soup.find_all("a"):
        href = a.get("href")
        anchor_text = a.get_text().strip()
        if href:
            short_code = format_link(href)
            replacement = f"{anchor_text} [LINK: {short_code}]" if anchor_text else f"[LINK: {short_code}]"
        else:
            replacement = anchor_text
        a.replace_with(replacement)
    
    return soup.get_text()

def extract_email_content(msg):
    """Extract the content from an email message, prioritizing plain text over HTML."""
    body = None
    html_content = None
    
    try:        
        if msg.is_multipart():
            parts = []
            for part in msg.walk():
                content_type = part.get_content_type()
                parts.append(content_type)
                
            # Prefer plain text over HTML
            if "text/plain" in parts:
                print("Plain")
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        decoded_content = decode_email_body(part)
                        if decoded_content:
                            body = decoded_content
                            break
            elif "text/html" in parts and not body:
                print("HTML")
                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        decoded_content = decode_email_body(part)
                        if decoded_content:
                            html_content = decoded_content
                            break
            else:
                print("N/A")
        else:
            print("Single-part email detected")
            content_type = msg.get_content_type()
            
            if content_type == "text/plain":
                print("Processing single-part plain text email")
                body = decode_email_body(msg)
            elif content_type == "text/html":
                print("Processing single-part HTML email")
                html_content = decode_email_body(msg)
            else:
                print(f"Unsupported single-part email type: {content_type}")
        
        # Only use HTML if no plain text was found
        if not body and html_content:
            body = process_html_content(html_content)
        
        return clean_email_text(body) if body else "No readable content found"
    except Exception as e:
        logger.error(f"Content extraction error: {str(e)}")
        return f"Error extracting content: {str(e)}"

def get_emails(host, user_email, token, after_date, since_time=None, before_date=None, old=None):
    """
    Fetch emails from an IMAP server with OAuth2 authentication.
    
    Args:
        host: Email provider ('gmail' currently supported)
        user_email: User's email address
        token: OAuth2 token
        after_date: Date string (MM-DD-YY)
        since_time: Optional time string (HH:MM:SS)
        before_date: Optional end date string (MM-DD-YY) (exclusive)
        old: Optional list of previously fetched emails to avoid duplicates
        
    Returns:
        List of email dictionaries with from, subject, and body fields
    """
    msgs = []
    try:
        # Map host to IMAP server
        if host == 'gmail':
            imap_host = 'imap.gmail.com'
            folder = 'INBOX'
        else:
            raise ValueError(f'Unsupported email host: {host}')

        # Parse date/time filters
        try:
            parsed_date = datetime.strptime(after_date, "%m-%d-%y")
            since_date = parsed_date.strftime("%d-%b-%Y")
            
            if since_time:
                parsed_datetime = datetime.strptime(f"{after_date} {since_time}", "%m-%d-%y %H:%M:%S")
            else:
                parsed_datetime = None
            
            before_datetime = None
            if before_date:
                before_datetime = datetime.strptime(before_date, "%m-%d-%y")
                before_date_str = before_datetime.strftime("%d-%b-%Y")
            
        except ValueError as e:
            raise ValueError(f"Invalid date format: {str(e)}")

        # Track new emails and use a set for faster duplicate checking
        emails = []
        existing_emails = set()
        if old:
            # Create a set of email fingerprints for fast lookup
            for item in old:
                email_key = f"{item['from']}|{item['subject']}"
                existing_emails.add(email_key)

        # Connect to IMAP server
        with IMAPClient(imap_host) as client:
            client.oauth2_login(user_email, token)
            client.select_folder(folder)
            
            # Construct search criteria
            search_criteria = ['SINCE', since_date]
            if before_date:
                search_criteria.extend(['BEFORE', before_date_str])
            
            # Fetch emails in the specified date range
            messages = client.search(search_criteria)
            logger.info(f"Found {len(messages)} messages from {since_date} to {before_date_str if before_date else 'present'}")
            
            # Process in batches for better memory management
            batch_size = 50
            for i in range(0, len(messages), batch_size):
                batch = messages[i:i+batch_size]
                if not batch:
                    continue

                logger.info(f"Processing batch {i//batch_size + 1}/{(len(messages) + batch_size - 1)//batch_size}")
                response = client.fetch(batch, ["RFC822", "INTERNALDATE"])
                
                for msgid, data in response.items():
                    try:
                        # Get message data
                        raw_email = data[b"RFC822"]
                        internal_date = data[b"INTERNALDATE"]
                        
                        # Skip if before specified time
                        if parsed_datetime and internal_date < parsed_datetime:
                            continue
                        
                        # Skip if after cap date
                        if before_datetime and internal_date >= before_datetime:
                            continue
                        
                        # Parse email
                        msg = email.message_from_bytes(raw_email)
                        subject = safe_decode_header(msg["Subject"])
                        from_email = safe_decode_header(msg["From"])
                        
                        # Check for duplicates
                        email_key = f"{from_email}|{subject}"
                        if email_key in existing_emails:
                            continue
                        
                        # Extract content
                        body = extract_email_content(msg)
                        new_email = {
                            'from': from_email,
                            'subject': subject,
                            'body': body,
                            'utc': internal_date,
                        }
                        msgs.append(msg)
                        emails.append(new_email)
                        existing_emails.add(email_key)
                        
                    except Exception as e:
                        logger.error(f"Error processing message {msgid}: {str(e)}")
                        continue               
        return emails
    except Exception as e:
        logger.error(f"Email fetching failed: {str(e)}")
        raise Exception(f"Email fetching failed: {str(e)}")
