"""
Unit tests for swadoc.reporting.reporter.Reporter (Requirements 8.1–8.6).
"""

import json
import sys
from pathlib import Path

import pytest

from swadoc.models import (
    EnrichmentOutcome,
    FailureCategory,
    RouteClassification,
    RouteReport,
    RunReport,
    RunSummary,
    ValidationIssue,
)
from swadoc.reporting.reporter import Reporter, _serialize_report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_summary(**kwargs) -> RunSummary:
    defaults = dict(
        start_timestamp="2025-01-01T00:00:00Z",
        duration_ms=100,
        routes_discovered=1,
        routes_documented=1,
        routes_enriched=0,
        validation_warnings=0,
        validation_errors=0,
    )
    defaults.update(kwargs)
    return RunSummary(**defaults)


def _make_route(**kwargs) -> RouteReport:
    defaults = dict(
        method="GET",
        uri="/api/test",
        handler="TestController@index",
        classification=RouteClassification.DOCUMENTED,
        enrichment_outcome=EnrichmentOutcome.NOT_ATTEMPTED,
    )
    defaults.update(kwargs)
    return RouteReport(**defaults)


def _make_report(**kwargs) -> RunReport:
    defaults = dict(
        summary=_make_summary(),
        routes=[_make_route()],
        dry_run=False,
        would_change=None,
        output_path="/out/openapi.json",
        pr_url=None,
    )
    defaults.update(kwargs)
    return RunReport(**defaults)


# ---------------------------------------------------------------------------
# print_summary tests (Req 8.1)
# ---------------------------------------------------------------------------

class TestPrintSummary:
    def test_all_fields_present(self, capsys):
        """print_summary outputs all seven required fields."""
        report = _make_report(
            summary=_make_summary(
                routes_discovered=5,
                routes_documented=3,
                routes_enriched=2,
                validation_warnings=1,
                validation_errors=0,
            ),
            output_path="/out/openapi.json",
            pr_url="https://github.com/org/repo/pull/7",
        )
        Reporter().print_summary(report)
        out = capsys.readouterr().out

        assert "Routes discovered: 5" in out
        assert "Routes documented: 3" in out
        assert "Routes enriched: 2" in out
        assert "Validation warnings: 1" in out
        assert "Validation errors: 0" in out
        assert "Output path: /out/openapi.json" in out
        assert "PR URL: https://github.com/org/repo/pull/7" in out

    def test_pr_url_none_when_absent(self, capsys):
        """PR URL shows 'none' when no PR was opened."""
        report = _make_report(pr_url=None)
        Reporter().print_summary(report)
        out = capsys.readouterr().out
        assert "PR URL: none" in out

    def test_output_path_none_when_absent(self, capsys):
        """Output path shows 'none' when not set."""
        report = _make_report(output_path=None)
        Reporter().print_summary(report)
        out = capsys.readouterr().out
        assert "Output path: none" in out

    def test_writes_to_stdout_not_stderr(self, capsys):
        """print_summary must write to stdout, not stderr."""
        Reporter().print_summary(_make_report())
        captured = capsys.readouterr()
        assert captured.out.strip() != ""
        assert captured.err == ""


# ---------------------------------------------------------------------------
# write_report tests (Req 8.2, 8.3, 8.4, 8.5, 8.6)
# ---------------------------------------------------------------------------

class TestWriteReport:
    def test_creates_json_file(self, tmp_path):
        """write_report creates a valid JSON file at the given path."""
        out = tmp_path / "report.json"
        Reporter().write_report(_make_report(), out)
        assert out.exists()
        data = json.loads(out.read_text())
        assert isinstance(data, dict)

    def test_overwrites_existing_file(self, tmp_path):
        """write_report overwrites any pre-existing file (Req 8.2)."""
        out = tmp_path / "report.json"
        out.write_text("old content")
        Reporter().write_report(_make_report(), out)
        data = json.loads(out.read_text())
        assert "summary" in data

    def test_json_indented_with_2_spaces(self, tmp_path):
        """JSON output uses indent=2."""
        out = tmp_path / "report.json"
        Reporter().write_report(_make_report(), out)
        raw = out.read_text()
        # indent=2 means lines start with exactly two spaces for first-level keys
        assert '  "summary"' in raw

    # --- Req 8.3: per-route fields ---

    def test_route_fields_present(self, tmp_path):
        """Each route entry contains all required fields (Req 8.3)."""
        route = _make_route(
            method="POST",
            uri="/api/items",
            handler="ItemController@store",
            classification=RouteClassification.UNDERDOCUMENTED,
            enrichment_outcome=EnrichmentOutcome.ENRICHED,
            failure_category=None,
            failure_reason=None,
            validation_warnings=[],
            validation_errors=[],
            missing_fields=["summary"],
        )
        out = tmp_path / "report.json"
        Reporter().write_report(_make_report(routes=[route]), out)
        data = json.loads(out.read_text())
        r = data["routes"][0]

        assert r["method"] == "POST"
        assert r["uri"] == "/api/items"
        assert r["handler"] == "ItemController@store"
        assert r["classification"] == "Underdocumented_Route"
        assert r["enrichment_outcome"] == "enriched"
        assert r["failure_category"] is None
        assert r["failure_reason"] is None
        assert r["validation_warnings"] == []
        assert r["validation_errors"] == []
        assert r["missing_fields"] == ["summary"]

    def test_enum_values_serialized_as_strings(self, tmp_path):
        """Enums are serialized as their .value strings, not enum names."""
        route = _make_route(
            classification=RouteClassification.DOCUMENTED,
            enrichment_outcome=EnrichmentOutcome.SKIPPED,
            failure_category=FailureCategory.TIMEOUT,
        )
        out = tmp_path / "report.json"
        Reporter().write_report(_make_report(routes=[route]), out)
        data = json.loads(out.read_text())
        r = data["routes"][0]

        assert r["classification"] == "Documented_Route"
        assert r["enrichment_outcome"] == "skipped"
        assert r["failure_category"] == "timeout"

    def test_validation_issues_included(self, tmp_path):
        """Validation warnings and errors are included per route."""
        warning = ValidationIssue(level="warning", route_uri="/api/test", annotation_id="op1", message="Missing example")
        error = ValidationIssue(level="error", route_uri="/api/test", annotation_id="op1", message="Invalid schema")
        route = _make_route(
            classification=RouteClassification.UNDERDOCUMENTED,
            enrichment_outcome=EnrichmentOutcome.FAILED,
            validation_warnings=[warning],
            validation_errors=[error],
        )
        out = tmp_path / "report.json"
        Reporter().write_report(_make_report(routes=[route]), out)
        data = json.loads(out.read_text())
        r = data["routes"][0]

        assert len(r["validation_warnings"]) == 1
        assert r["validation_warnings"][0]["message"] == "Missing example"
        assert len(r["validation_errors"]) == 1
        assert r["validation_errors"][0]["message"] == "Invalid schema"

    # --- Req 8.4: dry_run and would_change ---

    def test_dry_run_field_false_when_not_dry_run(self, tmp_path):
        """dry_run field is false when not in dry-run mode."""
        out = tmp_path / "report.json"
        Reporter().write_report(_make_report(dry_run=False), out)
        data = json.loads(out.read_text())
        assert data["dry_run"] is False

    def test_dry_run_field_true_and_would_change_present(self, tmp_path):
        """dry_run=True and would_change list are included in dry-run mode (Req 8.4)."""
        would_change = [{"file": "app/Http/Controllers/Foo.php", "method": "GET", "uri": "/foo"}]
        report = _make_report(dry_run=True, would_change=would_change)
        out = tmp_path / "report.json"
        Reporter().write_report(report, out)
        data = json.loads(out.read_text())

        assert data["dry_run"] is True
        assert data["would_change"] == would_change

    def test_would_change_omitted_when_not_dry_run(self, tmp_path):
        """would_change key is absent from the JSON when dry_run=False."""
        out = tmp_path / "report.json"
        Reporter().write_report(_make_report(dry_run=False, would_change=None), out)
        data = json.loads(out.read_text())
        assert "would_change" not in data

    # --- Req 8.5: I/O failure handling ---

    def test_io_failure_prints_to_stderr_and_exits(self, tmp_path, capsys):
        """On write failure, error goes to stderr and sys.exit(1) is called (Req 8.5)."""
        # Use a path inside a non-existent directory that cannot be created
        # by making the parent a file instead of a directory.
        blocker = tmp_path / "blocker"
        blocker.write_text("I am a file, not a directory")
        bad_path = blocker / "report.json"  # parent is a file → OSError

        with pytest.raises(SystemExit) as exc_info:
            Reporter().write_report(_make_report(), bad_path)

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "Error:" in err
        assert str(bad_path) in err

    # --- Req 8.6: top-level summary ---

    def test_summary_fields_present(self, tmp_path):
        """Top-level summary contains all required fields (Req 8.6)."""
        summary = _make_summary(
            start_timestamp="2025-06-01T12:00:00Z",
            duration_ms=4200,
            routes_discovered=10,
            routes_documented=7,
            routes_enriched=3,
            validation_warnings=2,
            validation_errors=1,
        )
        out = tmp_path / "report.json"
        Reporter().write_report(_make_report(summary=summary), out)
        data = json.loads(out.read_text())
        s = data["summary"]

        assert s["start_timestamp"] == "2025-06-01T12:00:00Z"
        assert s["duration_ms"] == 4200
        assert s["routes_discovered"] == 10
        assert s["routes_documented"] == 7
        assert s["routes_enriched"] == 3
        assert s["validation_warnings"] == 2
        assert s["validation_errors"] == 1

    def test_output_path_and_pr_url_in_report(self, tmp_path):
        """output_path and pr_url are included in the JSON report."""
        report = _make_report(
            output_path="/generated/openapi.json",
            pr_url="https://github.com/org/repo/pull/99",
        )
        out = tmp_path / "report.json"
        Reporter().write_report(report, out)
        data = json.loads(out.read_text())

        assert data["output_path"] == "/generated/openapi.json"
        assert data["pr_url"] == "https://github.com/org/repo/pull/99"

    def test_default_path_used_when_not_specified(self, tmp_path, monkeypatch):
        """Default path ./swagger-autodoc-report.json is used when path not given."""
        monkeypatch.chdir(tmp_path)
        Reporter().write_report(_make_report())
        default = tmp_path / "swagger-autodoc-report.json"
        assert default.exists()
        json.loads(default.read_text())  # must be valid JSON
