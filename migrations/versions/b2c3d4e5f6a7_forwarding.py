"""email forwarding: sign-in identities, forwarding addresses, inbound queue

Revision ID: b2c3d4e5f6a7
Revises: 9e8f7a6b5c4d
Create Date: 2026-09-28 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'b2c3d4e5f6a7'
down_revision = '9e8f7a6b5c4d'
branch_labels = None
depends_on = None


def _master_fk():
    return sa.ForeignKeyConstraint(['master_id'], ['master.id'], ondelete='CASCADE')


def upgrade():
    op.create_table(
        'identity',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('subject', sa.String(length=255), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('master_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        _master_fk(),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider', 'subject', name='uq_identity_provider_subject'),
    )
    op.create_index('ix_identity_master_id', 'identity', ['master_id'])

    op.create_table(
        'forwarding_address',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('master_id', sa.Integer(), nullable=False),
        sa.Column('token', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_received_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('confirmation_code', sa.String(length=32), nullable=True),
        sa.Column('confirmation_for', sa.String(length=255), nullable=True),
        sa.Column('confirmation_at', sa.DateTime(timezone=True), nullable=True),
        _master_fk(),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('master_id'),
    )
    op.create_index('ix_forwarding_address_token', 'forwarding_address', ['token'], unique=True)

    op.create_table(
        'inbound_email',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('master_id', sa.Integer(), nullable=False),
        sa.Column('source_email', sa.String(length=255), nullable=False),
        sa.Column('sender', sa.String(length=512), nullable=True),
        sa.Column('subject', sa.Text(), nullable=True),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('message_id', sa.String(length=998), nullable=True),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
        _master_fk(),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_inbound_email_master_id', 'inbound_email', ['master_id'])
    op.create_index('ix_inbound_email_message_id', 'inbound_email', ['message_id'])

    op.create_table(
        'pending_item',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('master_id', sa.Integer(), nullable=False),
        sa.Column('source_email', sa.String(length=255), nullable=False),
        sa.Column('action', sa.Text(), nullable=False),
        sa.Column('sender', sa.String(length=512), nullable=True),
        sa.Column('subject', sa.Text(), nullable=True),
        sa.Column('calendar_url', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        _master_fk(),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_pending_item_master_id', 'pending_item', ['master_id'])


def downgrade():
    for table, indexes in (
        ('pending_item', ['ix_pending_item_master_id']),
        ('inbound_email', ['ix_inbound_email_message_id', 'ix_inbound_email_master_id']),
        ('forwarding_address', ['ix_forwarding_address_token']),
        ('identity', ['ix_identity_master_id']),
    ):
        for ix in indexes:
            op.drop_index(ix, table_name=table)
        op.drop_table(table)
