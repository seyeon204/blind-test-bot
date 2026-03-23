"""Unit tests for app/core/ai_validator.py"""
import types
import pytest
from unittest.mock import AsyncMock, patch

from app.core.ai_validator import ai_validate_batch
from app.models.internal import ExecutionResult, TestCase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_tc(security_test_type=None) -> TestCase:
    return TestCase(
        endpoint_method="GET",
        endpoint_path="/items",
        description="test",
        expected_status_codes=[200],
        security_test_type=security_test_type,
    )


def make_exec(tc: TestCase, *, status=200, error=None) -> ExecutionResult:
    return ExecutionResult(
        test_case_id=tc.id,
        status_code=None if error else status,
        network_error=error,
    )


def make_claude_response(results: list[dict]):
    """Build a fake Anthropic Message with a validate_results tool_use block."""
    tool_use = types.SimpleNamespace(
        type="tool_use",
        input={"results": results},
    )
    return types.SimpleNamespace(content=[tool_use])


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_list_returns_empty():
    results = await ai_validate_batch([], [])
    assert results == []


# ---------------------------------------------------------------------------
# Claude success path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_uses_claude_verdict_passed():
    tc = make_tc()
    exec_ = make_exec(tc, status=200)
    response = make_claude_response([
        {"test_case_id": tc.id, "passed": True, "failures": [], "reasoning": "2xx = PASS"}
    ])

    with patch("app.core.ai_validator.chat_with_tools", new=AsyncMock(return_value=response)):
        results = await ai_validate_batch([tc], [exec_])

    assert len(results) == 1
    assert results[0].passed is True


@pytest.mark.asyncio
async def test_uses_claude_verdict_failed():
    tc = make_tc()
    exec_ = make_exec(tc, status=404)
    response = make_claude_response([
        {"test_case_id": tc.id, "passed": False, "failures": ["Expected 2xx, got 404"], "reasoning": "FAIL"}
    ])

    with patch("app.core.ai_validator.chat_with_tools", new=AsyncMock(return_value=response)):
        results = await ai_validate_batch([tc], [exec_])

    assert results[0].passed is False
    assert len(results[0].failures) > 0


@pytest.mark.asyncio
async def test_reasoning_preserved():
    tc = make_tc()
    exec_ = make_exec(tc, status=200)
    response = make_claude_response([
        {"test_case_id": tc.id, "passed": True, "failures": [], "reasoning": "chain-of-thought"}
    ])

    with patch("app.core.ai_validator.chat_with_tools", new=AsyncMock(return_value=response)):
        results = await ai_validate_batch([tc], [exec_])

    assert results[0].reasoning == "chain-of-thought"


# ---------------------------------------------------------------------------
# Fallback on Claude failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_falls_back_on_claude_exception():
    tc = make_tc()
    exec_ = make_exec(tc, status=200)

    with patch("app.core.ai_validator.chat_with_tools", new=AsyncMock(side_effect=RuntimeError("API down"))):
        results = await ai_validate_batch([tc], [exec_])

    # heuristic: status 200, expected [200] → PASS
    assert len(results) == 1
    assert results[0].passed is True


@pytest.mark.asyncio
async def test_falls_back_when_no_tool_use_in_response():
    tc = make_tc()
    exec_ = make_exec(tc, status=404)
    # Response with no tool_use block
    empty_response = types.SimpleNamespace(content=[
        types.SimpleNamespace(type="text", text="I cannot evaluate this.")
    ])

    with patch("app.core.ai_validator.chat_with_tools", new=AsyncMock(return_value=empty_response)):
        results = await ai_validate_batch([tc], [exec_])

    # heuristic: 404 not in [200] → FAIL
    assert results[0].passed is False


# ---------------------------------------------------------------------------
# Partial results — missing TCs fall back to heuristic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_partial_claude_results_heuristic_fills_gap():
    tc1 = make_tc()
    tc2 = make_tc()
    exec1 = make_exec(tc1, status=200)
    exec2 = make_exec(tc2, status=500)  # heuristic should FAIL this

    # Claude only returns verdict for tc1
    response = make_claude_response([
        {"test_case_id": tc1.id, "passed": True, "failures": [], "reasoning": "ok"}
    ])

    with patch("app.core.ai_validator.chat_with_tools", new=AsyncMock(return_value=response)):
        results = await ai_validate_batch([tc1, tc2], [exec1, exec2])

    assert len(results) == 2
    r1 = next(r for r in results if r.test_case_id == tc1.id)
    r2 = next(r for r in results if r.test_case_id == tc2.id)
    assert r1.passed is True    # Claude verdict
    assert r2.passed is False   # heuristic: 500 not in [200]


# ---------------------------------------------------------------------------
# Multiple TCs in one batch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_multiple_tcs():
    tcs = [make_tc() for _ in range(5)]
    execs = [make_exec(tc, status=200) for tc in tcs]
    claude_results = [
        {"test_case_id": tc.id, "passed": True, "failures": [], "reasoning": "ok"}
        for tc in tcs
    ]
    response = make_claude_response(claude_results)

    with patch("app.core.ai_validator.chat_with_tools", new=AsyncMock(return_value=response)):
        results = await ai_validate_batch(tcs, execs)

    assert len(results) == 5
    assert all(r.passed for r in results)


# ---------------------------------------------------------------------------
# Execution result missing from exec_map — TC skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tc_without_matching_exec_skipped():
    tc1 = make_tc()
    tc2 = make_tc()
    # Only exec for tc1
    exec1 = make_exec(tc1, status=200)
    response = make_claude_response([
        {"test_case_id": tc1.id, "passed": True, "failures": [], "reasoning": "ok"}
    ])

    with patch("app.core.ai_validator.chat_with_tools", new=AsyncMock(return_value=response)):
        results = await ai_validate_batch([tc1, tc2], [exec1])

    # tc2 has no exec → skipped
    ids = {r.test_case_id for r in results}
    assert tc1.id in ids
    assert tc2.id not in ids
