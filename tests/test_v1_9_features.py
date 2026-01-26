"""
Test v1.9 features:
1. sync_index batch processing (ChromaDB)
2. VERIFICATION + IMPACT_ANALYSIS integration
"""

import pytest
from pathlib import Path
from tools.chromadb_manager import ChromaDBManager
from tools.session import SessionState, Phase, VerificationEvidence, VerifiedHypothesis


class TestSyncIndexBatchProcessing:
    """Test v1.9 sync_index batch processing optimization"""

    def test_index_files_batch_single_file(self, tmp_path):
        """Test batch processing with a single file"""
        # Create test file
        test_file = tmp_path / "test.py"
        test_file.write_text("def hello(): pass\n")

        # Initialize ChromaDB manager
        manager = ChromaDBManager(project_root=tmp_path)

        # Test batch processing
        result = manager._index_files_batch([test_file])

        assert test_file in result
        assert result[test_file] >= 0  # Should have chunk count

    def test_index_files_batch_multiple_files(self, tmp_path):
        """Test batch processing with multiple files"""
        # Create test files
        files = []
        for i in range(5):
            test_file = tmp_path / f"test{i}.py"
            test_file.write_text(f"def func{i}(): pass\n")
            files.append(test_file)

        # Initialize ChromaDB manager
        manager = ChromaDBManager(project_root=tmp_path)

        # Test batch processing
        result = manager._index_files_batch(files)

        assert len(result) == 5
        for file_path in files:
            assert file_path in result
            assert result[file_path] >= 0

    def test_index_files_batch_empty_list(self, tmp_path):
        """Test batch processing with empty file list"""
        manager = ChromaDBManager(project_root=tmp_path)
        result = manager._index_files_batch([])

        assert result == {}

    def test_index_files_batch_nonexistent_file(self, tmp_path):
        """Test batch processing with nonexistent file"""
        manager = ChromaDBManager(project_root=tmp_path)
        nonexistent = tmp_path / "nonexistent.py"

        result = manager._index_files_batch([nonexistent])

        # Should handle gracefully
        assert nonexistent in result
        assert result[nonexistent] == 0

    def test_sync_forest_uses_batch_processing(self, tmp_path):
        """Test that sync_forest uses batch processing for modified/added files"""
        # Create source directory
        src_dir = tmp_path / "app"
        src_dir.mkdir()

        # Create test files
        test_file1 = src_dir / "test1.py"
        test_file1.write_text("def func1(): pass\n")
        test_file2 = src_dir / "test2.py"
        test_file2.write_text("def func2(): pass\n")

        # Initialize ChromaDB manager
        manager = ChromaDBManager(project_root=tmp_path)

        # Force sync (treats all as added)
        result = manager.sync_forest(force=True)

        assert result.added >= 2  # At least 2 files indexed
        assert result.errors == 0


@pytest.mark.skip(reason="v1.10: VERIFICATION_AND_IMPACT separated into VERIFICATION + IMPACT_ANALYSIS")
class TestVerificationAndImpactIntegration:
    """Test v1.9 VERIFICATION + IMPACT_ANALYSIS integration

    NOTE: These tests are skipped in v1.10 because:
    - Phase.VERIFICATION_AND_IMPACT was separated into Phase.VERIFICATION and Phase.IMPACT_ANALYSIS
    - submit_verification_and_impact() was replaced by submit_verification() and submit_impact_analysis()

    See test_v1_10_features.py for the new tests.
    """

    def test_integrated_phase_exists(self):
        """Test that VERIFICATION_AND_IMPACT phase exists"""
        assert hasattr(Phase, 'VERIFICATION_AND_IMPACT')

    def test_submit_verification_and_impact_method_exists(self):
        """Test that submit_verification_and_impact method exists"""
        session = SessionState(
            session_id="test",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )
        assert hasattr(session, 'submit_verification_and_impact')

    def test_submit_verification_and_impact_wrong_phase(self):
        """Test that submit_verification_and_impact fails in wrong phase"""
        session = SessionState(
            session_id="test",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )

        # Try to submit while in EXPLORATION phase
        result = session.submit_verification_and_impact(
            verified_hypotheses=[],
            verified_files=[],
        )

        assert result["success"] is False
        assert "phase" in result["message"].lower()

    def test_submit_verification_and_impact_requires_hypotheses(self):
        """Test that at least one hypothesis is required"""
        session = SessionState(
            session_id="test",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )

        # Transition to VERIFICATION_AND_IMPACT phase
        session.phase = Phase.VERIFICATION_AND_IMPACT

        # Try to submit with no hypotheses
        result = session.submit_verification_and_impact(
            verified_hypotheses=[],
            verified_files=[],
        )

        assert result["success"] is False
        assert "hypothesis" in result["message"].lower()

    def test_submit_verification_and_impact_success(self):
        """Test successful integrated submission"""
        session = SessionState(
            session_id="test",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )

        # Transition to VERIFICATION_AND_IMPACT phase
        session.phase = Phase.VERIFICATION_AND_IMPACT

        # Set up impact analysis context (required)
        session.set_impact_analysis_context(
            target_files=["test.py"],
            must_verify=["test.py"],
            should_verify=[],
            mode="standard",
        )

        # Submit with valid data
        result = session.submit_verification_and_impact(
            verified_hypotheses=[
                {
                    "hypothesis": "Test hypothesis",
                    "status": "confirmed",
                    "evidence": {
                        "tool": "find_references",
                        "target": "test_func",
                        "result": "Found in test.py:10",
                        "files": ["test.py"],
                    }
                }
            ],
            verified_files=[
                {
                    "file": "test.py",
                    "status": "will_modify",
                }
            ],
        )

        assert result["success"] is True
        assert result["next_phase"] == "READY"

    def test_submit_verification_and_impact_skip_implementation(self):
        """Test exploration-only mode with integrated phase"""
        session = SessionState(
            session_id="test",
            intent="INVESTIGATE",
            query="test query",
            repo_path=Path("."),
        )

        # Set skip_implementation flag
        session.skip_implementation = True

        # Transition to VERIFICATION_AND_IMPACT phase
        session.phase = Phase.VERIFICATION_AND_IMPACT

        # Set up impact analysis context
        session.set_impact_analysis_context(
            target_files=["test.py"],
            must_verify=["test.py"],
            should_verify=[],
            mode="standard",
        )

        # Submit with valid data
        result = session.submit_verification_and_impact(
            verified_hypotheses=[
                {
                    "hypothesis": "Test hypothesis",
                    "status": "confirmed",
                    "evidence": {
                        "tool": "find_references",
                        "target": "test_func",
                        "result": "Found in test.py:10",
                        "files": ["test.py"],
                    }
                }
            ],
            verified_files=[
                {
                    "file": "test.py",
                    "status": "no_change_needed",
                    "reason": "Exploration only",
                }
            ],
        )

        assert result["success"] is True
        assert result.get("exploration_complete") is True
        assert "Implementation skipped" in result["message"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
