"""CLI entry point for Swadoc using Click.

Defines the command-line interface, parses flags, loads configuration via
``swadoc.config.load_config``, and delegates to the pipeline orchestrator.
"""

from __future__ import annotations

import asyncio
import sys
import traceback

import click

from swadoc.adapters.adonisjs import AdonisJSAdapter
from swadoc.adapters.express import ExpressAdapter
from swadoc.adapters.laravel import LaravelAdapter
from swadoc.adapters.registry import AdapterRegistry, AdapterSelectionError
from swadoc.config import ConfigError, load_config
from swadoc.pipeline.orchestrator import PipelineOrchestrator


def _build_registry() -> AdapterRegistry:
    """Register all three framework adapters and return the registry."""
    registry = AdapterRegistry()
    registry.register(LaravelAdapter())
    registry.register(AdonisJSAdapter())
    registry.register(ExpressAdapter())
    return registry


@click.command()
@click.option(
    "--code-base",
    required=True,
    help="Target framework language. Accepted values: node, php.",
)
@click.option(
    "--project-path",
    required=True,
    help="Absolute or relative path to the root directory of the target codebase.",
)
@click.option(
    "--output-path",
    default=None,
    help="Absolute or relative path for the generated OpenAPI document. "
    "Defaults to ./openapi.json.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Perform all checks and LLM calls but write no files and open no pull requests.",
)
@click.option(
    "--open-pr",
    is_flag=True,
    default=False,
    help="Open a GitHub pull request with all generated changes after the run.",
)
def main(
    code_base: str,
    project_path: str,
    output_path: str | None,
    dry_run: bool,
    open_pr: bool,
) -> None:
    """Swadoc — Swagger Auto-Documentation Script.

    Crawls a target codebase's registered API routes, evaluates annotation
    quality, enriches underdocumented routes using an LLM, generates a valid
    OpenAPI 3.0 specification, and optionally opens a GitHub pull request.
    """
    try:
        config = load_config(
            code_base=code_base,
            project_path=project_path,
            output_path=output_path,
            dry_run=dry_run,
            open_pr=open_pr,
        )
    except ConfigError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    # Select the appropriate adapter
    registry = _build_registry()
    try:
        adapter = registry.select(config.code_base, config.project_path)
    except AdapterSelectionError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    # Run the pipeline
    orchestrator = PipelineOrchestrator(config=config, adapter=adapter)
    try:
        asyncio.run(orchestrator.run())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        click.echo("\nInterrupted.", err=True)
        sys.exit(1)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
