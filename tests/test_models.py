"""Unit tests for swadoc.models — dataclasses and enums.

Validates: Requirements 2.8, 8.3
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swadoc.models import (
    AnnotationBlock,
    EnrichmentOutcome,
    FailureCategory,
    HandlerSource,
    QualityResult,
    RouteClassification,
    RouteRecord,
    RouteReport,
    RunReport,
    RunSummary,
    SpecGenerationResult,
    SwadocConfig,
    ValidationIssue,
    WriteResult,
)


# ---------------------------------------------------------------------------
# Enum value tests
# ---------------------------------------------------------------------------


class TestRouteClassification:
    def test_documented_value(self):
        assert RouteClassification.DOCUMENTED.value == "Documented_Route"

    def test_underdocumented_value(self):
        assert RouteClassification.UNDERDOCUMENTED.value == "Underdocumented_Route"

    def test_enum_members(self):
        members = {m.value for m in RouteClassification}
        assert members == {"Documented_Route", "Underdocumented_Route"}


class TestEnrichmentOutcome:
    def test_enriched_value(self):
        assert EnrichmentOutcome.ENRICHED.value == "enriched"

    def test_skipped_value(self):
        assert EnrichmentOutcome.SKIPPED.value == "skipped"

    def test_failed_value(self):
        assert EnrichmentOutcome.FAILED.value == "failed"

    def test_not_attempted_value(self):
        assert EnrichmentOutcome.NOT_ATTEMPTED.value == "not_attempted"


class TestFailureCategory:
    def test_timeout_value(self):
        assert FailureCategory.TIMEOUT.value == "timeout"

    def test_llm_call_failed_value(self):
        assert FailureCategory.LLM_CALL_FAILED.value == "llm_call_failed"

    def test_llm_response_unparseable_value(self):
        assert FailureCategory.LLM_RESPONSE_UNPARSEABLE.value == "llm_response_unparseable"

    def test_format_mismatch_value(self):
        assert FailureCategory.FORMAT_MISMATCH.value == "format_mismatch"

    def test_pre_write_validation_failed_value(self):
        assert FailureCategory.PRE_WRITE_VALIDATION_FAILED.value == "pre_write_validation_failed"

    def test_post_write_parse_failure_value(self):
        assert FailureCategory.POST_WRITE_PARSE_FAILURE.value == "post_write_parse_failure"

    def test_backup_failure_value(self):
        assert FailureCategory.BACKUP_FAILURE.value == "backup_failure"


# ---------------------------------------------------------------------------
# Dataclass instantiation tests
# ---------------------------------------------------------------------------


class TestRouteRecord:
    def test_basic_instantiation(self):
        record = RouteRecord(
            method="GET",
            uri="/api/users",
            handler="UserController@index",
            handler_file=Path("app/Http/Controllers/UserController.php"),
            handler_function="index",
        )
        assert record.method == "GET"
        assert record.uri == "/api/users"
        assert record.handler == "UserController@index"
        assert record.handler_file == Path("app/Http/Controllers/UserController.php")
        assert record.handler_function == "index"

    def test_nullable_fields(self):
        record = RouteRecord(
            method="POST",
            uri="/api/items",
            handler="ItemController@store",
            handler_file=None,
            handler_function=None,
        )
        assert record.handler_file is None
        assert record.handler_function is None


class TestHandlerSource:
    def test_basic_instantiation(self):
        source = HandlerSource(
            content="function index() { return []; }",
            file_path=Path("app/Http/Controllers/UserController.php"),
            function_name="index",
            start_line=10,
            end_line=20,
            line_count=11,
        )
        assert source.content == "function index() { return []; }"
        assert source.start_line == 10
        assert source.end_line == 20
        assert source.line_count == 11
        assert source.was_truncated is False
        assert source.original_line_count is None

    def test_truncated_source(self):
        source = HandlerSource(
            content="...",
            file_path=Path("app/Http/Controllers/UserController.php"),
            function_name="index",
            start_line=1,
            end_line=200,
            line_count=200,
            was_truncated=True,
            original_line_count=500,
        )
        assert source.was_truncated is True
        assert source.original_line_count == 500


class TestAnnotationBlock:
    def test_basic_instantiation(self):
        block = AnnotationBlock(raw_text="/** @OA\\Get */", format="phpdoc")
        assert block.raw_text == "/** @OA\\Get */"
        assert block.format == "phpdoc"
        assert block.summary is None
        assert block.operation_id is None
        assert block.responses == []
        assert block.request_body is None
        assert block.headers == []
        assert block.path_params == []

    def test_full_instantiation(self):
        block = AnnotationBlock(
            raw_text="/** @OA\\Get */",
            format="jsdoc",
            summary="Get all users",
            operation_id="getUsers",
            responses=[{"status": 200, "description": "OK"}],
            request_body={"content": "application/json"},
            headers=[{"name": "Authorization"}],
            path_params=[{"name": "id"}],
        )
        assert block.summary == "Get all users"
        assert block.operation_id == "getUsers"
        assert len(block.responses) == 1
        assert block.request_body is not None
        assert len(block.headers) == 1
        assert len(block.path_params) == 1


class TestQualityResult:
    def test_documented_route(self):
        result = QualityResult(classification=RouteClassification.DOCUMENTED)
        assert result.classification == RouteClassification.DOCUMENTED
        assert result.missing_fields == []
        assert result.warnings == []

    def test_underdocumented_with_missing_fields(self):
        result = QualityResult(
            classification=RouteClassification.UNDERDOCUMENTED,
            missing_fields=["summary", "operationId"],
            warnings=["Dynamic header access detected"],
        )
        assert result.classification == RouteClassification.UNDERDOCUMENTED
        assert "summary" in result.missing_fields
        assert "operationId" in result.missing_fields
        assert len(result.warnings) == 1


class TestWriteResult:
    def test_success(self):
        result = WriteResult(success=True, file_path=Path("app/Controller.php"))
        assert result.success is True
        assert result.error is None

    def test_failure(self):
        result = WriteResult(
            success=False,
            file_path=Path("app/Controller.php"),
            error="Syntax error after write",
        )
        assert result.success is False
        assert result.error == "Syntax error after write"


class TestValidationIssue:
    def test_error_issue(self):
        issue = ValidationIssue(
            level="error",
            route_uri="/api/users",
            annotation_id="getUsers",
            message="Missing required field: responses",
        )
        assert issue.level == "error"
        assert issue.route_uri == "/api/users"
        assert issue.annotation_id == "getUsers"

    def test_warning_issue_nullable_fields(self):
        issue = ValidationIssue(
            level="warning",
            route_uri=None,
            annotation_id=None,
            message="Deprecated field used",
        )
        assert issue.route_uri is None
        assert issue.annotation_id is None


class TestSpecGenerationResult:
    def test_success(self):
        result = SpecGenerationResult(success=True, document={"openapi": "3.0.0"})
        assert result.success is True
        assert result.document == {"openapi": "3.0.0"}
        assert result.error is None

    def test_failure(self):
        result = SpecGenerationResult(success=False, error="Generator crashed")
        assert result.success is False
        assert result.document is None
        assert result.error == "Generator crashed"


class TestRouteReport:
    def test_basic_instantiation(self):
        report = RouteReport(
            method="GET",
            uri="/api/users",
            handler="UserController@index",
            classification=RouteClassification.DOCUMENTED,
            enrichment_outcome=EnrichmentOutcome.NOT_ATTEMPTED,
        )
        assert report.method == "GET"
        assert report.failure_category is None
        assert report.failure_reason is None
        assert report.validation_warnings == []
        assert report.validation_errors == []
        assert report.missing_fields == []

    def test_failed_route(self):
        report = RouteReport(
            method="POST",
            uri="/api/items",
            handler="ItemController@store",
            classification=RouteClassification.UNDERDOCUMENTED,
            enrichment_outcome=EnrichmentOutcome.FAILED,
            failure_category=FailureCategory.TIMEOUT,
            failure_reason="LLM call timed out after 30s",
        )
        assert report.enrichment_outcome == EnrichmentOutcome.FAILED
        assert report.failure_category == FailureCategory.TIMEOUT
        assert report.failure_reason == "LLM call timed out after 30s"


class TestRunSummary:
    def test_instantiation(self):
        summary = RunSummary(
            start_timestamp="2024-01-15T10:00:00Z",
            duration_ms=5000,
            routes_discovered=10,
            routes_documented=7,
            routes_enriched=3,
            validation_warnings=1,
            validation_errors=0,
        )
        assert summary.start_timestamp == "2024-01-15T10:00:00Z"
        assert summary.duration_ms == 5000
        assert summary.routes_discovered == 10
        assert summary.routes_documented == 7
        assert summary.routes_enriched == 3
        assert summary.validation_warnings == 1
        assert summary.validation_errors == 0


class TestRunReport:
    def test_basic_instantiation(self):
        summary = RunSummary(
            start_timestamp="2024-01-15T10:00:00Z",
            duration_ms=1000,
            routes_discovered=5,
            routes_documented=5,
            routes_enriched=0,
            validation_warnings=0,
            validation_errors=0,
        )
        report = RunReport(summary=summary, routes=[])
        assert report.dry_run is False
        assert report.would_change is None
        assert report.output_path is None
        assert report.pr_url is None

    def test_dry_run_report(self):
        summary = RunSummary(
            start_timestamp="2024-01-15T10:00:00Z",
            duration_ms=500,
            routes_discovered=3,
            routes_documented=1,
            routes_enriched=0,
            validation_warnings=0,
            validation_errors=0,
        )
        report = RunReport(
            summary=summary,
            routes=[],
            dry_run=True,
            would_change=[{"file": "app/Controller.php", "method": "GET", "uri": "/api/users"}],
        )
        assert report.dry_run is True
        assert report.would_change is not None
        assert len(report.would_change) == 1


class TestSwadocConfig:
    def test_basic_instantiation(self):
        config = SwadocConfig(
            code_base="php",
            project_path=Path("/var/www/myapp"),
            output_path=Path("./openapi.json"),
        )
        assert config.code_base == "php"
        assert config.project_path == Path("/var/www/myapp")
        assert config.output_path == Path("./openapi.json")
        assert config.dry_run is False
        assert config.open_pr is False
        assert config.llm_provider is None
        assert config.llm_model is None
        assert config.llm_api_key is None
        assert config.github_token is None
        assert config.github_repo is None

    def test_full_instantiation(self):
        config = SwadocConfig(
            code_base="node",
            project_path=Path("/var/www/nodeapp"),
            output_path=Path("./api-docs.yaml"),
            dry_run=True,
            open_pr=True,
            llm_provider="anthropic",
            llm_model="claude-3-5-sonnet-20241022",
            llm_api_key="sk-ant-test",
            github_token="ghp_test",
            github_repo="owner/repo",
        )
        assert config.code_base == "node"
        assert config.dry_run is True
        assert config.open_pr is True
        assert config.llm_provider == "anthropic"
        assert config.llm_model == "claude-3-5-sonnet-20241022"
        assert config.github_token == "ghp_test"
        assert config.github_repo == "owner/repo"
