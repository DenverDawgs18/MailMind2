from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '1ef98a9396bb'
down_revision = '11296ded5140'
branch_labels = None
depends_on = None

# Name for the foreign key constraint
FK_NAME = 'fk_todo_user_id_user'

def upgrade():
    with op.batch_alter_table('todo', schema=None) as batch_op:
        batch_op.add_column(sa.Column('item', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('user', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('done', sa.Boolean(), nullable=True))
        batch_op.create_foreign_key(FK_NAME, 'user', ['user'], ['id'])

def downgrade():
    with op.batch_alter_table('todo', schema=None) as batch_op:
        batch_op.drop_constraint(FK_NAME, type_='foreignkey')
        batch_op.drop_column('done')
        batch_op.drop_column('user')
        batch_op.drop_column('item')
