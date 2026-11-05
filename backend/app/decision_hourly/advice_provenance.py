"""Select recorded advice without mistaking historical replay time for existence.

No row in the legacy table records physical insertion/delivery time.  An absent
origin is the online writer's default, not proof that an operator saw the value.
"""
from copy import deepcopy

from ..hourly_brief.inputs import business_time
from ..telemetry.domain import iso_time

POLICY = 'recorded-advice-provenance-v1'
REPLAY_ORIGIN = 'advice-backfill-v1'


def origin_class(point):
    if point.get('origin') == REPLAY_ORIGIN:
        return 'historical-replay'
    return 'online-default' if 'origin' not in point else 'unknown'


def select_recorded(points, apply_start, through):
    """Last eligible revision, including missing/unknown rows; never older valid.

    Replay rows are not online observations at all. Unknown-origin rows remain
    barriers so that an earlier valid online value cannot silently replace them.
    Input order breaks equal-time ties, matching the indexed API's id ordering.
    """
    hour, cutoff = iso_time(business_time(apply_start)), business_time(through)
    selected, selected_at, ignored = None, None, 0
    for point in points:
        try:
            at = business_time(point.get('at') or point['generated_at'])
            key = iso_time(business_time(point['apply_start']))
        except (KeyError, TypeError, ValueError):
            continue
        if key != hour or at > cutoff:
            continue
        if origin_class(point) == 'historical-replay':
            ignored += 1
            continue
        if selected_at is None or at >= selected_at:
            selected, selected_at = point, at
    return selected, ignored


def public_advice(point):
    if point is None or origin_class(point) != 'online-default':
        return None
    return {**{key: deepcopy(point.get(key)) for key in
        ('value', 'apply_start', 'status', 'reason', 'origin', 'policy', 'target', 'ash', 'basis')},
        'generated_at': point.get('at') or point.get('generated_at'),
        'origin_class': 'online-default', 'physical_availability': 'not-recorded'}
