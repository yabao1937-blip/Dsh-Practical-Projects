"""Read the host's bounded ephemeral telemetry without downloading a database."""
from contextlib import closing
import json
import sqlite3

from ..config import REMOTE_READONLY

MAX_TRANSPORT_BYTES = 8 * 1024 * 1024


def transport_view(database_path):
    from ..telemetry.realtime import read_runtime_path
    from ..telemetry.domain import iso_time, utc_now
    result = {'sources': {}, 'checked_at': iso_time(utc_now())}
    for source, document in read_runtime_path(database_path).items():
        candidate = {**result['sources'], source: document}
        if len(json.dumps(candidate, ensure_ascii=False).encode('utf-8')) <= MAX_TRANSPORT_BYTES:
            result['sources'] = candidate
    return result


def attach_runtime(db, config=None):
    """Missing/old host support falls back to dated SQL samples, never local IPC.

    Only the authenticated transport supplies these documents. Consumers still
    fence the owner, lease, point definition and timestamps against the same SQL
    snapshot. The laptop continues calculating with its own application code.
    """
    configuration = REMOTE_READONLY if config is None else config
    if not configuration:
        return False
    from remote_sqlite import RemoteConnection
    db.info['telemetry_runtime'] = {}
    try:
        with closing(RemoteConnection(configuration, timeout=3)) as connection:
            data = connection._request('GET', '/live-cache')
        documents = data.get('sources')
        if (not isinstance(documents, dict) or len(documents) > 128
                or len(json.dumps(documents, allow_nan=False).encode('utf-8')) > MAX_TRANSPORT_BYTES):
            return False
        db.info['telemetry_runtime'] = documents
        return True
    except (sqlite3.Error, ValueError, TypeError, AttributeError):
        return False
