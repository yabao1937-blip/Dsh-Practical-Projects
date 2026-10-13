"""Fixed temporal evaluation, with recent samples isolated from all selection."""
from . import coal_response as cr
from .comparison import split_rows


def configurations():
    items = [dict(name='EXPERT8', family='EXPERT8'), dict(name='training_mean', family='mean')]
    items += [dict(name=f'bias_h{h}_c{c}', family='bias', half_life=h, cap=c)
              for c in (2, 4) for h in (2, 7)]
    items += [dict(name=f'K{k}_p{p}', family=f'K{k}', k=k, prior=p)
              for k in (1, 2, 3) for p in (4, 1)]
    return items


def combine(sections, configs):
    return {c['name']: cr.metrics([p for s in sections for p in s[c['name']]['predictions']]) for c in configs}


def fold_metadata(training, validation, nominal):
    return dict(train_n=len(training), nominal_train_n=len(nominal),
                purged_ids=[r['id'] for r in nominal if r not in training],
                train_last_day=max(r['day'] for r in training),
                training_max_available_at=max(r['available_at'] for r in training),
                validation_first_time=min(r['sample_at'] for r in validation),
                validation_last_time=max(r['sample_at'] for r in validation),
                validation_n=len(validation))


def historical_evaluation(rows):
    """Accepts historical rows only, so recent-assay labels cannot tune these results."""
    configs = configurations()
    development, tail, nominal_folds = split_rows(rows)
    folds = []
    for nominal, validation in nominal_folds:
        training = cr.purge(nominal, validation)
        bundle = cr.fit_bundle(training, configs)
        folds.append(dict(metadata=fold_metadata(training, validation, nominal), bundle=bundle,
                          validation=validation))
    # Select the single-curve fallback on development folds before scoring the mixtures.
    first = [cr.evaluate(f['validation'], f['bundle'], configs, development, 'K1_p4') for f in folds]
    pooled = combine(first, configs)
    k1 = min((c for c in configs if c['family'] == 'K1'), key=lambda c: pooled[c['name']]['day_balanced_mae'])
    scores = [cr.evaluate(f['validation'], f['bundle'], configs, development, k1['name']) for f in folds]
    pooled = combine(scores, configs)
    selected = {family: min((c for c in configs if c['family'] == family),
                           key=lambda c: pooled[c['name']]['day_balanced_mae'])
                for family in ('EXPERT8', 'mean', 'bias', 'K1', 'K2', 'K3')}
    chosen = list(selected.values())
    training = cr.purge(development, tail)
    frozen = cr.fit_bundle(training, chosen)
    forward = cr.evaluate(tail, frozen, chosen, rows, k1['name'])
    full = cr.fit_bundle(rows, chosen)
    historical = cr.evaluate(rows, full, chosen, rows, k1['name'])
    evidence = {}
    for family in ('K2', 'K3'):
        name = selected[family]['name']
        gain = 1-pooled[name]['day_balanced_mae']/pooled[k1['name']]['day_balanced_mae']
        improved_folds = sum(s[name]['metrics']['day_balanced_mae'] < s[k1['name']]['metrics']['day_balanced_mae']-1e-10
                             for s in scores)
        better_forward = forward[name]['metrics']['day_balanced_mae'] < forward[k1['name']]['metrics']['day_balanced_mae']-1e-10
        supported = frozen['curves'][name]['identifiable'] and full['curves'][name]['identifiable']
        evidence[family] = dict(validation_gain_over_K1=gain, improved_folds=improved_folds,
                                forward_improved=better_forward, training_supported=supported,
                                meets_engineering_gate=gain >= .05 and improved_folds >= 2 and better_forward and supported)
    return dict(selected=selected, configurations=configs, tuning_validation=dict(
        pooled=pooled, folds=[dict(**f['metadata'], results=s, models=f['bundle']) for f, s in zip(folds, scores)]),
        forward_time_check=dict(**fold_metadata(training, tail, development), results=forward, model=frozen),
        historical_fit=dict(label='全177条拟合后回算；标签已参与拟合，绝不是向前检验', results=historical, model=full),
        complexity_evidence=evidence)


def recent_rows(joined):
    rows = []
    for r in joined['rows']:
        if not r['offline_complete']:
            continue
        sampled = cr.instant(r['sample_at'])
        available = sampled.replace(hour=0, minute=0, second=0, microsecond=0)+cr.timedelta(days=1)
        rows.append(dict(r['features'], id=f"recent:{r['source_row']}", day=sampled.date().isoformat(),
                         sample_at=sampled.isoformat(), available_at=available.isoformat(),
                         ash_content=r['ash_content'], source_row=r['source_row']))
    return rows


def recent_evaluation(joined, historical_rows, result):
    rows = recent_rows(joined)
    configs = list(result['selected'].values())
    return dict(label='5条分钟历史近似配对，已查看；不训练、不调参，不是盲测或严格现场验收',
                joined_summary=joined['summary'],
                results=cr.evaluate(rows, result['historical_fit']['model'], configs,
                                    historical_rows+rows, result['selected']['K1']['name']))
