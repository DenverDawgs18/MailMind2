import re
from models import Link, Unsubscribe
from app import db
import short_url
def process_unsubscribe_links(emails, current_user):
    """Helper function to process unsubscribe links in emails"""
    changes_made = False
    
    for email in emails:
        unsubscribe_match = re.search(r"(?i)unsubscribe", email['body'])
                 
        if unsubscribe_match:
            unsubscribe_pos = unsubscribe_match.start()
            remaining_text = email['body'][unsubscribe_pos:]
            link_match = re.search(r"\[LINK:\s*([^\]]+)\]", remaining_text)
                         
            if link_match:
                code = link_match.group(1)
                try:
                    link_id = short_url.decode_url(code)
                    link_obj = Link.query.filter_by(id=link_id).first()
                                         
                    if link_obj and link_obj.link:
                        real_link = link_obj.link
                        unsubj = Unsubscribe.query.filter_by(sender=email["from"]).first()
                                                 
                        if not unsubj:
                            new_unsub = Unsubscribe(sender=email['from'], link=real_link, user=current_user.id)
                            db.session.add(new_unsub)
                            print(f"Added unsubscribe link for {email['from']}: {real_link}")
                            changes_made = True
                except Exception as e:
                    print(f"Error processing unsubscribe link: {str(e)}")
                    continue
    
    # Only commit if there were changes made
    if changes_made:
        try:
            db.session.commit()
            print("Successfully committed all unsubscribe link changes")
        except Exception as e:
            print(f"Error committing unsubscribe link changes: {str(e)}")
            db.session.rollback()
