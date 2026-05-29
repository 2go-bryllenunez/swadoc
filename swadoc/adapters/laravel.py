"""
Laravel framework adapter for Swadoc.

Implements AdapterProtocol for Laravel (PHP) projects:
- Route discovery via `php artisan route:list --json`
- Handler source extraction from PHP files
- PHPDoc annotation parsing and writing
- Spec generation via L5-Swagger (`php artisan l5-swagger:generate`)

Requirements: 2.1, 2.5, 2.8-2.13, 3.1, 5.1
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from swadoc.adapters.protocol import AdapterOperationError
from swadoc.models import (
    AnnotationBlock,
    HandlerSource,
    RouteRecord,
    SpecGenerationResult,
    WriteResult,
)

logger = logging.getLogger(__name__)

# Timeout for subprocess calls (Req 2.5)
_SUBPROCESS_TIMEOUT = 60

# Regex to match a PHP function/method definition line.
# Matches: [optional modifiers] function name(
_PHP_FUNCTION_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?:(?:public|protected|private|static|abstract|final)\s+)*"
    r"function\s+(?P<name>\w+)\s*\(",
    re.MULTILINE,
)

# Regex to match a PHPDoc block: /** ... */
_PHPDOC_BLOCK_RE = re.compile(r"/\*\*.*?\*/", re.DOTALL)


class LaravelAdapter:
    """Adapter for Laravel (PHP) projects.

    Implements the five AdapterProtocol operations for Laravel:
    discover_routes, get_handler_source, get_existing_annotation,
    write_annotation, and generate_spec.
    """

    framework_id = "laravel"

    # ------------------------------------------------------------------
    # discover_routes
    # ------------------------------------------------------------------

    def discover_routes(self, project_path: Path) -> list[RouteRecord]:
        """Invoke `php artisan route:list --json` and parse the output.

        Args:
            project_path: Absolute path to the Laravel project root.

        Returns:
            List of RouteRecord entries.

        Raises:
            AdapterOperationError: On non-zero exit, timeout, or parse error.
        """
        cmd = ["php", "artisan", "route:list", "--json"]
        try:
            result = subprocess.run(
                cmd,
                cwd=str(project_path),
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdapterOperationError(
                operation="discover_routes",
                message=(
                    f"php artisan route:list timed out after {_SUBPROCESS_TIMEOUT}s. "
                    f"stderr: {exc.stderr or ''}"
                ),
            ) from exc
        except FileNotFoundError as exc:
            raise AdapterOperationError(
                operation="discover_routes",
                message=(
                    "php executable not found. Ensure PHP is installed and on PATH. "
                    f"Detail: {exc}"
                ),
            ) from exc

        if result.returncode != 0:
            logger.error(
                "php artisan route:list exited with code %d. stderr: %s",
                result.returncode,
                result.stderr,
            )
            raise AdapterOperationError(
                operation="discover_routes",
                message=(
                    f"php artisan route:list exited with non-zero status "
                    f"{result.returncode}. stderr: {result.stderr}"
                ),
            )

        try:
            raw_routes = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            snippet = result.stdout[:200]
            logger.error(
                "Failed to parse route:list JSON output. Error: %s. Snippet: %s",
                exc,
                snippet,
            )
            raise AdapterOperationError(
                operation="discover_routes",
                message=(
                    f"Failed to parse php artisan route:list output as JSON: {exc}. "
                    f"Output snippet: {snippet!r}"
                ),
            ) from exc

        if not isinstance(raw_routes, list):
            raise AdapterOperationError(
                operation="discover_routes",
                message=(
                    "Expected a JSON array from php artisan route:list, "
                    f"got {type(raw_routes).__name__}."
                ),
            )

        routes: list[RouteRecord] = []
        for entry in raw_routes:
            route = self._parse_route_entry(entry, project_path)
            if route is not None:
                routes.append(route)

        return routes

    def _parse_route_entry(
        self, entry: dict, project_path: Path
    ) -> RouteRecord | None:
        """Parse a single route entry from Laravel's JSON output.

        Laravel's route:list JSON keys: method, uri, name, action, middleware.
        The ``action`` field is either ``"Closure"`` or
        ``"ControllerClass@method"`` (or a fully-qualified class name with
        ``@method``).

        Args:
            entry: A dict from the JSON array.
            project_path: Project root for resolving handler_file.

        Returns:
            A RouteRecord, or None if the entry is malformed.
        """
        # Normalise keys — older Laravel versions may use different casing
        method = str(entry.get("method", "")).upper().strip()
        uri = str(entry.get("uri", "")).strip()

        if not method or not uri:
            logger.warning(
                "Skipping route entry with missing method or uri: %s", entry
            )
            return None

        action = str(entry.get("action", "")).strip()
        handler = action if action else "Closure"

        handler_file: Path | None = None
        handler_function: str | None = None

        if action and action != "Closure" and "@" in action:
            # Format: "App\Http\Controllers\UserController@index"
            # or "UserController@index"
            class_part, method_part = action.rsplit("@", 1)
            handler_function = method_part.strip() or None

            # Convert namespace to file path:
            # "App\Http\Controllers\UserController" →
            # "app/Http/Controllers/UserController.php"
            resolved = self._resolve_controller_file(
                class_part, project_path
            )
            if resolved is not None:
                handler_file = resolved
            else:
                logger.warning(
                    "Could not resolve handler_file for route %s %s "
                    "(action: %s)",
                    method,
                    uri,
                    action,
                )
        elif action and action != "Closure":
            # Invokable controller: no "@method" suffix
            resolved = self._resolve_controller_file(action, project_path)
            if resolved is not None:
                handler_file = resolved
                handler_function = "__invoke"
            else:
                logger.warning(
                    "Could not resolve handler_file for route %s %s "
                    "(action: %s)",
                    method,
                    uri,
                    action,
                )
        else:
            # Closure route — handler_file and handler_function remain None
            logger.warning(
                "Route %s %s uses a Closure; handler_file and "
                "handler_function cannot be resolved.",
                method,
                uri,
            )

        return RouteRecord(
            method=method,
            uri=uri,
            handler=handler,
            handler_file=handler_file,
            handler_function=handler_function,
        )

    @staticmethod
    def _resolve_controller_file(
        class_name: str, project_path: Path
    ) -> Path | None:
        """Convert a PHP class name to a relative file path under project_path.

        Handles both backslash-separated namespaces and forward-slash paths.
        Strips a leading ``App\\`` prefix and maps to ``app/``.

        Args:
            class_name: Fully-qualified PHP class name, e.g.
                ``App\\Http\\Controllers\\UserController``.
            project_path: Laravel project root.

        Returns:
            Resolved Path if the file exists, else None.
        """
        # Normalise separators
        normalised = class_name.replace("\\", "/").strip("/")

        # Laravel convention: App\ → app/
        if normalised.startswith("App/"):
            normalised = "app/" + normalised[4:]

        candidate = project_path / (normalised + ".php")
        if candidate.is_file():
            return candidate

        # Try case-insensitive first segment lowercasing as a fallback
        parts = normalised.split("/")
        if parts:
            parts[0] = parts[0].lower()
            candidate2 = project_path / ("/".join(parts) + ".php")
            if candidate2.is_file():
                return candidate2

        return None

    # ------------------------------------------------------------------
    # get_handler_source
    # ------------------------------------------------------------------

    def get_handler_source(
        self, route: RouteRecord, project_path: Path
    ) -> HandlerSource:
        """Read the PHP handler file and extract the named function/method body.

        Args:
            route: Route whose handler source should be read.
            project_path: Laravel project root.

        Returns:
            HandlerSource with the function body and metadata.

        Raises:
            AdapterOperationError: If handler_file is None, the file is
                missing, or the function cannot be found.
        """
        if route.handler_file is None:
            raise AdapterOperationError(
                operation="get_handler_source",
                message=(
                    f"handler_file is None for route {route.method} {route.uri}; "
                    "cannot read handler source."
                ),
                route=route,
            )

        file_path = (
            route.handler_file
            if route.handler_file.is_absolute()
            else project_path / route.handler_file
        )

        if not file_path.is_file():
            raise AdapterOperationError(
                operation="get_handler_source",
                message=f"Handler file not found: {file_path}",
                route=route,
                file_path=file_path,
            )

        try:
            source = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AdapterOperationError(
                operation="get_handler_source",
                message=f"Cannot read handler file {file_path}: {exc}",
                route=route,
                file_path=file_path,
            ) from exc

        function_name = route.handler_function or "__invoke"
        lines = source.splitlines(keepends=True)

        start_line, end_line = self._find_function_bounds(
            lines, function_name, file_path, route
        )

        body_lines = lines[start_line - 1 : end_line]
        content = "".join(body_lines)
        line_count = len(body_lines)

        return HandlerSource(
            content=content,
            file_path=file_path,
            function_name=function_name,
            start_line=start_line,
            end_line=end_line,
            line_count=line_count,
        )

    @staticmethod
    def _find_function_bounds(
        lines: list[str],
        function_name: str,
        file_path: Path,
        route: RouteRecord,
    ) -> tuple[int, int]:
        """Locate the 1-based start and end line numbers of a PHP function.

        Uses brace counting to find the closing ``}`` of the function body.

        Args:
            lines: File lines (with line endings).
            function_name: Name of the function to locate.
            file_path: Used in error messages.
            route: Used in error messages.

        Returns:
            (start_line, end_line) as 1-based line numbers.

        Raises:
            AdapterOperationError: If the function is not found.
        """
        # Build a pattern that matches the specific function name
        fn_pattern = re.compile(
            r"^(?P<indent>[ \t]*)(?:(?:public|protected|private|static|abstract|final)\s+)*"
            r"function\s+" + re.escape(function_name) + r"\s*\(",
            re.MULTILINE,
        )

        full_source = "".join(lines)
        match = fn_pattern.search(full_source)
        if match is None:
            raise AdapterOperationError(
                operation="get_handler_source",
                message=(
                    f"Function '{function_name}' not found in {file_path}"
                ),
                route=route,
                file_path=file_path,
            )

        # Convert match start offset to 1-based line number
        start_offset = match.start()
        start_line = full_source[:start_offset].count("\n") + 1

        # Find the opening brace of the function body
        brace_pos = full_source.find("{", match.end())
        if brace_pos == -1:
            raise AdapterOperationError(
                operation="get_handler_source",
                message=(
                    f"Opening brace not found for function '{function_name}' "
                    f"in {file_path}"
                ),
                route=route,
                file_path=file_path,
            )

        # Count braces to find the matching closing brace
        depth = 0
        end_offset = brace_pos
        for i, ch in enumerate(full_source[brace_pos:], start=brace_pos):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_offset = i
                    break

        end_line = full_source[:end_offset + 1].count("\n") + 1
        return start_line, end_line

    # ------------------------------------------------------------------
    # get_existing_annotation
    # ------------------------------------------------------------------

    def get_existing_annotation(
        self, route: RouteRecord, project_path: Path
    ) -> AnnotationBlock | None:
        """Parse the PHPDoc block immediately above the handler function.

        Args:
            route: Route whose annotation should be extracted.
            project_path: Laravel project root.

        Returns:
            AnnotationBlock if a PHPDoc block exists, else None.

        Raises:
            AdapterOperationError: If the handler file cannot be read.
        """
        if route.handler_file is None:
            return None

        file_path = (
            route.handler_file
            if route.handler_file.is_absolute()
            else project_path / route.handler_file
        )

        if not file_path.is_file():
            raise AdapterOperationError(
                operation="get_existing_annotation",
                message=f"Handler file not found: {file_path}",
                route=route,
                file_path=file_path,
            )

        try:
            source = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AdapterOperationError(
                operation="get_existing_annotation",
                message=f"Cannot read handler file {file_path}: {exc}",
                route=route,
                file_path=file_path,
            ) from exc

        function_name = route.handler_function or "__invoke"
        return self._extract_phpdoc_before_function(source, function_name)

    @staticmethod
    def _extract_phpdoc_before_function(
        source: str, function_name: str
    ) -> AnnotationBlock | None:
        """Find the PHPDoc block (/** ... */) immediately preceding a function.

        "Immediately preceding" means only whitespace/blank lines are allowed
        between the end of the PHPDoc block and the function definition line.

        Args:
            source: Full PHP file content.
            function_name: Name of the target function.

        Returns:
            AnnotationBlock or None.
        """
        fn_pattern = re.compile(
            r"(?:(?:public|protected|private|static|abstract|final)\s+)*"
            r"function\s+" + re.escape(function_name) + r"\s*\(",
        )

        fn_match = fn_pattern.search(source)
        if fn_match is None:
            return None

        # Look at the text before the function definition
        preceding = source[: fn_match.start()]

        # Find the last PHPDoc block in the preceding text
        phpdoc_matches = list(_PHPDOC_BLOCK_RE.finditer(preceding))
        if not phpdoc_matches:
            return None

        last_phpdoc = phpdoc_matches[-1]
        between = preceding[last_phpdoc.end():]

        # Only whitespace/blank lines allowed between PHPDoc and function
        if between.strip():
            return None

        raw_text = last_phpdoc.group(0)
        return _parse_phpdoc_block(raw_text)

    # ------------------------------------------------------------------
    # write_annotation
    # ------------------------------------------------------------------

    def write_annotation(
        self,
        route: RouteRecord,
        annotation: AnnotationBlock,
        project_path: Path,
    ) -> WriteResult:
        """Insert a PHPDoc block immediately before the handler function.

        Preserves the file's existing line endings and the indentation of the
        function definition line. No other lines are modified.

        Args:
            route: Route whose handler should be annotated.
            annotation: Validated AnnotationBlock to write.
            project_path: Laravel project root.

        Returns:
            WriteResult indicating success or failure.
        """
        if route.handler_file is None:
            return WriteResult(
                success=False,
                file_path=project_path,
                error=(
                    f"handler_file is None for route {route.method} {route.uri}"
                ),
            )

        file_path = (
            route.handler_file
            if route.handler_file.is_absolute()
            else project_path / route.handler_file
        )

        if not file_path.is_file():
            return WriteResult(
                success=False,
                file_path=file_path,
                error=f"Handler file not found: {file_path}",
            )

        try:
            raw_bytes = file_path.read_bytes()
            source = raw_bytes.decode("utf-8")
        except OSError as exc:
            return WriteResult(
                success=False,
                file_path=file_path,
                error=f"Cannot read handler file {file_path}: {exc}",
            )

        function_name = route.handler_function or "__invoke"

        fn_pattern = re.compile(
            r"(?P<indent>[ \t]*)(?:(?:public|protected|private|static|abstract|final)\s+)*"
            r"function\s+" + re.escape(function_name) + r"\s*\(",
        )

        fn_match = fn_pattern.search(source)
        if fn_match is None:
            return WriteResult(
                success=False,
                file_path=file_path,
                error=(
                    f"Function '{function_name}' not found in {file_path}"
                ),
            )

        indent = fn_match.group("indent")

        # Detect line ending used in the file
        line_ending = _detect_line_ending(source)

        # Build the PHPDoc block with correct indentation and line endings
        phpdoc = _format_phpdoc_block(annotation.raw_text, indent, line_ending)

        # Insert the PHPDoc block immediately before the function definition
        insert_pos = fn_match.start()

        # Remove any existing PHPDoc block immediately before the function
        preceding = source[:insert_pos]
        phpdoc_matches = list(_PHPDOC_BLOCK_RE.finditer(preceding))
        if phpdoc_matches:
            last_phpdoc = phpdoc_matches[-1]
            between = preceding[last_phpdoc.end():]
            if not between.strip():
                # Replace the existing PHPDoc + whitespace before function
                insert_pos = last_phpdoc.start()
                # Preserve any leading whitespace/newlines before the old block
                source = source[:insert_pos] + source[fn_match.start():]
                # Recalculate fn_match position after removal
                fn_match2 = fn_pattern.search(source)
                if fn_match2 is None:
                    return WriteResult(
                        success=False,
                        file_path=file_path,
                        error=(
                            f"Function '{function_name}' not found after "
                            f"removing old PHPDoc in {file_path}"
                        ),
                    )
                insert_pos = fn_match2.start()

        new_source = source[:insert_pos] + phpdoc + source[insert_pos:]

        try:
            file_path.write_bytes(new_source.encode("utf-8"))
        except OSError as exc:
            return WriteResult(
                success=False,
                file_path=file_path,
                error=f"Cannot write to {file_path}: {exc}",
            )

        return WriteResult(success=True, file_path=file_path)

    # ------------------------------------------------------------------
    # generate_spec
    # ------------------------------------------------------------------

    def generate_spec(
        self, project_path: Path, output_path: Path
    ) -> SpecGenerationResult:
        """Invoke L5-Swagger to generate the OpenAPI specification.

        Runs `php artisan l5-swagger:generate` in the project directory.

        Args:
            project_path: Laravel project root.
            output_path: Destination for the generated OpenAPI document
                (used for reading the result after generation).

        Returns:
            SpecGenerationResult with the parsed document on success.
        """
        cmd = ["php", "artisan", "l5-swagger:generate"]
        try:
            result = subprocess.run(
                cmd,
                cwd=str(project_path),
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            return SpecGenerationResult(
                success=False,
                error=(
                    f"l5-swagger:generate timed out after {_SUBPROCESS_TIMEOUT}s. "
                    f"stderr: {exc.stderr or ''}"
                ),
            )
        except FileNotFoundError as exc:
            return SpecGenerationResult(
                success=False,
                error=f"php executable not found: {exc}",
            )

        if result.returncode != 0:
            logger.error(
                "l5-swagger:generate exited with code %d. stderr: %s",
                result.returncode,
                result.stderr,
            )
            return SpecGenerationResult(
                success=False,
                error=(
                    f"l5-swagger:generate exited with non-zero status "
                    f"{result.returncode}. stderr: {result.stderr}"
                ),
            )

        # Attempt to read the generated document from output_path
        if output_path.is_file():
            try:
                content = output_path.read_text(encoding="utf-8")
                ext = output_path.suffix.lower()
                if ext == ".json":
                    document = json.loads(content)
                elif ext in (".yaml", ".yml"):
                    try:
                        import yaml  # type: ignore[import-untyped]
                        document = yaml.safe_load(content)
                    except ImportError:
                        document = None
                        logger.warning(
                            "PyYAML not installed; cannot parse YAML spec."
                        )
                else:
                    document = None
                return SpecGenerationResult(success=True, document=document)
            except (OSError, json.JSONDecodeError, Exception) as exc:
                logger.warning(
                    "l5-swagger:generate succeeded but could not read "
                    "output file %s: %s",
                    output_path,
                    exc,
                )
                return SpecGenerationResult(success=True, document=None)

        # Generation succeeded but output_path not found — return success
        # without a parsed document (the caller may handle this)
        return SpecGenerationResult(success=True, document=None)


# ---------------------------------------------------------------------------
# PHPDoc helpers
# ---------------------------------------------------------------------------


def _parse_phpdoc_block(raw_text: str) -> AnnotationBlock:
    """Parse a raw PHPDoc string into an AnnotationBlock.

    Extracts summary, operationId, responses, request body, headers, and
    path parameters from the PHPDoc tags.

    Args:
        raw_text: The full ``/** ... */`` block text.

    Returns:
        An AnnotationBlock with ``format="phpdoc"``.
    """
    summary: str | None = None
    operation_id: str | None = None
    responses: list[dict] = []
    request_body: dict | None = None
    headers: list[dict] = []
    path_params: list[dict] = []

    # Extract summary from @OA\Get/Post/... summary="..."
    summary_match = re.search(r'summary\s*=\s*["\']([^"\']+)["\']', raw_text)
    if summary_match:
        summary = summary_match.group(1)

    # Extract operationId
    op_id_match = re.search(r'operationId\s*=\s*["\']([^"\']+)["\']', raw_text)
    if op_id_match:
        operation_id = op_id_match.group(1)

    # Extract @OA\Response entries
    for resp_match in re.finditer(
        r'@OA\\Response\s*\([^)]*response\s*=\s*(\d+)[^)]*description\s*=\s*["\']([^"\']*)["\']',
        raw_text,
        re.DOTALL,
    ):
        responses.append(
            {"status_code": int(resp_match.group(1)), "description": resp_match.group(2)}
        )

    # Extract @OA\RequestBody
    if re.search(r"@OA\\RequestBody", raw_text):
        request_body = {"raw": True}

    # Extract @OA\Header entries
    for hdr_match in re.finditer(
        r'@OA\\Header\s*\([^)]*name\s*=\s*["\']([^"\']+)["\']',
        raw_text,
        re.DOTALL,
    ):
        headers.append({"name": hdr_match.group(1)})

    # Extract @OA\Parameter entries for path params (in="path")
    for param_match in re.finditer(
        r'@OA\\Parameter\s*\([^)]*name\s*=\s*["\']([^"\']+)["\'][^)]*in\s*=\s*["\']path["\']',
        raw_text,
        re.DOTALL,
    ):
        path_params.append({"name": param_match.group(1)})

    return AnnotationBlock(
        raw_text=raw_text,
        format="phpdoc",
        summary=summary,
        operation_id=operation_id,
        responses=responses,
        request_body=request_body,
        headers=headers,
        path_params=path_params,
    )


def _detect_line_ending(source: str) -> str:
    """Detect the dominant line ending in a source string.

    Returns ``"\\r\\n"`` if CRLF is dominant, else ``"\\n"``.
    """
    crlf_count = source.count("\r\n")
    lf_count = source.count("\n") - crlf_count
    return "\r\n" if crlf_count > lf_count else "\n"


def _format_phpdoc_block(raw_text: str, indent: str, line_ending: str) -> str:
    """Re-indent a PHPDoc block and append a trailing newline.

    Each line of the PHPDoc block is prefixed with ``indent``. The block is
    terminated with a ``line_ending`` so it sits on its own line(s) before
    the function definition.

    Args:
        raw_text: The raw PHPDoc text (may already have indentation).
        indent: Indentation string to apply to each line.
        line_ending: Line ending character(s) to use.

    Returns:
        The re-indented PHPDoc block followed by a line ending.
    """
    # Strip existing leading/trailing whitespace from the raw block
    stripped = raw_text.strip()

    # Split into lines and re-indent each
    lines = stripped.splitlines()
    indented_lines = [indent + line.strip() for line in lines]
    block = line_ending.join(indented_lines)

    # Ensure the block ends with a line ending so the function starts on a
    # new line
    return block + line_ending
