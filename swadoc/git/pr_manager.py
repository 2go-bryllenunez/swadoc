"""
PR Manager: Git branch management, committing, and GitHub PR creation.

This module handles the Git operations side of the PR workflow.
GitHub API PR creation delegates to :class:`~swadoc.git.github_client.GitHubClient`.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from swadoc.git.github_client import GitHubClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class GitError(Exception):
    """Raised when a git subprocess command fails."""

    def __init__(self, message: str, returncode: int | None = None, stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class BranchCollisionError(Exception):
    """Raised when all 10 candidate branch names are already in use."""

    def __init__(self, base_name: str) -> None:
        super().__init__(
            f"Branch name collision: all 10 candidates for '{base_name}' are already in use. "
            "Delete or rename existing branches and retry."
        )
        self.base_name = base_name


# ---------------------------------------------------------------------------
# PRManager
# ---------------------------------------------------------------------------

_MAX_BRANCH_LENGTH = 255
_MAX_COLLISION_ATTEMPTS = 10


class PRManager:
    """Handles Git branching, committing, and GitHub PR creation.

    Git operations (create_branch, commit_changes) are implemented here.
    GitHub API PR creation (open_pr) is a placeholder until task 12.2.
    """

    def __init__(
        self,
        github_token: str,
        github_repo: str,
        project_path: Path,
        dry_run: bool = False,
    ) -> None:
        self.github_token = github_token
        self.github_repo = github_repo
        self.project_path = project_path
        self.dry_run = dry_run

    # ------------------------------------------------------------------
    # Branch management
    # ------------------------------------------------------------------

    def create_branch(self, timestamp: str) -> str:
        """Create a uniquely-named Git branch for the documentation run.

        Branch name format: ``docs/swagger-autogen-{timestamp}``
        where *timestamp* is ``YYYYMMDDTHHMMSSZ`` in UTC (Req 7.1).

        Total branch name length must not exceed 255 characters (Req 7.1).

        If the resolved name already exists, appends ``-N`` (N = 1..10)
        until a free name is found.  Raises :class:`BranchCollisionError`
        when all 10 candidates are taken (Req 7.2).

        In dry-run mode the branch is *not* created; the resolved name is
        returned for logging purposes (Req 7.11).

        Args:
            timestamp: UTC timestamp string in ``YYYYMMDDTHHMMSSZ`` format.

        Returns:
            The branch name that was created (or would be created in dry-run).

        Raises:
            BranchCollisionError: All 10 collision-avoidance candidates are taken.
            GitError: The ``git checkout -b`` command failed.
        """
        base_name = f"docs/swagger-autogen-{timestamp}"

        # Enforce max length on the base name itself (255 chars).
        # The -N suffix adds at most 3 chars ("-10"), so we pre-truncate to
        # leave room.  In practice timestamps are short, so this is a safety net.
        if len(base_name) > _MAX_BRANCH_LENGTH:
            base_name = base_name[:_MAX_BRANCH_LENGTH]

        branch_name = self._resolve_unique_branch(base_name)

        if self.dry_run:
            logger.info("[dry-run] Would create branch: %s", branch_name)
            return branch_name

        self._run_git(["checkout", "-b", branch_name])
        logger.info("Created branch: %s", branch_name)
        return branch_name

    def _resolve_unique_branch(self, base_name: str) -> str:
        """Return the first available branch name, trying -1 through -10 on collision."""
        existing = self._list_local_branches()

        if base_name not in existing:
            return base_name

        for n in range(1, _MAX_COLLISION_ATTEMPTS + 1):
            suffix = f"-{n}"
            candidate = base_name + suffix
            # Enforce 255-char limit on the suffixed name too.
            if len(candidate) > _MAX_BRANCH_LENGTH:
                # Truncate base to fit the suffix.
                candidate = base_name[: _MAX_BRANCH_LENGTH - len(suffix)] + suffix
            if candidate not in existing:
                return candidate

        raise BranchCollisionError(base_name)

    def _list_local_branches(self) -> set[str]:
        """Return the set of local branch names in the repository."""
        result = self._run_git(
            ["branch", "--format=%(refname:short)"],
            capture_output=True,
        )
        branches = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        return branches

    # ------------------------------------------------------------------
    # Committing
    # ------------------------------------------------------------------

    def commit_changes(self, files: list[Path], route_count: int) -> None:
        """Stage *files* and create a commit with the standard message.

        Commit message format (Req 7.3):
        ``docs: auto-enrich swagger annotations [{n} routes updated]``

        In dry-run mode no Git operations are performed (Req 7.11).

        Args:
            files: Absolute or project-relative paths of files to stage.
            route_count: Number of routes that were enriched (used in commit message).

        Raises:
            GitError: A ``git add`` or ``git commit`` command failed.
        """
        if self.dry_run:
            logger.info(
                "[dry-run] Would commit %d file(s) with message: "
                "docs: auto-enrich swagger annotations [%d routes updated]",
                len(files),
                route_count,
            )
            return

        for file_path in files:
            self._run_git(["add", str(file_path)])

        commit_message = f"docs: auto-enrich swagger annotations [{route_count} routes updated]"
        self._run_git(["commit", "-m", commit_message])
        logger.info("Committed %d file(s): %s", len(files), commit_message)

    # ------------------------------------------------------------------
    # PR creation
    # ------------------------------------------------------------------

    async def open_pr(self, branch: str, description: str) -> str:
        """Open a GitHub pull request for *branch* via the GitHub API.

        Delegates to :class:`~swadoc.git.github_client.GitHubClient` which
        handles description capping (65000 chars), exponential backoff for
        rate-limited responses, and auth-failure detection.

        In dry-run mode this method is not expected to be called; callers
        should guard with ``if not self.dry_run`` before invoking.

        Args:
            branch: The branch name to open a PR from.
            description: The PR body text (may be truncated by the client).

        Returns:
            The URL of the created pull request.

        Raises:
            GitHubAuthError: HTTP 401 or non-rate-limit HTTP 403 from GitHub.
            GitHubRateLimitError: Rate-limit retries exhausted.
            GitHubAPIError: Any other unexpected GitHub API error.
        """
        client = GitHubClient(token=self.github_token, repo=self.github_repo)
        pr_url = await client.create_pr(branch=branch, description=description)
        logger.info("Pull request created: %s", pr_url)
        return pr_url

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_git(
        self,
        args: list[str],
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess:
        """Run a git command in :attr:`project_path`.

        Args:
            args: Arguments to pass after ``git`` (e.g. ``["checkout", "-b", "name"]``).
            capture_output: When *True*, capture stdout/stderr instead of
                inheriting the parent process streams.

        Returns:
            The :class:`subprocess.CompletedProcess` result.

        Raises:
            GitError: The command exited with a non-zero return code.
        """
        cmd = ["git"] + args
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.project_path),
                capture_output=capture_output,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitError(
                "git executable not found. Ensure git is installed and on PATH."
            ) from exc

        if result.returncode != 0:
            stderr = result.stderr.strip() if capture_output else ""
            raise GitError(
                f"git {' '.join(args)} failed (exit {result.returncode}): {stderr}",
                returncode=result.returncode,
                stderr=stderr,
            )

        return result
