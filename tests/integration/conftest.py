"""Shared fixtures for integration tests."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app import config
from app.main import app
from app.services import test_orchestrator


# ---------------------------------------------------------------------------
# Mock settings: disable rate limiting + delays, use mock API key
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_settings(monkeypatch):
    monkeypatch.setattr(config.settings, "anthropic_api_key", "mock-test-key")
    monkeypatch.setattr(config.settings, "anthropic_rpm", 0)
    monkeypatch.setattr(config.settings, "tc_batch_delay_seconds", 0)


# ---------------------------------------------------------------------------
# Wipe all in-memory state between tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_orchestrator():
    yield
    test_orchestrator._store.clear()
    test_orchestrator._specs.clear()
    test_orchestrator._plans.clear()
    test_orchestrator._test_cases.clear()
    test_orchestrator._test_cases_internal.clear()
    test_orchestrator._run_logs.clear()
    test_orchestrator._tasks.clear()
    test_orchestrator._crud_scenarios_internal.clear()
    test_orchestrator._business_scenarios_internal.clear()


# ---------------------------------------------------------------------------
# Async HTTP client wired to the FastAPI app
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Poll helper: wait until a run reaches the target status
# ---------------------------------------------------------------------------

