"""Unit tests for async execution logic in app/core/executor.py"""
import pytest
from unittest.mock import AsyncMock, patch

from app.core.executor import execute_scenario, stream_executions
from app.models.internal import ExecutionResult, TestCase, TestScenario


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_tc(method="GET", path="/items", *, extract=None, scenario_id="s1", step_index=0) -> TestCase:
    return TestCase(
        endpoint_method=method,
        endpoint_path=path,
        description="step",
        expected_status_codes=[200],
        scenario_id=scenario_id,
        step_index=step_index,
        extract=extract or {},
    )


def make_exec(tc: TestCase, *, status=200, body=None, error=None) -> ExecutionResult:
    return ExecutionResult(
        test_case_id=tc.id,
        status_code=None if error else status,
        response_body=body,
        network_error=error,
    )


def make_scenario(steps: list[TestCase]) -> TestScenario:
    return TestScenario(id="s1", name="test scenario", description="desc", steps=steps)


# ---------------------------------------------------------------------------
# execute_scenario — sequential execution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_scenario_returns_one_result_per_step():
    step1 = make_tc("POST", "/login", step_index=0)
    step2 = make_tc("GET", "/profile", step_index=1)
    scenario = make_scenario([step1, step2])

    exec1 = make_exec(step1, status=200, body={"token": "abc"})
    exec2 = make_exec(step2, status=200, body={"name": "Alice"})

    with patch("app.core.executor._execute_one", new=AsyncMock(side_effect=[exec1, exec2])):
        results = await execute_scenario(scenario, base_url="http://test")

    assert len(results) == 2


@pytest.mark.asyncio
async def test_execute_scenario_extracts_value_for_next_step():
    """Step 1 returns {token: "abc"}, step 2 has {{token}} in header."""
    step1 = make_tc("POST", "/login", extract={"token": "token"}, step_index=0)
    step2 = TestCase(
        endpoint_method="GET",
        endpoint_path="/profile",
        description="get profile",
        expected_status_codes=[200],
        headers={"Authorization": "Bearer {{token}}"},
        scenario_id="s1",
        step_index=1,
    )
    scenario = make_scenario([step1, step2])

    exec1 = make_exec(step1, status=200, body={"token": "abc"})
    exec2 = make_exec(step2, status=200, body={"name": "Alice"})

    captured_calls = []

    async def fake_execute_one(client, semaphore, tc, base_url, timeout):
        captured_calls.append(tc)
        if tc.step_index == 0:
            return exec1
        return exec2

    with patch("app.core.executor._execute_one", new=fake_execute_one):
        results = await execute_scenario(scenario, base_url="http://test")

    # The resolved step 2 should have the substituted header
    resolved_step2 = captured_calls[1]
    assert resolved_step2.headers["Authorization"] == "Bearer abc"


@pytest.mark.asyncio
async def test_execute_scenario_extracted_value_in_result():
    step1 = make_tc("POST", "/login", extract={"userId": "user.id"}, step_index=0)
    scenario = make_scenario([step1])

    exec1 = make_exec(step1, status=201, body={"user": {"id": 99}})

    with patch("app.core.executor._execute_one", new=AsyncMock(return_value=exec1)):
        results = await execute_scenario(scenario, base_url="http://test")

    _, _, extracted = results[0]
    assert extracted == {"userId": 99}


@pytest.mark.asyncio
async def test_execute_scenario_stops_on_network_error():
    step1 = make_tc("POST", "/login", step_index=0)
    step2 = make_tc("GET", "/profile", step_index=1)
    step3 = make_tc("DELETE", "/session", step_index=2)
    scenario = make_scenario([step1, step2, step3])

    exec1 = make_exec(step1, error="connection refused")

    with patch("app.core.executor._execute_one", new=AsyncMock(return_value=exec1)) as mock:
        results = await execute_scenario(scenario, base_url="http://test")

    # Only step1 ran — step2 and step3 skipped
    assert len(results) == 1
    assert mock.call_count == 1


@pytest.mark.asyncio
async def test_execute_scenario_no_extract_context_unchanged():
    step1 = make_tc("GET", "/ping", extract={}, step_index=0)
    step2 = TestCase(
        endpoint_method="GET",
        endpoint_path="/ping2",
        description="ping2",
        expected_status_codes=[200],
        headers={"X-Static": "value"},
        scenario_id="s1",
        step_index=1,
    )
    scenario = make_scenario([step1, step2])

    exec1 = make_exec(step1, status=200, body={"irrelevant": "data"})
    exec2 = make_exec(step2, status=200)

    captured = []

    async def fake(client, semaphore, tc, base_url, timeout):
        captured.append(tc)
        return exec1 if tc.step_index == 0 else exec2

    with patch("app.core.executor._execute_one", new=fake):
        await execute_scenario(scenario, base_url="http://test")

    resolved_step2 = captured[1]
    assert resolved_step2.headers["X-Static"] == "value"  # unchanged


# ---------------------------------------------------------------------------
# stream_executions — basic behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_executions_empty_list_returns_nothing():
    results = []
    async for r in stream_executions([], base_url="http://test"):
        results.append(r)
    assert results == []


# ---------------------------------------------------------------------------
# execute_scenario — edge cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_scenario_nested_extract_path():
    """Extract value at dot-notation path 'user.id' → body={"user": {"id": 42}}."""
    step1 = make_tc("POST", "/login", extract={"userId": "user.id"}, step_index=0)
    step2 = TestCase(
        endpoint_method="GET",
        endpoint_path="/profile",
        description="get profile",
        expected_status_codes=[200],
        path_params={"id": "{{userId}}"},
        scenario_id="s1",
        step_index=1,
    )
    scenario = make_scenario([step1, step2])

    exec1 = make_exec(step1, status=200, body={"user": {"id": 42}})
    exec2 = make_exec(step2, status=200)

    captured = []

    async def fake(client, semaphore, tc, base_url, timeout):
        captured.append(tc)
        return exec1 if tc.step_index == 0 else exec2

    with patch("app.core.executor._execute_one", new=fake):
        await execute_scenario(scenario, base_url="http://test")

    resolved_step2 = captured[1]
    assert resolved_step2.path_params["id"] == "42"


@pytest.mark.asyncio
async def test_execute_scenario_missing_extract_path_leaves_template():
    """If extract path doesn't exist in the response body, the {{var}} stays unresolved."""
    step1 = make_tc("POST", "/login", extract={"token": "nonexistent.path"}, step_index=0)
    step2 = TestCase(
        endpoint_method="GET",
        endpoint_path="/profile",
        description="get profile",
        expected_status_codes=[200],
        headers={"Authorization": "Bearer {{token}}"},
        scenario_id="s1",
        step_index=1,
    )
    scenario = make_scenario([step1, step2])

    exec1 = make_exec(step1, status=200, body={"other": "data"})
    exec2 = make_exec(step2, status=200)

    captured = []

    async def fake(client, semaphore, tc, base_url, timeout):
        captured.append(tc)
        return exec1 if tc.step_index == 0 else exec2

    with patch("app.core.executor._execute_one", new=fake):
        await execute_scenario(scenario, base_url="http://test")

    resolved_step2 = captured[1]
    # token was never set in context → template stays as-is
    assert resolved_step2.headers["Authorization"] == "Bearer {{token}}"


@pytest.mark.asyncio
async def test_execute_scenario_context_accumulates_across_three_steps():
    """Step 1 extracts token, step 2 extracts userId — step 3 uses both."""
    step1 = make_tc("POST", "/login", extract={"token": "token"}, step_index=0)
    step2 = make_tc("POST", "/users", extract={"userId": "id"}, step_index=1)
    step3 = TestCase(
        endpoint_method="GET",
        endpoint_path="/users/{userId}",
        description="get user",
        expected_status_codes=[200],
        path_params={"userId": "{{userId}}"},
        headers={"Authorization": "Bearer {{token}}"},
        scenario_id="s1",
        step_index=2,
    )
    scenario = make_scenario([step1, step2, step3])

    exec1 = make_exec(step1, status=200, body={"token": "jwt-abc"})
    exec2 = make_exec(step2, status=201, body={"id": 99})
    exec3 = make_exec(step3, status=200)

    captured = []

    async def fake(client, semaphore, tc, base_url, timeout):
        captured.append(tc)
        if tc.step_index == 0:
            return exec1
        if tc.step_index == 1:
            return exec2
        return exec3

    with patch("app.core.executor._execute_one", new=fake):
        await execute_scenario(scenario, base_url="http://test")

    resolved_step3 = captured[2]
    assert resolved_step3.headers["Authorization"] == "Bearer jwt-abc"
    assert resolved_step3.path_params["userId"] == "99"


@pytest.mark.asyncio
async def test_stream_executions_yields_one_per_tc():
    tcs = [
        make_tc("GET", f"/items/{i}", scenario_id=None)
        for i in range(3)
    ]
    # Give each TC a unique ID and build matching exec results
    exec_map = {tc.id: make_exec(tc, status=200) for tc in tcs}

    async def fake(client, semaphore, tc, base_url, timeout):
        return exec_map[tc.id]

    with patch("app.core.executor._execute_one", new=fake):
        collected = []
        async for r in stream_executions(tcs, base_url="http://test"):
            collected.append(r)

    assert len(collected) == 3
    assert {r.test_case_id for r in collected} == {tc.id for tc in tcs}
