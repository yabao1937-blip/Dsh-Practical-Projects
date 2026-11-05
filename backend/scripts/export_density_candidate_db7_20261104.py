"""DB7 offline chart/CSV export and bounded LOCAL API checks. Never queries production SQL."""
from pathlib import Path
import argparse,collections,csv,datetime as dt,hashlib,json,math,os,sys,time,urllib.request,urllib.error

ROOT=Path(__file__).resolve().parents[2]
DATA=ROOT/'output/density-optimization-db7-20261104'
OUT=DATA/'integration'
CST=dt.timezone(dt.timedelta(hours=8))
sys.dont_write_bytecode=True
def stamp(value):return dt.datetime.fromisoformat(value)
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def number(value):return isinstance(value,(int,float)) and not isinstance(value,bool)
def write_json(name,value):
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
def write_csv(name,rows):
    fields=list(dict.fromkeys(k for row in rows for k in row))
    with (OUT/name).open('w',encoding='utf-8-sig',newline='') as file:
        writer=csv.DictWriter(file,fieldnames=fields);writer.writeheader();writer.writerows(rows)

def audit_hour():
    raw=DATA/'raw';s=json.loads((raw/'samples-20261028T1200Z.json').read_text(encoding='utf-8'))
    r=json.loads((raw/'recalculated-20261028T1200Z.json').read_text(encoding='utf-8'))
    witness=json.loads((raw/'reading-ids-20261028T1200Z.json').read_text(encoding='utf-8'))
    original={x['id']:x for x in witness['rows']};checks=collections.Counter();future=[]
    for sample,result in zip(s,r):
        now=stamp(sample['sampled_at']);pair=sample['snapshot']['density_channels']['A304']
        actual,setting=pair['actual'],pair['setpoint'];ids=sample['provenance']['reading_ids']
        assert sample['slot']==result['slot']
        assert result['input_evidence']['setpoint']==setting and result['input_evidence']['actual']==actual
        assert actual['source_id']==setting['source_id'] and actual['batch_id']==setting['batch_id']
        assert actual['observed_at']==setting['observed_at']
        for kind,cell in pair.items():
            matches=[original[i] for i in ids if i in original and all(original[i].get(k)==cell.get(k) for k in ['source_id','point_key','batch_id'])]
            assert len(matches)==1,(sample['slot'],kind,matches)
            reading=matches[0];assert reading['sample']['definition_id']==cell['definition_id']
            assert reading['received_at']==cell['received_at']
            assert abs(reading['sample']['value']-cell['value'])<1e-9
            checks['density_cells_backed_by_original_primary_id']+=1
            for key in ['observed_at','received_at']:
                if stamp(cell[key])>now:future.append([sample['slot'],kind,key])
        checks['paired_samples']+=1
    assert len(s)==len(r)==60 and not witness['missing_ids'] and not future
    result=dict(passed=True,samples=60,unique_original_readings=len(original),checks=dict(checks),future_times=future,
        statuses=dict(collections.Counter(x['status'] for x in r)),reasons=dict(collections.Counter(c for x in r for c in x['reason_codes'])),
        source_hashes={f.name:digest(f) for f in [raw/'samples-20261028T1200Z.json',raw/'recalculated-20261028T1200Z.json',raw/'reading-ids-20261028T1200Z.json']})
    write_json('independent-one-hour-audit.json',result)
    return result

def hand_checks(rows,advice):
    by_time={p['at']:p for p in advice};chosen=[]
    for provisional in [False,True]:
        point=next(p for p in rows if p['status']=='adjust' and p['ash']['provisional'] is provisional)
        ref=by_time[point['source']['reference_known_at']]
        current=point['setting']['value'];cap=.005 if provisional else .010
        expected=round(current+min(cap,max(-cap,ref['value']-current)),3)
        assert abs(expected-point['value'])<1e-10
        assert stamp(ref['at'])<=stamp(point['at'])
        assert point['observation']['ash_fully_after_action'] and point['observation']['actual_reading']['usable']
        assert abs(point['observation']['actual_reading']['difference_from_setting'])<=.0050001
        chosen.append(dict(at=point['at'],kind='provisional-adjust' if provisional else 'non-provisional-adjust',current=current,
            archived_reference=ref['value'],reference_known_at=ref['at'],cap=cap,
            arithmetic='round(current + clamp(reference-current, -cap, +cap), 3)',expected=expected,actual=point['value'],passed=True,
            ash=point['ash'],latest_action_after=point['observation'].get('after_at')))
    for code in ['waiting_post_action','actual_not_following','target_band','actual_unavailable']:
        point=next(p for p in rows if code in p['reason_codes'])
        if code in ['waiting_post_action','actual_not_following','target_band']:
            assert point['value']==point['setting']['value'] and point['delta']==0
        else:assert point['value'] is None
        chosen.append(dict(at=point['at'],kind=code,value=point['value'],setting=point['setting']['value'],
            ash=point['ash'],observation=point['observation'],passed=True))
    write_json('representative-hand-checks.json',chosen)
    return chosen

def prior_artifacts():
    prior=ROOT/'output/density-optimization-20261104/integration/candidate-final-hashes.json'
    saved=json.loads(prior.read_text(encoding='utf-8'))
    immutable={p:h for p,h in saved.items() if p.startswith(('output/','docs/'))}
    for name,expected in immutable.items():assert digest(ROOT/name)==expected,name
    month=ROOT/'output/density-trend-analysis-month-20261104'
    month_hashes=json.loads((month/'candidate-hashes.json').read_text(encoding='utf-8'))
    for name,expected in month_hashes.items():assert digest(ROOT/name)==expected,name
    manifest=json.loads((month/'manifest.json').read_text(encoding='utf-8'))
    for item in manifest['requests']:assert digest(month/item['file'])==item['sha256'],item['file']
    result=dict(passed=True,opt1_fixed_output_files=len(immutable),monthly_candidate_files=len(month_hashes),monthly_raw_responses=len(manifest['requests']),
        note='OPT-1 mutable source paths are excluded: later authorized DB7 source edits do not rewrite its fixed artifacts.')
    write_json('prior-fixed-artifact-check.json',result)
    return result

def export():
    manifest=json.loads((DATA/'manifest.json').read_text(encoding='utf-8'))
    series=json.loads((DATA/'series.json').read_text(encoding='utf-8'))
    assert digest(DATA/'series.json')==manifest['series']['sha256']
    rows=[]
    for entry in manifest['artifacts']:
        if not entry['path'].startswith('recalculated-'):continue
        path=DATA/'raw'/entry['path'];assert digest(path)==entry['sha256'],entry['path']
        rows.extend(json.loads(path.read_text(encoding='utf-8')))
    rows.sort(key=lambda r:stamp(r['at']))
    start,end=map(stamp,[series['period']['start'],series['period']['end']])
    assert len(rows)==series['summary']['total_samples']==10068
    assert len({r['slot'] for r in rows})==len(rows)
    assert all(start<=stamp(r['at'])<end for r in rows)
    markers={p['slot']:p for p in series['suggestions'] if p.get('break_before')}
    true_gaps={b['slot']:(stamp(b['at'])-stamp(a['at'])).total_seconds() for a,b in zip(rows,rows[1:])
        if (stamp(b['at'])-stamp(a['at'])).total_seconds()>max(15,1.5*b['source']['cadence_seconds'])}
    assert set(markers)==set(true_gaps) and len(true_gaps)==7
    points=[]
    for row in rows:
        reading=row['observation'].get('actual_reading') or {}
        points.append(dict(at=row['at'],beijing_time=stamp(row['at']).astimezone(CST).isoformat(),slot=row['slot'],
            historical_setting=row['setting']['value'],setting_raw=row['setting'].get('raw_value'),
            new_next_step=row['value'],status=row['status'],delta=row['delta'],reason_codes=';'.join(row['reason_codes']),
            actual_usable_value=reading.get('value') if reading.get('usable') else None,
            actual_raw_value=reading.get('raw_value'),actual_usable=reading.get('usable'),
            ash_basis_start=row['ash'].get('start'),ash_basis_end=row['ash'].get('end'),ash_basis_value=row['ash'].get('value'),
            ash_basis_provisional=row['ash'].get('provisional'),reference_known_at=row['source'].get('reference_known_at'),
            context_key=row['source'].get('context_key'),break_before=row['slot'] in markers,
            sample_gap_seconds=true_gaps.get(row['slot']),physical_outcome_simulated=row['source']['physical_outcome_simulated']))
    write_csv('db7-new-density-full.csv',points)
    write_csv('db7-ash-production-background.csv',series['ash'])
    audit=audit_hour();checks=hand_checks(rows,json.loads((DATA/'raw/advice-revisions.json').read_text(encoding='utf-8')))
    protection=prior_artifacts()
    summary=dict(period=series['period'],period_cst={k:stamp(v).astimezone(CST).isoformat() for k,v in series['period'].items() if isinstance(v,str)},
        full_samples=len(rows),plot_samples_api=series['summary']['plot_samples'],statuses=series['summary']['statuses'],
        non_null_candidates=sum(number(r['value']) for r in rows),actual_gaps=len(true_gaps),max_gap_seconds=max(true_gaps.values()),
        representative_checks=len(checks),one_hour_original_primary_ids=audit['unique_original_readings'],
        series_sha256=digest(DATA/'series.json'),manifest_sha256=digest(DATA/'manifest.json'),
        root_replay_check_sha256=digest(DATA/'root-replay-check.json'),prior_artifact_check=protection,
        ash_background_role='retrospective-production-background; never used as the historical candidate input',
        physical_outcome_simulated=False)
    make_plot(points,series,summary)
    write_json('export-summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print('PASS offline DB7 export: full rows, actual gap markers, original primary IDs, representative arithmetic, prior artifacts unchanged.')

def make_plot(points,series,summary):
    os.environ['MPLCONFIGDIR']=str(OUT/'mpl-cache')
    sys.path.insert(0,str(ROOT/'output/density-trend-analysis-20261104/plot-deps'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.font_manager import FontProperties
    from matplotlib.ticker import MultipleLocator,FormatStrFormatter
    plt.rcParams['font.family']=FontProperties(fname='C:/Windows/Fonts/msyh.ttc').get_name()
    plt.rcParams['axes.unicode_minus']=False;plt.rcParams['svg.hashsalt']='density-db7-20261104'
    fig,axes=plt.subplots(2,1,figsize=(16,9),sharex=True,gridspec_kw={'height_ratios':[1.5,1]})
    times=[];settings=[];new=[];waiting=[]
    for p in points:
        x=stamp(p['at']).astimezone(CST)
        if p['break_before']:times.append(x);settings.append(math.nan);new.append(math.nan);waiting.append(math.nan)
        times.append(x);settings.append(p['historical_setting'] if number(p['historical_setting']) else math.nan)
        new.append(p['new_next_step'] if p['status'] in ('adjust','hold') and number(p['new_next_step']) else math.nan)
        waiting.append(p['new_next_step'] if p['status']=='wait' and number(p['new_next_step']) else math.nan)
    axes[0].plot(times,settings,color='#1768a8',lw=1.3,ls='--',drawstyle='steps-post',label='人工 A304 设定（历史实录）')
    axes[0].plot(times,new,color='#13836d',lw=1.7,drawstyle='steps-post',label='新版建议（调整 / 保持）')
    axes[0].plot(times,waiting,color='#b57a1f',lw=1.5,ls=':',drawstyle='steps-post',label='等待观察（保留当前设定）')
    axes[0].fill_between(times,settings,new,step='post',color='#32a78b',alpha=.10)
    vals=[v for v in settings+new+waiting if math.isfinite(v)]
    axes[0].set_ylim(min(vals)-.008,max(vals)+.008)
    axes[0].yaxis.set_major_locator(MultipleLocator(.010));axes[0].yaxis.set_major_formatter(FormatStrFormatter('%.3f'))
    axes[0].set_ylabel('密度 g/cm³');axes[0].legend(loc='upper left',ncol=3,fontsize=10)
    axes[0].set_title('近7天新版建议与人工设定直接对比',loc='left',fontsize=16,pad=30)
    stats=summary['statuses']
    axes[0].text(0,1.025,f"全量 {summary['full_samples']:,} 个真实时点｜调整 {stats['adjust']} · 等待 {stats['wait']} · 保持 {stats['hold']} · 不可用 {stats['unavailable']}（断线）",transform=axes[0].transAxes,fontsize=10,color='#555')
    axes[1].axhspan(8.5,9,color='#66ab80',alpha=.18,label='目标灰分 8.5–9.0%')
    ash=series['ash']
    for a,b in zip(ash,ash[1:]):
        if number(a.get('value')) and number(b.get('value')) and (stamp(b['at'])-stamp(a['at'])).total_seconds()<=3601:
            axes[1].plot([stamp(a['at'])+dt.timedelta(minutes=30),stamp(b['at'])+dt.timedelta(minutes=30)],[a['value'],b['value']],color='#a0a5aa',lw=.8,zorder=1)
    for flag,color,label in [(True,'#bc8023','现场结算·暂估'),(False,'#397c51','现场结算·非暂估')]:
        group=[a for a in ash if number(a.get('value')) and a['provisional'] is flag]
        axes[1].scatter([stamp(a['at'])+dt.timedelta(minutes=30) for a in group],[a['value'] for a in group],s=22,c=color,label=label,zorder=2)
    axes[1].set_ylabel('总灰分 %');axes[1].legend(loc='upper right',ncol=3,fontsize=9)
    axes[1].set_title('灰分按生产小时显示（查询时结算，仅作历史背景，不送回重算输入）',loc='left',fontsize=10)
    for ax in axes:
        ax.grid(alpha=.18);ax.set_xlim(stamp(series['period']['start']),stamp(series['period']['end']))
    axes[1].xaxis.set_major_locator(mdates.DayLocator(tz=CST));axes[1].xaxis.set_major_formatter(mdates.DateFormatter('%m-%d\n%H:%M',tz=CST))
    fig.text(.06,.02,f"北京时间 {summary['period_cst']['start'][:16]} 至 {summary['period_cst']['end'][:16]}（结束不含）｜7处真实采样缺口断开，最长375秒。新版未实际执行，不代表灰分/产率改善。",fontsize=9,color='#555')
    fig.tight_layout(rect=(0,.055,1,1));fig.savefig(OUT/'db7-new-density-trend.png',dpi=150);fig.savefig(OUT/'db7-new-density-trend.svg',metadata={'Date':None});plt.close(fig)

def probe():
    base='http://127.0.0.1:8000';results=[]
    endpoints=[('runtime','/api/v1/runtime-mode'),('current','/api/v1/decision/current'),('hourly','/api/v1/decision/hourly'),('series','/api/v1/decision/evidence/density-series?limit=168')]
    for name,path in endpoints:
        before=time.monotonic()
        with urllib.request.urlopen(base+path,timeout=40) as response:raw=response.read();status=response.status
        data=json.loads(raw);(OUT/f'api-db7-{name}.json').write_bytes(raw)
        if name=='runtime':assert data['mode']=='readonly-test'
        elif name in ['current','hourly']:assert data['density_policy']=='density-candidate-v1',list(data)
        elif name=='series':
            assert data['summary']['total_samples']==10068
            assert sum(bool(p.get('break_before')) for p in data['suggestions'])==7
            assert data['source']['physical_outcome_simulated'] is False
        results.append(dict(name=name,path=path,status=status,seconds=round(time.monotonic()-before,4),keys=list(data)))
    try:
        urllib.request.urlopen(urllib.request.Request(base+'/api/v1/integration/blocked-write-probe',data=b'{}',method='POST',headers={'Content-Type':'application/json'}),timeout=15)
        raise AssertionError('Write probe unexpectedly accepted')
    except urllib.error.HTTPError as e:assert e.code==403
    state=json.loads((OUT/'runtime-state.json').read_text(encoding='utf-8'))
    write_json('api-db7-probe-summary.json',dict(passed=True,results=results,write_probe_status=403,runtime=state))
    print('PASS new-only current/hourly, frozen7d series10068/gaps7, runtime readonly-test, write probe403; actualPID',state['pid'])

def render_saved():
    """Render existing CSV without recalculating the DB7 candidate or refetching data."""
    with (OUT/'db7-new-density-full.csv').open(encoding='utf-8-sig',newline='') as file:points=list(csv.DictReader(file))
    for point in points:
        for key in ['historical_setting','new_next_step']:
            point[key]=float(point[key]) if point[key] else None
        point['break_before']=point['break_before']=='True'
    summary=json.loads((OUT/'export-summary.json').read_text(encoding='utf-8'))
    series=json.loads((DATA/'series.json').read_text(encoding='utf-8'))
    assert len(points)==summary['full_samples'] and sum(p['break_before'] for p in points)==7
    make_plot(points,series,summary)
    print('PASS render-only: 10068 existing CSV rows; wait visually separate; no recalculation or network.')

def verify_saved_api():
    checks=[]
    for name in ['current','hourly']:
        data=json.loads((OUT/f'api-db7-{name}.json').read_text(encoding='utf-8'))
        value=data['density_suggestion']['candidate']['value']
        assert data['density_policy']==data['guidance']['policy']=='density-candidate-v1'
        assert value==data['suggested_density']['value']==data['cells']['suggested_density']['value']
        assert value==data['guidance']['rhoNew']==data['execution_guidance']['rhoNew']
        checks.append(dict(view=name,generated_at=data['generated_at'],policy=data['density_policy'],
            setting=data['density_suggestion']['setting']['value'],suggested=value,
            status=data['density_suggestion']['candidate']['status'],all_main_aliases_match=True))
    data=json.loads((OUT/'api-db7-series.json').read_text(encoding='utf-8'))
    frozen=json.loads((DATA/'series.json').read_text(encoding='utf-8'))
    assert data['period']==frozen['period'] and data['summary']==frozen['summary']
    points={p['slot']:p for p in data['suggestions']}
    assert set(points)=={p['slot'] for p in frozen['suggestions']}
    invalid=0
    for point in data['channels']['actual']:
        expected=points[point['slot']]['observation']['actual_reading']
        assert point['value']==(expected['value'] if expected['usable'] else None)
        invalid+=not expected['usable']
    result=dict(passed=True,main_views=checks,series_full_samples=data['summary']['total_samples'],
        series_plot_samples=len(points),series_period_matches_frozen=True,actual_invalid_plot_points=invalid,
        actual_plot_values_follow_usable_only=True,no_new_network_calls=True)
    write_json('saved-api-consistency-check.json',result)
    print('PASS saved current/hourly primary suggestion aliases, frozen series identity, and actual usable/null DTO; no new network.')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--probe-only',action='store_true');parser.add_argument('--render-only',action='store_true');parser.add_argument('--verify-saved-api',action='store_true');args=parser.parse_args()
    OUT.mkdir(parents=True,exist_ok=True)
    if args.probe_only:probe()
    elif args.render_only:render_saved()
    elif args.verify_saved_api:verify_saved_api()
    else:export()
