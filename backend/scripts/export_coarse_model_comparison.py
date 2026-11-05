"""Export paired offline evidence to the fixed, read-only page artifact."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.coarse_model_comparison import (  # noqa: E402
    Comparison, EvaluationPool, Freeze, Model, PoolCount, Scope, SUMMARY_PATH, PHASES, digest, parse_time,
)

FUTURE_SYSTEMS = ('0','1','2','3','4','unknown')


def _score(rows, key, denominator=None):
    signed = [row[key] - row['actual'] for row in rows]
    errors = [abs(error) for error in signed]
    n = len(errors)
    hits = sum(e <= 1.0 for e in errors)
    sse = math.fsum(error * error for error in signed)
    average_actual = math.fsum(row['actual'] for row in rows) / n if n else None
    sst = math.fsum((row['actual'] - average_actual) ** 2 for row in rows) if n else 0
    return dict(mae=math.fsum(errors) / n if n else None,
                rmse=math.sqrt(sse / n) if n else None,
                bias=math.fsum(signed) / n if n else None,
                r2=1 - sse / sst if n >= 2 and sst > 0 else None,
                hit1=hits if n else None,n=n,hit1_rate=hits / n if n else None,
                eligible_n=denominator,coverage=n / denominator if denominator else None,
                date_range=dict(start=min(r['date'] for r in rows),end=max(r['date'] for r in rows)) if rows else None)


def _system_score(rows, key, cohorts=False, denominator=None, context=None, frozen_training=None, phase=''):
    train_min = [r['train_min_date'] for r in rows if r.get('train_min_date')]
    train_max = [r['train_max_date'] for r in rows if r.get('train_max_date')]
    train_range = dict(start=min(train_min) if train_min else None,end=max(train_max) if train_max else None) if train_min or train_max else None
    if not train_range and context and context.train_date_range:
        train_range = context.train_date_range.model_dump()
    if not train_range and key == 'candidate_prediction' and context and context.candidate_train_date_range:
        train_range = context.candidate_train_date_range.model_dump()
    if not train_range and key == 'candidate_prediction' and frozen_training:
        train_range = frozen_training
    if key == 'current_prediction':
        train_range = frozen_training
    fixed_policy = bool(rows) and all(row.get('training_mode') == 'fixed_policy' for row in rows)
    if fixed_policy or not rows:
        train_range = None
    if phase == 'future':
        if fixed_policy:
            training_note = '二系统固定14/15生产规则纯推理；零训练，无训练日期。'
        elif key == 'current_prediction':
            training_note = ('冻结现用EX8参数纯推理，实际生产RUNS口径；无重训。' if context and context.baseline_id == 'EX8_PRODUCTION_RUNS'
                             else '冻结现用模型/规则纯推理；输入口径以未来baseline身份为准，无重训。')
        else:
            training_note = '冻结候选参数纯推理；不重新训练、不调参。'
    else:
        training_note = '二系统固定14/15生产规则；零训练，无训练日期；属于规则历史回算。' if fixed_policy else (
            '冻结现用模型/规则，同池回算，不在各fold重新训练。' if key == 'current_prediction' else '候选训练日期；三系统TSCV各fold仅用更早日期训练。')
    return dict(**_score(rows, key, denominator.total_n if denominator else None),
                status='available' if rows else ('unavailable' if phase == 'future' and denominator and denominator.total_n else ('pending' if phase == 'future' else 'missing')),
                baseline_id=(context.baseline_id if key == 'current_prediction' else context.candidate_baseline_id) if phase == 'future' and context else None,
                baseline_label=(context.baseline_label if key == 'current_prediction' else context.candidate_baseline_label) if phase == 'future' and context else None,
                normal=_score([r for r in rows if r['normal']], key, denominator.normal_n if denominator else None),
                audited=_score([r for r in rows if r.get('cohort') == 'eligible'], key, denominator.audited_n if denominator else None) if cohorts else None,
                review=_score([r for r in rows if r.get('cohort') == 'label_review'], key, denominator.review_n if denominator else None) if cohorts else None,
                train_date_range=train_range,fold_count=0 if phase == 'future' else len({r['fold'] for r in rows if r.get('fold') and r.get('training_mode') != 'fixed_policy'}),
                fold_description=context.fold_description if context else '',
                training_note=training_note)


def build_summary(evidence_dir: Path) -> dict:
    manifest = json.loads((evidence_dir / 'evidence-manifest.json').read_text(encoding='utf-8'))
    for name in ('metadata.json', 'predictions.csv'):
        if hashlib.sha256((evidence_dir / name).read_bytes()).hexdigest() != manifest['files'][name]:
            raise ValueError(f'{name}: evidence hash mismatch')
    meta = json.loads((evidence_dir / 'metadata.json').read_text(encoding='utf-8'))
    if set(meta) - {'scope', 'freeze', 'models', 'valid_until', 'notes', 'evaluation_pools'}:
        raise ValueError('unexpected metadata fields')
    scope = Scope.model_validate(meta['scope'])
    freeze = Freeze.model_validate(meta['freeze'])
    pools = {phase: EvaluationPool.model_validate(value) for phase,value in meta.get('evaluation_pools', {}).items()}
    if set(pools) - set(PHASES):
        raise ValueError('unexpected evaluation pool phase')
    if 'history_all' not in pools:
        pools['history_all'] = EvaluationPool(total_n=scope.total_n,audited_n=scope.audited_n,review_n=scope.review_n,
            by_system={key:PoolCount(total_n=value.total_n,audited_n=value.audited_n,review_n=value.review_n) for key,value in scope.by_system.items()})
    rows = {phase: [] for phase in PHASES}
    seen = set()
    with (evidence_dir / 'predictions.csv').open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        required = {'phase', 'record_id', 'date', 'system_count', 'actual', 'current_prediction', 'candidate_prediction', 'normal'}
        if not required <= set(reader.fieldnames or []):
            raise ValueError('prediction CSV lacks required columns')
        for row in reader:
            phase, record_id = row['phase'], row['record_id']
            if phase not in rows or not record_id or (phase, record_id) in seen:
                raise ValueError('unexpected phase, missing identity or duplicate target')
            seen.add((phase, record_id))
            target_date = date.fromisoformat(row['date'])
            if row['system_count'] not in (FUTURE_SYSTEMS if phase == 'future' else ('2', '3')):
                raise ValueError('unsupported system count')
            training_mode = row.get('training_mode') or 'learned'
            if training_mode not in ('learned', 'fixed_policy'):
                raise ValueError('unknown training mode')
            row['training_mode'] = training_mode
            if training_mode == 'fixed_policy' and row['system_count'] != '2':
                raise ValueError('only explicit two-system fixed policy may bypass training dates')
            if phase == 'history_forward':
                if not row.get('fold'):
                    raise ValueError('forward requires an evaluation fold or explicit fixed policy')
                if training_mode != 'fixed_policy' and date.fromisoformat(row.get('train_max_date', '')) >= target_date:
                    raise ValueError('forward fold must train on earlier dates only')
                if training_mode != 'fixed_policy' and row.get('train_min_date') and date.fromisoformat(row['train_min_date']) > date.fromisoformat(row['train_max_date']):
                    raise ValueError('forward training range is reversed')
            if training_mode == 'fixed_policy' and (row.get('train_min_date') or row.get('train_max_date')):
                raise ValueError('fixed policy must not claim training dates')
            if phase == 'history_all' and row.get('cohort') not in ('eligible', 'label_review'):
                raise ValueError('history_all requires eligible or label_review cohort')
            if phase == 'future':
                if freeze.status != 'accepted' or target_date <= parse_time(freeze.sealed_at).date():
                    raise ValueError('future targets must follow genuine accepted freeze')
            if row['normal'].lower() not in ('true', 'false'):
                raise ValueError('normal flag must use audited training fence')
            row['normal'] = row['normal'].lower() == 'true'
            for key in ('actual', 'current_prediction', 'candidate_prediction'):
                if phase == 'future' and key != 'actual' and not str(row.get(key) or '').strip():
                    reason_key = key.replace('_prediction', '_reason')
                    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', row.get(reason_key) or ''):
                        raise ValueError('unavailable future prediction requires a safe reason code')
                    row[key] = None
                    continue
                row[key] = float(row[key])
                if not math.isfinite(row[key]):
                    raise ValueError('paired target requires two finite predictions and actual')
            if training_mode == 'fixed_policy':
                values = [row[key] for key in ('current_prediction','candidate_prediction') if row[key] is not None]
                if any(value not in (14.,15.) for value in values) or len(set(values)) > 1:
                    raise ValueError('fixed-policy exemption requires unchanged actual 14/15 rule predictions')
            if row['system_count'] == 'unknown' and any(row[key] is not None for key in ('current_prediction','candidate_prediction')):
                raise ValueError('unknown system future rows require unavailable predictions')
            rows[phase].append(row)
    if len(rows['history_all']) != scope.evaluable_n:
        raise ValueError('history_all must cover every legal paired target, including review diagnostics')
    if 'future' not in pools and rows['future']:
        pools['future'] = EvaluationPool(total_n=len(rows['future']),by_system={
            system:PoolCount(total_n=sum(row['system_count'] == system for row in rows['future']))
            for system in FUTURE_SYSTEMS if any(row['system_count'] == system for row in rows['future'])})
    future_context = pools.get('future')
    if future_context and future_context.total_n != len(rows['future']):
        raise ValueError('future labelled pool must include every revealed target, including prediction rejection')
    if rows['future'] and (not future_context or not future_context.baseline_id):
        raise ValueError('future evidence requires explicit baseline identity')
    if future_context:
        for system in FUTURE_SYSTEMS:
            count = sum(row['system_count'] == system for row in rows['future'])
            if system in future_context.by_system and future_context.by_system[system].total_n != count:
                raise ValueError('future system labelled denominator must match all revealed CSV targets')
            if count and system not in future_context.by_system:
                future_context.by_system[system] = PoolCount(total_n=count)
    models = {}
    for role in ('current', 'candidate'):
        model = Model.model_validate(meta['models'][role]).model_dump()
        for phase in PHASES:
            targets = rows[phase]
            pool = [row for row in targets if row[role + '_prediction'] is not None]
            context = pools.get(phase)
            denominator = context if targets or phase != 'future' else None
            model['metrics'][phase] = dict(
                **_system_score(pool, role + '_prediction', phase == 'history_all', denominator, context, model['training_date_range'],phase),
                note=meta.get('notes', {}).get(phase, ''),
                by_system={system: _system_score([r for r in pool if r['system_count'] == system], role + '_prediction', phase == 'history_all',
                           context.by_system.get(system) if context else None,context,model['training_date_range'],phase)
                           for system in FUTURE_SYSTEMS if any(r['system_count'] == system for r in targets)},
            )
            if phase == 'future':
                metric = model['metrics'][phase]
                pending = future_context.pending_label_n if future_context else 0
                attempted = len(targets) + pending
                available = (future_context.prediction_available_n.get(role) if future_context else None)
                if available is None and not pending and attempted:
                    available = len(pool)
                if available is not None and available < len(pool):
                    raise ValueError('all forecast availability count cannot be smaller than scored predictions')
                metric.update(labelled_n=len(targets),pending_label_n=pending,attempted_n=attempted,
                              prediction_n=available,prediction_coverage=available / attempted if available is not None and attempted else None,
                              reason_counts=dict(Counter(row[role + '_reason'] for row in targets if row[role + '_prediction'] is None)))
                for system, result in metric['by_system'].items():
                    result['reason_counts'] = dict(Counter(row[role + '_reason'] for row in targets if row['system_count'] == system and row[role + '_prediction'] is None))
        models[role] = model
    def common_score(targets):
        common = [row for row in targets if row['current_prediction'] is not None and row['candidate_prediction'] is not None]
        denominator = len(targets) or None
        return dict(status='available' if common else ('unavailable' if targets else 'pending'),
                    n=len(common),eligible_n=denominator,coverage=len(common)/denominator if denominator else None,
                    current=_score(common,'current_prediction',denominator),candidate=_score(common,'candidate_prediction',denominator))
    future_comparison = dict(**common_score(rows['future']),
        pending_label_n=future_context.pending_label_n if future_context else 0,
        baseline_id=future_context.baseline_id if future_context else None,
        baseline_label=future_context.baseline_label if future_context else None,
        candidate_baseline_id=future_context.candidate_baseline_id if future_context else None,
        candidate_baseline_label=future_context.candidate_baseline_label if future_context else None,
        by_system={system:common_score([row for row in rows['future'] if row['system_count'] == system])
                   for system in FUTURE_SYSTEMS if any(row['system_count'] == system for row in rows['future'])})
    payload = dict(schema_version=1, generated_at=datetime.now(timezone.utc).isoformat(),
                   valid_until=meta.get('valid_until'), status='ready' if freeze.status == 'accepted' else 'research',
                   reason='' if freeze.status == 'accepted' else '候选模型尚未独立验收封存；历史结果为开发诊断。',
                   scope=scope.model_dump(), freeze=freeze.model_dump(), models=models, future_comparison=future_comparison,artifact_sha256=None)
    payload = Comparison.model_validate(payload).model_dump()
    payload['artifact_sha256'] = digest(payload)
    return payload


def export(evidence_dir: Path) -> dict:
    payload = build_summary(evidence_dir)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SUMMARY_PATH.with_suffix('.json.tmp')
    temporary.write_text(encoded, encoding='utf-8')
    os.replace(temporary, SUMMARY_PATH)
    (evidence_dir / 'metrics-recomputed.json').write_text(encoded, encoding='utf-8')
    return payload


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = export(args.evidence_dir)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f'Comparison export rejected: {exc}\n')
    print(json.dumps({'status': result['status'], 'artifact_sha256': result['artifact_sha256'],
                      'history_n': result['models']['candidate']['metrics']['history_all']['n']}))
