"""
Reporter: stdout summary and JSON run report writer.

Implements Requirements 8.1–8.6.
"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

from swadoc.models import RunReport


def _serialize_report(report: RunReport) -> dict:
    """
    Convert a RunReport to a JSON-serializable dict.

    Enums are serialized as their .value strings.
    Path objects are serialized as strings.
    """
    raw = asdict(report)

    # Serialize enum values in each route entry
    for route in raw.get("routes", []):
        # classification: RouteClassification enum
        if route.get("classification") is not None:
            route["classification"] = route["classification"].value if hasattr(route["classification"], "value") else route["classification"]
        # enrichment_outcome: EnrichmentOutcome enum
        if route.get("enrichment_outcome") is not None:
            route["enrichment_outcome"] = route["enrichment_outcome"].value if hasattr(route["enrichment_outcome"], "value") else route["enrichment_outcome"]
        # failure_category: FailureCategory enum (optional)
        if route.get("failure_category") is not None:
            route["failure_category"] = route["failure_category"].value if hasattr(route["failure_category"], "value") else route["failure_category"]

    return raw


class Reporter:
    """Generates stdout summary and JSON run report (Requirements 8.1–8.6)."""

    def print_summary(self, report: RunReport) -> None:
        """
        Print a human-readable run summary to stdout (Req 8.1).

        Output format:
            Routes discovered: N
            Routes documented: N
            Routes enriched: N
            Validation warnings: N
            Validation errors: N
            Output path: {path}
            PR URL: {url or "none"}
        """
        s = report.summary
        output_path = report.output_path or "none"
        pr_url = report.pr_url or "none"

        print(f"Routes discovered: {s.routes_discovered}")
        print(f"Routes documented: {s.routes_documented}")
        print(f"Routes enriched: {s.routes_enriched}")
        print(f"Validation warnings: {s.validation_warnings}")
        print(f"Validation errors: {s.validation_errors}")
        print(f"Output path: {output_path}")
        print(f"PR URL: {pr_url}")

    def write_report(self, report: RunReport, path: Path = Path("./swagger-autodoc-report.json")) -> None:
        """
        Write the JSON RunReport to *path*, overwriting any existing file (Req 8.2).

        On I/O failure: print error to stderr and sys.exit(1) (Req 8.5).

        The JSON structure includes (Req 8.3, 8.4, 8.6):
          - summary: start_timestamp, duration_ms, routes_discovered,
                     routes_documented, routes_enriched, validation_warnings,
                     validation_errors
          - routes: per-route details with enum values as strings
          - dry_run: bool
          - would_change: list (only when dry_run=True)
          - output_path, pr_url
        """
        data = _serialize_report(report)

        # When not in dry-run mode, omit would_change entirely to keep the
        # report clean (it will be None anyway, but explicit omission is cleaner).
        if not report.dry_run:
            data.pop("would_change", None)

        try:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            print(
                f"Error: failed to write run report to '{path}': {exc}",
                file=sys.stderr,
            )
            sys.exit(1)
