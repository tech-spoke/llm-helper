"""
Branch Manager for Git-based Session Isolation.

This module provides git branch isolation for LLM sessions,
allowing all session changes to be tracked and reviewed before commit.

v1.2 Original: OverlayFS + Git Integration
v1.2.1: Git branch only (OverlayFS removed)

Rationale for removing OverlayFS:
- LLM edit tools use repository root path, not merged_path
- Changes were applied directly to lower layer, bypassing overlay
- Without parallel execution benefit, git branch alone is sufficient

Features:
- Create task branch at session start (llm_task_{session_id})
- Track changes via git diff (not overlay upper layer)
- Support garbage detection via LLM review (PRE_COMMIT phase)
- Clean merge back to base branch

Requirements:
- Git repository initialized

Usage:
    from tools.overlay_manager import BranchManager

    manager = BranchManager("/path/to/repo")

    # At session start
    result = await manager.setup_session("session_123")
    # result.branch_name = "llm_task_session_123"

    # At PRE_COMMIT (garbage detection)
    changes = await manager.get_changes()
    # returns list of changed files with diff

    # After review
    await manager.finalize(keep_files=["auth/service.py"], discard_files=["debug.log"])

    # Merge to base branch
    await manager.merge_to_base()

    # Cleanup (delete task branch without merge)
    await manager.cleanup()
"""

import asyncio
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass
class BranchSetupResult:
    """Result of branch setup operation."""
    success: bool
    session_id: str
    branch_name: str
    base_branch: str
    error: str | None = None


@dataclass
class FileChange:
    """Represents a single file change."""
    path: str  # Relative path from repo root
    change_type: Literal["added", "modified", "deleted"]
    diff: str | None = None  # Unified diff for text files
    is_binary: bool = False
    size_bytes: int = 0


@dataclass
class BranchChanges:
    """All changes in the current branch vs base branch."""
    session_id: str
    changes: list[FileChange] = field(default_factory=list)
    total_files: int = 0


@dataclass
class FinalizeResult:
    """Result of finalize operation."""
    success: bool
    commit_hash: str | None = None
    kept_files: list[str] = field(default_factory=list)
    discarded_files: list[str] = field(default_factory=list)
    error: str | None = None


class BranchManager:
    """
    Manages git branches for session-based file isolation.

    Each session gets its own branch, capturing all changes
    for review before commit and merge.
    """

    def __init__(self, repo_path: str):
        """
        Initialize BranchManager.

        Args:
            repo_path: Path to the git repository root
        """
        self.repo_path = Path(repo_path).resolve()

        # Active session tracking
        self._active_session: str | None = None
        self._branch_name: str | None = None
        self._base_branch: str | None = None  # Branch we started from

    @classmethod
    async def is_task_branch_checked_out(cls, repo_path: str) -> dict:
        """
        Check if a llm_task_* branch is currently checked out.

        This is used as a guard to prevent accidental parallel sessions.

        Args:
            repo_path: Path to the git repository root

        Returns:
            {
                "is_task_branch": bool,
                "current_branch": str,
                "session_id": str | None  # Extracted from branch name if task branch
            }
        """
        repo = Path(repo_path).resolve()

        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "--abbrev-ref", "HEAD",
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode != 0:
                return {
                    "is_task_branch": False,
                    "current_branch": "",
                    "session_id": None,
                }

            current_branch = stdout.decode().strip()
            is_task = current_branch.startswith("llm_task_")

            session_id = None
            if is_task:
                # Extract session_id from branch name
                # Format: llm_task_{session_id} or llm_task_session_{session_id}
                parts = current_branch.split("_", 2)  # ['llm', 'task', 'session_xxx']
                if len(parts) >= 3:
                    session_id = parts[2]

            return {
                "is_task_branch": is_task,
                "current_branch": current_branch,
                "session_id": session_id,
            }

        except Exception:
            return {
                "is_task_branch": False,
                "current_branch": "",
                "session_id": None,
            }

    @classmethod
    async def cleanup_stale_sessions(cls, repo_path: str) -> dict:
        """
        Clean up stale task branches from interrupted runs.

        Args:
            repo_path: Path to the git repository root

        Returns:
            {
                "deleted_branches": list,
                "errors": list,
            }
        """
        repo = Path(repo_path).resolve()
        errors = []
        deleted_branches = []

        try:
            # Get current branch to avoid deleting it
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "--abbrev-ref", "HEAD",
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            current_branch = stdout.decode().strip() if proc.returncode == 0 else ""

            # List all llm_task_* branches
            proc = await asyncio.create_subprocess_exec(
                "git", "branch", "--list", "llm_task_*",
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode == 0 and stdout:
                branches = [
                    b.strip().lstrip("* ")
                    for b in stdout.decode().strip().split("\n")
                    if b.strip()
                ]
                for branch in branches:
                    # Skip current branch
                    if branch == current_branch:
                        errors.append(f"Skipped {branch}: currently checked out")
                        continue

                    # Delete branch
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
                        else:
                            errors.append(f"Failed to delete {branch}")
                    except Exception as e:
                        errors.append(f"Delete branch {branch}: {e}")

        except Exception as e:
            errors.append(f"List branches: {e}")

        return {
            "deleted_branches": deleted_branches,
            "errors": errors,
        }

    async def setup_session(self, session_id: str) -> BranchSetupResult:
        """
        Set up Git branch for a new session.

        Steps:
        1. Check if already on a task branch (guard)
        2. Record current branch as base
        3. Create and checkout new branch: llm_task_{session_id}

        Args:
            session_id: Unique session identifier

        Returns:
            BranchSetupResult with branch info and status
        """
        try:
            # Check if already has an active session
            if self._active_session:
                return BranchSetupResult(
                    success=False,
                    session_id=session_id,
                    branch_name="",
                    base_branch="",
                    error=f"Session {self._active_session} is already active. Call cleanup() first.",
                )

            # Guard: Check if already on a task branch
            guard_result = await self.is_task_branch_checked_out(str(self.repo_path))
            if guard_result["is_task_branch"]:
                return BranchSetupResult(
                    success=False,
                    session_id=session_id,
                    branch_name="",
                    base_branch="",
                    error=(
                        f"Already on task branch '{guard_result['current_branch']}'. "
                        f"Another session may be active (session_id: {guard_result['session_id']}). "
                        f"Run cleanup_stale_sessions or checkout a different branch first."
                    ),
                )

            # Step 1: Get current branch (base branch to merge back to)
            base_result = await self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
            if base_result.returncode == 0:
                self._base_branch = base_result.stdout.strip()
            else:
                self._base_branch = "main"  # Fallback

            # Step 2: Create and checkout new git branch
            branch_name = f"llm_task_{session_id}"
            result = await self._run_git(["checkout", "-b", branch_name])
            if result.returncode != 0:
                # Branch might already exist, try to checkout
                result = await self._run_git(["checkout", branch_name])
                if result.returncode != 0:
                    return BranchSetupResult(
                        success=False,
                        session_id=session_id,
                        branch_name=branch_name,
                        base_branch=self._base_branch,
                        error=f"Failed to create/checkout branch: {result.stderr}",
                    )

            self._branch_name = branch_name
            self._active_session = session_id

            return BranchSetupResult(
                success=True,
                session_id=session_id,
                branch_name=branch_name,
                base_branch=self._base_branch,
            )

        except Exception as e:
            return BranchSetupResult(
                success=False,
                session_id=session_id,
                branch_name="",
                base_branch="",
                error=str(e),
            )

    async def get_changes(self) -> BranchChanges:
        """
        Get all changes in current branch compared to base branch.

        Includes both committed and uncommitted changes.
        This is critical because LLM edit tools modify working directory
        directly, not via overlay.

        Returns:
            BranchChanges with list of all changed files
        """
        if not self._active_session or not self._base_branch:
            return BranchChanges(session_id="", changes=[], total_files=0)

        changes = []

        try:
            # Get uncommitted changes (working directory vs HEAD)
            uncommitted_result = await self._run_git([
                "diff", "--name-status", "HEAD"
            ])

            # Get committed changes on this branch vs base
            committed_result = await self._run_git([
                "diff", "--name-status",
                f"{self._base_branch}...HEAD"
            ])

            if committed_result.returncode != 0:
                # Fall back to direct diff if three-dot fails
                committed_result = await self._run_git([
                    "diff", "--name-status",
                    self._base_branch, "HEAD"
                ])

            # Combine results (uncommitted changes take precedence)
            all_changes = {}

            # Add committed changes first
            if committed_result.returncode == 0 and committed_result.stdout.strip():
                for line in committed_result.stdout.strip().split("\n"):
                    if line:
                        parts = line.split("\t", 1)
                        if len(parts) >= 2:
                            all_changes[parts[1]] = parts[0]

            # Add/override with uncommitted changes
            if uncommitted_result.returncode == 0 and uncommitted_result.stdout.strip():
                for line in uncommitted_result.stdout.strip().split("\n"):
                    if line:
                        parts = line.split("\t", 1)
                        if len(parts) >= 2:
                            all_changes[parts[1]] = parts[0]

            # Process each changed file
            for filepath, status in all_changes.items():
                # Map git status to change type
                if status.startswith("A"):
                    change_type = "added"
                elif status.startswith("D"):
                    change_type = "deleted"
                else:  # M, R, C, etc.
                    change_type = "modified"

                # Get diff for this file (includes uncommitted changes)
                diff = None
                is_binary = False

                if change_type != "deleted":
                    # First try: working directory vs base branch
                    diff_result = await self._run_git([
                        "diff", self._base_branch, "--", filepath
                    ])
                    if diff_result.returncode != 0 or not diff_result.stdout.strip():
                        # Fallback: committed changes only
                        diff_result = await self._run_git([
                            "diff", f"{self._base_branch}...HEAD", "--", filepath
                        ])
                    if diff_result.returncode == 0:
                        diff = diff_result.stdout
                        # Check if binary
                        if "Binary files" in diff:
                            is_binary = True
                            diff = None

                changes.append(FileChange(
                    path=filepath,
                    change_type=change_type,
                    diff=diff,
                    is_binary=is_binary,
                    size_bytes=0,
                ))

            return BranchChanges(
                session_id=self._active_session,
                changes=changes,
                total_files=len(changes),
            )

        except Exception:
            return BranchChanges(
                session_id=self._active_session,
                changes=[],
                total_files=0,
            )

    async def finalize(
        self,
        keep_files: list[str] | None = None,
        discard_files: list[str] | None = None,
        commit_message: str | None = None,
    ) -> FinalizeResult:
        """
        Finalize changes after garbage review.

        For discarded files, reverts them to base branch state.
        Then creates a commit with the kept changes.

        Args:
            keep_files: Files to keep. If None, keep all.
            discard_files: Files to discard (revert). If None, discard none.
            commit_message: Commit message. Auto-generated if not provided.

        Returns:
            FinalizeResult with commit hash and file lists
        """
        if not self._active_session or not self._base_branch:
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

            # Revert discarded files to base branch state
            for filepath in files_to_discard:
                await self._run_git(["checkout", self._base_branch, "--", filepath])

            # Stage all changes (including reverts)
            if files_to_keep:
                await self._run_git(["add", "-A"])

                if commit_message is None:
                    commit_message = f"Session {self._active_session}: Apply {len(files_to_keep)} file(s)"

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
                    # Check if "nothing to commit"
                    if "nothing to commit" in result.stdout or "nothing to commit" in result.stderr:
                        return FinalizeResult(
                            success=True,
                            commit_hash=None,
                            kept_files=list(files_to_keep),
                            discarded_files=list(files_to_discard),
                        )
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

    @property
    def base_branch(self) -> str | None:
        """Get the base branch this session was started from."""
        return self._base_branch

    async def merge_to_base(self, delete_branch: bool = True) -> dict:
        """
        Merge task branch back to the base branch.

        Args:
            delete_branch: Delete task branch after successful merge (default: True)

        Returns:
            {
                "success": bool,
                "merged": bool,
                "branch_deleted": bool,
                "from_branch": str,
                "to_branch": str,
                "error": str | None
            }
        """
        if not self._branch_name:
            return {
                "success": False,
                "merged": False,
                "branch_deleted": False,
                "from_branch": "",
                "to_branch": "",
                "error": "No active branch",
            }

        if not self._base_branch:
            return {
                "success": False,
                "merged": False,
                "branch_deleted": False,
                "from_branch": self._branch_name,
                "to_branch": "",
                "error": "Base branch not recorded.",
            }

        task_branch = self._branch_name
        target_branch = self._base_branch

        try:
            # Checkout base branch
            result = await self._run_git(["checkout", target_branch])
            if result.returncode != 0:
                return {
                    "success": False,
                    "merged": False,
                    "branch_deleted": False,
                    "from_branch": task_branch,
                    "to_branch": target_branch,
                    "error": f"Failed to checkout {target_branch}: {result.stderr}",
                }

            # Merge task branch
            result = await self._run_git([
                "merge", "--no-ff", task_branch,
                "-m", f"Merge {task_branch}"
            ])
            if result.returncode != 0:
                # Revert to task branch on failure
                await self._run_git(["checkout", task_branch])
                return {
                    "success": False,
                    "merged": False,
                    "branch_deleted": False,
                    "from_branch": task_branch,
                    "to_branch": target_branch,
                    "error": f"Merge failed: {result.stderr}",
                }

            # Delete task branch after successful merge
            branch_deleted = False
            if delete_branch:
                delete_result = await self._run_git(["branch", "-d", task_branch])
                branch_deleted = delete_result.returncode == 0
                if not branch_deleted:
                    # Try force delete
                    delete_result = await self._run_git(["branch", "-D", task_branch])
                    branch_deleted = delete_result.returncode == 0

            # Clear session state
            self._active_session = None
            self._branch_name = None

            return {
                "success": True,
                "merged": True,
                "branch_deleted": branch_deleted,
                "from_branch": task_branch,
                "to_branch": target_branch,
                "error": None,
            }

        except Exception as e:
            return {
                "success": False,
                "merged": False,
                "branch_deleted": False,
                "from_branch": task_branch,
                "to_branch": target_branch,
                "error": str(e),
            }

    async def cleanup(self) -> bool:
        """
        Clean up session (checkout base branch, optionally delete task branch).

        This does NOT delete the task branch by default (use merge_to_base for that).

        Returns:
            True if cleanup successful
        """
        if not self._active_session:
            return True

        try:
            # Checkout base branch if we have one
            if self._base_branch:
                await self._run_git(["checkout", self._base_branch])

            self._active_session = None
            self._branch_name = None

            return True

        except Exception:
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


# =============================================================================
# Backward Compatibility Aliases
# =============================================================================

# Keep old names for backward compatibility
OverlayManager = BranchManager
OverlaySetupResult = BranchSetupResult
OverlayChanges = BranchChanges
