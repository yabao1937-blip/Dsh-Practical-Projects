"""Causal replay and production-isolation checks for the coal-response experiment."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path

import numpy as np
import pytest

from app.gpt_coarse import expert8
from app.gpt_coarse.repository import activate
from app.gpt_coarse.experiments import coal_response as cr
from app.gpt_coarse.experiments.coal_response_evaluation import historical_evaluation, recent_evaluation

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def rows():
    old = json.loads((ROOT/'backend/models/gpt_expert9/20261009.json').read_text(encoding='utf-8'))
    return cr.historical_rows(old['rows'])


@pytest.fixture(scope='module')
def evaluated(rows):
    return historical_evaluation(rows)


@pytest.fixture
def causal_case():
    base = json.loads((ROOT/'backend/models/gpt_expert8/20261012/model.json').read_text(encoding='utf-8'))['model']
    single = dict(components=1, curves=[[15.5, -7.5]])
    # Clear artificial separated responses exercise the classification path even when real data do not identify types.
    mixture = dict(components=2, curves=[[12., -3.], [22., -3.]], mixing=[.5, .5], sigma=1.5,
                   identifiable=True, converged=True,
                   support=[dict(level_range=[25, 75]), dict(level_range=[25, 75])])
    row = dict(zip(expert8.FEATURES[:-1], base['reference']), id='now', day='2026-10-10',
               level=50, sample_at='2026-10-10T12:00:00+08:00', ash_content=0)
    history = []
    for day, level in ((7, 30), (8, 50), (9, 70)):
        record = dict(row, id=f'lab:{day}', day=f'2026-10-{day:02d}', level=level,
                      sample_at=f'2026-10-{day:02d}T09:00:00+08:00',
                      available_at=f'2026-10-{day:02d}T18:00:00+08:00')
        record['ash_content'] = float(cr.curve_values(record, base, mixture)[0])
        history.append(record)
    return row, base, single, mixture, history


def test_current_target_and_future_unreleased_labels_cannot_select_response(causal_case):
    row, base, single, mixture, history = causal_case
    expected = cr.response_prediction(row, base, mixture, single, history)
    assert expected['type'] == 1 and expected['fallback_base'] is None
    for ash in (-1000, 0, 1000):
        current = dict(row, ash_content=ash, available_at=row['sample_at'])
        future = dict(current, id='future', sample_at='2026-10-11T00:00:00+08:00',
                      available_at='2026-10-11T01:00:00+08:00')
        unreleased = dict(current, id='not_yet_released', sample_at='2026-10-10T08:00:00+08:00',
                          available_at='2026-10-10T13:00:00+08:00')
        assert cr.response_prediction(current, base, mixture, single, history+[current, future, unreleased]) == expected
        assert cr.bias_prediction(current, base, history+[current, future, unreleased], 2, 2) == cr.bias_prediction(row, base, history, 2, 2)


def test_delaying_report_only_changes_predictions_after_release(causal_case):
    row, base, single, mixture, history = causal_case
    delayed = deepcopy(history)
    delayed[-1]['available_at'] = '2026-10-10T13:00:00+08:00'
    before = cr.response_prediction(row, base, mixture, single, delayed)
    assert before['type'] is None and before['history_n'] == 2
    after = cr.response_prediction(dict(row, sample_at='2026-10-10T13:00:00+08:00'), base, mixture, single, delayed)
    assert after['type'] == 1 and after['history_n'] == 3


def test_memory_expiration_and_liquid_extrapolation_fall_back(causal_case):
    row, base, single, mixture, history = causal_case
    expired = cr.response_prediction(dict(row, sample_at='2026-11-01T12:00:00+08:00'), base, mixture, single, history)
    assert expired['history_n'] == 0 and expired['fallback_base'] == 'K1'
    assert expired['value'] == cr.curve_values(row, base, single)[0]
    mixture['support'][0]['level_range'] = [30, 45]
    outside = cr.response_prediction(row, base, mixture, single, history)
    assert outside['reason'] == 'outside_selected_response_level_support'
    mixture['converged'] = False
    assert cr.response_prediction(row, base, mixture, single, history)['reason'] == 'curve_fit_not_converged'


def test_component_permutation_does_not_change_forecast(causal_case):
    row, base, single, mixture, history = causal_case
    expected = cr.response_prediction(row, base, mixture, single, history)
    reordered = deepcopy(mixture)
    for field in ('curves', 'mixing', 'support'):
        reordered[field].reverse()
    actual = cr.response_prediction(row, base, reordered, single, history)
    assert actual['value'] == pytest.approx(expected['value'])
    assert actual['posterior'] == pytest.approx(expected['posterior'][::-1])


def test_ambiguous_midnight_label_is_not_released_on_next_production_day(rows):
    r = cr.historical_rows([dict(rows[0], day='2026-06-16', timestamp='2026-06-16 00:18:00')])[0]
    assert r['available_at'] == '2026-06-18T00:00:00+08:00'
    prediction = dict(r, id='target', sample_at='2026-06-17T23:59:59+08:00')
    assert cr.eligible([r], prediction) == []
    prediction['sample_at'] = '2026-06-18T00:00:00+08:00'
    assert cr.eligible([r], prediction) == [r]


def test_temporal_training_is_purged_and_predictions_only_use_released_history(rows, evaluated):
    by_id = {r['id']: r for r in rows}
    stages = evaluated['tuning_validation']['folds']+[evaluated['forward_time_check']]
    for stage in stages:
        bundle = stage.get('models', stage.get('model'))
        first = cr.instant(stage['validation_first_time'])
        assert cr.instant(bundle['training_max_available_at']) <= first
        assert set(bundle['training_ids']).isdisjoint(stage['purged_ids'])
        for result in stage['results'].values():
            for p in result['predictions']:
                at = cr.instant(p['sample_at'])
                assert p['id'] not in p['history_ids']
                for key in p['history_ids']:
                    h = by_id[key]
                    assert cr.instant(h['available_at']) <= at
                    assert at-timedelta(days=14) <= cr.instant(h['sample_at']) < at


def test_tail_targets_cannot_change_tuning_or_frozen_parameters(rows, evaluated):
    changed = deepcopy(rows)
    latest = max(changed, key=lambda r: r['sample_at'])
    latest['ash_content'] = 50
    rerun = historical_evaluation(changed)
    assert rerun['selected'] == evaluated['selected']
    assert rerun['tuning_validation'] == evaluated['tuning_validation']
    assert rerun['forward_time_check']['model'] == evaluated['forward_time_check']['model']
    for name, result in rerun['forward_time_check']['results'].items():
        assert [p['value'] for p in result['predictions']] == [p['value'] for p in evaluated['forward_time_check']['results'][name]['predictions']]


def test_recent_assays_do_not_mutate_historical_fits_or_choices(rows, evaluated):
    joined = json.loads((ROOT/'backend/data/gpt_coarse/recent_assays/20261013/joined.json').read_text(encoding='utf-8'))
    before = deepcopy(evaluated)
    first = recent_evaluation(joined, rows, evaluated)
    for r in joined['rows']:
        r['ash_content'] = 99
    second = recent_evaluation(joined, rows, evaluated)
    assert evaluated == before
    assert first['results']['EXPERT8']['metrics']['mae'] != second['results']['EXPERT8']['metrics']['mae']


def test_raw_ash_is_not_an_input_and_curves_are_monotone(rows, evaluated):
    bundle = evaluated['historical_fit']['model']
    reference = rows[20]
    for model in bundle['curves'].values():
        for raw in (None, 0, 100):
            assert cr.curve_values(dict(reference, raw_ash=raw), bundle['base'], model) == pytest.approx(cr.curve_values(reference, bundle['base'], model))
        predictions = np.array([cr.curve_values(dict(reference, level=level), bundle['base'], model) for level in range(101)])
        assert np.isfinite(predictions).all()
        assert (np.diff(predictions, axis=0) <= 1e-10).all()
        assert model['sigma'] >= 1.5
    assert 'raw_ash' not in bundle['base']['feature_names']


def test_offline_bundle_cannot_be_activated(evaluated):
    with pytest.raises(ValueError):
        activate(None, dict(model=evaluated['historical_fit']['model'], status='production_ready'))
