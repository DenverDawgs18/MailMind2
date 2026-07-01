"""
Fetch recent emails from Gmail (IMAP) or Microsoft Graph.

Consumed only by the scheduler when building daily digests.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header

import html2text
import requests
from imapclient import IMAPClient

logger = logging.getLogger(__name__)

html_converter = html2text.HTML2Text()
html_converter.ignore_images = True
html_converter.ignore_links = False
html_converter.body_width = 0
html_converter.protect_links = True
html_converter.ignore_tables = False
html_converter.unicode_snob = True
html_converter.single_line_break = True


def _decode_part(part) -> str:
    raw = part.get_payload(decode=True) or b""
    for enc in filter(None, [part.get_content_charset(), 'utf-8', 'latin1']):
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode('utf-8', errors='replace')


def _safe_decode_header(header_value: str) -> str:
    if not header_value:
        return ''
    parts = []
    for chunk, charset in decode_header(header_value):
        if isinstance(chunk, bytes):
            try:
                parts.append(chunk.decode(charset or 'utf-8', errors='replace'))
            except (LookupError, UnicodeDecodeError):
                parts.append(chunk.decode('utf-8', errors='replace'))
        else:
            parts.append(str(chunk))
    return ''.join(parts).strip()


def _normalize_whitespace(text: str) -> str:
    text = text.replace('‌', '').replace('\xa0', ' ')
    return re.sub(r'\s+', ' ', text).strip()


def _extract_content(msg) -> str:
    plain, html = None, None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == 'text/plain' and plain is None:
                plain = _decode_part(part)
            elif ctype == 'text/html' and html is None:
                html = _decode_part(part)
    else:
        ctype = msg.get_content_type()
        content = _decode_part(msg)
        if ctype == 'text/plain':
            plain = content
        elif ctype == 'text/html':
            html = content

    text = plain if plain else (html_converter.handle(html) if html else '')
    return _normalize_whitespace(text)


def get_emails(host: str, user_email: str, token: str, hours: int = 24) -> list[dict]:
    """
    Return a list of ``{'from', 'subject', 'body', 'utc'}`` dicts for messages
    received in the last ``hours`` hours.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    host_lower = (host or '').lower()

    if host_lower in ('gmail', 'google'):
        return _get_gmail_emails(user_email, token, cutoff)
    if host_lower == 'microsoft':
        return _get_microsoft_emails(user_email, token, cutoff)
    raise ValueError(f'Unsupported host: {host}')


def _get_gmail_emails(user_email: str, token: str, cutoff: datetime) -> list[dict]:
    msgs = []
    # IMAP SINCE only supports date granularity; go back an extra day so we
    # don't miss anything on the boundary.
    since_date = (cutoff - timedelta(days=1)).strftime("%d-%b-%Y")

    with IMAPClient('imap.gmail.com') as client:
        client.oauth2_login(user_email, token)
        client.select_folder('INBOX')
        uids = client.search(['SINCE', since_date])
        logger.info("Gmail: %d UIDs since %s", len(uids), since_date)

        for i in range(0, len(uids), 50):
            batch = uids[i:i + 50]
            resp = client.fetch(batch, ['RFC822', 'INTERNALDATE'])
            for _uid, data in resp.items():
                internal_date = data[b'INTERNALDATE'].astimezone(timezone.utc)
                if internal_date < cutoff:
                    continue
                msg = message_from_bytes(data[b'RFC822'])
                subj = _safe_decode_header(msg['Subject'])
                if "daily to do list from mailmind" in subj.lower():
                    continue
                msgs.append({
                    'from': _safe_decode_header(msg['From']),
                    'subject': subj,
                    'body': _extract_content(msg),
                    'utc': internal_date,
                })

    logger.info("Gmail: returning %d messages", len(msgs))
    return msgs


def _get_microsoft_emails(user_email: str, token: str, cutoff: datetime) -> list[dict]:
    msgs = []
    filter_dt = cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    graph_url = "https://graph.microsoft.com/v1.0/me/messages"
    params = {
        '$filter': f"receivedDateTime ge {filter_dt}",
        '$orderby': 'receivedDateTime desc',
        '$top': 999,
        '$select': 'from,subject,body,receivedDateTime,bodyPreview',
    }
    headers = {'Authorization': f'Bearer {token}'}

    first_page = True
    while graph_url:
        resp = requests.get(
            graph_url, headers=headers,
            params=params if first_page else None, timeout=30,
        )
        first_page = False

        if resp.status_code == 401:
            raise RuntimeError("Microsoft Graph authentication failed")
        if resp.status_code != 200:
            raise RuntimeError(f"Microsoft Graph error: {resp.status_code}")

        data = resp.json()
        for message in data.get('value', []):
            try:
                received_str = message.get('receivedDateTime')
                if received_str:
                    received = datetime.fromisoformat(received_str.replace('Z', '+00:00'))
                    if received < cutoff:
                        continue
                else:
                    received = datetime.now(timezone.utc)

                from_field = message.get('from', {}).get('emailAddress', {})
                sender_name = from_field.get('name', '')
                sender_email = from_field.get('address', '')
                from_str = f"{sender_name} <{sender_email}>" if sender_name else sender_email

                body_obj = message.get('body', {})
                body_content = body_obj.get('content', '') or message.get('bodyPreview', '')
                if body_obj.get('contentType', '').lower() == 'html':
                    body_content = html_converter.handle(body_content)

                msgs.append({
                    'from': _safe_decode_header(from_str),
                    'subject': _safe_decode_header(message.get('subject', '')),
                    'body': _normalize_whitespace(body_content),
                    'utc': received,
                })
            except Exception as exc:
                logger.error("Error processing Microsoft message: %s", exc)
                continue

        graph_url = data.get('@odata.nextLink')

    logger.info("Microsoft: returning %d messages", len(msgs))
    return msgs
