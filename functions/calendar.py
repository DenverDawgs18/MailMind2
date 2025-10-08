from app import current_user
import requests
from refresh_token import refresh
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from flask import jsonify

def create_google_calendar_event(start_dt, end_dt, name):
    """Create event in Google Calendar"""
    try:
        access_token = refresh(current_user)
        creds = Credentials(token=access_token)
        service = build("calendar", "v3", credentials=creds)
        
        # Create event
        event = {
            "summary": name,
            "start": {
                'dateTime': start_dt.isoformat()
            },
            "end": {
                "dateTime": end_dt.isoformat()
            }
        }
        
        # Insert event into calendar
        event_result = service.events().insert(calendarId='primary', body=event).execute()
        
        return jsonify({
            "success": True, 
            "event_id": event_result.get('id'),
            "provider": "google"
        })
        
    except Exception as e:
        print(f"Google Calendar API error: {str(e)}")
        return jsonify({"success": False, "error": "Failed to create Google calendar event"})

def create_microsoft_calendar_event(start_dt, end_dt, name):
    """Create event in Microsoft Calendar (Outlook)"""
    try:
        access_token = refresh(current_user)  # Assuming you have a Microsoft refresh function
        
        # Microsoft Graph API endpoint
        url = "https://graph.microsoft.com/v1.0/me/events"
        
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        
        # Create event payload for Microsoft Graph
        event_data = {
            "subject": name,
            "start": {
                "dateTime": start_dt.isoformat(),
                "timeZone": str(start_dt.tzinfo) if start_dt.tzinfo else "UTC"
            },
            "end": {
                "dateTime": end_dt.isoformat(),
                "timeZone": str(end_dt.tzinfo) if end_dt.tzinfo else "UTC"
            }
        }
        
        response = requests.post(url, json=event_data, headers=headers)
        
        if response.status_code == 201:
            event_result = response.json()
            return jsonify({
                "success": True, 
                "event_id": event_result.get('id'),
                "provider": "microsoft"
            })
        else:
            print(f"Microsoft Graph API error: {response.status_code} - {response.text}")
            return jsonify({
                "success": False, 
                "error": f"Microsoft API error: {response.status_code}"
            })
            
    except Exception as e:
        print(f"Microsoft Calendar API error: {str(e)}")
        return jsonify({"success": False, "error": "Failed to create Microsoft calendar event"})