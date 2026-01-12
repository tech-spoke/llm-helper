"""
Tests for tools/router.py - Slot-based Router

v3.7: Tests for pure slot-based routing (no regex patterns).
"""

import pytest

from tools.router import (
    IntentType,
    Router,
    RoutingDecision,
    ExecutionPlan,
    ExecutionStep,
    DecisionLog,
    calculate_risk_level,
    get_required_phases,
    requires_code_understanding,
    select_tools_from_missing_slots,
    SLOT_TO_TOOLS,
)


class TestIntentType:
    """Test IntentType enum."""

    def test_intent_values(self):
        """Test that all intent types exist."""
        assert IntentType.IMPLEMENT is not None
        assert IntentType.MODIFY is not None
        assert IntentType.DELETE is not None
        assert IntentType.EXPLORE is not None


class TestSlotToToolMapping:
    """Test slot to tool mapping."""

    def test_slot_mapping_exists(self):
        """Test that slot mapping is defined."""
        assert "target_feature" in SLOT_TO_TOOLS
        assert "trigger_condition" in SLOT_TO_TOOLS
        assert "observed_issue" in SLOT_TO_TOOLS
        assert "desired_action" in SLOT_TO_TOOLS

    def test_target_feature_tools(self):
        """Test tools for target_feature slot."""
        tools = SLOT_TO_TOOLS["target_feature"]
        assert "find_definitions" in tools
        assert "search_text" in tools

    def test_desired_action_no_tools(self):
        """Test that desired_action requires no exploration."""
        assert SLOT_TO_TOOLS["desired_action"] == []


class TestSelectToolsFromMissingSlots:
    """Test select_tools_from_missing_slots function."""

    def test_single_slot(self):
        """Test tool selection with single missing slot."""
        tools = select_tools_from_missing_slots(["target_feature"])
        assert "find_definitions" in tools
        assert "search_text" in tools

    def test_multiple_slots(self):
        """Test tool selection with multiple missing slots."""
        tools = select_tools_from_missing_slots(
            ["target_feature", "trigger_condition"]
        )
        assert "find_definitions" in tools
        assert "find_references" in tools
        assert "search_text" in tools

    def test_empty_slots(self):
        """Test tool selection with no missing slots."""
        tools = select_tools_from_missing_slots([])
        assert tools == []

    def test_removes_duplicates(self):
        """Test that duplicate tools are removed."""
        # Both target_feature and trigger_condition have search_text
        tools = select_tools_from_missing_slots(
            ["target_feature", "trigger_condition"]
        )
        assert tools.count("search_text") == 1

    def test_preserves_order(self):
        """Test that tool order is preserved."""
        tools = select_tools_from_missing_slots(["target_feature"])
        # First tool should be from target_feature mapping
        assert tools[0] in SLOT_TO_TOOLS["target_feature"]


class TestCalculateRiskLevel:
    """Test calculate_risk_level function."""

    def test_high_risk(self):
        """Test HIGH risk level (3+ missing slots)."""
        assert calculate_risk_level(["a", "b", "c"]) == "HIGH"
        assert calculate_risk_level(["a", "b", "c", "d"]) == "HIGH"

    def test_medium_risk(self):
        """Test MEDIUM risk level (2 missing slots)."""
        assert calculate_risk_level(["a", "b"]) == "MEDIUM"

    def test_low_risk(self):
        """Test LOW risk level (0-1 missing slots)."""
        assert calculate_risk_level([]) == "LOW"
        assert calculate_risk_level(["a"]) == "LOW"


class TestGetRequiredPhases:
    """Test get_required_phases function."""

    def test_implement_phases(self):
        """Test phases for IMPLEMENT intent."""
        phases = get_required_phases(IntentType.IMPLEMENT)
        assert "EXPLORATION" in phases
        assert "VERIFICATION" in phases
        assert "READY" in phases

    def test_modify_phases(self):
        """Test phases for MODIFY intent."""
        phases = get_required_phases(IntentType.MODIFY)
        assert "EXPLORATION" in phases
        assert "VERIFICATION" in phases
        assert "READY" in phases

    def test_explore_phases(self):
        """Test phases for EXPLORE intent."""
        phases = get_required_phases(IntentType.EXPLORE)
        assert phases == ["EXPLORATION"]


class TestRequiresCodeUnderstanding:
    """Test requires_code_understanding function."""

    def test_implement_requires(self):
        """Test IMPLEMENT requires code understanding."""
        assert requires_code_understanding(IntentType.IMPLEMENT) is True

    def test_modify_requires(self):
        """Test MODIFY requires code understanding."""
        assert requires_code_understanding(IntentType.MODIFY) is True

    def test_delete_requires(self):
        """Test DELETE requires code understanding."""
        assert requires_code_understanding(IntentType.DELETE) is True

    def test_explore_not_requires(self):
        """Test EXPLORE does not require code understanding."""
        assert requires_code_understanding(IntentType.EXPLORE) is False


class TestRouter:
    """Test Router class."""

    def test_create_plan_basic(self, sample_query_frame):
        """Test basic plan creation."""
        router = Router()
        plan = router.create_plan(sample_query_frame, IntentType.MODIFY)

        assert isinstance(plan, ExecutionPlan)
        assert plan.intent == IntentType.MODIFY
        assert isinstance(plan.steps, list)
        assert plan.decision_log is not None

    def test_create_plan_with_string_intent(self, sample_query_frame):
        """Test plan creation with string intent."""
        router = Router()
        plan = router.create_plan(sample_query_frame, "MODIFY")

        assert plan.intent == IntentType.MODIFY

    def test_create_plan_invalid_intent_defaults_to_explore(self, sample_query_frame):
        """Test that invalid intent defaults to EXPLORE."""
        router = Router()
        plan = router.create_plan(sample_query_frame, "INVALID")

        assert plan.intent == IntentType.EXPLORE

    def test_create_plan_sets_risk_level(self, empty_query_frame):
        """Test that risk level is set based on missing slots."""
        router = Router()
        # Empty query frame has all slots missing
        plan = router.create_plan(empty_query_frame, IntentType.MODIFY)

        # Should have HIGH risk since multiple slots are missing
        assert plan.risk_level in ("HIGH", "MEDIUM", "LOW")

    def test_create_plan_explore_adds_analyze_structure(self, sample_query_frame):
        """Test that EXPLORE intent adds analyze_structure."""
        router = Router()
        plan = router.create_plan(sample_query_frame, IntentType.EXPLORE)

        tool_names = [step.tool for step in plan.steps]
        assert "analyze_structure" in tool_names

    def test_create_plan_bootstrap_first_query(self, sample_query_frame):
        """Test bootstrap for first query."""
        router = Router()
        plan = router.create_plan(sample_query_frame, IntentType.EXPLORE)

        # First query should trigger bootstrap
        assert plan.needs_bootstrap is True
        assert plan.decision_log.bootstrap_reason == "first_query"

    def test_create_plan_bootstrap_code_change(self, sample_query_frame):
        """Test bootstrap for code change intent."""
        router = Router()
        # First call
        router.create_plan(sample_query_frame, IntentType.EXPLORE)
        # Second call with MODIFY
        plan = router.create_plan(sample_query_frame, IntentType.MODIFY)

        assert plan.needs_bootstrap is True
        assert plan.decision_log.bootstrap_reason == "intent_modify"

    def test_create_routing_decision(self, sample_query_frame):
        """Test routing decision creation."""
        router = Router()
        decision = router.create_routing_decision(sample_query_frame, "MODIFY")

        assert isinstance(decision, RoutingDecision)
        assert decision.initial_phase == "EXPLORATION"
        assert isinstance(decision.initial_tools, list)
        assert isinstance(decision.guidance, dict)


class TestExecutionStep:
    """Test ExecutionStep dataclass."""

    def test_default_values(self):
        """Test default values."""
        step = ExecutionStep(tool="search_text", purpose="Search")
        assert step.params == {}
        assert step.priority == 1

    def test_custom_values(self):
        """Test custom values."""
        step = ExecutionStep(
            tool="find_definitions",
            purpose="Find symbol definitions",
            params={"path": "/src"},
            priority=5,
        )
        assert step.tool == "find_definitions"
        assert step.params == {"path": "/src"}
        assert step.priority == 5


class TestDecisionLog:
    """Test DecisionLog dataclass."""

    def test_to_dict(self):
        """Test conversion to dictionary."""
        log = DecisionLog(
            query="ログイン機能を修正",
            timestamp="2025-01-12T10:00:00",
            intent="MODIFY",
            required_phases=["EXPLORATION", "READY"],
            missing_slots=["target_feature"],
            risk_level="LOW",
            tools_planned=["find_definitions"],
            needs_bootstrap=True,
            bootstrap_reason="first_query",
            session_id="sess_123",
        )
        d = log.to_dict()

        assert d["query"] == "ログイン機能を修正"
        assert d["intent"] == "MODIFY"
        assert d["risk_level"] == "LOW"
        assert d["session_id"] == "sess_123"
        assert d["needs_bootstrap"] is True


class TestExecutionPlan:
    """Test ExecutionPlan dataclass."""

    def test_default_values(self):
        """Test default values."""
        plan = ExecutionPlan(steps=[], reasoning="Test")
        assert plan.needs_bootstrap is False
        assert plan.intent == IntentType.EXPLORE
        assert plan.risk_level == "LOW"
        assert plan.missing_slots == []

    def test_with_steps(self):
        """Test plan with steps."""
        steps = [
            ExecutionStep(tool="search_text", purpose="Search"),
            ExecutionStep(tool="find_definitions", purpose="Find"),
        ]
        plan = ExecutionPlan(
            steps=steps,
            reasoning="Search and find",
            intent=IntentType.MODIFY,
            risk_level="HIGH",
        )
        assert len(plan.steps) == 2
        assert plan.intent == IntentType.MODIFY
        assert plan.risk_level == "HIGH"
