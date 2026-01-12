"""
Tool Router v3.7 - Pure slot-based routing.

v3.7 changes:
- QueryClassifier REMOVED (no more regex patterns)
- Routing based purely on Intent + missing_slots
- Router is "traffic controller", not "decision maker"
- risk_level determines exploration strictness
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any


class IntentType(Enum):
    """High-level intent classification (determined by LLM, not Router)."""
    IMPLEMENT = auto()    # 新規実装・機能追加 (ADD)
    MODIFY = auto()       # 既存コード修正
    DELETE = auto()       # コード削除
    EXPLORE = auto()      # 調査・理解


def get_required_phases(intent: IntentType) -> list[str]:
    """Get required phases for the intent."""
    if intent == IntentType.IMPLEMENT:
        return ["EXPLORATION", "VERIFICATION", "READY"]
    elif intent == IntentType.MODIFY:
        return ["EXPLORATION", "VERIFICATION", "READY"]
    elif intent == IntentType.DELETE:
        return ["EXPLORATION", "VERIFICATION", "READY"]
    elif intent == IntentType.EXPLORE:
        return ["EXPLORATION"]
    return ["EXPLORATION"]


def requires_code_understanding(intent: IntentType) -> bool:
    """Check if intent requires mandatory code understanding."""
    return intent in (IntentType.IMPLEMENT, IntentType.MODIFY, IntentType.DELETE)


# =============================================================================
# v3.7: Slot-to-Tool Mapping (Primary Routing Mechanism)
# =============================================================================

SLOT_TO_TOOLS: dict[str, list[str]] = {
    "target_feature": ["find_definitions", "search_text", "get_symbols"],
    "trigger_condition": ["find_references", "search_text"],
    "observed_issue": ["search_text", "analyze_structure"],
    "desired_action": [],  # 探索不要
}

SLOT_TO_TOOLS_PURPOSE: dict[str, str] = {
    "find_definitions": "Find symbol definitions",
    "find_references": "Find symbol references and usage",
    "search_text": "Search for specific text patterns",
    "get_symbols": "List symbols to identify target feature",
    "analyze_structure": "Analyze code structure",
    "devrag_search": "Semantic search (SEMANTIC phase only)",
}


def select_tools_from_missing_slots(missing_slots: list[str]) -> list[str]:
    """
    v3.7: Select tools based on missing slots only.

    No regex patterns, no category matching.
    Pure slot → tool mapping.
    """
    tools = []
    for slot in missing_slots:
        tools.extend(SLOT_TO_TOOLS.get(slot, []))

    # Remove duplicates while preserving order
    seen = set()
    unique_tools = []
    for tool in tools:
        if tool not in seen:
            seen.add(tool)
            unique_tools.append(tool)

    return unique_tools


def calculate_risk_level(missing_slots: list[str]) -> str:
    """
    v3.7: Calculate risk level from missing slot count.

    No ambiguity detection, no pattern matching.
    Pure count-based logic.
    """
    count = len(missing_slots)
    if count >= 3:
        return "HIGH"
    elif count >= 2:
        return "MEDIUM"
    return "LOW"


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class ExecutionStep:
    """A single step in the execution plan."""
    tool: str
    purpose: str
    params: dict = field(default_factory=dict)
    priority: int = 1


@dataclass
class UnifiedResult:
    """Unified result format from any tool."""
    file_path: str
    symbol_name: str | None
    start_line: int
    end_line: int | None
    content_snippet: str
    source_tool: str
    confidence: float = 1.0


@dataclass
class FallbackDecision:
    """Fallback decision result (kept for backward compatibility)."""
    should_fallback: bool
    reason: str
    threshold: int
    code_results_count: int


@dataclass
class DecisionLog:
    """
    v3.7: Simplified decision log.

    Removed: categories, pattern_match_count, ambiguous, confidence
    Added: Pure slot-based fields
    """
    query: str
    timestamp: str
    intent: str
    required_phases: list[str]
    missing_slots: list[str]
    risk_level: str
    tools_planned: list[str]
    needs_bootstrap: bool
    bootstrap_reason: str | None
    session_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "query": self.query,
            "timestamp": self.timestamp,
            "intent": self.intent,
            "required_phases": self.required_phases,
            "missing_slots": self.missing_slots,
            "risk_level": self.risk_level,
            "tools_planned": self.tools_planned,
            "needs_bootstrap": self.needs_bootstrap,
            "bootstrap_reason": self.bootstrap_reason,
        }


@dataclass
class ExecutionPlan:
    """Complete execution plan for a query."""
    steps: list[ExecutionStep]
    reasoning: str
    needs_bootstrap: bool = False
    decision_log: DecisionLog | None = None
    intent: IntentType = IntentType.EXPLORE
    risk_level: str = "LOW"
    missing_slots: list[str] = field(default_factory=list)


@dataclass
class RoutingDecision:
    """
    v3.7: Pure slot-based routing decision.

    Router's job:
    - What tools to use (from missing_slots)
    - How strict to be (from risk_level)
    - NOT what the user "really wants" (that's Intent from LLM)
    """
    initial_phase: str  # Always "EXPLORATION"
    initial_tools: list[str]
    priority_slots: list[str]
    risk_level: str
    guidance: dict


# =============================================================================
# Router (v3.7 - No QueryClassifier)
# =============================================================================

class Router:
    """
    v3.7: Pure slot-based router.

    Changes from v3.6:
    - QueryClassifier REMOVED
    - No regex pattern matching
    - Intent comes from LLM (not inferred)
    - Tools selected from missing_slots only
    """

    def __init__(self):
        self._query_count = 0

    def create_plan(
        self,
        query_frame: "QueryFrame",
        intent: IntentType | str,
        context: dict | None = None,
    ) -> ExecutionPlan:
        """
        v3.7: Create execution plan from QueryFrame + Intent.

        Args:
            query_frame: Structured query from QueryDecomposer
            intent: Intent type from LLM (IMPLEMENT/MODIFY/DELETE/EXPLORE)
            context: Additional context (path, etc.)
        """
        context = context or {}
        path = context.get("path", ".")

        # Resolve intent
        if isinstance(intent, str):
            try:
                intent_type = IntentType[intent.upper()]
            except KeyError:
                intent_type = IntentType.EXPLORE
        else:
            intent_type = intent

        required_phases = get_required_phases(intent_type)
        req_code_understanding = requires_code_understanding(intent_type)

        # v3.7: Pure slot-based tool selection
        missing_slots = query_frame.get_missing_slots()
        risk_level = calculate_risk_level(missing_slots)
        tools = select_tools_from_missing_slots(missing_slots)

        # Add analyze_structure for EXPLORE intent
        if intent_type == IntentType.EXPLORE and "analyze_structure" not in tools:
            tools.append("analyze_structure")

        # Create execution steps
        steps = []
        for i, tool in enumerate(tools):
            purpose = SLOT_TO_TOOLS_PURPOSE.get(tool, f"Execute {tool}")
            steps.append(ExecutionStep(
                tool=tool,
                purpose=purpose,
                priority=len(tools) - i,
                params=dict(context),
            ))

        # Bootstrap decision
        is_first = self._query_count == 0
        self._query_count += 1

        needs_bootstrap = req_code_understanding or is_first
        bootstrap_reason = None
        if needs_bootstrap:
            if req_code_understanding:
                bootstrap_reason = f"intent_{intent_type.name.lower()}"
            elif is_first:
                bootstrap_reason = "first_query"

        # Generate reasoning
        reasoning = f"Intent: {intent_type.name}. "
        if missing_slots:
            reasoning += f"Missing slots: {missing_slots}. "
        reasoning += f"Risk level: {risk_level}. "
        reasoning += f"Tools: {[s.tool for s in steps]}. "

        # Create decision log
        decision_log = DecisionLog(
            query=query_frame.raw_query,
            timestamp=datetime.now().isoformat(),
            intent=intent_type.name,
            required_phases=required_phases,
            missing_slots=missing_slots,
            risk_level=risk_level,
            tools_planned=[s.tool for s in steps],
            needs_bootstrap=needs_bootstrap,
            bootstrap_reason=bootstrap_reason,
        )

        return ExecutionPlan(
            steps=steps,
            reasoning=reasoning,
            needs_bootstrap=needs_bootstrap,
            decision_log=decision_log,
            intent=intent_type,
            risk_level=risk_level,
            missing_slots=missing_slots,
        )

    def create_routing_decision(
        self,
        query_frame: "QueryFrame",
        intent: str,
    ) -> RoutingDecision:
        """
        v3.7: Create routing decision from QueryFrame.

        Pure "traffic controller" logic.
        """
        from tools.query_frame import generate_investigation_guidance

        missing_slots = query_frame.get_missing_slots()
        risk_level = calculate_risk_level(missing_slots)
        guidance = generate_investigation_guidance(missing_slots)

        # Prioritize slots based on intent
        if intent in ("IMPLEMENT", "MODIFY", "DELETE"):
            priority_order = ["target_feature", "observed_issue", "trigger_condition", "desired_action"]
        else:
            priority_order = ["target_feature", "trigger_condition", "observed_issue", "desired_action"]

        priority_slots = [s for s in priority_order if s in missing_slots]

        return RoutingDecision(
            initial_phase="EXPLORATION",
            initial_tools=select_tools_from_missing_slots(missing_slots),
            priority_slots=priority_slots,
            risk_level=risk_level,
            guidance=guidance,
        )


# =============================================================================
# Backward Compatibility (Deprecated, will be removed in v3.8)
# =============================================================================

class QuestionCategory(Enum):
    """DEPRECATED: Kept for backward compatibility only."""
    A_SYNTAX = auto()
    B_REFERENCE = auto()
    C_SEMANTIC = auto()
    D_IMPACT = auto()
