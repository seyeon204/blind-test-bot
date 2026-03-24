from __future__ import annotations

import uuid
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class ParameterSpec(BaseModel):
    name: str
    location: Literal["path", "query", "header", "cookie", "body"]
    required: bool
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")
    example: Any = None

    model_config = {"populate_by_name": True}


class EndpointSpec(BaseModel):
    method: str                                   # GET, POST, ...
    path: str                                     # /users/{id}
    summary: str = ""
    parameters: list[ParameterSpec] = []
    request_body_schema: Optional[dict[str, Any]] = None
    expected_responses: dict[str, dict[str, Any]] = {}  # "200": {schema: ...}
    security_schemes: list[str] = []


class ParsedSpec(BaseModel):
    source_format: Literal["openapi", "swagger", "document"]
    base_url: Optional[str] = None
    endpoints: list[EndpointSpec]
    raw_text: str = ""


class TestCase(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    endpoint_method: str
    endpoint_path: str
    description: str
    path_params: dict[str, Any] = {}
    query_params: dict[str, Any] = {}
    headers: dict[str, str] = {}
    body: Optional[Any] = None
    expected_status_codes: list[int]
    expected_body_schema: Optional[dict[str, Any]] = None
    expected_body_contains: dict[str, Any] = {}
    security_test_type: Optional[str] = None  # "auth_bypass" | "sql_injection" | "xss" | "idor" | "error_disclosure"
    repeat_count: int = 1  # >1 for rate_limit tests
    # Scenario fields (None = standalone test case)
    scenario_id: Optional[str] = None
    step_index: Optional[int] = None
    extract: dict[str, str] = {}  # {"varName": "response.field.path"} — dot notation


class ExecutionResult(BaseModel):
    test_case_id: str
    status_code: Optional[int] = None
    response_headers: dict[str, str] = {}
    response_body: Any = None
    latency_ms: float = 0.0
    network_error: Optional[str] = None


class ValidationResult(BaseModel):
    test_case_id: str
    passed: bool
    failures: list[str] = []
    reasoning: Optional[str] = None  # AI chain-of-thought explanation (None for heuristic fallback)
    validation_mode: Literal["ai", "heuristic"] = "heuristic"


class VulnerabilityResult(BaseModel):
    test_case_id: str
    endpoint: str                                        # "POST /users"
    severity: Literal["critical", "high", "medium", "low"]
    vuln_type: str                                       # "auth_bypass", "injection_error", etc.
    description: str
    evidence: dict[str, Any] = {}                        # request/response snapshot


class TestCaseResult(BaseModel):
    test_case: TestCase
    execution: ExecutionResult
    validation: ValidationResult


# ── Test Plan (Phase 1 output) ────────────────────────────────────────────────

class PlannedCase(BaseModel):
    description: str
    test_type: Optional[str] = None  # security_test_type if applicable


class PlannedEndpoint(BaseModel):
    method: str
    path: str
    planned_cases: list[PlannedCase]


class PlannedScenario(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str
    steps: list[str]   # ["POST /users", "GET /users/{id}", ...] — display only
    rationale: str
    scenario_type: str = "crud"   # "crud" | "business"
    domains: list[str] = []       # business scenarios only: e.g. ["issuance", "order"]


class TestPlan(BaseModel):
    individual_tests: list[PlannedEndpoint] = []
    crud_scenarios: list[PlannedScenario] = []      # single-domain CRUD / auth-flow scenarios
    business_scenarios: list[PlannedScenario] = []  # cross-domain business transaction scenarios


# ── Test Scenario (generated from PlannedScenario) ───────────────────────────

class TestScenario(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str
    steps: list[TestCase]  # ordered; each step has scenario_id + step_index set
