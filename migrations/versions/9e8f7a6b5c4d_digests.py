"""store generated lists so they can be viewed on the website

Revision ID: 9e8f7a6b5c4d
Revises: 7c1d2e3f4a5b
Create Date: 2026-09-28 18:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '9e8f7a6b5c4d'
down_revision = '7c1d2e3f4a5b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'digest',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('master_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('delivered', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(['master_id'], ['master.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_digest_master_id', 'digest', ['master_id'])
    op.create_index('ix_digest_created_at', 'digest', ['created_at'])

    op.create_table(
        'digest_item',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('digest_id', sa.Integer(), nullable=False),
        sa.Column('account_email', sa.String(length=255), nullable=False),
        sa.Column('action', sa.Text(), nullable=False),
        sa.Column('sender', sa.String(length=512), nullable=True),
        sa.Column('subject', sa.Text(), nullable=True),
        sa.Column('calendar_url', sa.Text(), nullable=True),
        sa.Column('done', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('done_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['digest_id'], ['digest.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_digest_item_digest_id', 'digest_item', ['digest_id'])


def downgrade():
    op.drop_index('ix_digest_item_digest_id', table_name='digest_item')
    op.drop_table('digest_item')
    op.drop_index('ix_digest_created_at', table_name='digest')
    op.drop_index('ix_digest_master_id', table_name='digest')
    op.drop_table('digest')
