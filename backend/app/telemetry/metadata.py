"""Database JSON boundary; domain functions continue seeing lossless old DTOs."""
import json
from urllib.request import Request, build_opener, ProxyHandler

from point_metadata import Catalog, DIRECTORY, MetadataError, MAX_ENTRY_BYTES


def install_codec(engine, remote=None, *, catalog=None, compact_writes=True):
    if catalog is not None:
        pass
    elif remote:
        def fetch(key):
            request = Request(remote['server_url'] + '/api/v1/readonly-db/point-metadata/' + key,
                              headers={'X-DMCS-Read-Token': remote['token']})
            with build_opener(ProxyHandler({})).open(request, timeout=8) as response:
                raw = response.read(MAX_ENTRY_BYTES + 1)
            if len(raw) > MAX_ENTRY_BYTES:
                raise MetadataError('Remote point metadata exceeds size limit')
            return json.loads(raw)
        catalog = Catalog(fetch=fetch, compact_writes=False)
    else:
        from pathlib import Path
        database = engine.url.database
        if not database or database == ':memory:':
            raise ValueError('Point metadata codec requires an explicit catalog for an in-memory database')
        catalog = Catalog(Path(database).parent / DIRECTORY, compact_writes=compact_writes)
    engine.point_metadata_catalog = catalog
    engine.dialect._json_serializer = catalog.dumps
    engine.dialect._json_deserializer = catalog.loads
    if catalog.compact_writes and catalog.root is not None and catalog.fetch is None:
        # Commit the dependency flag before any compact record can be written.
        # Readers/remote test engines never create or update this table.
        with engine.begin() as connection:
            required = connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='point_metadata_storage'").first()
            if required and not catalog.root.is_dir():
                raise MetadataError('Required point metadata directory is missing')
            catalog.root.mkdir(parents=True, exist_ok=True)
            connection.exec_driver_sql(
                'CREATE TABLE IF NOT EXISTS point_metadata_storage (format TEXT PRIMARY KEY)')
            connection.exec_driver_sql(
                "INSERT OR IGNORE INTO point_metadata_storage(format) VALUES ('compact-v1')")
    return catalog


def catalog_for(db):
    bind = db.get_bind() if hasattr(db, 'get_bind') else db
    return getattr(getattr(bind, 'engine', bind), 'point_metadata_catalog', None)


def pack(db, value):
    catalog = catalog_for(db)
    return catalog.pack(value) if catalog else value


def unpack(db, value, *, projected=False):
    catalog = catalog_for(db)
    return catalog.unpack(value, projected=projected) if catalog else value
