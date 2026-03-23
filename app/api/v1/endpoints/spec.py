"""Debug endpoints for inspecting parse and TC-generation stages independently."""
from __future__ import annotations

import json
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Form, HTTPException, UploadFile, File, status

from app.core.spec_parser import parse_spec
from app.core.tc_generator import generate_test_cases
from app.models.request import GenerateConfig, TestStrategy

router = APIRouter(prefix="/spec", tags=["spec"])


@router.post("/parse")
async def parse_spec_file(
    spec_file: Annotated[UploadFile, File(description="API spec file (Swagger YAML/JSON, plain document, or PDF)")],
    target_base_url: Annotated[Optional[str], Form()] = None,
) -> dict[str, Any]:
    """Parse the spec file and return the list of detected endpoints (no TC generation, no execution)."""
    raw = await spec_file.read()
    try:
        parsed = await parse_spec(raw, spec_file.filename or "")
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    return {
        "source_format": parsed.source_format,
        "base_url": parsed.base_url or target_base_url,
        "endpoint_count": len(parsed.endpoints),
        "endpoints": [
            {
                "method": ep.method,
                "path": ep.path,
                "summary": ep.summary,
                "parameters": [
                    {"name": p.name, "location": p.location, "required": p.required}
                    for p in ep.parameters
                ],
                "has_request_body": ep.request_body_schema is not None,
                "expected_responses": list(ep.expected_responses.keys()),
                "security_schemes": ep.security_schemes,
            }
            for ep in parsed.endpoints
        ],
    }


@router.post("/generate")
async def generate_test_cases_dry_run(
    spec_file: Annotated[UploadFile, File(description="API spec file (Swagger YAML/JSON, plain document, or PDF)")],
    target_base_url: Annotated[Optional[str], Form()] = None,
    strategy: Annotated[TestStrategy, Form()] = TestStrategy.standard,
    auth_headers: Annotated[Optional[str], Form(description="JSON string of auth headers")] = None,
    max_tc_per_endpoint: Annotated[Optional[int], Form(ge=1, le=100)] = None,
) -> dict[str, Any]:
    """Parse the spec and generate test cases, but do NOT execute them. Returns the full TC list."""
    raw = await spec_file.read()

    headers: dict[str, str] = {}
    if auth_headers:
        try:
            headers = json.loads(auth_headers)
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="auth_headers must be a valid JSON object string",
            )

    try:
        parsed = await parse_spec(raw, spec_file.filename or "")
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Parse failed: {e}")

    if target_base_url:
        parsed.base_url = target_base_url

    try:
        test_cases = await generate_test_cases(
            parsed,
            strategy=strategy,
            auth_headers=headers,
            max_tc_per_endpoint=max_tc_per_endpoint,
        )
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"TC generation failed: {e}")

    return {
        "source_format": parsed.source_format,
        "base_url": parsed.base_url,
        "endpoint_count": len(parsed.endpoints),
        "test_case_count": len(test_cases),
        "test_cases": [
            {
                "id": tc.id,
                "endpoint": f"{tc.endpoint_method} {tc.endpoint_path}",
                "description": tc.description,
                "path_params": tc.path_params,
                "query_params": tc.query_params,
                "headers": tc.headers,
                "body": tc.body,
                "expected_status_codes": tc.expected_status_codes,
                "security_test_type": tc.security_test_type,
            }
            for tc in test_cases
        ],
    }
