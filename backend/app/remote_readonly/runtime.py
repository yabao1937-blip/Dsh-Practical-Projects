"""Coalesce local test-server decisions without holding waiting DB sessions.

Only JSON responses are held in memory for five seconds.  Production routes do
not use this cache, and an expired response is never a fallback for a failed host.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from copy import deepcopy
from datetime import datetime
import json
import threading
import time

from fastapi import HTTPException
from sqlalchemy import text


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


def _build_bundle():
    from ..database import SessionLocal
    from ..services.decision_sources import shared_snapshot
    from ..decision_hourly.service import read_hour

    with SessionLocal() as db:
        db.execute(text('BEGIN'))
        # SQLite evaluates this on the host, so laptop clock skew does not make
        # production inputs appear expired or select a different published hour.
        observed = host_now(db)
        current = shared_snapshot(db, when=observed, live=True)
        hourly = read_hour(db, now=observed, live=current, defer_missing=True)
        # Cache detached JSON only, never ORM objects, connections or raw tables.
        return json.loads(json.dumps({'current': current, 'hourly': hourly}, allow_nan=False))


_decisions = SingleFlightBundle(_build_bundle)


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
