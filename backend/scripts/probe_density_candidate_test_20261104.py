"""Probe only the local integration service; write probe targets no real route."""
from pathlib import Path
import argparse,datetime as dt,json,time,urllib.request,urllib.error,urllib.parse

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'output/density-optimization-20261104/integration'
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8000);parser.add_argument('--label',default='baseline');parser.add_argument('--candidate',action='store_true');parser.add_argument('--frozen-month',action='store_true');args=parser.parse_args()
    base=f'http://127.0.0.1:{args.port}'
    rows=[]
    query='limit=6'
    if args.frozen_month:
        period=json.loads((ROOT/'output/density-trend-analysis-month-20261104/raw/trend.json').read_text(encoding='utf-8'))['period']
        # Custom API ranges require minute precision; the frozen raw snapshot
        # stays unchanged. This request is explicitly rounded down <60 seconds.
        period={k:dt.datetime.fromisoformat(v).replace(second=0,microsecond=0).isoformat() for k,v in period.items()}
        query=urllib.parse.urlencode(dict(limit=720,start=period['start'],end=period['end']))
    probes=[('/api/v1/runtime-mode','GET'),('/','GET'),('/api/v1/integration/blocked-write-probe','POST'),('/api/v1/decision/evidence/density-trend?'+query,'GET')]
    if args.candidate:probes += [('/api/v1/decision/experimental-density','GET'),('/api/v1/decision/evidence/density-candidates?'+query,'GET')]
    for path,method in probes:
        at=time.monotonic();request=urllib.request.Request(base+path,data=b'{}' if method=='POST' else None,method=method,headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=25) as response:status=response.status;raw=response.read()
        except urllib.error.HTTPError as error:status=error.code;raw=error.read()
        row=dict(path=path,method=method,status=status,seconds=round(time.monotonic()-at,4),bytes=len(raw))
        if 'experimental-density' in path or 'density-candidates?' in path:
            data=json.loads(raw);kind='current' if 'experimental-density' in path else 'history'
            (OUT/f'api-{args.label}-{kind}.json').write_bytes(raw)
            row['body_summary']=dict(keys=list(data),experimental=data.get('experimental'),rows=len(data.get('rows',[])))
            if kind=='current':
                reading=(data.get('observation') or {}).get('actual_reading')
                assert reading is not None and all(k in reading for k in ['value','observed_at','usable','difference_from_setting','tolerance']),data
                row['body_summary']['actual_reading']=reading
        elif 'density-trend?' in path:
            data=json.loads(raw)
            assert status==200,data
            row['body_summary']={k:data.get(k) for k in ['policy','generated_at','period','archive_available']}
            row['body_summary'].update(suggestions=len(data.get('suggestions',[])),actions=len(data.get('actions',[])),ash=len(data.get('ash',[])))
            if args.frozen_month:
                (OUT/f'api-{args.label}-trend.json').write_bytes(raw)
                row['body_summary']['prior_fields_present']=all('prior_hourly_advice' in a and 'prior_difference' in a and 'prior_availability' in a.get('comparison',{}) for a in data['actions'])
                assert row['body_summary']['prior_fields_present']
        elif path!='/':row['body']=json.loads(raw)
        else:row['readonly_marker']=b'name="dmcs-readonly" content="true"' in raw
        rows.append(row)
    assert rows[0]['body']['mode']=='readonly-test'
    assert rows[1]['status']==200 and rows[1]['readonly_marker']
    assert rows[2]['status']==403
    assert rows[3]['status']==200 and rows[3]['body_summary']['policy']=='density-trend-v2'
    if args.candidate:assert all(row['status']==200 for row in rows[4:])
    result=dict(at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),base=base,results=rows,passed=True,
        frozen_month_requested=args.frozen_month,api_range_minute_aligned=period if args.frozen_month else None)
    (OUT/f'probe-{args.label}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
