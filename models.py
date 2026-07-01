from datetime import datetime, timezone

from flask_login import UserMixin
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.types import Text

from app import db


def _utcnow():
    return datetime.now(timezone.utc)


class Master(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password = db.Column(db.Text(), nullable=False)
    stripe_customer_id = db.Column(db.String(), unique=True, default=None)
    last_login = db.Column(
        db.DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
    )
    time = db.Column(db.Text())
    temp = db.Column(db.Boolean, default=False)
    subscribed = db.Column(db.Boolean, default=False, nullable=False)
    timezone = db.Column(db.Text())


class EmailAccount(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    oauth_token = db.Column(db.Text(), nullable=False)
    high_priority = db.Column(JSON, default=list)
    provider = db.Column(db.Text())
    master_id = db.Column(
        db.Integer, db.ForeignKey('master.id', ondelete='CASCADE'), nullable=False
    )
    master = db.relationship(
        'Master', backref=db.backref('email_accounts', cascade='all, delete-orphan')
    )

    def __repr__(self):
        return f"{self.email}"


class Link(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    link = db.Column(db.Text())
    short = db.Column(db.String(1000), nullable=True)


class Unsubscribe(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    link = db.Column(db.Text())
    user = db.Column(db.Integer, db.ForeignKey("master.id", ondelete="CASCADE"))
    sender = db.Column(db.String(1000))


class Todo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item = db.Column(db.Text())
    master = db.Column(db.Integer, db.ForeignKey("master.id", ondelete="CASCADE"))
    done = db.Column(db.Boolean, default=False, nullable=False)


class CachedEmail(db.Model):
    """
    Processed email cached per user/account so we can render summary pages
    without stuffing full email bodies into the session store.
    """
    __tablename__ = "cached_email"

    id = db.Column(db.Integer, primary_key=True)
    master_id = db.Column(
        db.Integer, db.ForeignKey("master.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id = db.Column(
        db.Integer, db.ForeignKey("email_account.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_email = db.Column(db.String(120))
    message_hash = db.Column(db.String(64), index=True)
    sender = db.Column(db.Text())
    subject = db.Column(db.Text())
    body = db.Column(db.Text())
    received_utc = db.Column(db.DateTime(timezone=True), index=True)
    action_items = db.Column(db.Text(), default="Generating ...")
    calendar = db.Column(db.Boolean, default=False)
    is_first_of_account = db.Column(db.Boolean, default=False)
    todo_id = db.Column(db.Integer, db.ForeignKey("todo.id", ondelete="SET NULL"), nullable=True)
    fetched_at = db.Column(db.DateTime(timezone=True), default=_utcnow, index=True)

    __table_args__ = (
        db.UniqueConstraint("master_id", "account_id", "message_hash", name="uq_cached_email"),
    )
