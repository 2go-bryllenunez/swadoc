"""
AdonisJS framework adapter for Swadoc.

Implements AdapterProtocol for AdonisJS (Node.js) projects.
Supports route discovery via `node ace route:list --json`, JSDoc annotation
parsing and writing, handler source extraction, and swagger-jsdoc spec generation.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from swadoc.adapters.protocol import AdapterOperationError, AdapterProtocol
from swadoc.models import (
    AnnotationBlock,
    HandlerSource,
    RouteRecord,
    SpecGenerationResult,
    WriteResult,
)

logger = logging.getLogger(__name__)

# Timeout for subprocess calls (seconds) — Req 2.6
_SUBPROCESS_TIMEOUT = 60

# Candidate controller directories relative to project root (AdonisJS conventions)
_CONTROLLER_DIRS = [
    "app/Controllers/Http",
    "app/controllers",
    "app/Controllers",
]

# Supported file extensions for handler files
_HANDLER_EXTENSIONS = [".ts", ".js"]


class AdonisJSAdapter:
    """AdonisJS framework adapter implementing AdapterProtocol.

    Supports AdonisJS v5/v6 projects. Route discovery uses the built-in
    `node ace route:list --json` command. Handler source extraction reads
    TypeScript/JavaScript controller files. Annotations use JSDoc format.
    """

    framework_id = "adonisjs"

    # ------------------------------------------------------------------
    # Framework detection (Req 2.2)
    # ------------------------------------------------------------------

    def detect(self, project_path: Path) -> bool:
        """Return True if AdonisJS markers are present in *project_path*.

        Markers: ``.adonisrc.json`` file or ``ace`` file (Req 2.2).

        Args:
            project_path: Root directory of the target project.

        Returns:
            True when at least one AdonisJS marker is found.
        """
        return (project_path / ".adonisrc.json").exists() or (
            project_path / "ace"
        ).exists()

    # ------------------------------------------------------------------
    # Route discovery (Req 2.6, 2.8–2.13)
    # ------------------------------------------------------------------

    def discover_routes(self, project_path: Path) -> list[RouteRecord]:
        """Invoke ``node ace route:list --json`` and parse the output.

        Args:
            project_path: Absolute path to the root of the AdonisJS project.

        Returns:
            A list of RouteRecord entries with ``method``, ``uri``,
            ``handler``, ``handler_file``, and ``handler_function`` fields.
            ``handler_file`` and ``handler_function`` are None when they
            cannot be resolved (a warning is logged per Req 2.9).

        Raises:
            AdapterOperationError: On non-zero exit, timeout, or parse error
                (Req 2.12, 2.13).
        """
        cmd = ["node", "ace", "route:list", "--json"]
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
                    f"'node ace route:list --json' timed out after "
                    f"{_SUBPROCESS_TIMEOUT}s. "
                    f"stderr: {getattr(exc, 'stderr', '') or ''}"
                ),
            ) from exc
        except FileNotFoundError as exc:
            raise AdapterOperationError(
                operation="discover_routes",
                message=(
                    "Could not find 'node' executable. "
                    "Ensure Node.js is installed and on PATH."
                ),
            ) from exc

        if result.returncode != 0:
            raise AdapterOperationError(
                operation="discover_routes",
                message=(
                    f"'node ace route:list --json' exited with code "
                    f"{result.returncode}. "
                    f"stderr: {result.stderr.strip()}"
                ),
            )

        raw_output = result.stdout.strip()
        try:
            data = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            snippet = raw_output[:200]
            raise AdapterOperationError(
                operation="discover_routes",
                message=(
                    f"Failed to parse JSON output from 'node ace route:list': "
                    f"{exc}. Output snippet: {snippet!r}"
                ),
            ) from exc

        if not isinstance(data, list):
            snippet = raw_output[:200]
            raise AdapterOperationError(
                operation="discover_routes",
                message=(
                    f"Expected a JSON array from 'node ace route:list --json', "
                    f"got {type(data).__name__}. Output snippet: {snippet!r}"
                ),
            )

        routes: list[RouteRecord] = []
        for entry in data:
            route = self._parse_route_entry(entry, project_path)
            if route is not None:
                routes.append(route)

        return routes

    def _parse_route_entry(
        self, entry: dict, project_path: Path
    ) -> RouteRecord | None:
        """Parse a single route entry from the JSON output.

        AdonisJS route:list JSON fields vary by version:
        - v5: ``methods`` (list), ``pattern``, ``handler``
        - v6: ``method`` (str or list), ``uri`` or ``pattern``, ``handler``

        Returns None (and logs a warning) if ``method`` or ``uri`` are missing.
        """
        # Normalise method
        method_raw = entry.get("methods") or entry.get("method") or ""
        if isinstance(method_raw, list):
            method = "|".join(m.upper() for m in method_raw if m)
        else:
            method = str(method_raw).upper()

        # Normalise URI
        uri = entry.get("pattern") or entry.get("uri") or entry.get("url") or ""

        if not method or not uri:
            logger.warning(
                "Skipping route entry with missing method or URI: %s", entry
            )
            return None

        handler_str = entry.get("handler") or entry.get("name") or ""

        handler_file, handler_function = self._resolve_handler(
            handler_str, project_path
        )

        return RouteRecord(
            method=method,
            uri=uri,
            handler=handler_str,
            handler_file=handler_file,
            handler_function=handler_function,
        )

    def _resolve_handler(
        self, handler_str: str, project_path: Path
    ) -> tuple[Path | None, str | None]:
        """Resolve a handler string to (handler_file, handler_function).

        AdonisJS handler strings take the form:
        - ``"App/Controllers/Http/UserController.index"``
        - ``"UserController.index"``
        - ``"#controllers/user_controller.index"`` (v6 sub-path imports)
        - Closure / anonymous handlers (no dot separator)

        Returns (None, None) with a warning when resolution fails (Req 2.9).
        """
        if not handler_str:
            return None, None

        # Strip leading '#' used in AdonisJS v6 sub-path imports
        normalized = handler_str.lstrip("#")

        # Split on the last '.' to separate module path from method name
        if "." not in normalized:
            logger.warning(
                "Cannot resolve handler_file/handler_function for handler "
                "'%s': no dot separator found (may be a closure).",
                handler_str,
            )
            return None, None

        module_path, method_name = normalized.rsplit(".", 1)

        # Convert module path separators to filesystem path
        # e.g. "App/Controllers/Http/UserController" → "App/Controllers/Http/UserController"
        # e.g. "controllers/user_controller" → "controllers/user_controller"
        rel_path = module_path.replace("\\", "/")

        # Try to locate the file under known controller directories and
        # directly relative to project_path
        candidate_bases = [project_path] + [
            project_path / d for d in _CONTROLLER_DIRS
        ]

        for base in candidate_bases:
            for ext in _HANDLER_EXTENSIONS:
                candidate = base / (rel_path + ext)
                if candidate.is_file():
                    return candidate, method_name

        # Last-resort: try just the final component of the path in each dir
        last_component = rel_path.split("/")[-1]
        for base in candidate_bases:
            for ext in _HANDLER_EXTENSIONS:
                candidate = base / (last_component + ext)
                if candidate.is_file():
                    return candidate, method_name

        logger.warning(
            "Could not resolve handler file for handler '%s' "
            "(method=%s, module_path=%s). "
            "Searched under: %s",
            handler_str,
            method_name,
            rel_path,
            [str(b) for b in candidate_bases],
        )
        return None, None

    # ------------------------------------------------------------------
    # Handler source extraction (Req 3.1)
    # ------------------------------------------------------------------

    def get_handler_source(
        self, route: RouteRecord, project_path: Path
    ) -> HandlerSource:
        """Read the TypeScript/JS handler file and extract the named method body.

        Args:
            route: The route whose handler source should be read.
            project_path: Absolute path to the root of the target codebase.

        Returns:
            A HandlerSource with the method body, file path, and line range.

        Raises:
            AdapterOperationError: When handler_file is None, the file does
                not exist, or handler_function cannot be located.
        """
        if route.handler_file is None or route.handler_function is None:
            raise AdapterOperationError(
                operation="get_handler_source",
                message=(
                    f"Cannot read handler source for route "
                    f"{route.method} {route.uri}: "
                    "handler_file or handler_function is None."
                ),
                route=route,
            )

        file_path = route.handler_file
        if not file_path.is_absolute():
            file_path = project_path / file_path

        if not file_path.is_file():
            raise AdapterOperationError(
                operation="get_handler_source",
                message=(
                    f"Handler file not found: {file_path} "
                    f"(route: {route.method} {route.uri})"
                ),
                route=route,
                file_path=file_path,
            )

        try:
            source_text = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AdapterOperationError(
                operation="get_handler_source",
                message=f"Cannot read handler file {file_path}: {exc}",
                route=route,
                file_path=file_path,
            ) from exc

        lines = source_text.splitlines(keepends=True)
        start_line, end_line = _find_method_bounds(
            lines, route.handler_function
        )

        if start_line is None:
            raise AdapterOperationError(
                operation="get_handler_source",
                message=(
                    f"Function '{route.handler_function}' not found in "
                    f"{file_path} (route: {route.method} {route.uri})"
                ),
                route=route,
                file_path=file_path,
            )

        body_lines = lines[start_line - 1 : end_line]
        content = "".join(body_lines)
        line_count = len(body_lines)

        return HandlerSource(
            content=content,
            file_path=file_path,
            function_name=route.handler_function,
            start_line=start_line,
            end_line=end_line,
            line_count=line_count,
        )

    # ------------------------------------------------------------------
    # Annotation reading (Req 3.1)
    # ------------------------------------------------------------------

    def get_existing_annotation(
        self, route: RouteRecord, project_path: Path
    ) -> AnnotationBlock | None:
        """Parse the JSDoc block immediately above the handler method definition.

        Looks for a ``/** ... */`` block directly preceding the method
        definition line. Returns None when no such block exists.

        Args:
            route: The route whose annotation should be extracted.
            project_path: Absolute path to the root of the target codebase.

        Returns:
            An AnnotationBlock with ``format="jsdoc"`` if a JSDoc block is
            found, or None.

        Raises:
            AdapterOperationError: When the handler file cannot be read.
        """
        if route.handler_file is None or route.handler_function is None:
            return None

        file_path = route.handler_file
        if not file_path.is_absolute():
            file_path = project_path / file_path

        if not file_path.is_file():
            raise AdapterOperationError(
                operation="get_existing_annotation",
                message=f"Handler file not found: {file_path}",
                route=route,
                file_path=file_path,
            )

        try:
            source_text = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AdapterOperationError(
                operation="get_existing_annotation",
                message=f"Cannot read handler file {file_path}: {exc}",
                route=route,
                file_path=file_path,
            ) from exc

        lines = source_text.splitlines(keepends=True)
        method_line_idx = _find_method_line(lines, route.handler_function)
        if method_line_idx is None:
            return None

        jsdoc_raw = _extract_jsdoc_before(lines, method_line_idx)
        if jsdoc_raw is None:
            return None

        return _parse_jsdoc_block(jsdoc_raw)

    # ------------------------------------------------------------------
    # Annotation writing (Req 5.1)
    # ------------------------------------------------------------------

    def write_annotation(
        self,
        route: RouteRecord,
        annotation: AnnotationBlock,
        project_path: Path,
    ) -> WriteResult:
        """Insert a JSDoc block immediately before the handler method definition.

        Preserves the file's existing line endings and the indentation of the
        method definition line. No other lines are modified (Req 5.1).

        Args:
            route: The route whose handler should be annotated.
            annotation: The validated AnnotationBlock to write.
            project_path: Absolute path to the root of the target codebase.

        Returns:
            A WriteResult indicating success or failure.
        """
        if route.handler_file is None or route.handler_function is None:
            return WriteResult(
                success=False,
                file_path=project_path,
                error=(
                    f"Cannot write annotation for route "
                    f"{route.method} {route.uri}: "
                    "handler_file or handler_function is None."
                ),
            )

        file_path = route.handler_file
        if not file_path.is_absolute():
            file_path = project_path / file_path

        if not file_path.is_file():
            return WriteResult(
                success=False,
                file_path=file_path,
                error=f"Handler file not found: {file_path}",
            )

        try:
            raw_bytes = file_path.read_bytes()
            source_text = raw_bytes.decode("utf-8")
        except OSError as exc:
            return WriteResult(
                success=False,
                file_path=file_path,
                error=f"Cannot read handler file {file_path}: {exc}",
            )

        # Detect line ending used in the file
        line_ending = _detect_line_ending(raw_bytes)
        lines = source_text.splitlines(keepends=True)

        method_line_idx = _find_method_line(lines, route.handler_function)
        if method_line_idx is None:
            return WriteResult(
                success=False,
                file_path=file_path,
                error=(
                    f"Method '{route.handler_function}' not found in "
                    f"{file_path}"
                ),
            )

        # Determine indentation from the method definition line
        method_line = lines[method_line_idx]
        indent = _get_indentation(method_line)

        # Remove any existing JSDoc block immediately before the method
        insert_idx = method_line_idx
        if insert_idx > 0:
            jsdoc_start = _find_jsdoc_start_before(lines, method_line_idx)
            if jsdoc_start is not None:
                # Remove the old block
                del lines[jsdoc_start:method_line_idx]
                insert_idx = jsdoc_start

        # Build the JSDoc comment lines
        jsdoc_lines = _build_jsdoc_lines(
            annotation.raw_text, indent, line_ending
        )

        # Insert the new block
        for i, jsdoc_line in enumerate(jsdoc_lines):
            lines.insert(insert_idx + i, jsdoc_line)

        new_source = "".join(lines)

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
    # Spec generation (Req 6.1)
    # ------------------------------------------------------------------

    def generate_spec(
        self, project_path: Path, output_path: Path
    ) -> SpecGenerationResult:
        """Invoke swagger-jsdoc to generate an OpenAPI spec.

        Runs ``npx swagger-jsdoc`` (or a locally installed binary) with the
        project's swagger-jsdoc configuration file when present, otherwise
        uses sensible defaults.

        Args:
            project_path: Absolute path to the root of the AdonisJS project.
            output_path: Destination path for the generated OpenAPI document.

        Returns:
            A SpecGenerationResult with the parsed document on success, or an
            error description on failure.
        """
        # Look for a swagger-jsdoc config file in the project
        config_candidates = [
            project_path / "swagger.js",
            project_path / "swagger.json",
            project_path / "swagger-jsdoc.config.js",
            project_path / "jsdoc.config.js",
        ]
        config_file = next(
            (c for c in config_candidates if c.is_file()), None
        )

        if config_file is not None:
            cmd = [
                "npx",
                "swagger-jsdoc",
                "-d",
                str(config_file),
                "-o",
                str(output_path),
            ]
        else:
            # Fallback: scan all TS/JS files under app/
            cmd = [
                "npx",
                "swagger-jsdoc",
                "-o",
                str(output_path),
                str(project_path / "app/**/*.ts"),
                str(project_path / "app/**/*.js"),
            ]

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
                    f"swagger-jsdoc timed out after {_SUBPROCESS_TIMEOUT}s. "
                    f"stderr: {getattr(exc, 'stderr', '') or ''}"
                ),
            )
        except FileNotFoundError as exc:
            return SpecGenerationResult(
                success=False,
                error=(
                    "Could not find 'npx' executable. "
                    "Ensure Node.js and npm are installed and on PATH."
                ),
            )

        if result.returncode != 0:
            return SpecGenerationResult(
                success=False,
                error=(
                    f"swagger-jsdoc exited with code {result.returncode}. "
                    f"stderr: {result.stderr.strip()}"
                ),
            )

        # Read and parse the generated document
        if not output_path.is_file():
            return SpecGenerationResult(
                success=False,
                error=(
                    f"swagger-jsdoc succeeded but output file not found at "
                    f"{output_path}"
                ),
            )

        try:
            raw = output_path.read_text(encoding="utf-8")
            if output_path.suffix in {".yaml", ".yml"}:
                import yaml  # type: ignore[import]
                document = yaml.safe_load(raw)
            else:
                document = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            return SpecGenerationResult(
                success=False,
                error=f"Failed to parse generated spec at {output_path}: {exc}",
            )

        return SpecGenerationResult(success=True, document=document)


# ---------------------------------------------------------------------------
# Module-level helper functions
# ---------------------------------------------------------------------------

# Regex patterns for method/function detection in TypeScript/JavaScript
# Matches patterns like:
#   async index({ request, response }: HttpContext) {
#   index = async ({ request }: HttpContext) => {
#   public async index(ctx: HttpContext): Promise<void> {
#   index(ctx: HttpContext) {
_METHOD_PATTERN = re.compile(
    r"""
    (?:
        (?:public\s+|private\s+|protected\s+|static\s+|async\s+)*  # modifiers
        (?P<name1>\w+)                                               # method name
        \s*[=:]\s*                                                   # = or :
        (?:async\s+)?                                                # optional async
        (?:\([^)]*\)|\w+)                                           # params
        \s*=>                                                        # arrow
    |
        (?:public\s+|private\s+|protected\s+|static\s+|async\s+)*  # modifiers
        (?P<name2>\w+)                                               # method name
        \s*\(                                                        # opening paren
    )
    """,
    re.VERBOSE,
)

# Simpler targeted pattern used for line-by-line scanning
_METHOD_LINE_PATTERN_TMPL = (
    r"(?:^|\s)(?:public\s+|private\s+|protected\s+|static\s+|async\s+)*"
    r"{name}\s*[\(=]"
)


def _find_method_line(lines: list[str], method_name: str) -> int | None:
    """Return the 0-based index of the line defining *method_name*.

    Searches for the first line that looks like a method/function definition
    with the given name.

    Returns None when not found.
    """
    pattern = re.compile(
        _METHOD_LINE_PATTERN_TMPL.format(name=re.escape(method_name))
    )
    for idx, line in enumerate(lines):
        if pattern.search(line):
            return idx
    return None


def _find_method_bounds(
    lines: list[str], method_name: str
) -> tuple[int | None, int | None]:
    """Return (start_line, end_line) as 1-based line numbers for *method_name*.

    Uses brace counting to find the end of the method body.
    Returns (None, None) when the method is not found.
    """
    method_line_idx = _find_method_line(lines, method_name)
    if method_line_idx is None:
        return None, None

    start_line = method_line_idx + 1  # 1-based

    # Count braces to find the end of the method body
    brace_depth = 0
    found_open = False
    end_line = start_line

    for idx in range(method_line_idx, len(lines)):
        line = lines[idx]
        for ch in line:
            if ch == "{":
                brace_depth += 1
                found_open = True
            elif ch == "}":
                brace_depth -= 1

        if found_open and brace_depth == 0:
            end_line = idx + 1  # 1-based
            break
    else:
        # Reached end of file without closing brace
        end_line = len(lines)

    return start_line, end_line


def _extract_jsdoc_before(
    lines: list[str], method_line_idx: int
) -> str | None:
    """Extract the JSDoc block (``/** ... */``) immediately before *method_line_idx*.

    Skips blank lines between the JSDoc block and the method definition.
    Returns the raw JSDoc text, or None when no JSDoc block is found.
    """
    # Walk backwards from the line before the method, skipping blank lines
    idx = method_line_idx - 1
    while idx >= 0 and lines[idx].strip() == "":
        idx -= 1

    if idx < 0:
        return None

    # The line at idx should be the closing */ of a JSDoc block
    if "*/" not in lines[idx]:
        return None

    end_idx = idx

    # Walk backwards to find the opening /**
    while idx >= 0:
        if "/**" in lines[idx]:
            start_idx = idx
            jsdoc_lines = lines[start_idx : end_idx + 1]
            return "".join(jsdoc_lines)
        idx -= 1

    return None


def _find_jsdoc_start_before(
    lines: list[str], method_line_idx: int
) -> int | None:
    """Return the 0-based index of the ``/**`` line of the JSDoc block before *method_line_idx*.

    Returns None when no JSDoc block is found immediately before the method.
    """
    idx = method_line_idx - 1
    while idx >= 0 and lines[idx].strip() == "":
        idx -= 1

    if idx < 0 or "*/" not in lines[idx]:
        return None

    while idx >= 0:
        if "/**" in lines[idx]:
            return idx
        idx -= 1

    return None


def _parse_jsdoc_block(raw_text: str) -> AnnotationBlock:
    """Parse a raw JSDoc block string into an AnnotationBlock.

    Extracts summary, operationId, responses, request body, headers, and
    path parameters from ``@swagger`` or ``@openapi`` YAML content when
    present, or falls back to basic tag parsing.

    Args:
        raw_text: The raw JSDoc comment text including ``/**`` and ``*/``.

    Returns:
        An AnnotationBlock with ``format="jsdoc"``.
    """
    # Extract the content between /** and */
    inner = re.sub(r"^/\*\*", "", raw_text.strip())
    inner = re.sub(r"\*/$", "", inner.strip())

    # Strip leading " * " from each line
    cleaned_lines = []
    for line in inner.splitlines():
        stripped = line.strip()
        if stripped.startswith("* "):
            cleaned_lines.append(stripped[2:])
        elif stripped == "*":
            cleaned_lines.append("")
        else:
            cleaned_lines.append(stripped)

    cleaned = "\n".join(cleaned_lines).strip()

    # Try to parse @swagger / @openapi YAML block
    swagger_match = re.search(
        r"@(?:swagger|openapi)\s*\n(.*?)(?=\n\s*@|\Z)",
        cleaned,
        re.DOTALL,
    )

    summary: str | None = None
    operation_id: str | None = None
    responses: list[dict] = []
    request_body: dict | None = None
    headers: list[dict] = []
    path_params: list[dict] = []

    if swagger_match:
        yaml_text = swagger_match.group(1).strip()
        try:
            import yaml  # type: ignore[import]
            spec = yaml.safe_load(yaml_text)
            if isinstance(spec, dict):
                # The YAML may be a full path item or just the operation
                # Try to find the operation object
                operation: dict = {}
                for _path, path_item in spec.items():
                    if isinstance(path_item, dict):
                        for _method, op in path_item.items():
                            if isinstance(op, dict):
                                operation = op
                                break
                    break

                if not operation and isinstance(spec, dict):
                    # Might be the operation directly
                    operation = spec

                summary = operation.get("summary")
                operation_id = operation.get("operationId")

                # Responses
                for status, resp in (operation.get("responses") or {}).items():
                    entry: dict = {"status_code": str(status)}
                    if isinstance(resp, dict):
                        entry["description"] = resp.get("description", "")
                    responses.append(entry)

                # Request body
                if "requestBody" in operation:
                    request_body = operation["requestBody"]

                # Parameters
                for param in operation.get("parameters") or []:
                    if not isinstance(param, dict):
                        continue
                    if param.get("in") == "header":
                        headers.append(param)
                    elif param.get("in") == "path":
                        path_params.append(param)
        except Exception:  # noqa: BLE001
            pass  # Fall through to raw text storage

    return AnnotationBlock(
        raw_text=raw_text,
        format="jsdoc",
        summary=summary,
        operation_id=operation_id,
        responses=responses,
        request_body=request_body,
        headers=headers,
        path_params=path_params,
    )


def _detect_line_ending(raw_bytes: bytes) -> str:
    """Detect the dominant line ending in *raw_bytes*.

    Returns ``"\\r\\n"`` for CRLF files, ``"\\n"`` otherwise.
    """
    crlf_count = raw_bytes.count(b"\r\n")
    lf_count = raw_bytes.count(b"\n") - crlf_count
    return "\r\n" if crlf_count > lf_count else "\n"


def _get_indentation(line: str) -> str:
    """Return the leading whitespace of *line*."""
    return line[: len(line) - len(line.lstrip())]


def _build_jsdoc_lines(
    raw_text: str, indent: str, line_ending: str
) -> list[str]:
    """Build a list of JSDoc comment lines with the given indentation.

    If *raw_text* already looks like a JSDoc block (starts with ``/**``),
    it is re-indented. Otherwise it is wrapped in ``/** ... */``.

    Each returned line ends with *line_ending*.
    """
    stripped = raw_text.strip()

    if stripped.startswith("/**") and stripped.endswith("*/"):
        # Re-indent the existing block
        block_lines = stripped.splitlines()
    else:
        # Wrap plain text in a JSDoc block
        content_lines = stripped.splitlines()
        block_lines = ["/**"] + [f" * {l}" for l in content_lines] + [" */"]

    result = []
    for line in block_lines:
        result.append(indent + line.strip() + line_ending)

    return result
