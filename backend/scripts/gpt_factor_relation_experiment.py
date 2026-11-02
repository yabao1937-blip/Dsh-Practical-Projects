"""Locked four-model historical relation study, using frozen phase1 files only.

No network, database writes, production activation, model selection or search.
Each raw-label training fence is shared by all lags and all four learned models.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'backend'))
from app.gpt_coarse.expert8 import predict as expert_predict

BJ = timezone(timedelta(hours=8))
LOCKED_PROTOCOL_SHA = '682ec72b65d358315947c9bbb7e819e8ff8e3ffad69900d4ac388fb780591ad7'
SPECS = {'M0': (), 'M1': ('level',), 'M2': ('coal_amount',), 'M3': ('level', 'coal_amount')}
METHODS = (*SPECS, 'empirical_raw', 'frozen_expert8')


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def parse(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp must include a timezone')
    return result


def midnight(day):
    return datetime.fromisoformat(day).replace(tzinfo=BJ)


def verify_manifest(directory, expected_sha, source_root):
    manifest_path = directory/'manifest.json'
    if file_hash(manifest_path) != expected_sha:
        raise ValueError('Frozen phase1 manifest changed')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    for name, entry in manifest['files'].items():
        path = directory/name
        if file_hash(path) != entry['sha256'] or path.stat().st_size != entry['bytes']:
            raise ValueError('Frozen phase1 artifact changed: ' + name)
    for name, expected in manifest['source_scripts'].items():
        if file_hash(source_root/name) != expected:
            raise ValueError('Frozen phase1 source changed: ' + name)
    return manifest


def primary_rows(rows, protocol):
    combo = ','.join(str(v) for v in protocol['primary_combination'])
    selected = [r for r in rows if r['common_valid'] and r['eligibility'] == 'strict'
                and r['fixed_factor_status']['strict'] and r['six_switch_combination'] == combo]
    if [r['assay_id'] for r in selected] != protocol['primary_assay_ids']:
        raise ValueError('Primary cohort does not match the locked assay IDs/order')
    for row in selected:
        if not all(row['levels'][str(lag)]['status'] == 'good' for lag in protocol['lags_seconds']):
            raise ValueError('A common cohort level candidate is missing')
    return selected


def available(rows, cutoff, protocol):
    replay = protocol['historical_replay']
    return [r for r in rows if replay['training_start'] <= r['sample_local_date'] < replay['replay_end_exclusive']
            and parse(r['sample_at']) < cutoff and parse(r['created_at']) < cutoff]


def fence(rows, protocol):
    rule = protocol['normal_fence']
    q1, q3 = np.quantile([r['ash'] for r in rows], [rule['q1'], rule['q3']], method=rule['quantile_method'])
    spread = max(float(q3-q1), rule['iqr_floor_pp'])
    return dict(lower=float(q1-rule['iqr_multiplier']*spread), upper=float(q3+rule['iqr_multiplier']*spread),
                q1=float(q1), q3=float(q3), raw_iqr=float(q3-q1), effective_iqr=spread)


def inside(value, bounds):
    return bounds['lower'] <= value <= bounds['upper']


def inputs(row, lag):
    return dict(row['fixed_features'], level=row['levels'][str(lag)]['value'])


def ranges(rows, predictors, lag):
    return {key: dict(minimum=min(inputs(r, lag)[key] for r in rows),
                      maximum=max(inputs(r, lag)[key] for r in rows),
                      unique=len({inputs(r, lag)[key] for r in rows})) for key in predictors}


def fit_model(training, normal, excluded, model, lag, protocol):
    predictors = SPECS[model]
    y = np.asarray([r['ash'] for r in normal], dtype=float)
    p = 1 + len(predictors)
    result = dict(model=model, predictors=list(predictors), status='unidentifiable', reason=None,
                  raw_training_ids=[r['assay_id'] for r in training],
                  normal_training_ids=[r['assay_id'] for r in normal],
                  excluded_training_ids=[r['assay_id'] for r in excluded],
                  raw_n=len(training), normal_n=len(normal), coefficient_count=p,
                  raw_training_ranges=ranges(training, predictors, lag) if training else {},
                  normal_training_ranges=ranges(normal, predictors, lag) if normal else {},
                  parameters=None, standardized_parameters=None, center=None, scale=None,
                  rank=None, singular_values=None, condition_number=None,
                  residual_degrees_of_freedom=len(normal)-p)
    numerical = protocol['numerical_policy']
    if model == 'M0':
        if len(normal) < numerical['constant_minimum_normal_labels']:
            result['reason'] = 'insufficient_normal_training_labels'
            return result
        result.update(status='ok', parameters={'intercept': float(np.median(y))}, rank=1,
                      standardized_parameters=[float(np.median(y))], center={}, scale={},
                      method='training_median')
        return result
    if len(normal) < p + numerical['minimum_residual_degrees_of_freedom']:
        result['reason'] = 'insufficient_normal_labels_for_residual_degree_of_freedom'
        return result
    values = np.asarray([[inputs(r, lag)[key] for key in predictors] for r in normal], dtype=float)
    spans = np.ptp(values, axis=0)
    if any(span <= numerical['minimum_predictor_span'] for span in spans):
        result['reason'] = 'insufficient_predictor_variation'
        return result
    center, scale = values.mean(axis=0), values.std(axis=0, ddof=0)
    design = np.column_stack((np.ones(len(normal)), (values-center)/scale))
    coef, _, rank, singular = np.linalg.lstsq(design, y, rcond=numerical['svd_rcond'])
    result.update(rank=int(rank), singular_values=[float(v) for v in singular],
                  condition_number=float(singular[0]/singular[-1]) if singular[-1] > 0 else None)
    if rank != p:
        result['reason'] = 'rank_deficient'
        return result
    slopes = coef[1:]/scale
    parameters = dict(intercept=float(coef[0]-np.dot(slopes, center)))
    parameters.update({key: float(value) for key, value in zip(predictors, slopes)})
    result.update(status='ok', method='centered_scaled_OLS', parameters=parameters,
                  standardized_parameters=[float(v) for v in coef],
                  center={key: float(v) for key, v in zip(predictors, center)},
                  scale={key: float(v) for key, v in zip(predictors, scale)},
                  level_slope_pp_per_10_level_pp=parameters.get('level', 0)*10 if 'level' in parameters else None,
                  coal_slope_pp_per_100_t_h=parameters.get('coal_amount', 0)*100 if 'coal_amount' in parameters else None)
    return result


def make_fold(training, stage, identifier, cutoff, protocol):
    minimum = protocol['historical_replay']['minimum_available_labels']
    bounds = fence(training, protocol) if training else None
    normal = [r for r in training if inside(r['ash'], bounds)] if bounds else []
    excluded = [r for r in training if not inside(r['ash'], bounds)] if bounds else []
    result = dict(id=identifier, stage=stage, cutoff=cutoff.isoformat() if cutoff else None,
                  raw_training_ids=[r['assay_id'] for r in training],
                  normal_training_ids=[r['assay_id'] for r in normal],
                  excluded_training_ids=[r['assay_id'] for r in excluded], fence=bounds,
                  raw_training=[dict(assay_id=r['assay_id'], ash=r['ash'], sample_at=r['sample_at'],
                                     created_at=r['created_at']) for r in training],
                  status='ok' if len(training) >= minimum else 'insufficient_available_labels', models={})
    for lag in protocol['lags_seconds']:
        result['models'][str(lag)] = {}
        for model in SPECS:
            fitted = fit_model(training, normal, excluded, model, lag, protocol)
            if len(training) < minimum:
                fitted.update(status='unidentifiable', reason='insufficient_available_labels',
                              parameters=None, standardized_parameters=None)
            result['models'][str(lag)][model] = fitted
    return result


def predict_model(model, values):
    if model['status'] != 'ok':
        return None
    if model['model'] == 'M0':
        return model['parameters']['intercept']
    terms = [(values[key]-model['center'][key])/model['scale'][key] for key in model['predictors']]
    return float(np.dot(np.asarray([1.0, *terms]), np.asarray(model['standardized_parameters'])))


def prediction(row, lag, method, fold, frozen, phase):
    values = inputs(row, lag)
    record = dict(stage=fold['stage'], phase=phase, fold=fold['id'], cutoff=fold['cutoff'],
                  assay_id=row['assay_id'], sample_at=row['sample_at'], created_at=row['created_at'],
                  date=row['sample_local_date'], lag_seconds=lag, method=method, actual=row['ash'],
                  level=values['level'], coal_amount=values['coal_amount'],
                  level_reading_id=row['levels'][str(lag)]['reading_id'],
                  normal=inside(row['ash'], fold['fence']) if fold['fence'] else None,
                  lower=fold['fence']['lower'] if fold['fence'] else None,
                  upper=fold['fence']['upper'] if fold['fence'] else None,
                  status='ok', reason=None, prediction=None, error=None, abs_error=None,
                  outside_training_range=False, outside_predictors=[], outside_expert_level_range=False)
    if fold['status'] != 'ok':
        record.update(status='rejected', reason=fold['status'])
        return record
    if method in SPECS:
        fitted = fold['models'][str(lag)][method]
        estimated = predict_model(fitted, values)
        if estimated is None:
            record.update(status='rejected', reason=fitted['reason'])
            return record
        outside = [key for key in fitted['predictors'] if not
                   fitted['normal_training_ranges'][key]['minimum'] <= values[key] <= fitted['normal_training_ranges'][key]['maximum']]
        record.update(outside_training_range=bool(outside), outside_predictors=outside)
    elif method == 'empirical_raw':
        estimated = 30.5-.3*values['level']
    elif method == 'frozen_expert8':
        estimated = expert_predict(values, frozen)
        record['outside_expert_level_range'] = not 25 <= values['level'] <= 75
        if estimated is None:
            record.update(status='rejected', reason='frozen_expert8_rejected')
            return record
    else:
        raise ValueError('Undeclared method')
    if not np.isfinite(estimated):
        raise ValueError('A supposedly usable fit produced a nonfinite prediction')
    record.update(prediction=float(estimated), error=float(estimated-row['ash']), abs_error=float(abs(estimated-row['ash'])))
    return record


def score(records, protocol):
    selected = [r for r in records if r['status'] == 'ok']
    n = len(selected)
    result = dict(n=n, assay_ids=[r['assay_id'] for r in selected], dates=len({r['date'] for r in selected}),
                  hit1=0, hit2=0, hit1_rate=None, hit2_rate=None, mae=None, bias=None,
                  median_abs_error=None, p90_abs_error=None, max_abs_error=None,
                  wilson95_lower=None, wilson95_upper=None)
    if not n:
        return result
    errors = np.asarray([r['error'] for r in selected])
    absolute = abs(errors)
    rule = protocol['metrics']
    hit1 = int(np.sum(absolute <= rule['primary_tolerance_pp']+rule['numerical_hit_epsilon_pp']))
    hit2 = int(np.sum(absolute <= rule['secondary_tolerance_pp']+rule['numerical_hit_epsilon_pp']))
    z, phat = 1.96, hit1/n
    center = (phat+z*z/(2*n))/(1+z*z/n)
    half = z*np.sqrt(phat*(1-phat)/n+z*z/(4*n*n))/(1+z*z/n)
    result.update(hit1=hit1, hit2=hit2, hit1_rate=phat, hit2_rate=hit2/n,
                  mae=float(np.mean(absolute)), bias=float(np.mean(errors)),
                  median_abs_error=float(np.median(absolute)), p90_abs_error=float(np.quantile(absolute, .9, method='linear')),
                  max_abs_error=float(max(absolute)), wilson95_lower=float(center-half), wilson95_upper=float(center+half))
    return result


def summaries(predictions, protocol):
    metrics, coverage, by_date = [], [], []
    groups = [('historical_fit', 'full_history'), ('historical_replay', 'development_historical_replay'),
              ('historical_replay', 'later_historical_replay_already_viewed'), ('historical_replay', 'all_replay')]
    for stage, phase in groups:
        records = [r for r in predictions if r['stage'] == stage and (phase == 'all_replay' or r['phase'] == phase)]
        candidate_sets = [{r['assay_id'] for r in records if r['lag_seconds'] == lag and r['method'] == method and r['status'] == 'ok'}
                          for lag in protocol['lags_seconds'] for method in METHODS]
        intersection = set.intersection(*candidate_sets)
        for lag in protocol['lags_seconds']:
            for method in METHODS:
                group = [r for r in records if r['lag_seconds'] == lag and r['method'] == method]
                accepted = [r for r in group if r['status'] == 'ok']
                coverage.append(dict(stage=stage, phase=phase, lag_seconds=lag, method=method,
                                     eligible_n=len(group), predicted_n=len(accepted), rejected_n=len(group)-len(accepted),
                                     eligible_ids=[r['assay_id'] for r in group],
                                     rejected_ids=[r['assay_id'] for r in group if r['status'] != 'ok'],
                                     reject_reasons=dict(Counter(r['reason'] for r in group if r['status'] != 'ok')),
                                     normal_eligible_n=sum(r['normal'] is True for r in group),
                                     normal_predicted_n=sum(r['normal'] is True for r in accepted),
                                     extrapolated_n=sum(r['outside_training_range'] for r in accepted),
                                     comparable_intersection_n=len(intersection), comparable_intersection_ids=sorted(intersection)))
                for comparison in ('native', 'common_intersection'):
                    comparable = group if comparison == 'native' else [r for r in group if r['assay_id'] in intersection]
                    for subset in ('all', 'normal'):
                        subset_rows = [r for r in comparable if subset == 'all' or r['normal']]
                        identity = dict(stage=stage, phase=phase, lag_seconds=lag, method=method, comparison=comparison, subset=subset)
                        metrics.append(dict(identity, **score(subset_rows, protocol)))
                        if comparison == 'native' and phase != 'all_replay':
                            for day in sorted({r['date'] for r in comparable}):
                                daily = [r for r in subset_rows if r['date'] == day]
                                by_date.append(dict(identity, date=day, **score(daily, protocol)))
    return metrics, coverage, by_date


def check_invariants(folds, predictions, metrics, coverage, protocol):
    lags = [str(lag) for lag in protocol['lags_seconds']]
    for fold in folds.values():
        for lag in lags:
            for model in SPECS:
                fitted = fold['models'][lag][model]
                for key in ('raw_training_ids', 'normal_training_ids', 'excluded_training_ids'):
                    assert fitted[key] == fold[key], ('training_ids_changed', fold['id'], lag, model, key)
        for method in ('M0', 'M2'):
            assert all(fold['models'][lag][method] == fold['models'][lags[0]][method] for lag in lags[1:]), ('lag_dependent_fit', fold['id'], method)
        if fold['stage'] == 'historical_replay':
            cutoff = parse(fold['cutoff'])
            assert all(parse(r['sample_at']) < cutoff and parse(r['created_at']) < cutoff for r in fold['raw_training']), ('time_leak', fold['id'])
    keys = ('status', 'reason', 'prediction', 'error', 'abs_error', 'normal', 'outside_training_range', 'outside_predictors')
    for stage in ('historical_fit', 'historical_replay'):
        for method in ('M0', 'M2'):
            groups = [[{key: r[key] for key in ('assay_id', *keys)} for r in predictions
                       if r['stage'] == stage and r['method'] == method and str(r['lag_seconds']) == lag] for lag in lags]
            assert all(group == groups[0] for group in groups[1:]), ('lag_dependent_predictions', stage, method)
    for collection, label in ((metrics, 'scores'), (coverage, 'coverage')):
        for method in ('M0', 'M2'):
            groups = [[{k: v for k, v in row.items() if k != 'lag_seconds'} for row in collection
                       if row['method'] == method and str(row['lag_seconds']) == lag] for lag in lags]
            assert all(group == groups[0] for group in groups[1:]), ('lag_dependent_'+label, method)
    return dict(shared_training_ids_and_fences=True, prior_day_double_timestamp_cutoffs=True,
                M0_M2_parameters_exact_across_lags=True, M0_M2_per_row_predictions_exact_across_lags=True,
                M0_M2_scores_exact_across_lags=True, M0_M2_coverage_exact_across_lags=True)


def descriptive(rows, lags):
    output = {}
    for combo in sorted({r['six_switch_combination'] for r in rows if r['common_valid'] and r['system_count'] == 2}):
        selected = [r for r in rows if r['common_valid'] and r['six_switch_combination'] == combo]
        output[combo] = dict(n=len(selected), assay_ids=[r['assay_id'] for r in selected],
                            dates=dict(sorted(Counter(r['sample_local_date'] for r in selected).items())),
                            coal_amount=dict(minimum=min(r['fixed_features']['coal_amount'] for r in selected),
                                             maximum=max(r['fixed_features']['coal_amount'] for r in selected)),
                            ash=dict(minimum=min(r['ash'] for r in selected), maximum=max(r['ash'] for r in selected)),
                            levels={str(lag): dict(minimum=min(r['levels'][str(lag)]['value'] for r in selected),
                                                   maximum=max(r['levels'][str(lag)]['value'] for r in selected)) for lag in lags},
                            fitted=False, borrowed_three_system_predictions=False,
                            limitation='Small combination-specific date-bound coverage; collect actual second-level sampling times with clock evidence and repeated days/ranges before independent modeling.')
    return output


def run(rows, protocol, frozen):
    primary = primary_rows(rows, protocol)
    folds, predictions = {}, []
    full = make_fold(primary, 'historical_fit', 'full_history', None, protocol)
    folds[full['id']] = full
    for row in primary:
        for lag in protocol['lags_seconds']:
            for method in METHODS:
                predictions.append(prediction(row, lag, method, full, frozen, 'full_history'))
    replay = protocol['historical_replay']
    targets = [r for r in primary if replay['evaluation_start'] <= r['sample_local_date'] < replay['replay_end_exclusive']]
    for day in sorted({r['sample_local_date'] for r in targets}):
        cutoff = midnight(day)
        training = available(primary, cutoff, protocol)
        fold = make_fold(training, 'historical_replay', day, cutoff, protocol)
        folds[day] = fold
        phase = 'development_historical_replay' if day < replay['selection_end_exclusive'] else 'later_historical_replay_already_viewed'
        for row in [r for r in targets if r['sample_local_date'] == day]:
            for lag in protocol['lags_seconds']:
                for method in METHODS:
                    predictions.append(prediction(row, lag, method, fold, frozen, phase))
    metrics, coverage, by_date = summaries(predictions, protocol)
    invariants = check_invariants(folds, predictions, metrics, coverage, protocol)
    return dict(schema='gpt-factor-relation-v1', status='offline_only_not_activated', primary_ids=protocol['primary_assay_ids'],
                folds=folds, predictions=predictions, metrics=metrics, coverage=coverage, per_date=by_date,
                two_system_descriptive=descriptive(rows, protocol['lags_seconds']), invariants=invariants,
                source_coverage=dict(phase1_total_n=len(rows), primary_common_n=len(primary),
                                     original_eligibility=dict(Counter(r['eligibility'] for r in rows)),
                                     primary_combination_source_eligibility=dict(Counter(r['eligibility'] for r in rows
                                         if r['six_switch_combination'] == ','.join(str(v) for v in protocol['primary_combination'])))))


def write_csv(path, records):
    if not records:
        path.write_text('', encoding='utf-8-sig')
        return
    keys = list(dict.fromkeys(key for row in records for key in row))
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows({key: json.dumps(value, ensure_ascii=False, allow_nan=False) if isinstance(value, (list, dict)) else value
                          for key, value in row.items()} for row in records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol', type=Path, default=ROOT/'docs/verification/gpt-factor-relation-experiment-20261102.protocol.json')
    parser.add_argument('--output', type=Path, default=ROOT/'output/gpt-factor-phase2-20261102')
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Preserve the first experiment result: output directory already exists')
    if file_hash(args.protocol) != LOCKED_PROTOCOL_SHA:
        raise ValueError('The approved pre-fit protocol changed')
    protocol = json.loads(args.protocol.read_text(encoding='utf-8'))
    phase1 = (ROOT/protocol['phase1_manifest']).parent
    manifest = verify_manifest(phase1, protocol['phase1_manifest_sha256'], ROOT)
    frozen_path = ROOT/protocol['baselines']['artifact']
    if file_hash(frozen_path) != protocol['baselines']['artifact_sha256']:
        raise ValueError('Frozen GPT artifact changed')
    baseline_source = ROOT/'backend/app/gpt_coarse/expert8.py'
    if file_hash(baseline_source) != protocol['baselines']['implementation_sha256']:
        raise ValueError('Frozen GPT reference implementation changed')
    all_rows = json.loads((phase1/'pairings.json').read_text(encoding='utf-8'))['rows']
    frozen = json.loads(frozen_path.read_text(encoding='utf-8'))['model']
    primary_rows(all_rows, protocol)
    args.output.mkdir(parents=True)
    (args.output/'protocol.json').write_bytes(args.protocol.read_bytes())
    write_json(args.output/'input_hashes.json', dict(protocol_sha256=LOCKED_PROTOCOL_SHA,
                phase1_manifest_sha256=protocol['phase1_manifest_sha256'], phase1_manifest=manifest,
                frozen_artifact_sha256=file_hash(frozen_path), baseline_source_sha256=file_hash(baseline_source)))
    result = run(all_rows, protocol, frozen)
    result.update(protocol_sha256=LOCKED_PROTOCOL_SHA, phase1_manifest_sha256=protocol['phase1_manifest_sha256'],
                  generated_at=datetime.now(timezone.utc).isoformat(), runtime=dict(python=sys.version, numpy=np.__version__),
                  source_sha256={str(p.relative_to(ROOT)).replace('\\', '/'): file_hash(p) for p in
                                 (Path(__file__), ROOT/'backend/tests/test_gpt_factor_relation_experiment.py', baseline_source)})
    verify_manifest(phase1, protocol['phase1_manifest_sha256'], ROOT)
    result['invariants']['phase1_files_unchanged_before_after'] = True
    write_json(args.output/'result.json', result)
    for name, records in (('predictions.csv', result['predictions']), ('metrics.csv', result['metrics']),
                          ('coverage.csv', result['coverage']), ('per_date.csv', result['per_date'])):
        write_csv(args.output/name, records)
    write_json(args.output/'two_system_descriptive.json', result['two_system_descriptive'])
    train_outliers = []
    by_id = {row['assay_id']: row for row in all_rows}
    for fold in result['folds'].values():
        for identifier in fold['excluded_training_ids']:
            row = by_id[identifier]
            train_outliers.append(dict(fold=fold['id'], stage=fold['stage'], cutoff=fold['cutoff'],
                                       assay_id=identifier, ash=row['ash'], sample_at=row['sample_at'], created_at=row['created_at'],
                                       lower=fold['fence']['lower'], upper=fold['fence']['upper'],
                                       direction='below_training_fence' if row['ash'] < fold['fence']['lower'] else 'above_training_fence',
                                       interpretation='statistical subgroup only, not evidence of a laboratory error'))
    write_csv(args.output/'training_nonregular.csv', train_outliers)
    write_json(args.output/'manifest.json', dict(schema='gpt-factor-phase2-manifest-v1',
               protocol_sha256=LOCKED_PROTOCOL_SHA, source_sha256=result['source_sha256'],
               files={p.name: dict(bytes=p.stat().st_size, sha256=file_hash(p)) for p in sorted(args.output.iterdir()) if p.is_file()}))
    # This final guard is deliberately after every artifact export, not just
    # after the numerical work, so successful completion proves the full freeze.
    verify_manifest(phase1, protocol['phase1_manifest_sha256'], ROOT)
    print(json.dumps(dict(output=str(args.output.resolve()), primary_n=len(result['primary_ids']),
                          prediction_records=len(result['predictions']), folds=len(result['folds']),
                          invariants=result['invariants']), ensure_ascii=False))


if __name__ == '__main__':
    main()
