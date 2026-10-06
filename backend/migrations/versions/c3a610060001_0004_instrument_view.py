"""0004: six-column SQLite instrument view."""
from alembic import op
import sqlalchemy as sa

# Keep this revision's SQL fixed even if the runtime projection evolves later.
INSTRUMENT_VIEW_SQL = """
CREATE VIEW IF NOT EXISTS online_instruments AS
SELECT
    COALESCE(json_extract(config, '$.device'), '') AS "设备",
    COALESCE(NULLIF(json_extract(config, '$.tag_name'), ''), json_extract(config, '$.name')) AS "标签名",
    COALESCE(json_extract(config, '$.description'), '') AS "说明",
    COALESCE(NULLIF(json_extract(config, '$.item_id'), ''), json_extract(config, '$.node_id')) AS "ItemID",
    json_extract(latest, '$.value') AS "值",
    json_extract(latest, '$.received_at') AS "时间"
FROM telemetry_points
WHERE enabled = 1
"""

revision = "c3a610060001"
down_revision = "c3a610050001"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "sqlite":
        op.execute(sa.text(INSTRUMENT_VIEW_SQL))


def downgrade():
    if op.get_bind().dialect.name == "sqlite":
        op.execute(sa.text("DROP VIEW IF EXISTS online_instruments"))
