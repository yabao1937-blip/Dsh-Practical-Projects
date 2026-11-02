"""Pairing and provenance contracts on synthetic records; never train a model."""
import copy
from datetime import datetime, timedelta, timezone

import pytest

from scripts.audit_gpt_factor_pairing import (
    FIXED, POINT, build_pairings, classify_level, daily_coverage, digest,
    fetch_history, same_observations, sample_precision, stamp,
)

OBSERVED = datetime(2026, 10, 20, 8, 0, 7, tzinfo=timezone.utc)


def record(identifier, seconds_before, value=50.0, **changes):
    received = stamp(OBSERVED - timedelta(seconds=seconds_before))
    sample = dict(key=POINT[1], value=value, received_at=received,
                  source_at=stamp(OBSERVED - timedelta(seconds=seconds_before + 12)),
                  quality='good', issue=None, definition_id='frozen-definition',
                  storage_kind='periodic', cadence_seconds=60)
    sample.update(changes)
    return dict(id=identifier, batch_id=f'level-{identifier}', received_at=received, sample=sample)


def assay(identifier=1, eligibility='strict'):
    fixed = dict(coal_amount=600.0, sysA=0, sysB=1, sys401=1, sys402=1,
                 desliming473=0, desliming474=0)
    evidence = {key: dict(status='good', value=value, source_id=POINT[0], point_key=f'key-{key}',
                          reading_id=identifier * 100 + index, batch_id='fixed-batch',
                          received_at=stamp(OBSERVED - timedelta(seconds=2)),
                          target_at=stamp(OBSERVED), age_seconds=2,
                          quality='good', raw_sample_sha256=f'fixed-{index}')
                for index, (key, value) in enumerate(fixed.items())}
    raw = record(90, 92)
    evidence['level'] = dict(status='good', value=50.0, reading_id=raw['id'], batch_id=raw['batch_id'],
                             received_at=raw['received_at'], source_at=raw['sample']['source_at'],
                             target_at=stamp(OBSERVED - timedelta(seconds=90)), age_seconds=2,
                             definition_id='frozen-definition', quality='good', issue=None,
                             source_id=POINT[0], point_key=POINT[1], storage_kind='periodic',
                             configured_max_source_age_seconds=None, raw_sample_sha256=digest(raw['sample']))
    return dict(id=identifier, value=12.0, observed_at=stamp(OBSERVED),
                observed_local=OBSERVED.astimezone(timezone(timedelta(hours=8))).isoformat(),
                created_at=stamp(OBSERVED + timedelta(hours=1)), time_basis='production-time-v1',
                source_revision=4, pairing='strict', eligibility=eligibility,
                label_flags=[] if eligibility != 'label_review' else ['nearby_conflicting_labels'],
                evidence=evidence, features=dict(fixed, level=50.0))


def window(identifier=1, records=None, **changes):
    result = dict(assay_id=identifier, query_complete=True, status='ok',
                  records=records if records is not None else [record(60, 62), record(90, 92), record(120, 122)])
    result.update(changes)
    return result


@pytest.mark.parametrize('age,status', [(0, 'good'), (30, 'good'), (30.001, 'gap_over_30s')])
def test_inclusive_30_second_lookback(age, status):
    result = classify_level(assay(), 60, window(records=[record(1, 60 + age)]))
    assert result['status'] == status
    assert result['offset_seconds'] == pytest.approx(age, abs=.001)
    assert result['sample_to_reading_seconds'] == pytest.approx(60 + age, abs=.001)


def test_latest_raw_bad_quality_is_not_replaced_by_an_older_good_or_future_row():
    result = classify_level(assay(), 60, window(records=[
        record(1, 65), record(2, 61, quality='bad'), record(3, 59),
    ]))
    assert result['reading_id'] == 2
    assert result['status'] == 'bad_quality'
    assert result['missing_reason'] == 'bad_quality'


def test_same_timestamp_uses_highest_id_then_checks_definition_and_no_interpolation():
    result = classify_level(assay(), 60, window(records=[
        record(1, 61), record(2, 61, definition_id='other'),
    ]))
    assert result['reading_id'] == 2
    assert result['status'] == 'definition_mismatch'
    missing = classify_level(assay(), 120, window(records=[record(3, 119)]))
    assert missing['status'] == 'no_hot_history_in_30s'
    assert missing['value'] is None


def test_frozen_90_evidence_is_not_shifted_and_survives_missing_or_truncated_new_cache():
    saved = assay()
    for history in (None, window(records=[]), window(query_complete=False)):
        result = classify_level(saved, 90, history)
        assert result['status'] == 'good'
        assert result['reading_id'] == saved['evidence']['level']['reading_id']
        assert result['received_at'] == saved['evidence']['level']['received_at']
        assert result['offset_seconds'] == 2
        assert result['evidence_origin'] == 'frozen_audit_t90'
    assert classify_level(saved, 60, None)['status'] == 'no_cached_window'
    assert classify_level(saved, 60, window(query_complete=False))['status'] == 'incomplete_cached_window'


def test_source_timestamp_is_diagnostic_and_its_age_limit_is_only_explicit():
    raw = record(1, 61, source_at=stamp(OBSERVED - timedelta(hours=4)))
    result = classify_level(assay(), 60, window(records=[raw]))
    assert result['status'] == 'good'
    assert result['offset_seconds'] == 1
    assert result['target_minus_source_seconds'] == 4 * 3600 - 60
    limited = assay()
    limited['evidence']['level']['configured_max_source_age_seconds'] = 60
    assert classify_level(limited, 60, window(records=[raw]))['status'] == 'stale_source_time'


def test_strict_eligibility_common_denominator_and_fixed_inputs_are_preserved():
    sources = [assay(1), assay(2, 'label_review'), assay(3, 'minute_approximation'), assay(4)]
    before = copy.deepcopy(sources)
    # Row 4 has level evidence at t-90 and t-120 but none within t-60's lookback.
    cache = dict(windows=[window(1), window(2), window(3), window(4, records=[record(90, 92), record(120, 122)])])
    rows, summary = build_pairings(dict(rows=sources), cache)
    assert sources == before
    assert summary['candidates']['60']['strict_good_count'] == 1
    assert summary['candidates']['90']['strict_good_count'] == 2
    assert summary['common']['assay_ids'] == [1]
    assert rows[1]['original_pairing'] == 'strict' and not rows[1]['strict_analysis_eligible']
    assert not rows[2]['strict_analysis_eligible']
    for source, row in zip(sources, rows):
        assert row['fixed_features'] == {key: source['features'][key] for key in FIXED}
        assert row['fixed_evidence'] == {key: source['evidence'][key] for key in FIXED}
    coverage = daily_coverage(rows)
    assert len(coverage) == 1
    assert coverage[0]['strict_count'] == 2
    assert coverage[0]['strict_good_60'] == coverage[0]['common_valid_count'] == 1


def test_seconds_or_submission_timestamp_does_not_establish_sampling_precision():
    for at in (OBSERVED, OBSERVED.replace(second=0), OBSERVED.replace(microsecond=123000)):
        source = assay()
        source['observed_at'] = source['created_at'] = stamp(at)
        result = sample_precision(source)
        assert result['actual_sampling_precision'] == 'unconfirmed'
        assert result['clock_offset_seconds'] is None
        assert result['observed_equals_created']


def test_identity_value_and_source_time_diagnostics_remain_separate():
    a, b = record(1, 91), record(2, 91)
    first = classify_level(assay(), 60, window(records=[a]))
    second = classify_level(assay(), 90, window(records=[b]))
    first['status'] = 'good'  # synthetic identity comparison, independent of lag validity
    result = same_observations([dict(levels={'60': first, '90': second})], 60, 90)
    assert result['same_reading_id'] == 0
    assert result['same_received_observation'] == result['same_value'] == result['same_source_timestamp'] == 1
    second['value'] += 1e-12
    assert same_observations([dict(levels={'60': first, '90': second})], 60, 90)['same_value'] == 0


def test_remote_acquisition_stops_on_first_failure_without_retry_or_remaining_windows(monkeypatch, tmp_path):
    import remote_mode
    import remote_sqlite
    import scripts.audit_gpt_factor_pairing as module

    calls = []
    class FakeConnection:
        def __init__(self, config, *, timeout):
            assert timeout == 3
        def execute(self, sql, parameters):
            calls.append((sql, parameters))
            raise TimeoutError('synthetic transport timeout')
        def close(self):
            pass
    monkeypatch.setattr(remote_sqlite, 'RemoteConnection', FakeConnection)
    monkeypatch.setattr(remote_mode, 'settings', lambda: dict(server_url='http://10.255.2.7:8000', token='unused'))
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    cache = fetch_history(dict(rows=[assay(1), assay(2)]), 'audit-hash', tmp_path/'cache.json')
    assert len(calls) == 1
    assert 'INDEXED BY ix_telemetry_point_time' in calls[0][0]
    assert 'LIMIT 128' in calls[0][0]
    assert cache['stopped']['assay_id'] == 1
    assert len(cache['windows']) == 1 and not cache['windows'][0]['query_complete']
