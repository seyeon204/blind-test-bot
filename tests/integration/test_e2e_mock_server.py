"""E2E integration tests: full pipeline against a real mock HTTP server.

pytest-httpserver spins up an actual HTTP server on a random localhost port,
so TC executions produce real HTTP responses — not network errors.

Coverage:
- GET / POST happy paths pass when server responds correctly
- Not-found TC passes when server returns 404 for unknown IDs
- Auth-bypass TC passes when server correctly rejects unauthenticated requests
- SQL-injection TC passes when server rejects invalid input
- Body assertions (expected_body_contains) are verified against real responses
- Summary reflects actual pass/fail counts, not all-failure from network errors
"""
from __future__ import annotations

import json
import re
import textwrap

import pytest
from werkzeug.wrappers import Response

from tests.integration.helpers import BASE, wait_for_run

TR = f"{BASE}/test-runs"

# ── Spec ──────────────────────────────────────────────────────────────────────
#
# Enhanced version of SIMPLE_OPENAPI with:
#   - Response schemas with required fields  → exercises _response_body_assertions
#   - components/securitySchemes             → parser correctly marks DELETE as secured
#
SPEC = textwrap.dedent("""
    openapi: "3.0.0"
    info:
      title: Sample API
      version: "1.0"
    servers:
      - url: http://sample.example.com
    components:
      securitySchemes:
        bearerAuth:
          type: http
          scheme: bearer
    paths:
      /items:
        get:
          summary: List items
          responses:
            "200":
              description: OK
              content:
                application/json:
                  schema:
                    type: array
                    items:
                      type: object
        post:
          summary: Create item
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  properties:
                    name:
                      type: string
          responses:
            "201":
              description: Created
              content:
                application/json:
                  schema:
                    type: object
                    required: [id, name]
                    properties:
                      id:
                        type: integer
                      name:
                        type: string
      /items/{id}:
        get:
          summary: Get item
          parameters:
            - name: id
              in: path
              required: true
              schema:
                type: integer
          responses:
            "200":
              description: OK
              content:
                application/json:
                  schema:
                    type: object
                    required: [id, name]
                    properties:
                      id:
                        type: integer
                      name:
                        type: string
            "404":
              description: Not found
        delete:
          summary: Delete item
          security:
            - bearerAuth: []
          parameters:
            - name: id
              in: path
              required: true
              schema:
                type: integer
          responses:
            "204":
              description: Deleted
            "401":
              description: Unauthorized
""").strip().encode()

_ID_PATH = re.compile(r"^/items/[^/]+$")


# ── Mock server setup ─────────────────────────────────────────────────────────

def _setup_mock_server(httpserver) -> None:
    """Register handlers that simulate a real implementation of SPEC."""

    # GET /items → 200 []
    httpserver.expect_request("/items", method="GET").respond_with_json(
        [], status=200
    )

    # POST /items → 201 {id, name}  |  400 if name missing or suspicious patterns
    _SUSPICIOUS = re.compile(r"'|\bOR\b|UNION|SELECT|DROP|--", re.IGNORECASE)

    def handle_post_items(request: "werkzeug.wrappers.Request") -> Response:
        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict) or "name" not in body:
            return Response(
                json.dumps({"detail": "name is required"}),
                status=400,
                content_type="application/json",
            )
        if not isinstance(body["name"], str):
            return Response(
                json.dumps({"detail": "name must be a string"}),
                status=422,
                content_type="application/json",
            )
        # Simulate input validation — reject SQL/script payloads
        if _SUSPICIOUS.search(body["name"]) or "<script" in body["name"].lower():
            return Response(
                json.dumps({"detail": "Invalid characters in name"}),
                status=400,
                content_type="application/json",
            )
        return Response(
            json.dumps({"id": 1, "name": body["name"]}),
            status=201,
            content_type="application/json",
        )

    httpserver.expect_request("/items", method="POST").respond_with_handler(
        handle_post_items
    )

    # GET /items/{id} → 200 {id, name}  |  404 for id=99999
    def handle_get_item(request: "werkzeug.wrappers.Request") -> Response:
        item_id = request.path.rstrip("/").split("/")[-1]
        if item_id == "99999" or not item_id.isdigit():
            return Response(
                json.dumps({"detail": "Not found"}),
                status=404,
                content_type="application/json",
            )
        return Response(
            json.dumps({"id": int(item_id), "name": "widget"}),
            status=200,
            content_type="application/json",
        )

    httpserver.expect_request(_ID_PATH, method="GET").respond_with_handler(
        handle_get_item
    )

    # DELETE /items/{id} → 401 if no Authorization header  |  204 if authorized
    def handle_delete_item(request: "werkzeug.wrappers.Request") -> Response:
        if not request.headers.get("Authorization"):
            return Response(
                json.dumps({"detail": "Authentication required"}),
                status=401,
                content_type="application/json",
            )
        return Response("", status=204)

    httpserver.expect_request(_ID_PATH, method="DELETE").respond_with_handler(
        handle_delete_item
    )


# ── Shared helper ─────────────────────────────────────────────────────────────

async def _run_pipeline(client, httpserver) -> tuple[str, list[dict]]:
    """Start a full-run against the mock server; return (run_id, results)."""
    _setup_mock_server(httpserver)
    base_url = httpserver.url_for("/").rstrip("/")

    resp = await client.post(
        f"{TR}/full-run",
        files={"spec_file": ("openapi.yaml", SPEC, "application/yaml")},
        data={
            "target_base_url": base_url,
            "phase2_provider": "local",
            "strategy": "standard",
        },
    )
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["run_id"]
    await wait_for_run(client, run_id, {"completed", "failed"}, timeout=20.0)
    results = (await client.get(f"{TR}/{run_id}/results")).json()["results"]
    return run_id, results


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_network_errors_when_server_is_reachable(client, httpserver):
    """All TCs reach the server — zero network errors."""
    _, results = await _run_pipeline(client, httpserver)

    network_errors = [
        r for r in results
        if r.get("failures") and any("Network error" in f for f in r["failures"])
    ]
    assert network_errors == [], (
        f"Unexpected network errors: {[r['description'] for r in network_errors]}"
    )


@pytest.mark.asyncio
async def test_get_items_happy_path_passes(client, httpserver):
    """GET /items → 200 [] → happy path TC passes."""
    _, results = await _run_pipeline(client, httpserver)

    tc = next(
        (r for r in results
         if r["endpoint"] == "GET /items" and "Happy path" in r["description"]),
        None,
    )
    assert tc is not None, "GET /items happy-path TC not found"
    assert tc["passed"] is True, f"Failures: {tc['failures']}"


@pytest.mark.asyncio
async def test_post_items_happy_path_passes_with_body_assertions(client, httpserver):
    """POST /items → 201 {id, name} → happy path + body assertion both pass."""
    _, results = await _run_pipeline(client, httpserver)

    tc = next(
        (r for r in results
         if r["endpoint"] == "POST /items" and "Happy path" in r["description"]),
        None,
    )
    assert tc is not None, "POST /items happy-path TC not found"
    assert tc["passed"] is True, (
        f"POST /items happy path failed — body assertions may have fired: {tc['failures']}"
    )


@pytest.mark.asyncio
async def test_not_found_tc_passes(client, httpserver):
    """GET /items/99999 → 404 → not-found TC passes."""
    _, results = await _run_pipeline(client, httpserver)

    tc = next(
        (r for r in results
         if "/items/{id}" in r["endpoint"]
         and "GET" in r["endpoint"]
         and ("404" in r["description"] or "Non-existent" in r["description"])),
        None,
    )
    # Fallback: find by description keyword
    if tc is None:
        tc = next(
            (r for r in results if "non-existent" in r["description"].lower()),
            None,
        )
    assert tc is not None, "Not-found TC not found in results"
    assert tc["passed"] is True, f"Failures: {tc['failures']}"


@pytest.mark.asyncio
async def test_auth_bypass_passes_when_server_requires_auth(client, httpserver):
    """DELETE /items/{id} with no auth → 401 → auth-bypass TC passes."""
    _, results = await _run_pipeline(client, httpserver)

    tc = next(
        (r for r in results
         if "auth" in r["description"].lower() and "bypass" in r["description"].lower()),
        None,
    )
    assert tc is not None, "Auth-bypass TC not found in results"
    assert tc["passed"] is True, f"Failures: {tc['failures']}"


@pytest.mark.asyncio
async def test_sql_injection_tc_passes(client, httpserver):
    """POST /items with SQL payload → 400 → sql_injection TC passes (server rejects)."""
    _, results = await _run_pipeline(client, httpserver)

    tc = next(
        (r for r in results if "SQL injection" in r["description"] or "sql" in r["description"].lower()),
        None,
    )
    assert tc is not None, "SQL injection TC not found"
    assert tc["passed"] is True, f"Failures: {tc['failures']}"


@pytest.mark.asyncio
async def test_summary_has_real_pass_count(client, httpserver):
    """With a real server, passed > 0 and totals add up."""
    run_id, _ = await _run_pipeline(client, httpserver)
    final = (await client.get(f"{TR}/{run_id}")).json()

    assert final["status"] == "completed"
    summary = final["summary"]
    assert summary is not None
    assert summary["passed"] > 0, "Expected at least some TCs to pass with a real server"
    assert summary["passed"] + summary["failed"] == summary["total"]
