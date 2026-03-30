"""Phase 1 — Analyse the full API spec and produce a TestPlan.

Scenarios are split into two types:

  crud       Simple single-domain flows: create → read → update → delete,
             auth flows, dependency chains.  One Claude call.

  business   Cross-domain business transactions discovered via a 2-step process:
               Step A: domain decomposition — Claude identifies which business
                       domains exist and which endpoints belong to each.
               Step B: transaction scenario generation — Claude uses the domain
                       map to create realistic multi-domain flows
                       (e.g. KYC → account open → order → settlement).

Individual tests are planned per-endpoint, batched to stay within token limits.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel as _BaseModel
from pydantic import ValidationError, field_validator as _field_validator

from app.config import settings
from app.models.internal import (
    ParsedSpec,
    PlannedCase,
    PlannedEndpoint,
    PlannedScenario,
    TestPlan,
)
from app.utils.claude_client import chat_with_tools, get_phase_provider
from app.utils.exceptions import TCGenerationError

logger = logging.getLogger(__name__)

_BATCH_SIZE = 25

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_INDIVIDUAL_TESTS_TOOL: dict[str, Any] = {
    "name": "create_test_plan",
    "description": "List what test cases to generate for each endpoint.",
    "input_schema": {
        "type": "object",
        "properties": {
            "individual_tests": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "method": {"type": "string"},
                        "path": {"type": "string"},
                        "planned_cases": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "description": {"type": "string"},
                                    "test_type": {
                                        "type": "string",
                                        "enum": ["auth_bypass", "sql_injection", "xss", "idor", "error_disclosure"],
                                    },
                                },
                                "required": ["description"],
                            },
                        },
                    },
                    "required": ["method", "path", "planned_cases"],
                },
            },
            "scenarios": {
                "type": "array",
                "description": "Leave empty [].",
                "items": {"type": "object"},
            },
        },
        "required": ["individual_tests", "scenarios"],
    },
}

_CRUD_SCENARIOS_TOOL: dict[str, Any] = {
    "name": "create_test_plan",
    "description": "Identify CRUD and auth-flow scenarios within individual domains.",
    "input_schema": {
        "type": "object",
        "properties": {
            "individual_tests": {
                "type": "array",
                "description": "Leave empty [].",
                "items": {"type": "object"},
            },
            "scenarios": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "steps": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Ordered endpoint calls, e.g. ['POST /users', 'GET /users/{id}']",
                        },
                        "rationale": {"type": "string"},
                    },
                    "required": ["name", "description", "steps", "rationale"],
                },
            },
        },
        "required": ["individual_tests", "scenarios"],
    },
}

_DOMAIN_ANALYSIS_TOOL: dict[str, Any] = {
    "name": "analyze_domains",
    "description": "Decompose the API into business domains.",
    "input_schema": {
        "type": "object",
        "properties": {
            "domains": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Short domain name, e.g. 'issuance', 'order', 'settlement'",
                        },
                        "description": {
                            "type": "string",
                            "description": "What business capability this domain handles.",
                        },
                        "endpoint_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Paths belonging to this domain, e.g. ['/rest/v1/dlt/stc/issuances/{id}']",
                        },
                    },
                    "required": ["name", "description", "endpoint_paths"],
                },
            },
        },
        "required": ["domains"],
    },
}

_BUSINESS_SCENARIOS_TOOL: dict[str, Any] = {
    "name": "create_business_scenarios",
    "description": "Create realistic multi-domain business transaction scenarios.",
    "input_schema": {
        "type": "object",
        "properties": {
            "scenarios": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "domains": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Business domains this scenario touches, e.g. ['issuance', 'order']",
                        },
                        "steps": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Ordered endpoint calls",
                        },
                        "rationale": {
                            "type": "string",
                            "description": "What business value or bug this scenario verifies.",
                        },
                    },
                    "required": ["name", "description", "domains", "steps", "rationale"],
                },
            },
        },
        "required": ["scenarios"],
    },
}

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_SYSTEM_INDIVIDUAL = """You are a senior QA architect listing test cases for API endpoints.

For EVERY endpoint, list ALL test cases:
- Happy path (valid inputs)
- Missing / invalid required fields
- Wrong field types / boundary values
- Auth missing or invalid (mark test_type: auth_bypass)
- Non-existent resource (404 scenarios)
- SQL injection, XSS payloads where string fields exist (mark test_type accordingly)
- IDOR where resource IDs exist (mark test_type: idor)
- Error disclosure via malformed input (mark test_type: error_disclosure)

Leave `scenarios` as an empty list."""

_SYSTEM_CRUD_SCENARIOS = """You are a senior QA architect identifying CRUD and auth-flow scenarios.

Focus on single-domain flows:
- Full CRUD lifecycle: create → read → update → delete
- Auth flows: register → login → access protected → logout
- Dependency chains: endpoint whose path param is the ID returned by another endpoint
- Error recovery: create resource → try invalid operation → verify state unchanged

Do NOT create cross-domain business transactions (those are handled separately).
Leave `individual_tests` as an empty list."""

_SYSTEM_DOMAIN_ANALYSIS = """You are a business analyst decomposing an API into its core business domains.

A domain is a coherent set of endpoints that handle a single business capability.

Rules:
- Group endpoints by the business ENTITY and CAPABILITY they serve, NOT by URL prefix alone
- Every endpoint must belong to exactly one domain
- Use concise English domain names (snake_case, e.g. "token_issuance", "investor_account")

For each domain, also identify:
- The PRIMARY ACTORS who use it (e.g. admin, corporate_user, individual_user, operator)
- Whether it has an APPROVAL WORKFLOW (i.e. entities move through states: pending → approved → active)
- Whether it PRODUCES RESOURCES that other domains CONSUME
  (e.g. issuance produces a token → reserve, portfolio, transfer all consume it)

Common domain patterns to look for:
- Setup/onboarding: user registration, KYC, account approval
- Asset lifecycle: creation → approval → activation → issuance
- Reserve/collateral management: liquidity deposits, POR (proof of reserve)
- Transaction execution: conversion, transfer, withdrawal, settlement
- Governance/compliance: AML, freeze, suspend, recovery
- Monitoring/reporting: dashboard, fee management, audit logs"""

_SYSTEM_BUSINESS_SCENARIOS = """You are a senior QA architect creating realistic end-to-end business transaction scenarios.

You are given a domain map showing which business domains exist and which endpoints belong to each.

## What makes a good business scenario

A business scenario tests a COMPLETE, REALISTIC WORKFLOW that a real user or operator would perform.
It must cross multiple domains and exercise state transitions, not just isolated CRUD operations.

### Scenario length
- Minimum 6 steps, typical range 8–15 steps
- Complex flows (initial issuance, full onboarding) can be 15–20 steps
- Every step must be necessary — no filler calls

### Scenario types to generate

1. **Happy path end-to-end**: Full workflow from setup to completion
   Example (stablecoin issuance):
   - POST /mainnet (register mainnet)
   - POST /issuances (create issuance plan)
   - PATCH /issuances/{id} (update plan details)
   - POST /reserves (register reserve account)
   - POST /reserves/{id}/deposit (deposit collateral)
   - POST /issuances/{id}/submit (submit for approval)
   - POST /issuances/{id}/approve (admin approves)
   - POST /issuances/{id}/issue (execute issuance)
   - GET /issuances/{id} (verify issued amount)
   - GET /portfolio/{accountId} (verify holder balance)

2. **Multi-actor approval chain**: Operator creates, admin approves, user receives
   Example (corporate user onboarding):
   - POST /corporate-users (register corporate entity)
   - POST /corporate-users/{id}/kyc (submit KYC documents)
   - POST /corporate-users/{id}/approve (admin approves KYC)
   - POST /corporate-users/{id}/members (invite members)
   - POST /portfolios (create portfolio for the account)
   - POST /portfolios/{id}/permissions (set member permissions)

3. **State-gated negative test**: Attempt an action before prerequisite is satisfied
   Example (trade before KYC):
   - POST /individual-users (register user)
   - POST /conversions (attempt buy — expect 403 or 422, KYC not complete)
   - POST /individual-users/{id}/kyc (submit KYC)
   - POST /individual-users/{id}/approve (admin approves)
   - POST /conversions (retry buy — expect 200/201, now succeeds)

4. **AML / compliance flow**: Detect suspicious activity → freeze → investigate → recover
   Example:
   - POST /transfers (trigger suspicious transfer)
   - GET /aml/detections (verify detection created)
   - POST /aml/detections/{id}/freeze (freeze the account)
   - GET /accounts/{id} (verify account status = frozen)
   - POST /aml/detections/{id}/suspend (escalate to suspended)
   - POST /aml/detections/{id}/recover (lift suspension after investigation)
   - GET /accounts/{id} (verify account restored)

5. **Cross-domain data consistency**: Resource created in one domain is correctly referenced in another
   Example (reserve proof of reserve):
   - POST /reserves (create reserve)
   - POST /reserves/{id}/deposit (add liquidity)
   - POST /issuances/{id}/issue (issue tokens backed by reserve)
   - GET /por/reserves/{id} (verify proof of reserve reflects issued amount)
   - POST /reserves/{id}/withdraw (withdraw partial reserve)
   - GET /por/reserves/{id} (verify POR ratio updated correctly)

## Output rules
- Generate at least one scenario of EACH type that applies to the given domain map
- Each scenario must reference the ACTUAL endpoint paths from the domain map
- `domains` field: list the 2+ domain names this scenario spans
- `steps` field: each step is "METHOD /path" (use exact paths from domain map)
- `rationale`: explain what business risk or integration point this scenario validates
- Do NOT duplicate CRUD flows that only touch one domain (those are handled in crud scenarios)"""

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class _PlannedCaseRaw(_BaseModel):
    description: str
    test_type: str | None = None


class _PlannedEndpointRaw(_BaseModel):
    method: str
    path: str
    planned_cases: list[_PlannedCaseRaw] = []


class _ScenarioRaw(_BaseModel):
    name: str
    description: str
    steps: list[str]
    rationale: str


def _parse_list_if_string(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            pass
    return v


class _PlanOutput(_BaseModel):
    individual_tests: list[_PlannedEndpointRaw] = []
    scenarios: list[_ScenarioRaw] = []

    @_field_validator("individual_tests", "scenarios", mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        return _parse_list_if_string(v)


class _DomainRaw(_BaseModel):
    name: str
    description: str
    endpoint_paths: list[str] = []


class _DomainAnalysisOutput(_BaseModel):
    domains: list[_DomainRaw] = []

    @_field_validator("domains", mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        return _parse_list_if_string(v)


class _BusinessScenarioRaw(_BaseModel):
    name: str
    description: str
    domains: list[str] = []
    steps: list[str]
    rationale: str


class _BusinessScenariosOutput(_BaseModel):
    scenarios: list[_BusinessScenarioRaw] = []

    @_field_validator("scenarios", mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        return _parse_list_if_string(v)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _spec_summary(spec: ParsedSpec) -> str:
    lines: list[str] = []
    if spec.base_url:
        lines.append(f"Base URL: {spec.base_url}")
    lines.append(f"Total endpoints: {len(spec.endpoints)}\n")
    for ep in spec.endpoints:
        lines.append(f"## {ep.method} {ep.path}")
        if ep.summary:
            lines.append(f"Summary: {ep.summary}")
        if ep.parameters:
            for p in ep.parameters:
                lines.append(f"  param {p.name} ({p.location}, required={p.required}): {json.dumps(p.schema_)}")
        if ep.request_body_schema:
            lines.append(f"  body: {json.dumps(ep.request_body_schema)}")
        if ep.expected_responses:
            codes = ", ".join(f"{k}({v.get('description','')})" for k, v in ep.expected_responses.items())
            lines.append(f"  responses: {codes}")
        if ep.security_schemes:
            lines.append(f"  security: {', '.join(ep.security_schemes)}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Individual tests (batched)
# ---------------------------------------------------------------------------

async def _plan_individual_batch(
    batch: list,
    batch_idx: int,
    total_batches: int,
    model: str,
    context_md: str | None = None,
    provider: str = "anthropic",
) -> list[_PlannedEndpointRaw]:
    mini = ParsedSpec(source_format="openapi", base_url=None, endpoints=batch)
    spec_text = _spec_summary(mini)
    context_section = f"\n\n## Additional API Context\n{context_md}" if context_md else ""
    user_prompt = (
        f"API endpoint batch {batch_idx + 1}/{total_batches}:\n\n{spec_text}{context_section}\n\n"
        "For each endpoint, list all test cases to generate. Leave scenarios empty."
    )
    logger.info("[tc_planner] individual_tests batch %d/%d (%d endpoints)", batch_idx + 1, total_batches, len(batch))
    try:
        response = await chat_with_tools(
            system=_SYSTEM_INDIVIDUAL,
            user=user_prompt,
            tools=[_INDIVIDUAL_TESTS_TOOL],
            tool_choice={"type": "tool", "name": "create_test_plan"},
            model=model,
            cache_system=True,
            max_tokens=16384,
            provider=provider,
        )
    except Exception as e:
        logger.warning("[tc_planner] individual batch %d failed (%s) — skipping", batch_idx + 1, e)
        return []

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if not tool_use:
        return []
    try:
        raw = _PlanOutput.model_validate(tool_use.input)
    except ValidationError as e:
        logger.warning("[tc_planner] individual batch %d invalid (%s)", batch_idx + 1, e)
        return []
    return raw.individual_tests


# ---------------------------------------------------------------------------
# CRUD scenarios
# ---------------------------------------------------------------------------

async def _plan_crud_scenarios(spec: ParsedSpec, model: str, context_md: str | None = None, provider: str = "anthropic") -> list[PlannedScenario]:
    spec_text = _spec_summary(spec)
    context_section = f"\n\n## Additional API Context\n{context_md}" if context_md else ""
    user_prompt = (
        f"Here is the complete API specification:\n\n{spec_text}{context_section}\n\n"
        "Identify all CRUD and auth-flow scenarios. Leave individual_tests empty."
    )
    logger.info("[tc_planner] CRUD scenarios 분석 중...")
    try:
        response = await chat_with_tools(
            system=_SYSTEM_CRUD_SCENARIOS,
            user=user_prompt,
            tools=[_CRUD_SCENARIOS_TOOL],
            tool_choice={"type": "tool", "name": "create_test_plan"},
            model=model,
            cache_system=True,
            max_tokens=8192,
            provider=provider,
        )
    except Exception as e:
        logger.warning("[tc_planner] CRUD scenarios call failed (%s)", e)
        return []

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if not tool_use:
        return []
    try:
        raw = _PlanOutput.model_validate(tool_use.input)
    except ValidationError:
        return []

    return [
        PlannedScenario(
            name=s.name,
            description=s.description,
            steps=s.steps,
            rationale=s.rationale,
            scenario_type="crud",
        )
        for s in raw.scenarios
    ]


# ---------------------------------------------------------------------------
# Business scenarios (2-step: domain analysis → transaction scenarios)
# ---------------------------------------------------------------------------

async def _analyze_domains(spec: ParsedSpec, model: str, context_md: str | None = None, provider: str = "anthropic") -> list[_DomainRaw]:
    """Step A: ask Claude to decompose the spec into business domains."""
    spec_text = _spec_summary(spec)
    context_section = f"\n\n## Additional API Context\n{context_md}" if context_md else ""
    user_prompt = (
        f"Here is the complete API specification:\n\n{spec_text}{context_section}\n\n"
        "Identify all business domains and map each endpoint to its domain."
    )
    logger.info("[tc_planner] 도메인 분석 중...")
    try:
        response = await chat_with_tools(
            system=_SYSTEM_DOMAIN_ANALYSIS,
            user=user_prompt,
            tools=[_DOMAIN_ANALYSIS_TOOL],
            tool_choice={"type": "tool", "name": "analyze_domains"},
            model=model,
            cache_system=True,
            max_tokens=8192,
            provider=provider,
        )
    except Exception as e:
        logger.warning("[tc_planner] domain analysis failed (%s)", e)
        return []

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if not tool_use:
        return []
    try:
        raw = _DomainAnalysisOutput.model_validate(tool_use.input)
    except ValidationError:
        return []

    logger.info("[tc_planner] Domains found: %s", [d.name for d in raw.domains])
    return raw.domains


async def _plan_business_scenarios(
    spec: ParsedSpec,
    domains: list[_DomainRaw],
    model: str,
    provider: str = "anthropic",
) -> list[PlannedScenario]:
    """Step B: given domain map, ask Claude to create cross-domain transaction scenarios."""
    if not domains:
        return []

    domain_map = "\n".join(
        f"## Domain: {d.name}\n{d.description}\nEndpoints: {', '.join(d.endpoint_paths)}"
        for d in domains
    )
    user_prompt = (
        f"Domain map:\n\n{domain_map}\n\n"
        "Create realistic multi-domain business transaction scenarios."
    )
    logger.info("[tc_planner] 비즈니스 시나리오 생성 중... (도메인 %d개)", len(domains))
    try:
        response = await chat_with_tools(
            system=_SYSTEM_BUSINESS_SCENARIOS,
            user=user_prompt,
            tools=[_BUSINESS_SCENARIOS_TOOL],
            tool_choice={"type": "tool", "name": "create_business_scenarios"},
            model=model,
            max_tokens=8192,
            provider=provider,
        )
    except Exception as e:
        logger.warning("[tc_planner] business scenarios call failed (%s)", e)
        return []

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if not tool_use:
        return []
    try:
        raw = _BusinessScenariosOutput.model_validate(tool_use.input)
    except ValidationError:
        return []

    return [
        PlannedScenario(
            name=s.name,
            description=s.description,
            steps=s.steps,
            rationale=s.rationale,
            scenario_type="business",
            domains=s.domains,
        )
        for s in raw.scenarios
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def plan_test_cases(
    spec: ParsedSpec,
    model: str | None = None,
    context_md: str | None = None,
    provider: str | None = None,
) -> TestPlan:
    """Phase 1: produce a structured TestPlan.

    Runs sequentially to avoid saturating the rate limiter:
      1. individual_tests batches (25 eps each)
      2. CRUD scenarios (full spec, 1 call)
      3. Domain analysis (full spec, 1 call)
      4. Business scenarios (domain map, 1 call)
    """
    effective_model = model or settings.claude_model
    provider = provider or get_phase_provider("phase1")
    n = len(spec.endpoints)

    # ── Individual tests ────────────────────────────────────────────────────
    batches = [spec.endpoints[i:i + _BATCH_SIZE] for i in range(0, n, _BATCH_SIZE)]
    total = len(batches)

    if total > 1:
        logger.info(
            "[tc_planner] Large spec (%d endpoints) — %d individual_tests batches",
            n, total,
        )

    individual_raw: list[_PlannedEndpointRaw] = []
    for idx, batch in enumerate(batches):
        results = await _plan_individual_batch(batch, idx, total, effective_model, context_md=context_md, provider=provider)
        individual_raw.extend(results)

    # ── CRUD scenarios ───────────────────────────────────────────────────────
    crud_scenarios = await _plan_crud_scenarios(spec, effective_model, context_md=context_md, provider=provider)

    # ── Business scenarios (2-step) ──────────────────────────────────────────
    domains = await _analyze_domains(spec, effective_model, context_md=context_md, provider=provider)
    business_scenarios = await _plan_business_scenarios(spec, domains, effective_model, provider=provider)

    # ── Assemble ─────────────────────────────────────────────────────────────
    individual_tests = [
        PlannedEndpoint(
            method=ep.method.upper(),
            path=ep.path,
            planned_cases=[
                PlannedCase(description=c.description, test_type=c.test_type)
                for c in ep.planned_cases
            ],
        )
        for ep in individual_raw
    ]

    if n > 0 and not individual_tests:
        logger.warning(
            "[tc_planner] 0 individual_tests returned for %d endpoints — "
            "TC generation will proceed without per-endpoint plan guidance", n,
        )

    logger.info(
        "[tc_planner] Plan: %d endpoints, %d crud scenarios, %d business scenarios",
        len(individual_tests), len(crud_scenarios), len(business_scenarios),
    )
    return TestPlan(
        individual_tests=individual_tests,
        crud_scenarios=crud_scenarios,
        business_scenarios=business_scenarios,
    )
