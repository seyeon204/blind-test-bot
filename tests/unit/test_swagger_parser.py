"""Unit tests for app/core/swagger_parser.py"""
import json
import textwrap

import pytest

from app.core.swagger_parser import parse_swagger
from app.utils.exceptions import SpecParseError


# ---------------------------------------------------------------------------
# OpenAPI 3.x
# ---------------------------------------------------------------------------

OPENAPI3_MINIMAL = textwrap.dedent("""
    openapi: "3.0.0"
    info:
      title: Test API
      version: "1.0"
    servers:
      - url: https://api.example.com
    paths:
      /users:
        get:
          summary: List users
          parameters:
            - name: limit
              in: query
              required: false
              schema:
                type: integer
          responses:
            "200":
              description: OK
              content:
                application/json:
                  schema:
                    type: array
      /users/{id}:
        get:
          summary: Get user
          parameters:
            - name: id
              in: path
              required: true
              schema:
                type: integer
          responses:
            "200":
              description: OK
            "404":
              description: Not found
        delete:
          summary: Delete user
          security:
            - bearerAuth: []
          responses:
            "204":
              description: Deleted
""")

OPENAPI3_WITH_BODY = textwrap.dedent("""
    openapi: "3.0.0"
    info:
      title: API
      version: "1"
    paths:
      /users:
        post:
          summary: Create user
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  properties:
                    name:
                      type: string
                  required:
                    - name
          responses:
            "201":
              description: Created
""")


def test_openapi3_source_format():
    spec = parse_swagger(OPENAPI3_MINIMAL)
    assert spec.source_format == "openapi"


def test_openapi3_base_url():
    spec = parse_swagger(OPENAPI3_MINIMAL)
    assert spec.base_url == "https://api.example.com"


def test_openapi3_endpoint_count():
    spec = parse_swagger(OPENAPI3_MINIMAL)
    methods = [(e.method, e.path) for e in spec.endpoints]
    assert ("GET", "/users") in methods
    assert ("GET", "/users/{id}") in methods
    assert ("DELETE", "/users/{id}") in methods


def test_openapi3_query_param():
    spec = parse_swagger(OPENAPI3_MINIMAL)
    get_users = next(e for e in spec.endpoints if e.method == "GET" and e.path == "/users")
    param = next(p for p in get_users.parameters if p.name == "limit")
    assert param.location == "query"
    assert param.required is False
    assert param.schema_["type"] == "integer"


def test_openapi3_path_param():
    spec = parse_swagger(OPENAPI3_MINIMAL)
    get_user = next(e for e in spec.endpoints if e.method == "GET" and e.path == "/users/{id}")
    param = next(p for p in get_user.parameters if p.name == "id")
    assert param.location == "path"
    assert param.required is True


def test_openapi3_multiple_responses():
    spec = parse_swagger(OPENAPI3_MINIMAL)
    get_user = next(e for e in spec.endpoints if e.method == "GET" and e.path == "/users/{id}")
    assert "200" in get_user.expected_responses
    assert "404" in get_user.expected_responses


def test_openapi3_security_scheme():
    spec = parse_swagger(OPENAPI3_MINIMAL)
    delete_user = next(e for e in spec.endpoints if e.method == "DELETE")
    assert "bearerAuth" in delete_user.security_schemes


def test_openapi3_request_body_schema():
    spec = parse_swagger(OPENAPI3_WITH_BODY)
    post_users = next(e for e in spec.endpoints if e.method == "POST")
    assert post_users.request_body_schema is not None
    assert post_users.request_body_schema["type"] == "object"
    assert "name" in post_users.request_body_schema["properties"]


def test_openapi3_json_input():
    spec_dict = {
        "openapi": "3.0.0",
        "info": {"title": "T", "version": "1"},
        "paths": {
            "/ping": {
                "get": {
                    "summary": "ping",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    spec = parse_swagger(json.dumps(spec_dict))
    assert len(spec.endpoints) == 1
    assert spec.endpoints[0].path == "/ping"


# ---------------------------------------------------------------------------
# Swagger 2.x
# ---------------------------------------------------------------------------

SWAGGER2_MINIMAL = textwrap.dedent("""
    swagger: "2.0"
    info:
      title: Test
      version: "1"
    host: api.example.com
    basePath: /v1
    schemes:
      - https
    paths:
      /items:
        get:
          summary: List items
          parameters:
            - name: page
              in: query
              type: integer
              required: false
          responses:
            200:
              description: OK
        post:
          summary: Create item
          parameters:
            - name: body
              in: body
              schema:
                type: object
                properties:
                  name:
                    type: string
          responses:
            201:
              description: Created
      /items/{id}:
        get:
          summary: Get item
          parameters:
            - name: id
              in: path
              type: integer
              required: true
          responses:
            200:
              description: OK
""")


def test_swagger2_source_format():
    spec = parse_swagger(SWAGGER2_MINIMAL)
    assert spec.source_format == "swagger"


def test_swagger2_base_url():
    spec = parse_swagger(SWAGGER2_MINIMAL)
    assert spec.base_url == "https://api.example.com/v1"


def test_swagger2_endpoints():
    spec = parse_swagger(SWAGGER2_MINIMAL)
    methods = [(e.method, e.path) for e in spec.endpoints]
    assert ("GET", "/items") in methods
    assert ("POST", "/items") in methods
    assert ("GET", "/items/{id}") in methods


def test_swagger2_query_param():
    spec = parse_swagger(SWAGGER2_MINIMAL)
    get_items = next(e for e in spec.endpoints if e.method == "GET" and e.path == "/items")
    param = next(p for p in get_items.parameters if p.name == "page")
    assert param.location == "query"
    assert param.required is False


def test_swagger2_path_param():
    spec = parse_swagger(SWAGGER2_MINIMAL)
    get_item = next(e for e in spec.endpoints if e.path == "/items/{id}")
    param = next(p for p in get_item.parameters if p.name == "id")
    assert param.location == "path"
    assert param.required is True


def test_swagger2_body_param_becomes_request_body_schema():
    spec = parse_swagger(SWAGGER2_MINIMAL)
    post_items = next(e for e in spec.endpoints if e.method == "POST")
    assert post_items.request_body_schema is not None
    assert post_items.request_body_schema["type"] == "object"
    # Body param should NOT appear in parameters list
    assert all(p.location != "body" for p in post_items.parameters)


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

def test_invalid_yaml_raises():
    with pytest.raises(SpecParseError):
        parse_swagger(": : : not valid yaml or json")


def test_missing_openapi_or_swagger_key_raises():
    with pytest.raises(SpecParseError, match="missing"):
        parse_swagger('{"info": {"title": "no version key"}}')


def test_non_dict_raises():
    with pytest.raises(SpecParseError):
        parse_swagger("[1, 2, 3]")


def test_empty_paths():
    raw = '{"openapi": "3.0.0", "info": {"title": "T", "version": "1"}, "paths": {}}'
    spec = parse_swagger(raw)
    assert spec.endpoints == []


# ---------------------------------------------------------------------------
# $ref resolution
# ---------------------------------------------------------------------------

OPENAPI3_WITH_REF = textwrap.dedent("""
    openapi: "3.0.0"
    info:
      title: T
      version: "1"
    components:
      schemas:
        User:
          type: object
          properties:
            id:
              type: integer
    paths:
      /users/{id}:
        get:
          summary: get
          parameters:
            - name: id
              in: path
              required: true
              schema:
                $ref: "#/components/schemas/User"
          responses:
            "200":
              description: OK
              content:
                application/json:
                  schema:
                    $ref: "#/components/schemas/User"
""")


def test_ref_resolved_in_param_schema():
    spec = parse_swagger(OPENAPI3_WITH_REF)
    ep = spec.endpoints[0]
    param = ep.parameters[0]
    assert param.schema_.get("type") == "object"


def test_ref_resolved_in_response_schema():
    spec = parse_swagger(OPENAPI3_WITH_REF)
    ep = spec.endpoints[0]
    schema = ep.expected_responses["200"]["schema"]
    assert schema.get("type") == "object"
