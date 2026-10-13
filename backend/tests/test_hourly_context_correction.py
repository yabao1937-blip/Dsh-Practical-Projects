"""Frozen-hour corrections must not change measurements or relax advice guards."""
from copy import deepcopy
from datetime import timedelta

import pytest

from app.decision_hourly.corrections import correct_missing_level_context
from app.decision_hourly.statistics import digest, hour_advice
from app.telemetry.domain import iso_time
from test_decision_hourly import fake_rows, START, HOUR


def case():
    identity = str(['kep', 'level', 'JMCWC/LEVEL_B'])
    base = dict(point_definitions={identity:dict(definition_id='original', unit='%')},
        sources={'tail-level':'kep/level'}, model={'method':'expert8'}, density_guide={'maxStep':.03})
    missing = dict(base, point_definitions={})
    full_key, gap_key = digest(base), digest(missing)
    rows = fake_rows()
    for i, row in enumerate(rows):
        row.sampled_at = iso_time(START+timedelta(seconds=i*5))
        row.context_key = full_key
        row.snapshot['cells']['level'] = dict(value=50,status='good',source_id='kep',point_key='level',
            item_id='JMCWC/LEVEL_B',unit='%',definition_id='original')
        if 100 <= i < 110:
            row.context_key = gap_key
            row.snapshot['cells']['level'].update(value=None,status='missing',
                definition_id=None,reason='提前 90 秒液位缺失：目标时刻之前 30 秒内无采集记录')
            row.snapshot['suggested_density']['value'] = None
    basis = dict(value=1.5,status='good',observed_at=iso_time(HOUR-timedelta(seconds=2)))
    advice, coverage, guide = hour_advice(rows,START,HOUR,basis)
    summary = dict(period=dict(start=iso_time(START),end=iso_time(HOUR)),context_key=full_key,
        suggested_density=advice,coverage=coverage,guidance=dict(guide,validation='awaiting_experiment'),
        density_basis=basis,cells={'suggested_density':advice,'total_ash':{'value':8.8}},
        total_ash={'value':8.8},evidence={'segments':['original evidence']},generated_at=iso_time(HOUR))
    return summary, rows, {full_key:base,gap_key:missing}


def test_explicit_correction_preserves_evidence_gaps_and_is_idempotent():
    summary, rows, contexts = case()
    original = deepcopy((summary, rows, contexts))
    corrected = correct_missing_level_context(summary,rows,contexts,iso_time(HOUR))
    assert corrected['suggested_density']['value'] == 1.52
    assert corrected['coverage'] == summary['coverage']
    assert corrected['coverage']['valid_count'] == 710
    assert corrected['coverage']['longest_gap_seconds'] == 50
    for key in ('total_ash','evidence','density_basis','generated_at','context_key'):
        assert corrected[key] == summary[key]
    assert corrected['guidance']['mean_delta'] == summary['guidance']['mean_delta']
    assert corrected['guidance']['validation'] == 'awaiting_experiment'
    assert (summary, rows, contexts) == original
    assert correct_missing_level_context(corrected,rows,contexts,'later') == corrected


@pytest.mark.parametrize('change',['model','source','definition','cell_definition','sample_definition',
                                 'valid_suggestion','edge','gap','coverage','digest'])
def test_ambiguous_or_insufficient_hours_are_not_corrected(change):
    summary, rows, contexts = case()
    gap_key = rows[100].context_key
    if change in ('model','source','definition'):
        changed = deepcopy(contexts[gap_key])
        if change == 'model': changed['model']['method'] = 'other-model'
        elif change == 'source': changed['sources']['tail-level'] = 'other-point'
        else: changed['point_definitions'] = {str(['kep','level','JMCWC/LEVEL_B']):dict(definition_id='new-scale',unit='%')}
        key = digest(changed)
        contexts[key] = changed
        for row in rows[100:110]: row.context_key = key
    elif change == 'valid_suggestion': rows[100].snapshot['suggested_density']['value'] = 1.52
    elif change == 'cell_definition': rows[100].snapshot['cells']['level']['definition_id'] = 'different'
    elif change == 'sample_definition': rows[100].snapshot['cells']['level']['sample'] = {'definition_id':'different'}
    elif change == 'edge': rows = rows[100:]
    elif change == 'gap':
        for row in rows[110:114]: row.snapshot['suggested_density']['value'] = None
    elif change == 'coverage':
        for i in range(0,720,9): rows[i].snapshot['suggested_density']['value'] = None
    else: contexts[gap_key]['model'] = {'method':'tampered'}
    with pytest.raises(ValueError):
        correct_missing_level_context(summary,rows,contexts,iso_time(HOUR))
