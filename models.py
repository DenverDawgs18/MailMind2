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
    # Set only by Stripe webhooks: an active or trialing subscription.
    subscribed = db.Column(db.Boolean, default=False, nullable=False)
    # Legacy beta flag; access granted by codes now lives in comp_until.
    temp = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)
    # Free access from an invite code (or granted by hand) lasts until this
    # moment; FOREVER for no end date. Independent of Stripe.
    comp_until = db.Column(db.DateTime(timezone=True))
    access_code_id = db.Column(
        db.Integer, db.ForeignKey('access_code.id', ondelete='SET NULL'), index=True
    )
    access_code = db.relationship('AccessCode', backref=db.backref('redeemed_by', order_by='Master.id'))

    @property
    def comped(self) -> bool:
        if self.comp_until is None:
            return False
        until = self.comp_until if self.comp_until.tzinfo else self.comp_until.replace(tzinfo=timezone.utc)
        return until > _utcnow()

    @property
    def comped_forever(self) -> bool:
        return self.comped and self.comp_until.year >= FOREVER.year

    @property
    def has_access(self) -> bool:
        return bool(self.subscribed or self.comped)


# "No end date" for comped access.
FOREVER = datetime(9999, 12, 31, tzinfo=timezone.utc)


class AccessCode(db.Model):
    """
    An invite code the owner hands out for free access. Codes can be limited
    to a number of uses, stop working after a date, grant access for a fixed
    number of days (or forever), and be switched off at any time.
    """

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False, index=True)  # stored upper-case
    note = db.Column(db.String(255))            # who it's for, e.g. "Sam from the climbing gym"
    max_uses = db.Column(db.Integer)            # None = unlimited
    uses = db.Column(db.Integer, default=0, nullable=False)
    access_days = db.Column(db.Integer)         # None = forever
    expires_at = db.Column(db.DateTime(timezone=True))  # code stops working after this
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)

    def unusable_reason(self):
        """None if the code can be redeemed now, otherwise why not."""
        if not self.active:
            return "That code has been switched off."
        if self.expires_at is not None:
            exp = self.expires_at if self.expires_at.tzinfo else self.expires_at.replace(tzinfo=timezone.utc)
            if exp <= _utcnow():
                return "That code has expired."
        if self.max_uses is not None and self.uses >= self.max_uses:
            return "That code has already been used."
        return None


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


class Identity(db.Model):
    """
    A sign-in identity (provider + immutable subject id) that isn't tied to
    mailbox access. Lets people sign in with Google's basic scopes and feed
    MailMind by forwarding, without granting Gmail access.
    """

    __table_args__ = (db.UniqueConstraint('provider', 'subject', name='uq_identity_provider_subject'),)

    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(32), nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    master_id = db.Column(
        db.Integer, db.ForeignKey('master.id', ondelete='CASCADE'), nullable=False, index=True
    )
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)
    master = db.relationship('Master', backref=db.backref('identities', cascade='all, delete-orphan'))


class ForwardingAddress(db.Model):
    """The private address a user forwards mail to. One per account."""

    id = db.Column(db.Integer, primary_key=True)
    master_id = db.Column(
        db.Integer, db.ForeignKey('master.id', ondelete='CASCADE'), nullable=False, unique=True
    )
    token = db.Column(db.String(64), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)
    last_received_at = db.Column(db.DateTime(timezone=True))
    # Gmail asks the new forwarding target to confirm with a code; we catch it
    # and show it to the user in settings.
    confirmation_code = db.Column(db.String(32))
    confirmation_for = db.Column(db.String(255))
    confirmation_at = db.Column(db.DateTime(timezone=True))
    master = db.relationship(
        'Master', backref=db.backref('forwarding', uselist=False, cascade='all, delete-orphan')
    )


class InboundEmail(db.Model):
    """
    A forwarded email waiting to be read. Rows live only until the next
    scheduler tick (every 15 minutes) extracts action items, then are deleted.
    """

    id = db.Column(db.Integer, primary_key=True)
    master_id = db.Column(
        db.Integer, db.ForeignKey('master.id', ondelete='CASCADE'), nullable=False, index=True
    )
    source_email = db.Column(db.String(255), nullable=False)
    sender = db.Column(db.String(512))
    subject = db.Column(db.Text())
    body = db.Column(db.Text(), nullable=False)
    message_id = db.Column(db.String(998), index=True)
    received_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)
    master = db.relationship('Master', backref=db.backref('inbound_emails', cascade='all, delete-orphan'))


class PendingItem(db.Model):
    """An action item pulled from forwarded mail, waiting for the next list."""

    id = db.Column(db.Integer, primary_key=True)
    master_id = db.Column(
        db.Integer, db.ForeignKey('master.id', ondelete='CASCADE'), nullable=False, index=True
    )
    source_email = db.Column(db.String(255), nullable=False)
    action = db.Column(db.Text(), nullable=False)
    sender = db.Column(db.String(512))
    subject = db.Column(db.Text())
    calendar_url = db.Column(db.Text())
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)
    master = db.relationship('Master', backref=db.backref('pending_items', cascade='all, delete-orphan'))


# Forwarded mail that can't be processed (e.g. the model is down) is dropped
# after this long rather than kept indefinitely.
INBOUND_MAX_AGE_HOURS = 24
