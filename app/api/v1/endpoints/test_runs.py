from __future__ import annotations

import asyncio
import json
from typing import Annotated, Optional

from fastapi import APIRouter, Form, HTTPException, Query, UploadFile, File, status
from fastapi.responses import Response, StreamingResponse

from app.config import settings
from app.models.request import GenerateConfig, ExecuteConfig, LLMProvider, TestStrategy
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
    strategy: Annotated[TestStrategy, Form()] = TestStrategy.standard,
    auth_headers: Annotated[Optional[str], Form(description="JSON object of auth headers")] = None,
    max_tc_per_endpoint: Annotated[Optional[int], Form(ge=1, le=100)] = None,
    enable_rate_limit_tests: Annotated[bool, Form()] = False,
    phase1_provider: Annotated[LLMProvider, Form(description="Phase 1 provider: test planning (local=skip)")] = LLMProvider.local,
    phase2_provider: Annotated[LLMProvider, Form(description="Phase 2 provider: TC generation")] = LLMProvider.local,
    phase3_provider: Annotated[LLMProvider, Form(description="Phase 3 provider: AI validation (local=heuristic)")] = LLMProvider.local,
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
        strategy=strategy,
        auth_headers=headers,
        max_tc_per_endpoint=max_tc_per_endpoint,
        enable_rate_limit_tests=enable_rate_limit_tests,
        phase1_provider=phase1_provider,
        phase2_provider=phase2_provider,
        phase3_provider=phase3_provider,
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
    strategy: Annotated[TestStrategy, Form()] = TestStrategy.standard,
    auth_headers: Annotated[Optional[str], Form(description="JSON object of auth headers")] = None,
    max_tc_per_endpoint: Annotated[Optional[int], Form(ge=1, le=100)] = None,
    timeout_seconds: Annotated[int, Form(ge=1, le=300)] = 30,
    postman_file: Annotated[Optional[UploadFile], File(description="Postman collection JSON (optional). Claude uses it as reference to generate better test cases.")] = None,
    variables_file: Annotated[Optional[UploadFile], File(description="JSON file mapping Postman {{variable}} names to values")] = None,
    auth_context: Annotated[Optional[UploadFile], File(description="Markdown (.md) describing the authentication mechanism (e.g. challenge/connect flow, required headers, error codes).")] = None,
    scenario_context: Annotated[Optional[UploadFile], File(description="Markdown (.md) describing business domain rules, workflows, or scenario guidance for test generation.")] = None,
    phase1_provider: Annotated[LLMProvider, Form(description="Phase 1 provider: test planning (local=skip)")] = LLMProvider.local,
    phase2_provider: Annotated[LLMProvider, Form(description="Phase 2 provider: TC generation")] = LLMProvider.local,
    phase3_provider: Annotated[LLMProvider, Form(description="Phase 3 provider: AI validation (local=heuristic)")] = LLMProvider.local,
) -> dict:
    """Upload spec and run all three stages (parse → generate → execute) automatically.
    Optionally attach a Postman collection or one/more Markdown context files — Claude will use them to generate better test cases.
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

    context_md: str | None = None
    parts: list[str] = []
    if auth_context:
        content = (await auth_context.read()).decode("utf-8", errors="replace").strip()
        if content:
            parts.append(f"## Auth Context\n\n{content}")
    if scenario_context:
        content = (await scenario_context.read()).decode("utf-8", errors="replace").strip()
        if content:
            parts.append(f"## Scenario Context\n\n{content}")
    context_md = "\n\n---\n\n".join(parts) or None

    run_id = test_orchestrator.start_full_run(
        raw_spec=raw,
        filename=spec_file.filename or "",
        generate_config=GenerateConfig(
            strategy=strategy,
            auth_headers=headers,
            max_tc_per_endpoint=max_tc_per_endpoint,
            phase1_provider=phase1_provider,
            phase2_provider=phase2_provider,
            phase3_provider=phase3_provider,
        ),
        execute_config=ExecuteConfig(
            target_base_url=target_base_url,
            timeout_seconds=timeout_seconds,
        ),
        postman_raw=postman_raw,
        postman_variables=vars_map,
        context_md=context_md,
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
    format: Optional[str] = Query(None, description="Response format: 'junit' | 'md' | 'md-failures'"),
) -> Response:
    """Query execution results. Returns partial results while executing.
    Use ?passed=true/false to filter. Use ?track=individual|crud|business to filter by track.
    Use ?page/page_size for pagination.
    Use ?format=junit for JUnit XML.
    Use ?format=md for full Markdown summary report.
    Use ?format=md-failures for a detailed Markdown report of failed test cases only (with request/response/reasoning)."""
    run = test_orchestrator.get_run(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found")

    ind_results = test_orchestrator.get_results(run_id, passed=passed) or []
    crud_results = run.crud_scenario_results
    business_results = run.business_scenario_results

    if format == "junit":
        xml = _generate_junit_xml(run_id, ind_results)
        return Response(content=xml, media_type="application/xml")

    if format == "md":
        md = _generate_markdown_report(run_id, run, ind_results, crud_results, business_results)
        return Response(
            content=md,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="test-report-{run_id[:8]}.md"'},
        )

    if format == "md-failures":
        # Use unfiltered results so we always get all failures regardless of ?passed param
        all_ind = run.results or []
        all_crud = run.crud_scenario_results or []
        all_biz = run.business_scenario_results or []
        md = _generate_failures_markdown(run_id, run, all_ind, all_crud, all_biz)
        return Response(
            content=md,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="failures-{run_id[:8]}.md"'},
        )

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


@router.get("/{run_id}/plan")
async def get_plan(
    run_id: str,
    method: str | None = None,
    path: str | None = None,
    scenario_type: str | None = None,
    format: Optional[str] = Query(None, description="Response format: 'md' for Markdown"),
) -> Response:
    """Return the Phase 1 test plan (available after 'analyzing' completes).

    Query params:
    - **method**: filter individual_tests by HTTP method, e.g. `?method=GET`
    - **path**: substring filter on endpoint path, e.g. `?path=/users`
    - **scenario_type**: filter scenarios by type — `crud` or `business`
    - **format**: `md` for Markdown download

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
    if format == "md":
        md = _generate_plan_markdown(run_id, plan)
        return Response(
            content=md,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="phase1-plan-{run_id[:8]}.md"'},
        )
    from fastapi.responses import JSONResponse
    return JSONResponse(plan.model_dump())


@router.get("/{run_id}/test-cases")
async def get_test_cases(
    run_id: str,
    format: Optional[str] = Query(None, description="Response format: 'junit' | 'md'"),
) -> Response:
    result = test_orchestrator.get_test_cases(run_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found")
    if format == "junit":
        xml = _generate_test_cases_junit_xml(run_id, result)
        return Response(
            content=xml,
            media_type="application/xml",
            headers={"Content-Disposition": f'attachment; filename="phase2-testcases-{run_id[:8]}.xml"'},
        )
    if format == "md":
        md = _generate_test_cases_markdown(run_id, result)
        return Response(
            content=md,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="phase2-testcases-{run_id[:8]}.md"'},
        )
    from fastapi.responses import JSONResponse
    return JSONResponse([tc.model_dump() for tc in result])


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
    phase2_provider: LLMProvider = Query(LLMProvider.local),
    strategy: str = Query("standard"),
) -> CostEstimateResponse:
    """Estimate token cost before running AI TC generation."""
    run = test_orchestrator.get_run(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run '{run_id}' not found")
    estimate = test_orchestrator.get_cost_estimate(run_id, phase2_provider=phase2_provider.value, strategy=strategy)
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

def _generate_plan_markdown(run_id: str, plan) -> str:
    lines: list[str] = [
        f"# Phase 1 — Test Plan",
        f"",
        f"**Run ID:** `{run_id}`",
        f"",
        f"| | Count |",
        f"|---|---:|",
        f"| Endpoints | {plan.total_endpoints} |",
        f"| Planned test cases | {plan.total_planned_cases} |",
        f"| CRUD scenarios | {plan.crud_scenario_count} |",
        f"| Business scenarios | {plan.business_scenario_count} |",
        f"",
    ]

    if plan.individual_tests:
        lines += ["---", "", "## Individual Tests", ""]
        for ep in plan.individual_tests:
            lines.append(f"### `{ep.method} {ep.path}` — {len(ep.planned_cases)} cases")
            lines.append("")
            for c in ep.planned_cases:
                tag = f" `[{c.test_type}]`" if c.test_type else ""
                lines.append(f"- {c.description}{tag}")
            lines.append("")

    if plan.scenarios:
        crud = [s for s in plan.scenarios if s.scenario_type == "crud"]
        business = [s for s in plan.scenarios if s.scenario_type == "business"]

        if crud:
            lines += ["---", "", "## CRUD Scenarios", ""]
            for s in crud:
                lines.append(f"### {s.name}")
                lines.append(f"> {s.description}")
                lines.append("")
                for i, step in enumerate(s.steps, 1):
                    lines.append(f"{i}. `{step}`")
                lines.append("")
                lines.append(f"*{s.rationale}*")
                lines.append("")

        if business:
            lines += ["---", "", "## Business Scenarios", ""]
            for s in business:
                domains = ", ".join(s.domains) if s.domains else "—"
                lines.append(f"### {s.name}")
                lines.append(f"> {s.description}")
                lines.append(f"")
                lines.append(f"**Domains:** {domains}")
                lines.append("")
                for i, step in enumerate(s.steps, 1):
                    lines.append(f"{i}. `{step}`")
                lines.append("")
                lines.append(f"*{s.rationale}*")
                lines.append("")

    return "\n".join(lines)


def _generate_test_cases_markdown(run_id: str, test_cases: list) -> str:
    from collections import defaultdict

    by_ep: dict = defaultdict(list)
    for tc in test_cases:
        by_ep[tc.endpoint].append(tc)

    security_tcs = [tc for tc in test_cases if tc.security_test_type]

    lines: list[str] = [
        f"# Phase 2 — Generated Test Cases",
        f"",
        f"**Run ID:** `{run_id}`",
        f"",
        f"| | Count |",
        f"|---|---:|",
        f"| Total test cases | {len(test_cases)} |",
        f"| Endpoints covered | {len(by_ep)} |",
        f"| Security test cases | {len(security_tcs)} |",
        f"",
    ]

    for ep, cases in by_ep.items():
        lines.append("---")
        lines.append("")
        lines.append(f"## `{ep}`")
        lines.append("")
        lines.append(f"| # | Description | Type | Expected Status |")
        lines.append(f"|---|-------------|------|:--------------:|")
        for i, tc in enumerate(cases, 1):
            type_tag = f"`{tc.security_test_type}`" if tc.security_test_type else "functional"
            expected = ", ".join(str(s) for s in tc.expected_response.status_codes) if tc.expected_response else "—"
            lines.append(f"| {i} | {tc.description} | {type_tag} | {expected} |")
        lines.append("")

    return "\n".join(lines)


def _generate_markdown_report(run_id: str, run, ind_results: list, crud_results: list, business_results: list) -> str:
    from datetime import timezone
    from collections import defaultdict

    ts = run.completed_at or run.created_at
    ts_str = ts.strftime("%Y-%m-%d %H:%M UTC") if ts else "—"

    ind_pass = sum(1 for r in ind_results if r.passed)
    ind_fail = len(ind_results) - ind_pass
    crud_pass = sum(1 for s in crud_results if s.passed)
    crud_fail = len(crud_results) - crud_pass
    biz_pass = sum(1 for s in business_results if s.passed)
    biz_fail = len(business_results) - biz_pass
    total = len(ind_results) + len(crud_results) + len(business_results)
    total_pass = ind_pass + crud_pass + biz_pass
    total_fail = ind_fail + crud_fail + biz_fail

    # Collect vulnerabilities (security tests that failed = potential vulnerability)
    vulns = [r for r in ind_results if not r.passed and getattr(r, "security_test_type", None)]

    lines: list[str] = [
        f"# API Test Report",
        f"",
        f"**Run ID:** `{run_id}`  ",
        f"**Generated:** {ts_str}  ",
        f"**Status:** {run.status}",
        f"",
        f"---",
        f"",
        f"## Summary",
        f"",
        f"| Track | Total | Pass | Fail |",
        f"|-------|------:|-----:|-----:|",
        f"| Individual | {len(ind_results)} | {ind_pass} | {ind_fail} |",
        f"| CRUD Scenarios | {len(crud_results)} | {crud_pass} | {crud_fail} |",
        f"| Business Scenarios | {len(business_results)} | {biz_pass} | {biz_fail} |",
        f"| **Total** | **{total}** | **{total_pass}** | **{total_fail}** |",
        f"",
    ]

    if vulns:
        lines += [
            f"---",
            f"",
            f"## ⚠️ Potential Vulnerabilities ({len(vulns)})",
            f"",
            f"| Endpoint | Type | Status Code | Description |",
            f"|----------|------|:-----------:|-------------|",
        ]
        for r in vulns:
            sc = getattr(r, "status_code", "—")
            stype = getattr(r, "security_test_type", "")
            lines.append(f"| `{r.endpoint}` | {stype} | {sc} | {r.description} |")
        lines.append("")

    if ind_results:
        lines += ["---", "", "## Individual Test Results", ""]
        by_ep: dict = defaultdict(list)
        for r in ind_results:
            by_ep[r.endpoint].append(r)
        for ep, cases in by_ep.items():
            ep_pass = sum(1 for r in cases if r.passed)
            lines.append(f"### `{ep}` — {ep_pass}/{len(cases)} passed")
            lines.append("")
            lines.append("| # | Description | Result | HTTP |")
            lines.append("|---|-------------|:------:|-----:|")
            for i, r in enumerate(cases, 1):
                icon = "✅" if r.passed else "❌"
                sc = getattr(r, "status_code", "—")
                lines.append(f"| {i} | {r.description} | {icon} | {sc} |")
            lines.append("")

    if crud_results:
        lines += ["---", "", "## CRUD Scenario Results", ""]
        for s in crud_results:
            icon = "✅" if s.passed else "❌"
            lines.append(f"### {icon} {s.name}")
            if getattr(s, "steps", None):
                lines.append("")
                lines.append("| Step | Endpoint | Result | HTTP |")
                lines.append("|-----:|----------|:------:|-----:|")
                for i, step in enumerate(s.steps, 1):
                    step_icon = "✅" if getattr(step, "passed", True) else "❌"
                    sc = getattr(step, "status_code", "—")
                    ep = getattr(step, "endpoint", "—")
                    lines.append(f"| {i} | `{ep}` | {step_icon} | {sc} |")
            lines.append("")

    if business_results:
        lines += ["---", "", "## Business Scenario Results", ""]
        for s in business_results:
            icon = "✅" if s.passed else "❌"
            lines.append(f"### {icon} {s.name}")
            if getattr(s, "steps", None):
                lines.append("")
                lines.append("| Step | Endpoint | Result | HTTP |")
                lines.append("|-----:|----------|:------:|-----:|")
                for i, step in enumerate(s.steps, 1):
                    step_icon = "✅" if getattr(step, "passed", True) else "❌"
                    sc = getattr(step, "status_code", "—")
                    ep = getattr(step, "endpoint", "—")
                    lines.append(f"| {i} | `{ep}` | {step_icon} | {sc} |")
            lines.append("")

    return "\n".join(lines)


def _generate_failures_markdown(run_id: str, run, ind_results: list, crud_results: list, business_results: list) -> str:
    """Detailed Markdown report of failed test cases only.

    For each failure, shows:
    - Full request (method, path, params, body, headers)
    - Actual response (status code, body)
    - AI reasoning (if available)
    - Specific failure reasons
    """
    import json as _json
    from collections import defaultdict

    ts = run.completed_at or run.created_at
    ts_str = ts.strftime("%Y-%m-%d %H:%M UTC") if ts else "—"

    ind_fail = [r for r in ind_results if not r.passed]
    crud_fail = [s for s in crud_results if not s.passed]
    biz_fail = [s for s in business_results if not s.passed]
    total_fail = len(ind_fail) + len(crud_fail) + len(biz_fail)
    total = len(ind_results) + len(crud_results) + len(business_results)

    lines: list[str] = [
        f"# Failure Report",
        f"",
        f"**Run ID:** `{run_id}`  ",
        f"**Generated:** {ts_str}  ",
        f"**Failed:** {total_fail} / {total} test cases",
        f"",
        f"---",
        f"",
    ]

    if total_fail == 0:
        lines += ["✅ No failures — all test cases passed.", ""]
        return "\n".join(lines)

    def _fmt_json(obj) -> str:
        if obj is None:
            return "_none_"
        try:
            return f"```json\n{_json.dumps(obj, indent=2, ensure_ascii=False)}\n```"
        except Exception:
            return f"```\n{obj}\n```"

    def _fmt_headers(h: dict) -> str:
        if not h:
            return "_none_"
        safe = {k: v for k, v in h.items() if k.lower() not in ("authorization", "x-api-key", "cookie")}
        redacted = {k: "***" for k in h if k.lower() in ("authorization", "x-api-key", "cookie")}
        return _fmt_json({**safe, **redacted})

    # ── Individual failures ────────────────────────────────────────────────
    if ind_fail:
        lines += ["## Individual Test Failures", ""]
        by_ep: dict = defaultdict(list)
        for r in ind_fail:
            by_ep[r.endpoint].append(r)

        for ep, cases in by_ep.items():
            lines += [f"### `{ep}` — {len(cases)} failure(s)", ""]
            for i, r in enumerate(cases, 1):
                req = r.request or {}
                resp = r.response or {}
                sc = resp.get("status_code", "—")
                sec_type = getattr(r, "security_test_type", None)
                stype_badge = f" `[{sec_type}]`" if sec_type else ""

                lines += [
                    f"#### {i}. {r.description}{stype_badge}",
                    f"",
                    f"**Request**",
                    f"",
                    f"- Method/Path: `{req.get('method', '?')} {req.get('path', ep)}`",
                ]
                if req.get("path_params"):
                    lines.append(f"- Path params: `{req['path_params']}`")
                if req.get("query_params"):
                    lines.append(f"- Query params: `{req['query_params']}`")
                if req.get("headers"):
                    lines += [f"- Headers:", f"", _fmt_headers(req["headers"]), f""]
                if req.get("body") is not None:
                    lines += [f"- Body:", f"", _fmt_json(req["body"]), f""]

                lines += [
                    f"",
                    f"**Response**",
                    f"",
                    f"- Status: `{sc}`",
                ]
                if resp.get("body") is not None:
                    lines += [f"- Body:", f"", _fmt_json(resp["body"]), f""]

                if r.failures:
                    lines += [f"", f"**Failure reasons**", f""]
                    for f_msg in r.failures:
                        lines.append(f"- {f_msg}")

                if getattr(r, "reasoning", None):
                    lines += [f"", f"**AI reasoning**", f"", f"> {r.reasoning.strip()}", f""]

                lines += ["---", ""]

    # ── CRUD scenario failures ─────────────────────────────────────────────
    if crud_fail:
        lines += ["## CRUD Scenario Failures", ""]
        for s in crud_fail:
            lines += [f"### {s.name}", f""]
            if s.description:
                lines += [f"_{s.description}_", f""]
            failed_steps = [step for step in (s.steps or []) if not getattr(step, "passed", True)]
            lines += [f"**{len(failed_steps)} step(s) failed out of {len(s.steps or [])}**", f""]
            for step in failed_steps:
                req = step.request or {}
                resp = step.response or {}
                sc = resp.get("status_code", "—")
                lines += [
                    f"#### Step {step.step_index + 1}: {step.description}",
                    f"",
                    f"- Endpoint: `{req.get('method', '?')} {step.endpoint}`",
                ]
                if req.get("path_params"):
                    lines.append(f"- Path params: `{req['path_params']}`")
                if req.get("body") is not None:
                    lines += [f"- Body:", f"", _fmt_json(req["body"]), f""]
                lines += [f"- Status: `{sc}`"]
                if resp.get("body") is not None:
                    lines += [f"- Response:", f"", _fmt_json(resp["body"]), f""]
                if step.failures:
                    lines += [f"", f"**Failure reasons**", f""]
                    for f_msg in step.failures:
                        lines.append(f"- {f_msg}")
                if getattr(step, "extracted_values", None):
                    lines += [f"", f"- Extracted values: `{step.extracted_values}`"]
                lines += ["", "---", ""]

    # ── Business scenario failures ─────────────────────────────────────────
    if biz_fail:
        lines += ["## Business Scenario Failures", ""]
        for s in biz_fail:
            lines += [f"### {s.name}", f""]
            if s.description:
                lines += [f"_{s.description}_", f""]
            failed_steps = [step for step in (s.steps or []) if not getattr(step, "passed", True)]
            lines += [f"**{len(failed_steps)} step(s) failed out of {len(s.steps or [])}**", f""]
            for step in failed_steps:
                req = step.request or {}
                resp = step.response or {}
                sc = resp.get("status_code", "—")
                lines += [
                    f"#### Step {step.step_index + 1}: {step.description}",
                    f"",
                    f"- Endpoint: `{req.get('method', '?')} {step.endpoint}`",
                ]
                if req.get("path_params"):
                    lines.append(f"- Path params: `{req['path_params']}`")
                if req.get("body") is not None:
                    lines += [f"- Body:", f"", _fmt_json(req["body"]), f""]
                lines += [f"- Status: `{sc}`"]
                if resp.get("body") is not None:
                    lines += [f"- Response:", f"", _fmt_json(resp["body"]), f""]
                if step.failures:
                    lines += [f"", f"**Failure reasons**", f""]
                    for f_msg in step.failures:
                        lines.append(f"- {f_msg}")
                if getattr(step, "extracted_values", None):
                    lines += [f"", f"- Extracted values: `{step.extracted_values}`"]
                lines += ["", "---", ""]

    return "\n".join(lines)


def _generate_test_cases_junit_xml(run_id: str, test_cases: list) -> str:
    import json as _json
    total = len(test_cases)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuites>',
        f'  <testsuite name="phase2-{run_id}" tests="{total}" skipped="{total}">',
    ]
    for tc in test_cases:
        classname = tc.endpoint.replace(" ", "_").replace("/", ".")
        name = tc.description.replace('"', "'")
        lines.append(f'    <testcase name="{name}" classname="{classname}">')
        lines.append('      <skipped message="Not yet executed"/>')
        props = []
        if tc.path_params:
            props.append(('path_params', _json.dumps(tc.path_params, ensure_ascii=False)))
        if tc.query_params:
            props.append(('query_params', _json.dumps(tc.query_params, ensure_ascii=False)))
        if tc.body is not None:
            props.append(('body', _json.dumps(tc.body, ensure_ascii=False)))
        if tc.security_test_type:
            props.append(('security_test_type', tc.security_test_type))
        if tc.expected_response and tc.expected_response.status_codes:
            props.append(('expected_status_codes', str(tc.expected_response.status_codes)))
        if props:
            lines.append('      <properties>')
            for k, v in props:
                v_esc = v.replace('"', "'")
                lines.append(f'        <property name="{k}" value="{v_esc}"/>')
            lines.append('      </properties>')
        lines.append('    </testcase>')
    lines += ['  </testsuite>', '</testsuites>']
    return "\n".join(lines)


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
