"""只用合成点位和隔离 SQLite；不会连接现场 KEPServer。"""
import asyncio
import copy
import socket
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.telemetry.config import CollectorConfig, PointConfig
from app.telemetry.domain import availability, iso_time, normalize, utc_now
from app.telemetry.models import TelemetryPoint, TelemetryReading, TelemetrySource
from app.telemetry.repository import CollectorBusy, TelemetryRepository, latest_view
from app.telemetry.spool import DurableSpool, SpoolError
from app.telemetry.worker import retry_delay, run_collector


@pytest.fixture
def cfg():
    return CollectorConfig(enabled=True, endpoint="opc.tcp://127.0.0.1:49320", security="None", allow_insecure=True,
        poll_seconds=0.1, timeout_seconds=1, retry_min_seconds=0.1, retry_max_seconds=0.1,
        points=[PointConfig(key="density", name="密度", node_id="ns=2;s=Density", unit="g/cm³", minimum=0, maximum=3)])


@pytest.fixture
def repo(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "telemetry.db"), connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    repository = TelemetryRepository(sessionmaker(engine))
    yield repository
    engine.dispose()


def batch(cfg, value=1.45, status=0, when=None):
    when = when or utc_now()
    sample = normalize(cfg.points[0], value, status, when, when, when)
    return {"source_id": cfg.source_id, "batch_id": uuid.uuid4().hex, "samples": [sample]}


def count(repo):
    with repo.sessions() as db:
        return db.scalar(select(func.count()).select_from(TelemetryReading))


@pytest.mark.parametrize("overrides", [
    {"allow_insecure": False}, {"endpoint": "opc.tcp://user:secret@localhost:49320"},
    {"endpoint": "http://localhost:49320"}, {"endpoint": "opc.tcp://localhost"},
    {"points": []}, {"username_env": "USER", "password_env": "PASSWORD"},
    {"security": "Basic256Sha256"}, {"unknown_option": 1},
])
def test_rejects_unsafe_or_incomplete_config(cfg, overrides):
    with pytest.raises(ValidationError):
        CollectorConfig.model_validate({**cfg.model_dump(), **overrides})


def test_duplicate_points_rejected(cfg):
    with pytest.raises(ValidationError):
        CollectorConfig.model_validate({**cfg.model_dump(), "points": [cfg.points[0].model_dump()] * 2})


@pytest.mark.parametrize("value,status,issue", [
    (0, 0, None), (True, 0, "invalid_value_type"), ("1.45", 0, "invalid_value_type"),
    (float("nan"), 0, "non_finite_value"), (4, 0, "out_of_range"),
    (1.45, 0x80000000, "bad_quality"), (1.45, 0x40000000, "uncertain_quality"),
])
def test_quality_and_value_validation(cfg, value, status, issue):
    sample = batch(cfg, value, status)["samples"][0]
    assert sample["issue"] == issue
    if value == 0 and not isinstance(value, bool):
        assert sample["value"] == 0


def test_boolean_false_and_scale_are_preserved(cfg):
    now = utc_now()
    point = PointConfig(key="run", name="运行状态", node_id="ns=2;s=Run", value_type="boolean")
    assert normalize(point, False, 0, now, now, now)["value"] is False
    point = cfg.points[0].model_copy(update={"scale": 0.001})
    assert normalize(point, 1450, 0, now, now, now)["value"] == pytest.approx(1.45)


def test_freshness_and_source_clock_are_separate(cfg):
    now = utc_now()
    sample = normalize(cfg.points[0], 1.45, 0, now - timedelta(days=1), now, now)
    assert availability(sample, cfg.points[0].model_dump(), True, now) == (True, "good")
    assert availability(sample, cfg.points[0].model_dump(), True, now + timedelta(seconds=31)) == (False, "stale")
    assert availability(sample, cfg.points[0].model_dump(), False, now) == (False, "collector_offline")
    future = normalize(cfg.points[0], 1.45, 0, now + timedelta(minutes=1), now, now)
    assert future["issue"] == "future_source_time"


def test_lease_prevents_duplicate_collectors(cfg, repo):
    repo.acquire(cfg, "first")
    with pytest.raises(CollectorBusy):
        repo.acquire(cfg, "second")
    repo.state(cfg, "first", "stopped")
    repo.acquire(cfg, "second")


def test_history_is_throttled_quality_changes_recorded_and_replay_idempotent(cfg, repo):
    repo.acquire(cfg, "owner")
    now = utc_now()
    first = batch(cfg, when=now)
    repo.persist(cfg, "owner", first)
    repo.persist(cfg, "owner", first)
    repo.persist(cfg, "owner", batch(cfg, 1.46, when=now + timedelta(seconds=1)))
    assert count(repo) == 1
    repo.persist(cfg, "owner", batch(cfg, 1.46, 0x80000000, now + timedelta(seconds=2)))
    repo.persist(cfg, "owner", batch(cfg, 1.47, 0, now + timedelta(seconds=3)))
    assert count(repo) == 3
    with repo.sessions() as db:
        assert db.get(TelemetryPoint, (cfg.source_id, "density")).latest["value"] == 1.47
    # 更旧的批次重放不得回退当前读数。
    repo.persist(cfg, "owner", first)
    with repo.sessions() as db:
        assert db.get(TelemetryPoint, (cfg.source_id, "density")).latest["value"] == 1.47


def test_five_second_history_keeps_all_batches_despite_receipt_jitter(cfg,repo):
    cfg.poll_seconds=cfg.history_interval_seconds=5
    repo.acquire(cfg,'owner')
    now=utc_now()
    payloads=[batch(cfg,1.48,when=now+timedelta(seconds=s)) for s in (0,4.99,10.01,15)]
    payloads.append(batch(cfg,1.0,0x80000000,now+timedelta(seconds=19.99)))
    for payload in payloads+payloads:
        repo.persist(cfg,'owner',payload)
    assert count(repo)==5
    with repo.sessions() as db:
        saved=list(db.scalars(select(TelemetryReading).order_by(TelemetryReading.received_at)))
        assert [r.received_at for r in saved]==[p['samples'][0]['received_at'] for p in payloads]
        assert saved[-1].sample['quality']=='bad'


def test_reconfigured_scale_rejects_old_spool_as_current_value(cfg, repo):
    first = batch(cfg)
    repo.acquire(cfg, "owner")
    repo.state(cfg, "owner", "stopped")
    changed = copy.deepcopy(cfg)
    changed.points[0].scale = 2
    repo.acquire(changed, "new-owner")
    repo.persist(changed, "new-owner", first)
    with repo.sessions() as db:
        assert db.get(TelemetryPoint, (cfg.source_id, "density")).latest is None


def test_lease_expiry_disables_latest_values(cfg, repo):
    repo.acquire(cfg, "owner")
    repo.persist(cfg, "owner", batch(cfg))
    repo.state(cfg, "owner", "connected")
    with repo.sessions() as db:
        assert latest_view(db)["items"][0]["available"]
        expired = latest_view(db, now=utc_now() + timedelta(seconds=cfg.lease_seconds + 1))
        assert expired["sources"][0]["state"] == "offline"
        assert not expired["items"][0]["available"]


def test_spool_survives_restart_and_is_acknowledged_only_after_commit(cfg, repo, tmp_path):
    spool = DurableSpool(tmp_path / "spool", cfg.source_id)
    first = batch(cfg)
    path = spool.enqueue(first)
    restarted = DurableSpool(tmp_path / "spool", cfg.source_id)
    assert restarted.read(path) == first
    repo.acquire(cfg, "owner")
    repo.persist(cfg, "owner", restarted.read(path))
    # 模拟数据库已提交、缓存尚未删除时崩溃；再次重放不会重复历史。
    repo.persist(cfg, "owner", restarted.read(path))
    assert count(repo) == 1
    restarted.ack(path)
    assert restarted.stats() == {"batches": 0, "bytes": 0}


def test_spool_capacity_and_corruption_never_silently_discard(cfg, tmp_path):
    small = DurableSpool(tmp_path, cfg.source_id, max_bytes=1)
    with pytest.raises(SpoolError):
        small.enqueue(batch(cfg))
    assert not small.pending()
    broken = small.folder / "batch-1-00000000000000000000000000000000.json"
    broken.write_text("broken", encoding="utf-8")
    with pytest.raises(SpoolError):
        small.read(broken)
    assert broken.exists()


@pytest.mark.parametrize("legacy_days", [0, 1, 7, 90])
def test_legacy_retention_config_is_permanent(cfg, legacy_days, tmp_path):
    from app.telemetry.config import load_config
    assert CollectorConfig().retention_days == 0
    path = tmp_path / "collector.json"
    path.write_text(cfg.model_copy(update={"retention_days": legacy_days}).model_dump_json(), encoding="utf-8")
    loaded = load_config(path)
    assert loaded.retention_days == loaded.model_dump()["retention_days"] == 0


def test_permanent_history_survives_legacy_cleanup_and_restart(cfg, repo):
    repo.acquire(cfg, "owner")
    old = batch(cfg, when=utc_now() - timedelta(days=400))
    repo.persist(cfg, "owner", old)
    repo.persist(cfg, "owner", batch(cfg, status=0x80000000, when=utc_now() - timedelta(days=300)))
    repo.persist(cfg, "owner", batch(cfg))
    # Even an unvalidated legacy config cannot re-enable the removed cleanup.
    restarted = TelemetryRepository(repo.sessions)
    restarted.prune(cfg.model_copy(update={"retention_days": 7}))
    assert count(restarted) == 3
    with repo.sessions() as db:
        history = list(db.scalars(select(TelemetryReading).order_by(TelemetryReading.received_at)))
        assert history[0].sample == old["samples"][0]
        assert history[1].sample["quality"] == "bad"
        assert db.get(TelemetryPoint, (cfg.source_id, "density")).latest


def test_readonly_api_pagination_and_snapshot_isolation(cfg, repo, monkeypatch):
    from app.services.state import state_revision
    with repo.sessions() as db:
        original_revision = state_revision(db)
    repo.acquire(cfg, "owner")
    start = utc_now() - timedelta(minutes=3)
    for minute in range(3):
        repo.persist(cfg, "owner", batch(cfg, when=start + timedelta(minutes=minute)))
    with repo.sessions() as db:
        assert state_revision(db) == original_revision, "采集写入不得改变业务快照版本"

    def isolated_db():
        with repo.sessions() as db:
            yield db

    from app.services import migrate
    monkeypatch.setattr(migrate, "SessionLocal", repo.sessions)
    app.dependency_overrides[get_db] = isolated_db
    try:
        client = TestClient(app)
        state = client.get("/api/v1/state").json()
        response = client.get("/api/v1/telemetry/history", params={"source_id": cfg.source_id, "point_key": "density", "limit": 2})
        assert response.status_code == 200
        page = response.json()
        assert len(page["items"]) == 2 and page["next_before_id"]
        last = client.get("/api/v1/telemetry/history", params={"source_id": cfg.source_id, "point_key": "density", "before_id": page["next_before_id"]}).json()
        assert len(last["items"]) == 1
        assert client.get("/api/v1/telemetry/history", params={"source_id": cfg.source_id, "point_key": "density", "limit": 501}).status_code == 422
        assert client.put("/api/v1/state", json=state).json()["ok"]
        assert count(repo) == 3
        assert client.post("/api/v1/telemetry/latest", json={}).status_code == 405
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_worker_retries_network_failure_and_releases_lease(cfg, repo, tmp_path, monkeypatch):
    def unexpected_cleanup(_cfg):
        pytest.fail("Collector must not schedule history deletion")
    monkeypatch.setattr(repo, "prune", unexpected_cleanup)
    async def scenario():
        stop = asyncio.Event()
        calls = 0

        class Reader:
            async def __aenter__(self):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("synthetic offline")
                return self

            async def __aexit__(self, *_args):
                pass

            async def read(self):
                stop.set()
                return batch(cfg)["samples"]

        code = await run_collector(cfg, repo, DurableSpool(tmp_path / "spool", cfg.source_id), stop=stop, reader_factory=lambda _: Reader())
        assert code == 0 and calls == 2

    asyncio.run(scenario())
    assert count(repo) == 1
    with repo.sessions() as db:
        source = db.get(TelemetrySource, cfg.source_id)
        assert source.state == "stopped" and source.owner is None


def test_backoff_is_bounded():
    assert all(1 <= retry_delay(i, 1, 30) <= 30 for i in range(100))


def test_fatal_configuration_error_stays_visible_without_retry(cfg, repo, tmp_path):
    from app.telemetry.opcua import ConfigurationError

    class Reader:
        async def __aenter__(self):
            raise ConfigurationError("synthetic certificate error")

        async def __aexit__(self, *_args):
            pass

    code = asyncio.run(run_collector(cfg, repo, DurableSpool(tmp_path / "spool", cfg.source_id), reader_factory=lambda _: Reader()))
    assert code == 1 and count(repo) == 0
    with repo.sessions() as db:
        source = latest_view(db)["sources"][0]
        assert source["state"] == "fault" and source["error_code"] == "ConfigurationError"


def test_opcua_read_requests_both_timestamps_without_writes(cfg):
    asyncua = pytest.importorskip("asyncua")
    from app.telemetry.opcua import OpcUaReader
    from types import SimpleNamespace

    async def read(parameters):
        assert parameters.TimestampsToReturn == asyncua.ua.TimestampsToReturn.Both
        assert parameters.MaxAge == 0
        assert parameters.NodesToRead[0].AttributeId == asyncua.ua.AttributeIds.Value
        now = utc_now()
        return [asyncua.ua.DataValue(asyncua.ua.Variant(1.45), SourceTimestamp=now, ServerTimestamp=now)]

    reader = OpcUaReader(cfg)
    reader.client = SimpleNamespace(uaclient=SimpleNamespace(read=read))
    reader.nodes = [SimpleNamespace(nodeid=asyncua.ua.NodeId("Density", 2))]
    sample = asyncio.run(reader.read())[0]
    assert sample["value"] == 1.45 and sample["source_at"] and sample["server_at"]


def test_each_opcua_chunk_preserves_its_actual_receive_time(cfg, monkeypatch):
    asyncua = pytest.importorskip("asyncua")
    from app.telemetry import opcua
    from types import SimpleNamespace
    cfg.points = [cfg.points[0].model_copy(update={"key": f"point{i}", "node_id": f"ns=2;s=Point{i}"}) for i in range(101)]
    first = utc_now()
    times = iter([first, first + timedelta(seconds=40)])
    monkeypatch.setattr(opcua, "utc_now", lambda: next(times))

    async def read(parameters):
        return [asyncua.ua.DataValue(asyncua.ua.Variant(1.45), SourceTimestamp=first, ServerTimestamp=first) for _ in parameters.NodesToRead]

    reader = opcua.OpcUaReader(cfg)
    reader.client = SimpleNamespace(uaclient=SimpleNamespace(read=read))
    reader.nodes = [SimpleNamespace(nodeid=asyncua.ua.NodeId(f"Point{i}", 2)) for i in range(101)]
    samples = asyncio.run(reader.read())
    assert samples[0]["received_at"] == iso_time(first)
    assert samples[-1]["received_at"] == iso_time(first + timedelta(seconds=40))


@pytest.mark.parametrize("invalid", ["expired_client", "wrong_client_uri", "expired_server", "wrong_server_uri", "valid"])
def test_certificate_validation_and_pinned_policy(cfg, tmp_path, invalid):
    asyncua = pytest.importorskip("asyncua")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from asyncua.common.utils import ServiceError
    from app.telemetry.opcua import ConfigurationError, configure_security

    def certificate(uri, expired=False):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic test")])
        now = utc_now()
        return x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=2)).not_valid_after(now - timedelta(days=1) if expired else now + timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri)]), critical=False).sign(key, hashes.SHA256())

    cert = certificate("urn:wrong" if invalid == "wrong_client_uri" else cfg.application_uri, invalid == "expired_client")
    path = tmp_path / "client.der"
    path.write_bytes(cert.public_bytes(serialization.Encoding.DER))
    cfg.client_certificate = str(path)
    cfg.client_key = str(tmp_path / "client-key.pem")
    cfg.server_certificate = str(tmp_path / "pinned-server.der")

    class Client:
        async def set_security(self, policy, cert, key, *, server_certificate, mode):
            assert server_certificate == cfg.server_certificate
            assert mode == asyncua.ua.MessageSecurityMode.SignAndEncrypt
            assert policy.__name__ == "SecurityPolicyBasic256Sha256"

    async def scenario():
        client = Client()
        if invalid in {"expired_client", "wrong_client_uri"}:
            with pytest.raises(ConfigurationError):
                await configure_security(client, cfg)
            return
        await configure_security(client, cfg)
        server_cert = certificate("urn:wrong" if invalid == "wrong_server_uri" else "urn:dmcs:server", invalid == "expired_server")
        descriptor = asyncua.ua.ApplicationDescription(ApplicationUri="urn:dmcs:server", ApplicationType=asyncua.ua.ApplicationType.Server)
        if invalid in {"expired_server", "wrong_server_uri"}:
            with pytest.raises(ServiceError):
                await client.certificate_validator(server_cert, descriptor)
        else:
            await client.certificate_validator(server_cert, descriptor)

    asyncio.run(scenario())


def test_real_local_opcua_pipeline_is_readonly(cfg, repo, tmp_path):
    # 可选依赖未安装时明确跳过；安装 requirements-kepserver.txt 后验证真实协议。
    asyncua = pytest.importorskip("asyncua")

    async def scenario():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = asyncua.Server()
        await server.init()
        server.set_endpoint(f"opc.tcp://127.0.0.1:{port}/test/")
        server.set_security_policy([asyncua.ua.SecurityPolicyType.NoSecurity])
        namespace = await server.register_namespace("urn:dmcs:test")
        obj = await server.nodes.objects.add_object(namespace, "Test")
        node = await obj.add_variable(asyncua.ua.NodeId("Density", namespace), "Density", 1.48)
        cfg.endpoint = f"opc.tcp://127.0.0.1:{port}/test/"
        cfg.points[0].node_id = node.nodeid.to_string()
        async with server:
            code = await run_collector(cfg, repo, DurableSpool(tmp_path / "spool", cfg.source_id), once=True)
            assert code == 0
            assert await node.read_value() == 1.48
        with repo.sessions() as db:
            sample = db.get(TelemetryPoint, (cfg.source_id, "density")).latest
            assert sample["value"] == 1.48 and sample["quality"] == "good"
            assert sample["source_at"] and sample["server_at"] and sample["received_at"]
        assert count(repo) == 1

    asyncio.run(scenario())
