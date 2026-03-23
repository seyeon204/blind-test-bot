"""Unit tests for app/core/postman_parser.py"""
import json
import pytest

from app.core.postman_parser import (
    _extract_path,
    _substitute,
    is_postman,
    parse_postman_collection,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_collection(items: list, variables: list | None = None) -> bytes:
    data = {
        "info": {
            "name": "Test",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": items,
    }
    if variables:
        data["variable"] = variables
    return json.dumps(data).encode()


def make_request_item(name: str, method: str, path_parts: list, *, headers=None, body=None, query=None):
    url = {
        "raw": f"http://localhost/{'/'.join(path_parts)}",
        "host": ["localhost"],
        "path": path_parts,
    }
    if query:
        url["query"] = query
    return {
        "name": name,
        "request": {
            "method": method,
            "url": url,
            "header": headers or [],
            **({"body": body} if body else {}),
        },
    }


# ---------------------------------------------------------------------------
# is_postman
# ---------------------------------------------------------------------------

def test_is_postman_valid():
    col = make_collection([])
    assert is_postman(col) is True


def test_is_postman_missing_schema():
    data = {"info": {"name": "test"}, "item": []}
    assert is_postman(json.dumps(data).encode()) is False


def test_is_postman_wrong_schema_domain():
    data = {"info": {"name": "test", "schema": "https://other.com/schema"}, "item": []}
    assert is_postman(json.dumps(data).encode()) is False


def test_is_postman_invalid_json():
    assert is_postman(b"not json") is False


def test_is_postman_empty_bytes():
    assert is_postman(b"") is False


# ---------------------------------------------------------------------------
# _substitute
# ---------------------------------------------------------------------------

def test_substitute_replaces_variable():
    assert _substitute("Bearer {{token}}", {"token": "abc123"}) == "Bearer abc123"


def test_substitute_multiple_vars():
    result = _substitute("{{base}}/{{path}}", {"base": "http://api.com", "path": "users"})
    assert result == "http://api.com/users"


def test_substitute_missing_var_kept_as_is():
    assert _substitute("{{missing}}", {}) == "{{missing}}"


def test_substitute_no_vars_unchanged():
    assert _substitute("plain string", {"x": "1"}) == "plain string"


def test_substitute_hyphenated_var():
    assert _substitute("{{api-key}}", {"api-key": "secret"}) == "secret"


# ---------------------------------------------------------------------------
# _extract_path
# ---------------------------------------------------------------------------

def test_extract_path_string_url():
    path, qp = _extract_path("http://api.example.com/users/123", {})
    assert path == "/users/123"
    assert qp == {}


def test_extract_path_string_url_with_query_ignored():
    path, qp = _extract_path("http://api.example.com/items?limit=10", {})
    assert path == "/items"
    assert qp == {}  # query string in URL string not parsed into qp


def test_extract_path_object_url():
    url = {"path": ["users", "123"]}
    path, qp = _extract_path(url, {})
    assert path == "/users/123"


def test_extract_path_object_with_query_params():
    url = {
        "path": ["items"],
        "query": [{"key": "limit", "value": "10"}, {"key": "page", "value": "1"}],
    }
    path, qp = _extract_path(url, {})
    assert path == "/items"
    assert qp == {"limit": "10", "page": "1"}


def test_extract_path_disabled_query_param_skipped():
    url = {
        "path": ["items"],
        "query": [
            {"key": "limit", "value": "10", "disabled": True},
            {"key": "page", "value": "1"},
        ],
    }
    _, qp = _extract_path(url, {})
    assert "limit" not in qp
    assert "page" in qp


def test_extract_path_colon_param_converted():
    url = {"path": ["users", ":id"]}
    path, _ = _extract_path(url, {})
    assert "{id}" in path


def test_extract_path_variable_substitution_in_query():
    url = {
        "path": ["items"],
        "query": [{"key": "token", "value": "{{authToken}}"}],
    }
    _, qp = _extract_path(url, {"authToken": "abc"})
    assert qp["token"] == "abc"


def test_extract_path_object_with_segment_dict():
    # Some Postman collections use {"value": "segment"} instead of plain string
    url = {"path": [{"value": "users"}, {"value": "profile"}]}
    path, _ = _extract_path(url, {})
    assert path == "/users/profile"


# ---------------------------------------------------------------------------
# parse_postman_collection — basic
# ---------------------------------------------------------------------------

def test_parse_basic_get():
    col = make_collection([make_request_item("Get items", "GET", ["items"])])
    tcs = parse_postman_collection(col)
    assert len(tcs) == 1
    assert tcs[0].endpoint_method == "GET"
    assert tcs[0].endpoint_path == "/items"


def test_parse_preserves_item_name_as_description():
    col = make_collection([make_request_item("List all users", "GET", ["users"])])
    tcs = parse_postman_collection(col)
    assert tcs[0].description == "List all users"


def test_parse_headers_extracted():
    item = make_request_item("Get", "GET", ["items"], headers=[
        {"key": "Authorization", "value": "Bearer tok"},
    ])
    col = make_collection([item])
    tcs = parse_postman_collection(col)
    assert tcs[0].headers["Authorization"] == "Bearer tok"


def test_parse_disabled_header_skipped():
    item = make_request_item("Get", "GET", ["items"], headers=[
        {"key": "X-Secret", "value": "hidden", "disabled": True},
        {"key": "Accept", "value": "application/json"},
    ])
    col = make_collection([item])
    tcs = parse_postman_collection(col)
    assert "X-Secret" not in tcs[0].headers
    assert tcs[0].headers["Accept"] == "application/json"


def test_parse_raw_json_body():
    item = make_request_item("Create", "POST", ["items"],
        body={"mode": "raw", "raw": '{"name": "widget", "price": 9.99}'})
    col = make_collection([item])
    tcs = parse_postman_collection(col)
    assert tcs[0].body == {"name": "widget", "price": 9.99}


def test_parse_raw_non_json_body_stored_as_string():
    item = make_request_item("Upload", "POST", ["upload"],
        body={"mode": "raw", "raw": "plain text body"})
    col = make_collection([item])
    tcs = parse_postman_collection(col)
    assert tcs[0].body == "plain text body"


def test_parse_no_body_is_none():
    col = make_collection([make_request_item("Get", "GET", ["items"])])
    tcs = parse_postman_collection(col)
    assert tcs[0].body is None


# ---------------------------------------------------------------------------
# Variables substitution
# ---------------------------------------------------------------------------

def test_parse_user_variables_applied():
    item = make_request_item("Get", "GET", ["items"], headers=[
        {"key": "Authorization", "value": "Bearer {{token}}"},
    ])
    col = make_collection([item])
    tcs = parse_postman_collection(col, variables={"token": "mytoken"})
    assert tcs[0].headers["Authorization"] == "Bearer mytoken"


def test_parse_collection_level_variables():
    item = make_request_item("Get", "GET", ["items"], headers=[
        {"key": "X-Api-Key", "value": "{{apiKey}}"},
    ])
    col = make_collection([item], variables=[{"key": "apiKey", "value": "key123"}])
    tcs = parse_postman_collection(col)
    assert tcs[0].headers["X-Api-Key"] == "key123"


def test_parse_user_variables_override_collection():
    item = make_request_item("Get", "GET", ["items"], headers=[
        {"key": "X-Key", "value": "{{key}}"},
    ])
    col = make_collection([item], variables=[{"key": "key", "value": "collection-value"}])
    tcs = parse_postman_collection(col, variables={"key": "user-value"})
    assert tcs[0].headers["X-Key"] == "user-value"


def test_parse_variable_in_path_resolved_inline():
    """{{userId}} in path → substituted to concrete value, path has no {param} placeholder."""
    item = make_request_item("Get user", "GET", ["users", "{{userId}}"])
    col = make_collection([item])
    tcs = parse_postman_collection(col, variables={"userId": "42"})
    # Path is fully resolved: /users/42, no leftover path_params
    assert tcs[0].endpoint_path == "/users/42"
    assert tcs[0].path_params == {}


def test_parse_colon_path_param_gets_default_value():
    """:id style path param → converted to {id}, default value 1."""
    item = make_request_item("Get user", "GET", ["users", ":userId"])
    col = make_collection([item])
    tcs = parse_postman_collection(col)
    assert "{userId}" in tcs[0].endpoint_path
    assert tcs[0].path_params.get("userId") == 1  # default fallback


# ---------------------------------------------------------------------------
# Nested folders
# ---------------------------------------------------------------------------

def test_parse_nested_folder():
    folder = {
        "name": "Users",
        "item": [
            make_request_item("List", "GET", ["users"]),
            make_request_item("Create", "POST", ["users"]),
        ],
    }
    col = make_collection([folder])
    tcs = parse_postman_collection(col)
    assert len(tcs) == 2
    methods = {tc.endpoint_method for tc in tcs}
    assert methods == {"GET", "POST"}


def test_parse_deeply_nested_folder():
    inner = {"name": "Inner", "item": [make_request_item("Get", "GET", ["a"])]}
    outer = {"name": "Outer", "item": [inner]}
    col = make_collection([outer])
    tcs = parse_postman_collection(col)
    assert len(tcs) == 1


def test_parse_mixed_folders_and_items():
    folder = {"name": "F", "item": [make_request_item("A", "GET", ["a"])]}
    col = make_collection([folder, make_request_item("B", "POST", ["b"])])
    tcs = parse_postman_collection(col)
    assert len(tcs) == 2


# ---------------------------------------------------------------------------
# Default expected status codes
# ---------------------------------------------------------------------------

def test_parse_default_expected_status_codes():
    col = make_collection([make_request_item("Get", "GET", ["items"])])
    tcs = parse_postman_collection(col)
    assert set(tcs[0].expected_status_codes) == {200, 201, 204}
