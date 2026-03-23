"""Executes test cases against the target API using httpx."""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from app.config import settings
from app.models.internal import ExecutionResult, TestCase, TestScenario


# ---------------------------------------------------------------------------
# Template helpers for scenario value injection
# ---------------------------------------------------------------------------

def _extract_value(body: Any, path: str) -> Any:
    """Resolve a dot-notation path against a response body dict.

    Examples:
        "id"          → body["id"]
        "data.id"     → body["data"]["id"]
        "user.name"   → body["user"]["name"]
    """
    if body is None or not path:
        return None
    current = body
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _resolve_templates(value: Any, context: dict[str, Any]) -> Any:
    """Recursively replace {{varName}} in strings with context values."""
    if isinstance(value, str):
        for key, val in context.items():
            value = value.replace(f"{{{{{key}}}}}", str(val))
        return value
    if isinstance(value, dict):
        return {k: _resolve_templates(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_templates(item, context) for item in value]
    return value


def _build_url(base_url: str, path: str, path_params: dict[str, Any]) -> str:
    """Substitute path parameters and join with base URL."""
    resolved_path = path
    for key, value in path_params.items():
        resolved_path = resolved_path.replace(f"{{{key}}}", str(value))
    return base_url.strip().rstrip("/") + resolved_path


async def _execute_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    tc: TestCase,
    base_url: str,
    timeout: int,
) -> ExecutionResult:
    url = _build_url(base_url, tc.endpoint_path, tc.path_params)
    if unresolved := re.findall(r'\{[^}]+\}', url):
        return ExecutionResult(
            test_case_id=tc.id,
            latency_ms=0.0,
            network_error=f"Unresolved path params: {unresolved}",
        )

    async with semaphore:
        start = time.monotonic()
        try:
            response = await client.request(
                method=tc.endpoint_method,
                url=url,
                params=tc.query_params or None,
                headers=tc.headers or None,
                json=tc.body if tc.body is not None else None,
                timeout=timeout,
            )
            latency_ms = (time.monotonic() - start) * 1000

            body_bytes = response.content
            ct = response.headers.get("content-type", "")
            if any(b in ct for b in ("image/", "application/pdf", "application/octet")):
                body = {"__binary__": True, "content_type": ct, "size_bytes": len(body_bytes)}
            elif len(body_bytes) > settings.max_response_body_bytes:
                body = {
                    "__truncated__": True,
                    "__original_size_bytes__": len(body_bytes),
                    "__preview__": response.text[:500],
                }
            elif ct.startswith("application/json"):
                try:
                    body = response.json()
                except Exception:
                    body = response.text
            else:
                try:
                    body = response.json()
                except Exception:
                    body = response.text

            return ExecutionResult(
                test_case_id=tc.id,
                status_code=response.status_code,
                response_headers=dict(response.headers),
                response_body=body,
                latency_ms=round(latency_ms, 2),
            )
        except httpx.TimeoutException as e:
            latency_ms = (time.monotonic() - start) * 1000
            return ExecutionResult(
                test_case_id=tc.id,
                latency_ms=round(latency_ms, 2),
                network_error=f"Timeout: {e}",
            )
        except httpx.RequestError as e:
            latency_ms = (time.monotonic() - start) * 1000
            return ExecutionResult(
                test_case_id=tc.id,
                latency_ms=round(latency_ms, 2),
                network_error=f"Request error: {e}",
            )


async def _execute_rate_limit(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    tc: TestCase,
    base_url: str,
    timeout: int,
) -> ExecutionResult:
    """Execute a rate_limit TC by sending repeat_count requests sequentially.
    Returns PASS (429) if any response is 429, else the last result."""
    last: ExecutionResult | None = None
    for _ in range(tc.repeat_count):
        result = await _execute_one(client, semaphore, tc, base_url, timeout)
        last = result
        if result.status_code == 429:
            return ExecutionResult(
                test_case_id=tc.id,
                status_code=429,
                response_headers=result.response_headers,
                response_body=result.response_body,
                latency_ms=result.latency_ms,
            )
    return last  # type: ignore[return-value]


async def execute_test_cases(
    test_cases: list[TestCase],
    base_url: str,
    timeout: int | None = None,
) -> list[ExecutionResult]:
    """Run all test cases concurrently (bounded by MAX_CONCURRENT_REQUESTS)."""
    timeout = timeout or settings.request_timeout_seconds
    semaphore = asyncio.Semaphore(settings.max_concurrent_requests)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        tasks = [
            _execute_rate_limit(client, semaphore, tc, base_url, timeout)
            if tc.repeat_count > 1 and tc.security_test_type == "rate_limit"
            else _execute_one(client, semaphore, tc, base_url, timeout)
            for tc in test_cases
        ]
        return await asyncio.gather(*tasks)


async def stream_executions(
    test_cases: list[TestCase],
    base_url: str,
    timeout: int | None = None,
) -> AsyncGenerator[ExecutionResult, None]:
    """Yield ExecutionResults one-by-one as they complete (completion order, not submission order)."""
    if not test_cases:
        return

    timeout = timeout or settings.request_timeout_seconds
    semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
    queue: asyncio.Queue[ExecutionResult] = asyncio.Queue()

    async def _worker(client: httpx.AsyncClient, tc: TestCase) -> None:
        if tc.repeat_count > 1 and tc.security_test_type == "rate_limit":
            result = await _execute_rate_limit(client, semaphore, tc, base_url, timeout)
        else:
            result = await _execute_one(client, semaphore, tc, base_url, timeout)
        await queue.put(result)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        tasks = [asyncio.create_task(_worker(client, tc)) for tc in test_cases]
        for _ in test_cases:
            yield await queue.get()
        await asyncio.gather(*tasks)


async def execute_scenario(
    scenario: TestScenario,
    base_url: str,
    timeout: int | None = None,
) -> list[tuple[TestCase, ExecutionResult, dict[str, Any]]]:
    """Execute scenario steps sequentially, injecting extracted values into each subsequent step.

    Returns a list of (resolved_step, execution_result, extracted_values) tuples.
    Stops early on network error so downstream steps aren't run with missing context.
    """
    timeout = timeout or settings.request_timeout_seconds
    semaphore = asyncio.Semaphore(1)  # scenarios are always sequential
    context: dict[str, Any] = {}     # accumulated extracted values across steps
    results: list[tuple[TestCase, ExecutionResult, dict[str, Any]]] = []

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for step in scenario.steps:
            # Inject context values into path_params, query_params, body, headers
            resolved = step.model_copy(update={
                "path_params": _resolve_templates(step.path_params, context),
                "query_params": _resolve_templates(step.query_params, context),
                "body": _resolve_templates(step.body, context),
                "headers": _resolve_templates(step.headers, context),
            })

            execution = await _execute_one(client, semaphore, resolved, base_url, timeout)

            # Extract values from response body for use in subsequent steps
            step_extracted: dict[str, Any] = {}
            if execution.response_body and step.extract:
                for var_name, path in step.extract.items():
                    value = _extract_value(execution.response_body, path)
                    if value is not None:
                        context[var_name] = value
                        step_extracted[var_name] = value

            results.append((resolved, execution, step_extracted))

            # Stop if network error — remaining steps can't run meaningfully
            if execution.network_error:
                break

    return results
