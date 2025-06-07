import requests 
import os
from app import db 
from functions.encryption import decrypt_token, encrypt_token
import logging 
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

from functions.production import production

PRODUCTION = production()

if not PRODUCTION:
    from dotenv import load_dotenv
    load_dotenv()
    
def refresh(user):
    refresh_token = decrypt_token(user.oauth_token)
    data = {
        "client_id": os.getenv('GOOGLE_CLIENT_ID'),
        "client_secret": os.getenv('GOOGLE_CLIENT_SECRET'),
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    response = requests.post("https://oauth2.googleapis.com/token", data=data)

    if response.status_code == 200:
        new_tokens = response.json()
        new_refresh_token = new_tokens.get("refresh_token")
        if new_refresh_token:
            user.oauth_token = new_refresh_token
        db.session.commit()
        return new_tokens['access_token']
    else:
        # Log error or take action
        logger.info(f"Refresh failed for {response.json()}")
        return None

