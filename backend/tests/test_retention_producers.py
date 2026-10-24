"""Producer integration using temporary logs, SQLite files and fake processes."""
from datetime import datetime, timedelta, timezone
from contextlib import closing
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from app.services import migrate
from app.storage_retention import files
from app.telemetry import control


BACKEND = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 23, 6, tzinfo=timezone.utc)


def registry(root):
    return json.loads((root/files.REGISTRY_NAME).read_text(encoding="utf-8"))


def sqlite_file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as database:
        database.execute("create table evidence (value integer)")
        database.execute("insert into evidence values (42)")
        database.commit()
    return path


def load_update():
    spec = importlib.util.spec_from_file_location("retention_test_update", BACKEND/"scripts/windows_offline/apply_update.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_retention_module(root):
    target = root/"backend/app/storage_retention/files.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(files.__file__), target)


def test_web_startup_preserves_legacy_and_owned_rotations_are_90_day_errors(tmp_path):
    clock = [NOW]
    path = tmp_path/"dmcs_server.log"
    path.write_text("legacy log: origin and close status unknown", encoding="utf-8")
    writer = files.RotatingWebLog(path, tmp_path, max_bytes=32, clock=lambda:clock[0])
    archive_root = files.web_log_archive_root(tmp_path, path)
    assert len(list(archive_root.glob("*.log"))) == 1
    assert not (archive_root/files.REGISTRY_NAME).exists()
    writer.write("\u4e2d\u6587 first owned line\n")
    writer.flush()
    assert path.read_text(encoding="utf-8-sig") == "\u4e2d\u6587 first owned line\n"
    clock[0] += timedelta(days=1)
    writer.write("second day line\n")
    entries = registry(archive_root)["entries"]
    assert len(entries) == 1
    assert all(entry["category"] == "error" for entry in entries.values())
    assert path.read_text(encoding="utf-8-sig") == "second day line\n"
    writer.close()
    assert writer.closed and path.read_text(encoding="utf-8-sig") == "second day line\n"
    # Existing diagnostics can still read the final current log. The next
    # startup certifies it using the exact clean-close identity proof.
    next_writer = files.RotatingWebLog(path, tmp_path, clock=lambda:clock[0])
    next_writer.close()
    # The legacy file has no registry entry even after restart.
    assert len(list(archive_root.glob("*.log"))) == 3
    assert len(registry(archive_root)["entries"]) == 2
    plan = files.plan_cleanup(archive_root, installation_id=files.installation_identity(tmp_path),
                              now=clock[0]+timedelta(days=91))
    assert len(plan["actions"]) == 2
    files.execute_cleanup(archive_root, plan, installation_id=files.installation_identity(tmp_path),
                          now=clock[0]+timedelta(days=91))
    assert len(list(archive_root.glob("*.log"))) == 1


def test_web_size_rotation_and_writer_lease_keep_current_file_safe(tmp_path):
    path = tmp_path/"dmcs_server.log"
    writer = files.RotatingWebLog(path, tmp_path, max_bytes=20)
    try:
        writer.write("first long line\n")
        with pytest.raises(files.RetentionFileError, match="owns"):
            files.RotatingWebLog(path, tmp_path, max_bytes=20)
        writer.write("second long line\n")
        assert path.read_text(encoding="utf-8-sig") == "second long line\n"
        entries = registry(writer.root)["entries"]
        assert len(entries) == 1
        assert all(entry["path"] != path.name for entry in entries.values())
    finally:
        writer.close()


def test_web_retention_registration_failure_preserves_archive_and_logging(tmp_path, monkeypatch):
    writer = files.RotatingWebLog(tmp_path/"dmcs_server.log", tmp_path, max_bytes=20)
    monkeypatch.setattr(files, "register_closed_log", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("busy")))
    try:
        writer.write("first long line\n")
        writer.write("second long line\n")
        assert writer.path.read_text(encoding="utf-8-sig") == "second long line\n"
        assert writer.last_registration_error == "ValueError"
        assert len(list(writer.root.glob("*.log"))) == 1
        assert not files.plan_cleanup(writer.root, installation_id=writer.installation_id)["actions"]
    finally:
        writer.close()


def test_web_changed_file_after_clean_close_is_legacy_not_certified(tmp_path):
    path = tmp_path/"dmcs_server.log"
    writer = files.RotatingWebLog(path, tmp_path)
    writer.write("owned data\n")
    writer.close()
    path.write_text("an unknown writer changed the closed current log", encoding="utf-8")
    restarted = files.RotatingWebLog(path, tmp_path)
    try:
        assert len(list(restarted.root.glob("*.log"))) == 1
        assert not (restarted.root/files.REGISTRY_NAME).exists()
    finally:
        restarted.close()


def force_cross_volume(monkeypatch, current, archive_root):
    real_replace = os.replace
    def cross_volume(source, target):
        if Path(source) == current and Path(target).parent == archive_root:
            raise OSError(errno.EXDEV, "synthetic different volumes")
        return real_replace(source, target)
    monkeypatch.setattr(files.os, "replace", cross_volume)


def test_web_cross_volume_copy_is_durable_verified_then_removes_c_original(tmp_path, monkeypatch):
    path = tmp_path/"C temp/dmcs_server.log"
    install_root = tmp_path/"installation"
    install_root.mkdir()
    writer = files.RotatingWebLog(path, install_root, max_bytes=24)
    assert writer.root == install_root/"backend/data/server-logs"
    force_cross_volume(monkeypatch, path, writer.root)
    events = []
    real_fsync, real_digest, real_unlink = files.os.fsync, files.hashlib.file_digest, Path.unlink
    def synced(descriptor):
        events.append("fsync")
        return real_fsync(descriptor)
    def hashed(*args, **kwargs):
        events.append("hash_verified")
        return real_digest(*args, **kwargs)
    def unlinked(item, *args, **kwargs):
        if item == path:
            events.append("c_original_removed")
        return real_unlink(item, *args, **kwargs)
    monkeypatch.setattr(files.os, "fsync", synced)
    monkeypatch.setattr(files.hashlib, "file_digest", hashed)
    monkeypatch.setattr(Path, "unlink", unlinked)
    try:
        writer.write("first durable line\n")
        writer.write("second line\n")
        assert events.index("fsync") < events.index("hash_verified") < events.index("c_original_removed")
        assert path.read_text(encoding="utf-8-sig") == "second line\n"
        entries = registry(writer.root)["entries"]
        assert len(entries) == 1
        archived = writer.root/next(iter(entries.values()))["path"]
        assert archived.read_text(encoding="utf-8-sig") == "first durable line\n"
    finally:
        writer.close()


def test_web_bad_cross_volume_copy_retains_all_current_contents(tmp_path, monkeypatch):
    path = tmp_path/"dmcs_server.log"
    writer = files.RotatingWebLog(path, tmp_path, max_bytes=20)
    force_cross_volume(monkeypatch, path, writer.root)
    monkeypatch.setattr(files.hashlib, "file_digest", lambda *args, **kwargs:SimpleNamespace(hexdigest=lambda:"bad-copy"))
    try:
        writer.write("first long line\n")
        writer.write("second long line\n")
        assert path.read_text(encoding="utf-8-sig") == "first long line\nsecond long line\n"
        assert "integrity" in writer.last_rotation_error
        assert not list(writer.root.glob("*.log"))
        assert not list(writer.root.glob("*.copying"))
        assert not (writer.root/files.REGISTRY_NAME).exists()
    finally:
        writer.close()


def test_web_oversized_legacy_copy_is_deferred_without_repeated_io(tmp_path, monkeypatch):
    path = tmp_path/"dmcs_server.log"
    path.write_text("unknown old contents"*10)
    root = files.web_log_archive_root(tmp_path)
    force_cross_volume(monkeypatch, path, root)
    writer = files.RotatingWebLog(path, tmp_path, max_bytes=20, copy_budget_bytes=16)
    try:
        assert "budget" in writer.last_rotation_error
        writer.write("new messages\n")
        assert path.read_text().endswith("new messages\n")
        assert not list(root.glob("*.log"))
    finally:
        writer.close()


def test_migration_small_backup_is_certified_after_full_check(tmp_path):
    path = sqlite_file(tmp_path/"backend/data/backups/small.db")
    migrate._certify_migration_backup(path, install_root=tmp_path,
        created_at=datetime.now(timezone.utc)-timedelta(seconds=1))
    entry = registry(tmp_path)["entries"]["migration:small.db"]
    assert entry["state"] == "closed"
    assert entry["verification"]["method"] == "sqlite-full-check"
    assert entry["verification"]["result"] == "ok"
    assert registry(tmp_path)["stable_backup_id"] is None


def test_migration_large_backup_has_no_extra_sqlite_scan_and_remains_protected(tmp_path, monkeypatch):
    path = sqlite_file(tmp_path/"backend/data/backups/large.db")
    monkeypatch.setattr(migrate, "BACKUP_CONTENT_CHECK_MAX_BYTES", 0)
    def forbidden(*args, **kwargs):
        raise AssertionError("A large backup must not be reopened for certification")
    monkeypatch.setattr(migrate.sqlite3, "connect", forbidden)
    migrate._certify_migration_backup(path, install_root=tmp_path, created_at=NOW)
    entry = registry(tmp_path)["entries"]["migration:large.db"]
    assert entry["state"] == "unverified"
    assert "64MiB" in entry["protection_reason"]
    identity = files.installation_identity(tmp_path)
    assert not files.plan_cleanup(tmp_path, installation_id=identity)["actions"]


def test_migration_corrupt_content_never_receives_verified_certificate(tmp_path):
    path = tmp_path/"backend/data/backups/bad.db"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a SQLite database")
    with pytest.raises(sqlite3.DatabaseError):
        migrate._certify_migration_backup(path, install_root=tmp_path, created_at=NOW)
    assert not (tmp_path/files.REGISTRY_NAME).exists()
    assert path.exists()


def collector_metadata(tmp_path):
    root = tmp_path/"runtime"
    root.mkdir()
    config = (tmp_path/"kepserver.local.json").resolve()
    stdout = root/"source-20261023T000000.stdout.log"
    stderr = root/"source-20261023T000000.stderr.log"
    stdout.write_bytes(b"normal old messages")
    stderr.write_bytes(b"error old messages")
    path = root/"source.json"
    path.write_text(json.dumps(dict(source_id="source", config=str(config), pid=987654,
                                   process_identity="old-creation", stdout=str(stdout), stderr=str(stderr))))
    return root, path, config, stdout, stderr


def test_exited_collector_log_registration_never_signals_or_starts_process(tmp_path, monkeypatch):
    root, metadata, config, stdout, stderr = collector_metadata(tmp_path)
    monkeypatch.setattr(control, "_process_identity", lambda pid:None)
    monkeypatch.setattr(control, "_confirmed_process_exited", lambda pid:True)
    monkeypatch.setattr(control, "_log_has_no_writer", lambda path:True)
    monkeypatch.setattr(control.subprocess, "Popen", lambda *args, **kwargs:pytest.fail("No process start allowed"))
    control._register_exited_collector_logs(metadata, root, SimpleNamespace(source_id="source"), config)
    entries = registry(root)["entries"]
    assert {entry["category"] for entry in entries.values()} == {"normal", "error"}
    assert stdout.exists() and stderr.exists()
    assert json.loads(metadata.read_text())["pid"] == 987654


@pytest.mark.parametrize("identity,confirmed,no_writer", [
    ("old-creation", True, True), (None, False, True), (None, True, False),
])
def test_live_unknown_or_inherited_collector_writer_stays_protected(tmp_path, monkeypatch, identity, confirmed, no_writer):
    root, metadata, config, stdout, stderr = collector_metadata(tmp_path)
    monkeypatch.setattr(control, "_process_identity", lambda pid:identity)
    monkeypatch.setattr(control, "_confirmed_process_exited", lambda pid:confirmed)
    monkeypatch.setattr(control, "_log_has_no_writer", lambda path:no_writer)
    control._register_exited_collector_logs(metadata, root, SimpleNamespace(source_id="source"), config)
    assert not (root/files.REGISTRY_NAME).exists()
    assert stdout.exists() and stderr.exists()


def test_update_reuses_existing_full_check_without_another_database_scan(tmp_path, monkeypatch):
    root = tmp_path/"installation"
    install_retention_module(root)
    first = sqlite_file(root/"backups/first/dense_medium.db").parent
    second = sqlite_file(root/"backups/second/dense_medium.db").parent
    update = load_update()
    monkeypatch.setattr(update.sqlite3, "connect", lambda *args, **kwargs:pytest.fail("Certification must reuse prior checks"))
    update.register_retention_snapshot(root, first, {"content_check":"full"}, None, NOW)
    assert registry(root)["stable_backup_id"] is None
    update.register_retention_snapshot(root, second, {"content_check":"deferred"}, {"passed":True}, NOW)
    state = registry(root)
    assert len(state["entries"]) == 2
    assert state["stable_backup_id"] == "source-update:first"
    assert all(entry.get("runtime_verified_at") for entry in state["entries"].values())


def test_update_timeout_and_structural_pass_cannot_become_verified_or_stable(tmp_path):
    root = tmp_path/"installation"
    install_retention_module(root)
    backup = sqlite_file(root/"backups/timeout/dense_medium.db").parent
    update = load_update()
    update.register_retention_snapshot(root, backup, {"content_check":"deferred"}, {"passed":False}, NOW)
    entry = registry(root)["entries"]["source-update:timeout"]
    assert entry["state"] == "unverified"
    assert "verification" not in entry
    assert registry(root)["stable_backup_id"] is None


def test_update_and_migration_share_one_global_two_plus_stable_policy(tmp_path):
    root = tmp_path/"installation"
    install_retention_module(root)
    first = sqlite_file(root/"backups/stable-update/dense_medium.db").parent
    update = load_update()
    update.register_retention_snapshot(root, first, {"content_check":"full"}, None, NOW-timedelta(days=50))
    old = sqlite_file(root/"backend/data/backups/old-migration.db")
    newer = sqlite_file(root/"backend/data/backups/newer-migration.db")
    migrate._certify_migration_backup(old, install_root=root, created_at=NOW-timedelta(days=40))
    migrate._certify_migration_backup(newer, install_root=root, created_at=NOW-timedelta(days=20))
    newest = sqlite_file(root/"backups/newest-update/dense_medium.db").parent
    update.register_retention_snapshot(root, newest, {"content_check":"full"}, None, NOW-timedelta(days=1))
    scope = files.backup_registry_root(root, root/"backend/data/dense_medium.db")
    assert scope == root
    state = registry(scope)
    assert state["stable_backup_id"] == "source-update:stable-update"
    identity = files.installation_identity(root)
    after_delay = datetime.now(timezone.utc)+timedelta(minutes=10)
    plan = files.plan_cleanup(scope, installation_id=identity, now=after_delay)
    assert [item["id"] for item in plan["actions"]] == ["migration:old-migration.db"]
    files.execute_cleanup(scope, plan, installation_id=identity, now=after_delay)
    assert not old.exists()
    assert first.exists() and newer.exists() and newest.exists()


def test_old_per_directory_registry_is_not_adopted_into_global_scope(tmp_path):
    root = tmp_path/"installation"
    install_retention_module(root)
    old = sqlite_file(root/"backups/legacy-registered/dense_medium.db").parent
    identity = files.installation_identity(root)
    files.register_verified_backup(root/"backups", old, installation_id=identity, backup_id="old-scope",
        created_at=NOW, verification={"method":"sqlite-full-check", "result":"ok", "checked_at":NOW}, stable=True)
    old_registry = (root/"backups"/files.REGISTRY_NAME).read_bytes()
    fresh = sqlite_file(root/"backups/new-update/dense_medium.db").parent
    load_update().register_retention_snapshot(root, fresh, {"content_check":"full"}, None, NOW)
    assert "old-scope" not in registry(root)["entries"]
    assert (root/"backups"/files.REGISTRY_NAME).read_bytes() == old_registry
    assert old.exists()


def test_cross_volume_or_drive_root_registry_scope_is_refused(tmp_path, monkeypatch):
    def different_volumes(*args, **kwargs):
        raise ValueError("Paths do not have the same drive")
    monkeypatch.setattr(files.os.path, "commonpath", different_volumes)
    with pytest.raises(files.RetentionFileError, match="different volumes"):
        files.backup_registry_root(tmp_path, tmp_path/"data/live.db")
    monkeypatch.setattr(files.os.path, "commonpath", lambda *args:str(Path(tmp_path.anchor)))
    with pytest.raises(files.RetentionFileError, match="drive root"):
        files.backup_registry_root(tmp_path, tmp_path/"data/live.db")


@pytest.mark.skipif(os.name != "nt", reason="NTFS data-move junction layout")
def test_shared_registry_resolves_actual_c_to_d_style_junctions(tmp_path):
    import subprocess
    root, storage = tmp_path/"C installation", tmp_path/"D data storage"
    (root/"backend").mkdir(parents=True)
    (storage/"data").mkdir(parents=True)
    (storage/"backups").mkdir()
    links = [(root/"backups",storage/"backups"), (root/"backend/data",storage/"data")]
    created = []
    try:
        for link, target in links:
            result = subprocess.run(["cmd","/c","mklink","/J",str(link),str(target)], capture_output=True)
            if result.returncode:
                pytest.skip("Could not create temporary NTFS junction")
            created.append(link)
        assert files.backup_registry_root(root, root/"backend/data/live.db") == storage
    finally:
        for link in reversed(created):
            os.rmdir(link)


def test_actual_source_update_registers_backup_only_after_health_checks(tmp_path, monkeypatch):
    update = load_update()
    root, package = tmp_path/"host", tmp_path/"package"
    install_retention_module(root)
    sqlite_file(root/"backend/data/dense_medium.db")
    (root/"offline-install.json").write_text("{}")
    (root/"Start.ps1").write_text("unused")
    target = root/"frontend/js/example.js"
    target.parent.mkdir(parents=True)
    target.write_text("old")
    source = package/"payload/frontend/js/example.js"
    source.parent.mkdir(parents=True)
    source.write_text("new")
    manifest = dict(revision="temporary",files={"frontend/js/example.js":dict(
        sha256=update.digest(source), previous_sha256=update.digest(target))})
    monkeypatch.setattr(update, "powershell", lambda *args, **kwargs:None)
    monkeypatch.setattr(update, "restart", lambda *args:None)
    def response(url, **kwargs):
        assert not (root/files.REGISTRY_NAME).exists(), "No certificate before completed startup checks"
        return io.BytesIO(json.dumps(dict(advice_policy="gpt-hourly-reference-v4")).encode()
                          if "hourly" in url else "实时总精煤灰分".encode())
    monkeypatch.setattr(update, "urlopen", response)
    backup = update.apply(package, root, manifest)
    entry = registry(root)["entries"]["source-update:"+backup.name]
    assert entry["state"] == "closed" and entry["runtime_verified_at"]
    assert target.read_text() == "new"
    assert registry(root)["stable_backup_id"] is None


@pytest.mark.parametrize('name', ['backend/scripts/unknown.py', 'backend/scripts/storage_retention.py.extra',
                                  'backend/scripts/storage_retention.py/evil', 'backend/scripts/../storage_retention.py'])
def test_updater_only_allows_exact_retention_cli_script(tmp_path, name):
    update = load_update()
    assert update.allowed('backend/scripts/storage_retention.py')
    assert not update.allowed(name)
    with pytest.raises(ValueError, match='Forbidden update path'):
        update.safe_path(tmp_path, name)


@pytest.mark.skipif(os.name != "nt", reason="Windows shared handle behavior")
def test_windows_inherited_open_writer_is_not_certified_as_closed(tmp_path):
    path = tmp_path/"still-writing.stderr.log"
    with path.open("wb") as writer:
        writer.write(b"child writer still owns handle")
        writer.flush()
        assert control._log_has_no_writer(path) is False
    assert control._log_has_no_writer(path) is True
