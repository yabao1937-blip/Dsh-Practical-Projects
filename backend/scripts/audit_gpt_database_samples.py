"""Read-only inventory of manual coarse-ash assays and their eight-factor evidence.

No fitting, activation, production writes, interpolation or future-value fills.
Use bounded queries through the read-only bridge; credentials stay in memory.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from urllib.request import Request, build_opener, ProxyHandler

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.gpt_coarse.expert8 import FEATURES
from app.telemetry.derived import FOUR_SYSTEM_MEMBERS, DESLIMING_MEMBERS
from point_metadata import Catalog
from remote_mode import settings
from remote_sqlite import connect

LOCAL = timezone(timedelta(hours=8))
ITEMS = dict(zip(('sys401', 'sys402', 'sysA', 'sysB'), (p[1] for p in FOUR_SYSTEM_MEMBERS)))
ITEMS.update(dict(zip(('desliming473', 'desliming474'), (p[1] for p in DESLIMING_MEMBERS))))


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec='milliseconds')


def local(value):
    return datetime.fromisoformat(value).astimezone(LOCAL).isoformat(timespec='seconds')


def feature_target(feature, observed):
    return observed - timedelta(seconds=90 if feature == 'level' else 0)


def lookback_hours(target):
    target = datetime.fromisoformat(target)
    hour = (target - timedelta(seconds=120)).replace(minute=0, second=0, microsecond=0)
    result = []
    while hour <= target:
        result.append(stamp(hour))
        hour += timedelta(hours=1)
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def rows(connection, sql, params=()):
    cursor = connection.execute(sql, params)
    names = [c[0] for c in cursor.description]
    try:
        return [dict(zip(names, row)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def classify_evidence(feature, target, record, sample, config):
    """Validate the latest preceding row, never skip a bad row to find a good one."""
    if record.get('id') is None:
        return dict(status='no_hot_history_120s', value=None, age_seconds=None)
    age = (datetime.fromisoformat(target) - datetime.fromisoformat(record['received_at'])).total_seconds()
    value = sample.get('value')
    definition = hashlib.sha256(json.dumps({k: config.get(k) for k in
        ('node_id', 'unit', 'scale', 'offset', 'value_type', 'minimum', 'maximum')},
        sort_keys=True).encode()).hexdigest()
    status = 'good'
    if age < 0:
        status = 'future_reading'
    elif sample.get('received_at') != record['received_at']:
        status = 'timestamp_mismatch'
    elif sample.get('quality') != 'good' or sample.get('issue'):
        status = 'bad_quality'
    elif sample.get('definition_id') != definition:
        status = 'definition_mismatch'
    elif feature in ITEMS and not isinstance(value, bool):
        status = 'invalid_switch'
    elif feature not in ITEMS and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value)):
        status = 'invalid_value'
    elif feature == 'coal_amount' and value <= 10:
        status = 'stopped'
    elif feature == 'level' and not 0 <= value <= 100:
        status = 'invalid_level'
    elif age > 30:
        status = 'minute_approximation' if feature != 'level' and age <= 65 else 'gap'
    maximum_source_age = config.get('max_source_age_seconds')
    if status in ('good', 'minute_approximation') and maximum_source_age:
        try:
            source_age = (datetime.fromisoformat(target) - datetime.fromisoformat(sample['source_at'])).total_seconds()
            if source_age > maximum_source_age:
                status = 'stale_source_time'
        except (KeyError, TypeError, ValueError):
            status = 'unverifiable_source_time'
    return dict(status=status, value=int(value) if isinstance(value, bool) else value,
                age_seconds=round(age, 3), reading_id=record['id'], batch_id=record['batch_id'],
                received_at=record['received_at'], source_at=sample.get('source_at'),
                quality=sample.get('quality'), issue=sample.get('issue'),
                definition_id=sample.get('definition_id'),
                storage_kind=sample.get('storage_kind', 'legacy'),
                configured_max_age_seconds=config.get('max_age_seconds'),
                configured_max_source_age_seconds=maximum_source_age,
                raw_sample_sha256=digest(sample))


def pairing_status(evidence):
    if set(evidence) != set(FEATURES):
        return 'incomplete', False
    same_time = [evidence[f] for f in FEATURES if f != 'level']
    batches = {(e.get('source_id'), e.get('batch_id'), e.get('received_at')) for e in same_time}
    same_batch = len(batches) == 1 and all(e.get('batch_id') and e.get('received_at') for e in same_time)
    if not all(e.get('status') in ('good', 'minute_approximation') for e in evidence.values()):
        return 'incomplete', same_batch
    if not same_batch:
        return 'mixed_batches', False
    if all(e['status'] == 'good' for e in evidence.values()):
        return 'strict', True
    return ('minute_approximation', True) if evidence['level']['status'] == 'good' else ('incomplete', True)


def label_flags(assays):
    flags = {a['id']: [] for a in assays}
    nearby = []
    ordered = sorted(assays, key=lambda a: (a['observed_at'], a['id']))
    for a in ordered:
        if datetime.fromisoformat(a['observed_at']) > datetime.fromisoformat(a['created_at']):
            flags[a['id']].append('observed_after_created')
        if not 0 < a['value'] <= 100:
            flags[a['id']].append('invalid_label')
    for previous, current in zip(ordered, ordered[1:]):
        seconds = (datetime.fromisoformat(current['observed_at']) -
                   datetime.fromisoformat(previous['observed_at'])).total_seconds()
        if seconds <= 120:
            kind = 'nearby_same_label' if previous['value'] == current['value'] else 'nearby_conflicting_labels'
            flags[previous['id']].append(kind)
            flags[current['id']].append(kind)
            nearby.append(dict(ids=[previous['id'], current['id']], seconds=seconds, kind=kind))
    return {key: sorted(set(value)) for key, value in flags.items()}, nearby


def inventory(connection, catalog, *, max_id=None):
    sql = "SELECT id,value,observed_at,created_at,time_basis FROM manual_measurements WHERE metric=?"
    params = ['coarse_slime_ash']
    if max_id is not None:
        sql += ' AND id<=?'
        params.append(max_id)
    assays = rows(connection, sql + ' ORDER BY observed_at,id LIMIT 5001', params)
    if not assays or len(assays) > 5000:
        raise ValueError('No assays, or more than 5000 assays; choose a bounded audit batch')
    points = {(r['source_id'], r['key']): catalog.loads(r['config']) for r in
              rows(connection, 'SELECT source_id,key,config FROM telemetry_points')}
    revisions = rows(connection, 'SELECT id,effective_at,config FROM decision_source_revisions ORDER BY effective_at,id')
    for revision in revisions:
        revision['choices'] = catalog.loads(revision.pop('config')).get('choices', {})
    output, targets = {}, []
    flags, nearby = label_flags(assays)
    for assay in assays:
        at = datetime.fromisoformat(assay['observed_at'])
        result = dict(assay, observed_local=local(assay['observed_at']),
                      created_local=local(assay['created_at']), label_flags=flags[assay['id']], evidence={})
        output[assay['id']] = result
        applicable = [r for r in revisions if r['effective_at'] <= assay['observed_at']]
        revision = applicable[-1] if applicable else None
        result['source_revision'] = revision['id'] if revision else None
        for feature in FEATURES:
            group = {'coal_amount': 'coal-rate', 'level': 'tail-level'}.get(feature,
                      'system-switches' if feature.startswith('sys') else 'desliming')
            choice = revision['choices'].get(group) if revision else None
            choice = json.loads(choice) if choice else None
            if feature in ('coal_amount', 'level'):
                candidates = [tuple(choice)] if choice and tuple(choice) in points else []
            else:
                expected = 'four_system_status' if feature.startswith('sys') else 'desliming_status'
                candidates = [key for key, cfg in points.items() if cfg.get('item_id') == ITEMS[feature]
                              and choice and choice[1] == expected and key[0] == choice[0]]
            if len(candidates) != 1:
                result['evidence'][feature] = dict(status='ambiguous_point', value=None)
                continue
            target = feature_target(feature, at)
            targets.append((assay['id'], feature, *candidates[0], stamp(target),
                            stamp(target - timedelta(seconds=120))))
    for offset in range(0, len(targets), 128):
        block = targets[offset:offset + 128]
        values = ','.join(['(?,?,?,?,?,?)'] * len(block))
        sql = ('WITH targets(assay_id,feature,source_id,point_key,target_at,lower_at) AS (VALUES ' + values + ') '
               'SELECT t.assay_id,t.feature,t.source_id,t.point_key,t.target_at,'
               'r.id,r.batch_id,r.received_at,r.sample FROM targets t LEFT JOIN telemetry_readings r '
               'ON r.id=(SELECT id FROM telemetry_readings WHERE source_id=t.source_id AND point_key=t.point_key '
               'AND received_at<=t.target_at AND received_at>=t.lower_at '
               'ORDER BY received_at DESC,id DESC LIMIT 1)')
        for record in rows(connection, sql, [v for target in block for v in target]):
            sample = catalog.loads(record.pop('sample')) if record['id'] is not None else {}
            key = (record['source_id'], record['point_key'])
            result = classify_evidence(record['feature'], record['target_at'], record, sample, points[key])
            result.update(source_id=key[0], point_key=key[1], target_at=record['target_at'])
            output[record['assay_id']]['evidence'][record['feature']] = result
    for row in output.values():
        row['pairing'], row['same_batch'] = pairing_status(row['evidence'])
        row['eligibility'] = 'label_review' if row['label_flags'] else row['pairing']
        row['features'] = {f: row['evidence'][f].get('value') for f in FEATURES}
    # Only probe the relevant hourly catalog keys; never scan cold history.
    archive_checks = {}
    missing_hours = {hour for row in output.values() for e in row['evidence'].values()
                     if e['status'] == 'no_hot_history_120s' for hour in lookback_hours(e['target_at'])}
    has_archives = bool(rows(connection, "SELECT name FROM sqlite_master WHERE type='table' AND name='retention_archives'"))
    for hour in sorted(missing_hours):
        end = stamp(datetime.fromisoformat(hour) + timedelta(hours=1))
        archive_checks[hour] = rows(connection, 'SELECT start,end,status,raw_count FROM retention_archives '
                                    'WHERE start>=? AND start<?', (hour[:19], end[:19])) if has_archives else []
    model_rows = rows(connection, 'SELECT id,artifact_id,activated_at,artifact FROM gpt_coarse_model_revisions '
                       'WHERE activated_at<=? ORDER BY activated_at DESC,id DESC LIMIT 1', (stamp(datetime.now(timezone.utc)),))
    baseline = None
    if model_rows:
        baseline = model_rows[0]
        artifact = catalog.loads(baseline.pop('artifact'))
        baseline.update(kind=artifact['model'].get('kind'), n=artifact['model'].get('n'),
                        trained_at=artifact.get('trained_at'))
    result_rows = list(output.values())
    eligible = [r for r in result_rows if r['eligibility'] in ('strict', 'minute_approximation')]
    feature_summary = {}
    for feature in FEATURES:
        values = [r['features'][feature] for r in eligible]
        feature_summary[feature] = dict(min=min(values) if values else None, max=max(values) if values else None,
            unique=len(set(values)), states=dict(Counter(str(v) for v in values)) if feature in ITEMS else None)
    dates = sorted({r['observed_local'][:10] for r in result_rows})
    proposed_test_start = dates[-4] if len(dates) >= 8 else None
    summary = dict(total=len(result_rows), first_local=result_rows[0]['observed_local'],
        last_local=result_rows[-1]['observed_local'], dates=len(dates),
        pairing=dict(Counter(r['pairing'] for r in result_rows)),
        eligibility=dict(Counter(r['eligibility'] for r in result_rows)),
        feature_status=dict(Counter(e['status'] for r in result_rows for e in r['evidence'].values())),
        feature_summary=feature_summary, nearby_labels=nearby,
        proposed_holdout_start=proposed_test_start,
        proposed_development=dict(Counter(r['eligibility'] for r in eligible if proposed_test_start and r['observed_local'][:10] < proposed_test_start)),
        proposed_holdout=dict(Counter(r['eligibility'] for r in eligible if proposed_test_start and r['observed_local'][:10] >= proposed_test_start)))
    return dict(schema='gpt-assay-audit-v1', captured_at=stamp(datetime.now(timezone.utc)),
                max_assay_id=max(a['id'] for a in assays), baseline=baseline, summary=summary,
                archive_checks=archive_checks, dataset_sha256=digest(result_rows), rows=result_rows,
                limitations=['created_at is initial entry time, not report/revision availability',
                             'minute matches are approximate; no interpolation or future fills',
                             'label-review rows are retained but excluded from proposed fitting',
                             'not trained, scored, activated or deployed'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--max-id', type=int)
    args = parser.parse_args()
    if (args.output / 'audit.json').exists():
        raise SystemExit('Audit already exists; choose a new output directory')
    config = settings()
    if not config:
        raise SystemExit('Configured read-only bridge required; no local database fallback')
    def fetch_metadata(key):
        request = Request(config['server_url'] + '/api/v1/readonly-db/point-metadata/' + key,
                          headers={'X-DMCS-Read-Token': config['token']})
        with build_opener(ProxyHandler({})).open(request, timeout=8) as response:
            return json.load(response)
    catalog = Catalog(fetch=fetch_metadata, compact_writes=False)
    connection = connect(config, timeout=20)
    try:
        audit = inventory(connection, catalog, max_id=args.max_id)
    finally:
        connection.close()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
    fields = ['id', 'observed_local', 'created_local', 'value', 'pairing', 'eligibility', 'label_flags', *FEATURES]
    with (args.output / 'samples.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in audit['rows']:
            writer.writerow({**{k: row[k] for k in fields if k in row}, **row['features'],
                             'label_flags': ';'.join(row['label_flags'])})
    print(json.dumps(dict(output=str(args.output.resolve()), baseline=audit['baseline'],
                          sha256=audit['dataset_sha256'], summary=audit['summary']), ensure_ascii=False))


if __name__ == '__main__':
    main()
