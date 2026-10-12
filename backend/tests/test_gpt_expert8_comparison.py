"""Offline ablation invariants, honest time evaluation and production isolation."""
from copy import deepcopy
import itertools
import json
from pathlib import Path

import pytest

from app.gpt_coarse.experiments import expert8
from app.gpt_coarse.experiments.comparison import compare, split_rows
from app.gpt_coarse.predict import predict as predict9, valid as valid9
from app.gpt_coarse.repository import activate
from app.gpt_coarse.training import fit as fit9

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def original():
    return json.loads((ROOT/'backend/models/gpt_expert9/20261009.json').read_text(encoding='utf-8'))


@pytest.fixture(scope='module')
def candidate():
    return json.loads((ROOT/'backend/models/gpt_expert8/20261012/model.json').read_text(encoding='utf-8'))


def test_removing_raw_ash_is_real_and_cannot_change_fit_or_prediction(original, candidate):
    rows = [{k: v for k, v in r.items() if k != 'raw_ash'} for r in original['rows']]
    model = expert8.fit(rows, original['model']['alpha'])
    assert model == candidate['model']
    assert len(model['feature_names']) == 8 and 'raw_ash' not in model['feature_names']
    assert candidate['rows'] == rows
    expected = [expert8.predict(r, model) for r in rows]
    for raw in (None, 0, 100, float('nan'), 'invalid'):
        changed = [dict(r, raw_ash=raw) for r in rows]
        assert expert8.fit(changed, model['alpha']) == model
        assert [expert8.predict(r, model) for r in changed] == expected


def test_fit_matches_original_solver_with_raw_factor_inactive(original, candidate):
    # An independent controlled oracle: a constant raw-ash column in the old
    # implementation must produce the same fit as physically removing it.
    fixed_raw = [dict(r, raw_ash=40) for r in original['rows']]
    old = fit9(fixed_raw, original['model']['alpha'])
    new = candidate['model']
    assert old['coefs'][0] == 0
    assert new['coefs'] == pytest.approx(old['coefs'][1:], abs=1e-10)
    for r in fixed_raw:
        assert expert8.predict(r, new) == pytest.approx(predict9(r, old), abs=1e-10)


def test_expert_anchors_monotonicity_and_correction_bounds_are_preserved(candidate):
    model = candidate['model']
    ref = dict(zip(expert8.FEATURES[:-1], model['reference']))
    assert [expert8.predict(dict(ref, level=l), model) for l in (25, 50, 75)] == [23, 15.5, 8]
    for states in itertools.product((0, 1), repeat=6):
        row = dict(ref, **dict(zip(expert8.FEATURES[1:7], states)), coal_amount=1500)
        details = [expert8.explain(dict(row, level=level), model) for level in range(101)]
        assert all(abs(d['correction']) <= 2 for d in details)
        values = [d['value'] for d in details]
        assert all(b <= a+1e-12 for a, b in zip(values, values[1:]))
        assert values[0] == values[25] and values[75] == values[100]
    assert model['inactive_factors'] == ['sysA']
    assert model['coefs'][1] == 0


@pytest.mark.parametrize('key,value', [('coal_amount', None), ('coal_amount', 0), ('coal_amount', 10),
    ('sys401', .5), ('sysA', True), ('desliming473', None), ('level', float('nan')),
    ('level', -1), ('level', 101)])
def test_remaining_missing_or_invalid_factors_block_prediction(candidate, key, value):
    row = dict(candidate['rows'][0], **{key: value})
    assert expert8.predict(row, candidate['model']) is None
    with pytest.raises(ValueError):
        expert8.fit([row], .1)


def test_candidate_cannot_be_activated_by_production_loader(candidate):
    assert expert8.valid(candidate['model'])
    assert not valid9(candidate['model'])
    # activate rejects before any database access; no live session is supplied.
    with pytest.raises(ValueError, match='九因子'):
        activate(None, candidate)


def test_saved_report_and_models_reproduce_and_folds_are_temporal(original, candidate):
    model, report = compare(original)
    saved = json.loads((ROOT/'docs/verification/gpt-expert8-20261012/comparison.json').read_text(encoding='utf-8'))
    assert model == candidate['model']
    for key in ('historical_fit', 'tuning_validation', 'forward_time_check', 'expert_checks'):
        assert report[key] == saved[key]
    assert report['original_reproduction_max_abs_error'] < 1e-10
    assert report['tuning_validation']['selected_alpha'] == {'old': .1, 'new': .1}
    development, forward, folds = split_rows(original['rows'])
    assert (len(development), len(forward), len(folds)) == (142, 35, 3)
    assert {r['source_row'] for r in development}.isdisjoint(r['source_row'] for r in forward)
    for training, validation in folds:
        assert max(r['day'] for r in training) < min(r['day'] for r in validation)
        assert max(r['day'] for r in validation) < min(r['day'] for r in forward)
    assert report['forward_time_check']['frozen_models']['new']['n'] == 142


def test_forward_labels_never_change_validation_or_forward_model(original):
    _, report = compare(original)
    changed = deepcopy(original)
    cutoff = report['forward_time_check']['start']
    for row in changed['rows']:
        if row['day'] >= cutoff:
            row['ash_content'] = 60
    # Update only the historical reference fit so the reproduction guard passes.
    changed['model'] = fit9(changed['rows'], original['model']['alpha'])
    _, other = compare(changed)
    assert other['tuning_validation'] == report['tuning_validation']
    assert other['forward_time_check']['frozen_models'] == report['forward_time_check']['frozen_models']
