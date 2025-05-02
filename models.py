from app import db 
from datetime import datetime, timezone 
from flask_login import UserMixin
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.types import Text

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key = True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    oauth_token = db.Column(db.String(255), nullable=True)
    last_login = db.Column(db.DateTime, default=datetime.now(timezone.utc), onupdate=datetime.now(timezone.utc))
    high_priority = db.Column(JSON, default = list)
    def __repr__(self):
        return f"<User {self.email}>"
    
    
class Link(db.Model):
    id = db.Column(db.Integer, primary_key = True)
    link = db.Column(db.String(1000))
    short = db.Column(db.String(1000), nullable = True)

class Unsubscribe(db.Model):
    id = db.Column(db.Integer, primary_key = True)
    link = db.Column(db.String(1000))
    user = db.Column(db.ForeignKey("user.id"))
    sender = db.Column(db.String(1000))