import re
import short_url
from models import Link

def linkify_text(text):
    """
    Finds patterns like "word [LINK: CODE]" in the text,
    looks up the real URL using CODE, and replaces the pattern
    with an HTML anchor tag linking to the real URL, using the word as its text.
    """
    pattern = r'(\S+)\s*\[LINK:\s*(\w+)\]'

    
    def replacer(match):
        anchor_text = match.group(1)
        code = match.group(2)
        try:
            # Convert the short code back to the database id.
            link_id = short_url.decode_url(code)
            # Look up the corresponding Link object.
            link_obj = Link.query.filter_by(id=link_id).first()
            if link_obj:
                real_link = link_obj.link
                # Return an anchor tag using the anchor text.
                return f'<a href="{real_link}" target="_blank">{anchor_text}</a>'
        except Exception:
            pass
        # If lookup fails, return the original matched text unchanged.
        return match.group(0)
    
    # Return the replaced string (to be marked safe in the template).
    return re.sub(pattern, replacer, text)
