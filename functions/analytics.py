"""
First-party, cookie-light analytics: where visitors come from and how far
each source gets (signed up → forwarding set up → got a list → paying).

No third-party scripts and no IP addresses. The first page view from a new
browser sets a signed `mm_src` cookie holding the source, referring site and
landing page, and bumps a per-day counter. When that browser creates an
account, the source is copied onto the account.

Tag links with ?ref=<name> (or utm_source=<name>), e.g. mailmind.dev/?ref=hn.
"""
import re
import urllib.parse
from datetime import date, datetime, timezone

from flask import request
from itsdangerous import BadSignature, URLSafeSerializer

COOKIE = "mm_src"
COOKIE_MAX_AGE = 90 * 24 * 3600
_REF_RE = re.compile(r"[^a-z0-9._-]")
_BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|preview|facebookexternalhit|embedly|curl|wget|python-requests|"
    r"httpx|go-http|headless|lighthouse|monitor|uptime|health|consul", re.I)
# Only pages people land on; not assets, webhooks, OAuth callbacks or admin.
_SKIP_PREFIXES = ("/static", "/inbound", "/webhook", "/google/", "/microsoft/", "/admin",
                  "/create-", "/favicon", "/robots")


def clean_ref(raw) -> str:
    return _REF_RE.sub("", str(raw or "").strip().lower())[:40]


def _serializer(secret):
    return URLSafeSerializer(secret, salt="mm-src")


def read_source(secret):
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        data = _serializer(secret).loads(raw)
    except BadSignature:
        return None
    return data if isinstance(data, dict) else None


def _referrer_host(canonical_host):
    host = urllib.parse.urlsplit(request.referrer or "").netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    if not host or host == canonical_host or host.endswith(".fly.dev"):
        return ""
    return host[:120]


def label(data) -> str:
    """How a source shows up in reports: the ref tag, else the referring site, else direct."""
    if not data:
        return "unknown"
    return data.get("r") or data.get("h") or "direct"


def track_visit(secret, canonical_host, record_visit):
    """
    Called before each request. Returns (cookie_value or None). `record_visit`
    is called with (day, source, landing) for a new visitor.
    """
    if request.method != "GET" or request.path.startswith(_SKIP_PREFIXES):
        return None
    if _BOT_RE.search(request.headers.get("User-Agent", "")):
        return None

    ref = clean_ref(request.args.get("ref") or request.args.get("utm_source"))
    existing = read_source(secret)
    if existing and not (ref and not existing.get("r")):
        return None  # first touch wins; a tagged link can still fill in an untagged visit

    data = {
        "r": ref,
        "h": _referrer_host(canonical_host),
        "l": request.path[:120],
        "t": int(datetime.now(timezone.utc).timestamp()),
    }
    if existing is None:
        record_visit(date.today(), label(data), data["l"])
    return _serializer(secret).dumps(data)


def attribute(master, secret):
    """Copy the browser's source onto a newly created account."""
    data = read_source(secret)
    master.signup_source = label(data) if data else "direct"
    if data:
        master.signup_referrer = data.get("h") or None
        master.signup_landing = data.get("l") or None
