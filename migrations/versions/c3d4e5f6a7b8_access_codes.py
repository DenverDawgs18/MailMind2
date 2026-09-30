"""invite codes: access_code table, master.comp_until / access_code_id

Accounts that were let in by the old shared CODE / TEMP_CODE (subscribed with
no Stripe customer, or flagged temp) keep their access as comped-forever, and
`subscribed` goes back to meaning "Stripe says active".

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-29 16:00:00.000000

"""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = 'c3d4e5f6a7b8'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None

FOREVER = datetime(9999, 12, 31, tzinfo=timezone.utc)


def upgrade():
    op.create_table(
        'access_code',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=64), nullable=False),
        sa.Column('note', sa.String(length=255), nullable=True),
        sa.Column('max_uses', sa.Integer(), nullable=True),
        sa.Column('uses', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('access_days', sa.Integer(), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_access_code_code', 'access_code', ['code'], unique=True)

    with op.batch_alter_table('master') as batch:
        batch.add_column(sa.Column('comp_until', sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column('access_code_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_master_access_code', 'access_code', ['access_code_id'], ['id'],
                                 ondelete='SET NULL')
        batch.create_index('ix_master_access_code_id', ['access_code_id'])

    op.get_bind().execute(
        sa.text("UPDATE master SET comp_until = :forever, subscribed = :f "
                "WHERE (subscribed = :t AND stripe_customer_id IS NULL) OR temp = :t"),
        {"forever": FOREVER, "t": True, "f": False},
    )


def downgrade():
    op.get_bind().execute(
        sa.text("UPDATE master SET subscribed = :t WHERE comp_until IS NOT NULL"), {"t": True},
    )
    with op.batch_alter_table('master') as batch:
        batch.drop_index('ix_master_access_code_id')
        batch.drop_constraint('fk_master_access_code', type_='foreignkey')
        batch.drop_column('access_code_id')
        batch.drop_column('comp_until')
    op.drop_index('ix_access_code_code', table_name='access_code')
    op.drop_table('access_code')
