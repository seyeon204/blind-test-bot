"""Parses plain-text API documents (markdown, text, etc.) via Claude API."""
from __future__ import annotations

import logging
from app.models.internal import EndpointSpec, ParameterSpec, ParsedSpec
from app.utils.claude_client import chat_with_tools, chat_with_tools_pdf
from app.utils.exceptions import SpecParseError

logger = logging.getLogger(__name__)

_EXTRACT_TOOL = {
    "name": "extract_api_spec",
    "description": "Extract all API endpoints from the given API documentation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "base_url": {
                "type": "string",
                "description": "Base URL of the API if mentioned, otherwise null",
            },
            "endpoints": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "method": {"type": "string", "description": "HTTP method: GET, POST, PUT, PATCH, DELETE"},
                        "path": {"type": "string", "description": "URL path, e.g. /users/{id}"},
                        "summary": {"type": "string", "description": "Short description of this endpoint"},
                        "parameters": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "location": {"type": "string", "enum": ["path", "query", "header", "cookie"]},
                                    "required": {"type": "boolean"},
                                    "schema": {
                                        "type": "object",
                                        "description": "JSON Schema for this parameter",
                                    },
                                    "example": {},
                                },
                                "required": ["name", "location", "required"],
                            },
                        },
                        "request_body_schema": {
                            "type": "object",
                            "description": "JSON Schema for the request body, null if none",
                        },
                        "expected_responses": {
                            "type": "object",
                            "description": "Map of status_code (string) to {schema, description}",
                            "additionalProperties": {
                                "type": "object",
                                "properties": {
                                    "schema": {"type": "object"},
                                    "description": {"type": "string"},
                                },
                            },
                        },
                        "security_schemes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of security scheme names required (e.g. bearerAuth)",
                        },
                    },
                    "required": ["method", "path", "summary"],
                },
            },
        },
        "required": ["endpoints"],
    },
}

_SYSTEM = """You are an API specification analyst.
Given an API document (markdown, plain text, or PDF-extracted text), extract every HTTP endpoint
and produce structured JSON by calling the extract_api_spec tool.

Rules:
- Infer field types and constraints from the description if not explicit.
- Use JSON Schema format for all schema fields.
- If the base URL is not mentioned, omit it (null).
- Include all mentioned error response codes.
- Never invent endpoints that are not in the document.

Important: The input may be text extracted from a PDF, so formatting may be garbled —
table columns may be merged, whitespace may be irregular, and Korean/English may be mixed.
Look for patterns like "GET /path", "POST /path", HTTP method names, URL paths starting with /,
and parameter names even if they appear in unexpected positions. Extract all endpoints you can identify."""


async def parse_document(raw: str | bytes) -> ParsedSpec:
    """Parse a plain-text API document into ParsedSpec using Claude."""
    text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")

    response = await chat_with_tools(
        system=_SYSTEM,
        user=f"Here is the API documentation:\n\n{text}",
        tools=[_EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "extract_api_spec"},
        cache_system=True,
    )

    # Find the tool_use block
    tool_use = next(
        (block for block in response.content if block.type == "tool_use"),
        None,
    )
    if not tool_use:
        raise SpecParseError("Claude did not return a structured extraction result")

    data: dict = tool_use.input
    logger.info("[document_parser] base_url=%s, endpoints found=%d", data.get("base_url"), len(data.get("endpoints", [])))
    endpoints: list[EndpointSpec] = []

    for ep in data.get("endpoints", []):
        params = [
            ParameterSpec(
                name=p["name"],
                location=p["location"],
                required=p.get("required", False),
                schema=p.get("schema", {}),
                example=p.get("example"),
            )
            for p in ep.get("parameters", [])
        ]
        endpoints.append(EndpointSpec(
            method=ep["method"].upper(),
            path=ep["path"],
            summary=ep.get("summary", ""),
            parameters=params,
            request_body_schema=ep.get("request_body_schema"),
            expected_responses=ep.get("expected_responses", {}),
            security_schemes=ep.get("security_schemes", []),
        ))

    return ParsedSpec(
        source_format="document",
        base_url=data.get("base_url"),
        endpoints=endpoints,
        raw_text=text,
    )


def _extract_pdf_text(raw: bytes) -> str:
    """Extract plain text from PDF bytes using pypdf."""
    import io
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(raw))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages)


async def parse_pdf_document(raw: bytes) -> ParsedSpec:
    """Parse a PDF by sending it directly to Claude as a native document block.
    In Ollama mode, text is extracted via pypdf and sent as plain text instead."""
    from app.utils.claude_client import _use_claude_cli
    if _use_claude_cli():
        logger.info("[document_parser] Ollama mode — extracting PDF text via pypdf")
        text = _extract_pdf_text(raw)
        if not text.strip():
            raise SpecParseError("PDF text extraction returned empty content")
        return await parse_document(text)

    response = await chat_with_tools_pdf(
        pdf_bytes=raw,
        system=_SYSTEM,
        tools=[_EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "extract_api_spec"},
        cache_system=True,
    )

    tool_use = next(
        (block for block in response.content if block.type == "tool_use"),
        None,
    )
    if not tool_use:
        raise SpecParseError("Claude did not return a structured extraction result for PDF")

    data: dict = tool_use.input
    logger.info("[document_parser] PDF: base_url=%s, endpoints found=%d", data.get("base_url"), len(data.get("endpoints", [])))
    endpoints: list[EndpointSpec] = []

    for ep in data.get("endpoints", []):
        params = [
            ParameterSpec(
                name=p["name"],
                location=p["location"],
                required=p.get("required", False),
                schema=p.get("schema", {}),
                example=p.get("example"),
            )
            for p in ep.get("parameters", [])
        ]
        endpoints.append(EndpointSpec(
            method=ep["method"].upper(),
            path=ep["path"],
            summary=ep.get("summary", ""),
            parameters=params,
            request_body_schema=ep.get("request_body_schema"),
            expected_responses=ep.get("expected_responses", {}),
            security_schemes=ep.get("security_schemes", []),
        ))

    return ParsedSpec(
        source_format="document",
        base_url=data.get("base_url"),
        endpoints=endpoints,
        raw_text="<pdf>",
    )
