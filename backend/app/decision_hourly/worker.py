import asyncio
import logging
import time
from contextlib import asynccontextmanager

from .service import observation_tick, maintenance_tick
from .statistics import CADENCE, ARCHIVE_CADENCE

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    """Run the decision pipeline without the retired report generator."""
    stop = asyncio.Event()
    task = asyncio.create_task(run(stop))
    try:
        yield
    finally:
        stop.set()
        await task


def next_delay(now):
    # Windows timers may wake ~15 ms early. Run safely inside the next slot;
    # waking before its boundary could see the old slot and skip a whole cycle.
    return CADENCE-now%CADENCE+.1


async def run(stop):
    # Coalesce maintenance requests, never start overlapping hour calculations.
    # A request is queued only AFTER an observation has committed across the hour.
    requests = asyncio.Queue(maxsize=1)
    async def maintain():
        while True:
            now = await requests.get()
            if now is None:
                return
            try:
                await asyncio.to_thread(maintenance_tick,now)
            except Exception:
                logger.exception('Hourly decision maintenance failed; retry after next observation')
    task = asyncio.create_task(maintain())
    try:
        while not stop.is_set():
            try:
                now = await asyncio.to_thread(observation_tick, cadence_seconds=ARCHIVE_CADENCE)
                if now is not None:
                    if requests.full():
                        requests.get_nowait()
                    requests.put_nowait(now)
            except Exception:
                logger.exception('Hourly decision observation failed; retry on next slot')
            try:
                await asyncio.wait_for(stop.wait(), timeout=next_delay(time.time()))
            except asyncio.TimeoutError:
                pass
    finally:
        # Do not cancel a to_thread write midway through its transaction.
        if requests.full():
            requests.get_nowait()
        await requests.put(None)
        await task
