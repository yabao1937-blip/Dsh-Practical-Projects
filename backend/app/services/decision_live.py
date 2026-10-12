"""One read-only GPT decision snapshot; no training, EMA, latch or PLC writes."""
from copy import deepcopy
import hashlib
import json
import math
from datetime import datetime, timezone

from .density import calc_total_ash, compute_density_guidance, DENSITY_GUIDE
from .guidance import density_guard_state
from .modeling import MLR_FEATURES, predict_coarse_ash
from ..gpt_coarse.predict import FEATURES as EXPERT_FEATURES, valid as valid_expert, explain as explain_expert
from ..gpt_coarse.timing import POLICY as TIMING_POLICY, LEVEL_LAG_SECONDS, LEVEL_TOLERANCE_SECONDS

LABELS = dict(raw_ash='原煤灰分', coal_amount='入洗原煤量', level='精磁尾液位',
    sysA='A系统', sysB='B系统', sys401='401系统', sys402='402系统',
    desliming473='473脱粉', desliming474='474脱粉', is_stoppage='停机状态',
    heavy_ash='重介精煤灰分', float_ash='浮精灰分', float_amount='浮精量',
    coarse_amount='粗精煤泥量', belt501='501瞬时带煤量', belt502='502瞬时带煤量', density='当前分选密度')


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def gpt_group(store):
    expert = store.get('gptExpertModel')
    if isinstance(expert, dict) and expert.get('production') == 'expert9':
        return expert
    def is_gpt(group):
        if not isinstance(group, dict):
            return False
        return ((group.get('mlr') or {}).get('metrics') or {}).get('version') == 2
    current = store.get('coarseModel')
    if is_gpt(current):
        return current
    saved = (store.get('coarseModelVariants') or {}).get('gpt')
    return saved if is_gpt(saved) else None


def calculate(cells, store, target_min=8.5, target_max=9.0, now=None, shared_sources=False):
    now = now or datetime.now(timezone.utc)
    cells = deepcopy(cells)
    stamp = now.isoformat(timespec='milliseconds')
    def output(value=None, status='missing', reason=''):
        return dict(value=value, status=status, reason=reason)
    def problem(key):
        c = cells.get(key) or {}
        v = c.get('value')
        if c.get('status') == 'missing' or not numeric(v):
            return f'{LABELS[key]}不可用：{c.get("reason") or c.get("source") or "未提供"}'
        if c.get('status') == 'good':
            try:
                received = datetime.fromisoformat(c.get('observed_at') or '')
                age = (now-received).total_seconds()
            except (TypeError,ValueError):
                return f'{LABELS[key]}采集时间缺失或无效'
            if key == 'level' and shared_sources:
                alignment = c.get('alignment') or {}
                if alignment.get('version') != TIMING_POLICY['version'] or not LEVEL_LAG_SECONDS <= age <= LEVEL_LAG_SECONDS+LEVEL_TOLERANCE_SECONDS:
                    return '精磁尾液位未按提前 90 秒对齐，或对应采样已过期'
            elif age > 30 or age < -5:
                return f'{LABELS[key]}采集数据已过期或时钟异常，等待新采集'
        expected = 'g/cm³' if key == 'density' else '%' if key in ('raw_ash','level','heavy_ash','float_ash') else 't/h' if key in ('coal_amount','float_amount','coarse_amount','belt501','belt502') else ''
        if c.get('unit', '') != expected:
            return f'{LABELS[key]}单位应为 {expected or "状态"}'
        if v < 0 or (expected == '%' and v > 100) or (not expected and v not in (0, 1)):
            return f'{LABELS[key]}超出有效范围'
        if key == 'density' and (v <= 0 or abs(v-1) <= 1e-9):
            return '当前分选密度暂无有效非 1 读数' if shared_sources else 'A304 暂无有效非 1 密度'
        if key == 'density' and shared_sources and c.get('item_id') not in (
                'TIA_4_1.TIA_4_1.A303/MD', 'TIA_4_1.TIA_4_1.A304/MD', 'a303_a304_density_average'):
            return '当前分选密度须选择实际密度采集点'
        if key == 'density' and not shared_sources and c.get('item_id') != 'TIA_4_1.TIA_4_1.A304/MD':
            return '当前密度必须采用 A304 实际密度'
        return None
    warnings = [f'{LABELS[k]}：{c.get("source") or c["status"]}' for k,c in cells.items()
                if k in LABELS and c.get('status') in ('held','stopped','historical','default')]
    group = gpt_group(store)
    method = (group or {}).get('production')
    model = (group or {}).get(method) or {}
    expert = method == 'expert9'
    features_used = EXPERT_FEATURES if expert else MLR_FEATURES
    names = model.get('feature_names', model.get('featureNames', MLR_FEATURES))
    valid_model = (method in ('mlr','pls') and numeric(model.get('intercept')) and names == MLR_FEATURES
                   and len(model.get('coefs') or []) == len(MLR_FEATURES) and all(numeric(v) for v in model['coefs']))
    bounds = model.get('inputBounds') or []
    if model.get('inputPolicy') == 'clip-training-range':
        valid_model = valid_model and len(bounds) == len(MLR_FEATURES) and all(
            b is None or (isinstance(b,list) and len(b)==2 and all(numeric(v) for v in b) and b[0]<=b[1]) for b in bounds)
    if expert:
        valid_model = valid_expert(model)
    model_id = hashlib.sha256(json.dumps(model, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16] if valid_model else None
    result = dict(policy='decision-live-v1', generated_at=stamp, inputs=cells,
        model=dict(engine='gpt', method=method, trained_at=(group or {}).get('trainedAt'), id=model_id),
        features=[], coarse_ash=output(), heavy_amount=output(), total_ash=output(),
        suggested_density=output(), guidance=None, warnings=warnings,
        target=dict(min=target_min,max=target_max))
    if expert:
        result['model'].update(revision=group.get('revision'), artifact_id=group.get('artifact_id'),
                               activated_at=group.get('activated_at'), label='专家基准九因子 · 待现场验证')
    expert_detail = None
    feature_errors = [error for k in features_used if (error := problem(k))]
    if feature_errors:
        result['coarse_ash'] = output(reason='模型输入不完整：' + '；'.join(feature_errors))
    elif not valid_model:
        result['coarse_ash'] = output(reason='没有可用的 GPT 保存模型，或模型因子不匹配')
    else:
        features = {k:cells[k]['value'] for k in features_used}
        for j,k in enumerate(features_used):
            raw = features[k]
            used = min(bounds[j][1],max(bounds[j][0],raw)) if model.get('inputPolicy') == 'clip-training-range' and bounds[j] else raw
            result['features'].append(dict(key=k, raw=raw, used=used, clipped=used!=raw))
        expert_detail = explain_expert(features, model) if expert else None
        prediction = expert_detail['value'] if expert_detail else None if expert else predict_coarse_ash(features, model)
        if expert_detail:
            result['features'] = expert_detail['features']
            result['expert_explanation'] = expert_detail
            result['warnings'].append('GPT专家基准模型；历史标签与专家经验存在差异，尚待新化验验证')
            if expert_detail['outside_expert_range']:
                result['warnings'].append('液位超出专家基准25–75%，显示边界估算，暂缓调密')
        if numeric(prediction) and 0 <= prediction <= 100:
            held = any(cells[k]['status'] in ('held','stopped') for k in features_used)
            result['coarse_ash'] = output(prediction, 'held' if held else 'predicted', '采用沿用输入，仅供观察' if held else
                                         'GPT专家基准九因子预测；待现场验证' if expert else 'GPT 保存模型实时预测')
        else:
            result['coarse_ash'] = output(reason='模型预测超出有效灰分范围')
    amount_keys = ('belt501','belt502','float_amount','coarse_amount')
    amount_errors = [error for k in amount_keys if (error := problem(k))]
    if not amount_errors:
        a,b = cells['belt501'],cells['belt502']
        if (a.get('batch_id') != b.get('batch_id') or a.get('source_id') != b.get('source_id')
                or not a.get('observed_at') or a['observed_at'] != b.get('observed_at')):
            amount_errors.append('501、502 不属于同一采集批次')
    if not amount_errors:
        amounts = [cells[k]['value'] for k in amount_keys]
        heavy = amounts[0]+amounts[1]-amounts[2]-amounts[3]
        if -math.ulp(1.0)*max(amounts)*4 <= heavy < 0:
            heavy = 0.0
        if numeric(heavy) and heavy >= 0:
            held_amount = any(cells[k]['status'] in ('held','stopped') for k in amount_keys)
            result['heavy_amount'] = output(heavy,'held' if held_amount else 'calculated')
        else:
            amount_errors.append('煤量不平衡：扣减量超过 501、502 合计')
    if amount_errors:
        result['heavy_amount'] = output(reason='；'.join(amount_errors))
    total_errors = [error for k in ('heavy_ash','float_ash','float_amount','coarse_amount') if (error := problem(k))]
    for key,title in [('coarse_ash','粗精煤泥灰分'),('heavy_amount','重介精煤量')]:
        if result[key]['value'] is None:
            total_errors.append(title+'不可用：'+result[key]['reason'])
    if not total_errors:
        heavy = result['heavy_amount']['value']
        f,c = cells['float_amount']['value'],cells['coarse_amount']['value']
        if heavy+f+c > 0:
            total = calc_total_ash(cells['heavy_ash']['value'],heavy,cells['float_ash']['value'],f,result['coarse_ash']['value'],c)
            held_total = result['coarse_ash']['status']=='held' or result['heavy_amount']['status']=='held' or any(
                cells[k]['status'] in ('held','stopped') for k in ('heavy_ash','float_ash'))
            result['total_ash'] = output(total,'held' if held_total else 'calculated',
                '含沿用输入，仅供观察' if held_total else '三组分灰分按煤量加权') if numeric(total) else output(reason='总灰分计算结果无效')
        else:
            total_errors.append('总煤量为零')
    if total_errors:
        result['total_ash'] = output(reason='；'.join(total_errors))
    advice_errors = []
    if expert_detail and expert_detail['outside_expert_range']:
        advice_errors.append('液位超出专家基准25–75%，边界估算仅供观察')
    if expert and (error := problem('is_stoppage')):
        advice_errors.append(error)
    if result['total_ash']['value'] is None:
        advice_errors.append(result['total_ash']['reason'])
    if error := problem('density'):
        advice_errors.append(error)
    held = [LABELS[k] for k,c in cells.items() if k in LABELS and c.get('status') in ('held','stopped')]
    if held:
        advice_errors.append('输入沿用或设备停机，暂缓调密：'+'、'.join(held))
    if not problem('is_stoppage') and cells['is_stoppage']['value'] == 1:
        advice_errors.append('入洗原煤停机或低负荷，暂缓调密')
    if advice_errors:
        result['suggested_density'] = output(reason='；'.join(dict.fromkeys(advice_errors)))
        return result
    total = result['total_ash']['value']
    target = min(target_max,max(target_min,total))
    guard_store = deepcopy(store)
    times = []
    for key,c in cells.items():
        if key == 'density':
            continue  # A density refresh alone is not new ash-driving data.
        try:
            observed = datetime.fromisoformat(c.get('observed_at') or '')
            if observed.tzinfo and observed <= now:
                times.append(observed)
        except (TypeError, ValueError):
            pass
    driver_stamp = max(times).isoformat() if times else ''
    guard_store.update(totalAshManualOn=False, heavyAshManualOn=False, heavyAshInput={},
                       floatAshInput={}, coarseAshInput={}, coarseCoal=[{'timestamp':driver_stamp}])
    for key,field in [('heavy_ash','heavyAshInput'),('float_ash','floatAshInput')]:
        if cells[key].get('status') == 'manual':
            try:
                entered = datetime.fromisoformat(cells[key].get('entered_at') or '')
                guard_store[field] = dict(manual=cells[key]['value'],manualAt=entered.timestamp()*1000)
                if key == 'heavy_ash':
                    guard_store['heavyAshManualOn'] = True
            except (TypeError,ValueError):
                pass
    guard = density_guard_state(guard_store,total,cells['heavy_ash']['value'],'total',now.timestamp()*1000)
    step = (store.get('densityGuide') or {}).get('maxStep')
    if not numeric(step) or step <= 0:
        step = DENSITY_GUIDE['maxStep']
    guidance = compute_density_guidance(dict(rho_cur=cells['density']['value'], heavy_ash=cells['heavy_ash']['value'],
        actual_total=total, scheme='total', tol=0, max_step=step, hold=guard['hold'],
        hold_reason=guard['holdReason'], drive_key=guard['driveKey']), target)
    in_range = target_min <= total <= target_max
    if in_range:
        guidance['reason'] = f'预测总灰分 {total:.2f}% 在目标范围 {target_min:.1f}–{target_max:.1f}% 内，密度保持'
    else:
        guidance['reason'] = guidance['reason'].replace('实际总灰分','预测总灰分')
        if guidance['deltaRho'] == 0 and not guidance['hold']:
            guidance['direction'] = 'stable'
            guidance['reason'] = f'预测总灰分 {total:.2f}% 超出目标范围，但偏差未超过原算法死区 0.05 个百分点，密度保持'
    result['guidance'] = guidance
    blocked = guidance['hold'] and not in_range
    result['suggested_density'] = output(None if blocked else guidance['rhoNew'], 'missing' if blocked else 'calculated',guidance['reason'])
    return result
