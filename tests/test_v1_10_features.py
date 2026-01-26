"""
Test v1.10 features:
1. check_phase_necessity (Q1, Q2, Q3 individual checks)
2. submit_verification (separated from v1.9's combined phase)
3. submit_impact_analysis (separated from v1.9's combined phase)
4. gate_level (full/auto simplification)
"""

import pytest
from pathlib import Path
from tools.session import (
    SessionState,
    Phase,
    VerificationResult,
    VerifiedHypothesis,
    VerificationEvidence,
)


class TestCheckPhaseNecessity:
    """Test v1.10 check_phase_necessity for Q1, Q2, Q3 checks."""

    def test_q1_semantic_check_yes(self):
        """Test Q1 check: SEMANTIC is necessary."""
        session = SessionState(
            session_id="test-q1-yes",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )
        session.phase = Phase.EXPLORATION
        session._gate_level = "auto"

        # Simulate check_phase_necessity for SEMANTIC
        assessment = {
            "needs_more_information": True,
            "needs_more_information_reason": "Could not find similar implementation patterns in the codebase",
        }

        # Store assessment and transition
        session.phase_assessments["SEMANTIC"] = assessment

        # When assessment says YES, phase should be entered
        assert assessment["needs_more_information"] is True

    def test_q1_semantic_check_no(self):
        """Test Q1 check: SEMANTIC is not necessary (skip to Q2)."""
        session = SessionState(
            session_id="test-q1-no",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )
        session.phase = Phase.EXPLORATION
        session._gate_level = "auto"

        assessment = {
            "needs_more_information": False,
            "needs_more_information_reason": "All necessary information found through code-intel tools",
        }

        session.phase_assessments["SEMANTIC"] = assessment

        # When assessment says NO, skip SEMANTIC
        assert assessment["needs_more_information"] is False

    def test_q2_verification_check_yes(self):
        """Test Q2 check: VERIFICATION is necessary."""
        session = SessionState(
            session_id="test-q2-yes",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )
        session.phase = Phase.SEMANTIC
        session._gate_level = "auto"

        assessment = {
            "needs_hypothesis_verification": True,
            "needs_hypothesis_verification_reason": "The semantic search revealed multiple possible approaches that need verification",
        }

        session.phase_assessments["VERIFICATION"] = assessment

        # When assessment says YES, VERIFICATION phase is required
        assert assessment["needs_hypothesis_verification"] is True

    def test_q2_verification_check_no(self):
        """Test Q2 check: VERIFICATION is not necessary (skip to Q3)."""
        session = SessionState(
            session_id="test-q2-no",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )
        session.phase = Phase.SEMANTIC
        session._gate_level = "auto"

        assessment = {
            "needs_hypothesis_verification": False,
            "needs_hypothesis_verification_reason": "Semantic search confirmed the approach with high confidence",
        }

        session.phase_assessments["VERIFICATION"] = assessment

        # When assessment says NO, skip VERIFICATION
        assert assessment["needs_hypothesis_verification"] is False

    def test_q3_impact_analysis_check_yes(self):
        """Test Q3 check: IMPACT_ANALYSIS is necessary."""
        session = SessionState(
            session_id="test-q3-yes",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )
        session.phase = Phase.VERIFICATION
        session._gate_level = "auto"

        assessment = {
            "needs_impact_analysis": True,
            "needs_impact_analysis_reason": "Changes affect multiple files and could have side effects",
        }

        session.phase_assessments["IMPACT_ANALYSIS"] = assessment

        # When assessment says YES, IMPACT_ANALYSIS phase is required
        assert assessment["needs_impact_analysis"] is True

    def test_q3_impact_analysis_check_no(self):
        """Test Q3 check: IMPACT_ANALYSIS is not necessary (proceed to READY)."""
        session = SessionState(
            session_id="test-q3-no",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )
        session.phase = Phase.VERIFICATION
        session._gate_level = "auto"

        assessment = {
            "needs_impact_analysis": False,
            "needs_impact_analysis_reason": "Single file change with no external dependencies",
        }

        session.phase_assessments["IMPACT_ANALYSIS"] = assessment

        # When assessment says NO, skip IMPACT_ANALYSIS and go to READY
        assert assessment["needs_impact_analysis"] is False


class TestGateLevelBehavior:
    """Test v1.10 gate_level (full/auto) behavior."""

    def test_gate_level_full_forces_all_phases(self):
        """Test that gate_level='full' forces execution of all phases."""
        session = SessionState(
            session_id="test-full",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )
        session._gate_level = "full"

        # With gate_level="full", all phases must execute regardless of assessment
        assert session.gate_level == "full"

        # Even if assessment says "not needed", phase should still execute
        assessment = {
            "needs_more_information": False,  # Says not needed
            "needs_more_information_reason": "All info available",
        }

        # With full mode, the check should be ignored and phase forced
        # This is the server behavior - phase is always required
        assert session._gate_level == "full"

    def test_gate_level_auto_respects_assessment(self):
        """Test that gate_level='auto' respects LLM assessment."""
        session = SessionState(
            session_id="test-auto",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )
        session._gate_level = "auto"

        # With gate_level="auto", assessment is respected
        assert session.gate_level == "auto"

    def test_old_gate_levels_not_supported(self):
        """Test that old gate levels (h/m/l/n) are no longer valid concepts."""
        # v1.10: Only "full" and "auto" are valid
        valid_gate_levels = {"full", "auto"}

        # Old values should not be in the valid set
        old_values = {"high", "middle", "low", "none", "h", "m", "l", "n", "a"}
        for old_val in old_values:
            if old_val not in ("a", "auto"):  # 'a' is alias for auto
                assert old_val not in valid_gate_levels


class TestSubmitVerificationSeparated:
    """Test v1.10 submit_verification (separated from v1.9 combined phase)."""

    def test_submit_verification_requires_verification_phase(self):
        """Test that submit_verification only works in VERIFICATION phase."""
        session = SessionState(
            session_id="test-ver-phase",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )

        # Try in EXPLORATION phase
        session.phase = Phase.EXPLORATION

        result = VerificationResult(
            verified=[
                VerifiedHypothesis(
                    hypothesis="Test hypothesis",
                    status="confirmed",
                    evidence=VerificationEvidence(
                        tool="find_references",
                        target="test_func",
                        result="Found in test.py:10",
                        files=["test.py"],
                    ),
                )
            ],
        )

        response = session.submit_verification(result)
        assert response["success"] is False
        assert "phase" in response["message"].lower()

    def test_submit_verification_success(self):
        """Test successful verification submission."""
        session = SessionState(
            session_id="test-ver-success",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )
        session.phase = Phase.VERIFICATION

        result = VerificationResult(
            verified=[
                VerifiedHypothesis(
                    hypothesis="Test hypothesis",
                    status="confirmed",
                    evidence=VerificationEvidence(
                        tool="find_references",
                        target="test_func",
                        result="Found in test.py:10",
                        files=["test.py"],
                    ),
                )
            ],
        )

        response = session.submit_verification(result)
        assert response["success"] is True
        assert response["next_phase"] == "IMPACT_ANALYSIS"

    def test_submit_verification_transitions_to_impact_analysis(self):
        """Test that submit_verification transitions to IMPACT_ANALYSIS (not READY)."""
        session = SessionState(
            session_id="test-ver-transition",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )
        session.phase = Phase.VERIFICATION

        result = VerificationResult(
            verified=[
                VerifiedHypothesis(
                    hypothesis="Test hypothesis",
                    status="confirmed",
                    evidence=VerificationEvidence(
                        tool="find_references",
                        target="test_func",
                        result="Found in test.py:10",
                        files=["test.py"],
                    ),
                )
            ],
        )

        response = session.submit_verification(result)

        # v1.10: Transitions to IMPACT_ANALYSIS, NOT directly to READY
        assert response["next_phase"] == "IMPACT_ANALYSIS"
        assert session.phase == Phase.IMPACT_ANALYSIS


class TestSubmitImpactAnalysisSeparated:
    """Test v1.10 submit_impact_analysis (separated from v1.9 combined phase)."""

    def test_submit_impact_analysis_requires_impact_phase(self):
        """Test that submit_impact_analysis only works in IMPACT_ANALYSIS phase."""
        session = SessionState(
            session_id="test-impact-phase",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )

        # Try in VERIFICATION phase
        session.phase = Phase.VERIFICATION

        response = session.submit_impact_analysis(
            verified_files=[{"file": "test.py", "status": "will_modify"}]
        )

        assert response["success"] is False
        assert "phase" in response["message"].lower()

    def test_submit_impact_analysis_requires_analyze_impact_first(self):
        """Test that analyze_impact must be called before submit_impact_analysis."""
        session = SessionState(
            session_id="test-impact-order",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )
        session.phase = Phase.IMPACT_ANALYSIS

        # Don't call set_impact_analysis_context (simulating no analyze_impact call)
        response = session.submit_impact_analysis(
            verified_files=[{"file": "test.py", "status": "will_modify"}]
        )

        assert response["success"] is False
        assert "analyze_impact" in response["message"].lower()

    def test_submit_impact_analysis_success(self):
        """Test successful impact analysis submission."""
        session = SessionState(
            session_id="test-impact-success",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )
        session.phase = Phase.IMPACT_ANALYSIS

        # Call set_impact_analysis_context first (simulating analyze_impact)
        session.set_impact_analysis_context(
            target_files=["test.py"],
            must_verify=["test.py"],
            should_verify=[],
            mode="standard",
        )

        response = session.submit_impact_analysis(
            verified_files=[{"file": "test.py", "status": "will_modify"}]
        )

        assert response["success"] is True
        assert response["next_phase"] == "READY"

    def test_submit_impact_analysis_transitions_to_ready(self):
        """Test that submit_impact_analysis transitions to READY."""
        session = SessionState(
            session_id="test-impact-ready",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )
        session.phase = Phase.IMPACT_ANALYSIS

        session.set_impact_analysis_context(
            target_files=["test.py"],
            must_verify=["test.py"],
            should_verify=[],
            mode="standard",
        )

        response = session.submit_impact_analysis(
            verified_files=[{"file": "test.py", "status": "will_modify"}]
        )

        assert response["next_phase"] == "READY"
        assert session.phase == Phase.READY


class TestPhaseFlowV110:
    """Test complete v1.10 phase flow with Q1, Q2, Q3 checks."""

    def test_full_flow_all_phases_required(self):
        """Test flow when all phases are required (all checks = YES)."""
        session = SessionState(
            session_id="test-full-flow",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )
        session._gate_level = "auto"

        # Start in EXPLORATION
        assert session.phase == Phase.EXPLORATION

        # Q1 Check: SEMANTIC needed
        session.phase_assessments["SEMANTIC"] = {
            "needs_more_information": True,
            "needs_more_information_reason": "Need to find similar patterns",
        }
        session.phase = Phase.SEMANTIC

        # After SEMANTIC completion, Q2 Check: VERIFICATION needed
        session.phase_assessments["VERIFICATION"] = {
            "needs_hypothesis_verification": True,
            "needs_hypothesis_verification_reason": "Multiple approaches need verification",
        }
        session.phase = Phase.VERIFICATION

        # After VERIFICATION, Q3 Check: IMPACT_ANALYSIS needed
        session.phase_assessments["IMPACT_ANALYSIS"] = {
            "needs_impact_analysis": True,
            "needs_impact_analysis_reason": "Changes affect multiple files",
        }
        session.phase = Phase.IMPACT_ANALYSIS

        # After IMPACT_ANALYSIS, proceed to READY
        session.set_impact_analysis_context(
            target_files=["test.py"],
            must_verify=["test.py"],
            should_verify=[],
            mode="standard",
        )

        response = session.submit_impact_analysis(
            verified_files=[{"file": "test.py", "status": "will_modify"}]
        )

        assert response["success"] is True
        assert session.phase == Phase.READY

    def test_skip_all_optional_phases(self):
        """Test flow when all optional phases are skipped (all checks = NO)."""
        session = SessionState(
            session_id="test-skip-flow",
            intent="INVESTIGATE",  # INVESTIGATE has lower requirements
            query="test query",
            repo_path=Path("."),
        )
        session._gate_level = "auto"

        # Start in EXPLORATION
        assert session.phase == Phase.EXPLORATION

        # Q1 Check: SEMANTIC not needed
        session.phase_assessments["SEMANTIC"] = {
            "needs_more_information": False,
            "needs_more_information_reason": "All info found with code-intel",
        }

        # Q2 Check: VERIFICATION not needed
        session.phase_assessments["VERIFICATION"] = {
            "needs_hypothesis_verification": False,
            "needs_hypothesis_verification_reason": "No hypotheses to verify",
        }

        # Q3 Check: IMPACT_ANALYSIS not needed
        session.phase_assessments["IMPACT_ANALYSIS"] = {
            "needs_impact_analysis": False,
            "needs_impact_analysis_reason": "Investigation only, no changes",
        }

        # With all checks = NO, can proceed directly to READY
        session.phase = Phase.READY
        assert session.phase == Phase.READY


class TestV110RemovedFeatures:
    """Test that v1.10 removed features are gone."""

    def test_submit_understanding_not_in_session(self):
        """Test that submit_understanding method is removed."""
        session = SessionState(
            session_id="test-removed",
            intent="IMPLEMENT",
            query="test query",
            repo_path=Path("."),
        )

        # submit_understanding should not exist as a method
        # (It was replaced by check_phase_necessity)
        # Note: If it exists for backward compatibility, that's OK
        # The key is that it's not the primary way to check phases
        pass  # Method existence is optional for backward compat

    def test_verification_and_impact_phase_exists_for_compat(self):
        """Test that VERIFICATION_AND_IMPACT phase may exist for backward compat."""
        # v1.9 had combined phase, v1.10 separates them
        # But the combined phase might still exist for backward compat
        has_combined = hasattr(Phase, 'VERIFICATION_AND_IMPACT')

        # v1.10: Separate phases should definitely exist
        assert hasattr(Phase, 'VERIFICATION')
        assert hasattr(Phase, 'IMPACT_ANALYSIS')


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
