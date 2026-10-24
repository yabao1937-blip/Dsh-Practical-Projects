"""Explicit, fail-closed retention for this installation's backups and closed logs.

Legacy files are never adopted by filename or modification time.  The producer
registers a completed backup only after its full SQLite content check succeeds,
and explicitly designates a stable recovery point.  Registration records file
identities, not another expensive full-database scan.  Plans are JSON-serializable
audit artifacts; execution rechecks both the registry and the current filesystem.
No SQLite database is opened here and no service is stopped.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import stat
import threading
import time
import uuid


PROJECT = "dense-medium-density-control-system"
SCHEMA = 1
REGISTRY_NAME = ".dmcs-retention-registry.json"
LOCK_NAME = ".dmcs-retention.lock"
AUDIT_NAME = ".dmcs-retention-audit.jsonl"
MAX_REGISTRY_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_FILES = 10000
NEVER_CLOSED_NAMES = {"dmcs_server.log", "current.log", "active.log", "latest.log"}


class RetentionFileError(ValueError):
    """A destructive operation was refused; callers should report and retain."""


def installation_identity(install_root):
    """Stable across the C-to-D data move; distinct for separate installations."""
    canonical = os.path.normcase(str(Path(install_root).resolve(strict=True)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def backup_registry_root(install_root, db_path):
    """One installation-wide registry for update and migration recovery copies.

    Resolve the explicitly configured producer locations, including approved
    C-to-D junctions, before selecting their shared real ancestor. Destructive
    operations still reject junctions within that real root. Cross-volume or
    drive-root scopes cannot be unified safely and remain protected/unknown.
    Existing per-directory registries are never adopted or rewritten.
    """
    install_root = Path(install_root).resolve(strict=True)
    source_backups = (install_root / "backups").resolve()
    migration_backups = (Path(db_path).resolve().parent / "backups").resolve()
    try:
        common = Path(os.path.commonpath((str(source_backups), str(migration_backups))))
    except ValueError as exc:
        raise RetentionFileError("Backup locations on different volumes remain protected") from exc
    if common == Path(common.anchor) or not common.anchor:
        raise RetentionFileError("A drive root cannot be a backup registry scope")
    return _root(common)


def web_log_archive_root(install_root, log_path=None):
    # Current stays at DMCS_LOG for existing diagnostics. Only closed segments
    # use the configured data junction, so a C-to-D data move also moves growth.
    return (Path(install_root).resolve(strict=True) / "backend/data/server-logs").resolve()


def _utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RetentionFileError("Invalid timestamp") from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RetentionFileError("An explicit timezone-aware timestamp is required")
    return value.astimezone(timezone.utc)


def _iso(value):
    return _utc(value).isoformat()


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _reparse(metadata):
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def _root(root):
    path = Path(os.path.abspath(root))
    # Inspect lexical ancestors before resolve(), which would hide a junction.
    for node in reversed((path, *path.parents)):
        try:
            metadata = node.lstat()
        except OSError as exc:
            raise RetentionFileError("Storage root is missing or inaccessible") from exc
        if _reparse(metadata):
            raise RetentionFileError("Storage roots cannot traverse symlinks or junctions")
    if not path.is_dir():
        raise RetentionFileError("Storage root must be an existing directory")
    return path


def _identity(path):
    metadata = path.lstat()
    return {"device": metadata.st_dev, "inode": metadata.st_ino}


def _relative(root, path):
    supplied = Path(path)
    if not supplied.is_absolute():
        if any(part in (".", "..") for part in supplied.parts) or PureWindowsPath(str(path)).drive:
            raise RetentionFileError("Paths must stay strictly inside the storage root")
        supplied = root / supplied
    try:
        relative = supplied.relative_to(root)
    except ValueError as exc:
        raise RetentionFileError("Paths must stay strictly inside the storage root") from exc
    if not relative.parts or any(part in (".", "..") for part in relative.parts):
        raise RetentionFileError("The storage root itself cannot be a retention item")
    return relative.as_posix()


def _safe(root, relative, *, missing_ok=False):
    relative = _relative(root, relative)
    current = root
    for part in Path(relative).parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if missing_ok:
                continue
            raise RetentionFileError("Registered path is missing") from None
        if _reparse(metadata):
            raise RetentionFileError("Symlinks and junctions are protected")
    return current


def _signature(path):
    metadata = path.lstat()
    if _reparse(metadata) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RetentionFileError("Only regular, unlinked closed files can be registered")
    return {"size": metadata.st_size, "mtime_ns": metadata.st_mtime_ns,
            "ctime_ns": metadata.st_ctime_ns, "device": metadata.st_dev,
            "inode": metadata.st_ino}


def _snapshot(root, relative):
    path = _safe(root, relative)
    files, directories = {}, []
    if path.is_file():
        files[relative] = _signature(path)
    elif path.is_dir():
        pending = [path]
        while pending:
            directory = pending.pop()
            directories.append(directory.relative_to(root).as_posix())
            with os.scandir(directory) as children:
                for child in children:
                    item = _safe(root, Path(child.path).relative_to(root).as_posix())
                    metadata = item.lstat()
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append(item)
                    else:
                        files[item.relative_to(root).as_posix()] = _signature(item)
                    if len(files) + len(directories) + len(pending) > MAX_MANIFEST_FILES:
                        raise RetentionFileError("Item exceeds the manifest file budget")
    else:
        raise RetentionFileError("Unsupported retention item")
    if not files:
        raise RetentionFileError("Empty retention items cannot be certified")
    return {"files": dict(sorted(files.items())), "directories": sorted(directories)}


def _active(root, active_paths):
    result = set()
    for item in active_paths:
        path = Path(item)
        if path.is_absolute():
            try:
                path = path.relative_to(root)
            except ValueError:
                continue  # A different, explicit root owns this active file.
        result.add(_relative(root, path))
    return result


def _overlaps(first, second):
    a = tuple(os.path.normcase(part) for part in Path(first).parts)
    b = tuple(os.path.normcase(part) for part in Path(second).parts)
    return a == b or a[:len(b)] == b or b[:len(a)] == a


def _active_item(relative, active):
    return any(_overlaps(relative, item) for item in active)


def _load(root, installation_id, *, create=False):
    if not isinstance(installation_id, str) or not installation_id.strip():
        raise RetentionFileError("An installation identity is required")
    path = _safe(root, REGISTRY_NAME, missing_ok=True)
    if not path.exists():
        if create:
            return {"schema": SCHEMA, "project": PROJECT, "installation_id": installation_id,
                    "root_identity": _identity(root), "stable_backup_id": None, "entries": {}}, None
        return None, None
    if path.stat().st_size > MAX_REGISTRY_BYTES:
        raise RetentionFileError("Registry exceeds the read budget")
    _signature(path)
    raw = path.read_bytes()
    try:
        registry = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise RetentionFileError("Invalid retention registry") from exc
    if (registry.get("schema") != SCHEMA or registry.get("project") != PROJECT
            or registry.get("installation_id") != installation_id
            or registry.get("root_identity") != _identity(root)
            or not isinstance(registry.get("entries"), dict)):
        raise RetentionFileError("Unrecognized project registry or storage root")
    return registry, _digest(raw)


def _save(root, registry):
    path = _safe(root, REGISTRY_NAME, missing_ok=True)
    temporary = root / (".dmcs-retention-" + uuid.uuid4().hex + ".tmp")
    raw = _json_bytes(registry)
    if len(raw) > MAX_REGISTRY_BYTES:
        raise RetentionFileError("Registry exceeds the write budget")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return _digest(raw)


@contextmanager
def _locked(root):
    path = _safe(root, LOCK_NAME, missing_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RetentionFileError("Retention is busy; existing locks are never stolen") from exc
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        yield
    finally:
        os.close(descriptor)
        path.unlink(missing_ok=True)


def _new_entry(registry, entry_id, relative):
    if not isinstance(entry_id, str) or not entry_id or len(entry_id) > 200:
        raise RetentionFileError("An explicit bounded item identity is required")
    if entry_id in registry["entries"]:
        raise RetentionFileError("Existing registrations cannot be overwritten")
    for entry in registry["entries"].values():
        if entry.get("state") != "deleted" and _overlaps(relative, entry["path"]):
            raise RetentionFileError("Registered retention items cannot overlap")
    if Path(relative).parts[0].startswith(".dmcs-retention"):
        raise RetentionFileError("Retention metadata is protected")


def register_verified_backup(root, path, *, installation_id, backup_id, created_at,
                             verification, stable=False, active_paths=()):
    """Certify an immutable full backup using the producer's successful check.

    ``verification`` must be the actual full-check result, never a structural
    fingerprint, deferred flag, timeout or merely successful copy. ``stable`` is
    an operator/producer choice, not inferred from the file's age or mtime.
    """
    root = _root(root)
    if not isinstance(verification, dict) or verification.get("method") != "sqlite-full-check" or verification.get("result") != "ok":
        raise RetentionFileError("Only a successful full SQLite content check certifies a backup")
    checked_at = _utc(verification.get("checked_at"))
    created_at = _utc(created_at)
    if checked_at < created_at:
        raise RetentionFileError("Verification cannot precede backup creation")
    relative = _relative(root, path)
    if _active_item(relative, _active(root, active_paths)):
        raise RetentionFileError("Active files are protected")
    with _locked(root):
        registry, _ = _load(root, installation_id, create=True)
        _new_entry(registry, backup_id, relative)
        snapshot = _snapshot(root, relative)
        if not any(name.endswith(".db") for name in snapshot["files"]):
            raise RetentionFileError("Full database backup is absent")
        entry = {"id": backup_id, "path": relative, "kind": "backup", "state": "closed",
                 "created_at": _iso(created_at), "closed_at": _iso(checked_at),
                 "verification": {"method": "sqlite-full-check", "result": "ok", "checked_at": _iso(checked_at)},
                 "snapshot": snapshot}
        registry["entries"][backup_id] = entry
        if stable:
            registry["stable_backup_id"] = backup_id
        elif registry.get("stable_backup_id") is None:
            _choose_initial_stable(root, registry)
        _save(root, registry)
        return entry


def register_unverified_backup(root, path, *, installation_id, backup_id, created_at, reason):
    """Record a completed copy's unknown content status without scanning it."""
    root = _root(root)
    relative = _relative(root, path)
    if not isinstance(reason, str) or not reason.strip():
        raise RetentionFileError("An explicit protection reason is required")
    with _locked(root):
        registry, _ = _load(root, installation_id, create=True)
        _new_entry(registry, backup_id, relative)
        entry = {"id": backup_id, "path": relative, "kind": "backup", "state": "unverified",
                 "created_at": _iso(created_at), "protection_reason": reason[:500]}
        registry["entries"][backup_id] = entry
        _save(root, registry)
        return entry


def mark_backup_runtime_verified(root, backup_id, *, installation_id, verified_at,
                                  select_stable_if_ready=True):
    """Record actual project startup checks; never infer success from file age.

    The first explicitly runtime-validated update may become the initial stable
    point only once at least two intact full-checked backups exist. An existing
    stable designation is never silently replaced, even if it becomes invalid.
    """
    root = _root(root)
    with _locked(root):
        registry, _ = _load(root, installation_id)
        entry = registry and registry["entries"].get(backup_id)
        if (not entry or entry.get("kind") != "backup" or entry.get("state") != "closed"
                or _snapshot(root, entry["path"]) != entry["snapshot"]):
            raise RetentionFileError("Runtime validation requires an intact certified backup")
        entry["runtime_verified_at"] = _iso(verified_at)
        if select_stable_if_ready and registry.get("stable_backup_id") is None:
            _choose_initial_stable(root, registry)
        _save(root, registry)
        return registry.get("stable_backup_id")


def _choose_initial_stable(root, registry):
    intact = []
    for candidate in registry["entries"].values():
        if (candidate.get("kind") != "backup" or candidate.get("state") != "closed"
                or candidate.get("verification", {}).get("result") != "ok"
                or candidate.get("verification", {}).get("method") != "sqlite-full-check"):
            continue
        try:
            if _snapshot(root, candidate["path"]) == candidate["snapshot"]:
                intact.append(candidate)
        except (RetentionFileError, OSError):
            continue
    runtime_validated = [candidate for candidate in intact if candidate.get("runtime_verified_at")]
    if len(intact) >= 2 and runtime_validated:
        registry["stable_backup_id"] = min(runtime_validated,
            key=lambda value: (_utc(value["runtime_verified_at"]), value["id"]))["id"]


def set_stable_backup(root, backup_id, *, installation_id):
    """Explicitly select a currently intact, certified stable rollback point."""
    root = _root(root)
    with _locked(root):
        registry, _ = _load(root, installation_id)
        entry = registry and registry["entries"].get(backup_id)
        if not entry or entry.get("kind") != "backup" or entry.get("state") != "closed" or _snapshot(root, entry["path"]) != entry["snapshot"]:
            raise RetentionFileError("Stable point must be an intact certified backup")
        registry["stable_backup_id"] = backup_id
        _save(root, registry)


def register_closed_log(root, path, *, installation_id, category, closed_at,
                        log_id=None, active_paths=()):
    """Register only after the writer closes the file; stderr uses 90 days.

    Producer metadata supplies the category. Unknown files and current shared
    web logs are protected even if they happen to have an old modification time.
    """
    root = _root(root)
    if category not in ("normal", "error"):
        raise RetentionFileError("Explicit normal/error log classification is required")
    relative = _relative(root, path)
    if Path(relative).name.lower() in NEVER_CLOSED_NAMES or _active_item(relative, _active(root, active_paths)):
        raise RetentionFileError("Active and current logs are protected")
    with _locked(root):
        registry, _ = _load(root, installation_id, create=True)
        log_id = log_id or ("log:" + relative)
        _new_entry(registry, log_id, relative)
        snapshot = _snapshot(root, relative)
        if len(snapshot["files"]) != 1 or snapshot["directories"]:
            raise RetentionFileError("Closed logs must be individual regular files")
        entry = {"id": log_id, "path": relative, "kind": "log", "state": "closed",
                 "category": category, "closed_at": _iso(closed_at), "snapshot": snapshot}
        registry["entries"][log_id] = entry
        _save(root, registry)
        return entry


def _budget(max_items, max_bytes):
    if isinstance(max_items, bool) or isinstance(max_bytes, bool) or not isinstance(max_items, int) or not isinstance(max_bytes, int) or max_items < 1 or max_bytes < 1:
        raise RetentionFileError("Positive integer item and byte budgets are required")


def plan_cleanup(root, *, installation_id, now=None, active_paths=(), max_items=4,
                 max_bytes=16 * 1024**3, minimum_closed_seconds=300):
    """Preview only: no registry creation, file mutation, or legacy adoption."""
    root = _root(root)
    now = _utc(now or datetime.now(timezone.utc))
    _budget(max_items, max_bytes)
    if isinstance(minimum_closed_seconds, bool) or not isinstance(minimum_closed_seconds, int) or minimum_closed_seconds < 0:
        raise RetentionFileError("Closed-file safety delay cannot be negative")
    registry, digest = _load(root, installation_id)
    active = _active(root, active_paths)
    plan = {"schema": SCHEMA, "project": PROJECT, "installation_id": installation_id,
            "root": str(root), "root_identity": _identity(root), "registry_sha256": digest,
            "created_at": _iso(now), "active_paths": sorted(active),
            "budget": {"max_items": max_items, "max_bytes": max_bytes,
                       "minimum_closed_seconds": minimum_closed_seconds},
            "actions": [], "protected": [], "warnings": []}
    if registry is None:
        plan["warnings"].append("No certified registry; all legacy files are protected")
        return plan
    valid = {}
    for entry_id, entry in registry["entries"].items():
        reason = None
        try:
            if entry.get("state") != "closed":
                reason = "not_closed"
            elif _active_item(entry["path"], active):
                reason = "active"
            elif Path(entry["path"]).name.lower() in NEVER_CLOSED_NAMES:
                reason = "current_log"
            elif _snapshot(root, entry["path"]) != entry["snapshot"]:
                reason = "file_signature_changed"
            elif _utc(entry["closed_at"]) > now - timedelta(seconds=minimum_closed_seconds):
                reason = "recently_closed"
            elif entry.get("kind") == "backup":
                evidence = entry.get("verification", {})
                if evidence.get("method") != "sqlite-full-check" or evidence.get("result") != "ok":
                    reason = "backup_not_fully_verified"
                elif _utc(evidence["checked_at"]) < _utc(entry["created_at"]):
                    reason = "invalid_verification_time"
            elif entry.get("kind") != "log" or entry.get("category") not in ("normal", "error"):
                reason = "unrecognized_kind"
        except (KeyError, TypeError, ValueError, OSError):
            reason = "unrecognized_or_changed_manifest"
        if reason:
            plan["protected"].append({"id": entry_id, "path": entry.get("path"), "reason": reason})
        else:
            valid[entry_id] = entry
    backups = sorted((entry for entry in valid.values() if entry["kind"] == "backup"),
                     key=lambda entry: (_utc(entry["created_at"]), entry["id"]), reverse=True)
    newest = {entry["id"] for entry in backups[:2]}
    stable = registry.get("stable_backup_id")
    stable_valid = stable in valid and valid[stable]["kind"] == "backup"
    if not stable_valid:
        plan["warnings"].append("No intact explicitly selected stable backup; backup deletion is disabled")
    candidates = []
    for entry_id, entry in valid.items():
        if entry["kind"] == "backup":
            reason = "stable_backup" if entry_id == stable else "latest_verified_backup" if entry_id in newest else "no_stable_backup" if not stable_valid else None
        else:
            days = 90 if entry["category"] == "error" else 30
            reason = "within_log_retention" if _utc(entry["closed_at"]) >= now - timedelta(days=days) else None
        if reason:
            plan["protected"].append({"id": entry_id, "path": entry["path"], "reason": reason})
        else:
            candidates.append(entry)
    used = 0
    for entry in sorted(candidates, key=lambda item: (_utc(item["closed_at"]), item["id"])):
        size = sum(item["size"] for item in entry["snapshot"]["files"].values())
        if len(plan["actions"]) >= max_items or used + size > max_bytes:
            plan["protected"].append({"id": entry["id"], "path": entry["path"], "reason": "batch_budget"})
            continue
        plan["actions"].append({"id": entry["id"], "kind": entry["kind"], "path": entry["path"],
                                "bytes": size, "reason": "superseded_verified_backup" if entry["kind"] == "backup" else "expired_closed_log"})
        used += size
    plan["total_bytes"] = used
    plan["warnings"].append("Unregistered files are legacy or unknown and remain protected")
    return plan


def _audit(root, event):
    path = _safe(root, AUDIT_NAME, missing_ok=True)
    if path.exists():
        _signature(path)
    with path.open("ab") as stream:
        stream.write(_json_bytes(event) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def execute_cleanup(root, plan, *, installation_id, now=None, active_paths=(),
                    max_items=4, max_bytes=16 * 1024**3, max_seconds=10):
    """Execute an unchanged preview using bounded, explicit unlink operations.

    Interrupted partial items stay marked ``deleting`` and cannot count as a
    recovery point or be silently retried. No recursive deletion helper follows
    an unexpected directory, junction, file, or newly created path.
    """
    root = _root(root)
    now = _utc(now or datetime.now(timezone.utc))
    _budget(max_items, max_bytes)
    if isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)) or not math.isfinite(max_seconds) or max_seconds <= 0:
        raise RetentionFileError("A positive execution time budget is required")
    if (plan.get("schema") != SCHEMA or plan.get("project") != PROJECT
            or plan.get("installation_id") != installation_id or plan.get("root") != str(root)
            or plan.get("root_identity") != _identity(root)
            or not timedelta(0) <= now - _utc(plan["created_at"]) <= timedelta(hours=24)):
        raise RetentionFileError("Invalid, future, or expired cleanup preview")
    result = {"started_at": _iso(now), "deleted": [], "deferred": [], "bytes_deleted": 0}
    started = time.monotonic()
    with _locked(root):
        registry, digest = _load(root, installation_id)
        if registry is None or digest != plan.get("registry_sha256"):
            raise RetentionFileError("Registry changed after preview")
        active = set(plan.get("active_paths", ())) | _active(root, active_paths)
        actions = plan.get("actions", [])
        if not isinstance(actions, list) or not all(isinstance(item, dict) for item in actions) or len({item.get("id") for item in actions}) != len(actions):
            raise RetentionFileError("Invalid or duplicate cleanup actions")
        # Eligibility and this execution's budget are separate: accepting a
        # smaller execution budget must defer the rest of an otherwise valid
        # preview, never make protected items eligible or silently expand it.
        preview_budget = plan.get("budget", {})
        current = plan_cleanup(root, installation_id=installation_id, now=now, active_paths=active,
                               max_items=max(max_items, preview_budget.get("max_items", max_items)),
                               max_bytes=max(max_bytes, preview_budget.get("max_bytes", max_bytes)),
                               minimum_closed_seconds=max(300, plan.get("budget", {}).get("minimum_closed_seconds", 300)))
        allowed = {item["id"]: item for item in current["actions"]}
        for item in actions:
            if item != allowed.get(item.get("id")):
                raise RetentionFileError("Preview action is no longer eligible")
        expected_digest = digest
        for item in actions:
            if (len(result["deleted"]) >= max_items or result["bytes_deleted"] + item["bytes"] > max_bytes
                    or time.monotonic() - started >= max_seconds):
                result["deferred"].append(item["id"])
                continue
            _root(root)
            observed, observed_digest = _load(root, installation_id)
            if observed_digest != expected_digest:
                raise RetentionFileError("Registry changed during execution")
            entry = observed["entries"][item["id"]]
            if _active_item(entry["path"], active) or _snapshot(root, entry["path"]) != entry["snapshot"]:
                raise RetentionFileError("Item changed or became active after preview")
            # Persist intent before unlinking: a partial item is never advertised
            # as a valid restore point, even when the process loses power.
            entry["state"] = "deleting"
            expected_digest = _save(root, observed)
            _audit(root, {"event": "delete_started", "at": _iso(now), **item})
            if _load(root, installation_id)[1] != expected_digest:
                raise RetentionFileError("Registry changed immediately before deletion")
            snapshot = entry["snapshot"]
            for relative, signature in snapshot["files"].items():
                path = _safe(root, relative)
                if _signature(path) != signature:
                    raise RetentionFileError("File changed immediately before deletion")
                path.unlink()
            for relative in sorted(snapshot["directories"], key=lambda value: len(Path(value).parts), reverse=True):
                _safe(root, relative).rmdir()  # Unexpected new children cause refusal.
            entry["state"] = "deleted"
            entry["deleted_at"] = _iso(now)
            expected_digest = _save(root, observed)
            _audit(root, {"event": "delete_completed", "at": _iso(now), **item})
            registry = observed
            result["deleted"].append(item["id"])
            result["bytes_deleted"] += item["bytes"]
    return result


class RotatingWebLog:
    """Line-buffered shared stdout/stderr with closed-file certification.

    Each process takes a writer lease before touching the current log. Old
    unowned startup contents are moved aside but remain unregistered/retained.
    Only files written and closed by this producer are eligible for 90-day
    error-log retention. Rotation occurs at startup, daily, or after 32 MiB.
    """
    encoding = "utf-8"
    errors = "backslashreplace"

    def __init__(self, log_path, install_root, *, max_bytes=32 * 1024**2,
                 copy_budget_bytes=64 * 1024**2, clock=None):
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise RetentionFileError("A positive log rotation size is required")
        self.path = Path(log_path).absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._parent = _root(self.path.parent)
        _safe(self._parent, self.path.name, missing_ok=True)
        self.root = web_log_archive_root(install_root, self.path)
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = _root(self.root)
        self.installation_id = installation_identity(install_root)
        self.max_bytes = max_bytes
        if isinstance(copy_budget_bytes, bool) or not isinstance(copy_budget_bytes, int) or copy_budget_bytes < 1:
            raise RetentionFileError("A positive cross-volume copy budget is required")
        self.copy_budget_bytes = copy_budget_bytes
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._guard = threading.RLock()
        self._stream = None
        self._closed = False
        self._owned_current = False
        self.last_registration_error = None
        self.last_rotation_error = None
        self._rotation_retry_after = 0
        lease_path = _safe(self._parent, self.path.name + ".writer.lock", missing_ok=True)
        self._lease = lease_path.open("a+b")
        try:
            if self._lease.tell() == 0:
                self._lease.write(b"0")
                self._lease.flush()
            self._lease.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lease.close()
            raise RetentionFileError("Another process owns the current web log") from exc
        try:
            if self.path.exists() and self.path.stat().st_size > 3:
                closed_at = self._closed_proof()
                self._owned_current = closed_at is not None
                self._rotate(certify=self._owned_current, reopen=False, closed_at=closed_at)
            self._open()
        except BaseException:
            self._release_lease()
            raise

    def _open(self):
        _safe(self._parent, self.path.name, missing_ok=True)
        existing_size = self.path.stat().st_size if self.path.exists() else 0
        self._owned_current = existing_size <= 3
        if not self.path.exists() or existing_size == 0:
            with self.path.open("xb" if not self.path.exists() else "wb") as seed:
                seed.write(b"\xef\xbb\xbf")
        self._stream = self.path.open("a", encoding=self.encoding, errors=self.errors, buffering=1)
        self._bytes = self.path.stat().st_size
        self._day = _utc(self._clock()).date()

    def _closed_proof(self):
        """Use only our explicit clean-close proof, never file age or PID alone."""
        try:
            proof_path = _safe(self.root, ".dmcs-retention-web-closed.json")
            if proof_path.stat().st_size > 16384:
                return None
            _signature(proof_path)
            proof = json.loads(proof_path.read_bytes())
            if (proof.get("project") != PROJECT or proof.get("installation_id") != self.installation_id
                    or proof.get("path") != str(self.path) or proof.get("signature") != _signature(self.path)):
                return None
            return _utc(proof["closed_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save_closed_proof(self):
        proof_path = _safe(self.root, ".dmcs-retention-web-closed.json", missing_ok=True)
        if proof_path.exists():
            _signature(proof_path)
            previous = json.loads(proof_path.read_bytes())
            if previous.get("project") != PROJECT or previous.get("installation_id") != self.installation_id:
                raise RetentionFileError("Unknown clean-close metadata is protected")
        proof = {"project": PROJECT, "installation_id": self.installation_id,
                 "path": str(self.path), "signature": _signature(self.path),
                 "closed_at": _iso(self._clock())}
        temporary = self.root / (".dmcs-retention-" + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(_json_bytes(proof))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, proof_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _rotate(self, *, certify, reopen=True, closed_at=None):
        if self._stream is not None and not self._stream.closed:
            self._stream.flush()
            self._stream.close()
        stamp = _utc(self._clock())
        name = "dmcs_server-" + stamp.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex + ".error.log"
        archived = self.root / name
        try:
            _safe(self._parent, self.path.name)
            try:
                os.replace(self.path, archived)
            except OSError as exc:
                if exc.errno != errno.EXDEV and getattr(exc, "winerror", None) != 17:
                    raise
                self._copy_closed_segment(archived)
        except (OSError, ValueError) as exc:
            self.last_rotation_error = str(exc)[:160]
            self._rotation_retry_after = time.monotonic() + 60
            if reopen:
                self._open()
            return
        self.last_rotation_error = None
        try:
            if certify and getattr(self, "_owned_current", False):
                register_closed_log(self.root, archived, installation_id=self.installation_id,
                                    category="error", closed_at=closed_at or stamp)
        except (OSError, ValueError) as exc:
            # A busy/unknown registry protects the archive; logging must keep
            # working even if retention bookkeeping cannot certify it.
            self.last_registration_error = type(exc).__name__
        if closed_at is not None:
            try:
                _safe(self.root, ".dmcs-retention-web-closed.json").unlink()
            except (OSError, ValueError):
                pass
        if reopen:
            self._open()

    @contextmanager
    def _closed_source(self):
        """On Windows deny existing/new writers for the complete copy window."""
        if os.name != "nt":
            with self.path.open("rb") as stream:
                yield stream
            return
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(self.path), 0x80000000, 0x1 | 0x4, None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "Closed log cannot be copied while another writer owns it")
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        with os.fdopen(descriptor, "rb") as stream:
            yield stream  # fdopen owns and closes the native handle.

    def _copy_closed_segment(self, archived):
        """Cross-volume move: bounded copy, fsync, hash, recheck, then unlink."""
        original = _signature(self.path)
        if original["size"] > self.copy_budget_bytes:
            raise RetentionFileError("Closed log exceeds cross-volume copy budget; original retained")
        temporary = self.root / (".dmcs-retention-" + uuid.uuid4().hex + ".copying")
        try:
            with self._closed_source() as source:
                digest = hashlib.sha256()
                copied = 0
                with temporary.open("xb") as destination:
                    while chunk := source.read(1024**2):
                        copied += len(chunk)
                        if copied > self.copy_budget_bytes:
                            raise RetentionFileError("Closed log grew beyond copy budget")
                        digest.update(chunk)
                        destination.write(chunk)
                    destination.flush()
                    os.fsync(destination.fileno())
                with temporary.open("rb") as verification:
                    copied_digest = hashlib.file_digest(verification, "sha256").hexdigest()
                if (copied != original["size"] or copied_digest != digest.hexdigest()
                        or _signature(self.path) != original):
                    raise RetentionFileError("Closed log changed or copy integrity check failed")
                _root(self.root)
                os.replace(temporary, archived)
                # The source read handle shares DELETE but excludes WRITE on
                # Windows. Original removal occurs only after durable equality.
                if _signature(self.path) != original:
                    raise RetentionFileError("Closed log changed before source removal")
                self.path.unlink()
        finally:
            temporary.unlink(missing_ok=True)

    def write(self, value):
        if not isinstance(value, str):
            raise TypeError("Text log expects strings")
        with self._guard:
            if self._closed:
                raise ValueError("I/O operation on closed log")
            length = len(value.encode(self.encoding, self.errors))
            if (value and self._bytes > 3 and time.monotonic() >= self._rotation_retry_after
                    and (self._bytes + length > self.max_bytes or self._day != _utc(self._clock()).date())):
                self._rotate(certify=True)
            written = self._stream.write(value)
            self._bytes += length
            return written

    def flush(self):
        with self._guard:
            if self._stream is not None and not self._stream.closed:
                self._stream.flush()

    def fileno(self):
        return self._stream.fileno()

    def isatty(self):
        return False

    @property
    def closed(self):
        return self._closed

    def _release_lease(self):
        if self._lease.closed:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._lease.seek(0)
                msvcrt.locking(self._lease.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lease.fileno(), fcntl.LOCK_UN)
        finally:
            self._lease.close()

    def close(self):
        with self._guard:
            if self._closed:
                return
            try:
                self._stream.flush()
                self._stream.close()
                # Keep current in place for existing one-click diagnostics. On
                # the next startup the recorded identity proves this exact file
                # was closed by us before it is moved and certified.
                if self._owned_current:
                    try:
                        self._save_closed_proof()
                    except (OSError, ValueError) as exc:
                        self.last_registration_error = type(exc).__name__
            finally:
                self._closed = True
                self._release_lease()
