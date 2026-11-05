"""Frozen, provenance-aware candidate replay. Does not open databases or network."""
from pathlib import Path
import argparse,collections,csv,datetime as dt,hashlib,json,sys
from copy import deepcopy

ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/'output/density-trend-analysis-month-20261104'
OUT=ROOT/'output/density-optimization-20261104/integration'
UTC=dt.timezone.utc
def stamp(value):return dt.datetime.fromisoformat(value).astimezone(UTC)
def number(value):return isinstance(value,(int,float)) and not isinstance(value,bool)
def flatten(cell):
    cell=deepcopy(cell or {});sample=cell.pop('sample',None) or {}
    return {**sample,**cell}
def identity(cell):return tuple(cell.get(k) for k in ['source_id','point_key','definition_id'])
def sign(value):return 0 if abs(value)<.0005 else 1 if value>0 else -1
def write_csv(name,rows):
    fields=list(dict.fromkeys(k for row in rows for k in row))
    with (OUT/name).open('w',encoding='utf-8-sig',newline='') as file:
        writer=csv.DictWriter(file,fieldnames=fields);writer.writeheader();writer.writerows(rows)

def inventory():
    manifest=json.loads((SOURCE/'manifest.json').read_text(encoding='utf-8'))
    for item in manifest['requests']:
        assert hashlib.sha256((SOURCE/item['file']).read_bytes()).hexdigest()==item['sha256'],item['file']
    source_candidates=json.loads((SOURCE/'candidate-hashes.json').read_text(encoding='utf-8'))
    for file,digest in source_candidates.items():
        assert hashlib.sha256((ROOT/file).read_bytes()).hexdigest()==digest,file
    t=json.loads((SOURCE/'raw/trend.json').read_text(encoding='utf-8'))
    live=sorted([s for s in t['suggestions'] if 'origin' not in s],key=lambda s:stamp(s['at']))
    replay=[s for s in t['suggestions'] if s.get('origin')=='advice-backfill-v1']
    unknown=[s for s in t['suggestions'] if 'origin' in s and s.get('origin')!='advice-backfill-v1']
    assert not unknown,'Unknown origin cannot be used as historical prior'
    final={s['apply_start']:s for s in live}
    actions=[]
    snapshots=[]
    for i,a in enumerate(t['actions']):
        detail=json.loads((SOURCE/f'raw/action-{i:02d}.json').read_text(encoding='utf-8'))
        before=detail['evidence']['before'];before_at=stamp(before['sampled_at'])
        hour=before_at.replace(minute=0,second=0,microsecond=0)
        candidates=[s for s in live if stamp(s['apply_start'])==hour and stamp(s['at'])<=before_at]
        prior=candidates[-1] if candidates else None
        actions.append(dict(index=i,action=a,detail=detail,prior=prior))
        for side in ['before','after']:
            snapshots.append(dict(index=i,side=side,snapshot=detail['evidence'][side]))
    coverage=[]
    for i,s in enumerate(live):
        evaluated_at=stamp(s['at'])
        known=[x for x in snapshots if stamp(x['snapshot']['decision_at'])<=evaluated_at and
            0<=(evaluated_at-stamp(x['snapshot']['sampled_at'])).total_seconds()<=30]
        context=max(known,key=lambda x:stamp(x['snapshot']['decision_at'])) if known else None
        coverage.append(dict(revision_index=i,generated_at=s['at'],apply_start=s['apply_start'],reference=s['value'],
            final_valid_hour=final[s['apply_start']]['at']==s['at'] and s['value'] is not None,
            known_snapshot_context=context is not None,event_index=context['index'] if context else None,
            side=context['side'] if context else None,
            snapshot_sampled_at=context['snapshot']['sampled_at'] if context else None,
            snapshot_decision_at=context['snapshot']['decision_at'] if context else None,
            age_seconds=(evaluated_at-stamp(context['snapshot']['sampled_at'])).total_seconds() if context else None,
            limitation='Quality/identity still evaluated by candidate; one fresh reading is not density stability' if context else 'No historical instantaneous MD/operating snapshot within 30s; cannot fill from final ash or later data'))
    stats=dict(source_window=t['period'],raw_hashes_verified=len(manifest['requests']),
        source_candidate_hashes_verified=len(source_candidates),
        live_revisions=len(live),live_final_hours=len(final),live_valid_final_hours=sum(s['value'] is not None for s in final.values()),
        backfill_revisions=len(replay),backfill_valid_hours=sum(s['value'] is not None for s in replay),unknown_origin=len(unknown),
        event_contexts=len(actions),events_with_live_prior=sum(a['prior'] is not None and a['prior']['value'] is not None for a in actions),
        revision_known_contexts=sum(r['known_snapshot_context'] for r in coverage),
        valid_revision_known_contexts=sum(r['known_snapshot_context'] and r['reference'] is not None for r in coverage),
        final_valid_hours_known_contexts=sum(r['known_snapshot_context'] and r['final_valid_hour'] for r in coverage),
        historical_live_reference_fields=sorted(set(k for s in live for k in s)),
        context_limitation='Reference archives lack instantaneous actual density and operating state; action snapshots retain these at sparse event endpoints. No final ash revision is used as historical prior.')
    return t,live,replay,final,actions,stats,coverage

def make_evidence(reference,snapshot,evaluated_at,actions,exclude_index=None):
    """Only frozen fields available at evaluated_at; never final ash or current action."""
    setting=flatten(snapshot['pair'].get('setpoint'));actual=flatten(snapshot['pair'].get('actual'))
    known=[a for a in actions if a['index']!=exclude_index and stamp(a['action']['at'])<=stamp(evaluated_at)]
    previous=max(known,key=lambda a:stamp(a['action']['at'])) if known else None
    latest=None
    if previous:
        raw=previous['detail']['evidence'];b=flatten(raw['before']['pair'].get('setpoint'));a=flatten(raw['after']['pair'].get('setpoint'))
        latest=dict(id=previous['action']['id'],before_at=raw['before']['sampled_at'],after_at=raw['after']['sampled_at'],
            before=b.get('value'),after=a.get('value'),continuous=bool(raw.get('continuous')),gap=raw['before'].get('gap'),
            identity_known=all(identity(setting)) and identity(b)==identity(a)==identity(setting))
    ref=deepcopy(reference or {})
    # All calls here use only rows whose origin field is absent, matching the
    # audited default live writer; no explicit null/unknown is reclassified.
    assert 'origin' not in ref
    ref.update(identity='online-default',generated_at=ref.get('at'),availability='before-observation')
    stopped=(snapshot.get('operating_state') or {}).get('stopped')
    return dict(evaluated_at=evaluated_at,mode='historical-replay',reference=ref,
        current_setpoint=setting,actual_density=actual,
        operating=dict(status='good' if stopped in (0,1) else 'missing',stopped=stopped,observed_at=snapshot['sampled_at']),
        identity_known=all(identity(setting)) and actual.get('source_id')==setting.get('source_id'),
        action_history_status='good',latest_action=latest)

def result_row(result,mode,reference,current,**metadata):
    candidate=result['candidate'];ash=reference.get('ash') or {}
    desired=(reference['value']-current) if number(reference.get('value')) and number(current) else None
    delta=candidate.get('delta');value=candidate.get('value')
    return dict(**metadata,provisional_mode=mode,ash_provisional=ash.get('provisional'),
        archived_ash=ash.get('value'),ash_start=ash.get('start'),ash_end=ash.get('end'),
        ash_band='low' if number(ash.get('value')) and ash['value']<8.5 else 'high' if number(ash.get('value')) and ash['value']>9 else 'in-band' if number(ash.get('value')) else 'unknown',
        archived_hourly_reference=reference.get('value'),current_setpoint=current,
        reference_target_delta=desired,candidate=value,candidate_delta=delta,status=candidate['status'],
        reasons=';'.join(candidate.get('reason_codes',[])),
        candidate_minus_reference=value-reference['value'] if number(value) and number(reference.get('value')) else None,
        desired_direction=sign(desired) if desired is not None else None,
        candidate_direction=sign(delta) if number(delta) else None,
        quality=result['quality'].get('level'),observation_state=result['observation'].get('state'),
        actual_response=result['observation'].get('actual_response'),outcome_evaluation=False)

def replay_candidate(t,live,replay,final,actions,stats,coverage):
    sys.path.insert(0,str(ROOT/'backend'))
    from app.gpt_density.candidate import CandidateParameters,evaluate_candidate
    parameters={mode:CandidateParameters(provisional_mode=mode) for mode in ['cautious','hold']}
    action_rows=[];action_results=[];inputs=[];revision_rows=[]
    for context in actions:
        ref=context['prior']
        if ref is None or ref.get('value') is None:continue
        before=context['detail']['evidence']['before']
        # The snapshot is computed at decision_at, normally ~5s after sampled_at.
        # The physical action may already have happened inside (before,after];
        # this is a conditional snapshot trial, not proof of a pre-action decision.
        evaluated_at=before['decision_at']
        assert stamp(ref['at'])<=stamp(before['sampled_at'])
        evidence=make_evidence(ref,before,evaluated_at,actions,exclude_index=context['index'])
        assert evidence['latest_action'] is None or evidence['latest_action']['id']!=context['action']['id']
        inputs.append(dict(event_index=context['index'],kind='before-observation-snapshot-conditional',
            sampled_at=before['sampled_at'],physical_action_time='unknown within observation interval',evidence=evidence))
        setting=before['pair']['setpoint']['value']
        for mode,p in parameters.items():
            result=evaluate_candidate(evidence,p)
            row=result_row(result,mode,ref,setting,event_index=context['index'],
                evaluated_at=evaluated_at,sampled_at=before['sampled_at'],
                reference_generated_at=ref['at'],current_action_id=context['action']['id'],
                latest_known_action_id=(evidence.get('latest_action') or {}).get('id'),
                operating_stopped=bool(before['operating_state']['stopped']),
                recorded_instant_suggestion=(before.get('suggestion') or {}).get('value'),
                recorded_expert_delta=context['action']['after']-context['action']['before'],
                physical_before_action_proven=False)
            action_rows.append(row);action_results.append(dict(event_index=context['index'],provisional_mode=mode,result=result))
            if result['candidate']['status']=='adjust':
                cap=p.provisional_step if ref['ash']['provisional'] else p.max_step
                assert abs(result['candidate']['delta'])<=cap+1e-8
    for c in coverage:
        ref=live[c['revision_index']]
        for mode,p in parameters.items():
            common=dict(revision_index=c['revision_index'],evaluated_at=ref['at'],
                apply_start=ref['apply_start'],final_valid_hour=c['final_valid_hour'],
                known_snapshot_context=c['known_snapshot_context'],context_event_index=c['event_index'],context_side=c['side'])
            if c['known_snapshot_context']:
                snapshot=actions[c['event_index']]['detail']['evidence'][c['side']]
                assert stamp(snapshot['decision_at'])<=stamp(ref['at'])
                evidence=make_evidence(ref,snapshot,ref['at'],actions)
                result=evaluate_candidate(evidence,p)
                row=result_row(result,mode,ref,snapshot['pair']['setpoint']['value'],**common,evaluator_run=True)
            else:
                row=dict(**common,provisional_mode=mode,evaluator_run=False,archived_hourly_reference=ref.get('value'),
                    ash_provisional=(ref.get('ash') or {}).get('provisional'),candidate=None,candidate_delta=None,
                    status='not-comparable',reasons='historical_instantaneous_context_missing',outcome_evaluation=False)
            revision_rows.append(row)
    profiles={}
    for mode in parameters:
        rows=[r for r in action_rows if r['provisional_mode']==mode]
        running=[r for r in rows if not r['operating_stopped']]
        profiles[mode]=dict(conditional_snapshot_trials=len(rows),running_trials=len(running),
            statuses=dict(collections.Counter(r['status'] for r in rows)),
            running_statuses=dict(collections.Counter(r['status'] for r in running)),
            reason_codes=dict(collections.Counter(r['reasons'] for r in rows)),
            by_provisional={str(flag):dict(n=sum(r['ash_provisional'] is flag for r in rows),statuses=dict(collections.Counter(r['status'] for r in rows if r['ash_provisional'] is flag))) for flag in [True,False]},
            zero_step_while_reference_differs=sum(number(r['reference_target_delta']) and sign(r['reference_target_delta'])!=0 and r['candidate_direction']==0 for r in rows),
            max_abs_candidate_step=max(abs(r['candidate_delta']) for r in rows if number(r.get('candidate_delta'))),
            revision_statuses=dict(collections.Counter(r['status'] for r in revision_rows if r['provisional_mode']==mode)),
            valid_final_hour_statuses=dict(collections.Counter(r['status'] for r in revision_rows if r['provisional_mode']==mode and r['final_valid_hour'])))
    summary=dict(context_inventory=stats,profiles=profiles,parameters={k:dict(vars(v)) for k,v in parameters.items()},
        explicit_trial_settings=True,fit_to_month=False,historical_parameter_version_comparison=False,
        inference='Rule/guard behavior only. No counterfactual ash or yield improvement has been established.',
        unavailable_full_context_policy='239/241 revisions are not evaluated; 89/90 valid final hours lack contemporaneous instantaneous context.',
        recorded_month_ash_distribution=json.loads((SOURCE/'summary.json').read_text(encoding='utf-8'))['recorded_hour_distribution'])
    assert len(action_rows)==24 and all(p['conditional_snapshot_trials']==12 and p['running_trials']==10 for p in profiles.values())
    assert len(revision_rows)==482 and sum(r['evaluator_run'] for r in revision_rows)==4
    assert sum(r['final_valid_hour'] for r in revision_rows)==180
    write_csv('replay-action-candidates.csv',action_rows);write_csv('replay-revision-results.csv',revision_rows)
    write_csv('replay-final-valid-hours.csv',[r for r in revision_rows if r['final_valid_hour']])
    write_csv('replay-backfill-background.csv',[dict(apply_start=s['apply_start'],generated_historical_time=s['at'],value=s['value'],status=s['status'],
        origin=s['origin'],role='offline-background-only-not-prior') for s in replay])
    (OUT/'replay-action-inputs.json').write_text(json.dumps(inputs,ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT/'replay-action-results.json').write_text(json.dumps(action_results,ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT/'replay-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    return summary

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--inventory-only',action='store_true');args=parser.parse_args()
    t,live,replay,final,actions,stats,coverage=inventory()
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/'replay-context-inventory.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2),encoding='utf-8')
    with (OUT/'replay-revision-context-coverage.csv').open('w',encoding='utf-8-sig',newline='') as file:
        writer=csv.DictWriter(file,fieldnames=list(coverage[0]));writer.writeheader();writer.writerows(coverage)
    if args.inventory_only:print(json.dumps(stats,ensure_ascii=False))
    else:
        summary=replay_candidate(t,live,replay,final,actions,stats,coverage)
        print(json.dumps(summary,ensure_ascii=False,indent=2))
        print('PASS: source 57 raw/13 candidate hashes, no backfill priors, no current action in prior context, prior timestamps, 241 revisions/90 final valid hours, 12 conditional snapshots, fixed trial caps.')

if __name__=='__main__':main()
