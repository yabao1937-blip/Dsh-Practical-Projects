"""Temporary-only proofs for certified backup and closed-log retention."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess

import pytest

from app.storage_retention import files


NOW = datetime(2026, 10, 23, 6, tzinfo=timezone.utc)
INSTALLATION = "temporary-retention-test"


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "storage with spaces"
    root.mkdir()
    return root


def backup(store, name, age, *, stable=False):
    path = store / name
    path.mkdir()
    (path / "dense_medium.db").write_bytes(b"SQLite format 3\0" + name.encode() * 20)
    (path / "source").mkdir()
    (path / "source" / "old.py").write_text("# previous source", encoding="utf-8")
    created = NOW - timedelta(days=age)
    files.register_verified_backup(store, path, installation_id=INSTALLATION,
        backup_id=name, created_at=created,
        verification={"method": "sqlite-full-check", "result": "ok", "checked_at": created + timedelta(minutes=1)}, stable=stable)
    return path


def closed_log(store, name, age, category="normal"):
    path = store / name
    path.write_text("synthetic closed log\n", encoding="utf-8")
    files.register_closed_log(store, path, installation_id=INSTALLATION,
        category=category, closed_at=NOW-timedelta(days=age), log_id=name)
    return path


def preview(store, **kwargs):
    return files.plan_cleanup(store, installation_id=INSTALLATION, now=NOW, **kwargs)


def execute(store, plan, **kwargs):
    return files.execute_cleanup(store, plan, installation_id=INSTALLATION, now=NOW, **kwargs)


def test_preview_does_not_create_registry_or_adopt_legacy_files(store):
    (store / "unverified.db").write_bytes(b"must stay")
    (store / "source-update-old").mkdir()
    (store / "source-update-old" / "files.json").write_text("{}")
    before = sorted(path.relative_to(store).as_posix() for path in store.rglob("*"))
    plan = preview(store)
    assert not plan["actions"]
    assert "legacy" in plan["warnings"][0]
    assert before == sorted(path.relative_to(store).as_posix() for path in store.rglob("*"))


@pytest.mark.parametrize("verification", [
    {"content_check": "deferred", "passed": False},
    {"method": "sqlite-full-check", "result": "timeout", "checked_at": NOW.isoformat()},
    {"method": "sqlite-structural-check", "result": "ok", "checked_at": NOW.isoformat()},
    {"method": "sqlite-full-check", "result": "ok"},
])
def test_copy_structural_check_and_timeout_are_not_verified(store, verification):
    path = store / "incomplete.db"
    path.write_bytes(b"synthetic")
    with pytest.raises((files.RetentionFileError, ValueError, TypeError)):
        files.register_verified_backup(store, path, installation_id=INSTALLATION,
            backup_id="not-verified", created_at=NOW-timedelta(days=2), verification=verification)
    assert path.exists()
    assert not preview(store)["actions"]


def test_latest_two_and_explicit_stable_are_retained_regardless_of_mtime(store):
    oldest = backup(store, "stable", 50, stable=True)
    obsolete = backup(store, "obsolete", 40)
    recent = backup(store, "recent", 3)
    newest = backup(store, "newest", 1)
    # All mtimes are recent and cannot be used to select the stable point.
    legacy = store / "legacy.db"
    legacy.write_bytes(b"unknown recovery copy")
    plan = preview(store)
    assert [item["id"] for item in plan["actions"]] == ["obsolete"]
    result = execute(store, plan)
    assert result["deleted"] == ["obsolete"]
    assert oldest.exists() and recent.exists() and newest.exists() and legacy.exists()
    assert not obsolete.exists()
    audit = [json.loads(line) for line in (store/files.AUDIT_NAME).read_text().splitlines()]
    assert [item["event"] for item in audit] == ["delete_started", "delete_completed"]


def test_no_stable_point_disables_all_backup_deletion(store):
    for index in range(4):
        backup(store, "backup-"+str(index), 10-index)
    plan = preview(store)
    assert not plan["actions"]
    assert any("stable" in warning for warning in plan["warnings"])


def test_changed_stable_point_disables_backup_deletion(store):
    stable = backup(store, "stable", 50, stable=True)
    backup(store, "obsolete", 40)
    backup(store, "recent", 3)
    backup(store, "newest", 1)
    (stable/"dense_medium.db").write_bytes(b"changed stable snapshot")
    assert not preview(store)["actions"]


def test_stable_reselection_is_explicit_and_changes_preview(store):
    backup(store, "stable", 50, stable=True)
    backup(store, "obsolete", 40)
    backup(store, "recent", 3)
    backup(store, "newest", 1)
    plan = preview(store)
    files.set_stable_backup(store, "obsolete", installation_id=INSTALLATION)
    with pytest.raises(files.RetentionFileError, match="Registry changed"):
        execute(store, plan)
    assert [item["id"] for item in preview(store)["actions"]] == ["stable"]


def test_log_windows_are_30_and_90_days_with_strict_cutoff(store):
    normal_expired = closed_log(store, "old.stdout.log", 31)
    normal_boundary = closed_log(store, "boundary.stdout.log", 30)
    error_recent = closed_log(store, "recent.stderr.log", 31, "error")
    error_expired = closed_log(store, "old.stderr.log", 91, "error")
    error_boundary = closed_log(store, "boundary.stderr.log", 90, "error")
    plan = preview(store)
    assert {item["id"] for item in plan["actions"]} == {"old.stdout.log", "old.stderr.log"}
    execute(store, plan)
    assert not normal_expired.exists() and not error_expired.exists()
    assert normal_boundary.exists() and error_recent.exists() and error_boundary.exists()


def test_current_and_active_logs_cannot_be_registered_or_deleted(store):
    path = store / "dmcs_server.log"
    path.write_bytes(b"active shared log")
    with pytest.raises(files.RetentionFileError, match="Active and current"):
        files.register_closed_log(store, path, installation_id=INSTALLATION,
            category="normal", closed_at=NOW-timedelta(days=500))
    closed = closed_log(store, "prior.stdout.log", 31)
    assert not preview(store, active_paths=[closed])["actions"]
    plan = preview(store)
    with pytest.raises(files.RetentionFileError, match="no longer eligible"):
        execute(store, plan, active_paths=[closed])
    assert closed.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows paths are case-insensitive")
def test_case_alias_does_not_bypass_active_path_or_overlap_protection(store):
    path = closed_log(store, "Prior.stdout.log", 31)
    assert not preview(store, active_paths=[store/"PRIOR.STDOUT.LOG"])["actions"]
    with pytest.raises(files.RetentionFileError, match="overlap"):
        files.register_closed_log(store, store/"PRIOR.STDOUT.LOG", installation_id=INSTALLATION,
            category="normal", closed_at=NOW-timedelta(days=40), log_id="alias")
    assert path.exists()


def test_file_mutation_or_new_file_after_preview_is_refused(store):
    path = closed_log(store, "mutating.stdout.log", 31)
    plan = preview(store)
    path.write_bytes(b"writer resumed; do not delete")
    with pytest.raises(files.RetentionFileError, match="no longer eligible"):
        execute(store, plan)
    assert path.exists()


def test_unregistered_addition_inside_backup_protects_entire_item(store):
    backup(store, "stable", 50, stable=True)
    obsolete = backup(store, "obsolete", 40)
    backup(store, "recent", 3)
    backup(store, "newest", 1)
    plan = preview(store)
    (obsolete / "new-unregistered.db").write_bytes(b"cannot recursively remove this")
    with pytest.raises(files.RetentionFileError, match="no longer eligible"):
        execute(store, plan)
    assert (obsolete/"dense_medium.db").exists()
    assert (obsolete/"new-unregistered.db").exists()


def test_manifest_mutation_installation_mismatch_and_path_escape_refused(store, tmp_path):
    path = closed_log(store, "old.stdout.log", 31)
    plan = preview(store)
    registry_path = store/files.REGISTRY_NAME
    registry_path.write_bytes(registry_path.read_bytes()+b"\n")
    with pytest.raises(files.RetentionFileError, match="Registry changed"):
        execute(store, plan)
    with pytest.raises(files.RetentionFileError, match="Unrecognized"):
        files.plan_cleanup(store, installation_id="another-installation", now=NOW)
    outside = tmp_path / "external.log"
    outside.write_bytes(b"outside")
    with pytest.raises(files.RetentionFileError, match="inside"):
        files.register_closed_log(store, outside, installation_id=INSTALLATION,
            category="normal", closed_at=NOW-timedelta(days=31))
    assert path.exists() and outside.exists()


def test_edited_preview_cannot_delete_latest_backup_or_replay_actions(store):
    backup(store, "stable", 50, stable=True)
    backup(store, "obsolete", 40)
    backup(store, "recent", 3)
    newest = backup(store, "newest", 1)
    plan = preview(store)
    plan["actions"][0].update(id="newest", path="newest")
    with pytest.raises(files.RetentionFileError, match="no longer eligible"):
        execute(store, plan)
    assert newest.exists()
    plan = preview(store)
    plan["actions"].append(dict(plan["actions"][0]))
    with pytest.raises(files.RetentionFileError, match="duplicate"):
        execute(store, plan)


def test_byte_and_item_budgets_do_not_start_oversized_items(store):
    first = closed_log(store, "first.stdout.log", 40)
    second = closed_log(store, "second.stdout.log", 35)
    assert not preview(store, max_bytes=1)["actions"]
    plan = preview(store, max_items=1)
    assert len(plan["actions"]) == 1
    result = execute(store, plan, max_items=1)
    assert len(result["deleted"]) == 1
    assert not first.exists() and second.exists()


def test_smaller_execution_budget_defers_the_remaining_preview(store):
    first = closed_log(store, "first.stdout.log", 40)
    second = closed_log(store, "second.stdout.log", 35)
    plan = preview(store, max_items=2)
    result = execute(store, plan, max_items=1)
    assert result["deleted"] == ["first.stdout.log"]
    assert result["deferred"] == ["second.stdout.log"]
    assert not first.exists() and second.exists()


def test_registry_change_at_delete_intent_refuses_before_unlink(store, monkeypatch):
    path = closed_log(store, "old.stdout.log", 31)
    plan = preview(store)
    real_audit = files._audit
    def mutate_after_intent(root, event):
        real_audit(root, event)
        registry_path = root/files.REGISTRY_NAME
        registry_path.write_bytes(registry_path.read_bytes()+b"\n")
    monkeypatch.setattr(files, "_audit", mutate_after_intent)
    with pytest.raises(files.RetentionFileError, match="immediately before deletion"):
        execute(store, plan)
    assert path.exists()


@pytest.mark.parametrize("budget", [float("nan"), float("inf"), 0, True])
def test_unbounded_or_invalid_time_budget_is_refused(store, budget):
    path = closed_log(store, "old.stdout.log", 31)
    with pytest.raises(files.RetentionFileError, match="time budget"):
        execute(store, preview(store), max_seconds=budget)
    assert path.exists()


def test_partial_failure_is_audited_and_never_counts_as_verified_or_retried(store, monkeypatch):
    backup(store, "stable", 50, stable=True)
    obsolete = backup(store, "obsolete", 40)
    backup(store, "recent", 3)
    backup(store, "newest", 1)
    plan = preview(store)
    original_unlink = Path.unlink
    def fail_one(path, *args, **kwargs):
        if path.name == "old.py":
            raise PermissionError("synthetic open writer")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_one)
    with pytest.raises(PermissionError, match="writer"):
        execute(store, plan)
    registry = json.loads((store/files.REGISTRY_NAME).read_text())
    assert registry["entries"]["obsolete"]["state"] == "deleting"
    assert obsolete.exists()
    assert not preview(store)["actions"]
    assert "delete_started" in (store/files.AUDIT_NAME).read_text()


def test_old_or_future_plan_and_existing_lock_refuse_execution(store):
    path = closed_log(store, "old.stdout.log", 31)
    plan = preview(store)
    for when in (NOW-timedelta(seconds=1), NOW+timedelta(days=2)):
        with pytest.raises(files.RetentionFileError, match="Invalid, future, or expired"):
            files.execute_cleanup(store, plan, installation_id=INSTALLATION, now=when)
    (store/files.LOCK_NAME).write_text("other task")
    with pytest.raises(files.RetentionFileError, match="busy"):
        execute(store, plan)
    assert path.exists()
    assert (store/files.LOCK_NAME).exists()


def test_symlinks_and_hardlinks_are_never_followed(store, tmp_path):
    outside = tmp_path / "outside.log"
    outside.write_bytes(b"outside evidence")
    linked = store / "alias.stdout.log"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks require Windows developer mode or elevated rights")
    with pytest.raises(files.RetentionFileError, match="Symlinks"):
        files.register_closed_log(store, linked, installation_id=INSTALLATION,
            category="normal", closed_at=NOW-timedelta(days=40))
    linked.unlink()
    os.link(outside, linked)
    with pytest.raises(files.RetentionFileError, match="regular"):
        files.register_closed_log(store, linked, installation_id=INSTALLATION,
            category="normal", closed_at=NOW-timedelta(days=40))
    assert outside.read_bytes() == b"outside evidence"


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction behavior")
def test_junction_root_is_refused_without_touching_target(store, tmp_path):
    target = tmp_path / "real storage"
    target.mkdir()
    (target / "precious.db").write_bytes(b"production")
    junction = store / "junction"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)], capture_output=True)
    if result.returncode:
        pytest.skip("Could not create temporary NTFS junction")
    try:
        with pytest.raises(files.RetentionFileError, match="junctions"):
            preview(junction)
        assert (target/"precious.db").read_bytes() == b"production"
    finally:
        os.rmdir(junction)
