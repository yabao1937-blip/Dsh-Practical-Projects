"""0015: SQL view exposes dynamic values; fixed identity labels live in files."""
from alembic import op
import sqlalchemy as sa

revision = 'c3a610240001'
down_revision = 'c3a610160001'
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == 'sqlite':
        op.execute(sa.text('DROP VIEW IF EXISTS online_instruments'))
        op.execute(sa.text('''CREATE VIEW online_instruments AS SELECT
            source_id AS "来源", key AS "点位",
            COALESCE(json_extract(config, '$.description'), '') AS "说明",
            json_extract(latest, '$.value') AS "值",
            json_extract(latest, '$.received_at') AS "时间"
            FROM telemetry_points WHERE enabled = 1'''))


def downgrade():
    if op.get_bind().dialect.name == 'sqlite':
        op.execute(sa.text('DROP VIEW IF EXISTS online_instruments'))
        op.execute(sa.text('''CREATE VIEW online_instruments AS SELECT
            COALESCE(json_extract(config, '$.device'), '') AS "设备",
            COALESCE(NULLIF(json_extract(config, '$.tag_name'), ''), json_extract(config, '$.name')) AS "标签名",
            COALESCE(json_extract(config, '$.description'), '') AS "说明",
            COALESCE(NULLIF(json_extract(config, '$.item_id'), ''), json_extract(config, '$.node_id')) AS "ItemID",
            json_extract(latest, '$.value') AS "值",
            json_extract(latest, '$.received_at') AS "时间"
            FROM telemetry_points WHERE enabled = 1'''))
