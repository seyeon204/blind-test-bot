"""Shared non-fixture helpers for integration tests."""
from __future__ import annotations

import asyncio

from httpx import AsyncClient

BASE = "/api/v1"


async def wait_for_run(
    client: AsyncClient,
    run_id: str,
    target: set[str],
    timeout: float = 5.0,
) -> dict:
    """Poll GET /test-runs/{run_id} until status is in *target* or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        await asyncio.sleep(0.05)
        resp = await client.get(f"{BASE}/test-runs/{run_id}")
        assert resp.status_code == 200
        data = resp.json()
        if data["status"] in target:
            return data
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(
                f"Run {run_id} stuck at '{data['status']}' (expected {target})"
            )
