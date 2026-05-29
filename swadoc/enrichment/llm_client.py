"""LLM client abstraction for Swadoc enrichment.

Provides a Protocol-based interface for LLM providers (Anthropic, OpenAI),
implemented directly over httpx to avoid adding SDK dependencies.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class LLMTimeoutError(Exception):
    """Raised when an LLM call exceeds the configured timeout."""

    def __init__(self, provider: str, timeout: float) -> None:
        super().__init__(
            f"LLM call to {provider!r} timed out after {timeout:.1f}s"
        )
        self.provider = provider
        self.timeout = timeout


class LLMNetworkError(Exception):
    """Raised when a network-level error occurs during an LLM call."""

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"Network error calling {provider!r}: {reason}")
        self.provider = provider
        self.reason = reason


class LLMAuthError(Exception):
    """Raised when the LLM provider returns a 401 or 403 response."""

    def __init__(self, provider: str, status_code: int) -> None:
        super().__init__(
            f"Authentication/authorization error from {provider!r} (HTTP {status_code})"
        )
        self.provider = provider
        self.status_code = status_code


class LLMProviderError(Exception):
    """Raised when the LLM provider returns any other error response."""

    def __init__(self, provider: str, status_code: int, message: str) -> None:
        super().__init__(
            f"Provider error from {provider!r} (HTTP {status_code}): {message}"
        )
        self.provider = provider
        self.status_code = status_code
        self.message = message


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class TokenUsage:
    """Token consumption reported by the LLM provider."""

    input_tokens: int
    output_tokens: int


@dataclass
class LLMResponse:
    """Normalised response returned by any LLMClient implementation."""

    content: str
    usage: TokenUsage
    model: str


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMClient(Protocol):
    """Protocol that all LLM provider clients must satisfy."""

    async def complete(self, prompt: str, timeout: float = 30.0) -> LLMResponse:
        """Send *prompt* to the LLM and return the response.

        Args:
            prompt: The user prompt to send.
            timeout: Maximum seconds to wait for a response (default 30 s).

        Returns:
            An :class:`LLMResponse` with the generated text, token usage, and
            the model identifier echoed by the provider.

        Raises:
            LLMTimeoutError: The call exceeded *timeout* seconds.
            LLMNetworkError: A transport-level error occurred.
            LLMAuthError: The provider returned HTTP 401 or 403.
            LLMProviderError: The provider returned any other error status.
        """
        ...


# ---------------------------------------------------------------------------
# Anthropic implementation
# ---------------------------------------------------------------------------


class AnthropicClient:
    """LLMClient implementation that calls the Anthropic Messages API via httpx.

    Uses ``POST https://api.anthropic.com/v1/messages`` directly — no
    anthropic SDK dependency required.
    """

    _API_URL = "https://api.anthropic.com/v1/messages"
    _ANTHROPIC_VERSION = "2023-06-01"

    def __init__(self, model: str, api_key: str) -> None:
        self._model = model
        self._api_key = api_key

    async def complete(self, prompt: str, timeout: float = 30.0) -> LLMResponse:
        """Call the Anthropic Messages API and return a normalised response.

        Requirements: 4.4 (30-second timeout), 4.5 (timeout → skip),
        4.6 (other failures → skip).
        """
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self._ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        payload = {
            "model": self._model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    self._API_URL, headers=headers, json=payload
                )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("anthropic", timeout) from exc
        except httpx.RequestError as exc:
            raise LLMNetworkError("anthropic", str(exc)) from exc

        if response.status_code in (401, 403):
            raise LLMAuthError("anthropic", response.status_code)

        if response.status_code != 200:
            try:
                error_body = response.json()
                message = error_body.get("error", {}).get("message", response.text)
            except Exception:
                message = response.text
            raise LLMProviderError("anthropic", response.status_code, message)

        data = response.json()
        content_blocks = data.get("content", [])
        text = "".join(
            block.get("text", "")
            for block in content_blocks
            if block.get("type") == "text"
        )
        usage_data = data.get("usage", {})
        usage = TokenUsage(
            input_tokens=usage_data.get("input_tokens", 0),
            output_tokens=usage_data.get("output_tokens", 0),
        )
        return LLMResponse(content=text, usage=usage, model=data.get("model", self._model))


# ---------------------------------------------------------------------------
# OpenAI implementation
# ---------------------------------------------------------------------------


class OpenAIClient:
    """LLMClient implementation that calls the OpenAI Chat Completions API via httpx.

    Uses ``POST https://api.openai.com/v1/chat/completions`` directly — no
    openai SDK dependency required.
    """

    _API_URL = "https://api.openai.com/v1/chat/completions"

    def __init__(self, model: str, api_key: str) -> None:
        self._model = model
        self._api_key = api_key

    async def complete(self, prompt: str, timeout: float = 30.0) -> LLMResponse:
        """Call the OpenAI Chat Completions API and return a normalised response.

        Requirements: 4.4 (30-second timeout), 4.5 (timeout → skip),
        4.6 (other failures → skip).
        """
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
        }

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    self._API_URL, headers=headers, json=payload
                )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("openai", timeout) from exc
        except httpx.RequestError as exc:
            raise LLMNetworkError("openai", str(exc)) from exc

        if response.status_code in (401, 403):
            raise LLMAuthError("openai", response.status_code)

        if response.status_code != 200:
            try:
                error_body = response.json()
                message = (
                    error_body.get("error", {}).get("message", response.text)
                    if isinstance(error_body.get("error"), dict)
                    else response.text
                )
            except Exception:
                message = response.text
            raise LLMProviderError("openai", response.status_code, message)

        data = response.json()
        choices = data.get("choices", [])
        text = choices[0].get("message", {}).get("content", "") if choices else ""
        usage_data = data.get("usage", {})
        usage = TokenUsage(
            input_tokens=usage_data.get("prompt_tokens", 0),
            output_tokens=usage_data.get("completion_tokens", 0),
        )
        return LLMResponse(
            content=text,
            usage=usage,
            model=data.get("model", self._model),
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class LLMClientFactory:
    """Creates the appropriate LLMClient based on provider configuration."""

    _SUPPORTED_PROVIDERS = ("anthropic", "openai")

    @staticmethod
    def create(provider: str, model: str, api_key: str) -> LLMClient:
        """Instantiate an LLMClient for the given *provider*.

        Args:
            provider: One of ``"anthropic"`` or ``"openai"`` (case-sensitive,
                matching Requirement 1.9).
            model: The model identifier to use (e.g. ``"claude-3-5-sonnet-20241022"``).
            api_key: The provider API key.

        Returns:
            A concrete :class:`LLMClient` implementation.

        Raises:
            ValueError: If *provider* is not a supported value.
        """
        if provider == "anthropic":
            return AnthropicClient(model=model, api_key=api_key)
        if provider == "openai":
            return OpenAIClient(model=model, api_key=api_key)
        raise ValueError(
            f"Unsupported LLM provider {provider!r}. "
            f"Accepted values: {', '.join(LLMClientFactory._SUPPORTED_PROVIDERS)}"
        )
