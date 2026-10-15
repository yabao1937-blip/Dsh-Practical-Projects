"""Read-only action context for an hourly historical reference, independent of live quality."""
import json

from sqlalchemy import select

from ..decision_hourly.models import DensityAction
from ..hourly_brief.inputs import Inputs, numeric
from ..telemetry.derived import DENSITY_AVERAGE_MEMBERS
from ..telemetry.domain import definition_id, iso_time
from ..telemetry.models import TelemetryPoint
from .execution import EPSILON


def action_context(db, sources, since, now, provisional):
    """Only recorded moves on the selected route after the ash production hour.

    Match point definitions as well as names. A stale/missing latest reading must
    not conceal recorded moves; nor may another source's moves create a warning.
    """
    choice = (sources.get('resolved_choices') or {}).get('density-current')
    identities = {}
    if choice:
        source, key = json.loads(choice)
        actual = db.get(TelemetryPoint, (source, key))
        item = (actual.config.get('item_id') or key) if actual else key
        inputs = Inputs(db, now, authoritative=True)
        for channel, tag in DENSITY_AVERAGE_MEMBERS:
            if item not in (tag, 'a303_a304_density_average'):
                continue
            point = inputs.point(tag+'_SET', source)
            if point:
                identities[channel] = (point.source_id, point.key, definition_id(point.config))
    actions = []
    if identities:
        query = select(DensityAction).where(DensityAction.detected_at >= iso_time(since),
            DensityAction.detected_at <= iso_time(now), DensityAction.channel.in_(identities))
        for row in db.scalars(query.order_by(DensityAction.detected_at, DensityAction.id)):
            evidence = row.evidence or {}
            before = evidence.get('before', {}).get('pair', {}).get('setpoint', {})
            after = evidence.get('after', {}).get('pair', {}).get('setpoint', {})
            identity = tuple(after.get(k) for k in ('source_id', 'point_key', 'definition_id'))
            if (identity != identities[row.channel] or not numeric(before.get('value'))
                    or not numeric(after.get('value')) or not 1 < before['value'] < 3
                    or not 1 < after['value'] < 3 or abs(after['value']-before['value']) < EPSILON):
                continue
            actions.append(dict(action_id=row.id, channel=row.channel, detected_at=row.detected_at,
                before=before['value'], after=after['value'],
                before_at=before.get('received_at'), after_at=after.get('received_at'),
                continuous=evidence.get('continuous'), evidence='recorded-action'))
    messages = []
    if actions:
        messages.append(f'灰分对应时段结束后检测到 {len(actions)} 次设定密度变化，实际调密时间需核对；该灰分可能尚未反映这些变化，再次调密前请核对已执行调整')
    if provisional:
        messages.append('小时灰分含沿用化验值，补录对应时段化验后将更新参考目标')
    return dict(since=iso_time(since), through=iso_time(now), actions=actions,
        action_count=len(actions), last_action_at=actions[-1]['detected_at'] if actions else None,
        provisional=bool(provisional), messages=messages, blocks_advice=False)
