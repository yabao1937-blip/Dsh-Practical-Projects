from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3

import pytest

from scripts.audit_gpt_database_samples import (
    FEATURES, ITEMS, classify_evidence, feature_target, inventory, label_flags, lookback_hours, pairing_status, stamp,
)


CONFIG = dict(node_id='test', unit='%', scale=1, offset=0, value_type='float', minimum=0, maximum=100)
TARGET = datetime(2026, 10, 20, 2, 0, tzinfo=timezone.utc)


def evidence(feature, age, **changes):
    received = stamp(TARGET - timedelta(seconds=age))
    row = dict(id=1, batch_id='batch-1', received_at=received)
    sample = dict(received_at=received, quality='good', value=False if feature in ITEMS else 50,
                  definition_id=hashlib.sha256(json.dumps(CONFIG, sort_keys=True).encode()).hexdigest())
    sample.update(changes)
    return classify_evidence(feature, stamp(TARGET), row, sample, CONFIG)


@pytest.mark.parametrize('feature,age,status', [
    ('level', 30, 'good'), ('level', 30.001, 'gap'),
    ('coal_amount', 30, 'good'), ('coal_amount', 30.001, 'minute_approximation'),
    ('coal_amount', 65, 'minute_approximation'), ('coal_amount', 65.001, 'gap'),
    ('level', -1, 'future_reading'),
])
def test_alignment_boundaries(feature, age, status):
    assert evidence(feature, age)['status'] == status


def test_lag_quality_and_batch_contract():
    assert feature_target('level', TARGET) == TARGET - timedelta(seconds=90)
    assert feature_target('coal_amount', TARGET) == TARGET
    assert evidence('level', 1, quality='bad')['status'] == 'bad_quality'
    assert evidence('level', 1, definition_id='changed')['status'] == 'definition_mismatch'
    assert evidence('sysA', 1, value=0)['status'] == 'invalid_switch'
    assert evidence('coal_amount', 1, value=0)['status'] == 'stopped'
    ev = {f: dict(evidence(f, 5), source_id='kep') for f in FEATURES}
    ev['level']['batch_id'] = 'earlier-level-batch'
    assert pairing_status(ev) == ('strict', True)
    ev['sysA']['batch_id'] = 'other-batch'
    assert pairing_status(ev) == ('mixed_batches', False)
    ev['sysA']['batch_id'] = None
    assert pairing_status(ev) == ('mixed_batches', False)


def test_cross_hour_catalog_bounds_and_configured_source_age_only():
    assert lookback_hours('2026-10-20T02:01:00.000+00:00') == [
        '2026-10-20T01:00:00.000+00:00', '2026-10-20T02:00:00.000+00:00']
    record = dict(id=1, batch_id='b', received_at=stamp(TARGET))
    sample = dict(received_at=stamp(TARGET), source_at=stamp(TARGET-timedelta(seconds=31)),
                  value=50, quality='good', definition_id=hashlib.sha256(json.dumps(CONFIG, sort_keys=True).encode()).hexdigest())
    assert classify_evidence('level', stamp(TARGET), record, sample, CONFIG)['status'] == 'good'
    assert classify_evidence('level', stamp(TARGET), record, sample,
                             dict(CONFIG, max_source_age_seconds=30))['status'] == 'stale_source_time'


def test_labels_are_flagged_not_corrected_or_deleted():
    assays = [dict(id=1, observed_at=stamp(TARGET), created_at=stamp(TARGET + timedelta(hours=1)), value=12),
              dict(id=2, observed_at=stamp(TARGET + timedelta(seconds=10)), created_at=stamp(TARGET), value=13)]
    before = json.dumps(assays)
    flags, pairs = label_flags(assays)
    assert flags[1] == ['nearby_conflicting_labels']
    assert flags[2] == ['nearby_conflicting_labels', 'observed_after_created']
    assert pairs[0]['ids'] == [1, 2]
    assert json.dumps(assays) == before


def test_inventory_uses_latest_prior_bad_row_and_never_future_or_two_hour_shift():
    connection = sqlite3.connect(':memory:')
    connection.executescript('''
        CREATE TABLE manual_measurements (id,metric,value,observed_at,created_at,time_basis);
        CREATE TABLE telemetry_points (source_id,key,config);
        CREATE TABLE decision_source_revisions (id,effective_at,config);
        CREATE TABLE telemetry_readings (id INTEGER PRIMARY KEY,source_id,point_key,batch_id,received_at,sample);
        CREATE INDEX point_time ON telemetry_readings(source_id,point_key,received_at,id);
        CREATE TABLE gpt_coarse_model_revisions (id,artifact_id,activated_at,artifact);
    ''')
    connection.execute('INSERT INTO manual_measurements VALUES(1,?,?,?,?,?)',
        ('coarse_slime_ash', 12, stamp(TARGET), stamp(TARGET + timedelta(hours=1)), 'legacy-lag-2h'))
    choices = {k: json.dumps(['kep', v]) for k, v in
        [('coal-rate', 'coal_amount'), ('tail-level', 'level'),
         ('system-switches', 'four_system_status'), ('desliming', 'desliming_status')]}
    connection.execute('INSERT INTO decision_source_revisions VALUES(1,?,?)',
        (stamp(TARGET - timedelta(days=1)), json.dumps({'choices': choices})))
    for f in FEATURES:
        config = dict(CONFIG, item_id=ITEMS.get(f))
        connection.execute('INSERT INTO telemetry_points VALUES(?,?,?)', ('kep', f, json.dumps(config)))
        target = feature_target(f, TARGET)
        for delta, quality, value in [(-10, 'good', False if f in ITEMS else 50),
                                      (0, 'bad' if f == 'level' else 'good', False if f in ITEMS else 50),
                                      (1, 'good', True if f in ITEMS else 90)]:
            received = stamp(target + timedelta(seconds=delta))
            sample = dict(received_at=received, quality=quality, value=value,
                          definition_id=hashlib.sha256(json.dumps(CONFIG, sort_keys=True).encode()).hexdigest())
            connection.execute('INSERT INTO telemetry_readings(source_id,point_key,batch_id,received_at,sample) VALUES(?,?,?,?,?)',
                               ('kep', f, received, received, json.dumps(sample)))
    class Catalog:
        loads = staticmethod(json.loads)
    result = inventory(connection, Catalog(), max_id=1)
    row = result['rows'][0]
    assert row['pairing'] == 'incomplete'
    assert row['evidence']['level']['status'] == 'bad_quality'
    assert row['evidence']['level']['target_at'] == stamp(TARGET - timedelta(seconds=90))
    assert row['evidence']['coal_amount']['target_at'] == stamp(TARGET)
    assert row['features']['coal_amount'] == 50
    assert connection.execute('SELECT COUNT(*) FROM manual_measurements').fetchone()[0] == 1
    connection.close()
