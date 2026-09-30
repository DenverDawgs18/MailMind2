"""analytics: signup source on master, per-day source visit counts

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-30 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('master') as batch:
        batch.add_column(sa.Column('signup_source', sa.String(length=120), nullable=True))
        batch.add_column(sa.Column('signup_referrer', sa.String(length=120), nullable=True))
        batch.add_column(sa.Column('signup_landing', sa.String(length=120), nullable=True))
        batch.create_index('ix_master_signup_source', ['signup_source'])

    op.create_table(
        'source_visit',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('source', sa.String(length=120), nullable=False),
        sa.Column('landing', sa.String(length=120), nullable=False),
        sa.Column('visits', sa.Integer(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('day', 'source', 'landing', name='uq_source_visit'),
    )
    op.create_index('ix_source_visit_day', 'source_visit', ['day'])


def downgrade():
    op.drop_index('ix_source_visit_day', table_name='source_visit')
    op.drop_table('source_visit')
    with op.batch_alter_table('master') as batch:
        batch.drop_index('ix_master_signup_source')
        batch.drop_column('signup_landing')
        batch.drop_column('signup_referrer')
        batch.drop_column('signup_source')
