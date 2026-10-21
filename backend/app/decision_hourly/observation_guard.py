"""Small semantic configuration guard, excluding continuously arriving telemetry."""
import json
from hashlib import sha256

from sqlalchemy import select, func

from ..models import Setting, AutoState, CoarseModel, DecisionSourceRevision, ManualMeasurement
from ..gpt_coarse.models import GptCoarseRevision
from ..telemetry.models import TelemetryPoint
from ..services.resolvers import manual_valid
from ..services.state import SETTING_TO_STORE


CONFIGURATION_KEYS = (
    'amountInputs', 'ashInputs', 'instrumentInputs', 'heavyAshInput',
    'coarseAshInput', 'floatAshInput', 'densityActionLatch', 'densityLastMoveAt',
    'coarseModelVariants',
)


def manual_validity(values):
    """Automatic timestamps matter only when they change a manual override."""
    instruments = values.get('instrumentInputs') or {}
    instruments = instruments if isinstance(instruments, dict) else {}
    automatic = values.get('autoState') or {}
    automatic = automatic if isinstance(automatic, dict) else {}
    store = {'autoState': {key: stamp for key, stamp in automatic.items() if isinstance(stamp, dict)}}
    pairs = [(key, instruments.get(key) or {}) for key in ('level_tail', 'ash_502', 'scale_502')]
    pairs.append(('heavyAsh', values.get('heavyAshInput') or {}))
    return [(key, manual_valid(store, cfg if isinstance(cfg, dict) else {}, key)) for key, cfg in pairs]


def configuration_revision(db):
    # Never hash coal/telemetry histories, point.latest, leases, or heartbeats here.
    values = dict(db.execute(select(AutoState.key, AutoState.value).where(
        AutoState.key.in_((*CONFIGURATION_KEYS, 'autoState')))).all())
    # Ordinary browser mirrors recreate these rows without changing the model.
    model_columns = [column for column in CoarseModel.__table__.c
                     if column.key not in ('id', 'created_at', 'train_run_id')]
    payload = [
        [list(row) for row in db.execute(select(Setting.key, Setting.value, Setting.updated_at).where(
            Setting.key.in_(SETTING_TO_STORE)).order_by(Setting.key))],
        [list(row) for row in db.execute(select(TelemetryPoint.source_id, TelemetryPoint.key,
            TelemetryPoint.config, TelemetryPoint.enabled).order_by(
                TelemetryPoint.source_id, TelemetryPoint.key))],
        [dict(row) for row in db.execute(select(*model_columns).order_by(CoarseModel.method)).mappings()],
        db.scalar(select(func.max(DecisionSourceRevision.id))),
        db.scalar(select(func.max(GptCoarseRevision.id))),
        db.scalar(select(func.max(ManualMeasurement.id))),
        [(key, values[key]) for key in sorted(CONFIGURATION_KEYS) if key in values],
        manual_validity(values),
    ]
    return sha256(json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()
