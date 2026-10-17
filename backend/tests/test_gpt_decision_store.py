"""Opt-in GPT store has identical decisions without historical mirror reads."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path

import pytest
from sqlalchemy import delete, event, select

from app.gpt_coarse.models import GptCoarseRevision
from app.gpt_coarse.repository import activate
from app.gpt_density.hourly_anchor import capture
from app.gpt_runtime.decision_store import load_decision_store
from app.decision_hourly.models import DecisionCycleContext, DecisionCycleHour
from app.decision_hourly.service import context_definition, read_hour
from app.decision_hourly.statistics import digest
from app.hourly_brief.inputs import LOCAL
from app.models import AutoState, CoarseModel, Setting
from app.services.decision_live import gpt_group
from app.services.decision_sources import shared_snapshot
from app.services.state import load_store
from app.telemetry.domain import iso_time
from app.telemetry.models import TelemetryReading
from test_decision_sources import seeded
from test_hourly_brief import HOUR, sessions, send


ROOT = Path(__file__).resolve().parents[2]
RELATED_KEYS = ('gptExpertModel', 'coarseModel', 'coarseModelVariants',
                'densityGuide', 'densityLastMoveAt', 'densityActionLatch')
CASES = ('variant', 'expert8', 'expert9', 'current', 'ds_only', 'missing', 'bad_expert', 'bad_legacy')


def artifact(method):
    relative = ('backend/models/gpt_expert8/20261012/production.json' if method == 'expert8'
                else 'backend/models/gpt_expert9/20261009.json')
    value = json.loads((ROOT/relative).read_text(encoding='utf-8'))
    value['trained_at'] = iso_time(HOUR-timedelta(days=2))
    return value


def local_naive(when):
    return when.astimezone(LOCAL).replace(tzinfo=None)


def install_case(sessions, case):
    with sessions.begin() as db:
        before = local_naive(HOUR-timedelta(minutes=1))
        db.add(Setting(key='density_guide', value={
            'maxStep': .021, 'rhoMin': 1.36, 'rhoMax': 1.59,
            'unconsumed_context_field': {'preserve': True}}, updated_at=before))
        db.add(Setting(key='ash_target', value=99, updated_at=before))
        db.add(Setting(key='total_ash_manual_on', value=True, updated_at=before))
        db.add(AutoState(key='densityLastMoveAt', value=HOUR.timestamp()*1000, updated_at=before))
        db.add(AutoState(key='densityActionLatch', value={'key': 'old-data-lock'}, updated_at=before))
        db.add(AutoState(key='ashInputs', value={'totalAsh': {'manual': 99, 'manualAt': 0}}, updated_at=before))
        if case in ('current', 'ds_only', 'missing', 'bad_legacy'):
            db.execute(delete(AutoState).where(AutoState.key == 'coarseModelVariants'))
        if case in ('current', 'ds_only', 'bad_legacy'):
            db.add(CoarseModel(method='mlr', is_current=True, intercept=3,
                coefs=[.1, .002, 0, 0, 0, 0, 0, 0, 0, .02] if case != 'bad_legacy' else [1],
                feature_names=['deliberately-not-copied'], means=[1], stds=[2], std_coef=[3],
                metrics={'version': 1 if case == 'ds_only' else 2,
                         'inputPolicy': 'clip-training-range', 'inputBounds': [None]*10,
                         'config': {'preserve': 'legacy'}},
                impute_means=[4], n=22, tolerance=.6, train_range='fixture',
                trained_at='2026-10-08 09:58:00', created_at=before))
            db.add(CoarseModel(method='pls', is_current=False, intercept=4, coefs=[0]*10,
                means=[1], stds=[2], std_coef=[3], metrics={'version': 1}, pls_A=2,
                impute_means=[4], n=23, tolerance=.7, train_range='fixture2',
                trained_at='2026-10-08 09:59:00', created_at=before))
        if case in ('expert8', 'expert9'):
            activate(db, artifact(case), HOUR-timedelta(seconds=1))
        if case == 'bad_expert':
            broken = artifact('expert8')
            broken['model']['coefs'] = [1]
            db.add(GptCoarseRevision(artifact_id='isolated-broken', activated_at=iso_time(HOUR-timedelta(seconds=1)),
                                    artifact=broken))


@contextmanager
def statements(engine):
    captured = []
    def before_execute(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            captured.append(statement)
    event.listen(engine, 'before_cursor_execute', before_execute)
    try:
        yield captured
    finally:
        event.remove(engine, 'before_cursor_execute', before_execute)


def compare_current(sessions, when=HOUR):
    with sessions() as db:
        original = shared_snapshot(db, when=when, live=True)
        original_context = context_definition(original, load_store(db, model_at=when))
    with sessions() as db:
        optimized = shared_snapshot(db, when=when, live=True, store_loader=load_decision_store)
        optimized_context = context_definition(optimized, load_decision_store(db, model_at=when))
    assert optimized == original
    assert optimized_context == original_context
    return optimized


@pytest.mark.parametrize('case', CASES)
def test_related_values_and_complete_current_json_match_legacy_loader(sessions, seeded, case):
    install_case(sessions, case)
    with sessions() as db:
        full = load_store(db, model_at=HOUR)
        small = load_decision_store(db, model_at=HOUR)
        for key in RELATED_KEYS:
            assert (key in small, small.get(key)) == (key in full, full.get(key)), key
        assert not {'coarseCoal', 'floatCoal', 'magneticTail', 'calcLogs', 'heavySamples', '_revision'} & small.keys()
        assert small.get('densityGuide', {}).get('unconsumed_context_field') == {'preserve': True}
        if small['coarseModel']:
            assert 'feature_names' not in small['coarseModel']['mlr']
    result = compare_current(sessions)
    if case in ('ds_only', 'missing'):
        assert result['model']['method'] is None
        assert result['coarse_ash']['value'] is None
    elif case == 'bad_legacy':
        assert result['coarse_ash']['value'] is None
    else:
        assert result['coarse_ash']['value'] is not None
        assert result['suggested_density']['value'] is not None


def test_loader_has_exactly_four_small_table_selects_and_never_state_revision(sessions, seeded, monkeypatch):
    install_case(sessions, 'expert8')
    engine = sessions.kw['bind']
    with sessions() as db, statements(engine) as old_sql:
        full = load_store(db, model_at=HOUR)
    def forbidden(*args, **kwargs):
        pytest.fail('Light decision loader called full-store revision machinery')
    monkeypatch.setattr('app.services.state.state_revision', forbidden)
    with sessions() as db, statements(engine) as new_sql:
        result = load_decision_store(db, model_at=HOUR)
    assert len(old_sql) == 14
    assert len(new_sql) == 4
    assert {key: result.get(key) for key in RELATED_KEYS} == {key: full.get(key) for key in RELATED_KEYS}
    assert all(not any(table in query.lower() for table in
        ('coal_records', 'calc_logs', 'heavy_samples')) for query in new_sql)


@pytest.mark.parametrize('offset', [-1, 0, 1])
def test_expert_activation_boundary_and_model_at_none_semantics(sessions, seeded, offset):
    with sessions.begin() as db:
        activate(db, artifact('expert8'), HOUR+timedelta(seconds=offset))
    with sessions() as db:
        for model_at in (HOUR, None):
            full = load_store(db, model_at=model_at)
            small = load_decision_store(db, model_at=model_at)
            assert small['gptExpertModel'] == full['gptExpertModel']
    result = compare_current(sessions)
    assert result['model']['method'] == ('expert8' if offset <= 0 else 'mlr')


@pytest.mark.parametrize('which', ['setting', 'auto', 'model-created', 'model-trained'])
@pytest.mark.parametrize('offset', [-1, 0, 1])
def test_existing_future_value_gates_are_unchanged(sessions, seeded, which, offset):
    install_case(sessions, 'current' if which == 'model-created' else 'variant')
    shifted = HOUR+timedelta(seconds=offset)
    with sessions.begin() as db:
        if which == 'setting':
            db.get(Setting, 'density_guide').updated_at = local_naive(shifted)
        elif which == 'auto':
            db.get(AutoState, 'coarseModelVariants').updated_at = local_naive(shifted)
        elif which == 'model-created':
            db.scalar(select(CoarseModel).where(CoarseModel.method == 'pls')).created_at = local_naive(shifted)
        else:
            row = db.get(AutoState, 'coarseModelVariants')
            row.value = {'gpt': {**row.value['gpt'], 'trainedAt': iso_time(shifted)}}
    compare_current(sessions)


@pytest.mark.parametrize('problem', ['offline', 'density-one', 'bad-level', 'missing-level', 'old-manual', 'future-manual'])
def test_input_fallbacks_and_blocking_reasons_stay_identical(sessions, seeded, problem):
    install_case(sessions, 'expert8')
    cfg, repo, values = seeded
    if problem == 'offline':
        repo.state(cfg, 'owner', 'stopped')
    elif problem == 'density-one':
        changed = list(values)
        changed[4] = 1
        send(repo, cfg, HOUR-timedelta(seconds=1), changed)
    elif problem in ('missing-level', 'bad-level'):
        with sessions.begin() as db:
            query = select(TelemetryReading).where(TelemetryReading.point_key == 'point2',
                TelemetryReading.received_at <= iso_time(HOUR-timedelta(seconds=90)))
            rows = list(db.scalars(query))
            for row in rows:
                if problem == 'missing-level':
                    db.delete(row)
                else:
                    row.sample = {**row.sample, 'quality': 'bad', 'issue': 'isolated-fixture'}
    else:
        from app.models import ManualMeasurement
        with sessions.begin() as db:
            row = db.scalar(select(ManualMeasurement).where(ManualMeasurement.metric == 'heavy_clean_coal_ash'))
            if problem == 'old-manual':
                row.time_basis = 'legacy-lag-2h'
                row.observed_at = iso_time(HOUR-timedelta(minutes=3))
            else:
                row.created_at = iso_time(HOUR+timedelta(seconds=1))
    compare_current(sessions)


def prepare_hour(sessions, monkeypatch):
    start = HOUR-timedelta(hours=1)
    ash_start = start-timedelta(hours=1)
    with sessions.begin() as db:
        current = shared_snapshot(db, when=HOUR, live=True)
        definition = context_definition(current, load_store(db))
        key = digest(definition)
        db.add(DecisionCycleContext(key=key, definition=definition))
        capture(db, HOUR, current, definition)
        base = dict(policy='decision-hourly-v2', period=dict(start=iso_time(start), end=iso_time(HOUR)),
            sources=current['sources'], model=current['model'], target=current['target'], density_label='A304',
            context_key=key, evidence=dict(segments=[dict(state=dict(context_key=key))]),
            cells=dict(density=dict(value=1.48, status='average'), total_ash=dict(value=9.1, status='average'),
                       suggested_density=dict(value=None, status='missing')),
            total_ash=dict(value=9.1, status='average'), suggested_density=dict(value=None),
            coverage=dict(ratio=0), guidance={}, notes=[])
        for period_start in (ash_start, start):
            db.add(DecisionCycleHour(start=iso_time(period_start), end=iso_time(period_start+timedelta(hours=1)),
                                    generated_at=iso_time(HOUR), snapshot=deepcopy(base)))
    settled = dict(revision='isolated-settlement', heavy_ash=dict(value=8.5, records=[]),
        item=dict(value=9.3, coverage=1., valid_seconds=3600, sample_count=720, provisional=True,
                  aggregation='output-mass-v1', reason='isolated settlement'))
    monkeypatch.setattr('app.decision_hourly.quality_advice.get_settlement', lambda *a, **kw: deepcopy(settled))
    return current


@pytest.mark.parametrize('case', CASES)
def test_complete_hourly_json_context_and_reasons_match(sessions, seeded, monkeypatch, case):
    install_case(sessions, case)
    current = prepare_hour(sessions, monkeypatch)
    with sessions() as db:
        original = read_hour(db, HOUR, current, defer_missing=True)
    with sessions() as db:
        optimized = read_hour(db, HOUR, current, defer_missing=True, store_loader=load_decision_store)
    assert optimized == original
    if case not in ('bad_legacy', 'missing', 'ds_only'):
        assert optimized['suggested_density']['value'] is not None


def test_hourly_optional_loader_is_forwarded_but_keeps_none_model_time(sessions, seeded, monkeypatch):
    install_case(sessions, 'variant')
    prepare_hour(sessions, monkeypatch)
    times = []
    def loader(db, model_at=None):
        times.append(model_at)
        return load_decision_store(db, model_at)
    with sessions() as db:
        expected = read_hour(db, HOUR, defer_missing=True)
    with sessions() as db:
        result = read_hour(db, HOUR, defer_missing=True, store_loader=loader)
    assert result == expected
    assert times == [HOUR, None]


def test_explicit_light_path_never_calls_load_store_and_defaults_still_do(sessions, seeded, monkeypatch):
    install_case(sessions, 'variant')
    current = prepare_hour(sessions, monkeypatch)
    with sessions() as db:
        original = shared_snapshot(db, when=HOUR, live=True)
        original_hour = read_hour(db, HOUR, current, defer_missing=True)
    def forbidden(*args, **kwargs):
        pytest.fail('Explicit lightweight path returned to whole-store loading')
    monkeypatch.setattr('app.services.decision_sources.load_store', forbidden)
    monkeypatch.setattr('app.decision_hourly.service.load_store', forbidden)
    with sessions() as db:
        assert shared_snapshot(db, when=HOUR, live=True, store_loader=load_decision_store) == original
        assert read_hour(db, HOUR, current, defer_missing=True, store_loader=load_decision_store) == original_hour
        with pytest.raises(pytest.fail.Exception):
            shared_snapshot(db, when=HOUR, live=True)
        with pytest.raises(pytest.fail.Exception):
            read_hour(db, HOUR, current, defer_missing=True)
