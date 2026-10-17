"""Small, immutable inputs consumed by the retrospective settlement kernel.

This is an opt-in GPT test-server loader. It does not replace forensic readback
or the lossless loader. Shared weights, chemistry validation and arithmetic stay
in the original settlement implementation.
"""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import zlib
from types import SimpleNamespace

from sqlalchemy import func, select

from ..decision_hourly.models import DecisionCycleSample, DecisionEvidenceArchive
from ..decision_hourly.references import VERSION, chunks
from ..models import ManualMeasurement
from ..telemetry.domain import iso_time
from ..telemetry.models import TelemetryReading
from .settlement_rows import load_rows as lossless_rows


_BELT = ('value', 'status', 'unit', 'quality', 'source_id', 'batch_id', 'observed_at', 'definition_id')
_BASIC = ('value', 'status')
_CELLS = {
    'coarse_ash': _BASIC, 'heavy_amount': _BASIC, 'total_ash': _BASIC,
    'belt501': _BELT, 'belt502': _BELT,
    'float_ash': ('value', 'status', 'unit', 'observed_at'),
    'float_amount': _BASIC, 'coarse_amount': _BASIC,
}
_TOP_CELLS = ('coarse_ash', 'heavy_amount', 'total_ash')
_BASE = ('value', 'status', 'unit', 'quality', 'definition_id')


class _UnsupportedKernel(Exception):
    pass


def _cell(value, fields, packed):
    if not isinstance(value, dict):
        raise _UnsupportedKernel()
    marker = next((key for key in ('$reading', '$manual') if key in value), None) if packed else None
    if marker:
        names, overlay = value.get('fields'), value.get('overlay')
        if not isinstance(names, list) or not isinstance(overlay, dict):
            raise _UnsupportedKernel()
        kept = {key: deepcopy(item) for key, item in overlay.items() if key in fields}
        # Nested reference envelopes in a consumed value are unusual. Delegate
        # them to full readback instead of guessing at their required base fields.
        if any(isinstance(item, (dict, list)) for item in kept.values()):
            raise _UnsupportedKernel()
        return {marker: value[marker], 'fields': [key for key in names if key in fields], 'overlay': kept}
    return {key: deepcopy(item) for key, item in value.items() if key in fields}


def _sources(value):
    if not isinstance(value, dict):
        raise _UnsupportedKernel()
    return {'resolved_choices': deepcopy(value['resolved_choices'])} if 'resolved_choices' in value else {}


def kernel_snapshot(snapshot):
    packed = snapshot.get('storage_version') == VERSION
    result = {key: deepcopy(snapshot[key]) for key in ('storage_version', 'evidence_policy') if key in snapshot}
    if 'sources' in snapshot:
        result['sources'] = _sources(snapshot['sources'])
    cells = snapshot.get('cells', {})
    if not isinstance(cells, dict):
        raise _UnsupportedKernel()
    result['cells'] = {}
    for key, fields in _CELLS.items():
        source = snapshot if key in _TOP_CELLS else cells
        target = result if key in _TOP_CELLS else result['cells']
        if key in source:
            target[key] = _cell(source[key], fields, packed)
    return result


def _columns():
    column = DecisionCycleSample.snapshot
    result = [func.json_array(func.json_type(column, '$.sources'),
        func.json_type(column, '$.sources.resolved_choices'), func.json_type(column, '$.cells')).label('root_types'),
        func.json_extract(column, '$.storage_version', '$.evidence_policy',
            '$.sources.resolved_choices').label('root_values')]
    for key, fields in _CELLS.items():
        path = '$.'+key if key in _TOP_CELLS else '$.cells.'+key
        stored_fields = func.json_extract(column, path+'.fields')
        mask = []
        for name in fields:
            mask.extend((func.instr(stored_fields, '"'+name+'"') > 0,
                         func.json_type(column, path+'.overlay.'+name)))
        result.extend((func.json_array(func.json_type(column, path),
            func.json_type(column, path+'.$reading'), func.json_type(column, path+'.$manual'),
            func.json_type(column, path+'.fields'), func.json_type(column, path+'.overlay')).label(key+'_types'),
            func.json_extract(column, *[path+'.'+name for name in fields],
                *[path+'.overlay.'+name for name in fields],
                path+'.$reading', path+'.$manual').label(key+'_values'),
            func.json_array(*mask).label(key+'_mask')))
    return result


def _decode(row):
    data = row._mapping
    source_kind, choices_kind, cells_kind = json.loads(data['root_types'])
    version, policy, choices = json.loads(data['root_values'])
    if source_kind not in (None, 'object') or cells_kind not in (None, 'object'):
        raise _UnsupportedKernel()
    result = {'storage_version': version, 'evidence_policy': policy, 'cells': {}}
    if source_kind is not None:
        result['sources'] = {'resolved_choices': choices} if choices_kind is not None else {}
    for key, fields in _CELLS.items():
        kind, reading, manual, fields_kind, overlay_kind = json.loads(data[key+'_types'])
        if kind is None:
            continue
        if kind != 'object':
            raise _UnsupportedKernel()
        values = json.loads(data[key+'_values'])
        value = dict(zip(fields, values[:len(fields)]))
        if version == VERSION and reading is not None:
            value = {'$reading': values[-2]}
        elif version == VERSION and manual is not None:
            value = {'$manual': values[-1]}
        if '$reading' in value or '$manual' in value:
            if fields_kind != 'array' or overlay_kind != 'object':
                raise _UnsupportedKernel()
            mask = json.loads(data[key+'_mask'])
            value.update(fields=[name for index, name in enumerate(fields) if mask[index*2]],
                overlay={name: values[len(fields)+index] for index, name in enumerate(fields)
                         if mask[index*2+1] is not None})
        target = result if key in _TOP_CELLS else result['cells']
        target[key] = _cell(value, fields, version == VERSION)
    return result


def _positions(snapshot):
    for key in _CELLS:
        source = snapshot if key in _TOP_CELLS else snapshot.get('cells', {})
        if key in source:
            yield source, key


def _resolve(db, snapshots):
    wanted = {'$reading': set(), '$manual': set()}
    for snapshot in snapshots:
        if snapshot.get('storage_version') == VERSION:
            for container, key in _positions(snapshot):
                for marker in wanted:
                    if marker in container[key]:
                        wanted[marker].add(container[key][marker])
    bases = {marker: {} for marker in wanted}
    for ids in chunks(wanted['$reading']):
        kinds = func.json_array(*[func.json_type(TelemetryReading.sample, '$.'+key) for key in _BASE])
        values = func.json_extract(TelemetryReading.sample, *['$.'+key for key in _BASE])
        for row in db.execute(select(TelemetryReading.id, TelemetryReading.source_id,
            TelemetryReading.batch_id, TelemetryReading.received_at, kinds, values).where(TelemetryReading.id.in_(ids))):
            names = json.loads(row[4])
            base = {key: value for key, kind, value in zip(_BASE, names, json.loads(row[5])) if kind is not None}
            base.update(source_id=row[1], batch_id=row[2], received_at=row[3], observed_at=row[3])
            bases['$reading'][row[0]] = base
    for ids in chunks(wanted['$manual']):
        for row in db.execute(select(ManualMeasurement.id, ManualMeasurement.value,
            ManualMeasurement.unit, ManualMeasurement.observed_at).where(ManualMeasurement.id.in_(ids))):
            bases['$manual'][row[0]] = dict(value=row[1], unit=row[2], observed_at=row[3])
    for marker in wanted:
        missing = wanted[marker]-bases[marker].keys()
        if missing:
            raise ValueError(f'决策原始记录缺失，无法还原 {marker}: {sorted(missing)[:5]}')
    for snapshot in snapshots:
        if snapshot.get('storage_version') != VERSION:
            continue
        for container, key in _positions(snapshot):
            cell = container[key]
            marker = next((name for name in wanted if name in cell), None)
            if marker:
                base = bases[marker][cell[marker]]
                container[key] = {**{name: deepcopy(base[name]) for name in cell['fields']}, **deepcopy(cell['overlay'])}
    return snapshots


def _archive(db, start):
    saved = db.get(DecisionEvidenceArchive, iso_time(start))
    if saved is None:
        return []
    raw = zlib.decompress(saved.payload)
    if hashlib.sha256(raw).hexdigest() != saved.checksum:
        raise ValueError('逐次证据归档校验失败')
    return [dict(row, snapshot=kernel_snapshot(row['snapshot'])) for row in json.loads(raw)]


def load_kernel_rows(db, start):
    """Return only shared settlement-kernel inputs, with no database writes."""
    try:
        archived = _archive(db, start)
        hot = list(db.execute(select(DecisionCycleSample.slot, DecisionCycleSample.sampled_at,
            DecisionCycleSample.fingerprint, *_columns()).where(
                DecisionCycleSample.sampled_at >= iso_time(start),
                DecisionCycleSample.sampled_at < iso_time(start+timedelta(hours=1))).order_by(DecisionCycleSample.slot)))
        snapshots = _resolve(db, [row['snapshot'] for row in archived]+[_decode(row) for row in hot])
        rows = {row['slot']: SimpleNamespace(**dict(row, snapshot=snapshot))
            for row, snapshot in zip(archived, snapshots[:len(archived)])}
        rows.update({row.slot: SimpleNamespace(slot=row.slot, sampled_at=row.sampled_at,
            fingerprint=row.fingerprint, snapshot=snapshot) for row, snapshot in zip(hot, snapshots[len(archived):])})
        return sorted(rows.values(), key=lambda row: row.slot)
    except _UnsupportedKernel:
        return lossless_rows(db, start)


load_rows = load_kernel_rows
