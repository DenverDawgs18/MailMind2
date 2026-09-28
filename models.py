from datetime import datetime, timezone

from flask_login import UserMixin

from app import db


def _utcnow():
    return datetime.now(timezone.utc)


class Master(db.Model, UserMixin):
    """
    A MailMind account. There is no username/password — the account is keyed
    by the primary email verified through Google or Microsoft OAuth. More email
    accounts get attached through the same OAuth flows.
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
    """
    One linked inbox. ``provider_subject`` is the provider's immutable user id
    (Google ``sub`` / Microsoft object id) and is what identities are matched
    on — never the email address, which some providers let tenants rewrite.
    """

    __table_args__ = (
        db.UniqueConstraint('provider', 'provider_subject', name='uq_email_account_identity'),
    )

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    oauth_token = db.Column(db.Text(), nullable=False)  # Fernet-encrypted refresh token
    provider = db.Column(db.String(32), nullable=False)  # "google" or "microsoft"
    provider_subject = db.Column(db.String(255), nullable=True)
    # Set when the refresh token stops working (revoked, expired, undecryptable);
    # the user is asked to reconnect and the scheduler skips the inbox.
    needs_reauth = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)
    master_id = db.Column(
        db.Integer, db.ForeignKey('master.id', ondelete='CASCADE'), nullable=False
    )
    master = db.relationship(
        'Master', backref=db.backref('email_accounts', cascade='all, delete-orphan',
                                     order_by='EmailAccount.id')
    )

    def __repr__(self):
        return self.email


class Digest(db.Model):
    """One generated list: what the scheduler found for a user at one delivery time."""

    id = db.Column(db.Integer, primary_key=True)
    master_id = db.Column(
        db.Integer, db.ForeignKey('master.id', ondelete='CASCADE'), nullable=False, index=True
    )
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
    # Whether the digest email actually went out (the web list is stored either way).
    delivered = db.Column(db.Boolean, default=False, nullable=False)
    master = db.relationship(
        'Master', backref=db.backref('digests', cascade='all, delete-orphan')
    )
    items = db.relationship(
        'DigestItem', backref='digest', cascade='all, delete-orphan',
        order_by='DigestItem.id',
    )


class DigestItem(db.Model):
    """
    One action item. Only the extracted action and the sender/subject are kept
    (never the email body), and digests are purged after DIGEST_RETENTION_DAYS.
    """

    id = db.Column(db.Integer, primary_key=True)
    digest_id = db.Column(
        db.Integer, db.ForeignKey('digest.id', ondelete='CASCADE'), nullable=False, index=True
    )
    account_email = db.Column(db.String(255), nullable=False)
    action = db.Column(db.Text(), nullable=False)
    sender = db.Column(db.String(512))
    subject = db.Column(db.Text())
    calendar_url = db.Column(db.Text())
    done = db.Column(db.Boolean, default=False, nullable=False)
    done_at = db.Column(db.DateTime(timezone=True))


DIGEST_RETENTION_DAYS = 14
