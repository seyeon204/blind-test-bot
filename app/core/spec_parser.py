"""Detects spec format and routes to the appropriate parser."""
from __future__ import annotations

import io
import json
import logging
import re
import yaml

logger = logging.getLogger(__name__)

from app.core.swagger_parser import parse_swagger
from app.core.document_parser import parse_document
from app.models.internal import ParsedSpec


def _is_swagger_or_openapi(raw: str | bytes) -> bool:
    text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
    try:
        data = yaml.safe_load(text)
        if isinstance(data, dict) and ("openapi" in data or "swagger" in data):
            return True
    except Exception:
        pass
    try:
        data = json.loads(text)
        if isinstance(data, dict) and ("openapi" in data or "swagger" in data):
            return True
    except Exception:
        pass
    return False


_MAX_PDF_TEXT_CHARS = 18_000  # ~4,500 tokens — leaves room for prompt + TC gen calls


def _is_pdf(raw: bytes, filename: str) -> bool:
    return filename.lower().endswith(".pdf") or raw[:4] == b"%PDF"


# Matches lines that look API-related (HTTP methods, paths, parameters, Korean API terms)
_API_LINE_RE = re.compile(
    r"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)"
    r"|/[a-zA-Z0-9_\-{}][a-zA-Z0-9_\-{}/]*"   # URL path
    r"|(요청|응답|파라미터|헤더|바디|필수|선택|타입|형식|설명|예시)"  # Korean API terms
    r"|(request|response|param|header|body|required|schema|type|description|example)"
    r"|\b[1-5]\d{2}\b"              # HTTP status codes (200, 404, etc.)
    r"|(Content-Type|Authorization|Bearer|application/json)",
    re.IGNORECASE,
)
_PAGE_NUM_RE = re.compile(r"^\s*[-–]?\s*\d+\s*[-–]?\s*$")  # lone page numbers


def _compress_pdf_text(text: str) -> str:
    """Filter PDF text to API-relevant lines only, removing noise."""
    lines = text.split("\n")
    kept: list[str] = []
    prev_blank = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Drop lone page numbers
        if _PAGE_NUM_RE.match(stripped):
            continue

        if not stripped:
            if not prev_blank:
                kept.append("")
            prev_blank = True
            continue

        prev_blank = False

        # Always keep lines near an API-relevant line (±1 context)
        if _API_LINE_RE.search(stripped):
            # include previous line as context if not already added
            if kept and kept[-1] == "" and i > 0:
                prev_stripped = lines[i - 1].strip()
                if prev_stripped and not _PAGE_NUM_RE.match(prev_stripped):
                    kept[-1] = prev_stripped  # replace blank with context line
            kept.append(stripped)
        elif kept and kept[-1] != "":
            # keep one trailing context line after a relevant line
            kept.append(stripped)

    return "\n".join(kept)


def _extract_pdf_text(raw: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(raw))
    pages = [page.extract_text() or "" for page in reader.pages]
    raw_text = "\n\n".join(pages)
    logger.info("[spec_parser] PDF raw text: %d chars", len(raw_text))

    text = _compress_pdf_text(raw_text)
    logger.info("[spec_parser] PDF after compression: %d chars", len(text))

    if len(text) > _MAX_PDF_TEXT_CHARS:
        logger.warning("[spec_parser] PDF text still too long, truncating %d → %d chars", len(text), _MAX_PDF_TEXT_CHARS)
        text = text[:_MAX_PDF_TEXT_CHARS]
    return text


async def parse_spec(raw: str | bytes, filename: str = "") -> ParsedSpec:
    """
    Auto-detect format and parse.
    - If the content looks like OpenAPI/Swagger → swagger_parser
    - If the content is a PDF → extract text via pypdf, then document_parser (Claude)
    - Otherwise → document_parser (Claude)
    """
    raw_bytes = raw if isinstance(raw, bytes) else raw.encode("utf-8")

    # PDF detection — extract text to keep token count low
    if _is_pdf(raw_bytes, filename):
        logger.info("[spec_parser] PDF detected, extracting text...")
        text = _extract_pdf_text(raw_bytes)
        logger.info("[spec_parser] PDF text: %d chars, sending to document_parser", len(text))
        return await parse_document(text)

    # Hint from filename extension
    lower_name = filename.lower()
    if lower_name.endswith((".yaml", ".yml", ".json")):
        if _is_swagger_or_openapi(raw):
            return parse_swagger(raw)

    # Content-based detection for any extension
    if _is_swagger_or_openapi(raw):
        return parse_swagger(raw)

    # Fallback: treat as plain document, use Claude
    return await parse_document(raw)
