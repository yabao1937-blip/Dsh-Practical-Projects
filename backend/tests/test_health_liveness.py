"""Liveness responds even when every synchronous request worker is occupied."""
import asyncio
from pathlib import Path
import subprocess
import threading

import anyio
from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx

from app.routers import health


def test_liveness_does_not_wait_for_synchronous_worker_capacity(monkeypatch):
    class ForbiddenDatabase:
        def connect(self):
            raise AssertionError('Liveness must never query the database')

    monkeypatch.setattr(health, 'engine', ForbiddenDatabase())
    application = FastAPI()
    application.include_router(health.router, prefix='/api/v1')

    async def exercise():
        limiter = anyio.to_thread.current_default_thread_limiter()
        original_tokens = limiter.total_tokens
        limiter.total_tokens = 1
        entered, release = threading.Event(), threading.Event()

        def occupy_only_worker():
            entered.set()
            release.wait(3)

        occupying = asyncio.create_task(anyio.to_thread.run_sync(occupy_only_worker))
        try:
            while not entered.is_set():
                await asyncio.sleep(.001)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                    base_url='http://testserver') as client:
                response = await asyncio.wait_for(client.get('/api/v1/health/live'), timeout=.5)
                assert response.status_code == 200
                assert response.json()['status'] == 'ok'
                assert 'checks' not in response.json(), 'liveness must not claim database readiness'
        finally:
            release.set()
            await occupying
            limiter.total_tokens = original_tokens

    asyncio.run(exercise())


def test_readiness_failure_remains_503_while_liveness_succeeds(monkeypatch):
    class BrokenDatabase:
        def connect(self):
            raise RuntimeError('private synthetic database details')

    monkeypatch.setattr(health, 'engine', BrokenDatabase())
    application = FastAPI()
    application.include_router(health.router, prefix='/api/v1')
    with TestClient(application) as client:
        ready = client.get('/api/v1/health/ready')
        assert ready.status_code == 503
        assert ready.json()['checks']['database']['error_code'] == 'database_unavailable'
        assert 'private synthetic' not in ready.text
        assert client.get('/api/v1/health/live').json()['status'] == 'ok'


def test_frontend_liveness_readiness_statuses():
    subprocess.run(['node', str(Path(__file__).parents[1]/'scripts/verify_health_status.js')],
                   check=True, capture_output=True)
