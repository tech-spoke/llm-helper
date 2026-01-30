"""
Test v1.11 features:
1. submit_phase dispatch (unified API)
2. Q1/Q2/Q3 gates via submit_phase
3. Task orchestration (READY sub-steps 12-14)
4. POST_IMPL_VERIFY + failure counts
5. VERIFY_INTERVENTION + safety valves
6. QUALITY_REVIEW forced completion
7. Compaction resilience (get_session_status)
8. Phase enum expansion
9. End-to-end flows
"""

import pytest
from pathlib import Path
from tools.session import (
    SessionState,
    Phase,
    TaskModel,
    get_phase_response,
    PHASE_STEP_MAP,
    EXPECTED_PAYLOADS,
    PHASE_INSTRUCTIONS,
)


# =============================================================================
# Phase Enum
# =============================================================================

class TestPhaseEnum:
    """Test v1.11 Phase enum expansion."""

    def test_new_phases_exist(self):
        """All v1.11 new phases are defined."""
        assert Phase.BRANCH_INTERVENTION
        assert Phase.DOCUMENT_RESEARCH
        assert Phase.QUERY_FRAME
        assert Phase.Q1
        assert Phase.Q2
        assert Phase.Q3
        assert Phase.POST_IMPL_VERIFY
        assert Phase.VERIFY_INTERVENTION
        assert Phase.MERGE

    def test_existing_phases_preserved(self):
        """Existing phases are unchanged."""
        assert Phase.EXPLORATION
        assert Phase.SEMANTIC
        assert Phase.VERIFICATION
        assert Phase.IMPACT_ANALYSIS
        assert Phase.READY
        assert Phase.PRE_COMMIT
        assert Phase.QUALITY_REVIEW

    def test_phase_order(self):
        """Phases are in correct order."""
        phases = list(Phase)
        names = [p.name for p in phases]
        assert names.index("BRANCH_INTERVENTION") < names.index("EXPLORATION")
        assert names.index("EXPLORATION") < names.index("Q1")
        assert names.index("Q1") < names.index("SEMANTIC")
        assert names.index("SEMANTIC") < names.index("Q2")
        assert names.index("Q2") < names.index("VERIFICATION")
        assert names.index("VERIFICATION") < names.index("Q3")
        assert names.index("Q3") < names.index("IMPACT_ANALYSIS")
        assert names.index("IMPACT_ANALYSIS") < names.index("READY")
        assert names.index("READY") < names.index("POST_IMPL_VERIFY")
        assert names.index("POST_IMPL_VERIFY") < names.index("VERIFY_INTERVENTION")
        assert names.index("VERIFY_INTERVENTION") < names.index("PRE_COMMIT")
        assert names.index("PRE_COMMIT") < names.index("QUALITY_REVIEW")
        assert names.index("QUALITY_REVIEW") < names.index("MERGE")


# =============================================================================
# TaskModel
# =============================================================================

class TestTaskModel:
    """Test v1.11 TaskModel dataclass."""

    def test_create_task(self):
        t = TaskModel(id="t1", description="Fix CSS")
        assert t.id == "t1"
        assert t.description == "Fix CSS"
        assert t.status == "pending"
        assert t.failure_count == 0
        assert t.revert_reason is None

    def test_to_dict_minimal(self):
        t = TaskModel(id="t1", description="Fix CSS")
        d = t.to_dict()
        assert d == {"id": "t1", "description": "Fix CSS", "status": "pending"}
        assert "failure_count" not in d
        assert "revert_reason" not in d

    def test_to_dict_with_failure(self):
        t = TaskModel(id="t1", description="Fix CSS", failure_count=2, revert_reason="test failed")
        d = t.to_dict()
        assert d["failure_count"] == 2
        assert d["revert_reason"] == "test failed"


# =============================================================================
# get_phase_response
# =============================================================================

class TestPhaseResponse:
    """Test v1.11 self-contained response generation."""

    def test_response_has_required_fields(self):
        r = get_phase_response("EXPLORATION")
        assert "phase" in r
        assert "step" in r
        assert "instruction" in r
        assert "expected_payload" in r
        assert "call" in r
        assert r["call"] == "submit_phase"

    def test_exploration_response(self):
        r = get_phase_response("EXPLORATION")
        assert r["phase"] == "EXPLORATION"
        assert r["step"] == 5
        assert "explored_files" in r["expected_payload"]

    def test_q1_response(self):
        r = get_phase_response("Q1")
        assert r["phase"] == "Q1"
        assert r["step"] == 6
        assert "needs_more_information" in r["expected_payload"]

    def test_ready_plan_response(self):
        r = get_phase_response("READY", extra={"ready_substep": "READY_PLAN"})
        assert r["phase"] == "READY"
        assert "tasks" in r["expected_payload"]

    def test_ready_impl_response(self):
        r = get_phase_response("READY", extra={"ready_substep": "READY_IMPL"})
        assert "task_id" in r["expected_payload"]

    def test_merge_response(self):
        r = get_phase_response("MERGE")
        assert r["phase"] == "MERGE"
        assert r["step"] == 19
        assert r["expected_payload"] == {}


# =============================================================================
# Task Orchestration (register_tasks / complete_task / check_all_tasks_complete)
# =============================================================================

class TestTaskOrchestration:
    """READY phase task management (Steps 12-14)."""

    def _make_ready_session(self) -> SessionState:
        s = SessionState(
            session_id="test-task",
            intent="IMPLEMENT",
            query="test",
            repo_path=str(Path(".")),
        )
        s.phase = Phase.READY
        return s

    def test_register_tasks(self):
        s = self._make_ready_session()
        result = s.register_tasks([
            {"id": "t1", "description": "Task 1"},
            {"id": "t2", "description": "Task 2"},
        ])
        assert result.get("success") or result.get("tasks_registered") == 2
        assert len(s.tasks) == 2
        assert s.ready_substep == "implement"

    def test_register_tasks_empty_error(self):
        s = self._make_ready_session()
        result = s.register_tasks([])
        assert "error" in result
        assert result["error"] == "empty_tasks"

    def test_register_tasks_duplicate_id_error(self):
        s = self._make_ready_session()
        result = s.register_tasks([
            {"id": "t1", "description": "A"},
            {"id": "t1", "description": "B"},
        ])
        assert "error" in result
        assert result["error"] == "duplicate_task_ids"

    def test_register_tasks_no_pending_error(self):
        s = self._make_ready_session()
        result = s.register_tasks([
            {"id": "t1", "description": "A", "status": "completed"},
        ])
        assert "error" in result
        assert result["error"] == "no_pending_tasks"

    def test_complete_task_in_order(self):
        s = self._make_ready_session()
        s.register_tasks([
            {"id": "t1", "description": "Task 1"},
            {"id": "t2", "description": "Task 2"},
        ])
        result = s.complete_task("t1", "Done")
        assert "success" in result or "next_task" in result
        assert s.tasks[0].status == "completed"

    def test_reject_wrong_order(self):
        s = self._make_ready_session()
        s.register_tasks([
            {"id": "t1", "description": "Task 1"},
            {"id": "t2", "description": "Task 2"},
        ])
        result = s.complete_task("t2", "Done")
        assert "error" in result
        assert result["error"] == "wrong_order"

    def test_all_tasks_complete_signal(self):
        s = self._make_ready_session()
        s.register_tasks([
            {"id": "t1", "description": "Task 1"},
        ])
        result = s.complete_task("t1", "Done")
        assert result.get("all_complete") is True
        assert s.ready_substep == "complete"

    def test_block_incomplete_implementation(self):
        s = self._make_ready_session()
        s.register_tasks([
            {"id": "t1", "description": "Task 1"},
            {"id": "t2", "description": "Task 2"},
        ])
        # Only complete t1
        s.complete_task("t1", "Done")
        result = s.check_all_tasks_complete()
        assert "error" in result
        assert result["error"] == "incomplete_tasks"

    def test_allow_complete_implementation(self):
        s = self._make_ready_session()
        s.register_tasks([
            {"id": "t1", "description": "Task 1"},
        ])
        s.complete_task("t1", "Done")
        result = s.check_all_tasks_complete()
        assert result.get("success") is True

    def test_idempotent_task_reregistration(self):
        """On revert, re-registering full list works."""
        s = self._make_ready_session()
        s.register_tasks([
            {"id": "t1", "description": "Task 1", "status": "completed"},
            {"id": "fix_1", "description": "Fix task", "status": "pending", "failure_count": 1},
        ])
        assert len(s.tasks) == 2
        assert s.tasks[0].status == "completed"
        assert s.tasks[1].status == "pending"
        assert s.tasks[1].failure_count == 1

    def test_phase_mismatch_register(self):
        s = SessionState(
            session_id="test",
            intent="IMPLEMENT",
            query="test",
            repo_path=str(Path(".")),
        )
        s.phase = Phase.EXPLORATION
        result = s.register_tasks([{"id": "t1", "description": "X"}])
        assert "error" in result
        assert result["error"] == "phase_mismatch"


# =============================================================================
# Q1/Q2/Q3 Gates (Phase assessments in SessionState)
# =============================================================================

class TestQGates:
    """Q1/Q2/Q3 assessment storage in SessionState."""

    def test_store_q1_assessment(self):
        s = SessionState(
            session_id="test-q",
            intent="IMPLEMENT",
            query="test",
            repo_path=str(Path(".")),
        )
        s.phase_assessments["Q1"] = {"needs_more_information": True, "reason": "Need more info"}
        assert s.phase_assessments["Q1"]["needs_more_information"] is True

    def test_store_q2_assessment(self):
        s = SessionState(
            session_id="test-q",
            intent="IMPLEMENT",
            query="test",
            repo_path=str(Path(".")),
        )
        s.phase_assessments["Q2"] = {"has_unverified_hypotheses": False, "reason": "All verified"}
        assert s.phase_assessments["Q2"]["has_unverified_hypotheses"] is False

    def test_store_q3_assessment(self):
        s = SessionState(
            session_id="test-q",
            intent="IMPLEMENT",
            query="test",
            repo_path=str(Path(".")),
        )
        s.phase_assessments["Q3"] = {"needs_impact_analysis": True, "reason": "Large scope"}
        assert s.phase_assessments["Q3"]["needs_impact_analysis"] is True


# =============================================================================
# get_status (v1.11: includes expected_payload)
# =============================================================================

class TestCompactionResilience:
    """Test that get_session_status returns self-contained recovery data."""

    def test_status_contains_expected_payload(self):
        s = SessionState(
            session_id="test-status",
            intent="IMPLEMENT",
            query="test",
            repo_path=str(Path(".")),
        )
        s.phase = Phase.EXPLORATION
        status = s.get_status()
        assert "expected_payload" in status
        assert "instruction" in status
        assert "phase" in status
        assert status["phase"] == "EXPLORATION"

    def test_status_contains_step(self):
        s = SessionState(
            session_id="test-step",
            intent="IMPLEMENT",
            query="test",
            repo_path=str(Path(".")),
        )
        s.phase = Phase.Q1
        status = s.get_status()
        assert status["step"] == 6

    def test_status_ready_substep(self):
        s = SessionState(
            session_id="test-ready",
            intent="IMPLEMENT",
            query="test",
            repo_path=str(Path(".")),
        )
        s.phase = Phase.READY
        s.ready_substep = "plan"
        status = s.get_status()
        assert status["step"] == 12
        assert "tasks" in status["expected_payload"]

    def test_status_with_task_progress(self):
        s = SessionState(
            session_id="test-progress",
            intent="IMPLEMENT",
            query="test",
            repo_path=str(Path(".")),
        )
        s.phase = Phase.READY
        s.register_tasks([
            {"id": "t1", "description": "Task 1"},
            {"id": "t2", "description": "Task 2"},
        ])
        s.complete_task("t1", "Done")
        status = s.get_status()
        assert status["task_progress"] is not None
        assert status["task_progress"]["completed"] == 1
        assert status["task_progress"]["pending"] == 1


# =============================================================================
# Safety Valves
# =============================================================================

class TestLoopSafetyValves:
    """Test server-enforced loop limits."""

    def test_quality_revert_count_increments(self):
        s = SessionState(
            session_id="test-quality",
            intent="IMPLEMENT",
            query="test",
            repo_path=str(Path(".")),
        )
        assert s.quality_revert_count == 0
        s.quality_revert_count += 1
        assert s.quality_revert_count == 1

    def test_intervention_count_increments(self):
        s = SessionState(
            session_id="test-intervention",
            intent="IMPLEMENT",
            query="test",
            repo_path=str(Path(".")),
        )
        assert s.intervention_count == 0
        s.intervention_count += 1
        assert s.intervention_count == 1

    def test_task_failure_count(self):
        t = TaskModel(id="t1", description="Fix")
        assert t.failure_count == 0
        t.failure_count += 1
        t.failure_count += 1
        t.failure_count += 1
        assert t.failure_count >= 3

    def test_session_flags(self):
        s = SessionState(
            session_id="test-flags",
            intent="IMPLEMENT",
            query="test",
            repo_path=str(Path(".")),
        )
        assert s.no_verify is False
        assert s.no_quality is False
        assert s.fast_mode is False
        assert s.quick_mode is False
        assert s.no_doc is False
        assert s.no_intervention is False


# =============================================================================
# Phase Step Map
# =============================================================================

class TestPhaseStepMap:
    """Test phase-to-step mapping."""

    def test_all_phases_mapped(self):
        assert PHASE_STEP_MAP["BRANCH_INTERVENTION"] == 2
        assert PHASE_STEP_MAP["DOCUMENT_RESEARCH"] == 3
        assert PHASE_STEP_MAP["QUERY_FRAME"] == 4
        assert PHASE_STEP_MAP["EXPLORATION"] == 5
        assert PHASE_STEP_MAP["Q1"] == 6
        assert PHASE_STEP_MAP["SEMANTIC"] == 7
        assert PHASE_STEP_MAP["Q2"] == 8
        assert PHASE_STEP_MAP["VERIFICATION"] == 9
        assert PHASE_STEP_MAP["Q3"] == 10
        assert PHASE_STEP_MAP["IMPACT_ANALYSIS"] == 11
        assert PHASE_STEP_MAP["READY"] == 12
        assert PHASE_STEP_MAP["POST_IMPL_VERIFY"] == 15
        assert PHASE_STEP_MAP["VERIFY_INTERVENTION"] == 16
        assert PHASE_STEP_MAP["PRE_COMMIT"] == 17
        assert PHASE_STEP_MAP["QUALITY_REVIEW"] == 18
        assert PHASE_STEP_MAP["MERGE"] == 19


# =============================================================================
# Expected Payloads
# =============================================================================

class TestExpectedPayloads:
    """Test expected payload definitions exist for all phases."""

    def test_all_phases_have_payloads(self):
        required_keys = [
            "BRANCH_INTERVENTION", "DOCUMENT_RESEARCH", "QUERY_FRAME",
            "EXPLORATION", "Q1", "SEMANTIC", "Q2", "VERIFICATION", "Q3",
            "IMPACT_ANALYSIS", "READY_PLAN", "READY_IMPL", "READY_COMPLETE",
            "POST_IMPL_VERIFY", "VERIFY_INTERVENTION", "PRE_COMMIT",
            "QUALITY_REVIEW", "MERGE",
        ]
        for key in required_keys:
            assert key in EXPECTED_PAYLOADS, f"Missing expected payload for {key}"

    def test_all_phases_have_instructions(self):
        required_keys = [
            "BRANCH_INTERVENTION", "DOCUMENT_RESEARCH", "QUERY_FRAME",
            "EXPLORATION", "Q1", "SEMANTIC", "Q2", "VERIFICATION", "Q3",
            "IMPACT_ANALYSIS", "READY_PLAN", "READY_IMPL", "READY_COMPLETE",
            "POST_IMPL_VERIFY", "VERIFY_INTERVENTION", "PRE_COMMIT",
            "QUALITY_REVIEW", "MERGE",
        ]
        for key in required_keys:
            assert key in PHASE_INSTRUCTIONS, f"Missing instruction for {key}"


# =============================================================================
# POST_IMPL_VERIFY (submit_post_impl_verify logic in SessionState)
# =============================================================================

class TestPostImplVerifyLogic:
    """Test failure_count tracking at task level."""

    def _make_session_with_tasks(self) -> SessionState:
        s = SessionState(
            session_id="test-verify",
            intent="IMPLEMENT",
            query="test",
            repo_path=str(Path(".")),
        )
        s.phase = Phase.READY
        s.register_tasks([
            {"id": "t1", "description": "Task 1"},
            {"id": "t2", "description": "Task 2"},
        ])
        s.complete_task("t1", "done")
        s.complete_task("t2", "done")
        return s

    def test_task_failure_count_increment(self):
        s = self._make_session_with_tasks()
        # Simulate failure for task t1
        for t in s.tasks:
            if t.id == "t1":
                t.failure_count += 1
                break
        assert s.tasks[0].failure_count == 1

    def test_high_failure_detection(self):
        s = self._make_session_with_tasks()
        s.tasks[0].failure_count = 3
        high_failure = any(t.failure_count >= 3 for t in s.tasks)
        assert high_failure is True

    def test_no_high_failure(self):
        s = self._make_session_with_tasks()
        s.tasks[0].failure_count = 2
        high_failure = any(t.failure_count >= 3 for t in s.tasks)
        assert high_failure is False
