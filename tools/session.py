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


def extract_extensions_from_text(text: str) -> list[str]:
    """
    テキストからファイル拡張子を抽出。

    Args:
        text: 検索対象のテキスト（例: "sampleフォルダにhello worldのHTML"）

    Returns:
        見つかった拡張子のリスト（例: [".html"]）
    """
    import re
    extensions = []

    # パターン1: 明示的なファイル名（例: "hello.html", "style.css"）
    file_pattern = r'\b[\w\-]+(\.[a-zA-Z]{2,5})\b'
    for match in re.finditer(file_pattern, text):
        ext = match.group(1).lower()
        extensions.append(ext)

    # パターン2: ファイルタイプの言及（例: "HTMLを作成", "CSSファイル"）
    # 日本語と英語の両方に対応（単語境界 \b は日本語で機能しないため、パターンを調整）
    type_mappings = {
        r'(?i)html': '.html',
        r'(?i)(?<![a-z])htm(?![a-z])': '.htm',
        r'(?i)css': '.css',
        r'(?i)scss': '.scss',
        r'(?i)sass': '.sass',
        r'(?i)less(?![a-z])': '.less',  # "unless" などを除外
        r'(?i)(?<![a-z])xml(?![a-z])': '.xml',
        r'(?i)(?<![a-z])svg(?![a-z])': '.svg',
        r'(?i)markdown': '.md',
        r'(?i)(?<![a-z])md(?![a-z])': '.md',
    }
    for pattern, ext in type_mappings.items():
        if re.search(pattern, text):
            if ext not in extensions:
                extensions.append(ext)

    return extensions


def is_markup_context(files: list[str], target_files: list[str] | None = None) -> bool:
    """
    探索済みファイルがすべてマークアップ系かを判定。

    Args:
        files: 探索済みファイルのリスト
        target_files: 新規作成予定のファイルリスト（任意）

    Returns:
        True if all files are markup files (or directories with markup targets)

    判定ロジック:
    1. target_files が指定されている場合、それらの拡張子をチェック
    2. files にディレクトリ（拡張子なし or "/" で終わる）が含まれる場合:
       - target_files があれば、そちらで判定
       - なければ、ディレクトリは無視して他のファイルで判定
    3. すべてのファイルがマークアップ拡張子を持つ場合 True
    """
    if not files and not target_files:
        return False

    # target_files が指定されている場合、そちらを優先
    if target_files:
        for f in target_files:
            ext = Path(f).suffix.lower()
            if ext and ext not in MARKUP_EXTENSIONS:
                return False
        return True

    # files のみで判定
    has_markup_file = False
    for f in files:
        # ディレクトリパスをスキップ（拡張子なし or "/" で終わる）
        if f.endswith("/") or f.endswith("\\"):
            continue
        ext = Path(f).suffix.lower()
        if not ext:
            # 拡張子なしのパス（ディレクトリの可能性）はスキップ
            continue
        if ext not in MARKUP_EXTENSIONS:
            return False
        has_markup_file = True

    # マークアップファイルが1つ以上あるか、すべてディレクトリの場合は False
    return has_markup_file


class Phase(Enum):
    """Execution phases in order."""
    EXPLORATION = auto()      # Phase 1: code-intel required
    SEMANTIC = auto()         # Phase 2: semantic search allowed (if needed)
    VERIFICATION = auto()     # Phase 3: verify semantic hypotheses
    IMPACT_ANALYSIS = auto()  # Phase 4: analyze impact before implementation (v1.1)
    READY = auto()            # Phase 5: implementation allowed
    PRE_COMMIT = auto()       # Phase 6: garbage detection before commit (v1.2)
    QUALITY_REVIEW = auto()   # Phase 7: quality check before merge (v1.5)


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

# =============================================================================
# Gate Level Requirements (v1.2)
# =============================================================================

# Gate level determines exploration thoroughness requirements
# "none" skips exploration entirely (handled in create_session)

GATE_LEVEL_REQUIREMENTS = {
    "high": {
        # Strictest requirements - comprehensive exploration
        "symbols_identified": 5,
        "entry_points": 2,
        "files_analyzed": 4,
        "existing_patterns": 2,
        "required_tools": {"find_definitions", "find_references", "search_text"},
    },
    "middle": {
        # Standard requirements
        "symbols_identified": 3,
        "entry_points": 1,
        "files_analyzed": 2,
        "existing_patterns": 1,
        "required_tools": {"find_definitions", "find_references"},
    },
    "low": {
        # Minimal requirements
        "symbols_identified": 1,
        "entry_points": 0,
        "files_analyzed": 1,
        "existing_patterns": 0,
        "required_tools": {"find_definitions"},
    },
    "auto": None,  # Determined by server based on risk_level
}


def evaluate_exploration(
    result: "ExplorationResult",
    intent: str,
    gate_level: str = "high",
) -> tuple[str, list[str]]:
    """
    サーバー側で confidence を算出する。

    LLM の自己申告ではなく、成果物から機械的に判定。

    Args:
        result: 探索結果
        intent: IMPLEMENT, MODIFY, INVESTIGATE, QUESTION
        gate_level: v1.2 gate level (high, middle, low, auto, none)
                   "none" should not reach here (skipped in create_session)

    Returns:
        (confidence, missing_requirements)
    """
    missing = []
    tools_used = set(result.tools_used)

    # v1.2: Gate level determines requirements
    if gate_level in GATE_LEVEL_REQUIREMENTS and GATE_LEVEL_REQUIREMENTS[gate_level] is not None:
        reqs = GATE_LEVEL_REQUIREMENTS[gate_level]
    elif gate_level == "auto":
        # Auto mode: use old behavior based on intent
        if intent in ("IMPLEMENT", "MODIFY"):
            reqs = STRICT_EXPLORATION_REQUIREMENTS
        else:
            reqs = MIN_EXPLORATION_REQUIREMENTS
    else:
        # Fallback to old behavior (high by default)
        if intent in ("IMPLEMENT", "MODIFY"):
            reqs = STRICT_EXPLORATION_REQUIREMENTS
        else:
            reqs = MIN_EXPLORATION_REQUIREMENTS

    # Check each requirement
    if len(result.symbols_identified) < reqs["symbols_identified"]:
        missing.append(f"symbols_identified: {len(result.symbols_identified)}/{reqs['symbols_identified']}")

    if reqs.get("entry_points", 0) > 0:
        if len(result.entry_points) < reqs["entry_points"]:
            missing.append(f"entry_points: {len(result.entry_points)}/{reqs['entry_points']}")

    if len(result.files_analyzed) < reqs["files_analyzed"]:
        missing.append(f"files_analyzed: {len(result.files_analyzed)}/{reqs['files_analyzed']}")

    if not reqs["required_tools"].issubset(tools_used):
        missing_tools = reqs["required_tools"] - tools_used
        missing.append(f"required_tools: missing {missing_tools}")

    # Check existing_patterns if required
    if reqs.get("existing_patterns", 0) > 0:
        if len(result.existing_patterns) < reqs["existing_patterns"]:
            missing.append(f"existing_patterns: {len(result.existing_patterns)}/{reqs['existing_patterns']}")

    confidence = "high" if not missing else "low"
    return confidence, missing


def can_proceed_to_ready(
    result: "ExplorationResult",
    intent: str,
    gate_level: str = "high",
) -> tuple[bool, list[str]]:
    """
    IMPLEMENT/MODIFY は最低成果条件を満たさないと READY に進めない。

    Args:
        result: 探索結果
        intent: IMPLEMENT, MODIFY, INVESTIGATE, QUESTION
        gate_level: v1.2 gate level (high, middle, low, auto, none)

    Returns:
        (can_proceed, missing_requirements)
    """
    # gate_level="none" should not reach here (already at READY)
    if gate_level == "none":
        return True, []

    if intent not in ("IMPLEMENT", "MODIFY"):
        # INVESTIGATE can proceed with minimal exploration
        return True, []

    confidence, missing = evaluate_exploration(result, intent, gate_level)
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
    gate_level: str = "auto",
) -> tuple[str, list[str]]:
    """
    リスクレベルを考慮した成果評価。

    v1.1: マークアップファイルのみの場合は要件を緩和。
    v1.2: gate_level で明示的に要件レベルを指定可能。

    Args:
        result: 探索結果
        intent: IMPLEMENT, MODIFY, INVESTIGATE, QUESTION
        risk_level: HIGH, MEDIUM, LOW
        query_frame: QueryFrame（スロット証拠チェック用）
        gate_level: v1.2 gate level (high, middle, low, auto, none)
                   "auto" uses risk_level for determination

    Returns:
        (confidence, missing_requirements)
    """
    missing = []
    tools_used = set(result.tools_used)

    # v1.2: gate_level が指定されている場合はそれを優先
    if gate_level != "auto" and gate_level in GATE_LEVEL_REQUIREMENTS:
        if GATE_LEVEL_REQUIREMENTS[gate_level] is not None:
            reqs = GATE_LEVEL_REQUIREMENTS[gate_level].copy()
            # 必須スロットチェック用のフィールドを追加（互換性のため）
            reqs["required_slot_evidence"] = []
        else:
            # none は来ないはず（create_session で READY にスキップ）
            reqs = get_dynamic_requirements(risk_level, intent)
    else:
        # auto or fallback: 従来の risk_level ベースの要件を使用
        reqs = get_dynamic_requirements(risk_level, intent)

    # v1.1: マークアップコンテキストの判定
    # v1.2: QueryFrame の target_feature からも拡張子を抽出して判定
    markup_context = is_markup_context(result.files_analyzed)

    # files_analyzed だけでは判定できない場合、target_feature からヒントを得る
    if not markup_context and query_frame and query_frame.target_feature:
        inferred_extensions = extract_extensions_from_text(query_frame.target_feature)
        if inferred_extensions:
            # すべての推定拡張子がマークアップ系かチェック
            all_markup = all(ext in MARKUP_EXTENSIONS for ext in inferred_extensions)
            if all_markup:
                markup_context = True

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
    # v1.2: gate_level 指定時は reqs["required_tools"] を使用
    if markup_context:
        # マークアップの場合: search_text があればOK
        required_tools = {"search_text"}
    elif "required_tools" in reqs:
        # gate_level で指定された必須ツールを使用
        required_tools = reqs["required_tools"]
    else:
        # フォールバック: 従来通り
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
# v1.1: Impact Analysis Result
# =============================================================================

@dataclass
class VerifiedFile:
    """A file that was verified during impact analysis."""
    file: str
    status: str  # will_modify, no_change_needed, not_affected
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass
class ImpactAnalysisResult:
    """Result of impact analysis phase."""
    target_files: list[str] = field(default_factory=list)
    must_verify: list[str] = field(default_factory=list)
    should_verify: list[str] = field(default_factory=list)
    verified_files: list[VerifiedFile] = field(default_factory=list)
    inferred_from_rules: list[str] = field(default_factory=list)
    mode: str = "standard"  # standard or relaxed_markup

    def to_dict(self) -> dict:
        return {
            "target_files": self.target_files,
            "must_verify": self.must_verify,
            "should_verify": self.should_verify,
            "verified_files": [v.to_dict() for v in self.verified_files],
            "inferred_from_rules": self.inferred_from_rules,
            "mode": self.mode,
        }


# =============================================================================
# v1.2: Pre-Commit Review Result (Garbage Detection)
# =============================================================================

@dataclass
class ReviewedFile:
    """A file reviewed during PRE_COMMIT phase."""
    path: str
    decision: str  # keep, discard
    reason: str | None = None  # Required if discard
    change_type: str = "modified"  # added, modified, deleted

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "decision": self.decision,
            "reason": self.reason,
            "change_type": self.change_type,
        }


@dataclass
class PreCommitReviewResult:
    """Result of PRE_COMMIT garbage detection phase."""
    total_changes: int = 0
    reviewed_files: list[ReviewedFile] = field(default_factory=list)
    kept_files: list[str] = field(default_factory=list)
    discarded_files: list[str] = field(default_factory=list)
    review_notes: str = ""

    def to_dict(self) -> dict:
        return {
            "total_changes": self.total_changes,
            "reviewed_files": [f.to_dict() for f in self.reviewed_files],
            "kept_files": self.kept_files,
            "discarded_files": self.discarded_files,
            "review_notes": self.review_notes,
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
    impact_analysis: ImpactAnalysisResult | None = None  # v1.1
    pre_commit_review: PreCommitReviewResult | None = None  # v1.2

    # v1.2: Task branch management (renamed from overlay in v1.2.1)
    task_branch_name: str | None = None  # Git branch name (llm_task_{session_id}_from_{base})
    task_branch_enabled: bool = False  # Whether task branch is active

    # v1.2: Gate level for exploration phases
    _gate_level: str = field(default="high", init=False)  # high, middle, low, auto, none

    # Semantic search results (Forest/Map architecture)
    map_results: list[dict] = field(default_factory=list)  # 地図検索結果
    forest_results: list[dict] = field(default_factory=list)  # 森検索結果
    map_hit: bool = False  # 地図でヒットしたか（Short-circuit用）

    # Tracking
    tool_calls: list[dict] = field(default_factory=list)
    phase_history: list[dict] = field(default_factory=list)

    # v1.4: Intervention System
    verification_failure_count: int = 0  # POST_IMPLEMENTATION_VERIFICATION failures
    intervention_count: int = 0  # Number of interventions triggered
    failure_history: list[dict] = field(default_factory=list)  # History of failures for analysis

    # v1.5: Quality Review
    quality_revert_count: int = 0  # Number of reverts from QUALITY_REVIEW to READY
    quality_review_enabled: bool = True  # Whether quality review is enabled (--no-quality sets this to False)
    quality_review_max_revert: int = 3  # Max revert count before forced completion
    quality_review_completed: bool = False  # Whether quality review passed (issues_found=false)

    # v1.8: PRE_COMMIT + QUALITY_REVIEW Order Change
    commit_prepared: bool = False  # Whether commit is prepared (waiting for quality review)
    prepared_commit_message: str | None = None  # Commit message for prepared commit
    prepared_kept_files: list[str] = field(default_factory=list)  # Files to keep for prepared commit
    prepared_discarded_files: list[str] = field(default_factory=list)  # Files to discard for prepared commit

    # v1.8: Only Explore Mode
    skip_implementation: bool = False  # Whether to skip implementation phase (--only-explore sets this to True)

    # v1.7: Ctags Performance Optimization
    definitions_cache: dict[tuple[str, str, str | None, bool], dict] = field(default_factory=dict)
    cache_stats: dict[str, int] = field(default_factory=lambda: {"hits": 0, "misses": 0})

    @property
    def gate_level(self) -> str:
        """Get gate level for exploration phases."""
        return self._gate_level

    @gate_level.setter
    def gate_level(self, value: str) -> None:
        """Set gate level for exploration phases."""
        self._gate_level = value

    def record_tool_call_start(self, tool: str, params: dict) -> None:
        """
        Record tool call start (v1.8: Performance tracking).

        Args:
            tool: Tool name
            params: Tool parameters
        """
        now = datetime.now()
        record = {
            "tool": tool,
            "params": params,
            "phase": self.phase.name,
            "started_at": now.isoformat(),
            # completed_at will be added by record_tool_call_end
        }
        self.tool_calls.append(record)

    def record_tool_call_end(
        self,
        result_summary: str,
        result_detail: dict | None = None,
    ) -> None:
        """
        Record tool call completion (v1.8: Performance tracking).

        Args:
            result_summary: Summary of result
            result_detail: Detailed result (optional)
        """
        if not self.tool_calls:
            return

        now = datetime.now()
        last_call = self.tool_calls[-1]
        last_call["completed_at"] = now.isoformat()
        last_call["result_summary"] = result_summary

        # Calculate execution time
        if "started_at" in last_call:
            started = datetime.fromisoformat(last_call["started_at"])
            last_call["duration_seconds"] = (now - started).total_seconds()

        if result_detail:
            last_call["result_detail"] = result_detail

    def record_tool_call(
        self,
        tool: str,
        params: dict,
        result_summary: str,
        result_detail: dict | None = None,
    ) -> None:
        """
        Record a tool call for tracking (legacy method for backward compatibility).

        DEPRECATED: Use record_tool_call_start/end instead for performance tracking.

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

    def transition_to_phase(self, new_phase: Phase, reason: str = "") -> None:
        """
        Transition to a new phase with timestamp tracking (v1.8: Performance tracking).

        Args:
            new_phase: The phase to transition to
            reason: Reason for transition (e.g., "submit_understanding", "gate_requirement")
        """
        now = datetime.now()

        # Record end time for current phase
        if self.phase_history:
            last_phase = self.phase_history[-1]
            if "ended_at" not in last_phase:
                last_phase["ended_at"] = now.isoformat()
                if "started_at" in last_phase:
                    started = datetime.fromisoformat(last_phase["started_at"])
                    last_phase["duration_seconds"] = (now - started).total_seconds()

        # Transition to new phase
        old_phase = self.phase
        self.phase = new_phase

        # Record new phase start
        self.phase_history.append({
            "phase": new_phase.name,
            "started_at": now.isoformat(),
            "reason": reason,
            "from_phase": old_phase.name,
            # ended_at will be added on next transition
        })

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
        elif self.phase == Phase.IMPACT_ANALYSIS:
            # v1.1: analyze_impact と探索ツールを許可
            return [
                "analyze_impact",
                "submit_impact_analysis",
                "query", "search_text", "find_definitions",
                "find_references", "analyze_structure",
            ]
        elif self.phase == Phase.READY:
            # v1.2: READY allows all tools except commit-related
            return [
                "*",  # All exploration/implementation tools
                "submit_for_review",  # Transition to PRE_COMMIT
            ]
        elif self.phase == Phase.PRE_COMMIT:
            # v1.2: Only review and finalize tools
            return [
                "review_changes",  # Get changes for review
                "finalize_changes",  # Apply reviewed changes
                "merge_to_base",  # Merge to base branch
            ]
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
        self.transition_to_phase(Phase.EXPLORATION, reason="revert_to_exploration")

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
        # v1.2: QueryFrame の target_feature からも拡張子を抽出して判定
        effective_risk_level = self.risk_level
        markup_context = is_markup_context(result.files_analyzed)

        # files_analyzed だけでは判定できない場合、target_feature からヒントを得る
        if not markup_context and self.query_frame and self.query_frame.target_feature:
            inferred_extensions = extract_extensions_from_text(self.query_frame.target_feature)
            if inferred_extensions:
                all_markup = all(ext in MARKUP_EXTENSIONS for ext in inferred_extensions)
                if all_markup:
                    markup_context = True

        if markup_context and self.risk_level == "HIGH":
            effective_risk_level = "LOW"

        # v3.6: リスクレベルを考慮した成果評価
        # v1.2: gate_level も渡す
        confidence, missing = evaluate_exploration_v36(
            result,
            self.intent,
            effective_risk_level,
            self.query_frame,
            self._gate_level,
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

        # IMPLEMENT/MODIFY は最低成果条件を満たさないと IMPACT_ANALYSIS に進めない
        can_proceed = confidence == "high" and not consistency_errors and not hypothesis_block

        if can_proceed:
            # v1.1: READY ではなく IMPACT_ANALYSIS へ
            self.transition_to_phase(Phase.IMPACT_ANALYSIS, reason="submit_understanding_approved")
            response = {
                "success": True,
                "next_phase": Phase.IMPACT_ANALYSIS.name,
                "evaluated_confidence": confidence,
                "risk_level": effective_risk_level,
                "message": "Exploration sufficient. Proceed to impact analysis before implementation.",
                "next_step": "Call analyze_impact with target files, then submit_impact_analysis with verified_files.",
            }
            # v1.1: マークアップコンテキストの場合は明示
            if markup_context:
                response["markup_context"] = True
                response["relaxed_requirements"] = "find_definitions/find_references not required for markup files"
            return response
        else:
            self.transition_to_phase(Phase.SEMANTIC, reason="semantic_gate_required")
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

        self.transition_to_phase(Phase.VERIFICATION, reason="submit_semantic")
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

        # v1.1: READY ではなく IMPACT_ANALYSIS へ
        self.transition_to_phase(Phase.IMPACT_ANALYSIS, reason="submit_verification")

        rejected = [v for v in result.verified if v.status == "rejected"]
        if rejected:
            return {
                "success": True,
                "next_phase": Phase.IMPACT_ANALYSIS.name,
                "message": "Verification complete. Proceed to impact analysis.",
                "warning": f"{len(rejected)} hypotheses were rejected. Do NOT implement based on rejected hypotheses.",
                "rejected": [r.to_dict() for r in rejected],
                "next_step": "Call analyze_impact with target files, then submit_impact_analysis with verified_files.",
            }

        return {
            "success": True,
            "next_phase": Phase.IMPACT_ANALYSIS.name,
            "message": "All hypotheses verified. Proceed to impact analysis before implementation.",
            "next_step": "Call analyze_impact with target files, then submit_impact_analysis with verified_files.",
        }

    def set_impact_analysis_context(
        self,
        target_files: list[str],
        must_verify: list[str],
        should_verify: list[str],
        mode: str = "standard",
    ) -> None:
        """
        v1.1: Store analyze_impact result for validation in submit_impact_analysis.

        Called by analyze_impact tool handler.
        """
        self.impact_analysis = ImpactAnalysisResult(
            target_files=target_files,
            must_verify=must_verify,
            should_verify=should_verify,
            mode=mode,
        )

    def submit_impact_analysis(
        self,
        verified_files: list[dict],
        inferred_from_rules: list[str] | None = None,
    ) -> dict:
        """
        v1.1: Submit impact analysis results and move to READY.

        Validates that all must_verify files have responses.

        Returns: {"success": bool, "next_phase": str, "message": str}
        """
        if self.phase != Phase.IMPACT_ANALYSIS:
            return {
                "success": False,
                "next_phase": self.phase.name,
                "message": f"Cannot submit impact analysis in phase {self.phase.name}",
            }

        if self.impact_analysis is None:
            return {
                "success": False,
                "next_phase": self.phase.name,
                "message": "Must call analyze_impact before submit_impact_analysis.",
            }

        # Convert verified_files to VerifiedFile objects
        verified = []
        for vf in verified_files:
            verified.append(VerifiedFile(
                file=vf["file"],
                status=vf["status"],
                reason=vf.get("reason"),
            ))

        # Validate: all must_verify files must have a response
        verified_paths = {v.file for v in verified}
        missing_must_verify = []
        for must_file in self.impact_analysis.must_verify:
            if must_file not in verified_paths:
                missing_must_verify.append(must_file)

        if missing_must_verify:
            return {
                "success": False,
                "next_phase": self.phase.name,
                "message": "Not all must_verify files have been verified.",
                "missing_must_verify": missing_must_verify,
                "hint": "Provide status for all must_verify files before proceeding.",
            }

        # Validate: status != will_modify requires reason
        missing_reasons = []
        for v in verified:
            if v.status != "will_modify" and not v.reason:
                missing_reasons.append(v.file)

        if missing_reasons:
            return {
                "success": False,
                "next_phase": self.phase.name,
                "message": "Files with status != will_modify require a reason.",
                "missing_reasons": missing_reasons,
            }

        # Update impact_analysis result
        self.impact_analysis.verified_files = verified
        self.impact_analysis.inferred_from_rules = inferred_from_rules or []

        # Record phase transition
        self.phase_history.append({
            "from": Phase.IMPACT_ANALYSIS.name,
            "result": self.impact_analysis.to_dict(),
            "timestamp": datetime.now().isoformat(),
        })

        # v1.8: If skip_implementation is enabled, do not transition to READY
        if self.skip_implementation:
            # Keep phase as IMPACT_ANALYSIS (or could transition to a terminal phase if needed)
            # Return exploration complete message
            response = {
                "success": True,
                "next_phase": Phase.IMPACT_ANALYSIS.name,
                "exploration_complete": True,
                "message": "Exploration complete. Implementation skipped (--only-explore mode).",
                "verified_count": len(verified),
                "will_modify": [v.file for v in verified if v.status == "will_modify"],
            }

            # Check for should_verify warnings
            should_verify_missing = []
            for should_file in self.impact_analysis.should_verify:
                if should_file not in verified_paths:
                    should_verify_missing.append(should_file)

            if should_verify_missing:
                response["warning"] = f"Some should_verify files were not verified: {should_verify_missing}"

            return response

        # Normal flow: transition to READY
        self.transition_to_phase(Phase.READY, reason="submit_impact_analysis")

        # Check for should_verify warnings
        should_verify_missing = []
        for should_file in self.impact_analysis.should_verify:
            if should_file not in verified_paths:
                should_verify_missing.append(should_file)

        response = {
            "success": True,
            "next_phase": Phase.READY.name,
            "message": "Impact analysis complete. Ready for implementation.",
            "verified_count": len(verified),
            "will_modify": [v.file for v in verified if v.status == "will_modify"],
        }

        if should_verify_missing:
            response["warning"] = f"Some should_verify files were not verified: {should_verify_missing}"

        return response

    def submit_for_review(self) -> dict:
        """
        v1.2: Transition from READY to PRE_COMMIT for garbage detection.

        This should be called after implementation is complete.

        Returns: {"success": bool, "next_phase": str, "message": str}
        """
        if self.phase != Phase.READY:
            return {
                "success": False,
                "next_phase": self.phase.name,
                "message": f"Cannot submit for review in phase {self.phase.name}. Must be in READY phase.",
            }

        if not self.task_branch_enabled:
            return {
                "success": False,
                "next_phase": self.phase.name,
                "message": "Task branch not enabled. submit_for_review requires task branch to be active.",
            }

        # Record phase transition
        self.phase_history.append({
            "from": Phase.READY.name,
            "to": Phase.PRE_COMMIT.name,
            "timestamp": datetime.now().isoformat(),
        })

        self.transition_to_phase(Phase.PRE_COMMIT, reason="submit_for_review")

        return {
            "success": True,
            "next_phase": Phase.PRE_COMMIT.name,
            "message": "Implementation complete. Now in PRE_COMMIT phase for garbage detection.",
            "next_step": "Call review_changes to get all changes, then finalize_changes with decisions.",
        }

    def submit_pre_commit_review(
        self,
        reviewed_files: list[dict],
        review_notes: str = "",
    ) -> dict:
        """
        v1.2: Submit garbage detection review results.

        This validates that all changes have been reviewed.

        Args:
            reviewed_files: List of {"path": str, "decision": "keep"|"discard", "reason": str}
            review_notes: Optional notes about the review

        Returns: {"success": bool, "kept_files": list, "discarded_files": list}
        """
        if self.phase != Phase.PRE_COMMIT:
            return {
                "success": False,
                "message": f"Cannot submit review in phase {self.phase.name}. Must be in PRE_COMMIT phase.",
            }

        # Convert to ReviewedFile objects
        reviewed = []
        kept = []
        discarded = []

        for rf in reviewed_files:
            decision = rf.get("decision", "keep")
            path = rf["path"]

            # Validate: discard requires reason
            if decision == "discard" and not rf.get("reason"):
                return {
                    "success": False,
                    "message": f"Discarding '{path}' requires a reason.",
                }

            reviewed.append(ReviewedFile(
                path=path,
                decision=decision,
                reason=rf.get("reason"),
                change_type=rf.get("change_type", "modified"),
            ))

            if decision == "keep":
                kept.append(path)
            else:
                discarded.append(path)

        # Store result
        self.pre_commit_review = PreCommitReviewResult(
            total_changes=len(reviewed),
            reviewed_files=reviewed,
            kept_files=kept,
            discarded_files=discarded,
            review_notes=review_notes,
        )

        # Record phase transition
        self.phase_history.append({
            "from": Phase.PRE_COMMIT.name,
            "result": self.pre_commit_review.to_dict(),
            "timestamp": datetime.now().isoformat(),
        })

        return {
            "success": True,
            "kept_files": kept,
            "discarded_files": discarded,
            "message": f"Review complete. {len(kept)} files to keep, {len(discarded)} files to discard.",
            "next_step": "Call finalize_changes to apply decisions and commit.",
        }

    def get_status(self) -> dict:
        """Get current session status."""
        status = {
            "session_id": self.session_id,
            "intent": self.intent,
            "query": self.query,
            "current_phase": self.phase.name,
            "allowed_tools": self.get_allowed_tools(),
            "exploration": self.exploration.to_dict() if self.exploration else None,
            "semantic": self.semantic.to_dict() if self.semantic else None,
            "verification": self.verification.to_dict() if self.verification else None,
            "tool_calls_count": len(self.tool_calls),
            # v1.8: Performance tracking data
            "tool_calls": self.tool_calls,  # Includes started_at, completed_at, duration_seconds
            "phase_history": self.phase_history,  # Includes started_at, ended_at, duration_seconds
            # v1.8: Exploration-only mode flag
            "skip_implementation": self.skip_implementation,
        }

        # v1.2: Add task branch info if enabled
        if self.task_branch_enabled:
            status["task_branch"] = {
                "enabled": True,
                "branch": self.task_branch_name,
            }

        # v1.2: Add PRE_COMMIT info if available
        if self.pre_commit_review:
            status["pre_commit_review"] = self.pre_commit_review.to_dict()

        return status

    # =========================================================================
    # v1.4: Intervention System Methods
    # =========================================================================

    def record_verification_failure(self, failure_info: dict) -> dict:
        """
        Record a verification failure for intervention tracking.

        Args:
            failure_info: {
                "phase": str,  # Phase where failure occurred (e.g., "POST_IMPLEMENTATION_VERIFICATION")
                "error_message": str,  # What differed from expectation
                "problem_location": str,  # Where the problem was found
                "observed_values": str,  # Actual values observed
                "attempt_number": int,  # Which attempt this was
            }

        Returns:
            {"recorded": bool, "failure_count": int, "intervention_triggered": bool, "intervention_data": dict | None}
        """
        self.verification_failure_count += 1

        # Add to failure history
        failure_record = {
            "count": self.verification_failure_count,
            "timestamp": datetime.now().isoformat(),
            **failure_info,
        }
        self.failure_history.append(failure_record)

        # Check if intervention should be triggered (threshold = 3)
        intervention_triggered = self.verification_failure_count >= 3

        result = {
            "recorded": True,
            "failure_count": self.verification_failure_count,
            "intervention_triggered": intervention_triggered,
            "intervention_data": None,
        }

        if intervention_triggered:
            result["intervention_data"] = self._get_intervention_data()

        return result

    def _get_intervention_data(self) -> dict:
        """
        Get intervention data when threshold is reached.

        Returns data needed for LLM to select and follow intervention prompt.
        """
        # Force user_escalation after 2 interventions
        force_user_escalation = self.intervention_count >= 2

        return {
            "failure_count": self.verification_failure_count,
            "intervention_count": self.intervention_count,
            "force_user_escalation": force_user_escalation,
            "failure_history": self.failure_history[-3:],  # Last 3 failures
            "available_prompts": [
                "structure_review",  # Layout/positioning issues
                "hypothesis_review",  # Error messages changing
                "step_back",  # General stuck state
                "user_escalation",  # Escalate to user (mandatory after 2 interventions)
            ],
            "prompt_selection_guide": {
                "structure_review": "Select when layout/positioning adjustments are repeating",
                "hypothesis_review": "Select when error messages change each time",
                "step_back": "Select for general stuck state",
                "user_escalation": "MANDATORY if intervention_count >= 2, otherwise select when other interventions haven't helped",
            },
            "instructions": (
                "1. Analyze failure_history to understand the pattern\n"
                "2. Select appropriate intervention prompt from available_prompts\n"
                "3. Read the intervention prompt from .code-intel/interventions/{selected}.md\n"
                "4. Follow the instructions in the prompt\n"
                "5. If force_user_escalation is true, MUST use user_escalation.md"
            ),
        }

    def record_intervention_used(self, prompt_name: str) -> dict:
        """
        Record that an intervention prompt was used.

        Args:
            prompt_name: Name of the intervention prompt used (e.g., "step_back")

        Returns:
            {"recorded": bool, "intervention_count": int}
        """
        self.intervention_count += 1

        # Record in phase history
        self.phase_history.append({
            "action": "intervention",
            "prompt_name": prompt_name,
            "intervention_count": self.intervention_count,
            "verification_failures_at_trigger": self.verification_failure_count,
            "timestamp": datetime.now().isoformat(),
        })

        return {
            "recorded": True,
            "intervention_count": self.intervention_count,
            "message": f"Intervention '{prompt_name}' recorded. Total interventions: {self.intervention_count}",
        }

    def reset_verification_failures(self) -> dict:
        """
        Reset verification failure count after successful verification.

        Called when verification passes after intervention.

        Returns:
            {"reset": bool, "previous_count": int}
        """
        previous_count = self.verification_failure_count
        self.verification_failure_count = 0

        return {
            "reset": True,
            "previous_count": previous_count,
            "message": "Verification failure count reset after successful verification.",
        }

    def get_intervention_status(self) -> dict:
        """
        Get current intervention system status.

        Returns:
            Status of verification failures and interventions.
        """
        return {
            "verification_failure_count": self.verification_failure_count,
            "intervention_count": self.intervention_count,
            "intervention_threshold": 3,
            "force_escalation_threshold": 2,
            "near_intervention": self.verification_failure_count >= 2,
            "requires_user_escalation": self.intervention_count >= 2,
            "failure_history_count": len(self.failure_history),
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
            "impact_analysis": self.impact_analysis.to_dict() if self.impact_analysis else None,
            "pre_commit_review": self.pre_commit_review.to_dict() if self.pre_commit_review else None,
            "task_branch": {
                "enabled": self.task_branch_enabled,
                "branch": self.task_branch_name,
            } if self.task_branch_enabled else None,
            "tool_calls": self.tool_calls,
            "phase_history": self.phase_history,
            # v1.4: Intervention System
            "intervention": {
                "verification_failure_count": self.verification_failure_count,
                "intervention_count": self.intervention_count,
                "failure_history": self.failure_history,
            },
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
        gate_level: str = "high",
    ) -> SessionState:
        """
        Create a new session.

        Args:
            intent: IMPLEMENT, MODIFY, INVESTIGATE, QUESTION
            query: User's original query
            session_id: Optional session ID (auto-generated if not provided)
            repo_path: agreements と learned_pairs の保存先を指定
            gate_level: Gate level for exploration phases
                - "high": Strict requirements (default)
                - "middle": Standard requirements
                - "low": Minimal requirements
                - "auto": Server determines based on risk
                - "none": Skip exploration phases, go directly to READY
        """
        if session_id is None:
            session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        # Determine initial phase based on intent and gate_level
        if intent == "QUESTION":
            # QUESTION intent always skips to READY
            initial_phase = Phase.READY
        elif gate_level == "none":
            # --quick / -g=n: Skip exploration phases
            initial_phase = Phase.READY
        else:
            # Normal flow: Start with EXPLORATION
            initial_phase = Phase.EXPLORATION

        session = SessionState(
            session_id=session_id,
            intent=intent,
            query=query,
            phase=initial_phase,
            repo_path=repo_path,
        )

        # Store gate_level for later use (e.g., in evaluate_exploration)
        session._gate_level = gate_level

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
