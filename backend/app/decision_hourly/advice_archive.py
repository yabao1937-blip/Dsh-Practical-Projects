"""Archive the real hourly policy, never reconstruct past recommendations."""
from copy import deepcopy
from sqlalchemy import select, text

from ..database import SessionLocal, REMOTE_READONLY
from ..gpt_density.hourly_anchor import hour_start
from ..telemetry.domain import iso_time, utc_now
from .models import GptHourlyDensityAdvice, GptHourlyDensityAnchor, DecisionAshSettlement
from .statistics import digest


def compact(result):
    # Exclude volatile live execution/read times: those are not hourly advice.
    density = (result.get('advice_inputs') or {}).get('density') or {}
    ash = (result.get('advice_inputs') or {}).get('ash') or {}
    basis = density.get('execution_basis') or {}
    return dict(policy=result.get('advice_policy'), value=(result.get('suggested_density') or {}).get('value'),
        status=(result.get('suggested_density') or {}).get('status', 'pending'),
        reason=(result.get('suggested_density') or {}).get('reason') or result.get('reason', ''),
        target=deepcopy(result.get('target')), blockers=deepcopy(result.get('advice_blockers', [])),
        ash={key:deepcopy(ash.get(key)) for key in ('start','end','value','revision','provisional','coverage')},
        basis=dict(value=density.get('value'), captured_at=density.get('captured_at'),
            channels=sorted((basis.get('components') or {}).keys())))


def save_result(db, now, result):
    """Caller serializes writers. Identical ticks are not duplicate revisions."""
    key = iso_time(hour_start(now))
    payload = compact(result)
    fingerprint = digest(payload)
    previous = db.scalar(select(GptHourlyDensityAdvice).where(GptHourlyDensityAdvice.apply_start == key)
                         .order_by(GptHourlyDensityAdvice.id.desc()).limit(1))
    if previous and (previous.fingerprint == fingerprint or previous.generated_at >= iso_time(now)):
        return None
    row = GptHourlyDensityAdvice(apply_start=key, generated_at=iso_time(now),
                                fingerprint=fingerprint, snapshot=payload)
    db.add(row)
    return row


def archive_tick(sessions=SessionLocal, *, now=None):
    if REMOTE_READONLY:
        return  # A test server must never record today's test policy on the host.
    from .service import read_hour
    from .observation_guard import configuration_revision
    from ..gpt_runtime.decision_store import load_decision_store
    from ..services.decision_sources import shared_snapshot
    from .ash_settlement import latest_end
    explicit_time = now is not None
    now = now or utc_now()
    from datetime import timedelta
    def evidence_version(db):
        return (db.scalar(select(DecisionAshSettlement.generated_at).where(
            DecisionAshSettlement.start == iso_time(latest_end(now)-timedelta(hours=1)))),
            db.scalar(select(GptHourlyDensityAnchor.captured_at).where(
                GptHourlyDensityAnchor.start == iso_time(hour_start(now)))))
    with sessions() as db:
        if db.bind.dialect.name == 'sqlite':
            db.execute(text('BEGIN'))
        revision = configuration_revision(db)
        evidence = evidence_version(db)
        current = shared_snapshot(db, when=now, live=True, store_loader=load_decision_store, realtime=True)
        result = read_hour(db, now=now, live=current, defer_missing=True, store_loader=load_decision_store)
    # All calculations run outside the write lock. A changed configuration or
    # hour discards this preparation; the next worker tick will calculate again.
    with sessions() as db:
        if db.bind.dialect.name == 'sqlite':
            db.execute(text('BEGIN IMMEDIATE'))
        if (configuration_revision(db) != revision or evidence_version(db) != evidence
                or (not explicit_time and hour_start(utc_now()) != hour_start(now))):
            return
        save_result(db, now, result)
        db.commit()
