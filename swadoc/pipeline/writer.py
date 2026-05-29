"""
AnnotationWriter: writes validated annotation blocks to source files.

Responsibilities:
- Dry-run mode: log what would be modified without touching any file.
- Backup-before-write: call BackupManager before the first modification of
  each file; abort (re-raise BackupError) if the backup fails.
- Delegate the actual text insertion to the adapter's write_annotation().
- Post-write syntax check: lightweight structural check (balanced /** / */
  doc-comment delimiters and presence of the function definition) for PHP
  and JS/TS files.  No external interpreter is required.
- On syntax error: restore from backup and return a failure WriteResult.
"""

import logging
import re
from pathlib import Path

from swadoc.models import AnnotationBlock, RouteRecord, WriteResult
from swadoc.pipeline.backup import BackupError, BackupManager

logger = logging.getLogger(__name__)

# File extensions that use the PHP structural checker
_PHP_EXTENSIONS = {".php"}

# File extensions that use the JS/TS structural checker
_NODE_EXTENSIONS = {".js", ".ts", ".mjs", ".cjs"}


class AnnotationWriter:
    """Writes validated annotation blocks to source files safely.

    The writer coordinates three concerns:
    1. Dry-run mode — log intent, return success without touching files.
    2. Backup safety — ensure a backup exists before the first write to a
       file; abort if the backup cannot be created.
    3. Post-write validation — run a lightweight structural check on the
       modified file and roll back on any detected error.

    Args:
        adapter: A framework adapter whose ``write_annotation()`` method
            performs the actual text insertion.
        backup_manager: A BackupManager instance for backup/restore.
        dry_run: When True, no files are written or created.
    """

    def __init__(
        self,
        adapter,
        backup_manager: BackupManager,
        dry_run: bool = False,
    ) -> None:
        self._adapter = adapter
        self._backup_manager = backup_manager
        self._dry_run = dry_run

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write(
        self,
        route: RouteRecord,
        annotation: AnnotationBlock,
        project_path: Path,
    ) -> WriteResult:
        """Write *annotation* for *route* into the appropriate source file.

        Steps:
        1. Resolve the target file path from the route record.
        2. In dry-run mode: log the intent and return success immediately.
        3. Backup the file before the first modification (via BackupManager).
           Re-raise BackupError if the backup fails so the caller can abort.
        4. Delegate the write to ``adapter.write_annotation()``.
        5. Re-parse the modified file to verify no syntax errors were
           introduced.  On failure: restore from backup and return a failure
           WriteResult.

        Args:
            route: The route whose handler should be annotated.
            annotation: The validated AnnotationBlock to write.
            project_path: Absolute path to the root of the target codebase.

        Returns:
            WriteResult with ``success=True`` on success, or
            ``success=False`` with an ``error`` message on failure.

        Raises:
            BackupError: If the backup cannot be created (caller must abort
                the run without modifying the source file).
        """
        # Resolve the file path we are about to modify
        file_path = self._resolve_file_path(route, project_path)

        # ------------------------------------------------------------------
        # Dry-run: log and return without touching anything (Req 5.4, 5.5)
        # ------------------------------------------------------------------
        if self._dry_run:
            logger.info(
                "[DRY RUN] Would modify: %s (%s %s)",
                file_path,
                route.method,
                route.uri,
            )
            return WriteResult(success=True, file_path=file_path)

        # ------------------------------------------------------------------
        # Backup before first modification (Req 5.2, 5.3)
        # BackupError is intentionally NOT caught here — the caller must
        # abort the run when a backup fails (Req 5.3).
        # ------------------------------------------------------------------
        self._backup_manager.backup_file(file_path)

        # ------------------------------------------------------------------
        # Delegate the actual write to the adapter (Req 5.1)
        # ------------------------------------------------------------------
        write_result = self._adapter.write_annotation(route, annotation, project_path)

        if not write_result.success:
            # Adapter already failed — return its result as-is
            return write_result

        # ------------------------------------------------------------------
        # Post-write syntax check (Req 5.6, 5.7)
        # ------------------------------------------------------------------
        parse_error = self._check_syntax(file_path, route)
        if parse_error is not None:
            logger.warning(
                "Post-write syntax error in %s (%s %s): %s — restoring from backup",
                file_path,
                route.method,
                route.uri,
                parse_error,
            )
            self._backup_manager.restore_file(file_path)
            return WriteResult(
                success=False,
                file_path=file_path,
                error=f"post-write parse failure: {parse_error}",
            )

        return WriteResult(success=True, file_path=file_path)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_file_path(self, route: RouteRecord, project_path: Path) -> Path:
        """Return the absolute path of the file to be modified.

        Uses ``route.handler_file`` when available; falls back to
        ``project_path`` itself (which will cause the adapter to fail
        gracefully with its own error).
        """
        if route.handler_file is not None:
            p = Path(route.handler_file)
            if not p.is_absolute():
                p = project_path / p
            return p.resolve()
        # No handler_file — return a sentinel so the adapter can report the
        # error; the writer itself does not raise here.
        return project_path.resolve()

    def _check_syntax(self, file_path: Path, route: RouteRecord) -> str | None:
        """Run a lightweight structural check on *file_path*.

        This is a fast, interpreter-free check that verifies:
        - ``/**`` and ``*/`` doc-comment delimiters are balanced.
        - The handler function definition is still present in the file.

        Returns:
            None when the file passes the structural check.
            A non-empty error string when a structural problem is detected or
            the file cannot be read.
        """
        suffix = file_path.suffix.lower()

        if suffix in _PHP_EXTENSIONS:
            return self._structural_check(file_path, route, language="php")

        if suffix in _NODE_EXTENSIONS:
            return self._structural_check(file_path, route, language="js")

        # Unknown extension — skip syntax check and assume success
        logger.debug(
            "No syntax checker configured for extension '%s'; skipping post-write check for %s",
            suffix,
            file_path,
        )
        return None

    def _structural_check(
        self, file_path: Path, route: RouteRecord, language: str
    ) -> str | None:
        """Perform a language-appropriate structural check on *file_path*.

        Checks:
        1. The file can be read without error.
        2. The count of ``/**`` opening delimiters equals the count of
           ``*/`` closing delimiters (balanced doc-comment blocks).
        3. The handler function definition is still present in the file.

        Args:
            file_path: Path to the modified source file.
            route: The route record (used to locate the function definition).
            language: ``"php"`` or ``"js"`` — controls the function-definition
                      pattern used in check 3.

        Returns:
            None on success, or an error string describing the first failure.
        """
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"cannot read file after write: {exc}"

        # ------------------------------------------------------------------
        # Check 1: balanced /** ... */ doc-comment delimiters
        # ------------------------------------------------------------------
        open_count = content.count("/**")
        close_count = content.count("*/")
        if open_count != close_count:
            return (
                f"unbalanced doc-comment delimiters: "
                f"{open_count} opening '/**' vs {close_count} closing '*/'"
            )

        # ------------------------------------------------------------------
        # Check 2: handler function definition still present
        # ------------------------------------------------------------------
        if route.handler_function:
            if not self._function_definition_present(
                content, route.handler_function, language
            ):
                return (
                    f"handler function definition '{route.handler_function}' "
                    f"not found after write"
                )

        return None

    @staticmethod
    def _function_definition_present(
        content: str, function_name: str, language: str
    ) -> bool:
        """Return True if *function_name* is defined somewhere in *content*.

        Uses a simple regex that matches common function-definition patterns
        for PHP and JS/TS without requiring a full parser.

        PHP patterns matched:
        - ``function functionName(``
        - ``public function functionName(``
        - ``protected function functionName(``
        - ``private function functionName(``
        - ``static function functionName(``
        - ``public static function functionName(``
        - etc.

        JS/TS patterns matched:
        - ``function functionName(``
        - ``async function functionName(``
        - ``functionName(`` (method shorthand in class body)
        - ``functionName = function(``
        - ``functionName = async function(``
        - ``functionName = (`` (arrow function assignment)
        - ``functionName = async (`` (async arrow function assignment)
        """
        escaped = re.escape(function_name)

        if language == "php":
            # Match any visibility/static modifier combination followed by
            # "function <name>(" — covers standalone and class methods.
            pattern = (
                r"(?:(?:public|protected|private|static|abstract|final)\s+)*"
                r"function\s+" + escaped + r"\s*\("
            )
        else:
            # JS/TS: function declarations, async functions, method shorthands,
            # and arrow/regular function assignments.
            # Pattern covers:
            #   function name(          — function declaration
            #   async function name(    — async function declaration
            #   name(                   — method shorthand in class body
            #   async name(             — async method shorthand
            #   name = function(        — function expression assignment
            #   name = async function(  — async function expression assignment
            #   name = (                — arrow function assignment
            #   name = async (          — async arrow function assignment
            pattern = (
                r"(?:"
                r"(?:async\s+)?function\s+" + escaped + r"\s*\("
                r"|"
                r"(?:async\s+)?" + escaped + r"\s*\("
                r"|"
                + escaped + r"\s*=\s*(?:async\s+)?(?:function\s*)?\("
                r")"
            )

        return bool(re.search(pattern, content, re.MULTILINE))
