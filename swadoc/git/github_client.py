"""
GitHub API client for pull request creation.

Implements exponential backoff for rate-limited responses and raises
structured exceptions for auth failures and retry exhaustion.

Requirements: 7.4–7.9
"""

from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GITHUB_API_BASE = "https://api.github.com"
_PR_TITLE = "docs: auto-enrich swagger annotations"
_DESCRIPTION_CAP = 65_000
_TRUNCATION_NOTICE = "\n\n---\n*Description truncated: exceeded 65000 character limit.*"

# Exponential backoff settings (Req 7.7)
_BACKOFF_START_SECONDS = 1
_BACKOFF_MAX_SECONDS = 60
_MAX_RETRY_ATTEMPTS = 5


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class GitHubAPIError(Exception):
    """Raised for unexpected GitHub API errors (non-auth, non-rate-limit)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubAuthError(GitHubAPIError):
    """Raised on HTTP 401 or HTTP 403 (non-rate-limit) responses (Req 7.9)."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message, status_code=status_code)


class GitHubRateLimitError(GitHubAPIError):
    """Raised when all retry attempts for a rate-limited request are exhausted (Req 7.8)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


# ---------------------------------------------------------------------------
# GitHubClient
# ---------------------------------------------------------------------------


class GitHubClient:
    """Async GitHub API client for PR creation.

    Args:
        token: GitHub personal access token or app token.
        repo: Repository in ``owner/repo`` format.
    """

    def __init__(self, token: str, repo: str) -> None:
        self.token = token
        self.repo = repo
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_pr(self, branch: str, description: str) -> str:
        """Create a pull request via the GitHub API.

        Targets the repository's default branch as the base (Req 7.4).
        Caps the description at 65000 characters and appends a truncation
        notice if exceeded (Req 7.5).

        Implements exponential backoff for HTTP 429 or HTTP 403 with
        ``X-RateLimit-Remaining: 0`` (Req 7.7).  Raises
        :class:`GitHubRateLimitError` on retry exhaustion (Req 7.8).
        Raises :class:`GitHubAuthError` on HTTP 401 or non-rate-limit
        HTTP 403 (Req 7.9).

        Args:
            branch: The head branch name for the PR.
            description: The PR body text.

        Returns:
            The URL of the created pull request.

        Raises:
            GitHubAuthError: HTTP 401 or non-rate-limit HTTP 403.
            GitHubRateLimitError: Rate-limit retries exhausted.
            GitHubAPIError: Any other unexpected API error.
        """
        capped_description = self._cap_description(description)
        default_branch = await self.get_default_branch()

        payload = {
            "title": _PR_TITLE,
            "head": branch,
            "base": default_branch,
            "body": capped_description,
        }

        url = f"{_GITHUB_API_BASE}/repos/{self.repo}/pulls"
        response = await self._request_with_backoff("POST", url, json=payload)
        data = response.json()
        return data["html_url"]

    async def get_default_branch(self) -> str:
        """Fetch the repository's default branch name.

        Uses ``GET /repos/{owner}/{repo}`` and reads the ``default_branch``
        field from the response.

        Returns:
            The default branch name (e.g. ``"main"`` or ``"master"``).

        Raises:
            GitHubAuthError: HTTP 401 or non-rate-limit HTTP 403.
            GitHubRateLimitError: Rate-limit retries exhausted.
            GitHubAPIError: Any other unexpected API error.
        """
        url = f"{_GITHUB_API_BASE}/repos/{self.repo}"
        response = await self._request_with_backoff("GET", url)
        data = response.json()
        return data["default_branch"]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cap_description(description: str) -> str:
        """Cap description at 65000 chars, appending a truncation notice if needed (Req 7.5)."""
        if len(description) <= _DESCRIPTION_CAP:
            return description
        # Reserve space for the truncation notice inside the cap
        cutoff = _DESCRIPTION_CAP - len(_TRUNCATION_NOTICE)
        return description[:cutoff] + _TRUNCATION_NOTICE

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        """Return True if the response indicates a rate-limit condition (Req 7.7)."""
        if response.status_code == 429:
            return True
        if response.status_code == 403:
            remaining = response.headers.get("X-RateLimit-Remaining", "")
            if remaining == "0":
                return True
        return False

    async def _request_with_backoff(
        self,
        method: str,
        url: str,
        **kwargs,
    ) -> httpx.Response:
        """Execute an HTTP request with exponential backoff on rate-limit responses.

        Backoff schedule (Req 7.7):
        - Start: 1 second
        - Double each attempt, capped at 60 seconds
        - Maximum 5 attempts total

        Args:
            method: HTTP method (``"GET"``, ``"POST"``, etc.).
            url: Full request URL.
            **kwargs: Additional keyword arguments forwarded to ``httpx.AsyncClient.request``.

        Returns:
            The successful :class:`httpx.Response`.

        Raises:
            GitHubAuthError: HTTP 401 or non-rate-limit HTTP 403.
            GitHubRateLimitError: All retry attempts exhausted.
            GitHubAPIError: Unexpected non-success status code.
        """
        wait = _BACKOFF_START_SECONDS
        attempt = 0

        async with httpx.AsyncClient() as client:
            while True:
                attempt += 1
                response = await client.request(
                    method,
                    url,
                    headers=self._headers,
                    **kwargs,
                )

                # Success
                if response.is_success:
                    return response

                # Auth failure — abort immediately (Req 7.9)
                if response.status_code == 401:
                    raise GitHubAuthError(
                        f"GitHub API authentication failed (HTTP 401). "
                        f"Check that GITHUB_TOKEN is valid.",
                        status_code=401,
                    )

                if response.status_code == 403 and not self._is_rate_limited(response):
                    raise GitHubAuthError(
                        f"GitHub API authorization failure (HTTP 403). "
                        f"The token may lack required permissions for {self.repo}.",
                        status_code=403,
                    )

                # Rate-limit — retry with backoff (Req 7.7)
                if self._is_rate_limited(response):
                    if attempt >= _MAX_RETRY_ATTEMPTS:
                        raise GitHubRateLimitError(
                            f"GitHub API rate limit retry exhausted after {_MAX_RETRY_ATTEMPTS} "
                            f"attempts for {method} {url}. "
                            "Local branch and commit have been preserved."
                        )
                    logger.warning(
                        "GitHub rate limit hit (attempt %d/%d). Waiting %ds before retry.",
                        attempt,
                        _MAX_RETRY_ATTEMPTS,
                        wait,
                    )
                    await asyncio.sleep(wait)
                    wait = min(wait * 2, _BACKOFF_MAX_SECONDS)
                    continue

                # Any other non-success status
                raise GitHubAPIError(
                    f"GitHub API returned unexpected status {response.status_code} "
                    f"for {method} {url}: {response.text[:200]}",
                    status_code=response.status_code,
                )
