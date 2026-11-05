from copy import deepcopy
from datetime import timedelta

from app.decision_hourly.density_series import hourly_finals
from app.decision_hourly import density_series as series
from app.telemetry.domain import iso_time,parse_time


START=parse_time('2026-11-03T00:00:00+00:00')  # Beijing 08:00


def record(minutes,value=1.48,status='hold',slot=1):
    return dict(at=iso_time(START+timedelta(minutes=minutes)),value=value,status=status,
        slot=slot,source=dict(kind='real-db-sample'),break_before=True)


def test_latest_record_not_latest_good_and_original_identity_is_preserved():
    points=[record(5,1.49,'adjust',1),record(59,None,'unavailable',2)]
    original=deepcopy(points)
    rows,summary=hourly_finals(points,START,START+timedelta(hours=1))
    assert len(rows)==1 and rows[0]['value'] is None
    assert rows[0]['status']=='unavailable' and rows[0]['slot']==2
    assert rows[0]['at']==points[-1]['at']==rows[0]['final_sample_at']
    assert rows[0]['hour_start']==iso_time(START)
    assert rows[0]['hour_end']==iso_time(START+timedelta(hours=1))
    assert not rows[0]['final_valid'] and summary['missing_hours']==1
    assert points==original


def test_unfinished_last_hour_is_excluded_even_with_valid_intermediate_result():
    points=[record(59),record(61,1.49,'adjust')]
    rows,summary=hourly_finals(points,START,START+timedelta(minutes=64))
    assert len(rows)==1 and rows[0]['at']==points[0]['at']
    assert rows[0]['complete'] and summary['total_hours']==1


def test_custom_cutoff_does_not_relabel_mid_hour_sample_as_final():
    points=[record(10,1.49,'adjust',1),record(50,None,'unavailable',2),record(119,1.50,'hold',3)]
    rows,_=hourly_finals(points,START,START+timedelta(minutes=40))
    assert rows==[]
    rows,_=hourly_finals(points,START+timedelta(minutes=20),START+timedelta(minutes=120))
    assert [p['slot'] for p in rows]==[2,3]
    assert rows[0]['value'] is None  # Never choose minute 10's earlier good.


def test_completed_hour_at_exact_cutoff_and_equal_timestamp_last_row_wins():
    points=[record(59,1.48,'hold',1),record(59,1.49,'adjust',2),record(60,1.50,'adjust',3)]
    rows,_=hourly_finals(points,START,START+timedelta(minutes=60))
    assert [p['slot'] for p in rows]==[2]
    assert rows[0]['aggregation']=='last-record-of-completed-hour'


def test_business_hour_timezone_offsets_and_midnight_are_grouped_together():
    points=[dict(at='2026-11-03T23:58:00+08:00',value=1.48,status='hold',slot=1),
            dict(at='2026-11-03T15:59:00+00:00',value=1.49,status='adjust',slot=2),
            dict(at='2026-11-04T00:01:00+08:00',value=1.50,status='adjust',slot=3)]
    rows,_=hourly_finals(points,parse_time('2026-11-03T23:00:00+08:00'),parse_time('2026-11-04T00:04:00+08:00'))
    assert len(rows)==1 and rows[0]['slot']==2
    assert rows[0]['hour_start']=='2026-11-03T15:00:00.000+00:00'
    assert rows[0]['hour_end']=='2026-11-03T16:00:00.000+00:00'


def test_missing_whole_hour_breaks_hourly_line_but_minute_gap_is_not_reinterpreted():
    rows,_=hourly_finals([record(59),record(179,1.49,'hold',3)],START,START+timedelta(hours=3))
    assert not rows[0]['hourly_break_before']
    assert rows[1]['hourly_break_before']
    assert rows[0]['break_before']  # Original record's minute evidence is unchanged.


def test_invalid_numeric_last_record_stays_ineligible_without_older_good_fallback():
    rows,summary=hourly_finals([record(5),record(59,1.51,'unavailable',2)],START,START+timedelta(hours=1))
    assert rows[0]['value']==1.51  # Retain original raw result, but never draw as valid.
    assert not rows[0]['final_valid'] and summary['valid_hours']==0


def test_read_frozen_hourly_uses_full_records_even_when_plot_omits_last(tmp_path,monkeypatch):
    monkeypatch.setenv('DMCS_DENSITY_EXPERIMENT','1')
    monkeypatch.setattr('app.config.BASE_DIR',tmp_path/'backend')
    path=tmp_path/'output/density-optimization-db7-20261104/series.json'
    path.parent.mkdir(parents=True);path.write_text('{}')
    points=[dict(record(5,1.49,'adjust',1),input_evidence={},quality=dict(level='non-provisional'),reason_codes=[]),
            dict(record(59,None,'unavailable',2),input_evidence={},quality=dict(level='unavailable'),reason_codes=[])]
    source=dict(period=dict(start=iso_time(START),end=iso_time(START+timedelta(hours=6))),ash=[],source={})
    monkeypatch.setattr(series,'_frozen',lambda *args:(source,points))
    monkeypatch.setattr(series,'plot_indices',lambda *args:[0])
    result=series.read_frozen(hours=6)
    assert result['suggestions'][0]['slot']==1
    assert result['summary']['total_samples']==2
    assert result['hourly_suggestions'][0]['slot']==2
    assert result['latest_completed_hour']['value'] is None
    assert result['hourly_summary']['missing_hours']==1
