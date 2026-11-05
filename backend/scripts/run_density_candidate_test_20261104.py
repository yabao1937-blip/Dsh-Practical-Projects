"""Local-only integration server. Refuses to start without remote read-only mode."""
from pathlib import Path
import argparse,datetime as dt,json,os,socket,sys

BACKEND=Path(__file__).resolve().parents[1]
ROOT=BACKEND.parent
OUT=ROOT/'output/density-optimization-20261104/integration'

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--enable-candidate',action='store_true')
    parser.add_argument('--output-dir',type=Path,help='Evidence directory for a new task; prior artifacts stay untouched')
    args=parser.parse_args()
    output=(args.output_dir.resolve() if args.output_dir else OUT)
    approved=[(ROOT/'output'/name).resolve() for name in
        ['density-optimization-db7-20261104','density-chart-readability-20261104']]
    if args.output_dir and not any(output.is_relative_to(root) for root in approved):
        raise RuntimeError('Explicit evidence output must stay inside an approved task output directory')
    output.mkdir(parents=True,exist_ok=True)
    config=BACKEND/'data/remote-readonly.json'
    # An explicit config prevents isolated-db environment defaults from disabling
    # remote read-only mode. Auth material stays in its existing protected file.
    os.environ['DMCS_REMOTE_READONLY_CONFIG']=str(config)
    if args.enable_candidate:os.environ['DMCS_DENSITY_EXPERIMENT']='1'
    sys.path.insert(0,str(BACKEND));os.chdir(BACKEND)
    from remote_mode import settings
    remote=settings()
    if not remote:raise RuntimeError('Refusing production/write mode')
    baseline=dict(captured_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        test_host='127.0.0.1',selected_port=args.port,data_server=remote['server_url'],mode='readonly-test',
        existing_port_listeners={})
    for port in [8000,8010,8001,8765]:
        with socket.socket() as s:
            s.settimeout(.2)
            baseline['existing_port_listeners'][str(port)]=s.connect_ex(('127.0.0.1',port))==0
    if baseline['existing_port_listeners'].get(str(args.port)):
        raise RuntimeError(f'Port {args.port} already occupied; no process has been stopped')
    baseline_path=output/'baseline-runtime.json'
    if not baseline_path.exists():baseline_path.write_text(json.dumps(baseline,indent=2),encoding='utf-8')
    from app.config import REMOTE_READONLY
    assert REMOTE_READONLY and REMOTE_READONLY['server_url']==remote['server_url']
    state=dict(started_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),pid=os.getpid(),
        host='127.0.0.1',port=args.port,url=f'http://127.0.0.1:{args.port}/',mode='readonly-test',
        data_server=remote['server_url'],python=sys.executable,runner=str(Path(__file__).resolve()),
        arguments=sys.argv[1:],reload=False,collectors_enabled=False,production_database_writes=False)
    state['candidate_explicitly_enabled']=args.enable_candidate
    state['evidence_directory']=str(output)
    (output/'runtime-state.json').write_text(json.dumps(state,indent=2),encoding='utf-8')
    print(json.dumps(state),flush=True)
    import uvicorn
    uvicorn.run('app.main:app',host='127.0.0.1',port=args.port,reload=False,log_level='info')

if __name__=='__main__':main()
