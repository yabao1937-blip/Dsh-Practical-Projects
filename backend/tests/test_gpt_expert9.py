"""Expert constraints, honest temporal selection, source parsing and DS isolation."""
from copy import deepcopy
from datetime import timedelta
import itertools
import json
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import select

from app.gpt_coarse.predict import FEATURES, explain, predict, valid
from app.gpt_coarse.repository import activate, active, identity
from app.gpt_coarse.models import GptCoarseRevision
from app.gpt_coarse.training import train
from app.services.decision_live import calculate
from app.services.decision_sources import shared_snapshot
from app.services.state import load_store
from app.decision_hourly.service import context_definition
from test_decision_live import inputs, NOW
from test_decision_sources import sessions, seeded, HOUR

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def artifact():
    return json.loads((ROOT/'backend/models/gpt_expert9/20261009.json').read_text(encoding='utf-8'))


def test_anchors_monotonicity_and_correction_bound_across_switch_combinations(artifact):
    model = artifact['model']
    ref = dict(zip(FEATURES[:8], model['reference']))
    assert len(FEATURES) == 9 and 'is_stoppage' not in FEATURES
    assert [predict(dict(ref, level=l), model) for l in (25,50,75)] == [23,15.5,8]
    for states in itertools.product((0,1), repeat=6):
        record = dict(ref, **dict(zip(FEATURES[2:8], states)), raw_ash=60, coal_amount=1500)
        values = []
        for level in range(101):
            detail = explain(dict(record, level=level), model)
            assert abs(detail['correction']) <= 2
            assert 0 < detail['value'] < 100
            values.append(detail['value'])
        assert all(b <= a+1e-12 for a,b in zip(values,values[1:]))
        assert values[0] == values[25] and values[75] == values[100]


@pytest.mark.parametrize('key,value',[('raw_ash',None),('raw_ash',0),('coal_amount',0),('coal_amount',10),
    ('sys401',.5),('sysA',True),('level',float('nan')),('level',-1),('level',101)])
def test_missing_or_invalid_factors_never_imputed(artifact,key,value):
    row=dict(artifact['rows'][0]);row[key]=value
    assert predict(row, artifact['model']) is None


def test_corrupt_model_rejected(artifact):
    model=artifact['model']
    for field,bad in [('coefs',[0]),('feature_names',FEATURES+['is_stoppage']),('scale',[0]*8),
                      ('correction_limit',200),('expert',{'slope':.3}),('bounds',[[0,float('nan')]]*8)]:
        assert not valid(dict(model,**{field:bad}))


def test_live_chain_uses_expert9_and_retains_stop_guard(artifact,inputs):
    cells,store=inputs
    store['gptExpertModel']=dict(production='expert9',expert9=artifact['model'],trainedAt='2026-10-07T00:00:00Z')
    result=calculate(cells,store,now=NOW)
    expected=predict({k:v['value'] for k,v in cells.items()},artifact['model'])
    assert result['model']['method']=='expert9'
    assert result['coarse_ash']['value']==expected
    assert len(result['features'])==9
    assert result['total_ash']['value']==pytest.approx((8.5*445+9*40+expected*15)/500)
    assert result['suggested_density']['value'] is not None
    changed=deepcopy(cells);changed['level']['value']=80
    assert calculate(changed,store,now=NOW)['suggested_density']['value'] is not None
    for key,value in [('is_stoppage',1),('is_stoppage',None)]:
        changed=deepcopy(cells);changed[key]['value']=value
        after=calculate(changed,store,now=NOW)
        assert after['suggested_density']['value'] is None
        assert after['coarse_ash']['value'] is not None


def test_activation_time_new_table_isolation_and_full_hour_context(artifact,sessions,seeded):
    artifact['trained_at']='2026-10-01T00:00:00Z'
    with sessions.begin() as db:
        before=load_store(db)
        row=activate(db,artifact,HOUR+timedelta(seconds=1))
        key=row.artifact_id
        assert active(db,HOUR) is None  # Training earlier does NOT make model available before activation.
        assert shared_snapshot(db,HOUR)['model']['method']=='mlr'
        new=shared_snapshot(db,HOUR+timedelta(seconds=2))
        from app.hourly_brief.service import build_snapshot
        brief=build_snapshot(db,HOUR+timedelta(seconds=2))
        assert len(brief['cells'])==17
        assert list(brief['model_inputs'])==FEATURES
        assert brief['protection_inputs']=={'is_stoppage':0}
        assert all(cell==new['cells'][name] for name,cell in brief['cells'].items())
        assert new['model']['method']=='expert9'
        assert new['model']['artifact_id']==key
        after=load_store(db,model_at=HOUR+timedelta(seconds=2))
        assert after['coarseModelVariants']==before['coarseModelVariants']
        assert after['coarseCoal']==before['coarseCoal']
        assert context_definition(new,after)['model_definition']==artifact['model']
        assert activate(db,artifact,HOUR+timedelta(seconds=3)).id==row.id
        assert len(list(db.scalars(select(GptCoarseRevision))))==1
        # Old browser writes cannot own the dedicated GPT table, even if they invent a matching key.
        from app.services.migrate import _plan
        plan=_plan({'coarseModel':None,'gptExpertModel':{'production':'invalid'}})
        assert 'gpt_coarse_model_revisions' not in plan
        assert all(x['key']!='gptExpertModel' for x in plan['auto_state'])


def test_time_validation_and_forward_labels_do_not_select_parameters(artifact):
    rows=artifact['rows']
    first,report=train(rows)
    cut=report['forward_time_check']['start']
    changed=[dict(r,ash_content=60) if r['day']>=cut else r for r in rows]
    _,other=train(changed)
    assert report['tuning_validation']==other['tuning_validation']
    assert first['alpha']==artifact['model']['alpha']
    assert first['coefs']==pytest.approx(artifact['model']['coefs'],abs=1e-10)
    for trial in report['tuning_validation']['trials']:
        for fold in trial['folds']:
            assert fold['train_end']<fold['validation_start']<=fold['validation_end']<cut
    assert first['coefs'][2]==0  # A-system effect cannot be identified from one off observation.


def test_frontend_predictions_match_on_workbook_and_boundary_inputs(artifact):
    records=artifact['rows']+[dict(artifact['rows'][0],level=v) for v in (0,25,50,75,100)]
    records += [dict(records[0],raw_ash=None),dict(records[0],sysA=True)]
    script="""const fs=require('fs'),vm=require('vm');
    const input=JSON.parse(fs.readFileSync(0,'utf8'));
    vm.runInThisContext(fs.readFileSync('frontend/js/gpt-expert9.js','utf8'));
    process.stdout.write(JSON.stringify(input.rows.map(r=>GptExpert9.predict(r,input.model))));"""
    result=subprocess.run(['node','-e',script],input=json.dumps(dict(rows=records,model=artifact['model'])),
                          text=True,encoding='utf-8',cwd=ROOT,capture_output=True,check=True)
    actual=json.loads(result.stdout)
    for row,value in zip(records,actual):
        expected=predict(row,artifact['model'])
        assert value is None if expected is None else value==pytest.approx(expected,abs=1e-10)


def test_workbook_parser_preserves_source_and_audits_dates(tmp_path,artifact):
    from openpyxl import Workbook
    from datetime import time
    from app.gpt_coarse.workbook import read_workbook
    import hashlib
    book=Workbook();sheet=book.active;sheet.title='Sheet1'
    sheet.append(['title']);sheet.append(['日期','时间','原煤灰分','小时煤量',None,None,None,None,None,None,'315灰分','液位'])
    sheet.append([None,None,None,None,'A','B',401,402,473,474])
    for day,amount in [(6.29,800),(6.3,850),(7.9,900),(7.1,950),(7.11,1)]:
        sheet.append([day,time(9),40,amount,1,1,1,1,'开','关' if amount==1 else '停',15,50])
    path=tmp_path/'source.xlsx';book.save(path)
    before=path.read_bytes();rows,audit=read_workbook(path,2026)
    assert len(rows)==4 and len(audit['stopped_rows'])==1
    assert rows[1]['day']=='2026-06-30' and rows[3]['day']=='2026-07-10'
    assert len(audit['date_repairs'])==2
    assert path.read_bytes()==before and audit['sha256']==hashlib.sha256(before).hexdigest()
