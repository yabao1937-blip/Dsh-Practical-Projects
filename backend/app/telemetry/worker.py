"""独立采集进程；HTTP 服务不创建 OPC 连接，浏览器关闭也不停止采集。"""
import asyncio
import logging
import random
import sqlite3
import time
import uuid

from sqlalchemy.exc import OperationalError

from .opcua import ConfigurationError, OpcUaReader
from .repository import CollectorBusy, LeaseLost
from .spool import SpoolError
from .domain import parse_time
from .realtime import RuntimePublisher, select_alignment_evidence

log = logging.getLogger("dmcs.collector")


def retry_delay(attempt: int, minimum: float, maximum: float) -> float:
    cap = min(maximum, minimum * 2 ** min(attempt, 16))
    return random.uniform(minimum, cap)


TRANSIENT_ERROR_NAMES = {
    "BadConnectionClosed", "BadSessionClosed", "BadSessionIdInvalid", "BadSecureChannelClosed",
    "BadTimeout", "BadServerHalted", "BadCommunicationError", "BadNotConnected",
}


def exception_chain(exc):
    """还原异常链：库会把传输异常包成新异常重抛（raise ... from ex）。"""
    chain, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        chain.append(exc)
        exc = exc.__cause__ if exc.__cause__ is not None else exc.__context__
    return chain


def transient_error(exc):
    """瞬态（可重试）判定：异常本身或链条任一环属于传输类失败即瞬态。

    asyncua ua_client.send_request 会在 socket 仍打开时把发送异常（含 wait_for
    超时）包成裸 Exception 重抛；裸 Exception 不匹配 isinstance 白名单曾被判
    fatal 导致采集进程退出（2026-10-22 生产断采根因）。本仓库代码从不 raise
    裸 Exception，因此裸 Exception 只可能来自库传输层包装，按瞬态重试。已知
    致命错误（配置/认证/证书等）由 permanent_error 优先判定，不受此影响。
    """
    for error in exception_chain(exc):
        if isinstance(error, (OSError, asyncio.TimeoutError, ConnectionError)):
            return True
        if type(error).__name__ in TRANSIENT_ERROR_NAMES:
            return True
        if type(error) is Exception:
            return True
    return False


def permanent_error(exc):
    """致命判定沿异常链检查：库包装（raise ... from）不得掩盖底层配置/认证/证书错误。"""
    names = {
        "BadUserAccessDenied", "BadIdentityTokenInvalid", "BadIdentityTokenRejected",
        "BadSecurityChecksFailed", "BadCertificateInvalid", "BadCertificateUntrusted",
        "BadCertificateTimeInvalid", "BadCertificateUriInvalid", "BadSecurityPolicyRejected",
        "BadCertificateUseNotAllowed", "BadCertificateRevoked", "BadCertificateHostNameInvalid",
    }
    return any(
        isinstance(error, (ConfigurationError, ValueError, ImportError, SpoolError, LeaseLost))
        or type(error).__name__ in names
        for error in exception_chain(exc)
    )


async def run_collector(cfg, repository, spool, *, once=False, stop=None, reader_factory=OpcUaReader):
    if not cfg.enabled:
        return 0
    stop = stop or asyncio.Event()
    owner = uuid.uuid4().hex
    halt, wake, producer_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    desired = [0, "connecting", None, 0]
    acquired = False
    cancelled = False
    exit_code = 0
    last_error = None
    runtime = None

    def publish(state, code=None, delay=0, batch=None):
        if runtime is None:
            return
        try:
            runtime.publish(state, code, delay, batch)
        except Exception as exc:
            # IPC is replaceable. A failed display cache must not stop durable
            # sampling, disclose readings in logs, or trigger a UA reconnect.
            log.warning('collector_runtime_unavailable source=%s code=%s', cfg.source_id, type(exc).__name__)

    def state(state, code=None, delay=0):
        if desired[1:] != [state, code, delay]:
            desired[:] = [desired[0] + 1, state, code, delay]
            publish(state, code, delay)
        wake.set()

    def storage_error(phase, exc, delay=0):
        # Never format the exception: SQLAlchemy exceptions include SQL/parameters.
        original = getattr(exc, "orig", exc)
        log.warning("collector_storage_unavailable source=%s phase=%s code=%s sqlite_code=%s sqlite_name=%s retry_seconds=%.2f",
            cfg.source_id, phase, type(exc).__name__, getattr(original, "sqlite_errorcode", None),
            getattr(original, "sqlite_errorname", None), delay)

    def failed(phase, exc):
        nonlocal exit_code, last_error
        exit_code, last_error = 1, type(exc).__name__
        storage_error(phase, exc)
        halt.set()
        wake.set()

    async def db_call(function, *args):
        nonlocal cancelled
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        # Cancelling an asyncio wrapper does not stop a SQLite thread. Join it
        # before releasing the lease or starting another database operation.
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
                halt.set()
                wake.set()

    async def pause(seconds):
        try:
            await asyncio.wait_for(halt.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def watch_stop():
        await stop.wait()
        halt.set()
        wake.set()

    async def read_or_stop(reader):
        read = asyncio.create_task(reader.read())
        interrupted = asyncio.create_task(halt.wait())
        try:
            await asyncio.wait((read, interrupted), return_when=asyncio.FIRST_COMPLETED)
            # Some readers set stop inside read(). Preserve their completed batch.
            if read.done():
                return read.result()
            read.cancel()
            try:
                await read
            except asyncio.CancelledError:
                pass
            return None
        finally:
            interrupted.cancel()
            await asyncio.gather(interrupted, return_exceptions=True)

    async def produce():
        nonlocal exit_code, last_error
        attempt = 0
        last_bucket = None
        last_quality = None
        try:
            while not halt.is_set():
                try:
                    state("connecting")
                    async with reader_factory(cfg) as reader:
                        first_read = True
                        while not halt.is_set():
                            started = time.monotonic()
                            samples = await read_or_stop(reader)
                            if samples is None:
                                break
                            if len(samples) != len(cfg.points):
                                raise ValueError("incomplete read batch")
                            batch = {"source_id": cfg.source_id, "batch_id": uuid.uuid4().hex, "samples": samples}
                            publish('connected', batch=batch)
                            stamp = max(parse_time(s['received_at']).timestamp() for s in samples)
                            bucket = int(stamp // cfg.history_interval_seconds)
                            quality = tuple((s['key'], s['quality'], s.get('issue')) for s in samples)
                            periodic = (cfg.history_interval_seconds <= cfg.poll_seconds
                                        or last_bucket is None or bucket != last_bucket)
                            changed_quality = last_quality is not None and quality != last_quality
                            try:
                                # All spool operations run in this event loop;
                                # only repository calls run in a worker thread.
                                if once or periodic or changed_quality:
                                    cadence = cfg.history_interval_seconds if periodic or once else cfg.poll_seconds
                                    batch.update(storage_policy='periodic-v1', cadence_seconds=cadence,
                                        storage_kind='periodic' if periodic or once else 'quality-event')
                                    batch['samples'] = [dict(s, cadence_seconds=cadence,
                                        storage_kind=batch['storage_kind']) for s in samples]
                                    if (periodic or once) and cfg.history_interval_seconds == 60:
                                        batch['alignment_evidence'] = select_alignment_evidence(
                                            runtime.document if runtime is not None else None,
                                            cfg, owner, max(parse_time(s['received_at']) for s in samples))
                                    spool.enqueue(batch)
                                    if periodic:
                                        last_bucket = bucket
                                last_quality = quality
                            except Exception as exc:
                                failed("spool_enqueue", exc)
                                return
                            state("connected")
                            if first_read:
                                log.info("collector_connected source=%s points=%d", cfg.source_id, len(samples))
                                first_read = False
                            attempt = 0
                            if once:
                                return
                            await pause(max(.001, cfg.poll_seconds - (time.monotonic() - started)))
                except Exception as exc:
                    code = type(exc).__name__
                    last_error = code
                    transient = transient_error(exc)
                    fatal = permanent_error(exc) or not transient or once
                    delay = 0 if fatal else retry_delay(attempt, cfg.retry_min_seconds, cfg.retry_max_seconds)
                    log.warning("collector_failure source=%s code=%s retry_seconds=%.2f", cfg.source_id, code, delay)
                    state("fault" if fatal else "retrying", code, delay)
                    if fatal:
                        exit_code = 1
                        return
                    attempt += 1
                    await pause(delay)
        finally:
            producer_done.set()
            wake.set()

    async def store():
        written_revision = -1
        attempt = 0
        shutdown_deadline = None
        while True:
            if halt.is_set() and shutdown_deadline is None:
                shutdown_deadline = time.monotonic() + 15
            phase = "spool_read"
            try:
                paths = spool.pending()
                revision, wanted, code, delay = desired
                # A current UA failure must not be overwritten by an older
                # connected write returning after the read-side state changed.
                if written_revision != revision and (wanted != "connected" or not paths):
                    phase = "state"
                    await db_call(repository.state, cfg, owner, wanted, code, delay)
                    written_revision = revision
                    continue
                if paths:
                    path = paths[0]
                    batch = spool.read(path)
                    phase = "persist"
                    await db_call(repository.persist, cfg, owner, batch)
                    phase = "spool_ack"
                    spool.ack(path)
                    # Re-read desired state after every awaited database call.
                    revision, wanted, code, delay = desired
                    if written_revision != revision:
                        phase = "state"
                        await db_call(repository.state, cfg, owner, wanted, code, delay)
                        written_revision = revision
                    attempt = 0
                    if shutdown_deadline is not None and time.monotonic() >= shutdown_deadline:
                        return
                    continue
                if producer_done.is_set():
                    return
                wake.clear()
                # Producer cannot interleave between the synchronous empty
                # check and clear; its next enqueue/state change sets wake.
                await wake.wait()
            except Exception as exc:
                if sqlite_busy(exc):
                    delay = retry_delay(attempt, cfg.retry_min_seconds, cfg.retry_max_seconds)
                    storage_error(phase, exc, delay)
                    if halt.is_set() or producer_done.is_set():
                        # Keep unacknowledged files for the next start. --once
                        # cannot report success before its new batch commits.
                        if once:
                            nonlocal_set_failure(exc)
                        return
                    attempt += 1
                    await pause(delay)
                    continue
                failed(phase, exc)
                return

    def nonlocal_set_failure(exc):
        nonlocal exit_code, last_error
        exit_code, last_error = 1, type(exc).__name__

    watcher = asyncio.create_task(watch_stop())
    jobs = []
    try:
        for attempt in range(3):
            if halt.is_set():
                return 0
            try:
                await db_call(repository.acquire, cfg, owner)
                acquired = True
                break
            except CollectorBusy:
                log.error("collector_already_running source=%s", cfg.source_id)
                return 2
            except Exception as exc:
                delay = retry_delay(attempt, cfg.retry_min_seconds, cfg.retry_max_seconds)
                storage_error("acquire", exc, delay if sqlite_busy(exc) and attempt < 2 else 0)
                if not sqlite_busy(exc) or attempt == 2:
                    return 1
                await pause(delay)
        log.info("collector_started source=%s points=%d read_only=True history_retention=permanent", cfg.source_id, len(cfg.points))
        try:
            runtime = RuntimePublisher.for_repository(repository, cfg, owner)
        except Exception as exc:
            log.warning('collector_runtime_unavailable source=%s code=%s', cfg.source_id, type(exc).__name__)
        jobs = [asyncio.create_task(produce()), asyncio.create_task(store())]
        group = asyncio.gather(*jobs)
        while True:
            try:
                await asyncio.shield(group)
                break
            except asyncio.CancelledError:
                cancelled = True
                halt.set()
                wake.set()
    finally:
        halt.set()
        wake.set()
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        if acquired:
            publish('fault' if exit_code else 'stopped', last_error if exit_code else None)
            try:
                await db_call(repository.state, cfg, owner, "fault" if exit_code else "stopped",
                    last_error if exit_code else None, 0, True)
            except Exception as exc:
                storage_error("release", exc)
                log.error("collector_release_failed source=%s", cfg.source_id)
        if cancelled:
            raise asyncio.CancelledError
    return exit_code


def sqlite_busy(exc):
    """Retry only explicit SQLite BUSY/LOCKED codes, including extended codes."""
    if not isinstance(exc, OperationalError):
        return False
    code = getattr(exc.orig, "sqlite_errorcode", None)
    return isinstance(code, int) and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
