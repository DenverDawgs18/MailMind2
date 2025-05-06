import re
import short_url
from models import Link

def linkify_text(text):
    """
    Finds patterns like "[LINK: CODE]" in the text,
    looks up the real URL using CODE, and creates an HTML anchor tag
    that links the preceding word to the real URL, removing all bracket notation.
    """
    # This pattern looks for any text followed by a link pattern
    # The pattern matches:
    # - Group 1: Any text before the link pattern (may include brackets, etc.)
    # - Group 2: The link code between [LINK: and ]
    pattern = r'(.*?)\[LINK:\s*(\w+)\]'
    
    result = text
    for match in re.finditer(pattern, text):
        full_match = match.group(0)
        before_link = match.group(1)
        code = match.group(2)
        
        try:
            # Convert the short code to database id
            link_id = short_url.decode_url(code)
            # Look up the corresponding Link object
            link_obj = Link.query.filter_by(id=link_id).first()
            
            if link_obj and link_obj.link:
                real_link = link_obj.link
                
                # Find the last word in the preceding text (ignoring brackets, etc.)
                word_pattern = r'(\w+)[^\w]*$'
                word_match = re.search(word_pattern, before_link)
                
                if word_match:
                    anchor_text = word_match.group(1)
                    
                    # Calculate how much of the before_link to keep
                    # (everything up to the start of the last word)
                    word_start_pos = word_match.start(1)
                    text_to_keep = before_link[:word_start_pos]
                    
                    # Create the replacement: text_to_keep + linked word
                    replacement = f'{text_to_keep}<a href="{real_link}" target="_blank">{anchor_text}</a>'
                    
                    # Replace the entire match with our new content
                    result = result.replace(full_match, replacement)
                else:
                    # If no suitable word found, just remove the link marker
                    result = result.replace(full_match, before_link)
        except Exception as e:
            # If lookup fails, just remove the link marker
            result = result.replace(full_match, before_link)
    
    # Clean up any remaining brackets and angle brackets
    result = re.sub(r'\[\]|\(\)|\<\>|\[\s*\]|\(\s*\)|\<\s*\>', '', result)
    
    # Remove non-alphanumeric characters except for HTML tags, spaces, and punctuation needed for readability
    # First protect HTML tags by replacing them temporarily
    html_tags = []
    def protect_html(match):
        html_tags.append(match.group(0))
        return f"__HTML_TAG_{len(html_tags)-1}__"
    
    # Protect HTML tags
    protected_text = re.sub(r'<a\s+href="[^"]*"\s+target="_blank">[^<]*</a>', protect_html, result)
    
    # Remove unwanted characters but keep spaces, common punctuation, and newlines
    cleaned_text = re.sub(r'[^\w\s.,!?:;\-\'"/\n]', '', protected_text)
    
    # Restore HTML tags
    for i, tag in enumerate(html_tags):
        cleaned_text = cleaned_text.replace(f"__HTML_TAG_{i}__", tag)
    
    result = cleaned_text
    
    return result