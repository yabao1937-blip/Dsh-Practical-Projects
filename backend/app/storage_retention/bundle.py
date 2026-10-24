"""Streaming, self-contained cold bundles; never opens a production database.

The caller selects a dependency-complete set of rows. This module preserves
their SQLite value types, proves a full isolated restore, and only then
publishes the bundle. Original column declarations are metadata, not executable
SQL: restore tables deliberately have no type affinity, indexes or triggers.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence


FORMAT = "dmcs-cold-archive"
VERSION = 1
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ROW_BYTES = 8 * 1024 * 1024
MAX_TABLES = 256
MAX_COLUMNS = 512
MAX_TOTAL_BYTES = 128 * 1024**3
MAX_ROWS = 100_000_000
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_ARCHIVE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)
}


class BundleError(ValueError):
    """An incomplete, unsafe, unsupported or corrupt cold bundle."""


class BundleBudgetExceeded(BundleError):
    """Stop without publishing when a caller-supplied work budget is exhausted."""


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":"), sort_keys=True).encode("ascii")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BundleError("Duplicate JSON member")
        result[key] = value
    return result


def _load_json(data: bytes):
    try:
        return json.loads(data, object_pairs_hook=_unique_object,
                          parse_constant=lambda value: (_ for _ in ()).throw(
                              BundleError("Non-finite JSON number")))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise BundleError("Invalid bundle JSON") from exc


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise BundleError("Unsafe table or column name")
    if value.lower().startswith("sqlite_"):
        raise BundleError("Reserved SQLite name")
    return value


def _archive_id(value: str) -> str:
    if (not isinstance(value, str) or not _ARCHIVE_ID.fullmatch(value)
            or value.endswith(".") or value.split(".")[0].upper() in _RESERVED):
        raise BundleError("Unsafe archive identifier")
    return value


def _columns(columns: Sequence) -> list[dict]:
    if (not isinstance(columns, (list, tuple)) or not columns
            or len(columns) > MAX_COLUMNS):
        raise BundleError("Invalid column definitions")
    result = []
    for column in columns:
        if isinstance(column, str):
            result.append({"name": _identifier(column), "type": "", "notnull": False,
                           "pk": 0, "default": None})
            continue
        if not isinstance(column, Mapping) or set(column) - {
                "name", "type", "notnull", "pk", "default"}:
            raise BundleError("Invalid column definition")
        name = _identifier(column.get("name"))
        declared_type = column.get("type", "")
        primary_key = column.get("pk", 0)
        if (not isinstance(declared_type, str) or len(declared_type) > 256
                or type(primary_key) is not int or not 0 <= primary_key <= MAX_COLUMNS
                or column.get("notnull", False) not in (False, True, 0, 1)
                or not isinstance(column.get("default"), (str, type(None)))):
            raise BundleError("Invalid column declaration metadata")
        result.append({"name": name, "type": declared_type,
                       "notnull": bool(column.get("notnull", False)),
                       "pk": primary_key, "default": column.get("default")})
    if len({column["name"].lower() for column in result}) != len(result):
        raise BundleError("Duplicate column name")
    return result


def _encode_cell(value):
    if value is None:
        return ["n", None]
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        if not -(2**63) <= value < 2**63:
            raise BundleError("Integer is outside SQLite range")
        return ["i", value]
    if isinstance(value, float):
        if math.isnan(value):
            raise BundleError("NaN is not a persistent SQLite REAL value")
        return ["f", value.hex()]
    if isinstance(value, str):
        return ["t", value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ["b", base64.b64encode(value).decode("ascii")]
    raise BundleError("Unsupported SQLite value type")


def _encode_row(row: Sequence) -> bytes:
    data = _json_bytes([_encode_cell(value) for value in row]) + b"\n"
    if len(data) > MAX_ROW_BYTES:
        raise BundleError("Archive row exceeds the bounded record size")
    return data


def _decode_row(data: bytes, width: int) -> tuple:
    cells = _load_json(data)
    if not isinstance(cells, list) or len(cells) != width:
        raise BundleError("Wrong archive row width")
    values = []
    for cell in cells:
        if not isinstance(cell, list) or len(cell) != 2:
            raise BundleError("Invalid typed cell")
        tag, value = cell
        try:
            if tag == "n" and value is None:
                decoded = None
            elif tag == "i" and type(value) is int and -(2**63) <= value < 2**63:
                decoded = value
            elif tag == "t" and isinstance(value, str):
                decoded = value
            elif tag == "f" and isinstance(value, str):
                decoded = float.fromhex(value)
                if math.isnan(decoded) or decoded.hex() != value:
                    raise BundleError("Noncanonical REAL cell")
            elif tag == "b" and isinstance(value, str):
                decoded = base64.b64decode(value, validate=True)
                if base64.b64encode(decoded).decode("ascii") != value:
                    raise BundleError("Noncanonical BLOB cell")
            else:
                raise BundleError("Invalid SQLite cell type")
        except (ValueError, OverflowError, TypeError) as exc:
            raise BundleError("Invalid typed cell value") from exc
        values.append(decoded)
    return tuple(values)


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _member(root: Path, relative: str) -> Path:
    # Only our flat, generated table paths are part of format v1.
    if (not isinstance(relative, str) or "\\" in relative
            or not re.fullmatch(r"tables/[A-Za-z_][A-Za-z0-9_]{0,127}\.jsonl\.gz", relative)):
        raise BundleError("Unsafe bundle member path")
    path = root / relative
    for part in (root / "tables", path):
        if not part.exists() or _is_link(part):
            raise BundleError("Missing or linked bundle member")
    if not path.is_file() or not _inside(path.resolve(), root.resolve()):
        raise BundleError("Bundle member escapes archive")
    return path


def _hash_file(path: Path, check=lambda: None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            check()
            digest.update(block)
    return digest.hexdigest()


def _sync_file(path: Path):
    # Windows FlushFileBuffers requires a handle opened for writing.
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _acquire_archive_lock(path: Path):
    """Lock a stable file inode; OS releases ownership even after hard exit.

    The file intentionally survives release. Unlinking it would let a racing
    opener lock another inode and bypass an active writer's ownership.
    """
    if path.exists() and _is_link(path):
        raise BundleError("Archive lock must be a regular local file")
    stream = path.open("a+b")
    try:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        stream.close()
        raise FileExistsError(f"Archive writer already active: {path.name}") from exc
    except BaseException:
        stream.close()
        raise
    return stream


def _release_archive_lock(stream):
    if stream is None:
        return
    try:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


class ArchiveBundleWriter:
    """Append short batches with bounded memory, then atomically publish once.

    ``add_rows`` accepts tuples/sqlite3.Row or mappings and may be called
    repeatedly for the same table. A failed call poisons and removes the staging
    bundle. A positive time/byte/row budget aborts rather than publishing a
    partial archive. Use as a context manager, or call ``abort`` after abandoning
    a writer. The final path is ``target_dir/archive_id/manifest.json``.
    """

    def __init__(self, target_dir, archive_id: str, metadata: Mapping | None = None, *,
                 max_uncompressed_bytes: int = MAX_TOTAL_BYTES,
                 max_rows: int = MAX_ROWS, budget_seconds: float | None = None):
        self.archive_id = _archive_id(archive_id)
        if (type(max_uncompressed_bytes) is not int or max_uncompressed_bytes <= 0
                or type(max_rows) is not int or max_rows <= 0
                or (budget_seconds is not None and budget_seconds <= 0)):
            raise BundleError("Invalid archive work budget")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise BundleError("Archive metadata must be an object")
        self.metadata = _load_json(_json_bytes(dict(metadata or {})))
        self.root = Path(target_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.final = self.root / self.archive_id
        self.lock = self.root / f".{self.archive_id}.lock"
        self._lock_stream = None
        self.stage = None
        self.closed = False
        self.tables = {}
        self.total_bytes = 0
        self.total_rows = 0
        self.max_bytes = min(max_uncompressed_bytes, MAX_TOTAL_BYTES)
        self.max_rows = min(max_rows, MAX_ROWS)
        self.deadline = None if budget_seconds is None else time.monotonic() + budget_seconds
        if self.final.exists() or self.final.is_symlink():
            raise FileExistsError(self.final)
        self._lock_stream = _acquire_archive_lock(self.lock)
        try:
            if self.final.exists() or self.final.is_symlink():
                raise FileExistsError(self.final)
            self.stage = Path(tempfile.mkdtemp(prefix=f".{self.archive_id}.writing-", dir=self.root))
            (self.stage / "tables").mkdir()
        except BaseException:
            _release_archive_lock(self._lock_stream)
            self._lock_stream = None
            raise

    def _check_budget(self):
        if self.closed:
            raise BundleError("Archive writer is closed")
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise BundleBudgetExceeded("Archive time budget exhausted")
        if self.total_bytes > self.max_bytes or self.total_rows > self.max_rows:
            raise BundleBudgetExceeded("Archive row or byte budget exhausted")

    def add_rows(self, table: str, columns: Sequence, iterable: Iterable):
        try:
            self._check_budget()
            table = _identifier(table)
            definitions = _columns(columns)
            if any(name.lower() == table.lower() and name != table for name in self.tables):
                raise BundleError("Duplicate table name")
            if table not in self.tables:
                if len(self.tables) >= MAX_TABLES:
                    raise BundleError("Too many archive tables")
                self.tables[table] = {"name": table, "columns": definitions,
                    "path": f"tables/{table}.jsonl.gz", "row_count": 0,
                    "uncompressed_bytes": 0, "digest": hashlib.sha256()}
            state = self.tables[table]
            if state["columns"] != definitions:
                raise BundleError("Column definitions changed between batches")
            path = self.stage / state["path"]
            with path.open("ab") as raw:
                with gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=3, mtime=0) as stream:
                    for row in iterable:
                        if isinstance(row, Mapping):
                            if set(row) != {c["name"] for c in definitions}:
                                raise BundleError("Archive mapping columns differ")
                            values = tuple(row[c["name"]] for c in definitions)
                        else:
                            values = tuple(row)
                        if len(values) != len(definitions):
                            raise BundleError("Wrong archive row width")
                        line = _encode_row(values)
                        self.total_bytes += len(line)
                        self.total_rows += 1
                        self._check_budget()
                        stream.write(line)
                        state["digest"].update(line)
                        state["row_count"] += 1
                        state["uncompressed_bytes"] += len(line)
        except BaseException:
            self.abort()
            raise

    def finish(self) -> Path:
        try:
            self._check_budget()
            if not self.tables:
                raise BundleError("Cannot publish a bundle without tables")
            tables = []
            for state in self.tables.values():
                self._check_budget()
                item = {k: v for k, v in state.items() if k != "digest"}
                item["sha256"] = state["digest"].hexdigest()
                path = self.stage / item["path"]
                _sync_file(path)
                item["compressed_bytes"] = path.stat().st_size
                item["compressed_sha256"] = _hash_file(path, self._check_budget)
                tables.append(item)
            manifest = {"format": FORMAT, "version": VERSION,
                "archive_id": self.archive_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "metadata": self.metadata, "tables": tables,
                "verification": {"method": "isolated-sqlite-roundtrip", "passed": True}}
            data = _json_bytes(manifest)
            if len(data) > MAX_MANIFEST_BYTES:
                raise BundleError("Manifest exceeds size limit")
            path = self.stage / "manifest.json"
            path.write_bytes(data)
            (self.stage / "manifest.sha256").write_text(hashlib.sha256(data).hexdigest(), encoding="ascii")
            _sync_file(path)
            _sync_file(self.stage / "manifest.sha256")
            verify_bundle(path, _check=self._check_budget)
            self._check_budget()
            if self.final.exists() or self.final.is_symlink():
                raise FileExistsError(self.final)
            # The live OS lock serializes publishers of this archive id.
            self.stage.rename(self.final)
            self.stage = None
            self.closed = True
            _release_archive_lock(self._lock_stream)
            self._lock_stream = None
            return self.final / "manifest.json"
        except BaseException:
            self.abort()
            raise

    def abort(self):
        if self.closed:
            return
        self.closed = True
        if self.stage is not None:
            shutil.rmtree(self.stage, ignore_errors=True)
            self.stage = None
        _release_archive_lock(self._lock_stream)
        self._lock_stream = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if not self.closed:
            self.abort()


def _manifest(path) -> tuple[Path, dict]:
    path = Path(path)
    if path.is_dir():
        path = path / "manifest.json"
    if path.name != "manifest.json" or not path.exists() or _is_link(path) or _is_link(path.parent):
        raise BundleError("Expected a regular bundle manifest")
    root = path.parent.resolve()
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise BundleError("Manifest exceeds size limit")
    checksum = root / "manifest.sha256"
    if (not checksum.is_file() or _is_link(checksum) or checksum.stat().st_size != 64):
        raise BundleError("Missing manifest checksum")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest().encode("ascii") != checksum.read_bytes():
        raise BundleError("Manifest checksum mismatch")
    result = _load_json(data)
    if (not isinstance(result, dict) or result.get("format") != FORMAT
            or type(result.get("version")) is not int or result["version"] != VERSION
            or not isinstance(result.get("metadata"), dict)
            or not isinstance(result.get("tables"), list)
            or not 0 < len(result["tables"]) <= MAX_TABLES):
        raise BundleError("Unsupported archive manifest")
    _archive_id(result.get("archive_id"))
    names = set()
    total_bytes = total_rows = total_compressed = 0
    for table in result["tables"]:
        if not isinstance(table, dict):
            raise BundleError("Invalid table manifest")
        name = _identifier(table.get("name"))
        if name.lower() in names:
            raise BundleError("Duplicate archive table")
        names.add(name.lower())
        if table.get("columns") != _columns(table.get("columns")):
            raise BundleError("Noncanonical column definitions")
        if table.get("path") != f"tables/{name}.jsonl.gz":
            raise BundleError("Table path does not match table name")
        for key in ("row_count", "uncompressed_bytes", "compressed_bytes"):
            if type(table.get(key)) is not int or table[key] < 0:
                raise BundleError("Invalid archive size or count")
        for key in ("sha256", "compressed_sha256"):
            if not isinstance(table.get(key), str) or not _SHA256.fullmatch(table[key]):
                raise BundleError("Invalid archive checksum")
        total_bytes += table["uncompressed_bytes"]
        total_compressed += table["compressed_bytes"]
        total_rows += table["row_count"]
        if (total_bytes > MAX_TOTAL_BYTES or total_compressed > MAX_TOTAL_BYTES
                or total_rows > MAX_ROWS):
            raise BundleError("Archive exceeds restore safety budget")
        member = _member(root, table["path"])
        if member.stat().st_size != table["compressed_bytes"]:
            raise BundleError("Compressed archive size mismatch")
    return root, result


def _restore(path, destination, check) -> tuple[Path, dict]:
    root, manifest = _manifest(path)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    parent = destination.parent.resolve(strict=True)
    destination = parent / destination.name
    if _inside(destination, root):
        raise BundleError("Restore destination must be outside the bundle")
    # Keep the proof restore on this volume and fail before consuming the last
    # available space. JSONL is conservative for normal SQLite row storage.
    required = sum(table["uncompressed_bytes"] for table in manifest["tables"])
    required += len(manifest["tables"]) * 8192 + 1024 * 1024
    if shutil.disk_usage(parent).free < required:
        raise BundleBudgetExceeded("Insufficient space for isolated archive restore")
    fd, staging_name = tempfile.mkstemp(prefix=".restore-", suffix=".sqlite", dir=parent)
    os.close(fd)
    staging = Path(staging_name)
    db = None
    try:
        db = sqlite3.connect(str(staging))
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA cache_size=-2048")
        db.execute("PRAGMA temp_store=FILE")
        for table in manifest["tables"]:
            check()
            member = _member(root, table["path"])
            if _hash_file(member, check) != table["compressed_sha256"]:
                raise BundleError("Compressed archive checksum mismatch")
            columns = [column["name"] for column in table["columns"]]
            projection = ",".join(f'"{name}"' for name in columns)
            name = table["name"]
            db.execute(f'CREATE TABLE "{name}" ({projection})')
            insert = f'INSERT INTO "{name}" ({projection}) VALUES ({",".join("?" for _ in columns)})'
            digest = hashlib.sha256()
            count = size = batch_bytes = 0
            batch = []
            try:
                with gzip.open(member, "rb") as stream:
                    while line := stream.readline(MAX_ROW_BYTES + 1):
                        check()
                        if len(line) > MAX_ROW_BYTES or not line.endswith(b"\n"):
                            raise BundleError("Invalid or oversized archive record")
                        size += len(line)
                        count += 1
                        if count > table["row_count"] or size > table["uncompressed_bytes"]:
                            raise BundleError("Archive exceeds declared counts")
                        digest.update(line)
                        batch.append(_decode_row(line, len(columns)))
                        batch_bytes += len(line)
                        if len(batch) >= 256 or batch_bytes >= 4 * 1024 * 1024:
                            db.executemany(insert, batch)
                            batch.clear()
                            batch_bytes = 0
                    if batch:
                        db.executemany(insert, batch)
            except (OSError, EOFError, gzip.BadGzipFile) as exc:
                raise BundleError("Invalid compressed archive") from exc
            if (count != table["row_count"] or size != table["uncompressed_bytes"]
                    or digest.hexdigest() != table["sha256"]):
                raise BundleError("Archive row count or content checksum mismatch")
            # Re-read the actual restored SQLite values, not merely INSERT counts.
            restored_digest = hashlib.sha256()
            restored_count = 0
            hidden_rowid = next((alias for alias in ("rowid", "_rowid_", "oid")
                                 if alias not in {column.lower() for column in columns}), None)
            if hidden_rowid is None:
                raise BundleError("All SQLite row-order aliases are shadowed")
            cursor = db.execute(f'SELECT {projection} FROM "{name}" ORDER BY {hidden_rowid}')
            for row in cursor:
                check()
                restored_digest.update(_encode_row(row))
                restored_count += 1
            if restored_count != count or restored_digest.hexdigest() != table["sha256"]:
                raise BundleError("Restored SQLite content differs from archive")
            db.commit()
        check()
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise BundleError("Restored SQLite integrity check failed")
        check()
        db.close()
        db = None
        _sync_file(staging)
        check()
        # Hard-link publication is atomic and never overwrites an existing file.
        os.link(staging, destination)
        staging.unlink()
        return destination, manifest
    except sqlite3.Error as exc:
        raise BundleError("Unable to restore isolated SQLite archive") from exc
    finally:
        if db is not None:
            db.close()
        staging.unlink(missing_ok=True)
        Path(str(staging) + "-journal").unlink(missing_ok=True)


def restore_bundle(path, destination) -> Path:
    """Restore into a new isolated SQLite file; existing files are never touched."""
    return _restore(path, destination, lambda: None)[0]


def verify_bundle(path, *, _check=lambda: None) -> dict:
    """Verify checksums, counts and every value through a fresh SQLite restore.

    Temporary restore data stays on the archive's volume, not the system drive.
    It is removed on both success and failure. This validates supplied evidence;
    the calling retention service must additionally prove dependency closure.
    """
    root, _ = _manifest(path)
    with tempfile.TemporaryDirectory(prefix=".archive-verify-", dir=root.parent) as temporary:
        _, manifest = _restore(path, Path(temporary) / "restore.sqlite", _check)
    return manifest


def read_table(path, table: str, *, budget_seconds: float | None = None):
    """Yield trusted row mappings without materializing a whole SQLite restore.

    A private compressed snapshot of this table is checked in full *before*
    yielding the first row. This prevents partial output from a corrupt table,
    even if the published archive is concurrently changed. Memory is bounded
    by a single record; temporary disk is at most one compressed table. Close
    an abandoned iterator to promptly release its temporary file.
    """
    table = _identifier(table)
    if budget_seconds is not None and budget_seconds <= 0:
        raise BundleError("Invalid table read budget")
    deadline = None if budget_seconds is None else time.monotonic() + budget_seconds

    def check():
        if deadline is not None and time.monotonic() > deadline:
            raise BundleBudgetExceeded("Archive read time budget exhausted")

    root, manifest = _manifest(path)
    item = next((item for item in manifest["tables"] if item["name"] == table), None)
    if item is None:
        raise BundleError("Table does not exist in archive")
    if shutil.disk_usage(root.parent).free < item["compressed_bytes"] + 1024 * 1024:
        raise BundleBudgetExceeded("Insufficient space for verified table snapshot")
    member = _member(root, item["path"])
    names = [column["name"] for column in item["columns"]]
    with tempfile.TemporaryFile(mode="w+b", prefix=".archive-read-", dir=root.parent) as snapshot:
        compressed_digest = hashlib.sha256()
        copied = 0
        with member.open("rb") as source:
            while block := source.read(1024 * 1024):
                check()
                copied += len(block)
                if copied > item["compressed_bytes"]:
                    raise BundleError("Compressed archive exceeds declared size")
                compressed_digest.update(block)
                snapshot.write(block)
        if (copied != item["compressed_bytes"]
                or compressed_digest.hexdigest() != item["compressed_sha256"]):
            raise BundleError("Compressed archive checksum mismatch")
        snapshot.seek(0)
        digest = hashlib.sha256()
        count = size = 0
        try:
            with gzip.GzipFile(fileobj=snapshot, mode="rb") as stream:
                while line := stream.readline(MAX_ROW_BYTES + 1):
                    check()
                    if len(line) > MAX_ROW_BYTES or not line.endswith(b"\n"):
                        raise BundleError("Invalid or oversized archive record")
                    size += len(line)
                    count += 1
                    if count > item["row_count"] or size > item["uncompressed_bytes"]:
                        raise BundleError("Archive exceeds declared counts")
                    values = _decode_row(line, len(names))
                    if _encode_row(values) != line:
                        raise BundleError("Noncanonical typed archive record")
                    digest.update(line)
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            raise BundleError("Invalid compressed archive") from exc
        if (count != item["row_count"] or size != item["uncompressed_bytes"]
                or digest.hexdigest() != item["sha256"]):
            raise BundleError("Archive row count or content checksum mismatch")
        snapshot.seek(0)
        with gzip.GzipFile(fileobj=snapshot, mode="rb") as stream:
            while line := stream.readline(MAX_ROW_BYTES + 1):
                check()
                yield dict(zip(names, _decode_row(line, len(names))))
