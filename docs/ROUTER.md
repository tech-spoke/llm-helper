# Router 設計ドキュメント v3.6

## 概要

Routerは、ユーザークエリを解析し、適切なツールを選択・実行する中央コンポーネントです。
「LLMに判断をさせない」原則に基づき、パターンマッチングと閾値ベースの判定を採用しています。

### v3.6 の主要変更

- **QueryFrame**: 自然文を4+1スロットで構造化
- **QueryDecomposer**: LLMが抽出 → サーバーが検証（Quote-based validation）
- **risk_level**: HIGH/MEDIUM/LOW で動的に探索要件を決定
- **slot_source**: FACT（探索で確定）vs HYPOTHESIS（devragで推測）
- **NL→Symbol整合性**: 自然言語表現とコードシンボルの対応を検証

---

## アーキテクチャ

```
┌────────────────────────────────────────────────────────────────┐
│                          Router                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐ │
│  │QueryClassifier│  │ ToolSelector │  │  FallbackDecider     │ │
│  └──────┬───────┘  └──────┬───────┘  └────────┬─────────────┘ │
│         │                 │                    │               │
│         ▼                 ▼                    ▼               │
│  ┌────────────────────────────────────────────────────────┐   │
│  │                    create_plan()                        │   │
│  └────────────────────────────────────────────────────────┘   │
│                           │                                    │
│  ┌────────────────────────┼────────────────────────┐          │
│  ▼                        ▼                        ▼          │
│  ┌────────────┐   ┌────────────────┐   ┌────────────────┐    │
│  │SessionBoot-│   │ ResultIntegra- │   │  DecisionLog   │    │
│  │   strap    │   │     tor        │   │   (v3.6)       │    │
│  └────────────┘   └────────────────┘   └────────────────┘    │
│                                                                │
│  v3.6 追加コンポーネント                                        │
│  ┌────────────────┐   ┌────────────────┐                     │
│  │  QueryFrame    │   │ QueryDecomposer│                     │
│  └────────────────┘   └────────────────┘                     │
└────────────────────────────────────────────────────────────────┘
```

---

## v3.6 新コンポーネント

### 1. QueryFrame（クエリフレーム）

自然文から抽出された構造化情報。4+1スロットで表現。

| スロット | 説明 | 例 |
|----------|------|-----|
| `target_feature` | 対象機能・モジュール | 「ログイン機能」 |
| `trigger_condition` | 再現条件・トリガー | 「パスワードが空のとき」 |
| `observed_issue` | 観測された問題 | 「エラーが出ない」 |
| `desired_action` | 期待する修正 | 「チェックを追加」 |
| `mapped_symbols` | 探索で見つけたシンボル | `["LoginService", "AuthController"]` |

```python
@dataclass
class QueryFrame:
    raw_query: str  # 元のクエリ（証跡）

    # 4つのスロット（すべてOptional）
    target_feature: str | None = None
    trigger_condition: str | None = None
    observed_issue: str | None = None
    desired_action: str | None = None

    # 動的に更新されるシンボル
    mapped_symbols: list[MappedSymbol] = field(default_factory=list)

    # スロットのソース（FACT/HYPOTHESIS）
    slot_source: dict[str, SlotSource] = field(default_factory=dict)

    # スロットの証拠
    slot_evidence: dict[str, SlotEvidence] = field(default_factory=dict)

    # 抽出時の引用（検証用）
    slot_quotes: dict[str, str] = field(default_factory=dict)
```

#### SlotSource（スロットソース）

スロットがどのフェーズで確定したかを示す：

```python
class SlotSource(Enum):
    FACT = "FACT"              # EXPLORATIONで確定（事実）
    HYPOTHESIS = "HYPOTHESIS"  # SEMANTICで推測（仮説）
    UNRESOLVED = "UNRESOLVED"  # 未解決
```

**重要**: `HYPOTHESIS` のスロットは `VERIFICATION` フェーズで検証が必須。

---

### 2. QueryDecomposer（クエリ分解器）

LLMを使って自然文をQueryFrameに分解し、サーバーが検証する。

#### 抽出プロンプト

```python
@staticmethod
def get_extraction_prompt(query: str) -> str:
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
```

#### Quote検証（幻覚防止）

LLMが抽出した `quote` が元のクエリに存在するかサーバーが検証：

```python
def validate_slot(slot_name: str, extracted_data: dict, raw_query: str) -> tuple[str | None, str | None]:
    """
    スロットの値を検証し、幻覚を排除する。
    """
    value = extracted_data.get("value")
    quote = extracted_data.get("quote")

    # 引用がない、または原文に含まれていない → 幻覚
    if not quote or quote not in raw_query:
        return None, None

    # 値が引用と無関係 → 幻覚
    if not _is_semantically_consistent(value, quote):
        return None, None

    return value, quote
```

---

### 3. risk_level（リスクレベル）

QueryFrameとIntentに基づいてリスクを評価し、探索要件を動的に決定：

| Level | 条件 | 探索要件 |
|-------|------|----------|
| HIGH | MODIFY + issue不明、action有りissue無し | symbols: 5, entry_points: 2, files: 4, patterns: 2 |
| MEDIUM | IMPLEMENT、または部分不明 | symbols: 3, entry_points: 1, files: 2, patterns: 1 |
| LOW | INVESTIGATE、または全情報あり | symbols: 1, entry_points: 0, files: 1, patterns: 0 |

```python
def assess_risk_level(frame: QueryFrame, intent: str) -> str:
    # desired_action があるのに observed_issue がない → 高リスク
    if frame.desired_action and not frame.observed_issue:
        return "HIGH"

    # MODIFY で target_feature が不明 → 高リスク
    if intent == "MODIFY" and not frame.target_feature:
        return "HIGH"

    # IMPLEMENT で何も埋まっていない → 高リスク
    if intent == "IMPLEMENT" and not any([
        frame.target_feature, frame.trigger_condition,
        frame.observed_issue, frame.desired_action,
    ]):
        return "HIGH"

    return "LOW"
```

---

### 4. NL→Symbol整合性チェック

自然言語の表現とコードシンボルの対応を検証：

```python
# 基本的なシノニム辞書
BASIC_SYNONYMS = {
    "ログイン": ["login", "auth", "signin"],
    "認証": ["auth", "authentication"],
    "ユーザー": ["user", "account", "member"],
    # ...
}

def is_related(nl_term: str, symbol: str) -> bool:
    """NL用語とシンボルの関連性チェック"""
    nl_lower = nl_term.lower()
    sym_lower = symbol.lower()

    # 1. 直接的な部分一致
    if nl_lower in sym_lower or sym_lower in nl_lower:
        return True

    # 2. シノニム辞書によるマッチング
    for jp_term, en_terms in BASIC_SYNONYMS.items():
        if jp_term in nl_term:
            if any(en in sym_lower for en in en_terms):
                return True

    return False
```

---

### 5. Investigation Guidance（調査指示）

欠損スロットに基づいてツールを推奨：

```python
INVESTIGATION_HINTS = {
    "target_feature": {
        "hint": "対象となる機能やモジュールが特定できません。",
        "tools": ["query", "get_symbols", "analyze_structure"],
    },
    "observed_issue": {
        "hint": "修正の動機となる『現状の問題』が不明です。",
        "tools": ["search_text", "query"],
    },
    "trigger_condition": {
        "hint": "問題が発生する再現条件が特定されていません。",
        "tools": ["search_text", "find_definitions"],
    },
    "desired_action": {
        "hint": "期待する修正方針が不明です。",
        "tools": ["find_references", "analyze_structure"],
    },
}
```

---

## 既存コンポーネント（v3.5からの継続）

### QuestionCategory（質問カテゴリ）

4つのカテゴリでクエリを分類：

| カテゴリ | 説明 | 例 |
|----------|------|-----|
| `A_SYNTAX` | 構文・定義の質問 | 「関数の引数は？」 |
| `B_REFERENCE` | 参照・使用箇所の質問 | 「どこで使われている？」 |
| `C_SEMANTIC` | 意味・概念の質問 | 「なぜこの設計？」 |
| `D_IMPACT` | 影響範囲の質問 | 「変更したら何に影響？」 |

### IntentType（インテント分類）

| インテント | 条件 | 例 |
|------------|------|-----|
| `IMPLEMENT` | 作るものが明示 | 「ログイン機能を実装して」 |
| `MODIFY` | 既存コードの変更 | 「バグを直して」 |
| `INVESTIGATE` | 調査・理解のみ | 「どこで定義？」 |
| `QUESTION` | 一般的な質問 | 「Pythonとは？」 |

### DecisionLog（v3.6更新）

```python
@dataclass
class DecisionLog:
    """ルーターの判断記録（v3.6）"""
    session_id: str
    query: str
    timestamp: str

    # 分類結果
    intent: str
    category: str
    category_confidence: float

    # v3.6: QueryFrame情報
    query_frame: dict | None = None
    missing_slots: list[str] = field(default_factory=list)
    risk_level: str = "LOW"
    slot_based_tools: list[str] = field(default_factory=list)

    # ツール選択
    selected_tools: list[str] = field(default_factory=list)
    tool_selection_reason: str = ""

    # 実行結果
    tool_results: list[dict] = field(default_factory=list)
    fallback_triggered: bool = False
    fallback_reason: str | None = None

    # フェーズ情報
    final_phase: str = ""
    phase_transitions: list[str] = field(default_factory=list)
```

---

## フロー図（v3.6）

```
ユーザークエリ
     │
     ▼
┌──────────────────┐
│   start_session  │
│   (Intent判定)   │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ QueryDecomposer  │ ← LLMがスロット抽出
│ (extraction_prompt)
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ set_query_frame  │ ← サーバーがQuote検証
│ (validate_slot)  │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  risk_level判定  │
│  (HIGH/MED/LOW)  │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  Investigation   │ ← 欠損スロットに基づくツール推奨
│    Guidance      │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│   EXPLORATION    │ ← code-intelツール使用
│ (find_definitions│   slot_evidence蓄積
│  find_references)│
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│submit_understanding│
│ - NL→Symbol整合性 │
│ - slot_evidence確認│
│ - 動的要件チェック │
└────────┬─────────┘
         │
    ┌────┴────┐
    │         │
high│         │low
    ▼         ▼
┌───────┐  ┌───────────┐
│ READY │  │  SEMANTIC │ ← devrag使用（HYPOTHESIS）
└───────┘  └─────┬─────┘
                 │
                 ▼
           ┌───────────┐
           │VERIFICATION│ ← HYPOTHESISを検証
           └─────┬─────┘
                 │
                 ▼
           ┌───────────┐
           │   READY   │
           └───────────┘
```

---

## Routerの責務変更（v3.6）

### Before (v3.5): 意思決定者
- 「何を調べるべきか」を判断
- カテゴリ分類に基づくツール選択

### After (v3.6): 交通整理
- 「何が足りないか」に基づくツール推奨
- QueryFrameの欠損スロットがツール選択を駆動

```python
def create_plan_from_frame(
    self,
    frame: QueryFrame,
    intent: str,
    session_id: str,
) -> RoutingDecision:
    """
    v3.6: QueryFrameに基づいて実行計画を作成。
    """
    # 欠損スロットを取得
    missing_slots = frame.get_missing_slots()

    # 欠損スロットに基づいてツールを推奨
    guidance = generate_investigation_guidance(missing_slots)

    # リスクレベルに応じた要件を設定
    requirements = get_exploration_requirements(
        risk_level=assess_risk_level(frame, intent),
        intent=intent,
    )

    return RoutingDecision(
        recommended_tools=guidance["recommended_tools"],
        missing_slots=missing_slots,
        requirements=requirements,
        hints=guidance["hints"],
    )
```

---

## 設計原則

### 1. LLMに判断をさせない

- **v3.5**: パターンマッチングによるカテゴリ分類
- **v3.6追加**: Quote検証によるLLM出力の検証
  - LLMは「抽出」のみ担当
  - 「検証」はサーバーが担当

### 2. 動的要件

risk_levelに応じて探索要件を調整：

| risk_level | symbols | entry_points | files | patterns |
|------------|---------|--------------|-------|----------|
| HIGH       | 5       | 2            | 4     | 2        |
| MEDIUM     | 3       | 1            | 2     | 1        |
| LOW        | 1       | 0            | 1     | 0        |

### 3. FACT/HYPOTHESIS区別

- **FACT**: EXPLORATIONで確定した情報（信頼性高）
- **HYPOTHESIS**: SEMANTICで推測した情報（検証必須）

HYPOTHESISのままREADYには進めない：

```python
def validate_for_ready(frame: QueryFrame) -> list[str]:
    """HYPOTHESISスロット/シンボルが残っていればブロック"""
    errors = []
    for slot, source in frame.slot_source.items():
        if source == SlotSource.HYPOTHESIS:
            errors.append(f"Slot '{slot}' is still HYPOTHESIS")
    return errors
```

### 4. 追跡可能性

DecisionLogにQueryFrame情報を追加：
- missing_slots: 欠損していたスロット
- risk_level: 判定されたリスクレベル
- slot_based_tools: スロットに基づいて推奨されたツール

---

## 使用例

### v3.6 フロー

```python
# 1. セッション開始
result = await call_tool("start_session", {
    "intent": "MODIFY",
    "query": "ログイン機能でパスワードが空のときエラーが出ない"
})
# → extraction_prompt が返る

# 2. QueryFrame設定（LLMが抽出した結果を渡す）
result = await call_tool("set_query_frame", {
    "target_feature": {"value": "ログイン機能", "quote": "ログイン機能"},
    "trigger_condition": {"value": "パスワードが空", "quote": "パスワードが空"},
    "observed_issue": {"value": "エラーが出ない", "quote": "エラーが出ない"},
})
# → risk_level: "MEDIUM", missing_slots: ["desired_action"]
#   investigation_guidance: ["find_references", "analyze_structure"]

# 3. 探索
# → find_definitions, find_references を使用
# → slot_evidence を蓄積

# 4. 探索結果提出
result = await call_tool("submit_understanding", {
    "symbols_identified": ["LoginService", "AuthController", "UserValidator"],
    "entry_points": ["LoginService.authenticate()"],
    "existing_patterns": ["Service + Validator"],
    "files_analyzed": ["auth/login_service.py", "auth/controller.py"],
})
# → confidence: "high", next_phase: "READY"
```

---

## 関連ドキュメント

- [DESIGN_v3.6.md](./DESIGN_v3.6.md) - v3.6設計詳細
- [ARCHITECTURE.md](./ARCHITECTURE.md) - システム全体設計
- [code_intel_server.py](../code_intel_server.py) - MCPサーバー実装
- [session.py](../tools/session.py) - セッション管理
- [query_frame.py](../tools/query_frame.py) - QueryFrame実装
- [outcome_log.py](../tools/outcome_log.py) - 結果ログ
