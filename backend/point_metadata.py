"""Immutable file catalog for fixed KEP identities, outside the SQLite payload.

Only named metadata fields are externalized. Measurements, timestamps, quality,
units, definitions and arbitrary user content remain unchanged. Old JSON remains
readable; missing/corrupt catalog entries fail closed instead of using today's
point configuration to reinterpret history.
"""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from threading import RLock

MARKER = '$point_meta'
DIRECTORY = 'point-metadata'
FIELDS = frozenset(('device', 'tag_name', 'item_id', 'node_id'))
MAX_ENTRY_BYTES = 1024 * 1024
MAX_CACHE_BYTES = 8 * 1024 * 1024
REQUIREMENT_TABLE = 'point_metadata_storage'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(',', ':')).encode('utf-8')


def valid_key(key):
    return isinstance(key, str) and re.fullmatch('[0-9a-f]{64}', key) is not None


class MetadataError(ValueError):
    pass


class Catalog:
    def __init__(self, root=None, *, fetch=None, compact_writes=True):
        self.root = Path(root) if root is not None else None
        self.fetch = fetch
        self.compact_writes = compact_writes
        self.cache = OrderedDict()
        self.cache_sizes = {}
        self.cache_bytes = 0
        self.lock = RLock()

    def _remember(self, key, value):
        with self.lock:
            size = len(canonical(value))
            self.cache_bytes += size - self.cache_sizes.get(key, 0)
            self.cache_sizes[key] = size
            self.cache[key] = deepcopy(value)
            self.cache.move_to_end(key)
            while len(self.cache) > 2048 or self.cache_bytes > MAX_CACHE_BYTES:
                removed, _ = self.cache.popitem(last=False)
                self.cache_bytes -= self.cache_sizes.pop(removed)

    def get(self, key):
        if not valid_key(key):
            raise MetadataError('Invalid point metadata identity')
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return deepcopy(self.cache[key])
        if self.fetch is not None:
            value = self.fetch(key)
            raw = canonical(value)
        else:
            if self.root is None:
                raise MetadataError('Point metadata catalog is unavailable')
            path = self.root / (key + '.json')
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ENTRY_BYTES:
                raise MetadataError('Point metadata file is missing or invalid: ' + key)
            raw = path.read_bytes()
            value = json.loads(raw)
        if (len(raw) > MAX_ENTRY_BYTES or not isinstance(value, dict)
                or hashlib.sha256(raw).hexdigest() != key):
            raise MetadataError('Point metadata checksum mismatch: ' + key)
        self._remember(key, value)
        return deepcopy(value)

    def put(self, value):
        if self.root is None or self.fetch is not None:
            raise MetadataError('Read-only catalog cannot save point metadata')
        raw = canonical(value)
        if len(raw) > MAX_ENTRY_BYTES:
            raise MetadataError('Point metadata entry exceeds size limit')
        key = hashlib.sha256(raw).hexdigest()
        with self.lock:
            if key in self.cache:
                return key
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / (key + '.json')
            if path.exists():
                self.get(key)  # Never replace a damaged immutable version.
            else:
                fd, temporary = tempfile.mkstemp(prefix='.point-', suffix='.tmp', dir=self.root)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                self._remember(key, value)
        return key

    def pack(self, value):
        if not self.compact_writes:
            return value
        if isinstance(value, list):
            return [self.pack(child) for child in value]
        if not isinstance(value, dict):
            return value
        patches = []
        def extract(node, path):
            if isinstance(node, list):
                return [extract(child, path+[index]) for index, child in enumerate(node)]
            if not isinstance(node, dict):
                return node
            if MARKER in node:
                node = self.unpack(node)
            names = FIELDS.intersection(node)
            if 'node_id' in node or 'item_id' in node:
                names = names | ({'name'} if 'name' in node else set())
            if 'point_definitions' in node and isinstance(node['point_definitions'], dict):
                names = names | {'point_definitions'}
            if names:
                patches.append([path, {key: node[key] for key in names}])
            return {key: extract(child, path+[key]) for key, child in node.items() if key not in names}
        result = extract(value, [])
        if patches:
            # One immutable reference per document, not one long hash per small
            # repeated ItemID. The file contains only paths and fixed metadata.
            patches.sort(key=lambda entry: canonical(entry[0]))
            result[MARKER] = self.put({'format':'point-meta-tree-v1', 'patches':patches})
        return result

    def unpack(self, value, *, projected=False):
        if isinstance(value, list):
            return [self.unpack(child, projected=projected) for child in value]
        if not isinstance(value, dict):
            return value
        result = {key: self.unpack(child, projected=projected) for key, child in value.items() if key != MARKER}
        if MARKER in value:
            metadata = self.get(value[MARKER])
            patches = metadata['patches'] if metadata.get('format') == 'point-meta-tree-v1' else [[[],metadata]]
            for path, fields in patches:
                target = result
                try:
                    for part in path:
                        if isinstance(target, list) and type(part) is not int:
                            raise MetadataError('Point metadata position type mismatch')
                        target = target[part]
                except KeyError:
                    if projected:
                        continue
                    raise MetadataError('Point metadata position is missing') from None
                except (IndexError, TypeError):
                    raise MetadataError('Point metadata position is malformed') from None
                if not isinstance(target, dict) or target.keys() & fields.keys():
                    raise MetadataError('Conflicting point metadata fields')
                target.update(deepcopy(fields))
        return result

    def dumps(self, value):
        return json.dumps(self.pack(value), ensure_ascii=False)

    def loads(self, value):
        return self.unpack(json.loads(value))


def catalog_required(database):
    """A tiny durable flag survives source rollback and missing sidecar folders."""
    connection = sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (REQUIREMENT_TABLE,)).fetchone()
        return bool(exists and connection.execute(
            'SELECT 1 FROM point_metadata_storage WHERE format=?', ('compact-v1',)).fetchone())
    finally:
        connection.close()


def validate_catalog_presence(database):
    if catalog_required(database) and not (Path(database).parent / DIRECTORY).is_dir():
        raise MetadataError('Required point metadata directory is missing')


def copy_catalog(database, destination_database):
    """Copy immutable sidecars AFTER the SQLite snapshot, before certification.

    Writers publish a catalog entry before committing its reference. Therefore
    the post-snapshot directory includes every version that snapshot needs.
    """
    source = Path(database).parent / DIRECTORY
    target = Path(destination_database).parent / DIRECTORY
    if not source.exists():
        if catalog_required(destination_database):
            raise MetadataError('Required point metadata directory is missing')
        return
    if source.resolve() == target.resolve():
        return
    reader, writer = Catalog(source), Catalog(target)
    target.mkdir(parents=True, exist_ok=True)
    for path in source.glob('*.json'):
        if not valid_key(path.stem):
            raise MetadataError('Unexpected point metadata file')
        if writer.put(reader.get(path.stem)) != path.stem:
            raise MetadataError('Point metadata backup verification failed')
