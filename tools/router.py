"""
Tool Router - Cursor-style intelligent tool selection and execution.

This module implements the "thinking order" that determines:
- WHEN to use each tool
- WHY to use it
- In what ORDER to execute them
- HOW to integrate results

Key improvements (v3):
- SessionBootstrap: repo_pack as pre-processing cache with semantic usage
- Composite categories: All matching category tools are combined
- Enhanced patterns: Both Japanese and English support
- Intent confidence: Detect ambiguous queries and add devrag
- Dynamic fallback: Adaptive thresholds based on repo size and categories

v3.6 changes:
- QueryFrame-based routing: スロット欠損→ツール選択
- Router is now "traffic controller", not "decision maker"
- risk_level determines exploration strictness
- Slot-to-tool mapping for investigation guidance
"""

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any


class QuestionCategory(Enum):
    """Question classification categories."""
    A_SYNTAX = auto()      # 文字・構文ベースで答えられる
    B_REFERENCE = auto()   # シンボル関係・参照探索が必要
    C_SEMANTIC = auto()    # 意味・役割・設計意図に関する
    D_IMPACT = auto()      # 影響範囲・変更分析


class IntentType(Enum):
    """
    v3.1: High-level intent classification.

    Used to determine Router behavior before tool selection.
    """
    IMPLEMENT = auto()    # 新規実装・機能追加
    MODIFY = auto()       # 既存コード修正
    INVESTIGATE = auto()  # 調査・理解
    QUESTION = auto()     # 一般質問（ツール不要）


def get_required_phases(intent: IntentType) -> list[str]:
    """Get required phases for the intent."""
    if intent == IntentType.IMPLEMENT:
        return ["understand_structure", "find_patterns", "implement"]
    elif intent == IntentType.MODIFY:
        return ["find_target", "analyze_impact", "modify"]
    elif intent == IntentType.INVESTIGATE:
        return ["search", "analyze"]
    else:
        return []


def requires_code_understanding(intent: IntentType) -> bool:
    """Check if intent requires mandatory code understanding."""
    return intent in (IntentType.IMPLEMENT, IntentType.MODIFY)


@dataclass
class IntentAnalysis:
    """Analysis of query intent clarity."""
    categories: list[QuestionCategory]
    confidence: str  # "high", "medium", "low"
    pattern_match_count: int
    ambiguous: bool
    recommend_devrag: bool
    reasoning: str


@dataclass
class DecisionLog:
    """
    v3.1: Structured log of Router decisions.
    v3.5: Added session_id for Outcome Log matching.
    v3.5: Added failed_checks for improvement analysis.
    v3.6: Added QueryFrame-based routing info.

    Records WHY decisions were made without changing any logic.
    Used for observability and future optimization.
    """
    # Query info
    query: str
    timestamp: str

    # v3.1: Intent classification (IMPLEMENT/MODIFY/INVESTIGATE/QUESTION)
    intent: str
    requires_code_understanding: bool
    required_phases: list[str]

    # Classification decision
    categories: list[str]
    confidence: str
    pattern_match_count: int
    ambiguous: bool

    # Tool selection decision
    tools_planned: list[str]
    force_devrag: bool
    force_devrag_reason: str | None

    # Bootstrap decision
    needs_bootstrap: bool
    bootstrap_reason: str | None
    repo_size_category: str | None

    # v3.5: Session ID for matching with Outcome Log (optional, filled later)
    session_id: str | None = None

    # Fallback decision (filled after execution)
    fallback_triggered: bool = False
    fallback_reason: str | None = None
    fallback_threshold: int | None = None
    code_results_count: int | None = None

    # v3.5: Failed checks for improvement analysis
    # e.g., ["definition_not_found", "reference_empty", "pattern_unknown"]
    failed_checks: list[str] = field(default_factory=list)

    # v3.6: QueryFrame-based routing
    query_frame: dict | None = None  # QueryFrame.to_dict()
    missing_slots: list[str] = field(default_factory=list)
    risk_level: str = "LOW"  # HIGH, MEDIUM, LOW
    slot_based_tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "session_id": self.session_id,  # v3.5
            "query": self.query,
            "timestamp": self.timestamp,
            "intent": {
                "type": self.intent,
                "requires_code_understanding": self.requires_code_understanding,
                "required_phases": self.required_phases,
            },
            "classification": {
                "categories": self.categories,
                "confidence": self.confidence,
                "pattern_match_count": self.pattern_match_count,
                "ambiguous": self.ambiguous,
            },
            "tool_selection": {
                "tools_planned": self.tools_planned,
                "force_devrag": self.force_devrag,
                "force_devrag_reason": self.force_devrag_reason,
            },
            "bootstrap": {
                "needs_bootstrap": self.needs_bootstrap,
                "reason": self.bootstrap_reason,
                "repo_size_category": self.repo_size_category,
            },
            "fallback": {
                "triggered": self.fallback_triggered,
                "reason": self.fallback_reason,
                "threshold": self.fallback_threshold,
                "code_results_count": self.code_results_count,
            },
            "failed_checks": self.failed_checks,  # v3.5
            # v3.6
            "query_frame": self.query_frame,
            "missing_slots": self.missing_slots,
            "risk_level": self.risk_level,
            "slot_based_tools": self.slot_based_tools,
        }


@dataclass
class ExecutionStep:
    """A single step in the execution plan."""
    tool: str
    purpose: str
    params: dict = field(default_factory=dict)
    priority: int = 1  # Higher = execute first


@dataclass
class ExecutionPlan:
    """Complete execution plan for a query."""
    categories: list[QuestionCategory]
    steps: list[ExecutionStep]
    reasoning: str
    needs_bootstrap: bool = False
    intent_confidence: str = "high"  # v3: Track query clarity
    force_devrag: bool = False       # v3: Force devrag for ambiguous queries
    decision_log: DecisionLog | None = None  # v3.1: Structured decision log
    intent: IntentType = IntentType.QUESTION  # v3.1: High-level intent
    requires_code_understanding: bool = False  # v3.1: Mandatory tool usage


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


class SessionBootstrap:
    """
    Manages repo_pack as a session-level pre-processing cache.

    v3 improvements:
    - Provides repo summary for devrag query enhancement
    - Tracks repo size for dynamic fallback thresholds

    v3.1 improvements:
    - Cache invalidation based on file count and mtime
    """

    def __init__(self):
        self._cache: dict[str, Any] = {}
        self._initialized_paths: set[str] = set()
        self._repo_metadata: dict[str, dict] = {}  # v3: Store metadata
        self._cache_snapshot: dict[str, dict] = {}  # v3.1: file count/mtime at cache time

    def needs_bootstrap(
        self,
        path: str,
        categories: list[QuestionCategory],
        is_first_query: bool = False,
    ) -> bool:
        """Determine if repo_pack should be executed."""
        # v3.1: Check if cache is stale
        if path in self._initialized_paths:
            if self.is_cache_stale(path):
                self.invalidate(path)
            else:
                return False

        if is_first_query:
            return True

        if QuestionCategory.C_SEMANTIC in categories:
            return True
        if QuestionCategory.D_IMPACT in categories:
            return True

        return False

    def get_context(self, path: str) -> dict | None:
        """Get cached repo context."""
        return self._cache.get(path)

    def set_context(self, path: str, context: dict) -> None:
        """Cache repo context and extract metadata."""
        self._cache[path] = context
        self._initialized_paths.add(path)

        # Extract metadata for dynamic decisions
        self._repo_metadata[path] = {
            "file_count": context.get("file_count", 0),
            "summary": self._extract_summary(context),
            "languages": context.get("languages", []),
            "size_category": self._categorize_size(context.get("file_count", 0)),
        }

        # v3.1: Take snapshot for staleness check
        self._cache_snapshot[path] = self._take_snapshot(path)

    def _extract_summary(self, context: dict) -> str:
        """Extract a brief summary from repo_pack output."""
        content = context.get("content", "")
        if not content:
            return ""

        # Try to extract first few lines or structure overview
        lines = content.split("\n")[:20]
        summary_lines = []
        for line in lines:
            if line.strip() and not line.startswith("#"):
                summary_lines.append(line.strip())
            if len(summary_lines) >= 5:
                break

        return " | ".join(summary_lines)

    def _categorize_size(self, file_count: int) -> str:
        """Categorize repo size for threshold decisions."""
        if file_count <= 10:
            return "small"
        elif file_count <= 50:
            return "medium"
        elif file_count <= 200:
            return "large"
        else:
            return "xlarge"

    def get_metadata(self, path: str) -> dict:
        """Get repo metadata for dynamic decisions."""
        return self._repo_metadata.get(path, {
            "file_count": 0,
            "summary": "",
            "languages": [],
            "size_category": "unknown",
        })

    def get_enhanced_query(self, path: str, query: str) -> str:
        """
        Enhance query with repo context for devrag.

        v3: repo_pack is now "read" not just "held".
        """
        metadata = self.get_metadata(path)
        summary = metadata.get("summary", "")
        languages = metadata.get("languages", [])

        if not summary and not languages:
            return query

        context_parts = []
        if languages:
            context_parts.append(f"Languages: {', '.join(languages[:3])}")
        if summary:
            context_parts.append(f"Context: {summary[:200]}")

        if context_parts:
            return f"[{' | '.join(context_parts)}] {query}"
        return query

    def invalidate(self, path: str) -> None:
        """Invalidate cache for a path."""
        self._cache.pop(path, None)
        self._initialized_paths.discard(path)
        self._repo_metadata.pop(path, None)
        self._cache_snapshot.pop(path, None)  # v3.1

    def _take_snapshot(self, path: str) -> dict:
        """
        v3.1: Take a snapshot of repo state for staleness detection.

        Captures file count and latest mtime in the directory.
        """
        try:
            repo_path = Path(path).resolve()
            if not repo_path.is_dir():
                return {"file_count": 0, "latest_mtime": 0}

            file_count = 0
            latest_mtime = 0.0

            # Walk through directory (excluding common ignored dirs)
            ignored_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache"}

            for root, dirs, files in os.walk(repo_path):
                # Skip ignored directories
                dirs[:] = [d for d in dirs if d not in ignored_dirs]

                for f in files:
                    file_count += 1
                    try:
                        fpath = Path(root) / f
                        mtime = fpath.stat().st_mtime
                        if mtime > latest_mtime:
                            latest_mtime = mtime
                    except (OSError, IOError):
                        pass

            return {
                "file_count": file_count,
                "latest_mtime": latest_mtime,
            }
        except Exception:
            return {"file_count": 0, "latest_mtime": 0}

    def is_cache_stale(self, path: str) -> bool:
        """
        v3.1: Check if cached repo_pack is stale.

        Returns True if:
        - File count changed
        - Latest mtime is newer than cached snapshot
        """
        if path not in self._cache_snapshot:
            return True

        old_snapshot = self._cache_snapshot[path]
        new_snapshot = self._take_snapshot(path)

        # Check file count change
        if new_snapshot["file_count"] != old_snapshot["file_count"]:
            return True

        # Check if any file was modified
        if new_snapshot["latest_mtime"] > old_snapshot["latest_mtime"]:
            return True

        return False

    def is_initialized(self, path: str) -> bool:
        """Check if path has been bootstrapped."""
        return path in self._initialized_paths

    def get_cache_status(self, path: str) -> dict:
        """
        v3.1: Get cache status for logging.

        Returns info about cache state including staleness.
        """
        if path not in self._initialized_paths:
            return {
                "cached": False,
                "stale": None,
                "snapshot": None,
            }

        current = self._take_snapshot(path)
        cached = self._cache_snapshot.get(path, {})

        return {
            "cached": True,
            "stale": self.is_cache_stale(path),
            "snapshot": {
                "cached_file_count": cached.get("file_count", 0),
                "current_file_count": current.get("file_count", 0),
                "cached_mtime": cached.get("latest_mtime", 0),
                "current_mtime": current.get("latest_mtime", 0),
            },
        }


class QueryClassifier:
    """
    Classifies user queries into categories.

    v3 improvements:
    - Returns pattern match count for confidence scoring
    - Detects ambiguous queries
    """

    PATTERNS = {
        QuestionCategory.A_SYNTAX: [
            # Japanese
            r"どこで定義",
            r"定義場所",
            r"どのファイル",
            r"構文",
            r"インポート",
            r"呼び出し方",
            r"シグネチャ",
            r"型",
            r"引数",
            r"戻り値",
            # English
            r"where\s+(is|are)\s+.*(defined|declared)",
            r"definition\s+of",
            r"which\s+file",
            r"what\s+file",
            r"syntax",
            r"import",
            r"how\s+(to\s+)?(call|use|invoke)",
            r"signature",
            r"type\s+of",
            r"argument",
            r"parameter",
            r"return\s+(type|value)",
            r"find\s+.*(declaration|definition)",
            r"locate\s+.*(class|function|method)",
        ],
        QuestionCategory.B_REFERENCE: [
            # Japanese
            r"どこから呼ばれ",
            r"呼び出し元",
            r"参照",
            r"使われている",
            r"依存",
            r"継承",
            r"実装",
            r"誰が使",
            r"どこで使",
            # English
            r"where\s+(is|are)\s+.*(called|used|invoked)",
            r"who\s+(calls|uses|invokes)",
            r"caller",
            r"reference",
            r"used\s+by",
            r"usage",
            r"depend",
            r"inherit",
            r"implement",
            r"extend",
            r"find\s+.*(usage|reference|caller)",
            r"what\s+(calls|uses|depends)",
            r"list\s+(all\s+)?(usage|reference|caller)",
        ],
        QuestionCategory.C_SEMANTIC: [
            # Japanese
            r"なぜ",
            r"目的",
            r"役割",
            r"責務",
            r"設計",
            r"意図",
            r"理由",
            r"どういう意味",
            r"説明",
            r"概要",
            r"何をして",
            r"どう動",
            r"仕組み",
            r"アーキテクチャ",
            r"構成",
            r"全体像",
            # English
            r"why\s+(does|is|do|are|was|were|should)",
            r"purpose",
            r"role\s+of",
            r"responsibilit",
            r"design",
            r"intent",
            r"reason",
            r"what\s+(does|do)\s+.+\s+(mean|do)",
            r"explain",
            r"describe",
            r"overview",
            r"how\s+does\s+.+\s+work",
            r"what\s+is\s+(the\s+)?(purpose|goal|point)",
            r"architecture",
            r"structure\s+of",
            r"understand",
            r"what\s+is\s+this",
            r"tell\s+me\s+about",
        ],
        QuestionCategory.D_IMPACT: [
            # Japanese
            r"影響",
            r"変更.*(どこ|何)",
            r"壊れる",
            r"副作用",
            r"リファクタ",
            r"修正.*範囲",
            r"安全.*(変更|修正)",
            r"変えたら",
            r"消したら",
            r"削除.*(影響|大丈夫)",
            # English
            r"impact",
            r"affect",
            r"what\s+(will\s+)?(break|change|happen)",
            r"side\s+effect",
            r"refactor",
            r"scope\s+of\s+(change|modification)",
            r"safe\s+to\s+(change|modify|delete|remove)",
            r"if\s+i\s+(change|modify|delete|remove)",
            r"consequence",
            r"ripple\s+effect",
            r"what\s+depends\s+on",
        ],
    }

    def analyze_intent(self, query: str) -> IntentAnalysis:
        """
        Analyze query intent with confidence scoring.

        v3: Returns detailed analysis including ambiguity detection.
        """
        categories = []
        total_matches = 0
        query_lower = query.lower()

        for category, patterns in self.PATTERNS.items():
            category_matches = 0
            for pattern in patterns:
                if re.search(pattern, query_lower, re.IGNORECASE):
                    category_matches += 1
                    if category not in categories:
                        categories.append(category)

            total_matches += category_matches

        # Default to A_SYNTAX if no category matched
        if not categories:
            categories = [QuestionCategory.A_SYNTAX]

        # Determine confidence
        if total_matches >= 3:
            confidence = "high"
        elif total_matches >= 2:
            confidence = "medium"
        else:
            confidence = "low"

        # Detect ambiguity
        ambiguous = (
            total_matches < 2 and
            len(categories) == 1 and
            categories[0] == QuestionCategory.A_SYNTAX
        )

        # Recommend devrag for ambiguous queries
        recommend_devrag = ambiguous or confidence == "low"

        # Generate reasoning
        if ambiguous:
            reasoning = "Query is ambiguous (few pattern matches). Recommending devrag for context."
        elif confidence == "low":
            reasoning = "Low confidence classification. Consider adding devrag for better coverage."
        elif len(categories) > 1:
            reasoning = f"Multiple intents detected ({len(categories)} categories)."
        else:
            reasoning = f"Clear intent: {categories[0].name}."

        return IntentAnalysis(
            categories=categories,
            confidence=confidence,
            pattern_match_count=total_matches,
            ambiguous=ambiguous,
            recommend_devrag=recommend_devrag,
            reasoning=reasoning,
        )

    def classify(self, query: str) -> list[QuestionCategory]:
        """Classify a query into one or more categories."""
        return self.analyze_intent(query).categories


class ToolSelector:
    """Selects tools based on question categories."""

    CATEGORY_TOOLS = {
        QuestionCategory.A_SYNTAX: [
            ExecutionStep(
                tool="search_text",
                purpose="Search for text patterns in code",
                priority=2,
            ),
            ExecutionStep(
                tool="analyze_structure",
                purpose="Analyze code structure",
                priority=1,
            ),
        ],
        QuestionCategory.B_REFERENCE: [
            ExecutionStep(
                tool="find_definitions",
                purpose="Find where symbol is defined",
                priority=3,
            ),
            ExecutionStep(
                tool="find_references",
                purpose="Find where symbol is used",
                priority=2,
            ),
        ],
        QuestionCategory.C_SEMANTIC: [
            ExecutionStep(
                tool="analyze_structure",
                purpose="Get structural context",
                priority=2,
            ),
            ExecutionStep(
                tool="devrag_search",
                purpose="Search for semantic meaning and design intent",
                priority=1,
            ),
        ],
        QuestionCategory.D_IMPACT: [
            ExecutionStep(
                tool="find_definitions",
                purpose="Identify the target symbol",
                priority=3,
            ),
            ExecutionStep(
                tool="find_references",
                purpose="Find all usages that might be affected",
                priority=2,
            ),
            ExecutionStep(
                tool="devrag_search",
                purpose="Understand dependencies and relationships",
                priority=1,
            ),
        ],
    }

    def select_tools(
        self,
        categories: list[QuestionCategory],
        force_devrag: bool = False,
    ) -> list[ExecutionStep]:
        """
        Select tools based on question categories.

        v3: Can force devrag for ambiguous queries.
        """
        tool_map: dict[str, ExecutionStep] = {}

        for category in categories:
            if category in self.CATEGORY_TOOLS:
                for step in self.CATEGORY_TOOLS[category]:
                    existing = tool_map.get(step.tool)
                    if existing is None or step.priority > existing.priority:
                        tool_map[step.tool] = ExecutionStep(
                            tool=step.tool,
                            purpose=step.purpose,
                            priority=step.priority,
                            params=dict(step.params),
                        )

        # v3: Force devrag for ambiguous queries
        if force_devrag and "devrag_search" not in tool_map:
            tool_map["devrag_search"] = ExecutionStep(
                tool="devrag_search",
                purpose="Added for ambiguous query - provide semantic context",
                priority=0,  # Run last
            )

        steps = list(tool_map.values())
        steps.sort(key=lambda s: s.priority, reverse=True)

        return steps


class ResultIntegrator:
    """Integrates results from multiple tools into unified format."""

    def normalize(self, tool_name: str, raw_result: dict) -> list[UnifiedResult]:
        """Normalize tool output to unified format."""
        results = []

        if "error" in raw_result:
            return results

        if tool_name == "search_text":
            for match in raw_result.get("matches", []):
                results.append(UnifiedResult(
                    file_path=match.get("file", ""),
                    symbol_name=None,
                    start_line=match.get("line_number", 0),
                    end_line=None,
                    content_snippet=match.get("line_content", ""),
                    source_tool=tool_name,
                ))

        elif tool_name in ("find_definitions", "get_symbols"):
            items = raw_result.get("definitions", raw_result.get("symbols", []))
            for item in items:
                results.append(UnifiedResult(
                    file_path=item.get("file", ""),
                    symbol_name=item.get("name", ""),
                    start_line=item.get("line", 0),
                    end_line=None,
                    content_snippet=item.get("signature", ""),
                    source_tool=tool_name,
                ))

        elif tool_name == "find_references":
            for ref in raw_result.get("references", []):
                results.append(UnifiedResult(
                    file_path=ref.get("file", ""),
                    symbol_name=None,
                    start_line=ref.get("line", 0),
                    end_line=None,
                    content_snippet=ref.get("content", ""),
                    source_tool=tool_name,
                ))

        elif tool_name == "analyze_structure":
            for func in raw_result.get("functions", []):
                results.append(UnifiedResult(
                    file_path=raw_result.get("file_path", ""),
                    symbol_name=func.get("name", ""),
                    start_line=func.get("start_line", 0),
                    end_line=func.get("end_line"),
                    content_snippet=f"function {func.get('name', '')}",
                    source_tool=tool_name,
                ))
            for cls in raw_result.get("classes", []):
                results.append(UnifiedResult(
                    file_path=raw_result.get("file_path", ""),
                    symbol_name=cls.get("name", ""),
                    start_line=cls.get("start_line", 0),
                    end_line=cls.get("end_line"),
                    content_snippet=f"class {cls.get('name', '')}",
                    source_tool=tool_name,
                ))

        elif tool_name == "devrag_search":
            for item in raw_result.get("results", []):
                results.append(UnifiedResult(
                    file_path=item.get("file", item.get("path", "")),
                    symbol_name=None,
                    start_line=item.get("line", 0),
                    end_line=None,
                    content_snippet=item.get("content", item.get("text", "")),
                    source_tool=tool_name,
                    confidence=0.8,
                ))

        elif tool_name == "repo_pack":
            if raw_result.get("content"):
                results.append(UnifiedResult(
                    file_path="<repository>",
                    symbol_name=None,
                    start_line=0,
                    end_line=None,
                    content_snippet=f"Repository packed: {raw_result.get('file_count', '?')} files",
                    source_tool=tool_name,
                    confidence=1.0,
                ))

        return results

    def merge(self, all_results: list[UnifiedResult]) -> list[UnifiedResult]:
        """Merge results, preferring code-based results."""
        grouped: dict[tuple, list[UnifiedResult]] = {}

        for result in all_results:
            key = (result.file_path, result.symbol_name, result.start_line)
            if key not in grouped:
                grouped[key] = []
            grouped[key].append(result)

        merged = []
        code_tools = {"search_text", "find_definitions", "find_references",
                      "analyze_structure", "get_symbols", "repo_pack"}

        for key, results in grouped.items():
            results.sort(
                key=lambda r: (r.source_tool in code_tools, r.confidence),
                reverse=True
            )
            merged.append(results[0])

        return merged


@dataclass
class FallbackDecision:
    """v3.1: Structured fallback decision result."""
    should_fallback: bool
    reason: str
    threshold: int
    code_results_count: int


class FallbackDecider:
    """
    v3: Dynamic fallback decision based on multiple factors.

    Replaces static threshold with adaptive logic.
    """

    def should_fallback(
        self,
        results: list[UnifiedResult],
        categories: list[QuestionCategory],
        repo_size_category: str,
        intent_confidence: str,
    ) -> tuple[bool, str]:
        """
        Determine if devrag fallback should trigger.

        Returns:
            (should_fallback, reason)
        """
        decision = self.decide(results, categories, repo_size_category, intent_confidence)
        return decision.should_fallback, decision.reason

    def decide(
        self,
        results: list[UnifiedResult],
        categories: list[QuestionCategory],
        repo_size_category: str,
        intent_confidence: str,
    ) -> FallbackDecision:
        """
        v3.1: Determine fallback with full decision details.

        Returns FallbackDecision with all details for logging.
        """
        code_results = [
            r for r in results
            if r.source_tool != "devrag_search"
        ]
        code_count = len(code_results)

        # Dynamic threshold based on repo size
        thresholds = {
            "small": 1,
            "medium": 2,
            "large": 3,
            "xlarge": 5,
            "unknown": 2,
        }
        threshold = thresholds.get(repo_size_category, 2)

        # Lower threshold for semantic queries
        if QuestionCategory.C_SEMANTIC in categories:
            threshold = max(1, threshold - 1)

        # Lower threshold for low confidence queries
        if intent_confidence == "low":
            threshold = max(1, threshold - 1)

        # Already has devrag results
        if any(r.source_tool == "devrag_search" for r in results):
            return FallbackDecision(
                should_fallback=False,
                reason="devrag already executed",
                threshold=threshold,
                code_results_count=code_count,
            )

        # No results at all
        if not results:
            return FallbackDecision(
                should_fallback=True,
                reason="no results from any tool",
                threshold=threshold,
                code_results_count=0,
            )

        if code_count < threshold:
            return FallbackDecision(
                should_fallback=True,
                reason=f"insufficient results ({code_count} < {threshold})",
                threshold=threshold,
                code_results_count=code_count,
            )

        # Fragmentation check
        files = set(r.file_path for r in code_results)
        if len(files) > 5 and code_count < 10:
            return FallbackDecision(
                should_fallback=True,
                reason="results too fragmented",
                threshold=threshold,
                code_results_count=code_count,
            )

        # Low confidence with only A_SYNTAX
        if (intent_confidence == "low" and
            len(categories) == 1 and
            categories[0] == QuestionCategory.A_SYNTAX):
            return FallbackDecision(
                should_fallback=True,
                reason="ambiguous query with only syntax matches",
                threshold=threshold,
                code_results_count=code_count,
            )

        return FallbackDecision(
            should_fallback=False,
            reason="sufficient results",
            threshold=threshold,
            code_results_count=code_count,
        )


class Router:
    """
    Main router that orchestrates query classification, tool selection,
    execution, and result integration.

    v3 changes:
    - Intent confidence scoring
    - Dynamic fallback thresholds
    - repo_pack semantic usage

    v3.1 changes:
    - Intent classification (IMPLEMENT/MODIFY/INVESTIGATE/QUESTION)
    - Mandatory code understanding for IMPLEMENT/MODIFY
    """

    def __init__(self):
        self.classifier = QueryClassifier()
        self.selector = ToolSelector()
        self.integrator = ResultIntegrator()
        self.bootstrap = SessionBootstrap()
        self.fallback_decider = FallbackDecider()
        self._query_count = 0

    def create_plan(
        self,
        query: str,
        context: dict | None = None,
        intent: IntentType | str | None = None,  # v3.2: Accept intent from caller (LLM)
    ) -> ExecutionPlan:
        """
        Create an execution plan for a query.

        Args:
            query: User's question or request
            context: Additional context (path, symbol, etc.)
            intent: Intent type from caller. If None, defaults to INVESTIGATE.
                    Can be IntentType enum or string ("IMPLEMENT", "MODIFY", etc.)
        """
        context = context or {}
        path = context.get("path", ".")

        # Step 0: Resolve intent (v3.2: from caller, not internal classification)
        if intent is None:
            intent_type = IntentType.INVESTIGATE  # Safe default
        elif isinstance(intent, str):
            try:
                intent_type = IntentType[intent.upper()]
            except KeyError:
                intent_type = IntentType.INVESTIGATE
        else:
            intent_type = intent

        req_code_understanding = requires_code_understanding(intent_type)
        required_phases = get_required_phases(intent_type)

        # Step 1: Analyze query categories (v3: with confidence)
        query_analysis = self.classifier.analyze_intent(query)
        categories = query_analysis.categories

        # Step 2: Check if bootstrap needed
        is_first = self._query_count == 0
        self._query_count += 1

        # v3.2: IMPLEMENT/MODIFY always needs bootstrap
        if req_code_understanding:
            needs_bootstrap = True
        else:
            needs_bootstrap = self.bootstrap.needs_bootstrap(path, categories, is_first)

        # Step 3: Determine if devrag should be forced
        # v3.2: IMPLEMENT/MODIFY forces devrag for comprehensive understanding
        force_devrag = query_analysis.recommend_devrag or req_code_understanding

        # Step 4: Select tools
        # v3.2: IMPLEMENT/MODIFY ensures comprehensive tool selection
        if req_code_understanding:
            # Force all relevant categories for full understanding
            enhanced_categories = list(set(categories + [
                QuestionCategory.A_SYNTAX,
                QuestionCategory.B_REFERENCE,
            ]))
            if intent_type == IntentType.MODIFY:
                enhanced_categories.append(QuestionCategory.D_IMPACT)
            steps = self.selector.select_tools(enhanced_categories, force_devrag=True)
        else:
            steps = self.selector.select_tools(categories, force_devrag=force_devrag)

        # Step 5: Add context to steps
        for step in steps:
            if "symbol" in context and step.tool in (
                "find_definitions", "find_references", "search_text"
            ):
                step.params["symbol"] = context["symbol"]
            if "path" in context:
                step.params["path"] = context["path"]
            if "file_path" in context and step.tool == "analyze_structure":
                step.params["file_path"] = context["file_path"]

        # Step 6: Generate reasoning
        cat_names = [c.name for c in categories]
        tool_names = [s.tool for s in steps]

        # v3.2: Intent-aware reasoning
        reasoning = f"Intent: {intent_type.name}. "
        if req_code_understanding:
            reasoning += "Code understanding REQUIRED. "
            reasoning += f"Phases: {', '.join(required_phases)}. "
        reasoning += f"Categories: {', '.join(cat_names)}. "
        reasoning += f"Tools: {', '.join(tool_names)}. "

        if needs_bootstrap:
            reasoning += "Repository bootstrap will run first. "
        if force_devrag and not req_code_understanding:
            reasoning += "devrag forced due to ambiguous query. "
        if query_analysis.ambiguous:
            reasoning += f"({query_analysis.reasoning}) "

        # v3.2: Generate bootstrap reason
        bootstrap_reason = None
        if needs_bootstrap:
            if req_code_understanding:
                bootstrap_reason = f"intent_{intent_type.name.lower()}"
            elif is_first:
                bootstrap_reason = "first_query"
            elif QuestionCategory.C_SEMANTIC in categories:
                bootstrap_reason = "semantic_query"
            elif QuestionCategory.D_IMPACT in categories:
                bootstrap_reason = "impact_query"

        # v3.2: Get repo size if available
        repo_metadata = self.bootstrap.get_metadata(path)
        repo_size_category = repo_metadata.get("size_category")

        # v3.2: Create decision log with intent
        decision_log = DecisionLog(
            query=query,
            timestamp=datetime.now().isoformat(),
            intent=intent_type.name,
            requires_code_understanding=req_code_understanding,
            required_phases=required_phases,
            categories=cat_names,
            confidence=query_analysis.confidence,
            pattern_match_count=query_analysis.pattern_match_count,
            ambiguous=query_analysis.ambiguous,
            tools_planned=tool_names,
            force_devrag=force_devrag,
            force_devrag_reason=query_analysis.reasoning if force_devrag else None,
            needs_bootstrap=needs_bootstrap,
            bootstrap_reason=bootstrap_reason,
            repo_size_category=repo_size_category,
        )

        return ExecutionPlan(
            categories=categories,
            steps=steps,
            reasoning=reasoning,
            needs_bootstrap=needs_bootstrap,
            intent_confidence=query_analysis.confidence,
            force_devrag=force_devrag,
            decision_log=decision_log,
            intent=intent_type,  # v3.2
            requires_code_understanding=req_code_understanding,  # v3.2
        )

    def should_fallback_to_devrag(
        self,
        results: list[UnifiedResult],
        categories: list[QuestionCategory] | None = None,
        path: str = ".",
        intent_confidence: str = "high",
    ) -> tuple[bool, str]:
        """
        v3: Dynamic fallback decision.

        Returns:
            (should_fallback, reason)
        """
        categories = categories or [QuestionCategory.A_SYNTAX]
        repo_metadata = self.bootstrap.get_metadata(path)
        size_category = repo_metadata.get("size_category", "unknown")

        return self.fallback_decider.should_fallback(
            results=results,
            categories=categories,
            repo_size_category=size_category,
            intent_confidence=intent_confidence,
        )

    def get_fallback_decision(
        self,
        results: list[UnifiedResult],
        categories: list[QuestionCategory] | None = None,
        path: str = ".",
        intent_confidence: str = "high",
    ) -> FallbackDecision:
        """
        v3.1: Get fallback decision with full details for logging.

        Returns FallbackDecision with threshold and count info.
        """
        categories = categories or [QuestionCategory.A_SYNTAX]
        repo_metadata = self.bootstrap.get_metadata(path)
        size_category = repo_metadata.get("size_category", "unknown")

        return self.fallback_decider.decide(
            results=results,
            categories=categories,
            repo_size_category=size_category,
            intent_confidence=intent_confidence,
        )

    def format_plan_output(self, plan: ExecutionPlan) -> str:
        """Format execution plan for display."""
        lines = [
            "=" * 50,
            "EXECUTION PLAN",
            "=" * 50,
            "",
            f"Categories: {', '.join(c.name for c in plan.categories)}",
            f"Intent confidence: {plan.intent_confidence}",
            f"Bootstrap needed: {plan.needs_bootstrap}",
            f"Force devrag: {plan.force_devrag}",
            "",
            "Steps:",
        ]

        for i, step in enumerate(plan.steps, 1):
            lines.append(f"  {i}. {step.tool} (priority: {step.priority})")
            lines.append(f"     Purpose: {step.purpose}")
            if step.params:
                lines.append(f"     Params: {step.params}")

        lines.extend([
            "",
            f"Reasoning: {plan.reasoning}",
            "=" * 50,
        ])

        return "\n".join(lines)

    def format_results_output(self, results: list[UnifiedResult]) -> str:
        """Format unified results for display."""
        if not results:
            return "No results found."

        lines = [
            "=" * 50,
            f"RESULTS ({len(results)} items)",
            "=" * 50,
        ]

        for r in results:
            lines.append("")
            lines.append(f"File: {r.file_path}:{r.start_line}")
            if r.symbol_name:
                lines.append(f"Symbol: {r.symbol_name}")
            lines.append(f"Source: {r.source_tool} (confidence: {r.confidence})")
            if r.content_snippet:
                lines.append(f"Content: {r.content_snippet[:100]}...")

        return "\n".join(lines)

    # =========================================================================
    # v3.6: QueryFrame-based routing
    # =========================================================================

    def create_plan_from_frame(
        self,
        query_frame: "QueryFrame",
        intent: IntentType | str,
        context: dict | None = None,
    ) -> ExecutionPlan:
        """
        v3.6: Create execution plan from QueryFrame.

        This is the new "traffic controller" approach:
        - Router doesn't decide "what the user wants"
        - Router decides "what tools to use based on missing slots"

        Args:
            query_frame: Structured query from QueryDecomposer
            intent: Intent type from LLM
            context: Additional context (path, symbol, etc.)
        """
        from tools.query_frame import (
            assess_risk_level,
            generate_investigation_guidance,
        )

        context = context or {}
        path = context.get("path", ".")

        # Resolve intent
        if isinstance(intent, str):
            try:
                intent_type = IntentType[intent.upper()]
            except KeyError:
                intent_type = IntentType.INVESTIGATE
        else:
            intent_type = intent

        req_code_understanding = requires_code_understanding(intent_type)
        required_phases = get_required_phases(intent_type)

        # v3.6: Get missing slots and risk level
        missing_slots = query_frame.get_missing_slots()
        risk_level = assess_risk_level(query_frame, intent_type.name)

        # v3.6: Generate investigation guidance based on missing slots
        guidance = generate_investigation_guidance(missing_slots)
        slot_based_tools = guidance.get("recommended_tools", [])

        # Also run category-based analysis for backward compatibility
        query_analysis = self.classifier.analyze_intent(query_frame.raw_query)
        categories = query_analysis.categories

        # v3.6: Combine slot-based tools with category-based tools
        # Slot-based tools take priority for IMPLEMENT/MODIFY
        if req_code_understanding and slot_based_tools:
            # Slot-based tools first, then category tools
            combined_tools = list(dict.fromkeys(
                slot_based_tools + [s.tool for s in self.selector.select_tools(categories)]
            ))
        else:
            combined_tools = [s.tool for s in self.selector.select_tools(
                categories, force_devrag=query_analysis.recommend_devrag
            )]

        # Create execution steps from combined tools
        steps = []
        for i, tool in enumerate(combined_tools):
            purpose = SLOT_TO_TOOLS_PURPOSE.get(tool, f"Execute {tool}")
            steps.append(ExecutionStep(
                tool=tool,
                purpose=purpose,
                priority=len(combined_tools) - i,
                params=dict(context),
            ))

        # Bootstrap decision
        is_first = self._query_count == 0
        self._query_count += 1

        if req_code_understanding:
            needs_bootstrap = True
            bootstrap_reason = f"intent_{intent_type.name.lower()}"
        else:
            needs_bootstrap = self.bootstrap.needs_bootstrap(path, categories, is_first)
            bootstrap_reason = None
            if needs_bootstrap:
                if is_first:
                    bootstrap_reason = "first_query"
                elif QuestionCategory.C_SEMANTIC in categories:
                    bootstrap_reason = "semantic_query"

        repo_metadata = self.bootstrap.get_metadata(path)
        repo_size_category = repo_metadata.get("size_category")

        # Generate reasoning
        cat_names = [c.name for c in categories]
        reasoning = f"Intent: {intent_type.name}. "
        if missing_slots:
            reasoning += f"Missing slots: {missing_slots}. "
        reasoning += f"Risk level: {risk_level}. "
        if req_code_understanding:
            reasoning += "Code understanding REQUIRED. "
        reasoning += f"Tools: {combined_tools}. "

        # Create decision log with v3.6 fields
        decision_log = DecisionLog(
            query=query_frame.raw_query,
            timestamp=datetime.now().isoformat(),
            intent=intent_type.name,
            requires_code_understanding=req_code_understanding,
            required_phases=required_phases,
            categories=cat_names,
            confidence=query_analysis.confidence,
            pattern_match_count=query_analysis.pattern_match_count,
            ambiguous=query_analysis.ambiguous,
            tools_planned=combined_tools,
            force_devrag=False,  # v3.6: DEVRAG is controlled by phase, not router
            force_devrag_reason=None,
            needs_bootstrap=needs_bootstrap,
            bootstrap_reason=bootstrap_reason,
            repo_size_category=repo_size_category,
            # v3.6 fields
            query_frame=query_frame.to_dict(),
            missing_slots=missing_slots,
            risk_level=risk_level,
            slot_based_tools=slot_based_tools,
        )

        return ExecutionPlan(
            categories=categories,
            steps=steps,
            reasoning=reasoning,
            needs_bootstrap=needs_bootstrap,
            intent_confidence=query_analysis.confidence,
            force_devrag=False,
            decision_log=decision_log,
            intent=intent_type,
            requires_code_understanding=req_code_understanding,
        )


# =============================================================================
# v3.6: Slot-to-Tool Mapping
# =============================================================================

SLOT_TO_TOOLS: dict[str, list[str]] = {
    "target_feature": ["query", "get_symbols", "analyze_structure"],
    "trigger_condition": ["search_text", "find_definitions"],
    "observed_issue": ["search_text", "query"],
    "desired_action": ["find_references", "analyze_structure"],
}

SLOT_TO_TOOLS_PURPOSE: dict[str, str] = {
    "query": "Get overall codebase understanding",
    "get_symbols": "List symbols to identify target feature",
    "analyze_structure": "Analyze code structure",
    "search_text": "Search for specific text patterns",
    "find_definitions": "Find symbol definitions",
    "find_references": "Find symbol references and impact scope",
    "devrag_search": "Semantic search for context",
}


def select_tools_from_missing_slots(missing_slots: list[str], max_tools: int = 4) -> list[str]:
    """
    v3.6: Select tools based on missing slots.

    Args:
        missing_slots: List of missing slot names
        max_tools: Maximum number of tools to return

    Returns:
        List of tool names, prioritized by slot order
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

    return unique_tools[:max_tools]


# =============================================================================
# v3.6: Routing Decision (QueryFrame-based)
# =============================================================================

@dataclass
class RoutingDecision:
    """
    v3.6: Routing decision from QueryFrame.

    Router's job is to decide:
    - What tools to use (based on missing slots)
    - How strict to be (based on risk level)
    - NOT what the user "really wants"
    """
    initial_phase: str  # Always "EXPLORATION"
    initial_tools: list[str]
    priority_slots: list[str]
    risk_level: str  # HIGH, MEDIUM, LOW
    guidance: dict  # From generate_investigation_guidance


def create_routing_decision(
    query_frame: "QueryFrame",
    intent: str,
) -> RoutingDecision:
    """
    v3.6: Create routing decision from QueryFrame.

    This is the "traffic controller" logic.
    """
    from tools.query_frame import (
        assess_risk_level,
        generate_investigation_guidance,
    )

    missing_slots = query_frame.get_missing_slots()
    risk_level = assess_risk_level(query_frame, intent)
    guidance = generate_investigation_guidance(missing_slots)

    # Prioritize slots based on intent
    if intent in ("IMPLEMENT", "MODIFY"):
        priority_order = ["target_feature", "observed_issue", "trigger_condition", "desired_action"]
    else:
        priority_order = ["target_feature", "trigger_condition", "observed_issue", "desired_action"]

    priority_slots = [s for s in priority_order if s in missing_slots]

    return RoutingDecision(
        initial_phase="EXPLORATION",
        initial_tools=guidance.get("recommended_tools", ["query"]),
        priority_slots=priority_slots,
        risk_level=risk_level,
        guidance=guidance,
    )
