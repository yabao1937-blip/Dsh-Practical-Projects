"""本轮缺陷回归：只使用 conftest 隔离库和合成数据。"""
import copy
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.main import app
from app.schema_upgrade import upgrade_schema
from app.services import resolvers, training
from test_brief import fixture_store
from app.services.brief import build_hourly_brief

client = TestClient(app)


@pytest.mark.parametrize('raw,expected', [('7.8', 7.8), (' 0 ', 0), ('1e1', 10), ('', None), ('7x', None), (True, None), ('Infinity', None)])
def test_legacy_measurement_numbers(raw, expected):
    assert resolvers.measurement_number(raw) == expected


def test_future_measurements_do_not_rewrite_past():
    store = fixture_store()
    before = build_hourly_brief(store)['rows']
    store['heavyAshInput'] = {'manual': 20, 'manualAt': 1_900_000_000_000}
    store['heavySamples'] = [{'timestamp': '2026-08-03 10:00:00', 'ash_content': 10, 'rho': 1.5}]
    store['calcLogs'].append({'timestamp': '2026-08-03 10:00:00', 'calc_type': 'ash_meter',
                              'input_json': json.dumps({'belt': '502', 'value': '10.4'})})
    after = {r[0]: r for r in build_hourly_brief(store)['rows']}
    assert all(after[row[0]] == row for row in before)


def test_historical_channels_expire_independently():
    store = fixture_store()
    store['calcLogs'] = store['calcLogs'][:1] + [{
        'timestamp': '2026-08-02 12:00:00', 'calc_type': 'ash_meter',
        'input_json': json.dumps({'belt': '501', 'value': 9.5})}]
    row = build_hourly_brief(store)['rows'][-1]
    assert row[4] == row[7] == row[12] == ''
    assert row[6] == row[11] == '9.50'


def test_equal_count_concurrent_edit_is_rejected():
    first = client.get('/api/v1/state').json()
    old = copy.deepcopy(first)
    first['ashTarget'] = 8.61
    saved = client.put('/api/v1/state', json=first)
    assert saved.json()['ok'] is True
    old['ashTarget'] = 8.99
    stale = client.put('/api/v1/state', json=old)
    assert stale.status_code == 409 and stale.json()['conflict']
    assert client.get('/api/v1/state').json()['ashTarget'] == 8.61
    first['_revision'] = saved.json()['revision']
    first['ashTarget'] = 8.5
    assert client.put('/api/v1/state', json=first).json()['ok']
    assert client.put('/api/v1/state', json={'ashTarget': 9}).status_code == 409


def test_sample_retry_and_mirror_preservation():
    body = {'ts': '2026-09-21 08:00:00', 'rho': 1.47, 'ash_content': 8.2, 'client_id': 'regression-sample-1'}
    first = client.post('/api/v1/samples/heavy-ash', json=body)
    assert first.status_code == 200
    again = client.post('/api/v1/samples/heavy-ash', json=body)
    assert again.json()['id'] == first.json()['id']
    assert client.post('/api/v1/samples/heavy-ash', json={**body, 'ash_content': 9}).status_code == 409
    assert client.post('/api/v1/samples/heavy-ash', json={**body, 'rho': 0}).status_code == 422
    assert client.post('/api/v1/samples/heavy-ash', json={**body, 'ts': '2026-02-30 08:00:00'}).status_code == 422
    store = client.get('/api/v1/state').json()
    assert any(s['client_id'] == body['client_id'] for s in store['heavySamples'])
    store['heavySamples'] = []
    assert client.put('/api/v1/state', json=store).json()['ok']
    assert sum(s['client_id'] == body['client_id'] for s in client.get('/api/v1/samples/heavy-ash').json()) == 1


def test_two_belts_same_time_survive_roundtrip():
    store = client.get('/api/v1/state').json()
    store['calcLogs'].extend({'timestamp': '2026-09-21 08:01:00', 'calc_type': 'ash_density',
        'input_json': json.dumps({'system': 'A', 'belt': b, 'ash_content': a, 'density': 1.47})}
        for b, a in [('501', 9.1), ('502', 8.2)])
    assert client.put('/api/v1/state', json=store).json()['ok']
    rows = [json.loads(r['input_json']) for r in client.get('/api/v1/state').json()['calcLogs'] if r['timestamp'] == '2026-09-21 08:01:00']
    assert {r['belt'] for r in rows} == {'501', '502'}
    # 恢复测试前的覆盖型数据，避免干扰旧的固定种子测试。
    from app.services import migrate
    seed = json.loads((Path(__file__).parents[1] / 'data/seed_store.json').read_text(encoding='utf-8'))
    assert migrate.replace(seed, force=True)['ok']


def test_sqlite_upgrade_preserves_rows_and_allows_two_belts(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'legacy.db'))
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE coal_records (id INTEGER PRIMARY KEY, category TEXT, ts TEXT, system TEXT, belt TEXT, ash_content FLOAT, CONSTRAINT uq_coal_cat_ts_sys UNIQUE (category, ts, system))')
        conn.exec_driver_sql("INSERT INTO coal_records VALUES (1, 'ash_density', '2026-09-21 08:00:00', 'A', '502', 7.8)")
        conn.exec_driver_sql('CREATE TABLE heavy_samples (id INTEGER PRIMARY KEY, ts TEXT, rho FLOAT, ash_content FLOAT)')
        conn.exec_driver_sql("INSERT INTO heavy_samples VALUES (1, '2026-09-21 08:00:00', 1.47, 8.2)")
    upgrade_schema(engine)
    upgrade_schema(engine)
    with engine.begin() as conn:
        assert conn.exec_driver_sql('SELECT ash_content FROM coal_records WHERE id=1').scalar() == 7.8
        assert conn.exec_driver_sql('SELECT rho FROM heavy_samples WHERE id=1').scalar() == 1.47
        conn.exec_driver_sql("INSERT INTO coal_records VALUES (2, 'ash_density', '2026-09-21 08:00:00', 'A', '501', 9.1)")
    engine.dispose()


def test_pls_cv_matches_independent_one_feature_ols():
    # 一分量、单特征 PLS 与 OLS 相同；独立逐折计算验证填补/中心化无泄漏。
    x = [[None if i in (2, 9) else float(i)] for i in range(14)]
    y = [i + (i % 3) ** 2 for i in range(14)]
    sse = 0.0
    for i in range(len(x)):
        train = [r[0] for j, r in enumerate(x) if i != j and r[0] is not None]
        mean = sum(train) / len(train)
        tx = [r[0] if r[0] is not None else mean for j, r in enumerate(x) if j != i]
        ty = [v for j, v in enumerate(y) if j != i]
        ym = sum(ty) / len(ty)
        slope = sum((a - mean) * (b - ym) for a, b in zip(tx, ty)) / sum((a - mean) ** 2 for a in tx)
        pred = ym + slope * ((x[i][0] if x[i][0] is not None else mean) - mean)
        sse += (y[i] - pred) ** 2
    q2 = 1 - sse / sum((v - sum(y) / len(y)) ** 2 for v in y)
    assert training.train_pls(x, y, amax=1)['metrics']['q2'] == pytest.approx(q2, abs=1e-10)


def test_frontend_reliability():
    subprocess.run(['node', str(Path(__file__).parents[1] / 'scripts/verify_reliability.js')], check=True, capture_output=True)
