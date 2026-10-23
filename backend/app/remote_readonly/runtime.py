"""Coalesce local test-server decisions without holding waiting DB sessions.

Decision responses expire after five seconds; hour tables after one. A bounded evidence cache
supports local retrospective replay without database writes. Production routes
do not use either cache; expired responses never mask a failed host.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import json
import threading
import time

from fastapi import HTTPException
from sqlalchemy import text


# Limit cache producers before they acquire a pooled connection. Short display
# reads (including the projected hour table) keep an independent lane from the
# current/hourly calculation. The third connection remains available to
# explicit, uncached queries.
_READ_LANES = {name: threading.BoundedSemaphore(1) for name in ('display', 'calculation')}


@contextmanager
def read_window(kind):
    with _READ_LANES[kind]:
        yield


class ReadOnlyRuntimeUnavailable(Exception):
    pass


class SingleFlightBundle:
    def __init__(self, build, *, ttl_seconds=5, wait_seconds=25,
                 failure_seconds=5, clock=time.monotonic):
        self.build = build
        self.ttl_seconds = ttl_seconds
        self.wait_seconds = wait_seconds
        self.failure_seconds = failure_seconds
        self.clock = clock
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='readonly-decisions')
        self._future = None
        self._cached = None
        self._expires = 0
        self._failed_until = 0

    def _completed(self, future):
        with self._lock:
            if self._future is not future:
                return
            try:
                result = future.result()
            except Exception:
                self._cached = None
                self._failed_until = self.clock() + self.failure_seconds
            else:
                self._cached = result
                self._expires = self.clock() + self.ttl_seconds
                self._failed_until = 0
            finally:
                self._future = None

    def get(self):
        with self._lock:
            now = self.clock()
            if now < self._failed_until:
                raise ReadOnlyRuntimeUnavailable('Read-only decision calculation is temporarily unavailable')
            if self._cached is not None and now < self._expires:
                return deepcopy(self._cached)
            if self._future is None:
                # Drop expired results before starting a replacement.  A failure
                # must never relabel an old calculation as the current result.
                self._cached = None
                self._future = self._executor.submit(self.build)
                self._future.add_done_callback(self._completed)
            future = self._future
            # A very fast build may finish synchronously in add_done_callback.
            if future is None:
                if self._cached is not None:
                    return deepcopy(self._cached)
                raise ReadOnlyRuntimeUnavailable('Read-only decision calculation failed')
        try:
            result = future.result(timeout=self.wait_seconds)
            with self._lock:
                if self._future is future:
                    self._completed(future)
            return deepcopy(result)
        except FutureTimeout:
            with self._lock:
                self._failed_until = max(self._failed_until, self.clock() + self.failure_seconds)
            # Do not launch another producer or cancel its live read transaction;
            # the existing producer releases its session in its own finally path.
            raise ReadOnlyRuntimeUnavailable('Read-only decision calculation is still busy') from None
        except Exception:
            with self._lock:
                if self._future is future and future.done():
                    self._completed(future)
            raise ReadOnlyRuntimeUnavailable('Read-only decision calculation failed') from None

    def close(self):
        self._executor.shutdown(wait=True, cancel_futures=True)


def host_now(db):
    """Read the production host clock through the current read transaction."""
    stamp = db.execute(text("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')")).scalar_one()
    observed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    if observed.tzinfo is None:
        raise ValueError('Read-only host did not return an aware timestamp')
    return observed


_settlement_replay = None
_replay_lock = threading.Lock()


def settlement_replay():
    """Lazy, bounded derived evidence only; never a second database or writer."""
    global _settlement_replay
    with _replay_lock:
        if _settlement_replay is None:
            from .settlement_replay import SettlementReplay
            _settlement_replay = SettlementReplay()
        return _settlement_replay


def _build_bundle():
    from ..database import SessionLocal
    from ..services.decision_sources import shared_snapshot
    from ..decision_hourly.service import read_hour
    from ..gpt_runtime.decision_store import load_decision_store

    with read_window('calculation'), SessionLocal() as db:
        db.execute(text('BEGIN'))
        # SQLite evaluates this on the host, so laptop clock skew does not make
        # production inputs appear expired or select a different published hour.
        observed = host_now(db)
        from .live_cache import attach_runtime
        attach_runtime(db)
        current = shared_snapshot(db, when=observed, live=True, store_loader=load_decision_store, realtime=True)
        hourly = read_hour(db, now=observed, live=current, defer_missing=True,
                           store_loader=load_decision_store,
                           settlement_reader=settlement_replay().get)
        # Cache detached JSON only, never ORM objects, connections or raw tables.
        return json.loads(json.dumps({'current': current, 'hourly': hourly}, allow_nan=False))


_decisions = SingleFlightBundle(_build_bundle)


def _build_recent_hours():
    """Only the default published-hour table shares a bounded read snapshot."""
    from ..database import SessionLocal
    from .settled_hours import read_settled_hours

    with read_window('display'), SessionLocal() as db:
        db.execute(text('BEGIN'))
        result = read_settled_hours(db, now=host_now(db))
    if not any(item.get('pending') for item in result.get('items', [])[-10:]):
        return json.loads(json.dumps(result, allow_nan=False))
    # Release the short display lane and its snapshot before a cold replay. It
    # shares the calculation lane with hourly advice, leaving health reads free
    # and avoiding duplicate hydrations or a DB checkout waiting on the cache.
    with read_window('calculation'), SessionLocal() as db:
        db.execute(text('BEGIN'))
        result = read_settled_hours(db, now=host_now(db),
                                   settlement_reader=settlement_replay().replay)
    return json.loads(json.dumps(result, allow_nan=False))


# The page checks every 5–10 seconds. Keep only one second of result reuse so
# an assay arriving just after a response does not lose another five seconds
# before a cold ten-hour replay; immutable evidence has its own bounded cache.
_recent_hours = SingleFlightBundle(_build_recent_hours, ttl_seconds=1, failure_seconds=1)


def recent_hours_view():
    try:
        return _recent_hours.get()
    except ReadOnlyRuntimeUnavailable:
        raise HTTPException(503, detail={
            'error_code': 'readonly_recent_hours_busy',
            'message': '测试服正在读取主机小时灰分，请稍后重试',
        }) from None


def decision_view(kind):
    if kind not in ('current', 'hourly'):
        raise ValueError('Unknown read-only decision view')
    try:
        return _decisions.get()[kind]
    except ReadOnlyRuntimeUnavailable:
        raise HTTPException(503, detail={
            'error_code': 'readonly_decision_busy',
            'message': '测试服正在读取主机数据或主机暂不可用，请稍后重试',
        }) from None
