"""
Annotation Quality Checker for Swadoc.

Evaluates whether a route's existing AnnotationBlock meets the documentation
quality criteria defined in Requirements 3.1–3.6.
"""

import re
from swadoc.models import (
    AnnotationBlock,
    HandlerSource,
    QualityResult,
    RouteClassification,
    RouteRecord,
)


# ---------------------------------------------------------------------------
# Patterns for detecting request body access in handler source
# ---------------------------------------------------------------------------

# Static body access: req.body, $request->input(...), request.body, request.input(...)
_BODY_STATIC_PATTERNS = [
    re.compile(r"\breq\.body\b"),
    re.compile(r"\$request->input\s*\("),
    re.compile(r"\brequest\.body\b"),
    re.compile(r"\brequest\.input\s*\("),
]

# Dynamic body access: req.body[var], $request->input($var), request.body[var], request.input($var)
_BODY_DYNAMIC_PATTERNS = [
    re.compile(r"\breq\.body\s*\[\s*\w+\s*\]"),
    re.compile(r"\$request->input\s*\(\s*\$\w+"),
    re.compile(r"\brequest\.body\s*\[\s*\w+\s*\]"),
    re.compile(r"\brequest\.input\s*\(\s*\$\w+"),
]

# ---------------------------------------------------------------------------
# Patterns for detecting request header access in handler source
# ---------------------------------------------------------------------------

# Static header access: req.headers, $request->header(...), request.header(...)
_HEADER_STATIC_PATTERNS = [
    re.compile(r"\breq\.headers\b"),
    re.compile(r"\$request->header\s*\("),
    re.compile(r"\brequest\.header\s*\("),
]

# Dynamic header access: req.headers[var], $request->header($var), request.header($var)
_HEADER_DYNAMIC_PATTERNS = [
    re.compile(r"\breq\.headers\s*\[\s*\w+\s*\]"),
    re.compile(r"\$request->header\s*\(\s*\$\w+"),
    re.compile(r"\brequest\.header\s*\(\s*\$\w+"),
]

# ---------------------------------------------------------------------------
# Path parameter placeholder patterns in URIs
# ---------------------------------------------------------------------------

# Matches {param} (Laravel/AdonisJS style) or :param (Express style).
# The negative lookbehind avoids matching "://" (protocol separators) while
# still matching "/:param" which is the standard Express route segment form.
_PATH_PARAM_BRACE = re.compile(r"\{(\w+)\}")
_PATH_PARAM_COLON = re.compile(r"(?<!:):(\w+)")


def _extract_path_params(uri: str) -> list[str]:
    """Return a list of path parameter names found in the URI."""
    params: list[str] = []
    params.extend(_PATH_PARAM_BRACE.findall(uri))
    params.extend(_PATH_PARAM_COLON.findall(uri))
    return params


def _reads_body(source: str) -> bool:
    """Return True if the handler source contains any static body-read pattern."""
    return any(p.search(source) for p in _BODY_STATIC_PATTERNS)


def _reads_headers(source: str) -> bool:
    """Return True if the handler source contains any static header-read pattern."""
    return any(p.search(source) for p in _HEADER_STATIC_PATTERNS)


def _find_dynamic_accesses(
    source: str, route: RouteRecord
) -> list[str]:
    """
    Scan handler source for dynamic property access patterns.

    Returns a list of warning strings, each identifying the route, the
    approximate source location (line number), and the matched expression.
    """
    warnings: list[str] = []
    all_dynamic = _BODY_DYNAMIC_PATTERNS + _HEADER_DYNAMIC_PATTERNS
    lines = source.splitlines()
    for lineno, line in enumerate(lines, start=1):
        for pattern in all_dynamic:
            match = pattern.search(line)
            if match:
                expr = match.group(0)
                warnings.append(
                    f"Dynamic property access in route {route.method} {route.uri} "
                    f"at line {lineno}: {expr!r}"
                )
    return warnings


def _is_valid_status_code(code: int | str) -> bool:
    """Return True if code is an integer in the HTTP status range 100–599."""
    try:
        return 100 <= int(code) <= 599
    except (TypeError, ValueError):
        return False


class AnnotationQualityChecker:
    """
    Evaluates annotation completeness against the quality criteria in
    Requirements 3.1–3.6.

    Usage::

        checker = AnnotationQualityChecker()
        result = checker.evaluate(route, annotation, handler_source)
    """

    def evaluate(
        self,
        route: RouteRecord,
        annotation: AnnotationBlock | None,
        handler_source: HandlerSource | None,
    ) -> QualityResult:
        """
        Classify a route as Documented_Route or Underdocumented_Route.

        Parameters
        ----------
        route:
            The normalised route record (method, uri, handler, …).
        annotation:
            The parsed annotation block, or None if absent.
        handler_source:
            The extracted handler source, or None when the file/function
            could not be located.

        Returns
        -------
        QualityResult
            classification, missing_fields, and any warnings.
        """
        missing: list[str] = []
        warnings: list[str] = []

        # ------------------------------------------------------------------
        # Requirement 3.2 – missing handler file or function
        # ------------------------------------------------------------------
        if handler_source is None or route.handler_file is None or route.handler_function is None:
            missing_handler = route.handler or f"{route.method} {route.uri}"
            warnings.append(
                f"Handler source unavailable for route {route.method} {route.uri}: "
                f"handler={route.handler!r}, file={route.handler_file!r}, "
                f"function={route.handler_function!r}"
            )
            return QualityResult(
                classification=RouteClassification.UNDERDOCUMENTED,
                missing_fields=["handler_source"],
                warnings=warnings,
            )

        source_text = handler_source.content

        # ------------------------------------------------------------------
        # Requirement 3.5 – dynamic property access warnings (always checked)
        # ------------------------------------------------------------------
        warnings.extend(_find_dynamic_accesses(source_text, route))

        # ------------------------------------------------------------------
        # Requirement 3.4 – absent or structurally empty annotation
        # ------------------------------------------------------------------
        if annotation is None:
            missing.append("annotation")
            return QualityResult(
                classification=RouteClassification.UNDERDOCUMENTED,
                missing_fields=missing,
                warnings=warnings,
            )

        # ------------------------------------------------------------------
        # Requirement 3.3a – non-empty summary
        # ------------------------------------------------------------------
        if not annotation.summary or not annotation.summary.strip():
            missing.append("summary")

        # ------------------------------------------------------------------
        # Requirement 3.3b – non-empty operationId
        # ------------------------------------------------------------------
        if not annotation.operation_id or not annotation.operation_id.strip():
            missing.append("operation_id")

        # ------------------------------------------------------------------
        # Requirement 3.3c – at least one response with valid status + description
        # ------------------------------------------------------------------
        has_valid_response = any(
            _is_valid_status_code(r.get("status_code", r.get("status", "")))
            and bool(r.get("description", "").strip())
            for r in annotation.responses
        )
        if not has_valid_response:
            missing.append("responses")

        # ------------------------------------------------------------------
        # Requirement 3.3d – request body documented when handler reads body
        # ------------------------------------------------------------------
        if _reads_body(source_text) and annotation.request_body is None:
            missing.append("request_body")

        # ------------------------------------------------------------------
        # Requirement 3.3e – header entries when handler reads headers
        # ------------------------------------------------------------------
        if _reads_headers(source_text) and not annotation.headers:
            missing.append("headers")

        # ------------------------------------------------------------------
        # Requirement 3.3f – path param entries for every URI placeholder
        # ------------------------------------------------------------------
        uri_params = _extract_path_params(route.uri)
        if uri_params:
            documented_params = {
                (p.get("name") or "").strip().lower()
                for p in annotation.path_params
            }
            for param in uri_params:
                if param.lower() not in documented_params:
                    missing.append(f"path_param:{param}")

        # ------------------------------------------------------------------
        # Final classification
        # ------------------------------------------------------------------
        if missing:
            return QualityResult(
                classification=RouteClassification.UNDERDOCUMENTED,
                missing_fields=missing,
                warnings=warnings,
            )

        return QualityResult(
            classification=RouteClassification.DOCUMENTED,
            missing_fields=[],
            warnings=warnings,
        )
