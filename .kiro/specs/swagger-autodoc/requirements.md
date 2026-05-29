# Requirements Document

## Introduction

The Swagger Auto-Documentation Script (Swadoc) is a Python tool that crawls a target codebase's registered API routes, evaluates the quality of existing OpenAPI/Swagger annotations, enriches underdocumented routes using a Large Language Model (LLM) as a single-step text transformer, generates a valid OpenAPI 3.0 specification, and optionally opens a GitHub pull request for developer review.

The tool is non-agentic: the LLM is invoked per route to produce annotation text, while all control flow, file I/O, validation, and Git operations are owned by the script. The v0.1 scope supports Laravel (PHP), AdonisJS (Node.js), and Express (Node.js) projects. The script is designed to run manually, on a cron schedule, or as a step in a CI/CD pipeline.

Out of scope for v0.1: runtime contract testing, multi-repo orchestration, auto-merging of pull requests, GraphQL or non-REST APIs, and authentication/secrets management beyond `.env` configuration.

## Glossary

- **Swadoc**: The Swagger Auto-Documentation Script described by this document.
- **Route_Discovery_Adapter**: A pluggable component that lists registered routes for a specific framework (Laravel, AdonisJS, or Express) and normalizes them into a common route record.
- **Route_Record**: A normalized object containing the fields `method`, `uri`, `handler`, `handler_file`, and `handler_function` for a single API route.
- **Annotation_Block**: A framework-native documentation comment (PHPDoc for Laravel, JSDoc for AdonisJS/Express) that describes a route in a form consumable by the framework's Swagger generator.
- **Annotation_Quality_Checker**: The component that evaluates whether a route's existing Annotation_Block meets the documentation quality criteria.
- **LLM_Enricher**: The component that builds prompts, calls the configured LLM, and returns a candidate Annotation_Block for a single route.
- **Annotation_Writer**: The component that writes a validated Annotation_Block to the correct position in a source file.
- **Spec_Validator**: The component that validates a generated OpenAPI document against the OpenAPI 3.0 schema.
- **Spec_Generator**: The framework's native Swagger generator (e.g., L5-Swagger, swagger-jsdoc) invoked by Swadoc to produce the OpenAPI document.
- **PR_Manager**: The component that creates a Git branch, commits changes, and opens a GitHub pull request via the GitHub API.
- **Reporter**: The component that emits a stdout summary and writes the JSON run report.
- **Run_Report**: A JSON file written to `./swagger-autodoc-report.json` containing structured details of a Swadoc run.
- **Backup_Directory**: The directory `./autodoc-backup/` where original copies of files are stored before modification.
- **Dry_Run_Mode**: A mode in which Swadoc performs all checks and LLM calls but writes no files and opens no pull requests.
- **Documented_Route**: A route whose Annotation_Block satisfies all pass criteria defined in Requirement 3.
- **Underdocumented_Route**: A route whose Annotation_Block is missing, partially populated, or contains only a freeform description.

## Requirements

### Requirement 1: Command-Line Interface and Configuration

**User Story:** As a developer, I want to invoke Swadoc with explicit flags and environment variables, so that I can control its behavior across manual, cron, and CI/CD invocations.

#### Acceptance Criteria

1. THE Swadoc SHALL accept a `--code-base` flag whose value is exactly one of the case-sensitive literal strings `node` or `php`.
2. THE Swadoc SHALL accept a `--project-path` flag whose value is an absolute or relative filesystem path to the root directory of the target codebase.
3. THE Swadoc SHALL accept an `--output-path` flag whose value is an absolute or relative filesystem path for the generated OpenAPI document.
4. THE Swadoc SHALL accept a `--dry-run` boolean flag that, when present on the command line, activates Dry_Run_Mode.
5. THE Swadoc SHALL accept an `--open-pr` boolean flag that, when present on the command line, enables pull request creation.
6. THE Swadoc SHALL load configuration values from a `.env` file located in the current working directory when such a file exists, and from process environment variables.
7. WHEN a single configuration value is supplied both via a CLI flag and via the environment (including the `.env` file), THE Swadoc SHALL use the value supplied by the CLI flag.
8. WHEN Swadoc is about to issue an LLM request, THE Swadoc SHALL require the environment variables `LLM_PROVIDER`, `LLM_MODEL`, and `LLM_API_KEY` to each be set to a non-empty string value.
9. WHERE `LLM_PROVIDER` is set, THE Swadoc SHALL accept exactly one of the case-sensitive values `anthropic` or `openai`.
10. WHERE the `--open-pr` flag is supplied, THE Swadoc SHALL require the environment variables `GITHUB_TOKEN` and `GITHUB_REPO` to each be set to a non-empty string value before initiating pull request creation.
11. WHERE `OUTPUT_PATH` is not configured and `--output-path` is not supplied, THE Swadoc SHALL use `./openapi.json`, resolved relative to the current working directory, as the output path.
12. IF a required configuration value is missing or empty at the time it is needed, THEN THE Swadoc SHALL exit with a non-zero status code, emit an error message to standard error that names each missing configuration value, and perform no LLM call and no pull request creation.
13. IF `--code-base` is supplied with a value other than `node` or `php`, THEN THE Swadoc SHALL exit with a non-zero status code and emit an error message to standard error indicating the invalid value and the accepted values.
14. IF `--project-path` resolves to a path that does not exist or is not a readable directory, THEN THE Swadoc SHALL exit with a non-zero status code and emit an error message to standard error indicating the invalid project path.
15. IF `LLM_PROVIDER` is set to a value other than `anthropic` or `openai`, THEN THE Swadoc SHALL exit with a non-zero status code and emit an error message to standard error indicating the invalid provider value and the accepted values.

### Requirement 2: Route Discovery

**User Story:** As a developer, I want Swadoc to enumerate all registered routes in my project, so that documentation coverage can be evaluated for every endpoint.

#### Acceptance Criteria

1. WHEN `--code-base` is `php`, THE Swadoc SHALL select the Laravel Route_Discovery_Adapter.
2. WHEN `--code-base` is `node` and the target project's root directory contains a `.adonisrc.json` file or an `ace` file, THE Swadoc SHALL select the AdonisJS Route_Discovery_Adapter.
3. WHEN `--code-base` is `node` and the target project's `package.json` lists `express` in its `dependencies` or `devDependencies` and no AdonisJS markers are present, THE Swadoc SHALL select the Express Route_Discovery_Adapter.
4. IF `--code-base` is `node` and neither the AdonisJS detection criteria nor the Express detection criteria are satisfied, THEN THE Swadoc SHALL exit with a non-zero status code and emit an error message to standard error indicating that no supported Node.js framework was detected.
5. WHEN the Laravel Route_Discovery_Adapter runs, THE Swadoc SHALL invoke `php artisan route:list --json` in the directory specified by `--project-path` with a subprocess timeout of 60 seconds.
6. WHEN the AdonisJS Route_Discovery_Adapter runs, THE Swadoc SHALL invoke `node ace route:list --json` in the directory specified by `--project-path` with a subprocess timeout of 60 seconds.
7. WHEN the Express Route_Discovery_Adapter runs, THE Swadoc SHALL discover routes by performing a static AST crawl of the target codebase without executing any application code.
8. THE Swadoc SHALL produce, from any Route_Discovery_Adapter, a list of Route_Record entries containing the fields `method`, `uri`, `handler`, `handler_file`, and `handler_function`, where `method` and `uri` are non-empty strings for every entry.
9. IF a Route_Discovery_Adapter cannot resolve `handler_file` or `handler_function` for a discovered route, THEN THE Swadoc SHALL set the unresolved field to null in the Route_Record and log a warning identifying the route's `method` and `uri`.
10. THE Swadoc SHALL NOT start any application server during route discovery.
11. WHILE performing route discovery, THE Swadoc SHALL NOT create, modify, or delete any file inside the directory specified by `--project-path`.
12. IF the framework's route listing command exits with a non-zero status or its execution exceeds the 60-second subprocess timeout, THEN THE Swadoc SHALL log the command's stderr along with the failure cause and exit with a non-zero status code.
13. IF the framework's route listing command produces output that cannot be parsed as the expected format, THEN THE Swadoc SHALL log the parse error and a snippet of the offending output and exit with a non-zero status code.

### Requirement 3: Annotation Quality Evaluation

**User Story:** As a developer, I want Swadoc to identify which routes need documentation work, so that already-documented routes are left untouched and underdocumented routes are flagged.

#### Acceptance Criteria

1. WHEN evaluating each Route_Record, THE Swadoc SHALL locate and read the handler source code identified by `handler_file` and `handler_function`.
2. IF the file at `handler_file` does not exist, is not readable, or does not define a function named `handler_function`, THEN THE Swadoc SHALL classify the route as an Underdocumented_Route, record a warning naming the missing handler and the route identifier, and continue processing remaining routes.
3. THE Annotation_Quality_Checker SHALL classify a route as a Documented_Route only when ALL of the following hold for that route's Annotation_Block: (a) the block contains a non-empty `summary`; (b) the block contains a non-empty `operationId`; (c) the block contains at least one response entry that includes both an HTTP status code in the range 100-599 and a non-empty description; (d) when the handler reads a request body, the block documents a request body entry that names a content type or schema reference; (e) when the handler reads request headers, the block documents one header entry per distinct header key read; and (f) when the route URI contains path parameter placeholders, the block documents one path parameter entry for every placeholder.
4. IF the route's Annotation_Block is absent, contains only a freeform description with no structured tags, or fails one or more conditions in acceptance criterion 3, THEN THE Swadoc SHALL classify the route as an Underdocumented_Route and SHALL record in the Run_Report the list of specific required fields that were missing.
5. WHEN the Annotation_Quality_Checker encounters dynamic property access of the forms `req.body[var]`, `req.headers[var]`, `$request->input($var)`, or `$request->header($var)` while inspecting a handler, THE Swadoc SHALL record a warning identifying the route, the source location, and the unresolved access expression, and SHALL continue evaluation of the remaining routes.
6. THE Swadoc SHALL NOT invoke the LLM_Enricher for any route classified as a Documented_Route.

### Requirement 4: LLM Enrichment

**User Story:** As a developer, I want Swadoc to use an LLM to generate accurate annotations for underdocumented routes, so that documentation coverage improves without manual authoring.

#### Acceptance Criteria

1. WHEN a route is classified as an Underdocumented_Route, THE LLM_Enricher SHALL build a prompt containing the route's HTTP method, URI, middleware list, handler source, and any existing Annotation_Block.
2. THE LLM_Enricher SHALL instruct the LLM to document body fields when the handler reads a request body, document header fields when the handler reads request headers, infer response shapes from return statements and serializer calls in the handler source, use the `bearerAuth` security scheme when authentication middleware is detected, and return only a structured Annotation_Block with no markdown fences or surrounding prose.
3. THE LLM_Enricher SHALL invoke the configured LLM exactly once per Underdocumented_Route per Swadoc run.
4. THE LLM_Enricher SHALL apply a 30-second timeout to each LLM call, measured from request initiation to response receipt.
5. IF an LLM call exceeds the 30-second timeout, THEN THE Swadoc SHALL skip the route, leave the route's source file unmodified, record the route in the Run_Report under skipped routes with route identifier and failure category `timeout`, and continue with the remaining routes.
6. IF an LLM call fails for any non-timeout reason including network error, authentication error, or provider error, THEN THE Swadoc SHALL skip the route, leave the route's source file unmodified, record the route in the Run_Report under failed routes with route identifier and failure category `llm_call_failed` along with the error reason, and continue with the remaining routes.
7. WHEN the handler source exceeds the configured `LLM_MODEL`'s input token limit, THE LLM_Enricher SHALL truncate the handler source to its last 200 lines and SHALL record a truncation warning in the Run_Report including the route identifier and the original line count.
8. THE LLM_Enricher SHALL emit Annotation_Blocks in PHPDoc format for the Laravel adapter and in JSDoc format for the AdonisJS and Express adapters.
9. IF the LLM returns an empty response or a response that cannot be parsed as a structured Annotation_Block, THEN THE Swadoc SHALL discard the response, leave the route's source file unmodified, record the route in the Run_Report with failure category `llm_response_unparseable`, and continue with the remaining routes.
10. IF the LLM returns an Annotation_Block whose format does not match the format required by the active adapter (PHPDoc for Laravel, JSDoc for AdonisJS or Express), THEN THE Swadoc SHALL discard the Annotation_Block, leave the route's source file unmodified, record the route in the Run_Report with failure category `format_mismatch`, and continue with the remaining routes.
11. WHEN the LLM_Enricher receives a candidate Annotation_Block, THE Swadoc SHALL validate the candidate by confirming it is parseable by L5-Swagger for the Laravel adapter or by swagger-jsdoc for the AdonisJS and Express adapters before passing it to the Annotation_Writer.
12. IF a candidate Annotation_Block fails the parseability validation in acceptance criterion 11, THEN THE Swadoc SHALL discard the Annotation_Block, leave the route's source file unmodified, record the route in the Run_Report with failure category `pre_write_validation_failed`, and continue with the remaining routes.

### Requirement 5: Annotation Writing and Backup

**User Story:** As a developer, I want generated annotations written into source files in a safe, reversible way, so that I can review or roll back changes without losing original code.

#### Acceptance Criteria

1. WHEN an Annotation_Block has been validated for a handler_function, THE Annotation_Writer SHALL write the Annotation_Block to the source file identified by `handler_file` on the lines immediately preceding the `handler_function` definition, preserving the file's existing line endings and the indentation of the `handler_function` definition line, and SHALL NOT modify any other lines in the file.
2. BEFORE modifying any source file for the first time during a run, THE Swadoc SHALL copy the original unmodified file to the Backup_Directory (`./autodoc-backup/`) preserving the file's path relative to the target project root, and SHALL ensure the copied file is byte-for-byte identical to the original.
3. IF the Backup_Directory cannot be created or a source file cannot be copied to the Backup_Directory, THEN THE Swadoc SHALL abort the run before modifying any source file, SHALL leave all source files unchanged, and SHALL emit an error indicating the backup failure and the affected file path.
4. WHILE Dry_Run_Mode is active, THE Swadoc SHALL NOT write to or create any source file, SHALL NOT create or modify the OpenAPI output file, and SHALL NOT create the Backup_Directory.
5. WHILE Dry_Run_Mode is active, THE Swadoc SHALL log, for each route that would be modified, the absolute or project-relative source file path and the route identifier (HTTP method and path).
6. AFTER writing an Annotation_Block to a source file, THE Swadoc SHALL re-parse the modified file using the same language parser used during route discovery and SHALL verify that the parser reports no syntax errors.
7. IF post-write parsing of a modified source file reports any syntax error, THEN THE Swadoc SHALL restore that file from the Backup_Directory so that its contents are byte-for-byte identical to the original, SHALL record the affected route in the Run_Report as a write failure including the route identifier and a reason indicating post-write parse failure, and SHALL continue processing remaining routes.
8. WHEN Swadoc is run on a codebase in which every discovered route is a Documented_Route, THE Swadoc SHALL leave every source file byte-for-byte identical to its pre-run contents and SHALL leave the OpenAPI output file byte-for-byte identical to its pre-run contents.

### Requirement 6: OpenAPI Specification Generation and Validation

**User Story:** As a developer, I want Swadoc to produce a valid OpenAPI 3.0 document from the enriched codebase, so that the result is directly consumable by Swagger tooling.

#### Acceptance Criteria

1. WHEN all Annotation_Blocks for a run have been written, THE Swadoc SHALL invoke the framework's native Spec_Generator to produce a single OpenAPI 3.0 document.
2. WHEN the Spec_Generator produces an OpenAPI document, THE Swadoc SHALL write the document to the path resolved per Requirement 1, using JSON format if the output path extension is `.json` and YAML format if the output path extension is `.yaml` or `.yml`.
3. IF the output path extension is not one of `.json`, `.yaml`, or `.yml`, THEN THE Swadoc SHALL log an error indicating the unsupported output extension, SHALL NOT open a pull request, and SHALL exit with a non-zero status code.
4. IF the Spec_Generator fails or raises an exception during document production, THEN THE Swadoc SHALL log an error indicating the generator failure together with the failure description reported by the generator, SHALL NOT open a pull request, and SHALL exit with a non-zero status code.
5. WHEN the OpenAPI document has been written to the output path, THE Spec_Validator SHALL validate the document against the OpenAPI 3.0.x schema.
6. IF the Spec_Validator reports one or more validation errors, THEN THE Swadoc SHALL log each validation error together with the offending route URI, the associated Annotation_Block identifier, and the validator-provided error description, SHALL NOT open a pull request, and SHALL exit with a non-zero status code.
7. WHEN the Spec_Validator reports one or more validation warnings and zero validation errors, THE Swadoc SHALL include each warning in the Run_Report with its associated route URI and SHALL continue execution to completion.

### Requirement 7: Pull Request Creation

**User Story:** As a developer, I want Swadoc to open a GitHub pull request with all generated changes, so that I can review and approve documentation updates through the standard code review process.

#### Acceptance Criteria

1. WHERE the `--open-pr` flag is supplied AND Dry_Run_Mode is not active, THE PR_Manager SHALL create a new Git branch named `docs/swagger-autogen-{timestamp}` where `{timestamp}` is the run's start time formatted as `YYYYMMDDTHHMMSSZ` in UTC, and the total branch name length SHALL NOT exceed 255 characters.
2. IF a Git branch with the resolved name already exists in the local repository, THEN THE PR_Manager SHALL append the suffix `-N` to the branch name where N is the lowest integer in the range 1 to 10 that yields an available branch name, and IF all 10 candidate names are in use THEN THE PR_Manager SHALL abort the run with a non-zero exit code and emit an error indicating the branch-name collision.
3. WHERE the `--open-pr` flag is supplied AND Dry_Run_Mode is not active, THE PR_Manager SHALL commit all modified source files and the generated OpenAPI document on the new branch with the commit message `docs: auto-enrich swagger annotations [{n} routes updated]` where `{n}` is a non-negative integer count of enriched routes.
4. WHERE the `--open-pr` flag is supplied AND Dry_Run_Mode is not active, THE PR_Manager SHALL open a pull request against the repository identified by `GITHUB_REPO` using the GitHub API authenticated with `GITHUB_TOKEN`, targeting the repository's default branch as the base.
5. WHEN a pull request is opened, THE PR_Manager SHALL include in the pull request description the list of enriched routes, the list of skipped routes with skip reason, the list of validation warnings, and a link to the generated OpenAPI document, with the total description capped at 65000 characters and a truncation notice appended if the cap is exceeded.
6. THE PR_Manager SHALL NOT merge any pull request.
7. IF a GitHub API request returns an HTTP 429 response or an HTTP 403 response indicating rate limiting, THEN THE PR_Manager SHALL retry the request using exponential backoff starting at 1 second, doubling on each attempt, capped at 60 seconds, for a maximum of 5 attempts, and SHALL log the attempt number and wait duration on each retry.
8. IF the maximum retry count for a rate-limited request is exhausted, THEN THE PR_Manager SHALL abort the pull request creation, leave the local branch and commit in place, emit an error indicating retry exhaustion, and exit with a non-zero status code.
9. IF a GitHub API request returns an HTTP 401 response or an HTTP 403 response not indicating rate limiting, THEN THE PR_Manager SHALL abort the pull request creation, leave the local branch and commit in place, emit an error indicating the authentication or authorization failure, and exit with a non-zero status code.
10. IF either `GITHUB_TOKEN` or `GITHUB_REPO` is missing or empty when `--open-pr` is supplied, THEN THE PR_Manager SHALL abort before any branch creation, emit an error naming the missing variable, and exit with a non-zero status code.
11. WHILE Dry_Run_Mode is active, THE PR_Manager SHALL NOT create any branch, commit, or pull request, and SHALL log the message that would have been used as the pull request description.

### Requirement 8: Reporting

**User Story:** As a developer or CI system, I want a clear summary and a structured report of every Swadoc run, so that I can audit results and integrate them into automation.

#### Acceptance Criteria

1. WHEN a Swadoc run completes, THE Reporter SHALL print a run summary to standard output containing the count of routes discovered, the count of Documented_Routes, the count of routes enriched, the count of validation warnings, the count of validation errors, the absolute path of the generated OpenAPI document, and either the URL of the pull request when one was opened or the literal value `none` when no pull request was opened.
2. WHEN a Swadoc run completes, THE Reporter SHALL write a JSON Run_Report to `./swagger-autodoc-report.json`, overwriting any existing file at that path.
3. THE Run_Report SHALL contain, for each discovered route, the route's HTTP method, URI, handler identifier, classification (one of: `Documented_Route` or `Underdocumented_Route`), enrichment outcome (one of: `enriched`, `skipped`, or `failed`), and a list of validation warnings and validation errors associated with that route, where the list is empty when no warnings or errors apply.
4. WHILE Dry_Run_Mode is active, THE Reporter SHALL include in the Run_Report a `dry_run` field set to `true` and a `would_change` list enumerating the file paths and route identifiers (HTTP method and URI) that would have been modified.
5. IF the Reporter cannot write the Run_Report to `./swagger-autodoc-report.json` due to a file system or I/O failure, THEN THE Reporter SHALL print an error message to standard error indicating the failure reason and the target path, and SHALL terminate the run with a non-zero exit status.
6. THE Run_Report SHALL include a top-level summary object containing the run's start timestamp in ISO 8601 UTC format, the run's duration in milliseconds, and the total counts of routes discovered, Documented_Routes, routes enriched, validation warnings, and validation errors.

### Requirement 9: Performance and Reliability

**User Story:** As a CI/CD operator, I want Swadoc to complete within a predictable time budget and to fail loudly on errors, so that pipeline integration is reliable.

#### Acceptance Criteria

1. WHEN a run processes up to 50 discovered routes with LLM enrichment for up to 20 routes and every LLM call returns within its 30-second timeout, THE Swadoc SHALL complete in under 300 seconds of wall-clock time measured from process start to process exit.
2. IF any OpenAPI specification validation error is reported during a run, THEN THE Swadoc SHALL emit the count of validation errors to standard error and SHALL exit with a status code in the range 1 to 255.
3. WHEN a run completes with zero validation errors, THE Swadoc SHALL exit with status code 0 regardless of the number of validation warnings.
4. IF an unhandled exception occurs during a run, THEN THE Swadoc SHALL emit the exception type, message, and stack trace to standard error, SHALL leave no partial OpenAPI output file at the configured output path, and SHALL exit with a status code in the range 1 to 255.

### Requirement 10: Adapter Extensibility

**User Story:** As a maintainer, I want a uniform adapter interface, so that I can add support for additional frameworks without modifying core orchestration logic.

#### Acceptance Criteria

1. THE Swadoc SHALL define an adapter interface that requires the operations `discover_routes`, `get_handler_source`, `get_existing_annotation`, `write_annotation`, and `generate_spec`, where each operation has a documented input contract, output contract, and failure mode that returns a structured error indicating the failed operation and the offending route or file without terminating the orchestration process.
2. THE Swadoc SHALL provide concrete adapter implementations for Laravel, AdonisJS, and Express that implement all five operations declared by the adapter interface in acceptance criterion 1, and each adapter SHALL be selectable by a unique framework identifier matching one of the values `laravel`, `adonisjs`, or `express`.
3. WHEN a new adapter is registered with Swadoc via the documented registration mechanism, THE Swadoc SHALL select the new adapter based on the `--code-base` flag value matching the adapter's framework identifier, and on framework auto-detection when ambiguity must be resolved within the chosen `--code-base` language, without any modification to files in the orchestration layer.
4. IF an adapter registration is attempted with a framework identifier that is already registered, or with a class that does not implement all five required operations listed in acceptance criterion 1, THEN THE Swadoc SHALL reject the registration and return an error indicating the conflicting identifier or the missing operations, and SHALL leave the previously registered adapters unchanged.
5. IF the `--code-base` flag value does not match any registered adapter's framework identifier and framework auto-detection does not resolve to a registered adapter within 5 seconds, THEN THE Swadoc SHALL terminate execution and return an error indicating that no adapter was selected and listing the registered framework identifiers.
