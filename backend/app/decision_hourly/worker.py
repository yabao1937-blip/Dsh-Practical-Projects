import asyncio
import logging
import time

from .service import tick
from .statistics import CADENCE

logger = logging.getLogger(__name__)


def next_delay(now):
    # Windows timers may wake ~15 ms early. Run safely inside the next slot;
    # waking before its boundary could see the old slot and skip a whole cycle.
    return CADENCE-now%CADENCE+.1


async def run(stop):
    while not stop.is_set():
        try:
            await asyncio.to_thread(tick)
        except Exception:
            logger.exception('Hourly decision observation failed; retry on next slot')
        try:
            await asyncio.wait_for(stop.wait(), timeout=next_delay(time.time()))
        except asyncio.TimeoutError:
            pass
