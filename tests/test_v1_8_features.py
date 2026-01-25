"""Tests for v1.8 features: PRE_COMMIT order change and --only-explore mode."""

import pytest
import asyncio
from pathlib import Path
import sys

# Add parent directory to path to import tools
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.session import SessionState, Phase
from tools.branch_manager import BranchManager


class TestPRECOMMITOrderChange:
    """Test PRE_COMMIT + QUALITY_REVIEW order change (v1.8)."""

    def test_commit_preparation_state_initialization(self):
        """Test that commit preparation state is initialized correctly."""
        session = SessionState(
            session_id="test_session",
            intent="IMPLEMENT",
            query="test query",
            repo_path="."
        )

        # v1.8: Commit preparation state should be initialized
        assert session.commit_prepared == False
        assert session.prepared_commit_message is None
        assert session.prepared_kept_files == []
        assert session.prepared_discarded_files == []

    def test_skip_implementation_initialization(self):
        """Test that skip_implementation flag is initialized correctly."""
        session = SessionState(
            session_id="test_session",
            intent="IMPLEMENT",
            query="test query",
            repo_path="."
        )

        # v1.8: skip_implementation should be False by default
        assert session.skip_implementation == False

    def test_skip_implementation_enabled(self):
        """Test skip_implementation can be enabled."""
        session = SessionState(
            session_id="test_session",
            intent="IMPLEMENT",
            query="test query",
            repo_path="."
        )

        session.skip_implementation = True
        assert session.skip_implementation == True


class TestBranchManagerPrepareCommit:
    """Test BranchManager commit preparation (v1.8)."""

    @pytest.mark.asyncio
    async def test_finalize_with_execute_commit_false(self, tmp_path):
        """Test finalize with execute_commit=False returns prepared state."""
        # Create a temporary git repo
        repo = tmp_path / "test_repo"
        repo.mkdir()

        # Initialize git repo
        import subprocess
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)

        # Create initial commit
        test_file = repo / "test.txt"
        test_file.write_text("initial content")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)

        # Create branch manager and setup session
        manager = BranchManager(str(repo))
        session_id = "test_session_123"
        setup_result = await manager.setup_session(session_id)

        assert setup_result.success == True

        # Make changes
        test_file.write_text("modified content")

        # Finalize with execute_commit=False
        result = await manager.finalize(
            keep_files=["test.txt"],
            discard_files=[],
            commit_message="Test commit",
            execute_commit=False
        )

        # v1.8: Should return prepared=True and no commit_hash
        assert result.success == True
        assert result.prepared == True
        assert result.commit_hash is None
        assert "test.txt" in result.kept_files

    @pytest.mark.asyncio
    async def test_execute_prepared_commit(self, tmp_path):
        """Test execute_prepared_commit after finalize with execute_commit=False."""
        # Create a temporary git repo
        repo = tmp_path / "test_repo"
        repo.mkdir()

        # Initialize git repo
        import subprocess
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)

        # Create initial commit
        test_file = repo / "test.txt"
        test_file.write_text("initial content")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)

        # Create branch manager and setup session
        manager = BranchManager(str(repo))
        session_id = "test_session_123"
        await manager.setup_session(session_id)

        # Make changes
        test_file.write_text("modified content")

        # Prepare commit (don't execute)
        prepare_result = await manager.finalize(
            keep_files=["test.txt"],
            commit_message="Test commit",
            execute_commit=False
        )

        assert prepare_result.prepared == True
        assert prepare_result.commit_hash is None

        # Execute prepared commit
        execute_result = await manager.execute_prepared_commit("Quality review passed")

        # v1.8: Should return success with commit_hash
        assert execute_result.success == True
        assert execute_result.commit_hash is not None
        assert len(execute_result.commit_hash) > 0


class TestImpactAnalysisSkipImplementation:
    """Test submit_impact_analysis with skip_implementation (v1.8)."""

    def test_submit_impact_analysis_with_skip_implementation(self):
        """Test that submit_impact_analysis returns exploration_complete when skip_implementation=True."""
        session = SessionState(
            session_id="test_session",
            intent="IMPLEMENT",
            query="test query",
            repo_path="."
        )

        # Enable skip_implementation
        session.skip_implementation = True

        # Transition to IMPACT_ANALYSIS phase
        session.phase = Phase.IMPACT_ANALYSIS

        # Create mock impact analysis
        from tools.session import ImpactAnalysisResult
        session.impact_analysis = ImpactAnalysisResult(
            target_files=["test.py"],
            must_verify=[],
            should_verify=[]
        )

        # Submit impact analysis
        result = session.submit_impact_analysis(verified_files=[])

        # v1.8: Should return exploration_complete=True
        assert result["success"] == True
        assert result.get("exploration_complete") == True
        assert "Implementation skipped" in result["message"]


if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v"])
