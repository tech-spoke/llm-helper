"""
Outcome Log - Records session outcomes for improvement cycle.

v3.5: Added for matching with Decision Log.

Design principles:
- Observer only: Records, does not intervene
- Append-only: Never modifies past records
- Human-triggered: /outcome skill invokes this
- LLM-analyzed: LLM analyzes conversation context

Usage:
1. Human recognizes failure ("やり直して", "違う", etc.)
2. Human calls /outcome skill
3. LLM analyzes context and calls record_outcome()
4. This tool appends to logs/outcomes.jsonl
"""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Literal


# Log file location (relative to project root)
LOG_DIR = Path(__file__).parent.parent / "logs"
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
    devrag_used: bool = False
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
            "devrag_used": self.devrag_used,
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
    - devrag usage
    - confidence level
    """
    outcomes = get_recent_outcomes(limit=1000)

    stats = {
        "total": len(outcomes),
        "by_outcome": {"success": 0, "failure": 0, "partial": 0},
        "by_intent": {},
        "by_phase": {},
        "devrag_correlation": {"with_devrag": {"success": 0, "failure": 0}, "without_devrag": {"success": 0, "failure": 0}},
        "confidence_correlation": {"high": {"success": 0, "failure": 0}, "low": {"success": 0, "failure": 0}},
    }

    for o in outcomes:
        outcome = o.get("outcome", "unknown")
        intent = o.get("intent", "unknown")
        phase = o.get("phase_at_outcome", "unknown")
        devrag = o.get("devrag_used", False)
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

        # Devrag correlation
        devrag_key = "with_devrag" if devrag else "without_devrag"
        if outcome in ("success", "failure"):
            stats["devrag_correlation"][devrag_key][outcome] += 1

        # Confidence correlation
        if confidence in ("high", "low") and outcome in ("success", "failure"):
            stats["confidence_correlation"][confidence][outcome] += 1

    return stats
