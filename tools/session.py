"""
Session State Management for Phase-Gated Execution.

v3.6: 自然文対応とQueryFrame統合
- QueryFrame: 自然文を構造化したスロット
- slot_source: FACT/HYPOTHESIS の区別
- risk_level による動的成果条件
- NL→シンボル整合性検証

v3.4: 抜け穴を塞ぐ
- 成果物の相互整合性チェック（量だけでなく意味的整合性）
- SEMANTIC 突入理由を missing_requirements に紐付け
- READY での Write 対象を探索済みファイルに制限

v3.3: LLM に判断をさせない設計
- confidence はサーバー側で算出（LLM の自己申告を廃止）
- 最低成果条件を満たさないと READY に進めない
- evidence は構造化（裏取りしたフリを防止）
- Intent 再評価フックを追加

v3.2: Phase management
1. EXPLORATION - code-intel tools (required)
2. SEMANTIC - devrag (if exploration insufficient)
3. VERIFICATION - re-verify devrag hypotheses with code-intel
4. READY - implementation allowed
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Literal


class Phase(Enum):
    """Execution phases in order."""
    EXPLORATION = auto()      # Phase 1: code-intel required
    SEMANTIC = auto()         # Phase 2: devrag allowed (if needed)
    VERIFICATION = auto()     # Phase 3: verify devrag hypotheses
    READY = auto()            # Phase 4: implementation allowed


class DevragReason(Enum):
    """
    v3.3: Reasons for using devrag (semantic search).

    Must be one of these - no free text allowed.
    """
    NO_DEFINITION_FOUND = "no_definition_found"
    NO_REFERENCE_FOUND = "no_reference_found"
    NO_SIMILAR_IMPLEMENTATION = "no_similar_implementation"
    ARCHITECTURE_UNKNOWN = "architecture_unknown"
    CONTEXT_FRAGMENTED = "context_fragmented"


class IntentReclassificationRequired(Exception):
    """v3.3: Raised when Intent needs to be re-evaluated before Write."""
    pass


class InvalidSemanticReason(Exception):
    """v3.4: Raised when devrag_reason doesn't match missing_requirements."""
    pass


class WriteTargetBlocked(Exception):
    """v3.4: Raised when Write target was not explored."""
    pass


# =============================================================================
# v3.3: Exploration Evaluation (Server-side confidence calculation)
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
    v3.3: サーバー側で confidence を算出する。

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
    v3.3: IMPLEMENT/MODIFY は最低成果条件を満たさないと READY に進めない。

    Returns:
        (can_proceed, missing_requirements)
    """
    if intent not in ("IMPLEMENT", "MODIFY"):
        # INVESTIGATE can proceed with minimal exploration
        return True, []

    confidence, missing = evaluate_exploration(result, intent)
    return confidence == "high", missing


# =============================================================================
# v3.6: Dynamic Exploration Requirements based on risk_level
# =============================================================================

def get_dynamic_requirements(risk_level: str, intent: str) -> dict:
    """
    v3.6: リスクレベルに応じた成果条件を返す。

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
    v3.6: リスクレベルを考慮した成果評価。

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

    # 基本チェック
    if len(result.symbols_identified) < reqs["symbols_identified"]:
        missing.append(f"symbols_identified: {len(result.symbols_identified)}/{reqs['symbols_identified']}")

    if len(result.entry_points) < reqs["entry_points"]:
        missing.append(f"entry_points: {len(result.entry_points)}/{reqs['entry_points']}")

    if len(result.files_analyzed) < reqs["files_analyzed"]:
        missing.append(f"files_analyzed: {len(result.files_analyzed)}/{reqs['files_analyzed']}")

    if len(result.existing_patterns) < reqs.get("existing_patterns", 0):
        missing.append(f"existing_patterns: {len(result.existing_patterns)}/{reqs.get('existing_patterns', 0)}")

    # 必須ツールチェック
    required_tools = {"find_definitions", "find_references"}
    if not required_tools.issubset(tools_used):
        missing_tools = required_tools - tools_used
        missing.append(f"required_tools: missing {missing_tools}")

    # v3.6: スロット証拠チェック
    if query_frame and reqs.get("required_slot_evidence"):
        for slot in reqs["required_slot_evidence"]:
            if slot not in query_frame.slot_evidence:
                missing.append(f"slot_evidence: {slot} not evidenced")

    confidence = "high" if not missing else "low"
    return confidence, missing


# =============================================================================
# v3.4: Exploration Consistency Check (量だけでなく意味的整合性)
# =============================================================================

def validate_exploration_consistency(result: "ExplorationResult") -> list[str]:
    """
    v3.4: 成果物の相互整合性をチェック。

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
# v3.4: SEMANTIC Reason Validation (探索失敗の種類に対応)
# =============================================================================

# missing_requirements のキーに対応する許可される devrag_reason
DEVRAG_ALLOWED_REASONS_BY_MISSING = {
    "symbols_identified": {
        DevragReason.NO_DEFINITION_FOUND,
        DevragReason.ARCHITECTURE_UNKNOWN,
    },
    "entry_points": {
        DevragReason.NO_DEFINITION_FOUND,
        DevragReason.NO_REFERENCE_FOUND,
    },
    "existing_patterns": {
        DevragReason.NO_SIMILAR_IMPLEMENTATION,
        DevragReason.ARCHITECTURE_UNKNOWN,
    },
    "files_analyzed": {
        DevragReason.CONTEXT_FRAGMENTED,
        DevragReason.ARCHITECTURE_UNKNOWN,
    },
    "required_tools": {
        # ツール未使用は devrag の理由にはならない（ツールを使えば済む）
        # ただし使っても見つからない場合は他の理由が適用される
    },
}


def validate_semantic_reason(
    missing_requirements: list[str],
    devrag_reason: DevragReason,
) -> tuple[bool, str]:
    """
    v3.4: devrag_reason が missing_requirements に対応しているかチェック。

    「探索をサボる口実として SEMANTIC に逃げる」ことを防ぐ。

    Returns:
        (is_valid, error_message)
    """
    if not missing_requirements:
        # missing がないのに SEMANTIC に来た場合（理論上ありえないが）
        return False, "No missing requirements but entered SEMANTIC phase"

    # missing_requirements から許可される reason を収集
    allowed_reasons: set[DevragReason] = set()
    for missing in missing_requirements:
        # "symbols_identified: 1/3" → "symbols_identified"
        key = missing.split(":")[0].strip()
        allowed_reasons |= DEVRAG_ALLOWED_REASONS_BY_MISSING.get(key, set())

    # CONTEXT_FRAGMENTED と ARCHITECTURE_UNKNOWN は汎用的に許可
    allowed_reasons.add(DevragReason.CONTEXT_FRAGMENTED)
    allowed_reasons.add(DevragReason.ARCHITECTURE_UNKNOWN)

    if devrag_reason not in allowed_reasons:
        return False, (
            f"devrag_reason '{devrag_reason.value}' is not allowed for missing: {missing_requirements}. "
            f"Allowed reasons: {[r.value for r in allowed_reasons]}"
        )

    return True, ""


# =============================================================================
# v3.4: Write Target Validation (探索済み範囲に制限)
# =============================================================================

def validate_write_target(
    file_path: str,
    explored_files: set[str],
    allow_new_files: bool = True,
) -> tuple[bool, str]:
    """
    v3.4: Write 対象が探索済みかチェック。

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

    v3.3: confidence フィールドを削除。サーバー側で算出する。
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
    v3.5: 構造化された仮説。

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
    Phase 2 output: hypotheses from devrag.

    v3.3: devrag_reason は DevragReason Enum のみ許可。
    v3.5: hypotheses は Hypothesis オブジェクトのリスト（confidence 付き）。
    """
    hypotheses: list[Hypothesis] = field(default_factory=list)
    devrag_reason: DevragReason | None = None
    search_queries: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "hypotheses": [h.to_dict() for h in self.hypotheses],
            "devrag_reason": self.devrag_reason.value if self.devrag_reason else None,
            "search_queries": self.search_queries,
        }


@dataclass
class VerificationEvidence:
    """
    v3.3: 構造化された evidence。

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
    v3.3: 検証済み仮説。

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

    v3.3: verified は VerifiedHypothesis のリスト。
    """
    verified: list[VerifiedHypothesis] = field(default_factory=list)
    all_confirmed: bool = False

    def to_dict(self) -> dict:
        return {
            "verified": [v.to_dict() for v in self.verified],
            "all_confirmed": self.all_confirmed,
        }


# =============================================================================
# v3.3: Valid tools for verification evidence
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
    v3.3: evidence が有効かチェック。

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

    v3.8 changes:
    - Added repo_path for project-specific paths
    - Added map_results and forest_results for devrag dual search
    - Added map_hit flag for short-circuit logic

    v3.6 changes:
    - Added query_frame for natural language structuring
    - Added risk_level for dynamic requirements
    - Added NL→symbol mapping validation

    v3.5 changes:
    - Added decision_log for Outcome Log matching

    v3.3 changes:
    - confidence is server-calculated
    - Strict requirements for IMPLEMENT/MODIFY
    - Intent re-evaluation before Write

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

    # v3.8: Devrag dual search results
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
        result_detail: dict | None = None,  # v3.5: 詳細結果
    ) -> None:
        """
        Record a tool call for tracking.

        v3.5: result_detail を追加。改善サイクルでの分析用。
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
        v3.3: Get tools allowed in current phase.

        Semantic Search Rule をコードで実装。
        """
        if self.phase == Phase.EXPLORATION:
            # devrag は明示的に除外
            return [
                "query", "search_text", "find_definitions",
                "find_references", "analyze_structure", "get_symbols",
                "repo_pack", "get_function_at_line", "search_files",
                "submit_understanding",
            ]
        elif self.phase == Phase.SEMANTIC:
            return [
                "devrag_search",
                "submit_semantic",
                "search_text", "find_definitions", "find_references",
            ]
        elif self.phase == Phase.VERIFICATION:
            # devrag は明示的に除外
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
            if tool == "devrag_search":
                return (
                    "devrag is not allowed in EXPLORATION phase. "
                    "First use code-intel tools (query, find_definitions, find_references). "
                    "Then call submit_understanding. Server will evaluate if devrag is needed."
                )
        elif self.phase == Phase.VERIFICATION:
            if tool == "devrag_search":
                return (
                    "devrag is not allowed in VERIFICATION phase. "
                    "Use code-intel tools to verify hypotheses from devrag."
                )
        return f"Tool '{tool}' is not allowed in phase {self.phase.name}"

    def check_intent_before_write(self) -> None:
        """
        v3.3: READY フェーズで Write が要求された場合、Intent を確認。

        INVESTIGATE のまま Write しようとしたら例外。
        """
        if self.intent == "INVESTIGATE":
            raise IntentReclassificationRequired(
                "Intent is INVESTIGATE but Write was requested. "
                "Re-classify intent to IMPLEMENT or MODIFY first."
            )

    def check_write_target(self, file_path: str, allow_new_files: bool = True) -> dict:
        """
        v3.4: Write 対象が探索済みかチェック。

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
        }

    def submit_exploration(self, result: ExplorationResult) -> dict:
        """
        Submit exploration results and determine next phase.

        v3.6: risk_level と QueryFrame を考慮した動的成果条件。
        v3.3: confidence はサーバー側で算出。LLM の自己申告は無視。
        v3.4: 成果物の相互整合性チェックを追加。

        Returns: {"success": bool, "next_phase": str, "message": str}
        """
        if self.phase != Phase.EXPLORATION:
            return {
                "success": False,
                "next_phase": self.phase.name,
                "message": f"Cannot submit exploration in phase {self.phase.name}",
            }

        # v3.6: リスクレベルを考慮した成果評価
        confidence, missing = evaluate_exploration_v36(
            result,
            self.intent,
            self.risk_level,
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
            "risk_level": self.risk_level,  # v3.6
            "timestamp": datetime.now().isoformat(),
        })

        # v3.6: HYPOTHESISスロットが残っていればREADYに進めない
        hypothesis_block = self._check_hypothesis_slots()
        if hypothesis_block:
            missing.extend(hypothesis_block)
            confidence = "low"

        # IMPLEMENT/MODIFY は最低成果条件を満たさないと READY に進めない
        can_proceed = confidence == "high" and not consistency_errors and not hypothesis_block

        if can_proceed:
            self.phase = Phase.READY
            return {
                "success": True,
                "next_phase": Phase.READY.name,
                "evaluated_confidence": confidence,
                "risk_level": self.risk_level,
                "message": "Exploration sufficient. Ready for implementation.",
            }
        else:
            self.phase = Phase.SEMANTIC
            response = {
                "success": True,
                "next_phase": Phase.SEMANTIC.name,
                "evaluated_confidence": confidence,
                "missing_requirements": missing,
                "risk_level": self.risk_level,
                "message": "Exploration insufficient. Use devrag to gather more context.",
                "hint": "Use devrag_search, then submit_semantic with hypotheses and devrag_reason.",
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
        v3.6: NL用語とシンボルの整合性チェック。

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
        v3.6: HYPOTHESISスロットが残っていないかチェック。

        HYPOTHESISはVERIFICATION必須。
        """
        if not self.query_frame:
            return []

        from tools.query_frame import validate_for_ready

        return validate_for_ready(self.query_frame)

    def submit_semantic(self, result: SemanticResult) -> dict:
        """
        Submit semantic (devrag) results and move to verification.

        v3.3: devrag_reason は DevragReason Enum のみ許可。
        v3.4: devrag_reason が missing_requirements に対応しているかチェック。

        Returns: {"success": bool, "next_phase": str, "message": str}
        """
        if self.phase != Phase.SEMANTIC:
            return {
                "success": False,
                "next_phase": self.phase.name,
                "message": f"Cannot submit semantic in phase {self.phase.name}",
            }

        # v3.3: devrag_reason は Enum 必須
        if result.devrag_reason is None:
            return {
                "success": False,
                "next_phase": self.phase.name,
                "message": "devrag_reason is required (must be DevragReason enum).",
                "valid_reasons": [r.value for r in DevragReason],
            }

        if not isinstance(result.devrag_reason, DevragReason):
            return {
                "success": False,
                "next_phase": self.phase.name,
                "message": f"devrag_reason must be DevragReason enum, got: {type(result.devrag_reason)}",
                "valid_reasons": [r.value for r in DevragReason],
            }

        # v3.4: devrag_reason が missing_requirements に対応しているかチェック
        if self.exploration and self.exploration._missing_requirements:
            is_valid, error = validate_semantic_reason(
                self.exploration._missing_requirements,
                result.devrag_reason,
            )
            if not is_valid:
                return {
                    "success": False,
                    "next_phase": self.phase.name,
                    "message": error,
                    "missing_requirements": self.exploration._missing_requirements,
                    "hint": "Choose a devrag_reason that matches why exploration failed.",
                }

        if not result.hypotheses:
            return {
                "success": False,
                "next_phase": self.phase.name,
                "message": "At least one hypothesis is required from devrag results.",
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

        v3.3: evidence は構造化必須。

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
            "decision_log": self.decision_log,  # v3.5
            "query_frame": self.query_frame.to_dict() if self.query_frame else None,  # v3.6
            "risk_level": self.risk_level,  # v3.6
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
        repo_path: str = ".",  # v3.8: プロジェクトパス
    ) -> SessionState:
        """
        Create a new session.

        v3.8: repo_path を追加。agreements と learned_pairs の保存先を指定。
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
