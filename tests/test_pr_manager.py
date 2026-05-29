"""Unit tests for swadoc.git.pr_manager — Git operations and branch management.

Validates: Requirements 7.1, 7.2, 7.3, 7.10, 7.11
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from swadoc.git.pr_manager import BranchCollisionError, GitError, PRManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(
    project_path: Path | None = None,
    dry_run: bool = False,
) -> PRManager:
    return PRManager(
        github_token="ghp_test",
        github_repo="owner/repo",
        project_path=project_path or Path("."),
        dry_run=dry_run,
    )


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    """Build a fake CompletedProcess."""
    result = subprocess.CompletedProcess(args=[], returncode=returncode)
    result.stdout = stdout
    result.stderr = ""
    return result


# ---------------------------------------------------------------------------
# Branch name generation (Req 7.1)
# ---------------------------------------------------------------------------


class TestCreateBranchNameFormat:
    """Branch name must follow docs/swagger-autogen-{timestamp} format."""

    def test_branch_name_format(self, tmp_path):
        manager = _make_manager(project_path=tmp_path)
        timestamp = "20240115T103045Z"

        with patch.object(manager, "_list_local_branches", return_value=set()), \
             patch.object(manager, "_run_git", return_value=_completed()):
            branch = manager.create_branch(timestamp)

        assert branch == f"docs/swagger-autogen-{timestamp}"

    def test_branch_name_prefix(self, tmp_path):
        manager = _make_manager(project_path=tmp_path)

        with patch.object(manager, "_list_local_branches", return_value=set()), \
             patch.object(manager, "_run_git", return_value=_completed()):
            branch = manager.create_branch("20240101T000000Z")

        assert branch.startswith("docs/swagger-autogen-")

    def test_branch_name_max_255_chars(self, tmp_path):
        """Branch name must not exceed 255 characters (Req 7.1)."""
        manager = _make_manager(project_path=tmp_path)
        # Craft a timestamp that would push the name over 255 chars
        long_timestamp = "X" * 300

        with patch.object(manager, "_list_local_branches", return_value=set()), \
             patch.object(manager, "_run_git", return_value=_completed()):
            branch = manager.create_branch(long_timestamp)

        assert len(branch) <= 255

    def test_branch_name_exactly_255_chars_allowed(self, tmp_path):
        """A branch name of exactly 255 chars is valid."""
        manager = _make_manager(project_path=tmp_path)
        # "docs/swagger-autogen-" is 22 chars; pad timestamp to reach 255 total
        padding = "A" * (255 - len("docs/swagger-autogen-"))
        timestamp = padding

        with patch.object(manager, "_list_local_branches", return_value=set()), \
             patch.object(manager, "_run_git", return_value=_completed()):
            branch = manager.create_branch(timestamp)

        assert len(branch) == 255


# ---------------------------------------------------------------------------
# Branch collision handling (Req 7.2)
# ---------------------------------------------------------------------------


class TestCreateBranchCollision:
    """If the base name exists, append -N (N=1..10)."""

    def test_no_collision_uses_base_name(self, tmp_path):
        manager = _make_manager(project_path=tmp_path)
        ts = "20240115T103045Z"
        base = f"docs/swagger-autogen-{ts}"

        with patch.object(manager, "_list_local_branches", return_value=set()), \
             patch.object(manager, "_run_git", return_value=_completed()):
            branch = manager.create_branch(ts)

        assert branch == base

    def test_collision_appends_suffix_1(self, tmp_path):
        manager = _make_manager(project_path=tmp_path)
        ts = "20240115T103045Z"
        base = f"docs/swagger-autogen-{ts}"
        existing = {base}

        with patch.object(manager, "_list_local_branches", return_value=existing), \
             patch.object(manager, "_run_git", return_value=_completed()):
            branch = manager.create_branch(ts)

        assert branch == f"{base}-1"

    def test_collision_appends_suffix_5(self, tmp_path):
        manager = _make_manager(project_path=tmp_path)
        ts = "20240115T103045Z"
        base = f"docs/swagger-autogen-{ts}"
        existing = {base} | {f"{base}-{n}" for n in range(1, 5)}

        with patch.object(manager, "_list_local_branches", return_value=existing), \
             patch.object(manager, "_run_git", return_value=_completed()):
            branch = manager.create_branch(ts)

        assert branch == f"{base}-5"

    def test_collision_appends_suffix_10(self, tmp_path):
        manager = _make_manager(project_path=tmp_path)
        ts = "20240115T103045Z"
        base = f"docs/swagger-autogen-{ts}"
        existing = {base} | {f"{base}-{n}" for n in range(1, 10)}

        with patch.object(manager, "_list_local_branches", return_value=existing), \
             patch.object(manager, "_run_git", return_value=_completed()):
            branch = manager.create_branch(ts)

        assert branch == f"{base}-10"

    def test_all_10_candidates_taken_raises_branch_collision_error(self, tmp_path):
        """Req 7.2: raise BranchCollisionError when all 10 candidates are taken."""
        manager = _make_manager(project_path=tmp_path)
        ts = "20240115T103045Z"
        base = f"docs/swagger-autogen-{ts}"
        existing = {base} | {f"{base}-{n}" for n in range(1, 11)}

        with patch.object(manager, "_list_local_branches", return_value=existing):
            with pytest.raises(BranchCollisionError):
                manager.create_branch(ts)

    def test_branch_collision_error_message_contains_base_name(self, tmp_path):
        manager = _make_manager(project_path=tmp_path)
        ts = "20240115T103045Z"
        base = f"docs/swagger-autogen-{ts}"
        existing = {base} | {f"{base}-{n}" for n in range(1, 11)}

        with patch.object(manager, "_list_local_branches", return_value=existing):
            with pytest.raises(BranchCollisionError) as exc_info:
                manager.create_branch(ts)

        assert base in str(exc_info.value)

    def test_collision_suffix_respects_255_char_limit(self, tmp_path):
        """Suffixed branch name must also not exceed 255 chars."""
        manager = _make_manager(project_path=tmp_path)
        # Build a timestamp that makes the base name exactly 255 chars
        padding = "A" * (255 - len("docs/swagger-autogen-"))
        ts = padding
        base = f"docs/swagger-autogen-{ts}"
        assert len(base) == 255
        existing = {base}

        with patch.object(manager, "_list_local_branches", return_value=existing), \
             patch.object(manager, "_run_git", return_value=_completed()):
            branch = manager.create_branch(ts)

        assert len(branch) <= 255


# ---------------------------------------------------------------------------
# Git checkout -b is called (Req 7.1)
# ---------------------------------------------------------------------------


class TestCreateBranchGitCall:
    def test_git_checkout_b_is_called(self, tmp_path):
        manager = _make_manager(project_path=tmp_path)
        ts = "20240115T103045Z"
        expected_branch = f"docs/swagger-autogen-{ts}"

        with patch.object(manager, "_list_local_branches", return_value=set()), \
             patch.object(manager, "_run_git", return_value=_completed()) as mock_git:
            manager.create_branch(ts)

        mock_git.assert_called_once_with(["checkout", "-b", expected_branch])

    def test_git_error_propagates(self, tmp_path):
        manager = _make_manager(project_path=tmp_path)

        with patch.object(manager, "_list_local_branches", return_value=set()), \
             patch.object(manager, "_run_git", side_effect=GitError("branch failed", returncode=128)):
            with pytest.raises(GitError):
                manager.create_branch("20240115T103045Z")


# ---------------------------------------------------------------------------
# commit_changes — message format (Req 7.3)
# ---------------------------------------------------------------------------


class TestCommitChanges:
    def test_commit_message_format(self, tmp_path):
        """Commit message must be 'docs: auto-enrich swagger annotations [N routes updated]'."""
        manager = _make_manager(project_path=tmp_path)
        files = [tmp_path / "app" / "Controller.php"]

        with patch.object(manager, "_run_git", return_value=_completed()) as mock_git:
            manager.commit_changes(files, route_count=5)

        # Last call should be the commit
        commit_call = mock_git.call_args_list[-1]
        commit_args = commit_call[0][0]  # positional first arg (the list)
        assert commit_args[0] == "commit"
        assert commit_args[1] == "-m"
        assert commit_args[2] == "docs: auto-enrich swagger annotations [5 routes updated]"

    def test_commit_message_zero_routes(self, tmp_path):
        manager = _make_manager(project_path=tmp_path)

        with patch.object(manager, "_run_git", return_value=_completed()) as mock_git:
            manager.commit_changes([], route_count=0)

        commit_call = mock_git.call_args_list[-1]
        msg = commit_call[0][0][2]
        assert msg == "docs: auto-enrich swagger annotations [0 routes updated]"

    def test_commit_message_large_count(self, tmp_path):
        manager = _make_manager(project_path=tmp_path)

        with patch.object(manager, "_run_git", return_value=_completed()) as mock_git:
            manager.commit_changes([], route_count=999)

        commit_call = mock_git.call_args_list[-1]
        msg = commit_call[0][0][2]
        assert "999 routes updated" in msg

    def test_each_file_is_staged(self, tmp_path):
        """git add must be called for each file."""
        manager = _make_manager(project_path=tmp_path)
        files = [
            tmp_path / "app" / "ControllerA.php",
            tmp_path / "app" / "ControllerB.php",
            tmp_path / "openapi.json",
        ]

        with patch.object(manager, "_run_git", return_value=_completed()) as mock_git:
            manager.commit_changes(files, route_count=3)

        add_calls = [c for c in mock_git.call_args_list if c[0][0][0] == "add"]
        assert len(add_calls) == 3
        staged_paths = {c[0][0][1] for c in add_calls}
        assert staged_paths == {str(f) for f in files}

    def test_git_add_before_commit(self, tmp_path):
        """git add calls must precede git commit."""
        manager = _make_manager(project_path=tmp_path)
        files = [tmp_path / "file.php"]
        call_order = []

        def fake_run_git(args, **kwargs):
            call_order.append(args[0])
            return _completed()

        with patch.object(manager, "_run_git", side_effect=fake_run_git):
            manager.commit_changes(files, route_count=1)

        assert call_order.index("add") < call_order.index("commit")


# ---------------------------------------------------------------------------
# Dry-run mode (Req 7.11)
# ---------------------------------------------------------------------------


class TestDryRunMode:
    def test_create_branch_dry_run_no_git_call(self, tmp_path):
        """In dry-run mode, no git commands are executed."""
        manager = _make_manager(project_path=tmp_path, dry_run=True)

        with patch.object(manager, "_list_local_branches", return_value=set()), \
             patch.object(manager, "_run_git") as mock_git:
            branch = manager.create_branch("20240115T103045Z")

        mock_git.assert_not_called()

    def test_create_branch_dry_run_returns_branch_name(self, tmp_path):
        """Dry-run still returns the resolved branch name for logging."""
        manager = _make_manager(project_path=tmp_path, dry_run=True)
        ts = "20240115T103045Z"

        with patch.object(manager, "_list_local_branches", return_value=set()):
            branch = manager.create_branch(ts)

        assert branch == f"docs/swagger-autogen-{ts}"

    def test_commit_changes_dry_run_no_git_call(self, tmp_path):
        """In dry-run mode, commit_changes performs no git operations."""
        manager = _make_manager(project_path=tmp_path, dry_run=True)
        files = [tmp_path / "app" / "Controller.php"]

        with patch.object(manager, "_run_git") as mock_git:
            manager.commit_changes(files, route_count=3)

        mock_git.assert_not_called()

    def test_dry_run_collision_still_resolved(self, tmp_path):
        """Dry-run still resolves collisions (reads branch list) but doesn't create."""
        manager = _make_manager(project_path=tmp_path, dry_run=True)
        ts = "20240115T103045Z"
        base = f"docs/swagger-autogen-{ts}"
        existing = {base}

        with patch.object(manager, "_list_local_branches", return_value=existing), \
             patch.object(manager, "_run_git") as mock_git:
            branch = manager.create_branch(ts)

        mock_git.assert_not_called()
        assert branch == f"{base}-1"


# ---------------------------------------------------------------------------
# open_pr — wired to GitHubClient (task 12.2)
# ---------------------------------------------------------------------------


class TestOpenPr:
    @pytest.mark.asyncio
    async def test_open_pr_delegates_to_github_client(self, tmp_path):
        """open_pr should call GitHubClient.create_pr and return the PR URL."""
        manager = _make_manager(project_path=tmp_path)
        expected_url = "https://github.com/owner/repo/pull/42"

        with patch("swadoc.git.pr_manager.GitHubClient") as MockClient:
            instance = MockClient.return_value
            instance.create_pr = MagicMock(return_value=expected_url)
            # Make create_pr awaitable
            async def _create_pr(**kwargs):
                return expected_url
            instance.create_pr = _create_pr

            url = await manager.open_pr("docs/swagger-autogen-20240115T103045Z", "description")

        assert url == expected_url

    @pytest.mark.asyncio
    async def test_open_pr_passes_branch_and_description(self, tmp_path):
        """open_pr must forward branch and description to GitHubClient.create_pr."""
        manager = _make_manager(project_path=tmp_path)
        captured = {}

        with patch("swadoc.git.pr_manager.GitHubClient") as MockClient:
            async def _create_pr(branch, description):
                captured["branch"] = branch
                captured["description"] = description
                return "https://github.com/owner/repo/pull/1"
            MockClient.return_value.create_pr = _create_pr

            await manager.open_pr("my-branch", "my description")

        assert captured["branch"] == "my-branch"
        assert captured["description"] == "my description"

    @pytest.mark.asyncio
    async def test_open_pr_constructs_client_with_token_and_repo(self, tmp_path):
        """GitHubClient must be instantiated with the manager's token and repo."""
        manager = _make_manager(project_path=tmp_path)

        with patch("swadoc.git.pr_manager.GitHubClient") as MockClient:
            async def _create_pr(**kwargs):
                return "https://github.com/owner/repo/pull/1"
            MockClient.return_value.create_pr = _create_pr

            await manager.open_pr("branch", "desc")

        MockClient.assert_called_once_with(token="ghp_test", repo="owner/repo")


# ---------------------------------------------------------------------------
# GitError and BranchCollisionError exception types
# ---------------------------------------------------------------------------


class TestExceptionTypes:
    def test_git_error_is_exception(self):
        err = GitError("something went wrong", returncode=1, stderr="fatal: ...")
        assert isinstance(err, Exception)
        assert err.returncode == 1
        assert err.stderr == "fatal: ..."

    def test_branch_collision_error_is_exception(self):
        err = BranchCollisionError("docs/swagger-autogen-20240115T103045Z")
        assert isinstance(err, Exception)
        assert err.base_name == "docs/swagger-autogen-20240115T103045Z"

    def test_git_error_default_fields(self):
        err = GitError("oops")
        assert err.returncode is None
        assert err.stderr == ""
