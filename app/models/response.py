from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional
from pydantic import BaseModel


class EndpointSummary(BaseModel):
    method: str
    path: str
    summary: str = ""


class TestRunSummary(BaseModel):
    total: int
    passed: int
    failed: int
    skipped: int
    scenario_total: int = 0
    scenario_passed: int = 0
    scenario_failed: int = 0
    avg_latency_ms: float = 0.0


class Vulnerability(BaseModel):
    test_case_id: str
    endpoint: str
    severity: Literal["critical", "high", "medium", "low"]
    type: str
    description: str
    evidence: dict[str, Any] = {}


class ExpectedResponse(BaseModel):
    status_codes: list[int]
    body_schema: Optional[dict[str, Any]] = None   # JSON Schema
    body_contains: dict[str, Any] = {}             # shallow key-value assertions


class GeneratedTestCase(BaseModel):
    id: str
    endpoint: str
    description: str
    path_params: dict[str, Any] = {}
    query_params: dict[str, Any] = {}
    headers: dict[str, Any] = {}
    body: Any = None
    security_test_type: Optional[str] = None
    expected_response: ExpectedResponse


class TestCaseResultResponse(BaseModel):
    test_case_id: str
    endpoint: str
    description: str
    passed: bool
    request: dict[str, Any]
    response: dict[str, Any]
    failures: list[str]
    reasoning: Optional[str] = None  # AI chain-of-thought explanation; None when heuristic fallback was used
    validation_mode: str = "heuristic"


RunStatus = Literal[
    "parsing", "parsed",
    "analyzing",                          # Phase 1: Claude reads full spec, builds plan
    "generating", "generated",
    "executing", "running", "completed",
    "failed", "cancelled",
]


class PlannedCaseResponse(BaseModel):
    description: str
    test_type: Optional[str] = None


class PlannedEndpointResponse(BaseModel):
    method: str
    path: str
    planned_count: int
    security_count: int
    planned_cases: list[PlannedCaseResponse]


class PlannedScenarioResponse(BaseModel):
    id: str
    name: str
    description: str
    steps: list[str]
    rationale: str
    scenario_type: str        # "crud" | "business"
    domains: list[str] = []   # business scenarios only


class TestPlanResponse(BaseModel):
    total_endpoints: int
    total_planned_cases: int
    total_scenarios: int
    crud_scenario_count: int
    business_scenario_count: int
    individual_tests: list[PlannedEndpointResponse]
    scenarios: list[PlannedScenarioResponse]


class ScenarioStepResult(BaseModel):
    step_index: int
    test_case_id: str
    endpoint: str
    description: str
    passed: bool
    request: dict[str, Any]
    response: dict[str, Any]
    failures: list[str]
    extracted_values: dict[str, Any] = {}


class ScenarioResultResponse(BaseModel):
    scenario_id: str
    name: str
    description: str
    passed: bool  # True only if all steps passed
    steps: list[ScenarioStepResult]


class CostEstimateResponse(BaseModel):
    endpoint_count: int
    estimated_tc_count: int
    estimated_tokens: int
    estimated_cost_usd: float
    note: str


class TestRunStatusResponse(BaseModel):
    run_id: str
    status: RunStatus
    created_at: datetime
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    # Step 1: parse
    source_format: Optional[str] = None
    base_url: Optional[str] = None
    endpoints: list[EndpointSummary] = []
    # Step 2: generate
    test_case_count: int = 0
    scenario_count: int = 0
    skipped_endpoints: list[str] = []
    # Step 3: execute
    summary: Optional[TestRunSummary] = None
    results: list[TestCaseResultResponse] = []
    scenario_results: list[ScenarioResultResponse] = []
    vulnerabilities: list[Vulnerability] = []
    estimated_cost_usd: float = 0.0
