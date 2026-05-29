"""LLM enrichment pipeline for Swadoc.

Provides :class:`LLMEnricher` which builds prompts, calls the configured LLM,
parses the response into an :class:`AnnotationBlock`, and validates it before
handing it off to the annotation writer.

Requirements: 4.1–4.12
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from swadoc.enrichment.llm_client import (
    LLMAuthError,
    LLMClient,
    LLMNetworkError,
    LLMProviderError,
    LLMTimeoutError,
)
from swadoc.models import (
    AnnotationBlock,
    FailureCategory,
    HandlerSource,
    RouteRecord,
)

logger = logging.getLogger(__name__)

# Maximum number of handler source lines sent to the LLM (Req 4.7)
_MAX_HANDLER_LINES = 200

# Timeout applied to every LLM call in seconds (Req 4.4)
_LLM_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class EnrichmentError(Exception):
    """Raised when LLM enrichment fails for a route.

    Attributes:
        failure_category: Structured :class:`FailureCategory` value that
            identifies the kind of failure (timeout, unparseable response, …).
        reason: Human-readable description of the failure.
    """

    def __init__(self, failure_category: FailureCategory, reason: str) -> None:
        super().__init__(reason)
        self.failure_category = failure_category
        self.reason = reason

    def __repr__(self) -> str:
        return (
            f"EnrichmentError(failure_category={self.failure_category!r}, "
            f"reason={self.reason!r})"
        )


# ---------------------------------------------------------------------------
# Prompt building helpers
# ---------------------------------------------------------------------------


def _route_id(route: RouteRecord) -> str:
    """Return a short human-readable route identifier for logging."""
    return f"{route.method.upper()} {route.uri}"


def _truncate_handler_source(
    handler_source: HandlerSource, route: RouteRecord
) -> str:
    """Return handler source content, truncated to the last 200 lines if needed.

    When truncation occurs a warning is logged with the route identifier and
    the original line count (Req 4.7).
    """
    lines = handler_source.content.splitlines()
    if len(lines) <= _MAX_HANDLER_LINES:
        return handler_source.content

    original_count = len(lines)
    truncated = "\n".join(lines[-_MAX_HANDLER_LINES:])
    logger.warning(
        "Handler source for route %s truncated from %d lines to last %d lines "
        "(route id: %s)",
        _route_id(route),
        original_count,
        _MAX_HANDLER_LINES,
        _route_id(route),
    )
    return truncated


def _build_prompt(
    route: RouteRecord,
    handler_source: HandlerSource,
    existing_annotation: AnnotationBlock | None,
    adapter_format: str,
) -> str:
    """Build the LLM prompt for a single route (Req 4.1, 4.2).

    The prompt includes:
    - HTTP method and URI
    - Middleware list (derived from route.handler metadata when available)
    - Handler source code (truncated to last 200 lines if needed)
    - Existing annotation block (if any)
    - Format-specific instructions (PHPDoc vs JSDoc)
    - Explicit instructions for body fields, headers, responses, bearerAuth
    - Instruction to return ONLY the annotation with no markdown fences
    """
    source_content = _truncate_handler_source(handler_source, route)

    # Middleware: stored in route.handler as a comma-separated string when
    # adapters populate it; fall back to empty list gracefully.
    middleware_info = ""
    if route.handler and "middleware" in route.handler.lower():
        middleware_info = f"Middleware: {route.handler}\n"
    else:
        middleware_info = "Middleware: (none detected)\n"

    existing_block = ""
    if existing_annotation and existing_annotation.raw_text.strip():
        existing_block = (
            f"\n## Existing Annotation\n"
            f"The following annotation already exists for this route. "
            f"Improve or complete it:\n\n"
            f"{existing_annotation.raw_text}\n"
        )

    if adapter_format == "phpdoc":
        format_name = "PHPDoc"
        format_example = (
            "/**\n"
            " * @OA\\Get(\n"
            " *     path=\"/example\",\n"
            " *     summary=\"...\",\n"
            " *     ...\n"
            " * )\n"
            " */"
        )
        format_instruction = (
            "Emit a PHPDoc block compatible with L5-Swagger (OpenAPI 3.0). "
            "Use @OA\\<Method> annotations. "
            "Do NOT include markdown code fences (``` or ~~~) or any prose outside the comment block."
        )
    else:
        # jsdoc (AdonisJS / Express)
        format_name = "JSDoc"
        format_example = (
            "/**\n"
            " * @openapi\n"
            " * /example:\n"
            " *   get:\n"
            " *     summary: ...\n"
            " *     ...\n"
            " */"
        )
        format_instruction = (
            "Emit a JSDoc block compatible with swagger-jsdoc (OpenAPI 3.0). "
            "Use the @openapi tag with YAML content inside the comment. "
            "Do NOT include markdown code fences (``` or ~~~) or any prose outside the comment block."
        )

    prompt = f"""You are an OpenAPI documentation expert. Generate a complete {format_name} annotation block for the following API route.

## Route Information
HTTP Method: {route.method.upper()}
URI: {route.uri}
Handler: {route.handler or "(unknown)"}
{middleware_info}
## Handler Source Code
```
{source_content}
```
{existing_block}
## Instructions
1. Document ALL request body fields when the handler reads from the request body.
2. Document ALL request header fields when the handler reads specific headers.
3. Infer response shapes from return statements, response() calls, and serializer usage in the handler source.
4. Use the `bearerAuth` security scheme when authentication or auth middleware is detected.
5. Include a non-empty `summary` and a unique `operationId`.
6. Include at least one response entry with an HTTP status code and description.
7. Document path parameters for every placeholder in the URI (e.g., {{id}}, :id).
8. {format_instruction}

## Output Format
Return ONLY the {format_name} annotation block. No explanations, no markdown fences, no surrounding prose.
The output must start with `/**` and end with `*/`.

Example structure:
{format_example}
"""
    return prompt


# ---------------------------------------------------------------------------
# Response parsing helpers
# ---------------------------------------------------------------------------


def _extract_annotation_text(raw_response: str) -> str | None:
    """Extract the doc-comment block from the LLM response.

    Accepts responses that start directly with ``/**`` or that have the block
    embedded in the text. Returns None when no valid block is found.
    """
    if not raw_response or not raw_response.strip():
        return None

    text = raw_response.strip()

    # Find the first /** ... */ block
    start = text.find("/**")
    if start == -1:
        return None

    end = text.find("*/", start + 3)
    if end == -1:
        return None

    return text[start : end + 2]


def _detect_annotation_format(annotation_text: str) -> str | None:
    """Detect whether an annotation block is PHPDoc or JSDoc.

    Returns ``"phpdoc"``, ``"jsdoc"``, or ``None`` when the format cannot be
    determined.
    """
    if not annotation_text:
        return None

    # PHPDoc: contains @OA\ annotations (L5-Swagger style)
    if re.search(r"@OA\\", annotation_text):
        return "phpdoc"

    # JSDoc: contains @openapi or @swagger tag
    if re.search(r"@openapi|@swagger", annotation_text):
        return "jsdoc"

    # Heuristic: YAML-style path definitions inside a comment → JSDoc
    if re.search(r"^\s*\*\s+/\S+:", annotation_text, re.MULTILINE):
        return "jsdoc"

    return None


def _parse_annotation_block(
    annotation_text: str, expected_format: str
) -> AnnotationBlock:
    """Parse a raw annotation string into an :class:`AnnotationBlock`.

    Performs lightweight extraction of summary, operationId, and response
    entries from the raw text. Full structural parsing is left to the
    framework's native tooling (L5-Swagger / swagger-jsdoc).
    """
    # Extract summary
    summary: str | None = None
    summary_match = re.search(
        r"summary[:\s]+[\"']?([^\n\"'*]+)[\"']?", annotation_text, re.IGNORECASE
    )
    if summary_match:
        summary = summary_match.group(1).strip()

    # Extract operationId
    operation_id: str | None = None
    op_match = re.search(
        r"operationId[:\s]+[\"']?([^\n\"'*,\s]+)[\"']?",
        annotation_text,
        re.IGNORECASE,
    )
    if op_match:
        operation_id = op_match.group(1).strip()

    # Extract response status codes (rough heuristic)
    responses: list[dict] = []
    for m in re.finditer(
        r"(?:response|@OA\\Response)[^\n]*?(\d{3})", annotation_text, re.IGNORECASE
    ):
        code = int(m.group(1))
        if 100 <= code <= 599:
            responses.append({"status_code": code})

    return AnnotationBlock(
        raw_text=annotation_text,
        format=expected_format,
        summary=summary,
        operation_id=operation_id,
        responses=responses,
    )


# ---------------------------------------------------------------------------
# Pre-write parseability validation (Req 4.11, 4.12)
# ---------------------------------------------------------------------------


def _validate_parseability(annotation: AnnotationBlock) -> bool:
    """Validate that the annotation block is structurally parseable.

    This is a lightweight pre-write check that confirms:
    - The raw text is a well-formed doc-comment (starts with /** ends with */)
    - For PHPDoc: at least one @OA\\ tag is present
    - For JSDoc: the @openapi or @swagger tag is present

    Full validation by L5-Swagger / swagger-jsdoc happens after the spec is
    generated (Req 6.5). This check guards against obviously malformed output
    before touching any source file (Req 4.11, 4.12).
    """
    text = annotation.raw_text.strip()

    if not text.startswith("/**") or not text.endswith("*/"):
        return False

    if annotation.format == "phpdoc":
        # Must contain at least one L5-Swagger OA annotation
        return bool(re.search(r"@OA\\", text))

    if annotation.format == "jsdoc":
        # Must contain @openapi or @swagger tag
        return bool(re.search(r"@openapi|@swagger", text))

    return False


# ---------------------------------------------------------------------------
# LLMEnricher
# ---------------------------------------------------------------------------


class LLMEnricher:
    """Builds prompts, calls the LLM, and returns validated :class:`AnnotationBlock` objects.

    Args:
        llm_client: An :class:`LLMClient` implementation (Anthropic or OpenAI).
        adapter_format: ``"phpdoc"`` for Laravel, ``"jsdoc"`` for AdonisJS/Express.
    """

    def __init__(self, llm_client: LLMClient, adapter_format: str) -> None:
        if adapter_format not in ("phpdoc", "jsdoc"):
            raise ValueError(
                f"adapter_format must be 'phpdoc' or 'jsdoc', got {adapter_format!r}"
            )
        self._llm_client = llm_client
        self._adapter_format = adapter_format

    async def enrich(
        self,
        route: RouteRecord,
        handler_source: HandlerSource,
        existing_annotation: AnnotationBlock | None,
    ) -> AnnotationBlock:
        """Enrich a single route by calling the LLM and parsing the response.

        Args:
            route: The route to enrich.
            handler_source: The handler's source code.
            existing_annotation: Any existing annotation block, or None.

        Returns:
            A validated :class:`AnnotationBlock` ready for the annotation writer.

        Raises:
            EnrichmentError: When the LLM call fails, times out, returns an
                unparseable response, or the response format does not match the
                expected adapter format.
        """
        prompt = _build_prompt(
            route, handler_source, existing_annotation, self._adapter_format
        )

        # --- Call the LLM (Req 4.3, 4.4) ---
        try:
            response = await self._llm_client.complete(prompt, timeout=_LLM_TIMEOUT)
        except LLMTimeoutError as exc:
            # Req 4.5
            raise EnrichmentError(
                FailureCategory.TIMEOUT,
                f"LLM call timed out after {_LLM_TIMEOUT}s for route {_route_id(route)}: {exc}",
            ) from exc
        except (LLMNetworkError, LLMAuthError, LLMProviderError) as exc:
            # Req 4.6
            raise EnrichmentError(
                FailureCategory.LLM_CALL_FAILED,
                f"LLM call failed for route {_route_id(route)}: {exc}",
            ) from exc

        # --- Parse the response (Req 4.9) ---
        annotation_text = _extract_annotation_text(response.content)
        if not annotation_text:
            raise EnrichmentError(
                FailureCategory.LLM_RESPONSE_UNPARSEABLE,
                f"LLM returned empty or unparseable response for route {_route_id(route)}",
            )

        # --- Detect and validate format (Req 4.8, 4.10) ---
        detected_format = _detect_annotation_format(annotation_text)
        if detected_format is None:
            # Cannot determine format — treat as unparseable
            raise EnrichmentError(
                FailureCategory.LLM_RESPONSE_UNPARSEABLE,
                f"Could not detect annotation format in LLM response for route {_route_id(route)}",
            )

        if detected_format != self._adapter_format:
            raise EnrichmentError(
                FailureCategory.FORMAT_MISMATCH,
                (
                    f"LLM returned {detected_format!r} annotation but adapter expects "
                    f"{self._adapter_format!r} for route {_route_id(route)}"
                ),
            )

        # --- Build the AnnotationBlock ---
        annotation = _parse_annotation_block(annotation_text, self._adapter_format)

        # --- Pre-write parseability validation (Req 4.11, 4.12) ---
        if not _validate_parseability(annotation):
            raise EnrichmentError(
                FailureCategory.PRE_WRITE_VALIDATION_FAILED,
                f"Annotation block failed pre-write parseability validation for route {_route_id(route)}",
            )

        return annotation

    async def enrich_batch(
        self,
        routes: list[tuple[RouteRecord, HandlerSource, AnnotationBlock | None]],
    ) -> list[tuple[RouteRecord, AnnotationBlock | None, Exception | None]]:
        """Enrich multiple routes concurrently using :func:`asyncio.gather`.

        Args:
            routes: A list of ``(route, handler_source, existing_annotation)``
                tuples to enrich.

        Returns:
            A list of ``(route, annotation, error)`` tuples in the same order
            as the input. When enrichment succeeds ``error`` is ``None``.
            When enrichment fails ``annotation`` is ``None`` and ``error``
            holds the :class:`EnrichmentError` (or other exception).
        """

        async def _safe_enrich(
            route: RouteRecord,
            handler_source: HandlerSource,
            existing_annotation: AnnotationBlock | None,
        ) -> tuple[RouteRecord, AnnotationBlock | None, Exception | None]:
            try:
                annotation = await self.enrich(route, handler_source, existing_annotation)
                return (route, annotation, None)
            except Exception as exc:  # noqa: BLE001
                return (route, None, exc)

        tasks = [
            _safe_enrich(route, handler_source, existing_annotation)
            for route, handler_source, existing_annotation in routes
        ]
        results = await asyncio.gather(*tasks)
        return list(results)
