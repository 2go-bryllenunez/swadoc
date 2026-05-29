"""Pipeline orchestrator for Swadoc.

Wires the full pipeline: discover → evaluate → enrich → write →
generate → validate → report → (optional PR).

Requirements: 9.1–9.4
"""

from __future__ import annotations

import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from swadoc.adapters.protocol import AdapterOperationError, AdapterProtocol
from swadoc.config import ConfigError, validate_github_config, validate_llm_config
from swadoc.enrichment.enricher import EnrichmentError, LLMEnricher
from swadoc.enrichment.llm_client import LLMClient, LLMClientFactory
from swadoc.git.pr_manager import BranchCollisionError, PRManager
from swadoc.models import (
    AnnotationBlock,
    EnrichmentOutcome,
    FailureCategory,
    HandlerSource,
    RouteClassification,
    RouteRecord,
    RouteReport,
    RunReport,
    RunSummary,
    SwadocConfig,
    ValidationIssue,
)
from swadoc.pipeline.backup import BackupError, BackupManager
from swadoc.pipeline.quality_checker import AnnotationQualityChecker
from swadoc.pipeline.spec_generator import SpecGenerationError, SpecGenerator
from swadoc.pipeline.spec_validator import SpecValidator
from swadoc.pipeline.writer import AnnotationWriter
from swadoc.reporting.reporter import Reporter

logger = logging.getLogger(__name__)


class PipelineOrchestrator:
    """Coordinates the full Swadoc pipeline execution.

    Args:
        config: Fully-validated SwadocConfig.
        adapter: The selected framework adapter.
        llm_client: Optional pre-built LLM client (used in tests). When None,
            the client is built from config at enrichment time.
    """

    def __init__(
        self,
        config: SwadocConfig,
        adapter: AdapterProtocol,
        llm_client: LLMClient | None = None,
    ) -> None:
        self._config = config
        self._adapter = adapter
        self._llm_client = llm_client

    async def run(self) -> RunReport:
        """Execute the full pipeline and return a RunReport.

        Raises SystemExit with a non-zero code on fatal errors (validation
        errors, spec generation failure, unhandled exceptions).
        """
        start_time = datetime.now(timezone.utc)
        start_ts = start_time.isoformat(timespec="seconds").replace("+00:00", "Z")

        route_reports: list[RouteReport] = []
        output_path_str: str | None = None
        pr_url: str | None = None
        would_change: list[dict] | None = [] if self._config.dry_run else None

        try:
            # ------------------------------------------------------------------
            # 1. Discover routes
            # ------------------------------------------------------------------
            logger.info("Discovering routes in %s", self._config.project_path)
            try:
                routes = self._adapter.discover_routes(self._config.project_path)
            except AdapterOperationError as exc:
                print(f"Error: route discovery failed: {exc}", file=sys.stderr)
                raise SystemExit(1)

            logger.info("Discovered %d route(s)", len(routes))

            # ------------------------------------------------------------------
            # 2. Evaluate quality
            # ------------------------------------------------------------------
            checker = AnnotationQualityChecker()
            quality_map: dict[str, tuple] = {}  # route key → (route, quality, handler_src, annotation)

            for route in routes:
                handler_src: HandlerSource | None = None
                annotation: AnnotationBlock | None = None

                try:
                    handler_src = self._adapter.get_handler_source(route, self._config.project_path)
                except AdapterOperationError as exc:
                    logger.warning("Could not read handler source for %s %s: %s", route.method, route.uri, exc)

                try:
                    annotation = self._adapter.get_existing_annotation(route, self._config.project_path)
                except AdapterOperationError as exc:
                    logger.warning("Could not read annotation for %s %s: %s", route.method, route.uri, exc)

                quality = checker.evaluate(route, annotation, handler_src)
                key = f"{route.method}:{route.uri}"
                quality_map[key] = (route, quality, handler_src, annotation)

            # ------------------------------------------------------------------
            # 3. Enrich underdocumented routes
            # ------------------------------------------------------------------
            underdocumented: list[tuple[RouteRecord, HandlerSource, AnnotationBlock | None]] = []
            documented_routes: list[RouteRecord] = []

            for key, (route, quality, handler_src, annotation) in quality_map.items():
                if quality.classification == RouteClassification.DOCUMENTED:
                    documented_routes.append(route)
                elif handler_src is not None:
                    underdocumented.append((route, handler_src, annotation))
                else:
                    # Underdocumented but no handler source — record as failed
                    route_reports.append(RouteReport(
                        method=route.method,
                        uri=route.uri,
                        handler=route.handler,
                        classification=RouteClassification.UNDERDOCUMENTED,
                        enrichment_outcome=EnrichmentOutcome.FAILED,
                        failure_category=None,
                        failure_reason="handler source unavailable",
                        missing_fields=quality.missing_fields,
                    ))

            # Add documented routes to report
            for route in documented_routes:
                route_reports.append(RouteReport(
                    method=route.method,
                    uri=route.uri,
                    handler=route.handler,
                    classification=RouteClassification.DOCUMENTED,
                    enrichment_outcome=EnrichmentOutcome.NOT_ATTEMPTED,
                ))

            # Enrich underdocumented routes
            enriched_annotations: dict[str, AnnotationBlock] = {}

            if underdocumented:
                # Validate LLM config before first call (Req 1.8)
                try:
                    validate_llm_config(self._config)
                except ConfigError as exc:
                    print(f"Error: {exc}", file=sys.stderr)
                    raise SystemExit(1)

                # Build LLM client if not injected
                if self._llm_client is None:
                    self._llm_client = LLMClientFactory.create(
                        provider=self._config.llm_provider,
                        model=self._config.llm_model,
                        api_key=self._config.llm_api_key,
                        aws_access_key_id=self._config.aws_access_key_id,
                        aws_secret_access_key=self._config.aws_secret_access_key,
                        aws_region=self._config.aws_region,
                    )

                # Determine annotation format from adapter
                adapter_format = "phpdoc" if self._config.code_base == "php" else "jsdoc"
                enricher = LLMEnricher(self._llm_client, adapter_format)

                logger.info("Enriching %d underdocumented route(s)", len(underdocumented))
                results = await enricher.enrich_batch(underdocumented)

                for route, annotation_result, error in results:
                    key = f"{route.method}:{route.uri}"
                    _, quality, _, _ = quality_map[key]

                    if error is None and annotation_result is not None:
                        enriched_annotations[key] = annotation_result
                    else:
                        # Map error to failure category
                        failure_cat: FailureCategory | None = None
                        failure_reason: str | None = None
                        outcome = EnrichmentOutcome.FAILED

                        if isinstance(error, EnrichmentError):
                            failure_cat = error.failure_category
                            failure_reason = error.reason
                            if failure_cat == FailureCategory.TIMEOUT:
                                outcome = EnrichmentOutcome.SKIPPED
                        elif error is not None:
                            failure_reason = str(error)

                        route_reports.append(RouteReport(
                            method=route.method,
                            uri=route.uri,
                            handler=route.handler,
                            classification=RouteClassification.UNDERDOCUMENTED,
                            enrichment_outcome=outcome,
                            failure_category=failure_cat,
                            failure_reason=failure_reason,
                            missing_fields=quality.missing_fields,
                        ))

            # ------------------------------------------------------------------
            # 4. Write annotations
            # ------------------------------------------------------------------
            backup_manager = BackupManager(self._config.project_path, dry_run=self._config.dry_run)
            writer = AnnotationWriter(self._adapter, backup_manager, dry_run=self._config.dry_run)

            for key, annotation in enriched_annotations.items():
                route, quality, _, _ = quality_map[key]

                if self._config.dry_run:
                    # Record what would change
                    if would_change is not None:
                        would_change.append({
                            "file": str(route.handler_file) if route.handler_file else None,
                            "method": route.method,
                            "uri": route.uri,
                        })
                    route_reports.append(RouteReport(
                        method=route.method,
                        uri=route.uri,
                        handler=route.handler,
                        classification=RouteClassification.UNDERDOCUMENTED,
                        enrichment_outcome=EnrichmentOutcome.ENRICHED,
                        missing_fields=quality.missing_fields,
                    ))
                    continue

                try:
                    write_result = writer.write(route, annotation, self._config.project_path)
                except BackupError as exc:
                    print(f"Error: backup failed, aborting run: {exc}", file=sys.stderr)
                    raise SystemExit(1)

                if write_result.success:
                    route_reports.append(RouteReport(
                        method=route.method,
                        uri=route.uri,
                        handler=route.handler,
                        classification=RouteClassification.UNDERDOCUMENTED,
                        enrichment_outcome=EnrichmentOutcome.ENRICHED,
                        missing_fields=quality.missing_fields,
                    ))
                else:
                    route_reports.append(RouteReport(
                        method=route.method,
                        uri=route.uri,
                        handler=route.handler,
                        classification=RouteClassification.UNDERDOCUMENTED,
                        enrichment_outcome=EnrichmentOutcome.FAILED,
                        failure_category=FailureCategory.POST_WRITE_PARSE_FAILURE,
                        failure_reason=write_result.error,
                        missing_fields=quality.missing_fields,
                    ))

            # ------------------------------------------------------------------
            # 5. Generate spec (skip in dry-run)
            # ------------------------------------------------------------------
            validation_issues: list[ValidationIssue] = []

            if not self._config.dry_run:
                spec_gen = SpecGenerator(self._adapter)
                try:
                    spec_gen.generate(self._config.project_path, self._config.output_path)
                    output_path_str = str(self._config.output_path.resolve())
                except SpecGenerationError as exc:
                    print(f"Error: spec generation failed: {exc}", file=sys.stderr)
                    raise SystemExit(1)

                # ------------------------------------------------------------------
                # 6. Validate spec
                # ------------------------------------------------------------------
                validator = SpecValidator()
                # Read the generated document for validation
                try:
                    import json
                    import yaml
                    ext = self._config.output_path.suffix.lower()
                    content = self._config.output_path.read_text(encoding="utf-8")
                    document = json.loads(content) if ext == ".json" else yaml.safe_load(content)
                    validation_issues = validator.validate(document, self._config.output_path)
                except Exception as exc:
                    logger.warning("Could not read generated spec for validation: %s", exc)

                # Attach validation issues to route reports
                for issue in validation_issues:
                    for rr in route_reports:
                        if issue.route_uri and rr.uri == issue.route_uri:
                            if issue.level == "error":
                                rr.validation_errors.append(issue)
                            else:
                                rr.validation_warnings.append(issue)
                            break

            # ------------------------------------------------------------------
            # 7. Build RunReport
            # ------------------------------------------------------------------
            end_time = datetime.now(timezone.utc)
            duration_ms = int((end_time - start_time).total_seconds() * 1000)

            routes_enriched = sum(
                1 for rr in route_reports
                if rr.enrichment_outcome == EnrichmentOutcome.ENRICHED
            )
            routes_documented = sum(
                1 for rr in route_reports
                if rr.classification == RouteClassification.DOCUMENTED
            )
            val_errors = sum(1 for i in validation_issues if i.level == "error")
            val_warnings = sum(1 for i in validation_issues if i.level == "warning")

            summary = RunSummary(
                start_timestamp=start_ts,
                duration_ms=duration_ms,
                routes_discovered=len(routes),
                routes_documented=routes_documented,
                routes_enriched=routes_enriched,
                validation_warnings=val_warnings,
                validation_errors=val_errors,
            )

            report = RunReport(
                summary=summary,
                routes=route_reports,
                dry_run=self._config.dry_run,
                would_change=would_change,
                output_path=output_path_str,
                pr_url=pr_url,
            )

            # ------------------------------------------------------------------
            # 8. PR creation (optional)
            # ------------------------------------------------------------------
            if self._config.open_pr and not self._config.dry_run:
                try:
                    validate_github_config(self._config)
                except ConfigError as exc:
                    print(f"Error: {exc}", file=sys.stderr)
                    raise SystemExit(1)

                pr_manager = PRManager(
                    github_token=self._config.github_token,
                    github_repo=self._config.github_repo,
                    project_path=self._config.project_path,
                    dry_run=False,
                )
                ts = start_time.strftime("%Y%m%dT%H%M%SZ")
                try:
                    branch = pr_manager.create_branch(ts)
                    modified_files = [
                        Path(rr.handler) for rr in route_reports
                        if rr.enrichment_outcome == EnrichmentOutcome.ENRICHED
                        and rr.handler
                    ]
                    pr_manager.commit_changes(modified_files, routes_enriched)
                    description = _build_pr_description(report)
                    pr_url = await pr_manager.open_pr(branch, description)
                    report.pr_url = pr_url
                except BranchCollisionError as exc:
                    print(f"Error: {exc}", file=sys.stderr)
                    raise SystemExit(1)
                except Exception as exc:
                    print(f"Error: PR creation failed: {exc}", file=sys.stderr)
                    raise SystemExit(1)

            # ------------------------------------------------------------------
            # 9. Report
            # ------------------------------------------------------------------
            reporter = Reporter()
            reporter.print_summary(report)
            reporter.write_report(report, Path("./swagger-autodoc-report.json"))

            # Exit non-zero if validation errors (Req 9.2)
            if val_errors > 0:
                print(f"Error: {val_errors} validation error(s) found.", file=sys.stderr)
                raise SystemExit(1)

            return report

        except SystemExit:
            raise
        except KeyboardInterrupt:
            print("\nInterrupted by user.", file=sys.stderr)
            raise SystemExit(1)
        except Exception as exc:
            # Req 9.4: unhandled exception — emit to stderr, clean up partial output
            print(f"\nUnhandled exception: {type(exc).__name__}: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            # Clean up partial output file
            try:
                if self._config.output_path.exists():
                    self._config.output_path.unlink()
            except OSError:
                pass
            raise SystemExit(1)


def _build_pr_description(report: RunReport) -> str:
    """Build the PR description from the run report (Req 7.5)."""
    lines: list[str] = ["## Swadoc Auto-Documentation Run\n"]

    enriched = [r for r in report.routes if r.enrichment_outcome == EnrichmentOutcome.ENRICHED]
    skipped = [r for r in report.routes if r.enrichment_outcome == EnrichmentOutcome.SKIPPED]
    warnings = [i for r in report.routes for i in r.validation_warnings]

    if enriched:
        lines.append("### Enriched Routes\n")
        for r in enriched:
            lines.append(f"- `{r.method} {r.uri}` — {r.handler}\n")

    if skipped:
        lines.append("\n### Skipped Routes\n")
        for r in skipped:
            reason = r.failure_reason or str(r.failure_category)
            lines.append(f"- `{r.method} {r.uri}` — {reason}\n")

    if warnings:
        lines.append("\n### Validation Warnings\n")
        for w in warnings:
            lines.append(f"- {w.route_uri}: {w.message}\n")

    if report.output_path:
        lines.append(f"\n### Generated Spec\n`{report.output_path}`\n")

    return "".join(lines)
