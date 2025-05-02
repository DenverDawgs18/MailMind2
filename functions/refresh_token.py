import requests 
from dotenv import load_dotenv
import os
from app import db 
load_dotenv()

def refresh(user):
    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "client_id" : os.getenv('CLIENT_ID'),
        "client_secret" : os.getenv('CLIENT_SECRET'),
        "refresh_token" : user.oauth_token,
        "grant_type": "refresh_token",
    }
    response = requests.post(token_url, data=data)
    if response.status_code == 200:
        new_tokens = response.json()
        user.oauth_token = new_tokens.get("refresh_token", user.oauth_token)
        db.session.commit()
        return new_tokens['access_token']
    return None