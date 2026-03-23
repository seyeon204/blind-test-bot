"""Rule-based TC generator — no Claude API call, free and deterministic."""
from __future__ import annotations

import random
import re
import uuid
from typing import Any

from app.models.internal import EndpointSpec, ParsedSpec, TestCase
from app.models.request import TestStrategy

_STRATEGY_TC_COUNT = {
    TestStrategy.minimal: 2,
    TestStrategy.standard: 4,
    TestStrategy.exhaustive: 12,
}

# Security TC quota guaranteed per strategy (counts toward total limit)
_SECURITY_QUOTA = {
    TestStrategy.minimal: 0,
    TestStrategy.standard: 1,
    TestStrategy.exhaustive: 5,
}

# Payload pools — rotated per call so single-pattern WAF bypasses are caught
_SQL_PAYLOADS = [
    "' OR '1'='1",
    "'; DROP TABLE users; --",
    "1 UNION SELECT null,null,null--",
]
_XSS_PAYLOADS = [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "javascript:alert(1)",
]


# ── value helpers ─────────────────────────────────────────────────────────────

def _example_value(schema: dict, name: str = "") -> Any:
    """Generate a plausible example value from a JSON Schema fragment."""
    fmt = schema.get("format", "")
    typ = schema.get("type", "string")

    if typ == "integer" or typ == "number":
        return schema.get("example", schema.get("minimum", 1))
    if typ == "boolean":
        return True
    if typ == "array":
        item_schema = schema.get("items", {})
        return [_example_value(item_schema)]
    if typ == "object":
        return _build_body(schema, use_required_only=True)

    # string — check format/name hints
    if fmt == "email" or "email" in name:
        return "user@example.com"
    if fmt in ("uuid", "guid") or "id" in name.lower():
        return str(uuid.uuid4())
    if fmt == "date":
        return "2024-01-01"
    if fmt == "date-time":
        return "2024-01-01T00:00:00Z"
    if "password" in name.lower():
        return "P@ssword1!"
    if "url" in name.lower() or "uri" in name.lower():
        return "https://example.com"
    if "name" in name.lower():
        return "testuser"
    return schema.get("example", f"test_{name}" if name else "test_value")


def _build_body(schema: dict, use_required_only: bool = False) -> dict[str, Any]:
    """Build a request body dict from a JSON Schema object."""
    if not schema or schema.get("type") != "object":
        return {}
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    result: dict[str, Any] = {}
    for field, field_schema in props.items():
        if use_required_only and field not in required:
            continue
        result[field] = _example_value(field_schema, name=field)
    return result


def _path_params(path: str, params: list) -> dict[str, Any]:
    """Build path param values from the endpoint spec."""
    path_param_specs = {p.name: p for p in params if p.location == "path"}
    result: dict[str, Any] = {}
    for name in re.findall(r"\{(\w+)\}", path):
        spec = path_param_specs.get(name)
        schema = spec.schema_ if spec else {}
        result[name] = _example_value(schema, name=name)
    return result


def _query_params(params: list) -> dict[str, Any]:
    return {
        p.name: _example_value(p.schema_, name=p.name)
        for p in params
        if p.location == "query" and p.required
    }


# ── TC builders ───────────────────────────────────────────────────────────────

def _success_schema(ep: EndpointSpec) -> dict | None:
    """Extract the JSON Schema for the first 2xx response defined in the spec."""
    for code in sorted(ep.expected_responses):
        if code.startswith("2"):
            schema = ep.expected_responses[code].get("schema")
            if schema:
                return schema
    return None


def _response_body_assertions(ep: EndpointSpec) -> dict[str, Any]:
    """Build expected_body_contains from required fields in the 2xx response schema.

    For each required field in the success response schema, add {"field": "<any>"}
    so the heuristic validator checks that the response object has the right shape.
    Capped at 5 fields to avoid noise.
    """
    for code in sorted(ep.expected_responses):
        if code.startswith("2"):
            schema = ep.expected_responses[code].get("schema", {})
            if schema.get("type") == "object":
                required = schema.get("required", [])
                if required:
                    return {field: "<any>" for field in required[:5]}
    return {}


def _happy_path(ep: EndpointSpec, auth_headers: dict) -> TestCase:
    body = _build_body(ep.request_body_schema or {}) or None
    expected_codes = [int(c) for c in ep.expected_responses if c.startswith("2")] or [200]
    return TestCase(
        endpoint_method=ep.method,
        endpoint_path=ep.path,
        description="Happy path — valid request",
        path_params=_path_params(ep.path, ep.parameters),
        query_params=_query_params(ep.parameters),
        headers=dict(auth_headers),
        body=body,
        expected_status_codes=expected_codes,
        expected_body_schema=_success_schema(ep),
        expected_body_contains=_response_body_assertions(ep),
    )


def _auth_bypass(ep: EndpointSpec) -> TestCase | None:
    if not ep.security_schemes:
        return None
    return TestCase(
        endpoint_method=ep.method,
        endpoint_path=ep.path,
        description="Auth bypass — no credentials",
        path_params=_path_params(ep.path, ep.parameters),
        query_params=_query_params(ep.parameters),
        headers={},
        body=None,
        expected_status_codes=[401, 403],
        security_test_type="auth_bypass",
    )


def _not_found(ep: EndpointSpec, auth_headers: dict) -> TestCase | None:
    if not re.search(r"\{\w+\}", ep.path):
        return None
    # Replace all path params with an unlikely ID
    path_params = _path_params(ep.path, ep.parameters)
    for k, v in path_params.items():
        path_params[k] = 99999 if isinstance(v, int) else "nonexistent-id-00000"
    return TestCase(
        endpoint_method=ep.method,
        endpoint_path=ep.path,
        description="Non-existent resource — expect 404",
        path_params=path_params,
        query_params=_query_params(ep.parameters),
        headers=dict(auth_headers),
        body=None,
        expected_status_codes=[404],
    )


def _missing_required_field(ep: EndpointSpec, auth_headers: dict) -> TestCase | None:
    schema = ep.request_body_schema or {}
    required = schema.get("required", [])
    if not required or ep.method in ("GET", "DELETE"):
        return None
    body = _build_body(schema)
    # Remove the first required field
    body.pop(required[0], None)
    return TestCase(
        endpoint_method=ep.method,
        endpoint_path=ep.path,
        description=f"Missing required field '{required[0]}' — expect 4xx",
        path_params=_path_params(ep.path, ep.parameters),
        query_params=_query_params(ep.parameters),
        headers=dict(auth_headers),
        body=body or None,
        expected_status_codes=[400, 422],
    )


def _wrong_type(ep: EndpointSpec, auth_headers: dict) -> TestCase | None:
    schema = ep.request_body_schema or {}
    props = schema.get("properties", {})
    # Find first integer/number field to corrupt
    target = next(
        (name for name, s in props.items() if s.get("type") in ("integer", "number")),
        None,
    )
    if not target or ep.method in ("GET", "DELETE"):
        return None
    body = _build_body(schema)
    body[target] = "not_a_number"
    return TestCase(
        endpoint_method=ep.method,
        endpoint_path=ep.path,
        description=f"Wrong type for '{target}' (string instead of number) — expect 4xx",
        path_params=_path_params(ep.path, ep.parameters),
        query_params=_query_params(ep.parameters),
        headers=dict(auth_headers),
        body=body,
        expected_status_codes=[400, 422],
    )


def _sql_injection(ep: EndpointSpec, auth_headers: dict) -> TestCase | None:
    schema = ep.request_body_schema or {}
    props = schema.get("properties", {})
    target = next(
        (name for name, s in props.items() if s.get("type") == "string"),
        None,
    )
    if not target:
        return None
    body = _build_body(schema)
    body[target] = random.choice(_SQL_PAYLOADS)
    return TestCase(
        endpoint_method=ep.method,
        endpoint_path=ep.path,
        description=f"SQL injection in '{target}'",
        path_params=_path_params(ep.path, ep.parameters),
        query_params=_query_params(ep.parameters),
        headers=dict(auth_headers),
        body=body,
        expected_status_codes=[400, 422, 200],
        security_test_type="sql_injection",
    )


def _xss(ep: EndpointSpec, auth_headers: dict) -> TestCase | None:
    schema = ep.request_body_schema or {}
    props = schema.get("properties", {})
    target = next(
        (name for name, s in props.items() if s.get("type") == "string"),
        None,
    )
    if not target:
        return None
    body = _build_body(schema)
    body[target] = random.choice(_XSS_PAYLOADS)
    return TestCase(
        endpoint_method=ep.method,
        endpoint_path=ep.path,
        description=f"XSS payload in '{target}'",
        path_params=_path_params(ep.path, ep.parameters),
        query_params=_query_params(ep.parameters),
        headers=dict(auth_headers),
        body=body,
        expected_status_codes=[400, 422, 200],
        security_test_type="xss",
    )


def _boundary_values(ep: EndpointSpec, auth_headers: dict) -> list[TestCase]:
    """Boundary value tests derived from schema constraints.

    For string fields: uses maxLength+1 (or 10001 if unset), minLength-1 / empty string,
    and an invalid enum value when enum is defined.
    For numeric fields: uses minimum-1 (or -1 if unset), maximum+1, and zero.
    """
    if ep.method in ("GET", "DELETE"):
        return []
    schema = ep.request_body_schema or {}
    props = schema.get("properties", {})
    cases: list[TestCase] = []

    str_field = next((n for n, s in props.items() if s.get("type") == "string"), None)
    if str_field:
        field_schema = props[str_field]
        enum_values = field_schema.get("enum")
        max_length = field_schema.get("maxLength")
        min_length = field_schema.get("minLength")

        if enum_values:
            # Send a value not in the enum
            body = _build_body(schema)
            body[str_field] = "__invalid_enum_value__"
            cases.append(TestCase(
                endpoint_method=ep.method,
                endpoint_path=ep.path,
                description=f"Boundary: invalid enum value for '{str_field}' (not in {enum_values})",
                path_params=_path_params(ep.path, ep.parameters),
                query_params=_query_params(ep.parameters),
                headers=dict(auth_headers),
                body=body,
                expected_status_codes=[400, 422],
            ))
        else:
            # Empty / below minLength
            empty_val = "" if min_length is None else ("A" * max(0, min_length - 1))
            body = _build_body(schema)
            body[str_field] = empty_val
            cases.append(TestCase(
                endpoint_method=ep.method,
                endpoint_path=ep.path,
                description=f"Boundary: empty/too-short string for '{str_field}'",
                path_params=_path_params(ep.path, ep.parameters),
                query_params=_query_params(ep.parameters),
                headers=dict(auth_headers),
                body=body,
                expected_status_codes=[400, 422],
            ))

            # Over maxLength
            over_len = (max_length + 1) if max_length is not None else 10001
            body2 = _build_body(schema)
            body2[str_field] = "A" * over_len
            cases.append(TestCase(
                endpoint_method=ep.method,
                endpoint_path=ep.path,
                description=f"Boundary: too-long string for '{str_field}' ({over_len} chars)",
                path_params=_path_params(ep.path, ep.parameters),
                query_params=_query_params(ep.parameters),
                headers=dict(auth_headers),
                body=body2,
                expected_status_codes=[400, 422],
            ))

    int_field = next((n for n, s in props.items() if s.get("type") in ("integer", "number")), None)
    if int_field:
        field_schema = props[int_field]
        minimum = field_schema.get("minimum")
        maximum = field_schema.get("maximum")

        # Below minimum (or negative if no minimum defined)
        below_min = (minimum - 1) if minimum is not None else -1
        body3 = _build_body(schema)
        body3[int_field] = below_min
        cases.append(TestCase(
            endpoint_method=ep.method,
            endpoint_path=ep.path,
            description=f"Boundary: below-minimum ({below_min}) for '{int_field}'",
            path_params=_path_params(ep.path, ep.parameters),
            query_params=_query_params(ep.parameters),
            headers=dict(auth_headers),
            body=body3,
            expected_status_codes=[400, 422],
        ))

        if maximum is not None:
            # Above maximum
            body4 = _build_body(schema)
            body4[int_field] = maximum + 1
            cases.append(TestCase(
                endpoint_method=ep.method,
                endpoint_path=ep.path,
                description=f"Boundary: above-maximum ({maximum + 1}) for '{int_field}'",
                path_params=_path_params(ep.path, ep.parameters),
                query_params=_query_params(ep.parameters),
                headers=dict(auth_headers),
                body=body4,
                expected_status_codes=[400, 422],
            ))
        else:
            # Zero as a neutral boundary value
            body4 = _build_body(schema)
            body4[int_field] = 0
            cases.append(TestCase(
                endpoint_method=ep.method,
                endpoint_path=ep.path,
                description=f"Boundary: zero value for '{int_field}'",
                path_params=_path_params(ep.path, ep.parameters),
                query_params=_query_params(ep.parameters),
                headers=dict(auth_headers),
                body=body4,
                expected_status_codes=[400, 422, 200, 201],
            ))

    return cases


def _idor(ep: EndpointSpec, auth_headers: dict) -> TestCase | None:
    """IDOR — access a resource using an unlikely cross-user ID.

    Requires auth_headers to be non-empty: without authentication there is no
    "current user" context, and the test would be indistinguishable from auth_bypass.
    """
    if not auth_headers:
        return None
    id_patterns = re.compile(r"\{(id|userId|user_id|itemId|item_id|resourceId|resource_id)\}", re.IGNORECASE)
    if not id_patterns.search(ep.path):
        return None
    path_params = _path_params(ep.path, ep.parameters)
    for k in path_params:
        if re.search(r"id", k, re.IGNORECASE):
            path_params[k] = 99998 if isinstance(path_params[k], int) else "00000000-dead-beef-0000-000000000001"
    return TestCase(
        endpoint_method=ep.method,
        endpoint_path=ep.path,
        description="IDOR — access another user's resource",
        path_params=path_params,
        query_params=_query_params(ep.parameters),
        headers=dict(auth_headers),
        body=None,
        expected_status_codes=[403, 404],
        security_test_type="idor",
    )


def _error_disclosure(ep: EndpointSpec, auth_headers: dict) -> TestCase | None:
    """Error disclosure — send null body to provoke a verbose 500."""
    if ep.method in ("GET", "DELETE"):
        return None
    if not ep.request_body_schema:
        return None
    return TestCase(
        endpoint_method=ep.method,
        endpoint_path=ep.path,
        description="Error disclosure — null body to provoke verbose error",
        path_params=_path_params(ep.path, ep.parameters),
        query_params=_query_params(ep.parameters),
        headers=dict(auth_headers),
        body=None,
        expected_status_codes=[400, 422, 500],
        security_test_type="error_disclosure",
    )


def _path_traversal(ep: EndpointSpec, auth_headers: dict) -> TestCase | None:
    """Path traversal — inject ../../../etc/passwd into string params."""
    str_param = next(
        (p for p in ep.parameters if p.location in ("path", "query") and p.schema_.get("type") == "string"),
        None,
    )
    if not str_param:
        return None
    path_params = _path_params(ep.path, ep.parameters)
    query_params = _query_params(ep.parameters)
    if str_param.location == "path":
        path_params[str_param.name] = "../../../etc/passwd"
    else:
        query_params[str_param.name] = "../../../etc/passwd"
    return TestCase(
        endpoint_method=ep.method,
        endpoint_path=ep.path,
        description=f"Path traversal in '{str_param.name}'",
        path_params=path_params,
        query_params=query_params,
        headers=dict(auth_headers),
        body=None,
        expected_status_codes=[400, 403, 404],
        security_test_type="path_traversal",
    )


def _ssrf(ep: EndpointSpec, auth_headers: dict) -> TestCase | None:
    """SSRF — inject internal IP into url/target/redirect params."""
    _URL_KEYWORDS = {"url", "uri", "target", "redirect", "callback", "webhook"}
    ssrf_param = next(
        (p for p in ep.parameters if any(kw in p.name.lower() for kw in _URL_KEYWORDS)),
        None,
    )
    if not ssrf_param:
        # also check body fields
        schema = ep.request_body_schema or {}
        props = schema.get("properties", {})
        ssrf_field = next((n for n in props if any(kw in n.lower() for kw in _URL_KEYWORDS)), None)
        if not ssrf_field:
            return None
        body = _build_body(schema)
        body[ssrf_field] = "http://169.254.169.254/latest/meta-data/"
        return TestCase(
            endpoint_method=ep.method,
            endpoint_path=ep.path,
            description=f"SSRF payload in body field '{ssrf_field}'",
            path_params=_path_params(ep.path, ep.parameters),
            query_params=_query_params(ep.parameters),
            headers=dict(auth_headers),
            body=body,
            expected_status_codes=[400, 403],
            security_test_type="ssrf",
        )

    path_params = _path_params(ep.path, ep.parameters)
    query_params = _query_params(ep.parameters)
    if ssrf_param.location == "path":
        path_params[ssrf_param.name] = "http://169.254.169.254/latest/meta-data/"
    else:
        query_params[ssrf_param.name] = "http://169.254.169.254/latest/meta-data/"
    return TestCase(
        endpoint_method=ep.method,
        endpoint_path=ep.path,
        description=f"SSRF payload in '{ssrf_param.name}'",
        path_params=path_params,
        query_params=query_params,
        headers=dict(auth_headers),
        body=None,
        expected_status_codes=[400, 403],
        security_test_type="ssrf",
    )


def _mass_assignment(ep: EndpointSpec, auth_headers: dict) -> TestCase | None:
    """Mass assignment — inject privilege escalation fields into the body."""
    if ep.method in ("GET", "DELETE"):
        return None
    if not ep.request_body_schema:
        return None
    body = _build_body(ep.request_body_schema)
    body.update({"role": "admin", "isAdmin": True, "admin": True})
    return TestCase(
        endpoint_method=ep.method,
        endpoint_path=ep.path,
        description="Mass assignment — inject role/admin fields",
        path_params=_path_params(ep.path, ep.parameters),
        query_params=_query_params(ep.parameters),
        headers=dict(auth_headers),
        body=body,
        expected_status_codes=[400, 403, 422],
        security_test_type="mass_assignment",
    )


def _rate_limit(ep: EndpointSpec, auth_headers: dict, enable: bool = False) -> TestCase | None:
    """Rate limit test — send 20 rapid requests and expect 429."""
    if not enable:
        return None
    return TestCase(
        endpoint_method=ep.method,
        endpoint_path=ep.path,
        description="Rate limit — 20 rapid requests expect 429",
        path_params=_path_params(ep.path, ep.parameters),
        query_params=_query_params(ep.parameters),
        headers=dict(auth_headers),
        body=_build_body(ep.request_body_schema or {}) or None,
        expected_status_codes=[429],
        security_test_type="rate_limit",
        repeat_count=20,
    )


# ── public API ────────────────────────────────────────────────────────────────

_FUNCTIONAL_BUILDERS = [
    lambda ep, auth, **kw: _happy_path(ep, auth),
    lambda ep, auth, **kw: _auth_bypass(ep),
    lambda ep, auth, **kw: _not_found(ep, auth),
    lambda ep, auth, **kw: _missing_required_field(ep, auth),
    lambda ep, auth, **kw: _wrong_type(ep, auth),
]

_SECURITY_BUILDERS = [
    lambda ep, auth, **kw: _sql_injection(ep, auth),
    lambda ep, auth, **kw: _xss(ep, auth),
    lambda ep, auth, **kw: _idor(ep, auth),
    lambda ep, auth, **kw: _error_disclosure(ep, auth),
    lambda ep, auth, **kw: _path_traversal(ep, auth),
    lambda ep, auth, **kw: _ssrf(ep, auth),
    lambda ep, auth, **kw: _mass_assignment(ep, auth),
    lambda ep, auth, enable_rate_limit=False, **kw: _rate_limit(ep, auth, enable=enable_rate_limit),
]

# Multi-TC builders (each returns a list)
_MULTI_TC_BUILDERS = [
    lambda ep, auth, **kw: _boundary_values(ep, auth),
]


def generate_local(
    spec: ParsedSpec,
    strategy: TestStrategy = TestStrategy.standard,
    auth_headers: dict[str, str] | None = None,
    max_tc_per_endpoint: int | None = None,
    on_endpoint_done: callable = None,
    enable_rate_limit_tests: bool = False,
) -> list[TestCase]:
    auth = auth_headers or {}
    total_limit = max_tc_per_endpoint if max_tc_per_endpoint is not None else _STRATEGY_TC_COUNT[strategy]
    sec_quota = _SECURITY_QUOTA[strategy] if max_tc_per_endpoint is None else 0
    # Functional TCs fill slots up to (total - security quota)
    func_limit = total_limit - sec_quota
    all_cases: list[TestCase] = []
    total = len(spec.endpoints)

    for i, ep in enumerate(spec.endpoints):
        cases: list[TestCase] = []

        # ── functional TCs ──────────────────────────────────────────────────
        for builder in _FUNCTIONAL_BUILDERS:
            if len(cases) >= func_limit:
                break
            tc = builder(ep, auth, enable_rate_limit=enable_rate_limit_tests)
            if tc:
                cases.append(tc)

        # ── security TCs (quota-guaranteed) ────────────────────────────────
        sec_cases: list[TestCase] = []
        for builder in _SECURITY_BUILDERS:
            if len(sec_cases) >= sec_quota:
                break
            tc = builder(ep, auth, enable_rate_limit=enable_rate_limit_tests)
            if tc:
                sec_cases.append(tc)
        cases.extend(sec_cases)

        # ── fill remaining slots with boundary values ───────────────────────
        for multi_builder in _MULTI_TC_BUILDERS:
            if len(cases) >= total_limit:
                break
            for tc in multi_builder(ep, auth):
                if len(cases) >= total_limit:
                    break
                cases.append(tc)

        all_cases.extend(cases)
        if on_endpoint_done:
            on_endpoint_done(i + 1, total, ep.method, ep.path, cases)

    return all_cases
