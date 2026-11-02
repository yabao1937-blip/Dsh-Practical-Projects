"""Freeze 60/90/120-second level evidence; no fitting or production writes.

Default operation is entirely offline. --fetch-remote explicitly enables only
bounded single-point SELECTs through the existing read-only bridge. Raw history
is cached once; reruns use the cache and never silently fall back to a database.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import itertools
import json
import math
from pathlib import Path
import sys
import time

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

LAGS = (60, 90, 120)
LOOKBACK = 30
SWITCHES = ('sysA', 'sysB', 'sys401', 'sys402', 'desliming473', 'desliming474')
FIXED = ('coal_amount', *SWITCHES)
POINT = ('kepserver-main', 'tag_543c440c9a90e0e2daae')
SQL = ('SELECT id,batch_id,received_at,sample FROM telemetry_readings '
       'INDEXED BY ix_telemetry_point_time '
       'WHERE source_id=? AND point_key=? AND received_at>=? AND received_at<=? '
       'ORDER BY received_at,id LIMIT 128')
LOCAL = timezone(timedelta(hours=8))


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec='milliseconds')


def parse(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Unzoned timestamp is not admissible evidence')
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode('utf-8')).hexdigest()


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def query_rows(connection, sql, parameters):
    cursor = connection.execute(sql, parameters)
    try:
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def bounds(assay):
    observed = parse(assay['observed_at'])
    return stamp(observed - timedelta(seconds=150)), stamp(observed - timedelta(seconds=60))


def fetch_history(audit, audit_sha, destination):
    """Single serial bounded acquisition; first transport/query failure stops it."""
    from remote_mode import settings
    from remote_sqlite import RemoteConnection

    if destination.exists():
        raise ValueError('Refusing to overwrite an existing raw history cache')
    config = settings()
    if not config or config['server_url'] != 'http://10.255.2.7:8000':
        raise ValueError('Expected the configured production read-only origin')
    cache = dict(schema='gpt-factor-level-history-v1', audit_file_sha256=audit_sha,
                 acquired_at=stamp(datetime.now(timezone.utc)), origin='production-readonly-bridge',
                 source_id=POINT[0], point_key=POINT[1], clock='received_at', sql=SQL,
                 max_rows_per_window=128, timeout_seconds=3, serial=True,
                 inter_window_yield_seconds=0.05, windows=[], stopped=None)
    connection = RemoteConnection(config, timeout=3)
    try:
        for index, assay in enumerate(audit['rows']):
            lower, upper = bounds(assay)
            window = dict(assay_id=assay['id'], lower_at=lower, upper_at=upper,
                          origin='production-readonly-bridge', query_complete=False)
            start = time.monotonic()
            try:
                # Fixed known index removes planner ambiguity without a wide probe.
                records = query_rows(connection, SQL, (*POINT, lower, upper))
                connection.rollback()  # release this read snapshot before the next window
                for record in records:
                    if isinstance(record['sample'], str):
                        record['sample'] = json.loads(record['sample'])
                window.update(row_count=len(records), records=records,
                              query_complete=len(records) < 128,
                              status='ok' if len(records) < 128 else 'potentially_truncated',
                              elapsed_ms=round((time.monotonic() - start) * 1000, 2))
            except Exception as error:
                # Bridge errors deliberately do not expose credentials or responses.
                window.update(status='remote_query_failed', row_count=0, records=[],
                              error_type=type(error).__name__,
                              error_code=getattr(error, 'error_code', None))
                cache['stopped'] = dict(assay_id=assay['id'], reason='first_remote_failure_no_retry')
            cache['windows'].append(window)
            write_json(destination, cache)  # keep completed windows even after interruption
            if cache['stopped']:
                break
            if index == 0:
                print(json.dumps(dict(probe='healthy', row_count=window['row_count'],
                                      elapsed_ms=window['elapsed_ms']), ensure_ascii=False), flush=True)
            time.sleep(0.05)
    finally:
        try:
            connection.close()
        except Exception:
            pass
    cache['finished_at'] = stamp(datetime.now(timezone.utc))
    write_json(destination, cache)
    return cache


def fixed_status(assay):
    evidence = assay['evidence']
    selected = [evidence.get(feature, {}) for feature in FIXED]
    same_batch = len({(e.get('source_id'), e.get('batch_id'), e.get('received_at'))
                      for e in selected}) == 1 and all(e.get('batch_id') for e in selected)
    strict = same_batch and all(e.get('status') == 'good' for e in selected)
    return dict(strict=strict, same_batch=same_batch,
                status='strict' if strict else 'not_strict',
                input_sha256=digest({feature: evidence.get(feature) for feature in FIXED}))


def sample_precision(assay):
    observed, created = parse(assay['observed_at']), parse(assay['created_at'])
    return dict(stored_resolution='fractional_second' if observed.microsecond else
                'second_field_nonzero' if observed.second else 'zero_second_field',
                actual_sampling_precision='unconfirmed', clock_offset_seconds=None,
                clock_alignment='not_confirmed_for_this_sample',
                observed_equals_created=observed == created,
                created_minus_observed_seconds=round((created - observed).total_seconds(), 3),
                note='A nonzero second or millisecond field does not establish actual sampling precision; '
                     'blank-time entry can use submission time. No retrospective clock shift applied.')


def classify_level(assay, lag, window):
    observed = parse(assay['observed_at'])
    target = observed - timedelta(seconds=lag)
    output = dict(lag_seconds=lag, target_at=stamp(target), clock='received_at',
                  max_lookback_seconds=LOOKBACK, selection='latest-at-or-before-target',
                  value=None, reading_id=None, batch_id=None, received_at=None, source_at=None,
                  source_timestamp=None, offset_seconds=None, sample_to_reading_seconds=None,
                  target_minus_source_seconds=None, received_minus_source_seconds=None,
                  quality=None, issue=None, storage_kind=None, cadence_seconds=None,
                  definition_id=None, raw_sample_sha256=None, evidence_origin=None,
                  status='no_cached_window', missing_reason='no_cached_window',
                  source_id=POINT[0], point_key=POINT[1])
    # A saved t-90 selection is original audit evidence, not a shifted new observation.
    frozen = assay['evidence'].get('level', {})
    rows = window.get('records', []) if window else []
    eligible = [row for row in rows if parse(row['received_at']) <= target]
    latest = max(eligible, key=lambda row: (parse(row['received_at']), row['id']), default=None)
    if window and not window.get('query_complete'):
        if lag == 90 and frozen.get('reading_id') is not None:
            # The older saved selection is still valid evidence at its own target;
            # an interrupted new fetch must not erase it or imply a new selection.
            latest = None
        else:
            output.update(status='incomplete_cached_window', missing_reason='query_failed_or_potentially_truncated')
            return output
    if latest is not None:
        sample = latest['sample']
        output.update(reading_id=latest['id'], batch_id=latest['batch_id'],
                      received_at=latest['received_at'], value=sample.get('value'),
                      source_at=sample.get('source_at'), source_timestamp=sample.get('source_at'),
                      quality=sample.get('quality'), issue=sample.get('issue'),
                      storage_kind=sample.get('storage_kind', 'legacy'),
                      cadence_seconds=sample.get('cadence_seconds'),
                      definition_id=sample.get('definition_id'), raw_sample_sha256=digest(sample),
                      evidence_origin='raw_history_window')
        status = 'good'
        if sample.get('received_at') != latest['received_at']:
            status = 'timestamp_mismatch'
        elif sample.get('quality') != 'good' or sample.get('issue'):
            status = 'bad_quality'
        elif not frozen.get('definition_id'):
            status = 'unverifiable_definition'
        elif sample.get('definition_id') != frozen['definition_id']:
            status = 'definition_mismatch'
        elif isinstance(output['value'], bool) or not isinstance(output['value'], (int, float)) \
                or not math.isfinite(output['value']):
            status = 'invalid_value'
        elif not 0 <= output['value'] <= 100:
            status = 'invalid_level'
        age = (target - parse(latest['received_at'])).total_seconds()
        if status == 'good' and age > LOOKBACK:
            status = 'gap_over_30s'
        maximum_source_age = frozen.get('configured_max_source_age_seconds')
        if status == 'good' and maximum_source_age:
            try:
                if (target - parse(output['source_at'])).total_seconds() > maximum_source_age:
                    status = 'stale_source_time'
            except (TypeError, ValueError, AttributeError):
                status = 'unverifiable_source_time'
        output.update(status=status, missing_reason=None if status == 'good' else status)
    elif lag == 90 and frozen.get('reading_id') is not None:
        for key in ('value', 'reading_id', 'batch_id', 'received_at', 'source_at', 'quality', 'issue',
                    'definition_id', 'storage_kind', 'raw_sample_sha256', 'source_id', 'point_key'):
            output[key] = frozen.get(key)
        output.update(source_timestamp=frozen.get('source_at'), evidence_origin='frozen_audit_t90',
                      status=frozen['status'],
                      missing_reason=None if frozen['status'] == 'good' else frozen['status'])
    elif window:
        output.update(status='no_hot_history_in_30s', missing_reason='no_hot_history_in_30s')
    if output['received_at']:
        actual = parse(output['received_at'])
        output['offset_seconds'] = round((target - actual).total_seconds(), 3)
        output['sample_to_reading_seconds'] = round((observed - actual).total_seconds(), 3)
        if output['source_at']:
            source = parse(output['source_at'])
            output['target_minus_source_seconds'] = round((target - source).total_seconds(), 3)
            output['received_minus_source_seconds'] = round((actual - source).total_seconds(), 3)
    return output


def describe(values):
    values = sorted(values)
    if not values:
        return dict(n=0, minimum=None, median=None, maximum=None, unique=0)
    import statistics
    return dict(n=len(values), minimum=values[0], median=statistics.median(values),
                maximum=values[-1], unique=len(set(values)))


def group_summary(rows):
    return dict(n=len(rows), assay_ids=[r['assay_id'] for r in rows],
                days=dict(sorted(Counter(r['sample_local_date'] for r in rows).items())),
                coal_amount=describe([r['fixed_features']['coal_amount'] for r in rows]),
                level_by_lag={str(lag): describe([r['levels'][str(lag)]['value'] for r in rows
                                               if r['levels'][str(lag)]['status'] == 'good']) for lag in LAGS},
                ash=describe([r['ash'] for r in rows]))


def same_observations(rows, lag_a, lag_b):
    usable = [(r['levels'][str(lag_a)], r['levels'][str(lag_b)]) for r in rows
              if all(r['levels'][str(lag)]['status'] == 'good' for lag in (lag_a, lag_b))]
    return dict(n_both_good=len(usable),
                same_reading_id=sum(a['reading_id'] == b['reading_id'] and a['source_id'] == b['source_id']
                                    for a, b in usable),
                same_received_observation=sum((a['source_id'], a['point_key'], a['received_at']) ==
                                              (b['source_id'], b['point_key'], b['received_at']) for a, b in usable),
                same_source_timestamp=sum(a['source_at'] is not None and a['source_at'] == b['source_at']
                                          for a, b in usable),
                actual_received_spacing_seconds=describe([(parse(a['received_at']) - parse(b['received_at'])).total_seconds()
                                                          for a, b in usable]),
                same_value=sum(a['value'] == b['value'] for a, b in usable))


def history_summary(cache):
    unique_rows = {row['id']: row for window in cache['windows'] for row in window['records']}
    intervals = {}
    for window in cache['windows']:
        ordered = sorted(window['records'], key=lambda row: (parse(row['received_at']), row['id']))
        for first, second in zip(ordered, ordered[1:]):
            intervals[(first['id'], second['id'])] = (parse(second['received_at']) - parse(first['received_at'])).total_seconds()
    kinds = Counter((row['sample'].get('storage_kind', 'legacy'), row['sample'].get('cadence_seconds'))
                    for row in unique_rows.values())
    return dict(distinct_rows=len(unique_rows),
                kind_cadence_counts=[dict(storage_kind=kind, cadence_seconds=cadence, n=n)
                                     for (kind, cadence), n in sorted(kinds.items(), key=lambda pair: str(pair[0]))],
                adjacent_interval_seconds=describe(list(intervals.values())),
                interval_note='Deduplicated adjacent row pairs within each 90-second sample window only; '
                              'not a continuous history survey and not evidence of historic 30-second periodic policy.')


def daily_coverage(rows):
    result = []
    keys = sorted({(r['sample_local_date'], r['six_switch_combination']) for r in rows})
    for day, combo in keys:
        selected = [r for r in rows if r['sample_local_date'] == day and r['six_switch_combination'] == combo]
        entry = dict(sample_local_date=day, six_switch_combination=combo, system_count=selected[0]['system_count'],
                     all_count=len(selected), strict_count=sum(r['strict_analysis_eligible'] for r in selected),
                     minute_approximation_count=sum(r['eligibility'] == 'minute_approximation' for r in selected),
                     label_review_count=sum(r['eligibility'] == 'label_review' for r in selected),
                     incomplete_count=sum(r['eligibility'] == 'incomplete' for r in selected))
        for lag in LAGS:
            entry[f'level_good_{lag}'] = sum(r['levels'][str(lag)]['status'] == 'good' for r in selected)
            entry[f'strict_good_{lag}'] = sum(r['strict_analysis_eligible'] and r['levels'][str(lag)]['status'] == 'good'
                                             for r in selected)
        entry['common_valid_count'] = sum(r['common_valid'] for r in selected)
        entry['common_valid_ids'] = ';'.join(str(r['assay_id']) for r in selected if r['common_valid'])
        result.append(entry)
    return result


def build_pairings(audit, cache):
    windows = {window['assay_id']: window for window in cache['windows']}
    rows = []
    for input_row_number, assay in enumerate(audit['rows'], 1):
        fixed = fixed_status(assay)
        features = {feature: assay['features'][feature] for feature in FIXED}
        combo = ','.join(str(features[feature]) for feature in SWITCHES)
        levels = {str(lag): classify_level(assay, lag, windows.get(assay['id'])) for lag in LAGS}
        strict = assay['eligibility'] == 'strict' and fixed['strict']
        common = strict and all(e['status'] == 'good' for e in levels.values())
        rows.append(dict(assay_id=assay['id'], input_row_number=input_row_number, ash=assay['value'],
                         sample_at=assay['observed_at'], sample_at_local=assay['observed_local'],
                         sample_local_date=assay['observed_local'][:10], created_at=assay['created_at'],
                         time_basis=assay['time_basis'], source_revision=assay.get('source_revision'),
                         original_pairing=assay['pairing'], eligibility=assay['eligibility'],
                         label_flags=copy.deepcopy(assay['label_flags']), precision=sample_precision(assay),
                         fixed_features=features, fixed_evidence=copy.deepcopy({f: assay['evidence'][f] for f in FIXED}),
                         fixed_factor_status=fixed, six_switch_combination=combo,
                         system_count=sum(features[f] for f in SWITCHES[:4]) if all(features[f] in (0, 1) for f in SWITCHES[:4]) else None,
                         levels=levels, strict_analysis_eligible=strict, common_valid=common,
                         physical_lag_confirmed_sample=False,
                         historical_partition='historical_development' if assay['observed_local'][:10] < '2026-10-28'
                                              else 'historical_replay_already_viewed'))
    strict = [r for r in rows if r['strict_analysis_eligible']]
    common = [r for r in rows if r['common_valid']]
    combinations = {}
    for combination in sorted({r['six_switch_combination'] for r in rows}):
        selected = [r for r in rows if r['six_switch_combination'] == combination]
        combinations[combination] = dict(system_count=selected[0]['system_count'],
            all=dict(Counter(r['eligibility'] for r in selected)),
            strict=group_summary([r for r in selected if r['strict_analysis_eligible']]),
            common=group_summary([r for r in selected if r['common_valid']]))
    summary = dict(schema='gpt-factor-pairing-v1', sample_count=len(rows),
                   eligibility=dict(Counter(r['eligibility'] for r in rows)),
                   fixed_strict_count=sum(r['fixed_factor_status']['strict'] for r in rows),
                   stored_resolution=dict(Counter(r['precision']['stored_resolution'] for r in rows)),
                   observed_equals_created_count=sum(r['precision']['observed_equals_created'] for r in rows),
                   confirmed_actual_second_precision_count=0,
                   candidates={str(lag): dict(
                       all_status=dict(Counter(r['levels'][str(lag)]['status'] for r in rows)),
                       strict_status=dict(Counter(r['levels'][str(lag)]['status'] for r in strict)),
                       strict_good_ids=[r['assay_id'] for r in strict if r['levels'][str(lag)]['status'] == 'good'],
                       strict_good_count=sum(r['levels'][str(lag)]['status'] == 'good' for r in strict),
                       strict_denominator=len(strict),
                       strict_offset=describe([r['levels'][str(lag)]['offset_seconds'] for r in strict
                                               if r['levels'][str(lag)]['status'] == 'good']),
                       strict_actual_lead_seconds=describe([r['levels'][str(lag)]['sample_to_reading_seconds'] for r in strict
                                                           if r['levels'][str(lag)]['status'] == 'good']),
                       strict_received_minus_source_seconds=describe([r['levels'][str(lag)]['received_minus_source_seconds'] for r in strict
                                                                     if r['levels'][str(lag)]['status'] == 'good' and
                                                                     r['levels'][str(lag)]['received_minus_source_seconds'] is not None]))
                               for lag in LAGS},
                   common=group_summary(common), combinations=combinations,
                   pairwise={f'{a}_{b}': dict(strict=same_observations(strict, a, b),
                                             common=same_observations(common, a, b))
                             for a, b in itertools.combinations(LAGS, 2)},
                   historical_t90_consistency=dict(
                       checked_count=len(rows),
                       same_reading_id_count=sum(assay['evidence']['level'].get('reading_id') == row['levels']['90']['reading_id']
                                                 for assay, row in zip(audit['rows'], rows)),
                       both_missing_reading_id_count=sum(assay['evidence']['level'].get('reading_id') is None and
                                                         row['levels']['90']['reading_id'] is None
                                                         for assay, row in zip(audit['rows'], rows)),
                       same_value_count=sum(assay['evidence']['level'].get('value') == row['levels']['90']['value']
                                            for assay, row in zip(audit['rows'], rows))),
                   history=history_summary(cache),
                   query=dict(window_count=len(cache['windows']),
                              statuses=dict(Counter(w['status'] for w in cache['windows'])),
                              raw_rows_with_window_overlap=sum(len(w['records']) for w in cache['windows']),
                              distinct_raw_ids=len({r['id'] for w in cache['windows'] for r in w['records']}),
                              stopped=cache.get('stopped')))
    return rows, summary


def write_csv(path, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def export(output, audit, cache, audit_sha):
    rows, summary = build_pairings(audit, cache)
    summary.update(audit_file_sha256=audit_sha, audit_dataset_sha256=audit['dataset_sha256'],
                   policy=dict(lags_seconds=LAGS, lookback_seconds=LOOKBACK, clock='received_at',
                               interpolate=False, retrospective_clock_shift=False,
                               quality_selection='latest raw observation before quality validation',
                               original_eligibility_preserved=True, fixed_factors=FIXED,
                               regular_label_filter='none; historical bounds not reapplied',
                               purpose='freeze descriptive pairing input only; no model fitting'))
    write_json(output/'pairings.json', dict(schema='gpt-factor-pairing-rows-v1', rows=rows))
    write_json(output/'summary.json', summary)
    flattened = []
    for row in rows:
        for lag in LAGS:
            flattened.append(dict(assay_id=row['assay_id'], input_row_number=row['input_row_number'],
                                  sample_at=row['sample_at'], sample_at_local=row['sample_at_local'], ash=row['ash'],
                                  eligibility=row['eligibility'], original_pairing=row['original_pairing'],
                                  label_flags=';'.join(row['label_flags']),
                                  stored_resolution=row['precision']['stored_resolution'],
                                  actual_sampling_precision='unconfirmed', clock_offset_seconds=None,
                                  six_switch_combination=row['six_switch_combination'], system_count=row['system_count'],
                                  common_valid=row['common_valid'], **row['fixed_features'], **row['levels'][str(lag)]))
    write_csv(output/'pairings.csv', flattened)
    fixed_rows = []
    for row in rows:
        for factor in FIXED:
            evidence = row['fixed_evidence'][factor]
            fixed_rows.append(dict(assay_id=row['assay_id'], factor=factor, value=row['fixed_features'][factor],
                                   target_at=evidence.get('target_at'), status=evidence.get('status'),
                                   reading_id=evidence.get('reading_id'), batch_id=evidence.get('batch_id'),
                                   source_id=evidence.get('source_id'), point_key=evidence.get('point_key'),
                                   received_at=evidence.get('received_at'), source_at=evidence.get('source_at'),
                                   offset_seconds=evidence.get('age_seconds'), quality=evidence.get('quality'),
                                   issue=evidence.get('issue'), storage_kind=evidence.get('storage_kind'),
                                   definition_id=evidence.get('definition_id'), raw_sample_sha256=evidence.get('raw_sample_sha256')))
    write_csv(output/'fixed_factors.csv', fixed_rows)
    write_csv(output/'daily_coverage.csv', daily_coverage(rows))
    write_json(output/'cohorts.json', dict(common_valid_ids=[r['assay_id'] for r in rows if r['common_valid']],
                                         strict_source_ids=[r['assay_id'] for r in rows if r['strict_analysis_eligible']],
                                         physical_lag_confirmed_sample_ids=[],
                                         note='Existing development/replay data, never a new independent validation set.'))
    files = sorted(p for p in output.iterdir() if p.is_file() and p.name not in ('manifest.json', 'run.log', 'tests.log'))
    write_json(output/'manifest.json', dict(schema='gpt-factor-pairing-manifest-v1',
                                         source_scripts={str(Path(__file__).relative_to(BACKEND.parent)).replace('\\', '/'):
                                                         file_hash(Path(__file__)),
                                                         'backend/tests/test_gpt_factor_pairing.py':
                                                         file_hash(BACKEND/'tests/test_gpt_factor_pairing.py')},
                                         files={p.name: dict(bytes=p.stat().st_size, sha256=file_hash(p)) for p in files}))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--fetch-remote', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    audit_sha = file_hash(args.audit)
    audit = json.loads(args.audit.read_text(encoding='utf-8-sig'))
    frozen_input = args.output/'input_audit.json'
    if frozen_input.exists() and file_hash(frozen_input) != audit_sha:
        raise ValueError('Frozen source audit changed; choose a new output directory')
    if not frozen_input.exists():
        frozen_input.write_bytes(args.audit.read_bytes())
    cache_path = args.output/'history_cache.json'
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
    elif args.fetch_remote:
        cache = fetch_history(audit, audit_sha, cache_path)
    else:
        raise ValueError('Offline operation requires an existing history_cache.json')
    if cache['audit_file_sha256'] != audit_sha:
        raise ValueError('History cache is bound to a different frozen audit')
    summary = export(args.output, audit, cache, audit_sha)
    print(json.dumps(dict(output=str(args.output.resolve()), sample_count=summary['sample_count'],
                          candidate_good={k: v['strict_good_count'] for k, v in summary['candidates'].items()},
                          common_count=summary['common']['n'], queries=summary['query']), ensure_ascii=False))


if __name__ == '__main__':
    main()
