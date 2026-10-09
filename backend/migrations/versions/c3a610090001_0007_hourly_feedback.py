"""0007: append-only human feedback linked to frozen hourly reports."""
from alembic import op
import sqlalchemy as sa

revision = "c3a610090001"
down_revision = "c3a610080001"
branch_labels = None
depends_on = None


def upgrade():
    name = "hourly_brief_feedback"
    inspector = sa.inspect(op.get_bind())
    columns = [sa.Column("id", sa.Integer(), primary_key=True),
               sa.Column("hour", sa.String(32), sa.ForeignKey("hourly_brief_rows.hour"), nullable=False),
               sa.Column("content", sa.Text(), nullable=False),
               sa.Column("created_at", sa.String(32), nullable=False),
               sa.Column("client_id", sa.String(36), nullable=False, unique=True)]
    if inspector.has_table(name):
        actual, expected = inspector.get_columns(name), {c.name: c for c in columns}
        if ({c["name"] for c in actual} != set(expected)
            or inspector.get_pk_constraint(name)["constrained_columns"] != ["id"]
            or not any(c["column_names"] == ["client_id"] for c in inspector.get_unique_constraints(name))
            or not any(c["constrained_columns"] == ["hour"] and c["referred_table"] == "hourly_brief_rows"
                       and c["referred_columns"] == ["hour"] for c in inspector.get_foreign_keys(name))
            or any(c["nullable"] or not isinstance(c["type"], type(expected[c["name"]].type))
                   or getattr(c["type"], "length", None) != getattr(expected[c["name"]].type, "length", None)
                   for c in actual)):
            raise RuntimeError(f"Existing {name} schema differs; migration cancelled")
    else:
        op.create_table(name, *columns)
    indexes = {i["name"]: i for i in sa.inspect(op.get_bind()).get_indexes(name)}
    index = "ix_hourly_brief_feedback_hour_id"
    if index not in indexes:
        op.create_index(index, name, ["hour", "id"])
    elif indexes[index]["column_names"] != ["hour", "id"] or indexes[index]["unique"]:
        raise RuntimeError(f"Existing {name} index differs; migration cancelled")


def downgrade():
    op.drop_table("hourly_brief_feedback")
