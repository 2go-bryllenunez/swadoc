"""
Backup manager for safe, reversible file modification.

Handles copying source files to ./autodoc-backup/ before modification,
byte-for-byte verification, and restoration on rollback.
"""

import hashlib
import shutil
from pathlib import Path


class BackupError(Exception):
    """Raised when a backup or restore operation fails."""


class BackupManager:
    """
    Manages file backups before source modification.

    Copies files to ./autodoc-backup/ (relative to cwd) preserving the
    path structure relative to project_path. Verifies copies are
    byte-for-byte identical. Supports restore for rollback scenarios.
    In dry_run mode all file operations are skipped.
    """

    def __init__(self, project_path: Path, dry_run: bool = False) -> None:
        self.project_path = project_path.resolve()
        self.dry_run = dry_run
        self.backup_dir = Path("./autodoc-backup/").resolve()
        # Track which source files have already been backed up this run
        self._backed_up: set[Path] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def backup_file(self, source_file: Path) -> Path:
        """
        Copy *source_file* to the backup directory.

        The backup preserves the file's path relative to project_path:
            source  /project/app/Controller.php
            backup  ./autodoc-backup/app/Controller.php

        Returns the path of the backup copy.

        Raises BackupError if:
        - the backup directory cannot be created
        - the file cannot be copied
        - the copy is not byte-for-byte identical to the source

        In dry_run mode the method returns the would-be backup path
        without performing any I/O.
        """
        source_file = source_file.resolve()

        if self.dry_run:
            return self._backup_path(source_file)

        # Already backed up this run — return existing backup path
        if source_file in self._backed_up:
            return self._backup_path(source_file)

        backup_path = self._backup_path(source_file)

        # Ensure backup directory (and any intermediate dirs) exist
        try:
            backup_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackupError(
                f"Cannot create backup directory '{backup_path.parent}': {exc}"
            ) from exc

        # Copy the file
        try:
            shutil.copy2(source_file, backup_path)
        except OSError as exc:
            raise BackupError(
                f"Cannot copy '{source_file}' to backup '{backup_path}': {exc}"
            ) from exc

        # Verify byte-for-byte identity
        if not self._files_identical(source_file, backup_path):
            raise BackupError(
                f"Backup verification failed: '{backup_path}' is not "
                f"byte-for-byte identical to '{source_file}'"
            )

        self._backed_up.add(source_file)
        return backup_path

    def restore_file(self, source_file: Path) -> None:
        """
        Restore *source_file* from its backup copy.

        Raises BackupError if no backup exists for the file.
        """
        source_file = source_file.resolve()
        backup_path = self._backup_path(source_file)

        if not backup_path.exists():
            raise BackupError(
                f"No backup found for '{source_file}' "
                f"(expected at '{backup_path}')"
            )

        try:
            shutil.copy2(backup_path, source_file)
        except OSError as exc:
            raise BackupError(
                f"Cannot restore '{source_file}' from backup '{backup_path}': {exc}"
            ) from exc

    def is_backed_up(self, source_file: Path) -> bool:
        """Return True if *source_file* has already been backed up this run."""
        return source_file.resolve() in self._backed_up

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _backup_path(self, source_file: Path) -> Path:
        """
        Compute the backup path for *source_file*.

        Strips the project_path prefix and places the remainder under
        backup_dir.  If source_file is not under project_path the full
        absolute path (without drive letter on Windows) is used so that
        backups never collide.
        """
        source_file = source_file.resolve()
        try:
            relative = source_file.relative_to(self.project_path)
        except ValueError:
            # File is outside project_path — use absolute path segments
            relative = Path(*source_file.parts[1:])  # strip drive / root
        return self.backup_dir / relative

    @staticmethod
    def _file_hash(path: Path) -> str:
        """Return the SHA-256 hex digest of *path*."""
        sha256 = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    def _files_identical(self, a: Path, b: Path) -> bool:
        """Return True when *a* and *b* have the same SHA-256 digest."""
        return self._file_hash(a) == self._file_hash(b)
