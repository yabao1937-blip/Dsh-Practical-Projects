"""Bounded, process-local retrospective settlement for the read-only test server.

Only immutable production evidence is reused across assay revisions. Each caller
supplies a fresh read transaction; no sessions, SQLite copies or writes are kept.
"""
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
import json
from threading import RLock

from sqlalchemy import func, select

from ..decision_hourly.ash_settlement import (
    POLICY, build_settlement, get_settlement, latest_end, manual_basis, revision, source_revision,
)
from ..decision_hourly.models import DecisionAshSettlement
from ..decision_hourly.production_coverage import stopped_intervals
from ..gpt_runtime.settlement_kernel import load_rows
from ..telemetry.domain import iso_time


def _json_size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':'), sort_keys=True).encode('utf-8'))


@dataclass
class _Entry:
    start: object
    source_key: str
    rows: tuple
    stopped: list
    coverage_key: str
    coverage_sealed: bool
    evidence_bytes: int
    result_revision: str | None = None
    result: dict | None = None
    size: int = 0


class SettlementReplay:
    """Cache at most ten visible hours and 32 MiB of estimated JSON data.

    A single oversized hour is computed without retention. Optional ``basis``
    and ``source`` must come from the caller's current transaction, never a
    previous response. Results are detached copies, including on cache hits.
    """

    def __init__(self, *, max_hours=10, max_bytes=32*1024*1024):
        if type(max_hours) is not int or not 1 <= max_hours <= 10:
            raise ValueError('Replay cache must retain between one and ten hours')
        if type(max_bytes) is not int or max_bytes <= 0 or max_bytes > 32*1024*1024:
            raise ValueError('Replay cache size must be between one byte and 32 MiB')
        self._max_hours, self._max_bytes = max_hours, max_bytes
        self._entries, self._bytes, self._lock = OrderedDict(), 0, RLock()

    def _remove(self, key):
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._bytes -= entry.size

    def _prune(self, now):
        end = latest_end(now)
        for key, entry in list(self._entries.items()):
            if not end-timedelta(hours=10) <= entry.start < end:
                self._remove(key)

    def cache_info(self):
        with self._lock:
            return dict(hours=len(self._entries), estimated_bytes=self._bytes)

    def _coverage(self, db, start, source, rows):
        saved = db.execute(select(
            DecisionAshSettlement.snapshot['policy'].as_string(),
            DecisionAshSettlement.snapshot['production_source_revision'],
            DecisionAshSettlement.snapshot['stopped_intervals'],
            func.json_type(DecisionAshSettlement.snapshot, '$.stopped_intervals'))
            .where(DecisionAshSettlement.start == iso_time(start))).first()
        if saved and saved[0] == POLICY and saved[1] == source:
            stopped = [] if saved[3] is None else saved[2]
            if not isinstance(stopped, list):
                raise ValueError('Stored production coverage is malformed')
            return deepcopy(stopped), True
        choices = {json.dumps(row.snapshot.get('sources', {}).get('resolved_choices', {}),
                              sort_keys=True) for row in rows}
        sources = rows[0].snapshot.get('sources') if rows else None
        stopped = stopped_intervals(db, start, start+timedelta(hours=1), sources) if len(choices) <= 1 else []
        return stopped, False

    def get(self, db, start, now, defer_missing=False):
        """Keep valid host results; replay a stale visible hour without writes."""
        key = iso_time(start)
        with self._lock:
            self._prune(now)
            try:
                result = get_settlement(db, start, now, defer_missing=True)
                if not result.get('pending'):
                    return result
                return self.replay(db, start, now)
            except Exception:
                self._remove(key)
                raise

    def replay(self, db, start, now, basis=None, source=None):
        """Run original settlement math against bounded reusable evidence."""
        key = iso_time(start)
        with self._lock:
            self._prune(now)
            try:
                end = latest_end(now)
                if not end-timedelta(hours=10) <= start < end:
                    return get_settlement(db, start, now, defer_missing=True)
                basis = manual_basis(db, start, start+timedelta(hours=1), now) if basis is None else basis
                source = source_revision(db, start) if source is None else source
                source_key = json.dumps([POLICY, source], sort_keys=True, allow_nan=False)
                result_revision = revision(basis, source)
                entry = self._entries.get(key)
                if entry is not None and entry.source_key != source_key:
                    self._remove(key)
                    entry = None
                if entry is None:
                    rows = tuple(deepcopy(load_rows(db, start)))
                    stopped, sealed = self._coverage(db, start, source, rows)
                    evidence_bytes = _json_size(dict(policy=POLICY, source=source,
                        rows=[dict(slot=row.slot, sampled_at=row.sampled_at,
                                   fingerprint=row.fingerprint, snapshot=row.snapshot) for row in rows],
                        stopped=stopped))
                    entry = _Entry(start=start, source_key=source_key, rows=rows,
                        stopped=stopped, coverage_key=json.dumps(stopped, allow_nan=False),
                        coverage_sealed=sealed, evidence_bytes=evidence_bytes)
                elif not entry.coverage_sealed:
                    # Without a matching persisted settlement, production
                    # coverage still depends on mutable point definitions.
                    # Re-observe it even when chemistry/source revisions match.
                    stopped, sealed = self._coverage(db, start, source, entry.rows)
                    coverage_key = json.dumps(stopped, allow_nan=False)
                    if coverage_key != entry.coverage_key:
                        self._remove(key)
                        entry.evidence_bytes += _json_size(stopped)-_json_size(entry.stopped)
                        entry.stopped, entry.coverage_key = stopped, coverage_key
                        entry.result_revision, entry.result = None, None
                    entry.coverage_sealed = sealed
                if entry.result_revision == result_revision:
                    self._entries.move_to_end(key)
                    return deepcopy(entry.result)

                result = build_settlement(db, start, now, basis, source,
                                          rows=entry.rows, stopped=entry.stopped)
                result['calculation_origin'] = 'readonly-local-replay'
                result['item'].update(frozen=False, cached=False)
                result['item']['calculation_origin'] = 'readonly-local-replay'
                if result.get('policy') != POLICY or result.get('revision') != result_revision:
                    raise ValueError('Read-only settlement replay produced an incompatible result')
                result_bytes = _json_size(result)
                self._remove(key)
                entry.result_revision, entry.result = result_revision, deepcopy(result)
                entry.size = entry.evidence_bytes+result_bytes
                if entry.size <= self._max_bytes:
                    self._entries[key] = entry
                    self._bytes += entry.size
                    while len(self._entries) > self._max_hours or self._bytes > self._max_bytes:
                        self._remove(next(iter(self._entries)))
                return deepcopy(result)
            except Exception:
                # A failed revision must never make the preceding chemistry a
                # successful fallback, even if the evidence entry used to fit.
                self._remove(key)
                raise
