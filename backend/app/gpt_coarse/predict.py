"""Nine observed factors, fixed expert level curve and bounded residual effects."""
import math

KIND = 'gpt-expert9-v1'
FEATURES = ['raw_ash', 'coal_amount', 'sysA', 'sysB', 'sys401', 'sys402',
            'desliming473', 'desliming474', 'level']
LABELS = ['原煤灰分', '小时带煤量', 'A系统', 'B系统', '401系统', '402系统',
          '473脱粉', '474脱粉', '精磁尾液位']


def number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def valid(model):
    if not isinstance(model, dict) or model.get('kind') != KIND or model.get('feature_names') != FEATURES:
        return False
    for key in ('reference', 'scale', 'coefs'):
        values = model.get(key)
        if not isinstance(values, list) or len(values) != 8 or not all(number(v) for v in values):
            return False
    bounds = model.get('bounds')
    return (all(v > 0 for v in model['scale']) and isinstance(bounds, list) and len(bounds) == 8
            and all(isinstance(b, list) and len(b) == 2 and all(number(v) for v in b)
                    and b[0] <= b[1] for b in bounds)
            and model.get('expert') == {'intercept': 30.5, 'slope': -0.3, 'level_range': [25, 75]}
            and model.get('correction_limit') == 2.0)


def explain(record, model):
    if not valid(model):
        return None
    values = [record.get(k) for k in FEATURES]
    if not all(number(v) for v in values):
        return None  # Missing live factors must not silently become training means.
    if not (0 < values[0] <= 100 and values[1] > 10 and 0 <= values[-1] <= 100
            and all(v in (0, 1) for v in values[2:8])):
        return None
    used = [min(b[1], max(b[0], v)) for v, b in zip(values[:8], model['bounds'])]
    terms = [c * (v-r)/s for c, v, r, s in zip(model['coefs'], used, model['reference'], model['scale'])]
    limit = model['correction_limit']
    correction = limit * math.tanh(sum(terms)/limit)
    # Outside the expert range: boundary estimate only; no unsupported linear extrapolation.
    level = min(75, max(25, values[-1]))
    baseline = 30.5 - 0.3 * level
    return dict(value=baseline+correction, baseline=baseline, correction=correction,
                outside_expert_range=level != values[-1],
                features=[dict(key=k, raw=raw, used=v, clipped=raw != v)
                          for k, raw, v in zip(FEATURES, values, used+[level])])


def predict(record, model):
    result = explain(record, model)
    return result['value'] if result else None
