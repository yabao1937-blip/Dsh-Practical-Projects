"""Arithmetic sample means and signed adjustments; deliberately no model fitting."""
import hashlib
import json
from copy import deepcopy
from math import fsum

from ..hourly_brief.inputs import business_time, numeric

CADENCE = 5
EXPECTED = 3600 // CADENCE
MIN_COVERAGE = .9
COVERAGE_POLICY = 'coverage-over-90-v1'
VALID = {'good', 'manual', 'historical', 'default', 'predicted', 'calculated'}
COMPUTED = {'coarse_ash', 'heavy_amount', 'total_ash'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def valid(c, key=''):
    v = c.get('value')
    return (c.get('status') in VALID and (numeric(v) or (':' in key and isinstance(v, bool)))
            and not (key in ('raw_ash', 'coal_amount') and v == 0)
            and not (key == 'density' and (v <= 0 or abs(v-1) <= 1e-9)))


def event_key(c):
    # A repeated manual value is one measurement, even when read by 720 decisions.
    return digest({k:c.get(k) for k in ('source_id', 'point_key', 'item_id', 'record_id',
        'metric', 'input_path', 'observed_at', 'created_at', 'source')})


def mean_cell(key, candidates, fallback, start, end):
    events = {}
    for c in candidates:
        stamp_value = c.get('_period_at') or c.get('observed_at')
        stamp = business_time(stamp_value) if stamp_value else None
        if stamp and start <= stamp < end:
            events[c.get('_event') or event_key(c)] = c
    accepted = [c for c in events.values() if valid(c, key)]
    result = {k:deepcopy(fallback.get(k)) for k in
              ('unit', 'source', 'source_id', 'point_key', 'item_id', 'density_label') if k in fallback}
    result.update(value=None, status='missing', observed_at=None, sample_count=len(accepted),
                  excluded_count=len(events)-len(accepted), carried=False)
    if accepted:
        result.update(value=fsum(float(c['value']) for c in accepted)/len(accepted), status='average',
            observed_at=max((c['observed_at'] for c in accepted if c.get('observed_at')),default=None),
            first_at=min((c['observed_at'] for c in accepted if c.get('observed_at')),default=None), reason=f'{len(accepted)} 条有效样本算术平均')
    elif key not in COMPUTED and fallback.get('status') in ('manual', 'historical', 'default') and valid(fallback, key):
        stamp = business_time(fallback.get('observed_at')) if fallback.get('observed_at') else None
        # Explicit carry/default, never silently count it as a new hourly measurement.
        if stamp is None or stamp < start:
            result.update(value=fallback['value'], status='carried', carried=True,
                observed_at=fallback.get('observed_at'), reason='本小时无新增采样，沿用此前最近值' if stamp else '本小时无采样，采用在线仪表默认值')
    if result['value'] is None:
        result['reason'] = ('尚无该小时有效的逐次计算记录' if key in COMPUTED else '该小时无有效采样')
    return result


def hour_advice(rows, start, end, density, max_step=.03):
    seen, accepted = set(), []
    for row in rows:
        snap = row.snapshot
        c, rho = snap.get('suggested_density', {}), snap.get('cells', {}).get('density', {})
        if row.fingerprint in seen or not valid(c) or not valid(rho, 'density'):
            continue
        seen.add(row.fingerprint)
        accepted.append((row, c['value']-rho['value']))
    slots = sorted({row.slot-int(start.timestamp())//CADENCE for row, _ in accepted})
    gaps = [b-a-1 for a,b in zip([-1]+slots, slots+[EXPECTED])]
    gap = max(gaps, default=EXPECTED)*CADENCE
    count = len(slots)
    deltas = [delta for _, delta in accepted]
    mean = fsum(deltas)/len(deltas) if deltas else None
    coverage = dict(valid_count=count, recorded_count=len(rows), expected_count=EXPECTED,
        ratio=count/EXPECTED, longest_gap_seconds=gap, required_ratio=MIN_COVERAGE,
        max_gap_seconds=None, required_ratio_exclusive=True, policy=COVERAGE_POLICY, cadence_seconds=CADENCE,
        up=sum(v > 1e-9 for v in deltas), down=sum(v < -1e-9 for v in deltas),
        stable=sum(abs(v) <= 1e-9 for v in deltas))
    reasons = []
    if count/EXPECTED <= MIN_COVERAGE:
        reasons.append(f'有效建议覆盖 {count}/{EXPECTED}（{count/EXPECTED:.1%}）；需超过 90%')
    if len({r.context_key for r in rows}) > 1:
        reasons.append('小时内数据来源、模型或调密参数发生变化，暂不合并建议')
    at = business_time(density.get('observed_at')) if density.get('observed_at') else None
    if not valid(density, 'density') or not at or not 0 < (end-at).total_seconds() <= 30:
        reasons.append('小时末 30 秒内没有有效实测密度')
    limit = min(.03, max_step) if numeric(max_step) and max_step > 0 else .03
    delta = max(-limit, min(limit, mean)) if mean is not None else None
    value = round(density['value']+delta, 3) if not reasons and delta is not None else None
    reason = '；'.join(reasons) if reasons else '上一小时各次建议调密量取平均，以小时末有效实测密度为基准'
    return dict(value=value, status='calculated' if value is not None else 'missing', reason=reason,
                unit='g/cm³', source='小时调密建议'), coverage, dict(
        rhoCur=density.get('value'), basis_at=density.get('observed_at'), mean_delta=mean,
        deltaRho=delta, rhoNew=value, maxStep=limit,
        direction='stable' if delta is None or abs(delta)<.0005 else 'up' if delta>0 else 'down', reason=reason)


def refresh_coverage_policy(result):
    """Apply today's coverage gate to a response copy of frozen statistics.

    No model replay or storage writes. Only the exact legacy coverage blocker can
    be removed; source/model/basis blockers and subsequent live guards remain.
    """
    coverage = result['coverage']
    if coverage.get('policy') == COVERAGE_POLICY:
        return
    count, expected = coverage['valid_count'], coverage['expected_count']
    ratio = count/expected
    old_reason = (f'有效建议覆盖 {count}/{expected}（{ratio:.1%}），连续缺口 '
        f'{coverage["longest_gap_seconds"]} 秒；需覆盖至少 90% 且缺口不超过 60 秒')
    coverage.update(ratio=ratio, required_ratio=MIN_COVERAGE, required_ratio_exclusive=True,
                    max_gap_seconds=None, policy=COVERAGE_POLICY)
    advice, guide = result['suggested_density'], result['guidance']
    reason = guide.get('reason', '')
    legacy_blocked = old_reason in reason
    if not legacy_blocked and ratio > MIN_COVERAGE:
        return  # No changed gate: preserve existing advice or other blockers.
    remaining = reason.replace(old_reason, '').strip('；') if legacy_blocked else (reason if advice['value'] is None else '')
    if ratio <= MIN_COVERAGE:
        required = f'有效建议覆盖 {count}/{expected}（{ratio:.1%}）；需超过 90%'
        remaining = required+('；'+remaining if remaining else '')
    value = None
    if not remaining:
        density, delta = result['density_basis'], guide.get('deltaRho')
        end = business_time(result['period']['end'])
        at = business_time(density.get('observed_at')) if density.get('observed_at') else None
        step = guide.get('maxStep', .03)
        if (valid(density,'density') and at and 0 < (end-at).total_seconds() <= 30
                and numeric(delta) and numeric(step) and 0 < step <= .03 and abs(delta) <= step):
            value = round(density['value']+delta, 3)
        else:
            remaining = '小时末密度基准或已存建议偏移无效，暂缓建议'
    reason = remaining or '上一小时各次建议调密量取平均，以小时末有效实测密度为基准'
    # Keep any explanatory suffix (e.g. expert guidance validation status).
    suffix = advice.get('reason','').removeprefix(guide.get('reason',''))
    if not suffix.startswith('；'):
        suffix = ''
    result['suggested_density'] = dict(advice, value=value,
        status='calculated' if value is not None else 'missing', reason=reason+suffix)
    result['cells']['suggested_density'] = result['suggested_density']
    guide.update(rhoNew=value, reason=reason)
