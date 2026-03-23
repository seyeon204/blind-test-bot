"""Unit tests for pure helper functions in app/core/executor.py"""
import pytest

from app.core.executor import _build_url, _extract_value, _resolve_templates


# ---------------------------------------------------------------------------
# _extract_value
# ---------------------------------------------------------------------------

def test_extract_top_level_key():
    assert _extract_value({"id": 42}, "id") == 42


def test_extract_nested_key():
    assert _extract_value({"user": {"name": "Alice"}}, "user.name") == "Alice"


def test_extract_deeply_nested():
    body = {"a": {"b": {"c": "deep"}}}
    assert _extract_value(body, "a.b.c") == "deep"


def test_extract_missing_key_returns_none():
    assert _extract_value({"id": 1}, "missing") is None


def test_extract_partial_path_returns_none():
    assert _extract_value({"user": "not-a-dict"}, "user.name") is None


def test_extract_none_body_returns_none():
    assert _extract_value(None, "id") is None


def test_extract_empty_path_returns_none():
    assert _extract_value({"id": 1}, "") is None


# ---------------------------------------------------------------------------
# _resolve_templates
# ---------------------------------------------------------------------------

def test_resolve_simple_string():
    assert _resolve_templates("hello {{name}}", {"name": "world"}) == "hello world"


def test_resolve_multiple_vars():
    result = _resolve_templates("{{a}}-{{b}}", {"a": "foo", "b": "bar"})
    assert result == "foo-bar"


def test_resolve_in_dict():
    result = _resolve_templates({"url": "/users/{{id}}"}, {"id": "123"})
    assert result == {"url": "/users/123"}


def test_resolve_in_list():
    result = _resolve_templates(["{{x}}", "{{y}}"], {"x": "1", "y": "2"})
    assert result == ["1", "2"]


def test_resolve_nested_dict():
    result = _resolve_templates({"a": {"b": "{{val}}"}}, {"val": "ok"})
    assert result == {"a": {"b": "ok"}}


def test_resolve_non_string_value_unchanged():
    assert _resolve_templates(42, {"x": "1"}) == 42
    assert _resolve_templates(None, {"x": "1"}) is None
    assert _resolve_templates(True, {"x": "1"}) is True


def test_resolve_integer_context_value_converted_to_str():
    result = _resolve_templates("/items/{{id}}", {"id": 99})
    assert result == "/items/99"


def test_resolve_no_matching_var_unchanged():
    assert _resolve_templates("{{missing}}", {}) == "{{missing}}"


# ---------------------------------------------------------------------------
# _build_url
# ---------------------------------------------------------------------------

def test_build_url_no_path_params():
    assert _build_url("https://api.example.com", "/users", {}) == "https://api.example.com/users"


def test_build_url_single_path_param():
    result = _build_url("https://api.example.com", "/users/{id}", {"id": 42})
    assert result == "https://api.example.com/users/42"


def test_build_url_multiple_path_params():
    result = _build_url("https://api.example.com", "/orgs/{org}/repos/{repo}", {"org": "acme", "repo": "app"})
    assert result == "https://api.example.com/orgs/acme/repos/app"


def test_build_url_strips_trailing_slash_from_base():
    result = _build_url("https://api.example.com/", "/users", {})
    assert result == "https://api.example.com/users"


def test_build_url_preserves_unresolved_params():
    # If a path param is not in the dict, the placeholder stays
    result = _build_url("https://api.example.com", "/users/{id}", {})
    assert "{id}" in result
