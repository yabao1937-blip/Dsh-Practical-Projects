"""RUNS config delta and host-local recovery; no real OPC or production DB."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

SCRIPT = Path(__file__).resolve().parents[1]/'scripts/host_runs_points/add_runs_points.py'
spec = importlib.util.spec_from_file_location('runs_update',SCRIPT)
runs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runs)


def configuration():
    return dict(enabled=True,source_id='kepserver-main',name='保留 points 字样："points": [ ]',
        endpoint='opc.tcp://127.0.0.1:1',security='None',allow_insecure=True,
        poll_seconds=5,history_interval_seconds=60,level_history_interval_seconds=30,
        points=[dict(key=key,name='旧 if_prod '+str(index),node_id=node,item_id=node.split(';s=',1)[1],
                     value_type='boolean') for index,(key,node) in enumerate(runs.OLD_POINTS.items())])


@pytest.mark.parametrize('bom',[False,True])
def test_delta_preserves_every_old_field_and_bytes_outside_points(bom):
    cfg = configuration()
    cfg.update(security='Basic256Sha256',client_certificate='原证书.der',client_key='private-key.pem',
               server_certificate='server.der',username_env='KEEP_USER_ENV',password_env='KEEP_PASSWORD_ENV')
    original = json.dumps(cfg,ensure_ascii=True,indent=2).replace('\n','\r\n').encode()
    if bom: original = b'\xef\xbb\xbf'+original
    updated,added = runs.plan(original)
    parsed = runs.strict_json(updated.decode('utf-8-sig'))
    assert len(added) == 2 and len(parsed['points']) == len(cfg['points'])+2
    assert parsed['points'][:len(cfg['points'])] == cfg['points']
    assert {k:v for k,v in parsed.items() if k != 'points'} == {k:v for k,v in cfg.items() if k != 'points'}
    old_text,new_text = original.decode('utf-8-sig'),updated.decode('utf-8-sig')
    close = runs.points_close(old_text)
    insert = close
    while old_text[insert-1].isspace(): insert -= 1
    assert new_text.startswith(old_text[:insert]) and new_text.endswith(old_text[insert:])
    assert updated.startswith(b'\xef\xbb\xbf') == bom
    assert all('scale' not in p for p in parsed['points'][:2])
    assert runs.plan(updated) == (updated,[])
    assert [p['key'] for p in runs.POINTS] == ['tag_881030838dd4d05ebcda','tag_04a29ab776cb07869148']


def test_partial_idempotence_only_appends_missing_point():
    cfg = configuration()
    cfg['points'].append(deepcopy(runs.POINTS[0]))
    updated,added = runs.plan(json.dumps(cfg).encode())
    assert added == [runs.POINTS[1]['key']]
    assert json.loads(updated)['points'] == cfg['points']+[runs.POINTS[1]]


@pytest.mark.parametrize('conflict',['key','node_id','item_id','definition','duplicate'])
def test_conflicting_existing_definition_is_never_overwritten(conflict):
    cfg = configuration()
    point = deepcopy(runs.POINTS[0])
    if conflict == 'key': point['node_id'] = 'ns=2;s=Other'; point['item_id'] = 'Other'
    if conflict == 'node_id': point['key'] = 'different'; point['item_id'] = 'Other'
    if conflict == 'item_id': point['key'] = 'different'; point['node_id'] = 'ns=2;s=Other'
    if conflict == 'definition': point['scale'] = 2
    cfg['points'].append(point)
    if conflict == 'duplicate': cfg['points'].append(dict(point,key='duplicate',node_id='ns=2;s=Other'))
    original = json.dumps(cfg).encode()
    with pytest.raises((runs.RunsUpdateError,ValueError)): runs.plan(original)
    assert json.loads(original) == cfg


@pytest.mark.parametrize('change',[{'history_interval_seconds':5},{'poll_seconds':10},
    {'level_history_interval_seconds':None},{'enabled':False},{'source_id':'other'}])
def test_unexpected_host_policy_is_preserved_and_rejected(change):
    cfg = configuration()
    cfg.update(change)
    with pytest.raises((runs.RunsUpdateError,ValueError)): runs.plan(json.dumps(cfg).encode())


def test_duplicate_json_keys_are_rejected_without_interpreting_secrets():
    with pytest.raises(runs.RunsUpdateError,match='duplicate keys'):
        runs.plan(b'{"points":[],"points":[]}')


@pytest.fixture
def host(tmp_path,monkeypatch):
    root,package = tmp_path/'host',tmp_path/'package'
    (root/'backend/config').mkdir(parents=True)
    (root/'backend/data').mkdir()
    (root/'backend/scripts').mkdir()
    package.mkdir()
    (root/'offline-install.json').write_text('{}')
    (root/'backend/scripts/start_collector.py').write_text('fixture launcher')
    (package/'StopOwnedCollector.ps1').write_text('fixture owned stop')
    config = root/'backend/config/kepserver.local.json'
    original = json.dumps(configuration(),ensure_ascii=False,indent=2).encode()
    config.write_bytes(original)
    database = root/'backend/data/dense_medium.db'
    database.write_bytes(b'production-data-placeholder')
    spool = root/'backend/data/telemetry-spool/kepserver-main/kept.json'
    spool.parent.mkdir(parents=True)
    spool.write_bytes(b'original ordinary spool')
    bindings = root/'backend/config/input-choices.json'
    bindings.write_bytes(b'frozen bindings')
    events = []
    control = SimpleNamespace(
        collector_configuration=lambda root:dict(source_id='kepserver-main',enabled=True),
        collector_status=lambda *a:dict(owner='old-owner',state='connected',
            lease_until=(datetime.now(timezone.utc)+timedelta(minutes=3)).isoformat(),
            last_sample_at=datetime.now(timezone.utc).isoformat()),
        powershell=lambda script,*a,**kw:events.append('stop:'+script.name),
        wait_collector_lease=lambda *a:events.append('lease'),
        collector_ready=lambda *a:events.append('ready'))
    monkeypatch.setattr(runs,'start_collector',lambda root:events.append('start-collector'))
    monkeypatch.setattr(runs,'verify_samples',lambda *a,**kw:events.append('samples') or {'fresh':True})
    return SimpleNamespace(root=root,package=package,config=config,original=original,control=control,
                           events=events,database=database,spool=spool,bindings=bindings)


def test_dry_run_creates_no_backups_locks_or_service_actions(host):
    result = runs.apply(host.root,host.package,host.control,dry_run=True)
    assert result['status'] == 'dry-run' and len(result['added']) == 2
    assert host.config.read_bytes() == host.original and host.events == []
    assert not (host.root/'backups').exists()
    assert not (host.root/'backend/data/.runs-points-update.lock').exists()


def test_success_only_restarts_collector_and_repeat_is_noop(host):
    result = runs.apply(host.root,host.package,host.control)
    assert result['status'] == 'installed'
    assert host.events == ['stop:StopOwnedCollector.ps1','lease','start-collector','ready','samples']
    assert len(json.loads(host.config.read_bytes())['points']) == 4
    assert next((host.root/'backups').glob('runs-points-*/kepserver.local.json')).read_bytes() == host.original
    assert host.database.read_bytes() == b'production-data-placeholder'
    assert host.spool.read_bytes() == b'original ordinary spool' and host.bindings.read_bytes() == b'frozen bindings'
    host.events.clear()
    assert runs.apply(host.root,host.package,host.control)['status'] == 'unchanged-verified'
    assert host.events == ['samples']


@pytest.mark.parametrize('invalid',['stopped','owner','lease','sample'])
def test_existing_definitions_do_not_report_success_when_collector_is_inactive(host,invalid):
    host.config.write_bytes(runs.plan(host.original)[0])
    status = host.control.collector_status()
    if invalid == 'stopped': status['state'] = 'stopped'
    if invalid == 'owner': status['owner'] = None
    if invalid == 'lease': status['lease_until'] = '2020-01-01T00:00:00+00:00'
    if invalid == 'sample': status['last_sample_at'] = '2020-01-01T00:00:00+00:00'
    host.control.collector_status = lambda *a:status
    before = host.config.read_bytes()
    with pytest.raises(runs.RunsUpdateError,match='health was not verified'):
        runs.apply(host.root,host.package,host.control)
    assert host.config.read_bytes() == before and host.events == []
    assert not (host.root/'backups').exists()


def test_existing_definitions_require_fresh_good_new_and_old_samples(host,monkeypatch):
    host.config.write_bytes(runs.plan(host.original)[0])
    monkeypatch.setattr(runs,'verify_samples',lambda *a,**kw:(_ for _ in ()).throw(runs.RunsUpdateError('fresh points missing')))
    before = host.config.read_bytes()
    with pytest.raises(runs.RunsUpdateError,match='fresh points missing'):
        runs.apply(host.root,host.package,host.control)
    assert host.config.read_bytes() == before and host.events == []


@pytest.mark.parametrize('failure',['ready','samples','before_replace','after_replace'])
def test_failure_restores_exact_old_config_and_healthy_original_collector(host,monkeypatch,failure):
    if failure in ('ready','samples'):
        original = host.control.collector_ready if failure == 'ready' else runs.verify_samples
        count = 0
        def fail_once(*args):
            nonlocal count
            count += 1
            if count == 1: raise TimeoutError('synthetic sample failure')
            return original(*args)
        if failure == 'ready': host.control.collector_ready = fail_once
        else: monkeypatch.setattr(runs,'verify_samples',fail_once)
    else:
        atomic = runs.atomic_config
        count = 0
        def interrupted(path,data):
            nonlocal count
            count += 1
            if count == 1:
                if failure == 'after_replace': atomic(path,data)
                raise OSError('synthetic atomic interruption')
            atomic(path,data)
        monkeypatch.setattr(runs,'atomic_config',interrupted)
    with pytest.raises((TimeoutError,OSError)): runs.apply(host.root,host.package,host.control)
    assert host.config.read_bytes() == host.original
    assert host.events[-2:] == ['start-collector','ready']
    assert host.database.read_bytes() == b'production-data-placeholder' and host.spool.read_bytes() == b'original ordinary spool'


def test_readonly_client_and_missing_helper_fail_before_any_write(host,monkeypatch):
    monkeypatch.setenv('DMCS_REMOTE_READONLY_CONFIG','do-not-read-or-override.json')
    with pytest.raises(runs.RunsUpdateError,match='Read-only client'): runs.apply(host.root,host.package,host.control)
    monkeypatch.delenv('DMCS_REMOTE_READONLY_CONFIG')
    (host.package/'StopOwnedCollector.ps1').unlink()
    with pytest.raises(runs.RunsUpdateError,match='missing'): runs.apply(host.root,host.package,host.control)
    assert host.config.read_bytes() == host.original and host.events == []


def test_concurrent_runs_installer_is_refused(host):
    with runs.update_lock(host.root):
        with pytest.raises(runs.RunsUpdateError,match='Another RUNS'): runs.apply(host.root,host.package,host.control)
    assert host.config.read_bytes() == host.original and host.events == []


def test_external_config_edit_is_not_overwritten_on_rollback(host):
    changed = json.loads(host.original)
    changed['name'] = 'external edit retained'
    external = json.dumps(changed).encode()
    once = False
    def lease(*args):
        nonlocal once
        if not once: host.config.write_bytes(external); once = True
        host.events.append('lease')
    host.control.wait_collector_lease = lease
    with pytest.raises(runs.RunsUpdateError,match='externally'): runs.apply(host.root,host.package,host.control)
    assert host.config.read_bytes() == external
    assert 'start-collector' not in host.events


@pytest.mark.parametrize('damage',['bad_status','noninteger_status','received','key'])
def test_sample_verification_checks_status_bits_and_row_identity(tmp_path,damage):
    import sqlite3
    root = tmp_path/'host'
    database = root/'backend/data/dense_medium.db'
    database.parent.mkdir(parents=True)
    at = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE telemetry_readings(source_id TEXT,point_key TEXT,received_at TEXT,sample TEXT)')
        db.execute('CREATE INDEX ix_telemetry_point_time ON telemetry_readings(source_id,point_key,received_at)')
        for index,(key,node) in enumerate({**runs.OLD_POINTS,**{p['key']:p['node_id'] for p in runs.POINTS}}.items()):
            sample = dict(key=key,node_id=node,received_at=at,quality='good',issue=None,value=False,status_code=0)
            if index == 0:
                if damage == 'bad_status': sample['status_code'] = 0x80000000
                if damage == 'noninteger_status': sample['status_code'] = '0'
                if damage == 'received': sample['received_at'] = '2020-01-01T00:00:00+00:00'
                if damage == 'key': sample['key'] = 'different'
            db.execute('INSERT INTO telemetry_readings VALUES(?,?,?,?)',(runs.SOURCE_ID,key,at,json.dumps(sample)))
    with pytest.raises(runs.RunsUpdateError,match='mismatch'):
        runs.verify_samples(root,at,timeout=.1,decode=json.loads)


def test_compact_samples_use_confirmed_runs_without_rewriting_if_prod_history(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    from app.hourly_brief.inputs import Inputs
    from app.telemetry.config import CollectorConfig
    from app.telemetry.derived import DESLIMING_MEMBERS,system_status_points
    from app.telemetry.domain import iso_time,normalize,utc_now
    from app.telemetry.metadata import install_codec
    from app.telemetry.repository import TelemetryRepository,latest_view
    root = tmp_path/'host'
    data = root/'backend/data'
    data.mkdir(parents=True)
    engine = create_engine('sqlite:///'+(data/'dense_medium.db').as_posix())
    install_codec(engine)
    Base.metadata.create_all(engine)
    cfg = CollectorConfig.model_validate_json(runs.plan(json.dumps(configuration()).encode())[0])
    repo = TelemetryRepository(sessionmaker(engine))
    repo.acquire(cfg,'owner')
    repo.state(cfg,'owner','connected')
    now = utc_now()
    values = [False,True,True,False]
    batch = dict(source_id=cfg.source_id,batch_id=uuid4().hex,
        samples=[normalize(point,value,0,now,now,now) for point,value in zip(cfg.points,values)])
    repo.persist(cfg,'owner',batch)
    assert len(runs.verify_samples(root,iso_time(now-timedelta(seconds=1)),timeout=.1)) == 4
    with repo.sessions() as db:
        items = latest_view(db,now=now)['items']
        runs_items = [item for item in items if item['key'] in {p['key'] for p in runs.POINTS}]
        expected = system_status_points(runs_items)
        assert system_status_points(items) == expected
        assert system_status_points(list(reversed(items))) == expected
        group = Inputs(db,now,authoritative=True).group(DESLIMING_MEMBERS)
        assert group['value'] == {'473':True,'474':False} and group['status'] == 'good'
        assert {c['point_key'] for c in group['components'].values()} == {p['key'] for p in runs.POINTS}
        old = [item for item in items if item['key'] in runs.OLD_POINTS]
        assert [item['sample']['value'] for item in old] == [False, True]
    engine.dispose()
