"""Imports test cases directly from a Postman collection JSON."""
from __future__ import annotations

import json
import re
from typing import Any

from app.models.internal import TestCase


def is_postman(raw: bytes) -> bool:
    try:
        data = json.loads(raw)
        schema = data.get("info", {}).get("schema", "")
        return "getpostman.com" in schema
    except Exception:
        return False


def _substitute(value: str, variables: dict[str, str]) -> str:
    """Replace {{var}} placeholders with provided values."""
    def replace(m: re.Match) -> str:
        return variables.get(m.group(1), m.group(0))
    return re.sub(r"\{\{(\w[\w\-]*)\}\}", replace, value)


def _extract_path(url: dict | str, variables: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """Return (path_template, query_params) from a Postman URL object."""
    if isinstance(url, str):
        raw = _substitute(url, variables)
        path = re.sub(r"https?://[^/]+", "", raw).split("?")[0] or "/"
        return path, {}

    # Build path from parts, ignoring host/port
    path_parts = url.get("path", [])
    path = "/" + "/".join(
        _substitute(p if isinstance(p, str) else p.get("value", ""), variables)
        for p in path_parts
    )
    # Normalize :param → {param} (Postman sometimes uses :id)
    path = re.sub(r"/:(\w+)", r"/{\1}", path)

    query_params: dict[str, Any] = {}
    for q in url.get("query", []):
        if q.get("disabled"):
            continue
        key = q.get("key", "")
        val = _substitute(q.get("value", ""), variables)
        if key and val:
            query_params[key] = val

    return path, query_params


def _extract_body(body: dict | None, variables: dict[str, str]) -> Any:
    if not body:
        return None
    mode = body.get("mode")
    if mode == "raw":
        raw = _substitute(body.get("raw", ""), variables)
        try:
            return json.loads(raw)
        except Exception:
            return raw if raw.strip() else None
    return None


def _collect_requests(items: list, variables: dict[str, str]) -> list[TestCase]:
    cases: list[TestCase] = []
    for item in items:
        if "item" in item:
            # folder — recurse
            cases.extend(_collect_requests(item["item"], variables))
            continue

        req = item.get("request")
        if not req:
            continue

        method = req.get("method", "GET").upper()
        url_obj = req.get("url", {})

        path, query_params = _extract_path(url_obj, variables)

        # Extract path params: {reserveId} etc.
        path_params: dict[str, Any] = {}
        for name in re.findall(r"\{(\w+)\}", path):
            val = variables.get(name)
            if val is not None:
                try:
                    path_params[name] = int(val)
                except ValueError:
                    path_params[name] = val
            else:
                path_params[name] = 1  # default fallback

        # Headers
        headers: dict[str, str] = {}
        for h in req.get("header", []):
            if h.get("disabled"):
                continue
            key = h.get("key", "")
            val = _substitute(h.get("value", ""), variables)
            if key and val:
                headers[key] = val

        body = _extract_body(req.get("body"), variables)

        name = item.get("name", f"{method} {path}")

        cases.append(TestCase(
            endpoint_method=method,
            endpoint_path=path,
            description=name,
            path_params=path_params,
            query_params=query_params,
            headers=headers,
            body=body,
            expected_status_codes=[200, 201, 204],
        ))

    return cases


def parse_postman_collection(
    raw: bytes,
    variables: dict[str, str] | None = None,
) -> list[TestCase]:
    """Parse a Postman collection JSON and return TestCase list."""
    data = json.loads(raw)
    vars_map: dict[str, str] = {}

    # Merge collection-level variables
    for v in data.get("variable", []):
        key = v.get("key", "")
        val = str(v.get("value", ""))
        if key and val:
            vars_map[key] = val

    # User-provided variables override collection defaults
    if variables:
        vars_map.update(variables)

    items = data.get("item", [])
    return _collect_requests(items, vars_map)
