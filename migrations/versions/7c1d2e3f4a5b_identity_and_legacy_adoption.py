"""provider identities, reauth flag, and adoption of the legacy schema

Production's database was created by the pre-Sprint-4 app with db.create_all():
``master`` has username/password and no primary_email, and ``email_account``
has no provider identity columns. This revision brings either a legacy or a
fresh database to the current model without dropping anything:

- master: add primary_email (backfilled from the account's first linked inbox)
  and created_at; default null subscribed/temp to false.
- email_account: add provider_subject, needs_reauth, created_at; widen email
  to 255; default a null provider to "google" (the old app's default).

Legacy-only tables (link, unsubscribe, todo) and columns (username, password,
high_priority) are left in place and simply unused.

Revision ID: 7c1d2e3f4a5b
Revises: 34345d809b8a
Create Date: 2026-09-28 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '7c1d2e3f4a5b'
down_revision = '34345d809b8a'
branch_labels = None
depends_on = None


def _columns(table):
    return {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table):
    return {i['name'] for i in sa.inspect(op.get_bind()).get_indexes(table)}


def _uniques(table):
    return {u['name'] for u in sa.inspect(op.get_bind()).get_unique_constraints(table)}


def _backfill_primary_emails(bind):
    master = sa.table('master', sa.column('id', sa.Integer), sa.column('primary_email', sa.String),
                      *( [sa.column('username', sa.Text)] if 'username' in _columns('master') else [] ))
    account = sa.table('email_account', sa.column('id', sa.Integer), sa.column('email', sa.String),
                       sa.column('master_id', sa.Integer))

    taken = {r[0].lower() for r in bind.execute(
        sa.select(master.c.primary_email).where(master.c.primary_email.isnot(None))) if r[0]}
    missing = bind.execute(sa.select(master).where(master.c.primary_email.is_(None))).mappings().all()

    for row in missing:
        first = bind.execute(
            sa.select(account.c.email).where(account.c.master_id == row['id']).order_by(account.c.id).limit(1)
        ).scalar()
        candidates = [first, row.get('username')]
        email = next((c for c in candidates if c and '@' in c and c.lower() not in taken), None)
        if email is None:
            # No usable address; this account can't sign in until it links an
            # inbox, but keeping the row preserves its billing/history.
            email = f"legacy-{row['id']}@users.mailmind.invalid"
        taken.add(email.lower())
        bind.execute(master.update().where(master.c.id == row['id']).values(primary_email=email))


def upgrade():
    bind = op.get_bind()

    # ---- master -----------------------------------------------------------
    cols = _columns('master')
    with op.batch_alter_table('master') as batch:
        if 'primary_email' not in cols:
            batch.add_column(sa.Column('primary_email', sa.String(length=255), nullable=True))
        if 'created_at' not in cols:
            batch.add_column(sa.Column('created_at', sa.DateTime(timezone=True), nullable=True))

    _backfill_primary_emails(bind)
    bind.execute(sa.text("UPDATE master SET subscribed = :f WHERE subscribed IS NULL"), {"f": False})
    bind.execute(sa.text("UPDATE master SET temp = :f WHERE temp IS NULL"), {"f": False})

    with op.batch_alter_table('master') as batch:
        batch.alter_column('primary_email', existing_type=sa.String(length=255), nullable=False)
        batch.alter_column('subscribed', existing_type=sa.Boolean(), nullable=False)
        batch.alter_column('temp', existing_type=sa.Boolean(), nullable=False)
        if 'ix_master_primary_email' not in _indexes('master'):
            batch.create_index('ix_master_primary_email', ['primary_email'], unique=True)

    # ---- email_account ----------------------------------------------------
    cols = _columns('email_account')
    bind.execute(sa.text("UPDATE email_account SET provider = 'google' WHERE provider IS NULL"))
    with op.batch_alter_table('email_account') as batch:
        batch.alter_column('email', existing_type=sa.String(), type_=sa.String(length=255),
                           existing_nullable=False)
        batch.alter_column('provider', existing_type=sa.Text(), type_=sa.String(length=32),
                           nullable=False)
        if 'provider_subject' not in cols:
            batch.add_column(sa.Column('provider_subject', sa.String(length=255), nullable=True))
        if 'needs_reauth' not in cols:
            batch.add_column(sa.Column('needs_reauth', sa.Boolean(), nullable=False,
                                       server_default=sa.false()))
        if 'created_at' not in cols:
            batch.add_column(sa.Column('created_at', sa.DateTime(timezone=True), nullable=True))
        if 'uq_email_account_identity' not in _uniques('email_account'):
            batch.create_unique_constraint('uq_email_account_identity',
                                           ['provider', 'provider_subject'])


def downgrade():
    with op.batch_alter_table('email_account') as batch:
        batch.drop_constraint('uq_email_account_identity', type_='unique')
        batch.drop_column('created_at')
        batch.drop_column('needs_reauth')
        batch.drop_column('provider_subject')
