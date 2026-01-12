"""
QueryFrame and QueryDecomposer for v3.6.

v3.6: 自然文の構造化
- QueryFrame: 自然文から抽出された構造化情報
- QueryDecomposer: LLMを使った構造化 + サーバー検証
- SlotSource: FACT/HYPOTHESIS の区別
- MappedSymbol: シンボルの確実性管理
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal


class SlotSource(Enum):
    """スロットがどのフェーズで確定したか"""
    FACT = "FACT"              # EXPLORATIONで確定（事実）
    HYPOTHESIS = "HYPOTHESIS"  # SEMANTICで推測（仮説）
    UNRESOLVED = "UNRESOLVED"  # 未解決


@dataclass
class SlotData:
    """
    スロットの値と引用のペア。

    LLMが抽出した値と、その根拠となるraw_queryからの引用。
    """
    value: str
    quote: str  # raw_queryからの引用（原文そのまま）

    def to_dict(self) -> dict:
        return {"value": self.value, "quote": self.quote}


@dataclass
class SlotEvidence:
    """
    スロットを埋めた証拠。

    どのツールでどのようにスロットを埋めたかを記録。
    """
    tool: str
    params: dict
    result_summary: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "params": self.params,
            "result_summary": self.result_summary,
            "timestamp": self.timestamp,
        }


@dataclass
class MappedSymbol:
    """
    マッピングされたシンボル。

    NL用語から特定されたコード上のシンボル。
    source が HYPOTHESIS の場合は VERIFICATION 必須。
    """
    name: str
    source: SlotSource
    confidence: float  # 0.0-1.0
    evidence: SlotEvidence | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source": self.source.value,
            "confidence": self.confidence,
            "evidence": self.evidence.to_dict() if self.evidence else None,
        }


@dataclass
class QueryFrame:
    """
    自然文から抽出された構造化情報。

    v3.6: 自然文の複合的な情報を4+1スロットで表現。

    Slots:
        target_feature: 対象機能・モジュール（「ログイン機能」）
        trigger_condition: 再現条件・トリガー（「XXXを入力したとき」）
        observed_issue: 観測された問題（「エラーが出る」）
        desired_action: 期待する修正（「XXXするように修正」）
        mapped_symbols: 探索で見つけたシンボル（動的に更新）
    """
    raw_query: str  # 元のクエリ（証跡）

    # 抽出されたスロット（すべてOptional）
    target_feature: str | None = None
    trigger_condition: str | None = None
    observed_issue: str | None = None
    desired_action: str | None = None

    # 探索で見つけたシンボル（動的に更新）
    mapped_symbols: list[MappedSymbol] = field(default_factory=list)

    # スロットのソース（FACT/HYPOTHESIS）
    slot_source: dict[str, SlotSource] = field(default_factory=dict)

    # スロットの証拠
    slot_evidence: dict[str, SlotEvidence] = field(default_factory=dict)

    # 抽出時の引用（検証用）
    slot_quotes: dict[str, str] = field(default_factory=dict)

    def get_missing_slots(self) -> list[str]:
        """埋まっていないスロットを返す"""
        missing = []
        if not self.target_feature:
            missing.append("target_feature")
        if not self.trigger_condition:
            missing.append("trigger_condition")
        if not self.observed_issue:
            missing.append("observed_issue")
        if not self.desired_action:
            missing.append("desired_action")
        return missing

    def get_hypothesis_slots(self) -> list[str]:
        """HYPOTHESISのままのスロットを返す"""
        return [
            slot for slot, source in self.slot_source.items()
            if source == SlotSource.HYPOTHESIS
        ]

    def get_fact_symbols(self) -> list[MappedSymbol]:
        """FACTで確定したシンボルを返す"""
        return [s for s in self.mapped_symbols if s.source == SlotSource.FACT]

    def get_hypothesis_symbols(self) -> list[MappedSymbol]:
        """HYPOTHESISのシンボルを返す"""
        return [s for s in self.mapped_symbols if s.source == SlotSource.HYPOTHESIS]

    def update_slot(
        self,
        slot_name: str,
        value: str,
        source: SlotSource,
        evidence: SlotEvidence,
    ) -> None:
        """スロットを更新（証拠必須）"""
        setattr(self, slot_name, value)
        self.slot_source[slot_name] = source
        self.slot_evidence[slot_name] = evidence

    def add_mapped_symbol(
        self,
        name: str,
        source: SlotSource,
        confidence: float,
        evidence: SlotEvidence | None = None,
    ) -> None:
        """マッピングされたシンボルを追加"""
        # 重複チェック
        existing = [s for s in self.mapped_symbols if s.name == name]
        if existing:
            # 既存のシンボルを更新（より高い確実性で上書き）
            if source == SlotSource.FACT or confidence > existing[0].confidence:
                existing[0].source = source
                existing[0].confidence = confidence
                if evidence:
                    existing[0].evidence = evidence
        else:
            self.mapped_symbols.append(MappedSymbol(
                name=name,
                source=source,
                confidence=confidence,
                evidence=evidence,
            ))

    def to_dict(self) -> dict:
        return {
            "raw_query": self.raw_query,
            "target_feature": self.target_feature,
            "trigger_condition": self.trigger_condition,
            "observed_issue": self.observed_issue,
            "desired_action": self.desired_action,
            "mapped_symbols": [s.to_dict() for s in self.mapped_symbols],
            "slot_source": {k: v.value for k, v in self.slot_source.items()},
            "slot_evidence": {k: v.to_dict() for k, v in self.slot_evidence.items()},
            "slot_quotes": self.slot_quotes,
            "missing_slots": self.get_missing_slots(),
            "hypothesis_slots": self.get_hypothesis_slots(),
        }


# =============================================================================
# Slot Validation (幻覚チェック)
# =============================================================================

def validate_slot(
    slot_name: str,
    extracted_data: dict,
    raw_query: str,
) -> tuple[str | None, str | None]:
    """
    スロットの値を検証し、幻覚を排除する。

    Args:
        slot_name: スロット名
        extracted_data: {"value": ..., "quote": ...}
        raw_query: 元のクエリ

    Returns:
        (validated_value, quote): 検証済みの値と引用、または (None, None)
    """
    value = extracted_data.get("value")
    quote = extracted_data.get("quote")

    # 値がない
    if not value:
        return None, None

    # 引用がない、または原文に含まれていない → 幻覚
    if not quote or quote not in raw_query:
        return None, None

    # 値が引用と無関係 → 幻覚
    if not _is_semantically_consistent(value, quote):
        return None, None

    return value, quote


def _is_semantically_consistent(value: str, quote: str) -> bool:
    """値が引用と意味的に一致しているか"""
    value_lower = value.lower()
    quote_lower = quote.lower()

    # 単純な包含チェック
    if value_lower in quote_lower or quote_lower in value_lower:
        return True

    # 単語レベルでの共通性チェック（英語向け）
    value_words = set(value_lower.split())
    quote_words = set(quote_lower.split())

    # 少なくとも1単語が共通していればOK
    if bool(value_words & quote_words):
        return True

    # 日本語向け: 文字レベルの重複チェック
    # 助詞（が、を、に、で、は、の、と、も）を除いた共通文字の割合
    particles = set("がをにではのとも")
    value_chars = set(value_lower) - particles
    quote_chars = set(quote_lower) - particles

    if not value_chars or not quote_chars:
        return False

    # 50%以上の文字が共通していればOK
    common = value_chars & quote_chars
    overlap_ratio = len(common) / min(len(value_chars), len(quote_chars))
    return overlap_ratio >= 0.5


# =============================================================================
# NL → Symbol 整合性チェック
# =============================================================================

# 基本的なシノニム（Phase 1: 最小限のハードコード）
BASIC_SYNONYMS: dict[str, list[str]] = {
    "ログイン": ["login", "auth", "signin", "sign_in"],
    "認証": ["auth", "authentication", "authenticate"],
    "ユーザー": ["user", "account", "member"],
    "登録": ["register", "signup", "sign_up", "create"],
    "削除": ["delete", "remove", "destroy"],
    "更新": ["update", "edit", "modify"],
    "検索": ["search", "find", "query"],
    "一覧": ["list", "index", "all"],
    "詳細": ["detail", "show", "view"],
    "設定": ["setting", "config", "preference"],
    "通知": ["notification", "notify", "alert"],
    "メール": ["email", "mail"],
    "パスワード": ["password", "pass", "pwd"],
    "トークン": ["token", "jwt", "bearer"],
    "セッション": ["session", "sess"],
    "キャッシュ": ["cache", "cached"],
    "データベース": ["database", "db"],
    "API": ["api", "endpoint", "route"],
    "エラー": ["error", "exception", "err"],
    "バリデーション": ["validation", "validate", "validator"],
}


def is_related(nl_term: str, symbol: str) -> bool:
    """
    NL用語とシンボルの関連性チェック。

    Phase 1: 部分一致 + 基本的なシノニム

    Args:
        nl_term: 自然言語の用語（「ログイン機能」）
        symbol: コード上のシンボル（「LoginController」）

    Returns:
        関連していればTrue
    """
    nl_lower = nl_term.lower()
    sym_lower = symbol.lower()

    # 1. 直接的な部分一致
    if nl_lower in sym_lower or sym_lower in nl_lower:
        return True

    # 2. 単語レベルでの部分一致
    nl_words = set(nl_lower.replace("機能", "").replace("処理", "").split())
    for word in nl_words:
        if len(word) >= 2 and word in sym_lower:
            return True

    # 3. シノニム辞書によるマッチング（日本語→英語）
    for jp_term, en_terms in BASIC_SYNONYMS.items():
        if jp_term in nl_term:
            if any(en in sym_lower for en in en_terms):
                return True

    # 4. シノニム辞書によるマッチング（英語→英語）
    # nl_term が英語シノニムの場合、同じグループの他のシノニムとマッチ
    for jp_term, en_terms in BASIC_SYNONYMS.items():
        if any(en in nl_lower for en in en_terms):
            # nl_term がシノニムグループに含まれる
            if any(en in sym_lower for en in en_terms):
                return True

    return False


def validate_nl_symbol_mapping(
    nl_term: str,
    symbols: list[str],
) -> tuple[bool, list[str]]:
    """
    NL用語に対応するシンボルが見つかっているか検証。

    Args:
        nl_term: 自然言語の用語
        symbols: 見つかったシンボルのリスト

    Returns:
        (has_match, matched_symbols)
    """
    matched = [s for s in symbols if is_related(nl_term, s)]
    return bool(matched), matched


# =============================================================================
# QueryDecomposer
# =============================================================================

class QueryDecomposer:
    """
    LLMを使って自然文をQueryFrameに分解する。

    1. LLMにスロット抽出を依頼
    2. サーバーが結果を検証（幻覚チェック）
    3. 検証済みQueryFrameを返す
    """

    @staticmethod
    def get_extraction_prompt(query: str) -> str:
        """LLMへの抽出プロンプトを生成"""
        return f'''以下のクエリから情報を抽出してください。

ルール：
- 明示されている情報のみ抽出
- 各スロットには「value」と「quote」を必ず含める
- 「quote」は raw_query からの正確な引用（原文そのまま）
- 推測・補完は禁止。不明な場合は null

出力形式（JSON）：
{{
  "target_feature": {{"value": "対象機能", "quote": "raw_queryからの引用"}} または null,
  "trigger_condition": {{"value": "再現条件", "quote": "raw_queryからの引用"}} または null,
  "observed_issue": {{"value": "問題", "quote": "raw_queryからの引用"}} または null,
  "desired_action": {{"value": "期待する修正", "quote": "raw_queryからの引用"}} または null
}}

クエリ: {query}'''

    @staticmethod
    def validate_extraction(
        raw_query: str,
        extracted: dict,
    ) -> QueryFrame:
        """
        LLMの抽出結果を検証してQueryFrameを作成。

        Args:
            raw_query: 元のクエリ
            extracted: LLMが抽出したJSON

        Returns:
            検証済みQueryFrame
        """
        frame = QueryFrame(raw_query=raw_query)

        slot_names = ["target_feature", "trigger_condition", "observed_issue", "desired_action"]

        for slot_name in slot_names:
            data = extracted.get(slot_name)
            if data and isinstance(data, dict):
                value, quote = validate_slot(slot_name, data, raw_query)
                if value:
                    setattr(frame, slot_name, value)
                    frame.slot_source[slot_name] = SlotSource.FACT
                    frame.slot_quotes[slot_name] = quote

        return frame


# =============================================================================
# Investigation Guidance (調査指示の自動生成)
# =============================================================================

INVESTIGATION_HINTS: dict[str, dict] = {
    "target_feature": {
        "hint": "対象となる機能やモジュールが特定できません。",
        "action": "get_symbols や query を使用して、関連しそうなコードの全体像を把握してください。",
        "tools": ["query", "get_symbols", "analyze_structure"],
    },
    "observed_issue": {
        "hint": "修正の動機となる『現状の問題』が不明です。",
        "action": "search_text でエラー文言やログを確認してください。",
        "tools": ["search_text", "query"],
    },
    "trigger_condition": {
        "hint": "問題が発生する再現条件が特定されていません。",
        "action": "コード内の if 文や例外処理の条件を探索してください。",
        "tools": ["search_text", "find_definitions"],
    },
    "desired_action": {
        "hint": "期待する修正方針が不明です。",
        "action": "find_references で影響範囲を把握してください。",
        "tools": ["find_references", "analyze_structure"],
    },
}


def generate_investigation_guidance(missing_slots: list[str]) -> dict:
    """
    欠損スロットに基づいて調査指示を生成。

    Args:
        missing_slots: 埋まっていないスロットのリスト

    Returns:
        {"missing_slots": [...], "hints": [...], "recommended_tools": [...]}
    """
    guidance = {
        "missing_slots": missing_slots,
        "hints": [],
        "recommended_tools": [],
    }

    for slot in missing_slots:
        if slot in INVESTIGATION_HINTS:
            info = INVESTIGATION_HINTS[slot]
            guidance["hints"].append({
                "slot": slot,
                "hint": info["hint"],
                "action": info["action"],
            })
            guidance["recommended_tools"].extend(info["tools"])

    # 重複排除
    guidance["recommended_tools"] = list(dict.fromkeys(guidance["recommended_tools"]))

    return guidance


# =============================================================================
# Risk Level Assessment
# =============================================================================

def assess_risk_level(frame: QueryFrame, intent: str) -> str:
    """
    QueryFrameに基づいてリスクレベルを判定。

    Args:
        frame: QueryFrame
        intent: IMPLEMENT, MODIFY, INVESTIGATE, QUESTION

    Returns:
        "HIGH" | "MEDIUM" | "LOW"
    """
    # desired_action があるのに observed_issue がない → 高リスク
    if frame.desired_action and not frame.observed_issue:
        return "HIGH"

    # MODIFY で target_feature が不明 → 高リスク
    if intent == "MODIFY" and not frame.target_feature:
        return "HIGH"

    # IMPLEMENT で何も埋まっていない → 高リスク
    if intent == "IMPLEMENT" and not any([
        frame.target_feature,
        frame.trigger_condition,
        frame.observed_issue,
        frame.desired_action,
    ]):
        return "HIGH"

    # observed_issue が曖昧 → 中リスク
    if frame.observed_issue and len(frame.observed_issue) < 10:
        return "MEDIUM"

    # HYPOTHESISスロットがある → 中リスク
    if frame.get_hypothesis_slots():
        return "MEDIUM"

    return "LOW"


# =============================================================================
# Dynamic Exploration Requirements
# =============================================================================

def get_exploration_requirements(risk_level: str, intent: str) -> dict:
    """
    リスクレベルに応じた成果条件を返す。

    Args:
        risk_level: "HIGH" | "MEDIUM" | "LOW"
        intent: IMPLEMENT, MODIFY, INVESTIGATE, QUESTION

    Returns:
        成果条件の辞書
    """
    base = {
        "symbols_identified": 3,
        "entry_points": 1,
        "files_analyzed": 2,
        "existing_patterns": 1,
        "required_slot_evidence": [],
    }

    if intent not in ("IMPLEMENT", "MODIFY"):
        # INVESTIGATE は緩い条件
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


# =============================================================================
# Validation for READY transition
# =============================================================================

def validate_for_ready(frame: QueryFrame) -> list[str]:
    """
    READYフェーズに進めるか検証。

    HYPOTHESISスロット/シンボルが残っていればブロック。

    Returns:
        エラーメッセージのリスト（空なら通過）
    """
    errors = []

    # HYPOTHESISスロットのチェック
    for slot, source in frame.slot_source.items():
        if source == SlotSource.HYPOTHESIS:
            errors.append(
                f"Slot '{slot}' is still HYPOTHESIS. "
                f"Must verify with code intel tools first."
            )

    # HYPOTHESISシンボルのチェック
    hypothesis_symbols = frame.get_hypothesis_symbols()
    if hypothesis_symbols:
        names = [s.name for s in hypothesis_symbols]
        errors.append(
            f"Symbols {names} are still HYPOTHESIS. "
            f"Must verify with code intel tools first."
        )

    return errors
