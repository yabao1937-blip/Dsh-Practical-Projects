"""SQL-only dynamic values; fixed labels are resolved by the API file catalog."""
from sqlalchemy import text

INSTRUMENT_VIEW_SQL = """
CREATE VIEW IF NOT EXISTS online_instruments AS
SELECT
    source_id AS "来源",
    key AS "点位",
    COALESCE(json_extract(config, '$.description'), '') AS "说明",
    json_extract(latest, '$.value') AS "值",
    json_extract(latest, '$.received_at') AS "时间"
FROM telemetry_points
WHERE enabled = 1
"""


def ensure_sqlite_views(engine):
    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            connection.execute(text('DROP VIEW IF EXISTS online_instruments'))
            connection.execute(text(INSTRUMENT_VIEW_SQL))
