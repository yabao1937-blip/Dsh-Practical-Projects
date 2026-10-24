"""Low priority, single-flight retention; independent of minute decision storage."""
import asyncio
import logging
import os
from pathlib import Path
from contextlib import asynccontextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from .policy import RetentionPolicy, assert_writable
from .service import run_tick
from .housekeeping import file_tick

log = logging.getLogger(__name__)


def maintenance_sessions(engine):
    assert_writable(engine)
    maintenance = create_engine(engine.url, connect_args={'check_same_thread':False, 'timeout':.25},
        pool_size=1, max_overflow=0)
    @event.listens_for(maintenance, 'connect')
    def configure(connection, _):
        connection.execute('PRAGMA busy_timeout=250')
        connection.execute('PRAGMA foreign_keys=ON')
    return maintenance, sessionmaker(bind=maintenance, autoflush=False)


async def run(stop, sessions, policy):
    # Give startup, the collector and new hourly caches first access to the disk.
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=policy.interval_seconds)
            break
        except asyncio.TimeoutError:
            pass
        try:
            await asyncio.to_thread(file_tick, sessions.kw['bind'], Path(__file__).resolve().parents[3])
            result = await asyncio.to_thread(run_tick, sessions, policy=policy)
            if result.get('phase') != 'idle':
                log.info('Retention maintenance: %s', result)
        except Exception:
            # Short busy timeout / failed archive checks leave the source intact.
            log.exception('Retention deferred; original evidence was preserved')


@asynccontextmanager
async def lifespan(engine):
    if os.environ.get('DMCS_ISOLATED_TEST_DB') == '1':
        # Tests exercise maintenance explicitly on disposable databases. Never
        # derive real installation backup/log roots from a TestClient lifespan.
        yield
        return
    maintenance, sessions = maintenance_sessions(engine)
    stop = asyncio.Event()
    task = asyncio.create_task(run(stop, sessions, RetentionPolicy()))
    try:
        yield
    finally:
        stop.set()
        await task  # Never cancel an in-flight transaction or archive publication.
        maintenance.dispose()
