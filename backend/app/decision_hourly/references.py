"""Versioned, lossless references to authoritative telemetry and append-only assays.

Only decision-time interpretation/transformations stay inline. Resolving never reads
current point settings, current model coefficients or the mutable latest-value cache.
"""
from copy import deepcopy
from types import SimpleNamespace

from sqlalchemy import select, and_, or_, func
from sqlalchemy.dialects.sqlite import insert

from ..models import ManualMeasurement
from ..telemetry.models import TelemetryReading
from ..telemetry.retained import raw_sample
from .models import DecisionTelemetryPin

VERSION = 'authoritative-readings-v1'


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def chunks(values):
    values = list(values)
    for i in range(0, len(values), 400):
        yield values[i:i+400]


def telemetry_fields(row):
    return dict(raw_sample(row.sample) or {}, source_id=row.source_id, point_key=row.point_key,
                batch_id=row.batch_id, received_at=row.received_at, observed_at=row.received_at)


def manual_fields(row):
    return dict(record_id=row.id, metric=row.metric, value=row.value, unit=row.unit,
                observed_at=row.observed_at, created_at=row.created_at, entered_at=row.created_at)


def same(a, b):
    # bool signals become integer model factors. Preserve that deliberate conversion.
    return type(a) is type(b) and a == b


def pack_snapshot(db, snapshot, slot, *, pins=None):
    identities = set()
    def gather(value, source=None, point=None):
        if isinstance(value,list):
            for c in value:
                gather(c,source,point)
        elif isinstance(value,dict):
            source = value.get('source_id',source)
            point = value.get('point_key',value.get('key',point))
            if source and point and value.get('batch_id') and value.get('definition_id'):
                identities.add((source,point,value['batch_id']))
            for c in value.values():
                gather(c,source,point)
    gather(snapshot)
    readings = {}
    # Use the existing source/point/batch unique index, not a full batch-id scan.
    for group in chunks(identities):
        clauses = [and_(TelemetryReading.source_id==s,TelemetryReading.point_key==p,TelemetryReading.batch_id==b)
                   for s,p,b in group]
        for row in db.scalars(select(TelemetryReading).where(or_(*clauses))):
            readings[(row.source_id,row.point_key,row.batch_id)] = row
    used = set()

    def pack(value, source=None, point=None):
        if isinstance(value, list):
            return [pack(c, source, point) for c in value]
        if not isinstance(value, dict):
            return deepcopy(value)
        source = value.get('source_id', source)
        point = value.get('point_key', value.get('key', point))
        result = {k:pack(v, source, point) for k,v in value.items()}
        marker, row, base = None, None, None
        if source and point and value.get('batch_id') and value.get('definition_id'):
            row = readings.get((source,point,value['batch_id']))
            if row is None or row.sample.get('definition_id') != value['definition_id']:
                raise ValueError(f'采集历史引用缺失或定义不符：{source}/{point}/{value["batch_id"]}')
            marker, base = '$reading', telemetry_fields(row)
            used.add(row.id)
        elif value.get('metric') and value.get('record_id'):
            row = db.get(ManualMeasurement, value['record_id'])
            if row and row.metric == value['metric']:
                marker, base = '$manual', manual_fields(row)
        if marker:
            fields = [k for k,v in value.items() if k in base and same(v,base[k])]
            return {marker:row.id, 'fields':fields, 'overlay':{k:v for k,v in result.items() if k not in fields}}
        return result

    packed = pack(snapshot)
    # Identical input cells need only one reference description within this decision.
    for key,c in list(packed.get('inputs',{}).items()):
        if key in packed.get('cells',{}) and c == packed['cells'][key]:
            packed['inputs'][key] = {'$cell':key}
    packed['storage_version'] = VERSION
    if pins is not None:
        pins.update(used)
    else:
        pin_readings(db, used, slot)
    return packed


def pin_readings(db, used, slot):
    """Atomically widen usage bounds; never infer release safety from first use.

    A newer observation may arrive while historical pin usage is being backfilled.
    SQLite's conflict update preserves both bounds regardless of that ordering.
    Release additionally requires a globally completed legacy backfill, which is
    deliberately owned by the retention controller rather than this function.
    """
    pin_reading_bounds(db, {reading_id: (slot, slot) for reading_id in used})


def pin_reading_bounds(db, bounds):
    """Bulk-upsert prevalidated per-reading bounds, including legacy backfills."""
    values = sorted(bounds.items())
    for offset in range(0, len(values), 200):
        # Three values per row stay below SQLite's older 999-variable limit.
        statement = insert(DecisionTelemetryPin).values([
            dict(reading_id=reading_id, first_slot=lower, last_slot=upper)
            for reading_id, (lower, upper) in values[offset:offset+200]])
        statement = statement.on_conflict_do_update(
            index_elements=[DecisionTelemetryPin.reading_id],
            set_=dict(first_slot=func.min(DecisionTelemetryPin.first_slot, statement.excluded.first_slot),
                      last_slot=func.max(func.coalesce(DecisionTelemetryPin.last_slot,
                                                       statement.excluded.last_slot),
                                         statement.excluded.last_slot)))
        # Returning also refreshes pins already loaded in this Session.
        db.scalars(statement.returning(DecisionTelemetryPin),
                   execution_options={'populate_existing': True}).all()


def resolve_snapshots(db, snapshots):
    """Batch-load references once for an hour. Legacy inline snapshots pass through."""
    wanted = {'$reading':set(), '$manual':set()}
    for snapshot in snapshots:
        if snapshot.get('storage_version') == VERSION:
            for c in walk(snapshot):
                for marker in wanted:
                    if marker in c:
                        wanted[marker].add(c[marker])
    bases = {key:{} for key in wanted}
    for marker, model, fields in (('$reading',TelemetryReading,telemetry_fields),('$manual',ManualMeasurement,manual_fields)):
        for ids in chunks(wanted[marker]):
            bases[marker].update({r.id:fields(r) for r in db.scalars(select(model).where(model.id.in_(ids)))})
        missing = wanted[marker]-bases[marker].keys()
        if missing:
            raise ValueError(f'决策原始记录缺失，无法还原 {marker}: {sorted(missing)[:5]}')

    def resolve(value):
        if isinstance(value, list):
            return [resolve(v) for v in value]
        if not isinstance(value, dict):
            return deepcopy(value)
        for marker in wanted:
            if marker in value:
                base = bases[marker][value[marker]]
                return {**{k:deepcopy(base[k]) for k in value['fields']}, **resolve(value['overlay'])}
        return {k:resolve(v) for k,v in value.items()}

    results = []
    for snapshot in snapshots:
        if snapshot.get('storage_version') != VERSION:
            results.append(deepcopy(snapshot))
            continue
        result = resolve(snapshot)
        for key,c in list(result.get('inputs',{}).items()):
            if '$cell' in c:
                result['inputs'][key] = deepcopy(result['cells'][c['$cell']])
        results.append(result)
    return results


def hydrate_rows(db, rows):
    rows = list(rows)
    snapshots = resolve_snapshots(db, [r.snapshot for r in rows])
    return [SimpleNamespace(slot=r.slot, sampled_at=r.sampled_at, context_key=r.context_key,
                            fingerprint=r.fingerprint, snapshot=s) for r,s in zip(rows,snapshots)]
