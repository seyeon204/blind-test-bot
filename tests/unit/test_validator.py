"""Unit tests for app/core/validator.py"""
import pytest

from app.core.validator import (
    _check_body_contains,
    detect_vulnerability,
    validate_result,
    validate_all,
)
from app.models.internal import ExecutionResult, TestCase, ValidationResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_tc(**kwargs) -> TestCase:
    defaults = dict(
        endpoint_method="GET",
        endpoint_path="/items",
        description="test",
        expected_status_codes=[200],
    )
    defaults.update(kwargs)
    return TestCase(**defaults)


def make_exec(tc: TestCase, *, status_code: int = 200, body=None, error: str | None = None) -> ExecutionResult:
    return ExecutionResult(
        test_case_id=tc.id,
        status_code=None if error else status_code,
        response_body=body,
        network_error=error,
    )


# ---------------------------------------------------------------------------
# validate_result — network error
# ---------------------------------------------------------------------------

def test_network_error_always_fails():
    tc = make_tc()
    result = validate_result(tc, make_exec(tc, error="connection refused"))
    assert result.passed is False
    assert "Network error" in result.failures[0]


# ---------------------------------------------------------------------------
# validate_result — status code checks
# ---------------------------------------------------------------------------

def test_happy_path_2xx_passes():
    tc = make_tc(expected_status_codes=[200])
    assert validate_result(tc, make_exec(tc, status_code=200)).passed is True


def test_happy_path_wrong_status_fails():
    tc = make_tc(expected_status_codes=[200])
    result = validate_result(tc, make_exec(tc, status_code=404))
    assert result.passed is False
    assert "Status code" in result.failures[0]


def test_multiple_expected_status_codes():
    tc = make_tc(expected_status_codes=[200, 201])
    assert validate_result(tc, make_exec(tc, status_code=201)).passed is True


# ---------------------------------------------------------------------------
# validate_result — auth_bypass
# ---------------------------------------------------------------------------

def test_auth_bypass_4xx_passes():
    tc = make_tc(security_test_type="auth_bypass", expected_status_codes=[401])
    # Any 4xx should pass, even if not in expected list
    for code in [400, 401, 403, 404, 422]:
        result = validate_result(tc, make_exec(tc, status_code=code))
        assert result.passed is True, f"Expected PASS for auth_bypass + {code}"


def test_auth_bypass_2xx_fails():
    tc = make_tc(security_test_type="auth_bypass", expected_status_codes=[401])
    result = validate_result(tc, make_exec(tc, status_code=200))
    assert result.passed is False


def test_auth_bypass_5xx_fails():
    tc = make_tc(security_test_type="auth_bypass", expected_status_codes=[401])
    result = validate_result(tc, make_exec(tc, status_code=500))
    assert result.passed is False


# ---------------------------------------------------------------------------
# validate_result — body schema
# ---------------------------------------------------------------------------

def test_body_schema_valid_passes():
    schema = {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}
    tc = make_tc(expected_body_schema=schema)
    result = validate_result(tc, make_exec(tc, body={"id": 1}))
    assert result.passed is True


def test_body_schema_invalid_fails():
    schema = {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}
    tc = make_tc(expected_body_schema=schema)
    result = validate_result(tc, make_exec(tc, body={"id": "not-an-int"}))
    assert result.passed is False
    assert any("schema" in f.lower() for f in result.failures)


def test_body_schema_plain_string_fails():
    schema = {"type": "object"}
    tc = make_tc(expected_body_schema=schema)
    result = validate_result(tc, make_exec(tc, body="plain text"))
    assert result.passed is False


# ---------------------------------------------------------------------------
# _check_body_contains
# ---------------------------------------------------------------------------

def test_body_contains_matching_key_value():
    assert _check_body_contains({"status": "ok"}, {"status": "ok"}) == []


def test_body_contains_missing_key():
    failures = _check_body_contains({"other": 1}, {"status": "ok"})
    assert any("status" in f for f in failures)


def test_body_contains_wrong_value():
    failures = _check_body_contains({"status": "error"}, {"status": "ok"})
    assert any("status" in f for f in failures)


def test_body_contains_any_wildcard():
    assert _check_body_contains({"id": 999}, {"id": "<any>"}) == []


def test_body_contains_non_dict_body():
    failures = _check_body_contains("not a dict", {"key": "val"})
    assert len(failures) == 1


def test_body_contains_empty_expected():
    assert _check_body_contains("anything", {}) == []


# ---------------------------------------------------------------------------
# detect_vulnerability
# ---------------------------------------------------------------------------

def test_auth_bypass_2xx_is_critical_vuln():
    tc = make_tc(security_test_type="auth_bypass", expected_status_codes=[401])
    vuln = detect_vulnerability(tc, make_exec(tc, status_code=200))
    assert vuln is not None
    assert vuln.severity == "critical"
    assert vuln.vuln_type == "auth_bypass"


def test_auth_bypass_4xx_no_vuln():
    tc = make_tc(security_test_type="auth_bypass", expected_status_codes=[401])
    assert detect_vulnerability(tc, make_exec(tc, status_code=401)) is None


def test_sql_injection_500_is_high_vuln():
    tc = make_tc(security_test_type="sql_injection", expected_status_codes=[400])
    vuln = detect_vulnerability(tc, make_exec(tc, status_code=500))
    assert vuln is not None
    assert vuln.severity == "high"


def test_sql_injection_400_no_vuln():
    tc = make_tc(security_test_type="sql_injection", expected_status_codes=[400])
    assert detect_vulnerability(tc, make_exec(tc, status_code=400)) is None


def test_idor_2xx_is_high_vuln():
    tc = make_tc(security_test_type="idor", expected_status_codes=[403])
    vuln = detect_vulnerability(tc, make_exec(tc, status_code=200))
    assert vuln is not None
    assert vuln.vuln_type == "idor"


def test_idor_4xx_no_vuln():
    tc = make_tc(security_test_type="idor", expected_status_codes=[403])
    assert detect_vulnerability(tc, make_exec(tc, status_code=403)) is None


def test_error_disclosure_sensitive_pattern():
    tc = make_tc(security_test_type="error_disclosure", expected_status_codes=[400])
    exec_ = make_exec(tc, status_code=500, body="Traceback (most recent call last): ...")
    vuln = detect_vulnerability(tc, exec_)
    assert vuln is not None
    assert vuln.vuln_type == "error_disclosure"
    assert vuln.severity == "medium"


def test_error_disclosure_500_no_sensitive_pattern():
    tc = make_tc(security_test_type="error_disclosure", expected_status_codes=[400])
    exec_ = make_exec(tc, status_code=500, body="Internal error")
    assert detect_vulnerability(tc, exec_) is None


def test_no_security_type_no_vuln():
    tc = make_tc()
    assert detect_vulnerability(tc, make_exec(tc, status_code=200)) is None


def test_network_error_no_vuln():
    tc = make_tc(security_test_type="auth_bypass", expected_status_codes=[401])
    assert detect_vulnerability(tc, make_exec(tc, error="timeout")) is None


# ---------------------------------------------------------------------------
# validate_all
# ---------------------------------------------------------------------------

def test_validate_all_no_execution_result():
    tc = make_tc()
    results = validate_all([tc], [])
    assert results[0].passed is False
    assert "No execution result found" in results[0].failures[0]


def test_validate_all_matches_by_id():
    tc = make_tc()
    exec_ = make_exec(tc, status_code=200)
    results = validate_all([tc], [exec_])
    assert results[0].passed is True
