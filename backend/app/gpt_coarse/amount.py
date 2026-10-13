"""GPT-only coarse product rate derived from the same wash-feed observation."""
from copy import deepcopy
from datetime import datetime
from math import isfinite

POLICY = dict(version='wash-coarse-ratio-v1', input='coal_amount', divisor=40)
FORMULA = '入洗原煤量 ÷ 40'


def from_wash(coal, now):
    coal = coal or {}
    result = deepcopy(coal)
    value = coal.get('value')
    status = coal.get('status')
    reason = None
    if status not in ('good', 'manual', 'historical', 'default', 'held', 'stopped', 'calculated') or not (
            isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value >= 0):
        reason = '入洗原煤量不可用：' + (coal.get('reason') or '无有效读数')
    elif coal.get('unit') != 't/h':
        reason = '入洗原煤量单位应为 t/h'
    elif status == 'good':
        try:
            age = (now-datetime.fromisoformat(coal.get('observed_at') or '')).total_seconds()
            if age > 30 or age < -5:
                reason = '入洗原煤量采集数据已过期或时钟异常'
        except (ValueError, TypeError):
            reason = '入洗原煤量采集时间缺失或无效'
    stopped = coal.get('raw_value') == 0 or status == 'stopped' or value == 0
    result.update(value=None if reason else value / POLICY['divisor'], unit='t/h',
                  status='missing' if reason else 'stopped' if stopped else 'held' if status == 'held' else 'calculated',
                  source='计算 · ' + FORMULA, derivation=deepcopy(POLICY),
                  reason=reason or ('设备停机 · 按入洗量沿用值计算，仅供观察' if stopped else
                                    '入洗量沿用值计算，仅供观察' if status == 'held' else '入洗：粗精 = 40：1'))
    return result
