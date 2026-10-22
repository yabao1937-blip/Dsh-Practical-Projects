"""Collector storage failure isolation; synthetic readers and durable temp spool."""
import asyncio
import logging
import sqlite3
import threading
import time

import pytest
from sqlalchemy.exc import OperationalError

from app.telemetry.config import CollectorConfig, PointConfig
from app.telemetry.domain import normalize, utc_now
from app.telemetry.repository import LeaseLost
from app.telemetry.spool import DurableSpool
from app.telemetry.worker import run_collector, sqlite_busy


def config(**changes):
    return CollectorConfig(enabled=True, endpoint="opc.tcp://127.0.0.1:1", security="None", allow_insecure=True,
        poll_seconds=.1, timeout_seconds=1, retry_min_seconds=.1, retry_max_seconds=.1,
        points=[PointConfig(key="synthetic", name="Synthetic", node_id="ns=2;s=Synthetic")], **changes)


def busy(code=sqlite3.SQLITE_BUSY):
    original = sqlite3.OperationalError("synthetic SQL and private parameters must never be logged")
    original.sqlite_errorcode = code
    original.sqlite_errorname = "SQLITE_BUSY"
    return OperationalError("synthetic private SQL", {"private": "secret"}, original)


class Repository:
    def __init__(self):
        self.owner = None
        self.saved = set()
        self.states = []
        self.calls = []
        self.active = 0
        self.max_active = 0

    def acquire(self, cfg, owner):
        self.owner = owner
        self.calls.append("acquire")

    def state(self, cfg, owner, state, code=None, delay=0, release=False):
        self.calls.append("release" if release else "state")
        if self.owner != owner:
            raise LeaseLost("synthetic replaced owner")
        self.states.append(state)
        if release:
            self.owner = None

    def persist(self, cfg, owner, batch):
        self.calls.append("persist")
        if self.owner != owner:
            raise LeaseLost("synthetic replaced owner")
        self.saved.add(batch["batch_id"])


def reader_factory(cfg, stop, *, reads=3, callback=None):
    stats = {"connects": 0, "closes": 0, "reads": 0, "times": []}

    class Reader:
        async def __aenter__(self):
            stats["connects"] += 1
            return self

        async def __aexit__(self, *_):
            stats["closes"] += 1

        async def read(self):
            stats["reads"] += 1
            stats["times"].append(time.monotonic())
            if callback:
                await callback(stats)
            if reads and stats["reads"] >= reads:
                stop.set()
            now = utc_now()
            return [normalize(cfg.points[0], 1.5, 0, now, now, now)]

    return lambda _: Reader(), stats


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, 517, 262])
def test_sqlite_busy_matches_primary_and_extended_codes(code):
    assert sqlite_busy(busy(code))
    assert not sqlite_busy(busy(sqlite3.SQLITE_ERROR))
    assert not sqlite_busy(OperationalError("private", {}, Exception("locked")))


def test_busy_persist_keeps_sampling_and_does_not_reconnect(tmp_path, caplog):
    cfg = config()
    repo = Repository()
    original = repo.persist
    blocked_reads = []
    entered = threading.Event()

    def persist(*args):
        if not entered.is_set():
            entered.set()
            time.sleep(.65)
            blocked_reads.append(stats["reads"])
            raise busy()
        original(*args)

    repo.persist = persist
    spool = DurableSpool(tmp_path, cfg.source_id)

    async def scenario():
        nonlocal stats
        stop = asyncio.Event()
        factory, stats = reader_factory(cfg, stop, reads=12)
        return await asyncio.wait_for(run_collector(cfg, repo, spool, stop=stop, reader_factory=factory), 5)

    stats = {}
    with caplog.at_level(logging.INFO, logger="dmcs.collector"):
        assert asyncio.run(scenario()) == 0
    assert blocked_reads[0] >= 5, "SQLite waiting must not pause the OPC five-second cadence"
    assert stats["connects"] == stats["closes"] == 1
    assert len(repo.saved) == stats["reads"] == 12
    assert not spool.pending()
    assert "phase=persist" in caplog.text and "sqlite_code=5" in caplog.text
    assert "synthetic private SQL" not in caplog.text and "secret" not in caplog.text


@pytest.mark.parametrize("phase", ["acquire", "persist", "state"])
def test_cancel_joins_inflight_database_thread_before_lease_release(tmp_path, phase):
    cfg, repo = config(), Repository()
    entered, resume = threading.Event(), threading.Event()
    original = getattr(repo, phase)

    def blocked(*args):
        if not entered.is_set():
            entered.set()
            assert resume.wait(3)
        original(*args)

    setattr(repo, phase, blocked)

    async def scenario():
        stop = asyncio.Event()
        factory, stats = reader_factory(cfg, stop, reads=None)
        task = asyncio.create_task(run_collector(cfg, repo, DurableSpool(tmp_path, cfg.source_id),
                                               stop=stop, reader_factory=factory))
        while not entered.is_set():
            await asyncio.sleep(.005)
        task.cancel()
        await asyncio.sleep(.03)
        assert not task.done() and "release" not in repo.calls
        resume.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert repo.calls[-1] == "release" and repo.owner is None
        assert stats["connects"] == stats["closes"]

    asyncio.run(scenario())


def test_once_waits_for_its_new_batch_after_replaying_old_spool(tmp_path):
    import uuid
    cfg, repo = config(), Repository()
    spool = DurableSpool(tmp_path, cfg.source_id)
    now = utc_now()
    old_id = uuid.uuid4().hex
    spool.enqueue(dict(source_id=cfg.source_id, batch_id=old_id,
                       samples=[normalize(cfg.points[0], 1.4, 0, now, now, now)]))

    async def scenario():
        async def delayed(_):
            await asyncio.sleep(.1)
        stop = asyncio.Event()
        factory, stats = reader_factory(cfg, stop, reads=None, callback=delayed)
        code = await run_collector(cfg, repo, spool, once=True, stop=stop, reader_factory=factory)
        assert stats["reads"] == 1 and len(repo.saved) == 2 and old_id in repo.saved
        return code

    assert asyncio.run(scenario()) == 0 and not spool.pending()


def test_busy_startup_is_bounded_and_never_opens_opc(tmp_path, caplog):
    cfg, repo = config(), Repository()
    calls = []

    def acquire(*_):
        calls.append(1)
        raise busy()

    repo.acquire = acquire

    async def scenario():
        stop = asyncio.Event()
        factory, stats = reader_factory(cfg, stop)
        code = await run_collector(cfg, repo, DurableSpool(tmp_path, cfg.source_id), reader_factory=factory)
        assert not stats["connects"]
        return code

    assert asyncio.run(scenario()) == 1 and len(calls) == 3
    assert "phase=acquire" in caplog.text and "secret" not in caplog.text


def test_fatal_storage_error_is_not_treated_as_opc_failure(tmp_path, caplog):
    cfg, repo = config(), Repository()

    def persist(*_):
        raise busy(sqlite3.SQLITE_ERROR)

    repo.persist = persist
    spool = DurableSpool(tmp_path, cfg.source_id)

    async def scenario():
        stop = asyncio.Event()
        factory, stats = reader_factory(cfg, stop, reads=None)
        code = await run_collector(cfg, repo, spool, stop=stop, reader_factory=factory)
        assert stats["connects"] == stats["closes"] == 1
        return code

    assert asyncio.run(scenario()) == 1 and spool.pending()
    assert "collector_failure" not in caplog.text and "secret" not in caplog.text


def test_spool_full_is_visible_without_reconnecting_or_dropping_files(tmp_path, caplog):
    import uuid
    cfg, repo = config(), Repository()
    spool = DurableSpool(tmp_path, cfg.source_id)
    now, old_id = utc_now(), uuid.uuid4().hex
    spool.enqueue(dict(source_id=cfg.source_id, batch_id=old_id,
                       samples=[normalize(cfg.points[0], 1.4, 0, now, now, now)]))
    spool.max_bytes = 1

    async def scenario():
        stop = asyncio.Event()
        factory, stats = reader_factory(cfg, stop, reads=None)
        code = await run_collector(cfg, repo, spool, stop=stop, reader_factory=factory)
        assert stats["connects"] == stats["closes"] == 1
        return code

    assert asyncio.run(scenario()) == 1
    assert "phase=spool_enqueue" in caplog.text
    # Existing durable data may have committed concurrently; either outcome
    # must retain the batch rather than discarding it to free queue capacity.
    remaining = {spool.read(path)["batch_id"] for path in spool.pending()}
    assert old_id in repo.saved | remaining


@pytest.mark.parametrize("once,expected", [(False, 0), (True, 1)])
def test_shutdown_busy_keeps_uncommitted_batch_and_once_never_reports_success(tmp_path, once, expected):
    cfg, repo = config(), Repository()
    calls = []

    def persist(*_):
        calls.append(1)
        raise busy()

    repo.persist = persist
    spool = DurableSpool(tmp_path, cfg.source_id)

    async def scenario():
        stop = asyncio.Event()
        factory, stats = reader_factory(cfg, stop, reads=None if once else 1)
        code = await asyncio.wait_for(run_collector(cfg, repo, spool, once=once,
            stop=stop, reader_factory=factory), 1)
        assert stats["reads"] == 1 and stats["connects"] == stats["closes"] == 1
        return code

    assert asyncio.run(scenario()) == expected
    assert len(calls) == 1 and len(spool.pending()) == 1 and not repo.saved
    assert repo.owner is None


def test_lease_lost_stops_old_reader_and_release_cannot_replace_new_owner(tmp_path):
    cfg, repo = config(), Repository()

    def persist(*_):
        repo.owner = "replacement"
        raise LeaseLost("synthetic")

    repo.persist = persist

    async def scenario():
        stop = asyncio.Event()
        factory, stats = reader_factory(cfg, stop, reads=None)
        code = await run_collector(cfg, repo, DurableSpool(tmp_path, cfg.source_id), stop=stop, reader_factory=factory)
        assert stats["connects"] == stats["closes"] == 1
        return code

    assert asyncio.run(scenario()) == 1 and repo.owner == "replacement"


def test_commit_before_ack_failure_keeps_batch_for_idempotent_restart(tmp_path):
    cfg, repo = config(), Repository()
    spool = DurableSpool(tmp_path, cfg.source_id)
    original_ack = spool.ack

    def ack(_):
        raise OSError("synthetic filesystem unavailable")

    spool.ack = ack

    async def first():
        stop = asyncio.Event()
        factory, _ = reader_factory(cfg, stop, reads=None)
        return await run_collector(cfg, repo, spool, once=True, stop=stop, reader_factory=factory)

    assert asyncio.run(first()) == 1 and len(repo.saved) == 1 and spool.pending()
    spool.ack = original_ack
    assert asyncio.run(first()) == 0 and len(repo.saved) == 2 and not spool.pending()


def test_state_revision_catches_up_after_stale_connected_write(tmp_path):
    cfg = config()
    cfg.retry_min_seconds = cfg.retry_max_seconds = .3
    repo = Repository()
    entering, failed_read, resume = threading.Event(), threading.Event(), threading.Event()
    original = repo.state
    stats = {"connects": 0, "reads": 0}

    def state(*args):
        if args[2] == "connected" and not entering.is_set():
            entering.set()
            assert resume.wait(3)
        original(*args)

    repo.state = state

    async def scenario():
        stop = asyncio.Event()

        class Reader:
            async def __aenter__(self):
                stats["connects"] += 1
                return self

            async def __aexit__(self, *_):
                pass

            async def read(self):
                stats["reads"] += 1
                if stats["reads"] == 2:
                    failed_read.set()
                    raise OSError("synthetic UA network failure")
                if stats["reads"] >= 3:
                    stop.set()
                now = utc_now()
                return [normalize(cfg.points[0], 1.5, 0, now, now, now)]

        task = asyncio.create_task(run_collector(cfg, repo, DurableSpool(tmp_path, cfg.source_id),
                                               stop=stop, reader_factory=lambda _: Reader()))
        while not failed_read.is_set():
            await asyncio.sleep(.005)
        assert entering.is_set()
        resume.set()
        assert await asyncio.wait_for(task, 4) == 0

    asyncio.run(scenario())
    assert "retrying" in repo.states
    assert repo.states.index("retrying") > repo.states.index("connected")
    assert repo.states[-1] == "stopped" and stats["connects"] == 2


def test_bare_wrapped_exception_is_transient_and_reconnects(tmp_path):
    """回归（2026-10-22 生产断采根因）：asyncua ua_client.send_request 在 socket
    仍打开时把发送异常（含 wait_for 超时）包成裸 Exception 重抛。旧逻辑对它
    isinstance 白名单全失配 → 判 fatal → 采集进程退出不自愈。新逻辑必须按
    瞬态处理：进入 retrying 并重连成功。"""
    from app.telemetry.worker import transient_error

    cfg = config()
    cfg.retry_min_seconds = cfg.retry_max_seconds = .1
    repo = Repository()
    stats = {"connects": 0, "reads": 0}
    failed_read = asyncio.Event()

    # 复刻 asyncua ua_client.py:221 的包装方式：wait_for 超时被包成裸 Exception 重抛
    async def failing_send():
        try:
            await asyncio.wait_for(asyncio.sleep(2), timeout=0.05)
        except asyncio.TimeoutError as exc:
            raise Exception("Unhandled exception while sending request to OPC UA server") from exc

    async def scenario():
        stop = asyncio.Event()

        class Reader:
            async def __aenter__(self):
                stats["connects"] += 1
                return self

            async def __aexit__(self, *_):
                pass

            async def read(self):
                stats["reads"] += 1
                if stats["reads"] == 2:
                    failed_read.set()
                    raise await failing_send()
                if stats["reads"] >= 3:
                    stop.set()
                now = utc_now()
                return [normalize(cfg.points[0], 1.5, 0, now, now, now)]

        # 判定函数本身：包装异常必须判瞬态
        wrapped = None
        try:
            await failing_send()
        except Exception as exc:
            wrapped = exc
        assert transient_error(wrapped) is True
        assert transient_error(asyncio.TimeoutError()) is True
        assert transient_error(Exception("AsyncioTimeoutError cause", asyncio.TimeoutError())) is True

        task = asyncio.create_task(run_collector(cfg, repo, DurableSpool(tmp_path, cfg.source_id),
                                               stop=stop, reader_factory=lambda _: Reader()))
        while not failed_read.is_set():
            await asyncio.sleep(.005)
        assert await asyncio.wait_for(task, 4) == 0

    asyncio.run(scenario())
    assert "retrying" in repo.states, "包装异常必须判瞬态进入重试，而不是 fatal 退出"
    assert repo.states.index("retrying") > repo.states.index("connected")
    assert repo.states[-1] == "stopped" and stats["connects"] == 2


def test_permanent_error_chain_precedence_keeps_fault_classification():
    """链条判定不能放宽致命分类：认证/证书/配置类异常（即使被包装）仍必须 fatal。"""
    from app.telemetry.worker import permanent_error, transient_error

    class BadIdentityTokenRejected(Exception):
        pass

    try:
        raise BadIdentityTokenRejected("synthetic auth rejection")
    except BadIdentityTokenRejected as cause:
        try:
            raise Exception("wrapped transport noise") from cause
        except Exception as exc:
            wrapped_auth = exc
    assert permanent_error(wrapped_auth) is True

    from app.telemetry.opcua import ConfigurationError
    try:
        raise ConfigurationError("client certificate invalid")
    except ConfigurationError as cause:
        try:
            raise RuntimeError("lib wrapper") from cause
        except RuntimeError as exc:
            wrapped_cfg = exc
    assert permanent_error(wrapped_cfg) is True and transient_error(wrapped_cfg) is False
