"""独立采集进程；HTTP 服务不创建 OPC 连接，浏览器关闭也不停止采集。"""
import asyncio
import logging
import random
import time
import uuid

from sqlalchemy.exc import OperationalError

from .opcua import ConfigurationError, OpcUaReader
from .repository import CollectorBusy, LeaseLost
from .spool import SpoolError

log = logging.getLogger("dmcs.collector")


def retry_delay(attempt: int, minimum: float, maximum: float) -> float:
    cap = min(maximum, minimum * 2 ** min(attempt, 16))
    return random.uniform(minimum, cap)


def permanent_error(exc):
    return isinstance(exc, (ConfigurationError, ValueError, ImportError, SpoolError, LeaseLost)) or type(exc).__name__ in {
        "BadUserAccessDenied", "BadIdentityTokenInvalid", "BadIdentityTokenRejected",
        "BadSecurityChecksFailed", "BadCertificateInvalid", "BadCertificateUntrusted",
        "BadCertificateTimeInvalid", "BadCertificateUriInvalid", "BadSecurityPolicyRejected",
        "BadCertificateUseNotAllowed", "BadCertificateRevoked", "BadCertificateHostNameInvalid",
    }


async def run_collector(cfg, repository, spool, *, once=False, stop=None, reader_factory=OpcUaReader):
    if not cfg.enabled:
        return 0
    stop = stop or asyncio.Event()
    owner = uuid.uuid4().hex
    try:
        await asyncio.to_thread(repository.acquire, cfg, owner)
    except CollectorBusy:
        log.error("collector_already_running source=%s", cfg.source_id)
        return 2
    log.info("collector_started source=%s points=%d read_only=True", cfg.source_id, len(cfg.points))
    attempt, last_prune = 0, 0
    exit_code = 0
    last_error = None

    async def set_state(state, code=None, delay=0):
        await asyncio.to_thread(repository.state, cfg, owner, state, code, delay)

    async def drain():
        for path in spool.pending():
            batch = spool.read(path)
            await asyncio.to_thread(repository.persist, cfg, owner, batch)
            spool.ack(path)

    try:
        while not stop.is_set():
            try:
                await drain()
                await set_state("connecting")
                async with reader_factory(cfg) as reader:
                    first_read = True
                    while not stop.is_set():
                        cycle_started = time.monotonic()
                        samples = await reader.read()
                        if len(samples) != len(cfg.points):
                            raise ValueError("incomplete read batch")
                        batch = {"source_id": cfg.source_id, "batch_id": uuid.uuid4().hex, "samples": samples}
                        spool.enqueue(batch)
                        await drain()
                        await set_state("connected")
                        if first_read:
                            log.info("collector_connected source=%s points=%d", cfg.source_id, len(samples))
                            first_read = False
                        attempt = 0
                        if time.monotonic() - last_prune >= 60:
                            await asyncio.to_thread(repository.prune, cfg)
                            last_prune = time.monotonic()
                        if once:
                            return 0
                        try:
                            # Read/persist time belongs to this cycle, not to an extra
                            # delay after it. Slow cycles never fabricate catch-up data.
                            await asyncio.wait_for(stop.wait(), max(.001, cfg.poll_seconds-(time.monotonic()-cycle_started)))
                        except asyncio.TimeoutError:
                            pass
            except Exception as exc:
                code = type(exc).__name__
                last_error = code
                fatal = permanent_error(exc)
                transient = isinstance(exc, (OSError, asyncio.TimeoutError, ConnectionError, OperationalError)) or code in {
                    "BadConnectionClosed", "BadSessionClosed", "BadSessionIdInvalid", "BadSecureChannelClosed",
                    "BadTimeout", "BadServerHalted", "BadCommunicationError", "BadNotConnected",
                }
                fatal = fatal or not transient
                delay = 0 if fatal else retry_delay(attempt, cfg.retry_min_seconds, cfg.retry_max_seconds)
                log.warning("collector_failure source=%s code=%s retry_seconds=%.2f", cfg.source_id, code, delay)
                try:
                    await set_state("fault" if fatal else "retrying", code, delay)
                except (OperationalError, LeaseLost):
                    log.error("collector_status_storage_unavailable source=%s", cfg.source_id)
                if fatal or once:
                    exit_code = 1
                    break
                attempt += 1
                try:
                    await asyncio.wait_for(stop.wait(), delay)
                except asyncio.TimeoutError:
                    pass
    finally:
        try:
            await asyncio.to_thread(repository.state, cfg, owner, "fault" if exit_code else "stopped",
                last_error if exit_code else None, 0, True)
        except (OperationalError, LeaseLost):
            log.error("collector_release_failed source=%s", cfg.source_id)
    return exit_code
