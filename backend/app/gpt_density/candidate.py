"""Explicit, reproducible density experiment; never writes or replaces a policy.

The defaults are engineering trial settings, not fitted/validated optima.  A
complete post-action production window proves coverage, not physical stability.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
from math import isfinite

from ..hourly_brief.inputs import business_time
from ..telemetry.domain import iso_time

POLICY = 'density-candidate-v1'


@dataclass(frozen=True)
class CandidateParameters:
    max_step: float = .010
    provisional_step: float = .005
    provisional_mode: str = 'cautious'
    min_coverage: float = .8
    actual_follow_tolerance: float = .010
    max_age_seconds: float = 30
    follow_grace_seconds: float = 900
    actual_degraded_age_seconds: float = 300
    max_action_gap_seconds: float = 180
    rho_min: float = 1.35
    rho_max: float = 1.60

    def __post_init__(self):
        for key, value in asdict(self).items():
            if key != 'provisional_mode' and not number(value):
                raise ValueError('候选参数必须为有限数值：'+key)
        if not (0 < self.provisional_step <= self.max_step <= .03):
            raise ValueError('候选步长须为 0 < 暂估步长 <= 常规步长 <= 0.03')
        if self.provisional_mode not in ('cautious', 'hold'):
            raise ValueError('provisional_mode 须为 cautious 或 hold')
        if not (0 < self.min_coverage < 1 and 0 < self.max_age_seconds <= 60
                and 0 < self.actual_follow_tolerance <= .03
                and 30 <= self.max_action_gap_seconds <= 3600
                and 1 < self.rho_min < self.rho_max < 3):
            raise ValueError('候选质量、时效或运行范围参数无效')
        # 2026-11-13 三态口径：跟随容差放宽到一个常规步长（稳态 |实测−设定| 中位 0.0059，
        # 旧值 0.005 低于噪声地板）；新增跟随兜底与降级时效参数。
        if not (30 <= self.follow_grace_seconds <= 7200
                and self.max_age_seconds < self.actual_degraded_age_seconds <= 1800):
            raise ValueError('跟随兜底或降级时效参数无效')


def number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and isfinite(value)


def timestamp(value):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        result = business_time(value)
        return result if result.tzinfo is not None else None
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None


def evaluate_candidate(evidence, parameters=None):
    """Pure JSON-in/JSON-out. Missing evidence stays unknown, never zero-filled.

    Required evidence: evaluated_at, reference (value/status/identity/availability/
    generated_at/target/ash), current_setpoint, actual_density, operating,
    identity_known=True, action_history_status='good', latest_action or None.
    Current setpoint may wrap its A304 cell in components. Cells carry value,
    status, observed_at, unit, source_id, point_key, definition_id and batch_id.
    """
    p = parameters if isinstance(parameters, CandidateParameters) else CandidateParameters(**(parameters or {}))
    e = deepcopy(evidence)
    now = timestamp(e.get('evaluated_at'))
    ref = deepcopy(e.get('reference') or {})
    target, ash = ref.get('target') or {}, ref.get('ash') or {}
    setpoint = e.get('current_setpoint') or {}
    parts = setpoint.get('components')
    cell = (parts.get('A304') or {}) if isinstance(parts, dict) else setpoint
    raw = cell.get('value')
    # MD_SET is a three-decimal operator setting transported as a PLC float.
    current = round(raw, 3) if number(raw) else None
    actual, operating = e.get('actual_density') or {}, e.get('operating') or {}
    result = dict(experimental=True, policy=POLICY, mode=e.get('mode', 'current'),
        evaluated_at=iso_time(now) if now else e.get('evaluated_at'),
        setting=dict(value=current, raw_value=raw, at=cell.get('observed_at'), channel='A304'),
        reference=ref, basis=dict(ash=deepcopy(ash), target=deepcopy(target), band='unknown'),
        candidate=dict(value=None, status='unavailable', delta=None, reason_codes=[], reasons=[],
            rho_target=None, target_direction=None),
        observation=dict(state='unknown', action_id=None, before_at=None, after_at=None,
            ash_fully_after_action=None, actual_response='unknown'),
        quality=dict(level='unavailable', notes=[]), parameters=asdict(p),
        limitations=['仅测试候选；参数未标定，未证明统计最优或因果收益',
            '单次实测只用于当前读数跟随判断，不证明过程稳定或响应时间常数',
            '完整调后生产窗表示数据覆盖，不表示物理响应完成'])

    def finish(status, code, reason, value=None):
        result['candidate'].update(status=status, value=value,
            delta=round(value-current, 6) if value is not None and current is not None else None,
            reason_codes=[code], reasons=[reason])
        return result

    def good(c):
        at = timestamp(c.get('observed_at'))
        return (now is not None and at is not None and c.get('status') == 'good'
            and number(c.get('value')) and 1 < c['value'] < 3
            and c.get('unit') in ('g/cm³', 'g/cm3')
            and 0 <= (now-at).total_seconds() <= p.max_age_seconds)

    def usable(c):
        """2026-11-13 三态口径：30 秒内为新鲜；5 分钟内同观测的 good 读数降级可用。"""
        if good(c):
            return 'fresh'
        at = timestamp(c.get('observed_at'))
        if (c.get('status') == 'good' and at is not None and now is not None
                and 0 <= (now-at).total_seconds() <= p.actual_degraded_age_seconds
                and number(c.get('value')) and 1 < c['value'] < 3
                and c.get('unit') in ('g/cm³', 'g/cm3')):
            return 'degraded'
        return None

    actual_at = timestamp(actual.get('observed_at'))
    actual_age = (now-actual_at).total_seconds() if now and actual_at else None
    actual_state = usable(actual)
    actual_usable = actual_state is not None
    actual_status = ('good' if actual_state == 'fresh' else 'degraded' if actual_state == 'degraded' else
        'future' if actual_age is not None and actual_age < 0 else
        'stale' if actual_age is not None and actual_age > p.max_age_seconds else
        actual.get('status') if actual.get('status') not in (None, 'good') else 'invalid')
    result['observation']['actual_reading'] = dict(
        value=actual['value'] if actual_usable else None,
        raw_value=actual.get('value') if number(actual.get('value')) else None,
        observed_at=actual.get('observed_at'), status=actual_status,
        source_status=actual.get('status'), usable=actual_usable,
        age_seconds=round(actual_age, 3) if actual_age is not None else None,
        difference_from_setting=round(actual['value']-current, 6)
            if actual_usable and current is not None else None,
        tolerance=p.actual_follow_tolerance)

    if not now:
        return finish('unavailable', 'invalid_time', '缺少有效评估时刻')
    if parts is not None and sorted(parts) != ['A304']:
        return finish('unavailable', 'different_route', '候选仅适用于 A304 独立设定，不混用其它通道')
    if usable(cell) is None or current is None or not p.rho_min <= current <= p.rho_max:
        return finish('unavailable', 'setpoint_unavailable', '当前 A304 设定密度缺失、过期、未来或超范围')
    if usable(actual) is None:
        return finish('unavailable', 'actual_unavailable', '缺少新鲜有效的当前实测密度，不能评价下一步')
    if 'degraded' in (usable(cell), usable(actual)):
        result['quality']['notes'].append('实测/设定读数超过30秒（5分钟内同批），降级采用')
    if not e.get('identity_known') or not all(cell.get(k) for k in ('source_id', 'point_key', 'definition_id')):
        return finish('unavailable', 'identity_unknown', '来源、通道或点位定义证据不足')
    if (actual.get('source_id') != cell.get('source_id') or not cell.get('batch_id')
            or actual.get('batch_id') != cell.get('batch_id')
            or actual.get('observed_at') != cell.get('observed_at')):
        return finish('unavailable', 'batch_mismatch', '实测与设定不是同源同批同观测时刻')
    if operating.get('status') != 'good' or operating.get('stopped') not in (True, False, 0, 1):
        # 2026-11-13 三态口径：开停机证据缺失=未知态，保持当前设定并披露，不再整体拒算。
        return finish('hold', 'operating_unknown', '状态未确认（开停机证据缺失），保持当前设定', current)
    if 'observed_at' in operating:
        state_at = timestamp(operating['observed_at'])
        if not state_at or not 0 <= (now-state_at).total_seconds() <= p.max_age_seconds:
            return finish('hold', 'operating_unknown', '状态未确认（开停机观测过期或时序无效），保持当前设定', current)
    if operating['stopped']:
        # 停机：不给出建议密度（含保持值）。
        return finish('unavailable', 'stopped', '设备停机，不给出建议', None)
    if e.get('action_history_status') != 'good':
        return finish('unavailable', 'action_history_unknown', '调密历史查询缺失或失效，不能假定没有调密')
    if (ref.get('identity') not in ('live-calculation', 'online-default')
            or ref.get('origin') == 'advice-backfill-v1'
            or ref.get('availability') not in ('current-calculation', 'before-observation')):
        return finish('unavailable', 'reference_provenance', '原参考缺少可用的当时在线计算依据；回算不冒充现场先验')
    reference_basis = ref.get('basis') or {}
    reference_channels = reference_basis.get('channels')
    if reference_channels is None:
        reference_channels = sorted(((reference_basis.get('execution_basis') or {}).get('components') or {}).keys())
    if reference_channels != ['A304']:
        return finish('unavailable', 'reference_route', '原参考并非 A304 独立基准，不能混用通道')
    generated = timestamp(ref.get('generated_at'))
    if (generated is None or generated > now or not number(ref.get('value'))
            or ref.get('status') != 'calculated'):
        return finish('unavailable', 'reference_unavailable', '原小时参考缺失、后台结算中或晚于评估时刻')
    # 2026-11-06 优化：执行状态与趋向目标分离——等待/保持时 value 保持当前设定，
    # 但操作员仍能看到规则正在趋向的多步目标参考（只披露，不改变执行语义）。
    rho_target = round(min(max(ref['value'], p.rho_min), p.rho_max), 3)
    result['candidate']['rho_target'] = rho_target
    result['candidate']['target_direction'] = (
        'up' if rho_target > current else 'down' if rho_target < current else 'stable')
    if not (number(target.get('min')) and number(target.get('max'))
            and 0 <= target['min'] < target['max'] <= 100):
        return finish('unavailable', 'target_unknown', '缺少当时有效的灰分目标范围')
    start, end = timestamp(ash.get('start')), timestamp(ash.get('end'))
    if (not start or not end or (end-start).total_seconds() != 3600 or end > now
            or not number(ash.get('value')) or not 0 <= ash['value'] <= 100):
        return finish('unavailable', 'ash_unavailable', '缺少完整且不含未来数据的灰分生产小时')
    if not number(ash.get('coverage')) or not p.min_coverage < ash['coverage'] <= 1:
        return finish('unavailable', 'ash_coverage', '灰分生产有效覆盖须严格超过试验门槛')
    if not isinstance(ash.get('provisional'), bool):
        return finish('unavailable', 'ash_quality_unknown', '灰分暂估/实录来源不明')
    provisional = ash['provisional']
    result['quality']['level'] = 'provisional' if provisional else 'non-provisional'
    result['quality']['notes'].append('非暂估总灰分仍含计算/预测组分，不等同独立总精煤正式化验确认')
    if provisional:
        result['quality']['notes'].append('灰分含沿用暂估；小步仅为谨慎试算，不是正式化验响应确认')
    boundary = min(target['max'], max(target['min'], ash['value']))
    deviation = round(ash['value']-boundary, 3)
    band = 'in_band' if abs(deviation) <= .05 else 'high' if deviation > 0 else 'low'
    result['basis']['band'] = band
    # 2026-11-06 优化：暂估灰分敏感度披露——距方向判断边界/目标带的距离（百分点）。
    # 暂估修订超过该距离即跨界，方向或保持判断会翻转（11-04 案例：10.317→9.624 翻转目标带）。
    if band == 'in_band':
        sensitivity_edge = target['min'] if ash['value']-target['min'] <= target['max']-ash['value'] else target['max']
    else:
        sensitivity_edge = target['min'] if band == 'low' else target['max']
    sensitivity_distance = round(abs(ash['value']-sensitivity_edge), 3)
    result['basis']['ash_sensitivity'] = dict(
        provisional=provisional, band=band, edge=round(sensitivity_edge, 3),
        distance_pp=sensitivity_distance,
        note=(f"暂估灰分距{'目标带边界' if band == 'in_band' else '方向判断边界'} "
              f"{sensitivity_distance:.2f} 个百分点；修订越界将改变{'保持' if band == 'in_band' else '方向'}判断"
              if provisional else None))
    follow = abs(actual['value']-raw) <= p.actual_follow_tolerance+1e-7
    observation = result['observation']
    observation.update(state='no-recorded-action', actual_response='following' if follow else 'not-following')
    action = e.get('latest_action')
    waiting = None
    if action:
        before, after = timestamp(action.get('before_at')), timestamp(action.get('after_at'))
        observation.update(action_id=action.get('id'), before_at=action.get('before_at'), after_at=action.get('after_at'))
        if not after or after > now:
            return finish('unavailable', 'action_time_unknown', '最近调密时序无效，不能评价')
        covered = start >= after
        observation['ash_fully_after_action'] = covered
        interval_unknown = not before or before >= after
        if interval_unknown and not covered:
            return finish('unavailable', 'action_time_unknown', '最近调密区间无效且当前灰分尚未完全在其后，不能评价')
        long_gap = (interval_unknown or action.get('gap') or
            (after-before).total_seconds() > p.max_action_gap_seconds)
        if not action.get('continuous', False):
            result['quality']['notes'].append('调密连续性标志为否；短采样抖动只披露，真实操作仍在两次观测之间')
        if not covered:
            observation['state'] = 'waiting-post-action-production'
            waiting = ('waiting_post_action', '等待完整调后生产小时：当前灰分窗口尚含调密前生产，不继续追调')
            if long_gap or action.get('identity_known') is not True:
                waiting = ('uncertain_recent_action', '最近调密存在长缺口或来源不明；当前灰分尚未覆盖完整调后生产')
        else:
            observation['state'] = 'post-action-covered-provisional' if provisional else 'post-action-covered'
            if long_gap or action.get('identity_known') is not True:
                result['quality']['notes'].append('旧动作区间/来源不确定；当前灰分生产窗已完全在其后，不永久锁定')
    if band == 'in_band':
        return finish('hold', 'target_band', '灰分在目标范围或 0.05 灰分百分点死区内，保持当前设定', current)
    if waiting:
        return finish('wait', waiting[0], waiting[1], current)
    if not follow:
        # 2026-11-13 三态口径：变更后超过跟随兜底时限仍未跟随，明确报设备未执行，
        # 不再无限期等待（实测收敛中位 3 分钟、p90 9 分钟，兜底 15 分钟）。
        if action and after is not None and (now-after).total_seconds() > p.follow_grace_seconds:
            observation['state'] = 'device-not-following'
            return finish('unavailable', 'device_not_following',
                '实测密度超过15分钟未跟随设定，设备可能未执行上次调整，需现场确认', None)
        observation['state'] = 'waiting-current-reading'
        return finish('wait', 'actual_not_following', '当前实测读数尚未跟随设定；单样本不作稳定判断', current)
    if provisional and p.provisional_mode == 'hold':
        return finish('hold', 'provisional_hold', '试验参数要求暂估灰分保持，等待对应生产时段化验', current)
    gap = ref['value']-current
    desired_direction = -1 if deviation > 0 else 1
    if gap*desired_direction <= .0005:
        return finish('hold', 'reference_reached', '当前设定已达到或沿纠偏方向越过原参考，不反向追回', current)
    step = p.provisional_step if provisional else p.max_step
    value = round(current+desired_direction*min(step, abs(gap)), 3)
    value = max(p.rho_min, min(p.rho_max, value))
    if ((value-current)*desired_direction <= 0 or abs(value-current) > step+1e-7
            or (ref['value']-value)*desired_direction < -1e-7):
        return finish('hold', 'precision_hold', '三位小数网格不能同时满足步长与不越过原参考，保持', current)
    return finish('adjust', 'provisional_step' if provisional else 'bounded_step',
        '暂估灰分谨慎小步试算，调后响应未充分确认' if provisional else '朝原小时参考小步调整，不越过参考；仅测试候选', value)
