"""A separate policy: collector retention_days never performs unsafe raw DELETEs."""
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class RetentionPolicy:
    online_days: int = 30
    detail_days: int = 365
    delete_batch: int = 500
    interval_seconds: int = 60
    max_unit_rows: int = 100000
    max_unit_bytes: int = 128 * 1024 * 1024

    def __post_init__(self):
        if not (30 <= self.online_days < self.detail_days and self.detail_days >= 365):
            raise ValueError('Retention cannot shorten the approved 30/365-day minima')
        if not 1 <= self.delete_batch <= 1000 or self.interval_seconds < 60:
            raise ValueError('Maintenance must use small batches and a low frequency')

    def as_dict(self):
        return asdict(self)


def storage_root(engine):
    if engine.dialect.name != 'sqlite' or not engine.url.database:
        raise ValueError('Local SQLite storage is required')
    # Resolve the database itself: a C: directory junction must lead to D: storage.
    return Path(engine.url.database).resolve().parent / 'retention'


def assert_writable(engine):
    from ..config import REMOTE_READONLY
    if REMOTE_READONLY or getattr(engine, '_dmcs_readonly', False):
        raise PermissionError('Read-only test mode cannot archive or delete production data')
    if engine.dialect.name != 'sqlite':
        raise ValueError('Retention currently requires a local SQLite database')
