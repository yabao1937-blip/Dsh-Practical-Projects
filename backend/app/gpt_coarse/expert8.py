"""Eight-factor GPT inference, shared by the frozen experiment and live runtime."""
import math

from .predict import number

KIND = 'gpt-expert8-no-raw-v1'
FEATURES = ['coal_amount', 'sysA', 'sysB', 'sys401', 'sys402',
            'desliming473', 'desliming474', 'level']
LABELS = ['小时带煤量', 'A系统', 'B系统', '401系统', '402系统',
          '473脱粉', '474脱粉', '精磁尾液位']


def valid(model):
    if not isinstance(model, dict) or model.get('kind') != KIND or model.get('feature_names') != FEATURES:
        return False
    for key in ('reference', 'scale', 'coefs'):
        values = model.get(key)
        if not isinstance(values, list) or len(values) != 7 or not all(number(v) for v in values):
            return False
    bounds = model.get('bounds')
    return (all(v > 0 for v in model['scale']) and isinstance(bounds, list) and len(bounds) == 7
            and all(isinstance(b, list) and len(b) == 2 and all(number(v) for v in b)
                    and b[0] <= b[1] for b in bounds)
            and model.get('expert') == {'intercept': 30.5, 'slope': -0.3, 'level_range': [25, 75]}
            and model.get('correction_limit') == 2.0)


def explain(record, model):
    if not valid(model):
        return None
    values = [record.get(k) for k in FEATURES]
    if not all(number(v) for v in values):
        return None
    if not (values[0] > 10 and 0 <= values[-1] <= 100 and all(v in (0, 1) for v in values[1:7])):
        return None
    used = [min(b[1], max(b[0], v)) for v, b in zip(values[:7], model['bounds'])]
    terms = [c*(v-r)/s for c, v, r, s in zip(model['coefs'], used, model['reference'], model['scale'])]
    correction = 2*math.tanh(sum(terms)/2)
    level = min(75, max(25, values[-1]))
    baseline = 30.5-.3*level
    return dict(value=baseline+correction, baseline=baseline, correction=correction,
                outside_expert_range=level != values[-1],
                features=[dict(key=k, raw=raw, used=v, clipped=raw != v)
                          for k, raw, v in zip(FEATURES, values, used+[level])])


def predict(record, model):
    detail = explain(record, model)
    return detail['value'] if detail else None
