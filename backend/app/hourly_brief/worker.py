"""Web-service lifespan worker; runs even when every browser is closed."""
import asyncio
import logging
from contextlib import asynccontextmanager

from .service import tick

logger = logging.getLogger(__name__)


async def run(stop):
    while not stop.is_set():
        try:
            await asyncio.to_thread(tick)
        except Exception:
            logger.exception("Hourly report generation failed; will retry")
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app):
    await asyncio.to_thread(tick)
    stop = asyncio.Event()
    task = asyncio.create_task(run(stop))
    try:
        yield
    finally:
        stop.set()
        await task
