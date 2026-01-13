"""
Improvement Cycle Logs - Records decisions and outcomes for analysis.

Design principles:
- Observer only: Records, does not intervene
- Append-only: Never modifies past records
- Two-log system: DecisionLog (automatic) + OutcomeLog (human-triggered)

Log files:
- decisions.jsonl: Automatic recording at session start
- outcomes.jsonl: Human-triggered via /outcome skill

Improvement cycle:
1. Session starts -> DecisionLog recorded automatically
2. Session ends (success/failure)
3. Human calls /outcome -> OutcomeLog recorded
4. Analysis matches DecisionLog + OutcomeLog by session_id
"""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Literal


# Log file location (inside .code-intel for project isolation)
LOG_DIR = Path(__file__).parent.parent / ".code-intel" / "logs"
DECISION_LOG_FILE = LOG_DIR / "decisions.jsonl"
OUTCOME_LOG_FILE = LOG_DIR / "outcomes.jsonl"


@dataclass
class OutcomeAnalysis:
    """
    LLM's analysis of why the session failed/succeeded.

    This is filled by the /outcome skill (LLM agent).
    """
    root_cause: str  # What went wrong / what succeeded
    failure_point: str | None = None  # Where in the process it failed
    related_symbols: list[str] = field(default_factory=list)
    related_files: list[str] = field(default_factory=list)
    user_feedback_summary: str = ""  # Summary of user's complaint/praise


@dataclass
class OutcomeLog:
    """
    A single outcome record.

    Links to DecisionLog via session_id.
    """
    # Required fields (no defaults) must come first
    session_id: str
    outcome: Literal["success", "failure", "partial"]
    phase_at_outcome: str  # EXPLORATION, SEMANTIC, VERIFICATION, READY
    intent: str  # IMPLEMENT, MODIFY, INVESTIGATE, QUESTION

    # Optional fields (with defaults)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    semantic_used: bool = False
    confidence_was: str = ""  # "high" or "low"
    analysis: OutcomeAnalysis | None = None
    trigger_message: str = ""  # The message that triggered /outcome

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        result = {
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "outcome": self.outcome,
            "phase_at_outcome": self.phase_at_outcome,
            "intent": self.intent,
            "semantic_used": self.semantic_used,
            "confidence_was": self.confidence_was,
            "trigger_message": self.trigger_message,
        }
        if self.analysis:
            result["analysis"] = asdict(self.analysis)
        return result


def ensure_log_dir() -> None:
    """Ensure log directory exists."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def record_outcome(outcome_log: OutcomeLog) -> dict:
    """
    Record an outcome to the log file.

    Append-only: adds a new line to outcomes.jsonl.

    Returns:
        {"success": True, "log_file": str, "record_id": str}
        or {"success": False, "error": str}
    """
    try:
        ensure_log_dir()

        record = outcome_log.to_dict()
        record_id = f"outcome_{outcome_log.session_id}_{outcome_log.timestamp}"
        record["record_id"] = record_id

        with open(OUTCOME_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        return {
            "success": True,
            "log_file": str(OUTCOME_LOG_FILE),
            "record_id": record_id,
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


def get_outcomes_for_session(session_id: str) -> list[dict]:
    """
    Get all outcomes for a session.

    Used for analysis and debugging.
    """
    if not OUTCOME_LOG_FILE.exists():
        return []

    outcomes = []
    with open(OUTCOME_LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if record.get("session_id") == session_id:
                    outcomes.append(record)
            except json.JSONDecodeError:
                continue

    return outcomes


def get_recent_outcomes(limit: int = 100) -> list[dict]:
    """
    Get recent outcomes for analysis.

    Returns most recent `limit` records.
    """
    if not OUTCOME_LOG_FILE.exists():
        return []

    outcomes = []
    with open(OUTCOME_LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                outcomes.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Return most recent
    return outcomes[-limit:]


def get_failure_stats() -> dict:
    """
    Get statistics about failures for improvement analysis.

    Returns breakdown by:
    - intent type
    - phase at failure
    - semantic search usage
    - confidence level
    """
    outcomes = get_recent_outcomes(limit=1000)

    stats = {
        "total": len(outcomes),
        "by_outcome": {"success": 0, "failure": 0, "partial": 0},
        "by_intent": {},
        "by_phase": {},
        "semantic_correlation": {"with_semantic": {"success": 0, "failure": 0}, "without_semantic": {"success": 0, "failure": 0}},
        "confidence_correlation": {"high": {"success": 0, "failure": 0}, "low": {"success": 0, "failure": 0}},
    }

    for o in outcomes:
        outcome = o.get("outcome", "unknown")
        intent = o.get("intent", "unknown")
        phase = o.get("phase_at_outcome", "unknown")
        semantic = o.get("semantic_used", False)
        confidence = o.get("confidence_was", "unknown")

        # Count by outcome
        if outcome in stats["by_outcome"]:
            stats["by_outcome"][outcome] += 1

        # Count by intent
        if intent not in stats["by_intent"]:
            stats["by_intent"][intent] = {"success": 0, "failure": 0, "partial": 0}
        if outcome in stats["by_intent"][intent]:
            stats["by_intent"][intent][outcome] += 1

        # Count by phase
        if phase not in stats["by_phase"]:
            stats["by_phase"][phase] = {"success": 0, "failure": 0, "partial": 0}
        if outcome in stats["by_phase"][phase]:
            stats["by_phase"][phase][outcome] += 1

        # Semantic search correlation
        semantic_key = "with_semantic" if semantic else "without_semantic"
        if outcome in ("success", "failure"):
            stats["semantic_correlation"][semantic_key][outcome] += 1

        # Confidence correlation
        if confidence in ("high", "low") and outcome in ("success", "failure"):
            stats["confidence_correlation"][confidence][outcome] += 1

    return stats


# ============================================================================
# Decision Log Functions
# ============================================================================

def record_decision(decision_log: dict) -> dict:
    """
    Record a decision log at session start.

    Called automatically when a session starts.
    The decision_log should contain:
    - session_id: str
    - query: str
    - timestamp: str
    - intent: str
    - required_phases: list[str]
    - missing_slots: list[str]
    - risk_level: str
    - tools_planned: list[str]
    - needs_bootstrap: bool
    - bootstrap_reason: str | None

    Returns:
        {"success": True, "log_file": str, "record_id": str}
        or {"success": False, "error": str}
    """
    try:
        ensure_log_dir()

        session_id = decision_log.get("session_id", "unknown")
        timestamp = decision_log.get("timestamp", datetime.now().isoformat())
        record_id = f"decision_{session_id}_{timestamp}"
        decision_log["record_id"] = record_id

        with open(DECISION_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(decision_log, ensure_ascii=False) + "\n")

        return {
            "success": True,
            "log_file": str(DECISION_LOG_FILE),
            "record_id": record_id,
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


def get_decision_for_session(session_id: str) -> dict | None:
    """
    Get the decision log for a specific session.

    Returns the first decision log matching the session_id,
    or None if not found.
    """
    if not DECISION_LOG_FILE.exists():
        return None

    with open(DECISION_LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if record.get("session_id") == session_id:
                    return record
            except json.JSONDecodeError:
                continue

    return None


def get_session_analysis(session_id: str) -> dict:
    """
    Get combined decision + outcome analysis for a session.

    This is the key function for improvement cycle analysis.
    Links DecisionLog and OutcomeLog by session_id.

    Returns:
        {
            "session_id": str,
            "decision": dict | None,
            "outcomes": list[dict],
            "analysis": {
                "had_decision": bool,
                "had_outcome": bool,
                "final_outcome": str | None,
                "tools_planned": list[str],
                "failure_point": str | None,
            }
        }
    """
    decision = get_decision_for_session(session_id)
    outcomes = get_outcomes_for_session(session_id)

    # Determine final outcome (last recorded)
    final_outcome = None
    failure_point = None
    if outcomes:
        last_outcome = outcomes[-1]
        final_outcome = last_outcome.get("outcome")
        if last_outcome.get("analysis"):
            failure_point = last_outcome["analysis"].get("failure_point")

    return {
        "session_id": session_id,
        "decision": decision,
        "outcomes": outcomes,
        "analysis": {
            "had_decision": decision is not None,
            "had_outcome": len(outcomes) > 0,
            "final_outcome": final_outcome,
            "tools_planned": decision.get("tools_planned", []) if decision else [],
            "failure_point": failure_point,
        }
    }


def get_improvement_insights(limit: int = 100) -> dict:
    """
    Analyze recent sessions to find improvement opportunities.

    Looks for patterns in failures:
    - Which intents fail most often?
    - Which tools are associated with failures?
    - Are HIGH risk sessions more likely to fail?

    Returns actionable insights for system improvement.
    """
    # Get recent decisions
    decisions = {}
    if DECISION_LOG_FILE.exists():
        with open(DECISION_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    sid = record.get("session_id")
                    if sid:
                        decisions[sid] = record
                except json.JSONDecodeError:
                    continue

    # Get recent outcomes
    outcomes = get_recent_outcomes(limit=limit)

    # Match and analyze
    insights = {
        "total_sessions_with_outcomes": len(outcomes),
        "sessions_with_decisions": 0,
        "tool_failure_correlation": {},
        "risk_level_correlation": {"HIGH": {"success": 0, "failure": 0},
                                    "MEDIUM": {"success": 0, "failure": 0},
                                    "LOW": {"success": 0, "failure": 0}},
        "common_failure_points": {},
    }

    for outcome in outcomes:
        sid = outcome.get("session_id")
        outcome_result = outcome.get("outcome", "unknown")

        if sid in decisions:
            insights["sessions_with_decisions"] += 1
            decision = decisions[sid]

            # Risk level correlation
            risk = decision.get("risk_level", "UNKNOWN")
            if risk in insights["risk_level_correlation"]:
                if outcome_result in ("success", "failure"):
                    insights["risk_level_correlation"][risk][outcome_result] += 1

            # Tool failure correlation
            if outcome_result == "failure":
                for tool in decision.get("tools_planned", []):
                    if tool not in insights["tool_failure_correlation"]:
                        insights["tool_failure_correlation"][tool] = 0
                    insights["tool_failure_correlation"][tool] += 1

        # Common failure points
        if outcome_result == "failure" and outcome.get("analysis"):
            fp = outcome["analysis"].get("failure_point", "unknown")
            if fp:
                if fp not in insights["common_failure_points"]:
                    insights["common_failure_points"][fp] = 0
                insights["common_failure_points"][fp] += 1

    return insights
