"""
Express framework adapter for Swadoc.

Implements AdapterProtocol for Express (Node.js) projects.

Route discovery uses a bundled static AST crawler (express_ast_crawler.js)
invoked via a Node.js subprocess — no application code is executed (Req 2.7,
2.10).

Annotation format: JSDoc (Req 4.8).
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

# Path to the bundled JS crawler, resolved relative to this file.
_CRAWLER_JS = Path(__file__).parent / "express_ast_crawler.js"

# Subprocess timeout for the AST crawler (seconds).
_CRAWLER_TIMEOUT = 60

# Maximum lines returned in HandlerSource before truncation.
_MAX_HANDLER_LINES = 200


class ExpressAdapter:
    """Adapter for Express (Node.js) projects.

    Attributes:
        framework_id: Unique identifier used by AdapterRegistry.
    """

    framework_id: str = "express"

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def detect(self, project_path: Path) -> bool:
        """Return True when the project looks like an Express app.

        Checks that:
        - ``package.json`` lists ``express`` in dependencies or devDependencies
        - No AdonisJS markers (``.adonisrc.json`` or ``ace``) are present

        Args:
            project_path: Root directory of the target project.

        Returns:
            True if Express is detected, False otherwise.
        """
        # AdonisJS takes priority (Req 2.3)
        if (project_path / ".adonisrc.json").exists() or (project_path / "ace").exists():
            return False

        pkg_json = project_path / "package.json"
        if not pkg_json.is_file():
            return False

        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read package.json at %s: %s", pkg_json, exc)
            return False

        deps: dict = data.get("dependencies", {})
        dev_deps: dict = data.get("devDependencies", {})
        return "express" in deps or "express" in dev_deps

    # ------------------------------------------------------------------
    # Route discovery (Req 2.7, 2.8, 2.9, 2.10, 2.11, 2.12, 2.13)
    # ------------------------------------------------------------------

    def discover_routes(self, project_path: Path) -> list[RouteRecord]:
        """Discover Express routes via static AST crawl.

        Invokes the bundled ``express_ast_crawler.js`` via ``node`` with a
        60-second timeout.  No application code is executed.

        Args:
            project_path: Root directory of the Express project.

        Returns:
            List of RouteRecord entries.

        Raises:
            AdapterOperationError: On non-zero exit, timeout, or JSON parse
                failure.
        """
        if not _CRAWLER_JS.is_file():
            raise AdapterOperationError(
                operation="discover_routes",
                message=f"Bundled AST crawler not found: {_CRAWLER_JS}",
            )

        cmd = ["node", str(_CRAWLER_JS), str(project_path)]
        logger.debug("Running Express AST crawler: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_CRAWLER_TIMEOUT,
                cwd=str(project_path),
            )
        except subprocess.TimeoutExpired as exc:
            raise AdapterOperationError(
                operation="discover_routes",
                message=(
                    f"Express AST crawler timed out after {_CRAWLER_TIMEOUT}s. "
                    f"stderr: {getattr(exc, 'stderr', '')}"
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
                    f"Express AST crawler exited with code {result.returncode}. "
                    f"stderr: {result.stderr.strip()}"
                ),
            )

        raw_output = result.stdout.strip()
        if not raw_output:
            logger.warning("Express AST crawler produced no output; returning empty route list.")
            return []

        try:
            raw_routes: list[dict] = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            snippet = raw_output[:200]
            raise AdapterOperationError(
                operation="discover_routes",
                message=(
                    f"Failed to parse Express AST crawler output as JSON: {exc}. "
                    f"Output snippet: {snippet!r}"
                ),
            ) from exc

        routes: list[RouteRecord] = []
        for entry in raw_routes:
            method = (entry.get("method") or "").strip().upper()
            uri    = (entry.get("uri") or "").strip()

            if not method or not uri:
                logger.warning(
                    "Skipping route entry with missing method or uri: %s", entry
                )
                continue

            handler_file_raw = entry.get("handler_file")
            handler_function = entry.get("handler_function") or None
            handler          = entry.get("handler") or f"{method} {uri}"

            handler_file: Path | None = None
            if handler_file_raw:
                hf = Path(handler_file_raw)
                if hf.is_file():
                    handler_file = hf
                else:
                    logger.warning(
                        "handler_file '%s' for route %s %s does not exist; "
                        "setting to None (Req 2.9).",
                        handler_file_raw, method, uri,
                    )

            if handler_file is None or handler_function is None:
                logger.warning(
                    "Could not resolve handler_file or handler_function for "
                    "route %s %s (Req 2.9).",
                    method, uri,
                )

            routes.append(
                RouteRecord(
                    method=method,
                    uri=uri,
                    handler=handler,
                    handler_file=handler_file,
                    handler_function=handler_function,
                )
            )

        return routes

    # ------------------------------------------------------------------
    # Handler source extraction (Req 3.1)
    # ------------------------------------------------------------------

    def get_handler_source(
        self, route: RouteRecord, project_path: Path
    ) -> HandlerSource:
        """Read the source of the named handler function from the handler file.

        Args:
            route: Route whose handler source should be read.
            project_path: Root directory of the project (unused but required
                by protocol).

        Returns:
            HandlerSource with function body, file path, and line range.

        Raises:
            AdapterOperationError: When handler_file is None, the file does
                not exist, or the named function cannot be located.
        """
        if route.handler_file is None:
            raise AdapterOperationError(
                operation="get_handler_source",
                message=f"handler_file is None for route {route.method} {route.uri}",
                route=route,
            )

        file_path = route.handler_file
        if not file_path.is_file():
            raise AdapterOperationError(
                operation="get_handler_source",
                message=f"Handler file does not exist: {file_path}",
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

        lines = source.splitlines(keepends=True)

        if route.handler_function is None:
            raise AdapterOperationError(
                operation="get_handler_source",
                message=(
                    f"handler_function is None for route {route.method} {route.uri}"
                ),
                route=route,
                file_path=file_path,
            )

        start_line = _find_function_start(lines, route.handler_function)
        if start_line is None:
            raise AdapterOperationError(
                operation="get_handler_source",
                message=(
                    f"Function '{route.handler_function}' not found in "
                    f"{file_path}"
                ),
                route=route,
                file_path=file_path,
            )

        end_line = _find_function_end(lines, start_line)
        original_line_count = end_line - start_line + 1
        was_truncated = False

        # Truncate to last _MAX_HANDLER_LINES lines (Req 4.7)
        if original_line_count > _MAX_HANDLER_LINES:
            was_truncated = True
            start_line = end_line - _MAX_HANDLER_LINES + 1

        content = "".join(lines[start_line - 1 : end_line])
        line_count = end_line - start_line + 1

        return HandlerSource(
            content=content,
            file_path=file_path,
            function_name=route.handler_function,
            start_line=start_line,
            end_line=end_line,
            line_count=line_count,
            was_truncated=was_truncated,
            original_line_count=original_line_count if was_truncated else None,
        )

    # ------------------------------------------------------------------
    # Annotation reading (Req 3.1)
    # ------------------------------------------------------------------

    def get_existing_annotation(
        self, route: RouteRecord, project_path: Path
    ) -> AnnotationBlock | None:
        """Parse the JSDoc block immediately above the handler function.

        Args:
            route: Route whose annotation should be extracted.
            project_path: Root directory of the project.

        Returns:
            Parsed AnnotationBlock if a JSDoc block exists, None otherwise.

        Raises:
            AdapterOperationError: When the handler file cannot be read.
        """
        if route.handler_file is None or route.handler_function is None:
            return None

        file_path = route.handler_file
        if not file_path.is_file():
            raise AdapterOperationError(
                operation="get_existing_annotation",
                message=f"Handler file does not exist: {file_path}",
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

        lines = source.splitlines(keepends=True)
        func_line = _find_function_start(lines, route.handler_function)
        if func_line is None:
            return None

        jsdoc_raw = _extract_jsdoc_above(lines, func_line)
        if jsdoc_raw is None:
            return None

        return _parse_jsdoc(jsdoc_raw)

    # ------------------------------------------------------------------
    # Annotation writing (Req 5.1)
    # ------------------------------------------------------------------

    def write_annotation(
        self,
        route: RouteRecord,
        annotation: AnnotationBlock,
        project_path: Path,
    ) -> WriteResult:
        """Insert a JSDoc block immediately before the handler function.

        Preserves the file's existing line endings and the indentation of the
        function definition line.  No other lines are modified.

        Args:
            route: Route whose handler should be annotated.
            annotation: Validated AnnotationBlock to write.
            project_path: Root directory of the project.

        Returns:
            WriteResult indicating success or failure.
        """
        if route.handler_file is None or route.handler_function is None:
            return WriteResult(
                success=False,
                file_path=route.handler_file or project_path,
                error=(
                    "Cannot write annotation: handler_file or handler_function "
                    "is None."
                ),
            )

        file_path = route.handler_file
        if not file_path.is_file():
            return WriteResult(
                success=False,
                file_path=file_path,
                error=f"Handler file does not exist: {file_path}",
            )

        try:
            raw_bytes = file_path.read_bytes()
            source    = raw_bytes.decode("utf-8")
        except OSError as exc:
            return WriteResult(
                success=False,
                file_path=file_path,
                error=f"Cannot read handler file: {exc}",
            )

        # Detect line ending style
        line_ending = _detect_line_ending(raw_bytes)
        lines = source.splitlines(keepends=True)

        func_line = _find_function_start(lines, route.handler_function)
        if func_line is None:
            return WriteResult(
                success=False,
                file_path=file_path,
                error=(
                    f"Function '{route.handler_function}' not found in "
                    f"{file_path}"
                ),
            )

        # Determine indentation from the function definition line (1-indexed)
        func_line_text = lines[func_line - 1]
        indent = _leading_whitespace(func_line_text)

        # Remove any existing JSDoc block immediately above the function
        insert_at = func_line - 1  # 0-indexed insertion point
        existing_jsdoc_start = _find_jsdoc_start_above(lines, func_line)
        if existing_jsdoc_start is not None:
            # Remove lines from existing_jsdoc_start to func_line - 1 (0-indexed)
            del lines[existing_jsdoc_start : func_line - 1]
            insert_at = existing_jsdoc_start

        # Build the JSDoc block with correct indentation and line endings
        jsdoc_lines = _format_jsdoc(annotation.raw_text, indent, line_ending)

        # Insert the JSDoc block
        for i, jsdoc_line in enumerate(jsdoc_lines):
            lines.insert(insert_at + i, jsdoc_line)

        new_source = "".join(lines)

        try:
            file_path.write_bytes(new_source.encode("utf-8"))
        except OSError as exc:
            return WriteResult(
                success=False,
                file_path=file_path,
                error=f"Cannot write handler file: {exc}",
            )

        return WriteResult(success=True, file_path=file_path)

    # ------------------------------------------------------------------
    # Spec generation (Req 6.1)
    # ------------------------------------------------------------------

    def generate_spec(
        self, project_path: Path, output_path: Path
    ) -> SpecGenerationResult:
        """Invoke swagger-jsdoc to generate an OpenAPI spec.

        Requires ``swagger-jsdoc`` to be installed in the project
        (``npx swagger-jsdoc`` is used so a local install suffices).

        Args:
            project_path: Root directory of the Express project.
            output_path: Destination path for the generated OpenAPI document.

        Returns:
            SpecGenerationResult with the parsed document on success, or an
            error description on failure.
        """
        # Locate a swagger-jsdoc config file in the project root
        config_candidates = [
            project_path / "swagger.js",
            project_path / "swagger-jsdoc.js",
            project_path / "swaggerDef.js",
            project_path / "swagger.config.js",
        ]
        config_file: Path | None = next(
            (c for c in config_candidates if c.is_file()), None
        )

        if config_file is None:
            return SpecGenerationResult(
                success=False,
                error=(
                    "No swagger-jsdoc configuration file found in project root. "
                    "Expected one of: "
                    + ", ".join(c.name for c in config_candidates)
                ),
            )

        # Collect JS/TS source files to pass as API sources
        source_files = _collect_js_files(project_path)
        if not source_files:
            return SpecGenerationResult(
                success=False,
                error="No JS/TS source files found in project.",
            )

        cmd = [
            "npx",
            "--yes",
            "swagger-jsdoc",
            "-d", str(config_file),
            "-o", str(output_path),
        ] + [str(f) for f in source_files[:100]]  # cap to avoid arg-list overflow

        logger.debug("Running swagger-jsdoc: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(project_path),
            )
        except subprocess.TimeoutExpired as exc:
            return SpecGenerationResult(
                success=False,
                error=f"swagger-jsdoc timed out after 120s. stderr: {getattr(exc, 'stderr', '')}",
            )
        except FileNotFoundError as exc:
            return SpecGenerationResult(
                success=False,
                error=(
                    "Could not find 'npx' executable. "
                    "Ensure Node.js is installed and on PATH."
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

        # Parse the generated output file
        if not output_path.is_file():
            return SpecGenerationResult(
                success=False,
                error=f"swagger-jsdoc did not produce output at {output_path}",
            )

        try:
            ext = output_path.suffix.lower()
            content = output_path.read_text(encoding="utf-8")
            if ext in (".yaml", ".yml"):
                try:
                    import yaml  # type: ignore[import]
                    document = yaml.safe_load(content)
                except ImportError:
                    document = None
            else:
                document = json.loads(content)
        except Exception as exc:  # noqa: BLE001
            return SpecGenerationResult(
                success=False,
                error=f"Failed to parse generated spec: {exc}",
            )

        return SpecGenerationResult(success=True, document=document)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _find_function_start(lines: list[str], function_name: str) -> int | None:
    """Return the 1-indexed line number of the function definition.

    Searches for:
    - ``function <name>(``
    - ``async function <name>(``
    - ``const/let/var <name> = (async )?((...) =>``
    - ``const/let/var <name> = (async )?function``
    - ``<name>(<params>) {``  (class method / object method)
    - ``export (default )?function <name>(``

    Args:
        lines: File lines (with line endings).
        function_name: Name of the function to locate.

    Returns:
        1-indexed line number, or None if not found.
    """
    name = re.escape(function_name)
    patterns = [
        # function declaration
        re.compile(rf'(?:export\s+)?(?:async\s+)?function\s+{name}\s*\('),
        # arrow / function expression
        re.compile(rf'(?:const|let|var)\s+{name}\s*=\s*(?:async\s+)?(?:\([^)]*\)|\w+)\s*=>'),
        re.compile(rf'(?:const|let|var)\s+{name}\s*=\s*(?:async\s+)?function'),
        # class / object method
        re.compile(rf'(?:async\s+)?{name}\s*\([^)]*\)\s*\{{'),
        # export default function
        re.compile(rf'export\s+default\s+(?:async\s+)?function\s+{name}\s*\('),
    ]

    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        for pat in patterns:
            if pat.search(stripped):
                return i

    return None


def _find_function_end(lines: list[str], start_line: int) -> int:
    """Return the 1-indexed line number of the closing brace of a function.

    Uses brace counting starting from ``start_line``.  Falls back to the last
    line of the file if no matching brace is found.

    Args:
        lines: File lines.
        start_line: 1-indexed line where the function starts.

    Returns:
        1-indexed end line number.
    """
    depth = 0
    found_open = False

    for i in range(start_line - 1, len(lines)):
        for ch in lines[i]:
            if ch == '{':
                depth += 1
                found_open = True
            elif ch == '}':
                depth -= 1
                if found_open and depth == 0:
                    return i + 1  # 1-indexed

    return len(lines)


def _extract_jsdoc_above(lines: list[str], func_line: int) -> str | None:
    """Extract the JSDoc block (``/** … */``) immediately above ``func_line``.

    Scans upward from ``func_line - 1``, skipping blank lines, looking for a
    ``*/`` terminator and then a ``/**`` opener.

    Args:
        lines: File lines.
        func_line: 1-indexed line of the function definition.

    Returns:
        Raw JSDoc text (including ``/**`` and ``*/``), or None.
    """
    # Walk upward from the line before the function
    i = func_line - 2  # 0-indexed

    # Skip blank lines
    while i >= 0 and lines[i].strip() == "":
        i -= 1

    if i < 0:
        return None

    # Must end with */
    if not lines[i].strip().endswith("*/"):
        return None

    end_idx = i

    # Walk upward to find /**
    while i >= 0 and "/**" not in lines[i]:
        i -= 1

    if i < 0 or "/**" not in lines[i]:
        return None

    start_idx = i
    return "".join(lines[start_idx : end_idx + 1])


def _find_jsdoc_start_above(lines: list[str], func_line: int) -> int | None:
    """Return the 0-indexed start line of the JSDoc block above ``func_line``.

    Args:
        lines: File lines.
        func_line: 1-indexed line of the function definition.

    Returns:
        0-indexed start line of the JSDoc block, or None.
    """
    i = func_line - 2  # 0-indexed

    # Skip blank lines
    while i >= 0 and lines[i].strip() == "":
        i -= 1

    if i < 0 or not lines[i].strip().endswith("*/"):
        return None

    end_idx = i
    while i >= 0 and "/**" not in lines[i]:
        i -= 1

    if i < 0 or "/**" not in lines[i]:
        return None

    return i


def _parse_jsdoc(raw: str) -> AnnotationBlock:
    """Parse a raw JSDoc string into an AnnotationBlock.

    Extracts:
    - summary (first non-empty description line)
    - @operationId
    - @returns / @response tags → responses list
    - @requestBody → request_body dict
    - @header tags → headers list
    - @param {path} tags → path_params list

    Args:
        raw: Raw JSDoc text.

    Returns:
        Populated AnnotationBlock with format="jsdoc".
    """
    # Strip /** and */ markers, then strip leading * from each line
    inner_lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("/**"):
            stripped = stripped[3:].strip()
        elif stripped.startswith("*/"):
            stripped = stripped[2:].strip()
        elif stripped.startswith("*"):
            stripped = stripped[1:].strip()
        if stripped:
            inner_lines.append(stripped)

    summary: str | None = None
    operation_id: str | None = None
    responses: list[dict] = []
    request_body: dict | None = None
    headers: list[dict] = []
    path_params: list[dict] = []

    for line in inner_lines:
        if line.startswith("@"):
            tag_match = re.match(r'@(\S+)\s*(.*)', line)
            if not tag_match:
                continue
            tag  = tag_match.group(1).lower()
            rest = tag_match.group(2).strip()

            if tag == "operationid":
                operation_id = rest or None

            elif tag in ("returns", "return", "response"):
                # @returns {200} Description
                m = re.match(r'\{(\d+)\}\s*(.*)', rest)
                if m:
                    responses.append({
                        "status_code": int(m.group(1)),
                        "description": m.group(2).strip(),
                    })

            elif tag == "requestbody":
                request_body = {"description": rest}

            elif tag == "header":
                # @header {string} X-Auth-Token Description
                m = re.match(r'\{[^}]+\}\s+(\S+)\s*(.*)', rest)
                if m:
                    headers.append({
                        "name": m.group(1),
                        "description": m.group(2).strip(),
                    })

            elif tag == "param":
                # @param {path} {type} name Description
                m = re.match(r'\{path\}\s+\{[^}]+\}\s+(\S+)\s*(.*)', rest, re.IGNORECASE)
                if m:
                    path_params.append({
                        "name": m.group(1),
                        "description": m.group(2).strip(),
                    })
        else:
            # First non-tag line is the summary
            if summary is None:
                summary = line

    return AnnotationBlock(
        raw_text=raw,
        format="jsdoc",
        summary=summary,
        operation_id=operation_id,
        responses=responses,
        request_body=request_body,
        headers=headers,
        path_params=path_params,
    )


def _detect_line_ending(raw_bytes: bytes) -> str:
    """Detect the dominant line ending in a byte string.

    Args:
        raw_bytes: Raw file bytes.

    Returns:
        ``"\\r\\n"`` for CRLF, ``"\\r"`` for CR-only, ``"\\n"`` for LF.
    """
    crlf = raw_bytes.count(b"\r\n")
    cr   = raw_bytes.count(b"\r") - crlf
    lf   = raw_bytes.count(b"\n") - crlf

    if crlf >= lf and crlf >= cr:
        return "\r\n"
    if cr > lf:
        return "\r"
    return "\n"


def _leading_whitespace(line: str) -> str:
    """Return the leading whitespace characters of a line.

    Args:
        line: A single source line (may include line ending).

    Returns:
        String of leading spaces/tabs.
    """
    return line[: len(line) - len(line.lstrip())]


def _format_jsdoc(raw_text: str, indent: str, line_ending: str) -> list[str]:
    """Format a JSDoc block with the given indentation and line endings.

    If ``raw_text`` already looks like a JSDoc block (starts with ``/**``),
    it is re-indented.  Otherwise it is wrapped in ``/** … */``.

    Args:
        raw_text: Raw annotation text.
        indent: Indentation string to prepend to each line.
        line_ending: Line ending to use (``"\\n"``, ``"\\r\\n"``, etc.).

    Returns:
        List of formatted lines, each ending with ``line_ending``.
    """
    stripped = raw_text.strip()

    if stripped.startswith("/**"):
        # Re-indent existing JSDoc
        inner = stripped.splitlines()
        result = []
        for line in inner:
            result.append(indent + line.strip() + line_ending)
        return result

    # Wrap plain text in JSDoc
    content_lines = stripped.splitlines()
    result = [indent + "/**" + line_ending]
    for line in content_lines:
        result.append(indent + " * " + line + line_ending)
    result.append(indent + " */" + line_ending)
    return result


def _collect_js_files(project_path: Path) -> list[Path]:
    """Collect JS/TS files for swagger-jsdoc, skipping common non-source dirs.

    Args:
        project_path: Root directory of the project.

    Returns:
        List of absolute Path objects.
    """
    skip = {"node_modules", ".git", "dist", "build", "coverage"}
    extensions = {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts"}
    results: list[Path] = []

    def _walk(d: Path) -> None:
        try:
            for entry in d.iterdir():
                if entry.is_dir():
                    if entry.name not in skip:
                        _walk(entry)
                elif entry.is_file() and entry.suffix.lower() in extensions:
                    results.append(entry)
        except OSError:
            pass

    _walk(project_path)
    return results
