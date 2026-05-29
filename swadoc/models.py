from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from datetime import datetime


class RouteClassification(Enum):
    DOCUMENTED = "Documented_Route"
    UNDERDOCUMENTED = "Underdocumented_Route"


class EnrichmentOutcome(Enum):
    ENRICHED = "enriched"
    SKIPPED = "skipped"
    FAILED = "failed"
    NOT_ATTEMPTED = "not_attempted"


class FailureCategory(Enum):
    TIMEOUT = "timeout"
    LLM_CALL_FAILED = "llm_call_failed"
    LLM_RESPONSE_UNPARSEABLE = "llm_response_unparseable"
    FORMAT_MISMATCH = "format_mismatch"
    PRE_WRITE_VALIDATION_FAILED = "pre_write_validation_failed"
    POST_WRITE_PARSE_FAILURE = "post_write_parse_failure"
    BACKUP_FAILURE = "backup_failure"


@dataclass
class RouteRecord:
    method: str
    uri: str
    handler: str
    handler_file: Path | None
    handler_function: str | None


@dataclass
class HandlerSource:
    content: str
    file_path: Path
    function_name: str
    start_line: int
    end_line: int
    line_count: int
    was_truncated: bool = False
    original_line_count: int | None = None


@dataclass
class AnnotationBlock:
    raw_text: str
    format: str  # "phpdoc" or "jsdoc"
    summary: str | None = None
    operation_id: str | None = None
    responses: list[dict] = field(default_factory=list)
    request_body: dict | None = None
    headers: list[dict] = field(default_factory=list)
    path_params: list[dict] = field(default_factory=list)


@dataclass
class QualityResult:
    classification: RouteClassification
    missing_fields: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class WriteResult:
    success: bool
    file_path: Path
    error: str | None = None


@dataclass
class ValidationIssue:
    level: str  # "error" or "warning"
    route_uri: str | None
    annotation_id: str | None
    message: str


@dataclass
class SpecGenerationResult:
    success: bool
    document: dict | None = None
    error: str | None = None


@dataclass
class RouteReport:
    method: str
    uri: str
    handler: str
    classification: RouteClassification
    enrichment_outcome: EnrichmentOutcome
    failure_category: FailureCategory | None = None
    failure_reason: str | None = None
    validation_warnings: list[ValidationIssue] = field(default_factory=list)
    validation_errors: list[ValidationIssue] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)


@dataclass
class RunSummary:
    start_timestamp: str  # ISO 8601 UTC
    duration_ms: int
    routes_discovered: int
    routes_documented: int
    routes_enriched: int
    validation_warnings: int
    validation_errors: int


@dataclass
class RunReport:
    summary: RunSummary
    routes: list[RouteReport]
    dry_run: bool = False
    would_change: list[dict] | None = None  # Only in dry-run mode
    output_path: str | None = None
    pr_url: str | None = None


@dataclass
class SwadocConfig:
    code_base: str  # "node" or "php"
    project_path: Path
    output_path: Path
    dry_run: bool = False
    open_pr: bool = False
    llm_provider: str | None = None  # "anthropic" or "openai"
    llm_model: str | None = None
    llm_api_key: str | None = None
    github_token: str | None = None
    github_repo: str | None = None
