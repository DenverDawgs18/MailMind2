from datetime import datetime, timezone

from flask_login import UserMixin

from app import db


def _utcnow():
    return datetime.now(timezone.utc)


class Master(db.Model, UserMixin):
    """
    A MailMind account. There is no username/password — the account IS the
    primary email verified by Google or Microsoft OAuth. Additional email
    accounts get attached via the same OAuth flows.
    """

    id = db.Column(db.Integer, primary_key=True)
    primary_email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    stripe_customer_id = db.Column(db.String(), unique=True, default=None)
    last_login = db.Column(
        db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    time = db.Column(db.Text())
    timezone = db.Column(db.Text())
    subscribed = db.Column(db.Boolean, default=False, nullable=False)
    # Whether this account was comped via TEMP_CODE during beta.
    temp = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)


class EmailAccount(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    oauth_token = db.Column(db.Text(), nullable=False)  # encrypted refresh token
    provider = db.Column(db.String(32), nullable=False)  # "google" or "microsoft"
    master_id = db.Column(
        db.Integer, db.ForeignKey('master.id', ondelete='CASCADE'), nullable=False
    )
    master = db.relationship(
        'Master', backref=db.backref('email_accounts', cascade='all, delete-orphan')
    )

    def __repr__(self):
        return self.email
