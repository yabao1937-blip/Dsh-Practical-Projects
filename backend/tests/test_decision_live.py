"""Live decision contract: isolated fixtures, no production DB or training writes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.models import AutoState
from app.routers.decision import router
from app.services.decision_live import calculate
from app.services.modeling import MLR_FEATURES

NOW = datetime(2026, 10, 8, 6, tzinfo=timezone.utc)


@pytest.fixture
def inputs():
    values = dict(raw_ash=40, coal_amount=600, level=50, sysA=1, sysB=0, sys401=1,
                  sys402=1, desliming473=1, desliming474=0, is_stoppage=0,
                  heavy_ash=8.5, float_ash=9, float_amount=40, coarse_amount=20,
                  belt501=260, belt502=240, density=1.48)
    cells = {k:dict(value=v, status='good', source='fixture', observed_at=NOW.isoformat(),
                   unit='%' if k in ('raw_ash','level','heavy_ash','float_ash') else
                   't/h' if k in ('coal_amount','float_amount','coarse_amount','belt501','belt502') else
                   'g/cm³' if k == 'density' else '') for k,v in values.items()}
    for k in ('belt501','belt502'):
        cells[k].update(batch_id='batch1', source_id='kep')
    cells['density']['item_id'] = 'TIA_4_1.TIA_4_1.A304/MD'
    model = dict(intercept=3, coefs=[.1,.002,0,0,0,0,0,0,0,.02], metrics={'version':2})
    store = dict(coarseModel=dict(mlr=model, production='mlr'), densityGuide={'maxStep':.02})
    return cells,store


def test_same_snapshot_and_purity(inputs):
    cells,store = inputs
    before = deepcopy(inputs)
    r = calculate(cells,store,now=NOW)
    assert r['coarse_ash']['value'] == pytest.approx(9.2)
    assert r['inputs']['coarse_amount']['value'] == 15
    assert r['heavy_amount']['value'] == 445
    assert r['total_ash']['value'] == pytest.approx((8.5*445+9*40+9.2*15)/500)
    assert r['suggested_density']['value'] == 1.48
    assert r['guidance']['direction'] == 'stable'
    assert inputs == before
    cells['heavy_ash']['value'] = 11
    after = calculate(cells,store,now=NOW)
    assert after['coarse_ash'] == r['coarse_ash']
    assert after['total_ash']['value'] > 10
    assert after['suggested_density']['value'] == 1.46
    assert after['guidance']['targetTotal'] == 9


@pytest.mark.parametrize('ash,direction,target',[(7,'up',8.5),(8.5,'stable',None),(11,'down',9)])
def test_range_and_existing_step_limit(inputs,ash,direction,target):
    cells,store=inputs
    cells['heavy_ash']['value']=ash
    r=calculate(cells,store,now=NOW)
    assert r['guidance']['direction']==direction
    assert abs(r['suggested_density']['value']-1.48) <= .020000001
    if target:
        assert r['guidance']['targetTotal']==target


@pytest.mark.parametrize('key,change',[
    ('raw_ash',{'value':None}),('sysA',{'value':True}),('level',{'unit':'m'}),
    ('belt501',{'value':None}),('belt502',{'batch_id':'other'}),
    ('belt502',{'observed_at':'2026-10-08T05:59:59+00:00'}),
    ('float_amount',{'value':700}),('heavy_ash',{'value':101}),
    ('density',{'value':1}),('density',{'item_id':'TIA_4_1.TIA_4_1.A303/MD'}),
    ('density',{'status':'missing'}),('coal_amount',{'status':'stopped'}),
    ('heavy_ash',{'status':'held'}),('is_stoppage',{'value':1}),
    ('density',{'observed_at':'2026-10-08T05:59:29Z'}),
    ('raw_ash',{'observed_at':None}),('raw_ash',{'observed_at':'2026-10-08T06:00:10Z'}),
])
def test_unavailable_inputs_never_emit_advice(inputs,key,change):
    cells,store=inputs
    cells[key].update(change)
    r=calculate(cells,store,now=NOW)
    assert r['suggested_density']['value'] is None
    assert r['suggested_density']['reason']
    if key=='raw_ash':
        assert r['coarse_ash']['value'] is None  # No training-mean imputation.


def test_ds_is_never_used_and_saved_gpt_is_supported(inputs):
    cells,store=inputs
    gpt=deepcopy(store['coarseModel'])
    store['coarseModel']['mlr']['metrics']['version']=1
    assert calculate(cells,store,now=NOW)['coarse_ash']['value'] is None
    store['coarseModelVariants']={'gpt':gpt}
    assert calculate(cells,store,now=NOW)['coarse_ash']['value'] == pytest.approx(9.2)
    gpt['mlr']['inputPolicy']='clip-training-range'
    gpt['mlr']['inputBounds']=[[0,30]]+[None]*9
    r=calculate(cells,store,now=NOW)
    assert r['coarse_ash']['value']==pytest.approx(8.2)
    assert r['features'][0]==dict(key='raw_ash',raw=40,used=30,clipped=True)
    gpt['mlr']['coefs']=[0]
    assert calculate(cells,store,now=NOW)['coarse_ash']['value'] is None


def test_held_values_keep_observation_labels_through_total(inputs):
    cells,store=inputs
    cells['coal_amount']['status']='stopped'
    r=calculate(cells,store,now=NOW)
    assert r['coarse_ash']['status']=='held'
    assert r['total_ash']['status']=='held'
    assert r['total_ash']['value'] is not None
    assert r['suggested_density']['value'] is None


def test_guard_uses_actual_driver_and_only_selected_manuals(inputs):
    cells,store=inputs
    cells['heavy_ash']['value']=11
    first=calculate(cells,store,now=NOW)
    store['densityActionLatch']={'key':first['guidance']['driveKey']}
    cells['density']['observed_at']=(NOW+timedelta(seconds=5)).isoformat()
    held=calculate(cells,store,now=NOW+timedelta(seconds=10))
    assert held['guidance']['hold']
    assert held['suggested_density']['value'] is None
    store.pop('densityActionLatch')
    store['densityLastMoveAt']=(NOW-timedelta(minutes=5)).timestamp()*1000
    store['floatAshInput']={'manual':9,'manualAt':(NOW-timedelta(minutes=1)).timestamp()*1000}
    assert calculate(cells,store,now=NOW)['guidance']['hold']  # Unselected old source cannot unlock.
    cells['float_ash'].update(status='manual',entered_at=(NOW-timedelta(minutes=1)).isoformat())
    assert not calculate(cells,store,now=NOW)['guidance']['hold']


def test_api_reads_saved_gpt_without_writes(tmp_path,inputs):
    cells,store=inputs
    for cell in cells.values():
        cell['observed_at']=datetime.now(timezone.utc).isoformat()
    cells['belt502']['observed_at']=cells['belt501']['observed_at']
    engine=create_engine(f'sqlite:///{(tmp_path / "decision.db").as_posix()}',connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine)
    sessions=sessionmaker(bind=engine)
    with sessions.begin() as db:
        db.add(AutoState(key='coarseModelVariants',value={'gpt':store['coarseModel']}))
    def database():
        with sessions() as db:
            yield db
    app=FastAPI()
    app.include_router(router,prefix='/api/v1')
    app.dependency_overrides[get_db]=database
    with engine.connect() as c:
        before=list(c.connection.driver_connection.iterdump())
    with TestClient(app) as client:
        body={'cells':cells}
        response=client.post('/api/v1/decision/snapshots',json=body)
        assert response.status_code==200,response.text
        assert response.json()['coarse_ash']['value']==pytest.approx(9.2)
        assert client.post('/api/v1/decision/snapshots',json={**body,'target_min':10,'target_max':9}).status_code==422
        assert client.post('/api/v1/decision/snapshots',json={'cells':{'unknown':{}}}).status_code==422
        cells['raw_ash']['value']=True
        assert client.post('/api/v1/decision/snapshots',json=body).status_code==422
    with engine.connect() as c:
        assert list(c.connection.driver_connection.iterdump())==before
    engine.dispose()


def test_js_python_model_total_and_original_advice_parity(inputs):
    cells,store=inputs
    cases=[]
    for ash in (7,8.5,11):
        cells=deepcopy(cells)
        cells['heavy_ash']['value']=ash
        cases.append(dict(cells=cells,model=store['coarseModel']['mlr']))
    script=Path(__file__).resolve().parents[1]/'scripts'/'verify_decision_realtime.js'
    js=json.loads(subprocess.run(['node',str(script),'--parity-json'],input=json.dumps(cases),text=True,capture_output=True,check=True).stdout)
    for case,expected in zip(cases,js):
        r=calculate(case['cells'],store,now=NOW)
        for key in ('coarse_ash','heavy_amount','total_ash','suggested_density'):
            assert r[key]['value']==pytest.approx(expected[key],abs=1e-6)
