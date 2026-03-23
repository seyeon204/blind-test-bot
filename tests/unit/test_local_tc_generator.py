"""Unit tests for app/core/local_tc_generator.py"""
import pytest

from app.core.local_tc_generator import (
    _SQL_PAYLOADS,
    _XSS_PAYLOADS,
    _build_body,
    _example_value,
    _path_params,
    _query_params,
    generate_local,
)
from app.models.internal import EndpointSpec, ParsedSpec
from app.models.request import TestStrategy


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def make_param(name: str, location: str = "query", required: bool = False, schema: dict | None = None):
    from app.models.internal import ParameterSpec
    return ParameterSpec(name=name, location=location, required=required, schema=schema or {})


def make_ep(
    method: str = "GET",
    path: str = "/items",
    *,
    parameters=None,
    request_body_schema=None,
    expected_responses=None,
    security_schemes=None,
):
    return EndpointSpec(
        method=method,
        path=path,
        parameters=parameters or [],
        request_body_schema=request_body_schema,
        expected_responses=expected_responses or {"200": {"description": "OK"}},
        security_schemes=security_schemes or [],
    )


def make_spec(*endpoints) -> ParsedSpec:
    return ParsedSpec(source_format="openapi", endpoints=list(endpoints))


# ---------------------------------------------------------------------------
# _example_value
# ---------------------------------------------------------------------------

def test_example_value_integer():
    assert isinstance(_example_value({"type": "integer"}), int)


def test_example_value_boolean():
    assert _example_value({"type": "boolean"}) is True


def test_example_value_email_format():
    assert "@" in _example_value({"type": "string", "format": "email"})


def test_example_value_email_name_hint():
    assert "@" in _example_value({"type": "string"}, name="email")


def test_example_value_date_format():
    val = _example_value({"type": "string", "format": "date"})
    assert "-" in val  # e.g. "2024-01-01"


def test_example_value_datetime_format():
    val = _example_value({"type": "string", "format": "date-time"})
    assert "T" in val


def test_example_value_uuid_format():
    import uuid
    val = _example_value({"type": "string", "format": "uuid"})
    uuid.UUID(val)  # should not raise


def test_example_value_id_name_hint():
    import uuid
    val = _example_value({"type": "string"}, name="userId")
    uuid.UUID(val)  # id hint → uuid


def test_example_value_password_hint():
    val = _example_value({"type": "string"}, name="password")
    assert len(val) >= 6


def test_example_value_url_hint():
    val = _example_value({"type": "string"}, name="url")
    assert val.startswith("http")


def test_example_value_name_hint():
    val = _example_value({"type": "string"}, name="username")
    assert isinstance(val, str) and len(val) > 0


def test_example_value_array():
    val = _example_value({"type": "array", "items": {"type": "integer"}})
    assert isinstance(val, list) and len(val) == 1


def test_example_value_object():
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
    val = _example_value(schema)
    assert isinstance(val, dict) and "x" in val


def test_example_value_schema_example_used():
    val = _example_value({"type": "integer", "example": 42})
    assert val == 42


# ---------------------------------------------------------------------------
# _build_body
# ---------------------------------------------------------------------------

def test_build_body_all_fields():
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    }
    body = _build_body(schema)
    assert "name" in body and "age" in body


def test_build_body_required_only():
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "bio": {"type": "string"}},
        "required": ["name"],
    }
    body = _build_body(schema, use_required_only=True)
    assert "name" in body
    assert "bio" not in body


def test_build_body_non_object_returns_empty():
    assert _build_body({"type": "string"}) == {}
    assert _build_body({}) == {}


# ---------------------------------------------------------------------------
# _path_params / _query_params
# ---------------------------------------------------------------------------

def test_path_params_extracted_from_template():
    params = [make_param("id", location="path", required=True, schema={"type": "integer"})]
    result = _path_params("/items/{id}", params)
    assert "id" in result
    assert isinstance(result["id"], int)


def test_path_params_no_placeholders():
    result = _path_params("/items", [])
    assert result == {}


def test_query_params_required_only():
    params = [
        make_param("limit", location="query", required=False),
        make_param("q", location="query", required=True, schema={"type": "string"}),
    ]
    result = _query_params(params)
    assert "q" in result
    assert "limit" not in result


# ---------------------------------------------------------------------------
# TC builders via generate_local (simplest entry point)
# ---------------------------------------------------------------------------

def _run(ep, strategy=TestStrategy.exhaustive, auth=None):
    """Generate TCs for a single endpoint."""
    return generate_local(make_spec(ep), strategy=strategy, auth_headers=auth or {})


def test_happy_path_always_generated():
    ep = make_ep("GET", "/items")
    tcs = _run(ep)
    assert any("Happy path" in tc.description for tc in tcs)


def test_auth_bypass_generated_when_security_schemes():
    ep = make_ep("GET", "/items", security_schemes=["bearerAuth"])
    tcs = _run(ep)
    assert any(tc.security_test_type == "auth_bypass" for tc in tcs)


def test_auth_bypass_not_generated_without_security():
    ep = make_ep("GET", "/items", security_schemes=[])
    tcs = _run(ep)
    assert not any(tc.security_test_type == "auth_bypass" for tc in tcs)


def test_not_found_generated_for_path_param_endpoints():
    ep = make_ep("GET", "/items/{id}", parameters=[
        make_param("id", location="path", required=True, schema={"type": "integer"}),
    ])
    tcs = _run(ep)
    not_found = [tc for tc in tcs if "404" in tc.description or "Non-existent" in tc.description]
    assert len(not_found) > 0


def test_not_found_not_generated_without_path_params():
    ep = make_ep("GET", "/items")
    tcs = _run(ep)
    assert not any("Non-existent" in tc.description for tc in tcs)


def test_not_found_uses_unlikely_values():
    ep = make_ep("GET", "/items/{id}", parameters=[
        make_param("id", location="path", required=True, schema={"type": "integer"}),
    ])
    tcs = _run(ep)
    not_found = next(tc for tc in tcs if "Non-existent" in tc.description)
    assert not_found.path_params["id"] == 99999


def test_missing_required_field_generated_for_post():
    ep = make_ep("POST", "/items", request_body_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}, "price": {"type": "number"}},
        "required": ["name"],
    })
    tcs = _run(ep)
    assert any("Missing required field" in tc.description for tc in tcs)


def test_missing_required_field_not_generated_for_get():
    ep = make_ep("GET", "/items", request_body_schema={
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    })
    tcs = _run(ep)
    assert not any("Missing required field" in tc.description for tc in tcs)


def test_missing_required_field_not_generated_without_required():
    ep = make_ep("POST", "/items", request_body_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}},
    })
    tcs = _run(ep)
    assert not any("Missing required field" in tc.description for tc in tcs)


def test_wrong_type_generated_when_integer_field_exists():
    ep = make_ep("POST", "/items", request_body_schema={
        "type": "object",
        "properties": {"price": {"type": "integer"}},
    })
    tcs = _run(ep)
    wrong = [tc for tc in tcs if "Wrong type" in tc.description]
    assert len(wrong) > 0
    assert wrong[0].body["price"] == "not_a_number"


def test_wrong_type_not_generated_for_delete():
    ep = make_ep("DELETE", "/items/{id}", request_body_schema={
        "type": "object",
        "properties": {"count": {"type": "integer"}},
    })
    tcs = _run(ep)
    assert not any("Wrong type" in tc.description for tc in tcs)


def test_sql_injection_generated_for_string_field():
    ep = make_ep("POST", "/search", request_body_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
    })
    tcs = _run(ep)
    sqli = next(tc for tc in tcs if tc.security_test_type == "sql_injection")
    assert sqli.body["query"] in _SQL_PAYLOADS


def test_sql_injection_not_generated_without_string_field():
    ep = make_ep("POST", "/items", request_body_schema={
        "type": "object",
        "properties": {"count": {"type": "integer"}},
    })
    tcs = _run(ep)
    assert not any(tc.security_test_type == "sql_injection" for tc in tcs)


def test_xss_generated_for_string_field():
    ep = make_ep("POST", "/comments", request_body_schema={
        "type": "object",
        "properties": {"content": {"type": "string"}},
    })
    tcs = _run(ep)
    xss = next(tc for tc in tcs if tc.security_test_type == "xss")
    assert xss.body["content"] in _XSS_PAYLOADS


# ---------------------------------------------------------------------------
# Strategy limits
# ---------------------------------------------------------------------------

def test_minimal_strategy_max_2():
    ep = make_ep("POST", "/items",
        security_schemes=["bearerAuth"],
        request_body_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
            "required": ["name"],
        },
    )
    tcs = _run(ep, strategy=TestStrategy.minimal)
    assert len(tcs) <= 2


def test_standard_strategy_max_4():
    ep = make_ep("POST", "/items/{id}",
        parameters=[make_param("id", location="path", required=True, schema={"type": "integer"})],
        security_schemes=["bearerAuth"],
        request_body_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
            "required": ["name"],
        },
    )
    tcs = _run(ep, strategy=TestStrategy.standard)
    assert len(tcs) <= 4


def test_exhaustive_strategy_max_12():
    ep = make_ep("POST", "/items/{id}",
        parameters=[make_param("id", location="path", required=True, schema={"type": "integer"})],
        security_schemes=["bearerAuth"],
        request_body_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
            "required": ["name"],
        },
    )
    tcs = _run(ep, strategy=TestStrategy.exhaustive)
    assert len(tcs) <= 12


def test_max_tc_per_endpoint_overrides_strategy():
    ep = make_ep("POST", "/items",
        security_schemes=["bearerAuth"],
        request_body_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    )
    tcs = generate_local(make_spec(ep), strategy=TestStrategy.exhaustive, max_tc_per_endpoint=1)
    assert len(tcs) == 1


# ---------------------------------------------------------------------------
# generate_local — multi-endpoint + callback
# ---------------------------------------------------------------------------

def test_generate_local_multiple_endpoints():
    spec = make_spec(
        make_ep("GET", "/items"),
        make_ep("GET", "/users"),
    )
    tcs = generate_local(spec)
    assert len(tcs) >= 2


def test_on_endpoint_done_called_once_per_endpoint():
    spec = make_spec(
        make_ep("GET", "/a"),
        make_ep("GET", "/b"),
        make_ep("GET", "/c"),
    )
    calls = []
    generate_local(spec, on_endpoint_done=lambda done, total, method, path, cases: calls.append((done, total, method, path)))
    assert len(calls) == 3
    assert calls[-1][0] == 3
    assert calls[-1][1] == 3
    assert calls[-1][2] == "GET"
    assert calls[-1][3] == "/c"


def test_auth_headers_injected_into_happy_path():
    ep = make_ep("GET", "/items")
    tcs = generate_local(make_spec(ep), auth_headers={"Authorization": "Bearer tok"})
    happy = next(tc for tc in tcs if "Happy path" in tc.description)
    assert happy.headers.get("Authorization") == "Bearer tok"


def test_auth_bypass_has_empty_headers():
    ep = make_ep("GET", "/items", security_schemes=["bearerAuth"])
    tcs = generate_local(make_spec(ep), auth_headers={"Authorization": "Bearer tok"},
                         strategy=TestStrategy.exhaustive)
    bypass = next(tc for tc in tcs if tc.security_test_type == "auth_bypass")
    assert bypass.headers == {}
