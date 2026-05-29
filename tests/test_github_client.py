"""Unit tests for swadoc.git.github_client.GitHubClient.

Validates: Requirements 7.4–7.9
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from swadoc.git.github_client import (
    GitHubAuthError,
    GitHubClient,
    GitHubRateLimitError,
    _DESCRIPTION_CAP,
    _TRUNCATION_NOTICE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(token: str = "ghp_test", repo: str = "owner/repo") -> GitHubClient:
    return GitHubClient(token=token, repo=repo)


def _mock_response(
    status_code: int = 200,
    json_data: dict | None = None,
    headers: dict | None = None,
) -> MagicMock:
    """Build a fake httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.json.return_value = json_data or {}
    resp.headers = httpx.Headers(headers or {})
    resp.text = str(json_data or "")
    return resp


# ---------------------------------------------------------------------------
# _cap_description (Req 7.5)
# ---------------------------------------------------------------------------


class TestCapDescription:
    def test_short_description_unchanged(self):
        client = _make_client()
        desc = "Short description"
        assert client._cap_description(desc) == desc

    def test_exactly_at_cap_unchanged(self):
        client = _make_client()
        desc = "A" * _DESCRIPTION_CAP
        result = client._cap_description(desc)
        assert result == desc

    def test_over_cap_truncated_with_notice(self):
        client = _make_client()
        desc = "B" * (_DESCRIPTION_CAP + 1000)
        result = client._cap_description(desc)
        assert len(result) <= _DESCRIPTION_CAP
        assert result.endswith(_TRUNCATION_NOTICE)

    def test_truncated_result_length_at_most_cap(self):
        client = _make_client()
        desc = "C" * 100_000
        result = client._cap_description(desc)
        assert len(result) <= _DESCRIPTION_CAP


# ---------------------------------------------------------------------------
# _is_rate_limited (Req 7.7)
# ---------------------------------------------------------------------------


class TestIsRateLimited:
    def test_429_is_rate_limited(self):
        resp = _mock_response(status_code=429)
        assert GitHubClient._is_rate_limited(resp) is True

    def test_403_with_remaining_zero_is_rate_limited(self):
        resp = _mock_response(
            status_code=403,
            headers={"X-RateLimit-Remaining": "0"},
        )
        assert GitHubClient._is_rate_limited(resp) is True

    def test_403_without_rate_limit_header_not_rate_limited(self):
        resp = _mock_response(status_code=403)
        assert GitHubClient._is_rate_limited(resp) is False

    def test_403_with_remaining_nonzero_not_rate_limited(self):
        resp = _mock_response(
            status_code=403,
            headers={"X-RateLimit-Remaining": "10"},
        )
        assert GitHubClient._is_rate_limited(resp) is False

    def test_200_not_rate_limited(self):
        resp = _mock_response(status_code=200)
        assert GitHubClient._is_rate_limited(resp) is False


# ---------------------------------------------------------------------------
# get_default_branch (Req 7.4)
# ---------------------------------------------------------------------------


class TestGetDefaultBranch:
    @pytest.mark.asyncio
    async def test_returns_default_branch_from_api(self):
        client = _make_client()
        repo_response = _mock_response(
            status_code=200,
            json_data={"default_branch": "main", "name": "repo"},
        )

        with patch("httpx.AsyncClient") as MockAsyncClient:
            mock_http = AsyncMock()
            MockAsyncClient.return_value.__aenter__.return_value = mock_http
            mock_http.request.return_value = repo_response

            branch = await client.get_default_branch()

        assert branch == "main"

    @pytest.mark.asyncio
    async def test_uses_correct_url(self):
        client = _make_client(repo="myorg/myrepo")
        repo_response = _mock_response(
            status_code=200,
            json_data={"default_branch": "develop"},
        )
        captured_url = {}

        with patch("httpx.AsyncClient") as MockAsyncClient:
            mock_http = AsyncMock()
            MockAsyncClient.return_value.__aenter__.return_value = mock_http

            async def capture_request(method, url, **kwargs):
                captured_url["url"] = url
                return repo_response

            mock_http.request.side_effect = capture_request

            await client.get_default_branch()

        assert captured_url["url"] == "https://api.github.com/repos/myorg/myrepo"

    @pytest.mark.asyncio
    async def test_raises_auth_error_on_401(self):
        client = _make_client()
        auth_response = _mock_response(status_code=401)

        with patch("httpx.AsyncClient") as MockAsyncClient:
            mock_http = AsyncMock()
            MockAsyncClient.return_value.__aenter__.return_value = mock_http
            mock_http.request.return_value = auth_response

            with pytest.raises(GitHubAuthError) as exc_info:
                await client.get_default_branch()

        assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# create_pr — success path (Req 7.4)
# ---------------------------------------------------------------------------


class TestCreatePrSuccess:
    @pytest.mark.asyncio
    async def test_returns_pr_url(self):
        client = _make_client()
        repo_resp = _mock_response(200, {"default_branch": "main"})
        pr_resp = _mock_response(201, {"html_url": "https://github.com/owner/repo/pull/1"})

        with patch("httpx.AsyncClient") as MockAsyncClient:
            mock_http = AsyncMock()
            MockAsyncClient.return_value.__aenter__.return_value = mock_http
            mock_http.request.side_effect = [repo_resp, pr_resp]

            url = await client.create_pr("my-branch", "description")

        assert url == "https://github.com/owner/repo/pull/1"

    @pytest.mark.asyncio
    async def test_pr_payload_contains_required_fields(self):
        client = _make_client()
        repo_resp = _mock_response(200, {"default_branch": "main"})
        pr_resp = _mock_response(201, {"html_url": "https://github.com/owner/repo/pull/2"})
        captured_payload = {}

        with patch("httpx.AsyncClient") as MockAsyncClient:
            mock_http = AsyncMock()
            MockAsyncClient.return_value.__aenter__.return_value = mock_http

            call_count = 0

            async def side_effect(method, url, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return repo_resp
                captured_payload.update(kwargs.get("json", {}))
                return pr_resp

            mock_http.request.side_effect = side_effect

            await client.create_pr("feature-branch", "PR body text")

        assert captured_payload["title"] == "docs: auto-enrich swagger annotations"
        assert captured_payload["head"] == "feature-branch"
        assert captured_payload["base"] == "main"
        assert captured_payload["body"] == "PR body text"

    @pytest.mark.asyncio
    async def test_authorization_header_sent(self):
        client = _make_client(token="my_secret_token")
        repo_resp = _mock_response(200, {"default_branch": "main"})
        pr_resp = _mock_response(201, {"html_url": "https://github.com/owner/repo/pull/3"})
        captured_headers = {}

        with patch("httpx.AsyncClient") as MockAsyncClient:
            mock_http = AsyncMock()
            MockAsyncClient.return_value.__aenter__.return_value = mock_http

            call_count = 0

            async def side_effect(method, url, **kwargs):
                nonlocal call_count
                call_count += 1
                captured_headers.update(kwargs.get("headers", {}))
                return repo_resp if call_count == 1 else pr_resp

            mock_http.request.side_effect = side_effect

            await client.create_pr("branch", "desc")

        assert captured_headers.get("Authorization") == "Bearer my_secret_token"
        assert captured_headers.get("Accept") == "application/vnd.github+json"


# ---------------------------------------------------------------------------
# create_pr — description capping (Req 7.5)
# ---------------------------------------------------------------------------


class TestCreatePrDescriptionCap:
    @pytest.mark.asyncio
    async def test_long_description_is_capped(self):
        client = _make_client()
        long_desc = "X" * 100_000
        repo_resp = _mock_response(200, {"default_branch": "main"})
        pr_resp = _mock_response(201, {"html_url": "https://github.com/owner/repo/pull/4"})
        captured_body = {}

        with patch("httpx.AsyncClient") as MockAsyncClient:
            mock_http = AsyncMock()
            MockAsyncClient.return_value.__aenter__.return_value = mock_http

            call_count = 0

            async def side_effect(method, url, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    captured_body["body"] = kwargs.get("json", {}).get("body", "")
                return repo_resp if call_count == 1 else pr_resp

            mock_http.request.side_effect = side_effect

            await client.create_pr("branch", long_desc)

        assert len(captured_body["body"]) <= _DESCRIPTION_CAP
        assert captured_body["body"].endswith(_TRUNCATION_NOTICE)


# ---------------------------------------------------------------------------
# Auth failure handling (Req 7.9)
# ---------------------------------------------------------------------------


class TestAuthFailure:
    @pytest.mark.asyncio
    async def test_401_raises_github_auth_error(self):
        client = _make_client()
        auth_response = _mock_response(status_code=401)

        with patch("httpx.AsyncClient") as MockAsyncClient:
            mock_http = AsyncMock()
            MockAsyncClient.return_value.__aenter__.return_value = mock_http
            mock_http.request.return_value = auth_response

            with pytest.raises(GitHubAuthError) as exc_info:
                await client.create_pr("branch", "desc")

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_403_non_rate_limit_raises_github_auth_error(self):
        client = _make_client()
        # 403 without X-RateLimit-Remaining: 0 → auth error
        auth_response = _mock_response(
            status_code=403,
            headers={"X-RateLimit-Remaining": "100"},
        )

        with patch("httpx.AsyncClient") as MockAsyncClient:
            mock_http = AsyncMock()
            MockAsyncClient.return_value.__aenter__.return_value = mock_http
            mock_http.request.return_value = auth_response

            with pytest.raises(GitHubAuthError) as exc_info:
                await client.create_pr("branch", "desc")

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_401_does_not_retry(self):
        """Auth errors must abort immediately without retrying."""
        client = _make_client()
        auth_response = _mock_response(status_code=401)

        with patch("httpx.AsyncClient") as MockAsyncClient:
            mock_http = AsyncMock()
            MockAsyncClient.return_value.__aenter__.return_value = mock_http
            mock_http.request.return_value = auth_response

            with pytest.raises(GitHubAuthError):
                await client.create_pr("branch", "desc")

        # Only one request should have been made (no retries)
        assert mock_http.request.call_count == 1


# ---------------------------------------------------------------------------
# Rate-limit backoff and retry exhaustion (Req 7.7, 7.8)
# ---------------------------------------------------------------------------


class TestRateLimitBackoff:
    @pytest.mark.asyncio
    async def test_429_retries_up_to_max_attempts(self):
        """On 429, the client should retry up to 5 times then raise GitHubRateLimitError."""
        client = _make_client()
        rate_limit_response = _mock_response(status_code=429)

        with patch("httpx.AsyncClient") as MockAsyncClient, \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_http = AsyncMock()
            MockAsyncClient.return_value.__aenter__.return_value = mock_http
            mock_http.request.return_value = rate_limit_response

            with pytest.raises(GitHubRateLimitError):
                await client.create_pr("branch", "desc")

        # 5 attempts total (first attempt + 4 retries)
        assert mock_http.request.call_count == 5

    @pytest.mark.asyncio
    async def test_403_rate_limit_retries(self):
        """403 with X-RateLimit-Remaining: 0 should also trigger backoff."""
        client = _make_client()
        rate_limit_response = _mock_response(
            status_code=403,
            headers={"X-RateLimit-Remaining": "0"},
        )

        with patch("httpx.AsyncClient") as MockAsyncClient, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_http = AsyncMock()
            MockAsyncClient.return_value.__aenter__.return_value = mock_http
            mock_http.request.return_value = rate_limit_response

            with pytest.raises(GitHubRateLimitError):
                await client.create_pr("branch", "desc")

        assert mock_http.request.call_count == 5

    @pytest.mark.asyncio
    async def test_backoff_sleep_durations(self):
        """Backoff should start at 1s, double each time, capped at 60s."""
        client = _make_client()
        rate_limit_response = _mock_response(status_code=429)
        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        with patch("httpx.AsyncClient") as MockAsyncClient, \
             patch("asyncio.sleep", side_effect=fake_sleep):
            mock_http = AsyncMock()
            MockAsyncClient.return_value.__aenter__.return_value = mock_http
            mock_http.request.return_value = rate_limit_response

            with pytest.raises(GitHubRateLimitError):
                await client.create_pr("branch", "desc")

        # 5 attempts → 4 sleeps (sleep before each retry, not after last failure)
        assert len(sleep_calls) == 4
        assert sleep_calls[0] == 1
        assert sleep_calls[1] == 2
        assert sleep_calls[2] == 4
        assert sleep_calls[3] == 8

    @pytest.mark.asyncio
    async def test_backoff_cap_at_60_seconds(self):
        """Backoff wait time must not exceed 60 seconds."""
        client = _make_client()
        rate_limit_response = _mock_response(status_code=429)
        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        with patch("httpx.AsyncClient") as MockAsyncClient, \
             patch("asyncio.sleep", side_effect=fake_sleep):
            mock_http = AsyncMock()
            MockAsyncClient.return_value.__aenter__.return_value = mock_http
            mock_http.request.return_value = rate_limit_response

            with pytest.raises(GitHubRateLimitError):
                await client.create_pr("branch", "desc")

        for duration in sleep_calls:
            assert duration <= 60

    @pytest.mark.asyncio
    async def test_success_after_rate_limit_retry(self):
        """If a retry succeeds, the PR URL should be returned normally."""
        client = _make_client()
        rate_limit_response = _mock_response(status_code=429)
        repo_resp = _mock_response(200, {"default_branch": "main"})
        pr_resp = _mock_response(201, {"html_url": "https://github.com/owner/repo/pull/5"})

        responses = [rate_limit_response, repo_resp, pr_resp]

        with patch("httpx.AsyncClient") as MockAsyncClient, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_http = AsyncMock()
            MockAsyncClient.return_value.__aenter__.return_value = mock_http
            mock_http.request.side_effect = responses

            url = await client.create_pr("branch", "desc")

        assert url == "https://github.com/owner/repo/pull/5"


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class TestExceptionHierarchy:
    def test_github_auth_error_is_github_api_error(self):
        err = GitHubAuthError("auth failed", status_code=401)
        from swadoc.git.github_client import GitHubAPIError
        assert isinstance(err, GitHubAPIError)

    def test_github_rate_limit_error_is_github_api_error(self):
        err = GitHubRateLimitError("rate limit exhausted")
        from swadoc.git.github_client import GitHubAPIError
        assert isinstance(err, GitHubAPIError)

    def test_github_auth_error_stores_status_code(self):
        err = GitHubAuthError("forbidden", status_code=403)
        assert err.status_code == 403
