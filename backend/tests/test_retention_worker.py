"""The test harness and read-only server must never launch storage writers."""
import asyncio

import pytest

from app.storage_retention import worker


def test_isolated_app_lifespan_never_derives_real_installation_storage(monkeypatch):
    monkeypatch.setenv('DMCS_ISOLATED_TEST_DB','1')
    monkeypatch.setattr(worker,'maintenance_sessions',lambda engine: pytest.fail('No background writers in pytest'))
    async def exercise():
        async with worker.lifespan(object()):
            pass
    asyncio.run(exercise())


def test_readonly_cannot_create_maintenance_engine(monkeypatch):
    monkeypatch.setattr('app.config.REMOTE_READONLY',{'server_url':'http://read-only.invalid'})
    with pytest.raises(PermissionError,match='Read-only'):
        worker.maintenance_sessions(object())
