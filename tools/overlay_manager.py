"""
Overlay Manager for OverlayFS + Git Integration.

This module provides filesystem isolation using OverlayFS,
allowing all session changes to be captured and reviewed before commit.

Features:
- Mount OverlayFS at session start (before EXPLORATION)
- Capture all file changes in upper directory
- Support garbage detection via LLM review (PRE_COMMIT phase)
- Clean unmount and branch commit

Requirements:
- Linux kernel with OverlayFS support
- sudo privileges for mount/unmount operations
- Git repository initialized

Directory Structure:
    .overlay/
    ├── upper/{session_id}/   # Changed files
    ├── work/{session_id}/    # OverlayFS workdir
    └── merged/{session_id}/  # Mount point

Usage:
    from tools.overlay_manager import OverlayManager

    manager = OverlayManager("/path/to/repo")

    # At session start
    result = await manager.setup_session("session_123")
    # result.branch_name = "llm_task_session_123"
    # result.merged_path = "/path/to/repo/.overlay/merged/session_123"

    # At PRE_COMMIT (garbage detection)
    changes = await manager.get_changes()
    # returns list of changed files with diff

    # After review
    await manager.finalize(keep_files=["auth/service.py"], discard_files=["debug.log"])

    # Cleanup
    await manager.cleanup()
"""

import asyncio
import fcntl
import os
import shutil
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal


# =============================================================================
# Repository Lock for Concurrent Session Protection
# =============================================================================

class RepoLockError(Exception):
    """Raised when repository lock cannot be acquired."""
    pass


class RepoLock:
    """
    File-based lock for repository-level operations.

    Prevents concurrent git checkout + overlay mount operations.
    Only the setup phase (checkout + mount) needs to be serialized;
    once mounted, sessions can work independently.
    """

    # Class-level lock registry to prevent multiple locks per repo
    _locks: dict[str, "RepoLock"] = {}

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self.lock_file = repo_path / ".overlay" / ".repo.lock"
        self._fd: int | None = None

    @classmethod
    def for_repo(cls, repo_path: Path) -> "RepoLock":
        """Get or create a RepoLock for a repository."""
        key = str(repo_path.resolve())
        if key not in cls._locks:
            cls._locks[key] = cls(repo_path)
        return cls._locks[key]

    async def acquire(self, timeout: float = 30.0) -> bool:
        """
        Acquire the repository lock with timeout.

        Args:
            timeout: Maximum time to wait for lock (seconds)

        Returns:
            True if lock acquired, False if timeout

        Raises:
            RepoLockError if lock file cannot be created
        """
        # Ensure lock directory exists
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)

        start_time = asyncio.get_event_loop().time()

        while True:
            try:
                # Open or create lock file
                self._fd = os.open(
                    str(self.lock_file),
                    os.O_RDWR | os.O_CREAT,
                    0o644
                )

                # Try non-blocking exclusive lock
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

                # Write lock info for debugging
                os.write(self._fd, f"locked by pid {os.getpid()}\n".encode())
                os.fsync(self._fd)

                return True

            except (BlockingIOError, OSError) as e:
                # Lock held by another process
                if self._fd is not None:
                    os.close(self._fd)
                    self._fd = None

                # Check timeout
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= timeout:
                    return False

                # Wait and retry
                await asyncio.sleep(0.1)

    def release(self) -> None:
        """Release the repository lock."""
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            finally:
                self._fd = None

    @asynccontextmanager
    async def locked(self, timeout: float = 30.0):
        """
        Context manager for acquiring and releasing lock.

        Usage:
            async with repo_lock.locked():
                # checkout + mount operations

        Raises:
            RepoLockError if lock cannot be acquired within timeout
        """
        acquired = await self.acquire(timeout)
        if not acquired:
            raise RepoLockError(
                f"Could not acquire repository lock within {timeout}s. "
                "Another session may be starting. Try again shortly."
            )
        try:
            yield
        finally:
            self.release()


@dataclass
class OverlaySetupResult:
    """Result of overlay setup operation."""
    success: bool
    session_id: str
    branch_name: str
    merged_path: str
    upper_path: str
    error: str | None = None


@dataclass
class FileChange:
    """Represents a single file change in the overlay."""
    path: str  # Relative path from repo root
    change_type: Literal["added", "modified", "deleted"]
    diff: str | None = None  # Unified diff for text files
    is_binary: bool = False
    size_bytes: int = 0


@dataclass
class OverlayChanges:
    """All changes captured in the overlay upper directory."""
    session_id: str
    changes: list[FileChange] = field(default_factory=list)
    total_files: int = 0
    total_size_bytes: int = 0


@dataclass
class FinalizeResult:
    """Result of finalize operation."""
    success: bool
    commit_hash: str | None = None
    kept_files: list[str] = field(default_factory=list)
    discarded_files: list[str] = field(default_factory=list)
    error: str | None = None


class OverlayManager:
    """
    Manages OverlayFS for session-based file isolation.

    Each session gets its own overlay mount, capturing all changes
    in an upper directory for review before commit.
    """

    def __init__(self, repo_path: str):
        """
        Initialize OverlayManager.

        Args:
            repo_path: Path to the git repository root
        """
        self.repo_path = Path(repo_path).resolve()
        self.overlay_base = self.repo_path / ".overlay"

        # Active session tracking
        self._active_session: str | None = None
        self._branch_name: str | None = None
        self._is_mounted: bool = False

    @classmethod
    async def cleanup_stale_sessions(cls, repo_path: str) -> dict:
        """
        Clean up stale overlay sessions from interrupted runs.

        Call this at startup or when overlay setup fails due to existing mounts.

        Args:
            repo_path: Path to the git repository root

        Returns:
            {"unmounted": list, "removed_dirs": list, "deleted_branches": list, "errors": list}
        """
        repo = Path(repo_path).resolve()
        overlay_base = repo / ".overlay"
        errors = []
        unmounted = []
        removed_dirs = []
        deleted_branches = []

        # 1. Unmount any stale mounts in merged/
        merged_dir = overlay_base / "merged"
        if merged_dir.exists():
            for session_dir in merged_dir.iterdir():
                if session_dir.is_dir():
                    try:
                        # Try to unmount
                        proc = await asyncio.create_subprocess_exec(
                            "fusermount", "-u", str(session_dir),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        await proc.communicate()
                        if proc.returncode == 0:
                            unmounted.append(str(session_dir))
                    except Exception as e:
                        errors.append(f"Unmount {session_dir}: {e}")

        # 2. Remove stale directories
        for subdir in ["upper", "work", "merged"]:
            target = overlay_base / subdir
            if target.exists():
                for session_dir in target.iterdir():
                    if session_dir.is_dir():
                        try:
                            shutil.rmtree(session_dir, ignore_errors=True)
                            removed_dirs.append(str(session_dir))
                        except Exception as e:
                            errors.append(f"Remove {session_dir}: {e}")

        # 3. Clean up stale task branches (optional, be careful)
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "branch", "--list", "llm_task_*",
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode == 0 and stdout:
                branches = [b.strip().lstrip("* ") for b in stdout.decode().strip().split("\n") if b.strip()]
                for branch in branches:
                    # Only delete if it looks like a stale session branch
                    if branch.startswith("llm_task_session_"):
                        try:
                            proc = await asyncio.create_subprocess_exec(
                                "git", "branch", "-D", branch,
                                cwd=str(repo),
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                            )
                            await proc.communicate()
                            if proc.returncode == 0:
                                deleted_branches.append(branch)
                        except Exception as e:
                            errors.append(f"Delete branch {branch}: {e}")
        except Exception as e:
            errors.append(f"List branches: {e}")

        # 4. Remove stale lock file
        lock_file = overlay_base / ".repo.lock"
        if lock_file.exists():
            try:
                lock_file.unlink()
            except Exception as e:
                errors.append(f"Remove lock file: {e}")

        return {
            "unmounted": unmounted,
            "removed_dirs": removed_dirs,
            "deleted_branches": deleted_branches,
            "errors": errors,
        }

    @property
    def upper_path(self) -> Path | None:
        """Get upper directory path for active session."""
        if self._active_session:
            return self.overlay_base / "upper" / self._active_session
        return None

    @property
    def work_path(self) -> Path | None:
        """Get work directory path for active session."""
        if self._active_session:
            return self.overlay_base / "work" / self._active_session
        return None

    @property
    def merged_path(self) -> Path | None:
        """Get merged mount point for active session."""
        if self._active_session:
            return self.overlay_base / "merged" / self._active_session
        return None

    async def setup_session(self, session_id: str, lock_timeout: float = 30.0) -> OverlaySetupResult:
        """
        Set up Git branch and OverlayFS for a new session.

        This should be called at start_session, BEFORE EXPLORATION phase.

        Steps:
        1. Acquire repository lock (prevents concurrent setup)
        2. Create new git branch: llm_task_{session_id}
        3. Create overlay directories
        4. Mount OverlayFS with current state as lower
        5. Release lock (other sessions can now start)

        Args:
            session_id: Unique session identifier
            lock_timeout: Maximum time to wait for lock (seconds)

        Returns:
            OverlaySetupResult with paths and status

        Note:
            The lock is only held during checkout + mount operations.
            Once mounted, sessions work independently on their own overlay.
        """
        try:
            # Check if already has an active session
            if self._is_mounted:
                return OverlaySetupResult(
                    success=False,
                    session_id=session_id,
                    branch_name="",
                    merged_path="",
                    upper_path="",
                    error=f"Session {self._active_session} is already active. Call cleanup() first.",
                )

            # Get repository lock
            repo_lock = RepoLock.for_repo(self.repo_path)

            try:
                # Acquire lock with timeout - only for checkout + mount phase
                async with repo_lock.locked(timeout=lock_timeout):
                    # Step 1: Create git branch
                    branch_name = f"llm_task_{session_id}"
                    result = await self._run_git(["checkout", "-b", branch_name])
                    if result.returncode != 0:
                        # Branch might already exist, try to checkout
                        result = await self._run_git(["checkout", branch_name])
                        if result.returncode != 0:
                            return OverlaySetupResult(
                                success=False,
                                session_id=session_id,
                                branch_name=branch_name,
                                merged_path="",
                                upper_path="",
                                error=f"Failed to create/checkout branch: {result.stderr}",
                            )

                    self._branch_name = branch_name

                    # Step 2: Create overlay directories
                    upper = self.overlay_base / "upper" / session_id
                    work = self.overlay_base / "work" / session_id
                    merged = self.overlay_base / "merged" / session_id

                    for d in [upper, work, merged]:
                        d.mkdir(parents=True, exist_ok=True)

                    # Step 3: Mount OverlayFS
                    mount_result = await self._mount_overlay(upper, work, merged)
                    if not mount_result:
                        return OverlaySetupResult(
                            success=False,
                            session_id=session_id,
                            branch_name=branch_name,
                            merged_path=str(merged),
                            upper_path=str(upper),
                            error="Failed to mount OverlayFS. Install with: sudo apt-get install -y fuse-overlayfs",
                        )

                    # Lock released here - other sessions can now start
                    # This session continues with its own isolated overlay

            except RepoLockError as e:
                return OverlaySetupResult(
                    success=False,
                    session_id=session_id,
                    branch_name="",
                    merged_path="",
                    upper_path="",
                    error=str(e),
                )

            self._active_session = session_id
            self._is_mounted = True

            # Add .overlay to .gitignore if not present
            await self._ensure_gitignore()

            return OverlaySetupResult(
                success=True,
                session_id=session_id,
                branch_name=branch_name,
                merged_path=str(merged),
                upper_path=str(upper),
            )

        except Exception as e:
            return OverlaySetupResult(
                success=False,
                session_id=session_id,
                branch_name="",
                merged_path="",
                upper_path="",
                error=str(e),
            )

    async def get_changes(self) -> OverlayChanges:
        """
        Get all changes captured in the overlay upper directory.

        This is used in PRE_COMMIT phase for garbage detection.

        Returns:
            OverlayChanges with list of all changed files
        """
        if not self._active_session or not self.upper_path:
            return OverlayChanges(session_id="", changes=[], total_files=0)

        changes = []
        total_size = 0

        upper = self.upper_path
        if not upper.exists():
            return OverlayChanges(
                session_id=self._active_session,
                changes=[],
                total_files=0,
            )

        # Walk through upper directory to find all changes
        for root, dirs, files in os.walk(upper):
            # Skip OverlayFS metadata
            dirs[:] = [d for d in dirs if not d.startswith(".")]

            for filename in files:
                if filename.startswith("."):
                    continue

                filepath = Path(root) / filename
                relative_path = filepath.relative_to(upper)

                # Determine change type
                original_path = self.repo_path / relative_path

                # Check if it's a whiteout file (deletion marker)
                if filename.startswith(".wh."):
                    actual_name = filename[4:]  # Remove .wh. prefix
                    changes.append(FileChange(
                        path=str(relative_path.parent / actual_name),
                        change_type="deleted",
                        diff=None,
                        is_binary=False,
                        size_bytes=0,
                    ))
                    continue

                # Check if file is new or modified
                if original_path.exists():
                    change_type = "modified"
                else:
                    change_type = "added"

                # Get file info
                stat = filepath.stat()
                size = stat.st_size
                total_size += size

                # Get diff for text files
                diff = None
                is_binary = False

                if size < 1024 * 1024:  # Only diff files < 1MB
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            new_content = f.read()

                        if change_type == "modified" and original_path.exists():
                            with open(original_path, "r", encoding="utf-8") as f:
                                old_content = f.read()

                            # Generate unified diff
                            import difflib
                            diff_lines = list(difflib.unified_diff(
                                old_content.splitlines(keepends=True),
                                new_content.splitlines(keepends=True),
                                fromfile=f"a/{relative_path}",
                                tofile=f"b/{relative_path}",
                            ))
                            diff = "".join(diff_lines)
                        else:
                            # New file - show full content as diff
                            diff = f"+++ b/{relative_path}\n"
                            for line in new_content.splitlines():
                                diff += f"+{line}\n"

                    except (UnicodeDecodeError, PermissionError):
                        is_binary = True

                changes.append(FileChange(
                    path=str(relative_path),
                    change_type=change_type,
                    diff=diff,
                    is_binary=is_binary,
                    size_bytes=size,
                ))

        return OverlayChanges(
            session_id=self._active_session,
            changes=changes,
            total_files=len(changes),
            total_size_bytes=total_size,
        )

    async def finalize(
        self,
        keep_files: list[str] | None = None,
        discard_files: list[str] | None = None,
        commit_message: str | None = None,
    ) -> FinalizeResult:
        """
        Finalize changes after garbage review.

        This copies approved changes from upper to the repository
        and commits to the task branch.

        Args:
            keep_files: Files to keep (apply to repo). If None, keep all.
            discard_files: Files to discard (garbage). If None, discard none.
            commit_message: Commit message. Auto-generated if not provided.

        Returns:
            FinalizeResult with commit hash and file lists
        """
        if not self._active_session or not self.upper_path:
            return FinalizeResult(
                success=False,
                error="No active session",
            )

        try:
            # Get all changes
            changes = await self.get_changes()

            # Determine which files to keep
            all_files = {c.path for c in changes.changes}

            if keep_files is not None:
                files_to_keep = set(keep_files)
            else:
                # Keep all except explicit discards
                files_to_keep = all_files - set(discard_files or [])

            files_to_discard = all_files - files_to_keep

            # Apply kept files to repository
            kept = []
            for change in changes.changes:
                if change.path not in files_to_keep:
                    continue

                src = self.upper_path / change.path
                dst = self.repo_path / change.path

                if change.change_type == "deleted":
                    # Handle deletion
                    if dst.exists():
                        dst.unlink()
                        kept.append(change.path)
                elif src.exists():
                    # Copy file
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    kept.append(change.path)

            # Git add and commit
            if kept:
                await self._run_git(["add"] + kept)

                if commit_message is None:
                    commit_message = f"Session {self._active_session}: Apply {len(kept)} file(s)"

                result = await self._run_git(["commit", "-m", commit_message])

                if result.returncode == 0:
                    # Get commit hash
                    hash_result = await self._run_git(["rev-parse", "HEAD"])
                    commit_hash = hash_result.stdout.strip() if hash_result.returncode == 0 else None

                    return FinalizeResult(
                        success=True,
                        commit_hash=commit_hash,
                        kept_files=list(files_to_keep),
                        discarded_files=list(files_to_discard),
                    )
                else:
                    return FinalizeResult(
                        success=False,
                        error=f"Git commit failed: {result.stderr}",
                        kept_files=list(files_to_keep),
                        discarded_files=list(files_to_discard),
                    )
            else:
                return FinalizeResult(
                    success=True,
                    commit_hash=None,
                    kept_files=[],
                    discarded_files=list(files_to_discard),
                )

        except Exception as e:
            return FinalizeResult(
                success=False,
                error=str(e),
            )

    async def merge_to_main(self, main_branch: str = "main", delete_branch: bool = True) -> dict:
        """
        Merge task branch to main branch.

        Args:
            main_branch: Target branch to merge into
            delete_branch: Delete task branch after successful merge (default: True)

        Returns:
            {"success": bool, "merged": bool, "branch_deleted": bool, "error": str | None}
        """
        if not self._branch_name:
            return {"success": False, "merged": False, "branch_deleted": False, "error": "No active branch"}

        llm_task_branch = self._branch_name

        try:
            # Checkout main
            result = await self._run_git(["checkout", main_branch])
            if result.returncode != 0:
                return {"success": False, "merged": False, "branch_deleted": False, "error": f"Failed to checkout {main_branch}: {result.stderr}"}

            # Merge task branch
            result = await self._run_git(["merge", "--no-ff", llm_task_branch, "-m", f"Merge {llm_task_branch}"])
            if result.returncode != 0:
                # Revert to task branch on failure
                await self._run_git(["checkout", llm_task_branch])
                return {"success": False, "merged": False, "branch_deleted": False, "error": f"Merge failed: {result.stderr}"}

            # Delete task branch after successful merge
            branch_deleted = False
            if delete_branch:
                delete_result = await self._run_git(["branch", "-d", llm_task_branch])
                branch_deleted = delete_result.returncode == 0
                if not branch_deleted:
                    # Try force delete if normal delete fails (shouldn't happen after merge)
                    delete_result = await self._run_git(["branch", "-D", llm_task_branch])
                    branch_deleted = delete_result.returncode == 0

            return {"success": True, "merged": True, "branch_deleted": branch_deleted, "error": None}

        except Exception as e:
            return {"success": False, "merged": False, "branch_deleted": False, "error": str(e)}

    async def cleanup(self) -> bool:
        """
        Unmount OverlayFS and clean up session directories.

        Returns:
            True if cleanup successful
        """
        if not self._active_session:
            return True

        try:
            # Unmount
            if self._is_mounted and self.merged_path:
                await self._unmount_overlay()

            # Remove session directories
            for subdir in ["upper", "work", "merged"]:
                session_dir = self.overlay_base / subdir / self._active_session
                if session_dir.exists():
                    shutil.rmtree(session_dir, ignore_errors=True)

            self._active_session = None
            self._is_mounted = False

            return True

        except Exception:
            return False

    async def _mount_overlay(self, upper: Path, work: Path, merged: Path) -> bool:
        """
        Mount OverlayFS using fuse-overlayfs.

        fuse-overlayfs is preferred because:
        - No sudo required (FUSE-based)
        - CoW (Copy-on-Write) for efficiency
        - Works in user space

        Install: sudo apt-get install -y fuse-overlayfs
        """
        # fuse-overlayfs mount command
        # lowerdir: Original project (read-only)
        # upperdir: Where changes are written
        # workdir: Internal use (must be on same filesystem as upperdir)
        mount_opts = f"lowerdir={self.repo_path},upperdir={upper},workdir={work}"

        try:
            # Use fuse-overlayfs (no sudo needed)
            proc = await asyncio.create_subprocess_exec(
                "fuse-overlayfs",
                "-o", mount_opts,
                str(merged),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                # Log error for debugging
                error_msg = stderr.decode() if stderr else "Unknown error"
                print(f"fuse-overlayfs mount failed: {error_msg}")
                return False

            return True

        except FileNotFoundError:
            print("fuse-overlayfs not found. Install with: sudo apt-get install -y fuse-overlayfs")
            return False

    async def _unmount_overlay(self) -> bool:
        """
        Unmount OverlayFS using fusermount.

        fusermount -u is the standard way to unmount FUSE filesystems.
        """
        if not self.merged_path:
            return True

        try:
            # Use fusermount -u for FUSE-based overlay
            proc = await asyncio.create_subprocess_exec(
                "fusermount", "-u", str(self.merged_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode == 0:
                self._is_mounted = False
                return True

            # Log error for debugging
            error_msg = stderr.decode() if stderr else "Unknown error"
            print(f"fusermount unmount failed: {error_msg}")
            return False

        except FileNotFoundError:
            print("fusermount not found. This should be installed with fuse-overlayfs.")
            return False

    async def _run_git(self, args: list[str]) -> subprocess.CompletedProcess:
        """Run a git command."""
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(self.repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        return subprocess.CompletedProcess(
            args=["git"] + args,
            returncode=proc.returncode,
            stdout=stdout.decode() if stdout else "",
            stderr=stderr.decode() if stderr else "",
        )

    async def _ensure_gitignore(self) -> None:
        """Ensure .overlay is in .gitignore."""
        gitignore = self.repo_path / ".gitignore"

        if gitignore.exists():
            content = gitignore.read_text()
            if ".overlay/" not in content and ".overlay" not in content:
                with open(gitignore, "a") as f:
                    f.write("\n# OverlayFS session directories\n.overlay/\n")
        else:
            gitignore.write_text("# OverlayFS session directories\n.overlay/\n")
