"""
Branch Manager for Git-based Session Isolation.

This module provides git branch isolation for LLM sessions,
allowing all session changes to be tracked and reviewed before commit.

Features:
- Create task branch at session start (llm_task_{timestamp}_from_{base})
- Track changes via git diff
- Support garbage detection via LLM review (PRE_COMMIT phase)
- Clean merge back to base branch
- Base branch encoded in branch name (no external file needed)

Requirements:
- Git repository initialized

Usage:
    from tools.branch_manager import BranchManager

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
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
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
    prepared: bool = False  # v1.8: True if commit is prepared but not executed


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

    # =========================================================================
    # v1.2.2: Branch Naming Utilities
    # =========================================================================

    @staticmethod
    def _encode_branch_name(base_branch: str) -> str:
        """
        Encode branch name for use in task branch name.
        Replaces '/' with '__' to create valid git branch names.

        Examples:
            main -> main
            feature/login -> feature__login
            release/v1.0 -> release__v1.0
        """
        return base_branch.replace("/", "__")

    @staticmethod
    def _decode_branch_name(encoded: str) -> str:
        """
        Decode branch name from task branch name.
        Replaces '__' back to '/'.

        Examples:
            main -> main
            feature__login -> feature/login
        """
        return encoded.replace("__", "/")

    @classmethod
    def _generate_branch_name(cls, session_id: str, base_branch: str) -> str:
        """
        Generate task branch name with base branch info.

        Format: llm_task_{timestamp}_from_{encoded_base}

        Args:
            session_id: Session identifier (used as timestamp)
            base_branch: Base branch to encode

        Returns:
            Branch name like 'llm_task_20260120_114303_from_main'
        """
        encoded_base = cls._encode_branch_name(base_branch)
        return f"llm_task_{session_id}_from_{encoded_base}"

    @classmethod
    def parse_task_branch(cls, branch_name: str) -> dict | None:
        """
        Parse task branch name to extract session_id and base_branch.

        Args:
            branch_name: Branch name to parse

        Returns:
            {"session_id": str, "base_branch": str} or None if not a task branch
        """
        # Pattern: llm_task_{session_id}_from_{encoded_base}
        match = re.match(r"llm_task_([^_]+(?:_[^_]+)?)_from_(.+)$", branch_name)
        if match:
            session_id = match.group(1)
            encoded_base = match.group(2)
            return {
                "session_id": session_id,
                "base_branch": cls._decode_branch_name(encoded_base),
            }

        # Legacy format: llm_task_{session_id} (without _from_)
        # For backward compatibility
        legacy_match = re.match(r"llm_task_(.+)$", branch_name)
        if legacy_match and "_from_" not in branch_name:
            return {
                "session_id": legacy_match.group(1),
                "base_branch": None,  # Unknown for legacy format
            }

        return None

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
            base_branch = None
            if is_task:
                # Use parse_task_branch for consistent parsing
                parsed = cls.parse_task_branch(current_branch)
                if parsed:
                    session_id = parsed["session_id"]
                    base_branch = parsed["base_branch"]

            return {
                "is_task_branch": is_task,
                "current_branch": current_branch,
                "session_id": session_id,
                "base_branch": base_branch,  # v1.2.2: Include base branch info
            }

        except Exception:
            return {
                "is_task_branch": False,
                "current_branch": "",
                "session_id": None,
            }

    @classmethod
    async def list_stale_branches(cls, repo_path: str) -> dict:
        """
        List all llm_task_* branches that might be stale.

        v1.6: Used by begin_phase_gate for stale branch warning.

        Args:
            repo_path: Path to the git repository root

        Returns:
            {
                "current_branch": str,
                "is_on_task_branch": bool,
                "stale_branches": [
                    {
                        "name": str,
                        "session_id": str | None,
                        "base_branch": str | None,
                        "has_changes": bool,
                        "commit_count": int
                    }
                ]
            }
        """
        repo = Path(repo_path).resolve()
        stale_branches = []

        try:
            # Get current branch
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "--abbrev-ref", "HEAD",
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            current_branch = stdout.decode().strip() if proc.returncode == 0 else ""
            is_on_task_branch = current_branch.startswith("llm_task_")

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
                    # Parse branch info
                    parsed = cls.parse_task_branch(branch)
                    session_id = parsed["session_id"] if parsed else None
                    base_branch = parsed["base_branch"] if parsed else None

                    # Check if branch has changes compared to base
                    has_changes = False
                    commit_count = 0

                    if base_branch:
                        # Count commits ahead of base
                        proc = await asyncio.create_subprocess_exec(
                            "git", "rev-list", "--count",
                            f"{base_branch}..{branch}",
                            cwd=str(repo),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        stdout, _ = await proc.communicate()
                        if proc.returncode == 0:
                            try:
                                commit_count = int(stdout.decode().strip())
                                has_changes = commit_count > 0
                            except ValueError:
                                pass

                    stale_branches.append({
                        "name": branch,
                        "session_id": session_id,
                        "base_branch": base_branch,
                        "has_changes": has_changes,
                        "commit_count": commit_count,
                    })

            return {
                "current_branch": current_branch,
                "is_on_task_branch": is_on_task_branch,
                "stale_branches": stale_branches,
            }

        except Exception as e:
            return {
                "current_branch": "",
                "is_on_task_branch": False,
                "stale_branches": [],
                "error": str(e),
            }

    @classmethod
    async def delete_branch(cls, repo_path: str, branch_name: str, force: bool = True) -> dict:
        """
        Delete a specific branch.

        v1.6: Used by record_outcome to delete failed session branches.

        Args:
            repo_path: Path to the git repository root
            branch_name: Name of branch to delete
            force: Use -D (force) instead of -d

        Returns:
            {
                "success": bool,
                "deleted": str | None,
                "error": str | None
            }
        """
        repo = Path(repo_path).resolve()

        try:
            # Check if this is the current branch
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "--abbrev-ref", "HEAD",
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            current_branch = stdout.decode().strip() if proc.returncode == 0 else ""

            if current_branch == branch_name:
                return {
                    "success": False,
                    "deleted": None,
                    "error": f"Cannot delete current branch '{branch_name}'. Checkout another branch first.",
                }

            # Delete the branch
            delete_flag = "-D" if force else "-d"
            proc = await asyncio.create_subprocess_exec(
                "git", "branch", delete_flag, branch_name,
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode == 0:
                return {
                    "success": True,
                    "deleted": branch_name,
                    "error": None,
                }
            else:
                return {
                    "success": False,
                    "deleted": None,
                    "error": stderr.decode().strip() if stderr else "Unknown error",
                }

        except Exception as e:
            return {
                "success": False,
                "deleted": None,
                "error": str(e),
            }

    @classmethod
    async def cleanup_stale_sessions(cls, repo_path: str, action: str = "delete") -> dict:
        """
        Clean up stale task branches from interrupted runs.

        If currently on a llm_task_* branch, extracts base branch from
        the branch name (llm_task_{session}_from_{base}) and checks out
        to base branch before processing.

        Args:
            repo_path: Path to the git repository root
            action: "delete" to delete branches, "merge" to merge then delete

        Returns:
            {
                "deleted_branches": list,
                "merged_branches": list (only if action="merge"),
                "errors": list,
                "checked_out_to": str or None (if switched branch),
            }
        """
        repo = Path(repo_path).resolve()
        errors = []
        deleted_branches = []
        merged_branches = []
        checked_out_to = None

        try:
            # Get current branch
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "--abbrev-ref", "HEAD",
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            current_branch = stdout.decode().strip() if proc.returncode == 0 else ""

            # If currently on a llm_task_* branch, checkout to base branch first
            if current_branch.startswith("llm_task_"):
                # Extract base branch from name: llm_task_{session}_from_{base}
                # e.g., llm_task_session_123_from_main -> main
                # e.g., llm_task_session_123_from_feature/foo -> feature/foo
                if "_from_" in current_branch:
                    base_branch = current_branch.split("_from_", 1)[1]
                else:
                    # Fallback to main if pattern doesn't match
                    base_branch = "main"

                # Checkout to base branch
                proc = await asyncio.create_subprocess_exec(
                    "git", "checkout", base_branch,
                    cwd=str(repo),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    checked_out_to = base_branch
                else:
                    # Try 'master' as fallback
                    proc = await asyncio.create_subprocess_exec(
                        "git", "checkout", "master",
                        cwd=str(repo),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, stderr = await proc.communicate()
                    if proc.returncode == 0:
                        checked_out_to = "master"
                    else:
                        errors.append(f"Failed to checkout to {base_branch}: {stderr.decode().strip()}")

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

                # Re-check current branch after potential checkout
                proc = await asyncio.create_subprocess_exec(
                    "git", "rev-parse", "--abbrev-ref", "HEAD",
                    cwd=str(repo),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                current_branch_now = stdout.decode().strip() if proc.returncode == 0 else ""

                for branch in branches:
                    # Skip if still on this branch (checkout failed)
                    if branch == current_branch_now:
                        errors.append(f"Skipped {branch}: currently checked out")
                        continue

                    # Merge branch if action="merge"
                    if action == "merge":
                        try:
                            proc = await asyncio.create_subprocess_exec(
                                "git", "merge", branch, "--no-edit",
                                cwd=str(repo),
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                            )
                            stdout, stderr = await proc.communicate()
                            if proc.returncode == 0:
                                merged_branches.append(branch)
                            else:
                                errors.append(f"Failed to merge {branch}: {stderr.decode().strip()}")
                                continue  # Skip deletion if merge failed
                        except Exception as e:
                            errors.append(f"Merge branch {branch}: {e}")
                            continue  # Skip deletion if merge failed

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

        result = {
            "deleted_branches": deleted_branches,
            "errors": errors,
        }
        if merged_branches:
            result["merged_branches"] = merged_branches
        if checked_out_to:
            result["checked_out_to"] = checked_out_to

        return result

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

            # Step 2: Create and checkout new git branch (v1.2.2: with base branch info)
            branch_name = self._generate_branch_name(session_id, self._base_branch)
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
        This is critical because LLM edit tools modify working directory directly.

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

            # Add untracked files (new files not yet staged)
            untracked_result = await self._run_git([
                "ls-files", "--others", "--exclude-standard"
            ])
            if untracked_result.returncode == 0 and untracked_result.stdout.strip():
                for filepath in untracked_result.stdout.strip().split("\n"):
                    if filepath and filepath not in all_changes:
                        all_changes[filepath] = "A"  # Mark as added

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
                    if diff_result.returncode == 0 and diff_result.stdout.strip():
                        diff = diff_result.stdout
                        # Check if binary
                        if "Binary files" in diff:
                            is_binary = True
                            diff = None
                    elif change_type == "added":
                        # Untracked file: generate diff manually
                        diff_result = await self._run_git([
                            "diff", "--no-index", "/dev/null", filepath
                        ])
                        # --no-index returns 1 for differences, which is expected
                        if diff_result.stdout.strip():
                            diff = diff_result.stdout
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
        execute_commit: bool = True,  # v1.8: If False, prepare but don't execute commit
    ) -> FinalizeResult:
        """
        Finalize changes after garbage review.

        For discarded files, reverts them to base branch state.
        Then creates a commit with the kept changes (if execute_commit=True).

        Args:
            keep_files: Files to keep. If None, keep all.
            discard_files: Files to discard (revert). If None, discard none.
            commit_message: Commit message. Auto-generated if not provided.
            execute_commit: If True, execute commit. If False, prepare but don't commit (v1.8).

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

                # v1.8: If execute_commit=False, prepare but don't commit
                if not execute_commit:
                    return FinalizeResult(
                        success=True,
                        commit_hash=None,
                        kept_files=list(files_to_keep),
                        discarded_files=list(files_to_discard),
                        prepared=True,
                    )

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

    async def execute_prepared_commit(self, commit_message: str) -> FinalizeResult:
        """
        Execute a prepared commit (v1.8).

        This should be called after finalize(..., execute_commit=False).
        Executes the git commit on already-staged changes.

        Args:
            commit_message: Commit message

        Returns:
            FinalizeResult with commit hash
        """
        if not self._active_session or not self._base_branch:
            return FinalizeResult(
                success=False,
                error="No active session",
            )

        try:
            result = await self._run_git(["commit", "-m", commit_message])

            if result.returncode == 0:
                # Get commit hash
                hash_result = await self._run_git(["rev-parse", "HEAD"])
                commit_hash = hash_result.stdout.strip() if hash_result.returncode == 0 else None

                return FinalizeResult(
                    success=True,
                    commit_hash=commit_hash,
                )
            else:
                # Check if "nothing to commit"
                if "nothing to commit" in result.stdout or "nothing to commit" in result.stderr:
                    return FinalizeResult(
                        success=True,
                        commit_hash=None,
                    )
                return FinalizeResult(
                    success=False,
                    error=f"Git commit failed: {result.stderr}",
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


