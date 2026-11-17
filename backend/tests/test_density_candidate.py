"""Meaningful trial cases: preserved reference, timing, quality and step bounds."""
from copy import deepcopy
from datetime import timedelta

import pytest

from app.gpt_density.candidate import CandidateParameters, evaluate_candidate
from app.telemetry.domain import iso_time
from test_hourly_quality_advice import HOUR


def evidence(ash=9.3, reference=1.46, current=1.48, provisional=False):
    def cell(value, key):
        return dict(value=value, status='good', observed_at=iso_time(HOUR), unit='g/cm³',
            source_id='kep', point_key=key, definition_id=key+'-v1', batch_id='same-batch')
    return dict(evaluated_at=iso_time(HOUR), identity_known=True, action_history_status='good',
        mode='historical-replay', reference=dict(value=reference, status='calculated',
            policy='gpt-hourly-reference-v4', generated_at=iso_time(HOUR),
            identity='online-default', availability='before-observation',
            target=dict(min=8.5,max=9), basis=dict(value=1.48,channels=['A304']),
            ash=dict(value=ash,start=iso_time(HOUR-timedelta(hours=2)),
                end=iso_time(HOUR-timedelta(hours=1)),coverage=.95,provisional=provisional)),
        current_setpoint=cell(current,'A304/MD_SET'), actual_density=cell(current,'A304/MD'),
        operating=dict(status='good',stopped=False), latest_action=None)


def gate_evidence(**kw):
    """2026-11-17 收敛门控 tail_evidence 构造器。"""
    d = dict(point_count=3, mad=0.008, last2_within_tolerance=True)
    d.update(kw)
    return d


@pytest.mark.parametrize('ash,reference,expected', [(9.3,1.46,1.47),(8.2,1.50,1.49)])
@pytest.mark.parametrize('provisional', [False,True])
def test_current_step_both_directions_and_smaller_provisional(ash,reference,expected,provisional):
    e = evidence(ash,reference,provisional=provisional)
    original = deepcopy(e)
    result = evaluate_candidate(e)
    if provisional:
        expected = 1.475 if ash>9 else 1.485
    assert result['candidate']['value'] == expected
    assert result['candidate']['status'] == 'adjust'
    assert result['quality']['level'] == ('provisional' if provisional else 'non-provisional')
    assert result['reference'] == e['reference'] and e == original


def test_minimal_end_to_end_reference_is_preserved_and_not_a_new_step():
    e = evidence(current=1.52,reference=1.46)
    r = evaluate_candidate(e)
    assert r['reference']['value'] == 1.46
    assert r['setting']['value'] == 1.52
    assert r['candidate']['value'] == 1.51
    assert r['parameters']['max_step'] == .010


@pytest.mark.parametrize('status', ['missing','pending','blocked','held',None])
def test_numeric_invalid_status_reference_is_not_accepted(status):
    e=evidence()
    e['reference']['status']=status
    r=evaluate_candidate(e)
    assert r['candidate']['value'] is None
    assert r['candidate']['reason_codes']==['reference_unavailable']


def test_other_route_reference_cannot_be_used_for_a304():
    e=evidence()
    e['reference']['basis']['channels']=['A303']
    assert evaluate_candidate(e)['candidate']['reason_codes']==['reference_route']


@pytest.mark.parametrize('ash', [8.5,8.8,9.0,9.05,8.45])
def test_target_band_and_deadband_keep_actual_current_setting(ash):
    e = evidence(ash=ash,current=1.52,reference=1.48)
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'hold'
    assert r['candidate']['value'] == 1.52
    assert r['basis']['band'] == 'in_band'


@pytest.mark.parametrize('ash,reference,current', [(9.3,1.46,1.45),(8.2,1.50,1.51)])
def test_no_reverse_chase_when_operator_has_passed_reference(ash,reference,current):
    r = evaluate_candidate(evidence(ash,reference,current))
    assert r['candidate']['value'] == current
    assert r['candidate']['reason_codes'] == ['reference_reached']


def test_wait_uses_production_start_not_fixed_two_hour_timer_and_recovers():
    e = evidence()
    # 2026-11-16 用户口径：调密后>10分钟且实测已跟随→调密执行确认，不再等灰分窗口。
    action_at = HOUR-timedelta(hours=1,minutes=59)
    e['latest_action'] = dict(id='real',before_at=iso_time(action_at-timedelta(seconds=60.2)),
        after_at=iso_time(action_at),continuous=False,gap=False,identity_known=True)
    r = evaluate_candidate(e)
    # 调密后 1h59m >> 10 分钟且实测跟随 → 不再 wait，直接 adjust
    assert r['candidate']['status'] == 'adjust'
    assert r['observation']['state'] == 'post-action-confirmed-by-actual'
    assert r['observation']['ash_fully_after_action'] is False
    # 灰分窗口完全在调后时状态回到 covered
    e['reference']['ash'].update(start=iso_time(HOUR-timedelta(hours=1)),end=iso_time(HOUR))
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'adjust'
    assert r['observation']['state'] == 'post-action-covered'


def test_post_action_confirm_10min_follow_recovers():
    """2026-11-16 用户口径：调密后10分钟实测跟随→执行确认→恢复建议；<10分钟仍在确认中。"""
    e = evidence()
    # 场景 A：调密后仅 5 分钟（<10min）→ 仍 wait（确认中）
    action_at = HOUR-timedelta(minutes=5)
    e['latest_action'] = dict(id='r5',before_at=iso_time(action_at-timedelta(seconds=30)),
        after_at=iso_time(action_at),continuous=True,gap=False,identity_known=True)
    # ash 窗口需在 action 之前（covered=False）
    e['reference']['ash'].update(start=iso_time(HOUR-timedelta(hours=1)),end=iso_time(HOUR))
    # 实测跟随
    e['actual_density'].update(value=1.48,status='good',
        observed_at=iso_time(HOUR))
    e['current_setpoint'].update(value=1.48,status='good',
        observed_at=iso_time(HOUR))
    # evaluated_at = HOUR, action at HOUR-5min → post_elapsed=300 < 600 → wait
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'wait'
    assert r['candidate']['reason_codes'] == ['waiting_post_action']
    assert '确认中' in r['candidate']['reasons'][0]
    # 场景 B：调密后 15 分钟（>10min）→ 实测跟随 → 确认成功 → adjust
    e['latest_action']['after_at'] = iso_time(HOUR-timedelta(minutes=15))
    e['latest_action']['before_at'] = iso_time(HOUR-timedelta(minutes=16))
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'adjust'
    assert r['observation']['state'] == 'post-action-confirmed-by-actual'
    # 场景 C：调密后 15 分钟但实测未跟随 → 仍 wait（不走10分钟捷径）
    e['actual_density'].update(value=1.42)  # 远离设定 1.48
    r = evaluate_candidate(e)
    # not follow → 先走 follow 检查（但 waiting 优先于 follow）
    assert r['candidate']['status'] == 'wait'
    assert r['candidate']['reason_codes'] == ['waiting_post_action']


def test_rho_target_separates_aim_from_execution():
    """2026-11-06：等待/保持时 value=当前设定，rho_target 仍披露多步趋向目标。"""
    # 2026-11-16 等待场景（<10分钟确认期）：value 保持当前设定，目标参考照常可见
    e = evidence()
    action_at = HOUR-timedelta(minutes=5)  # 调密后仅5分钟 → 确认中
    e['latest_action'] = dict(id='real',before_at=iso_time(action_at-timedelta(seconds=60.2)),
        after_at=iso_time(action_at),continuous=False,gap=False,identity_known=True)
    # ash 窗口需在 action 之前（covered=False），否则不走 wait 分支
    e['reference']['ash'].update(start=iso_time(HOUR-timedelta(hours=2)),end=iso_time(HOUR-timedelta(hours=1)))
    e['reference']['generated_at'] = iso_time(HOUR-timedelta(hours=1))
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'wait'
    assert r['candidate']['value'] == 1.48            # 执行值 = 当前设定
    assert r['candidate']['rho_target'] == 1.46       # 趋向目标 = 参考值
    assert r['candidate']['target_direction'] == 'down'
    # 调整场景：rho_target 仍是参考值，与单步 value 不同
    r2 = evaluate_candidate(evidence())
    assert r2['candidate']['status'] == 'adjust'
    assert r2['candidate']['value'] == 1.47 and r2['candidate']['rho_target'] == 1.46
    # 参考不可用时目标不可见（不冒充）
    e2 = evidence()
    e2['reference']['status'] = 'settling'
    r3 = evaluate_candidate(e2)
    assert r3['candidate']['reason_codes'] == ['reference_unavailable']
    assert r3['candidate']['rho_target'] is None and r3['candidate']['target_direction'] is None


def test_ash_sensitivity_discloses_provisional_margin():
    """2026-11-06：暂估灰分披露距边界距离；非暂估不带警示 note。"""
    # 高于目标带 9.3（目标 8.5–9.0）：方向判断边界 = 9.0，距离 0.3pp
    r = evaluate_candidate(evidence(ash=9.3, provisional=True))
    s = r['basis']['ash_sensitivity']
    assert s['provisional'] is True and s['band'] == 'high'
    assert s['edge'] == 9.0 and s['distance_pp'] == 0.3
    assert '修订越界' in (s['note'] or '')
    # 目标带内 8.7：距更近边界 8.5 为 0.2pp
    r2 = evaluate_candidate(evidence(ash=8.7, provisional=True, reference=1.48))
    s2 = r2['basis']['ash_sensitivity']
    assert s2['band'] == 'in_band' and s2['edge'] == 8.5 and s2['distance_pp'] == 0.2
    assert '保持' in (s2['note'] or '')
    # 非暂估：结构仍返回，note 为 None（不渲染警示）
    r3 = evaluate_candidate(evidence(ash=9.3, provisional=False))
    s3 = r3['basis']['ash_sensitivity']
    assert s3['provisional'] is False and s3['distance_pp'] == 0.3 and s3['note'] is None


def test_long_gap_unknown_action_blocks_related_ash_but_not_forever():
    # 2026-11-16：>10分钟且实测跟随时，不确定动作也被10分钟确认规则放行。
    e = evidence()
    a = HOUR-timedelta(hours=1,minutes=30)
    e['latest_action'] = dict(id='gap',before_at=iso_time(a-timedelta(minutes=10)),
        after_at=iso_time(a),continuous=False,gap=True,identity_known=False)
    # 调密后90分钟>>10分钟且实测跟随 → 10分钟确认通过，不再被不确定动作阻塞
    assert evaluate_candidate(e)['candidate']['status'] == 'adjust'
    # 但<10分钟时不确认规则不适用，不确定动作仍阻塞
    e2 = evidence()
    a2 = HOUR-timedelta(minutes=5)
    e2['latest_action'] = dict(id='gap2',before_at=iso_time(a2-timedelta(minutes=10)),
        after_at=iso_time(a2),continuous=False,gap=True,identity_known=False)
    e2['reference']['ash'].update(start=iso_time(HOUR-timedelta(hours=2)),end=iso_time(HOUR-timedelta(hours=1)))
    e2['reference']['generated_at'] = iso_time(HOUR-timedelta(hours=1))
    assert evaluate_candidate(e2)['candidate']['reason_codes'] == ['uncertain_recent_action']
    e['reference']['ash'].update(start=iso_time(HOUR-timedelta(hours=1)),end=iso_time(HOUR))
    assert evaluate_candidate(e)['candidate']['status'] == 'adjust'


def test_old_missing_before_time_does_not_permanently_lock_new_production():
    e=evidence()
    e['latest_action']=dict(id='legacy',before_at=None,
        after_at=iso_time(HOUR-timedelta(hours=3)),continuous=False,gap=True,identity_known=False)
    assert evaluate_candidate(e)['candidate']['status']=='adjust'
    e['latest_action']['after_at']=iso_time(HOUR-timedelta(hours=1))
    assert evaluate_candidate(e)['candidate']['reason_codes']==['action_time_unknown']


def test_post_action_provisional_window_remains_unconfirmed():
    e = evidence(provisional=True)
    a = HOUR-timedelta(hours=3)
    e['latest_action'] = dict(id='old',before_at=iso_time(a-timedelta(seconds=60)),
        after_at=iso_time(a),continuous=True,gap=False,identity_known=True)
    r = evaluate_candidate(e)
    assert r['observation']['state'] == 'post-action-covered-provisional'
    assert r['candidate']['value'] == 1.475
    assert evaluate_candidate(e,{'provisional_mode':'hold'})['candidate']['status'] == 'hold'


@pytest.mark.parametrize('issue,code', [
    ('missing_setting','setpoint_unavailable'),('future_setting','setpoint_unavailable'),
    ('batch','batch_mismatch'),
    ('identity','identity_unknown'),('history','action_history_unknown'),
    ('replay','reference_provenance'),
    ('pending','reference_unavailable'),('future_reference','reference_unavailable'),
    ('coverage','ash_coverage'),('unknown_provisional','ash_quality_unknown'),
    ('future_ash','ash_unavailable')])
def test_insufficient_evidence_is_explicit_and_recovers(issue,code):
    e = evidence()
    if issue=='missing_setting': e['current_setpoint']['value']=None
    if issue=='future_setting': e['current_setpoint']['observed_at']=iso_time(HOUR+timedelta(seconds=1))
    if issue=='batch': e['actual_density']['batch_id']='different'
    if issue=='identity': e['identity_known']=False
    if issue=='history': e['action_history_status']='unavailable'
    if issue=='replay': e['reference']['origin']='advice-backfill-v1'
    if issue=='pending': e['reference'].update(value=None,status='pending')
    if issue=='future_reference': e['reference']['generated_at']=iso_time(HOUR+timedelta(seconds=1))
    if issue=='coverage': e['reference']['ash']['coverage']=.8
    if issue=='unknown_provisional': e['reference']['ash']['provisional']=None
    if issue=='future_ash': e['reference']['ash'].update(start=iso_time(HOUR),end=iso_time(HOUR+timedelta(hours=1)))
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'unavailable' and r['candidate']['value'] is None
    assert r['candidate']['reason_codes'] == [code]
    assert evaluate_candidate(evidence())['candidate']['status'] == 'adjust'


def test_operating_tri_state_unknown_holds_and_stopped_gives_no_advice():
    """2026-11-13 三态口径：证据缺失=未知态 hold=当前设定；停机=不给建议。"""
    e = evidence(current=1.48)
    e['operating']['status'] = 'missing'
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'hold' and r['candidate']['value'] == 1.48
    assert r['candidate']['reason_codes'] == ['operating_unknown']
    e2 = evidence(current=1.48)
    e2['operating']['observed_at'] = iso_time(HOUR-timedelta(seconds=31))
    r2 = evaluate_candidate(e2)
    assert r2['candidate']['status'] == 'hold' and r2['candidate']['value'] == 1.48
    assert r2['candidate']['reason_codes'] == ['operating_unknown']
    e3 = evidence(current=1.48)
    e3['operating']['stopped'] = True
    r3 = evaluate_candidate(e3)
    assert r3['candidate']['status'] == 'unavailable' and r3['candidate']['value'] is None
    assert r3['candidate']['reason_codes'] == ['stopped']


def test_degraded_same_batch_readings_within_five_minutes_still_usable():
    """30 秒外、5 分钟内的同批 good 读数降级可用并披露；超 5 分钟仍拒算。"""
    e = evidence()
    stale_at = iso_time(HOUR-timedelta(seconds=120))
    e['current_setpoint']['observed_at'] = stale_at
    e['actual_density']['observed_at'] = stale_at
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'adjust'
    assert any('降级采用' in note for note in r['quality']['notes'])
    too_old = iso_time(HOUR-timedelta(seconds=301))
    e['current_setpoint']['observed_at'] = too_old
    e['actual_density']['observed_at'] = too_old
    assert evaluate_candidate(e)['candidate']['reason_codes'] == ['setpoint_unavailable']


def test_follow_grace_reports_device_not_following_after_fifteen_minutes():
    """变更后超过兜底时限仍未跟随 → 明确报设备未执行，不再无限期等待。"""
    e = evidence()
    action_at = HOUR-timedelta(minutes=90)
    e['latest_action'] = dict(id='old',before_at=iso_time(action_at-timedelta(seconds=60)),
        after_at=iso_time(action_at),continuous=True,gap=False,identity_known=True)
    e['reference']['ash'].update(start=iso_time(HOUR-timedelta(hours=1)),end=iso_time(HOUR))
    e['actual_density']['value'] = e['current_setpoint']['value'] + .02
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'unavailable' and r['candidate']['value'] is None
    assert r['candidate']['reason_codes'] == ['device_not_following']
    assert r['observation']['state'] == 'device-not-following'


def test_stopped_and_actual_response_and_precision_bounds():
    e = evidence()
    e['operating']['stopped']=True
    assert evaluate_candidate(e)['candidate']['reason_codes'] == ['stopped']
    e['operating']['stopped']=False
    e['actual_density']['value']=1.50
    assert evaluate_candidate(e)['candidate']['reason_codes'] == ['actual_not_following']
    e = evidence(reference=1.477)
    assert evaluate_candidate(e)['candidate']['value'] == 1.477
    e['current_setpoint']['value']=1.480000019
    e['actual_density']['value']=1.48
    assert evaluate_candidate(e)['candidate']['value'] == 1.477


def test_stale_operating_timestamp_is_not_current_operating_evidence():
    e=evidence()
    e['operating']['observed_at']=iso_time(HOUR-timedelta(seconds=31))
    assert evaluate_candidate(e)['candidate']['reason_codes']==['operating_unknown']


def test_actual_single_reading_contract_remains_explicit_when_stale():
    e=evidence()
    e['actual_density']['value']=1.482
    reading=evaluate_candidate(e)['observation']['actual_reading']
    assert reading['value']==1.482 and reading['difference_from_setting']==.002
    assert reading['tolerance']==.010 and reading['observed_at']==iso_time(HOUR)
    assert reading['usable'] and reading['status']=='good' and reading['age_seconds']==0
    # 2026-11-13：31 秒同批 good 读数降级可用；超过 5 分钟才拒算
    e['actual_density']['observed_at']=iso_time(HOUR-timedelta(seconds=31))
    e['current_setpoint']['observed_at']=iso_time(HOUR-timedelta(seconds=31))
    reading=evaluate_candidate(e)['observation']['actual_reading']
    assert reading['usable'] and reading['age_seconds']==31
    e['actual_density']['observed_at']=iso_time(HOUR-timedelta(seconds=301))
    e['current_setpoint']['observed_at']=iso_time(HOUR-timedelta(seconds=301))
    reading=evaluate_candidate(e)['observation']['actual_reading']
    assert reading['value'] is None and reading['difference_from_setting'] is None
    assert reading['raw_value']==1.482 and reading['status']=='stale'
    assert not reading['usable'] and reading['age_seconds']==301


@pytest.mark.parametrize('actual,following', [(1.510,True),(1.530,True),
    (1.509999,False),(1.530001,False)])
def test_follow_tolerance_float_boundaries_are_symmetric(actual,following):
    e=evidence(current=1.520,reference=1.500)
    e['actual_density']['value']=actual
    result=evaluate_candidate(e)
    assert result['observation']['actual_response']==('following' if following else 'not-following')
    assert result['candidate']['status']==('adjust' if following else 'wait')
    assert result['parameters']['actual_follow_tolerance']==.010


@pytest.mark.parametrize('parameters', [{'max_step':.04},{'provisional_step':.02},
    {'provisional_mode':'bad'},{'actual_follow_tolerance':float('nan')},{'max_age_seconds':0}])
def test_trial_parameters_are_validated(parameters):
    with pytest.raises(ValueError):
        evaluate_candidate(evidence(), parameters)


# ══════════ 2026-11-17 收敛门控测试 ══════════

def _gate_event(minutes_since=16, tail=None):
    """构造带 tail_evidence 的事件。"""
    e = evidence()
    e['latest_action'] = dict(id='g1',
        before_at=iso_time(HOUR-timedelta(minutes=minutes_since+1)),
        after_at=iso_time(HOUR-timedelta(minutes=minutes_since)),
        continuous=True, gap=False, identity_known=True)
    e['tail_evidence'] = tail or gate_evidence()
    return e


def test_gate_wait_before_15min():
    """调密后不足15分钟 → wait(post_action_wait)。"""
    e = _gate_event(minutes_since=10)
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'wait'
    assert r['candidate']['reason_codes'] == ['post_action_wait']
    assert '等待15分钟' in r['candidate']['reasons'][0]


def test_gate_data_insufficient():
    """满15分钟但末段点数<2 → wait(tail_data_insufficient)。"""
    e = _gate_event(minutes_since=16, tail=gate_evidence(point_count=1))
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'wait'
    assert r['candidate']['reason_codes'] == ['tail_data_insufficient']


def test_gate_pause_above_015():
    """末段MAD>0.015 → unavailable(tail_pause)。"""
    e = _gate_event(minutes_since=16, tail=gate_evidence(mad=0.016))
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'unavailable'
    assert r['candidate']['reason_codes'] == ['tail_pause']
    assert r['candidate']['value'] is None


def test_gate_caution_between_010_015():
    """末段MAD在0.010-0.015 → wait(tail_caution)，再观察5分钟。"""
    e = _gate_event(minutes_since=16, tail=gate_evidence(mad=0.012))
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'wait'
    assert r['candidate']['reason_codes'] == ['tail_caution']


def test_gate_points_check_fails():
    """MAD≤0.010但最近2点不全在±0.010内 → wait(tail_points_check)。"""
    e = _gate_event(minutes_since=16,
        tail=gate_evidence(mad=0.008, last2_within_tolerance=False))
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'wait'
    assert r['candidate']['reason_codes'] == ['tail_points_check']


def test_gate_converged_allows_adjust():
    """MAD≤0.010且最近2点在容差内 → 收敛确认 → 正常给出adjust。"""
    e = _gate_event(minutes_since=16,
        tail=gate_evidence(mad=0.008, last2_within_tolerance=True))
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'adjust'
    assert r['observation']['state'] == 'post-action-converged'
    assert any('收敛确认' in n for n in r['quality']['notes'])


def test_gate_converged_allows_hold_if_in_band():
    """收敛确认后灰分在目标带内 → hold。"""
    e = _gate_event(minutes_since=16,
        tail=gate_evidence(mad=0.008))
    e['reference']['ash']['value'] = 8.8  # 在8.5-9.0内
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'hold'
    assert r['candidate']['reason_codes'] == ['target_band']


def test_gate_mad_exactly_010_passes():
    """MAD恰好=0.010 → 不超过，允许。"""
    e = _gate_event(minutes_since=16, tail=gate_evidence(mad=0.010))
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'adjust'


def test_gate_mad_exactly_015_pauses():
    """MAD恰好=0.015 → 不超过0.015，进入caution区间。"""
    e = _gate_event(minutes_since=16, tail=gate_evidence(mad=0.015))
    r = evaluate_candidate(e)
    # 0.015不>0.015, 所以走caution分支
    assert r['candidate']['status'] == 'wait'
    assert r['candidate']['reason_codes'] == ['tail_caution']
