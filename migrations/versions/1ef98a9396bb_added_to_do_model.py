from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = '1ef98a9396bb'
down_revision = '11296ded5140'
branch_labels = None
depends_on = None

# Name for the foreign key constraint
FK_NAME = 'fk_todo_user_id_user'

def upgrade():
    # Get connection and inspector to check existing columns
    conn = op.get_bind()
    inspector = inspect(conn)
    existing_columns = [col['name'] for col in inspector.get_columns('todo')]
    existing_fks = [fk['name'] for fk in inspector.get_foreign_keys('todo')]
    
    with op.batch_alter_table('todo', schema=None) as batch_op:
        # Only add columns that don't already exist
        if 'item' not in existing_columns:
            batch_op.add_column(sa.Column('item', sa.Text(), nullable=True))
            
        if 'user' not in existing_columns:
            batch_op.add_column(sa.Column('user', sa.Integer(), nullable=True))
            
        if 'done' not in existing_columns:
            batch_op.add_column(sa.Column('done', sa.Boolean(), nullable=True))
        
        # Only create foreign key if it doesn't already exist
        if FK_NAME not in existing_fks:
            batch_op.create_foreign_key(FK_NAME, 'user', ['user'], ['id'])

def downgrade():
    with op.batch_alter_table('todo', schema=None) as batch_op:
        batch_op.drop_constraint(FK_NAME, type_='foreignkey')
        batch_op.drop_column('done')
        batch_op.drop_column('user')
        batch_op.drop_column('item')