from __future__ import annotations

import asyncio
import json
from typing import Annotated, Optional

from fastapi import APIRouter, Form, HTTPException, Query, UploadFile, File, status
from fastapi.responses import Response, StreamingResponse

from app.config import settings
from app.models.request import GenerateConfig, ExecuteConfig, GeneratorType, TestStrategy
from app.models.response import CostEstimateResponse, GeneratedTestCase, ExpectedResponse, TestPlanResponse, TestRunStatusResponse
from app.services import test_orchestrator
from app.core.postman_parser import is_postman

router = APIRouter(prefix="/test-runs", tags=["test-runs"])


# ── step 1: parse ─────────────────────────────────────────────────────────────

@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_test_run(
    spec_file: Annotated[UploadFile, File(description="API spec file (OpenAPI YAML/JSON, plain document, or PDF)")],
) -> dict:
    """Upload a spec file and start parsing. Returns run_id immediately."""
    raw = await spec_file.read()
    run_id = test_orchestrator.start_parse(raw_spec=raw, filename=spec_file.filename or "")
    return {"run_id": run_id, "status": "parsing"}


# ── step 2: generate ──────────────────────────────────────────────────────────

@router.post("/{run_id}/generate", status_code=status.HTTP_202_ACCEPTED)
async def generate(
    run_id: str,
    generator: Annotated[GeneratorType, Form()] = GeneratorType.local,
    strategy: Annotated[TestStrategy, Form()] = TestStrategy.standard,
    auth_headers: Annotated[Optional[str], Form(description="JSON object of auth headers")] = None,
    max_tc_per_endpoint: Annotated[Optional[int], Form(ge=1, le=100)] = None,
    enable_rate_limit_tests: Annotated[bool, Form()] = False,
) -> dict:
    """Start generating test cases for a parsed run."""
    run = test_orchestrator.get_run(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found")
    if run.status != "parsed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run must be in 'parsed' status to generate (current: {run.status})",
        )

    headers: dict[str, str] = {}
    if auth_headers:
        try:
            headers = json.loads(auth_headers)
        except json.JSONDecodeError:
            raise HTTPException(status_code=422, detail="auth_headers must be a valid JSON object")

    config = GenerateConfig(
        generator=generator,
        strategy=strategy,
        auth_headers=headers,
        max_tc_per_endpoint=max_tc_per_endpoint,
        enable_rate_limit_tests=enable_rate_limit_tests,
    )
    test_orchestrator.start_generate(run_id, config)
    return {"run_id": run_id, "status": "generating"}


# ── step 3: execute ───────────────────────────────────────────────────────────

@router.post("/{run_id}/execute", status_code=status.HTTP_202_ACCEPTED)
async def execute(
    run_id: str,
    target_base_url: Annotated[str, Form(description="Target API base URL, e.g. http://localhost:8080")],
    timeout_seconds: Annotated[int, Form(ge=1, le=300)] = 30,
    webhook_url: Annotated[Optional[str], Form(description="URL to POST results to on completion")] = None,
) -> dict:
    """Start executing the generated test cases."""
    run = test_orchestrator.get_run(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found")
    if run.status != "generated":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run must be in 'generated' status to execute (current: {run.status})",
        )

    config = ExecuteConfig(target_base_url=target_base_url, timeout_seconds=timeout_seconds, webhook_url=webhook_url)
    test_orchestrator.start_execute(run_id, config)
    return {"run_id": run_id, "status": "executing"}


# ── full run ──────────────────────────────────────────────────────────────────

@router.post("/full-run", status_code=status.HTTP_202_ACCEPTED)
async def full_run(
    spec_file: Annotated[UploadFile, File(description="API spec file (OpenAPI YAML/JSON, plain document, or PDF)")],
    target_base_url: Annotated[str, Form(description="Target API base URL, e.g. http://localhost:8080")],
    generator: Annotated[GeneratorType, Form()] = GeneratorType.local,
    strategy: Annotated[TestStrategy, Form()] = TestStrategy.standard,
    auth_headers: Annotated[Optional[str], Form(description="JSON object of auth headers")] = None,
    max_tc_per_endpoint: Annotated[Optional[int], Form(ge=1, le=100)] = None,
    timeout_seconds: Annotated[int, Form(ge=1, le=300)] = 30,
    postman_file: Annotated[Optional[UploadFile], File(description="Postman collection JSON (optional). Claude uses it as reference to generate better test cases.")] = None,
    variables_file: Annotated[Optional[UploadFile], File(description="JSON file mapping Postman {{variable}} names to values")] = None,
) -> dict:
    """Upload spec and run all three stages (parse → generate → execute) automatically.
    Optionally attach a Postman collection — Claude will use its real auth headers and examples to generate better test cases.
    Progress is queryable at any point via GET /{run_id}, /{run_id}/logs, /{run_id}/test-cases.
    """
    raw = await spec_file.read()

    headers: dict[str, str] = {}
    if auth_headers:
        try:
            headers = json.loads(auth_headers)
        except json.JSONDecodeError:
            raise HTTPException(status_code=422, detail="auth_headers must be a valid JSON object")

    postman_raw: bytes | None = None
    vars_map: dict[str, str] | None = None
    if postman_file:
        postman_raw = await postman_file.read()
        if not is_postman(postman_raw):
            raise HTTPException(status_code=422, detail="postman_file does not appear to be a Postman collection (missing info.schema)")
        vars_map = await _parse_variables_file(variables_file) or None

    run_id = test_orchestrator.start_full_run(
        raw_spec=raw,
        filename=spec_file.filename or "",
        generate_config=GenerateConfig(
            generator=generator,
            strategy=strategy,
            auth_headers=headers,
            max_tc_per_endpoint=max_tc_per_endpoint,
        ),
        execute_config=ExecuteConfig(
            target_base_url=target_base_url,
            timeout_seconds=timeout_seconds,
        ),
        postman_raw=postman_raw,
        postman_variables=vars_map,
    )
    return {"run_id": run_id, "status": "parsing"}


# ── postman ────────────────────────────────────────────────────────────────────

async def _parse_variables_file(variables_file: Optional[UploadFile]) -> dict[str, str]:
    if not variables_file:
        return {}
    raw = await variables_file.read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="variables_file must be a valid JSON file")


async def _parse_postman_upload(collection_file: UploadFile, variables_file: Optional[UploadFile]) -> tuple[bytes, dict[str, str]]:
    raw = await collection_file.read()
    if not is_postman(raw):
        raise HTTPException(status_code=422, detail="File does not appear to be a Postman collection (missing info.schema)")
    return raw, await _parse_variables_file(variables_file)


@router.post("/import-postman", status_code=status.HTTP_201_CREATED)
async def import_postman(
    collection_file: Annotated[UploadFile, File(description="Postman collection JSON file")],
    variables_file: Annotated[Optional[UploadFile], File(description="JSON file mapping {{variable}} names to values")] = None,
) -> dict:
    """Import a Postman collection as test cases. Returns run_id in 'generated' status, ready for /execute."""
    raw, vars_map = await _parse_postman_upload(collection_file, variables_file)
    run_id = test_orchestrator.import_postman(raw, vars_map or None)
    run = test_orchestrator.get_run(run_id)
    return {"run_id": run_id, "status": run.status, "test_case_count": run.test_case_count}


@router.post("/postman-full-run", status_code=status.HTTP_202_ACCEPTED)
async def postman_full_run(
    collection_file: Annotated[UploadFile, File(description="Postman collection JSON file")],
    target_base_url: Annotated[str, Form(description="Target API base URL, e.g. http://localhost:8080")],
    timeout_seconds: Annotated[int, Form(ge=1, le=300)] = 30,
    variables_file: Annotated[Optional[UploadFile], File(description="JSON file mapping {{variable}} names to values")] = None,
) -> dict:
    """Import a Postman collection and execute immediately (import → execute).
    Progress is queryable at any point via GET /{run_id}, /{run_id}/logs, /{run_id}/test-cases.
    """
    raw, vars_map = await _parse_postman_upload(collection_file, variables_file)
    execute_config = ExecuteConfig(target_base_url=target_base_url, timeout_seconds=timeout_seconds)
    run_id = test_orchestrator.start_full_run_postman(raw, vars_map or None, execute_config)
    return {"run_id": run_id, "status": "executing"}


# ── queries ───────────────────────────────────────────────────────────────────

@router.get("/{run_id}", response_model=TestRunStatusResponse)
async def get_test_run(run_id: str) -> TestRunStatusResponse:
    run = test_orchestrator.get_run(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found")
    return run


@router.get("/{run_id}/logs")
async def get_run_logs(run_id: str) -> list[str]:
    logs = test_orchestrator.get_run_logs(run_id)
    if logs is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found")
    return logs


@router.get("/{run_id}/results")
async def get_results(
    run_id: str,
    passed: Optional[bool] = Query(None),
    track: Optional[str] = Query(None, description="Filter by track: individual | crud | business"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    format: Optional[str] = Query(None, description="Response format: 'junit' for JUnit XML"),
) -> Response:
    """Query execution results. Returns partial results while executing.
    Use ?passed=true/false to filter. Use ?track=individual|crud|business to filter by track.
    Use ?page/page_size for pagination. Use ?format=junit for JUnit XML."""
    run = test_orchestrator.get_run(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found")

    ind_results = test_orchestrator.get_results(run_id, passed=passed) or []
    crud_results = run.crud_scenario_results
    business_results = run.business_scenario_results

    if format == "junit":
        xml = _generate_junit_xml(run_id, ind_results)
        return Response(content=xml, media_type="application/xml")

    # Build per-track totals (unfiltered)
    ind_all = run.results
    total_summary = {
        "individual": {
            "total": len(ind_all),
            "passed": sum(1 for r in ind_all if r.passed),
            "failed": sum(1 for r in ind_all if not r.passed),
        },
        "crud": {
            "total": len(crud_results),
            "passed": sum(1 for s in crud_results if s.passed),
            "failed": sum(1 for s in crud_results if not s.passed),
        },
        "business": {
            "total": len(business_results),
            "passed": sum(1 for s in business_results if s.passed),
            "failed": sum(1 for s in business_results if not s.passed),
        },
    }

    # Apply track filter
    show_individual = track in (None, "individual")
    show_crud = track in (None, "crud")
    show_business = track in (None, "business")

    # Paginate individual results
    total_count = len(ind_results)
    start = (page - 1) * page_size
    paginated = ind_results[start:start + page_size] if show_individual else []

    from fastapi.responses import JSONResponse
    return JSONResponse({
        "status": run.status,
        "total_summary": total_summary,
        "page": page,
        "page_size": page_size,
        "result_count": total_count if show_individual else 0,
        "results": [r.model_dump() for r in paginated],
        "crud_scenario_results": [s.model_dump() for s in crud_results] if show_crud else [],
        "business_scenario_results": [s.model_dump() for s in business_results] if show_business else [],
    })


@router.get("/{run_id}/plan", response_model=TestPlanResponse)
async def get_plan(
    run_id: str,
    method: str | None = None,
    path: str | None = None,
    scenario_type: str | None = None,
) -> TestPlanResponse:
    """Return the Phase 1 test plan (available after 'analyzing' completes).

    Query params:
    - **method**: filter individual_tests by HTTP method, e.g. `?method=GET`
    - **path**: substring filter on endpoint path, e.g. `?path=/users`
    - **scenario_type**: filter scenarios by type — `crud` or `business`

    Examples:
    - `?scenario_type=business` — cross-domain business transaction scenarios only
    - `?scenario_type=crud` — CRUD / auth-flow scenarios only
    - `?method=POST&path=/orders` — planned cases for a specific endpoint
    """
    if not test_orchestrator.get_run(run_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found")
    plan = test_orchestrator.get_plan(run_id, method=method, path=path, scenario_type=scenario_type)
    if not plan:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Test plan not yet available — run must be past 'analyzing' stage (claude generator only)",
        )
    return plan


@router.get("/{run_id}/test-cases", response_model=list[GeneratedTestCase])
async def get_test_cases(run_id: str) -> list[GeneratedTestCase]:
    result = test_orchestrator.get_test_cases(run_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found")
    return result


@router.get("/{run_id}/test-cases/{tc_id}", response_model=GeneratedTestCase)
async def get_test_case(run_id: str, tc_id: str) -> GeneratedTestCase:
    if not test_orchestrator.get_run(run_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found")
    tc = test_orchestrator.get_test_case(run_id, tc_id)
    if not tc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Test case '{tc_id}' not found")
    return tc


@router.get("/{run_id}/test-cases/{tc_id}/expected-response", response_model=ExpectedResponse)
async def get_expected_response(run_id: str, tc_id: str) -> ExpectedResponse:
    if not test_orchestrator.get_run(run_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found")
    tc = test_orchestrator.get_test_case(run_id, tc_id)
    if not tc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Test case '{tc_id}' not found")
    return tc.expected_response


@router.post("/{run_id}/cancel")
async def cancel_test_run(run_id: str) -> dict:
    if not test_orchestrator.get_run(run_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found")
    cancelled = test_orchestrator.cancel_run(run_id)
    return {"run_id": run_id, "cancelled": cancelled}


@router.get("/{run_id}/estimate", response_model=CostEstimateResponse)
async def estimate_cost(
    run_id: str,
    generator: str = Query("claude"),
    strategy: str = Query("standard"),
) -> CostEstimateResponse:
    """Estimate token cost before running Claude TC generation."""
    run = test_orchestrator.get_run(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found")
    estimate = test_orchestrator.get_cost_estimate(run_id, generator=generator, strategy=strategy)
    if not estimate:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Estimate unavailable")
    return estimate


@router.post("/{run_id}/rerun", status_code=status.HTTP_202_ACCEPTED)
async def rerun(
    run_id: str,
    target_base_url: Annotated[str, Form(description="Target API base URL")],
    tc_ids: Annotated[Optional[str], Form(description="Comma-separated TC IDs to rerun (omit for network-error TCs only)")] = None,
    timeout_seconds: Annotated[int, Form(ge=1, le=300)] = 30,
) -> dict:
    """Re-execute specific test cases or all network-error TCs."""
    run = test_orchestrator.get_run(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found")
    id_list = [t.strip() for t in tc_ids.split(",")] if tc_ids else None
    config = ExecuteConfig(target_base_url=target_base_url, timeout_seconds=timeout_seconds)
    started = await test_orchestrator.rerun_test_cases(run_id, config, tc_ids=id_list)
    if not started:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No matching test cases to rerun")
    return {"run_id": run_id, "status": "executing"}


@router.get("/{run_id}/stream")
async def stream_events(run_id: str) -> StreamingResponse:
    """Server-Sent Events stream of execution results as they complete."""
    if not test_orchestrator.get_run(run_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found")

    async def _generator():
        seen = 0
        while True:
            run = test_orchestrator.get_run(run_id)
            if not run:
                yield f"event: error\ndata: not found\n\n"
                break
            new = run.results[seen:]
            for r in new:
                yield f"data: {r.model_dump_json()}\n\n"
                seen += 1
            if run.status in {"completed", "failed", "cancelled"}:
                yield f"event: done\ndata: {json.dumps({'status': run.status})}\n\n"
                break
            await asyncio.sleep(0.3)

    return StreamingResponse(_generator(), media_type="text/event-stream")


# ── JUnit helper ──────────────────────────────────────────────────────────────

def _generate_junit_xml(run_id: str, results: list) -> str:
    total = len(results)
    failed = sum(1 for r in results if not r.passed)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuites>',
        f'  <testsuite name="{run_id}" tests="{total}" failures="{failed}">',
    ]
    for r in results:
        classname = r.endpoint.replace(" ", "_").replace("/", ".")
        name = r.description.replace('"', "'")
        lines.append(f'    <testcase name="{name}" classname="{classname}">')
        if not r.passed:
            msg = "; ".join(r.failures).replace('"', "'")
            lines.append(f'      <failure message="{msg}"/>')
        lines.append('    </testcase>')
    lines += ['  </testsuite>', '</testsuites>']
    return "\n".join(lines)
