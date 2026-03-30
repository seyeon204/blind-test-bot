"""AI-based test result validation using Claude."""
from __future__ import annotations

import asyncio
import json
import logging

from app.config import settings
from app.models.internal import ExecutionResult, TestCase, ValidationResult
from app.utils.claude_client import chat_with_tools, get_phase_provider, _is_cli_provider

logger = logging.getLogger(__name__)

# Models that support adaptive thinking (thinking: {type: "adaptive"})
_ADAPTIVE_THINKING_MODELS = {
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-opus-4-5",
    "claude-opus-4-1",
    "claude-opus-4-0",
}

_VALIDATE_TOOL = {
    "name": "validate_results",
    "description": "Judge whether each API test case passed or failed based on the actual HTTP response.",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "test_case_id": {"type": "string"},
                        "reasoning": {
                            "type": "string",
                            "description": (
                                "Step-by-step chain-of-thought: "
                                "(1) What is the intent of this test? "
                                "(2) What outcome was expected? "
                                "(3) What did the API actually return? "
                                "(4) Does the actual outcome satisfy the intent? "
                                "Be concise but specific."
                            ),
                        },
                        "passed": {"type": "boolean"},
                        "failures": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Specific failure reasons. Empty list if passed.",
                        },
                    },
                    "required": ["test_case_id", "reasoning", "passed", "failures"],
                },
            }
        },
        "required": ["results"],
    },
}

_SYSTEM = """You are a senior QA engineer evaluating API test results. For each test case, reason through the intent before delivering a verdict.

## Validation rules

- **Happy path**: PASS if 2xx. FAIL if 4xx/5xx (API rejected valid input).
- **Negative tests** (missing/wrong fields, boundary values): PASS if 4xx. FAIL if 2xx (API accepted bad input).
- **auth_bypass**: PASS if any 4xx (401 or 403 both mean "protected"). FAIL if 2xx (authentication vulnerability!).
- **Non-existent resource**: PASS if 404. Borderline-pass if 403 (forbidden ≈ doesn't exist to you).
- **sql_injection / xss**: PASS if 4xx or 200 with sanitised output. FAIL if 2xx with reflected payload, or 500 with error details (possible injection).
- **idor**: PASS if 4xx. FAIL if 2xx (cross-user access succeeded — IDOR vulnerability!).
- **error_disclosure**: PASS if the 500 response does NOT reveal stack traces, SQL, or internal paths. FAIL if it does.
- **Network error**: always FAIL.

## Leniency rules
- 400 ≈ 422 (both are client-error validation rejections — treat them the same).
- 401 ≈ 403 for auth checks (both indicate the endpoint is protected).
- 201 / 202 / 204 count as 2xx success.
- If the test description says "should return X" but received Y and Y is semantically equivalent, use your judgement.
- **Application-level error codes**: Some APIs always return HTTP 200 and encode success/failure in the response body (e.g. `{"code": 1401, "errorCode": "BOOTSTRAP_REQUEST_UNAUTHORIZED"}`). If the body contains a non-success application code (non-zero `code`, or a non-null `errorCode`/`error` field indicating rejection), treat it as semantically equivalent to a 4xx response when evaluating negative tests and auth checks.

## Reasoning requirement
For every test case, reason step-by-step (intent → expectation → actual → verdict) in the `reasoning` field **before** setting `passed`. This prevents snap judgements on ambiguous cases."""


def _summarize(tc: TestCase, execution: ExecutionResult) -> str:
    lines = [
        f"test_case_id: {tc.id}",
        f"description: {tc.description}",
        f"endpoint: {tc.endpoint_method} {tc.endpoint_path}",
    ]
    if tc.security_test_type:
        lines.append(f"security_test_type: {tc.security_test_type}")
    if tc.path_params:
        lines.append(f"path_params: {json.dumps(tc.path_params)}")
    if tc.query_params:
        lines.append(f"query_params: {json.dumps(tc.query_params)}")
    if tc.body is not None:
        body_str = json.dumps(tc.body) if not isinstance(tc.body, str) else tc.body
        lines.append(f"request_body: {body_str[:300]}")
    if tc.expected_status_codes:
        lines.append(f"expected_status_codes: {tc.expected_status_codes}")

    if execution.network_error:
        lines.append(f"result: NETWORK ERROR — {execution.network_error}")
    else:
        lines.append(f"result: HTTP {execution.status_code}")
        # Include key response headers (content-type, location) for context
        useful_headers = {
            k: v for k, v in execution.response_headers.items()
            if k.lower() in ("content-type", "location", "www-authenticate")
        }
        if useful_headers:
            lines.append(f"response_headers: {json.dumps(useful_headers)}")
        body = execution.response_body
        if body is not None:
            body_str = json.dumps(body) if not isinstance(body, str) else body
            lines.append(f"response_body: {body_str[:500]}")
    return "\n".join(lines)


async def _validate_subset(
    test_cases: list[TestCase],
    exec_map: dict[str, ExecutionResult],
    effective_model: str,
    provider: str = "anthropic",
) -> list[ValidationResult]:
    """Send one Claude call for a homogeneous subset (security OR functional) and
    return ValidationResult list. Falls back to heuristic on any error.

    Validation calls bypass the global rate limiter (skip_rate_limit=True) because:
    - They are lightweight reads, not token-heavy generation calls
    - In streaming mode, the rate limiter otherwise serialises generation + validation
      and eliminates any concurrency benefit
    - 429s are still caught and retried by _with_retry in chat_with_tools
    """
    summaries = [_summarize(tc, exec_map[tc.id]) for tc in test_cases if tc.id in exec_map]
    if not summaries:
        return []

    user_prompt = "Evaluate the following API test results:\n\n" + "\n\n---\n\n".join(summaries)

    thinking = (
        {"type": "adaptive"}
        if effective_model in _ADAPTIVE_THINKING_MODELS
        else None
    )

    try:
        response = await asyncio.wait_for(
            chat_with_tools(
                system=_SYSTEM,
                user=user_prompt,
                tools=[_VALIDATE_TOOL],
                tool_choice={"type": "tool", "name": "validate_results"},
                model=effective_model,
                thinking=thinking,
                cache_system=True,
                skip_rate_limit=True,
                provider=provider,
            ),
            timeout=settings.ai_validate_timeout_seconds,
        )
    except (asyncio.TimeoutError, Exception) as e:
        logger.warning("[ai_validator] Claude call failed (%s) — falling back to heuristic", e)
        return _heuristic_fallback(test_cases, exec_map)

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if not tool_use:
        return _heuristic_fallback(test_cases, exec_map)

    id_to_verdict: dict[str, dict] = {
        r["test_case_id"]: r
        for r in tool_use.input.get("results", [])
        if isinstance(r, dict) and "test_case_id" in r
    }

    results: list[ValidationResult] = []
    for tc in test_cases:
        ex = exec_map.get(tc.id)
        if ex is None:
            continue
        verdict = id_to_verdict.get(tc.id)
        if verdict:
            results.append(ValidationResult(
                test_case_id=tc.id,
                passed=bool(verdict.get("passed", False)),
                failures=verdict.get("failures") or [],
                reasoning=verdict.get("reasoning") or None,
                validation_mode="ai",
            ))
        else:
            from app.core.validator import validate_result
            results.append(validate_result(tc, ex))
    return results


async def ai_validate_batch(
    test_cases: list[TestCase],
    executions: list[ExecutionResult],
    model: str | None = None,
    provider: str | None = None,
) -> list[ValidationResult]:
    """Use Claude to judge pass/fail for a batch of execution results.

    Network-error TCs are short-circuited to heuristic FAIL before Claude is
    called — they are always FAIL regardless of semantics, so sending them
    wastes tokens.

    Security TCs and functional TCs are sent in separate Claude calls so the
    model doesn't mix up their opposing pass/fail semantics (e.g. 4xx = PASS
    for auth_bypass, but FAIL for a happy path).

    Falls back to heuristic validator on Claude error.
    """
    if not test_cases:
        return []

    exec_map = {e.test_case_id: e for e in executions}
    effective_model = model or settings.claude_tc_model
    effective_provider = provider or get_phase_provider("phase3")

    # "local" provider: skip AI validation entirely, fall back to heuristic
    if effective_provider == "local":
        return _heuristic_fallback(test_cases, exec_map)

    if effective_model in _ADAPTIVE_THINKING_MODELS:
        logger.debug("[ai_validator] enabling adaptive thinking for model=%s", effective_model)

    # Network errors are always FAIL — skip Claude, validate directly
    network_error_tcs = [
        tc for tc in test_cases
        if tc.id in exec_map and exec_map[tc.id].network_error
    ]
    claude_tcs = [
        tc for tc in test_cases
        if tc.id in exec_map and not exec_map[tc.id].network_error
    ]

    results: list[ValidationResult] = _heuristic_fallback(network_error_tcs, exec_map)

    if not claude_tcs:
        return results

    # Split into security and functional subsets to avoid mixed semantics
    security_tcs = [tc for tc in claude_tcs if tc.security_test_type]
    functional_tcs = [tc for tc in claude_tcs if not tc.security_test_type]

    if functional_tcs:
        results.extend(await _validate_subset(functional_tcs, exec_map, effective_model, provider=effective_provider))
    if security_tcs:
        results.extend(await _validate_subset(security_tcs, exec_map, effective_model, provider=effective_provider))

    return results


def _heuristic_fallback(
    test_cases: list[TestCase],
    exec_map: dict[str, ExecutionResult],
) -> list[ValidationResult]:
    from app.core.validator import validate_result
    return [validate_result(tc, exec_map[tc.id]) for tc in test_cases if tc.id in exec_map]


async def ai_validate_batch_offline(
    test_cases: list[TestCase],
    executions: list[ExecutionResult],
    model: str | None = None,
) -> str:
    """Submit a Batches API job to validate results asynchronously (50% cheaper).
    Returns the batch_id. Poll with ai_resolve_batch_validation() later."""
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    from app.utils.claude_client import get_client

    if not test_cases:
        raise ValueError("No test cases to validate")

    exec_map = {e.test_case_id: e for e in executions}
    summaries = [_summarize(tc, exec_map[tc.id]) for tc in test_cases if tc.id in exec_map]
    if not summaries:
        raise ValueError("No matching executions found")

    user_prompt = "Evaluate the following API test results:\n\n" + "\n\n---\n\n".join(summaries)
    effective_model = model or settings.claude_tc_model

    client = get_client()
    batch = await client.messages.batches.create(
        requests=[
            Request(
                custom_id="validation",
                params=MessageCreateParamsNonStreaming(
                    model=effective_model,
                    max_tokens=8192,
                    system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": user_prompt}],
                    tools=[_VALIDATE_TOOL],
                    tool_choice={"type": "tool", "name": "validate_results"},
                ),
            )
        ]
    )
    logger.info("[ai_validator] Submitted offline batch id=%s for %d test cases", batch.id, len(test_cases))
    return batch.id


async def ai_resolve_batch_validation(
    batch_id: str,
    test_cases: list[TestCase],
    executions: list[ExecutionResult],
) -> list[ValidationResult]:
    """Poll and resolve a Batches API validation job submitted via ai_validate_batch_offline.
    Blocks until the batch ends. Falls back to heuristic on error."""
    import asyncio
    from app.utils.claude_client import get_client

    client = get_client()
    exec_map = {e.test_case_id: e for e in executions}

    # Poll until done
    while True:
        batch = await client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            break
        logger.debug("[ai_validator] Batch %s still processing (%s), waiting 30s…", batch_id, batch.processing_status)
        await asyncio.sleep(30)

    # Retrieve the single result
    id_to_verdict: dict[str, dict] = {}
    async for result in await client.messages.batches.results(batch_id):
        if result.result.type != "succeeded":
            logger.warning("[ai_validator] Batch result %s: %s", result.custom_id, result.result.type)
            continue
        msg = result.result.message
        tool_use = next((b for b in msg.content if b.type == "tool_use"), None)
        if tool_use:
            for r in tool_use.input.get("results", []):
                if isinstance(r, dict) and "test_case_id" in r:
                    id_to_verdict[r["test_case_id"]] = r

    results: list[ValidationResult] = []
    for tc in test_cases:
        ex = exec_map.get(tc.id)
        if ex is None:
            continue
        verdict = id_to_verdict.get(tc.id)
        if verdict:
            results.append(ValidationResult(
                test_case_id=tc.id,
                passed=bool(verdict.get("passed", False)),
                failures=verdict.get("failures") or [],
                reasoning=verdict.get("reasoning") or None,
                validation_mode="ai",
            ))
        else:
            from app.core.validator import validate_result
            results.append(validate_result(tc, ex))
    return results
