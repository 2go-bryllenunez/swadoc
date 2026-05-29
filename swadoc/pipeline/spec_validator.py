"""
SpecValidator: validates a generated OpenAPI document against the OpenAPI 3.0.x schema.

Responsibilities:
- Validate the document using openapi-spec-validator.
- Collect validation errors as ValidationIssue(level="error", ...).
- Collect validation warnings as ValidationIssue(level="warning", ...).
- Return a list of all ValidationIssue objects.
- The caller (orchestrator) decides whether to exit non-zero based on error count (Req 6.6).
- Warnings-only → continue execution (Req 6.7).
"""

import logging
from pathlib import Path

from openapi_spec_validator import validate as _validate_spec
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError

from swadoc.models import ValidationIssue

logger = logging.getLogger(__name__)


def _extract_route_uri(error: Exception) -> str | None:
    """
    Attempt to extract a route URI from the error context.

    openapi-spec-validator errors may carry a ``context`` attribute or
    an ``absolute_path`` deque that points to the offending location in
    the document.  We try to surface a path-like string from those
    attributes so callers can associate the issue with a specific route.
    """
    # Try absolute_path (jsonschema ValidationError attribute)
    absolute_path = getattr(error, "absolute_path", None)
    if absolute_path:
        try:
            parts = list(absolute_path)
            if parts:
                return "/".join(str(p) for p in parts)
        except Exception:
            pass

    # Try context (list of sub-errors)
    context = getattr(error, "context", None)
    if context:
        for sub in context:
            uri = _extract_route_uri(sub)
            if uri:
                return uri

    return None


def _extract_annotation_id(error: Exception) -> str | None:
    """
    Attempt to extract an annotation/operation identifier from the error.

    Looks for an ``operationId`` or similar key in the error's schema_path
    or absolute_path.
    """
    schema_path = getattr(error, "schema_path", None)
    if schema_path:
        try:
            parts = list(schema_path)
            for part in parts:
                if isinstance(part, str) and part.lower() in ("operationid", "operation_id"):
                    return part
        except Exception:
            pass

    return None


class SpecValidator:
    """
    Validates a generated OpenAPI document against the OpenAPI 3.0.x schema.

    Usage::

        validator = SpecValidator()
        issues = validator.validate(document, output_path)
        errors = [i for i in issues if i.level == "error"]
        if errors:
            # exit non-zero (Req 6.6)
            ...
        # warnings-only → continue (Req 6.7)
    """

    def validate(self, document: dict, output_path: Path) -> list[ValidationIssue]:
        """
        Validate *document* against the OpenAPI 3.0.x schema.

        Parameters
        ----------
        document:
            The parsed OpenAPI document as a Python dict.
        output_path:
            The path where the document was written (used for log messages).

        Returns
        -------
        list[ValidationIssue]
            A list of ValidationIssue objects.  Each issue has:
            - ``level``: ``"error"`` or ``"warning"``
            - ``route_uri``: the offending route URI when extractable, else ``None``
            - ``annotation_id``: the offending operationId when extractable, else ``None``
            - ``message``: the validator-provided error description

        Notes
        -----
        - Validation errors map to ``level="error"`` (Req 6.5–6.6).
        - Non-fatal issues (warnings) map to ``level="warning"`` (Req 6.7).
        - The caller is responsible for deciding whether to exit non-zero
          based on the presence of error-level issues (Req 6.6).
        """
        issues: list[ValidationIssue] = []

        logger.info("Validating OpenAPI document at: %s", output_path)

        try:
            _validate_spec(document)
            logger.info("OpenAPI document is valid: %s", output_path)

        except OpenAPIValidationError as exc:
            # The top-level exception may wrap multiple sub-errors via its
            # ``context`` attribute (jsonschema behaviour).  We unpack them
            # so each distinct violation becomes its own ValidationIssue.
            sub_errors = list(getattr(exc, "context", []))

            if sub_errors:
                for sub in sub_errors:
                    route_uri = _extract_route_uri(sub)
                    annotation_id = _extract_annotation_id(sub)
                    message = str(sub.message) if hasattr(sub, "message") else str(sub)
                    logger.error(
                        "OpenAPI validation error (route=%s, annotation=%s): %s",
                        route_uri,
                        annotation_id,
                        message,
                    )
                    issues.append(
                        ValidationIssue(
                            level="error",
                            route_uri=route_uri,
                            annotation_id=annotation_id,
                            message=message,
                        )
                    )
            else:
                # Single top-level error with no sub-errors
                route_uri = _extract_route_uri(exc)
                annotation_id = _extract_annotation_id(exc)
                message = str(exc.message) if hasattr(exc, "message") else str(exc)
                logger.error(
                    "OpenAPI validation error (route=%s, annotation=%s): %s",
                    route_uri,
                    annotation_id,
                    message,
                )
                issues.append(
                    ValidationIssue(
                        level="error",
                        route_uri=route_uri,
                        annotation_id=annotation_id,
                        message=message,
                    )
                )

        except Exception as exc:
            # Unexpected exception during validation — treat as a single error
            message = f"Unexpected error during OpenAPI validation: {exc}"
            logger.error(message)
            issues.append(
                ValidationIssue(
                    level="error",
                    route_uri=None,
                    annotation_id=None,
                    message=message,
                )
            )

        # Log a summary
        error_count = sum(1 for i in issues if i.level == "error")
        warning_count = sum(1 for i in issues if i.level == "warning")
        if error_count or warning_count:
            logger.info(
                "Validation complete: %d error(s), %d warning(s)",
                error_count,
                warning_count,
            )

        return issues
