"""
AdapterProtocol definition for Swadoc framework adapters.

All framework adapters (Laravel, AdonisJS, Express, and any future adapters)
must implement this protocol to be usable with the AdapterRegistry.
"""

from pathlib import Path
from typing import Protocol, runtime_checkable

from swadoc.models import (
    AnnotationBlock,
    HandlerSource,
    RouteRecord,
    SpecGenerationResult,
    WriteResult,
)


@runtime_checkable
class AdapterProtocol(Protocol):
    """Protocol that all framework adapters must implement.

    Each adapter is responsible for a specific framework (e.g., Laravel,
    AdonisJS, Express) and provides the five core operations needed by the
    Swadoc pipeline.

    Attributes:
        framework_id: Unique identifier for the framework, e.g. "laravel",
            "adonisjs", "express". Used by AdapterRegistry for selection.
    """

    framework_id: str

    def discover_routes(self, project_path: Path) -> list[RouteRecord]:
        """List all registered routes in the project.

        Args:
            project_path: Absolute path to the root of the target codebase.

        Returns:
            A list of RouteRecord entries. Each entry has non-empty ``method``
            and ``uri`` fields. ``handler_file`` and ``handler_function`` may
            be None when they cannot be resolved (a warning is logged in that
            case).

        Raises:
            AdapterOperationError: When route discovery fails (e.g. subprocess
                exits non-zero, timeout, or unparseable output).
        """
        ...

    def get_handler_source(
        self, route: RouteRecord, project_path: Path
    ) -> HandlerSource:
        """Read the source code of a route's handler function.

        Args:
            route: The route whose handler source should be read.
            project_path: Absolute path to the root of the target codebase.

        Returns:
            A HandlerSource containing the function body, file path, line
            range, and truncation metadata.

        Raises:
            AdapterOperationError: When the handler file does not exist, is
                not readable, or the named function cannot be located.
        """
        ...

    def get_existing_annotation(
        self, route: RouteRecord, project_path: Path
    ) -> AnnotationBlock | None:
        """Extract the existing documentation annotation for a route handler.

        Args:
            route: The route whose annotation should be extracted.
            project_path: Absolute path to the root of the target codebase.

        Returns:
            The parsed AnnotationBlock if one exists immediately before the
            handler function definition, or None if no annotation is present.

        Raises:
            AdapterOperationError: When the handler file cannot be read.
        """
        ...

    def write_annotation(
        self,
        route: RouteRecord,
        annotation: AnnotationBlock,
        project_path: Path,
    ) -> WriteResult:
        """Write a validated annotation block to the handler's source file.

        The annotation is inserted on the lines immediately preceding the
        handler function definition. The file's existing line endings and the
        indentation of the function definition line are preserved. No other
        lines in the file are modified.

        Args:
            route: The route whose handler should be annotated.
            annotation: The validated AnnotationBlock to write.
            project_path: Absolute path to the root of the target codebase.

        Returns:
            A WriteResult indicating success or failure with an error message.
        """
        ...

    def generate_spec(
        self, project_path: Path, output_path: Path
    ) -> SpecGenerationResult:
        """Invoke the framework's native spec generator.

        Args:
            project_path: Absolute path to the root of the target codebase.
            output_path: Destination path for the generated OpenAPI document.

        Returns:
            A SpecGenerationResult with the parsed document on success, or an
            error description on failure.
        """
        ...


class AdapterOperationError(Exception):
    """Raised by adapter operations when a structured failure occurs.

    This exception carries enough context for the orchestrator to record the
    failure in the run report without terminating the overall pipeline.

    Attributes:
        operation: Name of the failed operation (e.g. "discover_routes").
        route: The RouteRecord being processed, or None for non-route ops.
        file_path: The source file involved, or None when not applicable.
        message: Human-readable description of the failure.
    """

    def __init__(
        self,
        operation: str,
        message: str,
        route: RouteRecord | None = None,
        file_path: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.message = message
        self.route = route
        self.file_path = file_path

    def __repr__(self) -> str:
        return (
            f"AdapterOperationError(operation={self.operation!r}, "
            f"message={self.message!r}, route={self.route!r}, "
            f"file_path={self.file_path!r})"
        )
