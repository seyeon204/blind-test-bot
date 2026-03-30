"""Integration tests for the test-runs API pipeline."""
from __future__ import annotations

import json
import textwrap

import pytest

from tests.integration.helpers import BASE, wait_for_run

TR = f"{BASE}/test-runs"  # shorthand


# ---------------------------------------------------------------------------
# Sample OpenAPI spec (small, deterministic)
# ---------------------------------------------------------------------------

SIMPLE_OPENAPI = textwrap.dedent("""
    openapi: "3.0.0"
    info:
      title: Sample API
      version: "1.0"
    servers:
      - url: http://sample.example.com
    paths:
      /items:
        get:
          summary: List items
          parameters:
            - name: limit
              in: query
              required: false
              schema:
                type: integer
          responses:
            "200":
              description: OK
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
""").strip().encode()


POSTMAN_COLLECTION = json.dumps({
    "info": {
        "name": "Test Collection",
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
    },
    "item": [
        {
            "name": "List items",
            "request": {
                "method": "GET",
                "url": {
                    "raw": "http://localhost:8080/items",
                    "host": ["localhost"],
                    "path": ["items"],
                },
                "header": [{"key": "Authorization", "value": "Bearer token123"}],
            },
        },
        {
            "name": "Create item",
            "request": {
                "method": "POST",
                "url": {
                    "raw": "http://localhost:8080/items",
                    "host": ["localhost"],
                    "path": ["items"],
                },
                "header": [{"key": "Content-Type", "value": "application/json"}],
                "body": {"mode": "raw", "raw": '{"name": "widget"}'},
            },
        },
    ],
}).encode()


# ---------------------------------------------------------------------------
# 404 / not found
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_unknown_run_returns_404(client):
    resp = await client.get(f"{TR}/nonexistent-id")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_logs_unknown_run_returns_404(client):
    resp = await client.get(f"{TR}/nonexistent-id/logs")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_test_cases_unknown_run_returns_404(client):
    resp = await client.get(f"{TR}/nonexistent-id/test-cases")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_results_unknown_run_returns_404(client):
    resp = await client.get(f"{TR}/nonexistent-id/results")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_plan_unknown_run_returns_404(client):
    resp = await client.get(f"{TR}/nonexistent-id/plan")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Step 1: parse
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parse_returns_202_with_run_id(client):
    resp = await client.post(
        TR,
        files={"spec_file": ("openapi.yaml", SIMPLE_OPENAPI, "application/yaml")},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert "run_id" in data
    assert data["status"] == "parsing"


@pytest.mark.asyncio
async def test_parse_transitions_to_parsed(client):
    resp = await client.post(
        TR,
        files={"spec_file": ("openapi.yaml", SIMPLE_OPENAPI, "application/yaml")},
    )
    run_id = resp.json()["run_id"]
    final = await wait_for_run(client, run_id, {"parsed", "failed"})
    assert final["status"] == "parsed"
    assert len(final["endpoints"]) == 4  # GET/POST /items, GET/DELETE /items/{id}


@pytest.mark.asyncio
async def test_parsed_run_has_source_format(client):
    resp = await client.post(
        TR,
        files={"spec_file": ("openapi.yaml", SIMPLE_OPENAPI, "application/yaml")},
    )
    run_id = resp.json()["run_id"]
    final = await wait_for_run(client, run_id, {"parsed", "failed"})
    assert final["source_format"] == "openapi"


# ---------------------------------------------------------------------------
# Step 2: generate (after parse)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_wrong_status_returns_409(client):
    resp = await client.post(
        TR,
        files={"spec_file": ("openapi.yaml", SIMPLE_OPENAPI, "application/yaml")},
    )
    run_id = resp.json()["run_id"]

    # Immediately call generate before parsing completes — should get 409
    gen_resp = await client.post(
        f"{TR}/{run_id}/generate",
        data={"phase2_provider": "local", "strategy": "minimal"},
    )
    assert gen_resp.status_code == 409


@pytest.mark.asyncio
async def test_generate_after_parse_transitions_to_generated(client):
    resp = await client.post(
        TR,
        files={"spec_file": ("openapi.yaml", SIMPLE_OPENAPI, "application/yaml")},
    )
    run_id = resp.json()["run_id"]
    await wait_for_run(client, run_id, {"parsed"})

    gen_resp = await client.post(
        f"{TR}/{run_id}/generate",
        data={"phase2_provider": "local", "strategy": "standard"},
    )
    assert gen_resp.status_code == 202

    final = await wait_for_run(client, run_id, {"generated", "failed"})
    assert final["status"] == "generated"
    assert final["test_case_count"] > 0


@pytest.mark.asyncio
async def test_generated_run_has_test_cases(client):
    resp = await client.post(
        TR,
        files={"spec_file": ("openapi.yaml", SIMPLE_OPENAPI, "application/yaml")},
    )
    run_id = resp.json()["run_id"]
    await wait_for_run(client, run_id, {"parsed"})
    await client.post(
        f"{TR}/{run_id}/generate",
        data={"phase2_provider": "local", "strategy": "standard"},
    )
    await wait_for_run(client, run_id, {"generated"})

    tc_resp = await client.get(f"{TR}/{run_id}/test-cases")
    assert tc_resp.status_code == 200
    tcs = tc_resp.json()
    assert isinstance(tcs, list) and len(tcs) > 0
    tc = tcs[0]
    assert "id" in tc and "endpoint" in tc and "description" in tc


# ---------------------------------------------------------------------------
# Full run pipeline
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_run_returns_202_with_run_id(client):
    resp = await client.post(
        f"{TR}/full-run",
        files={"spec_file": ("openapi.yaml", SIMPLE_OPENAPI, "application/yaml")},
        data={"target_base_url": "http://localhost:19999", "phase2_provider": "local"},
    )
    assert resp.status_code == 202
    assert "run_id" in resp.json()


@pytest.mark.asyncio
async def test_full_run_completes(client):
    resp = await client.post(
        f"{TR}/full-run",
        files={"spec_file": ("openapi.yaml", SIMPLE_OPENAPI, "application/yaml")},
        data={"target_base_url": "http://localhost:19999", "phase2_provider": "local"},
    )
    run_id = resp.json()["run_id"]
    final = await wait_for_run(client, run_id, {"completed", "failed"}, timeout=10.0)
    assert final["status"] == "completed"


@pytest.mark.asyncio
async def test_full_run_has_summary(client):
    resp = await client.post(
        f"{TR}/full-run",
        files={"spec_file": ("openapi.yaml", SIMPLE_OPENAPI, "application/yaml")},
        data={"target_base_url": "http://localhost:19999", "phase2_provider": "local"},
    )
    run_id = resp.json()["run_id"]
    final = await wait_for_run(client, run_id, {"completed", "failed"}, timeout=10.0)
    assert final["summary"] is not None
    assert final["summary"]["total"] > 0
    # Network errors → all fail, but total == passed + failed
    assert final["summary"]["total"] == final["summary"]["passed"] + final["summary"]["failed"]


@pytest.mark.asyncio
async def test_full_run_has_results(client):
    resp = await client.post(
        f"{TR}/full-run",
        files={"spec_file": ("openapi.yaml", SIMPLE_OPENAPI, "application/yaml")},
        data={"target_base_url": "http://localhost:19999", "phase2_provider": "local"},
    )
    run_id = resp.json()["run_id"]
    await wait_for_run(client, run_id, {"completed", "failed"}, timeout=10.0)

    results_resp = await client.get(f"{TR}/{run_id}/results")
    assert results_resp.status_code == 200
    data = results_resp.json()
    assert isinstance(data["results"], list) and len(data["results"]) > 0


@pytest.mark.asyncio
async def test_full_run_results_filter_passed(client):
    resp = await client.post(
        f"{TR}/full-run",
        files={"spec_file": ("openapi.yaml", SIMPLE_OPENAPI, "application/yaml")},
        data={"target_base_url": "http://localhost:19999", "phase2_provider": "local"},
    )
    run_id = resp.json()["run_id"]
    await wait_for_run(client, run_id, {"completed", "failed"}, timeout=10.0)

    passed_resp = await client.get(f"{TR}/{run_id}/results?passed=true")
    assert all(r["passed"] is True for r in passed_resp.json()["results"])

    failed_resp = await client.get(f"{TR}/{run_id}/results?passed=false")
    assert all(r["passed"] is False for r in failed_resp.json()["results"])


@pytest.mark.asyncio
async def test_full_run_logs_non_empty(client):
    resp = await client.post(
        f"{TR}/full-run",
        files={"spec_file": ("openapi.yaml", SIMPLE_OPENAPI, "application/yaml")},
        data={"target_base_url": "http://localhost:19999", "phase2_provider": "local"},
    )
    run_id = resp.json()["run_id"]
    await wait_for_run(client, run_id, {"completed", "failed"}, timeout=10.0)

    logs = (await client.get(f"{TR}/{run_id}/logs")).json()
    assert isinstance(logs, list) and len(logs) > 0


# ---------------------------------------------------------------------------
# Step 3: execute (after generate) — separate 3-step flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_wrong_status_returns_409(client):
    resp = await client.post(
        TR,
        files={"spec_file": ("openapi.yaml", SIMPLE_OPENAPI, "application/yaml")},
    )
    run_id = resp.json()["run_id"]
    # Call execute while still parsing — should get 409
    exec_resp = await client.post(
        f"{TR}/{run_id}/execute",
        data={"target_base_url": "http://localhost:19999"},
    )
    assert exec_resp.status_code == 409


@pytest.mark.asyncio
async def test_three_step_separate_flow(client):
    """parse → generate → execute individually, all reaching correct statuses."""
    # Step 1: Parse
    resp = await client.post(
        TR,
        files={"spec_file": ("openapi.yaml", SIMPLE_OPENAPI, "application/yaml")},
    )
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    await wait_for_run(client, run_id, {"parsed"})

    # Step 2: Generate
    gen_resp = await client.post(
        f"{TR}/{run_id}/generate",
        data={"phase2_provider": "local", "strategy": "minimal"},
    )
    assert gen_resp.status_code == 202
    await wait_for_run(client, run_id, {"generated"})

    # Step 3: Execute
    exec_resp = await client.post(
        f"{TR}/{run_id}/execute",
        data={"target_base_url": "http://localhost:19999"},
    )
    assert exec_resp.status_code == 202
    final = await wait_for_run(client, run_id, {"completed", "failed"}, timeout=10.0)
    assert final["status"] == "completed"

    # Verify results are accessible
    results = (await client.get(f"{TR}/{run_id}/results")).json()
    assert isinstance(results["results"], list)
    assert len(results["results"]) > 0


# ---------------------------------------------------------------------------
# format=md-failures endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_results_md_failures_returns_markdown(client):
    """GET /results?format=md-failures returns text/markdown with correct filename."""
    resp = await client.post(
        f"{TR}/full-run",
        files={"spec_file": ("openapi.yaml", SIMPLE_OPENAPI, "application/yaml")},
        data={"target_base_url": "http://localhost:9999", "phase2_provider": "local"},
    )
    run_id = resp.json()["run_id"]
    await wait_for_run(client, run_id, {"completed", "failed"}, timeout=10.0)

    md_resp = await client.get(f"{TR}/{run_id}/results?format=md-failures")
    assert md_resp.status_code == 200
    assert "text/markdown" in md_resp.headers["content-type"]
    assert f"failures-{run_id[:8]}.md" in md_resp.headers["content-disposition"]

    body = md_resp.text
    assert "# Failure Report" in body
    assert "Run ID" in body


@pytest.mark.asyncio
async def test_results_md_failures_unknown_run_returns_404(client):
    resp = await client.get(f"{TR}/nonexistent/results?format=md-failures")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# import-postman
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_import_postman_returns_201(client):
    resp = await client.post(
        f"{TR}/import-postman",
        files={"collection_file": ("collection.json", POSTMAN_COLLECTION, "application/json")},
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_import_postman_jumps_to_generated(client):
    resp = await client.post(
        f"{TR}/import-postman",
        files={"collection_file": ("collection.json", POSTMAN_COLLECTION, "application/json")},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "generated"
    assert data["test_case_count"] == 2


@pytest.mark.asyncio
async def test_import_invalid_postman_returns_422(client):
    bad_json = b'{"info": {"name": "bad", "schema": "not-postman"}}'
    resp = await client.post(
        f"{TR}/import-postman",
        files={"collection_file": ("collection.json", bad_json, "application/json")},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_unknown_run_returns_404(client):
    resp = await client.post(f"{TR}/nonexistent-id/cancel")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cancel_completed_run_returns_false(client):
    resp = await client.post(
        f"{TR}/full-run",
        files={"spec_file": ("openapi.yaml", SIMPLE_OPENAPI, "application/yaml")},
        data={"target_base_url": "http://localhost:19999", "phase2_provider": "local"},
    )
    run_id = resp.json()["run_id"]
    await wait_for_run(client, run_id, {"completed", "failed"}, timeout=10.0)

    cancel_resp = await client.post(f"{TR}/{run_id}/cancel")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["cancelled"] is False
