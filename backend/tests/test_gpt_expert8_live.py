"""Eight-factor activation and raw-ash-independent decision chain, isolated DB."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import select

from app.gpt_coarse.expert8 import FEATURES, predict
from app.gpt_coarse.promotion import prepare_expert8
from app.gpt_coarse.repository import activate, active
from app.gpt_coarse.models import GptCoarseRevision
from app.services.decision_live import calculate
from app.services.decision_sources import shared_snapshot
from app.services.state import load_store
from app.hourly_brief.service import build_snapshot
from app.decision_hourly.service import context_definition, record, build_hour
from app.decision_hourly.models import DecisionCycleSample
from app.decision_hourly.statistics import hour_advice
from app.telemetry.domain import iso_time
from test_decision_live import inputs, NOW
from test_decision_sources import sessions, seeded, HOUR, send

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def artifact8():
    artifact = json.loads((ROOT/'backend/models/gpt_expert8/20261012/production.json').read_text(encoding='utf-8'))
    artifact['trained_at'] = '2026-10-01T00:00:00Z'
    return artifact


def use8(store, artifact):
    store['gptExpertModel'] = dict(production='expert8', expert8=artifact['model'],
                                  trainedAt=artifact['trained_at'], revision=2, artifact_id='fixture8')


@pytest.mark.parametrize('change', [None, dict(value=None,status='missing'), dict(value=40,status='held'),
    dict(value=40,status='stopped'), dict(value=0), dict(value=-1), dict(value=101), dict(unit='invalid'),
    dict(observed_at='2020-01-01T00:00:00Z')])
def test_raw_ash_is_optional_for_prediction_total_and_advice(inputs, artifact8, change):
    cells, store = inputs
    use8(store, artifact8)
    before = calculate(cells, store, now=NOW)
    if change is None:
        cells.pop('raw_ash')
    else:
        cells['raw_ash'].update(change)
    after = calculate(cells, store, now=NOW)
    for key in ('coarse_ash', 'total_ash', 'suggested_density', 'guidance'):
        assert after[key] == before[key]
    assert after['suggested_density']['value'] is not None
    assert after['coarse_ash']['status'] == 'predicted'
    assert [f['key'] for f in after['features']] == FEATURES


@pytest.mark.parametrize('key,change', [
    ('coal_amount',dict(value=None,status='missing')), ('coal_amount',dict(status='held')),
    ('coal_amount',dict(value=10)), ('sysA',dict(value=None)), ('sys401',dict(value=None)),
    ('desliming473',dict(value=None)), ('level',dict(value=None)), ('level',dict(value=80)),
    ('is_stoppage',dict(value=1)), ('is_stoppage',dict(value=None)),
    ('density',dict(value=None)), ('density',dict(value=1)),
    ('heavy_ash',dict(value=None)), ('belt501',dict(status='held'))])
def test_other_required_data_and_protections_still_block(inputs, artifact8, key, change):
    cells, store = inputs
    use8(store, artifact8)
    cells['raw_ash'].update(value=None,status='missing')
    cells[key].update(change)
    result = calculate(cells, store, now=NOW)
    assert result['suggested_density']['value'] is None
    assert result['suggested_density']['reason']


def test_raw_refresh_cannot_release_existing_action_latch(inputs, artifact8):
    cells, store = inputs
    use8(store, artifact8)
    cells['heavy_ash']['value'] = 11
    first = calculate(cells, store, now=NOW)
    store['densityActionLatch'] = dict(key=first['guidance']['driveKey'])
    cells['raw_ash'].update(value=50,observed_at=(NOW+timedelta(seconds=5)).isoformat())
    after = calculate(cells, store, now=NOW+timedelta(seconds=5))
    assert after['suggested_density']['value'] is None
    assert after['guidance']['hold']
    assert after['guidance']['driveKey'] == first['guidance']['driveKey']


def test_production_artifact_matches_candidate_and_rejects_unreviewed_input():
    path = ROOT/'backend/models/gpt_expert8/20261012/model.json'
    candidate = json.loads(path.read_text(encoding='utf-8'))
    report = json.loads((ROOT/'docs/verification/gpt-expert8-20261012/comparison.json').read_text(encoding='utf-8'))
    production = prepare_expert8(candidate, report, hashlib.sha256(path.read_bytes()).hexdigest())
    assert production == json.loads(path.with_name('production.json').read_text(encoding='utf-8'))
    assert production['model'] == candidate['model']
    assert production['report']['historical_fit'] == report['historical_fit']['new']
    with pytest.raises(ValueError):
        prepare_expert8(candidate, report, 'different-artifact')


def test_versioned_activation_shared_brief_and_raw_only_fingerprint(sessions, seeded, artifact8, monkeypatch):
    cfg, repo, values = seeded
    old = json.loads((ROOT/'backend/models/gpt_expert9/20261009.json').read_text(encoding='utf-8'))
    old['trained_at'] = artifact8['trained_at']
    with sessions.begin() as db:
        activate(db, old, HOUR-timedelta(minutes=2))
        before = load_store(db)
        row = activate(db, artifact8, HOUR-timedelta(seconds=1))
        assert active(db, HOUR-timedelta(seconds=2))['production'] == 'expert9'
        assert active(db, HOUR)['production'] == 'expert8'
        assert len(list(db.scalars(select(GptCoarseRevision)))) == 2
        assert activate(db, artifact8, HOUR).id == row.id
        assert load_store(db)['coarseModelVariants'] == before['coarseModelVariants']
    values[0] = 0
    send(repo,cfg,HOUR-timedelta(seconds=1),values)
    with sessions.begin() as db:
        live = shared_snapshot(db, HOUR)
        assert live['inputs']['raw_ash']['status'] == 'held'
        assert live['coarse_ash']['status'] == 'predicted'
        assert live['suggested_density']['value'] is not None
        brief = build_snapshot(db, HOUR)
        assert list(brief['model_inputs']) == FEATURES
        assert brief['cells']['suggested_density'] == live['cells']['suggested_density']
        store = load_store(db)
        context = context_definition(live,store)
        assert context['model_definition'] == artifact8['model']
        changed = deepcopy(live)
        changed['sources']['resolved_choices']['raw-ash'] = 'different-raw-source'
        changed['cells']['raw_ash']['sample']['definition_id'] = 'different-raw-definition'
        assert context_definition(changed,store) == context
        # Only raw data changes; a second archived slot must not invent new advice coverage.
        monkeypatch.setattr('app.decision_hourly.service.observe',lambda *args:None)
        monkeypatch.setattr('app.decision_hourly.service.pack_snapshot',lambda db,snapshot,slot:snapshot)
        record(db,HOUR,result=live,store=store)
        changed['generated_at'] = iso_time(HOUR+timedelta(seconds=5))
        changed['inputs']['raw_ash'].update(value=50,observed_at=changed['generated_at'],batch_id='new-raw-only')
        record(db,HOUR+timedelta(seconds=5),result=changed,store=store)
        rows = list(db.scalars(select(DecisionCycleSample).order_by(DecisionCycleSample.slot)))
        assert len(rows) == 2 and rows[0].fingerprint == rows[1].fingerprint
        assert hour_advice(rows,HOUR,HOUR+timedelta(hours=1),{})[1]['valid_count'] == 1


def test_full_hour_with_raw_missing_has_advice_and_history_is_not_recomputed(inputs, artifact8):
    from types import SimpleNamespace
    cells, store = inputs
    use8(store, artifact8)
    cells['raw_ash'].update(value=None,status='missing')
    result = calculate(cells, store, now=NOW)
    rows = [SimpleNamespace(slot=int(NOW.timestamp())//5+i,context_key='eight',fingerprint=str(i),
            snapshot=dict(suggested_density=result['suggested_density'],cells=dict(density=cells['density'],raw_ash=cells['raw_ash'])))
            for i in range(720)]
    basis = dict(cells['density'],observed_at=iso_time(NOW+timedelta(hours=1,seconds=-5)))
    advice,coverage,_ = hour_advice(rows,NOW,NOW+timedelta(hours=1),basis)
    assert advice['value'] is not None and coverage['ratio'] == 1
    rows[0].context_key = 'old-nine-factor'
    assert hour_advice(rows,NOW,NOW+timedelta(hours=1),basis)[0]['value'] is None


def test_frontend_eight_factor_predictions_match_all_rows_and_missing_raw(artifact8):
    rows = artifact8['rows']
    rows += [dict(rows[0],raw_ash=value) for value in (None,0,50,101)]
    rows += [dict(rows[0],level=value) for value in (0,25,50,75,100)]
    rows += [dict(rows[0],coal_amount=None),dict(rows[0],sysA=True)]
    script = """const fs=require('fs'),vm=require('vm');
    const input=JSON.parse(fs.readFileSync(0,'utf8'));
    vm.runInThisContext(fs.readFileSync('frontend/js/gpt-expert9.js','utf8'));
    process.stdout.write(JSON.stringify(input.rows.map(r=>GptExpert9.predict(r,input.model))));"""
    actual = json.loads(subprocess.run(['node','-e',script],cwd=ROOT,check=True,capture_output=True,
        input=json.dumps(dict(rows=rows,model=artifact8['model'])),text=True,encoding='utf-8').stdout)
    for row,value in zip(rows,actual):
        expected = predict(row,artifact8['model'])
        assert value is None if expected is None else value == pytest.approx(expected,abs=1e-10)
