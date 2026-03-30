"""Generates test cases from a ParsedSpec using Claude API (tool-use)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional

import anthropic
from pydantic import BaseModel, ValidationError, field_validator

logger = logging.getLogger(__name__)

from app.models.internal import EndpointSpec, ParsedSpec, PlannedEndpoint, PlannedScenario, TestCase, TestScenario
from app.models.request import TestStrategy
from app.config import settings
from app.utils.claude_client import chat_with_tools, get_phase_provider, _is_cli_provider
from app.utils.exceptions import TCGenerationError


def _fill_query_params(ep_spec: EndpointSpec, query_params: dict[str, Any]) -> dict[str, Any]:
    """Return query_params with placeholders for any required query param missing from the dict.

    The AI occasionally omits required query parameters (e.g. clientId, pageable).
    This ensures those TCs send something rather than silently skipping the param,
    which would cause the API to return a different error than intended.
    """
    required = [p for p in ep_spec.parameters if p.location == "query" and p.required]
    if not required:
        return query_params
    filled = dict(query_params)
    for param in required:
        if param.name in filled:
            continue
        schema_type = param.schema_.get("type", "string")
        if schema_type == "integer":
            placeholder: Any = 0
        elif schema_type == "boolean":
            placeholder = True
        elif schema_type == "number":
            placeholder = 0.0
        else:
            placeholder = "placeholder"
        filled[param.name] = placeholder
        logger.debug("[tc_generator] Auto-filled missing query param '%s' for %s", param.name, ep_spec.path)
    return filled


def _fill_path_params(path: str, path_params: dict[str, Any]) -> dict[str, Any]:
    """Return path_params with placeholders for any {param} missing from the dict.

    Claude occasionally omits path params despite the prompt instruction. This
    ensures every generated TC can actually fire an HTTP request (and get a real
    4xx back) rather than failing with a synthetic 'Unresolved path params' error.
    """
    required = re.findall(r'\{([^}]+)\}', path)
    if not required:
        return path_params
    filled = dict(path_params)
    for param in required:
        if param not in filled:
            filled[param] = "00000000-0000-0000-0000-000000000001"
            logger.debug("[tc_generator] Auto-filled missing path param '%s' for %s", param, path)
    return filled


# ---------------------------------------------------------------------------
# Pydantic models for validating Claude's tool_use output
# ---------------------------------------------------------------------------

class _TCItem(BaseModel):
    description: str
    path_params: dict[str, Any] = {}
    query_params: dict[str, Any] = {}
    headers: dict[str, Any] = {}
    body: Any = None
    expected_status_codes: list[int]
    expected_body_contains: dict[str, Any] = {}
    security_test_type: Optional[str] = None


class _EndpointBlock(BaseModel):
    endpoint_method: str
    endpoint_path: str
    test_cases: list[_TCItem] = []


class _GenerateOutput(BaseModel):
    endpoints: list[_EndpointBlock] = []

    @field_validator("endpoints", mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                pass
        return v

_STRATEGY_HINT = {
    TestStrategy.minimal: "Focus only on the most critical cases: 1 happy path + 1-2 key error cases per endpoint.",
    TestStrategy.standard: "Cover common cases: happy path, key error cases, and basic security checks.",
    TestStrategy.exhaustive: "Be thorough: cover all edge cases, every error condition, and all security test types listed below.",
}

_TC_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "path_params": {"type": "object"},
        "query_params": {"type": "object"},
        "headers": {"type": "object"},
        "body": {"description": "Request body (any JSON value), null if none"},
        "expected_status_codes": {"type": "array", "items": {"type": "integer"}},
        "expected_body_contains": {"type": "object"},
        "security_test_type": {
            "type": "string",
            "enum": ["auth_bypass", "sql_injection", "xss", "idor", "error_disclosure"],
        },
    },
    "required": ["description", "expected_status_codes"],
}

def _build_generate_tool(max_tc_per_endpoint: int | None = None) -> dict:
    tc_array: dict = {"type": "array", "items": _TC_ITEM_SCHEMA}
    if max_tc_per_endpoint:
        tc_array["maxItems"] = max_tc_per_endpoint
    return {
        "name": "generate_test_cases",
        "description": "Generate test cases for one or more API endpoints.",
        "input_schema": {
            "type": "object",
            "properties": {
                "endpoints": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "endpoint_method": {"type": "string"},
                            "endpoint_path": {"type": "string"},
                            "test_cases": tc_array,
                        },
                        "required": ["endpoint_method", "endpoint_path", "test_cases"],
                    },
                },
            },
            "required": ["endpoints"],
        },
    }

_SYSTEM = """You are a senior QA engineer writing blind API tests, with expertise in security testing.
Given endpoint specifications, generate as many test cases as you judge necessary for adequate coverage.
Call the generate_test_cases tool with your output — do not write free-form text.

Test case types to consider (use your judgment on which apply per endpoint):
1. Happy path — valid inputs, expect 2xx
2. Missing required fields — expect 4xx
3. Wrong field types — expect 4xx
4. Boundary values (empty string, 0, very long string, negative number)
5. Auth missing / invalid — set expected_status_codes to [401, 403] (set security_test_type: "auth_bypass")
6. Non-existent resource — expect 404
7. SQL injection payload in string fields — e.g. "' OR '1'='1" (set security_test_type: "sql_injection")
8. XSS payload in string fields — e.g. "<script>alert(1)</script>" (set security_test_type: "xss")
9. IDOR — access another user's resource using a different ID (set security_test_type: "idor")
10. Trigger verbose error — send malformed input to provoke a 500 with stack trace (set security_test_type: "error_disclosure")

Use realistic example values (not placeholder strings like "string" or 123).
CRITICAL: Every test case MUST populate path_params for ALL path parameters in the endpoint path.
If the path is /users/{userId}/roles, set path_params: {"userId": <some value>} — never leave {param} unresolved.
Use plausible integer or UUID values (e.g. userId: 42, orderId: "a1b2c3").
Only set security_test_type for cases 5-10 above."""


def _endpoint_summary(ep: EndpointSpec) -> str:
    lines = [
        f"Method: {ep.method}",
        f"Path: {ep.path}",
        f"Summary: {ep.summary}",
    ]
    if ep.parameters:
        lines.append("Parameters:")
        for p in ep.parameters:
            lines.append(f"  - {p.name} ({p.location}, required={p.required}): {json.dumps(p.schema_)}")
    if ep.request_body_schema:
        lines.append(f"Request body schema: {json.dumps(ep.request_body_schema)}")
    if ep.expected_responses:
        lines.append("Expected responses:")
        for code, info in ep.expected_responses.items():
            lines.append(f"  {code}: {info.get('description', '')}")
    if ep.security_schemes:
        lines.append(f"Security: {', '.join(ep.security_schemes)}")
    return "\n".join(lines)


def _postman_context_snippet(postman_raw: bytes, postman_variables: dict[str, str] | None = None) -> str:
    """Build a compact auth/header reference from a Postman collection for use in Claude prompts."""
    from app.core.postman_parser import parse_postman_collection
    try:
        cases = parse_postman_collection(postman_raw, postman_variables)
    except Exception:
        return ""
    if not cases:
        return ""

    # Collect unique header values across all requests
    all_headers: dict[str, list[str]] = {}
    for tc in cases:
        for k, v in tc.headers.items():
            bucket = all_headers.setdefault(k, [])
            if v not in bucket:
                bucket.append(v)

    lines = ["Auth/header patterns from Postman collection (use these exact values in every authenticated request):"]
    for key, values in all_headers.items():
        lines.append(f"  {key}: {values[0]}")

    # A few body examples for context
    body_examples = [(tc.endpoint_method, tc.endpoint_path, tc.body) for tc in cases if tc.body is not None][:3]
    if body_examples:
        lines.append("\nExample request bodies:")
        for method, path, body in body_examples:
            lines.append(f"  {method} {path}: {json.dumps(body)}")

    return "\n".join(lines)


async def generate_test_cases(
    spec: ParsedSpec,
    provider: str = "local",
    strategy: TestStrategy = TestStrategy.standard,
    auth_headers: dict[str, str] | None = None,
    max_tc_per_endpoint: int | None = None,
    on_endpoint_done: callable = None,
    postman_raw: bytes | None = None,
    postman_variables: dict[str, str] | None = None,
    plan_by_endpoint: dict[str, PlannedEndpoint] | None = None,
    enable_rate_limit_tests: bool = False,
    context_md: str | None = None,
) -> tuple[list[TestCase], list[str], dict]:
    """Generate test cases — local (free) or claude (AI).

    Returns (test_cases, skipped_endpoints, token_usage).
    token_usage keys: input, output, cache_creation, cache_read.

    plan_by_endpoint: optional dict keyed by "{METHOD} {path}" → PlannedEndpoint
    (provided by tc_planner Phase 1). When present, Claude uses the pre-planned
    case descriptions as concrete guidance instead of deriving them from scratch.
    """
    if provider == "local":
        from app.core.local_tc_generator import generate_local
        cases = generate_local(
            spec,
            strategy=strategy,
            auth_headers=auth_headers,
            max_tc_per_endpoint=max_tc_per_endpoint,
            on_endpoint_done=on_endpoint_done,
            enable_rate_limit_tests=enable_rate_limit_tests,
        )
        return cases, [], {}

    # AI path — sequential batch mode
    strategy_hint = _STRATEGY_HINT[strategy]
    postman_context = _postman_context_snippet(postman_raw, postman_variables) if postman_raw else ""
    context_snippet = f"\n\n## Additional API Context\n{context_md}" if context_md else ""
    # provider is passed in by the caller; fall back to env config if not set
    if not provider:
        provider = get_phase_provider("phase2a")

    # CLI mode: large batch (25) to minimize subprocess calls (each takes ~90s)
    # API mode: small batch (3) for focused, high-quality output per call
    if _is_cli_provider(provider):
        batch_size = 25
    else:
        batch_size = 3 if max_tc_per_endpoint is None else (5 if max_tc_per_endpoint <= 3 else 3 if max_tc_per_endpoint <= 6 else 2)

    endpoints = spec.endpoints
    total = len(endpoints)
    batches = [endpoints[i:i + batch_size] for i in range(0, total, batch_size)]
    all_cases: list[TestCase] = []
    skipped_endpoints: list[str] = []
    total_tokens: dict[str, int] = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
    done_count = 0
    # Adaptive delay: starts at configured value, backs off on 429, recovers on success
    delay = float(settings.tc_batch_delay_seconds)
    consecutive_successes = 0

    for b_idx, batch in enumerate(batches):
        if b_idx > 0:
            await asyncio.sleep(delay)
        ep_labels = ", ".join(f"{ep.method} {ep.path}" for ep in batch)
        logger.info("[tc_generator] Batch %d/%d (delay=%.1fs): %s", b_idx + 1, len(batches), delay, ep_labels)
        try:
            batch_plans = {f"{ep.method} {ep.path}": plan_by_endpoint[f"{ep.method} {ep.path}"] for ep in batch if plan_by_endpoint and f"{ep.method} {ep.path}" in plan_by_endpoint} if plan_by_endpoint else {}
            cases, usage = await _generate_for_batch(batch, strategy_hint, max_tc_per_endpoint, auth_headers or {}, postman_context + context_snippet, settings.claude_tc_model, batch_plans or None, provider=provider)
            total_tokens["input"] += usage.get("input", 0)
            total_tokens["output"] += usage.get("output", 0)
            total_tokens["cache_creation"] += usage.get("cache_creation", 0)
            total_tokens["cache_read"] += usage.get("cache_read", 0)
            consecutive_successes += 1
            # After 3 consecutive successes, halve the delay (floor: 2s)
            if consecutive_successes >= 3:
                delay = max(2.0, delay / 2)
                consecutive_successes = 0
                logger.debug("[tc_generator] Reducing batch delay to %.1fs", delay)
        except anthropic.RateLimitError:
            consecutive_successes = 0
            delay = min(120.0, delay * 2)
            logger.warning("[tc_generator] Rate limited — backing off to %.1fs delay", delay)
            cases = []
            skipped_endpoints.extend(f"{ep.method} {ep.path}" for ep in batch)
        except Exception as e:
            if isinstance(getattr(e, "__cause__", None), (anthropic.BadRequestError, anthropic.AuthenticationError)):
                raise
            consecutive_successes = 0
            logger.warning("[tc_generator] Batch %d failed (%s), falling back to local", b_idx + 1, e)
            # Local fallback for this batch
            from app.core.local_tc_generator import generate_local
            fallback_spec = ParsedSpec(source_format=spec.source_format, endpoints=batch)
            cases = generate_local(fallback_spec, strategy=strategy, auth_headers=auth_headers or {})
            skipped_endpoints.extend(f"{ep.method} {ep.path}" for ep in batch)

        by_ep: dict[str, list[TestCase]] = {}
        for tc in cases:
            by_ep.setdefault(f"{tc.endpoint_method} {tc.endpoint_path}", []).append(tc)
        for ep in batch:
            done_count += 1
            if on_endpoint_done:
                on_endpoint_done(done_count, total, ep.method, ep.path, by_ep.get(f"{ep.method} {ep.path}", []))
        all_cases.extend(cases)

    # Deduplicate by (method, path, description)
    seen: set[tuple] = set()
    deduped: list[TestCase] = []
    for tc in all_cases:
        key = (tc.endpoint_method, tc.endpoint_path, tc.description.lower().strip())
        if key not in seen:
            seen.add(key)
            deduped.append(tc)

    return deduped, skipped_endpoints, total_tokens


async def _generate_for_batch(
    batch: list[EndpointSpec],
    strategy_hint: str,
    max_tc_per_endpoint: int | None,
    auth_headers: dict[str, str],
    postman_context: str = "",
    model: str | None = None,
    batch_plans: dict[str, PlannedEndpoint] | None = None,
    provider: str = "anthropic",
) -> tuple[list[TestCase], dict]:
    auth_hint = f"Auth headers available: {list(auth_headers.keys())}" if auth_headers else "No auth headers provided."

    ep_parts: list[str] = []
    for i, ep in enumerate(batch):
        part = f"--- Endpoint {i + 1} ---\n{_endpoint_summary(ep)}"
        plan = batch_plans.get(f"{ep.method} {ep.path}") if batch_plans else None
        if plan and plan.planned_cases:
            cases_text = "\n".join(
                f"  - {c.description}" + (f" [security:{c.test_type}]" if c.test_type else "")
                for c in plan.planned_cases
            )
            part += f"\nPre-planned cases to generate (use these as your checklist):\n{cases_text}"
        ep_parts.append(part)

    endpoints_text = "\n\n".join(ep_parts)
    cap_line = f"Generate at most {max_tc_per_endpoint} test cases per endpoint." if max_tc_per_endpoint else "Generate as many test cases as you judge necessary for good coverage."
    plan_note = "A test plan has been provided above — generate exactly those cases (and only those)." if batch_plans else ""
    user_prompt = (
        f"Coverage guidance: {strategy_hint}\n"
        f"{cap_line}\n"
        + (f"{plan_note}\n" if plan_note else "")
        + f"Generate test cases for EACH of the following {len(batch)} endpoints.\n"
        f"Set endpoint_method and endpoint_path in every test case exactly as shown.\n\n"
        f"{endpoints_text}\n\n"
        f"{auth_hint}\n\n"
        + (f"{postman_context}\n\n" if postman_context else "")
        + "Include auth headers in test cases that require authentication."
    )

    try:
        response = await chat_with_tools(
            system=_SYSTEM,
            user=user_prompt,
            tools=[_build_generate_tool(max_tc_per_endpoint)],
            tool_choice={"type": "tool", "name": "generate_test_cases"},
            model=model,
            cache_system=True,
            provider=provider,
        )
    except Exception as e:
        raise TCGenerationError(f"Claude API error for batch: {e}") from e

    usage: dict = {}
    if hasattr(response, "usage") and response.usage:
        u = response.usage
        usage = {
            "input": getattr(u, "input_tokens", 0),
            "output": getattr(u, "output_tokens", 0),
            "cache_creation": getattr(u, "cache_creation_input_tokens", 0),
            "cache_read": getattr(u, "cache_read_input_tokens", 0),
        }

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if not tool_use:
        raise TCGenerationError("No tool_use block returned for batch")

    try:
        output = _GenerateOutput.model_validate(tool_use.input)
    except ValidationError as e:
        raise TCGenerationError(f"Claude returned invalid tool_use structure: {e}") from e

    valid = {(ep.method, ep.path) for ep in batch}
    spec_lookup: dict[tuple[str, str], EndpointSpec] = {(ep.method, ep.path): ep for ep in batch}

    cases: list[TestCase] = []
    for ep_block in output.endpoints:
        method = ep_block.endpoint_method.upper()
        path = ep_block.endpoint_path
        if (method, path) not in valid:
            continue
        ep_spec = spec_lookup[(method, path)]
        for tc in ep_block.test_cases:
            tc_headers = {**auth_headers, **tc.headers}
            cases.append(TestCase(
                endpoint_method=method,
                endpoint_path=path,
                description=tc.description,
                path_params=_fill_path_params(path, tc.path_params),
                query_params=_fill_query_params(ep_spec, tc.query_params),
                headers=tc_headers,
                body=tc.body,
                expected_status_codes=tc.expected_status_codes,
                expected_body_contains=tc.expected_body_contains,
                security_test_type=tc.security_test_type,
            ))
    return cases, usage


# ---------------------------------------------------------------------------
# Scenario TC generation
# ---------------------------------------------------------------------------

_SCENARIO_STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "endpoint_method": {"type": "string"},
        "endpoint_path": {"type": "string"},
        "description": {"type": "string"},
        "path_params": {"type": "object"},
        "query_params": {"type": "object"},
        "headers": {"type": "object"},
        "body": {"description": "Request body (any JSON value), null if none"},
        "expected_status_codes": {"type": "array", "items": {"type": "integer"}},
        "extract": {
            "type": "object",
            "description": "Dot-notation paths to extract from the response body for use in subsequent steps. e.g. {\"userId\": \"id\"} extracts response.id as userId.",
        },
    },
    "required": ["endpoint_method", "endpoint_path", "description", "expected_status_codes"],
}

_GENERATE_SCENARIO_TOOL = {
    "name": "generate_scenario_steps",
    "description": "Generate ordered test steps for an integration scenario. Each step may extract values from the response to inject into the next step via {{varName}} templates in path_params, query_params, or body.",
    "input_schema": {
        "type": "object",
        "properties": {
            "scenarios": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "scenario_id": {"type": "string"},
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "steps": {"type": "array", "items": _SCENARIO_STEP_SCHEMA},
                    },
                    "required": ["scenario_id", "name", "description", "steps"],
                },
            }
        },
        "required": ["scenarios"],
    },
}

_SCENARIO_SYSTEM = """You are a senior QA engineer generating integration test scenarios for an API.

Each scenario is a sequence of HTTP calls where later steps depend on values returned by earlier steps.

Rules:
- Use {{varName}} syntax in path_params, query_params, and body to reference values extracted by a previous step.
  Example: if step 1 extracts {"userId": "id"} from the response, step 2 can use {"userId": "{{userId}}"} in path_params.
- In the extract field, use dot notation to navigate the response body: "id", "data.id", "user.profile.id".
- Use realistic values for any fields not extracted from previous steps.
- CRITICAL: Every step must populate path_params for ALL path parameters in the endpoint path — never leave a {param} empty.
  If a prior step creates a resource (e.g. POST /api/tenants returns a tenant id), you MUST:
  (a) add extract: {"tenantId": "<json-path-to-id>"} on that step, AND
  (b) set path_params: {"tenantId": "{{tenantId}}"} on every subsequent step whose path contains {tenantId}.
- Set expected_status_codes appropriately for each step (2xx for success steps).
- Always include auth headers in steps that require authentication."""


class _ScenarioStepRaw(BaseModel):
    endpoint_method: str
    endpoint_path: str
    description: str
    path_params: dict[str, Any] = {}
    query_params: dict[str, Any] = {}
    headers: dict[str, Any] = {}
    body: Any = None
    expected_status_codes: list[int]
    extract: dict[str, str] = {}


class _GeneratedScenarioRaw(BaseModel):
    scenario_id: str
    name: str
    description: str
    steps: list[_ScenarioStepRaw] = []


class _GenerateScenariosOutput(BaseModel):
    scenarios: list[_GeneratedScenarioRaw] = []

    @field_validator("scenarios", mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                pass
        return v


async def generate_scenario_test_cases(
    planned_scenarios: list[PlannedScenario],
    spec: ParsedSpec,
    auth_headers: dict[str, str] | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> list[TestScenario]:
    """Phase 2b — generate concrete step-by-step TestCases for each PlannedScenario."""
    if not planned_scenarios:
        return []

    auth_hint = f"Auth headers available: {list(auth_headers.keys())}" if auth_headers else "No auth headers provided."

    # Build a compact endpoint index for Claude to reference
    ep_index = "\n".join(
        f"  {ep.method} {ep.path}" + (f" — {ep.summary}" if ep.summary else "")
        for ep in spec.endpoints
    )

    scenarios_text = "\n\n".join(
        f"Scenario ID: {s.id}\n"
        f"Name: {s.name}\n"
        f"Description: {s.description}\n"
        f"Steps: {' → '.join(s.steps)}\n"
        f"Rationale: {s.rationale}"
        for s in planned_scenarios
    )

    user_prompt = (
        f"Available endpoints:\n{ep_index}\n\n"
        f"Generate concrete test steps for the following {len(planned_scenarios)} integration scenarios.\n\n"
        f"{scenarios_text}\n\n"
        f"{auth_hint}"
    )

    provider = provider or get_phase_provider("phase2b")
    try:
        response = await chat_with_tools(
            system=_SCENARIO_SYSTEM,
            user=user_prompt,
            tools=[_GENERATE_SCENARIO_TOOL],
            tool_choice={"type": "tool", "name": "generate_scenario_steps"},
            model=model or settings.claude_tc_model,
            cache_system=True,
            provider=provider,
        )
    except Exception as e:
        raise TCGenerationError(f"Claude API error for scenario generation: {e}") from e

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if not tool_use:
        raise TCGenerationError("No tool_use block returned for scenario generation")

    try:
        output = _GenerateScenariosOutput.model_validate(tool_use.input)
    except ValidationError as e:
        raise TCGenerationError(f"Scenario tool_use invalid structure: {e}") from e

    id_to_planned = {s.id: s for s in planned_scenarios}
    result: list[TestScenario] = []

    for raw_scenario in output.scenarios:
        planned = id_to_planned.get(raw_scenario.scenario_id)
        if not planned:
            continue
        steps: list[TestCase] = []
        for idx, raw_step in enumerate(raw_scenario.steps):
            tc_headers = {**(auth_headers or {}), **raw_step.headers}
            steps.append(TestCase(
                endpoint_method=raw_step.endpoint_method.upper(),
                endpoint_path=raw_step.endpoint_path,
                description=raw_step.description,
                path_params=_fill_path_params(raw_step.endpoint_path, raw_step.path_params),
                query_params=raw_step.query_params,
                headers=tc_headers,
                body=raw_step.body,
                expected_status_codes=raw_step.expected_status_codes,
                scenario_id=raw_scenario.scenario_id,
                step_index=idx,
                extract=raw_step.extract,
            ))
        result.append(TestScenario(
            id=raw_scenario.scenario_id,
            name=raw_scenario.name,
            description=raw_scenario.description,
            steps=steps,
        ))

    logger.info("[tc_generator] Generated %d scenarios", len(result))
    return result
