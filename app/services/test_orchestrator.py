"""Orchestrates the three-stage pipeline: parse → generate → execute."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from app.core.executor import execute_scenario, stream_executions
from app.core.postman_parser import parse_postman_collection
from app.core.spec_parser import parse_spec
from app.core.tc_generator import generate_scenario_test_cases, generate_test_cases
from app.core.tc_planner import plan_test_cases
from app.core.ai_validator import ai_validate_batch
from app.core.validator import detect_vulnerability, validate_result
from app.models.internal import ParsedSpec, TestCase, TestPlan, TestScenario
from app.config import settings
from app.models.request import GenerateConfig, ExecuteConfig, GeneratorType
from app.models.response import (
    CostEstimateResponse,
    EndpointSummary,
    ExpectedResponse,
    GeneratedTestCase,
    PlannedCaseResponse,
    PlannedEndpointResponse,
    PlannedScenarioResponse,
    ScenarioResultResponse,
    ScenarioStepResult,
    TestCaseResultResponse,
    TestPlanResponse,
    TestRunStatusResponse,
    TestRunSummary,
    Vulnerability,
)

logger = logging.getLogger(__name__)

# run_id → TestRunStatusResponse
_store: dict[str, TestRunStatusResponse] = {}
# run_id → background asyncio.Task
_tasks: dict[str, asyncio.Task] = {}
# run_id → ParsedSpec (kept for step 2)
_specs: dict[str, ParsedSpec] = {}
# run_id → TestPlan (Phase 1 output, queryable via GET /plan)
_plans: dict[str, TestPlan] = {}
# run_id → list[TestScenario] for single-domain CRUD scenarios (kept for execution)
_crud_scenarios_internal: dict[str, list[TestScenario]] = {}
# run_id → list[TestScenario] for cross-domain business scenarios (kept for execution)
_business_scenarios_internal: dict[str, list[TestScenario]] = {}
# run_id → list[TestCase] (kept for step 3)
_test_cases_internal: dict[str, list[TestCase]] = {}
# run_id → list[GeneratedTestCase] (response model, queryable)
_test_cases: dict[str, list[GeneratedTestCase]] = {}
# run_id → event log
_run_logs: dict[str, list[str]] = {}


# ── helpers ──────────────────────────────────────────────────────────────────

def _log(run_id: str, msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    _run_logs.setdefault(run_id, []).append(f"[{ts}] {msg}")
    logger.info("[run:%s] %s", run_id[:8], msg)


def _fail(run_id: str, err: Exception) -> None:
    _log(run_id, f"ERROR: {err}")
    run = _store.get(run_id)
    if run:
        _store[run_id] = run.model_copy(update={
            "status": "failed",
            "completed_at": datetime.now(timezone.utc),
            "error_message": str(err),
        })


# ── public read API ───────────────────────────────────────────────────────────

def get_run(run_id: str) -> TestRunStatusResponse | None:
    return _store.get(run_id)


def get_run_logs(run_id: str) -> list[str] | None:
    if run_id not in _store:
        return None
    return _run_logs.get(run_id, [])


def get_test_cases(run_id: str) -> list[GeneratedTestCase] | None:
    if run_id not in _store:
        return None
    return _test_cases.get(run_id, [])


def get_test_case(run_id: str, tc_id: str) -> GeneratedTestCase | None:
    tcs = _test_cases.get(run_id)
    if tcs is None:
        return None
    return next((tc for tc in tcs if tc.id == tc_id), None)


def get_plan(
    run_id: str,
    method: str | None = None,
    path: str | None = None,
    scenario_type: str | None = None,
) -> TestPlanResponse | None:
    """Return the Phase 1 test plan, or None if not yet available.

    Optional filters:
      method        — case-insensitive HTTP method (e.g. "GET")
      path          — substring match against endpoint path (e.g. "/users")
      scenario_type — "crud" | "business" (filters scenarios list only)
    """
    if run_id not in _store:
        return None
    plan = _plans.get(run_id)
    if not plan:
        return None

    if scenario_type == "crud":
        scenarios = plan.crud_scenarios
        endpoints = []
    elif scenario_type == "business":
        scenarios = plan.business_scenarios
        endpoints = []
    else:
        scenarios = plan.crud_scenarios + plan.business_scenarios
        endpoints = plan.individual_tests
        if method:
            endpoints = [ep for ep in endpoints if ep.method.upper() == method.upper()]
        if path:
            endpoints = [ep for ep in endpoints if path.lower() in ep.path.lower()]

    endpoint_responses = [
        PlannedEndpointResponse(
            method=ep.method,
            path=ep.path,
            planned_count=len(ep.planned_cases),
            security_count=sum(1 for c in ep.planned_cases if c.test_type),
            planned_cases=[
                PlannedCaseResponse(description=c.description, test_type=c.test_type)
                for c in ep.planned_cases
            ],
        )
        for ep in endpoints
    ]
    scenario_responses = [
        PlannedScenarioResponse(
            id=s.id,
            name=s.name,
            description=s.description,
            steps=s.steps,
            rationale=s.rationale,
            scenario_type=s.scenario_type,
            domains=s.domains,
        )
        for s in scenarios
    ]

    crud_count = len(plan.crud_scenarios)
    business_count = len(plan.business_scenarios)

    return TestPlanResponse(
        total_endpoints=len(plan.individual_tests),
        total_planned_cases=sum(len(ep.planned_cases) for ep in plan.individual_tests),
        total_scenarios=len(plan.scenarios),
        crud_scenario_count=crud_count,
        business_scenario_count=business_count,
        individual_tests=endpoint_responses,
        scenarios=scenario_responses,
    )


def get_results(run_id: str, passed: bool | None = None) -> list[TestCaseResultResponse] | None:
    run = _store.get(run_id)
    if not run:
        return None
    if passed is None:
        return run.results
    return [r for r in run.results if r.passed == passed]


def cancel_run(run_id: str) -> bool:
    task = _tasks.get(run_id)
    if task and not task.done():
        task.cancel()
        run = _store.get(run_id)
        if run:
            _store[run_id] = run.model_copy(update={
                "status": "cancelled",
                "completed_at": datetime.now(timezone.utc),
            })
        _log(run_id, "Run cancelled")
        return True
    return False


# ── step 1: parse ─────────────────────────────────────────────────────────────

async def _parse_pipeline(run_id: str, raw_spec: bytes, filename: str) -> None:
    try:
        _log(run_id, f"Parsing spec: {filename}")
        spec = await parse_spec(raw_spec, filename)
        _specs[run_id] = spec
        _log(run_id, f"Parsed {len(spec.endpoints)} endpoints ({spec.source_format}, base_url={spec.base_url})")
        _store[run_id] = _store[run_id].model_copy(update={
            "status": "parsed",
            "source_format": spec.source_format,
            "base_url": spec.base_url,
            "endpoints": [
                EndpointSummary(method=ep.method, path=ep.path, summary=ep.summary)
                for ep in spec.endpoints
            ],
        })
    except asyncio.CancelledError:
        pass
    except Exception as e:
        _fail(run_id, e)


def start_parse(raw_spec: bytes, filename: str) -> str:
    run_id = str(uuid.uuid4())
    _store[run_id] = TestRunStatusResponse(
        run_id=run_id,
        status="parsing",
        created_at=datetime.now(timezone.utc),
    )
    task = asyncio.create_task(_parse_pipeline(run_id, raw_spec, filename))
    _tasks[run_id] = task
    return run_id


# ── step 2: generate ──────────────────────────────────────────────────────────

async def _generate_pipeline(run_id: str, config: GenerateConfig, postman_raw: bytes | None = None, postman_variables: dict[str, str] | None = None) -> None:
    try:
        spec = _specs.get(run_id)
        if not spec:
            raise ValueError("Parsed spec not found — run parse first")

        _test_cases[run_id] = []
        _test_cases_internal[run_id] = []
        _crud_scenarios_internal[run_id] = []
        _business_scenarios_internal[run_id] = []

        # ── Phase 1: Analyze (claude mode only) ──────────────────────────────
        plan_by_endpoint = None
        if config.generator == GeneratorType.claude:
            _log(run_id, "Phase 1: Analysing full spec to build test plan…")
            _store[run_id] = _store[run_id].model_copy(update={"status": "analyzing"})
            try:
                plan = await plan_test_cases(spec)
                _plans[run_id] = plan
                plan_by_endpoint = {f"{ep.method} {ep.path}": ep for ep in plan.individual_tests}
                _log(run_id, f"Plan ready: {len(plan.individual_tests)} endpoints, {len(plan.crud_scenarios)} crud + {len(plan.business_scenarios)} business scenarios")
            except Exception as e:
                _log(run_id, f"Planning failed ({e}), falling back to unguided generation")
                plan = None

        # ── Phase 2a: Individual TC generation ───────────────────────────────
        _log(run_id, f"Phase 2: Generating TCs (strategy={config.strategy}, max={config.max_tc_per_endpoint})")
        _store[run_id] = _store[run_id].model_copy(update={
            "status": "generating",
            "test_case_count": 0,
        })

        def _on_ep_done(done: int, ep_total: int, ep_method: str, ep_path: str, new_cases: list) -> None:
            for tc in new_cases:
                _test_cases[run_id].append(GeneratedTestCase(
                    id=tc.id,
                    endpoint=f"{tc.endpoint_method} {tc.endpoint_path}",
                    description=tc.description,
                    path_params=tc.path_params,
                    query_params=tc.query_params,
                    headers=tc.headers,
                    body=tc.body,
                    security_test_type=tc.security_test_type,
                    expected_response=ExpectedResponse(
                        status_codes=tc.expected_status_codes,
                        body_schema=tc.expected_body_schema,
                        body_contains=tc.expected_body_contains,
                    ),
                ))
                _test_cases_internal[run_id].append(tc)
            _log(run_id, f"[{done}/{ep_total}] {ep_method} {ep_path} → {len(new_cases)} TCs")
            _store[run_id] = _store[run_id].model_copy(update={
                "test_case_count": len(_test_cases[run_id]),
            })

        all_cases, skipped, tokens = await generate_test_cases(
            spec,
            generator=config.generator,
            strategy=config.strategy,
            auth_headers=config.auth_headers,
            max_tc_per_endpoint=config.max_tc_per_endpoint,
            on_endpoint_done=_on_ep_done,
            postman_raw=postman_raw,
            postman_variables=postman_variables,
            plan_by_endpoint=plan_by_endpoint,
            enable_rate_limit_tests=config.enable_rate_limit_tests,
        )
        _test_cases_internal[run_id] = all_cases
        cost = _compute_cost(tokens)
        if skipped:
            _store[run_id] = _store[run_id].model_copy(update={
                "skipped_endpoints": skipped,
                "estimated_cost_usd": _store[run_id].estimated_cost_usd + cost,
            })
        elif cost:
            _store[run_id] = _store[run_id].model_copy(update={
                "estimated_cost_usd": _store[run_id].estimated_cost_usd + cost,
            })

        # ── Phase 2b: Scenario TC generation (claude mode only, parallel) ────
        plan = _plans.get(run_id)
        if config.generator == GeneratorType.claude and plan and (plan.crud_scenarios or plan.business_scenarios):
            crud_count = len(plan.crud_scenarios)
            biz_count = len(plan.business_scenarios)
            _log(run_id, f"Phase 2b: Generating {crud_count} crud + {biz_count} business scenarios (parallel)…")
            try:
                crud_gen, business_gen = await asyncio.gather(
                    generate_scenario_test_cases(plan.crud_scenarios, spec, auth_headers=config.auth_headers),
                    generate_scenario_test_cases(plan.business_scenarios, spec, auth_headers=config.auth_headers),
                    return_exceptions=True,
                )
                if not isinstance(crud_gen, Exception):
                    _crud_scenarios_internal[run_id] = crud_gen
                else:
                    _log(run_id, f"CRUD scenario generation failed ({crud_gen}), continuing without crud scenarios")
                if not isinstance(business_gen, Exception):
                    _business_scenarios_internal[run_id] = business_gen
                else:
                    _log(run_id, f"Business scenario generation failed ({business_gen}), continuing without business scenarios")
                _store[run_id] = _store[run_id].model_copy(update={
                    "crud_scenario_count": len(_crud_scenarios_internal[run_id]),
                    "business_scenario_count": len(_business_scenarios_internal[run_id]),
                })
                total_scenarios = len(_crud_scenarios_internal[run_id]) + len(_business_scenarios_internal[run_id])
                total_steps = (
                    sum(len(s.steps) for s in _crud_scenarios_internal[run_id])
                    + sum(len(s.steps) for s in _business_scenarios_internal[run_id])
                )
                _log(run_id, f"Scenarios ready: {total_scenarios} with {total_steps} steps total")
            except Exception as e:
                _log(run_id, f"Scenario generation failed ({e}), continuing without scenarios")

        total_crud = len(_crud_scenarios_internal.get(run_id, []))
        total_biz = len(_business_scenarios_internal.get(run_id, []))
        _log(run_id, f"Generation complete: {len(all_cases)} TCs + {total_crud} crud + {total_biz} business scenarios")
        _store[run_id] = _store[run_id].model_copy(update={
            "status": "generated",
            "test_case_count": len(all_cases),
        })
    except asyncio.CancelledError:
        pass
    except Exception as e:
        _fail(run_id, e)


def start_generate(run_id: str, config: GenerateConfig) -> None:
    task = asyncio.create_task(_generate_pipeline(run_id, config))
    _tasks[run_id] = task


# ── scenario execution helper ─────────────────────────────────────────────────

async def _run_scenario_list(
    scenarios: list[TestScenario],
    run_id: str,
    base_url: str,
    config: ExecuteConfig,
) -> list[ScenarioResultResponse]:
    """Execute a list of scenarios sequentially, returning ScenarioResultResponse list."""
    results: list[ScenarioResultResponse] = []
    for scenario in scenarios:
        _log(run_id, f"[scenario] Running '{scenario.name}' ({len(scenario.steps)} steps)…")
        try:
            step_tuples = await execute_scenario(scenario, base_url=base_url, timeout=config.timeout_seconds)
        except Exception as e:
            _log(run_id, f"[scenario] '{scenario.name}' execution error: {e}")
            continue

        step_tcs = [t for t, _, _ in step_tuples]
        step_execs = [e for _, e, _ in step_tuples]
        step_validations = await ai_validate_batch(step_tcs, step_execs)
        val_map = {v.test_case_id: v for v in step_validations}

        step_results: list[ScenarioStepResult] = []
        for (resolved_tc, execution, extracted) in step_tuples:
            validation = val_map.get(resolved_tc.id) or validate_result(resolved_tc, execution)
            step_results.append(ScenarioStepResult(
                step_index=resolved_tc.step_index or 0,
                test_case_id=resolved_tc.id,
                endpoint=f"{resolved_tc.endpoint_method} {resolved_tc.endpoint_path}",
                description=resolved_tc.description,
                passed=validation.passed,
                request={
                    "method": resolved_tc.endpoint_method,
                    "path": resolved_tc.endpoint_path,
                    "path_params": resolved_tc.path_params,
                    "query_params": resolved_tc.query_params,
                    "headers": resolved_tc.headers,
                    "body": resolved_tc.body,
                },
                response={
                    "status_code": execution.status_code,
                    "body": execution.response_body,
                    "latency_ms": execution.latency_ms,
                    "network_error": execution.network_error,
                },
                failures=validation.failures,
                extracted_values=extracted,
            ))
            status_str = str(execution.status_code) if execution.status_code else f"ERR:{execution.network_error}"
            result_str = "PASS" if validation.passed else f"FAIL({', '.join(validation.failures)})"
            _log(run_id, f"[scenario:{scenario.name}] step{resolved_tc.step_index} {resolved_tc.endpoint_method} {resolved_tc.endpoint_path} → {status_str} {result_str}")

        scenario_passed = all(s.passed for s in step_results)
        results.append(ScenarioResultResponse(
            scenario_id=scenario.id,
            name=scenario.name,
            description=scenario.description,
            passed=scenario_passed,
            steps=step_results,
        ))
    return results


# ── step 3: execute ───────────────────────────────────────────────────────────

async def _execute_pipeline(run_id: str, config: ExecuteConfig) -> None:
    try:
        test_cases = _test_cases_internal.get(run_id, [])
        if not test_cases:
            raise ValueError("No test cases found — run generate first")

        base_url = config.target_base_url
        _log(run_id, f"Executing {len(test_cases)} TCs against {base_url}")
        _store[run_id] = _store[run_id].model_copy(update={
            "status": "executing",
            "base_url": base_url,
            "summary": TestRunSummary(
                total=len(test_cases),
                passed=0,
                failed=0,
                skipped=len(test_cases),
            ),
        })

        tc_map = {tc.id: tc for tc in test_cases}
        results: list[TestCaseResultResponse] = []
        vulnerabilities: list[Vulnerability] = []

        # Execute in chunks so AI validation batches stay manageable
        chunk_size = 10
        chunks = [test_cases[i:i + chunk_size] for i in range(0, len(test_cases), chunk_size)]
        for chunk in chunks:
            chunk_executions = []
            async for execution in stream_executions(chunk, base_url=base_url, timeout=config.timeout_seconds):
                chunk_executions.append(execution)

            validations = await ai_validate_batch(chunk, chunk_executions)
            validation_map = {v.test_case_id: v for v in validations}

            for execution in chunk_executions:
                tc = tc_map[execution.test_case_id]
                validation = validation_map.get(tc.id) or validate_result(tc, execution)
                vuln = detect_vulnerability(tc, execution)

                status_str = str(execution.status_code) if execution.status_code else f"ERR:{execution.network_error}"
                result_str = "PASS" if validation.passed else f"FAIL({', '.join(validation.failures)})"
                _log(run_id, f"{tc.endpoint_method} {tc.endpoint_path} → {status_str} {result_str}")

                results.append(TestCaseResultResponse(
                    test_case_id=tc.id,
                    endpoint=f"{tc.endpoint_method} {tc.endpoint_path}",
                    description=tc.description,
                    passed=validation.passed,
                    request={
                        "method": tc.endpoint_method,
                        "path": tc.endpoint_path,
                        "path_params": tc.path_params,
                        "query_params": tc.query_params,
                        "headers": tc.headers,
                        "body": tc.body,
                    },
                    response={
                        "status_code": execution.status_code,
                        "body": execution.response_body,
                        "latency_ms": execution.latency_ms,
                        "network_error": execution.network_error,
                    },
                    failures=validation.failures,
                    reasoning=validation.reasoning,
                    validation_mode=validation.validation_mode,
                ))
                if vuln:
                    vulnerabilities.append(Vulnerability(
                        test_case_id=vuln.test_case_id,
                        endpoint=vuln.endpoint,
                        severity=vuln.severity,
                        type=vuln.vuln_type,
                        description=vuln.description,
                        evidence=vuln.evidence,
                    ))

            completed = len(results)
            passed_count = sum(1 for r in results if r.passed)
            _store[run_id] = _store[run_id].model_copy(update={
                "results": list(results),
                "vulnerabilities": list(vulnerabilities),
                "summary": TestRunSummary(
                    total=len(test_cases),
                    passed=passed_count,
                    failed=completed - passed_count,
                    skipped=len(test_cases) - completed,
                ),
            })

        # ── Scenario execution (crud + business in parallel) ─────────────────
        crud_scenarios = _crud_scenarios_internal.get(run_id, [])
        business_scenarios = _business_scenarios_internal.get(run_id, [])
        crud_scenario_results, business_scenario_results = await asyncio.gather(
            _run_scenario_list(crud_scenarios, run_id, base_url, config),
            _run_scenario_list(business_scenarios, run_id, base_url, config),
        )

        # ── Final summary ─────────────────────────────────────────────────────
        passed_count = sum(1 for r in results if r.passed)
        crud_sc_passed = sum(1 for s in crud_scenario_results if s.passed)
        biz_sc_passed = sum(1 for s in business_scenario_results if s.passed)
        total_scenarios = len(crud_scenario_results) + len(business_scenario_results)
        latencies = [r.response.get("latency_ms", 0) for r in results if r.response.get("latency_ms")]
        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
        _log(run_id, f"Done — {passed_count}/{len(results)} TCs passed, {crud_sc_passed}/{len(crud_scenario_results)} crud + {biz_sc_passed}/{len(business_scenario_results)} business scenarios passed, {len(vulnerabilities)} vulnerabilities")
        _store[run_id] = _store[run_id].model_copy(update={
            "status": "completed",
            "completed_at": datetime.now(timezone.utc),
            "crud_scenario_results": crud_scenario_results,
            "business_scenario_results": business_scenario_results,
            "summary": TestRunSummary(
                total=len(test_cases),
                passed=passed_count,
                failed=len(results) - passed_count,
                skipped=0,
                crud_scenario_total=len(crud_scenario_results),
                crud_scenario_passed=crud_sc_passed,
                crud_scenario_failed=len(crud_scenario_results) - crud_sc_passed,
                business_scenario_total=len(business_scenario_results),
                business_scenario_passed=biz_sc_passed,
                business_scenario_failed=len(business_scenario_results) - biz_sc_passed,
                avg_latency_ms=round(avg_latency, 2),
            ),
        })

        # ── Webhook notification ──────────────────────────────────────────────
        if config.webhook_url:
            try:
                async with httpx.AsyncClient() as wh_client:
                    await wh_client.post(
                        config.webhook_url,
                        json=_store[run_id].model_dump(mode="json"),
                        timeout=10,
                    )
                _log(run_id, f"Webhook sent to {config.webhook_url}")
            except Exception as wh_err:
                _log(run_id, f"Webhook delivery failed: {wh_err}")

    except asyncio.CancelledError:
        pass
    except Exception as e:
        _fail(run_id, e)


def start_execute(run_id: str, config: ExecuteConfig) -> None:
    task = asyncio.create_task(_execute_pipeline(run_id, config))
    _tasks[run_id] = task


# ── postman helpers ───────────────────────────────────────────────────────────

def _load_postman_into_run(run_id: str, raw: bytes, variables: dict[str, str] | None) -> None:
    """Parse a Postman collection and populate an existing run's test cases."""
    test_cases = parse_postman_collection(raw, variables)
    _log(run_id, f"Imported {len(test_cases)} TCs from Postman collection")

    gen_tcs: list[GeneratedTestCase] = [
        GeneratedTestCase(
            id=tc.id,
            endpoint=f"{tc.endpoint_method} {tc.endpoint_path}",
            description=tc.description,
            path_params=tc.path_params,
            query_params=tc.query_params,
            headers=tc.headers,
            body=tc.body,
            security_test_type=tc.security_test_type,
            expected_response=ExpectedResponse(
                status_codes=tc.expected_status_codes,
                body_schema=tc.expected_body_schema,
                body_contains=tc.expected_body_contains,
            ),
        )
        for tc in test_cases
    ]
    _test_cases_internal[run_id] = test_cases
    _test_cases[run_id] = gen_tcs
    _store[run_id] = _store[run_id].model_copy(update={
        "status": "generated",
        "test_case_count": len(test_cases),
    })


# ── postman import (skip parse/generate, go straight to generated) ────────────

def import_postman(
    raw: bytes,
    variables: dict[str, str] | None = None,
) -> str:
    """Parse a Postman collection and register a run ready for execution."""
    run_id = str(uuid.uuid4())
    _store[run_id] = TestRunStatusResponse(
        run_id=run_id,
        status="generating",
        created_at=datetime.now(timezone.utc),
        source_format="postman",
    )
    try:
        _load_postman_into_run(run_id, raw, variables)
    except Exception as e:
        _fail(run_id, e)
    return run_id


async def _full_run_pipeline(
    run_id: str,
    raw_spec: bytes,
    filename: str,
    generate_config: GenerateConfig,
    execute_config: ExecuteConfig,
    postman_raw: bytes | None = None,
    postman_variables: dict[str, str] | None = None,
) -> None:
    """Sequential parse → generate → execute pipeline.

    Replaces the old _stream_generate_execute_pipeline approach which skipped
    Phase 1 (tc_planner) and scenario generation entirely.
    """
    try:
        await _parse_pipeline(run_id, raw_spec, filename)
        if _store[run_id].status == "failed":
            return
        await _generate_pipeline(run_id, generate_config, postman_raw, postman_variables)
        if _store[run_id].status == "failed":
            return
        await _execute_pipeline(run_id, execute_config)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        _fail(run_id, e)


def start_full_run(
    raw_spec: bytes,
    filename: str,
    generate_config: GenerateConfig,
    execute_config: ExecuteConfig,
    postman_raw: bytes | None = None,
    postman_variables: dict[str, str] | None = None,
) -> str:
    run_id = str(uuid.uuid4())
    _store[run_id] = TestRunStatusResponse(
        run_id=run_id,
        status="parsing",
        created_at=datetime.now(timezone.utc),
    )
    task = asyncio.create_task(
        _full_run_pipeline(run_id, raw_spec, filename, generate_config, execute_config, postman_raw, postman_variables)
    )
    _tasks[run_id] = task
    return run_id


def start_full_run_postman(
    raw: bytes,
    variables: dict[str, str] | None,
    execute_config: ExecuteConfig,
) -> str:
    """Import Postman collection then execute immediately (import → execute)."""
    run_id = str(uuid.uuid4())
    _store[run_id] = TestRunStatusResponse(
        run_id=run_id,
        status="generating",
        created_at=datetime.now(timezone.utc),
        source_format="postman",
    )
    try:
        _load_postman_into_run(run_id, raw, variables)
    except Exception as e:
        _fail(run_id, e)
        return run_id
    task = asyncio.create_task(_execute_pipeline(run_id, execute_config))
    _tasks[run_id] = task
    return run_id


# ── Cost helper ───────────────────────────────────────────────────────────────

def _compute_cost(tokens: dict) -> float:
    return (
        tokens.get("input", 0) * settings.model_input_price_per_mtok / 1_000_000
        + tokens.get("output", 0) * settings.model_output_price_per_mtok / 1_000_000
        + tokens.get("cache_creation", 0) * settings.model_cache_creation_price_per_mtok / 1_000_000
        + tokens.get("cache_read", 0) * settings.model_cache_read_price_per_mtok / 1_000_000
    )


# ── Cost estimate ─────────────────────────────────────────────────────────────

def get_cost_estimate(run_id: str, generator: str, strategy: str) -> CostEstimateResponse | None:
    run = _store.get(run_id)
    if not run:
        return None
    tc_per_ep = {"minimal": 2, "standard": 5, "exhaustive": 8}.get(strategy, 5)
    ep_count = len(run.endpoints)
    tc_count = ep_count * tc_per_ep
    tokens = tc_count * 800
    if generator != "claude":
        return CostEstimateResponse(
            endpoint_count=ep_count,
            estimated_tc_count=tc_count,
            estimated_tokens=0,
            estimated_cost_usd=0.0,
            note="Local generator uses no API tokens.",
        )
    cost = tokens / 1_000_000 * (settings.model_input_price_per_mtok + settings.model_output_price_per_mtok)
    return CostEstimateResponse(
        endpoint_count=ep_count,
        estimated_tc_count=tc_count,
        estimated_tokens=tokens,
        estimated_cost_usd=round(cost, 6),
        note=f"Estimate based on ~800 tokens/TC with {strategy} strategy.",
    )


# ── Memory GC ─────────────────────────────────────────────────────────────────

async def gc_loop() -> None:
    """Hourly GC: evict completed/failed/cancelled runs older than run_ttl_hours."""
    while True:
        await asyncio.sleep(3600)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.run_ttl_hours)
        expired = [
            rid for rid, run in list(_store.items())
            if run.status in {"completed", "failed", "cancelled"}
            and (run.completed_at or run.created_at) < cutoff
        ]
        for rid in expired:
            _store.pop(rid, None)
            _specs.pop(rid, None)
            _plans.pop(rid, None)
            _test_cases.pop(rid, None)
            _test_cases_internal.pop(rid, None)
            _crud_scenarios_internal.pop(rid, None)
            _business_scenarios_internal.pop(rid, None)
            _run_logs.pop(rid, None)
            _tasks.pop(rid, None)
        if expired:
            logger.info("[gc] Evicted %d expired runs", len(expired))


# ── Rerun ─────────────────────────────────────────────────────────────────────

async def _rerun_pipeline(run_id: str, test_cases: list[TestCase], config: ExecuteConfig) -> None:
    """Re-execute a subset of TCs and merge results back into the run."""
    try:
        base_url = config.target_base_url
        tc_ids = {tc.id for tc in test_cases}
        _log(run_id, f"Rerunning {len(test_cases)} TCs against {base_url}")

        from app.core.executor import execute_test_cases as _exec_all
        executions = await _exec_all(test_cases, base_url, timeout=config.timeout_seconds)
        validations = await ai_validate_batch(test_cases, executions)
        val_map = {v.test_case_id: v for v in validations}
        tc_map = {tc.id: tc for tc in test_cases}

        run = _store.get(run_id)
        if not run:
            return

        # Replace existing results for rerun TCs, keep others
        existing = [r for r in run.results if r.test_case_id not in tc_ids]
        new_results: list[TestCaseResultResponse] = []
        for execution in executions:
            tc = tc_map[execution.test_case_id]
            validation = val_map.get(tc.id) or validate_result(tc, execution)
            new_results.append(TestCaseResultResponse(
                test_case_id=tc.id,
                endpoint=f"{tc.endpoint_method} {tc.endpoint_path}",
                description=tc.description,
                passed=validation.passed,
                request={
                    "method": tc.endpoint_method,
                    "path": tc.endpoint_path,
                    "path_params": tc.path_params,
                    "query_params": tc.query_params,
                    "headers": tc.headers,
                    "body": tc.body,
                },
                response={
                    "status_code": execution.status_code,
                    "body": execution.response_body,
                    "latency_ms": execution.latency_ms,
                    "network_error": execution.network_error,
                },
                failures=validation.failures,
                reasoning=validation.reasoning,
                validation_mode=validation.validation_mode,
            ))

        all_results = existing + new_results
        passed_count = sum(1 for r in all_results if r.passed)
        latencies = [r.response.get("latency_ms", 0) for r in all_results if r.response.get("latency_ms")]
        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
        total = run.summary.total if run.summary else len(all_results)
        existing_summary = run.summary
        _store[run_id] = run.model_copy(update={
            "status": "completed",
            "completed_at": datetime.now(timezone.utc),
            "results": all_results,
            "summary": TestRunSummary(
                total=total,
                passed=passed_count,
                failed=len(all_results) - passed_count,
                skipped=0,
                crud_scenario_total=existing_summary.crud_scenario_total if existing_summary else 0,
                crud_scenario_passed=existing_summary.crud_scenario_passed if existing_summary else 0,
                crud_scenario_failed=existing_summary.crud_scenario_failed if existing_summary else 0,
                business_scenario_total=existing_summary.business_scenario_total if existing_summary else 0,
                business_scenario_passed=existing_summary.business_scenario_passed if existing_summary else 0,
                business_scenario_failed=existing_summary.business_scenario_failed if existing_summary else 0,
                avg_latency_ms=round(avg_latency, 2),
            ),
        })
        _log(run_id, f"Rerun done — {passed_count}/{len(all_results)} passed")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        _fail(run_id, e)


async def rerun_test_cases(run_id: str, config: ExecuteConfig, tc_ids: list[str] | None = None) -> bool:
    """Re-execute specific TCs or all network-error TCs if tc_ids is None."""
    tcs = _test_cases_internal.get(run_id, [])
    if not tcs:
        return False

    if tc_ids:
        id_set = set(tc_ids)
        tcs = [tc for tc in tcs if tc.id in id_set]
    else:
        run = _store.get(run_id)
        if not run:
            return False
        error_ids = {
            r.test_case_id
            for r in (run.results or [])
            if r.response.get("network_error")
        }
        tcs = [tc for tc in tcs if tc.id in error_ids]

    if not tcs:
        return False

    task = asyncio.create_task(_rerun_pipeline(run_id, tcs, config))
    _tasks[run_id] = task
    return True
