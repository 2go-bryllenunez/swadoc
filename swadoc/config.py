"""Configuration loader for Swadoc.

Loads configuration from .env file and environment variables, applies CLI
overrides, validates all values, and returns a SwadocConfig dataclass.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from swadoc.models import SwadocConfig

# Accepted literal values for validated fields
VALID_CODE_BASES: frozenset[str] = frozenset({"node", "php"})
VALID_LLM_PROVIDERS: frozenset[str] = frozenset({"anthropic", "openai"})

DEFAULT_OUTPUT_PATH = "./openapi.json"


class ConfigError(ValueError):
    """Raised when configuration validation fails."""


def _load_env() -> None:
    """Load .env from the current working directory if it exists.

    python-dotenv's load_dotenv() does not override already-set environment
    variables by default, which is the correct behaviour: real env vars take
    precedence over .env file values.
    """
    env_file = Path.cwd() / ".env"
    load_dotenv(dotenv_path=env_file, override=False)


def load_config(
    *,
    code_base: str | None = None,
    project_path: str | None = None,
    output_path: str | None = None,
    dry_run: bool = False,
    open_pr: bool = False,
) -> SwadocConfig:
    """Build a SwadocConfig from CLI arguments and environment variables.

    CLI arguments take precedence over environment variables (Requirement 1.7).
    The .env file in the current working directory is loaded first so that
    environment variables set in the shell still win over .env values
    (Requirement 1.6).

    Args:
        code_base: Value of --code-base CLI flag, or None if not supplied.
        project_path: Value of --project-path CLI flag, or None if not supplied.
        output_path: Value of --output-path CLI flag, or None if not supplied.
        dry_run: True when --dry-run flag is present on the command line.
        open_pr: True when --open-pr flag is present on the command line.

    Returns:
        A fully-populated SwadocConfig dataclass.

    Raises:
        ConfigError: When any required value is missing or invalid.
    """
    _load_env()

    # --code-base: CLI flag wins; no env-var equivalent (positional config only)
    resolved_code_base: str | None = code_base  # may be None; validated below

    # --project-path: CLI flag wins over PROJECT_PATH env var
    resolved_project_path_str: str | None = project_path or os.environ.get("PROJECT_PATH")

    # --output-path: CLI flag wins over OUTPUT_PATH env var; default to ./openapi.json
    resolved_output_path_str: str = (
        output_path
        or os.environ.get("OUTPUT_PATH")
        or DEFAULT_OUTPUT_PATH
    )

    # LLM settings — environment only (no CLI flags for these)
    llm_provider: str | None = os.environ.get("LLM_PROVIDER") or None
    llm_model: str | None = os.environ.get("LLM_MODEL") or None
    llm_api_key: str | None = os.environ.get("LLM_API_KEY") or None

    # GitHub settings — environment only
    github_token: str | None = os.environ.get("GITHUB_TOKEN") or None
    github_repo: str | None = os.environ.get("GITHUB_REPO") or None

    config = SwadocConfig(
        code_base=resolved_code_base or "",  # placeholder; validate_config checks this
        project_path=Path(resolved_project_path_str) if resolved_project_path_str else Path("."),
        output_path=Path(resolved_output_path_str),
        dry_run=dry_run,
        open_pr=open_pr,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        github_token=github_token,
        github_repo=github_repo,
    )

    # Store whether project_path was explicitly provided so validate_config can
    # distinguish "not supplied" from "supplied but invalid".
    config._project_path_supplied = resolved_project_path_str is not None  # type: ignore[attr-defined]

    validate_config(config)
    return config


def validate_config(config: SwadocConfig) -> None:
    """Validate a SwadocConfig and raise ConfigError on the first category of failure.

    Validation order follows the requirements:
      1. --code-base value (Req 1.1, 1.13)
      2. --project-path existence and readability (Req 1.2, 1.14)
      3. LLM_PROVIDER value when set (Req 1.9, 1.15)

    Missing required values (LLM credentials, GitHub credentials) are checked
    lazily at the point of use (before LLM calls and before PR creation) rather
    than at startup, because they are only required when those features are
    actually invoked (Req 1.8, 1.10, 1.12).

    Args:
        config: The SwadocConfig to validate.

    Raises:
        ConfigError: With a descriptive message naming the invalid value(s).
    """
    # 1. Validate --code-base (Req 1.1, 1.13)
    if not config.code_base:
        raise ConfigError(
            "--code-base is required. Accepted values: node, php."
        )
    if config.code_base not in VALID_CODE_BASES:
        raise ConfigError(
            f"Invalid --code-base value '{config.code_base}'. "
            f"Accepted values: {', '.join(sorted(VALID_CODE_BASES))}."
        )

    # 2. Validate --project-path (Req 1.2, 1.14)
    project_path_supplied = getattr(config, "_project_path_supplied", True)
    if not project_path_supplied:
        raise ConfigError(
            "--project-path is required. Provide an absolute or relative path "
            "to the root directory of the target codebase."
        )
    resolved = config.project_path.resolve()
    if not resolved.exists():
        raise ConfigError(
            f"--project-path '{config.project_path}' does not exist."
        )
    if not resolved.is_dir():
        raise ConfigError(
            f"--project-path '{config.project_path}' is not a directory."
        )
    # Check readability by attempting to list the directory
    try:
        next(resolved.iterdir(), None)
    except PermissionError:
        raise ConfigError(
            f"--project-path '{config.project_path}' is not readable (permission denied)."
        )

    # 3. Validate LLM_PROVIDER when it is set (Req 1.9, 1.15)
    if config.llm_provider is not None and config.llm_provider not in VALID_LLM_PROVIDERS:
        raise ConfigError(
            f"Invalid LLM_PROVIDER value '{config.llm_provider}'. "
            f"Accepted values: {', '.join(sorted(VALID_LLM_PROVIDERS))}."
        )


def validate_llm_config(config: SwadocConfig) -> None:
    """Validate that LLM credentials are present before issuing an LLM request.

    Call this immediately before the first LLM call in the pipeline.

    Args:
        config: The SwadocConfig to check.

    Raises:
        ConfigError: Naming each missing environment variable.
    """
    missing: list[str] = []
    if not config.llm_provider:
        missing.append("LLM_PROVIDER")
    if not config.llm_model:
        missing.append("LLM_MODEL")
    if not config.llm_api_key:
        missing.append("LLM_API_KEY")
    if missing:
        raise ConfigError(
            f"Missing required configuration value(s): {', '.join(missing)}."
        )


def validate_github_config(config: SwadocConfig) -> None:
    """Validate that GitHub credentials are present before creating a pull request.

    Call this immediately before initiating PR creation.

    Args:
        config: The SwadocConfig to check.

    Raises:
        ConfigError: Naming each missing environment variable.
    """
    missing: list[str] = []
    if not config.github_token:
        missing.append("GITHUB_TOKEN")
    if not config.github_repo:
        missing.append("GITHUB_REPO")
    if missing:
        raise ConfigError(
            f"Missing required configuration value(s): {', '.join(missing)}."
        )
