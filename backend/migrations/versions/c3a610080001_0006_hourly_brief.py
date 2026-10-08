"""0006: immutable hour-boundary reports, outside /state replacement."""
from alembic import op
import sqlalchemy as sa

revision = "c3a610080001"
down_revision = "c3a610070001"
branch_labels = None
depends_on = None


def upgrade():
    for name, columns, primary in (
        ("hourly_brief_schedule", [
            sa.Column("key", sa.String(16), primary_key=True),
            sa.Column("activated_at", sa.String(32), nullable=False),
            sa.Column("next_hour", sa.String(32), nullable=False)], "key"),
        ("hourly_brief_rows", [
            sa.Column("hour", sa.String(32), primary_key=True),
            sa.Column("generated_at", sa.String(32), nullable=False),
            sa.Column("snapshot", sa.JSON(), nullable=False)], "hour"),
    ):
        inspector = sa.inspect(op.get_bind())
        if inspector.has_table(name):
            actual = inspector.get_columns(name)
            expected = {c.name: c for c in columns}
            if ({c["name"] for c in actual} != set(expected)
                or inspector.get_pk_constraint(name)["constrained_columns"] != [primary]
                or any(c["nullable"] or not isinstance(c["type"], type(expected[c["name"]].type))
                       or getattr(c["type"], "length", None) != getattr(expected[c["name"]].type, "length", None)
                       for c in actual)):
                raise RuntimeError(f"Existing {name} schema differs; migration cancelled")
        else:
            op.create_table(name, *columns)


def downgrade():
    op.drop_table("hourly_brief_rows")
    op.drop_table("hourly_brief_schedule")
