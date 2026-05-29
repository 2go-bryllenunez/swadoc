"""
AdapterRegistry for managing and selecting framework adapters.

The registry is the single point of adapter registration and selection.
It enforces:
  - No duplicate framework_id registrations (Req 10.4)
  - All five required protocol methods must be present (Req 10.4)
  - Selection by code-base flag with framework auto-detection (Req 10.3)
  - 5-second timeout for auto-detection; error lists registered IDs (Req 10.5)
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
from pathlib import Path

from swadoc.adapters.protocol import AdapterProtocol

logger = logging.getLogger(__name__)

# The five operations every adapter must implement (Req 10.1)
_REQUIRED_METHODS: frozenset[str] = frozenset(
    {
        "discover_routes",
        "get_handler_source",
        "get_existing_annotation",
        "write_annotation",
        "generate_spec",
    }
)

# Mapping from code-base flag value to the PHP adapter framework_id
_PHP_FRAMEWORK_ID = "laravel"

# Timeout (seconds) for framework auto-detection within a code-base (Req 10.5)
_DETECTION_TIMEOUT_SECONDS = 5.0


class AdapterRegistrationError(Exception):
    """Raised when an adapter cannot be registered.

    This covers two cases:
    - A duplicate ``framework_id`` is already registered.
    - The adapter class is missing one or more of the five required methods.
    """


class AdapterSelectionError(Exception):
    """Raised when no adapter can be selected for the given code-base.

    Carries the list of registered framework identifiers so the caller can
    surface them in the error message (Req 10.5).
    """

    def __init__(self, message: str, registered_ids: list[str]) -> None:
        super().__init__(message)
        self.registered_ids = registered_ids


class AdapterRegistry:
    """Registry for framework adapters with conflict detection.

    Usage::

        registry = AdapterRegistry()
        registry.register(LaravelAdapter())
        registry.register(AdonisJSAdapter())
        registry.register(ExpressAdapter())

        adapter = registry.select("php", project_path)
    """

    def __init__(self) -> None:
        # Ordered dict preserves insertion order for list_registered()
        self._adapters: dict[str, AdapterProtocol] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, adapter: AdapterProtocol) -> None:
        """Register an adapter.

        Validates that:
        1. The adapter exposes a non-empty ``framework_id`` attribute.
        2. No adapter with the same ``framework_id`` is already registered.
        3. The adapter implements all five required protocol methods.

        Args:
            adapter: An object implementing :class:`AdapterProtocol`.

        Raises:
            AdapterRegistrationError: If the ``framework_id`` conflicts with
                an existing registration, or if any of the five required
                methods are absent.
        """
        framework_id: str = getattr(adapter, "framework_id", "")
        if not framework_id:
            raise AdapterRegistrationError(
                "Adapter must define a non-empty 'framework_id' attribute."
            )

        # Conflict detection (Req 10.4)
        if framework_id in self._adapters:
            raise AdapterRegistrationError(
                f"An adapter with framework_id '{framework_id}' is already "
                "registered. Each framework_id must be unique."
            )

        # Protocol completeness check (Req 10.4)
        missing = _REQUIRED_METHODS - {
            name
            for name in _REQUIRED_METHODS
            if callable(getattr(adapter, name, None))
        }
        if missing:
            sorted_missing = sorted(missing)
            raise AdapterRegistrationError(
                f"Adapter '{framework_id}' is missing required method(s): "
                + ", ".join(sorted_missing)
            )

        self._adapters[framework_id] = adapter
        logger.debug("Registered adapter: %s", framework_id)

    def select(self, code_base: str, project_path: Path) -> AdapterProtocol:
        """Select the appropriate adapter for the given code-base.

        Selection rules:
        - ``"php"``  → always selects the ``"laravel"`` adapter (Req 2.1).
        - ``"node"`` → runs each registered node adapter's ``_detect()``
          method (if present) or falls back to file-based detection within a
          5-second wall-clock timeout (Req 10.5).  The first adapter that
          reports a positive detection is returned.

        Args:
            code_base: Value of the ``--code-base`` CLI flag; ``"php"`` or
                ``"node"``.
            project_path: Absolute path to the root of the target codebase,
                used for framework auto-detection.

        Returns:
            The selected :class:`AdapterProtocol` instance.

        Raises:
            AdapterSelectionError: When no adapter matches within the allowed
                time, listing all registered framework identifiers.
        """
        registered_ids = self.list_registered()

        if code_base == "php":
            return self._select_php(registered_ids)

        if code_base == "node":
            return self._select_node(project_path, registered_ids)

        # Unknown code-base value — surface registered IDs for diagnostics
        raise AdapterSelectionError(
            f"Unknown code-base '{code_base}'. "
            f"Registered adapters: {registered_ids}",
            registered_ids,
        )

    def list_registered(self) -> list[str]:
        """Return the list of registered framework identifiers.

        Returns:
            A list of ``framework_id`` strings in registration order.
        """
        return list(self._adapters.keys())

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _select_php(self, registered_ids: list[str]) -> AdapterProtocol:
        """Select the Laravel adapter for PHP code-bases."""
        adapter = self._adapters.get(_PHP_FRAMEWORK_ID)
        if adapter is None:
            raise AdapterSelectionError(
                f"No adapter registered for PHP (expected framework_id "
                f"'{_PHP_FRAMEWORK_ID}'). "
                f"Registered adapters: {registered_ids}",
                registered_ids,
            )
        return adapter

    def _select_node(
        self, project_path: Path, registered_ids: list[str]
    ) -> AdapterProtocol:
        """Auto-detect the Node.js framework and return the matching adapter.

        Each registered adapter that is *not* the PHP adapter is a candidate.
        Detection runs concurrently with a 5-second overall timeout (Req 10.5).

        Detection strategy per adapter (in priority order):
        1. If the adapter exposes a ``detect(project_path)`` method, call it.
        2. Otherwise fall back to the built-in file-based heuristics:
           - AdonisJS: presence of ``.adonisrc.json`` or ``ace`` file (Req 2.2)
           - Express: ``express`` in ``package.json`` dependencies (Req 2.3)
        """
        node_adapters = [
            adapter
            for fid, adapter in self._adapters.items()
            if fid != _PHP_FRAMEWORK_ID
        ]

        if not node_adapters:
            raise AdapterSelectionError(
                "No Node.js adapters are registered. "
                f"Registered adapters: {registered_ids}",
                registered_ids,
            )

        # Run detection concurrently; honour the 5-second timeout
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(node_adapters)
        ) as executor:
            future_to_adapter = {
                executor.submit(
                    self._run_detection, adapter, project_path
                ): adapter
                for adapter in node_adapters
            }

            detected: AdapterProtocol | None = None
            try:
                for future in concurrent.futures.as_completed(
                    future_to_adapter, timeout=_DETECTION_TIMEOUT_SECONDS
                ):
                    adapter = future_to_adapter[future]
                    try:
                        if future.result():
                            detected = adapter
                            break
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Detection failed for adapter '%s': %s",
                            adapter.framework_id,
                            exc,
                        )
            except concurrent.futures.TimeoutError:
                pass  # Fall through to the error below

        if detected is None:
            raise AdapterSelectionError(
                "No adapter was selected for code-base 'node' within "
                f"{_DETECTION_TIMEOUT_SECONDS:.0f} seconds. "
                f"Registered adapters: {registered_ids}",
                registered_ids,
            )

        logger.debug(
            "Auto-detected Node.js framework: %s", detected.framework_id
        )
        return detected

    @staticmethod
    def _run_detection(
        adapter: AdapterProtocol, project_path: Path
    ) -> bool:
        """Run framework detection for a single adapter.

        Prefers the adapter's own ``detect()`` method when available;
        otherwise applies built-in file-based heuristics.

        Args:
            adapter: The adapter to test.
            project_path: Root directory of the target project.

        Returns:
            True if the adapter's framework is detected, False otherwise.
        """
        # Prefer adapter-provided detection
        detect_fn = getattr(adapter, "detect", None)
        if callable(detect_fn):
            return bool(detect_fn(project_path))

        # Built-in heuristics
        fid = adapter.framework_id
        if fid == "adonisjs":
            return _detect_adonisjs(project_path)
        if fid == "express":
            return _detect_express(project_path)

        # Unknown node adapter — cannot auto-detect
        logger.warning(
            "No detection method available for adapter '%s'; skipping.", fid
        )
        return False


# ---------------------------------------------------------------------------
# Built-in framework detection helpers (Req 2.2, 2.3)
# ---------------------------------------------------------------------------


def _detect_adonisjs(project_path: Path) -> bool:
    """Return True when AdonisJS markers are present in *project_path*.

    Markers: ``.adonisrc.json`` file or ``ace`` file (Req 2.2).
    """
    return (project_path / ".adonisrc.json").exists() or (
        project_path / "ace"
    ).exists()


def _detect_express(project_path: Path) -> bool:
    """Return True when Express is listed as a dependency in *project_path*.

    Checks ``package.json`` ``dependencies`` and ``devDependencies`` for the
    ``express`` package, and confirms no AdonisJS markers are present
    (Req 2.3).
    """
    if _detect_adonisjs(project_path):
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
