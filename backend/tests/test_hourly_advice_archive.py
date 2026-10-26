"""Background archives the card policy; GET never backfills historic advice."""
from datetime import timedelta
from sqlalchemy import select
from test_hourly_quality_advice import prepared, sessions, seeded, HOUR
from app.decision_hourly import advice_archive
from app.decision_hourly.models import GptHourlyDensityAdvice
from app.decision_hourly.service import read_hour


def test_background_archive_matches_card_and_records_late_revision(sessions, prepared):
    with sessions() as db:
        expected=advice_archive.compact(read_hour(db,now=HOUR,live=prepared[0],defer_missing=True))
    advice_archive.archive_tick(sessions,now=HOUR)
    advice_archive.archive_tick(sessions,now=HOUR+timedelta(seconds=1))
    with sessions() as db:
        rows=list(db.scalars(select(GptHourlyDensityAdvice)))
        assert len(rows)==1 and rows[0].snapshot==expected
        original=rows[0].snapshot
    prepared[1]['item']['value']=8.2
    prepared[1]['revision']='late-lab'
    advice_archive.archive_tick(sessions,now=HOUR+timedelta(seconds=2))
    with sessions() as db:
        rows=list(db.scalars(select(GptHourlyDensityAdvice).order_by(GptHourlyDensityAdvice.id)))
        assert len(rows)==2 and rows[0].snapshot==original
        assert rows[1].snapshot['ash']['revision']=='late-lab'
        assert rows[1].snapshot['basis']==rows[0].snapshot['basis']
        assert rows[1].snapshot['value']!=rows[0].snapshot['value']


def test_changed_configuration_during_calculation_is_discarded(sessions,prepared,monkeypatch):
    versions=iter(['before','after'])
    monkeypatch.setattr('app.decision_hourly.observation_guard.configuration_revision',lambda db:next(versions))
    advice_archive.archive_tick(sessions,now=HOUR)
    with sessions() as db:
        assert db.scalar(select(GptHourlyDensityAdvice.id)) is None


def test_calculation_crossing_hour_is_discarded(sessions,prepared,monkeypatch):
    times=iter([HOUR,HOUR+timedelta(hours=1)])
    monkeypatch.setattr(advice_archive,'utc_now',lambda:next(times))
    advice_archive.archive_tick(sessions)
    with sessions() as db:
        assert db.scalar(select(GptHourlyDensityAdvice.id)) is None


def test_pending_is_an_archived_gap_and_recovery_is_a_new_revision(sessions,prepared,monkeypatch):
    advice_archive.archive_tick(sessions,now=HOUR)
    from app.decision_hourly import service
    original=service.read_hour
    monkeypatch.setattr(service,'read_hour',lambda *args,**kwargs:dict(status='pending',reason='等待重新结算'))
    advice_archive.archive_tick(sessions,now=HOUR+timedelta(seconds=1))
    advice_archive.archive_tick(sessions,now=HOUR+timedelta(seconds=2))
    monkeypatch.setattr(service,'read_hour',original)
    advice_archive.archive_tick(sessions,now=HOUR+timedelta(seconds=3))
    with sessions() as db:
        rows=list(db.scalars(select(GptHourlyDensityAdvice).order_by(GptHourlyDensityAdvice.id)))
        assert len(rows)==3 and rows[1].snapshot['value'] is None
        assert rows[0].snapshot==rows[2].snapshot
