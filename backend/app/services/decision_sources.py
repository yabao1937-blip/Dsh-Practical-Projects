"""Shared source configuration and time-bounded inputs for GPT and hourly rows."""
import json
from copy import deepcopy

from sqlalchemy import select

from ..models import DecisionSourceRevision, CoalRecord, CoarseModel, Setting
from ..hourly_brief.columns import cell
from ..hourly_brief.inputs import Inputs, RAW_ASH, COAL_AMOUNT, LEVEL, LOCAL, business_time, numeric
from ..telemetry.derived import STATUS_GROUPS, DENSITY_AVERAGE_MEMBERS
from ..telemetry.density_effective import effective_component, effective_average, DENSITY_ITEMS
from ..telemetry.domain import definition_id, iso_time, utc_now
from ..telemetry.retained import density_history
from .manual_measurements import POINT_METRICS
from .decision_live import calculate, gpt_group
from .state import load_store, AUTO_STATE_KEYS, SETTING_TO_STORE
from ..gpt_coarse.timing import aligned_level, POLICY as TIMING_POLICY
from ..gpt_coarse.amount import from_wash

NODE_CELLS = {'raw-ash':'raw_ash', 'coal-rate':'coal_amount', 'tail-level':'level',
    'system-switches':'systems', 'desliming':'desliming', 'heavy-ash':'heavy_ash',
    'float-ash':'float_ash', 'float-amount':'float_amount', 'coarse-amount':'coarse_amount',
    'density-current':'density', 'float-press':'float_press', 'float-unmodeled':'reagent', 'coarse-slime':'raw_slime'}
DEFAULTS = {'raw-ash':RAW_ASH, 'coal-rate':COAL_AMOUNT, 'tail-level':LEVEL,
    'system-switches':'four_system_status', 'desliming':'desliming_status',
    'float-press':'float_press_status', 'density-current':'TIA_4_1.TIA_4_1.A304/MD',
    'heavy-ash':'heavy_clean_coal_ash', 'coarse-slime':'raw_coal_slime_content'}
GROUPS = {g['key']:g for g in STATUS_GROUPS}
UNITS = {'raw_ash':'%', 'coal_amount':'t/h', 'level':'%', 'heavy_ash':'%', 'float_ash':'%',
    'coarse_amount':'t/h', 'float_amount':'t/h', 'density':'g/cm³', 'raw_slime':'%'}


def config_at(db, when=None):
    query = select(DecisionSourceRevision)
    if when is not None:
        query = query.where(DecisionSourceRevision.effective_at <= iso_time(when))
    row = db.scalar(query.order_by(DecisionSourceRevision.id.desc()).limit(1))
    return dict(revision=row.id if row else 0, effective_at=row.effective_at if row else None,
                **(deepcopy(row.config) if row else dict(choices={}, target_min=8.5, target_max=9.0)))


def identity(source, key):
    return json.dumps([source, key], separators=(',', ':'))


def default_choice(inputs, node):
    target = DEFAULTS.get(node)
    if target in POINT_METRICS:
        return identity('manual-measurements', target) if inputs.manual(target) else ''
    if target in GROUPS:
        sources = {p.source_id for _, item in GROUPS[target]['members'] for p in inputs.points.get(item, [])}
        return identity(next(iter(sources)), target) if len(sources) == 1 else ''
    p = inputs.point(target)
    return identity(p.source_id, p.key) if p else ''


def config_view(db, when=None, inputs=None):
    when = when or utc_now()
    inputs = inputs or Inputs(db, when)
    config = config_at(db, when)
    config['resolved_choices'] = {node:config['choices'].get(node, default_choice(inputs, node)) for node in NODE_CELLS}
    config['resolved_choices']['coarse-amount'] = ''  # Always derived; old bindings remain only in saved history.
    choice = config['resolved_choices']['density-current']
    config['density_label'] = '未指定'
    if choice:
        source, key = json.loads(choice)
        point = next((p for group in inputs.points.values() for p in group if p.source_id == source and p.key == key), None)
        config['density_label'] = ('A303/A304 平均' if key == 'a303_a304_density_average' else
            point.config.get('item_id', point.key).split('.')[-1].split('/')[0] if point else '所选测点暂不可用')
    return config


def density_cell(inputs, points):
    items = []
    for p in points:
        sample = inputs.latest(p)
        items.append(dict(source_id=p.source_id, key=p.key, item_id=p.config['item_id'],
            unit=p.config.get('unit', ''), definition_id=definition_id(p.config), sample=sample, as_of=inputs.cutoff,
            available=inputs.usable(p, sample), last_density_sample=density_history(inputs.db, p, inputs.cutoff,
                authoritative=inputs.authoritative)))
    effective = effective_average(items) if len(points) == 2 else effective_component(items[0])
    label = 'A303/A304 平均' if len(points) == 2 else points[0].config['item_id'].split('.')[-1].split('/')[0]
    return cell(effective['value'], 'KEPServer · '+label, effective['received_at'], effective['status'],
        effective['reason'], unit=effective.get('unit') or 'g/cm³', item_id=effective.get('item_id', 'a303_a304_density_average'),
        source_id=points[0].source_id, point_key=points[0].key if len(points)==1 else 'a303_a304_density_average',
        sample=effective.get('sample'), effective_density=effective, density_label=label,
        components=effective.get('components'), raw_value=effective.get('raw_value'))


def selected_cell(inputs, node, choice):
    source, key = json.loads(choice)
    if source == 'manual-measurements':
        if key not in POINT_METRICS:
            return cell(reason='该手工指标仅作记录，不可用于决策计算', source_id=source, point_key=key)
        found = inputs.manual(key) or cell(reason='暂无整点前已录入的有效手工补录')
        return dict(found, unit=POINT_METRICS[key]['unit'], source_id=source, point_key=key)
    if key in GROUPS:
        return dict(inputs.group(GROUPS[key]['members'], source), source_id=source, point_key=key)
    if key == 'a303_a304_density_average':
        points = [inputs.point(item, source) for _, item in DENSITY_AVERAGE_MEMBERS]
        return density_cell(inputs, points) if all(points) else cell(reason='所选平均密度测点缺失')
    points = [p for group in inputs.points.values() for p in group if p.source_id == source and p.key == key]
    if len(points) != 1:
        return cell(reason='所选测点未返回或已停用', source_id=source, point_key=key)
    p = points[0]
    item = p.config.get('item_id', '')
    if node == 'density-current' and item in DENSITY_ITEMS:
        return density_cell(inputs, [p])
    unit = p.config.get('unit') or known_unit(item)
    return inputs.telemetry(item, zero_hold=node in ('raw-ash','coal-rate'), unit=unit, point=p)


def known_unit(item):
    if item in DENSITY_ITEMS:
        return 'g/cm³'
    if item.endswith('/COAL_CAP') and item.split('.')[-1].split('/')[0] in ('301','501','502'):
        return 't/h'
    return {RAW_ASH:'%', LEVEL:'%'}.get(item, '')


def latest_record(inputs, category):
    return inputs.db.scalar(select(CoalRecord).where(CoalRecord.category == category,
        CoalRecord.ts <= inputs.hour.astimezone(LOCAL).strftime('%Y-%m-%d %H:%M:%S'),
        CoalRecord.created_at <= inputs.hour.astimezone(LOCAL).replace(tzinfo=None))
        .order_by(CoalRecord.ts.desc(), CoalRecord.id.desc()).limit(1))


def collect_shared(inputs, config):
    cells = inputs.collect()
    # Historical/imported float assays are also visible in the overview. Respect their receipt time.
    row = latest_record(inputs, 'float')
    if row:
        if not inputs.manual(auto_paths=[('floatAshInput',)]) and numeric(row.ash_content):
            cells['float_ash'] = cell(row.ash_content, '历史浮精记录', iso_time(business_time(row.ts)),
                'historical', record_id=row.id, created_at=iso_time(business_time(row.created_at)))
        if not inputs.manual(auto_paths=[('amountInputs','floatAmount')]) and numeric(row.coal_amount):
            cells['float_amount'] = cell(row.coal_amount, '历史浮精记录', iso_time(business_time(row.ts)), 'historical', record_id=row.id)
    for node, key in NODE_CELLS.items():
        choice = config['resolved_choices'][node]
        if choice:
            cells[key] = selected_cell(inputs, node, choice)
        elif node == 'density-current':
            # An unavailable configured meter must never silently turn into a mean or a placeholder.
            cells[key] = cell(reason='当前分选密度尚无唯一有效采集来源', unit='g/cm³', density_label='未选择')
        elif node not in config['choices'] and node in DEFAULTS and DEFAULTS[node] not in POINT_METRICS:
            cells[key] = cell(reason='默认采集来源缺失或不唯一')
        cells[key].setdefault('unit', UNITS.get(key, ''))
    for belt in ('501','502'):
        cells['belt'+belt] = inputs.telemetry(f'TIA_3_4.TIA_3_4.{belt}/COAL_CAP', unit='t/h')
    cells['coarse_amount'] = from_wash(cells['coal_amount'], inputs.hour)
    return cells


def shared_snapshot(db, when=None):
    live = when is None
    when = when or utc_now()
    inputs = Inputs(db, when, live=live, authoritative=True)
    config = config_view(db, when, inputs)
    cells = collect_shared(inputs, config)
    level_observation = deepcopy(cells['level'])
    cells['level'] = aligned_level(inputs, level_observation)
    captured = {k:deepcopy(cells[k]) for k in ('raw_ash','coal_amount','level','heavy_ash','float_ash',
        'float_amount','coarse_amount','density','belt501','belt502')}
    for group, mapping in [('systems', {'401':'sys401','402':'sys402','A':'sysA','B':'sysB'}),
                            ('desliming', {'473':'desliming473','474':'desliming474'})]:
        for label, key in mapping.items():
            values = cells[group].get('value')
            value = values.get(label) if isinstance(values, dict) else None
            part = (cells[group].get('components') or {}).get(label) or cells[group]
            captured[key] = dict(part, value=int(value) if isinstance(value, bool) else None, unit='')
            if value is None:
                captured[key]['status'] = 'missing'
    raw = cells['coal_amount'].get('raw_value', cells['coal_amount']['value'])
    captured['is_stoppage'] = dict(cells['coal_amount'], value=int(raw <= 10) if numeric(raw) else None, unit='')
    for c in captured.values():
        c['entered_at'] = c.get('created_at')
        c['batch_id'] = (c.get('sample') or {}).get('batch_id')
    store = load_store(db, model_at=when)
    current_models = list(db.scalars(select(CoarseModel)))
    if any(m.created_at and business_time(m.created_at) > when for m in current_models):
        store['coarseModel'] = None
    for key in AUTO_STATE_KEYS:
        if key in inputs.auto:
            store[key] = inputs.auto[key]
        else:
            store.pop(key, None)
    group = gpt_group(store)
    if group and group.get('trainedAt') and business_time(group['trainedAt']) > when:
        store['coarseModel'] = None
        store['coarseModelVariants'] = {}
    for key, store_key in SETTING_TO_STORE.items():
        if key not in inputs.settings:
            store.pop(store_key, None)
    result = calculate(captured, store, config['target_min'], config['target_max'], now=when, shared_sources=True)
    from ..gpt_density.execution import apply_live
    apply_live(db, inputs, result, cells, store)
    unavailable_guard = any(r.key == 'density_guide' and business_time(r.updated_at) > when for r in db.scalars(select(Setting)))
    if unavailable_guard:
        result['suggested_density'] = dict(value=None, status='missing', reason='整点后调整了密度参数，无法还原当时配置，暂缓建议')
    for key in ('coarse_ash','heavy_amount','total_ash','suggested_density'):
        cells[key] = dict(result[key], observed_at=iso_time(when), source='决策总览共享 GPT 计算',
                          unit='g/cm³' if key == 'suggested_density' else 't/h' if key == 'heavy_amount' else '%')
    result.update(cells=cells, sources=config, density_label=config['density_label'],
                  level_observation=level_observation, input_timing=deepcopy(TIMING_POLICY))
    return result
