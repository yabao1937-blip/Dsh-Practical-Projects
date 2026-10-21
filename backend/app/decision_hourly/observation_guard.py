"""Small semantic configuration guard, excluding continuously arriving telemetry."""
import json
from hashlib import sha256

from sqlalchemy import select, func

from ..models import Setting, AutoState, CoarseModel, DecisionSourceRevision, ManualMeasurement
from ..gpt_coarse.models import GptCoarseRevision
from ..telemetry.models import TelemetryPoint
from ..services.state import SETTING_TO_STORE, AUTO_STATE_KEYS


def configuration_revision(db):
    # Never hash coal/telemetry histories, point.latest, leases, or heartbeats here.
    payload = [
        list(db.execute(select(Setting.key, Setting.value, Setting.updated_at).where(
            Setting.key.in_(SETTING_TO_STORE)).order_by(Setting.key))),
        list(db.execute(select(AutoState.key, AutoState.updated_at).where(
            AutoState.key.in_(AUTO_STATE_KEYS)).order_by(AutoState.key))),
        list(db.execute(select(TelemetryPoint.source_id, TelemetryPoint.key,
            TelemetryPoint.config, TelemetryPoint.enabled).order_by(
                TelemetryPoint.source_id, TelemetryPoint.key))),
        [dict(row) for row in db.execute(select(CoarseModel.__table__).order_by(CoarseModel.id)).mappings()],
        db.scalar(select(func.max(DecisionSourceRevision.id))),
        db.scalar(select(func.max(GptCoarseRevision.id))),
        db.scalar(select(func.max(ManualMeasurement.id))),
        [list(row) for row in db.execute(select(AutoState.key, AutoState.value).where(
            AutoState.key.in_(['amountInputs', 'ashInputs', 'instrumentInputs', 'heavyAshInput',
                'coarseAshInput', 'floatAshInput', 'densityActionLatch', 'densityLastMoveAt']))
            .order_by(AutoState.key))],
    ]
    # SQLAlchemy Row needs an explicit list; its repr is not a stable wire format.
    payload[:3] = [[list(row) for row in rows] for rows in payload[:3]]
    return sha256(json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()
