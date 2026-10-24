"""Cold evidence is removable only after complete, typed and isolated restore."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from app.storage_retention import bundle


def make_bundle(root, archive_id="2026-09-01", rows=None):
    if rows is None:
        rows = [(1, b"\x00\xff compressed evidence", None, 8.47, "中文灰分")]
    with bundle.ArchiveBundleWriter(root, archive_id, {"period": "2026-09"}) as writer:
        writer.add_rows("evidence", ["id", "payload", "missing", "ash", "label"], iter(rows))
        return writer.finish()


def edit_manifest(path, change):
    data = json.loads(path.read_bytes())
    change(data)
    raw = bundle._json_bytes(data)
    path.write_bytes(raw)
    path.with_name("manifest.sha256").write_text(hashlib.sha256(raw).hexdigest(), encoding="ascii")


def no_staging(root):
    assert not list(root.glob(".*.writing-*"))
    # Stable lock files stay in place; only the live OS handle owns a lock.
    assert all(path.is_file() for path in root.glob(".*.lock"))
    assert not list(root.glob(".archive-verify-*"))


def test_full_restore_preserves_types_blobs_null_and_original_column_metadata(tmp_path):
    columns = [{"name": "id", "type": "INTEGER", "pk": 1},
               {"name": "value", "type": "JSON", "default": "'{}'"}]
    rows = [(1, b"\x00\xff"), (2, None), (3, "001"), (4, 1.25),
            (5, -(2**63)), (6, 2**63-1), (7, float("inf")),
            (8, float("-inf")), (9, "中文\x00\ntext"), (10, -0.0)]
    with bundle.ArchiveBundleWriter(tmp_path / "cold", "2026-09", {"model": "EXPERT8"}) as writer:
        writer.add_rows("readings", columns, iter(rows[:4]))
        writer.add_rows("readings", columns, iter(rows[4:]))
        writer.add_rows("empty_table", ["id"], [])
        manifest_path = writer.finish()
    manifest = bundle.verify_bundle(manifest_path)
    assert manifest["tables"][0]["row_count"] == len(rows)
    assert manifest["tables"][0]["columns"][1]["type"] == "JSON"
    destination = bundle.restore_bundle(manifest_path, tmp_path / "restored.sqlite")
    with sqlite3.connect(destination) as db:
        restored = db.execute("SELECT id,value FROM readings ORDER BY rowid").fetchall()
        assert restored == rows
        assert [type(row[1]) for row in restored] == [type(row[1]) for row in rows]
        assert db.execute("SELECT COUNT(*) FROM empty_table").fetchone() == (0,)
        assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)
    no_staging(tmp_path / "cold")


def test_mapping_rows_and_sqlite_rows_are_supported(tmp_path):
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE readings(id, payload)")
        db.execute("INSERT INTO readings VALUES (?,?)", (1, b"blob"))
        source = db.execute("SELECT * FROM readings")
        with bundle.ArchiveBundleWriter(tmp_path, "mapping") as writer:
            writer.add_rows("readings", ["id", "payload"], source)
            writer.add_rows("readings", ["id", "payload"], [{"payload": None, "id": 2}])
            path = writer.finish()
    restored = bundle.restore_bundle(path, tmp_path / "restored.sqlite")
    with sqlite3.connect(restored) as db:
        assert db.execute("SELECT * FROM readings").fetchall() == [(1, b"blob"), (2, None)]


def test_generator_failure_never_publishes_partial_evidence(tmp_path):
    def broken():
        yield (1,)
        raise RuntimeError("source read interrupted")
    writer = bundle.ArchiveBundleWriter(tmp_path, "interrupted")
    with pytest.raises(RuntimeError, match="interrupted"):
        writer.add_rows("evidence", ["id"], broken())
    assert not (tmp_path / "interrupted").exists()
    no_staging(tmp_path)
    with pytest.raises(bundle.BundleError, match="closed"):
        writer.finish()


def test_restore_verification_failure_does_not_publish(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise bundle.BundleError("isolated restore failed")
    monkeypatch.setattr(bundle, "verify_bundle", fail)
    with bundle.ArchiveBundleWriter(tmp_path, "verification-failed") as writer:
        writer.add_rows("evidence", ["id"], [(1,)])
        with pytest.raises(bundle.BundleError, match="isolated restore"):
            writer.finish()
    assert not (tmp_path / "verification-failed").exists()
    no_staging(tmp_path)


def test_duplicate_writer_and_duplicate_publication_cannot_replace_archive(tmp_path):
    with bundle.ArchiveBundleWriter(tmp_path, "duplicate") as first:
        first.add_rows("evidence", ["id"], [(1,)])
        with pytest.raises(FileExistsError):
            bundle.ArchiveBundleWriter(tmp_path, "duplicate")
        path = first.finish()
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        bundle.ArchiveBundleWriter(tmp_path, "duplicate")
    assert path.read_bytes() == before
    assert bundle.verify_bundle(path)["tables"][0]["row_count"] == 1
    no_staging(tmp_path)


def test_publish_conflict_preserves_existing_directory(tmp_path):
    with bundle.ArchiveBundleWriter(tmp_path, "conflict") as writer:
        writer.add_rows("evidence", ["id"], [(1,)])
        (tmp_path / "conflict").mkdir()
        marker = tmp_path / "conflict" / "marker"
        marker.write_text("belongs to another operation")
        with pytest.raises(FileExistsError):
            writer.finish()
    assert marker.read_text() == "belongs to another operation"
    no_staging(tmp_path)


@pytest.mark.parametrize("archive_id", ["../escape", "/absolute", "C:drive", "..", "CON", "ends.", "a\\b"])
def test_unsafe_archive_names_are_rejected(tmp_path, archive_id):
    with pytest.raises(bundle.BundleError):
        bundle.ArchiveBundleWriter(tmp_path, archive_id)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("table,columns", [
    ("../evidence", ["id"]), ("evidence; DROP TABLE x", ["id"]),
    ("sqlite_master", ["id"]), ("safe", ["x", "X"]),
    ("safe", ["a); DELETE FROM x;--"]),
])
def test_identifiers_never_execute_untrusted_sql(tmp_path, table, columns):
    with bundle.ArchiveBundleWriter(tmp_path, "unsafe") as writer:
        with pytest.raises(bundle.BundleError):
            writer.add_rows(table, columns, [(1,)])
    assert not (tmp_path / "unsafe").exists()
    no_staging(tmp_path)


def test_original_type_declarations_are_data_never_executable_sql(tmp_path):
    with bundle.ArchiveBundleWriter(tmp_path, "declaration") as writer:
        writer.add_rows("readings", [{"name": "value", "type": "TEXT); DROP TABLE secrets;--"}], [("safe",)])
        path = writer.finish()
    restored = bundle.restore_bundle(path, tmp_path / "isolated.sqlite")
    with sqlite3.connect(restored) as db:
        assert db.execute("SELECT value FROM readings").fetchone() == ("safe",)


def test_corrupt_manifest_is_rejected(tmp_path):
    path = make_bundle(tmp_path)
    path.write_bytes(path.read_bytes().replace(b"EXPERT", b"broken") + b" ")
    with pytest.raises(bundle.BundleError, match="Manifest checksum"):
        bundle.verify_bundle(path)
    no_staging(tmp_path)


def test_corrupt_compressed_file_is_rejected_and_no_destination_remains(tmp_path):
    path = make_bundle(tmp_path)
    payload = path.parent / "tables/evidence.jsonl.gz"
    raw = bytearray(payload.read_bytes())
    raw[len(raw)//2] ^= 0xFF
    payload.write_bytes(raw)
    destination = tmp_path / "restored.sqlite"
    with pytest.raises(bundle.BundleError, match="checksum"):
        bundle.restore_bundle(path, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".restore-*"))


@pytest.mark.parametrize("change", [
    lambda m: m["tables"][0].update(path="../../production.sqlite"),
    lambda m: m["tables"][0].update(path="tables\\evidence.jsonl.gz"),
    lambda m: m["tables"][0].update(name="evidence; DROP TABLE x"),
    lambda m: m.update(version=999),
    lambda m: m["tables"][0].update(row_count=-1),
    lambda m: m["tables"][0].update(uncompressed_bytes=bundle.MAX_TOTAL_BYTES+1),
])
def test_validly_checksummed_but_unsafe_manifests_are_rejected(tmp_path, change):
    path = make_bundle(tmp_path)
    edit_manifest(path, change)
    with pytest.raises(bundle.BundleError):
        bundle.verify_bundle(path)
    no_staging(tmp_path)


def test_declared_row_count_and_decoded_checksum_are_both_enforced(tmp_path):
    path = make_bundle(tmp_path)
    edit_manifest(path, lambda m: m["tables"][0].update(row_count=2))
    with pytest.raises(bundle.BundleError, match="count or content"):
        bundle.verify_bundle(path)
    no_staging(tmp_path)


def test_gzip_damage_is_detected_even_when_compressed_checksum_was_updated(tmp_path):
    path = make_bundle(tmp_path)
    member = path.parent / "tables/evidence.jsonl.gz"
    data = bytearray(member.read_bytes())
    data[-8] ^= 1  # Break gzip CRC while preserving member length.
    member.write_bytes(data)
    edit_manifest(path, lambda m: m["tables"][0].update(compressed_sha256=hashlib.sha256(data).hexdigest()))
    with pytest.raises(bundle.BundleError, match="compressed archive"):
        bundle.verify_bundle(path)
    no_staging(tmp_path)


def test_missing_members_are_rejected(tmp_path):
    path = make_bundle(tmp_path)
    (path.parent / "tables/evidence.jsonl.gz").unlink()
    with pytest.raises(bundle.BundleError, match="member"):
        bundle.verify_bundle(path)


def test_member_links_are_rejected(tmp_path):
    path = make_bundle(tmp_path)
    member = path.parent / "tables/evidence.jsonl.gz"
    outside = tmp_path / "outside.gz"
    member.rename(outside)
    try:
        member.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("This Windows account cannot create symbolic links")
    with pytest.raises(bundle.BundleError, match="linked"):
        bundle.verify_bundle(path)


def test_restore_refuses_to_overwrite_any_file_including_source_database(tmp_path):
    path = make_bundle(tmp_path / "cold")
    existing = tmp_path / "production.sqlite"
    existing.write_bytes(b"production stays unchanged")
    with pytest.raises(FileExistsError):
        bundle.restore_bundle(path, existing)
    assert existing.read_bytes() == b"production stays unchanged"
    with pytest.raises(bundle.BundleError, match="outside"):
        bundle.restore_bundle(path, path.parent / "restored.sqlite")


def test_restore_publish_race_does_not_overwrite_other_file(tmp_path, monkeypatch):
    path = make_bundle(tmp_path / "cold")
    target = tmp_path / "restored.sqlite"
    link = bundle.os.link
    def competing_writer(source, destination):
        Path(destination).write_bytes(b"another owner")
        return link(source, destination)
    monkeypatch.setattr(bundle.os, "link", competing_writer)
    with pytest.raises(FileExistsError):
        bundle.restore_bundle(path, target)
    assert target.read_bytes() == b"another owner"
    assert not list(tmp_path.glob(".restore-*"))


@pytest.mark.parametrize("budgets", [{"max_rows": 1}, {"max_uncompressed_bytes": 1}])
def test_bounded_work_aborts_cleanly_before_publication(tmp_path, budgets):
    with bundle.ArchiveBundleWriter(tmp_path, "budget", **budgets) as writer:
        with pytest.raises(bundle.BundleBudgetExceeded):
            writer.add_rows("readings", ["id"], [(1,), (2,)])
    assert not (tmp_path / "budget").exists()
    no_staging(tmp_path)


def test_time_budget_applies_to_work_and_restore(tmp_path, monkeypatch):
    clock = [0]
    monkeypatch.setattr(bundle.time, "monotonic", lambda: clock[0])
    with bundle.ArchiveBundleWriter(tmp_path, "deadline", budget_seconds=5) as writer:
        writer.add_rows("readings", ["id"], [(1,)])
        clock[0] = 6
        with pytest.raises(bundle.BundleBudgetExceeded, match="time"):
            writer.finish()
    assert not (tmp_path / "deadline").exists()
    no_staging(tmp_path)


def test_inconsistent_batches_are_not_a_publishable_archive(tmp_path):
    with bundle.ArchiveBundleWriter(tmp_path, "columns") as writer:
        writer.add_rows("readings", ["id"], [(1,)])
        with pytest.raises(bundle.BundleError, match="changed"):
            writer.add_rows("readings", ["another_id"], [(2,)])
    assert not (tmp_path / "columns").exists()
    no_staging(tmp_path)


def test_abandoned_context_removes_unpublished_files(tmp_path):
    with bundle.ArchiveBundleWriter(tmp_path, "abandoned") as writer:
        writer.add_rows("readings", ["id"], [(1,)])
    assert not (tmp_path / "abandoned").exists()
    no_staging(tmp_path)


def test_rejects_unsupported_values_before_any_archive_is_publishable(tmp_path):
    with bundle.ArchiveBundleWriter(tmp_path, "unsupported") as writer:
        with pytest.raises(bundle.BundleError, match="Unsupported"):
            writer.add_rows("readings", ["id"], [(object(),)])
    assert not (tmp_path / "unsupported").exists()
    no_staging(tmp_path)


def test_large_generator_is_consumed_incrementally_not_as_a_month_in_memory(tmp_path):
    payload = b"unchanged production evidence" * 64
    rows = ((i, payload) for i in range(4000))
    with bundle.ArchiveBundleWriter(tmp_path, "streaming") as writer:
        writer.add_rows("readings", ["id", "payload"], rows)
        path = writer.finish()
    manifest = bundle.verify_bundle(path)
    assert manifest["tables"][0]["row_count"] == 4000
    assert manifest["tables"][0]["compressed_bytes"] < manifest["tables"][0]["uncompressed_bytes"] // 10
    no_staging(tmp_path)


def test_real_column_named_rowid_does_not_change_recovery_order(tmp_path):
    rows = [(20, "first"), (10, "second")]
    with bundle.ArchiveBundleWriter(tmp_path, "rowid-column") as writer:
        writer.add_rows("readings", ["rowid", "value"], rows)
        path = writer.finish()
    restored = bundle.restore_bundle(path, tmp_path / "restored.sqlite")
    with sqlite3.connect(restored) as db:
        assert db.execute("SELECT rowid,value FROM readings ORDER BY _rowid_").fetchall() == rows


def test_restore_refuses_insufficient_free_space_without_partial_output(tmp_path, monkeypatch):
    path = make_bundle(tmp_path / "cold")
    from collections import namedtuple
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(bundle.shutil, "disk_usage", lambda parent: Usage(100, 100, 0))
    target = tmp_path / "restored.sqlite"
    with pytest.raises(bundle.BundleBudgetExceeded, match="space"):
        bundle.restore_bundle(path, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".restore-*"))


def test_declared_uncompressed_digest_is_not_replaced_by_only_gzip_integrity(tmp_path):
    path = make_bundle(tmp_path)
    member = path.parent / "tables/evidence.jsonl.gz"
    cells = json.loads(gzip.decompress(member.read_bytes()))
    cells[-1][1] = "篡改数据"
    content = bundle._json_bytes(cells) + b"\n"
    compressed = gzip.compress(content, mtime=0)
    member.write_bytes(compressed)
    edit_manifest(path, lambda m: m["tables"][0].update(
        uncompressed_bytes=len(content), compressed_bytes=len(compressed),
        compressed_sha256=hashlib.sha256(compressed).hexdigest()))
    with pytest.raises(bundle.BundleError, match="count or content"):
        bundle.verify_bundle(path)
    no_staging(tmp_path)


def test_read_table_preserves_typed_rows_without_restoring_all_tables(tmp_path, monkeypatch):
    path = make_bundle(tmp_path / "cold")
    def forbidden(*args, **kwargs):
        raise AssertionError("Read table must not perform a SQLite restore")
    monkeypatch.setattr(bundle.sqlite3, "connect", forbidden)
    assert list(bundle.read_table(path, "evidence")) == [
        {"id": 1, "payload": b"\x00\xff compressed evidence", "missing": None,
         "ash": 8.47, "label": "中文灰分"}]
    assert not list(path.parent.parent.glob(".archive-read-*"))


def test_read_table_never_yields_a_prefix_of_a_corrupt_table(tmp_path):
    rows = [(i, b"payload", None, 8.47, "row") for i in range(100)]
    path = make_bundle(tmp_path / "cold", rows=rows)
    edit_manifest(path, lambda m: m["tables"][0].update(sha256="0" * 64))
    reader = bundle.read_table(path, "evidence")
    with pytest.raises(bundle.BundleError, match="count or content"):
        next(reader)
    assert not list(path.parent.parent.glob(".archive-read-*"))


def test_read_table_uses_verified_snapshot_even_if_original_is_changed(tmp_path):
    rows = [(i, b"payload", None, 8.47, "row") for i in range(3)]
    path = make_bundle(tmp_path / "cold", rows=rows)
    reader = bundle.read_table(path, "evidence")
    assert next(reader)["id"] == 0
    (path.parent / "tables/evidence.jsonl.gz").write_bytes(b"source changed")
    assert [row["id"] for row in reader] == [1, 2]
    assert not list(path.parent.parent.glob(".archive-read-*"))


def test_read_table_cleanup_on_early_iterator_close_and_missing_table(tmp_path):
    path = make_bundle(tmp_path / "cold", rows=[(i, None, None, 0.0, "row") for i in range(3)])
    reader = bundle.read_table(path, "evidence")
    next(reader)
    reader.close()
    assert not list(path.parent.parent.glob(".archive-read-*"))
    with pytest.raises(bundle.BundleError, match="does not exist"):
        list(bundle.read_table(path, "absent"))


def test_hard_process_exit_releases_archive_lock_and_preserves_unknown_staging(tmp_path):
    code = """
import os, sys
from app.storage_retention.bundle import ArchiveBundleWriter
writer = ArchiveBundleWriter(sys.argv[1], 'hard-exit')
writer.add_rows('readings', ['id'], [(1,)])
os._exit(23)
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 23, result.stderr
    abandoned = list(tmp_path.glob(".hard-exit.writing-*"))
    assert len(abandoned) == 1
    assert (tmp_path / ".hard-exit.lock").is_file()
    # The surviving lock file must not make this archive id permanently busy.
    with bundle.ArchiveBundleWriter(tmp_path, "hard-exit") as writer:
        writer.add_rows("readings", ["id"], [(2,)])
        path = writer.finish()
        writer.abort()  # Root cleanup calls remain harmless after publication.
    assert list(bundle.read_table(path, "readings")) == [{"id": 2}]
    assert abandoned[0].is_dir()
    assert (abandoned[0] / "tables/readings.jsonl.gz").is_file()
    assert list(tmp_path.glob(".hard-exit.writing-*")) == abandoned


def test_live_other_process_holds_exclusive_lock_until_forced_termination(tmp_path):
    ready = tmp_path / "child-ready"
    code = """
import os, sys, time
from pathlib import Path
from app.storage_retention.bundle import ArchiveBundleWriter
writer = ArchiveBundleWriter(sys.argv[1], 'live-owner')
writer.add_rows('readings', ['id'], [(1,)])
Path(sys.argv[2]).write_text(str(os.getpid()))
while True:
    time.sleep(1)
"""
    process = subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(ready)],
                               cwd=Path(__file__).resolve().parents[1],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    worker_pid = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "Child failed to acquire an archive lock"
        worker_pid = int(ready.read_text())
        with pytest.raises(FileExistsError, match="already active"):
            bundle.ArchiveBundleWriter(tmp_path, "live-owner")
        # Windows venv python.exe can be a launcher. Kill the actual worker
        # whose PID it recorded, rather than leaving that owner orphaned.
        os.kill(worker_pid, signal.SIGTERM)
        worker_pid = None
        process.wait(timeout=10)
        with bundle.ArchiveBundleWriter(tmp_path, "live-owner") as writer:
            writer.add_rows("readings", ["id"], [(2,)])
            path = writer.finish()
        assert list(bundle.read_table(path, "readings")) == [{"id": 2}]
    finally:
        if worker_pid is not None:
            try:
                os.kill(worker_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_old_zero_byte_lock_file_is_reusable_without_deleting_it(tmp_path):
    lock = tmp_path / ".old-lock.lock"
    lock.touch()
    with bundle.ArchiveBundleWriter(tmp_path, "old-lock") as writer:
        writer.add_rows("readings", ["id"], [(1,)])
        path = writer.finish()
    assert lock.read_bytes().startswith(b"0")
    assert list(bundle.read_table(path, "readings")) == [{"id": 1}]
