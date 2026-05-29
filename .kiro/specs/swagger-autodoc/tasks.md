# Implementation Plan: Swagger Auto-Documentation (Swadoc)

## Overview

This plan implements the Swadoc CLI tool as a pipeline-based Python application targeting Python 3.14+. The implementation follows the architecture defined in the design: a plugin-based adapter system, subprocess-isolated route discovery, LLM provider abstraction, backup-before-write safety, and structured reporting. Tasks are ordered to build foundational components first (data models, interfaces, configuration) then layer on adapters, enrichment, writing, spec generation, and finally PR management and reporting.

## Tasks

- [x] 1. Set up project structure and core data models
  - [x] 1.1 Create package structure and install dependencies
    - Create `swadoc/` package directory with `__init__.py`
    - Create subpackages: `adapters/`, `enrichment/`, `pipeline/`, `reporting/`, `git/`
    - Add dependencies to `pyproject.toml`: `click`, `python-dotenv`, `httpx`, `pyyaml`, `openapi-spec-validator`
    - Set up `pytest` and `pytest-asyncio` as dev dependencies
    - _Requirements: 1.1–1.7, 9.1_

  - [x] 1.2 Implement data models and enums
    - Create `swadoc/models.py` with all dataclasses: `RouteRecord`, `HandlerSource`, `AnnotationBlock`, `QualityResult`, `WriteResult`, `ValidationIssue`, `SpecGenerationResult`, `RouteReport`, `RunSummary`, `RunReport`, `SwadocConfig`
    - Create enums: `RouteClassification`, `EnrichmentOutcome`, `FailureCategory`
    - _Requirements: 2.8, 3.3, 3.4, 8.3, 8.6_

  - [ ]* 1.3 Write unit tests for data models
    - Test dataclass instantiation and defaults
    - Test enum values match expected strings
    - _Requirements: 2.8, 8.3_

- [x] 2. Implement CLI and configuration loading
  - [x] 2.1 Implement configuration loader
    - Create `swadoc/config.py` with `.env` file loading via `python-dotenv`
    - Implement environment variable reading for `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY`, `GITHUB_TOKEN`, `GITHUB_REPO`
    - Implement CLI-over-environment precedence logic
    - Implement validation: code-base values, project-path existence, LLM_PROVIDER values
    - Default `--output-path` to `./openapi.json` when not supplied
    - _Requirements: 1.1–1.15_

  - [x] 2.2 Implement CLI entry point with Click
    - Create `swadoc/cli.py` using `click` for argument parsing
    - Define flags: `--code-base`, `--project-path`, `--output-path`, `--dry-run`, `--open-pr`
    - Wire CLI to configuration loader and pipeline orchestrator
    - Handle validation errors with non-zero exit codes and stderr messages
    - Update `pyproject.toml` with `[project.scripts]` entry point
    - _Requirements: 1.1–1.15_

  - [ ]* 2.3 Write unit tests for CLI and configuration
    - Test valid flag combinations produce correct `SwadocConfig`
    - Test invalid `--code-base` values produce error exit
    - Test missing required env vars produce error exit with named variables
    - Test CLI flag overrides environment variable
    - Test default output path resolution
    - _Requirements: 1.1–1.15_

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Implement adapter protocol and registry
  - [x] 4.1 Define AdapterProtocol and AdapterRegistry
    - Create `swadoc/adapters/protocol.py` with `AdapterProtocol` (typing.Protocol)
    - Create `swadoc/adapters/registry.py` with `AdapterRegistry` class
    - Implement `register()` with conflict detection and protocol validation
    - Implement `select()` with code-base matching and framework auto-detection
    - Implement `list_registered()` method
    - _Requirements: 10.1–10.5_

  - [ ]* 4.2 Write unit tests for AdapterRegistry
    - Test registration of valid adapters
    - Test rejection of duplicate framework_id
    - Test rejection of incomplete protocol implementation
    - Test selection by code-base flag
    - Test auto-detection timeout (5-second limit)
    - _Requirements: 10.1–10.5_

- [x] 5. Implement framework adapters
  - [x] 5.1 Implement Laravel adapter
    - Create `swadoc/adapters/laravel.py` implementing `AdapterProtocol`
    - Implement `discover_routes()`: invoke `php artisan route:list --json` with 60s timeout
    - Implement `get_handler_source()`: read PHP handler file and extract function
    - Implement `get_existing_annotation()`: parse PHPDoc blocks above handler
    - Implement `write_annotation()`: insert PHPDoc block preserving indentation and line endings
    - Implement `generate_spec()`: invoke L5-Swagger generation command
    - _Requirements: 2.1, 2.5, 2.8–2.13, 3.1, 5.1_

  - [x] 5.2 Implement AdonisJS adapter
    - Create `swadoc/adapters/adonisjs.py` implementing `AdapterProtocol`
    - Implement `discover_routes()`: invoke `node ace route:list --json` with 60s timeout
    - Implement `get_handler_source()`: read TypeScript/JS handler file and extract method
    - Implement `get_existing_annotation()`: parse JSDoc blocks above handler
    - Implement `write_annotation()`: insert JSDoc block preserving indentation and line endings
    - Implement `generate_spec()`: invoke swagger-jsdoc generation
    - Implement framework detection: check for `.adonisrc.json` or `ace` file
    - _Requirements: 2.2, 2.6, 2.8–2.13, 3.1, 5.1_

  - [x] 5.3 Implement Express adapter
    - Create `swadoc/adapters/express.py` implementing `AdapterProtocol`
    - Implement `discover_routes()`: static AST crawl via bundled Node.js script (no app execution)
    - Create `swadoc/adapters/express_ast_crawler.js` for route extraction
    - Implement `get_handler_source()`: read JS/TS handler file and extract function
    - Implement `get_existing_annotation()`: parse JSDoc blocks above handler
    - Implement `write_annotation()`: insert JSDoc block preserving indentation and line endings
    - Implement `generate_spec()`: invoke swagger-jsdoc generation
    - Implement framework detection: check `package.json` for `express` dependency
    - _Requirements: 2.3, 2.7, 2.8–2.13, 3.1, 5.1_

  - [ ]* 5.4 Write unit tests for framework adapters
    - Test Laravel route discovery parsing from JSON output
    - Test AdonisJS framework detection logic
    - Test Express framework detection logic (express in dependencies, no AdonisJS markers)
    - Test handler source extraction for each adapter
    - Test annotation block parsing for PHPDoc and JSDoc formats
    - Test error handling for non-zero exit codes and timeouts
    - _Requirements: 2.1–2.13_

- [x] 6. Implement annotation quality evaluation
  - [x] 6.1 Implement AnnotationQualityChecker
    - Create `swadoc/pipeline/quality_checker.py`
    - Implement `evaluate()` method with full classification logic
    - Check for: non-empty summary, non-empty operationId, valid response entry, request body docs, header docs, path param docs
    - Inspect handler source for request body access, header access, path parameter placeholders
    - Handle dynamic property access patterns with warnings
    - Handle missing handler file/function gracefully (classify as Underdocumented)
    - _Requirements: 3.1–3.6_

  - [ ]* 6.2 Write unit tests for quality evaluation
    - Test fully documented route classified as Documented_Route
    - Test missing summary classified as Underdocumented_Route
    - Test missing response classified as Underdocumented_Route
    - Test handler reading body without body docs classified as Underdocumented_Route
    - Test dynamic property access generates warning
    - Test missing handler file classified as Underdocumented_Route with warning
    - _Requirements: 3.1–3.6_

- [x] 7. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Implement LLM client and enrichment
  - [x] 8.1 Implement LLM client abstraction
    - Create `swadoc/enrichment/llm_client.py` with `LLMClient` protocol
    - Implement `AnthropicClient` using the Anthropic SDK with 30s timeout
    - Implement `OpenAIClient` using the OpenAI SDK with 30s timeout
    - Create `LLMClientFactory.create()` to instantiate based on provider config
    - Handle timeout, network errors, auth errors with structured error types
    - _Requirements: 4.4–4.6, 1.8–1.9_

  - [x] 8.2 Implement LLM enrichment pipeline
    - Create `swadoc/enrichment/enricher.py` with `LLMEnricher` class
    - Implement prompt building: include HTTP method, URI, middleware, handler source, existing annotation
    - Implement format instruction: body fields, headers, responses, bearerAuth, no markdown fences
    - Implement handler source truncation to last 200 lines when exceeding token limit
    - Implement response parsing: extract structured AnnotationBlock from LLM output
    - Implement format validation: PHPDoc for Laravel, JSDoc for AdonisJS/Express
    - Implement pre-write parseability validation (L5-Swagger / swagger-jsdoc compatibility)
    - Use `asyncio` for concurrent enrichment calls
    - _Requirements: 4.1–4.12_

  - [ ]* 8.3 Write unit tests for LLM client and enrichment
    - Test prompt contains required elements (method, URI, handler source)
    - Test timeout handling marks route as skipped with category `timeout`
    - Test network error marks route as failed with category `llm_call_failed`
    - Test unparseable response marks route with category `llm_response_unparseable`
    - Test format mismatch marks route with category `format_mismatch`
    - Test handler truncation to 200 lines with warning
    - Test pre-write validation failure marks with category `pre_write_validation_failed`
    - _Requirements: 4.1–4.12_

- [x] 9. Implement annotation writing and backup system
  - [x] 9.1 Implement backup manager
    - Create `swadoc/pipeline/backup.py` with backup file operations
    - Implement copy-to-backup: preserve relative path structure in `./autodoc-backup/`
    - Implement byte-for-byte verification after copy
    - Implement restore-from-backup for rollback scenarios
    - Abort run if backup directory cannot be created or file cannot be copied
    - Skip all file operations in dry-run mode
    - _Requirements: 5.2–5.4_

  - [x] 9.2 Implement annotation writer with post-write validation
    - Create `swadoc/pipeline/writer.py` with `AnnotationWriter` class
    - Insert annotation block on lines immediately preceding handler function definition
    - Preserve file's existing line endings and handler indentation
    - Do not modify any other lines in the file
    - Invoke backup manager before first modification of each file
    - After writing, re-parse modified file to verify no syntax errors
    - Restore from backup on parse failure, record write failure in report
    - In dry-run mode: log file path and route identifier without writing
    - _Requirements: 5.1–5.8_

  - [ ]* 9.3 Write unit tests for backup and annotation writing
    - Test backup creates byte-identical copy with correct relative path
    - Test backup failure aborts run without modifying source
    - Test annotation insertion preserves indentation and line endings
    - Test post-write syntax error triggers rollback from backup
    - Test dry-run mode writes no files
    - Test fully-documented codebase leaves all files unchanged
    - _Requirements: 5.1–5.8_

- [ ] 10. Implement spec generation and validation
  - [x] 10.1 Implement spec generation and output writing
    - Create `swadoc/pipeline/spec_generator.py`
    - Invoke framework's native spec generator via adapter's `generate_spec()`
    - Write output as JSON (`.json` extension) or YAML (`.yaml`/`.yml` extension)
    - Error on unsupported output extension with non-zero exit
    - Error on generator failure with non-zero exit
    - _Requirements: 6.1–6.4_

  - [x] 10.2 Implement OpenAPI spec validation
    - Create `swadoc/pipeline/spec_validator.py`
    - Validate generated document against OpenAPI 3.0.x schema using `openapi-spec-validator`
    - Collect validation errors with route URI and annotation identifier
    - Collect validation warnings for inclusion in report
    - Exit with non-zero on validation errors; continue on warnings-only
    - _Requirements: 6.5–6.7_

  - [ ]* 10.3 Write unit tests for spec generation and validation
    - Test JSON output for `.json` extension
    - Test YAML output for `.yaml` extension
    - Test unsupported extension produces error exit
    - Test valid spec passes validation
    - Test invalid spec reports errors with route context
    - Test warnings-only allows continued execution
    - _Requirements: 6.1–6.7_

- [x] 11. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Implement PR manager
  - [x] 12.1 Implement Git operations and branch management
    - Create `swadoc/git/pr_manager.py` with `PRManager` class
    - Implement `create_branch()`: generate `docs/swagger-autogen-{timestamp}` name (UTC, max 255 chars)
    - Handle branch name collisions with `-N` suffix (N=1 to 10)
    - Implement `commit_changes()`: stage files and commit with standard message format
    - Skip all Git operations in dry-run mode (log PR description instead)
    - _Requirements: 7.1–7.3, 7.10–7.11_

  - [x] 12.2 Implement GitHub API PR creation
    - Create `swadoc/git/github_client.py` for GitHub API interactions
    - Implement `open_pr()`: create PR against default branch via GitHub API
    - Build PR description: enriched routes, skipped routes, validation warnings, spec link
    - Cap description at 65000 characters with truncation notice
    - Implement exponential backoff for 429/403-rate-limit responses (1s start, 2x, max 60s, 5 attempts)
    - Handle 401/403-non-rate-limit as auth failure with immediate abort
    - _Requirements: 7.4–7.9_

  - [ ]* 12.3 Write unit tests for PR manager
    - Test branch name generation format and length limit
    - Test branch collision suffix resolution
    - Test commit message format with route count
    - Test PR description includes all required sections
    - Test description truncation at 65000 characters
    - Test exponential backoff retry logic
    - Test auth failure handling (401/403)
    - Test dry-run skips all Git operations
    - _Requirements: 7.1–7.11_

- [x] 13. Implement reporting
  - [x] 13.1 Implement Reporter with stdout summary and JSON report
    - Create `swadoc/reporting/reporter.py` with `Reporter` class
    - Implement `print_summary()`: output counts, spec path, PR URL or `none`
    - Implement `write_report()`: write JSON to `./swagger-autodoc-report.json`
    - Include per-route detail: method, URI, handler, classification, enrichment outcome, failures, validation issues
    - Include top-level summary: start timestamp (ISO 8601 UTC), duration_ms, counts
    - Include `dry_run` field and `would_change` list in dry-run mode
    - Handle write failure: emit error to stderr and exit non-zero
    - _Requirements: 8.1–8.6_

  - [ ]* 13.2 Write unit tests for Reporter
    - Test stdout summary contains all required fields
    - Test JSON report structure matches schema
    - Test dry-run mode includes `dry_run: true` and `would_change` list
    - Test write failure produces stderr error and non-zero exit
    - _Requirements: 8.1–8.6_

- [x] 14. Implement pipeline orchestrator and wire all components
  - [x] 14.1 Implement PipelineOrchestrator
    - Create `swadoc/pipeline/orchestrator.py` with `PipelineOrchestrator` class
    - Wire full pipeline: discover → evaluate → enrich → write → generate → validate → report → PR
    - Implement async enrichment with `asyncio` for concurrent LLM calls
    - Implement error accumulation (don't abort on single route failure)
    - Implement timing: track start time and duration for report
    - Handle unhandled exceptions: emit to stderr, clean up partial output, exit non-zero
    - Ensure exit code 0 on success (zero validation errors), non-zero on validation errors
    - _Requirements: 9.1–9.4_

  - [x] 14.2 Wire orchestrator into CLI entry point
    - Connect `cli.py` to `PipelineOrchestrator`
    - Register all three adapters in `AdapterRegistry` at startup
    - Pass resolved `SwadocConfig` to orchestrator
    - Handle keyboard interrupt gracefully
    - Update `main.py` to delegate to CLI
    - _Requirements: 1.1–1.15, 9.1–9.4_

  - [ ]* 14.3 Write integration tests for full pipeline
    - Test end-to-end run with mock adapter and mock LLM client
    - Test dry-run mode produces report without modifying files
    - Test pipeline continues after individual route failures
    - Test exit code 0 on success, non-zero on validation errors
    - Test timing stays under 300s for 50 routes / 20 enrichments (performance baseline)
    - _Requirements: 9.1–9.4_

- [x] 15. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- The design has no Correctness Properties section, so property-based tests are omitted in favor of unit and integration tests
- All adapters use subprocess isolation — no target application code is executed
- Async patterns (asyncio) are used only in the enrichment phase for concurrent LLM calls
- Python 3.14+ features (e.g., improved type hints) should be leveraged where appropriate

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["1.3", "2.1"] },
    { "id": 3, "tasks": ["2.2", "4.1"] },
    { "id": 4, "tasks": ["2.3", "4.2", "5.1", "5.2", "5.3"] },
    { "id": 5, "tasks": ["5.4", "6.1"] },
    { "id": 6, "tasks": ["6.2", "8.1"] },
    { "id": 7, "tasks": ["8.2"] },
    { "id": 8, "tasks": ["8.3", "9.1"] },
    { "id": 9, "tasks": ["9.2"] },
    { "id": 10, "tasks": ["9.3", "10.1"] },
    { "id": 11, "tasks": ["10.2"] },
    { "id": 12, "tasks": ["10.3", "12.1"] },
    { "id": 13, "tasks": ["12.2", "13.1"] },
    { "id": 14, "tasks": ["12.3", "13.2"] },
    { "id": 15, "tasks": ["14.1"] },
    { "id": 16, "tasks": ["14.2"] },
    { "id": 17, "tasks": ["14.3"] }
  ]
}
```
