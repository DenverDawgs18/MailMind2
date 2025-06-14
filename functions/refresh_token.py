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

# Microsoft Graph OAuth endpoints
MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MICROSOFT_SCOPES = [
    'https://graph.microsoft.com/Mail.ReadWrite',
    'https://graph.microsoft.com/Mail.Send', 
    'https://graph.microsoft.com/User.Read',
    'offline_access',
    'openid',
    'profile',
    'email'
]

def refresh(user):
    """
    Refresh access token for both Google and Microsoft OAuth providers.
    Determines provider based on user.provider field or falls back to Google as default.
    """
    refresh_token = decrypt_token(user.oauth_token)
    
    # Determine provider - check if user has a provider field, otherwise default to Google
    provider = getattr(user, 'provider', 'google').lower()
    
    if provider == 'microsoft':
        return _refresh_microsoft_token(user, refresh_token)
    else:
        # Default to Google for backward compatibility
        return _refresh_google_token(user, refresh_token)

def _refresh_google_token(user, refresh_token):
    """Refresh Google OAuth token"""
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
        
        # Google sometimes doesn't return a new refresh token, keep the old one
        if new_refresh_token:
            user.oauth_token = encrypt_token(new_refresh_token)
            db.session.commit()
            logger.info(f"Updated Google refresh token for user {user.email}")
        
        logger.info(f"Google token refreshed successfully for user {user.email}")
        return new_tokens['access_token']
    else:
        logger.error(f"Google refresh failed for user {user.email}: {response.json()}")
        return None

def _refresh_microsoft_token(user, refresh_token):
    """Refresh Microsoft Graph OAuth token"""
    data = {
        'client_id': os.getenv("MICROSOFT_CLIENT_ID"),
        'client_secret': os.getenv("MICROSOFT_CLIENT_SECRET"),
        'refresh_token': refresh_token,
        'grant_type': 'refresh_token',
        'scope': ' '.join(MICROSOFT_SCOPES)
    }
    
    response = requests.post(
        MICROSOFT_TOKEN_URL,
        data=data,
        headers={'Content-Type': 'application/x-www-form-urlencoded'}
    )
    
    if response.status_code == 200:
        new_tokens = response.json()
        new_refresh_token = new_tokens.get("refresh_token")
        
        # Microsoft always returns a new refresh token
        if new_refresh_token:
            user.oauth_token = encrypt_token(new_refresh_token)
            db.session.commit()
            logger.info(f"Updated Microsoft refresh token for user {user.email}")
        
        logger.info(f"Microsoft token refreshed successfully for user {user.email}")
        return new_tokens['access_token']
    else:
        logger.error(f"Microsoft refresh failed for user {user.email}: {response.json()}")
        return None

def get_valid_access_token(user):
    """
    Helper function to get a valid access token, refreshing if necessary.
    This is useful when you need to make API calls and want to ensure the token is valid.
    """
    # First try to get a fresh token
    access_token = refresh(user)
    
    if not access_token:
        logger.error(f"Failed to refresh token for user {user.email}")
        return None
    
    return access_token