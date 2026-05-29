"""Unit tests for swadoc.config — configuration loading and validation.

Validates: Requirements 1.1–1.15
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from swadoc.config import (
    ConfigError,
    load_config,
    validate_config,
    validate_github_config,
    validate_llm_config,
)
from swadoc.models import SwadocConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**kwargs) -> SwadocConfig:
    """Build a minimal valid SwadocConfig for testing validate_config."""
    defaults = {
        "code_base": "php",
        "project_path": Path("."),  # current dir always exists
        "output_path": Path("./openapi.json"),
    }
    defaults.update(kwargs)
    config = SwadocConfig(**defaults)
    # Simulate that project_path was explicitly supplied
    config._project_path_supplied = True  # type: ignore[attr-defined]
    return config


# ---------------------------------------------------------------------------
# validate_config — code_base validation (Req 1.1, 1.13)
# ---------------------------------------------------------------------------


class TestValidateConfigCodeBase:
    def test_valid_php(self):
        config = _make_config(code_base="php")
        validate_config(config)  # should not raise

    def test_valid_node(self):
        config = _make_config(code_base="node")
        validate_config(config)  # should not raise

    def test_empty_code_base_raises(self):
        config = _make_config(code_base="")
        with pytest.raises(ConfigError, match="--code-base is required"):
            validate_config(config)

    def test_invalid_code_base_raises(self):
        config = _make_config(code_base="ruby")
        with pytest.raises(ConfigError, match="Invalid --code-base value 'ruby'"):
            validate_config(config)

    def test_invalid_code_base_message_includes_accepted_values(self):
        config = _make_config(code_base="java")
        with pytest.raises(ConfigError, match="node") as exc_info:
            validate_config(config)
        assert "php" in str(exc_info.value)

    def test_case_sensitive_node(self):
        config = _make_config(code_base="Node")
        with pytest.raises(ConfigError):
            validate_config(config)

    def test_case_sensitive_php(self):
        config = _make_config(code_base="PHP")
        with pytest.raises(ConfigError):
            validate_config(config)


# ---------------------------------------------------------------------------
# validate_config — project_path validation (Req 1.2, 1.14)
# ---------------------------------------------------------------------------


class TestValidateConfigProjectPath:
    def test_existing_directory_passes(self, tmp_path):
        config = _make_config(project_path=tmp_path)
        validate_config(config)  # should not raise

    def test_missing_project_path_raises(self):
        config = _make_config(code_base="php", project_path=Path("."))
        config._project_path_supplied = False  # type: ignore[attr-defined]
        with pytest.raises(ConfigError, match="--project-path is required"):
            validate_config(config)

    def test_nonexistent_path_raises(self, tmp_path):
        nonexistent = tmp_path / "does_not_exist"
        config = _make_config(project_path=nonexistent)
        with pytest.raises(ConfigError, match="does not exist"):
            validate_config(config)

    def test_file_path_raises(self, tmp_path):
        file_path = tmp_path / "somefile.txt"
        file_path.write_text("hello")
        config = _make_config(project_path=file_path)
        with pytest.raises(ConfigError, match="is not a directory"):
            validate_config(config)


# ---------------------------------------------------------------------------
# validate_config — LLM_PROVIDER validation (Req 1.9, 1.15)
# ---------------------------------------------------------------------------


class TestValidateConfigLLMProvider:
    def test_valid_anthropic(self):
        config = _make_config(llm_provider="anthropic")
        validate_config(config)  # should not raise

    def test_valid_openai(self):
        config = _make_config(llm_provider="openai")
        validate_config(config)  # should not raise

    def test_none_provider_passes(self):
        config = _make_config(llm_provider=None)
        validate_config(config)  # should not raise — lazy validation

    def test_invalid_provider_raises(self):
        config = _make_config(llm_provider="cohere")
        with pytest.raises(ConfigError, match="Invalid LLM_PROVIDER value 'cohere'"):
            validate_config(config)

    def test_invalid_provider_message_includes_accepted_values(self):
        config = _make_config(llm_provider="gemini")
        with pytest.raises(ConfigError, match="anthropic") as exc_info:
            validate_config(config)
        assert "openai" in str(exc_info.value)


# ---------------------------------------------------------------------------
# validate_llm_config — lazy LLM credential validation (Req 1.8, 1.12)
# ---------------------------------------------------------------------------


class TestValidateLLMConfig:
    def test_all_present_passes(self):
        config = _make_config(
            llm_provider="anthropic",
            llm_model="claude-3-5-sonnet-20241022",
            llm_api_key="sk-ant-test",
        )
        validate_llm_config(config)  # should not raise

    def test_missing_provider_raises(self):
        config = _make_config(llm_provider=None, llm_model="gpt-4o", llm_api_key="sk-test")
        with pytest.raises(ConfigError, match="LLM_PROVIDER"):
            validate_llm_config(config)

    def test_missing_model_raises(self):
        config = _make_config(llm_provider="openai", llm_model=None, llm_api_key="sk-test")
        with pytest.raises(ConfigError, match="LLM_MODEL"):
            validate_llm_config(config)

    def test_missing_api_key_raises(self):
        config = _make_config(llm_provider="openai", llm_model="gpt-4o", llm_api_key=None)
        with pytest.raises(ConfigError, match="LLM_API_KEY"):
            validate_llm_config(config)

    def test_all_missing_names_all_in_error(self):
        config = _make_config(llm_provider=None, llm_model=None, llm_api_key=None)
        with pytest.raises(ConfigError) as exc_info:
            validate_llm_config(config)
        msg = str(exc_info.value)
        assert "LLM_PROVIDER" in msg
        assert "LLM_MODEL" in msg
        assert "LLM_API_KEY" in msg


# ---------------------------------------------------------------------------
# validate_github_config — lazy GitHub credential validation (Req 1.10, 1.12)
# ---------------------------------------------------------------------------


class TestValidateGitHubConfig:
    def test_all_present_passes(self):
        config = _make_config(github_token="ghp_test", github_repo="owner/repo")
        validate_github_config(config)  # should not raise

    def test_missing_token_raises(self):
        config = _make_config(github_token=None, github_repo="owner/repo")
        with pytest.raises(ConfigError, match="GITHUB_TOKEN"):
            validate_github_config(config)

    def test_missing_repo_raises(self):
        config = _make_config(github_token="ghp_test", github_repo=None)
        with pytest.raises(ConfigError, match="GITHUB_REPO"):
            validate_github_config(config)

    def test_both_missing_names_both_in_error(self):
        config = _make_config(github_token=None, github_repo=None)
        with pytest.raises(ConfigError) as exc_info:
            validate_github_config(config)
        msg = str(exc_info.value)
        assert "GITHUB_TOKEN" in msg
        assert "GITHUB_REPO" in msg


# ---------------------------------------------------------------------------
# load_config — integration-style tests (Req 1.1–1.15)
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_valid_config_returns_swadoc_config(self, tmp_path):
        config = load_config(code_base="php", project_path=str(tmp_path))
        assert config.code_base == "php"
        assert config.project_path == tmp_path
        assert config.dry_run is False
        assert config.open_pr is False

    def test_default_output_path(self, tmp_path):
        """Req 1.11: default output path is ./openapi.json."""
        config = load_config(code_base="node", project_path=str(tmp_path))
        assert config.output_path == Path("./openapi.json")

    def test_custom_output_path(self, tmp_path):
        config = load_config(
            code_base="php",
            project_path=str(tmp_path),
            output_path="./docs/api.yaml",
        )
        assert config.output_path == Path("./docs/api.yaml")

    def test_dry_run_flag(self, tmp_path):
        config = load_config(code_base="php", project_path=str(tmp_path), dry_run=True)
        assert config.dry_run is True

    def test_open_pr_flag(self, tmp_path):
        config = load_config(code_base="php", project_path=str(tmp_path), open_pr=True)
        assert config.open_pr is True

    def test_invalid_code_base_raises_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match="Invalid --code-base"):
            load_config(code_base="ruby", project_path=str(tmp_path))

    def test_missing_project_path_raises_config_error(self):
        with pytest.raises(ConfigError, match="--project-path is required"):
            load_config(code_base="php", project_path=None)

    def test_nonexistent_project_path_raises_config_error(self, tmp_path):
        nonexistent = str(tmp_path / "no_such_dir")
        with pytest.raises(ConfigError, match="does not exist"):
            load_config(code_base="php", project_path=nonexistent)

    def test_env_var_project_path(self, tmp_path, monkeypatch):
        """Req 1.6: load PROJECT_PATH from environment."""
        monkeypatch.setenv("PROJECT_PATH", str(tmp_path))
        config = load_config(code_base="php")
        assert config.project_path == tmp_path

    def test_cli_overrides_env_var_project_path(self, tmp_path, monkeypatch):
        """Req 1.7: CLI flag takes precedence over env var."""
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        monkeypatch.setenv("PROJECT_PATH", str(tmp_path))
        config = load_config(code_base="php", project_path=str(other_dir))
        assert config.project_path == other_dir

    def test_env_var_output_path(self, tmp_path, monkeypatch):
        """Req 1.6: load OUTPUT_PATH from environment."""
        monkeypatch.setenv("OUTPUT_PATH", "./custom-output.json")
        config = load_config(code_base="php", project_path=str(tmp_path))
        assert config.output_path == Path("./custom-output.json")

    def test_cli_output_path_overrides_env(self, tmp_path, monkeypatch):
        """Req 1.7: CLI --output-path overrides OUTPUT_PATH env var."""
        monkeypatch.setenv("OUTPUT_PATH", "./env-output.json")
        config = load_config(
            code_base="php",
            project_path=str(tmp_path),
            output_path="./cli-output.json",
        )
        assert config.output_path == Path("./cli-output.json")

    def test_llm_env_vars_loaded(self, tmp_path, monkeypatch):
        """Req 1.6: LLM settings loaded from environment."""
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_MODEL", "gpt-4o")
        monkeypatch.setenv("LLM_API_KEY", "sk-test-key")
        config = load_config(code_base="php", project_path=str(tmp_path))
        assert config.llm_provider == "openai"
        assert config.llm_model == "gpt-4o"
        assert config.llm_api_key == "sk-test-key"

    def test_github_env_vars_loaded(self, tmp_path, monkeypatch):
        """Req 1.6: GitHub settings loaded from environment."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
        monkeypatch.setenv("GITHUB_REPO", "myorg/myrepo")
        config = load_config(code_base="php", project_path=str(tmp_path))
        assert config.github_token == "ghp_test_token"
        assert config.github_repo == "myorg/myrepo"

    def test_invalid_llm_provider_raises(self, tmp_path, monkeypatch):
        """Req 1.15: invalid LLM_PROVIDER raises ConfigError."""
        monkeypatch.setenv("LLM_PROVIDER", "cohere")
        with pytest.raises(ConfigError, match="Invalid LLM_PROVIDER"):
            load_config(code_base="php", project_path=str(tmp_path))
