"""
Tests for tools/session.py

Tests cover:
- Phase transitions (EXPLORATION -> SEMANTIC -> VERIFICATION -> READY)
- Validation functions (consistency, semantic reason, write target)
- Recovery features (add_explored_files, revert_to_exploration)
"""

import pytest
from unittest.mock import patch
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.session import (
    Phase,
    SemanticReason,
    SessionState,
    SessionManager,
    ExplorationResult,
    SemanticResult,
    VerificationResult,
    VerifiedHypothesis,
    VerificationEvidence,
    Hypothesis,
    evaluate_exploration,
    validate_exploration_consistency,
    validate_semantic_reason,
    validate_write_target,
    STRICT_EXPLORATION_REQUIREMENTS,
)


# =============================================================================
# Test: Phase enum
# =============================================================================

class TestPhase:
    def test_phase_order(self):
        """Phases should be in correct order."""
        assert Phase.EXPLORATION.value < Phase.SEMANTIC.value
        assert Phase.SEMANTIC.value < Phase.VERIFICATION.value
        assert Phase.VERIFICATION.value < Phase.READY.value

    def test_phase_names(self):
        """Phase names should be accessible."""
        assert Phase.EXPLORATION.name == "EXPLORATION"
        assert Phase.READY.name == "READY"


# =============================================================================
# Test: evaluate_exploration
# =============================================================================

class TestEvaluateExploration:
    def test_high_confidence_when_all_requirements_met(self):
        """Should return 'high' when all requirements are met."""
        result = ExplorationResult(
            symbols_identified=["Sym1", "Sym2", "Sym3"],
            entry_points=["Sym1.method()"],
            files_analyzed=["file1.py", "file2.py"],
            existing_patterns=["pattern1"],
            tools_used=["find_definitions", "find_references"],
        )
        confidence, missing = evaluate_exploration(result, "IMPLEMENT")
        assert confidence == "high"
        assert missing == []

    def test_low_confidence_when_missing_symbols(self):
        """Should return 'low' when symbols are insufficient."""
        result = ExplorationResult(
            symbols_identified=["Sym1"],  # Only 1, need 3
            entry_points=["Sym1.method()"],
            files_analyzed=["file1.py", "file2.py"],
            existing_patterns=["pattern1"],
            tools_used=["find_definitions", "find_references"],
        )
        confidence, missing = evaluate_exploration(result, "IMPLEMENT")
        assert confidence == "low"
        assert any("symbols_identified" in m for m in missing)

    def test_low_confidence_when_missing_tools(self):
        """Should return 'low' when required tools not used."""
        result = ExplorationResult(
            symbols_identified=["Sym1", "Sym2", "Sym3"],
            entry_points=["Sym1.method()"],
            files_analyzed=["file1.py", "file2.py"],
            existing_patterns=["pattern1"],
            tools_used=["find_definitions"],  # Missing find_references
        )
        confidence, missing = evaluate_exploration(result, "IMPLEMENT")
        assert confidence == "low"
        assert any("required_tools" in m for m in missing)

    def test_investigate_intent_has_lower_requirements(self):
        """INVESTIGATE intent should have lower requirements than IMPLEMENT."""
        result = ExplorationResult(
            symbols_identified=["Sym1", "Sym2"],
            entry_points=["Sym1.method()"],
            files_analyzed=["file1.py", "file2.py"],
            existing_patterns=[],
            tools_used=["find_definitions", "find_references"],
        )
        # Same result should pass for INVESTIGATE but may fail for IMPLEMENT
        confidence_inv, missing_inv = evaluate_exploration(result, "INVESTIGATE")
        confidence_impl, missing_impl = evaluate_exploration(result, "IMPLEMENT")
        # INVESTIGATE should have same or fewer missing requirements
        assert len(missing_inv) <= len(missing_impl)


# =============================================================================
# Test: validate_exploration_consistency
# =============================================================================

class TestValidateExplorationConsistency:
    def test_valid_exploration_returns_no_errors(self):
        """Valid exploration should return empty error list."""
        result = ExplorationResult(
            symbols_identified=["AuthService", "UserRepo"],
            entry_points=["AuthService.login()"],
            files_analyzed=["auth.py"],
            existing_patterns=["Service pattern"],
        )
        errors = validate_exploration_consistency(result)
        assert errors == []

    def test_entry_point_not_linked_to_symbol(self):
        """Entry point must be linked to a symbol."""
        result = ExplorationResult(
            symbols_identified=["AuthService"],
            entry_points=["UnknownClass.method()"],  # Not in symbols
            files_analyzed=["auth.py"],
            existing_patterns=[],
        )
        errors = validate_exploration_consistency(result)
        assert any("not linked" in e for e in errors)

    def test_duplicate_symbols_detected(self):
        """Duplicate symbols should be detected."""
        result = ExplorationResult(
            symbols_identified=["AuthService", "AuthService", "UserRepo"],
            entry_points=["AuthService.login()"],
            files_analyzed=["auth.py"],
            existing_patterns=[],
        )
        errors = validate_exploration_consistency(result)
        assert any("duplicate symbols" in e for e in errors)

    def test_duplicate_files_detected(self):
        """Duplicate files should be detected."""
        result = ExplorationResult(
            symbols_identified=["AuthService"],
            entry_points=["AuthService.login()"],
            files_analyzed=["auth.py", "auth.py"],
            existing_patterns=[],
        )
        errors = validate_exploration_consistency(result)
        assert any("duplicate files" in e for e in errors)

    def test_patterns_without_files(self):
        """Patterns require files to be analyzed."""
        result = ExplorationResult(
            symbols_identified=["AuthService"],
            entry_points=["AuthService.login()"],
            files_analyzed=[],  # No files
            existing_patterns=["Some pattern"],
        )
        errors = validate_exploration_consistency(result)
        assert any("no files analyzed" in e for e in errors)


# =============================================================================
# Test: validate_semantic_reason
# =============================================================================

class TestValidateSemanticReason:
    def test_valid_reason_for_missing_symbols(self):
        """Valid reason for missing symbols should pass."""
        is_valid, error = validate_semantic_reason(
            missing_requirements=["symbols_identified: 1/3"],
            semantic_reason=SemanticReason.NO_DEFINITION_FOUND,
        )
        assert is_valid is True
        assert error == ""

    def test_architecture_unknown_always_valid(self):
        """ARCHITECTURE_UNKNOWN should always be valid."""
        is_valid, error = validate_semantic_reason(
            missing_requirements=["symbols_identified: 1/3"],
            semantic_reason=SemanticReason.ARCHITECTURE_UNKNOWN,
        )
        assert is_valid is True

    def test_context_fragmented_always_valid(self):
        """CONTEXT_FRAGMENTED should always be valid."""
        is_valid, error = validate_semantic_reason(
            missing_requirements=["files_analyzed: 1/2"],
            semantic_reason=SemanticReason.CONTEXT_FRAGMENTED,
        )
        assert is_valid is True

    def test_empty_missing_requirements_fails(self):
        """No missing requirements should fail."""
        is_valid, error = validate_semantic_reason(
            missing_requirements=[],
            semantic_reason=SemanticReason.NO_DEFINITION_FOUND,
        )
        assert is_valid is False
        assert "No missing requirements" in error


# =============================================================================
# Test: validate_write_target
# =============================================================================

class TestValidateWriteTarget:
    def test_explored_file_is_valid(self):
        """Writing to explored file should be valid."""
        is_valid, error = validate_write_target(
            file_path="auth/service.py",
            explored_files={"auth/service.py", "auth/repo.py"},
        )
        assert is_valid is True
        assert error == ""

    def test_unexplored_file_is_invalid(self):
        """Writing to unexplored file should be invalid."""
        with patch("os.path.exists", return_value=True):  # File exists
            is_valid, error = validate_write_target(
                file_path="unknown/file.py",
                explored_files={"auth/service.py"},
            )
            assert is_valid is False
            assert "was not explored" in error

    def test_new_file_in_explored_directory_is_valid(self):
        """New file in explored directory should be valid."""
        with patch("os.path.exists", return_value=False):
            is_valid, error = validate_write_target(
                file_path="auth/new_service.py",
                explored_files={"auth/service.py"},
                allow_new_files=True,
            )
            assert is_valid is True

    def test_new_file_not_allowed(self):
        """New file creation should fail when not allowed."""
        with patch("os.path.exists", return_value=False):
            is_valid, error = validate_write_target(
                file_path="auth/new_service.py",
                explored_files={"auth/service.py"},
                allow_new_files=False,
            )
            assert is_valid is False
            assert "not allowed" in error


# =============================================================================
# Test: SessionState
# =============================================================================

class TestSessionState:
    def test_initial_phase_is_exploration(self):
        """New session should start in EXPLORATION phase."""
        session = SessionState(
            session_id="test_session",
            intent="IMPLEMENT",
            query="Test query",
        )
        assert session.phase == Phase.EXPLORATION

    def test_question_intent_starts_in_ready(self):
        """QUESTION intent should start in READY phase."""
        manager = SessionManager()
        session = manager.create_session(
            intent="QUESTION",
            query="What is X?",
        )
        assert session.phase == Phase.READY

    def test_allowed_tools_in_exploration(self):
        """EXPLORATION phase should allow code-intel tools."""
        session = SessionState(
            session_id="test",
            intent="IMPLEMENT",
            query="Test",
        )
        allowed = session.get_allowed_tools()
        assert "find_definitions" in allowed
        assert "find_references" in allowed
        assert "semantic_search" not in allowed

    def test_allowed_tools_in_ready(self):
        """READY phase should allow all tools."""
        session = SessionState(
            session_id="test",
            intent="IMPLEMENT",
            query="Test",
            phase=Phase.READY,
        )
        allowed = session.get_allowed_tools()
        assert "*" in allowed


# =============================================================================
# Test: v3.10 add_explored_files
# =============================================================================

class TestAddExploredFiles:
    def test_add_files_in_ready_phase(self):
        """Should add files when in READY phase."""
        session = SessionState(
            session_id="test",
            intent="IMPLEMENT",
            query="Test",
            phase=Phase.READY,
        )
        session.exploration = ExplorationResult(
            files_analyzed=["existing.py"],
        )

        result = session.add_explored_files(["new_file.py", "another.py"])

        assert result["success"] is True
        assert "new_file.py" in result["added"]
        assert "another.py" in result["added"]
        assert "new_file.py" in session.exploration.files_analyzed

    def test_add_files_fails_in_exploration_phase(self):
        """Should fail when not in READY phase."""
        session = SessionState(
            session_id="test",
            intent="IMPLEMENT",
            query="Test",
            phase=Phase.EXPLORATION,
        )

        result = session.add_explored_files(["new_file.py"])

        assert result["success"] is False
        assert "only allowed in READY phase" in result["error"]

    def test_add_files_skips_duplicates(self):
        """Should skip files already in list."""
        session = SessionState(
            session_id="test",
            intent="IMPLEMENT",
            query="Test",
            phase=Phase.READY,
        )
        session.exploration = ExplorationResult(
            files_analyzed=["existing.py"],
        )

        result = session.add_explored_files(["existing.py", "new_file.py"])

        assert result["success"] is True
        assert "existing.py" not in result["added"]
        assert "new_file.py" in result["added"]

    def test_add_files_creates_exploration_if_none(self):
        """Should create ExplorationResult if None."""
        session = SessionState(
            session_id="test",
            intent="IMPLEMENT",
            query="Test",
            phase=Phase.READY,
        )
        session.exploration = None

        result = session.add_explored_files(["new_file.py"])

        assert result["success"] is True
        assert session.exploration is not None
        assert "new_file.py" in session.exploration.files_analyzed

    def test_add_empty_files_fails(self):
        """Should fail when files list is empty."""
        session = SessionState(
            session_id="test",
            intent="IMPLEMENT",
            query="Test",
            phase=Phase.READY,
        )

        result = session.add_explored_files([])

        assert result["success"] is False
        assert "No files provided" in result["error"]


# =============================================================================
# Test: v3.10 revert_to_exploration
# =============================================================================

class TestRevertToExploration:
    def test_revert_from_ready_to_exploration(self):
        """Should revert from READY to EXPLORATION."""
        session = SessionState(
            session_id="test",
            intent="IMPLEMENT",
            query="Test",
            phase=Phase.READY,
        )
        session.exploration = ExplorationResult(
            symbols_identified=["Sym1"],
            files_analyzed=["file.py"],
        )

        result = session.revert_to_exploration()

        assert result["success"] is True
        assert result["previous_phase"] == "READY"
        assert result["current_phase"] == "EXPLORATION"
        assert session.phase == Phase.EXPLORATION
        # Exploration should be kept
        assert session.exploration is not None

    def test_revert_from_semantic_to_exploration(self):
        """Should revert from SEMANTIC to EXPLORATION."""
        session = SessionState(
            session_id="test",
            intent="IMPLEMENT",
            query="Test",
            phase=Phase.SEMANTIC,
        )

        result = session.revert_to_exploration()

        assert result["success"] is True
        assert result["previous_phase"] == "SEMANTIC"
        assert session.phase == Phase.EXPLORATION

    def test_revert_keeps_results_by_default(self):
        """Should keep exploration results by default."""
        session = SessionState(
            session_id="test",
            intent="IMPLEMENT",
            query="Test",
            phase=Phase.READY,
        )
        session.exploration = ExplorationResult(
            symbols_identified=["Sym1"],
        )
        session.semantic = SemanticResult(
            hypotheses=[Hypothesis(text="Test hypothesis")],
            semantic_reason=SemanticReason.ARCHITECTURE_UNKNOWN,
        )

        result = session.revert_to_exploration(keep_results=True)

        assert result["success"] is True
        # Semantic should be kept when keep_results=True
        assert session.semantic is not None

    def test_revert_clears_results_when_requested(self):
        """Should clear results when keep_results=False."""
        session = SessionState(
            session_id="test",
            intent="IMPLEMENT",
            query="Test",
            phase=Phase.READY,
        )
        session.exploration = ExplorationResult(
            symbols_identified=["Sym1"],
        )
        session.semantic = SemanticResult(
            hypotheses=[Hypothesis(text="Test hypothesis")],
            semantic_reason=SemanticReason.ARCHITECTURE_UNKNOWN,
        )

        result = session.revert_to_exploration(keep_results=False)

        assert result["success"] is True
        # Semantic should be cleared
        assert session.semantic is None
        # Exploration should be kept even when keep_results=False
        assert session.exploration is not None

    def test_revert_when_already_in_exploration(self):
        """Should succeed when already in EXPLORATION."""
        session = SessionState(
            session_id="test",
            intent="IMPLEMENT",
            query="Test",
            phase=Phase.EXPLORATION,
        )

        result = session.revert_to_exploration()

        assert result["success"] is True
        assert "Already in EXPLORATION" in result["message"]

    def test_revert_records_phase_history(self):
        """Should record revert in phase history."""
        session = SessionState(
            session_id="test",
            intent="IMPLEMENT",
            query="Test",
            phase=Phase.READY,
        )

        session.revert_to_exploration()

        assert len(session.phase_history) == 1
        assert session.phase_history[0]["action"] == "revert_to_exploration"
        assert session.phase_history[0]["from"] == "READY"
        assert session.phase_history[0]["to"] == "EXPLORATION"


# =============================================================================
# Test: SessionManager
# =============================================================================

class TestSessionManager:
    def test_create_session(self):
        """Should create and track session."""
        manager = SessionManager()
        session = manager.create_session(
            intent="IMPLEMENT",
            query="Test query",
        )

        assert session is not None
        assert session.intent == "IMPLEMENT"
        assert manager.get_active_session() == session

    def test_get_session_by_id(self):
        """Should retrieve session by ID."""
        manager = SessionManager()
        session = manager.create_session(
            intent="IMPLEMENT",
            query="Test",
        )

        retrieved = manager.get_session(session.session_id)
        assert retrieved == session

    def test_list_sessions(self):
        """Should list all sessions."""
        manager = SessionManager()
        # Clear any existing sessions from previous tests
        manager._sessions = {}
        manager._active_session_id = None

        # Use explicit session IDs to avoid timestamp collision
        manager.create_session(intent="IMPLEMENT", query="Test 1", session_id="test_session_1")
        manager.create_session(intent="MODIFY", query="Test 2", session_id="test_session_2")

        sessions = manager.list_sessions()
        assert len(sessions) == 2


# =============================================================================
# Test: check_write_target with recovery_options
# =============================================================================

class TestCheckWriteTargetRecovery:
    def test_blocked_response_includes_recovery_options(self):
        """Blocked write should include recovery options."""
        session = SessionState(
            session_id="test",
            intent="IMPLEMENT",
            query="Test",
            phase=Phase.READY,
        )
        session.exploration = ExplorationResult(
            files_analyzed=["existing/file.py"],
        )

        result = session.check_write_target("unknown/new_file.py")

        assert result["allowed"] is False
        assert "recovery_options" in result
        assert "add_explored_files" in result["recovery_options"]
        assert "revert_to_exploration" in result["recovery_options"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
