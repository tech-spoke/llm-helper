"""
Session State Management for Phase-Gated Execution.

v1.0: Code Intelligence MCP Server
- Phase-gated execution: EXPLORATION → SEMANTIC → VERIFICATION → READY
- Server-side confidence calculation (no LLM self-reporting)
- QueryFrame for structured natural language processing
- ChromaDB-based semantic search (Forest/Map architecture)

v1.1: Context-Aware Guardrails
- Markup files (.html, .css, .blade.php等) への緩和要件
- find_references/find_definitions 必須要件の条件付き解除
- trigger_condition 欠損時のリスク緩和
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Literal


# =============================================================================
# Context-Aware Constants (v1.1)
# =============================================================================

# マークアップファイル拡張子（シンボル検索が意味を成さないファイル）
# 注意: .blade.php, .vue, .jsx, .tsx, .svelte 等は除外
# これらはロジックと密接に結合しており、find_definitions/find_references が有効
MARKUP_EXTENSIONS = {
    ".html", ".htm",                    # 静的HTML
    ".css", ".scss", ".sass", ".less",  # スタイルシート
    ".xml", ".svg",                     # データ/グラフィック
    ".md", ".markdown",                 # ドキュメント
}


def is_markup_context(files: list[str]) -> bool:
    """
    探索済みファイルがすべてマークアップ系かを判定。

    Args:
        files: 探索済みファイルのリスト

    Returns:
        True if all files are markup files
    """
    if not files:
        return False

    for f in files:
        ext = Path(f).suffix.lower()
        if ext not in MARKUP_EXTENSIONS:
            return False
    return True


class Phase(Enum):
    """Execution phases in order."""
    EXPLORATION = auto()      # Phase 1: code-intel required
    SEMANTIC = auto()         # Phase 2: semantic search allowed (if needed)
    VERIFICATION = auto()     # Phase 3: verify semantic hypotheses
    READY = auto()            # Phase 4: implementation allowed


class SemanticReason(Enum):
    """
    Reasons for entering SEMANTIC phase (semantic search).

    Must be one of these - no free text allowed.
    """
    NO_DEFINITION_FOUND = "no_definition_found"
    NO_REFERENCE_FOUND = "no_reference_found"
    NO_SIMILAR_IMPLEMENTATION = "no_similar_implementation"
    ARCHITECTURE_UNKNOWN = "architecture_unknown"
    CONTEXT_FRAGMENTED = "context_fragmented"


class IntentReclassificationRequired(Exception):
    """Raised when Intent needs to be re-evaluated before Write."""
    pass


class InvalidSemanticReason(Exception):
    """Raised when semantic_reason doesn't match missing_requirements."""
    pass


class WriteTargetBlocked(Exception):
    """Raised when Write target was not explored."""
    pass


# =============================================================================
# Exploration Evaluation (Server-side confidence calculation)
# =============================================================================

# Minimum requirements for EXPLORATION to be considered "high" confidence
MIN_EXPLORATION_REQUIREMENTS = {
    "symbols_identified": 2,
    "entry_points": 1,
    "files_analyzed": 2,
    "required_tools": {"find_definitions", "find_references"},
}

# Strict requirements for IMPLEMENT/MODIFY to proceed to READY
STRICT_EXPLORATION_REQUIREMENTS = {
    "symbols_identified": 3,
    "entry_points": 1,
    "files_analyzed": 2,
    "required_tools": {"find_definitions", "find_references"},
    "existing_patterns": 1,
}


def evaluate_exploration(result: "ExplorationResult", intent: str) -> tuple[str, list[str]]:
    """
    サーバー側で confidence を算出する。

    LLM の自己申告ではなく、成果物から機械的に判定。

    Returns:
        (confidence, missing_requirements)
    """
    missing = []
    tools_used = set(result.tools_used)

    # Use strict requirements for IMPLEMENT/MODIFY
    if intent in ("IMPLEMENT", "MODIFY"):
        reqs = STRICT_EXPLORATION_REQUIREMENTS
    else:
        reqs = MIN_EXPLORATION_REQUIREMENTS

    # Check each requirement
    if len(result.symbols_identified) < reqs["symbols_identified"]:
        missing.append(f"symbols_identified: {len(result.symbols_identified)}/{reqs['symbols_identified']}")

    if len(result.entry_points) < reqs["entry_points"]:
        missing.append(f"entry_points: {len(result.entry_points)}/{reqs['entry_points']}")

    if len(result.files_analyzed) < reqs["files_analyzed"]:
        missing.append(f"files_analyzed: {len(result.files_analyzed)}/{reqs['files_analyzed']}")

    if not reqs["required_tools"].issubset(tools_used):
        missing_tools = reqs["required_tools"] - tools_used
        missing.append(f"required_tools: missing {missing_tools}")

    # For IMPLEMENT/MODIFY, also check existing_patterns
    if intent in ("IMPLEMENT", "MODIFY"):
        if len(result.existing_patterns) < reqs.get("existing_patterns", 0):
            missing.append(f"existing_patterns: {len(result.existing_patterns)}/{reqs.get('existing_patterns', 0)}")

    confidence = "high" if not missing else "low"
    return confidence, missing


def can_proceed_to_ready(result: "ExplorationResult", intent: str) -> tuple[bool, list[str]]:
    """
    IMPLEMENT/MODIFY は最低成果条件を満たさないと READY に進めない。

    Returns:
        (can_proceed, missing_requirements)
    """
    if intent not in ("IMPLEMENT", "MODIFY"):
        # INVESTIGATE can proceed with minimal exploration
        return True, []

    confidence, missing = evaluate_exploration(result, intent)
    return confidence == "high", missing


# =============================================================================
# Dynamic Exploration Requirements based on risk_level
# =============================================================================

def get_dynamic_requirements(risk_level: str, intent: str) -> dict:
    """
    リスクレベルに応じた成果条件を返す。

    HIGH リスクでは条件を厳しく、LOW では緩く。
    """
    base = {
        "symbols_identified": 3,
        "entry_points": 1,
        "files_analyzed": 2,
        "existing_patterns": 1,
        "required_slot_evidence": [],
    }

    if intent not in ("IMPLEMENT", "MODIFY"):
        return {
            "symbols_identified": 1,
            "entry_points": 0,
            "files_analyzed": 1,
            "existing_patterns": 0,
            "required_slot_evidence": [],
        }

    if risk_level == "HIGH":
        return {
            "symbols_identified": 5,
            "entry_points": 2,
            "files_analyzed": 4,
            "existing_patterns": 2,
            "required_slot_evidence": ["target_feature", "observed_issue"],
        }
    elif risk_level == "MEDIUM":
        return {
            **base,
            "required_slot_evidence": ["target_feature"],
        }
    else:
        return base


def evaluate_exploration_v36(
    result: "ExplorationResult",
    intent: str,
    risk_level: str = "LOW",
    query_frame: "QueryFrame | None" = None,
) -> tuple[str, list[str]]:
    """
    リスクレベルを考慮した成果評価。

    v1.1: マークアップファイルのみの場合は要件を緩和。

    Args:
        result: 探索結果
        intent: IMPLEMENT, MODIFY, INVESTIGATE, QUESTION
        risk_level: HIGH, MEDIUM, LOW
        query_frame: QueryFrame（スロット証拠チェック用）

    Returns:
        (confidence, missing_requirements)
    """
    missing = []
    tools_used = set(result.tools_used)
    reqs = get_dynamic_requirements(risk_level, intent)

    # v1.1: マークアップコンテキストの判定
    markup_context = is_markup_context(result.files_analyzed)

    # v1.1: マークアップの場合は要件を緩和
    if markup_context:
        reqs = {
            "symbols_identified": 0,  # シンボル不要
            "entry_points": 0,
            "files_analyzed": 1,
            "existing_patterns": 0,
            "required_slot_evidence": [],
        }

    # 基本チェック
    if len(result.symbols_identified) < reqs["symbols_identified"]:
        missing.append(f"symbols_identified: {len(result.symbols_identified)}/{reqs['symbols_identified']}")

    if len(result.entry_points) < reqs["entry_points"]:
        missing.append(f"entry_points: {len(result.entry_points)}/{reqs['entry_points']}")

    if len(result.files_analyzed) < reqs["files_analyzed"]:
        missing.append(f"files_analyzed: {len(result.files_analyzed)}/{reqs['files_analyzed']}")

    if len(result.existing_patterns) < reqs.get("existing_patterns", 0):
        missing.append(f"existing_patterns: {len(result.existing_patterns)}/{reqs.get('existing_patterns', 0)}")

    # v1.1: 必須ツールチェック（マークアップの場合は緩和）
    if markup_context:
        # マークアップの場合: search_text があればOK
        required_tools = {"search_text"}
    else:
        # ロジックファイルの場合: 従来通り
        required_tools = {"find_definitions", "find_references"}

    if not required_tools.issubset(tools_used):
        missing_tools = required_tools - tools_used
        missing.append(f"required_tools: missing {missing_tools}")

    # v3.6: スロット証拠チェック（マークアップの場合はスキップ）
    if not markup_context and query_frame and reqs.get("required_slot_evidence"):
        for slot in reqs["required_slot_evidence"]:
            if slot not in query_frame.slot_evidence:
                missing.append(f"slot_evidence: {slot} not evidenced")

    confidence = "high" if not missing else "low"
    return confidence, missing


# =============================================================================
# Exploration Consistency Check (量だけでなく意味的整合性)
# =============================================================================

def validate_exploration_consistency(result: "ExplorationResult") -> list[str]:
    """
    成果物の相互整合性をチェック。

    LLM が「形式的には条件を満たすが意味のない探索」をすることを防ぐ。

    Returns:
        List of consistency errors (empty if consistent)
    """
    errors = []

    # entry_points は symbols_identified に含まれているか
    # entry_point は通常 "SymbolName.method()" や "function()" の形式
    for ep in result.entry_points:
        # メソッド名を抽出（"Class.method()" → "Class" or "method"）
        base_name = ep.split(".")[0].split("(")[0].strip()
        if not any(
            base_name in sym or sym in base_name
            for sym in result.symbols_identified
        ):
            errors.append(f"entry_point '{ep}' not linked to any symbol in symbols_identified")

    # existing_patterns が files_analyzed に紐づいているか
    if result.existing_patterns and not result.files_analyzed:
        errors.append("patterns provided but no files analyzed")

    # symbols_identified に重複がないか（水増し防止）
    unique_symbols = set(result.symbols_identified)
    if len(unique_symbols) < len(result.symbols_identified):
        errors.append(f"duplicate symbols detected: {len(result.symbols_identified)} given, {len(unique_symbols)} unique")

    # files_analyzed に重複がないか（水増し防止）
    unique_files = set(result.files_analyzed)
    if len(unique_files) < len(result.files_analyzed):
        errors.append(f"duplicate files detected: {len(result.files_analyzed)} given, {len(unique_files)} unique")

    return errors


# =============================================================================
# SEMANTIC Reason Validation (探索失敗の種類に対応)
# =============================================================================

# missing_requirements のキーに対応する許可される semantic_reason
SEMANTIC_ALLOWED_REASONS_BY_MISSING = {
    "symbols_identified": {
        SemanticReason.NO_DEFINITION_FOUND,
        SemanticReason.ARCHITECTURE_UNKNOWN,
    },
    "entry_points": {
        SemanticReason.NO_DEFINITION_FOUND,
        SemanticReason.NO_REFERENCE_FOUND,
    },
    "existing_patterns": {
        SemanticReason.NO_SIMILAR_IMPLEMENTATION,
        SemanticReason.ARCHITECTURE_UNKNOWN,
    },
    "files_analyzed": {
        SemanticReason.CONTEXT_FRAGMENTED,
        SemanticReason.ARCHITECTURE_UNKNOWN,
    },
    # required_tools: ツール未使用は semantic の理由にはならない（ツールを使えば済む）
    # ただし使っても見つからない場合は他の理由が適用される
    "required_tools": set(),
}


def validate_semantic_reason(
    missing_requirements: list[str],
    semantic_reason: SemanticReason,
) -> tuple[bool, str]:
    """
    semantic_reason が missing_requirements に対応しているかチェック。

    「探索をサボる口実として SEMANTIC に逃げる」ことを防ぐ。

    Returns:
        (is_valid, error_message)
    """
    if not missing_requirements:
        # missing がないのに SEMANTIC に来た場合（理論上ありえないが）
        return False, "No missing requirements but entered SEMANTIC phase"

    # missing_requirements から許可される reason を収集
    allowed_reasons: set[SemanticReason] = set()
    for missing in missing_requirements:
        # "symbols_identified: 1/3" → "symbols_identified"
        key = missing.split(":")[0].strip()
        allowed_reasons |= SEMANTIC_ALLOWED_REASONS_BY_MISSING.get(key, set())

    # CONTEXT_FRAGMENTED と ARCHITECTURE_UNKNOWN は汎用的に許可
    allowed_reasons.add(SemanticReason.CONTEXT_FRAGMENTED)
    allowed_reasons.add(SemanticReason.ARCHITECTURE_UNKNOWN)

    if semantic_reason not in allowed_reasons:
        return False, (
            f"semantic_reason '{semantic_reason.value}' is not allowed for missing: {missing_requirements}. "
            f"Allowed reasons: {[r.value for r in allowed_reasons]}"
        )

    return True, ""


# =============================================================================
# Write Target Validation (探索済み範囲に制限)
# =============================================================================

def validate_write_target(
    file_path: str,
    explored_files: set[str],
    allow_new_files: bool = True,
) -> tuple[bool, str]:
    """
    Write 対象が探索済みかチェック。

    「見てないコードを書くな」を物理化。

    Args:
        file_path: 書き込み対象のファイルパス
        explored_files: 探索済みファイルのセット
        allow_new_files: 新規ファイル作成を許可するか

    Returns:
        (is_valid, error_message)
    """
    import os

    # 新規ファイル作成の場合
    if not os.path.exists(file_path):
        if allow_new_files:
            # 新規ファイルは許可（ただし親ディレクトリが探索済みか確認）
            parent_dir = os.path.dirname(file_path)
            explored_dirs = {os.path.dirname(f) for f in explored_files}
            if parent_dir in explored_dirs or not explored_dirs:
                return True, ""
            return False, (
                f"New file '{file_path}' is in unexplored directory. "
                f"Explored directories: {explored_dirs}"
            )
        return False, f"New file creation not allowed: {file_path}"

    # 既存ファイルの場合、探索済みか確認
    # パスの正規化（相対/絶対パスの差異を吸収）
    normalized_path = os.path.normpath(file_path)
    normalized_explored = {os.path.normpath(f) for f in explored_files}

    if normalized_path in normalized_explored:
        return True, ""

    # ファイル名のみでも一致を確認（パスの書き方が異なる場合の救済）
    file_name = os.path.basename(normalized_path)
    explored_names = {os.path.basename(f) for f in explored_files}
    if file_name in explored_names:
        return True, ""

    return False, (
        f"File '{file_path}' was not explored. "
        f"Run EXPLORATION first or add to files_analyzed. "
        f"Explored files: {explored_files}"
    )


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class ExplorationResult:
    """
    Phase 1 output: what was learned from code-intel.

    confidence はサーバー側で算出する（LLM の自己申告は不可）。
    """
    symbols_identified: list[str] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)
    existing_patterns: list[str] = field(default_factory=list)
    files_analyzed: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    notes: str = ""

    # v3.3: Server-calculated confidence (not from LLM)
    _evaluated_confidence: str = field(default="", init=False)
    _missing_requirements: list[str] = field(default_factory=list, init=False)

    def to_dict(self) -> dict:
        return {
            "symbols_identified": self.symbols_identified,
            "entry_points": self.entry_points,
            "existing_patterns": self.existing_patterns,
            "files_analyzed": self.files_analyzed,
            "tools_used": self.tools_used,
            "notes": self.notes,
            "evaluated_confidence": self._evaluated_confidence,
            "missing_requirements": self._missing_requirements,
        }


@dataclass
class Hypothesis:
    """
    構造化された仮説。

    改善サイクルで「低 confidence の仮説は失敗しやすい」等の分析が可能。
    """
    text: str
    confidence: Literal["high", "medium", "low"] = "medium"

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "confidence": self.confidence,
        }


@dataclass
class SemanticResult:
    """
    Phase 2 output: hypotheses from semantic search.

    semantic_reason は SemanticReason Enum のみ許可。
    """
    hypotheses: list[Hypothesis] = field(default_factory=list)
    semantic_reason: SemanticReason | None = None
    search_queries: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "hypotheses": [h.to_dict() for h in self.hypotheses],
            "semantic_reason": self.semantic_reason.value if self.semantic_reason else None,
            "search_queries": self.search_queries,
        }


@dataclass
class VerificationEvidence:
    """
    構造化された evidence。

    裏取りに使用したツール・対象・結果を必須化。
    """
    tool: str  # 使用したツール（find_definitions, find_references, etc.）
    target: str  # 検証対象（シンボル名、ファイル名など）
    result: str  # ツールの結果概要
    files: list[str] = field(default_factory=list)  # 関連ファイル

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "target": self.target,
            "result": self.result,
            "files": self.files,
        }


@dataclass
class VerifiedHypothesis:
    """
    検証済み仮説。

    evidence を構造化することで「裏取りしたフリ」を防止。
    """
    hypothesis: str
    status: Literal["confirmed", "rejected"]
    evidence: VerificationEvidence

    def to_dict(self) -> dict:
        return {
            "hypothesis": self.hypothesis,
            "status": self.status,
            "evidence": self.evidence.to_dict(),
        }


@dataclass
class VerificationResult:
    """
    Phase 3 output: verified hypotheses.

    verified は VerifiedHypothesis のリスト。
    """
    verified: list[VerifiedHypothesis] = field(default_factory=list)
    all_confirmed: bool = False

    def to_dict(self) -> dict:
        return {
            "verified": [v.to_dict() for v in self.verified],
            "all_confirmed": self.all_confirmed,
        }


# =============================================================================
# Valid tools for verification evidence
# =============================================================================

VALID_VERIFICATION_TOOLS = {
    "find_definitions",
    "find_references",
    "search_text",
    "analyze_structure",
    "query",
}


def validate_verification_evidence(evidence: VerificationEvidence) -> tuple[bool, str]:
    """
    evidence が有効かチェック。

    Returns:
        (is_valid, error_message)
    """
    if evidence.tool not in VALID_VERIFICATION_TOOLS:
        return False, f"Invalid tool '{evidence.tool}'. Must be one of: {VALID_VERIFICATION_TOOLS}"

    if not evidence.target:
        return False, "evidence.target is required"

    if not evidence.result:
        return False, "evidence.result is required"

    return True, ""


# =============================================================================
# Session State
# =============================================================================

@dataclass
class SessionState:
    """
    Manages the state of a code implementation session.

    Features:
    - Phase-gated execution (EXPLORATION → SEMANTIC → VERIFICATION → READY)
    - Server-side confidence calculation
    - QueryFrame for natural language structuring
    - Forest/Map dual search results
    - Write target validation

    Enforces phase-gated execution:
    - Tools are restricted based on current phase
    - Phase transitions require specific outputs
    - Prevents LLM from skipping steps
    """
    # Session identity
    session_id: str
    intent: str  # IMPLEMENT, MODIFY, INVESTIGATE, QUESTION
    query: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    # v3.8: Project path for agreements and learned_pairs
    repo_path: str = "."

    # v3.5: Decision Log for Outcome matching
    decision_log: dict | None = None

    # v3.6: QueryFrame for natural language structuring
    query_frame: "QueryFrame | None" = None
    risk_level: str = "LOW"  # HIGH, MEDIUM, LOW

    # Current phase
    phase: Phase = Phase.EXPLORATION

    # Phase outputs
    exploration: ExplorationResult | None = None
    semantic: SemanticResult | None = None
    verification: VerificationResult | None = None

    # Semantic search results (Forest/Map architecture)
    map_results: list[dict] = field(default_factory=list)  # 地図検索結果
    forest_results: list[dict] = field(default_factory=list)  # 森検索結果
    map_hit: bool = False  # 地図でヒットしたか（Short-circuit用）

    # Tracking
    tool_calls: list[dict] = field(default_factory=list)
    phase_history: list[dict] = field(default_factory=list)

    def record_tool_call(
        self,
        tool: str,
        params: dict,
        result_summary: str,
        result_detail: dict | None = None,
    ) -> None:
        """
        Record a tool call for tracking.

        result_detail: 改善サイクルでの分析用。
        """
        record = {
            "tool": tool,
            "params": params,
            "result_summary": result_summary,
            "phase": self.phase.name,
            "timestamp": datetime.now().isoformat(),
        }
        # v3.5: 詳細結果があれば追加
        if result_detail:
            record["result_detail"] = result_detail
        self.tool_calls.append(record)

    def get_allowed_tools(self) -> list[str]:
        """
        Get tools allowed in current phase.

        Semantic Search Rule をコードで実装。
        """
        if self.phase == Phase.EXPLORATION:
            # semantic_search は明示的に除外
            return [
                "query", "search_text", "find_definitions",
                "find_references", "analyze_structure", "get_symbols",
                "get_function_at_line", "search_files",
                "submit_understanding",
            ]
        elif self.phase == Phase.SEMANTIC:
            return [
                "semantic_search",
                "submit_semantic",
                "search_text", "find_definitions", "find_references",
            ]
        elif self.phase == Phase.VERIFICATION:
            # semantic_search は明示的に除外
            return [
                "query", "search_text", "find_definitions",
                "find_references", "analyze_structure",
                "submit_verification",
            ]
        elif self.phase == Phase.READY:
            return ["*"]
        return []

    def is_tool_allowed(self, tool: str) -> bool:
        """Check if a tool is allowed in current phase."""
        allowed = self.get_allowed_tools()
        if "*" in allowed:
            return True
        return tool in allowed

    def get_blocked_reason(self, tool: str) -> str:
        """Get reason why a tool is blocked."""
        if self.phase == Phase.EXPLORATION:
            if tool == "semantic_search":
                return (
                    "semantic_search is not allowed in EXPLORATION phase. "
                    "First use code-intel tools (query, find_definitions, find_references). "
                    "Then call submit_understanding. Server will evaluate if semantic search is needed."
                )
        elif self.phase == Phase.VERIFICATION:
            if tool == "semantic_search":
                return (
                    "semantic_search is not allowed in VERIFICATION phase. "
                    "Use code-intel tools to verify hypotheses from semantic search."
                )
        return f"Tool '{tool}' is not allowed in phase {self.phase.name}"

    def check_intent_before_write(self) -> None:
        """
        READY フェーズで Write が要求された場合、Intent を確認。

        INVESTIGATE のまま Write しようとしたら例外。
        """
        if self.intent == "INVESTIGATE":
            raise IntentReclassificationRequired(
                "Intent is INVESTIGATE but Write was requested. "
                "Re-classify intent to IMPLEMENT or MODIFY first."
            )

    def check_write_target(self, file_path: str, allow_new_files: bool = True) -> dict:
        """
        Write 対象が探索済みかチェック。

        「見てないコードを書くな」を物理化。

        Args:
            file_path: 書き込み対象のファイルパス
            allow_new_files: 新規ファイル作成を許可するか

        Returns:
            {"allowed": bool, "error": str | None}
        """
        if self.phase != Phase.READY:
            return {
                "allowed": False,
                "error": f"Write not allowed in phase {self.phase.name}",
            }

        # 探索済みファイルを取得
        explored_files: set[str] = set()
        if self.exploration:
            explored_files = set(self.exploration.files_analyzed)

        # 検証で触れたファイルも追加
        if self.verification:
            for vh in self.verification.verified:
                explored_files.update(vh.evidence.files)

        # ファイルがない場合（QUESTION intent など）は制限なし
        if not explored_files:
            return {"allowed": True, "error": None}

        is_valid, error = validate_write_target(file_path, explored_files, allow_new_files)
        if is_valid:
            return {"allowed": True, "error": None}

        return {
            "allowed": False,
            "error": error,
            "explored_files": list(explored_files),
            "hint": "Add the file to exploration first, or run additional exploration.",
            "recovery_options": {
                "add_explored_files": {
                    "description": "Add files/directories to explored list without leaving READY phase",
                    "example": "session.add_explored_files(['tests_with_code/'])",
                },
                "revert_to_exploration": {
                    "description": "Go back to EXPLORATION phase for thorough re-exploration",
                    "example": "session.revert_to_exploration()",
                },
            },
        }

    def add_explored_files(self, files: list[str]) -> dict:
        """
        READYフェーズで探索済みファイルを追加登録。

        check_write_target でブロックされた場合の軽量な復帰手段。
        新しいディレクトリやファイルを探索済みとして追加できる。

        Args:
            files: 追加する探索済みファイル/ディレクトリのリスト

        Returns:
            {"success": bool, "added": list, "explored_files": list}
        """
        if self.phase != Phase.READY:
            return {
                "success": False,
                "error": f"add_explored_files is only allowed in READY phase, current: {self.phase.name}",
            }

        if not files:
            return {
                "success": False,
                "error": "No files provided to add",
            }

        # exploration が None の場合は初期化
        if self.exploration is None:
            self.exploration = ExplorationResult()

        added = []
        for f in files:
            if f not in self.exploration.files_analyzed:
                self.exploration.files_analyzed.append(f)
                added.append(f)

        return {
            "success": True,
            "added": added,
            "explored_files": self.exploration.files_analyzed,
            "message": f"Added {len(added)} file(s) to explored list.",
        }

    def revert_to_exploration(self, keep_results: bool = True) -> dict:
        """
        EXPLORATIONフェーズに戻る。

        check_write_target でブロックされた場合や、
        追加の探索が必要な場合に使用。

        Args:
            keep_results: True の場合、既存の探索結果を保持

        Returns:
            {"success": bool, "previous_phase": str, "current_phase": str}
        """
        previous_phase = self.phase.name

        if self.phase == Phase.EXPLORATION:
            return {
                "success": True,
                "previous_phase": previous_phase,
                "current_phase": Phase.EXPLORATION.name,
                "message": "Already in EXPLORATION phase.",
            }

        # フェーズを戻す
        self.phase = Phase.EXPLORATION

        # 結果をクリアするかどうか
        if not keep_results:
            self.semantic = None
            self.verification = None
            # exploration は保持（追加探索のため）

        # フェーズ履歴に記録
        self.phase_history.append({
            "action": "revert_to_exploration",
            "from": previous_phase,
            "to": Phase.EXPLORATION.name,
            "keep_results": keep_results,
            "timestamp": datetime.now().isoformat(),
        })

        return {
            "success": True,
            "previous_phase": previous_phase,
            "current_phase": Phase.EXPLORATION.name,
            "kept_exploration": self.exploration.to_dict() if self.exploration else None,
            "message": f"Reverted from {previous_phase} to EXPLORATION. "
                      f"{'Previous exploration results kept.' if keep_results else 'Results cleared.'}",
        }

    def submit_exploration(self, result: ExplorationResult) -> dict:
        """
        Submit exploration results and determine next phase.

        Features:
        - risk_level と QueryFrame を考慮した動的成果条件
        - confidence はサーバー側で算出（LLM の自己申告は無視）
        - 成果物の相互整合性チェック
        - v1.1: マークアップコンテキストでのリスクレベル再評価

        Returns: {"success": bool, "next_phase": str, "message": str}
        """
        if self.phase != Phase.EXPLORATION:
            return {
                "success": False,
                "next_phase": self.phase.name,
                "message": f"Cannot submit exploration in phase {self.phase.name}",
            }

        # v1.1: マークアップコンテキストの場合、リスクレベルを再評価
        # trigger_condition欠損によるHIGHリスクを緩和
        effective_risk_level = self.risk_level
        markup_context = is_markup_context(result.files_analyzed)
        if markup_context and self.risk_level == "HIGH":
            effective_risk_level = "LOW"

        # v3.6: リスクレベルを考慮した成果評価
        confidence, missing = evaluate_exploration_v36(
            result,
            self.intent,
            effective_risk_level,
            self.query_frame,
        )

        # v3.4: 成果物の相互整合性チェック
        consistency_errors = validate_exploration_consistency(result)
        if consistency_errors:
            confidence = "low"
            missing.extend([f"consistency: {e}" for e in consistency_errors])

        # v3.6: NL→シンボル整合性チェック
        nl_symbol_errors = self._validate_nl_symbol_mapping(result)
        if nl_symbol_errors:
            confidence = "low"
            missing.extend(nl_symbol_errors)

        result._evaluated_confidence = confidence
        result._missing_requirements = missing

        self.exploration = result

        # Record phase transition
        self.phase_history.append({
            "from": Phase.EXPLORATION.name,
            "result": result.to_dict(),
            "evaluated_confidence": confidence,
            "missing_requirements": missing,
            "consistency_errors": consistency_errors,
            "risk_level": self.risk_level,
            "effective_risk_level": effective_risk_level,  # v1.1
            "markup_context": markup_context,  # v1.1
            "timestamp": datetime.now().isoformat(),
        })

        # v3.6: HYPOTHESISスロットが残っていればREADYに進めない
        # v1.1: マークアップコンテキストの場合はHYPOTHESISチェックをスキップ
        hypothesis_block = []
        if not markup_context:
            hypothesis_block = self._check_hypothesis_slots()
            if hypothesis_block:
                missing.extend(hypothesis_block)
                confidence = "low"

        # IMPLEMENT/MODIFY は最低成果条件を満たさないと READY に進めない
        can_proceed = confidence == "high" and not consistency_errors and not hypothesis_block

        if can_proceed:
            self.phase = Phase.READY
            response = {
                "success": True,
                "next_phase": Phase.READY.name,
                "evaluated_confidence": confidence,
                "risk_level": effective_risk_level,
                "message": "Exploration sufficient. Ready for implementation.",
            }
            # v1.1: マークアップコンテキストの場合は明示
            if markup_context:
                response["markup_context"] = True
                response["relaxed_requirements"] = "find_definitions/find_references not required for markup files"
            return response
        else:
            self.phase = Phase.SEMANTIC
            response = {
                "success": True,
                "next_phase": Phase.SEMANTIC.name,
                "evaluated_confidence": confidence,
                "missing_requirements": missing,
                "risk_level": effective_risk_level,
                "message": "Exploration insufficient. Use semantic_search to gather more context.",
                "hint": "Use semantic_search, then submit_semantic with hypotheses and semantic_reason.",
            }
            if consistency_errors:
                response["consistency_errors"] = consistency_errors
                response["consistency_hint"] = (
                    "Your exploration results have consistency issues. "
                    "Ensure entry_points are linked to symbols, no duplicates, etc."
                )
            return response

    def _validate_nl_symbol_mapping(self, result: ExplorationResult) -> list[str]:
        """
        NL用語とシンボルの整合性チェック。

        target_feature に対応するシンボルが見つかっているか検証。
        """
        errors = []

        if not self.query_frame or not self.query_frame.target_feature:
            return errors

        from tools.query_frame import validate_nl_symbol_mapping

        has_match, matched = validate_nl_symbol_mapping(
            self.query_frame.target_feature,
            result.symbols_identified,
        )

        if not has_match:
            errors.append(
                f"nl_symbol_mapping: '{self.query_frame.target_feature}' "
                f"has no matching symbol in {result.symbols_identified}"
            )

        return errors

    def _check_hypothesis_slots(self) -> list[str]:
        """
        HYPOTHESISスロットが残っていないかチェック。

        HYPOTHESISはVERIFICATION必須。
        """
        if not self.query_frame:
            return []

        from tools.query_frame import validate_for_ready

        return validate_for_ready(self.query_frame)

    def submit_semantic(self, result: SemanticResult) -> dict:
        """
        Submit semantic search results and move to verification.

        semantic_reason は SemanticReason Enum のみ許可。
        semantic_reason が missing_requirements に対応しているかチェック。

        Returns: {"success": bool, "next_phase": str, "message": str}
        """
        if self.phase != Phase.SEMANTIC:
            return {
                "success": False,
                "next_phase": self.phase.name,
                "message": f"Cannot submit semantic in phase {self.phase.name}",
            }

        # semantic_reason は Enum 必須
        if result.semantic_reason is None:
            return {
                "success": False,
                "next_phase": self.phase.name,
                "message": "semantic_reason is required (must be SemanticReason enum).",
                "valid_reasons": [r.value for r in SemanticReason],
            }

        if not isinstance(result.semantic_reason, SemanticReason):
            return {
                "success": False,
                "next_phase": self.phase.name,
                "message": f"semantic_reason must be SemanticReason enum, got: {type(result.semantic_reason)}",
                "valid_reasons": [r.value for r in SemanticReason],
            }

        # semantic_reason が missing_requirements に対応しているかチェック
        if self.exploration and self.exploration._missing_requirements:
            is_valid, error = validate_semantic_reason(
                self.exploration._missing_requirements,
                result.semantic_reason,
            )
            if not is_valid:
                return {
                    "success": False,
                    "next_phase": self.phase.name,
                    "message": error,
                    "missing_requirements": self.exploration._missing_requirements,
                    "hint": "Choose a semantic_reason that matches why exploration failed.",
                }

        if not result.hypotheses:
            return {
                "success": False,
                "next_phase": self.phase.name,
                "message": "At least one hypothesis is required from semantic search results.",
            }

        self.semantic = result

        # Record phase transition
        self.phase_history.append({
            "from": Phase.SEMANTIC.name,
            "result": result.to_dict(),
            "reason_validated": True,
            "timestamp": datetime.now().isoformat(),
        })

        self.phase = Phase.VERIFICATION
        return {
            "success": True,
            "next_phase": Phase.VERIFICATION.name,
            "message": "Semantic search complete. Now verify hypotheses with code-intel.",
            "hypotheses_to_verify": [h.to_dict() for h in result.hypotheses],
        }

    def submit_verification(self, result: VerificationResult) -> dict:
        """
        Submit verification results and move to ready.

        evidence は構造化必須。

        Returns: {"success": bool, "next_phase": str, "message": str}
        """
        if self.phase != Phase.VERIFICATION:
            return {
                "success": False,
                "next_phase": self.phase.name,
                "message": f"Cannot submit verification in phase {self.phase.name}",
            }

        if not result.verified:
            return {
                "success": False,
                "next_phase": self.phase.name,
                "message": "Must verify at least one hypothesis.",
            }

        # v3.3: evidence の検証
        for vh in result.verified:
            is_valid, error = validate_verification_evidence(vh.evidence)
            if not is_valid:
                return {
                    "success": False,
                    "next_phase": self.phase.name,
                    "message": f"Invalid evidence for hypothesis '{vh.hypothesis}': {error}",
                    "valid_tools": list(VALID_VERIFICATION_TOOLS),
                }

        self.verification = result

        # Record phase transition
        self.phase_history.append({
            "from": Phase.VERIFICATION.name,
            "result": result.to_dict(),
            "timestamp": datetime.now().isoformat(),
        })

        self.phase = Phase.READY

        rejected = [v for v in result.verified if v.status == "rejected"]
        if rejected:
            return {
                "success": True,
                "next_phase": Phase.READY.name,
                "message": "Verification complete with some rejected hypotheses.",
                "warning": f"{len(rejected)} hypotheses were rejected. Do NOT implement based on rejected hypotheses.",
                "rejected": [r.to_dict() for r in rejected],
            }

        return {
            "success": True,
            "next_phase": Phase.READY.name,
            "message": "All hypotheses verified. Ready for implementation.",
        }

    def get_status(self) -> dict:
        """Get current session status."""
        return {
            "session_id": self.session_id,
            "intent": self.intent,
            "query": self.query,
            "current_phase": self.phase.name,
            "allowed_tools": self.get_allowed_tools(),
            "exploration": self.exploration.to_dict() if self.exploration else None,
            "semantic": self.semantic.to_dict() if self.semantic else None,
            "verification": self.verification.to_dict() if self.verification else None,
            "tool_calls_count": len(self.tool_calls),
        }

    def to_dict(self) -> dict:
        """Full serialization for logging."""
        return {
            "session_id": self.session_id,
            "intent": self.intent,
            "query": self.query,
            "created_at": self.created_at,
            "current_phase": self.phase.name,
            "decision_log": self.decision_log,
            "query_frame": self.query_frame.to_dict() if self.query_frame else None,
            "risk_level": self.risk_level,
            "exploration": self.exploration.to_dict() if self.exploration else None,
            "semantic": self.semantic.to_dict() if self.semantic else None,
            "verification": self.verification.to_dict() if self.verification else None,
            "tool_calls": self.tool_calls,
            "phase_history": self.phase_history,
        }


# =============================================================================
# Session Manager
# =============================================================================

class SessionManager:
    """
    Manages multiple sessions.

    In practice, there's usually one active session per conversation,
    but this allows for session tracking and recovery.
    """

    def __init__(self):
        self._sessions: dict[str, SessionState] = {}
        self._active_session_id: str | None = None

    def create_session(
        self,
        intent: str,
        query: str,
        session_id: str | None = None,
        repo_path: str = ".",
    ) -> SessionState:
        """
        Create a new session.

        repo_path: agreements と learned_pairs の保存先を指定。
        """
        if session_id is None:
            session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        # QUESTION intent skips all phases
        initial_phase = Phase.READY if intent == "QUESTION" else Phase.EXPLORATION

        session = SessionState(
            session_id=session_id,
            intent=intent,
            query=query,
            phase=initial_phase,
            repo_path=repo_path,
        )

        self._sessions[session_id] = session
        self._active_session_id = session_id

        return session

    def get_session(self, session_id: str) -> SessionState | None:
        """Get a session by ID."""
        return self._sessions.get(session_id)

    def get_active_session(self) -> SessionState | None:
        """Get the currently active session."""
        if self._active_session_id:
            return self._sessions.get(self._active_session_id)
        return None

    def set_active_session(self, session_id: str) -> bool:
        """Set the active session."""
        if session_id in self._sessions:
            self._active_session_id = session_id
            return True
        return False

    def list_sessions(self) -> list[dict]:
        """List all sessions with basic info."""
        return [
            {
                "session_id": s.session_id,
                "intent": s.intent,
                "phase": s.phase.name,
                "created_at": s.created_at,
                "active": s.session_id == self._active_session_id,
            }
            for s in self._sessions.values()
        ]
