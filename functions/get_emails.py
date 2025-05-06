import re
import logging
from datetime import datetime
from imapclient import IMAPClient
from email import message_from_bytes
from email.header import decode_header
import html2text
import short_url

from app import db
from models import Link

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
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
               after_date: str,
               since_time: str = None,
               before_date: str = None,
               old: list = None) -> list:
    """
    Fetch and process emails via IMAP, returning list of dicts with keys:
    'from', 'subject', 'body', 'utc'.

    - Batches DB commits for link shortening.
    - Deduplicates based on existing 'old' list of dicts.
    """
    msgs = []
    try:
        # Map provider to IMAP settings
        if host.lower() == 'gmail':
            imap_host = 'imap.gmail.com'
            folder = 'INBOX'
        else:
            raise ValueError(f'Unsupported host: {host}')

        # Parse date filters
        parsed_after = datetime.strptime(after_date, "%m-%d-%y")
        since_date_str = parsed_after.strftime("%d-%b-%Y")
        parsed_after_dt = datetime.strptime(f"{after_date} {since_time}", "%m-%d-%y %H:%M:%S") if since_time else None

        before_date_str = None
        parsed_before_dt = None
        if before_date:
            parsed_before = datetime.strptime(before_date, "%m-%d-%y")
            before_date_str = parsed_before.strftime("%d-%b-%Y")
            parsed_before_dt = parsed_before

        # Prepare dedupe set
        existing = set()
        if old:
            for e in old:
                existing.add(f"{e['from']}|{e['subject']}")

        # Connect and fetch
        with IMAPClient(imap_host) as client:
            client.oauth2_login(user_email, token)
            client.select_folder(folder)

            criteria = ['SINCE', since_date_str]
            if before_date_str:
                criteria.extend(['BEFORE', before_date_str])

            uids = client.search(criteria)
            logger.info(f"Found {len(uids)} messages SINCE {since_date_str} TO {before_date_str or 'now'}")

            # Fetch in batches
            for i in range(0, len(uids), 50):
                batch = uids[i:i+50]
                resp = client.fetch(batch, ['RFC822', 'INTERNALDATE'])
                for uid, data in resp.items():
                    internal = data[b'INTERNALDATE']
                    if parsed_after_dt and internal < parsed_after_dt:
                        continue
                    if parsed_before_dt and internal >= parsed_before_dt:
                        continue

                    raw = data[b'RFC822']
                    msg = message_from_bytes(raw)
                    frm = safe_decode_header(msg['From'])
                    subj = safe_decode_header(msg['Subject'])
                    key = f"{frm}|{subj}"
                    if key in existing:
                        continue

                    body = extract_content(msg)
                    msgs.append({'from': frm, 'subject': subj, 'body': body, 'utc': internal})
                    existing.add(key)

        # Commit all new links at once
        db.session.commit()
        _link_cache.clear()
        return msgs

    except Exception as e:
        logger.error(f"Failed to fetch emails: {e}")
        raise
