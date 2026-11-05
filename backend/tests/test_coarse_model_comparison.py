"""Paired offline export, fail-closed package loading, and DB-free route."""
import csv
from copy import deepcopy
import hashlib
import importlib.util
import json
import math
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.services import coarse_model_comparison as service
from app.routers.training import router

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/export_coarse_model_comparison.py'
spec = importlib.util.spec_from_file_location('comparison_export', SCRIPT)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


@pytest.fixture
def evidence(tmp_path):
    meta = dict(
        scope=dict(total_n=4, evaluable_n=2, audited_n=1, review_n=1, rejected_n=2,
                   description='4 exposed: 1 audited, 1 review, 2 rejected', data_cutoff=None,
                   by_system={'3':dict(total_n=2,evaluable_n=1,audited_n=1,review_n=0,rejected_n=1),
                              '2':dict(total_n=2,evaluable_n=1,audited_n=0,review_n=1,rejected_n=1)}),
        freeze=dict(status='provisional', model_version='test', model_sha256='a' * 64,
                    sealed_at=None, due_at=None),
        models={role: dict(id=role, name=role, version='test', status='frozen' if role == 'current' else 'provisional',
                           method=dict(summary='Actual model metadata', details=[],
                                       by_system={'2':dict(summary='Actual two-system rule',details=[]),
                                                  '3':dict(summary='Frozen EX8',details=[])}))
                for role in ('current', 'candidate')},
        notes=dict(history_all='Historical Evaluation · MAE', history_forward='TSCV · MAE; development diagnostic', future='OOT · MAE; pending'),
        evaluation_pools={'history_forward':dict(total_n=3,by_system={'3':dict(total_n=2),'2':dict(total_n=1)},
                          fold_description='Expanding earlier-date-only; one rejected target remains in denominator')},
    )
    rows = [dict(phase=phase, record_id=str(i), date='2026-11-02', system_count=str(system),
                 actual=10, current_prediction=old, candidate_prediction=new,
                 normal=normal, train_max_date='2026-11-01', fold='earlier-date-only',
                 train_min_date='2026-10-01',
                 cohort='eligible' if i == 1 else 'label_review')
            for phase in ('history_all', 'history_forward')
            for i, system, old, new, normal in ((1, 3, 12, 10.5, 'true'), (2, 2, 9, 11.5, 'false'))]
    def write():
        (tmp_path / 'metadata.json').write_text(json.dumps(meta), encoding='utf-8')
        with (tmp_path / 'predictions.csv').open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        (tmp_path / 'evidence-manifest.json').write_text(json.dumps({'files': {
            name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
            for name in ('metadata.json', 'predictions.csv')}}), encoding='utf-8')
        return tmp_path
    write()
    return meta, rows, write


def test_recomputed_same_pool_overall_system_normal_and_future(evidence):
    _, _, write = evidence
    result = exporter.build_summary(write())
    a = result['models']['current']['metrics']
    b = result['models']['candidate']['metrics']
    assert a['history_all']['mae'] == 1.5
    assert a['history_all']['rmse'] == pytest.approx(math.sqrt(2.5))
    assert a['history_all']['bias'] == .5
    assert a['history_all']['r2'] is None  # constant actual labels
    assert b['history_all']['mae'] == 1.0
    assert a['history_all']['n'] == b['history_all']['n'] == 2
    assert b['history_all']['coverage'] == .5
    assert b['history_all']['hit1'] == 1
    assert b['history_all']['by_system']['3']['mae'] == .5
    assert b['history_all']['normal']['mae'] == .5
    assert b['history_all']['normal']['rmse'] == .5
    assert b['history_all']['normal']['bias'] == .5
    assert b['history_all']['audited']['n'] == 1
    assert b['history_all']['review']['n'] == 1
    assert b['history_all']['by_system']['2']['review']['mae'] == 1.5
    assert result['scope']['by_system']['3']['rejected_n'] == 1
    assert result['models']['current']['method']['by_system']['2']['summary'] == 'Actual two-system rule'
    assert a['history_forward']['n'] == b['history_forward']['n']
    assert b['history_forward']['eligible_n'] == 3
    assert b['history_forward']['coverage'] == pytest.approx(2 / 3)
    assert b['history_forward']['by_system']['3']['coverage'] == .5
    assert b['history_forward']['date_range'] == {'start':'2026-11-02','end':'2026-11-02'}
    assert b['history_forward']['train_date_range'] == {'start':'2026-10-01','end':'2026-11-01'}
    assert a['history_forward']['train_date_range'] is None  # EX8 is never refit on candidate fold dates
    assert b['future']['status'] == 'pending'
    assert b['future']['mae'] is None
    assert all(b['future'][key] is None for key in ('rmse','bias','r2','coverage','date_range','train_date_range'))
    assert result['status'] == 'research'


def test_r_squared_is_negative_when_worse_than_mean_and_null_when_undefined():
    rows = [dict(actual=0., prediction=3., date='2026-11-01'),
            dict(actual=1., prediction=3., date='2026-11-02')]
    result = exporter._score(rows, 'prediction', 3)
    assert result['r2'] == -25.0
    assert result['rmse'] == pytest.approx(math.sqrt(6.5))
    assert result['bias'] == 2.5
    assert result['n'] == 2 and result['eligible_n'] == 3
    assert result['coverage'] == pytest.approx(2/3)
    assert exporter._score(rows[:1], 'prediction')['r2'] is None
    assert exporter._score([dict(actual=1.,prediction=2.,date='2026-11-01')]*2,'prediction')['r2'] is None
    assert exporter._score([dict(actual=0.,prediction=0.,date='2026-11-01'),dict(actual=1.,prediction=1.,date='2026-11-02')],'prediction')['r2'] == 1.
    assert exporter._score([dict(actual=5.,prediction=1.,date='2026-11-01')],'prediction')['bias'] == -4.


def test_only_unchanged_two_system_fixed_policy_may_have_no_training_dates(evidence):
    _, rows, write = evidence
    target = rows[-1]
    target.update(training_mode='fixed_policy',fold='fixed-policy:no-training',
                  train_min_date='',train_max_date='',current_prediction=14.,candidate_prediction=14.)
    for row in rows:
        row.setdefault('training_mode','learned')
    body = exporter.build_summary(write())
    metric = body['models']['candidate']['metrics']['history_forward']['by_system']['2']
    assert metric['n'] == 1 and metric['train_date_range'] is None and metric['fold_count'] == 0
    assert '零训练' in metric['training_note']
    target['system_count'] = '3'
    with pytest.raises(ValueError,match='only explicit two-system'):
        exporter.build_summary(write())
    target['system_count'] = '2'
    target['candidate_prediction'] = 13.
    with pytest.raises(ValueError,match='unchanged actual 14/15'):
        exporter.build_summary(write())


def test_candidate_full_training_dates_do_not_become_current_or_forward_dates(evidence):
    metadata, _, write = evidence
    dates = {'start':'2026-10-16','end':'2026-11-03'}
    metadata['models']['candidate']['training_date_range'] = dates
    metadata['models']['current']['training_date_range'] = None
    metadata['evaluation_pools']['history_all'] = dict(
        total_n=4,audited_n=1,review_n=1,
        by_system={'3':dict(total_n=2,audited_n=1,review_n=0),
                   '2':dict(total_n=2,audited_n=0,review_n=1)},
        candidate_train_date_range=dates,
    )
    metadata['freeze']['freeze_manifest_sha256'] = 'b' * 64
    metadata['freeze']['sealed_runtime_manifest_sha256'] = 'c' * 64
    # Real historical replay rows have no forward fit dates.
    for row in evidence[1]:
        if row['phase'] == 'history_all':
            row['train_min_date'] = row['train_max_date'] = ''
    body = exporter.build_summary(write())
    assert body['models']['current']['metrics']['history_all']['train_date_range'] is None
    assert body['models']['candidate']['metrics']['history_all']['train_date_range'] == dates
    assert body['models']['candidate']['metrics']['history_forward']['train_date_range']['end'] == '2026-11-01'
    assert body['freeze']['freeze_manifest_sha256'] == 'b' * 64


def _with_future(evidence, predictions, pending_labels=0, forecast_counts=None):
    metadata, rows, write = evidence
    metadata['freeze'].update(status='accepted',sealed_at='2026-11-03T00:00:00Z',due_at='2026-11-10T00:00:00Z')
    metadata['evaluation_pools']['future'] = dict(total_n=len(predictions),pending_label_n=pending_labels,
        by_system={'3':dict(total_n=len(predictions))},prediction_available_n=forecast_counts or {},
        baseline_id='EX8_PRODUCTION_RUNS',baseline_label='现用EX8生产RUNS口径（参数冻结）',
        candidate_baseline_id='EX81_PRODUCTION_CANDIDATE',candidate_baseline_label='冻结候选纯推理')
    for row in rows:
        row.setdefault('current_reason','')
        row.setdefault('candidate_reason','')
    for i,(left,right) in enumerate(predictions):
        rows.append(dict(rows[0],phase='future',record_id=f'future-{i}',system_count='3',
            date=f'2026-11-{5+i:02d}',actual=10.,normal='true',cohort='',train_min_date='',train_max_date='',fold='',
            current_prediction='' if left is None else left,candidate_prediction='' if right is None else right,
            current_reason='missing_ex8_required_inputs' if left is None else '',
            candidate_reason='outside_candidate_support' if right is None else ''))
    return write


@pytest.mark.parametrize('left,right,expected_left_n,expected_right_n',[(None,10.5,0,1),(11.,None,1,0),(None,None,0,0),(12.,10.5,1,1)])
def test_future_models_score_independently_and_common_pool_is_explicit(evidence,left,right,expected_left_n,expected_right_n):
    write = _with_future(evidence,[(left,right)])
    body = exporter.build_summary(write())
    for role,n,prediction in [('current',expected_left_n,left),('candidate',expected_right_n,right)]:
        metric = body['models'][role]['metrics']['future']
        assert metric['n'] == n and metric['eligible_n'] == 1
        assert metric['status'] == ('available' if n else 'unavailable')
        assert metric['by_system']['3']['status'] == metric['status']
        assert metric['coverage'] == n
        assert metric['labelled_n'] == 1 and metric['pending_label_n'] == 0 and metric['attempted_n'] == 1
        assert metric['prediction_coverage'] == n
        if role == 'current':
            assert metric['baseline_id'] == metric['by_system']['3']['baseline_id'] == 'EX8_PRODUCTION_RUNS'
            assert '生产RUNS' in metric['training_note']
        else:
            assert metric['baseline_id'] == 'EX81_PRODUCTION_CANDIDATE'
        assert '纯推理' in metric['training_note']
        assert metric['fold_count'] == 0
        if n:
            assert metric['mae'] == abs(prediction-10.)
            assert metric['rmse'] == abs(prediction-10.)
            assert metric['bias'] == prediction-10.
            assert metric['normal']['n'] == 1
        else:
            assert all(metric[key] is None for key in ('mae','rmse','bias','r2','hit1','hit1_rate','date_range'))
            assert metric['reason_counts']
    common = body['future_comparison']
    common_n = int(left is not None and right is not None)
    assert common['n'] == common_n and common['coverage'] == common_n
    assert common['current']['n'] == common['candidate']['n'] == common_n
    assert common['status'] == ('available' if common_n else 'unavailable')
    assert common['by_system']['3']['n'] == common_n
    assert common['baseline_id'] == 'EX8_PRODUCTION_RUNS'
    assert common['candidate_baseline_id'] == 'EX81_PRODUCTION_CANDIDATE'


def test_future_disjoint_predictions_cannot_claim_paired_improvement(evidence):
    write = _with_future(evidence,[(None,10.5),(11.,None),(12.,10.)])
    body = exporter.build_summary(write())
    left = body['models']['current']['metrics']['future']
    right = body['models']['candidate']['metrics']['future']
    assert left['n'] == right['n'] == 2
    assert left['mae'] == 1.5 and right['mae'] == .25
    common = body['future_comparison']
    assert common['n'] == 1 and common['coverage'] == pytest.approx(1/3)
    assert common['current']['mae'] == 2.
    assert common['candidate']['mae'] == 0.
    assert common['current']['date_range'] == {'start':'2026-11-07','end':'2026-11-07'}


def test_future_both_individually_available_but_no_common_targets(evidence):
    body = exporter.build_summary(_with_future(evidence,[(None,10.5),(11.,None)])())
    assert body['models']['current']['metrics']['future']['status'] == 'available'
    assert body['models']['candidate']['metrics']['future']['status'] == 'available'
    common = body['future_comparison']
    assert common['n'] == 0 and common['coverage'] == 0.
    assert common['status'] == 'unavailable'
    assert common['current']['mae'] is None and common['candidate']['mae'] is None


def test_future_current_only_zero_one_four_system_targets_are_retained(evidence):
    write = _with_future(evidence,[(11.,None),(12.,None),(9.,None)])
    metadata,rows,_ = evidence
    future = [row for row in rows if row['phase'] == 'future']
    for system,row in zip(('0','1','4'),future):
        row['system_count'] = system
        row['candidate_reason'] = 'unsupported_system_count'
    metadata['evaluation_pools']['future']['by_system'] = {system:dict(total_n=1) for system in ('0','1','4')}
    body = exporter.build_summary(write())
    current = body['models']['current']['metrics']['future']
    candidate = body['models']['candidate']['metrics']['future']
    assert current['n'] == 3 and current['coverage'] == 1.
    assert current['mae'] == pytest.approx(4/3)
    assert candidate['n'] == 0 and candidate['status'] == 'unavailable'
    assert set(current['by_system']) == set(candidate['by_system']) == {'0','1','4'}
    assert all(current['by_system'][system]['n'] == 1 for system in ('0','1','4'))
    assert body['future_comparison']['n'] == 0
    assert set(body['future_comparison']['by_system']) == {'0','1','4'}
    rows[0]['system_count'] = '4'
    with pytest.raises(ValueError,match='unsupported system count'):
        exporter.build_summary(write())  # historical contract remains only2/3


def test_future_prediction_and_label_coverage_do_not_hide_pending_labels(evidence):
    write = _with_future(evidence,[(11.,10.5)],pending_labels=2,forecast_counts={'current':1,'candidate':3})
    body = exporter.build_summary(write())
    left,right = [body['models'][role]['metrics']['future'] for role in ('current','candidate')]
    assert left['n'] == right['n'] == 1
    assert left['coverage'] == right['coverage'] == 1.
    assert left['attempted_n'] == right['attempted_n'] == 3
    assert left['pending_label_n'] == right['pending_label_n'] == 2
    assert left['prediction_coverage'] == pytest.approx(1/3)
    assert right['prediction_coverage'] == 1.
    assert body['future_comparison']['pending_label_n'] == 2
    metadata,_,write_original = evidence
    metadata['evaluation_pools']['future']['prediction_available_n'] = {}
    unknown = exporter.build_summary(write_original())
    assert unknown['models']['candidate']['metrics']['future']['prediction_coverage'] is None


def test_future_without_revealed_labels_stays_pending(evidence):
    write = _with_future(evidence,[],pending_labels=2)
    body = exporter.build_summary(write())
    for role in ('current','candidate'):
        metric = body['models'][role]['metrics']['future']
        assert metric['status'] == 'pending' and metric['n'] == 0
        assert metric['pending_label_n'] == 2 and metric['attempted_n'] == 2
        assert metric['mae'] is None and metric['prediction_coverage'] is None
    assert body['future_comparison']['status'] == 'pending'


def test_future_null_prediction_requires_reason_and_never_accepts_infinite_actual(evidence):
    write = _with_future(evidence,[(None,10.5)])
    metadata,rows,_ = evidence
    rows[-1]['current_reason'] = ''
    with pytest.raises(ValueError,match='requires a safe reason'):
        exporter.build_summary(write())
    rows[-1]['current_reason'] = 'missing_ex8_required_inputs'
    rows[-1]['actual'] = 'nan'
    with pytest.raises(ValueError,match='finite'):
        exporter.build_summary(write())


@pytest.mark.parametrize('fault', ['same_day', 'one_prediction', 'duplicate', 'future_without_seal', 'shrunk_scope', 'missing_cohort', 'wrong_cohort', 'wrong_system'])
def test_reject_misleading_evidence(evidence, fault):
    _, rows, write = evidence
    if fault == 'same_day':
        rows[-1]['train_max_date'] = rows[-1]['date']
    elif fault == 'one_prediction':
        rows[0]['current_prediction'] = ''
    elif fault == 'duplicate':
        rows.append(deepcopy(rows[0]))
    elif fault == 'future_without_seal':
        future = dict(rows[-1], phase='future')
        rows.append(future)
    elif fault == 'missing_cohort':
        rows[0]['cohort'] = ''
    elif fault == 'wrong_cohort':
        rows[0]['cohort'] = 'label_review'
    elif fault == 'wrong_system':
        rows[0]['system_count'] = '2'
    else:
        rows.pop(0)
    with pytest.raises(ValueError):
        exporter.build_summary(write())


def test_reject_evidence_hash_mismatch(evidence):
    _, _, write = evidence
    path = write()
    (path / 'predictions.csv').write_text('damaged', encoding='utf-8')
    with pytest.raises(ValueError, match='hash mismatch'):
        exporter.build_summary(path)


def test_loading_route_does_not_query_database_or_change_artifact(evidence, monkeypatch, tmp_path):
    _, _, write = evidence
    result = exporter.build_summary(write())
    artifact = tmp_path / 'summary.json'
    artifact.write_text(json.dumps(result), encoding='utf-8')
    before = artifact.read_bytes()
    monkeypatch.setattr(service, 'SUMMARY_PATH', artifact)
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    # Standalone router has no app startup, workers or DB bootstrap.
    response = TestClient(app).get('/api/v1/training/coarse-model-comparison')
    assert response.status_code == 200
    assert response.json() == result
    assert artifact.read_bytes() == before
    assert 'actual' not in response.text and 'record_id' not in response.text
    route = next(r for r in router.routes if r.path.endswith('/coarse-model-comparison'))
    assert not route.dependant.dependencies


def test_fail_closed_missing_invalid_extra_and_expired(evidence, monkeypatch, tmp_path):
    _, _, write = evidence
    artifact = tmp_path / 'summary.json'
    monkeypatch.setattr(service, 'SUMMARY_PATH', artifact)
    assert service.load_comparison()['status'] == 'missing'
    result = exporter.build_summary(write())
    artifact.write_text(json.dumps(dict(result, artifact_sha256='0' * 64)), encoding='utf-8')
    assert service.load_comparison()['status'] == 'invalid'
    altered = dict(result, arbitrary_file='C:/secret')
    altered['artifact_sha256'] = service.digest(altered)
    artifact.write_text(json.dumps(altered), encoding='utf-8')
    assert service.load_comparison()['status'] == 'invalid'
    result['valid_until'] = '2020-01-01T00:00:00Z'
    result['artifact_sha256'] = service.digest(result)
    artifact.write_text(json.dumps(result), encoding='utf-8')
    assert service.load_comparison()['status'] == 'stale'


def test_export_is_explicit_and_only_writes_package_and_receipt(evidence, monkeypatch, tmp_path):
    _, _, write = evidence
    source = write()
    dest = tmp_path / 'packaged.json'
    monkeypatch.setattr(exporter, 'SUMMARY_PATH', dest)
    before = (source / 'predictions.csv').read_bytes()
    result = exporter.export(source)
    assert json.loads(dest.read_text()) == result
    assert json.loads((source / 'metrics-recomputed.json').read_text()) == result
    assert before == (source / 'predictions.csv').read_bytes()


def test_actual_packaged_evidence_matches_http_and_independent_csv_recalculation():
    root = Path(__file__).resolve().parents[2]
    csv_path = root / 'output/gpt-expert81-db-primary-20261103/comparison-evidence/predictions.csv'
    if not csv_path.exists() or not service.SUMMARY_PATH.exists():
        pytest.skip('real offline comparison evidence not packaged on this machine')
    before = service.SUMMARY_PATH.read_bytes()
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    response = TestClient(app).get('/api/v1/training/coarse-model-comparison')
    assert response.status_code == 200
    body = response.json()
    assert body['status'] in ('research', 'ready')
    with csv_path.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    for phase in service.PHASES:
        pool = [r for r in rows if r['phase'] == phase]
        for role in ('current', 'candidate'):
            metric = body['models'][role]['metrics'][phase]
            assert metric['n'] == len(pool)
            if not pool:
                assert metric['mae'] is None
                continue
            subsets = [(pool, metric)] + [
                ([r for r in pool if r['system_count'] == system], result)
                for system, result in metric['by_system'].items()]
            for subset, score in subsets:
                for cohort, key in ((None, None), ('eligible', 'audited'), ('label_review', 'review'), ('normal', 'normal')):
                    if key in ('audited', 'review') and phase != 'history_all':
                        continue
                    selected = subset if cohort is None else ([r for r in subset if r['normal'].lower() == 'true'] if key == 'normal' else [r for r in subset if r['cohort'] == cohort])
                    actual = score if key is None else score[key]
                    if actual['eligible_n'] is not None and actual['eligible_n'] > 0:
                        assert actual['coverage'] == pytest.approx(len(selected)/actual['eligible_n'],abs=1e-12)
                    errors = [abs(float(r[role + '_prediction']) - float(r['actual'])) for r in selected]
                    assert actual['n'] == len(errors)
                    if errors:
                        assert actual['mae'] == pytest.approx(math.fsum(errors) / len(errors), abs=1e-12)
                        assert actual['hit1'] == sum(e <= 1 for e in errors)
                        signed = [float(r[role + '_prediction']) - float(r['actual']) for r in selected]
                        mse = math.fsum(error ** 2 for error in signed) / len(signed)
                        assert actual['rmse'] == pytest.approx(math.sqrt(mse), abs=1e-12)
                        assert actual['bias'] == pytest.approx(math.fsum(signed) / len(signed), abs=1e-12)
                        center = math.fsum(float(r['actual']) for r in selected) / len(selected)
                        sst = math.fsum((float(r['actual'])-center)**2 for r in selected)
                        if len(selected) >= 2 and sst > 0:
                            assert actual['r2'] == pytest.approx(1 - mse*len(selected)/sst, abs=1e-12)
                        else:
                            assert actual['r2'] is None
                    else:
                        assert actual['mae'] is None
    assert service.SUMMARY_PATH.read_bytes() == before
