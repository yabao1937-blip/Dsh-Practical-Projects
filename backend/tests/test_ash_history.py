"""Frozen ash history: sample-weighted ranges, missing hours and read-only API."""
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select

from app.decision_hourly.models import DecisionCycleHour
from app.decision_hourly.ash_history import read_ash_hours
from app.telemetry.domain import iso_time
from test_hourly_brief import sessions
from test_decision_sources import client

NOW = datetime(2026, 10, 9, 2, 24, tzinfo=timezone.utc)
END = NOW.replace(minute=0)
START = END-timedelta(hours=3)


def save(db, start, value, count, status='average'):
    db.add(DecisionCycleHour(start=iso_time(start), end=iso_time(start+timedelta(hours=1)),
        generated_at=iso_time(start+timedelta(hours=1)), snapshot=dict(
            total_ash=dict(value=value, sample_count=count, status=status, reason='test'),
            # Advice count differs: ash coverage must use total_ash.sample_count.
            coverage=dict(valid_count=700), model=dict(method='old-model'))))


def test_range_weights_ash_samples_and_preserves_missing_hours(sessions):
    with sessions.begin() as db:
        save(db, START, 8, 100)
        save(db, START+timedelta(hours=1), 10, 300)
        save(db, END, 100, 720)  # Exclusive end: this hour must not enter results.
    with sessions() as db:
        before=list(db.connection().connection.driver_connection.iterdump())
        r=read_ash_hours(db, START, END, NOW)
        assert r['summary']['value'] == 9.5  # Not the unweighted hourly mean 9.
        assert r['summary']['sample_count'] == 400
        assert r['summary']['coverage'] == pytest.approx(400/2160)
        assert r['summary']['missing_hours'] == 1 and r['summary']['partial_hours'] == 2
        assert [i['value'] for i in r['items']] == [8, 10, None]
        assert r['items'][2]['frozen'] is False
        assert r == read_ash_hours(db, START, END, NOW)
        assert list(db.connection().connection.driver_connection.iterdump()) == before
        assert db.scalar(select(DecisionCycleHour).where(DecisionCycleHour.start==iso_time(START))).snapshot['model']['method']=='old-model'


@pytest.mark.parametrize('value,count,status,accepted',[
    (0,720,'average',True),(8.52,10,'average',True),(8.5,0,'average',False),
    (8.5,1,'held',False),(None,5,'missing',False),(True,5,'average',False),
    (float('inf'),1,'average',False),(101,1,'average',False),(8,True,'average',False)])
def test_only_valid_saved_means_are_used(sessions,value,count,status,accepted):
    with sessions.begin() as db:
        save(db,START,value,count,status)
    with sessions() as db:
        r=read_ash_hours(db,START,START+timedelta(hours=1),NOW)
        assert r['summary']['value'] == (value if accepted else None)
        assert r['summary']['valid_hours'] == int(accepted)
        assert r['items'][0]['frozen'] is True


def test_default_24_hours_and_timezone_equivalence(sessions):
    with sessions() as db:
        r=read_ash_hours(db,now=NOW)
        assert len(r['items'])==24 and r['summary']['value'] is None
        assert r['period']['end']==iso_time(END)
        local=timezone(timedelta(hours=8))
        assert read_ash_hours(db,START,END,NOW)==read_ash_hours(db,START.astimezone(local),END.astimezone(local),NOW)
        # Interval crosses UTC midnight while preserving the Beijing natural hours.
        assert datetime.fromisoformat(r['latest_end'])==END


@pytest.mark.parametrize('params',[
    {'start':'2026-10-09T08:00:00+08:00'},
    {'start':'2026-10-09T10:00:00+08:00','end':'2026-10-09T09:00:00+08:00'},
    {'start':'2026-10-09T09:30:00+08:00','end':'2026-10-09T10:00:00+08:00'},
    {'start':'2026-10-09T09:00:00+08:00','end':'2026-10-09T11:00:00+08:00'},
    {'start':'2026-10-09T09:00:00','end':'2026-10-09T10:00:00'},
    {'start':'2026-08-01T00:00:00+08:00','end':'2026-10-09T10:00:00+08:00'},
    {'start':'0001-01-01T00:00:00+08:00','end':'0001-01-02T00:00:00+08:00'},
    {'start':'not-a-date','end':'2026-10-09T10:00:00+08:00'},
])
def test_api_rejects_invalid_ranges(client,monkeypatch,params):
    monkeypatch.setattr('app.decision_hourly.ash_settlement.utc_now',lambda:NOW+timedelta(hours=2))
    assert client.get('/api/v1/decision/total-ash-hours',params=params).status_code==422


def test_api_is_read_only_and_returns_bounded_complete_range(client,sessions,monkeypatch):
    monkeypatch.setattr('app.decision_hourly.ash_settlement.utc_now',lambda:NOW+timedelta(hours=2))
    with sessions.begin() as db:
        save(db,START,8.5,720)
    r=client.get('/api/v1/decision/total-ash-hours',params={'start':iso_time(START),'end':iso_time(END)})
    assert r.status_code==200,r.text
    assert r.json()['summary']['value']==8.5
    assert len(r.json()['items'])==3 and r.json()['items'][0]['status']=='complete'
    with sessions() as db:
        assert len(list(db.scalars(select(DecisionCycleHour))))==1
        assert len(read_ash_hours(db,END-timedelta(days=31),END,NOW)['items'])==744
