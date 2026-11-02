"""Synthetic leakage, identifiability and frozen-input contracts for phase2."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from scripts.gpt_factor_relation_experiment import (
    BJ, LOCKED_PROTOCOL_SHA, ROOT, SPECS, available, check_invariants, fence,
    file_hash, fit_model, inside, make_fold, midnight, predict_model, run,
    verify_manifest, write_json,
)


@pytest.fixture
def protocol():
    return json.loads((ROOT/'docs/verification/gpt-factor-relation-experiment-20261102.protocol.json').read_text(encoding='utf-8'))


def row(identifier, *, level=50.0, coal=600.0, ash=12.0, day=16, hour=9):
    observed = datetime(2026, 10, day, hour, identifier % 60, tzinfo=BJ)
    fixed = dict(coal_amount=coal, sysA=0, sysB=1, sys401=1, sys402=1,
                 desliming473=0, desliming474=0)
    return dict(assay_id=identifier, sample_at=observed.isoformat(),
                created_at=(observed+timedelta(minutes=30)).isoformat(),
                sample_local_date=observed.date().isoformat(), ash=ash,
                eligibility='strict', common_valid=True, fixed_factor_status={'strict': True},
                six_switch_combination='0,1,1,1,0,0', system_count=3, fixed_features=fixed,
                levels={str(lag): dict(value=level+(lag-90)/30*(identifier % 3),
                                       status='good', reading_id=identifier*1000+lag) for lag in (60, 90, 120)})


def frozen():
    return dict(kind='gpt-expert8-no-raw-v1',
                feature_names=['coal_amount', 'sysA', 'sysB', 'sys401', 'sys402', 'desliming473', 'desliming474', 'level'],
                reference=[600, 0, 1, 1, 1, 0, 0], scale=[100, 1, 1, 1, 1, 1, 1], coefs=[0]*7,
                bounds=[[0, 2000], *[[0, 1] for _ in range(6)]],
                expert={'intercept': 30.5, 'slope': -0.3, 'level_range': [25, 75]}, correction_limit=2.0)


def test_availability_requires_both_times_strictly_before_beijing_midnight(protocol):
    cutoff = midnight('2026-10-20')
    candidates = [row(i, day=19) for i in range(1, 6)]
    candidates[1]['created_at'] = cutoff.isoformat()
    candidates[2]['sample_at'] = cutoff.isoformat()
    candidates[3]['created_at'] = (cutoff+timedelta(days=2)).isoformat()
    candidates[4]['sample_at'] = (cutoff-timedelta(milliseconds=1)).astimezone(timezone.utc).isoformat()
    candidates[4]['created_at'] = (cutoff-timedelta(milliseconds=1)).isoformat()
    assert [r['assay_id'] for r in available(candidates, cutoff, protocol)] == [1, 5]


def test_linear_quantiles_iqr_floor_and_inclusive_fence_are_training_only(protocol):
    training = [row(i, ash=y) for i, y in enumerate([10, 10, 10, 10, 14], 1)]
    bounds = fence(training, protocol)
    assert bounds == dict(lower=8.5, upper=11.5, q1=10.0, q3=10.0, raw_iqr=0.0, effective_iqr=1.0)
    assert inside(8.5, bounds) and inside(11.5, bounds)
    assert not inside(11.500001, bounds)
    # An extreme validation label is not part of the call's training labels.
    validation = row(99, ash=99)
    assert not inside(validation['ash'], bounds)
    assert fence(training, protocol) == bounds


def test_all_models_and_lags_share_one_fold_training_fence_and_ids(protocol):
    training = [row(i, level=30+i*4, coal=500+(i % 3)*30, ash=y)
                for i, y in enumerate([10, 10.4, 11, 11.4, 12, 30], 1)]
    snapshot = make_fold(training, 'historical_replay', '2026-10-20', midnight('2026-10-20'), protocol)
    assert snapshot['excluded_training_ids'] == [6]
    for models in snapshot['models'].values():
        for fitted in models.values():
            assert fitted['raw_training_ids'] == list(range(1, 7))
            assert fitted['normal_training_ids'] == list(range(1, 6))
            assert fitted['excluded_training_ids'] == [6]
    assert training[-1]['ash'] == 30  # retained, never corrected or deleted


def test_centered_scaled_ols_recovers_raw_unit_coefficients_and_scores_extrapolation(protocol):
    training = [row(i, level=level, coal=coal, ash=4+.25*level+.01*coal)
                for i, (level, coal) in enumerate([(20, 500), (30, 700), (40, 550), (60, 600), (70, 450), (80, 650)], 1)]
    fitted = fit_model(training, training, [], 'M3', 90, protocol)
    assert fitted['status'] == 'ok'
    assert fitted['parameters'] == pytest.approx({'intercept': 4, 'level': .25, 'coal_amount': .01})
    assert fitted['rank'] == 3
    assert predict_model(fitted, dict(level=95, coal_amount=850)) == pytest.approx(36.25)


@pytest.mark.parametrize('mode,expected', [('constant', 'insufficient_predictor_variation'),
                                          ('collinear', 'rank_deficient'),
                                          ('too_few', 'insufficient_normal_labels_for_residual_degree_of_freedom')])
def test_nonidentifiable_fits_never_expose_pseudoinverse_coefficients(protocol, mode, expected):
    training = [row(i, level=30+i*5, coal=400+i*25, ash=10+i*.3) for i in range(1, 7)]
    if mode == 'constant':
        for source in training:
            source['fixed_features']['coal_amount'] = 500
    if mode == 'too_few':
        training = training[:3]
    fitted = fit_model(training, training, [], 'M3', 90, protocol)
    assert fitted['status'] == 'unidentifiable' and fitted['reason'] == expected
    assert fitted['parameters'] is fitted['standardized_parameters'] is None
    assert predict_model(fitted, dict(level=50, coal_amount=500)) is None


@pytest.fixture
def synthetic_run(protocol):
    sources = [row(i, level=25+i*8, coal=q, ash=y)
               for i, (q, y) in enumerate([(500, 10), (540, 11), (510, 12), (580, 13), (530, 14)], 1)]
    sources += [row(6, level=90, coal=800, ash=12, day=20), row(7, level=45, coal=540, ash=13, day=21)]
    protocol['primary_assay_ids'] = [r['assay_id'] for r in sources]
    before = copy.deepcopy(sources)
    result = run(sources, protocol, frozen())
    assert sources == before
    return result, protocol


def test_M0_M2_are_exactly_lag_invariant_and_extrapolations_are_retained(synthetic_run):
    result, protocol = synthetic_run
    assert all(result['invariants'].values())
    extrapolated = [r for r in result['predictions'] if r['assay_id'] == 6 and r['stage'] == 'historical_replay' and r['method'] == 'M3']
    assert len(extrapolated) == 3
    assert all(r['outside_training_range'] and r['status'] == 'ok' and r['error'] is not None for r in extrapolated)
    # Independent mutation must be detected even when every input ID remains present.
    broken = copy.deepcopy(result['predictions'])
    target = next(r for r in broken if r['method'] == 'M2' and r['lag_seconds'] == 120)
    target['prediction'] += .01
    with pytest.raises(AssertionError, match='lag_dependent_predictions'):
        check_invariants(result['folds'], broken, result['metrics'], result['coverage'], protocol)


def test_operational_minimum_is_five_raw_available_labels_not_an_accuracy_claim(protocol):
    training = [row(i, level=25+i*8, coal=500+(i % 3)*40, ash=10+i) for i in range(1, 5)]
    snapshot = make_fold(training, 'historical_replay', '2026-10-20', midnight('2026-10-20'), protocol)
    assert snapshot['status'] == 'insufficient_available_labels'
    assert all(fitted['parameters'] is None and fitted['reason'] == 'insufficient_available_labels'
               for models in snapshot['models'].values() for fitted in models.values())


def test_protocol_and_input_hash_tampering_are_rejected_without_changing_files(tmp_path):
    protocol_path = ROOT/'docs/verification/gpt-factor-relation-experiment-20261102.protocol.json'
    assert file_hash(protocol_path) == LOCKED_PROTOCOL_SHA
    source = tmp_path/'source.py'
    source.write_text('frozen source\n', encoding='utf-8')
    artifact = tmp_path/'pairings.json'
    artifact.write_text('{"frozen":true}\n', encoding='utf-8')
    manifest = dict(files={'pairings.json': dict(bytes=artifact.stat().st_size, sha256=file_hash(artifact))},
                    source_scripts={'source.py': file_hash(source)})
    write_json(tmp_path/'manifest.json', manifest)
    expected = file_hash(tmp_path/'manifest.json')
    verify_manifest(tmp_path, expected, tmp_path)
    artifact.write_text('{"frozen":false}\n', encoding='utf-8')
    with pytest.raises(ValueError, match='artifact changed'):
        verify_manifest(tmp_path, expected, tmp_path)
    assert artifact.read_text(encoding='utf-8') == '{"frozen":false}\n'
