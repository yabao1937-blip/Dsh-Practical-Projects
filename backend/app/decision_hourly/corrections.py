"""Explicit, audited corrections of frozen advice; never replay historical models."""
from copy import deepcopy
from types import SimpleNamespace

from ..telemetry.domain import parse_time
from .evidence import PROVISIONAL
from .statistics import digest, hour_advice

VERSION = 'missing-level-context-v1'
CONTEXT_HOLD = '小时内数据来源、模型或调密参数发生变化，暂不合并建议'


def correct_missing_level_context(summary, rows, contexts, corrected_at):
    """Only repair bracketed, invalid level-gap rows with one omitted definition.

    Callers must explicitly select an hour, back up its snapshot and compare its
    hash under a write lock. Original rows/contexts and all other aggregates stay
    intact. Normal reads never call this function.
    """
    if any(c.get('version') == VERSION for c in summary.get('corrections', [])):
        return deepcopy(summary)
    if (summary['suggested_density']['value'] is not None
            or summary['guidance']['reason'] != CONTEXT_HOLD):
        raise ValueError('该小时并非仅被上下文变化拦截')
    base_key = summary['context_key']
    base = contexts[base_key]
    rows = sorted(rows, key=lambda r:r.slot)
    start, end = (parse_time(summary['period'][k]) for k in ('start', 'end'))
    if not rows or any(not start <= parse_time(r.sampled_at) < end for r in rows):
        raise ValueError('逐次记录时段不符')
    if any(digest(contexts[r.context_key]) != r.context_key for r in rows):
        raise ValueError('上下文摘要不符')
    affected = []
    for index, row in enumerate(rows):
        if row.context_key == base_key:
            continue
        level = row.snapshot['cells']['level']
        if (level.get('status') != 'missing' or level.get('value') is not None
                or level.get('definition_id') or (level.get('sample') or {}).get('definition_id')
                or '目标时刻之前 30 秒内无采集记录' not in level.get('reason', '')
                or row.snapshot['suggested_density']['value'] is not None):
            raise ValueError('异常上下文包含非缺样记录或有效建议')
        identity = str([level.get('source_id'), level.get('point_key'), level.get('item_id')])
        expected = deepcopy(base)
        definition = expected['point_definitions'].pop(identity, None)
        if (not definition or definition['unit'] != level.get('unit')
                or expected != contexts[row.context_key]):
            raise ValueError('存在真实来源、模型、参数或点位定义变化')
        # A missing run must be enclosed by the same observed point definition.
        for direction in (-1, 1):
            neighbor = index + direction
            while 0 <= neighbor < len(rows) and rows[neighbor].context_key != base_key:
                neighbor += direction
            if not 0 <= neighbor < len(rows):
                raise ValueError('缺样段未被同口径有效液位记录前后确认')
            observed = rows[neighbor].snapshot['cells']['level']
            if (observed.get('status') != 'good'
                    or [observed.get(k) for k in ('source_id','point_key','item_id')]
                    != [level.get(k) for k in ('source_id','point_key','item_id')]
                    or observed.get('definition_id') != definition['definition_id']):
                raise ValueError('缺样前后液位定义未获确认')
        affected.append(row.slot)
    if not affected:
        raise ValueError('没有可订正的缺样上下文')
    normalized = [SimpleNamespace(slot=r.slot, fingerprint=r.fingerprint,
        context_key=base_key, snapshot=r.snapshot) for r in rows]
    advice, coverage, guide = hour_advice(normalized, start, end, summary['density_basis'],
        (base.get('density_guide') or {}).get('maxStep', .03))
    if coverage != summary['coverage'] or advice['value'] is None:
        raise ValueError('覆盖情况变化或仍不满足建议门槛，不能订正')
    advice['reason'] += '；'+PROVISIONAL
    result = deepcopy(summary)
    result['suggested_density'] = advice
    result['cells']['suggested_density'] = deepcopy(advice)
    result['guidance'].update(guide)
    result.setdefault('corrections', []).append(dict(version=VERSION, corrected_at=corrected_at,
        original_digest=digest(summary), affected_slots=affected,
        reason='短暂缺少提前90秒液位，漏记点位定义；仅订正小时建议，不重跑模型或补造采样',
        original_advice=deepcopy(summary['suggested_density'])))
    return result
