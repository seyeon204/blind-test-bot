"""Unit tests for generate_scenario_test_cases in app/core/tc_generator.py"""
import types
import pytest
from unittest.mock import AsyncMock, patch

from app.core.tc_generator import generate_scenario_test_cases
from app.models.internal import EndpointSpec, ParsedSpec, PlannedScenario
from app.utils.exceptions import TCGenerationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_spec() -> ParsedSpec:
    return ParsedSpec(
        source_format="openapi",
        endpoints=[
            EndpointSpec(method="POST", path="/users", parameters=[], expected_responses={"201": {"description": "Created"}}),
            EndpointSpec(method="GET", path="/users/{id}", parameters=[], expected_responses={"200": {"description": "OK"}}),
        ],
    )


def make_planned(
    scenario_id: str = "sc1",
    name: str = "Create and fetch",
    description: str = "Create user then fetch",
) -> PlannedScenario:
    return PlannedScenario(
        id=scenario_id,
        name=name,
        description=description,
        steps=["POST /users", "GET /users/{id}"],
        rationale="Covers the create-then-read flow",
    )


def make_claude_response(scenarios: list[dict]):
    tool_use = types.SimpleNamespace(
        type="tool_use",
        input={"scenarios": scenarios},
    )
    return types.SimpleNamespace(content=[tool_use])


def _default_step_raw(method="POST", path="/users", desc="step", codes=None, extract=None):
    return {
        "endpoint_method": method,
        "endpoint_path": path,
        "description": desc,
        "expected_status_codes": codes or [200],
        "path_params": {},
        "query_params": {},
        "headers": {},
        "extract": extract or {},
    }


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_planned_scenarios_returns_empty():
    result = await generate_scenario_test_cases([], make_spec())
    assert result == []


# ---------------------------------------------------------------------------
# Correct structure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_returns_test_scenario_with_correct_id():
    planned = make_planned(scenario_id="sc1")
    response = make_claude_response([{
        "scenario_id": "sc1",
        "name": "Create and fetch",
        "description": "desc",
        "steps": [_default_step_raw()],
    }])
    with patch("app.core.tc_generator.chat_with_tools", new=AsyncMock(return_value=response)):
        result = await generate_scenario_test_cases([planned], make_spec())

    assert len(result) == 1
    assert result[0].id == "sc1"
    assert result[0].name == "Create and fetch"


@pytest.mark.asyncio
async def test_step_index_set_sequentially():
    planned = make_planned(scenario_id="sc1")
    response = make_claude_response([{
        "scenario_id": "sc1",
        "name": "n",
        "description": "d",
        "steps": [
            _default_step_raw("POST", "/users", "create"),
            _default_step_raw("GET", "/users/{id}", "fetch"),
        ],
    }])
    with patch("app.core.tc_generator.chat_with_tools", new=AsyncMock(return_value=response)):
        result = await generate_scenario_test_cases([planned], make_spec())

    steps = result[0].steps
    assert steps[0].step_index == 0
    assert steps[1].step_index == 1


@pytest.mark.asyncio
async def test_scenario_id_set_on_each_step():
    planned = make_planned(scenario_id="sc-xyz")
    response = make_claude_response([{
        "scenario_id": "sc-xyz",
        "name": "n",
        "description": "d",
        "steps": [_default_step_raw(), _default_step_raw()],
    }])
    with patch("app.core.tc_generator.chat_with_tools", new=AsyncMock(return_value=response)):
        result = await generate_scenario_test_cases([planned], make_spec())

    for step in result[0].steps:
        assert step.scenario_id == "sc-xyz"


@pytest.mark.asyncio
async def test_auth_headers_merged_into_step_headers():
    planned = make_planned(scenario_id="sc1")
    step_raw = _default_step_raw()
    step_raw["headers"] = {"X-Custom": "val"}
    response = make_claude_response([{
        "scenario_id": "sc1",
        "name": "n",
        "description": "d",
        "steps": [step_raw],
    }])
    with patch("app.core.tc_generator.chat_with_tools", new=AsyncMock(return_value=response)):
        result = await generate_scenario_test_cases(
            [planned], make_spec(), auth_headers={"Authorization": "Bearer tok"}
        )

    headers = result[0].steps[0].headers
    assert headers["Authorization"] == "Bearer tok"
    assert headers["X-Custom"] == "val"


@pytest.mark.asyncio
async def test_extract_field_preserved():
    planned = make_planned(scenario_id="sc1")
    step_raw = _default_step_raw(extract={"userId": "data.id"})
    response = make_claude_response([{
        "scenario_id": "sc1",
        "name": "n",
        "description": "d",
        "steps": [step_raw],
    }])
    with patch("app.core.tc_generator.chat_with_tools", new=AsyncMock(return_value=response)):
        result = await generate_scenario_test_cases([planned], make_spec())

    assert result[0].steps[0].extract == {"userId": "data.id"}


@pytest.mark.asyncio
async def test_step_method_uppercased():
    planned = make_planned(scenario_id="sc1")
    step_raw = _default_step_raw(method="post", path="/users")
    response = make_claude_response([{
        "scenario_id": "sc1",
        "name": "n",
        "description": "d",
        "steps": [step_raw],
    }])
    with patch("app.core.tc_generator.chat_with_tools", new=AsyncMock(return_value=response)):
        result = await generate_scenario_test_cases([planned], make_spec())

    assert result[0].steps[0].endpoint_method == "POST"


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_scenario_id_in_response_skipped():
    planned = make_planned(scenario_id="sc1")
    # Claude responds with a wrong scenario_id
    response = make_claude_response([{
        "scenario_id": "sc-WRONG",
        "name": "n",
        "description": "d",
        "steps": [_default_step_raw()],
    }])
    with patch("app.core.tc_generator.chat_with_tools", new=AsyncMock(return_value=response)):
        result = await generate_scenario_test_cases([planned], make_spec())

    assert result == []


@pytest.mark.asyncio
async def test_multiple_scenarios_all_returned():
    planned1 = make_planned(scenario_id="sc1", name="Scenario One")
    planned2 = make_planned(scenario_id="sc2", name="Scenario Two")
    response = make_claude_response([
        {"scenario_id": "sc1", "name": "Scenario One", "description": "d", "steps": [_default_step_raw()]},
        {"scenario_id": "sc2", "name": "Scenario Two", "description": "d", "steps": [_default_step_raw()]},
    ])
    with patch("app.core.tc_generator.chat_with_tools", new=AsyncMock(return_value=response)):
        result = await generate_scenario_test_cases([planned1, planned2], make_spec())

    assert len(result) == 2
    names = {s.name for s in result}
    assert names == {"Scenario One", "Scenario Two"}


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_raises_on_claude_exception():
    planned = make_planned()
    with patch("app.core.tc_generator.chat_with_tools", new=AsyncMock(side_effect=RuntimeError("API down"))):
        with pytest.raises(TCGenerationError, match="scenario generation"):
            await generate_scenario_test_cases([planned], make_spec())


@pytest.mark.asyncio
async def test_raises_when_no_tool_use_block():
    planned = make_planned()
    text_response = types.SimpleNamespace(content=[
        types.SimpleNamespace(type="text", text="I cannot help.")
    ])
    with patch("app.core.tc_generator.chat_with_tools", new=AsyncMock(return_value=text_response)):
        with pytest.raises(TCGenerationError, match="No tool_use"):
            await generate_scenario_test_cases([planned], make_spec())
