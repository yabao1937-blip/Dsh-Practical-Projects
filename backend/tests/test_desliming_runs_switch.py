"""Confirmed RUNS semantics, original historical identities and isolated host recovery."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.hourly_brief.inputs import Inputs
from app.models import DecisionSourceRevision, Setting
from app.services.decision_sources import identity, shared_snapshot
from app.telemetry.config import CollectorConfig, PointConfig
from app.telemetry.derived import (DESLIMING_MEMBERS, LEGACY_DESLIMING_MEMBERS,
    DESLIMING_CUTOVER_SETTING, DESLIMING_RUNS_VERSION)
from app.telemetry.domain import definition_id, iso_time, normalize
from app.telemetry.metadata import install_codec
from app.telemetry.models import TelemetryPoint, TelemetryReading
from app.telemetry.repository import TelemetryRepository, latest_view
from test_decision_sources import sessions, seeded, HOUR

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT/'backend/scripts/host_runs_switch/switch_runs.py'
spec = importlib.util.spec_from_file_location('switch_desliming_runs', SCRIPT)
switch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(switch)
add_spec = importlib.util.spec_from_file_location('add_runs_fixture', ROOT/'backend/scripts/host_runs_points/add_runs_points.py')
addition = importlib.util.module_from_spec(add_spec)
add_spec.loader.exec_module(addition)


def retired_points(db, at):
    for (key, item), value in zip(switch.OLD_POINTS.items(), (False, True)):
        cfg = PointConfig(key=key, name=item, node_id='ns=2;s='+item, item_id=item, value_type='boolean')
        sample = normalize(cfg, value, 0, at, at, at)
        db.add(TelemetryPoint(source_id=switch.SOURCE_ID, key=key, config=cfg.model_dump(), enabled=False, latest=None))
        db.add(TelemetryReading(source_id=switch.SOURCE_ID, point_key=key, received_at=iso_time(at),
            batch_id='old-raw-batch', sample=sample))


def test_live_card_business_group_and_eight_factor_inputs_use_runs_only(sessions, seeded):
    with sessions.begin() as db:
        retired_points(db, HOUR-timedelta(seconds=2))
    with sessions() as db:
        snapshot = shared_snapshot(db, HOUR, live=True)
        assert snapshot['cells']['desliming']['value'] == {'473':True, '474':False}
        for key, item in [('desliming473', DESLIMING_MEMBERS[0][1]), ('desliming474', DESLIMING_MEMBERS[1][1])]:
            assert snapshot['inputs'][key]['item_id'] == item
        assert [snapshot['inputs'][key]['value'] for key in ('desliming473','desliming474')] == [1, 0]
        assert snapshot['input_derivations']['desliming']['version'] == DESLIMING_RUNS_VERSION
        group = next(p for p in latest_view(db, now=HOUR)['derived_items'] if p['key'] == 'desliming_status')
        assert group['sample']['value'] == {'473':True, '474':False}


@pytest.mark.parametrize('live,realtime,after', [(False,False,False), (True,False,False), (False,True,False), (False,False,True)])
def test_only_pre_cutover_nonlive_nonrealtime_reads_reopen_exact_legacy_points(sessions, seeded, live, realtime, after):
    old_at = HOUR-timedelta(seconds=2)
    cutover = HOUR-timedelta(seconds=1)
    with sessions.begin() as db:
        retired_points(db, old_at)
        db.add(Setting(key=DESLIMING_CUTOVER_SETTING,
            value=dict(version=DESLIMING_RUNS_VERSION, source_id=switch.SOURCE_ID, effective_at=iso_time(cutover)),
            updated_at=(HOUR+timedelta(days=1)).replace(tzinfo=None)))
    when = HOUR if after else old_at
    with sessions() as db:
        inputs = Inputs(db, when, live=live, realtime=realtime, authoritative=True)
        group = inputs.group(DESLIMING_MEMBERS)
        legacy = not live and not realtime and not after
        assert inputs.legacy_desliming is legacy
        assert group['value'] == ({'473':False, '474':True} if legacy else {'473':True, '474':False})
        assert {c['item_id'] for c in group['components'].values()} == {p[1] for p in
            (LEGACY_DESLIMING_MEMBERS if legacy else DESLIMING_MEMBERS)}


def test_missing_or_bad_runs_cannot_fall_back_to_recent_good_retired_samples(sessions, seeded):
    with sessions.begin() as db:
        retired_points(db, HOUR-timedelta(seconds=2))
        for item in (p[1] for p in DESLIMING_MEMBERS):
            p = db.scalar(select(TelemetryPoint).where(TelemetryPoint.config['item_id'].as_string() == item))
            p.enabled = False
    with sessions() as db:
        result = shared_snapshot(db, HOUR, live=True)
        assert result['cells']['desliming']['value'] is None
        assert result['inputs']['desliming473']['value'] is None
        assert result['suggested_density']['value'] is None
        assert result['input_derivations']['desliming']['version'] == DESLIMING_RUNS_VERSION


def test_cross_cutover_hour_refuses_to_mix_frozen_if_prod_and_runs_states(sessions, seeded):
    from app.decision_hourly.history import historical_cells
    with sessions.begin() as db:
        retired_points(db, HOUR-timedelta(minutes=1))
        db.add(Setting(key=DESLIMING_CUTOVER_SETTING,
            value=dict(version=DESLIMING_RUNS_VERSION, source_id=switch.SOURCE_ID,
                effective_at=iso_time(HOUR-timedelta(minutes=30)))))
    with sessions() as db:
        cells, _, _ = historical_cells(db, HOUR-timedelta(hours=1), HOUR, [])
        assert cells['desliming']['status'] == 'missing'
        assert cells['desliming']['value'] == {'473':None, '474':None}
        assert 'if_prod' in cells['desliming']['reason']


def test_wrong_source_marker_never_opens_retired_points(sessions, seeded):
    with sessions.begin() as db:
        retired_points(db, HOUR-timedelta(seconds=2))
        db.add(Setting(key=DESLIMING_CUTOVER_SETTING, value=dict(version=DESLIMING_RUNS_VERSION,
            source_id='another-source', effective_at=iso_time(HOUR+timedelta(seconds=1)))))
    with sessions() as db:
        inputs = Inputs(db, HOUR, authoritative=True)
        assert not inputs.legacy_desliming
        assert inputs.group(DESLIMING_MEMBERS)['value'] == {'473':True, '474':False}


def test_missing_samples_still_separate_pre_and_post_cutover_contexts(sessions):
    from app.decision_hourly.service import context_definition
    from app.decision_hourly.statistics import digest
    with sessions.begin() as db:
        db.add(Setting(key=DESLIMING_CUTOVER_SETTING, value=dict(version=DESLIMING_RUNS_VERSION,
            source_id=switch.SOURCE_ID, effective_at=iso_time(HOUR-timedelta(seconds=1)))))
    with sessions() as db:
        old = shared_snapshot(db, HOUR-timedelta(seconds=2), live=False)
        new = shared_snapshot(db, HOUR, live=False)
        assert old['inputs']['desliming473']['value'] is new['inputs']['desliming473']['value'] is None
        old_context, new_context = context_definition(old, {}), context_definition(new, {})
        assert 'desliming' not in old_context['input_derivations']
        assert new_context['input_derivations']['desliming']['version'] == DESLIMING_RUNS_VERSION
        assert digest(old_context) != digest(new_context)


def fixture_config():
    points = [PointConfig(key=key, name=item, node_id='ns=2;s='+item, item_id=item, value_type='boolean').model_dump()
        for key,item in switch.OLD_POINTS.items()]
    points.extend(deepcopy(addition.POINTS))
    points.append(PointConfig(key='unrelated_478', name='keep /if_prod', node_id='ns=2;s=Other.478/if_prod',
        item_id='Other.478/if_prod', value_type='boolean').model_dump())
    return dict(enabled=True, source_id=switch.SOURCE_ID, name='host', endpoint='opc.tcp://localhost:1',
        security='None', allow_insecure=True, poll_seconds=5, history_interval_seconds=60,
        level_history_interval_seconds=30, points=points)


@pytest.mark.parametrize('bom', [False,True])
def test_config_delta_preserves_all_remaining_definitions_and_nonpoints_bytes(bom):
    cfg = fixture_config()
    cfg.update(security='Basic256Sha256', client_certificate='keep-client.der', client_key='keep-client.pem',
        server_certificate='keep-server.der', username_env='KEEP_THIS_ENV', password_env='KEEP_PASSWORD_ENV')
    raw = json.dumps(cfg, ensure_ascii=True, indent=2).replace('\n','\r\n').encode()
    if bom: raw = b'\xef\xbb\xbf' + raw
    updated, _, removed = switch.plan_config(raw)
    assert removed == list(switch.OLD_POINTS)
    before, after = raw.decode('utf-8-sig'), updated.decode('utf-8-sig')
    a,b,old_spans = switch.points_span(before)
    c,d,new_spans = switch.points_span(after)
    assert before[:a] == after[:c] and before[b:] == after[d:]
    old_kept = [before[a:b] for point,(a,b) in zip(cfg['points'],old_spans) if point['key'] not in switch.OLD_POINTS]
    assert old_kept == [after[a:b] for a,b in new_spans]
    assert switch.plan_config(updated)[0] == updated
    assert json.loads(updated)['points'][-1] == cfg['points'][-1]
    assert updated.startswith(b'\xef\xbb\xbf') is bom


@pytest.fixture
def host(tmp_path, monkeypatch):
    root, package = tmp_path/'host', tmp_path/'package'
    for directory in (root/'backend/config', root/'backend/data', root/'backend/scripts', package):
        directory.mkdir(parents=True, exist_ok=True)
    (root/'offline-install.json').write_text('{}')
    for name in ('start_collector.py', 'start_server.ps1'):
        (root/'backend/scripts'/name).write_text('fixture launcher')
    original = json.dumps(fixture_config(), ensure_ascii=True, indent=2).encode()
    config = root/'backend/config/kepserver.local.json'
    config.write_bytes(original)
    engine = create_engine('sqlite:///'+(root/'backend/data/dense_medium.db').as_posix())
    install_codec(engine)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    repo = TelemetryRepository(sessions)
    cfg = CollectorConfig.model_validate_json(original)
    repo.acquire(cfg, 'owner-before')
    now = datetime.now(timezone.utc)-timedelta(seconds=1)
    batch = dict(source_id=switch.SOURCE_ID, batch_id=uuid4().hex, storage_policy='periodic-v1',
        samples=[normalize(point, point.key != list(switch.OLD_POINTS)[0], 0, now, now, now) for point in cfg.points])
    repo.persist(cfg, 'owner-before', batch)
    repo.state(cfg, 'owner-before', 'connected')
    choices = dict(desliming=identity(switch.SOURCE_ID, list(switch.OLD_POINTS)[0]),
        **{'density-current':identity(switch.SOURCE_ID, 'unrelated_478'), 'raw-ash':''})
    with sessions.begin() as db:
        db.add(DecisionSourceRevision(effective_at=iso_time(now), config=dict(choices=choices,
            target_min=8.4, target_max=9.2, retained_extra={'do':'not change'})))
        db.add(Setting(key='unrelated-frozen', value={'coefs':[1,2,3]}))
    manifest = dict(format=1, purpose='desliming-runs-switch-v1', version='fixture', files={}, source_before_sha256={},
        variant_before_sha256={name:[] for name in switch.VARIANT_FILES})
    for name in switch.BUNDLE_FILES:
        path = package/name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name.startswith('payload/'):
            source_name = name[len('payload/'):]
            source = root/source_name
            source.parent.mkdir(parents=True, exist_ok=True)
            before = subprocess.check_output(['git','show','HEAD:'+source_name], cwd=ROOT)
            source.write_bytes(before)
            path.write_bytes((ROOT/source_name).read_bytes())
            manifest['source_before_sha256'][source_name] = [hashlib.sha256(before).hexdigest()]
        else:
            path.write_bytes(b'fixture helper\n')
        manifest['files'][name] = hashlib.sha256(path.read_bytes()).hexdigest()
    spool = root/'backend/data/telemetry-spool/ordinary-kept.json'
    spool.parent.mkdir(parents=True)
    spool.write_bytes(b'original ordinary spool')
    events = []
    status = lambda *_: dict(repo_status(root))
    def powershell(script, *args, **kwargs):
        events.append(script.name)
        if script.name == 'StopOwnedCollector.ps1':
            owner = status().get('owner')
            if owner: repo.state(cfg, owner, 'stopped', release=True)
    def ready(root, cfg, previous, report):
        events.append('ready')
        assert status()['owner'] != previous.get('owner')
        assert status()['last_sample_at'] > previous['last_sample_at']
    control = SimpleNamespace(collector_status=status, powershell=powershell,
        collector_configuration=lambda *_:dict(enabled=True,source_id=switch.SOURCE_ID,ready_timeout=1),
        wait_collector_lease=lambda *a:events.append('lease'), collector_ready=ready)
    def start(root):
        events.append('start-collector')
        current_cfg = CollectorConfig.model_validate_json(config.read_bytes())
        owner = 'owner-after-'+uuid4().hex
        repo.acquire(current_cfg, owner)
        at = datetime.now(timezone.utc)
        batch = dict(source_id=switch.SOURCE_ID, batch_id=uuid4().hex, storage_policy='periodic-v1',
            samples=[normalize(p, p.key == list(switch.RUNS_POINTS)[0], 0, at, at, at) for p in current_cfg.points])
        repo.persist(current_cfg, owner, batch)
        repo.state(current_cfg, owner, 'connected')
    monkeypatch.setattr(switch, 'start_collector', start)
    monkeypatch.setattr(switch, 'start_web', lambda *a:events.append('start-web'))
    monkeypatch.delenv('DMCS_REMOTE_READONLY_CONFIG', raising=False)
    yield SimpleNamespace(root=root,package=package,config=config,original=original,manifest=manifest,
        control=control,events=events,sessions=sessions,spool=spool,engine=engine)
    engine.dispose()


def repo_status(root):
    with sqlite3.connect(root/'backend/data/dense_medium.db') as db:
        db.row_factory = sqlite3.Row
        return db.execute('SELECT owner,lease_until,state,last_sample_at FROM telemetry_sources WHERE source_id=?', (switch.SOURCE_ID,)).fetchone()


def test_preflight_is_zero_write_and_compact_metadata_decodes(host):
    before = switch.database_state(host.root)
    with sqlite3.connect(host.root/'backend/data/dense_medium.db') as db:
        assert '$point_meta' in db.execute('SELECT config FROM telemetry_points LIMIT 1').fetchone()[0]
    files = {p.relative_to(host.root).as_posix():p.read_bytes() for p in host.root.rglob('*') if p.is_file()}
    result = switch.apply(host.root,host.package,host.manifest,host.control,dry_run=True)
    assert result['status'] == 'preflight-passed' and host.events == []
    assert switch.database_state(host.root) == before
    assert files == {p.relative_to(host.root).as_posix():p.read_bytes() for p in host.root.rglob('*') if p.is_file()}


def test_success_preserves_old_history_and_unrelated_data_and_rerun_keeps_cutover(host):
    before = switch.database_state(host.root)
    result = switch.apply(host.root,host.package,host.manifest,host.control)
    assert result['status'] == 'installed'
    assert host.events == ['StopOwnedWeb.ps1','StopOwnedCollector.ps1','lease','start-collector','ready','start-web']
    after = switch.database_state(host.root)
    assert not any(after['points'][key][1] for key in switch.OLD_POINTS)
    assert all(after['points'][key][1] for key in switch.RUNS_POINTS)
    with host.sessions() as db:
        revisions = list(db.scalars(select(DecisionSourceRevision).order_by(DecisionSourceRevision.id)))
        assert len(revisions) == 2
        expected = deepcopy(revisions[0].config)
        expected['choices']['desliming'] = identity(switch.SOURCE_ID,'desliming_status')
        assert revisions[1].config == expected
        assert db.get(Setting,'unrelated-frozen').value == {'coefs':[1,2,3]}
        assert db.scalar(select(TelemetryReading).where(TelemetryReading.point_key == list(switch.OLD_POINTS)[0])) is not None
    assert host.spool.read_bytes() == b'original ordinary spool'
    assert all(after['points'][key][0] == before['points'][key][0] for key in switch.OLD_POINTS)
    host.events.clear()
    assert switch.apply(host.root,host.package,host.manifest,host.control)['status'] == 'unchanged-verified'
    assert switch.database_state(host.root) == after and host.events == []


@pytest.mark.parametrize('failure', ['source-after-replace','config-after-replace','metadata-after-commit','samples','web'])
def test_failure_restores_exact_source_config_binding_marker_and_keeps_history(host, monkeypatch, failure):
    before = switch.database_state(host.root)
    sources = {name:(host.root/name).read_bytes() for name in switch.SOURCE_FILES}
    if failure in ('source-after-replace','config-after-replace','metadata-after-commit'):
        atomic = switch.atomic_file
        done = False
        def interrupted(path,data):
            nonlocal done
            atomic(path,data)
            hit = ((failure == 'source-after-replace' and path == host.root/next(iter(sources)))
                or failure == 'config-after-replace' and path == host.config
                or failure == 'metadata-after-commit' and path.name == 'journal.json' and b'switched-verifying' in data)
            if hit and not done: done = True; raise OSError('synthetic atomic interruption')
        monkeypatch.setattr(switch,'atomic_file',interrupted)
    else:
        target = 'verify_samples' if failure == 'samples' else 'start_web'
        original = getattr(switch,target)
        calls = 0
        def fail_once(*args,**kwargs):
            nonlocal calls
            calls += 1
            # verify_samples has two old-health prechecks before the post-start check.
            if calls == (3 if failure == 'samples' else 1): raise OSError('synthetic health failure')
            return original(*args,**kwargs)
        monkeypatch.setattr(switch,target,fail_once)
    with pytest.raises(OSError): switch.apply(host.root,host.package,host.manifest,host.control)
    assert host.config.read_bytes() == host.original
    assert {name:(host.root/name).read_bytes() for name in sources} == sources
    assert switch.database_state(host.root) == before
    assert host.events[-3:] == ['start-collector','ready','start-web']
    assert host.spool.read_bytes() == b'original ordinary spool'


def test_unknown_source_hash_and_external_config_edit_are_never_overwritten(host):
    name = next(iter(switch.SOURCE_FILES))
    (host.root/name).write_bytes(b'# external source edit\n')
    with pytest.raises(switch.SwitchError,match='Unknown installed source'):
        switch.apply(host.root,host.package,host.manifest,host.control)
    assert host.events == [] and host.config.read_bytes() == host.original


def test_unknown_source_diagnostic_reports_only_hash_size_and_line_endings(host):
    name = 'backend/app/telemetry/derived.py'
    unknown = b'\xef\xbb\xbf# PRIVATE-CODE-SENTINEL\r\n# second line\n'
    (host.root/name).write_bytes(unknown)
    with pytest.raises(switch.SwitchError,match='Unknown installed source') as error:
        switch.apply(host.root,host.package,host.manifest,host.control)
    message = str(error.value)
    assert 'PRIVATE-CODE-SENTINEL' not in message
    facts = json.loads(message.split('; actual=',1)[1])
    assert facts == dict(path=name,sha256=hashlib.sha256(unknown).hexdigest(),bytes=len(unknown),
        utf8_bom=True,crlf=1,lf_only=1,cr_only=0)
    assert host.events == []
    assert (host.root/name).read_bytes() == unknown
    assert host.config.read_bytes() == host.original


def test_external_config_edit_during_stop_blocks_overwrite_and_restart(host):
    external = host.original.replace(b'"host"', b'"externally-edited"', 1)
    def external_edit(*args):
        host.events.append('lease')
        host.config.write_bytes(external)
    host.control.wait_collector_lease = external_edit
    with pytest.raises(switch.SwitchError, match='externally'):
        switch.apply(host.root,host.package,host.manifest,host.control)
    assert host.config.read_bytes() == external
    assert 'start-collector' not in host.events and 'start-web' not in host.events


def test_collector_exit_during_web_start_cannot_report_installed(host, monkeypatch):
    before = switch.database_state(host.root)
    first = True
    def web_starts_then_collector_exits(root, control):
        nonlocal first
        host.events.append('start-web')
        if first:
            first = False
            owner = control.collector_status(root,switch.SOURCE_ID)['owner']
            repo = TelemetryRepository(host.sessions)
            cfg = CollectorConfig.model_validate_json(host.config.read_bytes())
            repo.state(cfg,owner,'stopped',release=True)
    monkeypatch.setattr(switch,'start_web',web_starts_then_collector_exits)
    with pytest.raises(switch.SwitchError,match='ownership'):
        switch.apply(host.root,host.package,host.manifest,host.control)
    assert host.config.read_bytes() == host.original
    assert switch.database_state(host.root) == before
    assert host.events[-3:] == ['start-collector','ready','start-web']


@pytest.mark.parametrize('crlf', [False,True])
def test_legacy_variant_keeps_press_bytes_and_is_idempotent_then_rolls_back(host, monkeypatch, crlf):
    builder_spec = importlib.util.spec_from_file_location('runs_switch_builder', ROOT/'backend/scripts/package_runs_switch.py')
    builder = importlib.util.module_from_spec(builder_spec)
    builder_spec.loader.exec_module(builder)
    current = (ROOT/switch.VARIANT_SOURCE).read_bytes()
    variant, legacy = builder.legacy_derived_variant(current, crlf=crlf)
    a,b = legacy.index(b'DESLIMING_MEMBERS = ('), legacy.index(b'FLOAT_PRESS_MEMBERS = ')
    c,d = variant.index(b'DESLIMING_MEMBERS = ('), variant.index(b'FLOAT_PRESS_MEMBERS = ')
    assert legacy[:a] == variant[:c] and legacy[b:] == variant[d:]
    assert b'"XMZX-YM.YM4_2.A416/YL_STATE"' in variant
    name = switch.VARIANT_FILES[int(crlf)]
    (host.package/name).write_bytes(variant)
    host.manifest['files'][name] = hashlib.sha256(variant).hexdigest()
    host.manifest['variant_before_sha256'][name] = [hashlib.sha256(legacy).hexdigest()]
    (host.root/switch.VARIANT_SOURCE).write_bytes(legacy)
    planned = switch.preflight(host.root,host.package,host.manifest)
    assert planned['sources'][switch.VARIANT_SOURCE]['updated'] == variant
    assert switch.apply(host.root,host.package,host.manifest,host.control)['status'] == 'installed'
    assert (host.root/switch.VARIANT_SOURCE).read_bytes() == variant
    assert switch.apply(host.root,host.package,host.manifest,host.control)['status'] == 'unchanged-verified'
    assert (host.root/switch.VARIANT_SOURCE).read_bytes() == variant
    # A new isolated application of the same variant must restore its original
    # alias-bearing source after a failed health check.
    journal = next((host.root/'backups').glob('runs-switch-*/journal.json'))
    saved = json.loads(journal.read_text())
    switch.rollback_database(host.root,saved['before'],saved['written'])
    host.config.write_bytes(host.original)
    for source_name in switch.SOURCE_FILES:
        (host.root/source_name).write_bytes((journal.parent/'source'/source_name).read_bytes())
    def failed_web(*args): raise OSError('synthetic failed Web')
    first = True
    def fail_once(*args):
        nonlocal first
        if first: first = False; failed_web()
        host.events.append('start-web')
    monkeypatch.setattr(switch,'start_web',fail_once)
    with pytest.raises(OSError): switch.apply(host.root,host.package,host.manifest,host.control)
    assert (host.root/switch.VARIANT_SOURCE).read_bytes() == legacy


@pytest.mark.parametrize('choice', ['', None, identity(switch.SOURCE_ID,'desliming_status')])
def test_empty_default_or_already_group_choice_does_not_create_a_revision(host, choice):
    with host.sessions.begin() as db:
        row = db.scalar(select(DecisionSourceRevision))
        cfg = deepcopy(row.config)
        if choice is None: cfg['choices'].pop('desliming')
        else: cfg['choices']['desliming'] = choice
        row.config = cfg
    before = switch.database_state(host.root)
    switch.apply(host.root,host.package,host.manifest,host.control)
    assert switch.database_state(host.root)['revision'] == before['revision']


@pytest.mark.parametrize('damage', ['bad','type','definition','batch','receipt'])
def test_bad_or_mixed_latest_runs_blocks_before_stopping_services(host, damage):
    raw_type = None
    with host.sessions.begin() as db:
        row = db.scalar(select(TelemetryReading).where(TelemetryReading.point_key == list(switch.RUNS_POINTS)[0]))
        sample = deepcopy(row.sample)
        if damage == 'bad': sample.update(quality='bad',status_code=0x80000000)
        if damage == 'type':
            sample['value'] = 1
            raw_type = (json.dumps(sample), row.id)
        if damage == 'definition': sample['definition_id'] = 'different'
        if damage == 'batch': row.batch_id = uuid4().hex
        if damage == 'receipt':
            row.received_at = iso_time(datetime.now(timezone.utc)-timedelta(seconds=2))
            sample['received_at'] = row.received_at
        row.sample = sample
    if raw_type:
        # Bypass the trusted compact writer: inject an invalid raw JSON value
        # to exercise the decoder/verifier's handling of a corrupt external row.
        with sqlite3.connect(host.root/'backend/data/dense_medium.db') as db:
            db.execute('UPDATE telemetry_readings SET sample=? WHERE id=?', raw_type)
    with pytest.raises(switch.SwitchError): switch.apply(host.root,host.package,host.manifest,host.control)
    assert host.events == [] and host.config.read_bytes() == host.original
