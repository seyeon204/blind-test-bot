import asyncio
import base64
import collections
import json
import logging
import random
import re
import types as _types
import anthropic
from app.config import settings

logger = logging.getLogger(__name__)


def _is_mock() -> bool:
    return settings.anthropic_api_key.lower().startswith("mock")


def _use_claude_cli() -> bool:
    return settings.llm_provider == "claude-cli"


def _mock_message(tool_name: str, input_data: dict):
    block = _types.SimpleNamespace(type="tool_use", name=tool_name, input=input_data)
    return _types.SimpleNamespace(content=[block])


_MOCK_RESPONSES: dict[str, dict] = {
    "extract_api_spec": {
        "base_url": None,
        "endpoints": [
            {
                "method": "GET",
                "path": "/mock-endpoint",
                "summary": "[MOCK] Placeholder — set a real ANTHROPIC_API_KEY to parse the actual document",
                "parameters": [],
                "request_body_schema": None,
                "expected_responses": {"200": {"description": "OK"}},
                "security_schemes": [],
            }
        ],
    },
    "generate_test_cases": {
        "endpoints": [
            {
                "endpoint_method": "GET",
                "endpoint_path": "/mock-endpoint",
                "test_cases": [
                    {
                        "description": "[MOCK] Happy path — set a real ANTHROPIC_API_KEY for real test cases",
                        "path_params": {},
                        "query_params": {},
                        "headers": {},
                        "body": None,
                        "expected_status_codes": [200],
                        "expected_body_contains": {},
                    }
                ],
            }
        ]
    },
    # validate_results: IDs are dynamic per run — mock returns empty list,
    # each TC falls through to heuristic validation individually.
    "validate_results": {"results": []},
    "create_test_plan": {
        "individual_tests": [
            {
                "method": "GET",
                "path": "/mock-endpoint",
                "planned_cases": [
                    {"description": "[MOCK] Happy path — set a real ANTHROPIC_API_KEY for real test planning"},
                    {"description": "[MOCK] Auth bypass", "test_type": "auth_bypass"},
                ],
            }
        ],
        "scenarios": [],
    },
    "generate_scenario_steps": {"scenarios": []},
}

_client: anthropic.AsyncAnthropic | None = None

# Sliding-window rate limiter — tracks all call timestamps in the last 60 s.
_rate_lock = asyncio.Lock()
_call_times: collections.deque[float] = collections.deque()


async def _rate_limit() -> None:
    """Block until sending a new request stays within the RPM cap."""
    rpm = settings.anthropic_rpm
    if rpm <= 0:
        return
    async with _rate_lock:
        while True:
            now = asyncio.get_event_loop().time()
            while _call_times and _call_times[0] <= now - 60.0:
                _call_times.popleft()
            if len(_call_times) < rpm:
                break
            sleep_for = _call_times[0] + 60.0 - now + 0.05
            await asyncio.sleep(sleep_for)
        _call_times.append(asyncio.get_event_loop().time())


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=0)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


async def _with_retry(coro_factory, max_retries: int, label: str):
    """Run coro_factory() with retry on rate-limit (429) and overloaded (529) errors."""
    for attempt in range(max_retries):
        try:
            return await coro_factory()
        except anthropic.RateLimitError:
            if attempt == max_retries - 1:
                raise
            wait = min(30 * (2 ** attempt), 300) + random.uniform(0, 10)
            logger.warning("[claude_client] %s rate limit, waiting %.0fs (retry %d/%d)", label, wait, attempt + 1, max_retries)
            await asyncio.sleep(wait)
        except anthropic.InternalServerError as e:
            if attempt == max_retries - 1:
                raise
            wait = min(15 * (2 ** attempt), 120) + random.uniform(0, 5)
            logger.warning("[claude_client] %s overloaded (%s), waiting %.0fs (retry %d/%d)", label, e, wait, attempt + 1, max_retries)
            await asyncio.sleep(wait)
    raise RuntimeError("unreachable")


async def chat_with_tools(
    system: str,
    user: str,
    tools: list[dict],
    tool_choice: dict | None = None,
    max_retries: int = 5,
    model: str | None = None,
    thinking: dict | None = None,
    cache_system: bool = False,
    skip_rate_limit: bool = False,
    max_tokens: int = 8192,
) -> anthropic.types.Message:
    if _is_mock():
        tool_name = tool_choice["name"] if tool_choice else tools[0]["name"]
        logger.info("[claude_client] MOCK mode — returning stub for tool=%s", tool_name)
        return _mock_message(tool_name, _MOCK_RESPONSES.get(tool_name, {}))

    if _use_claude_cli():
        return await _chat_with_tools_cli(
            system=system,
            user=user,
            tools=tools,
            tool_choice=tool_choice,
            max_retries=max_retries,
        )

    client = get_client()
    system_param: str | list = (
        [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if cache_system
        else system
    )
    kwargs: dict = {
        "model": model or settings.claude_model,
        "max_tokens": max_tokens,
        "system": system_param,
        "messages": [{"role": "user", "content": user}],
        "tools": tools,
    }
    if tool_choice:
        kwargs["tool_choice"] = tool_choice
    if thinking:
        kwargs["thinking"] = thinking

    if not skip_rate_limit:
        await _rate_limit()

    async def _stream():
        async with client.messages.stream(**kwargs) as stream:
            return await stream.get_final_message()

    return await _with_retry(_stream, max_retries, "chat_with_tools")


async def _chat_with_tools_cli(
    system: str,
    user: str,
    tools: list[dict],
    tool_choice: dict | None,
    max_retries: int,
) -> _types.SimpleNamespace:
    """Claude CLI subprocess — uses Pro subscription, no API key needed."""
    tool_name = tool_choice["name"] if tool_choice else tools[0]["name"]
    tool_def = next(t for t in tools if t["name"] == tool_name)
    schema_str = json.dumps(tool_def["input_schema"], indent=2)

    prompt = (
        f"{system}\n\n"
        f"{user}\n\n"
        f"CRITICAL: Respond with ONLY a valid JSON object matching the schema below. "
        f"No explanation, no markdown code blocks, no extra text — just raw JSON.\n\n"
        f"Schema:\n{schema_str}"
    )

    import os
    import time
    # Unset CLAUDECODE to allow subprocess claude calls when running inside Claude Code
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    for attempt in range(max_retries):
        try:
            logger.info("[claude_client] CLI → tool=%s (attempt %d/%d) ...", tool_name, attempt + 1, max_retries)
            t0 = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                "claude", "-p", prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            elapsed = time.monotonic() - t0
            text = stdout.decode().strip()

            if proc.returncode != 0:
                err = stderr.decode().strip()
                raise RuntimeError(f"CLI exit code {proc.returncode}: {err[:200]}")

            # Strip markdown code fences if present
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

            data = json.loads(text)
            logger.info("[claude_client] CLI ✓ tool=%s (%.1fs)", tool_name, elapsed)
            block = _types.SimpleNamespace(type="tool_use", name=tool_name, input=data)
            return _types.SimpleNamespace(content=[block])

        except (json.JSONDecodeError, asyncio.TimeoutError, Exception) as e:
            if attempt == max_retries - 1:
                logger.error("[claude_client] CLI ✗ tool=%s failed after %d attempts: %s", tool_name, max_retries, e)
                raise
            wait = 2 * (attempt + 1)
            logger.warning("[claude_client] CLI tool=%s attempt %d/%d failed (%s), retry in %.0fs", tool_name, attempt + 1, max_retries, e, wait)
            await asyncio.sleep(wait)
    raise RuntimeError("unreachable")


async def chat_with_tools_pdf(
    pdf_bytes: bytes,
    system: str,
    tools: list[dict],
    tool_choice: dict | None = None,
    max_retries: int = 5,
    cache_system: bool = False,
) -> anthropic.types.Message:
    """Like chat_with_tools but sends a PDF as a native document content block."""
    if _is_mock():
        tool_name = tool_choice["name"] if tool_choice else tools[0]["name"]
        logger.info("[claude_client] MOCK mode — returning stub for tool=%s (pdf)", tool_name)
        return _mock_message(tool_name, _MOCK_RESPONSES.get(tool_name, {}))

    if _use_claude_cli():
        raise NotImplementedError(
            "chat_with_tools_pdf should not be called in CLI mode — "
            "document_parser handles PDF via pypdf text extraction."
        )

    client = get_client()
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
    system_param: str | list = (
        [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if cache_system
        else system
    )
    kwargs: dict = {
        "model": settings.claude_model,
        "max_tokens": 8192,
        "system": system_param,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_b64,
                    },
                },
                {
                    "type": "text",
                    "text": "Extract all API endpoints from this PDF document.",
                },
            ],
        }],
        "tools": tools,
    }
    if tool_choice:
        kwargs["tool_choice"] = tool_choice

    await _rate_limit()

    async def _stream():
        async with client.messages.stream(**kwargs) as stream:
            return await stream.get_final_message()

    return await _with_retry(_stream, max_retries, "chat_with_tools_pdf")


async def simple_chat(system: str, user: str) -> str:
    if _is_mock():
        return "[MOCK] Set a real ANTHROPIC_API_KEY for actual responses."

    client = get_client()
    response = await client.messages.create(
        model=settings.claude_model,
        max_tokens=8192,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return response.content[0].text
