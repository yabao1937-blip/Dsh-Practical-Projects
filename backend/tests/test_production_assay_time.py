"""Actual production timestamps, backward compatibility and late carry propagation."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.decision_hourly.ash_settlement import get_settlement, manual_basis, latest_end, settle_due_hours
from app.decision_hourly.models import DecisionCycleSample
from app.hourly_brief.inputs import Inputs
from app.models import ManualMeasurement
from app.services.manual_measurements import latest_measurement, measurement_time, request_hash
from app.telemetry.domain import iso_time
from test_ash_settlement import START, END, PUBLISH, sample
from test_hourly_brief import sessions
from test_manual_measurements import setup, URL


def put(db, when, value, entered, policy='production-time-v1'):
    row = ManualMeasurement(metric='heavy_clean_coal_ash', name='重介精煤灰分', value=value, unit='%',
        observed_at=iso_time(when), created_at=iso_time(entered), time_basis=policy, request_hash=uuid4().hex)
    db.add(row)
    db.flush()
    return row


def test_user_timeline_revises_own_and_carried_hours_not_earlier_or_decisions(sessions):
    ten_twenty = PUBLISH+timedelta(minutes=20)
    eleven, twelve = PUBLISH+timedelta(hours=1), PUBLISH+timedelta(hours=2)
    with sessions.begin() as db:
        originals = [sample(db, START+timedelta(hours=i)) for i in range(3)]
        lab = put(db, START+timedelta(minutes=47), 8, ten_twenty)
        assert latest_end(ten_twenty) == END
        first = get_settlement(db, START, ten_twenty)
        assert first['heavy_ash']['records'][0]['production_at'] == lab.observed_at
        assert first['item']['value'] == pytest.approx(8.8) and not first['item']['provisional']
        assert latest_end(eleven) == END+timedelta(hours=1)
        second = get_settlement(db, END, eleven)
        assert second['heavy_ash']['value'] == 8 and second['item']['provisional']
        settle_due_hours(db, twelve)
        third_before = get_settlement(db, END+timedelta(hours=1), twelve)
        # The newly entered 09:20 assay changes 09–10 plus 10–11's default,
        # without changing 08–09 or any original online evidence.
        later = put(db, END+timedelta(minutes=20), 10, twelve)
        for start in (END, END+timedelta(hours=1)):
            revised = get_settlement(db, start, twelve)
            assert revised['heavy_ash']['records'][0]['id'] == later.id
            assert revised['item']['value'] == pytest.approx(10.5)
        assert get_settlement(db, END, twelve)['item']['provisional'] is False
        assert get_settlement(db, END+timedelta(hours=1), twelve)['item']['provisional'] is True
        assert get_settlement(db, END+timedelta(hours=1), twelve)['revision'] != third_before['revision']
        assert get_settlement(db, START, twelve)['revision'] == first['revision']
        own = put(db, END+timedelta(hours=1, minutes=15), 9, twelve)
        assert get_settlement(db, END+timedelta(hours=1), twelve)['heavy_ash']['records'][0]['id'] == own.id
        for original in originals:
            assert db.get(DecisionCycleSample, original.slot).snapshot == original.snapshot


def test_latest_within_hour_uses_sample_time_then_id_not_entry_time_or_mean(sessions):
    with sessions.begin() as db:
        newest = put(db, START+timedelta(minutes=47), 8, PUBLISH)
        put(db, START+timedelta(minutes=20), 99, PUBLISH+timedelta(minutes=1))
        assert manual_basis(db, START, END, PUBLISH+timedelta(minutes=2))['value'] == 8
        tied = put(db, START+timedelta(minutes=47), 9, PUBLISH+timedelta(minutes=2))
        put(db, END, 98, PUBLISH)  # The following hour cannot leak backward.
        put(db, START+timedelta(minutes=59), 97, PUBLISH+timedelta(minutes=4))
        result = manual_basis(db, START, END, PUBLISH+timedelta(minutes=3))
        assert result['value'] == 9 and result['records'][0]['id'] == tied.id
        assert newest.observed_at == iso_time(START+timedelta(minutes=47))


def test_mixed_policies_select_effective_latest_in_realtime_and_history(sessions):
    with sessions.begin() as db:
        old = put(db, PUBLISH+timedelta(minutes=47), 7, PUBLISH+timedelta(minutes=48), 'legacy-lag-2h')
        new = put(db, END+timedelta(minutes=20), 8, PUBLISH+timedelta(hours=1))
        now = PUBLISH+timedelta(hours=2)
        assert measurement_time(old) == iso_time(START+timedelta(minutes=47))
        assert latest_measurement(db, 'heavy_clean_coal_ash', iso_time(now)).id == new.id
        c = Inputs(db, now).manual('heavy_clean_coal_ash')
        assert c['value'] == 8 and c['time_basis'] == 'production-time-v1'
        assert manual_basis(db, START, END, now)['value'] == 7
        assert manual_basis(db, END, END+timedelta(hours=1), now)['value'] == 8


def test_old_instrument_manual_does_not_override_newer_production_sample(sessions):
    from app.models import AutoState
    with sessions.begin() as db:
        # 10:00 old report means production 08:00; 09:20 new input is newer.
        now = PUBLISH+timedelta(hours=2)
        put(db, END+timedelta(minutes=20), 8, now)
        db.add(AutoState(key='heavyAshInput',value=dict(manual=99, manualAt=iso_time(PUBLISH),
                                                     sampleAt=iso_time(PUBLISH))))
        db.flush()
        c = Inputs(db,now).manual('heavy_clean_coal_ash',auto_paths=(('heavyAshInput',),))
        assert c['value'] == 8 and c['observed_at'] == iso_time(END+timedelta(minutes=20))


def test_new_api_uses_production_time_and_legacy_retry_keeps_policy(setup):
    client, sessions = setup
    stamp = iso_time(PUBLISH)
    body = dict(metric='heavy_clean_coal_ash', value=8, observed_at=stamp, client_id='old-id')
    with sessions.begin() as db:
        old = ManualMeasurement(metric=body['metric'], name='重介精煤灰分', value=8, unit='%',
            observed_at=stamp, created_at=stamp, client_id='old-id', request_hash=request_hash(body['metric'],8,stamp))
        db.add(old)
    retry = client.post(URL, json=body)
    assert retry.status_code == 200 and retry.json()['time_basis'] == 'legacy-lag-2h'
    body.update(client_id='new-id', observed_at=iso_time(START+timedelta(minutes=47)))
    created = client.post(URL, json=body)
    assert created.status_code == 201 and created.json()['time_basis'] == 'production-time-v1'
    with sessions() as db:
        assert measurement_time(db.get(ManualMeasurement,created.json()['id'])) == body['observed_at']


def test_startup_upgrade_is_additive_and_idempotent(sessions):
    from app.schema_upgrade import upgrade_schema
    engine = sessions.kw['bind']
    with engine.begin() as conn:
        conn.exec_driver_sql('DROP INDEX ix_manual_measurement_effective_latest')
        conn.exec_driver_sql('ALTER TABLE manual_measurements DROP COLUMN time_basis')
        conn.exec_driver_sql("INSERT INTO manual_measurements(metric,name,value,unit,observed_at,created_at,request_hash) VALUES ('heavy_clean_coal_ash','old',8,'%',?,?, 'hash')", (iso_time(PUBLISH),iso_time(PUBLISH)))
        before = conn.exec_driver_sql('SELECT id,observed_at,created_at,request_hash FROM manual_measurements').all()
    upgrade_schema(engine)
    upgrade_schema(engine)
    with sessions() as db:
        assert db.execute(text('SELECT id,observed_at,created_at,request_hash FROM manual_measurements')).all() == before
        old = db.get(ManualMeasurement, 1)
        assert old.time_basis == 'legacy-lag-2h' and measurement_time(old) == iso_time(START)
