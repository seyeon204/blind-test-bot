"""Unit tests for app/core/tc_planner.py"""
import types
import pytest
from unittest.mock import AsyncMock, patch

from app.core.tc_planner import plan_test_cases
from app.models.internal import EndpointSpec, ParsedSpec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_spec(*endpoints) -> ParsedSpec:
    return ParsedSpec(
        source_format="openapi",
        endpoints=list(endpoints),
    )


def make_ep(method: str, path: str) -> EndpointSpec:
    return EndpointSpec(
        method=method,
        path=path,
        parameters=[],
        expected_responses={"200": {"description": "OK"}},
    )


def make_planner_response(individual_tests: list[dict], scenarios: list[dict]):
    """Returns a create_test_plan tool_use response (individual batches + CRUD scenarios)."""
    tool_use = types.SimpleNamespace(
        type="tool_use",
        input={"individual_tests": individual_tests, "scenarios": scenarios},
    )
    return types.SimpleNamespace(content=[tool_use])


def make_domain_response(domains: list[dict]):
    """Returns an analyze_domains tool_use response."""
    tool_use = types.SimpleNamespace(
        type="tool_use",
        input={"domains": domains},
    )
    return types.SimpleNamespace(content=[tool_use])


def make_business_scenarios_response(scenarios: list[dict]):
    """Returns a create_business_scenarios tool_use response."""
    tool_use = types.SimpleNamespace(
        type="tool_use",
        input={"scenarios": scenarios},
    )
    return types.SimpleNamespace(content=[tool_use])


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_plan_returns_individual_tests():
    spec = make_spec(make_ep("GET", "/items"), make_ep("POST", "/items"))
    response = make_planner_response(
        individual_tests=[
            {"method": "GET", "path": "/items", "planned_cases": [{"description": "Happy path"}]},
            {"method": "POST", "path": "/items", "planned_cases": [{"description": "Create item"}]},
        ],
        scenarios=[],
    )
    with patch("app.core.tc_planner.chat_with_tools", new=AsyncMock(return_value=response)):
        plan = await plan_test_cases(spec)

    assert len(plan.individual_tests) == 2
    assert plan.individual_tests[0].path == "/items"


@pytest.mark.asyncio
async def test_plan_method_uppercased():
    spec = make_spec(make_ep("get", "/items"))
    response = make_planner_response(
        individual_tests=[
            {"method": "get", "path": "/items", "planned_cases": [{"description": "test"}]},
        ],
        scenarios=[],
    )
    with patch("app.core.tc_planner.chat_with_tools", new=AsyncMock(return_value=response)):
        plan = await plan_test_cases(spec)

    assert plan.individual_tests[0].method == "GET"


@pytest.mark.asyncio
async def test_plan_returns_scenarios():
    spec = make_spec(make_ep("POST", "/users"), make_ep("GET", "/users/{id}"))
    response = make_planner_response(
        individual_tests=[
            {"method": "POST", "path": "/users", "planned_cases": []},
            {"method": "GET", "path": "/users/{id}", "planned_cases": []},
        ],
        scenarios=[
            {
                "name": "Create and fetch user",
                "description": "Create user then fetch by ID",
                "steps": ["POST /users", "GET /users/{id}"],
                "rationale": "Verify create-then-read flow",
            }
        ],
    )
    with patch("app.core.tc_planner.chat_with_tools", new=AsyncMock(return_value=response)):
        plan = await plan_test_cases(spec)

    assert len(plan.scenarios) == 1
    assert plan.scenarios[0].name == "Create and fetch user"
    assert plan.scenarios[0].steps == ["POST /users", "GET /users/{id}"]


@pytest.mark.asyncio
async def test_scenario_auto_generated_id():
    """Each PlannedScenario gets a UUID id (auto-generated, not from Claude)."""
    spec = make_spec(make_ep("POST", "/users"))
    response = make_planner_response(
        individual_tests=[{"method": "POST", "path": "/users", "planned_cases": []}],
        scenarios=[
            {
                "name": "Scenario",
                "description": "desc",
                "steps": ["POST /users"],
                "rationale": "reason",
            }
        ],
    )
    with patch("app.core.tc_planner.chat_with_tools", new=AsyncMock(return_value=response)):
        plan = await plan_test_cases(spec)

    assert plan.scenarios[0].id  # non-empty UUID string


@pytest.mark.asyncio
async def test_plan_empty_scenarios():
    spec = make_spec(make_ep("GET", "/health"))
    response = make_planner_response(
        individual_tests=[{"method": "GET", "path": "/health", "planned_cases": [{"description": "ping"}]}],
        scenarios=[],
    )
    with patch("app.core.tc_planner.chat_with_tools", new=AsyncMock(return_value=response)):
        plan = await plan_test_cases(spec)

    assert plan.scenarios == []


@pytest.mark.asyncio
async def test_planned_cases_include_security_test_type():
    spec = make_spec(make_ep("GET", "/items"))
    response = make_planner_response(
        individual_tests=[
            {
                "method": "GET",
                "path": "/items",
                "planned_cases": [
                    {"description": "SQL injection in search", "test_type": "sql_injection"},
                    {"description": "Happy path"},
                ],
            }
        ],
        scenarios=[],
    )
    with patch("app.core.tc_planner.chat_with_tools", new=AsyncMock(return_value=response)):
        plan = await plan_test_cases(spec)

    cases = plan.individual_tests[0].planned_cases
    assert cases[0].test_type == "sql_injection"
    assert cases[1].test_type is None


# ---------------------------------------------------------------------------
# Error / degradation paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claude_exception_returns_empty_plan():
    """All Claude calls failing → graceful degradation to empty plan, no exception raised."""
    spec = make_spec(make_ep("GET", "/items"))
    with patch("app.core.tc_planner.chat_with_tools", new=AsyncMock(side_effect=RuntimeError("API down"))):
        plan = await plan_test_cases(spec)
    assert plan.individual_tests == []
    assert plan.scenarios == []


@pytest.mark.asyncio
async def test_no_tool_use_block_returns_empty_plan():
    """Claude returning text instead of tool_use → graceful degradation to empty plan."""
    spec = make_spec(make_ep("GET", "/items"))
    text_response = types.SimpleNamespace(content=[
        types.SimpleNamespace(type="text", text="Sorry, I cannot help.")
    ])
    with patch("app.core.tc_planner.chat_with_tools", new=AsyncMock(return_value=text_response)):
        plan = await plan_test_cases(spec)
    assert plan.individual_tests == []
    assert plan.scenarios == []


# ---------------------------------------------------------------------------
# Scenario type tagging
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crud_scenarios_tagged():
    """Scenarios from the CRUD call are tagged scenario_type='crud'."""
    spec = make_spec(make_ep("POST", "/users"), make_ep("GET", "/users/{id}"))
    crud_response = make_planner_response(
        individual_tests=[
            {"method": "POST", "path": "/users", "planned_cases": []},
            {"method": "GET", "path": "/users/{id}", "planned_cases": []},
        ],
        scenarios=[
            {
                "name": "Create and fetch",
                "description": "CRUD flow",
                "steps": ["POST /users", "GET /users/{id}"],
                "rationale": "basic CRUD",
            }
        ],
    )
    with patch("app.core.tc_planner.chat_with_tools", new=AsyncMock(return_value=crud_response)):
        plan = await plan_test_cases(spec)

    crud = [s for s in plan.scenarios if s.scenario_type == "crud"]
    assert len(crud) == 1
    assert crud[0].name == "Create and fetch"


@pytest.mark.asyncio
async def test_business_scenarios_tagged_with_domains():
    """Business scenarios from the domain analysis path are tagged correctly."""
    spec = make_spec(make_ep("POST", "/orders"), make_ep("POST", "/settlements"))

    # Sequence: individual_tests batch → CRUD scenarios → domain analysis → business scenarios
    call_count = [0]

    def make_side_effect():
        async def side_effect(*args, **kwargs):
            call_count[0] += 1
            n = call_count[0]
            if n == 1:
                # individual batch
                return make_planner_response(
                    [{"method": "POST", "path": "/orders", "planned_cases": []}],
                    [],
                )
            elif n == 2:
                # CRUD scenarios
                return make_planner_response([], [])
            elif n == 3:
                # domain analysis
                return make_domain_response([
                    {"name": "order", "description": "Order management", "endpoint_paths": ["/orders"]},
                    {"name": "settlement", "description": "Settlement", "endpoint_paths": ["/settlements"]},
                ])
            else:
                # business scenarios
                return make_business_scenarios_response([
                    {
                        "name": "Order to settlement",
                        "description": "Place order then settle",
                        "domains": ["order", "settlement"],
                        "steps": ["POST /orders", "POST /settlements"],
                        "rationale": "End-to-end trade flow",
                    }
                ])
        return side_effect

    with patch("app.core.tc_planner.chat_with_tools", new=AsyncMock(side_effect=make_side_effect())):
        plan = await plan_test_cases(spec)

    business = [s for s in plan.scenarios if s.scenario_type == "business"]
    assert len(business) == 1
    assert business[0].name == "Order to settlement"
    assert business[0].domains == ["order", "settlement"]


@pytest.mark.asyncio
async def test_scenario_type_default_is_crud():
    """PlannedScenario defaults to scenario_type='crud'."""
    from app.models.internal import PlannedScenario
    s = PlannedScenario(name="x", description="y", steps=[], rationale="z")
    assert s.scenario_type == "crud"
    assert s.domains == []
