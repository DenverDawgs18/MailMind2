import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.executors.pool import ThreadPoolExecutor
from flask import current_app
from sqlalchemy import and_

from app import db 
from models import User, Unsubscribe, Link
import re
from functions.refresh_token import refresh 
from functions.get_emails import get_emails 
from functions.get_one_action import get_an_action 
from functions.reply import reply
from functions.linkify import linkify_text
import short_url 


logger = logging.getLogger(__name__)

# Global scheduler instance
scheduler = None
# Store Flask app reference for context
flask_app = None

def generate_email_html(action_items, user_email):
    """Generate rich HTML email content"""
    
    # HTML email template matching your summary page style
    html_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
    <meta charset="UTF-8" />
    <title>MailMind Daily Summary</title>
    <style>
        body {
        background: #171524;
        color: #f0f0f0;
        font-family: Inter, system-ui, sans-serif;
        margin: 0;
        padding: 20px;
        }
        .container {
        max-width: 600px;
        margin: 0 auto;
        background: linear-gradient(145deg, #514b7a, #5a5488);
        border: 2px solid black;
        border-radius: 20px;
        padding: 30px;
        box-shadow: 4px 4px 10px rgba(0, 0, 0, 0.4);
        }
        h1 {
        text-align: center;
        font-size: 24px;
        color: #e6d7a3;
        margin-bottom: 10px;
        }
        .top-link {
        text-align: center;
        margin-bottom: 20px;
        }
        .top-link a {
        color: #cc8400;
        font-weight: bold;
        text-decoration: none;
        font-size: 14px;
        }
        .summary-info {
        text-align: center;
        font-size: 14px;
        margin-bottom: 30px;
        color: #f0f0f0;
        }
        .item {
        background: #3a3560;
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-left: 4px solid #cc8400;
        border-radius: 12px;
        padding: 15px 20px;
        margin-bottom: 20px;
        }
        .item p {
        margin: 4px 0;
        font-size: 14px;
        }
        .item .action {
        color: #e6d7a3;
        font-weight: 600;
        }
        .footer {
        text-align: center;
        font-size: 12px;
        color: #ccc;
        margin-top: 30px;
        }
        .footer a {
        color: #e6d7a3;
        text-decoration: none;
        }
        .footer a:hover {
        color: #cc8400;
        }
    </style>
    </head>
    <body>
    <div class="container">
        <h1>📬 Your Daily Action Summary</h1>

        <div class="top-link">
        <a href="https://mailmind.fly.dev/summary">View Full List →</a>
        </div>

        <div class="summary-info">
        <p>{{ action_count }} action items • {{ current_date }}</p>
        <p>{{ user_email }}</p>
        </div>

        {% for email in action_items%}
        <div class="item">
        <p class="action">• {{ email.action_items | linkify_text }}</p>
        <p><strong>From:</strong> {{ email.from }}</p>
        <p><strong>Subject:</strong> {{ email.subject }}</p>
        </div>
        {% endfor %}

        <div class="footer">
        <p>
            Sent by <a href="https://mailmind.fly.dev">MailMind</a> • 
            <a href="mailto:pautomas55@gmail.com">Support</a> • 
        </p>
        </div>
    </div>
    </body>
    </html>

    """
    
    # Render the template with data
    from jinja2 import Template, Environment
    
    # Create Jinja2 environment with custom filter
    env = Environment()
    
    # Register the linkify_text filter (assuming it's available in your app context)
    # If you need to import it, add: from your_module import linkify_text
    env.filters['linkify_text'] = linkify_text
    
    template = env.from_string(html_template)
    
    return template.render(
        action_items=action_items,
        user_email=user_email,
        current_date=datetime.now().strftime('%B %d, %Y'),
        action_count=len(action_items)
    )

def is_calendar_worthy(action_text):
    """Check if action item is calendar-worthy"""
    calendar_keywords = ["meeting", "conference call", "calendar", "appointment", "call", "schedule", "event"]
    return any(keyword in action_text.lower() for keyword in calendar_keywords)

def send_reply_email_for_user(action_items, user):
    """Send email using the reply function"""
    try:
        # Generate HTML email content
        html_content = generate_email_html(action_items, user.email)
        
        # Prepare email data
        subject = f"Daily To Do List from MailMind for {datetime.now().strftime('%B %d, %Y')}"
        
        # Send email via reply function as HTML
        result = reply(
            user_email=user.email,
            oauth_token=refresh(user),
            to_email=user.email,
            subject=subject,
            body=html_content,
            reply=False,
            cc=None,
            bcc=None,
            provider=user.provider,  # Pass the provider from user object
            content_type='html'
        )
        
        if result:  # Assuming reply function returns True on success
            logger.info(f"Email sent successfully to {user.email}")
            return True
        else:
            logger.error(f"Failed to send email using reply function")
            return False
            
    except Exception as e:
        logger.error(f"Error sending email via reply function: {str(e)}")
        return False
    
def process_unsubscribe_links(emails, user):
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
                            new_unsub = Unsubscribe(sender=email['from'], link=real_link, user=user.id)
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

def init_scheduler(app):
    """Initialize the email scheduler"""
    global scheduler, flask_app
    
    # Store Flask app reference for context
    flask_app = app
    
    # Configure scheduler
    executors = {
        'default': ThreadPoolExecutor(max_workers=5)
    }
    
    job_defaults = {
        'coalesce': False,
        'max_instances': 3,
        'misfire_grace_time': 300  # 5 minutes
    }
    
    scheduler = BackgroundScheduler(
        executors=executors,
        job_defaults=job_defaults,
        timezone='UTC'
    )
    
    # Add the recurring job that runs every 15 minutes
    scheduler.add_job(
        func=check_and_send_emails,
        trigger=IntervalTrigger(minutes=15),
        id='email_summary_checker',
        name='Check for users to send email summaries',
        replace_existing=True
    )
    
    # Start the scheduler
    scheduler.start()
    logger.info("Email scheduler started")
    
    # Register shutdown handler
    import atexit
    atexit.register(shutdown_scheduler)
    
    return scheduler

def shutdown_scheduler():
    """Shutdown the scheduler"""
    global scheduler
    if scheduler:
        scheduler.shutdown()
        logger.info("Email scheduler stopped")

def parse_user_times(time_string: str) -> List[str]:
    """Parse comma-separated times from user.time field"""
    if not time_string:
        return []
    
    times = []
    for time_str in time_string.split(','):
        time_str = time_str.strip()
        if time_str:
            times.append(time_str)
    return times

def convert_to_24hour(time_str: str) -> str:
    """Convert 12-hour format to 24-hour format"""
    try:
        # Parse the time string
        time_obj = datetime.strptime(time_str, '%I:%M %p').time()
        return time_obj.strftime('%H:%M')
    except ValueError:
        try:
            # Try without space before AM/PM
            time_obj = datetime.strptime(time_str, '%I:%M%p').time()
            return time_obj.strftime('%H:%M')
        except ValueError:
            logger.error(f"Could not parse time string: {time_str}")
            return None

def is_time_match(user_time: str, current_time: datetime, timezone_str: str) -> bool:
    """Check if current time matches user's scheduled time within 15-minute window"""
    try:
        # Get user's timezone
        user_timezone = pytz.timezone(timezone_str)
        
        # Convert current UTC time to user's timezone
        current_local = current_time.astimezone(user_timezone)
        
        # Convert user's time to 24-hour format
        user_time_24 = convert_to_24hour(user_time)
        if not user_time_24:
            return False
        
        # Parse user's time
        user_hour, user_minute = map(int, user_time_24.split(':'))
        
        # Create target time for today in user's timezone
        target_time = current_local.replace(
            hour=user_hour,
            minute=user_minute,
            second=0,
            microsecond=0
        )
        
        # Check if current time is within 15 minutes of target time
        time_diff = abs((current_local - target_time).total_seconds())
        
        # 15 minutes = 900 seconds
        # We use a slightly larger window to account for scheduler timing variations
        return time_diff <= 900
        
    except Exception as e:
        logger.error(f"Error checking time match for user time {user_time}, timezone {timezone_str}: {e}")
        return False

def get_users_to_process() -> List[User]:
    """Get users who should receive email summaries at current time"""
    current_time = datetime.now(pytz.UTC)
    users_to_process = []
    
    try:
        # Get all users with valid timezone and time settings
        users = User.query.filter(
            and_(
                User.timezone.isnot(None),
                User.time.isnot(None),
                User.email.isnot(None)
            )
        ).all()
        
        for user in users:
            if not user.timezone or not user.time:
                continue
                
            user_times = parse_user_times(user.time)
            
            for user_time in user_times:
                if is_time_match(user_time, current_time, user.timezone):
                    users_to_process.append(user)
                    logger.info(f"User {user.email} scheduled for email summary at {user_time} ({user.timezone})")
                    break  # Only add user once even if multiple times match
                    
    except Exception as e:
        logger.error(f"Error getting users to process: {e}")
        
    return users_to_process

def send_email_summary_for_user(user: User) -> Dict[str, Any]:
    """Send email summary for a specific user (extracted from your /mail route)"""
    try:
        # Get user's access token
        access_token = refresh(user)
        
        # Get emails from last 24 hours
        emails = get_emails(user.provider, user.email, access_token)
        
        if not emails:
            return {
                "success": False,
                "message": "No emails found in the last 24 hours",
                "user": user.email
            }
        
        # Process emails for action items
        emails = list(reversed(emails))  # Newest first
        process_unsubscribe_links(emails, user)
        
        # Generate action items for all emails
        action_items = []
        for email in emails:
            try:
                body = email.get("body", "")
                if body:
                    action = get_an_action(body)
                    if action and action.lower() not in ["no action.", "no action", "no action required.", ""]:
                        email["action_items"] = action
                        email["calendar"] = is_calendar_worthy(action)
                        action_items.append(email)
                    else:
                        email["action_items"] = "No action"
                        email["calendar"] = False
                else:
                    email["action_items"] = "No action"
                    email["calendar"] = False
            except Exception as e:
                logger.error(f"Error generating action for email: {e}")
                email["action_items"] = "Error generating action"
                email["calendar"] = False
        
        # Send email summary
        if action_items:
            email_sent = send_reply_email_for_user(action_items, user)
            if email_sent:
                return {
                    "success": True,
                    "message": f"Email summary sent with {len(action_items)} action items",
                    "action_count": len(action_items),
                    "user": user.email
                }
            else:
                return {
                    "success": False,
                    "message": "Failed to send email summary",
                    "user": user.email
                }
        else:
            return {
                "success": True,
                "message": "No action items found to send",
                "action_count": 0,
                "user": user.email
            }
            
    except Exception as e:
        logger.error(f"Error in send_email_summary for user {user.email}: {str(e)}")
        return {
            "success": False,
            "message": "An error occurred while processing email summary",
            "user": user.email
        }

def send_reply_email_for_user(action_items: List[Dict], user: User) -> bool:
    """Send email using the reply function for a specific user"""
    try:
        # Generate HTML email content
        html_content = generate_email_html(action_items, user.email)
        
        # Prepare email data
        subject = f"Daily To Do List from MailMind for {datetime.now().strftime('%B %d, %Y')}"
        
        # Send email via reply function as HTML
        result = reply(
            user_email=user.email,
            oauth_token=refresh(user),
            to_email=user.email,
            subject=subject,
            body=html_content,
            reply=False,
            cc=None,
            bcc=None,
            provider=user.provider,
            content_type='html'
        )
        
        if result:
            logger.info(f"Scheduled email sent successfully to {user.email}")
            return True
        else:
            logger.error(f"Failed to send scheduled email to {user.email}")
            return False
            
    except Exception as e:
        logger.error(f"Error sending scheduled email to {user.email}: {str(e)}")
        return False

def check_and_send_emails():
    """Main function that runs every 15 minutes to check and send emails"""
    global flask_app
    
    logger.info("Checking for users to send email summaries...")
    
    # Use the stored Flask app reference for context
    if not flask_app:
        logger.error("Flask app not available for scheduler context")
        return
    
    with flask_app.app_context():
        try:
            users_to_process = get_users_to_process()
            
            if not users_to_process:
                logger.info("No users scheduled for email summaries at this time")
                return
            
            logger.info(f"Processing {len(users_to_process)} users for email summaries")
            
            # Process each user
            for user in users_to_process:
                try:
                    result = send_email_summary_for_user(user)
                    if result["success"]:
                        logger.info(f"Successfully sent email summary to {user.email}: {result['message']}")
                    else:
                        logger.warning(f"Failed to send email summary to {user.email}: {result['message']}")
                except Exception as e:
                    logger.error(f"Error processing user {user.email}: {str(e)}")
                    
        except Exception as e:
            logger.error(f"Error in check_and_send_emails: {str(e)}")

# Helper functions for manual testing/admin
def trigger_email_check():
    """Manually trigger email check (for testing/admin purposes)"""
    check_and_send_emails()

def get_scheduler_status():
    """Get scheduler status"""
    global scheduler
    if scheduler:
        jobs = scheduler.get_jobs()
        return {
            "scheduler_running": scheduler.running,
            "jobs": [{"id": job.id, "name": job.name, "next_run": str(job.next_run_time)} for job in jobs]
        }
    return {"scheduler_running": False, "jobs": []}