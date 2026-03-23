"""Validates execution results against test case expectations."""
from __future__ import annotations

from typing import Any

import jsonschema

from app.models.internal import ExecutionResult, TestCase, ValidationResult, VulnerabilityResult


def _check_body_contains(body: Any, expected: dict[str, Any]) -> list[str]:
    """Check that all expected key-value pairs exist in the response body (shallow)."""
    failures = []
    if not isinstance(body, dict):
        if expected:
            failures.append(f"Expected response body to be a JSON object, got {type(body).__name__}")
        return failures

    for key, expected_value in expected.items():
        if key not in body:
            failures.append(f"Response body missing key '{key}'")
        elif expected_value != "<any>" and body[key] != expected_value:
            failures.append(
                f"Key '{key}': expected {expected_value!r}, got {body[key]!r}"
            )
    return failures


def validate_result(tc: TestCase, execution: ExecutionResult) -> ValidationResult:
    failures: list[str] = []

    # Network error → instant failure
    if execution.network_error:
        return ValidationResult(
            test_case_id=tc.id,
            passed=False,
            failures=[f"Network error: {execution.network_error}"],
            validation_mode="heuristic",
        )

    # Status code check
    # For auth_bypass tests, any 4xx means the endpoint is protected — treat as pass
    auth_bypass_ok = (
        tc.security_test_type == "auth_bypass"
        and execution.status_code is not None
        and 400 <= execution.status_code < 500
    )
    if not auth_bypass_ok and execution.status_code not in tc.expected_status_codes:
        failures.append(
            f"Status code: expected one of {tc.expected_status_codes}, "
            f"got {execution.status_code}"
        )

    # If a JSON response is expected (schema defined), check that the body is actually JSON
    if tc.expected_body_schema and isinstance(execution.response_body, str):
        preview = execution.response_body[:80].strip()
        failures.append(f"Expected JSON response body but got plain string: {preview!r}")
    elif tc.expected_body_schema and execution.response_body is not None:
        try:
            jsonschema.validate(instance=execution.response_body, schema=tc.expected_body_schema)
        except jsonschema.ValidationError as e:
            failures.append(f"Response body schema mismatch: {e.message}")
        except jsonschema.SchemaError:
            pass  # malformed schema in spec — skip

    # Body contains check
    if tc.expected_body_contains:
        failures.extend(_check_body_contains(execution.response_body, tc.expected_body_contains))

    return ValidationResult(
        test_case_id=tc.id,
        passed=len(failures) == 0,
        failures=failures,
        validation_mode="heuristic",
    )


_SENSITIVE_PATTERNS = [
    "traceback", "stack trace", "exception", "syntaxerror", "typeerror",
    "valueerror", "sql syntax", "pg::", "mysql", "sqlite", "ora-",
    "internal server error", "debug", "secret", "password", "private_key",
]


def detect_vulnerability(tc: TestCase, execution: ExecutionResult) -> VulnerabilityResult | None:
    if not tc.security_test_type or execution.network_error:
        return None

    status = execution.status_code
    endpoint = f"{tc.endpoint_method} {tc.endpoint_path}"
    body_str = str(execution.response_body).lower() if execution.response_body is not None else ""

    if tc.security_test_type == "auth_bypass":
        if status is not None and 200 <= status < 300:
            return VulnerabilityResult(
                test_case_id=tc.id,
                endpoint=endpoint,
                severity="critical",
                vuln_type="auth_bypass",
                description="Endpoint returned 2xx without authentication credentials.",
                evidence={"status_code": status, "request_headers": tc.headers},
            )

    elif tc.security_test_type in ("sql_injection", "xss"):
        if status == 500:
            return VulnerabilityResult(
                test_case_id=tc.id,
                endpoint=endpoint,
                severity="high",
                vuln_type=tc.security_test_type,
                description=f"Server returned 500 on {tc.security_test_type.replace('_', ' ')} payload — possible unhandled injection.",
                evidence={"status_code": status, "request_body": tc.body, "response_body": execution.response_body},
            )

    elif tc.security_test_type == "idor":
        if status is not None and 200 <= status < 300:
            return VulnerabilityResult(
                test_case_id=tc.id,
                endpoint=endpoint,
                severity="high",
                vuln_type="idor",
                description="Endpoint returned 2xx for a cross-user resource — possible IDOR.",
                evidence={"status_code": status, "path_params": tc.path_params, "response_body": execution.response_body},
            )

    elif tc.security_test_type == "error_disclosure":
        if status == 500 and any(p in body_str for p in _SENSITIVE_PATTERNS):
            return VulnerabilityResult(
                test_case_id=tc.id,
                endpoint=endpoint,
                severity="medium",
                vuln_type="error_disclosure",
                description="Server returned a verbose error response that may expose internal implementation details.",
                evidence={"status_code": status, "response_body": execution.response_body},
            )

    return None


def detect_all_vulnerabilities(
    test_cases: list[TestCase],
    executions: list[ExecutionResult],
) -> list[VulnerabilityResult]:
    exec_map = {e.test_case_id: e for e in executions}
    results = []
    for tc in test_cases:
        execution = exec_map.get(tc.id)
        if execution:
            vuln = detect_vulnerability(tc, execution)
            if vuln:
                results.append(vuln)
    return results


def validate_all(
    test_cases: list[TestCase],
    executions: list[ExecutionResult],
) -> list[ValidationResult]:
    exec_map = {e.test_case_id: e for e in executions}
    results = []
    for tc in test_cases:
        execution = exec_map.get(tc.id)
        if execution is None:
            results.append(ValidationResult(
                test_case_id=tc.id,
                passed=False,
                failures=["No execution result found"],
                validation_mode="heuristic",
            ))
        else:
            results.append(validate_result(tc, execution))
    return results
