"""Read only chemistry evidence over the authenticated read-only SQL transport.

The original settlement loader is retained for production. Filtering in SQLite
keeps unrelated model explanations and display channels off the transport while
preserving the packed-reference format used by the normal evidence resolver.
"""
import json
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import case, func, select

from ..decision_hourly.archive import archived_rows
from ..decision_hourly.ash_settlement import settlement_snapshot
from ..decision_hourly.models import DecisionCycleSample
from ..decision_hourly.references import resolve_snapshots
from ..telemetry.domain import iso_time


_TOP = ('storage_version', 'evidence_policy', 'sources', 'coarse_ash', 'heavy_amount', 'total_ash',
        'cadence_seconds', 'quality_sources')
_CELLS = ('belt501', 'belt502', 'float_ash', 'float_amount', 'coarse_amount')


def _columns():
    column = DecisionCycleSample.snapshot
    fields = [(key, '$.'+key) for key in _TOP]
    fields.extend(('cell_'+key, '$.cells.'+key) for key in _CELLS)
    columns = []
    for label, path in fields:
        # Keep type separately because missing and explicit null both extract as
        # null. A two-path extraction returns JSON text rather than SQLite scalar
        # coercions, preserving booleans, full float precision and large integers.
        columns.extend((func.json_type(column, path).label(label+'_kind'),
                        func.json_extract(column, path, path).label(label+'_value')))
    cells_kind = func.json_type(column, '$.cells')
    columns.extend((cells_kind.label('cells_kind'),
        case((cells_kind != 'object', func.json_extract(column, '$.cells', '$.cells')),
             else_=None).label('malformed_cells')))
    return columns


def _value(kind, value):
    return json.loads(value)[0]


def _snapshot(row):
    values = row._mapping
    snapshot = {key: _value(values[key+'_kind'], values[key+'_value']) for key in _TOP
                if values[key+'_kind'] is not None}
    cells_kind = values['cells_kind']
    if cells_kind not in (None, 'object'):
        snapshot['cells'] = _value(cells_kind, values['malformed_cells'])
    else:
        snapshot['cells'] = {key: _value(values['cell_'+key+'_kind'], values['cell_'+key+'_value'])
            for key in _CELLS if values['cell_'+key+'_kind'] is not None}
    return settlement_snapshot(snapshot)


def load_rows(db, start):
    """Match the normal loader, including hot-over-archive slot precedence."""
    from ..storage_retention.basis import load_basis_rows
    permanent = load_basis_rows(db, start)
    if permanent is not None:
        return permanent
    end = start+timedelta(hours=1)
    rows = {row['slot']: SimpleNamespace(**row) for row in archived_rows(db, iso_time(start),
        project_snapshot=settlement_snapshot)}
    hot = list(db.execute(select(DecisionCycleSample.slot, DecisionCycleSample.sampled_at,
        DecisionCycleSample.fingerprint, *_columns()).where(
            DecisionCycleSample.sampled_at >= iso_time(start),
            DecisionCycleSample.sampled_at < iso_time(end)).order_by(DecisionCycleSample.slot)))
    # Apply the same consumer contract after decoding. This also preserves the
    # original explicit failure for malformed non-object cells, rather than
    # accepting a damaged snapshot as empty chemistry evidence.
    snapshots = [_snapshot(row) for row in hot]
    resolved = resolve_snapshots(db, snapshots)
    rows.update({row.slot: SimpleNamespace(slot=row.slot, sampled_at=row.sampled_at,
        fingerprint=row.fingerprint, snapshot=snapshot) for row, snapshot in zip(hot, resolved)})
    return sorted(rows.values(), key=lambda row: row.slot)
