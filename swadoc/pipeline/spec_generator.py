"""
SpecGenerator: invokes the framework's native spec generator and writes output.

Responsibilities:
- Validate the output path extension before invoking the generator.
- Invoke the adapter's generate_spec() to produce an OpenAPI document.
- Write the document to the output path in JSON or YAML format.
- Raise SpecGenerationError on unsupported extension or generator failure.
"""

import json
import logging
from pathlib import Path

import yaml

from swadoc.adapters.protocol import AdapterProtocol
from swadoc.models import SpecGenerationResult

logger = logging.getLogger(__name__)

# Supported output extensions
_JSON_EXTENSION = ".json"
_YAML_EXTENSIONS = {".yaml", ".yml"}
_SUPPORTED_EXTENSIONS = {_JSON_EXTENSION} | _YAML_EXTENSIONS


class SpecGenerationError(Exception):
    """Raised when spec generation or output writing fails.

    This exception signals a fatal error that should prevent pull request
    creation and cause the process to exit with a non-zero status code.

    Attributes:
        message: Human-readable description of the failure.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __repr__(self) -> str:
        return f"SpecGenerationError(message={self.message!r})"


class SpecGenerator:
    """Generates an OpenAPI specification document and writes it to disk.

    The generator delegates document production to the framework adapter's
    ``generate_spec()`` method, then serialises the result to JSON or YAML
    depending on the output path extension.

    Args:
        adapter: A framework adapter implementing ``AdapterProtocol``.
    """

    def __init__(self, adapter: AdapterProtocol) -> None:
        self._adapter = adapter

    def generate(self, project_path: Path, output_path: Path) -> SpecGenerationResult:
        """Generate the OpenAPI spec and write it to *output_path*.

        Steps:
        1. Validate the output path extension (must be ``.json``, ``.yaml``,
           or ``.yml``).  Raises ``SpecGenerationError`` immediately on an
           unsupported extension (Req 6.3).
        2. Invoke ``adapter.generate_spec(project_path, output_path)`` to
           produce the OpenAPI document (Req 6.1).
        3. If the adapter reports failure, raise ``SpecGenerationError`` with
           the failure description (Req 6.4).
        4. Write the document to *output_path* in the appropriate format
           (Req 6.2):
           - ``.json``       → ``json.dumps(document, indent=2)``
           - ``.yaml``/``.yml`` → ``yaml.dump(document)``

        Args:
            project_path: Absolute path to the root of the target codebase.
            output_path: Destination path for the generated OpenAPI document.

        Returns:
            A ``SpecGenerationResult`` with ``success=True`` and the parsed
            document on success.

        Raises:
            SpecGenerationError: When the output extension is unsupported or
                the adapter's spec generator fails.
        """
        # ------------------------------------------------------------------
        # Step 1: Validate output extension FIRST (Req 6.3)
        # ------------------------------------------------------------------
        extension = output_path.suffix.lower()
        if extension not in _SUPPORTED_EXTENSIONS:
            message = (
                f"Unsupported output file extension '{extension}'. "
                f"Supported extensions are: .json, .yaml, .yml"
            )
            logger.error(message)
            raise SpecGenerationError(message)

        # ------------------------------------------------------------------
        # Step 2: Invoke the adapter's spec generator (Req 6.1)
        # ------------------------------------------------------------------
        logger.info(
            "Invoking spec generator for project: %s", project_path
        )
        result: SpecGenerationResult = self._adapter.generate_spec(
            project_path, output_path
        )

        # ------------------------------------------------------------------
        # Step 3: Handle generator failure (Req 6.4)
        # ------------------------------------------------------------------
        if not result.success:
            failure_description = result.error or "unknown error"
            message = (
                f"Spec generator failed: {failure_description}"
            )
            logger.error(message)
            raise SpecGenerationError(message)

        document = result.document

        # ------------------------------------------------------------------
        # Step 4: Write document to output_path (Req 6.2)
        # ------------------------------------------------------------------
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if extension == _JSON_EXTENSION:
            content = json.dumps(document, indent=2)
        else:
            # .yaml or .yml
            content = yaml.dump(document)

        output_path.write_text(content, encoding="utf-8")
        logger.info("OpenAPI document written to: %s", output_path)

        return SpecGenerationResult(success=True, document=document)
