from app import db 
from datetime import datetime, timezone 
from flask_login import UserMixin
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.types import Text

class Master(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key = True)
    username = db.Column(db.Text())
    password = db.Column(db.Text())
    stripe_customer_id = db.Column(db.String(), unique=True, default = None)
    last_login = db.Column(db.DateTime, default=datetime.now(timezone.utc), onupdate=datetime.now(timezone.utc))
    time = db.Column(db.Text())
    temp = db.Column(db.Boolean, default=False)
    subscribed = db.Column(db.Boolean)
    timezone = db.Column(db.Text())


class EmailAccount(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    oauth_token = db.Column(db.Text(), nullable=False)
    high_priority = db.Column(JSON, default=list)
    provider = db.Column(db.Text())
    master_id = db.Column(db.Integer, db.ForeignKey('master.id', ondelete='CASCADE'), nullable=False)
    master = db.relationship('Master', backref=db.backref('email_accounts', cascade='all, delete-orphan'))

    def __repr__(self):
        return f"{self.email}"

    
class Link(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    link = db.Column(db.Text())  
    short = db.Column(db.String(1000), nullable=True)

class Unsubscribe(db.Model):
    id = db.Column(db.Integer, primary_key = True)
    link = db.Column(db.Text())
    user = db.Column(db.ForeignKey("master.id"))
    sender = db.Column(db.String(1000))

class Todo(db.Model):
    id = db.Column(db.Integer, primary_key = True)
    item = db.Column(db.Text())
    master = db.Column(db.ForeignKey("master.id"))
    done = db.Column(db.Boolean, default=False)
    