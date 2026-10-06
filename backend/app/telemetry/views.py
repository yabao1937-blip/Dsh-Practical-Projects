"""Six-column read-only projection of enabled instruments' last received samples."""
from sqlalchemy import text

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


def ensure_sqlite_views(engine):
    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            connection.execute(text(INSTRUMENT_VIEW_SQL))
