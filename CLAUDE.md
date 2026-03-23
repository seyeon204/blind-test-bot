# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

FastAPI service that auto-generates and executes API test cases from spec files.
"Blind" = tests are generated purely from specs, without access to the running API.

**3-stage pipeline:**
```
parse (spec → endpoints)  →  generate (endpoints → test cases)  →  execute (test cases → results)
```

## Dev Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run server (Swagger UI at http://localhost:8000/docs)
uvicorn app.main:app --reload

# Run with mock API key (no Anthropic tokens consumed)
ANTHROPIC_API_KEY=mock-anything uvicorn app.main:app --reload

# Run all tests
python3 -m pytest tests/

# Run a single test file
python3 -m pytest tests/unit/test_swagger_parser.py

# Run a single test by name
python3 -m pytest tests/unit/test_tc_planner.py -k "test_name"

# Run tests with coverage report
python3 -m pytest tests/ --cov=app --cov-report=term-missing
```

**Test layout:**
- `tests/unit/` — isolated unit tests (no HTTP, mock Claude via `mock-*` key)
- `tests/integration/` — full pipeline via `AsyncClient` against the FastAPI app
- `tests/integration/conftest.py` — `autouse` fixtures: sets `mock-test-key`, disables rate limits/delays, wipes all orchestrator state between tests

**.env** required:
```
ANTHROPIC_API_KEY=sk-ant-...
```

## Architecture

```
app/
├── api/v1/endpoints/test_runs.py   # All HTTP endpoints
├── services/test_orchestrator.py   # Pipeline state management + async tasks
├── core/
│   ├── spec_parser.py              # Dispatcher: routes to swagger/document parser
│   ├── swagger_parser.py           # Swagger/OpenAPI YAML/JSON → ParsedSpec
│   ├── document_parser.py          # Text/PDF → Claude → endpoint extraction
│   ├── postman_parser.py           # Postman collection → TestCase list
│   ├── tc_planner.py               # Phase 1: Claude builds a TestPlan before generating TCs
│   ├── tc_generator.py             # Claude-based TC generation (batched, tool-use)
│   ├── local_tc_generator.py       # Rule-based TC generation (free, no API calls)
│   ├── executor.py                 # httpx execution engine + stream_executions()
│   ├── ai_validator.py             # Claude-based pass/fail judgment
│   └── validator.py                # Heuristic pass/fail (fallback)
├── models/
│   ├── internal.py                 # ParsedSpec, EndpointSpec, TestCase, ExecutionResult, ...
│   ├── request.py                  # GenerateConfig, ExecuteConfig, TestStrategy, GeneratorType
│   └── response.py                 # TestRunStatusResponse, GeneratedTestCase, RunStatus, ...
├── utils/
│   ├── claude_client.py            # Anthropic async client, rate limiter, retry
│   └── exceptions.py               # TCGenerationError
└── config.py                       # Pydantic-settings, reads .env
```

## Key Design Decisions

### Run State Flow
```
parsing → parsed → analyzing → generating → generated → executing → completed
```
- `analyzing` = tc_planner Phase 1 (Claude generator only; skipped in `local` mode)
- `running` = full-run streaming mode (generate + execute concurrently)
- `failed` / `cancelled` can occur at any stage

### Generator Modes
- `local` (default): rule-based, free, instant
- `claude`: AI-generated via Haiku model, batched to stay within RPM limits

### TC Generation (Claude mode)
- Batches 3 endpoints per API call to minimize token usage
- TC count is decided by Claude based on endpoint complexity (not a fixed number)
- `strategy` (`minimal` / `standard` / `exhaustive`) hints at test depth
- `max_tc_per_endpoint` acts as a hard cap when set
- Batch delay: `tc_batch_delay_seconds` (default 10s) — prevents TPM overrun
- Fatal errors (`AuthenticationError`, `BadRequestError`) re-raise immediately, no retry

### AI Validation
- After execution, `ai_validator.py` sends the full result batch to Claude in one call
- Claude semantically determines PASS/FAIL based on test intent, not exact status code matching:
  - happy path → 2xx = PASS
  - negative test → 4xx = PASS
  - auth_bypass → 4xx = PASS, 2xx = vulnerability
- Falls back to `validator.py` heuristics if the Claude call fails

### Streaming Pipeline (full-run)
`test_orchestrator.py → _stream_generate_execute_pipeline`:
- `asyncio.Queue` bridges generator and executor
- `on_endpoint_done` callback pushes completed TC batches to the queue
- Executor coroutine pops from the queue and fires requests immediately
- Both run via `asyncio.gather(_generator(), _executor())`

### Rate Limiting
`claude_client.py` global serialization:
- `_rate_lock` + `_last_call_time` enforce minimum interval between API calls
- Based on `anthropic_rpm` setting (default 40 RPM)
- 429 retry: `min(30 * 2^attempt, 300) + random(0,10)` seconds (up to `max_retries=5`)
- 529 (overloaded) retry: `min(15 * 2^attempt, 120) + random(0,5)` seconds

### Postman Integration
Two distinct modes:
1. **Import**: `POST /import-postman` — collection → TestCase list, jumps to `generated` state
2. **Context**: attach `postman_file` in full-run — Claude reads actual auth headers and example bodies to produce more accurate TCs (not 1:1 import)

### In-Memory State
All state lives in module-level dicts in `test_orchestrator.py` — no database, resets on restart:
```python
_store: dict[str, TestRunStatusResponse]
_specs: dict[str, ParsedSpec]
_plans: dict[str, TestPlan]
_scenarios_internal: dict[str, list[TestScenario]]
_test_cases_internal: dict[str, list[TestCase]]
_test_cases: dict[str, list[GeneratedTestCase]]
_run_logs: dict[str, list[str]]
_tasks: dict[str, asyncio.Task]
```
A `gc_loop()` background task (started on lifespan) evicts runs older than `run_ttl_hours` (default 24h).

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/test-runs` | Upload spec, start parsing |
| POST | `/test-runs/{run_id}/generate` | Generate TCs for a parsed run |
| POST | `/test-runs/{run_id}/execute` | Execute generated TCs |
| POST | `/test-runs/{run_id}/cancel` | Cancel a running/generating run |
| POST | `/test-runs/{run_id}/rerun` | Re-execute specific TCs or all network-error TCs |
| POST | `/test-runs/full-run` | Parse + generate + execute in one shot |
| POST | `/test-runs/import-postman` | Import Postman collection |
| POST | `/test-runs/postman-full-run` | Import Postman + execute immediately |
| GET | `/test-runs/{run_id}` | Run status |
| GET | `/test-runs/{run_id}/logs` | Event log |
| GET | `/test-runs/{run_id}/plan` | Phase 1 test plan (`?method=`, `?path=`, `?scenario_type=crud\|business`) |
| GET | `/test-runs/{run_id}/estimate` | Token cost estimate before Claude TC generation |
| GET | `/test-runs/{run_id}/results` | Execution results (`?passed=`, `?page=`, `?page_size=`, `?format=junit`) |
| GET | `/test-runs/{run_id}/test-cases` | Generated test case list |
| GET | `/test-runs/{run_id}/test-cases/{tc_id}` | Single test case |
| GET | `/test-runs/{run_id}/test-cases/{tc_id}/expected-response` | Expected response for a TC |
| GET | `/test-runs/{run_id}/stream` | SSE stream of results as they complete |

**Notable endpoint params:**
- `full-run` and Postman endpoints accept an optional `variables_file` (JSON mapping `{{variable}}` → value) to resolve Postman collection variables
- `execute` accepts `webhook_url` — POSTs the completed results payload to that URL when done
- `results` supports `?format=junit` for JUnit XML output (CI integration)

## Configuration (`config.py` / `.env`)

| Key | Default | Description |
|-----|---------|-------------|
| `ANTHROPIC_API_KEY` | required | Use `mock-*` prefix for mock mode |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | Spec parsing / document understanding |
| `CLAUDE_TC_MODEL` | `claude-haiku-4-5-20251001` | TC generation + AI validation (cheaper) |
| `ANTHROPIC_RPM` | `40` | Requests per minute (0 = disabled) |
| `TC_BATCH_DELAY_SECONDS` | `10` | Delay between TC generation batches |
| `MAX_CONCURRENT_REQUESTS` | `10` | Concurrent httpx requests during execution |
| `REQUEST_TIMEOUT_SECONDS` | `30` | Per-request timeout |
| `RUN_TTL_HOURS` | `24` | Hours before completed runs are GC'd from memory |
| `AI_VALIDATE_TIMEOUT_SECONDS` | `120` | Hard timeout for AI validation batch call |
| `MAX_RESPONSE_BODY_BYTES` | `10240` | Response body truncation before storage |

### Scenario Step Chaining
`TestCase.extract` + `{{varName}}` templates wire scenario steps together:
- `extract: {"userId": "data.id"}` — after step N executes, dot-notation path `data.id` is pulled from the response body and stored as `userId` in the scenario context
- Subsequent steps reference it via `{{userId}}` anywhere in `path_params`, `query_params`, `headers`, or `body`
- `executor.py` resolves templates at execution time via `_resolve_templates()` / `_extract_value()`

## Security Test Types

Claude tags generated TCs with `security_test_type`:
- `auth_bypass` — no/invalid auth → 4xx = PASS, 2xx = vulnerability
- `sql_injection` — SQL payloads in string fields → 500 = possible injection
- `xss` — XSS payloads → 4xx = PASS
- `idor` — another user's resource ID → 2xx = IDOR vulnerability
- `error_disclosure` — malformed input → detailed 500 = vulnerability
