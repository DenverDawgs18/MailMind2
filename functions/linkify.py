import re

import bleach
import short_url
from markupsafe import Markup, escape

from models import Link

# HTML sanitizer policy — only anchors with these attributes survive.
_ALLOWED_TAGS = ["a"]
_ALLOWED_ATTRS = {"a": ["href", "target", "rel"]}
_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]

# ``[LINK: CODE]`` markers embedded by extract_content() in get_emails.py.
# The pattern also captures the word (if any) that immediately precedes the
# marker so we can turn that word into the anchor text — that behavior is
# preserved from the original implementation.
_LINK_PATTERN = re.compile(r"(\w+)?(\s*)\[LINK:\s*(\w+)\]")


def _resolve(code: str) -> str:
    try:
        link_id = short_url.decode_url(code)
    except Exception:
        return ""
    link_obj = Link.query.filter_by(id=link_id).first()
    return link_obj.link if link_obj and link_obj.link else ""


def linkify_text(text) -> Markup:
    """
    Replace ``[LINK: CODE]`` markers with anchor tags pointing at the resolved
    URL. Everything else is HTML-escaped and then run through ``bleach`` so
    hostile input from an email body cannot smuggle scripts into the page.
    """
    if text is None:
        return Markup("")

    escaped = str(escape(str(text)))

    def _replace(match: re.Match) -> str:
        preceding_word = match.group(1) or ""
        separator = match.group(2) or ""
        code = match.group(3)
        real_link = _resolve(code)
        if not real_link:
            return preceding_word + separator

        href = escape(real_link)
        anchor_text = preceding_word if preceding_word else "link"
        return (
            f'{separator if preceding_word else ""}'
            f'<a href="{href}" target="_blank" rel="noopener noreferrer">'
            f'{anchor_text}</a>'
        )

    replaced = _LINK_PATTERN.sub(_replace, escaped)

    cleaned = bleach.clean(
        replaced,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )
    return Markup(cleaned)
