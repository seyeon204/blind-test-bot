"""Parses OpenAPI 3.x / Swagger 2.0 specs into ParsedSpec."""
from __future__ import annotations

import json
from typing import Any
import yaml

from app.models.internal import EndpointSpec, ParameterSpec, ParsedSpec
from app.utils.exceptions import SpecParseError


def _resolve_ref(ref: str, root: dict) -> dict:
    """Resolve a $ref pointer like '#/components/schemas/User'."""
    if not ref.startswith("#/"):
        return {}
    parts = ref[2:].split("/")
    node = root
    for part in parts:
        node = node.get(part, {})
    return node


def _deref_schema(schema: Any, root: dict, depth: int = 0) -> Any:
    """Recursively dereference $ref in a schema (max depth 10)."""
    if depth > 10 or not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        schema = _resolve_ref(schema["$ref"], root)
    result = {}
    for k, v in schema.items():
        if isinstance(v, dict):
            result[k] = _deref_schema(v, root, depth + 1)
        elif isinstance(v, list):
            result[k] = [_deref_schema(i, root, depth + 1) if isinstance(i, dict) else i for i in v]
        else:
            result[k] = v
    return result


def _has_any_explicit_op_security(spec: dict) -> bool:
    """Return True if any operation in the spec has an explicit per-operation security field."""
    for path_item in spec.get("paths", {}).values():
        for method in ("get", "post", "put", "patch", "delete", "options", "head"):
            op = path_item.get(method)
            if op and op.get("security") is not None:
                return True
    return False


def _resolve_security(operation: dict, spec: dict, any_explicit_op_security: bool = False) -> list[str]:
    """Return the list of security scheme names that apply to this operation.

    Priority (highest first):
      1. operation-level `security` — explicit override, respected even when empty []
         (empty list = "no auth required for this endpoint")
      2. spec-level `security` — global default
      3. Inferred from securitySchemes/securityDefinitions — only when NO operation
         in the spec has any per-operation security annotation. This covers specs that
         declare schemes but omit both the global `security` array and per-op annotations
         (a common authoring oversight). When at least one operation annotates security,
         the spec author is intentionally controlling auth per-endpoint, so unannotated
         endpoints are treated as public.
    """
    op_security = operation.get("security")   # None = not set, [] = explicitly public
    if op_security is not None:
        return [list(s.keys())[0] for s in op_security if s]

    global_security = spec.get("security")    # None = not set, [] = explicitly public
    if global_security is not None:
        return [list(s.keys())[0] for s in global_security if s]

    # Inference only when no operation anywhere has explicit security (all-or-nothing heuristic)
    if any_explicit_op_security:
        return []

    # OpenAPI 3.x: components/securitySchemes
    scheme_names = list(spec.get("components", {}).get("securitySchemes", {}).keys())
    # Swagger 2.0: securityDefinitions
    if not scheme_names:
        scheme_names = list(spec.get("securityDefinitions", {}).keys())

    return scheme_names  # [] if truly no schemes declared


def _parse_openapi3(spec: dict) -> ParsedSpec:
    servers = spec.get("servers", [])
    base_url = servers[0].get("url") if servers else None
    endpoints = []
    any_explicit = _has_any_explicit_op_security(spec)

    for path, path_item in spec.get("paths", {}).items():
        for method in ("get", "post", "put", "patch", "delete", "options", "head"):
            operation = path_item.get(method)
            if not operation:
                continue

            params: list[ParameterSpec] = []
            # path-level params + operation-level params
            for raw_param in path_item.get("parameters", []) + operation.get("parameters", []):
                raw_param = _deref_schema(raw_param, spec)
                param_schema = _deref_schema(raw_param.get("schema", {}), spec)
                params.append(ParameterSpec(
                    name=raw_param.get("name", ""),
                    location=raw_param.get("in", "query"),
                    required=raw_param.get("required", False),
                    schema=param_schema,
                    example=raw_param.get("example"),
                ))

            # request body
            req_body_schema = None
            req_body = operation.get("requestBody", {})
            if req_body:
                req_body = _deref_schema(req_body, spec)
                content = req_body.get("content", {})
                json_content = content.get("application/json", content.get("*/*", {}))
                raw_schema = json_content.get("schema", {})
                req_body_schema = _deref_schema(raw_schema, spec)

            # responses
            expected_responses: dict[str, dict] = {}
            for status_code, resp_obj in operation.get("responses", {}).items():
                resp_obj = _deref_schema(resp_obj, spec)
                content = resp_obj.get("content", {})
                json_content = content.get("application/json", content.get("*/*", {}))
                raw_schema = json_content.get("schema", {})
                expected_responses[str(status_code)] = {
                    "schema": _deref_schema(raw_schema, spec),
                    "description": resp_obj.get("description", ""),
                }

            endpoints.append(EndpointSpec(
                method=method.upper(),
                path=path,
                summary=operation.get("summary", operation.get("operationId", "")),
                parameters=params,
                request_body_schema=req_body_schema,
                expected_responses=expected_responses,
                security_schemes=_resolve_security(operation, spec, any_explicit),
            ))

    return ParsedSpec(source_format="openapi", base_url=base_url, endpoints=endpoints)


def _parse_swagger2(spec: dict) -> ParsedSpec:
    host = spec.get("host", "")
    base_path = spec.get("basePath", "")
    schemes = spec.get("schemes", ["https"])
    base_url = f"{schemes[0]}://{host}{base_path}" if host else None
    endpoints = []
    any_explicit = _has_any_explicit_op_security(spec)

    for path, path_item in spec.get("paths", {}).items():
        for method in ("get", "post", "put", "patch", "delete", "options", "head"):
            operation = path_item.get(method)
            if not operation:
                continue

            params: list[ParameterSpec] = []
            for raw_param in path_item.get("parameters", []) + operation.get("parameters", []):
                raw_param = _deref_schema(raw_param, spec)
                if raw_param.get("in") == "body":
                    # handled as request_body_schema below
                    continue
                param_schema = _deref_schema(raw_param.get("schema", {"type": raw_param.get("type", "string")}), spec)
                params.append(ParameterSpec(
                    name=raw_param.get("name", ""),
                    location=raw_param.get("in", "query"),
                    required=raw_param.get("required", False),
                    schema=param_schema,
                    example=raw_param.get("example"),
                ))

            # request body (Swagger 2 uses 'body' parameter)
            req_body_schema = None
            for raw_param in operation.get("parameters", []):
                raw_param = _deref_schema(raw_param, spec)
                if raw_param.get("in") == "body":
                    req_body_schema = _deref_schema(raw_param.get("schema", {}), spec)
                    break

            # responses
            expected_responses: dict[str, dict] = {}
            for status_code, resp_obj in operation.get("responses", {}).items():
                resp_obj = _deref_schema(resp_obj, spec)
                raw_schema = _deref_schema(resp_obj.get("schema", {}), spec)
                expected_responses[str(status_code)] = {
                    "schema": raw_schema,
                    "description": resp_obj.get("description", ""),
                }

            endpoints.append(EndpointSpec(
                method=method.upper(),
                path=path,
                summary=operation.get("summary", operation.get("operationId", "")),
                parameters=params,
                request_body_schema=req_body_schema,
                expected_responses=expected_responses,
                security_schemes=_resolve_security(operation, spec, any_explicit),
            ))

    return ParsedSpec(source_format="swagger", base_url=base_url, endpoints=endpoints)


def parse_swagger(raw: str | bytes) -> ParsedSpec:
    """Parse a Swagger 2.0 or OpenAPI 3.x spec string/bytes into ParsedSpec."""
    text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
    try:
        spec: dict = yaml.safe_load(text)
    except yaml.YAMLError:
        try:
            spec = json.loads(text)
        except json.JSONDecodeError as e:
            raise SpecParseError(f"Failed to parse spec as YAML or JSON: {e}") from e

    if not isinstance(spec, dict):
        raise SpecParseError("Spec must be a YAML/JSON object")

    if "openapi" in spec:
        return _parse_openapi3(spec)
    elif "swagger" in spec:
        return _parse_swagger2(spec)
    else:
        raise SpecParseError("Not a valid OpenAPI/Swagger document (missing 'openapi' or 'swagger' key)")
