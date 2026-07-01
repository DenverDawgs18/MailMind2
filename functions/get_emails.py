import logging
import re
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header

import html2text
import requests
import short_url
from bs4 import BeautifulSoup
from imapclient import IMAPClient

from app import db
from models import Link, Unsubscribe

logger = logging.getLogger(__name__)

# Regex to match URLs
URL_PATTERN = re.compile(r'(https?://[^\s<>"\']+)')

# Initialize HTML-to-text converter
html_converter = html2text.HTML2Text()
html_converter.ignore_images = True
html_converter.ignore_links = False
html_converter.body_width = 0  # do not wrap lines
html_converter.protect_links = True
html_converter.ignore_tables = False
html_converter.unicode_snob = True
html_converter.single_line_break = True


def find_unsubscribe_link(email_body):
    """Find an unsubscribe link in Gmail email body."""
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


def find_unsubscribe_link_outlook(email_body):
    """Find an unsubscribe link in Outlook email body."""
    if not email_body:
        return ''

    try:
        soup = BeautifulSoup(email_body, 'html.parser')
        for link in soup.find_all('a', href=True):
            link_text = link.get_text().lower()
            link_href = link.get('href', '')
            if 'unsubscribe' in link_text or 'unsubscribe' in link_href.lower():
                return link_href
    except Exception:
        pass

    return find_unsubscribe_link(email_body)


def process_unsubscribe_links_batch(unsubscribe_data, master_id):
    """Persist unsubscribe links for the given master (user) in a single transaction."""
    if not unsubscribe_data or master_id is None:
        return

    changes_made = False
    try:
        for data in unsubscribe_data:
            link = data.get('link')
            sender = data.get('sender')
            if not link:
                continue

            try:
                if link.startswith('[LINK:') and link.endswith(']'):
                    code = link.strip('[LINK:').strip(']').strip()
                    try:
                        link_id = short_url.decode_url(code)
                    except Exception as exc:
                        logger.warning("Error decoding short URL: %s", exc)
                        continue
                    link_obj = Link.query.filter_by(id=link_id).first()
                    if not link_obj:
                        continue
                    link = link_obj.link

                match = re.search(r'https?://[^\s]+', link or '')
                link = match.group(0) if match else None
                if not link:
                    continue

                existing = Unsubscribe.query.filter_by(sender=sender, user=master_id).first()
                if not existing:
                    db.session.add(Unsubscribe(sender=sender, link=link, user=master_id))
                    changes_made = True
            except Exception as exc:
                logger.warning("Error processing unsubscribe link for %s: %s", sender, exc)
                continue

        if changes_made:
            db.session.commit()
    except Exception as exc:
        logger.exception("Error committing unsubscribe links: %s", exc)
        db.session.rollback()


def _get_or_create_short(url: str, cache: dict) -> str:
    """
    Retrieve or create a shortened code for the given URL.

    The cache is per-invocation (not module-level) so different requests do not
    share state — the previous module-level cache was a subtle memory leak /
    cross-request bug under gunicorn workers.
    """
    if url in cache:
        return cache[url]

    link = Link.query.filter_by(link=url).first()
    if not link:
        link = Link(link=url)
        db.session.add(link)
        db.session.flush()

    code = short_url.encode_url(link.id)
    # Only write ``short`` when it's actually new to avoid an UPDATE per URL.
    if link.short != code:
        link.short = code
    cache[url] = code
    return code


def normalize_whitespace(text: str) -> str:
    """Collapse all whitespace to single spaces, strip zero-width & nbsp chars."""
    text = text.replace('‌', '').replace('\xa0', ' ')
    return re.sub(r'\s+', ' ', text).strip()


def decode_part(part) -> str:
    raw = part.get_payload(decode=True) or b""
    candidates = [part.get_content_charset(), 'utf-8', 'latin1']
    for enc in filter(None, candidates):
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode('utf-8', errors='replace')


def safe_decode_header(header_value: str) -> str:
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


def extract_content(msg, link_cache: dict) -> str:
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

    if plain_text:
        text = plain_text
    elif html_text:
        text = html_converter.handle(html_text)
    else:
        return ''

    text = normalize_whitespace(text)
    text = URL_PATTERN.sub(lambda m: f"[LINK: {_get_or_create_short(m.group(1), link_cache)}]", text)
    return text


def get_emails(
    host: str,
    user_email: str,
    token: str,
    after_date: str = None,
    since_time: str = None,
    master_id: int = None,
) -> list:
    """
    Fetch and process emails via IMAP (Gmail) or Microsoft Graph API.

    Returns a list of dicts with keys: 'from', 'subject', 'body', 'utc'.

    ``master_id`` is required for unsubscribe-link persistence. When callers pass
    ``None`` (e.g. legacy contexts) the unsubscribe extraction is silently skipped.
    """
    link_cache: dict = {}
    pending_unsubscribes = []

    try:
        now = datetime.now(timezone.utc)
        if after_date and since_time:
            try:
                cutoff_datetime = datetime.strptime(
                    f"{after_date} {since_time}", "%m-%d-%y %H:%M:%S"
                )
                if cutoff_datetime.tzinfo is None:
                    cutoff_datetime = cutoff_datetime.replace(tzinfo=timezone.utc)
            except Exception as exc:
                logger.warning("Error parsing datetime: %s; defaulting to 24h", exc)
                cutoff_datetime = now - timedelta(hours=24)
        else:
            cutoff_datetime = now - timedelta(hours=24)

        logger.info("Getting emails since: %s", cutoff_datetime)

        host_lower = (host or '').lower()
        if host_lower in ('gmail', 'google'):
            msgs = _get_gmail_emails(user_email, token, cutoff_datetime, link_cache)
            for msg in msgs:
                unsub_link = find_unsubscribe_link(msg["body"])
                if unsub_link and msg["from"] not in [u['sender'] for u in pending_unsubscribes]:
                    pending_unsubscribes.append({'link': unsub_link, 'sender': msg['from']})
        elif host_lower == 'microsoft':
            msgs = _get_microsoft_emails(user_email, token, cutoff_datetime, link_cache)
            for msg in msgs:
                unsub_link = find_unsubscribe_link_outlook(msg["body"])
                if unsub_link and msg["from"] not in [u['sender'] for u in pending_unsubscribes]:
                    pending_unsubscribes.append({'link': unsub_link, 'sender': msg['from']})
        else:
            raise ValueError(f'Unsupported host: {host}')

        if pending_unsubscribes and master_id is not None:
            process_unsubscribe_links_batch(pending_unsubscribes, master_id)

        db.session.commit()
        return msgs

    except Exception as exc:
        logger.exception("Failed to fetch emails: %s", exc)
        raise


def _get_gmail_emails(user_email: str, token: str, cutoff_datetime: datetime, link_cache: dict) -> list:
    """Fetch emails from Gmail via IMAP."""
    msgs = []
    imap_host = 'imap.gmail.com'
    folder = 'INBOX'

    # IMAP SINCE only supports date granularity; go back an extra day so we
    # don't miss anything on the cutoff boundary.
    imap_since_date = (cutoff_datetime - timedelta(days=1)).strftime("%d-%b-%Y")

    with IMAPClient(imap_host) as client:
        client.oauth2_login(user_email, token)
        client.select_folder(folder)

        logger.info("Searching Gmail for emails since %s (IMAP date filter)", imap_since_date)
        uids = client.search(['SINCE', imap_since_date])
        logger.info("Found %d Gmail UIDs", len(uids))

        for i in range(0, len(uids), 50):
            batch = uids[i:i + 50]
            resp = client.fetch(batch, ['RFC822', 'INTERNALDATE'])
            for uid, data in resp.items():
                internal_date = data[b'INTERNALDATE'].astimezone(timezone.utc)
                if internal_date < cutoff_datetime:
                    continue

                raw = data[b'RFC822']
                msg = message_from_bytes(raw)
                frm = safe_decode_header(msg['From'])
                subj = safe_decode_header(msg['Subject'])
                if "daily to do list from mailmind" in subj.lower():
                    continue
                body = extract_content(msg, link_cache)
                msgs.append({'from': frm, 'subject': subj, 'body': body, 'utc': internal_date})

    logger.info("Gmail: returning %d messages after time filtering", len(msgs))
    return msgs


def _get_microsoft_emails(user_email: str, token: str, cutoff_datetime: datetime, link_cache: dict) -> list:
    """Fetch emails from Microsoft via Graph API."""
    msgs = []

    filter_datetime = cutoff_datetime.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    graph_url = "https://graph.microsoft.com/v1.0/me/messages"
    params = {
        '$filter': f"receivedDateTime ge {filter_datetime}",
        '$orderby': 'receivedDateTime desc',
        '$top': 999,
        '$select': 'from,subject,body,receivedDateTime,bodyPreview',
    }
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }

    logger.info("Searching Microsoft emails since %s", filter_datetime)

    first_page = True
    while graph_url:
        response = requests.get(
            graph_url,
            headers=headers,
            params=params if first_page else None,
            timeout=30,
        )
        first_page = False

        if response.status_code == 401:
            logger.error("Microsoft Graph API returned 401 - token may be expired")
            raise Exception("Microsoft Graph authentication failed - token may be expired")

        if response.status_code != 200:
            logger.error("Microsoft Graph API error: %s - %s", response.status_code, response.text)
            raise Exception(f"Microsoft Graph API error: {response.status_code}")

        data = response.json()
        messages = data.get('value', [])
        logger.info("Retrieved %d Microsoft messages", len(messages))

        for message in messages:
            try:
                received_dt_str = message.get('receivedDateTime')
                if received_dt_str:
                    received_dt = datetime.fromisoformat(received_dt_str.replace('Z', '+00:00'))
                    if received_dt < cutoff_datetime:
                        continue
                else:
                    received_dt = datetime.now(timezone.utc)

                from_field = message.get('from', {})
                sender_name = from_field.get('emailAddress', {}).get('name', '')
                sender_email = from_field.get('emailAddress', {}).get('address', '')
                from_str = f"{sender_name} <{sender_email}>" if sender_name else sender_email
                from_str = safe_decode_header(from_str)

                subject = safe_decode_header(message.get('subject', ''))

                body_obj = message.get('body', {})
                if body_obj.get('content'):
                    content = body_obj.get('content', '')
                    content_type = body_obj.get('contentType', 'text').lower()
                    body_content = (
                        html_converter.handle(content) if content_type == 'html' else content
                    )
                else:
                    body_content = message.get('bodyPreview', '')

                body_content = normalize_whitespace(body_content)
                body_content = URL_PATTERN.sub(
                    lambda m: f"[LINK: {_get_or_create_short(m.group(1), link_cache)}]",
                    body_content,
                )

                msgs.append({
                    'from': from_str,
                    'subject': subject,
                    'body': body_content,
                    'utc': received_dt,
                })

            except Exception as exc:
                logger.error("Error processing Microsoft message: %s", exc)
                continue

        graph_url = data.get('@odata.nextLink')

    logger.info("Microsoft: returning %d messages after time filtering", len(msgs))
    return msgs
